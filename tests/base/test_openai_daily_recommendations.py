import json
import os
import unittest
from collections import Counter
from datetime import datetime, timedelta
from unittest.mock import patch

from supysonic.daemon.server import Daemon
from supysonic.db import Album, Artist, Folder, Playlist, Track, TrackMetadata
from supysonic.db import User, User_Play_Activity
from supysonic.openai_recommendations import OpenAIRecommendationError
from supysonic.recommend import (
    RECOMMENDED_PLAYLIST_COMMENT,
    create_recommend_playlist,
    refreshDailyRecommendPlaylists,
)
from supysonic.recommendation_feedback import set_recommendation_feedback
from supysonic.recommendation_openai_daily import (
    DAILY_CANDIDATE_LIMIT,
    DAILY_CONTEXT_MAX_BYTES,
    _validate_ranking,
    select_openai_daily_tracks,
)

from ..testbase import TestBase


class OpenAIDailyRankingValidationTestCase(unittest.TestCase):
    def test_exact_permutation_required(self):
        self.assertEqual(_validate_ranking({"ranked_candidate_ids": [3, 1, 2]}, 3), [3, 1, 2])
        invalid = [
            None, [], {}, {"ranked_candidate_ids": [1, 2]},
            {"ranked_candidate_ids": [1, 2, 2]},
            {"ranked_candidate_ids": [1, 2, 4]},
            {"ranked_candidate_ids": [0, 1, 2]},
            {"ranked_candidate_ids": [True, 2, 3]},
            {"ranked_candidate_ids": ["1", 2, 3]},
            {"ranked_candidate_ids": [1.0, 2, 3]},
            {"ranked_candidate_ids": [{}, 2, 3]},
            {"ranked_candidate_ids": [1, 2, 3], "invented": True},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(OpenAIRecommendationError):
                _validate_ranking(payload, 3)


class OpenAIDailyRecommendationsTestCase(TestBase):
    __with_api__ = True

    def setUp(self):
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.track_number = 0
        self.reference_time = datetime(2026, 11, 14, 8)
        self.config.RECOMMENDATION_AGENT.update({
            "provider": "openai", "daily_selection_enabled": True,
            "model": "gpt-6-luna",
        })
        self.request_patch = patch(
            "supysonic.recommendation_openai_daily.request_recommendation_json",
            side_effect=self._rank_in_order,
        )
        self.request = self.request_patch.start()
        self.addCleanup(self.request_patch.stop)
        self.profile_patch = patch(
            "supysonic.recommendation_openai_profile.get_cached_openai_profile",
            return_value={"summary": "Cached music preference analysis"},
        )
        self.profile_patch.start()
        self.addCleanup(self.profile_patch.stop)
        self.time_patch = patch(
            "supysonic.user_listening_profile.now", return_value=self.reference_time
        )
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.profile_time_patch = patch(
            "supysonic.recommendation_openai_profile.now", return_value=self.reference_time
        )
        self.profile_time_patch.start()
        self.addCleanup(self.profile_time_patch.stop)

    @staticmethod
    def _rank_in_order(config, messages, schema, **kwargs):
        context = json.loads(messages[1]["content"])
        return {"ranked_candidate_ids": [candidate["id"] for candidate in context["candidates"]]}

    def _artist(self, name):
        artist = Artist.create(name=name)
        return artist, Album.create(name=name + " Album", artist=artist)

    def _track(self, title, artist_album=None, language="zh"):
        self.track_number += 1
        artist, album = artist_album or self._artist("Artist " + str(self.track_number))
        track = Track.create(
            title=title, disc=1, number=self.track_number, duration=180,
            has_art=False, artist=artist, album=album, genre="Pop", bitrate=320,
            path=os.path.join("/music", f"openai-daily-{self.track_number}.flac"),
            last_modification=1, root_folder=self.root, folder=self.root,
        )
        TrackMetadata.create(
            track=track, track_last_modification=1, language=language,
            energy=50, mood_json='["calm"]', confidence=0.9,
            provider="llm", source="llm",
        )
        return track

    def _play(self, track, days_ago=1, count=1):
        for _ in range(count):
            User_Play_Activity.create(
                user=self.user, track=track,
                time=self.reference_time - timedelta(days=days_ago),
            )

    def _generate(self, count=50):
        return create_recommend_playlist(
            num_songs=count, user=self.user, day="2026-11-14", config=self.config
        )

    def test_no_history_never_calls_model(self):
        self._track("Candidate")
        self.assertEqual(self._generate(), 0)
        self.request.assert_not_called()

    def test_same_day_playlist_reused_without_call(self):
        existing = Playlist.create(
            user=self.user, name="alice's 2026-11-14 recommend playlist",
            comment=RECOMMENDED_PLAYLIST_COMMENT,
        )
        self.assertEqual(self._generate(), 0)
        self.assertEqual(Playlist.get_by_id(existing.id).name, existing.name)
        self.request.assert_not_called()

    def test_feature_gate_keeps_legacy_selection_nonpaid(self):
        self.config.RECOMMENDATION_AGENT["daily_selection_enabled"] = False
        self._play(self._track("Seed"))
        candidate = self._track("Candidate")
        self.assertEqual(self._generate(1), 1)
        self.assertEqual(Playlist.get(Playlist.user == self.user).tracks, str(candidate.id))
        self.request.assert_not_called()

    def test_real_recent_language_facts_and_cached_analysis_reach_prompt(self):
        self._play(self._track("Old Seed", language="ko"), 90, 15)
        self._play(self._track("Recent Seed", language="zh"), 1, 3)
        self._track("Korean", language="ko")
        self._track("Chinese", language="zh")
        self.assertEqual(self._generate(2), 1)
        args, kwargs = self.request.call_args
        context = json.loads(args[1][1]["content"])
        self.assertEqual(context["statistics"]["realListening"]["lifetime"]["playCount"], 18)
        self.assertEqual(context["statistics"]["realListening"]["recent7Days"]["playCount"], 3)
        self.assertEqual(
            context["statistics"]["metadataCovered"]["recent7Days"]["topLanguages"],
            [{"value": "zh", "playCount": 3}],
        )
        self.assertGreater(
            context["weightedLanguagePreferences"]["zh"],
            context["weightedLanguagePreferences"]["ko"],
        )
        self.assertEqual(context["cachedAnalysis"]["summary"], "Cached music preference analysis")
        self.assertNotIn("alice", args[1][1]["content"])
        self.assertNotIn("/music", args[1][1]["content"])
        self.assertEqual(kwargs["workload"], "daily")
        self.assertEqual(kwargs["max_output_tokens"], 4096)
        self.assertEqual(User_Play_Activity.select().count(), 18)

    def test_model_order_controls_playlist_and_second_generation_is_free(self):
        self._play(self._track("Seed"))
        candidates = [self._track("Candidate " + str(index)) for index in range(3)]
        self.request.side_effect = lambda config, messages, schema, **kwargs: {
            "ranked_candidate_ids": list(reversed(self._rank_in_order(config, messages, schema)["ranked_candidate_ids"]))
        }
        self.assertEqual(self._generate(2), 1)
        context = json.loads(self.request.call_args.args[1][1]["content"])
        expected_titles = [item["title"] for item in reversed(context["candidates"])][:2]
        playlist = Playlist.get(Playlist.user == self.user)
        self.assertEqual([track.title for track in playlist.get_tracks()], expected_titles)
        self.assertTrue(set(playlist.tracks.split(",")).issubset({str(track.id) for track in candidates}))
        self.assertEqual(self._generate(2), 0)
        self.request.assert_called_once()

    def test_provider_failure_preserves_even_expired_playlists(self):
        self._play(self._track("Seed"))
        candidate = self._track("Candidate")
        old = Playlist.create(
            user=self.user, name="alice's 2026-10-01 recommend playlist",
            comment=RECOMMENDED_PLAYLIST_COMMENT, tracks=str(candidate.id),
        )
        self.request.side_effect = OpenAIRecommendationError("provider unavailable")
        with self.assertRaises(OpenAIRecommendationError):
            self._generate(1)
        self.assertEqual(Playlist.select().count(), 1)
        self.assertEqual(Playlist.get_by_id(old.id).tracks, str(candidate.id))

    def test_invalid_output_is_not_persisted_and_six_hour_retry_is_nonpaid(self):
        self._play(self._track("Seed"))
        self._track("Candidate")
        self.request.side_effect = lambda *args, **kwargs: {"ranked_candidate_ids": [999]}
        for _ in range(2):
            with self.assertRaises(OpenAIRecommendationError):
                self._generate(1)
        self.request.assert_called_once()
        self.assertEqual(Playlist.select().count(), 0)

    def test_cached_valid_ranking_avoids_rebilling(self):
        self._play(self._track("Seed"))
        candidates = [self._track("Candidate " + str(index)) for index in range(2)]
        for _ in range(2):
            result = select_openai_daily_tracks(
                self.config, self.user, candidates, 2, "2026-11-14"
            )
            self.assertEqual(len(result), 2)
        self.request.assert_called_once()

    def test_transport_schema_failure_also_delays_paid_retry(self):
        self._play(self._track("Seed"))
        self._track("Candidate")
        self.request.side_effect = OpenAIRecommendationError(
            "invalid JSON", reason="invalid_response"
        )
        for _ in range(2):
            with self.assertRaises(OpenAIRecommendationError):
                self._generate(1)
        self.request.assert_called_once()
        self.assertEqual(Playlist.select().count(), 0)

    def test_unenriched_playbacks_stay_in_real_counts_not_metadata_counts(self):
        seed = self._track("Unenriched Seed")
        TrackMetadata.delete().where(TrackMetadata.track == seed).execute()
        self._play(seed, count=2)
        self._track("Candidate")
        self.assertEqual(self._generate(1), 1)
        context = json.loads(self.request.call_args.args[1][1]["content"])
        statistics = context["statistics"]
        self.assertEqual(statistics["realListening"]["recent7Days"]["playCount"], 2)
        self.assertEqual(statistics["metadataCovered"]["recent7Days"]["playCount"], 0)

    def test_history_and_negative_feedback_are_rechecked_after_model_call(self):
        seed = self._track("Seed")
        self._play(seed)
        blocked = self._track("Already Blocked")
        now_blocked = self._track("Newly Blocked")
        now_heard = self._track("Newly Heard")
        remaining = self._track("Remaining")
        set_recommendation_feedback(self.user, str(blocked.id), "dislike")

        def rank_then_change(config, messages, schema, **kwargs):
            context = json.loads(messages[1]["content"])
            titles = {item["title"] for item in context["candidates"]}
            self.assertNotIn(seed.title, titles)
            self.assertNotIn(blocked.title, titles)
            set_recommendation_feedback(self.user, str(now_blocked.id), "dislike")
            self._play(now_heard, 0)
            return self._rank_in_order(config, messages, schema)

        self.request.side_effect = rank_then_change
        self.assertEqual(self._generate(), 1)
        self.assertEqual(Playlist.get(Playlist.user == self.user).tracks, str(remaining.id))

    def test_deleted_candidates_cannot_enter_playlist(self):
        self._play(self._track("Seed"))
        removed = self._track("Removed")
        remaining = self._track("Remaining")

        def rank_then_delete(config, messages, schema, **kwargs):
            removed.delete_instance(recursive=True)
            return self._rank_in_order(config, messages, schema)

        self.request.side_effect = rank_then_delete
        self.assertEqual(self._generate(), 1)
        self.assertEqual(Playlist.get(Playlist.user == self.user).tracks, str(remaining.id))

    def test_diversity_and_rotation_survive_artist_clustered_model_order(self):
        self._play(self._track("Seed"))
        candidates = []
        recent = set()
        for artist_index in range(10):
            artist = self._artist("Candidate Artist " + str(artist_index))
            for index in range(15):
                track = self._track(f"Candidate {artist_index}-{index}", artist)
                candidates.append(track)
                if index < 3:
                    recent.add(str(track.id))
        result = select_openai_daily_tracks(
            self.config, self.user, candidates, 50, "2026-11-14",
            recent_track_ids=recent, minimum_fresh_tracks=40,
        )
        self.assertEqual(len(result), 50)
        self.assertLessEqual(max(Counter(track.artist_id for track in result).values()), 5)
        self.assertGreaterEqual(sum(str(track.id) not in recent for track in result), 40)

    def test_multibyte_prompt_and_candidate_count_are_bounded(self):
        self._play(self._track("Seed"))
        artist = self._artist("\u6b4c" * 80)
        candidates = [self._track("\u6b4c" * 200, artist) for _ in range(DAILY_CANDIDATE_LIMIT + 1)]
        for metadata in TrackMetadata.select():
            metadata.set_tags(["\u6b4c" * 30] * 3)
            metadata.set_moods(["\u6b4c" * 30] * 3)
            metadata.save()
        select_openai_daily_tracks(self.config, self.user, candidates, 50, "2026-11-14")
        messages = self.request.call_args.args[1]
        context = json.loads(messages[1]["content"])
        self.assertEqual(len(context["candidates"]), DAILY_CANDIDATE_LIMIT)
        self.assertLessEqual(len(messages[1]["content"].encode("utf-8")), DAILY_CONTEXT_MAX_BYTES)

    def test_held_daily_lease_prevents_duplicate_paid_work(self):
        self._play(self._track("Seed"))
        self._track("Candidate")
        with patch("supysonic.openai_recommendations.recommendation_lease") as lease:
            lease.return_value.__enter__.return_value = False
            with self.assertRaises(OpenAIRecommendationError) as raised:
                self._generate()
            self.assertEqual(raised.exception.reason, "busy")
        self.request.assert_not_called()

    def test_daemon_retries_failed_generation_on_next_tick_without_rebilling_success(self):
        self._play(self._track("Seed"))
        self._track("Candidate")
        daemon = Daemon(self.config)
        self.request.side_effect = OpenAIRecommendationError("temporary failure", reason="network")
        with patch("supysonic.daemon.server.open_connection", return_value=False), self.assertLogs(
            level="ERROR"
        ):
            self.assertFalse(daemon._Daemon__refresh_recommend_playlists_if_needed("2026-11-14"))
        self.assertIsNone(daemon._Daemon__lastRecommendRefreshDay)
        self.request.side_effect = self._rank_in_order
        with patch("supysonic.daemon.server.open_connection", return_value=False):
            self.assertTrue(daemon._Daemon__refresh_recommend_playlists_if_needed("2026-11-14"))
            self.assertFalse(daemon._Daemon__refresh_recommend_playlists_if_needed("2026-11-14"))
        self.assertEqual(daemon._Daemon__lastRecommendRefreshDay, "2026-11-14")
        self.assertEqual(self.request.call_count, 2)
        self.assertEqual(Playlist.select().count(), 1)

    def test_daemon_retries_missing_playlist_when_candidates_arrive(self):
        self._play(self._track("Seed"))
        daemon = Daemon(self.config)
        with patch("supysonic.daemon.server.open_connection", return_value=False), self.assertLogs(
            "supysonic.daemon.server", level="ERROR"
        ):
            self.assertFalse(daemon._Daemon__refresh_recommend_playlists_if_needed("2026-11-14"))
        self.request.assert_not_called()
        self.assertIsNone(daemon._Daemon__lastRecommendRefreshDay)
        self._track("New Candidate")
        with patch("supysonic.daemon.server.open_connection", return_value=False):
            self.assertTrue(daemon._Daemon__refresh_recommend_playlists_if_needed("2026-11-14"))
        self.request.assert_called_once()

    def test_batch_continues_after_one_user_failure_but_reports_incomplete(self):
        users = [self.user, User.get(User.name == "bob")]
        playlist = object()
        with patch("supysonic.recommend._getUsersWithPlayActivity", return_value=users), patch(
            "supysonic.recommend._createRecommendPlaylistForUser",
            side_effect=[OpenAIRecommendationError("busy", reason="busy"), (playlist, True)],
        ) as create, self.assertLogs("supysonic.recommend", level="ERROR"):
            with self.assertRaises(OpenAIRecommendationError) as raised:
                refreshDailyRecommendPlaylists(day="2026-11-14", config=self.config)
        self.assertEqual(create.call_count, 2)
        self.assertEqual(raised.exception.reason, "incomplete")
