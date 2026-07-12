"""The execution guard: nothing moves money without a valid, fresh, unused,
amount-bound human approval.

This is the second half of the load-bearing invariant, enforced structurally:

* An :class:`ApprovalToken` is issued by the approval service (which alone holds
  the ``refund-EXECUTE`` credential) and is HMAC-signed over the exact previewed
  amount, currency, action type, and idempotency key. Any tampering invalidates
  the signature.
* ``authorize_execution`` refuses unless: the disposition is ELIGIBLE; the amount,
  currency, and action type agree across decision, prepared action, and token; the
  signature verifies; the token is within its freshness window; and the
  idempotency key is atomically **reserved** in a durable store — a check-and-set,
  not a read-only membership test, so two concurrent authorizations for the same
  token cannot both succeed (no TOCTOU double-pay).

The agent worker never constructs an ApprovalToken — it holds no signing secret
and only the ``refund-PREPARE`` credential. That split makes "the agent can
prepare but cannot execute" true in code.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Protocol

from .domain import ActionType, Disposition, PolicyDecision, PreparedAction

DEFAULT_MAX_TOKEN_AGE_SECONDS = 900  # 15 minutes


class ExecutionRefused(Exception):
    """Raised whenever execution is not authorized, carrying a machine-readable
    ``code`` so callers and tests can assert the precise reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class IdempotencyStore(Protocol):
    """A durable reservation store. ``reserve`` is an ATOMIC check-and-set: it
    returns True exactly once per key and False on every subsequent call, even
    under concurrency. Production backs this with, e.g., a Postgres
    ``INSERT ... ON CONFLICT DO NOTHING`` committed in the same transaction that
    persists the execution record."""

    def reserve(self, key: str) -> bool: ...


class InMemoryIdempotencyStore:
    """Reference/test implementation. Process-local and not durable — real
    deployments MUST use a transactional store so a worker restart cannot re-open
    the replay window."""

    def __init__(self) -> None:
        self._used: set[str] = set()

    def reserve(self, key: str) -> bool:
        if key in self._used:
            return False
        self._used.add(key)
        return True


@dataclass(frozen=True, slots=True)
class ApprovalToken:
    ticket_id: str
    action_type: str
    amount_minor: int
    currency: str
    idempotency_key: str
    approver: str
    issued_ts: int
    signature: str


def _sign(secret: bytes, *, ticket_id: str, action_type: str, amount_minor: int,
          currency: str, idempotency_key: str, approver: str, issued_ts: int) -> str:
    material = "\x1f".join([
        ticket_id, action_type, str(amount_minor), currency,
        idempotency_key, approver, str(issued_ts),
    ])
    return hmac.new(secret, material.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_approval_token(
    secret: bytes,
    *,
    prepared: PreparedAction,
    idempotency_key: str,
    approver: str,
    issued_ts: int,
) -> ApprovalToken:
    """Called ONLY by the approval service. Binds the token to the exact previewed
    amount, currency, and action type so the approved and executed actions cannot
    diverge."""
    action = prepared.action_type.value
    sig = _sign(
        secret, ticket_id=prepared.ticket_id, action_type=action,
        amount_minor=prepared.amount_minor, currency=prepared.currency,
        idempotency_key=idempotency_key, approver=approver, issued_ts=issued_ts,
    )
    return ApprovalToken(
        ticket_id=prepared.ticket_id, action_type=action,
        amount_minor=prepared.amount_minor, currency=prepared.currency,
        idempotency_key=idempotency_key, approver=approver,
        issued_ts=issued_ts, signature=sig,
    )


@dataclass(frozen=True, slots=True)
class ExecuteInstruction:
    """The only object the Stripe execute-activity acts on. It cannot be
    constructed except by passing the guard below."""

    ticket_id: str
    action_type: str
    amount_minor: int
    currency: str
    idempotency_key: str
    approver: str


def authorize_execution(
    secret: bytes,
    *,
    token: ApprovalToken,
    decision: PolicyDecision,
    prepared: PreparedAction,
    store: IdempotencyStore,
    now_ts: int,
    max_age_seconds: int = DEFAULT_MAX_TOKEN_AGE_SECONDS,
) -> ExecuteInstruction:
    """Return an :class:`ExecuteInstruction` iff execution is authorized; otherwise
    raise :class:`ExecutionRefused`. The single choke point every refund passes
    through. The idempotency-key reservation is the LAST check, so a refused
    authorization never consumes a key."""
    if decision.disposition is not Disposition.ELIGIBLE:
        raise ExecutionRefused("not_eligible", "policy decision is not ELIGIBLE")

    expected_sig = _sign(
        secret, ticket_id=token.ticket_id, action_type=token.action_type,
        amount_minor=token.amount_minor, currency=token.currency,
        idempotency_key=token.idempotency_key, approver=token.approver,
        issued_ts=token.issued_ts,
    )
    if not hmac.compare_digest(expected_sig, token.signature):
        raise ExecutionRefused("bad_signature", "approval token signature invalid")

    if token.amount_minor != decision.amount_minor:
        raise ExecutionRefused("amount_drift", "approved amount != policy amount")
    if token.amount_minor != prepared.amount_minor:
        raise ExecutionRefused("preview_drift", "approved amount != previewed amount")
    if not (token.currency == prepared.currency == decision.currency):
        raise ExecutionRefused("currency_mismatch", "currency disagreement")
    if token.ticket_id != prepared.ticket_id:
        raise ExecutionRefused("token_mismatch", "token ticket != prepared ticket")
    if token.action_type != prepared.action_type.value:
        raise ExecutionRefused("action_mismatch", "token action != prepared action")

    if token.issued_ts > now_ts:
        raise ExecutionRefused("future_token", "token issued in the future")
    if now_ts - token.issued_ts > max_age_seconds:
        raise ExecutionRefused("expired", "approval token is stale")

    # Atomic check-and-set: reserve the key durably before authorizing. This is
    # the exactly-once boundary; a False return means it already executed.
    if not store.reserve(token.idempotency_key):
        raise ExecutionRefused("replay", "idempotency key already reserved/executed")

    return ExecuteInstruction(
        ticket_id=token.ticket_id, action_type=token.action_type,
        amount_minor=token.amount_minor, currency=token.currency,
        idempotency_key=token.idempotency_key, approver=token.approver,
    )
