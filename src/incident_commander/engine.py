"""End-to-end incident pipeline: ingest -> triage -> correlate -> investigate -> propose -> act."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from incident_commander.agent.loop import InvestigationResult, Investigator, Step, seed_hypotheses
from incident_commander.correlate import Correlation, correlate
from incident_commander.environment import SimulatedEnvironment
from incident_commander.ingest import ingest_batch
from incident_commander.llm.base import LLMProvider
from incident_commander.models import Actor, EntryKind, Incident
from incident_commander.postmortem import render_postmortem
from incident_commander.remediation import approve, execute
from incident_commander.scenario import Scenario
from incident_commander.tools import build_diagnostic_registry
from incident_commander.triage import TriageResult, triage


class SimClock:
    """Deterministic replay clock: starts after the last signal and ticks on every call."""

    def __init__(self, start: datetime, tick: timedelta = timedelta(seconds=20)) -> None:
        self._now = start
        self._tick = tick

    def __call__(self) -> datetime:
        self._now += self._tick
        return self._now


@dataclass
class IncidentRun:
    scenario: Scenario
    incident: Incident
    env: SimulatedEnvironment
    triage: TriageResult
    correlation: Correlation
    investigation: InvestigationResult
    clock: SimClock

    @property
    def postmortem(self) -> str:
        return render_postmortem(self.incident, self.scenario.topology)

    @property
    def correct_root_cause(self) -> bool:
        root = self.incident.hypothesis(self.incident.root_cause_id or "")
        expected = self.scenario.expected
        return bool(
            root and root.suspected_cause == expected.cause and root.service == expected.service
        )


def run_scenario(
    scenario: Scenario,
    provider: LLMProvider,
    *,
    max_steps: int = 20,
    on_step: Callable[[Step], None] | None = None,
    on_progress: Callable[[Incident], None] | None = None,
) -> IncidentRun:
    """Replay ``scenario`` end to end.

    ``on_progress`` is called with the live incident once triage and correlation are done and
    again after every investigation step, so callers (the HTTP service) can publish snapshots.
    """
    signals = ingest_batch(scenario.events)
    correlation = correlate(signals, scenario.topology)
    related = [s for s in signals if s.id in set(correlation.signal_ids)]
    incident = Incident(
        title=scenario.title, opened_at=correlation.window_start or signals[0].timestamp
    )
    for signal in related:
        incident.add_signal(signal)
    clock = SimClock(max(scenario.environment.now, signals[-1].timestamp))

    result = triage(related, scenario.topology)
    incident.severity = result.severity
    incident.record(
        Actor.SYSTEM,
        EntryKind.TRIAGE,
        f"triaged {result.severity} (rule floor): {result.reasons[-1]}",
        ts=clock(),
    )
    incident.record(
        Actor.SYSTEM,
        EntryKind.NOTE,
        f"correlated {len(correlation.signal_ids)} signals across "
        f"{', '.join(correlation.services) or 'no services'}; "
        f"{len(correlation.unrelated_ids)} unrelated signal(s) set aside",
        ts=clock(),
    )
    seed_hypotheses(incident, correlation, clock())
    if on_progress is not None:
        on_progress(incident)

    def step_hook(step: Step) -> None:
        if on_step is not None:
            on_step(step)
        if on_progress is not None:
            on_progress(incident)

    env = SimulatedEnvironment(scenario)
    tools = build_diagnostic_registry(env, correlation.window_start)
    investigation = Investigator(
        provider, tools, incident, env, max_steps=max_steps, clock=clock, on_step=step_hook
    ).run(result, correlation)
    return IncidentRun(scenario, incident, env, result, correlation, investigation, clock)


def approve_and_execute(run: IncidentRun, approver: str) -> list[str]:
    """Human-in-the-loop step: approve every pending proposal and run it in the simulator."""
    outcomes = []
    for proposal in list(run.incident.proposals):
        if proposal.status != "proposed":
            continue
        approve(run.incident, proposal.id, approver, ts=run.clock())
        done = execute(run.incident, run.env, proposal.id, ts=run.clock())
        outcomes.append(f"{done.runbook_id}: {done.status} - {done.result}")
    return outcomes
