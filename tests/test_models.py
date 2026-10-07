from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from incident_commander.models import (
    Actor,
    EntryKind,
    Incident,
    RemediationProposal,
    Severity,
    Signal,
    SignalKind,
)

IST = timezone(timedelta(hours=5, minutes=30))


def make_signal(**overrides: object) -> Signal:
    data: dict[str, object] = {
        "kind": "alert",
        "service": "checkout-api",
        "timestamp": datetime(2026, 10, 7, 10, 0, tzinfo=UTC),
        "summary": "5xx rate above 5% for 5m",
    }
    data.update(overrides)
    return Signal.model_validate(data)


def test_severity_ordering() -> None:
    assert Severity.SEV1.is_worse_than(Severity.SEV2)
    assert not Severity.SEV3.is_worse_than(Severity.SEV3)
    assert Severity.worst(Severity.SEV3, Severity.SEV1, Severity.SEV4) is Severity.SEV1


def test_signal_timestamps_are_normalised_to_utc() -> None:
    signal = make_signal(timestamp=datetime(2026, 10, 7, 15, 30, tzinfo=IST))
    assert signal.timestamp == datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
    assert signal.kind is SignalKind.ALERT


@pytest.mark.parametrize(
    "overrides",
    [
        {"timestamp": datetime(2026, 10, 7, 10, 0)},
        {"service": "checkout api; rm -rf /"},
        {"summary": ""},
        {"kind": "email"},
        {"unexpected": "field"},
        {"attrs": {f"k{i}": i for i in range(51)}},
    ],
)
def test_signal_rejects_invalid_input(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_signal(**overrides)


def test_incident_tracks_services_and_timeline() -> None:
    incident = Incident(title="Checkout errors")
    incident.add_signal(make_signal())
    incident.add_signal(make_signal(kind="deploy", summary="checkout-api v2.3.1 deployed"))
    incident.add_signal(make_signal(service="payments", kind="log", summary="pool exhausted"))
    incident.record(Actor.AGENT, EntryKind.NOTE, "Investigating recent deploy")

    assert incident.services == ["checkout-api", "payments"]
    assert [entry.kind for entry in incident.timeline] == [
        EntryKind.SIGNAL,
        EntryKind.SIGNAL,
        EntryKind.SIGNAL,
        EntryKind.NOTE,
    ]
    assert incident.timeline[0].refs == [incident.signals[0].id]


def test_remediation_requires_a_runbook_identifier() -> None:
    proposal = RemediationProposal(runbook_id="rollback_deploy", rationale="bad deploy")
    assert proposal.status == "proposed"
    with pytest.raises(ValidationError):
        RemediationProposal(runbook_id="rm -rf", rationale="nope")
