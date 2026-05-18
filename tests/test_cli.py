from __future__ import annotations

import io
import shlex
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from codex_usage.cli import _default_hook_command, _toml_string, main
from codex_usage.collector import TranscriptScanStats


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


if __name__ == "__main__":
    unittest.main()
