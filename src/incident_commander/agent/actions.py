"""Parsing of the model's JSON action replies."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class ActionParseError(ValueError):
    pass


@dataclass(frozen=True)
class Action:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    thought: str = ""

    @property
    def is_finish(self) -> bool:
        return self.tool == "finish"


def _first_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for start, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_action(text: str) -> Action:
    """Extract a single action from a model reply.

    Accepts bare JSON, JSON inside a markdown fence, or JSON embedded in prose,
    because real models are not always perfectly obedient.
    """
    text = text.strip()
    data: dict[str, Any] | None = None
    fence = _FENCE_RE.search(text)
    if fence:
        try:
            loaded = json.loads(fence.group(1))
            data = loaded if isinstance(loaded, dict) else None
        except json.JSONDecodeError:
            data = None
    if data is None:
        data = _first_json_object(text)
    if data is None:
        raise ActionParseError("Reply did not contain a JSON object")

    tool = data.get("tool") or data.get("action")
    if not isinstance(tool, str) or not tool.strip():
        raise ActionParseError("JSON reply is missing a 'tool' field")
    args = data.get("args", data.get("arguments", {}))
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ActionParseError("'args' must be a JSON object")
    thought = data.get("thought", "")
    return Action(tool=tool.strip(), args=args, thought=str(thought))
