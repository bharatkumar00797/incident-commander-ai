"""Correlate signals into one incident and derive ranked candidate causes.

Heuristics an experienced on-call engineer applies first:

* a deploy or config change shortly before the first symptom on the same service (or on a
  service it serves) is the prime suspect;
* "too many connections" / "pool exhausted" / OOM text points at resource exhaustion;
* timeouts on several services that share a dependency point at that dependency;
* a request-rate surge points at a traffic spike.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from incident_commander.models import Signal, SignalKind
from incident_commander.topology import Topology
from incident_commander.triage import is_symptom, metric_ratio

CHANGE_LOOKBACK = timedelta(minutes=60)
SUSPECT_GAP = timedelta(minutes=30)

_EXHAUSTION_RE = re.compile(
    r"too many connections|pool exhausted|connection pool|out of memory|\boom\b|"
    r"no space left|max_connections|resource exhausted",
    re.IGNORECASE,
)
_DEPENDENCY_RE = re.compile(
    r"timed? ?out|timeout|connection refused|unavailable|upstream|503 from|deadline exceeded",
    re.IGNORECASE,
)


@dataclass
class CandidateCause:
    cause: str
    service: str
    score: float
    rationale: str
    refs: list[str] = field(default_factory=list)
    details: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.cause, self.service)


@dataclass
class Correlation:
    window_start: datetime | None
    window_end: datetime | None
    services: list[str]
    signal_ids: list[str]
    unrelated_ids: list[str]
    candidates: list[CandidateCause]

    @property
    def first_symptom_at(self) -> datetime | None:
        return self.window_start


def _impacted_by(topology: Topology, changed: str, symptom_service: str) -> bool:
    return symptom_service == changed or symptom_service in topology.upstream_of(changed)


def correlate(signals: Sequence[Signal], topology: Topology | None = None) -> Correlation:
    topology = topology or Topology()
    ordered = sorted(signals, key=lambda s: s.timestamp)
    symptoms = [s for s in ordered if is_symptom(s)]
    if not symptoms:
        return Correlation(None, None, [], [], [s.id for s in ordered], [])

    start, end = symptoms[0].timestamp, symptoms[-1].timestamp
    impaired = {s.service for s in symptoms}

    def in_scope(signal: Signal) -> bool:
        if not start - CHANGE_LOOKBACK <= signal.timestamp <= end:
            return False
        return any(topology.related(signal.service, svc) for svc in impaired)

    related = [s for s in ordered if in_scope(s)]
    unrelated = [s.id for s in ordered if not in_scope(s)]
    candidates: dict[tuple[str, str], CandidateCause] = {}

    def add(candidate: CandidateCause) -> None:
        current = candidates.get(candidate.key)
        if current is None or candidate.score > current.score:
            candidates[candidate.key] = candidate

    for change in (s for s in related if s.kind in {SignalKind.DEPLOY, SignalKind.CONFIG}):
        after = [
            s
            for s in symptoms
            if timedelta(0) <= s.timestamp - change.timestamp <= SUSPECT_GAP
            and _impacted_by(topology, change.service, s.service)
        ]
        if not after:
            continue
        gap_min = (after[0].timestamp - change.timestamp).total_seconds() / 60
        is_deploy = change.kind is SignalKind.DEPLOY
        base = 0.9 if is_deploy else 0.8
        details = {k: str(v) for k, v in change.attrs.items() if k in {"version", "key"}}
        if is_deploy and change.attrs.get("previous_version"):
            details["previous_version"] = str(change.attrs["previous_version"])
        add(
            CandidateCause(
                cause="bad_deploy" if is_deploy else "config_change",
                service=change.service,
                score=round(max(0.5, base - 0.01 * gap_min), 2),
                rationale=(
                    f"{change.summary} {gap_min:.0f} min before the first symptom "
                    f"({after[0].service}: {after[0].summary})"
                ),
                refs=[change.id, after[0].id],
                details=details,
            )
        )

    for symptom in symptoms:
        if _EXHAUSTION_RE.search(symptom.summary):
            add(
                CandidateCause(
                    cause="resource_exhaustion",
                    service=symptom.service,
                    score=0.75,
                    rationale=f"exhaustion pattern on {symptom.service}: {symptom.summary}",
                    refs=[symptom.id],
                )
            )

    timeouts = [s for s in symptoms if _DEPENDENCY_RE.search(s.summary)]
    for dependency in topology.services:
        callers = sorted(
            {s.service for s in timeouts if dependency in topology.dependencies(s.service)}
        )
        if not callers:
            continue
        mentions = [s for s in timeouts if dependency in s.summary]
        if len(callers) < 2 and not mentions:
            continue
        add(
            CandidateCause(
                cause="dependency_failure",
                service=dependency,
                score=round(min(0.9, 0.55 + 0.1 * len(callers) + (0.1 if mentions else 0)), 2),
                rationale=f"timeouts from {', '.join(callers)} which all depend on {dependency}",
                refs=[s.id for s in timeouts if s.service in callers][:5],
                details={"callers": ",".join(callers)},
            )
        )

    for symptom in symptoms:
        if symptom.kind is SignalKind.METRIC and symptom.attrs.get("name") == "request_rate":
            ratio = metric_ratio(symptom)
            if ratio >= 3:
                add(
                    CandidateCause(
                        cause="traffic_spike",
                        service=symptom.service,
                        score=0.6,
                        rationale=f"request rate {ratio:.1f}x baseline on {symptom.service}",
                        refs=[symptom.id],
                    )
                )

    ranked = sorted(candidates.values(), key=lambda c: (-c.score, c.cause, c.service))
    return Correlation(
        window_start=start,
        window_end=end,
        services=sorted({s.service for s in related}),
        signal_ids=[s.id for s in related],
        unrelated_ids=unrelated,
        candidates=ranked,
    )
