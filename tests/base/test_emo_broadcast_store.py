import concurrent.futures
import json
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
    BroadcastFeedbackSequenceConflictError,
    BroadcastLimitError,
    BroadcastResourceConflictError,
    buildTerminalBroadcastSnapshot,
    commitBroadcastRevision,
    compactExpiredBroadcastStates,
    createBroadcastRegistrationReplay,
    createBroadcastState,
    getBroadcastFenceForContext,
    getBroadcastFenceForPair,
    getBroadcastIntentOutcome,
    getBroadcastState,
    listTerminalRecoveries,
    saveBroadcastFeedbackSettlement,
    settleBroadcastFeedback,
    stopNonterminalBroadcastsForRestart,
    suspendBroadcastForAuthorityDisconnect,
    sweepBroadcastAuthorityDisconnectDeadlines,
    sweepBroadcastFeedbackDeadlines,
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
                        "deliveryId": delivery_id,
                        "currentIndex": 0,
                        "trackId": "song-1",
                        "state": "playing",
                        "positionMs": 1500,
                        "playbackRate": 1.0},
            "connectionNonce": "nonce-1", "createdAtMs": created_at_ms,
        }

    def _create(self):
        return createBroadcastState(
            self._snapshot(), [self._participant()], "fingerprint-1",
            {"broadcastId": "broadcast-1", "accepted": True},
            initial_deliveries=[self._delivery()],
        )

    def _applied_feedback(self, revision=1, delivery_id="delivery-1",
                          client_seq=1, **extra):
        payload = {
            "playbackContextId": "context-source",
            "broadcastId": "broadcast-1",
            "deviceSessionId": "device:participant-1",
            "deliveryId": delivery_id,
            "executionStatus": "applied",
            "appliedBroadcastRevision": revision,
            "queueIndex": 0,
            "trackId": "song-1",
            "state": "playing",
            "positionMs": 1510,
            "playbackRate": 1.0,
            "clientSeq": client_seq,
        }
        payload.update(extra)
        return payload

    def _settle(self, payload, fingerprint, server_time_ms):
        return settleBroadcastFeedback(
            "alice",
            "context-source",
            "broadcast-1",
            "participant-1",
            "device:participant-1",
            "nonce-1",
            1,
            payload,
            fingerprint,
            server_time_ms,
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

        full_replay = createBroadcastRegistrationReplay(
            "alice", "participant-1", "device:participant-1",
            "nonce-terminal-2", 31000,
        )
        self.assertEqual(full_replay["delivery"]["action"], "stop")
        self.assertEqual(full_replay["delivery"]["broadcastRevision"], 3)
        self.assertNotEqual(
            full_replay["delivery"]["deliveryId"], "terminal-delivery")

        compactExpiredBroadcastStates(now_ms=9999999999999)
        self.assertIsNone(getBroadcastState("broadcast-1"))
        self.assertIsNotNone(getBroadcastIntentOutcome(
            "alice", "context-source", "controller-1", "intent-1"))
        recovery = listTerminalRecoveries(
            "alice", "participant-1", "device:participant-1")
        self.assertEqual(recovery[0]["terminalBroadcastRevision"], 3)
        compact_replay = createBroadcastRegistrationReplay(
            "alice", "participant-1", "device:participant-1",
            "nonce-terminal-3", 32000,
            allow_nonterminal=False,
        )
        self.assertEqual(compact_replay["delivery"]["action"], "restore")
        self.assertEqual(
            compact_replay["delivery"]["payload"][
                "terminalBroadcastRevision"
            ],
            3,
        )
        self.assertEqual(
            compact_replay["delivery"]["payload"]["state"], "stopped")
        stale_feedback = self._applied_feedback(
            revision=3,
            delivery_id="stale-compact-delivery",
            state="stopped",
            restoreCompleted=True,
        )
        rejected = settleBroadcastFeedback(
            "alice", "context-source", "broadcast-1",
            "participant-1", "device:participant-1",
            "nonce-terminal-3", 1, stale_feedback,
            "compact-rejected", 32500,
        )
        self.assertEqual(
            rejected["canonicalResult"]["errorCode"],
            "revision_unknown",
        )
        self.assertEqual(rejected["followUpDelivery"]["action"], "restore")
        replacement_delivery_id = rejected["followUpDelivery"]["deliveryId"]
        compact_feedback = self._applied_feedback(
            revision=3,
            delivery_id=replacement_delivery_id,
            client_seq=2,
            state="stopped",
            restoreCompleted=True,
        )
        settled = settleBroadcastFeedback(
            "alice", "context-source", "broadcast-1",
            "participant-1", "device:participant-1",
            "nonce-terminal-3", 1, compact_feedback,
            "compact-feedback", 33000,
        )
        self.assertEqual(settled["action"], "broadcast.feedback")
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

    def test_applied_feedback_is_atomic_and_client_seq_idempotent(self):
        self._create()
        payload = self._applied_feedback()

        first = self._settle(payload, "feedback-1", 11000)
        replay = self._settle(payload, "feedback-1", 12000)

        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(
            first["canonicalResult"],
            replay["canonicalResult"],
        )
        participant = getBroadcastState("broadcast-1")[
            "participantStates"
        ][0]
        self.assertEqual(participant["syncStatus"], "applied")
        self.assertEqual(participant["appliedBroadcastRevision"], 1)
        self.assertEqual(participant["appliedPositionMs"], 1510)
        self.assertEqual(participant["appliedAtServerMs"], 11000)
        self.assertEqual(participant["lastFeedbackClientSeq"], 1)
        self.assertEqual(participant["lastFeedbackAtMs"], 11000)
        with self.assertRaises(BroadcastFeedbackSequenceConflictError):
            self._settle(payload, "different", 13000)

    def test_feedback_target_mismatch_has_no_side_effects(self):
        self._create()
        variants = (
            {"queueIndex": 1},
            {"trackId": "song-2"},
            {"state": "paused"},
            {"playbackRate": 1.25},
            {"positionMs": 2001},
        )
        for index, changes in enumerate(variants):
            payload = self._applied_feedback(**changes)
            with self.subTest(changes=changes):
                with self.assertRaises((BroadcastResourceConflictError, ValueError)):
                    settleBroadcastFeedback(
                        "alice",
                        "context-source",
                        "broadcast-1",
                        "participant-1",
                        "device:participant-1",
                        "nonce-1",
                        1,
                        payload,
                        "mismatch-%d" % index,
                        11000 + index,
                        track_duration_ms=2000,
                    )
        participant = getBroadcastState("broadcast-1")[
            "participantStates"
        ][0]
        self.assertEqual(participant["syncStatus"], "pending")
        self.assertIsNone(participant["appliedBroadcastRevision"])
        self.assertEqual(db.EmoBroadcastFeedbackSettlement.select().count(), 0)

    def test_feedback_ahead_rejection_creates_one_replacement_delivery(self):
        self._create()
        snapshot_before = getBroadcastState("broadcast-1")["snapshot"]
        payload = self._applied_feedback(
            revision=2,
            delivery_id="unknown-delivery",
        )

        first = self._settle(payload, "ahead-1", 11000)
        replay = self._settle(payload, "ahead-1", 12000)

        self.assertTrue(first["created"])
        self.assertEqual(first["action"], "broadcast.feedback.rejected")
        self.assertEqual(
            first["canonicalResult"]["errorCode"],
            "revision_ahead",
        )
        self.assertEqual(
            first["canonicalResult"]["currentBroadcastRevision"],
            1,
        )
        self.assertEqual(
            first["canonicalResult"]["minimumRetainedBroadcastRevision"],
            1,
        )
        replacement = first["followUpDelivery"]
        self.assertEqual(replacement["action"], "resync")
        self.assertEqual(replacement["broadcastRevision"], 1)
        self.assertEqual(replacement["effectiveAtServerMs"], 11250)
        self.assertEqual(
            replacement["payload"]["serverUpdatedAtMs"],
            10000,
        )
        self.assertEqual(
            replacement["payload"]["positionMs"],
            1200,
        )
        self.assertFalse(replay["created"])
        self.assertEqual(
            replay["canonicalResult"],
            first["canonicalResult"],
        )
        self.assertEqual(
            replay["followUpDelivery"]["deliveryId"],
            replacement["deliveryId"],
        )
        with self.assertRaises(BroadcastFeedbackSequenceConflictError):
            self._settle(
                dict(payload, deliveryId="different-unknown-delivery"),
                "ahead-conflict",
                12500,
            )
        state = getBroadcastState("broadcast-1")
        current = [
            item for item in state["deliveries"] if item["isCurrent"]
        ]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["deliveryId"], replacement["deliveryId"])
        self.assertEqual(state["snapshot"], snapshot_before)
        self.assertEqual(
            state["participantStates"][0]["feedbackDeadlineAtServerMs"],
            19250,
        )

        stale = self._settle(
            self._applied_feedback(client_seq=2),
            "superseded-delivery",
            13000,
        )
        stale_replay = self._settle(
            self._applied_feedback(client_seq=2),
            "superseded-delivery",
            14000,
        )
        self.assertTrue(stale["created"])
        self.assertEqual(stale["action"], "broadcast.feedback.rejected")
        self.assertEqual(
            stale["canonicalResult"]["errorCode"],
            "revision_unknown",
        )
        self.assertFalse(stale_replay["created"])
        self.assertEqual(
            stale_replay["followUpDelivery"]["deliveryId"],
            stale["followUpDelivery"]["deliveryId"],
        )
        state = getBroadcastState("broadcast-1")
        current = [item for item in state["deliveries"] if item["isCurrent"]]
        self.assertEqual(len(current), 1)
        self.assertEqual(
            current[0]["deliveryId"],
            stale["followUpDelivery"]["deliveryId"],
        )
        self.assertEqual(state["snapshot"], snapshot_before)

    def test_feedback_unknown_revision_classification(self):
        self._create()
        db.EmoBroadcastDelivery.delete().where(
            db.EmoBroadcastDelivery.delivery_id == "delivery-1"
        ).execute()
        unknown = self._settle(
            self._applied_feedback(delivery_id="missing-delivery"),
            "unknown-1",
            11000,
        )
        self.assertEqual(
            unknown["canonicalResult"]["errorCode"],
            "revision_unknown",
        )

    def test_feedback_expired_revision_classification(self):
        self._create()
        commitBroadcastRevision(
            "broadcast-1",
            1,
            self._snapshot(revision=2, updated_at_ms=12000),
            "progress",
            [self._delivery("delivery-2", 2, "progress", 12000)],
        )
        db.EmoBroadcastDelivery.delete().where(
            db.EmoBroadcastDelivery.broadcast_revision == 1
        ).execute()
        db.EmoBroadcastRevision.delete().where(
            db.EmoBroadcastRevision.broadcast_revision == 1
        ).execute()
        expired = self._settle(
            self._applied_feedback(delivery_id="expired-delivery"),
            "expired-1",
            13000,
        )
        self.assertEqual(
            expired["canonicalResult"]["errorCode"],
            "revision_expired",
        )
        self.assertEqual(
            expired["canonicalResult"]["minimumRetainedBroadcastRevision"],
            2,
        )

    def test_registration_replay_uses_new_nonce_and_waiting_is_untimed(self):
        self._create()
        same_connection = createBroadcastRegistrationReplay(
            "alice",
            "participant-1",
            "device:participant-1",
            "nonce-1",
            11000,
        )
        self.assertFalse(same_connection["created"])

        active = createBroadcastRegistrationReplay(
            "alice",
            "participant-1",
            "device:participant-1",
            "nonce-2",
            12000,
        )
        self.assertTrue(active["created"])
        self.assertEqual(active["delivery"]["action"], "resync")
        self.assertEqual(active["delivery"]["effectiveAtServerMs"], 12250)
        self.assertEqual(active["delivery"]["connectionNonce"], "nonce-2")

        for index, state_name in enumerate(("paused", "stopped"), 3):
            active_snapshot = self._snapshot(updated_at_ms=12000 + index)
            active_snapshot["state"] = state_name
            db.EmoBroadcast.update(
                snapshot_json=json.dumps(active_snapshot, sort_keys=True),
            ).where(
                db.EmoBroadcast.broadcast_id == "broadcast-1"
            ).execute()
            replay = createBroadcastRegistrationReplay(
                "alice",
                "participant-1",
                "device:participant-1",
                "nonce-%d" % index,
                12000 + index,
            )
            self.assertEqual(
                replay["delivery"]["effectiveAtServerMs"],
                12250 + index,
            )
            self.assertEqual(
                replay["delivery"]["payload"]["state"],
                state_name,
            )

        waiting_snapshot = self._snapshot(
            lifecycle="waitingForSource",
            updated_at_ms=13000,
        )
        db.EmoBroadcast.update(
            lifecycle_state="waitingForSource",
            snapshot_json=json.dumps(waiting_snapshot, sort_keys=True),
        ).where(
            db.EmoBroadcast.broadcast_id == "broadcast-1"
        ).execute()
        waiting = createBroadcastRegistrationReplay(
            "alice",
            "participant-1",
            "device:participant-1",
            "nonce-5",
            14000,
        )
        self.assertTrue(waiting["created"])
        self.assertIsNone(waiting["delivery"]["effectiveAtServerMs"])
        self.assertIsNone(waiting["delivery"]["serverTimeMs"])
        self.assertNotIn(
            "effectiveAtServerMs",
            waiting["delivery"]["payload"],
        )
        participant = getBroadcastState("broadcast-1")[
            "participantStates"
        ][0]
        self.assertEqual(participant["feedbackDeadlineAtServerMs"], 22000)

    def test_terminal_rejection_replaces_stop_without_clearing_restore(self):
        self._create()
        terminal = self._snapshot(
            revision=2,
            lifecycle="stopped",
            updated_at_ms=20000,
        )
        terminalBroadcastState(
            "broadcast-1",
            terminal,
            {"stopped": True},
            [self._delivery("terminal-delivery", 2, "stop", 20000)],
            expected_broadcast_revision=1,
            terminal_at_ms=20000,
        )
        db.EmoBroadcastDelivery.delete().where(
            db.EmoBroadcastDelivery.delivery_id == "terminal-delivery"
        ).execute()
        feedback = self._applied_feedback(
            revision=2,
            delivery_id="missing-terminal-delivery",
            state="stopped",
            restoreCompleted=True,
        )

        rejected = self._settle(feedback, "terminal-unknown", 21000)

        self.assertEqual(
            rejected["canonicalResult"]["errorCode"],
            "revision_unknown",
        )
        replacement = rejected["followUpDelivery"]
        self.assertEqual(replacement["action"], "stop")
        self.assertEqual(replacement["broadcastRevision"], 2)
        self.assertNotIn("effectiveAtServerMs", replacement["payload"])
        participant = getBroadcastState("broadcast-1")[
            "participantStates"
        ][0]
        self.assertTrue(participant["restorePending"])
        self.assertFalse(participant["terminalConfirmed"])
        self.assertEqual(
            participant["targetDeliveryId"],
            replacement["deliveryId"],
        )

    def test_source_disconnect_waiting_and_timeout_share_terminal_state(self):
        self._create()
        before = getBroadcastState("broadcast-1")

        waiting = suspendBroadcastForAuthorityDisconnect(
            "alice",
            "source-1",
            "device:source-1",
            12000,
            42000,
        )

        self.assertEqual(waiting["action"], "broadcast.waiting")
        self.assertEqual(waiting["snapshot"]["broadcastRevision"], 2)
        self.assertEqual(
            waiting["snapshot"]["lifecycleState"],
            "waitingForSource",
        )
        self.assertEqual(waiting["snapshot"]["state"], "paused")
        for field_name in (
            "sourceEpoch",
            "sourceVersion",
            "sourceQueueRevision",
            "sourceControlVersion",
        ):
            self.assertEqual(
                waiting["snapshot"][field_name],
                before["snapshot"][field_name],
            )
        self.assertEqual(
            getBroadcastState("broadcast-1")[
                "authorityDisconnectDeadlineMs"
            ],
            42000,
        )
        self.assertEqual(
            sweepBroadcastAuthorityDisconnectDeadlines(41999),
            [],
        )

        terminal = sweepBroadcastAuthorityDisconnectDeadlines(42000)

        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["action"], "broadcast.stop")
        self.assertEqual(terminal[0]["snapshot"]["broadcastRevision"], 3)
        persisted = getBroadcastState("broadcast-1")
        self.assertEqual(persisted["lifecycleState"], "stopped")
        self.assertIsNone(persisted["authorityDisconnectDeadlineMs"])
        self.assertTrue(persisted["participantStates"][0]["restorePending"])
        self.assertEqual(
            sweepBroadcastAuthorityDisconnectDeadlines(43000),
            [],
        )

    def test_restart_terminals_waiting_broadcast_once(self):
        self._create()
        suspendBroadcastForAuthorityDisconnect(
            "alice",
            "source-1",
            "device:source-1",
            12000,
            42000,
        )

        stopped = stopNonterminalBroadcastsForRestart(13000)

        self.assertEqual(stopped, ["broadcast-1"])
        persisted = getBroadcastState("broadcast-1")
        self.assertEqual(persisted["lifecycleState"], "stopped")
        self.assertEqual(persisted["broadcastRevision"], 3)
        self.assertTrue(persisted["participantStates"][0]["restorePending"])
        self.assertEqual(persisted["snapshot"]["positionMs"], 3200)
        self.assertEqual(stopNonterminalBroadcastsForRestart(14000), [])

    def test_terminal_snapshot_projects_only_a_playing_anchor(self):
        playing = buildTerminalBroadcastSnapshot(self._snapshot(), 13000)
        self.assertEqual(playing["positionMs"], 4200)
        self.assertEqual(playing["serverUpdatedAtMs"], 13000)

        for state_name in ("paused", "stopped"):
            with self.subTest(state=state_name):
                previous = dict(self._snapshot(), state=state_name)
                terminal = buildTerminalBroadcastSnapshot(previous, 13000)
                self.assertEqual(terminal["positionMs"], 1200)
                self.assertEqual(terminal["serverUpdatedAtMs"], 13000)

    def test_compaction_failure_preserves_full_terminal_record(self):
        self._create()
        terminalBroadcastState(
            "broadcast-1",
            self._snapshot(revision=2, lifecycle="stopped"),
            {"stopped": True},
            [self._delivery("terminal-delivery", 2, "stop")],
            terminal_at_ms=10000,
        )

        with mock.patch.object(
            db.EmoBroadcastTerminalRecovery,
            "create",
            side_effect=RuntimeError("compact recovery failure"),
        ):
            with self.assertRaises(RuntimeError):
                compactExpiredBroadcastStates(now_ms=9999999999999)

        persisted = getBroadcastState("broadcast-1")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["lifecycleState"], "stopped")
        self.assertTrue(persisted["participantStates"][0]["restorePending"])
        self.assertEqual(listTerminalRecoveries("alice"), [])

    def test_feedback_settlement_failure_rolls_back_participant(self):
        self._create()
        payload = self._applied_feedback()

        with mock.patch.object(
            db.EmoBroadcastFeedbackSettlement,
            "create",
            side_effect=RuntimeError("feedback settlement failure"),
        ):
            with self.assertRaises(RuntimeError):
                self._settle(payload, "feedback-failure", 11000)

        state = getBroadcastState("broadcast-1")
        participant = state["participantStates"][0]
        self.assertEqual(participant["syncStatus"], "pending")
        self.assertIsNone(participant["appliedBroadcastRevision"])
        self.assertEqual(state["deliveries"][0]["deliveryStatus"], "pending")
        self.assertEqual(db.EmoBroadcastFeedbackSettlement.select().count(), 0)

    def test_failed_feedback_can_converge_to_applied(self):
        self._create()
        self._settle(self._applied_feedback(), "applied-1", 11000)
        commitBroadcastRevision(
            "broadcast-1",
            1,
            self._snapshot(revision=2, updated_at_ms=12000),
            "progress",
            [self._delivery("delivery-2", 2, "progress", 12000)],
        )
        failed = {
            "playbackContextId": "context-source",
            "broadcastId": "broadcast-1",
            "deviceSessionId": "device:participant-1",
            "deliveryId": "delivery-2",
            "executionStatus": "failed",
            "failedBroadcastRevision": 2,
            "lastAppliedBroadcastRevision": 1,
            "errorCode": "track_load_failed",
            "errorMessage": "Unable to load target",
            "clientSeq": 2,
        }
        self._settle(failed, "failed-2", 13000)
        failed_state = getBroadcastState("broadcast-1")[
            "participantStates"
        ][0]
        self.assertEqual(failed_state["syncStatus"], "failed")
        self.assertEqual(failed_state["failedBroadcastRevision"], 2)
        self.assertEqual(
            failed_state["failedErrorMessage"],
            "Unable to load target",
        )

        applied = self._applied_feedback(
            revision=2,
            delivery_id="delivery-2",
            client_seq=3,
            positionMs=1600,
        )
        self._settle(applied, "applied-2", 14000)
        applied_state = getBroadcastState("broadcast-1")[
            "participantStates"
        ][0]
        self.assertEqual(applied_state["syncStatus"], "applied")
        self.assertEqual(applied_state["appliedBroadcastRevision"], 2)
        self.assertIsNone(applied_state["failedBroadcastRevision"])
        self.assertIsNone(applied_state["failedErrorCode"])

    def test_lagging_feedback_rebuilds_earliest_deadline(self):
        self._create()
        initial = getBroadcastState("broadcast-1")["participantStates"][0]
        commitBroadcastRevision(
            "broadcast-1",
            1,
            self._snapshot(revision=2, updated_at_ms=12000),
            "progress",
            [self._delivery("delivery-2", 2, "progress", 12000)],
        )
        commitBroadcastRevision(
            "broadcast-1",
            2,
            self._snapshot(revision=3, updated_at_ms=13000),
            "progress",
            [self._delivery("delivery-3", 3, "progress", 13000)],
        )
        pending = getBroadcastState("broadcast-1")["participantStates"][0]
        self.assertEqual(
            pending["feedbackDeadlineAtServerMs"],
            initial["feedbackDeadlineAtServerMs"],
        )
        self.assertEqual(pending["deadlineBroadcastRevision"], 1)
        self.assertEqual(pending["targetBroadcastRevision"], 3)

        self._settle(self._applied_feedback(), "applied-1", 15000)
        lagging = getBroadcastState("broadcast-1")["participantStates"][0]
        self.assertEqual(lagging["syncStatus"], "lagging")
        self.assertEqual(lagging["deadlineBroadcastRevision"], 2)
        self.assertEqual(lagging["feedbackDeadlineAtServerMs"], 23000)
        self._settle(self._applied_feedback(), "applied-1", 19000)
        replay = getBroadcastState("broadcast-1")["participantStates"][0]
        self.assertEqual(replay["deadlineBroadcastRevision"], 2)
        self.assertEqual(replay["feedbackDeadlineAtServerMs"], 23000)

    def test_deadline_timeout_is_not_reopened_by_progress(self):
        self._create()
        deadline = getBroadcastState("broadcast-1")["participantStates"][0][
            "feedbackDeadlineAtServerMs"
        ]

        timed_out = sweepBroadcastFeedbackDeadlines(deadline)

        self.assertEqual(len(timed_out), 1)
        self.assertEqual(timed_out[0]["syncStatus"], "timedOut")
        self.assertEqual(timed_out[0]["timedOutBroadcastRevision"], 1)
        commitBroadcastRevision(
            "broadcast-1",
            1,
            self._snapshot(revision=2, updated_at_ms=20000),
            "progress",
            [self._delivery("delivery-2", 2, "progress", 20000)],
        )
        after_progress = getBroadcastState("broadcast-1")[
            "participantStates"
        ][0]
        self.assertEqual(after_progress["syncStatus"], "timedOut")
        self.assertEqual(after_progress["deadlineBroadcastRevision"], 1)
        self.assertEqual(after_progress["feedbackDeadlineAtServerMs"], deadline)
        self.assertEqual(after_progress["targetBroadcastRevision"], 2)

        self._settle(self._applied_feedback(), "applied-after-timeout-1", 21000)
        lagging = getBroadcastState("broadcast-1")["participantStates"][0]
        self.assertEqual(lagging["syncStatus"], "lagging")
        self.assertIsNone(lagging["timedOutBroadcastRevision"])
        self.assertEqual(lagging["deadlineBroadcastRevision"], 2)
        self.assertEqual(lagging["feedbackDeadlineAtServerMs"], 29000)

        self._settle(
            self._applied_feedback(
                revision=2,
                delivery_id="delivery-2",
                client_seq=2,
                positionMs=1600,
            ),
            "applied-after-timeout-2",
            22000,
        )
        applied = getBroadcastState("broadcast-1")["participantStates"][0]
        self.assertEqual(applied["syncStatus"], "applied")
        self.assertEqual(applied["appliedBroadcastRevision"], 2)
        self.assertIsNone(applied["timedOutBroadcastRevision"])

    def test_terminal_applied_feedback_clears_restore_pending(self):
        self._create()
        terminal = self._snapshot(
            revision=2,
            lifecycle="stopped",
            updated_at_ms=20000,
        )
        terminalBroadcastState(
            "broadcast-1",
            terminal,
            {"stopped": True},
            [self._delivery("terminal-delivery", 2, "stop", 20000)],
            expected_broadcast_revision=1,
            terminal_at_ms=20000,
        )
        feedback = self._applied_feedback(
            revision=2,
            delivery_id="terminal-delivery",
            client_seq=1,
            state="stopped",
            restoreCompleted=True,
        )

        self._settle(feedback, "terminal-applied", 21000)

        participant = getBroadcastState("broadcast-1")[
            "participantStates"
        ][0]
        self.assertFalse(participant["restorePending"])
        self.assertTrue(participant["terminalConfirmed"])
        self.assertTrue(participant["restoreCompleted"])
        self.assertEqual(
            getBroadcastFenceForPair(
                "alice",
                "participant-1",
                "device:participant-1",
            ),
            None,
        )

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
