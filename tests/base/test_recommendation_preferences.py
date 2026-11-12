import os
import unittest

from datetime import datetime, timedelta
from types import SimpleNamespace
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
from supysonic.user_listening_profile import (
    build_metadata_preference_profile_from_track_counts,
    build_recommendation_play_weights,
    build_user_listening_profile,
    get_recommendation_language,
)

from ..testbase import TestBase


class RecommendationPreferencesTestCase(TestBase):
    def setUp(self):
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.artist = Artist.create(name="Preference Artist")
        self.album = Album.create(name="Preference Album", artist=self.artist)
        self.reference_time = datetime(2026, 11, 12, 0, 0, 0)
        self.track_number = 0

    def _track(self, title, genre="Pop"):
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

    def _play(self, track, count=1, *, days=0, user=None):
        values = {
            "track": track,
            "user": user or self.user,
            "time": self.reference_time - timedelta(days=days),
        }
        for offset in range(0, count, 100):
            User_Play_Activity.insert_many(
                [dict(values) for _ in range(min(100, count - offset))]
            ).execute()

    def _metadata(self, track, language, *, mood=None, energy=None, confidence=0.9):
        return TrackMetadata.create(
            track=track,
            track_last_modification=1,
            language=language,
            mood_json=mood,
            energy=energy,
            provider="llm",
            source="llm",
            confidence=confidence,
        )

    def test_recent_chinese_outweighs_thousands_of_old_korean_plays(self):
        old = self._track("Old Korean")
        recent = self._track("Recent Chinese")
        self._play(old, 2000, days=90)
        self._play(recent, 10, days=1)

        weights = build_recommendation_play_weights(self.user, self.reference_time)

        self.assertEqual(set(weights), {old.id, recent.id})
        self.assertAlmostEqual(sum(weights.values()), 1)
        self.assertGreater(weights[recent.id], 0.7)
        self.assertLess(weights[old.id], 0.3)

    def test_recent_weights_have_three_day_half_life(self):
        current = self._track("Current")
        older = self._track("Three days old")
        self._play(current)
        self._play(older, days=3)

        weights = build_recommendation_play_weights(self.user, self.reference_time)

        self.assertAlmostEqual(weights[current.id], 0.15 + 0.7 * 2 / 3)
        self.assertAlmostEqual(weights[older.id], 0.15 + 0.7 / 3)

    def test_without_recent_activity_falls_back_to_lifetime_ratios(self):
        first = self._track("First")
        second = self._track("Second")
        self._play(first, 3, days=8)
        self._play(second, days=100)

        weights = build_recommendation_play_weights(self.user, self.reference_time)

        self.assertEqual(weights, {first.id: 0.75, second.id: 0.25})

    def test_recent_window_includes_seven_day_boundary_but_not_future(self):
        boundary = self._track("Boundary")
        older = self._track("Outside window")
        future = self._track("Future")
        self._play(boundary, days=7)
        self._play(older, days=7 + 1 / 86400)
        self._play(future, days=-1)

        weights = build_recommendation_play_weights(self.user, self.reference_time)

        self.assertAlmostEqual(weights[boundary.id], 0.8)
        self.assertAlmostEqual(weights[older.id], 0.1)
        self.assertAlmostEqual(weights[future.id], 0.1)

    def test_other_user_activity_does_not_affect_weights(self):
        own = self._track("Own")
        other = self._track("Other")
        self._play(own, days=30)
        self._play(other, 10, user=User.get(User.name == "bob"))

        weights = build_recommendation_play_weights(self.user, self.reference_time)

        self.assertEqual(weights, {own.id: 1.0})

    def test_empty_history_and_absent_user_are_safe(self):
        self.assertEqual(build_recommendation_play_weights(None), {})
        self.assertEqual(build_recommendation_play_weights(self.user), {})

    def test_default_reference_time_uses_server_clock(self):
        track = self._track("Recent")
        self._play(track, days=1)
        with patch(
            "supysonic.user_listening_profile.now",
            return_value=self.reference_time,
        ) as clock:
            result = build_recommendation_play_weights(self.user)
        clock.assert_called_once_with()
        self.assertEqual(result, {track.id: 1.0})

    def test_new_track_play_between_queries_does_not_dilute_snapshot_weights(self):
        existing = self._track("Existing")
        arriving = self._track("Arriving")
        self._play(existing)
        original_select = User_Play_Activity.select
        select_count = 0

        def select_with_arriving_play(*fields):
            nonlocal select_count
            select_count += 1
            if select_count == 2:
                self._play(arriving)
            return original_select(*fields)

        with patch.object(User_Play_Activity, "select", side_effect=select_with_arriving_play):
            weights = build_recommendation_play_weights(self.user, self.reference_time)

        self.assertEqual(weights, {existing.id: 1.0})

    def test_fractional_semantic_weights_are_not_truncated(self):
        calm = self._track("Calm")
        lively = self._track("Lively")
        self._metadata(calm, "zh", mood='["calm"]', energy=20)
        self._metadata(lively, "ko", mood='["lively"]', energy=80)

        profile = build_metadata_preference_profile_from_track_counts(
            {calm.id: 0.7, lively.id: 0.3}
        )

        self.assertEqual(profile["mood_counts"], {"calm": 0.7, "lively": 0.3})
        self.assertEqual(profile["language_counts"], {"zh": 0.7, "ko": 0.3})
        self.assertEqual(profile["average_energy"], 38)

    def test_language_only_metadata_and_explicit_genre_contribute(self):
        mandarin = self._track("Language only")
        cantonese = self._track("Genre only", genre="Cantopop")
        unknown = self._track("No language", genre="K-Pop")
        self._metadata(mandarin, "Mandarin")

        profile = build_metadata_preference_profile_from_track_counts(
            {mandarin.id: 0.6, cantonese.id: 0.3, unknown.id: 0.1}
        )

        self.assertEqual(profile["language_counts"], {"zh": 0.6, "yue": 0.3})
        self.assertEqual(profile["mood_counts"], {})
        self.assertIsNone(profile["average_energy"])

    def test_metadata_and_track_labels_are_fetched_in_bulk(self):
        first = self._track("First", genre="Mandopop")
        second = self._track("Second", genre="Cantopop")
        with patch.object(TrackMetadata, "select", wraps=TrackMetadata.select) as metadata_query:
            with patch.object(Track, "select", wraps=Track.select) as track_query:
                profile = build_metadata_preference_profile_from_track_counts(
                    {first.id: 0.6, second.id: 0.4}
                )

        self.assertEqual(profile["language_counts"], {"zh": 0.6, "yue": 0.4})
        self.assertEqual(metadata_query.call_count, 1)
        self.assertEqual(track_query.call_count, 1)

    def test_invalid_weights_do_not_pollute_metadata_scores(self):
        track = self._track("Invalid weight", genre="Mandopop")
        self._metadata(track, "zh", mood='["calm"]')
        for weight in (float("nan"), float("inf"), -1, "bad", None):
            with self.subTest(weight=weight):
                profile = build_metadata_preference_profile_from_track_counts({track.id: weight})
                self.assertEqual(profile["language_counts"], {})
                self.assertEqual(profile["mood_counts"], {})

    def test_public_statistics_keep_integer_counts(self):
        track = self._track("Stats")
        self._metadata(track, "en", mood='["calm"]', energy=30)
        self._play(track, 2, days=1)

        profile = build_user_listening_profile(self.user, self.reference_time)

        self.assertIs(type(profile["playCount"]), int)
        self.assertIs(type(profile["moodCounts"]["calm"]), int)
        self.assertIs(type(profile["languageCounts"]["en"]), int)
        self.assertEqual(profile["playCount"], 2)
        self.assertEqual(profile["recent7Days"]["playCount"], 2)


