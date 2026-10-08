"""Synthetic incident scenarios: topology, raw signals, simulated telemetry, expected answer."""

from __future__ import annotations

import json
import re
from datetime import datetime
from importlib import resources
from typing import Any

from pydantic import Field, field_validator

from incident_commander.models import Strict
from incident_commander.topology import Topology

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class MetricSeries(Strict):
    unit: str = Field(default="", max_length=20)
    baseline: float
    limit: float | None = None
    start: datetime
    interval_minutes: int = Field(default=5, ge=1, le=60)
    points: list[float] = Field(min_length=1, max_length=500)


class DeployRecord(Strict):
    version: str = Field(max_length=100)
    previous_version: str | None = Field(default=None, max_length=100)
    at: datetime
    by: str = Field(default="unknown", max_length=100)
    commit: str = Field(default="", max_length=64)
    summary: str = Field(default="", max_length=500)


class ConfigChange(Strict):
    key: str = Field(max_length=200)
    old: str = Field(max_length=500)
    new: str = Field(max_length=500)
    at: datetime
    by: str = Field(default="unknown", max_length=100)


class DependencyHealth(Strict):
    status: str = Field(pattern=r"^(healthy|degraded|down)$")
    latency_ms: float = 0.0
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    note: str = Field(default="", max_length=500)


class EnvironmentData(Strict):
    now: datetime
    logs: dict[str, list[str]] = Field(default_factory=dict)
    metrics: dict[str, dict[str, MetricSeries]] = Field(default_factory=dict)
    deploys: dict[str, list[DeployRecord]] = Field(default_factory=dict)
    config_changes: dict[str, list[ConfigChange]] = Field(default_factory=dict)
    dependencies: dict[str, dict[str, DependencyHealth]] = Field(default_factory=dict)
    feature_flags: dict[str, bool] = Field(default_factory=dict)
    replicas: dict[str, int] = Field(default_factory=dict)
    failover_targets: dict[str, str] = Field(default_factory=dict)


class Expected(Strict):
    cause: str
    service: str
    runbook_id: str


class Scenario(Strict):
    name: str
    title: str = Field(max_length=200)
    description: str = Field(max_length=2000)
    topology: Topology
    events: list[Any] = Field(min_length=1, max_length=500)
    environment: EnvironmentData
    expected: Expected

    @field_validator("name")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not _NAME_RE.match(value):
            raise ValueError("scenario name must be a lowercase slug")
        return value


def _scenario_files() -> dict[str, Any]:
    root = resources.files("incident_commander").joinpath("scenarios")
    return {
        entry.name.removesuffix(".json"): entry
        for entry in root.iterdir()
        if entry.name.endswith(".json")
    }


def list_scenarios() -> list[str]:
    return sorted(_scenario_files())


def load_scenario(name: str) -> Scenario:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid scenario name {name!r}")
    files = _scenario_files()
    if name not in files:
        raise KeyError(f"unknown scenario {name!r}; available: {', '.join(sorted(files))}")
    return Scenario.model_validate(json.loads(files[name].read_text(encoding="utf-8")))
