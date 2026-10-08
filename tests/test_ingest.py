from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from incident_commander.ingest import (
    MAX_BATCH_SIGNALS,
    IngestError,
    ingest_batch,
    ingest_event,
    parse_log_line,
)
from incident_commander.models import Severity, SignalKind


def test_alertmanager_payload_becomes_alert_signals() -> None:
    payload = {
        "status": "firing",
        "alerts": [
            {
                "labels": {
                    "alertname": "High5xx",
                    "service": "checkout-api",
                    "severity": "critical",
                },
                "annotations": {"summary": "5xx at 18%"},
                "startsAt": "2026-10-08T03:36:00Z",
            },
            {
                "status": "resolved",
                "labels": {"alertname": "DiskFull", "job": "db host", "severity": "warning"},
                "annotations": {},
                "startsAt": "2026-10-08T03:37:00+05:30",
            },
        ],
    }
    first, second = ingest_event(payload)
    assert first.kind is SignalKind.ALERT and first.source == "alertmanager"
    assert first.summary == "High5xx: 5xx at 18%"
    assert first.severity_hint is Severity.SEV2
    assert first.timestamp == datetime(2026, 10, 8, 3, 36, tzinfo=UTC)
    assert second.service == "db-host"  # sanitised
    assert second.attrs["status"] == "resolved"
    assert second.severity_hint is Severity.SEV3


def test_pagerduty_event() -> None:
    (signal,) = ingest_event(
        {
            "event_action": "trigger",
            "payload": {
                "summary": "Checkout probe failing",
                "component": "web-frontend",
                "severity": "critical",
                "timestamp": "2026-10-08T03:39:00Z",
                "custom_details": {"failures": 3, "nested": {"ignored": True}},
            },
        }
    )
    assert signal.service == "web-frontend" and signal.source == "pagerduty"
    assert signal.attrs["failures"] == 3 and "nested" not in signal.attrs


def test_log_line_and_json_log() -> None:
    line = parse_log_line("2026-10-08T03:35:10Z ERROR checkout-api: NullPointerException in cart")
    assert line.kind is SignalKind.LOG and line.attrs["level"] == "error"
    assert line.summary == "NullPointerException in cart"
    (record,) = ingest_event(
        {"type": "log", "service": "api", "level": "warn", "msg": "slow", "ts": 1_791_430_000}
    )
    assert record.attrs["level"] == "warn" and record.timestamp.tzinfo is UTC


def test_metric_and_change_events() -> None:
    (metric,) = ingest_event(
        {
            "type": "metric",
            "service": "checkout-api",
            "name": "error_rate",
            "value": 0.18,
            "baseline": 0.004,
            "timestamp": "2026-10-08T03:37:00Z",
        }
    )
    assert metric.summary == "error_rate=0.18" and metric.attrs["baseline"] == 0.004
    (deploy,) = ingest_event(
        {
            "type": "deploy",
            "service": "checkout-api",
            "version": "v2.3.1",
            "previous_version": "v2.3.0",
            "timestamp": "2026-10-08T03:32:00Z",
            "by": "ci",
        }
    )
    assert deploy.kind is SignalKind.DEPLOY and deploy.attrs["previous_version"] == "v2.3.0"
    (config,) = ingest_event(
        {"type": "config", "service": "s", "key": "k", "old": 1, "new": 2, "timestamp": 0}
    )
    assert config.kind is SignalKind.CONFIG and "k: 1 -> 2" in config.summary


@pytest.mark.parametrize(
    "payload",
    [
        42,
        {"something": "else"},
        {"alerts": "nope"},
        {"type": "metric", "service": "s", "name": "x", "value": "high", "timestamp": 0},
        {"type": "deploy", "service": "s", "timestamp": 0},
        {"type": "deploy", "version": "v1", "timestamp": 0},
        {"type": "metric", "service": "s", "name": "x", "value": 1, "timestamp": "yesterday"},
        {"type": "metric", "service": "s", "name": "x", "value": 1},
        "not a log line",
        {"type": "log", "service": "s", "line": "x" * 3000},
    ],
)
def test_rejects_malformed_payloads(payload: Any) -> None:
    with pytest.raises(IngestError):
        ingest_event(payload)


def test_size_caps() -> None:
    big = {"type": "metric", "service": "s", "name": "x", "value": 1, "timestamp": 0}
    big["padding"] = "x" * 300_000
    with pytest.raises(IngestError, match="exceeds"):
        ingest_event(big)
    sample = {"type": "metric", "service": "s", "name": "x", "value": 1, "timestamp": 0}
    with pytest.raises(IngestError, match="batch"):
        ingest_batch([sample] * (MAX_BATCH_SIGNALS + 1))


def test_attributes_are_capped_and_truncated() -> None:
    labels = {f"k{i}": "v" * 900 for i in range(80)}
    labels.update({"alertname": "A", "service": "svc"})
    (signal,) = ingest_event({"alerts": [{"labels": labels, "startsAt": "2026-10-08T00:00:00Z"}]})
    assert len(signal.attrs) <= 50
    assert all(len(str(v)) <= 500 for v in signal.attrs.values())


def test_batch_is_time_ordered() -> None:
    late = "2026-10-08T03:40:00Z INFO a: later"
    early = "2026-10-08T03:30:00Z ERROR a: earlier"
    assert [s.summary for s in ingest_batch([late, early])] == ["earlier", "later"]
