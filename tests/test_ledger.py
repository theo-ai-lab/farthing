from __future__ import annotations

import unittest
from dataclasses import replace

from farthing.ledger import GENESIS_HASH, AuditLedger, verify_chain


def _sample() -> AuditLedger:
    led = AuditLedger()
    led.append(
        timestamp=1000, ticket_id="t1", action_type="refund", disposition="eligible",
        amount_minor=5000, currency="USD", policy_version="2026.07.1",
        approver="alice", idempotency_key="frthg_a", otel_trace_id="trace1",
    )
    led.append(
        timestamp=1001, ticket_id="t2", action_type="cancellation", disposition="eligible",
        amount_minor=0, currency="USD", policy_version="2026.07.1", approver="bob",
    )
    return led


class TestLedger(unittest.TestCase):
    def test_chain_links(self) -> None:
        led = _sample()
        e = led.entries()
        self.assertEqual(e[0].prev_hash, GENESIS_HASH)
        self.assertEqual(e[1].prev_hash, e[0].entry_hash)
        self.assertEqual(e[0].seq, 0)
        self.assertEqual(e[1].seq, 1)

    def test_verify_intact(self) -> None:
        self.assertTrue(verify_chain(_sample().entries()))

    def test_tamper_amount_breaks_chain(self) -> None:
        entries = list(_sample().entries())
        entries[0] = replace(entries[0], amount_minor=999999)  # forge a bigger refund
        self.assertFalse(verify_chain(entries))

    def test_tamper_rehash_still_breaks_backlink(self) -> None:
        # Even if an attacker recomputes the tampered entry's own hash, the next
        # entry's prev_hash no longer matches.
        entries = list(_sample().entries())
        forged = replace(entries[0], amount_minor=999999)
        forged = replace(forged, entry_hash=forged.compute_hash())
        entries[0] = forged
        self.assertFalse(verify_chain(entries))

    def test_reorder_breaks_chain(self) -> None:
        entries = list(_sample().entries())
        entries.reverse()
        self.assertFalse(verify_chain(entries))

    def test_head_hash_tracks_last(self) -> None:
        led = _sample()
        self.assertEqual(led.head_hash, led.entries()[-1].entry_hash)

    def test_truncation_undetected_without_anchor_but_caught_with_it(self) -> None:
        led = _sample()
        entries = list(led.entries())
        expected_head = led.head_hash
        expected_count = len(entries)
        # Drop the last entry: the shorter chain still self-verifies...
        truncated = entries[:-1]
        self.assertTrue(verify_chain(truncated))
        # ...but the out-of-band anchor catches it.
        self.assertFalse(
            verify_chain(truncated, expected_head_hash=expected_head, expected_count=expected_count)
        )
        # The intact chain passes with the anchor.
        self.assertTrue(
            verify_chain(entries, expected_head_hash=expected_head, expected_count=expected_count)
        )


if __name__ == "__main__":
    unittest.main()
