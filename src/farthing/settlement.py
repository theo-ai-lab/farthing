"""The money-movement sequence, with its adapters injected.

This is the ordering the exactly-once claim rests on, extracted from the Temporal
activity that used to hold it inline so it can actually be executed — and killed
at every boundary — offline:

1. ``authorize_execution`` runs every check and, LAST, atomically reserves the
   idempotency key. A refused authorization consumes nothing.
2. The reservation and the hash-chained audit row are written in the SAME
   transaction and committed BEFORE the provider is called. They are never rolled
   back on a provider failure.
3. Only then does money move.

The deliberate cost of that ordering is one-directional: a permanent provider
failure leaves an audit row with no refund, which the nightly reconciliation
raises as an incident. The direction it forecloses is the one that matters —
money can never move without a durable audit row, and a crash can never leave an
approved-but-invisible payment.

``settle_refund`` takes its edges as protocols (:class:`~farthing.db.DBConn`,
:class:`~farthing.execution_guard.IdempotencyStore`, :class:`RefundGateway`), so
the module stays pure standard library: no Temporal, no psycopg, no Stripe. The
activity in :mod:`farthing.activities` is the wiring that binds the real ones.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from . import pg_ledger
from .db import DBConn
from .domain import PolicyDecision, PreparedAction, RefundResult
from .execution_guard import (
    DEFAULT_MAX_TOKEN_AGE_SECONDS,
    ApprovalToken,
    ExecutionRefused,
    IdempotencyStore,
    authorize_execution,
)

#: Every durability boundary in the sequence, in execution order. A step observer
#: is called with each name as it is crossed. The fault-injection matrix derives
#: its kill points from this tuple and cross-checks it against the source, so a
#: boundary added below without being declared here fails the suite.
KILL_POINTS: tuple[str, ...] = (
    "before_authorize",
    "after_reserve",
    "after_ledger_append",
    "after_commit",
    "after_provider_create",
    "replay_before_lookup",
    "replay_after_lookup",
    "replay_after_create",
)

#: An observer invoked as each boundary in :data:`KILL_POINTS` is crossed.
#: Instrumentation only — the default does nothing. An observer that RAISES
#: aborts the sequence at exactly that boundary, which is how the crash matrix
#: simulates process death without a container. A production observer must not
#: raise.
StepObserver = Callable[[str], None]


def _ignore(step: str) -> None:
    del step


class RefundGateway(Protocol):
    """The payment-provider surface a settlement needs, and nothing more.

    ``create_refund`` must be idempotent on ``idempotency_key`` for as long as the
    provider's window lasts, and must persist that key on the refund so
    ``find_refund_by_key`` can resolve it afterwards — that lookup is what keeps
    recovery exactly-once once the provider's own window has expired.
    :class:`~farthing.stripe_client.ExecuteClient` satisfies this structurally.
    """

    def create_refund(
        self, *, charge_id: str, amount_minor: int, idempotency_key: str
    ) -> RefundResult: ...

    def find_refund_by_key(
        self, *, charge_id: str, idempotency_key: str
    ) -> RefundResult | None: ...


def settle_refund(
    secret: bytes,
    *,
    token: ApprovalToken,
    decision: PolicyDecision,
    prepared: PreparedAction,
    conn: DBConn,
    store: IdempotencyStore,
    gateway: RefundGateway,
    now_ts: int,
    trace_id: str | None = None,
    api_version: str | None = None,
    max_age_seconds: int = DEFAULT_MAX_TOKEN_AGE_SECONDS,
    on_step: StepObserver = _ignore,
) -> RefundResult:
    """Move the approved money exactly once, or raise :class:`ExecutionRefused`.

    ``ExecutionRefused`` is the single error shape: every refusal carries a
    machine-readable ``code``, and callers map it to whatever their runtime calls
    a terminal failure. Nothing else is raised on purpose.
    """
    on_step("before_authorize")
    try:
        instr = authorize_execution(
            secret, token=token, decision=decision, prepared=prepared,
            store=store, now_ts=now_ts, max_age_seconds=max_age_seconds,
        )
    except ExecutionRefused as refusal:
        if refusal.code != "replay":
            raise
        # The key was already reserved by an earlier attempt, which means that
        # attempt committed its audit row. Resolve idempotently rather than
        # creating a second refund.
        return _resolve_replay(token=token, gateway=gateway, on_step=on_step)
    on_step("after_reserve")

    # authorize_execution refuses a refund with no bound charge (unbound_charge),
    # so this is unreachable; assert it to keep the guarantee typed.
    if instr.charge_id is None:  # pragma: no cover - guarded upstream
        raise ExecutionRefused("unbound_charge", "authorized refund has no bound charge")

    pg_ledger.append_executed_refund(
        conn, timestamp=now_ts, ticket_id=instr.ticket_id,
        amount_minor=instr.amount_minor, currency=instr.currency,
        charge_id=instr.charge_id, approver=instr.approver,
        idempotency_key=instr.idempotency_key, otel_trace_id=trace_id,
        policy_version=decision.policy_version, api_version=api_version,
    )
    on_step("after_ledger_append")
    conn.commit()  # reservation + audit row become durable together, before money
    on_step("after_commit")

    result = gateway.create_refund(
        charge_id=instr.charge_id,  # the signed, bound charge — no unsigned fallback
        amount_minor=instr.amount_minor,
        idempotency_key=instr.idempotency_key,
    )
    on_step("after_provider_create")
    return result


def _resolve_replay(
    *, token: ApprovalToken, gateway: RefundGateway, on_step: StepObserver
) -> RefundResult:
    """Resolve an already-reserved key without moving more money.

    Return the refund the reservation belongs to if the provider already has it;
    otherwise re-drive the provider with the SAME key, which is the case where the
    reserving attempt died between its commit and the provider call. The charge is
    always ``token.charge_id`` — the signed target, never an unsigned argument.
    """
    if token.charge_id is None:  # pragma: no cover - guarded upstream
        raise ExecutionRefused("unbound_charge", "replayed refund token has no bound charge")
    on_step("replay_before_lookup")
    existing = gateway.find_refund_by_key(
        charge_id=token.charge_id, idempotency_key=token.idempotency_key
    )
    on_step("replay_after_lookup")
    if existing is not None:
        return existing
    created = gateway.create_refund(
        charge_id=token.charge_id, amount_minor=token.amount_minor,
        idempotency_key=token.idempotency_key,
    )
    on_step("replay_after_create")
    return created
