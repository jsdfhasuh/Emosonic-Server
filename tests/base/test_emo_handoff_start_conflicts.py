"""Strict Socket.IO Handoff-start errors retain their authorized Context scope."""

import time
import unittest

from supysonic.emo import ws as emo_ws
from supysonic.emo.strict_v2_contract import validate_strict_output
from supysonic.emo.ws_store import getPlaybackContextState
from tests.base import test_emo_strict_v2_handoff as handoff_tests


class HandoffStartConflictEnvelopeTestCase(unittest.TestCase):
    def setUp(self):
        self.harness = handoff_tests.StrictV2HandoffTestCase()
        self.harness.setUp()
        self.source, self.target, self.controller = (
            self.harness.connect_handoff_devices()
        )

    def tearDown(self):
        self.harness.tearDown()

    def assert_context_conflict(self, **start_options):
        messages = self.harness.start_handoff(self.controller, **start_options)
        message = self.harness.get_error(messages, "handoff-start-1")
        validate_strict_output(message)
        payload = message["payload"]
        self.assertEqual(payload["action"], "playback.handoff.start")
        self.assertEqual(payload["code"], "conflict")
        context = getPlaybackContextState("context-handoff-1")
        for wire_key, context_key in (
            ("playbackContextId", "playbackContextId"),
            ("currentEpoch", "epoch"),
            ("currentVersion", "version"),
            ("currentQueueRevision", "queueRevision"),
            ("currentControlVersion", "controlVersion"),
        ):
            self.assertEqual(payload[wire_key], context[context_key])
        return payload

    def clear_clock_gate(self, client_id):
        state = emo_ws.state
        sid = state.get_sid_for_client(client_id, user_name="alice")
        with state._lock:
            state._sessions[sid]["clockPingCount"] = 0

    def test_stale_source_returns_correlated_complete_context_cursors(self):
        time.sleep(2.1)
        payload = self.assert_context_conflict()
        self.assertIn("both be fresh", payload["message"])

    def test_source_clock_conflict_contains_complete_context_cursors(self):
        self.clear_clock_gate("source-1")
        payload = self.assert_context_conflict()
        self.assertIn("clock gate", payload["message"])

    def test_target_clock_conflict_contains_complete_context_cursors(self):
        self.clear_clock_gate("target-1")
        payload = self.assert_context_conflict()
        self.assertIn("clock gate", payload["message"])

    def test_control_baseline_conflict_reports_canonical_not_requested_cursors(self):
        payload = self.assert_context_conflict(base_control_version=99)
        self.assertEqual(payload["currentControlVersion"], 1)

    def test_request_fingerprint_conflict_remains_unscoped(self):
        self.assert_context_conflict(base_control_version=99)
        messages = self.harness.start_handoff(
            self.controller, base_control_version=98,
        )
        message = self.harness.get_error(messages, "handoff-start-1")
        validate_strict_output(message)
        self.assertEqual(message["payload"]["code"], "conflict")
        for key in (
            "playbackContextId", "currentEpoch", "currentVersion",
            "currentQueueRevision", "currentControlVersion",
        ):
            self.assertNotIn(key, message["payload"])

    def test_foreign_context_rejection_does_not_disclose_cursors(self):
        outsider = self.harness.connect_device(
            "bob", "B0b", "foreign-controller", "foreign-device",
            ["controller"], capabilities={"playbackContextV2": True},
        )
        messages = self.harness.start_handoff(outsider)
        message = self.harness.get_error(messages, "handoff-start-1")
        validate_strict_output(message)
        self.assertIn(message["payload"]["code"], {"forbidden", "not_found"})
        for key in (
            "playbackContextId", "currentEpoch", "currentVersion",
            "currentQueueRevision", "currentControlVersion",
        ):
            self.assertNotIn(key, message["payload"])
