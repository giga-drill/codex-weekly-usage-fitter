from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .paths import db_path, ensure_usage_dirs
from .transcript import TokenUsage, TranscriptSnapshot, WeeklyLimit, parse_transcript


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
                model TEXT,
                reasoning_effort TEXT
            );

            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                observed_at TEXT NOT NULL,
                hook_received_at TEXT,
                session_id TEXT,
                turn_id TEXT,
                model TEXT,
                reasoning_effort TEXT,
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

            CREATE TABLE IF NOT EXISTS model_effort_fits (
                weekly_resets_at INTEGER NOT NULL,
                model TEXT NOT NULL,
                reasoning_effort TEXT NOT NULL,
                sample_count INTEGER NOT NULL,
                token_delta_total INTEGER NOT NULL,
                percent_delta REAL NOT NULL,
                tokens_per_weekly_percent REAL,
                turns_per_weekly_percent REAL,
                confidence TEXT NOT NULL,
                external_usage_observed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (weekly_resets_at, model, reasoning_effort)
            );

            CREATE TABLE IF NOT EXISTS model_effort_global_fits (
                model TEXT NOT NULL,
                reasoning_effort TEXT NOT NULL,
                epoch_count INTEGER NOT NULL,
                sample_count INTEGER NOT NULL,
                token_delta_total INTEGER NOT NULL,
                percent_delta REAL NOT NULL,
                tokens_per_weekly_percent REAL,
                turns_per_weekly_percent REAL,
                confidence TEXT NOT NULL,
                external_usage_observed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (model, reasoning_effort)
            );

            CREATE TABLE IF NOT EXISTS usage_movement_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                weekly_resets_at INTEGER NOT NULL,
                from_sample_id INTEGER NOT NULL,
                to_sample_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                percent_delta REAL NOT NULL,
                bucket_count INTEGER NOT NULL,
                token_delta_total INTEGER NOT NULL,
                turn_count INTEGER NOT NULL,
                external_usage_observed INTEGER NOT NULL DEFAULT 0,
                buckets_json TEXT NOT NULL
            );
            """
        )
        self._ensure_column("sessions", "reasoning_effort", "TEXT")
        self._ensure_column("samples", "reasoning_effort", "TEXT")
        self._backfill_model_effort()
        self._ensure_column("model_effort_fits", "turns_per_weekly_percent", "REAL")
        self._ensure_model_effort_fits()
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _backfill_model_effort(self) -> None:
        rows = list(
            self.conn.execute(
                """
                SELECT id, transcript_path, turn_id, model, reasoning_effort
                FROM samples
                WHERE transcript_path IS NOT NULL
                  AND turn_id IS NOT NULL
                  AND (model IS NULL OR reasoning_effort IS NULL)
                """
            )
        )
        for row in rows:
            snapshot = parse_transcript(row["transcript_path"], turn_id=row["turn_id"])
            model = row["model"] or snapshot.model
            reasoning_effort = row["reasoning_effort"] or _normalize_effort(
                snapshot.reasoning_effort
            )
            if model == row["model"] and reasoning_effort == row["reasoning_effort"]:
                continue
            self.conn.execute(
                """
                UPDATE samples
                SET model = ?, reasoning_effort = ?
                WHERE id = ?
                """,
                (model, reasoning_effort, row["id"]),
            )

    def _ensure_model_effort_fits(self) -> None:
        sample_count = self.conn.execute(
            "SELECT COUNT(*) AS value FROM samples"
        ).fetchone()["value"]
        if sample_count == 0:
            return
        fit_count = self.conn.execute(
            "SELECT COUNT(*) AS value FROM model_effort_fits"
        ).fetchone()["value"]
        global_fit_count = self.conn.execute(
            "SELECT COUNT(*) AS value FROM model_effort_global_fits"
        ).fetchone()["value"]
        missing_turn_fit_count = self.conn.execute(
            """
            SELECT COUNT(*) AS value
            FROM model_effort_fits
            WHERE percent_delta > 0
              AND turns_per_weekly_percent IS NULL
            """
        ).fetchone()["value"]
        movement_event_count = self.conn.execute(
            "SELECT COUNT(*) AS value FROM usage_movement_events"
        ).fetchone()["value"]
        movement_epoch_count = self.conn.execute(
            """
            SELECT COUNT(*) AS value
            FROM (
                SELECT weekly_resets_at
                FROM samples
                WHERE weekly_resets_at IS NOT NULL
                  AND weekly_used_percent IS NOT NULL
                GROUP BY weekly_resets_at
                HAVING MAX(weekly_used_percent) > MIN(weekly_used_percent)
            )
            """
        ).fetchone()["value"]
        needs_event_backfill = movement_epoch_count > 0 and movement_event_count == 0
        if (
            fit_count == 0
            or global_fit_count == 0
            or missing_turn_fit_count
            or needs_event_backfill
        ):
            self.rebuild_epochs_and_fits()

    def record_sample(
        self,
        event: dict[str, Any],
        snapshot: TranscriptSnapshot,
        fallback_weekly: WeeklyLimit | None = None,
    ) -> bool:
        observed_at = _sample_observed_at(event, snapshot)
        session_id = _string_or_none(event.get("session_id"))
        turn_id = _string_or_none(event.get("turn_id"))
        model = _string_or_none(event.get("model")) or snapshot.model
        reasoning_effort = _normalize_effort(
            event.get("reasoning_effort")
            or event.get("reasoningEffort")
            or event.get("effort")
            or snapshot.reasoning_effort
        )
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
            "reasoning_effort": reasoning_effort,
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
                    reasoning_effort, transcript_path, token_delta, token_total, input_tokens,
                    cached_input_tokens, output_tokens, reasoning_output_tokens,
                    last_total_tokens, last_input_tokens, last_cached_input_tokens,
                    last_output_tokens, last_reasoning_output_tokens,
                    weekly_used_percent, weekly_resets_at, weekly_window_minutes,
                    percent_source, parse_error, raw_json
                )
                VALUES (
                    :event_id, :observed_at, :hook_received_at, :session_id, :turn_id,
                    :model, :reasoning_effort, :transcript_path, :token_delta, :token_total, :input_tokens,
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
                    reasoning_effort=reasoning_effort,
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
        reasoning_effort: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO sessions (
                session_id, last_total_tokens, last_seen_at, transcript_path, model,
                reasoning_effort
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                last_total_tokens = CASE
                    WHEN excluded.last_seen_at >= sessions.last_seen_at
                    THEN COALESCE(excluded.last_total_tokens, sessions.last_total_tokens)
                    ELSE sessions.last_total_tokens
                END,
                last_seen_at = CASE
                    WHEN excluded.last_seen_at >= sessions.last_seen_at
                    THEN excluded.last_seen_at
                    ELSE sessions.last_seen_at
                END,
                transcript_path = CASE
                    WHEN excluded.last_seen_at >= sessions.last_seen_at
                    THEN COALESCE(excluded.transcript_path, sessions.transcript_path)
                    ELSE sessions.transcript_path
                END,
                model = CASE
                    WHEN excluded.last_seen_at >= sessions.last_seen_at
                    THEN COALESCE(excluded.model, sessions.model)
                    ELSE sessions.model
                END,
                reasoning_effort = CASE
                    WHEN excluded.last_seen_at >= sessions.last_seen_at
                    THEN COALESCE(excluded.reasoning_effort, sessions.reasoning_effort)
                    ELSE sessions.reasoning_effort
                END
            """,
            (
                session_id,
                last_total_tokens,
                last_seen_at,
                transcript_path,
                model,
                reasoning_effort,
            ),
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
            self.conn.execute("DELETE FROM model_effort_fits")
            self.conn.execute("DELETE FROM model_effort_global_fits")
            self.conn.execute("DELETE FROM usage_movement_events")
            model_effort_groups = []
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
                        SELECT
                            id, observed_at, token_delta, weekly_used_percent,
                            model, reasoning_effort
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

                movement_events = _usage_movement_events(rows, int(reset_at))
                for event in movement_events:
                    self.conn.execute(
                        """
                        INSERT INTO usage_movement_events (
                            weekly_resets_at,
                            from_sample_id,
                            to_sample_id,
                            observed_at,
                            percent_delta,
                            bucket_count,
                            token_delta_total,
                            turn_count,
                            external_usage_observed,
                            buckets_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event["weekly_resets_at"],
                            event["from_sample_id"],
                            event["to_sample_id"],
                            event["observed_at"],
                            event["percent_delta"],
                            event["bucket_count"],
                            event["token_delta_total"],
                            event["turn_count"],
                            int(event["external_usage_observed"]),
                            event["buckets_json"],
                        ),
                    )

                for group in _model_effort_fit_rows_from_events(movement_events):
                    model_effort_groups.append(group)
                    self.conn.execute(
                        """
                        INSERT INTO model_effort_fits (
                            weekly_resets_at, model, reasoning_effort,
                            sample_count, token_delta_total, percent_delta,
                            tokens_per_weekly_percent, turns_per_weekly_percent,
                            confidence,
                            external_usage_observed, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            reset_at,
                            group["model"],
                            group["reasoning_effort"],
                            group["sample_count"],
                            group["token_delta_total"],
                            group["percent_delta"],
                            group["tokens_per_weekly_percent"],
                            group["turns_per_weekly_percent"],
                            group["confidence"],
                            int(group["external_usage_observed"]),
                            utc_now_iso(),
                        ),
                    )
            for group in _global_model_effort_fit_rows(model_effort_groups):
                self.conn.execute(
                    """
                    INSERT INTO model_effort_global_fits (
                        model, reasoning_effort, epoch_count, sample_count,
                        token_delta_total, percent_delta,
                        tokens_per_weekly_percent, turns_per_weekly_percent,
                        confidence,
                        external_usage_observed, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group["model"],
                        group["reasoning_effort"],
                        group["epoch_count"],
                        group["sample_count"],
                        group["token_delta_total"],
                        group["percent_delta"],
                        group["tokens_per_weekly_percent"],
                        group["turns_per_weekly_percent"],
                        group["confidence"],
                        int(group["external_usage_observed"]),
                        utc_now_iso(),
                    ),
                )

    def status(self) -> dict[str, Any]:
        latest = self.conn.execute(
            """
            SELECT *
            FROM samples
            WHERE parse_error IS NULL
              AND weekly_used_percent IS NOT NULL
            ORDER BY observed_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if latest is None:
            latest = self.conn.execute(
                "SELECT * FROM samples ORDER BY observed_at DESC, id DESC LIMIT 1"
            ).fetchone()
        latest_epoch = None
        latest_fit = None
        latest_model_effort_key = None
        latest_model_effort_fit = None
        latest_model_effort_weekly_fit = None
        model_effort_fits: list[sqlite3.Row] = []
        model_effort_global_fits: list[sqlite3.Row] = []
        mixed_movement_events: list[dict[str, Any]] = []
        if latest is not None and latest["weekly_resets_at"] is not None:
            latest_epoch = self.conn.execute(
                "SELECT * FROM epochs WHERE weekly_resets_at = ?",
                (latest["weekly_resets_at"],),
            ).fetchone()
            latest_fit = self.conn.execute(
                "SELECT * FROM fits WHERE weekly_resets_at = ?",
                (latest["weekly_resets_at"],),
            ).fetchone()
            latest_key = _model_effort_key(latest)
            latest_model_effort_key = {
                "model": latest_key[0],
                "reasoning_effort": latest_key[1],
            }
            latest_model_effort_weekly_fit = self.conn.execute(
                """
                SELECT *
                FROM model_effort_fits
                WHERE weekly_resets_at = ?
                  AND model = ?
                  AND reasoning_effort = ?
                """,
                (latest["weekly_resets_at"], latest_key[0], latest_key[1]),
            ).fetchone()
            latest_model_effort_fit = self.conn.execute(
                """
                SELECT *
                FROM model_effort_global_fits
                WHERE model = ?
                  AND reasoning_effort = ?
                """,
                latest_key,
            ).fetchone() or latest_model_effort_weekly_fit
            model_effort_fits = list(
                self.conn.execute(
                    """
                    SELECT *
                    FROM model_effort_fits
                    WHERE weekly_resets_at = ?
                    ORDER BY percent_delta DESC, token_delta_total DESC
                    LIMIT 5
                    """,
                    (latest["weekly_resets_at"],),
                )
            )
            model_effort_global_fits = list(
                self.conn.execute(
                    """
                    SELECT *
                    FROM model_effort_global_fits
                    ORDER BY percent_delta DESC, token_delta_total DESC
                    LIMIT 5
                    """
                )
            )
            mixed_movement_events = [
                _format_movement_event_row(row)
                for row in self.conn.execute(
                    """
                    SELECT *
                    FROM usage_movement_events
                    WHERE weekly_resets_at = ?
                      AND bucket_count > 1
                    ORDER BY observed_at DESC, id DESC
                    LIMIT 5
                    """,
                    (latest["weekly_resets_at"],),
                )
            ]

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
            "latest_sample": _public_sample_row(latest),
            "latest_epoch": _row_to_dict(latest_epoch),
            "latest_fit": _row_to_dict(latest_fit),
            "latest_model_effort_key": latest_model_effort_key,
            "latest_clean_model_effort_fit": _row_to_dict(latest_model_effort_fit),
            "latest_model_effort_fit": _row_to_dict(latest_model_effort_fit),
            "latest_model_effort_weekly_fit": _row_to_dict(
                latest_model_effort_weekly_fit
            ),
            "model_effort_fits": [_row_to_dict(row) for row in model_effort_fits],
            "model_effort_global_fits": [
                _row_to_dict(row) for row in model_effort_global_fits
            ],
            "latest_mixed_movement_events": mixed_movement_events,
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
                SELECT observed_at, weekly_used_percent, token_delta
                FROM samples
                WHERE observed_at >= ?
                  AND observed_at < ?
                  AND weekly_used_percent IS NOT NULL
                  AND parse_error IS NULL
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
            "latest_turn_token_delta": int(rows[-1]["token_delta"] or 0) if rows else None,
            "sample_count": len(rows),
        }

    def billing_stats(
        self,
        billing_day: int,
        period: str = "current",
        timezone_name: str | None = None,
        now: datetime | None = None,
        debug: bool = False,
    ) -> dict[str, Any]:
        if not 1 <= billing_day <= 31:
            raise ValueError("billing_day must be between 1 and 31")
        if period not in {"current", "previous"}:
            raise ValueError("period must be current or previous")

        local_tz = ZoneInfo(timezone_name) if timezone_name else datetime.now().astimezone().tzinfo
        if local_tz is None:
            local_tz = timezone.utc
        local_now = now.astimezone(local_tz) if now is not None else datetime.now(local_tz)
        current_start = _billing_period_start(local_now, billing_day, local_tz)
        current_end = _add_billing_month(current_start, billing_day)
        if period == "previous":
            period_end = current_start
            period_start = _add_billing_month(period_end, billing_day, months=-1)
            label = "Last billing period"
        else:
            period_start = current_start
            period_end = current_end
            label = "This billing period"

        rows = self._billing_sample_rows(period_end)
        weekly_windows = _billing_weekly_windows(period_start, period_end)
        week_metrics = [_empty_metric(window[0], window[1]) for window in weekly_windows]
        daily_metrics = [
            [_empty_metric(day_start, min(day_start + timedelta(days=1), week_end))
             for day_start in _day_starts(week_start, week_end)]
            for week_start, week_end in weekly_windows
        ]
        period_metric = _empty_metric(period_start, period_end)
        debug_samples = []
        previous_percent: float | None = None

        for row in rows:
            observed_utc = _parse_iso_datetime(row["observed_at"])
            if observed_utc is None:
                continue
            observed_local = observed_utc.astimezone(local_tz)
            current_percent = (
                float(row["weekly_used_percent"])
                if row["weekly_used_percent"] is not None
                else None
            )
            movement = 0.0
            if current_percent is not None and previous_percent is not None:
                movement = max(0.0, current_percent - previous_percent)

            in_period = period_start <= observed_local < period_end
            if in_period:
                token_delta = int(row["token_delta"] or 0)
                sample = {
                    "observed_at": observed_local.isoformat(timespec="seconds"),
                    "usage_percent": current_percent,
                    "usage_percent_delta": movement,
                    "token_delta": token_delta,
                    "session_id": row["session_id"],
                    "turn_id": row["turn_id"],
                    "model": row["model"],
                    "reasoning_effort": row["reasoning_effort"],
                }
                _add_sample_to_metric(period_metric, token_delta, movement)
                for week_index, metric in enumerate(week_metrics):
                    if metric["start_at"] <= observed_local < metric["end_at"]:
                        _add_sample_to_metric(metric, token_delta, movement)
                        for day_metric in daily_metrics[week_index]:
                            if day_metric["start_at"] <= observed_local < day_metric["end_at"]:
                                _add_sample_to_metric(day_metric, token_delta, movement)
                                break
                        break
                if debug:
                    debug_samples.append(sample)

            if current_percent is not None:
                previous_percent = current_percent

        formatted_weeks = []
        for metric, days in zip(week_metrics, daily_metrics):
            week = _format_metric(metric)
            week["days"] = [_format_metric(day) for day in days]
            formatted_weeks.append(week)

        mixed_events = self._mixed_movement_events_for_period(
            period_start=period_start,
            period_end=period_end,
            timezone_value=local_tz,
        )

        output = {
            "label": label,
            "period": _format_metric(period_metric),
            "weekly_windows": formatted_weeks,
            "billing_day": billing_day,
            "timezone": str(local_tz),
            "mixed_movement_events": mixed_events,
            "mixed_movement_combinations": _aggregate_mixed_combinations(mixed_events),
        }
        if debug:
            output["debug_samples"] = debug_samples
        return output

    def _billing_sample_rows(self, period_end: datetime) -> list[sqlite3.Row]:
        end_utc = period_end.astimezone(timezone.utc).isoformat(timespec="seconds")
        return list(
            self.conn.execute(
                """
                SELECT
                    observed_at, weekly_used_percent, token_delta, session_id,
                    turn_id, model, reasoning_effort
                FROM samples
                WHERE observed_at < ?
                  AND parse_error IS NULL
                ORDER BY observed_at, id
                """,
                (end_utc,),
            )
        )

    def _mixed_movement_events_for_period(
        self,
        period_start: datetime,
        period_end: datetime,
        timezone_value: timezone | ZoneInfo,
    ) -> list[dict[str, Any]]:
        start_utc = period_start.astimezone(timezone.utc).isoformat(timespec="seconds")
        end_utc = period_end.astimezone(timezone.utc).isoformat(timespec="seconds")
        rows = self.conn.execute(
            """
            SELECT *
            FROM usage_movement_events
            WHERE observed_at >= ?
              AND observed_at < ?
              AND bucket_count > 1
            ORDER BY observed_at, id
            """,
            (start_utc, end_utc),
        )
        output: list[dict[str, Any]] = []
        for row in rows:
            event = _format_movement_event_row(row)
            observed = _parse_iso_datetime(event["observed_at"])
            if observed is not None:
                event["observed_at_local"] = observed.astimezone(
                    timezone_value
                ).isoformat(timespec="seconds")
            output.append(event)
        return output

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


