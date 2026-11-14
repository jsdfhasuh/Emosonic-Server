import json
import unittest
from unittest.mock import Mock, patch

import requests
from peewee import SqliteDatabase

from supysonic.db_layer.core import Meta, db
from supysonic.metadata_budget import MetadataBudget
from supysonic.openai_recommendations import (
    OpenAIRecommendationError, STATE_KEY, recommendation_lease,
    recommendation_settings, request_recommendation_json,
)


BUDGET = {"monthly_budget_usd": 60, "budget_safety_buffer_usd": 3,
          "daily_budget_usd": 2, "budget_timezone": "Asia/Shanghai"}
SETTINGS = {
    "enabled": True, "provider": "openai", "model": "gpt-6-luna",
    "api_base_url": "https://api.openai.com/v1", "api_key": "test-private-key",
    "timeout_seconds": 120, "reasoning_effort": "low", "_budget_config": BUDGET,
}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok"],
          "properties": {"ok": {"type": "boolean"}}}


def completion(content=None, **overrides):
    result = {
        "model": "gpt-6-luna", "service_tier": "default",
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"ok": True} if content is None else content),
        }}],
    }
    result.update(overrides)
    return result


class OpenAIRecommendationsTestCase(unittest.TestCase):
    def setUp(self):
        self.previous = db.obj
        self.database = SqliteDatabase(":memory:")
        db.initialize(self.database)
        self.database.create_tables([Meta])
        self.budget = MetadataBudget(BUDGET)
        self.response = Mock(status_code=200)
        self.response.json.return_value = completion()
        self.post = patch("supysonic.openai_recommendations.requests.post", return_value=self.response).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.database.close()
        db.initialize(self.previous)

    def call(self, workload="daily", settings=None):
        return request_recommendation_json(settings or SETTINGS, [
            {"role": "user", "content": "Music preferences only"},
        ], SCHEMA, workload=workload)

    def test_each_workload_uses_one_shared_ledger_and_settles(self):
        for workload in ("profile", "daily", "chat"):
            self.assertEqual(self.call(workload), {"ok": True})
        snapshot = self.budget.snapshot()
        self.assertEqual(snapshot["month_spent_micro_usd"], 105)
        self.assertEqual(snapshot["month_reserved_micro_usd"], 0)
        kinds = {json.loads(row.value)["k"] for row in Meta.select().where(Meta.key.startswith("mb:r:"))}
        self.assertEqual(kinds, {"recommendation_profile", "recommendation_daily", "recommendation_chat"})
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload["max_completion_tokens"], 4096)
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertNotIn("temperature", payload)
        self.assertFalse(self.post.call_args.kwargs["allow_redirects"])

    def test_authoritative_budget_is_from_metadata_not_agent_overrides(self):
        result = recommendation_settings({
            "RECOMMENDATION_AGENT": dict(SETTINGS, _budget_config={"monthly_budget_usd": 999}),
            "TRACK_METADATA_LLM": dict(BUDGET, api_key="do-not-copy"),
        })
        self.assertEqual(result["_budget_config"], BUDGET)
        self.assertNotIn("do-not-copy", str(result))

    def test_admission_occurs_before_http(self):
        self.budget.reserve("web", 2_000_000)
        with self.assertRaises(OpenAIRecommendationError) as caught:
            self.call()
        self.assertEqual(caught.exception.reason, "budget")
        self.post.assert_not_called()

    def test_unknown_usage_keeps_reservation_and_blocks_later_calls(self):
        self.response.json.return_value = completion(usage=None)
        with self.assertRaises(OpenAIRecommendationError):
            self.call()
        snapshot = self.budget.snapshot()
        self.assertGreater(snapshot["month_reserved_micro_usd"], 0)
        with self.assertRaises(OpenAIRecommendationError):
            self.call()
        self.assertEqual(self.post.call_count, 1)
        self.assertTrue(json.loads(Meta.get_by_id(STATE_KEY).value)["hard"])

    def test_network_error_no_hidden_retry_and_success_resets_streak(self):
        self.post.side_effect = requests.Timeout("secret request details")
        with self.assertRaises(OpenAIRecommendationError) as caught:
            self.call()
        self.assertNotIn("secret", str(caught.exception))
        state = json.loads(Meta.get_by_id(STATE_KEY).value)
        self.assertEqual(state["n"], 1)
        with self.assertRaises(OpenAIRecommendationError):
            self.call()
        self.assertEqual(self.post.call_count, 1)
        state["u"] = 0
        Meta.update(value=json.dumps(state)).where(Meta.key == STATE_KEY).execute()
        self.post.side_effect = None
        self.call()
        self.assertIsNone(Meta.get_or_none(Meta.key == STATE_KEY))
        self.assertGreater(self.budget.snapshot()["month_reserved_micro_usd"], 0)

    def test_transient_failures_grow_only_to_30_minutes(self):
        self.post.side_effect = requests.Timeout()
        with patch("supysonic.openai_recommendations.time.time", return_value=10000):
            for count, delay in ((1, 300), (2, 600), (3, 1200), (4, 1800), (4, 1800)):
                with self.assertRaises(OpenAIRecommendationError):
                    self.call()
                state = json.loads(Meta.get_by_id(STATE_KEY).value)
                self.assertEqual((state["n"], state["u"]), (count, 10000 + delay))
                state["u"] = 0
                Meta.update(value=json.dumps(state)).where(Meta.key == STATE_KEY).execute()

    def test_metadata_pause_does_not_suppress_recommendations(self):
        Meta.create(key="metadata_provider_backoff", value="metadata-specific")
        self.assertTrue(self.call()["ok"])

    def test_invalid_output_is_charged_without_repair(self):
        self.response.json.return_value = completion({"invented": True})
        with self.assertRaises(OpenAIRecommendationError) as caught:
            self.call()
        self.assertEqual(caught.exception.reason, "invalid_response")
        self.assertEqual(self.budget.snapshot()["month_spent_micro_usd"], 35)
        self.assertEqual(self.post.call_count, 1)

    def test_truncation_and_refusal_are_not_used(self):
        for field, value in (("finish_reason", "length"), ("finish_reason", "content_filter")):
            result = completion()
            result["choices"][0][field] = value
            self.response.json.return_value = result
            with self.assertRaises(OpenAIRecommendationError):
                self.call()

    def test_wrong_model_and_nonstandard_tier_fail_closed(self):
        for overrides in ({"model": "other"}, {"service_tier": "priority"}):
            self.response.json.return_value = completion(**overrides)
            with self.assertRaises(OpenAIRecommendationError):
                self.call()
            Meta.delete().where(Meta.key == STATE_KEY).execute()
        self.assertEqual(self.budget.snapshot()["month_spent_micro_usd"], 0)
        self.assertGreater(self.budget.snapshot()["month_reserved_micro_usd"], 0)

    def test_quota_response_hard_pauses_without_exposing_body(self):
        self.response.status_code = 429
        self.response.json.return_value = {"error": {"code": "insufficient_quota", "message": "test-private-key"}}
        with self.assertRaises(OpenAIRecommendationError) as caught:
            self.call()
        self.assertNotIn("test-private-key", str(caught.exception))
        self.assertTrue(json.loads(Meta.get_by_id(STATE_KEY).value)["hard"])

    def test_bad_configuration_and_oversize_never_send(self):
        for overrides in ({"api_base_url": "https://other.example/v1"},
                          {"model": "other"}, {"_budget_config": {}},
                          {"timeout_seconds": float("nan")}):
            with self.assertRaises(OpenAIRecommendationError):
                self.call(settings=dict(SETTINGS, **overrides))
        with self.assertRaises(OpenAIRecommendationError):
            request_recommendation_json(SETTINGS, [{"role": "user", "content": "x" * 70000}], SCHEMA, workload="chat")
        self.post.assert_not_called()

    def test_nested_lease_excludes_duplicates_and_releases(self):
        with recommendation_lease("daily:user:day") as acquired:
            self.assertTrue(acquired)
            with recommendation_lease("daily:user:day") as duplicate:
                self.assertFalse(duplicate)
        with recommendation_lease("daily:user:day") as acquired:
            self.assertTrue(acquired)

    def test_busy_provider_does_not_reserve(self):
        with recommendation_lease("provider"):
            with self.assertRaises(OpenAIRecommendationError) as caught:
                self.call()
        self.assertEqual(caught.exception.reason, "busy")
        self.post.assert_not_called()
        self.assertEqual(self.budget.snapshot()["month_reserved_micro_usd"], 0)
