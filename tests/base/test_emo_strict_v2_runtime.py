import unittest

from supysonic.emo.strict_v2_runtime import (
    RequestFingerprintConflict,
    StrictRequestCache,
    request_fingerprint,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class StrictV2RuntimeTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.cache = StrictRequestCache(time_fn=self.clock)
        self.fingerprint = request_fingerprint(
            "command",
            "player.play",
            {"playbackContextId": "context-1", "baseControlVersion": 1},
        )

    def test_reserves_then_replays_a_deep_copy_of_the_result(self):
        first = self.cache.lookup_or_reserve("nonce-1", "request-1", self.fingerprint)
        self.assertEqual(first.status, "new")

        in_flight = self.cache.lookup_or_reserve("nonce-1", "request-1", self.fingerprint)
        self.assertEqual(in_flight.status, "in_flight")

        result = {"messages": [{"action": "system.ack"}]}
        self.cache.store_result("nonce-1", "request-1", self.fingerprint, result)
        result["messages"][0]["action"] = "changed"

        replay = self.cache.lookup_or_reserve("nonce-1", "request-1", self.fingerprint)
        self.assertEqual(replay.status, "cached")
        self.assertEqual(replay.result["messages"][0]["action"], "system.ack")

    def test_same_request_id_with_different_content_conflicts(self):
        self.cache.lookup_or_reserve("nonce-1", "request-1", self.fingerprint)
        conflicting = request_fingerprint(
            "command",
            "player.pause",
            {"playbackContextId": "context-1", "baseControlVersion": 1},
        )

        with self.assertRaises(RequestFingerprintConflict):
            self.cache.lookup_or_reserve("nonce-1", "request-1", conflicting)

    def test_entries_expire_after_sixty_seconds(self):
        self.cache.lookup_or_reserve("nonce-1", "request-1", self.fingerprint)
        self.cache.store_result("nonce-1", "request-1", self.fingerprint, {"ok": True})

        self.clock.now = 59.999
        self.assertEqual(
            self.cache.lookup_or_reserve("nonce-1", "request-1", self.fingerprint).status,
            "cached",
        )
        self.clock.now = 60.0
        self.assertEqual(
            self.cache.lookup_or_reserve("nonce-1", "request-1", self.fingerprint).status,
            "new",
        )

    def test_disconnect_cleanup_is_scoped_to_connection_nonce(self):
        self.cache.lookup_or_reserve("nonce-1", "request-1", self.fingerprint)
        self.cache.lookup_or_reserve("nonce-2", "request-1", self.fingerprint)

        self.cache.clear_connection("nonce-1")

        self.assertEqual(self.cache.size(), 1)
        self.assertEqual(
            self.cache.lookup_or_reserve("nonce-1", "request-1", self.fingerprint).status,
            "new",
        )
        self.assertEqual(
            self.cache.lookup_or_reserve("nonce-2", "request-1", self.fingerprint).status,
            "in_flight",
        )

    def test_failed_request_can_release_its_reservation(self):
        self.cache.lookup_or_reserve("nonce-1", "request-1", self.fingerprint)

        self.cache.release_reservation("nonce-1", "request-1", self.fingerprint)

        self.assertEqual(
            self.cache.lookup_or_reserve("nonce-1", "request-1", self.fingerprint).status,
            "new",
        )

    def test_ttl_shorter_than_contract_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least 60"):
            StrictRequestCache(ttl_seconds=59)


if __name__ == "__main__":
    unittest.main()
