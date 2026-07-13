# Architecture

Farthing is a durable, human-gated workflow that turns a billing-support ticket
into a policy-bounded, exactly-once action against Stripe. This document explains
the trust boundaries and the reliability primitives that enforce the one
invariant: **no dollar figure ever originates from a prompt, and no side effect
ever executes twice or without approval.**

## Two planes, never mixed

| | Trusted control plane | Untrusted / privileged execution |
|---|---|---|
| Runs | workflow, `extract`, `fetch_facts`, `draft_reply` | `execute_refund` |
| Worker | agent worker (`farthing-agent` queue) | execute worker (`farthing-execute` queue) |
| Stripe key | PREPARE (read + refund-prepare) | EXECUTE (refund-execute) |
| Signing secret | — | approval signing secret |
| Can move money | **no** | yes, and only after a signed approval |

The agent worker has no code path and no credential that can create a refund. The
execute worker has no LLM surface. `sessions`/deploys of one cannot escalate into
the other because the capability is the credential, and the credentials live in
different processes.

## The pipeline

1. **Trigger.** A ticket (`TicketInput`) starts a `BillingResolutionWorkflow`
   keyed `farthing-ticket-<id>` — one workflow per ticket, so idempotency and
   audit are per-ticket.
2. **Extract (LLM).** `extract` returns an `ExtractedRequest` with an action type
   and references — and, by type, **no amount**. Injection in the message can at
   most mislabel the action; a human sees the full preview before anything moves.
3. **Fetch facts (Stripe RO).** `fetch_facts` builds `AccountFacts` from the
   billing system: amount paid, prior refunds, purchase time, usage, plan terms,
   status. These are the only numbers a decision may use.
4. **Decide (pure engine).** `decide(req, facts)` is a deterministic pure function
   run *inside* the workflow (no I/O, no clock, no randomness). It computes
   eligibility and the exact, capped amount in `Decimal`. Same inputs → same
   decision, forever, and the `POLICY_VERSION` is stamped on the result.
5. **Branch.** Non-`ELIGIBLE` dispositions (`DENY`, `ESCALATE`) draft a reply and
   finish — they never reach an approval gate or a refund.
6. **Prepare + park.** For an eligible action, the workflow assembles a
   `PreparedAction` (the full preview a human will see), derives a deterministic
   idempotency key from the workflow id + step + payload, and **parks on a durable
   approval gate** (`wait_condition` on an approval/deny signal). This wait
   survives worker restarts and deploys for days.
7. **Approve.** The approval service — the only holder of the signing secret and
   the EXECUTE key — loads the prepared action from workflow state (server-side
   truth, never the client), issues an HMAC `ApprovalToken` **bound to the exact
   amount**, and signals the workflow.
8. **Execute (exactly-once).** The workflow schedules `execute_refund` **on the
   execute task queue**. That activity re-verifies the token against the decision
   and the prepared action (`authorize_execution`), then calls Stripe with the
   deterministic idempotency key. Stripe collapses any retry of the same key into
   the original refund; a refused authorization is a **non-retryable** error, so a
   blocked execution can never be retried into success.
9. **Record.** Every executed refund appends a hash-chained `LedgerEntry` tying
   the amount to the ticket, the approver, the OTel trace id, and the policy
   version.
10. **Reconcile.** A nightly Temporal Schedule runs `reconcile`: every ledger
    refund must exist in Stripe with the same amount, and every Stripe refund must
    have a ledger entry. A mismatch is an incident.

## Why these primitives

- **Typed extraction + policy engine** — separates "what did the customer ask"
  (LLM, fallible) from "what are we permitted to do and for how much" (code,
  deterministic). This is the "LLM proposes, code disposes" split, made structural.
- **Temporal durable execution** — the durable approval wait, idempotent retries,
  the nightly reconciliation Schedule, and the two-task-queue credential routing
  are all first-class Temporal primitives, not bespoke glue. A refund cannot be
  automatically un-executed (Stripe refunds are not reversible), so the recovery
  path is not a saga unwind: it is idempotent retry (the reservation + Stripe
  idempotency key make a second attempt safe) plus the nightly reconciliation,
  which surfaces any executed-but-unsettled or off-ledger refund as an incident.
- **Deterministic idempotency key (workflow id + step + ticket)** — a retried
  activity re-uses the key rather than minting a fresh one, which is what makes
  execution exactly-once across crashes and redeliveries.
- **HMAC approval token bound to the amount** — the approved figure and the
  executed figure cannot diverge; tampering invalidates the signature.
- **Hash-chained ledger** — tamper-evidence an auditor can verify independently
  with `verify_chain`.

## Failure modes

| Failure | Handling |
|---|---|
| LLM mis-extracts action | Human sees full preview before approval; deny routes back |
| Injection tries to set an amount | Type has no amount field; engine ignores text |
| Execute activity times out / retries | Same idempotency key → Stripe dedups → exactly-once |
| Approval refused mid-execute | `non_retryable` ApplicationError; the ticket is dead-lettered (see below), not retried into success |
| Execution fails terminally (guard refusal or exhausted retries) | Recorded to the dead-letter queue (`dead_letter`) for human resolution; the workflow completes as `dead_lettered` instead of crashing. Money did not move. |
| Money moved off-ledger | Nightly reconciliation flags unmatched Stripe refund as incident |
| Ledger tampered | `verify_chain` fails from the tampered entry onward |
| Worker/deploy during approval wait | Durable workflow resumes; wait survives |
