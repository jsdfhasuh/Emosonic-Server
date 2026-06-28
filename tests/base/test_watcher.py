# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2017-2022 Alban 'spl0k' Féron
#
# Distributed under terms of the GNU AGPLv3 license.

import mutagen
import os
import shutil
import tempfile
import time
import unittest

from hashlib import sha1
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from supysonic.db import Album, AlbumReviewTask, init_database, release_database, Track, Artist, Folder, Image
from supysonic.managers.folder import FolderManager
from supysonic.watcher import (
    FLAG_CREATE,
    FLAG_DIRECTORY,
    OP_MOVE,
    OP_REMOVE,
    OP_SCAN,
    ScannerProcessingQueue,
    SupysonicWatcher,
)

from ..testbase import TestConfig


class WatcherTestConfig(TestConfig):
    DAEMON = {"wait_delay": 0.5, "log_file": "/dev/null", "log_level": "DEBUG"}

    def __init__(self, db_uri):
        super().__init__(False, False)
        self.BASE["database_uri"] = db_uri


class WatcherTestBase(unittest.TestCase):
    @staticmethod
    def _table_exists(database, table_name):
        cursor = database.execute_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        )
        return cursor.fetchone() is not None

    @staticmethod
    def _column_exists(database, table_name, column_name):
        cursor = database.execute_sql(f"PRAGMA table_info({table_name})")
        return any(row[1] == column_name for row in cursor.fetchall())

    def setUp(self):
        self.__db = tempfile.mkstemp()
        dburi = "sqlite:///" + self.__db[1]
        init_database(dburi)
        database = Artist._meta.database

        if not self._table_exists(database, "album_artist"):
            database.execute_sql(
                """
                CREATE TABLE album_artist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    album_id INTEGER NOT NULL,
                    artist_id INTEGER NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        database.execute_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS album_artist_album_id_artist_id ON album_artist(album_id, artist_id)"
        )

        if not self._table_exists(database, "track_artist"):
            database.execute_sql(
                """
                CREATE TABLE track_artist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    track_id INTEGER NOT NULL,
                    artist_id INTEGER NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        database.execute_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS track_artist_track_id_artist_id ON track_artist(track_id, artist_id)"
        )

        if not self._table_exists(database, "image"):
            database.execute_sql(
                """
                CREATE TABLE image (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path VARCHAR(4096) NOT NULL,
                    image_type VARCHAR(10) NOT NULL,
                    related_id CHAR(36) NOT NULL,
                    created DATETIME NOT NULL
                )
                """
            )

        if not self._table_exists(database, "user_play_activity"):
            database.execute_sql(
                """
                CREATE TABLE user_play_activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    track_id CHAR(36) NOT NULL REFERENCES track(id) ON DELETE CASCADE,
                    user_id CHAR(36) NOT NULL REFERENCES user(id) ON DELETE CASCADE,
                    time DATETIME NOT NULL
                )
                """
            )
        database.execute_sql(
            "CREATE INDEX IF NOT EXISTS index_activity_user_id_fk ON user_play_activity(user_id)"
        )
        database.execute_sql(
            "CREATE INDEX IF NOT EXISTS index_activity_track_id_fk ON user_play_activity(track_id)"
        )

        if not self._column_exists(database, "artist", "artist_info_json"):
            database.execute_sql("ALTER TABLE artist ADD COLUMN artist_info_json VARCHAR(4096)")
        if not self._column_exists(database, "artist", "real_artist_id"):
            database.execute_sql("ALTER TABLE artist ADD COLUMN real_artist_id INTEGER")
        if not self._column_exists(database, "album", "year"):
            database.execute_sql("ALTER TABLE album ADD COLUMN year VARCHAR(255)")

        musicbrainz_patchers = [
            patch(
                "supysonic.scanner_func.scanner_album_enrich.search_musicbrainz_album",
                return_value={},
            ),
            patch(
                "supysonic.scanner_func.scanner_album_enrich.get_musicbrainz_album",
                return_value={},
            ),
            patch(
                "supysonic.scanner_func.scanner_enrich.search_musicbrainz_album",
                return_value={},
            ),
            patch(
                "supysonic.scanner_func.scanner_enrich.get_musicbrainz_album",
                return_value={},
            ),
            patch(
                "supysonic.scanner_func.scanner_cover.search_musicbrainz_album",
                return_value={},
            ),
            patch(
                "supysonic.scanner_func.scanner_cover.get_musicbrainz_album_image_info",
                return_value={},
            ),
        ]
        for patcher in musicbrainz_patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        conf = WatcherTestConfig(dburi)
        self.__sleep_time = conf.DAEMON["wait_delay"] + 1

        self.__watcher = SupysonicWatcher(conf)

    def tearDown(self):
        release_database()
        os.close(self.__db[0])
        os.remove(self.__db[1])

    def _start(self):
        self.__watcher.start()
        time.sleep(0.2)

    def _stop(self):
        self.__watcher.stop()

    def _is_alive(self):
        return self.__watcher.running

    def _sleep(self):
        time.sleep(self.__sleep_time)

    def _wait_until(self, predicate, timeout=None, interval=0.1):
        deadline = time.time() + (self.__sleep_time * 3 if timeout is None else timeout)
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()


