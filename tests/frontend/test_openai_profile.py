from unittest.mock import patch

from .frontendtestbase import FrontendTestBase


class OpenAIProfileFrontendTestCase(FrontendTestBase):
    def setUp(self):
        super().setUp()
        self.config.RECOMMENDATION_AGENT.update({
            "enabled": True, "provider": "openai", "profile_enabled": True,
        })
        self.analysis = {
            "summary": "<script>private()</script>", "recentTrend": "Recent music trend.",
            "preferredLanguages": ["zh"], "preferredMoods": ["calm"],
            "discoveryDirections": ["Explore pop."], "cautions": "Sparse sample.",
            "provider": "openai", "model": "gpt-6-luna",
            "generatedAt": "2026-11-14T09:00:00", "stale": True,
        }

    def test_profile_and_api_only_read_cache_and_preserve_real_stats(self):
        self._login("alice", "Alic3")
        with patch("supysonic.frontend.user.get_cached_openai_profile", return_value=self.analysis), patch(
            "supysonic.recommendation_openai_profile.request_recommendation_json",
        ) as paid_request:
            page = self.client.get("/user/me")
            response = self.client.get("/api/me/listening-profile")
        paid_request.assert_not_called()
        self.assertEqual(page.status_code, 200)
        self.assertIn("OpenAI Music Preferences", page.data)
        self.assertIn("Last available analysis", page.data)
        self.assertIn("&lt;script&gt;private()&lt;/script&gt;", page.data)
        self.assertNotIn("<script>private()</script>", page.data)
        self.assertEqual(response.json["aiAnalysis"], self.analysis)
        self.assertEqual(response.json["playCount"], 0)
        self.assertEqual(response.json["recent7Days"]["playCount"], 0)

    def test_disabled_profile_does_not_read_cache_or_call_model(self):
        self.config.RECOMMENDATION_AGENT["profile_enabled"] = False
        self._login("alice", "Alic3")
        with patch("supysonic.frontend.user.get_cached_openai_profile") as cache, patch(
            "supysonic.recommendation_openai_profile.request_recommendation_json",
        ) as paid_request:
            page = self.client.get("/user/me")
            response = self.client.get("/api/me/listening-profile")
        cache.assert_not_called()
        paid_request.assert_not_called()
        self.assertNotIn("OpenAI Music Preferences", page.data)
        self.assertIsNone(response.json["aiAnalysis"])

    def test_profile_api_requires_authentication(self):
        with patch("supysonic.frontend.user.get_cached_openai_profile") as cache:
            response = self.client.get("/api/me/listening-profile")
        cache.assert_not_called()
        self.assertEqual(response.status_code, 302)
