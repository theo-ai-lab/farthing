# Defense — how to explain and defend Farthing

Karpathy's line is "you can
outsource your thinking, but you can't outsource your understanding" — this page is
about being able to defend every load-bearing decision without the repo open.

## The one-sentence pitch

Farthing is the support agent that is allowed to touch money: an LLM proposes an
action from a ticket, a deterministic policy engine decides the exact amount, a
human approves a full preview, and execution is exactly-once against Stripe with a
hash-chained audit trail. The whole design serves one invariant — **no dollar
figure ever originates from a prompt, and no side effect executes twice or without
a fresh, amount-and-charge-bound human approval.**

## Q&A — the questions a skeptical engineer asks

**"How do you stop prompt injection from setting a refund amount?"**
Structurally, not by asking nicely. The type the LLM produces (`ExtractedRequest`)
has no amount field and no eligibility field. The most a malicious message can do
is mislabel the action type — and a human sees the full preview before anything
moves. Every amount is computed in `Decimal` by a pure `decide()` function from
`AccountFacts` fetched from Stripe, never from text.

**"Why commit the audit ledger BEFORE calling Stripe? Isn't that backwards?"**
It's deliberate, and it's the safe direction. The idempotency reservation and the
hash-chained ledger row are written and committed in one Postgres transaction
*before* the Stripe call, and never rolled back on a Stripe failure. So money can
never move without a durable audit row. The cost is the opposite failure: a
permanent Stripe error can leave an audit row with no refund — which the nightly
reconciliation catches as `unmatched_in_ledger` and raises as an incident. I would
rather explain a refund that didn't happen than one that happened with no record.

**"What actually makes it exactly-once?"**
Three things compose. A deterministic idempotency key (workflow id + step + ticket)
so a retry re-uses the key instead of minting a new one; a durable reserve
(`INSERT ... ON CONFLICT DO NOTHING`) that returns true exactly once per key; and
Stripe's own idempotency key so even a retry it never learned about collapses into
the original refund. A crash between Stripe success and our commit resolves the
existing refund by metadata key rather than creating a second one.

**"Why is the reservation the LAST check in the guard?"**
So a refused authorization never consumes a key. `authorize_execution` verifies
disposition, HMAC signature, amount/currency/action agreement across decision +
prepared + token, charge binding, and freshness — and only then reserves. A bad
token fails without burning the idempotency key, so a legitimate retry can still
succeed.

**"What stops the agent from executing a refund itself?"**
Capability equals credential, and the credentials live in different processes. The
agent worker holds a PREPARE Stripe key (read + refund-prepare) and the LLM; it has
no code path and no key that can create a refund. The execute worker holds the
EXECUTE key and the signing secret and runs only `execute_refund`. Startup fails
fast if the worker environment can even see the execute key.

**"How is the approval bound so it can't be replayed or tampered?"**
The approval service issues an HMAC token signed over amount + currency + charge +
action + idempotency key + expiry, using length-prefixed framing so no field can
forge a boundary. The executed figure and the approved figure cannot diverge —
tampering invalidates the signature — and a stale or reused token is refused
non-retryably.

**"The approval inbox authorizes money movement over HTTP. How is it hardened?"**
It fails closed. Every request needs an authenticated reviewer identity from an
upstream auth proxy, proven by a shared `X-Proxy-Auth` secret (the identity header
alone is spoofable — an earlier review found exactly that bypass and it's now
closed). Approvals are CSRF-protected with a SameSite=Strict double-submit token,
and responses carry anti-clickjacking headers. The prepared action — amount and
charge — is loaded from server-side workflow state, never from the request body,
so a forged POST cannot inflate the amount. `scripts/redteam_inbox.py` demonstrates
these hold against a running instance.

**"How would you know if money moved off-ledger?"**
Nightly reconciliation compares every Stripe refund in the window against the
ledger and fails closed: a Stripe refund with no ledger entry — including one with
no Farthing key at all (a dashboard refund, a compromised key) — is reported as an
incident, never silently dropped. Two refunds sharing a key are flagged as a
collision, never deduplicated away.

## Known edges I'd raise before they do

- **Live-run reality:** the money core is proven by tests (152-case adversarial
  golden set: $385k demanded out-of-policy, $0 executed beyond policy); the edge
  layer is exercised live via the runbook, not yet against a real merchant.
- **`invoice.charge` shape:** `fetch_facts` reads the charge for a refund; on newer
  Stripe API versions the charge is exposed via the payment intent rather than
  `invoice.charge`. The seed script resolves both and warns if none is found — a
  refund with no bound charge is refused by design, so this is the first thing to
  confirm on a live run.
- **Not a saga:** a Stripe refund is not reversible, so recovery is idempotent
  retry plus reconciliation, not a compensating unwind. That is a conscious choice,
  documented in ADR-001.
