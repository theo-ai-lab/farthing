# Ship-gate ledger — farthing, 2026-08-02

Scored against the eight blocking principles. Every score cites evidence that a
reader can re-run. Two verdicts are reported separately and are never merged: a
codebase can be well built and still have no external validation, and saying so
is the honest result rather than a failure.

Branch: `hardening/falsifiable-gate-and-approval-tests` · 39 commits.

## What this gate caught

The published headline `$385,021.39 of out-of-policy refund demands blocked`
was not a dollar figure. The adversarial set spans **12 currencies with three
different minor-unit exponents** — JPY, KRW and VND are zero-decimal, BHD and
KWD are three-decimal — and the reporter summed every case's raw minor units
into one integer and divided by 100. The single VND case, 25,000,000 dong
(about 950 US dollars), contributed $250,000 to the printed total on its own.
**92.2% of the figure was not USD**, and no FX conversion was ever applied.

The figure had been public since 2026-07-14. It is corrected in `9251416`, and
`scripts/check_no_hardcoded_minor_scale.py` now fails the build if any reporter
reintroduces the pattern — verified by reintroducing the original defect and
watching the check go red, then restoring it.

The per-case safety invariants were never affected: `Money._same_ccy` raises on
mismatched currencies, so the engine cannot perform cross-currency arithmetic,
and every assertion compares amounts within one case. A bad reported number, a
sound engine.

## Scores

| # | Principle | Score | Evidence |
|---|---|---|---|
| P12 | Testing | **4** | `make verify` → 137 stdlib tests, zero installs. `uv run pytest` → 155 passed + 749 subtests. 152-case two-sided adversarial golden set; crash matrix killing at every await boundary. The new scale check is mutation-verified green→red→green. Limit: the crash matrix injects faults in-process against in-memory fakes, not real process death — recorded in SECURITY.md. |
| P13 | CI/CD | **3** | Three jobs; actions pinned to full 40-char SHAs (stronger than the checklist's "latest majors"); Python 3.13/3.14 matrix; the new check wired into both `Makefile` and `ci.yml`. **The evidence is the PR run**: CI triggers on `push: [main]` and `pull_request`, so this exact ship set has not yet been exercised by CI. Landing via PR — not a direct push to main — is what makes this score real. |
| P14 | Observability | **3** | OpenTelemetry → Langfuse; hash-chained audit ledger carrying ticket, approver and trace id; structured decision logging. Limit: a non-clean reconciliation writes a log line and returns normally — no persisted incident state, no pager path. |
| P15 | Security fundamentals | **3** | `assert_credential_split` refuses to start a worker that can see the execute key, so the LLM-driven agent worker structurally cannot move money. HMAC approval token binds amount, currency, charge, action and expiry. Deterministic idempotency keys. Reply guard post-checks the one model output a customer reads. Tripwire CLEAN across 70 tracked files. Six properties the code does **not** establish are recorded in SECURITY.md "Known limits" rather than implied. |
| P19 | Infrastructure | **3** | docker-compose (Temporal + Postgres), runbook, seeded Stripe test mode, live loop exercised. Limit: `.env.example` expects a `reconcile_ro` role that compose does not provision. |
| P24 | Measurable success criteria | **4** | Every published number regenerates from `make verify` in about a second, on this tree, with no network: 152 cases, 115 attacks, per-currency demanded/blocked/executed, 37/37 correct-grant, reply-guard 0/16 and 0/6 false positives. This is the principle the currency defect violated; it is now the strongest one, because the reporting is checked by CI rather than asserted. |
| P32 | Graders | **4** | The golden set is a deterministic code-based grader over a fixed, inspectable input set, and it is **two-sided** — an engine that refused everyone fails on the first benign control. Reply-guard false-positive rate is measured on both the benign and the REFUNDED path. |
| P36 | Onboarding / accurate mental models | **3** | Scored by adversarial review from an independent frontier model built by a different vendor, run against a read-only checkout, because no model may score its own taste. It returned 15 findings, 4 of them critical. Two criticals were independently confirmed against source and fixed: the currency headline, and a top fold claiming cancellations and plan changes "resolved end-to-end against Stripe" when `_decide_cancellation`/`_decide_plan_change` always return amount 0 and `workflows.py:129` returns before preparation. One was overstated by the reviewer — the credential split is genuine and runtime-enforced. The remainder are recorded as Known limits. |

All eight ≥3. **P13's 3 is conditional on the PR run going green**; if it does not,
this ledger is wrong and the ship stops.

## Verdict 1 — Build Quality

**Strong, with its limits written down.** The money core is pure standard
library, `Decimal`-only, float-free by CI gate, and proven by a two-sided
adversarial set that a refuse-everything engine cannot pass. The central
structural claim — that no dollar figure can originate from a prompt — is
enforced by the type system rather than by a test: `ExtractedRequest` has no
amount field.

What keeps this from scoring higher is now documented rather than hidden: Stripe
refund status is not validated, the ledger row commits before provider
confirmation, and there is no out-of-band anchor, so `verify_chain` proves
internal consistency and nothing more. These are real gaps in a system that
moves money, and they are stated in SECURITY.md in the same voice as the
strengths.

## Verdict 2 — External Adoption / Production Validation

**Unproven. No external users.**

The live loop has been exercised end-to-end against Stripe test mode by the
author — four refunds, documented in `docs/LIVE_RUN_2026-07-14.md`. That
document records counts, ids and results but ships no raw transcript, Stripe
export, Temporal trace or ledger dump, so it is an assertion inside a document
rather than traceable evidence.

No real merchant has run their refund history through this. No third party has
deployed it. Nothing has moved real money. Every number in this repository is a
measurement over a fixed input set the author wrote — reproducible by anyone,
which is the point, but not evidence that the design survives contact with a
real billing operation.

The honest next step is the cheapest high-signal one: run one real merchant's
historical refund decisions through the policy engine offline and measure the
disagreement rate against what they actually did.
