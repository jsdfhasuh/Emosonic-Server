import os
import shutil
import tempfile
import unittest

from supysonic.db import release_database
from supysonic.emo.ws import socketio
from supysonic.emo.ws_state import get_state
from supysonic.managers.user import UserManager
from supysonic.web import create_application

from tests.testbase import TestConfig


class EmoWebSocketTestCase(unittest.TestCase):
  def setUp(self):
    self.__db = tempfile.mkstemp()
    self.__dir = tempfile.mkdtemp()
    self.config = TestConfig(False, False)
    self.config.BASE["database_uri"] = "sqlite:///" + self.__db[1]
    self.config.WEBAPP["cache_dir"] = self.__dir
    self.config.WEBAPP["mount_emosonic"] = True

    self.app = create_application(self.config)
    self.http_client = self.app.test_client()

    UserManager.add("alice", "Alic3", admin=True)
    UserManager.add("bob", "B0b")

    state = get_state()
    state._sessions.clear()
    state._client_to_sid.clear()
    state._clients.clear()
    state._queues.clear()
    state._local_queues.clear()
    state._playback_states.clear()
    state._session_subscriptions.clear()

    self.clients = []

  def tearDown(self):
    for client in self.clients:
      client.disconnect(namespace="/emo")
    release_database()
    shutil.rmtree(self.__dir)
    os.close(self.__db[0])
    os.remove(self.__db[1])

  def connect_authenticated_client(self, user_name, password, request_id="auth-1"):
    client = socketio.test_client(self.app, namespace="/emo", flask_test_client=self.http_client)
    self.clients.append(client)

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
    auth_messages = self.get_messages(client)
    self.assertTrue(
      any(message["action"] == "system.ack" and message["requestId"] == request_id for message in auth_messages)
    )
    return client

  def register_device(self, client, request_id, payload):
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
    return self.get_messages(client)

  def connect_device(self, user_name, password, client_id, session_id, roles):
    client = socketio.test_client(self.app, namespace="/emo", flask_test_client=self.http_client)
    self.clients.append(client)

    client.emit(
      "message",
      {
        "type": "auth",
        "action": "auth.login",
        "requestId": f"auth-{client_id}",
        "payload": {"u": user_name, "p": password},
      },
      namespace="/emo",
    )
    auth_messages = client.get_received("/emo")
    self.assertTrue(any(item["name"] == "message" for item in auth_messages))

    client.emit(
      "message",
      {
        "type": "device",
        "action": "device.register",
        "requestId": f"register-{client_id}",
        "payload": {
          "clientId": client_id,
          "deviceName": client_id,
          "roles": roles,
          "sessionId": session_id,
        },
      },
      namespace="/emo",
    )
    client.get_received("/emo")
    return client

  def get_messages(self, client):
    events = client.get_received("/emo")
    messages = []
    for item in events:
      if item["name"] != "message":
        continue

      args = item.get("args")
      if isinstance(args, list):
        messages.append(args[0])
      else:
        messages.append(args)
    return messages

  def subscribe_session(self, client, session_id, request_id="subscribe-1"):
    client.emit(
      "message",
      {
        "type": "state",
        "action": "session.subscribe",
        "requestId": request_id,
        "payload": {"sessionId": session_id},
      },
      namespace="/emo",
    )

  def test_device_register_keeps_alias_in_device_list(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-player-1")
    alias = "\u5ba2\u5385\u64ad\u653e\u5668"

    messages = self.register_device(
      client,
      "register-player-1",
      {
        "clientId": "player-1",
        "deviceName": "Windows Player",
        "alias": alias,
        "roles": ["player"],
        "sessionId": "sess-main",
      },
    )

    ack = next(message for message in messages if message["action"] == "system.ack")
    self.assertEqual(ack["payload"]["client"]["alias"], alias)

    device_list = next(message for message in messages if message["action"] == "device.list")
    device = next(device for device in device_list["payload"]["devices"] if device["clientId"] == "player-1")
    self.assertEqual(device["alias"], alias)

  def test_device_register_alias_falls_back_to_device_name(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-player-1")

    messages = self.register_device(
      client,
      "register-player-1",
      {
        "clientId": "player-1",
        "deviceName": "Living Room Player",
        "roles": ["player"],
        "sessionId": "sess-main",
      },
    )

    ack = next(message for message in messages if message["action"] == "system.ack")
    self.assertEqual(ack["payload"]["client"]["alias"], "Living Room Player")

  def test_device_register_alias_falls_back_to_client_id(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-player-1")

    messages = self.register_device(
      client,
      "register-player-1",
      {
        "clientId": "player-1",
        "roles": ["player"],
        "sessionId": "sess-main",
      },
    )

    ack = next(message for message in messages if message["action"] == "system.ack")
    self.assertEqual(ack["payload"]["client"]["alias"], "player-1")

  def test_device_register_rejects_non_string_alias(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-player-1")

    messages = self.register_device(
      client,
      "register-player-1",
      {
        "clientId": "player-1",
        "alias": 123,
        "roles": ["player"],
        "sessionId": "sess-main",
      },
    )

    error = next(message for message in messages if message["action"] == "system.error")
    self.assertEqual(error["requestId"], "register-player-1")
    self.assertEqual(error["payload"]["code"], "bad_request")

  def test_system_ping_refreshes_registered_client_last_seen(self):
    client = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    state = get_state()
    state._clients["player-1"]["lastSeenAt"] = 1

    client.emit(
      "message",
      {
        "type": "system",
        "action": "system.ping",
        "requestId": "ping-1",
        "payload": {},
      },
      namespace="/emo",
    )

    messages = self.get_messages(client)
    self.assertTrue(
      any(message["action"] == "system.pong" and message["requestId"] == "ping-1" for message in messages)
    )
    self.assertGreater(state.get_client("player-1")["lastSeenAt"], 1)

  def test_forward_queue_play_item_for_session_queue(self):
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "queue.playItem",
        "requestId": "play-session-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1", "queueIndex": 2},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    player_messages = self.get_messages(player)

    self.assertTrue(
      any(
        message["action"] == "system.ack"
        and message["requestId"] == "play-session-1"
        and message["payload"].get("forwarded") is True
        for message in controller_messages
      )
    )
    forwarded = next(message for message in player_messages if message["action"] == "queue.playItem")
    self.assertEqual(forwarded["payload"], {"sessionId": "sess-1", "queueIndex": 2})
    self.assertEqual(forwarded["sourceClientId"], "controller-1")
    self.assertEqual(forwarded["targetClientId"], "player-1")

  def test_forward_queue_play_item_for_local_queue(self):
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "queue.playItem",
        "requestId": "play-local-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1", "clientId": "player-1", "queueIndex": 1},
      },
      namespace="/emo",
    )

    player_messages = self.get_messages(player)
    forwarded = next(message for message in player_messages if message["action"] == "queue.playItem")
    self.assertEqual(
      forwarded["payload"],
      {"sessionId": "sess-1", "clientId": "player-1", "queueIndex": 1},
    )

  def test_reject_queue_play_item_when_client_id_differs_from_target(self):
    self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "queue.playItem",
        "requestId": "play-invalid-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1", "clientId": "player-2", "queueIndex": 1},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    error = next(message for message in controller_messages if message["action"] == "system.error")
    self.assertEqual(error["requestId"], "play-invalid-1")
    self.assertEqual(error["payload"]["code"], "bad_request")

  def test_reject_queue_play_item_across_users(self):
    self.connect_device("bob", "B0b", "player-bob", "sess-2", ["player"])
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "queue.playItem",
        "requestId": "play-cross-user-1",
        "targetClientId": "player-bob",
        "payload": {"sessionId": "sess-2", "queueIndex": 0},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    error = next(message for message in controller_messages if message["action"] == "system.error")
    self.assertEqual(error["requestId"], "play-cross-user-1")
    self.assertEqual(error["payload"]["code"], "forbidden")

  def test_forward_player_request_state(self):
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.requestState",
        "requestId": "request-state-1",
        "targetClientId": "player-1",
        "payload": {
          "sessionId": "sess-1",
          "includePlayback": True,
          "includeSessionQueue": True,
          "includeLocalQueue": True,
          "includeReadyState": False,
        },
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    player_messages = self.get_messages(player)

    self.assertTrue(
      any(
        message["action"] == "system.ack"
        and message["requestId"] == "request-state-1"
        and message["payload"].get("forwarded") is True
        for message in controller_messages
      )
    )
    forwarded = next(message for message in player_messages if message["action"] == "player.requestState")
    self.assertEqual(
      forwarded["payload"],
      {
        "sessionId": "sess-1",
        "includePlayback": True,
        "includeSessionQueue": True,
        "includeLocalQueue": True,
        "includeReadyState": False,
      },
    )
    self.assertEqual(forwarded["sourceClientId"], "controller-1")
    self.assertEqual(forwarded["targetClientId"], "player-1")

  def test_reject_player_request_state_with_invalid_flag(self):
    self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.requestState",
        "requestId": "request-state-invalid-1",
        "targetClientId": "player-1",
        "payload": {
          "includePlayback": "yes",
        },
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    error = next(message for message in controller_messages if message["action"] == "system.error")
    self.assertEqual(error["requestId"], "request-state-invalid-1")
    self.assertEqual(error["payload"]["code"], "bad_request")

  def test_broadcast_local_queue_to_session_members(self):
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.local.set",
        "requestId": "local-set-broadcast-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 1,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    local_queue = next(message for message in controller_messages if message["action"] == "queue.local.set")
    self.assertEqual(local_queue["payload"]["sessionId"], "sess-1")
    self.assertEqual(local_queue["payload"]["sourceClientId"], "player-1")
    self.assertEqual(local_queue["payload"]["currentIndex"], 1)

  def test_session_subscribe_receives_local_queue_snapshot(self):
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    observer = self.connect_device("alice", "Alic3", "observer-1", "observer-room", ["controller"])

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.local.set",
        "requestId": "local-set-snapshot-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1", "song-2", "song-3"],
          "currentIndex": 2,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    self.get_messages(player)

    self.subscribe_session(observer, "sess-1", request_id="subscribe-local-snapshot-1")
    observer_messages = self.get_messages(observer)

    local_queue = next(message for message in observer_messages if message["action"] == "queue.local.set")
    self.assertEqual(local_queue["payload"]["sessionId"], "sess-1")
    self.assertEqual(local_queue["payload"]["sourceClientId"], "player-1")
    self.assertEqual(local_queue["payload"]["currentIndex"], 2)
