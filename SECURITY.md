# Security

Farthing moves money, so its security model is explicit. This file records the
trust boundaries and the deployment requirements that the code depends on. It
reflects a CSO-style audit; findings that require deployment-side action (not just
code) live here.

## Trust boundaries

| Boundary | Control |
|---|---|
| Customer message → LLM | The extraction output type has no amount/eligibility field; prompt injection cannot move money. |
| Policy decision → money | Amounts are computed in `Decimal` from Stripe facts only; the HMAC-signed approval token binds amount, currency, charge, action, and expiry. |
| Agent worker → Stripe | Holds only the `refund-PREPARE` key; cannot create a refund. `refund-EXECUTE` lives only in the approval service on a separate task queue. |
| Reviewer → approval | The approval inbox authenticates and authorizes every route (see below). |
| Ledger integrity | Hash-chained, verifiable with `verify_chain` + out-of-band head/count anchors. |

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
- **Pin CI actions to commit SHAs.** `actions/checkout`, `actions/setup-python`,
  and `astral-sh/setup-uv` are currently pinned to mutable tags; pin them to
  full commit SHAs (Dependabot/renovate can maintain them) so a compromised tag
  cannot run code in CI. The invariant-core CI job carries no secrets, which
  limits blast radius, but the publish path should be SHA-pinned before any
  release.

## Data handling

- No secrets are stored in code or git history; credentials are `SecretStr` from
  the environment, and `.env` is gitignored.
- Customer message text (`raw_text`) flows through the Temporal workflow and is
  retained in its event history; it can contain PII. Set a Temporal retention
  policy appropriate to your data-handling obligations, and redact where required.
- The audit ledger stores ticket id, approver, amounts, and trace id — not the
  customer message.

## Reporting

This is a portfolio project. For a system actually moving customer money, engage
a professional penetration test before production; this file and the automated
review are a first pass, not a substitute.
