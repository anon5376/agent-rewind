import json
import tempfile
import unittest
from pathlib import Path

from agent_rewind.storage import RewindError, checkpoint, fork, load_run, scan


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.context = self.root / "task.txt"
        self.context.write_text("Fix empty input; keep existing behavior.\n")
        self.store = self.root / "state"
        (self.source / "code.py").write_text("value = 1\n")

    def save(self):
        return checkpoint(self.source, self.context, self.store)

    def test_branches_are_independent_and_preserve_source(self):
        saved = self.save()
        a = fork(self.store, saved["id"], "a")
        b = fork(self.store, saved["id"], "b")
        awork, meta, base = load_run(self.store, "a")
        (awork / "code.py").write_text("value = 2\n")
        self.assertEqual((Path(b["work"]) / "code.py").read_text(), "value = 1\n")
        self.assertEqual((self.source / "code.py").read_text(), "value = 1\n")
        self.assertEqual(base["context"], self.context.read_text())
        self.assertNotEqual(a["work"], b["work"])

    def test_exclusions_apply_without_following_links(self):
        cases = {".env": "SYNTHETIC_SECRET", "credentials.json": "fake",
                 ".netrc": "SYNTHETIC_SECRET", ".npmrc": "SYNTHETIC_SECRET",
                 "suspect.txt": 'api_key = "synthetic-test-marker-only"',
                 "config.json": '{"nested":{"api_key":"synthetic-test-marker-only"}}',
                 "settings.py": 'LOCAL_API_KEY = "synthetic-test-marker-only"',
                 "binary.bin": "\x00", "large.txt": "x" * (1024 * 1024 + 1)}
        for name, content in cases.items():
            (self.source / name).write_text(content)
        for directory in (".GiT", "node_modules", ".aws", "__pycache__"):
            (self.source / directory).mkdir()
            (self.source / directory / "ignored.txt").write_text("ignored")
        (self.source / "link").symlink_to(self.context)
        saved = self.save()
        files, skipped = scan(self.store / "checkpoints" / saved["id"] / "files")
        self.assertEqual(set(files), {"code.py"})
        self.assertEqual(skipped, {})
        self.assertIn("link", saved["skipped"])
        self.assertIn("suspect.txt", saved["skipped"])

    def test_empty_project_and_executable_mode_round_trip(self):
        (self.source / "code.py").unlink()
        saved = self.save()
        fork(self.store, saved["id"], "empty")
        self.assertEqual(list((self.store / "runs/empty/work").iterdir()), [])
        (self.source / "script").write_text("#!/bin/sh\nexit 0\n")
        (self.source / "script").chmod(0o755)
        saved = self.save()
        result = fork(self.store, saved["id"], "executable")
        self.assertTrue((Path(result["work"]) / "script").stat().st_mode & 0o111)

    def test_store_overlap_and_duplicate_branch_are_refused(self):
        with self.assertRaises(RewindError):
            checkpoint(self.source, self.context, self.source / "nested")
        with self.assertRaises(RewindError):
            checkpoint(self.source, self.context, self.root)
        saved = self.save()
        fork(self.store, saved["id"], "a")
        with self.assertRaises(RewindError):
            fork(self.store, saved["id"], "a")
        for name in ("../escape", "a/b", "-flag", "", ".", "..", "/absolute", "back\\slash", "new\nline", "A"):
            with self.subTest(name=name), self.assertRaises(RewindError):
                fork(self.store, saved["id"], name)

    def test_context_secret_or_oversize_is_refused_before_creating_snapshot(self):
        for content in ('password = "synthetic-marker-only"',
                        '{"OPENAI_API_KEY":"synthetic-marker-only"}',
                        'database_password: synthetic-marker-only',
                        'Authorization: Bearer synthetic-marker-only', "x" * 65537):
            with self.subTest(length=len(content)):
                self.context.write_text(content)
                with self.assertRaises(RewindError):
                    self.save()
        self.assertFalse((self.store / "checkpoints").exists())

    def test_snapshot_content_and_manifest_traversal_are_checked(self):
        saved = self.save()
        snap = self.store / "checkpoints" / saved["id"]
        (snap / "files/code.py").write_text("tampered\n")
        with self.assertRaisesRegex(RewindError, "integrity"):
            fork(self.store, saved["id"], "tampered")
        saved = self.save()
        snap = self.store / "checkpoints" / saved["id"]
        manifest = json.loads((snap / "manifest.json").read_text())
        manifest["files"]["../escape"] = manifest["files"].pop("code.py")
        (snap / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(RewindError):
            fork(self.store, saved["id"], "escape")

    def test_linked_state_directory_is_refused(self):
        saved = self.save()
        outside = self.root / "outside"
        outside.mkdir()
        (self.store / "runs").symlink_to(outside)
        with self.assertRaises(RewindError):
            fork(self.store, saved["id"], "escape")
        self.assertEqual(list(outside.iterdir()), [])

    def test_total_size_and_file_count_budgets_fail_without_partial_checkpoint(self):
        for index in range(21):
            (self.source / f"large-{index}.txt").write_bytes(b"x" * (1024 * 1024))
        with self.assertRaisesRegex(RewindError, "20 MiB"):
            self.save()
        self.assertFalse((self.store / "checkpoints").exists())
        for path in self.source.iterdir():
            path.unlink()
        for index in range(10001):
            (self.source / f"small-{index}.txt").touch()
        with self.assertRaisesRegex(RewindError, "10,000"):
            self.save()
        self.assertFalse((self.store / "checkpoints").exists())

    def test_existing_unrelated_store_is_refused_without_changing_it(self):
        self.store.mkdir(mode=0o755)
        marker = self.store / "unrelated.txt"
        marker.write_text("KEEP")
        mode = self.store.stat().st_mode
        with self.assertRaisesRegex(RewindError, "store"):
            self.save()
        self.assertEqual(marker.read_text(), "KEEP")
        self.assertEqual(self.store.stat().st_mode, mode)
        self.assertEqual(list(self.store.iterdir()), [marker])


if __name__ == "__main__":
    unittest.main()
