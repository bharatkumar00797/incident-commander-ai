from __future__ import annotations

import pytest

from incident_commander.config import env_bool
from incident_commander.engine import run_scenario
from incident_commander.llm import MockProvider
from incident_commander.models import Incident
from incident_commander.scenario import custom_scenario, load_scenario
from incident_commander.topology import ServiceInfo, Topology

RAW = [
    "2026-10-08T03:30:00Z ERROR checkout-api NullPointerException in CartService",
    {
        "type": "deploy",
        "service": "checkout-api",
        "version": "v2",
        "timestamp": "2026-10-08T03:20:00Z",
    },
]


def test_progress_hook_sees_growing_timeline() -> None:
    sizes: list[int] = []

    def hook(incident: Incident) -> None:
        sizes.append(len(incident.timeline))

    run = run_scenario(load_scenario("bad-deploy"), MockProvider(), on_progress=hook)
    assert len(sizes) == len(run.investigation.steps) + 1
    assert sizes == sorted(sizes)
    assert sizes[-1] <= len(run.incident.timeline)


def test_custom_scenario_adds_unknown_services_and_runs() -> None:
    topology = Topology(services={"web": ServiceInfo(depends_on=[], customer_facing=True)})
    scenario = custom_scenario(RAW, topology=topology)
    assert scenario.name == "custom"
    assert set(scenario.topology.services) == {"web", "checkout-api"}
    assert scenario.title.startswith("Incident on checkout-api")
    run = run_scenario(scenario, MockProvider())
    assert run.investigation.status in {"concluded", "max_steps"}
    assert run.incident.signals


def test_custom_scenario_rejects_empty_batch() -> None:
    with pytest.raises(ValueError):
        custom_scenario([])


@pytest.mark.parametrize(
    ("raw", "expected"), [("1", True), ("yes", True), ("off", False), ("", True)]
)
def test_env_bool(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("IC_TEST_FLAG", raw)
    assert env_bool("IC_TEST_FLAG", True) is expected


def test_env_bool_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IC_TEST_FLAG", "maybe")
    with pytest.raises(ValueError):
        env_bool("IC_TEST_FLAG", False)
