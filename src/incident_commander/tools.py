"""Read-only diagnostic tools exposed to the agent through a validating registry.

Tool output is telemetry - log lines and alert text written by systems (and possibly attackers) -
so it is always returned as JSON wrapped in an ``<untrusted-data>`` block, with ``<`` escaped so
the content can never close the block or forge prompt markup.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from incident_commander.environment import SimulatedEnvironment

ArgSpec = Mapping[str, tuple[type, bool, str]]  # name -> (type, required, description)

_IDENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
MAX_PATTERN = 200
MAX_WINDOW_MINUTES = 24 * 60


class ToolArgumentError(ValueError):
    pass


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str
    data: dict[str, Any] = field(default_factory=dict)


def to_untrusted_json(data: object) -> str:
    """JSON-encode ``data`` so it cannot contain a literal ``<`` (and hence no closing tag)."""
    return json.dumps(data, default=str, ensure_ascii=False).replace("<", "\\u003c")


def wrap_untrusted(source: str, data: object) -> str:
    return f'<untrusted-data source="{source}">\n{to_untrusted_json(data)}\n</untrusted-data>'


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: ArgSpec
    handler: Callable[[dict[str, Any]], dict[str, Any]]

    def validate(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(raw) - set(self.args)
        if unknown:
            raise ToolArgumentError(f"unknown argument(s) for {self.name}: {sorted(unknown)}")
        clean: dict[str, Any] = {}
        for name, (typ, required, _) in self.args.items():
            value = raw.get(name)
            if value is None:
                if required:
                    raise ToolArgumentError(f"missing required argument '{name}' for {self.name}")
                continue
            if typ is int and isinstance(value, str) and value.strip().isdigit():
                value = int(value)
            if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
                raise ToolArgumentError(f"argument '{name}' for {self.name} must be {typ.__name__}")
            clean[name] = value
        return clean

    def signature(self) -> str:
        params = ", ".join(
            f"{n}: {t.__name__}{'' if req else '?'}" for n, (t, req, _) in self.args.items()
        )
        lines = [f"- {self.name}({params}): {self.description}"]
        lines += [f"    {n}: {desc}" for n, (_, _, desc) in self.args.items()]
        return "\n".join(lines)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def describe(self) -> str:
        return "\n".join(tool.signature() for tool in self._tools.values())

    def execute(self, name: str, raw_args: Mapping[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(False, f"unknown tool '{name}'. Available: {', '.join(self._tools)}")
        try:
            data = tool.handler(tool.validate(raw_args))
        except Exception as exc:  # errors become observations; they never crash the loop
            return ToolResult(False, f"{exc.__class__.__name__}: {exc}")
        return ToolResult(True, wrap_untrusted(name, data), data)


def _ident(value: str, what: str) -> str:
    if not _IDENT_RE.match(value):
        raise ToolArgumentError(f"{what} must match [A-Za-z0-9._-]{{1,100}}")
    return value


def _window(args: Mapping[str, Any], default: int = 60) -> int:
    value = int(args.get("window_minutes", default))
    if not 1 <= value <= MAX_WINDOW_MINUTES:
        raise ToolArgumentError(f"window_minutes must be between 1 and {MAX_WINDOW_MINUTES}")
    return value


def build_diagnostic_registry(
    env: SimulatedEnvironment, incident_start: datetime | None
) -> ToolRegistry:
    registry = ToolRegistry()
    svc = (str, True, "service name from the topology")
    window = (int, False, f"look-back window in minutes (1-{MAX_WINDOW_MINUTES}, default 60)")

    def query_logs(a: dict[str, Any]) -> dict[str, Any]:
        pattern = str(a.get("pattern", "")).strip()
        if len(pattern) > MAX_PATTERN:
            raise ToolArgumentError(f"pattern longer than {MAX_PATTERN} characters")
        return env.query_logs(_ident(a["service"], "service"), pattern, _window(a))

    registry.register(
        Tool(
            "query_logs",
            "Search recent log lines of a service (case-insensitive terms, '|' = OR; no regex).",
            {
                "service": svc,
                "pattern": (str, False, "e.g. 'error|exception'"),
                "window_minutes": window,
            },
            query_logs,
        )
    )
    registry.register(
        Tool(
            "get_metric",
            "Fetch a metric series with baseline, peak, change_ratio and at_limit.",
            {
                "service": svc,
                "name": (str, True, "e.g. error_rate, latency_p99_ms, db_connections"),
                "window_minutes": window,
            },
            lambda a: env.get_metric(
                _ident(a["service"], "service"), _ident(a["name"], "metric name"), _window(a)
            ),
        )
    )
    registry.register(
        Tool(
            "list_deploys",
            "List recent deploys of a service with minutes_before_incident.",
            {"service": svc},
            lambda a: env.list_deploys(_ident(a["service"], "service"), incident_start),
        )
    )
    registry.register(
        Tool(
            "get_config_diff",
            "Show recent configuration changes of a service.",
            {"service": svc},
            lambda a: env.get_config_diff(_ident(a["service"], "service"), incident_start),
        )
    )
    registry.register(
        Tool(
            "check_dependency",
            "Health of a service's downstream dependencies (status, latency, error rate).",
            {"service": svc, "dependency": (str, False, "one dependency (default: all)")},
            lambda a: env.check_dependency(
                _ident(a["service"], "service"),
                _ident(a["dependency"], "dependency") if "dependency" in a else None,
            ),
        )
    )
    registry.register(
        Tool(
            "describe_service",
            "Owner, dependencies, callers, current version, replicas and feature flags.",
            {"service": svc},
            lambda a: env.describe_service(_ident(a["service"], "service")),
        )
    )
    return registry
