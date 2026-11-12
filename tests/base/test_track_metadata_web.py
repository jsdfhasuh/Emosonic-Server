import copy
import json
import unittest
from unittest.mock import Mock, patch

import requests

from supysonic.llm_client import (
    LlmConfigError, LlmInvalidResponseError, LlmQuotaError, LlmTimeoutError, LlmUpstreamError,
)
from supysonic.track_metadata_web import (
    WebMetadataProvider, WebMetadataResultRejected,
    build_web_payload, parse_web_response, post_web_response,
)


URL = "https://music.example.com/album/123"
CONFIG = {"model": "gpt-6-luna", "api_base_url": "https://api.openai.com/v1", "api_key": "test-key"}
TRACK = {"title": "Track", "artist": "Artist", "album": "Album", "year": 2019,
         "file_name": "private.flac", "path_hints": ["/private/path"], "genre": "Pop"}


def result():
    return {
        "identity_status": "matched", "identity_confidence": 0.9,
        "language_confidence": 0.8,
        "metadata": {"language": "zh", "mood": ["Calm"], "scene": ["Reading"],
                     "tags": ["Pop"], "summary": "A pop track.", "energy": 42,
                     "valence": 50, "danceability": 23, "confidence": 0.7},
        "sourced_fields": ["title", "artist", "language", "genre", "tags"],
        "inferred_fields": ["mood", "scene"], "uncertainty": "Contexts are estimates.",
        "sources": [{"url": URL, "title": "Album", "supported_fields": [
            "title", "artist", "language", "genre", "tags"], "evidence_summary": "Catalog identity and genre."}],
    }


def response(parsed=None):
    return {"status": "completed", "model": "gpt-6-luna", "service_tier": "default",
            "usage": {"input_tokens": 200, "output_tokens": 100, "total_tokens": 300},
            "tool_usage": {"web_search": {"num_requests": 1}},
            "output": [
                {"type": "reasoning", "summary": [{"text": "not retained"}]},
                {"type": "web_search_call", "status": "completed", "action": {
                    "type": "search", "sources": [{"url": URL}]}},
                {"type": "message", "content": [{"type": "output_text",
                    "text": json.dumps(parsed if parsed is not None else result()), "annotations": []}]},
            ]}


