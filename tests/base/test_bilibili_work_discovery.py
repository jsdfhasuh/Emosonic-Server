import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock

from supysonic.album_discovery_catalog import CatalogAlbum, CatalogArtist, CatalogError, CatalogSong, CatalogTrack
from supysonic.album_discovery_public_catalog import PublicAppleMusicCatalog
from supysonic.bilibili_discovery.exploration import build_exploration_plan, explore_task


def lead(title: str, artist: str = "2NE1", performer: str = "CL") -> dict:
    return {
        "kind": "work_lead", "title": title, "artist": artist,
        "performer": performer, "version": "unknown",
    }


class BilibiliWorkDiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = Mock()
        self.songs = {}
        self.albums = {}
        self.credits = {}
        self.catalog.search_seed_songs.side_effect = lambda *_: tuple(self.songs.values())
        self.catalog.get_song.side_effect = self.songs.get
        self.catalog.get_artists.side_effect = self.credits.get
        self.catalog.get_release.side_effect = lambda identifier, **_: self.albums.get(identifier)

    def add_song(self, song_id: str, album_id: str, album: str, *,
                 isrc: str = "", artist_id: str = "99", single: bool = False) -> None:
        track = CatalogTrack(song_id, "Fire", "2NE1", 1, 1, 180000, isrc or None)
        self.songs[song_id] = CatalogSong(track, album, (album_id,), (artist_id,), ("K-Pop",))
        self.credits[song_id] = (CatalogArtist(artist_id, "2NE1"),)
        self.albums[album_id] = CatalogAlbum(
            album_id, album, "2NE1", f"https://music.apple.com/cn/album/{album_id}",
            (track,), is_single=single,
        )

    def explore(self) -> tuple:
        return explore_task(self.catalog, {"kind": "work", "leads": [lead("Fire")]}, {})

    def test_plan_keeps_each_named_work_and_groups_repeated_sources(self) -> None:
        rows = [lead("FIRE"), lead("I AM THE BEST"), lead("Fire", performer="Someone Else")]
        rows.extend(lead(f"Song {i}") for i in range(40))
        rows.append(lead("Song Without Original Credit", artist=""))
        plan, skipped = build_exploration_plan(None, rows, datetime(2026, 11, 28))
        self.assertEqual(len(plan), 42)
        self.assertEqual(plan[0], {"kind": "work", "leads": [rows[0], rows[2]]})
        self.assertEqual(plan[1]["leads"], [rows[1]])
        self.assertEqual(skipped, {"work_artist_unverified": 1})

    def test_verified_original_is_separate_from_onstage_performer(self) -> None:
        self.add_song("71", "17", "Mini Album")
        suggestions, reason = self.explore()
        self.assertEqual(reason, "")
        suggestion, = suggestions
        self.assertEqual(suggestion.kind, "work")
        self.assertEqual(suggestion.song.track.artist, "2NE1")
        self.assertEqual(suggestion.basis, {
            "work_title": "Fire", "original_artist": "2NE1", "original_artist_id": "99",
            "performers": ["CL"], "song_id": "71", "album_id": "17", "album_title": "Mini Album",
            "catalog_version": "studio",
        })
        self.catalog.search_seed_songs.assert_called_once_with("Fire", "2NE1")

    def test_missing_artist_never_falls_back_to_the_stage_performer(self) -> None:
        self.assertEqual(explore_task(self.catalog, {
            "kind": "work", "leads": [lead("Fire", artist="")],
        }, {}), ([], "work_artist_unverified"))
        self.catalog.search_seed_songs.assert_not_called()

    def test_only_verified_edition_of_same_recording_is_selected(self) -> None:
        self.add_song("72", "18", "Fire - Single", isrc="KRA010900001", single=True)
        self.add_song("71", "17", "Mini Album", isrc="KRA010900001")
        self.assertEqual(self.explore()[0][0].basis["album_id"], "17")
        self.add_song("73", "19", "Other Studio Session", isrc="KRA010900002")
        task = {"kind": "work", "leads": [lead("Fire")]}
        self.assertEqual(explore_task(self.catalog, task, {}), ([], "catalog_work_recording_ambiguous"))
        self.assertEqual(task["outcome"], "catalog_work_recording_ambiguous")
        self.assertEqual({item["album_id"] for item in task["verified_releases"]}, {"17", "18", "19"})
        self.add_song("73", "19", "Other Edition")
        self.assertEqual(self.explore(), ([], "catalog_work_recording_ambiguous"))

    def test_incomplete_album_and_wrong_or_ambiguous_artist_ids_fail(self) -> None:
        self.add_song("71", "17", "Mini Album", isrc="KRA010900001")
        self.credits["71"] = (CatalogArtist("999", "2NE1"),)
        self.assertEqual(self.explore(), ([], "catalog_work_artist_unverified"))
        self.credits["71"] = (CatalogArtist("99", "2NE1"),)
        track = self.songs["71"].track
        self.albums["17"] = replace(self.albums["17"], tracks=(replace(track, track_number=2),))
        self.assertEqual(self.explore(), ([], "catalog_release_unverified"))
        self.add_song("71", "17", "Mini Album", isrc="KRA010900001")
        self.add_song("72", "18", "Alternate", isrc="KRA010900001", artist_id="199")
        self.assertEqual(self.explore(), ([], "catalog_work_artist_ambiguous"))

    def test_bad_public_song_page_keeps_other_verified_editions_without_selecting(self) -> None:
        self.add_song("71", "17", "Mini Album")
        public = Mock(spec=PublicAppleMusicCatalog)
        public.search_seed_song_ids.return_value = ("72", "71")
        def read_song(sid):
            if sid == "71":
                return self.songs[sid]
            raise CatalogError("unrecognized page")

        public.get_song.side_effect = read_song
        public.get_artists.side_effect = self.credits.get
        public.get_release.side_effect = lambda identifier, **_: self.albums.get(identifier)

        task = {"kind": "work", "leads": [lead("Fire")]}
        self.assertEqual(explore_task(public, task, {}), ([], "catalog_work_partial"))
        self.assertTrue(task["retryable"])
        self.assertEqual(task["failed_recordings"], {"72": "catalog_page_unavailable"})
        self.assertEqual(task["verified_releases"], [
            {"song_id": "71", "album_id": "17", "album_title": "Mini Album"},
        ])
        public.search_seed_songs.assert_not_called()
        public.search_seed_song_ids.return_value = ("71",)
        self.assertEqual(explore_task(public, task, {}), ([], "catalog_work_partial"))

    def test_search_outage_keeps_a_retryable_outcome(self) -> None:
        self.catalog.search_seed_songs.side_effect = CatalogError("unavailable")
        task = {"kind": "work", "leads": [lead("Fire")]}
        self.assertEqual(explore_task(self.catalog, task, {}), ([], "catalog_work_search_unavailable"))
        self.assertTrue(task["retryable"])
