import os
import shutil
import tempfile
import unittest

from types import SimpleNamespace
from unittest.mock import Mock, patch

from supysonic import db
from supysonic.scanner_func.scanner_cover import repairAlbumCover


class ScannerCoverTraceTestCase(unittest.TestCase):
    def test_repair_album_cover_logs_folder_cover_hit(self):
        scanner = SimpleNamespace(
            stats=lambda: SimpleNamespace(
                lost_covers=SimpleNamespace(albums=1),
                lost_covers_albums={},
            )
        )
        album = SimpleNamespace(id=3, name="Album", artist=SimpleNamespace(get_artist_name=lambda: "Artist"))
        track = SimpleNamespace(path="/music/Artist/Album/track.flac")
        track_query = Mock()
        track_query.where.return_value.order_by.return_value = [track]

        with patch("supysonic.scanner_func.scanner_cover.Track.select", return_value=track_query), patch(
            "supysonic.scanner_func.scanner_cover.find_cover_in_folder",
            return_value=SimpleNamespace(name="cover.jpg"),
        ), patch(
            "supysonic.scanner_func.scanner_cover.Image.get_or_create"
        ), patch(
            "supysonic.scanner_func.scanner_cover.markAlbumCoverRestored"
        ), patch(
            "supysonic.scanner_func.scanner_cover.logTrace",
            create=True,
        ) as log_trace:
            repairAlbumCover(scanner, album)

        _, trace_type, header_fields, detail_lines = log_trace.call_args[0]
        self.assertEqual(trace_type, "ALBUM_COVER_TRACE")
        self.assertEqual(header_fields["album"], "Album")
        self.assertEqual(header_fields["track_path"], track.path)
        self.assertIn("cover source: folder file", detail_lines)
        self.assertIn("selected cover file: cover.jpg", detail_lines)

    def test_repair_album_cover_logs_when_no_source_succeeds(self):
        scanner = SimpleNamespace(
            stats=lambda: SimpleNamespace(
                lost_covers=SimpleNamespace(albums=1),
                lost_covers_albums={},
            )
        )
        album = SimpleNamespace(id=3, name="Album", artist=SimpleNamespace(get_artist_name=lambda: "Artist"))
        track = SimpleNamespace(path="/music/Artist/Album/track.flac")
        track_query = Mock()
        track_query.where.return_value.order_by.return_value = [track]

        with patch("supysonic.scanner_func.scanner_cover.Track.select", return_value=track_query), patch(
            "supysonic.scanner_func.scanner_cover.find_cover_in_folder",
            return_value=None,
        ), patch(
            "supysonic.scanner_func.scanner_cover.mediafile.MediaFile",
            return_value=SimpleNamespace(art=None),
        ), patch(
            "supysonic.scanner_func.scanner_cover.logTrace",
            create=True,
        ) as log_trace:
            repairAlbumCover(scanner, album, get_cover_interner=False)

        _, trace_type, _, detail_lines = log_trace.call_args[0]
        self.assertEqual(trace_type, "ALBUM_COVER_TRACE")
        self.assertIn("folder cover lookup: miss", detail_lines)
        self.assertIn("embedded artwork: track path missing", detail_lines)
        self.assertIn("cover repair result: no source succeeded", detail_lines)


