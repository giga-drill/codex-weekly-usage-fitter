from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_usage.transcript import parse_conversation_turns, parse_transcript


class TranscriptParserTests(unittest.TestCase):
    def test_parses_local_token_count_event_with_weekly_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            turn = {
                "timestamp": "2026-05-06T01:02:01Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-1",
                    "model": "gpt-test",
                    "effort": "high",
                },
            }
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
            path.write_text(
                json.dumps(turn) + "\n" + json.dumps(event) + "\n",
                encoding="utf-8",
            )

            snapshot = parse_transcript(path, turn_id="turn-1")

            self.assertIsNone(snapshot.error)
            self.assertEqual(snapshot.model, "gpt-test")
            self.assertEqual(snapshot.reasoning_effort, "high")
            self.assertEqual(snapshot.total_usage.total_tokens, 130)
            self.assertEqual(snapshot.last_usage.total_tokens, 13)
            self.assertEqual(snapshot.weekly_limit.used_percent, 23.5)
            self.assertEqual(snapshot.weekly_limit.source, "transcript_raw")
            self.assertEqual(snapshot.weekly_limit.window_minutes, 10080)

    def test_parses_reasoning_effort_from_collaboration_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            turn = {
                "timestamp": "2026-05-06T01:02:01Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-2",
                    "model": "gpt-test",
                    "collaboration_mode": {
                        "settings": {"reasoning_effort": "xhigh"}
                    },
                },
            }
            event = {
                "timestamp": "2026-05-06T01:02:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 13},
                    },
                },
            }
            path.write_text(
                json.dumps(turn) + "\n" + json.dumps(event) + "\n",
                encoding="utf-8",
            )

            snapshot = parse_transcript(path, turn_id="turn-2")

            self.assertEqual(snapshot.model, "gpt-test")
            self.assertEqual(snapshot.reasoning_effort, "xhigh")

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

    def test_selects_token_count_for_requested_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            first_turn = {
                "timestamp": "2026-05-06T01:02:01Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-1",
                    "model": "gpt-a",
                    "effort": "high",
                },
            }
            first_token = {
                "timestamp": "2026-05-06T01:02:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {"total_tokens": 100},
                        "last_token_usage": {"total_tokens": 40},
                    },
                    "rate_limits": {
                        "secondary": {
                            "used_percent": 18,
                            "window_minutes": 10080,
                            "resets_at": 111,
                        }
                    },
                },
            }
            second_turn = {
                "timestamp": "2026-05-06T01:03:01Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-2",
                    "model": "gpt-b",
                    "effort": "medium",
                },
            }
            second_token = {
                "timestamp": "2026-05-06T01:03:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {"total_tokens": 175},
                        "last_token_usage": {"total_tokens": 75},
                    },
                    "rate_limits": {
                        "secondary": {
                            "used_percent": 19,
                            "window_minutes": 10080,
                            "resets_at": 111,
                        }
                    },
                },
            }
            path.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in [first_turn, first_token, second_turn, second_token]
                )
                + "\n",
                encoding="utf-8",
            )

            snapshot = parse_transcript(path, turn_id="turn-1")

            self.assertEqual(snapshot.model, "gpt-a")
            self.assertEqual(snapshot.reasoning_effort, "high")
            self.assertEqual(snapshot.total_usage.total_tokens, 100)
            self.assertEqual(snapshot.last_usage.total_tokens, 40)
            self.assertEqual(snapshot.weekly_limit.used_percent, 18.0)

    def test_missing_transcript_reports_error(self) -> None:
        snapshot = parse_transcript("/tmp/does-not-exist-codex-usage.jsonl")
        self.assertEqual(snapshot.error, "missing transcript")

    def test_parse_conversation_turns_aggregates_multiple_token_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            rows = [
                {
                    "timestamp": "2026-05-09T02:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "s1"},
                },
                {
                    "timestamp": "2026-05-09T02:00:01Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "t1", "model": "gpt-a", "effort": "high"},
                },
                {
                    "timestamp": "2026-05-09T02:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "hi"},
                },
                {
                    "timestamp": "2026-05-09T02:00:03Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 100}},
                        "rate_limits": {"secondary": {"used_percent": 10}},
                    },
                },
                {
                    "timestamp": "2026-05-09T02:00:04Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 130}},
                        "rate_limits": {"secondary": {"used_percent": 10.5}},
                    },
                },
                {
                    "timestamp": "2026-05-09T02:00:05Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "t1"},
                },
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            turns = parse_conversation_turns(path)
            self.assertEqual(len(turns), 1)
            self.assertEqual(turns[0].session_id, "s1")
            self.assertEqual(turns[0].token_total_end, 130)
            self.assertEqual(turns[0].sample_count, 2)
            self.assertEqual(turns[0].first_internal_turn_id, "t1")
            self.assertEqual(turns[0].last_internal_turn_id, "t1")

    def test_parse_conversation_turns_exposes_internal_delta_breakdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            rows = [
                {
                    "timestamp": "2026-05-09T02:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "s1"},
                },
                {
                    "timestamp": "2026-05-09T02:00:01Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "t1", "model": "gpt-a", "effort": "high"},
                },
                {
                    "timestamp": "2026-05-09T02:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "hi"},
                },
                {
                    "timestamp": "2026-05-09T02:00:03Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 120}},
                    },
                },
                {
                    "timestamp": "2026-05-09T02:00:04Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "t2", "model": "gpt-a", "effort": "high"},
                },
                {
                    "timestamp": "2026-05-09T02:00:05Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 200}},
                    },
                },
                {
                    "timestamp": "2026-05-09T02:00:06Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "t2"},
                },
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

            turns = parse_conversation_turns(path)

            self.assertEqual(len(turns), 1)
            self.assertEqual(turns[0].internal_turn_ids, ("t1", "t2"))
            self.assertEqual(
                getattr(turns[0], "internal_token_deltas", None),
                {"t1": 120, "t2": 80},
            )
            self.assertEqual(sum(turns[0].internal_token_deltas.values()), 200)

    def test_parse_conversation_turns_does_not_emit_active_eof_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            rows = [
                {
                    "timestamp": "2026-05-09T02:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "s1"},
                },
                {
                    "timestamp": "2026-05-09T02:00:01Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "t1", "model": "gpt-a", "effort": "high"},
                },
                {
                    "timestamp": "2026-05-09T02:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "hi"},
                },
                {
                    "timestamp": "2026-05-09T02:00:03Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 100}},
                    },
                },
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            turns = parse_conversation_turns(path)
            self.assertEqual(turns, [])


if __name__ == "__main__":
    unittest.main()
