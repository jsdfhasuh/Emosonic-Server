import base64
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from supysonic.db import release_database
from supysonic.emo import ws as emo_ws
from supysonic.emo.strict_v2_acceptance import (
    FAULT_DIRECTORY_ENV,
    arm_binding_emit_failure,
)
from supysonic.emo.strict_v2_contract import (
    StrictOutputValidationError,
    validate_strict_output,
)
from supysonic.emo.strict_v2_safety import strict_v2_safety
from supysonic.emo.ws import (
    begin_strict_v2_shutdown,
    socketio,
    strict_request_cache,
)
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
        state._device_volume_states.clear()
        state._strict_device_volume_sequences.clear()
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
            message = args[0] if isinstance(args, list) else args
            payload = message.get("payload") if isinstance(message, dict) else None
            if "connectionNonce" in message or (
                message.get("action") == "system.ack"
                and isinstance(payload, dict)
                and "strictV2" in payload
            ):
                validate_strict_output(message)
            messages.append(message)
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
        with mock.patch(
            "supysonic.emo.ws_store._new_playback_context_id",
            return_value="context-1",
        ):
            client.emit(
                "message",
                {
                    "type": "command",
                    "action": "playback.context.ensure",
                    "requestId": request_id,
                    "payload": {
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

    def test_device_volume_controls_online_idle_player_and_reports_actual_state(self):
        player = self.ready_strict_client(
            client_id="player-1",
            device_session_id="device:player-1",
            capability_overrides={"remoteVolumeControl": True},
        )
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-1",
            device_session_id="device:controller-1",
            capability_overrides={
                "canSetVolume": False,
                "remoteVolumeControl": True,
            },
        )
        self.messages(player)
        self.messages(controller)

        controller_messages = self.emit_strict(
            controller,
            "command",
            "device.setVolume",
            "device-volume-command-1",
            {
                "targetClientId": "player-1",
                "targetDeviceSessionId": "device:player-1",
                "volume": 65,
            },
        )
        player_messages = self.messages(player)

        self.assertEqual(
            [message["action"] for message in controller_messages],
            ["system.ack"],
        )
        command = next(
            message
            for message in player_messages
            if message["action"] == "device.setVolume"
        )
        self.assertEqual(
            command["payload"],
            {"sourceClientId": "controller-1", "volume": 65},
        )
        self.assertNotIn("requestId", command)
        self.assertEqual(get_state()._playback_contexts, {})

        replay_messages = self.emit_strict(
            controller,
            "command",
            "device.setVolume",
            "device-volume-command-1",
            {
                "targetClientId": "player-1",
                "targetDeviceSessionId": "device:player-1",
                "volume": 65,
            },
        )
        self.assertEqual(
            [message["action"] for message in replay_messages],
            ["system.ack"],
        )
        self.assertFalse(
            any(
                message["action"] == "device.setVolume"
                for message in self.messages(player)
            )
        )

        player_messages = self.emit_strict(
            player,
            "event",
            "device.volume.update",
            "device-volume-feedback-1",
            {
                "deviceSessionId": "device:player-1",
                "volume": 64,
                "clientSeq": 1,
            },
        )
        controller_pushes = self.messages(controller)
        confirmation = next(
            message
            for message in player_messages
            if message["action"] == "device.volume.update"
        )
        controller_update = next(
            message
            for message in controller_pushes
            if message["action"] == "device.volume.update"
        )
        self.assertNotIn("requestId", confirmation)
        self.assertEqual(confirmation["payload"]["volume"], 64)
        self.assertEqual(controller_update["payload"], confirmation["payload"])
        self.assertEqual(
            get_state().get_device_volume_state(
                "alice", "player-1", "device:player-1"
            )["volume"],
            64,
        )

        device_list = self.emit_strict(
            controller,
            "state",
            "device.list",
            "device-list-volume-1",
            {},
        )[0]
        listed_player = next(
            device
            for device in device_list["payload"]["devices"]
            if device["clientId"] == "player-1"
        )
        self.assertEqual(listed_player["volumeState"]["volume"], 64)
        self.assertIn(
            "remoteVolumeControl",
            listed_player["capabilities"],
        )

    def test_device_volume_requires_extended_capability_and_exact_live_pair(self):
        base_player = self.ready_strict_client(
            client_id="player-1",
            device_session_id="device:player-1",
        )
        base_controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-1",
            device_session_id="device:controller-1",
        )
        self.messages(base_player)
        self.messages(base_controller)

        missing_requester_capability = self.emit_strict(
            base_controller,
            "command",
            "device.setVolume",
            "device-volume-base-1",
            {
                "targetClientId": "player-1",
                "targetDeviceSessionId": "device:player-1",
                "volume": 50,
            },
        )[0]
        self.assertEqual(
            missing_requester_capability["payload"]["code"],
            "capability_required",
        )

        extended_controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-2",
            device_session_id="device:controller-2",
            capability_overrides={
                "canSetVolume": False,
                "remoteVolumeControl": True,
            },
        )
        self.messages(extended_controller)
        missing_target_capability = self.emit_strict(
            extended_controller,
            "command",
            "device.setVolume",
            "device-volume-target-cap-1",
            {
                "targetClientId": "player-1",
                "targetDeviceSessionId": "device:player-1",
                "volume": 50,
            },
        )[0]
        wrong_device = self.emit_strict(
            extended_controller,
            "command",
            "device.setVolume",
            "device-volume-wrong-pair-1",
            {
                "targetClientId": "player-1",
                "targetDeviceSessionId": "device:other",
                "volume": 50,
            },
        )[0]

        self.assertEqual(
            missing_target_capability["payload"]["code"],
            "capability_required",
        )
        self.assertEqual(wrong_device["payload"]["code"], "not_found")

    def test_device_volume_does_not_mutate_active_playback_context(self):
        player = self.ready_strict_client(
            capability_overrides={"remoteVolumeControl": True},
        )
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-1",
            device_session_id="device:controller-1",
            capability_overrides={
                "canSetVolume": False,
                "remoteVolumeControl": True,
            },
        )
        self.messages(player)
        self.messages(controller)
        before = getPlaybackContextState("context-1")

        self.emit_strict(
            controller,
            "command",
            "device.setVolume",
            "device-volume-active-1",
            {
                "targetClientId": "phone-1",
                "targetDeviceSessionId": "device:phone-1",
                "volume": 40,
            },
        )
        self.messages(player)
        after = getPlaybackContextState("context-1")

        for field_name in (
            "volume",
            "version",
            "controlVersion",
            "queueRevision",
            "epoch",
            "state",
            "positionMs",
        ):
            self.assertEqual(after[field_name], before[field_name])

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

    def test_pre_register_context_list_is_unauthorized_without_provenance(self):
        client = self.connect()
        self.authenticate(client)
        request_message = {
            "type": "state",
            "action": "playback.context.list",
            "requestId": "context-list-before-register",
            "payload": {},
        }

        client.emit("message", request_message, namespace="/emo")
        error = self.messages(client)[0]

        self.assertEqual(error["payload"]["action"], "playback.context.list")
        self.assertEqual(error["payload"]["code"], "unauthorized")
        self.assertNotIn("connectionNonce", error)
        self.assertNotIn("connectionEpoch", error)
        self.assertEqual(
            validate_strict_output(error, registered=False),
            error,
        )

        client.emit("message", request_message, namespace="/emo")
        replay = self.messages(client)
        self.assertEqual(replay, [error])

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

    def test_non_testing_deployment_requires_dual_local_evidence_gate(self):
        client = self.connect()
        self.authenticate(client)
        self.app.testing = False
        self.app.config["WEBAPP"].update(
            {
                "emo_strict_v2_core_enabled": True,
                "emo_strict_v2_follow_enabled": True,
                "emo_strict_v2_handoff_enabled": True,
                "emo_strict_v2_broadcast_enabled": True,
                "emo_strict_v2_allow_local_test_evidence": True,
                "emo_development_mode": False,
            }
        )

        try:
            rejected = self.register(
                client,
                "register-local-evidence-rejected",
                self.strict_registration_payload(),
            )
            error = next(
                message
                for message in rejected
                if message.get("requestId")
                == "register-local-evidence-rejected"
            )
            self.assertEqual(error["action"], "system.error")
            self.assertEqual(error["payload"]["code"], "not_supported")

            self.app.config["WEBAPP"]["emo_development_mode"] = True
            accepted = self.register(
                client,
                "register-local-evidence-accepted",
                self.strict_registration_payload(),
            )
            ack = next(
                message
                for message in accepted
                if message.get("requestId")
                == "register-local-evidence-accepted"
            )
            self.assertEqual(ack["action"], "system.ack")
            self.assertTrue(
                all(ack["payload"]["negotiatedCapabilities"].values())
            )
        finally:
            self.app.testing = True

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

    def test_each_physical_socket_uses_distinct_128_bit_nonce_and_epoch(self):
        nonces = []
        for index in range(2):
            client_id = "nonce-client-%d" % index
            client = self.connect()
            self.authenticate(
                client,
                request_id="auth-%s" % client_id,
            )
            with self.enable_all_profiles():
                response = self.register(
                    client,
                    "register-%s" % client_id,
                    self.strict_registration_payload(
                        client_id=client_id,
                        device_session_id="device:%s" % client_id,
                    ),
                )
            ack = next(
                message
                for message in response
                if message.get("requestId") == "register-%s" % client_id
            )
            nonce = ack["connectionNonce"]
            metadata = ack["payload"]["strictV2"]
            decoded = base64.urlsafe_b64decode(
                nonce + "=" * (-len(nonce) % 4)
            )

            self.assertGreaterEqual(len(decoded), 16)
            self.assertEqual(ack["connectionEpoch"], 1)
            self.assertEqual(metadata["connectionNonce"], nonce)
            self.assertEqual(metadata["connectionEpoch"], 1)
            nonces.append(nonce)

        self.assertEqual(len(set(nonces)), 2)

    def test_code_disabled_optional_profiles_override_enabled_deployment(self):
        client = self.connect()
        self.authenticate(client)
        self.app.config["WEBAPP"].update(
            {
                "emo_strict_v2_core_enabled": True,
                "emo_strict_v2_follow_enabled": True,
                "emo_strict_v2_handoff_enabled": True,
                "emo_strict_v2_broadcast_enabled": True,
            }
        )
        core_only_readiness = {
            "core": True,
            "follow": False,
            "handoff": False,
            "broadcast": False,
        }

        with mock.patch(
            "supysonic.emo.strict_v2_readiness.get_code_conformance_readiness",
            return_value=core_only_readiness,
        ):
            response = self.register(
                client,
                "register-packaged-optional-false-1",
                self.strict_registration_payload(["player", "controller"]),
            )

        ack = next(
            message
            for message in response
            if message.get("requestId") == "register-packaged-optional-false-1"
        )
        negotiated = ack["payload"]["negotiatedCapabilities"]
        self.assertTrue(negotiated["playbackContextV2"])
        self.assertFalse(negotiated["supportsFollow"])
        self.assertFalse(negotiated["playbackPrepare"])
        self.assertFalse(negotiated["effectiveAtPlayback"])
        self.assertFalse(negotiated["supportsBroadcast"])

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
            response = self.register(
                new_client,
                "register-new",
                self.strict_registration_payload(device_session_id="device:new"),
            )

        self.assertEqual(response[0]["action"], "system.ack")
        self.assertEqual(response[0]["requestId"], "register-new")
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

    def test_register_repeated_100_times_replays_without_second_mutation(self):
        client = self.connect()
        self.authenticate(client)
        payload = self.strict_registration_payload()

        with self.enable_all_profiles(), mock.patch(
            "supysonic.emo.ws._register_device",
            wraps=emo_ws._register_device,
        ) as register_device:
            responses = [
                self.register(client, "register-replay-1", payload)
                for _attempt in range(100)
            ]

        first_ack = next(
            message
            for message in responses[0]
            if message.get("requestId") == "register-replay-1"
        )
        self.assertTrue(
            all(response == [first_ack] for response in responses[1:])
        )
        register_device.assert_called_once()

    def test_register_post_ack_push_failure_does_not_emit_second_settlement(self):
        client = self.connect()
        self.authenticate(client)
        payload = self.strict_registration_payload()

        with self.enable_all_profiles(), mock.patch.object(
            emo_ws,
            "_register_device",
            wraps=emo_ws._register_device,
        ) as register_device, mock.patch.object(
            emo_ws,
            "_broadcast_clients",
            side_effect=RuntimeError("injected post-ACK device-list failure"),
        ) as broadcast_clients:
            first = self.register(
                client,
                "register-post-ack-failure",
                payload,
            )
            retry = self.register(
                client,
                "register-post-ack-failure",
                payload,
            )

        self.assertEqual([message["action"] for message in first], ["system.ack"])
        self.assertEqual([message["action"] for message in retry], ["system.ack"])
        self.assertEqual(retry[0], first[0])
        self.assertEqual(register_device.call_count, 1)
        self.assertEqual(broadcast_clients.call_count, 1)

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

    def test_playback_update_push_failure_replays_persisted_confirmation(self):
        client = self.ready_strict_client()
        self.create_context(client, state="stopped", position_ms=0)
        request_id = "playback-update-push-failure"
        update = {
            "type": "event",
            "action": "playback.update",
            "requestId": request_id,
            "payload": {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "state": "playing",
                "positionMs": 25,
                "clientSeq": 1,
                "trackId": "song-2",
            },
        }
        settled_before_push = []

        def fail_after_settlement(*_args, **_kwargs):
            settled_before_push.append(
                any(
                    cached_request_id == request_id and entry.result is not None
                    for (_nonce, cached_request_id), entry
                    in strict_request_cache._entries.items()
                )
            )
            raise RuntimeError("injected feedback push failure")

        with mock.patch.object(
            emo_ws,
            "_broadcast_v2_playback_update",
            side_effect=fail_after_settlement,
        ):
            client.emit("message", update, namespace="/emo")

        self.assertEqual(settled_before_push, [True])
        self.assertEqual(self.messages(client), [])
        feedback = get_state().get_device_playback_state(
            "context-1",
            "phone-1",
        )
        self.assertEqual(feedback["positionMs"], 25)
        self.assertEqual(feedback["clientSeq"], 1)

        client.emit("message", update, namespace="/emo")
        replay = self.messages(client)
        self.assertEqual(
            [message["action"] for message in replay],
            ["playback.update"],
        )
        self.assertEqual(replay[0]["payload"]["positionMs"], 25)
        self.assertEqual(replay[0]["payload"]["clientSeq"], 1)

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
                "clientSeq": 2,
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
        self.assertEqual(response[0]["payload"]["currentClientSeq"], 2)
        feedback = get_state().get_device_playback_state("context-1", "phone-1")
        self.assertEqual(feedback["positionMs"], 10)

        stale = dict(update, requestId="playback-update-seq-stale")
        stale["payload"] = dict(update["payload"], clientSeq=1)
        client.emit("message", stale, namespace="/emo")
        stale_response = self.messages(client)

        self.assertEqual(len(stale_response), 1)
        self.assertEqual(
            stale_response[0]["payload"]["code"],
            "client_sequence_conflict",
        )
        self.assertEqual(stale_response[0]["payload"]["currentClientSeq"], 2)
        self.assertEqual(
            get_state().get_device_playback_state("context-1", "phone-1")[
                "positionMs"
            ],
            10,
        )

    def test_playback_update_same_sequence_and_content_is_idempotent(self):
        client = self.ready_strict_client()
        self.create_context(client, state="stopped", position_ms=0)
        payload = {
            "playbackContextId": "context-1",
            "deviceSessionId": "device:phone-1",
            "state": "playing",
            "positionMs": 25,
            "clientSeq": 1,
            "trackId": "song-2",
        }

        first = self.emit_strict(
            client,
            "event",
            "playback.update",
            "playback-update-seq-idempotent-1",
            payload,
        )
        first_feedback = get_state().get_device_playback_state(
            "context-1",
            "phone-1",
        )
        second = self.emit_strict(
            client,
            "event",
            "playback.update",
            "playback-update-seq-idempotent-2",
            payload,
        )
        second_feedback = get_state().get_device_playback_state(
            "context-1",
            "phone-1",
        )

        self.assertEqual([message["action"] for message in first], ["playback.update"])
        self.assertEqual([message["action"] for message in second], ["playback.update"])
        self.assertEqual(first[0]["payload"], second[0]["payload"])
        self.assertEqual(
            first_feedback["serverUpdatedAtMs"],
            second_feedback["serverUpdatedAtMs"],
        )
        self.assertEqual(first_feedback["clientSeq"], 1)
        persisted_context = getPlaybackContextState("context-1")
        for cursor_name in ("epoch", "version", "queueRevision", "controlVersion"):
            self.assertEqual(persisted_context[cursor_name], 1)

    def test_playback_update_sequence_restarts_on_new_connection_nonce(self):
        client = self.ready_strict_client()
        self.create_context(client, state="stopped", position_ms=0)
        first_sid = get_state().get_sid_for_client("phone-1", user_name="alice")
        first_nonce = get_state().get_session(first_sid)["connectionNonce"]
        first_update = self.emit_strict(
            client,
            "event",
            "playback.update",
            "playback-update-before-reconnect",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "state": "playing",
                "positionMs": 30,
                "clientSeq": 7,
                "trackId": "song-2",
            },
        )
        self.assertEqual(
            [message["action"] for message in first_update],
            ["playback.update"],
        )
        client.disconnect(namespace="/emo")

        replacement = self.ready_strict_client()
        replacement_sid = get_state().get_sid_for_client(
            "phone-1",
            user_name="alice",
        )
        replacement_session = get_state().get_session(replacement_sid)
        self.assertNotEqual(replacement_session["connectionNonce"], first_nonce)
        restarted = self.emit_strict(
            replacement,
            "event",
            "playback.update",
            "playback-update-after-reconnect",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "state": "playing",
                "positionMs": 40,
                "clientSeq": 1,
                "trackId": "song-2",
            },
        )

        self.assertEqual(
            [message["action"] for message in restarted],
            ["playback.update"],
        )
        feedback = get_state().get_device_playback_state("context-1", "phone-1")
        self.assertEqual(feedback["clientSeq"], 1)
        self.assertEqual(feedback["positionMs"], 40)

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
        self.assertEqual(socketio.server.eio.ping_interval, 25)
        self.assertEqual(socketio.server.eio.ping_timeout, 20)

    def test_production_origin_defaults_to_same_origin(self):
        self.assertIsNone(socketio.server.eio.cors_allowed_origins)
        rejected = self.http_client.get(
            "/emo/ws/?EIO=4&transport=polling",
            headers={"Origin": "https://untrusted.example"},
        )
        self.assertEqual(rejected.status_code, 400)

        accepted = self.http_client.get(
            "/emo/ws/?EIO=4&transport=polling",
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(accepted.status_code, 200)

    def test_eleventh_unauthenticated_connection_from_ip_is_rejected(self):
        clients = [self.connect() for _ in range(11)]

        self.assertTrue(all(client.is_connected("/emo") for client in clients[:10]))
        self.assertFalse(clients[10].is_connected("/emo"))

    def test_twenty_first_authenticated_connection_for_user_is_rejected(self):
        for index in range(20):
            client = self.connect()
            response = self.authenticate(
                client,
                request_id="auth-limit-%d" % index,
            )
            self.assertEqual(response[0]["action"], "system.ack")

        overflow = self.connect()
        overflow.emit(
            "message",
            {
                "type": "auth",
                "action": "auth.login",
                "requestId": "auth-limit-overflow",
                "payload": {"u": "alice", "p": "Alic3"},
            },
            namespace="/emo",
        )
        self.assertFalse(overflow.is_connected("/emo"))

    def test_rate_limit_settles_before_context_mutation(self):
        client = self.ready_strict_client()
        safety_config = dict(self.app.config["WEBAPP"])
        safety_config["emo_strict_creates_per_connection_per_minute"] = 1
        strict_v2_safety.configure(safety_config)
        self.create_context(client)

        response = self.emit_strict(
            client,
            "command",
            "playback.context.ensure",
            "context-ensure-rate-limited",
            {
                "deviceSessionId": "device:phone-1",
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "positionMs": 0,
                "state": "stopped",
            },
        )

        self.assertEqual(len(response), 1)
        error = response[0]
        self.assertEqual(error["payload"]["code"], "rate_limited")
        self.assertTrue(error["payload"]["retryable"])
        self.assertGreater(error["payload"]["retryAfterMs"], 0)
        self.assertEqual(
            getPlaybackContextState("context-1")["controlVersion"],
            1,
        )

    def test_graceful_shutdown_rejects_new_connections_and_closes_existing(self):
        client = self.ready_strict_client()

        with self.app.app_context():
            self.assertTrue(begin_strict_v2_shutdown(0))

        self.assertFalse(client.is_connected("/emo"))
        replacement = self.connect()
        self.assertFalse(replacement.is_connected("/emo"))

    def test_shutdown_rejects_heartbeat_as_a_new_strict_request(self):
        client = self.ready_strict_client()
        self.assertTrue(strict_v2_safety.begin_shutdown(0))

        response = self.emit_strict(
            client,
            "system",
            "system.ping",
            "ping-during-shutdown",
            {},
        )

        self.assertEqual(len(response), 1)
        self.assertEqual(response[0]["action"], "system.error")
        self.assertEqual(response[0]["payload"]["code"], "internal_error")

    def test_internal_error_log_is_diagnostic_but_does_not_include_exception_text(self):
        client = self.ready_strict_client()
        exception_text = "password=Alic3 path=/private/music.db"

        with mock.patch.object(
            emo_ws,
            "_handle_playback_context_ensure",
            side_effect=RuntimeError(exception_text),
        ), self.assertLogs("supysonic.emo.ws", level="ERROR") as captured:
            response = self.create_context(
                client,
                request_id="context-internal-error",
            )

        self.assertEqual(response[0]["payload"]["code"], "internal_error")
        combined = "\n".join(captured.output)
        self.assertIn("exception_type=RuntimeError", combined)
        self.assertIn("client_request_id=context-internal-error", combined)
        self.assertNotIn(exception_text, combined)
        self.assertNotIn("Alic3", combined)

    def test_context_ensure_persists_exact_initial_snapshot_and_subscribes(self):
        client = self.ready_strict_client()

        response = self.create_context(client)

        self.assertEqual(len(response), 1)
        snapshot = response[0]["payload"]
        self.assertEqual(response[0]["action"], "playback.context.ensure")
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
        self.assertIsNone(persisted["creationFingerprint"])
        self.assertEqual(
            len(get_state().list_playback_context_subscribers("context-1")),
            1,
        )

    def test_context_ensure_creates_idle_initializes_and_rebinds_same_id(self):
        player = self.ready_strict_client()
        with mock.patch(
            "supysonic.emo.ws_store._new_playback_context_id",
            return_value="context-idle-1",
        ):
            idle = self.emit_strict(
                player,
                "command",
                "playback.context.ensure",
                "ensure-idle-1",
                {
                    "deviceSessionId": "device:phone-1",
                    "queueSongIds": [],
                    "positionMs": 0,
                    "state": "idle",
                },
            )

        self.assertEqual([message["action"] for message in idle], ["playback.context.ensure"])
        idle_snapshot = idle[0]["payload"]
        self.assertEqual(idle_snapshot["playbackContextId"], "context-idle-1")
        self.assertEqual(idle_snapshot["state"], "idle")
        self.assertNotIn("currentIndex", idle_snapshot)
        self.assertNotIn("trackId", idle_snapshot)

        initialized = self.emit_strict(
            player,
            "command",
            "playback.context.ensure",
            "ensure-initialize-1",
            {
                "deviceSessionId": "device:phone-1",
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "positionMs": 100,
                "state": "paused",
            },
        )
        initialized_snapshot = initialized[0]["payload"]
        self.assertEqual(initialized_snapshot["playbackContextId"], "context-idle-1")
        self.assertEqual(initialized_snapshot["controlVersion"], 2)
        self.assertEqual(initialized_snapshot["queueRevision"], 2)
        self.assertEqual(initialized_snapshot["version"], 2)

        player.disconnect(namespace="/emo")
        replacement = self.ready_strict_client(
            client_id="phone-1",
            device_session_id="device:phone-2",
        )
        rebound = self.emit_strict(
            replacement,
            "command",
            "playback.context.ensure",
            "ensure-rebind-1",
            {
                "deviceSessionId": "device:phone-2",
                "queueSongIds": [],
                "positionMs": 0,
                "state": "idle",
            },
        )
        rebound_snapshot = rebound[0]["payload"]
        self.assertEqual(rebound_snapshot["playbackContextId"], "context-idle-1")
        self.assertEqual(rebound_snapshot["epoch"], 2)
        self.assertEqual(rebound_snapshot["controlVersion"], 3)
        self.assertEqual(rebound_snapshot["queueSongIds"], ["song-1"])

    def test_idle_context_control_fails_with_queue_required_without_routing(self):
        player = self.ready_strict_client()
        with mock.patch(
            "supysonic.emo.ws_store._new_playback_context_id",
            return_value="context-idle-control",
        ):
            self.emit_strict(
                player,
                "command",
                "playback.context.ensure",
                "ensure-idle-control",
                {
                    "deviceSessionId": "device:phone-1",
                    "queueSongIds": [],
                    "positionMs": 0,
                    "state": "idle",
                },
            )
        self.messages(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-1",
            device_session_id="device:controller-1",
        )

        response = self.emit_strict(
            controller,
            "command",
            "player.play",
            "play-idle-1",
            {
                "playbackContextId": "context-idle-control",
                "baseControlVersion": 1,
            },
        )

        self.assertEqual([message["action"] for message in response], ["system.error"])
        error = response[0]["payload"]
        self.assertEqual(error["code"], "queue_required")
        self.assertEqual(error["playbackContextId"], "context-idle-control")
        self.assertEqual(error["currentControlVersion"], 1)
        self.assertEqual(error["currentQueueRevision"], 1)
        self.assertEqual(error["currentVersion"], 1)
        self.assertEqual(self.messages(player), [])

    def test_context_prepare_routes_once_and_prepared_ready_is_event_confirmed(self):
        player = self.ready_strict_client()
        with mock.patch(
            "supysonic.emo.ws_store._new_playback_context_id",
            return_value="context-prepare-1",
        ):
            self.emit_strict(
                player,
                "command",
                "playback.context.ensure",
                "ensure-prepare-1",
                {
                    "deviceSessionId": "device:phone-1",
                    "queueSongIds": [],
                    "positionMs": 0,
                    "state": "idle",
                },
            )
        self.messages(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-1",
            device_session_id="device:controller-1",
        )
        self.emit_strict(
            controller,
            "state",
            "playback.context.subscribe",
            "subscribe-prepare-1",
            {"playbackContextId": "context-prepare-1"},
        )

        with mock.patch.object(socketio, "start_background_task"):
            response = self.emit_strict(
                controller,
                "command",
                "playback.context.prepare",
                "prepare-1",
                {
                    "playbackContextId": "context-prepare-1",
                    "intentId": "intent-1",
                    "baseControlVersion": 1,
                    "initialQueueSongIds": ["song-1"],
                    "currentIndex": 0,
                    "positionMs": 0,
                },
            )

        self.assertTrue(any(message["action"] == "system.ack" for message in response))
        self.assertEqual(response[0]["payload"]["status"], "preparing")
        routed = self.messages(player)
        self.assertEqual([message["action"] for message in routed], ["playback.context.prepare"])
        self.assertEqual(routed[0]["payload"]["sourceClientId"], "controller-1")

        with mock.patch.object(socketio, "start_background_task"):
            replay = self.emit_strict(
                controller,
                "command",
                "playback.context.prepare",
                "prepare-1-retry",
                {
                    "playbackContextId": "context-prepare-1",
                    "intentId": "intent-1",
                    "baseControlVersion": 1,
                    "initialQueueSongIds": ["song-1"],
                    "currentIndex": 0,
                    "positionMs": 0,
                },
            )
        self.assertEqual(replay[0]["payload"]["status"], "preparing")
        self.assertEqual(self.messages(player), [])

        self.emit_strict(
            player,
            "state",
            "queue.context.sync",
            "queue-prepare-1",
            {
                "playbackContextId": "context-prepare-1",
                "deviceSessionId": "device:phone-1",
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "positionMs": 0,
                "baseQueueRevision": 1,
                "baseControlVersion": 1,
            },
        )
        self.messages(controller)

        confirmation = self.emit_strict(
            player,
            "event",
            "playback.context.prepared",
            "prepared-1",
            {
                "playbackContextId": "context-prepare-1",
                "deviceSessionId": "device:phone-1",
                "intentId": "intent-1",
                "ready": True,
            },
        )
        self.assertEqual(
            [message["action"] for message in confirmation],
            ["playback.context.prepared"],
        )
        self.assertTrue(confirmation[0]["payload"]["ready"])
        self.assertEqual(confirmation[0]["payload"]["controlVersion"], 2)
        controller_events = self.messages(controller)
        self.assertTrue(
            any(
                message["action"] == "playback.context.prepared"
                and message["payload"]["ready"] is True
                for message in controller_events
            )
        )

    def test_context_prepare_on_queue_backed_context_acks_ready_without_route(self):
        player = self.ready_strict_client()
        self.create_context(player)
        self.messages(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-ready",
            device_session_id="device:controller-ready",
        )

        response = self.emit_strict(
            controller,
            "command",
            "playback.context.prepare",
            "prepare-already-ready",
            {
                "playbackContextId": "context-1",
                "intentId": "intent-already-ready",
                "baseControlVersion": 1,
            },
        )

        self.assertEqual(response[0]["action"], "system.ack")
        self.assertEqual(response[0]["payload"]["status"], "ready")
        self.assertEqual(self.messages(player), [])
        terminal = emo_ws.getPlaybackPrepareTransaction(
            "context-1",
            1,
            "intent-already-ready",
        )
        self.assertEqual(terminal["status"], "ready")

    def test_context_prepare_timeout_persists_failed_canonical_result(self):
        player = self.ready_strict_client()
        with mock.patch(
            "supysonic.emo.ws_store._new_playback_context_id",
            return_value="context-prepare-timeout",
        ):
            self.emit_strict(
                player,
                "command",
                "playback.context.ensure",
                "ensure-prepare-timeout",
                {
                    "deviceSessionId": "device:phone-1",
                    "queueSongIds": [],
                    "positionMs": 0,
                    "state": "idle",
                },
            )
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-timeout",
            device_session_id="device:controller-timeout",
        )
        self.emit_strict(
            controller,
            "state",
            "playback.context.subscribe",
            "subscribe-timeout",
            {"playbackContextId": "context-prepare-timeout"},
        )
        with mock.patch.object(socketio, "start_background_task"):
            self.emit_strict(
                controller,
                "command",
                "playback.context.prepare",
                "prepare-timeout",
                {
                    "playbackContextId": "context-prepare-timeout",
                    "intentId": "intent-timeout",
                    "baseControlVersion": 1,
                },
            )
        self.messages(player)

        with mock.patch.object(socketio, "sleep"):
            emo_ws._expire_context_prepare_later(
                "context-prepare-timeout",
                1,
                "intent-timeout",
            )

        terminal = emo_ws.getPlaybackPrepareTransaction(
            "context-prepare-timeout",
            1,
            "intent-timeout",
        )
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["errorCode"], "prepare_timeout")
        events = self.messages(controller)
        self.assertTrue(
            any(
                message["action"] == "playback.context.prepared"
                and message["payload"]["errorCode"] == "prepare_timeout"
                for message in events
            )
        )

    def test_context_rebind_settles_pending_prepare_as_authority_changed(self):
        player = self.ready_strict_client()
        with mock.patch(
            "supysonic.emo.ws_store._new_playback_context_id",
            return_value="context-prepare-rebind",
        ):
            self.emit_strict(
                player,
                "command",
                "playback.context.ensure",
                "ensure-prepare-rebind",
                {
                    "deviceSessionId": "device:phone-1",
                    "queueSongIds": [],
                    "positionMs": 0,
                    "state": "idle",
                },
            )
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-rebind",
            device_session_id="device:controller-rebind",
        )
        self.emit_strict(
            controller,
            "state",
            "playback.context.subscribe",
            "subscribe-prepare-rebind",
            {"playbackContextId": "context-prepare-rebind"},
        )
        with mock.patch.object(socketio, "start_background_task"):
            self.emit_strict(
                controller,
                "command",
                "playback.context.prepare",
                "prepare-rebind",
                {
                    "playbackContextId": "context-prepare-rebind",
                    "intentId": "intent-rebind",
                    "baseControlVersion": 1,
                },
            )
        self.messages(player)
        self.messages(controller)
        player.disconnect(namespace="/emo")

        replacement = self.ready_strict_client(
            client_id="phone-1",
            device_session_id="device:phone-2",
        )
        self.emit_strict(
            replacement,
            "command",
            "playback.context.ensure",
            "ensure-rebind-terminal",
            {
                "deviceSessionId": "device:phone-2",
                "queueSongIds": [],
                "positionMs": 0,
                "state": "idle",
            },
        )

        terminal = emo_ws.getPlaybackPrepareTransaction(
            "context-prepare-rebind",
            1,
            "intent-rebind",
        )
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["errorCode"], "authority_changed")
        events = self.messages(controller)
        self.assertTrue(
            any(
                message["action"] == "playback.context.prepared"
                and message["payload"]["errorCode"] == "authority_changed"
                for message in events
            )
        )

    def test_remote_control_persists_pending_deadline_and_watchdog_settles_unknown(self):
        player = self.ready_strict_client()
        self.create_context(player)
        self.messages(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-control",
            device_session_id="device:controller-control",
        )
        self.emit_strict(
            controller,
            "state",
            "playback.context.subscribe",
            "subscribe-control",
            {"playbackContextId": "context-1"},
        )
        self.messages(controller)

        with mock.patch.object(socketio, "start_background_task"):
            response = self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-control-1",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )

        self.assertTrue(
            any(message["action"] == "system.ack" for message in response)
        )
        player_messages = self.messages(player)
        command = next(
            message
            for message in player_messages
            if message["action"] == "player.pause"
        )
        self.assertEqual(command["payload"]["executionTimeoutMs"], 15000)
        transaction = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(transaction["status"], "pending")
        self.assertEqual(transaction["requestingClientId"], "controller-control")
        self.assertEqual(
            transaction["watchdogDeadlineAtMs"] - transaction["acceptedAtMs"],
            17000,
        )
        self.messages(controller)

        emo_ws._sweep_expired_control_transactions(
            transaction["watchdogDeadlineAtMs"]
        )

        terminal = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["errorCode"], "execution_unknown")
        controller_events = self.messages(controller)
        settled = next(
            message
            for message in controller_events
            if message["action"] == "playback.control.settled"
        )
        self.assertEqual(
            settled["payload"]["requestingClientId"],
            "controller-control",
        )
        self.assertNotIn("sourceClientId", settled["payload"])
        self.assertNotIn("clientSeq", settled["payload"])
        authority_events = self.messages(player)
        self.assertTrue(
            any(
                message["action"] == "playback.control.settled"
                and message["payload"]["commandControlVersion"] == 2
                for message in authority_events
            )
        )
        persisted = getPlaybackContextState("context-1")
        self.assertEqual(persisted["state"], "paused")
        self.assertEqual(persisted["controlVersion"], 2)

    def test_authority_disconnect_settles_pending_unknown_without_playback_update(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-disconnect",
            device_session_id="device:controller-disconnect",
        )
        self.emit_strict(
            controller,
            "state",
            "playback.context.subscribe",
            "subscribe-disconnect",
            {"playbackContextId": "context-1"},
        )
        with mock.patch.object(socketio, "start_background_task"):
            self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-disconnect",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
            self.emit_strict(
                controller,
                "command",
                "player.seek",
                "seek-disconnect",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 2,
                    "positionMs": 5000,
                },
            )
            self.emit_strict(
                controller,
                "command",
                "player.play",
                "play-disconnect",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 3,
                },
            )
        self.messages(player)
        self.messages(controller)

        statuses_before_emit = []
        original_broadcast = emo_ws._broadcast_control_settled

        def record_persisted_terminals(transaction, playback_context):
            statuses_before_emit.append(
                [
                    emo_ws.getPlaybackControlTransaction(
                        "context-1",
                        1,
                        version,
                    )["status"]
                    for version in (2, 3, 4)
                ]
            )
            return original_broadcast(transaction, playback_context)

        with mock.patch.object(
            emo_ws,
            "_broadcast_control_settled",
            side_effect=record_persisted_terminals,
        ):
            player.disconnect(namespace="/emo")

        for version in (2, 3, 4):
            terminal = emo_ws.getPlaybackControlTransaction(
                "context-1",
                1,
                version,
            )
            self.assertEqual(terminal["status"], "failed")
            self.assertEqual(terminal["errorCode"], "execution_unknown")
        self.assertEqual(statuses_before_emit[0], ["failed", "failed", "failed"])
        controller_events = self.messages(controller)
        settled = [
            message
            for message in controller_events
            if message["action"] == "playback.control.settled"
        ]
        self.assertEqual(
            [message["payload"]["commandControlVersion"] for message in settled],
            [2, 3, 4],
        )
        self.assertFalse(
            any(message["action"] == "playback.update" for message in controller_events)
        )

    def test_socket_replacement_settles_old_pending_and_never_replays_command(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-replacement",
            device_session_id="device:controller-replacement",
        )
        self.emit_strict(
            controller,
            "state",
            "playback.context.subscribe",
            "subscribe-replacement",
            {"playbackContextId": "context-1"},
        )
        with mock.patch.object(socketio, "start_background_task"):
            self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-before-replacement",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
        self.messages(player)
        self.messages(controller)

        replacement = self.ready_strict_client(
            client_id="phone-1",
            device_session_id="device:phone-1",
        )

        self.assertFalse(player.is_connected(namespace="/emo"))
        terminal = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["errorCode"], "execution_unknown")
        settled = [
            message
            for message in self.messages(controller)
            if message["action"] == "playback.control.settled"
        ]
        self.assertEqual(
            [message["payload"]["commandControlVersion"] for message in settled],
            [2],
        )

        ensured = self.emit_strict(
            replacement,
            "command",
            "playback.context.ensure",
            "ensure-after-replacement",
            {
                "deviceSessionId": "device:phone-1",
                "queueSongIds": ["song-2", "song-1"],
                "currentIndex": 0,
                "positionMs": 1200,
                "state": "paused",
            },
        )
        self.assertFalse(
            any(message["action"] == "player.pause" for message in ensured)
        )

    def test_restart_marks_pending_unknown_without_replaying_to_reconnected_player(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-restart",
            device_session_id="device:controller-restart",
        )
        with mock.patch.object(socketio, "start_background_task"):
            self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-before-restart",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
        self.messages(player)
        self.messages(controller)
        with mock.patch.object(
            emo_ws,
            "_settle_authority_connection_controls_unknown",
            return_value=[],
        ):
            player.disconnect(namespace="/emo")
        self.assertEqual(
            emo_ws.getPlaybackControlTransaction("context-1", 1, 2)["status"],
            "pending",
        )

        emo_ws.init_socketio(self.app)

        terminal = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["errorCode"], "execution_unknown")
        replacement = self.ready_strict_client(
            client_id="phone-1",
            device_session_id="device:phone-1",
        )
        ensured = self.emit_strict(
            replacement,
            "command",
            "playback.context.ensure",
            "ensure-after-restart",
            {
                "deviceSessionId": "device:phone-1",
                "queueSongIds": ["song-2", "song-1"],
                "currentIndex": 0,
                "positionMs": 1200,
                "state": "paused",
            },
        )
        self.assertFalse(
            any(message["action"] == "player.pause" for message in ensured)
        )

    def test_r11_passive_and_remote_committed_updates_advance_applied_cursor(self):
        player = self.ready_strict_client()
        self.create_context(player)
        self.messages(player)

        passive = self.emit_strict(
            player,
            "event",
            "playback.update",
            "passive-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-2",
                "positionMs": 1200,
                "clientSeq": 1,
            },
        )
        self.assertEqual([message["action"] for message in passive], ["playback.update"])
        self.assertEqual(passive[0]["payload"]["origin"], "passive")

        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-applied",
            device_session_id="device:controller-applied",
        )
        self.emit_strict(
            controller,
            "state",
            "playback.context.subscribe",
            "subscribe-applied",
            {"playbackContextId": "context-1"},
        )
        with mock.patch.object(socketio, "start_background_task"):
            self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-applied",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
        self.messages(player)
        self.messages(controller)

        committed = self.emit_strict(
            player,
            "event",
            "playback.update",
            "committed-2",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "remoteCommand",
                "executionStatus": "committed",
                "commandControlVersion": 2,
                "appliedControlVersion": 2,
                "state": "paused",
                "trackId": "song-2",
                "positionMs": 1200,
                "clientSeq": 2,
            },
        )
        update = next(
            message for message in committed if message["action"] == "playback.update"
        )
        self.assertEqual(update["payload"]["executionStatus"], "committed")
        self.assertEqual(update["payload"]["controlVersion"], 2)
        self.assertEqual(update["payload"]["appliedControlVersion"], 2)
        self.assertEqual(
            emo_ws.getPlaybackControlTransaction("context-1", 1, 2)["status"],
            "committed",
        )

        status = self.emit_strict(
            controller,
            "state",
            "playback.context.status",
            "status-applied",
            {"playbackContextId": "context-1"},
        )
        status_message = next(
            message
            for message in status
            if message["action"] == "playback.context.status"
            and "requestId" in message
        )
        device_state = status_message["payload"]["deviceStates"][0]
        self.assertEqual(device_state["appliedControlVersion"], 2)

    def test_windows_execution_timeout_is_feedback_not_server_settled(self):
        player = self.ready_strict_client()
        self.create_context(player)
        self.emit_strict(
            player,
            "event",
            "playback.update",
            "passive-timeout-base",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-2",
                "positionMs": 1200,
                "clientSeq": 1,
            },
        )
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-timeout",
            device_session_id="device:controller-timeout",
        )
        self.emit_strict(
            controller,
            "state",
            "playback.context.subscribe",
            "subscribe-timeout",
            {"playbackContextId": "context-1"},
        )
        with mock.patch.object(socketio, "start_background_task"):
            self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-timeout",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
        self.messages(player)
        self.messages(controller)

        failed = self.emit_strict(
            player,
            "event",
            "playback.update",
            "execution-timeout-feedback",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "remoteCommand",
                "executionStatus": "failed",
                "commandControlVersion": 2,
                "appliedControlVersion": 1,
                "errorCode": "execution_timeout",
                "state": "playing",
                "trackId": "song-2",
                "positionMs": 1200,
                "clientSeq": 2,
            },
        )

        update = next(
            message for message in failed if message["action"] == "playback.update"
        )
        self.assertEqual(update["payload"]["errorCode"], "execution_timeout")
        self.assertFalse(
            any(
                message["action"] == "playback.control.settled"
                for message in failed
            )
        )
        terminal = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["errorCode"], "execution_timeout")
        self.assertEqual(terminal["appliedControlVersion"], 1)
        controller_events = self.messages(controller)
        self.assertTrue(
            any(
                message["action"] == "playback.update"
                and message["payload"].get("errorCode") == "execution_timeout"
                for message in controller_events
            )
        )
        self.assertFalse(
            any(
                message["action"] == "playback.control.settled"
                for message in controller_events
            )
        )

    def test_r11_failed_track_change_emits_one_settled_per_dependent_version(self):
        player = self.ready_strict_client()
        self.create_context(player)
        self.emit_strict(
            player,
            "event",
            "playback.update",
            "passive-dependency",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-2",
                "positionMs": 1200,
                "clientSeq": 1,
            },
        )
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-dependency",
            device_session_id="device:controller-dependency",
        )
        self.emit_strict(
            controller,
            "state",
            "playback.context.subscribe",
            "subscribe-dependency",
            {"playbackContextId": "context-1"},
        )
        with mock.patch.object(socketio, "start_background_task"):
            self.emit_strict(
                controller,
                "command",
                "player.next",
                "next-dependency",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
            self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-dependency",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 2,
                },
            )
            self.emit_strict(
                controller,
                "command",
                "player.seek",
                "seek-dependency",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 3,
                    "positionMs": 5000,
                },
            )
        self.messages(player)
        self.messages(controller)

        failed = self.emit_strict(
            player,
            "event",
            "playback.update",
            "failed-dependency",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "remoteCommand",
                "executionStatus": "failed",
                "commandControlVersion": 2,
                "appliedControlVersion": 1,
                "errorCode": "track_load_failed",
                "state": "playing",
                "trackId": "song-2",
                "positionMs": 1200,
                "clientSeq": 2,
            },
        )
        self.assertTrue(
            any(
                message["action"] == "playback.update"
                and message["payload"]["executionStatus"] == "failed"
                for message in failed
            )
        )
        settled = [
            message
            for message in self.messages(controller)
            if message["action"] == "playback.control.settled"
        ]
        self.assertEqual(
            [message["payload"]["commandControlVersion"] for message in settled],
            [3, 4],
        )
        for message in settled:
            self.assertEqual(message["payload"]["errorCode"], "dependency_failed")
            self.assertEqual(message["payload"]["dependsOnControlVersion"], 2)
            self.assertEqual(
                message["payload"]["requestingClientId"],
                "controller-dependency",
            )
        self.assertEqual(
            emo_ws.getPlaybackControlTransaction("context-1", 1, 3)["status"],
            "failed",
        )
        self.assertEqual(
            emo_ws.getPlaybackControlTransaction("context-1", 1, 4)["status"],
            "failed",
        )
        context = getPlaybackContextState("context-1")
        self.assertEqual(context["controlVersion"], 4)
        self.assertEqual(context["trackId"], "song-2")

    def test_stale_applied_update_returns_passive_correction_only_to_source(self):
        player = self.ready_strict_client()
        self.create_context(player)
        self.emit_strict(
            player,
            "event",
            "playback.update",
            "passive-stale-base",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-2",
                "positionMs": 100,
                "clientSeq": 1,
            },
        )
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-stale",
            device_session_id="device:controller-stale",
        )
        self.emit_strict(
            controller,
            "state",
            "playback.context.subscribe",
            "subscribe-stale",
            {"playbackContextId": "context-1"},
        )
        with mock.patch.object(socketio, "start_background_task"):
            self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-stale",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
        self.messages(player)
        self.messages(controller)
        self.emit_strict(
            player,
            "event",
            "playback.update",
            "committed-stale-base",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "remoteCommand",
                "executionStatus": "committed",
                "commandControlVersion": 2,
                "appliedControlVersion": 2,
                "state": "paused",
                "trackId": "song-2",
                "positionMs": 100,
                "clientSeq": 2,
            },
        )
        self.messages(controller)

        correction = self.emit_strict(
            player,
            "event",
            "playback.update",
            "stale-applied-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-2",
                "positionMs": 0,
                "clientSeq": 3,
            },
        )
        self.assertEqual([message["action"] for message in correction], ["playback.update"])
        self.assertEqual(correction[0]["payload"]["origin"], "passive")
        self.assertEqual(correction[0]["payload"]["appliedControlVersion"], 2)
        self.assertEqual(correction[0]["payload"]["state"], "paused")
        self.assertEqual(self.messages(controller), [])

    def test_local_user_update_allocates_server_version_and_supersedes_pending(self):
        player = self.ready_strict_client()
        self.create_context(player)
        self.emit_strict(
            player,
            "event",
            "playback.update",
            "passive-local-base",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-2",
                "positionMs": 0,
                "clientSeq": 1,
            },
        )
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-local",
            device_session_id="device:controller-local",
        )
        self.emit_strict(
            controller,
            "state",
            "playback.context.subscribe",
            "subscribe-local",
            {"playbackContextId": "context-1"},
        )
        with mock.patch.object(socketio, "start_background_task"):
            self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-before-local",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
        self.messages(player)
        self.messages(controller)

        local_payload = {
            "playbackContextId": "context-1",
            "deviceSessionId": "device:phone-1",
            "origin": "localUser",
            "executionStatus": "committed",
            "intentId": "local-intent-1",
            "epoch": 1,
            "observedControlVersion": 2,
            "queueIndex": 1,
            "state": "playing",
            "trackId": "song-1",
            "positionMs": 0,
            "clientSeq": 2,
        }
        local = self.emit_strict(
            player,
            "event",
            "playback.update",
            "local-user-1",
            local_payload,
        )
        update = next(
            message for message in local if message["action"] == "playback.update"
        )
        self.assertEqual(update["payload"]["origin"], "localUser")
        self.assertEqual(update["payload"]["controlVersion"], 3)
        self.assertEqual(update["payload"]["appliedControlVersion"], 3)
        self.assertEqual(
            update["payload"]["supersededThroughControlVersion"],
            2,
        )
        self.assertEqual(
            emo_ws.getPlaybackControlTransaction("context-1", 1, 2)["status"],
            "superseded",
        )
        self.messages(controller)

        retry_payload = dict(local_payload)
        retry_payload["clientSeq"] = 3
        replay = self.emit_strict(
            player,
            "event",
            "playback.update",
            "local-user-1-retry",
            retry_payload,
        )
        replay_update = next(
            message for message in replay if message["action"] == "playback.update"
        )
        self.assertEqual(replay_update["payload"], update["payload"])
        self.assertEqual(self.messages(controller), [])

    def test_context_list_discovers_exact_persisted_pair_and_replays_by_request_id(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-1",
            device_session_id="device:controller-1",
        )
        payload = {
            "authorityClientId": "phone-1",
            "authorityDeviceSessionId": "device:phone-1",
        }

        with mock.patch.object(
            emo_ws,
            "listActivePlaybackContextBindings",
            wraps=emo_ws.listActivePlaybackContextBindings,
        ) as list_bindings:
            first = self.emit_strict(
                controller,
                "state",
                "playback.context.list",
                "context-list-1",
                payload,
            )
            self.emit_strict(
                player,
                "command",
                "playback.context.create",
                "context-create-2",
                {
                    "playbackContextId": "context-2",
                    "deviceSessionId": "device:phone-1",
                    "queueSongIds": ["song-3"],
                    "currentIndex": 0,
                    "positionMs": 0,
                    "state": "paused",
                },
            )
            invalidations = self.messages(controller)
            self.assertTrue(
                any(
                    message.get("action")
                    == "playback.context.bindings.changed"
                    for message in invalidations
                )
            )
            replay = self.emit_strict(
                controller,
                "state",
                "playback.context.list",
                "context-list-1",
                payload,
            )
            refreshed = self.emit_strict(
                controller,
                "state",
                "playback.context.list",
                "context-list-2",
                payload,
            )

        self.assertEqual(
            first[0]["payload"]["contexts"],
            [
                {
                    "playbackContextId": "context-1",
                    "authorityClientId": "phone-1",
                    "authorityDeviceSessionId": "device:phone-1",
                }
            ],
        )
        self.assertEqual(replay, first)
        self.assertEqual(
            [
                binding["playbackContextId"]
                for binding in refreshed[0]["payload"]["contexts"]
            ],
            ["context-1", "context-2"],
        )
        self.assertEqual(list_bindings.call_count, 2)
        self.assertEqual(
            get_state().list_playback_context_subscribers("context-1"),
            [get_state().get_sid_for_client("phone-1", user_name="alice")],
        )

    def test_context_list_is_controller_only_and_hides_other_scopes(self):
        player = self.ready_strict_client()
        self.create_context(player)

        forbidden = self.emit_strict(
            player,
            "state",
            "playback.context.list",
            "context-list-player-only",
            {
                "authorityClientId": "phone-1",
                "authorityDeviceSessionId": "device:phone-1",
            },
        )[0]
        self.assertEqual(forbidden["payload"]["code"], "forbidden")

        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-1",
            device_session_id="device:controller-1",
        )
        wrong_device = self.emit_strict(
            controller,
            "state",
            "playback.context.list",
            "context-list-wrong-device",
            {
                "authorityClientId": "phone-1",
                "authorityDeviceSessionId": "device:phone-replaced",
            },
        )
        self.assertEqual(wrong_device[0]["payload"], {"contexts": []})

        other = self.connect()
        self.authenticate(other, "bob", "B0b", "auth-bob-list")
        with self.enable_all_profiles():
            self.register(
                other,
                "register-bob-list",
                self.strict_registration_payload(
                    roles=["controller"],
                    client_id="bob-controller",
                    device_session_id="device:bob-controller",
                ),
            )
        self.messages(other)
        cross_user = self.emit_strict(
            other,
            "state",
            "playback.context.list",
            "context-list-cross-user",
            {
                "authorityClientId": "phone-1",
                "authorityDeviceSessionId": "device:phone-1",
            },
        )
        self.assertEqual(cross_user[0]["payload"], {"contexts": []})

    def test_context_list_returns_persisted_binding_when_authority_is_offline(self):
        player = self.ready_strict_client()
        self.create_context(player)
        player.disconnect(namespace="/emo")

        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-offline-discovery",
            device_session_id="device:controller-offline-discovery",
        )
        response = self.emit_strict(
            controller,
            "state",
            "playback.context.list",
            "context-list-offline-authority",
            {
                "authorityClientId": "phone-1",
                "authorityDeviceSessionId": "device:phone-1",
            },
        )

        self.assertEqual(
            response[0]["payload"]["contexts"],
            [
                {
                    "playbackContextId": "context-1",
                    "authorityClientId": "phone-1",
                    "authorityDeviceSessionId": "device:phone-1",
                }
            ],
        )
        self.assertEqual(
            getPlaybackContextState("context-1")["lifecycle"],
            "active",
        )

    def test_context_list_requires_negotiated_playback_context_capability(self):
        client = self.connect()
        self.authenticate(client)
        self.register(
            client,
            "register-legacy-controller",
            {
                "clientId": "legacy-controller",
                "sessionId": "legacy:controller",
                "deviceName": "Legacy Controller",
                "roles": ["controller"],
                "capabilities": {},
            },
        )

        response = self.emit_strict(
            client,
            "state",
            "playback.context.list",
            "context-list-legacy",
            {
                "authorityClientId": "phone-1",
                "authorityDeviceSessionId": "device:phone-1",
            },
        )

        self.assertEqual(response[0]["payload"]["code"], "capability_required")
        self.assertNotIn("connectionNonce", response[0])

    def test_binding_events_fan_out_to_same_user_strict_controllers_only(self):
        player = self.ready_strict_client()
        first_controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-1",
            device_session_id="device:controller-1",
        )
        second_controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-2",
            device_session_id="device:controller-2",
        )
        legacy = self.connect()
        self.authenticate(legacy, request_id="auth-legacy-bindings")
        self.register(
            legacy,
            "register-legacy-bindings",
            {
                "clientId": "legacy-controller",
                "sessionId": "legacy:controller",
                "deviceName": "Legacy Controller",
                "roles": ["controller"],
                "capabilities": {},
            },
        )
        other_user = self.connect()
        self.authenticate(
            other_user,
            "bob",
            "B0b",
            "auth-bob-bindings",
        )
        with self.enable_all_profiles():
            self.register(
                other_user,
                "register-bob-bindings",
                self.strict_registration_payload(
                    roles=["controller"],
                    client_id="bob-controller",
                    device_session_id="device:bob-controller",
                ),
            )
        for client in self.clients:
            self.messages(client)

        self.create_context(player)
        first_events = [
            message
            for message in self.messages(first_controller)
            if message.get("action")
            == "playback.context.bindings.changed"
        ]
        second_events = [
            message
            for message in self.messages(second_controller)
            if message.get("action")
            == "playback.context.bindings.changed"
        ]

        self.assertEqual(len(first_events), 1)
        self.assertEqual(len(second_events), 1)
        for event in first_events + second_events:
            self.assertEqual(
                event["payload"],
                {
                    "authorityClientId": "phone-1",
                    "authorityDeviceSessionId": "device:phone-1",
                },
            )
            self.assertNotIn("requestId", event)
            self.assertNotIn("playbackContextId", event["payload"])
        self.assertNotEqual(
            first_events[0]["connectionNonce"],
            second_events[0]["connectionNonce"],
        )
        self.assertFalse(
            any(
                message.get("action")
                == "playback.context.bindings.changed"
                for message in self.messages(player)
            )
        )
        self.assertFalse(
            any(
                message.get("action")
                == "playback.context.bindings.changed"
                for message in self.messages(legacy)
            )
        )
        self.assertFalse(
            any(
                message.get("action")
                == "playback.context.bindings.changed"
                for message in self.messages(other_user)
            )
        )
        self.assertEqual(
            get_state().list_playback_context_subscribers("context-1"),
            [get_state().get_sid_for_client("phone-1", user_name="alice")],
        )

        self.create_context(
            player,
            request_id="context-create-idempotent-replay",
        )
        self.assertFalse(
            any(
                message.get("action")
                == "playback.context.bindings.changed"
                for message in self.messages(first_controller)
                + self.messages(second_controller)
            )
        )

        close_messages = self.emit_strict(
            first_controller,
            "command",
            "playback.context.close",
            "context-close-bindings",
            {"playbackContextId": "context-1"},
        )
        self.assertEqual(
            len(
                [
                    message
                    for message in close_messages
                    if message.get("action")
                    == "playback.context.bindings.changed"
                ]
            ),
            1,
        )
        self.assertTrue(
            any(
                message.get("action")
                == "playback.context.bindings.changed"
                for message in self.messages(second_controller)
            )
        )

        repeat_close = self.emit_strict(
            first_controller,
            "command",
            "playback.context.close",
            "context-close-bindings-repeat",
            {"playbackContextId": "context-1"},
        )
        self.assertFalse(
            any(
                message.get("action")
                == "playback.context.bindings.changed"
                for message in repeat_close
            )
        )

    def test_binding_event_backpressure_disconnects_only_stale_controller(self):
        player = self.ready_strict_client()
        stale_controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-stale",
            device_session_id="device:controller-stale",
        )
        healthy_controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-healthy",
            device_session_id="device:controller-healthy",
        )
        for client in self.clients:
            self.messages(client)
        stale_sid = get_state().get_sid_for_client(
            "controller-stale",
            user_name="alice",
        )
        real_reserve = strict_v2_safety.reserve_emit

        def reserve_except_stale(target_sid):
            if target_sid == stale_sid:
                return False
            return real_reserve(target_sid)

        with mock.patch.object(
            strict_v2_safety,
            "reserve_emit",
            side_effect=reserve_except_stale,
        ):
            self.create_context(player)

        self.assertEqual(
            getPlaybackContextState("context-1")["lifecycle"],
            "active",
        )
        self.assertFalse(stale_controller.is_connected(namespace="/emo"))
        self.assertTrue(healthy_controller.is_connected(namespace="/emo"))
        self.assertTrue(
            any(
                message.get("action")
                == "playback.context.bindings.changed"
                for message in self.messages(healthy_controller)
            )
        )

    def test_binding_event_emit_failure_disconnects_only_stale_controller(self):
        player = self.ready_strict_client()
        stale_controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-emit-failure",
            device_session_id="device:controller-emit-failure",
        )
        healthy_controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-emit-healthy",
            device_session_id="device:controller-emit-healthy",
        )
        for client in self.clients:
            self.messages(client)
        stale_sid = get_state().get_sid_for_client(
            "controller-emit-failure",
            user_name="alice",
        )
        real_emit = socketio.emit

        def emit_except_stale(event, message, *args, **kwargs):
            if (
                kwargs.get("to") == stale_sid
                and message.get("action")
                == "playback.context.bindings.changed"
            ):
                raise RuntimeError("injected binding invalidation emit failure")
            return real_emit(event, message, *args, **kwargs)

        with mock.patch.object(
            socketio,
            "emit",
            side_effect=emit_except_stale,
        ):
            self.create_context(player)

        self.assertEqual(
            getPlaybackContextState("context-1")["lifecycle"],
            "active",
        )
        self.assertFalse(stale_controller.is_connected(namespace="/emo"))
        self.assertTrue(healthy_controller.is_connected(namespace="/emo"))
        self.assertTrue(
            any(
                message.get("action")
                == "playback.context.bindings.changed"
                for message in self.messages(healthy_controller)
            )
        )

        reconnected_controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-emit-failure",
            device_session_id="device:controller-emit-failure",
        )
        self.messages(reconnected_controller)
        recovered = self.emit_strict(
            reconnected_controller,
            "state",
            "playback.context.list",
            "context-list-after-invalidation-reconnect",
            {
                "authorityClientId": "phone-1",
                "authorityDeviceSessionId": "device:phone-1",
            },
        )
        response = next(
            message
            for message in recovered
            if message.get("action") == "playback.context.list"
        )
        self.assertEqual(
            response["payload"]["contexts"],
            [
                {
                    "playbackContextId": "context-1",
                    "authorityClientId": "phone-1",
                    "authorityDeviceSessionId": "device:phone-1",
                }
            ],
        )

    def test_acceptance_fault_marker_disconnects_only_target_controller(self):
        player = self.ready_strict_client()
        target_controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-acceptance-fault",
            device_session_id="device:controller-acceptance-fault",
        )
        healthy_controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-acceptance-healthy",
            device_session_id="device:controller-acceptance-healthy",
        )
        for client in self.clients:
            self.messages(client)

        with tempfile.TemporaryDirectory() as fault_directory, mock.patch.dict(
            os.environ,
            {FAULT_DIRECTORY_ENV: fault_directory},
        ):
            marker = arm_binding_emit_failure(
                "alice",
                "controller-acceptance-fault",
                "device:controller-acceptance-fault",
            )
            self.create_context(player)

            self.assertFalse(marker.exists())

        self.assertEqual(
            getPlaybackContextState("context-1")["lifecycle"],
            "active",
        )
        self.assertFalse(target_controller.is_connected(namespace="/emo"))
        self.assertTrue(healthy_controller.is_connected(namespace="/emo"))
        self.assertTrue(
            any(
                message.get("action")
                == "playback.context.bindings.changed"
                for message in self.messages(healthy_controller)
            )
        )

    def test_acceptance_fault_marker_is_ignored_outside_local_test_mode(self):
        player = self.ready_strict_client()
        target_controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-production-safe",
            device_session_id="device:controller-production-safe",
        )
        for client in self.clients:
            self.messages(client)

        self.app.testing = False
        self.app.config["WEBAPP"].update(
            {
                "emo_development_mode": False,
                "emo_strict_v2_allow_local_test_evidence": True,
            }
        )
        try:
            with tempfile.TemporaryDirectory() as fault_directory, mock.patch.dict(
                os.environ,
                {FAULT_DIRECTORY_ENV: fault_directory},
            ):
                marker = arm_binding_emit_failure(
                    "alice",
                    "controller-production-safe",
                    "device:controller-production-safe",
                )
                self.create_context(player)

                self.assertTrue(marker.exists())
        finally:
            self.app.testing = True

        self.assertTrue(target_controller.is_connected(namespace="/emo"))
        self.assertTrue(
            any(
                message.get("action")
                == "playback.context.bindings.changed"
                for message in self.messages(target_controller)
            )
        )

    def test_multi_context_controls_conflict_without_authority_side_effects(self):
        player = self.ready_strict_client()
        self.create_context(
            player,
            queue_song_ids=["song-1", "song-2"],
        )
        self.emit_strict(
            player,
            "command",
            "playback.context.create",
            "context-create-ambiguous-2",
            {
                "playbackContextId": "context-2",
                "deviceSessionId": "device:phone-1",
                "queueSongIds": ["song-1", "song-2"],
                "currentIndex": 0,
                "positionMs": 1200,
                "state": "playing",
            },
        )
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-1",
            device_session_id="device:controller-1",
        )
        self.messages(player)
        self.messages(controller)
        controls = {
            "player.play": {"baseControlVersion": 1},
            "player.pause": {"baseControlVersion": 1},
            "player.seek": {
                "baseControlVersion": 1,
                "positionMs": 500,
            },
            "player.next": {"baseControlVersion": 1},
            "player.prev": {"baseControlVersion": 1},
            "queue.playItem": {
                "queueIndex": 1,
                "baseQueueRevision": 1,
                "baseControlVersion": 1,
            },
        }

        for action, action_payload in controls.items():
            with self.subTest(action=action):
                response = self.emit_strict(
                    controller,
                    "command",
                    action,
                    "ambiguous-%s" % action,
                    dict(
                        action_payload,
                        playbackContextId="context-1",
                    ),
                )
                error = response[0]
                self.assertEqual(error["payload"]["code"], "conflict")
                self.assertEqual(
                    {
                        field_name: error["payload"][field_name]
                        for field_name in (
                            "playbackContextId",
                            "currentControlVersion",
                            "currentQueueRevision",
                            "currentVersion",
                        )
                    },
                    {
                        "playbackContextId": "context-1",
                        "currentControlVersion": 1,
                        "currentQueueRevision": 1,
                        "currentVersion": 1,
                    },
                )
                self.assertFalse(
                    any(
                        message.get("action") == action
                        for message in self.messages(player)
                    )
                )
                canonical = getPlaybackContextState("context-1")
                self.assertEqual(canonical["state"], "playing")
                self.assertEqual(canonical["controlVersion"], 1)
                self.assertEqual(canonical["queueRevision"], 1)
                self.assertEqual(canonical["version"], 1)

        self.emit_strict(
            controller,
            "command",
            "playback.context.close",
            "close-ambiguous-context-2",
            {"playbackContextId": "context-2"},
        )
        self.messages(player)
        bindings = self.emit_strict(
            controller,
            "state",
            "playback.context.list",
            "list-after-ambiguity-resolved",
            {
                "authorityClientId": "phone-1",
                "authorityDeviceSessionId": "device:phone-1",
            },
        )
        self.assertEqual(
            bindings[0]["payload"]["contexts"],
            [
                {
                    "playbackContextId": "context-1",
                    "authorityClientId": "phone-1",
                    "authorityDeviceSessionId": "device:phone-1",
                }
            ],
        )
        status = self.emit_strict(
            controller,
            "state",
            "playback.context.status",
            "status-after-ambiguity-resolved",
            {"playbackContextId": "context-1"},
        )
        self.assertEqual(
            status[0]["payload"]["playbackContext"]["playbackContextId"],
            "context-1",
        )
        recovered = self.emit_strict(
            controller,
            "command",
            "player.pause",
            "pause-after-ambiguity-resolved",
            {
                "playbackContextId": "context-1",
                "baseControlVersion": 1,
            },
        )
        self.assertTrue(
            any(message.get("action") == "system.ack" for message in recovered)
        )
        self.assertTrue(
            any(
                message.get("action") == "player.pause"
                for message in self.messages(player)
            )
        )

    def test_context_create_response_emit_failure_replays_persisted_response(self):
        client = self.ready_strict_client()
        request = {
            "type": "command",
            "action": "playback.context.create",
            "requestId": "context-create-response-failure",
            "payload": {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "queueSongIds": ["song-2", "song-1"],
                "currentIndex": 0,
                "positionMs": 1200,
                "state": "playing",
            },
        }
        real_emit = emo_ws.socketio.emit

        def fail_direct_response(event, message, **kwargs):
            if message["action"] == "playback.context.create":
                raise RuntimeError("injected direct response failure")
            return real_emit(event, message, **kwargs)

        with mock.patch.object(
            emo_ws,
            "createStrictPlaybackContextState",
            wraps=emo_ws.createStrictPlaybackContextState,
        ) as create_context, mock.patch.object(
            emo_ws.socketio,
            "emit",
            side_effect=fail_direct_response,
        ):
            client.emit("message", request, namespace="/emo")

        self.assertEqual(self.messages(client), [])
        persisted = getPlaybackContextState("context-1")
        self.assertEqual(persisted["queueSongIds"], ["song-2", "song-1"])
        self.assertEqual(create_context.call_count, 1)

        client.emit("message", request, namespace="/emo")
        replay = self.messages(client)
        self.assertEqual(
            [message["action"] for message in replay],
            ["playback.context.create"],
        )
        self.assertEqual(replay[0]["requestId"], request["requestId"])
        self.assertEqual(replay[0]["payload"]["version"], 1)
        self.assertEqual(create_context.call_count, 1)

    def test_queue_sync_ack_backpressure_replays_without_second_mutation(self):
        client = self.ready_strict_client()
        self.create_context(client)
        self.messages(client)
        target_sid = get_state().get_sid_for_client(
            "phone-1",
            user_name="alice",
        )
        safety_config = dict(self.app.config["WEBAPP"])
        safety_config["emo_socketio_max_pending_emits_per_connection"] = 1
        strict_v2_safety.configure(safety_config)
        self.assertTrue(strict_v2_safety.reserve_emit(target_sid))
        request = {
            "type": "state",
            "action": "queue.context.sync",
            "requestId": "queue-ack-backpressure",
            "payload": {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "queueSongIds": ["song-2", "song-3"],
                "currentIndex": 0,
                "positionMs": 1200,
                "baseQueueRevision": 1,
            },
        }

        try:
            with mock.patch.object(
                emo_ws,
                "mutateStrictPlaybackContextQueue",
                wraps=emo_ws.mutateStrictPlaybackContextQueue,
            ) as mutate_queue:
                client.emit("message", request, namespace="/emo")
        finally:
            strict_v2_safety.release_emit(target_sid)

        self.assertEqual(self.messages(client), [])
        persisted = getPlaybackContextState("context-1")
        self.assertEqual(persisted["queueSongIds"], ["song-2", "song-3"])
        self.assertEqual(persisted["version"], 2)
        self.assertEqual(mutate_queue.call_count, 1)

        client.emit("message", request, namespace="/emo")
        replay = self.messages(client)
        self.assertEqual(
            [message["action"] for message in replay],
            ["system.ack"],
        )
        self.assertEqual(
            replay[0]["payload"],
            {"action": "queue.context.sync"},
        )
        self.assertEqual(mutate_queue.call_count, 1)

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
                "executionTimeoutMs": 15000,
                "positionMs": 42000,
            },
        )
        persisted = getPlaybackContextState("context-1")
        self.assertEqual(persisted["positionMs"], 42000)
        self.assertEqual(persisted["version"], 2)
        self.assertEqual(persisted["controlVersion"], 2)
        self.assertEqual(persisted["queueRevision"], 1)
        self.assertEqual(persisted["epoch"], 1)

    def test_control_does_not_mutate_when_authority_emit_capacity_is_unavailable(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-1",
            device_session_id="device:controller-1",
        )
        self.messages(player)
        self.messages(controller)
        authority_sid = get_state().get_sid_for_client(
            "phone-1",
            user_name="alice",
        )
        safety_config = dict(self.app.config["WEBAPP"])
        safety_config["emo_socketio_max_pending_emits_per_connection"] = 1
        strict_v2_safety.configure(safety_config)
        self.assertTrue(strict_v2_safety.reserve_emit(authority_sid))

        try:
            response = self.emit_strict(
                controller,
                "command",
                "player.seek",
                "seek-backpressure",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                    "positionMs": 42000,
                },
            )
        finally:
            strict_v2_safety.release_emit(authority_sid)

        self.assertEqual(len(response), 1)
        self.assertEqual(response[0]["payload"]["code"], "authority_offline")
        persisted = getPlaybackContextState("context-1")
        self.assertEqual(persisted["positionMs"], 1200)
        self.assertEqual(persisted["version"], 1)
        self.assertEqual(persisted["controlVersion"], 1)
        self.assertFalse(
            any(
                message["action"] == "player.seek"
                for message in self.messages(player)
            )
        )

    def test_strict_emit_rejects_invalid_output_before_socketio_send(self):
        client = self.ready_strict_client()
        self.messages(client)
        target_sid = get_state().get_sid_for_client(
            "phone-1",
            user_name="alice",
        )
        invalid = emo_ws._build_message(
            "event",
            "playback.context.closed",
            {},
        )

        with self.assertRaises(StrictOutputValidationError):
            emo_ws._emit_message(invalid, target_sid)

        self.assertEqual(self.messages(client), [])

    def test_emit_buffer_limit_rejects_second_concurrent_socketio_send(self):
        client = self.ready_strict_client()
        self.messages(client)
        target_sid = get_state().get_sid_for_client(
            "phone-1",
            user_name="alice",
        )
        safety_config = dict(self.app.config["WEBAPP"])
        safety_config["emo_socketio_max_pending_emits_per_connection"] = 1
        strict_v2_safety.configure(safety_config)
        emit_started = threading.Event()
        release_emit = threading.Event()
        thread_errors = []
        message = emo_ws._build_message(
            "event",
            "playback.context.closed",
            {"playbackContextId": "context-buffer-test"},
        )

        def blocking_emit(*args, **kwargs):
            emit_started.set()
            release_emit.wait(1)

        def first_send():
            try:
                emo_ws._emit_message(message, target_sid)
            except Exception as exc:  # pragma: nocover - asserted below
                thread_errors.append(exc)

        with mock.patch.object(
            emo_ws.socketio,
            "emit",
            side_effect=blocking_emit,
        ) as socket_emit:
            thread = threading.Thread(target=first_send)
            thread.start()
            self.assertTrue(emit_started.wait(1))

            with self.assertRaisesRegex(RuntimeError, "send buffer is full"):
                emo_ws._emit_message(message, target_sid)

            self.assertEqual(socket_emit.call_count, 1)
            release_emit.set()
            thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(thread_errors, [])
        self.assertTrue(strict_v2_safety.reserve_emit(target_sid))
        strict_v2_safety.release_emit(target_sid)

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
                error_payloads = []
                for playback_context_id in ("context-1", "unknown-context"):
                    response = self.emit_strict(
                        other,
                        "state",
                        action,
                        "cross-user-%s-%s"
                        % (action, playback_context_id),
                        {"playbackContextId": playback_context_id},
                    )
                    self.assertEqual(len(response), 1)
                    self.assertEqual(
                        response[0]["payload"]["code"],
                        "forbidden",
                    )
                    error_payloads.append(response[0]["payload"])
                self.assertEqual(error_payloads[0], error_payloads[1])

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
