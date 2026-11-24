"""Strict source freshness across real Socket.IO handlers and SQLite transactions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
import unittest
from unittest import mock

from supysonic import db
from supysonic.emo import ws
from tests.base import test_emo_strict_v2_broadcast as broadcast_tests
from tests.base import test_emo_strict_v2_follow as follow_tests


@contextmanager
def slow_handshakes(start):
    # Charge the observed 103 ms per physical DB connection to the server clock.
    # SQL and transaction logic still run on SQLite; the test needs no real sleep.
    with mock.patch.object(db.db.obj, "_connect", wraps=db.db.obj._connect) as opened, mock.patch.object(
        ws, "_server_time_ms", side_effect=lambda: start + 103 * opened.call_count
    ):
        yield opened


class SourceFactLatencyTestCase(unittest.TestCase):
    def harness(self, cls):
        harness = cls()
        harness.setUp()
        self.addCleanup(harness.tearDown)
        return harness

    def follow_devices(self, sample_age=0):
        h = self.harness(follow_tests.StrictV2FollowTestCase)
        caps = {"playbackContextV2": True, "effectiveAtPlayback": True}
        source = h.connect_device("alice", "Alic3", "owner-1", "device:owner-1", ["player"], capabilities=caps)
        follower = h.connect_device("alice", "Alic3", "follower-1", "device:follower-1", ["player"],
                                    capabilities=dict(caps, supportsFollow=True))
        now = ws._server_time_ms()
        h.prepare_follow_pair(source, follower, source_state="playing", source_sampled_at_server_ms=now - sample_age)
        return h, source, follower, now

    def test_follower_receives_ordered_facts_with_one_handshake_per_update(self):
        h, source, follower, now = self.follow_devices()
        h.get_ack(h.start_follow(follower), "follow-start-1")
        for seq in (2, 3):
            sample = now + 1000 * seq
            with slow_handshakes(sample) as opened:
                source.emit("message", {
                    "type": "event", "action": "playback.update", "requestId": "progress-%d" % seq,
                    "payload": {
                        "playbackContextId": "context-source-1", "deviceSessionId": "device:owner-1",
                        "origin": "passive", "state": "playing", "positionMs": 1000 * seq,
                        "trackId": "song-owner-1", "positionSampledAtServerMs": sample,
                        "playbackRate": 1.0, "clientSeq": seq, "appliedControlVersion": 1,
                    }
                }, namespace="/emo")
            for client in (source, follower):
                fact, = [msg["payload"] for msg in h.get_messages(client) if msg["action"] == "playback.update"]
                self.assertEqual((fact["clientSeq"], fact["positionMs"]), (seq, 1000 * seq))
                self.assertLessEqual(fact["serverUpdatedAtMs"] - sample, 103)
            self.assertEqual(opened.call_count, 1)

    def test_progress_during_follow_admission_is_delivered_after_subscription(self):
        h, source, follower, _ = self.follow_devices()
        created, release, updating = threading.Event(), threading.Event(), threading.Event()
        create_lease = ws.createFollowSafetyLease
        handle_update = ws._handle_strict_v2_playback_update

        def wait_before_subscribing(*args, **kwargs):
            result = create_lease(*args, **kwargs)
            created.set()
            if not release.wait(3):
                raise RuntimeError("follow admission barrier timed out")
            return result

        def progress(*args, **kwargs):
            updating.set()
            return handle_update(*args, **kwargs)

        with mock.patch.object(ws, "createFollowSafetyLease", wait_before_subscribing), mock.patch.object(
            ws, "_handle_strict_v2_playback_update", progress
        ), ThreadPoolExecutor(max_workers=2) as workers:
            admission = workers.submit(h.start_follow, follower)
            try:
                self.assertTrue(created.wait(3))
                update = workers.submit(h.report_strict_playback_context, source, "progress", "context-source-1",
                                        "device:owner-1", state="playing", position_ms=300, client_seq=2)
                self.assertTrue(updating.wait(3))
                self.assertFalse(update.done())
            finally:
                release.set()
            first = admission.result(timeout=3)
            update.result(timeout=3)
        h.get_ack(first, "follow-start-1")
        fact, = [m["payload"] for m in first + h.get_messages(follower) if m["action"] == "playback.update"]
        self.assertEqual((fact["clientSeq"], fact["positionMs"]), (2, 300))

    def test_follow_admits_fresh_source_when_database_handshakes_are_slow(self):
        h, _, follower, now = self.follow_devices(sample_age=1450)
        with slow_handshakes(now):
            messages = h.start_follow(follower)
        self.assertEqual(h.get_ack(messages, "follow-start-1")["payload"]["status"], "active")

    def test_broadcast_admits_fresh_source_when_database_handshakes_are_slow(self):
        h = self.harness(broadcast_tests.StrictV2BroadcastTestCase)
        source, _, controller = h.connect_broadcast_devices()
        now = ws._server_time_ms()
        h.report_source_state(source, sampled_at_ms=now - 1450, client_seq=2)
        with slow_handshakes(now):
            messages = h.start_strict_broadcast(controller, participants=["participant-1"])
        self.assertIsNotNone(h.get_ack(messages, "broadcast-start-1"))
