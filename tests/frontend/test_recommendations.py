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
        self.assertIn("AI Agent", rv.data)
        self.assertIn("Daily Recommendations", rv.data)
        self.assertIn("Recommendation Agent", rv.data)
        self.assertIn("/recommendations/agent", rv.data)
        self.assertIn("/recommendations/agent/stream", rv.data)
        self.assertIn("data-agent-retry", rv.data)
        self.assertIn("data-agent-stream-url", rv.data)
        self.assertIn("data-agent-inline-artist-cards", rv.data)
        self.assertIn("has-artist-cards", rv.data)
        self.assertIn("data-recommendation-row", rv.data)
        self.assertIn("data-recommendation-prev", rv.data)
        self.assertIn("data-recommendation-next", rv.data)
        self.assertIn("data-recommendation-page", rv.data)
        self.assertIn("initRecommendationTabs", rv.data)
        self.assertIn("ReadableStream", rv.data)
        self.assertIn("reply_delta", rv.data)
        self.assertIn("previousRecommendedArtists", rv.data)
        self.assertIn("abandonStreamRender", rv.data)
        self.assertIn("isCurrentRequest", rv.data)
        self.assertIn("AbortController", rv.data)

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
        Artist.create(name="G.E.M.")
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
                        {
                            "name": "邓紫棋 (G.E.M.)",
                            "reason": "Already local through an alias and should be filtered.",
                            "genres": ["C-pop"],
                            "starterTracks": ["Local Alias Song"],
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
        self.assertIn("G.E.M.", prompt_payload["context"]["libraryArtists"])

        payload = rv.json
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["agent"]["mode"], "llm")
        self.assertIn("filtered them out", payload["reply"])
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
