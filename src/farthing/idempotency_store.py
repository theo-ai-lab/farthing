"""Durable idempotency reservation store (Postgres).

Implements the :class:`~farthing.execution_guard.IdempotencyStore` protocol with a
transactional ``INSERT ... ON CONFLICT DO NOTHING``: the first caller to reserve a
key gets ``True`` and a committed row; every subsequent caller gets ``False``.
This is what makes ``authorize_execution`` exactly-once across processes and
restarts — an in-memory set cannot, because it is neither shared nor durable.

Kept out of the pure core on purpose: the core defines the protocol; this edge
module binds it to a database.
"""

from __future__ import annotations

from .db import DBConn

DDL = """
CREATE TABLE IF NOT EXISTS farthing_idempotency (
  key         TEXT PRIMARY KEY,
  reserved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class PostgresIdempotencyStore:
    """Reserve keys durably. Pass a live ``psycopg`` connection (or a pool the
    caller manages). The reservation should be committed in the SAME transaction
    that persists the execution/ledger record, so a crash between reserve and
    record cannot leave a reserved-but-unexecuted key without a trace."""

    def __init__(self, conn: DBConn) -> None:
        self._conn = conn

    def ensure_schema(self) -> None:
        self._conn.execute(DDL)
        self._conn.commit()

    def reserve(self, key: str) -> bool:
        # psycopg3 Connection.execute returns the executed statement handle;
        # rowcount == 1 means this call inserted the row (first reservation).
        result = self._conn.execute(
            "INSERT INTO farthing_idempotency (key) VALUES (%s) "
            "ON CONFLICT (key) DO NOTHING",
            (key,),
        )
        # Caller commits (ideally in the same txn as the execution record).
        return bool(result.rowcount == 1)
