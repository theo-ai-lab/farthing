from __future__ import annotations

import unittest
from decimal import Decimal

from farthing.idempotency import canonical_json, derive_idempotency_key


class TestIdempotency(unittest.TestCase):
    def test_deterministic(self) -> None:
        payload = {"amount": 5000, "currency": "USD"}
        a = derive_idempotency_key("wf-1", "execute_refund", payload)
        b = derive_idempotency_key("wf-1", "execute_refund", payload)
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("frthg_"))

    def test_key_order_does_not_matter(self) -> None:
        a = derive_idempotency_key("wf-1", "s", {"a": 1, "b": 2})
        b = derive_idempotency_key("wf-1", "s", {"b": 2, "a": 1})
        self.assertEqual(a, b)

    def test_different_payload_different_key(self) -> None:
        a = derive_idempotency_key("wf-1", "s", {"amount": 5000})
        b = derive_idempotency_key("wf-1", "s", {"amount": 5001})
        self.assertNotEqual(a, b)

    def test_different_workflow_different_key(self) -> None:
        self.assertNotEqual(
            derive_idempotency_key("wf-1", "s", {"x": 1}),
            derive_idempotency_key("wf-2", "s", {"x": 1}),
        )

    def test_decimal_serializes_stably(self) -> None:
        self.assertEqual(canonical_json({"f": Decimal("0.25")}), '{"f":"0.25"}')

    def test_value_equal_decimals_hash_identically(self) -> None:
        # 0.250 and 0.25 are the same value and must produce the same key, or a
        # re-derived Decimal would defeat Stripe dedup and allow a double refund.
        a = derive_idempotency_key("wf", "s", {"x": Decimal("0.250")})
        b = derive_idempotency_key("wf", "s", {"x": Decimal("0.25")})
        self.assertEqual(a, b)

    def test_decimal_never_serializes_in_exponent_form(self) -> None:
        self.assertEqual(canonical_json({"x": Decimal("100")}), '{"x":"100"}')
        self.assertEqual(canonical_json({"x": Decimal("1E+2")}), '{"x":"100"}')

    def test_requires_ids(self) -> None:
        with self.assertRaises(ValueError):
            derive_idempotency_key("", "s", {})


if __name__ == "__main__":
    unittest.main()
