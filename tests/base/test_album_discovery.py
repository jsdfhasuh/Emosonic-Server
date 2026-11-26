import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier, Event
from typing import Iterator, Optional
from unittest.mock import patch

from flask import Config
from peewee import Database

from supysonic import album_discovery_quota, db
from supysonic.album_discovery import (
    get_album_discovery_availability,
    reserve_discovered_album,
)
from supysonic.album_discovery_quota import (
    AlbumReservationResult,
    mark_album_download_submitted,
)
from supysonic.album_discovery_settings import (
    AlbumDiscoveryConfig,
    AlbumDiscoverySettingsError,
)
from supysonic.config import DefaultConfig


class AlbumDiscoveryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_dir.cleanup)
        directory = Path(temporary_dir.name)
        self.settings_file = directory / "settings.json"
        previous_db = db.db.obj
        db.init_database("sqlite:///" + str(directory / "discovery.sqlite"))
        self.addCleanup(self.restore_database, previous_db)
        self.daemon_config = DefaultConfig()
        self.daemon_config.ALBUM_DISCOVERY = {"settings_file": str(self.settings_file)}
        self.web_config = Config(str(directory))
        self.web_config.from_object(self.daemon_config)
        self.now = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)

    def restore_database(self, previous_db: Optional[Database]) -> None:
        db.release_database()
        db.db.initialize(previous_db)

    def write_settings(self, **settings: object) -> None:
        self.settings_file.write_text(json.dumps({"aiDiscovery": settings}), "utf-8")

    def test_preview_and_submission_share_live_settings(self) -> None:
        self.write_settings(maxAlbumsPerDay=3)
        preview = get_album_discovery_availability(self.web_config, "main", now=self.now)
        self.assertEqual(preview.quota.remaining, 3)
        self.assertFalse(preview.auto_submit)
        disabled = reserve_discovered_album(
            self.daemon_config, "main", "100", automatic=True, now=self.now
        )
        self.assertEqual(disabled.status, "submission_disabled")
        self.assertEqual(disabled.quota.remaining, 3)

        reservation = reserve_discovered_album(
            self.web_config, "main", "100", automatic=False, now=self.now
        )
        self.assertEqual(reservation.status, "reserved")

        self.write_settings(maxAlbumsPerDay=0, autoSubmit=True)
        paused = reserve_discovered_album(
            self.daemon_config, "main", "200", automatic=True, now=self.now
        )
        self.assertEqual(paused.status, "limit_reached")
        self.assertEqual((paused.quota.limit, paused.quota.reserved), (0, 1))

        self.settings_file.write_text("{", "utf-8")
        broken = get_album_discovery_availability(self.web_config, "main", now=self.now)
        self.assertTrue(broken.settings_error)
        self.assertEqual((broken.quota.remaining, broken.quota.reserved), (0, 1))
        with self.assertRaises(AlbumDiscoverySettingsError):
            reserve_discovered_album(
                self.web_config, "main", "200", automatic=False, now=self.now
            )
        self.assertTrue(mark_album_download_submitted(reservation.order, "oracle-100"))

        self.write_settings(maxAlbumsPerDay=2, autoSubmit=True)
        resumed = reserve_discovered_album(
            self.daemon_config, "main", "200", automatic=True, now=self.now
        )
        self.assertEqual(resumed.status, "reserved")
        self.assertEqual((resumed.quota.submitted, resumed.quota.remaining), (1, 0))

    def test_manual_and_automatic_share_the_last_slot(self) -> None:
        self.write_settings(maxAlbumsPerDay=1, autoSubmit=True)
        for library, automatic_id, second_status in (
            ("same_release", "100", "existing"),
            ("different_releases", "200", "limit_reached"),
        ):
            with self.subTest(library=library):
                barrier = Barrier(2, timeout=5)

                def reserve(
                    config: AlbumDiscoveryConfig, album_id: str, automatic: bool
                ) -> AlbumReservationResult:
                    barrier.wait()
                    return reserve_discovered_album(
                        config, library, album_id, automatic=automatic, now=self.now
                    )

                with ThreadPoolExecutor(max_workers=2) as pool:
                    manual = pool.submit(reserve, self.web_config, "100", False)
                    automatic = pool.submit(
                        reserve, self.daemon_config, automatic_id, True
                    )
                    results = manual.result(), automatic.result()

                self.assertCountEqual(
                    [item.status for item in results], ["reserved", second_status]
                )
                if second_status == "existing":
                    self.assertEqual(results[0].order, results[1].order)
                final = get_album_discovery_availability(
                    self.web_config, library, now=self.now
                )
                self.assertEqual((final.quota.reserved, final.quota.remaining), (1, 0))

    def test_waiting_submission_uses_settings_changed_before_reservation(self) -> None:
        self.write_settings(maxAlbumsPerDay=1)
        waiting = Event()
        write_transaction = album_discovery_quota._write_transaction

        @contextmanager
        def signal_transaction(library_key: str) -> Iterator[None]:
            waiting.set()
            with write_transaction(library_key):
                yield

        with patch.object(album_discovery_quota, "_write_transaction", signal_transaction):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with db.db.atomic("IMMEDIATE"):
                    result = pool.submit(
                        reserve_discovered_album,
                        self.web_config,
                        "main",
                        "100",
                        automatic=False,
                        now=self.now,
                    )
                    self.assertTrue(waiting.wait(5))
                    self.write_settings(maxAlbumsPerDay=0)

                reservation = result.result()
                self.assertEqual(reservation.status, "limit_reached")
                self.assertEqual(reservation.quota.limit, 0)
                self.assertEqual(reservation.quota.reserved, 0)
