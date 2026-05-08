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
            finally:
                store.close()

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

    def test_model_effort_fit_allocates_percent_by_pending_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(Path(tmp))
            try:
                samples = [
                    ("turn-1", 1_000, 10.0, "gpt-a", "high"),
                    ("turn-2", 3_000, 10.0, "gpt-a", "high"),
                    ("turn-3", 9_000, 10.0, "gpt-b", "medium"),
                    ("turn-4", 11_000, 12.0, "gpt-b", "medium"),
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
                groups = {
                    (fit["model"], fit["reasoning_effort"]): fit
                    for fit in status["model_effort_fits"]
                }
                self.assertAlmostEqual(groups[("gpt-a", "high")]["percent_delta"], 0.4)
                self.assertAlmostEqual(groups[("gpt-b", "medium")]["percent_delta"], 1.6)
                self.assertEqual(groups[("gpt-a", "high")]["token_delta_total"], 2_000)
                self.assertEqual(groups[("gpt-b", "medium")]["token_delta_total"], 8_000)
                self.assertEqual(
                    groups[("gpt-b", "medium")]["tokens_per_weekly_percent"],
                    5_000.0,
                )
                self.assertEqual(
                    groups[("gpt-b", "medium")]["turns_per_weekly_percent"],
                    1.25,
                )
                self.assertEqual(
                    status["latest_model_effort_fit"]["model"],
                    "gpt-b",
                )
                self.assertEqual(
                    status["latest_model_effort_fit"]["reasoning_effort"],
                    "medium",
                )
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
                self.assertEqual(status["latest_model_effort_fit"], fit)
                self.assertNotEqual(
                    status["latest_model_effort_weekly_fit"]["tokens_per_weekly_percent"],
                    fit["tokens_per_weekly_percent"],
                )
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
