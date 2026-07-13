from __future__ import annotations

import unittest

from farthing.ledger import AuditLedger
from farthing.reconcile import reconcile


def _ledger_with_refund(key: str, amount: int, ts: int = 1000, currency: str = "USD") -> AuditLedger:
    led = AuditLedger()
    led.append(
        timestamp=ts, ticket_id="t1", action_type="refund", disposition="refunded",
        amount_minor=amount, currency=currency, policy_version="2026.07.1",
        approver="alice", idempotency_key=key,
    )
    return led


def _stripe(rid: str, amount: int, key: str | None, currency: str = "usd") -> dict:
    d = {"id": rid, "amount": amount, "currency": currency}
    if key is not None:
        d["idempotency_key"] = key
    return d


class TestReconcile(unittest.TestCase):
    def test_clean_when_sides_agree(self) -> None:
        led = _ledger_with_refund("frthg_k", 5000)
        report = reconcile(list(led.entries()), [_stripe("re_1", 5000, "frthg_k")],
                           window_start_ts=0, window_end_ts=2000)
        self.assertTrue(report.clean)
        self.assertEqual(report.matched, 1)

    def test_keyless_stripe_refund_is_flagged_not_dropped(self) -> None:
        # The rogue off-ledger refund the audit exists to catch: no Farthing key.
        led = AuditLedger()
        report = reconcile(list(led.entries()), [_stripe("re_rogue", 9999, None)],
                           window_start_ts=0, window_end_ts=2000)
        self.assertFalse(report.clean)
        self.assertIn("re_rogue", report.unmatched_in_stripe)

    def test_stripe_refund_with_no_ledger_is_flagged(self) -> None:
        led = AuditLedger()
        report = reconcile(list(led.entries()), [_stripe("re_x", 9999, "frthg_x")],
                           window_start_ts=0, window_end_ts=2000)
        self.assertFalse(report.clean)
        self.assertIn("re_x", report.unmatched_in_stripe)

    def test_amount_mismatch_is_flagged(self) -> None:
        led = _ledger_with_refund("frthg_k", 5000)
        report = reconcile(list(led.entries()), [_stripe("re_1", 6000, "frthg_k")],
                           window_start_ts=0, window_end_ts=2000)
        self.assertFalse(report.clean)
        self.assertIn("re_1", report.mismatched)

    def test_currency_mismatch_is_flagged(self) -> None:
        # Same key and numeric amount, different currency -> must NOT reconcile clean.
        led = _ledger_with_refund("frthg_k", 5000, currency="USD")
        report = reconcile(list(led.entries()), [_stripe("re_1", 5000, "frthg_k", currency="jpy")],
                           window_start_ts=0, window_end_ts=2000)
        self.assertFalse(report.clean)
        self.assertIn("re_1", report.mismatched)

    def test_same_key_double_payout_is_flagged_not_deduped(self) -> None:
        led = _ledger_with_refund("frthg_k", 5000)
        stripe = [_stripe("re_1", 5000, "frthg_k"), _stripe("re_2", 5000, "frthg_k")]
        report = reconcile(list(led.entries()), stripe, window_start_ts=0, window_end_ts=2000)
        self.assertFalse(report.clean)
        self.assertIn("frthg_k", report.duplicate_keys)
        self.assertEqual(report.stripe_refunds, 2)  # counts rows, not distinct keys

    def test_malformed_record_quarantined_not_fatal(self) -> None:
        led = _ledger_with_refund("frthg_k", 5000)
        stripe = [_stripe("re_1", 5000, "frthg_k"), {"id": "re_bad"}]  # missing amount/currency
        report = reconcile(list(led.entries()), stripe, window_start_ts=0, window_end_ts=2000)
        self.assertIn("re_bad", report.malformed)
        self.assertEqual(report.matched, 1)  # the good one still reconciled
        self.assertFalse(report.clean)

    def test_ledger_entry_with_no_stripe_is_flagged(self) -> None:
        led = _ledger_with_refund("frthg_k", 5000)
        report = reconcile(list(led.entries()), [], window_start_ts=0, window_end_ts=2000)
        self.assertFalse(report.clean)
        self.assertIn("frthg_k", report.unmatched_in_ledger)


if __name__ == "__main__":
    unittest.main()
