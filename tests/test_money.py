from __future__ import annotations

import unittest
from decimal import Decimal

from farthing.money import Money, pct_of, prorate, zero


class TestMoney(unittest.TestCase):
    def test_no_floats_allowed(self) -> None:
        with self.assertRaises(TypeError):
            Money(1.5, "USD")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            pct_of(Money(1000, "USD"), 0.5)  # type: ignore[arg-type]

    def test_non_negative(self) -> None:
        with self.assertRaises(ValueError):
            Money(-1, "USD")

    def test_currency_shape(self) -> None:
        with self.assertRaises(ValueError):
            Money(100, "US")

    def test_add_and_currency_guard(self) -> None:
        self.assertEqual(Money(100, "USD").add(Money(50, "USD")), Money(150, "USD"))
        with self.assertRaises(ValueError):
            Money(100, "USD").add(Money(50, "EUR"))

    def test_sub_clamped_never_negative(self) -> None:
        self.assertEqual(Money(50, "USD").sub_clamped(Money(80, "USD")), zero("USD"))

    def test_pct_half_up_rounding(self) -> None:
        # 12345 * 0.5 = 6172.5 -> 6173 (HALF_UP)
        self.assertEqual(pct_of(Money(12345, "USD"), Decimal("0.5")), Money(6173, "USD"))

    def test_pct_bounds(self) -> None:
        with self.assertRaises(ValueError):
            pct_of(Money(100, "USD"), Decimal("1.5"))

    def test_prorate_cannot_exceed_amount(self) -> None:
        # remaining > total is clamped, so proration never exceeds the amount.
        self.assertEqual(
            prorate(Money(1000, "USD"), Decimal("30"), Decimal("14")),
            Money(1000, "USD"),
        )

    def test_prorate_partial(self) -> None:
        # 1000 * 7/14 = 500
        self.assertEqual(
            prorate(Money(1000, "USD"), Decimal("7"), Decimal("14")),
            Money(500, "USD"),
        )

    def test_prorate_requires_positive_total(self) -> None:
        with self.assertRaises(ValueError):
            prorate(Money(1000, "USD"), Decimal("1"), Decimal("0"))


if __name__ == "__main__":
    unittest.main()
