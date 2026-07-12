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

from temporalio.client import Client
from temporalio.worker import Worker

from . import activities
from .config import approval_settings, worker_settings
from .observability import configure_tracing
from .workflows import BillingResolutionWorkflow


async def _run_agent() -> None:
    s = worker_settings()  # asserts the execute key is NOT visible here
    configure_tracing("farthing-agent")
    client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    async with Worker(
        client,
        task_queue=s.task_queue,
        workflows=[BillingResolutionWorkflow],
        activities=[
            activities.extract,
            activities.fetch_facts,
            activities.draft_reply,
            activities.compensate,
        ],
    ):
        await asyncio.Event().wait()


async def _run_execute() -> None:
    s = approval_settings()
    configure_tracing("farthing-execute")
    client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
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
