from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import db_path


DEFAULT_RECENT_WINDOW_HOURS = 48.0
DEFAULT_MISSING_LIMIT = 10


@dataclass(frozen=True)
class TranscriptCoverageRecord:
    transcript_path: str
    session_id: str | None
    cwd: str | None
    model: str | None
    reasoning_effort: str | None
    last_timestamp: str | None
    mtime: float
    has_token_count: bool
    has_task_complete: bool


@dataclass(frozen=True)
class CoveragePresence:
    in_sessions: bool
    in_samples: bool
    in_raw_observation_layer: bool
    in_completed_conversation_turns: bool


def build_coverage_diagnostics(
    *,
    home: Path,
    codex_home: Path,
    since_hours: float = DEFAULT_RECENT_WINDOW_HOURS,
    missing_limit: int = DEFAULT_MISSING_LIMIT,
) -> dict[str, Any]:
    safe_since_hours = max(0.0, float(since_hours))
    safe_missing_limit = max(0, int(missing_limit))

    records = _scan_transcript_records(codex_home)
    presence_index = _load_db_presence(home)

    cutoff = time.time() - safe_since_hours * 3600
    recent_records = [record for record in records if record.mtime >= cutoff]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "home": str(home),
        "codex_home": str(codex_home),
        "recent_window_hours": safe_since_hours,
        "missing_limit": safe_missing_limit,
        "coverage": {
            "all_history": _build_window_summary(records, presence_index),
            "recent_window": _build_window_summary(recent_records, presence_index),
            "recent_missing_examples": _recent_missing_examples(
                records=recent_records,
                presence_index=presence_index,
                missing_limit=safe_missing_limit,
            ),
        },
    }


def _build_window_summary(
    records: list[TranscriptCoverageRecord],
    presence_index: dict[str, set[str]],
) -> dict[str, Any]:
    session_keys = {
        record.session_id or f"path:{record.transcript_path}" for record in records
    }
    token_records = [record for record in records if record.has_token_count]
    token_and_complete = [
        record
        for record in token_records
        if record.has_token_count and record.has_task_complete
    ]
    completed_eligible_total = len(token_and_complete)

    present_in_sessions_count = 0
    present_in_samples_count = 0
    present_in_raw_count = 0
    present_in_completed_count = 0
    for record in token_records:
        presence = _presence(record, presence_index)
        if presence.in_sessions:
            present_in_sessions_count += 1
        if presence.in_samples:
            present_in_samples_count += 1
        if presence.in_raw_observation_layer:
            present_in_raw_count += 1
        if record.has_task_complete and presence.in_completed_conversation_turns:
            present_in_completed_count += 1

    token_total = len(token_records)
    return {
        "transcript_files_discovered_count": len(records),
        "sessions_discovered_count": len(session_keys),
        "with_token_count_count": token_total,
        "with_token_count_and_task_complete_count": len(token_and_complete),
        "present_in_sessions_count": present_in_sessions_count,
        "present_in_samples_count": present_in_samples_count,
        "present_in_raw_observation_layer_count": present_in_raw_count,
        "present_in_completed_conversation_turns_count": present_in_completed_count,
        "missing_from_sessions_count": token_total - present_in_sessions_count,
        "missing_from_raw_observation_layer_count": token_total - present_in_raw_count,
        "missing_from_conversation_turns_count": (
            completed_eligible_total - present_in_completed_count
        ),
    }


