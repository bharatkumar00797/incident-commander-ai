# Incident Commander AI

[![CI](https://github.com/bharatkumar00797/incident-commander-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/bharatkumar00797/incident-commander-ai/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**An AI agent that runs production incidents from the first alert to the postmortem.**

> Status: early development. The core engine, API, dashboard and deploy configs are in progress.

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

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check . && mypy && pytest
```

## License

[MIT](LICENSE)
