"""Tests for CLI help output, actionable errors, secret non-echoing, and exit codes."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from satubrain.cli import main


class CliErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "memory.sqlite3"

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_missing_command_and_invalid_arguments_exit_2(self):
        for argv in ([], ["remember"], ["search", "--task", "DEMO-1", "--kind", "invalid"]):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
                main(argv)
            self.assertEqual(ctx.exception.code, 2)
            self.assertTrue(err.getvalue().strip())

    def test_doctor_reports_missing_fts5_with_exit_1(self):
        class _NoFtsConn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql):
                raise sqlite3.OperationalError("no such module: fts5")

        with patch("satubrain.cli.sqlite3.connect", return_value=_NoFtsConn()):
            code, _out, err = self._run(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("FTS5", err)
        self.assertIn("satubrain doctor", err)

    def test_import_missing_and_invalid_json_give_actionable_errors(self):
        missing = Path(self.tmp.name) / "does-not-exist.json"
        code, _out, err = self._run(["--db", str(self.db), "import", str(missing)])
        self.assertEqual(code, 1)
        self.assertIn("snapshot file not found", err)

        bad_json = Path(self.tmp.name) / "bad.json"
        bad_json.write_text("{not valid json", encoding="utf-8")
        code, _out, err = self._run(["--db", str(self.db), "import", str(bad_json)])
        self.assertEqual(code, 1)
        self.assertIn("invalid JSON", err)

    def test_export_existing_file_gives_actionable_error(self):
        target = Path(self.tmp.name) / "existing.json"
        target.write_text("{}", encoding="utf-8")
        code, _out, err = self._run(["--db", str(self.db), "export", str(target)])
        self.assertEqual(code, 1)
        self.assertIn("new file path", err)

    def test_sync_errors_do_not_echo_passphrase(self):
        sentinel = "synthetic-passphrase-never-echo-987654"
        sync_dir = Path(self.tmp.name) / "sync"
        sync_dir.mkdir()
        with patch.dict(os.environ, {"SATUBRAIN_SYNC_PASSPHRASE": sentinel}, clear=False):
            with patch("satubrain.cli.push", side_effect=ValueError(f"failed using {sentinel}")):
                code, out, err = self._run(["--db", str(self.db), "sync", "push", str(sync_dir)])
        self.assertEqual(code, 1)
        self.assertNotIn(sentinel, out)
        self.assertNotIn(sentinel, err)
        self.assertIn("[redacted]", err)

    def test_remember_rejects_credential_without_echoing_secret(self):
        fake_secret = "tok_" + "z" * 32
        code, out, err = self._run(
            ["--db", str(self.db), "remember", "--project", "demo", "--branch", "main",
             "--task", "DEMO-1", f"api_key = {fake_secret}"]
        )
        self.assertEqual(code, 1)
        self.assertNotIn(fake_secret, out)
        self.assertNotIn(fake_secret, err)
        self.assertIn("credential", err.lower())


if __name__ == "__main__":
    unittest.main()
