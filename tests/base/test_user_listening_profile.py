import json
import os
import unittest

from datetime import timedelta

from supysonic.db import (
    Album,
    Artist,
    Folder,
    Track,
    TrackMetadata,
    User,
    User_Play_Activity,
    now,
)
from supysonic.recommend import _buildMetadataPreferenceProfile
from supysonic.user_listening_profile import build_user_listening_profile

from ..testbase import TestBase


class UserListeningProfileTestCase(TestBase):
    def setUp(self):
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.artist = Artist.create(name="Profile Artist")
        self.album = Album.create(name="Profile Album", artist=self.artist)
        self.track_number = 0

    def _create_track(self, title, genre="pop"):
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

    def _metadata(
        self,
        track,
        *,
        mood,
        scene,
        tags,
        language="en",
        energy=50,
        valence=50,
        danceability=50,
        confidence=0.9,
        provider="llm",
        source="llm",
    ):
        return TrackMetadata.create(
            track=track,
            track_last_modification=track.last_modification,
            mood_json=json.dumps(mood, ensure_ascii=False),
            scene_json=json.dumps(scene, ensure_ascii=False),
            tags_json=json.dumps(tags, ensure_ascii=False),
            language=language,
            energy=energy,
            valence=valence,
            danceability=danceability,
            confidence=confidence,
            provider=provider,
            source=source,
        )

    def _play(self, track, count, played_at):
        for _ in range(count):
            User_Play_Activity.create(
                track=track,
                user=self.user,
                time=played_at,
            )

    def test_profile_weights_high_quality_metadata_by_play_count(self):
        reference_time = now()
        calm = self._create_track("Calm")
        energetic = self._create_track("Energetic")
        local = self._create_track("Local")
        low_confidence = self._create_track("Low Confidence")
        self._metadata(
            calm,
            mood=["calm"],
            scene=["night"],
            tags=["ambient"],
            energy=30,
            valence=60,
            danceability=35,
        )
        self._metadata(
            energetic,
            mood=["energetic"],
            scene=["workout"],
            tags=["rock"],
            energy=90,
            valence=70,
            danceability=80,
        )
        self._metadata(
            local,
            mood=["calm"],
            scene=["night"],
            tags=["ambient"],
            energy=20,
            confidence=0.25,
            provider="local",
            source="local",
        )
        self._metadata(
            low_confidence,
            mood=["low-confidence"],
            scene=["late night"],
            tags=["ignored"],
            energy=10,
            confidence=0.2,
            provider="llm",
            source="llm",
        )
        self._play(calm, 3, reference_time - timedelta(days=1))
        self._play(energetic, 1, reference_time - timedelta(days=10))
        self._play(local, 5, reference_time - timedelta(days=1))
        self._play(low_confidence, 2, reference_time - timedelta(days=1))

        profile = build_user_listening_profile(
            self.user,
            reference_time=reference_time,
        )

        self.assertEqual(profile["trackCount"], 2)
        self.assertEqual(profile["playCount"], 4)
        self.assertEqual(
            profile["topMoods"],
            [
                {"value": "calm", "playCount": 3},
                {"value": "energetic", "playCount": 1},
            ],
        )
        self.assertEqual(profile["topScenes"][0], {"value": "night", "playCount": 3})
        self.assertEqual(profile["topTags"][0], {"value": "ambient", "playCount": 3})
        self.assertEqual(profile["topLanguages"], [{"value": "en", "playCount": 4}])
        self.assertEqual(profile["averageEnergy"], 45.0)
        self.assertEqual(profile["averageValence"], 62.5)
        self.assertEqual(profile["averageDanceability"], 46.25)
        self.assertEqual(profile["recent7Days"]["playCount"], 3)
        self.assertEqual(profile["recent7Days"]["topMoods"][0]["value"], "calm")
        self.assertEqual(profile["recent30Days"]["playCount"], 4)
        self.assertNotIn(
            "low-confidence",
            [item["value"] for item in profile["topMoods"]],
        )

    def test_recommendation_metadata_profile_reuses_listening_profile_counts(self):
        calm = self._create_track("Calm")
        focus = self._create_track("Focus")
        self._metadata(
            calm,
            mood=["calm"],
            scene=["night"],
            tags=["ambient"],
            energy=30,
        )
        self._metadata(
            focus,
            mood=["focused"],
            scene=["study"],
            tags=["instrumental"],
            energy=50,
        )

        profile = _buildMetadataPreferenceProfile({calm.id: 2, focus.id: 1})

        self.assertEqual(profile["mood_counts"], {"calm": 2, "focused": 1})
        self.assertEqual(profile["scene_counts"], {"night": 2, "study": 1})
        self.assertEqual(
            profile["tag_counts"],
            {"ambient": 2, "instrumental": 1},
        )
        self.assertAlmostEqual(profile["average_energy"], 36.67, places=2)

    def test_empty_profile_is_safe(self):
        profile = build_user_listening_profile(None)

        self.assertEqual(profile["trackCount"], 0)
        self.assertEqual(profile["playCount"], 0)
        self.assertEqual(profile["topMoods"], [])
        self.assertIsNone(profile["averageEnergy"])
        self.assertEqual(profile["recent7Days"]["topScenes"], [])


if __name__ == "__main__":
    unittest.main()
