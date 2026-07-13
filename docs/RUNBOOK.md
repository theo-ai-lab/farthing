# Runbook

## Verify the core (zero dependencies)

The money-critical core needs nothing installed:

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/check_no_float_money.py
```

This runs the policy engine, ledger, idempotency, execution-guard, reconciliation,
property-fuzz, and adversarial golden-set tests. It is the invariant proof and it
is what CI gates on first.

## Run the full system locally

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker, a Stripe **test-mode**
account, and an Anthropic API key.

```bash
# 1. Install
uv sync
prek install    # git hooks: ruff + no-float-money + invariant suite on every commit

# 2. Infra: Temporal + Postgres (+ temporal-ui)
docker compose up -d

# 2b. Register the custom search attribute the approval inbox filters on to list
#     the workflows parked at the gate. One-time per namespace.
temporal operator search-attribute create --name farthing_state --type Keyword

# 3. Config
cp .env.example .env    # fill in the two Stripe restricted keys + Anthropic key
#   IMPORTANT: the PREPARE and EXECUTE keys are different keys in different
#   sections. Do not put the execute key in the worker section.

# 4. Workers (two processes, two credentials)
uv run farthing-worker --role agent      # PREPARE key + LLM
uv run farthing-worker --role execute    # EXECUTE key + signing secret

# 5. Approval inbox
uv run farthing-inbox                    # http://127.0.0.1:8080/inbox
```

## Create Stripe restricted keys

In the Stripe Dashboard (test mode) → Developers → API keys → Restricted keys:

- **PREPARE key**: Refunds = *None*, Charges/Invoices/Subscriptions = *Read*.
- **EXECUTE key**: Refunds = *Write*, everything else *None*.

The whole point is that the agent worker's key cannot create a refund.

## Run a ticket end-to-end

Start a workflow (see `examples/run_ticket.py`), open the inbox, review the full
previewed amount, click Approve, and watch the refund execute exactly once. Try a
hostile ticket (demand more than the plan allows) and watch it get denied or
escalated with \$0 moved.

## Nightly reconciliation

The reconciliation Schedule compares Stripe refunds to the ledger. If it reports
`unmatched_in_stripe`, money moved without a ledger entry — page on-call. If it
reports `unmatched_in_ledger`, a recorded refund never executed — investigate the
workflow.

## Key rotation (demo hygiene)

Rotate the Stripe test keys and the approval signing secret periodically; the
signing-secret rotation invalidates outstanding approval tokens, which is the
desired behavior.
