import os
import logging
import time
import uuid

from flask import current_app, request, session
from flask_socketio import Namespace, SocketIO, emit

from ..db import close_connection, open_connection
from ..logging_utils import format_log_event
from ..managers.user import UserManager
from .ws_store import (
    getLocalQueueState,
    getLocalQueueStates,
    getPlaybackState,
    getPlaybackStates,
    getQueueState,
    saveLocalQueueState,
    savePlaybackState,
    saveQueueState,
)
from .ws_state import (
    BroadcastInactiveError,
    BroadcastVersionMismatchError,
    ClientSeqStaleError,
    DEFAULT_CLIENT_STALE_SECONDS,
    DEFAULT_FOLLOW_DELAY_MS,
    QueueRevisionMismatchError,
    get_state,
)


logger = logging.getLogger(__name__)
state = get_state()
async_mode = os.environ.get("EMO_SOCKETIO_ASYNC_MODE") or None
socketio_kwargs = {"cors_allowed_origins": "*", "path": "/emo/ws"}
if async_mode is not None:
    socketio_kwargs["async_mode"] = async_mode
socketio = SocketIO(**socketio_kwargs)

ALLOWED_PRE_AUTH = {"auth.login", "system.ping"}
CONTROL_ACTIONS = {
    "player.play",
    "player.pause",
    "player.next",
    "player.prev",
    "player.seek",
    "player.setVolume",
    "player.requestState",
    "queue.playItem",
}
SESSION_ACTIONS = {"session.subscribe", "session.unsubscribe"}
FOLLOW_ACTIONS = {"follow.start", "follow.stop"}
BROADCAST_ACTIONS = {
    "broadcast.start",
    "broadcast.stop",
    "broadcast.queue.sync",
    "broadcast.playItem",
    "broadcast.play",
    "broadcast.pause",
    "broadcast.seek",
    "broadcast.status",
}
ACTION_EVENT_NAMES = {
    "auth.login": "auth_login",
    "device.register": "device_register",
    "session.subscribe": "session_subscribe",
    "session.unsubscribe": "session_unsubscribe",
    "follow.start": "follow_start",
    "follow.stop": "follow_stop",
    "playback.update": "playback_update",
    "queue.local.get": "queue_local_get",
    "queue.local.set": "queue_local_set",
    "queue.session.sync": "queue_session_sync",
    "queue.ready.complete": "queue_ready_complete",
    "broadcast.start": "broadcast_start",
    "broadcast.stop": "broadcast_stop",
    "broadcast.queue.sync": "broadcast_queue_sync",
    "broadcast.playItem": "broadcast_play_item",
    "broadcast.play": "broadcast_play",
    "broadcast.pause": "broadcast_pause",
    "broadcast.seek": "broadcast_seek",
    "broadcast.status": "broadcast_status",
}

CONTROL_POLICIES = {
    "owner_only",
    "controllers_only",
    "participants_can_control",
    "participants_and_controllers_can_control",
}


class BroadcastConflictError(Exception):
    def __init__(self, message, current_version=None, current_control_version=None):
        super().__init__(message)
        self.current_version = current_version
        self.current_control_version = current_control_version


class QueueConflictError(Exception):
    def __init__(self, message, current_queue_revision=None):
        super().__init__(message)
        self.current_queue_revision = current_queue_revision


class FollowControlForbiddenError(PermissionError):
    pass


def _log_emo_event(level, event, **fields):
    logger.log(level, format_log_event("emo", event, **fields))


def _build_queue_summary(session_id, source_client_id, queue_song_ids, current_index, position_ms, **extra):
    fields = {
        "session_id": session_id,
        "source_client_id": source_client_id,
        "queue_size": len(queue_song_ids),
        "current_index": current_index,
        "position_ms": position_ms,
    }
    fields.update(extra)
    return fields


def _build_playback_summary(session_id, source_client_id, payload, **extra):
    fields = {
        "session_id": session_id,
        "source_client_id": source_client_id,
        "playback_state": payload.get("state") or "-",
        "track_id": payload.get("trackId") or "-",
        "position_ms": payload.get("positionMs", 0),
    }
    fields.update(extra)
    return fields


def _get_action_event_name(action):
    if action in CONTROL_ACTIONS:
        return "control_forward"
    return ACTION_EVENT_NAMES.get(action)


def _get_client_stale_seconds():
    value = current_app.config["WEBAPP"].get(
        "emo_client_timeout", DEFAULT_CLIENT_STALE_SECONDS
    )
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_CLIENT_STALE_SECONDS
    return value if value > 0 else None


def _list_clients(user_name=None, session_id=None):
    return state.list_clients(
        user_name=user_name,
        session_id=session_id,
        stale_after_seconds=_get_client_stale_seconds(),
    )


def _build_action_log_context(action, request_id, current_user_name, current_client, payload, message):
    context = {
        "action": action,
        "client_request_id": request_id,
    }
    if current_user_name:
        context["user"] = current_user_name
    if current_client is not None:
        context["source_client_id"] = current_client.get("clientId")
        context["session_id"] = payload.get("sessionId") or current_client.get("sessionId")
    elif payload.get("sessionId"):
        context["session_id"] = payload.get("sessionId")

    target_client_id = message.get("targetClientId")
    if target_client_id:
        context["target_client_id"] = target_client_id
    return context


def init_socketio(app):
    socketio.init_app(app, path="/emo/ws")
    return socketio


def _get_access_logger():
    logger_name = current_app.extensions.get("supysonic_access_logger_name", "supysonic")
    return logging.getLogger(f"{logger_name}.access")


def _log_socket_access(event):
    _get_access_logger().info(
        format_log_event(
            "access",
            "socket",
            type="SOCKET",
            remote=request.remote_addr or "-",
            path=request.path or "/emo/ws",
            socket_event=event,
            sid=request.sid,
        )
    )


def _build_message(msg_type, action, payload=None, **extra):
    timestamp = time.time()
    message_payload = payload or {}
    if isinstance(message_payload, dict):
        message_payload = dict(message_payload)
        if "serverUpdatedAtMs" in message_payload:
            message_payload["serverTimeMs"] = int(timestamp * 1000)
    message = {
        "type": msg_type,
        "action": action,
        "payload": message_payload,
        "timestamp": timestamp,
    }
    message.update(extra)
    return message


def _server_time_ms():
    return int(time.time() * 1000)


def _send_ack(request_id=None, payload=None):
    emit("message", _build_message("system", "system.ack", payload, requestId=request_id))


def _send_error(code, message, request_id=None):
    payload = {"code": code, "message": message}
    emit("message", _build_message("system", "system.error", payload, requestId=request_id))


def _get_session_user():
    user_id = session.get("userid")
    if not user_id:
        return None
    open_connection(reuse=True)
    try:
        return UserManager.get(user_id)
    except Exception:  # pragma: nocover
        return None
    finally:
        close_connection()


def _authenticate(payload):
    session_user = _get_session_user()
    if session_user is not None:
        return session_user

    user_name = payload.get("u")
    password = payload.get("p")
    if not user_name or not password:
        return None

    open_connection(reuse=True)
    try:
        return UserManager.try_auth(user_name, password)
    finally:
        close_connection()


def _broadcast_clients(user_name):
    message = _build_message(
        "state", "device.list", {"devices": _list_clients(user_name=user_name)}
    )
    for target_sid, _ in state.list_sids(user_name=user_name):
        socketio.emit("message", message, to=target_sid, namespace="/emo")


