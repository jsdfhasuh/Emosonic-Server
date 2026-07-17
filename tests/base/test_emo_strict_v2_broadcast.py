import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Tuple
from unittest import mock

from flask_socketio.test_client import SocketIOTestClient

from supysonic.emo import ws as emo_ws
from supysonic.emo.ws import (
    _expire_strict_broadcast_authority_disconnect,
    socketio,
)
from supysonic.emo.ws_state import PlaybackContextConflictError, get_state

from tests.base.test_emo_ws import (
    CAPABILITY_PLAYBACK_CONTEXT_V2,
    EmoWebSocketTestCase,
)


class StrictV2BroadcastTestCase(EmoWebSocketTestCase):
    def setUp(self):
        super().setUp()
        self.broadcast_task_patcher = mock.patch(
            "supysonic.emo.ws.socketio.start_background_task",
            return_value=None,
        )
        self.broadcast_task_patcher.start()

    def tearDown(self):
        try:
            super().tearDown()
        finally:
            self.broadcast_task_patcher.stop()

    def connect_broadcast_devices(
        self,
    ) -> Tuple[
        SocketIOTestClient,
        SocketIOTestClient,
        SocketIOTestClient,
    ]:
        authority = self.connect_device(
            "alice",
            "Alic3",
            "authority-1",
            "device:authority-1",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        participant = self.connect_device(
            "alice",
            "Alic3",
            "participant-1",
            "device:participant-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "playbackPrepare": True,
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
        for client in (authority, participant, controller):
            self.get_messages(client)
        self.ensure_playback_context(
            authority,
            "context-create-broadcast-1",
            playback_context_id="context-broadcast-1",
            device_session_id="device:authority-1",
            queue_song_ids=["context-song-1", "context-song-2"],
            position_ms=900,
            state="playing",
        )
        for client in (authority, participant, controller):
            self.get_messages(client)
        return authority, participant, controller

    def start_strict_broadcast(
        self,
        client: SocketIOTestClient,
        request_id: str = "broadcast-start-1",
        participants: List[str] = None,
        **overrides: object,
    ) -> List[Dict[str, object]]:
        payload = {
            "playbackContextId": "context-broadcast-1",
            "queueSongIds": ["song-2", "song-1"],
            "currentIndex": 0,
            "positionMs": 1200,
            "autoPlay": True,
        }
        if participants is not None:
            payload["participants"] = participants
        payload.update(overrides)
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

    def complete_handoff_to_participant(
        self,
        source: SocketIOTestClient,
        target: SocketIOTestClient,
        controller: SocketIOTestClient,
    ) -> None:
        controller.emit(
            "message",
            {
                "type": "command",
                "action": "playback.handoff.start",
                "requestId": "broadcast-handoff-start",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "targetClientId": "participant-1",
                    "baseControlVersion": 1,
                },
            },
            namespace="/emo",
        )
        start_ack = self.get_ack(
            self.get_messages(controller),
            "broadcast-handoff-start",
        )
        self.get_messages(source)
        prepare = next(
            message
            for message in self.get_messages(target)
            if message["action"] == "playback.prepare"
        )
        target.emit(
            "message",
            {
                "type": "event",
                "action": "playback.ready",
                "requestId": "broadcast-handoff-ready",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "handoffId": start_ack["payload"]["handoffId"],
                    "prepareId": prepare["payload"]["prepareId"],
                    "ready": True,
                },
            },
            namespace="/emo",
        )
        self.get_messages(target)
        self.get_messages(source)
        self.get_messages(controller)
        target.emit(
            "message",
            {
                "type": "event",
                "action": "playback.handoff.complete",
                "requestId": "broadcast-handoff-complete",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "handoffId": start_ack["payload"]["handoffId"],
                    "positionMs": 1500,
                },
            },
            namespace="/emo",
        )
        for client in (source, target, controller):
            self.get_messages(client)

    def test_authority_start_status_and_stop_use_closed_r5_schemas(self):
        authority, participant, _controller = self.connect_broadcast_devices()

        start_messages = self.start_strict_broadcast(
            authority,
            participants=["participant-1"],
        )
        start_ack = self.get_ack(start_messages, "broadcast-start-1")
        authority_start = next(
            message
            for message in start_messages
            if message["action"] == "broadcast.start"
        )
        participant_start = next(
            message
            for message in self.get_messages(participant)
            if message["action"] == "broadcast.start"
        )
        broadcast_id = start_ack["payload"]["broadcastId"]

        self.assertEqual(
            set(start_ack["payload"]),
            {
                "action",
                "started",
                "broadcastId",
                "participants",
                "skippedClientIds",
            },
        )
        self.assertEqual(
            start_ack["payload"]["participants"],
            ["authority-1", "participant-1"],
        )
        self.assertEqual(start_ack["payload"]["skippedClientIds"], [])
        self.assertNotIn("requestId", authority_start)
        self.assertNotIn("targetClientId", authority_start)
        self.assertEqual(authority_start["payload"], participant_start["payload"])
        self.assertEqual(
            set(authority_start["payload"]),
            {
                "playbackContextId",
                "broadcastId",
                "ownerClientId",
                "authorityClientId",
                "queueSongIds",
                "currentIndex",
                "trackId",
                "positionMs",
                "state",
                "version",
                "queueRevision",
                "controlVersion",
                "epoch",
                "serverUpdatedAtMs",
                "playbackRate",
                "participants",
            },
        )
        self.assertEqual(authority_start["payload"]["queueSongIds"], ["song-2", "song-1"])
        self.assertEqual(authority_start["payload"]["authorityClientId"], "authority-1")

        context = get_state().get_playback_context("context-broadcast-1")
        self.assertEqual(context["authorityClientId"], "authority-1")
        self.assertEqual(context["queueSongIds"], ["context-song-1", "context-song-2"])

        retry_messages = self.start_strict_broadcast(
            authority,
            participants=["participant-1"],
        )
        retry_ack = self.get_ack(retry_messages, "broadcast-start-1")
        self.assertEqual([message["action"] for message in retry_messages], ["system.ack"])
        self.assertEqual(retry_ack["payload"]["broadcastId"], broadcast_id)
        self.assertEqual(self.get_messages(participant), [])
        self.assertEqual(len(get_state().list_broadcasts(user_name="alice")), 1)

        authority.emit(
            "message",
            {
                "type": "state",
                "action": "broadcast.status",
                "requestId": "broadcast-status-1",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "broadcastId": broadcast_id,
                },
            },
            namespace="/emo",
        )
        status_messages = self.get_messages(authority)
        status_ack = self.get_ack(status_messages, "broadcast-status-1")

        self.assertEqual([message["action"] for message in status_messages], ["system.ack"])
        self.assertEqual(
            set(status_ack["payload"]),
            {"action", "broadcast", "participantStates"},
        )
        self.assertEqual(
            status_ack["payload"]["participantStates"],
            [
                {
                    "broadcastId": broadcast_id,
                    "clientId": "authority-1",
                    "state": "playing",
                    "positionMs": 1200,
                    "online": True,
                },
                {
                    "broadcastId": broadcast_id,
                    "clientId": "participant-1",
                    "state": "playing",
                    "positionMs": 1200,
                    "online": True,
                },
            ],
        )

        authority.emit(
            "message",
            {
                "type": "command",
                "action": "broadcast.stop",
                "requestId": "broadcast-stop-1",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "broadcastId": broadcast_id,
                },
            },
            namespace="/emo",
        )
        stop_messages = self.get_messages(authority)
        stop_ack = self.get_ack(stop_messages, "broadcast-stop-1")
        authority_stop = next(
            message
            for message in stop_messages
            if message["action"] == "broadcast.stop"
        )
        participant_stop = next(
            message
            for message in self.get_messages(participant)
            if message["action"] == "broadcast.stop"
        )

        self.assertEqual(stop_ack["payload"], {"action": "broadcast.stop"})
        self.assertNotIn("requestId", authority_stop)
        self.assertEqual(authority_stop["payload"], participant_stop["payload"])
        self.assertEqual(authority_stop["payload"]["state"], "stopped")

        authority.emit(
            "message",
            {
                "type": "command",
                "action": "broadcast.stop",
                "requestId": "broadcast-stop-2",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "broadcastId": broadcast_id,
                },
            },
            namespace="/emo",
        )
        repeated_stop = self.get_messages(authority)
        self.assertEqual([message["action"] for message in repeated_stop], ["system.ack"])
        self.assertEqual(
            repeated_stop[0]["payload"],
            {"action": "broadcast.stop"},
        )
        self.assertEqual(self.get_messages(participant), [])

    def test_controller_owner_forces_authority_and_sorts_filtered_participants(self):
        authority, participant, controller = self.connect_broadcast_devices()
        unsupported = self.connect_device(
            "alice",
            "Alic3",
            "unsupported-1",
            "device:unsupported-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "canSeek": False,
            },
        )
        non_player = self.connect_device(
            "alice",
            "Alic3",
            "viewer-1",
            "device:viewer-1",
            ["controller"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        self.get_messages(unsupported)
        self.get_messages(non_player)

        messages = self.start_strict_broadcast(
            controller,
            participants=["viewer-1", "unsupported-1", "participant-1"],
        )
        ack = self.get_ack(messages, "broadcast-start-1")

        self.assertEqual(
            ack["payload"]["participants"],
            ["authority-1", "participant-1"],
        )
        self.assertEqual(
            ack["payload"]["skippedClientIds"],
            ["unsupported-1", "viewer-1"],
        )
        self.assertFalse(
            any(message["action"] == "broadcast.start" for message in messages)
        )
        self.assertTrue(
            any(
                message["action"] == "broadcast.start"
                for message in self.get_messages(authority)
            )
        )
        self.assertTrue(
            any(
                message["action"] == "broadcast.start"
                for message in self.get_messages(participant)
            )
        )
        self.assertEqual(self.get_messages(unsupported), [])
        self.assertEqual(self.get_messages(non_player), [])

    def test_omitted_participants_selects_all_eligible_online_players(self):
        _authority, _participant, controller = self.connect_broadcast_devices()
        extra = self.connect_device(
            "alice",
            "Alic3",
            "extra-1",
            "device:extra-1",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        self.get_messages(extra)

        ack = self.get_ack(
            self.start_strict_broadcast(controller),
            "broadcast-start-1",
        )

        self.assertEqual(
            ack["payload"]["participants"],
            ["authority-1", "extra-1", "participant-1"],
        )
        self.assertEqual(ack["payload"]["skippedClientIds"], [])

    def test_all_requested_participants_can_be_skipped_while_authority_is_forced(self):
        authority, participant, controller = self.connect_broadcast_devices()
        unsupported = self.connect_device(
            "alice",
            "Alic3",
            "unsupported-1",
            "device:unsupported-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "canSeek": False,
            },
        )
        viewer = self.connect_device(
            "alice",
            "Alic3",
            "viewer-1",
            "device:viewer-1",
            ["controller"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        for client in (authority, participant, controller, unsupported, viewer):
            self.get_messages(client)

        ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["viewer-1", "unsupported-1"],
            ),
            "broadcast-start-1",
        )

        self.assertEqual(ack["payload"]["participants"], ["authority-1"])
        self.assertEqual(
            ack["payload"]["skippedClientIds"],
            ["unsupported-1", "viewer-1"],
        )
        self.assertTrue(
            any(
                message["action"] == "broadcast.start"
                for message in self.get_messages(authority)
            )
        )
        for client in (participant, unsupported, viewer):
            self.assertFalse(
                any(
                    message["action"] == "broadcast.start"
                    for message in self.get_messages(client)
                )
            )

    def test_multi_participant_pushes_are_sorted_and_use_recipient_provenance(self):
        authority, participant, controller = self.connect_broadcast_devices()
        extra = self.connect_device(
            "alice",
            "Alic3",
            "extra-1",
            "device:extra-1",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        for client in (authority, participant, controller, extra):
            self.get_messages(client)

        with mock.patch.object(socketio, "emit", wraps=socketio.emit) as emit:
            ack = self.get_ack(
                self.start_strict_broadcast(
                    controller,
                    participants=["participant-1", "extra-1"],
                ),
                "broadcast-start-1",
            )

        participant_ids = ["authority-1", "extra-1", "participant-1"]
        expected_sids = [
            get_state().get_sid_for_client(client_id, user_name="alice")
            for client_id in participant_ids
        ]
        start_calls = [
            call
            for call in emit.call_args_list
            if call.args[1]["action"] == "broadcast.start"
        ]
        self.assertEqual(
            [call.kwargs["to"] for call in start_calls],
            expected_sids,
        )
        self.assertEqual(ack["payload"]["participants"], participant_ids)

        outgoing_messages = [call.args[1] for call in start_calls]
        self.assertEqual(
            [message["payload"] for message in outgoing_messages],
            [outgoing_messages[0]["payload"]] * 3,
        )
        expected_nonces = [
            get_state().get_session(sid)["connectionNonce"]
            for sid in expected_sids
        ]
        self.assertEqual(
            [message["connectionNonce"] for message in outgoing_messages],
            expected_nonces,
        )
        self.assertEqual(len(set(expected_nonces)), 3)
        self.assertEqual(
            [message["connectionEpoch"] for message in outgoing_messages],
            [1, 1, 1],
        )
        self.assertTrue(
            all("targetClientId" not in message for message in outgoing_messages)
        )

    def test_partial_start_push_failure_keeps_canonical_broadcast_recoverable(self):
        authority, participant, controller = self.connect_broadcast_devices()
        for client in (authority, participant, controller):
            self.get_messages(client)
        participant_sid = get_state().get_sid_for_client(
            "participant-1",
            user_name="alice",
        )
        real_emit = socketio.emit
        delivery_attempts = []

        def fail_second_participant(event, message, **kwargs):
            if message["action"] == "broadcast.start":
                delivery_attempts.append(kwargs["to"])
                if kwargs["to"] == participant_sid:
                    raise RuntimeError("injected participant emit failure")
            return real_emit(event, message, **kwargs)

        with mock.patch.object(
            socketio,
            "emit",
            side_effect=fail_second_participant,
        ):
            controller_messages = self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            )

        start_ack = self.get_ack(controller_messages, "broadcast-start-1")
        broadcast_id = start_ack["payload"]["broadcastId"]
        authority_sid = get_state().get_sid_for_client(
            "authority-1",
            user_name="alice",
        )
        self.assertEqual(delivery_attempts, [authority_sid, participant_sid])
        self.assertTrue(
            any(
                message["action"] == "broadcast.start"
                for message in self.get_messages(authority)
            )
        )
        self.assertEqual(self.get_messages(participant), [])

        persisted = get_state().get_broadcast(broadcast_id)
        self.assertTrue(get_state().is_broadcast_active(broadcast_id))
        self.assertEqual(
            persisted["participants"],
            ["authority-1", "participant-1"],
        )
        self.assertEqual(persisted["version"], 1)
        self.assertEqual(persisted["controlVersion"], 1)

        participant.emit(
            "message",
            {
                "type": "state",
                "action": "broadcast.status",
                "requestId": "broadcast-status-after-push-failure",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "broadcastId": broadcast_id,
                },
            },
            namespace="/emo",
        )
        status_ack = self.get_ack(
            self.get_messages(participant),
            "broadcast-status-after-push-failure",
        )
        self.assertEqual(
            status_ack["payload"]["broadcast"]["broadcastId"],
            broadcast_id,
        )
        self.assertEqual(
            status_ack["payload"]["broadcast"]["participants"],
            ["authority-1", "participant-1"],
        )

    def test_cross_user_target_and_authority_capability_are_closed_failures(self):
        authority, _participant, controller = self.connect_broadcast_devices()
        bob = self.connect_device(
            "bob",
            "B0b",
            "bob-player-1",
            "device:bob-player-1",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        self.get_messages(bob)

        cross_user = self.start_strict_broadcast(
            controller,
            request_id="broadcast-cross-user",
            participants=["bob-player-1"],
        )
        cross_user_error = self.get_error(cross_user, "broadcast-cross-user")
        self.assertEqual(cross_user_error["payload"]["code"], "forbidden")
        self.assertEqual(get_state().list_broadcasts(user_name="alice"), [])

        limited_authority = self.connect_device(
            "alice",
            "Alic3",
            "authority-1",
            "device:authority-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "canPause": False,
            },
        )
        self.clients.remove(authority)
        self.get_messages(limited_authority)
        capability = self.start_strict_broadcast(
            limited_authority,
            request_id="broadcast-authority-capability",
            participants=["authority-1"],
        )
        capability_error = self.get_error(
            capability,
            "broadcast-authority-capability",
        )
        self.assertEqual(capability_error["payload"]["code"], "capability_required")
        self.assertEqual(get_state().list_broadcasts(user_name="alice"), [])

    def test_control_actions_use_action_only_ack_and_canonical_cursor_matrix(self):
        authority, participant, _controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                authority,
                participants=["participant-1"],
                autoPlay=False,
            ),
            "broadcast-start-1",
        )
        broadcast_id = start_ack["payload"]["broadcastId"]
        self.get_messages(authority)
        participant_start = next(
            message
            for message in self.get_messages(participant)
            if message["action"] == "broadcast.start"
        )
        self.assertEqual(participant_start["payload"]["state"], "paused")
        self.assertTrue(get_state().is_broadcast_active(broadcast_id))

        actions = [
            ("broadcast.play", {}, "command", (2, 1, 2, "playing", 0)),
            (
                "broadcast.seek",
                {"positionMs": 3200},
                "command",
                (3, 1, 3, "playing", 0),
            ),
            ("broadcast.pause", {}, "command", (4, 1, 4, "paused", 0)),
            (
                "broadcast.playItem",
                {"queueIndex": 1},
                "command",
                (5, 2, 5, "playing", 1),
            ),
            (
                "broadcast.queue.sync",
                {
                    "queueSongIds": ["song-3", "song-2", "song-1"],
                    "currentIndex": 0,
                    "positionMs": 500,
                    "baseQueueRevision": 2,
                    "baseControlVersion": 5,
                },
                "state",
                (6, 3, 6, "playing", 0),
            ),
        ]

        for index, (action, extra, message_type, expected) in enumerate(actions, 1):
            request_id = "broadcast-control-%d" % index
            payload = {
                "playbackContextId": "context-broadcast-1",
                "broadcastId": broadcast_id,
            }
            payload.update(extra)
            authority.emit(
                "message",
                {
                    "type": message_type,
                    "action": action,
                    "requestId": request_id,
                    "payload": payload,
                },
                namespace="/emo",
            )
            authority_messages = self.get_messages(authority)
            ack = self.get_ack(authority_messages, request_id)
            push = next(
                message
                for message in authority_messages
                if message["action"] == action
            )
            participant_push = next(
                message
                for message in self.get_messages(participant)
                if message["action"] == action
            )

            self.assertEqual(ack["payload"], {"action": action})
            self.assertNotIn("requestId", push)
            self.assertEqual(push["payload"], participant_push["payload"])
            self.assertEqual(
                (
                    push["payload"]["version"],
                    push["payload"]["queueRevision"],
                    push["payload"]["controlVersion"],
                    push["payload"]["state"],
                    push["payload"]["currentIndex"],
                ),
                expected,
            )
            if action in {
                "broadcast.play",
                "broadcast.seek",
                "broadcast.pause",
                "broadcast.playItem",
            }:
                self.assertGreater(push["payload"]["effectiveAtServerMs"], 0)
                self.assertGreater(push["payload"]["serverTimeMs"], 0)
            else:
                self.assertNotIn("effectiveAtServerMs", push["payload"])
                self.assertNotIn("serverTimeMs", push["payload"])

        authority.emit(
            "message",
            {
                "type": "command",
                "action": "broadcast.stop",
                "requestId": "broadcast-control-stop",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "broadcastId": broadcast_id,
                },
            },
            namespace="/emo",
        )
        stop_messages = self.get_messages(authority)
        stop = next(
            message
            for message in stop_messages
            if message["action"] == "broadcast.stop"
        )
        self.assertEqual(stop["payload"]["version"], 7)
        self.assertEqual(stop["payload"]["queueRevision"], 3)
        self.assertEqual(stop["payload"]["controlVersion"], 6)
        self.get_messages(participant)

        authority.emit(
            "message",
            {
                "type": "command",
                "action": "broadcast.play",
                "requestId": "broadcast-control-after-stop",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "broadcastId": broadcast_id,
                },
            },
            namespace="/emo",
        )
        stopped_error = self.get_error(
            self.get_messages(authority),
            "broadcast-control-after-stop",
        )
        self.assertEqual(stopped_error["payload"]["code"], "conflict")

    def test_queue_sync_requires_conditional_base_cursors_and_reports_stale_version(self):
        authority, _participant, _controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                authority,
                participants=["authority-1"],
                autoPlay=False,
            ),
            "broadcast-start-1",
        )
        broadcast_id = start_ack["payload"]["broadcastId"]
        self.get_messages(authority)

        cases = (
            (
                "broadcast-queue-missing-queue-base",
                {
                    "queueSongIds": ["song-3", "song-1"],
                    "currentIndex": 0,
                    "positionMs": 1200,
                },
                "bad_request",
            ),
            (
                "broadcast-queue-missing-control-base",
                {
                    "queueSongIds": ["song-2", "song-1"],
                    "currentIndex": 0,
                    "positionMs": 1300,
                },
                "bad_request",
            ),
            (
                "broadcast-queue-stale-queue-base",
                {
                    "queueSongIds": ["song-3", "song-1"],
                    "currentIndex": 0,
                    "positionMs": 1200,
                    "baseQueueRevision": 0,
                    "baseControlVersion": 1,
                },
                "stale_version",
            ),
            (
                "broadcast-queue-stale-control-base",
                {
                    "queueSongIds": ["song-2", "song-1"],
                    "currentIndex": 0,
                    "positionMs": 1300,
                    "baseControlVersion": 0,
                },
                "stale_version",
            ),
        )
        for request_id, extra, error_code in cases:
            payload = {
                "playbackContextId": "context-broadcast-1",
                "broadcastId": broadcast_id,
            }
            payload.update(extra)
            authority.emit(
                "message",
                {
                    "type": "state",
                    "action": "broadcast.queue.sync",
                    "requestId": request_id,
                    "payload": payload,
                },
                namespace="/emo",
            )
            error = self.get_error(self.get_messages(authority), request_id)
            self.assertEqual(error["payload"]["code"], error_code)

        broadcast = get_state().get_broadcast(broadcast_id)
        self.assertEqual(broadcast["version"], 1)
        self.assertEqual(broadcast["queueRevision"], 1)
        self.assertEqual(broadcast["controlVersion"], 1)

    def test_non_authority_feedback_is_rejected_and_participant_cannot_control(self):
        authority, participant, _controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                authority,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )
        broadcast_id = start_ack["payload"]["broadcastId"]
        self.get_messages(authority)
        self.get_messages(participant)

        participant.emit(
            "message",
            {
                "type": "event",
                "action": "playback.update",
                "requestId": "broadcast-feedback-1",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "deviceSessionId": "device:participant-1",
                    "origin": "passive",
                    "appliedControlVersion": 1,
                    "state": "playing",
                    "trackId": "context-song-1",
                    "positionMs": 1500,
                    "clientSeq": 1,
                },
            },
            namespace="/emo",
        )
        feedback_messages = self.get_messages(participant)
        feedback_error = self.get_error(
            feedback_messages,
            "broadcast-feedback-1",
        )
        self.assertEqual(feedback_error["payload"]["code"], "forbidden")

        participant.emit(
            "message",
            {
                "type": "state",
                "action": "broadcast.status",
                "requestId": "broadcast-feedback-status-1",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "broadcastId": broadcast_id,
                },
            },
            namespace="/emo",
        )
        status = self.get_ack(
            self.get_messages(participant),
            "broadcast-feedback-status-1",
        )
        participant_state = next(
            item
            for item in status["payload"]["participantStates"]
            if item["clientId"] == "participant-1"
        )
        self.assertNotIn("clientSeq", participant_state)
        self.assertNotIn("serverUpdatedAtMs", participant_state)

        authority.emit(
            "message",
            {
                "type": "event",
                "action": "playback.update",
                "requestId": "broadcast-authority-feedback-1",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "deviceSessionId": "device:authority-1",
                    "origin": "passive",
                    "appliedControlVersion": 1,
                    "state": "playing",
                    "trackId": "context-song-1",
                    "positionMs": 1600,
                    "clientSeq": 1,
                },
            },
            namespace="/emo",
        )
        self.assertFalse(
            any(
                message["action"] == "system.ack"
                for message in self.get_messages(authority)
            )
        )
        self.get_messages(participant)

        participant.emit(
            "message",
            {
                "type": "state",
                "action": "broadcast.status",
                "requestId": "broadcast-all-feedback-status-1",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "broadcastId": broadcast_id,
                },
            },
            namespace="/emo",
        )
        all_feedback_status = self.get_ack(
            self.get_messages(participant),
            "broadcast-all-feedback-status-1",
        )
        all_participant_states = all_feedback_status["payload"][
            "participantStates"
        ]
        self.assertEqual(
            [item["clientId"] for item in all_participant_states],
            ["authority-1", "participant-1"],
        )
        authority_state = all_participant_states[0]
        self.assertNotIn("clientSeq", authority_state)
        self.assertNotIn("serverUpdatedAtMs", authority_state)

        participant.emit(
            "message",
            {
                "type": "command",
                "action": "broadcast.pause",
                "requestId": "broadcast-participant-pause-1",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "broadcastId": broadcast_id,
                },
            },
            namespace="/emo",
        )
        error = self.get_error(
            self.get_messages(participant),
            "broadcast-participant-pause-1",
        )
        self.assertEqual(error["payload"]["code"], "forbidden")

    def test_authority_disconnect_pauses_and_same_device_reconnect_cancels_timeout(self):
        authority, participant, _controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                authority,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )
        broadcast_id = start_ack["payload"]["broadcastId"]
        self.get_messages(authority)
        self.get_messages(participant)

        authority.disconnect(namespace="/emo")
        self.clients.remove(authority)
        pause = next(
            message
            for message in self.get_messages(participant)
            if message["action"] == "broadcast.pause"
        )
        suspended = get_state().get_broadcast(broadcast_id)

        self.assertEqual(pause["payload"]["state"], "paused")
        self.assertEqual(pause["payload"]["version"], 2)
        self.assertEqual(pause["payload"]["controlVersion"], 2)
        self.assertGreater(suspended["authorityDisconnectDeadlineMs"], 0)
        deadline_ms = suspended["authorityDisconnectDeadlineMs"]

        wrong_device = self.connect_device(
            "alice",
            "Alic3",
            "authority-1",
            "device:wrong-authority-1",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        self.get_messages(wrong_device)
        self.assertEqual(
            get_state().get_broadcast(broadcast_id)[
                "authorityDisconnectDeadlineMs"
            ],
            deadline_ms,
        )

        reconnected = self.connect_device(
            "alice",
            "Alic3",
            "authority-1",
            "device:authority-1",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        self.clients.remove(wrong_device)
        self.get_messages(reconnected)
        resumed = get_state().get_broadcast(broadcast_id)

        self.assertNotIn("authorityDisconnectDeadlineMs", resumed)
        self.assertEqual(resumed["state"], "paused")
        self.assertIsNone(
            _expire_strict_broadcast_authority_disconnect(
                broadcast_id,
                deadline_ms,
                now=(deadline_ms + 1) / 1000,
            )
        )
        self.assertTrue(get_state().is_broadcast_active(broadcast_id))
        self.assertFalse(
            any(
                message["action"] == "broadcast.stop"
                for message in self.get_messages(participant)
            )
        )

    def test_controller_owner_disconnect_does_not_stop_broadcast(self):
        authority, participant, controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )
        broadcast_id = start_ack["payload"]["broadcastId"]
        self.get_messages(authority)
        self.get_messages(participant)

        controller.disconnect(namespace="/emo")
        self.clients.remove(controller)

        broadcast = get_state().get_broadcast(broadcast_id)
        self.assertTrue(get_state().is_broadcast_active(broadcast_id))
        self.assertEqual(broadcast["ownerClientId"], "controller-1")
        self.assertEqual(broadcast["authorityClientId"], "authority-1")
        authority_disconnect_messages = self.get_messages(authority)
        self.assertFalse(
            any(
                message["action"] in {"broadcast.pause", "broadcast.stop"}
                for message in authority_disconnect_messages
            )
        )

        authority.emit(
            "message",
            {
                "type": "command",
                "action": "broadcast.pause",
                "requestId": "broadcast-pause-after-owner-disconnect",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "broadcastId": broadcast_id,
                },
            },
            namespace="/emo",
        )
        authority_messages = self.get_messages(authority)

        self.get_ack(
            authority_messages,
            "broadcast-pause-after-owner-disconnect",
        )
        paused = get_state().get_broadcast(broadcast_id)
        self.assertEqual(paused["state"], "paused")
        self.assertTrue(get_state().is_broadcast_active(broadcast_id))

    def test_authority_disconnect_timeout_and_restart_stop_freeze_control_cursor(self):
        authority, participant, _controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                authority,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )
        broadcast_id = start_ack["payload"]["broadcastId"]
        self.get_messages(authority)
        self.get_messages(participant)

        authority.disconnect(namespace="/emo")
        self.clients.remove(authority)
        self.get_messages(participant)
        suspended = get_state().get_broadcast(broadcast_id)
        deadline_ms = suspended["authorityDisconnectDeadlineMs"]
        stopped = _expire_strict_broadcast_authority_disconnect(
            broadcast_id,
            deadline_ms,
            now=(deadline_ms + 1) / 1000,
        )
        stop_push = next(
            message
            for message in self.get_messages(participant)
            if message["action"] == "broadcast.stop"
        )

        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(stopped["version"], 3)
        self.assertEqual(stopped["controlVersion"], 2)
        self.assertEqual(stop_push["payload"]["version"], 3)
        self.assertFalse(get_state().is_broadcast_active(broadcast_id))

        second_authority = self.connect_device(
            "alice",
            "Alic3",
            "authority-2",
            "device:authority-2",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        self.get_messages(second_authority)
        self.ensure_playback_context(
            second_authority,
            "context-create-broadcast-2",
            playback_context_id="context-broadcast-2",
            device_session_id="device:authority-2",
            queue_song_ids=["song-1"],
        )
        self.get_messages(second_authority)
        second_ack = self.get_ack(
            self.start_strict_broadcast(
                second_authority,
                request_id="broadcast-start-2",
                participants=["authority-2"],
                playbackContextId="context-broadcast-2",
            ),
            "broadcast-start-2",
        )
        second_broadcast_id = second_ack["payload"]["broadcastId"]
        self.get_messages(second_authority)

        restarted = get_state().stop_active_broadcasts_for_restart()
        restarted_broadcast = next(
            item
            for item in restarted
            if item["broadcastId"] == second_broadcast_id
        )
        self.assertEqual(restarted_broadcast["state"], "stopped")
        self.assertEqual(restarted_broadcast["version"], 2)
        self.assertEqual(restarted_broadcast["controlVersion"], 1)
        self.assertFalse(get_state().is_broadcast_active(second_broadcast_id))

    def test_authority_disconnect_live_background_timer_stops_broadcast(self):
        authority, participant, _controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                authority,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )
        broadcast_id = start_ack["payload"]["broadcastId"]
        self.get_messages(authority)
        self.get_messages(participant)

        self.broadcast_task_patcher.stop()
        try:
            real_start_background_task = socketio.start_background_task
            tasks = []

            def start_background_task(target, *args, **kwargs):
                task = real_start_background_task(target, *args, **kwargs)
                tasks.append(task)
                return task

            with mock.patch(
                "supysonic.emo.ws.BROADCAST_AUTHORITY_DISCONNECT_TIMEOUT_MS",
                5,
            ), mock.patch(
                "supysonic.emo.ws.socketio.start_background_task",
                side_effect=start_background_task,
            ):
                authority.disconnect(namespace="/emo")
                self.clients.remove(authority)
                self.assertEqual(len(tasks), 1)
                tasks[0].join(timeout=2)
        finally:
            self.broadcast_task_patcher.start()

        participant_messages = self.get_messages(participant)
        self.assertEqual(
            [
                message["action"]
                for message in participant_messages
                if message["action"] in {"broadcast.pause", "broadcast.stop"}
            ],
            ["broadcast.pause", "broadcast.stop"],
        )
        stopped = get_state().get_broadcast(broadcast_id)
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(stopped["version"], 3)
        self.assertEqual(stopped["controlVersion"], 2)
        self.assertNotIn("authorityDisconnectDeadlineMs", stopped)
        self.assertFalse(get_state().is_broadcast_active(broadcast_id))

    def test_pause_push_failure_still_schedules_authority_timeout(self):
        authority, participant, _controller = self.connect_broadcast_devices()
        start_ack = self.get_ack(
            self.start_strict_broadcast(
                authority,
                participants=["participant-1"],
            ),
            "broadcast-start-1",
        )
        broadcast_id = start_ack["payload"]["broadcastId"]
        self.get_messages(authority)
        self.get_messages(participant)
        background_task = socketio.start_background_task
        background_task.reset_mock()
        real_emit = emo_ws._emit_strict_broadcast_to_participants

        def fail_pause(broadcast, action, message_type):
            if action == "broadcast.pause":
                raise RuntimeError("injected pause push failure")
            return real_emit(broadcast, action, message_type)

        with mock.patch(
            "supysonic.emo.ws._emit_strict_broadcast_to_participants",
            side_effect=fail_pause,
        ):
            authority.disconnect(namespace="/emo")
        self.clients.remove(authority)

        background_task.assert_called_once()
        task_args = background_task.call_args.args
        self.assertEqual(task_args[1], broadcast_id)
        deadline_ms = task_args[2]
        suspended = get_state().get_broadcast(broadcast_id)
        self.assertEqual(
            suspended["authorityDisconnectDeadlineMs"],
            deadline_ms,
        )
        self.assertEqual(
            [
                message["action"]
                for message in self.get_messages(participant)
            ],
            [],
        )

        stopped = _expire_strict_broadcast_authority_disconnect(
            broadcast_id,
            deadline_ms,
            now=(deadline_ms + 1) / 1000,
        )
        participant_messages = self.get_messages(participant)
        self.assertEqual(
            [message["action"] for message in participant_messages],
            ["broadcast.stop"],
        )
        self.assertEqual(stopped["state"], "stopped")
        self.assertFalse(get_state().is_broadcast_active(broadcast_id))

    def test_handoff_authority_is_canonical_for_subsequent_broadcast(self):
        source, target, controller = self.connect_broadcast_devices()
        self.complete_handoff_to_participant(source, target, controller)
        context = get_state().get_playback_context("context-broadcast-1")
        self.assertEqual(context["authorityClientId"], "participant-1")
        self.assertEqual(
            context["authorityDeviceSessionId"],
            "device:participant-1",
        )

        source_start = self.start_strict_broadcast(
            source,
            request_id="broadcast-old-authority-start",
            participants=["authority-1"],
        )
        source_error = self.get_error(
            source_start,
            "broadcast-old-authority-start",
        )
        self.assertEqual(source_error["payload"]["code"], "forbidden")
        self.assertEqual(get_state().list_broadcasts(user_name="alice"), [])

        start_ack = self.get_ack(
            self.start_strict_broadcast(
                controller,
                request_id="broadcast-controller-after-handoff",
                participants=["authority-1"],
            ),
            "broadcast-controller-after-handoff",
        )
        broadcast_id = start_ack["payload"]["broadcastId"]
        self.assertEqual(
            start_ack["payload"]["participants"],
            ["authority-1", "participant-1"],
        )
        source_start_push = next(
            message
            for message in self.get_messages(source)
            if message["action"] == "broadcast.start"
        )
        target_start_push = next(
            message
            for message in self.get_messages(target)
            if message["action"] == "broadcast.start"
        )
        expected_identity = {
            "ownerClientId": "controller-1",
            "authorityClientId": "participant-1",
            "participants": ["authority-1", "participant-1"],
        }
        for message in (source_start_push, target_start_push):
            self.assertEqual(
                {
                    field: message["payload"][field]
                    for field in expected_identity
                },
                expected_identity,
            )
        broadcast = get_state().get_broadcast(broadcast_id)
        self.assertEqual(broadcast["ownerClientId"], "controller-1")
        self.assertEqual(broadcast["authorityClientId"], "participant-1")

        source.emit(
            "message",
            {
                "type": "command",
                "action": "broadcast.pause",
                "requestId": "broadcast-old-authority-control",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "broadcastId": broadcast_id,
                },
            },
            namespace="/emo",
        )
        source_control_error = self.get_error(
            self.get_messages(source),
            "broadcast-old-authority-control",
        )
        self.assertEqual(source_control_error["payload"]["code"], "forbidden")
        self.assertFalse(
            any(
                message["action"] == "broadcast.pause"
                for message in self.get_messages(target)
            )
        )

        target.emit(
            "message",
            {
                "type": "command",
                "action": "broadcast.pause",
                "requestId": "broadcast-new-authority-control",
                "payload": {
                    "playbackContextId": "context-broadcast-1",
                    "broadcastId": broadcast_id,
                },
            },
            namespace="/emo",
        )
        target_messages = self.get_messages(target)
        self.get_ack(target_messages, "broadcast-new-authority-control")
        target_pause = next(
            message
            for message in target_messages
            if message["action"] == "broadcast.pause"
        )
        self.assertEqual(
            target_pause["payload"]["authorityClientId"],
            "participant-1",
        )

    def test_simultaneous_start_has_one_atomic_winner_per_context(self):
        self.connect_broadcast_devices()
        broadcast_state = get_state()

        def create(candidate):
            try:
                return broadcast_state.create_broadcast(
                    "broadcast-race-%d" % candidate,
                    "alice",
                    "authority-1",
                    ["authority-1"],
                    ["song-1"],
                    playback_context_id="context-broadcast-1",
                    authority_client_id="authority-1",
                    require_context_available=True,
                )
            except PlaybackContextConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create, (1, 2)))

        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(
            len(
                [
                    broadcast
                    for broadcast in broadcast_state.list_broadcasts(
                        user_name="alice"
                    )
                    if broadcast.get("playbackContextId")
                    == "context-broadcast-1"
                ]
            ),
            1,
        )


def load_tests(loader, standard_tests, pattern):
    del loader, standard_tests, pattern
    suite = unittest.TestSuite()
    for test_name in sorted(
        name
        for name in StrictV2BroadcastTestCase.__dict__
        if name.startswith("test_")
    ):
        suite.addTest(StrictV2BroadcastTestCase(test_name))
    return suite


if __name__ == "__main__":
    unittest.main()
