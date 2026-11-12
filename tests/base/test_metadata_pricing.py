import unittest

from supysonic.llm_client import LlmConfigError
from supysonic.metadata_pricing import (
    reserve_request_micro_usd, response_cost_micro_usd,
)


class MetadataPricingTestCase(unittest.TestCase):
    def payload(self, kind="text"):
        result = {"model": "gpt-6-luna", "service_tier": "default"}
        if kind == "text":
            result.update({"messages": [{"role": "user", "content": "test"}],
                           "max_completion_tokens": 1536})
        else:
            result.update({"max_output_tokens": 4096, "max_tool_calls": 3,
                           "tools": [{"type": "web_search", "search_context_size": "low"}]})
        return result

    def response(self, kind="text", inputs=1000, outputs=100):
        usage = ({"prompt_tokens": inputs, "completion_tokens": outputs} if kind == "text"
                 else {"input_tokens": inputs, "output_tokens": outputs})
        return {"model": "gpt-6-luna", "service_tier": "default", "usage": usage,
                "tool_usage": {"web_search": {"num_requests": 1}}}

    def test_web_reservation_is_one_dollar(self):
        self.assertEqual(reserve_request_micro_usd(self.payload("web"), "web"), 1000000)

    def test_text_reservation_is_positive_and_bounded(self):
        reserved = reserve_request_micro_usd(self.payload())
        self.assertGreater(reserved, 768)
        self.assertLess(reserved, 20000)

    def test_request_model_tier_size_and_output_fail_closed(self):
        for update in ({"model": "unknown"}, {"service_tier": "fast"},
                       {"max_completion_tokens": 0}, {"max_completion_tokens": True},
                       {"max_completion_tokens": 4097}, {"n": 2},
                       {"input": "x" * 65536}, {"tools": [{"type": "web_search"}]}):
            with self.subTest(update=list(update)):
                payload = self.payload()
                payload.update(update)
                with self.assertRaises(LlmConfigError):
                    reserve_request_micro_usd(payload)
        payload = self.payload()
        del payload["service_tier"]
        with self.assertRaises(LlmConfigError):
            reserve_request_micro_usd(payload)

    def test_web_requires_low_context_and_tool_limit(self):
        for update in ({"max_tool_calls": 4}, {"max_tool_calls": True},
                       {"tools": [{"type": "web_search", "search_context_size": "high"}]}):
            payload = self.payload("web")
            payload.update(update)
            with self.assertRaises(LlmConfigError):
                reserve_request_micro_usd(payload, "web")

    def test_text_usage_conservatively_ignores_cache_discount(self):
        response = self.response()
        response["usage"]["prompt_tokens_details"] = {"cached_tokens": 600, "cache_write_tokens": 400}
        self.assertEqual(response_cost_micro_usd(response), 300)

    def test_web_uses_billable_requests_not_actions(self):
        response = self.response("web")
        response["tool_usage"]["web_search"]["num_requests"] = 2
        response["output"] = [{"type": "web_search_call"}] * 3
        self.assertEqual(response_cost_micro_usd(response, kind="web"), 20300)

    def test_missing_billable_usage_retains_reservation(self):
        response = self.response("web")
        for value in (None, {}, {"web_search": {}}, {"web_search": {"num_requests": -1}},
                      {"web_search": {"num_requests": True}}):
            response["tool_usage"] = value
            self.assertIsNone(response_cost_micro_usd(response, kind="web"))

    def test_long_context_boundary_and_rounding(self):
        self.assertEqual(response_cost_micro_usd(self.response(inputs=272000, outputs=1)), 68001)
        self.assertEqual(response_cost_micro_usd(self.response(inputs=272001, outputs=1)), 136002)

    def test_bad_usage_model_or_tier_does_not_release_reservation(self):
        for update in ({"usage": None}, {"model": "other"}, {"service_tier": "fast"},
                       {"usage": {"prompt_tokens": True, "completion_tokens": 1}},
                       {"usage": {"prompt_tokens": -1, "completion_tokens": 1}},
                       {"usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 4}},
                       {"usage": {"prompt_tokens": 10, "completion_tokens": 1,
                                  "prompt_tokens_details": {"cached_tokens": 11}}}):
            response = self.response()
            response.update(update)
            self.assertIsNone(response_cost_micro_usd(response))
        response = self.response()
        del response["service_tier"]
        self.assertIsNone(response_cost_micro_usd(response))

    def test_large_actual_charge_is_not_clamped_to_reservation(self):
        response = self.response("web", inputs=3000000, outputs=4096)
        self.assertGreater(response_cost_micro_usd(response, kind="web"), 1000000)