def _broadcast_queue(user_name, session_id):
    queue_state = state.get_queue(session_id)
    if queue_state is None:
        return
    message = _build_message("state", "queue.session.sync", queue_state)
    target_sids = {
        sid for sid, _ in state.list_sids(user_name=user_name, session_id=session_id)
    }
    target_sids.update(state.list_subscribers(session_id, user_name=user_name))
    for target_sid in target_sids:
        socketio.emit("message", message, to=target_sid, namespace="/emo")


def _broadcast_playback_state(user_name, session_id):
    playback_states = state.list_playback_states(session_id)
    if not playback_states:
        return
    target_sids = {
        sid for sid, _ in state.list_sids(user_name=user_name, session_id=session_id)
    }
    target_sids.update(state.list_subscribers(session_id, user_name=user_name))
    for playback_state in playback_states:
        message = _build_message("state", "playback.update", playback_state)
        for target_sid in target_sids:
            socketio.emit("message", message, to=target_sid, namespace="/emo")


def _broadcast_local_queue(user_name, session_id, client_id):
    local_queue = state.get_local_queue(session_id, client_id)
    if local_queue is None:
        return
    message = _build_message("state", "queue.local.set", local_queue)
    target_sids = {
        sid for sid, _ in state.list_sids(user_name=user_name, session_id=session_id)
    }
    target_sids.update(state.list_subscribers(session_id, user_name=user_name))
    for target_sid in target_sids:
        socketio.emit("message", message, to=target_sid, namespace="/emo")


def _broadcast_local_queue_to_client(target_client_id, session_id, client_id):
    local_queue = state.get_local_queue(session_id, client_id)
    if local_queue is None:
        return
    payload = dict(local_queue)
    message = _build_message("state", "queue.local.set", payload)
    message["targetClientId"] = target_client_id
    target_sid = state.get_sid_for_client(target_client_id)
    if target_sid:
        socketio.emit("message", message, to=target_sid, namespace="/emo")


def _broadcast_queue_ready_complete(user_name, session_id, ready_payload, exclude_sid=None):
    message = _build_message("state", "queue.ready.complete", ready_payload)
    target_sids = {
        sid for sid, _ in state.list_sids(user_name=user_name, session_id=session_id)
    }
    target_sids.update(state.list_subscribers(session_id, user_name=user_name))
    if exclude_sid is not None:
        target_sids.discard(exclude_sid)
    for target_sid in target_sids:
        socketio.emit("message", message, to=target_sid, namespace="/emo")


def _push_session_snapshot(sid, session_id):
    _restorePersistedState(sid, session_id)


def _restorePersistedState(sid, session_id):
    client_info = state.get_client_for_sid(sid)
    client_id = None if client_info is None else client_info.get("clientId")

    queue_state = state.get_queue(session_id)
    if queue_state is None:
        queue_state = getQueueState(session_id)
        if queue_state is not None:
            queue_state = state.restore_queue(
                session_id,
                queue_state,
            )

    if queue_state is not None:
        socketio.emit(
            "message",
            _build_message("state", "queue.session.sync", queue_state),
            to=sid,
            namespace="/emo",
        )

    local_queues = state.list_local_queues(session_id)
    if not local_queues:
        local_queues = getLocalQueueStates(session_id)
        restored_local_queues = []
        for local_queue in local_queues:
            restored_local_queues.append(
                state.restore_local_queue(
                    session_id,
                    local_queue.get("sourceClientId"),
                    local_queue,
                )
            )
        local_queues = [
            local_queue
            for local_queue in restored_local_queues
            if local_queue is not None
        ]
    logger.debug("First Restoring %d local queues for session %s, sid %s", len(local_queues), session_id, sid)
    for local_queue in local_queues:
        logger.debug(
            "Restoring local_queue sourceClientId=%s, client_id=%s",
            local_queue.get("sourceClientId"),
            client_id,
        )
        if client_id and local_queue.get("sourceClientId") == client_id:
            continue  # Skip sending the local queue back to its own client, as it should already have the latest state
        socketio.emit(
            "message",
            _build_message("state", "queue.local.set", local_queue),
            to=sid,
            namespace="/emo",
        )

    if client_id:
        active_broadcast_id = state.get_active_broadcast_for_client(client_id)
        active_broadcast = (
            None if active_broadcast_id is None else state.get_broadcast(active_broadcast_id)
        )
        if active_broadcast is not None:
            participant_state = (
                state.get_broadcast_participant_state(active_broadcast_id, client_id)
                or {}
            )
            state.update_broadcast_participant_state(
                active_broadcast_id,
                client_id,
                session_id,
                participant_state,
                online=True,
            )
            socketio.emit(
                "message",
                _build_message(
                    "state",
                    "broadcast.status",
                    _build_broadcast_status_payload(active_broadcast),
                ),
                to=sid,
                namespace="/emo",
            )

    playback_states = state.list_playback_states(session_id)
    if not playback_states:
        playback_states = getPlaybackStates(session_id)
        restored_playback_states = []
        for playback_state in playback_states:
            restored_playback_states.append(
                state.restore_playback_state(
                    session_id,
                    playback_state.get("sourceClientId"),
                    playback_state,
                )
            )
        playback_states = [
            playback_state
            for playback_state in restored_playback_states
            if playback_state is not None
        ]

    for playback_state in playback_states:
        socketio.emit(
            "message",
            _build_message("state", "playback.update", playback_state),
            to=sid,
            namespace="/emo",
        )


def _register_device(sid, user_name, payload):
    client_id = payload.get("clientId")
    if not client_id:
        raise ValueError("Missing clientId")

    roles = payload.get("roles")
    if roles is None:
        roles = []
    if not isinstance(roles, list):
        raise ValueError("roles must be a list")

    device_name = payload.get("deviceName")
    if device_name is None:
        device_name = client_id
    if not isinstance(device_name, str):
        raise ValueError("deviceName must be a string")
    device_name = device_name.strip() or client_id

    alias = payload.get("alias")
    if alias is None:
        alias = payload.get("deviceAlias")
    if alias is None:
        alias = device_name

    if not isinstance(alias, str):
        raise ValueError("alias must be a string")

    alias = alias.strip() or device_name

    return state.register_client(
        sid,
        client_id,
        {
            "userName": user_name,
            "deviceName": device_name,
            "alias": alias,
            "roles": roles,
            "sessionId": payload.get("sessionId") or client_id,
            "capabilities": payload.get("capabilities") or {},
        },
    )


def _route_command(sender, message):
    target_client_id = message.get("targetClientId")
    if not target_client_id:
        raise ValueError("Missing targetClientId")

    target_sid = state.get_sid_for_client(target_client_id)
    target_client = state.get_client(target_client_id)
    if target_sid is None or target_client is None:
        raise LookupError("Target client is offline")

    if target_client.get("userName") != sender.get("userName"):
        raise PermissionError("Cross-user control is not allowed")

    _ensure_not_follow_source_control(sender, target_client_id, message["action"])

    outgoing = _build_message(
        "command",
        message["action"],
        message["payload"],
        requestId=message.get("requestId"),
        sourceClientId=sender["clientId"],
        targetClientId=target_client_id,
    )
    socketio.emit("message", outgoing, to=target_sid, namespace="/emo")


def _get_active_follow_relationship(client_id):
    if not client_id:
        return None
    return state.get_follow_relationship(client_id)


def _ensure_not_follow_source_control(current_client, target_client_id, action):
    if current_client is None:
        return
    relationship = _get_active_follow_relationship(current_client.get("clientId"))
    if relationship is None:
        return
    if relationship.get("sourceClientId") != target_client_id:
        return
    if action in CONTROL_ACTIONS:
        raise FollowControlForbiddenError(
            "Follow participants cannot control the source timeline"
        )


