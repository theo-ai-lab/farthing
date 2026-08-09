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
execute worker has no LLM surface. A compromise of one process cannot escalate
into the other because the capability is the credential, and the credentials live
in different processes.

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
7. **Approve.** The approval service — the only issuer of approval tokens,
   holding the signing secret and no LLM surface — loads the prepared action from
   workflow state (server-side truth, never the client), issues an HMAC
   `ApprovalToken` **bound to the exact amount**, and signals the workflow.
8. **Execute (exactly-once).** The workflow schedules `execute_refund` **on the
   execute task queue**. That activity is wiring: it builds the real settings,
   database connection and Stripe client and hands them to `settlement.settle_refund`,
   which owns the ordering. `settle_refund` re-verifies the token against the
   decision and the prepared action (`authorize_execution`), commits the durable
   reservation and the audit row **together, before Stripe**, and only then calls
   Stripe with the deterministic idempotency key. Stripe collapses any retry of the
   same key into the original refund, and the key is persisted into refund metadata
   so recovery still resolves it after Stripe's window closes. A refused
   authorization is a **non-retryable** error, so a blocked execution can never be
   retried into success.
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
  execution exactly-once across crashes and redeliveries. The key is scoped to the
  **ticket**, deliberately: that is what bounds a ticket to one refund forever. The
  cost is that it does not identify the *action*, so a re-driven ticket can reach
  the replay path holding a different charge or amount against the same
  reservation. `settle_refund` closes that by checking the reservation's committed
  audit row before treating it as this action's own, and refusing when they
  disagree (`replay_action_drift`) — a dead-lettered ticket a human resolves,
  rather than a silent under-refund or a second charge.
- **HMAC approval token bound to the amount** — the approved figure and the
  executed figure cannot diverge; tampering invalidates the signature.
- **Hash-chained ledger** — tamper-evidence an auditor can verify independently
  with `verify_chain`.

## Failure modes

| Failure | Handling |
|---|---|
| LLM mis-extracts action | Human sees full preview before approval; deny routes back |
| Injection tries to set an amount | Type has no amount field; engine ignores text |
| Execute activity times out / retries | Same idempotency key → durable reservation + Stripe dedup → exactly-once |
| Approval refused mid-execute | `non_retryable` ApplicationError; the ticket is dead-lettered (see below), not retried into success |
| Execution fails terminally (guard refusal or exhausted retries) | Recorded to the dead-letter queue (`dead_letter`) for human resolution; the workflow completes as `dead_lettered` instead of crashing. Money did not move. |
| Money moved off-ledger | Nightly reconciliation flags unmatched Stripe refund as incident |
| Ledger tampered | `verify_chain` fails from the tampered entry onward |
| Worker/deploy during approval wait | Durable workflow resumes; wait survives |
| Process dies at any boundary in `settle_refund` | Recovery re-runs the activity; the reservation + audit row either both survived or both rolled back, so at most one refund exists. Enumerated and asserted in `tests/test_crash_matrix.py` |
| Process dies with the refund in flight at Stripe | The key is in refund metadata, so recovery resolves the existing refund instead of creating a second one — with Stripe's own idempotency window open **or** closed |
| A reserved key is replayed for a different action | Refused (`replay_action_drift`). The key is derived per ticket, so a re-driven ticket can arrive at the replay path with a different charge or amount; the committed audit row is checked before the reservation is treated as this action's own |
| A reserved key has no audit row | Refused (`replay_without_record`). The commit ordering makes this unreachable, so reaching it means the ordering is broken and paying would be worse than failing |

### What the crash matrix does and does not prove

`tests/test_crash_matrix.py` kills the process at every durability boundary in
`settle_refund`, on the first attempt and again during recovery, with the
provider's idempotency window both open and closed, and asserts after **every**
attempt that at most one refund exists, the chain verifies, no money moved beyond
the approved amount, and — the one that matters — money never moved without a
committed audit row. The boundaries are read from `settlement.KILL_POINTS`, which
is cross-checked against the source with `ast`, so the enumeration cannot silently
fall behind the code.

It is offline and deterministic. It proves the *logic* survives death at every
boundary. It does **not** prove a deployment does: killing the container mid-refund
against real Stripe is a separate exercise and has not been run.
