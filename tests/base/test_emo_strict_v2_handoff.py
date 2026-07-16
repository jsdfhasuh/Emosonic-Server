import time
import unittest
from typing import Dict, List, Tuple
from unittest import mock

from flask_socketio.test_client import SocketIOTestClient

from supysonic.emo.ws import (
    _expire_handoff_complete,
    _expire_prepare,
    socketio,
    strict_request_cache,
)
from supysonic.emo.ws_store import (
    failActivePlaybackHandoffsForRestart,
    getDevicePlaybackStates,
    getPlaybackContextState,
    getPlaybackHandoff,
    listPlaybackContexts,
    saveQueueState,
)
from supysonic.emo.ws_state import get_state

from tests.base.test_emo_ws import (
    CAPABILITY_PLAYBACK_CONTEXT_V2,
    EmoWebSocketTestCase,
)


class StrictV2HandoffTestCase(EmoWebSocketTestCase):
    def connect_handoff_devices(
        self,
    ) -> Tuple[SocketIOTestClient, SocketIOTestClient, SocketIOTestClient]:
        source = self.connect_device(
            "alice",
            "Alic3",
            "source-1",
            "device:source-1",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        target = self.connect_device(
            "alice",
            "Alic3",
            "target-1",
            "device:target-1",
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
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        for client in (source, target, controller):
            self.get_messages(client)
        self.create_playback_context(
            source,
            "context-create-handoff-1",
            playback_context_id="context-handoff-1",
            device_session_id="device:source-1",
            queue_song_ids=["song-1", "song-2"],
            position_ms=1200,
            state="playing",
        )
        for client in (source, target, controller):
            self.get_messages(client)
        return source, target, controller

    def start_handoff(
        self,
        controller: SocketIOTestClient,
        request_id: str = "handoff-start-1",
        target_client_id: str = "target-1",
    ) -> List[Dict[str, object]]:
        controller.emit(
            "message",
            {
                "type": "command",
                "action": "playback.handoff.start",
                "requestId": request_id,
                "payload": {
                    "playbackContextId": "context-handoff-1",
                    "targetClientId": target_client_id,
                    "baseControlVersion": 1,
                },
            },
            namespace="/emo",
        )
        return self.get_messages(controller)

    def test_success_uses_strict_settlement_schema_and_atomic_authority_switch(self):
        source, target, controller = self.connect_handoff_devices()

        start_messages = self.start_handoff(controller)
        start_ack = self.get_ack(start_messages, "handoff-start-1")
        target_messages = self.get_messages(target)
        prepare = next(
            message
            for message in target_messages
            if message["action"] == "playback.prepare"
        )

        self.assertEqual(
            set(start_ack["payload"]),
            {"action", "handoffId", "prepareId", "status", "controlVersion"},
        )
        self.assertEqual(start_ack["payload"]["status"], "preparing")
        self.assertEqual(start_ack["payload"]["controlVersion"], 2)
        self.assertNotIn("requestId", prepare)
        self.assertEqual(
            set(prepare["payload"]),
            {
                "playbackContextId",
                "handoffId",
                "prepareId",
                "sourceClientId",
                "authorityClientId",
                "deviceSessionId",
                "queueSongIds",
                "currentIndex",
                "trackId",
                "positionMs",
                "controlVersion",
                "timelineId",
            },
        )
        self.assertEqual(prepare["payload"]["deviceSessionId"], "device:target-1")
        self.assertFalse(
            any(message["action"] == "playback.prepare" for message in self.get_messages(source))
        )

        ready_started_at_ms = int(time.time() * 1000)
        target.emit(
            "message",
            {
                "type": "event",
                "action": "playback.ready",
                "requestId": "handoff-ready-1",
                "payload": {
                    "playbackContextId": "context-handoff-1",
                    "handoffId": start_ack["payload"]["handoffId"],
                    "prepareId": start_ack["payload"]["prepareId"],
                    "ready": True,
                },
            },
            namespace="/emo",
        )
        ready_messages = self.get_messages(target)
        commit = next(
            message
            for message in ready_messages
            if message["action"] == "player.play"
        )
        committing = next(
            message
            for message in ready_messages
            if message["action"] == "playback.handoff.status"
        )

        self.assertFalse(
            any(message["action"] == "system.ack" for message in ready_messages)
        )
        self.assertEqual(
            set(commit["payload"]),
            {
                "playbackContextId",
                "handoffId",
                "controlVersion",
                "sourceClientId",
                "effectiveAtServerMs",
                "positionMs",
            },
        )
        self.assertGreaterEqual(
            commit["payload"]["effectiveAtServerMs"] - ready_started_at_ms,
            250,
        )
        self.assertEqual(committing["payload"]["status"], "committing")
        self.get_messages(source)

        target.emit(
            "message",
            {
                "type": "event",
                "action": "playback.handoff.complete",
                "requestId": "handoff-complete-1",
                "payload": {
                    "playbackContextId": "context-handoff-1",
                    "handoffId": start_ack["payload"]["handoffId"],
                    "positionMs": 1500,
                },
            },
            namespace="/emo",
        )
        target_complete_messages = self.get_messages(target)
        source_complete_messages = self.get_messages(source)
        controller_complete_messages = self.get_messages(controller)

        self.assertFalse(
            any(
                message["action"] == "system.ack"
                for message in target_complete_messages
            )
        )
        self.assertEqual(
            [message["action"] for message in source_complete_messages],
            [
                "playback.handoff.status",
                "playback.context.status",
                "playback.handoff.release",
            ],
        )
        release = source_complete_messages[-1]
        self.assertEqual(
            release["payload"],
            {
                "playbackContextId": "context-handoff-1",
                "handoffId": start_ack["payload"]["handoffId"],
                "instruction": "pause",
                "controlVersion": 2,
                "newAuthorityClientId": "target-1",
            },
        )
        binding_events = [
            message
            for message in controller_complete_messages
            if message.get("action")
            == "playback.context.bindings.changed"
        ]
        self.assertEqual(
            [message["payload"] for message in binding_events],
            [
                {
                    "authorityClientId": "source-1",
                    "authorityDeviceSessionId": "device:source-1",
                },
                {
                    "authorityClientId": "target-1",
                    "authorityDeviceSessionId": "device:target-1",
                },
            ],
        )

        context = getPlaybackContextState("context-handoff-1")
        handoff = getPlaybackHandoff(start_ack["payload"]["handoffId"])
        self.assertEqual(context["authorityClientId"], "target-1")
        self.assertEqual(
            context["authorityDeviceSessionId"],
            "device:target-1",
        )
        self.assertEqual(context["positionMs"], 1500)
        self.assertEqual(context["controlVersion"], 2)
        self.assertEqual(context["version"], 2)
        self.assertEqual(context["epoch"], 2)
        self.assertEqual(handoff["status"], "completed")
        self.assertEqual(handoff["originClientId"], "controller-1")
        target_device_state = next(
            device_state
            for device_state in getDevicePlaybackStates("context-handoff-1")
            if device_state["sourceClientId"] == "target-1"
        )
        self.assertTrue(target_device_state["isAuthority"])

        target.emit(
            "message",
            {
                "type": "event",
                "action": "playback.handoff.complete",
                "requestId": "handoff-complete-1",
                "payload": {
                    "playbackContextId": "context-handoff-1",
                    "handoffId": start_ack["payload"]["handoffId"],
                    "positionMs": 1500,
                },
            },
            namespace="/emo",
        )
        replay_messages = self.get_messages(target)
        self.assertEqual(
            [message["action"] for message in replay_messages],
            ["playback.handoff.status", "playback.context.status"],
        )
        self.assertEqual(self.get_messages(source), [])
        self.assertFalse(
            any(
                message.get("action")
                == "playback.context.bindings.changed"
                for message in self.get_messages(controller)
            )
        )
        replayed_context = getPlaybackContextState("context-handoff-1")
        self.assertEqual(replayed_context["version"], 2)
        self.assertEqual(replayed_context["epoch"], 2)

    def test_complete_push_failure_preserves_commit_and_replays_confirmation(self):
        source, target, controller = self.connect_handoff_devices()
        start_ack = self.get_ack(
            self.start_handoff(controller),
            "handoff-start-1",
        )
        prepare = next(
            message
            for message in self.get_messages(target)
            if message["action"] == "playback.prepare"
        )
        self.get_messages(source)
        target.emit(
            "message",
            {
                "type": "event",
                "action": "playback.ready",
                "requestId": "handoff-ready-before-push-failure",
                "payload": {
                    "playbackContextId": "context-handoff-1",
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
        complete_request = {
            "type": "event",
            "action": "playback.handoff.complete",
            "requestId": "handoff-complete-push-failure",
            "payload": {
                "playbackContextId": "context-handoff-1",
                "handoffId": start_ack["payload"]["handoffId"],
                "positionMs": 1800,
            },
        }
        settled_before_push = []

        def fail_after_settlement(*_args, **_kwargs):
            settled_before_push.append(
                any(
                    cached_request_id == complete_request["requestId"]
                    and entry.result is not None
                    for (_nonce, cached_request_id), entry
                    in strict_request_cache._entries.items()
                )
            )
            raise RuntimeError("injected handoff status emit failure")

        with mock.patch(
            "supysonic.emo.ws._broadcast_handoff_status",
            side_effect=fail_after_settlement,
        ):
            target.emit("message", complete_request, namespace="/emo")

        self.assertEqual(settled_before_push, [True])
        first_target_messages = self.get_messages(target)
        self.assertFalse(
            any(
                message["action"] == "system.error"
                for message in first_target_messages
            )
        )
        self.assertTrue(
            any(
                message["action"] == "playback.context.status"
                for message in first_target_messages
            )
        )
        source_messages = self.get_messages(source)
        self.assertEqual(
            [message["action"] for message in source_messages],
            ["playback.context.status", "playback.handoff.release"],
        )
        context = getPlaybackContextState("context-handoff-1")
        handoff = getPlaybackHandoff(start_ack["payload"]["handoffId"])
        self.assertEqual(context["authorityClientId"], "target-1")
        self.assertEqual(context["positionMs"], 1800)
        self.assertEqual(context["version"], 2)
        self.assertEqual(handoff["status"], "completed")

        target.emit("message", complete_request, namespace="/emo")
        replay_messages = self.get_messages(target)
        self.assertEqual(
            [message["action"] for message in replay_messages],
            ["playback.handoff.status", "playback.context.status"],
        )
        self.assertEqual(
            replay_messages[0]["payload"]["status"],
            "completed",
        )
        self.assertEqual(
            replay_messages[1]["payload"]["playbackContext"][
                "authorityClientId"
            ],
            "target-1",
        )
        self.assertEqual(self.get_messages(source), [])

    def test_same_source_target_retry_reuses_handoff_and_prepare(self):
        _source, target, controller = self.connect_handoff_devices()

        first_ack = self.get_ack(
            self.start_handoff(controller, "handoff-start-first"),
            "handoff-start-first",
        )
        first_prepare = next(
            message
            for message in self.get_messages(target)
            if message["action"] == "playback.prepare"
        )
        retry_ack = self.get_ack(
            self.start_handoff(controller, "handoff-start-retry"),
            "handoff-start-retry",
        )

        self.assertEqual(
            retry_ack["payload"]["handoffId"],
            first_ack["payload"]["handoffId"],
        )
        self.assertEqual(
            retry_ack["payload"]["prepareId"],
            first_prepare["payload"]["prepareId"],
        )
        self.assertEqual(self.get_messages(target), [])

    def test_start_prepare_push_failure_keeps_ack_and_idempotent_handoff(self):
        _source, target, controller = self.connect_handoff_devices()

        with mock.patch(
            "supysonic.emo.ws._send_playback_prepare",
            side_effect=RuntimeError("injected prepare emit failure"),
        ):
            first_messages = self.start_handoff(controller)

        first_ack = self.get_ack(first_messages, "handoff-start-1")
        self.assertEqual(
            [message["action"] for message in first_messages],
            ["system.ack"],
        )
        self.assertEqual(first_ack["payload"]["status"], "preparing")
        self.assertEqual(self.get_messages(target), [])
        handoff = getPlaybackHandoff(first_ack["payload"]["handoffId"])
        self.assertEqual(handoff["status"], "preparing")

        retry_messages = self.start_handoff(controller)
        retry_ack = self.get_ack(retry_messages, "handoff-start-1")
        self.assertEqual(
            retry_ack["payload"]["handoffId"],
            first_ack["payload"]["handoffId"],
        )
        self.assertEqual(
            retry_ack["payload"]["prepareId"],
            first_ack["payload"]["prepareId"],
        )
        self.assertEqual(self.get_messages(target), [])

    def test_start_ack_emit_failure_still_sends_one_prepare_and_replays_ack(self):
        _source, target, controller = self.connect_handoff_devices()
        request_id = "handoff-start-ack-failure"
        real_emit = socketio.emit

        def fail_start_ack(event, message, **kwargs):
            if (
                message["action"] == "system.ack"
                and message.get("requestId") == request_id
            ):
                raise RuntimeError("injected handoff start ACK failure")
            return real_emit(event, message, **kwargs)

        with mock.patch.object(
            socketio,
            "emit",
            side_effect=fail_start_ack,
        ):
            first_messages = self.start_handoff(
                controller,
                request_id=request_id,
            )

        self.assertEqual(first_messages, [])
        target_messages = self.get_messages(target)
        prepare = next(
            message
            for message in target_messages
            if message["action"] == "playback.prepare"
        )
        handoff = getPlaybackHandoff(prepare["payload"]["handoffId"])
        self.assertEqual(handoff["status"], "preparing")

        replay_messages = self.start_handoff(
            controller,
            request_id=request_id,
        )
        replay_ack = self.get_ack(replay_messages, request_id)
        self.assertEqual(
            replay_ack["payload"]["handoffId"],
            prepare["payload"]["handoffId"],
        )
        self.assertEqual(
            replay_ack["payload"]["prepareId"],
            prepare["payload"]["prepareId"],
        )
        self.assertEqual(self.get_messages(target), [])

    def test_ready_commit_push_failure_replays_committing_confirmation(self):
        source, target, controller = self.connect_handoff_devices()
        start_ack = self.get_ack(
            self.start_handoff(controller),
            "handoff-start-1",
        )
        prepare = next(
            message
            for message in self.get_messages(target)
            if message["action"] == "playback.prepare"
        )
        self.get_messages(source)
        ready_request = {
            "type": "event",
            "action": "playback.ready",
            "requestId": "handoff-ready-commit-push-failure",
            "payload": {
                "playbackContextId": "context-handoff-1",
                "handoffId": start_ack["payload"]["handoffId"],
                "prepareId": prepare["payload"]["prepareId"],
                "ready": True,
            },
        }
        settled_before_push = []

        def fail_after_settlement(*_args, **_kwargs):
            settled_before_push.append(
                any(
                    cached_request_id == ready_request["requestId"]
                    and entry.result is not None
                    for (_nonce, cached_request_id), entry
                    in strict_request_cache._entries.items()
                )
            )
            raise RuntimeError("injected commit emit failure")

        with mock.patch(
            "supysonic.emo.ws._send_strict_handoff_commit",
            side_effect=fail_after_settlement,
        ):
            target.emit("message", ready_request, namespace="/emo")

        self.assertEqual(settled_before_push, [True])
        ready_messages = self.get_messages(target)
        self.assertEqual(
            [message["action"] for message in ready_messages],
            ["playback.handoff.status"],
        )
        self.assertEqual(ready_messages[0]["payload"]["status"], "committing")
        self.assertFalse(
            any(message["action"] == "player.play" for message in ready_messages)
        )
        context = getPlaybackContextState("context-handoff-1")
        handoff = getPlaybackHandoff(start_ack["payload"]["handoffId"])
        self.assertEqual(context["authorityClientId"], "source-1")
        self.assertIn(handoff["status"], {"committed", "committing"})

        self.get_messages(source)
        self.get_messages(controller)
        target.emit("message", ready_request, namespace="/emo")
        replay_messages = self.get_messages(target)
        self.assertEqual(
            [message["action"] for message in replay_messages],
            ["playback.handoff.status"],
        )
        self.assertEqual(replay_messages[0]["payload"]["status"], "committing")
        self.assertEqual(self.get_messages(source), [])

    def test_start_requires_controller_and_target_prepare_capabilities(self):
        source, _target, controller = self.connect_handoff_devices()

        source.emit(
            "message",
            {
                "type": "command",
                "action": "playback.handoff.start",
                "requestId": "handoff-start-player",
                "payload": {
                    "playbackContextId": "context-handoff-1",
                    "targetClientId": "target-1",
                    "baseControlVersion": 1,
                },
            },
            namespace="/emo",
        )
        role_error = self.get_error(
            self.get_messages(source),
            "handoff-start-player",
        )

        weak_target = self.connect_device(
            "alice",
            "Alic3",
            "weak-target-1",
            "device:weak-target-1",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        self.get_messages(weak_target)
        capability_error = self.get_error(
            self.start_handoff(
                controller,
                "handoff-start-weak-target",
                target_client_id="weak-target-1",
            ),
            "handoff-start-weak-target",
        )

        bob_target = self.connect_device(
            "bob",
            "B0b",
            "bob-target-1",
            "device:bob-target-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "playbackPrepare": True,
                "effectiveAtPlayback": True,
            },
        )
        self.get_messages(bob_target)
        cross_user_error = self.get_error(
            self.start_handoff(
                controller,
                "handoff-start-cross-user",
                target_client_id="bob-target-1",
            ),
            "handoff-start-cross-user",
        )

        self.assertEqual(role_error["payload"]["code"], "forbidden")
        self.assertEqual(
            capability_error["payload"]["code"],
            "capability_required",
        )
        self.assertEqual(cross_user_error["payload"]["code"], "forbidden")

    def test_source_requires_can_pause(self):
        source = self.connect_device(
            "alice",
            "Alic3",
            "source-1",
            "device:source-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "canPause": False,
            },
        )
        target = self.connect_device(
            "alice",
            "Alic3",
            "target-1",
            "device:target-1",
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
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        for client in (source, target, controller):
            self.get_messages(client)
        self.create_playback_context(
            source,
            "context-create-no-pause",
            playback_context_id="context-handoff-1",
            device_session_id="device:source-1",
        )
        self.get_messages(controller)

        error = self.get_error(
            self.start_handoff(controller, "handoff-start-no-pause"),
            "handoff-start-no-pause",
        )

        self.assertEqual(error["payload"]["code"], "capability_required")

    def test_ready_false_is_event_confirmed_failed_status(self):
        source, target, controller = self.connect_handoff_devices()
        start_ack = self.get_ack(
            self.start_handoff(controller),
            "handoff-start-1",
        )
        self.get_messages(target)

        target.emit(
            "message",
            {
                "type": "event",
                "action": "playback.ready",
                "requestId": "handoff-ready-failed",
                "payload": {
                    "playbackContextId": "context-handoff-1",
                    "handoffId": start_ack["payload"]["handoffId"],
                    "prepareId": start_ack["payload"]["prepareId"],
                    "ready": False,
                    "errorCode": "decoder_unavailable",
                    "errorMessage": "Decoder unavailable",
                },
            },
            namespace="/emo",
        )
        target_messages = self.get_messages(target)
        failed_status = next(
            message
            for message in target_messages
            if message["action"] == "playback.handoff.status"
        )

        self.assertFalse(
            any(message["action"] == "system.ack" for message in target_messages)
        )
        self.assertEqual(failed_status["payload"]["status"], "failed")
        self.assertEqual(
            failed_status["payload"]["errorCode"],
            "decoder_unavailable",
        )
        handoff = getPlaybackHandoff(start_ack["payload"]["handoffId"])
        self.assertEqual(handoff["status"], "failed")
        self.assertEqual(handoff["errorCode"], "decoder_unavailable")
        self.assertTrue(
            any(
                message["action"] == "playback.handoff.status"
                for message in self.get_messages(source)
            )
        )

    def test_ready_false_cancel_push_failure_replays_failed_confirmation(self):
        source, target, controller = self.connect_handoff_devices()
        start_ack = self.get_ack(
            self.start_handoff(controller),
            "handoff-start-1",
        )
        self.get_messages(target)
        self.get_messages(source)
        self.get_messages(controller)
        ready_request = {
            "type": "event",
            "action": "playback.ready",
            "requestId": "handoff-ready-failed-push-failure",
            "payload": {
                "playbackContextId": "context-handoff-1",
                "handoffId": start_ack["payload"]["handoffId"],
                "prepareId": start_ack["payload"]["prepareId"],
                "ready": False,
                "errorCode": "decoder_unavailable",
                "errorMessage": "Decoder unavailable",
            },
        }
        settled_before_push = []

        def fail_after_settlement(*_args, **_kwargs):
            settled_before_push.append(
                any(
                    cached_request_id == ready_request["requestId"]
                    and entry.result is not None
                    for (_nonce, cached_request_id), entry
                    in strict_request_cache._entries.items()
                )
            )
            raise RuntimeError("injected cancel emit failure")

        with mock.patch(
            "supysonic.emo.ws._broadcast_handoff_cancel",
            side_effect=fail_after_settlement,
        ):
            target.emit("message", ready_request, namespace="/emo")

        self.assertEqual(settled_before_push, [True])
        target_messages = self.get_messages(target)
        self.assertEqual(
            [message["action"] for message in target_messages],
            ["playback.handoff.status"],
        )
        self.assertEqual(target_messages[0]["payload"]["status"], "failed")
        self.assertEqual(
            target_messages[0]["payload"]["errorCode"],
            "decoder_unavailable",
        )
        handoff = getPlaybackHandoff(start_ack["payload"]["handoffId"])
        self.assertEqual(handoff["status"], "failed")
        self.assertEqual(handoff["errorCode"], "decoder_unavailable")

        self.get_messages(source)
        self.get_messages(controller)
        target.emit("message", ready_request, namespace="/emo")
        replay_messages = self.get_messages(target)
        self.assertEqual(
            [message["action"] for message in replay_messages],
            ["playback.handoff.status"],
        )
        self.assertEqual(replay_messages[0]["payload"]["status"], "failed")
        self.assertEqual(self.get_messages(source), [])

    def test_cancel_is_idempotent_action_ack_and_terminal_push(self):
        source, target, controller = self.connect_handoff_devices()
        start_ack = self.get_ack(
            self.start_handoff(controller),
            "handoff-start-1",
        )
        self.get_messages(target)

        cancel_request = {
            "type": "command",
            "action": "playback.handoff.cancel",
            "requestId": "handoff-cancel-1",
            "payload": {
                "playbackContextId": "context-handoff-1",
                "handoffId": start_ack["payload"]["handoffId"],
                "reason": "user_cancelled",
            },
        }
        controller.emit("message", cancel_request, namespace="/emo")
        controller_messages = self.get_messages(controller)
        self.assertEqual(
            [message["action"] for message in controller_messages],
            [
                "system.ack",
                "playback.handoff.cancel",
                "playback.handoff.status",
            ],
        )
        cancel_ack = self.get_ack(controller_messages, "handoff-cancel-1")
        cancel_push = next(
            message
            for message in controller_messages
            if message["action"] == "playback.handoff.cancel"
        )
        status_push = next(
            message
            for message in controller_messages
            if message["action"] == "playback.handoff.status"
        )

        self.assertEqual(cancel_ack["payload"], {"action": "playback.handoff.cancel"})
        self.assertEqual(status_push["payload"]["status"], "cancelled")
        self.assertEqual(
            cancel_push["payload"],
            {
                "playbackContextId": "context-handoff-1",
                "handoffId": start_ack["payload"]["handoffId"],
                "reason": "user_cancelled",
                "controlVersion": 2,
            },
        )
        self.assertEqual(
            [message["action"] for message in self.get_messages(target)],
            ["playback.handoff.cancel"],
        )
        self.assertEqual(
            [message["action"] for message in self.get_messages(source)],
            ["playback.handoff.cancel", "playback.handoff.status"],
        )

        retry_request = dict(cancel_request)
        retry_request["requestId"] = "handoff-cancel-2"
        controller.emit("message", retry_request, namespace="/emo")
        retry_messages = self.get_messages(controller)
        retry_ack = self.get_ack(retry_messages, "handoff-cancel-2")
        self.assertEqual(retry_ack["payload"], {"action": "playback.handoff.cancel"})
        self.assertEqual(len(retry_messages), 1)
        self.assertEqual(
            getPlaybackHandoff(start_ack["payload"]["handoffId"])["status"],
            "cancelled",
        )

    def test_cancel_push_failures_keep_single_ack_settlement(self):
        source, target, controller = self.connect_handoff_devices()
        start_ack = self.get_ack(
            self.start_handoff(controller),
            "handoff-start-1",
        )
        for client in (source, target, controller):
            self.get_messages(client)
        cancel_request = {
            "type": "command",
            "action": "playback.handoff.cancel",
            "requestId": "handoff-cancel-push-failure",
            "payload": {
                "playbackContextId": "context-handoff-1",
                "handoffId": start_ack["payload"]["handoffId"],
                "reason": "user_cancelled",
            },
        }

        with mock.patch(
            "supysonic.emo.ws._broadcast_handoff_cancel",
            side_effect=RuntimeError("injected cancel push failure"),
        ), mock.patch(
            "supysonic.emo.ws._broadcast_handoff_status",
            side_effect=RuntimeError("injected status push failure"),
        ):
            controller.emit("message", cancel_request, namespace="/emo")

        controller_messages = self.get_messages(controller)
        self.assertEqual(
            [message["action"] for message in controller_messages],
            ["system.ack"],
        )
        self.assertEqual(
            controller_messages[0]["payload"],
            {"action": "playback.handoff.cancel"},
        )
        self.assertEqual(self.get_messages(source), [])
        self.assertEqual(self.get_messages(target), [])
        self.assertEqual(
            getPlaybackHandoff(start_ack["payload"]["handoffId"])["status"],
            "cancelled",
        )

        controller.emit("message", cancel_request, namespace="/emo")
        replay_messages = self.get_messages(controller)
        self.assertEqual(
            [message["action"] for message in replay_messages],
            ["system.ack"],
        )
        self.assertEqual(
            replay_messages[0]["payload"],
            controller_messages[0]["payload"],
        )

    def test_prepare_timeout_is_terminal_and_keeps_source_authority(self):
        source, target, controller = self.connect_handoff_devices()
        start_ack = self.get_ack(
            self.start_handoff(controller),
            "handoff-start-1",
        )
        self.get_messages(target)
        prepare = get_state().get_prepare(start_ack["payload"]["prepareId"])
        for client in (source, target, controller):
            self.get_messages(client)

        with mock.patch(
            "supysonic.emo.ws._server_time_ms",
            return_value=prepare["expiresAtMs"],
        ):
            _expire_prepare(start_ack["payload"]["prepareId"])

        handoff = getPlaybackHandoff(start_ack["payload"]["handoffId"])
        context = getPlaybackContextState("context-handoff-1")
        target_messages = self.get_messages(target)
        self.assertEqual(handoff["status"], "timed_out")
        self.assertEqual(handoff["errorCode"], "prepare_timeout")
        self.assertEqual(context["authorityClientId"], "source-1")
        self.assertEqual(
            [message["action"] for message in target_messages],
            ["playback.handoff.cancel", "playback.handoff.status"],
        )
        self.assertEqual(
            target_messages[0]["payload"]["reason"],
            "prepare_timeout",
        )
        self.assertEqual(
            target_messages[1]["payload"]["status"],
            "timedOut",
        )

    def test_prepare_timeout_cancel_push_failure_still_emits_status(self):
        source, target, controller = self.connect_handoff_devices()
        start_ack = self.get_ack(
            self.start_handoff(controller),
            "handoff-start-1",
        )
        prepare = get_state().get_prepare(start_ack["payload"]["prepareId"])
        for client in (source, target, controller):
            self.get_messages(client)

        with mock.patch(
            "supysonic.emo.ws._broadcast_handoff_cancel",
            side_effect=RuntimeError("injected timeout cancel failure"),
        ), mock.patch(
            "supysonic.emo.ws._server_time_ms",
            return_value=prepare["expiresAtMs"],
        ):
            _expire_prepare(start_ack["payload"]["prepareId"])

        persisted = getPlaybackHandoff(start_ack["payload"]["handoffId"])
        self.assertEqual(persisted["status"], "timed_out")
        self.assertEqual(persisted["errorCode"], "prepare_timeout")
        for client in (source, target):
            messages = self.get_messages(client)
            self.assertEqual(
                [message["action"] for message in messages],
                ["playback.handoff.status"],
            )
            self.assertEqual(messages[0]["payload"]["status"], "timedOut")

    def test_commit_timeout_is_terminal_and_keeps_source_authority(self):
        source, target, controller = self.connect_handoff_devices()
        start_ack = self.get_ack(
            self.start_handoff(controller),
            "handoff-start-1",
        )
        self.get_messages(target)
        target.emit(
            "message",
            {
                "type": "event",
                "action": "playback.ready",
                "requestId": "handoff-ready-timeout",
                "payload": {
                    "playbackContextId": "context-handoff-1",
                    "handoffId": start_ack["payload"]["handoffId"],
                    "prepareId": start_ack["payload"]["prepareId"],
                    "ready": True,
                },
            },
            namespace="/emo",
        )
        self.get_messages(target)
        for client in (source, controller):
            self.get_messages(client)
        handoff = get_state().get_playback_handoff(
            start_ack["payload"]["handoffId"]
        )

        with mock.patch(
            "supysonic.emo.ws._server_time_ms",
            return_value=handoff["completeExpiresAtMs"],
        ):
            _expire_handoff_complete(start_ack["payload"]["handoffId"])

        persisted_handoff = getPlaybackHandoff(start_ack["payload"]["handoffId"])
        context = getPlaybackContextState("context-handoff-1")
        target_messages = self.get_messages(target)
        self.assertEqual(persisted_handoff["status"], "timed_out")
        self.assertEqual(persisted_handoff["errorCode"], "commit_timeout")
        self.assertEqual(context["authorityClientId"], "source-1")
        self.assertEqual(
            [message["action"] for message in target_messages],
            ["playback.handoff.cancel", "playback.handoff.status"],
        )

    def test_commit_timeout_cancel_push_failure_still_emits_status(self):
        source, target, controller = self.connect_handoff_devices()
        start_ack = self.get_ack(
            self.start_handoff(controller),
            "handoff-start-1",
        )
        self.get_messages(target)
        target.emit(
            "message",
            {
                "type": "event",
                "action": "playback.ready",
                "requestId": "handoff-ready-timeout-push-failure",
                "payload": {
                    "playbackContextId": "context-handoff-1",
                    "handoffId": start_ack["payload"]["handoffId"],
                    "prepareId": start_ack["payload"]["prepareId"],
                    "ready": True,
                },
            },
            namespace="/emo",
        )
        for client in (source, target, controller):
            self.get_messages(client)
        handoff = get_state().get_playback_handoff(
            start_ack["payload"]["handoffId"]
        )

        with mock.patch(
            "supysonic.emo.ws._broadcast_handoff_cancel",
            side_effect=RuntimeError("injected timeout cancel failure"),
        ), mock.patch(
            "supysonic.emo.ws._server_time_ms",
            return_value=handoff["completeExpiresAtMs"],
        ):
            _expire_handoff_complete(start_ack["payload"]["handoffId"])

        persisted = getPlaybackHandoff(start_ack["payload"]["handoffId"])
        self.assertEqual(persisted["status"], "timed_out")
        self.assertEqual(persisted["errorCode"], "commit_timeout")
        for client in (source, target):
            messages = self.get_messages(client)
            self.assertEqual(
                [message["action"] for message in messages],
                ["playback.handoff.status"],
            )
            self.assertEqual(messages[0]["payload"]["status"], "timedOut")

    def test_context_close_fences_late_handoff_complete(self):
        source, target, controller = self.connect_handoff_devices()
        start_ack = self.get_ack(
            self.start_handoff(controller),
            "handoff-start-1",
        )
        self.get_messages(target)
        target.emit(
            "message",
            {
                "type": "event",
                "action": "playback.ready",
                "requestId": "handoff-ready-before-close",
                "payload": {
                    "playbackContextId": "context-handoff-1",
                    "handoffId": start_ack["payload"]["handoffId"],
                    "prepareId": start_ack["payload"]["prepareId"],
                    "ready": True,
                },
            },
            namespace="/emo",
        )
        for client in (source, target, controller):
            self.get_messages(client)

        controller.emit(
            "message",
            {
                "type": "command",
                "action": "playback.context.close",
                "requestId": "context-close-during-handoff",
                "payload": {"playbackContextId": "context-handoff-1"},
            },
            namespace="/emo",
        )
        close_ack = self.get_ack(
            self.get_messages(controller),
            "context-close-during-handoff",
        )
        self.assertEqual(
            close_ack["payload"],
            {"action": "playback.context.close"},
        )
        self.get_messages(source)
        self.get_messages(target)

        target.emit(
            "message",
            {
                "type": "event",
                "action": "playback.handoff.complete",
                "requestId": "handoff-complete-after-close",
                "payload": {
                    "playbackContextId": "context-handoff-1",
                    "handoffId": start_ack["payload"]["handoffId"],
                    "positionMs": 1500,
                },
            },
            namespace="/emo",
        )
        complete_messages = self.get_messages(target)
        error = self.get_error(
            complete_messages,
            "handoff-complete-after-close",
        )
        context = getPlaybackContextState("context-handoff-1")
        handoff = getPlaybackHandoff(start_ack["payload"]["handoffId"])

        self.assertEqual(error["payload"]["code"], "context_closed")
        self.assertEqual(
            error["payload"]["playbackContextId"],
            "context-handoff-1",
        )
        self.assertEqual(context["lifecycle"], "closed")
        self.assertEqual(context["authorityClientId"], "source-1")
        self.assertEqual(context["version"], 2)
        self.assertEqual(context["controlVersion"], 1)
        self.assertEqual(handoff["status"], "failed")
        self.assertEqual(handoff["errorCode"], "context_closed")
        self.assertFalse(
            any(
                message["action"] == "playback.handoff.status"
                and message["payload"].get("status") == "completed"
                for message in complete_messages
            )
        )

    def test_target_disconnect_fails_handoff_without_switching_authority(self):
        source, target, controller = self.connect_handoff_devices()
        start_ack = self.get_ack(
            self.start_handoff(controller),
            "handoff-start-1",
        )
        for client in (source, target, controller):
            self.get_messages(client)

        target.disconnect(namespace="/emo")
        self.clients.remove(target)

        handoff = getPlaybackHandoff(start_ack["payload"]["handoffId"])
        context = getPlaybackContextState("context-handoff-1")
        source_messages = self.get_messages(source)
        self.assertEqual(handoff["status"], "failed")
        self.assertEqual(handoff["errorCode"], "target_disconnected")
        self.assertEqual(context["authorityClientId"], "source-1")
        self.assertEqual(
            [message["action"] for message in source_messages],
            ["playback.handoff.cancel", "playback.handoff.status"],
        )
        self.assertEqual(
            source_messages[1]["payload"]["status"],
            "failed",
        )

    def test_disconnect_cancel_push_failure_still_emits_terminal_status(self):
        source, target, controller = self.connect_handoff_devices()
        start_ack = self.get_ack(
            self.start_handoff(controller),
            "handoff-start-1",
        )
        for client in (source, target, controller):
            self.get_messages(client)

        with mock.patch(
            "supysonic.emo.ws._broadcast_handoff_cancel",
            side_effect=RuntimeError("injected disconnect cancel failure"),
        ):
            target.disconnect(namespace="/emo")
        self.clients.remove(target)

        persisted = getPlaybackHandoff(start_ack["payload"]["handoffId"])
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(persisted["errorCode"], "target_disconnected")
        source_messages = self.get_messages(source)
        self.assertEqual(
            [message["action"] for message in source_messages],
            ["playback.handoff.status"],
        )
        self.assertEqual(source_messages[0]["payload"]["status"], "failed")

    def test_source_disconnect_cancels_handoff_without_switching_authority(self):
        source, target, controller = self.connect_handoff_devices()
        start_ack = self.get_ack(
            self.start_handoff(controller),
            "handoff-start-1",
        )
        for client in (source, target, controller):
            self.get_messages(client)

        source.disconnect(namespace="/emo")
        self.clients.remove(source)

        handoff = getPlaybackHandoff(start_ack["payload"]["handoffId"])
        context = getPlaybackContextState("context-handoff-1")
        target_messages = self.get_messages(target)
        self.assertEqual(handoff["status"], "cancelled")
        self.assertEqual(handoff["errorCode"], "source_disconnected")
        self.assertEqual(context["authorityClientId"], "source-1")
        self.assertEqual(
            [message["action"] for message in target_messages],
            ["playback.handoff.cancel", "playback.handoff.status"],
        )
        self.assertEqual(
            target_messages[1]["payload"]["status"],
            "cancelled",
        )

    def test_restart_fails_nonterminal_handoff_with_server_restart(self):
        _source, target, controller = self.connect_handoff_devices()
        start_ack = self.get_ack(
            self.start_handoff(controller),
            "handoff-start-1",
        )
        self.get_messages(target)

        reconciled = failActivePlaybackHandoffsForRestart()
        get_state().restore_strict_playback_contexts(listPlaybackContexts())

        handoff = getPlaybackHandoff(start_ack["payload"]["handoffId"])
        self.assertEqual(reconciled, [start_ack["payload"]["handoffId"]])
        self.assertEqual(handoff["status"], "failed")
        self.assertEqual(handoff["errorCode"], "server_restart")
        self.assertIsNone(
            get_state().get_playback_handoff(start_ack["payload"]["handoffId"])
        )

    def test_start_does_not_restore_legacy_queue_as_strict_context(self):
        target = self.connect_device(
            "alice",
            "Alic3",
            "target-1",
            "device:target-1",
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
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        self.get_messages(target)
        self.get_messages(controller)
        saveQueueState(
            "context-handoff-1",
            "alice",
            "source-1",
            ["song-legacy"],
            0,
            0,
        )

        error = self.get_error(
            self.start_handoff(controller, "handoff-start-legacy-queue"),
            "handoff-start-legacy-queue",
        )

        self.assertEqual(error["payload"]["code"], "not_found")
        self.assertIsNone(getPlaybackContextState("context-handoff-1"))

    def test_cancel_and_complete_reject_session_id_before_mutation(self):
        _source, target, controller = self.connect_handoff_devices()
        start_ack = self.get_ack(
            self.start_handoff(controller),
            "handoff-start-1",
        )
        self.get_messages(target)

        for client, action, request_id in (
            (controller, "playback.handoff.cancel", "handoff-cancel-session"),
            (target, "playback.handoff.complete", "handoff-complete-session"),
        ):
            with self.subTest(action=action):
                client.emit(
                    "message",
                    {
                        "type": "command" if action.endswith("cancel") else "event",
                        "action": action,
                        "requestId": request_id,
                        "payload": {
                            "playbackContextId": "context-handoff-1",
                            "handoffId": start_ack["payload"]["handoffId"],
                            "sessionId": "legacy-room",
                        },
                    },
                    namespace="/emo",
                )
                error = self.get_error(self.get_messages(client), request_id)
                self.assertEqual(error["payload"]["code"], "bad_request")

        handoff = getPlaybackHandoff(start_ack["payload"]["handoffId"])
        self.assertEqual(handoff["status"], "preparing")


def load_tests(loader, standard_tests, pattern):
    del loader, standard_tests, pattern
    suite = unittest.TestSuite()
    for test_name in sorted(
        name
        for name in StrictV2HandoffTestCase.__dict__
        if name.startswith("test_")
    ):
        suite.addTest(StrictV2HandoffTestCase(test_name))
    return suite


if __name__ == "__main__":
    unittest.main()
