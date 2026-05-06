from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .paths import db_path, ensure_usage_dirs
from .transcript import TokenUsage, TranscriptSnapshot, WeeklyLimit


EXTERNAL_TOKEN_THRESHOLD = 1000


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UsageStore:
    def __init__(self, home: Path) -> None:
        self.home = home
        ensure_usage_dirs(home)
        self.conn = sqlite3.connect(db_path(home))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                last_total_tokens INTEGER,
                last_seen_at TEXT NOT NULL,
                transcript_path TEXT,
                model TEXT
            );

            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                observed_at TEXT NOT NULL,
                hook_received_at TEXT,
                session_id TEXT,
                turn_id TEXT,
                model TEXT,
                transcript_path TEXT,
                token_delta INTEGER NOT NULL DEFAULT 0,
                token_total INTEGER,
                input_tokens INTEGER,
                cached_input_tokens INTEGER,
                output_tokens INTEGER,
                reasoning_output_tokens INTEGER,
                last_total_tokens INTEGER,
                last_input_tokens INTEGER,
                last_cached_input_tokens INTEGER,
                last_output_tokens INTEGER,
                last_reasoning_output_tokens INTEGER,
                weekly_used_percent REAL,
                weekly_resets_at INTEGER,
                weekly_window_minutes INTEGER,
                percent_source TEXT,
                external_usage_observed INTEGER NOT NULL DEFAULT 0,
                parse_error TEXT,
                raw_json TEXT
            );

            CREATE TABLE IF NOT EXISTS epochs (
                weekly_resets_at INTEGER PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                first_used_percent REAL,
                last_used_percent REAL,
                token_delta_total INTEGER NOT NULL DEFAULT 0,
                external_usage_observed INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS fits (
                weekly_resets_at INTEGER PRIMARY KEY,
                sample_count INTEGER NOT NULL,
                token_delta_total INTEGER NOT NULL,
                percent_delta REAL NOT NULL,
                tokens_per_weekly_percent REAL,
                confidence TEXT NOT NULL,
                external_usage_observed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def record_sample(
        self,
        event: dict[str, Any],
        snapshot: TranscriptSnapshot,
        fallback_weekly: WeeklyLimit | None = None,
    ) -> bool:
        observed_at = utc_now_iso()
        session_id = _string_or_none(event.get("session_id"))
        turn_id = _string_or_none(event.get("turn_id"))
        model = _string_or_none(event.get("model"))
        transcript_path = _string_or_none(event.get("transcript_path")) or snapshot.path
        hook_received_at = _string_or_none(event.get("received_at"))

        total_usage = snapshot.total_usage
        last_usage = snapshot.last_usage
        weekly = snapshot.weekly_limit or fallback_weekly
        token_total = total_usage.total_tokens if total_usage else None
        previous_total = self._session_total(session_id) if session_id else None
        token_delta = self._compute_delta(previous_total, token_total)

        event_id = self._event_id(
            event=event,
            snapshot=snapshot,
            token_total=token_total,
            weekly=weekly,
        )

        row = {
            "event_id": event_id,
            "observed_at": observed_at,
            "hook_received_at": hook_received_at,
            "session_id": session_id,
            "turn_id": turn_id,
            "model": model,
            "transcript_path": transcript_path,
            "token_delta": token_delta,
            "token_total": token_total,
            "input_tokens": _usage_value(total_usage, "input_tokens"),
            "cached_input_tokens": _usage_value(total_usage, "cached_input_tokens"),
            "output_tokens": _usage_value(total_usage, "output_tokens"),
            "reasoning_output_tokens": _usage_value(
                total_usage, "reasoning_output_tokens"
            ),
            "last_total_tokens": _usage_value(last_usage, "total_tokens"),
            "last_input_tokens": _usage_value(last_usage, "input_tokens"),
            "last_cached_input_tokens": _usage_value(last_usage, "cached_input_tokens"),
            "last_output_tokens": _usage_value(last_usage, "output_tokens"),
            "last_reasoning_output_tokens": _usage_value(
                last_usage, "reasoning_output_tokens"
            ),
            "weekly_used_percent": weekly.used_percent if weekly else None,
            "weekly_resets_at": weekly.resets_at if weekly else None,
            "weekly_window_minutes": weekly.window_minutes if weekly else None,
            "percent_source": weekly.source if weekly else None,
            "parse_error": snapshot.error,
            "raw_json": json.dumps(_safe_raw(event, snapshot), separators=(",", ":")),
        }

        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO samples (
                    event_id, observed_at, hook_received_at, session_id, turn_id, model,
                    transcript_path, token_delta, token_total, input_tokens,
                    cached_input_tokens, output_tokens, reasoning_output_tokens,
                    last_total_tokens, last_input_tokens, last_cached_input_tokens,
                    last_output_tokens, last_reasoning_output_tokens,
                    weekly_used_percent, weekly_resets_at, weekly_window_minutes,
                    percent_source, parse_error, raw_json
                )
                VALUES (
                    :event_id, :observed_at, :hook_received_at, :session_id, :turn_id,
                    :model, :transcript_path, :token_delta, :token_total, :input_tokens,
                    :cached_input_tokens, :output_tokens, :reasoning_output_tokens,
                    :last_total_tokens, :last_input_tokens, :last_cached_input_tokens,
                    :last_output_tokens, :last_reasoning_output_tokens,
                    :weekly_used_percent, :weekly_resets_at, :weekly_window_minutes,
                    :percent_source, :parse_error, :raw_json
                )
                """,
                row,
            )
            inserted = cursor.rowcount > 0
            if inserted and session_id:
                self._upsert_session(
                    session_id=session_id,
                    last_total_tokens=token_total,
                    last_seen_at=observed_at,
                    transcript_path=transcript_path,
                    model=model,
                )

        if inserted:
            self.rebuild_epochs_and_fits()
        return inserted

    def _session_total(self, session_id: str | None) -> int | None:
        if not session_id:
            return None
        row = self.conn.execute(
            "SELECT last_total_tokens FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return row["last_total_tokens"]

    def _upsert_session(
        self,
        session_id: str,
        last_total_tokens: int | None,
        last_seen_at: str,
        transcript_path: str | None,
        model: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO sessions (
                session_id, last_total_tokens, last_seen_at, transcript_path, model
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                last_total_tokens = COALESCE(excluded.last_total_tokens, sessions.last_total_tokens),
                last_seen_at = excluded.last_seen_at,
                transcript_path = COALESCE(excluded.transcript_path, sessions.transcript_path),
                model = COALESCE(excluded.model, sessions.model)
            """,
            (session_id, last_total_tokens, last_seen_at, transcript_path, model),
        )

    def _compute_delta(self, previous: int | None, current: int | None) -> int:
        if previous is None or current is None:
            return 0
        return max(0, current - previous)

    def _event_id(
        self,
        event: dict[str, Any],
        snapshot: TranscriptSnapshot,
        token_total: int | None,
        weekly: WeeklyLimit | None,
    ) -> str:
        payload = {
            "session_id": event.get("session_id"),
            "turn_id": event.get("turn_id"),
            "transcript_path": event.get("transcript_path") or snapshot.path,
            "token_event_timestamp": snapshot.token_event_timestamp,
            "token_total": token_total,
            "weekly_used_percent": weekly.used_percent if weekly else None,
            "weekly_resets_at": weekly.resets_at if weekly else None,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return digest

    def rebuild_epochs_and_fits(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM epochs")
            self.conn.execute("DELETE FROM fits")
            resets = [
                row["weekly_resets_at"]
                for row in self.conn.execute(
                    """
                    SELECT DISTINCT weekly_resets_at
                    FROM samples
                    WHERE weekly_resets_at IS NOT NULL
                    ORDER BY weekly_resets_at
                    """
                )
            ]
            for reset_at in resets:
                rows = list(
                    self.conn.execute(
                        """
                        SELECT id, observed_at, token_delta, weekly_used_percent
                        FROM samples
                        WHERE weekly_resets_at = ?
                          AND weekly_used_percent IS NOT NULL
                        ORDER BY observed_at, id
                        """,
                        (reset_at,),
                    )
                )
                if not rows:
                    continue
                token_total = sum(int(row["token_delta"] or 0) for row in rows)
                first = rows[0]
                last = rows[-1]
                external = _external_usage_observed(rows)
                first_percent = float(first["weekly_used_percent"])
                last_percent = float(last["weekly_used_percent"])
                percent_delta = max(0.0, last_percent - first_percent)
                tokens_per_percent = (
                    token_total / percent_delta if percent_delta > 0 else None
                )
                confidence = _confidence(
                    sample_count=len(rows),
                    percent_delta=percent_delta,
                    external=external,
                )

                self.conn.execute(
                    """
                    INSERT INTO epochs (
                        weekly_resets_at, first_seen_at, last_seen_at,
                        first_used_percent, last_used_percent, token_delta_total,
                        external_usage_observed
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reset_at,
                        first["observed_at"],
                        last["observed_at"],
                        first_percent,
                        last_percent,
                        token_total,
                        int(external),
                    ),
                )
                self.conn.execute(
                    """
                    INSERT INTO fits (
                        weekly_resets_at, sample_count, token_delta_total,
                        percent_delta, tokens_per_weekly_percent, confidence,
                        external_usage_observed, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reset_at,
                        len(rows),
                        token_total,
                        percent_delta,
                        tokens_per_percent,
                        confidence,
                        int(external),
                        utc_now_iso(),
                    ),
                )
                self.conn.execute(
                    """
                    UPDATE samples
                    SET external_usage_observed = ?
                    WHERE weekly_resets_at = ?
                    """,
                    (int(external), reset_at),
                )

    def status(self) -> dict[str, Any]:
        latest = self.conn.execute(
            "SELECT * FROM samples ORDER BY observed_at DESC, id DESC LIMIT 1"
        ).fetchone()
        latest_epoch = None
        latest_fit = None
        if latest is not None and latest["weekly_resets_at"] is not None:
            latest_epoch = self.conn.execute(
                "SELECT * FROM epochs WHERE weekly_resets_at = ?",
                (latest["weekly_resets_at"],),
            ).fetchone()
            latest_fit = self.conn.execute(
                "SELECT * FROM fits WHERE weekly_resets_at = ?",
                (latest["weekly_resets_at"],),
            ).fetchone()

        total_observed = self.conn.execute(
            "SELECT COALESCE(SUM(token_delta), 0) AS value FROM samples"
        ).fetchone()["value"]

        epoch_observed = None
        if latest is not None and latest["weekly_resets_at"] is not None:
            epoch_observed = self.conn.execute(
                """
                SELECT COALESCE(SUM(token_delta), 0) AS value
                FROM samples
                WHERE weekly_resets_at = ?
                """,
                (latest["weekly_resets_at"],),
            ).fetchone()["value"]

        return {
            "home": str(self.home),
            "sample_count": self.conn.execute(
                "SELECT COUNT(*) AS value FROM samples"
            ).fetchone()["value"],
            "session_count": self.conn.execute(
                "SELECT COUNT(*) AS value FROM sessions"
            ).fetchone()["value"],
            "total_observed_tokens": total_observed,
            "epoch_observed_tokens": epoch_observed,
            "latest_sample": _row_to_dict(latest),
            "latest_epoch": _row_to_dict(latest_epoch),
            "latest_fit": _row_to_dict(latest_fit),
            "today_usage": self.today_usage(),
        }

    def today_usage(self, now: datetime | None = None) -> dict[str, Any]:
        local_now = now.astimezone() if now is not None else datetime.now().astimezone()
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        local_end = local_start + timedelta(days=1)
        start_utc = local_start.astimezone(timezone.utc).isoformat(timespec="seconds")
        end_utc = local_end.astimezone(timezone.utc).isoformat(timespec="seconds")

        rows = list(
            self.conn.execute(
                """
                SELECT observed_at, weekly_used_percent, token_delta, last_total_tokens
                FROM samples
                WHERE observed_at >= ?
                  AND observed_at < ?
                  AND weekly_used_percent IS NOT NULL
                ORDER BY observed_at, id
                """,
                (start_utc, end_utc),
            )
        )
        first_percent = float(rows[0]["weekly_used_percent"]) if rows else None
        last_percent = float(rows[-1]["weekly_used_percent"]) if rows else None
        used_delta = (
            max(0.0, last_percent - first_percent)
            if first_percent is not None and last_percent is not None
            else 0.0
        )
        return {
            "date": local_start.date().isoformat(),
            "first_used_percent": first_percent,
            "last_used_percent": last_percent,
            "used_percent_delta": used_delta,
            "level": _today_usage_level(used_delta),
            "token_delta_total": sum(int(row["token_delta"] or 0) for row in rows),
            "last_turn_token_total": int(rows[-1]["last_total_tokens"])
            if rows and rows[-1]["last_total_tokens"] is not None
            else None,
            "sample_count": len(rows),
        }

    def export_jsonl(self) -> str:
        rows = self.conn.execute("SELECT * FROM samples ORDER BY observed_at, id")
        return "".join(json.dumps(dict(row), separators=(",", ":")) + "\n" for row in rows)

    def export_csv(self) -> str:
        rows = list(self.conn.execute("SELECT * FROM samples ORDER BY observed_at, id"))
        if not rows:
            return ""
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
        return output.getvalue()


def _usage_value(usage: TokenUsage | None, name: str) -> int | None:
    if usage is None:
        return None
    return getattr(usage, name)


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _safe_raw(event: dict[str, Any], snapshot: TranscriptSnapshot) -> dict[str, Any]:
    return {
        "event": {
            "session_id": event.get("session_id"),
            "turn_id": event.get("turn_id"),
            "transcript_path": event.get("transcript_path"),
            "model": event.get("model"),
            "received_at": event.get("received_at"),
        },
        "snapshot": {
            "path": snapshot.path,
            "token_event_timestamp": snapshot.token_event_timestamp,
            "total_usage": asdict(snapshot.total_usage)
            if snapshot.total_usage is not None
            else None,
            "last_usage": asdict(snapshot.last_usage)
            if snapshot.last_usage is not None
            else None,
            "weekly_limit": asdict(snapshot.weekly_limit)
            if snapshot.weekly_limit is not None
            else None,
            "plan_type": snapshot.plan_type,
            "error": snapshot.error,
        },
    }


def _external_usage_observed(rows: Iterable[sqlite3.Row]) -> bool:
    previous_percent: float | None = None
    pending_tokens = 0
    for row in rows:
        percent = float(row["weekly_used_percent"])
        pending_tokens += int(row["token_delta"] or 0)
        if previous_percent is None:
            previous_percent = percent
            pending_tokens = 0
            continue
        if percent > previous_percent:
            if pending_tokens <= EXTERNAL_TOKEN_THRESHOLD:
                return True
            pending_tokens = 0
        previous_percent = percent
    return False


def _confidence(sample_count: int, percent_delta: float, external: bool) -> str:
    if sample_count < 2 or percent_delta <= 0:
        return "none"
    if percent_delta < 1 or sample_count < 5:
        return "low"
    if not external and percent_delta >= 3 and sample_count >= 10:
        return "high"
    return "medium"


def _today_usage_level(used_percent: float) -> str:
    if used_percent < 15:
        return "low"
    if used_percent <= 28:
        return "medium"
    return "high"


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)
