"""A simulated production environment backing the diagnostic tools and runbook executor.

Diagnostics only *read* from it. The only mutating entry point is :meth:`apply_runbook`, which is
reachable solely through the approval-gated executor in :mod:`incident_commander.remediation`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from incident_commander.ingest import IngestError, parse_log_line
from incident_commander.scenario import Scenario

MAX_LOG_RESULTS = 20


@dataclass
class ExecutionRecord:
    runbook_id: str
    params: dict[str, Any]
    message: str


@dataclass
class SimulatedEnvironment:
    scenario: Scenario
    current_versions: dict[str, str] = field(default_factory=dict)
    replicas: dict[str, int] = field(default_factory=dict)
    feature_flags: dict[str, bool] = field(default_factory=dict)
    failovers: dict[str, str] = field(default_factory=dict)
    executed: list[ExecutionRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        data = self.scenario.environment
        for service, deploys in data.deploys.items():
            if deploys:
                self.current_versions[service] = max(deploys, key=lambda d: d.at).version
        self.replicas = {name: data.replicas.get(name, 2) for name in self.services}
        self.feature_flags = dict(data.feature_flags)

    @property
    def services(self) -> list[str]:
        return list(self.scenario.topology.services)

    @property
    def now(self) -> datetime:
        return self.scenario.environment.now

    def require_service(self, service: str) -> None:
        if service not in self.scenario.topology:
            raise ValueError(f"unknown service {service!r}; known: {', '.join(self.services)}")

    # ---- read-only diagnostics -------------------------------------------------------------

    def query_logs(self, service: str, pattern: str, window_minutes: int) -> dict[str, Any]:
        self.require_service(service)
        terms = [t.strip().lower() for t in pattern.split("|") if t.strip()]
        since = self.now - timedelta(minutes=window_minutes)
        lines: list[str] = []
        total = 0
        for line in self.scenario.environment.logs.get(service, []):
            try:
                ts = parse_log_line(line).timestamp
            except IngestError:
                continue
            if ts < since:
                continue
            if terms and not any(term in line.lower() for term in terms):
                continue
            total += 1
            if len(lines) < MAX_LOG_RESULTS:
                lines.append(line)
        return {
            "service": service,
            "pattern": pattern,
            "window_minutes": window_minutes,
            "matches": total,
            "lines": lines,
        }

    def get_metric(self, service: str, name: str, window_minutes: int) -> dict[str, Any]:
        self.require_service(service)
        series = self.scenario.environment.metrics.get(service, {}).get(name)
        if series is None:
            available = sorted(self.scenario.environment.metrics.get(service, {}))
            raise ValueError(f"no metric {name!r} for {service}; available: {available}")
        since = self.now - timedelta(minutes=window_minutes)
        points = [
            (series.start + timedelta(minutes=i * series.interval_minutes), value)
            for i, value in enumerate(series.points)
        ]
        window = [(ts, v) for ts, v in points if ts >= since] or points[-1:]
        peak = max(v for _, v in window)
        ratio = peak / series.baseline if series.baseline > 0 else (float("inf") if peak else 1.0)
        return {
            "service": service,
            "metric": name,
            "unit": series.unit,
            "baseline": series.baseline,
            "limit": series.limit,
            "peak": peak,
            "latest": window[-1][1],
            "change_ratio": round(ratio, 2),
            "at_limit": series.limit is not None and peak >= series.limit,
            "points": [{"ts": ts.isoformat(), "value": v} for ts, v in window[-12:]],
        }

    def list_deploys(self, service: str, incident_start: datetime | None) -> dict[str, Any]:
        self.require_service(service)
        deploys = sorted(
            self.scenario.environment.deploys.get(service, []), key=lambda d: d.at, reverse=True
        )
        out = []
        for deploy in deploys[:10]:
            item = deploy.model_dump(mode="json")
            if incident_start is not None:
                item["minutes_before_incident"] = round(
                    (incident_start - deploy.at).total_seconds() / 60, 1
                )
            out.append(item)
        return {
            "service": service,
            "current_version": self.current_versions.get(service),
            "deploys": out,
        }

    def get_config_diff(self, service: str, incident_start: datetime | None) -> dict[str, Any]:
        self.require_service(service)
        changes = []
        for change in self.scenario.environment.config_changes.get(service, []):
            item = change.model_dump(mode="json")
            if incident_start is not None:
                item["minutes_before_incident"] = round(
                    (incident_start - change.at).total_seconds() / 60, 1
                )
            changes.append(item)
        return {"service": service, "changes": changes}

    def check_dependency(self, service: str, dependency: str | None) -> dict[str, Any]:
        self.require_service(service)
        deps = self.scenario.topology.dependencies(service)
        if dependency is not None and dependency not in deps:
            raise ValueError(f"{service} does not depend on {dependency!r}; deps: {deps}")
        health = self.scenario.environment.dependencies.get(service, {})
        targets = [dependency] if dependency else deps
        results: dict[str, dict[str, Any]] = {}
        for dep in targets:
            if dep in self.failovers:
                results[dep] = {
                    "status": "healthy",
                    "note": f"failed over to {self.failovers[dep]}",
                }
            elif dep in health:
                results[dep] = health[dep].model_dump()
            else:
                results[dep] = {"status": "healthy", "latency_ms": 20.0, "error_rate": 0.0}
        return {"service": service, "dependencies": results}

    def describe_service(self, service: str) -> dict[str, Any]:
        self.require_service(service)
        info = self.scenario.topology.services[service]
        return {
            "service": service,
            "description": info.description,
            "owner": info.owner,
            "customer_facing": info.customer_facing,
            "depends_on": info.depends_on,
            "called_by": self.scenario.topology.dependents(service),
            "current_version": self.current_versions.get(service),
            "replicas": self.replicas.get(service),
            "feature_flags": {
                k: v for k, v in self.feature_flags.items() if k.startswith(f"{service}.")
            },
        }

    # ---- mutation (approval-gated executor only) -------------------------------------------

    def apply_runbook(self, runbook_id: str, params: dict[str, Any]) -> str:
        if runbook_id == "rollback_deploy":
            self.current_versions[params["service"]] = str(params["to_version"])
            message = f"rolled back {params['service']} to {params['to_version']}"
        elif runbook_id == "restart_service":
            message = f"rolling restart of {params['service']} completed"
        elif runbook_id == "scale_out":
            self.replicas[params["service"]] = int(params["replicas"])
            message = f"scaled {params['service']} to {params['replicas']} replicas"
        elif runbook_id == "toggle_feature_flag":
            self.feature_flags[params["flag"]] = bool(params["enabled"])
            message = f"set feature flag {params['flag']}={params['enabled']}"
        elif runbook_id == "failover_dependency":
            target = self.scenario.environment.failover_targets[params["dependency"]]
            self.failovers[params["dependency"]] = target
            message = f"failed {params['dependency']} over to {target}"
        else:  # pragma: no cover - the runbook catalog is validated before this point
            raise ValueError(f"unknown runbook {runbook_id!r}")
        self.executed.append(ExecutionRecord(runbook_id, dict(params), message))
        return message

    def is_mitigated(self) -> bool:
        """Did an executed runbook address the scenario's real root cause?"""
        expected = self.scenario.expected
        for record in self.executed:
            if record.runbook_id != expected.runbook_id:
                continue
            target = record.params.get("service") or record.params.get("dependency")
            flag = str(record.params.get("flag", ""))
            if target == expected.service or flag.startswith(f"{expected.service}."):
                return True
        return False


def safe_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,100}", value))
