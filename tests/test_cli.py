from pathlib import Path

import pytest

from incident_commander import __version__
from incident_commander.cli import main


def test_version_flag_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "incident commander" in capsys.readouterr().out.lower()


def test_lists_scenarios(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scenarios"]) == 0
    assert "bad-deploy" in capsys.readouterr().out


def test_run_proposes_without_executing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IC_PROVIDER", raising=False)
    assert main(["run", "--scenario", "bad-deploy", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Triage: SEV2" in out and "rollback_deploy" in out and "Not executed" in out
    assert (tmp_path / "postmortem.md").exists() and (tmp_path / "incident.json").exists()


def test_run_with_approval_resolves(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "run",
            "--scenario",
            "bad-deploy",
            "--approve",
            "--approver",
            "bharat",
            "--quiet",
            "--out",
            str(tmp_path),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Executed (simulated)" in out and "Status: resolved" in out


def test_unknown_scenario_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--scenario", "nope"]) == 2
    assert "unknown scenario" in capsys.readouterr().err