class WatcherTestCase(WatcherTestBase):
    def setUp(self):
        super().setUp()
        self.__dir = tempfile.mkdtemp()
        FolderManager.add("Folder", self.__dir)
        self._start()

    def tearDown(self):
        self._stop()
        shutil.rmtree(self.__dir)
        super().tearDown()

    @staticmethod
    def _tempname():
        with tempfile.NamedTemporaryFile() as f:
            return os.path.basename(f.name)

    def _rootdir(self):
        return self.__dir

    def _temppath(self, suffix, depth=0):
        if depth > 0:
            dirpath = os.path.join(
                self.__dir, *(self._tempname() for _ in range(depth))
            )
            os.makedirs(dirpath)
        else:
            dirpath = self.__dir
        return os.path.join(dirpath, self._tempname() + suffix)

    def _addfile(self, depth=0):
        path = self._temppath(".mp3", depth)
        shutil.copyfile("tests/assets/folder/silence.mp3", path)
        return path

    def _addcover(self, suffix=None, depth=0):
        suffix = ".jpg" if suffix is None else (suffix + ".jpg")
        path = self._temppath(suffix, depth)
        shutil.copyfile("tests/assets/cover.jpg", path)
        return path


class AudioWatcherTestCase(WatcherTestCase):
    def assertTrackCountEqual(self, expected):
        self.assertEqual(Track.select().count(), expected)

    def _has_track_path(self, path):
        return Track.select().where(Track.path == path).count() == 1

    def _queue(self):
        return self._WatcherTestBase__watcher._SupysonicWatcher__queue

    def _add_file_to_dir(self, dirpath):
        os.makedirs(dirpath, exist_ok=True)
        path = os.path.join(dirpath, self._tempname() + ".mp3")
        shutil.copyfile("tests/assets/folder/silence.mp3", path)
        return path

    def test_add(self):
        self._addfile()
        self.assertTrackCountEqual(0)
        self._sleep()
        self.assertTrackCountEqual(1)

    def test_add_nowait_stop(self):
        self._addfile()
        # Add a small delay (< wait_delay) so wathdog can pick up that a file was added
        time.sleep(0.1)
        self._stop()
        self.assertTrackCountEqual(1)

    def test_add_multiple(self):
        self._addfile()
        self._addfile()
        self._addfile()
        self.assertTrackCountEqual(0)
        self._sleep()

        self.assertEqual(Track.select().count(), 3)
        self.assertEqual(Artist.select().count(), 1)

    def test_change(self):
        path = self._addfile()
        self._sleep()

        trackid = None
        self.assertEqual(Track.select().count(), 1)
        self.assertEqual(Artist.select().where(Artist.name == "Some artist").count(), 1)
        trackid = Track.select().first().id

        tags = mutagen.File(path, easy=True)
        tags["artist"] = "Renamed"
        tags.save()
        self.assertTrue(
            self._wait_until(
                lambda: Artist.select().where(Artist.name == "Some artist").count() == 0
                and Artist.select().where(Artist.name == "Renamed").count() == 1
            )
        )

        self.assertEqual(Track.select().count(), 1)
        self.assertEqual(Artist.select().where(Artist.name == "Some artist").count(), 0)
        self.assertEqual(Artist.select().where(Artist.name == "Renamed").count(), 1)
        self.assertEqual(Track.select().first().id, trackid)

    def test_rename(self):
        path = self._addfile()
        self._sleep()

        trackid = None
        self.assertEqual(Track.select().count(), 1)
        trackid = Track.select().first().id

        newpath = self._temppath(".mp3")
        shutil.move(path, newpath)
        self.assertTrue(
            self._wait_until(
                lambda: Track.select().count() == 1
                and self._has_track_path(newpath)
            )
        )

        track = Track.select().first()
        self.assertIsNotNone(track)
        self.assertNotEqual(track.path, path)
        self.assertEqual(track.path, newpath)
        self.assertEqual(
            track._path_hash, memoryview(sha1(newpath.encode("utf-8")).digest())
        )
        self.assertEqual(track.id, trackid)

    def test_move_in(self):
        filename = self._tempname() + ".mp3"
        initialpath = os.path.join(tempfile.gettempdir(), filename)
        shutil.copyfile("tests/assets/folder/silence.mp3", initialpath)
        shutil.move(initialpath, self._temppath(".mp3"))
        self._sleep()
        self.assertTrackCountEqual(1)

    def test_move_out(self):
        initialpath = self._addfile()
        self._sleep()
        self.assertTrackCountEqual(1)

        newpath = os.path.join(tempfile.gettempdir(), os.path.basename(initialpath))
        shutil.move(initialpath, newpath)
        self._sleep()
        self.assertTrackCountEqual(0)

        os.unlink(newpath)

    def test_directory_create_scans_existing_files(self):
        dirpath = os.path.join(self._rootdir(), self._tempname())
        path = self._add_file_to_dir(dirpath)
        self._queue().put(dirpath, OP_SCAN | FLAG_CREATE | FLAG_DIRECTORY)

        self.assertTrue(
            self._wait_until(
                lambda: Track.select().count() == 1
                and self._has_track_path(path)
            )
        )

    def test_directory_delete(self):
        path = self._addfile(depth=1)
        self._sleep()
        self.assertTrackCountEqual(1)

        dirpath = os.path.dirname(path)
        shutil.rmtree(dirpath)
        self._queue().put(dirpath, OP_REMOVE | FLAG_DIRECTORY)

        self.assertTrue(
            self._wait_until(
                lambda: Track.select().count() == 0
                and not Folder.select().where(Folder.path == dirpath).exists()
            )
        )
        self.assertTrackCountEqual(0)

    def test_directory_rename(self):
        path = self._addfile(depth=1)
        self._sleep()
        self.assertTrackCountEqual(1)

        old_dir = os.path.dirname(path)
        new_dir = os.path.join(os.path.dirname(old_dir), self._tempname())
        new_path = os.path.join(new_dir, os.path.basename(path))
        shutil.move(old_dir, new_dir)
        self._queue().put(new_dir, OP_MOVE | FLAG_DIRECTORY, src_path=old_dir)

        self.assertTrue(
            self._wait_until(
                lambda: Track.select().count() == 1
                and self._has_track_path(new_path)
                and not Folder.select().where(Folder.path == old_dir).exists()
            )
        )

        track = Track.select().first()
        self.assertEqual(track.path, new_path)
        self.assertFalse(Folder.select().where(Folder.path == old_dir).exists())

    def test_directory_delete_keeps_prefix_sibling(self):
        dirpath = os.path.join(self._rootdir(), "album")
        sibling_dir = os.path.join(self._rootdir(), "album-extra")
        self._add_file_to_dir(dirpath)
        sibling_path = self._add_file_to_dir(sibling_dir)

        self.assertTrue(self._wait_until(lambda: Track.select().count() == 2))

        shutil.rmtree(dirpath)
        self._queue().put(dirpath, OP_REMOVE | FLAG_DIRECTORY)

        self.assertTrue(
            self._wait_until(
                lambda: Track.select().count() == 1
                and self._has_track_path(sibling_path)
                and not Folder.select().where(Folder.path == dirpath).exists()
                and Folder.select().where(Folder.path == sibling_dir).exists()
            )
        )
        self.assertFalse(Folder.select().where(Folder.path == dirpath).exists())
        self.assertTrue(Folder.select().where(Folder.path == sibling_dir).exists())

    def test_delete(self):
        path = self._addfile()
        self._sleep()
        self.assertTrackCountEqual(1)

        os.unlink(path)
        self._sleep()
        self.assertTrackCountEqual(0)

    def test_add_delete(self):
        path = self._addfile()
        os.unlink(path)
        self._sleep()
        self.assertTrackCountEqual(0)

    def test_add_rename(self):
        path = self._addfile()
        shutil.move(path, self._temppath(".mp3"))
        self._sleep()
        self.assertTrackCountEqual(1)

    def test_rename_delete(self):
        path = self._addfile()
        self._sleep()
        self.assertTrackCountEqual(1)

        newpath = self._temppath(".mp3")
        shutil.move(path, newpath)
        os.unlink(newpath)
        self._sleep()
        self.assertTrackCountEqual(0)

    def test_add_rename_delete(self):
        path = self._addfile()
        newpath = self._temppath(".mp3")
        shutil.move(path, newpath)
        os.unlink(newpath)
        self._sleep()
        self.assertTrackCountEqual(0)

    def test_rename_rename(self):
        path = self._addfile()
        self._sleep()
        self.assertTrackCountEqual(1)

        newpath = self._temppath(".mp3")
        finalpath = self._temppath(".mp3")
        shutil.move(path, newpath)
        shutil.move(newpath, finalpath)
        self._sleep()
        self.assertTrackCountEqual(1)


