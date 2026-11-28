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

from mediafile import MediaFile

from supysonic import db
from supysonic.album_discovery_catalog import CatalogError
from supysonic.album_discovery_oracle import DeliveryFile, DeliveryManifest, OracleAlbumState
from supysonic.album_discovery_quota import (
    get_album_download_order,
    get_album_download_quota,
    mark_album_download_submitted,
    reserve_album_download,
)
from supysonic.album_discovery_service import AlbumDiscoveryError, AlbumDiscoveryService
from supysonic.album_discovery_settings import load_album_discovery_runtime
from supysonic.bilibili_discovery.orders import (
    confirm_bilibili_album,
    refresh_bilibili_candidate,
    release_track_is_current,
)
from supysonic.config import DefaultConfig
from tests.base.test_album_discovery_service import FakeCatalog, FakeOracle, candidate


class ClosureOracle(FakeOracle):
    def __init__(self) -> None:
        super().__init__()
        self.closed = []
        self.inspected = []
        self.completed = False
        self.refuse_unconfirmed_closure = False

    def inspect(self, album_id, album_url, *, task_id=None, order_id=None) -> OracleAlbumState:
        self.inspected.append((album_id, task_id, order_id))
        result = super().inspect(album_id, album_url, task_id=task_id, order_id=order_id)
        return replace(result, task_status="completed") if self.completed and result.task_id else result

    def close_if_unaccepted(self, album_id: str, album_url: str, order_id: str) -> OracleAlbumState:
        self.closed.append(order_id)
        if self.refuse_unconfirmed_closure:
            return OracleAlbumState("unknown")
        if order_id in self.accepted:
            return OracleAlbumState("submitted", self.accepted[order_id])
        return OracleAlbumState("rejected")


class BilibiliDiscoveryOrderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.db_uri = "sqlite:///" + (self.root / "bilibili.sqlite").as_posix()
        db.init_database(self.db_uri)
        self.addCleanup(db.release_database)
        self.now = datetime(2026, 11, 28, 13, tzinfo=timezone.utc)
        self.user = db.User.create(name="listener", password="fixture", salt="fixture")
        self.run = db.BilibiliDiscoveryRun.create(
            user=self.user, source_day=self.now.date() - timedelta(days=1), status="done",
        )
        self.settings_file = self.root / "settings.json"
        self.config = DefaultConfig()
        self.config.ALBUM_DISCOVERY = {
            "library_key": "main", "target_user": self.user.name,
            "library_root": str(self.root), "settings_file": str(self.settings_file),
        }
        self.config.BILIBILI_DISCOVERY = {"enabled": True, "auto_download": True}
        self.first, self.second = candidate("101"), candidate("102")
        self.oracle = ClosureOracle()
        self.catalog = FakeCatalog(self.first, self.second)
        self.service = self.make_service()
        self.write_policy(13, False)
        for target, member in (
            ("supysonic.bilibili_discovery.matcher.bilibili_target_allowed", "target_allowed"),
            ("supysonic.album_discovery_service.album_discovery_candidate_allowed", "legacy_allowed"),
        ):
            mock_patch = patch(target, return_value=True)
            setattr(self, member, mock_patch.start())
            self.addCleanup(mock_patch.stop)

    def make_service(self) -> AlbumDiscoveryService:
        return AlbumDiscoveryService(
            self.config, load_album_discovery_runtime(self.config),
            catalog=self.catalog, oracle=self.oracle,
        )

    def write_policy(self, limit: int, legacy_auto_submit: bool) -> None:
        self.settings_file.write_text(json.dumps({
            "aiDiscovery": {"maxAlbumsPerDay": limit, "autoSubmit": legacy_auto_submit},
        }), encoding="utf-8")

    def discovery(self, album, track, key: str = ""):
        return db.BilibiliDiscoveryCandidate.create(
            run=self.run, identity_key=track.song_id + key, title=track.title,
            artist=track.artist, version="studio", album_hint=album.title,
        )

    def confirm_bilibili(self, target, album=None, song_id=None):
        return confirm_bilibili_album(
            self.service, self.user, target, album or self.first.album,
            song_id or self.first.representative_song_id, now=self.now,
        )

    def confirm_legacy(self, selection):
        return self.service._confirm_one(
            self.user, selection, 0, automatic=True, preview_day=self.now.date(), now=self.now,
        )

    def test_new_order_requires_both_switches_and_shared_quota(self) -> None:
        target = self.discovery(self.first.album, self.first.album.tracks[0])
        for enabled, auto_download in ((False, False), (False, True), (True, False)):
            with self.subTest(enabled=enabled, auto_download=auto_download):
                self.config.BILIBILI_DISCOVERY.update(enabled=enabled, auto_download=auto_download)
                self.assertEqual(self.confirm_bilibili(target).status, "submission_disabled")
        self.config.BILIBILI_DISCOVERY.update(enabled=True, auto_download=True)
        self.write_policy(0, True)
        self.assertEqual(self.confirm_bilibili(target).status, "limit_reached")
        self.assertEqual(db.AlbumDiscoveryOrder.select().count(), 0)
        self.assertIsNone(db.BilibiliDiscoveryCandidate.get_by_id(target.id).release_id)
        self.assertFalse(self.oracle.attempts)

        self.write_policy(13, False)
        self.assertEqual(self.service.run_nightly(now=self.now), {"status": "paused"})
        result = self.confirm_bilibili(target)
        self.assertEqual(result.status, "submitted")
        self.assertEqual(db.BilibiliDiscoveryCandidate.get_by_id(target.id).status, "waiting_album")
        self.assertEqual(len(self.oracle.attempts), 1)
        for model in (db.AlbumDiscoverySelection, db.AlbumDiscoveryPlacement, db.User_Play_Activity):
            self.assertEqual(model.select().count(), 0)

    def test_legacy_and_bilibili_share_one_order_for_different_targets(self) -> None:
        album = self.first.album
        alternative = replace(album.tracks[0], song_id="10102", track_number=2, title="Other song")
        album = replace(album, tracks=album.tracks + (alternative,))
        self.catalog.releases[album.album_id] = album
        self.write_policy(13, True)
        target = self.discovery(album, alternative)

        first = self.confirm_bilibili(target, album, alternative.song_id)
        self.assertEqual(first.status, "submitted")
        self.config.BILIBILI_DISCOVERY["enabled"] = False
        legacy = self.confirm_legacy(replace(self.first, album=album))
        self.assertEqual(legacy.order_id, first.order_id)
        self.assertEqual(self.confirm_bilibili(target, album, alternative.song_id).order_id, first.order_id)
        linked = db.BilibiliDiscoveryCandidate.get_by_id(target.id)
        chosen = db.AlbumDiscoverySelection.get()
        self.assertEqual(linked.release_id, chosen.release_id)
        self.assertNotEqual(linked.apple_song_id, chosen.representative_song_id)
        self.assertEqual(get_album_download_quota("main", 13, now=self.now).submitted, 1)
        self.assertEqual(len(self.oracle.attempts), 1)

    def test_concurrent_sources_can_only_reserve_the_thirteenth_slot_once(self) -> None:
        self.write_policy(13, True)
        for apple_album_id in range(1000, 1012):
            result = reserve_album_download("main", str(apple_album_id), lambda: (13, True), now=self.now)
            self.assertTrue(mark_album_download_submitted(result.order, "fixture-" + str(apple_album_id)))
        target = self.discovery(self.first.album, self.first.album.tracks[0])
        start = Barrier(2, timeout=10)

        def bilibili():
            start.wait()
            return self.confirm_bilibili(target)

        def legacy():
            start.wait()
            return self.confirm_legacy(self.second)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(call) for call in (bilibili, legacy)]
            outcomes = {result.result().status for result in results}
        self.assertEqual(outcomes, {"submitted", "limit_reached"})
        self.assertEqual(len(self.oracle.attempts), 1)
        self.assertEqual(db.AlbumDiscoveryOrder.select().count(), 13)
        self.assertEqual(get_album_download_quota("main", 13, now=self.now).submitted, 13)

    def test_lost_acceptance_reconciles_original_id_on_restart_while_disabled(self) -> None:
        target = self.discovery(self.first.album, self.first.album.tracks[0])
        self.oracle.timeout.add(self.first.album.album_id)
        result = self.confirm_bilibili(target)
        self.assertEqual(result.status, "unknown")
        original = get_album_download_order("main", self.first.album.album_id)
        self.assertEqual(original.status, "reserved")
        db.release_database()
        db.init_database(self.db_uri)
        self.service = self.make_service()
        self.config.BILIBILI_DISCOVERY.update(enabled=False, auto_download=False)
        self.write_policy(0, False)
        self.assertEqual(
            self.service.reconcile_orders(now=self.now + timedelta(days=1)), {"checked": 1, "ready": 0},
        )
        reconciled = get_album_download_order("main", self.first.album.album_id)
        self.assertEqual((reconciled.id, reconciled.status, reconciled.quota_date), (
            original.id, "submitted", original.quota_date,
        ))
        self.assertIn((original.album_id, None, str(original.id)), self.oracle.inspected)
        self.assertFalse(self.oracle.closed)
        self.assertEqual(len(self.oracle.attempts), 1)

    def test_absent_get_does_not_release_an_unknown_order_without_confirmed_closure(self) -> None:
        target = self.discovery(self.first.album, self.first.album.tracks[0])
        with patch.object(self.oracle, "submit", return_value=OracleAlbumState("unknown")) as submit:
            result = self.confirm_bilibili(target)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(submit.call_args.args[-1], str(result.order_id))
        self.config.BILIBILI_DISCOVERY["auto_download"] = False
        self.oracle.refuse_unconfirmed_closure = True
        self.service.reconcile_orders(now=self.now)
        original = get_album_download_order("main", self.first.album.album_id)
        self.assertEqual(original.id, result.order_id)
        self.oracle.refuse_unconfirmed_closure = False
        self.service.reconcile_orders(now=self.now)
        self.assertEqual(self.oracle.closed, [str(result.order_id)] * 2)
        self.assertIsNone(get_album_download_order("main", self.first.album.album_id))
        self.assertFalse(self.oracle.attempts)
        self.assertEqual(get_album_download_quota("main", 13, now=self.now).remaining, 13)
        self.assertFalse(refresh_bilibili_candidate(self.service, target, now=self.now))
        self.assertEqual(db.BilibiliDiscoveryCandidate.get_by_id(target.id).reason, "oracle_rejected")

    def test_interrupted_reservation_is_closed_when_new_orders_are_disabled(self) -> None:
        target = self.discovery(self.first.album, self.first.album.tracks[0])
        with patch.object(self.oracle, "submit", side_effect=RuntimeError("fixture interruption")):
            with self.assertRaisesRegex(RuntimeError, "fixture interruption"):
                self.confirm_bilibili(target)
        original = get_album_download_order("main", self.first.album.album_id)
        self.config.BILIBILI_DISCOVERY["enabled"] = False
        self.service.reconcile_orders(now=self.now)
        self.assertFalse(self.oracle.attempts)
        self.assertEqual(self.oracle.closed, [str(original.id)])
        self.assertIsNone(get_album_download_order("main", self.first.album.album_id))

    def test_unlinked_unknown_order_still_settles_without_new_dispatch(self) -> None:
        target = self.discovery(self.first.album, self.first.album.tracks[0])
        self.oracle.timeout.add(self.first.album.album_id)
        original = self.confirm_bilibili(target)
        self.assertEqual(original.status, "unknown")
        target.delete_instance()
        result = self.service.reconcile_orders(now=self.now)
        self.assertEqual(result, {"checked": 1, "ready": 0})
        settled = get_album_download_order("main", original.album_id)
        self.assertEqual((settled.id, settled.status), (original.order_id, "submitted"))
        self.assertEqual(len(self.oracle.attempts), 1)

    def test_interrupted_reservation_rechecks_cn_and_feedback_before_replaying_same_id(self) -> None:
        target = self.discovery(self.first.album, self.first.album.tracks[0])
        with patch.object(self.oracle, "submit", side_effect=RuntimeError("fixture interruption")):
            with self.assertRaises(RuntimeError):
                self.confirm_bilibili(target)
        original = get_album_download_order("main", self.first.album.album_id)
        with patch.object(self.catalog, "get_release", side_effect=CatalogError("fixture offline")):
            self.service.reconcile_orders(now=self.now)
        self.assertFalse(self.oracle.attempts)
        self.assertFalse(self.oracle.closed)
        self.assertEqual(get_album_download_order("main", self.first.album.album_id), original)
        self.service.reconcile_orders(now=self.now)
        self.assertEqual(self.catalog.refreshed, [self.first.album.album_id])
        self.assertEqual(self.oracle.attempts[0][-1], str(original.id))
        self.assertEqual(self.target_allowed.call_count, 2)
        self.assertEqual(get_album_download_order("main", self.first.album.album_id).status, "submitted")

    def test_invalid_catalog_or_target_cannot_dispatch(self) -> None:
        album = self.first.album
        target = self.discovery(album, album.tracks[0])
        for invalid, song_id in (
            (replace(album, url=album.url.replace("/cn/", "/hk/")), album.tracks[0].song_id),
            (replace(album, tracks=album.tracks * 2), album.tracks[0].song_id),
            (album, "9999"),
        ):
            with self.subTest(invalid=invalid, song_id=song_id):
                with self.assertRaises(AlbumDiscoveryError):
                    self.confirm_bilibili(target, invalid, song_id)
        self.target_allowed.return_value = False
        self.assertEqual(self.confirm_bilibili(target).status, "submission_disabled")
        self.assertFalse(self.oracle.attempts)
        self.assertEqual(db.AlbumDiscoveryOrder.select().count(), 0)

    def test_verified_whole_album_maps_each_target_and_rechecks_changed_files(self) -> None:
        source = Path(__file__).parents[1] / "assets" / "formats" / "silence.flac"
        duration = MediaFile(str(source)).length
        edition = self.first.album
        expected = tuple(
            replace(
                edition.tracks[0], song_id=str(10100 + number), title=f"Fixture song {number:02}",
                track_number=number, duration_ms=round(duration * 1000),
            )
            for number in range(1, 13)
        )
        album = replace(edition, tracks=expected)
        self.catalog.releases[album.album_id] = album
        first = self.discovery(album, expected[0])
        last = self.discovery(album, expected[-1])
        for target, song in ((first, expected[0]), (last, expected[-1])):
            outcome = self.confirm_bilibili(target, album, song.song_id)
            self.assertEqual(outcome.status, "submitted")
        self.oracle.completed = True
        self.assertEqual(self.service.reconcile_orders(now=self.now), {"checked": 1, "ready": 0})
        self.assertFalse(refresh_bilibili_candidate(self.service, first, now=self.now))

        folder = db.Folder.create(name="Isolated library", path=str(self.root), root=True)
        artist = db.Artist.create(name=album.artist)
        local_album = db.Album.create(name=album.title, artist=artist)
        paths, files, indexed = {}, {}, {}

        def deliver(song):
            path = self.root / album.artist / album.title / f"1-{song.track_number:02} track.flac"
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, path)
            media = MediaFile(str(path))
            media.title, media.album = song.title, album.title
            media.artist = media.albumartist = song.artist
            media.disc, media.track = 1, song.track_number
            media.save()
            paths[song.song_id] = path
            files[song.song_id] = DeliveryFile(
                path.relative_to(self.root).as_posix(), path.stat().st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )

        def index(song):
            indexed[song.song_id] = db.Track.create(
                title=song.title, disc=1, number=song.track_number, album=local_album, artist=artist,
                folder=folder, root_folder=folder, path=str(paths[song.song_id]),
                duration=round(duration), bitrate=320, last_modification=1,
            )

        def receipts():
            self.oracle.manifests[album.album_id] = (
                DeliveryManifest("fixture-batch", album.url, "cleaned", tuple(files.values()), "a" * 64),
            )

        for song in expected[:-1]:
            deliver(song)
            index(song)
        receipts()
        self.assertEqual(self.service.reconcile_orders(now=self.now), {"checked": 1, "ready": 0})
        self.assertFalse(refresh_bilibili_candidate(self.service, first, now=self.now))
        deliver(expected[-1])
        receipts()
        self.assertEqual(self.service.reconcile_orders(now=self.now), {"checked": 1, "ready": 0})
        inventory = json.loads(db.AlbumDiscoveryRelease.get().reconciliation)
        self.assertEqual(inventory["tracks"][-1]["reason"], "waiting_for_index")

        index(expected[-1])
        self.oracle.manifests.clear()
        self.config.BILIBILI_DISCOVERY.update(enabled=False, auto_download=False)
        self.assertEqual(self.service.reconcile_orders(now=self.now), {"checked": 1, "ready": 1})
        for target, song in ((first, expected[0]), (last, expected[-1])):
            self.assertTrue(refresh_bilibili_candidate(self.service, target, now=self.now))
            recorded = db.BilibiliDiscoveryCandidate.get_by_id(target.id)
            self.assertEqual(recorded.track_id, indexed[song.song_id].id)
            self.assertTrue(release_track_is_current(recorded, recorded.track, "main"))
            self.assertFalse(release_track_is_current(recorded, recorded.track, "other-library"))

        reused = self.discovery(album, expected[5])
        with patch.object(self.service, "_inventory", side_effect=AssertionError("reused whole album")):
            self.assertEqual(self.confirm_bilibili(reused, album, expected[5].song_id).status, "ready")
        self.assertEqual(db.BilibiliDiscoveryCandidate.get_by_id(reused.id).track_id, indexed[expected[5].song_id].id)

        paths[expected[-1].song_id].unlink()
        stale = db.BilibiliDiscoveryCandidate.get_by_id(last.id)
        self.assertFalse(release_track_is_current(stale, stale.track, "main"))
        self.assertFalse(refresh_bilibili_candidate(self.service, last, now=self.now))
        self.assertEqual(db.BilibiliDiscoveryCandidate.get_by_id(last.id).status, "waiting_album")
        self.assertIsNone(db.AlbumDiscoveryRelease.get().ready_at)
        self.assertEqual(len(self.oracle.attempts), 1)
        self.assertEqual(db.AlbumDiscoverySelection.select().count(), 0)
        self.assertEqual(db.User_Play_Activity.select().count(), 0)
