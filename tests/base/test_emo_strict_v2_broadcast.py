import json
import time
import unittest
from unittest import mock

from supysonic import db
from supysonic.emo import ws as emo_ws
from supysonic.emo.broadcast_store import terminalBroadcastState
from supysonic.emo.ws_store import (
    PlaybackContextBroadcastBarrierError,
    applyStrictPlaybackUpdate,
    closeStrictPlaybackContextState,
    commitStrictPlaybackHandoff,
    completeStrictPlaybackHandoff,
    createPlaybackControlTransaction,
    createPlaybackPrepareTransaction,
    createStrictPlaybackContextState,
    createStrictPlaybackHandoff,
    ensureStrictPlaybackContextState,
    getDevicePlaybackState,
    getPlaybackContextState,
    mutateStrictPlaybackContextControl,
    mutateStrictPlaybackContextQueue,
    savePlaybackLocalIntent,
    settlePlaybackControlTransaction,
    settlePlaybackPrepareTransaction,
    terminateStrictPlaybackHandoff,
)
from supysonic.emo.ws_state import get_state

from tests.base.test_emo_ws import (
    CAPABILITY_PLAYBACK_CONTEXT_V2,
    EmoWebSocketTestCase,
)


class StrictV2BroadcastTestCase(EmoWebSocketTestCase):
    def setUp(self):
        self.broadcast_ready_patcher = mock.patch(
            "supysonic.emo.strict_v2_readiness.BROADCAST_IMPLEMENTATION_READY",
            True,
        )
        self.broadcast_ready_patcher.start()
        super().setUp()

    def tearDown(self):
        try:
            self.clients = [
                client
                for client in self.clients
                if client.is_connected(namespace="/emo")
            ]
            super().tearDown()
        finally:
            self.broadcast_ready_patcher.stop()

    def connect_broadcast_devices(self):
        authority = self.connect_device(
            "alice",
            "Alic3",
            "authority-1",
            "device:authority-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
            },
        )
        participant = self.connect_device(
            "alice",
            "Alic3",
            "participant-1",
            "device:participant-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
            },
        )
        controller = self.connect_device(
            "alice",
            "Alic3",
            "controller-1",
            "device:controller-1",
            ["controller"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "canPlay": False,
                "canPause": False,
                "canSeek": False,
            },
        )
        self.ensure_playback_context(
            authority,
            "context-create-broadcast-source",
            playback_context_id="context-broadcast-source",
            device_session_id="device:authority-1",
            queue_song_ids=["source-song-1", "source-song-2"],
            position_ms=900,
            state="playing",
        )
        self.ensure_playback_context(
            participant,
            "context-create-participant-original",
            playback_context_id="context-participant-original",
            device_session_id="device:participant-1",
            queue_song_ids=["original-song-1"],
            position_ms=500,
            state="paused",
        )
        for client in (authority, participant, controller):
            self.get_messages(client)
        self.report_source_state(authority)
        for client in (authority, participant, controller):
            self.get_messages(client)
        return authority, participant, controller

    def report_source_state(self, authority, sampled_at_ms=None, client_seq=1):
        if sampled_at_ms is None:
            sampled_at_ms = int(time.time() * 1000)
        authority.emit(
            "message",
            {
                "type": "event",
                "action": "playback.update",
                "requestId": "source-state-%d" % client_seq,
                "payload": {
                    "playbackContextId": "context-broadcast-source",
                    "deviceSessionId": "device:authority-1",
                    "origin": "passive",
                    "appliedControlVersion": 1,
                    "state": "playing",
                    "trackId": "source-song-1",
                    "positionMs": 1000,
                    "positionSampledAtServerMs": sampled_at_ms,
                    "playbackRate": 1.0,
                    "clientSeq": client_seq,
                },
            },
            namespace="/emo",
        )

    def sync_source_queue(
        self,
        authority,
        queue_song_ids,
        current_index=None,
        position_ms=0,
        sampled_at_ms=None,
        request_id="source-queue-sync-1",
    ):
        if sampled_at_ms is None:
            sampled_at_ms = int(time.time() * 1000)
        context = getPlaybackContextState("context-broadcast-source")
        payload = {
            "playbackContextId": "context-broadcast-source",
            "deviceSessionId": "device:authority-1",
            "queueSongIds": list(queue_song_ids),
            "positionMs": position_ms,
            "positionSampledAtServerMs": sampled_at_ms,
            "baseQueueRevision": context["queueRevision"],
            "baseControlVersion": context["controlVersion"],
        }
        if current_index is not None:
            payload["currentIndex"] = current_index
        authority.emit(
            "message",
            {
                "type": "state",
                "action": "queue.context.sync",
                "requestId": request_id,
                "payload": payload,
            },
            namespace="/emo",
        )

    def update_source_playback(
        self,
        authority,
        client_seq,
        sampled_at_ms,
        **extra,
    ):
        payload = {
            "playbackContextId": "context-broadcast-source",
            "deviceSessionId": "device:authority-1",
            "origin": "passive",
            "appliedControlVersion": getPlaybackContextState(
                "context-broadcast-source"
            )["controlVersion"],
            "state": "playing",
            "trackId": "source-song-1",
            "positionMs": 1000,
            "positionSampledAtServerMs": sampled_at_ms,
            "playbackRate": 1.0,
            "clientSeq": client_seq,
        }
        payload.update(extra)
        if payload["origin"] == "localUser":
            payload.pop("appliedControlVersion", None)
        authority.emit(
            "message",
            {
                "type": "event",
                "action": "playback.update",
                "requestId": "source-update-%d" % client_seq,
                "payload": payload,
            },
            namespace="/emo",
        )

    def send_broadcast_feedback(
        self,
        client,
        broadcast_id,
        delivery_payload,
        request_id="broadcast-feedback-1",
        client_seq=1,
        execution_status="applied",
        **extra,
    ):
        payload = {
            "playbackContextId": "context-broadcast-source",
            "broadcastId": broadcast_id,
            "deviceSessionId": "device:participant-1",
            "deliveryId": delivery_payload["deliveryId"],
            "executionStatus": execution_status,
            "clientSeq": client_seq,
        }
        if execution_status == "applied":
            payload.update(
                {
                    "appliedBroadcastRevision": delivery_payload[
                        "broadcastRevision"
                    ],
                    "queueIndex": delivery_payload["currentIndex"],
                    "trackId": delivery_payload["trackId"],
                    "state": delivery_payload["state"],
                    "positionMs": delivery_payload["positionMs"],
                    "playbackRate": delivery_payload["playbackRate"],
                }
            )
        else:
            payload.update(
                {
                    "failedBroadcastRevision": delivery_payload[
                        "broadcastRevision"
                    ],
                    "lastAppliedBroadcastRevision": 0,
                    "errorCode": "execution_failed",
                }
            )
        payload.update(extra)
        client.emit(
            "message",
            {
                "type": "event",
                "action": "broadcast.feedback",
                "requestId": request_id,
                "payload": payload,
            },
            namespace="/emo",
        )
        return payload

    def start_strict_broadcast(
        self,
        client,
        request_id="broadcast-start-1",
        intent_id="intent-1",
        participants=None,
        **extra,
    ):
        payload = {
            "playbackContextId": "context-broadcast-source",
            "intentId": intent_id,
        }
        if participants is not None:
            payload["participants"] = participants
        payload.update(extra)
        client.emit(
            "message",
            {
                "type": "command",
                "action": "broadcast.start",
                "requestId": request_id,
                "payload": payload,
            },
            namespace="/emo",
        )
        return self.get_messages(client)

    @staticmethod
    def _push(messages, action="broadcast.start"):
        return next(message for message in messages if message["action"] == action)

    def test_start_uses_source_context_and_separates_three_roles(self):
        authority, participant, controller = self.connect_broadcast_devices()
        source_state = getDevicePlaybackState(
            "context-broadcast-source",
            "authority-1",
        )

        controller_messages = self.start_strict_broadcast(
            controller,
            participants=["participant-1"],
        )
        ack = self.get_ack(controller_messages, "broadcast-start-1")
        controller_push = self._push(controller_messages)
        source_messages = self.get_messages(authority)
        participant_messages = self.get_messages(participant)
        source_push = self._push(source_messages)
        participant_push = self._push(participant_messages)

        self.assertEqual(
            set(ack["payload"]),
            {
                "action",
                "started",
                "intentId",
                "broadcastId",
                "participants",
                "skippedClientIds",
            },
        )
        self.assertEqual(ack["payload"]["participants"], ["participant-1"])
        self.assertEqual(ack["payload"]["intentId"], "intent-1")
        base = source_push["payload"]
        self.assertEqual(base["ownerClientId"], "controller-1")
        self.assertEqual(base["authorityClientId"], "authority-1")
        self.assertEqual(base["queueSongIds"], ["source-song-1", "source-song-2"])
        self.assertEqual(base["trackId"], "source-song-1")
        self.assertEqual(base["participants"], ["participant-1"])
        self.assertEqual(base["broadcastRevision"], 1)
        for forbidden in ("version", "queueRevision", "controlVersion", "epoch"):
            self.assertNotIn(forbidden, base)
        for lifecycle_copy in (source_push, controller_push):
            self.assertNotIn("deliveryId", lifecycle_copy["payload"])
            self.assertNotIn("effectiveAtServerMs", lifecycle_copy["payload"])
            self.assertNotIn("serverTimeMs", lifecycle_copy["payload"])
        execution = participant_push["payload"]
        self.assertIn("deliveryId", execution)
        self.assertGreaterEqual(
            execution["effectiveAtServerMs"] - execution["serverTimeMs"],
            250,
        )
        self.assertEqual(
            base["positionMs"],
            source_state["positionMs"]
            + int(
                (
                    execution["effectiveAtServerMs"]
                    - source_state["positionSampledAtServerMs"]
                )
                * source_state["playbackRate"]
            ),
        )
        self.assertEqual(
            {key: value for key, value in execution.items()
             if key not in {"deliveryId", "effectiveAtServerMs", "serverTimeMs"}},
            base,
        )
        self.assertFalse(
            any(
                message["action"].startswith("player.")
                or message["action"] == "queue.playItem"
                for message in source_messages
            )
        )
        self.assertEqual(db.EmoBroadcast.select().count(), 1)
        self.assertEqual(db.EmoBroadcastParticipant.select().count(), 1)
        self.assertEqual(db.EmoBroadcastDelivery.select().count(), 1)
        self.assertEqual(db.EmoPlaybackContext.select().count(), 2)

    def test_status_returns_persisted_anchor_and_frozen_pair(self):
        authority, participant, controller = self.connect_broadcast_devices()
        ack = self.get_ack(
            self.start_strict_broadcast(controller, participants=["participant-1"]),
            "broadcast-start-1",
        )
        self.get_messages(authority)
        self.get_messages(participant)
        broadcast_id = ack["payload"]["broadcastId"]
        controller.emit(
            "message",
            {
                "type": "state",
                "action": "broadcast.status",
                "requestId": "broadcast-status-1",
                "payload": {
                    "playbackContextId": "context-broadcast-source",
                    "broadcastId": broadcast_id,
                },
            },
            namespace="/emo",
        )
        status = self.get_ack(
            self.get_messages(controller),
            "broadcast-status-1",
        )["payload"]
        persisted_snapshot = emo_ws.getPersistentBroadcastState(broadcast_id)[
            "snapshot"
        ]
        self.assertIsInstance(status["serverTimeMs"], int)
        self.assertEqual(status["broadcast"], persisted_snapshot)
        self.assertNotIn("deliveryId", status["broadcast"])
        self.assertEqual(status["broadcast"]["broadcastRevision"], 1)
        self.assertEqual(len(status["participantStates"]), 1)
        participant_state = status["participantStates"][0]
        self.assertEqual(participant_state["clientId"], "participant-1")
        self.assertEqual(
            participant_state["deviceSessionId"],
            "device:participant-1",
        )
        self.assertEqual(participant_state["syncStatus"], "pending")
        self.assertTrue(participant_state["online"])

    def test_feedback_applied_confirms_only_requesting_participant(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        delivery = self._push(
            self.get_messages(participant),
            "broadcast.start",
        )["payload"]
        self.get_messages(authority)
        self.get_messages(controller)
        context_before = getPlaybackContextState("context-broadcast-source")
        snapshot_before = emo_ws.getPersistentBroadcastState(
            start_ack["broadcastId"]
        )["snapshot"]
        request_payload = self.send_broadcast_feedback(
            participant,
            start_ack["broadcastId"],
            delivery,
            positionMs=1234,
        )

        messages = self.get_messages(participant)
        confirmation = self._push(messages, "broadcast.feedback")
        self.assertFalse(
            any(message["action"] == "system.ack" for message in messages)
        )
        self.assertNotIn("requestId", confirmation)
        self.assertEqual(
            confirmation["payload"]["sourceClientId"],
            "participant-1",
        )
        for field_name in (
            "deliveryId",
            "executionStatus",
            "appliedBroadcastRevision",
            "queueIndex",
            "trackId",
            "state",
            "positionMs",
            "playbackRate",
            "clientSeq",
        ):
            self.assertEqual(
                confirmation["payload"][field_name],
                request_payload[field_name],
            )
        self.assertEqual(self.get_messages(authority), [])
        self.assertEqual(self.get_messages(controller), [])
        persisted_after = emo_ws.getPersistentBroadcastState(
            start_ack["broadcastId"]
        )
        self.assertEqual(persisted_after["snapshot"], snapshot_before)
        self.assertEqual(
            getPlaybackContextState("context-broadcast-source"),
            context_before,
        )

        controller.emit(
            "message",
            {
                "type": "state",
                "action": "broadcast.status",
                "requestId": "broadcast-status-feedback-1",
                "payload": {
                    "playbackContextId": "context-broadcast-source",
                    "broadcastId": start_ack["broadcastId"],
                },
            },
            namespace="/emo",
        )
        status = self.get_ack(
            self.get_messages(controller),
            "broadcast-status-feedback-1",
        )["payload"]["participantStates"][0]
        self.assertEqual(status["syncStatus"], "applied")
        self.assertEqual(status["appliedBroadcastRevision"], 1)
        self.assertEqual(status["positionMs"], 1234)
        self.assertEqual(status["lastFeedbackClientSeq"], 1)
        self.assertEqual(
            status["appliedAtServerMs"],
            status["lastFeedbackAtServerMs"],
        )

        self.send_broadcast_feedback(
            participant,
            start_ack["broadcastId"],
            delivery,
            request_id="broadcast-feedback-replay-1",
            positionMs=1234,
        )
        replay = self._push(
            self.get_messages(participant),
            "broadcast.feedback",
        )
        self.assertEqual(replay["payload"], confirmation["payload"])

        self.send_broadcast_feedback(
            participant,
            start_ack["broadcastId"],
            delivery,
            request_id="broadcast-feedback-conflict-1",
            positionMs=1235,
        )
        conflict = self.get_error(
            self.get_messages(participant),
            "broadcast-feedback-conflict-1",
        )
        self.assertEqual(
            conflict["payload"]["code"],
            "client_sequence_conflict",
        )
        self.assertEqual(conflict["payload"]["currentClientSeq"], 1)

    def test_feedback_confirmation_emit_failure_replays_cached_result(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        delivery = self._push(
            self.get_messages(participant),
            "broadcast.start",
        )["payload"]
        self.get_messages(authority)
        self.get_messages(controller)

        with mock.patch.object(
            emo_ws,
            "_emit_message",
            side_effect=RuntimeError("injected feedback confirmation failure"),
        ):
            request_payload = self.send_broadcast_feedback(
                participant,
                start_ack["broadcastId"],
                delivery,
                request_id="broadcast-feedback-emit-failure-1",
                positionMs=1234,
            )

        self.assertEqual(self.get_messages(participant), [])
        self.assertEqual(
            db.EmoBroadcastFeedbackSettlement.select().count(),
            1,
        )
        persisted = emo_ws.getPersistentBroadcastState(start_ack["broadcastId"])
        self.assertEqual(
            persisted["participantStates"][0]["appliedPositionMs"],
            1234,
        )

        self.send_broadcast_feedback(
            participant,
            start_ack["broadcastId"],
            delivery,
            request_id="broadcast-feedback-emit-failure-1",
            positionMs=1234,
        )
        replay = self._push(
            self.get_messages(participant),
            "broadcast.feedback",
        )
        for field_name, field_value in request_payload.items():
            self.assertEqual(replay["payload"][field_name], field_value)
        self.assertEqual(
            db.EmoBroadcastFeedbackSettlement.select().count(),
            1,
        )

    def test_feedback_failed_then_applied_converges_status(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        delivery = self._push(
            self.get_messages(participant),
            "broadcast.start",
        )["payload"]
        self.get_messages(authority)
        self.get_messages(controller)

        self.send_broadcast_feedback(
            participant,
            start_ack["broadcastId"],
            delivery,
            execution_status="failed",
            errorCode="track_load_failed",
            errorMessage="Unable to load target",
        )
        failed = self._push(
            self.get_messages(participant),
            "broadcast.feedback",
        )
        self.assertEqual(failed["payload"]["executionStatus"], "failed")
        persisted = emo_ws.getPersistentBroadcastState(start_ack["broadcastId"])
        failed_state = persisted["participantStates"][0]
        self.assertEqual(failed_state["syncStatus"], "failed")
        self.assertEqual(failed_state["failedErrorCode"], "track_load_failed")
        controller.emit(
            "message",
            {
                "type": "state",
                "action": "broadcast.status",
                "requestId": "broadcast-status-failed-1",
                "payload": {
                    "playbackContextId": "context-broadcast-source",
                    "broadcastId": start_ack["broadcastId"],
                },
            },
            namespace="/emo",
        )
        failed_status = self.get_ack(
            self.get_messages(controller),
            "broadcast-status-failed-1",
        )["payload"]["participantStates"][0]
        self.assertEqual(failed_status["syncStatus"], "failed")
        self.assertEqual(failed_status["failedBroadcastRevision"], 1)
        self.assertEqual(failed_status["errorCode"], "track_load_failed")
        self.assertEqual(
            failed_status["errorMessage"],
            "Unable to load target",
        )

        self.send_broadcast_feedback(
            participant,
            start_ack["broadcastId"],
            delivery,
            request_id="broadcast-feedback-applied-2",
            client_seq=2,
            positionMs=1300,
        )
        applied = self._push(
            self.get_messages(participant),
            "broadcast.feedback",
        )
        self.assertEqual(applied["payload"]["executionStatus"], "applied")
        converged = emo_ws.getPersistentBroadcastState(
            start_ack["broadcastId"]
        )["participantStates"][0]
        self.assertEqual(converged["syncStatus"], "applied")
        self.assertIsNone(converged["failedBroadcastRevision"])
        self.assertIsNone(converged["failedErrorCode"])

    def test_feedback_deadline_timeout_preserves_snapshot_and_deadline(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        for client in (authority, participant, controller):
            self.get_messages(client)
        before = emo_ws.getPersistentBroadcastState(start_ack["broadcastId"])
        participant_before = before["participantStates"][0]

        timed_out = emo_ws.sweepBroadcastFeedbackDeadlines(
            participant_before["feedbackDeadlineAtServerMs"]
        )

        self.assertEqual(len(timed_out), 1)
        after = emo_ws.getPersistentBroadcastState(start_ack["broadcastId"])
        participant_after = after["participantStates"][0]
        self.assertEqual(after["snapshot"], before["snapshot"])
        self.assertEqual(participant_after["syncStatus"], "timedOut")
        self.assertEqual(
            participant_after["timedOutBroadcastRevision"],
            participant_before["deadlineBroadcastRevision"],
        )
        self.assertEqual(
            participant_after["feedbackDeadlineAtServerMs"],
            participant_before["feedbackDeadlineAtServerMs"],
        )
        controller.emit(
            "message",
            {
                "type": "state",
                "action": "broadcast.status",
                "requestId": "broadcast-status-timeout-1",
                "payload": {
                    "playbackContextId": "context-broadcast-source",
                    "broadcastId": start_ack["broadcastId"],
                },
            },
            namespace="/emo",
        )
        status = self.get_ack(
            self.get_messages(controller),
            "broadcast-status-timeout-1",
        )["payload"]["participantStates"][0]
        self.assertEqual(status["syncStatus"], "timedOut")
        self.assertEqual(status["errorCode"], "feedback_timeout")
        self.assertEqual(
            status["timedOutBroadcastRevision"],
            status["deadlineBroadcastRevision"],
        )

    def test_source_cannot_send_broadcast_feedback(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        delivery = self._push(
            self.get_messages(participant),
            "broadcast.start",
        )["payload"]
        self.get_messages(authority)
        self.get_messages(controller)
        source_delivery = dict(delivery)
        self.send_broadcast_feedback(
            authority,
            start_ack["broadcastId"],
            source_delivery,
            request_id="broadcast-feedback-source-1",
            deviceSessionId="device:authority-1",
        )
        error = self.get_error(
            self.get_messages(authority),
            "broadcast-feedback-source-1",
        )
        self.assertEqual(error["payload"]["code"], "forbidden")

    def test_terminal_feedback_confirms_restore_and_releases_pair_fence(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        for client in (authority, participant, controller):
            self.get_messages(client)
        self.sync_source_queue(
            authority,
            [],
            position_ms=0,
            request_id="source-clear-terminal-feedback-1",
        )
        self.get_messages(authority)
        terminal_delivery = self._push(
            self.get_messages(participant),
            "broadcast.stop",
        )["payload"]
        self.get_messages(controller)

        self.send_broadcast_feedback(
            participant,
            start_ack["broadcastId"],
            terminal_delivery,
            request_id="broadcast-terminal-feedback-1",
            state="stopped",
            restoreCompleted=True,
        )

        confirmation = self._push(
            self.get_messages(participant),
            "broadcast.feedback",
        )
        self.assertEqual(confirmation["payload"]["state"], "stopped")
        self.assertTrue(confirmation["payload"]["restoreCompleted"])
        persisted = emo_ws.getPersistentBroadcastState(start_ack["broadcastId"])
        participant_state = persisted["participantStates"][0]
        self.assertFalse(participant_state["restorePending"])
        self.assertTrue(participant_state["terminalConfirmed"])
        self.assertEqual(
            db.EmoBroadcastFence.select().where(
                (db.EmoBroadcastFence.broadcast_id == start_ack["broadcastId"])
                & (db.EmoBroadcastFence.role == "ordinary")
            ).count(),
            0,
        )

    def test_old_start_snapshot_fields_are_rejected(self):
        authority, _participant, _controller = self.connect_broadcast_devices()
        messages = self.start_strict_broadcast(
            authority,
            queueSongIds=["old-song"],
            currentIndex=0,
            positionMs=0,
            autoPlay=True,
        )
        error = self.get_error(messages, "broadcast-start-1")
        self.assertEqual(error["payload"]["code"], "bad_request")
        self.assertEqual(db.EmoBroadcast.select().count(), 0)

    def test_source_must_have_fresh_playing_settled_device_state(self):
        authority, _participant, _controller = self.connect_broadcast_devices()
        db.EmoDevicePlaybackState.delete().execute()
        messages = self.start_strict_broadcast(authority)
        error = self.get_error(messages, "broadcast-start-1")
        self.assertEqual(error["payload"]["code"], "conflict")
        self.assertEqual(db.EmoBroadcast.select().count(), 0)

    def test_no_eligible_ordinary_rejects_without_creating_broadcast(self):
        authority, participant, controller = self.connect_broadcast_devices()
        state = get_state()
        sid = state.get_sid_for_client("participant-1", user_name="alice")
        with state._lock:
            state._sessions[sid]["clockPingCount"] = 2
        messages = self.start_strict_broadcast(
            controller,
            participants=["participant-1"],
        )
        error = self.get_error(messages, "broadcast-start-1")
        self.assertEqual(error["payload"]["code"], "bad_request")
        self.assertEqual(db.EmoBroadcast.select().count(), 0)
        self.assertEqual(db.EmoBroadcastParticipant.select().count(), 0)
        participant_messages = self.get_messages(participant)
        authority_messages = self.get_messages(authority)
        self.assertFalse(
            any(
                message["action"] == "broadcast.start"
                for message in participant_messages
            )
        )
        self.assertFalse(
            any(message["action"] == "broadcast.start"
                for message in authority_messages)
        )

    def test_recovery_slot_exhaustion_returns_rate_limited_without_rows(self):
        _authority, _participant, controller = self.connect_broadcast_devices()
        for index in range(256):
            db.EmoBroadcastFence.create(
                resource_key="occupied-slot:%d" % index,
                broadcast_id="old-broadcast:%d" % index,
                user_name="alice",
                role="ordinary",
                phase="restorePending",
                recovery_slot_reserved=1,
            )
        self.report_source_state(_authority, client_seq=2)
        messages = self.start_strict_broadcast(
            controller,
            participants=["participant-1"],
        )
        error = self.get_error(messages, "broadcast-start-1")
        self.assertEqual(
            error["payload"]["code"],
            "rate_limited",
            error,
        )
        self.assertGreater(error["payload"]["retryAfterMs"], 0)
        self.assertEqual(db.EmoBroadcast.select().count(), 0)
        self.assertEqual(db.EmoBroadcastParticipant.select().count(), 0)

    def test_same_intent_replays_ack_across_controller_reconnect_without_push(self):
        authority, participant, controller = self.connect_broadcast_devices()
        first = self.get_ack(
            self.start_strict_broadcast(controller, participants=["participant-1"]),
            "broadcast-start-1",
        )["payload"]
        self.get_messages(authority)
        self.get_messages(participant)
        controller.disconnect(namespace="/emo")
        replacement = self.connect_device(
            "alice",
            "Alic3",
            "controller-1",
            "device:controller-1",
            ["controller"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "canPlay": False,
                "canPause": False,
                "canSeek": False,
            },
        )
        replay = self.get_ack(
            self.start_strict_broadcast(
                replacement,
                request_id="broadcast-start-replay",
                participants=["participant-1"],
            ),
            "broadcast-start-replay",
        )["payload"]
        self.assertEqual(replay, first)
        self.assertEqual(self.get_messages(authority), [])
        self.assertEqual(self.get_messages(participant), [])
        self.assertEqual(db.EmoBroadcast.select().count(), 1)

    def test_same_intent_with_different_participants_conflicts(self):
        authority, _participant, controller = self.connect_broadcast_devices()
        self.start_strict_broadcast(controller, participants=["participant-1"])
        self.get_messages(authority)
        messages = self.start_strict_broadcast(
            controller,
            request_id="broadcast-start-conflict",
        )
        error = self.get_error(messages, "broadcast-start-conflict")
        self.assertEqual(error["payload"]["code"], "conflict")

    def test_source_change_before_store_commit_rejects_without_partial_rows(self):
        authority, _participant, controller = self.connect_broadcast_devices()
        create_broadcast = emo_ws.createBroadcastState

        def mutate_source_before_create(*args, **kwargs):
            db.EmoPlaybackContext.update(version=2).where(
                db.EmoPlaybackContext.playback_context_id
                == "context-broadcast-source"
            ).execute()
            return create_broadcast(*args, **kwargs)

        with mock.patch.object(
            emo_ws,
            "createBroadcastState",
            side_effect=mutate_source_before_create,
        ):
            messages = self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            )
        error = self.get_error(messages, "broadcast-start-1")
        self.assertEqual(error["payload"]["code"], "conflict")
        for model in (
            db.EmoBroadcast,
            db.EmoBroadcastParticipant,
            db.EmoBroadcastFence,
            db.EmoBroadcastRevision,
            db.EmoBroadcastDelivery,
        ):
            self.assertEqual(model.select().count(), 0, model.__name__)

    def test_ordinary_context_mutation_store_paths_hit_one_barrier(self):
        authority, _participant, controller = self.connect_broadcast_devices()
        self.start_strict_broadcast(
            controller,
            participants=["participant-1"],
        )
        self.get_messages(authority)
        context_id = "context-participant-original"
        before = getPlaybackContextState(context_id)
        mutation_functions = (
            createPlaybackControlTransaction,
            settlePlaybackControlTransaction,
            applyStrictPlaybackUpdate,
            createPlaybackPrepareTransaction,
            settlePlaybackPrepareTransaction,
            savePlaybackLocalIntent,
            createStrictPlaybackContextState,
            closeStrictPlaybackContextState,
            mutateStrictPlaybackContextQueue,
            mutateStrictPlaybackContextControl,
            createStrictPlaybackHandoff,
            terminateStrictPlaybackHandoff,
            commitStrictPlaybackHandoff,
        )
        for mutation in mutation_functions:
            with self.subTest(mutation=mutation.__name__):
                with self.assertRaises(PlaybackContextBroadcastBarrierError):
                    mutation(context_id)
        with self.assertRaises(PlaybackContextBroadcastBarrierError):
            ensureStrictPlaybackContextState(
                "alice",
                "participant-1",
                "device:participant-1",
                ["original-song-1"],
                0,
                500,
                "paused",
            )
        self.assertEqual(getPlaybackContextState(context_id), before)

    def test_ordinary_queue_sync_returns_canonical_conflict_without_push(self):
        authority, participant, controller = self.connect_broadcast_devices()
        self.start_strict_broadcast(
            controller,
            participants=["participant-1"],
        )
        for client in (authority, participant, controller):
            self.get_messages(client)
        before = getPlaybackContextState("context-participant-original")
        participant.emit(
            "message",
            {
                "type": "state",
                "action": "queue.context.sync",
                "requestId": "ordinary-queue-blocked-1",
                "payload": {
                    "playbackContextId": "context-participant-original",
                    "deviceSessionId": "device:participant-1",
                    "queueSongIds": ["replacement-song"],
                    "currentIndex": 0,
                    "positionMs": 0,
                    "positionSampledAtServerMs": int(time.time() * 1000),
                    "baseQueueRevision": before["queueRevision"],
                    "baseControlVersion": before["controlVersion"],
                },
            },
            namespace="/emo",
        )
        error = self.get_error(
            self.get_messages(participant),
            "ordinary-queue-blocked-1",
        )
        self.assertEqual(error["payload"]["code"], "conflict")
        self.assertEqual(
            error["payload"]["currentVersion"],
            before["version"],
        )
        self.assertEqual(
            error["payload"]["currentQueueRevision"],
            before["queueRevision"],
        )
        self.assertEqual(
            error["payload"]["currentControlVersion"],
            before["controlVersion"],
        )
        self.assertEqual(
            getPlaybackContextState("context-participant-original"),
            before,
        )
        self.assertEqual(self.get_messages(authority), [])
        self.assertEqual(self.get_messages(controller), [])

    def test_source_noop_ensure_allowed_but_close_and_handoff_are_blocked(self):
        authority, _participant, controller = self.connect_broadcast_devices()
        self.start_strict_broadcast(
            controller,
            participants=["participant-1"],
        )
        before = getPlaybackContextState("context-broadcast-source")
        ensured = ensureStrictPlaybackContextState(
            "alice",
            "authority-1",
            "device:authority-1",
            ["source-song-1", "source-song-2"],
            0,
            before["positionMs"],
            before["state"],
        )
        self.assertFalse(ensured.mutated)
        self.assertEqual(ensured.canonical_context, before)
        with self.assertRaises(PlaybackContextBroadcastBarrierError):
            closeStrictPlaybackContextState(
                "context-broadcast-source",
                "alice",
            )
        with self.assertRaises(PlaybackContextBroadcastBarrierError):
            createStrictPlaybackHandoff(
                "context-broadcast-source",
                {
                    "userName": "alice",
                    "sourceClientId": "authority-1",
                    "targetClientId": "participant-1",
                },
                "device:participant-1",
            )
        with self.assertRaises(PlaybackContextBroadcastBarrierError):
            ensureStrictPlaybackContextState(
                "alice",
                "authority-1",
                "device:authority-rebound",
                ["source-song-1", "source-song-2"],
                0,
                before["positionMs"],
                before["state"],
            )
        with self.assertRaises(PlaybackContextBroadcastBarrierError):
            createStrictPlaybackContextState(
                "context-second-source-binding",
                "alice",
                "authority-1",
                "device:authority-1",
                ["source-song-1"],
                0,
                0,
                "playing",
            )
        self.report_source_state(authority, client_seq=2)
        self.assertEqual(
            getDevicePlaybackState(
                "context-broadcast-source",
                "authority-1",
            )["clientSeq"],
            2,
        )
        self.assertEqual(
            getPlaybackContextState("context-broadcast-source")["lifecycle"],
            "active",
        )

    def test_handoff_target_pair_cannot_cross_ordinary_barrier(self):
        authority, _participant, controller = self.connect_broadcast_devices()
        self.start_strict_broadcast(
            controller,
            participants=["participant-1"],
        )
        self.get_messages(authority)
        source_result = createStrictPlaybackContextState(
            "context-handoff-other-source",
            "alice",
            "other-source",
            "device:other-source",
            ["other-song"],
            0,
            0,
            "playing",
        )
        handoff = {
            "handoffId": "handoff-blocked-target",
            "userName": "alice",
            "sourceClientId": "other-source",
            "targetClientId": "participant-1",
            "baseControlVersion": source_result.canonical_context[
                "controlVersion"
            ],
        }
        with self.assertRaises(PlaybackContextBroadcastBarrierError):
            createStrictPlaybackHandoff(
                "context-handoff-other-source",
                handoff,
                "device:participant-1",
            )
        db.EmoPlaybackHandoff.create(
            handoff_id="handoff-complete-blocked-target",
            playback_context_id="context-handoff-other-source",
            user_name="alice",
            source_client_id="other-source",
            target_client_id="participant-1",
            status="committing",
            base_control_version=source_result.canonical_context[
                "controlVersion"
            ],
            snapshot_json="{}",
        )
        with self.assertRaises(PlaybackContextBroadcastBarrierError):
            completeStrictPlaybackHandoff(
                "context-handoff-other-source",
                "handoff-complete-blocked-target",
                "alice",
                "participant-1",
                "device:participant-1",
            )
        unchanged_source = getPlaybackContextState(
            "context-handoff-other-source"
        )
        self.assertEqual(unchanged_source["authorityClientId"], "other-source")

    def test_restore_pending_ensure_is_cached_without_side_effects(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        persisted = emo_ws.getPersistentBroadcastState(
            start_ack["broadcastId"]
        )
        terminal_snapshot = dict(
            persisted["snapshot"],
            lifecycleState="stopped",
            broadcastRevision=2,
        )
        terminalBroadcastState(
            start_ack["broadcastId"],
            terminal_snapshot,
            {
                "action": "broadcast.stop",
                "broadcastId": start_ack["broadcastId"],
            },
        )
        for client in (authority, participant, controller):
            self.get_messages(client)
        before = getPlaybackContextState("context-participant-original")
        request_message = {
            "type": "command",
            "action": "playback.context.ensure",
            "requestId": "restore-pending-ensure-1",
            "payload": {
                "deviceSessionId": "device:participant-1",
                "queueSongIds": ["replacement-song"],
                "currentIndex": 0,
                "positionMs": 0,
                "state": "paused",
            },
        }
        participant.emit("message", request_message, namespace="/emo")
        first = self.get_error(
            self.get_messages(participant),
            "restore-pending-ensure-1",
        )
        participant.emit("message", request_message, namespace="/emo")
        replay = self.get_error(
            self.get_messages(participant),
            "restore-pending-ensure-1",
        )
        self.assertEqual(first["payload"], replay["payload"])
        self.assertEqual(first["payload"]["code"], "restore_in_progress")
        self.assertTrue(first["payload"]["retryable"])
        self.assertEqual(
            first["payload"]["playbackContextId"],
            "context-participant-original",
        )
        self.assertEqual(
            {
                key: first["payload"][key]
                for key in (
                    "currentVersion",
                    "currentQueueRevision",
                    "currentControlVersion",
                )
            },
            {
                "currentVersion": before["version"],
                "currentQueueRevision": before["queueRevision"],
                "currentControlVersion": before["controlVersion"],
            },
        )
        self.assertEqual(
            getPlaybackContextState("context-participant-original"),
            before,
        )

    def test_broadcast_pause_atomically_targets_source_and_ordinary(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        for client in (authority, participant, controller):
            self.get_messages(client)
        controller.emit(
            "message",
            {
                "type": "command",
                "action": "broadcast.pause",
                "requestId": "broadcast-pause-r18-1",
                "payload": {
                    "playbackContextId": "context-broadcast-source",
                    "broadcastId": start_ack["broadcastId"],
                    "baseControlVersion": 1,
                },
            },
            namespace="/emo",
        )
        controller_messages = self.get_messages(controller)
        self.assertFalse(
            [
                message
                for message in controller_messages
                if message["action"] == "system.error"
            ],
            controller_messages,
        )
        self.get_ack(controller_messages, "broadcast-pause-r18-1")
        observer = self._push(controller_messages, "broadcast.pause")
        source_messages = self.get_messages(authority)
        ordinary_messages = self.get_messages(participant)
        source_command = self._push(source_messages, "player.pause")
        ordinary = self._push(ordinary_messages, "broadcast.pause")
        self.assertFalse(
            any(
                message["action"] == "broadcast.pause"
                for message in source_messages
            )
        )
        self.assertNotIn("deliveryId", observer["payload"])
        self.assertIn("deliveryId", ordinary["payload"])
        for field_name in ("effectiveAtServerMs", "serverTimeMs"):
            self.assertEqual(
                source_command["payload"][field_name],
                ordinary["payload"][field_name],
            )
            self.assertEqual(
                observer["payload"][field_name],
                ordinary["payload"][field_name],
            )
        persisted = emo_ws.getPersistentBroadcastState(
            start_ack["broadcastId"]
        )
        context = getPlaybackContextState("context-broadcast-source")
        self.assertEqual(persisted["snapshot"]["broadcastRevision"], 2)
        self.assertEqual(persisted["snapshot"]["state"], "paused")
        self.assertEqual(
            persisted["snapshot"]["sourceControlVersion"],
            context["controlVersion"],
        )
        self.assertEqual(context["controlVersion"], 2)
        self.assertEqual(
            persisted["snapshot"]["serverUpdatedAtMs"],
            ordinary["payload"]["effectiveAtServerMs"],
        )
        transaction = db.EmoPlaybackControlTransaction.get()
        accepted_target = json.loads(transaction.accepted_target_json)
        self.assertEqual(
            accepted_target["effectiveAtServerMs"],
            ordinary["payload"]["effectiveAtServerMs"],
        )

    def test_broadcast_play_item_advances_source_and_broadcast_cursors_once(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        for client in (authority, participant, controller):
            self.get_messages(client)
        controller.emit(
            "message",
            {
                "type": "command",
                "action": "broadcast.playItem",
                "requestId": "broadcast-play-item-r18-1",
                "payload": {
                    "playbackContextId": "context-broadcast-source",
                    "broadcastId": start_ack["broadcastId"],
                    "queueIndex": 1,
                    "baseQueueRevision": 1,
                    "baseControlVersion": 1,
                },
            },
            namespace="/emo",
        )
        controller_messages = self.get_messages(controller)
        self.assertFalse(
            [
                message
                for message in controller_messages
                if message["action"] == "system.error"
            ],
            controller_messages,
        )
        self.get_ack(
            controller_messages,
            "broadcast-play-item-r18-1",
        )
        source = self._push(
            self.get_messages(authority),
            "queue.playItem",
        )
        ordinary = self._push(
            self.get_messages(participant),
            "broadcast.playItem",
        )
        context = getPlaybackContextState("context-broadcast-source")
        persisted = emo_ws.getPersistentBroadcastState(
            start_ack["broadcastId"]
        )
        self.assertEqual(context["currentIndex"], 1)
        self.assertEqual(context["queueRevision"], 2)
        self.assertEqual(context["controlVersion"], 2)
        self.assertEqual(persisted["snapshot"]["broadcastRevision"], 2)
        self.assertEqual(persisted["snapshot"]["sourceQueueRevision"], 2)
        self.assertEqual(persisted["snapshot"]["sourceControlVersion"], 2)
        self.assertEqual(persisted["snapshot"]["trackId"], "source-song-2")
        self.assertEqual(
            source["payload"]["effectiveAtServerMs"],
            ordinary["payload"]["effectiveAtServerMs"],
        )

    def test_broadcast_projection_emit_failure_does_not_block_ack_or_observer(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        for client in (authority, participant, controller):
            self.get_messages(client)
        participant_sid = get_state().get_sid_for_client(
            "participant-1",
            user_name="alice",
        )
        real_emit = emo_ws.socketio.emit

        def fail_participant_projection(event, message, *args, **kwargs):
            if (
                kwargs.get("to") == participant_sid
                and message.get("action") == "broadcast.pause"
            ):
                raise RuntimeError("injected participant projection failure")
            return real_emit(event, message, *args, **kwargs)

        with mock.patch.object(
            emo_ws.socketio,
            "emit",
            side_effect=fail_participant_projection,
        ):
            controller.emit(
                "message",
                {
                    "type": "command",
                    "action": "broadcast.pause",
                    "requestId": "broadcast-pause-fanout-failure-1",
                    "payload": {
                        "playbackContextId": "context-broadcast-source",
                        "broadcastId": start_ack["broadcastId"],
                        "baseControlVersion": 1,
                    },
                },
                namespace="/emo",
            )

        controller_messages = self.get_messages(controller)
        self.get_ack(
            controller_messages,
            "broadcast-pause-fanout-failure-1",
        )
        self._push(controller_messages, "broadcast.pause")
        self._push(self.get_messages(authority), "player.pause")
        self.assertFalse(
            any(
                message["action"] == "broadcast.pause"
                for message in self.get_messages(participant)
            )
        )
        persisted = emo_ws.getPersistentBroadcastState(
            start_ack["broadcastId"]
        )
        self.assertEqual(persisted["snapshot"]["broadcastRevision"], 2)

    def test_broadcast_source_emit_failure_persists_failed_transaction(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        for client in (authority, participant, controller):
            self.get_messages(client)
        authority_sid = get_state().get_sid_for_client(
            "authority-1",
            user_name="alice",
        )
        real_emit = emo_ws.socketio.emit

        def fail_source_command(event, message, *args, **kwargs):
            if (
                kwargs.get("to") == authority_sid
                and message.get("action") == "player.pause"
            ):
                raise RuntimeError("injected source command failure")
            return real_emit(event, message, *args, **kwargs)

        with mock.patch.object(
            emo_ws.socketio,
            "emit",
            side_effect=fail_source_command,
        ):
            controller.emit(
                "message",
                {
                    "type": "command",
                    "action": "broadcast.pause",
                    "requestId": "broadcast-pause-source-failure-1",
                    "payload": {
                        "playbackContextId": "context-broadcast-source",
                        "broadcastId": start_ack["broadcastId"],
                        "baseControlVersion": 1,
                    },
                },
                namespace="/emo",
            )

        error = self.get_error(
            self.get_messages(controller),
            "broadcast-pause-source-failure-1",
        )
        self.assertEqual(error["payload"]["code"], "internal_error")
        transaction = db.EmoPlaybackControlTransaction.get()
        self.assertEqual(transaction.status, "failed")
        self.assertEqual(transaction.error_code, "execution_unknown")
        persisted = emo_ws.getPersistentBroadcastState(
            start_ack["broadcastId"]
        )
        self.assertEqual(persisted["snapshot"]["broadcastRevision"], 2)
        self.assertEqual(persisted["snapshot"]["state"], "paused")
        self.assertFalse(
            any(
                message["action"] == "broadcast.pause"
                for message in self.get_messages(participant)
            )
        )

    def test_stale_broadcast_control_changes_no_context_or_revision(self):
        authority, _participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        for client in (authority, controller):
            self.get_messages(client)
        before_context = getPlaybackContextState("context-broadcast-source")
        before_broadcast = emo_ws.getPersistentBroadcastState(
            start_ack["broadcastId"]
        )
        controller.emit(
            "message",
            {
                "type": "command",
                "action": "broadcast.seek",
                "requestId": "broadcast-seek-stale-r18-1",
                "payload": {
                    "playbackContextId": "context-broadcast-source",
                    "broadcastId": start_ack["broadcastId"],
                    "positionMs": 5000,
                    "baseControlVersion": 2,
                },
            },
            namespace="/emo",
        )
        error = self.get_error(
            self.get_messages(controller),
            "broadcast-seek-stale-r18-1",
        )
        self.assertEqual(error["payload"]["code"], "stale_version")
        self.assertEqual(
            getPlaybackContextState("context-broadcast-source"),
            before_context,
        )
        after_broadcast = emo_ws.getPersistentBroadcastState(
            start_ack["broadcastId"]
        )
        self.assertEqual(
            after_broadcast["snapshot"],
            before_broadcast["snapshot"],
        )
        self.assertEqual(self.get_messages(authority), [])

    def test_source_queue_sync_derives_one_broadcast_queue_sync(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        for client in (authority, participant, controller):
            self.get_messages(client)
        before = getPlaybackContextState("context-broadcast-source")
        sampled_at_ms = int(time.time() * 1000)

        self.sync_source_queue(
            authority,
            ["source-song-1", "source-song-2"],
            current_index=1,
            position_ms=100,
            sampled_at_ms=sampled_at_ms,
        )

        source_messages = self.get_messages(authority)
        self.get_ack(source_messages, "source-queue-sync-1")
        self.assertFalse(
            any(
                message["action"] == "broadcast.queue.sync"
                for message in source_messages
            )
        )
        ordinary = self._push(
            self.get_messages(participant),
            "broadcast.queue.sync",
        )
        observer = self._push(
            self.get_messages(controller),
            "broadcast.queue.sync",
        )
        context = getPlaybackContextState("context-broadcast-source")
        persisted = emo_ws.getPersistentBroadcastState(start_ack["broadcastId"])
        self.assertEqual(context["version"], before["version"] + 1)
        self.assertEqual(
            context["queueRevision"],
            before["queueRevision"] + 1,
        )
        self.assertEqual(
            context["controlVersion"],
            before["controlVersion"] + 1,
        )
        self.assertEqual(context["currentIndex"], 1)
        self.assertEqual(context["trackId"], "source-song-2")
        self.assertEqual(persisted["snapshot"]["broadcastRevision"], 2)
        self.assertEqual(persisted["revisions"][-1]["action"], "queue.sync")
        self.assertEqual(
            persisted["snapshot"]["sourceQueueRevision"],
            context["queueRevision"],
        )
        self.assertIn("deliveryId", ordinary["payload"])
        self.assertNotIn("deliveryId", observer["payload"])
        for field_name in ("effectiveAtServerMs", "serverTimeMs"):
            self.assertEqual(
                ordinary["payload"][field_name],
                observer["payload"][field_name],
            )
        self.assertEqual(
            persisted["snapshot"]["serverUpdatedAtMs"],
            ordinary["payload"]["effectiveAtServerMs"],
        )

    def test_source_queue_clear_atomically_terminals_with_pre_idle_snapshot(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        for client in (authority, participant, controller):
            self.get_messages(client)
        before_context = getPlaybackContextState("context-broadcast-source")
        before_broadcast = emo_ws.getPersistentBroadcastState(
            start_ack["broadcastId"]
        )

        self.sync_source_queue(
            authority,
            [],
            position_ms=0,
            request_id="source-queue-clear-1",
        )

        source_messages = self.get_messages(authority)
        self.get_ack(source_messages, "source-queue-clear-1")
        source_stop = self._push(source_messages, "broadcast.stop")
        ordinary_messages = self.get_messages(participant)
        ordinary_stop = self._push(ordinary_messages, "broadcast.stop")
        observer_messages = self.get_messages(controller)
        observer_stop = self._push(observer_messages, "broadcast.stop")
        for messages in (source_messages, ordinary_messages, observer_messages):
            self.assertFalse(
                any(
                    message["action"] == "broadcast.queue.sync"
                    for message in messages
                )
            )
        self.assertNotIn("deliveryId", source_stop["payload"])
        self.assertIn("deliveryId", ordinary_stop["payload"])
        self.assertNotIn("deliveryId", observer_stop["payload"])
        for stop in (source_stop, ordinary_stop, observer_stop):
            self.assertNotIn("effectiveAtServerMs", stop["payload"])
            self.assertNotIn("serverTimeMs", stop["payload"])

        context = getPlaybackContextState("context-broadcast-source")
        persisted = emo_ws.getPersistentBroadcastState(start_ack["broadcastId"])
        terminal_snapshot = persisted["snapshot"]
        self.assertEqual(context["queueSongIds"], [])
        self.assertEqual(context["state"], "idle")
        self.assertEqual(context["version"], before_context["version"] + 1)
        self.assertEqual(
            context["queueRevision"],
            before_context["queueRevision"] + 1,
        )
        self.assertEqual(
            context["controlVersion"],
            before_context["controlVersion"] + 1,
        )
        self.assertEqual(terminal_snapshot["lifecycleState"], "stopped")
        self.assertEqual(terminal_snapshot["broadcastRevision"], 2)
        self.assertEqual(
            terminal_snapshot["queueSongIds"],
            before_broadcast["snapshot"]["queueSongIds"],
        )
        for field_name in (
            "sourceVersion",
            "sourceQueueRevision",
            "sourceControlVersion",
        ):
            self.assertEqual(
                terminal_snapshot[field_name],
                before_broadcast["snapshot"][field_name],
            )
        source_fences = db.EmoBroadcastFence.select().where(
            (db.EmoBroadcastFence.broadcast_id == start_ack["broadcastId"])
            & (db.EmoBroadcastFence.role == "source")
        )
        ordinary_fences = list(
            db.EmoBroadcastFence.select().where(
                (db.EmoBroadcastFence.broadcast_id == start_ack["broadcastId"])
                & (db.EmoBroadcastFence.role == "ordinary")
            )
        )
        participant_state = db.EmoBroadcastParticipant.get(
            db.EmoBroadcastParticipant.broadcast_id == start_ack["broadcastId"]
        )
        self.assertEqual(source_fences.count(), 0)
        self.assertTrue(ordinary_fences)
        self.assertEqual(
            {fence.phase for fence in ordinary_fences},
            {"restorePending"},
        )
        self.assertEqual(participant_state.restore_pending, 1)

    def test_passive_progress_pushes_at_most_once_per_second(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        for client in (authority, participant, controller):
            self.get_messages(client)
        before_context = getPlaybackContextState("context-broadcast-source")
        base_ms = int(time.time() * 1000)

        with mock.patch.object(emo_ws, "_server_time_ms", return_value=base_ms):
            self.update_source_playback(
                authority,
                2,
                base_ms,
                positionMs=1100,
            )
        first = self._push(
            self.get_messages(participant),
            "broadcast.progress",
        )
        self._push(self.get_messages(controller), "broadcast.progress")
        self.get_messages(authority)
        self.assertEqual(first["payload"]["broadcastRevision"], 2)
        self.assertEqual(first["payload"]["positionMs"], 1350)

        with mock.patch.object(
            emo_ws,
            "_server_time_ms",
            return_value=base_ms + 500,
        ):
            self.update_source_playback(
                authority,
                3,
                base_ms + 500,
                positionMs=1600,
            )
        self.get_messages(authority)
        self.assertFalse(
            any(
                message["action"] == "broadcast.progress"
                for message in self.get_messages(participant)
            )
        )
        self.assertFalse(
            any(
                message["action"] == "broadcast.progress"
                for message in self.get_messages(controller)
            )
        )
        self.assertEqual(
            emo_ws.getPersistentBroadcastState(start_ack["broadcastId"])[
                "snapshot"
            ]["broadcastRevision"],
            2,
        )

        with mock.patch.object(
            emo_ws,
            "_server_time_ms",
            return_value=base_ms + 1000,
        ):
            self.update_source_playback(
                authority,
                4,
                base_ms + 1000,
                positionMs=2100,
            )
        second = self._push(
            self.get_messages(participant),
            "broadcast.progress",
        )
        self._push(self.get_messages(controller), "broadcast.progress")
        self.assertEqual(second["payload"]["broadcastRevision"], 3)
        self.assertEqual(
            getPlaybackContextState("context-broadcast-source"),
            before_context,
        )

    def test_source_updates_use_deterministic_broadcast_actions(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        for client in (authority, participant, controller):
            self.get_messages(client)
        base_ms = int(time.time() * 1000)

        with mock.patch.object(emo_ws, "_server_time_ms", return_value=base_ms):
            self.update_source_playback(
                authority,
                2,
                base_ms,
                origin="localUser",
                executionStatus="committed",
                intentId="source-local-seek-1",
                epoch=1,
                observedControlVersion=1,
                queueIndex=0,
                positionMs=5000,
            )
        self._push(self.get_messages(participant), "broadcast.seek")
        self._push(self.get_messages(controller), "broadcast.seek")
        self.get_messages(authority)

        with mock.patch.object(
            emo_ws,
            "_server_time_ms",
            return_value=base_ms + 1000,
        ):
            self.update_source_playback(
                authority,
                3,
                base_ms + 1000,
                positionMs=6000,
                playbackRate=1.25,
            )
        self._push(self.get_messages(participant), "broadcast.state.sync")
        self._push(self.get_messages(controller), "broadcast.state.sync")
        self.get_messages(authority)

        with mock.patch.object(
            emo_ws,
            "_server_time_ms",
            return_value=base_ms + 1100,
        ):
            self.update_source_playback(
                authority,
                4,
                base_ms + 1100,
                state="paused",
                positionMs=6100,
                playbackRate=1.25,
            )
        self._push(self.get_messages(participant), "broadcast.pause")
        self._push(self.get_messages(controller), "broadcast.pause")
        self.get_messages(authority)

        with mock.patch.object(
            emo_ws,
            "_server_time_ms",
            return_value=base_ms + 1200,
        ):
            self.update_source_playback(
                authority,
                5,
                base_ms + 1200,
                state="stopped",
                positionMs=6100,
                playbackRate=1.25,
            )
        self._push(self.get_messages(participant), "broadcast.state.sync")
        self._push(self.get_messages(controller), "broadcast.state.sync")
        persisted = emo_ws.getPersistentBroadcastState(start_ack["broadcastId"])
        self.assertEqual(persisted["snapshot"]["broadcastRevision"], 5)
        self.assertEqual(
            [revision["action"] for revision in persisted["revisions"]],
            ["start", "seek", "state.sync", "pause", "state.sync"],
        )

    def test_equivalent_remote_commit_does_not_repeat_broadcast_revision(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        for client in (authority, participant, controller):
            self.get_messages(client)
        controller.emit(
            "message",
            {
                "type": "command",
                "action": "broadcast.pause",
                "requestId": "broadcast-pause-equivalent-1",
                "payload": {
                    "playbackContextId": "context-broadcast-source",
                    "broadcastId": start_ack["broadcastId"],
                    "baseControlVersion": 1,
                },
            },
            namespace="/emo",
        )
        self.get_ack(
            self.get_messages(controller),
            "broadcast-pause-equivalent-1",
        )
        source_command = self._push(
            self.get_messages(authority),
            "player.pause",
        )
        self._push(self.get_messages(participant), "broadcast.pause")
        for client in (authority, participant, controller):
            self.get_messages(client)
        sampled_at_ms = int(time.time() * 1000)

        self.update_source_playback(
            authority,
            2,
            sampled_at_ms,
            origin="remoteCommand",
            executionStatus="committed",
            commandControlVersion=2,
            appliedControlVersion=2,
            state="paused",
            trackId="source-song-1",
            positionMs=source_command["payload"]["positionMs"],
        )

        transaction = db.EmoPlaybackControlTransaction.get(
            db.EmoPlaybackControlTransaction.command_control_version == 2
        )
        persisted = emo_ws.getPersistentBroadcastState(start_ack["broadcastId"])
        self.assertEqual(transaction.status, "committed")
        self.assertEqual(persisted["snapshot"]["broadcastRevision"], 2)
        self.assertFalse(
            any(
                message["action"].startswith("broadcast.")
                for message in self.get_messages(authority)
            )
        )
        self.assertEqual(self.get_messages(participant), [])
        self.assertEqual(self.get_messages(controller), [])

    def test_terminal_hook_failure_rolls_back_context_and_broadcast(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )["payload"]
        for client in (authority, participant, controller):
            self.get_messages(client)
        before_context = getPlaybackContextState("context-broadcast-source")
        before_broadcast = emo_ws.getPersistentBroadcastState(
            start_ack["broadcastId"]
        )
        before_fences = db.EmoBroadcastFence.select().where(
            db.EmoBroadcastFence.broadcast_id == start_ack["broadcastId"]
        ).count()
        before_deliveries = db.EmoBroadcastDelivery.select().where(
            db.EmoBroadcastDelivery.broadcast_id == start_ack["broadcastId"]
        ).count()
        real_terminal = emo_ws.terminalBroadcastStateInTransaction

        def fail_after_terminal(*args, **kwargs):
            real_terminal(*args, **kwargs)
            raise RuntimeError("injected terminal transaction failure")

        with mock.patch.object(
            emo_ws,
            "terminalBroadcastStateInTransaction",
            side_effect=fail_after_terminal,
        ):
            self.sync_source_queue(
                authority,
                [],
                position_ms=0,
                request_id="source-queue-clear-failure-1",
            )

        error = self.get_error(
            self.get_messages(authority),
            "source-queue-clear-failure-1",
        )
        self.assertEqual(error["payload"]["code"], "internal_error")
        after_broadcast = emo_ws.getPersistentBroadcastState(
            start_ack["broadcastId"]
        )
        self.assertEqual(
            getPlaybackContextState("context-broadcast-source"),
            before_context,
        )
        self.assertEqual(after_broadcast["snapshot"], before_broadcast["snapshot"])
        self.assertEqual(
            db.EmoBroadcastFence.select().where(
                db.EmoBroadcastFence.broadcast_id == start_ack["broadcastId"]
            ).count(),
            before_fences,
        )
        self.assertEqual(
            db.EmoBroadcastDelivery.select().where(
                db.EmoBroadcastDelivery.broadcast_id == start_ack["broadcastId"]
            ).count(),
            before_deliveries,
        )
        self.assertEqual(self.get_messages(participant), [])
        self.assertEqual(self.get_messages(controller), [])


if __name__ == "__main__":
    unittest.main()
