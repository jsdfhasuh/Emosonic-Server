from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from typing import List, Optional, Sequence
import unittest

from supysonic import db
from supysonic.album_discovery_quota import (
    AlbumReservationResult,
    associate_album_download,
    get_album_download_quota,
    mark_album_download_submitted,
    release_album_download_reservation,
    reserve_album_download,
)


class AlbumDiscoveryQuotaTestCase(unittest.TestCase):
    before_midnight = datetime(2026, 11, 26, 15, 59, tzinfo=timezone.utc)
    after_midnight = before_midnight + timedelta(minutes=1)

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.uri = "sqlite:///" + (Path(directory.name) / "quota.sqlite").as_posix()
        db.init_database(self.uri)
        self.addCleanup(db.release_database)

    def reserve(
        self,
        album_id: str,
        limit: int = 1,
        *,
        when: Optional[datetime] = None,
        library: str = "main",
        allow_new: bool = True,
    ) -> AlbumReservationResult:
        return reserve_album_download(
            library, album_id, lambda: (limit, allow_new),
            now=when or self.before_midnight,
        )

    def run_concurrently(self, album_ids: Sequence[str]) -> List[AlbumReservationResult]:
        start = Barrier(len(album_ids), timeout=5)

        def reserve_one(album_id: str) -> AlbumReservationResult:
            start.wait()
            return self.reserve(album_id)

        with ThreadPoolExecutor(max_workers=len(album_ids)) as pool:
            return list(pool.map(reserve_one, album_ids))

    def test_same_release_concurrently_reserves_once(self) -> None:
        results = self.run_concurrently(["101", "101"])
        self.assertCountEqual(
            [result.status for result in results], ["reserved", "existing"]
        )
        self.assertEqual(results[0].order, results[1].order)
        self.assertEqual(db.AlbumDiscoveryOrder.select().count(), 1)

    def test_different_releases_competing_for_the_last_slot(self) -> None:
        results = self.run_concurrently(["101", "102"])
        self.assertCountEqual(
            [result.status for result in results], ["reserved", "limit_reached"]
        )
        self.assertEqual(db.AlbumDiscoveryOrder.select().count(), 1)
        self.assertEqual(self.reserve("102", library="other").status, "reserved")

    def test_restart_midnight_and_retry_preserve_the_original_charge(self) -> None:
        original = self.reserve("101").order
        db.release_database()
        db.init_database(self.uri)

        same_day = self.reserve("102")
        self.assertEqual(same_day.status, "limit_reached")
        self.assertEqual((same_day.quota.reserved, same_day.quota.submitted), (1, 0))
        self.assertEqual(self.reserve("101", when=self.after_midnight).order, original)
        self.assertTrue(mark_album_download_submitted(original, "oracle-1"))
        self.assertTrue(mark_album_download_submitted(original, "oracle-1"))
        self.assertFalse(mark_album_download_submitted(original, "oracle-2"))
        self.assertFalse(release_album_download_reservation(original))

        original_day = get_album_download_quota("main", 1, now=self.before_midnight)
        self.assertEqual(original_day.quota_date, date(2026, 11, 26))
        self.assertEqual((original_day.reserved, original_day.submitted), (0, 1))
        retry = self.reserve("101", when=self.after_midnight)
        self.assertEqual(retry.status, "existing")
        self.assertEqual(retry.order.id, original.id)
        self.assertEqual(retry.order.quota_date, original_day.quota_date)
        self.assertEqual(retry.order.oracle_task_id, "oracle-1")
        self.assertEqual(retry.quota.quota_date, date(2026, 11, 27))
        self.assertEqual(retry.quota.remaining, 1)
        self.assertEqual(
            self.reserve("102", when=self.after_midnight).status, "reserved"
        )

    def test_explicit_rejection_releases_and_fences_stale_reservation(self) -> None:
        rejected = self.reserve("101").order
        self.assertTrue(release_album_download_reservation(rejected))
        replacement = self.reserve("101").order
        self.assertNotEqual(rejected.id, replacement.id)
        self.assertFalse(mark_album_download_submitted(rejected, "old-task"))
        self.assertFalse(associate_album_download(rejected, "old-task"))
        self.assertFalse(release_album_download_reservation(rejected))
        self.assertEqual(self.reserve("101").order, replacement)
        self.assertTrue(mark_album_download_submitted(replacement, "new-task"))

    def test_reused_oracle_task_releases_quota_but_keeps_order(self) -> None:
        initial = self.reserve("101").order
        self.assertTrue(associate_album_download(initial, "existing-task"))
        self.assertTrue(associate_album_download(initial, "existing-task"))
        self.assertFalse(associate_album_download(initial, "different-task"))
        self.assertFalse(mark_album_download_submitted(initial, "existing-task"))
        self.assertFalse(release_album_download_reservation(initial))
        repeated = self.reserve("101")
        self.assertEqual(repeated.status, "existing")
        self.assertEqual(repeated.order.status, "associated")
        self.assertEqual(repeated.order.oracle_task_id, "existing-task")
        self.assertEqual(repeated.quota.remaining, 1)
        self.assertEqual(self.reserve("102").status, "reserved")

    def test_current_limit_controls_new_orders_but_preserves_existing_ones(self) -> None:
        first = self.reserve("101").order
        self.assertEqual(self.reserve("102", 2).status, "reserved")
        self.assertTrue(mark_album_download_submitted(first, "oracle-1"))
        self.assertEqual(self.reserve("103", 1).quota.remaining, 0)
        self.assertEqual(self.reserve("103", 0).status, "limit_reached")
        disabled = self.reserve("103", 3, allow_new=False)
        self.assertEqual(disabled.status, "submission_disabled")
        self.assertEqual(
            (disabled.quota.limit, disabled.quota.reserved, disabled.quota.submitted),
            (3, 1, 1),
        )
        self.assertEqual(disabled.quota.remaining, 1)
        self.assertEqual(self.reserve("101", 0, allow_new=False).status, "existing")
        self.assertEqual(self.reserve("103", 3).status, "reserved")
        self.assertEqual(self.reserve("104", 3).status, "limit_reached")

    def test_reservation_refuses_uncommitted_outer_transaction(self) -> None:
        with db.db.atomic(), self.assertRaisesRegex(RuntimeError, "outermost"):
            self.reserve("101")
        self.assertEqual(db.AlbumDiscoveryOrder.select().count(), 0)

    def test_upgrade_from_previous_schema_adds_usable_quota_tables(self) -> None:
        db.Meta.create(key="quota_test_marker", value="keep")
        db.db.drop_tables([db.AlbumDiscoveryOrder, db.AlbumDiscoveryQuotaLock])
        version = db.Meta["schema_version"]
        version.value = "20260814"
        version.save()
        db.release_database()
        db.init_database(self.uri)

        self.assertEqual(db.Meta["schema_version"].value, db.SCHEMA_VERSION)
        self.assertEqual(db.Meta["quota_test_marker"].value, "keep")
        self.assertEqual(self.reserve("101").status, "reserved")

    def test_input_validation_does_not_create_an_order(self) -> None:
        for album_id in ("101 ", "０１", "cn/album/101", "001", ""):
            with self.subTest(album_id=album_id), self.assertRaises(ValueError):
                self.reserve(album_id)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.reserve("101", when=self.before_midnight.replace(tzinfo=None))
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            self.reserve("101", True)
        self.assertEqual(db.AlbumDiscoveryOrder.select().count(), 0)
