# AGENTS.md — agent-first runbook for Farthing

You are an agent operating this repo. Farthing is a billing-refund automation
agent: it turns a support ticket into a policy-bounded, human-approved,
exactly-once refund against Stripe, recorded in a hash-chained audit ledger.

**The one invariant you must never break:** no dollar figure ever originates from
a prompt, and no side effect executes twice or without a fresh, amount-AND-charge-
bound human approval. If a change would let a model's text set an amount, or let a
refund run without a signed approval, stop.

## Layout

- `src/farthing/` — money-critical **core** (`money`, `domain`, `policy`, `ledger`,
  `idempotency`, `execution_guard`) is pure stdlib, dependency-free, strict-typed.
  Edge adapters (`activities`, `workflows`, `worker`, `stripe_client`, `approval/`,
  `pg_ledger`, `dead_letter`, `idempotency_store`, `reconcile`, `observability`)
  wrap Temporal / Stripe / FastAPI / Postgres.
- `tests/` — 82 tests. The zero-dep core (incl. 4000-ticket fuzz + 152-case
  adversarial golden set) runs with **no install**; edge tests skip offline.
- `docs/` — `ARCHITECTURE.md`, `RUNBOOK.md`, `MEASUREMENT.md`, `DEFENSE.md`,
  `decisions/ADR-001-money-safety-architecture.md`.
- `scripts/` — `check_no_float_money.py`, `seed_stripe_testmode.py`,
  `register_search_attribute.sh`, `dev_inbox_proxy.py`, `redteam_inbox.py`.

## Verify (do this before and after any change)

```bash
# Zero-dependency invariant gate (no install needed):
PYTHONPATH=src:tests python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/check_no_float_money.py

# Full gate (needs `uv sync --all-extras`):
uv run ruff check . && uv run pyright && uv run pytest -q
```

Both must be green. `pyright` is strict on the whole package; keep it at 0 errors.

## Run it live (needs Docker + a Stripe TEST account + an Anthropic key)

```bash
# 1. Install deps + infra
uv sync --all-extras
docker compose up -d                                   # Temporal + Postgres + UI (:8233)
./scripts/register_search_attribute.sh                 # one-time: farthing_state search attr

# 2. Stripe test-mode restricted keys (two DIFFERENT keys):
#    PREPARE = Read on subscriptions/invoices/refunds (+charges/paymentintents);
#              Refunds=Read (NOT None — it lists prior refunds), never Write
#    EXECUTE = Refunds=Write, everything else None
#    Seed the objects a ticket needs and copy the printed ids:
FARTHING_STRIPE_SEED_KEY=sk_test_xxx uv run python scripts/seed_stripe_testmode.py

# 3. Config
cp .env.example .env      # fill PREPARE+Anthropic (worker), EXECUTE+signing+proxy secret (approval)
#    Do NOT put the EXECUTE key in the worker section — startup fails on purpose.
#    Paste the seeded subscription_id/invoice_id/charge_id into examples/run_ticket.py.

# 4. Processes (four terminals)
uv run farthing-worker --role agent                    # PREPARE key + LLM
uv run farthing-worker --role execute                  # EXECUTE key + signing secret
uv run farthing-inbox                                  # money gate on :8080 (fails closed)
FARTHING_APPROVAL_PROXY_SHARED_SECRET=<same as .env> uv run python scripts/dev_inbox_proxy.py

# 5. Drive one ticket, then approve it in the browser
uv run python examples/run_ticket.py                   # starts a workflow
#    open http://127.0.0.1:8090/inbox  (the dev proxy — the raw :8080 rejects browsers)
#    review the previewed amount, click Approve -> refund executes once in Stripe test mode.
```

Then run a hostile ticket (demand more than the plan allows) and confirm it is
denied/escalated with $0 moved.

## Break it (adversarial pass against the running gate)

```bash
FARTHING_APPROVAL_PROXY_SHARED_SECRET=<same as .env> uv run python scripts/redteam_inbox.py
```

Every attack (unauthenticated read, identity-header spoof, wrong proxy secret,
forged CSRF) must be blocked; the control read must return 200. Non-zero exit =
the gate did not hold.

## Rules for edits

- New behavior gets a test first; watch it fail, then pass.
- Never introduce a float on the money path (`check_no_float_money.py` gates it).
- Commit messages are domain-only: no tooling/vendor names, no authorship or
  co-author provenance trailers (a local hook rejects them). Scan staged files first.
- Do not push to a remote or deploy without explicit human authorization.
