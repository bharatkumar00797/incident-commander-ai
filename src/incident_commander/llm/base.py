"""Provider-agnostic chat completion interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

Role = Literal["system", "user", "assistant"]


class LLMError(RuntimeError):
    """Raised when a provider cannot produce a completion."""


@dataclass(frozen=True)
class Message:
    role: Role
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class Completion:
    content: str
    model: str
    usage: Usage = field(default_factory=Usage)


class LLMProvider(ABC):
    """Minimal contract every model backend implements.

    The agent only needs plain chat completions; tool calls are expressed as JSON
    in the message body so that any OpenAI-compatible model (including small local
    ones without native function calling) can drive the agent.
    """

    name: str = "base"

    @abstractmethod
    def complete(self, messages: list[Message], *, temperature: float = 0.0) -> Completion:
        """Return the assistant reply for the given conversation."""
