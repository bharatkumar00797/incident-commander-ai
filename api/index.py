"""Serverless entrypoint (Vercel): exposes the Incident Commander FastAPI app as ``app``.

Serverless functions freeze or discard background threads once a response is sent, so
investigations run synchronously inside ``POST /api/incidents`` (``IC_SYNC_RUNS`` is switched on
automatically when ``VERCEL`` is set, and set here as a fallback). Unless API keys are
configured the deployment is a public demo: packaged scenarios and the offline mock provider,
with runbooks executed only against the simulated environment.

Incidents live in memory per function instance, so the full result is returned in the POST
response; a later approval may land on a fresh instance and get a 404 - fine for a demo.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("IC_SYNC_RUNS", "true")
os.environ.setdefault("IC_MAX_CONCURRENT_RUNS", "1")
os.environ.setdefault("IC_MAX_STEPS_CAP", "20")

from incident_commander.api import create_app  # noqa: E402  (path setup must run first)

app = create_app()
