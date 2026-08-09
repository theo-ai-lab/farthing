"""The approval inbox — the human gate that moves money.

A thin, server-rendered surface a CX or Finance reviewer clicks. It lists
workflows parked at the approval gate, shows the FULL assembled action (amount,
currency, policy reasons), and offers one-click Approve / Deny. On approve it
loads the prepared action from workflow state (never the request body), issues an
HMAC-signed :class:`ApprovalToken` bound to the exact amount and charge, and
signals the parked Temporal workflow.

Because this endpoint authorizes money movement, it is hardened as the security
boundary it is:

* **Authentication (fail closed):** every route requires an authenticated
  reviewer identity supplied by an upstream auth proxy (SSO) via a trusted header.
  No identity → 401. The approver recorded in the signed token and the audit
  ledger is that authenticated identity, not a constant.
* **CSRF:** approve/deny are state-changing POSTs, protected by a SameSite=Strict
  double-submit token — a cross-site page cannot forge an approval.
* **Clickjacking / headers:** every response carries `X-Frame-Options: DENY`,
  `Content-Security-Policy: frame-ancestors 'none'`, `nosniff`, and HSTS, so the
  Approve button cannot be framed and click-hijacked.

Deploy it behind an authenticating reverse proxy on HTTPS; the app fails closed if
the reverse proxy does not assert an identity.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import timedelta
from html import escape
from typing import Any

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from temporalio.client import Client

from ..config import approval_settings
from ..domain import ActionType, PreparedAction
from ..execution_guard import issue_approval_token
from ..idempotency import derive_idempotency_key
from ..money import format_money
from ..workflows import Approval

# docs_url/redoc_url/openapi_url are disabled: this is the money-moving surface,
# whose contract is that *every* route requires an authenticated reviewer
# identity (see the module docstring and `require_reviewer`). FastAPI's default
# `/openapi.json`, `/docs` and `/redoc` carry no such dependency, so leaving them
# on would hand a direct-reaching, unauthenticated caller the full map of the
# approve/deny surface — route templates, form fields and docstrings —
# contradicting that contract. Left enabled, all three answer an unauthenticated
# request against a running instance; disabled, they 404.
app = FastAPI(title="Farthing Approval Inbox", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def _security_headers(  # pyright: ignore[reportUnusedFunction]  # registered by the decorator
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'; style-src 'unsafe-inline'"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


def require_reviewer(request: Request) -> str:
    """Authenticated reviewer identity from the upstream auth proxy. Fails CLOSED.

    The identity header is only trusted after the request proves it came THROUGH
    the proxy by presenting the shared X-Proxy-Auth secret. Without this check a
    client that reaches the app directly could forge the identity header and
    approve refunds — the header alone is spoofable. The proxy MUST also strip any
    inbound X-Reviewer-Email so a caller cannot smuggle one; this secret is the
    defense-in-depth that holds even if that stripping is misconfigured."""
    s = approval_settings()
    presented = request.headers.get("x-proxy-auth", "")
    expected = s.proxy_shared_secret.get_secret_value()
    if not (presented and hmac.compare_digest(presented, expected)):
        raise HTTPException(status_code=401, detail="request did not come via the auth proxy")
    email = request.headers.get(s.reviewer_email_header)
    if not email or "@" not in email:
        raise HTTPException(status_code=401, detail="no authenticated reviewer identity")
    return email


def _csrf_for(session_id: str) -> str:
    s = approval_settings()
    return hmac.new(
        s.approval_signing_secret.get_secret_value().encode(),
        f"csrf\x1f{session_id}".encode(), hashlib.sha256,
    ).hexdigest()


def _verify_csrf(session_cookie: str | None, form_token: str | None) -> None:
    if not session_cookie or not form_token:
        raise HTTPException(status_code=403, detail="missing CSRF token")
    if not hmac.compare_digest(_csrf_for(session_cookie), form_token):
        raise HTTPException(status_code=403, detail="bad CSRF token")


_temporal_client: Client | None = None


async def _client() -> Client:
    """Reuse a single Temporal client across requests. Connecting per request (the
    inbox does so up to twice per approve) churns gRPC channels against the
    frontend."""
    global _temporal_client
    if _temporal_client is None:
        s = approval_settings()
        _temporal_client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    return _temporal_client


def _render(prepared: PreparedAction, csrf: str) -> str:
    amt = format_money(prepared.amount_minor, prepared.currency)  # currency-correct
    reasons = ", ".join(escape(r) for r in prepared.reason_codes)
    tid = escape(prepared.ticket_id)
    ct = escape(csrf)
    return f"""
    <div class="card">
      <h3>Ticket {tid} — {escape(prepared.action_type.value)}</h3>
      <table>
        <tr><td>Subscription</td><td>{escape(prepared.subscription_id)}</td></tr>
        <tr><td>Invoice</td><td>{escape(prepared.invoice_id or '—')}</td></tr>
        <tr><td><b>Amount to refund</b></td><td><b>{escape(amt)}</b></td></tr>
        <tr><td>Policy</td><td>{escape(prepared.policy_version)} ({reasons})</td></tr>
      </table>
      <form method="post" action="/inbox/{tid}/approve" style="display:inline">
        <input type="hidden" name="csrf" value="{ct}">
        <button class="ok">Approve — move {escape(amt)}</button>
      </form>
      <form method="post" action="/inbox/{tid}/deny" style="display:inline">
        <input type="hidden" name="csrf" value="{ct}">
        <button class="no">Deny</button>
      </form>
    </div>
    """


@app.get("/inbox", response_class=HTMLResponse)
async def inbox(request: Request, reviewer: str = Depends(require_reviewer),
                fh_session: str | None = Cookie(default=None)) -> HTMLResponse:
    session_id = fh_session or secrets.token_urlsafe(24)
    csrf = _csrf_for(session_id)
    style = (
        "body{font:15px system-ui;max-width:720px;margin:2rem auto}"
        ".card{border:1px solid #ddd;border-radius:12px;padding:1rem 1.25rem;margin:1rem 0}"
        "table{width:100%;border-collapse:collapse;margin:.5rem 0}td{padding:.25rem .5rem}"
        "button{padding:.5rem 1rem;border-radius:8px;border:0;cursor:pointer;margin-right:.5rem}"
        ".ok{background:#0a7d32;color:#fff}.no{background:#eee}"
    )
    pending = await _load_pending()
    items = "".join(_render(p, csrf) for p in pending)
    body = items or "<p>No actions awaiting approval.</p>"
    html = (f"<!doctype html><style>{style}</style>"
            f"<h1>Farthing — Approval Inbox</h1><p>Signed in as {escape(reviewer)}</p>{body}")
    resp = HTMLResponse(html)
    # SameSite=Strict + httpOnly: the cookie is not sent on cross-site requests,
    # so a forged POST from another origin carries neither cookie nor matching token.
    resp.set_cookie("fh_session", session_id, httponly=True, secure=True,
                    samesite="strict", max_age=3600)
    return resp


@app.post("/inbox/{ticket_id}/approve")
async def approve(ticket_id: str, csrf: str = Form(...),
                  reviewer: str = Depends(require_reviewer),
                  fh_session: str | None = Cookie(default=None)) -> RedirectResponse:
    """Issue a signed, amount-and-charge-bound token and signal the parked workflow.
    The prepared action is loaded from workflow state (server-side truth), and the
    approver is the authenticated reviewer — both go into the signed token and the
    audit ledger."""
    _verify_csrf(fh_session, csrf)
    s = approval_settings()
    prepared = await _load_prepared(ticket_id)  # server-side source of truth
    idem = derive_idempotency_key(
        _workflow_id(ticket_id), "execute_refund", {"ticket": ticket_id},
    )
    token = issue_approval_token(
        s.approval_signing_secret.get_secret_value().encode(),
        prepared=prepared, idempotency_key=idem,
        approver=reviewer, issued_ts=int(time.time()),
    )
    client = await _client()
    handle = client.get_workflow_handle(_workflow_id(ticket_id))
    # Serialize the FULL token (all fields incl. action_type + charge_id).
    await handle.signal("approve", Approval(token=asdict(token)))
    return RedirectResponse("/inbox", status_code=303)


@app.post("/inbox/{ticket_id}/deny")
async def deny(ticket_id: str, csrf: str = Form(...),
               reviewer: str = Depends(require_reviewer),
               fh_session: str | None = Cookie(default=None)) -> RedirectResponse:
    _verify_csrf(fh_session, csrf)
    client = await _client()
    handle = client.get_workflow_handle(_workflow_id(ticket_id))
    await handle.signal("deny")
    return RedirectResponse("/inbox", status_code=303)


# --- Server-side workflow state: the only source of prepared actions ---

def _workflow_id(ticket_id: str) -> str:
    return f"farthing-ticket-{ticket_id}"


def _prepared_from_query(d: dict[str, Any]) -> PreparedAction:
    """Build a PreparedAction from a workflow's server-side ``prepared_action``
    query payload (never from a request body)."""
    return PreparedAction(
        ticket_id=d["ticket_id"], action_type=ActionType(d["action_type"]),
        subscription_id=d["subscription_id"], invoice_id=d.get("invoice_id"),
        amount_minor=d["amount_minor"], currency=d["currency"],
        policy_version=d["policy_version"], charge_id=d.get("charge_id"),
        reason_codes=tuple(d.get("reason_codes", ())),
    )


async def _load_pending() -> list[PreparedAction]:
    """List the actions parked at the approval gate by querying Temporal visibility
    for workflows in the awaiting-approval state, then reading each one's
    server-side prepared action. Requires the ``farthing_state`` custom search
    attribute (see docs/RUNBOOK.md). Degrades to an empty list if a workflow
    finished between the visibility scan and the query."""
    client = await _client()
    out: list[PreparedAction] = []
    # ExecutionStatus filter matters: a query to a non-running workflow whose
    # task queue no longer has a worker does not fail — it parks waiting for one,
    # which would hang this page forever. The rpc_timeout is the backstop for the
    # same hang on a still-running workflow whose worker is down.
    query = "farthing_state = 'awaiting_approval' AND ExecutionStatus = 'Running'"
    async for wf in client.list_workflows(query):
        try:
            handle = client.get_workflow_handle(wf.id)
            d = await handle.query("prepared_action", rpc_timeout=timedelta(seconds=5))
        except Exception:  # finished mid-scan or worker unreachable; skip it
            continue
        if d:
            out.append(_prepared_from_query(d))
    return out


async def _load_prepared(ticket_id: str) -> PreparedAction:
    """Load the prepared action from WORKFLOW state (server-side truth) via a
    query — never from the request body. A refund with no bound charge is rejected
    rather than executed against an unsigned target."""
    client = await _client()
    handle = client.get_workflow_handle(_workflow_id(ticket_id))
    try:
        d = await handle.query("prepared_action", rpc_timeout=timedelta(seconds=5))
    except HTTPException:
        raise
    except Exception as e:  # not queryable (finished, missing, or worker unreachable)
        raise HTTPException(status_code=409, detail="ticket is not awaiting approval") from e
    if not d:
        raise HTTPException(status_code=409, detail="ticket is not awaiting approval")
    if d.get("action_type") == ActionType.REFUND.value and not d.get("charge_id"):
        raise HTTPException(status_code=400, detail="refund has no bound charge")
    return _prepared_from_query(d)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8080)
