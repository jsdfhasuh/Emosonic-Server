import os
from collections import Counter
from datetime import datetime, timedelta
from unittest.mock import patch

from supysonic.db import Album, Artist, Folder, Playlist, Track, TrackMetadata
from supysonic.db import User, User_Play_Activity
from supysonic.recommend import (
    RECOMMENDATION_SCORE_WEIGHTS,
    RECOMMENDED_PLAYLIST_COMMENT,
    _buildRecommendedTracks,
    _get_minimum_fresh_track_count,
    _get_recent_recommendation_counts,
    create_recommend_playlist,
)
from supysonic.recommendation_feedback import set_recommendation_feedback
from supysonic.user_listening_profile import build_recommendation_play_weights

from ..testbase import TestBase


class DailyRecommendationPersonalizationTestCase(TestBase):
    __with_api__ = True

    def setUp(self):
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.reference_time = datetime(2026, 11, 12, 0, 5)
        self.track_number = 0

    def _artist_album(self, name):
        artist = Artist.create(name=name)
        return artist, Album.create(name=name + " Album", artist=artist)

    def _track(self, title, artist_album, language=None, genre="Pop"):
        self.track_number += 1
        artist, album = artist_album
        track = Track.create(
            title=title,
            disc=1,
            number=self.track_number,
            duration=180,
            has_art=False,
            artist=artist,
            album=album,
            genre=genre,
            bitrate=320,
            path=os.path.join("/music", f"personalization-{self.track_number}.flac"),
            last_modification=1,
            root_folder=self.root,
            folder=self.root,
        )
        if language:
            TrackMetadata.create(
                track=track,
                track_last_modification=1,
                language=language,
                energy=50,
                mood_json='["calm"]',
                confidence=0.9,
                provider="llm",
                source="llm",
            )
        return track

    def _play(self, track, days_ago, count=1):
        for _ in range(count):
            User_Play_Activity.create(
                user=self.user,
                track=track,
                time=self.reference_time - timedelta(days=days_ago),
            )

    def _playlist(self, day, tracks, user=None):
        user = user or self.user
        return Playlist.create(
            user=user,
            name=f"{user.name}'s {day} recommend playlist",
            comment=RECOMMENDED_PLAYLIST_COMMENT,
            tracks=",".join(str(track.id) for track in tracks),
        )

    def test_daily_generation_prefers_recent_chinese_over_large_old_korean_history(self):
        old_artist = self._artist_album("Old Korean Artist")
        recent_artist = self._artist_album("Recent Chinese Artist")
        old_seed = self._track("Old Korean Seed", old_artist, "ko")
        recent_seed = self._track("Recent Chinese Seed", recent_artist, "zh")
        korean = self._track("Korean Candidate", old_artist, "ko")
        chinese = self._track("Chinese Candidate", recent_artist, "zh")
        self._play(old_seed, 90, 400)
        self._play(recent_seed, 1, 3)

        with patch(
            "supysonic.user_listening_profile.now", return_value=self.reference_time
        ), self.assertLogs("supysonic.recommend", level="INFO") as logs:
            created = create_recommend_playlist(
                num_songs=1, user=self.user, day="2026-11-12", config=self.config
            )
        playlist = Playlist.get(Playlist.user == self.user)
        self.assertEqual(created, 1)
        self.assertEqual(playlist.tracks, str(chinese.id))
        self.assertNotIn(str(korean.id), playlist.tracks)
        self.assertEqual(User_Play_Activity.select().count(), 403)
        self.assertIn("event=daily_personalization", "\n".join(logs.output))

    def test_language_changes_ranking_with_identical_artist_genre_and_album(self):
        artist = self._artist_album("Mixed Language Artist")
        old_seed = self._track("Old Seed", artist, "ko")
        recent_seed = self._track("Recent Seed", artist, "zh")
        korean = self._track("A Korean", artist, "ko")
        chinese = self._track("Z Chinese", artist, "Mandarin")
        unknown = self._track("Unknown Language", artist)
        self._play(old_seed, 90, 100)
        self._play(recent_seed, 1, 2)
        weights = build_recommendation_play_weights(self.user, self.reference_time)

        result = _buildRecommendedTracks(weights, 3)

        self.assertEqual(result[0].id, chinese.id)
        self.assertEqual({t.id for t in result}, {korean.id, chinese.id, unknown.id})

    def test_explicit_language_genre_fallback_works_without_metadata(self):
        artist = self._artist_album("No Metadata Artist")
        seed = self._track("Chinese Seed", artist, genre="Mandopop")
        chinese = self._track("Chinese Candidate", artist, genre="Mandopop")
        korean_style = self._track("Unknown Language K-Pop", artist, genre="K-Pop")
        result = _buildRecommendedTracks({seed.id: 0.001}, 2)
        self.assertEqual(result[0].id, chinese.id)
        self.assertIn(korean_style.id, {track.id for track in result})

    def test_diversity_and_three_day_rotation_hold_across_four_daily_playlists(self):
        seed_artist = self._artist_album("Seed Artist")
        seed = self._track("Seed", seed_artist, "zh")
        self._play(seed, 1)
        for artist_index in range(12):
            artist = self._artist_album(f"Candidate Artist {artist_index}")
            for index in range(20):
                self._track(f"Candidate {artist_index}-{index}", artist, "zh")
        generated = []
        for offset in range(4):
            reference = self.reference_time + timedelta(days=offset)
            day = reference.strftime("%Y-%m-%d")
            with patch("supysonic.user_listening_profile.now", return_value=reference):
                create_recommend_playlist(
                    num_songs=50, user=self.user, day=day, config=self.config
                )
            playlist = Playlist.get(Playlist.name == f"alice's {day} recommend playlist")
            tracks = playlist.get_tracks()
            ids = {str(track.id) for track in tracks}
            previous = set().union(*generated[-3:]) if generated else set()
            self.assertEqual(len(ids), 50)
            self.assertLessEqual(max(Counter(t.artist_id for t in tracks).values()), 5)
            self.assertGreaterEqual(len(ids - previous), 40)
            self.assertNotIn(str(seed.id), ids)
            generated.append(ids)

    def test_repeat_count_demotes_multiple_exposures_more_than_one(self):
        artist = self._artist_album("Tied Artist")
        repeated = self._track("A Repeated", artist)
        once = self._track("B Once", artist)
        fresh = self._track("Z Fresh", artist)

        tracks = _buildRecommendedTracks(
            {}, 3,
            recentRecommendationCounts={str(repeated.id): 3, str(once.id): 1},
        )

        self.assertEqual([t.id for t in tracks], [fresh.id, once.id, repeated.id])

    def test_recent_exposures_count_distinct_days_and_respect_user_and_window(self):
        artist = self._artist_album("Exposure Artist")
        track = self._track("Repeated", artist)
        outside = self._track("Outside", artist)
        self._playlist("2026-11-11", [track, track])
        self._playlist("2026-11-11", [track])
        self._playlist("2026-11-09", [track])
        self._playlist("2026-11-08", [outside])
        self._playlist("2026-11-12", [outside])
        self._playlist("2026-11-13", [outside])
        self._playlist("2026-11-10", [outside], User.get(User.name == "bob"))

        counts = _get_recent_recommendation_counts(self.user, "2026-11-12", 3)

        self.assertEqual(counts, {str(track.id): 2})
        self.assertEqual(_get_recent_recommendation_counts(self.user, "invalid", 3), {})

    def test_new_scoring_does_not_bypass_negative_feedback_or_heard_exclusion(self):
        artist = self._artist_album("Chinese Artist")
        seed = self._track("Heard", artist, "zh")
        blocked = self._track("Blocked", artist, "zh")
        visible = self._track("Visible", artist, "zh")
        self._play(seed, 1)
        set_recommendation_feedback(self.user, str(blocked.id), "dislike")
        with patch(
            "supysonic.user_listening_profile.now", return_value=self.reference_time
        ):
            create_recommend_playlist(
                num_songs=50, user=self.user, day="2026-11-12", config=self.config
            )
        playlist = Playlist.get(Playlist.user == self.user)
        self.assertEqual(playlist.tracks, str(visible.id))

    def test_api_serves_generated_fifty_track_playlist_without_changing_selection(self):
        seed = self._track("Seed", self._artist_album("Seed Artist"), "zh")
        self._play(seed, 1)
        for artist_index in range(10):
            artist = self._artist_album(f"API Artist {artist_index}")
            for index in range(6):
                self._track(f"API Candidate {artist_index}-{index}", artist, "zh")
        with patch(
            "supysonic.user_listening_profile.now", return_value=self.reference_time
        ):
            create_recommend_playlist(
                num_songs=50, user=self.user, day="2026-11-12", config=self.config
            )
            playlist = Playlist.get(Playlist.user == self.user)
            expected_ids = playlist.tracks.split(",")
            response = self.client.get(
                "/rest/getRecommendedPlaylists",
                query_string={
                    "u": "alice", "p": "Alic3", "c": "test", "v": "1.16.1",
                    "f": "json", "count": "50",
                },
            )
            payload = response.get_json()["subsonic-response"]
            response.close()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual([entry["id"] for entry in payload["playlist"]["entry"]], expected_ids)
        self.assertLessEqual(
            max(Counter(entry["artistId"] for entry in payload["playlist"]["entry"]).values()),
            5,
        )

    def test_defaults_and_explicit_rotation_override(self):
        self.assertAlmostEqual(sum(RECOMMENDATION_SCORE_WEIGHTS.values()), 1.0)
        self.assertEqual(_get_minimum_fresh_track_count(50), 40)
        self.assertEqual(_get_minimum_fresh_track_count(50, self.config), 40)
        self.config.DAEMON["recommend_playlist_rotation_ratio"] = 0.3
        self.assertEqual(_get_minimum_fresh_track_count(50, self.config), 15)
        self.config.DAEMON["recommend_playlist_rotation_ratio"] = 0
        self.assertEqual(_get_minimum_fresh_track_count(50, self.config), 0)
        self.config.DAEMON["recommend_playlist_rotation_ratio"] = float("nan")
        self.assertEqual(_get_minimum_fresh_track_count(50, self.config), 40)
