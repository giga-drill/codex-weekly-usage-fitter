from __future__ import annotations

import io
import json
import tempfile
import shlex
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from codex_usage.cli import _default_hook_command, _toml_string, main
from codex_usage.collector import TranscriptScanStats
from codex_usage.store import UsageStore


class CliTests(unittest.TestCase):
    def test_default_hook_command_uses_repo_src_pythonpath(self) -> None:
        with mock.patch("codex_usage.cli.shutil.which", return_value=None):
            command = _default_hook_command()

        expected_src = str((Path(__file__).resolve().parents[1] / "src"))
        self.assertIn("PYTHONPATH=", command)
        self.assertIn(expected_src, command)
        if " " in expected_src:
            self.assertIn(f"PYTHONPATH='{expected_src}'", command)
        else:
            self.assertIn(f"PYTHONPATH={expected_src}", command)

    def test_default_hook_command_quotes_mocked_space_path(self) -> None:
        fake_cli_path = "/tmp/codex usage/src/codex_usage/cli.py"
        expected_src = str((Path(fake_cli_path).resolve().parents[2] / "src"))
        expected_pythonpath = f"PYTHONPATH={shlex.quote(expected_src)}"
        with mock.patch("codex_usage.cli.__file__", fake_cli_path):
            with mock.patch("codex_usage.cli.shutil.which", return_value=None):
                command = _default_hook_command()

        self.assertIn(expected_pythonpath, command)

    def test_toml_string_escapes_double_quotes(self) -> None:
        self.assertEqual(_toml_string('a "quoted" path'), 'a \\"quoted\\" path')

    def test_backfill_transcripts_command_prints_full_scan_stats(self) -> None:
        collector = mock.Mock()
        collector.scan_transcripts.return_value = TranscriptScanStats(
            files_considered=3,
            turns_considered=8,
            samples_inserted=2,
        )
        output = io.StringIO()
        with mock.patch("codex_usage.cli.UsageCollector", return_value=collector) as mocked:
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "--home",
                        "/tmp/codex-usage-tests",
                        "backfill-transcripts",
                    ]
                )

        self.assertEqual(exit_code, 0)
        mocked.assert_called_once_with(
            mock.ANY,
            delay_seconds=0,
            use_app_server=False,
        )
        collector.scan_transcripts.assert_called_once_with(
            since_seconds=None, rebuild_per_insert=False
        )
        collector.close.assert_called_once()
        text = output.getvalue()
        self.assertIn("Considered 3 transcript file(s), 8 transcript turn(s).", text)
        self.assertIn("Inserted 2 transcript sample(s).", text)

    def test_backfill_transcripts_with_app_server_flag_enables_fallback(self) -> None:
        collector = mock.Mock()
        collector.scan_transcripts.return_value = TranscriptScanStats()
        with mock.patch("codex_usage.cli.UsageCollector", return_value=collector) as mocked:
            exit_code = main(
                [
                    "--home",
                    "/tmp/codex-usage-tests",
                    "backfill-transcripts",
                    "--with-app-server",
                ]
            )

        self.assertEqual(exit_code, 0)
        mocked.assert_called_once_with(
            mock.ANY,
            delay_seconds=0,
            use_app_server=True,
        )

    def test_coverage_command_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "usage"
            codex_home = root / "codex"
            session_dir = codex_home / "sessions" / "2026" / "05" / "18"
            session_dir.mkdir(parents=True)

            rollout_path = session_dir / "rollout-cli-coverage.jsonl"
            rollout_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-05-18T01:00:00Z",
                                "type": "session_meta",
                                "payload": {"id": "cli-session", "cwd": "/tmp/cli"},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-05-18T01:00:01Z",
                                "type": "turn_context",
                                "payload": {
                                    "turn_id": "cli-turn",
                                    "model": "gpt-cli",
                                    "effort": "high",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-05-18T01:00:02Z",
                                "type": "event_msg",
                                "payload": {
                                    "type": "token_count",
                                    "info": {
                                        "total_token_usage": {"total_tokens": 123}
                                    },
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            store = UsageStore(home)
            store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "--home",
                        str(home),
                        "coverage",
                        "--codex-home",
                        str(codex_home),
                        "--since-hours",
                        "72",
                        "--missing-limit",
                        "1",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertIn("coverage", payload)
            self.assertIn("all_history", payload["coverage"])
            self.assertIn("recent_window", payload["coverage"])
            self.assertEqual(payload["recent_window_hours"], 72.0)
            self.assertEqual(payload["missing_limit"], 1)

    def test_coverage_command_text_output_shows_missing_examples_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "usage"
            codex_home = root / "codex"
            session_dir = codex_home / "sessions" / "2026" / "05" / "18"
            session_dir.mkdir(parents=True)

            rollout_path = session_dir / "rollout-cli-coverage-text.jsonl"
            rollout_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-05-18T01:00:00Z",
                                "type": "session_meta",
                                "payload": {
                                    "id": "cli-session-text",
                                    "cwd": "/tmp/cli-text",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-05-18T01:00:01Z",
                                "type": "turn_context",
                                "payload": {
                                    "turn_id": "cli-turn-text",
                                    "model": "gpt-cli",
                                    "effort": "medium",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-05-18T01:00:02Z",
                                "type": "event_msg",
                                "payload": {
                                    "type": "token_count",
                                    "info": {
                                        "total_token_usage": {"total_tokens": 456}
                                    },
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            store = UsageStore(home)
            store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "--home",
                        str(home),
                        "coverage",
                        "--codex-home",
                        str(codex_home),
                        "--missing-limit",
                        "1",
                    ]
                )

            self.assertEqual(exit_code, 0)
            text = output.getvalue()
            self.assertIn("Transcript coverage audit", text)
            self.assertIn("Recent missing examples:", text)
            self.assertIn("session_id=cli-session-text", text)


if __name__ == "__main__":
    unittest.main()
