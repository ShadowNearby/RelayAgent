"""Tests for agents.llm_client.HttpChatClient — the OpenAI-SDK-free shim.

Runs a local http.server stub and checks the exact surface our callers
consume: request shape (auth header, extra_body merge), response attribute
access (choices[0].message.content / reasoning_content, usage token fields,
prompt_tokens_details.cached_tokens), error stringification (status + body,
for the max_tokens-retry sniffing in agent_base), and duck-typing through
flow_runner's _RecordingLLM proxy.
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from agents.llm_client import HttpChatClient, LLMHTTPError, make_llm_client

_OK_RESPONSE = {
    "id": "chatcmpl-1",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": "hello",
                "reasoning_content": "thinking...",
            },
        }
    ],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "prompt_tokens_details": {"cached_tokens": 3},
    },
}


class _Stub(BaseHTTPRequestHandler):
    """Captures the last request and serves the configured response."""

    last_request: dict = {}
    response_body: dict = _OK_RESPONSE
    response_status: int = 200

    def do_POST(self):  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length", "0"))
        type(self).last_request = {
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(self.rfile.read(length)),
        }
        payload = json.dumps(type(self).response_body).encode("utf-8")
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence test output
        pass


class HttpChatClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Stub)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Stub.response_body = _OK_RESPONSE
        _Stub.response_status = 200
        _Stub.last_request = {}
        self.client = HttpChatClient(self.base_url, "test-key", timeout=5.0)

    def _create(self, **kwargs):
        kwargs.setdefault("model", "qwen")
        kwargs.setdefault("messages", [{"role": "user", "content": "hi"}])
        return self.client.chat.completions.create(**kwargs)

    # -- request shape -------------------------------------------------------
    def test_request_path_auth_and_body(self):
        self._create(temperature=0.0, max_tokens=64)
        req = _Stub.last_request
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["auth"], "Bearer test-key")
        self.assertEqual(req["body"]["model"], "qwen")
        self.assertEqual(req["body"]["temperature"], 0.0)
        self.assertEqual(req["body"]["max_tokens"], 64)
        self.assertNotIn("stream", req["body"])

    def test_extra_body_merged(self):
        # the kimi quirk in agent_base passes extra_body={"enable_thinking": True}
        self._create(extra_body={"enable_thinking": True})
        self.assertIs(_Stub.last_request["body"]["enable_thinking"], True)

    def test_none_kwargs_dropped(self):
        self._create(temperature=None)
        self.assertNotIn("temperature", _Stub.last_request["body"])

    def test_stream_raises(self):
        with self.assertRaises(NotImplementedError):
            self._create(stream=True)

    # -- response surface ----------------------------------------------------
    def test_message_content_and_reasoning(self):
        resp = self._create()
        msg = resp.choices[0].message
        self.assertEqual(msg.content, "hello")
        self.assertEqual(getattr(msg, "reasoning_content", None), "thinking...")

    def test_usage_fields(self):
        resp = self._create()
        self.assertEqual(resp.usage.prompt_tokens, 10)
        self.assertEqual(resp.usage.completion_tokens, 5)
        self.assertEqual(resp.usage.total_tokens, 15)
        self.assertEqual(resp.usage.prompt_tokens_details.cached_tokens, 3)

    def test_null_content_passthrough(self):
        # qwen thinking models: content null, answer in reasoning_content.
        _Stub.response_body = {
            "choices": [{"message": {"content": None, "reasoning_content": "ans"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        resp = self._create()
        self.assertIsNone(resp.choices[0].message.content)
        self.assertEqual(resp.choices[0].message.reasoning_content, "ans")

    def test_missing_usage_is_none(self):
        _Stub.response_body = {"choices": [{"message": {"content": "x"}}]}
        resp = self._create()
        self.assertIsNone(resp.usage)

    def test_missing_token_fields_default_zero(self):
        _Stub.response_body = {
            "choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 7},
        }
        resp = self._create()
        self.assertEqual(resp.usage.completion_tokens, 0)
        self.assertEqual(resp.usage.total_tokens, 0)

    def test_no_choices_raises(self):
        _Stub.response_body = {"error": "nope"}
        with self.assertRaises(LLMHTTPError):
            self._create()

    # -- errors ----------------------------------------------------------------
    def test_http_error_carries_status_and_body(self):
        _Stub.response_status = 400
        _Stub.response_body = {
            "error": "max_tokens is not supported, use max_completion_tokens"
        }
        with self.assertRaises(LLMHTTPError) as ctx:
            self._create()
        text = str(ctx.exception)
        # agent_base's retry path sniffs for both substrings in str(e).
        self.assertIn("400", text)
        self.assertIn("max_tokens", text)
        self.assertIn("max_completion_tokens", text)

    def test_connection_error_raises(self):
        bad = HttpChatClient("http://127.0.0.1:1/v1", "k", timeout=0.5)
        with self.assertRaises(LLMHTTPError):
            bad.chat.completions.create(
                model="m", messages=[{"role": "user", "content": "x"}]
            )

    # -- integration with flow_runner's recorder -------------------------------
    def test_recording_llm_proxy_compat(self):
        from agents.flow_runner import _RecordingLLM

        rec = _RecordingLLM(self.client, retry=False)
        resp = rec.chat.completions.create(
            model="qwen",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0,
            max_tokens=8,
        )
        self.assertEqual(resp.choices[0].message.content, "hello")
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(rec.calls[0]["response"], "hello")
        self.assertEqual(rec.calls[0]["usage"]["prompt_tokens"], 10)


class MakeLLMClientTests(unittest.TestCase):
    def test_forced_http_shim(self):
        import os

        os.environ["RELAY_LLM_HTTP"] = "1"
        try:
            client = make_llm_client("http://example.invalid/v1", "")
            self.assertIsInstance(client, HttpChatClient)
            self.assertEqual(client.api_key, "empty")
        finally:
            del os.environ["RELAY_LLM_HTTP"]

    def test_default_prefers_sdk_when_available(self):
        try:
            import openai  # noqa: F401
        except ImportError:
            self.skipTest("openai SDK not installed")
        client = make_llm_client("http://example.invalid/v1", "k")
        self.assertNotIsInstance(client, HttpChatClient)


if __name__ == "__main__":
    unittest.main()
