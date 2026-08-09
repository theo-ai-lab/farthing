"""Offline tests for the approval inbox — the HTTP surface that authorizes money.

This is the one endpoint in the system whose POST makes a refund happen, and
until now nothing imported it: the live run on 2026-07-14 found a missing
`python-multipart` only by clicking the real button, because no test ever built
the app (docs/LIVE_RUN_2026-07-14.md, defect 3). These tests close that hole with
a FastAPI TestClient and a stubbed Temporal client — no network, no Stripe, no
Temporal server.

What is asserted here is the security contract of the money gate:

* the reviewer identity is only trusted through the proxy shared secret, so a
  spoofed identity header alone is a 401;
* state-changing POSTs need the SameSite double-submit CSRF token;
* the approved amount and charge come from WORKFLOW state, never the request
  body — an inflated body figure cannot reach the signed token;
* a ticket that is not parked at the gate is a 409, and a refund with no bound
  charge is a 400, rather than either becoming an unsigned money movement.

Needs fastapi/temporalio/python-multipart; skips otherwise, so the
zero-dependency `make verify` gate is unaffected.
"""

from __future__ import annotations

import importlib.util
import os
import re
import unittest
from typing import Any, ClassVar

_MISSING = [m for m in ("fastapi", "temporalio", "multipart", "httpx") if importlib.util.find_spec(m) is None]

SIGNING_SECRET = "test-approval-signing-secret"
PROXY_SECRET = "test-proxy-shared-secret"
REVIEWER = "reviewer@example.com"

PARKED_REFUND: dict[str, Any] = {
    "ticket_id": "tkt-1",
    "action_type": "refund",
    "subscription_id": "sub_123",
    "invoice_id": "in_123",
    "amount_minor": 5000,
    "currency": "USD",
    "policy_version": "2026.07.1",
    "charge_id": "ch_123",
    "reason_codes": ["within_window_low_usage"],
}


def setUpModule() -> None:
    if _MISSING:
        raise unittest.SkipTest(f"approval surface needs: {', '.join(_MISSING)}")
    os.environ.update(
        FARTHING_APPROVAL_STRIPE_EXECUTE_KEY="rk_test_execute",
        FARTHING_APPROVAL_APPROVAL_SIGNING_SECRET=SIGNING_SECRET,
        FARTHING_APPROVAL_DATABASE_URL="postgresql://localhost/farthing_test",
        FARTHING_APPROVAL_PROXY_SHARED_SECRET=PROXY_SECRET,
    )
    from farthing.config import approval_settings

    approval_settings.cache_clear()


class _FakeWorkflowInfo:
    def __init__(self, wf_id: str) -> None:
        self.id = wf_id


class _FakeHandle:
    """Just enough of a Temporal workflow handle: a queryable prepared action and
    a recorder for the signals the inbox sends."""

    def __init__(self, wf_id: str, client: _FakeClient) -> None:
        self.id = wf_id
        self._client = client

    async def query(self, name: str, **_: Any) -> Any:
        self._client.queries.append((self.id, name))
        if self._client.query_error is not None:
            raise self._client.query_error
        return self._client.prepared

    async def signal(self, name: str, arg: Any = None) -> None:
        self._client.signals.append((self.id, name, arg))


class _FakeClient:
    def __init__(self, prepared: dict[str, Any] | None, query_error: Exception | None = None) -> None:
        self.prepared = prepared
        self.query_error = query_error
        self.signals: list[tuple[str, str, Any]] = []
        self.queries: list[tuple[str, str]] = []

    def get_workflow_handle(self, wf_id: str) -> _FakeHandle:
        return _FakeHandle(wf_id, self)

    def list_workflows(self, _query: str) -> Any:
        """One parked workflow when there is a prepared action, else none."""
        ids = [f"farthing-ticket-{self.prepared['ticket_id']}"] if self.prepared else []

        class _Iter:
            def __init__(self) -> None:
                self._rest = list(ids)

            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> _FakeWorkflowInfo:
                if not self._rest:
                    raise StopAsyncIteration
                return _FakeWorkflowInfo(self._rest.pop(0))

        return _Iter()


