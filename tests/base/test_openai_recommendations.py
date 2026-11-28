import json
import unittest
from unittest.mock import Mock, patch

import requests
from peewee import SqliteDatabase

from supysonic.db_layer.core import Meta, db
from supysonic.metadata_budget import MetadataBudget
from supysonic.metadata_pricing import PROFILE_MODEL
from supysonic.openai_recommendations import (
    DAILY_SOL_STATE_KEY, DISCOVERY_STATE_KEY, OpenAIRecommendationError, PROFILE_STATE_KEY,
    STATE_KEY, daily_request_model, discovery_request_model, recommendation_lease,
    recommendation_settings, request_recommendation_json,
)


BUDGET = {"monthly_budget_usd": 60, "budget_safety_buffer_usd": 3,
          "daily_budget_usd": 2, "budget_timezone": "Asia/Shanghai"}
SETTINGS = {
    "enabled": True, "provider": "openai", "model": "gpt-6-luna", "daily_model": "gpt-6-luna",
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


def discovery_response(inputs=100, outputs=20):
    reply = Mock(status_code=200)
    reply.json.return_value = {
        "model": PROFILE_MODEL, "service_tier": "default", "status": "completed",
        "usage": {"input_tokens": inputs, "output_tokens": outputs, "total_tokens": inputs + outputs},
        "output": [{"type": "reasoning"}, {"type": "message", "role": "assistant",
                   "content": [{"type": "output_text", "text": '{"ok":true}'}]}],
    }
    return reply


def token_count(tokens):
    response = Mock(status_code=200)
    response.json.return_value = {"object": "response.input_tokens", "input_tokens": tokens}
    return response


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
        self.post.side_effect = lambda url, **kwargs: discovery_response() if url.endswith("/responses") else self.response
        for workload in ("profile", "daily", "chat", "album_discovery"):
            self.assertEqual(self.call(workload), {"ok": True})
        snapshot = self.budget.snapshot()
        self.assertEqual(snapshot["month_spent_micro_usd"], 805)
        self.assertEqual(snapshot["month_reserved_micro_usd"], 0)
        kinds = {json.loads(row.value)["k"] for row in Meta.select().where(Meta.key.startswith("mb:r:"))}
        self.assertEqual(kinds, {
            "recommendation_profile", "recommendation_daily", "recommendation_chat",
            "recommendation_album_discovery",
        })
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload["max_output_tokens"], 4096)
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertNotIn("temperature", payload)
        self.assertFalse(self.post.call_args.kwargs["allow_redirects"])

    def test_authoritative_budget_is_from_metadata_not_agent_overrides(self):
        result = recommendation_settings({
            "RECOMMENDATION_AGENT": dict(SETTINGS, _budget_config={"monthly_budget_usd": 999}),
            "TRACK_METADATA_LLM": dict(BUDGET, api_key="do-not-copy"),
        })
        self.assertEqual(result["_budget_config"], BUDGET)
        self.assertNotIn("do-not-copy", str(result))

    def test_daily_defaults_to_sol_and_explicit_luna_keeps_legacy_transport(self):
        settings = dict(SETTINGS, reasoning_effort="none")
        del settings["daily_model"]
        self.assertEqual(daily_request_model(settings), PROFILE_MODEL)
        self.post.side_effect = [discovery_response(), self.response, self.response]
        self.assertTrue(self.call("daily", settings)["ok"])
        url, payload = self.post.call_args.args[0], self.post.call_args.kwargs["json"]
        self.assertEqual(url, "https://api.openai.com/v1/responses")
        self.assertEqual(payload["model"], PROFILE_MODEL)
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertEqual(payload["text"]["format"]["name"], "music_daily")
        for workload in ("daily", "chat"):
            self.assertTrue(self.call(workload, SETTINGS)["ok"])
            self.assertEqual(self.post.call_args.args[0], "https://api.openai.com/v1/chat/completions")
            self.assertEqual(self.post.call_args.kwargs["json"]["model"], "gpt-6-luna")
        reservations = [json.loads(row.value) for row in Meta.select().where(Meta.key.startswith("mb:r:"))]
        self.assertEqual(sorted(row["a"] for row in reservations if row["k"] == "recommendation_daily"),
                         [35, 700])

    def test_large_daily_sol_uses_official_count_and_full_request_long_price(self):
        settings = dict(SETTINGS, daily_model=PROFILE_MODEL, _budget_config=dict(BUDGET, daily_budget_usd=10))
        messages = [{"role": "user", "content": "音乐" * 200_000}]
        self.post.side_effect = [token_count(272_001), discovery_response(inputs=272_001)]
        self.assertTrue(request_recommendation_json(settings, messages, SCHEMA, workload="daily")["ok"])
        count, generate = self.post.call_args_list
        self.assertEqual(count.args[0], "https://api.openai.com/v1/responses/input_tokens")
        self.assertEqual(generate.args[0], "https://api.openai.com/v1/responses")
        self.assertEqual(count.kwargs["json"], {k: generate.kwargs["json"][k]
                                               for k in ("model", "input", "text", "reasoning")})
        charge = json.loads(Meta.select().where(Meta.key.startswith("mb:r:")).get().value)
        self.assertEqual(charge["k"], "recommendation_daily")
        self.assertEqual(charge["r"], 272_001 * 10 + 4096 * 15)
        self.assertEqual(charge["a"], 272_001 * 10 + 20 * 15)

    def test_daily_sol_enforces_shared_limits_without_generating(self):
        settings = dict(SETTINGS, daily_model=PROFILE_MODEL)
        for tokens, reason in ((272_001, "budget"), (922_001, "context_limit")):
            with self.subTest(tokens=tokens):
                self.post.reset_mock()
                self.post.return_value = token_count(tokens)
                with self.assertRaises(OpenAIRecommendationError) as caught:
                    request_recommendation_json(settings, [{"role": "user", "content": "x" * 70_000}],
                                                SCHEMA, workload="daily")
                self.assertEqual(caught.exception.reason, reason)
                self.post.assert_called_once()
                self.assertTrue(self.post.call_args.args[0].endswith("/input_tokens"))
                self.assertEqual(self.budget.snapshot()["day_reserved_micro_usd"], 0)

    def test_daily_sol_uses_original_mutex_and_has_its_own_provider_pause(self):
        settings = dict(SETTINGS, daily_model=PROFILE_MODEL, profile_model=PROFILE_MODEL)
        with recommendation_lease("provider"), self.assertRaises(OpenAIRecommendationError) as caught:
            self.call("daily", settings)
        self.assertEqual(caught.exception.reason, "busy")
        self.post.assert_not_called()
        rejected = Mock(status_code=400, headers={})
        rejected.json.return_value = {"error": {"message": "bad daily request"}}
        profile = Mock(status_code=200)
        profile.json.return_value = completion(model=PROFILE_MODEL)
        self.post.side_effect = [rejected, profile, self.response, discovery_response()]
        with self.assertRaises(OpenAIRecommendationError):
            self.call("daily", settings)
        self.assertTrue(json.loads(Meta.get_by_id(DAILY_SOL_STATE_KEY).value)["hard"])
        self.assertTrue(self.call("profile", settings)["ok"])
        self.assertTrue(self.call("chat", settings)["ok"])
        self.assertTrue(self.call("album_discovery", settings)["ok"])
        with self.assertRaises(OpenAIRecommendationError) as caught:
            self.call("daily", settings)
        self.assertEqual(caught.exception.reason, "paused")
        self.assertEqual(self.post.call_count, 4)

    def test_daily_sol_ignores_profile_pause_and_honors_shared_accounting_stop(self):
        settings = dict(SETTINGS, daily_model=PROFILE_MODEL, profile_model=PROFILE_MODEL)
        Meta.create(key=PROFILE_STATE_KEY, value=json.dumps({"n": 1, "u": 0, "hard": True}))
        self.post.return_value = discovery_response()
        self.assertTrue(self.call("daily", settings)["ok"])
        invalid = discovery_response()
        invalid.json.return_value["usage"] = None
        self.post.return_value = invalid
        with self.assertRaises(OpenAIRecommendationError) as caught:
            self.call("daily", settings)
        self.assertEqual(caught.exception.reason, "accounting")
        self.assertTrue(json.loads(Meta.get_by_id(STATE_KEY).value)["accounting"])
        for workload in ("daily", "chat", "profile", "album_discovery"):
            with self.subTest(workload=workload), self.assertRaises(OpenAIRecommendationError) as caught:
                self.call(workload, settings)
            self.assertEqual(caught.exception.reason, "paused")
        self.assertEqual(self.post.call_count, 2)

    def test_discovery_defaults_to_sol_and_other_workloads_keep_their_limits(self):
        self.assertEqual(discovery_request_model(SETTINGS), "gpt-6.2-sol")
        self.post.return_value = discovery_response()
        self.assertEqual(self.call("album_discovery", dict(SETTINGS, reasoning_effort="none")), {"ok": True})
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "gpt-6.2-sol")
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertFalse(payload["store"])
        self.post.reset_mock()
        settings = dict(SETTINGS, profile_model=PROFILE_MODEL, discovery_model="gpt-6-luna")
        for workload in ("profile", "daily", "chat", "album_discovery"):
            with self.subTest(workload=workload), self.assertRaises(OpenAIRecommendationError):
                request_recommendation_json(settings, [{"role": "user", "content": "x" * 70_000}],
                                            SCHEMA, workload=workload)
        self.post.assert_not_called()

    def test_long_discovery_counts_the_same_model_input_and_reserves_full_long_price(self):
        messages = [{"role": "user", "content": "音乐" * 200_000}]
        budget = dict(BUDGET, daily_budget_usd=10)
        settings = dict(SETTINGS, _budget_config=budget)
        expected = 272_001 * 10 + 3_072 * 15
        def request(url, **kwargs):
            if url.endswith("/input_tokens"):
                self.assertEqual(MetadataBudget(budget).snapshot()["day_reserved_micro_usd"], 0)
                return token_count(272_001)
            self.assertEqual(MetadataBudget(budget).snapshot()["day_reserved_micro_usd"], expected)
            return discovery_response(272_001, 20)
        self.post.side_effect = request
        self.assertEqual(request_recommendation_json(settings, messages, SCHEMA, workload="album_discovery",
                                                     max_output_tokens=3072), {"ok": True})
        count, generation = self.post.call_args_list
        self.assertEqual(count.args[0], "https://api.openai.com/v1/responses/input_tokens")
        self.assertEqual(generation.args[0], "https://api.openai.com/v1/responses")
        self.assertEqual(count.kwargs["json"], {key: generation.kwargs["json"][key]
                                              for key in ("model", "input", "text", "reasoning")})
        totals = MetadataBudget(budget).snapshot()
        self.assertEqual(totals["day_reserved_micro_usd"], 0)
        self.assertEqual(totals["day_spent_micro_usd"], 272_001 * 10 + 20 * 15)

    def test_oversized_invalid_count_and_daily_cap_never_generate(self):
        messages = [{"role": "user", "content": "x" * 70_000}]
        for count, reason in ((922_001, "context_limit"), (True, "preflight"), (272_001, "budget")):
            with self.subTest(count=count):
                self.post.reset_mock()
                self.post.return_value = token_count(count)
                with self.assertRaises(OpenAIRecommendationError) as caught:
                    request_recommendation_json(SETTINGS, messages, SCHEMA, workload="album_discovery")
                self.assertEqual(caught.exception.reason, reason)
                self.post.assert_called_once()
                self.assertTrue(self.post.call_args.args[0].endswith("/input_tokens"))
                self.assertEqual(self.budget.snapshot()["day_reserved_micro_usd"], 0)

    def test_small_discovery_can_use_exact_count_near_daily_limit(self):
        self.budget.reserve("metadata", 1_950_000)
        self.post.side_effect = [token_count(100), discovery_response()]
        self.assertEqual(self.call("album_discovery"), {"ok": True})
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.budget.snapshot()["day_spent_micro_usd"], 700)

    def test_input_counter_explicit_context_rejection_never_reserves(self):
        reply = Mock(status_code=400)
        reply.json.return_value = {"error": {"code": "context_length_exceeded"}}
        self.post.return_value = reply
        with self.assertRaises(OpenAIRecommendationError) as caught:
            request_recommendation_json(SETTINGS, [{"role": "user", "content": "x" * 70_000}],
                                        SCHEMA, workload="album_discovery")
        self.assertEqual(caught.exception.reason, "context_limit")
        self.post.assert_called_once()
        self.assertEqual(self.budget.snapshot()["day_reserved_micro_usd"], 0)

    def test_incomplete_or_refused_responses_still_settle_usage(self):
        for refusal in (False, True):
            reply = discovery_response()
            if refusal:
                reply.json.return_value["output"][-1]["content"] = [{"type": "refusal", "refusal": "x"}]
            else:
                reply.json.return_value["status"] = "incomplete"
            self.post.return_value = reply
            with self.assertRaises(OpenAIRecommendationError) as caught:
                self.call("album_discovery")
            self.assertEqual(caught.exception.reason, "invalid_response")
        self.assertEqual(self.budget.snapshot()["day_spent_micro_usd"], 1400)

    def test_profile_model_and_pricing_are_independent_of_daily_and_chat(self):
        settings = dict(SETTINGS, profile_model=PROFILE_MODEL, reasoning_effort="none")
        self.response.json.return_value = completion(model=PROFILE_MODEL)
        self.assertTrue(self.call("profile", settings)["ok"])
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], PROFILE_MODEL)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(self.budget.snapshot()["month_spent_micro_usd"], 700)

        self.response.json.return_value = completion()
        for workload in ("daily", "chat"):
            self.assertTrue(self.call(workload, settings)["ok"])
            payload = self.post.call_args.kwargs["json"]
            self.assertEqual(payload["model"], SETTINGS["model"])
            self.assertEqual(payload["reasoning_effort"], "none")
        self.assertEqual(self.budget.snapshot()["month_spent_micro_usd"], 770)

    def test_profile_model_failure_does_not_pause_luna(self):
        self.response.status_code = 404
        self.response.json.return_value = {"error": {"code": "model_not_found"}}
        settings = dict(SETTINGS, profile_model=PROFILE_MODEL)
        with self.assertRaises(OpenAIRecommendationError):
            self.call("profile", settings)
        self.assertTrue(json.loads(Meta.get_by_id(PROFILE_STATE_KEY).value)["hard"])
        self.assertIsNone(Meta.get_or_none(Meta.key == STATE_KEY))

        self.response.status_code = 200
        self.response.json.return_value = completion()
        self.assertTrue(self.call("daily", settings)["ok"])
        self.assertGreater(self.budget.snapshot()["month_reserved_micro_usd"], 0)

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
                          {"daily_model": "other"}, {"_budget_config": {}},
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

    def test_discovery_hard_upstream_error_only_pauses_discovery(self):
        rejected = Mock(status_code=400, headers={})
        rejected.json.return_value = {"error": {"message": "request refused"}}
        self.post.side_effect = [rejected, self.response]
        with self.assertRaises(OpenAIRecommendationError) as caught:
            self.call("album_discovery")
        self.assertEqual(caught.exception.reason, "upstream")
        state = json.loads(Meta.get_by_id(DISCOVERY_STATE_KEY).value)
        self.assertEqual((state["hard"], state["accounting"]), (True, False))
        self.assertIsNone(Meta.get_or_none(Meta.key == STATE_KEY))
        self.assertEqual(self.call("daily"), {"ok": True})
        with self.assertRaises(OpenAIRecommendationError) as caught:
            self.call("album_discovery")
        self.assertEqual(caught.exception.reason, "paused")
        self.assertEqual(self.post.call_count, 2)
        self.assertGreater(self.budget.snapshot()["month_reserved_micro_usd"], 0)

    def test_discovery_network_failure_only_pauses_discovery(self):
        self.post.side_effect = [requests.Timeout(), self.response]
        with self.assertRaises(OpenAIRecommendationError) as caught:
            self.call("album_discovery")
        self.assertEqual(caught.exception.reason, "network")
        self.assertIsNone(Meta.get_or_none(Meta.key == STATE_KEY))
        self.assertEqual(self.call("daily"), {"ok": True})
        with self.assertRaises(OpenAIRecommendationError) as caught:
            self.call("album_discovery")
        self.assertEqual(caught.exception.reason, "paused")
        self.assertEqual(self.post.call_count, 2)

    def test_discovery_inflight_can_overlap_daily_but_not_another_discovery(self):
        def request(*args, **kwargs):
            if args[0].endswith("/responses"):
                with self.assertRaises(OpenAIRecommendationError) as caught:
                    self.call("album_discovery")
                self.assertEqual(caught.exception.reason, "busy")
                self.assertEqual(self.call("daily"), {"ok": True})
                return discovery_response()
            return self.response

        self.post.side_effect = request
        self.assertEqual(self.call("album_discovery"), {"ok": True})
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.budget.snapshot()["month_spent_micro_usd"], 735)

    def test_inflight_daily_success_cannot_clear_discovery_accounting_stop(self):
        expired = json.dumps({"n": 1, "u": 0, "hard": False})
        Meta.create(key=STATE_KEY, value=expired)
        unverifiable = Mock(status_code=200)
        unverifiable.json.return_value = completion(usage=None)

        def request(*args, **kwargs):
            if args[0].endswith("/responses"):
                return unverifiable
            with self.assertRaises(OpenAIRecommendationError) as caught:
                self.call("album_discovery")
            self.assertEqual(caught.exception.reason, "accounting")
            return self.response

        self.post.side_effect = request
        self.assertEqual(self.call("daily"), {"ok": True})
        state = json.loads(Meta.get_by_id(STATE_KEY).value)
        self.assertTrue(state["accounting"])
        self.assertGreater(self.budget.snapshot()["month_reserved_micro_usd"], 0)
        for workload in ("daily", "album_discovery"):
            with self.assertRaises(OpenAIRecommendationError) as caught:
                self.call(workload)
            self.assertEqual(caught.exception.reason, "paused")
        self.assertEqual(self.post.call_count, 2)

    def test_inflight_daily_network_failure_cannot_replace_discovery_accounting_stop(self):
        expired = json.dumps({"n": 1, "u": 0, "hard": False})
        Meta.create(key=STATE_KEY, value=expired)
        unverifiable = Mock(status_code=200)
        unverifiable.json.return_value = completion(usage=None)

        def request(*args, **kwargs):
            if args[0].endswith("/responses"):
                return unverifiable
            with self.assertRaises(OpenAIRecommendationError) as caught:
                self.call("album_discovery")
            self.assertEqual(caught.exception.reason, "accounting")
            raise requests.Timeout()

        self.post.side_effect = request
        with self.assertRaises(OpenAIRecommendationError) as caught:
            self.call("daily")
        self.assertEqual(caught.exception.reason, "network")
        self.assertTrue(json.loads(Meta.get_by_id(STATE_KEY).value)["accounting"])
        with self.assertRaises(OpenAIRecommendationError) as caught:
            self.call("daily")
        self.assertEqual(caught.exception.reason, "paused")
        self.assertEqual(self.post.call_count, 2)
