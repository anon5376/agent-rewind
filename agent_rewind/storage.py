"""Bounded plaintext snapshots and independent file copies."""

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath

MAX_FILE = 1024 * 1024
MAX_TOTAL = 20 * MAX_FILE
MAX_FILES = 10000
MAX_CONTEXT = 65536
IGNORED_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv",
                "venv", "dist", "build", "coverage", ".rewind", ".pytest_cache",
                ".mypy_cache", ".ruff_cache", ".codex", ".agents"}
SECRET_DIRS = {".aws", ".ssh", ".gnupg", ".kube", ".docker", ".azure", ".config",
               ".qagent", "secrets", "credentials"}
SECRET_FILES = {".netrc", ".npmrc", ".pypirc", ".git-credentials", ".envrc",
                "auth.json", "oauth.json", "kubeconfig", "service-account.json"}
SECRET_NAME = re.compile(
    r"^(?:\.env(?:\..*)?|.*(?:credentials?|secrets?|tokens?)(?:[._-].*)?|"
    r"id_(?:rsa|dsa|ecdsa|ed25519)(?:\..*)?|.*\.(?:pem|key|p12|pfx|keystore))$", re.I)
SECRET_FIELD = (r"(?:[A-Z0-9]+[_-])*(?:api[_-]?key|password|passwd|secret|"
                r"(?:secret[_-])?access[_-]?key|(?:access|auth|refresh)[_-]?token)")
SECRET_CONTENT = re.compile(
    r"-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----|"
    r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b|"
    r"(?i:[\"']" + SECRET_FIELD + r"[\"']\s*:\s*[\"'][^\"'\n]+[\"'])|"
    r"(?im:^\s*(?:export\s+)?" + SECRET_FIELD + r"\s*[:=]\s*[^\s]+)|"
    r"(?i:\bauthorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+\S+)")
UNSAFE_REASONS = {"secret-name", "secret-content", "secret-directory", "symlink",
                  "too-large", "binary", "unsafe-path", "special-file"}


class RewindError(Exception):
    """A user-facing error without file contents or remote response bodies."""


def safe_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", value):
        raise RewindError("invalid checkpoint/run name")
    return value


def safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 for c in value):
        raise RewindError("unsafe relative file path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p.lower() in {".", "..", ".git"} for p in value.split("/")):
        raise RewindError("unsafe relative file path")
    if path.as_posix() != value or value.startswith("-"):
        raise RewindError("unsafe relative file path")
    return path


def guarded(root, *parts):
    """Reject linked state components; this is not a hostile-process boundary."""
    root = Path(root)
    target = root.joinpath(*parts)
    current = target
    while current != root.parent:
        if current.is_symlink():
            raise RewindError("symlink in state path")
        if current == root:
            break
        current = current.parent
    return target


def secret_reason(path):
    if any(p.lower() in SECRET_DIRS for p in path.parts):
        return "secret-directory"
    if path.name.lower() in SECRET_FILES or SECRET_NAME.fullmatch(path.name):
        return "secret-name"
    return None


def text_reason(data):
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    if "\x00" in value:
        return "binary"
    if SECRET_CONTENT.search(value):
        return "secret-content"
    return None


def scan(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise RewindError("project/work path must be a regular directory")
    files, skipped, total = {}, {}, 0
    for directory, directories, names in os.walk(root, followlinks=False):
        for name in sorted(directories[:]):
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            reason = secret_reason(PurePosixPath(rel))
            if path.is_symlink():
                reason = "symlink"
            elif name.lower() in IGNORED_DIRS:
                reason = "service-directory"
            if reason:
                directories.remove(name)
                skipped[rel] = reason
        for name in sorted(names):
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            try:
                safe_relative(rel)
            except RewindError:
                skipped[rel] = "unsafe-path"
                continue
            reason = secret_reason(PurePosixPath(rel))
            if reason:
                skipped[rel] = reason
                continue  # Do not open named credentials.
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                skipped[rel] = "symlink"
                continue
            if not stat.S_ISREG(info.st_mode):
                skipped[rel] = "special-file"
                continue
            if name == ".DS_Store" or name.endswith((".pyc", ".pyo", ".log", ".sqlite", ".db")):
                skipped[rel] = "service-file"
                continue
            if info.st_size > MAX_FILE:
                skipped[rel] = "too-large"
                continue
            with path.open("rb") as handle:
                data = handle.read(MAX_FILE + 1)
            reason = "too-large" if len(data) > MAX_FILE else text_reason(data)
            if reason:
                skipped[rel] = reason
                continue
            total += len(data)
            if total > MAX_TOTAL or len(files) >= MAX_FILES:
                raise RewindError("snapshot exceeds 20 MiB or 10,000 files")
            files[rel] = (data, 0o755 if info.st_mode & 0o111 else 0o644)
    return files, skipped


def inventory(files):
    return {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": mode}
            for name, (data, mode) in sorted(files.items())}


def fingerprint(files):
    return hashlib.sha256(json.dumps(inventory(files), sort_keys=True).encode()).hexdigest()


def write_tree(root, files):
    for name, (data, mode) in files.items():
        target = root / Path(safe_relative(name))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)


def write_json(path, value):
    if path.is_symlink():
        raise RewindError("symlink in state path")
    tmp = path.with_name(".tmp-" + uuid.uuid4().hex)
    try:
        with tmp.open("x", encoding="utf-8") as handle:
            os.chmod(tmp, 0o600)
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * MAX_FILE:
        raise RewindError("missing, linked, or oversized state metadata")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise RewindError("invalid state metadata") from exc
    if not isinstance(value, dict):
        raise RewindError("invalid state metadata")
    return value


def validate_store(store):
    marker = read_json(guarded(store, "store.json"))
    if marker != {"application": "agent-rewind", "version": 1}:
        raise RewindError("invalid Agent Rewind store marker")


def initialize_store(store):
    if store.exists():
        if not store.is_dir():
            raise RewindError("store must be a directory")
        marker = guarded(store, "store.json")
        if marker.exists():
            validate_store(store)
            return
        if any(store.iterdir()):
            raise RewindError("store must be new/empty or an existing Agent Rewind store")
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    store.chmod(0o700)
    write_json(store / "store.json", {"application": "agent-rewind", "version": 1})


def checkpoint(project, context_file, store):
    project, context_file, store = Path(project).resolve(), Path(context_file), Path(store).absolute()
    if store.is_symlink():
        raise RewindError("symlink in state path")
    store = store.resolve()
    if project.is_relative_to(store) or store.is_relative_to(project):
        raise RewindError("project and store must be outside one another")
    if (context_file.is_symlink() or not context_file.is_file()
            or secret_reason(PurePosixPath(context_file.as_posix()))):
        raise RewindError("context must not be a secret or symlink file")
    with context_file.open("rb") as handle:
        context = handle.read(MAX_CONTEXT + 1)
    if len(context) > MAX_CONTEXT or text_reason(context):
        raise RewindError("context is too large or contains binary/recognized secret content")
    files, skipped = scan(project)
    initialize_store(store)
    ident = uuid.uuid4().hex[:12]
    parent = guarded(store, "checkpoints")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = parent / (".tmp-" + ident)
    staging.mkdir(mode=0o700)
    try:
        (staging / "files").mkdir()
        write_tree(staging / "files", files)
        manifest = {"version": 1, "id": ident, "source": str(project),
                    "context": context.decode("utf-8"), "context_sha256": hashlib.sha256(context).hexdigest(),
                    "files": inventory(files), "fingerprint": fingerprint(files), "skipped": skipped}
        write_json(staging / "manifest.json", manifest)
        staging.rename(parent / ident)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"id": ident, "file_count": len(files), "skipped": skipped}


