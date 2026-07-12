"""Test helpers to build coherent facts/requests without repetition."""

from __future__ import annotations

from decimal import Decimal

from farthing.domain import (
    AccountFacts,
    ActionType,
    ExtractedRequest,
    SubscriptionStatus,
)

DAY = 86_400


def facts(
    *,
    amount_paid_minor: int = 5000,
    prior_refunds_minor: int = 0,
    days_since_purchase: int = 3,
    usage_fraction: str = "0.05",
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    window_days: int = 14,
    allow_proration: bool = True,
    max_refund_pct: str = "1.0",
    currency: str = "USD",
) -> AccountFacts:
    now = 1_800_000_000
    return AccountFacts(
        subscription_id="sub_test",
        invoice_id="in_test",
        currency=currency,
        amount_paid_minor=amount_paid_minor,
        prior_refunds_minor=prior_refunds_minor,
        purchase_ts=now - days_since_purchase * DAY,
        now_ts=now,
        usage_fraction=Decimal(usage_fraction),
        subscription_status=status,
        plan_refund_window_days=window_days,
        plan_allow_proration=allow_proration,
        plan_max_refund_pct=Decimal(max_refund_pct),
    )


def refund_request(raw_text: str = "I'd like a refund please") -> ExtractedRequest:
    return ExtractedRequest(
        action_type=ActionType.REFUND,
        customer_ref="cus_test",
        subscription_ref="sub_test",
        invoice_ref="in_test",
        raw_text=raw_text,
    )
