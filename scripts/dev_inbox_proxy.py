"""Dev-only reverse proxy that makes the approval inbox browser-clickable.

The inbox fails CLOSED: every request must present the shared ``X-Proxy-Auth``
secret AND an authenticated ``X-Reviewer-Email`` — in production these come from
an SSO auth proxy (Caddy, oauth2-proxy, ...). A browser cannot set custom headers,
so for a LOCAL demo this ~40-line proxy injects both and forwards to the real
inbox. It is a development aid ONLY: it hands every visitor a reviewer identity,
which is exactly what a real proxy must never do. Never expose it publicly.

    FARTHING_APPROVAL_PROXY_SHARED_SECRET=... uv run python scripts/dev_inbox_proxy.py
    # then open http://127.0.0.1:8090/inbox

Env:
    FARTHING_APPROVAL_PROXY_SHARED_SECRET  (required) must match the inbox's secret
    FARTHING_DEV_REVIEWER                  reviewer identity to inject (default demo@example.com)
    FARTHING_DEV_PROXY_PORT                port to listen on (default 8090)
    FARTHING_INBOX_HOST / _PORT            the real inbox (default 127.0.0.1:8080)
"""

from __future__ import annotations

import http.client
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET_HOST = os.environ.get("FARTHING_INBOX_HOST", "127.0.0.1")
TARGET_PORT = int(os.environ.get("FARTHING_INBOX_PORT", "8080"))
LISTEN_PORT = int(os.environ.get("FARTHING_DEV_PROXY_PORT", "8090"))
REVIEWER = os.environ.get("FARTHING_DEV_REVIEWER", "demo@example.com")

# Headers we must not blindly forward (per-hop or length/host that we recompute).
_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})


def _proxy_secret() -> str:
    secret = os.environ.get("FARTHING_APPROVAL_PROXY_SHARED_SECRET", "")
    if not secret:
        sys.exit("Set FARTHING_APPROVAL_PROXY_SHARED_SECRET to the inbox's proxy secret.")
    return secret


class _Handler(BaseHTTPRequestHandler):
    proxy_secret = ""  # set in __main__

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
        headers["X-Proxy-Auth"] = self.proxy_secret          # prove we came "via the proxy"
        headers["X-Reviewer-Email"] = REVIEWER               # inject the authenticated identity
        conn = http.client.HTTPConnection(TARGET_HOST, TARGET_PORT)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status)
            for key, value in resp.getheaders():           # relay Set-Cookie, Location, Content-Type, ...
                if key.lower() not in _HOP:
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)
        finally:
            conn.close()

    do_GET = _forward
    do_POST = _forward

    def log_message(self, fmt: str, *args: object) -> None:  # quieter than the default
        return


def main() -> None:
    _Handler.proxy_secret = _proxy_secret()
    print(
        f"dev inbox proxy  http://127.0.0.1:{LISTEN_PORT}  ->  "
        f"http://{TARGET_HOST}:{TARGET_PORT}   (injecting reviewer={REVIEWER})\n"
        f"open http://127.0.0.1:{LISTEN_PORT}/inbox   (Ctrl-C to stop)"
    )
    ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), _Handler).serve_forever()


if __name__ == "__main__":
    main()
