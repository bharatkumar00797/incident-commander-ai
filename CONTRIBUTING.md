# Contributing to Incident Commander

Thanks for your interest in improving Incident Commander. Bug reports, documentation fixes and
pull requests are all welcome.

## Development setup

Requirements: Python 3.11 to 3.13, `git`. Docker is optional (only for the container checks).

```bash
git clone https://github.com/bharatkumar00797/incident-commander-ai.git
cd incident-commander-ai
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Everything runs offline with the default `mock` provider, so no API key is needed for
development or tests. To try a real model, copy `.env.example` to `.env` and set the
`IC_PROVIDER` / `IC_BASE_URL` / `IC_API_KEY` / `IC_MODEL` variables. Never commit `.env` or
any key.

## Checks

Run these before opening a pull request; CI runs the same commands on Python 3.11, 3.12 and 3.13.

```bash
ruff check .            # lint
ruff format .           # format (CI uses: ruff format --check .)
mypy                    # strict type checking of src/incident_commander
pytest                  # full test suite, offline
```

Optional end-to-end checks:

```bash
incident-commander run --scenario bad-deploy --approve --approver "$USER"
incident-commander serve --dev                       # dashboard on http://127.0.0.1:8000
python scripts/smoke_test.py http://127.0.0.1:8000   # health + every scenario through the API
docker compose up --build                            # hardened container
```

## Guidelines

- Keep the mock provider the default: every feature must work without network access or keys.
- New code is fully typed (`mypy --strict` must pass) and comes with tests.
- Diagnostic tools are read-only. Remediation may only propose runbook ids from the allowlist
  with schema-validated params; execution always goes through the approval gate and the
  simulated environment.
- Log lines, alert text and tool output are untrusted data: wrap them before they reach the
  model prompt and never treat them as instructions.
- Anything that touches auth, roles, rate limiting, the body cap or the approval gate needs a
  test that proves the guard holds (see `tests/test_api.py`, `tests/test_remediation.py`).
- Adding a scenario: put a JSON file under `src/incident_commander/scenarios/<name>.json` with
  topology, signals, simulated environment and an expected root cause; update
  `scripts/smoke_test.py` and the tests that keep it in sync.

## Commit style

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(api): add per-client public-demo ownership
fix(remediation): re-check preconditions at execute time
docs: explain the untrusted-data wrapping
test: cover timeline polling cursor
ci: build and smoke-test the Docker image
chore: bump version to 1.0.0
```

Keep commits small and focused, write the subject in the imperative mood, and explain the
"why" in the body when it is not obvious. Update `CHANGELOG.md` under **Unreleased** for
user-visible changes.

## Reporting security issues

Please do not open public issues for vulnerabilities; see [SECURITY.md](SECURITY.md).
