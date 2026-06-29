import threading
import time


DEFAULT_CLIENT_STALE_SECONDS = 90
DEFAULT_FOLLOW_DELAY_MS = 700


def _timestamp_ms(now=None):
    now = time.time() if now is None else now
    return int(now * 1000)


def _timestamp_ms_from_payload(payload, now=None):
    if not isinstance(payload, dict):
        return _timestamp_ms(now)
    server_updated_at_ms = payload.get("serverUpdatedAtMs")
    if isinstance(server_updated_at_ms, (int, float)):
        return int(server_updated_at_ms)
    updated_at = payload.get("updatedAt")
    if isinstance(updated_at, (int, float)):
        return int(updated_at * 1000)
    return _timestamp_ms(now)


def _int_or_default(value, default=0):
    if type(value) is int:
        return value
    return default


def _session_timeline_id(session_id, client_id):
    return f"session:{session_id}:client:{client_id}"


def _broadcast_timeline_id(broadcast_id):
    return f"broadcast:{broadcast_id}"


class BroadcastInactiveError(Exception):
    pass


class BroadcastVersionMismatchError(Exception):
    def __init__(self, current_version, current_control_version=None):
        super().__init__("Broadcast version mismatch")
        self.current_version = current_version
        self.current_control_version = current_control_version


class QueueRevisionMismatchError(Exception):
    def __init__(self, current_revision):
        super().__init__("Queue revision mismatch")
        self.current_revision = current_revision


