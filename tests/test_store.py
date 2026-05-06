from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
