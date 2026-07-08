import os
import shutil
import tempfile
import unittest

from supysonic.db import release_database
from supysonic.emo.ws import (
  _build_message,
  _commit_prepare,
  _expire_handoff_complete,
  _expire_prepare,
  socketio,
)
from supysonic.emo.ws_store import (
  getDevicePlaybackState,
  getLocalQueueState,
  getPlaybackContextState,
  getPlaybackState,
  saveLocalQueueState,
)
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
    state._playback_timelines.clear()
    state._session_subscriptions.clear()
    state._broadcasts.clear()
    state._broadcast_participants.clear()
    state._broadcast_playback_states.clear()
    state._client_active_broadcast.clear()
    state._follow_relationships.clear()
    state._pending_prepares.clear()
    state._playback_contexts.clear()
    state._device_playback_states.clear()
    state._handoffs.clear()
    state._handoff_request_index.clear()
    state._playback_context_subscriptions.clear()

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

  def connect_device(self, user_name, password, client_id, session_id, roles, capabilities=None):
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
          "capabilities": capabilities or {},
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

  def start_broadcast(self, client, target_client_ids, request_id="broadcast-start-1", **payload_overrides):
    payload = {
      "targetMode": "selectedClients",
      "targetClientIds": target_client_ids,
      "queueSongIds": ["song-1", "song-2", "song-3"],
      "currentIndex": 0,
      "positionMs": 0,
      "autoPlay": True,
    }
    payload.update(payload_overrides)
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

  def get_ack(self, messages, request_id):
    return next(
      message
      for message in messages
      if message["action"] == "system.ack" and message["requestId"] == request_id
    )

  def get_error(self, messages, request_id):
    return next(
      message
      for message in messages
      if message["action"] == "system.error" and message["requestId"] == request_id
    )

  def sync_playback_context(
    self,
    client,
    request_id,
    playback_context_id="playback:alice:main",
    device_session_id="root:phone",
    queue_song_ids=None,
    current_index=0,
    position_ms=0,
  ):
    client.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": request_id,
        "payload": {
          "playbackContextId": playback_context_id,
          "deviceSessionId": device_session_id,
          "queueSongIds": queue_song_ids or ["song-1"],
          "currentIndex": current_index,
          "positionMs": position_ms,
        },
      },
      namespace="/emo",
    )
    return self.get_ack(self.get_messages(client), request_id)

  def test_build_message_stamps_server_time_without_mutating_payload(self):
    payload = {"serverUpdatedAtMs": 1000, "positionMs": 10}

    message = _build_message("state", "playback.update", payload)

    self.assertIn("serverTimeMs", message["payload"])
    self.assertGreaterEqual(message["payload"]["serverTimeMs"], payload["serverUpdatedAtMs"])
    self.assertNotIn("serverTimeMs", payload)

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

  def test_device_register_accepts_device_session_id(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-player-1")

    messages = self.register_device(
      client,
      "register-player-1",
      {
        "clientId": "player-1",
        "deviceName": "Windows Player",
        "roles": ["player"],
        "deviceSessionId": "root:pc",
      },
    )

    ack = self.get_ack(messages, "register-player-1")
    registered = ack["payload"]["client"]
    self.assertEqual(registered["deviceSessionId"], "root:pc")
    self.assertEqual(registered["sessionId"], "root:pc")

  def test_device_register_alias_strips_whitespace(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-player-1")

    messages = self.register_device(
      client,
      "register-player-1",
      {
        "clientId": "player-1",
        "deviceName": "Windows Player",
        "alias": "  \u5ba2\u5385\u64ad\u653e\u5668  ",
        "roles": ["player"],
        "sessionId": "sess-main",
      },
    )

    ack = next(message for message in messages if message["action"] == "system.ack")
    self.assertEqual(ack["payload"]["client"]["alias"], "\u5ba2\u5385\u64ad\u653e\u5668")

  def test_device_register_blank_alias_falls_back_to_device_name(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-player-1")

    messages = self.register_device(
      client,
      "register-player-1",
      {
        "clientId": "player-1",
        "deviceName": "Windows Player",
        "alias": "   ",
        "roles": ["player"],
        "sessionId": "sess-main",
      },
    )

    ack = next(message for message in messages if message["action"] == "system.ack")
    self.assertEqual(ack["payload"]["client"]["alias"], "Windows Player")

  def test_device_register_blank_device_name_falls_back_to_client_id(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-player-1")

    messages = self.register_device(
      client,
      "register-player-1",
      {
        "clientId": "player-1",
        "deviceName": "   ",
        "roles": ["player"],
        "sessionId": "sess-main",
      },
    )

    ack = next(message for message in messages if message["action"] == "system.ack")
    self.assertEqual(ack["payload"]["client"]["deviceName"], "player-1")
    self.assertEqual(ack["payload"]["client"]["alias"], "player-1")

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

  def test_device_register_rejects_non_string_device_name(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-player-1")

    messages = self.register_device(
      client,
      "register-player-1",
      {
        "clientId": "player-1",
        "deviceName": 123,
        "roles": ["player"],
        "sessionId": "sess-main",
      },
    )

    error = next(message for message in messages if message["action"] == "system.error")
    self.assertEqual(error["requestId"], "register-player-1")
    self.assertEqual(error["payload"]["code"], "bad_request")

  def test_device_register_accepts_device_alias(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-player-1")

    messages = self.register_device(
      client,
      "register-player-1",
      {
        "clientId": "player-1",
        "deviceName": "Windows Player",
        "deviceAlias": "\u7535\u8111\u64ad\u653e\u5668",
        "roles": ["player"],
        "sessionId": "sess-main",
      },
    )

    ack = next(message for message in messages if message["action"] == "system.ack")
    self.assertEqual(ack["payload"]["client"]["alias"], "\u7535\u8111\u64ad\u653e\u5668")

  def test_device_register_preserves_playback_timing_capabilities(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-player-cap")

    messages = self.register_device(
      client,
      "register-player-cap",
      {
        "clientId": "player-cap",
        "deviceName": "Timing Player",
        "roles": ["player"],
        "sessionId": "sess-cap",
        "capabilities": {
          "effectiveAtPlayback": True,
          "playbackPrepare": True,
        },
      },
    )

    ack = next(message for message in messages if message["action"] == "system.ack")
    self.assertTrue(ack["payload"]["client"]["capabilities"]["effectiveAtPlayback"])
    self.assertTrue(ack["payload"]["client"]["capabilities"]["playbackPrepare"])

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

  def test_web_player_without_timing_capabilities_uses_legacy_direct_forwarding(self):
    web_player = self.connect_device("alice", "Alic3", "web-player-1", "web:player", ["player"])
    controller = self.connect_device("alice", "Alic3", "controller-1", "web:controller", ["controller"])
    self.get_messages(web_player)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.play",
        "requestId": "web-player-play-legacy-1",
        "targetClientId": "web-player-1",
        "payload": {"sessionId": "web:player"},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    web_messages = self.get_messages(web_player)
    ack = self.get_ack(controller_messages, "web-player-play-legacy-1")
    play_command = next(message for message in web_messages if message["action"] == "player.play")

    self.assertTrue(ack["payload"]["forwarded"])
    self.assertNotIn("effectiveAtServerMs", play_command["payload"])
    self.assertFalse(any(message["action"] == "playback.prepare" for message in web_messages))
    self.assertFalse(any(message["action"] == "playback.update" for message in web_messages))

  def test_player_play_uses_single_future_for_effective_at_player(self):
    capabilities = {"effectiveAtPlayback": True}
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"], capabilities=capabilities)
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])
    self.get_messages(player)
    self.get_messages(controller)

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "source-queue-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    self.get_messages(player)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.play",
        "requestId": "player-play-future-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1"},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    player_messages = self.get_messages(player)
    ack = self.get_ack(controller_messages, "player-play-future-1")
    playback = next(message for message in player_messages if message["action"] == "playback.update")
    play_command = next(message for message in player_messages if message["action"] == "player.play")

    self.assertEqual(ack["payload"]["protocolPath"], "single_future")
    self.assertEqual(playback["payload"]["trackId"], "song-1")
    self.assertEqual(playback["payload"]["state"], "playing")
    self.assertIn("effectiveAtServerMs", playback["payload"])
    self.assertEqual(play_command["payload"]["effectiveAtServerMs"], playback["payload"]["effectiveAtServerMs"])
    persisted = getPlaybackState("sess-1", "player-1")
    self.assertIsNotNone(persisted)
    self.assertEqual(persisted["state"], "playing")
    self.assertEqual(persisted["trackId"], "song-1")

  def test_player_play_keeps_reserved_control_version_after_controller_queue_sync(self):
    capabilities = {"effectiveAtPlayback": True}
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"], capabilities=capabilities)
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])
    self.get_messages(player)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "controller-queue-before-play-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    queue_ack = self.get_ack(self.get_messages(controller), "controller-queue-before-play-1")
    self.get_messages(player)
    self.assertEqual(queue_ack["payload"]["queue"]["controlVersion"], 1)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.play",
        "requestId": "controller-queue-player-play-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1"},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    player_messages = self.get_messages(player)
    ack = self.get_ack(controller_messages, "controller-queue-player-play-1")
    playback = next(message for message in player_messages if message["action"] == "playback.update")
    play_command = next(message for message in player_messages if message["action"] == "player.play")

    self.assertEqual(ack["payload"]["playback"]["controlVersion"], 2)
    self.assertEqual(playback["payload"]["controlVersion"], 2)
    self.assertEqual(play_command["payload"]["controlVersion"], 2)

  def test_playback_feedback_clears_previous_effective_at(self):
    capabilities = {"effectiveAtPlayback": True}
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"], capabilities=capabilities)
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])
    self.get_messages(player)
    self.get_messages(controller)

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "source-queue-effective-clear-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    self.get_messages(player)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.play",
        "requestId": "player-play-effective-clear-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1"},
      },
      namespace="/emo",
    )
    play_messages = self.get_messages(player)
    self.get_ack(self.get_messages(controller), "player-play-effective-clear-1")
    playback = next(message for message in play_messages if message["action"] == "playback.update")
    self.assertIn("effectiveAtServerMs", playback["payload"])

    player.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "playback-feedback-effective-clear-1",
        "payload": {
          "sessionId": "sess-1",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 1500,
        },
      },
      namespace="/emo",
    )

    feedback_messages = self.get_messages(player)
    feedback_playback = next(message for message in feedback_messages if message["action"] == "playback.update")
    state_playback = get_state().get_playback_state("sess-1", "player-1")
    persisted = getPlaybackState("sess-1", "player-1")

    self.assertNotIn("effectiveAtServerMs", feedback_playback["payload"])
    self.assertNotIn("effectiveAtServerMs", state_playback)
    self.assertNotIn("effectiveAtServerMs", persisted)

  def test_player_next_uses_single_future_for_effective_at_player(self):
    capabilities = {"effectiveAtPlayback": True}
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"], capabilities=capabilities)
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])
    self.get_messages(player)
    self.get_messages(controller)

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "source-queue-next-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1", "song-2", "song-3"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    self.get_messages(player)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.next",
        "requestId": "player-next-future-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1"},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    player_messages = self.get_messages(player)
    ack = self.get_ack(controller_messages, "player-next-future-1")
    playback = next(message for message in player_messages if message["action"] == "playback.update")
    play_command = next(message for message in player_messages if message["action"] == "player.play")

    self.assertEqual(ack["payload"]["protocolPath"], "single_future")
    self.assertEqual(playback["payload"]["trackId"], "song-2")
    self.assertEqual(playback["payload"]["currentIndex"], 1)
    self.assertIn("effectiveAtServerMs", playback["payload"])
    self.assertEqual(play_command["payload"]["effectiveAtServerMs"], playback["payload"]["effectiveAtServerMs"])
    self.assertFalse(any(message["action"] == "player.next" for message in player_messages))

  def test_player_prev_uses_single_future_for_effective_at_player(self):
    capabilities = {"effectiveAtPlayback": True}
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"], capabilities=capabilities)
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])
    self.get_messages(player)
    self.get_messages(controller)

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "source-queue-prev-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1", "song-2", "song-3"],
          "currentIndex": 1,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    self.get_messages(player)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.prev",
        "requestId": "player-prev-future-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1"},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    player_messages = self.get_messages(player)
    ack = self.get_ack(controller_messages, "player-prev-future-1")
    playback = next(message for message in player_messages if message["action"] == "playback.update")
    play_command = next(message for message in player_messages if message["action"] == "player.play")

    self.assertEqual(ack["payload"]["protocolPath"], "single_future")
    self.assertEqual(playback["payload"]["trackId"], "song-1")
    self.assertEqual(playback["payload"]["currentIndex"], 0)
    self.assertIn("effectiveAtServerMs", playback["payload"])
    self.assertEqual(play_command["payload"]["effectiveAtServerMs"], playback["payload"]["effectiveAtServerMs"])
    self.assertFalse(any(message["action"] == "player.prev" for message in player_messages))

  def test_queue_play_item_uses_prepare_ready_commit_for_prepare_player(self):
    capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"], capabilities=capabilities)
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])
    self.get_messages(player)
    self.get_messages(controller)

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "source-queue-prepare-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    self.get_messages(player)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "queue.playItem",
        "requestId": "queue-play-prepare-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1", "queueIndex": 1},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    player_messages = self.get_messages(player)
    ack = self.get_ack(controller_messages, "queue-play-prepare-1")
    prepare = next(message for message in player_messages if message["action"] == "playback.prepare")

    self.assertTrue(ack["payload"]["preparing"])
    self.assertEqual(prepare["payload"]["trackId"], "song-2")
    self.assertEqual(prepare["payload"]["sourceClientId"], "player-1")
    self.assertFalse(any(message["action"] == "player.play" for message in player_messages))

    player.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "queue-play-ready-1",
        "payload": {
          "prepareId": ack["payload"]["prepareId"],
          "clientId": "player-1",
          "ready": True,
          "positionMs": 0,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )

    ready_messages = self.get_messages(player)
    self.get_ack(ready_messages, "queue-play-ready-1")
    playback = next(message for message in ready_messages if message["action"] == "playback.update")
    play_command = next(message for message in ready_messages if message["action"] == "player.play")
    state_playback = get_state().get_playback_state("sess-1", "player-1")

    self.assertEqual(playback["payload"]["trackId"], "song-2")
    self.assertEqual(playback["payload"]["currentIndex"], 1)
    self.assertIn("effectiveAtServerMs", playback["payload"])
    self.assertEqual(play_command["payload"]["effectiveAtServerMs"], playback["payload"]["effectiveAtServerMs"])
    self.assertEqual(state_playback["controlVersion"], prepare["payload"]["controlVersion"])
    persisted = getPlaybackState("sess-1", "player-1")
    self.assertIsNotNone(persisted)
    self.assertEqual(persisted["trackId"], "song-2")
    self.assertEqual(persisted["currentIndex"], 1)

  def test_prepare_commit_is_idempotent_for_stale_prepare_copy(self):
    capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"], capabilities=capabilities)
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])
    self.get_messages(player)
    self.get_messages(controller)

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "source-queue-idempotent-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    self.get_messages(player)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "queue.playItem",
        "requestId": "queue-play-idempotent-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1", "queueIndex": 1},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    player_messages = self.get_messages(player)
    ack = self.get_ack(controller_messages, "queue-play-idempotent-1")
    prepare = next(message for message in player_messages if message["action"] == "playback.prepare")
    ready_prepare = get_state().update_prepare_ready(ack["payload"]["prepareId"], "player-1", True)

    first_commit = _commit_prepare(ready_prepare)
    second_commit = _commit_prepare(ready_prepare)
    commit_messages = self.get_messages(player)
    play_commands = [message for message in commit_messages if message["action"] == "player.play"]

    self.assertIsNotNone(first_commit)
    self.assertIsNone(second_commit)
    self.assertEqual(first_commit["controlVersion"], prepare["payload"]["controlVersion"])
    self.assertEqual(len(play_commands), 1)
    self.assertEqual(get_state().get_prepare(ack["payload"]["prepareId"])["status"], "committed")

  def test_prepare_timeout_rejects_late_ready_without_commit(self):
    capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"], capabilities=capabilities)
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])
    self.get_messages(player)
    self.get_messages(controller)

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "source-queue-timeout-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    self.get_messages(player)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "queue.playItem",
        "requestId": "queue-play-timeout-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1", "queueIndex": 1},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    player_messages = self.get_messages(player)
    ack = self.get_ack(controller_messages, "queue-play-timeout-1")
    prepare = next(message for message in player_messages if message["action"] == "playback.prepare")
    prepare_id = ack["payload"]["prepareId"]

    get_state()._pending_prepares[prepare_id]["expiresAtMs"] = 0
    expired_prepare = _expire_prepare(prepare_id)
    self.assertEqual(expired_prepare["status"], "timed_out")

    player.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "queue-play-ready-after-timeout-1",
        "payload": {
          "prepareId": prepare_id,
          "clientId": "player-1",
          "ready": True,
          "positionMs": 0,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )

    ready_messages = self.get_messages(player)
    ready_ack = self.get_ack(ready_messages, "queue-play-ready-after-timeout-1")

    self.assertTrue(ready_ack["payload"]["ignored"])
    self.assertEqual(ready_ack["payload"]["status"], "timed_out")
    self.assertFalse(any(message["action"] == "playback.update" for message in ready_messages))
    self.assertFalse(any(message["action"] == "player.play" for message in ready_messages))
    self.assertIsNone(get_state().get_playback_state("sess-1", "player-1"))

  def test_immediate_pause_supersedes_pending_prepare(self):
    capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"], capabilities=capabilities)
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])
    self.get_messages(player)
    self.get_messages(controller)

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "source-queue-supersede-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    self.get_messages(player)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "queue.playItem",
        "requestId": "queue-play-supersede-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1", "queueIndex": 1},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    player_messages = self.get_messages(player)
    prepare_ack = self.get_ack(controller_messages, "queue-play-supersede-1")
    prepare = next(message for message in player_messages if message["action"] == "playback.prepare")
    prepare_id = prepare_ack["payload"]["prepareId"]

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.pause",
        "requestId": "pause-supersede-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1", "positionMs": 5000},
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(controller), "pause-supersede-1")
    self.get_messages(player)
    self.assertEqual(get_state().get_prepare(prepare_id)["status"], "superseded")

    player.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "ready-after-supersede-1",
        "payload": {
          "prepareId": prepare_id,
          "clientId": "player-1",
          "ready": True,
          "positionMs": 0,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )

    ready_messages = self.get_messages(player)
    ready_ack = self.get_ack(ready_messages, "ready-after-supersede-1")
    playback_state = get_state().get_playback_state("sess-1", "player-1")

    self.assertTrue(ready_ack["payload"]["ignored"])
    self.assertEqual(ready_ack["payload"]["status"], "superseded")
    self.assertFalse(any(message["action"] == "player.play" for message in ready_messages))
    self.assertFalse(
      any(
        message["action"] == "playback.update"
        and message["payload"].get("state") == "playing"
        for message in ready_messages
      )
    )
    self.assertEqual(playback_state["state"], "paused")
    self.assertEqual(playback_state["positionMs"], 5000)

  def test_queue_session_sync_supersedes_pending_prepare(self):
    capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"], capabilities=capabilities)
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])
    self.get_messages(player)
    self.get_messages(controller)

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "source-queue-sync-supersede-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    self.get_messages(player)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "queue.playItem",
        "requestId": "queue-play-before-sync-supersede-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1", "queueIndex": 1},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    player_messages = self.get_messages(player)
    prepare_ack = self.get_ack(controller_messages, "queue-play-before-sync-supersede-1")
    prepare = next(message for message in player_messages if message["action"] == "playback.prepare")
    prepare_id = prepare_ack["payload"]["prepareId"]

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "source-queue-sync-newer-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-3"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(player), "source-queue-sync-newer-1")
    self.get_messages(controller)
    self.assertEqual(get_state().get_prepare(prepare_id)["status"], "superseded")

    player.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "ready-after-queue-sync-supersede-1",
        "payload": {
          "prepareId": prepare_id,
          "clientId": "player-1",
          "ready": True,
          "positionMs": 0,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )

    ready_messages = self.get_messages(player)
    ready_ack = self.get_ack(ready_messages, "ready-after-queue-sync-supersede-1")
    self.assertTrue(ready_ack["payload"]["ignored"])
    self.assertEqual(ready_ack["payload"]["status"], "superseded")
    self.assertFalse(any(message["action"] == "player.play" for message in ready_messages))
    self.assertEqual(get_state().get_queue("sess-1")["queueSongIds"], ["song-3"])

  def test_source_prepare_includes_capable_follow_participant_without_blocking_timeout_commit(self):
    capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"], capabilities=capabilities)
    laptop = self.connect_device("alice", "Alic3", "laptop-1", "root:laptop", ["player"], capabilities=capabilities)
    controller = self.connect_device("alice", "Alic3", "controller-1", "root:controller", ["controller"])
    self.get_messages(phone)
    self.get_messages(laptop)
    self.get_messages(controller)

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "source-follow-queue-1",
        "payload": {
          "sessionId": "root:phone",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    self.get_messages(phone)
    self.get_messages(laptop)
    self.get_messages(controller)

    laptop.emit(
      "message",
      {
        "type": "state",
        "action": "follow.start",
        "requestId": "follow-start-prepare-1",
        "payload": {
          "sourceClientId": "phone-1",
          "sourceSessionId": "root:phone",
        },
      },
      namespace="/emo",
    )
    self.get_messages(laptop)
    self.get_messages(phone)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.play",
        "requestId": "source-follow-play-1",
        "targetClientId": "phone-1",
        "payload": {"sessionId": "root:phone"},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    phone_messages = self.get_messages(phone)
    laptop_messages = self.get_messages(laptop)
    ack = self.get_ack(controller_messages, "source-follow-play-1")
    phone_prepare = next(message for message in phone_messages if message["action"] == "playback.prepare")
    laptop_prepare = next(message for message in laptop_messages if message["action"] == "playback.prepare")
    prepare_id = ack["payload"]["prepareId"]

    self.assertEqual(ack["payload"]["targetClientIds"], ["phone-1", "laptop-1"])
    self.assertEqual(phone_prepare["payload"]["prepareId"], prepare_id)
    self.assertEqual(laptop_prepare["payload"]["prepareId"], prepare_id)
    self.assertEqual(laptop_prepare["payload"]["sessionId"], "root:laptop")
    self.assertEqual(laptop_prepare["payload"]["sourceClientId"], "phone-1")
    self.assertEqual(
      laptop_prepare["payload"]["timelineId"],
      "session:root:phone:client:phone-1",
    )

    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "source-follow-ready-phone-1",
        "payload": {
          "prepareId": prepare_id,
          "clientId": "phone-1",
          "ready": True,
          "positionMs": 0,
          "controlVersion": phone_prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )
    phone_ready_messages = self.get_messages(phone)
    ready_ack = self.get_ack(phone_ready_messages, "source-follow-ready-phone-1")
    self.assertEqual(ready_ack["payload"]["status"], "preparing")
    self.assertFalse(any(message["action"] == "player.play" for message in phone_ready_messages))

    get_state()._pending_prepares[prepare_id]["expiresAtMs"] = 0
    committed = _expire_prepare(prepare_id)
    self.assertEqual(committed["state"], "playing")

    phone_commit_messages = self.get_messages(phone)
    laptop_commit_messages = self.get_messages(laptop)
    phone_playback = next(message for message in phone_commit_messages if message["action"] == "playback.update")
    laptop_playback = next(message for message in laptop_commit_messages if message["action"] == "playback.update")
    phone_play = next(message for message in phone_commit_messages if message["action"] == "player.play")

    self.assertEqual(phone_playback["payload"]["sourceClientId"], "phone-1")
    self.assertEqual(laptop_playback["payload"]["sourceClientId"], "phone-1")
    self.assertEqual(
      laptop_playback["payload"]["effectiveAtServerMs"],
      phone_playback["payload"]["effectiveAtServerMs"],
    )
    self.assertEqual(
      phone_play["payload"]["effectiveAtServerMs"],
      phone_playback["payload"]["effectiveAtServerMs"],
    )

  def test_player_seek_uses_future_commit_for_effective_at_player(self):
    capabilities = {"effectiveAtPlayback": True}
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"], capabilities=capabilities)
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])
    self.get_messages(player)
    self.get_messages(controller)

    player.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "seek-source-state-1",
        "payload": {
          "sessionId": "sess-1",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 1000,
        },
      },
      namespace="/emo",
    )
    self.get_messages(player)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.seek",
        "requestId": "player-seek-future-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1", "positionMs": 45000},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    player_messages = self.get_messages(player)
    ack = self.get_ack(controller_messages, "player-seek-future-1")
    playback = next(message for message in player_messages if message["action"] == "playback.update")
    seek_command = next(message for message in player_messages if message["action"] == "player.seek")

    self.assertEqual(ack["payload"]["protocolPath"], "single_future")
    self.assertEqual(playback["payload"]["positionMs"], 45000)
    self.assertEqual(playback["payload"]["state"], "playing")
    self.assertIn("effectiveAtServerMs", playback["payload"])
    self.assertEqual(seek_command["payload"]["effectiveAtServerMs"], playback["payload"]["effectiveAtServerMs"])

  def test_source_control_rejects_stale_base_control_version(self):
    capabilities = {"effectiveAtPlayback": True}
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"], capabilities=capabilities)
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])
    self.get_messages(player)
    self.get_messages(controller)

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "source-base-queue-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    self.get_messages(player)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.play",
        "requestId": "source-base-play-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1"},
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(controller), "source-base-play-1")
    self.get_messages(player)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.seek",
        "requestId": "source-base-seek-stale-1",
        "targetClientId": "player-1",
        "payload": {
          "sessionId": "sess-1",
          "positionMs": 45000,
          "baseControlVersion": 0,
        },
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    player_messages = self.get_messages(player)
    error = next(message for message in controller_messages if message["action"] == "system.error")
    playback_state = get_state().get_playback_state("sess-1", "player-1")

    self.assertEqual(error["requestId"], "source-base-seek-stale-1")
    self.assertEqual(error["payload"]["code"], "conflict")
    self.assertEqual(
      error["payload"]["currentControlVersion"],
      playback_state["controlVersion"],
    )
    self.assertFalse(any(message["action"] == "player.seek" for message in player_messages))
    self.assertFalse(any(message["action"] == "playback.update" for message in player_messages))
    self.assertEqual(playback_state["positionMs"], 0)

  def test_player_seek_track_change_uses_prepare_ready_commit_for_prepare_player(self):
    capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"], capabilities=capabilities)
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])
    self.get_messages(player)
    self.get_messages(controller)

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "seek-track-queue-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    player.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "seek-track-source-1",
        "payload": {
          "sessionId": "sess-1",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 1000,
        },
      },
      namespace="/emo",
    )
    self.get_messages(player)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.seek",
        "requestId": "player-seek-track-change-1",
        "targetClientId": "player-1",
        "payload": {"sessionId": "sess-1", "queueIndex": 1, "positionMs": 12000},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    player_messages = self.get_messages(player)
    ack = self.get_ack(controller_messages, "player-seek-track-change-1")
    prepare = next(message for message in player_messages if message["action"] == "playback.prepare")

    self.assertTrue(ack["payload"]["preparing"])
    self.assertEqual(prepare["payload"]["trackId"], "song-2")
    self.assertEqual(prepare["payload"]["currentIndex"], 1)
    self.assertEqual(prepare["payload"]["positionMs"], 12000)
    self.assertFalse(any(message["action"] == "player.seek" for message in player_messages))

    player.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "player-seek-track-ready-1",
        "payload": {
          "prepareId": ack["payload"]["prepareId"],
          "clientId": "player-1",
          "ready": True,
          "positionMs": 12000,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )

    ready_messages = self.get_messages(player)
    self.get_ack(ready_messages, "player-seek-track-ready-1")
    playback = next(message for message in ready_messages if message["action"] == "playback.update")
    play_command = next(message for message in ready_messages if message["action"] == "player.play")

    self.assertEqual(playback["payload"]["trackId"], "song-2")
    self.assertEqual(playback["payload"]["currentIndex"], 1)
    self.assertEqual(playback["payload"]["positionMs"], 12000)
    self.assertIn("effectiveAtServerMs", playback["payload"])
    self.assertEqual(play_command["payload"]["effectiveAtServerMs"], playback["payload"]["effectiveAtServerMs"])

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

  def test_controller_sets_local_queue_for_payload_client_id(self):
    self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])

    controller.emit(
      "message",
      {
        "type": "state",
        "action": "queue.local.set",
        "requestId": "local-set-target-owner-1",
        "payload": {
          "sessionId": "sess-1",
          "clientId": "player-1",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 0,
          "positionMs": 500,
        },
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    local_queue = next(message for message in controller_messages if message["action"] == "queue.local.set")
    self.assertEqual(local_queue["payload"]["sourceClientId"], "player-1")
    self.assertEqual(local_queue["payload"]["positionMs"], 500)

    state = get_state()
    self.assertIsNotNone(state.get_local_queue("sess-1", "player-1"))
    self.assertIsNone(state.get_local_queue("sess-1", "controller-1"))

    persisted_queue = getLocalQueueState("sess-1", "player-1")
    self.assertIsNotNone(persisted_queue)
    self.assertEqual(persisted_queue["sourceClientId"], "player-1")
    self.assertIsNone(getLocalQueueState("sess-1", "controller-1"))

  def test_queue_local_get_restores_persisted_payload_with_server_fields(self):
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    self.get_messages(player)
    saveLocalQueueState("sess-1", "player-1", ["song-1"], 0, 250)

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.local.get",
        "requestId": "local-get-restore-1",
        "payload": {
          "sessionId": "sess-1",
          "clientId": "player-1",
        },
      },
      namespace="/emo",
    )

    messages = self.get_messages(player)
    ack = self.get_ack(messages, "local-get-restore-1")
    local_queue = next(message for message in messages if message["action"] == "queue.local.set")
    state_queue = get_state().get_local_queue("sess-1", "player-1")
    self.assertTrue(ack["payload"]["found"])
    self.assertEqual(local_queue["payload"]["sourceClientId"], "player-1")
    self.assertEqual(local_queue["payload"]["positionMs"], 250)
    self.assertIn("serverUpdatedAtMs", local_queue["payload"])
    self.assertIn("serverTimeMs", local_queue["payload"])
    self.assertNotIn("serverTimeMs", state_queue)

  def test_reject_local_queue_set_for_cross_user_payload_client_id(self):
    self.connect_device("bob", "B0b", "player-bob", "sess-bob", ["player"])
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])

    controller.emit(
      "message",
      {
        "type": "state",
        "action": "queue.local.set",
        "requestId": "local-set-cross-user-1",
        "payload": {
          "sessionId": "sess-bob",
          "clientId": "player-bob",
          "queueSongIds": ["song-1"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    error = next(message for message in controller_messages if message["action"] == "system.error")
    self.assertEqual(error["requestId"], "local-set-cross-user-1")
    self.assertEqual(error["payload"]["code"], "forbidden")

  def test_reject_local_queue_set_when_payload_client_id_is_outside_session(self):
    self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])

    controller.emit(
      "message",
      {
        "type": "state",
        "action": "queue.local.set",
        "requestId": "local-set-session-mismatch-1",
        "payload": {
          "sessionId": "other-session",
          "clientId": "player-1",
          "queueSongIds": ["song-1"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    error = next(message for message in controller_messages if message["action"] == "system.error")
    self.assertEqual(error["requestId"], "local-set-session-mismatch-1")
    self.assertEqual(error["payload"]["code"], "bad_request")

  def test_reject_local_queue_set_with_explicit_empty_owner_fields(self):
    self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])

    cases = (
      (
        "local-set-empty-session-1",
        {
          "sessionId": "",
          "clientId": "player-1",
          "queueSongIds": ["song-1"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      ),
      (
        "local-set-empty-client-1",
        {
          "sessionId": "sess-1",
          "clientId": "",
          "queueSongIds": ["song-1"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      ),
    )

    for request_id, payload in cases:
      with self.subTest(request_id=request_id):
        controller.emit(
          "message",
          {
            "type": "state",
            "action": "queue.local.set",
            "requestId": request_id,
            "payload": payload,
          },
          namespace="/emo",
        )

        controller_messages = self.get_messages(controller)
        error = next(message for message in controller_messages if message["action"] == "system.error")
        self.assertEqual(error["requestId"], request_id)
        self.assertEqual(error["payload"]["code"], "bad_request")

    state = get_state()
    self.assertIsNone(state.get_local_queue("sess-1", "player-1"))
    self.assertIsNone(state.get_local_queue("sess-1", "controller-1"))

  def test_reject_local_queue_set_with_explicit_invalid_target_client_id(self):
    self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])

    cases = (
      ("local-set-empty-target-1", ""),
      ("local-set-null-target-1", None),
    )

    for request_id, target_client_id in cases:
      with self.subTest(request_id=request_id):
        controller.emit(
          "message",
          {
            "type": "state",
            "action": "queue.local.set",
            "requestId": request_id,
            "targetClientId": target_client_id,
            "payload": {
              "sessionId": "sess-1",
              "clientId": "player-1",
              "queueSongIds": ["song-1"],
              "currentIndex": 0,
              "positionMs": 0,
            },
          },
          namespace="/emo",
        )

        controller_messages = self.get_messages(controller)
        error = next(message for message in controller_messages if message["action"] == "system.error")
        self.assertEqual(error["requestId"], request_id)
        self.assertEqual(error["payload"]["code"], "bad_request")

    self.assertIsNone(get_state().get_local_queue("sess-1", "player-1"))

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

  def test_session_subscribe_receives_session_queue_and_playback_snapshot(self):
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    observer = self.connect_device("alice", "Alic3", "observer-1", "observer-room", ["controller"])

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "queue-snapshot-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 1,
          "positionMs": 1000,
        },
      },
      namespace="/emo",
    )
    player.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "playback-snapshot-1",
        "payload": {
          "sessionId": "sess-1",
          "state": "playing",
          "trackId": "song-2",
          "positionMs": 1000,
        },
      },
      namespace="/emo",
    )
    self.get_messages(player)

    self.subscribe_session(observer, "sess-1", request_id="subscribe-snapshot-1")
    observer_messages = self.get_messages(observer)

    queue = next(message for message in observer_messages if message["action"] == "queue.session.sync")
    playback = next(message for message in observer_messages if message["action"] == "playback.update")
    self.assertEqual(queue["payload"]["sourceClientId"], "player-1")
    self.assertEqual(queue["payload"]["currentIndex"], 1)
    self.assertEqual(playback["payload"]["sourceClientId"], "player-1")
    self.assertEqual(playback["payload"]["trackId"], "song-2")

  def test_controller_syncs_session_queue_for_payload_client_id(self):
    self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    controller = self.connect_device("alice", "Alic3", "controller-1", "control-room", ["controller"])
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "queue-session-owner-1",
        "payload": {
          "sessionId": "sess-1",
          "clientId": "player-1",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )

    messages = self.get_messages(controller)
    ack = self.get_ack(messages, "queue-session-owner-1")
    queue = ack["payload"]["queue"]
    self.assertEqual(queue["sourceClientId"], "player-1")
    self.assertEqual(queue["authorityClientId"], "player-1")
    self.assertEqual(queue["timelineId"], "session:sess-1:client:player-1")
    self.assertEqual(get_state().get_queue("sess-1")["sourceClientId"], "player-1")

  def test_queue_session_sync_creates_playback_context(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"])
    self.get_messages(phone)
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "context-queue-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )

    messages = self.get_messages(phone)
    ack = self.get_ack(messages, "context-queue-1")
    queue = ack["payload"]["queue"]
    self.assertEqual(queue["playbackContextId"], "playback:alice:main")
    self.assertEqual(queue["deviceSessionId"], "root:phone")
    self.assertEqual(queue["authorityClientId"], "phone-1")
    self.assertEqual(queue["queueRevision"], 1)

    persisted = getPlaybackContextState("playback:alice:main")
    self.assertEqual(persisted["authorityClientId"], "phone-1")
    self.assertEqual(persisted["queueSongIds"], ["song-1", "song-2"])

  def test_non_authority_playback_update_is_device_feedback_only(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"])
    self.get_messages(phone)
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "context-queue-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "queueSongIds": ["song-1"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    self.get_messages(phone)
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "phone-playback-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 100,
        },
      },
      namespace="/emo",
    )
    phone_ack = self.get_ack(self.get_messages(phone), "phone-playback-1")
    self.assertTrue(phone_ack["payload"]["authoritative"])

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "pc-feedback-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:pc",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 999,
        },
      },
      namespace="/emo",
    )
    pc_ack = self.get_ack(self.get_messages(pc), "pc-feedback-1")
    self.assertFalse(pc_ack["payload"]["authoritative"])
    self.assertTrue(pc_ack["payload"]["deviceFeedback"])
    self.assertEqual(pc_ack["payload"]["currentAuthorityClientId"], "phone-1")

    context = get_state().get_playback_context("playback:alice:main")
    self.assertEqual(context["authorityClientId"], "phone-1")
    self.assertEqual(context["positionMs"], 100)
    feedback = getDevicePlaybackState("playback:alice:main", "pc-1")
    self.assertEqual(feedback["positionMs"], 999)
    self.assertFalse(feedback["isAuthority"])

  def test_handoff_complete_transfers_authority_and_can_switch_back(self):
    capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"], capabilities=capabilities)
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"], capabilities=capabilities)
    self.get_messages(phone)
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "context-queue-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 0,
          "positionMs": 30000,
        },
      },
      namespace="/emo",
    )
    self.get_messages(phone)
    self.get_messages(pc)
    context = get_state().get_playback_context("playback:alice:main")

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.start",
        "requestId": "handoff-phone-pc-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "sourceClientId": "phone-1",
          "targetClientId": "pc-1",
          "baseControlVersion": context["controlVersion"],
        },
      },
      namespace="/emo",
    )
    start_ack = self.get_ack(self.get_messages(phone), "handoff-phone-pc-1")
    prepare = next(message for message in self.get_messages(pc) if message["action"] == "playback.prepare")
    self.assertEqual(prepare["payload"]["playbackContextId"], "playback:alice:main")
    self.assertEqual(prepare["payload"]["deviceSessionId"], "root:pc")
    self.assertEqual(prepare["payload"]["authorityClientId"], "phone-1")
    self.assertEqual(get_state().get_playback_context("playback:alice:main")["authorityClientId"], "phone-1")

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "handoff-ready-pc-1",
        "payload": {
          "prepareId": prepare["payload"]["prepareId"],
          "ready": True,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )
    pc_ready_messages = self.get_messages(pc)
    self.get_ack(pc_ready_messages, "handoff-ready-pc-1")
    self.assertTrue(any(message["action"] == "player.play" for message in pc_ready_messages))

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.handoff.complete",
        "requestId": "handoff-complete-pc-1",
        "payload": {
          "handoffId": start_ack["payload"]["handoffId"],
          "playbackContextId": "playback:alice:main",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 30200,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )
    complete_ack = self.get_ack(self.get_messages(pc), "handoff-complete-pc-1")
    self.assertEqual(complete_ack["payload"]["authorityClientId"], "pc-1")
    context = get_state().get_playback_context("playback:alice:main")
    self.assertEqual(context["authorityClientId"], "pc-1")
    self.assertEqual(get_state().get_client("phone-1")["deviceSessionId"], "root:phone")
    self.assertEqual(get_state().get_client("pc-1")["deviceSessionId"], "root:pc")

    pc.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.start",
        "requestId": "handoff-pc-phone-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "sourceClientId": "pc-1",
          "targetClientId": "phone-1",
          "baseControlVersion": context["controlVersion"],
        },
      },
      namespace="/emo",
    )
    switch_back_ack = self.get_ack(self.get_messages(pc), "handoff-pc-phone-1")
    phone_prepare = next(message for message in self.get_messages(phone) if message["action"] == "playback.prepare")
    self.assertEqual(phone_prepare["payload"]["deviceSessionId"], "root:phone")

    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "handoff-ready-phone-1",
        "payload": {
          "prepareId": phone_prepare["payload"]["prepareId"],
          "ready": True,
          "controlVersion": phone_prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(phone), "handoff-ready-phone-1")
    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.handoff.complete",
        "requestId": "handoff-complete-phone-1",
        "payload": {
          "handoffId": switch_back_ack["payload"]["handoffId"],
          "playbackContextId": "playback:alice:main",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 31000,
          "controlVersion": phone_prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(phone), "handoff-complete-phone-1")
    self.assertEqual(get_state().get_playback_context("playback:alice:main")["authorityClientId"], "phone-1")

  def test_handoff_start_rejects_cross_user_playback_context(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    bob_controller = self.connect_device("bob", "B0b", "bob-controller-1", "root:bob-controller", ["controller"])
    bob_player = self.connect_device("bob", "B0b", "bob-player-1", "root:bob-player", ["player"])
    self.get_messages(phone)
    self.get_messages(bob_controller)
    self.get_messages(bob_player)

    self.sync_playback_context(phone, "context-queue-1")
    context = get_state().get_playback_context("playback:alice:main")

    bob_controller.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.start",
        "requestId": "cross-user-handoff-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "sourceClientId": "phone-1",
          "targetClientId": "bob-player-1",
          "baseControlVersion": context["controlVersion"],
        },
      },
      namespace="/emo",
    )

    error = self.get_error(self.get_messages(bob_controller), "cross-user-handoff-1")
    self.assertEqual(error["payload"]["code"], "forbidden")
    self.assertEqual(get_state().get_playback_context("playback:alice:main")["authorityClientId"], "phone-1")

  def test_handoff_complete_requires_ready_status(self):
    capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"], capabilities=capabilities)
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"], capabilities=capabilities)
    self.get_messages(phone)
    self.get_messages(pc)

    self.sync_playback_context(phone, "context-queue-1")
    context = get_state().get_playback_context("playback:alice:main")

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.start",
        "requestId": "handoff-phone-pc-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "sourceClientId": "phone-1",
          "targetClientId": "pc-1",
          "baseControlVersion": context["controlVersion"],
        },
      },
      namespace="/emo",
    )
    start_ack = self.get_ack(self.get_messages(phone), "handoff-phone-pc-1")
    self.get_messages(pc)

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.handoff.complete",
        "requestId": "handoff-complete-before-ready-1",
        "payload": {
          "handoffId": start_ack["payload"]["handoffId"],
          "playbackContextId": "playback:alice:main",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 100,
          "controlVersion": start_ack["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )

    error = self.get_error(self.get_messages(pc), "handoff-complete-before-ready-1")
    self.assertEqual(error["payload"]["code"], "conflict")
    self.assertEqual(get_state().get_playback_context("playback:alice:main")["authorityClientId"], "phone-1")

  def test_handoff_ready_without_complete_times_out_and_allows_new_handoff(self):
    capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"], capabilities=capabilities)
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"], capabilities=capabilities)
    self.get_messages(phone)
    self.get_messages(pc)

    self.sync_playback_context(phone, "context-queue-1")
    context = get_state().get_playback_context("playback:alice:main")

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.start",
        "requestId": "handoff-timeout-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "sourceClientId": "phone-1",
          "targetClientId": "pc-1",
          "baseControlVersion": context["controlVersion"],
        },
      },
      namespace="/emo",
    )
    start_ack = self.get_ack(self.get_messages(phone), "handoff-timeout-1")
    prepare = next(message for message in self.get_messages(pc) if message["action"] == "playback.prepare")

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "handoff-ready-timeout-1",
        "payload": {
          "prepareId": prepare["payload"]["prepareId"],
          "ready": True,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(pc), "handoff-ready-timeout-1")

    get_state().update_playback_handoff(
      start_ack["payload"]["handoffId"],
      complete_expires_at_ms=0,
    )
    expired = _expire_handoff_complete(start_ack["payload"]["handoffId"])
    self.assertEqual(expired["status"], "timed_out")
    self.assertEqual(get_state().get_playback_context("playback:alice:main")["authorityClientId"], "phone-1")

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.start",
        "requestId": "handoff-after-timeout-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "sourceClientId": "phone-1",
          "targetClientId": "pc-1",
          "baseControlVersion": context["controlVersion"],
        },
      },
      namespace="/emo",
    )
    retry_ack = self.get_ack(self.get_messages(phone), "handoff-after-timeout-1")
    self.assertEqual(retry_ack["payload"]["status"], "preparing")

  def test_duplicate_handoff_start_request_is_idempotent(self):
    capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"], capabilities=capabilities)
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"], capabilities=capabilities)
    self.get_messages(phone)
    self.get_messages(pc)

    self.sync_playback_context(phone, "context-queue-1")
    context = get_state().get_playback_context("playback:alice:main")
    start_message = {
      "type": "command",
      "action": "playback.handoff.start",
      "requestId": "handoff-idempotent-1",
      "payload": {
        "playbackContextId": "playback:alice:main",
        "sourceClientId": "phone-1",
        "targetClientId": "pc-1",
        "baseControlVersion": context["controlVersion"],
      },
    }

    phone.emit("message", start_message, namespace="/emo")
    first_ack = self.get_ack(self.get_messages(phone), "handoff-idempotent-1")
    first_prepare = next(message for message in self.get_messages(pc) if message["action"] == "playback.prepare")

    phone.emit("message", start_message, namespace="/emo")
    duplicate_ack = self.get_ack(self.get_messages(phone), "handoff-idempotent-1")
    duplicate_target_messages = self.get_messages(pc)

    self.assertTrue(duplicate_ack["payload"]["duplicate"])
    self.assertEqual(duplicate_ack["payload"]["handoffId"], first_ack["payload"]["handoffId"])
    self.assertEqual(duplicate_ack["payload"]["prepareId"], first_prepare["payload"]["prepareId"])
    self.assertFalse(any(message["action"] == "playback.prepare" for message in duplicate_target_messages))

  def test_new_playback_update_requires_existing_context(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    self.get_messages(phone)

    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "unknown-context-playback-1",
        "payload": {
          "playbackContextId": "playback:alice:missing",
          "deviceSessionId": "root:phone",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 100,
        },
      },
      namespace="/emo",
    )

    error = self.get_error(self.get_messages(phone), "unknown-context-playback-1")
    self.assertEqual(error["payload"]["code"], "not_found")
    self.assertIsNone(get_state().get_playback_context("playback:alice:missing"))

  def test_playback_update_broadcasts_to_session_subscriber(self):
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    observer = self.connect_device("alice", "Alic3", "observer-1", "observer-room", ["controller"])
    self.subscribe_session(observer, "sess-1", request_id="subscribe-playback-live-1")
    self.get_messages(observer)

    player.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "playback-live-1",
        "payload": {
          "sessionId": "sess-1",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 2500,
        },
      },
      namespace="/emo",
    )

    observer_messages = self.get_messages(observer)
    playback = next(message for message in observer_messages if message["action"] == "playback.update")
    self.assertEqual(playback["payload"]["sourceClientId"], "player-1")
    self.assertEqual(playback["payload"]["positionMs"], 2500)

  def test_session_queue_update_broadcasts_to_session_subscriber(self):
    controller = self.connect_device("alice", "Alic3", "controller-1", "sess-1", ["controller"])
    observer = self.connect_device("alice", "Alic3", "observer-1", "observer-room", ["controller"])
    self.subscribe_session(observer, "sess-1", request_id="subscribe-queue-live-1")
    self.get_messages(observer)

    controller.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "queue-live-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1", "song-2", "song-3"],
          "currentIndex": 2,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )

    observer_messages = self.get_messages(observer)
    queue = next(message for message in observer_messages if message["action"] == "queue.session.sync")
    self.assertEqual(queue["payload"]["sourceClientId"], "controller-1")
    self.assertEqual(queue["payload"]["queueSongIds"], ["song-1", "song-2", "song-3"])

  def test_broadcast_start_selected_clients(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"])
    self.get_messages(phone)
    self.get_messages(pc)

    self.start_broadcast(phone, ["phone-1", "pc-1"], request_id="broadcast-start-selected-1")

    phone_messages = self.get_messages(phone)
    pc_messages = self.get_messages(pc)
    ack = self.get_ack(phone_messages, "broadcast-start-selected-1")
    broadcast_id = ack["payload"]["broadcastId"]

    self.assertEqual(ack["payload"]["participants"], ["phone-1", "pc-1"])
    self.assertEqual(ack["payload"]["broadcast"]["version"], 1)
    self.assertEqual(ack["payload"]["broadcast"]["trackId"], "song-1")

    pc_start = next(message for message in pc_messages if message["action"] == "broadcast.start")
    self.assertEqual(pc_start["sourceClientId"], "phone-1")
    self.assertEqual(pc_start["targetClientId"], "pc-1")
    self.assertEqual(pc_start["payload"]["broadcastId"], broadcast_id)
    self.assertEqual(pc_start["payload"]["state"], "playing")
    self.assertTrue(pc_start["payload"]["autoPlay"])

    state = get_state()
    self.assertEqual(state.get_active_broadcast_for_client("phone-1"), broadcast_id)
    self.assertEqual(state.get_active_broadcast_for_client("pc-1"), broadcast_id)

  def test_broadcast_start_uses_single_future_for_effective_at_clients(self):
    capabilities = {"effectiveAtPlayback": True}
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"], capabilities=capabilities)
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"], capabilities=capabilities)
    self.get_messages(phone)
    self.get_messages(pc)

    self.start_broadcast(phone, ["phone-1", "pc-1"], request_id="broadcast-start-future-1")

    phone_messages = self.get_messages(phone)
    pc_messages = self.get_messages(pc)
    ack = self.get_ack(phone_messages, "broadcast-start-future-1")
    pc_start = next(message for message in pc_messages if message["action"] == "broadcast.start")

    self.assertEqual(ack["payload"]["protocolPath"], "single_future")
    self.assertIn("effectiveAtServerMs", ack["payload"]["broadcast"])
    self.assertGreater(
      ack["payload"]["broadcast"]["effectiveAtServerMs"],
      ack["payload"]["broadcast"]["serverUpdatedAtMs"],
    )
    self.assertEqual(
      pc_start["payload"]["effectiveAtServerMs"],
      ack["payload"]["broadcast"]["effectiveAtServerMs"],
    )
    self.assertFalse(any(message["action"] == "playback.prepare" for message in pc_messages))

  def test_broadcast_start_with_legacy_participant_uses_whole_control_legacy_fallback(self):
    capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"], capabilities=capabilities)
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"])
    self.get_messages(phone)
    self.get_messages(pc)

    self.start_broadcast(phone, ["phone-1", "pc-1"], request_id="broadcast-start-mixed-legacy-1")

    phone_messages = self.get_messages(phone)
    pc_messages = self.get_messages(pc)
    ack = self.get_ack(phone_messages, "broadcast-start-mixed-legacy-1")
    pc_start = next(message for message in pc_messages if message["action"] == "broadcast.start")

    self.assertEqual(ack["payload"]["protocolPath"], "legacy")
    self.assertNotIn("effectiveAtServerMs", ack["payload"]["broadcast"])
    self.assertNotIn("effectiveAtServerMs", pc_start["payload"])
    self.assertFalse(any(message["action"] == "playback.prepare" for message in phone_messages))
    self.assertFalse(any(message["action"] == "playback.prepare" for message in pc_messages))

  def test_broadcast_start_uses_prepare_ready_commit_for_prepare_clients(self):
    capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"], capabilities=capabilities)
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"], capabilities=capabilities)
    self.get_messages(phone)
    self.get_messages(pc)

    self.start_broadcast(phone, ["phone-1", "pc-1"], request_id="broadcast-start-prepare-1")

    phone_messages = self.get_messages(phone)
    pc_messages = self.get_messages(pc)
    ack = self.get_ack(phone_messages, "broadcast-start-prepare-1")
    phone_prepare = next(message for message in phone_messages if message["action"] == "playback.prepare")
    pc_prepare = next(message for message in pc_messages if message["action"] == "playback.prepare")
    prepare_id = ack["payload"]["prepareId"]

    self.assertTrue(ack["payload"]["preparing"])
    self.assertEqual(phone_prepare["payload"]["prepareId"], prepare_id)
    self.assertEqual(pc_prepare["payload"]["prepareId"], prepare_id)
    self.assertFalse(any(message["action"] == "broadcast.start" for message in pc_messages))

    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "ready-phone-1",
        "payload": {
          "prepareId": prepare_id,
          "clientId": "phone-1",
          "ready": True,
          "positionMs": 0,
          "controlVersion": phone_prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(phone), "ready-phone-1")
    self.assertIsNone(get_state().get_broadcast(ack["payload"]["broadcastId"]))

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "ready-pc-1",
        "payload": {
          "prepareId": prepare_id,
          "clientId": "pc-1",
          "ready": True,
          "positionMs": 0,
          "controlVersion": pc_prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )

    pc_ready_messages = self.get_messages(pc)
    phone_commit_messages = self.get_messages(phone)
    self.get_ack(pc_ready_messages, "ready-pc-1")
    pc_start = next(message for message in pc_ready_messages if message["action"] == "broadcast.start")
    phone_start = next(message for message in phone_commit_messages if message["action"] == "broadcast.start")
    broadcast = get_state().get_broadcast(ack["payload"]["broadcastId"])

    self.assertIsNotNone(broadcast)
    self.assertEqual(pc_start["payload"]["protocolPath"], "two_phase")
    self.assertEqual(phone_start["payload"]["effectiveAtServerMs"], pc_start["payload"]["effectiveAtServerMs"])
    self.assertEqual(broadcast["effectiveAtServerMs"], pc_start["payload"]["effectiveAtServerMs"])

  def test_broadcast_prepare_rejects_stale_base_control_version_before_prepare(self):
    capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"], capabilities=capabilities)
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"], capabilities=capabilities)
    self.get_messages(phone)
    self.get_messages(pc)

    self.start_broadcast(
      phone,
      ["phone-1", "pc-1"],
      request_id="broadcast-start-stale-prepare-base-1",
      autoPlay=False,
    )
    start_ack = self.get_ack(self.get_messages(phone), "broadcast-start-stale-prepare-base-1")
    broadcast_id = start_ack["payload"]["broadcastId"]
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.playItem",
        "requestId": "broadcast-play-item-stale-prepare-base-1",
        "payload": {
          "broadcastId": broadcast_id,
          "queueIndex": 1,
          "positionMs": 0,
          "baseControlVersion": 0,
        },
      },
      namespace="/emo",
    )

    phone_messages = self.get_messages(phone)
    pc_messages = self.get_messages(pc)
    error = next(message for message in phone_messages if message["action"] == "system.error")

    self.assertEqual(error["requestId"], "broadcast-play-item-stale-prepare-base-1")
    self.assertEqual(error["payload"]["code"], "conflict")
    self.assertEqual(error["payload"]["currentControlVersion"], 1)
    self.assertFalse(any(message["action"] == "playback.prepare" for message in phone_messages))
    self.assertFalse(any(message["action"] == "playback.prepare" for message in pc_messages))

  def test_device_register_restores_active_broadcast_status(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"])
    self.get_messages(phone)
    self.get_messages(pc)
    self.start_broadcast(phone, ["pc-1"], request_id="broadcast-start-restore-1")
    broadcast_id = self.get_ack(
      self.get_messages(phone),
      "broadcast-start-restore-1",
    )["payload"]["broadcastId"]
    self.get_messages(pc)

    pc.disconnect(namespace="/emo")
    self.clients.remove(pc)
    reconnected = self.connect_authenticated_client("alice", "Alic3", "auth-pc-restore-1")
    restore_messages = self.register_device(
      reconnected,
      "register-pc-restore-1",
      {
        "clientId": "pc-1",
        "deviceName": "pc-1",
        "roles": ["player"],
        "sessionId": "root:pc",
      },
    )

    status = next(
      message
      for message in restore_messages
      if message["action"] == "broadcast.status"
    )
    self.assertEqual(status["payload"]["broadcast"]["broadcastId"], broadcast_id)
    self.assertEqual(status["payload"]["broadcast"]["trackId"], "song-1")
    participant = status["payload"]["participantStates"][0]
    self.assertEqual(participant["clientId"], "pc-1")
    self.assertTrue(participant["online"])

  def test_broadcast_start_all_online_players_excludes_controller(self):
    self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"])
    controller = self.connect_device("alice", "Alic3", "web-control-1", "web-control:alice", ["controller"])
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.start",
        "requestId": "broadcast-start-all-1",
        "payload": {
          "targetMode": "allOnlinePlayers",
          "queueSongIds": ["song-1"],
          "currentIndex": 0,
          "positionMs": 0,
          "autoPlay": True,
        },
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    ack = self.get_ack(controller_messages, "broadcast-start-all-1")
    self.assertEqual(ack["payload"]["participants"], ["phone-1", "pc-1"])
    self.assertNotIn("web-control-1", ack["payload"]["participants"])

  def test_broadcast_start_skips_offline_and_non_player_targets(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    self.connect_device("alice", "Alic3", "controller-target", "root:controller", ["controller"])
    self.get_messages(phone)

    self.start_broadcast(
      phone,
      ["phone-1", "offline-player", "controller-target"],
      request_id="broadcast-start-skipped-1",
      queueSongIds=["song-1"],
    )

    phone_messages = self.get_messages(phone)
    ack = self.get_ack(phone_messages, "broadcast-start-skipped-1")
    self.assertEqual(ack["payload"]["participants"], ["phone-1"])
    self.assertEqual(ack["payload"]["skippedClientIds"], ["offline-player", "controller-target"])

  def test_broadcast_start_empty_queue_creates_stopped_state(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    self.get_messages(phone)

    self.start_broadcast(
      phone,
      ["phone-1"],
      request_id="broadcast-start-empty-1",
      queueSongIds=[],
      currentIndex=0,
      positionMs=0,
      autoPlay=True,
    )

    phone_messages = self.get_messages(phone)
    ack = self.get_ack(phone_messages, "broadcast-start-empty-1")
    start = next(message for message in phone_messages if message["action"] == "broadcast.start")
    self.assertEqual(ack["payload"]["broadcast"]["state"], "stopped")
    self.assertIsNone(ack["payload"]["broadcast"]["trackId"])
    self.assertFalse(start["payload"]["autoPlay"])
    self.assertEqual(start["payload"]["state"], "stopped")

  def test_broadcast_empty_queue_can_sync_queue_before_stop(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    self.get_messages(phone)

    self.start_broadcast(
      phone,
      ["phone-1"],
      request_id="broadcast-start-empty-sync-1",
      queueSongIds=[],
      currentIndex=0,
      positionMs=0,
      autoPlay=True,
    )
    ack = self.get_ack(self.get_messages(phone), "broadcast-start-empty-sync-1")
    broadcast_id = ack["payload"]["broadcastId"]

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "broadcast.queue.sync",
        "requestId": "broadcast-empty-sync-1",
        "payload": {
          "broadcastId": broadcast_id,
          "queueSongIds": ["song-1"],
          "currentIndex": 0,
          "positionMs": 0,
          "baseVersion": 1,
        },
      },
      namespace="/emo",
    )

    sync_ack = self.get_ack(self.get_messages(phone), "broadcast-empty-sync-1")
    self.assertEqual(sync_ack["payload"]["broadcast"]["version"], 2)
    self.assertEqual(sync_ack["payload"]["broadcast"]["trackId"], "song-1")
    self.assertEqual(sync_ack["payload"]["broadcast"]["state"], "stopped")

  def test_broadcast_play_rejects_empty_queue(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    self.get_messages(phone)

    self.start_broadcast(
      phone,
      ["phone-1"],
      request_id="broadcast-start-empty-play-1",
      queueSongIds=[],
      currentIndex=0,
      positionMs=0,
      autoPlay=True,
    )
    ack = self.get_ack(self.get_messages(phone), "broadcast-start-empty-play-1")

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.play",
        "requestId": "broadcast-play-empty-1",
        "payload": {"broadcastId": ack["payload"]["broadcastId"]},
      },
      namespace="/emo",
    )

    messages = self.get_messages(phone)
    error = next(message for message in messages if message["action"] == "system.error")
    self.assertEqual(error["requestId"], "broadcast-play-empty-1")
    self.assertEqual(error["payload"]["code"], "bad_request")
    self.assertEqual(
      get_state().get_broadcast(ack["payload"]["broadcastId"])["state"],
      "stopped",
    )

  def test_broadcast_start_rejects_cross_user_target(self):
    self.connect_device("bob", "B0b", "bob-player", "bob:player", ["player"])
    controller = self.connect_device("alice", "Alic3", "controller-1", "alice:controller", ["controller"])

    self.start_broadcast(
      controller,
      ["bob-player"],
      request_id="broadcast-cross-user-1",
      queueSongIds=["song-1"],
    )

    controller_messages = self.get_messages(controller)
    error = next(message for message in controller_messages if message["action"] == "system.error")
    self.assertEqual(error["requestId"], "broadcast-cross-user-1")
    self.assertEqual(error["payload"]["code"], "forbidden")

  def test_controller_can_start_broadcast_without_becoming_participant_and_control_queue(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"])
    controller = self.connect_device("alice", "Alic3", "web-control-1", "web-control:alice", ["controller"])
    self.get_messages(phone)
    self.get_messages(pc)
    self.get_messages(controller)

    self.start_broadcast(
      controller,
      ["phone-1", "pc-1"],
      request_id="broadcast-controller-start-1",
      queueSongIds=["song-1", "song-2"],
    )

    controller_messages = self.get_messages(controller)
    ack = self.get_ack(controller_messages, "broadcast-controller-start-1")
    broadcast_id = ack["payload"]["broadcastId"]
    self.assertEqual(ack["payload"]["participants"], ["phone-1", "pc-1"])
    self.assertFalse(any(message["action"] == "broadcast.start" for message in controller_messages))
    self.assertIsNone(get_state().get_active_broadcast_for_client("web-control-1"))

    self.get_messages(phone)
    self.get_messages(pc)
    controller.emit(
      "message",
      {
        "type": "state",
        "action": "broadcast.queue.sync",
        "requestId": "broadcast-controller-queue-1",
        "payload": {
          "broadcastId": broadcast_id,
          "queueSongIds": ["song-3", "song-4"],
          "currentIndex": 1,
          "positionMs": 0,
          "baseVersion": 1,
        },
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    phone_messages = self.get_messages(phone)
    queue_ack = self.get_ack(controller_messages, "broadcast-controller-queue-1")
    self.assertEqual(queue_ack["payload"]["broadcast"]["version"], 2)
    self.assertEqual(queue_ack["payload"]["broadcast"]["trackId"], "song-4")
    phone_queue = next(message for message in phone_messages if message["action"] == "broadcast.queue.sync")
    self.assertEqual(phone_queue["payload"]["updatedByClientId"], "web-control-1")
    self.assertEqual(phone_queue["payload"]["queueSongIds"], ["song-3", "song-4"])

  def test_broadcast_queue_version_conflict(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    self.get_messages(phone)
    self.start_broadcast(phone, ["phone-1"], request_id="broadcast-start-conflict-1")
    ack = self.get_ack(self.get_messages(phone), "broadcast-start-conflict-1")

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "broadcast.queue.sync",
        "requestId": "broadcast-conflict-1",
        "payload": {
          "broadcastId": ack["payload"]["broadcastId"],
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 0,
          "positionMs": 0,
          "baseVersion": 0,
        },
      },
      namespace="/emo",
    )

    messages = self.get_messages(phone)
    error = next(message for message in messages if message["action"] == "system.error")
    self.assertEqual(error["requestId"], "broadcast-conflict-1")
    self.assertEqual(error["payload"]["code"], "conflict")
    self.assertEqual(error["payload"]["currentVersion"], 1)
    self.assertEqual(get_state().get_broadcast(ack["payload"]["broadcastId"])["version"], 1)

  def test_broadcast_status_returns_broadcast_and_participant_states(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    controller = self.connect_device("alice", "Alic3", "controller-1", "root:controller", ["controller"])
    self.get_messages(phone)
    self.get_messages(controller)
    self.start_broadcast(phone, ["phone-1"], request_id="broadcast-start-status-1")
    ack = self.get_ack(self.get_messages(phone), "broadcast-start-status-1")
    broadcast_id = ack["payload"]["broadcastId"]

    controller.emit(
      "message",
      {
        "type": "state",
        "action": "broadcast.status",
        "requestId": "broadcast-status-1",
        "payload": {"broadcastId": broadcast_id},
      },
      namespace="/emo",
    )

    controller_messages = self.get_messages(controller)
    status_ack = self.get_ack(controller_messages, "broadcast-status-1")
    status = next(message for message in controller_messages if message["action"] == "broadcast.status")
    self.assertEqual(status_ack["payload"]["broadcast"]["broadcastId"], broadcast_id)
    self.assertEqual(status["payload"]["broadcast"]["broadcastId"], broadcast_id)
    self.assertEqual(status["payload"]["participantStates"][0]["clientId"], "phone-1")
    self.assertTrue(status["payload"]["participantStates"][0]["online"])

  def test_broadcast_play_item_broadcasts_command(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"])
    self.get_messages(phone)
    self.get_messages(pc)
    self.start_broadcast(phone, ["phone-1", "pc-1"], request_id="broadcast-start-play-item-1")
    ack = self.get_ack(self.get_messages(phone), "broadcast-start-play-item-1")
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.playItem",
        "requestId": "broadcast-play-item-1",
        "payload": {
          "broadcastId": ack["payload"]["broadcastId"],
          "queueIndex": 1,
          "positionMs": 0,
          "baseVersion": 1,
        },
      },
      namespace="/emo",
    )

    phone_messages = self.get_messages(phone)
    pc_messages = self.get_messages(pc)
    play_ack = self.get_ack(phone_messages, "broadcast-play-item-1")
    self.assertEqual(play_ack["payload"]["broadcast"]["currentIndex"], 1)
    self.assertEqual(play_ack["payload"]["broadcast"]["trackId"], "song-2")
    self.assertEqual(play_ack["payload"]["broadcast"]["version"], 2)
    pc_command = next(message for message in pc_messages if message["action"] == "broadcast.playItem")
    self.assertEqual(pc_command["payload"]["queueIndex"], 1)
    self.assertEqual(pc_command["payload"]["trackId"], "song-2")
    self.assertEqual(pc_command["payload"]["state"], "playing")

  def test_broadcast_seek_and_pause_broadcast_commands(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"])
    self.get_messages(phone)
    self.get_messages(pc)
    self.start_broadcast(phone, ["phone-1", "pc-1"], request_id="broadcast-start-seek-1")
    ack = self.get_ack(self.get_messages(phone), "broadcast-start-seek-1")
    broadcast_id = ack["payload"]["broadcastId"]
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.seek",
        "requestId": "broadcast-seek-1",
        "payload": {"broadcastId": broadcast_id, "positionMs": 45000},
      },
      namespace="/emo",
    )

    pc_messages = self.get_messages(pc)
    seek = next(message for message in pc_messages if message["action"] == "broadcast.seek")
    self.assertEqual(seek["payload"]["positionMs"], 45000)
    self.assertEqual(seek["payload"]["version"], 2)

    self.get_messages(phone)
    self.get_messages(pc)
    phone.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.pause",
        "requestId": "broadcast-pause-1",
        "payload": {"broadcastId": broadcast_id, "positionMs": 46000},
      },
      namespace="/emo",
    )

    pc_messages = self.get_messages(pc)
    pause = next(message for message in pc_messages if message["action"] == "broadcast.pause")
    self.assertEqual(pause["payload"]["state"], "paused")
    self.assertEqual(pause["payload"]["positionMs"], 46000)
    self.assertEqual(pause["payload"]["version"], 3)

  def test_broadcast_playback_update_records_participant_state(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"])
    self.get_messages(phone)
    self.get_messages(pc)
    self.start_broadcast(phone, ["phone-1", "pc-1"], request_id="broadcast-start-feedback-1")
    ack = self.get_ack(self.get_messages(phone), "broadcast-start-feedback-1")
    broadcast_id = ack["payload"]["broadcastId"]

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "broadcast-feedback-1",
        "payload": {
          "sessionId": "root:pc",
          "mode": "broadcast",
          "broadcastId": broadcast_id,
          "state": "playing",
          "trackId": "song-2",
          "positionMs": 12000,
          "syncDriftMs": -200,
        },
      },
      namespace="/emo",
    )

    pc_messages = self.get_messages(pc)
    self.get_ack(pc_messages, "broadcast-feedback-1")
    participant_state = get_state().get_broadcast_participant_state(broadcast_id, "pc-1")
    self.assertEqual(participant_state["state"], "playing")
    self.assertEqual(participant_state["trackId"], "song-2")
    self.assertEqual(participant_state["positionMs"], 12000)
    self.assertEqual(participant_state["syncDriftMs"], -200)
    self.assertTrue(participant_state["online"])
    self.assertIsNone(get_state().get_playback_state("root:pc", "pc-1"))

  def test_broadcast_stale_participant_is_marked_offline(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"])
    self.get_messages(phone)
    self.get_messages(pc)
    self.start_broadcast(phone, ["phone-1", "pc-1"], request_id="broadcast-start-stale-1")
    ack = self.get_ack(self.get_messages(phone), "broadcast-start-stale-1")
    broadcast_id = ack["payload"]["broadcastId"]

    state = get_state()
    state._clients["pc-1"]["lastSeenAt"] = 1
    state.prune_stale_clients(stale_after_seconds=5, now=10)

    participant_state = state.get_broadcast_participant_state(broadcast_id, "pc-1")
    self.assertFalse(participant_state["online"])

  def test_broadcast_stop_clears_active_broadcast_and_notifies_participants(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"])
    self.get_messages(phone)
    self.get_messages(pc)
    self.start_broadcast(phone, ["phone-1", "pc-1"], request_id="broadcast-start-stop-1")
    ack = self.get_ack(self.get_messages(phone), "broadcast-start-stop-1")
    broadcast_id = ack["payload"]["broadcastId"]
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.stop",
        "requestId": "broadcast-stop-1",
        "payload": {"broadcastId": broadcast_id},
      },
      namespace="/emo",
    )

    phone_messages = self.get_messages(phone)
    pc_messages = self.get_messages(pc)
    stop_ack = self.get_ack(phone_messages, "broadcast-stop-1")
    self.assertEqual(stop_ack["payload"]["broadcast"]["state"], "stopped")
    pc_stop = next(message for message in pc_messages if message["action"] == "broadcast.stop")
    self.assertEqual(pc_stop["payload"]["state"], "stopped")
    self.assertIsNone(get_state().get_active_broadcast_for_client("phone-1"))
    self.assertIsNone(get_state().get_active_broadcast_for_client("pc-1"))

  def test_broadcast_control_after_stop_is_rejected(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"])
    self.get_messages(phone)
    self.get_messages(pc)
    self.start_broadcast(phone, ["phone-1", "pc-1"], request_id="broadcast-start-stop-control-1")
    ack = self.get_ack(self.get_messages(phone), "broadcast-start-stop-control-1")
    broadcast_id = ack["payload"]["broadcastId"]
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.stop",
        "requestId": "broadcast-stop-control-1",
        "payload": {"broadcastId": broadcast_id},
      },
      namespace="/emo",
    )
    stop_ack = self.get_ack(self.get_messages(phone), "broadcast-stop-control-1")
    stopped_version = stop_ack["payload"]["broadcast"]["version"]
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.playItem",
        "requestId": "broadcast-play-item-after-stop-1",
        "payload": {
          "broadcastId": broadcast_id,
          "queueIndex": 1,
          "positionMs": 0,
          "baseVersion": stopped_version,
        },
      },
      namespace="/emo",
    )

    phone_messages = self.get_messages(phone)
    pc_messages = self.get_messages(pc)
    error = next(message for message in phone_messages if message["action"] == "system.error")
    self.assertEqual(error["requestId"], "broadcast-play-item-after-stop-1")
    self.assertEqual(error["payload"]["code"], "bad_request")
    self.assertFalse(any(message["action"] == "broadcast.playItem" for message in pc_messages))

    broadcast = get_state().get_broadcast(broadcast_id)
    self.assertEqual(broadcast["state"], "stopped")
    self.assertEqual(broadcast["version"], stopped_version)

  def test_broadcast_playback_update_after_stop_is_rejected(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"])
    self.get_messages(phone)
    self.get_messages(pc)
    self.start_broadcast(phone, ["phone-1", "pc-1"], request_id="broadcast-start-stop-feedback-1")
    ack = self.get_ack(self.get_messages(phone), "broadcast-start-stop-feedback-1")
    broadcast_id = ack["payload"]["broadcastId"]
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.stop",
        "requestId": "broadcast-stop-feedback-1",
        "payload": {"broadcastId": broadcast_id},
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(phone), "broadcast-stop-feedback-1")
    self.get_messages(pc)

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "broadcast-feedback-after-stop-1",
        "payload": {
          "sessionId": "root:pc",
          "mode": "broadcast",
          "broadcastId": broadcast_id,
          "state": "playing",
          "trackId": "song-2",
          "positionMs": 88000,
        },
      },
      namespace="/emo",
    )

    pc_messages = self.get_messages(pc)
    error = next(message for message in pc_messages if message["action"] == "system.error")
    self.assertEqual(error["requestId"], "broadcast-feedback-after-stop-1")
    self.assertEqual(error["payload"]["code"], "forbidden")

    participant_state = get_state().get_broadcast_participant_state(broadcast_id, "pc-1")
    self.assertNotEqual(participant_state["positionMs"], 88000)

  def test_playback_update_adds_authoritative_timeline_fields_and_rejects_stale_seq(self):
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    self.get_messages(player)

    player.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "playback-seq-1",
        "payload": {
          "sessionId": "sess-1",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 2500,
          "updatedAt": 1,
          "clientInstanceId": "boot-a",
          "clientSeq": 2,
        },
      },
      namespace="/emo",
    )

    messages = self.get_messages(player)
    self.get_ack(messages, "playback-seq-1")
    playback = next(message for message in messages if message["action"] == "playback.update")
    self.assertEqual(playback["payload"]["timelineId"], "session:sess-1:client:player-1")
    self.assertEqual(playback["payload"]["authorityClientId"], "player-1")
    self.assertEqual(playback["payload"]["version"], 1)
    self.assertEqual(playback["payload"]["epoch"], 1)
    self.assertEqual(playback["payload"]["clientInstanceId"], "boot-a")
    self.assertEqual(playback["payload"]["clientSeq"], 2)
    self.assertEqual(playback["payload"]["followDelayMs"], 0)
    self.assertIn("serverUpdatedAtMs", playback["payload"])
    self.assertNotEqual(playback["payload"]["updatedAt"], 1)

    player.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "playback-seq-stale-1",
        "payload": {
          "sessionId": "sess-1",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 3000,
          "clientInstanceId": "boot-a",
          "clientSeq": 2,
        },
      },
      namespace="/emo",
    )

    stale_messages = self.get_messages(player)
    error = next(message for message in stale_messages if message["action"] == "system.error")
    self.assertEqual(error["requestId"], "playback-seq-stale-1")
    self.assertEqual(error["payload"]["code"], "stale_client_seq")

    player.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "playback-seq-restart-1",
        "payload": {
          "sessionId": "sess-1",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 3500,
          "clientInstanceId": "boot-b",
          "clientSeq": 1,
        },
      },
      namespace="/emo",
    )

    restart_messages = self.get_messages(player)
    self.get_ack(restart_messages, "playback-seq-restart-1")
    restarted = next(message for message in restart_messages if message["action"] == "playback.update")
    self.assertEqual(restarted["payload"]["version"], 2)
    self.assertEqual(restarted["payload"]["clientInstanceId"], "boot-b")
    self.assertEqual(restarted["payload"]["clientSeq"], 1)

  def test_queue_session_sync_uses_base_queue_revision_not_playback_version(self):
    player = self.connect_device("alice", "Alic3", "player-1", "sess-1", ["player"])
    self.get_messages(player)

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "queue-rev-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    first_messages = self.get_messages(player)
    first_queue = next(message for message in first_messages if message["action"] == "queue.session.sync")
    self.assertEqual(first_queue["payload"]["queueRevision"], 1)

    for seq in range(1, 4):
      player.emit(
        "message",
        {
          "type": "event",
          "action": "playback.update",
          "requestId": f"playback-heartbeat-{seq}",
          "payload": {
            "sessionId": "sess-1",
            "state": "playing",
            "trackId": "song-1",
            "positionMs": seq * 1000,
            "clientInstanceId": "boot-a",
            "clientSeq": seq,
          },
        },
        namespace="/emo",
      )
      self.get_messages(player)

    playback = get_state().get_playback_state("sess-1", "player-1")
    self.assertGreater(playback["version"], first_queue["payload"]["version"])

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "queue-rev-2",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1", "song-2", "song-3"],
          "currentIndex": 0,
          "positionMs": 0,
          "baseQueueRevision": 1,
        },
      },
      namespace="/emo",
    )
    updated_messages = self.get_messages(player)
    updated_ack = self.get_ack(updated_messages, "queue-rev-2")
    self.assertEqual(updated_ack["payload"]["queue"]["queueRevision"], 2)

    player.emit(
      "message",
      {
        "type": "state",
        "action": "queue.session.sync",
        "requestId": "queue-rev-stale-1",
        "payload": {
          "sessionId": "sess-1",
          "queueSongIds": ["song-1"],
          "currentIndex": 0,
          "positionMs": 0,
          "baseQueueRevision": 1,
        },
      },
      namespace="/emo",
    )
    conflict_messages = self.get_messages(player)
    error = next(message for message in conflict_messages if message["action"] == "system.error")
    self.assertEqual(error["requestId"], "queue-rev-stale-1")
    self.assertEqual(error["payload"]["code"], "conflict")
    self.assertEqual(error["payload"]["currentQueueRevision"], 2)

  def test_follow_start_records_relationship_and_blocks_source_control_without_payload_flag(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    laptop = self.connect_device("alice", "Alic3", "laptop-1", "root:laptop", ["player"])
    self.get_messages(phone)
    self.get_messages(laptop)

    laptop.emit(
      "message",
      {
        "type": "state",
        "action": "follow.start",
        "requestId": "follow-start-1",
        "payload": {
          "sourceClientId": "phone-1",
          "sourceSessionId": "root:phone",
        },
      },
      namespace="/emo",
    )

    follow_messages = self.get_messages(laptop)
    follow_ack = self.get_ack(follow_messages, "follow-start-1")
    self.assertEqual(follow_ack["payload"]["relationship"]["sourceClientId"], "phone-1")
    self.assertEqual(
      get_state().get_follow_relationship("laptop-1")["sourceSessionId"],
      "root:phone",
    )

    laptop.emit(
      "message",
      {
        "type": "command",
        "action": "player.seek",
        "requestId": "follow-seek-source-1",
        "targetClientId": "phone-1",
        "payload": {"positionMs": 90000},
      },
      namespace="/emo",
    )

    laptop_messages = self.get_messages(laptop)
    phone_messages = self.get_messages(phone)
    error = next(message for message in laptop_messages if message["action"] == "system.error")
    self.assertEqual(error["requestId"], "follow-seek-source-1")
    self.assertEqual(error["payload"]["code"], "follow_control_forbidden")
    self.assertFalse(any(message["action"] == "player.seek" for message in phone_messages))

  def test_follow_playback_feedback_does_not_overwrite_source_timeline(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    laptop = self.connect_device("alice", "Alic3", "laptop-1", "root:laptop", ["player"])
    self.get_messages(phone)
    self.get_messages(laptop)

    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "source-playback-1",
        "payload": {
          "sessionId": "root:phone",
          "state": "playing",
          "trackId": "song-source",
          "positionMs": 60000,
        },
      },
      namespace="/emo",
    )
    self.get_messages(phone)

    laptop.emit(
      "message",
      {
        "type": "state",
        "action": "follow.start",
        "requestId": "follow-start-feedback-1",
        "payload": {
          "sourceClientId": "phone-1",
          "sourceSessionId": "root:phone",
        },
      },
      namespace="/emo",
    )
    self.get_messages(laptop)

    laptop.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "follow-feedback-1",
        "payload": {
          "sessionId": "root:laptop",
          "mode": "follow",
          "followSourceClientId": "phone-1",
          "state": "playing",
          "trackId": "song-source",
          "positionMs": 60300,
          "syncDriftMs": -200,
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(laptop), "follow-feedback-1")

    source_state = get_state().get_playback_state("root:phone", "phone-1")
    follower_state = get_state().get_playback_state("root:laptop", "laptop-1")
    self.assertEqual(source_state["positionMs"], 60000)
    self.assertEqual(source_state["timelineId"], "session:root:phone:client:phone-1")
    self.assertEqual(source_state["followDelayMs"], 0)
    self.assertEqual(follower_state["positionMs"], 60300)
    self.assertEqual(follower_state["timelineId"], "session:root:laptop:client:laptop-1")
    self.assertEqual(follower_state["followDelayMs"], 0)

  def test_broadcast_accepts_base_control_version_and_emits_timeline_fields(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"])
    self.get_messages(phone)
    self.get_messages(pc)

    self.start_broadcast(phone, ["phone-1", "pc-1"], request_id="broadcast-start-authority-1")
    start_ack = self.get_ack(self.get_messages(phone), "broadcast-start-authority-1")
    broadcast_id = start_ack["payload"]["broadcastId"]
    self.assertEqual(start_ack["payload"]["broadcast"]["timelineId"], f"broadcast:{broadcast_id}")
    self.assertEqual(start_ack["payload"]["broadcast"]["controlVersion"], 1)
    self.assertEqual(start_ack["payload"]["broadcast"]["queueRevision"], 1)
    self.assertEqual(start_ack["payload"]["broadcast"]["followDelayMs"], 0)
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "broadcast.queue.sync",
        "requestId": "broadcast-base-control-1",
        "payload": {
          "broadcastId": broadcast_id,
          "queueSongIds": ["song-3", "song-4"],
          "currentIndex": 1,
          "positionMs": 0,
          "baseControlVersion": 1,
        },
      },
      namespace="/emo",
    )

    phone_messages = self.get_messages(phone)
    pc_messages = self.get_messages(pc)
    queue_ack = self.get_ack(phone_messages, "broadcast-base-control-1")
    self.assertEqual(queue_ack["payload"]["broadcast"]["controlVersion"], 2)
    self.assertEqual(queue_ack["payload"]["broadcast"]["queueRevision"], 2)
    self.assertEqual(queue_ack["payload"]["broadcast"]["timelineId"], f"broadcast:{broadcast_id}")
    pc_queue = next(message for message in pc_messages if message["action"] == "broadcast.queue.sync")
    self.assertEqual(pc_queue["payload"]["controlVersion"], 2)
    self.assertEqual(pc_queue["payload"]["queueRevision"], 2)
    self.assertIn("serverUpdatedAtMs", pc_queue["payload"])

  def test_broadcast_transport_rejects_stale_base_control_version(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    pc = self.connect_device("alice", "Alic3", "pc-1", "root:pc", ["player"])
    self.get_messages(phone)
    self.get_messages(pc)
    self.start_broadcast(phone, ["phone-1", "pc-1"], request_id="broadcast-start-stale-control-1")
    start_ack = self.get_ack(self.get_messages(phone), "broadcast-start-stale-control-1")
    broadcast_id = start_ack["payload"]["broadcastId"]
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.seek",
        "requestId": "broadcast-seek-stale-control-1",
        "payload": {
          "broadcastId": broadcast_id,
          "positionMs": 45000,
          "baseControlVersion": 0,
        },
      },
      namespace="/emo",
    )

    phone_messages = self.get_messages(phone)
    pc_messages = self.get_messages(pc)
    error = next(message for message in phone_messages if message["action"] == "system.error")
    self.assertEqual(error["requestId"], "broadcast-seek-stale-control-1")
    self.assertEqual(error["payload"]["code"], "conflict")
    self.assertEqual(error["payload"]["currentControlVersion"], 1)
    self.assertFalse(any(message["action"] == "broadcast.seek" for message in pc_messages))
    self.assertEqual(get_state().get_broadcast(broadcast_id)["positionMs"], 0)
