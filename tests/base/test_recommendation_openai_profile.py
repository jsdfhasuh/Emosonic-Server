import json

from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import patch

from supysonic.db import (
    Album, Artist, Folder, RecommendationAgentCache, Track, TrackMetadata,
    User, User_Play_Activity, now,
)
from supysonic.recommendation_openai_profile import (
    PROFILE_ATTEMPT_HASH, PROFILE_CONTEXT_HASH, PROFILE_SCHEMA, build_openai_profile_facts,
    get_cached_openai_profile, openai_profile_enabled, refresh_openai_profile,
    refresh_openai_profiles,
)
from supysonic.openai_recommendations import OpenAIRecommendationError, _validate_json

from ..testbase import TestBase


class OpenAIProfileTestCase(TestBase):
    def setUp(self):
        super().setUp()
        self.clock = now()
        for target in ("supysonic.recommendation_openai_profile.now",
                       "supysonic.recommendation_agent_cache.now"):
            patcher = patch(target, side_effect=lambda: self.clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.lease = patch("supysonic.recommendation_openai_profile.recommendation_lease",
                           side_effect=lambda *args, **kwargs: nullcontext(True))
        self.lease_mock = self.lease.start()
        self.addCleanup(self.lease.stop)
        self.user = User.get(User.name == "alice")
        self.config.RECOMMENDATION_AGENT.update({
            "enabled": True, "provider": "openai", "profile_enabled": True,
            "model": "gpt-6-luna", "profile_batch_size": 1,
        })
        folder = Folder.create(root=True, name="Root", path="/private/music")
        artist = Artist.create(name="Example Artist")
        album = Album.create(name="Example Album", artist=artist)
        self.track = Track.create(
            disc=1, number=1, title="Example Song", duration=180, album=album,
            artist=artist, genre="pop", bitrate=320, path="/private/music/example.flac",
            last_modification=1, root_folder=folder, folder=folder,
        )
        self.metadata = TrackMetadata.create(
            track=self.track, track_last_modification=1, mood_json='["calm"]',
            scene_json='["night"]', tags_json='["pop"]', language="en",
            energy=30, valence=60, danceability=35, confidence=0.9,
            provider="llm", source="llm",
        )
        User_Play_Activity.create(track=self.track, user=self.user, time=self.clock)
        self.analysis = {
            "summary": "Music preference based on real plays.",
            "recentTrend": "Recent preference remains calm.",
            "preferredLanguages": ["en"], "preferredMoods": ["calm"],
            "discoveryDirections": ["Explore low-energy pop."],
            "cautions": "Limited listening sample.",
        }

    def request(self, **kwargs):
        options = {"return_value": self.analysis}
        options.update(kwargs)
        return patch("supysonic.recommendation_openai_profile.request_recommendation_json", **options)

    def expire(self, hours=7):
        self.clock += timedelta(hours=hours)

    def test_facts_are_bounded_aggregates_without_account_or_local_paths(self):
        self.user.mail = "private@example.invalid"
        self.user.save()
        facts = build_openai_profile_facts(self.user)
        self.assertEqual(facts["realListening"]["lifetime"]["playCount"], 1)
        self.assertEqual(facts["metadataCovered"]["lifetime"]["playCount"], 1)
        serialized = json.dumps(facts)
        for private in ("alice", "private@example.invalid", "/private", str(self.user.id),
                        str(self.track.id), "password", "lastfm_session"):
            self.assertNotIn(private, serialized)
        self.assertLess(len(serialized), 12000)

    def test_missing_metadata_is_not_reported_as_missing_listening(self):
        self.metadata.confidence = 0.1
        self.metadata.save()
        facts = build_openai_profile_facts(self.user)
        self.assertEqual(facts["realListening"]["lifetime"]["playCount"], 1)
        self.assertEqual(facts["metadataCovered"]["lifetime"]["playCount"], 0)
        self.assertEqual(facts["metadataCovered"]["lifetime"]["topLanguages"], [])

    def test_future_events_are_excluded_from_both_actual_and_covered_counts(self):
        User_Play_Activity.create(track=self.track, user=self.user,
                                  time=self.clock + timedelta(days=1))
        facts = build_openai_profile_facts(self.user)
        for window in ("lifetime", "recent7Days", "recent30Days"):
            self.assertEqual(facts["realListening"][window]["playCount"], 1)
            self.assertEqual(facts["metadataCovered"][window]["playCount"], 1)

    def test_recent_language_shift_is_preserved_in_factual_prompt(self):
        User_Play_Activity.update(time=self.clock - timedelta(days=40)).execute()
        self.metadata.language = "ko"
        self.metadata.save()
        chinese = Track.create(
            disc=1, number=2, title="Recent Chinese Song", duration=180,
            album=self.track.album, artist=self.track.artist, genre="Mandopop", bitrate=320,
            path="/private/music/chinese.flac", last_modification=1,
            root_folder=self.track.root_folder, folder=self.track.folder,
        )
        TrackMetadata.create(track=chinese, track_last_modification=1,
                             language="zh", mood_json='["calm"]', confidence=0.9,
                             provider="llm", source="llm")
        User_Play_Activity.create(track=chinese, user=self.user, time=self.clock)
        facts = build_openai_profile_facts(self.user)
        self.assertEqual(facts["metadataCovered"]["recent7Days"]["topLanguages"],
                         [{"value": "zh", "playCount": 1}])
        self.assertEqual(len(facts["metadataCovered"]["lifetime"]["topLanguages"]), 2)

    def test_profile_schema_is_supported_by_shared_transport_validator(self):
        _validate_json(self.analysis, PROFILE_SCHEMA)

    def test_cannot_pay_when_attempt_state_cannot_be_persisted(self):
        with patch("supysonic.recommendation_openai_profile._save_cache",
                   side_effect=RuntimeError("storage unavailable")), self.request() as request:
            with self.assertRaises(RuntimeError):
                refresh_openai_profile(self.config, self.user)
        request.assert_not_called()

    def test_provider_unavailability_sets_retry_without_creating_empty_profile(self):
        with self.request(side_effect=OpenAIRecommendationError("unavailable")):
            self.assertEqual(refresh_openai_profile(self.config, self.user), "failed")
        self.assertIsNone(get_cached_openai_profile(self.user))
        attempt = RecommendationAgentCache.get(RecommendationAgentCache.context_hash == PROFILE_ATTEMPT_HASH)
        self.assertEqual(attempt.expires_at, self.clock + timedelta(minutes=15))

    def test_refresh_saves_valid_cache_and_read_or_same_interval_never_calls_again(self):
        with self.request() as request:
            self.assertEqual(refresh_openai_profile(self.config, self.user), "updated")
            self.assertEqual(refresh_openai_profile(self.config, self.user), "cached")
            cached = get_cached_openai_profile(self.user)
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["workload"], "profile")
        self.assertEqual(request.call_args.kwargs["max_output_tokens"], 2048)
        self.assertEqual(cached["summary"], self.analysis["summary"])
        self.assertFalse(cached["stale"])
        self.assertEqual(cached["provider"], "openai")
        self.assertEqual(User_Play_Activity.select().count(), 1)
        self.assertIsNone(get_cached_openai_profile(User.get(User.name == "bob")))

    def test_unchanged_fingerprint_extends_cache_without_paid_call(self):
        with self.request() as request:
            refresh_openai_profile(self.config, self.user)
            generated = get_cached_openai_profile(self.user)["generatedAt"]
            self.expire()
            self.assertTrue(get_cached_openai_profile(self.user)["stale"])
            self.assertEqual(refresh_openai_profile(self.config, self.user), "unchanged")
        request.assert_called_once()
        self.assertEqual(get_cached_openai_profile(self.user)["generatedAt"], generated)
        self.assertFalse(get_cached_openai_profile(self.user)["stale"])

    def test_recent_changed_facts_trigger_refresh_only_after_minimum_interval(self):
        with self.request() as request:
            refresh_openai_profile(self.config, self.user)
            User_Play_Activity.create(track=self.track, user=self.user, time=self.clock)
            self.assertEqual(refresh_openai_profile(self.config, self.user), "cached")
            self.expire()
            self.assertEqual(refresh_openai_profile(self.config, self.user), "updated")
        self.assertEqual(request.call_count, 2)

    def test_invalid_result_retains_previous_profile_and_uses_bounded_retry(self):
        with self.request():
            refresh_openai_profile(self.config, self.user)
        previous = get_cached_openai_profile(self.user)
        self.expire()
        User_Play_Activity.create(track=self.track, user=self.user, time=self.clock)
        with self.request(return_value={"summary": "broken"}) as request:
            self.assertEqual(refresh_openai_profile(self.config, self.user), "failed")
            self.assertEqual(refresh_openai_profile(self.config, self.user), "cached")
            self.clock += timedelta(minutes=15)
            self.assertEqual(refresh_openai_profile(self.config, self.user), "failed")
            attempt = RecommendationAgentCache.get(RecommendationAgentCache.context_hash == PROFILE_ATTEMPT_HASH)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(attempt.expires_at, self.clock + timedelta(minutes=30))
        retained = get_cached_openai_profile(self.user)
        self.assertEqual(retained["summary"], previous["summary"])
        self.assertEqual(retained["generatedAt"], previous["generatedAt"])
        self.assertTrue(retained["stale"])

    def test_success_resets_failure_streak(self):
        with self.request(return_value={}):
            self.assertEqual(refresh_openai_profile(self.config, self.user), "failed")
        self.clock += timedelta(minutes=16)
        with self.request():
            self.assertEqual(refresh_openai_profile(self.config, self.user), "updated")
        attempt = RecommendationAgentCache.get(RecommendationAgentCache.context_hash == PROFILE_ATTEMPT_HASH)
        self.assertEqual(json.loads(attempt.payload_json)["failures"], 0)

    def test_unobserved_language_is_rejected(self):
        with self.request(return_value={**self.analysis, "preferredLanguages": ["ko"]}):
            self.assertEqual(refresh_openai_profile(self.config, self.user), "failed")
        self.assertIsNone(get_cached_openai_profile(self.user))

    def test_empty_history_does_not_call_model(self):
        User_Play_Activity.delete().execute()
        with self.request() as request:
            self.assertEqual(refresh_openai_profile(self.config, self.user), "empty")
        request.assert_not_called()
        self.assertIsNone(get_cached_openai_profile(self.user))

    def test_busy_lease_skips_paid_request_and_cache_writes(self):
        self.lease_mock.side_effect = lambda *args, **kwargs: nullcontext(False)
        with self.request() as request:
            self.assertEqual(refresh_openai_profile(self.config, self.user), "busy")
        request.assert_not_called()
        self.assertEqual(RecommendationAgentCache.select().count(), 0)

    def test_disabled_configuration_never_calls_model(self):
        for settings in ({"provider": "mimo"}, {"profile_enabled": False}, {"enabled": False}):
            config = {"RECOMMENDATION_AGENT": {**self.config.RECOMMENDATION_AGENT, **settings}}
            self.assertFalse(openai_profile_enabled(config))
            with self.request() as request:
                self.assertEqual(refresh_openai_profile(config, self.user), "disabled")
            request.assert_not_called()

    def test_background_batch_is_bounded_and_does_not_starve_other_users(self):
        bob = User.get(User.name == "bob")
        User_Play_Activity.create(track=self.track, user=bob, time=self.clock)
        with self.request() as request:
            first = refresh_openai_profiles(self.config)
            second = refresh_openai_profiles(self.config)
            third = refresh_openai_profiles(self.config)
        self.assertEqual(first, {"selected": 1, "updated": 1, "failed": 0, "skipped": 0})
        self.assertEqual(second, first)
        self.assertEqual(third["selected"], 0)
        self.assertEqual(request.call_count, 2)

    def test_malformed_cache_is_ignored_without_request(self):
        with self.request() as request:
            refresh_openai_profile(self.config, self.user)
            RecommendationAgentCache.update(payload_json="not-json").where(
                RecommendationAgentCache.context_hash == PROFILE_CONTEXT_HASH,
            ).execute()
            self.assertIsNone(get_cached_openai_profile(self.user))
        request.assert_called_once()
