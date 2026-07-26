import time
import unittest
from unittest import mock

from supysonic import db
from supysonic.emo import ws as emo_ws
from supysonic.emo.ws_store import getDevicePlaybackState
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
        self.assertIsInstance(status["serverTimeMs"], int)
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


if __name__ == "__main__":
    unittest.main()
