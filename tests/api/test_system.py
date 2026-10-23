# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2017 Alban 'spl0k' Féron
#               2017 Óscar García Amor
#
# Distributed under terms of the GNU AGPLv3 license.

import base64
import unittest

from supysonic.db import Meta, close_connection, open_connection
from supysonic.server_identity import SERVER_IDENTITY_KEY, get_server_identity

from .apitestbase import ApiTestBase


class SystemTestCase(ApiTestBase):
    def test_ping(self):
        self._make_request("ping")

    def test_identity_is_persistent_and_shared_by_addresses(self):
        def ping(host):
            response = self.client.get(
                "/rest/ping.view",
                base_url=host,
                query_string={
                    "u": "alice", "p": "Alic3", "c": "tests", "f": "json",
                    "includeServerIdentity": "true",
                },
            )
            return response.get_json()["subsonic-response"]["serverIdentity"]

        first = ping("https://lan.example")
        second = ping("https://relay.example")
        self.assertEqual(first, second)
        self.assertEqual(first["version"], 1)
        self.assertEqual(len(base64.b64decode(first["id"], validate=True)), 32)
        self.assertTrue(first["accountId"])
        self.assertEqual(Meta.get_by_id(SERVER_IDENTITY_KEY).value, first["id"])
        self.assertEqual(get_server_identity(), first["id"])
        close_connection()
        open_connection()
        self.assertEqual(get_server_identity(), first["id"])

    def test_identity_distinguishes_accounts_on_the_same_server(self):
        def ping(username, password):
            response = self.client.get("/rest/ping.view", query_string={
                "u": username, "p": password, "c": "tests", "f": "json",
                "includeServerIdentity": "true",
            })
            return response.get_json()["subsonic-response"]["serverIdentity"]

        alice, bob = ping("alice", "Alic3"), ping("bob", "B0b")
        self.assertEqual(alice["id"], bob["id"])
        self.assertNotEqual(alice["accountId"], bob["accountId"])

    def test_identity_requires_authentication(self):
        response = self.client.get(
            "/rest/ping.view",
            query_string={
                "u": "alice", "p": "wrong", "c": "tests", "f": "json",
                "includeServerIdentity": "true",
            },
        )
        payload = response.get_json()["subsonic-response"]
        self.assertEqual(payload["status"], "failed")
        self.assertNotIn("serverIdentity", payload)

    def test_corrupt_identity_is_not_silently_replaced(self):
        identity, _ = Meta.get_or_create(
            key=SERVER_IDENTITY_KEY, defaults={"value": "broken"}
        )
        identity.value = "broken"
        identity.save()
        with self.assertRaises(RuntimeError):
            get_server_identity()
        self.assertEqual(Meta.get_by_id(SERVER_IDENTITY_KEY).value, "broken")

    def test_get_license(self):
        rv, child = self._make_request("getLicense", tag="license")
        self.assertEqual(child.get("valid"), "true")

    def test_open_subsonic_extensions_excludes_unsupported_transcode_offset(self):
        rv = self.client.get(
            "/rest/getOpenSubsonicExtensions.view",
            query_string={"u": "alice", "p": "Alic3", "c": "tests", "f": "json"},
        )

        data = rv.get_json()
        extensions = data["subsonic-response"]["openSubsonicExtensions"]
        self.assertNotIn(
            "transcodeOffset",
            {extension["name"] for extension in extensions},
        )

    def test_open_subsonic_extensions_includes_music_request_board(self):
        rv = self.client.get(
            "/rest/getOpenSubsonicExtensions.view",
            query_string={"u": "alice", "p": "Alic3", "c": "tests", "f": "json"},
        )

        data = rv.get_json()
        extensions = data["subsonic-response"]["openSubsonicExtensions"]
        by_name = {extension["name"]: extension for extension in extensions}
        self.assertIn("musicRequestBoard", by_name)
        self.assertEqual(by_name["musicRequestBoard"]["versions"], [1])


if __name__ == "__main__":
    unittest.main()
