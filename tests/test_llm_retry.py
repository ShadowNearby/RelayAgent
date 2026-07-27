"""create_with_retry: transient-vs-deterministic error classification and the
no-nested-retry contract with flow_runner's _RecordingLLM proxy."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from agents.llm.llm_client import LLMHTTPError
from agents.llm.llm_retry import _is_retryable, create_with_retry


class _StatusCodeError(Exception):
    """openai-SDK-shaped error: carries `.status_code` (APIStatusError)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"Error code: {status_code}")
        self.status_code = status_code


class _ScriptedClient:
    """Raises the scripted exceptions in order, then returns `response`."""

    def __init__(self, errors: list[Exception], response="ok") -> None:
        self._errors = list(errors)
        self.response = response
        self.n_calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *args, **kwargs):
        self.n_calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self.response


class ClassificationTests(unittest.TestCase):
    def test_deterministic_statuses_not_retryable(self):
        for status in (400, 401, 403, 404, 422):
            self.assertFalse(
                _is_retryable(LLMHTTPError(f"HTTP {status} from http://gw: nope")),
                status,
            )
            self.assertFalse(_is_retryable(_StatusCodeError(status)), status)

    def test_transient_statuses_retryable(self):
        for status in (408, 409, 429, 500, 502, 503):
            self.assertTrue(
                _is_retryable(LLMHTTPError(f"HTTP {status} from http://gw: busy")),
                status,
            )
            self.assertTrue(_is_retryable(_StatusCodeError(status)), status)

    def test_statusless_errors_assumed_transient(self):
        self.assertTrue(
            _is_retryable(LLMHTTPError("chat/completions request failed: timed out"))
        )
        self.assertTrue(_is_retryable(RuntimeError("connection reset")))
        self.assertTrue(_is_retryable(TimeoutError()))

    def test_type_error_is_deterministic(self):
        self.assertFalse(_is_retryable(TypeError("unexpected keyword argument")))


@mock.patch("agents.llm.llm_retry.time.sleep")
class CreateWithRetryTests(unittest.TestCase):
    def test_non_retryable_raises_on_first_attempt(self, _sleep):
        client = _ScriptedClient([LLMHTTPError("HTTP 400 from http://gw: bad")] * 3)
        with self.assertRaises(LLMHTTPError):
            create_with_retry(client, model="m", messages=[])
        self.assertEqual(client.n_calls, 1)
        _sleep.assert_not_called()

    def test_transient_retries_then_succeeds(self, _sleep):
        client = _ScriptedClient([LLMHTTPError("HTTP 500 from http://gw: boom")])
        self.assertEqual(create_with_retry(client, model="m", messages=[]), "ok")
        self.assertEqual(client.n_calls, 2)

    def test_transient_exhausts_attempts(self, _sleep):
        client = _ScriptedClient([LLMHTTPError("HTTP 429 from http://gw: slow")] * 5)
        with self.assertRaises(LLMHTTPError):
            create_with_retry(client, model="m", messages=[])
        self.assertEqual(client.n_calls, 3)  # retry_times default

    def test_internally_retrying_proxy_not_wrapped_again(self, _sleep):
        # A client advertising `_retry` truthy (flow_runner's _RecordingLLM
        # default) already routes its own create through create_with_retry —
        # the outer wrapper must pass straight through, once.
        client = _ScriptedClient([LLMHTTPError("HTTP 500 from http://gw: boom")] * 9)
        client._retry = True
        with self.assertRaises(LLMHTTPError):
            create_with_retry(client, model="m", messages=[])
        self.assertEqual(client.n_calls, 1)

    def test_recording_llm_no_nested_3x3(self, _sleep):
        # End-to-end pin of the recovery-reroute path shape:
        # create_with_retry(_RecordingLLM(retry=True)) must yield exactly
        # retry_times attempts on the real client — not 3x3=9.
        from agents.flow.flow_recording_llm import _RecordingLLM

        inner = _ScriptedClient([LLMHTTPError("HTTP 503 from http://gw: down")] * 99)
        recorder = _RecordingLLM(inner)  # retry=True default
        with self.assertRaises(LLMHTTPError):
            create_with_retry(recorder, model="m", messages=[])
        self.assertEqual(inner.n_calls, 3)

    def test_caller_owned_retry_proxy_still_retried_by_wrapper(self, _sleep):
        # retry=False (planner/router construction) → the recorder does NOT
        # retry internally, so the outer wrapper must keep doing it.
        from agents.flow.flow_recording_llm import _RecordingLLM

        inner = _ScriptedClient([LLMHTTPError("HTTP 503 from http://gw: down")] * 99)
        recorder = _RecordingLLM(inner, retry=False)
        with self.assertRaises(LLMHTTPError):
            create_with_retry(recorder, model="m", messages=[])
        self.assertEqual(inner.n_calls, 3)


if __name__ == "__main__":
    unittest.main()
