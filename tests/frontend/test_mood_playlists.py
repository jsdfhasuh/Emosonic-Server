import json
import os
import unittest

from unittest.mock import patch

from supysonic.db import Album, Artist, Folder, Playlist, Track, TrackMetadata, User
from supysonic.mood_scene_playlist_service import (
    MOOD_SCENE_PLAYLIST_COMMENT_PREFIX,
    SAVED_MOOD_SCENE_PLAYLIST_COMMENT_PREFIX,
    get_mood_scene_playlist_comment,
)

from .frontendtestbase import FrontendTestBase


class MoodPlaylistsFrontendTestCase(FrontendTestBase):
    def setUp(self):
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.artist = Artist.create(name="Mood Frontend Artist")
        self.album = Album.create(name="Mood Frontend Album", artist=self.artist)
        self.track_number = 0

    def _create_track(self, title, genre="jazz"):
        self.track_number += 1
        return Track.create(
            disc=1,
            number=self.track_number,
            title=title,
            duration=180,
            has_art=False,
            album=self.album,
            artist=self.artist,
            genre=genre,
            bitrate=320,
            path=os.path.join("/music", f"{self.track_number}.flac"),
            last_modification=1,
            root_folder=self.root,
            folder=self.root,
        )

    def _metadata(self, track):
        return TrackMetadata.create(
            track=track,
            track_last_modification=track.last_modification,
            mood_json=json.dumps(["感伤"], ensure_ascii=False),
            scene_json=json.dumps(["深夜"], ensure_ascii=False),
            tags_json=json.dumps(["梦幻"], ensure_ascii=False),
            language="en",
            energy=25,
            valence=45,
            danceability=35,
            confidence=0.9,
            provider="llm",
            source="llm",
        )

    def test_mood_playlists_page_shows_scene_cards_tracks_and_reasons(self):
        track = self._create_track("Night Frontend")
        self._metadata(track)

        self._login("alice", "Alic3")
        with patch(
            "supysonic.frontend.mood_playlists.getRecommendationDay",
            return_value="2026-07-07",
        ):
            rv = self.client.get("/mood-playlists")

        self.assertEqual(rv.status_code, 200)
        for scene_key in (
            "night",
            "study",
            "commute",
            "relax",
            "high_energy",
            "low_energy",
            "cantonese",
            "nostalgic",
            "emo",
        ):
            self.assertIn(scene_key, rv.data)
        self.assertIn("Night Frontend", rv.data)
        self.assertIn("scene: 深夜", rv.data)
        self.assertIn("/mood-playlists/night/refresh", rv.data)
        self.assertIn("/mood-playlists/night/save", rv.data)

    def test_refresh_scene_creates_or_updates_today_playlist(self):
        track = self._create_track("Refresh Frontend")
        self._metadata(track)

        self._login("alice", "Alic3")
        with patch(
            "supysonic.frontend.mood_playlists.getRecommendationDay",
            return_value="2026-07-07",
        ):
            rv = self.client.post(
                "/mood-playlists/night/refresh",
                follow_redirects=True,
            )

        playlist = Playlist.get_or_none(
            Playlist.user == self.user,
            Playlist.comment == "mood_scene_playlist:night:2026-07-07",
        )
        self.assertEqual(rv.status_code, 200)
        self.assertIsNotNone(playlist)
        self.assertEqual(playlist.get_tracks(), [track])
        self.assertIn("Mood playlist created.", rv.data)

    def test_refresh_all_reports_partial_failures(self):
        self._login("alice", "Alic3")
        with patch(
            "supysonic.frontend.mood_playlists.getRecommendationDay",
            return_value="2026-07-07",
        ), patch(
            "supysonic.frontend.mood_playlists.refresh_daily_mood_scene_playlists_for_user",
            return_value={
                "created": 1,
                "updated": 2,
                "skipped": 3,
                "failed": 1,
                "results": [],
            },
        ):
            rv = self.client.post(
                "/mood-playlists/refresh",
                follow_redirects=True,
            )

        self.assertEqual(rv.status_code, 200)
        self.assertIn("1 created, 2 updated, 3 skipped, 1 failed", rv.data)

    def test_save_scene_creates_plain_saved_copy_and_redirects_to_playlist(self):
        track = self._create_track("Saved Frontend")
        self._metadata(track)

        self._login("alice", "Alic3")
        with patch(
            "supysonic.frontend.mood_playlists.getRecommendationDay",
            return_value="2026-07-07",
        ):
            rv = self.client.post(
                "/mood-playlists/night/save",
                follow_redirects=True,
            )

        saved = Playlist.get_or_none(
            Playlist.user == self.user,
            Playlist.comment == "saved_mood_scene_playlist:night:2026-07-07",
        )
        self.assertEqual(rv.status_code, 200)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.get_tracks(), [track])
        self.assertIn("Saved Frontend", rv.data)
        self.assertFalse(saved.comment.startswith(MOOD_SCENE_PLAYLIST_COMMENT_PREFIX))

    def test_playlist_index_shows_system_mood_playlists_and_saved_copies(self):
        track = self._create_track("Visible Saved")
        system = Playlist.create(
            name="alice's 2026-07-07 night mood playlist",
            user=self.user,
            comment=get_mood_scene_playlist_comment("night", "2026-07-07"),
        )
        system.add(track)
        system.save()
        saved = Playlist.create(
            name="Saved mood copy",
            user=self.user,
            comment=f"{SAVED_MOOD_SCENE_PLAYLIST_COMMENT_PREFIX}night:2026-07-07",
        )
        saved.add(track)
        saved.save()

        self._login("alice", "Alic3")
        rv = self.client.get("/playlist")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("2026-07-07 night mood playlist", rv.data)
        self.assertIn("Saved mood copy", rv.data)


if __name__ == "__main__":
    unittest.main()
