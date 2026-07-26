import concurrent.futures
import os
import tempfile
import threading
import unittest
from unittest import mock

from supysonic import db
from supysonic.emo.broadcast_store import (
    BROADCAST_FEEDBACK_WINDOW_MS,
    MAX_CONTEXT_BROADCAST_INTENTS,
    MAX_USER_RECOVERY_SLOTS,
    MIN_RETAINED_BROADCAST_REVISIONS,
    BroadcastIntentConflictError,
    BroadcastLimitError,
    BroadcastResourceConflictError,
    commitBroadcastRevision,
    compactExpiredBroadcastStates,
    confirmBroadcastRestore,
    createBroadcastState,
    getBroadcastFenceForContext,
    getBroadcastFenceForPair,
    getBroadcastIntentOutcome,
    getBroadcastState,
    listTerminalRecoveries,
    saveBroadcastFeedbackSettlement,
    terminalBroadcastState,
)


class EmoBroadcastStoreTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp()
        os.close(handle)
        db.init_database("sqlite:///" + self.db_path)

    def tearDown(self):
        db.release_database()
        os.remove(self.db_path)

    def _snapshot(self, broadcast_id="broadcast-1", intent_id="intent-1",
                  context_id="context-source", revision=1,
                  lifecycle="active", updated_at_ms=10000):
        return {
            "broadcastId": broadcast_id, "userName": "alice",
            "playbackContextId": context_id, "intentId": intent_id,
            "ownerClientId": "controller-1", "authorityClientId": "source-1",
            "authorityDeviceSessionId": "device:source-1",
            "lifecycleState": lifecycle, "broadcastRevision": revision,
            "queueSongIds": ["song-1", "song-2"], "currentIndex": 0,
            "trackId": "song-1", "positionMs": 1200, "state": "playing",
            "playbackRate": 1.0, "sourceVersion": 2,
            "sourceQueueRevision": 2, "sourceControlVersion": 2,
            "sourceEpoch": 1, "serverUpdatedAtMs": updated_at_ms,
            "participants": ["participant-1"],
        }

    def _participant(self, client_id="participant-1",
                     device_id="device:participant-1",
                     context_id="context-participant-1"):
        return {
            "clientId": client_id, "deviceSessionId": device_id,
            "suspendedPlaybackContextId": context_id, "suspendedEpoch": 3,
            "suspendedVersion": 7, "suspendedQueueRevision": 5,
            "suspendedControlVersion": 6,
            "suspendedAppliedControlVersion": 6,
        }

    def _delivery(self, delivery_id="delivery-1", revision=1,
                  action="start", created_at_ms=10000):
        return {
            "deliveryId": delivery_id, "clientId": "participant-1",
            "deviceSessionId": "device:participant-1", "action": action,
            "effectiveAtServerMs": created_at_ms + 300,
            "serverTimeMs": created_at_ms, "deliveryPositionMs": 1500,
            "feedbackDeadlineAtServerMs": created_at_ms + 8300,
            "payload": {"broadcastId": "broadcast-1",
                        "broadcastRevision": revision,
                        "deliveryId": delivery_id},
            "connectionNonce": "nonce-1", "createdAtMs": created_at_ms,
        }

    def _create(self):
        return createBroadcastState(
            self._snapshot(), [self._participant()], "fingerprint-1",
            {"broadcastId": "broadcast-1", "accepted": True},
            initial_deliveries=[self._delivery()],
        )

    def test_create_read_and_intent_replay(self):
        self.assertTrue(self._create()["created"])
        state = getBroadcastState("broadcast-1")
        self.assertEqual(state["snapshot"]["trackId"], "song-1")
        self.assertEqual(state["participantStates"][0]["targetDeliveryId"],
                         "delivery-1")
        self.assertEqual((len(state["revisions"]), len(state["deliveries"])),
                         (1, 1))
        self.assertEqual(
            getBroadcastFenceForContext("alice", "context-source")["role"],
            "source",
        )
        self.assertEqual(
            getBroadcastFenceForPair(
                "alice", "participant-1", "device:participant-1"
            )["role"],
            "ordinary",
        )
        replay = self._create()
        self.assertFalse(replay["created"])
        self.assertEqual(replay["intentOutcome"]["startAck"]["broadcastId"],
                         "broadcast-1")
        with self.assertRaises(BroadcastIntentConflictError):
            createBroadcastState(self._snapshot(), [self._participant()],
                                 "different", {"accepted": True})

    def test_concurrent_start_has_one_winner(self):
        barrier = threading.Barrier(2)

        def start(index):
            barrier.wait()
            try:
                createBroadcastState(
                    self._snapshot("broadcast-%d" % index,
                                   "intent-%d" % index),
                    [self._participant("participant-%d" % index,
                                       "device:participant-%d" % index,
                                       "context-participant-%d" % index)],
                    "fingerprint-%d" % index,
                    {"broadcastId": "broadcast-%d" % index},
                )
                return "created"
            except BroadcastResourceConflictError:
                return "conflict"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(start, (1, 2)))
        self.assertEqual(sorted(results), ["conflict", "created"])
        self.assertEqual(db.EmoBroadcast.select().count(), 1)

    def test_start_and_terminal_failures_leave_no_partial_rows(self):
        with mock.patch.object(db.EmoBroadcastDelivery, "create",
                               side_effect=RuntimeError("start failure")):
            with self.assertRaises(RuntimeError):
                self._create()
        for model in (db.EmoBroadcast, db.EmoBroadcastIntentOutcome,
                      db.EmoBroadcastFence, db.EmoBroadcastParticipant,
                      db.EmoBroadcastRevision, db.EmoBroadcastDelivery):
            self.assertEqual(model.select().count(), 0, model.__name__)

        self._create()
        with mock.patch.object(db.EmoBroadcastDelivery, "create",
                               side_effect=RuntimeError("terminal failure")):
            with self.assertRaises(RuntimeError):
                terminalBroadcastState(
                    "broadcast-1", self._snapshot(revision=2,
                                                   lifecycle="stopped"),
                    {"stopped": True},
                    [self._delivery("terminal-delivery", 2, "stop")],
                )
        state = getBroadcastState("broadcast-1")
        self.assertEqual((state["lifecycleState"], state["broadcastRevision"]),
                         ("active", 1))
        self.assertFalse(state["participantStates"][0]["restorePending"])
        self.assertEqual(db.EmoBroadcastFence.select().count(), 4)

    def test_revision_terminal_restore_and_compaction(self):
        self._create()
        commitBroadcastRevision(
            "broadcast-1", 1, self._snapshot(revision=2, updated_at_ms=20000),
            "progress", [self._delivery("delivery-2", 2, "progress", 20000)],
        )
        terminal = self._snapshot(revision=3, lifecycle="stopped",
                                  updated_at_ms=30000)
        first = terminalBroadcastState(
            "broadcast-1", terminal, {"stopped": True},
            [self._delivery("terminal-delivery", 3, "stop", 30000)],
            expected_broadcast_revision=2, terminal_at_ms=30000,
        )
        self.assertTrue(first["created"])
        replay = terminalBroadcastState("broadcast-1", terminal,
                                        {"ignored": True})
        self.assertFalse(replay["created"])
        self.assertEqual(db.EmoBroadcastRevision.select().count(), 3)
        self.assertEqual(db.EmoBroadcastFence.select()
                         .where(db.EmoBroadcastFence.role == "source").count(), 0)
        self.assertEqual({x.phase for x in db.EmoBroadcastFence.select()},
                         {"restorePending"})

        compactExpiredBroadcastStates(now_ms=9999999999999)
        self.assertIsNone(getBroadcastState("broadcast-1"))
        self.assertIsNotNone(getBroadcastIntentOutcome(
            "alice", "context-source", "controller-1", "intent-1"))
        recovery = listTerminalRecoveries(
            "alice", "participant-1", "device:participant-1")
        self.assertEqual(recovery[0]["terminalBroadcastRevision"], 3)
        self.assertTrue(confirmBroadcastRestore(
            "broadcast-1", "alice", "participant-1",
            "device:participant-1", 3, "terminal-delivery"))
        self.assertEqual(listTerminalRecoveries("alice"), [])
        self.assertEqual(db.EmoBroadcastFence.select().count(), 0)

    def test_feedback_settlement_is_idempotent(self):
        args = ("context-source", "broadcast-1", "participant-1",
                "device:participant-1", "nonce-1", 1, 1,
                "feedback-fingerprint", {"status": "applied"})
        self.assertTrue(saveBroadcastFeedbackSettlement(*args)["created"])
        replay = saveBroadcastFeedbackSettlement(*args)
        self.assertFalse(replay["created"])
        with self.assertRaises(BroadcastResourceConflictError):
            saveBroadcastFeedbackSettlement(*args[:7], "different",
                                            {"status": "failed"})

    def test_20_and_256_limits(self):
        participants = [self._participant(
            "participant-%d" % i, "device:participant-%d" % i,
            "context-participant-%d" % i) for i in range(21)]
        with self.assertRaises(BroadcastLimitError) as conflict:
            createBroadcastState(self._snapshot(), participants, "fp", {})
        self.assertEqual(conflict.exception.limit, 20)
        for index in range(MAX_USER_RECOVERY_SLOTS):
            db.EmoBroadcastFence.create(
                resource_key="reserved:%d" % index,
                broadcast_id="old:%d" % index, user_name="alice",
                role="ordinary", phase="restorePending",
                recovery_slot_reserved=1,
            )
        with self.assertRaises(BroadcastLimitError) as conflict:
            self._create()
        self.assertEqual(conflict.exception.limit, MAX_USER_RECOVERY_SLOTS)

    def test_exactly_twenty_participants_are_accepted(self):
        participants = [self._participant(
            "participant-%d" % i, "device:participant-%d" % i,
            "context-participant-%d" % i) for i in range(20)]
        snapshot = self._snapshot()
        snapshot["participants"] = sorted(
            item["clientId"] for item in participants)
        result = createBroadcastState(
            snapshot, participants, "fingerprint-20", {"accepted": True})
        self.assertTrue(result["created"])
        self.assertEqual(
            db.EmoBroadcastParticipant.select().count(),
            20,
        )

    def test_start_atomically_skips_participants_without_recovery_slots(self):
        for index in range(MAX_USER_RECOVERY_SLOTS - 1):
            db.EmoBroadcastFence.create(
                resource_key="reserved:%d" % index,
                broadcast_id="old:%d" % index,
                user_name="alice",
                role="ordinary",
                phase="restorePending",
                recovery_slot_reserved=1,
            )
        participants = [
            self._participant(
                "participant-%d" % index,
                "device:participant-%d" % index,
                "context-participant-%d" % index,
            )
            for index in range(2)
        ]
        snapshot = self._snapshot()
        snapshot["participants"] = [item["clientId"] for item in participants]
        start_ack = {
            "participants": list(snapshot["participants"]),
            "skippedClientIds": [],
        }
        result = createBroadcastState(
            snapshot,
            participants,
            "slot-fingerprint",
            start_ack,
            skip_unavailable_participants=True,
        )
        self.assertTrue(result["created"])
        self.assertEqual(
            result["intentOutcome"]["startAck"]["participants"],
            ["participant-0"],
        )
        self.assertEqual(
            result["intentOutcome"]["startAck"]["skippedClientIds"],
            ["participant-1"],
        )
        self.assertEqual(
            result["broadcast"]["snapshot"]["participants"],
            ["participant-0"],
        )

    def test_1024_intent_limit_replays_old_first(self):
        for index in range(MAX_CONTEXT_BROADCAST_INTENTS):
            db.EmoBroadcastIntentOutcome.create(
                user_name="alice", playback_context_id="context-source",
                owner_client_id="controller-1",
                intent_id="old-intent-%d" % index,
                request_fingerprint="old-fingerprint-%d" % index,
                broadcast_id="old-broadcast-%d" % index,
                final_participants_json="[]", skipped_client_ids_json="[]",
                start_ack_json='{"old":true}',
            )
        replay = createBroadcastState(
            self._snapshot("old-broadcast-0", "old-intent-0"), [],
            "old-fingerprint-0", {})
        self.assertFalse(replay["created"])
        with self.assertRaises(BroadcastLimitError) as conflict:
            self._create()
        self.assertEqual(conflict.exception.limit,
                         MAX_CONTEXT_BROADCAST_INTENTS)

    def test_512_revision_floor_and_feedback_window(self):
        self._create()
        for revision in range(2, MIN_RETAINED_BROADCAST_REVISIONS + 3):
            db.EmoBroadcastRevision.create(
                broadcast_id="broadcast-1", broadcast_revision=revision,
                snapshot_json="{}", canonical_action="progress",
                created_at_ms=1)
        record = db.EmoBroadcast.get(
            db.EmoBroadcast.broadcast_id == "broadcast-1")
        record.broadcast_revision = MIN_RETAINED_BROADCAST_REVISIONS + 2
        record.save()
        next_revision = MIN_RETAINED_BROADCAST_REVISIONS + 3
        commitBroadcastRevision(
            "broadcast-1", next_revision - 1,
            self._snapshot(revision=next_revision,
                           updated_at_ms=BROADCAST_FEEDBACK_WINDOW_MS + 10000),
            "progress", created_at_ms=BROADCAST_FEEDBACK_WINDOW_MS + 10000)
        revisions = {x.broadcast_revision for x in
                     db.EmoBroadcastRevision.select().where(
                         db.EmoBroadcastRevision.broadcast_id == "broadcast-1")}
        self.assertGreaterEqual(len(revisions),
                                MIN_RETAINED_BROADCAST_REVISIONS)
        self.assertNotIn(2, revisions)
        self.assertNotIn(3, revisions)


if __name__ == "__main__":
    unittest.main()
