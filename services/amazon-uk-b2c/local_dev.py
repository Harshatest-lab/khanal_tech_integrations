#!/usr/bin/env python3
"""Local dev runner — exercises the REAL Lambda handlers on your machine.

Serves dashboard/ statics directly off disk and /api/* through
src/handlers.py's dashboard_handler() by synthesizing API Gateway HTTP API
(payload v2.0) events (matching the real deploy split: statics on S3, API
on this Lambda), and runs fetch_handler / post_invoices_handler directly,
so what runs here for /api/* is exactly what runs in Lambda.

Note: dashboard/app.js hardcodes API_BASE to the deployed AWS API Gateway
URL (see its top-of-file comment), so opening this local server's `/` in a
browser will still call the real deployed API, not this local one — the
local /api/* here is for direct curl/testing against the real handler code,
not for browser preview. Same tradeoff the Walmart dashboard's hardcoded
API_BASE already has.

Usage (from this folder):

    # 1. point it at the credentials (reads KEY=value lines)
    cp ~/amazon-uk/.env .env            # same keys the standalone uses

    # 2. map the SAP_B1_* keys to the shared layer's SAP_SL_* names
    export SAP_SL_USE_SSM=false
    export SAP_SL_BASE_URL=$(grep ^SAP_B1_BASE_URL .env | cut -d= -f2)
    export SAP_SL_COMPANYDB=$(grep ^SAP_B1_COMPANY_DB .env | cut -d= -f2)
    export SAP_SL_USERNAME=$(grep ^SAP_B1_USERNAME .env | cut -d= -f2)
    export SAP_SL_PASSWORD=$(grep ^SAP_B1_PASSWORD .env | cut -d= -f2)

    # 3. run (dashboard statics + local /api, port 8091)
    python3 local_dev.py dashboard

    # 4. pipeline smoke test WITHOUT touching SAP (sync only, safe)
    python3 local_dev.py incremental
    python3 local_dev.py incremental 1      # first run: 1-day catch-up

    # 5. SAP posting (LIVE — hits the SAP company DB in SAP_SL_COMPANYDB)
    python3 local_dev.py post <amazon_order_id>
    export AMZ_UK_POSTING_CUTOFF_DATE=2026-09-01     # required for post-all / retry
    python3 local_dev.py post-all
    python3 local_dev.py retry

This local server's own /api/* has no Cognito check (there's no API Gateway
authorizer running locally) — curl it directly to test dashboard.py without
a token. A browser hitting `/` here still goes through real Cognito login,
since app.js's hardcoded API_BASE points at the real deployed API either way.

The dashboard is read-only (GET only). In AWS, fetch and post run only from
their EventBridge schedules; the fetch/post commands below are developer
tools that run the same handlers from this machine. post-all / retry also
need AMZ_UK_POSTING_CUTOFF_DATE=YYYY-MM-DD exported (same required setting
the scheduled run uses).
"""

from __future__ import annotations

import base64
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "src"))
sys.path.insert(0, str(_HERE.parent.parent / "shared"))  # khanal_shared


def _load_dotenv() -> None:
    """Load KEY=value from .env into os.environ (missing keys left alone)."""
    import os

    env_path = _HERE / ".env"
    if not env_path.exists():
        print(f"note: no {env_path} — relying on exported environment variables")
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def _load_handlers():
    import handlers

    return handlers


def _http_event(method: str, path: str, query: str, body: bytes) -> dict:
    return {
        "version": "2.0",
        "requestContext": {"http": {"method": method, "path": path}},
        "rawQueryString": query,
        "isBase64Encoded": True,
        "body": base64.b64encode(body).decode(),
    }