class ApprovalAppTestBase(unittest.TestCase):
    """Builds the real FastAPI app with the Temporal client stubbed out."""

    prepared: ClassVar[dict[str, Any] | None] = PARKED_REFUND
    query_error: ClassVar[Exception | None] = None

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from farthing.approval import app as app_module

        self.app_module = app_module
        self.fake = _FakeClient(self.prepared, self.query_error)

        async def _fake_client() -> Any:
            return self.fake

        self._real_client = app_module._client
        app_module._client = _fake_client
        self.addCleanup(setattr, app_module, "_client", self._real_client)

        # HTTPS base URL: the session cookie is Secure, so an http:// test client
        # would silently drop it and every CSRF assertion would be vacuous.
        self.client = TestClient(app_module.app, base_url="https://testserver")
        self.addCleanup(self.client.close)

    def proxy_headers(self, *, proxy: bool = True, reviewer: str | None = REVIEWER) -> dict[str, str]:
        h: dict[str, str] = {}
        if proxy:
            h["X-Proxy-Auth"] = PROXY_SECRET
        if reviewer is not None:
            h["X-Reviewer-Email"] = reviewer
        return h

    def sign_in(self) -> str:
        """Load the inbox as an authenticated reviewer and return the CSRF token,
        taken from the rendered form exactly as a browser would submit it. An
        empty inbox renders no form, so those cases derive the token from the
        session cookie the same page set."""
        r = self.client.get("/inbox", headers=self.proxy_headers())
        self.assertEqual(r.status_code, 200, r.text)
        m = re.search(r'name="csrf" value="([0-9a-f]+)"', r.text)
        if m is not None:
            return m.group(1)
        session = self.client.cookies.get("fh_session")
        self.assertIsNotNone(session, "inbox set no session cookie")
        assert session is not None
        return self.app_module._csrf_for(session)


class TestApprovalAuthentication(ApprovalAppTestBase):
    def test_inbox_without_proxy_secret_is_401(self) -> None:
        r = self.client.get("/inbox", headers={"X-Reviewer-Email": REVIEWER})
        self.assertEqual(r.status_code, 401, r.text)

    def test_spoofed_reviewer_identity_without_proxy_secret_is_401(self) -> None:
        """The identity header alone is forgeable by anyone who can reach the app
        directly. Without the proxy secret it must buy nothing."""
        csrf = self.sign_in()
        r = self.client.post(
            "/inbox/tkt-1/approve",
            data={"csrf": csrf},
            headers={"X-Reviewer-Email": "attacker@evil.example"},
        )
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(self.fake.signals, [], "a spoofed identity signaled the workflow")

    def test_wrong_proxy_secret_is_401(self) -> None:
        r = self.client.get(
            "/inbox",
            headers={"X-Proxy-Auth": "not-the-secret", "X-Reviewer-Email": REVIEWER},
        )
        self.assertEqual(r.status_code, 401, r.text)

    def test_proxy_secret_without_reviewer_identity_is_401(self) -> None:
        r = self.client.get("/inbox", headers=self.proxy_headers(reviewer=None))
        self.assertEqual(r.status_code, 401, r.text)

    def test_authenticated_reviewer_sees_the_parked_action(self) -> None:
        r = self.client.get("/inbox", headers=self.proxy_headers())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(REVIEWER, r.text)
        self.assertIn("tkt-1", r.text)
        self.assertIn("50.00", r.text)


class TestApprovalCsrf(ApprovalAppTestBase):
    def test_approve_with_csrf_token_present_succeeds(self) -> None:
        csrf = self.sign_in()
        r = self.client.post(
            "/inbox/tkt-1/approve", data={"csrf": csrf},
            headers=self.proxy_headers(), follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303, r.text)
        self.assertEqual([(w, n) for w, n, _ in self.fake.signals], [("farthing-ticket-tkt-1", "approve")])

    def test_approve_with_csrf_token_absent_is_refused(self) -> None:
        """No token in the body at all: the required Form field rejects it at
        validation (422) before any workflow is touched."""
        self.sign_in()
        self.fake.queries.clear()  # the sign-in page legitimately queried
        r = self.client.post(
            "/inbox/tkt-1/approve", data={},
            headers=self.proxy_headers(), follow_redirects=False,
        )
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.fake.signals, [], "approved without a CSRF token")
        self.assertEqual(self.fake.queries, [], "loaded workflow state without a CSRF token")

    def test_approve_with_wrong_csrf_token_is_403(self) -> None:
        self.sign_in()
        r = self.client.post(
            "/inbox/tkt-1/approve", data={"csrf": "00" * 32},
            headers=self.proxy_headers(), follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.fake.signals, [], "approved with a forged CSRF token")

    def test_approve_without_the_session_cookie_is_403(self) -> None:
        """A cross-site POST carries the token but not the SameSite=Strict cookie."""
        csrf = self.sign_in()
        self.client.cookies.clear()
        r = self.client.post(
            "/inbox/tkt-1/approve", data={"csrf": csrf},
            headers=self.proxy_headers(), follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.fake.signals, [], "approved without the session cookie")

    def test_deny_requires_csrf(self) -> None:
        self.sign_in()
        r = self.client.post(
            "/inbox/tkt-1/deny", data={"csrf": "00" * 32},
            headers=self.proxy_headers(), follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.fake.signals, [], "denied with a forged CSRF token")


