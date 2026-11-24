"""Deterministic regression fixtures for duplicate notes, branch isolation, and bounded retrieval."""

from pathlib import Path
import tempfile
import unittest

from satubrain.store import Store, json_bytes


FIXTURE_SCOPE = {"project": "alpha-app", "task": "TASK-101", "branch": "main"}


class RetrievalRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "regression.sqlite3")
        self.scope = (FIXTURE_SCOPE["project"], FIXTURE_SCOPE["task"], FIXTURE_SCOPE["branch"])

    def _save(self, text, kind="note", created_at="2026-01-10T10:00:00+00:00", **overrides):
        fields = {
            **FIXTURE_SCOPE,
            "kind": kind,
            "text": text,
            "created_at": created_at,
            **overrides,
        }
        return self.store.save(**fields)

    def test_duplicate_notes_collapse_within_scope_without_cross_kind_or_branch_collision(self):
        first = self._save(
            "Cache invalidation uses ETags.",
            created_at="2026-01-10T10:00:00+00:00",
        )
        variants = (
            "  cache   invalidation uses etags. ",
            "Ｃache invalidation uses ETags.",
            "CACHE INVALIDATION USES ETAGS.",
        )
        for index, variant in enumerate(variants, start=1):
            result = self._save(
                variant,
                created_at=f"2026-01-10T10:0{index}:00+00:00",
            )
            self.assertEqual(result["status"], "duplicate")
            self.assertEqual(result["id"], first["id"])

        # Same normalized text in a different kind or branch stays distinct.
        decision = self._save(
            "Cache invalidation uses ETags.",
            kind="decision",
            created_at="2026-01-10T10:05:00+00:00",
        )
        other_branch = self._save(
            "Cache invalidation uses ETags.",
            branch="feature/cache",
            created_at="2026-01-10T10:06:00+00:00",
        )
        self.assertEqual(decision["status"], "saved")
        self.assertEqual(other_branch["status"], "saved")
        self.assertNotEqual(decision["id"], first["id"])
        self.assertNotEqual(other_branch["id"], first["id"])

        notes = self.store.search(*self.scope, "etags", kind="note")
        self.assertEqual([row["id"] for row in notes], [first["id"]])
        self.assertEqual(notes[0]["text"], "Cache invalidation uses ETags.")

        all_in_scope = self.store.search(*self.scope, "etags")
        self.assertEqual({row["id"] for row in all_in_scope}, {first["id"], decision["id"]})

    def test_branch_and_scope_isolation_prevents_cross_scope_leakage(self):
        fixtures = [
            ("alpha-app", "TASK-101", "main", "scheduler backoff uses jitter on main"),
            ("alpha-app", "TASK-101", "feature/retry", "scheduler backoff uses linear delay"),
            ("alpha-app", "TASK-101", "release/v2", "scheduler backoff frozen for release"),
            ("alpha-app", "TASK-202", "main", "scheduler backoff for secondary task"),
            ("beta-app", "TASK-101", "main", "scheduler backoff for beta project"),
        ]
        saved = {}
        for index, (project, task, branch, text) in enumerate(fixtures):
            key = (project, task, branch)
            res = self.store.save(
                project=project,
                task=task,
                branch=branch,
                kind="note",
                text=text,
                created_at=f"2026-01-11T09:0{index}:00+00:00",
            )
            saved[key] = res["id"]
            self.store.save(
                project=project,
                task=task,
                branch=branch,
                kind="handoff",
                text=f"handoff checkpoint for {project}/{task}/{branch}",
                created_at=f"2026-01-11T10:0{index}:00+00:00",
            )

        for project, task, branch, expected_text in fixtures:
            key = (project, task, branch)
            results = self.store.search(project, task, branch, "scheduler")
            self.assertEqual([row["id"] for row in results], [saved[key]])
            self.assertEqual(results[0]["text"], expected_text)

            ctx = self.store.context(project, task, branch, "scheduler", budget=1200)
            ctx_texts = [item["text"] for item in ctx["items"]]
            self.assertEqual(ctx_texts[0], f"handoff checkpoint for {project}/{task}/{branch}")
            self.assertIn(expected_text, ctx_texts)
            for other_project, other_task, other_branch, other_text in fixtures:
                if (other_project, other_task, other_branch) != key:
                    self.assertNotIn(other_text, ctx_texts)
                    self.assertIsNone(
                        self.store.get(project, task, branch, saved[(other_project, other_task, other_branch)])
                    )

    def test_bounded_retrieval_respects_limits_and_byte_budgets(self):
        # Insert deterministic notes with increasing timestamps.
        for index in range(15):
            self._save(
                f"Worker queue item {index:02d} handles synthetic batch payload.",
                created_at=f"2026-01-12T12:{index:02d}:00+00:00",
            )
        # Add an oversized handoff to verify truncation and budget sharing.
        self._save(
            "Latest handoff summary: " + ("synthetic-checkpoint-detail " * 40),
            kind="handoff",
            created_at="2026-01-12T13:00:00+00:00",
        )

        # Search limit bounds result count deterministically.
        for limit in (1, 3, 5, 10):
            rows = self.store.search(*self.scope, "", limit=limit, kind="note")
            self.assertEqual(len(rows), limit)
            expected_indices = list(range(14, 14 - limit, -1))
            self.assertEqual(
                [row["text"] for row in rows],
                [f"Worker queue item {i:02d} handles synthetic batch payload." for i in expected_indices],
            )

        # Context retrieval stays strictly within every tested byte budget.
        for budget in (64, 160, 320, 640, 1024):
            ctx = self.store.context(*self.scope, "synthetic", budget=budget)
            encoded = json_bytes(ctx["items"])
            self.assertEqual(ctx["budget"], budget)
            self.assertEqual(ctx["context_bytes"], len(encoded))
            self.assertLessEqual(ctx["context_bytes"], budget)
            if budget >= 160:
                self.assertGreaterEqual(len(ctx["items"]), 2)
                self.assertEqual(ctx["items"][0]["kind"], "handoff")
                self.assertTrue(ctx["items"][0]["text"].endswith("…"))
                # Oversized handoff must not consume more than half the budget.
                self.assertLessEqual(len(json_bytes(ctx["items"][0])), max(64, budget // 2))

        # Invalid bounds raise ValueError.
        for bad_limit in (0, -1, 101):
            with self.assertRaises(ValueError):
                self.store.search(*self.scope, "synthetic", limit=bad_limit)
        for bad_budget in (63, 0, 32001):
            with self.assertRaises(ValueError):
                self.store.context(*self.scope, "synthetic", budget=bad_budget)


if __name__ == "__main__":
    unittest.main()
