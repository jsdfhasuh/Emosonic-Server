import threading
import time


DEFAULT_CLIENT_STALE_SECONDS = 90


class BroadcastInactiveError(Exception):
    pass


class BroadcastVersionMismatchError(Exception):
    def __init__(self, current_version):
        super().__init__("Broadcast version mismatch")
        self.current_version = current_version


class WebSocketState:
    def __init__(self):
        self._lock = threading.RLock()
        # sid -> session metadata for each live Socket.IO connection
        self._sessions = {}
        # clientId -> sid lookup for command routing
        self._client_to_sid = {}
        # clientId -> registered device metadata
        self._clients = {}
        # sessionId -> shared room queue snapshot
        self._queues = {}
        # (sessionId, clientId) -> device-local queue snapshot
        self._local_queues = {}
        # (sessionId, clientId) -> device playback snapshot
        self._playback_states = {}
        # sid -> subscribed sessionIds for passive observers/controllers
        self._session_subscriptions = {}
        # broadcastId -> authoritative broadcast playback state
        self._broadcasts = {}
        # broadcastId -> set(clientId) for playback participants
        self._broadcast_participants = {}
        # (broadcastId, clientId) -> participant execution state
        self._broadcast_playback_states = {}
        # clientId -> active broadcastId for playback participants
        self._client_active_broadcast = {}

    def register_session(self, sid, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._sessions[sid] = {
                "sid": sid,
                "connectedAt": now,
                "lastSeenAt": now,
                "authenticated": False,
                "userName": None,
                "clientId": None,
            }

    def authenticate_session(self, sid, user_name):
        with self._lock:
            session_info = self._sessions.get(sid)
            if session_info is None:
                return None
            session_info["authenticated"] = True
            session_info["userName"] = user_name
            return dict(session_info)

    def get_session(self, sid):
        with self._lock:
            session_info = self._sessions.get(sid)
            return dict(session_info) if session_info is not None else None

    def register_client(self, sid, client_id, info, now=None):
        # Registered device metadata usually includes userName, deviceName,
        # roles, sessionId, capabilities, clientId, and connectedAt.
        now = time.time() if now is None else now
        client_info = dict(info)
        client_info["clientId"] = client_id
        client_info["connectedAt"] = now
        client_info["lastSeenAt"] = now
        with self._lock:
            previous_sid = self._client_to_sid.get(client_id)
            if previous_sid is not None and previous_sid != sid:
                previous_session = self._sessions.get(previous_sid)
                if previous_session is not None:
                    previous_session["clientId"] = None
            self._clients[client_id] = client_info
            self._client_to_sid[client_id] = sid
            session_info = self._sessions.get(sid)
            if session_info is not None:
                session_info["clientId"] = client_id
                session_info["lastSeenAt"] = now
                if client_info.get("userName"):
                    session_info["userName"] = client_info["userName"]
                    session_info["authenticated"] = True
        return dict(client_info)

    def touch_session(self, sid, now=None):
        now = time.time() if now is None else now
        with self._lock:
            session_info = self._sessions.get(sid)
            if session_info is None:
                return None
            session_info["lastSeenAt"] = now
            client_id = session_info.get("clientId")
            if client_id and self._client_to_sid.get(client_id) == sid:
                client_info = self._clients.get(client_id)
                if client_info is not None:
                    client_info["lastSeenAt"] = now
            return dict(session_info)

    def prune_stale_clients(self, stale_after_seconds=DEFAULT_CLIENT_STALE_SECONDS, now=None):
        if stale_after_seconds is None or stale_after_seconds <= 0:
            return []

        now = time.time() if now is None else now
        removed = []
        with self._lock:
            for client_id, client_info in list(self._clients.items()):
                last_seen_at = client_info.get("lastSeenAt") or client_info.get("connectedAt")
                if last_seen_at is None or now - last_seen_at <= stale_after_seconds:
                    continue

                sid = self._client_to_sid.get(client_id)
                if sid is not None:
                    session_info = self._sessions.get(sid)
                    if session_info is not None and session_info.get("clientId") == client_id:
                        session_info["clientId"] = None
                self._client_to_sid.pop(client_id, None)
                removed_client = self._clients.pop(client_id)
                self._mark_broadcast_participant_offline_locked(client_id, now=now)
                removed.append(removed_client)
            return [dict(client) for client in removed]

    def unregister_session(self, sid):
        with self._lock:
            session_info = self._sessions.pop(sid, None)
            if session_info is None:
                return None, None
            self._session_subscriptions.pop(sid, None)
            client_id = session_info.get("clientId")
            client_info = None
            if client_id:
                current_sid = self._client_to_sid.get(client_id)
                if current_sid == sid:
                    self._client_to_sid.pop(client_id, None)
                    client_info = self._clients.pop(client_id, None)
                    self._mark_broadcast_participant_offline_locked(client_id)
            return session_info, client_info

    def get_client(self, client_id):
        with self._lock:
            client = self._clients.get(client_id)
            return dict(client) if client is not None else None

    def get_sid_for_client(self, client_id):
        with self._lock:
            return self._client_to_sid.get(client_id)

    def get_client_for_sid(self, sid):
        # Resolve the current sending device from a live Socket.IO sid.
        with self._lock:
            session_info = self._sessions.get(sid)
            if session_info is None or not session_info.get("clientId"):
                return None
            client = self._clients.get(session_info["clientId"])
            return dict(client) if client is not None else None

    def list_clients(self, user_name=None, session_id=None, stale_after_seconds=None, now=None):
        if stale_after_seconds is not None:
            self.prune_stale_clients(stale_after_seconds, now=now)
        with self._lock:
            clients = []
            for client in self._clients.values():
                if user_name is not None and client.get("userName") != user_name:
                    continue
                if session_id is not None and client.get("sessionId") != session_id:
                    continue
                clients.append(dict(client))
            return clients

    def list_sids(self, user_name=None, session_id=None, exclude_sid=None):
        with self._lock:
            items = []
            for sid, session_info in self._sessions.items():
                if exclude_sid is not None and sid == exclude_sid:
                    continue
                client_id = session_info.get("clientId")
                if not client_id:
                    continue
                client = self._clients.get(client_id)
                if client is None:
                    continue
                if user_name is not None and client.get("userName") != user_name:
                    continue
                if session_id is not None and client.get("sessionId") != session_id:
                    continue
                items.append((sid, dict(client)))
            return items

    def subscribe_session(self, sid, session_id):
        if not session_id:
            return []
        with self._lock:
            subscriptions = self._session_subscriptions.setdefault(sid, set())
            subscriptions.add(session_id)
            return list(subscriptions)

    def unsubscribe_session(self, sid, session_id=None):
        with self._lock:
            if sid not in self._session_subscriptions:
                return []
            if session_id is None:
                self._session_subscriptions[sid].clear()
            else:
                self._session_subscriptions[sid].discard(session_id)
            subscriptions = self._session_subscriptions[sid]
            if not subscriptions:
                self._session_subscriptions.pop(sid, None)
                return []
            return list(subscriptions)

    def list_subscribers(self, session_id, user_name=None, exclude_sid=None):
        # Return passive observers/controllers that subscribed to a sessionId
        # but are not necessarily registered as playback devices in that room.
        with self._lock:
            items = []
            for sid, subscriptions in self._session_subscriptions.items():
                if exclude_sid is not None and sid == exclude_sid:
                    continue
                if session_id not in subscriptions:
                    continue
                session_info = self._sessions.get(sid)
                if session_info is None:
                    continue
                if user_name is not None and session_info.get("userName") != user_name:
                    continue
                items.append(sid)
            return items

    def update_queue(self, session_id, queue_song_ids, current_index=0, position_ms=0, source_client_id=None):
        if not session_id:
            return None
        # Shared room queue keyed only by sessionId.
        queue_state = {
            "sessionId": session_id,
            "queueSongIds": list(queue_song_ids),
            "currentIndex": current_index,
            "positionMs": position_ms,
            "sourceClientId": source_client_id,
            "updatedAt": time.time(),
        }
        with self._lock:
            self._queues[session_id] = queue_state
        return dict(queue_state)

    def update_local_queue(self, session_id, client_id, queue_song_ids, current_index=0, position_ms=0):
        if not session_id or not client_id:
            return None
        # Device-local queue keyed by (sessionId, clientId).
        local_queue = {
            "sessionId": session_id,
            "sourceClientId": client_id,
            "queueSongIds": list(queue_song_ids),
            "currentIndex": current_index,
            "positionMs": position_ms,
            "updatedAt": time.time(),
        }
        with self._lock:
            self._local_queues[(session_id, client_id)] = local_queue
        return dict(local_queue)

    def get_local_queue(self, session_id, client_id):
        with self._lock:
            queue_state = self._local_queues.get((session_id, client_id))
            return dict(queue_state) if queue_state is not None else None

    def list_local_queues(self, session_id):
        with self._lock:
            items = []
            for (queue_session_id, _client_id), queue_state in self._local_queues.items():
                if queue_session_id != session_id:
                    continue
                items.append(dict(queue_state))
            return items

    def clear_local_queue(self, session_id, client_id):
        with self._lock:
            return self._local_queues.pop((session_id, client_id), None)

    def get_queue(self, session_id):
        with self._lock:
            queue_state = self._queues.get(session_id)
            return dict(queue_state) if queue_state is not None else None

    def update_playback_state(self, session_id, client_id, state):
        if not session_id or not client_id:
            return None
        # Playback state is also device-scoped, so the key is (sessionId, clientId).
        playback_state = dict(state)
        playback_state["sessionId"] = session_id
        playback_state["sourceClientId"] = client_id
        playback_state["updatedAt"] = time.time()
        with self._lock:
            self._playback_states[(session_id, client_id)] = playback_state
        return dict(playback_state)

    def get_playback_state(self, session_id, client_id):
        with self._lock:
            playback_state = self._playback_states.get((session_id, client_id))
            return dict(playback_state) if playback_state is not None else None

    def list_playback_states(self, session_id):
        with self._lock:
            items = []
            for (state_session_id, _client_id), playback_state in self._playback_states.items():
                if state_session_id != session_id:
                    continue
                items.append(dict(playback_state))
            return items

    def create_broadcast(
        self,
        broadcast_id,
        user_name,
        owner_client_id,
        participants,
        queue_song_ids,
        current_index=0,
        position_ms=0,
        state_name="stopped",
        control_policy="participants_and_controllers_can_control",
        updated_by_client_id=None,
        now=None,
    ):
        now = time.time() if now is None else now
        participant_ids = list(participants)
        track_id = queue_song_ids[current_index] if queue_song_ids else None
        broadcast = {
            "broadcastId": broadcast_id,
            "userName": user_name,
            "ownerClientId": owner_client_id,
            "participants": participant_ids,
            "queueSongIds": list(queue_song_ids),
            "currentIndex": current_index,
            "trackId": track_id,
            "positionMs": position_ms,
            "state": state_name,
            "version": 1,
            "updatedByClientId": updated_by_client_id or owner_client_id,
            "createdAt": now,
            "updatedAt": now,
            "controlPolicy": control_policy,
        }
        with self._lock:
            self._broadcasts[broadcast_id] = broadcast
            self._broadcast_participants[broadcast_id] = set(participant_ids)
            for client_id in participant_ids:
                self._client_active_broadcast[client_id] = broadcast_id
        return dict(broadcast)

    def get_broadcast(self, broadcast_id):
        with self._lock:
            broadcast = self._broadcasts.get(broadcast_id)
            return dict(broadcast) if broadcast is not None else None

    def list_broadcasts(self, user_name=None):
        with self._lock:
            broadcasts = []
            for broadcast in self._broadcasts.values():
                if user_name is not None and broadcast.get("userName") != user_name:
                    continue
                broadcasts.append(dict(broadcast))
            return broadcasts

    def list_broadcast_participants(self, broadcast_id):
        with self._lock:
            broadcast = self._broadcasts.get(broadcast_id)
            if broadcast is None:
                return []
            return list(broadcast.get("participants") or [])

    def is_broadcast_participant(self, broadcast_id, client_id):
        with self._lock:
            return client_id in self._broadcast_participants.get(broadcast_id, set())

    def get_active_broadcast_for_client(self, client_id):
        with self._lock:
            return self._client_active_broadcast.get(client_id)

    def is_broadcast_active(self, broadcast_id):
        with self._lock:
            return self._is_broadcast_active_locked(broadcast_id)

    def _is_broadcast_active_locked(self, broadcast_id):
        for client_id in self._broadcast_participants.get(broadcast_id, set()):
            if self._client_active_broadcast.get(client_id) == broadcast_id:
                return True
        return False

    def set_active_broadcast_for_client(self, client_id, broadcast_id):
        with self._lock:
            self._client_active_broadcast[client_id] = broadcast_id

    def clear_active_broadcast_for_client(self, client_id, broadcast_id=None):
        with self._lock:
            active_broadcast_id = self._client_active_broadcast.get(client_id)
            if broadcast_id is not None and active_broadcast_id != broadcast_id:
                return
            self._client_active_broadcast.pop(client_id, None)

    def update_broadcast_state(
        self,
        broadcast_id,
        updated_by_client_id,
        queue_song_ids=None,
        current_index=None,
        position_ms=None,
        state_name=None,
        increment_version=True,
        expected_version=None,
        require_active=False,
        now=None,
    ):
        now = time.time() if now is None else now
        with self._lock:
            broadcast = self._broadcasts.get(broadcast_id)
            if broadcast is None:
                return None
            if require_active and not self._is_broadcast_active_locked(broadcast_id):
                raise BroadcastInactiveError("Broadcast is not active")
            if (
                expected_version is not None
                and broadcast.get("version") != expected_version
            ):
                raise BroadcastVersionMismatchError(broadcast.get("version"))

            if queue_song_ids is not None:
                broadcast["queueSongIds"] = list(queue_song_ids)
            if current_index is not None:
                broadcast["currentIndex"] = current_index
            if position_ms is not None:
                broadcast["positionMs"] = position_ms
            if state_name is not None:
                broadcast["state"] = state_name

            queue = broadcast.get("queueSongIds") or []
            index = broadcast.get("currentIndex", 0)
            broadcast["trackId"] = queue[index] if queue and 0 <= index < len(queue) else None
            broadcast["updatedByClientId"] = updated_by_client_id
            broadcast["updatedAt"] = now
            if increment_version:
                broadcast["version"] = broadcast.get("version", 0) + 1
            return dict(broadcast)

    def stop_broadcast(self, broadcast_id, updated_by_client_id, now=None):
        now = time.time() if now is None else now
        with self._lock:
            broadcast = self._broadcasts.get(broadcast_id)
            if broadcast is None:
                return None
            if not self._is_broadcast_active_locked(broadcast_id):
                raise BroadcastInactiveError("Broadcast is not active")

            broadcast["state"] = "stopped"
            queue = broadcast.get("queueSongIds") or []
            index = broadcast.get("currentIndex", 0)
            broadcast["trackId"] = queue[index] if queue and 0 <= index < len(queue) else None
            broadcast["updatedByClientId"] = updated_by_client_id
            broadcast["updatedAt"] = now
            broadcast["version"] = broadcast.get("version", 0) + 1

            for client_id in self._broadcast_participants.get(broadcast_id, set()):
                if self._client_active_broadcast.get(client_id) == broadcast_id:
                    self._client_active_broadcast.pop(client_id, None)
            return dict(broadcast)

    def update_broadcast_participant_state(
        self,
        broadcast_id,
        client_id,
        session_id,
        playback_state=None,
        now=None,
        online=True,
    ):
        now = time.time() if now is None else now
        playback_state = dict(playback_state or {})
        participant_state = {
            "broadcastId": broadcast_id,
            "clientId": client_id,
            "sessionId": session_id,
            "state": playback_state.get("state") or "unknown",
            "trackId": playback_state.get("trackId"),
            "positionMs": playback_state.get("positionMs", 0),
            "syncDriftMs": playback_state.get("syncDriftMs"),
            "online": online,
            "errorCode": playback_state.get("errorCode"),
            "errorMessage": playback_state.get("errorMessage"),
            "lastSeenAt": now,
        }
        with self._lock:
            self._broadcast_playback_states[(broadcast_id, client_id)] = participant_state
        return dict(participant_state)

    def get_broadcast_participant_state(self, broadcast_id, client_id):
        with self._lock:
            participant_state = self._broadcast_playback_states.get((broadcast_id, client_id))
            return dict(participant_state) if participant_state is not None else None

    def list_broadcast_participant_states(self, broadcast_id):
        with self._lock:
            participant_states = []
            for (state_broadcast_id, _client_id), participant_state in self._broadcast_playback_states.items():
                if state_broadcast_id != broadcast_id:
                    continue
                participant_states.append(dict(participant_state))
            return participant_states

    def mark_broadcast_participant_offline(self, client_id, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._mark_broadcast_participant_offline_locked(client_id, now=now)

    def _mark_broadcast_participant_offline_locked(self, client_id, now=None):
        now = time.time() if now is None else now
        for (broadcast_id, state_client_id), participant_state in self._broadcast_playback_states.items():
            if state_client_id != client_id:
                continue
            participant_state["online"] = False
            participant_state["lastSeenAt"] = now


_state = WebSocketState()


def get_state():
    return _state
