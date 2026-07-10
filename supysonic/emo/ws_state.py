import threading
import time


DEFAULT_CLIENT_STALE_SECONDS = 90
DEFAULT_FOLLOW_DELAY_MS = 0


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


def _playback_context_timeline_id(playback_context_id):
    return f"playback:{playback_context_id}"


def _queue_track_id(queue_song_ids, current_index):
    if queue_song_ids and 0 <= current_index < len(queue_song_ids):
        return queue_song_ids[current_index]
    return None


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


class PlaybackAuthorityMismatchError(Exception):
    def __init__(self, current_authority_client_id):
        super().__init__("Playback authority mismatch")
        self.current_authority_client_id = current_authority_client_id


class PlaybackControlVersionMismatchError(Exception):
    def __init__(self, current_control_version):
        super().__init__("Playback control version mismatch")
        self.current_control_version = current_control_version


class PlaybackContextConflictError(Exception):
    def __init__(self, playback_context_id, existing_context=None):
        super().__init__("Playback context already belongs to another resource")
        self.playback_context_id = playback_context_id
        self.existing_context = dict(existing_context or {})


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
        # prepareId -> pending two-phase playback control state
        self._pending_prepares = {}
        # playbackContextId -> server-owned playback context
        self._playback_contexts = {}
        # (playbackContextId, clientId) -> device feedback state
        self._device_playback_states = {}
        # handoffId -> playback authority handoff state
        self._handoffs = {}
        # (userName, requestId) -> handoffId for idempotent start requests
        self._handoff_request_index = {}
        # sid -> subscribed playbackContextIds
        self._playback_context_subscriptions = {}

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
            self._playback_context_subscriptions.pop(sid, None)
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

    def subscribe_playback_context(self, sid, playback_context_id):
        if not playback_context_id:
            return []
        with self._lock:
            subscriptions = self._playback_context_subscriptions.setdefault(sid, set())
            subscriptions.add(playback_context_id)
            return sorted(subscriptions)

    def unsubscribe_playback_context(self, sid, playback_context_id=None):
        with self._lock:
            if sid not in self._playback_context_subscriptions:
                return []
            if playback_context_id is None:
                self._playback_context_subscriptions[sid].clear()
            else:
                self._playback_context_subscriptions[sid].discard(playback_context_id)
            subscriptions = self._playback_context_subscriptions[sid]
            if not subscriptions:
                self._playback_context_subscriptions.pop(sid, None)
                return []
            return sorted(subscriptions)

    def list_playback_context_subscribers(
        self,
        playback_context_id,
        user_name=None,
        exclude_sid=None,
    ):
        with self._lock:
            items = []
            for sid, subscriptions in self._playback_context_subscriptions.items():
                if exclude_sid is not None and sid == exclude_sid:
                    continue
                if playback_context_id not in subscriptions:
                    continue
                session_info = self._sessions.get(sid)
                if session_info is None:
                    continue
                if user_name is not None and session_info.get("userName") != user_name:
                    continue
                items.append(sid)
            return items

    def clear_playback_context_subscriptions(self, playback_context_id):
        if not playback_context_id:
            return 0
        with self._lock:
            cleared = 0
            for sid, subscriptions in list(self._playback_context_subscriptions.items()):
                if playback_context_id not in subscriptions:
                    continue
                subscriptions.discard(playback_context_id)
                cleared += 1
                if not subscriptions:
                    self._playback_context_subscriptions.pop(sid, None)
            return cleared

    def list_context_participant_sids(
        self,
        playback_context_id,
        user_name=None,
        exclude_sid=None,
    ):
        with self._lock:
            context = self._playback_contexts.get(playback_context_id)
            participant_client_ids = set()
            if context is not None and context.get("authorityClientId"):
                participant_client_ids.add(context.get("authorityClientId"))
            for state_context_id, client_id in self._device_playback_states:
                if state_context_id == playback_context_id:
                    participant_client_ids.add(client_id)

            items = []
            for client_id in participant_client_ids:
                sid = self._client_to_sid.get(client_id)
                if sid is None or sid == exclude_sid:
                    continue
                client = self._clients.get(client_id)
                if client is None:
                    continue
                if user_name is not None and client.get("userName") != user_name:
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
        effective_at_server_ms = timeline.get("effectiveAtServerMs")
        if effective_at_server_ms is not None:
            fields["effectiveAtServerMs"] = effective_at_server_ms
        if server_time_ms is not None:
            fields["serverTimeMs"] = server_time_ms
        return fields

    def _strip_expired_effective_at_locked(self, payload, now=None):
        effective_at_server_ms = payload.get("effectiveAtServerMs")
        if not isinstance(effective_at_server_ms, (int, float)):
            payload.pop("effectiveAtServerMs", None)
            return
        if effective_at_server_ms <= _timestamp_ms(now):
            payload.pop("effectiveAtServerMs", None)

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

    def _copy_playback_context_locked(self, context):
        copied = dict(context)
        copied["queueSongIds"] = list(context.get("queueSongIds") or [])
        copied["sessionId"] = context.get("playbackContextId")
        copied["sourceClientId"] = context.get("authorityClientId")
        copied["timelineId"] = context.get("timelineId") or _playback_context_timeline_id(
            context.get("playbackContextId")
        )
        copied["authoritative"] = True
        return copied

    def _get_or_create_playback_context_locked(
        self,
        playback_context_id,
        user_name=None,
        authority_client_id=None,
        queue_song_ids=None,
        current_index=0,
        position_ms=0,
        now=None,
    ):
        context = self._playback_contexts.get(playback_context_id)
        if context is not None:
            if authority_client_id and not context.get("authorityClientId"):
                context["authorityClientId"] = authority_client_id
            if user_name and not context.get("userName"):
                context["userName"] = user_name
            return context

        server_updated_at_ms = _timestamp_ms(now)
        queue_song_ids = list(queue_song_ids or [])
        track_id = _queue_track_id(queue_song_ids, current_index)
        context = {
            "playbackContextId": playback_context_id,
            "sessionId": playback_context_id,
            "userName": user_name,
            "authorityClientId": authority_client_id,
            "originClientId": authority_client_id,
            "timelineId": _playback_context_timeline_id(playback_context_id),
            "queueSongIds": queue_song_ids,
            "currentIndex": current_index,
            "trackId": track_id,
            "state": "stopped",
            "positionMs": position_ms,
            "volume": None,
            "queueRevision": 0,
            "controlVersion": 0,
            "version": 0,
            "epoch": 0,
            "serverUpdatedAtMs": server_updated_at_ms,
            "updatedAt": server_updated_at_ms / 1000,
            "authoritative": True,
        }
        self._playback_contexts[playback_context_id] = context
        return context

    def get_playback_context(self, playback_context_id):
        if not playback_context_id:
            return None
        with self._lock:
            context = self._playback_contexts.get(playback_context_id)
            return None if context is None else self._copy_playback_context_locked(context)

    def create_playback_context(
        self,
        playback_context_id,
        device_session_id,
        user_name,
        authority_client_id,
        queue_song_ids=None,
        current_index=0,
        position_ms=0,
        now=None,
    ):
        if not playback_context_id:
            return None, False
        server_updated_at_ms = _timestamp_ms(now)
        queue_song_ids = list(queue_song_ids or [])
        track_id = _queue_track_id(queue_song_ids, current_index)
        with self._lock:
            context = self._playback_contexts.get(playback_context_id)
            if context is not None:
                return self._copy_playback_context_locked(context), False

            context = {
                "playbackContextId": playback_context_id,
                "sessionId": playback_context_id,
                "userName": user_name,
                "authorityClientId": authority_client_id,
                "originClientId": authority_client_id,
                "timelineId": _playback_context_timeline_id(playback_context_id),
                "queueSongIds": queue_song_ids,
                "currentIndex": current_index,
                "trackId": track_id,
                "state": "stopped",
                "positionMs": position_ms,
                "volume": None,
                "queueRevision": 0,
                "controlVersion": 0,
                "version": 0,
                "epoch": 0,
                "deviceSessionId": device_session_id,
                "serverUpdatedAtMs": server_updated_at_ms,
                "updatedAt": server_updated_at_ms / 1000,
                "authoritative": True,
            }
            self._playback_contexts[playback_context_id] = context
            return self._copy_playback_context_locked(context), True

    def restore_playback_context(self, playback_context_id, payload, now=None):
        if not playback_context_id or payload is None:
            return None
        queue_song_ids = list(payload.get("queueSongIds") or [])
        current_index = _int_or_default(payload.get("currentIndex"), 0)
        server_updated_at_ms = _timestamp_ms_from_payload(payload, now)
        context = {
            "playbackContextId": playback_context_id,
            "sessionId": payload.get("sessionId") or playback_context_id,
            "userName": payload.get("userName"),
            "authorityClientId": payload.get("authorityClientId"),
            "originClientId": payload.get("originClientId"),
            "timelineId": payload.get("timelineId")
            or _playback_context_timeline_id(playback_context_id),
            "queueSongIds": queue_song_ids,
            "currentIndex": current_index,
            "trackId": payload.get("trackId")
            or _queue_track_id(queue_song_ids, current_index),
            "state": payload.get("state") or "stopped",
            "positionMs": _int_or_default(payload.get("positionMs"), 0),
            "volume": payload.get("volume"),
            "queueRevision": max(0, _int_or_default(payload.get("queueRevision"), 1)),
            "controlVersion": max(0, _int_or_default(payload.get("controlVersion"), 1)),
            "version": max(0, _int_or_default(payload.get("version"), 1)),
            "epoch": max(0, _int_or_default(payload.get("epoch"), 1)),
            "serverUpdatedAtMs": server_updated_at_ms,
            "updatedAt": server_updated_at_ms / 1000,
            "authoritative": True,
        }
        for field_name in (
            "contextType",
            "broadcastId",
            "ownerClientId",
            "participants",
            "controlPolicy",
            "followDelayMs",
            "playbackRate",
            "updatedByClientId",
            "effectiveAtServerMs",
        ):
            if field_name in payload:
                context[field_name] = payload[field_name]
        with self._lock:
            self._playback_contexts[playback_context_id] = context
            return self._copy_playback_context_locked(context)

    def update_playback_context_queue(
        self,
        playback_context_id,
        device_session_id,
        queue_song_ids,
        current_index=0,
        position_ms=0,
        source_client_id=None,
        user_name=None,
        expected_queue_revision=None,
        now=None,
    ):
        if not playback_context_id:
            return None
        server_updated_at_ms = _timestamp_ms(now)
        queue_song_ids = list(queue_song_ids or [])
        with self._lock:
            is_new_context = playback_context_id not in self._playback_contexts
            context = self._get_or_create_playback_context_locked(
                playback_context_id,
                user_name=user_name,
                authority_client_id=source_client_id,
                queue_song_ids=queue_song_ids,
                current_index=current_index,
                position_ms=position_ms,
                now=now,
            )
            current_revision = context.get("queueRevision", 0)
            if (
                expected_queue_revision is not None
                and expected_queue_revision != current_revision
            ):
                raise QueueRevisionMismatchError(current_revision)

            previous_queue_song_ids = list(context.get("queueSongIds") or [])
            previous_index = context.get("currentIndex", 0)
            previous_track_id = context.get("trackId")
            next_track_id = _queue_track_id(queue_song_ids, current_index)
            queue_identity_changed = (
                is_new_context
                or previous_queue_song_ids != queue_song_ids
                or previous_index != current_index
            )
            if not context.get("authorityClientId") and source_client_id:
                context["authorityClientId"] = source_client_id
            context["originClientId"] = source_client_id or context.get("originClientId")
            context["userName"] = user_name or context.get("userName")
            context["queueSongIds"] = queue_song_ids
            context["currentIndex"] = current_index
            context["trackId"] = next_track_id
            context["positionMs"] = position_ms
            context["queueRevision"] = (
                current_revision + 1 if queue_identity_changed else current_revision
            )
            context["controlVersion"] = context.get("controlVersion", 0) + 1
            context["version"] = context.get("version", 0) + 1
            if context.get("epoch", 0) == 0:
                context["epoch"] = 1
            elif queue_identity_changed or previous_track_id != next_track_id:
                context["epoch"] = context.get("epoch", 0) + 1
            context["deviceSessionId"] = device_session_id
            context["sessionId"] = playback_context_id
            context["serverUpdatedAtMs"] = server_updated_at_ms
            context["updatedAt"] = server_updated_at_ms / 1000
            return self._copy_playback_context_locked(context)

    def update_existing_playback_context_queue(
        self,
        playback_context_id,
        device_session_id,
        queue_song_ids,
        current_index=0,
        position_ms=0,
        source_client_id=None,
        user_name=None,
        expected_queue_revision=None,
        now=None,
    ):
        if not playback_context_id:
            return None
        server_updated_at_ms = _timestamp_ms(now)
        queue_song_ids = list(queue_song_ids or [])
        with self._lock:
            context = self._playback_contexts.get(playback_context_id)
            if context is None:
                return None

            current_revision = context.get("queueRevision", 0)
            if (
                expected_queue_revision is not None
                and expected_queue_revision != current_revision
            ):
                raise QueueRevisionMismatchError(current_revision)

            previous_queue_song_ids = list(context.get("queueSongIds") or [])
            previous_index = context.get("currentIndex", 0)
            previous_track_id = context.get("trackId")
            next_track_id = _queue_track_id(queue_song_ids, current_index)
            queue_identity_changed = (
                previous_queue_song_ids != queue_song_ids
                or previous_index != current_index
            )
            context["originClientId"] = source_client_id or context.get("originClientId")
            context["userName"] = user_name or context.get("userName")
            context["queueSongIds"] = queue_song_ids
            context["currentIndex"] = current_index
            context["trackId"] = next_track_id
            context["positionMs"] = position_ms
            context["queueRevision"] = (
                current_revision + 1 if queue_identity_changed else current_revision
            )
            context["controlVersion"] = context.get("controlVersion", 0) + 1
            context["version"] = context.get("version", 0) + 1
            if context.get("epoch", 0) == 0:
                context["epoch"] = 1
            elif queue_identity_changed or previous_track_id != next_track_id:
                context["epoch"] = context.get("epoch", 0) + 1
            context["deviceSessionId"] = device_session_id
            context["sessionId"] = playback_context_id
            context["serverUpdatedAtMs"] = server_updated_at_ms
            context["updatedAt"] = server_updated_at_ms / 1000
            return self._copy_playback_context_locked(context)

    def apply_playback_context_control(
        self,
        playback_context_id,
        updated_by_client_id,
        state_name=None,
        position_ms=None,
        current_index=None,
        control_version=None,
        now=None,
    ):
        if not playback_context_id:
            return None
        server_updated_at_ms = _timestamp_ms(now)
        with self._lock:
            context = self._playback_contexts.get(playback_context_id)
            if context is None:
                return None
            if state_name is not None:
                context["state"] = state_name
            if position_ms is not None:
                context["positionMs"] = position_ms
            if current_index is not None:
                if type(current_index) is not int or current_index < 0:
                    raise ValueError("currentIndex must be a non-negative integer")
                queue_song_ids = context.get("queueSongIds") or []
                if current_index >= len(queue_song_ids):
                    raise ValueError("currentIndex is out of bounds")
                previous_index = context.get("currentIndex", 0)
                previous_track_id = context.get("trackId")
                context["currentIndex"] = current_index
                context["trackId"] = queue_song_ids[current_index]
                if (
                    previous_index != current_index
                    or previous_track_id != context.get("trackId")
                ):
                    context["queueRevision"] = context.get("queueRevision", 0) + 1
                    context["epoch"] = max(1, context.get("epoch", 0) + 1)
            context["originClientId"] = updated_by_client_id or context.get("originClientId")
            if control_version is None:
                control_version = context.get("controlVersion", 0) + 1
            context["controlVersion"] = control_version
            context["version"] = context.get("version", 0) + 1
            context["serverUpdatedAtMs"] = server_updated_at_ms
            context["updatedAt"] = server_updated_at_ms / 1000
            return self._copy_playback_context_locked(context)

    def close_playback_context(
        self,
        playback_context_id,
        updated_by_client_id=None,
        state_name="closed",
        now=None,
    ):
        if not playback_context_id:
            return None
        server_updated_at_ms = _timestamp_ms(now)
        with self._lock:
            context = self._playback_contexts.get(playback_context_id)
            if context is None:
                return None
            context["state"] = state_name
            context["originClientId"] = updated_by_client_id or context.get("originClientId")
            context["controlVersion"] = context.get("controlVersion", 0) + 1
            context["version"] = context.get("version", 0) + 1
            context["serverUpdatedAtMs"] = server_updated_at_ms
            context["updatedAt"] = server_updated_at_ms / 1000
            return self._copy_playback_context_locked(context)

    def record_device_playback_state(
        self,
        playback_context_id,
        device_session_id,
        client_id,
        user_name,
        playback_state,
        is_authority=False,
        mode="normal",
        now=None,
    ):
        if not playback_context_id or not client_id:
            return None
        server_updated_at_ms = _timestamp_ms(now)
        payload = dict(playback_state or {})
        payload.update(
            {
                "playbackContextId": playback_context_id,
                "deviceSessionId": device_session_id,
                "sessionId": device_session_id,
                "sourceClientId": client_id,
                "state": payload.get("state") or "unknown",
                "trackId": payload.get("trackId"),
                "positionMs": payload.get("positionMs", 0),
                "volume": payload.get("volume"),
                "isAuthority": bool(is_authority),
                "mode": mode,
                "serverUpdatedAtMs": server_updated_at_ms,
                "updatedAt": server_updated_at_ms / 1000,
            }
        )
        with self._lock:
            if is_authority:
                for (state_context_id, _client_id), device_state in self._device_playback_states.items():
                    if state_context_id == playback_context_id:
                        device_state["isAuthority"] = False
            self._device_playback_states[(playback_context_id, client_id)] = payload
            return dict(payload)

    def get_device_playback_state(self, playback_context_id, client_id):
        with self._lock:
            playback_state = self._device_playback_states.get(
                (playback_context_id, client_id)
            )
            return dict(playback_state) if playback_state is not None else None

    def list_device_playback_states(self, playback_context_id):
        with self._lock:
            states = []
            for (state_context_id, _client_id), playback_state in self._device_playback_states.items():
                if state_context_id != playback_context_id:
                    continue
                states.append(dict(playback_state))
            return states

    def apply_authority_playback_update(
        self,
        playback_context_id,
        device_session_id,
        client_id,
        user_name,
        playback_state,
        create_if_missing=True,
        now=None,
    ):
        if not playback_context_id or not client_id:
            return None, False
        server_updated_at_ms = _timestamp_ms(now)
        playback_state = dict(playback_state or {})
        with self._lock:
            if create_if_missing:
                context = self._get_or_create_playback_context_locked(
                    playback_context_id,
                    user_name=user_name,
                    authority_client_id=client_id,
                    now=now,
                )
            else:
                context = self._playback_contexts.get(playback_context_id)
                if context is None:
                    return None, False
            authority_client_id = context.get("authorityClientId")
            authoritative = authority_client_id in (None, client_id)
            if not authoritative:
                return self._copy_playback_context_locked(context), False

            previous_track_id = context.get("trackId")
            previous_index = context.get("currentIndex", 0)
            previous_queue_song_ids = list(context.get("queueSongIds") or [])
            if not authority_client_id:
                context["authorityClientId"] = client_id
            context["originClientId"] = playback_state.get("originClientId") or client_id
            context["userName"] = user_name or context.get("userName")
            if "queueSongIds" in playback_state:
                queue_song_ids = playback_state.get("queueSongIds")
                if not isinstance(queue_song_ids, list):
                    raise ValueError("queueSongIds must be a list")
                context["queueSongIds"] = list(queue_song_ids)
            if "currentIndex" in playback_state:
                if type(playback_state.get("currentIndex")) is not int:
                    raise ValueError("currentIndex must be an integer")
                context["currentIndex"] = playback_state["currentIndex"]
            queue_song_ids = context.get("queueSongIds") or []
            current_index = context.get("currentIndex", 0)
            if current_index < 0:
                raise ValueError("currentIndex must be a non-negative integer")
            if queue_song_ids and current_index >= len(queue_song_ids):
                raise ValueError("currentIndex is out of bounds")
            if not queue_song_ids and current_index != 0:
                raise ValueError("empty queue must use currentIndex=0")
            if "trackId" in playback_state:
                context["trackId"] = playback_state.get("trackId")
            elif (
                previous_queue_song_ids != queue_song_ids
                or previous_index != current_index
                or context.get("trackId") is None
            ):
                context["trackId"] = _queue_track_id(
                    queue_song_ids,
                    current_index,
                )
            if "state" in playback_state:
                context["state"] = playback_state.get("state") or "unknown"
            if "positionMs" in playback_state:
                if (
                    type(playback_state.get("positionMs")) is not int
                    or playback_state.get("positionMs") < 0
                ):
                    raise ValueError("positionMs must be a non-negative integer")
                context["positionMs"] = playback_state.get("positionMs", 0)
            if "logicalVolume" in playback_state:
                context["volume"] = playback_state.get("logicalVolume")
            context["deviceSessionId"] = device_session_id
            context["serverUpdatedAtMs"] = server_updated_at_ms
            context["updatedAt"] = server_updated_at_ms / 1000
            context["version"] = context.get("version", 0) + 1
            if context.get("epoch", 0) == 0:
                context["epoch"] = 1
            elif (
                previous_track_id != context.get("trackId")
                or previous_index != context.get("currentIndex", 0)
            ):
                context["epoch"] = context.get("epoch", 0) + 1
            if context.get("controlVersion", 0) <= 0:
                context["controlVersion"] = 1
            queue_identity_changed = (
                previous_queue_song_ids != context.get("queueSongIds", [])
                or previous_index != context.get("currentIndex", 0)
            )
            if context.get("queueRevision", 0) <= 0:
                context["queueRevision"] = 1
            elif queue_identity_changed:
                context["queueRevision"] = context.get("queueRevision", 0) + 1
            return self._copy_playback_context_locked(context), True

    def transfer_playback_authority(
        self,
        playback_context_id,
        source_client_id,
        target_client_id,
        expected_control_version=None,
        next_control_version=None,
        playback_state=None,
        origin_client_id=None,
        now=None,
    ):
        if not playback_context_id:
            return None
        server_updated_at_ms = _timestamp_ms(now)
        playback_state = dict(playback_state or {})
        with self._lock:
            context = self._playback_contexts.get(playback_context_id)
            if context is None:
                return None
            current_authority = context.get("authorityClientId")
            if current_authority != source_client_id:
                raise PlaybackAuthorityMismatchError(current_authority)
            current_control_version = context.get("controlVersion", 0)
            if (
                expected_control_version is not None
                and expected_control_version != current_control_version
            ):
                raise PlaybackControlVersionMismatchError(current_control_version)

            previous_track_id = context.get("trackId")
            previous_index = context.get("currentIndex", 0)
            context["authorityClientId"] = target_client_id
            context["originClientId"] = origin_client_id or source_client_id
            if "queueSongIds" in playback_state:
                context["queueSongIds"] = list(playback_state.get("queueSongIds") or [])
            if isinstance(playback_state.get("currentIndex"), int):
                context["currentIndex"] = playback_state["currentIndex"]
            if "trackId" in playback_state:
                context["trackId"] = playback_state.get("trackId")
            if "state" in playback_state:
                context["state"] = playback_state.get("state") or "unknown"
            if "positionMs" in playback_state:
                context["positionMs"] = playback_state.get("positionMs", 0)
            if "logicalVolume" in playback_state:
                context["volume"] = playback_state.get("logicalVolume")
            context["controlVersion"] = (
                next_control_version
                if type(next_control_version) is int
                else current_control_version + 1
            )
            context["version"] = context.get("version", 0) + 1
            if context.get("epoch", 0) == 0:
                context["epoch"] = 1
            elif (
                previous_track_id != context.get("trackId")
                or previous_index != context.get("currentIndex", 0)
            ):
                context["epoch"] = context.get("epoch", 0) + 1
            else:
                context["epoch"] = context.get("epoch", 0) + 1
            context["serverUpdatedAtMs"] = server_updated_at_ms
            context["updatedAt"] = server_updated_at_ms / 1000
            return self._copy_playback_context_locked(context)

    def create_playback_handoff(
        self,
        handoff_id,
        request_id,
        playback_context_id,
        user_name,
        source_client_id,
        target_client_id,
        base_control_version,
        control_version,
        snapshot,
        prepare_id=None,
        origin_client_id=None,
        now=None,
    ):
        now_ms = _timestamp_ms(now)
        origin_client_id = origin_client_id or source_client_id
        request_key = (
            (user_name, origin_client_id, request_id)
            if user_name and origin_client_id and request_id
            else None
        )
        with self._lock:
            if request_key and request_key in self._handoff_request_index:
                existing_id = self._handoff_request_index[request_key]
                existing = self._handoffs.get(existing_id)
                if existing is not None:
                    return dict(existing)
            for handoff in self._handoffs.values():
                if handoff.get("playbackContextId") != playback_context_id:
                    continue
                if handoff.get("status") in ("preparing", "ready", "committed"):
                    raise PlaybackAuthorityMismatchError(
                        handoff.get("sourceClientId")
                    )
            handoff = {
                "handoffId": handoff_id,
                "requestId": request_id,
                "playbackContextId": playback_context_id,
                "userName": user_name,
                "sourceClientId": source_client_id,
                "targetClientId": target_client_id,
                "originClientId": origin_client_id,
                "status": "preparing",
                "baseControlVersion": base_control_version,
                "controlVersion": control_version,
                "snapshot": dict(snapshot or {}),
                "createdAtMs": now_ms,
                "updatedAtMs": now_ms,
            }
            if prepare_id is not None:
                handoff["prepareId"] = prepare_id
            self._handoffs[handoff_id] = handoff
            if request_key:
                self._handoff_request_index[request_key] = handoff_id
            return dict(handoff)

    def get_playback_handoff(self, handoff_id):
        with self._lock:
            handoff = self._handoffs.get(handoff_id)
            return dict(handoff) if handoff is not None else None

    def get_playback_handoff_by_request(self, user_name, origin_client_id, request_id):
        if not user_name or not origin_client_id or not request_id:
            return None
        with self._lock:
            handoff_id = self._handoff_request_index.get(
                (user_name, origin_client_id, request_id)
            )
            if handoff_id is None:
                return None
            handoff = self._handoffs.get(handoff_id)
            return dict(handoff) if handoff is not None else None

    def update_playback_handoff(
        self,
        handoff_id,
        status=None,
        error_code=None,
        error_message=None,
        prepare_id=None,
        complete_expires_at_ms=None,
        now=None,
    ):
        now_ms = _timestamp_ms(now)
        with self._lock:
            handoff = self._handoffs.get(handoff_id)
            if handoff is None:
                return None
            if status is not None:
                handoff["status"] = status
            if error_code is not None:
                handoff["errorCode"] = error_code
            if error_message is not None:
                handoff["errorMessage"] = error_message
            if prepare_id is not None:
                handoff["prepareId"] = prepare_id
            if complete_expires_at_ms is not None:
                handoff["completeExpiresAtMs"] = complete_expires_at_ms
            handoff["updatedAtMs"] = now_ms
            return dict(handoff)

    def expire_playback_handoff_if_status(
        self,
        handoff_id,
        allowed_statuses,
        status,
        error_code=None,
        error_message=None,
        now=None,
    ):
        now_ms = _timestamp_ms(now)
        with self._lock:
            handoff = self._handoffs.get(handoff_id)
            if handoff is None:
                return None
            if handoff.get("status") not in set(allowed_statuses or []):
                return None
            expires_at_ms = handoff.get("completeExpiresAtMs")
            if expires_at_ms is None or now_ms < expires_at_ms:
                return None
            handoff["status"] = status
            if error_code is not None:
                handoff["errorCode"] = error_code
            if error_message is not None:
                handoff["errorMessage"] = error_message
            handoff["updatedAtMs"] = now_ms
            return dict(handoff)

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
            timeline.pop("effectiveAtServerMs", None)
            playback_state.pop("effectiveAtServerMs", None)

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

    def update_playback_control(
        self,
        session_id,
        client_id,
        state_name=None,
        track_id=None,
        position_ms=None,
        queue_song_ids=None,
        current_index=None,
        updated_by_client_id=None,
        effective_at_server_ms=None,
        control_version=None,
        now=None,
    ):
        if not session_id or not client_id:
            return None
        server_updated_at_ms = _timestamp_ms(now)
        with self._lock:
            previous_state = self._playback_states.get((session_id, client_id)) or {}
            queue_state = self._queues.get(session_id)
            local_queue_state = self._local_queues.get((session_id, client_id))
            timeline = self._get_or_create_playback_timeline_locked(session_id, client_id)

            if queue_song_ids is None:
                if local_queue_state is not None:
                    queue_song_ids = local_queue_state.get("queueSongIds")
                elif queue_state is not None:
                    queue_song_ids = queue_state.get("queueSongIds")
            queue_song_ids = list(queue_song_ids or previous_state.get("queueSongIds") or [])

            if not isinstance(current_index, int):
                if local_queue_state is not None:
                    current_index = local_queue_state.get("currentIndex", 0)
                elif queue_state is not None:
                    current_index = queue_state.get("currentIndex", 0)
                else:
                    current_index = previous_state.get("currentIndex", 0)
            if not isinstance(current_index, int):
                current_index = 0

            if track_id is None:
                if queue_song_ids and 0 <= current_index < len(queue_song_ids):
                    track_id = queue_song_ids[current_index]
                else:
                    track_id = previous_state.get("trackId")

            if position_ms is None:
                position_ms = previous_state.get("positionMs", 0)
            position_ms = _int_or_default(position_ms, 0)

            if state_name is None:
                state_name = previous_state.get("state") or "stopped"

            queue_revision = timeline.get("queueRevision", 0)
            if queue_state is not None and queue_state.get("sourceClientId") == client_id:
                queue_revision = queue_state.get("queueRevision", queue_revision)

            previous_media_identity = timeline.get("mediaIdentity")
            media_identity = (track_id, current_index, queue_revision, client_id)

            timeline["originClientId"] = updated_by_client_id or client_id
            timeline["trackId"] = track_id
            timeline["state"] = state_name
            timeline["positionMs"] = position_ms
            timeline["playbackRate"] = previous_state.get("playbackRate", timeline.get("playbackRate", 1.0))
            timeline["queueRevision"] = queue_revision
            timeline["version"] = timeline.get("version", 0) + 1
            if type(control_version) is int:
                timeline["controlVersion"] = control_version
            else:
                timeline["controlVersion"] = timeline.get("controlVersion", 0) + 1
            if timeline.get("epoch", 0) == 0:
                timeline["epoch"] = 1
            elif previous_media_identity is not None and previous_media_identity != media_identity:
                timeline["epoch"] = timeline.get("epoch", 0) + 1
            timeline["mediaIdentity"] = media_identity
            timeline["serverUpdatedAtMs"] = server_updated_at_ms
            if effective_at_server_ms is not None and state_name == "playing":
                timeline["effectiveAtServerMs"] = effective_at_server_ms
            else:
                timeline.pop("effectiveAtServerMs", None)

            playback_state = dict(previous_state)
            playback_state.update(
                {
                    "sessionId": session_id,
                    "sourceClientId": client_id,
                    "state": state_name,
                    "trackId": track_id,
                    "positionMs": position_ms,
                    "queueSongIds": queue_song_ids,
                    "currentIndex": current_index,
                    "queueType": playback_state.get("queueType") or "session",
                }
            )
            playback_state.update(self._copy_playback_timeline_fields(timeline))
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
            self._strip_expired_effective_at_locked(playback_state, now=now)
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
            if "effectiveAtServerMs" in playback_state:
                timeline["effectiveAtServerMs"] = playback_state["effectiveAtServerMs"]
            else:
                timeline.pop("effectiveAtServerMs", None)

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
        effective_at_server_ms=None,
        playback_context_id=None,
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
        if playback_context_id is not None:
            broadcast["playbackContextId"] = playback_context_id
        if effective_at_server_ms is not None and state_name == "playing":
            broadcast["effectiveAtServerMs"] = effective_at_server_ms
        with self._lock:
            self._broadcasts[broadcast_id] = broadcast
            self._broadcast_participants[broadcast_id] = set(participant_ids)
            for client_id in participant_ids:
                self._client_active_broadcast[client_id] = broadcast_id
        return dict(broadcast)

    def upsert_broadcast_playback_context(
        self,
        playback_context_id,
        broadcast_id,
        user_name,
        authority_client_id,
        origin_client_id,
        participants,
        queue_song_ids,
        owner_client_id=None,
        control_policy=None,
        follow_delay_ms=None,
        current_index=0,
        position_ms=0,
        state_name="stopped",
        queue_revision=None,
        control_version=None,
        version=None,
        epoch=None,
        timeline_id=None,
        now=None,
    ):
        if not playback_context_id:
            return None
        server_updated_at_ms = _timestamp_ms(now)
        queue_song_ids = list(queue_song_ids or [])
        track_id = _queue_track_id(queue_song_ids, current_index)
        with self._lock:
            existing = self._playback_contexts.get(playback_context_id)
            if existing is not None and not (
                existing.get("userName") == user_name
                and existing.get("contextType") == "broadcast"
                and existing.get("broadcastId") == broadcast_id
            ):
                raise PlaybackContextConflictError(
                    playback_context_id,
                    existing_context=existing,
                )
            if existing is None:
                existing = {
                    "playbackContextId": playback_context_id,
                    "sessionId": playback_context_id,
                    "volume": None,
                    "queueRevision": 0,
                    "controlVersion": 0,
                    "version": 0,
                    "epoch": 0,
                    "authoritative": True,
                }
                self._playback_contexts[playback_context_id] = existing

            existing.update(
                {
                    "contextType": "broadcast",
                    "broadcastId": broadcast_id,
                    "userName": user_name,
                    "authorityClientId": authority_client_id,
                    "originClientId": origin_client_id,
                    "ownerClientId": owner_client_id,
                    "timelineId": timeline_id
                    or _playback_context_timeline_id(playback_context_id),
                    "participants": list(participants or []),
                    "queueSongIds": queue_song_ids,
                    "currentIndex": current_index,
                    "trackId": track_id,
                    "state": state_name,
                    "positionMs": position_ms,
                    "serverUpdatedAtMs": server_updated_at_ms,
                    "updatedAt": server_updated_at_ms / 1000,
                    "authoritative": True,
                }
            )
            if control_policy is not None:
                existing["controlPolicy"] = control_policy
            if follow_delay_ms is not None:
                existing["followDelayMs"] = follow_delay_ms
            if queue_revision is not None:
                existing["queueRevision"] = queue_revision
            if control_version is not None:
                existing["controlVersion"] = control_version
            if version is not None:
                existing["version"] = version
            if epoch is not None:
                existing["epoch"] = epoch
            return self._copy_playback_context_locked(existing)

    def restore_broadcast_playback_context(self, playback_context):
        if not isinstance(playback_context, dict):
            return None
        playback_context_id = playback_context.get("playbackContextId")
        broadcast_id = playback_context.get("broadcastId")
        if (
            not playback_context_id
            or not broadcast_id
            or playback_context.get("contextType") != "broadcast"
        ):
            return None

        participants = list(playback_context.get("participants") or [])
        queue_song_ids = list(playback_context.get("queueSongIds") or [])
        current_index = _int_or_default(playback_context.get("currentIndex"), 0)
        state_name = playback_context.get("state") or "stopped"
        broadcast = {
            "broadcastId": broadcast_id,
            "playbackContextId": playback_context_id,
            "timelineId": playback_context.get("timelineId")
            or _broadcast_timeline_id(broadcast_id),
            "userName": playback_context.get("userName"),
            "ownerClientId": playback_context.get("ownerClientId")
            or playback_context.get("originClientId"),
            "authorityClientId": playback_context.get("authorityClientId")
            or "server",
            "originClientId": playback_context.get("originClientId"),
            "participants": participants,
            "queueSongIds": queue_song_ids,
            "currentIndex": current_index,
            "trackId": playback_context.get("trackId")
            or _queue_track_id(queue_song_ids, current_index),
            "positionMs": _int_or_default(playback_context.get("positionMs"), 0),
            "state": state_name,
            "playbackRate": playback_context.get("playbackRate", 1.0),
            "version": _int_or_default(playback_context.get("version"), 0),
            "epoch": _int_or_default(playback_context.get("epoch"), 0),
            "queueRevision": _int_or_default(
                playback_context.get("queueRevision"),
                0,
            ),
            "controlVersion": _int_or_default(
                playback_context.get("controlVersion"),
                0,
            ),
            "updatedByClientId": playback_context.get("originClientId"),
            "serverUpdatedAtMs": _timestamp_ms_from_payload(playback_context),
            "updatedAt": playback_context.get("updatedAt"),
            "followDelayMs": playback_context.get(
                "followDelayMs",
                DEFAULT_FOLLOW_DELAY_MS,
            ),
            "controlPolicy": playback_context.get("controlPolicy")
            or "participants_and_controllers_can_control",
        }
        if playback_context.get("effectiveAtServerMs") is not None:
            broadcast["effectiveAtServerMs"] = playback_context.get(
                "effectiveAtServerMs"
            )

        with self._lock:
            existing = self._broadcasts.get(broadcast_id)
            if existing is not None:
                if existing.get("playbackContextId") != playback_context_id:
                    raise PlaybackContextConflictError(
                        playback_context_id,
                        existing_context=playback_context,
                    )
                return dict(existing)
            self._broadcasts[broadcast_id] = broadcast
            self._broadcast_participants[broadcast_id] = set(participants)
            if state_name not in ("stopped", "ended", "closed", "expired"):
                for client_id in participants:
                    self._client_active_broadcast[client_id] = broadcast_id
            return dict(broadcast)

    def get_broadcast(self, broadcast_id):
        with self._lock:
            broadcast = self._broadcasts.get(broadcast_id)
            return dict(broadcast) if broadcast is not None else None

    def get_broadcast_by_playback_context(self, playback_context_id):
        if not playback_context_id:
            return None
        with self._lock:
            for broadcast in self._broadcasts.values():
                if broadcast.get("playbackContextId") == playback_context_id:
                    return dict(broadcast)
            return None

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
        effective_at_server_ms=None,
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
            if effective_at_server_ms is not None and broadcast.get("state") == "playing":
                broadcast["effectiveAtServerMs"] = effective_at_server_ms
            elif state_name is not None and state_name != "playing":
                broadcast.pop("effectiveAtServerMs", None)
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
            broadcast.pop("effectiveAtServerMs", None)
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
        source_playback_context_id=None,
        now=None,
    ):
        if not follower_client_id or (
            not source_client_id and not source_playback_context_id
        ):
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
        if source_playback_context_id is not None:
            relationship["sourcePlaybackContextId"] = source_playback_context_id
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

    def create_prepare(
        self,
        prepare_id,
        action,
        timeline_id,
        target_client_ids,
        required_client_ids,
        control_version,
        commit_payload,
        created_at_ms,
        expires_at_ms,
        request_sid=None,
        request_id=None,
    ):
        prepare = {
            "prepareId": prepare_id,
            "action": action,
            "timelineId": timeline_id,
            "targetClientIds": list(target_client_ids),
            "requiredClientIds": list(required_client_ids),
            "readyClientIds": set(),
            "failedClientIds": set(),
            "controlVersion": control_version,
            "commitPayload": dict(commit_payload),
            "createdAtMs": created_at_ms,
            "expiresAtMs": expires_at_ms,
            "status": "preparing",
            "requestSid": request_sid,
            "requestId": request_id,
        }
        with self._lock:
            playback_context_id = prepare["commitPayload"].get(
                "playbackContextId"
            )
            if action == "broadcast.start" and playback_context_id:
                for existing_prepare in self._pending_prepares.values():
                    if existing_prepare.get("status") != "preparing":
                        continue
                    if existing_prepare.get("action") != "broadcast.start":
                        continue
                    existing_context_id = (
                        existing_prepare.get("commitPayload") or {}
                    ).get("playbackContextId")
                    if existing_context_id == playback_context_id:
                        raise PlaybackContextConflictError(playback_context_id)
            self.supersede_prepares_for_timeline(timeline_id)
            self._pending_prepares[prepare_id] = prepare
            return self._copy_prepare_locked(prepare)

    def _copy_prepare_locked(self, prepare):
        copied = dict(prepare)
        copied["targetClientIds"] = list(prepare.get("targetClientIds") or [])
        copied["requiredClientIds"] = list(prepare.get("requiredClientIds") or [])
        copied["readyClientIds"] = set(prepare.get("readyClientIds") or set())
        copied["failedClientIds"] = set(prepare.get("failedClientIds") or set())
        copied["commitPayload"] = dict(prepare.get("commitPayload") or {})
        return copied

    def get_prepare(self, prepare_id):
        with self._lock:
            prepare = self._pending_prepares.get(prepare_id)
            return None if prepare is None else self._copy_prepare_locked(prepare)

    def update_prepare_ready(self, prepare_id, client_id, ready):
        with self._lock:
            prepare = self._pending_prepares.get(prepare_id)
            if prepare is None:
                return None
            if prepare.get("status") != "preparing":
                return self._copy_prepare_locked(prepare)
            if client_id not in set(prepare.get("targetClientIds") or []):
                return None
            if ready:
                prepare["readyClientIds"].add(client_id)
                prepare["failedClientIds"].discard(client_id)
            else:
                prepare["failedClientIds"].add(client_id)
                prepare["readyClientIds"].discard(client_id)
            return self._copy_prepare_locked(prepare)

    def finish_prepare(self, prepare_id, status):
        with self._lock:
            prepare = self._pending_prepares.get(prepare_id)
            if prepare is None:
                return None
            prepare["status"] = status
            return self._copy_prepare_locked(prepare)

    def finish_prepare_if_preparing(self, prepare_id, status):
        with self._lock:
            prepare = self._pending_prepares.get(prepare_id)
            if prepare is None or prepare.get("status") != "preparing":
                return None
            prepare["status"] = status
            return self._copy_prepare_locked(prepare)

    def supersede_prepares_for_timeline(self, timeline_id):
        now_ms = _timestamp_ms()
        superseded = []
        with self._lock:
            for prepare in self._pending_prepares.values():
                if (
                    prepare.get("timelineId") == timeline_id
                    and prepare.get("status") == "preparing"
                ):
                    prepare["status"] = "superseded"
                    superseded.append(self._copy_prepare_locked(prepare))
                    handoff_id = (prepare.get("commitPayload") or {}).get("handoffId")
                    handoff = self._handoffs.get(handoff_id)
                    if handoff is not None and handoff.get("status") == "preparing":
                        handoff["status"] = "superseded"
                        handoff["updatedAtMs"] = now_ms
            return superseded

    def get_follow_relationship(self, follower_client_id, active_only=True):
        with self._lock:
            relationship = self._follow_relationships.get(follower_client_id)
            if relationship is None:
                return None
            if active_only and not relationship.get("active"):
                return None
            return dict(relationship)

    def list_followers_for_source(self, source_client_id, active_only=True):
        with self._lock:
            followers = []
            for relationship in self._follow_relationships.values():
                if relationship.get("sourceClientId") != source_client_id:
                    continue
                if active_only and not relationship.get("active"):
                    continue
                followers.append(dict(relationship))
            return sorted(
                followers,
                key=lambda relationship: relationship.get("followerClientId") or "",
            )

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
