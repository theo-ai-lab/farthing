# Security

Farthing moves money, so its security model is explicit. This file records the
trust boundaries and the deployment requirements that the code depends on.
Findings from security review that require deployment-side action (not just
code) live here.

## Trust boundaries

| Boundary | Control |
|---|---|
| Customer message → LLM | The extraction output type has no amount/eligibility field; prompt injection cannot move money. |
| Policy decision → money | Amounts are computed in `Decimal` from Stripe facts only; the HMAC-signed approval token binds amount, currency, charge, action, and expiry. |
| Agent worker → Stripe | Holds only the `refund-PREPARE` key; cannot create a refund. `refund-EXECUTE` lives only in the execute worker's environment, on a separate task queue. |
| Reviewer → approval | Every route requires the proxy shared secret **and** an identity the proxy asserts (see below). Authentication is delegated to the SSO proxy; the app performs **no per-reviewer authorization** — any identity the proxy vouches for may approve. A reviewer allowlist or role check is a deployment concern today, not a code control. |
| Ledger integrity | Hash-chained and verifiable in-process with `verify_chain`, which proves the chain is **internally consistent**. It cannot detect a whole-ledger re-seal by an attacker with write access; only an out-of-band anchor can, and **no anchor writer is implemented** — see Known limits. |

## Approval inbox — the money gate (deployment requirements)

The inbox is the human control that authorizes refunds. It authenticates via an
upstream auth proxy (SSO). Two requirements are **load-bearing** — the code fails
closed without them, but they must be configured correctly:

1. **The proxy MUST send `X-Proxy-Auth: <FARTHING_APPROVAL_PROXY_SHARED_SECRET>`.**
   The app rejects any request that does not present this shared secret, so a
   client that reaches the app directly and forges the identity header still
   cannot authenticate. Rotate this secret on exposure.
2. **The proxy MUST strip any inbound `X-Reviewer-Email`** so a caller cannot
   smuggle an identity. The shared secret is the defense-in-depth if this is
   misconfigured, but strip it anyway.
3. **Bind the app to loopback / the proxy network only.** It is not meant to be
   internet-reachable directly.

CSRF is handled by a SameSite=Strict double-submit token; responses carry
`X-Frame-Options: DENY` + CSP `frame-ancestors 'none'` (anti-clickjacking), nosniff,
and HSTS. Serve over HTTPS.

## Supply chain

- **Commit `uv.lock`.** Run `uv lock` and check it in; CI installs with
  `uv sync --locked`. The money-path adapters also carry upper version caps in
  `pyproject.toml` as a backstop, but the lockfile is the primary guarantee.
- **CI actions are pinned to commit SHAs.** `actions/checkout`,
  `actions/setup-python`, and `astral-sh/setup-uv` are pinned to full commit
  SHAs (not mutable tags), so a compromised tag cannot run code in CI. The
  invariant-core CI job also carries no secrets, which limits blast radius.

## Data handling

- No secrets are stored in code or git history; credentials are `SecretStr` from
  the environment, and `.env` is gitignored.
- Customer message text (`raw_text`) flows through the Temporal workflow and is
  retained in its event history; it can contain PII. Set a Temporal retention
  policy appropriate to your data-handling obligations, and redact where required.
- The audit ledger stores ticket id, approver, amounts, and trace id — not the
  customer message.

## Known limits

These are properties the code does **not** establish today. They are listed
because a security file that only lists strengths is marketing.

- **No provider-status validation.** `stripe_client.create_refund` returns
  Stripe's status without checking it, and `settlement` accepts any refund
  object. Stripe can return `pending`, `requires_action`, `failed` or
  `canceled`; today only `succeeded` is assumed. A non-succeeded refund is
  recorded and replied to as a completed one.
- **The ledger append precedes provider confirmation.** `append_executed_refund`
  writes `disposition="refunded"` and commits before the Stripe call returns, so
  a permanent provider failure leaves a `refunded` audit row for money that
  never moved. The README architecture diagram shows the opposite ordering; the
  diagram describes the intent, the code does not yet match it.
- **No out-of-band ledger anchor.** `verify_chain` proves internal consistency
  only. An attacker with write access to the ledger can rewrite a row and
  re-derive every hash; the result verifies clean. Detecting that requires
  publishing chain heads somewhere the attacker does not control, which is not
  implemented.
- **No per-reviewer authorization.** See the trust-boundary table above.
- **Reconciliation raises no incident.** A non-clean reconciliation writes a log
  line and returns normally. There is no persisted incident state and no pager
  path.
- **The crash matrix injects faults in-process.** It kills at every await
  boundary against in-memory Postgres and Stripe fakes. It does not test process
  death, real Postgres isolation, concurrent workers, or a lost provider
  response.

None of these affect the policy engine's amount decisions, which are pure,
`Decimal`-only, and covered by the adversarial golden set.

## Reporting

Found a vulnerability? Open a GitHub issue (the project runs only against Stripe
test mode, so public disclosure carries no customer risk). For a system actually
moving customer money, engage a professional penetration test before production;
this file is a first pass, not a substitute.
