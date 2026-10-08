#!/usr/bin/env python3
"""End-to-end smoke test against a running server (stdlib only).

Usage: python3 scripts/smoke_test.py [BASE_URL] [--api-key KEY]

For every packaged scenario: open an incident, wait for the investigation (works with background
and sync mode), approve the proposed runbook, and check the incident resolves and the postmortem
renders. Exits non-zero on the first failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any


class Client:
    def __init__(self, base: str, api_key: str | None) -> None:
        self.base = base.rstrip("/")
        self.api_key = api_key

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("X-API-Key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                raw = res.read().decode()
                ctype = res.headers.get("content-type", "")
                payload = json.loads(raw) if "json" in ctype else raw
                return res.status, payload, res.headers
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"FAIL {method} {path}: HTTP {exc.code} {exc.read()[:300]!r}") from exc


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL {message}")


def run_scenario(client: Client, name: str) -> None:
    status, inc, _ = client.request("POST", "/api/incidents", {"scenario": name})
    check(status in (200, 202), f"{name}: create returned {status}")
    iid = inc["id"]
    deadline = time.monotonic() + 60
    while True:
        _, tl, _ = client.request("GET", f"/api/incidents/{iid}/timeline?since=0")
        if tl["done"]:
            break
        check(time.monotonic() < deadline, f"{name}: investigation timed out")
        time.sleep(0.5)
    _, detail, _ = client.request("GET", f"/api/incidents/{iid}")
    check(detail["state"] == "finished", f"{name}: state {detail['state']} ({detail['error']})")
    check(detail["investigation_status"] == "concluded", f"{name}: not concluded")
    pending = [p for p in detail["proposals"] if p["status"] == "proposed"]
    check(len(pending) == 1, f"{name}: expected one pending proposal")
    action = pending[0]
    _, decision, _ = client.request(
        "POST",
        f"/api/incidents/{iid}/actions/{action['id']}/approve",
        {"approver": "smoke-test"},
    )
    check(decision["incident_status"] == "resolved", f"{name}: not resolved after approval")
    _, md, headers = client.request("GET", f"/api/incidents/{iid}/postmortem")
    check(headers.get("content-type", "").startswith("text/markdown"), f"{name}: postmortem type")
    check("**Status:** resolved" in md, f"{name}: postmortem missing resolved status")
    print(f"PASS {name}: {detail['severity']} -> {action['runbook_id']} -> resolved")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args()
    client = Client(args.base, args.api_key)

    deadline = time.monotonic() + 60
    while True:
        try:
            _, health, _ = client.request("GET", "/healthz")
            break
        except (urllib.error.URLError, ConnectionError, SystemExit):
            check(time.monotonic() < deadline, "server did not become healthy")
            time.sleep(1)
    check(health["status"] == "ok", "health status")
    _, page, headers = client.request("GET", "/")
    check("Incident Commander" in page, "dashboard page")
    check("default-src 'self'" in headers.get("content-security-policy", ""), "CSP header")
    _, config, _ = client.request("GET", "/api/config")
    print(f"PASS health + dashboard: v{health['version']}, mode {config['access_mode']}")
    for name in config["scenarios"]:
        run_scenario(client, name)
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
