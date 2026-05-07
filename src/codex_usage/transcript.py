from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class WeeklyLimit:
    used_percent: float
    resets_at: int | None = None
    window_minutes: int | None = None
    source: str = "transcript_raw"


@dataclass(frozen=True)
class TranscriptSnapshot:
    path: str
    token_event_timestamp: str | None = None
    total_usage: TokenUsage | None = None
    last_usage: TokenUsage | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    model_context_window: int | None = None
    weekly_limit: WeeklyLimit | None = None
    plan_type: str | None = None
    raw_token_count: dict[str, Any] | None = None
    error: str | None = None


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value if value else None


def _parse_usage(raw: Any) -> TokenUsage | None:
    if not isinstance(raw, dict):
        return None
    return TokenUsage(
        input_tokens=_as_int(_first(raw, "input_tokens", "inputTokens")),
        cached_input_tokens=_as_int(
            _first(raw, "cached_input_tokens", "cachedInputTokens")
        ),
        output_tokens=_as_int(_first(raw, "output_tokens", "outputTokens")),
        reasoning_output_tokens=_as_int(
            _first(raw, "reasoning_output_tokens", "reasoningOutputTokens")
        ),
        total_tokens=_as_int(_first(raw, "total_tokens", "totalTokens")),
    )


def _parse_weekly_limit(rate_limits: Any) -> WeeklyLimit | None:
    if not isinstance(rate_limits, dict):
        return None

    secondary = _first(rate_limits, "secondary")
    if not isinstance(secondary, dict):
        by_id = _first(rate_limits, "rateLimitsByLimitId", "rate_limits_by_limit_id")
        if isinstance(by_id, dict):
            codex = by_id.get("codex")
            if isinstance(codex, dict):
                secondary = codex.get("secondary")

    if not isinstance(secondary, dict):
        return None

    used_percent = _as_float(_first(secondary, "used_percent", "usedPercent"))
    if used_percent is None:
        return None

    return WeeklyLimit(
        used_percent=used_percent,
        resets_at=_as_int(_first(secondary, "resets_at", "resetsAt")),
        window_minutes=_as_int(
            _first(
                secondary,
                "window_minutes",
                "windowMinutes",
                "window_duration_mins",
                "windowDurationMins",
            )
        ),
        source="transcript_raw",
    )


def _turn_effort(payload: dict[str, Any]) -> str | None:
    effort = _as_text(_first(payload, "effort", "reasoning_effort", "reasoningEffort"))
    if effort is not None:
        return effort
    collaboration = payload.get("collaboration_mode")
    if isinstance(collaboration, dict):
        settings = collaboration.get("settings")
        if isinstance(settings, dict):
            return _as_text(
                _first(settings, "reasoning_effort", "reasoningEffort", "effort")
            )
    return None


def parse_transcript(path: str | Path, turn_id: str | None = None) -> TranscriptSnapshot:
    transcript_path = Path(path).expanduser()
    if not transcript_path.exists():
        return TranscriptSnapshot(path=str(transcript_path), error="missing transcript")

    latest_model: str | None = None
    latest_reasoning_effort: str | None = None
    latest_token_timestamp: str | None = None
    latest_total_usage: TokenUsage | None = None
    latest_last_usage: TokenUsage | None = None
    latest_context_window: int | None = None
    latest_weekly_limit: WeeklyLimit | None = None
    latest_plan_type: str | None = None
    latest_payload: dict[str, Any] | None = None
    target_turn_active = turn_id is None

    try:
        with transcript_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    envelope = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(envelope, dict):
                    continue

                payload = envelope.get("payload")
                if envelope.get("type") == "turn_context" and isinstance(payload, dict):
                    payload_turn_id = _first(payload, "turn_id", "turnId")
                    if turn_id is not None:
                        if target_turn_active and payload_turn_id != turn_id:
                            break
                        target_turn_active = payload_turn_id == turn_id
                    if target_turn_active:
                        model = _as_text(_first(payload, "model"))
                        effort = _turn_effort(payload)
                        if model is not None:
                            latest_model = model
                        if effort is not None:
                            latest_reasoning_effort = effort
                    continue

                if envelope.get("type") != "event_msg" or not isinstance(payload, dict):
                    continue
                payload_type = _first(payload, "type")
                if payload_type not in {"token_count", "TokenCount"}:
                    continue
                if not target_turn_active:
                    continue

                latest_token_timestamp = _first(envelope, "timestamp")
                latest_payload = payload

                info = payload.get("info")
                if isinstance(info, dict):
                    total_usage = _parse_usage(
                        _first(info, "total_token_usage", "totalTokenUsage", "total")
                    )
                    last_usage = _parse_usage(
                        _first(info, "last_token_usage", "lastTokenUsage", "last")
                    )
                    if total_usage is not None:
                        latest_total_usage = total_usage
                    if last_usage is not None:
                        latest_last_usage = last_usage
                    context_window = _as_int(
                        _first(info, "model_context_window", "modelContextWindow")
                    )
                    if context_window is not None:
                        latest_context_window = context_window

                weekly_limit = _parse_weekly_limit(payload.get("rate_limits"))
                if weekly_limit is None:
                    weekly_limit = _parse_weekly_limit(payload.get("rateLimits"))
                if weekly_limit is not None:
                    latest_weekly_limit = weekly_limit

                plan_type = _first(payload, "plan_type", "planType")
                if isinstance(plan_type, str):
                    latest_plan_type = plan_type
    except OSError as exc:
        return TranscriptSnapshot(path=str(transcript_path), error=str(exc))

    if (
        latest_total_usage is None
        and latest_last_usage is None
        and latest_weekly_limit is None
    ):
        return TranscriptSnapshot(path=str(transcript_path), error="no token_count event")

    return TranscriptSnapshot(
        path=str(transcript_path),
        token_event_timestamp=latest_token_timestamp,
        total_usage=latest_total_usage,
        last_usage=latest_last_usage,
        model=latest_model,
        reasoning_effort=latest_reasoning_effort,
        model_context_window=latest_context_window,
        weekly_limit=latest_weekly_limit,
        plan_type=latest_plan_type,
        raw_token_count=latest_payload,
    )
