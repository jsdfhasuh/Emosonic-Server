import json
import os
import unittest

from supysonic.db import Album, Artist, Folder, Track, TrackMetadata, User

from .frontendtestbase import FrontendTestBase


class HomeSmartCardsFrontendTestCase(FrontendTestBase):
    def setUp(self):
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.track_number = 0
        with self.client.session_transaction() as session:
            session["userid"] = str(self.user.id)

    def _create_track(self, title, genre="ambient", play_count=0):
        self.track_number += 1
        artist = Artist.create(name=f"{title} Artist")
        album = Album.create(name=f"{title} Album", artist=artist)
        return Track.create(
            disc=1,
            number=self.track_number,
            title=title,
            duration=180,
            has_art=False,
            album=album,
            artist=artist,
            genre=genre,
            bitrate=320,
            play_count=play_count,
            path=os.path.join("/music", f"{self.track_number}.flac"),
            last_modification=1,
            root_folder=self.root,
            folder=self.root,
        )

    def _metadata(
        self,
        track,
        *,
        mood,
        scene,
        tags,
        language="en",
        energy=40,
    ):
        return TrackMetadata.create(
            track=track,
            track_last_modification=track.last_modification,
            mood_json=json.dumps(mood, ensure_ascii=False),
            scene_json=json.dumps(scene, ensure_ascii=False),
            tags_json=json.dumps(tags, ensure_ascii=False),
            language=language,
            energy=energy,
            valence=50,
            danceability=40,
            confidence=0.9,
            provider="llm",
            source="llm",
        )

    def _create_scene_tracks(
        self,
        prefix,
        count,
        *,
        mood,
        scene,
        tags,
        language="en",
        energy=40,
        genre="ambient",
    ):
        for index in range(count):
            track = self._create_track(
                f"{prefix} {index + 1}",
                genre=genre,
                play_count=count - index,
            )
            self._metadata(
                track,
                mood=mood,
                scene=scene,
                tags=tags,
                language=language,
                energy=energy,
            )

    def test_home_renders_smart_cards_with_reasons(self):
        self._create_scene_tracks(
            "Night",
            6,
            mood=["平静"],
            scene=["深夜"],
            tags=["氛围"],
            energy=28,
        )
        self._create_scene_tracks(
            "Study",
            6,
            mood=["沉思"],
            scene=["学习"],
            tags=["器乐"],
            energy=42,
            genre="classical",
        )
        self._create_scene_tracks(
            "Canto",
            6,
            mood=["怀旧"],
            scene=["通勤"],
            tags=["粤语流行", "经典金曲"],
            language="yue",
            energy=55,
            genre="cantopop",
        )

        rv = self.client.get("/")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Smart picks", rv.data)
        self.assertIn("Tonight fits", rv.data)
        self.assertIn("Study focus", rv.data)
        self.assertIn("Cantonese nostalgia", rv.data)
        self.assertIn("Night 1", rv.data)
        self.assertIn("scene: 深夜", rv.data)
        self.assertIn("language: yue", rv.data)


if __name__ == "__main__":
    unittest.main()
