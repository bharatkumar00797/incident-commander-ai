from __future__ import annotations

import json

import httpx
import pytest

from incident_commander.agent.actions import ActionParseError, parse_action
from incident_commander.config import Settings
from incident_commander.llm import (
    LLMError,
    Message,
    MockProvider,
    OpenAICompatibleProvider,
    build_provider,
)


@pytest.mark.parametrize(
    "reply",
    [
        '{"tool": "list_deploys", "args": {"service": "a"}}',
        'Sure!\n```json\n{"tool": "list_deploys", "args": {"service": "a"}}\n```',
        'I will check deploys {"action": "list_deploys", "arguments": {"service": "a"}} now',
    ],
)
def test_tolerant_action_parser(reply: str) -> None:
    action = parse_action(reply)
    assert action.tool == "list_deploys" and action.args == {"service": "a"}


@pytest.mark.parametrize("reply", ["no json here", '{"args": {}}', '{"tool": "x", "args": [1]}'])
def test_parser_rejects_bad_replies(reply: str) -> None:
    with pytest.raises(ActionParseError):
        parse_action(reply)


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def test_openai_compatible_success_and_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("incident_commander.llm.openai_compat.time.sleep", lambda _s: None)
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(503, text="busy")
        body = {
            "model": "llama-3.1-8b",
            "choices": [{"message": {"content": '{"tool": "conclude"}'}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }
        return httpx.Response(200, json=body)

    provider = OpenAICompatibleProvider(
        base_url="https://api.groq.com/openai/v1",
        model="llama-3.1-8b",
        client=_client(httpx.MockTransport(handler)),
    )
    completion = provider.complete([Message("user", "hi")])
    assert completion.content == '{"tool": "conclude"}' and completion.usage.total_tokens == 15
    assert len(calls) == 2 and calls[0]["model"] == "llama-3.1-8b"


def test_openai_compatible_does_not_retry_client_errors() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="bad key")

    provider = OpenAICompatibleProvider(
        base_url="http://localhost:11434/v1",
        model="m",
        client=_client(httpx.MockTransport(handler)),
    )
    with pytest.raises(LLMError, match="401"):
        provider.complete([Message("user", "hi")])
    assert calls == 1


def test_provider_factory() -> None:
    assert isinstance(build_provider(Settings()), MockProvider)
    assert isinstance(build_provider(Settings(provider="groq")), OpenAICompatibleProvider)
    with pytest.raises(ValueError):
        build_provider(Settings(provider="magic"))
    with pytest.raises(ValueError):
        OpenAICompatibleProvider(base_url="file:///etc", model="m")
