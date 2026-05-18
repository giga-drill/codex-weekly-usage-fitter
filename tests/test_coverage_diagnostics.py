from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from codex_usage.coverage_diagnostics import build_coverage_diagnostics
from codex_usage.store import UsageStore


def _write_rollout(
    path: Path,
    *,
    session_id: str,
    turn_id: str,
    cwd: str,
    model: str,
    effort: str,
    timestamp: str,
    include_token_count: bool,
    include_task_complete: bool,
    total_tokens: int = 100,
) -> None:
    rows: list[dict[str, object]] = [
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": cwd},
        },
        {
            "timestamp": timestamp,
            "type": "turn_context",
            "payload": {"turn_id": turn_id, "model": model, "effort": effort},
        },
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hello"},
        },
    ]
    if include_token_count:
        rows.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": total_tokens}},
                },
            }
        )
    if include_task_complete:
        rows.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": turn_id},
            }
        )
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class CoverageDiagnosticsTests(unittest.TestCase):
    def test_reports_layered_coverage_for_all_and_recent_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "usage"
            codex_home = root / "codex"
            session_dir = codex_home / "sessions" / "2026" / "05" / "18"
            session_dir.mkdir(parents=True)

            no_token = session_dir / "rollout-no-token.jsonl"
            active_missing = session_dir / "rollout-active-missing.jsonl"
            completed_covered = session_dir / "rollout-complete-covered.jsonl"
            completed_missing_raw = session_dir / "rollout-complete-missing-raw.jsonl"
            active_sessions_only = session_dir / "rollout-active-sessions-only.jsonl"

            _write_rollout(
                no_token,
                session_id="s-no-token",
                turn_id="t-no-token",
                cwd="/tmp/no-token",
                model="gpt-a",
                effort="high",
                timestamp="2026-05-10T01:00:00Z",
                include_token_count=False,
                include_task_complete=False,
            )
            _write_rollout(
                active_missing,
                session_id="s-active-missing",
                turn_id="t-active-missing",
                cwd="/tmp/active-missing",
                model="gpt-b",
                effort="medium",
                timestamp="2026-05-18T02:00:00Z",
                include_token_count=True,
                include_task_complete=False,
                total_tokens=120,
            )
            _write_rollout(
                completed_covered,
                session_id="s-complete-covered",
                turn_id="t-complete-covered",
                cwd="/tmp/complete-covered",
                model="gpt-c",
                effort="low",
                timestamp="2026-05-18T03:00:00Z",
                include_token_count=True,
                include_task_complete=True,
                total_tokens=260,
            )
            _write_rollout(
                completed_missing_raw,
                session_id="s-complete-missing-raw",
                turn_id="t-complete-missing-raw",
                cwd="/tmp/complete-missing-raw",
                model="gpt-d",
                effort="xhigh",
                timestamp="2026-05-18T04:00:00Z",
                include_token_count=True,
                include_task_complete=True,
                total_tokens=310,
            )
            _write_rollout(
                active_sessions_only,
                session_id="s-active-sessions-only",
                turn_id="t-active-sessions-only",
                cwd="/tmp/active-sessions-only",
                model="gpt-e",
                effort="high",
                timestamp="2026-05-18T05:00:00Z",
                include_token_count=True,
                include_task_complete=False,
                total_tokens=400,
            )

            now = time.time()
            os.utime(no_token, (now - 96 * 3600, now - 96 * 3600))
            for path in [
                active_missing,
                completed_covered,
                completed_missing_raw,
                active_sessions_only,
            ]:
                os.utime(path, (now, now))

            store = UsageStore(home)
            try:
                with store.conn:
                    store.conn.execute(
                        """
                        INSERT INTO sessions (
                            session_id, last_total_tokens, last_seen_at, transcript_path, model, reasoning_effort
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "s-complete-covered",
                            260,
                            "2026-05-18T03:00:00Z",
                            str(completed_covered),
                            "gpt-c",
                            "low",
                        ),
                    )
                    store.conn.execute(
                        """
                        INSERT INTO sessions (
                            session_id, last_total_tokens, last_seen_at, transcript_path, model, reasoning_effort
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "s-active-sessions-only",
                            400,
                            "2026-05-18T05:00:00Z",
                            str(active_sessions_only),
                            "gpt-e",
                            "high",
                        ),
                    )
                    store.conn.execute(
                        """
                        INSERT INTO samples (
                            event_id, observed_at, session_id, turn_id, model,
                            reasoning_effort, transcript_path, token_delta
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "event-complete-covered",
                            "2026-05-18T03:00:00Z",
                            "s-complete-covered",
                            "t-complete-covered",
                            "gpt-c",
                            "low",
                            str(completed_covered),
                            260,
                        ),
                    )
                    store.conn.execute(
                        """
                        INSERT INTO conversation_turns (
                            session_id, conversation_turn_key, user_message_timestamp,
                            user_message_index, start_observed_at, end_observed_at,
                            transcript_path, first_internal_turn_id, last_internal_turn_id,
                            internal_turn_ids_json, internal_token_deltas_json, model,
                            reasoning_effort, sample_count, token_delta, token_total_start,
                            token_total_end, completed
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            "s-complete-covered",
                            "coverage-turn-1",
                            "2026-05-18T03:00:00Z",
                            1,
                            "2026-05-18T03:00:00Z",
                            "2026-05-18T03:00:00Z",
                            str(completed_covered),
                            "t-complete-covered",
                            "t-complete-covered",
                            json.dumps(["t-complete-covered"]),
                            json.dumps({"t-complete-covered": 260}),
                            "gpt-c",
                            "low",
                            1,
                            260,
                            0,
                            260,
                        ),
                    )
            finally:
                store.close()

            report = build_coverage_diagnostics(
                home=home,
                codex_home=codex_home,
                since_hours=48,
                missing_limit=2,
            )

            all_history = report["coverage"]["all_history"]
            recent = report["coverage"]["recent_window"]
            examples = report["coverage"]["recent_missing_examples"]

            self.assertEqual(all_history["transcript_files_discovered_count"], 5)
            self.assertEqual(all_history["sessions_discovered_count"], 5)
            self.assertEqual(all_history["with_token_count_count"], 4)
            self.assertEqual(all_history["with_token_count_and_task_complete_count"], 2)
            self.assertEqual(all_history["present_in_sessions_count"], 2)
            self.assertEqual(all_history["present_in_samples_count"], 1)
            self.assertEqual(all_history["present_in_raw_observation_layer_count"], 2)
            self.assertEqual(
                all_history["present_in_completed_conversation_turns_count"], 1
            )
            self.assertEqual(all_history["missing_from_sessions_count"], 2)
            self.assertEqual(all_history["missing_from_conversation_turns_count"], 1)

            self.assertEqual(recent["transcript_files_discovered_count"], 4)
            self.assertEqual(recent["sessions_discovered_count"], 4)
            self.assertEqual(recent["with_token_count_count"], 4)
            self.assertEqual(recent["with_token_count_and_task_complete_count"], 2)
            self.assertEqual(recent["missing_from_conversation_turns_count"], 1)

            self.assertEqual(len(examples), 2)
            self.assertEqual(examples[0]["session_id"], "s-complete-missing-raw")
            self.assertEqual(examples[1]["session_id"], "s-active-missing")
            self.assertIn("transcript_path", examples[0])
            self.assertIn("cwd", examples[0])
            self.assertIn("model", examples[0])
            self.assertIn("reasoning_effort", examples[0])
            self.assertIn("last_timestamp", examples[0])
            self.assertTrue(examples[0]["missing_from_conversation_turns"])
            self.assertFalse(examples[1]["missing_from_conversation_turns"])
            self.assertNotIn(
                "s-active-sessions-only", [item["session_id"] for item in examples]
            )

    def test_old_db_without_conversation_turns_keeps_raw_presence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "usage"
            home.mkdir(parents=True, exist_ok=True)
            codex_home = root / "codex"
            session_dir = codex_home / "sessions" / "2026" / "05" / "18"
            session_dir.mkdir(parents=True)

            transcript_path = session_dir / "rollout-old-db.jsonl"
            _write_rollout(
                transcript_path,
                session_id="legacy-session-1",
                turn_id="legacy-turn-1",
                cwd="/tmp/legacy",
                model="gpt-legacy",
                effort="medium",
                timestamp="2026-05-18T09:00:00Z",
                include_token_count=True,
                include_task_complete=True,
                total_tokens=888,
            )
            now = time.time()
            os.utime(transcript_path, (now, now))

            conn = sqlite3.connect(home / "usage.sqlite")
            try:
                conn.execute(
                    """
                    CREATE TABLE sessions (
                        session_id TEXT PRIMARY KEY,
                        transcript_path TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE samples (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT,
                        transcript_path TEXT
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO sessions (session_id, transcript_path) VALUES (?, ?)",
                    ("legacy-session-1", str(transcript_path)),
                )
                conn.execute(
                    "INSERT INTO samples (session_id, transcript_path) VALUES (?, ?)",
                    ("legacy-session-1", str(transcript_path)),
                )
                conn.commit()
            finally:
                conn.close()

            report = build_coverage_diagnostics(
                home=home,
                codex_home=codex_home,
                since_hours=48,
                missing_limit=5,
            )
            all_history = report["coverage"]["all_history"]

            self.assertEqual(all_history["with_token_count_count"], 1)
            self.assertEqual(all_history["present_in_sessions_count"], 1)
            self.assertEqual(all_history["present_in_samples_count"], 1)
            self.assertEqual(all_history["present_in_raw_observation_layer_count"], 1)
            self.assertEqual(all_history["missing_from_sessions_count"], 0)
            self.assertEqual(
                all_history["present_in_completed_conversation_turns_count"], 0
            )
            self.assertEqual(all_history["missing_from_conversation_turns_count"], 1)


if __name__ == "__main__":
    unittest.main()
