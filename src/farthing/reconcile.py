"""Nightly Stripe-to-ledger reconciliation.

Run on a Temporal Schedule. Pulls every refund Stripe recorded in the window,
pulls every executed-refund entry from the audit ledger, and asserts they agree:

* every ledger-recorded refund exists in Stripe with the same amount, and
* every Stripe refund has a matching ledger entry (no money moved off-ledger).

A discrepancy is an incident, not a warning: an unmatched Stripe refund means
something moved money without going through the guard, and an unmatched ledger
entry means a recorded refund never actually executed.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ledger import LedgerEntry


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    window_start_ts: int
    window_end_ts: int
    ledger_refunds: int
    stripe_refunds: int
    matched: int
    unmatched_in_stripe: tuple[str, ...]   # Stripe refund ids with no ledger entry
    unmatched_in_ledger: tuple[str, ...]   # ledger idempotency keys with no Stripe refund

    @property
    def clean(self) -> bool:
        return not self.unmatched_in_stripe and not self.unmatched_in_ledger


def reconcile(
    ledger_entries: list[LedgerEntry],
    stripe_refunds: list[dict],
    *,
    window_start_ts: int,
    window_end_ts: int,
) -> ReconciliationReport:
    """Pure reconciliation given both sides. The Stripe fetch lives in an activity;
    the matching logic is pure and unit-testable.

    Matching key is the deterministic idempotency key, which Stripe echoes back in
    ``request.idempotency_key`` / stored metadata on the refund."""
    ledger_by_key = {
        e.idempotency_key: e
        for e in ledger_entries
        if e.idempotency_key and e.disposition == "refunded"
        and window_start_ts <= e.timestamp <= window_end_ts
    }
    stripe_by_key = {r["idempotency_key"]: r for r in stripe_refunds if r.get("idempotency_key")}

    matched = 0
    unmatched_stripe: list[str] = []
    for key, r in stripe_by_key.items():
        led = ledger_by_key.get(key)
        if led is not None and led.amount_minor == int(r["amount"]):
            matched += 1
        else:
            unmatched_stripe.append(r["id"])

    unmatched_ledger = [k for k in ledger_by_key if k not in stripe_by_key]

    return ReconciliationReport(
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
        ledger_refunds=len(ledger_by_key),
        stripe_refunds=len(stripe_by_key),
        matched=matched,
        unmatched_in_stripe=tuple(unmatched_stripe),
        unmatched_in_ledger=tuple(unmatched_ledger),
    )
