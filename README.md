# Incident Commander AI

[![CI](https://github.com/bharatkumar00797/incident-commander-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/bharatkumar00797/incident-commander-ai/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**An AI agent that runs production incidents from the first alert to the postmortem.**

> Status: engine, CLI, HTTP API and web dashboard are working end to end; deploy configs are in progress.

## What it does

Incident Commander is an LLM-driven agent for on-call and SRE teams. It:

1. **Ingests signals** – alerts, log lines, metrics and deploy/config events.
2. **Triages severity** – rule-based floor (SLO burn, error rate, blast radius) that the model can raise but never silently lower.
3. **Correlates** related signals into one incident using time windows and the service dependency graph.
4. **Investigates** – forms root-cause hypotheses and tests them with **read-only** diagnostic tools.
5. **Proposes remediation** from a **runbook allowlist**; nothing runs without explicit human approval.
6. **Keeps a timeline** of every signal, hypothesis, tool call, approval and action.
7. **Drafts a blameless postmortem** with impact, root cause and action items.

It works fully offline with a deterministic mock LLM provider and synthetic incident scenarios, and can be
pointed at any OpenAI-compatible model (OpenAI, Groq, Ollama) via environment variables.

## Quick start

```bash
pip install -e .
incident-commander scenarios                       # list bundled synthetic incidents
incident-commander run --scenario bad-deploy       # triage, investigate, propose a fix
incident-commander run --scenario bad-deploy --approve --approver "$USER"   # approve + run (simulated)
```

Each run prints the triage decision, every diagnostic step, the ranked hypotheses and the proposed
runbook, and writes `incident-output/postmortem.md` and `incident-output/incident.json`.

Bundled scenarios: `bad-deploy`, `db-connection-exhaustion` (with a red-herring deploy that has to be
ruled out) and `dependency-outage` (third-party identity provider timing out across services).

### Using a real model

The default provider is a deterministic offline policy, so no API key is needed. To use a hosted or
local model, set (see `.env.example`):

```bash
export IC_PROVIDER=openai            # any OpenAI-compatible endpoint
export IC_BASE_URL=https://api.groq.com/openai/v1   # or http://localhost:11434/v1 for Ollama
export IC_MODEL=llama-3.1-8b-instant
export IC_API_KEY=...                # never committed; read from the environment only
```

## API and dashboard

```bash
incident-commander serve                 # http://127.0.0.1:8000 (honours $PORT and $HOST)
incident-commander serve --dev           # local development: no API key needed
```

Open `http://127.0.0.1:8000/` for the dashboard (pick a scenario, watch the live timeline, review
hypotheses, approve or reject the proposed runbook, read the postmortem) or `/docs` for the
OpenAPI reference.

| Method | Path | Purpose |
|--------|------|---------|
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

Access modes:

- **Public demo** (default, no keys): packaged scenarios and the offline mock provider only.
  Approvals are allowed because every runbook runs against the simulated environment, and the
  API and dashboard label it as simulated.
- **API keys**: `IC_API_KEYS` (responder: open and follow own incidents) and `IC_APPROVER_KEYS`
  (approver: see all incidents, approve or reject). Send `X-API-Key: <key>` or
  `Authorization: Bearer <key>`.
- **Dev mode**: `IC_DEV_MODE=true` or `--dev`, no authentication; for localhost only.

Investigations run in a bounded background worker pool (`POST` answers `202`, poll the timeline);
with `IC_SYNC_RUNS=true` (automatic on Vercel / AWS Lambda) they finish inside the request
(`200` with the full timeline). Rate limits, a 256 KB body cap, strict CSP and the other settings
are listed in `.env.example`.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check . && mypy && pytest
```

## License

[MIT](LICENSE)
