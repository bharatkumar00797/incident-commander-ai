"""The container smoke test must know the expected runbook of every packaged scenario."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from incident_commander.scenario import list_scenarios, load_scenario

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_test.py"


def test_smoke_script_covers_every_packaged_scenario() -> None:
    spec = importlib.util.spec_from_file_location("smoke_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = module.EXPECTED_RUNBOOKS
    assert set(expected) == set(list_scenarios())
    for name, runbook in expected.items():
        assert load_scenario(name).expected.runbook_id == runbook
