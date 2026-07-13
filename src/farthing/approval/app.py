"""The approval inbox.

A thin, server-rendered surface a CX or Finance reviewer actually clicks. It
lists workflows parked at the approval gate, shows the FULL assembled action —
amount, currency, policy reasons — and offers one-click Approve / Deny. On
approve it (1) derives nothing and trusts nothing from the client, (2) issues an
HMAC-signed :class:`ApprovalToken` bound to the exact previewed amount, and (3)
signals the parked Temporal workflow. The token is what the execute activity
verifies before Stripe is ever called.

Deliberately no heavy SPA: the full-preview diff is the product, and a solo
builder should not sink the differentiator's time into a React frontend.
"""

from __future__ import annotations

from html import escape

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from temporalio.client import Client

from ..config import approval_settings
from ..domain import ActionType, PreparedAction
from ..execution_guard import issue_approval_token
from ..idempotency import derive_idempotency_key

app = FastAPI(title="Farthing Approval Inbox")


async def _client() -> Client:
    s = approval_settings()
    return await Client.connect(s.temporal_address, namespace=s.temporal_namespace)


def _pending_query() -> list[dict]:
    """Pending items come from Temporal visibility (workflows with
    ``farthing_state = awaiting_approval``). Shown here as the shape the template
    needs; the live query uses ``client.list_workflows``."""
    return []  # populated by list_workflows in production; kept thin for the demo


def _render(prepared: PreparedAction) -> str:
    amt = f"{prepared.amount_minor / 100:.2f} {prepared.currency}"
    reasons = ", ".join(escape(r) for r in prepared.reason_codes)
    return f"""
    <div class="card">
      <h3>Ticket {escape(prepared.ticket_id)} — {escape(prepared.action_type.value)}</h3>
      <table>
        <tr><td>Subscription</td><td>{escape(prepared.subscription_id)}</td></tr>
        <tr><td>Invoice</td><td>{escape(prepared.invoice_id or '—')}</td></tr>
        <tr><td><b>Amount to refund</b></td><td><b>{escape(amt)}</b></td></tr>
        <tr><td>Policy</td><td>{escape(prepared.policy_version)} ({reasons})</td></tr>
      </table>
      <form method="post" action="/inbox/{escape(prepared.ticket_id)}/approve" style="display:inline">
        <button class="ok">Approve — move {escape(amt)}</button>
      </form>
      <form method="post" action="/inbox/{escape(prepared.ticket_id)}/deny" style="display:inline">
        <button class="no">Deny</button>
      </form>
    </div>
    """


@app.get("/inbox", response_class=HTMLResponse)
async def inbox() -> str:
    style = (
        "body{font:15px system-ui;max-width:720px;margin:2rem auto}"
        ".card{border:1px solid #ddd;border-radius:12px;padding:1rem 1.25rem;margin:1rem 0}"
        "table{width:100%;border-collapse:collapse;margin:.5rem 0}td{padding:.25rem .5rem}"
        "button{padding:.5rem 1rem;border-radius:8px;border:0;cursor:pointer;margin-right:.5rem}"
        ".ok{background:#0a7d32;color:#fff}.no{background:#eee}"
    )
    items = "".join(_render(p) for p in [])  # bound to live pending set in production
    body = items or "<p>No actions awaiting approval.</p>"
    return f"<!doctype html><style>{style}</style><h1>Farthing — Approval Inbox</h1>{body}"


@app.post("/inbox/{ticket_id}/approve")
async def approve(ticket_id: str) -> RedirectResponse:
    """Issue a signed, amount-bound token and signal the parked workflow.

    In production, the prepared action is loaded from workflow state (a query),
    not from the request body, so a client cannot inflate the amount. The token is
    signed over that server-side amount."""
    from dataclasses import asdict

    s = approval_settings()
    prepared = await _load_prepared(ticket_id)  # server-side source of truth
    # Key on ticket identity only (matches the workflow's derivation) so both
    # sides produce the same key regardless of amount.
    idem = derive_idempotency_key(
        _workflow_id(ticket_id), "execute_refund", {"ticket": ticket_id},
    )
    token = issue_approval_token(
        s.approval_signing_secret.get_secret_value().encode(),
        prepared=prepared, idempotency_key=idem,
        approver=_current_reviewer(), issued_ts=_now_ts(),
    )
    client = await _client()
    handle = client.get_workflow_handle(_workflow_id(ticket_id))
    from ..workflows import Approval

    # Serialize the FULL token (all fields, including action_type and charge_id) —
    # a hand-picked subset drops fields ApprovalToken(**d) needs and would crash
    # the executor before authorization even runs.
    await handle.signal("approve", Approval(token=asdict(token)))
    return RedirectResponse("/inbox", status_code=303)


@app.post("/inbox/{ticket_id}/deny")
async def deny(ticket_id: str) -> RedirectResponse:
    client = await _client()
    handle = client.get_workflow_handle(_workflow_id(ticket_id))
    await handle.signal("deny")
    return RedirectResponse("/inbox", status_code=303)


# --- Seams the demo wiring fills in (kept explicit so the trust boundary is clear) ---

def _workflow_id(ticket_id: str) -> str:
    return f"farthing-ticket-{ticket_id}"


async def _load_prepared(ticket_id: str) -> PreparedAction:
    """Load the prepared action from workflow state (server-side truth). Stubbed
    to a zero-amount action so a misconfigured deploy fails safe rather than
    refunding an attacker-supplied amount."""
    return PreparedAction(
        ticket_id=ticket_id, action_type=ActionType.REFUND, subscription_id="",
        invoice_id=None, amount_minor=0, currency="USD", policy_version="unknown",
        charge_id=None,
    )


def _current_reviewer() -> str:
    return "reviewer@farthing.local"  # wired to session auth in production


def _now_ts() -> int:
    import time

    return int(time.time())


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8080)
