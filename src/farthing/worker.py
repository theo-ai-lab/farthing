"""Temporal worker entrypoints.

Two workers, deliberately separate processes with separate credentials:

* ``main()`` / ``--role agent`` — runs the workflow and the PREPARE-scope
  activities on the ``farthing-agent`` task queue.
* ``--role execute`` — runs ONLY ``execute_refund`` on the ``farthing-execute``
  task queue, in the approval service's trust zone with the EXECUTE key.

Running them apart is what makes the least-privilege story real: the agent worker
has no execute credential and the execute worker has no LLM/prepare surface.
"""

from __future__ import annotations

import argparse
import asyncio
import os

from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.worker import Worker

from . import activities
from .config import approval_settings, worker_settings
from .observability import configure_tracing
from .workflows import BillingResolutionWorkflow, ReconciliationWorkflow


async def _run_agent() -> None:
    s = worker_settings()  # asserts the execute key is NOT visible here
    # Make the configured Anthropic key reach the SDK (pydantic-ai's provider reads
    # ANTHROPIC_API_KEY). Without this, FARTHING_ANTHROPIC_API_KEY is a dead field.
    os.environ.setdefault("ANTHROPIC_API_KEY", s.anthropic_api_key.get_secret_value())
    configure_tracing("farthing-agent")
    client = await Client.connect(
        s.temporal_address, namespace=s.temporal_namespace,
        interceptors=[TracingInterceptor()],  # emit spans for workflows + activities
    )
    await setup_reconciliation_schedule(client, s.task_queue)
    async with Worker(
        client,
        task_queue=s.task_queue,
        workflows=[BillingResolutionWorkflow, ReconciliationWorkflow],
        activities=[
            activities.extract,
            activities.fetch_facts,
            activities.draft_reply,
            activities.compensate,
            activities.reconcile_window,
        ],
    ):
        await asyncio.Event().wait()


async def setup_reconciliation_schedule(client: Client, task_queue: str) -> None:
    """Create (idempotently) the nightly reconciliation Schedule that fires
    ReconciliationWorkflow. Skipped if already present."""
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleAlreadyRunningError,
        ScheduleSpec,
    )

    try:
        await client.create_schedule(
            "farthing-reconciliation-nightly",
            Schedule(
                action=ScheduleActionStartWorkflow(
                    ReconciliationWorkflow.run, id="farthing-reconciliation",
                    task_queue=task_queue,
                ),
                spec=ScheduleSpec(cron_expressions=["0 3 * * *"]),  # 03:00 daily
            ),
        )
    except ScheduleAlreadyRunningError:
        pass


async def _run_execute() -> None:
    s = approval_settings()
    configure_tracing("farthing-execute")
    client = await Client.connect(
        s.temporal_address, namespace=s.temporal_namespace,
        interceptors=[TracingInterceptor()],  # the executed refund is inside a live span
    )
    async with Worker(
        client,
        task_queue=s.execute_task_queue,
        activities=[activities.execute_refund],
    ):
        await asyncio.Event().wait()


def main() -> None:
    p = argparse.ArgumentParser(description="Farthing Temporal worker")
    p.add_argument("--role", choices=["agent", "execute"], default="agent")
    args = p.parse_args()
    asyncio.run(_run_agent() if args.role == "agent" else _run_execute())


if __name__ == "__main__":
    main()
