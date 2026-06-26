# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2017-2022 Alban 'spl0k' Féron
#
# Distributed under terms of the GNU AGPLv3 license.

import os.path
import unittest
from xml.etree import ElementTree

import flask.json
import requests

from supysonic.db import Folder, Artist, Album, Track

from ..api.apitestbase import ApiTestBase


class LyricsTestCase(ApiTestBase):
    def setUp(self):
        super().setUp()

        self.config.WEBAPP["online_lyrics"] = True

        folder = Folder.create(
            name="Root",
            path=os.path.abspath("tests/assets/lyrics"),
            root=True,
        )

        artist = Artist.create(name="Artist")
        album = Album.create(artist=artist, name="Album")

        Track.create(
            title="Nope",
            number=1,
            disc=1,
            artist=artist,
            album=album,
            path=os.path.abspath("tests/assets/lyrics/empty.mp3"),
            root_folder=folder,
            folder=folder,
            duration=2,
            bitrate=320,
            last_modification=0,
        )
        Track.create(
            title="Yay",
            number=1,
            disc=1,
            artist=artist,
            album=album,
            path=os.path.abspath("tests/assets/lyrics/withlyrics.mp3"),
            root_folder=folder,
            folder=folder,
            duration=2,
            bitrate=320,
            last_modification=0,
        )

    def _skip_if_chartlyrics_unavailable(self):
        try:
            r = requests.get(
                "http://api.chartlyrics.com/apiv1.asmx/SearchLyricDirect",
                params={"artist": "The Clash", "song": "London Calling"},
                timeout=5,
            )
            root = ElementTree.fromstring(r.content)
        except (requests.exceptions.RequestException, ElementTree.ParseError) as e:
            self.skipTest("ChartLyrics down: " + e.__class__.__name__)

        ns = {"cl": "http://api.chartlyrics.com/"}
        lyric = root.find("cl:Lyric", namespaces=ns)
        if lyric is None or lyric.text is None or "live by the river" not in lyric.text:
            self.skipTest("ChartLyrics did not return expected lyrics")

    def test_get_lyrics(self):
        self._make_request("getLyrics", error=10)
        self._make_request("getLyrics", {"artist": "artist"}, error=10)
        self._make_request("getLyrics", {"title": "title"}, error=10)

        # Local file
        rv, child = self._make_request(
            "getLyrics", {"artist": "artist", "title": "nope"}, tag="lyrics"
        )
        self.assertIn("text file", child.text)

        # Metadata
        rv, child = self._make_request(
            "getLyrics", {"artist": "artist", "title": "yay"}, tag="lyrics"
        )
        self.assertIn("Some words", child.text)

    def test_get_online_lyrics(self):
        # Potentially skip the tests if ChartLyrics is down (which happens quite often)
        self._skip_if_chartlyrics_unavailable()

        rv, child = self._make_request(
            "getLyrics",
            {
                "artist": "some really long name hoping",
                "title": "to get absolutely no result",
            },
            tag="lyrics",
        )
        self.assertIsNone(child.text)

        # ChartLyrics
        rv, child = self._make_request(
            "getLyrics",
            {"artist": "The Clash", "title": "London Calling"},
            tag="lyrics",
        )
        self.assertIn("live by the river", child.text)
        # ChartLyrics, JSON format
        args = {
            "u": "alice",
            "p": "Alic3",
            "c": "tests",
            "f": "json",
            "artist": "The Clash",
            "title": "London Calling",
        }
        rv = self.client.get("/rest/getLyrics.view", query_string=args)
        json = flask.json.loads(rv.data)
        self.assertIn("value", json["subsonic-response"]["lyrics"])
        self.assertIn("live by the river", json["subsonic-response"]["lyrics"]["value"])


if __name__ == "__main__":
    unittest.main()
