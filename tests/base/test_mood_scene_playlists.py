import json
import os
import unittest

from supysonic.db import Album, Artist, Folder, Track, TrackMetadata, User
from supysonic.mood_scene_playlists import (
    get_mood_scene_playlist,
    list_mood_scene_playlist_keys,
)
from supysonic.recommendation_feedback import set_recommendation_feedback

from ..testbase import TestBase


class MoodScenePlaylistsTestCase(TestBase):
    def setUp(self):
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.artist = Artist.create(name="Playlist Artist")
        self.album = Album.create(name="Playlist Album", artist=self.artist)

    def _create_track(
        self,
        title,
        number,
        genre="ambient",
        artist=None,
        album=None,
        play_count=0,
    ):
        artist = artist or self.artist
        album = album or self.album
        return Track.create(
            disc=1,
            number=number,
            title=title,
            duration=180,
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

    def _create_artist_track(self, title, number, artist_name, genre="ambient"):
        artist = Artist.create(name=artist_name)
        album = Album.create(name=f"{artist_name} Album", artist=artist)
        return self._create_track(
            title,
            number,
            genre=genre,
            artist=artist,
            album=album,
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

    def test_night_playlist_uses_high_quality_metadata_and_explains_results(self):
        night = self._create_artist_track("Night", 1, "Night Artist", genre="jazz")
        tag_only = self._create_artist_track("Tag Only", 2, "Tag Artist", genre="jazz")
        local = self._create_artist_track("Local", 3, "Local Artist", genre="jazz")
        self._metadata(
            night,
            mood=["平静"],
            scene=["深夜"],
            tags=["氛围"],
            energy=30,
        )
        self._metadata(tag_only, tags=["ambient"], energy=80)
        self._metadata(
            local,
            mood=["平静"],
            scene=["深夜"],
            tags=["氛围"],
            confidence=0.25,
            provider="local",
            source="local",
        )

        results = get_mood_scene_playlist("night", limit=3)

        self.assertEqual(
            [result["track"].id for result in results],
            [night.id, tag_only.id],
        )
        self.assertTrue(all(result["reasons"] for result in results))
        self.assertTrue(
            any("scene: 深夜" in reason for reason in results[0]["reasons"])
        )
        self.assertNotIn(local.id, [result["track"].id for result in results])

    def test_study_playlist_applies_scene_and_energy_rules_with_stable_order(self):
        focused = self._create_artist_track("Focused", 1, "Focused Artist")
        quiet = self._create_artist_track("Quiet", 2, "Quiet Artist")
        quiet.play_count = 8
        quiet.save()
        workout = self._create_artist_track("Workout", 3, "Workout Artist")
        self._metadata(
            focused,
            mood=["沉思"],
            scene=["学习"],
            tags=["器乐"],
            energy=42,
            danceability=20,
        )
        self._metadata(
            quiet,
            mood=["平静"],
            scene=["安静时刻"],
            energy=45,
            danceability=30,
        )
        self._metadata(
            workout,
            mood=["兴奋"],
            scene=["运动"],
            tags=["rock"],
            energy=95,
            danceability=90,
        )

        first = get_mood_scene_playlist("study", limit=2)
        second = get_mood_scene_playlist("学习", limit=2)

        self.assertEqual(
            [result["track"].id for result in first],
            [result["track"].id for result in second],
        )
        self.assertEqual(
            [result["track"].id for result in first],
            [focused.id, quiet.id],
        )
        self.assertNotIn(workout.id, [result["track"].id for result in first])
        self.assertTrue(any("energy: 42" in reason for reason in first[0]["reasons"]))

    def test_cantonese_playlist_matches_language_and_tags(self):
        language_match = self._create_artist_track(
            "Language Match",
            1,
            "Language Artist",
            genre="pop",
        )
        tag_match = self._create_artist_track("Tag Match", 2, "Tag Artist", genre="pop")
        other = self._create_artist_track("Other", 3, "Other Artist", genre="pop")
        self._metadata(language_match, language="yue", tags=["流行"])
        self._metadata(tag_match, language="zh", tags=["粤语流行"])
        self._metadata(other, language="zh", tags=["华语流行"])

        results = get_mood_scene_playlist("cantonese", limit=3)

        self.assertEqual(
            [result["track"].id for result in results],
            [language_match.id, tag_match.id],
        )
        self.assertTrue(
            any("language: yue" in reason for reason in results[0]["reasons"])
        )
        self.assertTrue(
            any("tags: 粤语流行" in reason for reason in results[1]["reasons"])
        )

    def test_playlist_falls_back_to_genre_and_popularity_when_results_are_short(self):
        semantic = self._create_artist_track(
            "Semantic",
            1,
            "Semantic Artist",
            genre="jazz",
        )
        fallback_popular = self._create_artist_track(
            "Fallback Popular",
            2,
            "Fallback Popular Artist",
            genre="ambient",
        )
        fallback_low = self._create_artist_track(
            "Fallback Low",
            3,
            "Fallback Low Artist",
            genre="ambient",
        )
        ignored = self._create_artist_track(
            "Ignored",
            4,
            "Ignored Artist",
            genre="metal",
        )
        fallback_popular.play_count = 20
        fallback_popular.save()
        self._metadata(semantic, mood=["平静"], scene=["深夜"], energy=25)

        results = get_mood_scene_playlist("night", limit=3)

        self.assertEqual(
            [result["track"].id for result in results],
            [semantic.id, fallback_popular.id, fallback_low.id],
        )
        fallback_popular_result = results[1]["reasons"][0]
        self.assertIn("fallback genre: ambient", fallback_popular_result)
        self.assertEqual(fallback_popular_result, "fallback genre: ambient")
        self.assertNotIn(ignored.id, [result["track"].id for result in results])

    def test_playlist_filters_negative_feedback(self):
        seed = self._create_artist_track("Seed", 1, "Seed Artist", genre="ambient")
        hidden = self._create_artist_track(
            "Hidden",
            2,
            "Hidden Artist",
            genre="ambient",
        )
        visible = self._create_artist_track(
            "Visible",
            3,
            "Visible Artist",
            genre="ambient",
        )
        self._metadata(seed, mood=["平静"], scene=["深夜"], energy=25)
        self._metadata(hidden, mood=["平静"], scene=["深夜"], energy=25)
        self._metadata(visible, mood=["平静"], scene=["深夜"], energy=25)
        set_recommendation_feedback(self.user, str(seed.id), "dislike")
        set_recommendation_feedback(self.user, str(hidden.artist_id), "hide_artist")

        results = get_mood_scene_playlist("night", limit=10, user=self.user)

        self.assertEqual([result["track"].id for result in results], [visible.id])

    def test_empty_or_unknown_playlist_returns_empty_list(self):
        self.assertEqual(get_mood_scene_playlist("night", limit=10), [])
        self.assertEqual(get_mood_scene_playlist("unknown", limit=10), [])
        self.assertEqual(get_mood_scene_playlist("night", limit=0), [])

    def test_lists_all_first_batch_scene_keys(self):
        self.assertEqual(
            list_mood_scene_playlist_keys(),
            [
                "night",
                "study",
                "commute",
                "relax",
                "high_energy",
                "low_energy",
                "cantonese",
                "nostalgic",
                "emo",
            ],
        )


if __name__ == "__main__":
    unittest.main()
