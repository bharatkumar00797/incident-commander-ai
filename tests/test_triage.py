from __future__ import annotations

import pytest

from incident_commander.models import Severity
from incident_commander.topology import Topology
from incident_commander.triage import apply_model_severity, triage

from .conftest import make_signal


def metric(service: str, name: str, value: float, baseline: float = 0.01) -> object:
    return make_signal(
        kind="metric",
        service=service,
        summary=f"{name}={value}",
        attrs={"name": name, "value": value, "baseline": baseline},
    )


def test_defaults_to_sev4_without_symptoms() -> None:
    result = triage([make_signal(kind="deploy", summary="deployed v2")])
    assert result.severity is Severity.SEV4 and result.symptomatic_services == []


@pytest.mark.parametrize(
    ("rate", "expected"),
    [(0.3, Severity.SEV1), (0.06, Severity.SEV2), (0.02, Severity.SEV3)],
)
def test_error_rate_thresholds(rate: float, expected: Severity) -> None:
    assert triage([metric("api", "error_rate", rate, 0.001)]).severity is expected


def test_alert_hint_and_customer_facing_floor() -> None:
    topo = Topology.model_validate({"services": {"web": {"customer_facing": True}}})
    warning = make_signal(service="web", severity_hint="SEV3")
    result = triage([warning], topo)
    assert result.severity is Severity.SEV2
    assert any("customer-facing" in r for r in result.reasons)


def test_resolved_alerts_are_not_symptoms() -> None:
    resolved = make_signal(severity_hint="SEV1", attrs={"status": "resolved"})
    assert triage([resolved]).severity is Severity.SEV4


def test_blast_radius() -> None:
    logs = [
        make_signal(kind="log", service=f"svc{i}", summary="boom", attrs={"level": "error"})
        for i in range(5)
    ]
    assert triage(logs[:3]).severity is Severity.SEV2
    assert triage(logs).severity is Severity.SEV1


def test_model_can_escalate_but_never_downgrade() -> None:
    up = apply_model_severity(Severity.SEV3, Severity.SEV1, "payments down")
    assert up.accepted and up.severity is Severity.SEV1
    down = apply_model_severity(Severity.SEV1, Severity.SEV3, "looks minor")
    assert not down.accepted and down.severity is Severity.SEV1
    assert "refused downgrade" in down.note
