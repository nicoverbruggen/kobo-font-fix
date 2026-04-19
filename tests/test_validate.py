from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import validate


class ValidateScriptTests(unittest.TestCase):
    def test_find_available_ots_prefers_system_binary(self) -> None:
        with mock.patch("validate.shutil.which", return_value="/usr/local/bin/ots-sanitize"):
            with mock.patch("validate._find_binary") as find_binary:
                resolved = validate.find_available_ots()

        self.assertEqual(resolved, Path("/usr/local/bin/ots-sanitize"))
        find_binary.assert_not_called()

    def test_find_available_ots_uses_cached_binary_when_system_binary_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tools_dir = Path(tmpdir)
            cached_binary = tools_dir / "ots-v9.2.0" / "ots-sanitize"
            cached_binary.parent.mkdir(parents=True)
            cached_binary.write_text("", encoding="utf-8")

            with mock.patch("validate.TOOLS_DIR", tools_dir):
                with mock.patch("validate.shutil.which", return_value=None):
                    resolved = validate.find_available_ots()

        self.assertEqual(resolved, cached_binary)

    def test_find_available_ots_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tools_dir = Path(tmpdir) / ".tools"
            with mock.patch("validate.TOOLS_DIR", tools_dir):
                with mock.patch("validate.shutil.which", return_value=None):
                    resolved = validate.find_available_ots()

        self.assertIsNone(resolved)

    def test_ensure_ots_downloads_when_not_already_available(self) -> None:
        with mock.patch("validate.find_available_ots", return_value=None):
            with mock.patch("validate.TOOLS_DIR") as tools_dir:
                with mock.patch("validate._fetch_latest_release", return_value={"tag_name": "v1", "assets": []}):
                    with self.assertRaises(RuntimeError):
                        validate._ensure_ots()

        tools_dir.mkdir.assert_called_once_with(exist_ok=True)


if __name__ == "__main__":
    unittest.main()
