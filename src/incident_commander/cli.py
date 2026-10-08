"""Command-line entry point."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from incident_commander import __version__
from incident_commander.agent.loop import Step
from incident_commander.config import Settings
from incident_commander.engine import approve_and_execute, run_scenario
from incident_commander.llm import build_provider
from incident_commander.scenario import list_scenarios, load_scenario


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="incident-commander",
        description="AI incident commander: triage, investigate and remediate incidents.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="replay a scenario through the full incident pipeline")
    run.add_argument("--scenario", required=True, help="scenario name (see `scenarios`)")
    run.add_argument("--provider", help="mock (default) or openai/groq/ollama (uses IC_* env)")
    run.add_argument("--max-steps", type=int, help="investigation step cap (default 20)")
    run.add_argument(
        "--approve", action="store_true", help="approve the proposal and run it (simulated)"
    )
    run.add_argument("--approver", default=None, help="name recorded on the approval")
    run.add_argument("--out", type=Path, default=Path("incident-output"), help="output directory")
    run.add_argument("--quiet", action="store_true", help="only print the final summary")

    sub.add_parser("scenarios", help="list bundled scenarios")

    serve = sub.add_parser("serve", help="start the REST API and web dashboard")
    serve.add_argument(
        "--host",
        default=os.getenv("HOST", "127.0.0.1"),
        help="bind address (default: $HOST or 127.0.0.1)",
    )
    serve.add_argument("--port", type=int, default=None, help="port (default: $PORT or 8000)")
    serve.add_argument("--dev", action="store_true", help="dev mode: no API key required")
    return parser


def _print_step(step: Step) -> None:
    mark = "ok " if step.ok else "ERR"
    args = json.dumps(step.args, default=str)
    print(f"  [{step.index:02d}] {mark} {step.tool} {args[:110]}")


def _cmd_run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if args.provider:
        settings = replace(settings, provider=args.provider.lower())
    if args.max_steps:
        settings = replace(settings, max_steps=args.max_steps)
    try:
        scenario = load_scenario(args.scenario)
        provider = build_provider(settings)
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    say = (lambda *_: None) if args.quiet else print
    say(f"Scenario: {scenario.name} - {scenario.title}")
    run = run_scenario(
        scenario,
        provider,
        max_steps=settings.max_steps,
        on_step=None if args.quiet else _print_step,
    )
    inc = run.incident
    say(f"\nTriage: {run.triage.severity}")
    for reason in run.triage.reasons:
        say(f"  - {reason}")
    say("\nHypotheses:")
    for h in inc.hypotheses:
        say(f"  {h.id} [{h.status:9}] {h.confidence:.2f} {h.suspected_cause} on {h.service}")
    print(f"\nInvestigation: {run.investigation.status} - {run.investigation.summary}")
    for p in inc.proposals:
        params = ", ".join(f"{k}={v}" for k, v in p.params.items())
        print(f"Proposal {p.id}: {p.runbook_id}({params}) [risk {p.risk}] -> {p.status}")

    if args.approve:
        approver = args.approver or getpass.getuser() or "on-call"
        for outcome in approve_and_execute(run, approver):
            print(f"Executed (simulated): {outcome}")
    elif inc.proposals:
        print("Not executed: re-run with --approve to approve the proposal (simulated runbook).")

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "postmortem.md").write_text(run.postmortem, encoding="utf-8")
    (args.out / "incident.json").write_text(inc.model_dump_json(indent=2), encoding="utf-8")
    print(f"Status: {inc.status} | postmortem: {args.out / 'postmortem.md'}")
    return 0 if run.investigation.status == "concluded" else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from incident_commander.api import ApiSettings, create_app

    try:
        port = args.port or int(os.getenv("PORT") or 8000)
        settings = ApiSettings.from_env()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.dev:
        settings = replace(settings, dev_mode=True)
    if settings.access_mode == "dev" and args.host not in {"127.0.0.1", "::1", "localhost"}:
        print("warning: dev mode without API keys is exposed beyond localhost", file=sys.stderr)
    mode = {
        "api-key": "API-key auth (responder/approver roles)",
        "dev": "dev mode (no auth)",
        "public-demo": "public demo (packaged scenarios + mock provider, simulated runbooks)",
    }[settings.access_mode]
    sync = " | sync investigations" if settings.sync_runs else ""
    print(
        f"Incident Commander {__version__} on http://{args.host}:{port} | {mode}{sync}", flush=True
    )
    uvicorn.run(create_app(settings), host=args.host, port=port, log_level="info")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "scenarios":
        for name in list_scenarios():
            print(f"{name:28} {load_scenario(name).title}")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
