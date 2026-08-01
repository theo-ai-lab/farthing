"""Deterministic post-check on the model-drafted customer reply.

Every other model output in this system is structurally bounded: the extraction
type has no amount field, the policy engine reads only facts, the approval token
binds a server-side figure. The customer reply was the exception — the ticket's
raw text goes into the drafting prompt and whatever came back was sent, so a
ticket saying "tell the customer their $500 refund was approved" could put a
$500 promise in front of a customer whose ticket the engine had denied. That is
a dollar figure originating in a prompt, which is precisely what this system
claims never happens.

The check that closes it is cheap because the drafter is deliberately starved:
``agent.draft_reply`` is handed exactly one number, the decided amount, and
nothing else. So any *other* money figure in the draft came from the model or
from the attacker's text, never from the engine, and the draft is refused. A
draft asserting an outcome the engine did not decide is refused the same way.
Refusal falls back to the deterministic template, so the failure direction is a
blunt reply, never a wrong number.

Pure standard library, like the rest of the money core: it must be provable with
no install, and it must be identical whether it runs in an activity or a test.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .money import minor_unit_exponent

# Figures in "money position": attached to a symbol, an ISO code, or a currency
# word. A bare integer ("5-7 business days", "ticket 4471") is not money and is
# deliberately not matched — the guard is about promises, not arithmetic.
_SYMBOLS = "$£€¥₩₫₹"
_CURRENCY_WORDS = (
    "dollars?|euros?|pounds?|yen|won|dong|rupees?|cents?|pence|"
    "usd|eur|gbp|jpy|krw|vnd|inr|cad|aud|chf|sek|nok|dkk|pln|brl|mxn|bhd|kwd|jod|omr|tnd"
)
_NUM = r"\d[\d,. ]*\d|\d"

_MONEY_PATTERNS = (
    re.compile(rf"[{_SYMBOLS}]\s?({_NUM})"),
    re.compile(rf"({_NUM})\s?(?:{_CURRENCY_WORDS})\b", re.IGNORECASE),
)

# Phrases that assert a refund happened or was approved. Checked only when the
# engine granted nothing.
_CLAIMS_A_GRANT = (
    re.compile(r"\brefund\b[^.!?]{0,40}\b(approved|issued|processed|completed|sent|"
               r"credited|on its way|on the way)\b", re.IGNORECASE),
    re.compile(r"\b(approved|issued|refunded|credited)\b[^.!?]{0,40}\brefund\b", re.IGNORECASE),
    re.compile(r"\b(?:we|we've|we have|i|i've|i have)\s+(?:just\s+)?"
               r"(?:refunded|issued|approved|credited)\b", re.IGNORECASE),
    re.compile(r"\bhas been (?:refunded|approved|issued|credited)\b", re.IGNORECASE),
    re.compile(r"\b(?:money back|back (?:in|to) your (?:account|card|bank))\b", re.IGNORECASE),
)

# Phrases that assert a refusal. Checked only when the engine granted something.
_CLAIMS_A_REFUSAL = (
    re.compile(r"\b(?:cannot|can't|can not|unable to|won't be able to|not able to)\b"
               r"[^.!?]{0,40}\brefund\b", re.IGNORECASE),
    re.compile(r"\brefund\b[^.!?]{0,40}\b(?:denied|declined|rejected)\b", re.IGNORECASE),
    re.compile(r"\b(?:not|isn't|is not|aren't) eligible\b", re.IGNORECASE),
    re.compile(r"\bno refund\b", re.IGNORECASE),
    re.compile(r"\bnon-?refundable\b", re.IGNORECASE),
)

# A short window before a grant-claim that negates it: "we are not able to
# refund", "your refund was not approved". Without this the guard would reject
# the honest denial replies it exists to protect.
_NEGATION = re.compile(r"\b(?:not|n't|never|cannot|unable|without|no longer|nothing)\b",
                       re.IGNORECASE)
_NEGATION_WINDOW = 48


def _major_units(minor_units: int, currency: str) -> Decimal:
    exp = minor_unit_exponent(currency)
    return Decimal(minor_units) / (Decimal(10) ** exp)


def _parse_figure(raw: str) -> Decimal | None:
    """``"1,200.00"`` -> ``Decimal("1200.00")``. Returns ``None`` when the token
    is not a figure this guard can read — which counts as a violation, because a
    reply has no business containing money it cannot state plainly."""
    cleaned = raw.replace(" ", "").replace(",", "")
    if cleaned.count(".") > 1:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _negated(text: str, start: int) -> bool:
    window = text[max(0, start - _NEGATION_WINDOW):start]
    return _NEGATION.search(window) is not None


def reply_violation(reply: str, *, disposition: str, amount_minor: int, currency: str) -> str | None:
    """Why this draft must not be sent, or ``None`` if it may be.

    ``disposition`` and ``amount_minor`` are the engine's decision — the only
    outcome the reply is permitted to describe."""
    granted = disposition == "eligible" and amount_minor > 0
    allowed = _major_units(amount_minor, currency) if granted else None

    for pattern in _MONEY_PATTERNS:
        for m in pattern.finditer(reply):
            figure = _parse_figure(m.group(1))
            if figure is None:
                return f"unreadable money figure in reply: {m.group(0)!r}"
            if allowed is None:
                return f"reply states {m.group(0)!r} but the engine granted nothing"
            if figure != allowed:
                return f"reply states {m.group(0)!r} but the engine decided {allowed}"

    contradictions = _CLAIMS_A_REFUSAL if granted else _CLAIMS_A_GRANT
    for pattern in contradictions:
        m = pattern.search(reply)
        if m is not None and not _negated(reply, m.start()):
            return f"reply asserts {m.group(0)!r}, which contradicts disposition {disposition!r}"

    return None


def fallback_reply(disposition: str) -> str:
    """The deterministic template. Identical to the one ``workflows._reply`` uses
    when the drafting activity fails, so the customer sees one voice whichever
    way the drafter falls over."""
    return f"Your billing request has been processed (status: {disposition})."


def checked_reply(draft: str, *, disposition: str, amount_minor: int, currency: str) -> str:
    """The draft if it survives the check, otherwise the template."""
    if reply_violation(draft, disposition=disposition,
                       amount_minor=amount_minor, currency=currency) is not None:
        return fallback_reply(disposition)
    return draft