def _ensure_not_follow_source_queue_update(current_client, session_id, owner_client_id=None):
    if current_client is None:
        return
    relationship = _get_active_follow_relationship(current_client.get("clientId"))
    if relationship is None:
        return
    if (
        relationship.get("sourceSessionId") == session_id
        or (
            owner_client_id is not None
            and relationship.get("sourceClientId") == owner_client_id
        )
    ):
        raise FollowControlForbiddenError(
            "Follow participants cannot control the source timeline"
        )


def _new_broadcast_id():
    return f"broadcast-{uuid.uuid4().hex[:12]}"


def _is_non_negative_int(value):
    return type(value) is int and value >= 0


def _is_int(value):
    return type(value) is int


def _build_broadcast_core_payload(broadcast, extra=None):
    server_time_ms = _server_time_ms()
    server_updated_at_ms = broadcast.get("serverUpdatedAtMs")
    if server_updated_at_ms is None and isinstance(broadcast.get("updatedAt"), (int, float)):
        server_updated_at_ms = int(broadcast["updatedAt"] * 1000)
    payload = {
        "broadcastId": broadcast.get("broadcastId"),
        "timelineId": broadcast.get("timelineId") or f"broadcast:{broadcast.get('broadcastId')}",
        "authorityClientId": broadcast.get("authorityClientId") or "server",
        "originClientId": broadcast.get("originClientId") or broadcast.get("updatedByClientId"),
        "ownerClientId": broadcast.get("ownerClientId"),
        "participants": list(broadcast.get("participants") or []),
        "queueSongIds": list(broadcast.get("queueSongIds") or []),
        "currentIndex": broadcast.get("currentIndex", 0),
        "trackId": broadcast.get("trackId"),
        "positionMs": broadcast.get("positionMs", 0),
        "state": broadcast.get("state") or "stopped",
        "version": broadcast.get("version", 0),
        "epoch": broadcast.get("epoch", 0),
        "queueRevision": broadcast.get("queueRevision", 0),
        "controlVersion": broadcast.get("controlVersion", broadcast.get("version", 0)),
        "serverUpdatedAtMs": server_updated_at_ms,
        "serverTimeMs": server_time_ms,
        "playbackRate": broadcast.get("playbackRate", 1.0),
        "followDelayMs": broadcast.get("followDelayMs", DEFAULT_FOLLOW_DELAY_MS),
        "updatedByClientId": broadcast.get("updatedByClientId"),
        "controlPolicy": broadcast.get("controlPolicy"),
        "updatedAt": None if server_updated_at_ms is None else server_updated_at_ms / 1000,
    }
    if extra:
        payload.update(extra)
    return payload


def _build_broadcast_status_payload(broadcast):
    return {
        "broadcast": _build_broadcast_core_payload(broadcast),
        "participantStates": state.list_broadcast_participant_states(
            broadcast.get("broadcastId")
        ),
    }


def _get_broadcast_from_payload(current_user_name, payload):
    broadcast_id = payload.get("broadcastId")
    if not isinstance(broadcast_id, str) or not broadcast_id:
        raise ValueError("broadcastId must be a non-empty string")

    broadcast = state.get_broadcast(broadcast_id)
    if broadcast is None:
        raise LookupError("Broadcast not found")
    if broadcast.get("userName") != current_user_name:
        raise PermissionError("Cross-user broadcast access is not allowed")
    return broadcast


def _validate_control_policy(control_policy):
    if control_policy is None:
        return "participants_and_controllers_can_control"
    if control_policy not in CONTROL_POLICIES:
        raise ValueError("Unsupported broadcast controlPolicy")
    return control_policy


def _validate_broadcast_queue(queue_song_ids, current_index, position_ms):
    if not isinstance(queue_song_ids, list):
        raise ValueError("queueSongIds must be a list")
    if not all(isinstance(song_id, str) and song_id for song_id in queue_song_ids):
        raise ValueError("queueSongIds must contain non-empty strings")
    if not _is_int(current_index):
        raise ValueError("currentIndex must be an integer")
    if not _is_int(position_ms):
        raise ValueError("positionMs must be an integer")
    if queue_song_ids:
        if current_index < 0 or current_index >= len(queue_song_ids):
            raise ValueError("currentIndex is out of bounds")
        if position_ms < 0:
            raise ValueError("positionMs must be >= 0")
    elif current_index != 0 or position_ms != 0:
        raise ValueError("empty queue must use currentIndex=0 and positionMs=0")


def _get_broadcast_base_control_version(payload):
    if "baseControlVersion" in payload:
        base_version = payload.get("baseControlVersion")
        if not _is_int(base_version):
            raise ValueError("baseControlVersion must be an integer")
        return base_version
    base_version = payload.get("baseVersion")
    if not _is_int(base_version):
        raise ValueError("baseVersion must be an integer")
    return base_version


def _get_optional_broadcast_base_control_version(payload):
    if "baseControlVersion" in payload or "baseVersion" in payload:
        return _get_broadcast_base_control_version(payload)
    return None


def _get_base_queue_revision(payload):
    if "baseQueueRevision" not in payload:
        return None
    base_revision = payload.get("baseQueueRevision")
    if not _is_int(base_revision):
        raise ValueError("baseQueueRevision must be an integer")
    return base_revision


def _update_active_broadcast_state(broadcast_id, updated_by_client_id, **kwargs):
    try:
        updated = state.update_broadcast_state(
            broadcast_id,
            updated_by_client_id,
            require_active=True,
            **kwargs,
        )
    except BroadcastInactiveError as exc:
        raise ValueError(str(exc))
    except BroadcastVersionMismatchError as exc:
        raise BroadcastConflictError(
            "Broadcast control version conflict",
            current_version=exc.current_version,
            current_control_version=exc.current_control_version,
        )
    if updated is None:
        raise LookupError("Broadcast not found")
    return updated


def _has_role(client, role):
    return role in (client.get("roles") or [])


def _can_control_broadcast(client, broadcast):
    if client.get("userName") != broadcast.get("userName"):
        return False

    client_id = client.get("clientId")
    if client_id == broadcast.get("ownerClientId"):
        return True

    is_participant = client_id in (broadcast.get("participants") or [])
    is_controller = _has_role(client, "controller")
    control_policy = broadcast.get("controlPolicy") or "participants_and_controllers_can_control"

    if control_policy == "participants_can_control":
        return is_participant
    if control_policy == "controllers_only":
        return is_controller
    if control_policy == "participants_and_controllers_can_control":
        return is_participant or is_controller
    if control_policy == "owner_only":
        return False
    return False


def _require_broadcast_control(current_client, broadcast):
    if current_client is None:
        raise PermissionError("Register the device before controlling broadcast")
    if not _can_control_broadcast(current_client, broadcast):
        raise PermissionError("Broadcast control is not allowed")


def _resolve_broadcast_start_participants(current_user_name, current_client, payload):
    target_mode = payload.get("targetMode")
    if target_mode not in (
        "selectedClients",
        "allOnlinePlayers",
        "allOnlinePlayersExceptSelf",
    ):
        raise ValueError("Unsupported broadcast targetMode")

    participants = []
    skipped_client_ids = []
    seen = set()

    def maybe_add_client(client_id):
        if not isinstance(client_id, str) or not client_id:
            raise ValueError("targetClientIds must contain non-empty strings")
        if client_id in seen:
            return
        seen.add(client_id)

        client = state.get_client(client_id)
        if client is None:
            skipped_client_ids.append(client_id)
            return
        if client.get("userName") != current_user_name:
            raise PermissionError("Cross-user broadcast target is not allowed")
        if not _has_role(client, "player"):
            skipped_client_ids.append(client_id)
            return
        participants.append(client_id)

    if target_mode == "selectedClients":
        target_client_ids = payload.get("targetClientIds")
        if not isinstance(target_client_ids, list):
            raise ValueError("targetClientIds must be a list")
        for client_id in target_client_ids:
            maybe_add_client(client_id)
    else:
        for client in _list_clients(user_name=current_user_name):
            if not _has_role(client, "player"):
                continue
            if (
                target_mode == "allOnlinePlayersExceptSelf"
                and client.get("clientId") == current_client.get("clientId")
            ):
                continue
            maybe_add_client(client.get("clientId"))

    if not participants:
        raise ValueError("broadcast.start requires at least one online player participant")
    return participants, skipped_client_ids


