"""Real SQLite regressions for a retired client returning in a new session."""

import json
import os
import tempfile
import unittest

from supysonic import db
from supysonic.emo import ws_store
from tests.base import test_emo_ws_store as store_tests


class HandoffReturnGenerationTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp()
        os.close(handle)
        db.init_database("sqlite:///" + self.db_path)
        self.helper = store_tests.EmoWebSocketStoreTestCase()
        self.context_id = "return-context"
        self.first = self.helper._commit_exact_handoff_source(
            self.context_id, "outbound",
        )
        self.helper._complete_exact_handoff(
            self.context_id, "outbound", "alice",
            self.first["targetClientId"],
            self.first["targetDeviceSessionId"],
        )
        self.target_client_id = self.first["sourceClientId"]
        self.old_target_device = self.first["sourceDeviceSessionId"]
        self.target_device = "restarted-target-device"
        ws_store.ensureStrictPlaybackContextState(
            "alice", self.target_client_id, self.target_device,
            [], None, 0, "idle",
        )
        ws_store.applyStrictPlaybackUpdate(
            self.context_id, "alice", self.first["targetClientId"],
            self.first["targetDeviceSessionId"],
            self.first["targetConnectionNonce"],
            {
                "playbackContextId": self.context_id,
                "deviceSessionId": self.first["targetDeviceSessionId"],
                "origin": "passive", "appliedControlVersion": 2,
                "state": "playing", "trackId": "song-1",
                "positionMs": 1000, "positionSampledAtServerMs": 2000,
                "playbackRate": 0.8, "clientSeq": 2,
            },
            2000,
        )
        ws_store.createStrictPlaybackHandoff(
            self.context_id,
            {
                "handoffId": "return", "requestId": "return-request",
                "playbackContextId": self.context_id, "userName": "alice",
                "sourceClientId": self.first["targetClientId"],
                "sourceDeviceSessionId": self.first["targetDeviceSessionId"],
                "sourceConnectionNonce": self.first["targetConnectionNonce"],
                "sourceConnectionEpoch": self.first["targetConnectionEpoch"],
                "targetClientId": self.target_client_id,
                "targetDeviceSessionId": self.target_device,
                "targetConnectionNonce": "restarted-target-nonce",
                "targetConnectionEpoch": 1,
                "originClientId": self.first["targetClientId"],
                "status": "preparing", "baseControlVersion": 2,
                "controlVersion": 3, "prepareId": "return-prepare",
                "snapshot": {},
            },
            self.target_device,
        )
        ws_store.commitStrictPlaybackHandoff(
            self.context_id, "return", "alice", 9000, 2100, 180000,
        )
        ws_store.markStrictPlaybackHandoffCommitEnqueued(
            self.context_id, "return", "alice",
        )

    def tearDown(self):
        db.release_database()
        os.remove(self.db_path)

    def complete(self, **overrides):
        handoff = ws_store.getPlaybackHandoff("return")
        snapshot = handoff["snapshot"]
        kwargs = {
            "position_ms": snapshot["positionMs"],
            "queue_index": snapshot["currentIndex"],
            "track_id": snapshot["trackId"], "state": "playing",
            "position_sampled_at_server_ms": snapshot["effectiveAtServerMs"],
            "playback_rate": snapshot["playbackRate"],
            "applied_control_version": handoff["controlVersion"],
            "client_seq": 1,
            "server_received_at_ms": snapshot["effectiveAtServerMs"],
            "expected_source_client_id": handoff["sourceClientId"],
            "expected_source_device_session_id": handoff["sourceDeviceSessionId"],
            "expected_source_connection_nonce": handoff["sourceConnectionNonce"],
            "expected_source_connection_epoch": handoff["sourceConnectionEpoch"],
            "expected_target_connection_nonce": handoff["targetConnectionNonce"],
            "expected_target_connection_epoch": handoff["targetConnectionEpoch"],
        }
        target_device = overrides.pop("target_device", self.target_device)
        kwargs.update(overrides)
        return ws_store.completeStrictPlaybackHandoff(
            self.context_id, "return", "alice", self.target_client_id,
            target_device, **kwargs,
        )

    def old_target_row(self):
        db.open_connection(reuse=True)
        return db.EmoDevicePlaybackState.get(
            (db.EmoDevicePlaybackState.playback_context_id == self.context_id)
            & (db.EmoDevicePlaybackState.owner_client_id == self.target_client_id)
        )

    def test_return_replaces_only_retired_target_generation(self):
        before = self.old_target_row()
        self.assertEqual(before.device_session_id, self.old_target_device)
        self.assertEqual(before.context_epoch, 1)
        self.assertEqual(before.is_authority, 0)
        result = self.complete()
        self.assertTrue(result.mutated)
        self.assertEqual(result[0]["authorityClientId"], self.target_client_id)
        self.assertEqual(result[0]["authorityDeviceSessionId"], self.target_device)
        self.assertEqual(result[0]["epoch"], 3)
        row = self.old_target_row()
        self.assertEqual(row.id, before.id)
        self.assertEqual(row.device_session_id, self.target_device)
        self.assertEqual(row.is_authority, 1)
        self.assertEqual(row.context_epoch, 3)
        self.assertEqual(row.applied_control_version, 3)
        self.assertEqual(json.loads(row.playback_json)["_connectionNonce"],
                         "restarted-target-nonce")

    def test_return_exact_replay_is_noop_but_changed_proof_rejected(self):
        self.assertTrue(self.complete().mutated)
        self.assertFalse(self.complete().mutated)
        with self.assertRaises(ws_store.PlaybackClientSequenceConflictError):
            self.complete(client_seq=2)
        with self.assertRaises(ws_store.PlaybackHandoffTargetConflictError):
            self.complete(target_device=self.old_target_device)

    def test_return_keeps_all_frozen_generation_checks(self):
        cases = (
            {"expected_source_client_id": "old-source"},
            {"expected_source_device_session_id": "old-source-device"},
            {"expected_source_connection_nonce": "old-source-nonce"},
            {"expected_source_connection_epoch": 2},
            {"target_device": self.old_target_device},
            {"expected_target_connection_nonce": "old-target-nonce"},
            {"expected_target_connection_epoch": 2},
        )
        before = ws_store.getPlaybackContextState(self.context_id)
        for override in cases:
            with self.subTest(override=next(iter(override))):
                expected_error = (
                    ValueError if next(iter(override)).endswith("_epoch")
                    else ws_store.PlaybackHandoffTargetConflictError
                )
                with self.assertRaises(expected_error):
                    self.complete(**override)
                self.assertEqual(ws_store.getPlaybackContextState(self.context_id), before)
                self.assertEqual(ws_store.getPlaybackHandoff("return")["status"], "committed")

    def test_return_does_not_replace_current_future_authoritative_or_foreign_row(self):
        cases = (
            {"context_epoch": 2}, {"context_epoch": 3},
            {"is_authority": 1}, {"user_name": "bob"},
        )
        before = ws_store.getPlaybackContextState(self.context_id)
        for overrides in cases:
            with self.subTest(overrides=overrides):
                row = self.old_target_row()
                row.context_epoch, row.is_authority, row.user_name = 1, 0, "alice"
                for name, value in overrides.items():
                    setattr(row, name, value)
                row.save()
                db.close_connection()
                with self.assertRaises(ws_store.PlaybackHandoffTargetConflictError):
                    self.complete()
                self.assertEqual(ws_store.getPlaybackContextState(self.context_id), before)
                self.assertEqual(ws_store.getPlaybackHandoff("return")["status"], "committed")

    def test_retired_device_feedback_stays_fenced_after_return(self):
        self.complete()
        with self.assertRaises(PermissionError):
            ws_store.applyStrictPlaybackUpdate(
                self.context_id, "alice", self.target_client_id,
                self.old_target_device, self.first["sourceConnectionNonce"],
                {
                    "playbackContextId": self.context_id,
                    "deviceSessionId": self.old_target_device,
                    "origin": "passive", "appliedControlVersion": 3,
                    "state": "playing", "trackId": "song-1",
                    "positionMs": 1100, "positionSampledAtServerMs": 2200,
                    "playbackRate": 0.8, "clientSeq": 3,
                },
                2200,
            )
