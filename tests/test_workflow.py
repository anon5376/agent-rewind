import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_rewind.evidence import compare, export_patch, run_test
from agent_rewind.storage import RewindError, checkpoint, fork


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "value.py").write_text("value = 1\n")
        self.context = self.root / "task.txt"
        self.context.write_text("Set value to two.\n")
        self.store = self.root / "state"
        self.saved = checkpoint(self.source, self.context, self.store)
        self.a = Path(fork(self.store, self.saved["id"], "a")["work"])
        self.b = Path(fork(self.store, self.saved["id"], "b")["work"])

    def test_failed_passed_and_stale_test_evidence(self):
        command = [sys.executable, "-c", "from value import value; assert value == 2"]
        failed = run_test(self.store, "a", command, 5)
        self.assertEqual(failed["status"], "fail")
        (self.b / "value.py").write_text("value = 2\n")
        passed = run_test(self.store, "b", command, 5)
        self.assertEqual(passed["status"], "pass")
        result = compare(self.store, "a", "b")
        self.assertEqual(result["runs"][0]["tests"]["status"], "fail")
        self.assertEqual(result["runs"][1]["tests"]["status"], "pass")
        self.assertIn("+value = 2", result["runs"][1]["diff"])
        (self.b / "value.py").write_text("value = 3\n")
        self.assertEqual(compare(self.store, "a", "b")["runs"][1]["tests"]["status"], "stale")
        self.assertEqual((self.source / "value.py").read_text(), "value = 1\n")

    def test_timeout_and_modification_during_test_are_visible(self):
        result = run_test(self.store, "a", [sys.executable, "-c", "import time; time.sleep(10)"], 0.1)
        self.assertEqual(result["status"], "timeout")
        result = run_test(self.store, "b", [sys.executable, "-c",
            "from pathlib import Path; Path('value.py').write_text('value = 7\\n')"], 5)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(compare(self.store, "a", "b")["runs"][1]["tests"]["status"], "stale")

    def test_export_patch_applies_add_delete_mode_spaces_unicode_and_no_newline(self):
        (self.b / "value.py").unlink()
        (self.b / "spaced file ü.py").write_text("print(2)")
        (self.b / "spaced file ü.py").chmod(0o755)
        patch = self.root / "choice.patch"
        result = export_patch(self.store, "b", patch)
        self.assertTrue(patch.exists())
        self.assertEqual(result["tests"]["status"], "not-run")
        review = self.root / "review"
        shutil.copytree(self.source, review)
        applied = subprocess.run(["git", "apply", str(patch)], cwd=review,
            env={"PATH": os.environ.get("PATH", os.defpath), "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": os.devnull}, capture_output=True, text=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertFalse((review / "value.py").exists())
        self.assertEqual((review / "spaced file ü.py").read_bytes(), b"print(2)")
        self.assertTrue((review / "spaced file ü.py").stat().st_mode & 0o111)
        self.assertEqual(json.loads(patch.with_suffix(".patch.json").read_text())["run"], "b")

    def test_unsafe_candidate_cannot_silently_export_filtered_changes(self):
        cases = [("value.py", 'password = "synthetic-test-only"'),
                 ("value.py", '{"OPENAI_API_KEY":"synthetic-test-only"}'),
                 (".env", "synthetic"), ("value.py", "\x00"),
                 ("value.py", "x" * (1024 * 1024 + 1))]
        for name, content in cases:
            with self.subTest(name=name, length=len(content)):
                (self.b / "value.py").write_text("value = 1\n")
                (self.b / ".env").unlink(missing_ok=True)
                (self.b / name).write_text(content)
                with self.assertRaises(RewindError):
                    export_patch(self.store, "b", self.root / "unsafe.patch")
                item = compare(self.store, "a", "b")["runs"][1]
                self.assertFalse(item["exportable"])
                self.assertNotIn("synthetic-test-only", item["diff"])

    def test_export_refuses_source_state_and_existing_output(self):
        for target in (self.source / "overwrite.patch", self.store / "overwrite.patch"):
            with self.subTest(target=target), self.assertRaises(RewindError):
                export_patch(self.store, "b", target)
        output = self.root / "existing.patch"
        output.write_text("KEEP")
        with self.assertRaises(RewindError):
            export_patch(self.store, "b", output)
        self.assertEqual(output.read_text(), "KEEP")

    def test_explicit_command_uses_literal_arguments_and_no_project_command(self):
        (self.a / "package.json").write_text(json.dumps({"scripts": {"test": "touch BAD"}}))
        self.assertEqual(compare(self.store, "a", "b")["runs"][0]["tests"]["status"], "not-run")
        result = run_test(self.store, "a", [sys.executable, "-c", "import sys; print(sys.argv[1])",
                                          "hello; touch BAD"], 5)
        self.assertEqual(result["stdout"].strip(), "hello; touch BAD")
        self.assertFalse((self.a / "BAD").exists())
        with self.assertRaises(RewindError):
            run_test(self.store, "a", [], 5)

    def test_comparison_refuses_different_checkpoints(self):
        other = checkpoint(self.source, self.context, self.store)
        fork(self.store, other["id"], "other")
        with self.assertRaises(RewindError):
            compare(self.store, "a", "other")

    def test_ignored_test_artifacts_do_not_invalidate_code_evidence(self):
        run_test(self.store, "b", [sys.executable, "-c",
            "from pathlib import Path; Path('.pytest_cache').mkdir(); Path('.pytest_cache/result').write_text('data')"], 5)
        self.assertEqual(compare(self.store, "a", "b")["runs"][1]["tests"]["status"], "pass")

    def test_cli_requires_command_separator_and_reports_json(self):
        base = [sys.executable, "-m", "agent_rewind"]
        denied = subprocess.run(base + ["test", str(self.store), "a"], capture_output=True, text=True)
        self.assertEqual(denied.returncode, 2)
        good = subprocess.run(base + ["test", str(self.store), "a", "--timeout", "5", "--",
                                    sys.executable, "-c", "print('explicit')"], capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(json.loads(good.stdout)["status"], "pass")

    def test_source_drift_requires_explicit_opt_in_and_keeps_checkpoint_diff(self):
        (self.b / "value.py").write_text("value = 2\n")
        (self.source / "value.py").write_text("value = 99\n")
        output = self.root / "drift.patch"
        with self.assertRaisesRegex(RewindError, "source.*changed"):
            export_patch(self.store, "b", output)
        self.assertFalse(output.exists())
        self.assertFalse(output.with_name(output.name + ".json").exists())
        compared = compare(self.store, "a", "b")["runs"][1]
        self.assertEqual(compared["source"]["status"], "changed")
        self.assertFalse(compared["exportable"])
        receipt = export_patch(self.store, "b", output, allow_source_drift=True)
        self.assertEqual(receipt["source"]["status"], "changed")
        self.assertTrue(receipt["source_drift_allowed"])
        self.assertIn("-value = 1", output.read_text())
        self.assertNotIn("value = 99", output.read_text())
        self.assertEqual((self.source / "value.py").read_text(), "value = 99\n")

    def test_work_and_export_parent_symlinks_cannot_redirect_writes(self):
        parent_link = self.root / "source-link"
        parent_link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(RewindError):
            export_patch(self.store, "b", parent_link / "BAD.patch")
        self.assertFalse((self.source / "BAD.patch").exists())
        (self.b / "value.py").unlink()
        (self.b / "value.py").symlink_to(self.source / "value.py")
        with self.assertRaises(RewindError):
            export_patch(self.store, "b", self.root / "BAD.patch")
        shutil.rmtree(self.b)
        self.b.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(RewindError):
            run_test(self.store, "b", [sys.executable, "-c",
                "from pathlib import Path; Path('BAD').write_text('executed')"], 5)
        self.assertFalse((self.source / "BAD").exists())
        self.assertEqual((self.source / "value.py").read_text(), "value = 1\n")


if __name__ == "__main__":
    unittest.main()
