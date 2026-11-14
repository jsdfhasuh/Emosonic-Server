import json
from unittest.mock import Mock, patch

from flask import current_app

from supysonic.db import RecommendationAgentCache, User
from supysonic.metadata_budget import MetadataBudget
from supysonic.openai_recommendations import recommendation_lease
from supysonic.recommendation_agent import _bounded_openai_prompt
from supysonic.recommendation_agent_cache import save_recommendation_agent_cache_payload

from .frontendtestbase import FrontendTestBase
from ..base.test_openai_recommendations import BUDGET, SETTINGS, completion


class OpenAIRecommendationChatTestCase(FrontendTestBase):
    def setUp(self):
        super().setUp()
        self._login("alice", "Alic3")
        with self.app_context():
            current_app.config["RECOMMENDATION_AGENT"] = dict(SETTINGS)
            current_app.config["TRACK_METADATA_LLM"] = dict(BUDGET)
        self.payload = {"reply": "Try melodic pop.", "recommendedArtists": [], "nextActions": []}
        self.response = Mock(status_code=200)
        self.response.json.return_value = completion(self.payload)
        self.post = patch("supysonic.openai_recommendations.requests.post", return_value=self.response).start()
        self.addCleanup(patch.stopall)

    def test_sync_is_accounted_cached_and_does_not_send_account_name(self):
        for _ in range(2):
            result = self.client.post("/recommendations/agent", json={"message": "Suggest music", "language": "en"})
            self.assertEqual(result.status_code, 200, result.data)
            self.assertEqual(result.json["agent"]["model"], "gpt-6-luna")
        self.assertEqual(self.post.call_count, 1)
        sent = self.post.call_args.kwargs["json"]
        context = json.loads(sent["messages"][1]["content"])["context"]
        self.assertNotIn("user", context)
        self.assertNotIn("alice", json.dumps(context))
        with self.app_context():
            self.assertEqual(MetadataBudget(BUDGET).snapshot()["month_spent_micro_usd"], 35)

    def test_sse_uses_same_accounted_path_and_event_shape(self):
        result = self.client.post("/recommendations/agent/stream", json={"message": "Suggest music"})
        self.assertEqual(result.status_code, 200)
        data = result.data
        self.assertIn("event: final", data)
        self.assertIn("event: reply_delta", data)
        self.assertEqual(self.post.call_count, 1)
        self.assertNotIn("stream", self.post.call_args.kwargs["json"])

    def test_bad_output_never_repairs_or_falls_back(self):
        self.response.json.return_value = completion({"bad": "data"})
        result = self.client.post("/recommendations/agent", json={"message": "Suggest music"})
        self.assertGreaterEqual(result.status_code, 500)
        self.assertEqual(self.post.call_count, 1)

    def test_clear_chat_preserves_background_caches(self):
        with self.app_context():
            user = User.get(User.name == "alice")
            for i, marker in enumerate(("__openai_profile_v1__", "__openai_profile_attempt_v1__",
                                        "__openai_daily_v1__", "__openai_daily_attempt_v1__", "chat")):
                save_recommendation_agent_cache_payload(user, str(i) * 64, marker, "en", "gpt-6-luna", {"ok": True})
        result = self.client.post("/recommendations/agent/session/clear")
        self.assertEqual(result.status_code, 200)
        with self.app_context():
            self.assertEqual(RecommendationAgentCache.select().count(), 4)

    def test_health_is_nonbillable(self):
        result = self.client.get("/recommendations/agent/health")
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json["configured"])
        self.assertEqual(result.json["provider"], "openai")
        self.post.assert_not_called()

    def test_same_user_chat_lease_prevents_another_paid_request(self):
        with self.app_context():
            user = User.get(User.name == "alice")
            with recommendation_lease("chat:" + str(user.id)):
                result = self.client.post("/recommendations/agent", json={"message": "Suggest music"})
                self.assertGreaterEqual(result.status_code, 500)
        self.post.assert_not_called()

    def test_non_ascii_context_is_byte_bounded_without_losing_facts(self):
        context = {"statistics": {"realListening": {"playCount": 17}},
                   "libraryArtists": ["\u4e2d" * 80] * 300,
                   "currentRecommendationTracks": [{"title": "\u4e2d" * 80}] * 50,
                   "playHistory": [{"title": "\u4e2d" * 80}] * 50,
                   "openaiProfile": {"summary": "A musical preference"}}
        result = _bounded_openai_prompt("music", context)
        self.assertLessEqual(len(result.encode("utf-8")), 48 * 1024)
        self.assertEqual(json.loads(result)["context"]["statistics"]["realListening"]["playCount"], 17)
