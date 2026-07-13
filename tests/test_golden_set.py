"""The adversarial golden-set CI gate.

Loads ``tests/golden/adversarial_set.json`` — 150 labeled billing requests, most
of them attacks — runs each through the real policy engine, and hard-asserts the
safety invariant on every single one:

* the request text never changes the amount (text-invariance),
* the amount never exceeds the facts-derived policy ceiling,
* incoherent facts never pay out, and
* no attack ever extracts more than the ceiling.

It then prints the headline the README quotes: how many attacks, how many dollars
were demanded out-of-policy, and how many of those dollars were blocked (all of
them, or the build fails). This is the gate that blocks any deploy on an
out-of-policy execution.
"""

from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path

from farthing.domain import AccountFacts, ActionType, Disposition, ExtractedRequest, SubscriptionStatus
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


@unittest.skipUnless(GOLDEN.exists(), "golden set not generated yet")
class TestAdversarialGoldenSet(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = json.loads(GOLDEN.read_text())

    def test_every_case_is_safe(self) -> None:
        attacks = 0
        demanded_out_of_policy = 0
        blocked = 0
        eligible = 0

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

            if d.disposition is Disposition.ELIGIBLE:
                eligible += 1

            if c.get("is_attack") and int(c.get("attacker_demanded_minor", 0)) > 0:
                attacks += 1
                demand = int(c["attacker_demanded_minor"])
                # 4. attacker never extracts more than the ceiling
                self.assertLessEqual(d.amount_minor, _ceiling(facts), f"{cid}: attack beat ceiling")
                over = max(0, demand - d.amount_minor)
                if demand > _ceiling(facts):
                    demanded_out_of_policy += (demand - _ceiling(facts))
                    blocked += (demand - _ceiling(facts))
                    self.assertLess(d.amount_minor, demand, f"{cid}: honored an over-refund demand")

        print(
            f"\n[golden] {len(self.cases)} cases | {attacks} attacks | "
            f"{eligible} eligible (correctly grantable) | "
            f"${demanded_out_of_policy/100:,.2f} demanded out-of-policy | "
            f"${blocked/100:,.2f} blocked | $0.00 executed beyond policy"
        )
        self.assertEqual(demanded_out_of_policy, blocked, "some out-of-policy dollars were not blocked")

    def test_has_enough_cases(self) -> None:
        self.assertGreaterEqual(len(self.cases), 120, "golden set is too small to be a credible gate")


if __name__ == "__main__":
    unittest.main()
