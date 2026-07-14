import json
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supysonic.db import release_database
from supysonic.emo.browser_auth import (
    BrowserOneTimePasswordCapacityExceeded,
    BrowserOneTimePasswordRateLimited,
    BrowserOneTimePasswordStore,
    browser_one_time_passwords,
)
from supysonic.emo.ws import socketio
from supysonic.emo.ws_state import get_state
from supysonic.emo.ws_store import (
    closeStrictPlaybackContextState,
    createStrictPlaybackContextState,
    getPlaybackContextState,
)
from supysonic.managers.user import UserManager
from supysonic.web import create_application

from tests.testbase import TestConfig


ROOT = Path(__file__).resolve().parents[2]
WEB_FIXTURES = json.loads(
    (ROOT / "tests" / "fixtures" / "emo_web_strict_v2" / "requests.json").read_text(
        encoding="utf-8"
    )
)


class EmoWebStrictV2TestCase(unittest.TestCase):
    def setUp(self):
        self.database = tempfile.mkstemp()
        self.cache_directory = tempfile.mkdtemp()
        self.config = TestConfig(True, False)
        self.config.BASE["database_uri"] = "sqlite:///" + self.database[1]
        self.config.WEBAPP.update(
            {
                "cache_dir": self.cache_directory,
                "mount_emosonic": True,
                "emo_strict_v2_core_enabled": True,
                "emo_strict_v2_follow_enabled": True,
                "emo_strict_v2_handoff_enabled": True,
                "emo_strict_v2_broadcast_enabled": True,
            }
        )
        self.readiness = mock.patch(
            "supysonic.emo.strict_v2_readiness.get_code_conformance_readiness",
            return_value={
                "core": True,
                "follow": True,
                "handoff": True,
                "broadcast": True,
            },
        )
        self.readiness.start()
        self.app = create_application(self.config)
        self.http = self.app.test_client()
        UserManager.add("alice", "Alic3", admin=True)
        UserManager.add("bob", "B0b")
        browser_one_time_passwords.clear()
        state = get_state()
        state._sessions.clear()
        state._client_to_sid.clear()
        state._clients.clear()
        state._playback_contexts.clear()
        state._device_playback_states.clear()
        state._playback_context_subscriptions.clear()
        self.socket_clients = []

    def tearDown(self):
        for client in self.socket_clients:
            if client.is_connected(namespace="/emo"):
                client.disconnect(namespace="/emo")
        browser_one_time_passwords.clear()
        release_database()
        shutil.rmtree(self.cache_directory)
        os.close(self.database[0])
        os.remove(self.database[1])
        self.readiness.stop()

    def login(self, client=None, user_name="alice", password="Alic3"):
        target = client or self.http
        return target.post(
            "/user/login",
            data={"user": user_name, "password": password},
            follow_redirects=True,
        )

    def initialize_browser_session(self, client=None):
        target = client or self.http
        target.get("/player")
        with target.session_transaction() as browser_session:
            return (
                browser_session["emo_browser_csrf_token"],
                browser_session["emo_browser_session_id"],
            )

    def issue_credential(self, client=None, origin="http://localhost"):
        target = client or self.http
        csrf_token, _browser_session_id = self.initialize_browser_session(target)
        return target.post(
            "/emo/browser-auth-password",
            headers={
                "Origin": origin,
                "X-Emo-CSRF-Token": csrf_token,
            },
        )

    def connect(self, flask_client=None):
        client = socketio.test_client(
            self.app,
            namespace="/emo",
            flask_test_client=flask_client or self.http,
        )
        self.socket_clients.append(client)
        return client

    @staticmethod
    def messages(client):
        messages = []
        for event in client.get_received("/emo"):
            if event["name"] != "message":
                continue
            args = event.get("args")
            messages.append(args[0] if isinstance(args, list) else args)
        return messages

    def authenticate_with(self, client, user_name, password, request_id):
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

    def fixture_message(self, action, request_id=None):
        matches = [
            item["message"]
            for item in WEB_FIXTURES["requests"]
            if item["message"]["action"] == action
            and (request_id is None or item["message"]["requestId"] == request_id)
        ]
        self.assertEqual(len(matches), 1)
        return json.loads(json.dumps(matches[0]))

    def register_web_player(
        self,
        client_id,
        device_session_id,
        *,
        supports_follow=False,
        supports_broadcast=False,
        supports_handoff=False,
    ):
        password = self.issue_credential().json["oneTimePassword"]
        client = self.connect()
        self.authenticate_with(
            client,
            "alice",
            password,
            "auth-%s" % client_id,
        )
        request_message = self.fixture_message(
            "device.register", "web-player-register-1"
        )
        request_message["requestId"] = "register-%s" % client_id
        request_message["payload"]["clientId"] = client_id
        request_message["payload"]["deviceSessionId"] = device_session_id
        capabilities = request_message["payload"]["capabilities"]
        capabilities["supportsFollow"] = supports_follow
        capabilities["supportsBroadcast"] = supports_broadcast
        capabilities["playbackPrepare"] = supports_handoff
        capabilities["effectiveAtPlayback"] = supports_handoff
        client.emit("message", request_message, namespace="/emo")
        messages = self.messages(client)
        ack = next(
            message
            for message in messages
            if message.get("requestId") == request_message["requestId"]
        )
        self.assertEqual(ack["action"], "system.ack")
        return client

    def register_web_control(self, *, supports_broadcast=False):
        password = self.issue_credential().json["oneTimePassword"]
        client = self.connect()
        self.authenticate_with(client, "alice", password, "auth-web-control")
        request_message = self.fixture_message(
            "device.register", "web-control-register-1"
        )
        request_message["payload"]["capabilities"][
            "supportsBroadcast"
        ] = supports_broadcast
        client.emit("message", request_message, namespace="/emo")
        messages = self.messages(client)
        ack = next(
            message
            for message in messages
            if message.get("requestId") == request_message["requestId"]
        )
        self.assertEqual(ack["action"], "system.ack")
        return client

    def create_web_context(self, player, context_id="ctx-1", device_session_id="web-player-device:1"):
        request_message = self.fixture_message("playback.context.create")
        request_message["requestId"] = "create-%s" % context_id
        request_message["payload"]["playbackContextId"] = context_id
        request_message["payload"]["deviceSessionId"] = device_session_id
        player.emit("message", request_message, namespace="/emo")
        response = next(
            message
            for message in self.messages(player)
            if message.get("requestId") == request_message["requestId"]
        )
        return response["payload"]

    @staticmethod
    def assert_no_session_fields(value):
        if isinstance(value, dict):
            if "sessionId" in value or "sourceSessionId" in value:
                raise AssertionError("strict web message contains a legacy session field")
            for item in value.values():
                EmoWebStrictV2TestCase.assert_no_session_fields(item)
        elif isinstance(value, list):
            for item in value:
                EmoWebStrictV2TestCase.assert_no_session_fields(item)

    def test_browser_password_requires_login_csrf_and_same_origin(self):
        anonymous = self.http.post("/emo/browser-auth-password")
        self.assertEqual(anonymous.status_code, 302)

        self.login()
        self.initialize_browser_session()
        missing_csrf = self.http.post("/emo/browser-auth-password")
        self.assertEqual(missing_csrf.status_code, 403)

        with self.http.session_transaction() as browser_session:
            csrf_token = browser_session["emo_browser_csrf_token"]
        cross_origin = self.http.post(
            "/emo/browser-auth-password",
            headers={
                "Origin": "https://attacker.example",
                "X-Emo-CSRF-Token": csrf_token,
            },
        )
        self.assertEqual(cross_origin.status_code, 403)

    def test_browser_password_accepts_forwarded_https_same_host(self):
        proxy_http = self.app.test_client()
        proxy_http.post(
            "/user/login",
            base_url="http://music.example",
            data={"user": "alice", "password": "Alic3"},
            follow_redirects=True,
        )
        proxy_http.get("/player", base_url="http://music.example")
        with proxy_http.session_transaction(
            base_url="http://music.example"
        ) as browser_session:
            csrf_token = browser_session["emo_browser_csrf_token"]

        response = proxy_http.post(
            "/emo/browser-auth-password",
            base_url="http://music.example",
            headers={
                "Origin": "https://music.example",
                "X-Forwarded-Proto": "https",
                "X-Emo-CSRF-Token": csrf_token,
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_browser_password_is_no_store_and_authenticates_exact_u_p_shape(self):
        self.login()
        response = self.issue_credential()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Pragma"], "no-cache")
        self.assertEqual(response.json["userName"], "alice")
        self.assertTrue(response.json["oneTimePassword"].startswith("browser-otp:"))

        client = self.connect()
        messages = self.authenticate_with(
            client,
            "alice",
            response.json["oneTimePassword"],
            "browser-auth-1",
        )
        ack = next(message for message in messages if message["requestId"] == "browser-auth-1")
        self.assertEqual(ack["action"], "system.ack")
        self.assertEqual(ack["payload"]["action"], "auth.login")
        self.assertEqual(ack["payload"]["userName"], "alice")

    def test_real_password_with_browser_otp_prefix_still_authenticates(self):
        UserManager.add("prefixed", "browser-otp:real-password")
        fresh_http = self.app.test_client()
        client = self.connect(fresh_http)
        messages = self.authenticate_with(
            client,
            "prefixed",
            "browser-otp:real-password",
            "auth-prefixed-password",
        )
        response = next(
            message
            for message in messages
            if message.get("requestId") == "auth-prefixed-password"
        )
        self.assertEqual(response["action"], "system.ack")
        self.assertEqual(response["payload"]["userName"], "prefixed")

    def test_browser_password_replay_cross_session_and_expiry_are_rejected(self):
        self.login()
        credential = self.issue_credential().json["oneTimePassword"]
        first = self.connect()
        self.authenticate_with(first, "alice", credential, "auth-first")

        replay = self.connect()
        replay_messages = self.authenticate_with(replay, "alice", credential, "auth-replay")
        replay_error = next(message for message in replay_messages if message["requestId"] == "auth-replay")
        self.assertEqual(replay_error["payload"]["code"], "unauthorized")

        other_http = self.app.test_client()
        self.login(other_http)
        self.initialize_browser_session(other_http)
        credential = self.issue_credential().json["oneTimePassword"]
        cross_session = self.connect(other_http)
        cross_messages = self.authenticate_with(
            cross_session,
            "alice",
            credential,
            "auth-cross-session",
        )
        cross_error = next(
            message for message in cross_messages if message["requestId"] == "auth-cross-session"
        )
        self.assertEqual(cross_error["payload"]["code"], "unauthorized")

        store = BrowserOneTimePasswordStore()
        password, _expires_at_ms = store.issue("alice", "browser-1", ttl_seconds=1, now_ms=1000)
        self.assertFalse(store.consume("alice", "browser-1", password, now_ms=2001))

    def test_browser_password_store_allows_small_per_session_window_and_enforces_limits(self):
        store = BrowserOneTimePasswordStore()
        first, _expires_at_ms = store.issue(
            "alice",
            "browser-1",
            now_ms=1000,
            max_issues_per_minute=2,
            global_capacity=2,
        )
        second, _expires_at_ms = store.issue(
            "alice",
            "browser-1",
            now_ms=2000,
            max_issues_per_minute=2,
            global_capacity=2,
        )
        self.assertTrue(store.consume("alice", "browser-1", first, now_ms=2000))
        self.assertTrue(store.consume("alice", "browser-1", second, now_ms=2000))

        with self.assertRaises(BrowserOneTimePasswordRateLimited) as limited:
            store.issue(
                "alice",
                "browser-1",
                now_ms=3000,
                max_issues_per_minute=2,
                global_capacity=2,
            )
        self.assertEqual(limited.exception.retry_after_ms, 58000)

        capacity_store = BrowserOneTimePasswordStore()
        capacity_store.issue("alice", "browser-1", now_ms=1000, global_capacity=2)
        capacity_store.issue("alice", "browser-2", now_ms=1000, global_capacity=2)
        with self.assertRaises(BrowserOneTimePasswordCapacityExceeded):
            capacity_store.issue("alice", "browser-3", now_ms=1000, global_capacity=2)

        rate_capacity_store = BrowserOneTimePasswordStore()
        for browser_session_id in ("browser-1", "browser-2"):
            password, _expires_at_ms = rate_capacity_store.issue(
                "alice",
                browser_session_id,
                now_ms=1000,
                global_capacity=2,
            )
            self.assertTrue(
                rate_capacity_store.consume(
                    "alice",
                    browser_session_id,
                    password,
                    now_ms=1000,
                )
            )
        with self.assertRaises(BrowserOneTimePasswordCapacityExceeded):
            rate_capacity_store.issue(
                "alice",
                "browser-3",
                now_ms=1000,
                global_capacity=2,
            )

        eviction_store = BrowserOneTimePasswordStore()
        oldest, _expires_at_ms = eviction_store.issue(
            "alice",
            "browser-1",
            now_ms=1000,
            max_issues_per_minute=3,
            outstanding_per_session=2,
            global_capacity=2,
        )
        middle, _expires_at_ms = eviction_store.issue(
            "alice",
            "browser-1",
            now_ms=2000,
            max_issues_per_minute=3,
            outstanding_per_session=2,
            global_capacity=2,
        )
        newest, _expires_at_ms = eviction_store.issue(
            "alice",
            "browser-1",
            now_ms=3000,
            max_issues_per_minute=3,
            outstanding_per_session=2,
            global_capacity=2,
        )
        self.assertFalse(eviction_store.consume("alice", "browser-1", oldest, now_ms=3000))
        self.assertTrue(eviction_store.consume("alice", "browser-1", middle, now_ms=3000))
        self.assertTrue(eviction_store.consume("alice", "browser-1", newest, now_ms=3000))

    def test_same_browser_session_can_authenticate_player_and_control_otps(self):
        self.login()
        player_password = self.issue_credential().json["oneTimePassword"]
        control_password = self.issue_credential().json["oneTimePassword"]

        player = self.connect()
        control = self.connect()
        player_messages = self.authenticate_with(
            player,
            "alice",
            player_password,
            "auth-player-tab",
        )
        control_messages = self.authenticate_with(
            control,
            "alice",
            control_password,
            "auth-control-tab",
        )

        for messages, request_id in (
            (player_messages, "auth-player-tab"),
            (control_messages, "auth-control-tab"),
        ):
            response = next(
                message for message in messages if message["requestId"] == request_id
            )
            self.assertEqual(response["action"], "system.ack")
            self.assertEqual(response["payload"]["userName"], "alice")

    def test_browser_password_endpoint_rate_limit_is_no_store(self):
        self.login()
        self.app.config["WEBAPP"][
            "emo_browser_otp_issues_per_session_per_minute"
        ] = 2
        self.assertEqual(self.issue_credential().status_code, 200)
        self.assertEqual(self.issue_credential().status_code, 200)

        limited = self.issue_credential()

        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json, {"error": "rate_limited"})
        self.assertEqual(limited.headers["Cache-Control"], "no-store")
        self.assertGreaterEqual(int(limited.headers["Retry-After"]), 1)

    def test_browser_password_and_real_password_are_not_logged(self):
        self.login()
        password = self.issue_credential().json["oneTimePassword"]
        client = self.connect()
        with self.assertLogs("supysonic.emo.ws", level=logging.WARNING) as captured:
            self.authenticate_with(client, "bob", password, "auth-wrong-user")
        combined = "\n".join(captured.output)
        self.assertNotIn(password, combined)
        self.assertNotIn("Alic3", combined)

    def test_context_bindings_are_minimal_user_scoped_active_and_no_store(self):
        createStrictPlaybackContextState(
            "ctx-alice",
            "alice",
            "web-player-alice",
            "web-player-device:alice",
            ["track-1"],
            0,
            0,
            "paused",
        )
        createStrictPlaybackContextState(
            "ctx-bob",
            "bob",
            "web-player-bob",
            "web-player-device:bob",
            ["track-2"],
            0,
            0,
            "paused",
        )
        self.login()
        response = self.http.get("/emo/web-context-bindings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            response.json,
            {
                "bindings": [
                    {
                        "clientId": "web-player-alice",
                        "deviceSessionId": "web-player-device:alice",
                        "playbackContextId": "ctx-alice",
                    }
                ]
            },
        )
        closeStrictPlaybackContextState("ctx-alice", "alice")
        self.assertEqual(self.http.get("/emo/web-context-bindings").json, {"bindings": []})

    def test_acceptance_state_is_default_off_and_reports_non_secret_liveness(self):
        createStrictPlaybackContextState(
            "ctx-alice",
            "alice",
            "web-player-alice",
            "web-player-device:alice",
            ["track-1"],
            0,
            0,
            "paused",
        )
        self.login()
        self.assertEqual(
            self.http.get("/emo/web-strict-v2-acceptance-state").status_code,
            404,
        )

        self.app.config["WEBAPP"]["emo_web_strict_v2_acceptance_mode"] = True
        response = self.http.get("/emo/web-strict-v2-acceptance-state")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            response.json,
            {
                "contexts": [
                    {
                        "playbackContextId": "ctx-alice",
                        "authorityClientId": "web-player-alice",
                        "authorityDeviceSessionId": "web-player-device:alice",
                        "authorityClientPresent": False,
                        "authoritySidPresent": False,
                    }
                ]
            },
        )

    def test_exact_web_player_and_control_payloads_complete_core_round_trip(self):
        self.login()

        player_password = self.issue_credential().json["oneTimePassword"]
        player = self.connect()
        self.authenticate_with(player, "alice", player_password, "web-auth-player")
        player_register = self.fixture_message(
            "device.register", "web-player-register-1"
        )
        player.emit("message", player_register, namespace="/emo")
        player_registration_messages = self.messages(player)
        player_ack = next(
            message
            for message in player_registration_messages
            if message.get("requestId") == "web-player-register-1"
        )
        self.assertEqual(
            player_ack["payload"]["negotiatedCapabilities"],
            player_register["payload"]["capabilities"],
        )

        create_request = self.fixture_message("playback.context.create")
        player.emit("message", create_request, namespace="/emo")
        create_messages = self.messages(player)
        created = next(
            message
            for message in create_messages
            if message.get("requestId") == create_request["requestId"]
        )
        self.assertEqual(created["action"], "playback.context.create")

        control_password = self.issue_credential().json["oneTimePassword"]
        control = self.connect()
        self.authenticate_with(control, "alice", control_password, "web-auth-control")
        control_register = self.fixture_message(
            "device.register", "web-control-register-1"
        )
        control.emit("message", control_register, namespace="/emo")
        control_registration_messages = self.messages(control)
        control_ack = next(
            message
            for message in control_registration_messages
            if message.get("requestId") == "web-control-register-1"
        )
        self.assertEqual(
            control_ack["payload"]["negotiatedCapabilities"],
            control_register["payload"]["capabilities"],
        )

        list_request = self.fixture_message("device.list")
        control.emit("message", list_request, namespace="/emo")
        list_messages = self.messages(control)
        device_list = next(
            message
            for message in list_messages
            if message.get("requestId") == list_request["requestId"]
        )
        self.assertEqual(device_list["action"], "device.list")
        self.assertEqual(
            [device["clientId"] for device in device_list["payload"]["devices"]],
            ["web-control-1", "web-player-1"],
        )
        self.assertTrue(
            all("playbackContextId" not in device for device in device_list["payload"]["devices"])
        )

        bindings = self.http.get("/emo/web-context-bindings").json["bindings"]
        self.assertEqual(
            bindings,
            [
                {
                    "clientId": "web-player-1",
                    "deviceSessionId": "web-player-device:1",
                    "playbackContextId": "ctx-1",
                }
            ],
        )

        subscribe_request = self.fixture_message("playback.context.subscribe")
        control.emit("message", subscribe_request, namespace="/emo")
        self.messages(control)
        status_request = self.fixture_message("playback.context.status")
        control.emit("message", status_request, namespace="/emo")
        status_messages = self.messages(control)
        status = next(
            message
            for message in status_messages
            if message.get("requestId") == status_request["requestId"]
        )
        context = status["payload"]["playbackContext"]

        play_request = {
            "type": "command",
            "action": "player.play",
            "requestId": "web-control-play-1",
            "payload": {
                "playbackContextId": "ctx-1",
                "baseControlVersion": context["controlVersion"],
                "positionMs": 3000,
            },
        }
        control.emit("message", play_request, namespace="/emo")
        control_messages = self.messages(control)
        player_messages = self.messages(player)
        self.assertTrue(
            any(
                message["action"] == "system.ack"
                and message.get("requestId") == "web-control-play-1"
                for message in control_messages
            )
        )
        forwarded = next(
            message for message in player_messages if message["action"] == "player.play"
        )
        self.assertEqual(
            forwarded["payload"],
            {
                "playbackContextId": "ctx-1",
                "controlVersion": context["controlVersion"] + 1,
                "sourceClientId": "web-control-1",
                "positionMs": 3000,
            },
        )

        feedback_request = self.fixture_message("playback.update")
        feedback_request["payload"]["positionMs"] = 3000
        feedback_request["payload"]["trackId"] = "track-1"
        player.emit("message", feedback_request, namespace="/emo")
        player_feedback = self.messages(player)
        control_feedback = self.messages(control)
        self.assertTrue(any(message["action"] == "playback.update" for message in player_feedback))
        self.assertTrue(any(message["action"] == "playback.update" for message in control_feedback))

        for message in (
            player_registration_messages
            + create_messages
            + control_registration_messages
            + list_messages
            + status_messages
            + control_messages
            + player_messages
            + player_feedback
            + control_feedback
        ):
            self.assert_no_session_fields(message)

    def test_exact_web_broadcast_uses_two_players_and_participant_feedback(self):
        self.login()
        player_one = self.register_web_player(
            "web-player-1",
            "web-player-device:1",
            supports_broadcast=True,
        )
        context = self.create_web_context(player_one)
        player_two = self.register_web_player(
            "web-player-2",
            "web-player-device:2",
            supports_broadcast=True,
        )
        control = self.register_web_control(supports_broadcast=True)
        self.messages(player_one)
        self.messages(player_two)

        start = self.fixture_message("broadcast.start")
        start["payload"]["autoPlay"] = False
        control.emit("message", start, namespace="/emo")
        control_messages = self.messages(control)
        start_ack = next(
            message
            for message in control_messages
            if message.get("requestId") == start["requestId"]
        )
        broadcast_id = start_ack["payload"]["broadcastId"]
        self.assertEqual(
            start_ack["payload"]["participants"],
            ["web-player-1", "web-player-2"],
        )
        first_start = next(
            message
            for message in self.messages(player_one)
            if message["action"] == "broadcast.start"
        )
        second_start = next(
            message
            for message in self.messages(player_two)
            if message["action"] == "broadcast.start"
        )
        self.assertEqual(first_start["payload"], second_start["payload"])
        self.assertEqual(first_start["payload"]["broadcastId"], broadcast_id)

        feedback = {
            "type": "event",
            "action": "playback.update",
            "requestId": "broadcast-feedback-player-2",
            "payload": {
                "playbackContextId": "ctx-1",
                "deviceSessionId": "web-player-device:2",
                "state": "paused",
                "positionMs": 12000,
                "clientSeq": 1,
                "trackId": context["trackId"],
                "volume": 70,
                "muted": False,
            },
        }
        player_two.emit("message", feedback, namespace="/emo")
        feedback_messages = self.messages(player_two)
        self.assertTrue(
            any(message["action"] == "playback.update" for message in feedback_messages)
        )

        status = self.fixture_message("broadcast.status")
        status["payload"]["broadcastId"] = broadcast_id
        control.emit("message", status, namespace="/emo")
        status_ack = next(
            message
            for message in self.messages(control)
            if message.get("requestId") == status["requestId"]
        )
        participant_state = next(
            item
            for item in status_ack["payload"]["participantStates"]
            if item["clientId"] == "web-player-2"
        )
        self.assertEqual(participant_state["clientSeq"], 1)

        play = self.fixture_message("broadcast.play")
        play["payload"]["broadcastId"] = broadcast_id
        control.emit("message", play, namespace="/emo")
        self.messages(control)
        self.assertTrue(
            any(message["action"] == "broadcast.play" for message in self.messages(player_one))
        )
        self.assertTrue(
            any(message["action"] == "broadcast.play" for message in self.messages(player_two))
        )

        stop = self.fixture_message("broadcast.stop")
        stop["payload"]["broadcastId"] = broadcast_id
        control.emit("message", stop, namespace="/emo")
        self.messages(control)
        stop_messages = self.messages(player_one) + self.messages(player_two)
        self.assertEqual(
            sum(message["action"] == "broadcast.stop" for message in stop_messages),
            2,
        )
        for message in control_messages + feedback_messages + stop_messages:
            self.assert_no_session_fields(message)

    def test_exact_web_follow_is_started_by_follower_player(self):
        self.login()
        source = self.register_web_player(
            "web-player-source", "web-player-device:source"
        )
        self.create_web_context(
            source,
            context_id="ctx-source",
            device_session_id="web-player-device:source",
        )
        follower = self.register_web_player(
            "web-player-follower",
            "web-player-device:follower",
            supports_follow=True,
        )
        self.messages(source)

        start = self.fixture_message("follow.start")
        start["payload"] = {
            "sourcePlaybackContextId": "ctx-source",
            "deviceSessionId": "web-player-device:follower",
        }
        follower.emit("message", start, namespace="/emo")
        start_messages = self.messages(follower)
        start_ack = next(
            message
            for message in start_messages
            if message.get("requestId") == start["requestId"]
        )
        self.assertEqual(start_ack["payload"], {"action": "follow.start"})
        relationship = get_state().get_follow_relationship("web-player-follower")
        self.assertEqual(relationship["sourcePlaybackContextId"], "ctx-source")

        subscribe = self.fixture_message("playback.context.subscribe")
        subscribe["payload"]["playbackContextId"] = "ctx-source"
        follower.emit("message", subscribe, namespace="/emo")
        self.messages(follower)
        status = self.fixture_message("playback.context.status")
        status["payload"]["playbackContextId"] = "ctx-source"
        follower.emit("message", status, namespace="/emo")
        status_response = next(
            message
            for message in self.messages(follower)
            if message.get("requestId") == status["requestId"]
        )
        self.assertEqual(
            status_response["payload"]["playbackContext"]["playbackContextId"],
            "ctx-source",
        )

        stop = self.fixture_message("follow.stop")
        stop["payload"]["sourcePlaybackContextId"] = "ctx-source"
        follower.emit("message", stop, namespace="/emo")
        stop_ack = next(
            message
            for message in self.messages(follower)
            if message.get("requestId") == stop["requestId"]
        )
        self.assertEqual(stop_ack["payload"], {"action": "follow.stop"})
        self.assertIsNone(get_state().get_follow_relationship("web-player-follower"))
        for message in start_messages + [status_response, stop_ack]:
            self.assert_no_session_fields(message)

    def test_exact_web_handoff_completes_controller_and_two_player_flow(self):
        self.login()
        source = self.register_web_player(
            "web-player-source", "web-player-device:source"
        )
        context = self.create_web_context(
            source,
            context_id="ctx-handoff",
            device_session_id="web-player-device:source",
        )
        target = self.register_web_player(
            "web-player-target",
            "web-player-device:target",
            supports_handoff=True,
        )
        control = self.register_web_control()
        self.messages(source)
        self.messages(target)

        start = self.fixture_message("playback.handoff.start")
        start["payload"] = {
            "playbackContextId": "ctx-handoff",
            "targetClientId": "web-player-target",
            "baseControlVersion": context["controlVersion"],
        }
        control.emit("message", start, namespace="/emo")
        control_start_messages = self.messages(control)
        start_ack = next(
            message
            for message in control_start_messages
            if message.get("requestId") == start["requestId"]
        )
        handoff_id = start_ack["payload"]["handoffId"]
        prepare_id = start_ack["payload"]["prepareId"]
        prepare = next(
            message
            for message in self.messages(target)
            if message["action"] == "playback.prepare"
        )
        self.assertEqual(prepare["payload"]["deviceSessionId"], "web-player-device:target")

        ready = self.fixture_message("playback.ready")
        ready["payload"] = {
            "playbackContextId": "ctx-handoff",
            "prepareId": prepare_id,
            "handoffId": handoff_id,
            "ready": True,
        }
        target.emit("message", ready, namespace="/emo")
        target_ready_messages = self.messages(target)
        commit = next(
            message
            for message in target_ready_messages
            if message["action"] == "player.play"
            and message["payload"].get("handoffId") == handoff_id
        )
        self.assertEqual(commit["payload"]["sourceClientId"], "web-player-source")
        self.assertGreater(commit["payload"]["effectiveAtServerMs"], 0)

        complete = self.fixture_message("playback.handoff.complete")
        complete["payload"] = {
            "playbackContextId": "ctx-handoff",
            "handoffId": handoff_id,
            "positionMs": commit["payload"]["positionMs"],
        }
        target.emit("message", complete, namespace="/emo")
        target_complete_messages = self.messages(target)
        source_complete_messages = self.messages(source)
        control_complete_messages = self.messages(control)
        self.assertTrue(
            any(
                message["action"] == "playback.handoff.status"
                and message["payload"]["status"] == "completed"
                for message in target_complete_messages
                + source_complete_messages
                + control_complete_messages
            )
        )
        self.assertTrue(
            any(
                message["action"] == "playback.handoff.release"
                and message["payload"]["instruction"] == "pause"
                for message in source_complete_messages
            )
        )
        persisted = getPlaybackContextState("ctx-handoff")
        self.assertEqual(persisted["authorityClientId"], "web-player-target")
        self.assertEqual(
            persisted["authorityDeviceSessionId"], "web-player-device:target"
        )
        for message in (
            control_start_messages
            + [prepare]
            + target_ready_messages
            + target_complete_messages
            + source_complete_messages
            + control_complete_messages
        ):
            self.assert_no_session_fields(message)


if __name__ == "__main__":
    unittest.main()
