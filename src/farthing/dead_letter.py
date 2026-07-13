"""Dead-letter queue for terminally-failed refund executions.

A refund execution can fail terminally in two ways: the guard refuses it for a
non-recoverable reason (bad signature, amount drift, unbound charge, not
eligible), or the idempotent retries are exhausted (Stripe/Postgres unreachable
for the whole retry budget). Either way the ticket must not vanish — it lands in
the dead-letter queue for a human to resolve, and it is visible to the same
nightly reconciliation that watches the ledger.

The entry type and builder are pure and testable; a thin Postgres persistence
layer wraps them at the edge (same shape as ``pg_ledger``).
"""

from __future__ import annotations

from dataclasses import dataclass

from .db import DBConn


@dataclass(frozen=True, slots=True)
class DeadLetterEntry:
    timestamp: int
    ticket_id: str
    reason_code: str
    amount_minor: int
    currency: str
    detail: str


def build_dead_letter(
    *,
    timestamp: int,
    ticket_id: str,
    reason_code: str,
    amount_minor: int,
    currency: str,
    detail: str,
) -> DeadLetterEntry:
    """Construct a dead-letter entry for a terminally-failed execution."""
    return DeadLetterEntry(
        timestamp=timestamp, ticket_id=ticket_id, reason_code=reason_code,
        amount_minor=amount_minor, currency=currency, detail=detail,
    )


DDL = """
CREATE TABLE IF NOT EXISTS farthing_dead_letter (
  id          BIGSERIAL PRIMARY KEY,
  timestamp   BIGINT NOT NULL,
  ticket_id   TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  amount_minor BIGINT NOT NULL,
  currency    TEXT NOT NULL,
  detail      TEXT NOT NULL,
  resolved    BOOLEAN NOT NULL DEFAULT FALSE
);
"""


def ensure_schema(conn: DBConn) -> None:
    conn.execute(DDL)


def record_dead_letter(conn: DBConn, entry: DeadLetterEntry) -> None:
    """Persist a dead-letter entry. Caller commits."""
    conn.execute(
        "INSERT INTO farthing_dead_letter "
        "(timestamp, ticket_id, reason_code, amount_minor, currency, detail) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (entry.timestamp, entry.ticket_id, entry.reason_code, entry.amount_minor,
         entry.currency, entry.detail),
    )


def read_dead_letters(conn: DBConn, *, unresolved_only: bool = False) -> list[DeadLetterEntry]:
    """Read dead-letter entries for the ops/reconciliation surface."""
    where = "WHERE resolved = FALSE " if unresolved_only else ""
    rows = conn.execute(
        f"SELECT timestamp, ticket_id, reason_code, amount_minor, currency, detail "
        f"FROM farthing_dead_letter {where}ORDER BY timestamp"
    ).fetchall()
    return [DeadLetterEntry(*row) for row in rows]
