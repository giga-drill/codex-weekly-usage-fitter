from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from codex_usage.store import UsageStore
from codex_usage.transcript import TokenUsage, TranscriptSnapshot, WeeklyLimit


class StoreTests(unittest.TestCase):
    def insert_sample(
        self,
        store: UsageStore,
        event_id: str,
        observed_at: str,
        token_delta: int,
        weekly_used_percent: float,
        *,
        weekly_resets_at: int = 111,
        model: str | None = "gpt-test",
        reasoning_effort: str | None = "high",
    ) -> None:
        if not hasattr(self, "_insert_state"):
            self._insert_state = {}
        state = self._insert_state.setdefault(
            id(store),
            {
                "total": 0,
                "percent": None,
                "index": 0,
                "transcript_path": str(store.home / "test-transcript.jsonl"),
                "session_meta_written": False,
            },
        )
        state["index"] += 1
        previous_total = int(state["total"])
        token_total_end = previous_total + int(token_delta)
        previous_percent = state["percent"]
        weekly_start = float(previous_percent) if previous_percent is not None else float(weekly_used_percent)
        weekly_delta = (
            max(0.0, float(weekly_used_percent) - float(previous_percent))
            if previous_percent is not None
            else 0.0
        )
        transcript_path = state["transcript_path"]
        transcript_lines: list[dict[str, object]] = []
        if not state["session_meta_written"]:
            transcript_lines.append(
                {
                    "timestamp": observed_at,
                    "type": "session_meta",
                    "payload": {"id": "session"},
                }
            )
            state["session_meta_written"] = True
        transcript_lines.extend(
            [
                {
                    "timestamp": observed_at,
                    "type": "turn_context",
                    "payload": {
                        "turn_id": event_id,
                        "model": model,
                        "effort": reasoning_effort,
                    },
                },
                {
                    "timestamp": observed_at,
                    "type": "event_msg",
                    "payload": {"type": "user_message"},
                },
                {
                    "timestamp": observed_at,
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "total_tokens": token_total_end,
                            }
                        },
                        "rate_limits": {
                            "secondary": {
                                "used_percent": float(weekly_used_percent),
                                "resets_at": int(weekly_resets_at),
                                "window_minutes": 10080,
                            }
                        },
                    },
                },
                {
                    "timestamp": observed_at,
                    "type": "event_msg",
                    "payload": {"type": "task_complete"},
                },
            ]
        )
        with Path(transcript_path).open("a", encoding="utf-8") as handle:
            for row in transcript_lines:
                handle.write(json.dumps(row))
                handle.write("\n")

        with store.conn:
            store.conn.execute(
                """
                INSERT INTO samples (
                    event_id, observed_at, token_delta, weekly_used_percent,
                    weekly_resets_at, weekly_window_minutes,
                    session_id, turn_id, model, reasoning_effort, transcript_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    observed_at,
                    token_delta,
                    weekly_used_percent,
                    weekly_resets_at,
                    10080,
                    "session",
                    event_id,
                    model,
                    reasoning_effort,
                    transcript_path,
                ),
            )
            store.conn.execute(
                """
                INSERT INTO conversation_turns (
                    session_id, conversation_turn_key, user_message_timestamp,
                    user_message_index, start_observed_at, end_observed_at,
                    transcript_path, first_internal_turn_id, last_internal_turn_id,
                    internal_turn_ids_json, model, reasoning_effort, sample_count,
                    token_delta, token_total_start, token_total_end,
                    weekly_used_percent_start, weekly_used_percent_end,
                    weekly_percent_delta, weekly_resets_at, weekly_window_minutes,
                    completed
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    "session",
                    f"test-{event_id}",
                    observed_at,
                    int(state["index"]),
                    observed_at,
                    observed_at,
                    transcript_path,
                    event_id,
                    event_id,
                    json.dumps([event_id]),
                    "gpt-test",
                    "high",
                    1,
                    int(token_delta),
                    previous_total,
                    token_total_end,
                    weekly_start,
                    float(weekly_used_percent),
                    weekly_delta,
                    weekly_resets_at,
                    10080,
                ),
            )
        state["total"] = token_total_end
        state["percent"] = float(weekly_used_percent)

    def write_transcript(
        self,
        path: Path,
        session_id: str,
        turns: list[dict[str, object]],
    ) -> None:
        rows: list[dict[str, object]] = [
            {
                "timestamp": "2026-05-12T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": session_id},
            }
        ]
        minute = 1
        for user_index, turn in enumerate(turns, start=1):
            token_events = list(turn["token_events"])  # type: ignore[index]
            first_event = token_events[0]
            active_turn_id = str(first_event["turn_id"])  # type: ignore[index]
            rows.append(
                {
                    "timestamp": f"2026-05-12T00:{minute:02d}:00Z",
                    "type": "turn_context",
                    "payload": {
                        "turn_id": active_turn_id,
                        "model": turn.get("model", "gpt-a"),
                        "effort": turn.get("effort", "high"),
                    },
                }
            )
            rows.append(
                {
                    "timestamp": f"2026-05-12T00:{minute:02d}:01Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": f"message {user_index}",
                    },
                }
            )
            for event_index, token_event in enumerate(token_events, start=2):
                event_turn_id = str(token_event["turn_id"])  # type: ignore[index]
                if event_turn_id != active_turn_id:
                    active_turn_id = event_turn_id
                    rows.append(
                        {
                            "timestamp": f"2026-05-12T00:{minute:02d}:{event_index:02d}Z",
                            "type": "turn_context",
                            "payload": {
                                "turn_id": active_turn_id,
                                "model": turn.get("model", "gpt-a"),
                                "effort": turn.get("effort", "high"),
                            },
                        }
                    )
                rows.append(
                    {
                        "timestamp": f"2026-05-12T00:{minute:02d}:{event_index + 10:02d}Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "total_tokens": int(token_event["total"])  # type: ignore[index]
                                }
                            },
                            "rate_limits": {
                                "secondary": {
                                    "used_percent": float(token_event["percent"]),  # type: ignore[index]
                                    "resets_at": int(token_event.get("reset", 111)),  # type: ignore[attr-defined]
                                    "window_minutes": 10080,
                                }
                            },
                        },
                    }
                )
            rows.append(
                {
                    "timestamp": f"2026-05-12T00:{minute:02d}:59Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": active_turn_id},
                }
            )
            minute += 1

        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def insert_raw_sample_row(
        self,
        store: UsageStore,
        *,
        event_id: str,
        observed_at: str,
        transcript_path: Path,
        session_id: str,
        token_delta: int,
        weekly_used_percent: float,
        weekly_resets_at: int = 111,
    ) -> None:
        with store.conn:
            store.conn.execute(
                """
                INSERT INTO samples (
                    event_id, observed_at, token_delta, weekly_used_percent,
                    weekly_resets_at, weekly_window_minutes,
                    session_id, turn_id, model, reasoning_effort, transcript_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    observed_at,
                    token_delta,
                    weekly_used_percent,
                    weekly_resets_at,
                    10080,
                    session_id,
                    event_id,
                    "gpt-a",
                    "high",
                    str(transcript_path),
                ),
            )

    def test_first_session_sample_is_baseline_then_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                first = TranscriptSnapshot(
                    path="a.jsonl",
                    token_event_timestamp="t1",
                    total_usage=TokenUsage(total_tokens=100),
                    last_usage=TokenUsage(total_tokens=20),
                    weekly_limit=WeeklyLimit(used_percent=10.0, resets_at=111),
                )
                second = TranscriptSnapshot(
                    path="a.jsonl",
                    token_event_timestamp="t2",
                    total_usage=TokenUsage(total_tokens=160),
                    last_usage=TokenUsage(total_tokens=60),
                    weekly_limit=WeeklyLimit(used_percent=10.0, resets_at=111),
                )

                self.assertTrue(
                    store.record_sample(
                        {"session_id": "s1", "turn_id": "turn-1", "model": "m"},
                        first,
                    )
                )
                self.assertTrue(
                    store.record_sample(
                        {"session_id": "s1", "turn_id": "turn-2", "model": "m"},
                        second,
                    )
                )

                status = store.status()
                self.assertEqual(status["total_observed_tokens"], 60)
                self.assertEqual(status["latest_sample"]["token_delta"], 60)
                self.assertNotIn("last_total_tokens", status["latest_sample"])
            finally:
                store.close()

    def test_older_backfill_does_not_regress_session_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                newer = TranscriptSnapshot(
                    path="a.jsonl",
                    token_event_timestamp="2026-05-08T02:00:00Z",
                    total_usage=TokenUsage(total_tokens=300),
                    weekly_limit=WeeklyLimit(used_percent=10.0, resets_at=111),
                )
                older = TranscriptSnapshot(
                    path="a.jsonl",
                    token_event_timestamp="2026-05-08T01:00:00Z",
                    total_usage=TokenUsage(total_tokens=100),
                    weekly_limit=WeeklyLimit(used_percent=10.0, resets_at=111),
                )
                latest = TranscriptSnapshot(
                    path="a.jsonl",
                    token_event_timestamp="2026-05-08T03:00:00Z",
                    total_usage=TokenUsage(total_tokens=350),
                    weekly_limit=WeeklyLimit(used_percent=10.0, resets_at=111),
                )

                store.record_sample(
                    {"session_id": "s1", "turn_id": "newer", "source": "transcript_scan"},
                    newer,
                )
                store.record_sample(
                    {"session_id": "s1", "turn_id": "older", "source": "transcript_scan"},
                    older,
                )
                store.record_sample(
                    {"session_id": "s1", "turn_id": "latest", "source": "transcript_scan"},
                    latest,
                )

                rows = list(
                    store.conn.execute(
                        "SELECT turn_id, token_delta FROM samples ORDER BY id"
                    )
                )
                session = store.conn.execute(
                    "SELECT last_total_tokens FROM sessions WHERE session_id = 's1'"
                ).fetchone()
            finally:
                store.close()

            self.assertEqual([row["token_delta"] for row in rows], [0, 0, 50])
            self.assertEqual(session["last_total_tokens"], 350)

    def test_conversation_turn_percent_delta_is_global_across_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                s1_path = Path(tmp) / "s1.jsonl"
                s2_path = Path(tmp) / "s2.jsonl"
                self.write_transcript(
                    s1_path,
                    "s1",
                    [
                        {
                            "token_events": [
                                {"turn_id": "s1-t1", "total": 100, "percent": 25.0}
                            ]
                        },
                        {
                            "token_events": [
                                {"turn_id": "s1-t2", "total": 200, "percent": 26.0}
                            ]
                        },
                    ],
                )
                self.write_transcript(
                    s2_path,
                    "s2",
                    [
                        {
                            "token_events": [
                                {"turn_id": "s2-t1", "total": 100, "percent": 25.0}
                            ]
                        },
                        {
                            "token_events": [
                                {"turn_id": "s2-t2", "total": 200, "percent": 26.0}
                            ]
                        },
                    ],
                )
                self.insert_raw_sample_row(
                    store,
                    event_id="s1-raw",
                    observed_at="2026-05-12T00:01:59+00:00",
                    transcript_path=s1_path,
                    session_id="s1",
                    token_delta=0,
                    weekly_used_percent=25.0,
                )
                self.insert_raw_sample_row(
                    store,
                    event_id="s2-raw",
                    observed_at="2026-05-12T00:01:59+00:00",
                    transcript_path=s2_path,
                    session_id="s2",
                    token_delta=0,
                    weekly_used_percent=25.0,
                )

                store.rebuild_epochs_and_fits()
                row = store.conn.execute(
                    """
                    SELECT
                        MIN(weekly_used_percent_end) AS min_percent,
                        MAX(weekly_used_percent_end) AS max_percent,
                        SUM(weekly_percent_delta) AS percent_delta
                    FROM conversation_turns
                    WHERE weekly_resets_at = 111
                    """
                ).fetchone()
            finally:
                store.close()

            self.assertEqual(row["min_percent"], 25.0)
            self.assertEqual(row["max_percent"], 26.0)
            self.assertEqual(row["percent_delta"], 1.0)

    def test_latest_fit_uses_conversation_turns_not_raw_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                transcript_path = Path(tmp) / "session.jsonl"
                self.write_transcript(
                    transcript_path,
                    "s1",
                    [
                        {
                            "token_events": [
                                {"turn_id": "t1", "total": 100, "percent": 10.0}
                            ]
                        },
                        {
                            "token_events": [
                                {"turn_id": "t2", "total": 250, "percent": 11.0}
                            ]
                        },
                    ],
                )
                for index, token_delta in enumerate([3000, 4000, 5000], start=1):
                    self.insert_raw_sample_row(
                        store,
                        event_id=f"raw-{index}",
                        observed_at=f"2026-05-12T00:0{index}:00+00:00",
                        transcript_path=transcript_path,
                        session_id="s1",
                        token_delta=token_delta,
                        weekly_used_percent=10.0 + (index - 1) * 0.5,
                    )

                store.rebuild_epochs_and_fits()
                fit = store.status()["latest_fit"]
            finally:
                store.close()

            self.assertIsNotNone(fit)
            self.assertEqual(fit["sample_count"], 2)
            self.assertEqual(fit["token_delta_total"], 250)
            self.assertEqual(fit["percent_delta"], 1.0)
            self.assertEqual(fit["tokens_per_weekly_percent"], 250.0)

    def test_existing_db_backfill_rebuilds_dependent_fit_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            transcript_path = home / "session.jsonl"
            self.write_transcript(
                transcript_path,
                "s1",
                [
                    {
                        "token_events": [
                            {"turn_id": "t1", "total": 100, "percent": 10.0}
                        ]
                    },
                    {
                        "token_events": [
                            {"turn_id": "t2", "total": 250, "percent": 11.0}
                        ]
                    },
                ],
            )
            store = UsageStore(home)
            try:
                self.insert_raw_sample_row(
                    store,
                    event_id="raw-1",
                    observed_at="2026-05-12T00:01:00+00:00",
                    transcript_path=transcript_path,
                    session_id="s1",
                    token_delta=0,
                    weekly_used_percent=10.0,
                )
                self.insert_raw_sample_row(
                    store,
                    event_id="raw-2",
                    observed_at="2026-05-12T00:02:00+00:00",
                    transcript_path=transcript_path,
                    session_id="s1",
                    token_delta=150,
                    weekly_used_percent=11.0,
                )
            finally:
                store.close()

            reopened = UsageStore(home)
            try:
                conversation_count = reopened.conn.execute(
                    "SELECT COUNT(*) AS value FROM conversation_turns"
                ).fetchone()["value"]
                movement_count = reopened.conn.execute(
                    "SELECT COUNT(*) AS value FROM usage_movement_events"
                ).fetchone()["value"]
                latest_fit = reopened.status()["latest_fit"]
            finally:
                reopened.close()

            self.assertEqual(conversation_count, 2)
            self.assertEqual(movement_count, 1)
            self.assertIsNotNone(latest_fit)

    def test_existing_db_stale_conversation_turns_trigger_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            transcript_path = home / "session.jsonl"
            self.write_transcript(
                transcript_path,
                "s1",
                [
                    {
                        "token_events": [
                            {"turn_id": "t1a", "total": 100, "percent": 10.0},
                            {"turn_id": "t1b", "total": 220, "percent": 12.0},
                        ]
                    },
                    {
                        "token_events": [
                            {"turn_id": "t2a", "total": 300, "percent": 11.0},
                        ]
                    },
                    {
                        "token_events": [
                            {"turn_id": "t3a", "total": 430, "percent": 13.0},
                        ]
                    },
                ],
            )

            store = UsageStore(home)
            try:
                for index, percent in enumerate([10.0, 12.0, 11.0, 13.0], start=1):
                    self.insert_raw_sample_row(
                        store,
                        event_id=f"raw-{index}",
                        observed_at=f"2026-05-12T00:0{index}:00+00:00",
                        transcript_path=transcript_path,
                        session_id="s1",
                        token_delta=100,
                        weekly_used_percent=percent,
                    )
                store.rebuild_epochs_and_fits()
                with store.conn:
                    store.conn.execute(
                        """
                        UPDATE conversation_turns
                        SET internal_token_deltas_json = NULL,
                            weekly_percent_delta = 2.0
                        WHERE id = (
                            SELECT id
                            FROM conversation_turns
                            ORDER BY end_observed_at DESC, id DESC
                            LIMIT 1
                        )
                        """
                    )
                    store.conn.execute(
                        "UPDATE usage_movement_events SET token_delta_total = -1"
                    )
                    store.conn.execute("UPDATE fits SET token_delta_total = -1")
            finally:
                store.close()

            reopened = UsageStore(home)
            try:
                stale_internal_count = reopened.conn.execute(
                    """
                    SELECT COUNT(*) AS value
                    FROM conversation_turns
                    WHERE internal_token_deltas_json IS NULL
                    """
                ).fetchone()["value"]
                conversation_percent_delta = reopened.conn.execute(
                    """
                    SELECT SUM(weekly_percent_delta) AS value
                    FROM conversation_turns
                    WHERE weekly_resets_at = 111
                    """
                ).fetchone()["value"]
                movement_token_min = reopened.conn.execute(
                    "SELECT MIN(token_delta_total) AS value FROM usage_movement_events"
                ).fetchone()["value"]
                fit_token_min = reopened.conn.execute(
                    "SELECT MIN(token_delta_total) AS value FROM fits"
                ).fetchone()["value"]
            finally:
                reopened.close()

            self.assertEqual(stale_internal_count, 0)
            self.assertEqual(conversation_percent_delta, 1.0)
            self.assertGreaterEqual(movement_token_min, 0)
            self.assertGreaterEqual(fit_token_min, 0)

    def test_high_water_percent_movement_with_drop_is_not_overcounted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                self.insert_sample(
                    store, "turn-1", "2026-05-12T01:00:00+00:00", 0, 10.0,
                    model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-2", "2026-05-12T02:00:00+00:00", 1_000, 12.0,
                    model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-3", "2026-05-12T03:00:00+00:00", 1_000, 11.0,
                    model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-4", "2026-05-12T04:00:00+00:00", 1_000, 13.0,
                    model="gpt-a", reasoning_effort="high",
                )
                store.rebuild_epochs_and_fits()

                conversation_delta = store.conn.execute(
                    """
                    SELECT SUM(weekly_percent_delta) AS value
                    FROM conversation_turns
                    WHERE weekly_resets_at = 111
                    """
                ).fetchone()["value"]
                event_delta = store.conn.execute(
                    """
                    SELECT SUM(percent_delta) AS value
                    FROM usage_movement_events
                    WHERE weekly_resets_at = 111
                    """
                ).fetchone()["value"]
                events = list(
                    store.conn.execute(
                        """
                        SELECT percent_delta
                        FROM usage_movement_events
                        WHERE weekly_resets_at = 111
                        ORDER BY id
                        """
                    )
                )
            finally:
                store.close()

            self.assertEqual(conversation_delta, 3.0)
            self.assertEqual(event_delta, 3.0)
            self.assertEqual([row["percent_delta"] for row in events], [2.0, 1.0])

    def test_first_conversation_turn_uses_internal_delta_sum_for_token_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                transcript_path = Path(tmp) / "session.jsonl"
                self.write_transcript(
                    transcript_path,
                    "s1",
                    [
                        {
                            "token_events": [
                                {"turn_id": "t1", "total": 120, "percent": 10.0},
                                {"turn_id": "t2", "total": 200, "percent": 10.0},
                            ]
                        },
                        {
                            "token_events": [
                                {"turn_id": "t3", "total": 260, "percent": 11.0},
                            ]
                        },
                    ],
                )
                self.insert_raw_sample_row(
                    store,
                    event_id="raw-1",
                    observed_at="2026-05-12T00:01:00+00:00",
                    transcript_path=transcript_path,
                    session_id="s1",
                    token_delta=0,
                    weekly_used_percent=10.0,
                )
                store.rebuild_epochs_and_fits()
                first_turn = store.conn.execute(
                    """
                    SELECT token_delta, token_total_start, token_total_end, internal_token_deltas_json
                    FROM conversation_turns
                    WHERE session_id = 's1'
                    ORDER BY user_message_index
                    LIMIT 1
                    """
                ).fetchone()
            finally:
                store.close()

            self.assertEqual(first_turn["token_delta"], 200)
            self.assertEqual(first_turn["token_total_start"], 0)
            self.assertEqual(first_turn["token_total_end"], 200)
            self.assertEqual(
                json.loads(first_turn["internal_token_deltas_json"]),
                {"t1": 120, "t2": 80},
            )

    def test_reset_creates_separate_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                samples = [
                    ("turn-1", 100, 10.0, 111),
                    ("turn-2", 200, 11.0, 111),
                    ("turn-3", 250, 1.0, 222),
                    ("turn-4", 350, 2.0, 222),
                ]
                for turn_id, total, percent, reset in samples:
                    snapshot = TranscriptSnapshot(
                        path="a.jsonl",
                        token_event_timestamp=turn_id,
                        total_usage=TokenUsage(total_tokens=total),
                        weekly_limit=WeeklyLimit(
                            used_percent=percent,
                            resets_at=reset,
                        ),
                    )
                    store.record_sample(
                        {"session_id": "s1", "turn_id": turn_id, "model": "m"},
                        snapshot,
                    )

                status = store.status()
                self.assertEqual(status["latest_epoch"]["weekly_resets_at"], 222)
                self.assertEqual(status["epoch_observed_tokens"], 150)
                self.assertEqual(
                    status["latest_fit"]["tokens_per_weekly_percent"],
                    150.0,
                )
            finally:
                store.close()

    def test_external_usage_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                for turn_id, total, percent in [
                    ("turn-1", 100, 10.0),
                    ("turn-2", 100, 11.0),
                ]:
                    snapshot = TranscriptSnapshot(
                        path="a.jsonl",
                        token_event_timestamp=turn_id,
                        total_usage=TokenUsage(total_tokens=total),
                        weekly_limit=WeeklyLimit(
                            used_percent=percent,
                            resets_at=111,
                        ),
                    )
                    store.record_sample(
                        {"session_id": "s1", "turn_id": turn_id, "model": "m"},
                        snapshot,
                    )

                status = store.status()
                self.assertEqual(status["latest_fit"]["external_usage_observed"], 1)
            finally:
                store.close()

    def test_clean_movement_event_creates_model_effort_fit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                self.insert_sample(
                    store, "turn-1", "2026-05-12T01:00:00+00:00", 0, 10.0,
                    model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-2", "2026-05-12T02:00:00+00:00", 3_000, 11.0,
                    model="gpt-a", reasoning_effort="high",
                )
                store.rebuild_epochs_and_fits()

                status = store.status()
                events = list(
                    store.conn.execute(
                        """
                        SELECT bucket_count, token_delta_total, percent_delta
                        FROM usage_movement_events
                        ORDER BY id
                        """
                    )
                )
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["bucket_count"], 1)
                self.assertEqual(events[0]["token_delta_total"], 3_000)
                self.assertEqual(events[0]["percent_delta"], 1.0)

                groups = {
                    (fit["model"], fit["reasoning_effort"]): fit
                    for fit in status["model_effort_fits"]
                }
                self.assertEqual(
                    groups[("gpt-a", "high")]["tokens_per_weekly_percent"],
                    3_000.0,
                )
                self.assertEqual(
                    groups[("gpt-a", "high")]["turns_per_weekly_percent"],
                    1.0,
                )
                self.assertEqual(
                    status["latest_clean_model_effort_fit"]["model"],
                    "gpt-a",
                )
                self.assertEqual(
                    status["latest_clean_model_effort_fit"]["reasoning_effort"],
                    "high",
                )
            finally:
                store.close()

    def test_mixed_movement_event_does_not_create_per_bucket_fit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                self.insert_sample(
                    store, "turn-1", "2026-05-12T01:00:00+00:00", 0, 10.0,
                    model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-2", "2026-05-12T02:00:00+00:00", 3_000, 10.0,
                    model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-3", "2026-05-12T03:00:00+00:00", 3_000, 10.0,
                    model="gpt-b", reasoning_effort="medium",
                )
                self.insert_sample(
                    store, "turn-4", "2026-05-12T04:00:00+00:00", 2_000, 11.0,
                    model="gpt-b", reasoning_effort="medium",
                )
                store.rebuild_epochs_and_fits()

                events = list(
                    store.conn.execute(
                        """
                        SELECT bucket_count, token_delta_total, turn_count
                        FROM usage_movement_events
                        ORDER BY id
                        """
                    )
                )
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["bucket_count"], 2)
                self.assertEqual(events[0]["token_delta_total"], 8_000)
                self.assertEqual(events[0]["turn_count"], 3)

                status = store.status()
                self.assertEqual(status["model_effort_fits"], [])
                self.assertIsNone(status["latest_clean_model_effort_fit"])
                self.assertEqual(len(status["latest_mixed_movement_events"]), 1)
            finally:
                store.close()

    def test_model_effort_global_fit_aggregates_epochs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                self.insert_sample(
                    store, "turn-1", "2026-05-12T01:00:00+00:00", 0, 10.0,
                    weekly_resets_at=111, model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-2", "2026-05-12T02:00:00+00:00", 2_000, 11.0,
                    weekly_resets_at=111, model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-3", "2026-05-13T01:00:00+00:00", 200, 5.0,
                    weekly_resets_at=222, model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-4", "2026-05-13T02:00:00+00:00", 3_000, 7.0,
                    weekly_resets_at=222, model="gpt-a", reasoning_effort="high",
                )
                store.rebuild_epochs_and_fits()

                status = store.status()
                global_groups = {
                    (fit["model"], fit["reasoning_effort"]): fit
                    for fit in status["model_effort_global_fits"]
                }
                fit = global_groups[("gpt-a", "high")]
                self.assertEqual(fit["epoch_count"], 2)
                self.assertEqual(fit["sample_count"], 2)
                self.assertEqual(fit["token_delta_total"], 5_000)
                self.assertEqual(fit["percent_delta"], 3.0)
                self.assertAlmostEqual(fit["tokens_per_weekly_percent"], 5000 / 3)
                self.assertAlmostEqual(fit["turns_per_weekly_percent"], 2 / 3)
                self.assertEqual(status["latest_clean_model_effort_fit"], fit)
                self.assertNotEqual(
                    status["latest_model_effort_weekly_fit"]["tokens_per_weekly_percent"],
                    fit["tokens_per_weekly_percent"],
                )
            finally:
                store.close()

    def test_clean_fit_turn_count_uses_positive_token_delta_intervals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                self.insert_sample(
                    store, "turn-1", "2026-05-12T01:00:00+00:00", 0, 10.0,
                    model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-2", "2026-05-12T02:00:00+00:00", 0, 10.0,
                    model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-3", "2026-05-12T03:00:00+00:00", 0, 11.0,
                    model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-4", "2026-05-12T04:00:00+00:00", 1_000, 12.0,
                    model="gpt-a", reasoning_effort="high",
                )
                store.rebuild_epochs_and_fits()

                events = list(
                    store.conn.execute(
                        """
                        SELECT percent_delta, token_delta_total, turn_count
                        FROM usage_movement_events
                        ORDER BY id
                        """
                    )
                )
                self.assertEqual(len(events), 2)
                self.assertEqual(events[0]["percent_delta"], 1.0)
                self.assertEqual(events[0]["token_delta_total"], 0)
                self.assertEqual(events[0]["turn_count"], 0)
                self.assertEqual(events[1]["percent_delta"], 1.0)
                self.assertEqual(events[1]["token_delta_total"], 1_000)
                self.assertEqual(events[1]["turn_count"], 1)

                fit = store.status()["latest_clean_model_effort_fit"]
                self.assertIsNotNone(fit)
                self.assertEqual(fit["sample_count"], 2)
                self.assertEqual(fit["token_delta_total"], 1_000)
                self.assertAlmostEqual(fit["tokens_per_weekly_percent"], 500.0)
                self.assertAlmostEqual(fit["turns_per_weekly_percent"], 0.5)
            finally:
                store.close()

    def test_percent_recovery_without_new_high_water_creates_no_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                self.insert_sample(
                    store, "turn-1", "2026-05-12T01:00:00+00:00", 0, 10.0,
                    model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-2", "2026-05-12T02:00:00+00:00", 1_000, 9.0,
                    model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-3", "2026-05-12T03:00:00+00:00", 1_000, 10.0,
                    model="gpt-a", reasoning_effort="high",
                )
                store.rebuild_epochs_and_fits()

                event_count = store.conn.execute(
                    """
                    SELECT COUNT(*) AS value
                    FROM usage_movement_events
                    """
                ).fetchone()["value"]
                self.assertEqual(event_count, 0)
            finally:
                store.close()

    def test_weekly_reset_starts_new_event_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                self.insert_sample(
                    store, "turn-1", "2026-05-12T01:00:00+00:00", 0, 10.0,
                    weekly_resets_at=111, model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-2", "2026-05-12T02:00:00+00:00", 1_000, 11.0,
                    weekly_resets_at=111, model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-3", "2026-05-13T01:00:00+00:00", 500, 1.0,
                    weekly_resets_at=222, model="gpt-a", reasoning_effort="high",
                )
                self.insert_sample(
                    store, "turn-4", "2026-05-13T02:00:00+00:00", 1_000, 2.0,
                    weekly_resets_at=222, model="gpt-a", reasoning_effort="high",
                )
                store.rebuild_epochs_and_fits()

                rows = list(
                    store.conn.execute(
                        """
                        SELECT weekly_resets_at, token_delta_total, turn_count
                        FROM usage_movement_events
                        ORDER BY id
                        """
                    )
                )
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["weekly_resets_at"], 111)
                self.assertEqual(rows[0]["token_delta_total"], 1_000)
                self.assertEqual(rows[1]["weekly_resets_at"], 222)
                self.assertEqual(rows[1]["token_delta_total"], 1_000)
                self.assertEqual(rows[1]["turn_count"], 1)
            finally:
                store.close()

    def test_unknown_model_effort_keeps_unknown_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                self.insert_sample(
                    store, "turn-1", "2026-05-12T01:00:00+00:00", 0, 10.0,
                    model=None, reasoning_effort=None,
                )
                self.insert_sample(
                    store, "turn-2", "2026-05-12T02:00:00+00:00", 2_000, 11.0,
                    model=None, reasoning_effort=None,
                )
                store.rebuild_epochs_and_fits()

                status = store.status()
                fit = status["latest_clean_model_effort_fit"]
                self.assertIsNotNone(fit)
                self.assertEqual(fit["model"], "unknown")
                self.assertEqual(fit["reasoning_effort"], "unknown")
            finally:
                store.close()

    def test_today_usage_uses_local_day_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                for turn_id, total, percent in [
                    ("turn-1", 100, 14.0),
                    ("turn-2", 180, 20.0),
                    ("turn-3", 260, 30.0),
                ]:
                    snapshot = TranscriptSnapshot(
                        path="a.jsonl",
                        token_event_timestamp=turn_id,
                        total_usage=TokenUsage(total_tokens=total),
                        last_usage=TokenUsage(total_tokens=total - 90),
                        weekly_limit=WeeklyLimit(
                            used_percent=percent,
                            resets_at=111,
                        ),
                    )
                    store.record_sample(
                        {"session_id": "s1", "turn_id": turn_id, "model": "m"},
                        snapshot,
                    )

                today = store.status()["today_usage"]
                self.assertIsNone(today["first_used_percent"])
                self.assertIsNone(today["last_used_percent"])
                self.assertEqual(today["used_percent_delta"], 0.0)
                self.assertEqual(today["level"], "low")
                self.assertEqual(today["token_delta_total"], 0)
                self.assertIsNone(today["latest_turn_token_delta"])
                self.assertNotIn("last_turn_token_total", today)
                self.assertEqual(today["sample_count"], 0)
                self.assertEqual(today["conversation_turn_count"], 0)
                self.assertEqual(
                    today["error"], "conversation_turns_unavailable_for_today"
                )
                self.assertEqual(today["raw_sample_count"], 3)
            finally:
                store.close()

    def test_billing_stats_accumulates_positive_usage_after_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                self.insert_sample(store, "before", "2026-05-11T15:59:00+00:00", 50, 98.0)
                self.insert_sample(store, "reset", "2026-05-12T02:00:00+00:00", 100, 2.0)
                self.insert_sample(store, "growth", "2026-05-13T02:00:00+00:00", 200, 3.5)

                stats = store.billing_stats(
                    billing_day=12,
                    timezone_name="Asia/Shanghai",
                    now=datetime(2026, 5, 20, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
                    debug=True,
                )

                self.assertEqual(stats["period"]["start"], "2026-05-12")
                self.assertEqual(stats["period"]["end"], "2026-06-12")
                self.assertEqual(stats["period"]["token_delta_total"], 300)
                self.assertEqual(stats["period"]["turn_count"], 2)
                self.assertEqual(stats["period"]["usage_percent_delta"], 1.5)
                self.assertEqual(len(stats["debug_samples"]), 2)
                day_totals = [
                    day["usage_percent_delta"]
                    for window in stats["weekly_windows"]
                    for day in window["days"]
                ]
                self.assertAlmostEqual(sum(day_totals), 1.5)
            finally:
                store.close()

    def test_billing_stats_previous_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                self.insert_sample(store, "apr13", "2026-04-13T02:00:00+00:00", 100, 10.0)
                self.insert_sample(store, "apr14", "2026-04-14T02:00:00+00:00", 300, 11.0)
                self.insert_sample(store, "may13", "2026-05-13T02:00:00+00:00", 900, 20.0)

                stats = store.billing_stats(
                    billing_day=12,
                    period="previous",
                    timezone_name="Asia/Shanghai",
                    now=datetime(2026, 5, 20, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
                )

                self.assertEqual(stats["label"], "Last billing period")
                self.assertEqual(stats["period"]["start"], "2026-04-12")
                self.assertEqual(stats["period"]["end"], "2026-05-12")
                self.assertEqual(stats["period"]["token_delta_total"], 400)
                self.assertEqual(stats["period"]["turn_count"], 2)
                self.assertEqual(stats["period"]["usage_percent_delta"], 1.0)
            finally:
                store.close()

    def test_billing_stats_turn_count_ignores_zero_delta_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                self.insert_sample(store, "reset", "2026-05-12T02:00:00+00:00", 0, 2.0)
                self.insert_sample(store, "external", "2026-05-13T02:00:00+00:00", 0, 3.0)
                self.insert_sample(store, "local", "2026-05-14T02:00:00+00:00", 120, 4.0)

                stats = store.billing_stats(
                    billing_day=12,
                    timezone_name="Asia/Shanghai",
                    now=datetime(2026, 5, 20, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
                )

                self.assertEqual(stats["period"]["usage_percent_delta"], 2.0)
                self.assertEqual(stats["period"]["token_delta_total"], 120)
                self.assertEqual(stats["period"]["turn_count"], 1)
                days = [
                    day
                    for window in stats["weekly_windows"]
                    for day in window["days"]
                    if day["token_delta_total"] > 0 or day["usage_percent_delta"] > 0
                ]
                self.assertEqual(len(days), 2)
                self.assertEqual(days[0]["usage_percent_delta"], 1.0)
                self.assertEqual(days[0]["token_delta_total"], 0)
                self.assertEqual(days[0]["turn_count"], 0)
                self.assertEqual(days[1]["usage_percent_delta"], 1.0)
                self.assertEqual(days[1]["token_delta_total"], 120)
                self.assertEqual(days[1]["turn_count"], 1)
            finally:
                store.close()

    def test_billing_stats_clamps_billing_day_to_month_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                stats = store.billing_stats(
                    billing_day=31,
                    timezone_name="Asia/Shanghai",
                    now=datetime(2026, 3, 15, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
                )

                self.assertEqual(stats["period"]["start"], "2026-02-28")
                self.assertEqual(stats["period"]["end"], "2026-03-31")
                self.assertEqual(stats["weekly_windows"][-1]["start"], "2026-03-28")
                self.assertEqual(stats["weekly_windows"][-1]["end"], "2026-03-31")
            finally:
                store.close()

    def test_records_reasoning_effort_from_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                snapshot = TranscriptSnapshot(
                    path="a.jsonl",
                    token_event_timestamp="turn-1",
                    total_usage=TokenUsage(total_tokens=100),
                    last_usage=TokenUsage(total_tokens=100),
                    weekly_limit=WeeklyLimit(used_percent=14.0, resets_at=111),
                    model="gpt-from-transcript",
                    reasoning_effort="x high",
                )
                store.record_sample(
                    {"session_id": "s1", "turn_id": "turn-1"},
                    snapshot,
                )

                latest = store.status()["latest_sample"]
                self.assertEqual(latest["model"], "gpt-from-transcript")
                self.assertEqual(latest["reasoning_effort"], "xhigh")
            finally:
                store.close()

    def test_backfills_reasoning_effort_from_existing_sample_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            transcript_path = home / "session.jsonl"
            turn = {
                "timestamp": "2026-05-06T01:02:01Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-1",
                    "model": "gpt-backfill",
                    "effort": "medium",
                },
            }
            event = {
                "timestamp": "2026-05-06T01:02:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {"total_tokens": 10}},
                },
            }
            transcript_path.write_text(
                json.dumps(turn) + "\n" + json.dumps(event) + "\n",
                encoding="utf-8",
            )

            store = UsageStore(home)
            try:
                with store.conn:
                    store.conn.execute(
                        """
                        INSERT INTO samples (
                            event_id, observed_at, session_id, turn_id,
                            transcript_path, token_delta
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "event-1",
                            "2026-05-06T01:02:04+00:00",
                            "session-1",
                            "turn-1",
                            str(transcript_path),
                            10,
                        ),
                    )
            finally:
                store.close()

            reopened = UsageStore(home)
            try:
                latest = reopened.status()["latest_sample"]
                self.assertEqual(latest["model"], "gpt-backfill")
                self.assertEqual(latest["reasoning_effort"], "medium")
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
