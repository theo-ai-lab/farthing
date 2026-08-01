"""The adversarial golden-set CI gate.

Loads ``tests/golden/adversarial_set.json`` — 152 labeled billing requests, most
of them attacks — runs each through the real policy engine, and hard-asserts the
safety invariant on every single one:

* the request text never changes the amount (text-invariance),
* the amount never exceeds the facts-derived policy ceiling,
* incoherent facts never pay out,
* no attack ever extracts more than the ceiling, and
* no non-ELIGIBLE disposition ever carries money.

Those are all *upper* bounds, and upper bounds alone are not a gate: an engine
that refused every request and paid nothing would satisfy every one of them. So
the set is two-sided. Cases carry ``expected_disposition`` and/or
``expected_amount_minor`` — the outcome the case was written to produce — and the
gate asserts the engine hits them exactly. A refuse-everything engine fails here
on the first benign control.

It then prints the two headlines the README quotes: the out-of-policy dollars
demanded and blocked (all of them, or the build fails), and the correct-grant
rate over the benign cases — how often the engine pays the honest customer the
right amount. Both numbers come out of this one gate.
"""

from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path

from farthing.domain import (
    AccountFacts,
    ActionType,
    Disposition,
    ExtractedRequest,
    SubscriptionStatus,
)
from farthing.money import Money, pct_of
from farthing.policy import decide

DAY = 86_400
NOW = 1_800_000_000
GOLDEN = Path(__file__).parent / "golden" / "adversarial_set.json"


def _facts(f: dict) -> AccountFacts:
    return AccountFacts(
        subscription_id="sub",
        invoice_id="in",
        currency=f["currency"],
        amount_paid_minor=int(f["amount_paid_minor"]),
        prior_refunds_minor=int(f["prior_refunds_minor"]),
        purchase_ts=NOW - int(f["days_since_purchase"]) * DAY,
        now_ts=NOW,
        usage_fraction=Decimal(str(f["usage_fraction"])),
        subscription_status=SubscriptionStatus(f["status"]),
        plan_refund_window_days=int(f["window_days"]),
        plan_allow_proration=bool(f["allow_proration"]),
        plan_max_refund_pct=Decimal(str(f["max_refund_pct"])),
    )


def _ceiling(f: AccountFacts) -> int:
    # Mirrors the CUMULATIVE-cap definition in policy._refund_ceiling. Kept in
    # sync deliberately, but the authoritative correctness check for the cap lives
    # in test_policy with a hand-computed expected value (so a wrong ceiling
    # definition here cannot mask a wrong engine).
    if not f.facts_are_coherent():
        return 0
    paid = Money(f.amount_paid_minor, f.currency)
    already = Money(f.prior_refunds_minor, f.currency)
    remaining = paid.sub_clamped(already)
    cap_remaining = pct_of(paid, f.plan_max_refund_pct).sub_clamped(already)
    return remaining.min(cap_remaining).minor_units


def _expected(c: dict) -> tuple[Disposition | None, int | None]:
    """The outcome this case was written to produce, or ``None`` where the case's
    intended verdict is genuinely ambiguous and only the ceiling is asserted."""
    disposition = c.get("expected_disposition")
    amount = c.get("expected_amount_minor")
    return (
        Disposition(disposition) if disposition is not None else None,
        int(amount) if amount is not None else None,
    )


