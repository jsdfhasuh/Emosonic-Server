import unittest
from unittest.mock import Mock

import requests

from supysonic.album_discovery_catalog import CatalogArtist
from supysonic.album_discovery_oracle import OracleAlbumClient, OracleSubscriptionUnavailable


class OracleArtistSubscriptionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.session = Mock(spec=requests.Session)
        self.client = OracleAlbumClient("https://oracle.invalid", "control", "delivery", session=self.session)
        self.artist = CatalogArtist("123", "Artist")
        self.hit = {"artistId": "123", "artistName": "Artist", "storefront": "cn",
                    "artistUrl": "https://music.apple.com/cn/artist/artist/123"}
        self.row = {**self.hit, "id": 1, "enabled": True, "newAlbumPolicy": "confirm"}

    @staticmethod
    def response(payload: object, status: int = 200) -> Mock:
        return Mock(status_code=status, json=Mock(return_value=payload))

    def test_search_verifies_cn_identity_deduplicates_and_uses_control_token(self) -> None:
        self.session.request.return_value = self.response({"results": [self.hit, self.hit]})
        self.assertEqual(self.client.search_subscription_artists("Artist"), [self.artist])
        call = self.session.request.call_args
        self.assertEqual(call.args, ("POST", "https://oracle.invalid/api/subscriptions/search"))
        self.assertEqual(call.kwargs["json"], {"term": "Artist"})
        self.assertEqual(call.kwargs["headers"], {"Authorization": "Bearer control"})
        self.assertFalse(call.kwargs["allow_redirects"])
        for bad in ({"storefront": "us"}, {"artistName": ""}, {"artistId": "124"},
                    {"artistUrl": "https://other.invalid/cn/artist/artist/123"},
                    {"artistUrl": "https://music.apple.com/cn/album/artist/123"}):
            with self.subTest(bad=bad):
                self.session.request.return_value = self.response({"results": [{**self.hit, **bad}]})
                with self.assertRaises(OracleSubscriptionUnavailable):
                    self.client.search_subscription_artists("Artist")

    def test_active_and_disabled_preexisting_subscriptions_are_read_only(self) -> None:
        for enabled, expected in ((False, "artist_subscription_disabled"), (True, "artist_subscription_existing")):
            self.session.request.reset_mock()
            self.session.request.return_value = self.response([{**self.row, "enabled": enabled}])
            self.assertEqual(self.client.ensure_artist_subscription(self.artist), expected)
            self.assertEqual(self.session.request.call_count, 1)
            self.assertEqual(self.session.request.call_args.args[0], "GET")

    def test_creates_with_only_cn_url_and_confirms_with_independent_readback(self) -> None:
        self.session.request.side_effect = [
            self.response([]), self.response({"subscription": self.row, "created": True}, 201),
            self.response([self.row]),
        ]
        self.assertEqual(self.client.ensure_artist_subscription(self.artist), "artist_subscription_created")
        read, post, readback = self.session.request.call_args_list
        self.assertEqual((read.args[0], post.args[0], readback.args[0]), ("GET", "POST", "GET"))
        self.assertEqual(post.kwargs["json"], {"artistUrl": "https://music.apple.com/cn/artist/-/123"})
        self.assertEqual({call.args[1] for call in (read, post, readback)},
                         {"https://oracle.invalid/api/subscriptions"})

    def test_timeout_reconciles_before_retry_and_never_claims_unseen_creation(self) -> None:
        self.session.request.side_effect = [
            self.response([]), requests.Timeout(), self.response([self.row]), self.response([self.row]),
        ]
        for _ in range(2):
            self.assertEqual(self.client.ensure_artist_subscription(self.artist), "artist_subscription_existing")
        self.assertEqual([call.args[0] for call in self.session.request.call_args_list],
                         ["GET", "POST", "GET", "GET"])
        self.session.request.side_effect = [self.response([]), requests.Timeout(), self.response([])]
        with self.assertRaises(OracleSubscriptionUnavailable):
            self.client.ensure_artist_subscription(self.artist)

    def test_malformed_list_wrong_id_wrong_policy_or_disappeared_entry_cannot_pass(self) -> None:
        for broken in ([{}], [{**self.row, "enabled": "false"}], [self.row, self.row],
                       [{**self.row, "artistUrl": "https://music.apple.com/hk/artist/artist/123"}]):
            self.session.request.reset_mock()
            self.session.request.return_value = self.response(broken)
            with self.subTest(broken=broken), self.assertRaises(OracleSubscriptionUnavailable):
                self.client.ensure_artist_subscription(self.artist)
            self.assertEqual(self.session.request.call_count, 1)
        receipt = {"created": True, "subscription": self.row}
        for claimed, readback in (
            (receipt, [{**self.row, "newAlbumPolicy": "auto"}]),
            ({"created": True, "subscription": {**self.row, "artistName": "Another"}}, [self.row]),
            (receipt, []),
        ):
            self.session.request.side_effect = [self.response([]), self.response(claimed, 201), self.response(readback)]
            with self.subTest(claimed=claimed, readback=readback), self.assertRaises(OracleSubscriptionUnavailable):
                self.client.ensure_artist_subscription(self.artist)
