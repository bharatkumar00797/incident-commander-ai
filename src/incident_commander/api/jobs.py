"""Background investigations with bounded concurrency and an in-memory incident store."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from incident_commander.config import Settings
from incident_commander.engine import IncidentRun, run_scenario
from incident_commander.llm import build_provider
from incident_commander.models import Incident, ProposalStatus, RemediationProposal, new_id
from incident_commander.postmortem import render_postmortem
from incident_commander.remediation import RemediationError, approve, execute, reject
from incident_commander.scenario import Scenario

JobState = Literal["queued", "running", "finished", "error"]
ProgressFn = Callable[[Incident], None]
RunFn = Callable[[Scenario, Settings, str, ProgressFn], IncidentRun]


def default_run(
    scenario: Scenario, settings: Settings, incident_id: str, on_progress: ProgressFn
) -> IncidentRun:
    return run_scenario(
        scenario,
        build_provider(settings),
        max_steps=settings.max_steps,
        on_progress=on_progress,
        incident_id=incident_id,
    )


def _now() -> datetime:
    return datetime.now(UTC)


class QueueFull(RuntimeError):
    """Too many investigations are queued or running."""


class ProposalNotFound(LookupError):
    """The incident has no remediation proposal with that id."""


@dataclass
class IncidentRecord:
    """One incident as tracked by the service.

    The worker thread mutates its own live :class:`Incident`; readers only ever see
    ``snapshot``, a deep copy published after triage and after each investigation step, so a
    poll never observes a half-updated object.
    """

    id: str
    owner: str
    scenario: Scenario
    provider: str
    max_steps: int
    created_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    state: JobState = "queued"
    snapshot: Incident | None = None
    run: IncidentRun | None = None
    error: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def done(self) -> bool:
        return self.state in ("finished", "error")

    def publish(self, incident: Incident) -> None:
        copy = incident.model_copy(deep=True)
        with self.lock:
            self.snapshot = copy

    def view(self) -> Incident | None:
        with self.lock:
            return self.snapshot

    def postmortem(self) -> str | None:
        snap = self.view()
        return render_postmortem(snap, self.scenario.topology) if snap else None


class IncidentManager:
    """Owns the worker pool and the incident records.

    Investigations run in a thread pool. ``max_active`` bounds queued + running work so a burst
    cannot pile up unbounded jobs (callers get :class:`QueueFull` -> HTTP 503); the oldest
    finished incidents are evicted beyond ``max_kept``. With ``inline=True`` there is no pool:
    :meth:`submit` runs the investigation in the calling thread (serverless mode).
    """

    def __init__(
        self,
        *,
        max_workers: int = 2,
        max_active: int = 8,
        max_kept: int = 200,
        run_fn: RunFn = default_run,
        inline: bool = False,
    ) -> None:
        self.inline = inline
        self._executor = (
            None
            if inline
            else ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="incident")
        )
        self._max_active = max_active
        self._max_kept = max_kept
        self._run_fn = run_fn
        self._records: OrderedDict[str, IncidentRecord] = OrderedDict()
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- public
    def submit(self, *, owner: str, scenario: Scenario, settings: Settings) -> IncidentRecord:
        record = IncidentRecord(
            id=new_id("inc"),
            owner=owner,
            scenario=scenario,
            provider=settings.provider,
            max_steps=settings.max_steps,
        )
        with self._lock:
            if self._active_unlocked() >= self._max_active:
                raise QueueFull("Too many investigations in progress; try again shortly")
            self._records[record.id] = record
            self._evict_unlocked()
        if self._executor is None:
            self._execute(record, settings)
        else:
            self._executor.submit(self._execute, record, settings)
        return record

    def get(self, incident_id: str, owner: str | None) -> IncidentRecord | None:
        """Look up an incident; ``owner=None`` means the caller may see every incident."""
        with self._lock:
            record = self._records.get(incident_id)
        if record is None or (owner is not None and record.owner != owner):
            return None
        return record

    def list(self, owner: str | None, limit: int = 50) -> list[IncidentRecord]:
        with self._lock:
            records = [
                r for r in reversed(self._records.values()) if owner is None or r.owner == owner
            ]
        return records[:limit]

    def active_count(self) -> int:
        with self._lock:
            return self._active_unlocked()

    def decide(
        self,
        record: IncidentRecord,
        proposal_id: str,
        approver: str,
        *,
        approved: bool,
        reason: str = "",
    ) -> RemediationProposal:
        """Apply a human decision. Approval immediately runs the (simulated) runbook."""
        with record.lock:
            run = record.run
            if run is None or record.state != "finished":
                raise RemediationError("the investigation has not finished yet")
            if run.incident.proposal(proposal_id) is None:
                raise ProposalNotFound(proposal_id)
            if approved:
                approve(run.incident, proposal_id, approver, ts=run.clock())
                proposal = execute(run.incident, run.env, proposal_id, ts=run.clock())
            else:
                proposal = reject(run.incident, proposal_id, approver, reason, ts=run.clock())
            record.publish(run.incident)
            return proposal.model_copy()

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

    # --------------------------------------------------------------- internal
    def _active_unlocked(self) -> int:
        return sum(1 for r in self._records.values() if not r.done)

    def _evict_unlocked(self) -> None:
        overflow = len(self._records) - self._max_kept
        if overflow <= 0:
            return
        for rid in [rid for rid, r in self._records.items() if r.done][:overflow]:
            del self._records[rid]

    def _execute(self, record: IncidentRecord, settings: Settings) -> None:
        with record.lock:
            record.state = "running"
            record.started_at = _now()
        try:
            run = self._run_fn(record.scenario, settings, record.id, record.publish)
        except Exception as exc:  # a crashed investigation must never take down the worker
            with record.lock:
                record.state = "error"
                record.error = f"{type(exc).__name__}: {exc}"[:500]
                record.finished_at = _now()
            return
        with record.lock:
            record.run = run
            record.publish(run.incident)
            record.state = "finished"
            record.finished_at = _now()


def pending_proposals(incident: Incident) -> list[RemediationProposal]:
    return [p for p in incident.proposals if p.status is ProposalStatus.PROPOSED]
