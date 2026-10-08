"""Blameless postmortem rendered as Markdown from incident state."""

from __future__ import annotations

from incident_commander.models import (
    EntryKind,
    HypothesisStatus,
    Incident,
    ProposalStatus,
)
from incident_commander.topology import Topology

ACTION_ITEMS: dict[str, list[str]] = {
    "bad_deploy": [
        "Gate rollouts of {service} on canary error-rate analysis with automatic rollback.",
        "Add a regression test reproducing the failure fixed by the rollback.",
    ],
    "config_change": [
        "Require review and staged rollout for configuration changes to {service}.",
    ],
    "resource_exhaustion": [
        "Alert on {service} pool/resource saturation before it reaches the hard limit.",
        "Find and fix the resource leak; load-test pool sizing.",
    ],
    "dependency_failure": [
        "Automate failover for {service}; add circuit breakers and tight timeouts in callers.",
        "Agree an SLO with the owners of {service} and alert on its error budget burn.",
    ],
    "traffic_spike": ["Enable autoscaling for {service} with headroom for 3x peak traffic."],
}


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _ts(value: object) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S UTC") if hasattr(value, "strftime") else "-"


def render_postmortem(incident: Incident, topology: Topology | None = None) -> str:
    topology = topology or Topology()
    root = incident.hypothesis(incident.root_cause_id) if incident.root_cause_id else None
    start = incident.opened_at
    end = incident.resolved_at
    duration = f"{(end - start).total_seconds() / 60:.0f} min" if end else "ongoing"
    customer = [s for s in incident.services if topology.is_customer_facing(s)]

    lines = [
        f"# Postmortem: {incident.title}",
        "",
        "> Blameless review: we look at how the system and process allowed this to happen, "
        "not at who made a mistake.",
        "",
        f"- **Incident:** `{incident.id}`",
        f"- **Severity:** {incident.severity}",
        f"- **Status:** {incident.status}",
        f"- **Started:** {_ts(start)}",
        f"- **Resolved:** {_ts(end) if end else 'not yet'} ({duration})",
        f"- **Services involved:** {', '.join(incident.services) or '-'}",
        "",
        "## Summary",
        "",
        incident.summary or "Investigation did not reach a conclusion.",
        "",
        "## Impact",
        "",
        f"- Customer-facing services affected: {', '.join(customer) or 'none identified'}",
        f"- Signals received: {len(incident.signals)}",
        "",
        "## Root cause",
        "",
    ]
    if root is not None:
        lines += [f"**{root.suspected_cause.replace('_', ' ')}** - {root.statement}", ""]
        lines += [f"- Evidence: {_cell(e)}" for e in root.evidence_for] + [""]
    else:
        lines += ["Not determined. Follow-up investigation required.", ""]

    lines += [
        "## Hypotheses considered",
        "",
        "| ID | Cause | Status | Confidence | Evidence |",
        "|----|-------|--------|------------|----------|",
    ]
    for h in incident.hypotheses:
        evidence = "; ".join(h.evidence_for + h.evidence_against) or "not tested"
        lines.append(
            f"| {h.id} | {_cell(h.suspected_cause)} | {h.status} | {h.confidence:.2f} | "
            f"{_cell(evidence)} |"
        )

    lines += ["", "## Remediation", ""]
    if not incident.proposals:
        lines.append("No remediation was proposed.")
    for p in incident.proposals:
        params = ", ".join(f"{k}={v}" for k, v in p.params.items())
        who = f" by {p.decided_by}" if p.decided_by else ""
        lines.append(f"- `{p.runbook_id}({params})` [risk {p.risk}] - **{p.status}**{who}")
        if p.result:
            lines.append(f"  - Result: {_cell(p.result)}")

    lines += [
        "",
        "## Timeline (UTC)",
        "",
        "| Time | Actor | Event | Details |",
        "|------|-------|-------|---------|",
    ]
    for entry in sorted(incident.timeline, key=lambda e: e.ts):
        lines.append(
            f"| {entry.ts.strftime('%H:%M:%S')} | {entry.actor} | {entry.kind} | "
            f"{_cell(entry.text)} |"
        )

    went_well = ["Signals were correlated into a single incident with ranked hypotheses."]
    if root is not None:
        went_well.append("Root cause was confirmed with diagnostic evidence before acting.")
    if any(p.status is ProposalStatus.EXECUTED for p in incident.proposals):
        went_well.append("Remediation ran only after explicit human approval.")
    poorly = []
    if any(h.status is HypothesisStatus.REFUTED for h in incident.hypotheses):
        poorly.append("Some alerts pointed at a misleading cause, costing investigation time.")
    if any(e.kind is EntryKind.SIGNAL for e in incident.timeline):
        poorly.append("The failure reached production before any pre-release check caught it.")

    lines += ["", "## What went well", ""] + [f"- {x}" for x in went_well]
    lines += ["", "## What could be improved", ""] + [f"- {x}" for x in poorly or ["-"]]
    lines += ["", "## Action items", ""]
    service = (root.service or "the service") if root else ""
    items = ACTION_ITEMS.get(root.suspected_cause, []) if root else []
    lines += [f"- [ ] {item.format(service=service)}" for item in items]
    lines.append("- [ ] Review alert thresholds and runbooks used during this incident.")
    return "\n".join(lines) + "\n"
