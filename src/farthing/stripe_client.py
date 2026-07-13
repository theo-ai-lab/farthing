"""Stripe adapters.

Two deliberately separate surfaces mirror the two restricted keys:

* :class:`PrepareClient` (read + refund-prepare) — fetches the facts the policy
  engine needs and never calls a money-moving endpoint.
* :class:`ExecuteClient` (refund-execute) — the ONLY place a Stripe refund is
  actually created, always with a caller-supplied deterministic idempotency key
  so a retried activity re-uses the same key rather than minting a new one.

The pinned Stripe major (15.x) fixes the API version; the pinned API version is
recorded on every ledger entry so hash-chained payloads stay byte-stable across
library upgrades.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import stripe

from .domain import AccountFacts, RefundResult, SubscriptionStatus

# Pin the API version alongside the library major so behavior is reproducible and
# auditable. Record this string in the ledger with every executed refund.
STRIPE_API_VERSION = "2026-06-30.acacia"

_STATUS_MAP = {
    "active": SubscriptionStatus.ACTIVE,
    "trialing": SubscriptionStatus.TRIALING,
    "past_due": SubscriptionStatus.PAST_DUE,
    "canceled": SubscriptionStatus.CANCELED,
    "unpaid": SubscriptionStatus.PAST_DUE,
    "incomplete": SubscriptionStatus.PAST_DUE,
}


class PrepareClient:
    """Read-only + prepare scope. Builds AccountFacts. Cannot move money."""

    def __init__(self, api_key: str) -> None:
        self._c = stripe.StripeClient(api_key, stripe_version=STRIPE_API_VERSION)

    def fetch_facts(self, *, subscription_id: str, invoice_id: str | None, now_ts: int) -> AccountFacts:
        # Stripe SDK objects are dynamically shaped and only partially typed by the
        # stubs; hold them as Any at this boundary and narrow every value into the
        # strongly-typed AccountFacts below. The `.v1.` accessors are the current,
        # non-deprecated service surface in the pinned Stripe major.
        sub: Any = self._c.v1.subscriptions.retrieve(subscription_id)
        invoice: Any = self._c.v1.invoices.retrieve(invoice_id) if invoice_id else None

        amount_paid = int(invoice.amount_paid) if invoice else 0
        currency = str(invoice.currency if invoice else sub.currency or "usd").upper()

        charge_id = getattr(invoice, "charge", None) if invoice is not None else None
        prior_refunds = 0
        if charge_id:
            # Auto-paginate: a charge with >100 prior refunds must not be
            # undercounted, or the cumulative ceiling would be inflated.
            for r in self._c.v1.refunds.list({"charge": charge_id, "limit": 100}).auto_paging_iter():
                prior_refunds += int(r.amount)

        # Walk the dynamically-shaped subscription payload defensively; each hop
        # stays Any so a missing field yields None (guarded) instead of forcing a
        # typed empty-dict literal into the expression.
        sub_items: Any = sub.get("items")
        data: Any = sub_items.get("data") if sub_items else None
        plan: Any = data[0]["price"] if data else None
        meta_raw: Any = plan.get("metadata") if plan else None
        meta: dict[str, Any] = dict(meta_raw) if meta_raw else {}

        return AccountFacts(
            subscription_id=subscription_id,
            invoice_id=invoice_id,
            currency=currency,
            amount_paid_minor=amount_paid,
            prior_refunds_minor=prior_refunds,
            purchase_ts=int(sub.start_date or sub.created),
            now_ts=now_ts,
            usage_fraction=Decimal(meta.get("usage_fraction", "0")),
            subscription_status=_STATUS_MAP.get(sub.status, SubscriptionStatus.PAST_DUE),
            plan_refund_window_days=int(meta.get("refund_window_days", "14")),
            plan_allow_proration=meta.get("allow_proration", "true") == "true",
            plan_max_refund_pct=Decimal(meta.get("max_refund_pct", "1.0")),
            charge_id=charge_id,  # the refund MUST target this exact charge
        )

    def list_refunds_in_window(self, *, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        """All refunds Stripe recorded in a window, for nightly reconciliation.
        Auto-paginates; carries the Farthing key from metadata so reconcile() can
        match (and flag keyless/off-ledger refunds)."""
        out: list[dict[str, Any]] = []
        page: Any = self._c.v1.refunds.list({"created": {"gte": start_ts, "lte": end_ts}, "limit": 100})
        for r in page.auto_paging_iter():
            out.append({
                "id": r.id, "amount": int(r.amount), "currency": r.currency,
                "idempotency_key": dict(getattr(r, "metadata", {})).get("farthing_idempotency_key"),
            })
        return out


class ExecuteClient:
    """Refund-execute scope. The ONLY money-moving surface in the system."""

    def __init__(self, api_key: str) -> None:
        self._c = stripe.StripeClient(api_key, stripe_version=STRIPE_API_VERSION)

    def create_refund(
        self, *, charge_id: str, amount_minor: int, idempotency_key: str, reason: str = "requested_by_customer"
    ) -> RefundResult:
        """Create a refund on the bound charge. The idempotency key is
        caller-derived and deterministic, so any retry within Stripe's window
        collapses into the original refund. The key is ALSO persisted into refund
        metadata so a retry after that window (or a crash) can resolve the existing
        refund by key instead of creating a duplicate — see ``find_refund_by_key``.
        Stripe always refunds in the charge's own currency; the guard validates
        that the decided currency equals the charge/invoice currency upstream."""
        params: Any = {
            "charge": charge_id, "amount": amount_minor, "reason": reason,
            "metadata": {"farthing_idempotency_key": idempotency_key},
        }
        refund: Any = self._c.v1.refunds.create(params, {"idempotency_key": idempotency_key})
        return {"id": str(refund.id), "amount": int(refund.amount), "status": str(refund.status),
                "currency": str(refund.currency), "idempotency_key": idempotency_key,
                "api_version": STRIPE_API_VERSION}

    def find_refund_by_key(self, *, charge_id: str, idempotency_key: str) -> RefundResult | None:
        """Look up an already-created refund by the Farthing key stored in metadata.
        Used to resolve a retried execution idempotently (return the existing refund
        rather than create a second one) even after Stripe's idempotency window."""
        refunds: Any = self._c.v1.refunds.list({"charge": charge_id, "limit": 100})
        for r in refunds.auto_paging_iter():
            if dict(getattr(r, "metadata", {})).get("farthing_idempotency_key") == idempotency_key:
                return {"id": str(r.id), "amount": int(r.amount), "status": str(r.status),
                        "currency": str(r.currency), "idempotency_key": idempotency_key,
                        "api_version": STRIPE_API_VERSION}
        return None
