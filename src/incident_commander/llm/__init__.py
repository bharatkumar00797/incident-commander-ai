"""LLM provider implementations and factory."""

from __future__ import annotations

from incident_commander.config import Settings
from incident_commander.llm.base import Completion, LLMError, LLMProvider, Message, Usage
from incident_commander.llm.mock import MockProvider
from incident_commander.llm.openai_compat import OpenAICompatibleProvider

__all__ = [
    "Completion",
    "LLMError",
    "LLMProvider",
    "Message",
    "MockProvider",
    "OpenAICompatibleProvider",
    "Usage",
    "build_provider",
]

OPENAI_COMPATIBLE = {"openai", "groq", "ollama", "openai-compatible"}


def build_provider(settings: Settings) -> LLMProvider:
    """Create the configured provider; the offline mock is the default."""
    if settings.provider in OPENAI_COMPATIBLE:
        return OpenAICompatibleProvider(
            base_url=settings.base_url, model=settings.model, api_key=settings.api_key
        )
    if settings.provider != "mock":
        raise ValueError(f"unknown provider {settings.provider!r} (use 'mock' or 'openai')")
    return MockProvider()
