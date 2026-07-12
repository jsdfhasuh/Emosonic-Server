import os
import shutil
import tempfile
import unittest
from unittest import mock

from supysonic.db import release_database
from supysonic.emo import ws as emo_ws
from supysonic.emo.ws import socketio, strict_request_cache
from supysonic.emo.ws_state import get_state
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
    ):
        return {
            "clientId": client_id,
            "deviceSessionId": device_session_id,
            "deviceName": "Phone",
            "roles": roles or ["player"],
            "capabilities": {
                "playbackContextV2": True,
                "playbackPrepare": True,
                "effectiveAtPlayback": True,
                "canPlay": True,
                "canPause": True,
                "canSeek": True,
                "canSetVolume": True,
                "supportsFollow": True,
                "supportsBroadcast": True,
            },
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
            "record_device_playback_state",
            wraps=state.record_device_playback_state,
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


if __name__ == "__main__":
    unittest.main()
