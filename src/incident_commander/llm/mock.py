"""Deterministic offline incident commander policy.

It behaves like a disciplined on-call engineer: take the correlator's hypotheses in rank order,
run a fixed diagnostic playbook for each, judge the evidence, refute or confirm, propose the
matching runbook and conclude. It is stateless - the next action is derived purely from the
transcript - so the whole pipeline runs in CI and demos with zero API keys. Telemetry is only
ever read as JSON data; nothing inside it can change what the policy does.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from incident_commander.agent.actions import ActionParseError, parse_action
from incident_commander.llm.base import Completion, LLMProvider, Message, Usage

_HYPOTHESES_RE = re.compile(r"<hypotheses>\s*(.*?)\s*</hypotheses>", re.DOTALL)
_DATA_RE = re.compile(r'<untrusted-data source="[^"]*">\n(.*)\n</untrusted-data>', re.DOTALL)

Check = tuple[str, dict[str, Any]]


@dataclass
class _Turn:
    tool: str
    args: dict[str, Any]
    data: dict[str, Any] | None  # parsed tool output, None on error / non-diagnostic
    ok: bool = True


def playbook(hypothesis: dict[str, Any]) -> list[Check]:
    service = str(hypothesis["service"])
    cause = hypothesis["cause"]
    details = hypothesis.get("details") or {}
    if cause == "bad_deploy":
        return [
            ("list_deploys", {"service": service}),
            ("get_metric", {"service": service, "name": "error_rate"}),
            ("query_logs", {"service": service, "pattern": "exception|panic|traceback"}),
        ]
    if cause == "config_change":
        return [
            ("get_config_diff", {"service": service}),
            ("get_metric", {"service": service, "name": "error_rate"}),
        ]
    if cause == "resource_exhaustion":
        return [
            ("get_metric", {"service": service, "name": "db_connections"}),
            ("query_logs", {"service": service, "pattern": "connection|pool|memory"}),
        ]
    if cause == "dependency_failure":
        callers = [c for c in str(details.get("callers", "")).split(",") if c]
        caller = callers[0] if callers else service
        return [
            ("check_dependency", {"service": caller, "dependency": service}),
            ("query_logs", {"service": caller, "pattern": f"timeout|{service}"}),
        ]
    if cause == "traffic_spike":
        return [
            ("get_metric", {"service": service, "name": "request_rate"}),
            ("describe_service", {"service": service}),
        ]
    return [("describe_service", {"service": service})]


def anomalous(tool: str, data: dict[str, Any] | None) -> bool:
    """Does one diagnostic result look abnormal?"""
    if not data:
        return False
    if tool == "list_deploys":
        return any(0 <= float(d.get("minutes_before_incident", -1)) <= 60 for d in data["deploys"])
    if tool == "get_config_diff":
        return any(0 <= float(c.get("minutes_before_incident", -1)) <= 60 for c in data["changes"])
    if tool == "get_metric":
        return bool(data.get("at_limit")) or float(data.get("change_ratio", 1)) >= 2.0
    if tool == "query_logs":
        return int(data.get("matches", 0)) > 0
    if tool == "check_dependency":
        return any(d.get("status") != "healthy" for d in data["dependencies"].values())
    return tool == "describe_service"


def _describe(tool: str, data: dict[str, Any] | None) -> str:
    if not data:
        return f"{tool}: no data"
    if tool == "list_deploys":
        recent = [d for d in data["deploys"] if 0 <= d.get("minutes_before_incident", -1) <= 60]
        if recent:
            d = recent[0]
            return f"{d['version']} deployed {d['minutes_before_incident']:g} min before impact"
        return "no deploy in the hour before impact"
    if tool == "get_config_diff":
        return f"{len(data['changes'])} recent config change(s)"
    if tool == "get_metric":
        limit = " (at limit)" if data.get("at_limit") else ""
        return f"{data['metric']} peak {data['peak']:g} vs baseline {data['baseline']:g}{limit}"
    if tool == "query_logs":
        return f"{data['matches']} log line(s) matching '{data['pattern']}'"
    if tool == "check_dependency":
        return "; ".join(f"{k} {v.get('status')}" for k, v in data["dependencies"].items())
    return f"{tool} ok"


def _remediation(hypothesis: dict[str, Any], turns: list[_Turn]) -> Check | None:
    service = str(hypothesis["service"])
    cause = hypothesis["cause"]
    details = hypothesis.get("details") or {}
    if cause == "bad_deploy":
        target = details.get("previous_version")
        for turn in turns:
            if turn.tool == "list_deploys" and turn.data and turn.data["deploys"]:
                target = turn.data["deploys"][0].get("previous_version") or target
        if target:
            return ("rollback_deploy", {"service": service, "to_version": target})
        return None
    if cause == "resource_exhaustion":
        return ("restart_service", {"service": service})
    if cause == "dependency_failure":
        return ("failover_dependency", {"dependency": service})
    if cause == "traffic_spike":
        for turn in turns:
            if turn.tool == "describe_service" and turn.data and turn.data.get("replicas"):
                return (
                    "scale_out",
                    {"service": service, "replicas": int(turn.data["replicas"]) * 2},
                )
    return None


def _action(thought: str, tool: str, args: dict[str, Any]) -> str:
    return json.dumps({"thought": thought, "tool": tool, "args": args})


class MockProvider(LLMProvider):
    name = "mock"

    def complete(self, messages: list[Message], *, temperature: float = 0.0) -> Completion:
        content = self._next(messages)
        return Completion(content=content, model="mock-commander", usage=Usage(0, 0))

    @staticmethod
    def _history(messages: list[Message]) -> list[_Turn]:
        turns: list[_Turn] = []
        for i, message in enumerate(messages):
            if message.role != "assistant":
                continue
            try:
                action = parse_action(message.content)
            except ActionParseError:
                continue
            data = None
            reply = messages[i + 1].content if i + 1 < len(messages) else ""
            ok = "(ok)" in reply.split("\n", 1)[0]
            if ok:
                match = _DATA_RE.search(reply)
                if match:
                    try:
                        loaded = json.loads(match.group(1))
                        data = loaded if isinstance(loaded, dict) else None
                    except json.JSONDecodeError:
                        data = None
            turns.append(_Turn(action.tool, action.args, data, ok))
        return turns

    def _next(self, messages: list[Message]) -> str:
        task = next((m.content for m in messages if m.role == "user"), "")
        match = _HYPOTHESES_RE.search(task)
        hypotheses: list[dict[str, Any]] = json.loads(match.group(1)) if match else []
        turns = self._history(messages)

        for hypothesis in hypotheses:
            hid = hypothesis["id"]
            checks = playbook(hypothesis)
            results: list[_Turn] = []
            for tool, args in checks:
                done = next((t for t in turns if t.tool == tool and t.args == args), None)
                if done is None:
                    return _action(
                        f"Testing {hid} ({hypothesis['cause']}) with {tool}.", tool, args
                    )
                results.append(done)
            hits = [t for t in results if anomalous(t.tool, t.data)]
            supported = len(hits) * 3 >= len(results) * 2
            evidence = "; ".join(_describe(t.tool, t.data) for t in results)
            recorded = any(
                t.tool == "record_evidence" and t.args.get("hypothesis_id") == hid for t in turns
            )
            if not recorded:
                verdict = "supported" if supported else "refuted"
                return _action(
                    f"{len(hits)}/{len(results)} checks abnormal for {hid}.",
                    "record_evidence",
                    {"hypothesis_id": hid, "verdict": verdict, "evidence": evidence},
                )
            if not supported:
                continue
            fix = _remediation(hypothesis, results)
            proposed = any(t.tool == "propose_remediation" for t in turns)  # one attempt only
            if fix is not None and not proposed:
                runbook, params = fix
                return _action(
                    f"{hid} is confirmed; proposing {runbook}.",
                    "propose_remediation",
                    {
                        "runbook_id": runbook,
                        "params": params,
                        "rationale": f"{hypothesis['cause'].replace('_', ' ')} on "
                        f"{hypothesis['service']}: {evidence}",
                        "hypothesis_id": hid,
                    },
                )
            return _action(
                "Root cause confirmed with evidence.",
                "conclude",
                {
                    "root_cause_hypothesis_id": hid,
                    "summary": f"Root cause: {hypothesis['cause'].replace('_', ' ')} on "
                    f"{hypothesis['service']}. Evidence: {evidence}.",
                },
            )
        return _action(
            "No hypothesis survived testing; escalating to humans.",
            "conclude",
            {"summary": "Inconclusive: no candidate cause was supported. Escalate to the owners."},
        )
