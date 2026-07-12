import os
import shutil
import tempfile
import unittest
from unittest import mock

from jsonschema import Draft202012Validator

from supysonic.db import release_database
from supysonic.emo.protocol_metadata import (
  get_strict_v2_metadata,
  get_strict_v2_registration_descriptor,
)
from supysonic.emo.ws import (
  CAPABILITY_PLAYBACK_CONTEXT_V2,
  _build_message,
  _commit_prepare,
  _expire_handoff_complete,
  _expire_prepare,
  init_socketio,
  socketio,
)
from supysonic.emo.ws_store import (
  getDevicePlaybackState,
  getLocalQueueState,
  getPlaybackContextState,
  getPlaybackState,
  getQueueState,
  saveLocalQueueState,
  saveQueueState,
)
from supysonic.emo.ws_state import get_state
from supysonic.managers.user import UserManager
from supysonic.web import create_application

from tests.testbase import TestConfig


STRICT_V2_CAPABILITIES = {
  "playbackContextV2": True,
  "playbackPrepare": False,
  "effectiveAtPlayback": False,
  "canPlay": True,
  "canPause": True,
  "canSeek": True,
  "canSetVolume": True,
  "supportsFollow": True,
  "supportsBroadcast": True,
}


class EmoWebSocketTestCase(unittest.TestCase):
  def setUp(self):
    self.__db = tempfile.mkstemp()
    self.__dir = tempfile.mkdtemp()
    self.config = TestConfig(False, False)
    self.config.BASE["database_uri"] = "sqlite:///" + self.__db[1]
    self.config.WEBAPP["cache_dir"] = self.__dir
    self.config.WEBAPP["mount_emosonic"] = True
    self.config.WEBAPP["emo_strict_v2_core_enabled"] = True
    self.config.WEBAPP["emo_strict_v2_follow_enabled"] = True
    self.config.WEBAPP["emo_strict_v2_handoff_enabled"] = True
    self.config.WEBAPP["emo_strict_v2_broadcast_enabled"] = True

    self.strict_conformance_patcher = mock.patch(
      "supysonic.emo.strict_v2_readiness.get_code_conformance_readiness",
      return_value={
        "core": True,
        "follow": True,
        "handoff": True,
        "broadcast": True,
      },
    )
    self.strict_conformance_patcher.start()

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
    self.strict_conformance_patcher.stop()

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
    auth_ack = self.get_ack(auth_messages, request_id)
    self.assertEqual(auth_ack["payload"]["action"], "auth.login")
    return client

  def test_socketio_initialization_logs_strict_v2_static_metadata(self):
    metadata = {
      "protocolVersion": "2.1.0",
      "schemaHash": "a" * 64,
      "serverBuildCommit": "b" * 40,
    }

    with mock.patch.object(socketio, "init_app") as init_app:
      with mock.patch("supysonic.emo.ws.get_strict_v2_metadata", return_value=metadata):
        with self.assertLogs("supysonic.emo.ws", level="WARNING") as logs:
          init_socketio(self.app)

    init_app.assert_called_once_with(self.app, path="/emo/ws")
    self.assertEqual(
      logs.output,
      [
        "WARNING:supysonic.emo.ws:"
        "emo event=strict_v2_registration_metadata "
        "protocol_version=2.1.0 "
        f"schema_hash={'a' * 64} "
        f"server_build_commit={'b' * 40}",
      ],
    )

  def register_device(self, client, request_id, payload):
    payload = dict(payload)
    capabilities = payload.get("capabilities")
    if isinstance(capabilities, dict) and capabilities.get(CAPABILITY_PLAYBACK_CONTEXT_V2) is True:
      strict_capabilities = dict(STRICT_V2_CAPABILITIES)
      strict_capabilities.update(capabilities)
      payload["capabilities"] = strict_capabilities
      payload.setdefault("deviceName", payload.get("clientId"))
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

    register_payload = {
      "clientId": client_id,
      "deviceName": client_id,
      "roles": roles,
      "capabilities": capabilities or {},
    }
    if register_payload["capabilities"].get(CAPABILITY_PLAYBACK_CONTEXT_V2) is True:
      register_payload["deviceSessionId"] = session_id
      strict_capabilities = dict(STRICT_V2_CAPABILITIES)
      strict_capabilities.update(register_payload["capabilities"])
      register_payload["capabilities"] = strict_capabilities
    else:
      register_payload["sessionId"] = session_id

    client.emit(
      "message",
      {
        "type": "device",
        "action": "device.register",
        "requestId": f"register-{client_id}",
        "payload": register_payload,
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

  def create_playback_context(
    self,
    client,
    request_id,
    playback_context_id="playback:alice:main",
    device_session_id="root:phone",
    queue_song_ids=None,
    current_index=0,
    position_ms=0,
  ):
    payload = {
      "playbackContextId": playback_context_id,
      "deviceSessionId": device_session_id,
      "queueSongIds": queue_song_ids or ["song-1"],
      "currentIndex": current_index,
      "positionMs": position_ms,
    }
    client.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.create",
        "requestId": request_id,
        "payload": payload,
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
    self.assertEqual(device["sessionId"], "sess-main")

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

  def test_v2_device_register_requires_device_session_id(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-player-v2")

    messages = self.register_device(
      client,
      "register-player-v2",
      {
        "clientId": "player-v2",
        "deviceName": "V2 Player",
        "roles": ["player"],
        "capabilities": {CAPABILITY_PLAYBACK_CONTEXT_V2: True},
      },
    )

    error = self.get_error(messages, "register-player-v2")
    self.assertEqual(error["payload"]["code"], "bad_request")

  def test_v2_device_register_rejects_session_id(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-player-v2")

    messages = self.register_device(
      client,
      "register-player-v2",
      {
        "clientId": "player-v2",
        "deviceName": "V2 Player",
        "roles": ["player"],
        "deviceSessionId": "device:player-v2",
        "sessionId": "legacy-room",
        "capabilities": {CAPABILITY_PLAYBACK_CONTEXT_V2: True},
      },
    )

    error = self.get_error(messages, "register-player-v2")
    self.assertEqual(error["payload"]["code"], "bad_request")

  def test_v2_device_register_does_not_return_session_id(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-player-v2")

    messages = self.register_device(
      client,
      "register-player-v2",
      {
        "clientId": "player-v2",
        "deviceName": "V2 Player",
        "roles": ["player"],
        "deviceSessionId": "device:player-v2",
        "capabilities": {CAPABILITY_PLAYBACK_CONTEXT_V2: True},
      },
    )

    ack = self.get_ack(messages, "register-player-v2")
    self.assertEqual(ack["payload"]["deviceSessionId"], "device:player-v2")
    self.assertNotIn("client", ack["payload"])
    self.assertNotIn("sessionId", ack["payload"])
    self.assertEqual(len(ack["payload"]["negotiatedCapabilities"]), 9)

  def test_v2_device_register_returns_strict_v2_metadata(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-player-v2")
    commit = "a" * 40

    with mock.patch.dict(
      os.environ,
      {"EMO_SERVER_BUILD_COMMIT": commit},
      clear=False,
    ):
      messages = self.register_device(
        client,
        "register-player-v2",
        {
          "clientId": "player-v2",
          "deviceName": "V2 Player",
          "roles": ["player"],
          "deviceSessionId": "device:player-v2",
          "capabilities": {CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        },
      )
      expected_metadata = get_strict_v2_metadata()

    ack = self.get_ack(messages, "register-player-v2")
    strict_v2 = ack["payload"]["strictV2"]
    self.assertEqual(ack["payload"]["action"], "device.register")
    self.assertEqual(ack["payload"]["clientId"], "player-v2")
    self.assertEqual(ack["payload"]["deviceSessionId"], "device:player-v2")
    self.assertEqual(
      {
        "protocolVersion": strict_v2["protocolVersion"],
        "schemaHash": strict_v2["schemaHash"],
        "serverBuildCommit": strict_v2["serverBuildCommit"],
      },
      expected_metadata,
    )
    self.assertEqual(strict_v2["serverBuildCommit"], commit)
    self.assertRegex(strict_v2["schemaHash"], r"^[0-9a-f]{64}$")
    self.assertEqual(strict_v2["protocolVersion"], "2.1.0")
    self.assertIsInstance(strict_v2["connectionNonce"], str)
    self.assertTrue(strict_v2["connectionNonce"])
    self.assertEqual(strict_v2["connectionEpoch"], 1)
    descriptor = get_strict_v2_registration_descriptor()
    validator = Draft202012Validator(descriptor["schema"])
    self.assertTrue(
      validator.is_valid(ack),
      list(validator.iter_errors(ack)),
    )

  def test_strict_replies_echo_action_and_include_connection_provenance(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-strict-replies")
    registration = self.get_ack(
      self.register_device(
        client,
        "register-strict-replies",
        {
          "clientId": "strict-replies",
          "deviceSessionId": "device:strict-replies",
          "capabilities": {CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        },
      ),
      "register-strict-replies",
    )
    strict_v2 = registration["payload"]["strictV2"]

    client.emit(
      "message",
      {
        "type": "system",
        "action": "system.ping",
        "requestId": "strict-ping-1",
        "payload": {},
      },
      namespace="/emo",
    )
    pong = next(
      message
      for message in self.get_messages(client)
      if message["action"] == "system.pong"
    )
    self.assertEqual(pong["connectionNonce"], strict_v2["connectionNonce"])
    self.assertEqual(pong["connectionEpoch"], strict_v2["connectionEpoch"])

    client.emit(
      "message",
      {
        "type": "state",
        "action": "device.list",
        "requestId": "strict-device-list-1",
        "payload": {},
      },
      namespace="/emo",
    )
    device_list = next(
      message
      for message in self.get_messages(client)
      if message["action"] == "device.list"
    )
    self.assertEqual(device_list["requestId"], "strict-device-list-1")
    self.assertEqual(device_list["connectionNonce"], strict_v2["connectionNonce"])
    self.assertEqual(device_list["connectionEpoch"], strict_v2["connectionEpoch"])

    error = self.get_error(
      self.register_device(
        client,
        "invalid-strict-register-1",
        {
          "clientId": "strict-replies",
          "capabilities": {CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        },
      ),
      "invalid-strict-register-1",
    )
    self.assertEqual(error["payload"]["action"], "device.register")
    self.assertEqual(error["connectionNonce"], strict_v2["connectionNonce"])
    self.assertEqual(error["connectionEpoch"], strict_v2["connectionEpoch"])

  def test_strict_registration_rejects_incomplete_roles_and_capabilities(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-invalid-strict-register")
    incomplete_capabilities = dict(STRICT_V2_CAPABILITIES)
    del incomplete_capabilities["supportsBroadcast"]

    client.emit(
      "message",
      {
        "type": "device",
        "action": "device.register",
        "requestId": "invalid-strict-capabilities-1",
        "payload": {
          "clientId": "invalid-strict-capabilities",
          "deviceSessionId": "device:invalid-strict-capabilities",
          "deviceName": "Invalid strict client",
          "roles": ["player", "controller"],
          "capabilities": incomplete_capabilities,
        },
      },
      namespace="/emo",
    )
    capability_error = self.get_error(
      self.get_messages(client),
      "invalid-strict-capabilities-1",
    )
    self.assertEqual(capability_error["payload"]["action"], "device.register")
    self.assertEqual(capability_error["payload"]["code"], "bad_request")

    client.emit(
      "message",
      {
        "type": "device",
        "action": "device.register",
        "requestId": "invalid-strict-roles-1",
        "payload": {
          "clientId": "invalid-strict-roles",
          "deviceSessionId": "device:invalid-strict-roles",
          "deviceName": "Invalid strict client",
          "roles": ["player", "player"],
          "capabilities": STRICT_V2_CAPABILITIES,
        },
      },
      namespace="/emo",
    )
    roles_error = self.get_error(self.get_messages(client), "invalid-strict-roles-1")
    self.assertEqual(roles_error["payload"]["action"], "device.register")
    self.assertEqual(roles_error["payload"]["code"], "bad_request")

  def test_strict_playback_update_broadcasts_feedback_and_context_status(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    observer = self.connect_device(
      "alice",
      "Alic3",
      "observer-1",
      "root:observer",
      ["controller"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(observer)
    self.create_playback_context(phone, "context-create-feedback-1")
    self.get_messages(phone)
    self.get_messages(observer)

    observer.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.subscribe",
        "requestId": "context-subscribe-feedback-1",
        "payload": {"playbackContextId": "playback:alice:main"},
      },
      namespace="/emo",
    )
    self.get_messages(observer)

    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "playback-feedback-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 1200,
          "clientSeq": 7,
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(phone), "playback-feedback-1")
    observer_messages = self.get_messages(observer)
    feedback = next(
      message
      for message in observer_messages
      if message["action"] == "playback.update"
    )
    status = next(
      message
      for message in observer_messages
      if message["action"] == "playback.context.status"
    )

    self.assertEqual(feedback["type"], "event")
    self.assertEqual(
      feedback["payload"],
      {
        "playbackContextId": "playback:alice:main",
        "sourceClientId": "phone-1",
        "deviceSessionId": "root:phone",
        "state": "playing",
        "positionMs": 1200,
        "trackId": "song-1",
        "clientSeq": 7,
        "serverUpdatedAtMs": feedback["payload"]["serverUpdatedAtMs"],
        "serverTimeMs": feedback["payload"]["serverTimeMs"],
      },
    )
    self.assertEqual(status["type"], "state")
    self.assertEqual(status["payload"]["playbackContext"]["state"], "playing")

  def test_strict_follow_and_broadcast_require_declared_capabilities(self):
    source = self.connect_device(
      "alice",
      "Alic3",
      "source-1",
      "root:source",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    unsupported = self.connect_device(
      "alice",
      "Alic3",
      "unsupported-1",
      "root:unsupported",
      ["player"],
      capabilities={
        CAPABILITY_PLAYBACK_CONTEXT_V2: True,
        "supportsFollow": False,
        "supportsBroadcast": False,
      },
    )
    self.get_messages(source)
    self.get_messages(unsupported)
    self.create_playback_context(source, "context-create-capability-1", device_session_id="root:source")
    self.get_messages(source)
    self.get_messages(unsupported)

    unsupported.emit(
      "message",
      {
        "type": "command",
        "action": "follow.start",
        "requestId": "follow-no-capability-1",
        "payload": {
          "sourcePlaybackContextId": "playback:alice:main",
          "deviceSessionId": "root:unsupported",
        },
      },
      namespace="/emo",
    )
    follow_error = self.get_error(self.get_messages(unsupported), "follow-no-capability-1")
    self.assertEqual(follow_error["payload"]["code"], "forbidden")

    unsupported.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.start",
        "requestId": "broadcast-no-capability-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "targetMode": "selectedClients",
          "targetClientIds": ["source-1"],
          "queueSongIds": ["song-1"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    broadcast_error = self.get_error(self.get_messages(unsupported), "broadcast-no-capability-1")
    self.assertEqual(broadcast_error["payload"]["code"], "forbidden")

  def test_v2_device_register_nonce_is_unique_per_connection(self):
    first_client = self.connect_authenticated_client("alice", "Alic3", "auth-player-v2-first")
    second_client = self.connect_authenticated_client("alice", "Alic3", "auth-player-v2-second")

    first_ack = self.get_ack(
      self.register_device(
        first_client,
        "register-player-v2-first",
        {
          "clientId": "player-v2-first",
          "deviceSessionId": "device:player-v2-first",
          "capabilities": {CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        },
      ),
      "register-player-v2-first",
    )
    second_ack = self.get_ack(
      self.register_device(
        second_client,
        "register-player-v2-second",
        {
          "clientId": "player-v2-second",
          "deviceSessionId": "device:player-v2-second",
          "capabilities": {CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        },
      ),
      "register-player-v2-second",
    )

    self.assertNotEqual(
      first_ack["payload"]["strictV2"]["connectionNonce"],
      second_ack["payload"]["strictV2"]["connectionNonce"],
    )

  def test_legacy_device_register_does_not_return_strict_v2_metadata(self):
    client = self.connect_authenticated_client("alice", "Alic3", "auth-player-legacy")

    messages = self.register_device(
      client,
      "register-player-legacy",
      {
        "clientId": "player-legacy",
        "deviceName": "Legacy Player",
        "roles": ["player"],
        "sessionId": "legacy-room",
      },
    )

    ack = self.get_ack(messages, "register-player-legacy")
    self.assertNotIn("strictV2", ack["payload"])
    self.assertEqual(ack["payload"]["client"]["sessionId"], "legacy-room")

  def test_v2_device_list_omits_session_id_aliases(self):
    legacy = self.connect_device("alice", "Alic3", "legacy-player-1", "legacy-room", ["player"])
    v2 = self.connect_device(
      "alice",
      "Alic3",
      "v2-player-1",
      "device:v2-player-1",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(legacy)
    self.get_messages(v2)

    v2.emit(
      "message",
      {
        "type": "device",
        "action": "device.list",
        "requestId": "v2-device-list-1",
        "payload": {},
      },
      namespace="/emo",
    )

    device_list = next(message for message in self.get_messages(v2) if message["action"] == "device.list")
    self.assertEqual(
      {device["clientId"] for device in device_list["payload"]["devices"]},
      {"legacy-player-1", "v2-player-1"},
    )
    for device in device_list["payload"]["devices"]:
      self.assertNotIn("sessionId", device)
      self.assertIn("deviceSessionId", device)

  def test_v2_device_list_broadcast_omits_session_id_aliases(self):
    v2 = self.connect_device(
      "alice",
      "Alic3",
      "v2-player-1",
      "device:v2-player-1",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(v2)
    legacy = self.connect_authenticated_client("alice", "Alic3", "auth-legacy-player-1")

    self.register_device(
      legacy,
      "register-legacy-player-1",
      {
        "clientId": "legacy-player-1",
        "deviceName": "Legacy Player",
        "roles": ["player"],
        "sessionId": "legacy-room",
      },
    )

    device_list = next(message for message in self.get_messages(v2) if message["action"] == "device.list")
    legacy_device = next(
      device for device in device_list["payload"]["devices"] if device["clientId"] == "legacy-player-1"
    )
    self.assertEqual(legacy_device["deviceSessionId"], "legacy-room")
    self.assertNotIn("sessionId", legacy_device)

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
    state._clients[("alice", "player-1")]["lastSeenAt"] = 1

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

  def test_v2_playback_context_create_sets_current_client_as_authority(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.create",
        "requestId": "context-create-1",
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

    ack = self.get_ack(self.get_messages(phone), "context-create-1")
    context = ack["payload"]["playbackContext"]
    self.assertTrue(ack["payload"]["created"])
    self.assertEqual(context["playbackContextId"], "playback:alice:main")
    self.assertEqual(context["authorityClientId"], "phone-1")
    self.assertNotIn("sessionId", context)
    self.assertNotIn("sourceClientId", context)
    self.assertIsNone(getQueueState("playback:alice:main"))
    persisted_context = getPlaybackContextState("playback:alice:main")
    for counter_name in ("queueRevision", "controlVersion", "version", "epoch"):
      self.assertEqual(persisted_context[counter_name], context[counter_name])

  def test_v2_playback_context_queue_payload_rejects_invalid_values(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.create",
        "requestId": "context-create-invalid-queue-1",
        "payload": {
          "playbackContextId": "playback:alice:invalid",
          "deviceSessionId": "root:phone",
          "queueSongIds": "",
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )
    invalid_queue = self.get_error(
      self.get_messages(phone),
      "context-create-invalid-queue-1",
    )
    self.assertEqual(invalid_queue["payload"]["code"], "bad_request")
    self.assertIsNone(getPlaybackContextState("playback:alice:invalid"))

    self.create_playback_context(phone, "context-create-valid-1")
    phone.emit(
      "message",
      {
        "type": "state",
        "action": "queue.context.sync",
        "requestId": "context-sync-negative-position-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "queueSongIds": ["song-1"],
          "currentIndex": 0,
          "positionMs": -1,
        },
      },
      namespace="/emo",
    )
    negative_position = self.get_error(
      self.get_messages(phone),
      "context-sync-negative-position-1",
    )
    self.assertEqual(negative_position["payload"]["code"], "bad_request")
    self.assertEqual(getPlaybackContextState("playback:alice:main")["positionMs"], 0)

  def test_v2_playback_update_rejects_invalid_shared_state(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.create_playback_context(phone, "context-create-1")

    invalid_payloads = (
      ("invalid-queue", {"queueSongIds": "bad", "currentIndex": 0}),
      ("invalid-index", {"queueSongIds": ["song-1"], "currentIndex": True}),
      ("invalid-position", {"positionMs": -1}),
    )
    for suffix, invalid_fields in invalid_payloads:
      payload = {
        "playbackContextId": "playback:alice:main",
        "deviceSessionId": "root:phone",
        "state": "playing",
        "trackId": "song-1",
        "positionMs": 0,
      }
      payload.update(invalid_fields)
      request_id = f"playback-update-{suffix}-1"
      phone.emit(
        "message",
        {
          "type": "event",
          "action": "playback.update",
          "requestId": request_id,
          "payload": payload,
        },
        namespace="/emo",
      )
      error = self.get_error(self.get_messages(phone), request_id)
      self.assertEqual(error["payload"]["code"], "bad_request")

    persisted = getPlaybackContextState("playback:alice:main")
    self.assertEqual(persisted["queueSongIds"], ["song-1"])
    self.assertEqual(persisted["currentIndex"], 0)
    self.assertEqual(persisted["positionMs"], 0)
    self.assertEqual(persisted["trackId"], "song-1")

  def test_v2_playback_context_create_restores_existing_persisted_context(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)

    first_ack = self.create_playback_context(
      phone,
      "context-create-1",
      queue_song_ids=["song-1", "song-2"],
      current_index=1,
      position_ms=1200,
    )
    persisted_context = getPlaybackContextState("playback:alice:main")
    get_state()._playback_contexts.clear()

    second_ack = self.create_playback_context(
      phone,
      "context-create-2",
      queue_song_ids=["song-1"],
      current_index=0,
      position_ms=0,
    )
    restored_context = second_ack["payload"]["playbackContext"]

    self.assertTrue(first_ack["payload"]["created"])
    self.assertFalse(second_ack["payload"]["created"])
    self.assertEqual(restored_context["queueSongIds"], ["song-1", "song-2"])
    self.assertEqual(restored_context["currentIndex"], 1)
    self.assertEqual(restored_context["positionMs"], 1200)
    for counter_name in ("queueRevision", "controlVersion", "version", "epoch"):
      self.assertEqual(restored_context[counter_name], persisted_context[counter_name])

  def test_v2_playback_context_status_uses_v2_serializer_without_session_id(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.create",
        "requestId": "context-create-1",
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

    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "context-playback-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 500,
        },
      },
      namespace="/emo",
    )
    self.get_messages(phone)

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.status",
        "requestId": "context-status-1",
        "payload": {"playbackContextId": "playback:alice:main"},
      },
      namespace="/emo",
    )

    ack = self.get_ack(self.get_messages(phone), "context-status-1")
    context = ack["payload"]["playbackContext"]
    device_state = ack["payload"]["deviceStates"][0]
    self.assertEqual(context["playbackContextId"], "playback:alice:main")
    self.assertNotIn("sessionId", context)
    self.assertNotIn("sourceClientId", context)
    self.assertEqual(device_state["clientId"], "phone-1")
    self.assertEqual(device_state["deviceSessionId"], "root:phone")
    self.assertNotIn("sessionId", device_state)
    self.assertNotIn("sourceClientId", device_state)

  def test_v2_playback_context_subscribe_pushes_snapshot(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    observer = self.connect_device(
      "alice",
      "Alic3",
      "observer-1",
      "root:observer",
      ["controller"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(observer)
    self.create_playback_context(phone, "context-create-1")

    observer.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.subscribe",
        "requestId": "context-subscribe-1",
        "payload": {"playbackContextId": "playback:alice:main"},
      },
      namespace="/emo",
    )

    messages = self.get_messages(observer)
    ack = self.get_ack(messages, "context-subscribe-1")
    snapshot = next(
      message for message in messages if message["action"] == "playback.context.status"
    )
    self.assertEqual(ack["payload"]["subscriptions"], ["playback:alice:main"])
    self.assertEqual(
      snapshot["payload"]["playbackContext"]["playbackContextId"],
      "playback:alice:main",
    )
    self.assertNotIn("sessionId", snapshot["payload"]["playbackContext"])

  def test_v2_playback_context_subscriber_receives_queue_context_sync(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    observer = self.connect_device(
      "alice",
      "Alic3",
      "observer-1",
      "root:observer",
      ["controller"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    bystander = self.connect_device(
      "alice",
      "Alic3",
      "bystander-1",
      "root:bystander",
      ["controller"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(observer)
    self.get_messages(bystander)
    self.create_playback_context(phone, "context-create-1")
    self.get_messages(observer)
    self.get_messages(bystander)

    observer.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.subscribe",
        "requestId": "context-subscribe-1",
        "payload": {"playbackContextId": "playback:alice:main"},
      },
      namespace="/emo",
    )
    self.get_messages(observer)

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "queue.context.sync",
        "requestId": "queue-context-sync-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 1,
          "positionMs": 250,
          "baseQueueRevision": 0,
        },
      },
      namespace="/emo",
    )
    self.get_messages(phone)
    observer_messages = self.get_messages(observer)
    bystander_messages = self.get_messages(bystander)

    queue_update = next(
      message for message in observer_messages if message["action"] == "queue.context.sync"
    )
    self.assertEqual(queue_update["payload"]["currentIndex"], 1)
    self.assertNotIn("sessionId", queue_update["payload"])
    self.assertFalse(
      any(message["action"] == "queue.context.sync" for message in bystander_messages)
    )

  def test_v2_playback_context_unsubscribe_stops_queue_updates(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    observer = self.connect_device(
      "alice",
      "Alic3",
      "observer-1",
      "root:observer",
      ["controller"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(observer)
    self.create_playback_context(phone, "context-create-1")
    self.get_messages(observer)

    observer.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.subscribe",
        "requestId": "context-subscribe-1",
        "payload": {"playbackContextId": "playback:alice:main"},
      },
      namespace="/emo",
    )
    self.get_messages(observer)
    observer.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.unsubscribe",
        "requestId": "context-unsubscribe-1",
        "payload": {"playbackContextId": "playback:alice:main"},
      },
      namespace="/emo",
    )
    unsubscribe_ack = self.get_ack(self.get_messages(observer), "context-unsubscribe-1")
    self.assertEqual(unsubscribe_ack["payload"]["subscriptions"], [])

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "queue.context.sync",
        "requestId": "queue-context-sync-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 1,
          "positionMs": 250,
          "baseQueueRevision": 0,
        },
      },
      namespace="/emo",
    )
    self.get_messages(phone)
    observer_messages = self.get_messages(observer)
    self.assertFalse(
      any(message["action"] == "queue.context.sync" for message in observer_messages)
    )

  def test_v2_playback_context_close_closes_context_and_clears_subscribers(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    observer = self.connect_device(
      "alice",
      "Alic3",
      "observer-1",
      "root:observer",
      ["controller"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(observer)
    self.create_playback_context(phone, "context-create-1")
    self.get_messages(observer)

    observer.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.subscribe",
        "requestId": "context-subscribe-1",
        "payload": {"playbackContextId": "playback:alice:main"},
      },
      namespace="/emo",
    )
    self.get_messages(observer)

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.close",
        "requestId": "context-close-1",
        "payload": {"playbackContextId": "playback:alice:main"},
      },
      namespace="/emo",
    )

    close_ack = self.get_ack(self.get_messages(phone), "context-close-1")
    observer_messages = self.get_messages(observer)
    closed = next(
      message
      for message in observer_messages
      if message["action"] == "playback.context.closed"
    )
    runtime_context = get_state().get_playback_context("playback:alice:main")
    persisted_context = getPlaybackContextState("playback:alice:main")

    self.assertTrue(close_ack["payload"]["closed"])
    self.assertEqual(close_ack["payload"]["playbackContext"]["state"], "closed")
    self.assertNotIn("sessionId", close_ack["payload"]["playbackContext"])
    self.assertEqual(closed["type"], "event")
    self.assertEqual(closed["payload"], {"playbackContextId": "playback:alice:main"})
    self.assertIn("connectionNonce", closed)
    self.assertEqual(closed["connectionEpoch"], 1)
    self.assertEqual(runtime_context["state"], "closed")
    self.assertEqual(persisted_context["state"], "closed")
    self.assertEqual(
      get_state().list_playback_context_subscribers(
        "playback:alice:main",
        user_name="alice",
      ),
      [],
    )

  def test_v2_player_pause_controls_context_authority(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    controller = self.connect_device(
      "alice",
      "Alic3",
      "controller-1",
      "root:controller",
      ["controller"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(controller)
    self.create_playback_context(phone, "context-create-1")
    self.get_messages(phone)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.pause",
        "requestId": "v2-pause-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "baseControlVersion": 0,
          "positionMs": 1200,
        },
      },
      namespace="/emo",
    )

    ack = self.get_ack(self.get_messages(controller), "v2-pause-1")
    phone_messages = self.get_messages(phone)
    command = next(message for message in phone_messages if message["action"] == "player.pause")
    context = get_state().get_playback_context("playback:alice:main")
    self.assertTrue(ack["payload"]["updated"])
    self.assertEqual(ack["payload"]["authorityClientId"], "phone-1")
    self.assertEqual(ack["payload"]["playbackContext"]["state"], "paused")
    self.assertNotIn("sessionId", ack["payload"]["playbackContext"])
    self.assertNotIn("targetClientId", command)
    self.assertEqual(command["payload"]["playbackContextId"], "playback:alice:main")
    self.assertNotIn("sessionId", command["payload"])
    self.assertEqual(context["state"], "paused")
    self.assertEqual(context["positionMs"], 1200)
    self.assertIsNone(getPlaybackState("root:phone", "phone-1"))

  def test_v2_player_seek_updates_context_control_version(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    controller = self.connect_device(
      "alice",
      "Alic3",
      "controller-1",
      "root:controller",
      ["controller"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(controller)
    self.create_playback_context(phone, "context-create-1")
    self.get_messages(phone)
    self.get_messages(controller)

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
          "positionMs": 500,
        },
      },
      namespace="/emo",
    )
    self.get_messages(phone)
    context = get_state().get_playback_context("playback:alice:main")

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.seek",
        "requestId": "v2-seek-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "baseControlVersion": context["controlVersion"],
          "positionMs": 4200,
        },
      },
      namespace="/emo",
    )

    ack = self.get_ack(self.get_messages(controller), "v2-seek-1")
    phone_messages = self.get_messages(phone)
    command = next(message for message in phone_messages if message["action"] == "player.seek")
    updated = get_state().get_playback_context("playback:alice:main")
    self.assertEqual(ack["payload"]["playbackContext"]["positionMs"], 4200)
    self.assertEqual(
      ack["payload"]["playbackContext"]["controlVersion"],
      context["controlVersion"] + 1,
    )
    self.assertNotIn("targetClientId", command)
    self.assertEqual(command["payload"]["positionMs"], 4200)
    self.assertNotIn("sessionId", command["payload"])
    self.assertEqual(updated["state"], "playing")
    self.assertEqual(updated["positionMs"], 4200)
    self.assertIsNone(getPlaybackState("root:phone", "phone-1"))

  def test_v2_player_play_routes_to_context_authority(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    controller = self.connect_device(
      "alice",
      "Alic3",
      "controller-1",
      "root:controller",
      ["controller"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(controller)
    self.create_playback_context(phone, "context-create-1")
    self.get_messages(phone)
    self.get_messages(controller)

    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "phone-paused-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "state": "paused",
          "trackId": "song-1",
          "positionMs": 500,
        },
      },
      namespace="/emo",
    )
    self.get_messages(phone)
    context = get_state().get_playback_context("playback:alice:main")

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.play",
        "requestId": "v2-play-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "baseControlVersion": context["controlVersion"],
        },
      },
      namespace="/emo",
    )

    ack = self.get_ack(self.get_messages(controller), "v2-play-1")
    phone_messages = self.get_messages(phone)
    command = next(message for message in phone_messages if message["action"] == "player.play")
    updated = get_state().get_playback_context("playback:alice:main")
    self.assertNotIn("targetClientId", command)
    self.assertEqual(command["payload"]["playbackContextId"], "playback:alice:main")
    self.assertEqual(command["payload"]["sourceClientId"], "controller-1")
    self.assertEqual(command["payload"]["controlVersion"], context["controlVersion"] + 1)
    self.assertEqual(command["payload"]["positionMs"], 500)
    self.assertNotIn("baseControlVersion", command["payload"])
    self.assertNotIn("queueIndex", command["payload"])
    self.assertNotIn("trackId", command["payload"])
    self.assertNotIn("sessionId", command["payload"])
    self.assertEqual(ack["payload"]["playbackContext"]["state"], "playing")
    self.assertEqual(ack["payload"]["playbackContext"]["positionMs"], 500)
    self.assertEqual(
      ack["payload"]["playbackContext"]["controlVersion"],
      context["controlVersion"] + 1,
    )
    self.assertEqual(updated["state"], "playing")
    self.assertEqual(updated["positionMs"], 500)
    self.assertIsNone(getPlaybackState("root:phone", "phone-1"))

  def test_v2_player_next_uses_context_queue(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    controller = self.connect_device(
      "alice",
      "Alic3",
      "controller-1",
      "root:controller",
      ["controller"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(controller)
    self.create_playback_context(
      phone,
      "context-create-1",
      queue_song_ids=["song-1", "song-2", "song-3"],
    )
    self.get_messages(phone)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.next",
        "requestId": "v2-next-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "baseControlVersion": 0,
        },
      },
      namespace="/emo",
    )

    ack = self.get_ack(self.get_messages(controller), "v2-next-1")
    phone_messages = self.get_messages(phone)
    command = next(message for message in phone_messages if message["action"] == "player.next")
    context = get_state().get_playback_context("playback:alice:main")
    self.assertNotIn("targetClientId", command)
    self.assertEqual(command["payload"]["playbackContextId"], "playback:alice:main")
    self.assertEqual(command["payload"]["sourceClientId"], "controller-1")
    self.assertEqual(command["payload"]["controlVersion"], 1)
    self.assertNotIn("baseControlVersion", command["payload"])
    self.assertNotIn("queueIndex", command["payload"])
    self.assertNotIn("trackId", command["payload"])
    self.assertNotIn("positionMs", command["payload"])
    self.assertNotIn("sessionId", command["payload"])
    self.assertEqual(ack["payload"]["playbackContext"]["controlVersion"], 1)
    self.assertEqual(ack["payload"]["playbackContext"]["currentIndex"], 1)
    self.assertEqual(ack["payload"]["playbackContext"]["trackId"], "song-2")
    self.assertEqual(ack["payload"]["playbackContext"]["state"], "playing")
    self.assertEqual(context["currentIndex"], 1)
    self.assertEqual(context["trackId"], "song-2")
    self.assertIsNone(getPlaybackState("root:phone", "phone-1"))

  def test_v2_rapid_player_next_advances_from_optimistic_context(self):
    capabilities = {CAPABILITY_PLAYBACK_CONTEXT_V2: True}
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities=capabilities,
    )
    controller = self.connect_device(
      "alice",
      "Alic3",
      "controller-1",
      "root:controller",
      ["controller"],
      capabilities=capabilities,
    )
    self.get_messages(phone)
    self.get_messages(controller)
    self.create_playback_context(
      phone,
      "context-create-1",
      queue_song_ids=["song-1", "song-2", "song-3"],
    )
    self.get_messages(phone)
    self.get_messages(controller)

    for request_id, base_control_version in (("v2-next-1", 0), ("v2-next-2", 1)):
      controller.emit(
        "message",
        {
          "type": "command",
          "action": "player.next",
          "requestId": request_id,
          "payload": {
            "playbackContextId": "playback:alice:main",
            "baseControlVersion": base_control_version,
          },
        },
        namespace="/emo",
      )
      self.get_ack(self.get_messages(controller), request_id)

    commands = [
      message
      for message in self.get_messages(phone)
      if message["action"] == "player.next"
    ]
    context = get_state().get_playback_context("playback:alice:main")
    self.assertEqual(
      [message["payload"]["controlVersion"] for message in commands],
      [1, 2],
    )
    self.assertTrue(
      all("queueIndex" not in message["payload"] for message in commands)
    )
    self.assertEqual(context["currentIndex"], 2)
    self.assertEqual(context["trackId"], "song-3")
    self.assertEqual(context["controlVersion"], 2)

  def test_v2_player_prev_uses_context_queue(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    controller = self.connect_device(
      "alice",
      "Alic3",
      "controller-1",
      "root:controller",
      ["controller"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(controller)
    self.create_playback_context(
      phone,
      "context-create-1",
      queue_song_ids=["song-1", "song-2", "song-3"],
      current_index=1,
    )
    self.get_messages(phone)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "player.prev",
        "requestId": "v2-prev-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "baseControlVersion": 0,
        },
      },
      namespace="/emo",
    )

    ack = self.get_ack(self.get_messages(controller), "v2-prev-1")
    phone_messages = self.get_messages(phone)
    command = next(message for message in phone_messages if message["action"] == "player.prev")
    context = get_state().get_playback_context("playback:alice:main")
    self.assertNotIn("targetClientId", command)
    self.assertEqual(command["payload"]["playbackContextId"], "playback:alice:main")
    self.assertEqual(command["payload"]["sourceClientId"], "controller-1")
    self.assertEqual(command["payload"]["controlVersion"], 1)
    self.assertNotIn("baseControlVersion", command["payload"])
    self.assertNotIn("queueIndex", command["payload"])
    self.assertNotIn("trackId", command["payload"])
    self.assertNotIn("positionMs", command["payload"])
    self.assertNotIn("sessionId", command["payload"])
    self.assertEqual(ack["payload"]["playbackContext"]["controlVersion"], 1)
    self.assertEqual(ack["payload"]["playbackContext"]["currentIndex"], 0)
    self.assertEqual(ack["payload"]["playbackContext"]["trackId"], "song-1")
    self.assertEqual(ack["payload"]["playbackContext"]["state"], "playing")
    self.assertEqual(context["currentIndex"], 0)
    self.assertEqual(context["trackId"], "song-1")
    self.assertIsNone(getPlaybackState("root:phone", "phone-1"))

  def test_v2_queue_play_item_uses_context_queue(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    controller = self.connect_device(
      "alice",
      "Alic3",
      "controller-1",
      "root:controller",
      ["controller"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(controller)
    self.create_playback_context(
      phone,
      "context-create-1",
      queue_song_ids=["song-1", "song-2"],
    )
    self.get_messages(phone)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "queue.playItem",
        "requestId": "v2-play-item-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "baseControlVersion": 0,
          "queueIndex": 1,
          "positionMs": 50,
        },
      },
      namespace="/emo",
    )

    ack = self.get_ack(self.get_messages(controller), "v2-play-item-1")
    phone_messages = self.get_messages(phone)
    command = next(message for message in phone_messages if message["action"] == "queue.playItem")
    context = get_state().get_playback_context("playback:alice:main")
    self.assertEqual(ack["payload"]["playbackContext"]["controlVersion"], 1)
    self.assertEqual(ack["payload"]["playbackContext"]["currentIndex"], 1)
    self.assertEqual(ack["payload"]["playbackContext"]["trackId"], "song-2")
    self.assertEqual(ack["payload"]["playbackContext"]["state"], "playing")
    self.assertNotIn("targetClientId", command)
    self.assertEqual(command["payload"]["playbackContextId"], "playback:alice:main")
    self.assertEqual(command["payload"]["queueSongIds"], ["song-1", "song-2"])
    self.assertEqual(command["payload"]["queueIndex"], 1)
    self.assertEqual(command["payload"]["queueRevision"], context["queueRevision"])
    self.assertEqual(command["payload"]["controlVersion"], context["controlVersion"])
    self.assertEqual(command["payload"]["sourceClientId"], "controller-1")
    self.assertNotIn("baseControlVersion", command["payload"])
    self.assertNotIn("trackId", command["payload"])
    self.assertNotIn("sessionId", command["payload"])
    self.assertEqual(context["currentIndex"], 1)
    self.assertEqual(context["trackId"], "song-2")

    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "phone-play-item-confirm-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "state": "playing",
          "trackId": "song-2",
          "currentIndex": 1,
          "positionMs": 50,
        },
      },
      namespace="/emo",
    )
    self.get_messages(phone)
    confirmed = get_state().get_playback_context("playback:alice:main")
    self.assertEqual(confirmed["currentIndex"], 1)
    self.assertEqual(confirmed["trackId"], "song-2")
    self.assertEqual(confirmed["state"], "playing")
    self.assertEqual(confirmed["positionMs"], 50)
    self.assertIsNone(getPlaybackState("root:phone", "phone-1"))

  def test_v2_queue_context_sync_requires_existing_context(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "queue.context.sync",
        "requestId": "queue-context-missing-1",
        "payload": {
          "playbackContextId": "playback:alice:missing",
          "deviceSessionId": "root:phone",
          "queueSongIds": ["song-1"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )

    error = self.get_error(self.get_messages(phone), "queue-context-missing-1")
    self.assertEqual(error["payload"]["code"], "not_found")
    self.assertIsNone(get_state().get_playback_context("playback:alice:missing"))

  def test_v2_queue_context_sync_requires_authority(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    pc = self.connect_device(
      "alice",
      "Alic3",
      "pc-1",
      "root:pc",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.create",
        "requestId": "context-create-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
        },
      },
      namespace="/emo",
    )
    self.get_messages(phone)

    pc.emit(
      "message",
      {
        "type": "state",
        "action": "queue.context.sync",
        "requestId": "queue-context-non-authority-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:pc",
          "queueSongIds": ["song-1"],
          "currentIndex": 0,
          "positionMs": 0,
        },
      },
      namespace="/emo",
    )

    error = self.get_error(self.get_messages(pc), "queue-context-non-authority-1")
    self.assertEqual(error["payload"]["code"], "forbidden")

  def test_v2_queue_context_sync_does_not_write_emo_session_queue(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.create",
        "requestId": "context-create-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
        },
      },
      namespace="/emo",
    )
    self.get_messages(phone)

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "queue.context.sync",
        "requestId": "queue-context-sync-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 1,
          "positionMs": 250,
          "baseQueueRevision": 0,
        },
      },
      namespace="/emo",
    )

    ack = self.get_ack(self.get_messages(phone), "queue-context-sync-1")
    queue = ack["payload"]["queue"]
    self.assertTrue(ack["payload"]["updated"])
    self.assertEqual(queue["playbackContextId"], "playback:alice:main")
    self.assertEqual(queue["currentIndex"], 1)
    self.assertNotIn("sessionId", queue)
    self.assertNotIn("sourceClientId", queue)
    self.assertIsNone(getQueueState("playback:alice:main"))
    persisted = getPlaybackContextState("playback:alice:main")
    self.assertEqual(persisted["queueSongIds"], ["song-1", "song-2"])

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
    phone_messages = self.get_messages(phone)
    release = next(
      message for message in phone_messages if message["action"] == "playback.handoff.release"
    )
    self.assertEqual(release["payload"]["reason"], "handoff_completed")
    self.assertEqual(release["payload"]["authorityClientId"], "pc-1")
    self.assertEqual(release["targetClientId"], "phone-1")
    self.assertFalse(any(message["action"] == "player.pause" for message in phone_messages))
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
    pc_messages = self.get_messages(pc)
    release = next(
      message for message in pc_messages if message["action"] == "playback.handoff.release"
    )
    self.assertEqual(release["payload"]["reason"], "timed_out")
    self.assertEqual(release["targetClientId"], "pc-1")
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

  def test_v2_handoff_timeout_releases_target(self):
    capabilities = {
      CAPABILITY_PLAYBACK_CONTEXT_V2: True,
      "effectiveAtPlayback": True,
      "playbackPrepare": True,
    }
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities=capabilities,
    )
    pc = self.connect_device(
      "alice",
      "Alic3",
      "pc-1",
      "root:pc",
      ["player"],
      capabilities=capabilities,
    )
    self.get_messages(phone)
    self.get_messages(pc)
    self.create_playback_context(phone, "context-create-1")
    self.get_messages(phone)
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.start",
        "requestId": "v2-handoff-timeout-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "sourceClientId": "phone-1",
          "targetClientId": "pc-1",
          "baseControlVersion": 0,
        },
      },
      namespace="/emo",
    )
    start_ack = self.get_ack(self.get_messages(phone), "v2-handoff-timeout-1")
    prepare = next(message for message in self.get_messages(pc) if message["action"] == "playback.prepare")

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "v2-handoff-ready-timeout-1",
        "payload": {
          "prepareId": prepare["payload"]["prepareId"],
          "ready": True,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(pc), "v2-handoff-ready-timeout-1")

    get_state().update_playback_handoff(
      start_ack["payload"]["handoffId"],
      complete_expires_at_ms=0,
    )
    expired = _expire_handoff_complete(start_ack["payload"]["handoffId"])
    self.assertEqual(expired["status"], "timed_out")
    pc_messages = self.get_messages(pc)
    release = next(
      message for message in pc_messages if message["action"] == "playback.handoff.release"
    )
    self.assertEqual(release["payload"]["reason"], "timed_out")
    self.assertEqual(release["payload"]["playbackContextId"], "playback:alice:main")
    self.assertNotIn("sessionId", release["payload"])
    self.assertNotIn("targetClientId", release)
    self.assertEqual(
      get_state().get_playback_context("playback:alice:main")["authorityClientId"],
      "phone-1",
    )

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
    self.assertEqual(first_ack["payload"]["originClientId"], "phone-1")

    phone.emit("message", start_message, namespace="/emo")
    duplicate_ack = self.get_ack(self.get_messages(phone), "handoff-idempotent-1")
    duplicate_target_messages = self.get_messages(pc)

    self.assertTrue(duplicate_ack["payload"]["duplicate"])
    self.assertEqual(duplicate_ack["payload"]["handoffId"], first_ack["payload"]["handoffId"])
    self.assertEqual(duplicate_ack["payload"]["prepareId"], first_prepare["payload"]["prepareId"])
    self.assertFalse(any(message["action"] == "playback.prepare" for message in duplicate_target_messages))

  def test_persisted_duplicate_handoff_start_rebuilds_missing_prepare(self):
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
      "requestId": "handoff-rebuild-1",
      "payload": {
        "playbackContextId": "playback:alice:main",
        "sourceClientId": "phone-1",
        "targetClientId": "pc-1",
        "baseControlVersion": context["controlVersion"],
      },
    }

    phone.emit("message", start_message, namespace="/emo")
    first_ack = self.get_ack(self.get_messages(phone), "handoff-rebuild-1")
    first_prepare = next(message for message in self.get_messages(pc) if message["action"] == "playback.prepare")

    state = get_state()
    state._pending_prepares.clear()
    state._handoffs.clear()
    state._handoff_request_index.clear()
    state._playback_contexts.clear()

    phone.emit("message", start_message, namespace="/emo")
    duplicate_ack = self.get_ack(self.get_messages(phone), "handoff-rebuild-1")
    pc_messages = self.get_messages(pc)
    rebuilt_prepare = next(message for message in pc_messages if message["action"] == "playback.prepare")

    self.assertTrue(duplicate_ack["payload"]["duplicate"])
    self.assertEqual(duplicate_ack["payload"]["handoffId"], first_ack["payload"]["handoffId"])
    self.assertEqual(duplicate_ack["payload"]["prepareId"], first_prepare["payload"]["prepareId"])
    self.assertEqual(rebuilt_prepare["payload"]["prepareId"], first_prepare["payload"]["prepareId"])
    self.assertIsNotNone(get_state().get_prepare(first_prepare["payload"]["prepareId"]))

  def test_restart_rejects_second_active_handoff_for_same_context(self):
    capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities=capabilities,
    )
    pc = self.connect_device(
      "alice",
      "Alic3",
      "pc-1",
      "root:pc",
      ["player"],
      capabilities=capabilities,
    )
    tablet = self.connect_device(
      "alice",
      "Alic3",
      "tablet-1",
      "root:tablet",
      ["player"],
      capabilities=capabilities,
    )
    self.get_messages(phone)
    self.get_messages(pc)
    self.get_messages(tablet)
    self.sync_playback_context(phone, "context-queue-1")
    context = get_state().get_playback_context("playback:alice:main")

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.start",
        "requestId": "handoff-before-restart-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "sourceClientId": "phone-1",
          "targetClientId": "pc-1",
          "baseControlVersion": context["controlVersion"],
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(phone), "handoff-before-restart-1")
    self.get_messages(pc)

    state = get_state()
    state._pending_prepares.clear()
    state._handoffs.clear()
    state._handoff_request_index.clear()
    state._playback_contexts.clear()

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.start",
        "requestId": "handoff-after-restart-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "sourceClientId": "phone-1",
          "targetClientId": "tablet-1",
          "baseControlVersion": context["controlVersion"],
        },
      },
      namespace="/emo",
    )
    error = self.get_error(
      self.get_messages(phone),
      "handoff-after-restart-1",
    )
    self.assertEqual(error["payload"]["code"], "conflict")
    self.assertFalse(
      any(
        message["action"] == "playback.prepare"
        for message in self.get_messages(tablet)
      )
    )

  def test_persisted_ready_handoff_retry_resends_player_play(self):
    capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities=capabilities,
    )
    pc = self.connect_device(
      "alice",
      "Alic3",
      "pc-1",
      "root:pc",
      ["player"],
      capabilities=capabilities,
    )
    self.get_messages(phone)
    self.get_messages(pc)
    self.sync_playback_context(phone, "context-queue-1")
    context = get_state().get_playback_context("playback:alice:main")
    start_message = {
      "type": "command",
      "action": "playback.handoff.start",
      "requestId": "handoff-ready-retry-1",
      "payload": {
        "playbackContextId": "playback:alice:main",
        "sourceClientId": "phone-1",
        "targetClientId": "pc-1",
        "baseControlVersion": context["controlVersion"],
      },
    }

    phone.emit("message", start_message, namespace="/emo")
    start_ack = self.get_ack(self.get_messages(phone), "handoff-ready-retry-1")
    prepare = next(
      message
      for message in self.get_messages(pc)
      if message["action"] == "playback.prepare"
    )
    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "handoff-ready-retry-ready-1",
        "payload": {
          "prepareId": prepare["payload"]["prepareId"],
          "ready": True,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )
    ready_messages = self.get_messages(pc)
    self.get_ack(ready_messages, "handoff-ready-retry-ready-1")
    self.assertTrue(
      any(message["action"] == "player.play" for message in ready_messages)
    )

    state = get_state()
    state._pending_prepares.clear()
    state._handoffs.clear()
    state._handoff_request_index.clear()
    state._playback_contexts.clear()

    phone.emit("message", start_message, namespace="/emo")
    duplicate_ack = self.get_ack(
      self.get_messages(phone),
      "handoff-ready-retry-1",
    )
    replayed_messages = self.get_messages(pc)
    replayed_play = next(
      message
      for message in replayed_messages
      if message["action"] == "player.play"
    )
    self.assertTrue(duplicate_ack["payload"]["duplicate"])
    self.assertEqual(duplicate_ack["payload"]["status"], "ready")
    self.assertEqual(
      replayed_play["payload"]["handoffId"],
      start_ack["payload"]["handoffId"],
    )

  def test_v2_handoff_cancel_aborts_pending_prepare(self):
    capabilities = {
      CAPABILITY_PLAYBACK_CONTEXT_V2: True,
      "effectiveAtPlayback": True,
      "playbackPrepare": True,
    }
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities=capabilities,
    )
    pc = self.connect_device(
      "alice",
      "Alic3",
      "pc-1",
      "root:pc",
      ["player"],
      capabilities=capabilities,
    )
    self.get_messages(phone)
    self.get_messages(pc)
    self.create_playback_context(phone, "context-create-1")
    self.get_messages(phone)
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.start",
        "requestId": "v2-handoff-cancel-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "sourceClientId": "phone-1",
          "targetClientId": "pc-1",
          "baseControlVersion": 0,
        },
      },
      namespace="/emo",
    )
    start_ack = self.get_ack(self.get_messages(phone), "v2-handoff-cancel-1")
    prepare = next(message for message in self.get_messages(pc) if message["action"] == "playback.prepare")

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.cancel",
        "requestId": "v2-handoff-cancel-command-1",
        "payload": {
          "handoffId": start_ack["payload"]["handoffId"],
        },
      },
      namespace="/emo",
    )

    cancel_ack = self.get_ack(self.get_messages(phone), "v2-handoff-cancel-command-1")
    pc_messages = self.get_messages(pc)
    release = next(
      message for message in pc_messages if message["action"] == "playback.handoff.release"
    )
    self.assertTrue(cancel_ack["payload"]["canceled"])
    self.assertEqual(release["payload"]["reason"], "canceled")
    self.assertEqual(release["payload"]["handoffId"], start_ack["payload"]["handoffId"])

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "v2-handoff-late-ready-1",
        "payload": {
          "prepareId": prepare["payload"]["prepareId"],
          "ready": True,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )
    late_ready_messages = self.get_messages(pc)
    late_ready_ack = self.get_ack(late_ready_messages, "v2-handoff-late-ready-1")
    self.assertTrue(late_ready_ack["payload"]["ignored"])
    self.assertEqual(late_ready_ack["payload"]["status"], "canceled")
    self.assertFalse(any(message["action"] == "player.play" for message in late_ready_messages))
    self.assertEqual(
      get_state().get_playback_handoff(start_ack["payload"]["handoffId"])["status"],
      "canceled",
    )
    self.assertEqual(
      get_state().get_playback_context("playback:alice:main")["authorityClientId"],
      "phone-1",
    )

  def test_v2_handoff_complete_rejects_session_id(self):
    capabilities = {
      CAPABILITY_PLAYBACK_CONTEXT_V2: True,
      "effectiveAtPlayback": True,
      "playbackPrepare": True,
    }
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities=capabilities,
    )
    pc = self.connect_device(
      "alice",
      "Alic3",
      "pc-1",
      "root:pc",
      ["player"],
      capabilities=capabilities,
    )
    self.get_messages(phone)
    self.get_messages(pc)
    self.create_playback_context(phone, "context-create-1")
    self.get_messages(phone)
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.start",
        "requestId": "v2-handoff-complete-session-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "sourceClientId": "phone-1",
          "targetClientId": "pc-1",
          "baseControlVersion": 0,
        },
      },
      namespace="/emo",
    )
    start_ack = self.get_ack(self.get_messages(phone), "v2-handoff-complete-session-1")
    prepare = next(message for message in self.get_messages(pc) if message["action"] == "playback.prepare")

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "v2-handoff-complete-session-ready-1",
        "payload": {
          "prepareId": prepare["payload"]["prepareId"],
          "ready": True,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(pc), "v2-handoff-complete-session-ready-1")

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.handoff.complete",
        "requestId": "v2-handoff-complete-session-rejected-1",
        "payload": {
          "handoffId": start_ack["payload"]["handoffId"],
          "playbackContextId": "playback:alice:main",
          "sessionId": "legacy-room",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 30100,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )

    error = self.get_error(self.get_messages(pc), "v2-handoff-complete-session-rejected-1")
    self.assertEqual(error["payload"]["code"], "bad_request")
    self.assertEqual(
      get_state().get_playback_context("playback:alice:main")["authorityClientId"],
      "phone-1",
    )

  def test_v2_handoff_cancel_rejects_session_id(self):
    capabilities = {
      CAPABILITY_PLAYBACK_CONTEXT_V2: True,
      "effectiveAtPlayback": True,
      "playbackPrepare": True,
    }
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities=capabilities,
    )
    pc = self.connect_device(
      "alice",
      "Alic3",
      "pc-1",
      "root:pc",
      ["player"],
      capabilities=capabilities,
    )
    self.get_messages(phone)
    self.get_messages(pc)
    self.create_playback_context(phone, "context-create-1")
    self.get_messages(phone)
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.start",
        "requestId": "v2-handoff-cancel-session-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "sourceClientId": "phone-1",
          "targetClientId": "pc-1",
          "baseControlVersion": 0,
        },
      },
      namespace="/emo",
    )
    start_ack = self.get_ack(self.get_messages(phone), "v2-handoff-cancel-session-1")
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.cancel",
        "requestId": "v2-handoff-cancel-session-rejected-1",
        "payload": {
          "handoffId": start_ack["payload"]["handoffId"],
          "sessionId": "legacy-room",
        },
      },
      namespace="/emo",
    )

    error = self.get_error(self.get_messages(phone), "v2-handoff-cancel-session-rejected-1")
    self.assertEqual(error["payload"]["code"], "bad_request")
    self.assertEqual(
      get_state().get_playback_handoff(start_ack["payload"]["handoffId"])["status"],
      "preparing",
    )

  def test_v2_handoff_keeps_context_and_transfers_authority(self):
    capabilities = {
      CAPABILITY_PLAYBACK_CONTEXT_V2: True,
      "effectiveAtPlayback": True,
      "playbackPrepare": True,
    }
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities=capabilities,
    )
    pc = self.connect_device(
      "alice",
      "Alic3",
      "pc-1",
      "root:pc",
      ["player"],
      capabilities=capabilities,
    )
    self.get_messages(phone)
    self.get_messages(pc)
    self.create_playback_context(
      phone,
      "context-create-1",
      queue_song_ids=["song-1", "song-2"],
      current_index=0,
      position_ms=30000,
    )
    self.get_messages(phone)
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.start",
        "requestId": "v2-handoff-phone-pc-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "targetClientId": "pc-1",
          "baseControlVersion": 0,
        },
      },
      namespace="/emo",
    )

    start_ack = self.get_ack(self.get_messages(phone), "v2-handoff-phone-pc-1")
    prepare = next(message for message in self.get_messages(pc) if message["action"] == "playback.prepare")
    handoff = get_state().get_playback_handoff(start_ack["payload"]["handoffId"])
    self.assertEqual(start_ack["payload"]["playbackContextId"], "playback:alice:main")
    self.assertEqual(start_ack["payload"]["sourceClientId"], "phone-1")
    self.assertEqual(start_ack["payload"]["originClientId"], "phone-1")
    self.assertEqual(handoff["originClientId"], "phone-1")
    self.assertEqual(prepare["payload"]["playbackContextId"], "playback:alice:main")
    self.assertNotIn("sessionId", prepare["payload"])

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "v2-handoff-ready-pc-1",
        "payload": {
          "prepareId": prepare["payload"]["prepareId"],
          "ready": True,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(pc), "v2-handoff-ready-pc-1")

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.handoff.complete",
        "requestId": "v2-handoff-complete-pc-1",
        "payload": {
          "handoffId": start_ack["payload"]["handoffId"],
          "playbackContextId": "playback:alice:main",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 30100,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )

    complete_ack = self.get_ack(self.get_messages(pc), "v2-handoff-complete-pc-1")
    phone_messages = self.get_messages(phone)
    release = next(
      message for message in phone_messages if message["action"] == "playback.handoff.release"
    )
    context = get_state().get_playback_context("playback:alice:main")
    self.assertTrue(complete_ack["payload"]["completed"])
    self.assertEqual(complete_ack["payload"]["playbackContextId"], "playback:alice:main")
    self.assertEqual(complete_ack["payload"]["authorityClientId"], "pc-1")
    self.assertEqual(context["playbackContextId"], "playback:alice:main")
    self.assertEqual(context["authorityClientId"], "pc-1")
    self.assertEqual(context["originClientId"], "phone-1")
    self.assertEqual(release["payload"]["reason"], "handoff_completed")
    self.assertNotIn("targetClientId", release)

  def test_v2_handoff_controller_origin_client_id_is_controller(self):
    player_capabilities = {
      CAPABILITY_PLAYBACK_CONTEXT_V2: True,
      "effectiveAtPlayback": True,
      "playbackPrepare": True,
    }
    controller_capabilities = {CAPABILITY_PLAYBACK_CONTEXT_V2: True}
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities=player_capabilities,
    )
    pc = self.connect_device(
      "alice",
      "Alic3",
      "pc-1",
      "root:pc",
      ["player"],
      capabilities=player_capabilities,
    )
    controller = self.connect_device(
      "alice",
      "Alic3",
      "controller-1",
      "root:controller",
      ["controller"],
      capabilities=controller_capabilities,
    )
    self.get_messages(phone)
    self.get_messages(pc)
    self.get_messages(controller)
    self.create_playback_context(phone, "context-create-1")
    self.get_messages(phone)
    self.get_messages(pc)
    self.get_messages(controller)

    controller.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.start",
        "requestId": "v2-controller-handoff-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "sourceClientId": "phone-1",
          "targetClientId": "pc-1",
          "baseControlVersion": 0,
        },
      },
      namespace="/emo",
    )

    ack = self.get_ack(self.get_messages(controller), "v2-controller-handoff-1")
    handoff = get_state().get_playback_handoff(ack["payload"]["handoffId"])
    self.assertEqual(ack["payload"]["originClientId"], "controller-1")
    self.assertEqual(handoff["originClientId"], "controller-1")
    self.assertEqual(handoff["sourceClientId"], "phone-1")

    prepare = next(message for message in self.get_messages(pc) if message["action"] == "playback.prepare")
    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.ready",
        "requestId": "v2-controller-handoff-ready-1",
        "payload": {
          "prepareId": prepare["payload"]["prepareId"],
          "ready": True,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(pc), "v2-controller-handoff-ready-1")

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.handoff.complete",
        "requestId": "v2-controller-handoff-complete-1",
        "payload": {
          "handoffId": ack["payload"]["handoffId"],
          "playbackContextId": "playback:alice:main",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 100,
          "controlVersion": prepare["payload"]["controlVersion"],
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(pc), "v2-controller-handoff-complete-1")
    context = get_state().get_playback_context("playback:alice:main")
    self.assertEqual(context["authorityClientId"], "pc-1")
    self.assertEqual(context["originClientId"], "controller-1")

  def test_v2_handoff_target_requires_prepare_capabilities(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={
        CAPABILITY_PLAYBACK_CONTEXT_V2: True,
        "effectiveAtPlayback": True,
        "playbackPrepare": True,
      },
    )
    pc = self.connect_device(
      "alice",
      "Alic3",
      "pc-1",
      "root:pc",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(pc)
    self.create_playback_context(phone, "context-create-1")
    self.get_messages(phone)
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.start",
        "requestId": "v2-handoff-missing-capability-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "sourceClientId": "phone-1",
          "targetClientId": "pc-1",
          "baseControlVersion": 0,
        },
      },
      namespace="/emo",
    )

    error = self.get_error(self.get_messages(phone), "v2-handoff-missing-capability-1")
    self.assertEqual(error["payload"]["code"], "forbidden")
    self.assertIsNone(next(iter(get_state()._handoffs.values()), None))

  def test_v2_handoff_start_does_not_restore_legacy_queue_as_context(self):
    capabilities = {
      CAPABILITY_PLAYBACK_CONTEXT_V2: True,
      "effectiveAtPlayback": True,
      "playbackPrepare": True,
    }
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities=capabilities,
    )
    pc = self.connect_device(
      "alice",
      "Alic3",
      "pc-1",
      "root:pc",
      ["player"],
      capabilities=capabilities,
    )
    self.get_messages(phone)
    self.get_messages(pc)
    saveQueueState("legacy-room", "alice", "phone-1", ["song-1"], 0, 0)
    self.assertIsNotNone(getQueueState("legacy-room"))
    self.assertIsNone(getPlaybackContextState("legacy-room"))

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "playback.handoff.start",
        "requestId": "v2-handoff-legacy-queue-1",
        "payload": {
          "playbackContextId": "legacy-room",
          "sourceClientId": "phone-1",
          "targetClientId": "pc-1",
          "baseControlVersion": 1,
        },
      },
      namespace="/emo",
    )

    error = self.get_error(self.get_messages(phone), "v2-handoff-legacy-queue-1")
    self.assertEqual(error["payload"]["code"], "not_found")
    self.assertIsNone(getPlaybackContextState("legacy-room"))
    self.assertIsNone(get_state().get_playback_context("legacy-room"))

  def test_v2_playback_update_keeps_device_volume_in_device_state(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.create_playback_context(phone, "context-create-1")

    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "v2-playback-volume-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 100,
          "volume": 65,
          "muted": True,
          "outputDeviceId": "dac-1",
          "audioDeviceName": "USB DAC",
        },
      },
      namespace="/emo",
    )

    ack = self.get_ack(self.get_messages(phone), "v2-playback-volume-1")
    context = get_state().get_playback_context("playback:alice:main")
    persisted_context = getPlaybackContextState("playback:alice:main")
    device_state = getDevicePlaybackState("playback:alice:main", "phone-1")

    self.assertTrue(ack["payload"]["authoritative"])
    self.assertIsNone(context["volume"])
    self.assertIsNone(persisted_context["volume"])
    self.assertNotIn("muted", context)
    self.assertNotIn("outputDeviceId", context)
    self.assertNotIn("audioDeviceName", context)
    self.assertNotIn("muted", persisted_context)
    self.assertNotIn("outputDeviceId", persisted_context)
    self.assertNotIn("audioDeviceName", persisted_context)
    self.assertEqual(device_state["volume"], 65)
    self.assertTrue(device_state["muted"])
    self.assertEqual(device_state["outputDeviceId"], "dac-1")
    self.assertEqual(device_state["audioDeviceName"], "USB DAC")
    self.assertTrue(device_state["isAuthority"])

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.status",
        "requestId": "v2-playback-volume-status-1",
        "payload": {"playbackContextId": "playback:alice:main"},
      },
      namespace="/emo",
    )
    status_ack = self.get_ack(
      self.get_messages(phone),
      "v2-playback-volume-status-1",
    )
    status_context = status_ack["payload"]["playbackContext"]
    status_device = status_ack["payload"]["deviceStates"][0]
    self.assertNotIn("muted", status_context)
    self.assertNotIn("outputDeviceId", status_context)
    self.assertNotIn("audioDeviceName", status_context)
    self.assertNotIn("sessionId", status_device)
    self.assertEqual(status_device["volume"], 65)
    self.assertTrue(status_device["muted"])
    self.assertEqual(status_device["outputDeviceId"], "dac-1")
    self.assertEqual(status_device["audioDeviceName"], "USB DAC")

  def test_v2_context_status_merges_persisted_and_runtime_device_states(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    pc = self.connect_device(
      "alice",
      "Alic3",
      "pc-1",
      "root:pc",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(pc)
    self.create_playback_context(phone, "context-create-1")

    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "status-merge-phone-1",
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
    self.get_ack(self.get_messages(phone), "status-merge-phone-1")

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "status-merge-pc-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:pc",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 900,
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(pc), "status-merge-pc-1")
    self.assertEqual(
      getDevicePlaybackState("playback:alice:main", "pc-1")["positionMs"],
      900,
    )

    get_state()._device_playback_states.clear()
    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "status-merge-phone-2",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 200,
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(phone), "status-merge-phone-2")

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.status",
        "requestId": "status-merge-context-1",
        "payload": {"playbackContextId": "playback:alice:main"},
      },
      namespace="/emo",
    )
    status_ack = self.get_ack(
      self.get_messages(phone),
      "status-merge-context-1",
    )
    device_states = {
      device_state["clientId"]: device_state
      for device_state in status_ack["payload"]["deviceStates"]
    }
    self.assertEqual(device_states["phone-1"]["positionMs"], 200)
    self.assertEqual(device_states["pc-1"]["positionMs"], 900)

  def test_v2_playback_update_serializes_logical_volume_without_volume(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.create_playback_context(phone, "context-create-1")

    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "v2-playback-logical-volume-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 100,
          "logicalVolume": 40,
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(phone), "v2-playback-logical-volume-1")

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.status",
        "requestId": "v2-playback-logical-volume-status-1",
        "payload": {"playbackContextId": "playback:alice:main"},
      },
      namespace="/emo",
    )

    status_ack = self.get_ack(
      self.get_messages(phone),
      "v2-playback-logical-volume-status-1",
    )
    playback_context = status_ack["payload"]["playbackContext"]
    persisted_context = getPlaybackContextState("playback:alice:main")

    self.assertEqual(persisted_context["volume"], 40)
    self.assertEqual(playback_context["logicalVolume"], 40)
    self.assertNotIn("volume", playback_context)

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

  def test_strict_v2_session_id_payload_is_rejected(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)

    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "strict-v2-session-id-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "sessionId": "legacy-room",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 100,
        },
      },
      namespace="/emo",
    )

    error = self.get_error(self.get_messages(phone), "strict-v2-session-id-1")
    self.assertEqual(error["payload"]["code"], "bad_request")

  def test_strict_v2_playback_update_does_not_fallback_to_device_session_id(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)

    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "strict-v2-missing-context-1",
        "payload": {
          "deviceSessionId": "root:phone",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 100,
        },
      },
      namespace="/emo",
    )

    error = self.get_error(self.get_messages(phone), "strict-v2-missing-context-1")
    self.assertEqual(error["payload"]["code"], "bad_request")
    self.assertIsNone(get_state().get_playback_context("root:phone"))

  def test_context_compatible_session_id_payload_is_not_used_as_context_id(self):
    phone = self.connect_device("alice", "Alic3", "phone-1", "root:phone", ["player"])
    self.get_messages(phone)
    self.sync_playback_context(phone, "context-queue-1")
    self.get_messages(phone)

    phone.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "context-compatible-update-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:phone",
          "sessionId": "legacy-wrong-context",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 1234,
        },
      },
      namespace="/emo",
    )

    ack = self.get_ack(self.get_messages(phone), "context-compatible-update-1")
    self.assertEqual(ack["payload"]["playbackContextId"], "playback:alice:main")
    context = get_state().get_playback_context("playback:alice:main")
    self.assertEqual(context["positionMs"], 1234)
    self.assertIsNone(get_state().get_playback_context("legacy-wrong-context"))
    self.assertIsNone(getPlaybackState("root:phone", "phone-1"))
    self.assertIsNone(getPlaybackState("legacy-wrong-context", "phone-1"))

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

  def test_v2_broadcast_start_creates_broadcast_playback_context(self):
    capabilities = {CAPABILITY_PLAYBACK_CONTEXT_V2: True}
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities=capabilities,
    )
    pc = self.connect_device(
      "alice",
      "Alic3",
      "pc-1",
      "root:pc",
      ["player"],
      capabilities=capabilities,
    )
    self.get_messages(phone)
    self.get_messages(pc)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.start",
        "requestId": "v2-broadcast-start-1",
        "payload": {
          "playbackContextId": "broadcast:alice:main",
          "targetMode": "selectedClients",
          "targetClientIds": ["phone-1", "pc-1"],
          "queueSongIds": ["song-1", "song-2"],
          "currentIndex": 0,
          "positionMs": 1000,
          "autoPlay": True,
        },
      },
      namespace="/emo",
    )

    phone_messages = self.get_messages(phone)
    pc_messages = self.get_messages(pc)
    ack = self.get_ack(phone_messages, "v2-broadcast-start-1")
    broadcast_id = ack["payload"]["broadcastId"]
    context = get_state().get_playback_context("broadcast:alice:main")
    pc_start = next(message for message in pc_messages if message["action"] == "broadcast.start")

    self.assertEqual(ack["payload"]["broadcast"]["playbackContextId"], "broadcast:alice:main")
    self.assertEqual(context["contextType"], "broadcast")
    self.assertEqual(context["broadcastId"], broadcast_id)
    self.assertEqual(context["authorityClientId"], "server")
    self.assertEqual(context["participants"], ["phone-1", "pc-1"])
    self.assertEqual(context["queueSongIds"], ["song-1", "song-2"])
    self.assertEqual(context["state"], "playing")
    self.assertEqual(pc_start["payload"]["playbackContextId"], "broadcast:alice:main")
    self.assertEqual(pc_start["payload"]["contextType"], "broadcast")
    self.assertNotIn("sessionId", pc_start["payload"])

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "v2-broadcast-feedback-1",
        "payload": {
          "broadcastId": broadcast_id,
          "playbackContextId": "broadcast:alice:main",
          "deviceSessionId": "root:pc",
          "mode": "broadcast",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 1500,
          "syncDriftMs": -100,
        },
      },
      namespace="/emo",
    )

    feedback_ack = self.get_ack(self.get_messages(pc), "v2-broadcast-feedback-1")
    device_state = get_state().get_device_playback_state("broadcast:alice:main", "pc-1")
    participant_state = get_state().get_broadcast_participant_state(broadcast_id, "pc-1")
    self.assertTrue(feedback_ack["payload"]["participantFeedback"])
    self.assertEqual(device_state["mode"], "broadcast")
    self.assertFalse(device_state["isAuthority"])
    self.assertEqual(device_state["positionMs"], 1500)
    self.assertEqual(participant_state["syncDriftMs"], -100)
    self.assertIsNone(get_state().get_playback_state("root:pc", "pc-1"))

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.status",
        "requestId": "v2-broadcast-status-1",
        "payload": {
          "playbackContextId": "broadcast:alice:main",
        },
      },
      namespace="/emo",
    )
    broadcast_status = self.get_ack(
      self.get_messages(phone),
      "v2-broadcast-status-1",
    )
    self.assertEqual(
      broadcast_status["payload"]["broadcast"]["playbackContextId"],
      "broadcast:alice:main",
    )
    self.assertEqual(
      broadcast_status["payload"]["broadcast"]["contextType"],
      "broadcast",
    )

    phone.emit(
      "message",
      {
        "type": "state",
        "action": "playback.context.status",
        "requestId": "v2-broadcast-context-status-1",
        "payload": {"playbackContextId": "broadcast:alice:main"},
      },
      namespace="/emo",
    )
    context_status = self.get_ack(
      self.get_messages(phone),
      "v2-broadcast-context-status-1",
    )
    context_payload = context_status["payload"]["playbackContext"]
    context_device = next(
      state
      for state in context_status["payload"]["deviceStates"]
      if state["clientId"] == "pc-1"
    )
    self.assertEqual(context_payload["playbackContextId"], "broadcast:alice:main")
    self.assertEqual(context_payload["contextType"], "broadcast")
    self.assertEqual(context_payload["broadcastId"], broadcast_id)
    self.assertEqual(
      context_payload["trackId"],
      broadcast_status["payload"]["broadcast"]["trackId"],
    )
    self.assertNotIn("sessionId", context_payload)
    self.assertNotIn("sourceClientId", context_payload)
    self.assertEqual(context_device["mode"], "broadcast")
    self.assertEqual(context_device["syncDriftMs"], -100)
    self.assertNotIn("sessionId", context_device)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.stop",
        "requestId": "v2-broadcast-stop-1",
        "payload": {
          "broadcastId": broadcast_id,
          "playbackContextId": "broadcast:alice:main",
        },
      },
      namespace="/emo",
    )

    stop_ack = self.get_ack(self.get_messages(phone), "v2-broadcast-stop-1")
    stopped_context = get_state().get_playback_context("broadcast:alice:main")
    self.assertEqual(stop_ack["payload"]["broadcast"]["state"], "stopped")
    self.assertEqual(stopped_context["state"], "stopped")
    self.assertEqual(stopped_context["broadcastId"], broadcast_id)

  def test_v2_broadcast_start_rejects_existing_playback_context_id(self):
    capabilities = {CAPABILITY_PLAYBACK_CONTEXT_V2: True}
    alice = self.connect_device(
      "alice",
      "Alic3",
      "alice-phone",
      "device:alice-phone",
      ["player"],
      capabilities=capabilities,
    )
    bob = self.connect_device(
      "bob",
      "B0b",
      "bob-phone",
      "device:bob-phone",
      ["player"],
      capabilities=capabilities,
    )
    self.get_messages(alice)
    self.get_messages(bob)
    self.create_playback_context(
      alice,
      "alice-context-create-1",
      playback_context_id="shared-context-id",
      device_session_id="device:alice-phone",
    )

    self.start_broadcast(
      bob,
      ["bob-phone"],
      request_id="bob-context-collision-1",
      playbackContextId="shared-context-id",
      autoPlay=False,
    )
    cross_user_error = self.get_error(
      self.get_messages(bob),
      "bob-context-collision-1",
    )
    self.assertEqual(cross_user_error["payload"]["code"], "forbidden")

    self.start_broadcast(
      alice,
      ["alice-phone"],
      request_id="alice-context-collision-1",
      playbackContextId="shared-context-id",
      autoPlay=False,
    )
    same_user_error = self.get_error(
      self.get_messages(alice),
      "alice-context-collision-1",
    )
    self.assertEqual(same_user_error["payload"]["code"], "conflict")

    persisted = getPlaybackContextState("shared-context-id")
    self.assertEqual(persisted["userName"], "alice")
    self.assertNotEqual(persisted.get("contextType"), "broadcast")
    self.assertEqual(get_state().list_broadcasts(user_name="bob"), [])

  def test_v2_broadcast_restores_from_persisted_playback_context(self):
    capabilities = {CAPABILITY_PLAYBACK_CONTEXT_V2: True}
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities=capabilities,
    )
    pc = self.connect_device(
      "alice",
      "Alic3",
      "pc-1",
      "root:pc",
      ["player"],
      capabilities=capabilities,
    )
    self.get_messages(phone)
    self.get_messages(pc)
    self.start_broadcast(
      phone,
      ["phone-1", "pc-1"],
      request_id="broadcast-restart-start-1",
      playbackContextId="broadcast:alice:restart",
      autoPlay=True,
      controlPolicy="owner_only",
    )
    start_ack = self.get_ack(
      self.get_messages(phone),
      "broadcast-restart-start-1",
    )
    broadcast_id = start_ack["payload"]["broadcastId"]
    self.get_messages(pc)

    pc.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "broadcast-restart-feedback-1",
        "payload": {
          "broadcastId": broadcast_id,
          "playbackContextId": "broadcast:alice:restart",
          "deviceSessionId": "root:pc",
          "state": "playing",
          "trackId": "song-1",
          "positionMs": 500,
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(pc), "broadcast-restart-feedback-1")

    state = get_state()
    state._broadcasts.clear()
    state._broadcast_participants.clear()
    state._broadcast_playback_states.clear()
    state._client_active_broadcast.clear()
    state._playback_contexts.clear()

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.status",
        "requestId": "broadcast-restart-status-1",
        "payload": {
          "broadcastId": broadcast_id,
          "playbackContextId": "broadcast:alice:restart",
        },
      },
      namespace="/emo",
    )
    status_ack = self.get_ack(
      self.get_messages(phone),
      "broadcast-restart-status-1",
    )
    restored = get_state().get_broadcast(broadcast_id)
    participant = get_state().get_broadcast_participant_state(
      broadcast_id,
      "pc-1",
    )
    self.assertEqual(status_ack["payload"]["broadcast"]["broadcastId"], broadcast_id)
    self.assertEqual(restored["ownerClientId"], "phone-1")
    self.assertEqual(restored["controlPolicy"], "owner_only")
    self.assertEqual(restored["state"], "playing")
    self.assertEqual(participant["positionMs"], 500)
    self.assertEqual(get_state().get_active_broadcast_for_client("pc-1"), broadcast_id)

    phone.emit(
      "message",
      {
        "type": "command",
        "action": "broadcast.pause",
        "requestId": "broadcast-restart-pause-1",
        "payload": {
          "broadcastId": broadcast_id,
          "playbackContextId": "broadcast:alice:restart",
          "positionMs": 700,
        },
      },
      namespace="/emo",
    )
    pause_ack = self.get_ack(
      self.get_messages(phone),
      "broadcast-restart-pause-1",
    )
    self.assertEqual(pause_ack["payload"]["broadcast"]["state"], "paused")

  def test_v2_broadcast_prepare_keeps_session_id_for_non_v2_target(self):
    owner_capabilities = {
      CAPABILITY_PLAYBACK_CONTEXT_V2: True,
      "effectiveAtPlayback": True,
      "playbackPrepare": True,
    }
    prepare_capabilities = {"effectiveAtPlayback": True, "playbackPrepare": True}
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities=owner_capabilities,
    )
    pc = self.connect_device(
      "alice",
      "Alic3",
      "pc-1",
      "root:pc",
      ["player"],
      capabilities=prepare_capabilities,
    )
    self.get_messages(phone)
    self.get_messages(pc)

    self.start_broadcast(
      phone,
      ["phone-1", "pc-1"],
      request_id="v2-broadcast-mixed-prepare-1",
      playbackContextId="broadcast:alice:mixed",
    )

    phone_messages = self.get_messages(phone)
    pc_messages = self.get_messages(pc)
    ack = self.get_ack(phone_messages, "v2-broadcast-mixed-prepare-1")
    phone_prepare = next(
      message for message in phone_messages if message["action"] == "playback.prepare"
    )
    pc_prepare = next(
      message for message in pc_messages if message["action"] == "playback.prepare"
    )

    self.assertTrue(ack["payload"]["preparing"])
    self.assertEqual(phone_prepare["payload"]["playbackContextId"], "broadcast:alice:mixed")
    self.assertNotIn("sessionId", phone_prepare["payload"])
    self.assertEqual(pc_prepare["payload"]["playbackContextId"], "broadcast:alice:mixed")
    self.assertEqual(pc_prepare["payload"]["sessionId"], "root:pc")

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
    state._clients[("alice", "pc-1")]["lastSeenAt"] = 1
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

  def test_v2_follow_start_subscribes_playback_context_without_source_online(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    laptop = self.connect_device(
      "alice",
      "Alic3",
      "laptop-1",
      "root:laptop",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(laptop)
    self.create_playback_context(
      phone,
      "v2-follow-context-create-1",
      queue_song_ids=["song-source"],
      position_ms=12000,
    )
    self.get_messages(laptop)

    phone.disconnect(namespace="/emo")
    self.clients.remove(phone)
    self.assertIsNone(get_state().get_client("phone-1"))

    laptop.emit(
      "message",
      {
        "type": "state",
        "action": "follow.start",
        "requestId": "v2-follow-start-1",
        "payload": {
          "sourcePlaybackContextId": "playback:alice:main",
          "deviceSessionId": "root:laptop",
        },
      },
      namespace="/emo",
    )

    follow_messages = self.get_messages(laptop)
    follow_ack = self.get_ack(follow_messages, "v2-follow-start-1")
    relationship = follow_ack["payload"]["relationship"]
    self.assertEqual(relationship["sourcePlaybackContextId"], "playback:alice:main")
    self.assertEqual(relationship["sourceClientId"], "phone-1")
    self.assertIsNone(relationship["sourceSessionId"])
    self.assertEqual(follow_ack["payload"]["subscriptions"], ["playback:alice:main"])
    snapshot = next(
      message for message in follow_messages if message["action"] == "playback.context.status"
    )
    playback_context = snapshot["payload"]["playbackContext"]
    self.assertEqual(playback_context["playbackContextId"], "playback:alice:main")
    self.assertEqual(playback_context["queueSongIds"], ["song-source"])
    self.assertNotIn("sessionId", playback_context)
    self.assertEqual(
      get_state().get_follow_relationship("laptop-1")["sourcePlaybackContextId"],
      "playback:alice:main",
    )

    laptop.emit(
      "message",
      {
        "type": "state",
        "action": "follow.stop",
        "requestId": "v2-follow-stop-1",
        "payload": {
          "sourcePlaybackContextId": "playback:alice:main",
          "deviceSessionId": "root:laptop",
        },
      },
      namespace="/emo",
    )

    stop_ack = self.get_ack(self.get_messages(laptop), "v2-follow-stop-1")
    self.assertEqual(stop_ack["payload"]["subscriptions"], [])
    self.assertIsNone(get_state().get_follow_relationship("laptop-1"))

  def test_v2_follow_update_is_device_feedback_only(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    laptop = self.connect_device(
      "alice",
      "Alic3",
      "laptop-1",
      "root:laptop",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(laptop)
    self.create_playback_context(
      phone,
      "v2-follow-feedback-context-create-1",
      queue_song_ids=["song-source"],
      position_ms=12000,
    )
    self.get_messages(phone)
    self.get_messages(laptop)

    laptop.emit(
      "message",
      {
        "type": "state",
        "action": "follow.start",
        "requestId": "v2-follow-feedback-start-1",
        "payload": {
          "sourcePlaybackContextId": "playback:alice:main",
          "deviceSessionId": "root:laptop",
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(laptop), "v2-follow-feedback-start-1")

    laptop.emit(
      "message",
      {
        "type": "event",
        "action": "playback.update",
        "requestId": "v2-follow-feedback-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:laptop",
          "mode": "follow",
          "state": "playing",
          "trackId": "song-source",
          "positionMs": 12300,
          "syncDriftMs": -200,
        },
      },
      namespace="/emo",
    )

    ack = self.get_ack(self.get_messages(laptop), "v2-follow-feedback-1")
    self.assertTrue(ack["payload"]["deviceFeedback"])
    self.assertFalse(ack["payload"]["authoritative"])
    context = get_state().get_playback_context("playback:alice:main")
    device_state = get_state().get_device_playback_state("playback:alice:main", "laptop-1")
    self.assertEqual(context["positionMs"], 12000)
    self.assertEqual(context["authorityClientId"], "phone-1")
    self.assertEqual(device_state["mode"], "follow")
    self.assertFalse(device_state["isAuthority"])
    self.assertEqual(device_state["positionMs"], 12300)

  def test_v2_follow_participant_cannot_control_source_context(self):
    phone = self.connect_device(
      "alice",
      "Alic3",
      "phone-1",
      "root:phone",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    laptop = self.connect_device(
      "alice",
      "Alic3",
      "laptop-1",
      "root:laptop",
      ["player"],
      capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
    )
    self.get_messages(phone)
    self.get_messages(laptop)
    self.create_playback_context(phone, "v2-follow-control-context-create-1")
    self.get_messages(phone)
    self.get_messages(laptop)

    laptop.emit(
      "message",
      {
        "type": "state",
        "action": "follow.start",
        "requestId": "v2-follow-control-start-1",
        "payload": {
          "sourcePlaybackContextId": "playback:alice:main",
          "deviceSessionId": "root:laptop",
        },
      },
      namespace="/emo",
    )
    self.get_ack(self.get_messages(laptop), "v2-follow-control-start-1")

    laptop.emit(
      "message",
      {
        "type": "command",
        "action": "player.seek",
        "requestId": "v2-follow-control-seek-1",
        "payload": {
          "playbackContextId": "playback:alice:main",
          "deviceSessionId": "root:laptop",
          "positionMs": 90000,
        },
      },
      namespace="/emo",
    )

    error = self.get_error(self.get_messages(laptop), "v2-follow-control-seek-1")
    phone_messages = self.get_messages(phone)
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
