import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from agent_rewind.model import continue_run, models
from agent_rewind.storage import RewindError, checkpoint, fork


class ModelTests(unittest.TestCase):
    """Fake HTTP responses verify the adapter; these are not model runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "code.py").write_text("value = 1\n")
        (self.source / "other.py").write_text("KEEP\n")
        context = self.root / "task.txt"
        context.write_text("Set value to two.")
        self.store = self.root / "state"
        saved = checkpoint(self.source, context, self.store)
        self.work = Path(fork(self.store, saved["id"], "a")["work"])
        self.proposal = {"writes": [{"path": "code.py", "content": "value = 2\n"}], "deletes": []}
        self.raw_response = None
        self.calls = []
        self.redirect = False
        self.advertised_model = "fixture-only"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, value):
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                owner.calls.append(("GET", self.path, None))
                if owner.redirect:
                    self.send_response(302)
                    self.send_header("Location", "/redirect-target")
                    self.end_headers()
                else:
                    self.reply({"data": [{"id": owner.advertised_model}]})

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append(("POST", self.path, data))
                self.reply(owner.raw_response if owner.raw_response is not None else {
                    "model": "fixture-only", "choices": [{"message": {"content": json.dumps(owner.proposal)}}],
                    "usage": {"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54}})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_server)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def continuation(self, files=None):
        return continue_run(self.store, "a", self.endpoint, "fixture-only",
                            ["code.py"] if files is None else files, "Use an assignment.", 5)

    def test_wire_contract_applies_only_allowed_files_and_records_usage(self):
        self.assertEqual(models(self.endpoint), ["fixture-only"])
        result = self.continuation()
        self.assertEqual((self.work / "code.py").read_text(), "value = 2\n")
        self.assertEqual((self.work / "other.py").read_text(), "KEEP\n")
        self.assertEqual((self.source / "code.py").read_text(), "value = 1\n")
        request = [call for call in self.calls if call[0] == "POST"][0]
        self.assertEqual(request[1], "/v1/chat/completions")
        self.assertEqual(request[2]["model"], "fixture-only")
        self.assertFalse(request[2]["stream"])
        prompt = request[2]["messages"][1]["content"]
        self.assertIn("Set value to two.", prompt)
        self.assertIn("value = 1", prompt)
        self.assertNotIn("KEEP", prompt)
        self.assertEqual(result["usage"]["total_tokens"], 54)
        self.assertEqual(result["evidence_type"], "endpoint-response; model provenance unverified")

    def test_unknown_model_is_refused_without_generation(self):
        self.advertised_model = "different"
        with self.assertRaises(RewindError):
            self.continuation()
        self.assertFalse(any(call[0] == "POST" for call in self.calls))
        self.assertEqual((self.work / "code.py").read_text(), "value = 1\n")

    def test_bad_proposals_leave_all_files_unchanged(self):
        proposals = [
            {"writes": [{"path": "../escape", "content": "BAD"}], "deletes": []},
            {"writes": [{"path": str(self.root / "escape"), "content": "BAD"}], "deletes": []},
            {"writes": [{"path": "nested/../../escape", "content": "BAD"}], "deletes": []},
            {"writes": [{"path": "..\\escape", "content": "BAD"}], "deletes": []},
            {"writes": [{"path": "other.py", "content": "BAD"}], "deletes": []},
            {"writes": [{"path": "code.py", "content": 'password = "synthetic-only"'}], "deletes": []},
            {"writes": [{"path": "code.py", "content": "ok"},
                        {"path": "other.py", "content": "BAD"}], "deletes": []},
            {"writes": [{"path": "code.py", "content": "\x00"}], "deletes": []},
            {"writes": [{"path": "code.py", "content": "x" * (1024 * 1024 + 1)}], "deletes": []},
            {"writes": [], "deletes": ["other.py"]},
            {"writes": [], "deletes": [], "command": "touch BAD"},
            {"writes": [{"path": "code.py", "content": "ok"}], "deletes": ["code.py"]},
        ]
        for proposal in proposals:
            with self.subTest(proposal=list(proposal)):
                self.proposal = proposal
                with self.assertRaises(RewindError):
                    self.continuation()
                self.assertEqual((self.work / "code.py").read_text(), "value = 1\n")
                self.assertFalse((self.work / "BAD").exists())
                self.assertFalse((self.root / "escape").exists())

    def test_malformed_response_and_redirect_are_refused(self):
        responses = [{"choices": []}, {"choices": [None]},
                     {"choices": [{"message": {"content": "not JSON"}}]},
                     {"choices": [{"message": {"content": None}}]},
                     {"choices": [{"message": {"content": "[]"}}]}]
        for response in responses:
            with self.subTest(response=response):
                self.raw_response = response
                with self.assertRaises(RewindError):
                    self.continuation()
                self.assertEqual((self.work / "code.py").read_text(), "value = 1\n")
        self.redirect = True
        self.calls.clear()
        with self.assertRaises(RewindError):
            models(self.endpoint)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], "/v1/models")

    def test_remote_or_credential_url_and_symlink_paths_are_refused(self):
        for endpoint in ("https://example.org/v1", "http://localhost.evil/v1",
                         "http://user:pass@127.0.0.1/v1", "http://127.0.0.1/v1?key=x"):
            with self.subTest(endpoint=endpoint), self.assertRaises(RewindError):
                models(endpoint)
        (self.work / "linked").symlink_to(self.source, target_is_directory=True)
        self.proposal = {"writes": [{"path": "linked/code.py", "content": "BAD"}], "deletes": []}
        with self.assertRaises(RewindError):
            self.continuation(["linked/code.py"])
        self.assertEqual((self.source / "code.py").read_text(), "value = 1\n")

    def test_explicit_new_file_and_delete_are_supported(self):
        self.proposal = {"writes": [{"path": "new/name.py", "content": "value = 2\n"}],
                         "deletes": ["code.py"]}
        self.continuation(["new/name.py", "code.py"])
        self.assertFalse((self.work / "code.py").exists())
        self.assertEqual((self.work / "new/name.py").read_text(), "value = 2\n")

    def test_proposed_file_directory_collision_and_invalid_unicode_are_refused(self):
        cases = [(["new", "new/child"], [{"path": "new", "content": "file"},
                                          {"path": "new/child", "content": "child"}]),
                 (["new", "NEW/child"], [{"path": "new", "content": "file"},
                                          {"path": "NEW/child", "content": "child"}]),
                 (["new", "NEW"], [{"path": "new", "content": "file"},
                                    {"path": "NEW", "content": "other"}]),
                 (["code.py"], [{"path": "code.py", "content": "\ud800"}])]
        for paths, writes in cases:
            with self.subTest(paths=paths):
                self.proposal = {"writes": writes, "deletes": []}
                with self.assertRaises(RewindError):
                    self.continuation(paths)
                self.assertFalse((self.work / "new").exists())
                self.assertEqual((self.work / "code.py").read_text(), "value = 1\n")

    def test_model_generated_python_and_tool_calls_are_never_executed(self):
        code = "from pathlib import Path\nPath('BAD_MODEL_EXECUTION').write_text('executed')\n"
        self.proposal = {"writes": [{"path": "code.py", "content": code}], "deletes": []}
        self.raw_response = {"choices": [{"message": {"content": json.dumps(self.proposal),
            "tool_calls": [{"type": "function", "function": {"name": "shell",
                "arguments": json.dumps({"command": "touch BAD_TOOL_EXECUTION"})}}]}}]}
        self.continuation()
        from agent_rewind.evidence import compare
        fork(self.store, json.loads((self.work.parent / "run.json").read_text())["checkpoint"], "b")
        result = compare(self.store, "a", "b")
        self.assertEqual((self.work / "code.py").read_text(), code)
        self.assertFalse((self.work / "BAD_MODEL_EXECUTION").exists())
        self.assertFalse((self.work / "BAD_TOOL_EXECUTION").exists())
        self.assertEqual(result["runs"][0]["tests"]["status"], "not-run")


if __name__ == "__main__":
    unittest.main()
