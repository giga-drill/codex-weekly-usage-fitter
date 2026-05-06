from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_usage.collector import UsageCollector, enqueue_stop_event


class SampleStopTests(unittest.TestCase):
    def test_spools_when_daemon_is_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            sent = enqueue_stop_event(
                home,
                {
                    "session_id": "s1",
                    "turn_id": "t1",
                    "transcript_path": "/tmp/session.jsonl",
                    "model": "gpt-test",
                },
                timeout_seconds=0.01,
            )

            self.assertFalse(sent)
            files = list((home / "spool").glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            self.assertIn('"session_id":"s1"', files[0].read_text(encoding="utf-8"))

    def test_ignores_stop_event_without_transcript_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            collector = UsageCollector(home, delay_seconds=0, use_app_server=False)
            try:
                inserted = collector.process_event(
                    {
                        "session_id": "internal-session",
                        "turn_id": "internal-turn",
                        "transcript_path": None,
                        "model": "gpt-test",
                    }
                )
                sample_count = collector.store.conn.execute(
                    "SELECT COUNT(*) AS value FROM samples"
                ).fetchone()["value"]
            finally:
                collector.close()

            self.assertFalse(inserted)
            self.assertEqual(sample_count, 0)


if __name__ == "__main__":
    unittest.main()