def load_checkpoint(store, ident):
    validate_store(Path(store).absolute())
    directory = guarded(Path(store).absolute(), "checkpoints", safe_name(ident))
    manifest = read_json(guarded(directory, "manifest.json"))
    if manifest.get("version") != 1 or manifest.get("id") != ident:
        raise RewindError("invalid checkpoint metadata")
    expected = manifest.get("files")
    context = manifest.get("context")
    if not isinstance(expected, dict) or not isinstance(context, str) or not isinstance(manifest.get("source"), str):
        raise RewindError("invalid checkpoint metadata")
    for name in expected:
        safe_relative(name)
    files, skipped = scan(guarded(directory, "files"))
    if (skipped or inventory(files) != expected or fingerprint(files) != manifest.get("fingerprint")
            or hashlib.sha256(context.encode()).hexdigest() != manifest.get("context_sha256")
            or len(context.encode()) > MAX_CONTEXT or text_reason(context.encode())):
        raise RewindError("checkpoint integrity check failed")
    return manifest, files


def fork(store, ident, name):
    store = Path(store).absolute()
    safe_name(name)
    base, files = load_checkpoint(store, ident)
    parent = guarded(store, "runs")
    parent.mkdir(mode=0o700, exist_ok=True)
    target = guarded(parent, name)
    if target.exists() or any(entry.name.casefold() == name.casefold() for entry in parent.iterdir()):
        raise RewindError("run already exists; choose a new name")
    staging = parent / (".tmp-" + uuid.uuid4().hex)
    staging.mkdir(mode=0o700)
    try:
        (staging / "work").mkdir()
        write_tree(staging / "work", files)
        write_json(staging / "run.json", {"version": 1, "name": name, "checkpoint": ident})
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"name": name, "checkpoint": base["id"], "work": str(target / "work")}


def load_run(store, name):
    directory = guarded(Path(store).absolute(), "runs", safe_name(name))
    meta = read_json(guarded(directory, "run.json"))
    if meta.get("version") != 1 or meta.get("name") != name:
        raise RewindError("invalid run metadata")
    base, _ = load_checkpoint(store, meta.get("checkpoint"))
    work = guarded(directory, "work")
    if not work.is_dir():
        raise RewindError("missing run working copy")
    return work, meta, base
