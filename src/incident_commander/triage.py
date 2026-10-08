"""Deterministic severity triage.

Rules set a severity *floor*. A model may argue for a worse severity, but it can never silently
downgrade what the rules decided: a SEV1 page stays a SEV1 until a human says otherwise.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from incident_commander.models import Severity, Signal, SignalKind
from incident_commander.topology import Topology

ERROR_RATE_THRESHOLDS: tuple[tuple[float, Severity], ...] = (
    (0.25, Severity.SEV1),
    (0.05, Severity.SEV2),
    (0.01, Severity.SEV3),
)
LATENCY_RATIO_SEV3 = 2.0
WIDE_BLAST_RADIUS_SEV2 = 3
WIDE_BLAST_RADIUS_SEV1 = 5


@dataclass
class TriageResult:
    severity: Severity
    reasons: list[str] = field(default_factory=list)
    symptomatic_services: list[str] = field(default_factory=list)

    def raise_to(self, level: Severity, reason: str) -> None:
        if level.is_worse_than(self.severity):
            self.severity = level
        self.reasons.append(f"{level}: {reason}")


def is_symptom(signal: Signal) -> bool:
    """Alerts, error logs and anomalous metrics are symptoms; deploys/configs are changes."""
    if signal.kind is SignalKind.ALERT:
        return str(signal.attrs.get("status", "firing")) != "resolved"
    if signal.kind is SignalKind.LOG:
        return signal.attrs.get("level") in {"error", "err", "critical", "fatal"}
    if signal.kind is SignalKind.METRIC:
        return metric_ratio(signal) >= LATENCY_RATIO_SEV3 or _metric_over_threshold(signal)
    return False


def metric_ratio(signal: Signal) -> float:
    value, baseline = signal.attrs.get("value"), signal.attrs.get("baseline")
    if not isinstance(value, int | float) or not isinstance(baseline, int | float):
        return 1.0
    if baseline <= 0:
        return float("inf") if value > 0 else 1.0
    return float(value) / float(baseline)


def _metric_over_threshold(signal: Signal) -> bool:
    value, threshold = signal.attrs.get("value"), signal.attrs.get("threshold")
    return (
        isinstance(value, int | float)
        and isinstance(threshold, int | float)
        and float(value) >= float(threshold)
    )


def triage(signals: Sequence[Signal], topology: Topology | None = None) -> TriageResult:
    topology = topology or Topology()
    result = TriageResult(severity=Severity.SEV4, reasons=["SEV4: default for any open incident"])
    symptomatic: list[str] = []

    for signal in signals:
        if not is_symptom(signal):
            continue
        if signal.service not in symptomatic:
            symptomatic.append(signal.service)
        if signal.kind is SignalKind.ALERT and signal.severity_hint is not None:
            result.raise_to(signal.severity_hint, f"alert on {signal.service}: {signal.summary}")
        if signal.kind is SignalKind.METRIC and signal.attrs.get("name") == "error_rate":
            rate = signal.attrs.get("value")
            if isinstance(rate, int | float):
                for threshold, level in ERROR_RATE_THRESHOLDS:
                    if rate >= threshold:
                        result.raise_to(
                            level, f"{signal.service} error rate {rate:.1%} >= {threshold:.0%}"
                        )
                        break
        if signal.kind is SignalKind.METRIC and "latency" in str(signal.attrs.get("name")):
            result.raise_to(
                Severity.SEV3,
                f"{signal.service} {signal.attrs.get('name')} {metric_ratio(signal):.1f}x baseline",
            )

    for service in symptomatic:
        if topology.is_customer_facing(service):
            result.raise_to(Severity.SEV2, f"customer-facing service {service} is impaired")
            break

    if len(symptomatic) >= WIDE_BLAST_RADIUS_SEV1:
        result.raise_to(Severity.SEV1, f"{len(symptomatic)} services impaired")
    elif len(symptomatic) >= WIDE_BLAST_RADIUS_SEV2:
        result.raise_to(Severity.SEV2, f"{len(symptomatic)} services impaired")

    result.symptomatic_services = symptomatic
    return result


@dataclass(frozen=True)
class SeverityDecision:
    severity: Severity
    accepted: bool
    note: str


def apply_model_severity(floor: Severity, proposed: Severity, reason: str) -> SeverityDecision:
    """Let the model escalate, never downgrade below the rule-based floor."""
    if proposed.is_worse_than(floor):
        return SeverityDecision(proposed, True, f"escalated {floor} -> {proposed}: {reason}")
    if proposed is floor:
        return SeverityDecision(floor, True, f"confirmed {floor}: {reason}")
    return SeverityDecision(
        floor, False, f"refused downgrade {floor} -> {proposed}; rules require at least {floor}"
    )
