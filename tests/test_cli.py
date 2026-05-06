from __future__ import annotations

import unittest
from unittest import mock

from codex_usage.cli import _default_hook_command, _toml_string


class CliTests(unittest.TestCase):
    def test_default_hook_command_quotes_repo_path_with_spaces(self) -> None:
        with mock.patch("codex_usage.cli.shutil.which", return_value=None):
            command = _default_hook_command()

        self.assertIn("PYTHONPATH=", command)
        self.assertIn("codex usage/src", command)
        self.assertIn("PYTHONPATH='", command)

    def test_toml_string_escapes_double_quotes(self) -> None:
        self.assertEqual(_toml_string('a "quoted" path'), 'a \\"quoted\\" path')


if __name__ == "__main__":
    unittest.main()
