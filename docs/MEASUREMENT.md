# Measurement methodology

Farthing's headline numbers are measured, not asserted, and this document says
exactly how — including the honest limits, because a constructed baseline
presented as production data is the fastest way to lose a reviewer's trust.

## 1. Safety: adversarial golden set (hard CI gate)

`tests/test_golden_set.py` runs all 152 labeled cases in
`tests/golden/adversarial_set.json` through the real policy engine and asserts, on
every case:

- **text-invariance** — re-running with benign text yields the identical amount;
- **ceiling** — the amount never exceeds the facts-derived policy ceiling;
- **incoherent facts never pay** — bad data escalates with \$0;
- **attacks blocked** — no attack extracts more than the ceiling.

It prints the headline: number of attacks, and the amount demanded out-of-policy
and blocked **per currency** — never as one cross-currency total, which would be a
number with no unit. The gate fails the build if a single out-of-policy amount
would execute, asserted per currency. This is a measurement over a fixed, inspectable input set — reproducible
by anyone who clones the repo, with no network.

> The model id used for any LLM-in-the-loop measurement is pinned. Bumping the
> model invalidates the evidence and forces a full re-run — `model: latest` is
> banned in the gate.

## 2. Latency: manual baseline vs Farthing

- **Manual baseline** — an industry-standard assumption of one to two business
  days for a human-handled billing refund. This is an assumption, not a measured
  number, and it is labeled as such wherever it appears.
- **Farthing** — wall-clock from ticket ingestion to executed refund on the demo
  tenant, including the human approval click, with the approval-wait time broken
  out separately (it is human latency, not system latency). With no production
  ticket sample committed, this is a method statement, not a headline number —
  see the honest limit below.

## 3. Integrity: nightly reconciliation

The reconciliation report (`farthing.reconcile`) compares the ledger to Stripe
refund-by-refund. Agreement means every executed refund appears on both sides
with the same amount; an orphan on either side is reported as an incident, never
silently dropped.

## The honest limit

With no real merchant, the volume and latency numbers are **demo-tenant / Stripe
test-mode** measurements, not production traffic. Every figure in the README is
framed that way. The safety numbers (the golden-set
gate) are fully real regardless of merchant, because they exercise the actual
decision engine on a fixed adversarial distribution. The single most valuable next
step is one design-partner merchant whose latency and integrity numbers are
production-observed.
