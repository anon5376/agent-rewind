import contextlib
import io
import json
import tempfile
import termios
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_rewind import anthropic_trial as trial

# An inert test marker, never an account credential or a network-capable key.
DUMMY = "sk-ant-inert-test-marker-not-a-real-api-key"


def response(content=None):
    proposal = {"writes": [{"path": "slugify.py", "content":
                           'def slugify(text):\n    return "-".join(text.strip().lower().split())\n'}], "deletes": []}
    return {"type": "message", "model": trial.MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": json.dumps(proposal) if content is None else content}],
            "usage": {"input_tokens": 200, "output_tokens": 60}}


class AnthropicTrialTests(unittest.TestCase):
    def connection(self, value=None, status=200):
        conn = Mock()
        conn.getresponse.return_value.status = status
        conn.getresponse.return_value.read.return_value = json.dumps(response() if value is None else value).encode()
        return conn

    def test_fixed_https_messages_contract_and_two_attempt_limit(self):
        conn = self.connection()
        with patch.object(trial.http.client, "HTTPSConnection", return_value=conn) as https:
            client = trial.AnthropicTrial(DUMMY)
            client.generate("a")
            client.generate("b")
            with self.assertRaises(trial.TrialError):
                client.generate("a")
            self.assertEqual(https.call_count, 2)
            self.assertEqual(https.call_args.args[0], "api.anthropic.com")
            self.assertIsNone(https.call_args.kwargs["context"].keylog_filename)
            for call in conn.request.call_args_list:
                self.assertEqual(call.args[:2], ("POST", "/v1/messages"))
                headers = call.kwargs["headers"]
                body = call.kwargs["body"]
                self.assertEqual(headers["x-api-key"], DUMMY)
                self.assertEqual(headers["anthropic-version"], "2023-06-01")
                self.assertNotIn(DUMMY, body.decode())
                payload = json.loads(body)
                self.assertEqual(payload["model"], trial.MODEL)
                self.assertEqual(payload["max_tokens"], 1024)
                self.assertEqual(payload["thinking"], {"type": "disabled"})
                self.assertEqual(payload["output_config"]["effort"], "low")
                self.assertNotIn("tools", payload)
                self.assertNotIn("temperature", payload)
            conn.set_debuglevel.assert_called_with(0)
            client.close()
            self.assertNotIn(DUMMY, repr(client))
            with self.assertRaises(trial.TrialError):
                client.generate("a")

    def test_provider_failure_or_redirect_is_not_retried_or_echoed(self):
        for status in (401, 429, 302, 500):
            with self.subTest(status=status):
                conn = self.connection({"error": DUMMY}, status)
                with patch.object(trial.http.client, "HTTPSConnection", return_value=conn) as https:
                    with trial.AnthropicTrial(DUMMY) as client:
                        with self.assertRaises(trial.TrialError) as caught:
                            client.generate("a")
                        self.assertNotIn(DUMMY, str(caught.exception))
                        self.assertEqual(client.attempts, 1)
                        self.assertEqual(https.call_count, 1)
                        conn.getresponse.return_value.read.assert_not_called()

    def test_key_echo_malformed_tool_or_truncated_response_is_rejected(self):
        bad = [response(DUMMY), {"unexpected": DUMMY},
               {**response(), "stop_reason": "max_tokens"},
               {**response(), "content": [{"type": "tool_use", "name": "shell", "input": {"command": "touch BAD"}}]},
               {**response(), "usage": {"input_tokens": 200, "output_tokens": 1025}}]
        for value in bad:
            with self.subTest(fields=list(value)):
                with patch.object(trial.http.client, "HTTPSConnection", return_value=self.connection(value)):
                    with trial.AnthropicTrial(DUMMY) as client:
                        with self.assertRaises(trial.TrialError) as caught:
                            client.generate("a")
                        self.assertNotIn(DUMMY, str(caught.exception))

    def test_only_synthetic_files_are_sent_and_key_is_absent_from_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / "PRIVATE.txt"
            private.write_text("PRIVATE_DATA_MUST_NOT_BE_SENT")
            conn = self.connection()
            with patch.object(trial.http.client, "HTTPSConnection", return_value=conn):
                with trial.AnthropicTrial(DUMMY) as client:
                    result = trial.run_synthetic_trial(client, root / "demo")
            self.assertEqual(result["calls_attempted"], 2)
            self.assertTrue(result["source_unchanged"])
            self.assertEqual(result["tests"], "not-run; generated code was not executed")
            for call in conn.request.call_args_list:
                body = call.kwargs["body"].decode()
                self.assertNotIn("PRIVATE_DATA_MUST_NOT_BE_SENT", body)
                self.assertNotIn(str(private), body)
            for file in (root / "demo").rglob("*"):
                if file.is_file():
                    self.assertNotIn(DUMMY.encode(), file.read_bytes(), str(file))

    def test_launcher_refuses_noninteractive_agent_terminal_before_key_or_network(self):
        output, error = io.StringIO(), io.StringIO()
        with patch.object(trial.sys.stdin, "isatty", return_value=False), \
             patch.object(trial.getpass, "getpass") as secret, \
             patch.object(trial.http.client, "HTTPSConnection") as network, \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(trial.main([]), 2)
        secret.assert_not_called()
        network.assert_not_called()
        self.assertNotIn("Traceback", output.getvalue() + error.getvalue())

    def test_launcher_failure_never_prints_exception_key_or_traceback(self):
        output, error = io.StringIO(), io.StringIO()
        with patch.object(trial, "require_user_terminal"), \
             patch.object(trial, "read_key", return_value=DUMMY), \
             patch("builtins.input", return_value="PROMO"), \
             patch.object(trial, "run_synthetic_trial", side_effect=RuntimeError(DUMMY)), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(trial.main([]), 2)
        shown = output.getvalue() + error.getvalue()
        self.assertNotIn(DUMMY, shown)
        self.assertNotIn("Traceback", shown)

    def test_empty_key_cancels_before_network(self):
        with patch.object(trial, "require_user_terminal"), \
             patch.object(trial.getpass, "getpass", return_value=""), \
             patch.object(trial.http.client, "HTTPSConnection") as network, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(trial.main([]), 2)
        network.assert_not_called()

    def test_ready_marker_requires_printed_prompt_and_disabled_terminal_echo(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ready.json"
            stream = trial.PromptStream(io.StringIO(), marker)
            with patch.object(trial.termios, "tcgetattr", return_value=[0, 0, 0, termios.ECHO, 0, 0, []]):
                stream.write(trial.KEY_PROMPT)
                stream.flush()
            self.assertFalse(marker.exists())
            with patch.object(trial.termios, "tcgetattr", return_value=[0, 0, 0, 0, 0, 0, []]):
                stream.flush()
            ready = json.loads(marker.read_text())
            self.assertEqual(ready["state"], "awaiting_hidden_key")
            self.assertEqual(ready["prompt"], trial.KEY_PROMPT)
            self.assertEqual(set(ready), {"state", "prompt", "pid"})

    def test_transport_exception_never_contains_header_key_or_retries(self):
        conn = self.connection()
        conn.request.side_effect = RuntimeError(DUMMY)
        with patch.object(trial.http.client, "HTTPSConnection", return_value=conn) as https:
            with trial.AnthropicTrial(DUMMY) as client:
                with self.assertRaises(trial.TrialError) as caught:
                    client.generate("a")
                self.assertNotIn(DUMMY, str(caught.exception))
                self.assertEqual(https.call_count, 1)


if __name__ == "__main__":
    unittest.main()
