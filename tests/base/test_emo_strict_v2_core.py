import base64
import os
import shutil
import tempfile
import threading
import unittest
from contextlib import contextmanager
from unittest import mock

from supysonic.db import release_database
from supysonic.emo import ws as emo_ws
from supysonic.emo import ws_store as emo_store
from supysonic.emo.follow_store import getFollowSafetyLeaseForFollower
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
            "remoteVolumeControl": True,
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
        for index in range(3):
            client.emit(
                "message",
                {
                    "type": "system",
                    "action": "system.ping",
                    "requestId": "clock-%s-%d" % (client_id, index),
                    "payload": {},
                },
                namespace="/emo",
            )
            self.messages(client)
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
        payload = dict(payload)
        if action == "playback.update":
            payload.setdefault("positionSampledAtServerMs", 1)
            payload.setdefault("playbackRate", 1.0)
        elif action == "queue.context.sync":
            payload.setdefault("positionSampledAtServerMs", 1)
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

    def replace_registered_generation(
        self,
        client_id,
        device_session_id=None,
        sid_suffix="replacement",
    ):
        state = get_state()
        current_sid = state.get_sid_for_client(client_id, user_name="alice")
        current_client = state.get_client(client_id, user_name="alice")
        self.assertIsNotNone(current_sid)
        self.assertIsNotNone(current_client)
        replacement_sid = "%s-%s-%d" % (
            client_id,
            sid_suffix,
            len(state.list_session_sids()),
        )
        state.register_session(replacement_sid, remote_address="test")
        state.authenticate_session(replacement_sid, "alice")
        replacement_client = dict(current_client)
        if device_session_id is not None:
            replacement_client["deviceSessionId"] = device_session_id
        state.register_client(
            replacement_sid,
            client_id,
            replacement_client,
        )
        return replacement_sid

    def authenticated_unregistered_client(self, client_id):
        client = self.connect()
        self.authenticate(client, request_id="auth-unregistered-%s" % client_id)
        return client

    def register_real_replacement(
        self,
        client,
        request_id,
        client_id,
        device_session_id,
        roles=None,
    ):
        with self.enable_all_profiles():
            return self.register(
                client,
                request_id,
                self.strict_registration_payload(
                    roles=roles or ["player"],
                    client_id=client_id,
                    device_session_id=device_session_id,
                ),
            )

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

    def test_device_list_volume_state_is_trimmed_by_recipient_negotiation(self):
        player = self.ready_strict_client(
            client_id="player-volume-list",
            device_session_id="device:player-volume-list",
            capability_overrides={"remoteVolumeControl": True},
        )
        allowed = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-volume-allowed",
            device_session_id="device:controller-volume-allowed",
            capability_overrides={
                "canSetVolume": False,
                "remoteVolumeControl": True,
            },
        )
        denied = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-volume-denied",
            device_session_id="device:controller-volume-denied",
            capability_overrides={
                "canSetVolume": False,
                "remoteVolumeControl": False,
            },
        )
        for client in (player, allowed, denied):
            self.messages(client)

        self.emit_strict(
            player,
            "event",
            "device.volume.update",
            "volume-list-feedback-1",
            {
                "deviceSessionId": "device:player-volume-list",
                "volume": 37,
                "clientSeq": 1,
            },
        )
        self.messages(allowed)
        self.messages(denied)

        allowed_list = self.emit_strict(
            allowed,
            "state",
            "device.list",
            "device-list-volume-allowed-1",
            {},
        )[0]
        denied_list = self.emit_strict(
            denied,
            "state",
            "device.list",
            "device-list-volume-denied-1",
            {},
        )[0]
        allowed_player = next(
            device
            for device in allowed_list["payload"]["devices"]
            if device["clientId"] == "player-volume-list"
        )
        denied_player = next(
            device
            for device in denied_list["payload"]["devices"]
            if device["clientId"] == "player-volume-list"
        )

        self.assertEqual(allowed_player["volumeState"]["volume"], 37)
        self.assertNotIn("volumeState", denied_player)
        self.assertEqual(len(allowed_player["capabilities"]), 10)
        self.assertEqual(len(denied_player["capabilities"]), 10)

    def test_device_volume_requires_extended_capability_and_exact_live_pair(self):
        base_player = self.ready_strict_client(
            client_id="player-1",
            device_session_id="device:player-1",
            capability_overrides={"remoteVolumeControl": False},
        )
        base_controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-1",
            device_session_id="device:controller-1",
            capability_overrides={"remoteVolumeControl": False},
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

    def test_runtime_readiness_fails_closed_without_conformance_evidence(self):
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
                if message.get("requestId") == "register-local-evidence-rejected"
            )
            self.assertEqual(error["action"], "system.error")
            self.assertEqual(error["payload"]["code"], "not_supported")
            self.assertIsNone(get_state().get_client("phone-1"))
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
        self.create_context(
            client,
            queue_song_ids=["song-1"],
            state="stopped",
            position_ms=0,
        )
        self.messages(client)
        update = {
            "type": "event",
            "action": "playback.update",
            "requestId": "playback-update-1",
            "payload": {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "positionMs": 10,
                "positionSampledAtServerMs": 1,
                "playbackRate": 1.0,
                "clientSeq": 1,
                "trackId": "song-1",
            },
        }
        with mock.patch.object(
            emo_ws,
            "applyStrictPlaybackUpdate",
            wraps=emo_ws.applyStrictPlaybackUpdate,
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
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "positionMs": 25,
                "positionSampledAtServerMs": 1,
                "playbackRate": 1.0,
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
        feedback = emo_ws.getDevicePlaybackState(
            "context-1",
            "phone-1",
        )
        self.assertEqual(feedback["positionMs"], 25)
        self.assertEqual(feedback["clientSeq"], 1)
        self.assertEqual(feedback["positionSampledAtServerMs"], 1)
        self.assertEqual(feedback["playbackRate"], 1.0)
        self.assertGreater(feedback["serverUpdatedAtMs"], 1)

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
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "positionMs": 10,
                "positionSampledAtServerMs": 1,
                "playbackRate": 1.0,
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
        feedback = emo_ws.getDevicePlaybackState("context-1", "phone-1")
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
            emo_ws.getDevicePlaybackState("context-1", "phone-1")[
                "positionMs"
            ],
            10,
        )

    def test_stale_passive_applied_version_returns_retryable_conflict(self):
        client = self.ready_strict_client()
        self.create_context(client)
        updated = emo_store.mutateStrictPlaybackContextQueue(
            "context-1",
            "alice",
            "phone-1",
            "device:phone-1",
            ["song-1", "song-2"],
            0,
            1200,
            1,
            1,
            position_sampled_at_server_ms=1,
        )
        before_context = emo_ws.getPlaybackContextState("context-1")
        before_feedback = emo_ws.getDevicePlaybackState("context-1", "phone-1")

        response = self.emit_strict(
            client,
            "event",
            "playback.update",
            "playback-update-stale-applied",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "positionMs": 1200,
                "clientSeq": 1,
                "trackId": "song-2",
            },
        )

        self.assertEqual(len(response), 1)
        self.assertEqual(response[0]["action"], "system.error")
        self.assertEqual(response[0]["payload"]["action"], "playback.update")
        self.assertEqual(response[0]["payload"]["code"], "conflict")
        self.assertTrue(response[0]["payload"]["retryable"])
        self.assertEqual(
            response[0]["payload"]["currentEpoch"],
            updated["epoch"],
        )
        self.assertEqual(
            response[0]["payload"]["currentControlVersion"],
            updated["controlVersion"],
        )
        self.assertEqual(
            response[0]["payload"]["currentQueueRevision"],
            updated["queueRevision"],
        )
        self.assertEqual(
            response[0]["payload"]["currentVersion"],
            updated["version"],
        )
        self.assertEqual(
            emo_ws.getPlaybackContextState("context-1"),
            before_context,
        )
        self.assertEqual(
            emo_ws.getDevicePlaybackState("context-1", "phone-1"),
            before_feedback,
        )

    def test_playback_update_same_sequence_and_content_is_idempotent(self):
        client = self.ready_strict_client()
        self.create_context(client, state="stopped", position_ms=0)
        payload = {
            "playbackContextId": "context-1",
            "deviceSessionId": "device:phone-1",
            "origin": "passive",
            "appliedControlVersion": 1,
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
        first_feedback = emo_ws.getDevicePlaybackState(
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
        second_feedback = emo_ws.getDevicePlaybackState(
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
                "origin": "passive",
                "appliedControlVersion": 1,
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
                "origin": "passive",
                "appliedControlVersion": 1,
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
        feedback = emo_ws.getDevicePlaybackState("context-1", "phone-1")
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

    def test_startup_recovery_gate_rejects_connections_and_strict_requests(self):
        client = self.ready_strict_client()
        strict_v2_safety.begin_startup_recovery()
        try:
            rejected_connection = self.connect()
            self.assertFalse(rejected_connection.is_connected("/emo"))

            response = self.emit_strict(
                client,
                "system",
                "system.ping",
                "ping-during-startup-recovery",
                {},
            )
            self.assertEqual(len(response), 1)
            self.assertEqual(response[0]["action"], "system.error")
            self.assertEqual(
                response[0]["payload"]["code"],
                "internal_error",
            )
        finally:
            strict_v2_safety.complete_startup_recovery()

    def test_strict_registration_fails_closed_while_recovery_is_incomplete(self):
        client = self.connect()
        self.authenticate(client)
        strict_v2_safety.begin_startup_recovery()
        try:
            with self.enable_all_profiles(), mock.patch.object(
                strict_v2_safety,
                "begin_request",
                return_value=True,
            ):
                response = self.register(
                    client,
                    "register-during-startup-recovery",
                    self.strict_registration_payload(),
                )
            self.assertEqual(len(response), 1)
            self.assertEqual(response[0]["action"], "system.error")
            self.assertEqual(response[0]["payload"]["code"], "not_supported")
            self.assertIn("startup recovery", response[0]["payload"]["message"])
            self.assertIsNone(
                get_state().get_sid_for_client(
                    "phone-1",
                    user_name="alice",
                )
            )
        finally:
            strict_v2_safety.complete_startup_recovery()

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
        self.assertEqual(
            snapshot["authorityDeviceSessionId"],
            "device:phone-1",
        )
        self.assertEqual(snapshot["state"], "playing")
        self.assertEqual(snapshot["positionMs"], 1200)
        for cursor_name in ("epoch", "version", "queueRevision", "controlVersion"):
            self.assertEqual(snapshot[cursor_name], 1)
        self.assertEqual(
            set(snapshot),
            {
                "playbackContextId",
                "authorityClientId",
                "authorityDeviceSessionId",
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
        self.assertEqual(error["currentEpoch"], 1)
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

    def test_core_prepare_does_not_require_handoff_playback_prepare(self):
        player = self.ready_strict_client(
            capability_overrides={"playbackPrepare": False},
        )
        with mock.patch(
            "supysonic.emo.ws_store._new_playback_context_id",
            return_value="context-core-prepare-no-handoff",
        ):
            self.emit_strict(
                player,
                "command",
                "playback.context.ensure",
                "ensure-core-prepare-no-handoff",
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
            client_id="controller-core-prepare",
            device_session_id="device:controller-core-prepare",
        )
        self.messages(controller)

        with mock.patch.object(socketio, "start_background_task"):
            response = self.emit_strict(
                controller,
                "command",
                "playback.context.prepare",
                "core-prepare-no-handoff-1",
                {
                    "playbackContextId": "context-core-prepare-no-handoff",
                    "intentId": "intent-core-prepare-no-handoff",
                    "baseControlVersion": 1,
                },
            )

        self.assertEqual([message["action"] for message in response], ["system.ack"])
        self.assertEqual(response[0]["payload"]["status"], "preparing")
        routed = self.messages(player)
        self.assertEqual(
            [message["action"] for message in routed],
            ["playback.context.prepare"],
        )
        current_sid = get_state().get_sid_for_client("phone-1", user_name="alice")
        current_session = get_state().get_session(current_sid)
        prepare = emo_ws.getPlaybackPrepareTransaction(
            "context-core-prepare-no-handoff",
            1,
            "intent-core-prepare-no-handoff",
        )
        self.assertEqual(prepare["authorityClientId"], "phone-1")
        self.assertEqual(
            prepare["authorityDeviceSessionId"],
            "device:phone-1",
        )
        self.assertEqual(
            prepare["routedConnectionNonce"],
            current_session["connectionNonce"],
        )
        self.assertEqual(prepare["routedConnectionEpoch"], 1)

    def test_core_prepare_independence_preserves_current_role_and_can_play_gates(self):
        player = self.ready_strict_client()
        with mock.patch(
            "supysonic.emo.ws_store._new_playback_context_id",
            return_value="context-core-prepare-gates",
        ):
            self.emit_strict(
                player,
                "command",
                "playback.context.ensure",
                "ensure-core-prepare-gates",
                {
                    "deviceSessionId": "device:phone-1",
                    "queueSongIds": [],
                    "positionMs": 0,
                    "state": "idle",
                },
            )
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-core-prepare-gates",
            device_session_id="device:controller-core-prepare-gates",
        )
        self.messages(player)
        self.messages(controller)

        no_play = self.ready_strict_client(
            client_id="phone-1",
            device_session_id="device:phone-1",
            capability_overrides={
                "playbackPrepare": False,
                "canPlay": False,
            },
        )
        self.messages(no_play)
        denied = self.emit_strict(
            controller,
            "command",
            "playback.context.prepare",
            "core-prepare-no-can-play-1",
            {
                "playbackContextId": "context-core-prepare-gates",
                "intentId": "intent-core-prepare-no-can-play",
                "baseControlVersion": 1,
            },
        )
        self.assertEqual(denied[0]["payload"]["code"], "capability_required")
        self.assertEqual(self.messages(no_play), [])

        non_player = self.ready_strict_client(
            roles=["controller"],
            client_id="phone-1",
            device_session_id="device:phone-1",
            capability_overrides={"playbackPrepare": False},
        )
        self.messages(non_player)
        denied = self.emit_strict(
            controller,
            "command",
            "playback.context.prepare",
            "core-prepare-non-player-1",
            {
                "playbackContextId": "context-core-prepare-gates",
                "intentId": "intent-core-prepare-non-player",
                "baseControlVersion": 1,
            },
        )
        self.assertEqual(denied[0]["payload"]["code"], "authority_offline")
        self.assertEqual(self.messages(non_player), [])
        self.assertEqual(
            emo_ws.listActivePlaybackPrepareTransactions(
                "context-core-prepare-gates"
            ),
            [],
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
            transaction["watchdogDeadlineAtMs"],
            transaction["executionEligibleAtMs"]
            + transaction["executionTimeoutMs"]
            + 2000,
        )
        self.assertLess(
            transaction["acceptedAtMs"],
            transaction["executionEligibleAtMs"],
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
        self.assertEqual(
            settled["payload"]["requestingDeviceSessionId"],
            "device:controller-control",
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

    def test_dependency_commit_activates_enqueued_command_and_starts_watchdog(self):
        player = self.ready_strict_client()
        self.create_context(player)
        self.messages(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-held",
            device_session_id="device:controller-held",
        )

        with mock.patch.object(emo_ws, "_start_control_watchdog") as watchdog:
            self.emit_strict(
                controller,
                "command",
                "player.next",
                "next-held-root",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
            root_commands = [
                message
                for message in self.messages(player)
                if message["action"] == "player.next"
            ]
            self.assertEqual(len(root_commands), 1)
            held_response = self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-held-dependent",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 2,
                },
            )
            self.assertTrue(
                any(message["action"] == "system.ack" for message in held_response)
            )
            dependent_commands = [
                message
                for message in self.messages(player)
                if message["action"] == "player.pause"
            ]
            self.assertEqual(len(dependent_commands), 1)
            held = emo_ws.getPlaybackControlTransaction("context-1", 1, 3)
            self.assertEqual(held["dependsOnControlVersion"], 2)
            self.assertNotIn("executionEligibleAtMs", held)
            self.assertNotIn("watchdogDeadlineAtMs", held)
            self.assertEqual(watchdog.call_count, 1)

            feedback = self.emit_strict(
                player,
                "event",
                "playback.update",
                "commit-held-root",
                {
                    "playbackContextId": "context-1",
                    "deviceSessionId": "device:phone-1",
                    "origin": "remoteCommand",
                    "executionStatus": "committed",
                    "commandControlVersion": 2,
                    "appliedControlVersion": 2,
                    "state": "playing",
                    "trackId": "song-1",
                    "positionMs": 0,
                    "clientSeq": 1,
                },
            )

        self.assertFalse(
            any(message["action"] == "player.pause" for message in feedback)
        )
        self.assertEqual(
            dependent_commands[0]["payload"]["dependsOnControlVersion"],
            2,
        )
        self.assertEqual(dependent_commands[0]["payload"]["controlVersion"], 3)
        eligible = emo_ws.getPlaybackControlTransaction("context-1", 1, 3)
        self.assertEqual(
            eligible["watchdogDeadlineAtMs"],
            eligible["executionEligibleAtMs"]
            + eligible["executionTimeoutMs"]
            + 2000,
        )
        self.assertEqual(watchdog.call_count, 2)
        self.assertEqual(
            watchdog.call_args.args[0]["commandControlVersion"],
            3,
        )

    def test_dependency_chain_activates_only_each_direct_successor(self):
        player = self.ready_strict_client()
        self.create_context(
            player,
            queue_song_ids=["song-1", "song-2", "song-3"],
            position_ms=0,
        )
        self.messages(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-chain",
            device_session_id="device:controller-chain",
        )

        with mock.patch.object(emo_ws, "_start_control_watchdog") as watchdog:
            for action, request_id, base_version in (
                ("player.next", "next-chain-root", 1),
                ("player.next", "next-chain-dependent", 2),
                ("player.pause", "pause-chain-dependent", 3),
            ):
                response = self.emit_strict(
                    controller,
                    "command",
                    action,
                    request_id,
                    {
                        "playbackContextId": "context-1",
                        "baseControlVersion": base_version,
                    },
                )
                self.assertTrue(
                    any(message["action"] == "system.ack" for message in response)
                )
            initial_commands = [
                message
                for message in self.messages(player)
                if message["action"] in {"player.next", "player.pause"}
            ]
            self.assertEqual(
                [message["payload"]["controlVersion"] for message in initial_commands],
                [2, 3, 4],
            )
            self.assertNotIn("dependsOnControlVersion", initial_commands[0]["payload"])
            self.assertEqual(
                initial_commands[1]["payload"]["dependsOnControlVersion"],
                2,
            )
            self.assertEqual(
                initial_commands[2]["payload"]["dependsOnControlVersion"],
                3,
            )
            self.assertEqual(
                emo_ws.getPlaybackControlTransaction("context-1", 1, 3)[
                    "dependsOnControlVersion"
                ],
                2,
            )
            self.assertEqual(
                emo_ws.getPlaybackControlTransaction("context-1", 1, 4)[
                    "dependsOnControlVersion"
                ],
                3,
            )

            first_feedback = self.emit_strict(
                player,
                "event",
                "playback.update",
                "commit-chain-root",
                {
                    "playbackContextId": "context-1",
                    "deviceSessionId": "device:phone-1",
                    "origin": "remoteCommand",
                    "executionStatus": "committed",
                    "commandControlVersion": 2,
                    "appliedControlVersion": 2,
                    "state": "playing",
                    "trackId": "song-2",
                    "positionMs": 0,
                    "clientSeq": 1,
                },
            )
            first_successors = [
                message
                for message in first_feedback
                if message["action"] in {"player.next", "player.pause"}
            ]
            self.assertEqual(first_successors, [])
            self.assertNotIn(
                "executionEligibleAtMs",
                emo_ws.getPlaybackControlTransaction("context-1", 1, 4),
            )

            second_feedback = self.emit_strict(
                player,
                "event",
                "playback.update",
                "commit-chain-dependent",
                {
                    "playbackContextId": "context-1",
                    "deviceSessionId": "device:phone-1",
                    "origin": "remoteCommand",
                    "executionStatus": "committed",
                    "commandControlVersion": 3,
                    "appliedControlVersion": 3,
                    "state": "playing",
                    "trackId": "song-3",
                    "positionMs": 0,
                    "clientSeq": 2,
                },
            )

        second_successors = [
            message
            for message in second_feedback
            if message["action"] in {"player.next", "player.pause"}
        ]
        self.assertEqual(second_successors, [])
        self.assertEqual(watchdog.call_count, 3)

    def test_control_settlement_routes_to_exact_unsubscribed_generations(self):
        player = self.ready_strict_client()
        self.create_context(player)
        state = get_state()
        player_sid = state.get_sid_for_client("phone-1", user_name="alice")
        state.unsubscribe_playback_context(player_sid, "context-1")
        self.messages(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-exact-settled",
            device_session_id="device:controller-exact-settled",
        )
        controller_sid = state.get_sid_for_client(
            "controller-exact-settled",
            user_name="alice",
        )
        state.unsubscribe_playback_context(controller_sid, "context-1")

        with mock.patch.object(emo_ws, "_start_control_watchdog"):
            self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-exact-settled",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
        self.messages(player)
        self.messages(controller)
        transaction = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)

        emo_ws._sweep_expired_control_transactions(
            transaction["watchdogDeadlineAtMs"]
        )

        for client in (controller, player):
            settled = [
                message
                for message in self.messages(client)
                if message["action"] == "playback.control.settled"
            ]
            self.assertEqual(len(settled), 1)
            self.assertEqual(
                settled[0]["payload"]["requestingDeviceSessionId"],
                "device:controller-exact-settled",
            )

    def test_requester_replacement_does_not_receive_historical_settlement(self):
        player = self.ready_strict_client()
        self.create_context(player)
        self.messages(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-settled-replacement",
            device_session_id="device:controller-settled-replacement",
        )
        with mock.patch.object(emo_ws, "_start_control_watchdog"):
            self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-before-requester-replacement",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
        transaction = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.messages(player)
        self.messages(controller)

        replacement = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-settled-replacement",
            device_session_id="device:controller-settled-replacement",
        )
        replacement_generation = get_state().get_current_physical_generation(
            "alice",
            "controller-settled-replacement",
            "device:controller-settled-replacement",
        )
        self.assertNotEqual(
            replacement_generation["connectionNonce"],
            transaction["requestingConnectionNonce"],
        )
        self.messages(replacement)
        self.messages(player)

        emo_ws._sweep_expired_control_transactions(
            transaction["watchdogDeadlineAtMs"]
        )

        self.assertFalse(
            any(
                message["action"] == "playback.control.settled"
                for message in self.messages(replacement)
            )
        )
        self.assertEqual(
            len(
                [
                    message
                    for message in self.messages(player)
                    if message["action"] == "playback.control.settled"
                ]
            ),
            1,
        )

    def test_eligible_dependent_unknown_omits_dependency_from_wire(self):
        player = self.ready_strict_client()
        self.create_context(player)
        self.messages(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-dependent-unknown",
            device_session_id="device:controller-dependent-unknown",
        )
        with mock.patch.object(emo_ws, "_start_control_watchdog"):
            self.emit_strict(
                controller,
                "command",
                "player.next",
                "next-dependent-unknown",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
            self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-dependent-unknown",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 2,
                },
            )
            self.messages(player)
            self.emit_strict(
                player,
                "event",
                "playback.update",
                "commit-before-dependent-unknown",
                {
                    "playbackContextId": "context-1",
                    "deviceSessionId": "device:phone-1",
                    "origin": "remoteCommand",
                    "executionStatus": "committed",
                    "commandControlVersion": 2,
                    "appliedControlVersion": 2,
                    "state": "playing",
                    "trackId": "song-1",
                    "positionMs": 0,
                    "clientSeq": 1,
                },
            )
        self.messages(controller)

        player.disconnect(namespace="/emo")

        transaction = emo_ws.getPlaybackControlTransaction("context-1", 1, 3)
        self.assertEqual(transaction["errorCode"], "execution_unknown")
        self.assertEqual(transaction["dependsOnControlVersion"], 2)
        settled = next(
            message
            for message in self.messages(controller)
            if message["action"] == "playback.control.settled"
            and message["payload"]["commandControlVersion"] == 3
        )
        self.assertEqual(settled["payload"]["errorCode"], "execution_unknown")
        self.assertNotIn("dependsOnControlVersion", settled["payload"])

    def test_dependent_queue_play_item_uses_frozen_queue_snapshot(self):
        player = self.ready_strict_client()
        self.create_context(player)
        self.messages(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-held-play-item",
            device_session_id="device:controller-held-play-item",
        )
        with mock.patch.object(emo_ws, "_start_control_watchdog"):
            self.emit_strict(
                controller,
                "command",
                "player.next",
                "next-before-held-play-item",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
            self.emit_strict(
                controller,
                "command",
                "queue.playItem",
                "held-play-item",
                {
                    "playbackContextId": "context-1",
                    "queueIndex": 0,
                    "baseQueueRevision": 2,
                    "baseControlVersion": 2,
                },
            )
            commands = [
                message
                for message in self.messages(player)
                if message["action"] == "queue.playItem"
            ]
            self.assertEqual(len(commands), 1)
            feedback = self.emit_strict(
                player,
                "event",
                "playback.update",
                "commit-before-held-play-item",
                {
                    "playbackContextId": "context-1",
                    "deviceSessionId": "device:phone-1",
                    "origin": "remoteCommand",
                    "executionStatus": "committed",
                    "commandControlVersion": 2,
                    "appliedControlVersion": 2,
                    "state": "playing",
                    "trackId": "song-1",
                    "positionMs": 0,
                    "clientSeq": 1,
                },
            )

        command = commands[0]
        self.assertFalse(
            any(message["action"] == "queue.playItem" for message in feedback)
        )
        self.assertEqual(command["payload"]["queueSongIds"], ["song-2", "song-1"])
        self.assertEqual(command["payload"]["queueIndex"], 0)
        self.assertEqual(command["payload"]["queueRevision"], 3)
        self.assertEqual(command["payload"]["dependsOnControlVersion"], 2)

    def test_dependency_failure_cascade_emits_each_direct_edge(self):
        player = self.ready_strict_client()
        self.create_context(
            player,
            queue_song_ids=["song-1", "song-2", "song-3"],
            position_ms=0,
        )
        self.messages(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-cascade",
            device_session_id="device:controller-cascade",
        )
        with mock.patch.object(emo_ws, "_start_control_watchdog"):
            for action, request_id, base_version in (
                ("player.next", "next-cascade-root", 1),
                ("player.next", "next-cascade-child", 2),
                ("player.pause", "pause-cascade-grandchild", 3),
            ):
                self.emit_strict(
                    controller,
                    "command",
                    action,
                    request_id,
                    {
                        "playbackContextId": "context-1",
                        "baseControlVersion": base_version,
                    },
                )
        initial_commands = [
            message
            for message in self.messages(player)
            if message["action"] in {"player.next", "player.pause"}
        ]
        self.assertEqual(
            [message["payload"]["controlVersion"] for message in initial_commands],
            [2, 3, 4],
        )
        self.messages(controller)

        self.emit_strict(
            player,
            "event",
            "playback.update",
            "fail-cascade-root",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "remoteCommand",
                "executionStatus": "failed",
                "commandControlVersion": 2,
                "appliedControlVersion": 1,
                "errorCode": "track_load_failed",
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 0,
                "clientSeq": 1,
            },
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
        self.assertEqual(
            [message["payload"]["dependsOnControlVersion"] for message in settled],
            [2, 3],
        )
        self.assertTrue(
            all(
                message["payload"]["requestingDeviceSessionId"]
                == "device:controller-cascade"
                for message in settled
            )
        )

    def test_dependency_eligible_watchdog_failure_settles_unknown_once(self):
        player = self.ready_strict_client()
        self.create_context(player)
        self.messages(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-held-emit-failure",
            device_session_id="device:controller-held-emit-failure",
        )
        watchdog_versions = []

        def schedule_watchdog(transaction):
            watchdog_versions.append(transaction["commandControlVersion"])
            if transaction["commandControlVersion"] == 3:
                raise RuntimeError("dependent watchdog failure")

        with mock.patch.object(
            emo_ws,
            "_start_control_watchdog",
            side_effect=schedule_watchdog,
        ) as watchdog:
            self.emit_strict(
                controller,
                "command",
                "player.next",
                "next-held-emit-failure",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
            self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-held-emit-failure",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 2,
                },
            )
            initial_commands = [
                message
                for message in self.messages(player)
                if message["action"] in {"player.next", "player.pause"}
            ]
            self.assertEqual(
                [message["payload"]["controlVersion"] for message in initial_commands],
                [2, 3],
            )
            self.messages(controller)
            with self.assertLogs("supysonic.emo.ws", level="ERROR"):
                feedback = self.emit_strict(
                    player,
                    "event",
                    "playback.update",
                    "commit-held-emit-failure",
                    {
                        "playbackContextId": "context-1",
                        "deviceSessionId": "device:phone-1",
                        "origin": "remoteCommand",
                        "executionStatus": "committed",
                        "commandControlVersion": 2,
                        "appliedControlVersion": 2,
                        "state": "playing",
                        "trackId": "song-1",
                        "positionMs": 0,
                        "clientSeq": 1,
                    },
                )

        self.assertTrue(
            any(message["action"] == "playback.update" for message in feedback)
        )
        self.assertFalse(
            any(message["action"] == "player.pause" for message in feedback)
        )
        terminal = emo_ws.getPlaybackControlTransaction("context-1", 1, 3)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["errorCode"], "execution_unknown")
        self.assertIn("executionEligibleAtMs", terminal)
        self.assertEqual(watchdog.call_count, 2)
        self.assertEqual(watchdog_versions, [2, 3])
        settlements = [
            message
            for message in self.messages(controller)
            if message["action"] == "playback.control.settled"
            and message["payload"]["commandControlVersion"] == 3
        ]
        self.assertEqual(len(settlements), 1)

    def test_replacement_requester_receives_settlement_only_as_subscriber(self):
        player = self.ready_strict_client()
        self.create_context(player)
        self.messages(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-replacement-subscriber",
            device_session_id="device:controller-replacement-subscriber",
        )
        with mock.patch.object(emo_ws, "_start_control_watchdog"):
            self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-replacement-subscriber",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
        transaction = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.messages(player)
        replacement = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-replacement-subscriber",
            device_session_id="device:controller-replacement-subscriber",
        )
        self.emit_strict(
            replacement,
            "state",
            "playback.context.subscribe",
            "subscribe-replacement-settlement",
            {"playbackContextId": "context-1"},
        )
        self.messages(replacement)
        self.messages(player)

        emo_ws._sweep_expired_control_transactions(
            transaction["watchdogDeadlineAtMs"]
        )

        settled = [
            message
            for message in self.messages(replacement)
            if message["action"] == "playback.control.settled"
        ]
        self.assertEqual(len(settled), 1)

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

    def test_legacy_terminal_without_requester_pair_emits_no_invalid_settlement(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-legacy-terminal",
            device_session_id="device:controller-legacy-terminal",
        )
        self.emit_strict(
            controller,
            "state",
            "playback.context.subscribe",
            "subscribe-legacy-terminal",
            {"playbackContextId": "context-1"},
        )
        self.messages(player)
        self.messages(controller)
        context = getPlaybackContextState("context-1")
        legacy_terminal = {
            "playbackContextId": "context-1",
            "userName": "alice",
            "epoch": context["epoch"],
            "commandControlVersion": context["controlVersion"],
            "status": "failed",
            "errorCode": "execution_unknown",
            "requestingClientId": "controller-legacy-terminal",
            "authorityClientId": "phone-1",
            "authorityDeviceSessionId": "device:phone-1",
            "terminalAtMs": 2000,
        }

        with self.assertLogs("supysonic.emo.ws", level="WARNING") as captured:
            emitted = emo_ws._broadcast_control_settled(
                legacy_terminal,
                context,
            )

        self.assertFalse(emitted)
        self.assertTrue(
            any(
                "Skipping invalid playback.control.settled" in line
                for line in captured.output
            )
        )
        for client in (player, controller):
            self.assertFalse(
                any(
                    message["action"] == "playback.control.settled"
                    for message in self.messages(client)
                )
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
        failed_confirmation = next(
            message
            for message in failed
            if message["action"] == "playback.update"
            and message["payload"]["executionStatus"] == "failed"
        )
        self.assertEqual(failed_confirmation["payload"]["commandControlVersion"], 2)
        self.assertEqual(failed_confirmation["payload"]["controlVersion"], 5)
        self.assertEqual(
            failed_confirmation["payload"]["appliedControlVersion"],
            5,
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
        self.assertEqual(context["controlVersion"], 5)
        self.assertEqual(context["trackId"], "song-2")
        reconciliations = emo_store.listPlaybackControlReconciliations(
            "context-1",
            1,
        )
        self.assertEqual(len(reconciliations), 1)
        self.assertEqual(reconciliations[0]["reconciliationControlVersion"], 5)
        self.assertEqual(reconciliations[0]["throughControlVersion"], 4)
        self.assertEqual(
            {
                emo_ws.getPlaybackControlTransaction(
                    "context-1",
                    1,
                    command_version,
                )["reconciledByControlVersion"]
                for command_version in (2, 3, 4)
            },
            {5},
        )

    def test_passive_actual_reconciles_execution_unknown_gap(self):
        player = self.ready_strict_client()
        self.create_context(player, queue_song_ids=["song-1"])
        self.emit_strict(
            player,
            "event",
            "playback.update",
            "unknown-gap-baseline",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 1200,
                "clientSeq": 1,
            },
        )
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-unknown-gap",
            device_session_id="device:controller-unknown-gap",
        )
        with mock.patch.object(socketio, "start_background_task"):
            self.emit_strict(
                controller,
                "command",
                "player.pause",
                "unknown-gap-pause",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
        self.messages(player)
        transaction = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        emo_store.settlePlaybackControlTransaction(
            "context-1",
            1,
            2,
            "failed",
            transaction["executionEligibleAtMs"] + 1,
            error_code="execution_unknown",
            applied_control_version=1,
        )

        messages = self.emit_strict(
            player,
            "event",
            "playback.update",
            "unknown-gap-passive",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 1350,
                "clientSeq": 2,
            },
        )

        confirmation = next(
            message
            for message in messages
            if message["action"] == "playback.update"
        )
        self.assertEqual(confirmation["payload"]["origin"], "passive")
        self.assertEqual(confirmation["payload"]["controlVersion"], 3)
        self.assertEqual(confirmation["payload"]["appliedControlVersion"], 3)
        self.assertTrue(
            any(
                message["action"] == "playback.context.status"
                for message in messages
            )
        )
        context = getPlaybackContextState("context-1")
        self.assertEqual(context["controlVersion"], 3)
        self.assertEqual(context["state"], "playing")
        self.assertEqual(context["positionMs"], 1350)
        terminal = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(terminal["errorCode"], "execution_unknown")
        self.assertEqual(terminal["reconciledByControlVersion"], 3)
        self.assertEqual(
            emo_ws.getDevicePlaybackState("context-1", "phone-1")[
                "appliedControlVersion"
            ],
            3,
        )

    def test_single_item_prev_and_next_use_no_repeat_boundaries(self):
        player = self.ready_strict_client()
        self.create_context(
            player,
            queue_song_ids=["song-only"],
            position_ms=500,
        )
        self.emit_strict(
            player,
            "event",
            "playback.update",
            "single-boundary-baseline",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-only",
                "positionMs": 500,
                "clientSeq": 1,
            },
        )
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-boundary",
            device_session_id="device:controller-boundary",
        )

        with mock.patch.object(socketio, "start_background_task"):
            prev_messages = self.emit_strict(
                controller,
                "command",
                "player.prev",
                "single-first-prev",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
        self.assertTrue(
            any(message["action"] == "system.ack" for message in prev_messages)
        )
        prev_command = next(
            message
            for message in self.messages(player)
            if message["action"] == "player.prev"
        )
        self.assertEqual(prev_command["payload"]["controlVersion"], 2)
        after_prev = getPlaybackContextState("context-1")
        self.assertEqual(after_prev["currentIndex"], 0)
        self.assertEqual(after_prev["trackId"], "song-only")
        self.assertEqual(after_prev["queueRevision"], 1)
        self.assertEqual(after_prev["state"], "playing")
        self.assertEqual(after_prev["positionMs"], 0)

        self.emit_strict(
            player,
            "event",
            "playback.update",
            "single-prev-committed",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "remoteCommand",
                "executionStatus": "committed",
                "commandControlVersion": 2,
                "appliedControlVersion": 2,
                "state": "playing",
                "trackId": "song-only",
                "positionMs": 0,
                "clientSeq": 2,
            },
        )
        self.messages(controller)

        with mock.patch.object(socketio, "start_background_task"):
            next_messages = self.emit_strict(
                controller,
                "command",
                "player.next",
                "single-last-next",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 2,
                },
            )
        self.assertTrue(
            any(message["action"] == "system.ack" for message in next_messages)
        )
        next_command = next(
            message
            for message in self.messages(player)
            if message["action"] == "player.next"
        )
        self.assertEqual(next_command["payload"]["controlVersion"], 3)
        after_next = getPlaybackContextState("context-1")
        self.assertEqual(after_next["currentIndex"], 0)
        self.assertEqual(after_next["trackId"], "song-only")
        self.assertEqual(after_next["queueRevision"], 1)
        self.assertEqual(after_next["state"], "stopped")
        self.assertEqual(after_next["positionMs"], 0)

    def test_last_track_natural_terminal_pushes_context_once(self):
        player = self.ready_strict_client()
        self.create_context(player, queue_song_ids=["song-only"])
        self.emit_strict(
            player,
            "event",
            "playback.update",
            "natural-terminal-baseline",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-only",
                "positionMs": 1200,
                "clientSeq": 1,
            },
        )
        before = getPlaybackContextState("context-1")
        terminal_payload = {
            "playbackContextId": "context-1",
            "deviceSessionId": "device:phone-1",
            "origin": "passive",
            "appliedControlVersion": 1,
            "state": "stopped",
            "trackId": "song-only",
            "positionMs": 0,
            "clientSeq": 2,
        }

        messages = self.emit_strict(
            player,
            "event",
            "playback.update",
            "natural-terminal",
            terminal_payload,
        )
        after = getPlaybackContextState("context-1")
        self.assertEqual(after["version"], before["version"] + 1)
        self.assertEqual(after["queueRevision"], before["queueRevision"])
        self.assertEqual(after["controlVersion"], before["controlVersion"])
        self.assertEqual(after["state"], "stopped")
        self.assertEqual(
            sum(
                message["action"] == "playback.context.status"
                for message in messages
            ),
            1,
        )

        duplicate = self.emit_strict(
            player,
            "event",
            "playback.update",
            "natural-terminal-duplicate",
            terminal_payload,
        )
        self.assertEqual(getPlaybackContextState("context-1"), after)
        self.assertFalse(
            any(
                message["action"] == "playback.context.status"
                for message in duplicate
            )
        )

    def test_stale_passive_update_returns_conflict_only_to_source(self):
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

        before_context = getPlaybackContextState("context-1")
        before_device = emo_ws.getDevicePlaybackState("context-1", "phone-1")
        rejection = self.emit_strict(
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
        self.assertEqual([message["action"] for message in rejection], ["system.error"])
        self.assertEqual(rejection[0]["payload"]["action"], "playback.update")
        self.assertEqual(rejection[0]["payload"]["code"], "conflict")
        self.assertTrue(rejection[0]["payload"]["retryable"])
        self.assertEqual(getPlaybackContextState("context-1"), before_context)
        self.assertEqual(
            emo_ws.getDevicePlaybackState("context-1", "phone-1"),
            before_device,
        )
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
            self.create_context(
                player,
                request_id="context-ensure-2",
                queue_song_ids=["song-3"],
                state="paused",
                position_ms=0,
            )
            invalidations = self.messages(controller)
            self.assertEqual(invalidations, [])
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
            refreshed[0]["payload"]["contexts"],
            first[0]["payload"]["contexts"],
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
            {
                "playbackContextId": "context-1",
                "expectedEpoch": 1,
                "baseVersion": 1,
            },
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
            {
                "playbackContextId": "context-1",
                "expectedEpoch": 1,
                "baseVersion": 1,
            },
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

    def test_ensure_keeps_one_context_per_authority_pair(self):
        player = self.ready_strict_client()
        self.create_context(
            player,
            queue_song_ids=["song-1", "song-2"],
        )
        with mock.patch(
            "supysonic.emo.ws_store._new_playback_context_id",
            return_value="context-2",
        ):
            ensured = self.emit_strict(
                player,
                "command",
                "playback.context.ensure",
                "context-ensure-same-authority",
                {
                    "deviceSessionId": "device:phone-1",
                    "queueSongIds": ["other-song"],
                    "currentIndex": 0,
                    "positionMs": 0,
                    "state": "paused",
                },
            )
        self.assertEqual(ensured[0]["payload"]["playbackContextId"], "context-1")
        self.assertIsNone(getPlaybackContextState("context-2"))
        canonical = getPlaybackContextState("context-1")
        self.assertEqual(canonical["queueSongIds"], ["song-1", "song-2"])
        self.assertEqual(canonical["controlVersion"], 1)

        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-1",
            device_session_id="device:controller-1",
        )
        self.messages(player)
        self.messages(controller)
        recovered = self.emit_strict(
            controller,
            "command",
            "player.pause",
            "pause-single-context",
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

    def test_context_ensure_response_emit_failure_replays_persisted_response(self):
        client = self.ready_strict_client()
        request = {
            "type": "command",
            "action": "playback.context.ensure",
            "requestId": "context-ensure-response-failure",
            "payload": {
                "deviceSessionId": "device:phone-1",
                "queueSongIds": ["song-2", "song-1"],
                "currentIndex": 0,
                "positionMs": 1200,
                "state": "playing",
            },
        }
        real_emit = emo_ws.socketio.emit

        def fail_direct_response(event, message, **kwargs):
            if message["action"] == "playback.context.ensure":
                raise RuntimeError("injected direct response failure")
            return real_emit(event, message, **kwargs)

        with mock.patch(
            "supysonic.emo.ws_store._new_playback_context_id",
            return_value="context-1",
        ), mock.patch.object(
            emo_ws,
            "ensureStrictPlaybackContextState",
            wraps=emo_ws.ensureStrictPlaybackContextState,
        ) as ensure_context, mock.patch.object(
            emo_ws.socketio,
            "emit",
            side_effect=fail_direct_response,
        ):
            client.emit("message", request, namespace="/emo")

        self.assertEqual(self.messages(client), [])
        persisted = getPlaybackContextState("context-1")
        self.assertEqual(persisted["queueSongIds"], ["song-2", "song-1"])
        self.assertEqual(ensure_context.call_count, 1)

        client.emit("message", request, namespace="/emo")
        replay = self.messages(client)
        self.assertEqual(
            [message["action"] for message in replay],
            ["playback.context.ensure"],
        )
        self.assertEqual(replay[0]["requestId"], request["requestId"])
        self.assertEqual(replay[0]["payload"]["version"], 1)
        self.assertEqual(ensure_context.call_count, 1)

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
                "positionSampledAtServerMs": 1,
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
        self.assertEqual(persisted["queueRevision"], 2)
        self.assertEqual(persisted["controlVersion"], 1)
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
        self.assertEqual(getPlaybackContextState("context-1"), persisted)

    def test_context_ensure_retry_after_runtime_reset_uses_persisted_context(self):
        client = self.ready_strict_client()
        first = self.create_context(client, request_id="context-create-first")
        get_state()._playback_contexts.clear()

        replay = self.create_context(client, request_id="context-create-retry")

        self.assertEqual(replay[0]["payload"], first[0]["payload"])

    def test_context_ensure_keeps_existing_non_idle_context(self):
        client = self.ready_strict_client()
        self.create_context(client, request_id="context-create-first")

        ensured = self.create_context(
            client,
            request_id="context-create-conflict",
            queue_song_ids=["other-song"],
        )

        self.assertEqual(ensured[0]["payload"]["playbackContextId"], "context-1")
        self.assertEqual(ensured[0]["payload"]["queueSongIds"], ["song-2", "song-1"])
        self.assertEqual(ensured[0]["payload"]["version"], 1)

    def test_closed_context_is_a_terminal_tombstone(self):
        client = self.ready_strict_client()
        self.create_context(client)

        stale = self.emit_strict(
            client,
            "command",
            "playback.context.close",
            "context-close-stale",
            {
                "playbackContextId": "context-1",
                "expectedEpoch": 1,
                "baseVersion": 2,
            },
        )
        self.assertEqual(stale[0]["payload"]["code"], "stale_version")
        self.assertEqual(
            {
                field: stale[0]["payload"][field]
                for field in (
                    "currentEpoch",
                    "currentVersion",
                    "currentQueueRevision",
                    "currentControlVersion",
                )
            },
            {
                "currentEpoch": 1,
                "currentVersion": 1,
                "currentQueueRevision": 1,
                "currentControlVersion": 1,
            },
        )
        self.assertEqual(getPlaybackContextState("context-1")["version"], 1)

        client.emit(
            "message",
            {
                "type": "command",
                "action": "playback.context.close",
                "requestId": "context-close-1",
                "payload": {
                    "playbackContextId": "context-1",
                    "expectedEpoch": 1,
                    "baseVersion": 1,
                },
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

        exact_replay = self.emit_strict(
            client,
            "command",
            "playback.context.close",
            "context-close-exact-replay",
            {
                "playbackContextId": "context-1",
                "expectedEpoch": 1,
                "baseVersion": 1,
            },
        )
        self.assertEqual(
            [message["action"] for message in exact_replay],
            ["system.ack"],
        )
        self.assertEqual(exact_replay[0]["payload"], closed_messages[0]["payload"])
        self.assertEqual(getPlaybackContextState("context-1")["version"], 2)

        get_state()._playback_contexts.clear()
        restart_replay = self.emit_strict(
            client,
            "command",
            "playback.context.close",
            "context-close-restart-replay",
            {
                "playbackContextId": "context-1",
                "expectedEpoch": 1,
                "baseVersion": 1,
            },
        )
        self.assertEqual(
            [message["action"] for message in restart_replay],
            ["system.ack"],
        )
        self.assertEqual(restart_replay[0]["payload"], closed_messages[0]["payload"])

        mismatched_replay = self.emit_strict(
            client,
            "command",
            "playback.context.close",
            "context-close-mismatch",
            {
                "playbackContextId": "context-1",
                "expectedEpoch": 1,
                "baseVersion": 2,
            },
        )
        self.assertEqual(
            [message["action"] for message in mismatched_replay],
            ["system.error"],
        )
        self.assertEqual(mismatched_replay[0]["payload"]["code"], "context_closed")
        self.assertEqual(
            {
                field: mismatched_replay[0]["payload"][field]
                for field in (
                    "currentEpoch",
                    "currentVersion",
                    "currentQueueRevision",
                    "currentControlVersion",
                )
            },
            {
                "currentEpoch": 1,
                "currentVersion": 2,
                "currentQueueRevision": 1,
                "currentControlVersion": 1,
            },
        )
        self.assertEqual(getPlaybackContextState("context-1")["version"], 2)

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
                    "positionSampledAtServerMs": 1,
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

        before_stale = dict(persisted)
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
                "positionSampledAtServerMs": 1,
                "baseQueueRevision": 1,
            },
        }
        client.emit("message", stale, namespace="/emo")
        error = self.messages(client)[0]
        self.assertEqual(error["payload"]["code"], "stale_version")
        self.assertEqual(error["payload"]["currentEpoch"], 1)
        self.assertEqual(error["payload"]["currentControlVersion"], 2)
        self.assertEqual(error["payload"]["currentQueueRevision"], 2)
        self.assertEqual(error["payload"]["currentVersion"], 2)
        after_stale = getPlaybackContextState("context-1")
        self.assertEqual(after_stale, before_stale)

    def test_queue_sync_natural_progress_keeps_control_and_applied(self):
        client = self.ready_strict_client()
        self.create_context(client)
        self.emit_strict(
            client,
            "event",
            "playback.update",
            "natural-progress-baseline",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-2",
                "positionMs": 1200,
                "positionSampledAtServerMs": 100,
                "playbackRate": 1.0,
                "clientSeq": 1,
            },
        )

        scenarios = (
            ("append", ["song-2", "song-1", "song-50"], 1800, 200, 2),
            ("position", ["song-2", "song-1", "song-50"], 2400, 300, 3),
        )
        for name, queue_song_ids, position_ms, sampled_at_ms, expected_version in scenarios:
            with self.subTest(name=name):
                before = getPlaybackContextState("context-1")
                messages = self.emit_strict(
                    client,
                    "state",
                    "queue.context.sync",
                    "natural-progress-%s" % name,
                    {
                        "playbackContextId": "context-1",
                        "deviceSessionId": "device:phone-1",
                        "queueSongIds": queue_song_ids,
                        "currentIndex": 0,
                        "positionMs": position_ms,
                        "positionSampledAtServerMs": sampled_at_ms,
                        "baseQueueRevision": before["queueRevision"],
                    },
                )

                self.assertEqual(
                    [message["action"] for message in messages],
                    ["system.ack", "queue.context.sync"],
                )
                persisted = getPlaybackContextState("context-1")
                self.assertEqual(persisted["version"], expected_version)
                self.assertEqual(
                    persisted["queueRevision"],
                    expected_version,
                )
                self.assertEqual(persisted["controlVersion"], 1)
                self.assertEqual(persisted["positionMs"], position_ms)
                self.assertEqual(
                    persisted["positionSampledAtServerMs"],
                    sampled_at_ms,
                )
                device = emo_ws.getDevicePlaybackState(
                    "context-1",
                    "phone-1",
                )
                self.assertEqual(device["appliedControlVersion"], 1)
                self.assertEqual(device["positionMs"], position_ms)
                self.assertEqual(
                    device["positionSampledAtServerMs"],
                    sampled_at_ms,
                )

    def test_queue_sync_advances_authority_applied_and_accepts_passive(self):
        client = self.ready_strict_client()
        self.create_context(client)
        self.emit_strict(
            client,
            "event",
            "playback.update",
            "queue-applied-baseline",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-2",
                "positionMs": 1200,
                "positionSampledAtServerMs": 100,
                "playbackRate": 1.25,
                "volume": 40,
                "muted": True,
                "clientSeq": 1,
            },
        )

        messages = self.emit_strict(
            client,
            "state",
            "queue.context.sync",
            "queue-applied-sync",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "queueSongIds": ["song-2", "song-1"],
                "currentIndex": 1,
                "positionMs": 500,
                "positionSampledAtServerMs": 200,
                "baseQueueRevision": 1,
                "baseControlVersion": 1,
            },
        )
        queue_push = next(
            message
            for message in messages
            if message["action"] == "queue.context.sync"
        )
        self.assertEqual(queue_push["payload"]["controlVersion"], 2)
        device = emo_ws.getDevicePlaybackState("context-1", "phone-1")
        self.assertEqual(device["appliedControlVersion"], 2)
        self.assertEqual(device["trackId"], "song-1")
        self.assertEqual(device["positionMs"], 500)
        self.assertEqual(device["positionSampledAtServerMs"], 200)
        self.assertEqual(device["playbackRate"], 1.25)
        self.assertEqual(device["volume"], 40)
        self.assertIs(device["muted"], True)
        self.assertEqual(device["clientSeq"], 1)

        status = self.emit_strict(
            client,
            "state",
            "playback.context.status",
            "queue-applied-status",
            {"playbackContextId": "context-1"},
        )
        status_message = next(
            message
            for message in status
            if message["action"] == "playback.context.status"
            and "requestId" in message
        )
        status_device = status_message["payload"]["deviceStates"][0]
        self.assertEqual(status_device["appliedControlVersion"], 2)
        self.assertEqual(status_device["trackId"], "song-1")
        self.assertEqual(status_device["positionMs"], 500)
        self.assertEqual(status_device["playbackRate"], 1.25)
        self.assertEqual(status_device["volume"], 40)
        self.assertIs(status_device["muted"], True)

        context_before_stale = getPlaybackContextState("context-1")
        device_before_stale = emo_ws.getDevicePlaybackState(
            "context-1",
            "phone-1",
        )
        stale = self.emit_strict(
            client,
            "event",
            "playback.update",
            "queue-applied-stale",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-2",
                "positionMs": 1200,
                "positionSampledAtServerMs": 250,
                "playbackRate": 1.25,
                "clientSeq": 2,
            },
        )
        self.assertEqual(
            [message["action"] for message in stale],
            ["system.error"],
        )
        conflict = stale[0]["payload"]
        self.assertEqual(conflict["action"], "playback.update")
        self.assertEqual(conflict["code"], "conflict")
        self.assertTrue(conflict["retryable"])
        self.assertEqual(conflict["currentControlVersion"], 2)
        self.assertEqual(
            getPlaybackContextState("context-1"),
            context_before_stale,
        )
        self.assertEqual(
            emo_ws.getDevicePlaybackState("context-1", "phone-1"),
            device_before_stale,
        )

        passive = self.emit_strict(
            client,
            "event",
            "playback.update",
            "queue-applied-passive",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 2,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 600,
                "positionSampledAtServerMs": 300,
                "playbackRate": 1.25,
                "clientSeq": 2,
            },
        )
        self.assertEqual(
            [message["action"] for message in passive],
            ["playback.update"],
        )
        persisted = getPlaybackContextState("context-1")
        self.assertEqual(
            (
                persisted["version"],
                persisted["queueRevision"],
                persisted["controlVersion"],
            ),
            (2, 2, 2),
        )

    def test_status_allows_equal_applied_with_different_actual_state(self):
        client = self.ready_strict_client()
        self.create_context(client, state="paused")
        self.messages(client)

        update = self.emit_strict(
            client,
            "event",
            "playback.update",
            "passive-actual-playing",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-2",
                "positionMs": 1500,
                "positionSampledAtServerMs": 100,
                "playbackRate": 1.5,
                "clientSeq": 1,
            },
        )
        self.assertEqual(
            [message["action"] for message in update],
            ["playback.update"],
        )

        status = self.emit_strict(
            client,
            "state",
            "playback.context.status",
            "status-context-paused-device-playing",
            {"playbackContextId": "context-1"},
        )
        status_message = next(
            message
            for message in status
            if message["action"] == "playback.context.status"
            and "requestId" in message
        )
        payload = status_message["payload"]
        device = payload["deviceStates"][0]

        self.assertEqual(payload["playbackContext"]["state"], "paused")
        self.assertEqual(
            payload["playbackContext"]["controlVersion"],
            1,
        )
        self.assertEqual(device["state"], "playing")
        self.assertEqual(device["appliedControlVersion"], 1)
        self.assertEqual(device["positionMs"], 1500)
        self.assertEqual(device["playbackRate"], 1.5)

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

    def test_strict_control_persists_exact_requester_and_routed_generations(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-exact",
            device_session_id="device:controller-exact",
        )

        with mock.patch.object(socketio, "start_background_task"):
            response = self.emit_strict(
                controller,
                "command",
                "player.seek",
                "seek-exact-generation",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                    "positionMs": 42000,
                },
            )

        self.assertTrue(
            any(message["action"] == "system.ack" for message in response)
        )
        state = get_state()
        requester_sid = state.get_sid_for_client(
            "controller-exact",
            user_name="alice",
        )
        authority_sid = state.get_sid_for_client(
            "phone-1",
            user_name="alice",
        )
        requester = state.get_current_physical_generation(
            "alice",
            "controller-exact",
            "device:controller-exact",
            expected_sid=requester_sid,
        )
        authority = state.get_current_physical_generation(
            "alice",
            "phone-1",
            "device:phone-1",
            expected_sid=authority_sid,
        )
        transaction = emo_ws.getPlaybackControlTransaction(
            "context-1",
            1,
            2,
        )

        self.assertEqual(transaction["requestingClientId"], "controller-exact")
        self.assertEqual(
            transaction["requestingDeviceSessionId"],
            requester["deviceSessionId"],
        )
        self.assertEqual(
            transaction["requestingConnectionNonce"],
            requester["connectionNonce"],
        )
        self.assertEqual(transaction["requestingConnectionEpoch"], 1)
        self.assertEqual(transaction["authorityClientId"], "phone-1")
        self.assertEqual(
            transaction["authorityDeviceSessionId"],
            authority["deviceSessionId"],
        )
        self.assertEqual(
            transaction["routedConnectionNonce"],
            authority["connectionNonce"],
        )
        self.assertEqual(transaction["routedConnectionEpoch"], 1)
        self.assertNotIn("sid", transaction)
        self.assertNotIn("effectiveAtServerMs", transaction)

    def test_strict_control_orders_emit_eligibility_watchdog_and_ack(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-order",
            device_session_id="device:controller-order",
        )
        events = []
        watchdog_transactions = []
        real_emit = emo_ws._emit_message
        real_mutate = emo_ws.mutateStrictPlaybackContextControl
        real_mark = emo_ws.markPlaybackControlTransactionExecutionEligible
        real_ack = emo_ws._send_ack
        clock = {"value": 1000}

        def controlled_time():
            clock["value"] += 10
            return clock["value"]

        def record_mutate(*args, **kwargs):
            events.append("mutate")
            return real_mutate(*args, **kwargs)

        def record_emit(message, *args, **kwargs):
            if message.get("action") == "player.seek":
                events.append("emit")
            return real_emit(message, *args, **kwargs)

        def record_mark(*args, **kwargs):
            result = real_mark(*args, **kwargs)
            events.append("eligible")
            return result

        def record_watchdog(transaction):
            events.append("watchdog")
            watchdog_transactions.append(dict(transaction))

        def record_ack(request_id=None, payload=None):
            if request_id == "seek-order":
                events.append("ack")
            return real_ack(request_id, payload)

        with mock.patch.object(
            emo_ws,
            "_server_time_ms",
            side_effect=controlled_time,
        ), mock.patch.object(
            emo_ws,
            "mutateStrictPlaybackContextControl",
            side_effect=record_mutate,
        ), mock.patch.object(
            emo_ws,
            "_emit_message",
            side_effect=record_emit,
        ), mock.patch.object(
            emo_ws,
            "markPlaybackControlTransactionExecutionEligible",
            side_effect=record_mark,
        ), mock.patch.object(
            emo_ws,
            "_start_control_watchdog",
            side_effect=record_watchdog,
        ), mock.patch.object(
            emo_ws,
            "_send_ack",
            side_effect=record_ack,
        ):
            response = self.emit_strict(
                controller,
                "command",
                "player.seek",
                "seek-order",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                    "positionMs": 9000,
                },
            )

        self.assertEqual([message["action"] for message in response], ["system.ack"])
        self.assertEqual(
            events,
            ["mutate", "emit", "eligible", "watchdog", "ack"],
        )
        self.assertEqual(len(watchdog_transactions), 1)
        transaction = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(
            watchdog_transactions[0]["executionEligibleAtMs"],
            transaction["executionEligibleAtMs"],
        )
        self.assertEqual(
            watchdog_transactions[0]["watchdogDeadlineAtMs"],
            transaction["executionEligibleAtMs"]
            + transaction["executionTimeoutMs"]
            + 2000,
        )
        self.assertLess(
            transaction["acceptedAtMs"],
            transaction["executionEligibleAtMs"],
        )

    def _run_immediate_feedback_race(
        self,
        execution_status,
        fail_eligibility=False,
    ):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-feedback-race",
            device_session_id="device:controller-feedback-race",
        )
        self.emit_strict(
            controller,
            "state",
            "playback.context.subscribe",
            "subscribe-feedback-race",
            {"playbackContextId": "context-1"},
        )
        self.messages(player)
        self.messages(controller)

        events = []
        mark_entered = threading.Event()
        feedback_started = threading.Event()
        feedback_finished = threading.Event()
        feedback_apply_before_mark = []
        feedback_errors = []
        feedback_thread = []
        watchdog_transactions = []
        real_apply = emo_ws.applyStrictPlaybackUpdate
        real_emit = emo_ws._emit_message
        real_mark = emo_ws.markPlaybackControlTransactionExecutionEligible
        real_mutate = emo_ws.mutateStrictPlaybackContextControl
        real_ack = emo_ws._send_ack

        feedback_payload = {
            "playbackContextId": "context-1",
            "deviceSessionId": "device:phone-1",
            "origin": "remoteCommand",
            "executionStatus": execution_status,
            "commandControlVersion": 2,
            "appliedControlVersion": (
                2 if execution_status == "committed" else 1
            ),
            "state": "paused" if execution_status == "committed" else "playing",
            "trackId": "song-2",
            "positionMs": 1200,
            "positionSampledAtServerMs": 1,
            "playbackRate": 1.0,
            "clientSeq": 1,
        }
        if execution_status == "failed":
            feedback_payload.update(
                {
                    "errorCode": "track_load_failed",
                    "errorMessage": "track load failed",
                }
            )

        def send_feedback():
            feedback_started.set()
            try:
                player.emit(
                    "message",
                    {
                        "type": "event",
                        "action": "playback.update",
                        "requestId": "feedback-race-%s" % execution_status,
                        "payload": feedback_payload,
                    },
                    namespace="/emo",
                )
            except Exception as exc:  # pragma: nocover - asserted below
                feedback_errors.append(exc)
            finally:
                feedback_finished.set()

        def record_mutate(*args, **kwargs):
            events.append("mutate")
            return real_mutate(*args, **kwargs)

        def record_emit(message, *args, **kwargs):
            result = real_emit(message, *args, **kwargs)
            if message.get("action") == "player.pause":
                thread = threading.Thread(target=send_feedback)
                feedback_thread.append(thread)
                thread.start()
                self.assertTrue(feedback_started.wait(1))
                events.append("emit")
            return result

        def record_apply(*args, **kwargs):
            if kwargs.get("require_execution_eligible"):
                feedback_apply_before_mark.append(mark_entered.is_set())
            return real_apply(*args, **kwargs)

        def record_mark(*args, **kwargs):
            mark_entered.set()
            if fail_eligibility:
                events.append("eligible")
                raise RuntimeError("injected eligibility failure")
            result = real_mark(*args, **kwargs)
            events.append("eligible")
            return result

        def record_watchdog(transaction):
            events.append("watchdog")
            watchdog_transactions.append(dict(transaction))

        def record_ack(request_id=None, payload=None):
            if request_id == "pause-feedback-race":
                events.append("ack")
            return real_ack(request_id, payload)

        with mock.patch.object(
            emo_ws,
            "mutateStrictPlaybackContextControl",
            side_effect=record_mutate,
        ), mock.patch.object(
            emo_ws,
            "_emit_message",
            side_effect=record_emit,
        ), mock.patch.object(
            emo_ws,
            "applyStrictPlaybackUpdate",
            side_effect=record_apply,
        ), mock.patch.object(
            emo_ws,
            "markPlaybackControlTransactionExecutionEligible",
            side_effect=record_mark,
        ), mock.patch.object(
            emo_ws,
            "_start_control_watchdog",
            side_effect=record_watchdog,
        ), mock.patch.object(
            emo_ws,
            "_send_ack",
            side_effect=record_ack,
        ):
            response = self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-feedback-race",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )
            self.assertEqual(len(feedback_thread), 1)
            feedback_thread[0].join(2)
            self.assertFalse(feedback_thread[0].is_alive())
            self.assertTrue(feedback_finished.is_set())
            self.assertEqual(feedback_errors, [])
        self.assertEqual(
            events,
            (
                ["mutate", "emit", "eligible"]
                if fail_eligibility
                else ["mutate", "emit", "eligible", "watchdog", "ack"]
            ),
        )
        self.assertEqual(feedback_apply_before_mark, [True])
        if fail_eligibility:
            self.assertFalse(
                any(message["action"] == "system.ack" for message in response)
            )
            self.assertTrue(
                any(
                    message["action"] == "system.error"
                    and message["payload"].get("code") == "internal_error"
                    for message in response
                )
            )
            transaction = emo_ws.getPlaybackControlTransaction(
                "context-1",
                1,
                2,
            )
            self.assertEqual(transaction["status"], "failed")
            self.assertEqual(transaction["errorCode"], "execution_unknown")
            self.assertNotIn("executionEligibleAtMs", transaction)
            self.assertNotIn("watchdogDeadlineAtMs", transaction)
            self.assertEqual(watchdog_transactions, [])
            self.assertEqual(
                len(
                    [
                        message
                        for message in self.messages(player)
                        if message["action"] == "player.pause"
                    ]
                ),
                1,
            )
            self.assertIsNone(
                emo_ws.getDevicePlaybackState("context-1", "phone-1")
            )
            return

        self.assertEqual(len(watchdog_transactions), 1)
        self.assertTrue(
            any(message["action"] == "system.ack" for message in response)
        )
        transaction = emo_ws.getPlaybackControlTransaction(
            "context-1",
            1,
            2,
        )
        self.assertEqual(transaction["status"], execution_status)
        self.assertIsNotNone(transaction["executionEligibleAtMs"])
        self.assertIsNotNone(transaction["watchdogDeadlineAtMs"])
        self.assertGreaterEqual(
            transaction["terminalAtMs"],
            transaction["executionEligibleAtMs"],
        )
        self.assertEqual(
            transaction["watchdogDeadlineAtMs"],
            transaction["executionEligibleAtMs"]
            + transaction["executionTimeoutMs"]
            + 2000,
        )
        self.assertEqual(
            watchdog_transactions[0]["executionEligibleAtMs"],
            transaction["executionEligibleAtMs"],
        )
        if execution_status == "failed":
            self.assertEqual(transaction["errorCode"], "track_load_failed")
            self.assertEqual(transaction["errorMessage"], "track load failed")
        controller_events = self.messages(controller)
        self.assertTrue(
            any(
                message["action"] == "playback.update"
                and message["payload"].get("executionStatus")
                == execution_status
                for message in controller_events
            )
        )
        self.assertFalse(
            any(
                message["action"] == "system.error"
                and message["payload"].get("code") == "internal_error"
                for message in controller_events
            )
        )

    def test_strict_control_immediate_committed_feedback_waits_for_eligibility(self):
        self._run_immediate_feedback_race("committed")

    def test_strict_control_immediate_failed_feedback_waits_for_eligibility(self):
        self._run_immediate_feedback_race("failed")

    def test_strict_control_eligibility_failure_rejects_waiting_feedback(self):
        self._run_immediate_feedback_race(
            "committed",
            fail_eligibility=True,
        )

    def test_real_authority_replacement_first_rejects_old_generation(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-real-replacement-first",
            device_session_id="device:controller-real-replacement-first",
        )
        self.messages(player)
        self.messages(controller)

        replacement = self.authenticated_unregistered_client(
            "phone-1-replacement-first"
        )
        self.register_real_replacement(
            replacement,
            "register-authority-replacement-first",
            "phone-1",
            "device:phone-1-replacement",
        )
        self.messages(replacement)

        before = getPlaybackContextState("context-1")
        with mock.patch.object(emo_ws, "_start_control_watchdog") as watchdog:
            response = self.emit_strict(
                controller,
                "command",
                "player.seek",
                "seek-authority-replacement-first",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                    "positionMs": 1000,
                },
            )

        self.assertEqual(response[0]["payload"]["code"], "authority_offline")
        self.assertEqual(getPlaybackContextState("context-1"), before)
        self.assertIsNone(
            emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        )
        self.assertFalse(
            any(
                message["action"] == "player.seek"
                for message in self.messages(replacement)
            )
        )
        self.assertFalse(
            any(message["action"] == "system.ack" for message in response)
        )
        watchdog.assert_not_called()

    def test_real_authority_replacement_waits_until_control_dispatch_finishes(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-real-control-first",
            device_session_id="device:controller-real-control-first",
        )
        self.messages(player)
        self.messages(controller)
        replacement = self.authenticated_unregistered_client(
            "phone-1-control-first"
        )
        old_authority_sid = get_state().get_sid_for_client(
            "phone-1",
            user_name="alice",
        )

        replacement_trying = threading.Event()
        replacement_entered = threading.Event()
        dispatch_window = threading.Event()
        control_result = []
        control_errors = []
        replacement_errors = []
        watchdog_transactions = []
        authority_commands = []
        real_lifecycle = emo_ws.strictPhysicalGenerationLockSet
        real_emit = emo_ws._emit_message
        replacement_thread = None

        def observed_lifecycle(keys):
            @contextmanager
            def observed():
                is_replacement = threading.current_thread() is replacement_thread
                if is_replacement:
                    replacement_trying.set()
                with real_lifecycle(keys):
                    if is_replacement:
                        replacement_entered.set()
                    yield

            return observed()

        def register_replacement():
            try:
                self.register_real_replacement(
                    replacement,
                    "register-authority-control-first",
                    "phone-1",
                    "device:phone-1-control-first",
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                replacement_errors.append(exc)

        replacement_thread = threading.Thread(target=register_replacement)

        def emit_and_hold(message, *args, **kwargs):
            result = real_emit(message, *args, **kwargs)
            if message.get("action") == "player.seek":
                authority_commands.append((message, args[0] if args else None))
                replacement_thread.start()
                if not replacement_trying.wait(1):
                    raise RuntimeError("replacement did not reach lifecycle lock")
                if not dispatch_window.wait(2):
                    raise RuntimeError("dispatch test window was not released")
            return result

        def run_control():
            try:
                control_result.extend(
                    self.emit_strict(
                        controller,
                        "command",
                        "player.seek",
                        "seek-authority-control-first",
                        {
                            "playbackContextId": "context-1",
                            "baseControlVersion": 1,
                            "positionMs": 2000,
                        },
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                control_errors.append(exc)

        control_thread = threading.Thread(target=run_control)
        with mock.patch.object(
            emo_ws,
            "strictPhysicalGenerationLockSet",
            side_effect=observed_lifecycle,
        ), mock.patch.object(
            emo_ws,
            "_emit_message",
            side_effect=emit_and_hold,
        ), mock.patch.object(
            emo_ws,
            "_start_control_watchdog",
            side_effect=lambda transaction: watchdog_transactions.append(
                dict(transaction)
            ),
        ):
            control_thread.start()
            self.assertTrue(replacement_trying.wait(2))
            self.assertFalse(replacement_entered.is_set())
            dispatch_window.set()
            control_thread.join(2)
            replacement_thread.join(2)

        self.assertFalse(control_thread.is_alive())
        self.assertFalse(replacement_thread.is_alive())
        self.assertEqual(control_errors, [])
        self.assertEqual(replacement_errors, [])
        self.assertTrue(replacement_entered.is_set())
        self.assertEqual(
            len(
                [
                    message
                    for message in self.messages(replacement)
                    if message["action"] == "player.seek"
                ]
            ),
            0,
        )
        self.assertEqual(
            len(
                [
                    message
                    for message, target_sid in authority_commands
                    if message["action"] == "player.seek"
                    and target_sid == old_authority_sid
                ]
            ),
            1,
        )
        self.assertTrue(
            any(message["action"] == "system.ack" for message in control_result)
        )
        self.assertEqual(len(watchdog_transactions), 1)
        transaction = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(transaction["status"], "failed")
        self.assertEqual(transaction["errorCode"], "execution_unknown")
        self.assertIsNotNone(transaction["executionEligibleAtMs"])
        self.assertGreaterEqual(
            transaction["terminalAtMs"],
            transaction["executionEligibleAtMs"],
        )

    def test_real_replacement_attempt_after_final_recheck_cannot_precede_emit(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-final-recheck-race",
            device_session_id="device:controller-final-recheck-race",
        )
        self.messages(player)
        self.messages(controller)
        old_authority_sid = get_state().get_sid_for_client(
            "phone-1",
            user_name="alice",
        )
        replacement = self.authenticated_unregistered_client(
            "phone-1-final-recheck"
        )

        replacement_trying = threading.Event()
        replacement_entered = threading.Event()
        restore_ready = threading.Event()
        allow_restore = threading.Event()
        control_result = []
        control_errors = []
        replacement_errors = []
        authority_commands = []
        real_lifecycle = emo_ws.strictPhysicalGenerationLockSet
        real_emit = emo_ws._emit_message
        real_restore = get_state().restore_playback_context
        replacement_thread = None

        def observed_lifecycle(keys):
            @contextmanager
            def observed():
                is_replacement = threading.current_thread() is replacement_thread
                if is_replacement:
                    replacement_trying.set()
                with real_lifecycle(keys):
                    if is_replacement:
                        replacement_entered.set()
                    yield

            return observed()

        def register_replacement():
            try:
                self.register_real_replacement(
                    replacement,
                    "register-authority-final-recheck",
                    "phone-1",
                    "device:phone-1-final-recheck",
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                replacement_errors.append(exc)

        replacement_thread = threading.Thread(target=register_replacement)

        def observe_emit(message, *args, **kwargs):
            if message.get("action") == "player.pause":
                authority_commands.append((message, args[0] if args else None))
            return real_emit(message, *args, **kwargs)

        def restore_then_start(*args, **kwargs):
            result = real_restore(*args, **kwargs)
            replacement_thread.start()
            if not replacement_trying.wait(1):
                raise RuntimeError("replacement did not reach final-check race")
            restore_ready.set()
            if not allow_restore.wait(2):
                raise RuntimeError("final-check race was not released")
            return result

        def run_control():
            try:
                control_result.extend(
                    self.emit_strict(
                        controller,
                        "command",
                        "player.pause",
                        "pause-final-recheck-race",
                        {
                            "playbackContextId": "context-1",
                            "baseControlVersion": 1,
                        },
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                control_errors.append(exc)

        control_thread = threading.Thread(target=run_control)
        with mock.patch.object(
            emo_ws,
            "strictPhysicalGenerationLockSet",
            side_effect=observed_lifecycle,
        ), mock.patch.object(
            get_state(),
            "restore_playback_context",
            side_effect=restore_then_start,
        ), mock.patch.object(
            emo_ws,
            "_emit_message",
            side_effect=observe_emit,
        ), mock.patch.object(emo_ws, "_start_control_watchdog"):
            control_thread.start()
            self.assertTrue(restore_ready.wait(2))
            self.assertFalse(replacement_entered.is_set())
            allow_restore.set()
            control_thread.join(2)
            replacement_thread.join(2)

        self.assertFalse(control_thread.is_alive())
        self.assertFalse(replacement_thread.is_alive())
        self.assertEqual(control_errors, [])
        self.assertEqual(replacement_errors, [])
        self.assertTrue(replacement_entered.is_set())
        self.assertTrue(
            any(message["action"] == "system.ack" for message in control_result)
        )
        self.assertEqual(
            len(
                [
                    message
                    for message in self.messages(replacement)
                    if message["action"] == "player.pause"
                ]
            ),
            0,
        )
        self.assertEqual(
            len(
                [
                    message
                    for message, target_sid in authority_commands
                    if message["action"] == "player.pause"
                    and target_sid == old_authority_sid
                ]
            ),
            1,
        )
        transaction = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(transaction["status"], "failed")
        self.assertEqual(transaction["errorCode"], "execution_unknown")
        self.assertIsNotNone(transaction["executionEligibleAtMs"])

    def test_real_authority_disconnect_waits_until_control_dispatch_finishes(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-disconnect-control-first",
            device_session_id="device:controller-disconnect-control-first",
        )
        self.messages(player)
        self.messages(controller)
        old_authority_sid = get_state().get_sid_for_client(
            "phone-1",
            user_name="alice",
        )

        disconnect_trying = threading.Event()
        disconnect_entered = threading.Event()
        dispatch_window = threading.Event()
        disconnect_finished = threading.Event()
        disconnect_errors = []
        control_result = []
        control_errors = []
        authority_commands = []
        real_lifecycle = emo_ws.strictPhysicalGenerationLockSet
        real_emit = emo_ws._emit_message
        disconnect_thread = None

        def observed_lifecycle(keys):
            @contextmanager
            def observed():
                is_disconnect = threading.current_thread() is disconnect_thread
                if is_disconnect:
                    disconnect_trying.set()
                with real_lifecycle(keys):
                    if is_disconnect:
                        disconnect_entered.set()
                    yield

            return observed()

        def disconnect_authority():
            try:
                player.disconnect(namespace="/emo")
            except BaseException as exc:  # pragma: no cover - asserted below
                disconnect_errors.append(exc)
            finally:
                disconnect_finished.set()

        disconnect_thread = threading.Thread(target=disconnect_authority)

        def emit_and_hold(message, *args, **kwargs):
            if message.get("action") == "player.seek":
                authority_commands.append((message, args[0] if args else None))
            result = real_emit(message, *args, **kwargs)
            if message.get("action") == "player.seek":
                disconnect_thread.start()
                if not disconnect_trying.wait(1):
                    raise RuntimeError("disconnect did not reach lifecycle lock")
                if not dispatch_window.wait(2):
                    raise RuntimeError("disconnect test window was not released")
            return result

        def run_control():
            try:
                control_result.extend(
                    self.emit_strict(
                        controller,
                        "command",
                        "player.seek",
                        "seek-authority-disconnect-race",
                        {
                            "playbackContextId": "context-1",
                            "baseControlVersion": 1,
                            "positionMs": 3000,
                        },
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                control_errors.append(exc)

        control_thread = threading.Thread(target=run_control)
        with mock.patch.object(
            emo_ws,
            "strictPhysicalGenerationLockSet",
            side_effect=observed_lifecycle,
        ), mock.patch.object(
            emo_ws,
            "_emit_message",
            side_effect=emit_and_hold,
        ), mock.patch.object(emo_ws, "_start_control_watchdog"):
            control_thread.start()
            self.assertTrue(disconnect_trying.wait(2))
            self.assertFalse(disconnect_entered.is_set())
            dispatch_window.set()
            control_thread.join(2)
            disconnect_thread.join(2)

        self.assertFalse(control_thread.is_alive())
        self.assertFalse(disconnect_thread.is_alive())
        self.assertTrue(disconnect_finished.is_set())
        self.assertEqual(control_errors, [])
        self.assertEqual(disconnect_errors, [])
        self.assertTrue(disconnect_entered.is_set())
        self.assertTrue(
            any(message["action"] == "system.ack" for message in control_result)
        )
        self.assertEqual(
            len(
                [
                    message
                    for message, target_sid in authority_commands
                    if message["action"] == "player.seek"
                    and target_sid == old_authority_sid
                ]
            ),
            1,
        )
        transaction = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(transaction["status"], "failed")
        self.assertEqual(transaction["errorCode"], "execution_unknown")
        self.assertIsNotNone(transaction["executionEligibleAtMs"])
        self.assertIsNone(get_state().get_sid_for_client("phone-1", "alice"))

    def test_real_authority_disconnect_first_rejects_control_without_mutation(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-disconnect-first",
            device_session_id="device:controller-disconnect-first",
        )
        player.disconnect(namespace="/emo")
        before = getPlaybackContextState("context-1")

        response = self.emit_strict(
            controller,
            "command",
            "player.pause",
            "pause-authority-disconnect-first",
            {
                "playbackContextId": "context-1",
                "baseControlVersion": 1,
            },
        )

        self.assertEqual(response[0]["payload"]["code"], "authority_offline")
        self.assertEqual(getPlaybackContextState("context-1"), before)
        self.assertIsNone(
            emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        )
        self.assertFalse(
            any(message["action"] == "system.ack" for message in response)
        )

    def test_real_requester_replacement_first_rejects_old_generation(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-requester-replacement-first",
            device_session_id="device:controller-requester-replacement-first",
        )
        self.messages(player)
        self.messages(controller)
        before = getPlaybackContextState("context-1")

        self.replace_registered_generation(
            "controller-requester-replacement-first",
            device_session_id="device:controller-requester-replacement-new",
            sid_suffix="requester-first",
        )
        response = self.emit_strict(
            controller,
            "command",
            "player.seek",
            "seek-requester-replacement-first",
            {
                "playbackContextId": "context-1",
                "baseControlVersion": 1,
                "positionMs": 1000,
            },
        )

        self.assertEqual(response[0]["action"], "system.error")
        self.assertEqual(response[0]["payload"]["code"], "unauthorized")
        self.assertEqual(getPlaybackContextState("context-1"), before)
        self.assertIsNone(
            emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        )
        self.assertFalse(
            any(
                message["action"] == "player.seek"
                for message in self.messages(player)
            )
        )

    def test_real_requester_replacement_waits_until_control_dispatch_finishes(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-requester-control-first",
            device_session_id="device:controller-requester-control-first",
        )
        self.messages(player)
        self.messages(controller)
        old_generation = get_state().get_current_physical_generation(
            "alice",
            "controller-requester-control-first",
            "device:controller-requester-control-first",
        )
        replacement = self.authenticated_unregistered_client(
            "controller-requester-control-first-new"
        )

        replacement_trying = threading.Event()
        replacement_entered = threading.Event()
        replacement_before_ack = threading.Event()
        dispatch_window = threading.Event()
        control_result = []
        control_errors = []
        replacement_errors = []
        watchdog_transactions = []
        ack_sent = threading.Event()
        real_lifecycle = emo_ws.strictPhysicalGenerationLockSet
        real_emit = emo_ws._emit_message
        replacement_thread = None

        def observed_lifecycle(keys):
            @contextmanager
            def observed():
                is_replacement = threading.current_thread() is replacement_thread
                if is_replacement:
                    replacement_trying.set()
                with real_lifecycle(keys):
                    if is_replacement:
                        if not ack_sent.is_set():
                            replacement_before_ack.set()
                        replacement_entered.set()
                    yield

            return observed()

        def register_replacement():
            try:
                self.register_real_replacement(
                    replacement,
                    "register-requester-control-first",
                    "controller-requester-control-first",
                    "device:controller-requester-control-first-new",
                    roles=["controller"],
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                replacement_errors.append(exc)

        replacement_thread = threading.Thread(target=register_replacement)

        def emit_and_hold(message, *args, **kwargs):
            result = real_emit(message, *args, **kwargs)
            if (
                message.get("action") == "system.ack"
                and message.get("requestId") == "seek-requester-control-first"
            ):
                ack_sent.set()
            if message.get("action") == "player.seek":
                replacement_thread.start()
                if not replacement_trying.wait(1):
                    raise RuntimeError("requester replacement did not reach lock")
                if not dispatch_window.wait(2):
                    raise RuntimeError("requester dispatch window was not released")
            return result

        def run_control():
            try:
                controller.emit(
                    "message",
                    {
                        "type": "command",
                        "action": "player.seek",
                        "requestId": "seek-requester-control-first",
                        "payload": {
                            "playbackContextId": "context-1",
                            "baseControlVersion": 1,
                            "positionMs": 2000,
                        },
                    },
                    namespace="/emo",
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                control_errors.append(exc)

        control_thread = threading.Thread(target=run_control)
        with mock.patch.object(
            emo_ws,
            "strictPhysicalGenerationLockSet",
            side_effect=observed_lifecycle,
        ), mock.patch.object(
            emo_ws,
            "_emit_message",
            side_effect=emit_and_hold,
        ), mock.patch.object(
            emo_ws,
            "_start_control_watchdog",
            side_effect=lambda transaction: watchdog_transactions.append(
                dict(transaction)
            ),
        ):
            control_thread.start()
            self.assertTrue(replacement_trying.wait(2))
            self.assertFalse(replacement_entered.is_set())
            dispatch_window.set()
            control_thread.join(2)
            replacement_thread.join(2)

        self.assertFalse(control_thread.is_alive())
        self.assertFalse(replacement_thread.is_alive())
        self.assertEqual(control_errors, [])
        self.assertEqual(replacement_errors, [])
        self.assertTrue(replacement_entered.is_set())
        self.assertFalse(replacement_before_ack.is_set())
        self.assertTrue(ack_sent.is_set())
        self.assertEqual(
            len(
                [
                    message
                    for message in self.messages(player)
                    if message["action"] == "player.seek"
                ]
            ),
            1,
        )
        transaction = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(transaction["requestingDeviceSessionId"], old_generation["deviceSessionId"])
        self.assertEqual(
            transaction["requestingConnectionNonce"],
            old_generation["connectionNonce"],
        )
        self.assertEqual(transaction["requestingConnectionEpoch"], 1)
        self.assertIsNotNone(transaction["executionEligibleAtMs"])
        self.assertEqual(len(watchdog_transactions), 1)

    def test_strict_control_emit_holds_only_generation_and_dispatch_locks(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-lock-boundary",
            device_session_id="device:controller-lock-boundary",
        )
        self.messages(player)
        self.messages(controller)
        active = {
            "generation": 0,
            "context": 0,
            "pair": 0,
            "database": 0,
        }
        observations = []
        real_lifecycle = emo_ws.strictPhysicalGenerationLockSet
        real_context_lock = emo_store._strict_playback_context_lock
        real_pair_transaction = emo_store._strict_authority_pair_transaction
        real_database_transaction = emo_store._strict_playback_context_transaction
        real_emit = emo_ws._emit_message

        def observed_lifecycle(keys):
            @contextmanager
            def observed():
                with real_lifecycle(keys):
                    active["generation"] += 1
                    try:
                        yield
                    finally:
                        active["generation"] -= 1

            return observed()

        def observed_context_lock(*args, **kwargs):
            @contextmanager
            def observed():
                active["context"] += 1
                try:
                    with real_context_lock(*args, **kwargs):
                        yield
                finally:
                    active["context"] -= 1

            return observed()

        def observed_pair_transaction(*args, **kwargs):
            @contextmanager
            def observed():
                active["pair"] += 1
                try:
                    with real_pair_transaction(*args, **kwargs):
                        yield
                finally:
                    active["pair"] -= 1

            return observed()

        def observed_database_transaction(*args, **kwargs):
            @contextmanager
            def observed():
                active["database"] += 1
                try:
                    with real_database_transaction(*args, **kwargs):
                        yield
                finally:
                    active["database"] -= 1

            return observed()

        def record_emit(message, *args, **kwargs):
            if message.get("action") == "player.seek":
                observations.append(dict(active))
            return real_emit(message, *args, **kwargs)

        with mock.patch.object(
            emo_ws,
            "strictPhysicalGenerationLockSet",
            side_effect=observed_lifecycle,
        ), mock.patch.object(
            emo_ws,
            "_emit_message",
            side_effect=record_emit,
        ), mock.patch.object(
            emo_store,
            "_strict_playback_context_lock",
            side_effect=observed_context_lock,
        ), mock.patch.object(
            emo_store,
            "_strict_authority_pair_transaction",
            side_effect=observed_pair_transaction,
        ), mock.patch.object(
            emo_store,
            "_strict_playback_context_transaction",
            side_effect=observed_database_transaction,
        ), mock.patch.object(emo_ws, "_start_control_watchdog"):
            response = self.emit_strict(
                controller,
                "command",
                "player.seek",
                "seek-lock-boundary",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                    "positionMs": 3000,
                },
            )

        self.assertEqual([message["action"] for message in response], ["system.ack"])
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["generation"], 1)
        self.assertEqual(observations[0]["context"], 0)
        self.assertEqual(observations[0]["pair"], 0)
        self.assertEqual(observations[0]["database"], 0)

    def test_strict_control_requester_replacement_fails_before_mutation(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-requester-replacement",
            device_session_id="device:controller-requester-replacement",
        )
        before = getPlaybackContextState("context-1")
        real_mutate = emo_ws.mutateStrictPlaybackContextControl

        def replace_requester_before_validator(*args, **kwargs):
            validator = kwargs["pre_mutation_validator"]

            def replacement_validator(current):
                self.replace_registered_generation(
                    "controller-requester-replacement",
                    sid_suffix="requester-replaced",
                )
                return validator(current)

            kwargs["pre_mutation_validator"] = replacement_validator
            return real_mutate(*args, **kwargs)

        with mock.patch.object(
            emo_ws,
            "mutateStrictPlaybackContextControl",
            side_effect=replace_requester_before_validator,
        ), mock.patch.object(emo_ws, "_start_control_watchdog") as watchdog:
            response = self.emit_strict(
                controller,
                "command",
                "player.seek",
                "seek-requester-replacement",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                    "positionMs": 1000,
                },
            )

        self.assertEqual(response[0]["action"], "system.error")
        self.assertEqual(response[0]["payload"]["code"], "forbidden")
        self.assertEqual(getPlaybackContextState("context-1"), before)
        self.assertIsNone(
            emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        )
        self.assertFalse(
            any(
                message["action"] == "player.seek"
                for message in self.messages(player)
            )
        )
        watchdog.assert_not_called()

    def test_strict_control_authority_replacement_fails_before_mutation(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-authority-replacement",
            device_session_id="device:controller-authority-replacement",
        )
        before = getPlaybackContextState("context-1")
        real_mutate = emo_ws.mutateStrictPlaybackContextControl

        def replace_authority_before_validator(*args, **kwargs):
            validator = kwargs["pre_mutation_validator"]

            def replacement_validator(current):
                self.replace_registered_generation(
                    "phone-1",
                    device_session_id="device:phone-1",
                    sid_suffix="authority-replaced",
                )
                return validator(current)

            kwargs["pre_mutation_validator"] = replacement_validator
            return real_mutate(*args, **kwargs)

        with mock.patch.object(
            emo_ws,
            "mutateStrictPlaybackContextControl",
            side_effect=replace_authority_before_validator,
        ), mock.patch.object(emo_ws, "_start_control_watchdog") as watchdog:
            response = self.emit_strict(
                controller,
                "command",
                "player.seek",
                "seek-authority-replacement",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                    "positionMs": 1000,
                },
            )

        error = next(
            message for message in response if message["action"] == "system.error"
        )
        self.assertEqual(error["payload"]["code"], "authority_offline")
        self.assertEqual(getPlaybackContextState("context-1"), before)
        self.assertIsNone(
            emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        )
        self.assertFalse(
            any(
                message["action"] == "player.seek"
                for message in self.messages(player)
            )
        )
        watchdog.assert_not_called()

    def test_strict_control_authority_replacement_after_commit_is_unknown(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-after-commit",
            device_session_id="device:controller-after-commit",
        )
        state = get_state()
        real_restore = state.restore_playback_context

        def restore_then_replace(*args, **kwargs):
            result = real_restore(*args, **kwargs)
            self.replace_registered_generation(
                "phone-1",
                device_session_id="device:phone-1",
                sid_suffix="authority-after-commit",
            )
            return result

        with mock.patch.object(
            state,
            "restore_playback_context",
            side_effect=restore_then_replace,
        ), mock.patch.object(emo_ws, "_start_control_watchdog") as watchdog:
            response = self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-authority-after-commit",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )

        error = next(
            message for message in response if message["action"] == "system.error"
        )
        self.assertEqual(error["payload"]["code"], "authority_offline")
        persisted = getPlaybackContextState("context-1")
        self.assertEqual(persisted["controlVersion"], 2)
        transaction = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(transaction["status"], "failed")
        self.assertEqual(transaction["errorCode"], "execution_unknown")
        self.assertEqual(transaction["requestingClientId"], "controller-after-commit")
        self.assertNotIn("executionEligibleAtMs", transaction)
        self.assertNotIn("watchdogDeadlineAtMs", transaction)
        self.assertFalse(
            any(
                message["action"] in {"player.pause", "playback.update"}
                for message in self.messages(player)
            )
        )
        self.assertFalse(
            any(message["action"] == "system.ack" for message in response)
        )
        watchdog.assert_not_called()

    def test_strict_control_emit_failure_settles_exact_transaction_unknown(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-emit-failure",
            device_session_id="device:controller-emit-failure",
        )
        real_emit = emo_ws._emit_message

        def fail_command(message, *args, **kwargs):
            if message.get("action") == "player.seek":
                raise RuntimeError("injected command emit failure")
            return real_emit(message, *args, **kwargs)

        with mock.patch.object(
            emo_ws,
            "_emit_message",
            side_effect=fail_command,
        ), mock.patch.object(emo_ws, "_start_control_watchdog") as watchdog:
            response = self.emit_strict(
                controller,
                "command",
                "player.seek",
                "seek-emit-failure",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                    "positionMs": 1000,
                },
            )

        self.assertTrue(
            any(message["action"] == "system.error" for message in response)
        )
        transaction = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(transaction["status"], "failed")
        self.assertEqual(transaction["errorCode"], "execution_unknown")
        self.assertEqual(transaction["requestingClientId"], "controller-emit-failure")
        self.assertNotIn("executionEligibleAtMs", transaction)
        self.assertNotIn("watchdogDeadlineAtMs", transaction)
        self.assertFalse(
            any(
                message["action"] == "player.seek"
                for message in self.messages(player)
            )
        )
        self.assertFalse(
            any(message["action"] == "system.ack" for message in response)
        )
        watchdog.assert_not_called()

    def test_strict_control_emit_failure_preserves_original_exception(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-emit-compensation-failure",
            device_session_id="device:controller-emit-compensation-failure",
        )

        class EmitFailure(Exception):
            pass

        class CompensationFailure(Exception):
            pass

        real_emit = emo_ws._emit_message

        def fail_command(message, *args, **kwargs):
            if message.get("action") == "player.seek":
                raise EmitFailure("injected command emit failure")
            return real_emit(message, *args, **kwargs)

        with mock.patch.object(
            emo_ws,
            "_emit_message",
            side_effect=fail_command,
        ), mock.patch.object(
            emo_ws,
            "_settle_strict_control_execution_unknown",
            side_effect=CompensationFailure("injected compensation failure"),
        ), self.assertLogs("supysonic.emo.ws", level="ERROR") as captured:
            response = self.emit_strict(
                controller,
                "command",
                "player.seek",
                "seek-emit-compensation-failure",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                    "positionMs": 1000,
                },
            )

        self.assertTrue(
            any(message["action"] == "system.error" for message in response)
        )
        self.assertEqual(response[0]["payload"]["code"], "internal_error")
        combined = "\n".join(captured.output)
        self.assertIn("exception_type=EmitFailure", combined)
        self.assertIn("Unable to persist emit failure execution_unknown", combined)
        self.assertNotIn("exception_type=CompensationFailure", combined)

    def test_emit_failure_terminal_replays_without_second_mutation(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-emit-replay",
            device_session_id="device:controller-emit-replay",
        )
        real_emit = emo_ws._emit_message

        def fail_command(message, *args, **kwargs):
            if message.get("action") == "player.seek":
                raise RuntimeError("injected command emit failure")
            return real_emit(message, *args, **kwargs)

        with mock.patch.object(
            emo_ws,
            "_emit_message",
            side_effect=fail_command,
        ):
            initial = self.emit_strict(
                controller,
                "command",
                "player.seek",
                "seek-emit-replay",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                    "positionMs": 1000,
                },
            )

        self.assertEqual(
            len(
                [
                    message
                    for message in initial
                    if message["action"] == "playback.control.settled"
                ]
            ),
            1,
        )
        terminal = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        context_before = getPlaybackContextState("context-1")
        self.messages(player)
        self.messages(controller)

        replay = emo_ws._settle_strict_control_execution_unknown(
            "context-1",
            terminal,
            context_before,
            "phone-1",
        )

        self.assertFalse(replay.mutated)
        self.assertEqual(replay.dependency_settlements, ())
        self.assertEqual(
            emo_ws.getPlaybackControlTransaction("context-1", 1, 2),
            terminal,
        )
        context_after = getPlaybackContextState("context-1")
        for cursor_name in ("epoch", "version", "queueRevision", "controlVersion"):
            self.assertEqual(
                context_after[cursor_name],
                context_before[cursor_name],
            )
        for client in (player, controller):
            settlements = [
                message
                for message in self.messages(client)
                if message["action"] == "playback.control.settled"
            ]
            self.assertEqual(len(settlements), 1)
            self.assertEqual(
                settlements[0]["payload"]["commandControlVersion"],
                2,
            )

    def test_strict_control_eligibility_failure_settles_without_retry(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-eligibility-failure",
            device_session_id="device:controller-eligibility-failure",
        )

        with mock.patch.object(
            emo_ws,
            "markPlaybackControlTransactionExecutionEligible",
            side_effect=RuntimeError("injected eligibility failure"),
        ), mock.patch.object(emo_ws, "_start_control_watchdog") as watchdog:
            response = self.emit_strict(
                controller,
                "command",
                "player.seek",
                "seek-eligibility-failure",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                    "positionMs": 1000,
                },
            )

        self.assertTrue(
            any(message["action"] == "system.error" for message in response)
        )
        transaction = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(transaction["status"], "failed")
        self.assertEqual(transaction["errorCode"], "execution_unknown")
        self.assertNotIn("executionEligibleAtMs", transaction)
        self.assertNotIn("watchdogDeadlineAtMs", transaction)
        authority_commands = [
            message
            for message in self.messages(player)
            if message["action"] == "player.seek"
        ]
        self.assertEqual(len(authority_commands), 1)
        self.assertFalse(
            any(message["action"] == "system.ack" for message in response)
        )
        watchdog.assert_not_called()

    def test_strict_control_rejects_invalid_requester_or_authority_epoch(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-invalid-epoch",
            device_session_id="device:controller-invalid-epoch",
        )
        state = get_state()
        real_get_generation = state.get_current_physical_generation

        for client_id, label in (
            ("controller-invalid-epoch", "requester"),
            ("phone-1", "authority"),
        ):
            for invalid_epoch in (None, 0, True, "1"):
                with self.subTest(
                    client_id=client_id,
                    label=label,
                    invalid_epoch=repr(invalid_epoch),
                ):
                    before = getPlaybackContextState("context-1")

                    def malformed_generation(
                        user_name,
                        requested_client_id,
                        device_session_id,
                        expected_sid=None,
                    ):
                        generation = real_get_generation(
                            user_name,
                            requested_client_id,
                            device_session_id,
                            expected_sid=expected_sid,
                        )
                        if (
                            generation is not None
                            and requested_client_id == client_id
                        ):
                            generation["connectionEpoch"] = invalid_epoch
                        return generation

                    with mock.patch.object(
                        state,
                        "get_current_physical_generation",
                        side_effect=malformed_generation,
                    ):
                        response = self.emit_strict(
                            controller,
                            "command",
                            "player.pause",
                            "pause-invalid-epoch-%s-%s"
                            % (label, repr(invalid_epoch)),
                            {
                                "playbackContextId": "context-1",
                                "baseControlVersion": 1,
                            },
                        )

                    self.assertEqual(response[0]["action"], "system.error")
                    self.assertEqual(getPlaybackContextState("context-1"), before)
                    self.assertIsNone(
                        emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
                    )
                    self.assertFalse(
                        any(
                            message["action"] == "player.pause"
                            for message in self.messages(player)
                        )
                    )

    def test_strict_control_rechecks_requester_controller_role_before_mutation(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-role-recheck",
            device_session_id="device:controller-role-recheck",
        )
        before = getPlaybackContextState("context-1")
        state = get_state()
        real_mutate = emo_ws.mutateStrictPlaybackContextControl

        def remove_requester_role_before_validator(*args, **kwargs):
            validator = kwargs["pre_mutation_validator"]

            def changed_role_validator(current):
                current_client = state.get_client(
                    "controller-role-recheck",
                    user_name="alice",
                )
                current_client["roles"] = ["player"]
                state.register_client(
                    state.get_sid_for_client(
                        "controller-role-recheck",
                        user_name="alice",
                    ),
                    "controller-role-recheck",
                    current_client,
                )
                return validator(current)

            kwargs["pre_mutation_validator"] = changed_role_validator
            return real_mutate(*args, **kwargs)

        with mock.patch.object(
            emo_ws,
            "mutateStrictPlaybackContextControl",
            side_effect=remove_requester_role_before_validator,
        ):
            response = self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-requester-role-recheck",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )

        self.assertEqual(response[0]["action"], "system.error")
        self.assertEqual(response[0]["payload"]["code"], "forbidden")
        self.assertEqual(getPlaybackContextState("context-1"), before)
        self.assertIsNone(
            emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        )

    def test_strict_control_rechecks_authority_role_before_mutation(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-authority-role",
            device_session_id="device:controller-authority-role",
        )
        before = getPlaybackContextState("context-1")
        state = get_state()
        real_mutate = emo_ws.mutateStrictPlaybackContextControl

        def remove_authority_role_before_validator(*args, **kwargs):
            validator = kwargs["pre_mutation_validator"]

            def changed_role_validator(current):
                authority_client = state.get_client("phone-1", user_name="alice")
                authority_client["roles"] = ["controller"]
                state.register_client(
                    state.get_sid_for_client("phone-1", user_name="alice"),
                    "phone-1",
                    authority_client,
                )
                return validator(current)

            kwargs["pre_mutation_validator"] = changed_role_validator
            return real_mutate(*args, **kwargs)

        with mock.patch.object(
            emo_ws,
            "mutateStrictPlaybackContextControl",
            side_effect=remove_authority_role_before_validator,
        ):
            response = self.emit_strict(
                controller,
                "command",
                "player.pause",
                "pause-authority-role-recheck",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                },
            )

        self.assertEqual(response[0]["action"], "system.error")
        self.assertEqual(response[0]["payload"]["code"], "forbidden")
        self.assertEqual(getPlaybackContextState("context-1"), before)
        self.assertIsNone(
            emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        )

    def test_strict_control_rechecks_authority_action_capability_before_mutation(self):
        player = self.ready_strict_client()
        self.create_context(player)
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-capability-recheck",
            device_session_id="device:controller-capability-recheck",
        )
        state = get_state()
        real_mutate = emo_ws.mutateStrictPlaybackContextControl
        required_capability = {"value": None}

        def remove_authority_capability_before_validator(*args, **kwargs):
            validator = kwargs["pre_mutation_validator"]

            def changed_capability_validator(current):
                authority_client = state.get_client("phone-1", user_name="alice")
                capabilities = dict(authority_client["capabilities"])
                capabilities[required_capability["value"]] = False
                authority_client["capabilities"] = capabilities
                state.register_client(
                    state.get_sid_for_client("phone-1", user_name="alice"),
                    "phone-1",
                    authority_client,
                )
                return validator(current)

            kwargs["pre_mutation_validator"] = changed_capability_validator
            return real_mutate(*args, **kwargs)

        with mock.patch.object(
            emo_ws,
            "mutateStrictPlaybackContextControl",
            side_effect=remove_authority_capability_before_validator,
        ):
            for action, capability, extra in (
                ("player.play", "canPlay", {}),
                ("player.pause", "canPause", {}),
                ("player.seek", "canSeek", {"positionMs": 3000}),
            ):
                with self.subTest(action=action):
                    required_capability["value"] = capability
                    response = self.emit_strict(
                        controller,
                        "command",
                        action,
                        "control-capability-recheck-%s" % action,
                        dict(
                            {
                                "playbackContextId": "context-1",
                                "baseControlVersion": 1,
                            },
                            **extra,
                        ),
                    )
                    self.assertEqual(response[0]["action"], "system.error")
                    self.assertEqual(
                        response[0]["payload"]["code"],
                        "capability_required",
                    )
                    self.assertIsNone(
                        emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
                    )
                    authority_client = state.get_client(
                        "phone-1",
                        user_name="alice",
                    )
                    capabilities = dict(authority_client["capabilities"])
                    capabilities[capability] = True
                    authority_client["capabilities"] = capabilities
                    state.register_client(
                        state.get_sid_for_client("phone-1", user_name="alice"),
                        "phone-1",
                        authority_client,
                    )

        self.assertEqual(
            getPlaybackContextState("context-1")["controlVersion"],
            1,
        )

    def test_strict_queue_play_item_persists_exact_generation_and_eligibility(self):
        player = self.ready_strict_client()
        self.create_context(player, queue_song_ids=["song-2", "song-1", "song-3"])
        controller = self.ready_strict_client(
            roles=["controller"],
            client_id="controller-play-item-exact",
            device_session_id="device:controller-play-item-exact",
        )
        watchdog_transactions = []

        with mock.patch.object(
            emo_ws,
            "_start_control_watchdog",
            side_effect=lambda transaction: watchdog_transactions.append(
                dict(transaction)
            ),
        ):
            response = self.emit_strict(
                controller,
                "command",
                "queue.playItem",
                "play-item-exact-generation",
                {
                    "playbackContextId": "context-1",
                    "baseControlVersion": 1,
                    "baseQueueRevision": 1,
                    "queueIndex": 1,
                },
            )

        self.assertEqual([message["action"] for message in response], ["system.ack"])
        transaction = emo_ws.getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(transaction["requestingClientId"], "controller-play-item-exact")
        self.assertEqual(
            transaction["requestingDeviceSessionId"],
            "device:controller-play-item-exact",
        )
        self.assertEqual(transaction["requestingConnectionEpoch"], 1)
        self.assertEqual(transaction["authorityClientId"], "phone-1")
        self.assertEqual(transaction["routedConnectionEpoch"], 1)
        self.assertEqual(transaction["acceptedTarget"]["queueRevision"], 2)
        self.assertEqual(getPlaybackContextState("context-1")["queueRevision"], 2)
        self.assertEqual(len(watchdog_transactions), 1)
        self.assertEqual(
            watchdog_transactions[0]["watchdogDeadlineAtMs"],
            transaction["executionEligibleAtMs"]
            + transaction["executionTimeoutMs"]
            + 2000,
        )

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

    def test_strict_output_accepts_handoff_commit_with_timing_fields(self):
        validate_strict_output(
            {
                "type": "command",
                "action": "player.play",
                "payload": {
                    "playbackContextId": "context-1",
                    "handoffId": "handoff-1",
                    "controlVersion": 2,
                    "sourceClientId": "phone-1",
                    "serverTimeMs": 1780000004500,
                    "effectiveAtServerMs": 1780000005000,
                    "positionMs": 1200,
                    "playbackRate": 1.0,
                },
                "timestamp": 1780000004.5,
                "connectionNonce": "nonce-1",
                "connectionEpoch": 1,
            },
            registered=True,
        )

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
            ("position", ["song-2", "song-3"], 0, 29639, None, (3, 3, 1)),
            ("track", ["song-4", "song-3"], 0, 29639, 1, (4, 4, 2)),
            ("index", ["song-4", "song-3"], 1, 0, 2, (5, 5, 3)),
            ("no-op", ["song-4", "song-3"], 1, 0, None, (6, 6, 3)),
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
                self.assertEqual(
                    [message["action"] for message in messages],
                    ["system.ack", "queue.context.sync"],
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
                        "authorityDeviceSessionId",
                        "queueSongIds",
                        "currentIndex",
                        "trackId",
                        "state",
                        "positionMs",
                        "positionSampledAtServerMs",
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
                self.assertEqual(
                    tuple(
                        push["payload"][field]
                        for field in ("version", "queueRevision", "controlVersion")
                    ),
                    expected,
                )
                if name in {"track", "index"}:
                    device = emo_ws.getDevicePlaybackState(
                        "context-1",
                        "phone-1",
                    )
                    self.assertEqual(
                        device["appliedControlVersion"],
                        expected[2],
                    )

        before = getPlaybackContextState("context-1")
        before_device = emo_ws.getDevicePlaybackState("context-1", "phone-1")
        error = self.emit_strict(
            client,
            "state",
            "queue.context.sync",
            "queue-missing-control",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "queueSongIds": ["song-4", "song-3"],
                "currentIndex": 0,
                "positionMs": 1,
                "baseQueueRevision": before["queueRevision"],
            },
        )[0]
        self.assertEqual(error["payload"]["code"], "bad_request")
        after = getPlaybackContextState("context-1")
        self.assertEqual(after, before)
        self.assertEqual(
            emo_ws.getDevicePlaybackState("context-1", "phone-1"),
            before_device,
        )

        stale_control = self.emit_strict(
            client,
            "state",
            "queue.context.sync",
            "queue-stale-control",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "queueSongIds": ["song-4", "song-3"],
                "currentIndex": 0,
                "positionMs": 1,
                "baseQueueRevision": before["queueRevision"],
                "baseControlVersion": before["controlVersion"] - 1,
            },
        )[0]
        self.assertEqual(stale_control["payload"]["code"], "stale_version")
        self.assertEqual(
            stale_control["payload"]["currentControlVersion"],
            before["controlVersion"],
        )
        self.assertEqual(getPlaybackContextState("context-1"), before)
        self.assertEqual(
            emo_ws.getDevicePlaybackState("context-1", "phone-1"),
            before_device,
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

    def test_cross_user_status_and_subscribe_are_concealed_as_not_found(self):
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
                        "not_found",
                    )
                    error_payloads.append(response[0]["payload"])
                self.assertEqual(error_payloads[0], error_payloads[1])

    def test_foreign_and_missing_context_mutations_are_identically_concealed(self):
        owner = self.ready_strict_client()
        self.create_context(owner)
        other = self.connect()
        self.authenticate(other, "bob", "B0b", "auth-bob-mutations")
        with self.enable_all_profiles():
            self.register(
                other,
                "register-bob-mutations",
                self.strict_registration_payload(
                    roles=["player", "controller"],
                    client_id="bob-phone",
                    device_session_id="device:bob-phone",
                ),
            )
        self.messages(other)

        context_before = getPlaybackContextState("context-1")
        device_before = emo_ws.getDevicePlaybackState(
            "context-1",
            "bob-phone",
        )
        pending_before = emo_store.listPendingPlaybackControlTransactions(
            "context-1",
            1,
        )
        cases = (
            (
                "command",
                "player.seek",
                {
                    "baseControlVersion": 1,
                    "positionMs": 2500,
                },
            ),
            (
                "command",
                "playback.context.prepare",
                {
                    "intentId": "concealed-prepare",
                    "baseControlVersion": 1,
                },
            ),
            (
                "event",
                "playback.context.prepared",
                {
                    "deviceSessionId": "device:bob-phone",
                    "intentId": "concealed-prepared",
                    "ready": True,
                },
            ),
            (
                "state",
                "queue.context.sync",
                {
                    "deviceSessionId": "device:bob-phone",
                    "queueSongIds": ["song-1"],
                    "currentIndex": 0,
                    "positionMs": 0,
                    "baseQueueRevision": 1,
                },
            ),
            (
                "event",
                "playback.update",
                {
                    "deviceSessionId": "device:bob-phone",
                    "origin": "passive",
                    "appliedControlVersion": 1,
                    "state": "playing",
                    "positionMs": 10,
                    "clientSeq": 1,
                    "trackId": "song-1",
                },
            ),
            (
                "command",
                "playback.context.close",
                {
                    "expectedEpoch": 1,
                    "baseVersion": 1,
                },
            ),
        )

        for message_type, action, action_payload in cases:
            with self.subTest(action=action):
                error_payloads = []
                for scope, playback_context_id in (
                    ("foreign", "context-1"),
                    ("missing", "missing-context"),
                ):
                    response = self.emit_strict(
                        other,
                        message_type,
                        action,
                        "concealed-%s-%s" % (action, scope),
                        dict(
                            action_payload,
                            playbackContextId=playback_context_id,
                        ),
                    )
                    self.assertEqual(
                        [message["action"] for message in response],
                        ["system.error"],
                    )
                    self.assertEqual(response[0]["payload"]["code"], "not_found")
                    error_payloads.append(response[0]["payload"])
                self.assertEqual(error_payloads[0], error_payloads[1])

        self.assertEqual(getPlaybackContextState("context-1"), context_before)
        self.assertEqual(
            emo_ws.getDevicePlaybackState("context-1", "bob-phone"),
            device_before,
        )
        self.assertEqual(
            emo_store.listPendingPlaybackControlTransactions(
                "context-1",
                1,
            ),
            pending_before,
        )
        self.assertIsNone(
            emo_ws.getPlaybackPrepareTransaction(
                "context-1",
                1,
                "concealed-prepare",
            )
        )
        self.assertIsNone(
            emo_ws.getPlaybackPrepareTransaction(
                "context-1",
                1,
                "concealed-prepared",
            )
        )
        self.assertEqual(self.messages(owner), [])

    def test_source_context_close_transitions_follow_to_cleanup_required(self):
        owner = self.ready_strict_client()
        self.create_context(owner)
        self.emit_strict(
            owner,
            "event",
            "playback.update",
            "follow-source-fact-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "stopped",
                "positionMs": 1200,
                "clientSeq": 1,
                "trackId": "song-2",
            },
        )
        follower = self.ready_strict_client(
            client_id="follower-1",
            device_session_id="device:follower-1",
        )
        with mock.patch(
            "supysonic.emo.ws_store._new_playback_context_id",
            return_value="context-follower-1",
        ):
            self.emit_strict(
                follower,
                "command",
                "playback.context.ensure",
                "follow-suspended-context-1",
                {
                    "deviceSessionId": "device:follower-1",
                    "queueSongIds": ["song-follower"],
                    "currentIndex": 0,
                    "positionMs": 0,
                    "state": "stopped",
                },
            )
        self.emit_strict(
            follower,
            "event",
            "playback.update",
            "follow-suspended-fact-1",
            {
                "playbackContextId": "context-follower-1",
                "deviceSessionId": "device:follower-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "stopped",
                "positionMs": 0,
                "clientSeq": 1,
                "trackId": "song-follower",
            },
        )
        follow_messages = self.emit_strict(
            follower,
            "command",
            "follow.start",
            "follow-start-1",
            {
                "sourcePlaybackContextId": "context-1",
                "deviceSessionId": "device:follower-1",
            },
        )
        self.assertEqual(
            [message["action"] for message in follow_messages],
            ["system.ack"],
        )
        relationship = get_state().get_follow_relationship("follower-1")
        self.assertEqual(relationship["sourcePlaybackContextId"], "context-1")
        self.messages(owner)

        close_messages = self.emit_strict(
            owner,
            "command",
            "playback.context.close",
            "context-close-followers",
            {
                "playbackContextId": "context-1",
                "expectedEpoch": 1,
                "baseVersion": 1,
            },
        )
        follower_messages = self.messages(follower)
        self.assertEqual(
            [message["action"] for message in close_messages],
            ["system.ack", "playback.context.closed"],
        )
        self.assertTrue(
            any(
                message["action"] == "playback.context.closed"
                for message in follower_messages
            )
        )
        self.assertIsNone(get_state().get_follow_relationship("follower-1"))
        lease = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        self.assertEqual(lease["phase"], "cleanupRequired")
        context = getPlaybackContextState("context-1")
        self.assertEqual(context["lifecycle"], "closed")
        self.assertEqual(context["version"], 2)

    def test_source_context_close_rolls_back_when_follow_cleanup_fails(self):
        owner = self.ready_strict_client()
        self.create_context(owner)
        self.emit_strict(
            owner,
            "event",
            "playback.update",
            "follow-source-fact-close-rollback",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:phone-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "stopped",
                "positionMs": 1200,
                "clientSeq": 1,
                "trackId": "song-2",
            },
        )
        follower = self.ready_strict_client(
            client_id="follower-1",
            device_session_id="device:follower-1",
        )
        with mock.patch(
            "supysonic.emo.ws_store._new_playback_context_id",
            return_value="context-follower-1",
        ):
            self.emit_strict(
                follower,
                "command",
                "playback.context.ensure",
                "follow-suspended-context-close-rollback",
                {
                    "deviceSessionId": "device:follower-1",
                    "queueSongIds": ["song-follower"],
                    "currentIndex": 0,
                    "positionMs": 0,
                    "state": "stopped",
                },
            )
        self.emit_strict(
            follower,
            "event",
            "playback.update",
            "follow-suspended-fact-close-rollback",
            {
                "playbackContextId": "context-follower-1",
                "deviceSessionId": "device:follower-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "stopped",
                "positionMs": 0,
                "clientSeq": 1,
                "trackId": "song-follower",
            },
        )
        self.emit_strict(
            follower,
            "command",
            "follow.start",
            "follow-start-close-rollback",
            {
                "sourcePlaybackContextId": "context-1",
                "deviceSessionId": "device:follower-1",
            },
        )
        self.messages(owner)
        self.messages(follower)

        with mock.patch.object(
            emo_ws,
            "markFollowSourceContextsClosedInTransaction",
            side_effect=RuntimeError("injected Follow close cleanup failure"),
        ):
            close_messages = self.emit_strict(
                owner,
                "command",
                "playback.context.close",
                "context-close-follow-cleanup-failure",
                {
                    "playbackContextId": "context-1",
                    "expectedEpoch": 1,
                    "baseVersion": 1,
                },
            )

        self.assertEqual(
            [message["action"] for message in close_messages],
            ["system.error"],
        )
        self.assertEqual(
            close_messages[0]["payload"]["code"],
            "internal_error",
        )
        context = getPlaybackContextState("context-1")
        self.assertEqual(context["lifecycle"], "active")
        self.assertEqual(context["version"], 1)
        lease = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        self.assertEqual(lease["phase"], "active")
        self.assertTrue(
            get_state().get_follow_relationship("follower-1")["active"]
        )
        self.assertFalse(
            any(
                message["action"] == "playback.context.closed"
                for message in self.messages(follower)
            )
        )

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
