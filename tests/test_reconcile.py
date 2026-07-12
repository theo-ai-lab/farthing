from __future__ import annotations

import unittest

from farthing.ledger import AuditLedger
from farthing.reconcile import reconcile


def _ledger_with_refund(key: str, amount: int, ts: int = 1000) -> AuditLedger:
    led = AuditLedger()
    led.append(
        timestamp=ts, ticket_id="t1", action_type="refund", disposition="refunded",
        amount_minor=amount, currency="USD", policy_version="2026.07.1",
        approver="alice", idempotency_key=key,
    )
    return led


class TestReconcile(unittest.TestCase):
    def test_clean_when_sides_agree(self) -> None:
        led = _ledger_with_refund("frthg_k", 5000)
        stripe = [{"id": "re_1", "amount": 5000, "idempotency_key": "frthg_k"}]
        report = reconcile(list(led.entries()), stripe, window_start_ts=0, window_end_ts=2000)
        self.assertTrue(report.clean)
        self.assertEqual(report.matched, 1)

    def test_stripe_refund_with_no_ledger_is_flagged(self) -> None:
        # Money moved off-ledger — the worst case. Must not be clean.
        led = AuditLedger()
        stripe = [{"id": "re_rogue", "amount": 9999, "idempotency_key": "frthg_x"}]
        report = reconcile(list(led.entries()), stripe, window_start_ts=0, window_end_ts=2000)
        self.assertFalse(report.clean)
        self.assertIn("re_rogue", report.unmatched_in_stripe)

    def test_amount_mismatch_is_flagged(self) -> None:
        led = _ledger_with_refund("frthg_k", 5000)
        stripe = [{"id": "re_1", "amount": 6000, "idempotency_key": "frthg_k"}]
        report = reconcile(list(led.entries()), stripe, window_start_ts=0, window_end_ts=2000)
        self.assertFalse(report.clean)

    def test_ledger_entry_with_no_stripe_is_flagged(self) -> None:
        led = _ledger_with_refund("frthg_k", 5000)
        report = reconcile(list(led.entries()), [], window_start_ts=0, window_end_ts=2000)
        self.assertFalse(report.clean)
        self.assertIn("frthg_k", report.unmatched_in_ledger)


if __name__ == "__main__":
    unittest.main()
