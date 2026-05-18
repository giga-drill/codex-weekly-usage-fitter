from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from codex_usage.collector import UsageCollector
from codex_usage.transcript import WeeklyLimit


def _write_rollout(
    path: Path,
    *,
    session_id: str,
    turn_id: str,
    total_tokens: int,
    used_percent: float,
    model: str = "gpt-test",
    effort: str = "high",
    include_rate_limits: bool = True,
) -> None:
    token_count_payload = {
        "type": "token_count",
        "info": {"total_token_usage": {"total_tokens": total_tokens}},
    }
    if include_rate_limits:
        token_count_payload["rate_limits"] = {
            "secondary": {
                "used_percent": used_percent,
                "window_minutes": 10080,
                "resets_at": 1777609978,
            }
        }

    rows = [
        {
            "timestamp": "2026-05-08T01:00:00Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/tmp/project"},
        },
        {
            "timestamp": "2026-05-08T01:00:01Z",
            "type": "turn_context",
            "payload": {"turn_id": turn_id, "model": model, "effort": effort},
        },
        {
            "timestamp": "2026-05-08T01:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hello"},
        },
        {
            "timestamp": "2026-05-08T01:00:03Z",
            "type": "event_msg",
            "payload": token_count_payload,
        },
        {
            "timestamp": "2026-05-08T01:00:04Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": turn_id},
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class TranscriptBackfillTests(unittest.TestCase):
    def test_recent_scan_skips_old_mtime_but_backfill_includes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "usage"
            codex_home = root / "codex"
            session_dir = codex_home / "sessions" / "2026" / "05" / "08"
            session_dir.mkdir(parents=True)

            old_path = session_dir / "rollout-old.jsonl"
            new_path = session_dir / "rollout-new.jsonl"
            _write_rollout(
                old_path,
                session_id="019e1000-0000-7000-8000-000000000000",
                turn_id="019e1000-0001-7000-8000-000000000000",
                total_tokens=100,
                used_percent=10.0,
            )
            _write_rollout(
                new_path,
                session_id="019e2000-0000-7000-8000-000000000000",
                turn_id="019e2000-0001-7000-8000-000000000000",
                total_tokens=220,
                used_percent=12.0,
            )

            now = time.time()
            os.utime(old_path, (now - 7 * 24 * 3600, now - 7 * 24 * 3600))
            os.utime(new_path, (now, now))

            collector = UsageCollector(
                home, codex_home=codex_home, delay_seconds=0, use_app_server=False
            )
            try:
                recent = collector.scan_transcripts(since_seconds=3600)
                full = collector.scan_transcripts(since_seconds=None)
            finally:
                collector.close()

            self.assertEqual(recent.files_considered, 1)
            self.assertEqual(recent.turns_considered, 1)
            self.assertEqual(recent.samples_inserted, 1)

            self.assertEqual(full.files_considered, 2)
            self.assertEqual(full.turns_considered, 2)
            self.assertEqual(full.samples_inserted, 1)

    def test_backfill_is_idempotent_for_samples_and_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "usage"
            codex_home = root / "codex"
            session_dir = codex_home / "sessions" / "2026" / "05" / "08"
            session_dir.mkdir(parents=True)

            _write_rollout(
                session_dir / "rollout-a.jsonl",
                session_id="019e3000-0000-7000-8000-000000000000",
                turn_id="019e3000-0001-7000-8000-000000000000",
                total_tokens=100,
                used_percent=10.0,
            )
            _write_rollout(
                session_dir / "rollout-b.jsonl",
                session_id="019e4000-0000-7000-8000-000000000000",
                turn_id="019e4000-0001-7000-8000-000000000000",
                total_tokens=220,
                used_percent=12.0,
            )

            collector = UsageCollector(
                home, codex_home=codex_home, delay_seconds=0, use_app_server=False
            )
            try:
                with mock.patch.object(
                    collector.store,
                    "rebuild_epochs_and_fits",
                    wraps=collector.store.rebuild_epochs_and_fits,
                ) as rebuild:
                    first = collector.scan_transcripts(
                        since_seconds=None, rebuild_per_insert=False
                    )
                    first_rebuild_count = rebuild.call_count
                counts_after_first = _table_counts(collector)
                with mock.patch.object(
                    collector.store,
                    "rebuild_epochs_and_fits",
                    wraps=collector.store.rebuild_epochs_and_fits,
                ) as rebuild:
                    second = collector.scan_transcripts(
                        since_seconds=None, rebuild_per_insert=False
                    )
                    second_rebuild_count = rebuild.call_count
                counts_after_second = _table_counts(collector)
            finally:
                collector.close()

            self.assertEqual(first.files_considered, 2)
            self.assertEqual(first.turns_considered, 2)
            self.assertEqual(first.samples_inserted, 2)
            self.assertEqual(second.files_considered, 2)
            self.assertEqual(second.turns_considered, 2)
            self.assertEqual(second.samples_inserted, 0)
            self.assertEqual(first_rebuild_count, 1)
            self.assertEqual(second_rebuild_count, 0)

            self.assertGreater(counts_after_first["samples"], 0)
            self.assertGreater(counts_after_first["sessions"], 0)
            self.assertGreater(counts_after_first["conversation_turns"], 0)
            self.assertGreater(counts_after_first["epochs"], 0)
            self.assertGreater(counts_after_first["fits"], 0)
            self.assertGreater(counts_after_first["model_effort_fits"], 0)
            self.assertGreater(counts_after_first["model_effort_global_fits"], 0)
            self.assertGreater(counts_after_first["usage_movement_events"], 0)

            self.assertEqual(counts_after_second, counts_after_first)

    def test_backfill_without_rate_limits_is_idempotent_when_app_server_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "usage"
            codex_home = root / "codex"
            session_dir = codex_home / "sessions" / "2026" / "05" / "08"
            session_dir.mkdir(parents=True)

            _write_rollout(
                session_dir / "rollout-missing-ratelimit.jsonl",
                session_id="019e5000-0000-7000-8000-000000000000",
                turn_id="019e5000-0001-7000-8000-000000000000",
                total_tokens=321,
                used_percent=0.0,
                include_rate_limits=False,
            )

            app_server = _ChangingWeeklyAppServer(
                [
                    WeeklyLimit(used_percent=10, resets_at=111, window_minutes=10080),
                    WeeklyLimit(used_percent=60, resets_at=222, window_minutes=10080),
                ]
            )
            collector = UsageCollector(
                home,
                codex_home=codex_home,
                app_server=app_server,
                delay_seconds=0,
                use_app_server=False,
            )
            try:
                first = collector.scan_transcripts(
                    since_seconds=None, rebuild_per_insert=False
                )
                counts_after_first = _table_counts(collector)
                second = collector.scan_transcripts(
                    since_seconds=None, rebuild_per_insert=False
                )
                counts_after_second = _table_counts(collector)
            finally:
                collector.close()

            self.assertEqual(first.samples_inserted, 1)
            self.assertEqual(second.samples_inserted, 0)
            self.assertEqual(app_server.read_calls, 0)
            self.assertEqual(counts_after_second, counts_after_first)

    def test_recent_scan_keeps_per_insert_rebuild_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "usage"
            codex_home = root / "codex"
            session_dir = codex_home / "sessions" / "2026" / "05" / "08"
            session_dir.mkdir(parents=True)

            _write_rollout(
                session_dir / "rollout-recent.jsonl",
                session_id="019e6000-0000-7000-8000-000000000000",
                turn_id="019e6000-0001-7000-8000-000000000000",
                total_tokens=300,
                used_percent=16.0,
            )

            collector = UsageCollector(
                home, codex_home=codex_home, delay_seconds=0, use_app_server=False
            )
            try:
                with mock.patch.object(
                    collector.store,
                    "rebuild_epochs_and_fits",
                    wraps=collector.store.rebuild_epochs_and_fits,
                ) as rebuild:
                    inserted = collector.scan_recent_transcripts(since_seconds=3600)
                    rebuild_count = rebuild.call_count
            finally:
                collector.close()

            self.assertEqual(inserted, 1)
            self.assertEqual(rebuild_count, 1)

    def test_recent_scan_with_fallback_then_backfill_does_not_duplicate_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "usage"
            codex_home = root / "codex"
            session_dir = codex_home / "sessions" / "2026" / "05" / "08"
            session_dir.mkdir(parents=True)

            _write_rollout(
                session_dir / "rollout-recent-to-backfill.jsonl",
                session_id="019e7000-0000-7000-8000-000000000000",
                turn_id="019e7000-0001-7000-8000-000000000000",
                total_tokens=456,
                used_percent=0.0,
                include_rate_limits=False,
            )

            app_server = _ChangingWeeklyAppServer(
                [WeeklyLimit(used_percent=12, resets_at=333, window_minutes=10080)]
            )
            recent_collector = UsageCollector(
                home,
                codex_home=codex_home,
                app_server=app_server,
                delay_seconds=0,
                use_app_server=True,
            )
            try:
                inserted_recent = recent_collector.scan_recent_transcripts(since_seconds=3600)
                counts_after_recent = _table_counts(recent_collector)
            finally:
                recent_collector.close()

            backfill_collector = UsageCollector(
                home, codex_home=codex_home, delay_seconds=0, use_app_server=False
            )
            try:
                inserted_backfill = backfill_collector.scan_transcripts(
                    since_seconds=None, rebuild_per_insert=False
                )
                counts_after_backfill = _table_counts(backfill_collector)
            finally:
                backfill_collector.close()

            self.assertEqual(inserted_recent, 1)
            self.assertGreaterEqual(app_server.read_calls, 1)
            self.assertEqual(inserted_backfill.samples_inserted, 0)
            self.assertEqual(
                counts_after_backfill["samples"], counts_after_recent["samples"]
            )
            self.assertEqual(
                counts_after_backfill["conversation_turns"],
                counts_after_recent["conversation_turns"],
            )
            self.assertEqual(
                counts_after_backfill["epochs"], counts_after_recent["epochs"]
            )
            self.assertEqual(counts_after_backfill["fits"], counts_after_recent["fits"])
            self.assertEqual(
                counts_after_backfill["model_effort_fits"],
                counts_after_recent["model_effort_fits"],
            )
            self.assertEqual(
                counts_after_backfill["model_effort_global_fits"],
                counts_after_recent["model_effort_global_fits"],
            )
            self.assertEqual(
                counts_after_backfill["usage_movement_events"],
                counts_after_recent["usage_movement_events"],
            )


def _table_counts(collector: UsageCollector) -> dict[str, int]:
    conn = collector.store.conn
    tables = [
        "samples",
        "sessions",
        "conversation_turns",
        "epochs",
        "fits",
        "model_effort_fits",
        "model_effort_global_fits",
        "usage_movement_events",
    ]
    return {
        table: conn.execute(f"SELECT COUNT(*) AS value FROM {table}").fetchone()["value"]
        for table in tables
    }


class _ChangingWeeklyAppServer:
    def __init__(self, values: list[WeeklyLimit]) -> None:
        self._values = list(values)
        self._index = 0
        self.read_calls = 0

    def read_weekly_limit(self) -> WeeklyLimit | None:
        self.read_calls += 1
        if not self._values:
            return None
        value = self._values[min(self._index, len(self._values) - 1)]
        self._index += 1
        return value

    def close(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
