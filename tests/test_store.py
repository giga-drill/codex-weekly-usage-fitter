from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_usage.store import UsageStore
from codex_usage.transcript import TokenUsage, TranscriptSnapshot, WeeklyLimit


class StoreTests(unittest.TestCase):
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
