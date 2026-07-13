# ADR-001: Money-safety architecture

## Status
Accepted

## Date
2026-07-12

## Context
Farthing is a support agent that executes real money movement (Stripe refunds) in
response to free-text customer messages interpreted by an LLM. The failure modes
are financial and irreversible: an over-refund, a double refund, a refund of the
wrong charge, or money that moves with no audit trail. The design must make those
outcomes structurally impossible — not merely unlikely — even against a fully
prompt-injected model, a crashing worker, or a compromised component. Four
decisions carry that guarantee.

## Decision

### 1. "LLM proposes, code disposes": typed extraction with no amount field
The LLM's only jobs are to turn a message into a typed `ExtractedRequest` and to
draft a reply *after* the outcome is decided. `ExtractedRequest` has **no amount
field and no eligibility field**. A deterministic, versioned policy engine
computes every dollar figure from trusted `AccountFacts` (sourced from Stripe) in
`Decimal` arithmetic. The engine never reads the request text to decide money.

### 2. PREPARE/EXECUTE credential split across two workers
Stripe access uses two restricted keys. The `refund-PREPARE` key (read + prepare)
is the only credential the agent worker holds; the `refund-EXECUTE` key lives only
in the approval service, and the money-moving activity runs only on a separate
`farthing-execute` task queue. Capability is the credential, so the agent worker
has no code path to a refund.

### 3. Reserve-before-Stripe exactly-once ordering
A deterministic idempotency key is derived inside the durable workflow. The
execution guard reserves that key in a durable, transactional store
(`INSERT ... ON CONFLICT DO NOTHING`) as its last check, and the reservation is
committed **before** Stripe is called and **never rolled back on a Stripe
failure**. A retry finds the key reserved and resolves the existing refund by
metadata key rather than creating a second one.

### 4. Audit-before-money ledger ordering
The hash-chained audit ledger entry is written in the **same transaction** as the
idempotency reservation, committed before Stripe. Money therefore can never move
without a durable audit row.

## Alternatives Considered

### Let the model return the refund amount (rejected)
Simplest to build, but it makes prompt injection a money-movement vector and
defeats the entire safety story. Rejected: the type system must make a
model-chosen amount impossible, not merely discouraged.

### One Stripe key for the whole app (rejected)
Less configuration. Rejected: a single key means the agent worker — the component
most exposed to untrusted input — can move money directly, so a prompt-injection
or code-execution foothold there is a direct financial compromise.

### Rely solely on Stripe's idempotency key for exactly-once (rejected)
Stripe's idempotency window is ~24 hours. A workflow paused for days, a long
worker outage, or an operator re-running a ticket later would fall outside it and
double-refund. Rejected: exactly-once must survive arbitrary delay, so a durable
reservation owns it and Stripe's key is the concurrency backstop.

### Write the ledger after Stripe confirms (rejected)
Intuitive ("record what actually happened"). Rejected: a crash between the Stripe
success and the ledger write leaves money moved with no audit row — the
unacceptable direction. Audit-before-money inverts the failure into an audit row
with no refund, which the nightly reconciliation flags as `unmatched_in_ledger`.

## Consequences
- The money-critical core (`money`, `domain`, `policy`, `ledger`, `pg_ledger`,
  `idempotency`, `idempotency_store`, `execution_guard`, `reconcile`) is pure
  standard library and testable with no Temporal, Stripe, LLM, or network — which
  is why the invariant can be proven offline (property fuzz + adversarial golden
  set).
- The approval token must bind amount, currency, action, charge, and expiry via
  HMAC, so the approved and executed actions cannot diverge without the signing
  secret.
- Audit-before-money means a permanent Stripe failure produces a ledger row with
  no refund. This is deliberate and safe; the nightly reconciliation is the
  detective control that surfaces it as an incident.
- The two-worker split adds deployment complexity (two processes, two credential
  sources) — accepted as the cost of a real least-privilege boundary.

See `docs/ARCHITECTURE.md` for the data-flow detail and `README.md` for the
one-sentence invariant this ADR exists to preserve.
