from __future__ import annotations

import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

import incident_commander
from incident_commander.api import ApiSettings, create_app
from incident_commander.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]


def test_package_version_matches_pyproject() -> None:
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["project"]["version"] == incident_commander.__version__


def test_version_is_reported_by_api_and_cli() -> None:
    app = create_app(ApiSettings(dev_mode=True))
    with TestClient(app) as client:
        assert client.get("/healthz").json()["version"] == incident_commander.__version__
        assert client.get("/api/config").json()["version"] == incident_commander.__version__
        assert client.get("/openapi.json").json()["info"]["version"] == incident_commander.__version__
    version_action = next(a for a in build_parser()._actions if a.dest == "version")
    assert version_action.version == f"%(prog)s {incident_commander.__version__}"
