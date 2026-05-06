from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_usage.transcript import parse_transcript


class TranscriptParserTests(unittest.TestCase):
    def test_parses_local_token_count_event_with_weekly_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            event = {
                "timestamp": "2026-05-06T01:02:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 20,
                            "output_tokens": 30,
                            "reasoning_output_tokens": 4,
                            "total_tokens": 130,
                        },
                        "last_token_usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 2,
                            "output_tokens": 3,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 13,
                        },
                        "model_context_window": 258400,
                    },
                    "rate_limits": {
                        "secondary": {
                            "used_percent": 23.5,
                            "window_minutes": 10080,
                            "resets_at": 1777609978,
                        }
                    },
                    "plan_type": "prolite",
                },
            }
            path.write_text(json.dumps(event) + "\n", encoding="utf-8")

            snapshot = parse_transcript(path)

            self.assertIsNone(snapshot.error)
            self.assertEqual(snapshot.total_usage.total_tokens, 130)
            self.assertEqual(snapshot.last_usage.total_tokens, 13)
            self.assertEqual(snapshot.weekly_limit.used_percent, 23.5)
            self.assertEqual(snapshot.weekly_limit.source, "transcript_raw")
            self.assertEqual(snapshot.weekly_limit.window_minutes, 10080)

    def test_handles_rate_limit_only_token_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            event = {
                "timestamp": "2026-05-06T01:02:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": None,
                    "rate_limits": {
                        "secondary": {
                            "used_percent": 24,
                            "window_minutes": 10080,
                            "resets_at": 1777609978,
                        }
                    },
                },
            }
            path.write_text(json.dumps(event) + "\n", encoding="utf-8")

            snapshot = parse_transcript(path)

            self.assertIsNone(snapshot.error)
            self.assertIsNone(snapshot.total_usage)
            self.assertEqual(snapshot.weekly_limit.used_percent, 24.0)

    def test_missing_transcript_reports_error(self) -> None:
        snapshot = parse_transcript("/tmp/does-not-exist-codex-usage.jsonl")
        self.assertEqual(snapshot.error, "missing transcript")


if __name__ == "__main__":
    unittest.main()