class TestApprovedAmountComesFromWorkflowState(ApprovalAppTestBase):
    def test_request_body_cannot_inflate_the_approved_amount(self) -> None:
        """The whole point of the gate: the reviewer's browser POST carries no
        authority over the figure. An inflated body must not reach the token."""
        csrf = self.sign_in()
        r = self.client.post(
            "/inbox/tkt-1/approve",
            data={
                "csrf": csrf,
                "amount_minor": "99999999",
                "currency": "EUR",
                "charge_id": "ch_attacker",
                "ticket_id": "tkt-other",
            },
            headers=self.proxy_headers(), follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303, r.text)

        (_wf, _name, approval) = self.fake.signals[0]
        token = approval.token
        self.assertEqual(token["amount_minor"], 5000, "the request body moved the approved amount")
        self.assertEqual(token["currency"], "USD", "the request body moved the approved currency")
        self.assertEqual(token["charge_id"], "ch_123", "the request body redirected the charge")
        self.assertEqual(token["ticket_id"], "tkt-1")
        self.assertEqual(token["approver"], REVIEWER, "the approver is not the authenticated reviewer")

    def test_the_issued_token_verifies_against_the_signing_secret(self) -> None:
        """A token bound to server-side figures is only worth anything if the
        signature over those figures is real."""
        from farthing.execution_guard import ApprovalToken, _sign

        csrf = self.sign_in()
        self.client.post(
            "/inbox/tkt-1/approve", data={"csrf": csrf},
            headers=self.proxy_headers(), follow_redirects=False,
        )
        token = ApprovalToken(**self.fake.signals[0][2].token)
        expected = _sign(
            SIGNING_SECRET.encode(), ticket_id=token.ticket_id, action_type=token.action_type,
            amount_minor=token.amount_minor, currency=token.currency, charge_id=token.charge_id,
            idempotency_key=token.idempotency_key, approver=token.approver, issued_ts=token.issued_ts,
        )
        self.assertEqual(token.signature, expected)
        self.assertEqual(token.amount_minor, 5000)


class TestTicketNotParked(ApprovalAppTestBase):
    prepared: ClassVar[dict[str, Any] | None] = None

    def test_approving_a_ticket_with_no_prepared_action_is_409(self) -> None:
        csrf = self.sign_in()
        r = self.client.post(
            "/inbox/tkt-1/approve", data={"csrf": csrf},
            headers=self.proxy_headers(), follow_redirects=False,
        )
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.fake.signals, [], "signaled a ticket that is not parked")


class TestTicketNotQueryable(ApprovalAppTestBase):
    query_error: ClassVar[Exception | None] = RuntimeError("workflow not found")

    def test_approving_an_unqueryable_workflow_is_409(self) -> None:
        csrf = self.sign_in()
        r = self.client.post(
            "/inbox/tkt-1/approve", data={"csrf": csrf},
            headers=self.proxy_headers(), follow_redirects=False,
        )
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.fake.signals, [], "signaled an unqueryable workflow")


class TestRefundWithoutBoundCharge(ApprovalAppTestBase):
    prepared: ClassVar[dict[str, Any] | None] = {**PARKED_REFUND, "charge_id": None}

    def test_refund_with_no_bound_charge_is_400(self) -> None:
        """Without a charge the refund target is unsigned; refuse rather than let
        Stripe pick one."""
        csrf = self.sign_in()
        r = self.client.post(
            "/inbox/tkt-1/approve", data={"csrf": csrf},
            headers=self.proxy_headers(), follow_redirects=False,
        )
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.fake.signals, [], "signaled a refund with no bound charge")


class TestSecurityHeaders(ApprovalAppTestBase):
    def test_inbox_cannot_be_framed(self) -> None:
        r = self.client.get("/inbox", headers=self.proxy_headers())
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")


class TestNoUnauthenticatedApiSchemaDisclosure(ApprovalAppTestBase):
    """The money service must expose no ungated informational route.

    The module contract is explicit: "every route requires an authenticated
    reviewer identity supplied by an upstream auth proxy ... No identity → 401",
    and `require_reviewer` exists precisely to defend the case where a client
    "reaches the app directly". FastAPI's default `/openapi.json`, `/docs` and
    `/redoc` are routes that carry no such dependency, so a direct-reaching,
    unauthenticated caller is handed the full map of the approve/deny surface —
    route templates, form fields, endpoint docstrings — which contradicts the
    stated contract. Left enabled, all three answer an unauthenticated request
    against a running instance.

    The fix is to disable the auto-generated schema/docs on this surface, which
    makes the "every route requires auth" claim true again.
    """

    def test_openapi_schema_is_not_served_unauthenticated(self) -> None:
        r = self.client.get("/openapi.json")
        self.assertEqual(r.status_code, 404, r.text)

    def test_swagger_ui_is_not_served_unauthenticated(self) -> None:
        r = self.client.get("/docs")
        self.assertEqual(r.status_code, 404, r.text)

    def test_redoc_is_not_served_unauthenticated(self) -> None:
        r = self.client.get("/redoc")
        self.assertEqual(r.status_code, 404, r.text)


if __name__ == "__main__":
    unittest.main()
