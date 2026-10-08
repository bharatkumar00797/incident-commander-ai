"""Domain model: signals in, incidents with a timeline, hypotheses and remediation proposals out."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_TEXT = 4000
MAX_ATTRS = 50


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SignalKind(StrEnum):
    ALERT = "alert"
    LOG = "log"
    METRIC = "metric"
    DEPLOY = "deploy"
    CONFIG = "config"


class Severity(StrEnum):
    """SEV1 is the most severe. Ordering helpers keep comparisons explicit."""

    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"
    SEV4 = "SEV4"

    @property
    def rank(self) -> int:
        return int(self.value[-1])

    def is_worse_than(self, other: Severity) -> bool:
        return self.rank < other.rank

    @classmethod
    def worst(cls, *levels: Severity) -> Severity:
        return min(levels, key=lambda level: level.rank)


class IncidentStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    MITIGATING = "mitigating"
    RESOLVED = "resolved"


class Actor(StrEnum):
    AGENT = "agent"
    HUMAN = "human"
    SYSTEM = "system"


class EntryKind(StrEnum):
    SIGNAL = "signal"
    TRIAGE = "triage"
    HYPOTHESIS = "hypothesis"
    DIAGNOSTIC = "diagnostic"
    PROPOSAL = "proposal"
    APPROVAL = "approval"
    ACTION = "action"
    NOTE = "note"


class Signal(Strict):
    """One normalised observation: an alert, log line, metric sample or change event."""

    id: str = Field(default_factory=lambda: new_id("sig"))
    kind: SignalKind
    service: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    timestamp: datetime
    summary: str = Field(min_length=1, max_length=MAX_TEXT)
    severity_hint: Severity | None = None
    attrs: dict[str, str | int | float | bool] = Field(default_factory=dict)
    source: str = Field(default="unknown", max_length=100)

    @field_validator("attrs")
    @classmethod
    def _cap_attrs(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > MAX_ATTRS:
            raise ValueError(f"at most {MAX_ATTRS} attributes per signal")
        return value

    @field_validator("timestamp")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)


class TimelineEntry(Strict):
    ts: datetime = Field(default_factory=utcnow)
    actor: Actor
    kind: EntryKind
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    refs: list[str] = Field(default_factory=list)


class HypothesisStatus(StrEnum):
    OPEN = "open"
    SUPPORTED = "supported"
    REFUTED = "refuted"


class Hypothesis(Strict):
    id: str = Field(default_factory=lambda: new_id("hyp"))
    statement: str = Field(min_length=1, max_length=MAX_TEXT)
    suspected_cause: str = Field(min_length=1, max_length=100)
    service: str | None = Field(default=None, max_length=100)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)
    status: HypothesisStatus = HypothesisStatus.OPEN


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


class RemediationProposal(Strict):
    """A runbook step the agent wants to take. It never runs without a human approval."""

    id: str = Field(default_factory=lambda: new_id("act"))
    runbook_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=MAX_TEXT)
    risk: Risk = Risk.MEDIUM
    hypothesis_id: str | None = Field(default=None, max_length=100)
    status: ProposalStatus = ProposalStatus.PROPOSED
    decided_by: str | None = None
    result: str | None = Field(default=None, max_length=MAX_TEXT)


class Incident(Strict):
    id: str = Field(default_factory=lambda: new_id("inc"))
    title: str = Field(min_length=1, max_length=200)
    severity: Severity = Severity.SEV4
    status: IncidentStatus = IncidentStatus.OPEN
    services: list[str] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    proposals: list[RemediationProposal] = Field(default_factory=list)
    root_cause_id: str | None = None
    summary: str | None = Field(default=None, max_length=MAX_TEXT)
    opened_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None

    def record(
        self,
        actor: Actor,
        kind: EntryKind,
        text: str,
        *refs: str,
        ts: datetime | None = None,
    ) -> TimelineEntry:
        entry = TimelineEntry(
            ts=ts or utcnow(), actor=actor, kind=kind, text=text[:MAX_TEXT], refs=list(refs)
        )
        self.timeline.append(entry)
        return entry

    def add_signal(self, signal: Signal) -> None:
        self.signals.append(signal)
        if signal.service not in self.services:
            self.services.append(signal.service)
        self.record(
            Actor.SYSTEM,
            EntryKind.SIGNAL,
            f"[{signal.kind}] {signal.service}: {signal.summary}",
            signal.id,
            ts=signal.timestamp,
        )

    def hypothesis(self, hypothesis_id: str) -> Hypothesis | None:
        return next((h for h in self.hypotheses if h.id == hypothesis_id), None)

    def proposal(self, proposal_id: str) -> RemediationProposal | None:
        return next((p for p in self.proposals if p.id == proposal_id), None)
