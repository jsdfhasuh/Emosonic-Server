import json
import os
import unittest

from unittest.mock import patch

import requests
from flask import current_app

from supysonic.db import (
    Album,
    Artist,
    Folder,
    Playlist,
    Track,
    User,
    User_Play_Activity,
)
from supysonic.recommend import RECOMMENDED_PLAYLIST_COMMENT, getRecommendationDay
from supysonic.recommendation_feedback import set_recommendation_feedback

from .frontendtestbase import FrontendTestBase


class StubModelResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class RecommendationsTestCase(FrontendTestBase):
    def setUp(self):
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.artist = Artist.create(name="Artist!")
        self.album = Album.create(name="Album!", artist=self.artist)
        self.track = self._create_track("Recommended One", 1, genre="rock")
        self.other_track = self._create_track("Recommended Two", 2, genre="jazz")

    def _create_track(self, title: str, number: int, genre: str) -> Track:
        return Track.create(
            disc=1,
            number=number,
            title=title,
            duration=180 + number,
            has_art=False,
            album=self.album,
            artist=self.artist,
            genre=genre,
            bitrate=320,
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
        self.assertIn("Recommendation Agent", rv.data)
        self.assertIn("/recommendations/agent", rv.data)

    def test_recommendations_filter_disliked_tracks(self):
        self._create_recommended_playlist(self.track, self.other_track)
        set_recommendation_feedback(self.user, str(self.track.id), "dislike")

        self._login("alice", "Alic3")
        rv = self.client.get("/recommendations")

        self.assertEqual(rv.status_code, 200)
        self.assertNotIn("Recommended One", rv.data)
        self.assertIn("Recommended Two", rv.data)

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

    def test_recommendation_agent_calls_llm_with_play_context_and_filters_library_artists(self):
        self._enable_recommendation_agent()
        User_Play_Activity.create(track=self.track, user=self.user)
        User_Play_Activity.create(track=self.track, user=self.user)

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
        prompt_payload = json.loads(request_payload["messages"][1]["content"])
        self.assertEqual(prompt_payload["userMessage"], "Find outside artists")
        self.assertIn(
            "Recommended One",
            json.dumps(prompt_payload["context"]["playHistory"], ensure_ascii=False),
        )
        self.assertIn("Artist!", prompt_payload["context"]["libraryArtists"])

        payload = rv.json
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["agent"]["mode"], "llm")
        self.assertEqual(payload["reply"], "Try Outside Artist and skip local artists.")
        self.assertEqual(
            [artist["name"] for artist in payload["recommendedArtists"]],
            ["Outside Artist"],
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


if __name__ == "__main__":
    unittest.main()
