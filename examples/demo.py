"""A reproducible file workflow. Default continuations are manual fixtures."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INITIAL = 'def slugify(text):\n    return text.strip().lower().replace(" ", "-")\n'
SHORTCUT = 'def slugify(text):\n    return text.lower().replace(" ", "-")\n'
FIX = 'def slugify(text):\n    return "-".join(text.strip().lower().split())\n'
TESTS = '''import unittest
from slugify import slugify

class SlugTests(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(slugify("Hello World"), "hello-world")
    def test_multiple_spaces(self):
        self.assertEqual(slugify("  Hello   World  "), "hello-world")
    def test_tabs_and_newlines(self):
        self.assertEqual(slugify("Hello\\tWorld\\nAgain"), "hello-world-again")
    def test_empty(self):
        self.assertEqual(slugify(""), "")
'''


def cli(*args, expected=None):
    proc = subprocess.run([sys.executable, "-m", "agent_rewind", *map(str, args)],
                          cwd=REPO, capture_output=True, text=True,
                          env={"PATH": os.environ.get("PATH", os.defpath), "PYTHONDONTWRITEBYTECODE": "1"})
    if expected is not None and proc.returncode not in expected:
        raise RuntimeError(proc.stderr or proc.stdout)
    if proc.returncode == 2:
        raise RuntimeError(proc.stderr)
    return json.loads(proc.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="fresh directory for source, state and evidence")
    parser.add_argument("--select", choices=["a", "b"], help="explicitly export this run, even if its tests fail")
    parser.add_argument("--endpoint", help="existing numeric loopback /v1 endpoint; switches to model mode")
    parser.add_argument("--model", help="already-listed local model; verify its provenance/license separately")
    args = parser.parse_args()
    if bool(args.endpoint) != bool(args.model):
        parser.error("provide --endpoint and --model together")
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source, store = root / "source", root / "state"
    source.mkdir()
    (source / "slugify.py").write_text(INITIAL)
    (source / "test_slugify.py").write_text(TESTS)
    task = root / "task.txt"
    task.write_text("Fix slugify: strip boundary whitespace, lowercase, collapse every whitespace run into one hyphen. "
                    "Keep punctuation unchanged and empty input empty. Preserve the tests.")
    snapshot = cli("checkpoint", source, "--context", task, "--store", store, expected={0})
    runs = {name: cli("fork", store, snapshot["id"], "--name", name, expected={0}) for name in ("a", "b")}
    if args.endpoint:
        for name, instruction in (("a", "Use a readable regular expression for whitespace."),
                                  ("b", "Use Python string operations for whitespace.")):
            cli("continue", store, name, "--endpoint", args.endpoint, "--model", args.model,
                "--files", "slugify.py", "--instruction", instruction, expected={0})
        mode = "endpoint-response; model provenance requires operator verification"
    else:
        for name, content in (("a", SHORTCUT), ("b", FIX)):
            (Path(runs[name]["work"]) / "slugify.py").write_text(content)
        mode = "manual-fixture; no AI generation"
    for name in runs:
        cli("test", store, name, "--timeout", "10", "--", sys.executable,
            "-m", "unittest", "discover", "-s", ".", "-v", expected={0, 1})
    comparison = cli("compare", store, "a", "b", expected={0})
    (root / "comparison.json").write_text(json.dumps(comparison, indent=2) + "\n")
    exported = None
    if args.select:
        exported = cli("export", store, args.select, "--output", root / "selected.patch", expected={0})
    source_unchanged = (source / "slugify.py").read_text() == INITIAL and (source / "test_slugify.py").read_text() == TESTS
    summary = {"mode": mode, "source_unchanged": source_unchanged,
               "results": {item["run"]: {"tests": item["tests"]["status"], "exit_code": item["tests"].get("exit_code"),
                            "elapsed_seconds": item["tests"].get("elapsed_seconds"), "model": item["model"]}
                           for item in comparison["runs"]},
               "selected": args.select, "patch": exported["patch"] if exported else None,
               "comparison": str(root / "comparison.json")}
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0 if source_unchanged else 1


if __name__ == "__main__":
    sys.exit(main())
