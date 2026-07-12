"""Start one billing-resolution ticket and print the result.

Requires the infra (`docker compose up -d`) and the agent + execute workers to be
running (`uv run farthing-worker --role agent` and `--role execute`). Open the
approval inbox to approve the eligible action.

    uv run python examples/run_ticket.py
"""

from __future__ import annotations

import asyncio

from temporalio.client import Client

from farthing.config import worker_settings
from farthing.workflows import BillingResolutionWorkflow, TicketInput


async def main() -> None:
    s = worker_settings()
    client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)

    ticket = TicketInput(
        ticket_id="demo-001",
        raw_text="Hi, I signed up two days ago and haven't used it — can I get a refund?",
        subscription_id="sub_demo",   # a real test-mode subscription id
        invoice_id="in_demo",
        charge_id="ch_demo",
    )

    handle = await client.start_workflow(
        BillingResolutionWorkflow.run,
        ticket,
        id=f"farthing-ticket-{ticket.ticket_id}",
        task_queue=s.task_queue,
    )
    print(f"started {handle.id}; open http://127.0.0.1:8080/inbox to approve")
    result = await handle.result()
    print("result:", result)


if __name__ == "__main__":
    asyncio.run(main())
