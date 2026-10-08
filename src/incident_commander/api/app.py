"""FastAPI application: REST endpoints for incidents plus the static dashboard."""

from __future__ import annotations

import hashlib
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi import status as http
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from incident_commander import __version__
from incident_commander.api.jobs import (
    IncidentManager,
    IncidentRecord,
    ProposalNotFound,
    QueueFull,
    RunFn,
    default_run,
    pending_proposals,
)
from incident_commander.api.middleware import BodyLimitMiddleware, SecurityHeadersMiddleware
from incident_commander.api.schemas import (
    ConfigOut,
    DecisionIn,
    DecisionOut,
    HealthOut,
    IncidentCreate,
    IncidentCreatedOut,
    IncidentDetailOut,
    IncidentListOut,
    IncidentSummaryOut,
    ScenarioListOut,
    ScenarioOut,
    TimelineOut,
)
from incident_commander.api.security import ApiKeyAuth, RateLimiter, Role
from incident_commander.api.settings import ApiSettings
from incident_commander.config import Settings
from incident_commander.ingest import IngestError
from incident_commander.remediation import RemediationError
from incident_commander.scenario import Scenario, custom_scenario, list_scenarios, load_scenario

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 256 * 1024
IdParam = Annotated[str, PathParam(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


@dataclass(frozen=True)
class Caller:
    """``owner`` tags new incidents, ``scope`` limits visibility (``None`` = all incidents),
    ``client`` keys rate limits and ``trusted`` unlocks real providers and raw signals."""

    owner: str
    scope: str | None
    client: str
    role: Role
    trusted: bool
    key_id: str | None = None


def client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        # A proxy appends the address it saw, so the right-most entry is the one added by the
        # trusted edge; anything to its left is client-supplied and can be forged.
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",")]
        hops = [h for h in hops if h]
        if hops:
            return hops[-1][:64]
    return request.client.host if request.client else "unknown"


def presented_key(request: Request) -> str | None:
    key = request.headers.get("x-api-key")
    if key and key.strip():
        return key.strip()
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _retry_after(seconds: float) -> dict[str, str]:
    return {"Retry-After": str(max(1, math.ceil(seconds)))}


