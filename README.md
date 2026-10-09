# Incident Commander — AI Production Incident Agent

[![CI](https://github.com/bharatkumar00797/incident-commander-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/bharatkumar00797/incident-commander-ai/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Type checked: mypy strict](https://img.shields.io/badge/mypy-strict-informational.svg)](pyproject.toml)
[![Lint: ruff](https://img.shields.io/badge/lint-ruff-261230.svg)](https://github.com/astral-sh/ruff)

**An AI agent that runs production incidents from the first alert to the postmortem.**

Incident Commander ingests alerts, logs, metrics and deploy/config events, triages severity,
correlates related signals into one incident, forms root-cause hypotheses and tests them with
**read-only** diagnostic tools, proposes remediation from a **runbook allowlist**, waits for
**human approval**, executes against a **simulated** environment, and drafts a blameless
postmortem. It works fully offline with a deterministic mock LLM provider — no API keys needed
for demos, tests or CI.

## Contents

- [The problem](#the-problem)
- [Features](#features)
- [Screenshots](#screenshots)
- [Architecture](#architecture)
- [How an investigation works](#how-an-investigation-works)
- [LLM providers](#llm-providers)
- [Quick start](#quick-start)
- [REST API](#rest-api)
- [Security model](#security-model)
- [Deployment](#deployment)
- [Configuration](#configuration)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

## The problem

On-call pages arrive as a pile of alerts, log snippets and deploy notifications. A human
incident commander has to triage severity, decide which signals belong together, dig through
logs and metrics for a root cause, pick a safe remediation, get a second pair of eyes on it,
and write the postmortem — often under time pressure, with noisy red herrings and the risk of
executing the wrong fix.

Incident Commander automates the investigation loop while keeping the dangerous part —
changing production — behind an allowlist and an explicit human approval gate. Diagnostics are
read-only by construction; remediations only ever touch a simulator in this project.

## Features

- **Ingest** Alertmanager / PagerDuty-style alerts, plain and JSON log lines, metric samples,
  deploy and config events (256 KB payload cap, batch and attribute caps, UTC normalisation).
- **Triage floor** Rule-based severity (error rate, latency, blast radius, customer-facing) that
  the model can escalate but never silently downgrade.
- **Correlate** Time-window + service dependency graph; ranked candidates (bad deploy, resource
  exhaustion, dependency failure, config change, traffic spike); unrelated signals set aside.
- **Investigate** Plan → act → observe agent loop with a tolerant JSON action parser, invalid
  reply recovery and a step cap. Six read-only tools against a simulated environment.
- **Remediate safely** Allowlisted runbooks with typed params and preconditions; approve /
  reject with an audited approver identity; execute only after approval, re-checking
  preconditions; resolve only when the simulator reports the real cause fixed.
- **Timeline + postmortem** Every signal, hypothesis, tool call, approval and action is
  recorded; a blameless Markdown postmortem covers impact, evidence, remediation and action items.
- **Offline by default** Deterministic mock provider reaches the expected root cause and runbook
  on all six packaged scenarios (including red herrings and a prompt-injection log line).
- **HTTP API + dashboard** FastAPI service with responder / approver roles, public demo, live
  timeline, Approve & run UI, deep links; hardened multi-platform deploy configs.

## Screenshots

Dashboard deep links use `#incident=<id>&tab=<name>` (list, timeline, hypotheses, remediations,
postmortem).

| Incident list | Live timeline |
| --- | --- |
| ![Incident list](docs/screenshots/list.png) | ![Timeline](docs/screenshots/timeline.png) |

| Hypotheses | Remediations (Approve UI) |
| --- | --- |
| ![Hypotheses](docs/screenshots/hypotheses.png) | ![Remediations](docs/screenshots/remediations.png) |

| Postmortem |
| --- |
| ![Postmortem](docs/screenshots/postmortem.png) |

## Architecture

```mermaid
flowchart LR
    subgraph Clients
        CLI["incident-commander run - CLI"]
        UI["Web dashboard - static HTML/JS"]
        HTTP["REST client - curl, CI, scripts"]
    end

    subgraph Service["FastAPI service - incident-commander serve"]
        Guard["AccessGuard: API key / roles / public-demo, rate limits, body cap, CSP"]
        Jobs["IncidentManager: thread pool or inline sync mode"]
    end

    subgraph Core["Agent core"]
        Ingest["Ingest + triage + correlate"]
        Agent["Agent loop: plan, act, observe"]
        LLM["LLM provider: mock or OpenAI-compatible"]
        Tools["Read-only diagnostic tools"]
        Rem["Runbook allowlist + approval gate"]
        PM["Blameless postmortem"]
    end

    subgraph Sim["Simulated environment"]
        Env["Logs, metrics, deploys, config, deps"]
    end

    UI --> Guard
    HTTP --> Guard
    Guard --> Jobs --> Ingest --> Agent
    CLI --> Ingest
    Agent <--> LLM
    Agent --> Tools
    Tools --> Env
    Agent --> Rem
    Rem --> Env
    Agent --> PM
```

| Layer | Module | Responsibility |
| --- | --- | --- |
| Interfaces | `cli.py`, `api/` | CLI, REST API, dashboard, auth / roles, rate limiting, incident lifecycle |
| Orchestration | `engine.py` | ingest → triage → correlate → agent loop → postmortem with a deterministic clock |
| Agent | `agent/` | loop, prompts, tolerant JSON action parser, remediation / conclude actions |
| LLM | `llm/` | provider interface, mock policy, OpenAI-compatible HTTP client |
| Diagnostics | `tools.py`, `environment.py` | validated read-only tools, simulated telemetry |
| Remediation | `remediation.py` | runbook allowlist, params / preconditions, approve / execute |
| Domain | `models.py`, `ingest/`, `triage.py`, `correlate.py`, `topology.py`, `postmortem.py` | signals, incidents, severity, correlation, Markdown postmortem |
| Scenarios | `scenarios/*.json` | packaged synthetic incidents shipped in the wheel |

## How an investigation works

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant API as API or CLI
    participant Eng as Engine
    participant Agent as Agent loop
    participant LLM as LLM provider
    participant Tools as Read-only tools
    participant Rem as Approval gate

    User->>API: scenario or raw signals
    API->>Eng: ingest, triage floor, correlate
    Eng->>Agent: incident + ranked hypotheses
    loop until conclude or step limit
        Agent->>LLM: conversation so far
        LLM-->>Agent: JSON action with thought, tool and args
        Agent->>Tools: execute validated diagnostic
        Tools-->>Agent: observation wrapped as untrusted-data
        Agent->>Agent: record evidence for or against hypotheses
    end
    Agent->>Rem: propose allowlisted runbook + params
    Rem-->>User: pending approval
    User->>Rem: approve or reject
    Rem->>Rem: re-check preconditions, execute on simulator
    Eng-->>API: timeline, root cause, postmortem
```

1. **Ingest.** Parsers normalise Alertmanager / PagerDuty webhooks, log lines, metric samples
   and deploy / config events into `Signal` objects (size, count and attribute caps; timestamps
   forced to UTC).
2. **Triage.** Deterministic rules set a severity floor from error-rate and latency thresholds,
   blast radius and customer-facing tags. The model may escalate with a justification; it cannot
   silently downgrade a SEV1 page.
3. **Correlate.** Signals in a time window on related services (via the scenario topology) become
   one incident; unrelated ones are noted and dropped. Ranked candidates seed hypotheses H1…Hn.
4. **Investigate.** The agent receives the hypotheses as JSON inside `<hypotheses>` (with `<`
   escaped). Each reply is a JSON action. Tool results come back as JSON inside
   `<untrusted-data source=…>` so log content cannot close the block or be treated as
   instructions. Evidence updates confidence; `conclude` is refused without supporting evidence.
5. **Propose.** The only remediation path is an allowlisted runbook id with schema-validated
   params that pass preconditions (for example rollback target must exist and differ from
   current). Status becomes `proposed`; nothing executes yet.
6. **Approve and execute.** A human with the approver role (or public-demo / `--approve` on the
   CLI) decides. Approve immediately executes against the simulator, re-checking preconditions;
   the incident resolves only if the real cause is fixed. Every decision is audited on the
   timeline (`decided_by = "<name> (key <fp8>)"`).
7. **Postmortem.** A blameless Markdown document covers summary, impact, root cause with
   evidence, hypotheses table, remediation, UTC timeline, what went well / poorly and
   cause-specific action items.

## LLM providers

| Provider | When to use | Configuration |
| --- | --- | --- |
| `mock` (default) | demos, CI, offline development | nothing to set |
| `openai` | any OpenAI-compatible `/chat/completions` API | `IC_BASE_URL`, `IC_API_KEY`, `IC_MODEL` |

```bash
# OpenAI
export IC_PROVIDER=openai
export IC_BASE_URL=https://api.openai.com/v1
export IC_MODEL=gpt-4o-mini
export IC_API_KEY=sk-...

# Groq
export IC_PROVIDER=openai
export IC_BASE_URL=https://api.groq.com/openai/v1
export IC_MODEL=llama-3.1-8b-instant
export IC_API_KEY=gsk_...

# Ollama (local, no key needed)
export IC_PROVIDER=openai
export IC_BASE_URL=http://localhost:11434/v1
export IC_MODEL=llama3.1
export IC_API_KEY=ollama
```

The mock provider follows a fixed playbook per suspected cause (list deploys + error rate +
exception logs for a bad deploy; pool metric + connection logs for exhaustion; and so on),
refutes red herrings, proposes the matching runbook and concludes. That is what CI and the
container smoke test assert.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
incident-commander scenarios                       # list bundled synthetic incidents
incident-commander run --scenario bad-deploy       # triage, investigate, propose a fix
incident-commander run --scenario bad-deploy --approve --approver "$USER"
```

Each run prints the triage decision, every diagnostic step, the ranked hypotheses and the
proposed runbook, and writes `incident-output/postmortem.md` and `incident-output/incident.json`.

Bundled scenarios (each has a known answer that the tests and the container smoke test check):

| Scenario | What happens | Red herring ruled out | Proposed runbook |
| --- | --- | --- | --- |
| `bad-deploy` | checkout-api v2.3.1 throws on every cart, 5xx spike | — (includes a prompt-injection log line) | `rollback_deploy` to v2.3.0 |
| `db-connection-exhaustion` | leaked connections exhaust the payments Postgres pool | harmless deploy minutes earlier | `restart_service` payments-api |
| `dependency-outage` | third-party identity provider times out across 3 services (SEV1) | — | `failover_dependency` auth-provider |
| `memory-leak` | search-api memory grows for hours until pods are OOM-killed | log-level config change | `restart_service` search-api |
| `bad-config-push` | enabling a discount feature flag breaks checkout totals | tax-service deploy | `toggle_feature_flag` back off |
| `traffic-spike` | ticket on-sale sends 8× traffic, CPU saturates | copy-only deploy | `scale_out` ticketing-api 4 → 8 |

### REST API + dashboard

```bash
incident-commander serve                 # http://127.0.0.1:8000 (honours $PORT and $HOST)
incident-commander serve --dev           # local development: no API key needed
```

Open `http://127.0.0.1:8000/` for the dashboard (pick a scenario, watch the live timeline,
review hypotheses, approve or reject the proposed runbook, read the postmortem) or `/docs` for
the OpenAPI reference.

## REST API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/incidents` | open an incident from `{"scenario": "bad-deploy"}` or a raw `{"signals": [...], "topology": {...}}` batch |
| `GET` | `/api/incidents` | incidents visible to the caller |
| `GET` | `/api/incidents/{id}` | severity, status, hypotheses, proposals |
| `GET` | `/api/incidents/{id}/timeline?since=N` | timeline entries after cursor `N` (polling) |
| `POST` | `/api/incidents/{id}/actions/{action_id}/approve` | approve and run the runbook (simulated) |
| `POST` | `/api/incidents/{id}/actions/{action_id}/reject` | reject with an optional reason |
| `GET` | `/api/incidents/{id}/postmortem` | blameless postmortem as Markdown |
| `GET` | `/api/scenarios`, `/api/config`, `/healthz` | metadata and health |

```bash
curl -s -X POST localhost:8000/api/incidents -H 'Content-Type: application/json' \
  -d '{"scenario": "bad-deploy"}'
curl -s "localhost:8000/api/incidents/<id>/timeline?since=0"
curl -s -X POST localhost:8000/api/incidents/<id>/actions/<action_id>/approve \
  -H 'Content-Type: application/json' -d '{"approver": "on-call"}'
```

### Access modes

| Mode | How to enable | Who can do what |
| --- | --- | --- |
| **Public demo** (default) | no keys, `IC_DEV_MODE` unset | packaged scenarios + mock only; per-client (hashed IP) ownership; approvals allowed because every runbook runs on the simulator; every response / UI labelled simulated |
| **API key** | `IC_API_KEYS` and/or `IC_APPROVER_KEYS` | responders open and follow their own incidents; approvers see all incidents and may approve / reject (four-eyes). Send `X-API-Key: <key>` or `Authorization: Bearer <key>` |
| **Dev** | `IC_DEV_MODE=true` or `serve --dev` | no authentication; caller acts as approver; for localhost only |

Investigations run in a bounded background worker pool (`POST` answers `202`, poll the
timeline). With `IC_SYNC_RUNS=true` (automatic on Vercel / AWS Lambda) they finish inside the
request (`200` with the full timeline).

### Error codes

| Status | When |
| --- | --- |
| `401` | missing or unknown API key |
| `403` | public-demo forbids raw signals / real providers; approve needs an approver key |
| `404` | unknown incident or proposal |
| `409` | investigation still running, or proposal already decided / preconditions failed |
| `413` | request body over 256 KB (Content-Length or streamed) |
| `422` | unknown scenario, invalid signals / topology, or `max_steps` above the cap |
| `429` | general or incident-creation rate limit exceeded |
| `503` | investigation queue full |

## Security model

An agent that reads production telemetry and proposes changes to production needs hard
boundaries. Incident Commander layers several guards; none of them is a silver bullet.

**Untrusted data and prompt injection**

- Log lines, alert text and tool output are wrapped as JSON inside
  `<untrusted-data source=…>` with every `<` escaped to `\u003c`, so content cannot close the
  block or look like a new tag. The system prompt tells the model to treat the block as data,
  never as instructions.
- The `bad-deploy` scenario includes an injection-style log line; tests assert it only ever
  appears inside the untrusted block.
- Tool names and arguments from the model are validated against a fixed registry; unknown tools
  and out-of-range args are rejected before anything runs.

**Read-only diagnostics**

- Tools are `query_logs`, `get_metric`, `list_deploys`, `get_config_diff`, `check_dependency`,
  `describe_service`. There is no shell, no arbitrary HTTP, no write path.
- `query_logs` uses case-insensitive substring terms (split on `|`), not regex — no ReDoS.
  Windows are clamped to 1…1440 minutes; identifiers match `[A-Za-z0-9._-]{1,100}`.

**Runbook allowlist and approval gate**

- The only remediation path is an allowlisted runbook id with schema-validated params and
  preconditions (rollback target in history and ≠ current; scale_out ≤ 4× and ≤ 50; flag must
  exist and change; failover needs a configured target).
- `requires_approval` is always true. Approve / reject needs an approver-role key (or public
  demo / CLI `--approve`). `decided_by` records the self-declared name plus a key fingerprint.
- `execute()` re-validates preconditions (a second identical rollback fails) and only resolves
  the incident when the simulator reports the expected cause fixed. Real executors are out of
  scope and documented as such.

**HTTP API**

- API keys are stored as SHA-256 digests and compared with `hmac.compare_digest` against every
  configured key (constant time). Responders see only their own incidents; approvers see all
  (they must review responders' work — four-eyes).
- Sliding-window rate limits per key or IP on every `/api` call, plus a stricter limit on
  incident creation (counted only after validation passes).
- Bounded investigation queue (`503` when full), step cap, 256 KB ASGI body cap on both
  Content-Length and streamed / chunked bytes.
- Strict pydantic validation (`extra=forbid`). Security headers: strict CSP
  (`default-src 'self'`, no inline scripts, `frame-ancestors 'none'`), `X-Frame-Options: DENY`,
  `nosniff`, `no-referrer`, `Cache-Control: no-store` on `/api/*`. CORS is off unless
  `IC_CORS_ORIGINS` is set. The dashboard renders all server data with `textContent` (no HTML
  sinks).
- Client IP: `X-Forwarded-For` is used only when `IC_TRUST_PROXY=true`, and then the
  **right-most** hop (the one your proxy appended) is taken; the left-most value is
  client-controlled and would let callers dodge rate limits or share a public-demo bucket.
- Secrets only come from the environment; provider keys never appear in any response (tested).

**Public demo and serverless caveats**

- Public demo scopes ownership to `anon:` + `sha256(client_ip)[:12]`. Behind a reverse proxy
  without `IC_TRUST_PROXY=true`, every visitor shares one IP bucket — set the flag only behind
  a proxy you control.
- Incidents live in memory per process. On serverless (Vercel / Lambda) an approval may land on
  a fresh instance and return `404`; use a container platform for anything beyond a demo.
  Sync mode (`IC_SYNC_RUNS`, auto-on serverless) returns the full timeline in the `POST`
  response so a single-request demo still works.

**Container hardening**

The Docker image runs as non-root uid 10001 (`commander`) with a two-stage build. The compose
file adds a read-only root filesystem, a `nosuid,nodev` tmpfs for `/tmp`, `cap_drop: ALL`,
`no-new-privileges`, and pids / memory / CPU limits. Prefer a container deployment for any
shared or internet-facing instance.

**Residual risks (honest list)**

- A real LLM may propose a wrong runbook; proposals always require human approval and run on
  the simulator in this project.
- Rate limits, incident history and ownership are in memory: per process, reset on restart and
  only best-effort across serverless instances.
- Public-demo IP hashing is only as good as the client IP you feed it (see `IC_TRUST_PROXY`).
- The mock provider is safe by construction; treat hosted-model output as advisory.

See [SECURITY.md](SECURITY.md) for how to report a vulnerability.

## Deployment

All container platforms build the same hardened, non-root image (`Dockerfile`) and probe
`/healthz`; the server listens on `$PORT`. Without API keys every deployment is a public demo.
Lock a deployment down by setting `IC_API_KEYS` / `IC_APPROVER_KEYS` as platform secrets.

### Long-running container vs serverless

| | Container (Docker / Render / Fly / Railway) | Serverless (Vercel) |
| --- | --- | --- |
| Process lifetime | long-lived; background investigations OK | per-request; use sync mode |
| Incident store | in-memory for the life of the process | in-memory per instance; approve may 404 |
| Best for | demos and small shared instances | quick public demos, single-request flows |

### Docker and docker compose

```bash
docker compose up --build
python3 scripts/smoke_test.py http://127.0.0.1:8000   # all 6 scenarios end to end
```

Compose runs read-only with all capabilities dropped, a `tmpfs` for `/tmp`, and memory / pid
limits. The image health-checks `/healthz` and honours `$PORT`.

### Render

`render.yaml` Blueprint: Docker runtime, free plan, health check on `/healthz`,
`IC_PROVIDER=mock`, `IC_TRUST_PROXY=true`, one concurrent investigation. Connect the repo and
Render applies the Blueprint.

### Fly.io

```bash
fly launch --copy-config --no-deploy
fly deploy
```

`fly.toml` sets `PORT=8080` (= `internal_port`), a `/healthz` check and a 512 MB VM.

### Railway

`railway.json` selects the Dockerfile builder, probes `/healthz` and restarts on failure.

### Vercel

`api/index.py` is the serverless entry; `vercel.json` bundles `src/incident_commander/**`
(scenarios + dashboard) and rewrites every path to the function. Investigations run in sync
mode because functions cannot keep background threads. Prefer a container platform if you need
approve-after-the-fact across requests.

## Configuration

All settings are environment variables (see [`.env.example`](.env.example)).

| Variable | Default | Description |
| --- | --- | --- |
| `IC_PROVIDER` | `mock` | `mock` (offline) or `openai` (any OpenAI-compatible API) |
| `IC_BASE_URL` | `https://api.openai.com/v1` | API base URL (Groq, Ollama, OpenRouter, vLLM, …) |
| `IC_API_KEY` | _(empty)_ | provider key; sent as a Bearer token, never returned |
| `IC_MODEL` | `gpt-4o-mini` | model name for the `openai` provider |
| `IC_MAX_STEPS` | `20` | default agent step budget |
| `IC_API_KEYS` | _(empty)_ | comma-separated responder keys; enables `api-key` mode |
| `IC_APPROVER_KEYS` | _(empty)_ | comma-separated approver keys (also see all incidents) |
| `IC_DEV_MODE` | `false` | no auth (localhost only); same as `serve --dev` |
| `IC_CORS_ORIGINS` | _(empty)_ | comma-separated browser origins; empty = same-origin only |
| `IC_RATE_LIMIT_PER_MINUTE` | `120` | `/api` requests per key or IP per minute |
| `IC_INCIDENT_LIMIT_PER_MINUTE` | `6` | incident creations per key or IP per minute |
| `IC_MAX_CONCURRENT_RUNS` | `2` | worker threads (minimum 1) |
| `IC_MAX_QUEUED_RUNS` | `8` | queued + running investigations before `503` |
| `IC_MAX_INCIDENTS_KEPT` | `200` | finished incidents kept in memory (minimum 10) |
| `IC_MAX_STEPS_CAP` | `30` | upper bound for a request's `max_steps` |
| `IC_TRUST_PROXY` | `false` | use the right-most `X-Forwarded-For` hop as client IP |
| `IC_SYNC_RUNS` | auto | run inside `POST /api/incidents`; auto-on with `VERCEL` / `AWS_LAMBDA_FUNCTION_NAME` |
| `PORT` | `8000` | container listen port (Render, Railway and Fly inject it) |
| `HOST` | `127.0.0.1` | bind address for `incident-commander serve` |

## Project layout

```
.
├── src/incident_commander/
│   ├── agent/           plan → act → observe loop, JSON action parser, prompts
│   ├── llm/             provider interface, offline mock policy, OpenAI-compatible client
│   ├── api/             FastAPI app, incident manager, auth + roles, rate limiting, settings
│   │   └── static/      dashboard (plain HTML/CSS/JS, no build step)
│   ├── ingest/          Alertmanager / PagerDuty / log / metric / deploy parsers
│   ├── scenarios/       packaged synthetic incidents (shipped in the wheel)
│   ├── models.py        Signal, Incident, Hypothesis, RemediationProposal, …
│   ├── triage.py        rule-based severity floor
│   ├── correlate.py     time-window + topology correlation
│   ├── topology.py      service dependency graph
│   ├── tools.py         read-only diagnostic registry
│   ├── environment.py   simulated logs / metrics / deploys / config / deps
│   ├── remediation.py   runbook allowlist, approve / execute
│   ├── postmortem.py    blameless Markdown postmortem
│   ├── engine.py        wires one investigation end to end
│   ├── config.py        IC_* agent settings
│   └── cli.py           `incident-commander run` and `serve`
├── api/index.py         Vercel serverless entrypoint
├── scripts/             smoke_test.py for any running instance
├── tests/               unit, API, scenario and deploy-config tests
├── docs/screenshots/    dashboard screenshots
├── Dockerfile, docker-compose.yml, render.yaml, fly.toml, railway.json, vercel.json
└── .github/workflows/ci.yml
```

## Testing

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy
pytest
```

CI runs the same checks on Python 3.11, 3.12 and 3.13, then builds the Docker image and
smoke-tests every packaged scenario. Locally:

```bash
incident-commander run --scenario memory-leak --approve --approver ci
docker compose up --build --wait
python3 scripts/smoke_test.py http://127.0.0.1:8000
```

## Roadmap

- Pluggable real executors behind the same approval gate (feature-flagged, still allowlisted).
- Derive a minimal simulated environment from raw signal batches so custom incidents can
  investigate beyond "inconclusive".
- Persistent incident store for multi-instance / serverless approve-after-the-fact.
- Slack / PagerDuty notification hooks for pending approvals.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please report vulnerabilities privately via
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
