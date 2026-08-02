"""Preconditions the live stack needs, checked where they are needed.

The approval inbox lists parked workflows by filtering on a CUSTOM Temporal
search attribute. Temporal requires custom attributes to be registered against
the namespace before a workflow may set one; a workflow that upserts an
unregistered attribute does not fail with anything resembling that sentence — the
workflow task fails with ``WORKFLOW_TASK_FAILED_CAUSE_BAD_SEARCH_ATTRIBUTES`` and
the caller sees only ``Unable to query workflow due to Workflow Task in failed
state``. Worse, an inbox pointed at such a namespace renders an EMPTY LIST, which
is indistinguishable from "no refunds are waiting for you".

So the check lives here, in one pure-stdlib place, and returns the remediation
rather than just the diagnosis. :mod:`farthing.workflows` imports the attribute
name from here so the registered name and the upserted name cannot drift.
"""

from __future__ import annotations

from collections.abc import Iterable

REQUIRED_SEARCH_ATTRIBUTE = "farthing_state"
"""Keyword attribute the workflow upserts and the approval inbox filters on."""

REGISTER_COMMAND = "./scripts/register_search_attribute.sh"
"""One-time-per-namespace registration. Safe to re-run."""


def missing_search_attribute(registered: Iterable[str]) -> str | None:
    """Return an actionable message if ``registered`` lacks the attribute, else None.

    ``registered`` is the namespace's custom search attribute names, as returned by
    Temporal's operator service. Kept as a plain iterable of strings so this stays
    importable without ``temporalio`` and runs inside the zero-install gate.
    """
    if REQUIRED_SEARCH_ATTRIBUTE in set(registered):
        return None
    return (
        f"Temporal namespace has no custom search attribute {REQUIRED_SEARCH_ATTRIBUTE!r}. "
        f"The workflow cannot park for approval without it and the inbox would show an "
        f"empty list rather than an error. Register it once with: {REGISTER_COMMAND}"
    )