def _broadcast_to_participants(broadcast, action, msg_type, source_client_id, request_id=None, extra_payload=None):
    payload = _build_broadcast_core_payload(broadcast, extra_payload)
    for target_client_id in broadcast.get("participants") or []:
        target_sid = state.get_sid_for_client(target_client_id)
        if target_sid is None:
            continue
        message = _build_message(
            msg_type,
            action,
            payload,
            requestId=request_id,
            sourceClientId=source_client_id,
            targetClientId=target_client_id,
        )
        socketio.emit("message", message, to=target_sid, namespace="/emo")


def _broadcast_status_to_requester(broadcast):
    emit(
        "message",
        _build_message(
            "state",
            "broadcast.status",
            _build_broadcast_status_payload(broadcast),
        ),
    )


def _estimate_broadcast_position_ms(broadcast, now=None):
    position_ms = broadcast.get("positionMs", 0)
    if broadcast.get("state") != "playing":
        return position_ms

    now = time.time() if now is None else now
    updated_at = broadcast.get("updatedAt")
    if not isinstance(updated_at, (int, float)):
        return position_ms
    return max(0, int(position_ms + ((now - updated_at) * 1000)))


def _handle_broadcast_start(current_user_name, current_client, payload, request_id):
    if current_client is None:
        raise PermissionError("Register the device before starting broadcast")

    queue_song_ids = payload.get("queueSongIds")
    current_index = payload.get("currentIndex", 0)
    position_ms = payload.get("positionMs", 0)
    auto_play = payload.get("autoPlay", False)
    if not isinstance(auto_play, bool):
        raise ValueError("autoPlay must be a boolean")

    _validate_broadcast_queue(queue_song_ids, current_index, position_ms)
    queue_song_ids = list(queue_song_ids)
    if not queue_song_ids:
        auto_play = False
        state_name = "stopped"
        current_index = 0
        position_ms = 0
    else:
        state_name = "playing" if auto_play else "stopped"

    control_policy = _validate_control_policy(payload.get("controlPolicy"))
    participant_ids, skipped_client_ids = _resolve_broadcast_start_participants(
        current_user_name,
        current_client,
        payload,
    )
    broadcast = state.create_broadcast(
        _new_broadcast_id(),
        current_user_name,
        current_client.get("clientId"),
        participant_ids,
        queue_song_ids,
        current_index,
        position_ms,
        state_name,
        control_policy,
        current_client.get("clientId"),
    )

    for participant_id in participant_ids:
        participant = state.get_client(participant_id)
        if participant is None:
            continue
        state.update_broadcast_participant_state(
            broadcast["broadcastId"],
            participant_id,
            participant.get("sessionId"),
            {
                "state": state_name,
                "trackId": broadcast.get("trackId"),
                "positionMs": position_ms,
            },
            online=True,
        )

    _send_ack(
        request_id,
        {
            "started": True,
            "broadcastId": broadcast["broadcastId"],
            "participants": list(broadcast.get("participants") or []),
            "skippedClientIds": skipped_client_ids,
            "broadcast": broadcast,
        },
    )
    _broadcast_to_participants(
        broadcast,
        "broadcast.start",
        "command",
        current_client.get("clientId"),
        request_id=request_id,
        extra_payload={"autoPlay": auto_play, "serverStartAt": None},
    )
    return broadcast


def _handle_broadcast_status(current_user_name, current_client, payload, request_id):
    if current_client is None:
        raise PermissionError("Register the device before requesting broadcast status")
    broadcast = _get_broadcast_from_payload(current_user_name, payload)
    _send_ack(request_id, _build_broadcast_status_payload(broadcast))
    _broadcast_status_to_requester(broadcast)
    return broadcast


def _handle_broadcast_queue_sync(current_user_name, current_client, payload, request_id):
    broadcast = _get_broadcast_from_payload(current_user_name, payload)
    _require_broadcast_control(current_client, broadcast)
    base_version = _get_broadcast_base_control_version(payload)

    queue_song_ids = payload.get("queueSongIds")
    current_index = payload.get("currentIndex", 0)
    position_ms = payload.get("positionMs", 0)
    _validate_broadcast_queue(queue_song_ids, current_index, position_ms)

    state_name = "stopped" if not queue_song_ids else broadcast.get("state") or "stopped"
    updated = _update_active_broadcast_state(
        broadcast["broadcastId"],
        current_client.get("clientId"),
        queue_song_ids=queue_song_ids,
        current_index=current_index,
        position_ms=position_ms,
        state_name=state_name,
        expected_version=base_version,
        increment_queue_revision=True,
    )
    _send_ack(request_id, {"updated": True, "broadcast": updated})
    _broadcast_to_participants(
        updated,
        "broadcast.queue.sync",
        "state",
        current_client.get("clientId"),
        request_id=request_id,
    )
    return updated


def _handle_broadcast_play_item(current_user_name, current_client, payload, request_id):
    broadcast = _get_broadcast_from_payload(current_user_name, payload)
    _require_broadcast_control(current_client, broadcast)
    base_version = _get_broadcast_base_control_version(payload)

    queue_index = payload.get("queueIndex")
    if not _is_int(queue_index) or queue_index < 0:
        raise ValueError("queueIndex must be a non-negative integer")
    queue_song_ids = broadcast.get("queueSongIds") or []
    if queue_index >= len(queue_song_ids):
        raise ValueError("queueIndex is out of bounds")

    position_ms = payload.get("positionMs", 0)
    if not _is_non_negative_int(position_ms):
        raise ValueError("positionMs must be a non-negative integer")

    updated = _update_active_broadcast_state(
        broadcast["broadcastId"],
        current_client.get("clientId"),
        current_index=queue_index,
        position_ms=position_ms,
        state_name="playing",
        expected_version=base_version,
    )
    _send_ack(request_id, {"updated": True, "broadcast": updated})
    _broadcast_to_participants(
        updated,
        "broadcast.playItem",
        "command",
        current_client.get("clientId"),
        request_id=request_id,
        extra_payload={"queueIndex": queue_index},
    )
    return updated


def _handle_broadcast_play(current_user_name, current_client, payload, request_id):
    broadcast = _get_broadcast_from_payload(current_user_name, payload)
    _require_broadcast_control(current_client, broadcast)
    queue_song_ids = broadcast.get("queueSongIds") or []
    current_index = broadcast.get("currentIndex", 0)
    if not queue_song_ids:
        raise ValueError("broadcast.play requires a non-empty queue")
    if not _is_int(current_index) or current_index < 0 or current_index >= len(queue_song_ids):
        raise ValueError("broadcast.play requires a valid currentIndex")

    updated = _update_active_broadcast_state(
        broadcast["broadcastId"],
        current_client.get("clientId"),
        state_name="playing",
        expected_version=_get_optional_broadcast_base_control_version(payload),
    )
    _send_ack(request_id, {"updated": True, "broadcast": updated})
    _broadcast_to_participants(
        updated,
        "broadcast.play",
        "command",
        current_client.get("clientId"),
        request_id=request_id,
    )
    return updated


