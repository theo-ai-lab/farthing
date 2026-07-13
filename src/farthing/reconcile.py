"""Nightly Stripe-to-ledger reconciliation — the detective control.

Run on a Temporal Schedule. Pulls every refund Stripe recorded in the window,
pulls every executed-refund entry from the audit ledger, and asserts they agree.
A discrepancy is an incident, not a warning.

This control is designed to FAIL CLOSED. A refund Stripe recorded that has no
matching ledger entry — including one with no Farthing idempotency key at all (a
dashboard refund, a compromised key, a refund whose key we never persisted) — is
reported as an unmatched incident, never silently dropped. A single malformed
Stripe record is quarantined, not allowed to abort the whole run. Two refunds
sharing a key are flagged as a collision, never deduplicated away. Amount AND
currency must both agree for a match.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ledger import LedgerEntry


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    window_start_ts: int
    window_end_ts: int
    ledger_refunds: int
    stripe_refunds: int
    matched: int
    # Stripe refund ids with no ledger match (includes keyless / off-ledger refunds).
    unmatched_in_stripe: tuple[str, ...] = field(default_factory=tuple)
    # Ledger keys with no Stripe refund.
    unmatched_in_ledger: tuple[str, ...] = field(default_factory=tuple)
    # Same key, different amount or currency.
    mismatched: tuple[str, ...] = field(default_factory=tuple)
    # A key seen on more than one refund (either side).
    duplicate_keys: tuple[str, ...] = field(default_factory=tuple)
    # Unparseable Stripe records.
    malformed: tuple[str, ...] = field(default_factory=tuple)

    @property
    def clean(self) -> bool:
        return not (
            self.unmatched_in_stripe or self.unmatched_in_ledger
            or self.mismatched or self.duplicate_keys or self.malformed
        )


def reconcile(
    ledger_entries: list[LedgerEntry],
    stripe_refunds: list[dict],
    *,
    window_start_ts: int,
    window_end_ts: int,
) -> ReconciliationReport:
    """Pure reconciliation given both sides. The Stripe fetch lives in an activity;
    the matching logic is pure and unit-testable."""
    # Index the ledger by key, detecting collisions (two 'refunded' rows sharing a
    # key must never be collapsed — that would hide a double payout).
    ledger_by_key: dict[str, LedgerEntry] = {}
    dup_keys: set[str] = set()
    for e in ledger_entries:
        if e.disposition != "refunded" or not e.idempotency_key:
            continue
        if not (window_start_ts <= e.timestamp <= window_end_ts):
            continue
        if e.idempotency_key in ledger_by_key:
            dup_keys.add(e.idempotency_key)
        ledger_by_key[e.idempotency_key] = e

    matched = 0
    unmatched_stripe: list[str] = []
    mismatched: list[str] = []
    malformed: list[str] = []
    stripe_keys_seen: set[str] = set()
    stripe_count = 0

    for r in stripe_refunds:
        # Defensive parse: a bad record is quarantined, not fatal.
        try:
            rid = str(r["id"])
            amount = int(r["amount"])
            currency = str(r["currency"]).upper()
        except (KeyError, ValueError, TypeError):
            malformed.append(str(r.get("id", "<no-id>")) if isinstance(r, dict) else "<non-dict>")
            continue
        stripe_count += 1
        key = r.get("idempotency_key") if isinstance(r, dict) else None

        if not key:
            # A refund with no Farthing key is exactly the off-ledger money this
            # control exists to catch. Never drop it.
            unmatched_stripe.append(rid)
            continue
        if key in stripe_keys_seen:
            dup_keys.add(key)
        stripe_keys_seen.add(key)

        led = ledger_by_key.get(key)
        if led is None:
            unmatched_stripe.append(rid)
        elif led.amount_minor != amount or led.currency.upper() != currency:
            mismatched.append(rid)
        else:
            matched += 1

    unmatched_ledger = [k for k in ledger_by_key if k not in stripe_keys_seen]

    return ReconciliationReport(
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
        ledger_refunds=len(ledger_by_key),
        stripe_refunds=stripe_count,
        matched=matched,
        unmatched_in_stripe=tuple(unmatched_stripe),
        unmatched_in_ledger=tuple(unmatched_ledger),
        mismatched=tuple(mismatched),
        duplicate_keys=tuple(sorted(dup_keys)),
        malformed=tuple(malformed),
    )
