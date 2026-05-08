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
    ) -> None:
        with store.conn:
            store.conn.execute(
                """
                INSERT INTO samples (
                    event_id, observed_at, token_delta, weekly_used_percent,
                    session_id, turn_id, model, reasoning_effort
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    observed_at,
                    token_delta,
                    weekly_used_percent,
                    "session",
                    event_id,
                    "gpt-test",
                    "high",
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
                samples = [
                    ("turn-1", 1_000, 10.0, "gpt-a", "high"),
                    ("turn-2", 4_000, 11.0, "gpt-a", "high"),
                ]
                for turn_id, total, percent, model, effort in samples:
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
                        {
                            "session_id": "s1",
                            "turn_id": turn_id,
                            "model": model,
                            "reasoning_effort": effort,
                        },
                        snapshot,
                    )

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
                samples = [
                    ("turn-1", 1_000, 10.0, "gpt-a", "high"),
                    ("turn-2", 4_000, 10.0, "gpt-a", "high"),
                    ("turn-3", 7_000, 10.0, "gpt-b", "medium"),
                    ("turn-4", 9_000, 11.0, "gpt-b", "medium"),
                ]
                for turn_id, total, percent, model, effort in samples:
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
                        {
                            "session_id": "s1",
                            "turn_id": turn_id,
                            "model": model,
                            "reasoning_effort": effort,
                        },
                        snapshot,
                    )

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
                samples = [
                    ("turn-1", 1_000, 10.0, 111, "gpt-a", "high"),
                    ("turn-2", 3_000, 11.0, 111, "gpt-a", "high"),
                    ("turn-3", 3_200, 5.0, 222, "gpt-a", "high"),
                    ("turn-4", 6_200, 7.0, 222, "gpt-a", "high"),
                ]
                for turn_id, total, percent, reset, model, effort in samples:
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
                        {
                            "session_id": "s1",
                            "turn_id": turn_id,
                            "model": model,
                            "reasoning_effort": effort,
                        },
                        snapshot,
                    )

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
                samples = [
                    ("turn-1", 1_000, 10.0, "gpt-a", "high"),
                    ("turn-2", 1_000, 10.0, "gpt-a", "high"),
                    ("turn-3", 1_000, 11.0, "gpt-a", "high"),
                    ("turn-4", 2_000, 12.0, "gpt-a", "high"),
                ]
                for turn_id, total, percent, model, effort in samples:
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
                        {
                            "session_id": "s1",
                            "turn_id": turn_id,
                            "model": model,
                            "reasoning_effort": effort,
                        },
                        snapshot,
                    )

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

    def test_negative_percent_movement_does_not_create_positive_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                samples = [
                    ("turn-1", 1_000, 10.0),
                    ("turn-2", 2_000, 9.0),
                    ("turn-3", 3_000, 10.0),
                ]
                for turn_id, total, percent in samples:
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
                        {
                            "session_id": "s1",
                            "turn_id": turn_id,
                            "model": "gpt-a",
                            "reasoning_effort": "high",
                        },
                        snapshot,
                    )

                event = store.conn.execute(
                    """
                    SELECT token_delta_total, turn_count
                    FROM usage_movement_events
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertIsNotNone(event)
                self.assertEqual(event["token_delta_total"], 1_000)
                self.assertEqual(event["turn_count"], 1)
            finally:
                store.close()

    def test_weekly_reset_starts_new_event_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                samples = [
                    ("turn-1", 1_000, 10.0, 111),
                    ("turn-2", 2_000, 11.0, 111),
                    ("turn-3", 2_500, 1.0, 222),
                    ("turn-4", 3_500, 2.0, 222),
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
                        {
                            "session_id": "s1",
                            "turn_id": turn_id,
                            "model": "gpt-a",
                            "reasoning_effort": "high",
                        },
                        snapshot,
                    )

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
                first = TranscriptSnapshot(
                    path="a.jsonl",
                    token_event_timestamp="turn-1",
                    total_usage=TokenUsage(total_tokens=1_000),
                    weekly_limit=WeeklyLimit(used_percent=10.0, resets_at=111),
                )
                second = TranscriptSnapshot(
                    path="a.jsonl",
                    token_event_timestamp="turn-2",
                    total_usage=TokenUsage(total_tokens=3_000),
                    weekly_limit=WeeklyLimit(used_percent=11.0, resets_at=111),
                )
                store.record_sample({"session_id": "s1", "turn_id": "turn-1"}, first)
                store.record_sample({"session_id": "s1", "turn_id": "turn-2"}, second)

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
                self.assertEqual(today["first_used_percent"], 14.0)
                self.assertEqual(today["last_used_percent"], 30.0)
                self.assertEqual(today["used_percent_delta"], 16.0)
                self.assertEqual(today["level"], "medium")
                self.assertEqual(today["token_delta_total"], 160)
                self.assertEqual(today["latest_turn_token_delta"], 80)
                self.assertNotIn("last_turn_token_total", today)
                self.assertEqual(today["sample_count"], 3)
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
