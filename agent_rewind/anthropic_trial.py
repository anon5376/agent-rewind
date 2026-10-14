"""User-operated, two-message synthetic trial. Never reads stored credentials."""

import faulthandler
import getpass
import http.client
import json
import os
import re
import resource
import ssl
import sys
import termios
import time
import uuid
import warnings
from pathlib import Path

from .evidence import compare, tree_state
from .model import apply_proposal, parse_proposal
from .storage import checkpoint, fork, load_run, write_json

MODEL = "claude-haiku-5-5"
KEY_PROMPT = "Anthropic API key (hidden; Enter confirms 2 requests): "
MAX_CALLS = 2
MAX_OUTPUT = 1024
MAX_RESPONSE_BYTES = 1024 * 1024
ESTIMATED_INPUT_PER_CALL = 10000  # Deliberately generous for the fixed <8 KiB prompt.
INPUT_USD_PER_MILLION = 0.10
OUTPUT_USD_PER_MILLION = 0.50
ESTIMATE_USD = MAX_CALLS * (ESTIMATED_INPUT_PER_CALL * INPUT_USD_PER_MILLION
                          + MAX_OUTPUT * OUTPUT_USD_PER_MILLION) / 1000000
INITIAL = 'def slugify(text):\n    return text.strip().lower().replace(" ", "-")\n'
TASK = "Fix slugify: lowercase, strip boundary whitespace, collapse every whitespace run into one hyphen. Keep punctuation and empty input unchanged."
TESTS = '''import unittest
from slugify import slugify
class SlugTests(unittest.TestCase):
    def test_simple(self): self.assertEqual(slugify("Hello World"), "hello-world")
    def test_spaces(self): self.assertEqual(slugify("  Hello   World  "), "hello-world")
    def test_tabs(self): self.assertEqual(slugify("Hello\\tWorld\\nAgain"), "hello-world-again")
    def test_empty(self): self.assertEqual(slugify(""), "")
'''
SYSTEM = ('Return only JSON: {"writes":[{"path":"slugify.py","content":"complete Python file"}],'
          '"deletes":[]}. Change only slugify.py. No tools, commands, credentials, or explanations. '
          'The task and code are synthetic demonstration data.')


class TrialError(Exception):
    """Only fixed, nonsensitive error codes cross this boundary."""


SAFE_ERRORS = {"open_your_own_mac_terminal", "secure_input_unavailable", "invalid_key_format",
               "invalid_variant", "fixed_prompt_limit_exceeded", "call_limit_or_closed",
               "provider_rejected_no_retry", "response_too_large", "sensitive_response_refused",
               "incomplete_or_invalid_response", "non_text_response_refused", "invalid_usage_response",
               "unexpected_model_response", "request_failed_no_retry", "output_parent_link_refused",
               "no_arguments_allowed", "cancelled_before_key"}


class PromptStream:
    """Publish only fixed readiness metadata after getpass has disabled echo."""

    def __init__(self, stream, ready_file):
        self.stream, self.ready_file = stream, ready_file
        self.prompt_written = False
        self.ready_written = False

    def write(self, value):
        self.stream.write(value)
        if value == KEY_PROMPT:
            self.prompt_written = True

    def flush(self):
        self.stream.flush()
        if self.ready_file and self.prompt_written and not self.ready_written:
            flags = termios.tcgetattr(sys.stdin.fileno())[3]
            if not flags & termios.ECHO:
                write_json(self.ready_file, {"state": "awaiting_hidden_key", "prompt": KEY_PROMPT, "pid": os.getpid()})
                self.ready_written = True


def require_user_terminal():
    if (not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr))
            or os.environ.get("TERM_PROGRAM") not in {"Apple_Terminal", "iTerm.app"}
            or sys.gettrace() is not None):
        raise TrialError("open_your_own_mac_terminal")
    # A child-process setting: no core dump containing in-memory credentials.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    faulthandler.disable()


def read_key(ready_file=None):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            key = getpass.getpass(KEY_PROMPT, stream=PromptStream(sys.stderr, ready_file)).strip()
        except getpass.GetPassWarning:
            raise TrialError("secure_input_unavailable") from None
    if not key:
        raise TrialError("cancelled_before_key")
    if not re.fullmatch(r"sk-ant-[A-Za-z0-9_-]{15,300}", key):
        raise TrialError("invalid_key_format")
    return key