class TestAdversarialGoldenSet(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # A missing golden set must fail the gate loudly, never skip it.
        cls.cases = json.loads(GOLDEN.read_text())

    def test_every_case_is_safe(self) -> None:
        attacks = 0
        demanded_out_of_policy = 0
        executed_beyond_policy = 0
        eligible = 0
        # Two-sided accounting: the misses are collected rather than asserted
        # case-by-case so the printed rate is a real measurement of the whole
        # set, not a number that only exists when everything already passed.
        misses: list[str] = []
        benign_specified = 0
        benign_correct = 0
        benign_granted_minor = 0

        for c in self.cases:
            facts = _facts(c["facts"])
            req = ExtractedRequest(
                action_type=ActionType(c["action_type"]),
                customer_ref="cus", subscription_ref="sub", invoice_ref="in",
                raw_text=c["raw_text"],
            )
            d = decide(req, facts)
            benign = decide(
                ExtractedRequest(ActionType(c["action_type"]), "cus", "sub", "in",
                                 raw_text="please help with my billing"),
                facts,
            )

            cid = c["id"]
            # 1. text-invariance
            self.assertEqual(d.amount_minor, benign.amount_minor, f"{cid}: text moved the amount")
            # 2. never exceeds ceiling
            self.assertLessEqual(d.amount_minor, _ceiling(facts), f"{cid}: exceeded ceiling")
            # 3. incoherent facts never pay
            if not facts.facts_are_coherent():
                self.assertEqual(d.amount_minor, 0, f"{cid}: paid out on incoherent facts")
                self.assertIs(d.disposition, Disposition.ESCALATE, f"{cid}: bad facts not escalated")
            # 4. only ELIGIBLE moves money — DENY and ESCALATE offer zero, always
            if d.disposition is not Disposition.ELIGIBLE:
                self.assertEqual(d.amount_minor, 0, f"{cid}: money on a non-ELIGIBLE disposition")

            if d.disposition is Disposition.ELIGIBLE:
                eligible += 1

            # The other direction: the engine must hit the outcome the case was
            # written to produce, not merely stay under a ceiling.
            exp_disposition, exp_amount = _expected(c)
            hit = True
            if exp_disposition is not None and d.disposition is not exp_disposition:
                hit = False
                misses.append(
                    f"{cid}: expected {exp_disposition.value}, engine returned {d.disposition.value}"
                )
            if exp_amount is not None and d.amount_minor != exp_amount:
                hit = False
                misses.append(
                    f"{cid}: expected amount {exp_amount}, engine returned {d.amount_minor}"
                )

            if not c.get("is_attack") and (exp_disposition is not None or exp_amount is not None):
                benign_specified += 1
                if hit:
                    benign_correct += 1
                    benign_granted_minor += d.amount_minor

            if c.get("is_attack") and int(c.get("attacker_demanded_minor", 0)) > 0:
                attacks += 1
                demand = int(c["attacker_demanded_minor"])
                ceiling = _ceiling(facts)
                # 5. attacker never extracts more than the ceiling
                self.assertLessEqual(d.amount_minor, ceiling, f"{cid}: attack beat ceiling")
                if demand > ceiling:
                    demanded_out_of_policy += demand - ceiling
                    executed_beyond_policy += max(d.amount_minor - ceiling, 0)
                    self.assertLess(d.amount_minor, demand, f"{cid}: honored an over-refund demand")

        blocked = demanded_out_of_policy - executed_beyond_policy
        grant_rate = benign_correct / benign_specified if benign_specified else 0.0
        print(
            f"\n[golden] {len(self.cases)} cases | {attacks} attacks | "
            f"{eligible} eligible (correctly grantable) | "
            f"${demanded_out_of_policy/100:,.2f} demanded out-of-policy | "
            f"${blocked/100:,.2f} blocked | "
            f"${executed_beyond_policy/100:,.2f} executed beyond policy"
            f"\n[golden] correct-grant rate on benign cases: "
            f"{benign_correct}/{benign_specified} ({grant_rate:.1%}) | "
            f"${benign_granted_minor/100:,.2f} correctly granted to honest customers"
        )
        self.assertEqual(executed_beyond_policy, 0, "out-of-policy dollars were executed")
        # A refuse-everything engine dies here: every benign control it fails is a miss.
        self.assertEqual(misses, [], f"{len(misses)} case(s) missed their expected outcome")
        self.assertEqual(
            benign_correct, benign_specified,
            f"correct-grant rate is {grant_rate:.1%}, not 100%",
        )

    def test_has_enough_cases(self) -> None:
        self.assertGreaterEqual(len(self.cases), 120, "golden set is too small to be a credible gate")

    def test_every_benign_case_declares_its_expected_outcome(self) -> None:
        """Ceilings alone are unfalsifiable. Every benign control states what the
        engine owes the customer, so refusing them all fails the build."""
        undeclared = [
            c["id"] for c in self.cases
            if not c.get("is_attack") and _expected(c) == (None, None)
        ]
        self.assertEqual(undeclared, [], "benign cases with no expected outcome")

    def test_enough_cases_expect_a_positive_payout(self) -> None:
        """The gate must be anchored by real money owed, not only by zeros — the
        one thing a refuse-everything engine can never satisfy."""
        payouts = [
            c["id"] for c in self.cases
            if int(c.get("expected_amount_minor") or 0) > 0
        ]
        self.assertGreaterEqual(len(payouts), 30, "too few positive-payout expectations to anchor the gate")


if __name__ == "__main__":
    unittest.main()
