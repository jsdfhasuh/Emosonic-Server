# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2017-2022 Alban 'spl0k' Féron
#
# Distributed under terms of the GNU AGPLv3 license.

import os.path
import unittest
import uuid

from contextlib import closing
from io import BytesIO
from mutagen.flac import FLAC
from PIL import Image
from unittest.mock import Mock, patch

from supysonic.db import Folder, Artist, Album, Track

from .apitestbase import ApiTestBase


class MediaTestCase(ApiTestBase):
    def setUp(self):
        super().setUp()

        folder = Folder.create(
            name="Root",
            path=os.path.abspath("tests/assets"),
            root=True,
            cover_art="cover.jpg",
        )
        folder = Folder.get(name="Root")
        self.folderid = folder.id

        artist = Artist.create(name="Artist")
        album = Album.create(artist=artist, name="Album")

        track = Track.create(
            title="23bytes",
            number=1,
            disc=1,
            artist=artist,
            album=album,
            path=os.path.abspath("tests/assets/23bytes"),
            root_folder=folder,
            folder=folder,
            duration=2,
            bitrate=320,
            last_modification=0,
        )
        self.trackid = track.id

        self.formats = ["mp3", "flac", "ogg", "m4a"]
        self.format_trackids = {}
        for i, format_name in enumerate(self.formats):
            track_embeded_art = Track.create(
                title="[silence]",
                number=1,
                disc=1,
                artist=artist,
                album=album,
                path=os.path.abspath(f"tests/assets/formats/silence.{format_name}"),
                root_folder=folder,
                folder=folder,
                duration=2,
                bitrate=320,
                last_modification=0,
            )
            self.format_trackids[format_name] = track_embeded_art.id
            self.formats[i] = track_embeded_art.id

    def test_lyrics_ignores_malformed_external_response(self):
        self.config.WEBAPP["online_lyrics"] = True
        response = Mock(content=b"<SearchLyricResult></Lyric>")

        with patch("supysonic.api.media.requests.get", return_value=response):
            rv, child = self._make_request(
                "getLyrics",
                {"artist": "Nobody", "title": "Nowhere"},
                tag="lyrics",
            )

        self.assertIsNone(child.text)

    def test_stream(self):
        self._make_request("stream", error=10)
        self._make_request("stream", {"id": "string"}, error=0)
        self._make_request("stream", {"id": str(uuid.uuid4())}, error=70)
        self._make_request("stream", {"id": str(self.folderid)}, error=0)
        self._make_request(
            "stream", {"id": str(self.trackid), "maxBitRate": "string"}, error=0
        )
        self._make_request(
            "stream", {"id": str(self.trackid), "timeOffset": 2}, error=0
        )
        self._make_request(
            "stream", {"id": str(self.trackid), "size": "640x480"}, error=0
        )

        with closing(
            self.client.get(
                "/rest/stream.view",
                query_string={
                    "u": "alice",
                    "p": "Alic3",
                    "c": "tests",
                    "id": str(self.trackid),
                },
            )
        ) as rv:
            self.assertEqual(rv.status_code, 200)
            self.assertEqual(len(rv.data), 23)
        self.assertEqual(Track[self.trackid].play_count, 1)

    def test_stream_media_headers_head_and_range_probe(self):
        trackid = self.format_trackids["flac"]
        track = Track[trackid]
        args = {
            "u": "alice",
            "p": "Alic3",
            "c": "tests",
            "id": str(trackid),
        }

        with closing(
            self.client.head("/rest/stream.view", query_string=args)
        ) as rv:
            self.assertEqual(rv.status_code, 200)
            self.assertEqual(rv.data, b"")
            self.assertEqual(rv.headers["Accept-Ranges"], "bytes")
            self.assertEqual(rv.headers["EmoSonic-Source-Container"], "flac")
            self.assertEqual(rv.headers["EmoSonic-Output-Container"], "flac")
            self.assertEqual(
                rv.headers["EmoSonic-Source-Attached-Picture-Count"], "1"
            )
            self.assertEqual(
                rv.headers["EmoSonic-Output-Attached-Picture-Count"], "1"
            )
            self.assertEqual(rv.headers["EmoSonic-Sanitized-Available"], "true")
            self.assertEqual(
                rv.headers["EmoSonic-Preferred-Compatible-Variant"],
                "flac_no_picture",
            )
            self.assertEqual(rv.headers["EmoSonic-Stream-Variant"], "original")
            self.assertEqual(
                rv.headers["EmoSonic-Output-Content-Length"],
                str(os.path.getsize(track.path)),
            )
        self.assertEqual(Track[trackid].play_count, 0)

        with closing(
            self.client.get(
                "/rest/stream.view",
                query_string=args,
                headers={"Range": "bytes=0-0"},
            )
        ) as rv:
            self.assertEqual(rv.status_code, 206)
            self.assertEqual(rv.headers["Content-Length"], "1")
            self.assertEqual(
                rv.headers["Content-Range"],
                f"bytes 0-0/{os.path.getsize(track.path)}",
            )
            self.assertEqual(
                rv.headers["EmoSonic-Output-Content-Length"],
                str(os.path.getsize(track.path)),
            )
            self.assertEqual(len(rv.data), 1)
        self.assertEqual(Track[trackid].play_count, 0)

    def test_stream_flac_no_picture_variant(self):
        trackid = self.format_trackids["flac"]
        args = {
            "u": "alice",
            "p": "Alic3",
            "c": "tests",
            "id": str(trackid),
            "variant": "flac_no_picture",
        }

        with closing(self.client.get("/rest/stream.view", query_string=args)) as rv:
            self.assertEqual(rv.status_code, 200)
            self.assertEqual(rv.mimetype, "audio/flac")
            self.assertEqual(rv.headers["Accept-Ranges"], "bytes")
            self.assertEqual(
                rv.headers["EmoSonic-Stream-Variant"], "flac_no_picture"
            )
            self.assertEqual(
                rv.headers["EmoSonic-Source-Attached-Picture-Count"], "1"
            )
            self.assertEqual(
                rv.headers["EmoSonic-Output-Attached-Picture-Count"], "0"
            )
            self.assertIn("EmoSonic-Sanitized-Fingerprint", rv.headers)
            self.assertEqual(
                rv.headers["EmoSonic-Output-Content-Length"],
                rv.headers["Content-Length"],
            )
            self.assertEqual(len(FLAC(BytesIO(rv.data)).pictures), 0)
        self.assertEqual(Track[trackid].play_count, 1)

    def test_stream_media_headers_tolerate_none_images(self):
        trackid = self.format_trackids["mp3"]
        args = {
            "u": "alice",
            "p": "Alic3",
            "c": "tests",
            "id": str(trackid),
        }
        fake_tag = Mock(
            type="mp3",
            samplerate=44100,
            bitdepth=0,
            channels=2,
            bitrate=128000,
            length=4.8,
            images=None,
        )

        with patch("supysonic.api.media.mediafile.MediaFile", return_value=fake_tag):
            with closing(
                self.client.get("/rest/stream.view", query_string=args)
            ) as rv:
                self.assertEqual(rv.status_code, 200)
                self.assertEqual(
                    rv.headers["EmoSonic-Source-Attached-Picture-Count"], "0"
                )
                self.assertEqual(
                    rv.headers["EmoSonic-Output-Attached-Picture-Count"], "0"
                )
                self.assertEqual(rv.headers["EmoSonic-Sanitized-Available"], "false")

    def test_stream_flac_headers_fall_back_to_mutagen_pictures(self):
        trackid = self.format_trackids["flac"]
        args = {
            "u": "alice",
            "p": "Alic3",
            "c": "tests",
            "id": str(trackid),
        }
        fake_tag = Mock(
            type="flac",
            samplerate=96000,
            bitdepth=24,
            channels=2,
            bitrate=2867904,
            length=180.0,
            images=None,
        )
        fake_flac = Mock(
            pictures=[
                Mock(
                    mime="image/png",
                    width=4000,
                    height=4000,
                    data=b"image",
                )
            ]
        )

        with patch("supysonic.api.media.mediafile.MediaFile", return_value=fake_tag):
            with patch("supysonic.api.media.FLAC", return_value=fake_flac):
                with closing(
                    self.client.head("/rest/stream.view", query_string=args)
                ) as rv:
                    self.assertEqual(rv.status_code, 200)
                    self.assertEqual(
                        rv.headers["EmoSonic-Source-Attached-Picture-Count"], "1"
                    )
                    self.assertEqual(
                        rv.headers["EmoSonic-Source-Attached-Picture-Codec"], "png"
                    )
                    self.assertEqual(
                        rv.headers["EmoSonic-Source-Attached-Picture-Width"], "4000"
                    )
                    self.assertEqual(
                        rv.headers["EmoSonic-Source-Attached-Picture-Height"], "4000"
                    )
                    self.assertEqual(
                        rv.headers["EmoSonic-Sanitized-Available"], "true"
                    )

    def test_stream_variant_errors_have_http_status(self):
        base_args = {
            "u": "alice",
            "p": "Alic3",
            "c": "tests",
            "id": str(self.trackid),
        }

        args = dict(base_args, variant="unknown")
        with closing(self.client.get("/rest/stream.view", query_string=args)) as rv:
            self.assertEqual(rv.status_code, 400)

        args = dict(base_args, variant="flac_no_picture")
        with closing(self.client.get("/rest/stream.view", query_string=args)) as rv:
            self.assertEqual(rv.status_code, 404)

        args = dict(
            base_args,
            id=str(self.format_trackids["flac"]),
            variant="flac_no_picture",
        )
        with patch(
            "supysonic.api.media._get_or_create_flac_no_picture",
            side_effect=OSError("boom"),
        ):
            with closing(
                self.client.get("/rest/stream.view", query_string=args)
            ) as rv:
                self.assertEqual(rv.status_code, 500)

    def test_download(self):
        self._make_request("download", error=10)
        self._make_request("download", {"id": "string"}, error=0)
        self._make_request("download", {"id": str(uuid.uuid4())}, error=70)

        # download single file
        with closing(
            self.client.get(
                "/rest/download.view",
                query_string={
                    "u": "alice",
                    "p": "Alic3",
                    "c": "tests",
                    "id": str(self.trackid),
                },
            )
        ) as rv:
            self.assertEqual(rv.status_code, 200)
            self.assertEqual(len(rv.data), 23)
        self.assertEqual(Track[self.trackid].play_count, 0)

        # dowload folder
        rv = self.client.get(
            "/rest/download.view",
            query_string={
                "u": "alice",
                "p": "Alic3",
                "c": "tests",
                "id": str(self.folderid),
            },
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.mimetype, "application/zip")

    def __assert_image_data(self, resp, format, size):
        with Image.open(BytesIO(resp.data)) as im:
            self.assertEqual(im.format, format)
            self.assertEqual(im.size, (size, size))

    def test_get_cover_art(self):
        self._make_request("getCoverArt", error=10)
        self._make_request("getCoverArt", {"id": "string"}, error=0)
        self._make_request("getCoverArt", {"id": str(uuid.uuid4())}, error=70)
        self._make_request("getCoverArt", {"id": str(self.trackid)}, error=70)
        self._make_request(
            "getCoverArt", {"id": str(self.folderid), "size": "large"}, error=0
        )

        args = {"u": "alice", "p": "Alic3", "c": "tests", "id": str(self.folderid)}
        with closing(
            self.client.get("/rest/getCoverArt.view", query_string=args)
        ) as rv:
            self.assertEqual(rv.status_code, 200)
            self.assertEqual(rv.mimetype, "image/jpeg")
            self.__assert_image_data(rv, "JPEG", 420)

        args["size"] = 600
        with closing(
            self.client.get("/rest/getCoverArt.view", query_string=args)
        ) as rv:
            self.assertEqual(rv.status_code, 200)
            self.assertEqual(rv.mimetype, "image/jpeg")
            self.__assert_image_data(rv, "JPEG", 420)

        args["size"] = 120
        with closing(
            self.client.get("/rest/getCoverArt.view", query_string=args)
        ) as rv:
            self.assertEqual(rv.status_code, 200)
            self.assertEqual(rv.mimetype, "image/jpeg")
            self.__assert_image_data(rv, "JPEG", 120)

        # rerequest, just in case
        with closing(
            self.client.get("/rest/getCoverArt.view", query_string=args)
        ) as rv:
            self.assertEqual(rv.status_code, 200)
            self.assertEqual(rv.mimetype, "image/jpeg")
            self.__assert_image_data(rv, "JPEG", 120)

        # TODO test non square covers

        # Test extracting cover art from embeded media
        for args["id"] in self.formats:
            with closing(
                self.client.get("/rest/getCoverArt.view", query_string=args)
            ) as rv:
                self.assertEqual(rv.status_code, 200)
                self.assertEqual(rv.mimetype, "image/png")
                self.__assert_image_data(rv, "PNG", 120)

    def test_get_avatar(self):
        self._make_request("getAvatar", error=0)

    def test_get_cover_art_does_not_print_debug_output(self):
        args = {"u": "alice", "p": "Alic3", "c": "tests", "id": str(self.folderid)}

        with patch("builtins.print") as print_mock, closing(
            self.client.get("/rest/getCoverArt.view", query_string=args)
        ) as rv:
            self.assertEqual(rv.status_code, 200)

        print_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
