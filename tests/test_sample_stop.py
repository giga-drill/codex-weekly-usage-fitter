from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_usage.collector import enqueue_stop_event


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


if __name__ == "__main__":
    unittest.main()
