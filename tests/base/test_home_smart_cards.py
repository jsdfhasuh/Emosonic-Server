import json
import os
import unittest

from unittest.mock import patch

from supysonic.db import (
    Album,
    Artist,
    Folder,
    Track,
    TrackMetadata,
    User,
    User_Play_Activity,
)
from supysonic.home_smart_cards import build_home_smart_cards

from ..testbase import TestBase


class HomeSmartCardsTestCase(TestBase):
    def setUp(self):
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.track_number = 0

    def _create_track(self, title, genre="ambient", artist_name=None, play_count=0):
        self.track_number += 1
        artist = Artist.create(name=artist_name or f"{title} Artist")
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
        mood=None,
        scene=None,
        tags=None,
        language="en",
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
            mood_json=json.dumps(mood, ensure_ascii=False) if mood else None,
            scene_json=json.dumps(scene, ensure_ascii=False) if scene else None,
            tags_json=json.dumps(tags, ensure_ascii=False) if tags else None,
            language=language,
            energy=energy,
            valence=valence,
            danceability=danceability,
            confidence=confidence,
            provider=provider,
            source=source,
        )

    def _create_scene_tracks(
        self,
        prefix,
        count,
        *,
        mood,
        scene,
        tags=None,
        language="en",
        energy=40,
        genre="ambient",
    ):
        tracks = []
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
            tracks.append(track)
        return tracks

    def test_builds_three_cards_without_user_history(self):
        self._create_scene_tracks(
            "Night",
            6,
            mood=["平静"],
            scene=["深夜"],
            tags=["氛围"],
            energy=28,
            genre="ambient",
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

        cards = build_home_smart_cards(None, card_limit=3, track_limit=6)

        self.assertEqual(
            [card["key"] for card in cards],
            ["night", "study", "cantonese_nostalgic"],
        )
        self.assertTrue(all(len(card["tracks"]) == 6 for card in cards))
        self.assertTrue(
            all(item["reason"] for card in cards for item in card["tracks"])
        )

    def test_scene_cards_reuse_preloaded_metadata(self):
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

        with patch("supysonic.mood_scene_playlists._load_metadata_by_track_id") as load:
            cards = build_home_smart_cards(None, card_limit=3, track_limit=6)

        self.assertEqual(len(cards), 3)
        load.assert_not_called()

    def test_local_metadata_does_not_drive_card_primary_results(self):
        night_tracks = self._create_scene_tracks(
            "Night",
            6,
            mood=["平静"],
            scene=["深夜"],
            tags=["氛围"],
            energy=25,
        )
        local_track = self._create_track(
            "Local Night",
            genre="ambient",
            play_count=100,
        )
        self._metadata(
            local_track,
            mood=["平静"],
            scene=["深夜"],
            tags=["氛围"],
            confidence=0.25,
            provider="local",
            source="local",
        )

        cards = build_home_smart_cards(None, card_limit=1, track_limit=6)

        self.assertEqual(cards[0]["key"], "night")
        self.assertEqual(
            [item["track"].id for item in cards[0]["tracks"]],
            [track.id for track in night_tracks],
        )
        self.assertNotIn(
            local_track.id,
            [item["track"].id for item in cards[0]["tracks"]],
        )

    def test_recent_history_card_uses_similar_tracks_when_available(self):
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
        )
        seed = self._create_track("Seed", genre="dream pop")
        self._metadata(
            seed,
            mood=["curious"],
            scene=["studio"],
            tags=["minimal"],
            energy=64,
        )
        similar_tracks = self._create_scene_tracks(
            "Similar",
            6,
            mood=["curious"],
            scene=["studio"],
            tags=["minimal"],
            energy=64,
            genre="dream pop",
        )
        User_Play_Activity.create(track=seed, user=self.user)

        cards = build_home_smart_cards(self.user, card_limit=3, track_limit=6)
        recent_card = cards[2]

        self.assertEqual(recent_card["key"], "recent_similar")
        self.assertEqual(
            [item["track"].id for item in recent_card["tracks"]],
            [track.id for track in similar_tracks],
        )
        self.assertTrue(
            all(
                item["reason"].startswith("Similar to Seed:")
                for item in recent_card["tracks"]
            )
        )

    def test_short_scene_results_fall_back_to_recommendations(self):
        self._create_scene_tracks(
            "Night",
            2,
            mood=["平静"],
            scene=["深夜"],
            tags=["氛围"],
            energy=28,
        )
        for index in range(4):
            self._create_track(
                f"General {index + 1}",
                genre="pop",
            )

        cards = build_home_smart_cards(None, card_limit=1, track_limit=6)

        self.assertEqual(cards[0]["key"], "night")
        self.assertEqual(len(cards[0]["tracks"]), 6)
        self.assertTrue(
            any(
                item["source"] == "recommendation"
                for item in cards[0]["tracks"]
            )
        )

    def test_fallback_cards_do_not_repeat_tracks(self):
        tracks = [
            self._create_track(
                f"General {index + 1:02}",
                genre="pop",
                play_count=20 - index,
            )
            for index in range(18)
        ]

        cards = build_home_smart_cards(None, card_limit=3, track_limit=6)

        self.assertEqual(len(cards), 3)
        card_track_ids = [
            item["track"].id
            for card in cards
            for item in card["tracks"]
        ]
        self.assertEqual(len(card_track_ids), 18)
        self.assertEqual(len(set(card_track_ids)), 18)
        self.assertEqual(card_track_ids, [track.id for track in tracks])


if __name__ == "__main__":
    unittest.main()
