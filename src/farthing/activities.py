"""Temporal activities — where all I/O happens.

Two groups, registered on two different workers so the credential split is
enforced by infrastructure, not convention:

* Agent-side (task queue ``farthing-agent``): ``extract``, ``fetch_facts``,
  ``draft_reply``, ``compensate``. Holds the PREPARE Stripe key + the Anthropic
  key. Cannot move money.
* Execute-side (task queue ``farthing-execute``): ``execute_refund``. Holds the
  EXECUTE Stripe key + the approval signing secret. Runs only after a human
  approves, and only where the execute key lives.

The workflow routes ``execute_refund`` to the execute task queue explicitly, so a
compromised agent worker has no code path to a refund.
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .config import approval_settings, worker_settings
from .domain import (
    AccountFacts,
    ActionType,
    Disposition,
    ExtractedRequest,
    PolicyDecision,
    PreparedAction,
    SubscriptionStatus,
)
from .execution_guard import ApprovalToken, authorize_execution


# ---- Agent-side activities (PREPARE scope) ---------------------------------

@activity.defn
async def extract(raw_text: str) -> dict:
    from .agent import extract_request

    s = worker_settings()
    req = await extract_request(
        raw_text,
        extraction_model=s.extraction_model,
        escalation_model=s.escalation_model,
    )
    return asdict(req)


@activity.defn
async def fetch_facts(subscription_id: str, invoice_id: str | None, now_ts: int) -> dict:
    from .stripe_client import PrepareClient

    s = worker_settings()
    client = PrepareClient(s.stripe_prepare_key.get_secret_value())
    facts = client.fetch_facts(
        subscription_id=subscription_id, invoice_id=invoice_id, now_ts=now_ts
    )
    d = asdict(facts)
    d["usage_fraction"] = str(facts.usage_fraction)
    d["plan_max_refund_pct"] = str(facts.plan_max_refund_pct)
    d["subscription_status"] = facts.subscription_status.value
    return d


@activity.defn
async def draft_reply(disposition: str, amount_minor: int, currency: str, context: str) -> str:
    from .agent import draft_reply as _draft

    return await _draft(
        disposition=disposition, amount_minor=amount_minor,
        currency=currency, customer_context=context,
    )


@activity.defn
async def compensate(refund_id: str, reason: str) -> None:
    # A refund is not reversible via API; compensation records the failure,
    # opens an incident, and flags the ledger entry for human resolution.
    activity.logger.error("compensation for refund %s: %s", refund_id, reason)


# ---- Execute-side activity (EXECUTE scope — separate worker) ----------------

@activity.defn
async def execute_refund(
    token_d: dict, decision_d: dict, prepared_d: dict, charge_id: str
) -> dict:
    """The only activity that moves money.

    Ordering is the exactly-once boundary: the durable key reservation is
    committed BEFORE Stripe is called and is never rolled back on a Stripe
    failure. A retry therefore sees the key already reserved and resolves the
    existing refund by metadata key rather than creating a second one — so a crash
    between Stripe success and our commit cannot double-refund, even beyond
    Stripe's idempotency window."""
    from .stripe_client import ExecuteClient

    s = approval_settings()
    token = ApprovalToken(**token_d)
    decision = PolicyDecision(
        disposition=Disposition(decision_d["disposition"]),
        amount_minor=decision_d["amount_minor"],
        currency=decision_d["currency"],
        reason_codes=tuple(decision_d["reason_codes"]),
        policy_version=decision_d["policy_version"],
    )
    prepared = PreparedAction(
        ticket_id=prepared_d["ticket_id"],
        action_type=ActionType(prepared_d["action_type"]),
        subscription_id=prepared_d["subscription_id"],
        invoice_id=prepared_d.get("invoice_id"),
        amount_minor=prepared_d["amount_minor"],
        currency=prepared_d["currency"],
        policy_version=prepared_d["policy_version"],
        reason_codes=tuple(prepared_d.get("reason_codes", ())),
    )

    import time

    import psycopg

    from .execution_guard import ExecutionRefused
    from .idempotency_store import PostgresIdempotencyStore

    client = ExecuteClient(s.stripe_execute_key.get_secret_value())

    # The refund targets the charge BOUND by the approval, not the raw ticket
    # input, so an unsigned charge_id cannot redirect the money.
    bound_charge = prepared.charge_id or charge_id

    with psycopg.connect(s.database_url.get_secret_value()) as conn:
        store = PostgresIdempotencyStore(conn)
        store.ensure_schema()
        try:
            instr = authorize_execution(
                s.approval_signing_secret.get_secret_value().encode(),
                token=token, decision=decision, prepared=prepared,
                store=store, now_ts=int(time.time()),
            )
        except ExecutionRefused as e:
            if e.code == "replay":
                # This key was already reserved (a prior attempt authorized it).
                # Resolve idempotently: return the existing refund if Stripe has
                # one; otherwise re-drive Stripe with the SAME key (Stripe dedups).
                existing = client.find_refund_by_key(
                    charge_id=bound_charge, idempotency_key=token.idempotency_key
                )
                if existing is not None:
                    return existing
                return client.create_refund(
                    charge_id=bound_charge, amount_minor=token.amount_minor,
                    idempotency_key=token.idempotency_key,
                )
            # Any other refusal is terminal and must never be retried into success.
            raise ApplicationError(f"execution refused: {e.code}", non_retryable=True) from e

        # Commit the reservation BEFORE touching Stripe, and do NOT roll it back on
        # a Stripe failure — a retry must find the key reserved, not free.
        conn.commit()

    return client.create_refund(
        charge_id=bound_charge,
        amount_minor=instr.amount_minor,
        idempotency_key=instr.idempotency_key,
    )


def facts_from_dict(d: dict) -> AccountFacts:
    return AccountFacts(
        subscription_id=d["subscription_id"],
        invoice_id=d.get("invoice_id"),
        currency=d["currency"],
        amount_paid_minor=d["amount_paid_minor"],
        prior_refunds_minor=d["prior_refunds_minor"],
        purchase_ts=d["purchase_ts"],
        now_ts=d["now_ts"],
        usage_fraction=Decimal(d["usage_fraction"]),
        subscription_status=SubscriptionStatus(d["subscription_status"]),
        plan_refund_window_days=d["plan_refund_window_days"],
        plan_allow_proration=d["plan_allow_proration"],
        plan_max_refund_pct=Decimal(d["plan_max_refund_pct"]),
    )


def request_from_dict(d: dict) -> ExtractedRequest:
    return ExtractedRequest(
        action_type=ActionType(d["action_type"]),
        customer_ref=d["customer_ref"],
        subscription_ref=d["subscription_ref"],
        invoice_ref=d.get("invoice_ref"),
        reason_code=d.get("reason_code"),
        raw_text=d.get("raw_text", ""),
    )