class CoverWatcherTestCase(WatcherTestCase):
    def assertAlbumCoverImageEqual(self, expected_path):
        self.assertTrue(
            self._wait_until(
                lambda: Image.select().count() == 1
                and Image.select().first().path == expected_path
            )
        )
        self.assertEqual(Image.select().count(), 1)
        self.assertEqual(Image.select().first().path, expected_path)

    def assertAlbumCoverImageMissing(self):
        self.assertTrue(self._wait_until(lambda: Image.select().count() == 0))
        self.assertEqual(Image.select().count(), 0)

    def test_add_file_then_cover(self):
        self._addfile()
        path = self._addcover()
        self._sleep()

        self.assertEqual(Folder.select().first().cover_art, os.path.basename(path))
        self.assertAlbumCoverImageEqual(path)

    def test_add_cover_then_file(self):
        path = self._addcover()
        self._addfile()
        self._sleep()

        self.assertEqual(Folder.select().first().cover_art, os.path.basename(path))
        self.assertAlbumCoverImageEqual(path)

    def test_add_cover_then_file_in_new_folder_syncs_cover(self):
        self._stop()
        dirpath = os.path.join(self._rootdir(), self._tempname())
        os.makedirs(dirpath)
        cover_path = os.path.join(dirpath, "cover.jpg")
        shutil.copyfile("tests/assets/cover.jpg", cover_path)

        self._start()
        audio_path = os.path.join(dirpath, self._tempname() + ".mp3")
        shutil.copyfile("tests/assets/folder/silence.mp3", audio_path)

        self.assertTrue(
            self._wait_until(
                lambda: Folder.select()
                .where(Folder.path == dirpath, Folder.cover_art == "cover.jpg")
                .exists()
                and Image.select().where(Image.path == cover_path).exists()
            )
        )
        self.assertEqual(Folder.get(Folder.path == dirpath).cover_art, "cover.jpg")
        self.assertAlbumCoverImageEqual(cover_path)

    def test_remove_cover(self):
        self._addfile()
        path = self._addcover()
        self._sleep()

        os.unlink(path)
        self.assertTrue(self._wait_until(lambda: Folder.select().first().cover_art is None))

        self.assertIsNone(Folder.select().first().cover_art)
        self.assertAlbumCoverImageMissing()

    def test_naming_add_good(self):
        self._addcover()
        self._sleep()
        good = os.path.basename(self._addcover("cover"))
        self._sleep()

        self.assertEqual(Folder.select().first().cover_art, good)

    def test_naming_add_bad(self):
        good = os.path.basename(self._addcover("cover"))
        self._sleep()
        self._addcover()
        self._sleep()

        self.assertEqual(Folder.select().first().cover_art, good)

    def test_naming_remove_good(self):
        bad = self._addcover()
        good = self._addcover("cover")
        self._sleep()
        os.unlink(good)
        self._sleep()

        self.assertEqual(Folder.select().first().cover_art, os.path.basename(bad))

    def test_naming_remove_bad(self):
        bad = self._addcover()
        good = self._addcover("cover")
        self._sleep()
        os.unlink(bad)
        self._sleep()

        self.assertEqual(Folder.select().first().cover_art, os.path.basename(good))

    def test_rename(self):
        path = self._addcover()
        self._sleep()
        newpath = self._temppath(".jpg")
        shutil.move(path, newpath)
        self._sleep()

        self.assertEqual(Folder.select().first().cover_art, os.path.basename(newpath))

    def test_add_to_folder_without_track(self):
        path = self._addcover(depth=1)
        self._sleep()

        self.assertFalse(
            Folder.select().where(Folder.cover_art == os.path.basename(path)).exists()
        )

    def test_remove_from_folder_without_track(self):
        path = self._addcover(depth=1)
        self._sleep()
        os.unlink(path)
        self._sleep()

    def test_add_track_to_empty_folder(self):
        self._addfile(1)
        self._sleep()


