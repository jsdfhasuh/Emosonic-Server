import json
import os
import unittest

from supysonic.db import Album, Artist, Folder, Track, TrackMetadata
from supysonic.natural_language_track_search import (
    parse_natural_language_track_query,
    search_tracks_by_natural_language,
)

from ..testbase import TestBase


class NaturalLanguageTrackSearchTestCase(TestBase):
    def setUp(self):
        super().setUp()
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.artist = Artist.create(name="Search Artist")
        self.album = Album.create(name="Search Album", artist=self.artist)
        self.track_number = 0

    def _create_track(self, title, genre="pop", play_count=0):
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
        language="en",
        mood=None,
        scene=None,
        tags=None,
        energy=40,
        valence=50,
        danceability=40,
        confidence=0.9,
        provider="llm",
        source="llm",
    ):
        return TrackMetadata.create(
            track=track,
            track_last_modification=track.last_modification,
            language=language,
            mood_json=json.dumps(mood, ensure_ascii=False) if mood else None,
            scene_json=json.dumps(scene, ensure_ascii=False) if scene else None,
            tags_json=json.dumps(tags, ensure_ascii=False) if tags else None,
            energy=energy,
            valence=valence,
            danceability=danceability,
            confidence=confidence,
            provider=provider,
            source=source,
        )

    def test_quiet_cantonese_query_maps_to_filter_and_explains_match(self):
        match = self._create_track("Quiet Cantonese")
        wrong_language = self._create_track("Quiet Mandarin")
        too_loud = self._create_track("Loud Cantonese")
        self._metadata(
            match,
            language="yue",
            mood=["平静"],
            scene=["安静时刻"],
            tags=["粤语流行"],
            energy=35,
        )
        self._metadata(
            wrong_language,
            language="zh",
            mood=["平静"],
            scene=["安静时刻"],
            tags=["华语流行"],
            energy=35,
        )
        self._metadata(
            too_loud,
            language="yue",
            mood=["兴奋"],
            scene=["派对"],
            tags=["粤语流行"],
            energy=85,
        )

        result = search_tracks_by_natural_language("安静的粤语歌", limit=10)

        self.assertFalse(result["fallback"])
        self.assertEqual(result["filters"]["language"], "yue")
        self.assertEqual(result["filters"]["moods"], ["平静"])
        self.assertEqual(result["filters"]["energy_max"], 45)
        self.assertEqual([item["track"].id for item in result["items"]], [match.id])
        self.assertIn("language: yue", result["items"][0]["reasons"])
        self.assertIn("mood: 平静", result["items"][0]["reasons"])
        self.assertIn("energy: 35 <= 45", result["items"][0]["reasons"])

    def test_coding_query_maps_to_focus_scene(self):
        focus = self._create_track("Focus Coding")
        commute = self._create_track("Commute Pop")
        self._metadata(
            focus,
            mood=["平静"],
            scene=["专注"],
            tags=["器乐"],
            energy=45,
        )
        self._metadata(
            commute,
            mood=["明亮"],
            scene=["通勤"],
            tags=["流行"],
            energy=60,
        )

        parsed = parse_natural_language_track_query("写代码听什么")
        result = search_tracks_by_natural_language("写代码听什么", limit=10)

        self.assertEqual(parsed["filters"]["scenes"], ["专注"])
        self.assertEqual([item["track"].id for item in result["items"]], [focus.id])
        self.assertIn("scene: 专注", result["items"][0]["reasons"])

    def test_exact_match_empty_page_does_not_return_fallback(self):
        for index in range(3):
            track = self._create_track(f"Quiet {index + 1}")
            self._metadata(
                track,
                mood=["平静"],
                scene=["安静时刻"],
                tags=["ambient"],
                energy=35,
            )

        result = search_tracks_by_natural_language("安静", limit=2, page=3)

        self.assertFalse(result["fallback"])
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["page"], 3)
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["items"], [])

    def test_no_exact_result_returns_near_scene_recommendations(self):
        near = self._create_track("Melancholy Near", genre="emo")
        self._metadata(
            near,
            mood=["忧郁"],
            scene=["深夜"],
            tags=["emo"],
            energy=45,
        )

        result = search_tracks_by_natural_language("emo", limit=10)

        self.assertTrue(result["fallback"])
        self.assertEqual([item["track"].id for item in result["items"]], [near.id])
        self.assertTrue(
            any(
                reason.startswith("near match:")
                for reason in result["items"][0]["reasons"]
            )
        )

    def test_unknown_query_returns_library_fallback(self):
        track = self._create_track("General Track", play_count=4)

        result = search_tracks_by_natural_language("完全不存在的描述", limit=10)

        self.assertTrue(result["fallback"])
        self.assertEqual(result["matchedRules"], [])
        self.assertEqual([item["track"].id for item in result["items"]], [track.id])
        self.assertEqual(
            result["items"][0]["reasons"],
            ["near match: library fallback"],
        )

    def test_local_metadata_is_not_an_exact_semantic_match(self):
        local = self._create_track("Local Quiet")
        self._metadata(
            local,
            mood=["平静"],
            scene=["安静时刻"],
            tags=["local"],
            energy=30,
            confidence=0.25,
            provider="local",
            source="local",
        )

        result = search_tracks_by_natural_language("安静", limit=10)

        self.assertTrue(result["fallback"])
        self.assertEqual([item["track"].id for item in result["items"]], [local.id])
        self.assertEqual(
            result["items"][0]["reasons"],
            ["near match: library fallback"],
        )


if __name__ == "__main__":
    unittest.main()
