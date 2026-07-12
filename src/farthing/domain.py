"""Domain types for Farthing.

The single most important design decision lives here: :class:`ExtractedRequest`
— the object the LLM produces — has **no amount field and no eligibility field**.
The type system makes it structurally impossible for a model (or a prompt
injection) to hand a dollar figure to the policy engine. The engine computes
every amount from :class:`AccountFacts`, which come from the billing system, not
from text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class ActionType(str, Enum):
    REFUND = "refund"
    CANCELLATION = "cancellation"
    PLAN_CHANGE = "plan_change"


class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    TRIALING = "trialing"
    PAST_DUE = "past_due"
    CANCELED = "canceled"


class Disposition(str, Enum):
    ELIGIBLE = "eligible"  # policy permits the action; still requires human approval
    DENY = "deny"          # policy forbids it; nothing to approve
    ESCALATE = "escalate"  # facts are missing/contradictory; a human must decide


@dataclass(frozen=True, slots=True)
class ExtractedRequest:
    """What the LLM extracted from a customer message.

    Deliberately carries NO amount and NO eligibility verdict. ``raw_text`` is
    retained for the audit trail and for the human reviewer, but the policy engine
    never reads it to decide money. ``reason_code`` is a bounded enum-like hint,
    not free text.
    """

    action_type: ActionType
    customer_ref: str
    subscription_ref: str
    invoice_ref: str | None = None
    reason_code: str | None = None
    raw_text: str = ""


@dataclass(frozen=True, slots=True)
class AccountFacts:
    """Trusted, system-of-record facts about the account. The ONLY source of the
    numbers a policy decision may use."""

    subscription_id: str
    invoice_id: str | None
    currency: str
    amount_paid_minor: int
    prior_refunds_minor: int
    purchase_ts: int  # epoch seconds
    now_ts: int       # epoch seconds (passed in, never read from the clock here)
    usage_fraction: Decimal
    subscription_status: SubscriptionStatus
    plan_refund_window_days: int
    plan_allow_proration: bool
    plan_max_refund_pct: Decimal

    def facts_are_coherent(self) -> bool:
        """Cheap internal consistency check. Incoherent facts must ESCALATE, never
        silently produce a number — and never crash. Malformed decimals (NaN,
        infinity) are exactly the shape a bad billing-system field can take, so
        they are treated as incoherent, not compared."""
        for d in (self.usage_fraction, self.plan_max_refund_pct):
            if not isinstance(d, Decimal) or not d.is_finite():
                return False
        if self.amount_paid_minor < 0 or self.prior_refunds_minor < 0:
            return False
        if self.prior_refunds_minor > self.amount_paid_minor:
            return False
        if self.now_ts < self.purchase_ts:
            return False
        if not (Decimal(0) <= self.usage_fraction <= Decimal(1)):
            return False
        if not (Decimal(0) <= self.plan_max_refund_pct <= Decimal(1)):
            return False
        if self.plan_refund_window_days < 0:
            return False
        if not (isinstance(self.currency, str) and self.currency.isalpha() and len(self.currency) == 3):
            return False
        return True


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The output of the deterministic policy engine.

    ``amount_minor`` is 0 for DENY and ESCALATE. For ELIGIBLE it is the exact,
    capped amount the human will be asked to approve — never more."""

    disposition: Disposition
    amount_minor: int
    currency: str
    reason_codes: tuple[str, ...]
    policy_version: str

    def __post_init__(self) -> None:
        if self.disposition is not Disposition.ELIGIBLE and self.amount_minor != 0:
            raise ValueError("non-ELIGIBLE decisions must carry a zero amount")
        if self.amount_minor < 0:
            raise ValueError("amount_minor must be non-negative")


@dataclass(frozen=True, slots=True)
class PreparedAction:
    """The fully-assembled, human-previewable action. This is what the approval
    inbox renders and what an approval token is bound to."""

    ticket_id: str
    action_type: ActionType
    subscription_id: str
    invoice_id: str | None
    amount_minor: int
    currency: str
    policy_version: str
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
