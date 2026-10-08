"""Parsers for the payload shapes on-call tooling actually emits.

Every parser normalises into :class:`~incident_commander.models.Signal` and enforces size caps,
so a noisy or hostile source can't blow up memory or smuggle unbounded text into prompts.
Supported shapes:

* Prometheus Alertmanager webhook (``{"alerts": [...]}``)
* PagerDuty Events API v2 (``{"event_action": ..., "payload": {...}}``)
* plain log lines (``<ISO time> <LEVEL> <service>: <message>``) and JSON log records
* metric samples (``{"type": "metric", ...}``)
* deploy and config-change events (``{"type": "deploy" | "config", ...}``)
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from incident_commander.models import MAX_ATTRS, MAX_TEXT, Severity, Signal, SignalKind

MAX_PAYLOAD_BYTES = 256 * 1024
MAX_BATCH_SIGNALS = 500
MAX_ATTR_KEY = 64
MAX_ATTR_VALUE = 500
MAX_LOG_LINE = 2000

Scalar = str | int | float | bool

_SERVICE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+"
    r"(?P<level>TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|CRITICAL|FATAL)\s+"
    r"\[?(?P<service>[A-Za-z0-9._-]+)\]?:?\s+(?P<msg>.+)$",
    re.IGNORECASE,
)

_SEVERITY_WORDS: dict[str, Severity] = {
    "sev1": Severity.SEV1,
    "p1": Severity.SEV1,
    "page": Severity.SEV1,
    "sev2": Severity.SEV2,
    "p2": Severity.SEV2,
    "critical": Severity.SEV2,
    "high": Severity.SEV2,
    "error": Severity.SEV2,
    "sev3": Severity.SEV3,
    "p3": Severity.SEV3,
    "warning": Severity.SEV3,
    "warn": Severity.SEV3,
    "medium": Severity.SEV3,
    "sev4": Severity.SEV4,
    "p4": Severity.SEV4,
    "info": Severity.SEV4,
    "low": Severity.SEV4,
}

_LOG_LEVEL_SEVERITY: dict[str, Severity | None] = {
    "fatal": Severity.SEV2,
    "critical": Severity.SEV2,
    "error": Severity.SEV3,
    "err": Severity.SEV3,
    "warn": Severity.SEV4,
    "warning": Severity.SEV4,
}


class IngestError(ValueError):
    """Raised when a payload is malformed, unsupported or exceeds a size cap."""


def _clean_service(raw: object) -> str:
    name = _SERVICE_RE.sub("-", str(raw or "").strip()).strip("-")[:100]
    if not name:
        raise IngestError("payload does not name a service")
    return name


def _text(raw: object, limit: int = MAX_TEXT) -> str:
    return " ".join(str(raw or "").split())[:limit]


def _timestamp(raw: object, default: datetime | None = None) -> datetime:
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, int | float) and not isinstance(raw, bool):
        value = datetime.fromtimestamp(float(raw), UTC)
    elif isinstance(raw, str) and raw.strip():
        try:
            value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise IngestError(f"invalid timestamp {raw[:40]!r}") from exc
    elif default is not None:
        value = default
    else:
        raise IngestError("payload is missing a timestamp")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)  # monitoring tools that omit the zone mean UTC
    return value.astimezone(UTC)


def _severity(raw: object) -> Severity | None:
    if raw is None:
        return None
    return _SEVERITY_WORDS.get(str(raw).strip().lower())


def _attrs(*sources: Mapping[str, Any] | None) -> dict[str, Scalar]:
    """Flatten scalar attributes, truncating keys/values and capping the count."""
    out: dict[str, Scalar] = {}
    for source in sources:
        for key, value in (source or {}).items():
            if len(out) >= MAX_ATTRS:
                return out
            name = _text(key, MAX_ATTR_KEY)
            if not name:
                continue
            if isinstance(value, bool | int | float):
                out[name] = value
            elif isinstance(value, str):
                out[name] = value[:MAX_ATTR_VALUE]
    return out


def _build(**fields: Any) -> Signal:
    try:
        return Signal.model_validate(fields)
    except ValidationError as exc:
        raise IngestError(f"invalid signal: {exc.errors()[0]['msg']}") from exc


def _check_size(payload: object) -> None:
    try:
        size = len(json.dumps(payload, default=str))
    except (TypeError, ValueError) as exc:
        raise IngestError("payload is not JSON-serialisable") from exc
    if size > MAX_PAYLOAD_BYTES:
        raise IngestError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")


def parse_alertmanager(payload: Mapping[str, Any]) -> list[Signal]:
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        raise IngestError("Alertmanager payload needs an 'alerts' list")
    signals: list[Signal] = []
    for alert in alerts[:MAX_BATCH_SIGNALS]:
        if not isinstance(alert, Mapping):
            raise IngestError("each alert must be an object")
        labels = alert.get("labels") or {}
        notes = alert.get("annotations") or {}
        if not isinstance(labels, Mapping) or not isinstance(notes, Mapping):
            raise IngestError("alert labels/annotations must be objects")
        name = _text(labels.get("alertname") or "alert", 200)
        summary = _text(notes.get("summary") or notes.get("description") or name)
        status = str(alert.get("status") or payload.get("status") or "firing")
        signals.append(
            _build(
                kind=SignalKind.ALERT,
                service=_clean_service(
                    labels.get("service") or labels.get("job") or labels.get("app")
                ),
                timestamp=_timestamp(alert.get("startsAt")),
                summary=f"{name}: {summary}" if summary != name else name,
                severity_hint=_severity(labels.get("severity")),
                attrs=_attrs(labels, {"status": status, "description": notes.get("description")}),
                source="alertmanager",
            )
        )
    return signals


def parse_pagerduty(payload: Mapping[str, Any]) -> list[Signal]:
    body = payload.get("payload")
    if not isinstance(body, Mapping):
        raise IngestError("PagerDuty event needs a 'payload' object")
    details = body.get("custom_details")
    return [
        _build(
            kind=SignalKind.ALERT,
            service=_clean_service(body.get("component") or body.get("source")),
            timestamp=_timestamp(body.get("timestamp")),
            summary=_text(body.get("summary") or "PagerDuty event"),
            severity_hint=_severity(body.get("severity")),
            attrs=_attrs(
                details if isinstance(details, Mapping) else None,
                {"event_action": payload.get("event_action"), "class": body.get("class")},
            ),
            source="pagerduty",
        )
    ]


def parse_log_line(line: str, *, default_service: str | None = None) -> Signal:
    """Parse ``2026-10-08T03:35:12Z ERROR checkout-api: message`` style lines."""
    raw = line.strip()[:MAX_LOG_LINE]
    match = _LOG_RE.match(raw)
    if match is None:
        raise IngestError("log line is not in '<timestamp> <LEVEL> <service>: <message>' form")
    level = match["level"].lower()
    return _build(
        kind=SignalKind.LOG,
        service=_clean_service(match["service"] or default_service),
        timestamp=_timestamp(match["ts"]),
        summary=_text(match["msg"]),
        severity_hint=_LOG_LEVEL_SEVERITY.get(level),
        attrs={"level": level},
        source="log",
    )


def parse_json_log(record: Mapping[str, Any]) -> Signal:
    level = str(record.get("level") or "info").lower()
    message = record.get("message") or record.get("msg")
    if not message:
        raise IngestError("log record has no message")
    return _build(
        kind=SignalKind.LOG,
        service=_clean_service(record.get("service")),
        timestamp=_timestamp(record.get("timestamp") or record.get("ts")),
        summary=_text(message),
        severity_hint=_LOG_LEVEL_SEVERITY.get(level),
        attrs=_attrs({"level": level}, record.get("fields")),
        source="log",
    )


def parse_metric_sample(sample: Mapping[str, Any]) -> Signal:
    name = _text(sample.get("name"), 100)
    value = sample.get("value")
    if not name:
        raise IngestError("metric sample needs a 'name'")
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise IngestError("metric 'value' must be a number")
    attrs: dict[str, Any] = {"name": name, "value": value}
    for key in ("baseline", "threshold", "unit"):
        if sample.get(key) is not None:
            attrs[key] = sample[key]
    unit = f" {sample['unit']}" if sample.get("unit") else ""
    return _build(
        kind=SignalKind.METRIC,
        service=_clean_service(sample.get("service")),
        timestamp=_timestamp(sample.get("timestamp")),
        summary=f"{name}={value:g}{unit}",
        attrs=_attrs(attrs),
        source=_text(sample.get("source") or "metrics", 100),
    )


def parse_change_event(event: Mapping[str, Any]) -> Signal:
    kind = str(event.get("type") or "").lower()
    service = _clean_service(event.get("service"))
    who = _text(event.get("by") or event.get("author") or "unknown", 100)
    if kind == "deploy":
        version = _text(event.get("version"), 100)
        if not version:
            raise IngestError("deploy event needs a 'version'")
        previous = _text(event.get("previous_version"), 100)
        summary = f"deployed {version}" + (f" (from {previous})" if previous else "")
        attrs = {
            "version": version,
            "previous_version": previous,
            "by": who,
            "commit": event.get("commit"),
            "change": event.get("summary"),
        }
        signal_kind = SignalKind.DEPLOY
    elif kind == "config":
        key = _text(event.get("key"), 200)
        if not key:
            raise IngestError("config event needs a 'key'")
        summary = f"config {key}: {_text(event.get('old'), 100)} -> {_text(event.get('new'), 100)}"
        attrs = {"key": key, "old": event.get("old"), "new": event.get("new"), "by": who}
        signal_kind = SignalKind.CONFIG
    else:
        raise IngestError(f"unsupported change event type {kind!r}")
    return _build(
        kind=signal_kind,
        service=service,
        timestamp=_timestamp(event.get("timestamp")),
        summary=f"{summary} by {who}",
        attrs=_attrs({k: v for k, v in attrs.items() if v not in (None, "")}),
        source=_text(event.get("source") or "change-feed", 100),
    )


def ingest_event(payload: object) -> list[Signal]:
    """Detect the payload shape and parse it. A bare string is treated as a log line."""
    if isinstance(payload, str):
        return [parse_log_line(payload)]
    if not isinstance(payload, Mapping):
        raise IngestError("payload must be a JSON object or a log line string")
    _check_size(payload)
    if "alerts" in payload:
        return parse_alertmanager(payload)
    if "event_action" in payload and "payload" in payload:
        return parse_pagerduty(payload)
    kind = str(payload.get("type") or "").lower()
    if kind == "metric":
        return [parse_metric_sample(payload)]
    if kind in {"deploy", "config"}:
        return [parse_change_event(payload)]
    if kind == "log":
        line = payload.get("line")
        if isinstance(line, str):
            return [parse_log_line(line, default_service=payload.get("service"))]
        return [parse_json_log(payload)]
    raise IngestError("unrecognised payload shape")


def ingest_batch(payloads: Iterable[object]) -> list[Signal]:
    """Parse many payloads into a time-ordered list, enforcing the batch cap."""
    signals: list[Signal] = []
    for payload in payloads:
        signals.extend(ingest_event(payload))
        if len(signals) > MAX_BATCH_SIGNALS:
            raise IngestError(f"batch exceeds {MAX_BATCH_SIGNALS} signals")
    return sorted(signals, key=lambda s: s.timestamp)
