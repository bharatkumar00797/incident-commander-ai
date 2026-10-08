from __future__ import annotations

import pytest

from incident_commander.engine import approve_and_execute, run_scenario
from incident_commander.llm import MockProvider
from incident_commander.models import HypothesisStatus, IncidentStatus, ProposalStatus
from incident_commander.postmortem import render_postmortem
from incident_commander.scenario import list_scenarios, load_scenario

SCENARIOS = list_scenarios()


def test_bundled_scenarios() -> None:
    assert {
        "bad-deploy",
        "bad-config-push",
        "db-connection-exhaustion",
        "dependency-outage",
        "memory-leak",
        "traffic-spike",
    } <= set(SCENARIOS)
    with pytest.raises(KeyError):
        load_scenario("nope")
    with pytest.raises(ValueError):
        load_scenario("../etc/passwd")


@pytest.mark.parametrize("name", SCENARIOS)
def test_mock_commander_finds_the_expected_root_cause(name: str) -> None:
    run = run_scenario(load_scenario(name), MockProvider())
    expected = run.scenario.expected
    assert run.investigation.status == "concluded"
    assert run.correct_root_cause, run.investigation.summary
    (proposal,) = run.incident.proposals
    assert proposal.runbook_id == expected.runbook_id
    assert proposal.status is ProposalStatus.PROPOSED  # nothing runs without approval
    assert run.env.executed == []

    outcomes = approve_and_execute(run, "on-call")
    assert len(outcomes) == 1 and run.incident.status is IncidentStatus.RESOLVED
    assert run.env.is_mitigated()


def test_red_herring_is_refuted_before_the_real_cause() -> None:
    run = run_scenario(load_scenario("db-connection-exhaustion"), MockProvider())
    h1, h2 = run.incident.hypotheses[:2]
    assert (h1.suspected_cause, h1.status) == ("bad_deploy", HypothesisStatus.REFUTED)
    assert (h2.suspected_cause, h2.status) == ("resource_exhaustion", HypothesisStatus.SUPPORTED)


def test_triage_floors() -> None:
    sev = {n: run_scenario(load_scenario(n), MockProvider()).incident.severity for n in SCENARIOS}
    assert sev["bad-deploy"] == "SEV2" and sev["dependency-outage"] == "SEV1"


def test_unrelated_signals_are_left_out_of_the_incident() -> None:
    run = run_scenario(load_scenario("bad-deploy"), MockProvider())
    assert "payments-api" not in run.incident.services  # deploy hours earlier, out of window


def test_postmortem_is_complete_and_escapes_tables() -> None:
    run = run_scenario(load_scenario("bad-deploy"), MockProvider())
    approve_and_execute(run, "bharat")
    text = render_postmortem(run.incident, run.scenario.topology)
    for heading in ("## Summary", "## Root cause", "## Timeline (UTC)", "## Action items"):
        assert heading in text
    assert "Blameless" in text and "rollback_deploy" in text and "by bharat" in text
    assert "canary" in text and "checkout-api" in text
    assert "exception\\|panic" in text  # pipes in evidence cannot break the table


RED_HERRINGS = {
    "memory-leak": ("config_change", "resource_exhaustion"),
    "bad-config-push": ("bad_deploy", "config_change"),
    "traffic-spike": ("bad_deploy", "traffic_spike"),
}


@pytest.mark.parametrize("name", sorted(RED_HERRINGS))
def test_recent_change_red_herring_is_refuted(name: str) -> None:
    herring, real = RED_HERRINGS[name]
    run = run_scenario(load_scenario(name), MockProvider())
    h1, h2 = run.incident.hypotheses[:2]
    assert (h1.suspected_cause, h1.status) == (herring, HypothesisStatus.REFUTED)
    assert (h2.suspected_cause, h2.status) == (real, HypothesisStatus.SUPPORTED)
    assert run.incident.root_cause_id == h2.id


def test_memory_leak_checks_memory_not_connections() -> None:
    run = run_scenario(load_scenario("memory-leak"), MockProvider())
    metrics = [s.args.get("name") for s in run.investigation.steps if s.tool == "get_metric"]
    assert "memory_usage" in metrics and "db_connections" not in metrics
    (proposal,) = run.incident.proposals
    assert proposal.params == {"service": "search-api"}


def test_bad_config_push_switches_the_flag_back_off() -> None:
    run = run_scenario(load_scenario("bad-config-push"), MockProvider())
    (proposal,) = run.incident.proposals
    assert proposal.runbook_id == "toggle_feature_flag"
    assert proposal.params == {"flag": "pricing-api.dynamic_discounts_v2", "enabled": False}
    approve_and_execute(run, "on-call")
    assert run.env.feature_flags["pricing-api.dynamic_discounts_v2"] is False
    assert run.incident.status is IncidentStatus.RESOLVED


def test_traffic_spike_doubles_capacity() -> None:
    run = run_scenario(load_scenario("traffic-spike"), MockProvider())
    (proposal,) = run.incident.proposals
    assert proposal.params == {"service": "ticketing-api", "replicas": 8}
    approve_and_execute(run, "on-call")
    assert run.env.replicas["ticketing-api"] == 8
