"""The durable billing-resolution workflow.

One workflow per ticket. It orchestrates but holds no secrets and moves no money
directly — the money-moving step is an activity routed to the execute-only task
queue. The approval gate is a durable wait: the workflow parks on a signal from
the approval service and survives worker restarts and deploys for as long as it
takes a human to click.

The pure core (``decide``, ``PreparedAction``, ``derive_idempotency_key``) is
imported through the sandbox pass-through: it is deterministic, does no I/O, and
reads no clock, so it is safe to run inside the workflow.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from .activities import facts_from_dict, request_from_dict
    from .domain import Disposition, PreparedAction
    from .policy import decide

# Custom search attribute the approval inbox filters on to list parked workflows.
# Register it once per Temporal namespace (see docs/RUNBOOK.md).
FARTHING_STATE = SearchAttributeKey.for_keyword("farthing_state")


@dataclass
class TicketInput:
    ticket_id: str
    raw_text: str
    subscription_id: str
    invoice_id: str | None
    charge_id: str | None


@dataclass
class Approval:
    token: dict[str, Any]  # a signed ApprovalToken as a dict, issued by the approval service


@dataclass
class TicketResult:
    disposition: str
    amount_minor: int
    currency: str
    executed_refund_id: str | None
    reply: str


@workflow.defn
class BillingResolutionWorkflow:
    def __init__(self) -> None:
        self._approval: Approval | None = None
        self._denied = False
        self._prepared: dict[str, Any] | None = None

    @workflow.signal
    async def approve(self, approval: Approval) -> None:
        self._approval = approval

    @workflow.signal
    async def deny(self) -> None:
        self._denied = True

    @workflow.query
    def pending(self) -> bool:
        return self._approval is None and not self._denied

    async def _reply(self, disposition: str, amount_minor: int, currency: str,
                     raw_text: str) -> str:
        """Draft the customer reply with a BOUNDED retry and a templated fallback.
        This step is cosmetic and runs after the refund and ledger commit — it must
        never be able to wedge (via Temporal's default unlimited retries) a workflow
        whose money step already succeeded."""
        try:
            return await workflow.execute_activity(
                "draft_reply", args=[disposition, amount_minor, currency, raw_text],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=4),
            )
        except ActivityError:
            return f"Your billing request has been processed (status: {disposition})."

    @workflow.query
    def prepared_action(self) -> dict[str, Any] | None:
        """Server-side source of truth for the approval service: the exact
        previewed action (including the signed charge). The approval inbox loads
        the amount and charge from HERE, never from the client, so a request body
        cannot inflate the amount or redirect the charge."""
        return self._prepared

    @workflow.run
    async def run(self, ticket: TicketInput) -> TicketResult:
        now_ts = int(workflow.now().timestamp())

        req_d = await workflow.execute_activity(
            "extract", ticket.raw_text,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        facts_d = await workflow.execute_activity(
            "fetch_facts", args=[ticket.subscription_id, ticket.invoice_id, now_ts],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

        req = request_from_dict(req_d)
        facts = facts_from_dict(facts_d)
        decision = decide(req, facts)  # pure, deterministic, in-workflow

        # Non-eligible dispositions never reach an approval gate or a refund.
        if decision.disposition is not Disposition.ELIGIBLE or decision.amount_minor == 0:
            reply = await self._reply(
                decision.disposition.value, decision.amount_minor,
                decision.currency, req.raw_text,
            )
            return TicketResult(
                disposition=decision.disposition.value, amount_minor=decision.amount_minor,
                currency=decision.currency, executed_refund_id=None, reply=reply,
            )

        prepared = PreparedAction(
            ticket_id=ticket.ticket_id, action_type=req.action_type,
            subscription_id=ticket.subscription_id, invoice_id=ticket.invoice_id,
            amount_minor=decision.amount_minor, currency=decision.currency,
            policy_version=decision.policy_version,
            charge_id=facts.charge_id,  # refund the charge whose facts set the amount
            reason_codes=decision.reason_codes,
        )
        self._prepared = asdict(prepared)  # publish for the approval service to read + sign

        # Publish the pending prepared action + idempotency key for the approval
        # service to render and sign, then park until a human decides.
        workflow.upsert_search_attributes(  # pyright: ignore[reportUnknownMemberType]  # temporalio stub gap
            [FARTHING_STATE.value_set("awaiting_approval")]
        )
        await workflow.wait_condition(lambda: self._approval is not None or self._denied)

        if self._denied or self._approval is None:
            reply = await self._reply("denied_by_reviewer", 0, decision.currency, req.raw_text)
            return TicketResult("denied_by_reviewer", 0, decision.currency, None, reply)

        # Execute on the EXECUTE task queue only — the agent worker cannot run this.
        # execute_refund is idempotent by construction (durable reservation + Stripe
        # idempotency key + find_refund_by_key), so a generous retry count carries no
        # double-pay risk and keeps a transient Stripe/Postgres blip from dumping the
        # ticket into the manual reconcile path.
        try:
            refund = await workflow.execute_activity(
                "execute_refund",
                args=[self._approval.token, asdict(decision), asdict(prepared)],
                task_queue="farthing-execute",
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=6, non_retryable_error_types=["ApplicationError"]),
            )
        except ActivityError as e:
            # Terminal execution failure (guard refusal or exhausted retries). Money
            # did not move; dead-letter the ticket for human resolution instead of
            # crashing the workflow. The nightly reconciliation also surfaces it.
            await workflow.execute_activity(
                "record_dead_letter",
                args=[ticket.ticket_id, "execution_failed", decision.amount_minor,
                      decision.currency, str(e)],
                task_queue="farthing-execute",
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            reply = await self._reply("execution_failed", 0, decision.currency, req.raw_text)
            return TicketResult("dead_lettered", 0, decision.currency, None, reply)

        reply = await self._reply("refunded", decision.amount_minor, decision.currency, req.raw_text)
        return TicketResult(
            disposition="refunded", amount_minor=decision.amount_minor,
            currency=decision.currency, executed_refund_id=refund.get("id"), reply=reply,
        )


@workflow.defn
class ReconciliationWorkflow:
    """Fired by a nightly Temporal Schedule. Reconciles the trailing 24h+margin of
    ledger refunds against Stripe; a non-clean report is logged as an incident by
    the activity."""

    @workflow.run
    async def run(self, window_seconds: int = 90_000) -> dict[str, Any]:  # ~25h, overlaps for safety
        end_ts = int(workflow.now().timestamp())
        start_ts = end_ts - window_seconds
        return await workflow.execute_activity(
            "reconcile_window", args=[start_ts, end_ts],
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
