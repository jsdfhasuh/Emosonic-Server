import os
import logging
import time

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
from .ws_state import DEFAULT_CLIENT_STALE_SECONDS, get_state


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
ACTION_EVENT_NAMES = {
    "auth.login": "auth_login",
    "device.register": "device_register",
    "session.subscribe": "session_subscribe",
    "session.unsubscribe": "session_unsubscribe",
    "playback.update": "playback_update",
    "queue.local.get": "queue_local_get",
    "queue.local.set": "queue_local_set",
    "queue.session.sync": "queue_session_sync",
    "queue.ready.complete": "queue_ready_complete",
}


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
    message = {
        "type": msg_type,
        "action": action,
        "payload": payload or {},
        "timestamp": time.time(),
    }
    message.update(extra)
    return message


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
    queue_state = state.get_queue(session_id)
    if queue_state is None:
        queue_state = getQueueState(session_id)
        if queue_state is not None:
            state.update_queue(
                session_id,
                queue_state.get("queueSongIds") or [],
                queue_state.get("currentIndex", 0),
                queue_state.get("positionMs", 0),
                queue_state.get("sourceClientId"),
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
        for local_queue in local_queues:
            state.update_local_queue(
                session_id,
                local_queue.get("sourceClientId"),
                local_queue.get("queueSongIds") or [],
                local_queue.get("currentIndex", 0),
                local_queue.get("positionMs", 0),
            )
    logger.debug("First Restoring %d local queues for session %s, sid %s", len(local_queues), session_id, sid)
    for local_queue in local_queues:
        client_info = state.get_client_for_sid(sid) # Try to find the client_id associated with this sid
        logger.debug(f"test local_queue sourceClientId={local_queue.get('sourceClientId')}, client_id={client_info['clientId']}")
        if client_info and local_queue.get("sourceClientId") == client_info['clientId']:
            continue  # Skip sending the local queue back to its own client, as it should already have the latest state
        socketio.emit(
            "message",
            _build_message("state", "queue.local.set", local_queue),
            to=sid,
            namespace="/emo",
        )

    playback_states = state.list_playback_states(session_id)
    if not playback_states:
        playback_states = getPlaybackStates(session_id)
        for playback_state in playback_states:
            state.update_playback_state(
                session_id,
                playback_state.get("sourceClientId"),
                playback_state,
            )

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

    roles = payload.get("roles") or []
    if not isinstance(roles, list):
        raise ValueError("roles must be a list")

    device_name = payload.get("deviceName") or client_id

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

    outgoing = _build_message(
        "command",
        message["action"],
        message["payload"],
        requestId=message.get("requestId"),
        sourceClientId=sender["clientId"],
        targetClientId=target_client_id,
    )
    socketio.emit("message", outgoing, to=target_sid, namespace="/emo")


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
            emit("message", _build_message("system", "system.pong", {}, requestId=request_id))
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
            elif action == "playback.update":
                if current_client is None:
                    raise PermissionError("Register the device before publishing state")
                session_id = payload.get("sessionId") or current_client.get("sessionId")
                playback_payload = dict(payload)
                playback_payload["sourceClientId"] = current_client.get("clientId")
                state.update_playback_state(
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
                        state.update_local_queue(
                            session_id,
                            client_id,
                            local_queue.get("queueSongIds") or [],
                            local_queue.get("currentIndex", 0),
                            local_queue.get("positionMs", 0),
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
                if current_client is None and not payload.get("clientId"):
                    raise PermissionError("Register the device before updating local queue")
                current_client_id = current_client.get("clientId")
                session_id = payload.get("sessionId") or current_client.get("sessionId")
                queue_song_ids = payload.get("queueSongIds")
                targetClientId = message.get("targetClientId","")
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
                    current_client_id,
                    queue_song_ids,
                    current_index,
                    position_ms,
                )
                saveLocalQueueState(
                    session_id,
                    current_client_id,
                    queue_song_ids,
                    current_index,
                    position_ms,
                )
                _log_emo_event(
                    logging.INFO,
                    "queue_local_set",
                    result="direct" if targetClientId else "broadcast",
                    user=current_user_name,
                    client_request_id=request_id,
                    target_client_id=targetClientId or "-",
                    **_build_queue_summary(
                        session_id,
                        current_client_id,
                        queue_song_ids,
                        current_index,
                        position_ms,
                    ),
                )
                _send_ack(request_id, {"updated": True})
                if targetClientId:
                    logger.info("Broadcasting local queue update to specific client %s", targetClientId)
                    _broadcast_local_queue_to_client(
                        targetClientId,
                        session_id,
                        current_client_id,
                    )
                else:
                    logger.info("Broadcasting local queue update to all clients in session %s", session_id)
                    _broadcast_local_queue(
                        current_user_name,
                        session_id,
                        current_client_id,
                    )
            elif action == "queue.session.sync":
                if current_client is None and not payload.get("clientId"):
                    raise PermissionError("Register the device before syncing queue")
                session_id = payload.get("sessionId") or current_client.get("sessionId")
                current_client_id = payload.get("clientId") or current_client.get("clientId")
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

                state.update_queue(
                    session_id,
                    queue_song_ids,
                    current_index,
                    position_ms,
                    current_client_id,
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
                _send_ack(request_id, {"updated": True})
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
