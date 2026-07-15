"""Append-only, hash-chained audit ledger.

Every money-moving decision and execution is recorded as an entry whose hash
covers the previous entry's hash. Tampering with any historical entry breaks the
chain from that point forward, so the ledger can be independently verified with
``verify_chain`` — the property an auditor actually cares about.

The ledger here is the pure, in-memory chain logic. Durable persistence
(Postgres) wraps it at the edge; the hashing and verification never depend on the
storage engine.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .idempotency import canonical_json

GENESIS_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    seq: int
    timestamp: int
    ticket_id: str
    action_type: str
    disposition: str
    amount_minor: int
    currency: str
    approver: str | None
    idempotency_key: str | None
    otel_trace_id: str | None
    policy_version: str
    charge_id: str | None
    api_version: str | None
    prev_hash: str
    entry_hash: str = field(default="")

    def _hashable(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("entry_hash", None)
        return d

    def compute_hash(self) -> str:
        return hashlib.sha256(canonical_json(self._hashable()).encode("utf-8")).hexdigest()


class AuditLedger:
    """An append-only chain. Not thread-safe by itself; the persistence layer
    serializes appends (e.g. a Postgres transaction with a per-ticket lock)."""

    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []

    @property
    def head_hash(self) -> str:
        return self._entries[-1].entry_hash if self._entries else GENESIS_HASH

    def append(
        self,
        *,
        timestamp: int,
        ticket_id: str,
        action_type: str,
        disposition: str,
        amount_minor: int,
        currency: str,
        policy_version: str,
        approver: str | None = None,
        idempotency_key: str | None = None,
        otel_trace_id: str | None = None,
        charge_id: str | None = None,
        api_version: str | None = None,
    ) -> LedgerEntry:
        seq = len(self._entries)
        draft = LedgerEntry(
            seq=seq,
            timestamp=timestamp,
            ticket_id=ticket_id,
            action_type=action_type,
            disposition=disposition,
            amount_minor=amount_minor,
            currency=currency,
            approver=approver,
            idempotency_key=idempotency_key,
            otel_trace_id=otel_trace_id,
            policy_version=policy_version,
            charge_id=charge_id,
            api_version=api_version,
            prev_hash=self.head_hash,
        )
        entry = replace(draft, entry_hash=draft.compute_hash())
        self._entries.append(entry)
        return entry

    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)


def verify_chain(
    entries: tuple[LedgerEntry, ...] | list[LedgerEntry],
    *,
    expected_head_hash: str | None = None,
    expected_count: int | None = None,
) -> bool:
    """True iff the chain is intact.

    Internal consistency (sequential seqs, correct back-links, recomputed hashes)
    is necessary but NOT sufficient: a hash chain alone cannot detect trailing
    truncation (dropping the last N entries leaves a valid shorter chain) or a
    wholesale rebuild by anyone with write access. To catch those, pass the
    authoritative ``expected_head_hash`` and ``expected_count`` recorded
    out-of-band (e.g. a periodically signed/published head). A production
    deployment should additionally sign each entry, or the head, with a key the
    ledger writer does not hold."""
    prev = GENESIS_HASH
    for i, e in enumerate(entries):
        if e.seq != i:
            return False
        if e.prev_hash != prev:
            return False
        if e.entry_hash != e.compute_hash():
            return False
        prev = e.entry_hash
    if expected_count is not None and len(entries) != expected_count:
        return False
    if expected_head_hash is not None and prev != expected_head_hash:
        return False
    return True
