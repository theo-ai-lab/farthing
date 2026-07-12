"""Farthing — the support agent that is allowed to touch money.

The public surface is intentionally the *pure domain core*: everything that
decides or moves a dollar is importable and testable without Temporal, Stripe, an
LLM, or a network. The framework adapters live alongside but are imported lazily
so the core stays dependency-light and verifiable.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .domain import (
    AccountFacts,
    ActionType,
    Disposition,
    ExtractedRequest,
    PolicyDecision,
    PreparedAction,
    SubscriptionStatus,
)
from .execution_guard import (
    ApprovalToken,
    ExecuteInstruction,
    ExecutionRefused,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    authorize_execution,
    issue_approval_token,
)
from .idempotency import derive_idempotency_key
from .ledger import AuditLedger, LedgerEntry, verify_chain
from .money import Money
from .policy import POLICY_VERSION, decide

__all__ = [
    "AccountFacts",
    "ActionType",
    "ApprovalToken",
    "AuditLedger",
    "Disposition",
    "ExecuteInstruction",
    "ExecutionRefused",
    "ExtractedRequest",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "LedgerEntry",
    "Money",
    "POLICY_VERSION",
    "PolicyDecision",
    "PreparedAction",
    "SubscriptionStatus",
    "authorize_execution",
    "decide",
    "derive_idempotency_key",
    "issue_approval_token",
    "verify_chain",
]
