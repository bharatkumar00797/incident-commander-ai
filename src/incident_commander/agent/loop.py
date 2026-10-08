"""The investigate loop: hypothesise -> run a read-only diagnostic -> record evidence -> propose."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from incident_commander.agent.actions import ActionParseError, parse_action
from incident_commander.agent.prompts import (
    INVALID_REPLY,
    OBSERVATION_TEMPLATE,
    SYSTEM_PROMPT,
    TASK_TEMPLATE,
)
from incident_commander.correlate import Correlation
from incident_commander.environment import SimulatedEnvironment
from incident_commander.llm.base import LLMError, LLMProvider, Message
from incident_commander.models import (
    MAX_TEXT,
    Actor,
    EntryKind,
    Hypothesis,
    HypothesisStatus,
    Incident,
    IncidentStatus,
    Severity,
)
from incident_commander.remediation import RemediationError, describe_runbooks, propose
from incident_commander.tools import ToolRegistry, to_untrusted_json, wrap_untrusted
from incident_commander.triage import TriageResult, apply_model_severity

Clock = Callable[[], datetime]
StepCallback = Callable[["Step"], None]
MAX_OBSERVATION = 6_000
MAX_SIGNALS_IN_PROMPT = 40


@dataclass
class Step:
    index: int
    tool: str
    args: dict[str, Any]
    thought: str
    ok: bool
    observation: str


@dataclass
class InvestigationResult:
    status: str  # concluded | max_steps | failed
    summary: str
    root_cause_id: str | None
    steps: list[Step] = field(default_factory=list)
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


def seed_hypotheses(incident: Incident, correlation: Correlation, ts: datetime | None) -> None:
    for index, candidate in enumerate(correlation.candidates[:6], start=1):
        hypothesis = Hypothesis(
            id=f"H{index}",
            statement=f"{candidate.cause.replace('_', ' ')} on {candidate.service}: "
            f"{candidate.rationale}"[:MAX_TEXT],
            suspected_cause=candidate.cause,
            service=candidate.service,
            confidence=candidate.score,
        )
        incident.hypotheses.append(hypothesis)
        incident.record(
            Actor.AGENT,
            EntryKind.HYPOTHESIS,
            f"{hypothesis.id} ({candidate.score:.2f}): {hypothesis.statement}",
            hypothesis.id,
            *candidate.refs,
            ts=ts,
        )


def _truncate(text: str, limit: int = MAX_OBSERVATION) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n...[{len(text) - limit} chars truncated]...\n{text[-half:]}"


class Investigator:
    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry,
        incident: Incident,
        env: SimulatedEnvironment,
        *,
        max_steps: int = 20,
        max_invalid_replies: int = 3,
        clock: Clock | None = None,
        on_step: StepCallback | None = None,
    ) -> None:
        if not 1 <= max_steps <= 100:
            raise ValueError("max_steps must be between 1 and 100")
        self.provider = provider
        self.tools = tools
        self.incident = incident
        self.env = env
        self.max_steps = max_steps
        self.max_invalid_replies = max_invalid_replies
        self.clock = clock
        self.on_step = on_step

    def _now(self) -> datetime | None:
        return self.clock() if self.clock else None

    def _task(self, triage: TriageResult, correlation: Correlation) -> str:
        wanted = set(correlation.signal_ids)
        signals = [
            {
                "id": s.id,
                "ts": s.timestamp.isoformat(),
                "kind": str(s.kind),
                "service": s.service,
                "summary": s.summary,
            }
            for s in self.incident.signals
            if s.id in wanted
        ][:MAX_SIGNALS_IN_PROMPT]
        hypotheses = [
            {
                "id": h.id,
                "cause": h.suspected_cause,
                "service": c.service,
                "prior": h.confidence,
                "rationale": c.rationale,
                "details": c.details,
            }
            for h, c in zip(self.incident.hypotheses, correlation.candidates, strict=False)
        ]
        return TASK_TEMPLATE.format(
            incident_id=self.incident.id,
            title=self.incident.title,
            severity=self.incident.severity,
            reasons="\n".join(f"- {r}" for r in triage.reasons),
            signals=wrap_untrusted("signals", signals),
            hypotheses=to_untrusted_json(hypotheses),
        )

    def run(self, triage: TriageResult, correlation: Correlation) -> InvestigationResult:
        self.incident.status = IncidentStatus.INVESTIGATING
        messages = [
            Message(
                "system",
                SYSTEM_PROMPT.format(tools=self.tools.describe(), runbooks=describe_runbooks()),
            ),
            Message("user", self._task(triage, correlation)),
        ]
        result = InvestigationResult(
            "max_steps", f"stopped at the step limit ({self.max_steps})", None
        )
        invalid = 0
        for index in range(1, self.max_steps + 1):
            try:
                completion = self.provider.complete(messages)
            except LLMError as exc:
                result.status, result.summary = "failed", f"LLM provider error: {exc}"
                break
            result.model = completion.model
            result.prompt_tokens += completion.usage.prompt_tokens
            result.completion_tokens += completion.usage.completion_tokens
            messages.append(Message("assistant", completion.content))
            try:
                action = parse_action(completion.content)
            except ActionParseError as exc:
                invalid += 1
                if invalid >= self.max_invalid_replies:
                    result.status, result.summary = "failed", "model kept returning invalid actions"
                    break
                messages.append(Message("user", INVALID_REPLY.format(error=exc)))
                continue
            invalid = 0

            if action.tool == "conclude":
                done, text = self._conclude(action.args)
                self._step(
                    result, Step(index, action.tool, action.args, action.thought, done, text)
                )
                if done:
                    result.status = "concluded"
                    result.summary = self.incident.summary or text
                    result.root_cause_id = self.incident.root_cause_id
                    break
            else:
                ok, text = self._dispatch(action.tool, action.args)
                text = _truncate(text)
                self._step(result, Step(index, action.tool, action.args, action.thought, ok, text))
            messages.append(
                Message(
                    "user",
                    OBSERVATION_TEMPLATE.format(
                        tool=action.tool,
                        status="ok" if result.steps[-1].ok else "error",
                        output=result.steps[-1].observation,
                        step=index,
                        max_steps=self.max_steps,
                    ),
                )
            )
        if result.status != "concluded":
            self.incident.record(
                Actor.AGENT,
                EntryKind.NOTE,
                f"investigation ended: {result.summary}",
                ts=self._now(),
            )
        return result

    def _step(self, result: InvestigationResult, step: Step) -> None:
        result.steps.append(step)
        if self.on_step is not None:
            self.on_step(step)

    def _dispatch(self, tool: str, args: dict[str, Any]) -> tuple[bool, str]:
        handlers = {
            "add_hypothesis": self._add_hypothesis,
            "record_evidence": self._record_evidence,
            "escalate_severity": self._escalate,
            "propose_remediation": self._propose,
        }
        if tool in handlers:
            try:
                return True, handlers[tool](args)
            except (KeyError, TypeError, ValueError) as exc:
                return False, f"{exc.__class__.__name__}: {exc}"
        observed = self.tools.execute(tool, args)
        if observed.ok:
            self.incident.record(
                Actor.AGENT, EntryKind.DIAGNOSTIC, f"ran {tool}({_fmt(args)})", ts=self._now()
            )
        return observed.ok, observed.output

    def _add_hypothesis(self, args: dict[str, Any]) -> str:
        if len(self.incident.hypotheses) >= 20:
            raise ValueError("hypothesis limit reached (20)")
        hypothesis = Hypothesis(
            id=f"H{len(self.incident.hypotheses) + 1}",
            statement=str(args["statement"]),
            suspected_cause=str(args.get("cause") or "unknown"),
            service=str(args["service"]) if args.get("service") else None,
        )
        self.incident.hypotheses.append(hypothesis)
        self.incident.record(
            Actor.AGENT,
            EntryKind.HYPOTHESIS,
            f"{hypothesis.id}: {hypothesis.statement}",
            hypothesis.id,
            ts=self._now(),
        )
        return f"added hypothesis {hypothesis.id}"

    def _record_evidence(self, args: dict[str, Any]) -> str:
        hypothesis = self.incident.hypothesis(str(args["hypothesis_id"]))
        if hypothesis is None:
            raise ValueError(f"unknown hypothesis {args['hypothesis_id']!r}")
        verdict = str(args.get("verdict", "inconclusive")).lower()
        evidence = str(args.get("evidence") or "").strip()[:1000]
        if not evidence:
            raise ValueError("evidence text is required")
        raw_conf = args.get("confidence")
        if verdict == "supported":
            hypothesis.evidence_for.append(evidence)
            hypothesis.confidence = min(0.99, hypothesis.confidence + 0.15)
            hypothesis.status = HypothesisStatus.SUPPORTED
        elif verdict == "refuted":
            hypothesis.evidence_against.append(evidence)
            hypothesis.confidence = max(0.01, hypothesis.confidence - 0.4)
            hypothesis.status = HypothesisStatus.REFUTED
        elif verdict == "inconclusive":
            hypothesis.evidence_against.append(f"(inconclusive) {evidence}")
        else:
            raise ValueError("verdict must be supported, refuted or inconclusive")
        if isinstance(raw_conf, int | float) and not isinstance(raw_conf, bool):
            hypothesis.confidence = round(min(0.99, max(0.01, float(raw_conf))), 2)
        hypothesis.confidence = round(hypothesis.confidence, 2)
        self.incident.record(
            Actor.AGENT,
            EntryKind.HYPOTHESIS,
            f"{hypothesis.id} {verdict} ({hypothesis.confidence:.2f}): {evidence}",
            hypothesis.id,
            ts=self._now(),
        )
        return f"{hypothesis.id} is now {hypothesis.status} (confidence {hypothesis.confidence})"

    def _escalate(self, args: dict[str, Any]) -> str:
        proposed = Severity(str(args["severity"]).upper())
        decision = apply_model_severity(
            self.incident.severity, proposed, str(args.get("reason", ""))[:500]
        )
        self.incident.severity = decision.severity
        self.incident.record(Actor.AGENT, EntryKind.TRIAGE, decision.note, ts=self._now())
        return decision.note

    def _propose(self, args: dict[str, Any]) -> str:
        params = args.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        hypothesis_id = args.get("hypothesis_id")
        try:
            proposal = propose(
                self.incident,
                self.env,
                str(args["runbook_id"]),
                params,
                str(args.get("rationale") or "")[:1000],
                hypothesis_id=str(hypothesis_id) if hypothesis_id else None,
                ts=self._now(),
            )
        except RemediationError as exc:
            raise ValueError(str(exc)) from exc
        return f"proposal {proposal.id} recorded; it will run only after human approval"

    def _conclude(self, args: dict[str, Any]) -> tuple[bool, str]:
        summary = str(args.get("summary") or "").strip()[:2000] or "Investigation concluded."
        root = args.get("root_cause_hypothesis_id")
        if root:
            hypothesis = self.incident.hypothesis(str(root))
            if hypothesis is None:
                return False, f"unknown hypothesis {root!r}"
            if not hypothesis.evidence_for:
                return False, f"{hypothesis.id} has no supporting evidence; record evidence first"
            self.incident.root_cause_id = hypothesis.id
            self.incident.status = IncidentStatus.MITIGATING
        self.incident.summary = summary
        self.incident.record(Actor.AGENT, EntryKind.NOTE, f"concluded: {summary}", ts=self._now())
        return True, summary


def _fmt(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items())
