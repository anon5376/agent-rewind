"""Explicit tests, diffs against the checkpoint, and opt-in patch export."""

import datetime
import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .storage import (RewindError, UNSAFE_REASONS, fingerprint, guarded, inventory,
                      load_checkpoint, load_run, read_json, scan, text_reason,
                      write_json, write_tree)

MAX_LOG = 65536


def process_env():
    # Do not propagate ambient credentials or user Git configuration.
    return {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}


def tree_state(work):
    files, skipped = scan(work)
    # Include exclusions: adding a symlink/secret must invalidate old test evidence.
    import hashlib
    import json
    tree = hashlib.sha256((fingerprint(files) + json.dumps(unsafe(skipped), sort_keys=True)).encode()).hexdigest()
    return files, skipped, tree


def unsafe(skipped):
    return {name: reason for name, reason in skipped.items() if reason in UNSAFE_REASONS}


def safe_log(handle):
    handle.seek(0)
    raw = handle.read(MAX_LOG + 1)
    truncated = len(raw) > MAX_LOG
    raw = raw[:MAX_LOG]
    if text_reason(raw) == "secret-content":
        return "[redacted: recognized secret content]", truncated
    return raw.decode("utf-8", errors="replace"), truncated


def run_test(store, name, command, timeout=30):
    if (not command or not all(isinstance(arg, str) for arg in command)
            or not math.isfinite(timeout) or not 0 < timeout <= 600):
        raise RewindError("supply an explicit command and a timeout between 0 and 600 seconds")
    work, _, _ = load_run(store, name)
    _, _, before = tree_state(work)
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    clock = time.monotonic()
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            proc = subprocess.Popen(command, cwd=work, env=process_env(), stdin=subprocess.DEVNULL,
                                    stdout=stdout, stderr=stderr, start_new_session=(os.name == "posix"))
        except OSError as exc:
            raise RewindError("explicit test executable could not be started") from exc
        status = "pass"
        try:
            proc.wait(timeout=timeout)
            if proc.returncode != 0:
                status = "fail"
        except subprocess.TimeoutExpired:
            status = "timeout"
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            proc.wait()
        out, out_truncated = safe_log(stdout)
        err, err_truncated = safe_log(stderr)
    _, _, after = tree_state(work)
    result = {"status": status, "exit_code": proc.returncode,
              "elapsed_seconds": round(time.monotonic() - clock, 4), "started_at": started,
              "command": command, "tree_before": before, "tree_after": after,
              "stdout": out, "stderr": err, "logs_truncated": out_truncated or err_truncated}
    write_json(guarded(work.parent, "test.json"), result)
    return result


def git_patch(base, candidate):
    """Generate applicable patches without committing or using source Git state."""
    with tempfile.TemporaryDirectory(prefix="rewind-diff-") as directory:
        root = Path(directory)

        def git(*args):
            result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull,
                "-c", "core.autocrlf=false", "-c", "core.filemode=true", *args],
                cwd=root, env=process_env(), capture_output=True, timeout=30)
            if result.returncode:
                raise RewindError("Git patch generation failed; Git must be installed")
            return result.stdout

        git("init", "--quiet", "--template=", ".")
        write_tree(root, base)
        git("add", "-f", "--", ".")
        for path in root.iterdir():
            if path.name == ".git":
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        write_tree(root, candidate)
        added = sorted(set(candidate) - set(base))
        if added:
            git("add", "--intent-to-add", "-f", "--", *added)
        return git("diff", "--binary", "--no-ext-diff", "--no-textconv", "--").decode("utf-8")


def test_evidence(work, tree):
    path = guarded(work.parent, "test.json")
    if not path.exists():
        return {"status": "not-run"}
    result = read_json(path)
    if result.get("status") not in {"pass", "fail", "timeout"}:
        raise RewindError("invalid test evidence")
    if result.get("tree_before") != tree or result.get("tree_after") != tree:
        return {**result, "recorded_status": result["status"], "status": "stale"}
    return result


def source_state(base):
    """Compare retained source files; never follow a replaced source root link."""
    result = {"checkpoint_tree": base["fingerprint"], "current_tree": None, "status": "unavailable"}
    try:
        files, _ = scan(Path(base["source"]))
    except (OSError, RewindError):
        return result
    current = fingerprint(files)
    return {**result, "current_tree": current,
            "status": "unchanged" if current == base["fingerprint"] else "changed"}


def describe_run(store, name):
    work, meta, base = load_run(store, name)
    _, original = load_checkpoint(store, meta["checkpoint"])
    files, skipped, tree = tree_state(work)
    old, new = inventory(original), inventory(files)
    changed = {path: ("added" if path not in old else "deleted" if path not in new else "modified")
               for path in sorted(set(old) | set(new)) if old.get(path) != new.get(path)}
    model_path = guarded(work.parent, "model.json")
    model = read_json(model_path) if model_path.exists() else {"status": "not-run"}
    if model.get("status") == "completed" and model.get("tree_after") != tree:
        model = {**model, "status": "stale"}
    blocked = unsafe(skipped)
    source = source_state(base)
    return {"run": name, "checkpoint": base["id"], "tree": tree,
            "source": source,
            "changes": changed, "diff": git_patch(original, files), "skipped": skipped,
            "exportable": not blocked and source["status"] == "unchanged", "blocked_files": blocked,
            "tests": test_evidence(work, tree), "model": model}


def compare(store, a, b):
    left, right = describe_run(store, a), describe_run(store, b)
    if left["checkpoint"] != right["checkpoint"]:
        raise RewindError("compare requires the same checkpoint")
    return {"checkpoint": left["checkpoint"], "runs": [left, right]}


def export_patch(store, name, output, allow_source_drift=False):
    work, _, base = load_run(store, name)
    output = Path(output).absolute()
    sidecar = output.with_name(output.name + ".json")
    source = Path(base["source"]).resolve()
    store_path = Path(store).resolve()
    for path in (output, sidecar):
        resolved = path.resolve()
        if resolved.is_relative_to(source) or resolved.is_relative_to(store_path):
            raise RewindError("export must be outside the original project and store")
        if path.exists() or path.is_symlink():
            raise RewindError("export output already exists")
    result = describe_run(store, name)
    if result["blocked_files"]:
        raise RewindError("unsafe candidate files; inspect compare exclusions before exporting")
    if result["source"]["status"] != "unchanged" and not allow_source_drift:
        raise RewindError("original source has changed or is unavailable; create a fresh checkpoint or explicitly allow source drift")
    receipt = {key: value for key, value in result.items() if key != "diff"}
    receipt["source_drift_allowed"] = allow_source_drift and result["source"]["status"] != "unchanged"
    receipt["exportable"] = True
    receipt["patch"] = str(output)
    import hashlib
    import json
    receipt["patch_sha256"] = hashlib.sha256(result["diff"].encode()).hexdigest()
    # Exclusive creation closes the overwrite race; remove only our own files on failure.
    created = []
    try:
        for path, data in ((output, result["diff"]), (sidecar, json.dumps(receipt, indent=2) + "\n")):
            with path.open("x", encoding="utf-8", newline="") as handle:
                created.append(path)
                handle.write(data)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return receipt
