"""Durable-ledger append test against a faithful in-memory fake of the Postgres
semantics the code relies on (tail-lock read, append, windowed read). Proves the
DB-backed chain that append_executed_refund produces verifies with the same
verify_chain used on the pure ledger — the whole point of the module."""

from __future__ import annotations

import unittest
from dataclasses import replace

from farthing import pg_ledger
from farthing.ledger import verify_chain


class _Result:
    def __init__(self, rows: list, rowcount: int = 0) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Simulates just enough of psycopg for pg_ledger: a single append-only table."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []  # tuples in _COLS order (seq..entry_hash)

    def execute(self, sql: str, params=None):
        s = sql.strip()
        if s.startswith("CREATE TABLE"):
            return _Result([])
        if s.startswith("SELECT seq, entry_hash"):  # tail lock (ORDER BY seq DESC LIMIT 1)
            return _Result([(self.rows[-1][0], self.rows[-1][13])] if self.rows else [])
        if s.startswith("INSERT INTO farthing_ledger"):
            self.rows.append(tuple(params))
            return _Result([], rowcount=1)
        if s.startswith("SELECT") and "disposition = 'refunded'" in s:
            start, end = params
            sel = [r for r in self.rows if r[4] == "refunded" and start <= r[1] <= end]
            return _Result(sorted(sel, key=lambda r: r[0]))
        return _Result([])

    def commit(self) -> None:
        pass


def _append(conn, ts, ticket, amount, key):
    return pg_ledger.append_executed_refund(
        conn, timestamp=ts, ticket_id=ticket, amount_minor=amount, currency="USD",
        charge_id="ch_1", approver="alice", idempotency_key=key,
        otel_trace_id="trace", policy_version="2026.07.1",
    )


class TestDurableLedger(unittest.TestCase):
    def test_append_produces_a_valid_chain(self) -> None:
        conn = FakeConn()
        pg_ledger.ensure_schema(conn)
        _append(conn, 1000, "t1", 5000, "frthg_a")
        _append(conn, 1001, "t2", 2500, "frthg_b")
        entries = pg_ledger.read_refunded_entries(conn, start_ts=0, end_ts=2000)
        self.assertEqual(len(entries), 2)
        self.assertTrue(verify_chain(entries))
        self.assertEqual(entries[0].charge_id, "ch_1")

    def test_windowed_read_excludes_out_of_window(self) -> None:
        conn = FakeConn()
        _append(conn, 1000, "t1", 5000, "frthg_a")
        _append(conn, 9999, "t2", 2500, "frthg_b")
        entries = pg_ledger.read_refunded_entries(conn, start_ts=0, end_ts=2000)
        self.assertEqual([e.ticket_id for e in entries], ["t1"])

    def test_tampered_read_breaks_verification(self) -> None:
        conn = FakeConn()
        _append(conn, 1000, "t1", 5000, "frthg_a")
        _append(conn, 1001, "t2", 2500, "frthg_b")
        entries = pg_ledger.read_refunded_entries(conn, start_ts=0, end_ts=2000)
        entries[0] = replace(entries[0], amount_minor=999999)
        self.assertFalse(verify_chain(entries))


if __name__ == "__main__":
    unittest.main()
