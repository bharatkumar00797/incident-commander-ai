"""Client for any OpenAI-compatible `/chat/completions` endpoint.

Works with OpenAI, Groq, OpenRouter, Together, Ollama (``/v1``), vLLM and LM Studio.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from incident_commander.llm.base import Completion, LLMError, LLMProvider, Message, Usage

_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class OpenAICompatibleProvider(LLMProvider):
    name = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 60.0,
        max_retries: int = 3,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_retries = max(0, max_retries)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.Client(timeout=timeout, headers=headers)

    def complete(self, messages: list[Message], *, temperature: float = 0.0) -> Completion:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature,
        }
        url = f"{self.base_url}/chat/completions"
        last_error: str = "unknown error"
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.post(url, json=payload)
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc.__class__.__name__}"
            else:
                if response.status_code == 200:
                    return self._parse(response.json())
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                if response.status_code not in _RETRYABLE_STATUS:
                    break
            if attempt < self.max_retries:
                time.sleep(min(2**attempt, 8))
        raise LLMError(f"LLM request failed after retries ({last_error})")

    def _parse(self, data: dict[str, Any]) -> Completion:
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("Malformed completion response") from exc
        usage = data.get("usage") or {}
        return Completion(
            content=content,
            model=str(data.get("model", self.model)),
            usage=Usage(
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
            ),
        )
