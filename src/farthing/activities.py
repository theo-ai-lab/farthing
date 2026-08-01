"""Temporal activities — where all I/O happens.

Two groups, registered on two different workers so the credential split is
enforced by infrastructure, not convention:

* Agent-side (task queue ``farthing-agent``): ``extract``, ``fetch_facts``,
  ``draft_reply``. Holds the PREPARE Stripe key + the Anthropic key. Cannot
  move money.
* Execute-side (task queue ``farthing-execute``): ``execute_refund``. Holds the
  EXECUTE Stripe key + the approval signing secret. Runs only after a human
  approves, and only where the execute key lives.

The workflow routes ``execute_refund`` to the execute task queue explicitly, so a
compromised agent worker has no code path to a refund.
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any

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
    RefundResult,
    SubscriptionStatus,
)
from .execution_guard import ApprovalToken

if TYPE_CHECKING:
    from .db import DBConn
    from .idempotency_store import PostgresIdempotencyStore

# Bootstrap the execute-side schema once per worker process, not on every refund.
_schema_ready = False


def _ensure_schema_once(store: PostgresIdempotencyStore, conn: DBConn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    from . import pg_ledger

    store.ensure_schema()
    pg_ledger.ensure_schema(conn)
    conn.commit()
    _schema_ready = True


# ---- Agent-side activities (PREPARE scope) ---------------------------------

@activity.defn
async def extract(raw_text: str) -> dict[str, Any]:
    from .agent import extract_request

    s = worker_settings()
    req = await extract_request(
        raw_text,
        extraction_model=s.extraction_model,
        escalation_model=s.escalation_model,
    )
    return asdict(req)


@activity.defn
async def fetch_facts(subscription_id: str, invoice_id: str | None, now_ts: int) -> dict[str, Any]:
    import stripe

    from .stripe_client import PrepareClient

    s = worker_settings()
    client = PrepareClient(s.stripe_prepare_key.get_secret_value())
    try:
        facts = client.fetch_facts(
            subscription_id=subscription_id, invoice_id=invoice_id, now_ts=now_ts
        )
    except stripe.InvalidRequestError as e:
        # Deterministic 4xx (bad/nonexistent id): retrying will never succeed, so
        # fail immediately instead of burning the retry budget.
        raise ApplicationError(f"invalid Stripe request: {e}", non_retryable=True) from e
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
async def reconcile_window(start_ts: int, end_ts: int) -> dict[str, Any]:
    """Nightly reconciliation: compare committed ledger refunds against Stripe for
    the window and return the report. A non-clean report is an incident. Read-only
    (PREPARE scope), so it runs on the agent worker. Wire it to a Temporal Schedule
    (see worker.setup_reconciliation_schedule)."""
    import psycopg

    from . import pg_ledger
    from .reconcile import reconcile
    from .stripe_client import PrepareClient

    s = worker_settings()
    if s.reconcile_database_url is None:
        # Reconciliation is the safety net; it must fail loud and non-retryable
        # rather than crash nightly with an AttributeError on an unset optional.
        raise ApplicationError(
            "FARTHING_RECONCILE_DATABASE_URL is not configured — reconciliation cannot run",
            non_retryable=True,
        )
    with psycopg.connect(s.reconcile_database_url.get_secret_value()) as conn:
        ledger_entries = pg_ledger.read_refunded_entries(conn, start_ts=start_ts, end_ts=end_ts)
    stripe_refunds = PrepareClient(s.stripe_prepare_key.get_secret_value()).list_refunds_in_window(
        start_ts=start_ts, end_ts=end_ts
    )
    report = reconcile(ledger_entries, stripe_refunds, window_start_ts=start_ts, window_end_ts=end_ts)
    if not report.clean:
        activity.logger.error(
            "RECONCILIATION INCIDENT %s..%s: unmatched_stripe=%s unmatched_ledger=%s "
            "mismatched=%s duplicate_keys=%s malformed=%s",
            start_ts, end_ts, report.unmatched_in_stripe, report.unmatched_in_ledger,
            report.mismatched, report.duplicate_keys, report.malformed,
        )
    return {"clean": report.clean, "matched": report.matched,
            "unmatched_in_stripe": list(report.unmatched_in_stripe),
            "unmatched_in_ledger": list(report.unmatched_in_ledger),
            "mismatched": list(report.mismatched),
            "duplicate_keys": list(report.duplicate_keys),
            "malformed": list(report.malformed)}


# ---- Execute-side activity (EXECUTE scope — separate worker) ----------------

@activity.defn
async def execute_refund(
    token_d: dict[str, Any], decision_d: dict[str, Any], prepared_d: dict[str, Any]
) -> RefundResult:
    """The only activity that moves money.

    This is wiring, not logic: it binds the real settings, database and Stripe
    client to :func:`farthing.settlement.settle_refund`, which owns the ordering
    that makes execution exactly-once (reservation + audit row committed together,
    BEFORE Stripe, never rolled back on a Stripe failure). That sequence is killed
    at every one of its boundaries in ``tests/test_crash_matrix.py``; keeping it
    out of this activity is what makes that possible."""
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
        charge_id=prepared_d.get("charge_id"),  # keep the signed charge across the boundary
        reason_codes=tuple(prepared_d.get("reason_codes", ())),
    )

    import time

    import psycopg

    from .execution_guard import ExecutionRefused
    from .idempotency_store import PostgresIdempotencyStore
    from .observability import current_trace_id
    from .settlement import settle_refund
    from .stripe_client import STRIPE_API_VERSION, ExecuteClient

    client = ExecuteClient(s.stripe_execute_key.get_secret_value())

    with psycopg.connect(s.database_url.get_secret_value()) as conn:
        store = PostgresIdempotencyStore(conn)
        _ensure_schema_once(store, conn)
        try:
            return settle_refund(
                s.approval_signing_secret.get_secret_value().encode(),
                token=token, decision=decision, prepared=prepared,
                conn=conn, store=store, gateway=client,
                now_ts=int(time.time()), trace_id=current_trace_id(),
                api_version=STRIPE_API_VERSION,
            )
        except ExecutionRefused as e:
            # Every refusal is terminal: retrying a bad signature, a stale token or
            # a drifted amount can only fail again. Money did not move.
            raise ApplicationError(f"execution refused: {e.code}", non_retryable=True) from e


@activity.defn
async def record_dead_letter(
    ticket_id: str, reason_code: str, amount_minor: int, currency: str, detail: str
) -> None:
    """Persist a terminally-failed refund execution to the dead-letter queue for
    human resolution. Runs on the execute worker (has the ledger DB)."""
    import time

    import psycopg

    from . import dead_letter

    s = approval_settings()
    entry = dead_letter.build_dead_letter(
        timestamp=int(time.time()), ticket_id=ticket_id, reason_code=reason_code,
        amount_minor=amount_minor, currency=currency, detail=detail,
    )
    with psycopg.connect(s.database_url.get_secret_value()) as conn:
        dead_letter.ensure_schema(conn)
        dead_letter.record_dead_letter(conn, entry)
        conn.commit()


def facts_from_dict(d: dict[str, Any]) -> AccountFacts:
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
        charge_id=d.get("charge_id"),
    )


def request_from_dict(d: dict[str, Any]) -> ExtractedRequest:
    return ExtractedRequest(
        action_type=ActionType(d["action_type"]),
        customer_ref=d["customer_ref"],
        subscription_ref=d["subscription_ref"],
        invoice_ref=d.get("invoice_ref"),
        reason_code=d.get("reason_code"),
        raw_text=d.get("raw_text", ""),
    )
