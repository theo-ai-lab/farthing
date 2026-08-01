# Plan — kill-at-every-boundary crash matrix (vertical slice)

Date: 2026-08-01
Branch: `elite/falsifiable-gate-and-approval-tests`

## Goal

Turn Farthing's central claim — *exactly-once money movement, and correct
recovery when the process dies* — from a design argument in `ARCHITECTURE.md`
into a falsifiable, offline, deterministic test that kills the process at **every
boundary on the money path** and asserts the money invariant after recovery.

Today the exactly-once claim rests on:

- prose in `docs/ARCHITECTURE.md` ("Failure modes" table),
- a code comment in `activities.execute_refund` describing the ordering, and
- `test_execution_guard.py`, which proves the *guard* is exactly-once but never
  exercises the reserve → ledger → commit → Stripe **sequence** at all.

Nothing today executes that sequence. `execute_refund` is untested: it is welded
to `approval_settings()`, `psycopg.connect()` and `stripe`, so it cannot run
offline. That is the gap this slice closes.

## Architecture

### 1. A seam: `farthing/settlement.py`

Extract the money-movement sequence out of `activities.execute_refund` into a
pure-stdlib module with its adapters injected. `execute_refund` becomes the thin
wiring layer it should always have been (settings → psycopg → Stripe → call the
sequence → map errors to Temporal's `ApplicationError`).

Contract (typed, one error shape, additive):

```python
class RefundGateway(Protocol):
    def create_refund(self, *, charge_id, amount_minor, idempotency_key) -> RefundResult: ...
    def find_refund_by_key(self, *, charge_id, idempotency_key) -> RefundResult | None: ...

def settle_refund(
    secret: bytes, *, token, decision, prepared,
    conn: DBConn, store: IdempotencyStore, gateway: RefundGateway,
    now_ts: int, trace_id: str | None, api_version: str | None,
    on_step: StepObserver = _ignore,
) -> RefundResult
```

- Input: the already-signed `ApprovalToken`, the `PolicyDecision`, the
  `PreparedAction`, and three injected ports (`DBConn`, `IdempotencyStore`,
  `RefundGateway`). `DBConn` and `IdempotencyStore` already exist as protocols.
- Output: `RefundResult` (existing TypedDict).
- Errors: exactly one shape — `ExecutionRefused(code, message)`, the type the
  guard already raises. No new exception hierarchy. The activity maps it to a
  non-retryable `ApplicationError`, preserving today's behavior exactly.
- `ExecuteClient` already satisfies `RefundGateway` structurally; no change to it.

`on_step(name)` is an instrumentation seam called at each durability boundary. It
is a no-op by default. **This is also the fault-injection point**: an observer
that raises aborts the sequence at exactly that boundary, which is how we
simulate process death without a container.

### 2. Programmatic kill-point enumeration

`settlement.KILL_POINTS` declares the boundary names. The test parses
`settlement.py` with `ast` and asserts the declared tuple equals the
`on_step("...")` literals found in the source, in source order. Adding a new
boundary to the sequence without declaring it fails the test; declaring a
boundary the source never calls fails the test. The matrix is then built from
`KILL_POINTS`, so a new boundary automatically extends it. A second check asserts
every declared point was actually *reached* during the matrix run, so a
non-executable declaration cannot hide.

Provider-side death (the payment provider applied the refund but we never saw the
response) cannot be expressed as an `on_step` call inside our own process, so the
harness declares those separately in `PROVIDER_KILL_POINTS` and the matrix runs
the union.

### 3. Honest fakes (`tests/_fault_injection.py`)

- `FakeDatabase` / `FakeConn`: models the psycopg semantics the code actually
  relies on — **uncommitted writes are visible to the writing transaction and
  lost on process death**, `INSERT ... ON CONFLICT DO NOTHING` rowcount, the
  `SELECT ... FOR UPDATE` tail read. The real `PostgresIdempotencyStore` and the
  real `pg_ledger` run against it, so the matrix exercises production code, not a
  re-implementation.
- `FakeStripe`: records applied refunds separately from create *calls*, so a
  double-create is visible even when it is deduplicated. Parameterized by
  `idempotency_window_open`: when `False` the provider has **no** idempotency
  memory, which proves exactly-once is *ours* and not borrowed from Stripe's
  24-hour window.

### 4. The invariant asserted at every kill point

For every kill point, after every attempt (not just the last):

1. **At most one refund** exists at the provider for the ticket's charge.
2. **The ledger chain verifies** (`verify_chain` over everything committed).
3. **Recorded state matches the provider, or reconciliation raises an incident.**
   Concretely: `reconcile()` must report `unmatched_in_stripe == ()` (no money
   moved without an audit row — the "approved-but-invisible payment" case),
   `mismatched == ()` and `duplicate_keys == ()`. The one permitted divergence is
   `unmatched_in_ledger` — the deliberate audit-before-money direction — and it is
   only permitted when the provider genuinely holds no refund.
4. **No money moved beyond policy**: every applied refund equals
   `decision.amount_minor`, and the total moved never exceeds it.

Matrix shape: every kill point on attempt 1 × every kill point on attempt 2
(pairwise — a crash during recovery is the case a single-fault matrix misses) ×
`{provider idempotency window open, expired}`.

## TDD tasks (bite-sized, each RED first)

1. `settle_refund` happy path: reserves the key, appends one ledger entry,
   commits, calls the gateway once, returns the refund. RED (no module).
2. `settle_refund` replay path: a second call with the same key resolves the
   existing refund instead of creating a second one.
3. `settle_refund` refusal path: a tampered token raises `ExecutionRefused` and
   reserves nothing, writes nothing.
4. Rewire `activities.execute_refund` onto `settle_refund`; existing suite stays
   green; `ExecutionRefused` still maps to a non-retryable `ApplicationError`.
5. Kill-point declaration guard: AST-derived names == `KILL_POINTS`.
6. The single-fault matrix: kill at each point, recover, assert the invariant.
7. The pairwise matrix (crash during recovery) × both provider window modes.
8. Reachability: every declared kill point was exercised.
9. Permanent-provider-failure scenario: the audit-before-money row exists with no
   refund, and `reconcile()` reports it as an incident (proves the safety net the
   architecture doc claims, rather than asserting it in prose).

## Scope — what this slice is NOT

- **Out of scope, explicitly: the live docker-kill drill against real Stripe.**
  Everything here is offline and deterministic. This matrix proves the *logic*
  survives death at every boundary; it does not prove the deployment does.
- Workflow-level kill points (before/after the policy decision, before/after the
  approval signal) are Temporal's durable-history responsibility and are not
  re-tested here; the matrix covers the boundaries *we* implement, which is where
  the money is.
- No new runtime dependency. Pure stdlib, runs under `make verify`.
