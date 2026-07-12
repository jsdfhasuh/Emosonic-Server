import os
import shutil
import tempfile
import unittest
from unittest import mock

from supysonic.db import release_database
from supysonic.emo import ws as emo_ws
from supysonic.emo.ws import socketio, strict_request_cache
from supysonic.emo.ws_state import get_state
from supysonic.emo.ws_store import getPlaybackContextState
from supysonic.managers.user import UserManager
from supysonic.web import create_application

from tests.testbase import TestConfig


class StrictV2CoreTestCase(unittest.TestCase):
    def setUp(self):
        self.database = tempfile.mkstemp()
        self.cache_directory = tempfile.mkdtemp()
        self.config = TestConfig(False, False)
        self.config.BASE["database_uri"] = "sqlite:///" + self.database[1]
        self.config.WEBAPP["cache_dir"] = self.cache_directory
        self.config.WEBAPP["mount_emosonic"] = True
        self.app = create_application(self.config)
        self.http_client = self.app.test_client()
        UserManager.add("alice", "Alic3", admin=True)
        UserManager.add("bob", "B0b")
        self.clients = []

        state = get_state()
        state._sessions.clear()
        state._client_to_sid.clear()
        state._clients.clear()
        state._playback_contexts.clear()
        state._device_playback_states.clear()
        state._strict_feedback_sequences.clear()
        state._playback_context_subscriptions.clear()
        state._handoffs.clear()
        state._pending_prepares.clear()

    def tearDown(self):
        for client in self.clients:
            if client.is_connected(namespace="/emo"):
                client.disconnect(namespace="/emo")
        release_database()
        shutil.rmtree(self.cache_directory)
        os.close(self.database[0])
        os.remove(self.database[1])

    def connect(self):
        client = socketio.test_client(
            self.app,
            namespace="/emo",
            flask_test_client=self.http_client,
        )
        self.clients.append(client)
        return client

    def messages(self, client):
        messages = []
        for event in client.get_received("/emo"):
            if event["name"] != "message":
                continue
            args = event.get("args")
            messages.append(args[0] if isinstance(args, list) else args)
        return messages

    def authenticate(self, client, user_name="alice", password="Alic3", request_id="auth-1"):
        client.emit(
            "message",
            {
                "type": "auth",
                "action": "auth.login",
                "requestId": request_id,
                "payload": {"u": user_name, "p": password},
            },
            namespace="/emo",
        )
        return self.messages(client)

    def strict_registration_payload(
        self,
        roles=None,
        client_id="phone-1",
        device_session_id="device:phone-1",
        capability_overrides=None,
    ):
        capabilities = {
            "playbackContextV2": True,
            "playbackPrepare": True,
            "effectiveAtPlayback": True,
            "canPlay": True,
            "canPause": True,
            "canSeek": True,
            "canSetVolume": True,
            "supportsFollow": True,
            "supportsBroadcast": True,
        }
        capabilities.update(capability_overrides or {})
        return {
            "clientId": client_id,
            "deviceSessionId": device_session_id,
            "deviceName": "Phone",
            "roles": roles or ["player"],
            "capabilities": capabilities,
        }

    def enable_all_profiles(self):
        self.app.config["WEBAPP"].update(
            {
                "emo_strict_v2_core_enabled": True,
                "emo_strict_v2_follow_enabled": True,
                "emo_strict_v2_handoff_enabled": True,
                "emo_strict_v2_broadcast_enabled": True,
            }
        )
        return mock.patch(
            "supysonic.emo.strict_v2_readiness.get_code_conformance_readiness",
            return_value={
                "core": True,
                "follow": True,
                "handoff": True,
                "broadcast": True,
            },
        )

    def register(self, client, request_id, payload):
        client.emit(
            "message",
            {
                "type": "device",
                "action": "device.register",
                "requestId": request_id,
                "payload": payload,
            },
            namespace="/emo",
        )
        return self.messages(client)

    def ready_strict_client(
        self,
        roles=None,
        client_id="phone-1",
        device_session_id="device:phone-1",
        capability_overrides=None,
    ):
        client = self.connect()
        self.authenticate(client, request_id="auth-%s" % client_id)
        with self.enable_all_profiles():
            self.register(
                client,
                "register-%s" % client_id,
                self.strict_registration_payload(
                    roles=roles,
                    client_id=client_id,
                    device_session_id=device_session_id,
                    capability_overrides=capability_overrides,
                ),
            )
        self.messages(client)
        return client

    def create_context(
        self,
        client,
        request_id="context-create-1",
        queue_song_ids=None,
        state="playing",
        position_ms=1200,
    ):
        client.emit(
            "message",
            {
                "type": "command",
                "action": "playback.context.create",
                "requestId": request_id,
                "payload": {
                    "playbackContextId": "context-1",
                    "deviceSessionId": "device:phone-1",
                    "queueSongIds": queue_song_ids or ["song-2", "song-1"],
                    "currentIndex": 0,
                    "positionMs": position_ms,
                    "state": state,
                },
            },
            namespace="/emo",
        )
        return self.messages(client)

    def emit_strict(self, client, message_type, action, request_id, payload):
        client.emit(
            "message",
            {
                "type": message_type,
                "action": action,
                "requestId": request_id,
                "payload": payload,
            },
            namespace="/emo",
        )
        return self.messages(client)

    def test_non_object_envelope_disconnects_without_error(self):
        client = self.connect()

        client.emit("message", [], namespace="/emo")

        self.assertFalse(client.is_connected(namespace="/emo"))

    def test_missing_request_id_disconnects_without_fabricated_error(self):
        client = self.connect()

        client.emit(
            "message",
            {
                "type": "auth",
                "action": "auth.login",
                "payload": {"u": "alice", "p": "Alic3"},
            },
            namespace="/emo",
        )

        self.assertFalse(client.is_connected(namespace="/emo"))

    def test_correlatable_bootstrap_schema_error_returns_bad_request(self):
        client = self.connect()

        client.emit(
            "message",
            {
                "type": "auth",
                "action": "auth.login",
                "requestId": "auth-invalid-1",
                "payload": {"u": "alice", "p": "Alic3", "unexpected": True},
            },
            namespace="/emo",
        )

        errors = [
            message
            for message in self.messages(client)
            if message.get("action") == "system.error"
        ]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["requestId"], "auth-invalid-1")
        self.assertEqual(errors[0]["payload"]["action"], "auth.login")
        self.assertEqual(errors[0]["payload"]["code"], "bad_request")
        self.assertTrue(client.is_connected(namespace="/emo"))

    def test_bootstrap_type_action_mismatch_returns_bad_request(self):
        client = self.connect()

        client.emit(
            "message",
            {
                "type": "command",
                "action": "auth.login",
                "requestId": "auth-type-1",
                "payload": {"u": "alice", "p": "Alic3"},
            },
            namespace="/emo",
        )

        error = self.messages(client)[0]
        self.assertEqual(error["requestId"], "auth-type-1")
        self.assertEqual(error["payload"]["code"], "bad_request")

    def test_core_disabled_returns_not_supported_without_registering(self):
        client = self.connect()
        self.authenticate(client)

        client.emit(
            "message",
            {
                "type": "device",
                "action": "device.register",
                "requestId": "register-disabled-1",
                "payload": self.strict_registration_payload(),
            },
            namespace="/emo",
        )

        error = self.messages(client)[0]
        self.assertEqual(error["requestId"], "register-disabled-1")
        self.assertEqual(error["payload"]["action"], "device.register")
        self.assertEqual(error["payload"]["code"], "not_supported")
        self.assertIsNone(get_state().get_client("phone-1"))

    def test_ready_core_registers_single_role_and_returns_full_negotiation(self):
        client = self.connect()
        self.authenticate(client)
        self.app.config["WEBAPP"].update(
            {
                "emo_strict_v2_core_enabled": True,
                "emo_strict_v2_follow_enabled": False,
                "emo_strict_v2_handoff_enabled": False,
                "emo_strict_v2_broadcast_enabled": False,
            }
        )
        code_readiness = {
            "core": True,
            "follow": True,
            "handoff": True,
            "broadcast": True,
        }

        with mock.patch(
            "supysonic.emo.strict_v2_readiness.get_code_conformance_readiness",
            return_value=code_readiness,
        ):
            client.emit(
                "message",
                {
                    "type": "device",
                    "action": "device.register",
                    "requestId": "register-ready-1",
                    "payload": self.strict_registration_payload(["player"]),
                },
                namespace="/emo",
            )

        ack = next(
            message
            for message in self.messages(client)
            if message.get("requestId") == "register-ready-1"
        )
        negotiated = ack["payload"]["negotiatedCapabilities"]
        self.assertEqual(ack["payload"]["action"], "device.register")
        self.assertEqual(ack["payload"]["clientId"], "phone-1")
        self.assertEqual(ack["payload"]["deviceSessionId"], "device:phone-1")
        self.assertEqual(set(negotiated), set(self.strict_registration_payload()["capabilities"]))
        self.assertTrue(negotiated["playbackContextV2"])
        self.assertFalse(negotiated["supportsFollow"])
        self.assertFalse(negotiated["playbackPrepare"])
        self.assertFalse(negotiated["effectiveAtPlayback"])
        self.assertFalse(negotiated["supportsBroadcast"])
        registered = get_state().get_client("phone-1")
        self.assertEqual(registered["roles"], ["player"])
        self.assertIn("rawCapabilities", registered)

    def test_same_user_registration_disconnects_old_sid(self):
        old_client = self.connect()
        new_client = self.connect()
        self.authenticate(old_client, request_id="auth-old")
        self.authenticate(new_client, request_id="auth-new")

        with self.enable_all_profiles():
            self.register(
                old_client,
                "register-old",
                self.strict_registration_payload(device_session_id="device:old"),
            )
            self.register(
                new_client,
                "register-new",
                self.strict_registration_payload(device_session_id="device:new"),
            )

        self.assertFalse(old_client.is_connected(namespace="/emo"))
        self.assertTrue(new_client.is_connected(namespace="/emo"))
        registered = get_state().get_client("phone-1", user_name="alice")
        self.assertEqual(registered["deviceSessionId"], "device:new")

    def test_same_client_id_for_different_users_stays_isolated(self):
        alice = self.connect()
        bob = self.connect()
        self.authenticate(alice, "alice", "Alic3", "auth-alice")
        self.authenticate(bob, "bob", "B0b", "auth-bob")

        with self.enable_all_profiles():
            self.register(
                alice,
                "register-alice",
                self.strict_registration_payload(device_session_id="device:alice"),
            )
            self.register(
                bob,
                "register-bob",
                self.strict_registration_payload(device_session_id="device:bob"),
            )

        self.assertTrue(alice.is_connected(namespace="/emo"))
        self.assertTrue(bob.is_connected(namespace="/emo"))
        self.assertEqual(
            get_state().get_client("phone-1", user_name="alice")["deviceSessionId"],
            "device:alice",
        )
        self.assertEqual(
            get_state().get_client("phone-1", user_name="bob")["deviceSessionId"],
            "device:bob",
        )
        self.assertIsNone(get_state().get_client("phone-1"))

    def test_duplicate_register_replays_cached_ack_without_second_mutation(self):
        client = self.connect()
        self.authenticate(client)
        payload = self.strict_registration_payload()

        with self.enable_all_profiles(), mock.patch(
            "supysonic.emo.ws._register_device",
            wraps=emo_ws._register_device,
        ) as register_device:
            first = self.register(client, "register-replay-1", payload)
            second = self.register(client, "register-replay-1", payload)

        first_ack = next(
            message
            for message in first
            if message.get("requestId") == "register-replay-1"
        )
        self.assertEqual(second, [first_ack])
        register_device.assert_called_once()

    def test_reused_register_request_id_with_different_payload_conflicts(self):
        client = self.connect()
        self.authenticate(client)
        first_payload = self.strict_registration_payload()
        conflicting_payload = dict(first_payload, deviceName="Other Phone")

        with self.enable_all_profiles():
            self.register(client, "register-conflict-1", first_payload)
            response = self.register(
                client,
                "register-conflict-1",
                conflicting_payload,
            )

        self.assertEqual(len(response), 1)
        self.assertEqual(response[0]["action"], "system.error")
        self.assertEqual(response[0]["payload"]["action"], "device.register")
        self.assertEqual(response[0]["payload"]["code"], "conflict")
        registered = get_state().get_client("phone-1", user_name="alice")
        self.assertEqual(registered["deviceName"], "Phone")

    def test_disconnect_clears_request_cache_for_connection_nonce(self):
        client = self.connect()
        nonce = next(iter(get_state()._sessions.values()))["connectionNonce"]
        self.authenticate(client, request_id="auth-cache-1")
        self.assertGreater(strict_request_cache.size(), 0)

        client.disconnect(namespace="/emo")

        self.assertEqual(
            [key for key in strict_request_cache._entries if key[0] == nonce],
            [],
        )

    def test_ping_requires_completed_registration(self):
        client = self.connect()
        self.authenticate(client)

        client.emit(
            "message",
            {
                "type": "system",
                "action": "system.ping",
                "requestId": "ping-before-register-1",
                "payload": {},
            },
            namespace="/emo",
        )

        response = self.messages(client)
        self.assertEqual(len(response), 1)
        self.assertEqual(response[0]["action"], "system.error")
        self.assertEqual(response[0]["payload"]["code"], "unauthorized")

    def test_duplicate_playback_update_replays_only_canonical_confirmation(self):
        client = self.connect()
        self.authenticate(client)
        with self.enable_all_profiles():
            self.register(
                client,
                "register-update-1",
                self.strict_registration_payload(),
            )
        client.emit(
            "message",
            {
                "type": "command",
                "action": "playback.context.create",
                "requestId": "context-create-1",
                "payload": {
                    "playbackContextId": "context-1",
                    "deviceSessionId": "device:phone-1",
                    "queueSongIds": ["song-1"],
                    "currentIndex": 0,
                    "positionMs": 0,
                    "state": "stopped",
                },
            },
            namespace="/emo",
        )
        self.messages(client)
        update = {
            "type": "event",
            "action": "playback.update",
            "requestId": "playback-update-1",
            "payload": {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "state": "playing",
                "positionMs": 10,
                "clientSeq": 1,
                "trackId": "song-1",
            },
        }
        state = get_state()

        with mock.patch.object(
            state,
            "record_strict_device_playback_state",
            wraps=state.record_strict_device_playback_state,
        ) as record_feedback:
            client.emit("message", update, namespace="/emo")
            first = self.messages(client)
            client.emit("message", update, namespace="/emo")
            replay = self.messages(client)

        self.assertEqual(record_feedback.call_count, 1)
        self.assertFalse(any(message["action"] == "system.ack" for message in first))
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0]["action"], "playback.update")
        self.assertNotIn("requestId", replay[0])
        self.assertEqual(replay[0]["payload"]["clientSeq"], 1)
        persisted_context = getPlaybackContextState("context-1")
        self.assertEqual(persisted_context["state"], "stopped")
        for cursor_name in ("epoch", "version", "queueRevision", "controlVersion"):
            self.assertEqual(persisted_context[cursor_name], 1)

    def test_playback_update_client_sequence_conflict_does_not_mutate_feedback(self):
        client = self.ready_strict_client()
        self.create_context(client, state="stopped", position_ms=0)
        update = {
            "type": "event",
            "action": "playback.update",
            "requestId": "playback-update-seq-1",
            "payload": {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "state": "playing",
                "positionMs": 10,
                "clientSeq": 1,
                "trackId": "song-2",
            },
        }
        client.emit("message", update, namespace="/emo")
        first = self.messages(client)
        conflicting = dict(update, requestId="playback-update-seq-conflict")
        conflicting["payload"] = dict(update["payload"], positionMs=20)

        client.emit("message", conflicting, namespace="/emo")
        response = self.messages(client)

        self.assertTrue(any(message["action"] == "playback.update" for message in first))
        self.assertEqual(len(response), 1)
        self.assertEqual(response[0]["payload"]["code"], "client_sequence_conflict")
        self.assertEqual(response[0]["payload"]["currentClientSeq"], 1)
        feedback = get_state().get_device_playback_state("context-1", "phone-1")
        self.assertEqual(feedback["positionMs"], 10)

    def test_device_list_is_sorted_and_contains_only_contract_fields(self):
        phone = self.connect()
        desktop = self.connect()
        self.authenticate(phone, request_id="auth-phone")
        self.authenticate(desktop, request_id="auth-desktop")
        with self.enable_all_profiles():
            self.register(
                phone,
                "register-phone",
                self.strict_registration_payload(
                    roles=["controller", "player"],
                    client_id="phone-1",
                    device_session_id="device:phone-1",
                ),
            )
            self.register(
                desktop,
                "register-desktop",
                self.strict_registration_payload(
                    client_id="desktop-1",
                    device_session_id="device:desktop-1",
                ),
            )
        self.messages(phone)

        phone.emit(
            "message",
            {
                "type": "state",
                "action": "device.list",
                "requestId": "device-list-1",
                "payload": {},
            },
            namespace="/emo",
        )

        response = self.messages(phone)
        self.assertEqual(len(response), 1)
        self.assertEqual(response[0]["action"], "device.list")
        self.assertEqual(response[0]["requestId"], "device-list-1")
        devices = response[0]["payload"]["devices"]
        self.assertEqual(
            [device["clientId"] for device in devices],
            ["desktop-1", "phone-1"],
        )
        allowed_fields = {
            "clientId",
            "deviceSessionId",
            "deviceName",
            "roles",
            "capabilities",
            "alias",
        }
        self.assertTrue(all(set(device) <= allowed_fields for device in devices))
        phone_device = next(
            device for device in devices if device["clientId"] == "phone-1"
        )
        self.assertEqual(phone_device["roles"], ["player", "controller"])
        self.assertEqual(
            set(phone_device["capabilities"]),
            set(self.strict_registration_payload()["capabilities"]),
        )

    def test_transport_message_limit_is_256_kib(self):
        self.assertEqual(socketio.server.eio.max_http_buffer_size, 256 * 1024)

    def test_context_create_persists_exact_initial_snapshot_and_subscribes(self):
        client = self.ready_strict_client()

        response = self.create_context(client)

        self.assertEqual(len(response), 1)
        snapshot = response[0]["payload"]
        self.assertEqual(response[0]["action"], "playback.context.create")
        self.assertEqual(snapshot["queueSongIds"], ["song-2", "song-1"])
        self.assertEqual(snapshot["state"], "playing")
        self.assertEqual(snapshot["positionMs"], 1200)
        for cursor_name in ("epoch", "version", "queueRevision", "controlVersion"):
            self.assertEqual(snapshot[cursor_name], 1)
        self.assertEqual(
            set(snapshot),
            {
                "playbackContextId",
                "authorityClientId",
                "queueSongIds",
                "currentIndex",
                "trackId",
                "state",
                "positionMs",
                "queueRevision",
                "controlVersion",
                "version",
                "epoch",
                "timelineId",
                "serverUpdatedAtMs",
            },
        )
        persisted = getPlaybackContextState("context-1")
        self.assertEqual(persisted["authorityDeviceSessionId"], "device:phone-1")
        self.assertRegex(persisted["creationFingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            len(get_state().list_playback_context_subscribers("context-1")),
            1,
        )

    def test_context_create_retry_after_runtime_reset_uses_persisted_intent(self):
        client = self.ready_strict_client()
        first = self.create_context(client, request_id="context-create-first")
        get_state()._playback_contexts.clear()

        replay = self.create_context(client, request_id="context-create-retry")

        self.assertEqual(replay[0]["payload"], first[0]["payload"])

    def test_context_create_with_same_id_and_different_intent_conflicts(self):
        client = self.ready_strict_client()
        self.create_context(client, request_id="context-create-first")

        conflict = self.create_context(
            client,
            request_id="context-create-conflict",
            queue_song_ids=["other-song"],
        )

        self.assertEqual(len(conflict), 1)
        error = conflict[0]
        self.assertEqual(error["payload"]["code"], "conflict")
        self.assertEqual(error["payload"]["playbackContextId"], "context-1")
        self.assertEqual(error["payload"]["currentVersion"], 1)
        self.assertEqual(error["payload"]["currentQueueRevision"], 1)
        self.assertEqual(error["payload"]["currentControlVersion"], 1)

    def test_closed_context_is_a_terminal_tombstone(self):
        client = self.ready_strict_client()
        self.create_context(client)
        client.emit(
            "message",
            {
                "type": "command",
                "action": "playback.context.close",
                "requestId": "context-close-1",
                "payload": {"playbackContextId": "context-1"},
            },
            namespace="/emo",
        )
        closed_messages = self.messages(client)
        self.assertEqual(
            [message["action"] for message in closed_messages],
            ["system.ack", "playback.context.closed"],
        )
        persisted = getPlaybackContextState("context-1")
        self.assertEqual(persisted["lifecycle"], "closed")
        self.assertEqual(persisted["state"], "playing")
        self.assertEqual(persisted["version"], 2)

        get_state()._playback_contexts.clear()
        recreate = self.create_context(client, request_id="context-recreate-1")
        self.assertEqual(recreate[0]["payload"]["code"], "context_closed")
        client.emit(
            "message",
            {
                "type": "state",
                "action": "playback.context.status",
                "requestId": "context-status-closed-1",
                "payload": {"playbackContextId": "context-1"},
            },
            namespace="/emo",
        )
        status = self.messages(client)
        self.assertEqual(status[0]["payload"]["code"], "context_closed")

    def test_queue_sync_advances_only_contract_cursors_and_rejects_stale_base(self):
        client = self.ready_strict_client()
        self.create_context(client)
        client.emit(
            "message",
            {
                "type": "state",
                "action": "queue.context.sync",
                "requestId": "queue-sync-1",
                "payload": {
                    "playbackContextId": "context-1",
                    "deviceSessionId": "device:phone-1",
                    "queueSongIds": ["song-3", "song-1"],
                    "currentIndex": 1,
                    "positionMs": 500,
                    "baseQueueRevision": 1,
                    "baseControlVersion": 1,
                },
            },
            namespace="/emo",
        )
        messages = self.messages(client)

        ack = next(message for message in messages if message["action"] == "system.ack")
        queue_push = next(
            message for message in messages if message["action"] == "queue.context.sync"
        )
        self.assertEqual(ack["payload"], {"action": "queue.context.sync"})
        self.assertNotIn("baseQueueRevision", queue_push["payload"])
        self.assertNotIn("baseControlVersion", queue_push["payload"])
        persisted = getPlaybackContextState("context-1")
        self.assertEqual(persisted["state"], "playing")
        self.assertEqual(persisted["epoch"], 1)
        self.assertEqual(persisted["version"], 2)
        self.assertEqual(persisted["queueRevision"], 2)
        self.assertEqual(persisted["controlVersion"], 2)

        stale = {
            "type": "state",
            "action": "queue.context.sync",
            "requestId": "queue-sync-stale",
            "payload": {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "queueSongIds": ["song-3", "song-1"],
                "currentIndex": 1,
                "positionMs": 500,
                "baseQueueRevision": 1,
            },
        }
        client.emit("message", stale, namespace="/emo")
        error = self.messages(client)[0]
        self.assertEqual(error["payload"]["code"], "stale_version")
        self.assertEqual(error["payload"]["currentQueueRevision"], 2)
        self.assertEqual(getPlaybackContextState("context-1")["version"], 2)

    def test_controller_control_routes_only_to_bound_authority_then_acks(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.connect()
        self.authenticate(controller, request_id="auth-controller")
        with self.enable_all_profiles():
            self.register(
                controller,
                "register-controller",
                self.strict_registration_payload(
                    roles=["controller"],
                    client_id="controller-1",
                    device_session_id="device:controller-1",
                ),
            )
        self.messages(player)
        self.messages(controller)

        controller.emit(
            "message",
            {
                "type": "command",
                "action": "player.seek",
                "requestId": "seek-1",
                "payload": {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                    "positionMs": 42000,
                },
            },
            namespace="/emo",
        )

        controller_messages = self.messages(controller)
        player_messages = self.messages(player)
        self.assertEqual(
            [message["action"] for message in controller_messages],
            ["system.ack"],
        )
        self.assertEqual(controller_messages[0]["payload"], {"action": "player.seek"})
        command = next(
            message for message in player_messages if message["action"] == "player.seek"
        )
        self.assertNotIn("requestId", command)
        self.assertNotIn("targetClientId", command)
        self.assertEqual(
            command["payload"],
            {
                "playbackContextId": "context-1",
                "controlVersion": 2,
                "sourceClientId": "controller-1",
                "positionMs": 42000,
            },
        )
        persisted = getPlaybackContextState("context-1")
        self.assertEqual(persisted["positionMs"], 42000)
        self.assertEqual(persisted["version"], 2)
        self.assertEqual(persisted["controlVersion"], 2)
        self.assertEqual(persisted["queueRevision"], 1)
        self.assertEqual(persisted["epoch"], 1)

    def test_queue_sync_cursor_matrix_and_closed_push_schema(self):
        client = self.ready_strict_client()
        self.create_context(client)
        scenarios = (
            ("content", ["song-2", "song-3"], 0, 1200, None, (2, 2, 1)),
            ("position", ["song-2", "song-3"], 0, 1300, 1, (3, 3, 2)),
            ("index", ["song-2", "song-3"], 1, 0, 2, (4, 4, 3)),
            ("no-op", ["song-2", "song-3"], 1, 0, None, (5, 5, 3)),
        )
        for name, queue, index, position, base_control, expected in scenarios:
            with self.subTest(name=name):
                current = getPlaybackContextState("context-1")
                payload = {
                    "playbackContextId": "context-1",
                    "deviceSessionId": "device:phone-1",
                    "queueSongIds": queue,
                    "currentIndex": index,
                    "positionMs": position,
                    "baseQueueRevision": current["queueRevision"],
                }
                if base_control is not None:
                    payload["baseControlVersion"] = base_control
                messages = self.emit_strict(
                    client,
                    "state",
                    "queue.context.sync",
                    "queue-%s" % name,
                    payload,
                )
                push = next(
                    message
                    for message in messages
                    if message["action"] == "queue.context.sync"
                )
                self.assertEqual(
                    set(push["payload"]),
                    {
                        "playbackContextId",
                        "authorityClientId",
                        "queueSongIds",
                        "currentIndex",
                        "positionMs",
                        "queueRevision",
                        "controlVersion",
                        "version",
                        "epoch",
                        "timelineId",
                        "serverUpdatedAtMs",
                    },
                )
                persisted = getPlaybackContextState("context-1")
                self.assertEqual(
                    (
                        persisted["version"],
                        persisted["queueRevision"],
                        persisted["controlVersion"],
                    ),
                    expected,
                )

        before = getPlaybackContextState("context-1")
        error = self.emit_strict(
            client,
            "state",
            "queue.context.sync",
            "queue-missing-control",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "queueSongIds": ["song-2", "song-3"],
                "currentIndex": 1,
                "positionMs": 1,
                "baseQueueRevision": before["queueRevision"],
            },
        )[0]
        self.assertEqual(error["payload"]["code"], "bad_request")
        after = getPlaybackContextState("context-1")
        self.assertEqual(
            tuple(after[field] for field in ("version", "queueRevision", "controlVersion")),
            tuple(before[field] for field in ("version", "queueRevision", "controlVersion")),
        )

    def test_all_core_controls_follow_cursor_matrix_and_wire_schema(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-1",
            device_session_id="device:controller-1",
        )
        self.messages(player)

        controls = (
            ("player.pause", {"baseControlVersion": 1}, (2, 1, 2, 0, "paused", 1200)),
            ("player.play", {"baseControlVersion": 2}, (3, 1, 3, 0, "playing", 1200)),
            (
                "player.seek",
                {"baseControlVersion": 3, "positionMs": 3000},
                (4, 1, 4, 0, "playing", 3000),
            ),
            ("player.next", {"baseControlVersion": 4}, (5, 2, 5, 1, "playing", 0)),
            ("player.prev", {"baseControlVersion": 5}, (6, 3, 6, 0, "playing", 0)),
            (
                "queue.playItem",
                {"baseControlVersion": 6, "baseQueueRevision": 3, "queueIndex": 1},
                (7, 4, 7, 1, "playing", 0),
            ),
        )
        for index, (action, action_payload, expected) in enumerate(controls):
            with self.subTest(action=action):
                payload = {"playbackContextId": "context-1"}
                payload.update(action_payload)
                requester_messages = self.emit_strict(
                    controller,
                    "command",
                    action,
                    "control-%d" % index,
                    payload,
                )
                self.assertEqual(
                    [message["action"] for message in requester_messages],
                    ["system.ack"],
                )
                authority_messages = self.messages(player)
                command = next(
                    message for message in authority_messages if message["action"] == action
                )
                self.assertNotIn("requestId", command)
                self.assertNotIn("targetClientId", command)
                self.assertNotIn("baseControlVersion", command["payload"])
                self.assertNotIn("baseQueueRevision", command["payload"])
                persisted = getPlaybackContextState("context-1")
                self.assertEqual(
                    (
                        persisted["version"],
                        persisted["queueRevision"],
                        persisted["controlVersion"],
                        persisted["currentIndex"],
                        persisted["state"],
                        persisted["positionMs"],
                    ),
                    expected,
                )

    def test_control_rejects_missing_capability_and_replaced_authority_device(self):
        player = self.ready_strict_client(
            capability_overrides={"canSeek": False},
        )
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-1",
            device_session_id="device:controller-1",
        )
        self.messages(player)

        capability_error = self.emit_strict(
            controller,
            "command",
            "player.seek",
            "seek-without-capability",
            {
                "playbackContextId": "context-1",
                "baseControlVersion": 1,
                "positionMs": 2000,
            },
        )[0]
        self.assertEqual(capability_error["payload"]["code"], "capability_required")
        self.assertEqual(getPlaybackContextState("context-1")["version"], 1)
        self.assertFalse(
            any(message["action"] == "player.seek" for message in self.messages(player))
        )

        self.ready_strict_client(
            client_id="phone-1",
            device_session_id="device:phone-replacement",
        )
        self.messages(controller)
        offline_error = self.emit_strict(
            controller,
            "command",
            "player.play",
            "play-after-device-replacement",
            {"playbackContextId": "context-1", "baseControlVersion": 1},
        )[0]
        self.assertEqual(offline_error["payload"]["code"], "authority_offline")
        self.assertEqual(getPlaybackContextState("context-1")["version"], 1)

    def test_cross_user_status_and_subscribe_are_forbidden(self):
        owner = self.ready_strict_client()
        self.create_context(owner)
        other = self.connect()
        self.authenticate(other, "bob", "B0b", "auth-bob")
        with self.enable_all_profiles():
            self.register(
                other,
                "register-bob",
                self.strict_registration_payload(
                    client_id="bob-phone",
                    device_session_id="device:bob-phone",
                ),
            )
        self.messages(other)

        for action in ("playback.context.status", "playback.context.subscribe"):
            with self.subTest(action=action):
                response = self.emit_strict(
                    other,
                    "state",
                    action,
                    "cross-user-%s" % action,
                    {"playbackContextId": "context-1"},
                )
                self.assertEqual(len(response), 1)
                self.assertEqual(response[0]["payload"]["code"], "forbidden")

    def test_context_close_notifies_and_stops_followers(self):
        owner = self.ready_strict_client()
        self.create_context(owner)
        follower = self.ready_strict_client(
            client_id="follower-1",
            device_session_id="device:follower-1",
        )
        self.emit_strict(
            follower,
            "command",
            "follow.start",
            "follow-start-1",
            {
                "sourcePlaybackContextId": "context-1",
                "deviceSessionId": "device:follower-1",
            },
        )
        relationship = get_state().get_follow_relationship("follower-1")
        self.assertEqual(relationship["sourcePlaybackContextId"], "context-1")
        self.messages(owner)

        self.emit_strict(
            owner,
            "command",
            "playback.context.close",
            "context-close-followers",
            {"playbackContextId": "context-1"},
        )
        follower_messages = self.messages(follower)
        self.assertTrue(
            any(message["action"] == "playback.context.closed" for message in follower_messages)
        )
        self.assertIsNone(get_state().get_follow_relationship("follower-1"))

    def test_queue_sync_commits_before_emit_and_recovers_after_push_failure(self):
        client = self.ready_strict_client()
        self.create_context(client)
        events = []
        real_mutation = emo_ws.mutateStrictPlaybackContextQueue
        real_emit = emo_ws._emit_message

        def record_mutation(*args, **kwargs):
            events.append("commit")
            return real_mutation(*args, **kwargs)

        def record_emit(*args, **kwargs):
            events.append("emit")
            return real_emit(*args, **kwargs)

        payload = {
            "playbackContextId": "context-1",
            "deviceSessionId": "device:phone-1",
            "queueSongIds": ["song-2", "song-3"],
            "currentIndex": 0,
            "positionMs": 1200,
            "baseQueueRevision": 1,
        }
        with mock.patch.object(
            emo_ws,
            "mutateStrictPlaybackContextQueue",
            side_effect=record_mutation,
        ), mock.patch.object(emo_ws, "_emit_message", side_effect=record_emit):
            self.emit_strict(
                client,
                "state",
                "queue.context.sync",
                "queue-ordering",
                payload,
            )
        self.assertEqual(events[0], "commit")
        self.assertIn("emit", events[1:])

        failed_push_payload = dict(
            payload,
            queueSongIds=["song-2", "song-4"],
            baseQueueRevision=2,
        )
        with mock.patch.object(
            emo_ws,
            "_broadcast_context_queue_v2",
            side_effect=RuntimeError("injected post-commit emit failure"),
        ):
            response = self.emit_strict(
                client,
                "state",
                "queue.context.sync",
                "queue-push-failure",
                failed_push_payload,
            )
        self.assertEqual(
            [message["action"] for message in response],
            ["system.ack"],
        )
        persisted = getPlaybackContextState("context-1")
        self.assertEqual(persisted["queueSongIds"], ["song-2", "song-4"])
        self.assertEqual(persisted["version"], 3)

        status = self.emit_strict(
            client,
            "state",
            "playback.context.status",
            "status-after-push-failure",
            {"playbackContextId": "context-1"},
        )
        self.assertEqual(len(status), 1)
        self.assertEqual(
            status[0]["payload"]["playbackContext"]["queueSongIds"],
            ["song-2", "song-4"],
        )


if __name__ == "__main__":
    unittest.main()