class RegularItemCoverRefreshTestCase(unittest.TestCase):
    @staticmethod
    def _scanner():
        return SimpleNamespace(
            find_cover=Mock(),
            move_file=Mock(),
            remove_file=Mock(),
            scan_file=Mock(),
        )

    def test_regular_scan_refreshes_parent_cover(self):
        scanner = self._scanner()
        item = SimpleNamespace(
            operation=OP_SCAN,
            path="/music/Album/track.flac",
            src_path=None,
        )

        ScannerProcessingQueue(0)._ScannerProcessingQueue__process_regular_item(
            scanner, item
        )

        scanner.scan_file.assert_called_once_with("/music/Album/track.flac")
        scanner.find_cover.assert_called_once_with("/music/Album")

    def test_regular_move_refreshes_source_and_destination_parent_covers(self):
        scanner = self._scanner()
        item = SimpleNamespace(
            operation=OP_MOVE,
            path="/music/New Album/track.flac",
            src_path="/music/Old Album/track.flac",
        )

        ScannerProcessingQueue(0)._ScannerProcessingQueue__process_regular_item(
            scanner, item
        )

        scanner.move_file.assert_called_once_with(
            "/music/Old Album/track.flac",
            "/music/New Album/track.flac",
        )
        scanner.find_cover.assert_has_calls(
            [call("/music/Old Album"), call("/music/New Album")]
        )


