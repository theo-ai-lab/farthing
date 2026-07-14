"""Seed a Stripe TEST-MODE account with the objects one Farthing ticket needs, and
print the real ids to paste into examples/run_ticket.py.

Creates: a customer with a test card, a product + price carrying the plan terms in
price metadata (what the policy engine reads), a subscription, and its first paid
invoice (which produces a charge). The seeded plan is deliberately refund-eligible
(recent purchase, low usage, full refund window) so the happy-path demo yields an
ELIGIBLE decision you can approve.

Needs a Stripe TEST secret key with WRITE scope — this is a one-time SETUP concern,
separate from the runtime PREPARE/EXECUTE restricted keys. It refuses any key that
is not sk_test_...  Never run it against a live key.

    FARTHING_STRIPE_SEED_KEY=sk_test_xxx uv run python scripts/seed_stripe_testmode.py
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import stripe


def _require_test_key() -> str:
    key = os.environ.get("FARTHING_STRIPE_SEED_KEY", "")
    if not key.startswith("sk_test_"):
        sys.exit("Set FARTHING_STRIPE_SEED_KEY to a Stripe TEST secret key (sk_test_...). Refusing to run.")
    return key


def _charge_id(client: stripe.StripeClient, invoice: Any) -> str | None:
    """Resolve the charge for an invoice across API-version shapes: legacy
    invoice.charge, else the invoice payment -> payment_intent -> latest_charge.
    Retries briefly because a subscription's first invoice settles asynchronously."""
    charge = getattr(invoice, "charge", None)
    if charge:
        return str(charge)
    inv: Any = client.v1.invoices.retrieve(invoice.id, {"expand": ["payments"]})
    pi_id = getattr(inv, "payment_intent", None)
    if not pi_id:
        payments = getattr(inv, "payments", None)
        data = getattr(payments, "data", None) if payments is not None else None
        if data:
            payment = getattr(data[0], "payment", None)
            pi_id = getattr(payment, "payment_intent", None) if payment is not None else None
    if not pi_id:
        return None
    for _ in range(10):
        pi: Any = client.v1.payment_intents.retrieve(str(pi_id))
        latest = getattr(pi, "latest_charge", None)
        if latest:
            return str(latest)
        time.sleep(1.5)
    return None


def main() -> None:
    client = stripe.StripeClient(_require_test_key())

    customer: Any = client.v1.customers.create({
        "name": "Farthing Demo Customer",
        "email": "demo-customer@example.com",
        "payment_method": "pm_card_visa",
        "invoice_settings": {"default_payment_method": "pm_card_visa"},
    })

    product: Any = client.v1.products.create({"name": "Farthing Demo Plan"})
    price: Any = client.v1.prices.create({
        "product": product.id,
        "unit_amount": 5000,  # $50.00 / month
        "currency": "usd",
        "recurring": {"interval": "month"},
        "metadata": {  # the plan terms the policy engine reads off the price
            "usage_fraction": "0.05",       # ~5% used -> eligible for a near-full refund
            "refund_window_days": "14",
            "allow_proration": "true",
            "max_refund_pct": "1.0",
        },
    })

    subscription: Any = client.v1.subscriptions.create({
        "customer": customer.id,
        "items": [{"price": price.id}],
        "expand": ["latest_invoice"],
    })
    invoice: Any = subscription.latest_invoice
    charge_id = _charge_id(client, invoice)

    print("\nSeeded Stripe test-mode objects:")
    print(f"  customer_id     = {customer.id}")
    print(f"  subscription_id = {subscription.id}")
    print(f"  invoice_id      = {invoice.id}")
    print(f"  charge_id       = {charge_id}")

    if not charge_id:
        print(
            "\n!! No charge id resolved. The first invoice may not have been paid yet, or this API\n"
            "   version exposes the charge only via the payment_intent. A refund needs a bound\n"
            "   charge, so fetch_facts must return one — re-run after the invoice settles, or check\n"
            "   how invoice->charge is exposed on your pinned Stripe API version (see docs/DEFENSE.md)."
        )

    print("\nPaste into examples/run_ticket.py (TicketInput):")
    print(f'    subscription_id="{subscription.id}",')
    print(f'    invoice_id="{invoice.id}",')
    print(f'    charge_id="{charge_id}",')


if __name__ == "__main__":
    main()
