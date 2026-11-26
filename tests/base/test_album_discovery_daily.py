import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
from typing import List, Optional, Tuple
from unittest.mock import patch

from peewee import IntegrityError

from supysonic.album_discovery_catalog import CatalogAlbum, CatalogTrack
from supysonic.album_discovery_daily import album_discovery_metrics
from supysonic.album_discovery_oracle import DeliveryManifest
from supysonic.db import (
    Album,
    AlbumDiscoveryOrder,
    AlbumDiscoveryPlacement,
    AlbumDiscoveryRelease,
    AlbumDiscoverySelection,
    Artist,
    Folder,
    Playlist,
    StarredTrack,
    Track,
    User,
    User_Play_Activity,
    connection_scope,
)
from supysonic.recommend import (
    RECOMMENDED_PLAYLIST_COMMENT,
    _save_new_recommended_playlist,
    buildRecommendationReasonMap,
    create_recommend_playlist,
    getRecommendedPlaylistForDay,
    getRecommendPlaylistName,
)
from supysonic.recommendation_feedback import set_recommendation_feedback

from ..testbase import TestBase


class AlbumDiscoveryDailyTestCase(TestBase):
    __with_api__ = True
    LIBRARY = "main"
    FIRST_DAY = "2026-11-27"
    READY_AT = datetime(2026, 11, 26, 20)

    def setUp(self) -> None:
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.other = User.get(User.name == "bob")
        self.root = Folder.create(root=True, name="Root", path="/discovery-library")
        self.number = 0
        self.clock = patch(
            "supysonic.album_discovery_daily.database_now",
            return_value=self.READY_AT + timedelta(days=3),
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def _album(self, artist_name: str, title: str) -> Album:
        return Album.create(name=title, artist=Artist.create(name=artist_name))

    def _track(
        self, title: str, album: Optional[Album] = None, number: int = 1,
        duration: int = 180, path: Optional[Path] = None,
    ) -> Track:
        self.number += 1
        album = album or self._album(f"Artist {self.number}", f"Album {self.number}")
        return Track.create(
            title=title,
            disc=1,
            number=number,
            duration=duration,
            artist=album.artist,
            album=album,
            path=str(path) if path is not None else f"/discovery-library/{self.number}.flac",
            bitrate=320,
            last_modification=1,
            root_folder=self.root,
            folder=self.root,
        )

    def _store_ready_selection(
        self,
        album_id: str,
        track: Track,
        *,
        priority: int = 0,
        album_tracks: Optional[List[Track]] = None,
        ready_at: Optional[datetime] = READY_AT,
        seed_titles: Tuple[str, ...] = (),
    ) -> AlbumDiscoverySelection:
        song_id = album_id + "1"
        song_tracks = [
            (song_id if item.id == track.id else f"{album_id}{index + 100}", item)
            for index, item in enumerate(album_tracks or [track])
        ]
        release = AlbumDiscoveryRelease.create(
            library_key=self.LIBRARY,
            album_id=album_id,
            album_url=f"https://music.apple.com/cn/album/fixture/{album_id}",
            title=track.album.name,
            artist=track.artist.name,
            expected_tracks=json.dumps({"tracks": [
                {"song_id": catalog_id} for catalog_id, _ in song_tracks
            ]}),
            reconciliation=json.dumps({"tracks": [
                {"song_id": catalog_id, "track_id": str(item.id), "status": "verified"}
                for catalog_id, item in song_tracks
            ]}),
            ready_at=ready_at,
        )
        return AlbumDiscoverySelection.create(
            release=release,
            user=self.user,
            representative_song_id=song_id,
            representative_track=track,
            priority=priority,
            evidence=json.dumps([
                {"song_id": song_id, "seed_title": title, "selected": True}
                for title in seed_titles
            ]),
        )

    def _previous_playlist(self, day: str, tracks: List[Track]) -> Playlist:
        return Playlist.create(
            user=self.user,
            name=getRecommendPlaylistName(self.user, day),
            comment=RECOMMENDED_PLAYLIST_COMMENT,
            tracks=",".join(str(track.id) for track in tracks),
        )

    def _ordinary(self, count: int) -> List[Track]:
        return [self._track(f"Ordinary song {number}") for number in range(count)]

    def _create_daily(self, day: str, tracks: List[Track]) -> int:
        with patch("supysonic.recommend._buildRecommendedTracks", return_value=tracks):
            return create_recommend_playlist(
                num_songs=len(tracks), user=self.user, day=day, config=self.config
            )

    def test_shoebox_cn_uses_original_11_positions_and_recommends_only_track_11(self) -> None:
        from supysonic.album_discovery_delivery import reconcile_album

        fixture_path = (
            Path(__file__).resolve().parents[1] / "fixtures" / "album_discovery" / "shoebox_cn.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        catalog = CatalogAlbum(
            fixture["album_id"], fixture["title"], fixture["artist"], fixture["album_url"],
            tuple(CatalogTrack(**entry, artist=fixture["artist"]) for entry in fixture["tracks"]),
        )
        representative_song_id = fixture["representative_song_id"]
        album = self._album(fixture["artist"], fixture["title"])
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        self.root = Folder.create(root=True, name="SHOEBOX fixture", path=str(root))
        indexed_tracks = {}
        files = []
        media_tags = {}
        for item in catalog.tracks:
            relative_path = Path(catalog.artist) / catalog.title / f"{item.track_number:02d}.flac"
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            content = f"Synthetic SHOEBOX track {item.track_number}".encode("utf-8")
            target.write_bytes(content)
            files.append({
                "id": len(files),
                "path": relative_path.as_posix(),
                "size": len(content),
                "sha256": sha256(content).hexdigest(),
            })
            media_tags[target.resolve()] = SimpleNamespace(
                album=catalog.title,
                albumartist=catalog.artist,
                artist=item.artist,
                title=item.title,
                disc=item.disc_number,
                track=item.track_number,
                length=item.duration_ms / 1000,
                isrc=None,
            )
            indexed_tracks[item.song_id] = self._track(
                item.title, album, item.track_number, item.duration_ms // 1000, path=target
            )
        batch_id = "52c74a4173694367a34c45c92d1007be"
        manifest_data = {"id": batch_id, "url": catalog.url, "files": files}
        canonical = json.dumps(manifest_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        manifest = DeliveryManifest.from_payload(
            dict(manifest_data, state="acknowledged", manifestSha256=sha256(canonical.encode()).hexdigest()),
            batch_id,
            catalog.album_id,
        )
        with patch(
            "supysonic.album_discovery_delivery.MediaFile",
            side_effect=lambda path: media_tags[Path(path).resolve()],
        ):
            reconciled = reconcile_album(catalog, representative_song_id, root, (manifest,))
        self.assertTrue(reconciled.ready, reconciled.missing)
        self.assertEqual(str(reconciled.representative_track_id), str(indexed_tracks[representative_song_id].id))
        self.assertEqual(
            {str(track.id) for track in indexed_tracks.values()},
            {match["track_id"] for match in reconciled.to_payload()["tracks"]},
        )
        self.assertEqual(len({track.id for track in indexed_tracks.values()}), 11)
        self.assertEqual(
            [track.number for track in indexed_tracks.values()],
            [1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12],
        )
        representative = indexed_tracks[representative_song_id]
        self.assertEqual((representative.title, representative.number), ("EYES NOSE LIPS", 11))

        selection = self._store_ready_selection(
            fixture["album_id"], representative, album_tracks=list(indexed_tracks.values()),
            seed_titles=("Seed title A", "Seed title B"),
        )
        selection.release.expected_tracks = json.dumps({"tracks": catalog.to_payload()["tracks"]})
        selection.release.reconciliation = json.dumps(reconciled.to_payload())
        selection.release.album_url = fixture["album_url"]
        selection.release.save()
        selection.representative_song_id = representative_song_id
        evidence = json.loads(selection.evidence)
        for item in evidence:
            item["song_id"] = selection.representative_song_id
        selection.evidence = json.dumps(evidence)
        selection.save()

        User_Play_Activity.create(user=self.user, track=self._track("Played seed"))
        regular = self._ordinary(50)
        existing = self._previous_playlist(fixture["previous_day"], regular)
        self.assertEqual(self._create_daily(fixture["previous_day"], regular), 0)
        self.assertEqual(Playlist[existing.id].tracks, existing.tracks)
        self.assertFalse(AlbumDiscoveryPlacement.select().exists())

        # The ordinary daily rank already selected the representative at a lower slot.
        ordinary_rank = regular[:49]
        ordinary_rank.insert(41, representative)
        self.assertEqual(self._create_daily(fixture["placement_day"], ordinary_rank), 1)
        actual = getRecommendedPlaylistForDay(self.user, fixture["placement_day"])
        actual_ids = [track.id for track in actual.get_tracks()]
        expected = regular[:3] + [representative] + regular[3:49]
        self.assertEqual(actual_ids, [track.id for track in expected])
        self.assertEqual(len(actual_ids), 50)
        self.assertEqual(actual_ids.count(representative.id), 1)
        saved = AlbumDiscoveryPlacement.get(AlbumDiscoveryPlacement.selection == selection)
        self.assertEqual(
            (saved.playlist_id, saved.track_id, saved.position), (actual.id, representative.id, 4)
        )
        self.assertEqual(saved.day, date(2026, 11, 27))
        with patch("supysonic.recommend.getRecommendationDay", return_value=fixture["placement_day"]):
            response = self.client.get(
                "/rest/getRecommendedPlaylists.view",
                query_string={"u": "alice", "p": "Alic3", "c": "tests", "f": "json", "count": 50},
            )
        entry = response.get_json()["subsonic-response"]["playlist"]["entry"][3]
        self.assertEqual(entry["id"], str(representative.id))
        self.assertIn("《Seed title A》和《Seed title B》", entry["recommendReason"])
        self.assertEqual(self._create_daily(fixture["placement_day"], regular), 0)
        self.assertEqual(Playlist[actual.id].tracks, actual.tracks)
        self.assertEqual(self._create_daily("2026-11-28", regular), 1)
        self.assertEqual(AlbumDiscoveryPlacement.select().count(), 1)
        self.assertFalse(AlbumDiscoveryOrder.select().exists())

        reasons = buildRecommendationReasonMap(self.user, [representative], playlist=actual)
        self.assertIn("《Seed title A》和《Seed title B》", reasons[str(representative.id)])
        non_owner = buildRecommendationReasonMap(self.other, [representative], playlist=actual)
        self.assertNotIn("Seed title", non_owner[str(representative.id)])
        unrelated = buildRecommendationReasonMap(self.user, [representative], playlist=existing)
        self.assertNotIn("Seed title", unrelated[str(representative.id)])

    def test_skips_played_disliked_recent_and_not_ready_representatives(self) -> None:
        ordinary = self._ordinary(3)
        unavailable, played, disliked, recent, allowed = [
            self._track(label) for label in ("Pending", "Played", "Disliked", "Recent", "Allowed")
        ]
        self._store_ready_selection("301", unavailable, ready_at=None, priority=0)
        self._store_ready_selection("302", played, priority=1)
        self._store_ready_selection("303", disliked, priority=2)
        self._store_ready_selection("304", recent, priority=3)
        eligible = self._store_ready_selection("305", allowed, priority=4)
        User_Play_Activity.create(user=self.user, track=played, time=datetime(2023, 1, 1))
        set_recommendation_feedback(self.user, str(disliked.album_id), "hide_album")
        self._previous_playlist("2026-11-25", [recent])

        playlist, created, tracks = _save_new_recommended_playlist(
            self.user, self.FIRST_DAY, ordinary, len(ordinary), 3
        )
        self.assertTrue(created)
        self.assertEqual(tracks, [ordinary[0], ordinary[1], allowed])
        placement = AlbumDiscoveryPlacement.get()
        self.assertEqual((placement.selection_id, placement.position), (eligible.id, 3))
        self.assertEqual(placement.playlist_id, playlist.id)

    def test_does_not_place_stale_ready_release_after_track_removal(self) -> None:
        ordinary = self._ordinary(4)
        representative = self._track("Representative")
        missing = self._track("No longer indexed", representative.album)
        self._store_ready_selection("306", representative, album_tracks=[representative, missing])
        missing.delete_instance()

        playlist, created, tracks = _save_new_recommended_playlist(
            self.user, self.FIRST_DAY, ordinary, len(ordinary), 3
        )
        self.assertTrue(created)
        self.assertEqual(tracks, ordinary)
        self.assertEqual(playlist.get_tracks(), ordinary)
        self.assertFalse(AlbumDiscoveryPlacement.select().exists())

    def test_preserves_artist_cap_and_shifts_other_ready_albums_to_future_days(self) -> None:
        ordinary = self._ordinary(10)
        shared_artist = self._track("Same artist as a selected song", ordinary[1].album)
        scheduled_first = self._store_ready_selection("401", shared_artist, priority=1)
        remaining = self._track("Distinct discovery")
        scheduled_second = self._store_ready_selection("402", remaining, priority=2)

        _, _, day_one = _save_new_recommended_playlist(self.user, self.FIRST_DAY, ordinary, 10, 3)
        self.assertEqual(day_one[3], shared_artist)
        self.assertEqual([track for track in day_one if track != shared_artist], ordinary[:1] + ordinary[2:])
        self.assertEqual(sum(track.artist_id == shared_artist.artist_id for track in day_one), 1)
        _, _, day_two = _save_new_recommended_playlist(self.user, "2026-11-28", ordinary, 10, 3)
        self.assertEqual(day_two[3], remaining)
        self.assertEqual(
            [row.selection_id for row in AlbumDiscoveryPlacement.select().order_by(AlbumDiscoveryPlacement.day)],
            [scheduled_first.id, scheduled_second.id],
        )

    def test_first_save_and_placement_commit_together_under_concurrency(self) -> None:
        ordinary = self._ordinary(4)
        self._store_ready_selection("501", self._track("Representative"))
        with patch("supysonic.recommend.record_daily_discovery", side_effect=IntegrityError("forced")):
            with self.assertRaises(IntegrityError):
                _save_new_recommended_playlist(self.user, self.FIRST_DAY, ordinary, 4, 3)
        self.assertIsNone(getRecommendedPlaylistForDay(self.user, self.FIRST_DAY))
        self.assertFalse(AlbumDiscoveryPlacement.select().exists())

        barrier = Barrier(2, timeout=5)

        def generate() -> Tuple[Optional[Playlist], bool, List[Track]]:
            with connection_scope():
                barrier.wait()
                return _save_new_recommended_playlist(self.user, self.FIRST_DAY, ordinary, 4, 3)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(generate), pool.submit(generate)
            results = first.result(timeout=10), second.result(timeout=10)
        self.assertCountEqual([result[1] for result in results], [True, False])
        self.assertEqual(results[0][0].id, results[1][0].id)
        self.assertEqual(AlbumDiscoveryPlacement.select().count(), 1)

    def test_metrics_observe_only_this_users_listening_within_window_and_keep_history(self) -> None:
        ordinary = self._ordinary(4)
        representative = self._track("Representative")
        sibling = self._track("Same album", representative.album)
        late_sibling = self._track("Outside observation window", representative.album)
        selection = self._store_ready_selection(
            "601", representative, album_tracks=[representative, sibling, late_sibling]
        )
        AlbumDiscoveryOrder.create(
            library_key=self.LIBRARY,
            album_id=selection.release.album_id,
            storefront="cn",
            quota_date=self.READY_AT.date(),
            status="submitted",
            created_at=self.READY_AT - timedelta(hours=2),
        )
        playlist, _, _ = _save_new_recommended_playlist(self.user, self.FIRST_DAY, ordinary, 4, 3)
        placement = AlbumDiscoveryPlacement.get()
        placed = datetime(2026, 11, 27, 0, 5)
        placement.created_at = placed
        placement.save()
        first = placed + timedelta(days=1)
        plays = [
            (self.user, representative, placed - timedelta(seconds=1)),
            (self.other, representative, placed),
            (self.user, representative, first),
            (self.user, representative, first),
            (self.user, representative, first + timedelta(days=2)),
            (self.user, sibling, first + timedelta(days=6)),
            (self.user, late_sibling, first + timedelta(days=7, seconds=1)),
            (self.user, ordinary[0], first + timedelta(days=3)),
        ]
        for user, track, listened in plays:
            User_Play_Activity.create(user=user, track=track, time=listened)
        StarredTrack.create(user=self.user, starred=representative, date=first)
        playlist.delete_instance()

        metrics = album_discovery_metrics(self.user, self.LIBRARY, now=placed + timedelta(days=10))
        self.assertEqual((metrics["submittedAlbums"], metrics["readyAlbums"]), (1, 1))
        observed = metrics["placements"][0]
        self.assertEqual(observed["playlistId"], str(playlist.id))
        self.assertEqual(observed["firstObservedAt"], first.isoformat())
        self.assertTrue(observed["representativeReplayObserved"])
        self.assertEqual(observed["otherAlbumTracksObserved"], [str(sibling.id)])
        self.assertTrue(observed["retainedNewFavorite"])
        self.assertEqual(observed["source"], "unknown")
        self.assertEqual(album_discovery_metrics(self.other, self.LIBRARY, now=placed)["placements"], [])