class DummyObserver:
    def __init__(self):
        self._alive = False

    def schedule(self, handler, path, recursive=True):
        return path

    def unschedule(self, watch):
        return None

    def start(self):
        self._alive = True

    def stop(self):
        self._alive = False

    def join(self):
        return None

    def is_alive(self):
        return self._alive


class ReviewTaskWatcherTestCase(WatcherTestBase):
    def setUp(self):
        super().setUp()
        self.__dir = tempfile.mkdtemp()
        FolderManager.add("Folder", self.__dir)

    def tearDown(self):
        if self._is_alive():
            self._stop()
        shutil.rmtree(self.__dir)
        super().tearDown()

    def test_regular_scan_batch_creates_pending_review_task(self):
        artist = Artist.create(name="Review Artist")
        album = Album.create(name="Review Album", artist=artist, year="2024")
        prune = Mock()
        fake_scanner = SimpleNamespace(
            review_task_album_ids={album.id},
            scan_file=lambda path: None,
            find_cover=lambda path: None,
            find_lost_information=lambda: None,
            prune=prune,
            stats=lambda: SimpleNamespace(
                lost_covers=SimpleNamespace(artists=0, albums=0),
                lost_covers_albums={},
                lost_covers_artists=[],
                lost_year_albums={},
            ),
        )

        with patch("supysonic.watcher.Observer", DummyObserver), patch(
            "supysonic.watcher.Scanner", return_value=fake_scanner
        ):
            self._start()
            queue = self._WatcherTestBase__watcher._SupysonicWatcher__queue
            queue.put(os.path.join(self.__dir, "track.mp3"), OP_SCAN)
            self._sleep()

        self.assertEqual(AlbumReviewTask.select().count(), 1)
        task = AlbumReviewTask.get()
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.album_id, album.id)
        self.assertEqual(prune.call_count, 3)


if __name__ == "__main__":
    unittest.main()
