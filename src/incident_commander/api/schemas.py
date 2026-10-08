"""Request and response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from incident_commander.api.jobs import JobState
from incident_commander.api.settings import AccessMode
from incident_commander.ingest import MAX_BATCH_SIGNALS
from incident_commander.models import (
    Hypothesis,
    IncidentStatus,
    RemediationProposal,
    Severity,
    TimelineEntry,
)
from incident_commander.topology import Topology

SIMULATED_NOTE = (
    "Runbooks execute against a simulated environment; no real infrastructure is touched."
)


def _no_control_chars(value: str, field: str) -> str:
    if any(ord(ch) < 32 for ch in value):
        raise ValueError(f"{field} must not contain control characters")
    return value.strip()


class IncidentCreate(BaseModel):
    """Open an incident from a packaged scenario *or* from a raw batch of signals."""

    model_config = ConfigDict(extra="forbid")

    scenario: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9-]{0,63}$",
        description="packaged scenario name (see GET /api/scenarios)",
        examples=["bad-deploy"],
    )
    signals: list[str | dict[str, Any]] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_BATCH_SIGNALS,
        description="raw payloads: Alertmanager/PagerDuty webhooks, log lines, metric samples, "
        "deploy/config events (same parsers as the CLI)",
    )
    topology: Topology | None = Field(
        default=None, description="optional service dependency graph for raw signals"
    )
    title: str | None = Field(default=None, min_length=1, max_length=200)
    provider: Literal["mock", "openai"] | None = None
    max_steps: int | None = Field(default=None, ge=1, le=100)

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str | None) -> str | None:
        return None if value is None else _no_control_chars(value, "title")

    @model_validator(mode="after")
    def _one_source(self) -> IncidentCreate:
        if (self.scenario is None) == (self.signals is None):
            raise ValueError("provide exactly one of 'scenario' or 'signals'")
        if self.scenario is not None and (self.topology is not None or self.title is not None):
            raise ValueError("'topology' and 'title' only apply to raw 'signals'")
        return self


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approver: str = Field(min_length=1, max_length=80, description="name recorded on the audit")
    reason: str = Field(default="", max_length=500)

    @field_validator("approver")
    @classmethod
    def _clean_approver(cls, value: str) -> str:
        value = _no_control_chars(value, "approver")
        if not value:
            raise ValueError("approver must not be blank")
        return value

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: str) -> str:
        return " ".join(value.split())


class IncidentSummaryOut(BaseModel):
    id: str
    state: JobState = Field(description="investigation job state")
    scenario: str
    title: str
    provider: str
    severity: Severity | None = None
    status: IncidentStatus | None = Field(default=None, description="incident lifecycle status")
    services: list[str] = Field(default_factory=list)
    pending_approvals: int = 0
    created_at: datetime
    finished_at: datetime | None = None


class IncidentDetailOut(IncidentSummaryOut):
    simulated: bool = True
    simulated_note: str = SIMULATED_NOTE
    started_at: datetime | None = None
    opened_at: datetime | None = None
    resolved_at: datetime | None = None
    max_steps: int
    summary: str | None = None
    root_cause_id: str | None = None
    investigation_status: str | None = Field(
        default=None, description="concluded | max_steps | failed"
    )
    triage_reasons: list[str] = Field(default_factory=list)
    signal_count: int = 0
    timeline_count: int = 0
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    proposals: list[RemediationProposal] = Field(default_factory=list)
    error: str | None = None


class IncidentCreatedOut(IncidentDetailOut):
    """Response of ``POST /api/incidents``.

    In background mode the investigation is still ``queued``: poll the timeline endpoint. In
    sync (serverless) mode it has finished and ``timeline`` holds every entry, so clients do
    not depend on a follow-up request reaching the same instance.
    """

    timeline: list[TimelineEntry] = Field(default_factory=list)


class IncidentListOut(BaseModel):
    incidents: list[IncidentSummaryOut]


class TimelineOut(BaseModel):
    id: str
    state: JobState
    done: bool
    status: IncidentStatus | None = None
    next_cursor: int = Field(description="pass as ?since= to fetch only newer entries")
    entries: list[TimelineEntry]


class DecisionOut(BaseModel):
    incident_id: str
    proposal: RemediationProposal
    incident_status: IncidentStatus
    simulated: bool = True
    simulated_note: str = SIMULATED_NOTE


class ScenarioOut(BaseModel):
    name: str
    title: str
    description: str
    services: list[str]


class ScenarioListOut(BaseModel):
    scenarios: list[ScenarioOut]


class ConfigOut(BaseModel):
    version: str
    auth_required: bool
    access_mode: AccessMode
    caller_role: Literal["responder", "approver"] | None = Field(
        default=None, description="role of the presented key (null when not authenticated)"
    )
    raw_signals_allowed: bool
    providers: list[str]
    default_provider: str
    default_max_steps: int
    max_steps_cap: int
    sync_runs: bool = Field(description="investigations finish within POST /api/incidents")
    simulated_environment: bool = True
    simulated_note: str = SIMULATED_NOTE
    scenarios: list[str]


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    active_investigations: int
