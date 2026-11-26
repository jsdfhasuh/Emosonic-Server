import hashlib
import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import requests
from mediafile import MediaFile

from supysonic import db
from supysonic.album_discovery_catalog import CatalogAlbum, CatalogTrack
from supysonic.album_discovery_delivery import AlbumReconciliation, reconcile_album
from supysonic.album_discovery_oracle import DeliveryFile, DeliveryManifest, OracleAlbumClient


ALBUM_URL = "https://music.apple.com/cn/album/release/100"
ALBUM_ARTIST = "Artist"
ALBUM_TITLE = "Release"
SOURCE = Path(__file__).parents[1] / "assets" / "formats" / "silence.flac"
ISRC = "KRA401400129"


class AlbumDeliveryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = (Path(directory.name) / "completed").resolve()
        self.root.mkdir()
        db.init_database("sqlite:///" + str(self.root.parent / "delivery.db"))
        self.addCleanup(db.release_database)
        self.folder = db.Folder.create(root=True, name="library", path=str(self.root))
        self.artist = db.Artist.create(name=ALBUM_ARTIST)
        self.local_album = db.Album.create(artist=self.artist, name=ALBUM_TITLE)
        self.length = MediaFile(str(SOURCE)).length
        self.expected = (
            self.expected_track(101, "First", 1, 1), self.expected_track(102, "Second", 1, 3),
            self.expected_track(103, "Third", 2, 1),
        )
        self.album = CatalogAlbum("100", ALBUM_TITLE, ALBUM_ARTIST, ALBUM_URL, self.expected)

    def expected_track(self, identifier: int, title: str, disc: int, number: int) -> CatalogTrack:
        return CatalogTrack(str(identifier), title, ALBUM_ARTIST, disc, number,
                            round(self.length * 1000), ISRC)

    def write_audio(self, relative: str, item: CatalogTrack, album: str = ALBUM_TITLE) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE, path)
        media = MediaFile(str(path))
        media.title, media.album = item.title, album
        media.artist = media.albumartist = ALBUM_ARTIST
        media.disc, media.track, media.isrc = item.disc_number, item.track_number, ISRC
        media.save()
        return path

    def add_index(self, path: Path, item: CatalogTrack, album: db.Album = None) -> db.Track:
        return db.Track.create(
            path=str(path), disc=item.disc_number, number=item.track_number, title=item.title,
            duration=round(self.length), bitrate=123, last_modification=0, artist=self.artist,
            album=album or self.local_album, root_folder=self.folder, folder=self.folder,
        )

    def delivered(self, path: Path) -> DeliveryFile:
        return DeliveryFile(
            path.relative_to(self.root).as_posix(), path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def manifest(self, batch_id: str, *files: DeliveryFile) -> DeliveryManifest:
        return DeliveryManifest(batch_id, ALBUM_URL, "cleaned", files, "a" * 64)

    def test_partial_cross_batch_receipts_skip_attachments_and_wait_for_index(self) -> None:
        first = self.write_audio("Artist/Release/CD1/1-01 First.flac", self.expected[0])
        first_id = str(self.add_index(first, self.expected[0]).id)
        second = self.write_audio("Artist/Release/CD1/1-03 Second.flac", self.expected[1])
        third = self.write_audio("Artist/Release/CD2/2-01 Third.flac", self.expected[2])
        third_id = str(self.add_index(third, self.expected[2]).id)
        foreign = self.write_audio("Artist/Anthology/1-03 Second.flac", self.expected[1], "Anthology")
        first_batch = self.manifest(
            "batch-a", self.delivered(second), self.delivered(foreign),
            DeliveryFile("Artist/Release/cover.jpg", 9, "a" * 64),
        )
        second_batch = self.manifest("batch-b", self.delivered(third), self.delivered(second))
        partial = reconcile_album(self.album, "102", self.root, [first_batch, second_batch])
        self.assertFalse(partial.ready)
        self.assertEqual({item.track_id for item in partial.verified}, {first_id, third_id})
        self.assertEqual(partial.missing[0].track_number, 3)
        self.assertEqual(partial.missing[0].reason, "waiting_for_index")
        self.assertEqual(partial.missing[0].sha256, self.delivered(second).sha256)
        self.assertEqual(AlbumReconciliation.from_payload(partial.to_payload()), partial)

        second_id = str(self.add_index(second, self.expected[1]).id)
        verified = reconcile_album(
            self.album, "102", self.root, previous_tracks=partial.to_payload()["tracks"],
            require_receipts=True,
        )
        self.assertTrue(verified.ready)
        self.assertEqual(verified.local_album_id, str(self.local_album.id))
        self.assertEqual(verified.representative_track_id, second_id)
        self.assertEqual([item.track_id for item in verified.verified], [first_id, second_id, third_id])
        replayed = reconcile_album(
            self.album, "102", self.root, [first_batch, second_batch, first_batch],
        )
        self.assertEqual([item.track_id for item in replayed.verified], [first_id, second_id, third_id])

    def test_same_path_checksum_conflict_has_no_verified_track(self) -> None:
        track = self.expected[0]
        path = self.write_audio("Artist/Release/1-01 First.flac", track)
        self.add_index(path, track)
        file = self.delivered(path)
        batch = self.manifest("batch", DeliveryFile(file.path, file.size, "b" * 64))
        result = reconcile_album(self.album, track.song_id, self.root, [batch])
        self.assertFalse(result.ready)
        self.assertEqual(result.tracks[0].reason, "checksum_conflict")
        self.assertIsNone(result.tracks[0].track_id)
        self.assertEqual(self.delivered(path), file)

    def test_order_rejects_new_indexed_audio_without_receipt(self) -> None:
        track = self.expected[0]
        album = CatalogAlbum("100", ALBUM_TITLE, ALBUM_ARTIST, ALBUM_URL, (track,))
        path = self.write_audio("Artist/Release/1-01 First.flac", track)
        local_id = str(self.add_index(path, track).id)
        pending = reconcile_album(album, track.song_id, self.root, require_receipts=True)
        self.assertFalse(pending.ready)
        self.assertEqual(pending.tracks[0].reason, "waiting_for_receipt")
        self.assertIsNone(pending.tracks[0].sha256)

        batch = self.manifest("batch", self.delivered(path))
        received = reconcile_album(
            album, track.song_id, self.root, [batch], require_receipts=True,
        )
        self.assertEqual(received.representative_track_id, local_id)
        self.assertTrue(received.ready)

    def test_duplicate_track_and_other_edition_are_not_counted(self) -> None:
        track = self.expected[0]
        album = CatalogAlbum("100", ALBUM_TITLE, ALBUM_ARTIST, ALBUM_URL, (track,))
        anthology = db.Album.create(artist=self.artist, name="Anthology")
        unrelated = self.write_audio("Artist/Anthology/First.flac", track, "Anthology")
        self.add_index(unrelated, track, anthology)
        result = reconcile_album(album, track.song_id, self.root)
        self.assertFalse(result.ready)
        self.assertEqual(result.verified, ())

        one = self.write_audio("Artist/Release/1-01 First.flac", track)
        two = self.write_audio("Artist/Release/1-01 First remaster.flac", track)
        self.add_index(one, track)
        self.add_index(two, track)
        result = reconcile_album(album, track.song_id, self.root)
        self.assertFalse(result.ready)
        self.assertEqual(result.missing[0].reason, "ambiguous_recording")


class OracleClientTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.session = Mock(spec=requests.Session)
        self.client = OracleAlbumClient("https://oracle.invalid", "control", "delivery",
                                        session=self.session)

    def response(self, status: int, payload: object) -> Mock:
        return Mock(status_code=status, json=Mock(return_value=payload))

    def state(self, disposition: str, task_id=None, batch_ids=(), order_id="order") -> dict:
        return {
            "status": disposition, "albumId": "100", "orderId": order_id,
            "taskId": task_id, "taskStatus": "queued" if task_id else None,
            "batchIds": list(batch_ids),
        }

    def batch(self, batch_id: str, *, url=ALBUM_URL, file="Artist/Release/First.flac") -> dict:
        body = {
            "id": batch_id, "url": url,
            "files": [{"id": 0, "path": file, "size": 20, "sha256": "a" * 64}],
        }
        canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {**body, "state": "cleaned",
                "manifestSha256": hashlib.sha256(canonical.encode()).hexdigest()}

    def test_only_matching_durable_nonacceptance_releases_an_order(self) -> None:
        proof = {**self.state("rejected"), "decision": "closed_without_acceptance"}
        for response in (
            self.response(401, proof), self.response(404, proof),
            self.response(200, self.state("absent")),
            self.response(200, self.state("rejected")),
            self.response(200, {**proof, "orderId": "different"}),
            self.response(200, {**proof, "albumId": "999"}),
        ):
            self.session.request.return_value = response
            self.assertEqual(self.client.close_if_unaccepted("100", ALBUM_URL, "order").status, "unknown")
        self.session.request.return_value = self.response(200, proof)
        self.assertEqual(self.client.close_if_unaccepted("100", ALBUM_URL, "order").status, "rejected")
        self.assertTrue(self.session.request.call_args.args[1].endswith("/100/close-if-unaccepted"))

    def test_lost_response_keeps_unknown_then_reports_owned_task_after_restart(self) -> None:
        self.session.request.side_effect = (
            requests.Timeout(),
            self.response(200, self.state("submitted", "new-task", ["older-task", "new-task"])),
            self.response(200, self.batch("older-task")),
            self.response(200, self.batch("new-task")),
        )
        self.assertEqual(self.client.submit("100", ALBUM_URL, "order").status, "unknown")
        state = self.client.inspect("100", ALBUM_URL, "known-old-task", "order")
        self.assertEqual(state.status, "submitted")
        self.assertEqual(state.task_id, "new-task")
        self.assertEqual([batch.batch_id for batch in state.deliveries], ["older-task", "new-task"])
        post, lookup, old_batch, new_batch = self.session.request.call_args_list
        self.assertEqual(post.kwargs["json"], {"url": ALBUM_URL, "orderId": "order"})
        self.assertFalse(post.kwargs["allow_redirects"])
        self.assertEqual(lookup.kwargs["params"], {"orderId": "order", "taskId": "known-old-task"})
        self.assertEqual(lookup.kwargs["headers"]["Authorization"], "Bearer control")
        self.assertEqual(old_batch.kwargs["headers"]["Authorization"], "Bearer delivery")
        self.assertEqual(new_batch.kwargs["headers"]["Authorization"], "Bearer delivery")

    def test_missing_protocol_route_and_region_mismatch_are_not_absence(self) -> None:
        self.session.request.return_value = self.response(404, {})
        self.assertEqual(self.client.inspect("100", ALBUM_URL).status, "unknown")
        self.session.request.reset_mock()
        for url in (ALBUM_URL + "?i=101", ALBUM_URL.replace("/cn/", "/hk/")):
            self.assertEqual(self.client.submit("100", url, "order").status, "unknown")
        self.session.request.assert_not_called()

    def test_batches_require_cn_edition_canonical_digest_and_safe_paths(self) -> None:
        bad_digest = {**self.batch("digest"), "manifestSha256": "f" * 64}
        self.session.request.side_effect = (
            self.response(200, self.state("associated", "task", ["region", "digest", "path"])),
            self.response(200, self.batch("region", url=ALBUM_URL.replace("/cn/", "/hk/"))),
            self.response(200, bad_digest),
            self.response(200, self.batch("path", file="Artist/Release/../outside.flac")),
        )
        result = self.client.inspect("100", ALBUM_URL, order_id="order")
        self.assertEqual(result.status, "associated")
        self.assertEqual(result.deliveries, ())
        self.assertEqual(result.detail, "delivery_manifest_invalid")
