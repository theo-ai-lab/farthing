"""Adversarial probe of the RUNNING approval inbox — the "ten agents try to break
the deployed thing" test, aimed at the money gate's HTTP boundary.

It sends hostile requests to a live inbox and asserts each is refused: an
unauthenticated read, the header-spoof bypass (identity header without the proxy
secret), a wrong proxy secret, and forged/mismatched CSRF on the approve POST. A
single control request proves the legitimate path still returns 200, so the 401s
mean "blocked", not "inbox is down". Deeper money-path attacks (replay,
amount-drift, unbound-charge) are enforced at the guard and covered by
tests/test_execution_guard.py; this script covers the deployment surface those
tests can't reach.

    FARTHING_APPROVAL_PROXY_SHARED_SECRET=... uv run python scripts/redteam_inbox.py
    # exits non-zero if ANY attack was not blocked (or the control failed)

Env:
    FARTHING_APPROVAL_PROXY_SHARED_SECRET  (required) the inbox's real proxy secret
    FARTHING_INBOX_BASE                     base URL (default http://127.0.0.1:8080)
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlencode

BASE = os.environ.get("FARTHING_INBOX_BASE", "http://127.0.0.1:8080").rstrip("/")
SECRET = os.environ.get("FARTHING_APPROVAL_PROXY_SHARED_SECRET", "")
REVIEWER = "attacker@evil.example"
FORM = urlencode({"csrf": "forged-token"}).encode()


def _status(method: str, path: str, headers: dict[str, str], data: bytes | None = None) -> int:
    req = urllib.request.Request(f"{BASE}{path}", method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


# name, method, path, headers, body, expected status, verdict-word-if-expected
CHECKS = [
    ("unauthenticated read",        "GET",  "/inbox", {}, None, 401, "blocked"),
    ("identity-header spoof",       "GET",  "/inbox", {"X-Reviewer-Email": REVIEWER}, None, 401, "blocked"),
    ("wrong proxy secret",          "GET",  "/inbox",
     {"X-Proxy-Auth": "not-the-secret", "X-Reviewer-Email": REVIEWER}, None, 401, "blocked"),
    ("CONTROL: legit read",         "GET",  "/inbox",
     {"X-Proxy-Auth": SECRET, "X-Reviewer-Email": "reviewer@example.com"}, None, 200, "allowed"),
    ("approve, forged CSRF (no cookie)", "POST", "/inbox/demo-001/approve",
     {"X-Proxy-Auth": SECRET, "X-Reviewer-Email": "reviewer@example.com",
      "Content-Type": "application/x-www-form-urlencoded"}, FORM, 403, "blocked"),
    ("approve, forged CSRF + cookie", "POST", "/inbox/demo-001/approve",
     {"X-Proxy-Auth": SECRET, "X-Reviewer-Email": "reviewer@example.com",
      "Content-Type": "application/x-www-form-urlencoded", "Cookie": "fh_session=forged"}, FORM, 403, "blocked"),
]


def main() -> None:
    if not SECRET:
        sys.exit("FARTHING_APPROVAL_PROXY_SHARED_SECRET is required (the inbox's proxy secret, for the control check).")

    print(f"Red-teaming inbox at {BASE}\n")
    print(f"{'attack':<34}{'want':>6}{'got':>6}  result")
    print("-" * 60)
    failures = 0
    for name, method, path, headers, body, expected, _word in CHECKS:
        got = _status(method, path, headers, body)
        ok = got == expected
        failures += 0 if ok else 1
        print(f"{name:<34}{expected:>6}{got:>6}  {'PASS' if ok else 'FAIL <-- NOT BLOCKED'}")

    print("-" * 60)
    if failures:
        print(f"\n{failures} check(s) FAILED — the money gate did not hold. Investigate before demoing.")
        sys.exit(1)
    print("\nAll boundary attacks blocked; legitimate access works. Money gate holds.")


if __name__ == "__main__":
    main()
