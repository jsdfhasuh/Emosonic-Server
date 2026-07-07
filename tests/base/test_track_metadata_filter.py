import json
import os
import unittest

from supysonic.db import Album, Artist, Folder, Track, TrackMetadata
from supysonic.track_metadata_filter import filter_tracks_by_metadata

from ..testbase import TestBase


class TrackMetadataFilterTestCase(TestBase):
    def setUp(self):
        super().setUp()
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.artist = Artist.create(name="Filter Artist")
        self.album = Album.create(name="Filter Album", artist=self.artist)

    def _create_track(self, title, number, genre="pop"):
        return Track.create(
            disc=1,
            number=number,
            title=title,
            duration=180,
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

    def test_combined_language_and_tag_filter_supports_cantonese_nostalgia(self):
        match = self._create_track("Cantonese Classic", 1)
        wrong_language = self._create_track("Mandarin Classic", 2)
        wrong_tag = self._create_track("Cantonese Pop", 3)
        self._metadata(match, language="yue", tags=["怀旧", "粤语流行"])
        self._metadata(wrong_language, language="zh", tags=["怀旧"])
        self._metadata(wrong_tag, language="yue", tags=["粤语流行"])

        page = filter_tracks_by_metadata(language="粤语", tags=["怀旧"])

        self.assertEqual(page["total"], 1)
        self.assertEqual([item["track"].id for item in page["items"]], [match.id])

    def test_energy_range_filter(self):
        low = self._create_track("Low Energy", 1)
        match = self._create_track("Medium Energy", 2)
        high = self._create_track("High Energy", 3)
        self._metadata(low, energy=10)
        self._metadata(match, energy=35)
        self._metadata(high, energy=80)

        page = filter_tracks_by_metadata(energy_min=20, energy_max=50)

        self.assertEqual([item["track"].id for item in page["items"]], [match.id])

    def test_confidence_and_provider_filters(self):
        high = self._create_track("High Confidence", 1)
        low = self._create_track("Low Confidence", 2)
        local = self._create_track("Local", 3)
        self._metadata(high, confidence=0.8, provider="llm", source="llm")
        self._metadata(low, confidence=0.4, provider="llm", source="llm")
        self._metadata(
            local,
            tags=["pop"],
            confidence=0.2,
            provider="local",
            source="local",
        )

        high_page = filter_tracks_by_metadata(provider="llm", confidence_min=0.7)
        low_page = filter_tracks_by_metadata(
            provider="llm",
            include_low_confidence=True,
            confidence_max=0.5,
        )
        local_page = filter_tracks_by_metadata(provider="local")

        self.assertEqual([item["track"].id for item in high_page["items"]], [high.id])
        self.assertEqual([item["track"].id for item in low_page["items"]], [low.id])
        self.assertEqual([item["track"].id for item in local_page["items"]], [local.id])

    def test_provider_filter_matches_llm_source(self):
        track = self._create_track("Source LLM", 1)
        self._metadata(
            track,
            tags=["source-only"],
            confidence=0.8,
            provider="openai",
            source="llm",
        )

        llm_page = filter_tracks_by_metadata(provider="llm", tags=["source-only"])
        provider_page = filter_tracks_by_metadata(
            provider="openai",
            tags=["source-only"],
        )

        self.assertEqual([item["track"].id for item in llm_page["items"]], [track.id])
        self.assertEqual(
            [item["track"].id for item in provider_page["items"]],
            [track.id],
        )

    def test_default_hides_local_and_low_confidence_metadata(self):
        high = self._create_track("High Quality", 1)
        low = self._create_track("Low Confidence", 2)
        local = self._create_track("Local", 3)
        self._metadata(high, tags=["dreamy"], confidence=0.8)
        self._metadata(low, tags=["dreamy"], confidence=0.3)
        self._metadata(
            local,
            tags=["dreamy"],
            confidence=0.2,
            provider="local",
            source="local",
        )

        default_page = filter_tracks_by_metadata(tags=["dreamy"])
        expanded_page = filter_tracks_by_metadata(
            tags=["dreamy"],
            include_local=True,
            include_low_confidence=True,
        )

        self.assertEqual(
            [item["track"].id for item in default_page["items"]],
            [high.id],
        )
        self.assertEqual(
            [item["track"].id for item in expanded_page["items"]],
            [high.id, local.id, low.id],
        )

    def test_pagination_is_stable(self):
        tracks = []
        for index in range(5):
            track = self._create_track(f"Track {index}", index + 1)
            tracks.append(track)
            self._metadata(track, tags=["paged"], confidence=0.9)

        first = filter_tracks_by_metadata(tags=["paged"], page=1, page_size=2)
        second = filter_tracks_by_metadata(tags=["paged"], page=2, page_size=2)
        third = filter_tracks_by_metadata(tags=["paged"], page=3, page_size=2)

        self.assertEqual(first["total"], 5)
        self.assertEqual(first["pages"], 3)
        self.assertEqual(
            [item["track"].id for item in first["items"]],
            [tracks[0].id, tracks[1].id],
        )
        self.assertEqual(
            [item["track"].id for item in second["items"]],
            [tracks[2].id, tracks[3].id],
        )
        self.assertEqual([item["track"].id for item in third["items"]], [tracks[4].id])

    def test_tracks_without_metadata_do_not_error(self):
        self._create_track("No Metadata", 1)

        page = filter_tracks_by_metadata(tags=["anything"])

        self.assertEqual(page["total"], 0)
        self.assertEqual(page["items"], [])


if __name__ == "__main__":
    unittest.main()