# dashboard_handler only serves /api/* now (the real deploy hosts the
# static files on S3 instead) — serve dashboard/ directly here so the local
# one-command preview still works.
_DASHBOARD_DIR = _HERE / "dashboard"
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class Handler(BaseHTTPRequestHandler):
    lambda_handler = staticmethod(_load_handlers().dashboard_handler)

    def _serve_static(self, path: str) -> None:
        import os

        rel = path.lstrip("/") or "index.html"
        root = os.path.realpath(str(_DASHBOARD_DIR))
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            self._respond({"statusCode": 404, "body": "not found"})
            return
        ctype = _STATIC_TYPES.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            content = f.read()
        self._respond({
            "statusCode": 200,
            "headers": {"Content-Type": ctype},
            "isBase64Encoded": True,
            "body": base64.b64encode(content).decode(),
        })

    def _respond(self, resp: dict) -> None:
        body = resp.get("body", "")
        if resp.get("isBase64Encoded"):
            raw = base64.b64decode(body)
        elif isinstance(body, str):
            raw = body.encode()
        else:
            raw = json.dumps(body, default=str).encode()
        self.send_response(resp.get("statusCode", 200))
        for k, v in (resp.get("headers") or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        event = _http_event(method, parsed.path, parsed.query, body)
        try:
            self._respond(self.lambda_handler(event, None))
        except Exception as e:  # keep the local server alive
            import traceback

            traceback.print_exc()
            self._respond({"statusCode": 500, "body": json.dumps({"error": str(e)})})

    def do_GET(self):
        if not urlparse(self.path).path.startswith("/api/"):
            self._serve_static(urlparse(self.path).path)
            return
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, fmt, *args):
        pass


def run_dashboard(port: int = 8091) -> None:
    print(f"Dashboard on http://localhost:{port}  (Ctrl+C to stop)")
    print("Note: app.js's hardcoded API_BASE calls the real deployed API —")
    print("this port's own /api/* (no auth) is for direct curl testing only.")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def run_fn(name: str, event: dict) -> None:
    handler = getattr(_load_handlers(), name)
    print(f"--> {name}({json.dumps(event)})\n")
    result = handler(event, None)
    print("\nresult:", json.dumps(result, indent=2, default=str)[:4000])


def _confirm_live(what: str) -> None:
    if input(f"This will POST live to SAP ({what}). Type 'yes' to continue: ").strip().lower() != "yes":
        sys.exit("aborted")


if __name__ == "__main__":
    _load_dotenv()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dashboard"
    if cmd == "dashboard":
        run_dashboard(int(sys.argv[2]) if len(sys.argv) > 2 else 8091)
    elif cmd == "incremental":
        event = {"mode": "incremental"}
        if len(sys.argv) > 2:
            event["initial_lookback_days"] = int(sys.argv[2])
        run_fn("fetch_handler", event)
    elif cmd == "post":
        if len(sys.argv) < 3:
            sys.exit("usage: local_dev.py post <amazon_order_id>")
        _confirm_live(sys.argv[2])
        run_fn("post_invoices_handler", {"amazon_order_id": sys.argv[2]})
    elif cmd == "post-all":
        _confirm_live("all Shipped, not-yet-posted orders")
        run_fn("post_invoices_handler", {"mode": "post"})
    elif cmd == "retry":
        _confirm_live("all recorded failures")
        run_fn("post_invoices_handler", {"mode": "retry"})
    elif cmd == "refunds-incremental":
        event = {"mode": "incremental"}
        if len(sys.argv) > 2:
            event["initial_lookback_days"] = int(sys.argv[2])
        run_fn("fetch_refunds_handler", event)
    elif cmd == "post-credit-note":
        if len(sys.argv) < 3:
            sys.exit("usage: local_dev.py post-credit-note <transaction_id>")
        _confirm_live(sys.argv[2])
        run_fn("post_credit_notes_handler", {"transaction_id": sys.argv[2]})
    elif cmd == "post-all-credit-notes":
        _confirm_live("all not-yet-posted RELEASED refunds")
        run_fn("post_credit_notes_handler", {"mode": "post"})
    elif cmd == "retry-credit-notes":
        _confirm_live("all recorded credit-note failures")
        run_fn("post_credit_notes_handler", {"mode": "retry"})
    else:
        sys.exit(
            f"unknown command {cmd!r} (use dashboard | incremental [days] | post <order_id> | post-all | retry | "
            "refunds-incremental [days] | post-credit-note <transaction_id> | post-all-credit-notes | "
            "retry-credit-notes)"
        )
