"""Runbook allowlist, proposal validation and the human approval gate.

The agent can only *propose* one of the runbooks below with parameters that pass validation and
preconditions. Nothing runs until a human approves it, and execution is re-validated and audited
in the incident timeline. In this project the executor targets the simulated environment only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from incident_commander.environment import SimulatedEnvironment
from incident_commander.models import (
    Actor,
    EntryKind,
    Incident,
    IncidentStatus,
    ProposalStatus,
    RemediationProposal,
    Risk,
)
from incident_commander.tools import Tool, ToolArgumentError

Precondition = Callable[[SimulatedEnvironment, dict[str, Any]], str | None]


class RemediationError(ValueError):
    """A proposal or execution request was rejected by the guardrails."""


@dataclass(frozen=True)
class Runbook:
    id: str
    description: str
    params: Mapping[str, tuple[type, bool, str]]
    risk: Risk
    precondition: Precondition

    def validate(self, env: SimulatedEnvironment, raw: Mapping[str, Any]) -> dict[str, Any]:
        tool = Tool(self.id, self.description, self.params, lambda a: a)
        try:
            params = tool.validate(raw)
        except ToolArgumentError as exc:
            raise RemediationError(str(exc)) from exc
        problem = self.precondition(env, params)
        if problem:
            raise RemediationError(f"precondition failed for {self.id}: {problem}")
        return params

    def signature(self) -> str:
        args = ", ".join(f"{n}: {t.__name__}" for n, (t, _, _) in self.params.items())
        return f"- {self.id}({args}) [risk: {self.risk}]: {self.description}"


def _known_service(env: SimulatedEnvironment, p: dict[str, Any]) -> str | None:
    service = p.get("service")
    return None if service in env.scenario.topology else f"unknown service {service!r}"


def _rollback_ok(env: SimulatedEnvironment, p: dict[str, Any]) -> str | None:
    if problem := _known_service(env, p):
        return problem
    history = [d.version for d in env.scenario.environment.deploys.get(p["service"], [])]
    current = env.current_versions.get(p["service"])
    if p["to_version"] not in history:
        return f"{p['to_version']!r} was never deployed to {p['service']}"
    if p["to_version"] == current:
        return f"{p['service']} already runs {current}"
    return None


def _scale_ok(env: SimulatedEnvironment, p: dict[str, Any]) -> str | None:
    if problem := _known_service(env, p):
        return problem
    current = env.replicas.get(p["service"], 0)
    if not current < p["replicas"] <= max(current * 4, 4) or p["replicas"] > 50:
        return f"replicas must be > {current}, at most 4x current and <= 50"
    return None


def _flag_ok(env: SimulatedEnvironment, p: dict[str, Any]) -> str | None:
    if p["flag"] not in env.feature_flags:
        return f"unknown feature flag {p['flag']!r}"
    if env.feature_flags[p["flag"]] == p["enabled"]:
        return f"flag already {p['enabled']}"
    return None


def _failover_ok(env: SimulatedEnvironment, p: dict[str, Any]) -> str | None:
    dep = p["dependency"]
    if dep not in env.scenario.topology:
        return f"unknown dependency {dep!r}"
    if dep not in env.scenario.environment.failover_targets:
        return f"no failover target configured for {dep}"
    if dep in env.failovers:
        return f"{dep} is already failed over"
    return None


RUNBOOKS: dict[str, Runbook] = {
    rb.id: rb
    for rb in (
        Runbook(
            "rollback_deploy",
            "Roll a service back to a previously deployed version.",
            {
                "service": (str, True, "service to roll back"),
                "to_version": (str, True, "previously deployed version"),
            },
            Risk.MEDIUM,
            _rollback_ok,
        ),
        Runbook(
            "restart_service",
            "Rolling restart of a service (recycles leaked connections/memory).",
            {"service": (str, True, "service to restart")},
            Risk.LOW,
            _known_service,
        ),
        Runbook(
            "scale_out",
            "Increase replica count of a service (bounded to 4x, max 50).",
            {"service": (str, True, "service"), "replicas": (int, True, "new replica count")},
            Risk.LOW,
            _scale_ok,
        ),
        Runbook(
            "toggle_feature_flag",
            "Enable or disable an existing feature flag.",
            {"flag": (str, True, "flag name"), "enabled": (bool, True, "new state")},
            Risk.LOW,
            _flag_ok,
        ),
        Runbook(
            "failover_dependency",
            "Switch traffic for a dependency to its configured standby/secondary.",
            {"dependency": (str, True, "dependency service with a failover target")},
            Risk.HIGH,
            _failover_ok,
        ),
    )
}


def describe_runbooks() -> str:
    return "\n".join(rb.signature() for rb in RUNBOOKS.values())


def propose(
    incident: Incident,
    env: SimulatedEnvironment,
    runbook_id: str,
    params: Mapping[str, Any],
    rationale: str,
    *,
    hypothesis_id: str | None = None,
    ts: datetime | None = None,
) -> RemediationProposal:
    runbook = RUNBOOKS.get(runbook_id)
    if runbook is None:
        raise RemediationError(
            f"runbook {runbook_id!r} is not on the allowlist: {', '.join(RUNBOOKS)}"
        )
    if hypothesis_id is not None and incident.hypothesis(hypothesis_id) is None:
        raise RemediationError(f"unknown hypothesis {hypothesis_id!r}")
    clean = runbook.validate(env, params)
    proposal = RemediationProposal(
        runbook_id=runbook.id,
        params=clean,
        rationale=rationale or runbook.description,
        risk=runbook.risk,
        hypothesis_id=hypothesis_id,
    )
    incident.proposals.append(proposal)
    incident.record(
        Actor.AGENT,
        EntryKind.PROPOSAL,
        f"proposed {runbook.id}({_fmt(clean)}) [risk {runbook.risk}] - awaiting human approval",
        proposal.id,
        ts=ts,
    )
    return proposal


def _fmt(params: Mapping[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in params.items())


def _pending(incident: Incident, proposal_id: str) -> RemediationProposal:
    proposal = incident.proposal(proposal_id)
    if proposal is None:
        raise RemediationError(f"unknown proposal {proposal_id!r}")
    return proposal


def _approver(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 100:
        raise RemediationError("an approver name (1-100 chars) is required")
    return name


def approve(
    incident: Incident, proposal_id: str, approver: str, *, ts: datetime | None = None
) -> RemediationProposal:
    proposal = _pending(incident, proposal_id)
    if proposal.status is not ProposalStatus.PROPOSED:
        raise RemediationError(f"proposal is {proposal.status}, not awaiting approval")
    proposal.status = ProposalStatus.APPROVED
    proposal.decided_by = _approver(approver)
    incident.record(
        Actor.HUMAN,
        EntryKind.APPROVAL,
        f"{proposal.decided_by} approved {proposal.runbook_id}",
        proposal.id,
        ts=ts,
    )
    return proposal


def reject(
    incident: Incident,
    proposal_id: str,
    approver: str,
    reason: str = "",
    *,
    ts: datetime | None = None,
) -> RemediationProposal:
    proposal = _pending(incident, proposal_id)
    if proposal.status is not ProposalStatus.PROPOSED:
        raise RemediationError(f"proposal is {proposal.status}, not awaiting approval")
    proposal.status = ProposalStatus.REJECTED
    proposal.decided_by = _approver(approver)
    note = f": {reason[:500]}" if reason else ""
    incident.record(
        Actor.HUMAN,
        EntryKind.APPROVAL,
        f"{proposal.decided_by} rejected {proposal.runbook_id}{note}",
        proposal.id,
        ts=ts,
    )
    return proposal


def execute(
    incident: Incident,
    env: SimulatedEnvironment,
    proposal_id: str,
    *,
    ts: datetime | None = None,
) -> RemediationProposal:
    """Run an APPROVED proposal against the simulated environment, re-checking preconditions."""
    proposal = _pending(incident, proposal_id)
    if proposal.status is not ProposalStatus.APPROVED:
        raise RemediationError(f"proposal is {proposal.status}; only approved actions can run")
    runbook = RUNBOOKS[proposal.runbook_id]
    incident.status = IncidentStatus.MITIGATING
    try:
        params = runbook.validate(env, proposal.params)
        message = env.apply_runbook(runbook.id, params)
    except RemediationError as exc:
        proposal.status = ProposalStatus.FAILED
        proposal.result = str(exc)
        incident.record(
            Actor.SYSTEM, EntryKind.ACTION, f"{runbook.id} failed: {exc}", proposal.id, ts=ts
        )
        return proposal
    proposal.status = ProposalStatus.EXECUTED
    mitigated = env.is_mitigated()
    proposal.result = message + ("; health checks recovered" if mitigated else "; symptoms persist")
    incident.record(
        Actor.SYSTEM,
        EntryKind.ACTION,
        f"executed {runbook.id} (simulated): {proposal.result}",
        proposal.id,
        ts=ts,
    )
    if mitigated:
        incident.status = IncidentStatus.RESOLVED
        incident.resolved_at = ts or incident.timeline[-1].ts
        incident.record(
            Actor.SYSTEM, EntryKind.NOTE, "incident resolved: metrics back to baseline", ts=ts
        )
    return proposal
