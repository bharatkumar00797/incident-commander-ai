# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-10-09

First stable release.

### Added

- End-to-end incident commander agent: ingest signals, triage severity, correlate into one
  incident, investigate with read-only diagnostic tools, propose allowlisted remediations,
  gate execution on human approval, and draft a blameless postmortem.
- LLM providers: deterministic offline `mock` policy (default) and an OpenAI-compatible client
  (OpenAI, Groq, Ollama, OpenRouter, vLLM) with retries and exponential backoff.
- Rule-based triage floor (error rate, latency, blast radius, customer-facing) that the model
  can escalate but never silently downgrade.
- Correlation over a service dependency graph with ranked root-cause candidates (bad deploy,
  resource exhaustion, dependency failure, config change, traffic spike).
- Read-only diagnostic tools against a simulated environment: `query_logs`, `get_metric`,
  `list_deploys`, `get_config_diff`, `check_dependency`, `describe_service`. Tool output is
  wrapped in `<untrusted-data>` with `<` escaped so log content cannot close the block.
- Runbook allowlist with typed params and preconditions: `rollback_deploy`, `restart_service`,
  `scale_out`, `toggle_feature_flag`, `failover_dependency`. Execution re-checks preconditions
  and runs against the simulator only.
- Six packaged scenarios with known answers: `bad-deploy`, `db-connection-exhaustion`,
  `dependency-outage`, `memory-leak`, `bad-config-push`, `traffic-spike` (each with red
  herrings the mock must refute).
- `incident-commander run` CLI and `incident-commander serve` REST API (FastAPI) with
  background or sync investigations, timeline polling, OpenAPI docs and `/healthz`.
- Access modes `api-key` (responder + approver roles), `dev` and `public-demo`; constant-time
  API-key checks, four-eyes approval audit, per-client ownership in public demo, sliding-window
  rate limits, bounded queue, 256 KB body cap, strict security headers and CSP, opt-in CORS,
  right-most `X-Forwarded-For` hop behind a trusted proxy.
- Static web dashboard: scenario picker, incident list with SEV badges, live timeline,
  hypotheses with confidence bars, Approve & run / Reject UI, postmortem tab and
  `#incident=<id>&tab=<name>` deep links.
- Serverless sync mode (auto-enabled on Vercel / AWS Lambda): investigations finish inside
  `POST /api/incidents` and the response carries the full timeline.
- Deployment: multi-stage non-root Dockerfile with health check, hardened docker compose,
  `render.yaml`, `fly.toml`, `railway.json`, `vercel.json` + `api/index.py`.
- `scripts/smoke_test.py` and a CI job that builds the image and smoke-tests every scenario.
- Documentation: README with architecture and sequence diagrams, security model, deploy guides
  and configuration reference; dashboard screenshots; CONTRIBUTING and SECURITY policies.

[Unreleased]: https://github.com/bharatkumar00797/incident-commander-ai/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/bharatkumar00797/incident-commander-ai/releases/tag/v1.0.0
