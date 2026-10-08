from __future__ import annotations

import pytest

from incident_commander.engine import approve_and_execute, run_scenario
from incident_commander.llm import MockProvider
from incident_commander.models import HypothesisStatus, IncidentStatus, ProposalStatus
from incident_commander.postmortem import render_postmortem
from incident_commander.scenario import list_scenarios, load_scenario

SCENARIOS = list_scenarios()


def test_bundled_scenarios() -> None:
    assert {"bad-deploy", "db-connection-exhaustion", "dependency-outage"} <= set(SCENARIOS)
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