def _handle_broadcast_pause(current_user_name, current_client, payload, request_id):
    broadcast = _get_broadcast_from_payload(current_user_name, payload)
    _require_broadcast_control(current_client, broadcast)
    position_ms = payload.get("positionMs")
    if position_ms is None:
        position_ms = _estimate_broadcast_position_ms(broadcast)
    if not _is_non_negative_int(position_ms):
        raise ValueError("positionMs must be a non-negative integer")

    updated = _update_active_broadcast_state(
        broadcast["broadcastId"],
        current_client.get("clientId"),
        position_ms=position_ms,
        state_name="paused",
        expected_version=_get_optional_broadcast_base_control_version(payload),
    )
    _send_ack(request_id, {"updated": True, "broadcast": updated})
    _broadcast_to_participants(
        updated,
        "broadcast.pause",
        "command",
        current_client.get("clientId"),
        request_id=request_id,
    )
    return updated


def _handle_broadcast_seek(current_user_name, current_client, payload, request_id):
    broadcast = _get_broadcast_from_payload(current_user_name, payload)
    _require_broadcast_control(current_client, broadcast)
    position_ms = payload.get("positionMs")
    if not _is_non_negative_int(position_ms):
        raise ValueError("positionMs must be a non-negative integer")

    updated = _update_active_broadcast_state(
        broadcast["broadcastId"],
        current_client.get("clientId"),
        position_ms=position_ms,
        expected_version=_get_optional_broadcast_base_control_version(payload),
    )
    _send_ack(request_id, {"updated": True, "broadcast": updated})
    _broadcast_to_participants(
        updated,
        "broadcast.seek",
        "command",
        current_client.get("clientId"),
        request_id=request_id,
    )
    return updated


def _handle_broadcast_stop(current_user_name, current_client, payload, request_id):
    broadcast = _get_broadcast_from_payload(current_user_name, payload)
    _require_broadcast_control(current_client, broadcast)
    try:
        updated = state.stop_broadcast(
            broadcast["broadcastId"],
            current_client.get("clientId"),
            expected_version=_get_optional_broadcast_base_control_version(payload),
        )
    except BroadcastInactiveError as exc:
        raise ValueError(str(exc))
    except BroadcastVersionMismatchError as exc:
        raise BroadcastConflictError(
            "Broadcast control version conflict",
            current_version=exc.current_version,
            current_control_version=exc.current_control_version,
        )
    if updated is None:
        raise LookupError("Broadcast not found")
    _send_ack(request_id, {"stopped": True, "broadcast": updated})
    _broadcast_to_participants(
        updated,
        "broadcast.stop",
        "command",
        current_client.get("clientId"),
        request_id=request_id,
    )
    return updated


def _handle_broadcast_action(current_user_name, current_client, action, payload, request_id):
    if action == "broadcast.start":
        return _handle_broadcast_start(current_user_name, current_client, payload, request_id)
    if action == "broadcast.status":
        return _handle_broadcast_status(current_user_name, current_client, payload, request_id)
    if action == "broadcast.queue.sync":
        return _handle_broadcast_queue_sync(current_user_name, current_client, payload, request_id)
    if action == "broadcast.playItem":
        return _handle_broadcast_play_item(current_user_name, current_client, payload, request_id)
    if action == "broadcast.play":
        return _handle_broadcast_play(current_user_name, current_client, payload, request_id)
    if action == "broadcast.pause":
        return _handle_broadcast_pause(current_user_name, current_client, payload, request_id)
    if action == "broadcast.seek":
        return _handle_broadcast_seek(current_user_name, current_client, payload, request_id)
    if action == "broadcast.stop":
        return _handle_broadcast_stop(current_user_name, current_client, payload, request_id)
    return None


def _validate_queue_play_item(target_client_id, payload):
    session_id = payload.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("queue.playItem requires a non-empty sessionId")

    queue_index = payload.get("queueIndex")
    if not isinstance(queue_index, int):
        raise ValueError("queue.playItem requires queueIndex as an integer")
    if queue_index < 0:
        raise ValueError("queue.playItem requires queueIndex >= 0")

    client_id = payload.get("clientId")
    if client_id is not None:
        if not isinstance(client_id, str) or not client_id:
            raise ValueError("queue.playItem clientId must be a non-empty string")
        if client_id != target_client_id:
            raise ValueError("queue.playItem clientId must match targetClientId")


def _validate_player_request_state(payload):
    session_id = payload.get("sessionId")
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        raise ValueError("player.requestState sessionId must be a non-empty string")

    for field_name in (
        "includePlayback",
        "includeSessionQueue",
        "includeLocalQueue",
        "includeReadyState",
    ):
        field_value = payload.get(field_name)
        if field_value is not None and not isinstance(field_value, bool):
            raise ValueError(f"player.requestState {field_name} must be a boolean")


def _handle_follow_start(current_user_name, current_client, payload, request_id, sid):
    if current_client is None:
        raise PermissionError("Register the device before starting follow playback")

    source_client_id = payload.get("sourceClientId") or payload.get("followSourceClientId")
    if not isinstance(source_client_id, str) or not source_client_id:
        raise ValueError("follow.start requires a non-empty sourceClientId")
    if source_client_id == current_client.get("clientId"):
        raise ValueError("follow.start sourceClientId cannot be the current client")

    source_client = state.get_client(source_client_id)
    if source_client is None:
        raise LookupError("Follow source client is offline")
    if source_client.get("userName") != current_user_name:
        raise PermissionError("Cross-user follow is not allowed")

    source_session_id = (
        payload.get("sourceSessionId")
        or payload.get("followSessionId")
        or payload.get("sessionId")
        or source_client.get("sessionId")
    )
    if not isinstance(source_session_id, str) or not source_session_id:
        raise ValueError("follow.start requires a non-empty sourceSessionId")
    if source_client.get("sessionId") != source_session_id:
        raise ValueError("follow.start sourceSessionId must match the source client")

    relationship = state.start_follow_relationship(
        current_client.get("clientId"),
        current_client.get("sessionId"),
        source_client_id,
        source_session_id,
        current_user_name,
    )
    subscriptions = state.subscribe_session(sid, source_session_id)
    _send_ack(
        request_id,
        {"relationship": relationship, "subscriptions": subscriptions},
    )
    _push_session_snapshot(sid, source_session_id)
    return relationship


def _handle_follow_stop(current_client, payload, request_id, sid):
    if current_client is None:
        raise PermissionError("Register the device before stopping follow playback")

    relationship = state.stop_follow_relationship(current_client.get("clientId"))
    session_id = payload.get("sourceSessionId") or payload.get("followSessionId") or payload.get("sessionId")
    if session_id is None and relationship is not None:
        session_id = relationship.get("sourceSessionId")

    subscriptions = []
    if session_id:
        if not isinstance(session_id, str):
            raise ValueError("follow.stop sessionId must be a string")
        subscriptions = state.unsubscribe_session(sid, session_id)
    else:
        subscriptions = state.unsubscribe_session(sid)

    _send_ack(
        request_id,
        {"relationship": relationship, "subscriptions": subscriptions},
    )
    return relationship


def _resolve_local_queue_owner(current_user_name, current_client, payload):
    if current_client is None:
        raise PermissionError("Register the device before updating local queue")

    if "sessionId" in payload:
        session_id = payload.get("sessionId")
    else:
        session_id = current_client.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("queue.local.set requires a non-empty sessionId")

    if "clientId" in payload:
        owner_client_id = payload.get("clientId")
    else:
        owner_client_id = current_client.get("clientId")
    if not isinstance(owner_client_id, str) or not owner_client_id:
        raise ValueError("queue.local.set clientId must be a non-empty string")

    owner_client = state.get_client(owner_client_id)
    if owner_client is None:
        raise LookupError("Local queue client is offline")
    if owner_client.get("userName") != current_user_name:
        raise PermissionError("Cross-user local queue update is not allowed")
    if owner_client.get("sessionId") != session_id:
        raise ValueError("queue.local.set clientId must belong to sessionId")

    return session_id, owner_client_id


