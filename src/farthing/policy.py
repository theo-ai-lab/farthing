"""The deterministic, versioned policy engine.

``decide()`` is a pure function of ``(ExtractedRequest, AccountFacts)``. It reads
the *action type* from the request and everything else — every number — from the
trusted facts. It never reads ``raw_text`` and never sees a customer-demanded
amount, because :class:`~farthing.domain.ExtractedRequest` has no such field.

The engine is the reason a social-engineered "please refund me $500 on my $50
plan" cannot move $500: the engine computes the capped, policy-permitted amount
from facts and returns that; the $450 difference is structurally impossible to
emit.

Bump ``POLICY_VERSION`` whenever a rule changes. The version is stamped on every
decision and written into the audit ledger, so a historical entry can always be
re-derived under the exact rules that produced it.
"""

from __future__ import annotations

from decimal import Decimal

from .domain import (
    AccountFacts,
    ActionType,
    Disposition,
    ExtractedRequest,
    PolicyDecision,
    SubscriptionStatus,
)
from .money import Money, pct_of

POLICY_VERSION = "2026.07.1"

SECONDS_PER_DAY = 86_400


def _refund_ceiling(facts: AccountFacts) -> Money:
    """The most that could be refunded on THIS request.

    The percentage cap is CUMULATIVE over the account's lifetime: it bounds
    *total* refunds to ``max_refund_pct`` of the original payment, so a prior
    refund reduces this transaction's cap. Otherwise paid=100, prior=50, cap=40%
    would still allow 40 more (total 90) and breach the 40% cap. The ceiling is
    the smaller of the remaining balance and the remaining room under the cap."""
    paid = Money(facts.amount_paid_minor, facts.currency)
    already = Money(facts.prior_refunds_minor, facts.currency)
    remaining = paid.sub_clamped(already)
    cap_total = pct_of(paid, facts.plan_max_refund_pct)
    cap_remaining = cap_total.sub_clamped(already)
    return remaining.min(cap_remaining)


def _days_since_purchase(facts: AccountFacts) -> Decimal:
    return Decimal(facts.now_ts - facts.purchase_ts) / Decimal(SECONDS_PER_DAY)


def _decide_refund(facts: AccountFacts) -> PolicyDecision:
    reasons: list[str] = []
    ceiling = _refund_ceiling(facts)

    if ceiling.minor_units == 0:
        return PolicyDecision(
            Disposition.DENY, 0, facts.currency,
            ("fully_refunded_or_zero_ceiling",), POLICY_VERSION,
        )

    days = _days_since_purchase(facts)
    window = Decimal(facts.plan_refund_window_days)

    # Full refund inside the window, provided usage is low enough to be a genuine
    # "changed my mind" rather than "used it and wants money back".
    if days <= window:
        if facts.usage_fraction <= Decimal("0.25"):
            reasons.append("within_window_low_usage")
            amount = ceiling
        elif facts.plan_allow_proration:
            # Refund the UNUSED value: ceiling * (1 - usage_fraction). The math
            # uses usage, matching the reason code, and is clamped to the ceiling.
            reasons.append("within_window_prorated_for_usage")
            unused = Decimal(1) - facts.usage_fraction
            amount = pct_of(ceiling, unused)
        else:
            return PolicyDecision(
                Disposition.DENY, 0, facts.currency,
                ("within_window_but_used_no_proration",), POLICY_VERSION,
            )
        if amount.minor_units == 0:
            return PolicyDecision(
                Disposition.DENY, 0, facts.currency,
                ("prorated_to_zero",), POLICY_VERSION,
            )
        return PolicyDecision(
            Disposition.ELIGIBLE, amount.minor_units, facts.currency,
            tuple(reasons), POLICY_VERSION,
        )

    # Outside the window: no automatic eligibility. A human may still choose to
    # act, so escalate rather than hard-deny — but the engine offers zero.
    return PolicyDecision(
        Disposition.ESCALATE, 0, facts.currency,
        ("outside_refund_window",), POLICY_VERSION,
    )


def _decide_cancellation(facts: AccountFacts) -> PolicyDecision:
    # Cancellation moves no money by itself; a refund, if any, is a separate action.
    if facts.subscription_status is SubscriptionStatus.CANCELED:
        return PolicyDecision(
            Disposition.DENY, 0, facts.currency,
            ("already_canceled",), POLICY_VERSION,
        )
    return PolicyDecision(
        Disposition.ELIGIBLE, 0, facts.currency,
        ("cancellation_permitted",), POLICY_VERSION,
    )


def _decide_plan_change(facts: AccountFacts) -> PolicyDecision:
    # The engine only GATES eligibility here and moves no money (amount 0). A plan
    # change that should move money must, if ever wired for execution, compute and
    # bind a preview proration amount into PreparedAction and the signed token and
    # route through the same guard — it must never execute an unbound amount.
    if facts.subscription_status not in (
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.TRIALING,
    ):
        return PolicyDecision(
            Disposition.ESCALATE, 0, facts.currency,
            ("plan_change_requires_active_subscription",), POLICY_VERSION,
        )
    return PolicyDecision(
        Disposition.ELIGIBLE, 0, facts.currency,
        ("plan_change_permitted",), POLICY_VERSION,
    )


def decide(req: ExtractedRequest, facts: AccountFacts) -> PolicyDecision:
    """Pure, deterministic policy decision. Same inputs → same output, always."""
    if not facts.facts_are_coherent():
        return PolicyDecision(
            Disposition.ESCALATE, 0, facts.currency or "XXX",
            ("incoherent_facts",), POLICY_VERSION,
        )

    if req.action_type is ActionType.REFUND:
        return _decide_refund(facts)
    if req.action_type is ActionType.CANCELLATION:
        return _decide_cancellation(facts)
    if req.action_type is ActionType.PLAN_CHANGE:
        return _decide_plan_change(facts)

    # Unreachable while ActionType is exhaustive; escalate defensively rather than
    # fall through to any money-moving path.
    return PolicyDecision(
        Disposition.ESCALATE, 0, facts.currency,
        ("unknown_action_type",), POLICY_VERSION,
    )