class TrackMetadataWebTestCase(unittest.TestCase):
    def test_payload_caps_and_no_paths_or_keys(self):
        payload = build_web_payload(TRACK, CONFIG)
        self.assertEqual(payload["max_tool_calls"], 3)
        self.assertEqual(payload["max_output_tokens"], 4096)
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertEqual(payload["service_tier"], "default")
        self.assertFalse(payload["store"])
        serialized = json.dumps(payload)
        for excluded in ("test-key", "private.flac", "/private/path"):
            self.assertNotIn(excluded, serialized)
        self.assertIn("song or album detail page", payload["instructions"])

    def test_invalid_config_and_identity_fail_before_request(self):
        for update in ({"model": "other"}, {"api_base_url": "http://localhost"},
                       {"web_max_tool_calls": 4}, {"web_max_tool_calls": True},
                       {"web_max_output_tokens": 0}, {"web_max_output_tokens": 1.5}):
            config = dict(CONFIG, **update)
            with self.assertRaises(LlmConfigError):
                build_web_payload(TRACK, config)
        with self.assertRaises(LlmConfigError):
            build_web_payload(dict(TRACK, title=""), CONFIG)

    def test_parser_preserves_evidence_not_hidden_reasoning_or_audio_numbers(self):
        parsed = parse_web_response(response())
        self.assertEqual(parsed["source"], "llm")
        self.assertEqual(parsed["provider"], "llm")
        self.assertEqual(parsed["language"], "zh")
        self.assertIsNone(parsed["energy"])
        self.assertIsNone(parsed["valence"])
        self.assertIsNone(parsed["danceability"])
        evidence = parsed["raw"]["web_lookup"]
        self.assertTrue(evidence["identity_verified"])
        self.assertEqual(evidence["billable_searches"], 1)
        self.assertEqual(evidence["usage"]["input_tokens"], 200)
        self.assertNotIn("not retained", json.dumps(parsed))

    def test_ambiguous_identity_is_inconclusive_not_accepted(self):
        data = result()
        data["identity_status"] = "ambiguous"
        parsed = parse_web_response(response(data))
        self.assertIsNone(parsed["language"])
        self.assertEqual(parsed["confidence"], 0)
        self.assertEqual(parsed["mood"], [])
        self.assertEqual(parsed["raw"]["web_lookup"]["status"], "inconclusive")

    def test_matched_without_identity_evidence_is_inconclusive(self):
        data = result()
        data["sources"] = []
        parsed = parse_web_response(response(data))
        self.assertFalse(parsed["raw"]["web_lookup"]["identity_verified"])

    def test_language_requires_field_specific_source(self):
        data = result()
        data["sources"][0]["supported_fields"].remove("language")
        parsed = parse_web_response(response(data))
        self.assertIsNone(parsed["language"])

    def test_identity_only_does_not_support_semantic_inferences(self):
        data = result()
        data["sources"][0]["supported_fields"] = ["title", "artist"]
        parsed = parse_web_response(response(data))
        self.assertEqual(parsed["mood"], [])
        self.assertEqual(parsed["scene"], [])
        self.assertEqual(parsed["tags"], [])

    def test_refusal_incomplete_no_search_and_over_budget_rejected(self):
        cases = []
        value = response()
        value["status"] = "incomplete"
        cases.append(value)
        value = response()
        value["output"][-1]["content"] = [{"type": "refusal", "refusal": "no"}]
        cases.append(value)
        value = response()
        del value["output"][1]
        cases.append(value)
        value = response()
        value["output"] += [copy.deepcopy(value["output"][1])] * 3
        cases.append(value)
        for value in cases:
            with self.assertRaises(LlmInvalidResponseError):
                parse_web_response(value)

    def test_schema_rejects_missing_extra_nonfinite_and_bool_fields(self):
        cases = []
        value = result()
        del value["uncertainty"]
        cases.append(value)
        value = result()
        value["extra"] = "ignored?"
        cases.append(value)
        value = result()
        value["metadata"]["confidence"] = float("nan")
        cases.append(value)
        value = result()
        value["metadata"]["energy"] = True
        cases.append(value)
        for value in cases:
            with self.assertRaises(LlmInvalidResponseError):
                parse_web_response(response(value))

    def test_unretrieved_and_unsafe_source_urls_rejected(self):
        for url in ("https://music.example.com/invented", "http://music.example.com/song",
                    "https://user:pass@music.example.com/song", "https://127.0.0.1/song",
                    "https://host.local/song", "https://music.example.com/song?token=secret",
                    "https://music.example.com/song#access_token=secret"):
            data = result()
            data["sources"][0]["url"] = url
            value = response(data)
            if "invented" not in url:
                value["output"][1]["action"]["sources"] = [{"url": url}]
            with self.subTest(url=url), self.assertRaises(LlmInvalidResponseError):
                parse_web_response(value)

    def test_source_url_citation_can_supply_provenance(self):
        value = response()
        value["output"][1]["action"]["sources"] = []
        value["output"][-1]["content"][0]["annotations"] = [{"type": "url_citation", "url": URL}]
        self.assertEqual(parse_web_response(value)["language"], "zh")

    def test_opened_or_searched_detail_page_action_url_supplies_provenance(self):
        detail_url = "https://music.example.com/song/456"
        for action_type in ("open_page", "find_in_page"):
            with self.subTest(action_type=action_type):
                data = result()
                data["sources"][0]["url"] = detail_url
                value = response(data)
                value["output"].insert(2, {
                    "type": "web_search_call", "status": "completed",
                    "action": {"type": action_type, "url": detail_url},
                })
                parsed = parse_web_response(value)
                self.assertEqual(parsed["language"], "zh")
                self.assertEqual(parsed["raw"]["web_lookup"]["sources"][0]["url"], detail_url)

    def test_unsafe_detail_page_action_url_does_not_supply_provenance(self):
        for action_type in ("open_page", "find_in_page"):
            for url in ("http://music.example.com/song", "https://user:pass@music.example.com/song",
                        "https://127.0.0.1/song", "https://host.local/song",
                        "https://music.example.com/song?token=secret"):
                with self.subTest(action_type=action_type, url=url):
                    data = result()
                    data["sources"][0]["url"] = url
                    value = response(data)
                    value["output"].insert(2, {
                        "type": "web_search_call", "status": "completed",
                        "action": {"type": action_type, "url": url},
                    })
                    with self.assertRaises(LlmInvalidResponseError):
                        parse_web_response(value)

    def test_search_action_url_alone_does_not_supply_detail_page_provenance(self):
        value = response()
        value["output"][1]["action"] = {"type": "search", "url": URL, "sources": []}
        with self.assertRaises(LlmInvalidResponseError):
            parse_web_response(value)

    def test_opened_detail_page_does_not_trust_a_different_model_url(self):
        data = result()
        data["sources"][0]["url"] = "https://music.example.com/song/invented"
        value = response(data)
        value["output"].insert(2, {
            "type": "web_search_call", "status": "completed",
            "action": {"type": "open_page", "url": "https://music.example.com/song/456"},
        })
        with self.assertRaises(LlmInvalidResponseError):
            parse_web_response(value)

    def test_injected_request_callback_is_used_once(self):
        callback = Mock(return_value=response())
        parsed = WebMetadataProvider(CONFIG, request_callback=callback).enrich(TRACK)
        self.assertEqual(parsed["model"], "gpt-6-luna")
        callback.assert_called_once()

    def test_provider_evidence_rejection_after_successful_callback_has_distinct_type(self):
        value = response()
        value["output"] = []
        callback = Mock(return_value=value)
        with self.assertRaises(WebMetadataResultRejected):
            WebMetadataProvider(CONFIG, request_callback=callback).enrich(TRACK)
        callback.assert_called_once()

    def test_callback_or_transport_invalid_response_is_not_translated(self):
        for use_callback in (True, False):
            with self.subTest(use_callback=use_callback):
                error = LlmInvalidResponseError("Unparsed upstream response.")
                callback = Mock(side_effect=error)
                provider = WebMetadataProvider(CONFIG, request_callback=callback if use_callback else None)
                with patch("supysonic.track_metadata_web.post_web_response", side_effect=error) as post:
                    with self.assertRaises(LlmInvalidResponseError) as raised:
                        provider.enrich(TRACK)
                self.assertIs(raised.exception, error)
                self.assertNotIsInstance(raised.exception, WebMetadataResultRejected)
                if use_callback:
                    callback.assert_called_once()
                    post.assert_not_called()
                else:
                    post.assert_called_once()

    @patch("supysonic.track_metadata_web.requests.post")
    def test_transport_fixed_endpoint_no_redirect_or_retry(self, post):
        upstream = Mock(status_code=200, content=b"{}")
        upstream.json.return_value = response()
        post.return_value = upstream
        self.assertEqual(post_web_response("key", build_web_payload(TRACK, CONFIG))["status"], "completed")
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://api.openai.com/v1/responses")
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        upstream.close.assert_called_once()

    @patch("supysonic.track_metadata_web.requests.post")
    def test_timeout_and_request_exception_never_retry_or_echo_secrets(self, post):
        for error, expected in ((requests.exceptions.Timeout("secret-key"), LlmTimeoutError),
                                (requests.exceptions.ConnectionError("secret-key"), LlmUpstreamError)):
            post.reset_mock()
            post.side_effect = error
            with self.assertRaises(expected) as raised:
                post_web_response("secret-key", build_web_payload(TRACK, CONFIG))
            self.assertNotIn("secret-key", str(raised.exception))
            post.assert_called_once()

    @patch("supysonic.track_metadata_web.requests.post")
    def test_http_redirect_and_quota_are_safe(self, post):
        for status, code, expected in ((302, None, LlmUpstreamError),
                                       (429, "insufficient_quota", LlmQuotaError)):
            upstream = Mock(status_code=status)
            upstream.json.return_value = {"error": {"code": code, "message": "Bearer secret-key"}}
            post.return_value = upstream
            with self.assertRaises(expected) as raised:
                post_web_response("secret-key", build_web_payload(TRACK, CONFIG))
            self.assertNotIn("secret-key", str(raised.exception))
            self.assertNotIn("secret-key", str(raised.exception.details))
            upstream.close.assert_called_once()
