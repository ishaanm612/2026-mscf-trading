"""Tests for local dotenv loading without real practice credentials."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from environment import load_env_file


class EnvironmentTests(unittest.TestCase):
    """Verify predictable dotenv parsing and shell-variable precedence."""

    def test_loads_quotes_comments_and_export_syntax(self) -> None:
        """Read standard local configuration without interpreting shell syntax."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("ONE=first # note\nexport TWO='two # kept'\nTHREE=\"three\"\n", encoding="utf-8")
            original = {key: os.environ.get(key) for key in ("ONE", "TWO", "THREE")}
            try:
                for key in original:
                    os.environ.pop(key, None)
                self.assertEqual(load_env_file(path), ("ONE", "TWO", "THREE"))
                self.assertEqual(os.environ["ONE"], "first")
                self.assertEqual(os.environ["TWO"], "two # kept")
                self.assertEqual(os.environ["THREE"], "three")
            finally:
                for key, value in original.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_shell_value_takes_precedence_without_override(self) -> None:
        """Leave an explicitly exported operational setting unchanged."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("RIT_TEST_SETTING=file\n", encoding="utf-8")
            previous = os.environ.get("RIT_TEST_SETTING")
            try:
                os.environ["RIT_TEST_SETTING"] = "shell"
                self.assertEqual(load_env_file(path), ())
                self.assertEqual(os.environ["RIT_TEST_SETTING"], "shell")
            finally:
                if previous is None:
                    os.environ.pop("RIT_TEST_SETTING", None)
                else:
                    os.environ["RIT_TEST_SETTING"] = previous


if __name__ == "__main__":
    unittest.main()
