"""Durable, hash-chained audit ledger backed by Postgres.

Reuses the pure :class:`~farthing.ledger.LedgerEntry` hashing so the on-disk chain
verifies with the same :func:`~farthing.ledger.verify_chain`. The append is meant
to run in the SAME transaction as the idempotency reservation and the execution,
so a refund, its reservation, and its ledger entry commit atomically — there is
no window in which money moved but the audit row is missing.
"""

from __future__ import annotations

from dataclasses import replace

from .db import DBConn
from .ledger import GENESIS_HASH, LedgerEntry

DDL = """
CREATE TABLE IF NOT EXISTS farthing_ledger (
  seq             BIGINT PRIMARY KEY,
  timestamp       BIGINT NOT NULL,
  ticket_id       TEXT NOT NULL,
  action_type     TEXT NOT NULL,
  disposition     TEXT NOT NULL,
  amount_minor    BIGINT NOT NULL,
  currency        TEXT NOT NULL,
  approver        TEXT,
  idempotency_key TEXT,
  otel_trace_id   TEXT,
  policy_version  TEXT NOT NULL,
  charge_id       TEXT,
  api_version     TEXT,
  prev_hash       TEXT NOT NULL,
  entry_hash      TEXT NOT NULL
);
"""

# The replay path looks a reservation up by its key to prove the reservation
# belongs to the refund in hand, so that lookup must not degrade into a scan.
DDL_IDEMPOTENCY_KEY_INDEX = (
    "CREATE INDEX IF NOT EXISTS farthing_ledger_idempotency_key_idx "
    "ON farthing_ledger (idempotency_key);"
)


def ensure_schema(conn: DBConn) -> None:
    conn.execute(DDL)
    conn.execute(DDL_IDEMPOTENCY_KEY_INDEX)


def append_executed_refund(
    conn: DBConn,
    *,
    timestamp: int,
    ticket_id: str,
    amount_minor: int,
    currency: str,
    charge_id: str | None,
    approver: str | None,
    idempotency_key: str,
    otel_trace_id: str | None,
    policy_version: str,
    api_version: str | None = None,
) -> str:
    """Append a 'refunded' entry chained to the current head, within the caller's
    transaction. Serializes appends by locking the tail row (SELECT ... FOR
    UPDATE), so two concurrent appends cannot fork the chain. Returns the new
    entry hash. Caller commits (together with the reservation)."""
    head = conn.execute(
        "SELECT seq, entry_hash FROM farthing_ledger ORDER BY seq DESC LIMIT 1 FOR UPDATE"
    ).fetchone()
    if head is None:
        seq, prev_hash = 0, GENESIS_HASH
    else:
        seq, prev_hash = int(head[0]) + 1, str(head[1])

    draft = LedgerEntry(
        seq=seq, timestamp=timestamp, ticket_id=ticket_id, action_type="refund",
        disposition="refunded", amount_minor=amount_minor, currency=currency,
        approver=approver, idempotency_key=idempotency_key, otel_trace_id=otel_trace_id,
        policy_version=policy_version, charge_id=charge_id, api_version=api_version,
        prev_hash=prev_hash,
    )
    entry = replace(draft, entry_hash=draft.compute_hash())

    conn.execute(
        "INSERT INTO farthing_ledger (seq, timestamp, ticket_id, action_type, "
        "disposition, amount_minor, currency, approver, idempotency_key, "
        "otel_trace_id, policy_version, charge_id, api_version, prev_hash, entry_hash) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (entry.seq, entry.timestamp, entry.ticket_id, entry.action_type,
         entry.disposition, entry.amount_minor, entry.currency, entry.approver,
         entry.idempotency_key, entry.otel_trace_id, entry.policy_version,
         entry.charge_id, entry.api_version, entry.prev_hash, entry.entry_hash),
    )
    return entry.entry_hash


_COLS = ("seq,timestamp,ticket_id,action_type,disposition,amount_minor,currency,"
         "approver,idempotency_key,otel_trace_id,policy_version,charge_id,api_version,prev_hash,entry_hash")


def find_by_idempotency_key(conn: DBConn, key: str) -> LedgerEntry | None:
    """The committed entry a reservation belongs to, or None.

    Because the reservation and this row are written in the same transaction, a
    reserved key always has exactly one row. That makes this the authoritative
    answer to "what was this key actually spent on" — which is what the replay
    path needs before it can treat a taken reservation as *its own* prior
    attempt."""
    row = conn.execute(
        f"SELECT {_COLS} FROM farthing_ledger WHERE idempotency_key = %s ORDER BY seq LIMIT 1",
        (key,),
    ).fetchone()
    return LedgerEntry(*row) if row is not None else None


def read_refunded_entries(conn: DBConn, *, start_ts: int, end_ts: int) -> list[LedgerEntry]:
    """Read committed 'refunded' ledger entries in a time window, for the nightly
    reconciliation to compare against Stripe."""
    rows = conn.execute(
        f"SELECT {_COLS} FROM farthing_ledger "
        "WHERE disposition = 'refunded' AND timestamp BETWEEN %s AND %s ORDER BY seq",
        (start_ts, end_ts),
    ).fetchall()
    return [LedgerEntry(*row) for row in rows]
