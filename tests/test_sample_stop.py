from __future__ import annotations

import json
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

    def test_scan_recent_transcripts_records_turns_without_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "usage"
            codex_home = root / "codex"
            session_dir = codex_home / "sessions" / "2026" / "05" / "08"
            session_dir.mkdir(parents=True)
            transcript = session_dir / "rollout-2026-05-08T01-00-00-session.jsonl"
            rows = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "019e1000-0000-7000-8000-000000000000",
                        "cwd": "/tmp/project",
                    },
                },
                {
                    "timestamp": "2026-05-08T00:59:01Z",
                    "type": "turn_context",
                    "payload": {
                        "turn_id": "019e0000-0000-7000-8000-000000000000",
                        "model": "gpt-parent",
                        "effort": "high",
                    },
                },
                {
                    "timestamp": "2026-05-08T00:59:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 999}},
                        "rate_limits": {"secondary": {"used_percent": 9}},
                    },
                },
                {
                    "timestamp": "2026-05-08T01:00:01Z",
                    "type": "turn_context",
                    "payload": {
                        "turn_id": "019e1000-0001-7000-8000-000000000000",
                        "model": "gpt-test",
                        "effort": "high",
                    },
                },
                {
                    "timestamp": "2026-05-08T01:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 100}},
                        "rate_limits": {"secondary": {"used_percent": 10}},
                    },
                },
                {
                    "timestamp": "2026-05-08T01:01:01Z",
                    "type": "turn_context",
                    "payload": {
                        "turn_id": "019e1001-0000-7000-8000-000000000000",
                        "model": "gpt-test",
                        "effort": "high",
                    },
                },
                {
                    "timestamp": "2026-05-08T01:01:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 175}},
                        "rate_limits": {"secondary": {"used_percent": 11}},
                    },
                },
            ]
            transcript.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

            collector = UsageCollector(
                home,
                codex_home=codex_home,
                delay_seconds=0,
                use_app_server=False,
            )
            try:
                inserted = collector.scan_recent_transcripts(since_seconds=3600)
                samples = list(
                    collector.store.conn.execute(
                        """
                        SELECT session_id, turn_id, token_total, token_delta
                        FROM samples
                        ORDER BY id
                        """
                    )
                )
                inserted_again = collector.scan_recent_transcripts(since_seconds=3600)
            finally:
                collector.close()

            self.assertEqual(inserted, 2)
            self.assertEqual(inserted_again, 0)
            self.assertEqual(len(samples), 2)
            self.assertEqual(
                samples[0]["session_id"],
                "019e1000-0000-7000-8000-000000000000",
            )
            self.assertEqual(
                samples[0]["turn_id"],
                "019e1000-0001-7000-8000-000000000000",
            )
            self.assertEqual(samples[0]["token_delta"], 0)
            self.assertEqual(
                samples[1]["turn_id"],
                "019e1001-0000-7000-8000-000000000000",
            )
            self.assertEqual(samples[1]["token_total"], 175)
            self.assertEqual(samples[1]["token_delta"], 75)


if __name__ == "__main__":
    unittest.main()
