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

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .activities import facts_from_dict, request_from_dict
    from .domain import Disposition, PreparedAction
    from .idempotency import derive_idempotency_key
    from .policy import decide


@dataclass
class TicketInput:
    ticket_id: str
    raw_text: str
    subscription_id: str
    invoice_id: str | None
    charge_id: str | None


@dataclass
class Approval:
    token: dict  # a signed ApprovalToken as a dict, issued by the approval service


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

    @workflow.signal
    async def approve(self, approval: Approval) -> None:
        self._approval = approval

    @workflow.signal
    async def deny(self) -> None:
        self._denied = True

    @workflow.query
    def pending(self) -> bool:
        return self._approval is None and not self._denied

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
            reply = await workflow.execute_activity(
                "draft_reply",
                args=[decision.disposition.value, decision.amount_minor,
                      decision.currency, req.raw_text],
                start_to_close_timeout=timedelta(seconds=60),
            )
            return TicketResult(
                disposition=decision.disposition.value, amount_minor=decision.amount_minor,
                currency=decision.currency, executed_refund_id=None, reply=reply,
            )

        prepared = PreparedAction(
            ticket_id=ticket.ticket_id, action_type=req.action_type,
            subscription_id=ticket.subscription_id, invoice_id=ticket.invoice_id,
            amount_minor=decision.amount_minor, currency=decision.currency,
            policy_version=decision.policy_version, reason_codes=decision.reason_codes,
        )
        idem_key = derive_idempotency_key(
            workflow.info().workflow_id, "execute_refund",
            {"amount": decision.amount_minor, "currency": decision.currency,
             "ticket": ticket.ticket_id},
        )

        # Publish the pending prepared action + idempotency key for the approval
        # service to render and sign, then park until a human decides.
        workflow.upsert_search_attributes({"farthing_state": ["awaiting_approval"]})
        await workflow.wait_condition(lambda: self._approval is not None or self._denied)

        if self._denied or self._approval is None:
            reply = await workflow.execute_activity(
                "draft_reply",
                args=["denied_by_reviewer", 0, decision.currency, req.raw_text],
                start_to_close_timeout=timedelta(seconds=60),
            )
            return TicketResult("denied_by_reviewer", 0, decision.currency, None, reply)

        # Execute on the EXECUTE task queue only — the agent worker cannot run this.
        refund = await workflow.execute_activity(
            "execute_refund",
            args=[self._approval.token, asdict(decision), asdict(prepared), ticket.charge_id],
            task_queue="farthing-execute",
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3, non_retryable_error_types=["ApplicationError"]),
        )

        reply = await workflow.execute_activity(
            "draft_reply",
            args=["refunded", decision.amount_minor, decision.currency, req.raw_text],
            start_to_close_timeout=timedelta(seconds=60),
        )
        return TicketResult(
            disposition="refunded", amount_minor=decision.amount_minor,
            currency=decision.currency, executed_refund_id=refund.get("id"), reply=reply,
        )