class RecommendationLanguageTestCase(unittest.TestCase):
    def _metadata(self, language, confidence=0.9, provider="llm"):
        return SimpleNamespace(language=language, confidence=confidence, provider=provider)

    def test_normalizes_language_aliases_without_merging_cantonese(self):
        cases = {
            "zh": "zh", " Mandarin ": "zh", "cmn": "zh", "zh_CN": "zh",
            "ZH-HANT": "zh", "Cantonese": "yue", "zh-yue": "yue",
            "Korean": "ko", "kor": "ko", "Japanese": "ja", "English": "en",
        }
        track = SimpleNamespace(genre="Pop")
        for label, expected in cases.items():
            with self.subTest(label=label):
                self.assertEqual(
                    get_recommendation_language(track, self._metadata(label)),
                    expected,
                )

    def test_language_only_metadata_is_valid_at_quality_boundary(self):
        self.assertEqual(
            get_recommendation_language(SimpleNamespace(genre=None), self._metadata("zh", 0.5)),
            "zh",
        )

    def test_missing_invalid_or_untrusted_metadata_is_unknown(self):
        track = SimpleNamespace(genre="Pop")
        metadata_cases = [
            None,
            self._metadata("zh", 0.49),
            self._metadata("zh", None),
            self._metadata("zh", float("nan")),
            self._metadata("zh", float("inf")),
            self._metadata("zh", provider="local"),
            self._metadata("unknown"),
            self._metadata("ko,en"),
            self._metadata("['Cantonese']"),
        ]
        for metadata in metadata_cases:
            with self.subTest(metadata=metadata):
                self.assertIsNone(get_recommendation_language(track, metadata))

    def test_explicit_genres_can_supply_missing_language(self):
        cases = {
            "Mandopop": "zh",
            "\u56fd\u8bed\u6d41\u884c": "zh",
            "Cantopop": "yue",
            "\u7ca4\u8bed\u6d41\u884c": "yue",
        }
        for genre, expected in cases.items():
            with self.subTest(genre=genre):
                self.assertEqual(
                    get_recommendation_language(SimpleNamespace(genre=genre), None),
                    expected,
                )

    def test_known_metadata_takes_precedence_over_genre(self):
        self.assertEqual(
            get_recommendation_language(SimpleNamespace(genre="Mandopop"), self._metadata("yue")),
            "yue",
        )

    def test_generic_genres_titles_and_artist_names_are_not_language_evidence(self):
        for genre in (None, "", "Pop", "K-Pop", "J-Pop", "Chinese Music"):
            with self.subTest(genre=genre):
                track = SimpleNamespace(
                    genre=genre,
                    title="\u4e2d\u6587\u6807\u9898",
                    artist="Korean Artist",
                )
                self.assertIsNone(get_recommendation_language(track, None))


if __name__ == "__main__":
    unittest.main()
