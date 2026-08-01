"""The money-movement sequence, exercised offline.

``settle_refund`` is the ordering argument the whole exactly-once claim rests on:
reserve the key and append the audit row in ONE transaction, commit that
transaction BEFORE the provider is called, and never roll it back on a provider
failure. Until this file existed, nothing executed that sequence — it lived only
in a comment inside an activity welded to settings, psycopg and stripe.

These are the behavioral tests for the seam. The fault-injection matrix that
kills the process at every boundary of it lives in ``test_crash_matrix.py``.
"""

from __future__ import annotations

import unittest

from _fault_injection import NOW_TS, SECRET, Case, FakeConn, FakeDatabase, FakeStripe, committed_entries
from farthing.execution_guard import ExecutionRefused
from farthing.idempotency_store import PostgresIdempotencyStore
from farthing.ledger import verify_chain
from farthing.settlement import settle_refund

API_VERSION = "2026-06-24.dahlia"


def _settle(case: Case, db: FakeDatabase, gateway: FakeStripe, **kw: object):
    conn = FakeConn(db)
    return settle_refund(
        SECRET,
        token=case.token, decision=case.decision, prepared=case.prepared,
        conn=conn, store=PostgresIdempotencyStore(conn), gateway=gateway,
        now_ts=NOW_TS, trace_id="trace-1", api_version=API_VERSION,
        **kw,  # pyright: ignore[reportArgumentType]
    )


class TestSettlementSequence(unittest.TestCase):
    def test_happy_path_reserves_records_and_refunds_once(self) -> None:
        case, db, stripe = Case(), FakeDatabase(), FakeStripe()
        result = _settle(case, db, stripe)

        self.assertEqual(result["amount"], case.amount_minor)
        self.assertEqual(len(stripe.applied), 1, "exactly one refund must exist")
        self.assertEqual(stripe.create_calls[0]["idempotency_key"], case.idempotency_key)
        # The reservation and the audit row committed together.
        self.assertEqual(db.reserved_keys, [case.idempotency_key])
        entries = committed_entries(db)
        self.assertEqual(len(entries), 1)
        self.assertTrue(verify_chain(entries))
        self.assertEqual(entries[0].amount_minor, case.amount_minor)
        self.assertEqual(entries[0].charge_id, case.charge_id)
        self.assertEqual(entries[0].approver, "reviewer@example.com")
        self.assertEqual(entries[0].idempotency_key, case.idempotency_key)
        self.assertEqual(entries[0].api_version, API_VERSION)

    def test_second_settlement_of_the_same_token_moves_no_more_money(self) -> None:
        """The replay path: the key is already reserved, so the existing refund is
        resolved instead of a second one being created."""
        case, db, stripe = Case(), FakeDatabase(), FakeStripe()
        first = _settle(case, db, stripe)
        second = _settle(case, db, stripe)

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(stripe.applied), 1)
        self.assertEqual(len(stripe.create_calls), 1, "replay must not re-drive the provider")
        self.assertEqual(len(committed_entries(db)), 1, "replay must not append a second audit row")

    def test_replay_resolves_by_key_even_with_the_provider_window_closed(self) -> None:
        """Exactly-once must be ours, not borrowed from Stripe's 24h idempotency
        window: with that window closed, the metadata lookup still resolves it."""
        case, db, stripe = Case(), FakeDatabase(), FakeStripe(idempotency_window_open=False)
        first = _settle(case, db, stripe)
        second = _settle(case, db, stripe)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(stripe.applied), 1)

    def test_tampered_token_refuses_and_writes_nothing(self) -> None:
        case, db, stripe = Case(), FakeDatabase(), FakeStripe()
        from dataclasses import replace

        case.token = replace(case.token, amount_minor=case.amount_minor * 10)
        with self.assertRaises(ExecutionRefused) as ctx:
            _settle(case, db, stripe)
        self.assertEqual(ctx.exception.code, "bad_signature")
        self.assertEqual(stripe.applied, [])
        self.assertEqual(db.reserved_keys, [], "a refused authorization must not consume a key")
        self.assertEqual(committed_entries(db), [])

    def test_refusal_leaves_the_key_available_for_a_valid_retry(self) -> None:
        """A refusal must not burn the key: the ticket has to stay settleable."""
        case, db, stripe = Case(), FakeDatabase(), FakeStripe()
        from dataclasses import replace

        bad = Case()
        bad.token = replace(bad.token, approver="attacker@example.com")
        with self.assertRaises(ExecutionRefused):
            _settle(bad, db, stripe)
        result = _settle(case, db, stripe)
        self.assertEqual(result["amount"], case.amount_minor)
        self.assertEqual(len(stripe.applied), 1)

    def test_audit_row_is_committed_before_the_provider_is_called(self) -> None:
        """The ordering the whole recovery argument depends on. If the provider is
        reached before the commit, a crash in between moves money with no audit
        row — the one failure this system may never have."""
        case, db, stripe = Case(), FakeDatabase(), FakeStripe()
        seen_at_create: list[int] = []

        class _Watching(FakeStripe):
            def create_refund(self, **kw: object):  # type: ignore[override]
                seen_at_create.append(len(db.ledger_rows))
                return FakeStripe.create_refund(self, **kw)  # pyright: ignore[reportArgumentType]

        watcher = _Watching()
        _settle(case, db, watcher)
        self.assertEqual(seen_at_create, [1], "audit row must be committed before the refund call")
        del stripe


if __name__ == "__main__":
    unittest.main()
