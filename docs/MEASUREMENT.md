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

It prints the headline: number of attacks, dollars demanded out-of-policy, and
dollars blocked. The gate fails the build if a single out-of-policy dollar would
execute. This is a measurement over a fixed, inspectable input set — reproducible
by anyone who clones the repo, with no network.

> The model id used for any LLM-in-the-loop measurement is pinned. Bumping the
> model invalidates the evidence and forces a full re-run — `model: latest` is
> banned in the gate.

## 2. Latency: manual baseline vs Farthing

- **Manual baseline** — the documented industry norm for a human handling a
  billing refund is one to two business days per ticket (source cited inline in
  the repo write-up). Where a self-timed baseline is used instead, the timing
  script and the ticket sample are committed.
- **Farthing** — measured wall-clock from ticket ingestion to executed refund on
  the demo tenant, including the human approval click. Reported as a median over
  the demo ticket set, with the approval-wait time broken out separately (it is
  human latency, not system latency).

## 3. Integrity: nightly reconciliation

The reconciliation report (`farthing.reconcile`) over the demo tenant shows
ledger-to-Stripe agreement. "100% agreement, zero orphaned transactions" means
every executed refund appears on both sides with the same amount.

## The honest limit

With no real merchant, the volume and latency numbers are **demo-tenant / Stripe
test-mode** measurements, not production traffic. Every figure in the README and
the portfolio write-up is framed that way. The safety numbers (the golden-set
gate) are fully real regardless of merchant, because they exercise the actual
decision engine on a fixed adversarial distribution. The single most valuable next
step is one design-partner merchant whose latency and integrity numbers are
production-observed.
