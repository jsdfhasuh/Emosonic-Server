import json
import os
import unittest

from datetime import timedelta
from unittest.mock import patch

import requests
from flask import current_app

from supysonic.db import (
    Album,
    Artist,
    Folder,
    MusicRequest,
    Playlist,
    RecommendationAgentCache,
    RecommendationAgentSession,
    Track,
    TrackMetadata,
    User,
    User_Play_Activity,
    UserRecommendationFeedback,
    now,
)
from supysonic.recommend import RECOMMENDED_PLAYLIST_COMMENT, getRecommendationDay
from supysonic.recommendation_agent import (
    build_recommendation_agent_context,
    reset_recommendation_agent_health_state,
)
from supysonic.recommendation_feedback import set_recommendation_feedback

from .frontendtestbase import FrontendTestBase


class StubModelResponse:
    def __init__(self, payload, status_code=200, text="", stream_lines=None):
        self.payload = payload
        self.status_code = status_code
        self.text = text
        self.stream_lines = stream_lines or []

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def iter_lines(self, decode_unicode=False):
        for line in self.stream_lines:
            yield line if decode_unicode else line.encode("utf-8")


class RecommendationsTestCase(FrontendTestBase):
    def setUp(self):
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.artist = Artist.create(name="Artist!")
        self.album = Album.create(name="Album!", artist=self.artist)
        self.track = self._create_track("Recommended One", 1, genre="rock")
        self.other_track = self._create_track("Recommended Two", 2, genre="jazz")
        reset_recommendation_agent_health_state()

    def _create_track(
        self,
        title: str,
        number: int,
        genre: str,
        play_count: int = 0,
        artist: Artist = None,
        album: Album = None,
    ) -> Track:
        artist = artist or self.artist
        album = album or self.album
        return Track.create(
            disc=1,
            number=number,
            title=title,
            duration=180 + number,
            has_art=False,
            album=album,
            artist=artist,
            genre=genre,
            bitrate=320,
            play_count=play_count,
            path=os.path.join("/music", f"{number}.flac"),
            last_modification=1,
            root_folder=self.root,
            folder=self.root,
        )

    def _create_recommended_playlist(self, *tracks: Track) -> Playlist:
        playlist = Playlist.create(
            user=self.user,
            name=f"{self.user.name}'s {getRecommendationDay()} recommend playlist",
            comment=RECOMMENDED_PLAYLIST_COMMENT,
        )
        for track in tracks:
            playlist.add(track)
        playlist.save()
        return playlist

    def _enable_recommendation_agent(self, **overrides):
        with self.app_context():
            agent_config = {
                "enabled": True,
                "api_base_url": "https://llm.example/v1",
                "api_key": "test-key",
                "model": "test-model",
                "timeout_seconds": 5,
                "history_limit": 200,
                "max_output_tokens": 900,
                "temperature": 0.7,
            }
            agent_config.update(overrides)
            current_app.config["RECOMMENDATION_AGENT"].update(agent_config)

    def _model_response(self, content):
        return StubModelResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(content),
                        },
                    },
                ],
            }
        )

    def _stream_model_response(self, *content_chunks, status_code=200, payload=None):
        stream_lines = []
        for chunk in content_chunks:
            stream_lines.append(
                "data: "
                + json.dumps(
                    {"choices": [{"delta": {"content": chunk}}]},
                    ensure_ascii=False,
                )
            )
            stream_lines.append("")
        stream_lines.append("data: [DONE]")
        stream_lines.append("")
        return StubModelResponse(
            payload or {},
            status_code=status_code,
            stream_lines=stream_lines,
        )

    def test_recommendations_nav_and_page_show_daily_playlist_tracks(self):
        self._create_recommended_playlist(self.track)

        self._login("alice", "Alic3")
        home = self.client.get("/")
        rv = self.client.get("/recommendations")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Recommendations", home.data)
        self.assertIn("Recommendations", rv.data)
        self.assertIn("Daily", rv.data)
        self.assertIn("Recommended One", rv.data)
        self.assertIn("Artist!", rv.data)
        self.assertIn("Album!", rv.data)
        self.assertIn("Reason", rv.data)
        self.assertIn("Because it adds rock variety", rv.data)
        self.assertIn("AI Agent", rv.data)
        self.assertIn("Daily Recommendations", rv.data)
        self.assertIn("Recommendation Agent", rv.data)
        self.assertIn("/recommendations/agent", rv.data)
        self.assertIn("/recommendations/agent/stream", rv.data)
        self.assertIn("/recommendations/agent/health", rv.data)
        self.assertIn("/recommendations/agent/starter-playlist", rv.data)
        self.assertIn("data-agent-retry", rv.data)
        self.assertIn("data-agent-clear", rv.data)
        self.assertIn("data-agent-stream-url", rv.data)
        self.assertIn("data-agent-health-url", rv.data)
        self.assertIn("data-agent-feedback-url", rv.data)
        self.assertIn("data-agent-starter-url", rv.data)
        self.assertIn("data-agent-health-summary", rv.data)
        self.assertIn("data-agent-health-metrics", rv.data)
        self.assertIn("data-agent-feedback-status", rv.data)
        self.assertIn("data-agent-inline-artist-cards", rv.data)
        self.assertIn("data-agent-artist-card", rv.data)
        self.assertIn("data-agent-artist-action", rv.data)
        self.assertIn("has-artist-cards", rv.data)
        self.assertIn("data-recommendation-row", rv.data)
        self.assertIn("data-recommendation-feedback-url", rv.data)
        self.assertIn("data-recommendation-feedback-action", rv.data)
        self.assertIn("data-feedback-restore-action", rv.data)
        self.assertIn("data-recommendation-feedback-status", rv.data)
        self.assertIn("data-recommendation-why", rv.data)
        self.assertIn("recommendation-row-actions", rv.data)
        self.assertIn("Why?", rv.data)
        self.assertIn("More Like This", rv.data)
        self.assertIn("Less Artist", rv.data)
        self.assertIn("Less Album", rv.data)
        self.assertIn("Less Style", rv.data)
        self.assertIn("data-recommendation-prev", rv.data)
        self.assertIn("data-recommendation-next", rv.data)
        self.assertIn("data-recommendation-page", rv.data)
        self.assertIn("initRecommendationTabs", rv.data)
        self.assertIn("showRecommendationReason", rv.data)
        self.assertIn("normalizedFeedbackTarget", rv.data)
        self.assertIn("toLocaleLowerCase", rv.data)
        self.assertIn("ReadableStream", rv.data)
        self.assertIn("reply_delta", rv.data)
        self.assertIn("previousRecommendedArtists", rv.data)
        self.assertIn("abandonStreamRender", rv.data)
        self.assertIn("isCurrentRequest", rv.data)
        self.assertIn("AbortController", rv.data)
        self.assertIn("recommendation-agent-artist-meta", rv.data)
        self.assertIn("loadAgentHealth", rv.data)
        self.assertIn("agent_average_latency_ms", rv.data)
        self.assertIn("agent_filtered_feedback_artist_count", rv.data)
        self.assertIn("agent_artist_feedback", rv.data)
        self.assertIn("createAgentStarterPlaylist", rv.data)
        self.assertIn("generate_starters", rv.data)
        self.assertIn("not_interested", rv.data)
        self.assertIn("listenLater", rv.data)
        self.assertIn("nextActions", rv.data)

    def test_recommendations_page_shows_agent_listening_profile_summary(self):
        self._create_recommended_playlist(self.track)
        User_Play_Activity.create(track=self.track, user=self.user)
        User_Play_Activity.create(track=self.track, user=self.user)
        TrackMetadata.create(
            track=self.track,
            track_last_modification=self.track.last_modification,
            mood_json=json.dumps(["bright"]),
            scene_json=json.dumps(["commute"]),
            tags_json=json.dumps(["alt-pop"]),
            language="en",
            energy=70,
            valence=60,
            danceability=55,
            confidence=0.9,
            provider="llm",
            source="llm",
        )

        self._login("alice", "Alic3")
        rv = self.client.get("/recommendations")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Agent context", rv.data)
        self.assertIn("bright", rv.data)
        self.assertIn("commute", rv.data)
        self.assertIn("alt-pop", rv.data)
        self.assertIn("average energy", rv.data)

    def test_recommendations_page_embeds_recent_agent_sessions(self):
        RecommendationAgentSession.create(
            user=self.user,
            message="old agent question",
            reply="old agent reply",
            recommended_artists_json=json.dumps(
                [{"name": "Old Session Artist"}]
            ),
            context_summary_json="{}",
            model="test-model",
            language="en",
        )
        self._login("alice", "Alic3")

        rv = self.client.get("/recommendations")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("data-agent-initial-sessions", rv.data)
        self.assertIn("old agent question", rv.data)
        self.assertIn("old agent reply", rv.data)
        self.assertIn("Old Session Artist", rv.data)
        self.assertIn("knownAgentSessions", rv.data)
        self.assertIn("latestSessionRecommendedArtists", rv.data)
        self.assertIn(
            "latestRecommendedArtists = latestSessionRecommendedArtists",
            rv.data,
        )

    def test_recommendation_feedback_endpoint_records_and_restores_web_actions(self):
        self._login("alice", "Alic3")

        hidden = self.client.post(
            "/recommendations/feedback",
            json={
                "id": str(self.artist.id),
                "targetType": "artist",
                "action": "hide_artist",
            },
        )
        restored = self.client.post(
            "/recommendations/feedback",
            json={
                "id": str(self.artist.id),
                "targetType": "artist",
                "action": "restore_artist",
            },
        )

        self.assertEqual(hidden.status_code, 200)
        self.assertTrue(hidden.json["ok"])
        self.assertEqual(hidden.json["feedback"]["targetType"], "artist")
        self.assertEqual(hidden.json["feedback"]["targetId"], str(self.artist.id))
        self.assertEqual(hidden.json["feedback"]["action"], "hide_artist")
        self.assertEqual(restored.status_code, 200)
        feedback = UserRecommendationFeedback.get()
        self.assertEqual(feedback.action, "restore_artist")
        self.assertIsNotNone(feedback.deleted_at)

    def test_recommendation_feedback_endpoint_rejects_invalid_action(self):
        self._login("alice", "Alic3")

        rv = self.client.post(
            "/recommendations/feedback",
            json={
                "id": str(self.track.id),
                "targetType": "song",
                "action": "hide_forever",
            },
        )

        self.assertEqual(rv.status_code, 400)
        self.assertFalse(rv.json["ok"])
        self.assertIn("invalid recommendation feedback action", rv.json["error"])

    def test_recommendation_feedback_endpoint_rejects_invalid_scope(self):
        self._login("alice", "Alic3")

        rv = self.client.post(
            "/recommendations/feedback",
            json={
                "id": str(self.track.id),
                "targetType": "song",
                "action": "dislike_song",
                "scope": "daily_mix",
            },
        )

        self.assertEqual(rv.status_code, 400)
        self.assertFalse(rv.json["ok"])
        self.assertIn("invalid recommendation feedback scope", rv.json["error"])
        self.assertEqual(UserRecommendationFeedback.select().count(), 0)

    def test_recommendation_feedback_endpoint_accepts_target_id_alias(self):
        self._login("alice", "Alic3")

        rv = self.client.post(
            "/recommendations/feedback",
            json={
                "target_id": str(self.track.id),
                "target_type": "song",
                "action": "dislike_song",
            },
        )

        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.json["ok"])
        self.assertEqual(rv.json["feedback"]["targetType"], "song")
        self.assertEqual(rv.json["feedback"]["targetId"], str(self.track.id))
        self.assertEqual(rv.json["feedback"]["target_id"], str(self.track.id))
        feedback = UserRecommendationFeedback.get()
        self.assertEqual(feedback.target_id, str(self.track.id))
        self.assertEqual(feedback.action, "dislike_song")

    def test_recommendation_agent_starter_playlist_creates_local_playlist_when_tracks_match(self):
        local_artist = Artist.create(name="Outside Starter Artist")
        local_album = Album.create(name="Starter Album", artist=local_artist)
        first_track = Track.create(
            disc=1,
            number=10,
            title="First Starter",
            duration=201,
            has_art=False,
            album=local_album,
            artist=local_artist,
            genre="indie",
            bitrate=320,
            path=os.path.join("/music", "starter-1.flac"),
            last_modification=1,
            root_folder=self.root,
            folder=self.root,
        )
        second_track = Track.create(
            disc=1,
            number=11,
            title="Second Starter",
            duration=202,
            has_art=False,
            album=local_album,
            artist=local_artist,
            genre="indie",
            bitrate=320,
            path=os.path.join("/music", "starter-2.flac"),
            last_modification=1,
            root_folder=self.root,
            folder=self.root,
        )
        self._login("alice", "Alic3")

        rv = self.client.post(
            "/recommendations/agent/starter-playlist",
            json={
                "artistName": "Outside Starter Artist",
                "starterTracks": [
                    "Second Starter",
                    "Missing Starter",
                    "First Starter",
                    "First Starter",
                ],
            },
        )

        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.json["ok"])
        self.assertEqual(rv.json["mode"], "playlist")
        self.assertFalse(rv.json["reused"])
        self.assertEqual(rv.json["playlist"]["trackCount"], 2)
        self.assertIn("/playlist/", rv.json["playlist"]["url"])
        playlist = Playlist.get_by_id(rv.json["playlist"]["id"])
        self.assertEqual(playlist.user.name, "alice")
        self.assertEqual(playlist.name, "Outside Starter Artist starter playlist")
        self.assertEqual(playlist.comment, "Recommendation Agent starter playlist")
        self.assertEqual(
            [track.id for track in playlist.get_tracks()],
            [second_track.id, first_track.id],
        )
        self.assertEqual(MusicRequest.select().count(), 0)

        duplicate = self.client.post(
            "/recommendations/agent/starter-playlist",
            json={
                "artistName": "Outside Starter Artist",
                "starterTracks": [
                    "Second Starter",
                    "Missing Starter",
                    "First Starter",
                    "First Starter",
                ],
            },
        )

        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json["reused"])
        self.assertEqual(duplicate.json["playlist"]["id"], rv.json["playlist"]["id"])
        self.assertEqual(Playlist.select().count(), 1)
        self.assertEqual(MusicRequest.select().count(), 0)

    def test_recommendation_agent_starter_playlist_creates_music_request_for_outside_tracks(self):
        self._login("alice", "Alic3")

        rv = self.client.post(
            "/recommendations/agent/starter-playlist",
            json={
                "artistName": "Outside Request Artist",
                "starterTracks": ["Request Song A", "Request Song B", "Request Song A"],
            },
        )

        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.json["ok"])
        self.assertEqual(rv.json["mode"], "music_request")
        self.assertFalse(rv.json["reused"])
        self.assertEqual(rv.json["musicRequest"]["trackCount"], 2)
        self.assertEqual(rv.json["musicRequest"]["url"], "/music-requests")
        record = MusicRequest.get_by_id(rv.json["musicRequest"]["id"])
        self.assertEqual(record.user.name, "alice")
        self.assertEqual(record.artist_name, "Outside Request Artist")
        self.assertIsNone(record.album_name)
        self.assertEqual(record.get_track_titles(), ["Request Song A", "Request Song B"])
        self.assertEqual(
            record.note,
            "Created from Recommendation Agent starter playlist action.",
        )
        self.assertEqual(Playlist.select().count(), 0)

        duplicate = self.client.post(
            "/recommendations/agent/starter-playlist",
            json={
                "artistName": "Outside Request Artist",
                "starterTracks": ["Request Song A", "Request Song B", "Request Song A"],
            },
        )

        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json["reused"])
        self.assertEqual(
            duplicate.json["musicRequest"]["id"],
            rv.json["musicRequest"]["id"],
        )
        self.assertEqual(MusicRequest.select().count(), 1)
        self.assertEqual(Playlist.select().count(), 0)

    def test_recommendation_agent_starter_playlist_validates_required_fields(self):
        self._login("alice", "Alic3")

        missing_artist = self.client.post(
            "/recommendations/agent/starter-playlist",
            json={"starterTracks": ["Song"]},
        )
        missing_tracks = self.client.post(
            "/recommendations/agent/starter-playlist",
            json={"artistName": "Outside"},
        )

        self.assertEqual(missing_artist.status_code, 400)
        self.assertFalse(missing_artist.json["ok"])
        self.assertIn("artist name is required", missing_artist.json["error"])
        self.assertEqual(missing_tracks.status_code, 400)
        self.assertFalse(missing_tracks.json["ok"])
        self.assertIn("starter tracks are required", missing_tracks.json["error"])
        self.assertEqual(Playlist.select().count(), 0)
        self.assertEqual(MusicRequest.select().count(), 0)

    def test_recommendations_filter_disliked_tracks(self):
        self._create_recommended_playlist(self.track, self.other_track)
        backfill_track = self._create_track(
            "Recommended Backfill",
            3,
            genre="rock",
            play_count=8,
        )
        set_recommendation_feedback(self.user, str(self.track.id), "dislike")

        self._login("alice", "Alic3")
        rv = self.client.get("/recommendations?count=2")

        self.assertEqual(rv.status_code, 200)
        self.assertNotIn("Recommended One", rv.data)
        self.assertIn("Recommended Two", rv.data)
        self.assertIn(backfill_track.title, rv.data)

    def test_recommendations_backfill_from_like_more_seed(self):
        self._create_recommended_playlist(self.track, self.other_track)
        seed_artist = Artist.create(name="Web Seed Artist")
        seed_album = Album.create(name="Web Seed Album", artist=seed_artist)
        seed_track = self._create_track(
            "Web Liked Seed",
            3,
            genre="ambient",
            artist=seed_artist,
            album=seed_album,
        )
        similar_track = self._create_track(
            "Web Similar Seed",
            4,
            genre="ambient",
            play_count=10,
            artist=seed_artist,
            album=seed_album,
        )
        popular_fallback = self._create_track(
            "Web Popular Fallback",
            5,
            genre="pop",
            play_count=100,
        )
        set_recommendation_feedback(self.user, str(self.track.id), "dislike")
        set_recommendation_feedback(self.user, str(seed_track.id), "like_more")

        self._login("alice", "Alic3")
        rv = self.client.get("/recommendations?count=2")

        self.assertEqual(rv.status_code, 200)
        self.assertNotIn("Recommended One", rv.data)
        self.assertIn("Recommended Two", rv.data)
        self.assertIn(similar_track.title, rv.data)
        self.assertNotIn(popular_fallback.title, rv.data)

    def test_recommendations_fall_back_to_library_tracks_without_playlist(self):
        self._login("alice", "Alic3")
        rv = self.client.get("/recommendations?count=1")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Random", rv.data)
        self.assertTrue(
            "Recommended One" in rv.data or "Recommended Two" in rv.data
        )

    def test_recommendation_agent_requires_configured_model(self):
        self._login("alice", "Alic3")
        rv = self.client.get("/recommendations/agent?lang=en")

        self.assertEqual(rv.status_code, 503)
        self.assertFalse(rv.json["ok"])
        self.assertEqual(
            rv.json["errorCode"],
            "recommendation_agent_not_configured",
        )

    def test_recommendation_agent_health_reports_configuration_state(self):
        self._login("alice", "Alic3")

        rv = self.client.get("/recommendations/agent/health")

        self.assertEqual(rv.status_code, 200)
        self.assertFalse(rv.json["enabled"])
        self.assertFalse(rv.json["configured"])
        self.assertEqual(rv.json["model"], "")
        self.assertEqual(rv.json["apiBaseUrl"], "https://api.openai.com/v1")
        self.assertEqual(rv.json["lastSuccessAt"], "")
        self.assertIsNone(rv.json["lastError"])
        self.assertEqual(rv.json["metrics"]["agent_request_count"], 0)
        self.assertEqual(rv.json["metrics"]["agent_success_count"], 0)
        self.assertEqual(rv.json["metrics"]["agent_error_count"], 0)
        self.assertEqual(rv.json["metrics"]["agent_timeout_count"], 0)

        self._enable_recommendation_agent()
        rv = self.client.get("/recommendations/agent/health")

        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.json["enabled"])
        self.assertTrue(rv.json["configured"])
        self.assertEqual(rv.json["model"], "test-model")
        self.assertEqual(rv.json["apiBaseUrl"], "https://llm.example/v1")

    def test_recommendation_agent_health_tracks_success_and_error(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = self._model_response(
                {
                    "reply": "Healthy response.",
                    "recommendedArtists": [],
                }
            )
            success = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Check health success"},
            )

        self.assertEqual(success.status_code, 200)
        health = self.client.get("/recommendations/agent/health")
        self.assertTrue(health.json["lastSuccessAt"])
        self.assertIsNone(health.json["lastError"])
        self.assertEqual(health.json["metrics"]["agent_request_count"], 1)
        self.assertEqual(health.json["metrics"]["agent_success_count"], 1)
        self.assertEqual(health.json["metrics"]["agent_error_count"], 0)
        self.assertEqual(health.json["metrics"]["agent_empty_result_count"], 1)
        self.assertGreaterEqual(health.json["metrics"]["agent_latency_ms"], 0)
        self.assertGreaterEqual(
            health.json["metrics"]["agent_payload_size_bytes"],
            1,
        )

        with patch(
            "supysonic.recommendation_agent.requests.post",
            side_effect=requests.exceptions.Timeout("slow model"),
        ):
            failure = self.client.post(
                "/recommendations/agent",
                json={
                    "language": "en",
                    "message": "Check health failure",
                    "forceRefresh": True,
                },
            )

        self.assertEqual(failure.status_code, 504)
        health = self.client.get("/recommendations/agent/health")
        self.assertTrue(health.json["lastSuccessAt"])
        self.assertEqual(
            health.json["lastError"]["errorCode"],
            "recommendation_agent_timeout",
        )
        self.assertIn("timed out", health.json["lastError"]["message"])
        self.assertEqual(health.json["metrics"]["agent_request_count"], 2)
        self.assertEqual(health.json["metrics"]["agent_success_count"], 1)
        self.assertEqual(health.json["metrics"]["agent_error_count"], 1)
        self.assertEqual(health.json["metrics"]["agent_timeout_count"], 1)

    def test_recommendation_agent_metrics_track_cache_filtering_and_logs(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = self._model_response(
                {
                    "reply": "Metrics response.",
                    "recommendedArtists": [
                        {
                            "name": "Outside Metrics",
                            "reason": "It matches your listening.",
                            "genres": ["art pop"],
                            "starterTracks": ["Metric Song"],
                        },
                        {
                            "name": "artist!",
                            "reason": "Already local and should be filtered.",
                            "genres": ["rock"],
                            "starterTracks": ["Local Song"],
                        },
                    ],
                }
            )
            with self.assertLogs(
                "supysonic.recommendation_agent",
                level="INFO",
            ) as logs:
                first = self.client.post(
                    "/recommendations/agent",
                    json={"language": "en", "message": "Find metrics artists"},
                )
                second = self.client.post(
                    "/recommendations/agent",
                    json={"language": "en", "message": "Find metrics artists"},
                )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        post.assert_called_once()
        self.assertFalse(first.json["cache"]["hit"])
        self.assertTrue(second.json["cache"]["hit"])

        logged_metrics = "\n".join(logs.output)
        self.assertIn("recommendation_agent_metrics status=success", logged_metrics)
        self.assertIn("agent_cache_hit_count=1", logged_metrics)
        self.assertIn("agent_payload_size_bytes=", logged_metrics)
        self.assertIn("agent_filtered_local_artist_count=", logged_metrics)
        self.assertIn("agent_filtered_feedback_artist_count=", logged_metrics)

        health = self.client.get("/recommendations/agent/health")
        metrics = health.json["metrics"]
        self.assertEqual(metrics["agent_request_count"], 2)
        self.assertEqual(metrics["agent_success_count"], 2)
        self.assertEqual(metrics["agent_error_count"], 0)
        self.assertEqual(metrics["agent_cache_hit_count"], 1)
        self.assertEqual(metrics["agent_filtered_local_artist_count"], 1)
        self.assertEqual(metrics["agent_last_filtered_local_artist_count"], 0)
        self.assertEqual(metrics["agent_filtered_feedback_artist_count"], 0)
        self.assertEqual(metrics["agent_last_filtered_feedback_artist_count"], 0)
        self.assertEqual(metrics["agent_empty_result_count"], 0)
        self.assertGreaterEqual(metrics["agent_average_latency_ms"], 0)

    def test_recommendation_agent_calls_llm_with_play_context_and_filters_library_artists(self):
        self._enable_recommendation_agent()
        Artist.create(name="G.E.M.")
        User_Play_Activity.create(track=self.track, user=self.user)
        User_Play_Activity.create(track=self.track, user=self.user)
        TrackMetadata.create(
            track=self.track,
            track_last_modification=self.track.last_modification,
            mood_json='["bright"]',
            scene_json='["commute"]',
            tags_json='["alt-pop"]',
            summary="A bright local favorite.",
            energy=70,
            valence=80,
            danceability=60,
            confidence=0.9,
            provider="llm",
            source="llm",
        )
        TrackMetadata.create(
            track=self.other_track,
            track_last_modification=self.other_track.last_modification,
            mood_json='["focused"]',
            scene_json='["work"]',
            tags_json='["jazz"]',
            summary="A focused current recommendation.",
            energy=55,
            valence=65,
            danceability=50,
            confidence=0.8,
            provider="llm",
            source="llm",
        )

        self._login("alice", "Alic3")
        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = self._model_response(
                {
                    "reply": "Try Outside Artist and skip local artists.",
                    "recommendedArtists": [
                        {
                            "name": "Outside Artist",
                            "reason": "It expands from your rock listening.",
                            "genres": ["rock", "alt-pop"],
                            "starterTracks": ["First Song"],
                            "similarTo": ["Artist!"],
                            "confidence": 0.84,
                            "mood": ["melodic", "bright"],
                        },
                        {
                            "name": "artist!",
                            "reason": "Already local and should be filtered.",
                            "genres": ["rock"],
                            "starterTracks": ["Local Song"],
                        },
                        {
                            "name": "邓紫棋 (G.E.M.)",
                            "reason": "Already local through an alias and should be filtered.",
                            "genres": ["C-pop"],
                            "starterTracks": ["Local Alias Song"],
                        },
                    ],
                    "nextActions": ["Generate a starter playlist"],
                }
            )
            rv = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Find outside artists"},
            )

        self.assertEqual(rv.status_code, 200)
        post.assert_called_once()
        self.assertEqual(
            post.call_args.args[0],
            "https://llm.example/v1/chat/completions",
        )
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer test-key",
        )
        request_payload = post.call_args.kwargs["json"]
        self.assertEqual(request_payload["model"], "test-model")
        self.assertEqual(request_payload["max_tokens"], 900)
        self.assertIn(
            "outside the user's local music library",
            request_payload["messages"][0]["content"],
        )
        self.assertIn("context.listeningProfile", request_payload["messages"][0]["content"])
        self.assertIn("topMoods", request_payload["messages"][0]["content"])
        self.assertIn("topScenes", request_payload["messages"][0]["content"])
        self.assertIn("topTags", request_payload["messages"][0]["content"])
        self.assertIn("topLanguages", request_payload["messages"][0]["content"])
        self.assertIn("averageEnergy", request_payload["messages"][0]["content"])
        self.assertIn(
            "do not invent semantic metadata",
            request_payload["messages"][0]["content"],
        )
        self.assertIn("similarTo", request_payload["messages"][0]["content"])
        self.assertIn("confidence", request_payload["messages"][0]["content"])
        self.assertIn("nextActions", request_payload["messages"][0]["content"])
        prompt_payload = json.loads(request_payload["messages"][1]["content"])
        self.assertEqual(prompt_payload["userMessage"], "Find outside artists")
        self.assertIn(
            "Recommended One",
            json.dumps(prompt_payload["context"]["playHistory"], ensure_ascii=False),
        )
        play_history_metadata = prompt_payload["context"]["playHistory"][0][
            "semanticMetadata"
        ]
        self.assertEqual(play_history_metadata["mood"], ["bright"])
        self.assertEqual(play_history_metadata["scene"], ["commute"])
        self.assertEqual(play_history_metadata["tags"], ["alt-pop"])
        listening_profile = prompt_payload["context"]["listeningProfile"]
        self.assertEqual(listening_profile["playCount"], 2)
        self.assertEqual(
            listening_profile["topMoods"],
            [{"value": "bright", "playCount": 2}],
        )
        self.assertEqual(listening_profile["averageEnergy"], 70.0)
        current_track_metadata = prompt_payload["context"][
            "currentRecommendationTracks"
        ][0]["semanticMetadata"]
        self.assertIn("summary", current_track_metadata)
        self.assertIn("Artist!", prompt_payload["context"]["libraryArtists"])
        self.assertIn("G.E.M.", prompt_payload["context"]["libraryArtists"])

        payload = rv.json
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["agent"]["mode"], "llm")
        self.assertIn("filtered them out", payload["reply"])
        self.assertEqual(
            [artist["name"] for artist in payload["recommendedArtists"]],
            ["Outside Artist"],
        )
        artist = payload["recommendedArtists"][0]
        self.assertEqual(artist["similarTo"], ["Artist!"])
        self.assertEqual(artist["confidence"], 0.84)
        self.assertEqual(artist["mood"], ["melodic", "bright"])
        self.assertEqual(payload["nextActions"], ["Generate a starter playlist"])
        self.assertEqual(
            payload["agentSession"]["recommendedArtists"][0]["similarTo"],
            ["Artist!"],
        )

    def test_recommendation_agent_context_omits_low_quality_semantic_metadata(self):
        User_Play_Activity.create(track=self.track, user=self.user)
        TrackMetadata.create(
            track=self.track,
            track_last_modification=self.track.last_modification,
            mood_json='["local-mood"]',
            scene_json='["local-scene"]',
            tags_json='["local-tag"]',
            energy=30,
            valence=40,
            danceability=50,
            confidence=0.25,
            provider="local",
            source="local",
        )
        TrackMetadata.create(
            track=self.other_track,
            track_last_modification=self.other_track.last_modification,
            mood_json='["low-confidence"]',
            scene_json='["work"]',
            tags_json='["jazz"]',
            energy=55,
            valence=65,
            danceability=50,
            confidence=0.2,
            provider="llm",
            source="llm",
        )

        context = build_recommendation_agent_context(
            self.user,
            [self.other_track],
            {},
            history_limit=10,
        )

        self.assertNotIn("semanticMetadata", context["playHistory"][0])
        self.assertNotIn(
            "semanticMetadata",
            context["currentRecommendationTracks"][0],
        )

    def test_recommendation_agent_suggested_prompts_include_style_and_obscure_followups(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.side_effect = [
                self._model_response(
                    {
                        "reply": "English prompts.",
                        "recommendedArtists": [],
                    }
                ),
                self._model_response(
                    {
                        "reply": "Chinese prompts.",
                        "recommendedArtists": [],
                    }
                ),
            ]
            english = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Prompt options en"},
            )
            chinese = self.client.post(
                "/recommendations/agent",
                json={"language": "zh", "message": "Prompt options zh"},
            )

        self.assertEqual(english.status_code, 200)
        self.assertEqual(chinese.status_code, 200)
        self.assertIn("Try a different style", english.json["suggestedPrompts"])
        self.assertIn(
            "Recommend more obscure artists",
            english.json["suggestedPrompts"],
        )
        self.assertIn("换一种风格", chinese.json["suggestedPrompts"])
        self.assertIn("推荐更冷门的", chinese.json["suggestedPrompts"])

    def test_recommendation_agent_sanitizes_enhanced_output_fields(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = self._model_response(
                {
                    "reply": "Clean these fields.",
                    "recommendedArtists": [
                        {
                            "name": " Outside Clean Artist ",
                            "reason": "  It matches the cleaned structure. ",
                            "genres": [
                                "dream pop",
                                "",
                                123,
                                "indie",
                                "ambient",
                                "folk",
                                "electronic",
                                "extra",
                            ],
                            "starterTracks": [
                                " First ",
                                "",
                                "Second",
                                "Third",
                                "Fourth",
                                "Fifth",
                                "Sixth",
                                "Seventh",
                                "Eighth",
                                "Ninth",
                            ],
                            "similarTo": [
                                " Artist! ",
                                "",
                                "Local Two",
                                "Local Three",
                                "Local Four",
                                "Local Five",
                                "Local Six",
                                "Local Seven",
                            ],
                            "confidence": 2.4,
                            "mood": [
                                " melodic ",
                                "",
                                "bright",
                                "late night",
                                "warm",
                                "soft",
                                "focused",
                                "extra",
                            ],
                        },
                    ],
                    "nextActions": [
                        " Generate starter tracks ",
                        "",
                        "Try a different style",
                        "Recommend more obscure artists",
                        "Explain this pick",
                        "Extra action",
                    ],
                }
            )
            rv = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Clean enhanced fields"},
            )

        self.assertEqual(rv.status_code, 200)
        artist = rv.json["recommendedArtists"][0]
        self.assertEqual(artist["name"], "Outside Clean Artist")
        self.assertEqual(artist["reason"], "It matches the cleaned structure.")
        self.assertEqual(
            artist["genres"],
            ["dream pop", "123", "indie", "ambient", "folk", "electronic"],
        )
        self.assertEqual(
            artist["starterTracks"],
            [
                "First",
                "Second",
                "Third",
                "Fourth",
                "Fifth",
                "Sixth",
                "Seventh",
                "Eighth",
            ],
        )
        self.assertEqual(
            artist["similarTo"],
            [
                "Artist!",
                "Local Two",
                "Local Three",
                "Local Four",
                "Local Five",
                "Local Six",
            ],
        )
        self.assertEqual(artist["confidence"], 1.0)
        self.assertEqual(
            artist["mood"],
            ["melodic", "bright", "late night", "warm", "soft", "focused"],
        )
        self.assertEqual(
            rv.json["nextActions"],
            [
                "Generate starter tracks",
                "Try a different style",
                "Recommend more obscure artists",
                "Explain this pick",
            ],
        )

    def test_recommendation_agent_filters_hidden_feedback_artists_and_refreshes_cache(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.side_effect = [
                self._model_response(
                    {
                        "reply": "Try Hidden Agent Artist.",
                        "recommendedArtists": [
                            {
                                "name": "Hidden Agent Artist",
                                "reason": "It initially matches your listening.",
                                "genres": ["indie"],
                                "starterTracks": ["Hidden Song"],
                            },
                        ],
                    }
                ),
                self._model_response(
                    {
                        "reply": "Try Hidden Agent Artist and Fresh Feedback Artist.",
                        "recommendedArtists": [
                            {
                                "name": "Hidden Agent Artist",
                                "reason": "This should be filtered by feedback.",
                                "genres": ["indie"],
                                "starterTracks": ["Hidden Song"],
                            },
                            {
                                "name": "Fresh Feedback Artist",
                                "reason": "This is still eligible.",
                                "genres": ["dream pop"],
                                "starterTracks": ["Fresh Song"],
                            },
                        ],
                    }
                ),
            ]
            first = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Find feedback artists"},
            )
            set_recommendation_feedback(
                self.user,
                "Hidden Agent Artist",
                "hide_artist",
                target_type="artist",
                reason="agent_artist_feedback",
                source="web_agent",
            )
            second = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Find feedback artists"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertFalse(first.json["cache"]["hit"])
        self.assertFalse(second.json["cache"]["hit"])

        second_prompt = json.loads(
            post.call_args_list[1].kwargs["json"]["messages"][1]["content"]
        )
        self.assertEqual(
            second_prompt["context"]["recommendationFeedback"]["hiddenArtistNames"],
            ["Hidden Agent Artist"],
        )
        self.assertIn("hidden by the user", post.call_args_list[1].kwargs["json"]["messages"][0]["content"])
        self.assertIn("filtered them out", second.json["reply"])
        self.assertEqual(
            [artist["name"] for artist in second.json["recommendedArtists"]],
            ["Fresh Feedback Artist"],
        )
        health = self.client.get("/recommendations/agent/health")
        self.assertEqual(
            health.json["metrics"]["agent_filtered_feedback_artist_count"],
            1,
        )
        self.assertEqual(
            health.json["metrics"]["agent_last_filtered_feedback_artist_count"],
            1,
        )

    def test_recommendation_agent_resolves_hidden_artist_ids_to_names(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")
        set_recommendation_feedback(
            self.user,
            str(self.artist.id),
            "hide_artist",
            target_type="artist",
            reason="web_recommendation_feedback",
            source="web",
        )

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = self._model_response(
                {
                    "reply": "Resolved hidden artist ids.",
                    "recommendedArtists": [],
                }
            )
            rv = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Find artist id feedback"},
            )

        self.assertEqual(rv.status_code, 200)
        prompt_payload = json.loads(
            post.call_args.kwargs["json"]["messages"][1]["content"]
        )
        hidden_names = prompt_payload["context"]["recommendationFeedback"][
            "hiddenArtistNames"
        ]
        self.assertEqual(hidden_names, ["Artist!"])
        self.assertNotIn(str(self.artist.id), hidden_names)

    def test_recommendation_agent_filters_long_hidden_agent_artist_name(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")
        long_artist_name = "Long Agent " + ("A" * 109)

        feedback = self.client.post(
            "/recommendations/feedback",
            json={
                "action": "hide_artist",
                "targetType": "artist",
                "targetId": long_artist_name,
                "reason": "agent_artist_feedback",
                "source": "web_agent",
            },
        )

        self.assertEqual(feedback.status_code, 200)
        self.assertTrue(feedback.json["ok"])
        self.assertEqual(feedback.json["feedback"]["targetId"], long_artist_name)

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = self._model_response(
                {
                    "reply": "Try the long hidden artist and a fresh one.",
                    "recommendedArtists": [
                        {
                            "name": long_artist_name,
                            "reason": "This should be hidden by exact feedback.",
                            "genres": ["ambient"],
                            "starterTracks": ["Hidden Long Song"],
                        },
                        {
                            "name": "Fresh Long Filter Artist",
                            "reason": "Still allowed.",
                            "genres": ["ambient"],
                            "starterTracks": ["Fresh Song"],
                        },
                    ],
                }
            )
            rv = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Find long feedback artists"},
            )

        self.assertEqual(rv.status_code, 200)
        prompt_payload = json.loads(
            post.call_args.kwargs["json"]["messages"][1]["content"]
        )
        self.assertEqual(
            prompt_payload["context"]["recommendationFeedback"]["hiddenArtistNames"],
            [long_artist_name],
        )
        self.assertEqual(
            [artist["name"] for artist in rv.json["recommendedArtists"]],
            ["Fresh Long Filter Artist"],
        )

    def test_recommendation_agent_omits_max_tokens_when_configured_zero(self):
        self._enable_recommendation_agent(max_output_tokens=0)
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = self._model_response(
                {
                    "reply": "Unlimited provider output budget.",
                    "recommendedArtists": [],
                }
            )
            rv = self.client.get("/recommendations/agent?lang=en")

        self.assertEqual(rv.status_code, 200)
        request_payload = post.call_args.kwargs["json"]
        self.assertNotIn("max_tokens", request_payload)

    def test_recommendation_agent_sends_previous_artists_for_starter_track_followup(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = self._model_response(
                {
                    "reply": "Here are starter tracks for the previous artists.",
                    "recommendedArtists": [
                        {
                            "name": "Outside Previous",
                            "reason": "Same artist, expanded with starter songs.",
                            "genres": ["indie pop"],
                            "starterTracks": ["Song A", "Song B", "Song C"],
                        },
                    ],
                }
            )
            rv = self.client.post(
                "/recommendations/agent",
                json={
                    "language": "en",
                    "message": "Give me starter tracks",
                    "previousRecommendedArtists": [
                        {
                            "name": "Outside Previous",
                            "reason": "Recommended earlier.",
                            "genres": ["indie pop"],
                            "starterTracks": ["Old Song"],
                        }
                    ],
                },
            )

        self.assertEqual(rv.status_code, 200)
        request_payload = post.call_args.kwargs["json"]
        self.assertIn(
            "previousRecommendedArtists",
            json.loads(request_payload["messages"][1]["content"])["context"],
        )
        self.assertIn(
            "starter tracks",
            request_payload["messages"][0]["content"],
        )
        prompt_payload = json.loads(request_payload["messages"][1]["content"])
        self.assertEqual(
            prompt_payload["context"]["previousRecommendedArtists"][0]["name"],
            "Outside Previous",
        )
        self.assertEqual(
            rv.json["recommendedArtists"][0]["starterTracks"],
            ["Song A", "Song B", "Song C"],
        )

    def test_recommendation_agent_saves_session_and_uses_recent_session_for_followup(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.side_effect = [
                self._model_response(
                    {
                        "reply": "Try Session Artist.",
                        "recommendedArtists": [
                            {
                                "name": "Session Artist",
                                "reason": "It matches your recent listening.",
                                "genres": ["dream pop"],
                                "starterTracks": ["First Session Song"],
                            },
                        ],
                    }
                ),
                self._model_response(
                    {
                        "reply": "These were recommended because of your listening context.",
                        "recommendedArtists": [
                            {
                                "name": "Session Artist",
                                "reason": "Still the same artist from the last session.",
                                "genres": ["dream pop"],
                                "starterTracks": [
                                    "First Session Song",
                                    "Second Session Song",
                                ],
                            },
                        ],
                    }
                ),
            ]
            first = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Find a new artist"},
            )
            RecommendationAgentSession.create(
                user=self.user,
                message="Empty follow-up separator",
                reply="No artists this time.",
                recommended_artists_json="[]",
                context_summary_json="{}",
                model="test-model",
                language="en",
                created_at=now() + timedelta(seconds=1),
            )
            second = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Why these?"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(RecommendationAgentSession.select().count(), 3)
        self.assertEqual(
            first.json["agentSession"]["recommendedArtists"][0]["name"],
            "Session Artist",
        )
        second_prompt = json.loads(
            post.call_args_list[1].kwargs["json"]["messages"][1]["content"]
        )
        second_context = second_prompt["context"]
        self.assertEqual(second_prompt["userMessage"], "Why these?")
        self.assertEqual(
            second_context["recentAgentSessions"][0]["message"],
            "Find a new artist",
        )
        self.assertEqual(
            second_context["recentAgentSessions"][1]["message"],
            "Empty follow-up separator",
        )
        self.assertEqual(
            second_context["recentAgentSessions"][0]["recommendedArtists"][0]["name"],
            "Session Artist",
        )
        self.assertEqual(
            second_context["previousRecommendedArtists"][0]["name"],
            "Session Artist",
        )
        self.assertEqual(len(second.json["agentSessions"]), 3)

    def test_recommendation_agent_session_summary_includes_recommendation_tracks(self):
        self._enable_recommendation_agent()
        self._create_recommended_playlist(self.track, self.other_track)
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = self._model_response(
                {
                    "reply": "Session context saved.",
                    "recommendedArtists": [
                        {
                            "name": "Context Summary Artist",
                            "reason": "It matches the saved recommendation context.",
                            "genres": ["indie"],
                            "starterTracks": ["Context Song"],
                        },
                    ],
                }
            )
            rv = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Save session context"},
            )

        self.assertEqual(rv.status_code, 200)
        post.assert_called_once()
        context_summary = rv.json["agentSession"]["contextSummary"]
        track_titles = {
            track["title"]
            for track in context_summary["currentRecommendationTracks"]
        }
        self.assertEqual(context_summary["recommendationSummary"]["trackCount"], 2)
        self.assertIn("Recommended One", track_titles)
        self.assertIn("Recommended Two", track_titles)
        self.assertEqual(
            rv.json["agentSessions"][0]["contextSummary"][
                "currentRecommendationTracks"
            ][0]["id"],
            context_summary["currentRecommendationTracks"][0]["id"],
        )

    def test_recommendation_agent_uses_recent_session_for_next_action_followups(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        followups = (
            ("en", "Try a different style"),
            ("en", "Recommend more obscure artists"),
            ("en", "What about the last recommendation?"),
            ("zh", "换一种风格"),
            ("zh", "推荐更冷门的"),
            ("zh", "上一轮推荐呢？"),
        )
        for language, message in followups:
            with self.subTest(message=message):
                RecommendationAgentSession.delete().where(
                    RecommendationAgentSession.user == self.user
                ).execute()
                RecommendationAgentCache.delete().where(
                    RecommendationAgentCache.user == self.user
                ).execute()
                RecommendationAgentSession.create(
                    user=self.user,
                    message="Find a seed artist",
                    reply="Try Prompt Button Artist.",
                    recommended_artists_json=json.dumps(
                        [
                            {
                                "name": "Prompt Button Artist",
                                "reason": "Seeded from the previous session.",
                                "genres": ["art pop"],
                                "starterTracks": ["Prompt Song"],
                            }
                        ]
                    ),
                    context_summary_json="{}",
                    model="test-model",
                    language=language,
                    created_at=now(),
                )

                with patch("supysonic.recommendation_agent.requests.post") as post:
                    post.return_value = self._model_response(
                        {
                            "reply": "Using the previous session as reference.",
                            "recommendedArtists": [
                                {
                                    "name": "Followup Result Artist",
                                    "reason": "A fresh direction from the prior pick.",
                                    "genres": ["leftfield pop"],
                                    "starterTracks": ["Followup Song"],
                                }
                            ],
                        }
                    )
                    rv = self.client.post(
                        "/recommendations/agent",
                        json={"language": language, "message": message},
                    )

                self.assertEqual(rv.status_code, 200)
                request_payload = post.call_args.kwargs["json"]
                prompt_payload = json.loads(
                    request_payload["messages"][1]["content"]
                )
                self.assertEqual(prompt_payload["userMessage"], message)
                self.assertEqual(
                    prompt_payload["context"]["previousRecommendedArtists"][0]["name"],
                    "Prompt Button Artist",
                )

    def test_recommendation_agent_clear_session_endpoint_removes_user_sessions(self):
        bob = User.get(User.name == "bob")
        RecommendationAgentSession.create(
            user=self.user,
            message="old question",
            reply="old reply",
            recommended_artists_json="[]",
            context_summary_json="{}",
            model="test-model",
            language="en",
        )
        RecommendationAgentCache.create(
            user=self.user,
            context_hash="a" * 64,
            message="old question",
            language="en",
            model="test-model",
            payload_json="{}",
            expires_at=now(),
        )
        RecommendationAgentSession.create(
            user=bob,
            message="bob question",
            reply="bob reply",
            recommended_artists_json="[]",
            context_summary_json="{}",
            model="test-model",
            language="en",
        )
        RecommendationAgentCache.create(
            user=bob,
            context_hash="b" * 64,
            message="bob question",
            language="en",
            model="test-model",
            payload_json="{}",
            expires_at=now(),
        )
        self._login("alice", "Alic3")

        rv = self.client.post("/recommendations/agent/session/clear")

        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.json["ok"])
        self.assertEqual(rv.json["deleted"], 1)
        self.assertEqual(rv.json["deletedCache"], 1)
        self.assertEqual(rv.json["agentSessions"], [])
        self.assertIsNone(
            RecommendationAgentSession.get_or_none(
                RecommendationAgentSession.user == self.user
            )
        )
        self.assertIsNone(
            RecommendationAgentCache.get_or_none(
                RecommendationAgentCache.user == self.user
            )
        )
        self.assertIsNotNone(
            RecommendationAgentSession.get_or_none(
                RecommendationAgentSession.user == bob
            )
        )
        self.assertIsNotNone(
            RecommendationAgentCache.get_or_none(
                RecommendationAgentCache.user == bob
            )
        )

    def test_recommendation_agent_reuses_cached_result_for_same_context(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = self._model_response(
                {
                    "reply": "Cached artist response.",
                    "recommendedArtists": [
                        {
                            "name": "Cached Artist",
                            "reason": "It matches your listening.",
                            "genres": ["synth-pop"],
                            "starterTracks": ["Cache Song"],
                        },
                    ],
                }
            )
            first = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Find cached artists"},
            )
            second = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Find cached artists"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        post.assert_called_once()
        self.assertFalse(first.json["cache"]["hit"])
        self.assertTrue(second.json["cache"]["hit"])
        self.assertTrue(second.json["agent"]["cached"])
        self.assertEqual(RecommendationAgentSession.select().count(), 2)
        self.assertEqual(
            second.json["recommendedArtists"][0]["name"],
            "Cached Artist",
        )

    def test_recommendation_agent_cache_depends_on_previous_artist_details(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.side_effect = [
                self._model_response(
                    {
                        "reply": "First starter expansion.",
                        "recommendedArtists": [
                            {
                                "name": "Context Artist",
                                "reason": "Expands from old starter context.",
                                "genres": ["indie"],
                                "starterTracks": ["Fresh Starter A"],
                            },
                        ],
                    }
                ),
                self._model_response(
                    {
                        "reply": "Second starter expansion.",
                        "recommendedArtists": [
                            {
                                "name": "Context Artist",
                                "reason": "Expands from changed starter context.",
                                "genres": ["indie"],
                                "starterTracks": ["Fresh Starter B"],
                            },
                        ],
                    }
                ),
            ]
            first = self.client.post(
                "/recommendations/agent",
                json={
                    "language": "en",
                    "message": "Give me more starter tracks",
                    "previousRecommendedArtists": [
                        {
                            "name": "Context Artist",
                            "reason": "First previous reason.",
                            "genres": ["indie"],
                            "starterTracks": ["Old Starter A"],
                        }
                    ],
                },
            )
            second = self.client.post(
                "/recommendations/agent",
                json={
                    "language": "en",
                    "message": "Give me more starter tracks",
                    "previousRecommendedArtists": [
                        {
                            "name": "Context Artist",
                            "reason": "Changed previous reason.",
                            "genres": ["indie"],
                            "starterTracks": ["Old Starter B"],
                        }
                    ],
                },
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertFalse(first.json["cache"]["hit"])
        self.assertFalse(second.json["cache"]["hit"])
        self.assertNotEqual(
            first.json["cache"]["contextHash"],
            second.json["cache"]["contextHash"],
        )
        self.assertEqual(second.json["reply"], "Second starter expansion.")

    def test_recommendation_agent_cache_depends_on_recommendation_track_summary(self):
        self._enable_recommendation_agent()
        self._create_recommended_playlist(self.track)
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.side_effect = [
                self._model_response(
                    {
                        "reply": "First context response.",
                        "recommendedArtists": [],
                    }
                ),
                self._model_response(
                    {
                        "reply": "Changed track context response.",
                        "recommendedArtists": [],
                    }
                ),
            ]
            first = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Find context artists"},
            )
            self.track.title = "Changed Recommended One"
            self.track.genre = "ambient"
            self.track.play_count = 8
            self.track.save()
            second = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Find context artists"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertFalse(first.json["cache"]["hit"])
        self.assertFalse(second.json["cache"]["hit"])
        self.assertNotEqual(
            first.json["cache"]["contextHash"],
            second.json["cache"]["contextHash"],
        )
        self.assertEqual(second.json["reply"], "Changed track context response.")

    def test_recommendation_agent_cache_depends_on_play_history_beyond_recent_summary(self):
        self._enable_recommendation_agent()
        base_time = now()
        history_tracks = [
            self._create_track(f"History Track {index}", 40 + index, genre="rock")
            for index in range(7)
        ]
        for index, track in enumerate(history_tracks[:6]):
            User_Play_Activity.create(
                track=track,
                user=self.user,
                time=base_time - timedelta(minutes=index),
            )
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.side_effect = [
                self._model_response(
                    {
                        "reply": "First play history response.",
                        "recommendedArtists": [],
                    }
                ),
                self._model_response(
                    {
                        "reply": "Changed deep history response.",
                        "recommendedArtists": [],
                    }
                ),
            ]
            first = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Find history artists"},
            )
            oldest_activity = User_Play_Activity.get(
                User_Play_Activity.track == history_tracks[5]
            )
            oldest_activity.track = history_tracks[6]
            oldest_activity.save()
            second = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Find history artists"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertFalse(first.json["cache"]["hit"])
        self.assertFalse(second.json["cache"]["hit"])
        self.assertNotEqual(
            first.json["cache"]["contextHash"],
            second.json["cache"]["contextHash"],
        )
        self.assertEqual(second.json["reply"], "Changed deep history response.")

    def test_recommendation_agent_stream_reuses_cached_result_for_same_context(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = self._model_response(
                {
                    "reply": "Cached stream artist response.",
                    "recommendedArtists": [
                        {
                            "name": "Cached Stream Artist",
                            "reason": "It matches your streamed listening.",
                            "genres": ["dream-pop"],
                            "starterTracks": ["Stream Cache Song"],
                        },
                    ],
                }
            )
            first = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Find stream cached artists"},
            )
            second = self.client.post(
                "/recommendations/agent/stream",
                json={"language": "en", "message": "Find stream cached artists"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.mimetype, "text/event-stream")
        post.assert_called_once()
        self.assertFalse(first.json["cache"]["hit"])
        body = second.data
        self.assertIn("event: status", body)
        self.assertIn('"status": "cached"', body)
        self.assertIn("event: final", body)
        self.assertIn('"hit": true', body)
        self.assertIn("Cached Stream Artist", body)
        self.assertEqual(RecommendationAgentSession.select().count(), 2)
        health = self.client.get("/recommendations/agent/health")
        self.assertEqual(health.json["metrics"]["agent_request_count"], 2)
        self.assertEqual(health.json["metrics"]["agent_success_count"], 2)
        self.assertEqual(health.json["metrics"]["agent_cache_hit_count"], 1)

    def test_recommendation_agent_force_refresh_bypasses_cache(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.side_effect = [
                self._model_response(
                    {
                        "reply": "First cached response.",
                        "recommendedArtists": [],
                    }
                ),
                self._model_response(
                    {
                        "reply": "Forced fresh response.",
                        "recommendedArtists": [],
                    }
                ),
            ]
            first = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Refresh me"},
            )
            second = self.client.post(
                "/recommendations/agent",
                json={
                    "language": "en",
                    "message": "Refresh me",
                    "forceRefresh": True,
                },
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertFalse(first.json["cache"]["hit"])
        self.assertFalse(second.json["cache"]["hit"])
        self.assertEqual(second.json["reply"], "Forced fresh response.")

    def test_recommendation_agent_stream_force_refresh_bypasses_cache(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.side_effect = [
                self._model_response(
                    {
                        "reply": "Initial stream cache response.",
                        "recommendedArtists": [],
                    }
                ),
                self._stream_model_response(
                    '{"reply":"Forced stream response.","recommendedArtists":[]}'
                ),
            ]
            first = self.client.post(
                "/recommendations/agent",
                json={"language": "en", "message": "Refresh stream"},
            )
            second = self.client.post(
                "/recommendations/agent/stream",
                json={
                    "language": "en",
                    "message": "Refresh stream",
                    "forceRefresh": True,
                },
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertFalse(first.json["cache"]["hit"])
        body = second.data
        self.assertNotIn('"status": "cached"', body)
        self.assertIn("Forced stream response.", body)
        self.assertIn('"hit": false', body)

    def test_recommendation_agent_ignores_malformed_previous_artist_lists(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = self._model_response(
                {
                    "reply": "Handled malformed follow-up context.",
                    "recommendedArtists": [],
                }
            )
            rv = self.client.post(
                "/recommendations/agent",
                json={
                    "language": "en",
                    "message": "Give me starter tracks",
                    "previousRecommendedArtists": [
                        {
                            "name": "Outside Previous",
                            "reason": "Recommended earlier.",
                            "genres": 1,
                            "starterTracks": {"bad": "shape"},
                            "similarTo": {"bad": "shape"},
                            "confidence": "not-a-number",
                            "mood": 1,
                        }
                    ],
                },
            )

        self.assertEqual(rv.status_code, 200)
        request_payload = post.call_args.kwargs["json"]
        prompt_payload = json.loads(request_payload["messages"][1]["content"])
        previous_artist = prompt_payload["context"]["previousRecommendedArtists"][0]
        self.assertEqual(previous_artist["name"], "Outside Previous")
        self.assertEqual(previous_artist["genres"], [])
        self.assertEqual(previous_artist["starterTracks"], [])
        self.assertEqual(previous_artist["similarTo"], [])
        self.assertEqual(previous_artist["confidence"], 0.0)
        self.assertEqual(previous_artist["mood"], [])

    def test_recommendation_agent_preserves_enhanced_previous_artist_context(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = self._model_response(
                {
                    "reply": "Expanded enhanced previous context.",
                    "recommendedArtists": [],
                }
            )
            rv = self.client.post(
                "/recommendations/agent",
                json={
                    "language": "en",
                    "message": "Give me more like this",
                    "previousRecommendedArtists": [
                        {
                            "name": "Enhanced Previous",
                            "reason": "Recommended earlier.",
                            "genres": ["art pop"],
                            "starterTracks": ["Start Here"],
                            "similarTo": ["Local Artist"],
                            "confidence": 0.82,
                            "mood": ["melodic", "relaxed"],
                        }
                    ],
                },
            )

        self.assertEqual(rv.status_code, 200)
        request_payload = post.call_args.kwargs["json"]
        prompt_payload = json.loads(request_payload["messages"][1]["content"])
        previous_artist = prompt_payload["context"]["previousRecommendedArtists"][0]
        self.assertEqual(previous_artist["name"], "Enhanced Previous")
        self.assertEqual(previous_artist["similarTo"], ["Local Artist"])
        self.assertEqual(previous_artist["confidence"], 0.82)
        self.assertEqual(previous_artist["mood"], ["melodic", "relaxed"])

    def test_recommendation_agent_retries_retryable_upstream_error_once(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.side_effect = [
                StubModelResponse(
                    {"error": {"message": "rate limited", "code": "rate_limit"}},
                    status_code=429,
                ),
                self._model_response(
                    {
                        "reply": "Recovered after retry.",
                        "recommendedArtists": [],
                    }
                ),
            ]
            rv = self.client.get("/recommendations/agent?lang=en")

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(rv.json["reply"], "Recovered after retry.")

    def test_recommendation_agent_reports_non_retryable_upstream_error_details(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = StubModelResponse(
                {"error": {"message": "invalid api key", "code": "unauthorized"}},
                status_code=401,
            )
            rv = self.client.get("/recommendations/agent?lang=en")

        self.assertEqual(rv.status_code, 502)
        self.assertEqual(post.call_count, 1)
        self.assertFalse(rv.json["ok"])
        self.assertIn("requestId", rv.json)
        self.assertEqual(
            rv.json["errorCode"],
            "recommendation_agent_upstream_error",
        )
        self.assertEqual(rv.json["details"]["upstreamStatus"], 401)
        self.assertEqual(rv.json["details"]["upstreamErrorCode"], "unauthorized")
        self.assertFalse(rv.json["details"]["retryable"])
        self.assertEqual(
            rv.json["details"]["contextStats"]["playHistoryCount"],
            0,
        )
        self.assertIn("requestPayloadBytes", rv.json["details"]["contextStats"])
        response_text = json.dumps(rv.json)
        self.assertNotIn("test-key", response_text)
        self.assertNotIn("Recommended One", response_text)

    def test_recommendation_agent_retries_without_response_format_when_rejected(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.side_effect = [
                StubModelResponse(
                    {
                        "error": {
                            "message": "response_format is not supported",
                            "code": "bad_request",
                        }
                    },
                    status_code=400,
                ),
                self._model_response(
                    {
                        "reply": "Recovered without response_format.",
                        "recommendedArtists": [],
                    }
                ),
            ]
            rv = self.client.get("/recommendations/agent?lang=en")

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertIn("response_format", post.call_args_list[0].kwargs["json"])
        self.assertNotIn("response_format", post.call_args_list[1].kwargs["json"])
        self.assertEqual(rv.json["reply"], "Recovered without response_format.")

    def test_recommendation_agent_reports_network_error_details(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch(
            "supysonic.recommendation_agent.requests.post",
            side_effect=requests.exceptions.ConnectionError("connection reset"),
        ) as post:
            rv = self.client.get("/recommendations/agent?lang=en")

        self.assertEqual(rv.status_code, 502)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            rv.json["errorCode"],
            "recommendation_agent_upstream_error",
        )
        self.assertEqual(
            rv.json["details"]["upstreamErrorCode"],
            "ConnectionError",
        )
        self.assertTrue(rv.json["details"]["retryable"])
        self.assertIn("contextStats", rv.json["details"])

    def test_recommendation_agent_repairs_invalid_model_json_once(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.side_effect = [
                StubModelResponse({"choices": [{"message": {"content": "not json"}}]}),
                self._model_response(
                    {
                        "reply": "Repaired JSON response.",
                        "recommendedArtists": [
                            {
                                "name": "Outside Repair",
                                "reason": "It matches the repaired request.",
                                "genres": ["pop"],
                                "starterTracks": ["Starter"],
                            },
                        ],
                    }
                ),
            ]
            rv = self.client.get("/recommendations/agent?lang=en")

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(post.call_count, 2)
        repair_payload = post.call_args_list[1].kwargs["json"]
        repair_prompt = json.loads(repair_payload["messages"][1]["content"])
        self.assertIn("previousError", repair_prompt)
        self.assertEqual(rv.json["reply"], "Repaired JSON response.")
        self.assertEqual(
            [artist["name"] for artist in rv.json["recommendedArtists"]],
            ["Outside Repair"],
        )

    def test_recommendation_agent_extracts_json_from_model_text_without_repair(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        model_content = (
            "Here is the JSON:\n"
            '{"reply": "Extracted JSON response.", "recommendedArtists": []}'
            "\nDone."
        )
        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = StubModelResponse(
                {"choices": [{"message": {"content": model_content}}]}
            )
            rv = self.client.get("/recommendations/agent?lang=en")

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(rv.json["reply"], "Extracted JSON response.")

    def test_recommendation_agent_reports_error_when_json_repair_fails(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = StubModelResponse(
                {"choices": [{"message": {"content": "not json"}}]}
            )
            rv = self.client.get("/recommendations/agent?lang=en")

        self.assertEqual(rv.status_code, 502)
        self.assertEqual(post.call_count, 2)
        self.assertFalse(rv.json["ok"])
        self.assertEqual(
            rv.json["errorCode"],
            "recommendation_agent_invalid_response",
        )

    def test_recommendation_agent_rejects_invalid_model_json(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")
        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = StubModelResponse(
                {"choices": [{"message": {"content": "not json"}}]}
            )
            rv = self.client.get("/recommendations/agent?lang=en")

        self.assertEqual(rv.status_code, 502)
        self.assertEqual(post.call_count, 2)
        self.assertFalse(rv.json["ok"])
        self.assertEqual(
            rv.json["errorCode"],
            "recommendation_agent_invalid_response",
        )

    def test_recommendation_agent_reports_model_timeout(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")
        with patch(
            "supysonic.recommendation_agent.requests.post",
            side_effect=requests.exceptions.Timeout,
        ):
            rv = self.client.get("/recommendations/agent?lang=en")

        self.assertEqual(rv.status_code, 504)
        self.assertFalse(rv.json["ok"])
        self.assertEqual(rv.json["errorCode"], "recommendation_agent_timeout")

    def test_recommendation_agent_streams_reply_delta_and_final_payload(self):
        self._enable_recommendation_agent(max_output_tokens=0)
        Artist.create(name="G.E.M.")
        self._login("alice", "Alic3")

        content = (
            '{"reply":"Hello stream world","recommendedArtists":['
            '{"name":"Outside Stream","reason":"Fits rock listening.",'
            '"genres":["rock"],"starterTracks":["Starter"]},'
            '{"name":"邓紫棋 (G.E.M.)","reason":"Already local.",'
            '"genres":["C-pop"],"starterTracks":["Local"]}'
            "]}"
        )
        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = self._stream_model_response(
                content[:16],
                content[16:32],
                content[32:],
            )
            rv = self.client.get("/recommendations/agent/stream?lang=en")
            body = rv.data

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.mimetype, "text/event-stream")
        post.assert_called_once()
        request_payload = post.call_args.kwargs["json"]
        self.assertTrue(request_payload["stream"])
        self.assertNotIn("max_tokens", request_payload)
        self.assertIn("event: status", body)
        self.assertIn("event: reply_delta", body)
        self.assertIn("event: final", body)
        self.assertIn('"delta": "Hello ', body)
        self.assertIn('"delta": "stream world"', body)
        self.assertIn("Outside Stream", body)
        self.assertNotIn("邓紫棋", body)

    def test_recommendation_agent_stream_retries_retryable_upstream_error_once(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.side_effect = [
                StubModelResponse(
                    {"error": {"message": "rate limited", "code": "rate_limit"}},
                    status_code=429,
                ),
                self._stream_model_response(
                    '{"reply":"Recovered stream.","recommendedArtists":[]}'
                ),
            ]
            rv = self.client.get("/recommendations/agent/stream?lang=en")
            body = rv.data

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertIn("event: final", body)
        self.assertIn("Recovered stream.", body)

    def test_recommendation_agent_stream_retries_without_response_format_when_rejected(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.side_effect = [
                StubModelResponse(
                    {
                        "error": {
                            "message": "response_format is not supported",
                            "code": "bad_request",
                        }
                    },
                    status_code=400,
                ),
                self._stream_model_response(
                    '{"reply":"Stream without response_format.",'
                    '"recommendedArtists":[]}'
                ),
            ]
            rv = self.client.get("/recommendations/agent/stream?lang=en")
            body = rv.data

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertTrue(post.call_args_list[0].kwargs["json"]["stream"])
        self.assertIn("response_format", post.call_args_list[0].kwargs["json"])
        self.assertTrue(post.call_args_list[1].kwargs["json"]["stream"])
        self.assertNotIn("response_format", post.call_args_list[1].kwargs["json"])
        self.assertIn("event: final", body)
        self.assertIn("Stream without response_format.", body)

    def test_recommendation_agent_stream_repairs_invalid_final_json_once(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.side_effect = [
                self._stream_model_response("not json"),
                self._model_response(
                    {
                        "reply": "Repaired stream JSON.",
                        "recommendedArtists": [
                            {
                                "name": "Outside Stream Repair",
                                "reason": "It repairs the streamed result.",
                                "genres": ["pop"],
                                "starterTracks": ["Starter"],
                            },
                        ],
                    }
                ),
            ]
            rv = self.client.get("/recommendations/agent/stream?lang=en")
            body = rv.data

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertIn("event: status", body)
        self.assertIn('"status": "repairing"', body)
        self.assertIn("event: final", body)
        self.assertIn("Repaired stream JSON.", body)
        self.assertIn("Outside Stream Repair", body)

    def test_recommendation_agent_stream_reports_error_when_json_repair_fails(self):
        self._enable_recommendation_agent()
        self._login("alice", "Alic3")

        with patch("supysonic.recommendation_agent.requests.post") as post:
            post.return_value = self._stream_model_response("not json")
            rv = self.client.get("/recommendations/agent/stream?lang=en")
            body = rv.data

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertIn("event: error", body)
        self.assertIn("recommendation_agent_invalid_response", body)
        self.assertIn("contextStats", body)
        self.assertNotIn("event: final", body)


if __name__ == "__main__":
    unittest.main()
