import json
from datetime import timedelta

from supysonic.db import (
    Album, Artist, Folder, Image, Track, TrackMetadata, User, User_Play_Activity, now,
)
from supysonic.profile_home import build_profile_home

from .frontendtestbase import FrontendTestBase


class ProfileHomeTestCase(FrontendTestBase):
    def setUp(self):
        super().setUp()
        self.alice = User.get(User.name == "alice")
        self.bob = User.get(User.name == "bob")
        self.reference = now()
        self.folder = Folder.create(root=True, name="Profile", path="/music/profile")
        self.artist = Artist.create(name="Profile artist")
        self.album = Album.create(name="Profile album", artist=self.artist)

    def add_track(self, title, language=None):
        track = Track.create(
            disc=1, number=1, title=title, duration=180, has_art=False,
            album=self.album, artist=self.artist, genre="pop", bitrate=320,
            path=f"/music/profile/{title}.flac", last_modification=1,
            root_folder=self.folder, folder=self.folder,
        )
        if language:
            TrackMetadata.create(
                track=track, track_last_modification=1, language=language,
                mood_json=json.dumps(["Warm"]), energy=55, danceability=33,
                confidence=0.9, provider="llm", source="llm",
            )
        return track

    def add_play(self, track, days_ago, user=None):
        User_Play_Activity.create(
            user=user or self.alice, track=track,
            time=self.reference - timedelta(days=days_ago),
        )

    def test_window_and_language_counts_use_their_own_denominators(self):
        song = self.add_track("Seven day song", "cantonese")
        other = self.add_track("Same language song", "yue")
        no_metadata = self.add_track("Song missing metadata")
        older = self.add_track("Thirty day song", "en")
        self.add_play(song, 1)
        self.add_play(song, 7)
        self.add_play(other, 2)
        self.add_play(no_metadata, 3)
        self.add_play(older, 28)
        self.add_play(older, 40)
        self.add_play(older, -1)
        self.add_play(older, 1, user=self.bob)
        Image.create(image_type="album", related_id=str(self.album.id), path="/music/cover.jpg")

        week = build_profile_home(self.alice, "7", reference_time=self.reference)
        self.assertEqual((week["play_count"], week["track_count"], week["covered_plays"]), (4, 3, 3))
        self.assertEqual(week["coverage"], 75)
        self.assertEqual(week["languages"], [
            {"label": "Cantonese", "label_zh": "粤语", "play_count": 3, "share": 100.0},
        ])
        self.assertEqual(week["energy"], 55.0)
        self.assertEqual(week["top_tracks"][0]["title"], "Seven day song")
        self.assertTrue(week["top_tracks"][0]["has_art"])
        self.assertEqual(week["top_artists"], [{"name": "Profile artist", "plays": 4}])
        self.assertEqual(week["last_play"], self.reference - timedelta(days=1))
        month = build_profile_home(self.alice, "30", reference_time=self.reference)
        self.assertEqual(month["play_count"], 5)
        self.assertEqual(month["languages"][1]["share"], 25.0)
        self.assertEqual(build_profile_home(self.alice, "all", self.reference)["play_count"], 6)

    def test_page_period_navigation_and_private_account_data(self):
        song = self.add_track("Beyond this week", "en")
        self.add_play(song, 12, user=self.bob)
        self._login("bob", "B0b")

        week = self.client.get("/user/me?period=wrong")
        self.assertEqual(week.status_code, 200)
        self.assertEqual(week.headers["Cache-Control"], "private, no-store")
        self.assertNotIn("Beyond this week", week.data)
        month = self.client.get("/user/me?period=30")
        self.assertIn("Beyond this week", month.data)

        self._logout()
        self._login("alice", "Alic3")
        self.assertNotIn("Beyond this week", self.client.get("/user/me?period=all").data)