def _recent_missing_examples(
    *,
    records: list[TranscriptCoverageRecord],
    presence_index: dict[str, set[str]],
    missing_limit: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for record in records:
        if not record.has_token_count:
            continue
        presence = _presence(record, presence_index)
        missing_from_sessions = not presence.in_sessions
        missing_from_conversation_turns = (
            record.has_task_complete and (not presence.in_completed_conversation_turns)
        )
        if not (missing_from_sessions or missing_from_conversation_turns):
            continue
        candidates.append(
            {
                "session_id": record.session_id,
                "cwd": record.cwd,
                "model": record.model,
                "reasoning_effort": record.reasoning_effort,
                "last_timestamp": record.last_timestamp,
                "transcript_path": record.transcript_path,
                "has_task_complete": record.has_task_complete,
                "present_in_sessions": presence.in_sessions,
                "present_in_samples": presence.in_samples,
                "present_in_completed_conversation_turns": (
                    presence.in_completed_conversation_turns
                ),
                "missing_from_sessions": missing_from_sessions,
                "missing_from_conversation_turns": missing_from_conversation_turns,
                "_mtime": record.mtime,
            }
        )
    candidates.sort(
        key=lambda item: (
            item.get("last_timestamp") or "",
            float(item.get("_mtime") or 0.0),
            item.get("transcript_path") or "",
        ),
        reverse=True,
    )
    output = candidates[:missing_limit] if missing_limit > 0 else []
    for item in output:
        item.pop("_mtime", None)
    return output


def _scan_transcript_records(codex_home: Path) -> list[TranscriptCoverageRecord]:
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.exists():
        return []
    records: list[TranscriptCoverageRecord] = []
    for path in sessions_dir.rglob("rollout-*.jsonl"):
        records.append(_scan_one_transcript(path))
    records.sort(key=lambda record: (record.mtime, record.transcript_path))
    return records


def _scan_one_transcript(path: Path) -> TranscriptCoverageRecord:
    session_id: str | None = None
    cwd: str | None = None
    active_model: str | None = None
    active_reasoning_effort: str | None = None
    last_timestamp: str | None = None
    has_token_count = False
    has_task_complete = False

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0

    try:
        with path.open("r", encoding="utf-8") as handle:
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

                timestamp = envelope.get("timestamp")
                if isinstance(timestamp, str) and timestamp:
                    last_timestamp = timestamp

                payload = envelope.get("payload")
                envelope_type = envelope.get("type")
                if envelope_type == "session_meta" and isinstance(payload, dict):
                    if session_id is None:
                        session_id = _as_non_empty_text(payload.get("id"))
                    if cwd is None:
                        cwd = _as_non_empty_text(payload.get("cwd"))
                    continue
                if envelope_type == "turn_context" and isinstance(payload, dict):
                    active_model = _as_non_empty_text(payload.get("model")) or active_model
                    effort = _turn_effort(payload)
                    active_reasoning_effort = effort or active_reasoning_effort
                    continue
                if envelope_type != "event_msg" or not isinstance(payload, dict):
                    continue

                payload_type = payload.get("type")
                if payload_type in {"token_count", "TokenCount"}:
                    has_token_count = True
                if payload_type == "task_complete":
                    has_task_complete = True
    except OSError:
        pass

    return TranscriptCoverageRecord(
        transcript_path=str(path),
        session_id=session_id,
        cwd=cwd,
        model=active_model,
        reasoning_effort=active_reasoning_effort,
        last_timestamp=last_timestamp,
        mtime=mtime,
        has_token_count=has_token_count,
        has_task_complete=has_task_complete,
    )


def _load_db_presence(home: Path) -> dict[str, set[str]]:
    path = db_path(home)
    if not path.exists():
        return {
            "session_paths": set(),
            "sample_paths": set(),
            "conversation_paths": set(),
            "session_ids": set(),
            "sample_session_ids": set(),
            "conversation_session_ids": set(),
        }

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {
            "session_paths": set(),
            "sample_paths": set(),
            "conversation_paths": set(),
            "session_ids": set(),
            "sample_session_ids": set(),
            "conversation_session_ids": set(),
        }

    try:
        return {
            "session_paths": _safe_query_text_set(
                conn,
                "SELECT transcript_path FROM sessions WHERE transcript_path IS NOT NULL AND transcript_path != ''",
            ),
            "sample_paths": _safe_query_text_set(
                conn,
                "SELECT transcript_path FROM samples WHERE transcript_path IS NOT NULL AND transcript_path != ''",
            ),
            "conversation_paths": _safe_query_text_set(
                conn,
                "SELECT transcript_path FROM conversation_turns WHERE completed = 1 AND transcript_path IS NOT NULL AND transcript_path != ''",
            ),
            "session_ids": _safe_query_text_set(
                conn,
                "SELECT session_id FROM sessions WHERE session_id IS NOT NULL AND session_id != ''",
            ),
            "sample_session_ids": _safe_query_text_set(
                conn,
                "SELECT session_id FROM samples WHERE session_id IS NOT NULL AND session_id != ''",
            ),
            "conversation_session_ids": _safe_query_text_set(
                conn,
                "SELECT session_id FROM conversation_turns WHERE completed = 1 AND session_id IS NOT NULL AND session_id != ''",
            ),
        }
    finally:
        conn.close()


def _query_text_set(conn: sqlite3.Connection, query: str) -> set[str]:
    rows = conn.execute(query).fetchall()
    values: set[str] = set()
    for row in rows:
        value = row[0] if row else None
        if isinstance(value, str) and value:
            values.add(value)
    return values


def _safe_query_text_set(conn: sqlite3.Connection, query: str) -> set[str]:
    try:
        return _query_text_set(conn, query)
    except sqlite3.Error:
        return set()


def _presence(
    record: TranscriptCoverageRecord, presence_index: dict[str, set[str]]
) -> CoveragePresence:
    transcript_path = record.transcript_path
    session_id = record.session_id

    in_sessions = transcript_path in presence_index["session_paths"] or (
        bool(session_id) and session_id in presence_index["session_ids"]
    )
    in_samples = transcript_path in presence_index["sample_paths"] or (
        bool(session_id) and session_id in presence_index["sample_session_ids"]
    )
    in_raw = in_sessions or in_samples
    in_completed = transcript_path in presence_index["conversation_paths"] or (
        bool(session_id) and session_id in presence_index["conversation_session_ids"]
    )
    return CoveragePresence(
        in_sessions=bool(in_sessions),
        in_samples=bool(in_samples),
        in_raw_observation_layer=bool(in_raw),
        in_completed_conversation_turns=bool(in_completed),
    )


def _as_non_empty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value if value else None


def _turn_effort(payload: dict[str, Any]) -> str | None:
    effort = _as_non_empty_text(
        payload.get("effort")
        or payload.get("reasoning_effort")
        or payload.get("reasoningEffort")
    )
    if effort is not None:
        return effort
    collaboration = payload.get("collaboration_mode")
    if isinstance(collaboration, dict):
        settings = collaboration.get("settings")
        if isinstance(settings, dict):
            return _as_non_empty_text(
                settings.get("reasoning_effort")
                or settings.get("reasoningEffort")
                or settings.get("effort")
            )
    return None
