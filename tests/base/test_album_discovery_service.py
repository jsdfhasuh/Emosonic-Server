import hashlib
import json
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from mediafile import MediaFile

from supysonic import db
from supysonic.album_discovery_candidates import (
    AlbumDiscoveryCandidate,
    AlbumDiscoveryCandidates,
    AlbumDiscoveryEvidence,
)
from supysonic.album_discovery_catalog import CatalogAlbum, CatalogTrack
from supysonic.album_discovery_oracle import DeliveryFile, DeliveryManifest, OracleAlbumState
from supysonic.album_discovery_quota import get_album_download_order, get_album_download_quota
from supysonic.album_discovery_service import AlbumDiscoveryError, AlbumDiscoveryService
from supysonic.album_discovery_settings import load_album_discovery_runtime
from supysonic.config import DefaultConfig


def candidate(album_id: str) -> AlbumDiscoveryCandidate:
    song_id = album_id + "01"
    return AlbumDiscoveryCandidate(
        CatalogAlbum(
            album_id, "Edition " + album_id, "Artist " + album_id,
            "https://music.apple.com/cn/album/edition/" + album_id,
            (CatalogTrack(song_id, "Track " + album_id, "Artist " + album_id, 1, 1, 205000),),
        ),
        song_id,
        (AlbumDiscoveryEvidence(
            str(uuid4()), "Seed", "Seed artist", "1", album_id, "Artist " + album_id,
            song_id, "Track " + album_id, True,
        ),),
    )


class FakeCatalog:
    def __init__(self, *candidates: AlbumDiscoveryCandidate) -> None:
        self.releases = {item.album.album_id: item.album for item in candidates}
        self.refreshed = []

    def get_release(self, album_id: str, *, refresh: bool = False):
        if refresh:
            self.refreshed.append(album_id)
        return self.releases.get(album_id)


class FakeOracle:
    def __init__(self) -> None:
        self.attempts = []
        self.accepted = {}
        self.external = {}
        self.timeout = set()
        self.reject = set()
        self.return_rejection = set()
        self.manifests = {}

    def inspect(self, album_id, album_url, *, task_id=None, order_id=None) -> OracleAlbumState:
        if order_id and order_id in self.accepted:
            return OracleAlbumState(
                "submitted", self.accepted[order_id], self.manifests.get(album_id, ()),
            )
        if order_id and album_id in self.return_rejection:
            return OracleAlbumState("rejected")
        if album_id in self.external:
            return OracleAlbumState("associated", self.external[album_id])
        return OracleAlbumState("absent")

    def submit(self, album_id, album_url, order_id) -> OracleAlbumState:
        self.attempts.append((album_id, album_url, order_id))
        if album_id in self.reject:
            return OracleAlbumState("rejected")
        task = self.accepted.setdefault(order_id, "oracle-" + album_id)
        if album_id in self.timeout:
            return OracleAlbumState("unknown", detail="oracle_unavailable")
        return OracleAlbumState("submitted", task)


class AlbumDiscoveryServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db_uri = "sqlite:///" + (self.root / "service.sqlite").as_posix()
        db.init_database(self.db_uri)
        self.addCleanup(db.release_database)
        self.settings_file = self.root / "settings.json"
        self.config = DefaultConfig()
        self.config.ALBUM_DISCOVERY = {
            "library_key": "main", "target_user": "listener", "library_root": str(self.root),
            "settings_file": str(self.settings_file),
        }
        self.user = db.User.create(name="listener", password="password", salt="salt")
        self.other = db.User.create(name="another", password="password", salt="salt")
        self.now = datetime(2026, 11, 26, 13, tzinfo=timezone.utc)
        self.candidates = tuple(candidate(str(album_id)) for album_id in range(101, 106))
        self.catalog = FakeCatalog(*self.candidates)
        self.oracle = FakeOracle()
        self.service = self.make_service()
        self.write_policy(3, False)
        preferences = patch(
            "supysonic.album_discovery_service.album_discovery_candidate_allowed",
            return_value=True,
        )
        self.preferences = preferences.start()
        self.addCleanup(preferences.stop)

    def make_service(self) -> AlbumDiscoveryService:
        return AlbumDiscoveryService(
            self.config, load_album_discovery_runtime(self.config),
            catalog=self.catalog, oracle=self.oracle,
        )

    def write_policy(self, max_albums: int, auto_submit: bool) -> None:
        payload = {"aiDiscovery": {"maxAlbumsPerDay": max_albums, "autoSubmit": auto_submit}}
        self.settings_file.write_text(json.dumps(payload), encoding="utf-8")

    def preview(self):
        candidates = AlbumDiscoveryCandidates(candidates=self.candidates)
        with patch(
            "supysonic.album_discovery_service.discover_album_candidates", return_value=candidates,
        ):
            return self.service.preview(self.user, generate=True, now=self.now)

    def confirm(self, preview, album_id: str):
        return self.service.confirm(self.user, preview.preview_id, album_id, now=self.now)

    def test_preview_is_read_only_and_confirmation_checks_owner_release_and_policy(self) -> None:
        preview = self.preview()
        self.assertEqual(len(preview.assessments), 5)
        self.assertTrue(all(item.action == "new_order" for item in preview.assessments))
        self.assertEqual(preview.availability.quota.remaining, 3)
        self.assertFalse(self.oracle.attempts)
        for model in (db.AlbumDiscoveryOrder, db.AlbumDiscoverySelection, db.Playlist):
            self.assertEqual(model.select().count(), 0)
        with self.assertRaisesRegex(AlbumDiscoveryError, "account"):
            self.service.confirm(self.other, preview.preview_id, "101", now=self.now)
        with self.assertRaisesRegex(AlbumDiscoveryError, "not in"):
            self.confirm(preview, "777")
        with self.assertRaisesRegex(AlbumDiscoveryError, "new preview"):
            self.service.confirm(
                self.user, preview.preview_id, "101", now=self.now + timedelta(days=1)
            )

        self.write_policy(0, False)
        self.assertEqual(self.confirm(preview, "101").status, "limit_reached")
        self.assertEqual(db.AlbumDiscoverySelection.select().count(), 0)
        self.assertFalse(self.oracle.attempts)
        self.write_policy(3, False)
        with patch.object(self.catalog, "get_release", return_value=None):
            with self.assertRaisesRegex(AlbumDiscoveryError, "unavailable"):
                self.confirm(preview, "101")
        self.preferences.return_value = False
        with self.assertRaisesRegex(AlbumDiscoveryError, "current feedback"):
            self.confirm(preview, "101")
        self.preferences.return_value = True
        self.assertEqual(self.confirm(preview, "102").status, "submitted")
        self.assertEqual(self.confirm(preview, "102").status, "submitted")
        self.assertEqual([call[0] for call in self.oracle.attempts], ["102"])
        self.assertNotIn("?", self.oracle.attempts[0][1])
        self.assertEqual(db.AlbumDiscoverySelection.select().count(), 1)

    def test_nightly_and_manual_share_three_slots_across_wakeups(self) -> None:
        preview = self.preview()
        self.assertEqual(self.service.run_nightly(now=self.now), {"status": "paused"})
        self.assertEqual(self.confirm(preview, "103").status, "submitted")
        self.write_policy(3, True)
        self.oracle.external["105"] = "subscription-105"
        first = self.service.run_nightly(now=self.now)
        self.assertEqual(first["submitted"], 2)
        self.assertEqual(first["limit_reached"], 1)
        self.assertEqual(first["associated"], 1)
        self.service.run_nightly(now=self.now + timedelta(minutes=5))
        self.assertEqual(self.confirm(preview, "104").status, "limit_reached")
        self.assertEqual([call[0] for call in self.oracle.attempts], ["103", "101", "102"])
        self.assertEqual(get_album_download_quota("main", 3, now=self.now).remaining, 0)

    def test_zero_quota_reuses_later_local_and_inflight_albums(self) -> None:
        item = self.candidates[3]
        source = Path(__file__).parents[1] / "assets" / "formats" / "silence.flac"
        duration = MediaFile(str(source)).length
        expected = replace(item.album.tracks[0], duration_ms=round(duration * 1000))
        album = replace(item.album, tracks=(expected,))
        self.candidates = (*self.candidates[:3], replace(item, album=album), self.candidates[4])
        self.catalog.releases[album.album_id] = album
        path = self.root / album.artist / album.title / "01 track.flac"
        path.parent.mkdir(parents=True)
        shutil.copyfile(source, path)
        media = MediaFile(str(path))
        media.title, media.album = expected.title, album.title
        media.artist = media.albumartist = album.artist
        media.disc, media.track = 1, 1
        media.save()
        folder = db.Folder.create(name="Test library", path=str(self.root), root=True)
        artist = db.Artist.create(name=album.artist)
        local_album = db.Album.create(name=album.title, artist=artist)
        db.Track.create(
            title=expected.title, disc=1, number=1, album=local_album, artist=artist,
            root_folder=folder, folder=folder, path=str(path), duration=round(duration),
            bitrate=320, last_modification=1,
        )
        self.oracle.external["105"] = "subscription-105"
        self.preview()
        self.write_policy(0, True)
        result = self.service.run_nightly(now=self.now)
        self.assertEqual(result, {"status": "complete", "limit_reached": 3, "ready": 1, "associated": 1})
        self.assertFalse(self.oracle.attempts)
        self.assertIsNone(get_album_download_order("main", album.album_id))
        self.assertEqual(get_album_download_order("main", "105").status, "associated")
        self.assertEqual(db.AlbumDiscoverySelection.select().count(), 2)

    def test_nightly_and_manual_race_can_send_only_one_new_oracle_request(self) -> None:
        preview = self.preview()
        self.write_policy(1, True)
        start = Barrier(2, timeout=5)

        def manual():
            start.wait()
            return self.confirm(preview, "102")

        def automatic():
            start.wait()
            return self.service.run_nightly(now=self.now)

        with ThreadPoolExecutor(max_workers=2) as pool:
            manual_result = pool.submit(manual)
            automatic_result = pool.submit(automatic)
            manual_result.result()
            automatic_result.result()
        self.assertEqual(len(self.oracle.attempts), 1)
        self.assertEqual(db.AlbumDiscoveryOrder.select().count(), 1)
        self.assertEqual(db.AlbumDiscoverySelection.select().count(), 1)

    def test_preview_cannot_reserve_for_tomorrow_if_confirmation_crosses_midnight(self) -> None:
        preview = self.preview()
        with patch(
            "supysonic.album_discovery_service._current_time",
            side_effect=(self.now, self.now + timedelta(days=1)),
        ):
            with self.assertRaisesRegex(AlbumDiscoveryError, "date changed"):
                self.service.confirm(self.user, preview.preview_id, "101")
        self.assertFalse(self.oracle.attempts)
        self.assertEqual(db.AlbumDiscoveryOrder.select().count(), 0)
        self.assertEqual(db.AlbumDiscoverySelection.select().count(), 0)

    def test_explicit_alternate_must_have_preview_evidence_and_current_cn_availability(self) -> None:
        first = self.candidates[0]
        alternate = replace(first.album.tracks[0], song_id="10102", track_number=2, title="Alternate")
        evidence = replace(first.evidence[0], song_id=alternate.song_id, song_title=alternate.title)
        offered = replace(first, album=replace(first.album, tracks=first.album.tracks + (alternate,)),
                          evidence=first.evidence + (evidence,))
        self.candidates = (offered,)
        preview = self.preview()
        self.catalog.releases[first.album.album_id] = replace(first.album, tracks=(alternate,))
        with self.assertRaisesRegex(AlbumDiscoveryError, "alternate"):
            self.service.confirm(
                self.user, preview.preview_id, first.album.album_id,
                representative_song_id="777", now=self.now,
            )
        with self.assertRaisesRegex(AlbumDiscoveryError, "unavailable"):
            self.confirm(preview, first.album.album_id)
        response = self.service.confirm(
            self.user, preview.preview_id, first.album.album_id,
            representative_song_id=alternate.song_id, now=self.now,
        )
        self.assertEqual(response.status, "submitted")
        self.assertEqual(db.AlbumDiscoverySelection.get().representative_song_id, alternate.song_id)

    def test_provider_and_budget_failures_leave_the_ordinary_daily_playlist_available(self) -> None:
        from supysonic.openai_recommendations import OpenAIRecommendationError
        from supysonic.recommend import getRecommendedPlaylistForDay, refreshDailyRecommendPlaylists

        folder = db.Folder.create(name="Test library", path=str(self.root), root=True)
        artist = db.Artist.create(name="Local artist")
        album = db.Album.create(name="Local edition", artist=artist)
        track = db.Track.create(
            disc=1, number=1, title="Local track", album=album, artist=artist,
            folder=folder, root_folder=folder, path=str(self.root / "local.flac"),
            duration=200, bitrate=320, last_modification=1,
        )
        seed = db.Track.create(
            disc=1, number=2, title="Played seed", album=album, artist=artist,
            folder=folder, root_folder=folder, path=str(self.root / "seed.flac"),
            duration=200, bitrate=320, last_modification=1,
        )
        db.User_Play_Activity.create(user=self.user, track=seed)
        self.write_policy(3, True)
        for reason in ("catalog_unavailable", "ranking_unavailable", "ranking_budget"):
            with patch(
                "supysonic.album_discovery_service.discover_album_candidates",
                return_value=AlbumDiscoveryCandidates(reason=reason),
            ):
                self.assertEqual(self.service.run_nightly(now=self.now), {"status": reason})
        self.assertEqual(db.AlbumDiscoveryOrder.select().count(), 0)
        day = self.now.date().isoformat()
        self.assertGreater(refreshDailyRecommendPlaylists(num_songs=1, day=day, config=self.config), 0)
        playlist = getRecommendedPlaylistForDay(self.user, day)
        self.assertEqual(playlist.get_tracks(), [track])
        next_day = (self.now + timedelta(days=1)).date().isoformat()
        with patch("supysonic.recommend.openai_daily_selection_enabled", return_value=True), patch(
            "supysonic.recommend.select_openai_daily_tracks",
            side_effect=OpenAIRecommendationError("Budget exhausted", reason="budget"),
        ):
            self.assertGreater(
                refreshDailyRecommendPlaylists(num_songs=1, day=next_day, config=self.config), 0,
            )
        self.assertEqual(getRecommendedPlaylistForDay(self.user, next_day).get_tracks(), [track])

    def test_timeout_restart_cross_midnight_and_zero_still_settle_original(self) -> None:
        preview = self.preview()
        self.oracle.timeout.add("101")
        self.assertEqual(self.confirm(preview, "101").status, "unknown")
        original = get_album_download_order("main", "101")
        self.assertEqual(original.status, "reserved")
        self.assertEqual(db.AlbumDiscoverySelection.select().count(), 1)

        db.release_database()
        db.init_database(self.db_uri)
        self.service = self.make_service()
        self.write_policy(0, True)
        next_day = self.now + timedelta(days=1)
        self.assertEqual(self.service.run_nightly(now=next_day), {"status": "no_seeds"})
        self.assertEqual(self.service.reconcile_orders()["checked"], 1)
        settled = get_album_download_order("main", "101")
        self.assertEqual((settled.id, settled.status, settled.quota_date), (
            original.id, "submitted", original.quota_date,
        ))
        self.assertEqual(settled.oracle_task_id, "oracle-101")
        self.assertEqual(get_album_download_quota("main", 0, now=next_day).submitted, 0)
        self.assertEqual(len(self.oracle.attempts), 1)

    def test_receipt_and_late_index_make_an_existing_order_ready_while_paused(self) -> None:
        source = Path(__file__).parents[1] / "assets" / "formats" / "silence.flac"
        initial = self.candidates[0]
        duration = MediaFile(str(source)).length
        expected = replace(initial.album.tracks[0], duration_ms=round(duration * 1000))
        album = replace(initial.album, tracks=(expected,))
        self.candidates = (replace(initial, album=album),)
        self.catalog.releases[album.album_id] = album
        preview = self.preview()
        self.assertEqual(self.confirm(preview, album.album_id).status, "submitted")
        self.write_policy(0, True)

        path = self.root / album.artist / album.title / ("1-01 " + expected.title + ".flac")
        path.parent.mkdir(parents=True)
        shutil.copyfile(source, path)
        media = MediaFile(str(path))
        media.title, media.album = expected.title, album.title
        media.artist = media.albumartist = album.artist
        media.disc, media.track = 1, 1
        media.save()
        receipt = DeliveryFile(
            path.relative_to(self.root).as_posix(),
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        batch = DeliveryManifest("delivery-101", album.url, "cleaned", (receipt,), "a" * 64)
        self.oracle.manifests[album.album_id] = (batch,)
        self.assertEqual(self.service.reconcile_orders(), {"checked": 1, "ready": 0})
        tracked = self.service.tracked_releases(self.user)[0]
        self.assertEqual(tracked.reconciliation.missing[0].reason, "waiting_for_index")

        folder = db.Folder.create(name="Test library", path=str(self.root), root=True)
        artist = db.Artist.create(name=album.artist)
        local_album = db.Album.create(name=album.title, artist=artist)
        local_track = db.Track.create(
            disc=1, number=1, title=expected.title, album=local_album, artist=artist,
            folder=folder, root_folder=folder, path=str(path), duration=round(duration),
            bitrate=320, last_modification=1,
        )
        self.oracle.manifests.clear()
        result = self.service.reconcile_orders()
        tracked = self.service.tracked_releases(self.user)[0]
        self.assertEqual(result, {"checked": 1, "ready": 1}, tracked.reconciliation.missing)
        self.assertEqual(db.AlbumDiscoverySelection.get().representative_track_id, local_track.id)
        self.assertEqual(self.service.tracked_releases(self.user)[0].status, "ready")
        self.assertEqual(len(self.oracle.attempts), 1)

    def test_existing_remote_and_explicit_rejection_never_charge_a_new_slot(self) -> None:
        self.oracle.external["101"] = "subscription-101"
        preview = self.preview()
        self.write_policy(0, False)
        self.assertEqual(self.confirm(preview, "101").status, "associated")
        order = get_album_download_order("main", "101")
        self.assertEqual((order.status, order.oracle_task_id), ("associated", "subscription-101"))
        self.assertFalse(self.oracle.attempts)

        self.oracle.reject.add("102")
        self.write_policy(1, True)
        self.assertEqual(self.confirm(preview, "102").status, "rejected")
        self.assertIsNone(get_album_download_order("main", "102"))
        self.assertEqual(get_album_download_quota("main", 1, now=self.now).remaining, 1)
        self.assertEqual(self.confirm(preview, "103").status, "submitted")

    def test_previous_quota_schema_upgrades_with_order_intact(self) -> None:
        preview = self.preview()
        self.confirm(preview, "101")
        original = get_album_download_order("main", "101")
        db.db.drop_tables([
            db.AlbumDiscoveryPlacement, db.AlbumDiscoverySelection,
            db.AlbumDiscoveryPreview, db.AlbumDiscoveryRelease,
        ])
        db.Meta.update(value="20261126").where(db.Meta.key == "schema_version").execute()
        db.release_database()
        db.init_database(self.db_uri)
        self.assertEqual(db.Meta["schema_version"].value, db.SCHEMA_VERSION)
        self.assertEqual(get_album_download_order("main", "101"), original)
        self.assertTrue(db.AlbumDiscoveryPlacement.table_exists())
