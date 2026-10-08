from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from incident_commander.api import ApiSettings, create_app
from incident_commander.api.app import MAX_BODY_BYTES
from incident_commander.api.jobs import ProgressFn, default_run
from incident_commander.config import Settings
from incident_commander.engine import IncidentRun
from incident_commander.scenario import Scenario, list_scenarios, load_scenario

RESPONDER = "responder-key-0123456789"
RESPONDER_2 = "responder-key-other-9876"
APPROVER = "approver-key-abcdefghijk"
PROVIDER_SECRET = "sk-provider-secret-never-leak"
RAW_SIGNALS = [
    "2026-10-08T03:30:00Z ERROR checkout-api NullPointerException in CartService",
    {
        "type": "deploy",
        "service": "checkout-api",
        "version": "v2",
        "timestamp": "2026-10-08T03:20:00Z",
    },
]


def make_client(**overrides: Any) -> TestClient:
    run_fn = overrides.pop("run_fn", None)
    settings = ApiSettings(**{"sync_runs": True, **overrides})
    agent = Settings(api_key=PROVIDER_SECRET)
    return TestClient(create_app(settings, agent_settings=agent, run_fn=run_fn))


def key(value: str) -> dict[str, str]:
    return {"X-API-Key": value}


def wait_done(client: TestClient, incident_id: str, headers: dict[str, str] | None = None) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        data = client.get(f"/api/incidents/{incident_id}/timeline", headers=headers).json()
        if data["done"]:
            return
        time.sleep(0.02)
    raise AssertionError("investigation did not finish")


