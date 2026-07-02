# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2017-2018 Alban 'spl0k' Féron
# Copyright (C) 2024 Iván Ávalos
#
# Distributed under terms of the GNU AGPLv3 license.

import logging
import unittest
from unittest.mock import Mock, patch

from supysonic.listenbrainz import ListenBrainz

from ..frontend.frontendtestbase import FrontendTestBase


class ListenBrainzTestCase(unittest.TestCase):
    """Basic test of unauthenticated ListenBrainz API method"""

    def test_request(self):
        logging.getLogger("supysonic.listenbrainz").addHandler(logging.NullHandler())
        listenbrainz = ListenBrainz({"api_url": "https://api.listenbrainz.org/"}, None)
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"users": [{"user_name": "aavalos"}]}

        user = "aavalos"
        with patch("supysonic.listenbrainz.requests.get", return_value=response) as get:
            rv = listenbrainz._ListenBrainz__api_request(
                False, "/1/search/users/?search_term={0}".format(user), token="123"
            )

        self.assertIsInstance(rv, dict)
        self.assertEqual(rv["users"][0]["user_name"], user)
        get.assert_called_once()


class FrontendListenBrainzCase(FrontendTestBase):
    def test_listenbrainz_link(self):
        self._login("alice", "Alic3")
        rv = self.client.get("/user/me/listenbrainz/link", follow_redirects=True)
        self.assertIn("Missing ListenBrainz auth token", rv.data)
        with patch.object(
            ListenBrainz,
            "link_account",
            return_value=(False, "Error connecting to ListenBrainz"),
        ):
            rv = self.client.get(
                "/user/me/listenbrainz/link",
                query_string={"token": "abcdef"},
                follow_redirects=True,
            )
        self.assertIn("Error connecting to ListenBrainz", rv.data)

        with patch.object(
            ListenBrainz,
            "link_account",
            return_value=(False, "Error: invalid token"),
        ):
            rv = self.client.get(
                "/user/me/listenbrainz/link",
                query_string={"token": "abcdef"},
                follow_redirects=True,
            )
        self.assertIn("Error: ", rv.data)


if __name__ == "__main__":
    unittest.main()
