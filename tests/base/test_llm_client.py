import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from supysonic.llm_client import (
    LlmConfigError,
    LlmInvalidResponseError,
    LlmTimeoutError,
    LlmUpstreamError,
    build_chat_completion_payload,
    parse_json_object_response,
    post_chat_completion,
)


class LlmClientTestCase(unittest.TestCase):
    def test_payload_preserves_legacy_defaults(self):
        payload = build_chat_completion_payload(
            "test-model", [], max_output_tokens=900, temperature=0.2
        )
        self.assertEqual(payload["max_tokens"], 900)
        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("reasoning_effort", payload)

    def test_payload_passes_explicit_reasoning_effort(self):
        for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                payload = build_chat_completion_payload(
                    "test-model", [], reasoning_effort=effort
                )
                self.assertEqual(payload["reasoning_effort"], effort)

    def test_payload_omits_empty_reasoning_effort(self):
        for effort in (None, "", "  "):
            with self.subTest(effort=effort):
                payload = build_chat_completion_payload(
                    "test-model", [], reasoning_effort=effort
                )
                self.assertNotIn("reasoning_effort", payload)

    def test_payload_rejects_invalid_reasoning_effort(self):
        with self.assertRaisesRegex(LlmConfigError, "reasoning effort"):
            build_chat_completion_payload("test-model", [], reasoning_effort="invalid")

    def test_payload_supports_completion_tokens_without_temperature(self):
        payload = build_chat_completion_payload(
            "test-model", [], max_output_tokens=900,
            token_limit_parameter="max_completion_tokens",
        )
        self.assertEqual(payload["max_completion_tokens"], 900)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("temperature", payload)

    def test_zero_token_limit_omits_both_limit_fields(self):
        for parameter in ("max_tokens", "max_completion_tokens"):
            with self.subTest(parameter=parameter):
                payload = build_chat_completion_payload(
                    "test-model", [], max_output_tokens=0,
                    token_limit_parameter=parameter,
                )
                self.assertNotIn("max_tokens", payload)
                self.assertNotIn("max_completion_tokens", payload)

    def test_invalid_token_parameter_fails_before_request(self):
        with self.assertRaises(LlmConfigError):
            build_chat_completion_payload(
                "test-model", [], token_limit_parameter="invalid"
            )

    def test_parser_rejects_complete_json_with_length_finish_reason(self):
        with self.assertRaisesRegex(LlmInvalidResponseError, "truncated"):
            parse_json_object_response({
                "choices": [{
                    "finish_reason": "length",
                    "message": {"content": '{"language":"zh"}'},
                }],
            })

    def test_parser_rejects_refusal_without_content(self):
        with self.assertRaisesRegex(LlmInvalidResponseError, "refused"):
            parse_json_object_response({
                "choices": [{"message": {"refusal": "Cannot comply"}}],
            })

    def test_parser_rejects_content_filter(self):
        with self.assertRaisesRegex(LlmInvalidResponseError, "refused"):
            parse_json_object_response({
                "choices": [{
                    "finish_reason": "content_filter",
                    "message": {"content": '{}'},
                }],
            })

    def test_strict_schema_error_does_not_downgrade(self):
        payload = build_chat_completion_payload(
            "test-model", [], json_schema={
                "name": "test_schema", "strict": True,
                "schema": {"type": "object", "properties": {},
                           "required": [], "additionalProperties": False},
            },
        )
        response = SimpleNamespace(
            status_code=400,
            json=lambda: {"error": {"message": "response_format not supported"}},
        )
        with patch("supysonic.llm_client.requests.post", return_value=response) as post:
            with self.assertRaises(LlmUpstreamError):
                post_chat_completion("https://llm.example/v1", "test-key", payload)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["response_format"]["type"], "json_schema")

    def test_upstream_errors_redact_key_and_bearer_values(self):
        key = "test-private-api-key"
        response = SimpleNamespace(
            status_code=401,
            json=lambda: {"error": {
                "message": f"Incorrect key {key}; Authorization: Bearer other-secret",
                "code": f"invalid_{key}",
            }},
        )
        with patch("supysonic.llm_client.requests.post", return_value=response):
            with self.assertRaises(LlmUpstreamError) as caught:
                post_chat_completion("https://llm.example/v1", key, {})
        details = json.dumps(caught.exception.details)
        self.assertNotIn(key, details)
        self.assertNotIn("other-secret", details)
        self.assertIn("[REDACTED]", details)
        self.assertEqual(caught.exception.details["upstreamStatus"], 401)

    def test_transport_error_redacts_key_and_suppresses_raw_cause(self):
        key = "test-private-api-key"
        error = requests.exceptions.ConnectionError(f"Connection failed with {key}")
        with patch("supysonic.llm_client.requests.post", side_effect=error):
            with self.assertRaises(LlmUpstreamError) as caught:
                post_chat_completion("https://llm.example/v1", key, {})
        self.assertNotIn(key, json.dumps(caught.exception.details))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_timeout_suppresses_raw_exception_details(self):
        error = requests.exceptions.Timeout("Timeout with test-private-api-key")
        with patch("supysonic.llm_client.requests.post", side_effect=error):
            with self.assertRaises(LlmTimeoutError) as caught:
                post_chat_completion("https://llm.example/v1", "test-private-api-key", {})
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