class ClientSeqStaleError(Exception):
    def __init__(self, current_seq):
        super().__init__("Client sequence is stale")
        self.current_seq = current_seq


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
        # timelineId -> authoritative playback ordering metadata
        self._playback_timelines = {}
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
        # follower clientId -> server-owned follow relationship
        self._follow_relationships = {}

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
                self._deactivate_follow_relationships_for_client_locked(client_id, now=now)
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
                    self._deactivate_follow_relationships_for_client_locked(client_id)
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

    def _get_or_create_playback_timeline_locked(self, session_id, client_id):
        timeline_id = _session_timeline_id(session_id, client_id)
        timeline = self._playback_timelines.get(timeline_id)
        if timeline is None:
            timeline = {
                "timelineId": timeline_id,
                "sessionId": session_id,
                "authorityClientId": client_id,
                "originClientId": client_id,
                "trackId": None,
                "state": "stopped",
                "positionMs": 0,
                "playbackRate": 1.0,
                "version": 0,
                "epoch": 0,
                "queueRevision": 0,
                "controlVersion": 0,
                "serverUpdatedAtMs": 0,
                "lastClientSeqByClientInstance": {},
            }
            self._playback_timelines[timeline_id] = timeline
        return timeline

    def _copy_playback_timeline_fields(self, timeline, server_time_ms=None):
        fields = {
            "timelineId": timeline.get("timelineId"),
            "authorityClientId": timeline.get("authorityClientId"),
            "originClientId": timeline.get("originClientId"),
            "version": timeline.get("version", 0),
            "epoch": timeline.get("epoch", 0),
            "queueRevision": timeline.get("queueRevision", 0),
            "controlVersion": timeline.get("controlVersion", 0),
            "serverUpdatedAtMs": timeline.get("serverUpdatedAtMs", 0),
            "updatedAt": (timeline.get("serverUpdatedAtMs", 0) or 0) / 1000,
            "playbackRate": timeline.get("playbackRate", 1.0),
        }
        if server_time_ms is not None:
            fields["serverTimeMs"] = server_time_ms
        return fields

    def update_queue(
        self,
        session_id,
        queue_song_ids,
        current_index=0,
        position_ms=0,
        source_client_id=None,
        expected_queue_revision=None,
        now=None,
    ):
        if not session_id:
            return None
        server_updated_at_ms = _timestamp_ms(now)
        # Shared room queue keyed only by sessionId.
        with self._lock:
            previous_queue = self._queues.get(session_id)
            current_revision = 0
            if previous_queue is not None:
                current_revision = previous_queue.get("queueRevision", 0)
            if (
                expected_queue_revision is not None
                and expected_queue_revision != current_revision
            ):
                raise QueueRevisionMismatchError(current_revision)

            previous_queue_song_ids = (
                [] if previous_queue is None else previous_queue.get("queueSongIds") or []
            )
            previous_index = (
                0 if previous_queue is None else previous_queue.get("currentIndex", 0)
            )
            queue_identity_changed = (
                previous_queue is None
                or previous_queue_song_ids != list(queue_song_ids)
                or previous_index != current_index
            )
            queue_revision = (
                current_revision + 1 if queue_identity_changed else current_revision
            )
            timeline_fields = {}
            if source_client_id:
                timeline = self._get_or_create_playback_timeline_locked(
                    session_id,
                    source_client_id,
                )
                previous_track_id = (
                    previous_queue_song_ids[previous_index]
                    if previous_queue_song_ids
                    and 0 <= previous_index < len(previous_queue_song_ids)
                    else None
                )
                next_track_id = (
                    queue_song_ids[current_index]
                    if queue_song_ids and 0 <= current_index < len(queue_song_ids)
                    else None
                )
                timeline["originClientId"] = source_client_id
                timeline["queueRevision"] = queue_revision
                timeline["controlVersion"] = timeline.get("controlVersion", 0) + 1
                timeline["version"] = timeline.get("version", 0) + 1
                if timeline.get("epoch", 0) == 0:
                    timeline["epoch"] = 1
                elif (
                    queue_identity_changed
                    or previous_track_id != next_track_id
                ):
                    timeline["epoch"] = timeline.get("epoch", 0) + 1
                timeline["trackId"] = next_track_id
                timeline["positionMs"] = position_ms
                timeline["currentIndex"] = current_index
                timeline["queueSongIds"] = list(queue_song_ids)
                timeline["serverUpdatedAtMs"] = server_updated_at_ms
                timeline_fields = self._copy_playback_timeline_fields(
                    timeline,
                )

            queue_state = {
                "sessionId": session_id,
                "queueSongIds": list(queue_song_ids),
                "currentIndex": current_index,
                "positionMs": position_ms,
                "sourceClientId": source_client_id,
                "queueRevision": queue_revision,
                "serverUpdatedAtMs": server_updated_at_ms,
                "updatedAt": server_updated_at_ms / 1000,
            }
            queue_state.update(timeline_fields)
            self._queues[session_id] = queue_state
        return dict(queue_state)

    def restore_queue(self, session_id, queue_state, now=None):
        if not session_id or queue_state is None:
            return None
        queue_song_ids = list(queue_state.get("queueSongIds") or [])
        current_index = _int_or_default(queue_state.get("currentIndex"), 0)
        position_ms = _int_or_default(queue_state.get("positionMs"), 0)
        source_client_id = queue_state.get("sourceClientId")
        queue_revision = _int_or_default(queue_state.get("queueRevision"), 1)
        if queue_revision <= 0:
            queue_revision = 1
        server_updated_at_ms = _timestamp_ms_from_payload(queue_state, now)

        with self._lock:
            timeline_fields = {}
            if source_client_id:
                timeline = self._get_or_create_playback_timeline_locked(
                    session_id,
                    source_client_id,
                )
                next_track_id = (
                    queue_song_ids[current_index]
                    if queue_song_ids and 0 <= current_index < len(queue_song_ids)
                    else None
                )
                version = _int_or_default(
                    queue_state.get("version"),
                    max(timeline.get("version", 0), queue_revision),
                )
                if version <= 0:
                    version = 1
                epoch = _int_or_default(
                    queue_state.get("epoch"),
                    timeline.get("epoch", 0),
                )
                if epoch <= 0:
                    epoch = 1
                control_version = _int_or_default(
                    queue_state.get("controlVersion"),
                    max(timeline.get("controlVersion", 0), version),
                )
                if control_version <= 0:
                    control_version = version

                timeline["originClientId"] = (
                    queue_state.get("originClientId") or source_client_id
                )
                timeline["trackId"] = next_track_id
                timeline["positionMs"] = position_ms
                timeline["currentIndex"] = current_index
                timeline["queueSongIds"] = queue_song_ids
                timeline["queueRevision"] = queue_revision
                timeline["version"] = version
                timeline["epoch"] = epoch
                timeline["controlVersion"] = control_version
                timeline["serverUpdatedAtMs"] = server_updated_at_ms
                timeline["mediaIdentity"] = (
                    next_track_id,
                    current_index,
                    queue_revision,
                    source_client_id,
                )
                timeline_fields = self._copy_playback_timeline_fields(timeline)

            restored = {
                "sessionId": session_id,
                "queueSongIds": queue_song_ids,
                "currentIndex": current_index,
                "positionMs": position_ms,
                "sourceClientId": source_client_id,
                "queueRevision": queue_revision,
                "serverUpdatedAtMs": server_updated_at_ms,
                "updatedAt": server_updated_at_ms / 1000,
            }
            restored.update(timeline_fields)
            self._queues[session_id] = restored
        return dict(restored)

    def update_local_queue(self, session_id, client_id, queue_song_ids, current_index=0, position_ms=0):
        if not session_id or not client_id:
            return None
        server_updated_at_ms = _timestamp_ms()
        # Device-local queue keyed by (sessionId, clientId).
        local_queue = {
            "sessionId": session_id,
            "sourceClientId": client_id,
            "queueSongIds": list(queue_song_ids),
            "currentIndex": current_index,
            "positionMs": position_ms,
            "serverUpdatedAtMs": server_updated_at_ms,
            "updatedAt": server_updated_at_ms / 1000,
        }
        with self._lock:
            self._local_queues[(session_id, client_id)] = local_queue
        return dict(local_queue)

    def restore_local_queue(self, session_id, client_id, queue_state, now=None):
        if not session_id or not client_id or queue_state is None:
            return None
        server_updated_at_ms = _timestamp_ms_from_payload(queue_state, now)
        local_queue = {
            "sessionId": session_id,
            "sourceClientId": client_id,
            "queueSongIds": list(queue_state.get("queueSongIds") or []),
            "currentIndex": _int_or_default(queue_state.get("currentIndex"), 0),
            "positionMs": _int_or_default(queue_state.get("positionMs"), 0),
            "serverUpdatedAtMs": server_updated_at_ms,
            "updatedAt": server_updated_at_ms / 1000,
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

    def update_playback_state(self, session_id, client_id, state, now=None):
        if not session_id or not client_id:
            return None
        # Playback state is also device-scoped, so the key is (sessionId, clientId).
        server_updated_at_ms = _timestamp_ms(now)
        with self._lock:
            previous_state = self._playback_states.get((session_id, client_id))
            timeline = self._get_or_create_playback_timeline_locked(session_id, client_id)

            client_instance_id = state.get("clientInstanceId")
            client_seq = state.get("clientSeq")
            if client_instance_id and type(client_seq) is int:
                last_seq_by_instance = timeline.setdefault(
                    "lastClientSeqByClientInstance",
                    {},
                )
                last_seq = last_seq_by_instance.get(client_instance_id)
                if last_seq is not None and client_seq <= last_seq:
                    raise ClientSeqStaleError(last_seq)
                last_seq_by_instance[client_instance_id] = client_seq

            queue_state = self._queues.get(session_id)
            queue_revision = timeline.get("queueRevision", 0)
            if (
                queue_state is not None
                and queue_state.get("sourceClientId") == client_id
            ):
                queue_revision = queue_state.get("queueRevision", queue_revision)

            playback_state = dict(state)
            playback_state["sessionId"] = session_id
            playback_state["sourceClientId"] = client_id

            playback_rate = playback_state.get("playbackRate", timeline.get("playbackRate", 1.0))
            if not isinstance(playback_rate, (int, float)) or playback_rate <= 0:
                playback_rate = 1.0

            current_index = playback_state.get("currentIndex")
            if not isinstance(current_index, int):
                current_index = None
                if queue_state is not None:
                    current_index = queue_state.get("currentIndex")

            previous_media_identity = timeline.get("mediaIdentity")
            media_identity = (
                playback_state.get("trackId"),
                current_index,
                queue_revision,
                client_id,
            )

            timeline["originClientId"] = playback_state.get("originClientId") or client_id
            timeline["trackId"] = playback_state.get("trackId")
            timeline["state"] = playback_state.get("state") or "unknown"
            timeline["positionMs"] = playback_state.get("positionMs", 0)
            timeline["playbackRate"] = playback_rate
            timeline["queueRevision"] = queue_revision
            timeline["version"] = timeline.get("version", 0) + 1
            if timeline.get("epoch", 0) == 0:
                timeline["epoch"] = 1
            elif previous_media_identity is not None and previous_media_identity != media_identity:
                timeline["epoch"] = timeline.get("epoch", 0) + 1
            elif previous_media_identity is None and previous_state is not None:
                previous_track_id = previous_state.get("trackId")
                if previous_track_id != playback_state.get("trackId"):
                    timeline["epoch"] = timeline.get("epoch", 0) + 1
            timeline["mediaIdentity"] = media_identity
            timeline["serverUpdatedAtMs"] = server_updated_at_ms

            playback_state.update(
                self._copy_playback_timeline_fields(
                    timeline,
                )
            )
            if client_instance_id:
                playback_state["clientInstanceId"] = client_instance_id
            if type(client_seq) is int:
                playback_state["clientSeq"] = client_seq
            playback_state.setdefault("followDelayMs", DEFAULT_FOLLOW_DELAY_MS)
            self._playback_states[(session_id, client_id)] = playback_state
        return dict(playback_state)

    def restore_playback_state(self, session_id, client_id, state, now=None):
        if not session_id or not client_id or state is None:
            return None
        server_updated_at_ms = _timestamp_ms_from_payload(state, now)
        with self._lock:
            queue_state = self._queues.get(session_id)
            timeline = self._get_or_create_playback_timeline_locked(session_id, client_id)

            queue_revision = _int_or_default(
                state.get("queueRevision"),
                timeline.get("queueRevision", 0),
            )
            if (
                queue_state is not None
                and queue_state.get("sourceClientId") == client_id
            ):
                queue_revision = max(
                    queue_revision,
                    _int_or_default(queue_state.get("queueRevision"), queue_revision),
                )

            version = _int_or_default(state.get("version"), timeline.get("version", 0))
            if version <= 0:
                version = 1
            epoch = _int_or_default(state.get("epoch"), timeline.get("epoch", 0))
            if epoch <= 0:
                epoch = 1
            control_version = _int_or_default(
                state.get("controlVersion"),
                timeline.get("controlVersion", version),
            )
            if control_version <= 0:
                control_version = version

            playback_state = dict(state)
            playback_state.pop("serverTimeMs", None)
            playback_state["sessionId"] = session_id
            playback_state["sourceClientId"] = client_id
            playback_state["updatedAt"] = server_updated_at_ms / 1000

            playback_rate = playback_state.get(
                "playbackRate",
                timeline.get("playbackRate", 1.0),
            )
            if not isinstance(playback_rate, (int, float)) or playback_rate <= 0:
                playback_rate = 1.0

            current_index = playback_state.get("currentIndex")
            if not isinstance(current_index, int):
                current_index = None
                if queue_state is not None:
                    current_index = queue_state.get("currentIndex")

            media_identity = (
                playback_state.get("trackId"),
                current_index,
                queue_revision,
                client_id,
            )
            timeline["originClientId"] = playback_state.get("originClientId") or client_id
            timeline["trackId"] = playback_state.get("trackId")
            timeline["state"] = playback_state.get("state") or "unknown"
            timeline["positionMs"] = playback_state.get("positionMs", 0)
            timeline["playbackRate"] = playback_rate
            timeline["queueRevision"] = queue_revision
            timeline["version"] = version
            timeline["epoch"] = epoch
            timeline["controlVersion"] = control_version
            timeline["mediaIdentity"] = media_identity
            timeline["serverUpdatedAtMs"] = server_updated_at_ms

            client_instance_id = playback_state.get("clientInstanceId")
            client_seq = playback_state.get("clientSeq")
            if client_instance_id and type(client_seq) is int:
                timeline.setdefault("lastClientSeqByClientInstance", {})[
                    client_instance_id
                ] = client_seq

            playback_state.update(self._copy_playback_timeline_fields(timeline))
            playback_state.setdefault("followDelayMs", DEFAULT_FOLLOW_DELAY_MS)
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
        server_updated_at_ms = _timestamp_ms(now)
        participant_ids = list(participants)
        track_id = queue_song_ids[current_index] if queue_song_ids else None
        broadcast = {
            "broadcastId": broadcast_id,
            "timelineId": _broadcast_timeline_id(broadcast_id),
            "userName": user_name,
            "ownerClientId": owner_client_id,
            "authorityClientId": "server",
            "originClientId": updated_by_client_id or owner_client_id,
            "participants": participant_ids,
            "queueSongIds": list(queue_song_ids),
            "currentIndex": current_index,
            "trackId": track_id,
            "positionMs": position_ms,
            "state": state_name,
            "playbackRate": 1.0,
            "version": 1,
            "epoch": 1,
            "queueRevision": 1,
            "controlVersion": 1,
            "updatedByClientId": updated_by_client_id or owner_client_id,
            "createdAt": now,
            "serverUpdatedAtMs": server_updated_at_ms,
            "updatedAt": server_updated_at_ms / 1000,
            "followDelayMs": DEFAULT_FOLLOW_DELAY_MS,
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
        increment_queue_revision=False,
        increment_control_version=True,
        require_active=False,
        now=None,
    ):
        now = time.time() if now is None else now
        server_updated_at_ms = _timestamp_ms(now)
        with self._lock:
            broadcast = self._broadcasts.get(broadcast_id)
            if broadcast is None:
                return None
            if require_active and not self._is_broadcast_active_locked(broadcast_id):
                raise BroadcastInactiveError("Broadcast is not active")
            if (
                expected_version is not None
                and broadcast.get("controlVersion", broadcast.get("version")) != expected_version
            ):
                raise BroadcastVersionMismatchError(
                    broadcast.get("version"),
                    broadcast.get("controlVersion", broadcast.get("version")),
                )

            previous_queue = list(broadcast.get("queueSongIds") or [])
            previous_index = broadcast.get("currentIndex", 0)
            previous_track_id = broadcast.get("trackId")
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
            if (
                broadcast.get("epoch", 0) == 0
                or previous_track_id != broadcast.get("trackId")
                or previous_index != index
                or (queue_song_ids is not None and previous_queue != list(queue_song_ids))
            ):
                broadcast["epoch"] = broadcast.get("epoch", 0) + 1
            if increment_queue_revision:
                broadcast["queueRevision"] = broadcast.get("queueRevision", 0) + 1
            if increment_control_version:
                broadcast["controlVersion"] = broadcast.get(
                    "controlVersion",
                    broadcast.get("version", 0),
                ) + 1
            broadcast["originClientId"] = updated_by_client_id
            broadcast["updatedByClientId"] = updated_by_client_id
            broadcast["serverUpdatedAtMs"] = server_updated_at_ms
            broadcast["updatedAt"] = server_updated_at_ms / 1000
            if increment_version:
                broadcast["version"] = broadcast.get("version", 0) + 1
            return dict(broadcast)

    def stop_broadcast(self, broadcast_id, updated_by_client_id, expected_version=None, now=None):
        now = time.time() if now is None else now
        server_updated_at_ms = _timestamp_ms(now)
        with self._lock:
            broadcast = self._broadcasts.get(broadcast_id)
            if broadcast is None:
                return None
            if not self._is_broadcast_active_locked(broadcast_id):
                raise BroadcastInactiveError("Broadcast is not active")
            if (
                expected_version is not None
                and broadcast.get("controlVersion", broadcast.get("version")) != expected_version
            ):
                raise BroadcastVersionMismatchError(
                    broadcast.get("version"),
                    broadcast.get("controlVersion", broadcast.get("version")),
                )

            broadcast["state"] = "stopped"
            queue = broadcast.get("queueSongIds") or []
            index = broadcast.get("currentIndex", 0)
            broadcast["trackId"] = queue[index] if queue and 0 <= index < len(queue) else None
            broadcast["originClientId"] = updated_by_client_id
            broadcast["updatedByClientId"] = updated_by_client_id
            broadcast["controlVersion"] = broadcast.get(
                "controlVersion",
                broadcast.get("version", 0),
            ) + 1
            broadcast["serverUpdatedAtMs"] = server_updated_at_ms
            broadcast["updatedAt"] = server_updated_at_ms / 1000
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

    def start_follow_relationship(
        self,
        follower_client_id,
        follower_session_id,
        source_client_id,
        source_session_id,
        user_name,
        now=None,
    ):
        if not follower_client_id or not source_client_id:
            return None
        now_ms = _timestamp_ms(now)
        relationship = {
            "followerClientId": follower_client_id,
            "followerSessionId": follower_session_id,
            "sourceClientId": source_client_id,
            "sourceSessionId": source_session_id,
            "userName": user_name,
            "active": True,
            "createdAtMs": now_ms,
            "updatedAtMs": now_ms,
        }
        with self._lock:
            existing = self._follow_relationships.get(follower_client_id)
            if existing is not None and existing.get("createdAtMs"):
                relationship["createdAtMs"] = existing["createdAtMs"]
            self._follow_relationships[follower_client_id] = relationship
        return dict(relationship)

    def stop_follow_relationship(self, follower_client_id, now=None):
        now_ms = _timestamp_ms(now)
        with self._lock:
            relationship = self._follow_relationships.get(follower_client_id)
            if relationship is None:
                return None
            relationship["active"] = False
            relationship["updatedAtMs"] = now_ms
            return dict(relationship)

    def get_follow_relationship(self, follower_client_id, active_only=True):
        with self._lock:
            relationship = self._follow_relationships.get(follower_client_id)
            if relationship is None:
                return None
            if active_only and not relationship.get("active"):
                return None
            return dict(relationship)

    def _deactivate_follow_relationships_for_client_locked(self, client_id, now=None):
        now_ms = _timestamp_ms(now)
        for relationship in self._follow_relationships.values():
            if not relationship.get("active"):
                continue
            if (
                relationship.get("followerClientId") != client_id
                and relationship.get("sourceClientId") != client_id
            ):
                continue
            relationship["active"] = False
            relationship["updatedAtMs"] = now_ms


_state = WebSocketState()


def get_state():
    return _state