def _normalize_effort(value: Any) -> str | None:
    text = _string_or_none(value)
    if text is None:
        return None
    normalized = text.strip().lower().replace("-", " ").replace("_", " ")
    normalized = " ".join(normalized.split())
    aliases = {
        "none": "none",
        "minimal": "minimal",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "x high": "xhigh",
        "xhigh": "xhigh",
        "extra high": "xhigh",
    }
    return aliases.get(normalized, normalized)


def _billing_period_start(
    local_now: datetime, billing_day: int, local_tz: timezone | ZoneInfo
) -> datetime:
    month_start = _billing_month_datetime(
        local_now.year, local_now.month, billing_day, local_tz
    )
    if local_now >= month_start:
        return month_start
    return _add_billing_month(month_start, billing_day, months=-1)


def _add_billing_month(
    value: datetime, billing_day: int, months: int = 1
) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return _billing_month_datetime(year, month, billing_day, value.tzinfo or timezone.utc)


def _billing_month_datetime(
    year: int, month: int, billing_day: int, local_tz: timezone | ZoneInfo
) -> datetime:
    day = min(billing_day, _days_in_month(year, month))
    return datetime.combine(date(year, month, day), time.min, tzinfo=local_tz)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - timedelta(days=1)).day


def _billing_weekly_windows(
    period_start: datetime, period_end: datetime
) -> list[tuple[datetime, datetime]]:
    windows = []
    start = period_start
    while start < period_end:
        end = min(start + timedelta(days=7), period_end)
        windows.append((start, end))
        start = end
    return windows


