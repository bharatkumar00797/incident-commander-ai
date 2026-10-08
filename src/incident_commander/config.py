"""Runtime configuration loaded from environment variables (secrets only ever come from env)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    provider: str = "mock"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    max_steps: int = 20

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            provider=os.getenv("IC_PROVIDER", "mock").strip().lower(),
            base_url=os.getenv("IC_BASE_URL", cls.base_url),
            api_key=os.getenv("IC_API_KEY", ""),
            model=os.getenv("IC_MODEL", cls.model),
            max_steps=env_int("IC_MAX_STEPS", cls.max_steps),
        )