def payload_for(variant):
    if variant not in {"a", "b"}:
        raise TrialError("invalid_variant")
    user = json.dumps({"task": TASK, "files": {"slugify.py": INITIAL},
        "instruction": "Use a readable regular expression." if variant == "a" else "Use Python string operations.",
        "examples": [["  Hello   World  ", "hello-world"], ["Hello\tWorld\nAgain", "hello-world-again"], ["", ""]]})
    payload = {"model": MODEL, "max_tokens": MAX_OUTPUT, "stream": False,
               "thinking": {"type": "disabled"}, "output_config": {"effort": "low"},
               "system": SYSTEM, "messages": [{"role": "user", "content": user}]}
    body = json.dumps(payload).encode("utf-8")
    if len(body) > 8192:
        raise TrialError("fixed_prompt_limit_exceeded")
    return body


class AnthropicTrial:
    """Fixed host/model/prompt, no retries, no environment key or SDK logging."""

    def __init__(self, key):
        self._key = key
        self.attempts = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self._key = None

    def generate(self, variant):
        body = payload_for(variant)
        if self._key is None or self.attempts >= MAX_CALLS:
            raise TrialError("call_limit_or_closed")
        self.attempts += 1  # A failed/uncertain attempt consumes the limit too.
        conn = None
        try:
            # Construct explicitly: create_default_context can honor SSLKEYLOGFILE.
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            tls.load_default_certs()
            tls.keylog_filename = None
            conn = http.client.HTTPSConnection("api.anthropic.com", timeout=30, context=tls)
            conn.set_debuglevel(0)
            conn.request("POST", "/v1/messages", body=body, headers={
                "x-api-key": self._key, "anthropic-version": "2023-06-01",
                "content-type": "application/json"})
            response = conn.getresponse()
            if response.status != 200:
                raise TrialError("provider_rejected_no_retry")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise TrialError("response_too_large")
            value = json.loads(raw)
            # Never persist even a service response that echoes the supplied key.
            if self._key in json.dumps(value, ensure_ascii=False):
                raise TrialError("sensitive_response_refused")
            if not isinstance(value, dict) or value.get("stop_reason") != "end_turn":
                raise TrialError("incomplete_or_invalid_response")
            blocks = value.get("content")
            if (not isinstance(blocks, list) or not blocks
                    or any(not isinstance(block, dict) or block.get("type") != "text"
                           or not isinstance(block.get("text"), str) for block in blocks)):
                raise TrialError("non_text_response_refused")
            usage = value.get("usage")
            if (not isinstance(usage, dict) or any(type(usage.get(name)) is not int or usage[name] < 0
                   for name in ("input_tokens", "output_tokens"))
                    or usage["input_tokens"] > ESTIMATED_INPUT_PER_CALL or usage["output_tokens"] > MAX_OUTPUT):
                raise TrialError("invalid_usage_response")
            reported = value.get("model")
            if not isinstance(reported, str) or not re.fullmatch(r"claude-haiku-5-5(?:-[0-9]{8})?", reported):
                raise TrialError("unexpected_model_response")
            return {"text": "\n".join(block["text"] for block in blocks), "reported_model": reported,
                    "usage": {name: usage[name] for name in ("input_tokens", "output_tokens")}}
        except TrialError:
            raise
        except Exception:
            # Do not stringify transport/JSON exceptions: they may carry headers.
            raise TrialError("request_failed_no_retry") from None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def run_synthetic_trial(client, root):
    root = Path(root).absolute()
    if root.parent.is_symlink():
        raise TrialError("output_parent_link_refused")
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(mode=0o700, exist_ok=False)
    source, store, context = root / "source", root / "state", root / "task.txt"
    source.mkdir()
    (source / "slugify.py").write_text(INITIAL)
    (source / "test_slugify.py").write_text(TESTS)
    context.write_text(TASK)
    saved = checkpoint(source, context, store)
    receipts = {}
    for name in ("a", "b"):
        fork(store, saved["id"], name)
        work, _, _ = load_run(store, name)
        files, _, before = tree_state(work)
        started = time.monotonic()
        result = client.generate(name)
        candidate, touched = parse_proposal({"choices": [{"message": {"content": result["text"]}}]},
                                           {"slugify.py"}, files, work)
        apply_proposal(work, files, candidate, touched)
        record = {"status": "completed", "evidence_type": "anthropic-api-response; synthetic task",
                  "provider": "anthropic", "requested_model": MODEL, "reported_model": result["reported_model"],
                  "usage": result["usage"], "elapsed_seconds": round(time.monotonic() - started, 4),
                  "max_output_tokens": MAX_OUTPUT, "tree_before": before, "tree_after": tree_state(work)[2]}
        write_json(work.parent / "model.json", record)
        receipts[name] = {"usage": result["usage"], "reported_model": result["reported_model"]}
    comparison = compare(store, "a", "b")
    write_json(root / "comparison.json", comparison)
    input_tokens = sum(record["usage"]["input_tokens"] for record in receipts.values())
    output_tokens = sum(record["usage"]["output_tokens"] for record in receipts.values())
    summary = {"status": "completed", "mode": "anthropic-api; synthetic task; proprietary model",
               "calls_attempted": client.attempts, "max_output_tokens_per_call": MAX_OUTPUT,
               "model": MODEL, "variants": receipts,
               "estimated_usage_cost_usd": round((input_tokens * INPUT_USD_PER_MILLION
                                                  + output_tokens * OUTPUT_USD_PER_MILLION) / 1000000, 6),
               "preflight_estimate_usd": ESTIMATE_USD, "funding": "user-confirmed promo credits; not API verified",
               "tests": "not-run; generated code was not executed", "selected": None,
               "source_unchanged": (source / "slugify.py").read_text() == INITIAL,
               "output": str(root), "comparison": str(root / "comparison.json")}
    write_json(root / "summary.json", summary)
    return summary


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv in (["--help"], ["-h"]):
        print("In your own macOS Terminal: python3 -B -m agent_rewind.anthropic_trial")
        return 0
    client, key, ready_file = None, None, None
    try:
        if len(argv) == 2 and argv[0] == "--ready-file":
            ready_file = Path(argv[1]).absolute()
            if not ready_file.parent.is_dir() or ready_file.exists() or ready_file.is_symlink():
                raise TrialError("no_arguments_allowed")
        elif argv:
            raise TrialError("no_arguments_allowed")
        require_user_terminal()
        print(f"Two synthetic calls to {MODEL}, max {MAX_OUTPUT} output tokens each; no automatic retries.")
        print(f"Conservative estimate ${ESTIMATE_USD:.6f}; reserve $0.01 promo credits. No private files are sent.")
        print("In Claude Console verify PROMOTIONAL credit balance and no card/auto-reload. Funding cannot be verified by this script.")
        print("Pasting a key and pressing Enter confirms promo-only funding and sends the two requests. Empty input cancels.")
        key = read_key(ready_file)
        client = AnthropicTrial(key)
        key = None
        root = Path(__file__).resolve().parents[1] / ".demo" / ("anthropic-" + uuid.uuid4().hex[:10])
        result = run_synthetic_trial(client, root)
        client.close()
        if ready_file:
            write_json(ready_file, {"state": "completed", "pid": os.getpid()})
        print(json.dumps(result, indent=2))
        print("The key is not saved. This process now exits. Review the copies before running tests in a separate command.")
        return 0
    except KeyboardInterrupt:
        if ready_file:
            write_json(ready_file, {"state": "cancelled", "pid": os.getpid()})
        print(json.dumps({"status": "cancelled", "calls_attempted": client.attempts if client else 0}), file=sys.stderr)
        return 130
    except BaseException as exc:
        code = str(exc) if isinstance(exc, TrialError) and str(exc) in SAFE_ERRORS else "trial_stopped_without_diagnostics"
        if ready_file:
            try:
                write_json(ready_file, {"state": "stopped", "error": code, "pid": os.getpid()})
            except Exception:
                pass
        print(json.dumps({"status": "stopped", "error": code,
                          "calls_attempted": client.attempts if client else 0}), file=sys.stderr)
        return 2
    finally:
        key = None
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