def _day_starts(start: datetime, end: datetime) -> list[datetime]:
    days = []
    cursor = start
    while cursor < end:
        days.append(cursor)
        cursor = min(cursor + timedelta(days=1), end)
    return days


def _empty_metric(start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "start_at": start,
        "end_at": end,
        "usage_percent_delta": 0.0,
        "token_delta_total": 0,
        "turn_count": 0,
        "avg_tokens_per_turn": None,
    }


def _add_sample_to_metric(
    metric: dict[str, Any], token_delta: int, usage_percent_delta: float
) -> None:
    metric["usage_percent_delta"] += usage_percent_delta
    metric["token_delta_total"] += token_delta
    if token_delta > 0:
        metric["turn_count"] += 1


def _format_metric(metric: dict[str, Any]) -> dict[str, Any]:
    turns = int(metric["turn_count"])
    tokens = int(metric["token_delta_total"])
    return {
        "start": metric["start_at"].date().isoformat(),
        "end": metric["end_at"].date().isoformat(),
        "start_at": metric["start_at"].isoformat(timespec="seconds"),
        "end_at": metric["end_at"].isoformat(timespec="seconds"),
        "usage_percent_delta": float(metric["usage_percent_delta"]),
        "token_delta_total": tokens,
        "turn_count": turns,
        "avg_tokens_per_turn": tokens / turns if turns > 0 else None,
    }


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = _string_or_none(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _safe_raw(event: dict[str, Any], snapshot: TranscriptSnapshot) -> dict[str, Any]:
    return {
        "event": {
            "session_id": event.get("session_id"),
            "turn_id": event.get("turn_id"),
            "transcript_path": event.get("transcript_path"),
            "model": event.get("model"),
            "reasoning_effort": event.get("reasoning_effort")
            or event.get("reasoningEffort")
            or event.get("effort"),
            "received_at": event.get("received_at"),
            "source": event.get("source"),
        },
        "snapshot": {
            "path": snapshot.path,
            "token_event_timestamp": snapshot.token_event_timestamp,
            "model": snapshot.model,
            "reasoning_effort": snapshot.reasoning_effort,
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


def _sample_observed_at(
    event: dict[str, Any],
    snapshot: TranscriptSnapshot,
) -> str:
    raw_observed = _string_or_none(event.get("observed_at"))
    if raw_observed is not None:
        return _iso_to_utc_seconds(raw_observed)
    if event.get("source") == "transcript_scan" and snapshot.token_event_timestamp:
        return _iso_to_utc_seconds(snapshot.token_event_timestamp)
    return utc_now_iso()


def _iso_to_utc_seconds(value: str) -> str:
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return utc_now_iso()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _usage_movement_events(
    rows: list[sqlite3.Row], weekly_resets_at: int
) -> list[dict[str, Any]]:
    if len(rows) < 2:
        return []

    output: list[dict[str, Any]] = []
    pending_rows: list[sqlite3.Row] = []
    previous_percent = float(rows[0]["weekly_used_percent"])

    for row in rows[1:]:
        pending_rows.append(row)
        current_percent = float(row["weekly_used_percent"])
        movement = current_percent - previous_percent
        if movement > 0:
            buckets: dict[tuple[str, str], dict[str, Any]] = {}
            token_delta_total = 0
            turn_count = 0
            for pending in pending_rows:
                key = _model_effort_key(pending)
                token_delta = int(pending["token_delta"] or 0)
                bucket = buckets.get(key)
                if bucket is None:
                    bucket = {
                        "model": key[0],
                        "reasoning_effort": key[1],
                        "token_delta_total": 0,
                        "turn_count": 0,
                    }
                    buckets[key] = bucket
                bucket["token_delta_total"] += token_delta
                if token_delta > 0:
                    bucket["turn_count"] += 1
                token_delta_total += token_delta
                if token_delta > 0:
                    turn_count += 1

            ordered_buckets = [
                buckets[key]
                for key in sorted(
                    buckets.keys(),
                    key=lambda item: (item[0], item[1]),
                )
            ]
            output.append(
                {
                    "weekly_resets_at": weekly_resets_at,
                    "from_sample_id": int(pending_rows[0]["id"]),
                    "to_sample_id": int(pending_rows[-1]["id"]),
                    "observed_at": pending_rows[-1]["observed_at"],
                    "percent_delta": float(movement),
                    "bucket_count": len(ordered_buckets),
                    "token_delta_total": token_delta_total,
                    "turn_count": turn_count,
                    "external_usage_observed": token_delta_total <= EXTERNAL_TOKEN_THRESHOLD,
                    "buckets_json": json.dumps(
                        ordered_buckets, separators=(",", ":"), sort_keys=True
                    ),
                }
            )
            pending_rows.clear()
        elif movement < 0:
            # Percent dropped within the same reset window. Treat this as a boundary
            # and avoid attributing earlier pending turns across the drop.
            pending_rows.clear()
        previous_percent = current_percent
    return output


def _model_effort_fit_rows_from_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stats: dict[tuple[str, str], dict[str, Any]] = {}

    for event in events:
        percent_delta = float(event["percent_delta"])
        if percent_delta <= 0:
            continue
        if int(event["bucket_count"]) != 1:
            continue
        buckets = _deserialize_buckets(event["buckets_json"])
        if len(buckets) != 1:
            continue
        bucket = buckets[0]
        key = (
            _string_or_none(bucket.get("model")) or "unknown",
            _normalize_effort(bucket.get("reasoning_effort")) or "unknown",
        )
        stat = stats.get(key)
        if stat is None:
            stat = {
                "model": key[0],
                "reasoning_effort": key[1],
                "sample_count": 0,
                "token_delta_total": 0,
                "turn_count": 0,
                "percent_delta": 0.0,
                "external_usage_observed": False,
            }
            stats[key] = stat
        stat["sample_count"] += 1
        stat["token_delta_total"] += int(event["token_delta_total"] or 0)
        stat["turn_count"] += int(event["turn_count"] or 0)
        stat["percent_delta"] += percent_delta
        stat["external_usage_observed"] = bool(
            stat["external_usage_observed"] or event.get("external_usage_observed")
        )

    output = []
    for stat in stats.values():
        percent_delta = float(stat["percent_delta"])
        token_total = int(stat["token_delta_total"])
        turn_count = int(stat["turn_count"])
        stat["tokens_per_weekly_percent"] = (
            token_total / percent_delta if percent_delta > 0 else None
        )
        stat["turns_per_weekly_percent"] = (
            turn_count / percent_delta if percent_delta > 0 else None
        )
        stat["confidence"] = _confidence(
            sample_count=int(stat["sample_count"]),
            percent_delta=percent_delta,
            external=bool(stat["external_usage_observed"]),
        )
        output.append(stat)
    return output


def _global_model_effort_fit_rows(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stats: dict[tuple[str, str], dict[str, Any]] = {}

    for group in groups:
        key = (group["model"], group["reasoning_effort"])
        if key not in stats:
            stats[key] = {
                "model": key[0],
                "reasoning_effort": key[1],
                "epoch_count": 0,
                "sample_count": 0,
                "token_delta_total": 0,
                "turn_count": 0,
                "percent_delta": 0.0,
                "external_usage_observed": False,
            }
        stat = stats[key]
        stat["epoch_count"] += 1
        stat["external_usage_observed"] = bool(
            stat["external_usage_observed"]
            or group.get("external_usage_observed")
        )

        percent_delta = float(group.get("percent_delta") or 0.0)
        if percent_delta <= 0:
            continue
        stat["sample_count"] += int(group.get("sample_count") or 0)
        stat["token_delta_total"] += int(group.get("token_delta_total") or 0)
        stat["turn_count"] = int(stat.get("turn_count") or 0) + int(
            group.get("turn_count") or 0
        )
        stat["percent_delta"] += percent_delta

    output = []
    for stat in stats.values():
        percent_delta = float(stat["percent_delta"])
        token_total = int(stat["token_delta_total"])
        stat["tokens_per_weekly_percent"] = (
            token_total / percent_delta if percent_delta > 0 else None
        )
        stat["turns_per_weekly_percent"] = (
            int(stat.get("turn_count") or 0) / percent_delta
            if percent_delta > 0
            else None
        )
        stat["confidence"] = _confidence(
            sample_count=int(stat["sample_count"]),
            percent_delta=percent_delta,
            external=bool(stat["external_usage_observed"]),
        )
        output.append(stat)
    return output


def _format_movement_event_row(row: sqlite3.Row) -> dict[str, Any]:
    buckets = _deserialize_buckets(row["buckets_json"])
    return {
        "id": int(row["id"]),
        "weekly_resets_at": int(row["weekly_resets_at"]),
        "from_sample_id": int(row["from_sample_id"]),
        "to_sample_id": int(row["to_sample_id"]),
        "observed_at": row["observed_at"],
        "percent_delta": float(row["percent_delta"]),
        "bucket_count": int(row["bucket_count"]),
        "token_delta_total": int(row["token_delta_total"]),
        "turn_count": int(row["turn_count"]),
        "external_usage_observed": bool(row["external_usage_observed"]),
        "buckets": buckets,
        "combination": _bucket_combination_label(buckets),
    }


def _deserialize_buckets(value: Any) -> list[dict[str, Any]]:
    text = _string_or_none(value)
    if text is None:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    output: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        output.append(
            {
                "model": _string_or_none(item.get("model")) or "unknown",
                "reasoning_effort": _normalize_effort(item.get("reasoning_effort"))
                or "unknown",
                "token_delta_total": int(item.get("token_delta_total") or 0),
                "turn_count": int(item.get("turn_count") or 0),
            }
        )
    return output


def _bucket_combination_label(buckets: list[dict[str, Any]]) -> str:
    if not buckets:
        return "unknown"
    labels = [
        f"{bucket.get('model') or 'unknown'}/{bucket.get('reasoning_effort') or 'unknown'}"
        for bucket in buckets
    ]
    labels.sort()
    return " + ".join(labels)


def _aggregate_mixed_combinations(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for event in events:
        key = event.get("combination") or "unknown"
        stat = stats.get(key)
        if stat is None:
            stat = {
                "combination": key,
                "event_count": 0,
                "percent_delta": 0.0,
                "token_delta_total": 0,
                "turn_count": 0,
            }
            stats[key] = stat
        stat["event_count"] += 1
        stat["percent_delta"] += float(event.get("percent_delta") or 0.0)
        stat["token_delta_total"] += int(event.get("token_delta_total") or 0)
        stat["turn_count"] += int(event.get("turn_count") or 0)
    return sorted(
        stats.values(),
        key=lambda item: (item["percent_delta"], item["token_delta_total"]),
        reverse=True,
    )


def _model_effort_key(row: sqlite3.Row) -> tuple[str, str]:
    model = _string_or_none(row["model"]) or "unknown"
    effort = _normalize_effort(row["reasoning_effort"]) or "unknown"
    return model, effort


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


def _public_sample_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    data = _row_to_dict(row)
    if data is None:
        return None
    return {
        key: value
        for key, value in data.items()
        if not key.startswith("last_")
    }
