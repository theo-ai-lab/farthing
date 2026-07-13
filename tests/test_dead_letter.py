from __future__ import annotations

import unittest
from typing import Any

from farthing import dead_letter


class _Result:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def execute(self, sql: str, params: Any = None):
        s = sql.strip()
        if s.startswith("CREATE TABLE"):
            return _Result()
        if s.startswith("INSERT INTO farthing_dead_letter"):
            self.rows.append(tuple(params))
            return _Result(rowcount=1)
        if s.startswith("SELECT"):
            return _Result(self.rows)
        return _Result()

    def commit(self):
        pass


class TestDeadLetter(unittest.TestCase):
    def test_build_dead_letter_entry(self) -> None:
        e = dead_letter.build_dead_letter(
            timestamp=1000, ticket_id="t1", reason_code="not_eligible",
            amount_minor=5000, currency="USD", detail="guard refused: not_eligible",
        )
        self.assertEqual(e.ticket_id, "t1")
        self.assertEqual(e.reason_code, "not_eligible")
        self.assertEqual(e.amount_minor, 5000)
        self.assertEqual(e.detail, "guard refused: not_eligible")

    def test_record_and_read_dead_letter(self) -> None:
        conn = FakeConn()
        dead_letter.ensure_schema(conn)
        e = dead_letter.build_dead_letter(
            timestamp=1000, ticket_id="t1", reason_code="retries_exhausted",
            amount_minor=2500, currency="USD", detail="Stripe unreachable after 6 attempts",
        )
        dead_letter.record_dead_letter(conn, e)
        rows = dead_letter.read_dead_letters(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].reason_code, "retries_exhausted")
        self.assertEqual(rows[0].amount_minor, 2500)


if __name__ == "__main__":
    unittest.main()