def _validate_local_queue_target(current_user_name, target_client_id):
    if not isinstance(target_client_id, str) or not target_client_id:
        raise ValueError("targetClientId must be a non-empty string")

    target_client = state.get_client(target_client_id)
    if target_client is None:
        raise LookupError("Target client is offline")
    if target_client.get("userName") != current_user_name:
        raise PermissionError("Cross-user local queue routing is not allowed")


def _build_ready_complete_payload(current_client, payload):
    if current_client is None:
        raise PermissionError("Register the device before sending ready signal")

    session_id = payload.get("sessionId") or current_client.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("queue.ready.complete requires a non-empty sessionId")

    queue_type = payload.get("queueType")
    if queue_type not in ("session", "local"):
        raise ValueError("queueType must be either 'session' or 'local'")

    queue_song_ids = payload.get("queueSongIds")
    if not isinstance(queue_song_ids, list):
        raise ValueError("queueSongIds must be a list")
    if not all(isinstance(song_id, str) and song_id for song_id in queue_song_ids):
        raise ValueError("queueSongIds must contain non-empty strings")

    current_client_id = current_client.get("clientId")
    ready_payload = {
        "sessionId": session_id,
        "queueType": queue_type,
        "queueSongIds": list(queue_song_ids),
        "sourceClientId": current_client_id,
    }

    if queue_type == "local":
        client_id = payload.get("clientId") or current_client_id
        if not isinstance(client_id, str) or not client_id:
            raise ValueError("queue.ready.complete local queue requires a non-empty clientId")
        if client_id != current_client_id:
            raise ValueError("queue.ready.complete clientId must match the current device")
        ready_payload["clientId"] = client_id

    return ready_payload


