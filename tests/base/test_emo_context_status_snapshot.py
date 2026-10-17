"""Coherent strict status from actual SQLite mutations and Socket.IO handlers."""

import concurrent.futures
import threading
import unittest
from unittest import mock

from flask import g

from supysonic import db
from supysonic.emo import ws as emo_ws
from supysonic.emo import ws_store
from supysonic.emo.strict_v2_contract import StrictOutputValidationError, validate_strict_output
from tests.base import test_emo_ws as ws_tests


class ContextStatusSnapshotTestCase(unittest.TestCase):
    context_id = "status-snapshot-context"

    def setUp(self):
        self.harness = ws_tests.EmoWebSocketTestCase()
        self.harness.setUp()
        self.phone = self.harness.connect_device(
            "alice", "Alic3", "phone", "device:phone", ["player"],
            capabilities={"playbackContextV2": True},
        )
        self.observer = self.harness.connect_device(
            "alice", "Alic3", "observer", "device:observer", ["controller"],
            capabilities={"playbackContextV2": True},
        )
        self.harness.ensure_playback_context(
            self.phone, "ensure", playback_context_id=self.context_id,
            device_session_id="device:phone", queue_song_ids=["song-1"],
            state="paused", position_ms=100,
        )
        self.harness.report_strict_playback_context(
            self.phone, "fact", self.context_id, "device:phone",
            client_seq=1, position_ms=100,
        )
        self.harness.get_messages(self.phone)
        self.harness.get_messages(self.observer)

    def tearDown(self):
        self.harness.clients = [
            client for client in self.harness.clients
            if client.is_connected(namespace="/emo")
        ]
        self.harness.tearDown()

    def advance(self):
        self.phone.emit("message", {
            "type": "event", "action": "playback.update", "requestId": "advance",
            "payload": {
                "playbackContextId": self.context_id,
                "deviceSessionId": "device:phone", "origin": "localUser",
                "executionStatus": "committed", "intentId": "seek-intent",
                "epoch": 1, "observedControlVersion": 1, "queueIndex": 0,
                "trackId": "song-1", "state": "paused", "positionMs": 200,
                "positionSampledAtServerMs": emo_ws._server_time_ms(),
                "playbackRate": 1.0, "clientSeq": 2,
            },
        }, namespace="/emo")
        messages = self.harness.get_messages(self.phone)
        self.assertFalse(any(m["action"] == "system.error" for m in messages))
        self.assertEqual(ws_store.getPlaybackContextState(self.context_id)["controlVersion"], 2)

    def status(self, client=None, request_id="status"):
        client = client or self.observer
        client.emit("message", {
            "type": "state", "action": "playback.context.status",
            "requestId": request_id,
            "payload": {"playbackContextId": self.context_id},
        }, namespace="/emo")
        return self.harness.get_messages(client)

    def assert_snapshot(self, message, expected_version):
        validate_strict_output(message)
        payload = message["payload"]
        self.assertEqual(payload["playbackContext"]["controlVersion"], expected_version)
        for device in payload["deviceStates"]:
            self.assertLessEqual(device["appliedControlVersion"], expected_version)
        return payload

    def test_status_reloads_coherent_snapshot_after_captured_context_races_update(self):
        captured, release = threading.Event(), threading.Event()
        original = emo_ws._build_playback_context_status_payload

        def delayed(context):
            if getattr(g, "emo_request_action", None) == "playback.context.status":
                self.assertEqual(context["controlVersion"], 1)
                captured.set()
                if not release.wait(3):
                    raise RuntimeError("Status scheduling barrier expired")
            return original(context)

        with mock.patch.object(emo_ws, "_build_playback_context_status_payload", delayed):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self.status)
                try:
                    self.assertTrue(captured.wait(3))
                    self.advance()
                finally:
                    release.set()
                messages = future.result(timeout=3)
        self.assertFalse(any(m["action"] == "system.error" for m in messages))
        response = self.harness.get_direct_response(messages, "playback.context.status", "status")
        payload = self.assert_snapshot(response, 2)
        self.assertEqual(payload["deviceStates"][0]["appliedControlVersion"], 2)
        self.assertEqual(payload["deviceStates"][0]["positionMs"], 200)

    def test_push_and_subscribed_status_refresh_stale_context(self):
        stale = ws_store.getPlaybackContextState(self.context_id)
        self.observer.emit("message", {
            "type": "state", "action": "playback.context.subscribe",
            "requestId": "subscribe", "payload": {"playbackContextId": self.context_id},
        }, namespace="/emo")
        self.harness.get_ack(self.harness.get_messages(self.observer), "subscribe")
        self.advance()
        self.harness.get_messages(self.observer)
        sid = emo_ws.state.get_sid_for_client("observer", user_name="alice")
        with self.harness.app.app_context():
            emo_ws._push_playback_context_snapshot(sid, stale)
        pushed = next(m for m in self.harness.get_messages(self.observer)
                      if m["action"] == "playback.context.status")
        self.assert_snapshot(pushed, 2)
        response = self.harness.get_direct_response(self.status(), "playback.context.status", "status")
        self.assert_snapshot(response, 2)

    def test_durable_snapshot_is_one_joined_select_and_serializes_strict_writer(self):
        reading, release = threading.Event(), threading.Event()
        writer_started, writer_done = threading.Event(), threading.Event()
        original_sql = db.db.execute_sql
        selects = []
        reader_thread = []

        def observed_sql(sql, *args, **kwargs):
            if "LEFT OUTER JOIN" in sql and threading.get_ident() in reader_thread:
                selects.append(sql)
                reading.set()
                if not release.wait(3):
                    raise RuntimeError("Joined snapshot barrier expired")
            return original_sql(sql, *args, **kwargs)

        def writer():
            writer_started.set()
            self.advance()
            writer_done.set()

        def reader():
            reader_thread.append(threading.get_ident())
            return ws_store.getPlaybackContextWithDeviceStates(self.context_id, "alice")

        with mock.patch.object(db.db.obj, "execute_sql", side_effect=observed_sql):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                read = executor.submit(reader)
                try:
                    self.assertTrue(reading.wait(3))
                    write = executor.submit(writer)
                    self.assertTrue(writer_started.wait(3))
                    self.assertFalse(writer_done.wait(0.1))
                finally:
                    release.set()
                snapshot = read.result(timeout=3)
                write.result(timeout=3)
        self.assertEqual(len(selects), 1)
        self.assertEqual(snapshot["playbackContext"]["controlVersion"], 1)
        self.assertEqual(snapshot["deviceStates"][0]["appliedControlVersion"], 1)
        self.assertEqual(ws_store.getPlaybackContextState(self.context_id)["controlVersion"], 2)

    def test_joined_snapshot_keeps_context_without_device_rows(self):
        context = ws_store.ensureStrictPlaybackContextState(
            "alice", "standby", "device:standby",
            [], None, 0, "idle",
        )[0]
        snapshot = ws_store.getPlaybackContextWithDeviceStates(
            context["playbackContextId"], "alice",
        )
        self.assertEqual(snapshot["playbackContext"]["state"], "idle")
        self.assertEqual(snapshot["deviceStates"], [])
        self.assertIsNone(ws_store.getPlaybackContextWithDeviceStates(
            context["playbackContextId"], "bob",
        ))

    def runtime_fact(self, client_id="runtime-player", device="device:runtime", **overrides):
        generation = emo_ws.state.get_current_physical_generation("alice", client_id, device)
        payload = {
            "contextEpoch": 1, "appliedControlVersion": 1, "clientSeq": 2,
            "state": "paused", "trackId": "song-1", "positionMs": 700,
            "positionSampledAtServerMs": emo_ws._server_time_ms(),
            "playbackRate": 1.0, "volume": 65, "muted": True,
            "_connectionNonce": generation["connectionNonce"],
            "_connectionEpoch": generation["connectionEpoch"],
        }
        payload.update(overrides)
        return emo_ws.state.record_device_playback_state(
            self.context_id, device, client_id, "alice", payload,
        )

    def test_current_scoped_runtime_only_fact_is_preserved(self):
        self.harness.connect_device(
            "alice", "Alic3", "runtime-player", "device:runtime", ["player"],
            capabilities={"playbackContextV2": True},
        )
        self.runtime_fact()
        self.assertIsNone(ws_store.getDevicePlaybackState(self.context_id, "runtime-player"))
        response = self.harness.get_direct_response(self.status(), "playback.context.status", "status")
        payload = self.assert_snapshot(response, 1)
        runtime = next(d for d in payload["deviceStates"] if d["clientId"] == "runtime-player")
        self.assertEqual(runtime["positionMs"], 700)
        self.assertEqual(runtime["volume"], 65)
        self.assertTrue(runtime["muted"])
        self.assertNotIn("_connectionNonce", runtime)

    def test_wrong_epoch_user_or_unproven_runtime_fact_does_not_cross_scope(self):
        self.harness.connect_device(
            "alice", "Alic3", "runtime-player", "device:runtime", ["player"],
            capabilities={"playbackContextV2": True},
        )
        for index, override in enumerate((
            {"contextEpoch": 2}, {"userName": "bob"},
            {"contextEpoch": True}, {"_connectionEpoch": True},
            {"_connectionNonce": "retired-nonce"}, {"_connectionNonce": None},
            {"_connectionEpoch": None},
        )):
            with self.subTest(override=override):
                self.runtime_fact(**override)
                request_id = "status-%d" % index
                response = self.harness.get_direct_response(
                    self.status(request_id=request_id), "playback.context.status", request_id,
                )
                payload = self.assert_snapshot(response, 1)
                self.assertEqual([d["clientId"] for d in payload["deviceStates"]], ["phone"])

    def test_old_runtime_nonce_high_sequence_cannot_override_reconnected_durable_fact(self):
        old = emo_ws.state.get_current_physical_generation("alice", "phone", "device:phone")
        replacement = self.harness.connect_device(
            "alice", "Alic3", "phone", "device:phone", ["player"],
            capabilities={"playbackContextV2": True},
        )
        self.harness.report_strict_playback_context(
            replacement, "new-generation-fact", self.context_id, "device:phone",
            position_ms=300, client_seq=1,
        )
        self.runtime_fact("phone", "device:phone", clientSeq=99,
                          _connectionNonce=old["connectionNonce"])
        response = self.harness.get_direct_response(self.status(), "playback.context.status", "status")
        payload = self.assert_snapshot(response, 1)
        self.assertEqual(payload["deviceStates"][0]["clientSeq"], 1)
        self.assertEqual(payload["deviceStates"][0]["positionMs"], 300)

    def test_current_runtime_low_sequence_replaces_retired_durable_nonce(self):
        self.harness.report_strict_playback_context(
            self.phone, "old-high-sequence", self.context_id, "device:phone",
            position_ms=111, client_seq=99,
        )
        self.harness.connect_device(
            "alice", "Alic3", "phone", "device:phone", ["player"],
            capabilities={"playbackContextV2": True},
        )
        self.runtime_fact("phone", "device:phone", clientSeq=1)
        response = self.harness.get_direct_response(self.status(), "playback.context.status", "status")
        payload = self.assert_snapshot(response, 1)
        self.assertEqual(payload["deviceStates"][0]["clientSeq"], 1)
        self.assertEqual(payload["deviceStates"][0]["positionMs"], 700)

    def test_in_scope_newer_applied_fact_is_not_clamped_or_hidden(self):
        self.harness.connect_device(
            "alice", "Alic3", "runtime-player", "device:runtime", ["player"],
            capabilities={"playbackContextV2": True},
        )
        self.runtime_fact(appliedControlVersion=2)
        payload = emo_ws._build_playback_context_status_payload(
            ws_store.getPlaybackContextState(self.context_id),
        )
        runtime = next(d for d in payload["deviceStates"] if d["clientId"] == "runtime-player")
        self.assertEqual(runtime["appliedControlVersion"], 2)
        with self.assertRaisesRegex(
            StrictOutputValidationError, "appliedControlVersion exceeds Context",
        ):
            validate_strict_output({
                "type": "state", "action": "playback.context.status",
                "timestamp": 0, "payload": payload,
                "connectionNonce": "snapshot-test-nonce", "connectionEpoch": 1,
            })

    def test_lifecycle_and_owner_are_rechecked_from_durable_context(self):
        stale = ws_store.getPlaybackContextState(self.context_id)
        ws_store.closeStrictPlaybackContextState(
            self.context_id, "alice", expected_epoch=1, base_version=1,
            requesting_client_id="phone", requesting_device_session_id="device:phone",
        )
        with self.assertRaises(ws_store.PlaybackContextClosedError):
            emo_ws._build_playback_context_status_payload(stale)
        with self.assertRaises(LookupError):
            emo_ws._build_playback_context_status_payload(dict(stale, userName="bob"))
