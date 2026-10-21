#!/usr/bin/env python3
"""Local-first smoke test for the AURA first-user workflow (Server 1).

Exercises the real HTTP API end to end against a running backend:

    register -> login -> /auth/me -> /brain/query

Safety properties:

* Defaults to ``http://127.0.0.1:8000`` and refuses any non-loopback host
  unless BOTH ``--allow-remote`` and ``AURA_ALLOW_REMOTE_WORKFLOW=1`` are set,
  so it cannot be pointed at production by accident.
* Never prints passwords, bearer/refresh tokens or raw response bodies. Only
  status codes and opaque identifiers are shown; a generated password is only
  echoed when ``--print-password`` is passed explicitly.
* Uses only the standard library (no third-party dependencies).

Examples::

    # local backend already running on :8000
    python scripts/dev_user_workflow.py

    # lock-down registration: create the first account with a bootstrap token
    python scripts/dev_user_workflow.py --bootstrap-token "$BOOTSTRAP_TOKEN"
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlparse

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_loopback(base_url: str) -> bool:
    host = urlparse(base_url).hostname or ""
    return host in LOOPBACK_HOSTS


def _request(
    method: str,
    url: str,
    body: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30.0,
) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            parsed: object = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"detail": raw[:200]}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        print(f"error: cannot reach {url}: {exc.reason}", file=sys.stderr)
        raise SystemExit(2)


def _fail(step: str, status: int, payload: object) -> None:
    detail = payload.get("detail") if isinstance(payload, dict) else payload
    print(f"FAILED at {step}: HTTP {status} ({detail})", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.environ.get("AURA_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--email", default=f"dev-{uuid.uuid4().hex[:8]}@example.com")
    parser.add_argument("--password", default=None, help="Defaults to a random password")
    parser.add_argument("--full-name", default="Dev Workflow User")
    parser.add_argument("--query", default="Say hello to my Personal Brain in one sentence.")
    parser.add_argument("--bootstrap-token", default=os.environ.get("BOOTSTRAP_TOKEN", ""))
    parser.add_argument("--print-password", action="store_true", help="Echo the account password (dev only)")
    parser.add_argument("--allow-remote", action="store_true", help="Allow a non-loopback base URL")
    parser.add_argument("--timeout", type=float, default=120.0, help="Per-request timeout in seconds")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    if not _is_loopback(base_url):
        if not args.allow_remote or os.environ.get("AURA_ALLOW_REMOTE_WORKFLOW") != "1":
            print(
                "refusing to run against a non-loopback host.\n"
                f"  base_url = {base_url}\n"
                "  set --allow-remote AND AURA_ALLOW_REMOTE_WORKFLOW=1 to override",
                file=sys.stderr,
            )
            return 2

    password = args.password or secrets.token_urlsafe(12)
    if len(password) < 8:
        print("error: password must be at least 8 characters", file=sys.stderr)
        return 2
    if args.print_password:
        print(f"account password: {password}", file=sys.stderr)

    print(f"base_url: {base_url}")
    print(f"email:    {args.email}")

    # 1. Register (idempotent: an existing account is fine).
    register_headers = {"X-Bootstrap-Token": args.bootstrap_token} if args.bootstrap_token else None
    status, payload = _request(
        "POST",
        f"{base_url}/api/v1/users",
        {"email": args.email, "password": password, "full_name": args.full_name},
        register_headers,
        args.timeout,
    )
    if status == 201:
        print(f"register: HTTP {status} (created)")
    elif status == 400:
        print(f"register: HTTP {status} (already exists, continuing)")
    elif status == 403 and args.bootstrap_token:
        _fail("register (bootstrap token rejected)", status, payload)
    else:
        _fail("register", status, payload)

    # 2. Login.
    status, payload = _request(
        "POST",
        f"{base_url}/api/v1/auth/login",
        {"email": args.email, "password": password},
        None,
        args.timeout,
    )
    if status != 200 or not isinstance(payload, dict) or "access_token" not in payload:
        _fail("login", status, payload)
    access_token = payload["access_token"]
    print(f"login:    HTTP {status} (tokens received)")

    auth_headers = {"Authorization": f"Bearer {access_token}"}

    # 3. Authenticated identity.
    status, payload = _request("GET", f"{base_url}/api/v1/auth/me", None, auth_headers, args.timeout)
    if status != 200 or not isinstance(payload, dict):
        _fail("GET /auth/me", status, payload)
    if payload.get("email") and payload["email"] != args.email:
        _fail("GET /auth/me", status, "returned a different account")
    print(f"me:       HTTP {status} user_id={payload.get('user_id')} email={payload.get('email')}")

    # 4. Personal Brain query (proves user -> identity -> brain binding works).
    status, payload = _request(
        "POST",
        f"{base_url}/api/v1/brain/query",
        {"query": args.query, "use_memory": True, "persist_interaction": False},
        auth_headers,
        args.timeout,
    )
    if status != 200 or not isinstance(payload, dict):
        _fail("POST /brain/query", status, payload)
    source = payload.get("source") or "unknown"
    provider = payload.get("provider") or "unknown"
    answer = payload.get("answer") or payload.get("response") or ""
    print(f"brain:    HTTP {status} source={source} provider={provider} answer_chars={len(str(answer))}")

    if source == "server2_error":
        print(
            "WARNING: brain query was answered by the degraded fallback, not real "
            "inference. Check Server 2 reachability and that AI_SERVER_MODEL matches "
            "Server 2's model (see GET /api/v1/health -> services.ai_server).",
            file=sys.stderr,
        )
        return 3

    print("OK: first-user workflow completed end to end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
