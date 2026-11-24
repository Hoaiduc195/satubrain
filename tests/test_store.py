from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from satubrain.cli import history_import
from satubrain.scope import Workspace, remote_id
from satubrain.store import Store, json_bytes, record

SCOPE = {"project": "repo-a", "task": "DEMO-17", "branch": "main"}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.store = Store(self.folder / "memory.sqlite3")
        self.scope = tuple(SCOPE.values())

    def save(self, text, kind="note", **extra):
        return self.store.save(**(SCOPE | {"kind": kind, "text": text} | extra))

    def test_persistence_and_scope_isolation(self):
        for overrides in ({"project": "repo-b"}, {"task": "QT-2"}, {"branch": "feature"}):
            self.save("migration stays isolated", **overrides)
        own = self.save("migration on this branch")
        reopened = Store(self.store.path)
        self.assertEqual([row["id"] for row in reopened.search(*self.scope, "migration")], [own["id"]])
        self.assertEqual(reopened.search(*self.scope, '"migration" -- words OR'), reopened.search(*self.scope, "migration"))
        self.assertEqual(reopened.search(*self.scope, "!!!"), [])
        self.assertFalse(reopened.delete("repo-b", "DEMO-17", "main", own["id"]))

    def test_duplicate_detection_and_simultaneous_saves(self):
        self.store.scopes()
        notes = ["Ａuth  FLOW", "auth flow"] * 6
        with ThreadPoolExecutor(max_workers=6) as workers:
            results = list(workers.map(self.save, notes))
        self.assertEqual(sum(result["status"] == "saved" for result in results), 1)
        self.assertEqual(len({result["id"] for result in results}), 1)
        self.assertEqual(len(self.store.search(*self.scope)), 1)

    def test_context_priority_and_byte_budget(self):
        latest = "Latest branch deployment is green. " + "日🙂\"\\ " * 60
        self.save("Database retry uses WAL.")
        self.save("Other checkpoint.", "handoff")
        self.save(latest, "handoff")
        result = self.store.context(*self.scope, "retry", budget=264)
        items = result["items"]
        self.assertTrue(items[0]["text"].startswith("Latest branch deployment"))
        self.assertTrue(items[0]["text"].endswith("…"))
        self.assertEqual(items[1]["text"], "Database retry uses WAL.")
        self.assertEqual(result["context_bytes"], len(json_bytes(items)))
        self.assertLessEqual(len(json_bytes(items)), result["budget"])

    def test_snapshot_merge_converges_and_propagates_deletion(self):
        other = Store(self.folder / "other.sqlite3")
        later = self.save("  WAL mode", created_at="2025-06-05T00:00:00+00:00")
        earlier = other.save(**SCOPE, kind="note", text="wal mode", created_at="2025-05-05T00:00:00+00:00")
        self.assertEqual(later["id"], earlier["id"])
        self.store.merge(other.export())
        other.merge(self.store.export())
        self.assertEqual(self.store.export(), other.export())
        self.assertEqual(self.store.search(*self.scope)[0]["text"], "wal mode")
        stale = self.store.export()
        self.assertTrue(self.store.delete(*self.scope, later["id"]))
        other.merge(self.store.export())
        other.merge(stale)
        self.assertEqual(other.search(*self.scope, "wal"), [])
        self.assertEqual(other.save(**SCOPE, kind="note", text="WAL mode")["status"], "deleted")

    def test_import_rejects_invalid_entry_atomically(self):
        good = record(**SCOPE, kind="note", text="first note")
        invalid = record(**SCOPE, kind="note", text="second note")
        invalid["text"] = "changed"
        with self.assertRaisesRegex(ValueError, "modified"):
            self.store.merge({"version": 1, "entries": [good, invalid], "tombstones": []})
        self.assertEqual(self.store.search(*self.scope), [])
        with self.assertRaisesRegex(ValueError, "credential"):
            self.save("api_key = " + "a" * 16)
        self.assertEqual(self.store.search(*self.scope), [])

    def test_credential_formats_are_rejected_before_storage(self):
        sample = "x" * 32
        cases = (
            json.dumps({"api_key": sample}),
            "CLIENT_SECRET = " + sample,
            "Data Source=localhost;User ID=demo;Password=" + sample,
            f"postgresql://user:{sample}@example.test/demo",
            "Authorization: Basic " + sample,
            "eyJ" + sample + "." + sample + "." + sample,
        )
        for index, candidate in enumerate(cases):
            with self.subTest(case=index), self.assertRaisesRegex(ValueError, "credential"):
                self.save(candidate)
        self.assertEqual(self.store.search(*self.scope), [])
        self.assertEqual(self.save("Rotate the API key before publishing.")["status"], "saved")

    def test_history_order_and_import_idempotence(self):
        filename = self.folder / "talk.jsonl"
        messages = [{"role": "assistant", "position": 2, "text": "Memory ready"},
                    {"role": "user", "position": 0, "text": "Check memory"}]
        filename.write_text("\n".join(json.dumps(message) for message in messages), encoding="utf-8")
        self.assertEqual(history_import(self.store, filename, SCOPE, "codex", "session-a")["added"], 2)
        self.assertEqual(history_import(self.store, filename, SCOPE, "codex", "session-a")["added"], 0)
        history = self.store.history(*self.scope, "session-a")
        self.assertEqual([row["position"] for row in history], [0, 2])
        self.assertEqual(len(self.store.history(*self.scope, "session-a", after=0)), 1)
        self.assertEqual(len(self.store.search(*self.scope, "memory", kind="message")), 2)


class WorkspaceTests(unittest.TestCase):
    def test_remote_formats(self):
        expected = "github.com/owner/repo"
        sample = "x" * 12
        self.assertEqual(remote_id(f"https://user:{sample}@GitHub.COM/owner/repo.git?ignored=1"), expected)
        self.assertEqual(remote_id("git@github.com:owner/repo.git"), expected)
        self.assertEqual(remote_id("ssh://git@github.com:22/owner/repo.git"), expected)
        self.assertIsNone(remote_id("/some/local/repo"))

    @unittest.skipUnless(shutil.which("git"), "Git is required for workspace resolution")
    def test_git_tracks_current_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            def run(*args):
                subprocess.run(["git", "-C", directory, *args], check=True, capture_output=True)

            run("init", "-b", "main")
            run("remote", "add", "origin", "git@example.org:team/repo.git")
            self.assertEqual(Workspace.resolve(directory), Workspace("example.org/team/repo", "main"))
            run("symbolic-ref", "HEAD", "refs/heads/feature")
            self.assertEqual(Workspace.resolve(directory), Workspace("example.org/team/repo", "feature"))

    def test_missing_git_keeps_local_projects_separate(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with patch("satubrain.scope.git", return_value=None):
                self.assertNotEqual(Workspace.resolve(first).project, Workspace.resolve(second).project)
                self.assertEqual(Workspace.resolve(first, "portable"), Workspace("portable", "default"))
