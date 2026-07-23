import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from supysonic.db import release_database
from supysonic.emo.ws import socketio
from supysonic.managers.user import UserManager
from supysonic.web import create_application

from tests.testbase import TestConfig


class EmoLoggingTestCase(unittest.TestCase):
    def setUp(self):
        self._db = tempfile.mkstemp()
        self._dir = tempfile.mkdtemp()
        self.config = TestConfig(True, False)
        self.config.BASE["database_uri"] = "sqlite:///" + self._db[1]
        self.config.WEBAPP["cache_dir"] = self._dir
        self.config.WEBAPP["log_dir"] = self._dir
        self.config.WEBAPP["log_level"] = "INFO"
        self.config.WEBAPP["mount_emosonic"] = True
        self.uploadDir = os.path.join(self._dir, "uploaded-emo-logs")
        self.config.WEBAPP["emo_log_upload_dir"] = self.uploadDir
        self.app = create_application(self.config)
        self.client = self.app.test_client()
        self.http_client = self.app.test_client()
        UserManager.add("alice", "Alic3", admin=True)
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            if client.is_connected(namespace="/emo"):
                client.disconnect(namespace="/emo")
        release_database()
        shutil.rmtree(self._dir)
        os.close(self._db[0])
        os.remove(self._db[1])

    def readEmoLog(self):
        with open(os.path.join(self._dir, "emo.log"), "r", encoding="utf-8") as f:
            return f.read()

    def connectDevice(self, userName, password, clientId, sessionId, roles):
        client = socketio.test_client(self.app, namespace="/emo", flask_test_client=self.http_client)
        self.clients.append(client)

        client.emit(
            "message",
            {
                "type": "auth",
                "action": "auth.login",
                "requestId": f"auth-{clientId}",
                "payload": {"u": userName, "p": password},
            },
            namespace="/emo",
        )
        client.get_received("/emo")

        client.emit(
            "message",
            {
                "type": "device",
                "action": "device.register",
                "requestId": f"register-{clientId}",
                "payload": {
                    "clientId": clientId,
                    "deviceName": clientId,
                    "roles": roles,
                    "sessionId": sessionId,
                },
            },
            namespace="/emo",
        )
        client.get_received("/emo")
        return client

    def test_logs_auth_login_and_device_register_to_emo_log(self):
        client = socketio.test_client(self.app, namespace="/emo", flask_test_client=self.http_client)
        self.clients.append(client)

        client.emit(
            "message",
            {
                "type": "auth",
                "action": "auth.login",
                "requestId": "auth-1",
                "payload": {"u": "alice", "p": "Alic3"},
            },
            namespace="/emo",
        )
        client.get_received("/emo")

        client.emit(
            "message",
            {
                "type": "device",
                "action": "device.register",
                "requestId": "register-1",
                "payload": {
                    "clientId": "player-1",
                    "deviceName": "player-1",
                    "roles": ["player"],
                    "sessionId": "sess-1",
                },
            },
            namespace="/emo",
        )
        client.get_received("/emo")

        content = self.readEmoLog()

        self.assertIn("emo event=auth_login result=success user=alice client_request_id=auth-1", content)
        self.assertIn(
            "emo event=device_register result=success user=alice client_id=player-1 session_id=sess-1 roles=player client_request_id=register-1",
            content,
        )
        self.assertNotIn("Alic3", content)

    def test_logs_failed_auth_without_password(self):
        client = socketio.test_client(self.app, namespace="/emo", flask_test_client=self.http_client)
        self.clients.append(client)

        client.emit(
            "message",
            {
                "type": "auth",
                "action": "auth.login",
                "requestId": "auth-fail-1",
                "payload": {"u": "alice", "p": "wrong-password"},
            },
            namespace="/emo",
        )
        client.get_received("/emo")

        content = self.readEmoLog()

        self.assertIn(
            "emo event=auth_login result=failure user=alice client_request_id=auth-fail-1 action=auth.login",
            content,
        )
        self.assertNotIn("wrong-password", content)

    def test_logs_http_auth_failure_without_password(self):
        rv = self.client.get("/emo/getVersion?u=alice&p=wrong-password&f=json")

        self.assertEqual(rv.status_code, 200)
        content = self.readEmoLog()

        self.assertIn("emo event=auth_failure", content)
        self.assertIn("result=failure", content)
        self.assertIn("user=alice", content)
        self.assertIn("reason=wrong_credentials", content)
        self.assertNotIn("wrong-password", content)

    def test_logs_upload_log_missing_file_part(self):
        rv = self.client.post("/emo/upload_log?u=alice&p=Alic3")

        self.assertEqual(rv.status_code, 400)
        content = self.readEmoLog()

        self.assertIn("emo event=upload_log_failed", content)
        self.assertIn("result=bad_request", content)
        self.assertIn("reason=no_file_part", content)

    def test_logs_upload_log_empty_filename(self):
        rv = self.client.post(
            "/emo/upload_log?u=alice&p=Alic3",
            data={"file": (io.BytesIO(b"test"), "")},
            content_type="multipart/form-data",
        )

        self.assertEqual(rv.status_code, 400)
        content = self.readEmoLog()

        self.assertIn("emo event=upload_log_failed", content)
        self.assertIn("result=bad_request", content)
        self.assertIn("reason=empty_filename", content)

    def test_logs_upload_log_success(self):
        rv = self.client.post(
            "/emo/upload_log?u=alice&p=Alic3",
            data={"file": (io.BytesIO(b"test"), "mobile.log")},
            content_type="multipart/form-data",
        )

        self.assertEqual(rv.status_code, 200)
        content = self.readEmoLog()

        self.assertIn("emo event=upload_log_succeeded", content)
        self.assertIn("result=success", content)
        self.assertIn("user=alice", content)
        self.assertIn("filename=mobile.log", content)
        self.assertIn("path=/emo/upload_log", content)

    def test_logs_upload_log_uses_configured_upload_directory(self):
        rv = self.client.post(
            "/emo/upload_log?u=alice&p=Alic3",
            data={"file": (io.BytesIO(b"configured log"), "configured.log")},
            content_type="multipart/form-data",
        )

        self.assertEqual(rv.status_code, 200)
        savedPath = rv.get_json()["path"]
        self.assertTrue(savedPath.startswith(self.uploadDir))
        self.assertTrue(os.path.isfile(savedPath))

        listResponse = self.client.get("/emo/logs?u=alice&p=Alic3")
        self.assertEqual(listResponse.status_code, 200)
        self.assertIn(os.path.basename(savedPath).encode("utf-8"), listResponse.data)

    def test_logs_upload_log_save_failure(self):
        with patch("werkzeug.datastructures.FileStorage.save", side_effect=OSError("disk full")):
            rv = self.client.post(
                "/emo/upload_log?u=alice&p=Alic3",
                data={"file": (io.BytesIO(b"test"), "mobile.log")},
                content_type="multipart/form-data",
            )

        self.assertEqual(rv.status_code, 500)
        content = self.readEmoLog()

        self.assertIn("emo event=upload_log_failed", content)
        self.assertIn("result=server_error", content)
        self.assertIn("reason=save_failed", content)
        self.assertIn("user=alice", content)
        self.assertIn("filename=mobile.log", content)

    def test_logs_view_log_missing_file(self):
        rv = self.client.get("/emo/viewlog?u=alice&p=Alic3&filename=missing.log")

        self.assertEqual(rv.status_code, 404)
        content = self.readEmoLog()

        self.assertIn("emo event=view_log_failed", content)
        self.assertIn("result=not_found", content)
        self.assertIn("filename=missing.log", content)

    def test_logs_download_log_missing_file(self):
        rv = self.client.get("/emo/downloadlog?u=alice&p=Alic3&filename=missing.log")

        self.assertEqual(rv.status_code, 404)
        content = self.readEmoLog()

        self.assertIn("emo event=download_log_failed", content)
        self.assertIn("result=not_found", content)
        self.assertIn("filename=missing.log", content)

    def test_logs_session_subscribe_and_unsubscribe(self):
        self.connectDevice("alice", "Alic3", "player-1", "sess-1", ["player"])
        observer = self.connectDevice("alice", "Alic3", "observer-1", "observer-room", ["controller"])

        observer.emit(
            "message",
            {
                "type": "state",
                "action": "session.subscribe",
                "requestId": "subscribe-1",
                "payload": {"sessionId": "sess-1"},
            },
            namespace="/emo",
        )
        observer.get_received("/emo")

        observer.emit(
            "message",
            {
                "type": "state",
                "action": "session.unsubscribe",
                "requestId": "unsubscribe-1",
                "payload": {"sessionId": "sess-1"},
            },
            namespace="/emo",
        )
        observer.get_received("/emo")

        content = self.readEmoLog()

        self.assertIn("emo event=session_subscribe", content)
        self.assertIn("client_request_id=subscribe-1", content)
        self.assertIn("session_id=sess-1", content)
        self.assertIn("client_id=observer-1", content)
        self.assertIn("emo event=session_unsubscribe", content)
        self.assertIn("client_request_id=unsubscribe-1", content)

    def test_logs_control_forward_summary(self):
        self.connectDevice("alice", "Alic3", "player-1", "sess-1", ["player"])
        controller = self.connectDevice("alice", "Alic3", "controller-1", "sess-1", ["controller"])

        controller.emit(
            "message",
            {
                "type": "command",
                "action": "player.pause",
                "requestId": "pause-1",
                "targetClientId": "player-1",
                "payload": {"sessionId": "sess-1"},
            },
            namespace="/emo",
        )
        controller.get_received("/emo")

        content = self.readEmoLog()

        self.assertIn("emo event=control_forward", content)
        self.assertIn("action=player.pause", content)
        self.assertIn("source_client_id=controller-1", content)
        self.assertIn("target_client_id=player-1", content)
        self.assertIn("client_request_id=pause-1", content)

    def test_logs_queue_local_set_summary_without_song_ids(self):
        self.connectDevice("alice", "Alic3", "player-1", "sess-1", ["player"])
        controller = self.connectDevice("alice", "Alic3", "controller-1", "sess-1", ["controller"])

        controller.emit(
            "message",
            {
                "type": "state",
                "action": "queue.local.set",
                "requestId": "local-set-1",
                "payload": {
                    "sessionId": "sess-1",
                    "queueSongIds": ["song-1", "song-2"],
                    "currentIndex": 1,
                    "positionMs": 0,
                },
            },
            namespace="/emo",
        )
        controller.get_received("/emo")

        content = self.readEmoLog()

        self.assertIn("emo event=queue_local_set", content)
        self.assertIn("queue_size=2", content)
        self.assertIn("current_index=1", content)
        self.assertIn("position_ms=0", content)
        self.assertIn("session_id=sess-1", content)
        self.assertIn("source_client_id=controller-1", content)
        self.assertIn("client_request_id=local-set-1", content)
        self.assertNotIn("song-1", content)
        self.assertNotIn("song-2", content)

    def test_logs_bad_message_when_action_is_missing(self):
        client = socketio.test_client(self.app, namespace="/emo", flask_test_client=self.http_client)
        self.clients.append(client)

        client.emit(
            "message",
            {
                "type": "auth",
                "requestId": "missing-action-1",
                "payload": {},
            },
            namespace="/emo",
        )
        self.assertFalse(client.is_connected(namespace="/emo"))

        content = self.readEmoLog()

        self.assertIn("emo event=bad_message", content)
        self.assertIn("result=bad_request", content)
        self.assertIn("reason=invalid_correlation", content)

    def test_logs_unauthorized_action_before_authentication(self):
        client = socketio.test_client(self.app, namespace="/emo", flask_test_client=self.http_client)
        self.clients.append(client)

        client.emit(
            "message",
            {
                "type": "command",
                "action": "player.pause",
                "requestId": "unauthorized-1",
                "targetClientId": "player-1",
                "payload": {"sessionId": "sess-1"},
            },
            namespace="/emo",
        )
        client.get_received("/emo")

        content = self.readEmoLog()

        self.assertIn("emo event=unauthorized_action", content)
        self.assertIn("action=player.pause", content)
        self.assertIn("result=unauthorized", content)
        self.assertIn("client_request_id=unauthorized-1", content)

    def test_logs_unsupported_action(self):
        controller = self.connectDevice("alice", "Alic3", "controller-1", "sess-1", ["controller"])

        controller.emit(
            "message",
            {
                "type": "command",
                "action": "player.shuffleEverything",
                "requestId": "unsupported-1",
                "payload": {},
            },
            namespace="/emo",
        )
        controller.get_received("/emo")

        content = self.readEmoLog()

        self.assertIn("emo event=unsupported_action", content)
        self.assertIn("action=player.shuffleEverything", content)
        self.assertIn("client_request_id=unsupported-1", content)

    def test_logs_playback_update_summary(self):
        player = self.connectDevice("alice", "Alic3", "player-1", "sess-1", ["player"])

        player.emit(
            "message",
            {
                "type": "event",
                "action": "playback.update",
                "requestId": "playback-1",
                "payload": {
                    "sessionId": "sess-1",
                    "state": "paused",
                    "trackId": "track-1",
                    "positionMs": 81234,
                },
            },
            namespace="/emo",
        )
        player.get_received("/emo")

        content = self.readEmoLog()

        self.assertIn("emo event=playback_update", content)
        self.assertIn("result=success", content)
        self.assertIn("session_id=sess-1", content)
        self.assertIn("source_client_id=player-1", content)
        self.assertIn("playback_state=paused", content)
        self.assertIn("track_id=track-1", content)
        self.assertIn("position_ms=81234", content)
        self.assertIn("client_request_id=playback-1", content)

    def test_logs_queue_local_get_summary_without_song_ids(self):
        player = self.connectDevice("alice", "Alic3", "player-1", "sess-1", ["player"])

        player.emit(
            "message",
            {
                "type": "state",
                "action": "queue.local.set",
                "requestId": "local-set-before-get-1",
                "payload": {
                    "sessionId": "sess-1",
                    "queueSongIds": ["song-1", "song-2"],
                    "currentIndex": 0,
                    "positionMs": 42,
                },
            },
            namespace="/emo",
        )
        player.get_received("/emo")

        player.emit(
            "message",
            {
                "type": "state",
                "action": "queue.local.get",
                "requestId": "local-get-1",
                "payload": {
                    "sessionId": "sess-1",
                    "clientId": "player-1",
                },
            },
            namespace="/emo",
        )
        player.get_received("/emo")

        content = self.readEmoLog()

        self.assertIn("emo event=queue_local_get", content)
        self.assertIn("result=hit", content)
        self.assertIn("session_id=sess-1", content)
        self.assertIn("source_client_id=player-1", content)
        self.assertIn("queue_size=2", content)
        self.assertIn("position_ms=42", content)
        self.assertIn("client_request_id=local-get-1", content)
        self.assertNotIn("song-1", content)
        self.assertNotIn("song-2", content)

    def test_logs_queue_session_sync_summary_without_song_ids(self):
        controller = self.connectDevice("alice", "Alic3", "controller-1", "sess-1", ["controller"])

        controller.emit(
            "message",
            {
                "type": "state",
                "action": "queue.session.sync",
                "requestId": "queue-sync-1",
                "payload": {
                    "sessionId": "sess-1",
                    "queueSongIds": ["song-1", "song-2", "song-3"],
                    "currentIndex": 2,
                    "positionMs": 8000,
                },
            },
            namespace="/emo",
        )
        controller.get_received("/emo")

        content = self.readEmoLog()

        self.assertIn("emo event=queue_session_sync", content)
        self.assertIn("result=success", content)
        self.assertIn("session_id=sess-1", content)
        self.assertIn("source_client_id=controller-1", content)
        self.assertIn("queue_size=3", content)
        self.assertIn("current_index=2", content)
        self.assertIn("position_ms=8000", content)
        self.assertIn("client_request_id=queue-sync-1", content)
        self.assertNotIn("song-1", content)
        self.assertNotIn("song-2", content)
        self.assertNotIn("song-3", content)

    def test_logs_queue_ready_complete_summary_without_song_ids(self):
        controller = self.connectDevice("alice", "Alic3", "controller-1", "sess-1", ["controller"])

        controller.emit(
            "message",
            {
                "type": "state",
                "action": "queue.ready.complete",
                "requestId": "queue-ready-1",
                "payload": {
                    "sessionId": "sess-1",
                    "queueType": "session",
                    "queueSongIds": ["song-1", "song-2"],
                },
            },
            namespace="/emo",
        )
        controller.get_received("/emo")

        content = self.readEmoLog()

        self.assertIn("emo event=queue_ready_complete", content)
        self.assertIn("result=broadcast", content)
        self.assertIn("session_id=sess-1", content)
        self.assertIn("source_client_id=controller-1", content)
        self.assertIn("queue_type=session", content)
        self.assertIn("queue_size=2", content)
        self.assertIn("client_request_id=queue-ready-1", content)
        self.assertNotIn("song-1", content)
        self.assertNotIn("song-2", content)

    def test_logs_control_forward_target_not_found(self):
        controller = self.connectDevice("alice", "Alic3", "controller-1", "sess-1", ["controller"])

        controller.emit(
            "message",
            {
                "type": "command",
                "action": "player.pause",
                "requestId": "pause-missing-1",
                "targetClientId": "missing-player",
                "payload": {"sessionId": "sess-1"},
            },
            namespace="/emo",
        )
        controller.get_received("/emo")

        content = self.readEmoLog()

        self.assertIn("emo event=control_forward", content)
        self.assertIn("result=not_found", content)
        self.assertIn("action=player.pause", content)
        self.assertIn("client_request_id=pause-missing-1", content)

    def test_logs_queue_session_sync_bad_request(self):
        controller = self.connectDevice("alice", "Alic3", "controller-1", "sess-1", ["controller"])

        controller.emit(
            "message",
            {
                "type": "state",
                "action": "queue.session.sync",
                "requestId": "queue-sync-bad-1",
                "payload": {
                    "sessionId": "sess-1",
                    "queueSongIds": ["song-1"],
                    "currentIndex": 3,
                    "positionMs": 0,
                },
            },
            namespace="/emo",
        )
        controller.get_received("/emo")

        content = self.readEmoLog()

        self.assertIn("emo event=queue_session_sync", content)
        self.assertIn("result=bad_request", content)
        self.assertIn("client_request_id=queue-sync-bad-1", content)
