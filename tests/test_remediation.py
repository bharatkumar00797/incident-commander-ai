from __future__ import annotations

import pytest

from incident_commander.environment import SimulatedEnvironment
from incident_commander.models import EntryKind, Incident, IncidentStatus, ProposalStatus
from incident_commander.remediation import RemediationError, approve, execute, propose, reject
from incident_commander.scenario import load_scenario


@pytest.fixture
def env() -> SimulatedEnvironment:
    return SimulatedEnvironment(load_scenario("bad-deploy"))


@pytest.fixture
def incident() -> Incident:
    return Incident(title="checkout errors")


@pytest.mark.parametrize(
    ("runbook", "params", "message"),
    [
        ("drop_database", {}, "not on the allowlist"),
        ("rollback_deploy", {"service": "checkout-api"}, "missing required"),
        ("rollback_deploy", {"service": "checkout-api", "to_version": "v9"}, "never deployed"),
        ("rollback_deploy", {"service": "checkout-api", "to_version": "v2.3.1"}, "already runs"),
        ("restart_service", {"service": "ghost"}, "unknown service"),
        ("scale_out", {"service": "checkout-api", "replicas": 500}, "replicas must be"),
        ("scale_out", {"service": "checkout-api", "replicas": "seven"}, "must be int"),
        ("toggle_feature_flag", {"flag": "nope", "enabled": False}, "unknown feature flag"),
        ("failover_dependency", {"dependency": "payments-api"}, "no failover target"),
        ("restart_service", {"service": "checkout-api", "command": "rm -rf /"}, "unknown argument"),
    ],
)
def test_proposals_are_validated(
    env: SimulatedEnvironment,
    incident: Incident,
    runbook: str,
    params: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(RemediationError, match=message):
        propose(incident, env, runbook, params, "because")
    assert incident.proposals == []


def test_execution_requires_human_approval(env: SimulatedEnvironment, incident: Incident) -> None:
    proposal = propose(
        incident, env, "rollback_deploy", {"service": "checkout-api", "to_version": "v2.3.0"}, "x"
    )
    with pytest.raises(RemediationError, match="only approved"):
        execute(incident, env, proposal.id)
    assert env.executed == []

    approve(incident, proposal.id, "bharat")
    with pytest.raises(RemediationError, match="not awaiting"):
        approve(incident, proposal.id, "bharat")
    done = execute(incident, env, proposal.id)
    assert done.status is ProposalStatus.EXECUTED and done.decided_by == "bharat"
    assert env.current_versions["checkout-api"] == "v2.3.0"
    assert incident.status is IncidentStatus.RESOLVED
    kinds = [e.kind for e in incident.timeline]
    assert kinds[:3] == [EntryKind.PROPOSAL, EntryKind.APPROVAL, EntryKind.ACTION]
    with pytest.raises(RemediationError):
        execute(incident, env, proposal.id)  # cannot replay an executed action


def test_rejected_proposals_never_run(env: SimulatedEnvironment, incident: Incident) -> None:
    proposal = propose(incident, env, "restart_service", {"service": "checkout-api"}, "x")
    reject(incident, proposal.id, "bharat", "too risky during peak")
    assert proposal.status is ProposalStatus.REJECTED
    with pytest.raises(RemediationError):
        execute(incident, env, proposal.id)
    other = propose(incident, env, "restart_service", {"service": "web-frontend"}, "x")
    with pytest.raises(RemediationError, match="approver"):
        approve(incident, other.id, "   ")


def test_preconditions_are_rechecked_at_execution(
    env: SimulatedEnvironment, incident: Incident
) -> None:
    params = {"service": "checkout-api", "to_version": "v2.3.0"}
    first = propose(incident, env, "rollback_deploy", params, "x")
    second = propose(incident, env, "rollback_deploy", params, "x")
    for p in (first, second):
        approve(incident, p.id, "bharat")
    execute(incident, env, first.id)
    failed = execute(incident, env, second.id)
    assert failed.status is ProposalStatus.FAILED and "already runs" in (failed.result or "")


def test_wrong_runbook_does_not_resolve(env: SimulatedEnvironment, incident: Incident) -> None:
    proposal = propose(incident, env, "restart_service", {"service": "checkout-api"}, "x")
    approve(incident, proposal.id, "bharat")
    execute(incident, env, proposal.id)
    assert incident.status is IncidentStatus.MITIGATING
    assert "symptoms persist" in (proposal.result or "")