@dataclass(frozen=True)
class AccessGuard:
    """Authenticates a request and applies the general per-caller rate limit."""

    settings: ApiSettings
    auth: ApiKeyAuth
    limiter: RateLimiter

    def identify(self, request: Request) -> Caller | None:
        if self.auth.enabled:
            identity = self.auth.verify(presented_key(request))
            if identity is None:
                return None
            fp = identity.fingerprint
            # Responders see their own incidents; approvers review everyone's.
            scope = None if identity.role is Role.APPROVER else fp
            return Caller(fp, scope, f"key:{fp}", identity.role, True, fp[:8])
        ip = client_ip(request, self.settings.trust_proxy)
        if self.settings.dev_mode:
            return Caller("dev", None, f"ip:{ip}", Role.APPROVER, True)
        anon = "anon:" + hashlib.sha256(ip.encode()).hexdigest()[:12]
        return Caller(anon, anon, f"ip:{ip}", Role.APPROVER, False)

    def __call__(self, request: Request) -> Caller:
        who = self.identify(request)
        if who is None:
            raise HTTPException(
                http.HTTP_401_UNAUTHORIZED,
                "Missing or invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        retry = self.limiter.check(who.client)
        if retry is not None:
            raise HTTPException(
                http.HTTP_429_TOO_MANY_REQUESTS, "Rate limit exceeded", headers=_retry_after(retry)
            )
        return who


def get_caller(request: Request) -> Caller:
    guard: AccessGuard = request.app.state.guard
    return guard(request)


CallerDep = Annotated[Caller, Depends(get_caller)]


def _summary(record: IncidentRecord) -> IncidentSummaryOut:
    snap = record.view()
    with record.lock:
        state, finished = record.state, record.finished_at
    return IncidentSummaryOut(
        id=record.id,
        state=state,
        scenario=record.scenario.name,
        title=snap.title if snap else record.scenario.title,
        provider=record.provider,
        severity=snap.severity if snap else None,
        status=snap.status if snap else None,
        services=list(snap.services) if snap else [],
        pending_approvals=len(pending_proposals(snap)) if snap else 0,
        created_at=record.created_at,
        finished_at=finished,
    )


def _detail(record: IncidentRecord) -> IncidentDetailOut:
    snap = record.view()
    with record.lock:
        run, error, started = record.run, record.error, record.started_at
    return IncidentDetailOut(
        **_summary(record).model_dump(),
        started_at=started,
        opened_at=snap.opened_at if snap else None,
        resolved_at=snap.resolved_at if snap else None,
        max_steps=record.max_steps,
        summary=snap.summary if snap else None,
        root_cause_id=snap.root_cause_id if snap else None,
        investigation_status=run.investigation.status if run else None,
        triage_reasons=list(run.triage.reasons) if run else [],
        signal_count=len(snap.signals) if snap else 0,
        timeline_count=len(snap.timeline) if snap else 0,
        hypotheses=list(snap.hypotheses) if snap else [],
        proposals=list(snap.proposals) if snap else [],
        error=error,
    )


def create_app(
    settings: ApiSettings | None = None,
    *,
    agent_settings: Settings | None = None,
    run_fn: RunFn | None = None,
) -> FastAPI:
    """Build the application. Arguments exist mainly so tests can inject config."""
    cfg = settings or ApiSettings.from_env()
    base = agent_settings or Settings.from_env()
    auth = ApiKeyAuth(cfg.responder_keys, cfg.approver_keys)
    incident_limiter = RateLimiter(cfg.incident_limit_per_minute)
    manager = IncidentManager(
        max_workers=cfg.max_concurrent_runs,
        max_active=cfg.max_queued_runs,
        max_kept=cfg.max_incidents_kept,
        run_fn=run_fn or default_run,
        inline=cfg.sync_runs,
    )
    scenario_names = list_scenarios()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        manager.shutdown()

    app = FastAPI(
        title="Incident Commander API",
        version=__version__,
        description=(
            "AI incident commander: triages signals, tests root-cause hypotheses with read-only "
            "diagnostics and proposes approval-gated remediation (simulated environment)."
        ),
        lifespan=lifespan,
    )
    app.state.guard = AccessGuard(cfg, auth, RateLimiter(cfg.rate_limit_per_minute))
    app.state.manager = manager

    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-API-Key", "Authorization"],
            allow_credentials=False,
            max_age=600,
        )
    app.add_middleware(BodyLimitMiddleware, max_bytes=MAX_BODY_BYTES)
    app.add_middleware(SecurityHeadersMiddleware)

    def load(incident_id: str, who: Caller) -> IncidentRecord:
        record = manager.get(incident_id, who.scope)
        if record is None:
            raise HTTPException(http.HTTP_404_NOT_FOUND, "Incident not found")
        return record

    def build_scenario(body: IncidentCreate, who: Caller) -> Scenario:
        if body.scenario is not None:
            try:
                return load_scenario(body.scenario)
            except (KeyError, ValueError) as exc:
                raise HTTPException(
                    http.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown scenario {body.scenario!r}"
                ) from exc
        if not who.trusted:
            raise HTTPException(
                http.HTTP_403_FORBIDDEN,
                "Public demo: only packaged scenarios are enabled (raw signals need an API key)",
            )
        try:
            return custom_scenario(body.signals or [], topology=body.topology, title=body.title)
        except (IngestError, ValidationError, ValueError) as exc:
            message = str(exc).splitlines()[0][:300]
            raise HTTPException(
                http.HTTP_422_UNPROCESSABLE_CONTENT, f"invalid signals: {message}"
            ) from exc

    # ------------------------------------------------------------------ meta
    @app.get("/healthz", response_model=HealthOut, tags=["meta"])
    def healthz() -> HealthOut:
        return HealthOut(version=__version__, active_investigations=manager.active_count())

    @app.get("/api/config", response_model=ConfigOut, tags=["meta"])
    def config(request: Request) -> ConfigOut:
        guard: AccessGuard = request.app.state.guard
        who = guard.identify(request) if presented_key(request) or not cfg.auth_enabled else None
        return ConfigOut(
            version=__version__,
            auth_required=cfg.auth_enabled,
            access_mode=cfg.access_mode,
            caller_role=who.role.value if who else None,
            raw_signals_allowed=not cfg.public_demo,
            providers=["mock"] if cfg.public_demo else ["mock", "openai"],
            default_provider="mock" if cfg.public_demo else base.provider,
            default_max_steps=min(base.max_steps, cfg.max_steps_cap),
            max_steps_cap=cfg.max_steps_cap,
            sync_runs=cfg.sync_runs,
            scenarios=scenario_names,
        )

    @app.get("/api/scenarios", response_model=ScenarioListOut, tags=["meta"])
    def scenarios(_: CallerDep) -> ScenarioListOut:
        out = []
        for name in scenario_names:
            sc = load_scenario(name)
            out.append(
                ScenarioOut(
                    name=sc.name,
                    title=sc.title,
                    description=sc.description,
                    services=list(sc.topology.services),
                )
            )
        return ScenarioListOut(scenarios=out)

    # ------------------------------------------------------------- incidents
    @app.post(
        "/api/incidents",
        response_model=IncidentCreatedOut,
        status_code=http.HTTP_202_ACCEPTED,
        tags=["incidents"],
        responses={200: {"description": "Finished investigation (sync mode)"}},
    )
    def create_incident(
        body: IncidentCreate, who: CallerDep, response: Response
    ) -> IncidentCreatedOut:
        if body.max_steps is not None and body.max_steps > cfg.max_steps_cap:
            raise HTTPException(
                http.HTTP_422_UNPROCESSABLE_CONTENT, f"max_steps must be <= {cfg.max_steps_cap}"
            )
        provider = body.provider or ("mock" if cfg.public_demo else base.provider)
        if provider != "mock" and not who.trusted:
            raise HTTPException(
                http.HTTP_403_FORBIDDEN, "Only the offline mock provider is enabled here"
            )
        scenario = build_scenario(body, who)
        # Only well-formed requests count towards the stricter incident-creation limit.
        retry = incident_limiter.check(who.client)
        if retry is not None:
            raise HTTPException(
                http.HTTP_429_TOO_MANY_REQUESTS,
                "Incident creation limit exceeded",
                headers=_retry_after(retry),
            )
        run_settings = replace(
            base,
            provider=provider,
            max_steps=min(body.max_steps or base.max_steps, cfg.max_steps_cap),
        )
        try:
            record = manager.submit(owner=who.owner, scenario=scenario, settings=run_settings)
        except QueueFull as exc:
            raise HTTPException(
                http.HTTP_503_SERVICE_UNAVAILABLE, str(exc), headers={"Retry-After": "10"}
            ) from exc
        detail = _detail(record).model_dump()
        if not manager.inline:
            return IncidentCreatedOut(**detail)  # 202: poll the timeline endpoint
        response.status_code = http.HTTP_200_OK
        snap = record.view()
        return IncidentCreatedOut(**detail, timeline=list(snap.timeline) if snap else [])

    @app.get("/api/incidents", response_model=IncidentListOut, tags=["incidents"])
    def list_incidents(
        who: CallerDep, limit: Annotated[int, Query(ge=1, le=100)] = 25
    ) -> IncidentListOut:
        return IncidentListOut(incidents=[_summary(r) for r in manager.list(who.scope, limit)])

    @app.get("/api/incidents/{incident_id}", response_model=IncidentDetailOut, tags=["incidents"])
    def get_incident(incident_id: IdParam, who: CallerDep) -> IncidentDetailOut:
        return _detail(load(incident_id, who))

    @app.get(
        "/api/incidents/{incident_id}/timeline", response_model=TimelineOut, tags=["incidents"]
    )
    def get_timeline(
        incident_id: IdParam,
        who: CallerDep,
        since: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    ) -> TimelineOut:
        record = load(incident_id, who)
        with record.lock:
            snap, state, done = record.snapshot, record.state, record.done
        entries = list(snap.timeline[since:]) if snap else []
        return TimelineOut(
            id=record.id,
            state=state,
            done=done,
            status=snap.status if snap else None,
            next_cursor=since + len(entries),
            entries=entries,
        )

    @app.get(
        "/api/incidents/{incident_id}/postmortem",
        response_class=PlainTextResponse,
        tags=["incidents"],
        responses={200: {"content": {"text/markdown": {}}}},
    )
    def get_postmortem(incident_id: IdParam, who: CallerDep) -> PlainTextResponse:
        text = load(incident_id, who).postmortem()
        if text is None:
            raise HTTPException(http.HTTP_409_CONFLICT, "Investigation has not started yet")
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    def decide(
        incident_id: str, action_id: str, body: DecisionIn, who: Caller, *, approved: bool
    ) -> DecisionOut:
        if who.role is not Role.APPROVER:
            raise HTTPException(
                http.HTTP_403_FORBIDDEN, "Approving or rejecting remediation needs an approver key"
            )
        record = load(incident_id, who)
        # The name is self-declared, so the key that made the call is recorded alongside it.
        approver = f"{body.approver} (key {who.key_id})" if who.key_id else body.approver
        try:
            proposal = manager.decide(
                record, action_id, approver, approved=approved, reason=body.reason
            )
        except ProposalNotFound as exc:
            raise HTTPException(http.HTTP_404_NOT_FOUND, "Proposal not found") from exc
        except RemediationError as exc:
            raise HTTPException(http.HTTP_409_CONFLICT, str(exc)) from exc
        snap = record.view()
        assert snap is not None  # decide() only succeeds on a finished investigation
        return DecisionOut(incident_id=record.id, proposal=proposal, incident_status=snap.status)

    @app.post(
        "/api/incidents/{incident_id}/actions/{action_id}/approve",
        response_model=DecisionOut,
        tags=["remediation"],
    )
    def approve_action(
        incident_id: IdParam, action_id: IdParam, body: DecisionIn, who: CallerDep
    ) -> DecisionOut:
        """Approve a proposed runbook; it then runs immediately against the simulator."""
        return decide(incident_id, action_id, body, who, approved=True)

    @app.post(
        "/api/incidents/{incident_id}/actions/{action_id}/reject",
        response_model=DecisionOut,
        tags=["remediation"],
    )
    def reject_action(
        incident_id: IdParam, action_id: IdParam, body: DecisionIn, who: CallerDep
    ) -> DecisionOut:
        return decide(incident_id, action_id, body, who, approved=False)

    # ------------------------------------------------------------- dashboard
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
