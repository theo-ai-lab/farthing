"""The policy decision must be correct INSIDE the Temporal workflow sandbox.

Regression lock for a live-only bug the offline suite cannot see: importing
``farthing.workflows`` in the sandbox first executes the package ``__init__``,
which imports ``.domain`` and ``.policy`` — planting sandboxed COPIES of those
modules before the ``imports_passed_through()`` block in ``workflows.py`` runs.
``request_from_dict`` (host module) then builds enums the sandboxed ``decide``
does not recognize by identity, and every eligible refund fails closed as
``unknown_action_type`` / escalate. The worker's runner must pass the pure core
through at the RUNNER level so module identity is unified regardless of import
order.

Needs a reachable local Temporal dev server (``docker compose up -d`` plus the
``farthing_state`` search attribute registered); skips otherwise, so the
zero-dependency gate is unaffected.
"""

from __future__ import annotations

import asyncio
import importlib.util
import unittest
import uuid
from typing import Any

# workflows.py imports temporalio (an edge dependency). Offline, skip — find_spec
# checks availability without importing the stack.
_HAVE_TEMPORAL = importlib.util.find_spec("temporalio") is not None

# A textbook-eligible refund: day 0 of a 14-day window, 5% usage, no prior
# refunds. Timestamps are fixed so the case is deterministic.
_REQ: dict[str, Any] = {
    "action_type": "refund",
    "customer_ref": "<UNKNOWN>",
    "invoice_ref": None,
    "raw_text": "Hi, I signed up two days ago and haven't used it — can I get a refund?",
    "reason_code": "changed_mind",
    "subscription_ref": "<UNKNOWN>",
}
_FACTS: dict[str, Any] = {
    "amount_paid_minor": 5000,
    "charge_id": "ch_sandbox_test",
    "currency": "USD",
    "invoice_id": "in_sandbox_test",
    "now_ts": 1_784_064_293,
    "plan_allow_proration": True,
    "plan_max_refund_pct": "1.0",
    "plan_refund_window_days": 14,
    "prior_refunds_minor": 0,
    "purchase_ts": 1_784_064_105,
    "subscription_id": "sub_sandbox_test",
    "subscription_status": "active",
    "usage_fraction": "0.05",
}


@unittest.skipUnless(_HAVE_TEMPORAL, "temporalio not installed")
class WorkflowSandboxDecisionTest(unittest.IsolatedAsyncioTestCase):
    async def test_eligible_ticket_parks_for_approval_inside_the_sandbox(self) -> None:
        from temporalio import activity
        from temporalio.client import Client, WorkflowExecutionStatus
        from temporalio.worker import Worker

        from farthing.worker import workflow_sandbox_runner
        from farthing.workflows import BillingResolutionWorkflow, TicketInput

        try:
            client = await asyncio.wait_for(Client.connect("127.0.0.1:7233"), timeout=3)
        except Exception:
            self.skipTest("no local Temporal dev server on 127.0.0.1:7233")

        @activity.defn(name="extract")
        async def extract(raw_text: str) -> dict[str, Any]:
            return _REQ

        @activity.defn(name="fetch_facts")
        async def fetch_facts(subscription_id: str, invoice_id: str | None, now_ts: int) -> dict[str, Any]:
            return _FACTS

        @activity.defn(name="draft_reply")
        async def draft_reply(disposition: str, amount_minor: int, currency: str, raw_text: str) -> str:
            return f"stub-reply:{disposition}"

        run_id = uuid.uuid4().hex[:8]
        async with Worker(
            client,
            task_queue=f"sandbox-test-{run_id}",
            workflow_runner=workflow_sandbox_runner(),
            workflows=[BillingResolutionWorkflow],
            activities=[extract, fetch_facts, draft_reply],
        ):
            handle = await client.start_workflow(
                BillingResolutionWorkflow.run,
                TicketInput(
                    ticket_id=f"sandbox-test-{run_id}",
                    raw_text=str(_REQ["raw_text"]),
                    subscription_id=str(_FACTS["subscription_id"]),
                    invoice_id=str(_FACTS["invoice_id"]),
                    charge_id=str(_FACTS["charge_id"]),
                ),
                id=f"farthing-sandbox-test-{run_id}",
                task_queue=f"sandbox-test-{run_id}",
            )

            # An eligible decision parks awaiting approval (prepared_action set);
            # the regression's failure mode completes immediately with escalate.
            prepared: dict[str, Any] | None = None
            for _ in range(100):
                desc = await handle.describe()
                if desc.status is not None and desc.status is not WorkflowExecutionStatus.RUNNING:
                    result = await handle.result()
                    self.fail(f"workflow completed instead of parking for approval: {result}")
                prepared = await handle.query(BillingResolutionWorkflow.prepared_action)
                if prepared is not None:
                    break
                await asyncio.sleep(0.2)
            assert prepared is not None, "workflow neither parked nor completed in time"

            # The previewed action is the policy's answer: full $50.00 refund.
            self.assertEqual(prepared["amount_minor"], 5000)
            self.assertEqual(prepared["currency"], "USD")

            # Clean up: deny, so no workflow is left parked on the dev server.
            await handle.signal(BillingResolutionWorkflow.deny)
            result = await handle.result()
            self.assertEqual(result.disposition, "denied_by_reviewer")

            # Leaving the gate must ADVANCE the visibility state: a completed
            # workflow still reading "awaiting_approval" would be scanned and
            # queried by the approval inbox forever (and a query to a completed
            # workflow with no live worker hangs the inbox page).
            desc = await handle.describe()
            sa = desc.typed_search_attributes
            values = {str(pair.value) for pair in sa} if sa is not None else set()
            self.assertIn("denied", values)
            self.assertNotIn("awaiting_approval", values)


if __name__ == "__main__":
    unittest.main()