class ScannerCoverInvalidPathTestCase(unittest.TestCase):
    def setUp(self):
        db.init_database("sqlite:")

    def tearDown(self):
        db.release_database()

    def test_repair_album_cover_removes_stale_track_path(self):
        base_path = tempfile.mkdtemp()
        track_dir = os.path.join(base_path, "Artist", "Album")
        track_path = os.path.join(track_dir, "track.flac")
        shutil.rmtree(base_path)

        root = db.Folder.create(root=True, name="Root", path=base_path)
        folder = db.Folder.create(
            root=False,
            name="Album",
            path=track_dir,
            parent=root,
        )
        artist = db.Artist.create(name="Artist")
        album = db.Album.create(name="Album", artist=artist)
        db.Track.create(
            title="Track",
            album=album,
            artist=artist,
            disc=1,
            number=1,
            duration=1,
            bitrate=320,
            path=track_path,
            last_modification=1234,
            root_folder=root,
            folder=folder,
        )

        class ScannerStub:
            def __init__(self):
                self.removed_path = None
                self._stats = SimpleNamespace(
                    lost_covers=SimpleNamespace(albums=1),
                    lost_covers_albums={},
                    deleted=SimpleNamespace(tracks=0),
                )

            def stats(self):
                return self._stats

            def remove_file(self, path):
                self.removed_path = path
                db.Track.get(path=path).delete_instance(recursive=True)
                self._stats.deleted.tracks += 1

        scanner = ScannerStub()

        with patch(
            "supysonic.scanner_func.scanner_cover.mediafile.MediaFile"
        ) as media_file, patch(
            "supysonic.scanner_func.scanner_cover.logTrace",
            create=True,
        ) as log_trace:
            repairAlbumCover(scanner, album)

        self.assertEqual(scanner.removed_path, track_path)
        self.assertFalse(db.Track.select().where(db.Track.path == track_path).exists())
        media_file.assert_not_called()
        _, trace_type, header_fields, detail_lines = log_trace.call_args[0]
        self.assertEqual(trace_type, "ALBUM_COVER_TRACE")
        self.assertEqual(header_fields["track_path"], track_path)
        self.assertIn("stale track path removed", detail_lines)
        self.assertIn("cover repair result: invalid track path", detail_lines)

    def test_repair_album_cover_continues_after_stale_track_path(self):
        base_path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base_path, ignore_errors=True)
        stale_dir = os.path.join(base_path, "missing")
        stale_path = os.path.join(stale_dir, "stale.flac")
        valid_dir = os.path.join(base_path, "Artist", "Album")
        valid_path = os.path.join(valid_dir, "track.flac")
        os.makedirs(valid_dir)
        cover_path = os.path.join(valid_dir, "cover.jpg")
        shutil.copyfile("tests/assets/cover.jpg", cover_path)

        root = db.Folder.create(root=True, name="Root", path=base_path)
        stale_folder = db.Folder.create(
            root=False,
            name="Missing",
            path=stale_dir,
            parent=root,
        )
        valid_folder = db.Folder.create(
            root=False,
            name="Album",
            path=valid_dir,
            parent=root,
        )
        artist = db.Artist.create(name="Artist")
        album = db.Album.create(name="Album", artist=artist)
        db.Track.create(
            title="Stale Track",
            album=album,
            artist=artist,
            disc=1,
            number=1,
            duration=1,
            bitrate=320,
            path=stale_path,
            last_modification=1234,
            root_folder=root,
            folder=stale_folder,
        )
        db.Track.create(
            title="Valid Track",
            album=album,
            artist=artist,
            disc=1,
            number=2,
            duration=1,
            bitrate=320,
            path=valid_path,
            last_modification=1234,
            root_folder=root,
            folder=valid_folder,
        )

        class ScannerStub:
            def __init__(self):
                self.removed_paths = []
                self._stats = SimpleNamespace(
                    lost_covers=SimpleNamespace(albums=1),
                    lost_covers_albums={},
                    deleted=SimpleNamespace(tracks=0),
                )

            def stats(self):
                return self._stats

            def remove_file(self, path):
                self.removed_paths.append(path)
                db.Track.get(path=path).delete_instance(recursive=True)
                self._stats.deleted.tracks += 1

        scanner = ScannerStub()

        with patch(
            "supysonic.scanner_func.scanner_cover.mediafile.MediaFile"
        ) as media_file, patch(
            "supysonic.scanner_func.scanner_cover.logTrace",
            create=True,
        ) as log_trace:
            repairAlbumCover(scanner, album)

        self.assertEqual(scanner.removed_paths, [stale_path])
        self.assertFalse(db.Track.select().where(db.Track.path == stale_path).exists())
        self.assertTrue(db.Track.select().where(db.Track.path == valid_path).exists())
        self.assertEqual(db.Image.select().count(), 1)
        self.assertEqual(db.Image.select().first().path, cover_path)
        media_file.assert_not_called()

        detail_blocks = [call_args[0][3] for call_args in log_trace.call_args_list]
        self.assertIn("stale track path removed", detail_blocks[0])
        self.assertIn("cover source: folder file", detail_blocks[-1])
