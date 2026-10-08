from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from incident_commander.llm.base import Completion, LLMError, LLMProvider, Message
from incident_commander.models import Signal

T0 = datetime(2026, 10, 8, 3, 30, tzinfo=UTC)


def make_signal(**overrides: Any) -> Signal:
    data: dict[str, Any] = {
        "kind": "alert",
        "service": "checkout-api",
        "timestamp": T0,
        "summary": "5xx rate above 5%",
    }
    data.update(overrides)
    return Signal.model_validate(data)


class ScriptedProvider(LLMProvider):
    """Replays canned replies; raises LLMError for entries that are exceptions."""

    name = "scripted"

    def __init__(self, replies: list[str | dict[str, Any] | Exception]) -> None:
        self.replies = list(replies)
        self.seen: list[list[Message]] = []

    def complete(self, messages: list[Message], *, temperature: float = 0.0) -> Completion:
        self.seen.append(list(messages))
        reply = self.replies.pop(0) if self.replies else {"tool": "conclude", "args": {}}
        if isinstance(reply, Exception):
            raise LLMError(str(reply))
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return Completion(content=text, model="scripted")


@pytest.fixture
def scripted() -> Callable[[list[str | dict[str, Any] | Exception]], ScriptedProvider]:
    return ScriptedProvider
