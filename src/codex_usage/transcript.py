from __future__ import annotations

import hashlib
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


@dataclass(frozen=True)
class TranscriptConversationTurn:
    session_id: str | None
    transcript_path: str
    conversation_turn_key: str
    user_message_timestamp: str
    user_message_index: int
    start_observed_at: str
    end_observed_at: str
    first_internal_turn_id: str | None
    last_internal_turn_id: str | None
    internal_turn_ids: tuple[str, ...]
    internal_token_deltas: dict[str, int]
    sample_count: int
    model: str | None
    reasoning_effort: str | None
    token_total_end: int
    weekly_used_percent_end: float | None
    weekly_resets_at_end: int | None
    weekly_window_minutes_end: int | None


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


def _parse_token_total(payload: dict[str, Any]) -> int | None:
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    total_raw = _first(info, "total_token_usage", "totalTokenUsage", "total")
    total_usage = _parse_usage(total_raw)
    if total_usage is None:
        return None
    return total_usage.total_tokens


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


def parse_conversation_turns(path: str | Path) -> list[TranscriptConversationTurn]:
    transcript_path = Path(path).expanduser()
    if not transcript_path.exists():
        return []

    session_id: str | None = None
    active_internal_turn_id: str | None = None
    active_model: str | None = None
    active_reasoning_effort: str | None = None
    user_message_index = 0
    output: list[TranscriptConversationTurn] = []
    current: dict[str, Any] | None = None
    latest_total_seen: int | None = None

    def _finalize_current(force_complete: bool) -> None:
        nonlocal current
        if current is None:
            return
        # Only emit completed conversation turns with a usable final total.
        if (not force_complete and not current["has_task_complete"]) or (
            current["token_total_end"] is None
        ):
            current = None
            return
        turn_key_source = (
            f"{session_id or '-'}|{transcript_path}|"
            f"{current['user_message_timestamp']}|{current['user_message_index']}"
        )
        turn_key = hashlib.sha256(turn_key_source.encode("utf-8")).hexdigest()
        internal_ids = tuple(current["internal_turn_ids"])
        output.append(
            TranscriptConversationTurn(
                session_id=session_id,
                transcript_path=str(transcript_path),
                conversation_turn_key=turn_key,
                user_message_timestamp=current["user_message_timestamp"],
                user_message_index=current["user_message_index"],
                start_observed_at=current["start_observed_at"],
                end_observed_at=current["end_observed_at"],
                first_internal_turn_id=internal_ids[0] if internal_ids else None,
                last_internal_turn_id=internal_ids[-1] if internal_ids else None,
                internal_turn_ids=internal_ids,
                internal_token_deltas=dict(current["internal_token_deltas"]),
                sample_count=int(current["sample_count"]),
                model=current["model"],
                reasoning_effort=current["reasoning_effort"],
                token_total_end=int(current["token_total_end"]),
                weekly_used_percent_end=current["weekly_used_percent_end"],
                weekly_resets_at_end=current["weekly_resets_at_end"],
                weekly_window_minutes_end=current["weekly_window_minutes_end"],
            )
        )
        current = None

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
                envelope_type = envelope.get("type")
                timestamp = _as_text(envelope.get("timestamp"))
                payload = envelope.get("payload")

                if envelope_type == "session_meta" and isinstance(payload, dict):
                    if session_id is None:
                        session_id = _as_text(payload.get("id"))
                    continue

                if envelope_type == "turn_context" and isinstance(payload, dict):
                    active_internal_turn_id = _as_text(
                        _first(payload, "turn_id", "turnId")
                    )
                    active_model = _as_text(_first(payload, "model")) or active_model
                    active_reasoning_effort = _turn_effort(payload) or active_reasoning_effort
                    continue

                if envelope_type != "event_msg" or not isinstance(payload, dict):
                    continue
                payload_type = _first(payload, "type")
                if payload_type == "user_message":
                    _finalize_current(force_complete=True)
                    user_message_index += 1
                    if timestamp is None:
                        continue
                    current = {
                        "user_message_timestamp": timestamp,
                        "user_message_index": user_message_index,
                        "start_observed_at": timestamp,
                        "end_observed_at": timestamp,
                        "internal_turn_ids": [],
                        "internal_token_deltas": {},
                        "sample_count": 0,
                        "model": active_model,
                        "reasoning_effort": active_reasoning_effort,
                        "token_total_end": None,
                        "weekly_used_percent_end": None,
                        "weekly_resets_at_end": None,
                        "weekly_window_minutes_end": None,
                        "has_task_complete": False,
                    }
                    continue
                if current is None:
                    continue
                if payload_type in {"token_count", "TokenCount"}:
                    token_total = _parse_token_total(payload)
                    weekly = _parse_weekly_limit(
                        payload.get("rate_limits")
                    ) or _parse_weekly_limit(payload.get("rateLimits"))
                    if token_total is not None:
                        delta = (
                            max(0, token_total)
                            if latest_total_seen is None
                            else max(0, token_total - latest_total_seen)
                        )
                        if active_internal_turn_id is not None:
                            deltas = current["internal_token_deltas"]
                            deltas[active_internal_turn_id] = int(
                                deltas.get(active_internal_turn_id, 0)
                            ) + int(delta)
                        latest_total_seen = token_total
                        current["token_total_end"] = token_total
                        current["sample_count"] = int(current["sample_count"]) + 1
                    if weekly is not None:
                        current["weekly_used_percent_end"] = weekly.used_percent
                        current["weekly_resets_at_end"] = weekly.resets_at
                        current["weekly_window_minutes_end"] = weekly.window_minutes
                    if active_internal_turn_id is not None:
                        ids = current["internal_turn_ids"]
                        if not ids or ids[-1] != active_internal_turn_id:
                            ids.append(active_internal_turn_id)
                    if active_model is not None:
                        current["model"] = active_model
                    if active_reasoning_effort is not None:
                        current["reasoning_effort"] = active_reasoning_effort
                    if timestamp is not None:
                        current["end_observed_at"] = timestamp
                    continue
                if payload_type == "task_complete":
                    current["has_task_complete"] = True
                    if timestamp is not None:
                        current["end_observed_at"] = timestamp
                    continue
    except OSError:
        return []

    _finalize_current(force_complete=False)
    return output


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
