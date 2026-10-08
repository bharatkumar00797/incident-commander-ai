"""HTTP service (FastAPI) and web dashboard for Incident Commander."""

from incident_commander.api.app import create_app
from incident_commander.api.settings import ApiSettings

__all__ = ["ApiSettings", "create_app"]
