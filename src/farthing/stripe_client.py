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

import stripe

from .domain import AccountFacts, SubscriptionStatus

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
        sub = self._c.subscriptions.retrieve(subscription_id)
        invoice = self._c.invoices.retrieve(invoice_id) if invoice_id else None

        amount_paid = int(invoice.amount_paid) if invoice else 0
        currency = (invoice.currency if invoice else sub.currency or "usd").upper()

        charge_id = getattr(invoice, "charge", None) if invoice is not None else None
        prior_refunds = 0
        if charge_id:
            # Auto-paginate: a charge with >100 prior refunds must not be
            # undercounted, or the cumulative ceiling would be inflated.
            for r in self._c.refunds.list({"charge": charge_id, "limit": 100}).auto_paging_iter():
                prior_refunds += int(r.amount)

        plan = sub["items"]["data"][0]["price"] if sub.get("items") else {}
        meta = dict(plan.get("metadata", {})) if plan else {}

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


class ExecuteClient:
    """Refund-execute scope. The ONLY money-moving surface in the system."""

    def __init__(self, api_key: str) -> None:
        self._c = stripe.StripeClient(api_key, stripe_version=STRIPE_API_VERSION)

    def create_refund(
        self, *, charge_id: str, amount_minor: int, idempotency_key: str, reason: str = "requested_by_customer"
    ) -> dict:
        """Create a refund on the bound charge. The idempotency key is
        caller-derived and deterministic, so any retry within Stripe's window
        collapses into the original refund. The key is ALSO persisted into refund
        metadata so a retry after that window (or a crash) can resolve the existing
        refund by key instead of creating a duplicate — see ``find_refund_by_key``.
        Stripe always refunds in the charge's own currency; the guard validates
        that the decided currency equals the charge/invoice currency upstream."""
        refund = self._c.refunds.create(
            {
                "charge": charge_id, "amount": amount_minor, "reason": reason,
                "metadata": {"farthing_idempotency_key": idempotency_key},
            },
            {"idempotency_key": idempotency_key},
        )
        return {"id": refund.id, "amount": int(refund.amount), "status": refund.status,
                "currency": refund.currency, "idempotency_key": idempotency_key,
                "api_version": STRIPE_API_VERSION}

    def find_refund_by_key(self, *, charge_id: str, idempotency_key: str) -> dict | None:
        """Look up an already-created refund by the Farthing key stored in metadata.
        Used to resolve a retried execution idempotently (return the existing refund
        rather than create a second one) even after Stripe's idempotency window."""
        refunds = self._c.refunds.list({"charge": charge_id, "limit": 100})
        for r in refunds.auto_paging_iter():
            if dict(getattr(r, "metadata", {})).get("farthing_idempotency_key") == idempotency_key:
                return {"id": r.id, "amount": int(r.amount), "status": r.status,
                        "currency": r.currency, "idempotency_key": idempotency_key,
                        "api_version": STRIPE_API_VERSION}
        return None
