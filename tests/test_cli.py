from __future__ import annotations

import shlex
import unittest
from pathlib import Path
from unittest import mock

from codex_usage.cli import _default_hook_command, _toml_string


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


if __name__ == "__main__":
    unittest.main()