class EmoNamespace(Namespace):
    def on_connect(self):
        if not current_app.config["WEBAPP"].get("emo_ws_enabled", True):
            return False
        state.register_session(request.sid)
        _log_socket_access("connect")
        _log_emo_event(logging.INFO, "socket_connect", sid=request.sid)

    def on_disconnect(self):
        session_info, client_info = state.unregister_session(request.sid)
        _log_socket_access("disconnect")
        _log_emo_event(logging.INFO, "socket_disconnect", sid=request.sid)
        if client_info is not None:
            _broadcast_clients(client_info["userName"])
        elif session_info is not None and session_info.get("userName"):
            _broadcast_clients(session_info["userName"])

    def on_message(self, message):
        if not isinstance(message, dict):
            _log_emo_event(logging.WARNING, "bad_message", result="bad_request", reason="message_not_object")
            _send_error("bad_request", "Message must be a JSON object")
            return

        action = message.get("action")
        request_id = message.get("requestId")
        if not isinstance(action, str) or not action:
            _log_emo_event(
                logging.WARNING,
                "bad_message",
                result="bad_request",
                reason="missing_action",
                client_request_id=request_id,
            )
            _send_error("bad_request", "Missing message action", request_id)
            return

        payload = message.get("payload")
        if payload is None:
            payload = {}
            message["payload"] = payload
        elif not isinstance(payload, dict):
            _log_emo_event(
                logging.WARNING,
                "bad_message",
                result="bad_request",
                reason="payload_not_object",
                action=action,
                client_request_id=request_id,
            )
            _send_error("bad_request", "Payload must be a JSON object", request_id)
            return

        session_info = state.get_session(request.sid)
        current_user_name = None if session_info is None else session_info.get("userName")
        current_client = state.get_client_for_sid(request.sid)

        state.touch_session(request.sid)
        state.prune_stale_clients(_get_client_stale_seconds())

        if action == "system.ping":
            emit(
                "message",
                _build_message(
                    "system",
                    "system.pong",
                    {"serverTimeMs": _server_time_ms()},
                    requestId=request_id,
                ),
            )
            return

        if (session_info is None or not session_info.get("authenticated")) and action not in ALLOWED_PRE_AUTH:
            _log_emo_event(
                logging.WARNING,
                "unauthorized_action",
                result="unauthorized",
                action=action,
                client_request_id=request_id,
                reason="authenticate_first",
                sid=request.sid,
            )
            _send_error("unauthorized", "Authenticate first", request_id)
            return

        try:
            if action == "auth.login":
                user = _authenticate(payload)
                if user is None:
                    _log_emo_event(
                        logging.WARNING,
                        "auth_login",
                        result="failure",
                        user=payload.get("u"),
                        client_request_id=request_id,
                        reason="invalid_credentials",
                        sid=request.sid,
                    )
                    _send_error("unauthorized", "Invalid credentials", request_id)
                    return
                state.authenticate_session(request.sid, user.name)
                current_user_name = user.name
                _log_emo_event(
                    logging.INFO,
                    "auth_login",
                    result="success",
                    user=user.name,
                    client_request_id=request_id,
                    sid=request.sid,
                )
                _send_ack(request_id, {"authenticated": True, "userName": user.name})
            elif action == "device.register":
                if not current_user_name:
                    raise PermissionError("Authenticate first")
                current_client = _register_device(request.sid, current_user_name, payload)
                _log_emo_event(
                    logging.INFO,
                    "device_register",
                    result="success",
                    user=current_user_name,
                    client_id=current_client.get("clientId"),
                    session_id=current_client.get("sessionId"),
                    roles=current_client.get("roles") or [],
                    client_request_id=request_id,
                    sid=request.sid,
                )
                _send_ack(request_id, {"client": current_client})
                _broadcast_clients(current_user_name)
                _restorePersistedState(request.sid, current_client.get("sessionId"))
            elif action == "device.list":
                emit(
                    "message",
                    _build_message(
                        "state",
                        "device.list",
                        {"devices": _list_clients(user_name=current_user_name)},
                    ),
                )
            elif action in SESSION_ACTIONS:
                if current_client is None:
                    raise PermissionError("Register the device before managing subscriptions")
                session_id = payload.get("sessionId")
                if not isinstance(session_id, str) or not session_id:
                    raise ValueError("Missing sessionId")
                if action == "session.subscribe":
                    if not any(
                        device.get("sessionId") == session_id
                        for device in _list_clients(user_name=current_user_name)
                    ):
                        raise PermissionError("Cannot subscribe to a session outside your scope")
                    subscriptions = state.subscribe_session(request.sid, session_id)
                    _log_emo_event(
                        logging.INFO,
                        "session_subscribe",
                        result="success",
                        user=current_user_name,
                        session_id=session_id,
                        client_id=current_client.get("clientId"),
                        client_request_id=request_id,
                    )
                    _send_ack(request_id, {"subscriptions": subscriptions})
                    _push_session_snapshot(request.sid, session_id)
                else:
                    subscriptions = state.unsubscribe_session(request.sid, session_id)
                    _log_emo_event(
                        logging.INFO,
                        "session_unsubscribe",
                        result="success",
                        user=current_user_name,
                        session_id=session_id,
                        client_id=current_client.get("clientId"),
                        client_request_id=request_id,
                    )
                    _send_ack(request_id, {"subscriptions": subscriptions})
            elif action in FOLLOW_ACTIONS:
                if action == "follow.start":
                    relationship = _handle_follow_start(
                        current_user_name,
                        current_client,
                        payload,
                        request_id,
                        request.sid,
                    )
                else:
                    relationship = _handle_follow_stop(
                        current_client,
                        payload,
                        request_id,
                        request.sid,
                    )
                _log_emo_event(
                    logging.INFO,
                    _get_action_event_name(action),
                    result="success",
                    user=current_user_name,
                    client_request_id=request_id,
                    source_client_id=None if current_client is None else current_client.get("clientId"),
                    target_client_id=None if relationship is None else relationship.get("sourceClientId"),
                    session_id=None if relationship is None else relationship.get("sourceSessionId"),
                )
            elif action in CONTROL_ACTIONS:
                if current_client is None:
                    raise PermissionError("Register the device before sending commands")
                if action == "queue.playItem":
                    _validate_queue_play_item(message.get("targetClientId"), payload)
                elif action == "player.requestState":
                    _validate_player_request_state(payload)
                _route_command(current_client, message)
                _log_emo_event(
                    logging.INFO,
                    "control_forward",
                    result="success",
                    action=action,
                    user=current_user_name,
                    session_id=payload.get("sessionId") or current_client.get("sessionId"),
                    source_client_id=current_client.get("clientId"),
                    target_client_id=message.get("targetClientId"),
                    client_request_id=request_id,
                )
                _send_ack(request_id, {"forwarded": True})
            elif action in BROADCAST_ACTIONS:
                updated_broadcast = _handle_broadcast_action(
                    current_user_name,
                    current_client,
                    action,
                    payload,
                    request_id,
                )
                _log_emo_event(
                    logging.INFO,
                    _get_action_event_name(action),
                    result="success",
                    user=current_user_name,
                    client_request_id=request_id,
                    source_client_id=None if current_client is None else current_client.get("clientId"),
                    broadcast_id=None if updated_broadcast is None else updated_broadcast.get("broadcastId"),
                    version=None if updated_broadcast is None else updated_broadcast.get("version"),
                )
            elif action == "playback.update":
                if current_client is None:
                    raise PermissionError("Register the device before publishing state")
                session_id = payload.get("sessionId") or current_client.get("sessionId")
                if not isinstance(session_id, str) or not session_id:
                    raise ValueError("playback.update requires a non-empty sessionId")
                payload_source_client_id = payload.get("sourceClientId")
                if (
                    payload_source_client_id is not None
                    and payload_source_client_id != current_client.get("clientId")
                ):
                    raise PermissionError("Cannot publish playback for another client")
                playback_payload = dict(payload)
                playback_payload["sourceClientId"] = current_client.get("clientId")
                broadcast_for_update = None
                if payload.get("broadcastId") is not None:
                    broadcast_for_update = _get_broadcast_from_payload(current_user_name, payload)
                    broadcast_id = broadcast_for_update["broadcastId"]
                    if not state.is_broadcast_participant(
                        broadcast_id,
                        current_client.get("clientId"),
                    ):
                        raise PermissionError("Broadcast playback update requires participant")
                    if (
                        state.get_active_broadcast_for_client(current_client.get("clientId"))
                        != broadcast_id
                    ):
                        raise PermissionError(
                            "Broadcast playback update requires active participant"
                        )
                playback_payload = state.update_playback_state(
                    session_id,
                    current_client.get("clientId"),
                    playback_payload,
                )
                savePlaybackState(
                    session_id,
                    current_user_name,
                    current_client.get("clientId"),
                    playback_payload,
                )
                _log_emo_event(
                    logging.INFO,
                    "playback_update",
                    result="success",
                    user=current_user_name,
                    client_request_id=request_id,
                    **_build_playback_summary(
                        session_id,
                        current_client.get("clientId"),
                        playback_payload,
                    ),
                )
                if broadcast_for_update is not None:
                    state.update_broadcast_participant_state(
                        broadcast_for_update["broadcastId"],
                        current_client.get("clientId"),
                        session_id,
                        playback_payload,
                        online=True,
                    )
                _send_ack(request_id, {"updated": True})
                _broadcast_playback_state(current_user_name, session_id)
            elif action == "queue.local.get":
                if current_client is None:
                    raise PermissionError("Register the device before requesting local queue")
                session_id = payload.get("sessionId") or current_client.get("sessionId")
                client_id = payload.get("clientId") or current_client.get("clientId")
                local_queue = state.get_local_queue(session_id, client_id)
                if local_queue is None:
                    local_queue = getLocalQueueState(session_id, client_id)
                    if local_queue is not None:
                        local_queue = state.restore_local_queue(
                            session_id,
                            client_id,
                            local_queue,
                        )
                if local_queue is None:
                    _log_emo_event(
                        logging.INFO,
                        "queue_local_get",
                        result="miss",
                        user=current_user_name,
                        client_request_id=request_id,
                        session_id=session_id,
                        source_client_id=client_id,
                    )
                else:
                    _log_emo_event(
                        logging.INFO,
                        "queue_local_get",
                        result="hit",
                        user=current_user_name,
                        client_request_id=request_id,
                        **_build_queue_summary(
                            session_id,
                            client_id,
                            local_queue.get("queueSongIds") or [],
                            local_queue.get("currentIndex", 0),
                            local_queue.get("positionMs", 0),
                        ),
                    )
                _send_ack(request_id, {"found": local_queue is not None})
                if local_queue is not None:
                    emit(
                        "message",
                        _build_message("state", "queue.local.set", local_queue),
                    )
            elif action == "queue.local.set":
                session_id, owner_client_id = _resolve_local_queue_owner(
                    current_user_name,
                    current_client,
                    payload,
                )
                _ensure_not_follow_source_queue_update(
                    current_client,
                    session_id,
                    owner_client_id,
                )
                queue_song_ids = payload.get("queueSongIds")
                targetClientId = message.get("targetClientId")
                if "targetClientId" in message:
                    _validate_local_queue_target(current_user_name, targetClientId)
                if not isinstance(queue_song_ids, list):
                    raise ValueError("queueSongIds must be a list")
                if not all(isinstance(song_id, str) and song_id for song_id in queue_song_ids):
                    raise ValueError("queueSongIds must contain non-empty strings")
                current_index = payload.get("currentIndex", 0)
                position_ms = payload.get("positionMs", 0)
                if not isinstance(current_index, int):
                    raise ValueError("currentIndex must be an integer")
                if not isinstance(position_ms, int):
                    raise ValueError("positionMs must be an integer")
                if queue_song_ids:
                    if current_index < 0 or current_index >= len(queue_song_ids):
                        raise ValueError("currentIndex is out of bounds")
                elif current_index != 0 or position_ms != 0:
                    raise ValueError("empty queue must use currentIndex=0 and positionMs=0")

                local_queue = state.update_local_queue(
                    session_id,
                    owner_client_id,
                    queue_song_ids,
                    current_index,
                    position_ms,
                )
                saveLocalQueueState(
                    session_id,
                    owner_client_id,
                    queue_song_ids,
                    current_index,
                    position_ms,
                )
                _log_emo_event(
                    logging.INFO,
                    "queue_local_set",
                    result="direct" if targetClientId is not None else "broadcast",
                    user=current_user_name,
                    client_request_id=request_id,
                    target_client_id=targetClientId or "-",
                    **_build_queue_summary(
                        session_id,
                        owner_client_id,
                        queue_song_ids,
                        current_index,
                        position_ms,
                    ),
                )
                _send_ack(request_id, {"updated": True})
                if targetClientId is not None:
                    logger.info("Broadcasting local queue update to specific client %s", targetClientId)
                    _broadcast_local_queue_to_client(
                        targetClientId,
                        session_id,
                        owner_client_id,
                    )
                else:
                    logger.info("Broadcasting local queue update to all clients in session %s", session_id)
                    _broadcast_local_queue(
                        current_user_name,
                        session_id,
                        owner_client_id,
                    )
            elif action == "queue.session.sync":
                if current_client is None:
                    raise PermissionError("Register the device before syncing queue")
                session_id = payload.get("sessionId") or current_client.get("sessionId")
                if not isinstance(session_id, str) or not session_id:
                    raise ValueError("queue.session.sync requires a non-empty sessionId")
                current_client_id = payload.get("clientId") or current_client.get("clientId")
                if not isinstance(current_client_id, str) or not current_client_id:
                    raise ValueError("queue.session.sync clientId must be a non-empty string")
                if payload.get("clientId"):
                    owner_client = state.get_client(current_client_id)
                    if owner_client is None:
                        raise LookupError("Queue owner client is offline")
                    if owner_client.get("userName") != current_user_name:
                        raise PermissionError("Cross-user queue sync is not allowed")
                    if owner_client.get("sessionId") != session_id:
                        raise ValueError("queue.session.sync clientId must belong to sessionId")
                _ensure_not_follow_source_queue_update(
                    current_client,
                    session_id,
                    current_client_id,
                )
                queue_song_ids = payload.get("queueSongIds")
                if not isinstance(queue_song_ids, list):
                    raise ValueError("queueSongIds must be a list")
                if not all(isinstance(song_id, str) and song_id for song_id in queue_song_ids):
                    raise ValueError("queueSongIds must contain non-empty strings")
                current_index = payload.get("currentIndex", 0)
                position_ms = payload.get("positionMs", 0)
                if not isinstance(current_index, int):
                    raise ValueError("currentIndex must be an integer")
                if not isinstance(position_ms, int):
                    raise ValueError("positionMs must be an integer")
                if queue_song_ids:
                    if current_index < 0 or current_index >= len(queue_song_ids):
                        raise ValueError("currentIndex is out of bounds")
                elif current_index != 0 or position_ms != 0:
                    raise ValueError("empty queue must use currentIndex=0 and positionMs=0")

                try:
                    queue_state = state.update_queue(
                        session_id,
                        queue_song_ids,
                        current_index,
                        position_ms,
                        current_client_id,
                        expected_queue_revision=_get_base_queue_revision(payload),
                    )
                except QueueRevisionMismatchError as exc:
                    raise QueueConflictError(
                        "Queue revision conflict",
                        current_queue_revision=exc.current_revision,
                    )
                saveQueueState(
                    session_id,
                    current_user_name,
                    current_client_id,
                    queue_song_ids,
                    current_index,
                    position_ms,
                )
                _log_emo_event(
                    logging.INFO,
                    "queue_session_sync",
                    result="success",
                    user=current_user_name,
                    client_request_id=request_id,
                    **_build_queue_summary(
                        session_id,
                        current_client_id,
                        queue_song_ids,
                        current_index,
                        position_ms,
                    ),
                )
                _send_ack(request_id, {"updated": True, "queue": queue_state})
                _broadcast_queue(current_user_name, session_id)
            elif action == "queue.ready.complete":
                ready_payload = _build_ready_complete_payload(current_client, payload)
                _log_emo_event(
                    logging.INFO,
                    "queue_ready_complete",
                    result="broadcast",
                    user=current_user_name,
                    client_request_id=request_id,
                    session_id=ready_payload["sessionId"],
                    source_client_id=ready_payload["sourceClientId"],
                    queue_type=ready_payload["queueType"],
                    queue_size=len(ready_payload.get("queueSongIds") or []),
                    client_id=ready_payload.get("clientId") or "-",
                )
                _send_ack(request_id, {"synced": True})
                _broadcast_queue_ready_complete(
                    current_user_name,
                    ready_payload["sessionId"],
                    ready_payload,
                    exclude_sid=request.sid,
                )
            else:
                _log_emo_event(
                    logging.WARNING,
                    "unsupported_action",
                    result="not_supported",
                    user=current_user_name,
                    action=action,
                    client_request_id=request_id,
                    source_client_id=None if current_client is None else current_client.get("clientId"),
                )
                _send_error("not_supported", f"Unsupported action: {action}", request_id)
        except QueueConflictError as exc:
            event_name = _get_action_event_name(action) or "bad_message"
            _log_emo_event(
                logging.WARNING,
                event_name,
                result="conflict",
                reason=str(exc),
                **_build_action_log_context(
                    action,
                    request_id,
                    current_user_name,
                    current_client,
                    payload,
                    message,
                ),
            )
            error_payload = {"code": "conflict", "message": str(exc)}
            if exc.current_queue_revision is not None:
                error_payload["currentQueueRevision"] = exc.current_queue_revision
            emit(
                "message",
                _build_message(
                    "system",
                    "system.error",
                    error_payload,
                    requestId=request_id,
                ),
            )
        except BroadcastConflictError as exc:
            event_name = _get_action_event_name(action) or "bad_message"
            _log_emo_event(
                logging.WARNING,
                event_name,
                result="conflict",
                reason=str(exc),
                **_build_action_log_context(
                    action,
                    request_id,
                    current_user_name,
                    current_client,
                    payload,
                    message,
                ),
            )
            error_payload = {"code": "conflict", "message": str(exc)}
            if exc.current_version is not None:
                error_payload["currentVersion"] = exc.current_version
            if exc.current_control_version is not None:
                error_payload["currentControlVersion"] = exc.current_control_version
            emit(
                "message",
                _build_message(
                    "system",
                    "system.error",
                    error_payload,
                    requestId=request_id,
                ),
            )
        except ClientSeqStaleError as exc:
            _log_emo_event(
                logging.WARNING,
                _get_action_event_name(action) or "bad_message",
                result="stale_client_seq",
                reason=str(exc),
                **_build_action_log_context(
                    action,
                    request_id,
                    current_user_name,
                    current_client,
                    payload,
                    message,
                ),
            )
            emit(
                "message",
                _build_message(
                    "system",
                    "system.error",
                    {
                        "code": "stale_client_seq",
                        "message": str(exc),
                        "currentClientSeq": exc.current_seq,
                    },
                    requestId=request_id,
                ),
            )
        except FollowControlForbiddenError as exc:
            _log_emo_event(
                logging.WARNING,
                "unauthorized_action",
                result="follow_control_forbidden",
                reason=str(exc),
                **_build_action_log_context(
                    action,
                    request_id,
                    current_user_name,
                    current_client,
                    payload,
                    message,
                ),
            )
            _send_error("follow_control_forbidden", str(exc), request_id)
        except PermissionError as exc:
            _log_emo_event(
                logging.WARNING,
                "unauthorized_action",
                result="forbidden",
                reason=str(exc),
                **_build_action_log_context(
                    action,
                    request_id,
                    current_user_name,
                    current_client,
                    payload,
                    message,
                ),
            )
            _send_error("forbidden", str(exc), request_id)
        except LookupError as exc:
            event_name = _get_action_event_name(action) or "bad_message"
            _log_emo_event(
                logging.WARNING,
                event_name,
                result="not_found",
                reason=str(exc),
                **_build_action_log_context(
                    action,
                    request_id,
                    current_user_name,
                    current_client,
                    payload,
                    message,
                ),
            )
            _send_error("not_found", str(exc), request_id)
        except ValueError as exc:
            event_name = _get_action_event_name(action) or "bad_message"
            _log_emo_event(
                logging.WARNING,
                event_name,
                result="bad_request",
                reason=str(exc),
                **_build_action_log_context(
                    action,
                    request_id,
                    current_user_name,
                    current_client,
                    payload,
                    message,
                ),
            )
            _send_error("bad_request", str(exc), request_id)


socketio.on_namespace(EmoNamespace("/emo"))