class Gate:
    """A run function that blocks until released, to observe in-flight states."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()

    def __call__(
        self, scenario: Scenario, settings: Settings, incident_id: str, on_progress: ProgressFn
    ) -> IncidentRun:
        self.started.set()
        self.release.wait(10)
        return default_run(scenario, settings, incident_id, on_progress)


@pytest.fixture
def demo() -> Iterator[TestClient]:
    with make_client() as client:
        yield client


# ---------------------------------------------------------------------------------- meta
def test_healthz_and_security_headers(demo: TestClient) -> None:
    res = demo.get("/healthz")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    csp = res.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["referrer-policy"] == "no-referrer"
    assert demo.get("/api/config").headers["cache-control"] == "no-store"


def test_docs_are_served_without_strict_csp(demo: TestClient) -> None:
    res = demo.get("/docs")
    assert res.status_code == 200
    assert "content-security-policy" not in res.headers
    assert demo.get("/openapi.json").json()["info"]["title"] == "Incident Commander API"


def test_public_demo_config(demo: TestClient) -> None:
    cfg = demo.get("/api/config").json()
    assert cfg["access_mode"] == "public-demo"
    assert cfg["auth_required"] is False
    assert cfg["providers"] == ["mock"] and cfg["default_provider"] == "mock"
    assert cfg["raw_signals_allowed"] is False
    assert cfg["caller_role"] == "approver"
    assert cfg["simulated_environment"] is True
    assert cfg["sync_runs"] is True
    assert set(cfg["scenarios"]) == set(list_scenarios())


def test_scenarios_endpoint(demo: TestClient) -> None:
    data = demo.get("/api/scenarios").json()["scenarios"]
    names = {s["name"] for s in data}
    assert {"bad-deploy", "db-connection-exhaustion", "dependency-outage"} <= names
    assert all(s["services"] and s["description"] for s in data)


def test_dashboard_is_served_and_never_uses_html_sinks(demo: TestClient) -> None:
    page = demo.get("/")
    assert page.status_code == 200
    assert "Incident Commander" in page.text
    assert "content-security-policy" in page.headers
    script = demo.get("/static/app.js")
    assert script.status_code == 200
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in script.text
    assert demo.get("/static/styles.css").status_code == 200


# ------------------------------------------------------------------- incident lifecycle
@pytest.mark.parametrize("name", list_scenarios())
def test_sync_scenario_approve_resolves(demo: TestClient, name: str) -> None:
    expected = load_scenario(name).expected
    res = demo.post("/api/incidents", json={"scenario": name})
    assert res.status_code == 200, res.text
    inc = res.json()
    assert inc["state"] == "finished" and inc["scenario"] == name
    assert inc["simulated"] is True and "simulated" in inc["simulated_note"]
    assert inc["timeline"] and inc["timeline_count"] == len(inc["timeline"])
    assert inc["investigation_status"] == "concluded"
    root = next(h for h in inc["hypotheses"] if h["id"] == inc["root_cause_id"])
    assert (root["suspected_cause"], root["service"]) == (expected.cause, expected.service)
    proposal = next(p for p in inc["proposals"] if p["status"] == "proposed")
    assert proposal["runbook_id"] == expected.runbook_id
    assert inc["pending_approvals"] == 1

    res = demo.post(
        f"/api/incidents/{inc['id']}/actions/{proposal['id']}/approve",
        json={"approver": "Priya"},
    )
    assert res.status_code == 200, res.text
    decision = res.json()
    assert decision["proposal"]["status"] == "executed"
    assert decision["proposal"]["decided_by"] == "Priya"
    assert decision["incident_status"] == "resolved"
    assert decision["simulated"] is True

    detail = demo.get(f"/api/incidents/{inc['id']}").json()
    assert detail["status"] == "resolved" and detail["resolved_at"]
    assert detail["pending_approvals"] == 0

    md = demo.get(f"/api/incidents/{inc['id']}/postmortem")
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert md.text.startswith("# Postmortem:")
    assert "**Status:** resolved" in md.text and expected.runbook_id in md.text


def test_async_investigation_and_timeline_cursor() -> None:
    with make_client(sync_runs=False) as client:
        res = client.post("/api/incidents", json={"scenario": "bad-deploy"})
        assert res.status_code == 202
        created = res.json()
        assert created["state"] in {"queued", "running", "finished"}
        assert created["timeline"] == []
        wait_done(client, created["id"])
        full = client.get(f"/api/incidents/{created['id']}/timeline").json()
        assert full["state"] == "finished" and full["entries"]
        assert full["next_cursor"] == len(full["entries"])
        tail = client.get(
            f"/api/incidents/{created['id']}/timeline", params={"since": full["next_cursor"]}
        ).json()
        assert tail["entries"] == [] and tail["next_cursor"] == full["next_cursor"]
        part = client.get(f"/api/incidents/{created['id']}/timeline", params={"since": 3}).json()
        assert part["entries"] == full["entries"][3:]
        listed = client.get("/api/incidents").json()["incidents"]
        assert [i["id"] for i in listed] == [created["id"]]
        assert listed[0]["severity"] == "SEV2"


def test_in_flight_incident_is_visible_but_cannot_be_approved() -> None:
    gate = Gate()
    with make_client(sync_runs=False, run_fn=gate) as client:
        inc = client.post("/api/incidents", json={"scenario": "bad-deploy"}).json()
        assert gate.started.wait(5)
        detail = client.get(f"/api/incidents/{inc['id']}").json()
        assert detail["state"] == "running"
        assert client.get(f"/api/incidents/{inc['id']}/postmortem").status_code == 409
        res = client.post(
            f"/api/incidents/{inc['id']}/actions/act-123/approve", json={"approver": "a"}
        )
        assert res.status_code == 409
        gate.release.set()
        wait_done(client, inc["id"])
        assert client.get(f"/api/incidents/{inc['id']}").json()["state"] == "finished"


def test_reject_records_reason_and_blocks_second_decision(demo: TestClient) -> None:
    inc = demo.post("/api/incidents", json={"scenario": "bad-deploy"}).json()
    pid = inc["proposals"][0]["id"]
    res = demo.post(
        f"/api/incidents/{inc['id']}/actions/{pid}/reject",
        json={"approver": "Sam", "reason": "  want a\nforward fix  "},
    )
    assert res.status_code == 200
    assert res.json()["proposal"]["status"] == "rejected"
    assert res.json()["incident_status"] != "resolved"
    entries = demo.get(f"/api/incidents/{inc['id']}/timeline").json()["entries"]
    assert any("want a forward fix" in e["text"] for e in entries)
    again = demo.post(f"/api/incidents/{inc['id']}/actions/{pid}/approve", json={"approver": "Sam"})
    assert again.status_code == 409


def test_unknown_incident_and_proposal(demo: TestClient) -> None:
    assert demo.get("/api/incidents/inc-doesnotexist").status_code == 404
    assert demo.get("/api/incidents/bad.id!").status_code == 422
    inc = demo.post("/api/incidents", json={"scenario": "bad-deploy"}).json()
    res = demo.post(f"/api/incidents/{inc['id']}/actions/act-nope/approve", json={"approver": "x"})
    assert res.status_code == 404


def test_crashed_investigation_reports_error() -> None:
    def boom(*_: Any) -> IncidentRun:
        raise RuntimeError("provider exploded")

    with make_client(run_fn=boom) as client:
        res = client.post("/api/incidents", json={"scenario": "bad-deploy"})
        assert res.status_code == 200
        body = res.json()
        assert body["state"] == "error" and "provider exploded" in body["error"]
        assert client.get("/healthz").json()["active_investigations"] == 0


def test_queue_full_returns_503() -> None:
    gate = Gate()
    with make_client(sync_runs=False, run_fn=gate, max_queued_runs=1) as client:
        assert client.post("/api/incidents", json={"scenario": "bad-deploy"}).status_code == 202
        res = client.post("/api/incidents", json={"scenario": "bad-deploy"})
        assert res.status_code == 503 and res.headers["retry-after"]
        gate.release.set()


# ---------------------------------------------------------------- public demo guardrails
def test_public_demo_blocks_raw_signals_and_real_providers(demo: TestClient) -> None:
    assert demo.post("/api/incidents", json={"signals": RAW_SIGNALS}).status_code == 403
    res = demo.post("/api/incidents", json={"scenario": "bad-deploy", "provider": "openai"})
    assert res.status_code == 403


def test_public_demo_scopes_incidents_per_client() -> None:
    with make_client(trust_proxy=True) as client:
        a = {"X-Forwarded-For": "198.51.100.1"}
        b = {"X-Forwarded-For": "198.51.100.2"}
        inc = client.post("/api/incidents", json={"scenario": "bad-deploy"}, headers=a).json()
        assert client.get(f"/api/incidents/{inc['id']}", headers=b).status_code == 404
        assert client.get("/api/incidents", headers=b).json()["incidents"] == []
        assert client.get(f"/api/incidents/{inc['id']}", headers=a).status_code == 200


def test_provider_key_never_appears_in_responses(demo: TestClient) -> None:
    inc = demo.post("/api/incidents", json={"scenario": "dependency-outage"}).json()
    bodies = [
        demo.get("/api/config").text,
        demo.get(f"/api/incidents/{inc['id']}").text,
        demo.get(f"/api/incidents/{inc['id']}/postmortem").text,
        demo.get("/openapi.json").text,
    ]
    assert all(PROVIDER_SECRET not in body for body in bodies)


# ------------------------------------------------------------------------ API-key mode
@pytest.fixture
def keyed() -> Iterator[TestClient]:
    with make_client(responder_keys=(RESPONDER, RESPONDER_2), approver_keys=(APPROVER,)) as c:
        yield c


def test_api_key_required(keyed: TestClient) -> None:
    assert keyed.get("/api/incidents").status_code == 401
    res = keyed.get("/api/incidents", headers=key("wrong"))
    assert res.status_code == 401 and res.headers["www-authenticate"] == "Bearer"
    assert keyed.get("/api/incidents", headers=key(RESPONDER)).status_code == 200
    bearer = {"Authorization": f"Bearer {APPROVER}"}
    assert keyed.get("/api/incidents", headers=bearer).status_code == 200
    assert keyed.get("/healthz").status_code == 200


def test_config_reports_caller_role(keyed: TestClient) -> None:
    assert keyed.get("/api/config").json()["caller_role"] is None
    assert keyed.get("/api/config", headers=key(RESPONDER)).json()["caller_role"] == "responder"
    assert keyed.get("/api/config", headers=key(APPROVER)).json()["caller_role"] == "approver"
    cfg = keyed.get("/api/config", headers=key(APPROVER)).json()
    assert cfg["access_mode"] == "api-key" and cfg["raw_signals_allowed"] is True


def test_roles_ownership_and_approval_gate(keyed: TestClient) -> None:
    inc = keyed.post(
        "/api/incidents", json={"scenario": "bad-deploy"}, headers=key(RESPONDER)
    ).json()
    pid = inc["proposals"][0]["id"]
    url = f"/api/incidents/{inc['id']}/actions/{pid}"

    # Responders cannot approve, even their own incident.
    res = keyed.post(f"{url}/approve", json={"approver": "Rae"}, headers=key(RESPONDER))
    assert res.status_code == 403
    # Another responder cannot even see it.
    assert keyed.get(f"/api/incidents/{inc['id']}", headers=key(RESPONDER_2)).status_code == 404
    assert keyed.get("/api/incidents", headers=key(RESPONDER_2)).json()["incidents"] == []
    # Approvers review everyone's incidents and their key is recorded with the decision.
    listed = keyed.get("/api/incidents", headers=key(APPROVER)).json()["incidents"]
    assert [i["id"] for i in listed] == [inc["id"]]
    res = keyed.post(f"{url}/approve", json={"approver": "Lee"}, headers=key(APPROVER))
    assert res.status_code == 200
    decided_by = res.json()["proposal"]["decided_by"]
    assert decided_by.startswith("Lee (key ") and APPROVER not in decided_by
    assert res.json()["incident_status"] == "resolved"


def test_trusted_caller_can_submit_raw_signals(keyed: TestClient) -> None:
    res = keyed.post(
        "/api/incidents",
        json={
            "signals": RAW_SIGNALS,
            "title": "Checkout errors",
            "topology": {
                "services": {
                    "web": {"depends_on": ["checkout-api"], "customer_facing": True},
                    "checkout-api": {},
                }
            },
        },
        headers=key(RESPONDER),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["scenario"] == "custom" and body["title"] == "Checkout errors"
    assert body["signal_count"] >= 1


def test_raw_signals_validation_errors(keyed: TestClient) -> None:
    bad = keyed.post("/api/incidents", json={"signals": [{"weird": True}]}, headers=key(RESPONDER))
    assert bad.status_code == 422 and "invalid signals" in bad.json()["detail"]
    topo = keyed.post(
        "/api/incidents",
        json={"signals": RAW_SIGNALS, "topology": {"services": {"a": {"depends_on": ["zz"]}}}},
        headers=key(RESPONDER),
    )
    assert topo.status_code == 422


def test_dev_mode_allows_everything_without_key() -> None:
    with make_client(dev_mode=True) as client:
        cfg = client.get("/api/config").json()
        assert cfg["access_mode"] == "dev" and cfg["providers"] == ["mock", "openai"]
        res = client.post("/api/incidents", json={"signals": RAW_SIGNALS})
        assert res.status_code == 200


# --------------------------------------------------------------------------- validation
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"scenario": "bad-deploy", "signals": RAW_SIGNALS},
        {"scenario": "bad-deploy", "extra": 1},
        {"scenario": "../etc/passwd"},
        {"scenario": "bad-deploy", "max_steps": 0},
        {"scenario": "bad-deploy", "max_steps": 31},
        {"scenario": "bad-deploy", "provider": "anthropic"},
        {"scenario": "bad-deploy", "title": "only for raw signals"},
        {"scenario": "no-such-scenario"},
        {"signals": []},
    ],
)
def test_create_validation_errors(demo: TestClient, body: dict[str, Any]) -> None:
    assert demo.post("/api/incidents", json=body).status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"approver": ""},
        {"approver": "   "},
        {"approver": "a\nb"},
        {"approver": "x" * 81},
        {"approver": "ok", "reason": "r" * 501},
        {"approver": "ok", "role": "admin"},
    ],
)
def test_decision_validation_errors(demo: TestClient, body: dict[str, Any]) -> None:
    inc = demo.post("/api/incidents", json={"scenario": "bad-deploy"}).json()
    pid = inc["proposals"][0]["id"]
    res = demo.post(f"/api/incidents/{inc['id']}/actions/{pid}/approve", json=body)
    assert res.status_code == 422


def test_body_cap_by_content_length(demo: TestClient) -> None:
    payload = b"{" + b" " * (MAX_BODY_BYTES + 10) + b"}"
    res = demo.post("/api/incidents", content=payload, headers={"Content-Type": "application/json"})
    assert res.status_code == 413
    assert "content-security-policy" in res.headers


def test_body_cap_for_streamed_body(demo: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(10):
            yield b" " * (MAX_BODY_BYTES // 4)

    res = demo.post(
        "/api/incidents", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert res.status_code == 413


# ------------------------------------------------------------------------- rate limits
def test_general_rate_limit() -> None:
    with make_client(rate_limit_per_minute=3) as client:
        codes = [client.get("/api/incidents").status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]
        res = client.get("/api/incidents")
        assert int(res.headers["retry-after"]) >= 1


def test_incident_creation_limit_counts_only_valid_requests() -> None:
    with make_client(incident_limit_per_minute=1) as client:
        assert client.post("/api/incidents", json={"scenario": "nope-x"}).status_code == 422
        assert client.post("/api/incidents", json={"scenario": "bad-deploy"}).status_code == 200
        res = client.post("/api/incidents", json={"scenario": "bad-deploy"})
        assert res.status_code == 429 and res.headers["retry-after"]


def test_forwarded_for_uses_right_most_hop_only_when_trusted() -> None:
    with make_client(trust_proxy=True, rate_limit_per_minute=1) as client:
        assert client.get("/api/incidents", headers={"X-Forwarded-For": "1.1.1.1"}).is_success
        spoofed = {"X-Forwarded-For": "9.9.9.9, 1.1.1.1"}
        assert client.get("/api/incidents", headers=spoofed).status_code == 429
        assert client.get("/api/incidents", headers={"X-Forwarded-For": "2.2.2.2"}).is_success
    with make_client(rate_limit_per_minute=1) as client:
        assert client.get("/api/incidents", headers={"X-Forwarded-For": "3.3.3.3"}).is_success
        res = client.get("/api/incidents", headers={"X-Forwarded-For": "4.4.4.4"})
        assert res.status_code == 429


# --------------------------------------------------------------------------------- CORS
def test_cors_only_for_configured_origins() -> None:
    with make_client(cors_origins=("https://ops.example.com",)) as client:
        ok = client.get("/api/config", headers={"Origin": "https://ops.example.com"})
        assert ok.headers["access-control-allow-origin"] == "https://ops.example.com"
        other = client.get("/api/config", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in other.headers
    with make_client() as client:
        res = client.get("/api/config", headers={"Origin": "https://ops.example.com"})
        assert "access-control-allow-origin" not in res.headers


# ----------------------------------------------------------------------------- settings
def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "IC_SYNC_RUNS", "IC_DEV_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("IC_API_KEYS", "a, b ,")
    monkeypatch.setenv("IC_APPROVER_KEYS", "c")
    monkeypatch.setenv("IC_CORS_ORIGINS", "https://x.example")
    cfg = ApiSettings.from_env()
    assert cfg.responder_keys == ("a", "b") and cfg.approver_keys == ("c",)
    assert cfg.access_mode == "api-key" and cfg.sync_runs is False
    monkeypatch.setenv("VERCEL", "1")
    assert ApiSettings.from_env().sync_runs is True
    monkeypatch.setenv("IC_SYNC_RUNS", "false")
    assert ApiSettings.from_env().sync_runs is False


def test_serve_command_honours_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import uvicorn

    from incident_commander.cli import main

    seen: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw, app=app))
    monkeypatch.setenv("PORT", "9123")
    monkeypatch.delenv("IC_API_KEYS", raising=False)
    monkeypatch.delenv("IC_APPROVER_KEYS", raising=False)
    assert main(["serve"]) == 0
    assert seen["port"] == 9123 and seen["host"] == "127.0.0.1"
    assert main(["serve", "--port", "8001", "--host", "0.0.0.0"]) == 0
    assert seen["port"] == 8001 and seen["host"] == "0.0.0.0"
