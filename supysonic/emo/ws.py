import os
import logging
import time
import uuid
from typing import Dict, Optional

from flask import (
    current_app,
    g,
    has_app_context,
    has_request_context,
    request,
    session,
)
from flask_socketio import Namespace, SocketIO, disconnect

from ..db import User, close_connection, open_connection
from ..logging_utils import format_log_event
from ..managers.user import UserManager
from .browser_auth import BROWSER_OTP_PREFIX, browser_one_time_passwords
from .protocol_metadata import (
    get_strict_v2_metadata,
    get_strict_v2_registration_metadata,
)
from .strict_v2_acceptance import consume_binding_emit_failure
from .strict_v2_contract import (
    ACTION_SCHEMAS,
    StrictRequestValidationError,
    is_strict_registration_request,
    validate_strict_output,
    validate_strict_request,
)
from .strict_v2_readiness import (
    CoreProfileNotReady,
    is_local_test_evidence_allowed,
    is_local_test_evidence_requested,
    negotiate_capabilities,
)
from .strict_v2_runtime import (
    RequestFingerprintConflict,
    StrictRequestCache,
    request_fingerprint,
)
from .strict_v2_safety import resolve_allowed_origins, strict_v2_safety
from .ws_store import (
    PlaybackContextAuthorityAmbiguousError,
    PlaybackContextClosedError,
    PlaybackContextEnsureConflictError,
    PlaybackContextIntentConflictError,
    PlaybackContextQueueRequiredError,
    PlaybackContextStaleVersionError,
    closeStrictPlaybackContextState,
    commitStrictPlaybackHandoff,
    completeStrictPlaybackHandoff,
    createPlaybackContextState,
    ensureStrictPlaybackContextState,
    failActivePlaybackHandoffsForRestart,
    getActivePlaybackHandoffs,
    getLocalQueueState,
    getLocalQueueStates,
    getDevicePlaybackStates,
    getPlaybackContextState,
    getPlaybackHandoff,
    getPlaybackHandoffByRequest,
    getPlaybackState,
    getPlaybackStates,
    getQueueState,
    listActivePlaybackContextBindings,
    listPlaybackContexts,
    mutateStrictPlaybackContextControl,
    mutateStrictPlaybackContextQueue,
    saveDevicePlaybackState,
    saveLocalQueueState,
    savePlaybackContextState,
    savePlaybackHandoff,
    savePlaybackState,
    saveQueueState,
    serializeDevicePlaybackStateV2,
    serializePlaybackContextV2,
    terminateStrictPlaybackHandoff,
    updatePlaybackContextState,
)
from .ws_state import (
    BroadcastCursorMismatchError,
    BroadcastInactiveError,
    BroadcastVersionMismatchError,
    ClientSeqStaleError,
    DEFAULT_CLIENT_STALE_SECONDS,
    DEFAULT_FOLLOW_DELAY_MS,
    PlaybackAuthorityMismatchError,
    PlaybackContextConflictError,
    PlaybackControlVersionMismatchError,
    QueueRevisionMismatchError,
    get_state,
)


logger = logging.getLogger(__name__)
state = get_state()
async_mode = os.environ.get("EMO_SOCKETIO_ASYNC_MODE") or None
socketio_kwargs = {
    "cors_allowed_origins": None,
    "max_http_buffer_size": 256 * 1024,
    "path": "/emo/ws",
    "ping_interval": 25,
    "ping_timeout": 20,
}
if async_mode is not None:
    socketio_kwargs["async_mode"] = async_mode
socketio = SocketIO(**socketio_kwargs)
strict_request_cache = StrictRequestCache()

ALLOWED_PRE_AUTH = {"auth.login"}
EVENT_CONFIRMED_ACTIONS = {
    "device.volume.update",
    "playback.update",
    "playback.ready",
    "playback.handoff.complete",
}
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
HANDOFF_ACTIONS = {
    "playback.handoff.start",
    "playback.handoff.cancel",
    "playback.handoff.complete",
}
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
    "device.setVolume": "device_set_volume",
    "device.volume.update": "device_volume_update",
    "session.subscribe": "session_subscribe",
    "session.unsubscribe": "session_unsubscribe",
    "follow.start": "follow_start",
    "follow.stop": "follow_stop",
    "playback.ready": "playback_ready",
    "playback.context.ensure": "playback_context_ensure",
    "playback.context.list": "playback_context_list",
    "playback.context.status": "playback_context_status",
    "playback.context.subscribe": "playback_context_subscribe",
    "playback.context.unsubscribe": "playback_context_unsubscribe",
    "playback.context.close": "playback_context_close",
    "playback.handoff.start": "playback_handoff_start",
    "playback.handoff.cancel": "playback_handoff_cancel",
    "playback.handoff.complete": "playback_handoff_complete",
    "playback.update": "playback_update",
    "queue.context.sync": "queue_context_sync",
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

CAPABILITY_EFFECTIVE_AT = "effectiveAtPlayback"
CAPABILITY_PLAYBACK_PREPARE = "playbackPrepare"
CAPABILITY_PLAYBACK_CONTEXT_V2 = "playbackContextV2"
CAPABILITY_CAN_PLAY = "canPlay"
CAPABILITY_CAN_PAUSE = "canPause"
CAPABILITY_CAN_SEEK = "canSeek"
CAPABILITY_CAN_SET_VOLUME = "canSetVolume"
CAPABILITY_REMOTE_VOLUME_CONTROL = "remoteVolumeControl"
CAPABILITY_SUPPORTS_FOLLOW = "supportsFollow"
CAPABILITY_SUPPORTS_BROADCAST = "supportsBroadcast"
STRICT_BROADCAST_PARTICIPANT_CAPABILITIES = (
    CAPABILITY_SUPPORTS_BROADCAST,
    CAPABILITY_CAN_PLAY,
    CAPABILITY_CAN_PAUSE,
    CAPABILITY_CAN_SEEK,
)
STRICT_V2_REQUIRED_CAPABILITIES = (
    CAPABILITY_PLAYBACK_CONTEXT_V2,
    CAPABILITY_PLAYBACK_PREPARE,
    CAPABILITY_EFFECTIVE_AT,
    CAPABILITY_CAN_PLAY,
    CAPABILITY_CAN_PAUSE,
    CAPABILITY_CAN_SEEK,
    CAPABILITY_CAN_SET_VOLUME,
    CAPABILITY_SUPPORTS_FOLLOW,
    CAPABILITY_SUPPORTS_BROADCAST,
)
STRICT_V2_OPTIONAL_CAPABILITIES = (CAPABILITY_REMOTE_VOLUME_CONTROL,)
PROTOCOL_LEGACY = "legacy"
PROTOCOL_SINGLE_FUTURE = "single_future"
PROTOCOL_TWO_PHASE = "two_phase"
TWO_PHASE_COMMIT_LEAD_MS = 350
SINGLE_PHASE_COMMIT_LEAD_MS = 700
PREPARE_TIMEOUT_MS = 1200
HANDOFF_PREPARE_TIMEOUT_MS = 8000
HANDOFF_COMPLETE_TIMEOUT_MS = 5000
BROADCAST_AUTHORITY_DISCONNECT_TIMEOUT_MS = 30000


class BroadcastConflictError(Exception):
    def __init__(self, message, current_version=None, current_control_version=None):
        super().__init__(message)
        self.current_version = current_version
        self.current_control_version = current_control_version


class QueueConflictError(Exception):
    def __init__(self, message, current_queue_revision=None):
        super().__init__(message)
        self.current_queue_revision = current_queue_revision


class ControlConflictError(Exception):
    def __init__(self, message, current_control_version=None):
        super().__init__(message)
        self.current_control_version = current_control_version


class PlaybackAuthorityOfflineError(Exception):
    pass


class CapabilityRequiredError(PermissionError):
    pass


class FollowConflictError(Exception):
    pass


class FollowControlForbiddenError(PermissionError):
    pass


def _log_emo_event(level, event, **fields):
    if has_request_context() and getattr(g, "emo_strict_request", False):
        allowed_fields = {
            "action",
            "authority_client_id",
            "authority_device_session_id",
            "client_request_id",
            "context_count",
            "exception_type",
            "failed_sid_count",
            "recipient_count",
            "result",
            "source_client_id",
            "user",
        }
        fields = {
            key: value
            for key, value in fields.items()
            if key in allowed_fields
        }
        started_at = getattr(g, "emo_request_started_at", None)
        if started_at is not None:
            fields["latency_ms"] = max(
                0,
                int((time.monotonic() - started_at) * 1000),
            )
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


def _serialize_client_info_v2(client, target_client=None):
    client_capabilities = client.get("capabilities")
    if not isinstance(client_capabilities, dict):
        client_capabilities = {}
    target_capabilities = _client_capabilities(target_client)
    capability_names = list(STRICT_V2_REQUIRED_CAPABILITIES)
    if CAPABILITY_REMOTE_VOLUME_CONTROL in target_capabilities:
        capability_names.extend(STRICT_V2_OPTIONAL_CAPABILITIES)
    capabilities = {
        capability: (
            client_capabilities[capability]
            if type(client_capabilities.get(capability)) is bool
            else False
        )
        for capability in capability_names
    }
    payload = {
        "clientId": client.get("clientId"),
        "deviceSessionId": client.get("deviceSessionId"),
        "deviceName": client.get("deviceName"),
        "roles": list(client.get("roles") or []),
        "capabilities": capabilities,
    }
    if isinstance(client.get("alias"), str) and client.get("alias"):
        payload["alias"] = client["alias"]
    if CAPABILITY_REMOTE_VOLUME_CONTROL in target_capabilities:
        volume_state = state.get_device_volume_state(
            client.get("userName"),
            client.get("clientId"),
            client.get("deviceSessionId"),
        )
        if volume_state is not None:
            payload["volumeState"] = {
                "volume": volume_state["volume"],
                "clientSeq": volume_state["clientSeq"],
                "serverUpdatedAtMs": volume_state["serverUpdatedAtMs"],
            }
    return payload


def _serialize_clients_for_target(clients, target_client):
    if _is_strict_playback_context_v2(target_client):
        return [
            _serialize_client_info_v2(client, target_client)
            for client in sorted(clients, key=lambda item: item.get("clientId") or "")
        ]
    return clients


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
    webapp_config = app.config["WEBAPP"]
    development = bool(webapp_config.get("emo_development_mode", False))
    local_test_evidence_requested = is_local_test_evidence_requested(
        webapp_config
    )
    local_test_evidence_allowed = is_local_test_evidence_allowed(
        webapp_config,
        app.testing,
    )
    if local_test_evidence_requested:
        if local_test_evidence_allowed:
            logger.warning(
                format_log_event(
                    "emo",
                    "strict_v2_local_test_evidence",
                    result="enabled",
                    app_testing=app.testing,
                )
            )
        else:
            logger.warning(
                format_log_event(
                    "emo",
                    "strict_v2_local_test_evidence",
                    result="ignored",
                    reason="development_mode_disabled",
                )
            )
    allowed_origins = resolve_allowed_origins(
        webapp_config,
        development=development,
    )
    socketio.init_app(
        app,
        path="/emo/ws",
        cors_allowed_origins=allowed_origins,
        max_http_buffer_size=256 * 1024,
        ping_interval=webapp_config.get("emo_socketio_ping_interval", 25),
        ping_timeout=webapp_config.get("emo_socketio_ping_timeout", 20),
    )
    strict_v2_safety.configure(webapp_config)
    strict_request_cache.clear_all()
    state.restore_strict_playback_contexts(listPlaybackContexts())
    failed_handoffs = failActivePlaybackHandoffsForRestart()
    if failed_handoffs:
        logger.warning(
            "Marked %d active Emo handoffs failed after restart",
            len(failed_handoffs),
        )
    stopped_broadcasts = state.stop_active_broadcasts_for_restart()
    if stopped_broadcasts:
        logger.warning(
            "Marked %d active strict Emo broadcasts stopped after restart",
            len(stopped_broadcasts),
        )
    metadata = get_strict_v2_metadata()
    logger.warning(
        format_log_event(
            "emo",
            "strict_v2_registration_metadata",
            protocol_version=metadata["protocolVersion"],
            schema_hash=metadata["schemaHash"],
            server_build_commit=metadata["serverBuildCommit"],
        )
    )
    return socketio


def begin_strict_v2_shutdown(timeout_seconds=None):
    if timeout_seconds is None:
        timeout_seconds = current_app.config["WEBAPP"].get(
            "emo_strict_shutdown_grace_seconds",
            5,
        )
    drained = strict_v2_safety.begin_shutdown(float(timeout_seconds))
    for sid in state.list_session_sids():
        socketio.server.disconnect(sid, namespace="/emo")
    return drained


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
        if action in {"system.ack", "system.error"}:
            request_action = (
                getattr(g, "emo_request_action", None)
                if has_request_context()
                else None
            )
            if request_action:
                message_payload["action"] = request_action
        if action == "system.error":
            retryable_codes = {"authority_offline", "rate_limited", "internal_error"}
            message_payload.setdefault(
                "retryable",
                message_payload.get("code") in retryable_codes,
            )
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


def _message_for_recipient(message, target_sid):
    """Add strict-v2 connection provenance for one Socket.IO recipient."""
    target_client = state.get_client_for_sid(target_sid)
    strict_target = _is_strict_playback_context_v2(target_client)
    strict_request_reply = (
        has_request_context()
        and target_sid == request.sid
        and getattr(g, "emo_strict_request", False)
    )
    if not strict_target and not strict_request_reply:
        return message

    outgoing = dict(message)
    if outgoing.get("action") not in {"system.ack", "system.error", "system.pong", "device.list"}:
        outgoing.pop("targetClientId", None)
    outgoing_payload = _strip_strict_transport_fields(outgoing.get("payload"))
    if (
        isinstance(outgoing_payload, dict)
        and outgoing.get("action")
        not in {
            "system.pong",
            "broadcast.play",
            "broadcast.pause",
            "broadcast.seek",
            "broadcast.playItem",
        }
    ):
        outgoing_payload.pop("serverTimeMs", None)
    outgoing["payload"] = outgoing_payload
    if strict_target:
        session_info = state.get_session(target_sid)
        if session_info is not None:
            connection_nonce = session_info.get("connectionNonce")
            if isinstance(connection_nonce, str) and connection_nonce:
                outgoing["connectionNonce"] = connection_nonce
                outgoing["connectionEpoch"] = 1
    validate_strict_output(outgoing, registered=strict_target)
    return outgoing


def _strip_strict_transport_fields(value):
    if isinstance(value, dict):
        return {
            key: _strip_strict_transport_fields(item)
            for key, item in value.items()
            if key not in {"sessionId", "targetClientId"}
        }
    if isinstance(value, list):
        return [_strip_strict_transport_fields(item) for item in value]
    return value


def _emit_message(
    message,
    target_sid=None,
    record_settlement=True,
    emit_reserved=False,
):
    """Emit a message to one recipient with its strict-v2 provenance."""
    if target_sid is None:
        if not has_request_context():
            raise RuntimeError("A target Socket.IO sid is required outside a request")
        target_sid = request.sid
    outgoing = _message_for_recipient(message, target_sid)
    settled_request_id = (
        getattr(g, "emo_settled_request_id", None)
        if record_settlement and has_request_context()
        else None
    )
    if (
        settled_request_id is not None
        and target_sid == request.sid
        and outgoing.get("requestId") == settled_request_id
    ):
        _log_emo_event(
            logging.ERROR,
            "duplicate_settlement",
            result="suppressed",
            action=outgoing.get("action"),
            client_request_id=settled_request_id,
        )
        return
    cache_context = (
        getattr(g, "emo_request_cache_context", None)
        if record_settlement and has_request_context()
        else None
    )
    settlement_stored = (
        cache_context is not None
        and target_sid == request.sid
        and outgoing.get("requestId") == cache_context["requestId"]
    )
    if settlement_stored:
        strict_request_cache.store_result(
            cache_context["connectionNonce"],
            cache_context["requestId"],
            cache_context["fingerprint"],
            {"messages": [outgoing]},
        )
        g.emo_settled_request_id = cache_context["requestId"]
        g.emo_request_cache_context = None

    reserved = emit_reserved or strict_v2_safety.reserve_emit(target_sid)
    if not reserved:
        if settlement_stored:
            _log_emo_event(
                logging.ERROR,
                "settlement_emit",
                result="send_buffer_full",
                action=outgoing.get("action"),
                client_request_id=outgoing.get("requestId"),
            )
            return
        raise RuntimeError("Socket.IO send buffer is full")
    try:
        try:
            socketio.emit(
                "message",
                outgoing,
                to=target_sid,
                namespace="/emo",
            )
        except Exception as exc:
            if not settlement_stored:
                raise
            _log_emo_event(
                logging.ERROR,
                "settlement_emit",
                result="emit_failed",
                action=outgoing.get("action"),
                client_request_id=outgoing.get("requestId"),
                exception_type=type(exc).__name__,
            )
    finally:
        if not emit_reserved:
            strict_v2_safety.release_emit(target_sid)


def _store_event_confirmations(messages):
    if not has_request_context():
        raise RuntimeError("Event confirmations require a request context")
    cache_context = getattr(g, "emo_request_cache_context", None)
    if cache_context is None:
        return
    outgoing = [
        _message_for_recipient(message, request.sid)
        for message in messages
        if message is not None
    ]
    if not outgoing:
        raise RuntimeError("Event-confirmed request produced no canonical confirmation")
    strict_request_cache.store_result(
        cache_context["connectionNonce"],
        cache_context["requestId"],
        cache_context["fingerprint"],
        {"messages": outgoing},
    )
    g.emo_settled_request_id = cache_context["requestId"]
    g.emo_request_cache_context = None


def _server_time_ms():
    return int(time.time() * 1000)


def _new_prepare_id():
    return f"prep-{uuid.uuid4().hex[:12]}"


def _new_handoff_id():
    return f"handoff-{uuid.uuid4().hex[:12]}"


def _device_session_id(client):
    if not isinstance(client, dict):
        return None
    return client.get("deviceSessionId") or client.get("sessionId")


def _resolve_device_session_id(payload, current_client):
    return (
        payload.get("deviceSessionId")
        or payload.get("sessionId")
        or _device_session_id(current_client)
    )


def _resolve_playback_context_id(payload, current_client):
    return (
        payload.get("playbackContextId")
        or payload.get("sessionId")
        or _device_session_id(current_client)
    )


def _is_context_payload(payload):
    return "playbackContextId" in payload or "deviceSessionId" in payload


def _is_follow_context_payload(payload):
    return "sourcePlaybackContextId" in payload or "playbackContextId" in payload


def _is_strict_playback_context_v2(client):
    return _client_supports(client, CAPABILITY_PLAYBACK_CONTEXT_V2)


def _reject_session_id_for_strict_v2(payload, strict_v2):
    if strict_v2 and "sessionId" in payload:
        raise ValueError("sessionId is not allowed in PlaybackContext v2 payload")


def _resolve_v2_device_session_id(payload, current_client, strict_v2=False):
    _reject_session_id_for_strict_v2(payload, strict_v2)
    current_device_session_id = None
    if isinstance(current_client, dict):
        current_device_session_id = current_client.get("deviceSessionId")
    return payload.get("deviceSessionId") or current_device_session_id


def _resolve_v2_playback_context_id(payload, strict_v2=False):
    _reject_session_id_for_strict_v2(payload, strict_v2)
    return payload.get("playbackContextId")


def _client_capabilities(client):
    capabilities = client.get("capabilities") if isinstance(client, dict) else None
    return capabilities if isinstance(capabilities, dict) else {}


def _client_supports(client, capability):
    return _client_capabilities(client).get(capability) is True


def _select_playback_protocol(client_ids, user_name=None):
    if not client_ids:
        return PROTOCOL_LEGACY

    clients = []
    for client_id in client_ids:
        client = state.get_client(client_id, user_name=user_name)
        if client is None or not _has_role(client, "player"):
            return PROTOCOL_LEGACY
        clients.append(client)

    if all(
        _client_supports(client, CAPABILITY_EFFECTIVE_AT)
        and _client_supports(client, CAPABILITY_PLAYBACK_PREPARE)
        for client in clients
    ):
        return PROTOCOL_TWO_PHASE
    if all(_client_supports(client, CAPABILITY_EFFECTIVE_AT) for client in clients):
        return PROTOCOL_SINGLE_FUTURE
    return PROTOCOL_LEGACY


def _commit_lead_ms(protocol):
    if protocol == PROTOCOL_SINGLE_FUTURE:
        return SINGLE_PHASE_COMMIT_LEAD_MS
    return TWO_PHASE_COMMIT_LEAD_MS


def _effective_at_server_ms(protocol):
    return _server_time_ms() + _commit_lead_ms(protocol)


def _send_ack(request_id=None, payload=None):
    if has_request_context() and getattr(g, "emo_suppress_success_ack", False):
        return
    _emit_message(_build_message("system", "system.ack", payload, requestId=request_id))


def _send_error(code, message, request_id=None, **fields):
    current_client = (
        state.get_client_for_sid(request.sid) if has_request_context() else None
    )
    if _is_strict_playback_context_v2(current_client):
        if code == "stale_client_seq":
            code = "client_sequence_conflict"
        elif code == "follow_control_forbidden":
            code = "forbidden"
    payload = {"code": code, "message": message}
    payload.update({key: value for key, value in fields.items() if value is not None})
    _emit_message(_build_message("system", "system.error", payload, requestId=request_id))


def _send_direct_response(msg_type, action, request_id, payload):
    _emit_message(
        _build_message(
            msg_type,
            action,
            payload,
            requestId=request_id,
        )
    )


def _prepare_strict_request_cache(
    message,
    session_info,
    action,
    request_id,
):
    connection_nonce = (
        None if session_info is None else session_info.get("connectionNonce")
    )
    fingerprint = request_fingerprint(
        message.get("type"),
        action,
        message.get("payload"),
    )
    try:
        lookup = strict_request_cache.lookup_or_reserve(
            connection_nonce,
            request_id,
            fingerprint,
        )
    except RequestFingerprintConflict as exc:
        _send_error("conflict", str(exc), request_id)
        return False
    if lookup.status == "cached":
        for cached_message in lookup.result["messages"]:
            _emit_message(
                cached_message,
                request.sid,
                record_settlement=False,
            )
        return False
    if lookup.status == "in_flight":
        _log_emo_event(
            logging.INFO,
            "strict_v2_duplicate_in_flight",
            action=action,
            client_request_id=request_id,
            sid=request.sid,
        )
        return False
    g.emo_request_cache_context = {
        "connectionNonce": connection_nonce,
        "requestId": request_id,
        "fingerprint": fingerprint,
    }
    g.emo_suppress_success_ack = action in EVENT_CONFIRMED_ACTIONS

    retry_after_ms = strict_v2_safety.check_rate_limit(
        connection_nonce,
        action,
    )
    if retry_after_ms is None:
        return True
    _log_emo_event(
        logging.WARNING,
        "strict_v2_rate_limit",
        result="rate_limited",
        action=action,
        client_request_id=request_id,
        user=None if session_info is None else session_info.get("userName"),
        source_client_id=None,
    )
    _send_error(
        "rate_limited",
        "Strict-v2 request rate limit exceeded",
        request_id,
        retryAfterMs=retry_after_ms,
    )
    return False


def _run_post_commit_push(action, request_id, push):
    try:
        return push()
    except Exception:
        _log_emo_event(
            logging.ERROR,
            "post_commit_push",
            result="emit_failed",
            action=action,
            client_request_id=request_id,
        )
        return None


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


def _authenticate(payload: Dict[str, object]) -> Optional[User]:
    user_name = payload.get("u")
    password = payload.get("p")
    if not user_name or not password:
        return None

    if isinstance(password, str) and password.startswith(BROWSER_OTP_PREFIX):
        session_user = _get_session_user()
        browser_session_id = session.get("emo_browser_session_id")
        if (
            session_user is not None
            and session_user.name == user_name
            and isinstance(browser_session_id, str)
            and browser_session_id
            and browser_one_time_passwords.consume(
                user_name,
                browser_session_id,
                password,
            )
        ):
            return session_user

    open_connection(reuse=True)
    try:
        return UserManager.try_auth(user_name, password)
    finally:
        close_connection()


def _broadcast_clients(user_name: str) -> None:
    """Broadcast topology snapshots only to legacy clients."""
    clients = _list_clients(user_name=user_name)
    for target_sid, target_client in state.list_sids(user_name=user_name):
        if target_client is None or _is_strict_playback_context_v2(target_client):
            continue
        message = _build_message(
            "state",
            "device.list",
            {"devices": _serialize_clients_for_target(clients, target_client)},
        )
        _emit_message(message, target_sid)


def _disconnect_strict_recipient(target_sid):
    try:
        socketio.server.disconnect(target_sid, namespace="/emo")
    except Exception:
        logger.exception(
            "Unable to disconnect a stale strict-v2 recipient"
        )


def _consume_acceptance_binding_emit_failure(user_name, target_sid):
    if not has_app_context() or not is_local_test_evidence_allowed(
        current_app.config["WEBAPP"],
        current_app.testing,
    ):
        return False
    target_client = state.get_client_for_sid(target_sid)
    if not isinstance(target_client, dict):
        return False
    client_id = target_client.get("clientId")
    device_session_id = target_client.get("deviceSessionId")
    if not isinstance(client_id, str) or not isinstance(device_session_id, str):
        return False
    if not consume_binding_emit_failure(
        user_name,
        client_id,
        device_session_id,
    ):
        return False
    _log_emo_event(
        logging.WARNING,
        "strict_v2_acceptance_fault",
        result="injected",
        fault="binding_emit_failure",
        user=user_name,
        client_id=client_id,
        device_session_id=device_session_id,
        sid=target_sid,
    )
    return True


def _broadcast_playback_context_bindings_changed(
    user_name,
    affected_authority_pairs,
):
    authority_pairs = sorted(
        {
            tuple(authority_pair)
            for authority_pair in affected_authority_pairs
            if (
                isinstance(authority_pair, (list, tuple))
                and len(authority_pair) == 3
                and authority_pair[0] == user_name
            )
        }
    )
    for _, authority_client_id, authority_device_session_id in authority_pairs:
        recipient_sids = state.list_strict_controller_sids(user_name)
        failed_sids = []
        for target_sid in recipient_sids:
            if not strict_v2_safety.reserve_emit(target_sid):
                failed_sids.append(target_sid)
                _disconnect_strict_recipient(target_sid)
                continue
            try:
                if _consume_acceptance_binding_emit_failure(
                    user_name,
                    target_sid,
                ):
                    raise RuntimeError(
                        "injected strict-v2 acceptance binding emit failure"
                    )
                _emit_message(
                    _build_message(
                        "event",
                        "playback.context.bindings.changed",
                        {
                            "authorityClientId": authority_client_id,
                            "authorityDeviceSessionId": (
                                authority_device_session_id
                            ),
                        },
                    ),
                    target_sid,
                    record_settlement=False,
                    emit_reserved=True,
                )
            except Exception:
                failed_sids.append(target_sid)
                _disconnect_strict_recipient(target_sid)
            finally:
                strict_v2_safety.release_emit(target_sid)
        _log_emo_event(
            logging.INFO if not failed_sids else logging.ERROR,
            "playback_context_bindings_changed",
            result="success" if not failed_sids else "recipient_failed",
            user=user_name,
            authority_client_id=authority_client_id,
            authority_device_session_id=authority_device_session_id,
            recipient_count=len(recipient_sids),
            failed_sid_count=len(failed_sids),
        )


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
        _emit_message(message, target_sid)


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
            _emit_message(message, target_sid)


def _broadcast_playback_context_queue(user_name, playback_context_id):
    context = state.get_playback_context(playback_context_id)
    if context is None:
        return
    message = _build_message("state", "queue.session.sync", context)
    target_sids = {
        sid for sid, _ in state.list_sids(user_name=user_name)
    }
    for target_sid in target_sids:
        _emit_message(message, target_sid)


def _broadcast_context_queue_v2(user_name, playback_context_id):
    context = state.get_playback_context(playback_context_id)
    if context is None:
        return
    message = _build_message(
        "state",
        "queue.context.sync",
        _build_context_queue_payload_v2(context),
    )
    target_sids = set(
        state.list_playback_context_subscribers(
            playback_context_id,
            user_name=user_name,
        )
    )
    target_sids.update(
        state.list_context_participant_sids(
            playback_context_id,
            user_name=user_name,
        )
    )
    for target_sid in target_sids:
        _emit_message(message, target_sid)


def _build_context_queue_payload_v2(context):
    payload = {
        "playbackContextId": context.get("playbackContextId"),
        "authorityClientId": context.get("authorityClientId"),
        "queueSongIds": list(context.get("queueSongIds") or []),
        "state": context.get("state") or "idle",
        "positionMs": context.get("positionMs", 0),
        "queueRevision": context.get("queueRevision", 1),
        "controlVersion": context.get("controlVersion", 1),
        "version": context.get("version", 1),
        "epoch": context.get("epoch", 1),
        "serverUpdatedAtMs": context.get("serverUpdatedAtMs"),
    }
    queue_song_ids = payload["queueSongIds"]
    if queue_song_ids:
        payload["currentIndex"] = context.get("currentIndex", 0)
        payload["trackId"] = context.get("trackId") or queue_song_ids[
            payload["currentIndex"]
        ]
    timeline_id = context.get("timelineId")
    if timeline_id is not None:
        payload["timelineId"] = timeline_id
    return payload


def _broadcast_playback_context_state(user_name, playback_context_id):
    context = state.get_playback_context(playback_context_id)
    if context is None:
        return
    message = _build_message("state", "playback.update", context)
    target_sids = {
        sid for sid, _ in state.list_sids(user_name=user_name)
    }
    for target_sid in target_sids:
        _emit_message(message, target_sid)


def _broadcast_playback_context_state_v2(user_name, playback_context_id):
    context = state.get_playback_context(playback_context_id)
    if context is None:
        return
    message = _build_playback_context_status_message(context)
    target_sids = set(
        state.list_playback_context_subscribers(
            playback_context_id,
            user_name=user_name,
        )
    )
    target_sids.update(
        state.list_context_participant_sids(
            playback_context_id,
            user_name=user_name,
        )
    )
    for target_sid in target_sids:
        _emit_message(message, target_sid)
    return message


def _build_playback_context_status_message(context):
    return _build_message(
        "state",
        "playback.context.status",
        _build_playback_context_status_payload(context),
    )


def _build_v2_playback_update_payload(device_state):
    if not isinstance(device_state, dict):
        return None
    payload = {
        "playbackContextId": device_state.get("playbackContextId"),
        "sourceClientId": device_state.get("sourceClientId"),
        "deviceSessionId": device_state.get("deviceSessionId"),
        "state": device_state.get("state"),
        "positionMs": device_state.get("positionMs"),
    }
    if not all(
        payload.get(field_name) is not None
        for field_name in (
            "playbackContextId",
            "sourceClientId",
            "deviceSessionId",
            "state",
            "positionMs",
        )
    ):
        return None
    for field_name in ("trackId", "volume", "muted", "clientSeq", "serverUpdatedAtMs"):
        if field_name in device_state and device_state[field_name] is not None:
            payload[field_name] = device_state[field_name]
    return payload


def _broadcast_v2_playback_update(user_name, playback_context_id, device_state):
    message = _build_v2_playback_update_message(device_state)
    if message is None:
        return
    target_sids = set(
        state.list_playback_context_subscribers(
            playback_context_id,
            user_name=user_name,
        )
    )
    target_sids.update(
        state.list_context_participant_sids(
            playback_context_id,
            user_name=user_name,
        )
    )
    for target_sid in target_sids:
        _emit_message(message, target_sid)
    return message


def _build_v2_playback_update_message(device_state):
    payload = _build_v2_playback_update_payload(device_state)
    if payload is None:
        return None
    return _build_message("event", "playback.update", payload)


def _broadcast_playback_context_closed_v2(user_name, playback_context_id):
    target_sids = set(
        state.list_playback_context_subscribers(
            playback_context_id,
            user_name=user_name,
        )
    )
    target_sids.update(
        state.list_context_participant_sids(
            playback_context_id,
            user_name=user_name,
        )
    )
    message = _build_message(
        "event",
        "playback.context.closed",
        {"playbackContextId": playback_context_id},
    )
    for target_sid in target_sids:
        _emit_message(message, target_sid)


def _save_playback_state_snapshot(user_name, playback_state):
    if not playback_state:
        return
    session_id = playback_state.get("sessionId")
    source_client_id = playback_state.get("sourceClientId")
    if not session_id or not source_client_id:
        return
    savePlaybackState(session_id, user_name, source_client_id, playback_state)


def _save_playback_context_snapshot(user_name, playback_context):
    if not playback_context:
        return
    playback_context_id = playback_context.get("playbackContextId")
    if not playback_context_id:
        return
    savePlaybackContextState(playback_context_id, user_name, playback_context)


def _create_playback_context_snapshot(user_name, playback_context):
    if not playback_context:
        return False
    playback_context_id = playback_context.get("playbackContextId")
    if not playback_context_id:
        return False
    return createPlaybackContextState(playback_context_id, user_name, playback_context)


def _update_playback_context_snapshot(user_name, playback_context):
    if not playback_context:
        return False
    playback_context_id = playback_context.get("playbackContextId")
    if not playback_context_id:
        return False
    return updatePlaybackContextState(playback_context_id, user_name, playback_context)


def _save_device_playback_state_snapshot(user_name, device_state):
    if not device_state:
        return
    playback_context_id = device_state.get("playbackContextId")
    device_session_id = device_state.get("deviceSessionId") or device_state.get("sessionId")
    source_client_id = device_state.get("sourceClientId")
    if not playback_context_id or not device_session_id or not source_client_id:
        return
    saveDevicePlaybackState(
        playback_context_id,
        device_session_id,
        user_name,
        source_client_id,
        device_state,
        is_authority=device_state.get("isAuthority") is True,
        mode=device_state.get("mode") or "normal",
    )


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
        _emit_message(message, target_sid)


def _broadcast_local_queue_to_client(user_name, target_client_id, session_id, client_id):
    local_queue = state.get_local_queue(session_id, client_id)
    if local_queue is None:
        return
    payload = dict(local_queue)
    message = _build_message("state", "queue.local.set", payload)
    message["targetClientId"] = target_client_id
    target_sid = state.get_sid_for_client(target_client_id, user_name=user_name)
    if target_sid:
        _emit_message(message, target_sid)


def _broadcast_queue_ready_complete(user_name, session_id, ready_payload, exclude_sid=None):
    message = _build_message("state", "queue.ready.complete", ready_payload)
    target_sids = {
        sid for sid, _ in state.list_sids(user_name=user_name, session_id=session_id)
    }
    target_sids.update(state.list_subscribers(session_id, user_name=user_name))
    if exclude_sid is not None:
        target_sids.discard(exclude_sid)
    for target_sid in target_sids:
        _emit_message(message, target_sid)


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
        _emit_message(_build_message("state", "queue.session.sync", queue_state), sid)

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
        _emit_message(_build_message("state", "queue.local.set", local_queue), sid)

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
            _emit_message(
                _build_message(
                    "state",
                    "broadcast.status",
                    _build_broadcast_status_payload(active_broadcast),
                ),
                sid,
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
        _emit_message(_build_message("state", "playback.update", playback_state), sid)


def _register_device(sid, user_name, payload):
    client_id = payload.get("clientId")
    if not isinstance(client_id, str) or not client_id:
        raise ValueError("clientId must be a non-empty string")

    roles = payload.get("roles")
    if roles is None:
        roles = []
    if not isinstance(roles, list):
        raise ValueError("roles must be a list")

    device_name = payload.get("deviceName")
    capabilities = payload.get("capabilities") or {}
    strict_v2 = (
        isinstance(capabilities, dict)
        and capabilities.get(CAPABILITY_PLAYBACK_CONTEXT_V2) is True
    )
    if strict_v2:
        if not roles or len(roles) > 2 or len(set(roles)) != len(roles):
            raise ValueError("strict-v2 roles must contain distinct player/controller values")
        if not all(role in {"player", "controller"} for role in roles):
            raise ValueError("strict-v2 roles contain an unsupported value")
        roles = [role for role in ("player", "controller") if role in roles]
        for capability in STRICT_V2_REQUIRED_CAPABILITIES:
            if type(capabilities.get(capability)) is not bool:
                raise ValueError(
                    f"strict-v2 capabilities.{capability} must be a boolean"
                )
        if not isinstance(device_name, str) or not device_name.strip():
            raise ValueError("strict-v2 deviceName must be a non-empty string")
        negotiated_capabilities = negotiate_capabilities(
            capabilities,
            roles,
            current_app.config["WEBAPP"],
            allow_local_test_evidence=is_local_test_evidence_allowed(
                current_app.config["WEBAPP"],
                current_app.testing,
            ),
        )
    else:
        if device_name is None:
            device_name = client_id
        if not isinstance(device_name, str):
            raise ValueError("deviceName must be a string")
        device_name = device_name.strip() or client_id

    device_name = device_name.strip()
    alias = payload.get("alias")
    if alias is None:
        alias = payload.get("deviceAlias")
    if alias is None:
        alias = device_name

    if not isinstance(alias, str):
        raise ValueError("alias must be a string")
    if strict_v2 and not alias.strip():
        raise ValueError("strict-v2 alias must be a non-empty string")
    alias = alias.strip() or device_name
    if strict_v2:
        _reject_session_id_for_strict_v2(payload, strict_v2=True)
        device_session_id = payload.get("deviceSessionId")
    else:
        device_session_id = payload.get("deviceSessionId") or payload.get("sessionId") or client_id
    if not isinstance(device_session_id, str) or not device_session_id:
        raise ValueError("deviceSessionId must be a non-empty string")

    client_info = {
        "userName": user_name,
        "deviceName": device_name,
        "alias": alias,
        "roles": roles,
        "deviceSessionId": device_session_id,
        "capabilities": negotiated_capabilities if strict_v2 else capabilities,
    }
    if strict_v2:
        client_info["rawCapabilities"] = dict(capabilities)
    if not strict_v2:
        client_info["sessionId"] = device_session_id

    registered_client = state.register_client(sid, client_id, client_info)
    return registered_client


def _route_command(sender, message):
    target_client_id, target_sid, target_client = _resolve_control_target(sender, message)

    outgoing = _build_message(
        "command",
        message["action"],
        message["payload"],
        requestId=message.get("requestId"),
        sourceClientId=sender["clientId"],
        targetClientId=target_client_id,
    )
    _emit_message(outgoing, target_sid)


def _resolve_control_target(sender, message):
    target_client_id = message.get("targetClientId")
    if not target_client_id:
        raise ValueError("Missing targetClientId")

    user_name = sender.get("userName")
    target_sid = state.get_sid_for_client(target_client_id, user_name=user_name)
    target_client = state.get_client(target_client_id, user_name=user_name)
    if target_client is None and state.get_client(target_client_id) is not None:
        raise PermissionError("Cross-user control is not allowed")
    if target_sid is None or target_client is None:
        raise LookupError("Target client is offline")

    if target_client.get("userName") != sender.get("userName"):
        raise PermissionError("Cross-user control is not allowed")

    _ensure_not_follow_source_control(sender, target_client_id, message["action"])

    return target_client_id, target_sid, target_client


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


def _ensure_not_follow_source_context_control(current_client, playback_context_id, action):
    if current_client is None:
        return
    relationship = _get_active_follow_relationship(current_client.get("clientId"))
    if relationship is None:
        return
    if relationship.get("sourcePlaybackContextId") != playback_context_id:
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
    if broadcast.get("playbackContextId") is not None:
        payload["playbackContextId"] = broadcast.get("playbackContextId")
        payload["contextType"] = "broadcast"
    if broadcast.get("effectiveAtServerMs") is not None:
        payload["effectiveAtServerMs"] = broadcast.get("effectiveAtServerMs")
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


def _build_strict_broadcast_snapshot(broadcast):
    payload = {
        "playbackContextId": broadcast.get("playbackContextId"),
        "broadcastId": broadcast.get("broadcastId"),
        "ownerClientId": broadcast.get("ownerClientId"),
        "authorityClientId": broadcast.get("authorityClientId"),
        "queueSongIds": list(broadcast.get("queueSongIds") or []),
        "currentIndex": broadcast.get("currentIndex", 0),
        "positionMs": broadcast.get("positionMs", 0),
        "state": broadcast.get("state") or "stopped",
        "version": broadcast.get("version", 1),
        "queueRevision": broadcast.get("queueRevision", 1),
        "controlVersion": broadcast.get("controlVersion", 1),
        "epoch": broadcast.get("epoch", 1),
        "serverUpdatedAtMs": broadcast.get("serverUpdatedAtMs"),
        "playbackRate": broadcast.get("playbackRate", 1.0),
        "participants": sorted(set(broadcast.get("participants") or [])),
    }
    track_id = broadcast.get("trackId")
    if track_id is not None:
        payload["trackId"] = track_id
    return payload


def _strict_broadcast_client_online(user_name, client_id):
    sid = state.get_sid_for_client(client_id, user_name=user_name)
    return sid is not None and socketio.server.manager.is_connected(
        sid,
        namespace="/emo",
    )


def _build_strict_broadcast_participant_states(broadcast):
    broadcast_id = broadcast.get("broadcastId")
    initial_state = broadcast.get("state") or "stopped"
    initial_position_ms = broadcast.get("positionMs", 0)
    participant_states = []
    for client_id in sorted(set(broadcast.get("participants") or [])):
        feedback = state.get_broadcast_participant_state(
            broadcast_id,
            client_id,
        ) or {}
        client_seq = feedback.get("clientSeq")
        server_updated_at_ms = feedback.get("serverUpdatedAtMs")
        has_feedback = (
            type(client_seq) is int
            and client_seq >= 1
            and type(server_updated_at_ms) is int
        )
        participant_state = {
            "broadcastId": broadcast_id,
            "clientId": client_id,
            "state": feedback.get("state") if has_feedback else initial_state,
            "positionMs": (
                feedback.get("positionMs", initial_position_ms)
                if has_feedback
                else initial_position_ms
            ),
            "online": _strict_broadcast_client_online(
                broadcast.get("userName"),
                client_id,
            ),
        }
        if has_feedback:
            participant_state["clientSeq"] = client_seq
            participant_state["serverUpdatedAtMs"] = server_updated_at_ms
        participant_states.append(participant_state)
    return participant_states


def _build_strict_broadcast_status_payload(broadcast):
    return {
        "broadcast": _build_strict_broadcast_snapshot(broadcast),
        "participantStates": _build_strict_broadcast_participant_states(
            broadcast
        ),
    }


def _get_broadcast_from_payload(current_user_name, payload):
    broadcast_id = payload.get("broadcastId")
    playback_context_id = _get_broadcast_playback_context_id(payload)
    if broadcast_id is not None:
        if not isinstance(broadcast_id, str) or not broadcast_id:
            raise ValueError("broadcastId must be a non-empty string")
        broadcast = state.get_broadcast(broadcast_id)
        if broadcast is None and playback_context_id is not None:
            broadcast = _restore_broadcast_from_playback_context(
                current_user_name,
                playback_context_id,
            )
    elif playback_context_id is not None:
        broadcast = state.get_broadcast_by_playback_context(playback_context_id)
        if broadcast is None:
            broadcast = _restore_broadcast_from_playback_context(
                current_user_name,
                playback_context_id,
            )
    else:
        raise ValueError("broadcastId must be a non-empty string")
    if broadcast is None:
        raise LookupError("Broadcast not found")
    if broadcast.get("userName") != current_user_name:
        raise PermissionError("Cross-user broadcast access is not allowed")
    if broadcast_id is not None and broadcast.get("broadcastId") != broadcast_id:
        raise ValueError("broadcastId does not match playbackContextId")
    if playback_context_id is not None:
        broadcast_playback_context_id = broadcast.get("playbackContextId")
        if broadcast_playback_context_id != playback_context_id:
            raise ValueError("playbackContextId does not match broadcast")
    return broadcast


def _get_broadcast_playback_context_id(payload, strict_v2=False):
    _reject_session_id_for_strict_v2(payload, strict_v2)
    playback_context_id = payload.get("playbackContextId")
    if playback_context_id is None:
        return None
    if not isinstance(playback_context_id, str) or not playback_context_id:
        raise ValueError("playbackContextId must be a non-empty string")
    return playback_context_id


def _sync_broadcast_playback_context(broadcast):
    playback_context_id = broadcast.get("playbackContextId")
    if not playback_context_id:
        return None
    existing_context = state.get_playback_context(playback_context_id)
    if existing_context is None:
        persisted_context = getPlaybackContextState(playback_context_id)
        if persisted_context is not None:
            existing_context = state.restore_playback_context(
                playback_context_id,
                persisted_context,
            )
    if existing_context is not None and not (
        existing_context.get("userName") == broadcast.get("userName")
        and existing_context.get("contextType") == "broadcast"
        and existing_context.get("broadcastId") == broadcast.get("broadcastId")
    ):
        if existing_context.get("userName") != broadcast.get("userName"):
            raise PermissionError("Playback context belongs to another user")
        raise BroadcastConflictError("Playback context is already in use")
    try:
        playback_context = state.upsert_broadcast_playback_context(
            playback_context_id,
            broadcast.get("broadcastId"),
            broadcast.get("userName"),
            broadcast.get("authorityClientId") or "server",
            broadcast.get("originClientId") or broadcast.get("updatedByClientId"),
            broadcast.get("participants") or [],
            broadcast.get("queueSongIds") or [],
            owner_client_id=broadcast.get("ownerClientId"),
            control_policy=broadcast.get("controlPolicy"),
            follow_delay_ms=broadcast.get("followDelayMs"),
            current_index=broadcast.get("currentIndex", 0),
            position_ms=broadcast.get("positionMs", 0),
            state_name=broadcast.get("state") or "stopped",
            queue_revision=broadcast.get("queueRevision"),
            control_version=broadcast.get(
                "controlVersion",
                broadcast.get("version"),
            ),
            version=broadcast.get("version"),
            epoch=broadcast.get("epoch"),
            timeline_id=broadcast.get("timelineId"),
        )
    except PlaybackContextConflictError:
        raise BroadcastConflictError("Playback context is already in use")
    if playback_context is not None:
        _save_playback_context_snapshot(broadcast.get("userName"), playback_context)
    return playback_context


def _restore_broadcast_from_playback_context(user_name, playback_context_id):
    playback_context = _get_existing_playback_context(playback_context_id)
    if playback_context is None or playback_context.get("contextType") != "broadcast":
        return None
    _ensure_playback_context_for_user(playback_context, user_name)
    try:
        broadcast = state.restore_broadcast_playback_context(playback_context)
    except PlaybackContextConflictError:
        raise BroadcastConflictError("Playback context is already in use")
    if broadcast is None:
        return None

    participant_ids = set(broadcast.get("participants") or [])
    for device_state in getDevicePlaybackStates(playback_context_id):
        client_id = (
            device_state.get("clientId")
            or device_state.get("sourceClientId")
            or device_state.get("ownerClientId")
        )
        if client_id not in participant_ids:
            continue
        state.update_broadcast_participant_state(
            broadcast.get("broadcastId"),
            client_id,
            device_state.get("deviceSessionId") or device_state.get("sessionId"),
            device_state,
            online=state.get_sid_for_client(client_id, user_name=user_name) is not None,
        )
    return broadcast


def _ensure_broadcast_playback_context_available(user_name, playback_context_id):
    if not playback_context_id:
        return
    existing_context = _get_existing_playback_context(playback_context_id)
    if existing_context is None:
        return
    _ensure_playback_context_for_user(existing_context, user_name)
    raise BroadcastConflictError("Playback context is already in use")


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


def _ensure_broadcast_base_control_version_current(broadcast, expected_version):
    if expected_version is None:
        return
    current_control_version = broadcast.get(
        "controlVersion",
        broadcast.get("version"),
    )
    if current_control_version != expected_version:
        raise BroadcastConflictError(
            "Broadcast control version conflict",
            current_version=broadcast.get("version"),
            current_control_version=current_control_version,
        )


def _get_optional_source_base_control_version(payload):
    if "baseControlVersion" not in payload:
        return None
    base_version = payload.get("baseControlVersion")
    if not _is_int(base_version):
        raise ValueError("baseControlVersion must be an integer")
    return base_version


def _get_base_queue_revision(payload):
    if "baseQueueRevision" not in payload:
        return None
    base_revision = payload.get("baseQueueRevision")
    if not _is_int(base_revision):
        raise ValueError("baseQueueRevision must be an integer")
    return base_revision


def _update_active_broadcast_state(
    broadcast_id,
    updated_by_client_id,
    supersede_pending=True,
    **kwargs,
):
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
    if supersede_pending:
        _supersede_prepares_for_timeline(
            updated.get("timelineId") or f"broadcast:{broadcast_id}"
        )
    _sync_broadcast_playback_context(updated)
    return updated


def _has_role(client, role):
    return role in (client.get("roles") or [])


def _handle_device_set_volume(
    current_user_name,
    current_client,
    payload,
    request_id,
):
    if current_client is None or not _has_role(current_client, "controller"):
        raise PermissionError("Device volume control requires the controller role")
    if not _client_supports(
        current_client,
        CAPABILITY_REMOTE_VOLUME_CONTROL,
    ):
        raise CapabilityRequiredError(
            "Controller did not negotiate remoteVolumeControl"
        )

    target_client_id = payload["targetClientId"]
    target_device_session_id = payload["targetDeviceSessionId"]
    target_client = state.get_client(
        target_client_id,
        user_name=current_user_name,
    )
    target_sid = state.get_sid_for_client(
        target_client_id,
        user_name=current_user_name,
    )
    if target_client is None or target_sid is None:
        raise LookupError("Target device is not online")
    if target_client.get("deviceSessionId") != target_device_session_id:
        raise LookupError("Target device session is not online")
    if not _has_role(target_client, "player"):
        raise PermissionError("Target device is not a player")
    if not _client_supports(
        target_client,
        CAPABILITY_REMOTE_VOLUME_CONTROL,
    ):
        raise CapabilityRequiredError(
            "Target device did not negotiate remoteVolumeControl"
        )
    if not _client_supports(target_client, CAPABILITY_CAN_SET_VOLUME):
        raise CapabilityRequiredError("Target device cannot set volume")
    if not socketio.server.manager.is_connected(target_sid, namespace="/emo"):
        raise LookupError("Target device socket is not online")
    if not strict_v2_safety.reserve_emit(target_sid):
        raise LookupError("Target device send buffer is unavailable")
    try:
        _emit_message(
            _build_message(
                "command",
                "device.setVolume",
                {
                    "sourceClientId": current_client.get("clientId"),
                    "volume": payload["volume"],
                },
            ),
            target_sid,
            emit_reserved=True,
        )
    finally:
        strict_v2_safety.release_emit(target_sid)
    _send_ack(request_id)


def _build_device_volume_update_message(volume_state):
    if not isinstance(volume_state, dict):
        return None
    return _build_message(
        "event",
        "device.volume.update",
        {
            "sourceClientId": volume_state.get("sourceClientId"),
            "deviceSessionId": volume_state.get("deviceSessionId"),
            "volume": volume_state.get("volume"),
            "clientSeq": volume_state.get("clientSeq"),
            "serverUpdatedAtMs": volume_state.get("serverUpdatedAtMs"),
        },
    )


def _broadcast_device_volume_update(user_name, source_sid, volume_state):
    message = _build_device_volume_update_message(volume_state)
    if message is None:
        return None
    target_sids = {source_sid}
    for target_sid, target_client in state.list_sids(user_name=user_name):
        if (
            _has_role(target_client, "controller")
            and _client_supports(
                target_client,
                CAPABILITY_REMOTE_VOLUME_CONTROL,
            )
        ):
            target_sids.add(target_sid)
    for target_sid in sorted(target_sids):
        _emit_message(message, target_sid)
    return message


def _handle_device_volume_update(
    current_user_name,
    current_client,
    payload,
    request_id,
):
    if current_client is None or not _has_role(current_client, "player"):
        raise PermissionError("Device volume feedback requires the player role")
    if not _client_supports(
        current_client,
        CAPABILITY_REMOTE_VOLUME_CONTROL,
    ) or not _client_supports(current_client, CAPABILITY_CAN_SET_VOLUME):
        raise CapabilityRequiredError(
            "Device did not negotiate remote volume feedback"
        )
    if payload["deviceSessionId"] != current_client.get("deviceSessionId"):
        raise PermissionError("deviceSessionId does not match the registered device")
    session_info = state.get_session(request.sid) or {}
    connection_nonce = session_info.get("connectionNonce")
    if not isinstance(connection_nonce, str) or not connection_nonce:
        raise PermissionError("Registered connection provenance is missing")

    volume_state, created = state.record_strict_device_volume_state(
        current_user_name,
        current_client.get("clientId"),
        current_client.get("deviceSessionId"),
        payload,
        connection_nonce,
    )
    update_message = _build_device_volume_update_message(volume_state)
    _store_event_confirmations([update_message])
    if created:
        _run_post_commit_push(
            "device.volume.update",
            request_id,
            lambda: _broadcast_device_volume_update(
                current_user_name,
                request.sid,
                volume_state,
            ),
        )
    else:
        _run_post_commit_push(
            "device.volume.update",
            request_id,
            lambda: _emit_message(update_message, request.sid),
        )
    return volume_state


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

        client = state.get_client(client_id, user_name=current_user_name)
        if client is None:
            if state.get_client(client_id) is not None:
                raise PermissionError("Cross-user broadcast target is not allowed")
            skipped_client_ids.append(client_id)
            return
        if client.get("userName") != current_user_name:
            raise PermissionError("Cross-user broadcast target is not allowed")
        if not _has_role(client, "player"):
            skipped_client_ids.append(client_id)
            return
        if (
            _is_strict_playback_context_v2(client)
            and not _client_supports(client, CAPABILITY_SUPPORTS_BROADCAST)
        ):
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


def _strict_broadcast_participant_eligible(client):
    return (
        client is not None
        and _has_role(client, "player")
        and all(
            _client_supports(client, capability)
            for capability in STRICT_BROADCAST_PARTICIPANT_CAPABILITIES
        )
    )


def _get_strict_broadcast_context(current_user_name, playback_context_id):
    context = _get_existing_playback_context(playback_context_id)
    if context is None:
        raise LookupError("Playback context not found")
    _ensure_playback_context_for_user(context, current_user_name)
    _ensure_playback_context_active(context)
    return context


def _get_online_strict_broadcast_authority(current_user_name, context):
    authority_client_id = context.get("authorityClientId")
    authority = state.get_client(
        authority_client_id,
        user_name=current_user_name,
    )
    authority_sid = state.get_sid_for_client(
        authority_client_id,
        user_name=current_user_name,
    )
    if authority is None or authority_sid is None:
        raise PlaybackAuthorityOfflineError("Playback context authority is offline")
    if not socketio.server.manager.is_connected(authority_sid, namespace="/emo"):
        raise PlaybackAuthorityOfflineError(
            "Playback context authority socket is unavailable"
        )
    if authority.get("deviceSessionId") != context.get("authorityDeviceSessionId"):
        raise PlaybackAuthorityOfflineError(
            "Playback context authority device is not connected"
        )
    if not _strict_broadcast_participant_eligible(authority):
        raise CapabilityRequiredError(
            "Playback context authority lacks Broadcast playback capabilities"
        )
    return authority


def _resolve_strict_broadcast_start_participants(
    current_user_name,
    context,
    payload,
):
    authority = _get_online_strict_broadcast_authority(
        current_user_name,
        context,
    )
    authority_client_id = authority["clientId"]
    requested_participants = payload.get("participants")
    participants = {authority_client_id}
    skipped_client_ids = set()

    if requested_participants is None:
        for client in _list_clients(user_name=current_user_name):
            client_id = client.get("clientId")
            if not client_id or not _strict_broadcast_participant_eligible(client):
                continue
            if _strict_broadcast_client_online(current_user_name, client_id):
                participants.add(client_id)
        return sorted(participants), []

    for client_id in requested_participants:
        client = state.get_client(client_id, user_name=current_user_name)
        if client is None:
            other_user_client = state.get_client(client_id)
            if other_user_client is not None:
                raise PermissionError("Cross-user broadcast target is not allowed")
            skipped_client_ids.add(client_id)
            continue
        if client.get("userName") != current_user_name:
            raise PermissionError("Cross-user broadcast target is not allowed")
        if not _strict_broadcast_participant_eligible(client):
            skipped_client_ids.add(client_id)
            continue
        if not _strict_broadcast_client_online(current_user_name, client_id):
            skipped_client_ids.add(client_id)
            continue
        participants.add(client_id)

    return sorted(participants), sorted(skipped_client_ids - participants)


def _get_strict_broadcast_from_payload(current_user_name, payload):
    broadcast = state.get_broadcast(payload.get("broadcastId"))
    if broadcast is None:
        raise LookupError("Broadcast not found")
    if broadcast.get("userName") != current_user_name:
        raise PermissionError("Cross-user broadcast access is not allowed")
    if broadcast.get("playbackContextId") != payload.get("playbackContextId"):
        raise ValueError("playbackContextId does not match broadcast")
    return broadcast


def _emit_strict_broadcast_to_participants(broadcast, action, msg_type):
    payload = _build_strict_broadcast_snapshot(broadcast)
    if action in {
        "broadcast.play",
        "broadcast.pause",
        "broadcast.seek",
        "broadcast.playItem",
    }:
        payload["effectiveAtServerMs"] = broadcast["effectiveAtServerMs"]
        payload["serverTimeMs"] = _server_time_ms()
    message = _build_message(
        msg_type,
        action,
        payload,
    )
    for client_id in sorted(set(broadcast.get("participants") or [])):
        sid = state.get_sid_for_client(
            client_id,
            user_name=broadcast.get("userName"),
        )
        if sid is None:
            continue
        _emit_message(message, sid)


def _expire_strict_broadcast_authority_disconnect(
    broadcast_id,
    deadline_ms,
    now=None,
):
    stopped = state.stop_broadcast_if_authority_deadline(
        broadcast_id,
        deadline_ms,
        now=now,
    )
    if stopped is None:
        return None
    _run_post_commit_push(
        "broadcast.stop",
        None,
        lambda: _emit_strict_broadcast_to_participants(
            stopped,
            "broadcast.stop",
            "command",
        ),
    )
    return stopped


def _expire_strict_broadcast_authority_disconnect_later(
    broadcast_id,
    deadline_ms,
):
    delay_seconds = max(0, deadline_ms - _server_time_ms()) / 1000
    socketio.sleep(delay_seconds)
    _expire_strict_broadcast_authority_disconnect(
        broadcast_id,
        deadline_ms,
    )


def _suspend_strict_broadcasts_for_authority_disconnect(client_info):
    client_id = client_info.get("clientId")
    user_name = client_info.get("userName")
    device_session_id = client_info.get("deviceSessionId")
    for broadcast in state.list_broadcasts(user_name=user_name):
        if broadcast.get("authorityClientId") != client_id:
            continue
        if broadcast.get("authorityClientId") == "server":
            continue
        deadline_ms = _server_time_ms() + BROADCAST_AUTHORITY_DISCONNECT_TIMEOUT_MS
        suspended = state.suspend_broadcast_for_authority_disconnect(
            broadcast["broadcastId"],
            client_id,
            device_session_id,
            deadline_ms,
        )
        if suspended is None:
            continue
        suspended["effectiveAtServerMs"] = _effective_at_server_ms(
            PROTOCOL_SINGLE_FUTURE
        )
        _run_post_commit_push(
            "broadcast.pause",
            None,
            lambda: _emit_strict_broadcast_to_participants(
                suspended,
                "broadcast.pause",
                "command",
            ),
        )
        _run_post_commit_push(
            "broadcast.stop",
            None,
            lambda: socketio.start_background_task(
                _expire_strict_broadcast_authority_disconnect_later,
                broadcast["broadcastId"],
                deadline_ms,
            ),
        )


def _resume_strict_broadcast_authority_registration(client_info):
    if not _strict_broadcast_participant_eligible(client_info):
        return []
    client_id = client_info.get("clientId")
    user_name = client_info.get("userName")
    device_session_id = client_info.get("deviceSessionId")
    resumed = []
    for broadcast in state.list_broadcasts(user_name=user_name):
        if broadcast.get("authorityClientId") != client_id:
            continue
        cleared = state.clear_broadcast_authority_disconnect(
            broadcast["broadcastId"],
            client_id,
            device_session_id,
        )
        if cleared is not None:
            resumed.append(cleared)
    return resumed


def _broadcast_to_participants(broadcast, action, msg_type, source_client_id, request_id=None, extra_payload=None):
    payload = _build_broadcast_core_payload(broadcast, extra_payload)
    for target_client_id in broadcast.get("participants") or []:
        target_client = state.get_client(
            target_client_id,
            user_name=broadcast.get("userName"),
        )
        if (
            _is_strict_playback_context_v2(target_client)
            and not _client_supports(target_client, CAPABILITY_SUPPORTS_BROADCAST)
        ):
            continue
        target_sid = state.get_sid_for_client(
            target_client_id,
            user_name=broadcast.get("userName"),
        )
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
        _emit_message(message, target_sid)


def _broadcast_status_to_requester(broadcast):
    _emit_message(
        _build_message(
            "state",
            "broadcast.status",
            _build_broadcast_status_payload(broadcast),
        )
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


def _required_broadcast_ready_clients(owner_client_id, participant_ids):
    if owner_client_id in participant_ids:
        return [owner_client_id]
    return list(participant_ids)


def _send_playback_prepare(prepare, payload):
    user_name = (prepare.get("commitPayload") or {}).get("userName")
    for target_client_id in prepare.get("targetClientIds") or []:
        target_client = state.get_client(target_client_id, user_name=user_name)
        target_sid = state.get_sid_for_client(target_client_id, user_name=user_name)
        if target_client is None or target_sid is None:
            continue
        target_payload = dict(payload)
        is_context_prepare = _is_context_payload(target_payload)
        target_payload["deviceSessionId"] = _device_session_id(target_client)
        if not is_context_prepare or not _is_strict_playback_context_v2(target_client):
            target_payload["sessionId"] = _device_session_id(target_client)
        elif _is_strict_playback_context_v2(target_client):
            target_payload.pop("targetClientId", None)
        if _is_strict_playback_context_v2(target_client):
            message = _build_message(
                "command",
                "playback.prepare",
                target_payload,
            )
        else:
            message = _build_message(
                "command",
                "playback.prepare",
                target_payload,
                requestId=f"{prepare['prepareId']}-{target_client_id}",
                targetClientId=target_client_id,
            )
        _emit_message(message, target_sid)


def _send_target_player_play(
    target_client_id,
    source_client_id,
    request_id,
    session_id,
    effective_at_server_ms,
    control_version,
    extra_payload=None,
    user_name=None,
):
    target_sid = state.get_sid_for_client(target_client_id, user_name=user_name)
    if target_sid is None:
        return
    target_client = state.get_client(target_client_id, user_name=user_name)
    payload = {
        "effectiveAtServerMs": effective_at_server_ms,
        "controlVersion": control_version,
    }
    if not _is_strict_playback_context_v2(target_client):
        payload["sessionId"] = session_id
    payload.update(extra_payload or {})
    _emit_message(
        _build_message(
            "command",
            "player.play",
            payload,
            requestId=request_id,
            sourceClientId=source_client_id,
            targetClientId=target_client_id,
        ),
        target_sid,
    )


def _send_strict_handoff_commit(
    target_client_id: str,
    user_name: str,
    playback_context_id: str,
    handoff_id: str,
    source_client_id: str,
    control_version: int,
    effective_at_server_ms: int,
    position_ms: int,
) -> bool:
    target_sid = state.get_sid_for_client(
        target_client_id,
        user_name=user_name,
    )
    if target_sid is None:
        return False
    _emit_message(
        _build_message(
            "command",
            "player.play",
            {
                "playbackContextId": playback_context_id,
                "handoffId": handoff_id,
                "controlVersion": control_version,
                "sourceClientId": source_client_id,
                "effectiveAtServerMs": effective_at_server_ms,
                "positionMs": position_ms,
            },
        ),
        target_sid,
    )
    return True


def _prepare_ready_to_commit(prepare, now_ms=None):
    now_ms = _server_time_ms() if now_ms is None else now_ms
    ready = set(prepare.get("readyClientIds") or set())
    failed = set(prepare.get("failedClientIds") or set())
    targets = set(prepare.get("targetClientIds") or [])
    required = set(prepare.get("requiredClientIds") or [])
    if required & failed:
        return False
    if targets and targets <= ready:
        return True
    return bool(required and required <= ready and now_ms >= prepare.get("expiresAtMs", 0))


def _create_broadcast_prepare(
    action,
    current_user_name,
    current_client,
    participant_ids,
    request_id,
    request_sid,
    commit_payload,
):
    prepare_id = _new_prepare_id()
    now_ms = _server_time_ms()
    try:
        prepare = state.create_prepare(
            prepare_id,
            action,
            commit_payload["timelineId"],
            participant_ids,
            _required_broadcast_ready_clients(
                commit_payload["ownerClientId"],
                participant_ids,
            ),
            commit_payload["controlVersion"],
            commit_payload,
            now_ms,
            now_ms + PREPARE_TIMEOUT_MS,
            request_sid=request_sid,
            request_id=request_id,
        )
    except PlaybackContextConflictError:
        raise BroadcastConflictError("Playback context is already in use")
    prepare_payload = {
        "prepareId": prepare_id,
        "broadcastId": commit_payload["broadcastId"],
        "ownerClientId": commit_payload["ownerClientId"],
        "timelineId": commit_payload["timelineId"],
        "sourceClientId": current_client.get("clientId"),
        "queueSongIds": list(commit_payload.get("queueSongIds") or []),
        "currentIndex": commit_payload.get("currentIndex", 0),
        "trackId": commit_payload.get("trackId"),
        "positionMs": commit_payload.get("positionMs", 0),
        "controlVersion": commit_payload["controlVersion"],
        "state": "playing",
    }
    if commit_payload.get("playbackContextId") is not None:
        prepare_payload["playbackContextId"] = commit_payload.get("playbackContextId")
        prepare_payload["contextType"] = "broadcast"
    _send_playback_prepare(prepare, prepare_payload)
    socketio.start_background_task(_expire_prepare_later, prepare_id)
    _send_ack(
        request_id,
        {
            "preparing": True,
            "prepareId": prepare_id,
            "broadcastId": commit_payload["broadcastId"],
            "participants": list(participant_ids),
            "protocolPath": PROTOCOL_TWO_PHASE,
        },
    )
    _log_emo_event(
        logging.INFO,
        "playback_prepare",
        result="created",
        user=current_user_name,
        client_request_id=request_id,
        source_client_id=current_client.get("clientId"),
        broadcast_id=commit_payload["broadcastId"],
        prepare_id=prepare_id,
        protocol_path=PROTOCOL_TWO_PHASE,
    )
    return prepare


def _expire_prepare_later(prepare_id):
    prepare = state.get_prepare(prepare_id)
    if prepare is None:
        return
    delay_ms = max(0, prepare.get("expiresAtMs", 0) - _server_time_ms())
    socketio.sleep(delay_ms / 1000)
    _expire_prepare(prepare_id)


def _expire_prepare(prepare_id):
    prepare = state.get_prepare(prepare_id)
    if prepare is None or prepare.get("status") != "preparing":
        return None
    now_ms = _server_time_ms()
    if now_ms < prepare.get("expiresAtMs", 0):
        return None
    if _prepare_ready_to_commit(prepare, now_ms=now_ms):
        return _commit_prepare(prepare)
    commit_payload = prepare.get("commitPayload") or {}
    handoff_id = commit_payload.get("handoffId")
    if handoff_id:
        terminal_result = terminateStrictPlaybackHandoff(
            commit_payload.get("playbackContextId"),
            handoff_id,
            commit_payload.get("userName"),
            "timed_out",
            error_code="prepare_timeout",
            error_message="Handoff prepare timed out",
        )
        if terminal_result is None:
            return None
        handoff, transitioned = terminal_result
        state.update_playback_handoff(
            handoff_id,
            status=handoff.get("status"),
            error_code=handoff.get("errorCode"),
            error_message=handoff.get("errorMessage"),
        )
        if not transitioned:
            return None
    timed_out = state.finish_prepare_if_preparing(prepare_id, "timed_out")
    if timed_out is None:
        return None
    if handoff_id:
        target_client = state.get_client(
            commit_payload.get("targetClientId"),
            user_name=commit_payload.get("userName"),
        )
        if _is_strict_playback_context_v2(target_client):
            _run_post_commit_push(
                "playback.handoff.start",
                prepare.get("requestId"),
                lambda: _broadcast_handoff_cancel(
                    handoff,
                    "prepare_timeout",
                ),
            )
            _run_post_commit_push(
                "playback.handoff.start",
                prepare.get("requestId"),
                lambda: _broadcast_handoff_status(
                    handoff,
                    include_sid=state.get_sid_for_client(
                        commit_payload.get("targetClientId"),
                        user_name=commit_payload.get("userName"),
                    ),
                ),
            )
        else:
            _send_handoff_release(
                handoff,
                handoff.get("targetClientId"),
                "timed_out",
                request_id=prepare.get("requestId"),
                authority_client_id=handoff.get("sourceClientId"),
                source_client_id=handoff.get("sourceClientId"),
            )
    else:
        _update_handoff_for_prepare(
            timed_out,
            "timed_out",
            error_code="prepare_timeout",
            error_message="Handoff prepare timed out",
        )
    _log_emo_event(
        logging.INFO,
        "playback_prepare",
        result="timed_out",
        action=prepare.get("action"),
        prepare_id=prepare_id,
        timeline_id=prepare.get("timelineId"),
        source_client_id=commit_payload.get("sourceClientId"),
        target_client_id=commit_payload.get("targetClientId"),
        broadcast_id=commit_payload.get("broadcastId"),
    )
    return timed_out


def _update_handoff_for_prepare(
    prepare,
    status,
    error_code=None,
    error_message=None,
    post_commit_action=None,
    post_commit_request_id=None,
):
    commit_payload = prepare.get("commitPayload") or {}
    handoff_id = commit_payload.get("handoffId")
    if not handoff_id:
        return None

    handoff = state.update_playback_handoff(
        handoff_id,
        status=status,
        error_code=error_code,
        error_message=error_message,
    )
    if handoff is None:
        handoff = getPlaybackHandoff(handoff_id)
        if handoff is None:
            return None
        handoff = dict(handoff)
        handoff["status"] = status
        if error_code is not None:
            handoff["errorCode"] = error_code
        if error_message is not None:
            handoff["errorMessage"] = error_message
    savePlaybackHandoff(handoff)
    target_client = state.get_client(
        handoff.get("targetClientId") or commit_payload.get("targetClientId"),
        user_name=handoff.get("userName"),
    )
    if _is_strict_playback_context_v2(target_client) and status in {
        "failed",
        "timed_out",
    }:
        if post_commit_action == "playback.ready":
            _store_event_confirmations(
                [_build_handoff_status_message(handoff)]
            )
        push = lambda: _broadcast_handoff_cancel(
            handoff,
            error_code or status,
            include_sid=prepare.get("requestSid"),
        )
        if post_commit_action is None:
            push()
        else:
            _run_post_commit_push(
                post_commit_action,
                post_commit_request_id,
                push,
            )
        return handoff
    release_reason = None
    if status == "timed_out":
        release_reason = "timed_out"
    elif status in ("aborted", "superseded"):
        release_reason = "aborted"
    if release_reason is not None:
        _send_handoff_release(
            handoff,
            handoff.get("targetClientId") or commit_payload.get("targetClientId"),
            release_reason,
            request_id=prepare.get("requestId"),
            authority_client_id=handoff.get("sourceClientId"),
            source_client_id=handoff.get("sourceClientId"),
        )
    return handoff


def _supersede_prepares_for_timeline(timeline_id):
    superseded_prepares = state.supersede_prepares_for_timeline(timeline_id)
    for prepare in superseded_prepares or []:
        _update_handoff_for_prepare(
            prepare,
            "superseded",
            error_code="prepare_superseded",
            error_message="Handoff prepare was superseded",
        )
    return superseded_prepares


def _expire_handoff_complete_later(handoff_id):
    handoff = state.get_playback_handoff(handoff_id)
    if handoff is None:
        return
    expires_at_ms = handoff.get("completeExpiresAtMs")
    if expires_at_ms is None:
        return
    delay_ms = max(0, expires_at_ms - _server_time_ms())
    socketio.sleep(delay_ms / 1000)
    _expire_handoff_complete(handoff_id)


def _expire_handoff_complete(handoff_id):
    handoff = state.get_playback_handoff(handoff_id) or getPlaybackHandoff(
        handoff_id
    )
    target_client = None if handoff is None else state.get_client(
        handoff.get("targetClientId"),
        user_name=handoff.get("userName"),
    )
    if (
        handoff is not None
        and handoff.get("status") == "ready"
        and not _is_strict_playback_context_v2(target_client)
    ):
        expires_at_ms = handoff.get("completeExpiresAtMs")
        if expires_at_ms is None or _server_time_ms() < expires_at_ms:
            return None
        expired = state.expire_playback_handoff_if_status(
            handoff_id,
            ("ready",),
            "timed_out",
            error_code="complete_timeout",
            error_message="Handoff complete timed out",
            now=_server_time_ms() / 1000,
        )
        if expired is not None:
            savePlaybackHandoff(expired)
            _send_handoff_release(
                expired,
                expired.get("targetClientId"),
                "timed_out",
                request_id=expired.get("requestId"),
                authority_client_id=expired.get("sourceClientId"),
                source_client_id=expired.get("sourceClientId"),
            )
        return expired
    if handoff is None or handoff.get("status") not in (
        "committed",
        "committing",
    ):
        return None
    expires_at_ms = handoff.get("completeExpiresAtMs")
    if expires_at_ms is None or _server_time_ms() < expires_at_ms:
        return None

    terminal_result = terminateStrictPlaybackHandoff(
        handoff.get("playbackContextId"),
        handoff_id,
        handoff.get("userName"),
        "timed_out",
        error_code="commit_timeout",
        error_message="Handoff commit timed out",
    )
    if terminal_result is None:
        return None
    expired, transitioned = terminal_result
    state.update_playback_handoff(
        handoff_id,
        status=expired.get("status"),
        error_code=expired.get("errorCode"),
        error_message=expired.get("errorMessage"),
    )
    if not transitioned:
        return None

    target_client = state.get_client(
        expired.get("targetClientId"),
        user_name=expired.get("userName"),
    )
    if _is_strict_playback_context_v2(target_client):
        _run_post_commit_push(
            "playback.handoff.complete",
            expired.get("requestId"),
            lambda: _broadcast_handoff_cancel(
                expired,
                "commit_timeout",
            ),
        )
        _run_post_commit_push(
            "playback.handoff.complete",
            expired.get("requestId"),
            lambda: _broadcast_handoff_status(
                expired,
                include_sid=state.get_sid_for_client(
                    expired.get("targetClientId"),
                    user_name=expired.get("userName"),
                ),
            ),
        )
    else:
        _send_handoff_release(
            expired,
            expired.get("targetClientId"),
            "timed_out",
            request_id=expired.get("requestId"),
            authority_client_id=expired.get("sourceClientId"),
            source_client_id=expired.get("sourceClientId"),
        )
    return expired


def _commit_prepare(prepare, post_commit_request_id=None):
    if prepare.get("status") != "preparing":
        return None
    if not _prepare_ready_to_commit(prepare):
        return None
    commit_payload = prepare.get("commitPayload") or {}
    action = prepare.get("action")
    if action not in (
        "broadcast.start",
        "broadcast.playItem",
        "broadcast.play",
        "player.play",
        "queue.playItem",
        "playback.handoff.start",
    ):
        return None

    handoff_target = (
        state.get_client(
            commit_payload.get("targetClientId"),
            user_name=commit_payload.get("userName"),
        )
        if action == "playback.handoff.start"
        else None
    )
    if action == "playback.handoff.start" and _is_strict_playback_context_v2(
        handoff_target
    ):
        effective_at_server_ms = _effective_at_server_ms(PROTOCOL_TWO_PHASE)
        complete_expires_at_ms = (
            effective_at_server_ms + HANDOFF_COMPLETE_TIMEOUT_MS
        )
        terminal_result = commitStrictPlaybackHandoff(
            commit_payload.get("playbackContextId"),
            commit_payload.get("handoffId"),
            commit_payload.get("userName"),
            complete_expires_at_ms,
        )
        if terminal_result is None:
            return None
        handoff, transitioned = terminal_result
        if not transitioned:
            return None
        claimed_prepare = state.finish_prepare_if_preparing(
            prepare["prepareId"],
            "committed",
        )
        if claimed_prepare is None:
            return None
        target_client = state.get_client(
            commit_payload["targetClientId"],
            user_name=commit_payload.get("userName"),
        )
        target_sid = state.get_sid_for_client(
            commit_payload["targetClientId"],
            user_name=commit_payload.get("userName"),
        )
        if (
            target_client is None
            or target_sid != commit_payload.get("targetSid")
            or _device_session_id(target_client)
            != commit_payload.get("targetDeviceSessionId")
        ):
            terminateStrictPlaybackHandoff(
                commit_payload.get("playbackContextId"),
                commit_payload.get("handoffId"),
                commit_payload.get("userName"),
                "failed",
                error_code="target_disconnected",
                error_message="Handoff target changed before commit",
            )
            return None
        handoff = state.update_playback_handoff(
            commit_payload["handoffId"],
            status="committed",
            complete_expires_at_ms=complete_expires_at_ms,
        ) or handoff
        if post_commit_request_id is not None:
            _store_event_confirmations(
                [_build_handoff_status_message(handoff)]
            )
        socketio.start_background_task(
            _expire_handoff_complete_later,
            commit_payload["handoffId"],
        )
        if _is_strict_playback_context_v2(target_client):
            _run_post_commit_push(
                "playback.ready",
                post_commit_request_id,
                lambda: _send_strict_handoff_commit(
                    commit_payload["targetClientId"],
                    commit_payload.get("userName"),
                    commit_payload["playbackContextId"],
                    commit_payload["handoffId"],
                    commit_payload.get("sourceClientId"),
                    commit_payload["controlVersion"],
                    effective_at_server_ms,
                    commit_payload.get("positionMs", 0),
                ),
            )
        else:
            _send_target_player_play(
                commit_payload["targetClientId"],
                commit_payload.get("sourceClientId"),
                prepare.get("requestId"),
                _device_session_id(target_client),
                effective_at_server_ms,
                commit_payload["controlVersion"],
                extra_payload={
                    "playbackContextId": commit_payload["playbackContextId"],
                    "deviceSessionId": _device_session_id(target_client),
                    "handoffId": commit_payload["handoffId"],
                    "trackId": commit_payload.get("trackId"),
                    "positionMs": commit_payload.get("positionMs", 0),
                    "state": "playing",
                    "completeExpiresAtServerMs": complete_expires_at_ms,
                },
                user_name=commit_payload.get("userName"),
            )
        return handoff

    claimed_prepare = state.finish_prepare_if_preparing(
        prepare["prepareId"],
        "committed",
    )
    if claimed_prepare is None:
        return None
    prepare = claimed_prepare
    commit_payload = prepare.get("commitPayload") or {}
    effective_at_server_ms = _effective_at_server_ms(PROTOCOL_TWO_PHASE)

    if action == "broadcast.start":
        broadcast = state.create_broadcast(
            commit_payload["broadcastId"],
            commit_payload["userName"],
            commit_payload["ownerClientId"],
            commit_payload["participants"],
            commit_payload["queueSongIds"],
            commit_payload.get("currentIndex", 0),
            commit_payload.get("positionMs", 0),
            commit_payload.get("stateName", "playing"),
            commit_payload.get("controlPolicy"),
            commit_payload.get("updatedByClientId"),
            effective_at_server_ms=effective_at_server_ms,
            playback_context_id=commit_payload.get("playbackContextId"),
        )
        _sync_broadcast_playback_context(broadcast)
        for participant_id in broadcast.get("participants") or []:
            participant = state.get_client(
                participant_id,
                user_name=commit_payload.get("userName"),
            )
            if participant is None:
                continue
            state.update_broadcast_participant_state(
                broadcast["broadcastId"],
                participant_id,
                participant.get("sessionId"),
                {
                    "state": broadcast.get("state"),
                    "trackId": broadcast.get("trackId"),
                    "positionMs": broadcast.get("positionMs", 0),
                },
                online=True,
            )
        _broadcast_to_participants(
            broadcast,
            "broadcast.start",
            "command",
            commit_payload.get("sourceClientId"),
            request_id=prepare.get("requestId"),
            extra_payload={
                "autoPlay": commit_payload.get("autoPlay", True),
                "protocolPath": PROTOCOL_TWO_PHASE,
            },
        )
    elif action == "broadcast.playItem":
        broadcast = _update_active_broadcast_state(
            commit_payload["broadcastId"],
            commit_payload["updatedByClientId"],
            current_index=commit_payload["queueIndex"],
            position_ms=commit_payload.get("positionMs", 0),
            state_name="playing",
            expected_version=commit_payload.get("expectedVersion"),
            effective_at_server_ms=effective_at_server_ms,
            supersede_pending=False,
        )
        _broadcast_to_participants(
            broadcast,
            "broadcast.playItem",
            "command",
            commit_payload.get("sourceClientId"),
            request_id=prepare.get("requestId"),
            extra_payload={
                "queueIndex": commit_payload["queueIndex"],
                "protocolPath": PROTOCOL_TWO_PHASE,
            },
        )
    elif action == "broadcast.play":
        broadcast = _update_active_broadcast_state(
            commit_payload["broadcastId"],
            commit_payload["updatedByClientId"],
            state_name="playing",
            expected_version=commit_payload.get("expectedVersion"),
            effective_at_server_ms=effective_at_server_ms,
            supersede_pending=False,
        )
        _broadcast_to_participants(
            broadcast,
            "broadcast.play",
            "command",
            commit_payload.get("sourceClientId"),
            request_id=prepare.get("requestId"),
            extra_payload={"protocolPath": PROTOCOL_TWO_PHASE},
        )
    elif action in ("player.play", "queue.playItem"):
        playback_state = state.update_playback_control(
            commit_payload["sessionId"],
            commit_payload["targetClientId"],
            state_name="playing",
            track_id=commit_payload.get("trackId"),
            position_ms=commit_payload.get("positionMs", 0),
            queue_song_ids=commit_payload.get("queueSongIds"),
            current_index=commit_payload.get("currentIndex"),
            updated_by_client_id=commit_payload.get("sourceClientId"),
            effective_at_server_ms=effective_at_server_ms,
            control_version=commit_payload.get("controlVersion"),
        )
        _save_playback_state_snapshot(
            commit_payload.get("userName"),
            playback_state,
        )
        _broadcast_playback_state(
            commit_payload["userName"],
            commit_payload["sessionId"],
        )
        _send_target_player_play(
            commit_payload["targetClientId"],
            commit_payload.get("sourceClientId"),
            prepare.get("requestId"),
            commit_payload["sessionId"],
            effective_at_server_ms,
            playback_state["controlVersion"],
            user_name=commit_payload.get("userName"),
        )
        return playback_state
    elif action == "playback.handoff.start":
        target_client = state.get_client(
            commit_payload["targetClientId"],
            user_name=commit_payload.get("userName"),
        )
        target_device_session_id = _device_session_id(target_client)
        complete_expires_at_ms = (
            effective_at_server_ms + HANDOFF_COMPLETE_TIMEOUT_MS
        )
        handoff = state.update_playback_handoff(
            commit_payload["handoffId"],
            status="ready",
            complete_expires_at_ms=complete_expires_at_ms,
        )
        if handoff is not None:
            savePlaybackHandoff(handoff)
            socketio.start_background_task(
                _expire_handoff_complete_later,
                commit_payload["handoffId"],
            )
        _send_target_player_play(
            commit_payload["targetClientId"],
            commit_payload.get("sourceClientId"),
            prepare.get("requestId"),
            target_device_session_id,
            effective_at_server_ms,
            commit_payload["controlVersion"],
            extra_payload={
                "playbackContextId": commit_payload["playbackContextId"],
                "deviceSessionId": target_device_session_id,
                "handoffId": commit_payload["handoffId"],
                "trackId": commit_payload.get("trackId"),
                "positionMs": commit_payload.get("positionMs", 0),
                "state": "playing",
                "completeExpiresAtServerMs": complete_expires_at_ms,
            },
            user_name=commit_payload.get("userName"),
        )
        return handoff
    else:
        return None

    request_sid = prepare.get("requestSid")
    if request_sid:
        _emit_message(
            _build_message(
                "state",
                "broadcast.status",
                _build_broadcast_status_payload(broadcast),
                requestId=prepare.get("requestId"),
            ),
            request_sid,
        )
    return broadcast


def _resolve_control_queue(target_client_id, payload):
    session_id = payload.get("sessionId")
    queue_client_id = payload.get("clientId")
    if queue_client_id:
        queue_state = state.get_local_queue(session_id, queue_client_id)
    else:
        queue_state = state.get_queue(session_id)
    if queue_state is None:
        return None
    return queue_state


def _source_timeline_id(session_id, client_id):
    return f"session:{session_id}:client:{client_id}"


def _current_source_control_version(session_id, target_client_id):
    playback_state = state.get_playback_state(session_id, target_client_id) or {}
    current_control_version = playback_state.get("controlVersion", 0)
    queue_state = state.get_local_queue(session_id, target_client_id) or state.get_queue(session_id)
    if queue_state is not None:
        current_control_version = max(
            current_control_version,
            queue_state.get("controlVersion", 0),
        )
    return current_control_version


def _validate_source_base_control_version(
    session_id,
    target_client_id,
    payload,
    current_control_version=None,
):
    base_control_version = _get_optional_source_base_control_version(payload)
    if base_control_version is None:
        return
    if current_control_version is None:
        current_control_version = _current_source_control_version(
            session_id,
            target_client_id,
        )
    if base_control_version != current_control_version:
        raise ControlConflictError(
            "Playback control version conflict",
            current_control_version=current_control_version,
        )


def _build_source_control_commit_payload(
    current_user_name,
    current_client,
    target_client_id,
    target_client,
    action,
    payload,
):
    session_id = payload.get("sessionId") or target_client.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError(f"{action} requires a non-empty sessionId")

    playback_state = state.get_playback_state(session_id, target_client_id) or {}
    queue_state = None
    queue_song_ids = playback_state.get("queueSongIds") or []
    current_index = playback_state.get("currentIndex", 0)
    track_id = playback_state.get("trackId")
    position_ms = payload.get("positionMs", playback_state.get("positionMs", 0))

    if action in ("queue.playItem", "player.next", "player.prev"):
        queue_state = _resolve_control_queue(target_client_id, payload)
        if queue_state is None and action in ("player.next", "player.prev"):
            queue_state = state.get_local_queue(session_id, target_client_id) or state.get_queue(session_id)
        if queue_state is None:
            return None
        queue_song_ids = list(queue_state.get("queueSongIds") or [])
        if action == "player.next":
            queue_index = queue_state.get("currentIndex", 0) + 1
        elif action == "player.prev":
            queue_index = max(0, queue_state.get("currentIndex", 0) - 1)
        else:
            queue_index = payload.get("queueIndex")
        if queue_index >= len(queue_song_ids):
            raise ValueError(f"{action} queueIndex is out of bounds")
        current_index = queue_index
        track_id = queue_song_ids[current_index]
        position_ms = payload.get("positionMs", 0)
    else:
        queue_state = state.get_local_queue(session_id, target_client_id) or state.get_queue(session_id)
        if queue_state is not None:
            queue_song_ids = list(queue_state.get("queueSongIds") or [])
            current_index = queue_state.get("currentIndex", current_index)
            if not track_id and queue_song_ids and 0 <= current_index < len(queue_song_ids):
                track_id = queue_song_ids[current_index]

    if not track_id:
        return None

    timeline_id = f"session:{session_id}:client:{target_client_id}"
    current_control_version = playback_state.get("controlVersion", 0)
    if queue_state is not None:
        current_control_version = max(
            current_control_version,
            queue_state.get("controlVersion", 0),
        )
    _validate_source_base_control_version(
        session_id,
        target_client_id,
        payload,
        current_control_version=current_control_version,
    )
    control_version = current_control_version + 1
    return {
        "userName": current_user_name,
        "action": "queue.playItem" if action in ("player.next", "player.prev") else action,
        "sessionId": session_id,
        "targetClientId": target_client_id,
        "sourceClientId": current_client.get("clientId"),
        "timelineId": timeline_id,
        "queueSongIds": list(queue_song_ids or []),
        "currentIndex": current_index,
        "trackId": track_id,
        "positionMs": position_ms,
        "controlVersion": control_version,
    }


def _build_seek_media_change_commit_payload(
    current_user_name,
    current_client,
    target_client_id,
    target_client,
    payload,
):
    session_id = payload.get("sessionId") or target_client.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("player.seek requires a non-empty sessionId")

    position_ms = payload.get("positionMs")
    if not _is_non_negative_int(position_ms):
        raise ValueError("player.seek positionMs must be a non-negative integer")

    playback_state = state.get_playback_state(session_id, target_client_id) or {}
    queue_state = state.get_local_queue(session_id, target_client_id) or state.get_queue(session_id)
    queue_song_ids = list(
        (queue_state or {}).get("queueSongIds")
        or playback_state.get("queueSongIds")
        or []
    )
    current_index = playback_state.get("currentIndex", 0)
    requested_index = payload.get("queueIndex", payload.get("currentIndex"))
    requested_track_id = payload.get("trackId")

    if requested_index is not None:
        if not _is_int(requested_index) or requested_index < 0:
            raise ValueError("player.seek queueIndex must be a non-negative integer")
        if requested_index >= len(queue_song_ids):
            raise ValueError("player.seek queueIndex is out of bounds")
        current_index = requested_index
        requested_track_id = queue_song_ids[current_index]
    elif isinstance(requested_track_id, str) and requested_track_id:
        if queue_song_ids and requested_track_id in queue_song_ids:
            current_index = queue_song_ids.index(requested_track_id)
    else:
        return None

    if requested_track_id == playback_state.get("trackId"):
        return None

    current_control_version = playback_state.get("controlVersion", 0)
    if queue_state is not None:
        current_control_version = max(
            current_control_version,
            queue_state.get("controlVersion", 0),
        )
    _validate_source_base_control_version(
        session_id,
        target_client_id,
        payload,
        current_control_version=current_control_version,
    )

    return {
        "userName": current_user_name,
        "action": "queue.playItem",
        "sessionId": session_id,
        "targetClientId": target_client_id,
        "sourceClientId": current_client.get("clientId"),
        "timelineId": f"session:{session_id}:client:{target_client_id}",
        "queueSongIds": queue_song_ids,
        "currentIndex": current_index,
        "trackId": requested_track_id,
        "positionMs": position_ms,
        "controlVersion": current_control_version + 1,
    }


def _source_prepare_target_client_ids(source_client_id, user_name):
    target_client_ids = [source_client_id]
    for relationship in state.list_followers_for_source(source_client_id):
        follower_client_id = relationship.get("followerClientId")
        if not follower_client_id or follower_client_id in target_client_ids:
            continue
        follower_client = state.get_client(
            follower_client_id,
            user_name=user_name,
        )
        if follower_client is None or not _has_role(follower_client, "player"):
            continue
        if not (
            _client_supports(follower_client, CAPABILITY_EFFECTIVE_AT)
            and _client_supports(follower_client, CAPABILITY_PLAYBACK_PREPARE)
        ):
            continue
        target_client_ids.append(follower_client_id)
    return target_client_ids


def _create_source_prepare(current_user_name, current_client, target_client_id, request_id, commit_payload):
    prepare_id = _new_prepare_id()
    now_ms = _server_time_ms()
    target_client_ids = _source_prepare_target_client_ids(
        target_client_id,
        current_user_name,
    )
    prepare = state.create_prepare(
        prepare_id,
        commit_payload["action"],
        commit_payload["timelineId"],
        target_client_ids,
        [target_client_id],
        commit_payload["controlVersion"],
        commit_payload,
        now_ms,
        now_ms + PREPARE_TIMEOUT_MS,
        request_sid=request.sid,
        request_id=request_id,
    )
    _send_playback_prepare(
        prepare,
        {
            "prepareId": prepare_id,
            "sourceClientId": target_client_id,
            "timelineId": commit_payload["timelineId"],
            "queueSongIds": list(commit_payload.get("queueSongIds") or []),
            "currentIndex": commit_payload.get("currentIndex", 0),
            "trackId": commit_payload.get("trackId"),
            "positionMs": commit_payload.get("positionMs", 0),
            "controlVersion": commit_payload["controlVersion"],
            "state": "playing",
        },
    )
    socketio.start_background_task(_expire_prepare_later, prepare_id)
    _send_ack(
        request_id,
        {
            "preparing": True,
            "prepareId": prepare_id,
            "targetClientId": target_client_id,
            "targetClientIds": target_client_ids,
            "protocolPath": PROTOCOL_TWO_PHASE,
        },
    )
    _log_emo_event(
        logging.INFO,
        "playback_prepare",
        result="created",
        user=current_user_name,
        client_request_id=request_id,
        source_client_id=current_client.get("clientId"),
        target_client_id=target_client_id,
        prepare_id=prepare_id,
        protocol_path=PROTOCOL_TWO_PHASE,
    )
    return prepare


def _handle_strict_v2_context_control(
    current_user_name,
    current_client,
    action,
    payload,
    request_id,
):
    playback_context_id = payload["playbackContextId"]
    context = _get_existing_playback_context(playback_context_id)
    if context is None:
        raise LookupError("Playback context not found")
    _ensure_playback_context_for_user(context, current_user_name)
    _ensure_playback_context_active(context)
    if not _has_role(current_client, "controller"):
        raise PermissionError("Player control requires the controller role")
    _ensure_not_follow_source_context_control(
        current_client,
        playback_context_id,
        action,
    )

    authority_client_id = context.get("authorityClientId")
    authority_client = state.get_client(
        authority_client_id,
        user_name=current_user_name,
    )
    authority_sid = state.get_sid_for_client(
        authority_client_id,
        user_name=current_user_name,
    )
    if authority_client is None or authority_sid is None:
        raise PlaybackAuthorityOfflineError("Playback context authority is offline")
    if not socketio.server.manager.is_connected(authority_sid, namespace="/emo"):
        raise PlaybackAuthorityOfflineError("Playback context authority socket is unavailable")
    if (
        authority_client.get("deviceSessionId")
        != context.get("authorityDeviceSessionId")
    ):
        raise PlaybackAuthorityOfflineError(
            "Playback context authority device is not connected"
        )
    if not _has_role(authority_client, "player"):
        raise PlaybackAuthorityOfflineError("Playback context authority is not a player")

    required_capability = {
        "player.pause": CAPABILITY_CAN_PAUSE,
        "player.seek": CAPABILITY_CAN_SEEK,
    }.get(action, CAPABILITY_CAN_PLAY)
    if not _client_supports(authority_client, required_capability):
        raise CapabilityRequiredError(
            "Playback authority lacks %s" % required_capability
        )

    requested_index = None
    position_ms = payload.get("positionMs")
    if action == "queue.playItem":
        requested_index = payload["queueIndex"]
        position_ms = 0
    elif action == "player.next":
        position_ms = 0
    elif action == "player.prev":
        position_ms = 0
    elif action == "player.play" and position_ms is None:
        position_ms = context.get("positionMs", 0)

    if not strict_v2_safety.reserve_emit(authority_sid):
        raise PlaybackAuthorityOfflineError(
            "Playback context authority send buffer is unavailable"
        )
    try:
        updated_context = mutateStrictPlaybackContextControl(
            playback_context_id,
            current_user_name,
            current_client.get("clientId"),
            action,
            payload["baseControlVersion"],
            base_queue_revision=payload.get("baseQueueRevision"),
            position_ms=position_ms,
            current_index=requested_index,
        )
        if updated_context is None:
            raise LookupError("Playback context not found")
        state.restore_playback_context(playback_context_id, updated_context)

        source_client_id = current_client.get("clientId")
        if action == "queue.playItem":
            outgoing_payload = {
                "playbackContextId": playback_context_id,
                "queueSongIds": list(updated_context["queueSongIds"]),
                "queueIndex": updated_context["currentIndex"],
                "queueRevision": updated_context["queueRevision"],
                "controlVersion": updated_context["controlVersion"],
                "sourceClientId": source_client_id,
            }
        else:
            outgoing_payload = {
                "playbackContextId": playback_context_id,
                "controlVersion": updated_context["controlVersion"],
                "sourceClientId": source_client_id,
            }
            if action in {"player.play", "player.pause", "player.seek"}:
                outgoing_payload["positionMs"] = updated_context["positionMs"]
        _emit_message(
            _build_message("command", action, outgoing_payload),
            authority_sid,
            emit_reserved=True,
        )
    finally:
        strict_v2_safety.release_emit(authority_sid)
    _send_ack(request_id)
    if action == "queue.playItem":
        _run_post_commit_push(
            action,
            request_id,
            lambda: _broadcast_context_queue_v2(
                current_user_name,
                playback_context_id,
            ),
        )
    _run_post_commit_push(
        action,
        request_id,
        lambda: _broadcast_playback_context_state_v2(
            current_user_name,
            playback_context_id,
        ),
    )
    return True


def _handle_v2_context_control(current_user_name, current_client, action, payload, request_id):
    strict_v2 = _is_strict_playback_context_v2(current_client)
    if strict_v2:
        return _handle_strict_v2_context_control(
            current_user_name,
            current_client,
            action,
            payload,
            request_id,
        )
    playback_context_id = _resolve_v2_playback_context_id(
        payload,
        strict_v2=strict_v2,
    )
    if not isinstance(playback_context_id, str) or not playback_context_id:
        raise ValueError(f"{action} requires a non-empty playbackContextId")

    context = _get_existing_playback_context(playback_context_id)
    if context is None:
        raise LookupError("Playback context not found")
    _ensure_playback_context_for_user(context, current_user_name)
    _ensure_not_follow_source_context_control(
        current_client,
        playback_context_id,
        action,
    )

    current_control_version = context.get("controlVersion", 0)
    base_control_version = _get_optional_source_base_control_version(payload)
    if (
        base_control_version is not None
        and base_control_version != current_control_version
    ):
        raise ControlConflictError(
            "Playback control version conflict",
            current_control_version=current_control_version,
        )

    authority_client_id = context.get("authorityClientId")
    authority_client = state.get_client(
        authority_client_id,
        user_name=current_user_name,
    )
    authority_sid = state.get_sid_for_client(
        authority_client_id,
        user_name=current_user_name,
    )
    if authority_client is None or authority_sid is None:
        raise PlaybackAuthorityOfflineError("Playback context authority is offline")
    if authority_client.get("userName") != current_user_name:
        raise PermissionError("Playback context authority belongs to another user")

    command_payload = dict(payload)
    command_payload["playbackContextId"] = playback_context_id
    command_payload["authorityClientId"] = authority_client_id
    command_payload["controlVersion"] = current_control_version + 1
    command_payload.pop("sessionId", None)

    if action == "player.pause":
        position_ms = payload.get("positionMs")
        if position_ms is not None and not _is_non_negative_int(position_ms):
            raise ValueError("player.pause positionMs must be a non-negative integer")
        updated_context = state.apply_playback_context_control(
            playback_context_id,
            current_client.get("clientId"),
            state_name="paused",
            position_ms=position_ms,
            control_version=current_control_version + 1,
        )
    elif action == "player.seek":
        position_ms = payload.get("positionMs")
        if not _is_non_negative_int(position_ms):
            raise ValueError("player.seek positionMs must be a non-negative integer")
        updated_context = state.apply_playback_context_control(
            playback_context_id,
            current_client.get("clientId"),
            state_name=context.get("state") or "paused",
            position_ms=position_ms,
            control_version=current_control_version + 1,
        )
    elif action in ("player.play", "player.next", "player.prev", "queue.playItem"):
        queue_song_ids = list(context.get("queueSongIds") or [])
        current_index = context.get("currentIndex", 0)
        if not _is_int(current_index) or current_index < 0:
            current_index = 0
        requested_index = current_index
        if action == "queue.playItem":
            requested_index = payload.get("queueIndex", payload.get("currentIndex"))
            requested_track_id = payload.get("trackId")
            if requested_index is None:
                if not isinstance(requested_track_id, str) or not requested_track_id:
                    raise ValueError("queue.playItem requires queueIndex or trackId")
                if requested_track_id not in queue_song_ids:
                    raise ValueError("queue.playItem trackId is not in context queue")
                requested_index = queue_song_ids.index(requested_track_id)
        elif action == "player.next":
            requested_index = current_index + 1
        elif action == "player.prev":
            requested_index = max(0, current_index - 1)
        if not _is_int(requested_index) or requested_index < 0:
            raise ValueError(f"{action} queueIndex must be a non-negative integer")
        if requested_index >= len(queue_song_ids):
            raise ValueError(f"{action} queueIndex is out of bounds")
        position_ms = payload.get(
            "positionMs",
            context.get("positionMs", 0) if action == "player.play" else 0,
        )
        if not _is_non_negative_int(position_ms):
            raise ValueError(f"{action} positionMs must be a non-negative integer")
        command_payload["queueIndex"] = requested_index
        command_payload["currentIndex"] = requested_index
        command_payload["trackId"] = queue_song_ids[requested_index]
        command_payload["positionMs"] = position_ms
        updated_context = state.apply_playback_context_control(
            playback_context_id,
            current_client.get("clientId"),
            state_name="playing",
            position_ms=position_ms,
            current_index=requested_index,
            control_version=current_control_version + 1,
        )
    else:
        return False

    if updated_context is None:
        raise LookupError("Playback context not found")
    _supersede_prepares_for_timeline(
        updated_context.get("timelineId") or f"playback:{playback_context_id}"
    )
    _update_playback_context_snapshot(current_user_name, updated_context)
    _broadcast_playback_context_state_v2(current_user_name, playback_context_id)
    outgoing_payload = command_payload
    if _is_strict_playback_context_v2(authority_client):
        source_client_id = current_client.get("clientId")
        if action == "queue.playItem":
            outgoing_payload = {
                "playbackContextId": playback_context_id,
                "queueSongIds": list(updated_context.get("queueSongIds") or []),
                "queueIndex": updated_context.get("currentIndex"),
                "queueRevision": updated_context.get("queueRevision"),
                "controlVersion": updated_context.get("controlVersion"),
                "sourceClientId": source_client_id,
            }
        else:
            outgoing_payload = {
                "playbackContextId": playback_context_id,
                "controlVersion": updated_context.get("controlVersion"),
                "sourceClientId": source_client_id,
            }
            if action in {"player.play", "player.pause", "player.seek"}:
                outgoing_payload["positionMs"] = updated_context.get("positionMs", 0)
    _emit_message(
        _build_message(
            "command",
            action,
            outgoing_payload,
            requestId=request_id,
            sourceClientId=current_client.get("clientId"),
            targetClientId=authority_client_id,
        ),
        authority_sid,
    )
    _send_ack(
        request_id,
        {
            "updated": True,
            "protocolPath": "playback_context_v2",
            "playbackContext": serializePlaybackContextV2(updated_context),
            "authorityClientId": authority_client_id,
        },
    )
    return True


def _handle_server_mediated_control(current_user_name, current_client, message, request_id):
    action = message["action"]
    if action not in (
        "player.play",
        "player.pause",
        "player.seek",
        "player.next",
        "player.prev",
        "queue.playItem",
    ):
        return False

    payload = message.get("payload") or {}
    if action in (
        "player.play",
        "player.pause",
        "player.seek",
        "player.next",
        "player.prev",
        "queue.playItem",
    ) and (
        _is_strict_playback_context_v2(current_client) or _is_context_payload(payload)
    ):
        return _handle_v2_context_control(
            current_user_name,
            current_client,
            action,
            payload,
            request_id,
        )

    target_client_id, target_sid, target_client = _resolve_control_target(current_client, message)
    protocol = _select_playback_protocol(
        [target_client_id],
        user_name=current_user_name,
    )
    if protocol == PROTOCOL_LEGACY:
        return False

    if action == "player.pause":
        session_id = payload.get("sessionId") or target_client.get("sessionId")
        current_control_version = _current_source_control_version(
            session_id,
            target_client_id,
        )
        _validate_source_base_control_version(
            session_id,
            target_client_id,
            payload,
            current_control_version=current_control_version,
        )
        position_ms = payload.get("positionMs")
        playback_state = state.update_playback_control(
            session_id,
            target_client_id,
            state_name="paused",
            position_ms=position_ms,
            updated_by_client_id=current_client.get("clientId"),
            control_version=current_control_version + 1,
        )
        _supersede_prepares_for_timeline(
            playback_state.get("timelineId") or _source_timeline_id(session_id, target_client_id)
        )
        _save_playback_state_snapshot(current_user_name, playback_state)
        _broadcast_playback_state(current_user_name, session_id)
        _emit_message(
            _build_message(
                "command",
                "player.pause",
                payload,
                requestId=request_id,
                sourceClientId=current_client.get("clientId"),
                targetClientId=target_client_id,
            ),
            target_sid,
        )
        _send_ack(
            request_id,
            {
                "updated": True,
                "protocolPath": protocol,
                "playback": playback_state,
            },
        )
        return True

    commit_payload = None
    if action == "player.seek":
        commit_payload = _build_seek_media_change_commit_payload(
            current_user_name,
            current_client,
            target_client_id,
            target_client,
            payload,
        )

    if action == "player.seek" and commit_payload is None:
        session_id = payload.get("sessionId") or target_client.get("sessionId")
        position_ms = payload.get("positionMs")
        if not _is_non_negative_int(position_ms):
            raise ValueError("player.seek positionMs must be a non-negative integer")
        current_playback = state.get_playback_state(session_id, target_client_id) or {}
        current_control_version = _current_source_control_version(
            session_id,
            target_client_id,
        )
        _validate_source_base_control_version(
            session_id,
            target_client_id,
            payload,
            current_control_version=current_control_version,
        )
        state_name = current_playback.get("state") or "paused"
        effective_at_server_ms = (
            _server_time_ms() + 250
            if state_name == "playing"
            else None
        )
        playback_state = state.update_playback_control(
            session_id,
            target_client_id,
            state_name=state_name,
            track_id=current_playback.get("trackId"),
            position_ms=position_ms,
            queue_song_ids=current_playback.get("queueSongIds"),
            current_index=current_playback.get("currentIndex"),
            updated_by_client_id=current_client.get("clientId"),
            effective_at_server_ms=effective_at_server_ms,
            control_version=current_control_version + 1,
        )
        _supersede_prepares_for_timeline(
            playback_state.get("timelineId") or _source_timeline_id(session_id, target_client_id)
        )
        _save_playback_state_snapshot(current_user_name, playback_state)
        _broadcast_playback_state(current_user_name, session_id)
        command_payload = dict(payload)
        if effective_at_server_ms is not None:
            command_payload["effectiveAtServerMs"] = effective_at_server_ms
            command_payload["controlVersion"] = playback_state["controlVersion"]
        _emit_message(
            _build_message(
                "command",
                "player.seek",
                command_payload,
                requestId=request_id,
                sourceClientId=current_client.get("clientId"),
                targetClientId=target_client_id,
            ),
            target_sid,
        )
        _send_ack(
            request_id,
            {
                "updated": True,
                "protocolPath": PROTOCOL_SINGLE_FUTURE,
                "playback": playback_state,
            },
        )
        return True

    if commit_payload is None:
        commit_payload = _build_source_control_commit_payload(
            current_user_name,
            current_client,
            target_client_id,
            target_client,
            action,
            payload,
        )
    if commit_payload is None:
        return False

    if protocol == PROTOCOL_TWO_PHASE:
        _create_source_prepare(
            current_user_name,
            current_client,
            target_client_id,
            request_id,
            commit_payload,
        )
        return True

    effective_at_server_ms = _effective_at_server_ms(PROTOCOL_SINGLE_FUTURE)
    playback_state = state.update_playback_control(
        commit_payload["sessionId"],
        target_client_id,
        state_name="playing",
        track_id=commit_payload.get("trackId"),
        position_ms=commit_payload.get("positionMs", 0),
        queue_song_ids=commit_payload.get("queueSongIds"),
        current_index=commit_payload.get("currentIndex"),
        updated_by_client_id=current_client.get("clientId"),
        effective_at_server_ms=effective_at_server_ms,
        control_version=commit_payload.get("controlVersion"),
    )
    _supersede_prepares_for_timeline(
        playback_state.get("timelineId")
        or _source_timeline_id(commit_payload["sessionId"], target_client_id)
    )
    _save_playback_state_snapshot(current_user_name, playback_state)
    _broadcast_playback_state(current_user_name, commit_payload["sessionId"])
    _send_target_player_play(
        target_client_id,
        current_client.get("clientId"),
        request_id,
        commit_payload["sessionId"],
        effective_at_server_ms,
        playback_state["controlVersion"],
        user_name=current_user_name,
    )
    _send_ack(
        request_id,
        {
            "updated": True,
            "protocolPath": protocol,
            "playback": playback_state,
        },
    )
    return True


def _handle_strict_broadcast_start(
    current_user_name,
    current_client,
    payload,
    request_id,
):
    playback_context_id = payload["playbackContextId"]
    context = _get_strict_broadcast_context(
        current_user_name,
        playback_context_id,
    )
    client_id = current_client.get("clientId")
    if client_id != context.get("authorityClientId") and not _has_role(
        current_client,
        "controller",
    ):
        raise PermissionError(
            "Broadcast start requires Context authority or controller"
        )

    active_broadcast = state.get_broadcast_by_playback_context(
        playback_context_id
    )
    if active_broadcast is not None and state.is_broadcast_active(
        active_broadcast.get("broadcastId")
    ):
        raise BroadcastConflictError("Playback context already has an active Broadcast")

    queue_song_ids = list(payload["queueSongIds"])
    current_index = payload["currentIndex"]
    position_ms = payload["positionMs"]
    auto_play = payload.get("autoPlay", False)
    participant_ids, skipped_client_ids = (
        _resolve_strict_broadcast_start_participants(
            current_user_name,
            context,
            payload,
        )
    )
    try:
        broadcast = state.create_broadcast(
            _new_broadcast_id(),
            current_user_name,
            client_id,
            participant_ids,
            queue_song_ids,
            current_index,
            position_ms,
            "playing" if auto_play else "paused",
            "owner_only",
            client_id,
            playback_context_id=playback_context_id,
            authority_client_id=context.get("authorityClientId"),
            require_context_available=True,
        )
    except PlaybackContextConflictError:
        raise BroadcastConflictError(
            "Playback context already has an active Broadcast"
        )
    for participant_id in participant_ids:
        participant = state.get_client(
            participant_id,
            user_name=current_user_name,
        )
        state.update_broadcast_participant_state(
            broadcast["broadcastId"],
            participant_id,
            None if participant is None else participant.get("deviceSessionId"),
            {
                "state": broadcast["state"],
                "positionMs": broadcast["positionMs"],
            },
            online=True,
        )

    _send_ack(
        request_id,
        {
            "started": True,
            "broadcastId": broadcast["broadcastId"],
            "participants": participant_ids,
            "skippedClientIds": skipped_client_ids,
        },
    )
    _run_post_commit_push(
        "broadcast.start",
        request_id,
        lambda: _emit_strict_broadcast_to_participants(
            broadcast,
            "broadcast.start",
            "command",
        ),
    )
    return broadcast


def _require_strict_broadcast_status_access(current_client, broadcast):
    client_id = current_client.get("clientId")
    if client_id not in {
        broadcast.get("ownerClientId"),
        broadcast.get("authorityClientId"),
        *(broadcast.get("participants") or []),
    }:
        raise PermissionError("Broadcast status access is not allowed")


def _require_strict_broadcast_control(
    current_user_name,
    current_client,
    broadcast,
    require_authority_online=True,
):
    context = _get_strict_broadcast_context(
        current_user_name,
        broadcast.get("playbackContextId"),
    )
    client_id = current_client.get("clientId")
    if client_id not in {
        broadcast.get("ownerClientId"),
        context.get("authorityClientId"),
    }:
        raise PermissionError("Broadcast control is not allowed")
    if require_authority_online:
        _get_online_strict_broadcast_authority(
            current_user_name,
            context,
        )
    return context


def _handle_strict_broadcast_status(
    current_user_name,
    current_client,
    payload,
    request_id,
):
    broadcast = _get_strict_broadcast_from_payload(
        current_user_name,
        payload,
    )
    _require_strict_broadcast_status_access(current_client, broadcast)
    _send_ack(request_id, _build_strict_broadcast_status_payload(broadcast))
    return broadcast


def _handle_strict_broadcast_stop(
    current_user_name,
    current_client,
    payload,
    request_id,
):
    broadcast = _get_strict_broadcast_from_payload(
        current_user_name,
        payload,
    )
    _require_strict_broadcast_control(
        current_user_name,
        current_client,
        broadcast,
        require_authority_online=False,
    )
    if not state.is_broadcast_active(broadcast["broadcastId"]):
        _send_ack(request_id)
        return broadcast

    updated = state.stop_broadcast(
        broadcast["broadcastId"],
        current_client.get("clientId"),
        increment_control_version=False,
    )
    if updated is None:
        raise LookupError("Broadcast not found")
    _send_ack(request_id)
    _run_post_commit_push(
        "broadcast.stop",
        request_id,
        lambda: _emit_strict_broadcast_to_participants(
            updated,
            "broadcast.stop",
            "command",
        ),
    )
    return updated


def _update_strict_broadcast_state(broadcast, current_client, **changes):
    try:
        updated = state.update_broadcast_state(
            broadcast["broadcastId"],
            current_client.get("clientId"),
            require_active=True,
            **changes,
        )
    except BroadcastInactiveError:
        raise BroadcastConflictError("Broadcast is stopped")
    except BroadcastVersionMismatchError:
        current = state.get_broadcast(broadcast["broadcastId"]) or broadcast
        raise PlaybackContextStaleVersionError(current, "controlVersion")
    except BroadcastCursorMismatchError as exc:
        raise PlaybackContextStaleVersionError(
            exc.broadcast,
            exc.cursor_name,
        )
    if updated is None:
        raise LookupError("Broadcast not found")
    return updated


def _settle_strict_broadcast_mutation(
    action,
    request_id,
    updated,
    msg_type,
):
    _send_ack(request_id)
    _run_post_commit_push(
        action,
        request_id,
        lambda: _emit_strict_broadcast_to_participants(
            updated,
            action,
            msg_type,
        ),
    )
    return updated


def _handle_strict_broadcast_play(
    current_user_name,
    current_client,
    payload,
    request_id,
):
    broadcast = _get_strict_broadcast_from_payload(current_user_name, payload)
    _require_strict_broadcast_control(
        current_user_name,
        current_client,
        broadcast,
    )
    updated = _update_strict_broadcast_state(
        broadcast,
        current_client,
        state_name="playing",
        effective_at_server_ms=_effective_at_server_ms(PROTOCOL_SINGLE_FUTURE),
    )
    return _settle_strict_broadcast_mutation(
        "broadcast.play",
        request_id,
        updated,
        "command",
    )


def _handle_strict_broadcast_pause(
    current_user_name,
    current_client,
    payload,
    request_id,
):
    broadcast = _get_strict_broadcast_from_payload(current_user_name, payload)
    _require_strict_broadcast_control(
        current_user_name,
        current_client,
        broadcast,
    )
    updated = _update_strict_broadcast_state(
        broadcast,
        current_client,
        position_ms=_estimate_broadcast_position_ms(broadcast),
        state_name="paused",
    )
    updated["effectiveAtServerMs"] = _effective_at_server_ms(
        PROTOCOL_SINGLE_FUTURE
    )
    return _settle_strict_broadcast_mutation(
        "broadcast.pause",
        request_id,
        updated,
        "command",
    )


def _handle_strict_broadcast_seek(
    current_user_name,
    current_client,
    payload,
    request_id,
):
    broadcast = _get_strict_broadcast_from_payload(current_user_name, payload)
    _require_strict_broadcast_control(
        current_user_name,
        current_client,
        broadcast,
    )
    updated = _update_strict_broadcast_state(
        broadcast,
        current_client,
        position_ms=payload["positionMs"],
        effective_at_server_ms=_effective_at_server_ms(PROTOCOL_SINGLE_FUTURE),
    )
    updated.setdefault(
        "effectiveAtServerMs",
        _effective_at_server_ms(PROTOCOL_SINGLE_FUTURE),
    )
    return _settle_strict_broadcast_mutation(
        "broadcast.seek",
        request_id,
        updated,
        "command",
    )


def _handle_strict_broadcast_play_item(
    current_user_name,
    current_client,
    payload,
    request_id,
):
    broadcast = _get_strict_broadcast_from_payload(current_user_name, payload)
    _require_strict_broadcast_control(
        current_user_name,
        current_client,
        broadcast,
    )
    queue_index = payload["queueIndex"]
    if queue_index >= len(broadcast.get("queueSongIds") or []):
        raise ValueError("broadcast.playItem queueIndex is out of bounds")
    updated = _update_strict_broadcast_state(
        broadcast,
        current_client,
        current_index=queue_index,
        position_ms=0,
        state_name="playing",
        increment_queue_revision=True,
        effective_at_server_ms=_effective_at_server_ms(PROTOCOL_SINGLE_FUTURE),
    )
    return _settle_strict_broadcast_mutation(
        "broadcast.playItem",
        request_id,
        updated,
        "command",
    )


def _handle_strict_broadcast_queue_sync(
    current_user_name,
    current_client,
    payload,
    request_id,
):
    broadcast = _get_strict_broadcast_from_payload(current_user_name, payload)
    _require_strict_broadcast_control(
        current_user_name,
        current_client,
        broadcast,
    )
    queue_song_ids = list(payload["queueSongIds"])
    current_index = payload["currentIndex"]
    position_ms = payload["positionMs"]
    previous_queue = list(broadcast.get("queueSongIds") or [])
    previous_index = broadcast.get("currentIndex", 0)
    previous_position_ms = broadcast.get("positionMs", 0)
    previous_track_id = broadcast.get("trackId")
    track_id = queue_song_ids[current_index]
    queue_changed = queue_song_ids != previous_queue
    control_changed = (
        current_index != previous_index
        or track_id != previous_track_id
        or position_ms != previous_position_ms
    )
    if queue_changed and "baseQueueRevision" not in payload:
        raise ValueError(
            "broadcast.queue.sync requires baseQueueRevision when queue changes"
        )
    if control_changed and "baseControlVersion" not in payload:
        raise ValueError(
            "broadcast.queue.sync requires baseControlVersion when control changes"
        )
    updated = _update_strict_broadcast_state(
        broadcast,
        current_client,
        queue_song_ids=queue_song_ids,
        current_index=current_index,
        position_ms=position_ms,
        increment_queue_revision=True,
        increment_control_version=control_changed,
        expected_version=payload.get("baseControlVersion"),
        expected_queue_revision=payload.get("baseQueueRevision"),
    )
    return _settle_strict_broadcast_mutation(
        "broadcast.queue.sync",
        request_id,
        updated,
        "state",
    )


def _handle_broadcast_start(current_user_name, current_client, payload, request_id):
    if current_client is None:
        raise PermissionError("Register the device before starting broadcast")

    strict_v2 = _is_strict_playback_context_v2(current_client)
    if strict_v2:
        return _handle_strict_broadcast_start(
            current_user_name,
            current_client,
            payload,
            request_id,
        )
    playback_context_id = _get_broadcast_playback_context_id(
        payload,
        strict_v2=strict_v2,
    )
    if strict_v2 and playback_context_id is None:
        raise ValueError("broadcast.start requires a non-empty playbackContextId")
    _ensure_broadcast_playback_context_available(
        current_user_name,
        playback_context_id,
    )

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
    protocol = (
        _select_playback_protocol(participant_ids, user_name=current_user_name)
        if state_name == "playing"
        else PROTOCOL_LEGACY
    )
    if protocol == PROTOCOL_TWO_PHASE:
        broadcast_id = _new_broadcast_id()
        track_id = queue_song_ids[current_index] if queue_song_ids else None
        prepare_payload = {
            "broadcastId": broadcast_id,
            "timelineId": f"broadcast:{broadcast_id}",
            "userName": current_user_name,
            "ownerClientId": current_client.get("clientId"),
            "sourceClientId": current_client.get("clientId"),
            "participants": list(participant_ids),
            "skippedClientIds": list(skipped_client_ids),
            "queueSongIds": list(queue_song_ids),
            "currentIndex": current_index,
            "trackId": track_id,
            "positionMs": position_ms,
            "stateName": state_name,
            "controlPolicy": control_policy,
            "updatedByClientId": current_client.get("clientId"),
            "autoPlay": auto_play,
            "controlVersion": 1,
        }
        if playback_context_id is not None:
            prepare_payload["playbackContextId"] = playback_context_id
        return _create_broadcast_prepare(
            "broadcast.start",
            current_user_name,
            current_client,
            participant_ids,
            request_id,
            request.sid,
            prepare_payload,
        )

    effective_at_server_ms = (
        _effective_at_server_ms(PROTOCOL_SINGLE_FUTURE)
        if protocol == PROTOCOL_SINGLE_FUTURE and state_name == "playing"
        else None
    )
    broadcast = state.create_broadcast(
        _new_broadcast_id(),
        current_user_name,
        current_client.get("clientId"),
        participant_ids,
        queue_song_ids,
        current_index if queue_song_ids else None,
        position_ms,
        state_name,
        control_policy,
        current_client.get("clientId"),
        effective_at_server_ms=effective_at_server_ms,
        playback_context_id=playback_context_id,
    )
    _sync_broadcast_playback_context(broadcast)

    for participant_id in participant_ids:
        participant = state.get_client(
            participant_id,
            user_name=current_user_name,
        )
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
            "protocolPath": protocol,
        },
    )
    _broadcast_to_participants(
        broadcast,
        "broadcast.start",
        "command",
        current_client.get("clientId"),
        request_id=request_id,
        extra_payload={"autoPlay": auto_play, "serverStartAt": None, "protocolPath": protocol},
    )
    return broadcast


def _handle_broadcast_status(current_user_name, current_client, payload, request_id):
    if current_client is None:
        raise PermissionError("Register the device before requesting broadcast status")
    if _is_strict_playback_context_v2(current_client):
        return _handle_strict_broadcast_status(
            current_user_name,
            current_client,
            payload,
            request_id,
        )
    broadcast = _get_broadcast_from_payload(current_user_name, payload)
    _send_ack(request_id, _build_broadcast_status_payload(broadcast))
    _broadcast_status_to_requester(broadcast)
    return broadcast


def _handle_broadcast_queue_sync(current_user_name, current_client, payload, request_id):
    if _is_strict_playback_context_v2(current_client):
        return _handle_strict_broadcast_queue_sync(
            current_user_name,
            current_client,
            payload,
            request_id,
        )
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
    if _is_strict_playback_context_v2(current_client):
        return _handle_strict_broadcast_play_item(
            current_user_name,
            current_client,
            payload,
            request_id,
        )
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
    _ensure_broadcast_base_control_version_current(broadcast, base_version)

    protocol = _select_playback_protocol(
        broadcast.get("participants") or [],
        user_name=current_user_name,
    )
    if protocol == PROTOCOL_TWO_PHASE:
        prepare_payload = {
            "broadcastId": broadcast["broadcastId"],
            "timelineId": broadcast.get("timelineId") or f"broadcast:{broadcast['broadcastId']}",
            "ownerClientId": broadcast.get("ownerClientId"),
            "sourceClientId": current_client.get("clientId"),
            "participants": list(broadcast.get("participants") or []),
            "queueSongIds": list(queue_song_ids),
            "currentIndex": queue_index,
            "trackId": queue_song_ids[queue_index],
            "positionMs": position_ms,
            "updatedByClientId": current_client.get("clientId"),
            "queueIndex": queue_index,
            "expectedVersion": base_version,
            "controlVersion": broadcast.get("controlVersion", broadcast.get("version", 0)) + 1,
        }
        if broadcast.get("playbackContextId") is not None:
            prepare_payload["playbackContextId"] = broadcast.get("playbackContextId")
        return _create_broadcast_prepare(
            "broadcast.playItem",
            current_user_name,
            current_client,
            broadcast.get("participants") or [],
            request_id,
            request.sid,
            prepare_payload,
        )

    effective_at_server_ms = (
        _effective_at_server_ms(PROTOCOL_SINGLE_FUTURE)
        if protocol == PROTOCOL_SINGLE_FUTURE
        else None
    )
    updated = _update_active_broadcast_state(
        broadcast["broadcastId"],
        current_client.get("clientId"),
        current_index=queue_index,
        position_ms=position_ms,
        state_name="playing",
        expected_version=base_version,
        effective_at_server_ms=effective_at_server_ms,
    )
    _send_ack(request_id, {"updated": True, "broadcast": updated})
    _broadcast_to_participants(
        updated,
        "broadcast.playItem",
        "command",
        current_client.get("clientId"),
        request_id=request_id,
        extra_payload={"queueIndex": queue_index, "protocolPath": protocol},
    )
    return updated


def _handle_broadcast_play(current_user_name, current_client, payload, request_id):
    if _is_strict_playback_context_v2(current_client):
        return _handle_strict_broadcast_play(
            current_user_name,
            current_client,
            payload,
            request_id,
        )
    broadcast = _get_broadcast_from_payload(current_user_name, payload)
    _require_broadcast_control(current_client, broadcast)
    queue_song_ids = broadcast.get("queueSongIds") or []
    current_index = broadcast.get("currentIndex", 0)
    if not queue_song_ids:
        raise ValueError("broadcast.play requires a non-empty queue")
    if not _is_int(current_index) or current_index < 0 or current_index >= len(queue_song_ids):
        raise ValueError("broadcast.play requires a valid currentIndex")

    expected_version = _get_optional_broadcast_base_control_version(payload)
    _ensure_broadcast_base_control_version_current(broadcast, expected_version)
    protocol = _select_playback_protocol(
        broadcast.get("participants") or [],
        user_name=current_user_name,
    )
    if protocol == PROTOCOL_TWO_PHASE:
        prepare_payload = {
            "broadcastId": broadcast["broadcastId"],
            "timelineId": broadcast.get("timelineId") or f"broadcast:{broadcast['broadcastId']}",
            "ownerClientId": broadcast.get("ownerClientId"),
            "sourceClientId": current_client.get("clientId"),
            "participants": list(broadcast.get("participants") or []),
            "queueSongIds": list(queue_song_ids),
            "currentIndex": current_index,
            "trackId": queue_song_ids[current_index],
            "positionMs": broadcast.get("positionMs", 0),
            "updatedByClientId": current_client.get("clientId"),
            "expectedVersion": expected_version,
            "controlVersion": broadcast.get("controlVersion", broadcast.get("version", 0)) + 1,
        }
        if broadcast.get("playbackContextId") is not None:
            prepare_payload["playbackContextId"] = broadcast.get("playbackContextId")
        return _create_broadcast_prepare(
            "broadcast.play",
            current_user_name,
            current_client,
            broadcast.get("participants") or [],
            request_id,
            request.sid,
            prepare_payload,
        )

    effective_at_server_ms = (
        _effective_at_server_ms(PROTOCOL_SINGLE_FUTURE)
        if protocol == PROTOCOL_SINGLE_FUTURE
        else None
    )
    updated = _update_active_broadcast_state(
        broadcast["broadcastId"],
        current_client.get("clientId"),
        state_name="playing",
        expected_version=expected_version,
        effective_at_server_ms=effective_at_server_ms,
    )
    _send_ack(request_id, {"updated": True, "broadcast": updated})
    _broadcast_to_participants(
        updated,
        "broadcast.play",
        "command",
        current_client.get("clientId"),
        request_id=request_id,
        extra_payload={"protocolPath": protocol},
    )
    return updated


def _handle_broadcast_pause(current_user_name, current_client, payload, request_id):
    if _is_strict_playback_context_v2(current_client):
        return _handle_strict_broadcast_pause(
            current_user_name,
            current_client,
            payload,
            request_id,
        )
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
    if _is_strict_playback_context_v2(current_client):
        return _handle_strict_broadcast_seek(
            current_user_name,
            current_client,
            payload,
            request_id,
        )
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
    if _is_strict_playback_context_v2(current_client):
        return _handle_strict_broadcast_stop(
            current_user_name,
            current_client,
            payload,
            request_id,
        )
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
    _supersede_prepares_for_timeline(
        updated.get("timelineId") or f"broadcast:{broadcast['broadcastId']}"
    )
    _sync_broadcast_playback_context(updated)
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
    strict_v2 = _is_strict_playback_context_v2(current_client)
    if strict_v2:
        if not _client_supports(current_client, CAPABILITY_SUPPORTS_BROADCAST):
            raise CapabilityRequiredError("strict-v2 client does not support Broadcast")
        _reject_session_id_for_strict_v2(payload, strict_v2=True)
        if action != "broadcast.start" and not payload.get("playbackContextId"):
            raise ValueError(f"{action} requires a non-empty playbackContextId")
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

    strict_v2 = _is_strict_playback_context_v2(current_client)
    if strict_v2 or _is_follow_context_payload(payload):
        if strict_v2 and not _client_supports(current_client, CAPABILITY_SUPPORTS_FOLLOW):
            raise CapabilityRequiredError("strict-v2 client does not support Follow")
        _reject_session_id_for_strict_v2(payload, strict_v2)
        if strict_v2 and not _has_role(current_client, "player"):
            raise PermissionError("Follow requires the player role")
        if strict_v2 and not _client_supports(current_client, CAPABILITY_CAN_PLAY):
            raise CapabilityRequiredError("Follow requires canPlay")
        source_playback_context_id = payload.get("sourcePlaybackContextId")
        if source_playback_context_id is None:
            source_playback_context_id = payload.get("playbackContextId")
        if not isinstance(source_playback_context_id, str) or not source_playback_context_id:
            raise ValueError("follow.start requires a non-empty sourcePlaybackContextId")

        playback_context = _get_existing_playback_context(source_playback_context_id)
        if playback_context is None:
            raise LookupError("Playback context not found")
        _ensure_playback_context_for_user(playback_context, current_user_name)
        _ensure_playback_context_active(playback_context)

        source_client_id = playback_context.get("authorityClientId")
        if source_client_id == current_client.get("clientId"):
            raise ValueError("follow.start source cannot be the current client")

        device_session_id = _resolve_v2_device_session_id(
            payload,
            current_client,
            strict_v2=strict_v2,
        )
        if not isinstance(device_session_id, str) or not device_session_id:
            raise ValueError("follow.start requires a non-empty deviceSessionId")
        if strict_v2 and device_session_id != current_client.get("deviceSessionId"):
            raise PermissionError("deviceSessionId does not match the registered device")

        existing = state.get_follow_relationship(current_client.get("clientId"))
        if existing is not None:
            if existing.get("sourcePlaybackContextId") != source_playback_context_id:
                raise FollowConflictError(
                    "Follow relationship already targets another playback context"
                )
            state.subscribe_playback_context(sid, source_playback_context_id)
            _send_ack(request_id)
            return existing

        relationship = state.start_follow_relationship(
            current_client.get("clientId"),
            device_session_id,
            source_client_id,
            None,
            current_user_name,
            source_playback_context_id=source_playback_context_id,
        )
        state.subscribe_playback_context(sid, source_playback_context_id)
        _send_ack(request_id)
        return relationship

    source_client_id = payload.get("sourceClientId") or payload.get("followSourceClientId")
    if not isinstance(source_client_id, str) or not source_client_id:
        raise ValueError("follow.start requires a non-empty sourceClientId")
    if source_client_id == current_client.get("clientId"):
        raise ValueError("follow.start sourceClientId cannot be the current client")

    source_client = state.get_client(
        source_client_id,
        user_name=current_user_name,
    )
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

    strict_v2 = _is_strict_playback_context_v2(current_client)
    if strict_v2 or _is_follow_context_payload(payload):
        if strict_v2 and not _client_supports(current_client, CAPABILITY_SUPPORTS_FOLLOW):
            raise CapabilityRequiredError("strict-v2 client does not support Follow")
        _reject_session_id_for_strict_v2(payload, strict_v2)
        source_playback_context_id = payload.get("sourcePlaybackContextId")
        if source_playback_context_id is None:
            source_playback_context_id = payload.get("playbackContextId")
        relationship = state.get_follow_relationship(current_client.get("clientId"))
        if (
            relationship is not None
            and source_playback_context_id is not None
            and relationship.get("sourcePlaybackContextId")
            != source_playback_context_id
        ):
            raise FollowConflictError(
                "Follow relationship targets another playback context"
            )
        if source_playback_context_id is None and relationship is not None:
            source_playback_context_id = relationship.get("sourcePlaybackContextId")

        relationship = state.stop_follow_relationship(current_client.get("clientId"))

        if source_playback_context_id:
            if not isinstance(source_playback_context_id, str):
                raise ValueError("follow.stop sourcePlaybackContextId must be a string")
            state.unsubscribe_playback_context(
                sid,
                source_playback_context_id,
            )
        else:
            state.unsubscribe_playback_context(sid)

        _send_ack(request_id)
        return relationship

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

    owner_client = state.get_client(
        owner_client_id,
        user_name=current_user_name,
    )
    if owner_client is None:
        if state.get_client(owner_client_id) is not None:
            raise PermissionError("Cross-user local queue update is not allowed")
        raise LookupError("Local queue client is offline")
    if owner_client.get("userName") != current_user_name:
        raise PermissionError("Cross-user local queue update is not allowed")
    if owner_client.get("sessionId") != session_id:
        raise ValueError("queue.local.set clientId must belong to sessionId")

    return session_id, owner_client_id


def _validate_local_queue_target(current_user_name, target_client_id):
    if not isinstance(target_client_id, str) or not target_client_id:
        raise ValueError("targetClientId must be a non-empty string")

    target_client = state.get_client(
        target_client_id,
        user_name=current_user_name,
    )
    if target_client is None:
        if state.get_client(target_client_id) is not None:
            raise PermissionError("Cross-user local queue routing is not allowed")
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


def _handle_playback_ready(current_client, payload, request_id):
    if current_client is None:
        raise PermissionError("Register the device before sending playback ready")

    prepare_id = payload.get("prepareId")
    if not isinstance(prepare_id, str) or not prepare_id:
        raise ValueError("playback.ready requires a non-empty prepareId")

    ready = payload.get("ready")
    if not isinstance(ready, bool):
        raise ValueError("playback.ready ready must be a boolean")

    payload_client_id = payload.get("clientId")
    current_client_id = current_client.get("clientId")
    if payload_client_id is not None and payload_client_id != current_client_id:
        raise PermissionError("playback.ready clientId must match the current device")

    prepare = state.get_prepare(prepare_id)
    strict_v2 = _is_strict_playback_context_v2(current_client)
    if prepare is None:
        if strict_v2:
            raise LookupError("Playback prepare not found")
        _send_ack(request_id, {"ignored": True, "prepareId": prepare_id})
        return None
    commit_payload = prepare.get("commitPayload") or {}
    if strict_v2:
        if prepare.get("action") != "playback.handoff.start":
            raise PermissionError("Strict playback.ready requires a handoff prepare")
        if payload.get("playbackContextId") != commit_payload.get(
            "playbackContextId"
        ):
            raise ValueError("playback.ready playbackContextId does not match prepare")
        payload_handoff_id = payload.get("handoffId")
        if (
            payload_handoff_id is not None
            and payload_handoff_id != commit_payload.get("handoffId")
        ):
            raise ValueError("playback.ready handoffId does not match prepare")
        if current_client_id != commit_payload.get("targetClientId"):
            raise PermissionError("playback.ready sender is not the handoff target")
        if current_client.get("deviceSessionId") != commit_payload.get(
            "targetDeviceSessionId"
        ):
            raise PermissionError("playback.ready device does not match handoff target")

    if prepare.get("status") != "preparing":
        if not strict_v2:
            _send_ack(
                request_id,
                {
                    "ignored": True,
                    "prepareId": prepare_id,
                    "status": prepare.get("status"),
                },
            )
        return prepare

    control_version = payload.get("controlVersion")
    if (
        control_version is not None
        and control_version != prepare.get("controlVersion")
    ):
        raise ValueError("playback.ready controlVersion does not match prepare")

    updated_prepare = state.update_prepare_ready(
        prepare_id,
        current_client_id,
        ready,
    )
    if updated_prepare is None:
        raise PermissionError("playback.ready sender is not a prepare target")
    if updated_prepare.get("status") != "preparing":
        _send_ack(
            request_id,
            {
                "ignored": True,
                "prepareId": prepare_id,
                "status": updated_prepare.get("status"),
            },
        )
        return updated_prepare

    if (
        not ready
        and current_client_id in set(updated_prepare.get("requiredClientIds") or [])
    ):
        terminal_status = "failed" if strict_v2 else "aborted"
        aborted_prepare = state.finish_prepare_if_preparing(
            prepare_id,
            terminal_status,
        )
        if aborted_prepare is None:
            latest_prepare = state.get_prepare(prepare_id) or updated_prepare
            if not strict_v2:
                _send_ack(
                    request_id,
                    {
                        "ignored": True,
                        "prepareId": prepare_id,
                        "status": latest_prepare.get("status"),
                    },
                )
            return latest_prepare
        _update_handoff_for_prepare(
            aborted_prepare,
            terminal_status,
            error_code=payload.get("errorCode") or "prepare_failed",
            error_message=payload.get("errorMessage"),
            post_commit_action="playback.ready" if strict_v2 else None,
            post_commit_request_id=request_id,
        )
        if not strict_v2:
            _send_ack(
                request_id,
                {"ready": False, "prepareId": prepare_id, "status": "aborted"},
            )
        return aborted_prepare

    committed = None
    if _prepare_ready_to_commit(updated_prepare):
        committed = _commit_prepare(
            updated_prepare,
            post_commit_request_id=request_id,
        )
    if not strict_v2:
        _send_ack(
            request_id,
            {
                "ready": ready,
                "prepareId": prepare_id,
                "status": "committed" if committed is not None else "preparing",
            },
        )
    return committed or updated_prepare


def _get_or_restore_playback_context(playback_context_id):
    context = state.get_playback_context(playback_context_id)
    if context is not None:
        return context

    persisted_context = getPlaybackContextState(playback_context_id)
    if persisted_context is not None:
        return state.restore_playback_context(playback_context_id, persisted_context)

    legacy_queue = getQueueState(playback_context_id)
    if legacy_queue is None:
        return None
    return state.restore_playback_context(
        playback_context_id,
        {
            "playbackContextId": playback_context_id,
            "userName": legacy_queue.get("userName"),
            "authorityClientId": legacy_queue.get("sourceClientId"),
            "originClientId": legacy_queue.get("sourceClientId"),
            "queueSongIds": legacy_queue.get("queueSongIds") or [],
            "currentIndex": legacy_queue.get("currentIndex", 0),
            "positionMs": legacy_queue.get("positionMs", 0),
            "queueRevision": legacy_queue.get("queueRevision", 1),
            "controlVersion": legacy_queue.get("controlVersion", 1),
            "version": legacy_queue.get("version", 1),
            "epoch": legacy_queue.get("epoch", 1),
            "serverUpdatedAtMs": legacy_queue.get("serverUpdatedAtMs"),
        },
    )


def _get_existing_playback_context(playback_context_id):
    context = state.get_playback_context(playback_context_id)
    if context is not None:
        return context

    persisted_context = getPlaybackContextState(playback_context_id)
    if persisted_context is not None:
        return state.restore_playback_context(playback_context_id, persisted_context)

    return None


def _ensure_playback_context_for_user(context, user_name):
    context_user_name = context.get("userName")
    if context_user_name is not None and context_user_name != user_name:
        raise PermissionError("Playback context belongs to another user")

    authority_client_id = context.get("authorityClientId")
    authority_client = state.get_client(authority_client_id, user_name=user_name)
    if authority_client is not None and authority_client.get("userName") != user_name:
        raise PermissionError("Playback context authority belongs to another user")


def _ensure_playback_context_active(context):
    if context.get("lifecycle") == "closed":
        raise PlaybackContextClosedError(context)


def _validate_playback_context_queue_payload(payload):
    queue_song_ids = payload.get("queueSongIds")
    if queue_song_ids is None:
        queue_song_ids = []
    if not isinstance(queue_song_ids, list):
        raise ValueError("queueSongIds must be a list")
    if not all(isinstance(song_id, str) and song_id for song_id in queue_song_ids):
        raise ValueError("queueSongIds must contain non-empty strings")

    current_index = payload.get("currentIndex", 0)
    position_ms = payload.get("positionMs", 0)
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

    return queue_song_ids, current_index, position_ms


def _normalize_v2_playback_update_payload(
    payload,
    playback_context=None,
    authoritative=False,
):
    normalized = dict(payload)
    if "state" in normalized:
        state_name = normalized.get("state")
        if not isinstance(state_name, str) or not state_name:
            raise ValueError("playback.update state must be a non-empty string")
    if "trackId" in normalized:
        track_id = normalized.get("trackId")
        if track_id is not None and (
            not isinstance(track_id, str) or not track_id
        ):
            raise ValueError("playback.update trackId must be null or a non-empty string")
    if "positionMs" in normalized and not _is_non_negative_int(
        normalized.get("positionMs")
    ):
        raise ValueError("playback.update positionMs must be a non-negative integer")
    if "currentIndex" in normalized:
        current_index = normalized.get("currentIndex")
        if not _is_int(current_index) or current_index < 0:
            raise ValueError("playback.update currentIndex must be a non-negative integer")
    if "queueSongIds" in normalized:
        queue_song_ids = normalized.get("queueSongIds")
        if not isinstance(queue_song_ids, list):
            raise ValueError("playback.update queueSongIds must be a list")
        if not all(
            isinstance(song_id, str) and song_id
            for song_id in queue_song_ids
        ):
            raise ValueError(
                "playback.update queueSongIds must contain non-empty strings"
            )

    if playback_context is None:
        queue_song_ids = normalized.get("queueSongIds")
        current_index = normalized.get("currentIndex")
        if queue_song_ids is not None and current_index is not None:
            if queue_song_ids and current_index >= len(queue_song_ids):
                raise ValueError("playback.update currentIndex is out of bounds")
            if not queue_song_ids and current_index != 0:
                raise ValueError("empty queue must use currentIndex=0")
        return normalized

    queue_song_ids = list(
        normalized.get(
            "queueSongIds",
            playback_context.get("queueSongIds") or [],
        )
    )
    current_index = normalized.get(
        "currentIndex",
        playback_context.get("currentIndex", 0),
    )
    if not _is_int(current_index) or current_index < 0:
        raise ValueError("playback.update currentIndex must be a non-negative integer")
    if queue_song_ids:
        if current_index >= len(queue_song_ids):
            raise ValueError("playback.update currentIndex is out of bounds")
        expected_track_id = queue_song_ids[current_index]
    else:
        if current_index != 0:
            raise ValueError("empty queue must use currentIndex=0")
        expected_track_id = None

    if authoritative:
        if "trackId" in normalized and normalized.get("trackId") != expected_track_id:
            raise ValueError("playback.update trackId does not match context queue")
        if "queueSongIds" in normalized or "currentIndex" in normalized:
            normalized.setdefault("trackId", expected_track_id)
    return normalized


def _build_playback_context_status_payload(playback_context):
    playback_context_id = playback_context.get("playbackContextId")
    persisted_device_states = getDevicePlaybackStates(playback_context_id)
    runtime_device_states = state.list_device_playback_states(playback_context_id)
    device_states = []
    device_states_by_client_id = {}
    for device_state in persisted_device_states + runtime_device_states:
        client_id = (
            device_state.get("clientId")
            or device_state.get("sourceClientId")
            or device_state.get("ownerClientId")
        )
        if not client_id:
            device_states.append(device_state)
            continue
        device_states_by_client_id[client_id] = device_state
    device_states.extend(device_states_by_client_id.values())
    serialized_device_states = [
        serializeDevicePlaybackStateV2(device_state)
        for device_state in device_states
    ]
    serialized_device_states = sorted(
        (
            device_state
            for device_state in serialized_device_states
            if device_state is not None
        ),
        key=lambda device_state: device_state["clientId"],
    )
    return {
        "playbackContext": serializePlaybackContextV2(playback_context),
        "deviceStates": serialized_device_states,
    }


def _push_playback_context_snapshot(sid, playback_context):
    _emit_message(
        _build_message(
            "state",
            "playback.context.status",
            _build_playback_context_status_payload(playback_context),
        ),
        sid,
    )


def _handle_playback_context_ensure(
    current_user_name,
    current_client,
    payload,
    request_id,
    sid,
):
    if current_client is None:
        raise PermissionError("Register the device before ensuring playback context")
    _reject_session_id_for_strict_v2(payload, strict_v2=True)
    device_session_id = _resolve_v2_device_session_id(
        payload,
        current_client,
        strict_v2=True,
    )
    if not isinstance(device_session_id, str) or not device_session_id:
        raise ValueError("playback.context.ensure requires a non-empty deviceSessionId")
    if not _has_role(current_client, "player"):
        raise PermissionError("Only a player can ensure a playback context")
    if not _client_supports(current_client, CAPABILITY_CAN_PLAY):
        raise CapabilityRequiredError("playback.context.ensure requires canPlay")
    if device_session_id != current_client.get("deviceSessionId"):
        raise PermissionError("deviceSessionId does not match the registered device")

    queue_song_ids, current_index, position_ms = _validate_playback_context_queue_payload(
        payload
    )
    ensure_result = ensureStrictPlaybackContextState(
        current_user_name,
        current_client.get("clientId"),
        device_session_id,
        queue_song_ids,
        current_index if queue_song_ids else None,
        position_ms,
        payload["state"],
    )
    playback_context, _mutated = ensure_result
    playback_context_id = playback_context["playbackContextId"]
    _ensure_playback_context_for_user(playback_context, current_user_name)
    playback_context = state.restore_playback_context(
        playback_context_id,
        playback_context,
    )
    state.subscribe_playback_context(sid, playback_context_id)
    _send_direct_response(
        "state",
        "playback.context.ensure",
        request_id,
        serializePlaybackContextV2(playback_context),
    )
    if ensure_result.binding_mutated:
        _run_post_commit_push(
            "playback.context.ensure",
            request_id,
            lambda: _broadcast_playback_context_bindings_changed(
                current_user_name,
                ensure_result.affected_authority_pairs,
            ),
        )
    return playback_context


def _handle_playback_context_list(
    current_user_name,
    current_client,
    payload,
    request_id,
):
    if current_client is None:
        raise PermissionError(
            "Register the device before listing playback contexts"
        )
    if not _client_supports(
        current_client,
        CAPABILITY_PLAYBACK_CONTEXT_V2,
    ):
        raise CapabilityRequiredError(
            "playbackContextV2 is required to list playback contexts"
        )
    if not _has_role(current_client, "controller"):
        raise PermissionError(
            "Only a controller can list playback contexts"
        )

    authority_client_id = payload["authorityClientId"]
    authority_device_session_id = payload[
        "authorityDeviceSessionId"
    ]
    bindings = listActivePlaybackContextBindings(
        current_user_name,
        authority_client_id,
        authority_device_session_id,
    )
    _send_direct_response(
        "state",
        "playback.context.list",
        request_id,
        {"contexts": bindings},
    )
    return bindings


def _handle_playback_context_status(current_user_name, current_client, payload, request_id):
    if current_client is None:
        raise PermissionError("Register the device before requesting playback context")
    _reject_session_id_for_strict_v2(payload, strict_v2=True)
    playback_context_id = _resolve_v2_playback_context_id(payload, strict_v2=True)
    if not isinstance(playback_context_id, str) or not playback_context_id:
        raise ValueError("playback.context.status requires a non-empty playbackContextId")

    playback_context = _get_existing_playback_context(playback_context_id)
    if (
        playback_context is None
        or playback_context.get("userName") != current_user_name
    ):
        raise PermissionError("Playback context access is not allowed")
    _ensure_playback_context_active(playback_context)
    _send_direct_response(
        "state",
        "playback.context.status",
        request_id,
        _build_playback_context_status_payload(playback_context),
    )
    return playback_context


def _handle_playback_context_subscribe(
    current_user_name,
    current_client,
    payload,
    request_id,
    sid,
):
    if current_client is None:
        raise PermissionError("Register the device before subscribing to playback context")
    _reject_session_id_for_strict_v2(payload, strict_v2=True)
    playback_context_id = _resolve_v2_playback_context_id(payload, strict_v2=True)
    if not isinstance(playback_context_id, str) or not playback_context_id:
        raise ValueError("playback.context.subscribe requires a non-empty playbackContextId")

    playback_context = _get_existing_playback_context(playback_context_id)
    if (
        playback_context is None
        or playback_context.get("userName") != current_user_name
    ):
        raise PermissionError("Playback context access is not allowed")
    _ensure_playback_context_active(playback_context)
    state.subscribe_playback_context(sid, playback_context_id)
    _send_ack(request_id)
    return playback_context


def _handle_playback_context_unsubscribe(
    current_user_name,
    current_client,
    payload,
    request_id,
    sid,
):
    if current_client is None:
        raise PermissionError("Register the device before unsubscribing from playback context")
    _reject_session_id_for_strict_v2(payload, strict_v2=True)
    playback_context_id = _resolve_v2_playback_context_id(payload, strict_v2=True)
    if not isinstance(playback_context_id, str) or not playback_context_id:
        raise ValueError("playback.context.unsubscribe requires a non-empty playbackContextId")

    playback_context = _get_existing_playback_context(playback_context_id)
    if playback_context is not None:
        _ensure_playback_context_for_user(playback_context, current_user_name)
        _ensure_playback_context_active(playback_context)
    state.unsubscribe_playback_context(sid, playback_context_id)
    _send_ack(request_id)
    return playback_context


def _handle_playback_context_close(current_user_name, current_client, payload, request_id):
    if current_client is None:
        raise PermissionError("Register the device before closing playback context")
    _reject_session_id_for_strict_v2(payload, strict_v2=True)
    playback_context_id = _resolve_v2_playback_context_id(payload, strict_v2=True)
    if not isinstance(playback_context_id, str) or not playback_context_id:
        raise ValueError("playback.context.close requires a non-empty playbackContextId")

    playback_context = _get_existing_playback_context(playback_context_id)
    if playback_context is None:
        raise LookupError("Playback context not found")
    _ensure_playback_context_for_user(playback_context, current_user_name)
    if (
        playback_context.get("authorityClientId") != current_client.get("clientId")
        and not _has_role(current_client, "controller")
    ):
        raise PermissionError("Only playback context authority or a controller can close context")

    active_handoffs = getActivePlaybackHandoffs(playback_context_id)
    closed_context = closeStrictPlaybackContextState(
        playback_context_id,
        current_user_name,
    )
    if closed_context is None:
        raise LookupError("Playback context not found")
    state.restore_playback_context(playback_context_id, closed_context)
    for active_handoff in active_handoffs:
        handoff = getPlaybackHandoff(active_handoff.get("handoffId"))
        if handoff is None:
            continue
        state.update_playback_handoff(
            handoff["handoffId"],
            status=handoff.get("status"),
            error_code=handoff.get("errorCode"),
            error_message=handoff.get("errorMessage"),
        )
    _send_ack(request_id)
    _run_post_commit_push(
        "playback.context.close",
        request_id,
        lambda: _broadcast_playback_context_closed_v2(
            current_user_name,
            playback_context_id,
        ),
    )
    if closed_context.mutated:
        _run_post_commit_push(
            "playback.context.close",
            request_id,
            lambda: _broadcast_playback_context_bindings_changed(
                current_user_name,
                closed_context.affected_authority_pairs,
            ),
        )
    state.stop_follow_relationships_for_context(playback_context_id)
    state.clear_playback_context_subscriptions(playback_context_id)
    return closed_context


def _handle_queue_context_sync(current_user_name, current_client, payload, request_id):
    if current_client is None:
        raise PermissionError("Register the device before syncing context queue")
    _reject_session_id_for_strict_v2(payload, strict_v2=True)
    playback_context_id = _resolve_v2_playback_context_id(payload, strict_v2=True)
    if not isinstance(playback_context_id, str) or not playback_context_id:
        raise ValueError("queue.context.sync requires a non-empty playbackContextId")
    device_session_id = _resolve_v2_device_session_id(
        payload,
        current_client,
        strict_v2=True,
    )
    if not isinstance(device_session_id, str) or not device_session_id:
        raise ValueError("queue.context.sync requires a non-empty deviceSessionId")

    playback_context = _get_existing_playback_context(playback_context_id)
    if playback_context is None:
        raise LookupError("Playback context not found")
    _ensure_playback_context_for_user(playback_context, current_user_name)
    _ensure_playback_context_active(playback_context)
    if playback_context.get("authorityClientId") != current_client.get("clientId"):
        raise PermissionError("Playback context authority mismatch")
    if (
        device_session_id != current_client.get("deviceSessionId")
        or playback_context.get("authorityDeviceSessionId") != device_session_id
    ):
        raise PermissionError("Playback context authority device mismatch")

    queue_song_ids, current_index, position_ms = _validate_playback_context_queue_payload(
        payload
    )
    updated_context = mutateStrictPlaybackContextQueue(
        playback_context_id,
        current_user_name,
        current_client.get("clientId"),
        device_session_id,
        queue_song_ids,
        current_index,
        position_ms,
        _get_base_queue_revision(payload),
        payload.get("baseControlVersion"),
    )
    if updated_context is None:
        raise LookupError("Playback context not found")

    state.restore_playback_context(playback_context_id, updated_context)
    _send_ack(request_id)
    _run_post_commit_push(
        "queue.context.sync",
        request_id,
        lambda: _broadcast_context_queue_v2(
            current_user_name,
            playback_context_id,
        ),
    )
    return updated_context


def _ensure_handoff_for_user(handoff, user_name):
    handoff_user_name = handoff.get("userName")
    if handoff_user_name is not None and handoff_user_name != user_name:
        raise PermissionError("Playback handoff belongs to another user")


def _send_handoff_start_ack(request_id, handoff, duplicate=False):
    status = handoff.get("status") or "preparing"
    payload = {
        "handoffId": handoff.get("handoffId"),
        "prepareId": handoff.get("prepareId"),
        "controlVersion": handoff.get("controlVersion"),
        "status": status,
    }
    _send_ack(request_id, payload)


def _build_handoff_status_payload(handoff):
    status = handoff.get("status")
    status = {
        "aborted": "failed",
        "canceled": "cancelled",
        "committed": "committing",
        "superseded": "failed",
        "timed_out": "timedOut",
    }.get(status, status)
    payload = {
        "playbackContextId": handoff.get("playbackContextId"),
        "handoffId": handoff.get("handoffId"),
        "status": status,
        "controlVersion": handoff.get("controlVersion"),
    }
    source_client_id = handoff.get("sourceClientId")
    if source_client_id:
        payload["sourceClientId"] = source_client_id
    if status == "completed":
        payload["newAuthorityClientId"] = handoff.get("targetClientId")
    elif status in {"failed", "timedOut"}:
        payload["errorCode"] = handoff.get("errorCode") or "prepare_failed"
        if handoff.get("errorMessage"):
            payload["errorMessage"] = handoff["errorMessage"]
    return payload


def _broadcast_handoff_status(handoff, include_sid=None):
    user_name = handoff.get("userName")
    playback_context_id = handoff.get("playbackContextId")
    message = _build_handoff_status_message(handoff)
    target_sids = set(
        state.list_playback_context_subscribers(
            playback_context_id,
            user_name=user_name,
        )
    )
    target_sids.update(
        state.list_context_participant_sids(
            playback_context_id,
            user_name=user_name,
        )
    )
    if include_sid is not None:
        target_sids.add(include_sid)
    for target_sid in target_sids:
        _emit_message(message, target_sid)
    return message


def _build_handoff_status_message(handoff):
    return _build_message(
        "state",
        "playback.handoff.status",
        _build_handoff_status_payload(handoff),
    )


def _broadcast_handoff_cancel(
    handoff: Dict[str, object],
    reason: str,
    include_sid: Optional[str] = None,
) -> Dict[str, object]:
    payload = {
        "playbackContextId": handoff.get("playbackContextId"),
        "handoffId": handoff.get("handoffId"),
        "reason": reason,
        "controlVersion": handoff.get("controlVersion"),
    }
    if handoff.get("errorCode"):
        payload["errorCode"] = handoff["errorCode"]
    if handoff.get("errorMessage"):
        payload["errorMessage"] = handoff["errorMessage"]
    message = _build_message(
        "command",
        "playback.handoff.cancel",
        payload,
    )
    target_sids = set(
        state.list_playback_context_subscribers(
            handoff.get("playbackContextId"),
            user_name=handoff.get("userName"),
        )
    )
    for client_id in (
        handoff.get("sourceClientId"),
        handoff.get("targetClientId"),
        handoff.get("originClientId"),
    ):
        target_sid = state.get_sid_for_client(
            client_id,
            user_name=handoff.get("userName"),
        )
        if target_sid is not None:
            target_sids.add(target_sid)
    if include_sid is not None:
        target_sids.add(include_sid)
    for target_sid in target_sids:
        _emit_message(message, target_sid)
    return message


def _handoff_expiry_ms(handoff):
    snapshot = handoff.get("snapshot") or {}
    status = handoff.get("status")
    if status == "preparing":
        expires_at_ms = handoff.get("prepareExpiresAtMs")
        if expires_at_ms is None:
            expires_at_ms = snapshot.get("prepareExpiresAtMs")
        timeout_ms = HANDOFF_PREPARE_TIMEOUT_MS
    elif status in ("ready", "committed"):
        expires_at_ms = handoff.get("completeExpiresAtMs")
        if expires_at_ms is None:
            expires_at_ms = snapshot.get("completeExpiresAtMs")
        timeout_ms = HANDOFF_COMPLETE_TIMEOUT_MS
    else:
        return None
    if isinstance(expires_at_ms, (int, float)):
        return int(expires_at_ms)

    updated_at_ms = handoff.get("updatedAtMs")
    if not isinstance(updated_at_ms, (int, float)):
        updated_at = handoff.get("updatedAt") or handoff.get("createdAt")
        if isinstance(updated_at, (int, float)):
            updated_at_ms = updated_at * 1000
    if not isinstance(updated_at_ms, (int, float)):
        return None
    return int(updated_at_ms + timeout_ms)


def _expire_stale_handoff(handoff, now_ms=None):
    expires_at_ms = _handoff_expiry_ms(handoff)
    now_ms = _server_time_ms() if now_ms is None else now_ms
    if expires_at_ms is None or now_ms < expires_at_ms:
        return None

    handoff_id = handoff.get("handoffId")
    error_code = (
        "prepare_timeout"
        if handoff.get("status") == "preparing"
        else "commit_timeout"
    )
    error_message = (
        "Handoff prepare timed out"
        if handoff.get("status") == "preparing"
        else "Handoff commit timed out"
    )
    expired = state.update_playback_handoff(
        handoff_id,
        status="timed_out",
        error_code=error_code,
        error_message=error_message,
    )
    if expired is None:
        expired = dict(handoff)
        expired["status"] = "timed_out"
        expired["errorCode"] = error_code
        expired["errorMessage"] = error_message
    prepare_id = handoff.get("prepareId")
    if prepare_id:
        state.finish_prepare_if_preparing(prepare_id, "timed_out")
    savePlaybackHandoff(expired)
    _send_handoff_release(
        expired,
        expired.get("targetClientId"),
        "timed_out",
        request_id=expired.get("requestId"),
        authority_client_id=expired.get("sourceClientId"),
        source_client_id=expired.get("sourceClientId"),
    )
    return expired


def _require_online_handoff_target(handoff):
    target_client_id = handoff.get("targetClientId")
    target_client = state.get_client(
        target_client_id,
        user_name=handoff.get("userName"),
    )
    target_sid = state.get_sid_for_client(
        target_client_id,
        user_name=handoff.get("userName"),
    )
    if target_client is None and state.get_client(target_client_id) is not None:
        raise PermissionError("Cross-user handoff is not allowed")
    if target_client is None or target_sid is None:
        raise LookupError("Handoff target client is offline")
    if target_client.get("userName") != handoff.get("userName"):
        raise PermissionError("Cross-user handoff is not allowed")
    if not _has_role(target_client, "player"):
        raise PermissionError("Handoff target must be a player")
    if strict_v2:
        if not (
            _client_supports(target_client, CAPABILITY_EFFECTIVE_AT)
            and _client_supports(target_client, CAPABILITY_PLAYBACK_PREPARE)
            and _client_supports(target_client, CAPABILITY_CAN_PLAY)
        ):
            raise CapabilityRequiredError(
                "Handoff target requires playbackPrepare, effectiveAtPlayback, and canPlay"
            )
    elif not (
        _client_supports(target_client, CAPABILITY_EFFECTIVE_AT)
        and _client_supports(target_client, CAPABILITY_PLAYBACK_PREPARE)
    ):
        raise PermissionError(
            "Handoff target must support playbackPrepare and effectiveAtPlayback"
        )
    return target_client


def _rebuild_handoff_prepare_if_missing(handoff, context, request_sid):
    prepare_id = handoff.get("prepareId")
    if (
        handoff.get("status") != "preparing"
        or not prepare_id
        or state.get_prepare(prepare_id) is not None
    ):
        return handoff

    target_client_id = handoff.get("targetClientId")
    target_client = _require_online_handoff_target(handoff)

    playback_context_id = handoff.get("playbackContextId")
    source_client_id = handoff.get("sourceClientId")
    target_device_session_id = _device_session_id(target_client)
    origin_client_id = handoff.get("originClientId") or source_client_id
    base_control_version = handoff.get("baseControlVersion")
    if base_control_version is None:
        base_control_version = context.get("controlVersion", 0)
    control_version = handoff.get("controlVersion")
    if control_version is None:
        control_version = (handoff.get("snapshot") or {}).get("handoffControlVersion")
    if control_version is None:
        control_version = context.get("controlVersion", 0) + 1

    now_ms = _server_time_ms()
    expires_at_ms = now_ms + HANDOFF_PREPARE_TIMEOUT_MS
    snapshot = dict(handoff.get("snapshot") or context)
    snapshot["handoffControlVersion"] = control_version
    snapshot["prepareId"] = prepare_id
    snapshot["prepareExpiresAtMs"] = expires_at_ms
    snapshot["targetDeviceSessionId"] = target_device_session_id
    snapshot["targetSid"] = state.get_sid_for_client(
        target_client_id,
        user_name=handoff.get("userName"),
    )
    if state.get_playback_handoff(handoff.get("handoffId")) is None:
        handoff = state.create_playback_handoff(
            handoff.get("handoffId"),
            handoff.get("requestId"),
            playback_context_id,
            handoff.get("userName"),
            source_client_id,
            target_client_id,
            base_control_version,
            control_version,
            snapshot,
            prepare_id=prepare_id,
            origin_client_id=origin_client_id,
        )

    handoff = dict(handoff)
    handoff["snapshot"] = snapshot
    savePlaybackHandoff(handoff)
    commit_payload = {
        "userName": handoff.get("userName"),
        "handoffId": handoff.get("handoffId"),
        "playbackContextId": playback_context_id,
        "sourceClientId": source_client_id,
        "targetClientId": target_client_id,
        "originClientId": origin_client_id,
        "targetDeviceSessionId": target_device_session_id,
        "targetSid": snapshot.get("targetSid"),
        "timelineId": context.get("timelineId") or f"playback:{playback_context_id}",
        "queueSongIds": list(context.get("queueSongIds") or []),
        "currentIndex": context.get("currentIndex", 0),
        "trackId": context.get("trackId"),
        "positionMs": context.get("positionMs", 0),
        "state": context.get("state") or "stopped",
        "queueRevision": context.get("queueRevision", 0),
        "baseControlVersion": base_control_version,
        "controlVersion": control_version,
    }
    prepare = state.create_prepare(
        prepare_id,
        "playback.handoff.start",
        commit_payload["timelineId"],
        [target_client_id],
        [target_client_id],
        control_version,
        commit_payload,
        now_ms,
        expires_at_ms,
        request_sid=request_sid,
        request_id=handoff.get("requestId"),
    )
    prepare_payload = {
        "prepareId": prepare_id,
        "handoffId": handoff.get("handoffId"),
        "playbackContextId": playback_context_id,
        "deviceSessionId": target_device_session_id,
        "sourceClientId": source_client_id,
        "authorityClientId": source_client_id,
        "queueSongIds": list(context.get("queueSongIds") or []),
        "currentIndex": context.get("currentIndex", 0),
        "trackId": context.get("trackId"),
        "positionMs": context.get("positionMs", 0),
        "controlVersion": control_version,
    }
    if context.get("timelineId"):
        prepare_payload["timelineId"] = context["timelineId"]
    _send_playback_prepare(prepare, prepare_payload)
    socketio.start_background_task(_expire_prepare_later, prepare_id)
    return handoff


def _restore_ready_handoff_if_missing(handoff, context):
    if (
        handoff.get("status") != "ready"
        or state.get_playback_handoff(handoff.get("handoffId")) is not None
    ):
        return handoff

    target_client = _require_online_handoff_target(handoff)
    snapshot = dict(handoff.get("snapshot") or context)
    control_version = handoff.get("controlVersion")
    if control_version is None:
        control_version = snapshot.get("handoffControlVersion")
    if control_version is None:
        control_version = context.get("controlVersion", 0) + 1
    base_control_version = handoff.get("baseControlVersion")
    if base_control_version is None:
        base_control_version = context.get("controlVersion", 0)

    restored = state.create_playback_handoff(
        handoff.get("handoffId"),
        handoff.get("requestId"),
        handoff.get("playbackContextId"),
        handoff.get("userName"),
        handoff.get("sourceClientId"),
        handoff.get("targetClientId"),
        base_control_version,
        control_version,
        snapshot,
        prepare_id=handoff.get("prepareId"),
        origin_client_id=handoff.get("originClientId"),
    )
    complete_expires_at_ms = _server_time_ms() + HANDOFF_COMPLETE_TIMEOUT_MS
    restored = state.update_playback_handoff(
        restored.get("handoffId"),
        status="ready",
        complete_expires_at_ms=complete_expires_at_ms,
    )
    savePlaybackHandoff(restored)
    effective_at_server_ms = _effective_at_server_ms(PROTOCOL_TWO_PHASE)
    target_device_session_id = _device_session_id(target_client)
    _send_target_player_play(
        restored.get("targetClientId"),
        restored.get("sourceClientId"),
        restored.get("requestId"),
        target_device_session_id,
        effective_at_server_ms,
        control_version,
        extra_payload={
            "playbackContextId": restored.get("playbackContextId"),
            "deviceSessionId": target_device_session_id,
            "handoffId": restored.get("handoffId"),
            "trackId": context.get("trackId"),
            "positionMs": context.get("positionMs", 0),
            "state": context.get("state") or "playing",
            "completeExpiresAtServerMs": complete_expires_at_ms,
        },
        user_name=restored.get("userName"),
    )
    socketio.start_background_task(
        _expire_handoff_complete_later,
        restored.get("handoffId"),
    )
    return restored


def _send_handoff_release(
    handoff,
    target_client_id,
    reason,
    request_id=None,
    authority_client_id=None,
    source_client_id=None,
):
    if not isinstance(target_client_id, str) or not target_client_id:
        return False
    target_sid = state.get_sid_for_client(
        target_client_id,
        user_name=handoff.get("userName"),
    )
    if target_sid is None:
        return False
    playback_context_id = handoff.get("playbackContextId")
    authority_client_id = authority_client_id or handoff.get("sourceClientId")
    target_client = state.get_client(
        target_client_id,
        user_name=handoff.get("userName"),
    )
    if (
        _is_strict_playback_context_v2(target_client)
        and reason == "handoff_completed"
    ):
        _emit_message(
            _build_message(
                "command",
                "playback.handoff.release",
                {
                    "playbackContextId": playback_context_id,
                    "handoffId": handoff.get("handoffId"),
                    "instruction": "pause",
                    "controlVersion": handoff.get("controlVersion"),
                    "newAuthorityClientId": authority_client_id,
                },
            ),
            target_sid,
        )
        return True
    _emit_message(
        _build_message(
            "command",
            "playback.handoff.release",
            {
                "playbackContextId": playback_context_id,
                "handoffId": handoff.get("handoffId"),
                "authorityClientId": authority_client_id,
                "reason": reason,
            },
            requestId=request_id,
            sourceClientId=source_client_id or authority_client_id,
            targetClientId=target_client_id,
        ),
        target_sid,
    )
    return True


def _handle_handoff_start(current_user_name, current_client, payload, request_id, request_sid):
    if current_client is None:
        raise PermissionError("Register the device before starting handoff")

    strict_v2 = _is_strict_playback_context_v2(current_client)
    use_v2_context = strict_v2 or _is_context_payload(payload)
    if use_v2_context:
        playback_context_id = _resolve_v2_playback_context_id(
            payload,
            strict_v2=strict_v2,
        )
    else:
        playback_context_id = _resolve_playback_context_id(payload, current_client)
    if not isinstance(playback_context_id, str) or not playback_context_id:
        raise ValueError("playback.handoff.start requires a non-empty playbackContextId")

    context = (
        _get_existing_playback_context(playback_context_id)
        if use_v2_context
        else _get_or_restore_playback_context(playback_context_id)
    )
    if context is None:
        raise LookupError("Playback context not found")
    _ensure_playback_context_for_user(context, current_user_name)
    if strict_v2:
        _ensure_playback_context_active(context)
        if not _has_role(current_client, "controller"):
            raise PermissionError("Playback handoff requires the controller role")

    origin_client_id = current_client.get("clientId")
    requested_source_client_id = payload.get("sourceClientId")
    source_client_id = requested_source_client_id or context.get("authorityClientId")
    target_client_id = payload.get("targetClientId")
    if not isinstance(source_client_id, str) or not source_client_id:
        raise ValueError("playback.handoff.start requires a non-empty sourceClientId")
    if not isinstance(target_client_id, str) or not target_client_id:
        raise ValueError("playback.handoff.start requires a non-empty targetClientId")
    if source_client_id == target_client_id:
        raise ValueError("playback.handoff.start source and target must be different")

    existing_handoff = (
        state.get_playback_handoff_by_request(
            current_user_name,
            origin_client_id,
            request_id,
        )
        or getPlaybackHandoffByRequest(
            current_user_name,
            origin_client_id,
            request_id,
        )
    )
    if existing_handoff is not None:
        _ensure_handoff_for_user(existing_handoff, current_user_name)
        if existing_handoff.get("playbackContextId") != playback_context_id:
            raise ControlConflictError(
                "Playback handoff requestId already belongs to another context",
                current_control_version=context.get("controlVersion", 0),
            )
        if (
            requested_source_client_id is not None
            and existing_handoff.get("sourceClientId") != requested_source_client_id
        ):
            raise ControlConflictError(
                "Playback handoff requestId already belongs to another source",
                current_control_version=context.get("controlVersion", 0),
            )
        if existing_handoff.get("targetClientId") != target_client_id:
            raise ControlConflictError(
                "Playback handoff requestId already belongs to another target",
                current_control_version=context.get("controlVersion", 0),
            )
        expired_handoff = _expire_stale_handoff(existing_handoff)
        if expired_handoff is not None:
            existing_handoff = expired_handoff
        else:
            try:
                existing_handoff = _rebuild_handoff_prepare_if_missing(
                    existing_handoff,
                    context,
                    request_sid,
                )
                existing_handoff = _restore_ready_handoff_if_missing(
                    existing_handoff,
                    context,
                )
            except PlaybackAuthorityMismatchError:
                raise ControlConflictError(
                    "Playback handoff already in progress",
                    current_control_version=context.get("controlVersion", 0),
                )
        _send_handoff_start_ack(request_id, existing_handoff, duplicate=True)
        return existing_handoff

    for active_handoff in getActivePlaybackHandoffs(playback_context_id):
        _ensure_handoff_for_user(active_handoff, current_user_name)
        if _expire_stale_handoff(active_handoff) is not None:
            continue
        if strict_v2 and (
            active_handoff.get("sourceClientId") == source_client_id
            and active_handoff.get("targetClientId") == target_client_id
            and active_handoff.get("originClientId") == origin_client_id
        ):
            active_handoff = _rebuild_handoff_prepare_if_missing(
                active_handoff,
                context,
                request_sid,
            )
            _send_handoff_start_ack(request_id, active_handoff, duplicate=True)
            return active_handoff
        raise ControlConflictError(
            "Playback handoff already in progress",
            current_control_version=context.get("controlVersion", 0),
        )

    if context.get("authorityClientId") != source_client_id:
        raise PermissionError("sourceClientId must be the current authority")
    if (
        current_client.get("clientId") != source_client_id
        and not _has_role(current_client, "controller")
    ):
        raise PermissionError("Only handoff source or a controller can start handoff")

    source_client = state.get_client(
        source_client_id,
        user_name=current_user_name,
    )
    source_sid = state.get_sid_for_client(
        source_client_id,
        user_name=current_user_name,
    )
    if source_client is not None and source_client.get("userName") != current_user_name:
        raise PermissionError("Handoff source belongs to another user")
    if strict_v2:
        if source_client is None or source_sid is None:
            raise PlaybackAuthorityOfflineError("Playback context authority is offline")
        if not _has_role(source_client, "player"):
            raise PermissionError("Playback context authority must be a player")
        if not _client_supports(source_client, CAPABILITY_CAN_PAUSE):
            raise CapabilityRequiredError("Handoff source requires canPause")
        if (
            source_client.get("deviceSessionId")
            != context.get("authorityDeviceSessionId")
        ):
            raise PlaybackAuthorityOfflineError(
                "Playback context authority device is unavailable"
            )

    target_client = state.get_client(
        target_client_id,
        user_name=current_user_name,
    )
    target_sid = state.get_sid_for_client(
        target_client_id,
        user_name=current_user_name,
    )
    if target_client is None and state.get_client(target_client_id) is not None:
        raise PermissionError("Cross-user handoff is not allowed")
    if target_client is None or target_sid is None:
        raise LookupError("Handoff target client is offline")
    if target_client.get("userName") != current_user_name:
        raise PermissionError("Cross-user handoff is not allowed")
    if not _has_role(target_client, "player"):
        raise PermissionError("Handoff target must be a player")
    if _is_strict_playback_context_v2(target_client):
        if not (
            _client_supports(target_client, CAPABILITY_EFFECTIVE_AT)
            and _client_supports(target_client, CAPABILITY_PLAYBACK_PREPARE)
            and _client_supports(target_client, CAPABILITY_CAN_PLAY)
        ):
            raise CapabilityRequiredError(
                "Handoff target requires playbackPrepare, effectiveAtPlayback, and canPlay"
            )
    elif not (
        _client_supports(target_client, CAPABILITY_EFFECTIVE_AT)
        and _client_supports(target_client, CAPABILITY_PLAYBACK_PREPARE)
    ):
        raise PermissionError(
            "Handoff target must support playbackPrepare and effectiveAtPlayback"
        )

    base_control_version = payload.get("baseControlVersion")
    if base_control_version is None:
        base_control_version = context.get("controlVersion", 0)
    if base_control_version != context.get("controlVersion", 0):
        raise ControlConflictError(
            "Playback control version conflict",
            current_control_version=context.get("controlVersion", 0),
        )

    handoff_id = payload.get("handoffId") or _new_handoff_id()
    if not isinstance(handoff_id, str) or not handoff_id:
        raise ValueError("handoffId must be a non-empty string")
    control_version = context.get("controlVersion", 0) + 1
    prepare_id = _new_prepare_id()
    now_ms = _server_time_ms()
    prepare_expires_at_ms = now_ms + HANDOFF_PREPARE_TIMEOUT_MS
    snapshot = dict(context)
    snapshot["handoffControlVersion"] = control_version
    snapshot["prepareId"] = prepare_id
    snapshot["prepareExpiresAtMs"] = prepare_expires_at_ms
    snapshot["targetDeviceSessionId"] = _device_session_id(target_client)
    snapshot["targetSid"] = target_sid
    try:
        handoff = state.create_playback_handoff(
            handoff_id,
            request_id,
            playback_context_id,
            current_user_name,
            source_client_id,
            target_client_id,
            base_control_version,
            control_version,
            snapshot,
            prepare_id=prepare_id,
            origin_client_id=origin_client_id,
        )
    except PlaybackAuthorityMismatchError:
        raise ControlConflictError(
            "Playback handoff already in progress",
            current_control_version=context.get("controlVersion", 0),
        )
    savePlaybackHandoff(handoff)

    target_device_session_id = _device_session_id(target_client)
    commit_payload = {
        "userName": current_user_name,
        "handoffId": handoff["handoffId"],
        "playbackContextId": playback_context_id,
        "sourceClientId": source_client_id,
        "targetClientId": target_client_id,
        "originClientId": origin_client_id,
        "targetDeviceSessionId": target_device_session_id,
        "targetSid": target_sid,
        "timelineId": context.get("timelineId") or f"playback:{playback_context_id}",
        "queueSongIds": list(context.get("queueSongIds") or []),
        "currentIndex": context.get("currentIndex", 0),
        "trackId": context.get("trackId"),
        "positionMs": context.get("positionMs", 0),
        "state": context.get("state") or "stopped",
        "queueRevision": context.get("queueRevision", 0),
        "baseControlVersion": base_control_version,
        "controlVersion": control_version,
    }
    prepare = state.create_prepare(
        prepare_id,
        "playback.handoff.start",
        commit_payload["timelineId"],
        [target_client_id],
        [target_client_id],
        control_version,
        commit_payload,
        now_ms,
        prepare_expires_at_ms,
        request_sid=request_sid,
        request_id=request_id,
    )
    prepare_payload = {
        "prepareId": prepare_id,
        "handoffId": handoff["handoffId"],
        "playbackContextId": playback_context_id,
        "deviceSessionId": target_device_session_id,
        "sourceClientId": source_client_id,
        "authorityClientId": source_client_id,
        "queueSongIds": list(context.get("queueSongIds") or []),
        "currentIndex": context.get("currentIndex", 0),
        "trackId": context.get("trackId"),
        "positionMs": context.get("positionMs", 0),
        "controlVersion": control_version,
    }
    if context.get("timelineId"):
        prepare_payload["timelineId"] = context["timelineId"]
    _send_handoff_start_ack(request_id, handoff)
    _run_post_commit_push(
        "playback.handoff.start",
        request_id,
        lambda: _send_playback_prepare(prepare, prepare_payload),
    )
    _run_post_commit_push(
        "playback.handoff.start",
        request_id,
        lambda: socketio.start_background_task(
            _expire_prepare_later,
            prepare_id,
        ),
    )
    return handoff


def _handle_handoff_complete(
    current_user_name: str,
    current_client: Optional[Dict[str, object]],
    payload: Dict[str, object],
    request_id: str,
    request_sid: Optional[str] = None,
) -> Dict[str, object]:
    if current_client is None:
        raise PermissionError("Register the device before completing handoff")
    _reject_session_id_for_strict_v2(
        payload,
        _is_strict_playback_context_v2(current_client),
    )

    handoff_id = payload.get("handoffId")
    if not isinstance(handoff_id, str) or not handoff_id:
        raise ValueError("playback.handoff.complete requires a non-empty handoffId")
    handoff = state.get_playback_handoff(handoff_id) or getPlaybackHandoff(handoff_id)
    if handoff is None:
        raise LookupError("Playback handoff not found")
    _ensure_handoff_for_user(handoff, current_user_name)
    if handoff.get("targetClientId") != current_client.get("clientId"):
        raise PermissionError("playback.handoff.complete sender must be targetClientId")

    strict_v2 = _is_strict_playback_context_v2(current_client)
    if strict_v2:
        playback_context_id = payload.get("playbackContextId")
        if playback_context_id != handoff.get("playbackContextId"):
            raise ValueError("playback.handoff.complete playbackContextId does not match")
        playback_context = _get_or_restore_playback_context(playback_context_id)
        if playback_context is None:
            raise LookupError("Playback context not found")
        _ensure_playback_context_for_user(playback_context, current_user_name)
        _ensure_playback_context_active(playback_context)
        snapshot = handoff.get("snapshot") or {}
        target_device_session_id = current_client.get("deviceSessionId")
        if target_device_session_id != snapshot.get("targetDeviceSessionId"):
            raise PermissionError(
                "playback.handoff.complete device does not match handoff target"
            )
        target_sid = snapshot.get("targetSid")
        if target_sid is not None and request_sid != target_sid:
            raise PermissionError(
                "playback.handoff.complete socket does not match handoff target"
            )
        if handoff.get("status") not in (
            "committed",
            "committing",
            "completed",
        ):
            raise ControlConflictError(
                "Playback handoff is not committing",
                current_control_version=handoff.get("controlVersion"),
            )
        try:
            result = completeStrictPlaybackHandoff(
                playback_context_id,
                handoff_id,
                current_user_name,
                current_client.get("clientId"),
                target_device_session_id,
                position_ms=payload.get("positionMs"),
            )
        except ValueError as exc:
            if str(exc) == "Playback handoff is not committing":
                raise ControlConflictError(
                    str(exc),
                    current_control_version=handoff.get("controlVersion"),
                )
            raise
        if result is None:
            raise LookupError("Playback handoff not found")
        updated_context, completed_handoff, device_state, _mutated = result
        if result.mutated:
            completed_handoff["_bindingMutation"] = {
                "mutated": result.mutated,
                "affectedAuthorityPairs": result.affected_authority_pairs,
            }
        state.restore_playback_context(playback_context_id, updated_context)
        if device_state is not None:
            state.record_device_playback_state(
                playback_context_id,
                target_device_session_id,
                current_client.get("clientId"),
                current_user_name,
                device_state,
                is_authority=True,
                mode="handoff",
            )
        state.update_playback_handoff(handoff_id, status="completed")
        return completed_handoff

    playback_context_id = payload.get("playbackContextId") or handoff.get("playbackContextId")
    if playback_context_id != handoff.get("playbackContextId"):
        raise ValueError("playback.handoff.complete playbackContextId does not match")
    expected_control_version = handoff.get("controlVersion")
    if expected_control_version is None:
        expected_control_version = (handoff.get("snapshot") or {}).get("handoffControlVersion")
    control_version = payload.get("controlVersion", expected_control_version)
    if control_version != expected_control_version:
        raise ControlConflictError(
            "Playback control version conflict",
            current_control_version=expected_control_version,
        )

    context = _get_or_restore_playback_context(playback_context_id)
    if context is None:
        raise LookupError("Playback context not found")
    _ensure_playback_context_for_user(context, current_user_name)

    if handoff.get("status") == "ready":
        expired_handoff = _expire_handoff_complete(handoff_id)
        if expired_handoff is not None:
            handoff = expired_handoff
    if handoff.get("status") == "completed":
        _send_ack(
            request_id,
            {
                "completed": True,
                "duplicate": True,
                "handoffId": handoff_id,
                "playbackContextId": playback_context_id,
                "authorityClientId": handoff.get("targetClientId"),
            },
        )
        return handoff
    if handoff.get("status") != "ready":
        raise ControlConflictError(
            "Playback handoff is not ready",
            current_control_version=expected_control_version,
        )

    playback_payload = dict(payload)
    playback_payload.setdefault("state", context.get("state") or "playing")
    playback_payload.setdefault("trackId", context.get("trackId"))
    playback_payload.setdefault("positionMs", context.get("positionMs", 0))
    playback_payload.setdefault("queueSongIds", context.get("queueSongIds") or [])
    playback_payload.setdefault("currentIndex", context.get("currentIndex", 0))

    try:
        updated_context = state.transfer_playback_authority(
            playback_context_id,
            handoff.get("sourceClientId"),
            handoff.get("targetClientId"),
            expected_control_version=handoff.get("baseControlVersion"),
            next_control_version=control_version,
            playback_state=playback_payload,
            origin_client_id=handoff.get("originClientId"),
        )
    except PlaybackAuthorityMismatchError as exc:
        raise PermissionError(
            f"Current authority is {exc.current_authority_client_id}"
        )
    except PlaybackControlVersionMismatchError as exc:
        raise ControlConflictError(
            "Playback control version conflict",
            current_control_version=exc.current_control_version,
        )
    if updated_context is None:
        raise LookupError("Playback context not found")

    target_device_state = state.record_device_playback_state(
        playback_context_id,
        _device_session_id(current_client),
        current_client.get("clientId"),
        current_user_name,
        playback_payload,
        is_authority=True,
        mode="handoff",
    )
    updated_handoff = state.update_playback_handoff(handoff_id, status="completed")
    if updated_handoff is None:
        handoff = dict(handoff)
        handoff["status"] = "completed"
    else:
        handoff = updated_handoff
    savePlaybackHandoff(handoff)
    _update_playback_context_snapshot(current_user_name, updated_context)
    _save_device_playback_state_snapshot(current_user_name, target_device_state)

    _send_handoff_release(
        handoff,
        handoff.get("sourceClientId"),
        "handoff_completed",
        request_id=f"{request_id}-release" if request_id else None,
        authority_client_id=handoff.get("targetClientId"),
        source_client_id=handoff.get("targetClientId"),
    )

    _send_ack(
        request_id,
        {
            "completed": True,
            "handoffId": handoff_id,
            "playbackContextId": playback_context_id,
            "authorityClientId": handoff.get("targetClientId"),
            "playback": updated_context,
        },
    )
    _broadcast_playback_context_state_v2(current_user_name, playback_context_id)
    return handoff


def _handle_handoff_cancel(current_user_name, current_client, payload, request_id):
    if current_client is None:
        raise PermissionError("Register the device before canceling handoff")
    _reject_session_id_for_strict_v2(
        payload,
        _is_strict_playback_context_v2(current_client),
    )
    handoff_id = payload.get("handoffId")
    if not isinstance(handoff_id, str) or not handoff_id:
        raise ValueError("playback.handoff.cancel requires a non-empty handoffId")
    handoff = state.get_playback_handoff(handoff_id) or getPlaybackHandoff(handoff_id)
    if handoff is None:
        raise LookupError("Playback handoff not found")
    _ensure_handoff_for_user(handoff, current_user_name)
    strict_v2 = _is_strict_playback_context_v2(current_client)
    if strict_v2:
        if payload.get("playbackContextId") != handoff.get("playbackContextId"):
            raise ValueError("playback.handoff.cancel playbackContextId does not match")
        status = handoff.get("status")
        if status == "cancelled":
            _send_ack(request_id)
            return handoff
        if status in {"completed", "failed", "timed_out"}:
            raise ControlConflictError(
                "Playback handoff is already terminal",
                current_control_version=handoff.get("controlVersion"),
            )
        if current_client.get("clientId") not in (
            handoff.get("sourceClientId"),
            handoff.get("targetClientId"),
            handoff.get("originClientId"),
        ):
            raise PermissionError("Only handoff members can cancel handoff")
        terminal_result = terminateStrictPlaybackHandoff(
            handoff.get("playbackContextId"),
            handoff_id,
            current_user_name,
            "cancelled",
        )
        if terminal_result is None:
            raise LookupError("Playback handoff not found")
        handoff, transitioned = terminal_result
        if handoff.get("status") != "cancelled":
            raise ControlConflictError(
                "Playback handoff is already terminal",
                current_control_version=handoff.get("controlVersion"),
            )
        state.update_playback_handoff(handoff_id, status="cancelled")
        if handoff.get("prepareId"):
            state.finish_prepare_if_preparing(
                handoff["prepareId"],
                "cancelled",
            )
        _send_ack(request_id)
        if not transitioned:
            return handoff
        reason = payload.get("reason") or "cancelled"
        _run_post_commit_push(
            "playback.handoff.cancel",
            request_id,
            lambda: _broadcast_handoff_cancel(
                handoff,
                reason,
                include_sid=request.sid,
            ),
        )
        _run_post_commit_push(
            "playback.handoff.cancel",
            request_id,
            lambda: _broadcast_handoff_status(
                handoff,
                include_sid=request.sid,
            ),
        )
        return handoff
    if handoff.get("status") == "ready":
        expired_handoff = _expire_handoff_complete(handoff_id)
        if expired_handoff is not None:
            handoff = expired_handoff
    if handoff.get("status") == "completed":
        _send_ack(request_id, {"ignored": True, "handoffId": handoff_id, "status": "completed"})
        return handoff
    if handoff.get("status") in ("canceled", "timed_out", "aborted", "superseded"):
        _send_ack(
            request_id,
            {
                "ignored": True,
                "handoffId": handoff_id,
                "status": handoff.get("status"),
            },
        )
        return handoff
    if current_client.get("clientId") not in (
        handoff.get("sourceClientId"),
        handoff.get("targetClientId"),
    ):
        raise PermissionError("Only handoff source or target can cancel handoff")
    previous_status = handoff.get("status")
    updated_handoff = state.update_playback_handoff(handoff_id, status="canceled")
    if updated_handoff is None:
        handoff = dict(handoff)
        handoff["status"] = "canceled"
    else:
        handoff = updated_handoff
    savePlaybackHandoff(handoff)
    prepare_id = handoff.get("prepareId")
    if prepare_id:
        state.finish_prepare_if_preparing(prepare_id, "canceled")
    if previous_status in ("preparing", "ready", "committed"):
        _send_handoff_release(
            handoff,
            handoff.get("targetClientId"),
            "canceled",
            request_id=request_id,
            authority_client_id=handoff.get("sourceClientId"),
            source_client_id=current_client.get("clientId"),
        )
    context = _get_or_restore_playback_context(handoff.get("playbackContextId"))
    _send_ack(
        request_id,
        {
            "canceled": True,
            "handoffId": handoff_id,
            "status": "canceled",
            "authorityClientId": None if context is None else context.get("authorityClientId"),
            "sourceKeptAuthority": (
                context is not None
                and context.get("authorityClientId") == handoff.get("sourceClientId")
            ),
        },
    )
    return handoff


class EmoNamespace(Namespace):
    def on_connect(self):
        if not current_app.config["WEBAPP"].get("emo_ws_enabled", True):
            return False
        if not strict_v2_safety.accepts_connections():
            return False
        remote_address = request.remote_addr or "unknown"
        session_info = state.try_register_session(
            request.sid,
            remote_address,
            strict_v2_safety.limit("unauthenticated_connections_per_ip"),
        )
        if session_info is None:
            _log_emo_event(
                logging.WARNING,
                "socket_connect",
                result="rate_limited",
            )
            return False
        _log_socket_access("connect")
        _log_emo_event(logging.INFO, "socket_connect", sid=request.sid)

    def on_disconnect(self):
        session_info, client_info = state.unregister_session(request.sid)
        if session_info is not None and session_info.get("connectionNonce"):
            strict_request_cache.clear_connection(session_info["connectionNonce"])
            state.clear_strict_feedback_connection(session_info["connectionNonce"])
            strict_v2_safety.clear_connection(session_info["connectionNonce"])
        _log_socket_access("disconnect")
        _log_emo_event(logging.INFO, "socket_disconnect", sid=request.sid)
        if client_info is not None:
            _suspend_strict_broadcasts_for_authority_disconnect(client_info)
            for handoff in state.fail_playback_handoffs_for_disconnect(
                client_info.get("clientId")
            ):
                terminal_result = terminateStrictPlaybackHandoff(
                    handoff.get("playbackContextId"),
                    handoff.get("handoffId"),
                    handoff.get("userName"),
                    handoff.get("status"),
                    error_code=handoff.get("errorCode"),
                    error_message=handoff.get("errorMessage"),
                )
                if terminal_result is None:
                    continue
                handoff, transitioned = terminal_result
                state.update_playback_handoff(
                    handoff.get("handoffId"),
                    status=handoff.get("status"),
                    error_code=handoff.get("errorCode"),
                    error_message=handoff.get("errorMessage"),
                )
                if not transitioned:
                    continue
                if (handoff.get("snapshot") or {}).get("targetDeviceSessionId"):
                    _run_post_commit_push(
                        "playback.handoff.cancel",
                        None,
                        lambda: _broadcast_handoff_cancel(
                            handoff,
                            handoff.get("errorCode") or "disconnected",
                        ),
                    )
                    include_sid = None
                    for client_id in (
                        handoff.get("targetClientId"),
                        handoff.get("sourceClientId"),
                        handoff.get("originClientId"),
                    ):
                        include_sid = state.get_sid_for_client(
                            client_id,
                            user_name=handoff.get("userName"),
                        )
                        if include_sid is not None:
                            break
                    _run_post_commit_push(
                        "playback.handoff.cancel",
                        None,
                        lambda: _broadcast_handoff_status(
                            handoff,
                            include_sid=include_sid,
                        ),
                    )
            _broadcast_clients(client_info["userName"])
        elif session_info is not None and session_info.get("userName"):
            _broadcast_clients(session_info["userName"])

    def on_message(self, message):
        g.emo_request_started_at = time.monotonic()
        if not isinstance(message, dict):
            _log_emo_event(logging.WARNING, "bad_message", result="bad_request", reason="message_not_object")
            disconnect(sid=request.sid, namespace="/emo")
            return

        action = message.get("action")
        request_id = message.get("requestId")
        if (
            not isinstance(action, str)
            or not action.strip()
            or not isinstance(request_id, str)
            or not request_id.strip()
        ):
            _log_emo_event(
                logging.WARNING,
                "bad_message",
                result="bad_request",
                reason="invalid_correlation",
            )
            disconnect(sid=request.sid, namespace="/emo")
            return
        g.emo_request_action = action

        session_info = state.get_session(request.sid)
        current_user_name = None if session_info is None else session_info.get("userName")
        current_client = state.get_client_for_sid(request.sid)
        unregistered_strict_business = (
            current_client is None
            and action in ACTION_SCHEMAS
            and action not in {"auth.login", "device.register"}
        )
        strict_request = (
            action == "auth.login"
            or is_strict_registration_request(message)
            or _is_strict_playback_context_v2(current_client)
            or unregistered_strict_business
            or action == "playback.context.list"
        )
        g.emo_strict_request = strict_request
        if unregistered_strict_business:
            if not _prepare_strict_request_cache(
                message,
                session_info,
                action,
                request_id,
            ):
                return
            _log_emo_event(
                logging.WARNING,
                "unauthorized_action",
                result="unauthorized",
                action=action,
                client_request_id=request_id,
                reason="register_first",
                sid=request.sid,
            )
            _send_error(
                "unauthorized",
                "Register the device before sending business requests",
                request_id,
            )
            return
        if strict_request:
            try:
                message = validate_strict_request(message)
            except StrictRequestValidationError as exc:
                _log_emo_event(
                    logging.WARNING,
                    "strict_v2_contract_rejected",
                    result=exc.code,
                    reason=str(exc),
                    action=action,
                    client_request_id=request_id,
                    sid=request.sid,
                )
                if not exc.correlatable:
                    disconnect(sid=request.sid, namespace="/emo")
                else:
                    _send_error(exc.code, str(exc), request_id)
                return
            action = message["action"]
            request_id = message["requestId"]

        if strict_request and not _prepare_strict_request_cache(
            message,
            session_info,
            action,
            request_id,
        ):
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

        strict_runtime_request = False
        if strict_request:
            strict_runtime_request = strict_v2_safety.begin_request()
            if not strict_runtime_request:
                _send_error(
                    "internal_error",
                    "Server is shutting down",
                    request_id,
                )
                return

        state.touch_session(request.sid)
        state.prune_stale_clients(_get_client_stale_seconds())

        if action == "system.ping":
            try:
                if current_client is None:
                    _send_error(
                        "unauthorized",
                        "Register the device before sending heartbeat",
                        request_id,
                    )
                    return
                _emit_message(
                    _build_message(
                        "system",
                        "system.pong",
                        {"serverTimeMs": _server_time_ms()},
                        requestId=request_id,
                    )
                )
            finally:
                if strict_runtime_request:
                    strict_v2_safety.finish_request()
            return

        if (session_info is None or not session_info.get("authenticated")) and action not in ALLOWED_PRE_AUTH:
            try:
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
            finally:
                if strict_runtime_request:
                    strict_v2_safety.finish_request()
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
                        action=action,
                        reason="invalid_credentials",
                        sid=request.sid,
                    )
                    _send_error("unauthorized", "Invalid credentials", request_id)
                    return
                authenticated_session = state.try_authenticate_session(
                    request.sid,
                    user.name,
                    strict_v2_safety.limit("authenticated_connections_per_user"),
                )
                if authenticated_session is None:
                    _send_error(
                        "rate_limited",
                        "Authenticated connection limit exceeded",
                        request_id,
                        retryAfterMs=1000,
                    )
                    disconnect(sid=request.sid, namespace="/emo")
                    return
                current_user_name = user.name
                _log_emo_event(
                    logging.INFO,
                    "auth_login",
                    result="success",
                    user=user.name,
                    client_request_id=request_id,
                    action=action,
                    sid=request.sid,
                )
                _send_ack(
                    request_id,
                    {"authenticated": True, "userName": user.name},
                )
            elif action == "device.register":
                if not current_user_name:
                    raise PermissionError("Authenticate first")
                previous_sid = state.get_sid_for_client(
                    payload.get("clientId"),
                    user_name=current_user_name,
                )
                current_client = _register_device(request.sid, current_user_name, payload)
                if _is_strict_playback_context_v2(current_client):
                    _resume_strict_broadcast_authority_registration(
                        current_client
                    )
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
                ack_payload = {
                    "clientId": current_client.get("clientId"),
                    "deviceSessionId": current_client.get("deviceSessionId"),
                }
                if _is_strict_playback_context_v2(current_client):
                    ack_payload["negotiatedCapabilities"] = dict(
                        current_client.get("capabilities") or {}
                    )
                    ack_payload["strictV2"] = get_strict_v2_registration_metadata(
                        session_info.get("connectionNonce") if session_info else None
                    )
                else:
                    ack_payload["client"] = current_client
                _send_ack(request_id, ack_payload)
                if previous_sid is not None and previous_sid != request.sid:
                    socketio.server.disconnect(previous_sid, namespace="/emo")
                _broadcast_clients(current_user_name)
                if not _is_strict_playback_context_v2(current_client):
                    _restorePersistedState(request.sid, current_client.get("sessionId"))
            elif action == "device.list":
                _emit_message(
                    _build_message(
                        "state",
                        "device.list",
                        {
                            "devices": _serialize_clients_for_target(
                                _list_clients(user_name=current_user_name),
                                current_client,
                            )
                        },
                        requestId=request_id,
                    ),
                )
            elif action == "device.setVolume":
                _handle_device_set_volume(
                    current_user_name,
                    current_client,
                    payload,
                    request_id,
                )
                _log_emo_event(
                    logging.INFO,
                    "device_set_volume",
                    result="success",
                    user=current_user_name,
                    client_request_id=request_id,
                    source_client_id=None
                    if current_client is None
                    else current_client.get("clientId"),
                    target_client_id=payload.get("targetClientId"),
                    target_device_session_id=payload.get(
                        "targetDeviceSessionId"
                    ),
                    volume=payload.get("volume"),
                )
            elif action == "device.volume.update":
                volume_state = _handle_device_volume_update(
                    current_user_name,
                    current_client,
                    payload,
                    request_id,
                )
                _log_emo_event(
                    logging.INFO,
                    "device_volume_update",
                    result="success",
                    user=current_user_name,
                    client_request_id=request_id,
                    source_client_id=None
                    if current_client is None
                    else current_client.get("clientId"),
                    device_session_id=payload.get("deviceSessionId"),
                    volume=None
                    if volume_state is None
                    else volume_state.get("volume"),
                    client_seq=payload.get("clientSeq"),
                )
            elif action == "playback.context.list":
                bindings = _handle_playback_context_list(
                    current_user_name,
                    current_client,
                    payload,
                    request_id,
                )
                _log_emo_event(
                    logging.INFO,
                    "playback_context_list",
                    result="success",
                    user=current_user_name,
                    client_request_id=request_id,
                    source_client_id=None
                    if current_client is None
                    else current_client.get("clientId"),
                    authority_client_id=payload.get("authorityClientId"),
                    context_count=len(bindings),
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
                    if not (
                        _is_strict_playback_context_v2(current_client)
                        or _is_context_payload(payload)
                    ):
                        _validate_queue_play_item(message.get("targetClientId"), payload)
                elif action == "player.requestState":
                    _validate_player_request_state(payload)
                if _handle_server_mediated_control(
                    current_user_name,
                    current_client,
                    message,
                    request_id,
                ):
                    _log_emo_event(
                        logging.INFO,
                        "control_forward",
                        result="server_mediated",
                        action=action,
                        user=current_user_name,
                        session_id=payload.get("sessionId") or current_client.get("sessionId"),
                        source_client_id=current_client.get("clientId"),
                        target_client_id=message.get("targetClientId"),
                        client_request_id=request_id,
                    )
                    return
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
            elif action in HANDOFF_ACTIONS:
                if action == "playback.handoff.start":
                    handoff = _handle_handoff_start(
                        current_user_name,
                        current_client,
                        payload,
                        request_id,
                        request.sid,
                    )
                elif action == "playback.handoff.complete":
                    handoff = _handle_handoff_complete(
                        current_user_name,
                        current_client,
                        payload,
                        request_id,
                        request.sid,
                    )
                    if _is_strict_playback_context_v2(current_client):
                        handoff_status_message = _build_handoff_status_message(
                            handoff
                        )
                        playback_context_id = handoff.get("playbackContextId")
                        playback_context = state.get_playback_context(
                            playback_context_id
                        )
                        if playback_context is None:
                            raise LookupError("Playback context not found")
                        context_status_message = (
                            _build_playback_context_status_message(
                                playback_context
                            )
                        )
                        _store_event_confirmations(
                            [handoff_status_message, context_status_message]
                        )
                        _run_post_commit_push(
                            action,
                            request_id,
                            lambda: _broadcast_handoff_status(
                                handoff,
                                include_sid=request.sid,
                            ),
                        )
                        _run_post_commit_push(
                            action,
                            request_id,
                            lambda: _broadcast_playback_context_state_v2(
                                current_user_name,
                                playback_context_id,
                            ),
                        )
                        _run_post_commit_push(
                            action,
                            request_id,
                            lambda: _send_handoff_release(
                                handoff,
                                handoff.get("sourceClientId"),
                                "handoff_completed",
                                authority_client_id=handoff.get(
                                    "targetClientId"
                                ),
                                source_client_id=handoff.get(
                                    "targetClientId"
                                ),
                            ),
                        )
                        binding_mutation = handoff.get(
                            "_bindingMutation"
                        ) or {}
                        if binding_mutation.get("mutated"):
                            _run_post_commit_push(
                                action,
                                request_id,
                                lambda: (
                                    _broadcast_playback_context_bindings_changed(
                                        current_user_name,
                                        binding_mutation.get(
                                            "affectedAuthorityPairs"
                                        ) or (),
                                    )
                                ),
                            )
                else:
                    handoff = _handle_handoff_cancel(
                        current_user_name,
                        current_client,
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
                    playback_context_id=None if handoff is None else handoff.get("playbackContextId"),
                    handoff_id=None if handoff is None else handoff.get("handoffId"),
                )
            elif action == "playback.ready":
                ready_result = _handle_playback_ready(
                    current_client,
                    payload,
                    request_id,
                )
                if _is_strict_playback_context_v2(current_client):
                    prepare = state.get_prepare(payload.get("prepareId")) or {}
                    commit_payload = (
                        prepare.get("commitPayload")
                        or (ready_result or {}).get("commitPayload")
                        or {}
                    )
                    handoff_id = (
                        payload.get("handoffId")
                        or (ready_result or {}).get("handoffId")
                        or commit_payload.get("handoffId")
                    )
                    handoff = (
                        state.get_playback_handoff(handoff_id)
                        or getPlaybackHandoff(handoff_id)
                    )
                    if handoff is None:
                        raise LookupError("Playback handoff not found")
                    handoff_status_message = _build_handoff_status_message(
                        handoff
                    )
                    _store_event_confirmations([handoff_status_message])
                    _run_post_commit_push(
                        action,
                        request_id,
                        lambda: _broadcast_handoff_status(
                            handoff,
                            include_sid=request.sid,
                        ),
                    )
                _log_emo_event(
                    logging.INFO,
                    "playback_ready",
                    result="success",
                    user=current_user_name,
                    client_request_id=request_id,
                    source_client_id=None if current_client is None else current_client.get("clientId"),
                    prepare_id=payload.get("prepareId"),
                    status=None if ready_result is None else ready_result.get("status"),
                )
            elif action == "playback.context.ensure":
                playback_context = _handle_playback_context_ensure(
                    current_user_name,
                    current_client,
                    payload,
                    request_id,
                    request.sid,
                )
                _log_emo_event(
                    logging.INFO,
                    "playback_context_ensure",
                    result="success",
                    user=current_user_name,
                    client_request_id=request_id,
                    source_client_id=None
                    if current_client is None
                    else current_client.get("clientId"),
                    playback_context_id=None
                    if playback_context is None
                    else playback_context.get("playbackContextId"),
                )
            elif action == "playback.context.status":
                playback_context = _handle_playback_context_status(
                    current_user_name,
                    current_client,
                    payload,
                    request_id,
                )
                _log_emo_event(
                    logging.INFO,
                    "playback_context_status",
                    result="success",
                    user=current_user_name,
                    client_request_id=request_id,
                    source_client_id=None
                    if current_client is None
                    else current_client.get("clientId"),
                    playback_context_id=None
                    if playback_context is None
                    else playback_context.get("playbackContextId"),
                )
            elif action == "playback.context.subscribe":
                playback_context = _handle_playback_context_subscribe(
                    current_user_name,
                    current_client,
                    payload,
                    request_id,
                    request.sid,
                )
                _log_emo_event(
                    logging.INFO,
                    "playback_context_subscribe",
                    result="success",
                    user=current_user_name,
                    client_request_id=request_id,
                    source_client_id=None
                    if current_client is None
                    else current_client.get("clientId"),
                    playback_context_id=None
                    if playback_context is None
                    else playback_context.get("playbackContextId"),
                )
            elif action == "playback.context.unsubscribe":
                playback_context = _handle_playback_context_unsubscribe(
                    current_user_name,
                    current_client,
                    payload,
                    request_id,
                    request.sid,
                )
                _log_emo_event(
                    logging.INFO,
                    "playback_context_unsubscribe",
                    result="success",
                    user=current_user_name,
                    client_request_id=request_id,
                    source_client_id=None
                    if current_client is None
                    else current_client.get("clientId"),
                    playback_context_id=payload.get("playbackContextId")
                    if playback_context is None
                    else playback_context.get("playbackContextId"),
                )
            elif action == "playback.context.close":
                playback_context = _handle_playback_context_close(
                    current_user_name,
                    current_client,
                    payload,
                    request_id,
                )
                _log_emo_event(
                    logging.INFO,
                    "playback_context_close",
                    result="success",
                    user=current_user_name,
                    client_request_id=request_id,
                    source_client_id=None
                    if current_client is None
                    else current_client.get("clientId"),
                    playback_context_id=None
                    if playback_context is None
                    else playback_context.get("playbackContextId"),
                )
            elif action == "playback.update":
                if current_client is None:
                    raise PermissionError("Register the device before publishing state")
                strict_v2 = _is_strict_playback_context_v2(current_client)
                context_payload = _is_context_payload(payload)
                _reject_session_id_for_strict_v2(
                    payload,
                    strict_v2,
                )
                if strict_v2:
                    if not isinstance(payload.get("state"), str) or not payload.get("state"):
                        raise ValueError("playback.update requires a non-empty state")
                    if not _is_non_negative_int(payload.get("positionMs")):
                        raise ValueError("playback.update requires a non-negative positionMs")
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
                    if strict_v2:
                        device_session_id = _resolve_v2_device_session_id(
                            payload,
                            current_client,
                            strict_v2=True,
                        )
                    else:
                        device_session_id = _resolve_device_session_id(payload, current_client)
                    if not isinstance(device_session_id, str) or not device_session_id:
                        raise ValueError("playback.update requires a non-empty deviceSessionId")
                    broadcast_for_update = _get_broadcast_from_payload(current_user_name, payload)
                    playback_payload = _normalize_v2_playback_update_payload(
                        playback_payload
                    )
                    broadcast_id = broadcast_for_update["broadcastId"]
                    payload_playback_context_id = payload.get("playbackContextId")
                    broadcast_playback_context_id = broadcast_for_update.get(
                        "playbackContextId"
                    )
                    if strict_v2 and not isinstance(payload_playback_context_id, str):
                        raise ValueError("playback.update requires a non-empty playbackContextId")
                    if payload_playback_context_id is not None:
                        if (
                            not isinstance(payload_playback_context_id, str)
                            or not payload_playback_context_id
                        ):
                            raise ValueError("playback.update requires a non-empty playbackContextId")
                        if (
                            broadcast_playback_context_id is not None
                            and payload_playback_context_id != broadcast_playback_context_id
                        ):
                            raise ValueError("playback.update playbackContextId does not match broadcast")
                        broadcast_playback_context_id = payload_playback_context_id
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
                    state.update_broadcast_participant_state(
                        broadcast_for_update["broadcastId"],
                        current_client.get("clientId"),
                        device_session_id,
                        playback_payload,
                        online=True,
                    )
                    if broadcast_playback_context_id:
                        playback_payload["playbackContextId"] = broadcast_playback_context_id
                        playback_payload["deviceSessionId"] = device_session_id
                        device_feedback = state.record_device_playback_state(
                            broadcast_playback_context_id,
                            device_session_id,
                            current_client.get("clientId"),
                            current_user_name,
                            playback_payload,
                            is_authority=False,
                            mode="broadcast",
                        )
                        _save_device_playback_state_snapshot(
                            current_user_name,
                            device_feedback,
                        )
                        if strict_v2:
                            update_message = _build_v2_playback_update_message(
                                device_feedback
                            )
                            _store_event_confirmations([update_message])
                            _run_post_commit_push(
                                action,
                                request_id,
                                lambda: _broadcast_v2_playback_update(
                                    current_user_name,
                                    broadcast_playback_context_id,
                                    device_feedback,
                                ),
                            )
                    _log_emo_event(
                        logging.INFO,
                        "playback_update",
                        result="participant_feedback",
                        user=current_user_name,
                        client_request_id=request_id,
                        **_build_playback_summary(
                            device_session_id,
                            current_client.get("clientId"),
                            playback_payload,
                            broadcast_id=broadcast_id,
                        ),
                    )
                    _send_ack(request_id, {"updated": True, "participantFeedback": True})
                    return

                use_v2_context = strict_v2 or context_payload
                if use_v2_context:
                    playback_context_id = _resolve_v2_playback_context_id(
                        payload,
                        strict_v2=strict_v2,
                    )
                    if not isinstance(playback_context_id, str) or not playback_context_id:
                        raise ValueError("playback.update requires a non-empty playbackContextId")
                    device_session_id = _resolve_v2_device_session_id(
                        payload,
                        current_client,
                        strict_v2=strict_v2,
                    )
                    if not isinstance(device_session_id, str) or not device_session_id:
                        raise ValueError("playback.update requires a non-empty deviceSessionId")
                    playback_payload["playbackContextId"] = playback_context_id
                    playback_payload["deviceSessionId"] = device_session_id
                    context = _get_existing_playback_context(playback_context_id)
                    if context is None:
                        raise LookupError("Playback context not found")
                    _ensure_playback_context_for_user(context, current_user_name)
                    _ensure_playback_context_active(context)
                    if strict_v2 and device_session_id != current_client.get("deviceSessionId"):
                        raise PermissionError(
                            "deviceSessionId does not match the registered device"
                        )
                    authoritative_update = (
                        context.get("authorityClientId") == current_client.get("clientId")
                        and context.get("authorityDeviceSessionId") == device_session_id
                    )
                    playback_payload = _normalize_v2_playback_update_payload(
                        playback_payload,
                        playback_context=context,
                        authoritative=authoritative_update,
                    )

                    if strict_v2:
                        authoritative_context = context
                        authoritative = authoritative_update
                        device_feedback, feedback_created = (
                            state.record_strict_device_playback_state(
                                playback_context_id,
                                device_session_id,
                                current_client.get("clientId"),
                                current_user_name,
                                playback_payload,
                                session_info.get("connectionNonce"),
                                is_authority=authoritative,
                            )
                        )
                    else:
                        authoritative_context, authoritative = (
                            state.apply_authority_playback_update(
                                playback_context_id,
                                device_session_id,
                                current_client.get("clientId"),
                                current_user_name,
                                playback_payload,
                                create_if_missing=False,
                            )
                        )
                        device_feedback = state.record_device_playback_state(
                            playback_context_id,
                            device_session_id,
                            current_client.get("clientId"),
                            current_user_name,
                            playback_payload,
                            is_authority=authoritative,
                            mode=playback_payload.get("mode") or "normal",
                        )
                        feedback_created = True
                    if authoritative and not strict_v2:
                        _update_playback_context_snapshot(
                            current_user_name,
                            authoritative_context,
                        )
                    if feedback_created:
                        _save_device_playback_state_snapshot(
                            current_user_name,
                            device_feedback,
                        )
                    if strict_v2:
                        active_broadcast_id = state.get_active_broadcast_for_client(
                            current_client.get("clientId")
                        )
                        active_broadcast = (
                            state.get_broadcast(active_broadcast_id)
                            if active_broadcast_id is not None
                            else None
                        )
                        if (
                            active_broadcast is not None
                            and active_broadcast.get("playbackContextId")
                            == playback_context_id
                        ):
                            state.update_broadcast_participant_state(
                                active_broadcast_id,
                                current_client.get("clientId"),
                                device_session_id,
                                device_feedback,
                                online=True,
                            )
                    _log_emo_event(
                        logging.INFO,
                        "playback_update",
                        result="authority" if authoritative else "device_feedback",
                        user=current_user_name,
                        client_request_id=request_id,
                        **_build_playback_summary(
                            device_session_id,
                            current_client.get("clientId"),
                            playback_payload,
                            playback_context_id=playback_context_id,
                        ),
                    )
                    ack_payload = {
                        "updated": True,
                        "playbackContextId": playback_context_id,
                        "authorityClientId": (
                            authoritative_context.get("authorityClientId")
                            if authoritative_context is not None
                            else None
                        ),
                        "authoritative": authoritative,
                    }
                    if not authoritative:
                        ack_payload["deviceFeedback"] = True
                        ack_payload["currentAuthorityClientId"] = (
                            authoritative_context.get("authorityClientId")
                            if authoritative_context is not None
                            else None
                        )
                    _send_ack(request_id, ack_payload)
                    if strict_v2:
                        update_message = _build_v2_playback_update_message(
                            device_feedback
                        )
                        _store_event_confirmations([update_message])
                        if not feedback_created:
                            _run_post_commit_push(
                                action,
                                request_id,
                                lambda: _emit_message(
                                    update_message,
                                    request.sid,
                                ),
                            )
                        else:
                            _run_post_commit_push(
                                action,
                                request_id,
                                lambda: _broadcast_v2_playback_update(
                                    current_user_name,
                                    playback_context_id,
                                    device_feedback,
                                ),
                            )
                    else:
                        update_message = _broadcast_v2_playback_update(
                            current_user_name,
                            playback_context_id,
                            device_feedback,
                        )
                    if authoritative and not strict_v2:
                        _broadcast_playback_context_state_v2(
                            current_user_name,
                            playback_context_id,
                        )
                    return

                playback_context_id = _resolve_playback_context_id(payload, current_client)
                if not isinstance(playback_context_id, str) or not playback_context_id:
                    raise ValueError("playback.update requires a non-empty playbackContextId")
                device_session_id = _resolve_device_session_id(payload, current_client)
                if not isinstance(device_session_id, str) or not device_session_id:
                    raise ValueError("playback.update requires a non-empty deviceSessionId")
                playback_payload["playbackContextId"] = playback_context_id
                playback_payload["deviceSessionId"] = device_session_id
                context = _get_or_restore_playback_context(playback_context_id)
                if context is not None:
                    _ensure_playback_context_for_user(context, current_user_name)
                legacy_playback_payload = state.update_playback_state(
                    device_session_id,
                    current_client.get("clientId"),
                    playback_payload,
                )
                authoritative_context, authoritative = state.apply_authority_playback_update(
                    playback_context_id,
                    device_session_id,
                    current_client.get("clientId"),
                    current_user_name,
                    playback_payload,
                )
                if context is None:
                    context = authoritative_context
                device_feedback = state.record_device_playback_state(
                    playback_context_id,
                    device_session_id,
                    current_client.get("clientId"),
                    current_user_name,
                    playback_payload,
                    is_authority=authoritative,
                )
                if authoritative:
                    _save_playback_context_snapshot(
                        current_user_name,
                        authoritative_context,
                    )
                _save_device_playback_state_snapshot(
                    current_user_name,
                    device_feedback,
                )
                savePlaybackState(
                    device_session_id,
                    current_user_name,
                    current_client.get("clientId"),
                    legacy_playback_payload,
                )
                _log_emo_event(
                    logging.INFO,
                    "playback_update",
                    result="authority" if authoritative else "device_feedback",
                    user=current_user_name,
                    client_request_id=request_id,
                    **_build_playback_summary(
                        device_session_id,
                        current_client.get("clientId"),
                        playback_payload,
                        playback_context_id=playback_context_id,
                    ),
                )
                ack_payload = {
                    "updated": True,
                    "playbackContextId": playback_context_id,
                    "authorityClientId": (
                        authoritative_context.get("authorityClientId")
                        if authoritative_context is not None
                        else None
                    ),
                    "authoritative": authoritative,
                }
                if not authoritative:
                    ack_payload["deviceFeedback"] = True
                    ack_payload["currentAuthorityClientId"] = (
                        authoritative_context.get("authorityClientId")
                        if authoritative_context is not None
                        else None
                    )
                _send_ack(request_id, ack_payload)
                _broadcast_playback_state(current_user_name, device_session_id)
                if authoritative:
                    _broadcast_playback_context_state(
                        current_user_name,
                        playback_context_id,
                    )
            elif action == "queue.context.sync":
                playback_context = _handle_queue_context_sync(
                    current_user_name,
                    current_client,
                    payload,
                    request_id,
                )
                _log_emo_event(
                    logging.INFO,
                    "queue_context_sync",
                    result="success",
                    user=current_user_name,
                    client_request_id=request_id,
                    source_client_id=None
                    if current_client is None
                    else current_client.get("clientId"),
                    playback_context_id=None
                    if playback_context is None
                    else playback_context.get("playbackContextId"),
                )
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
                    _emit_message(_build_message("state", "queue.local.set", local_queue))
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
                        current_user_name,
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
                playback_context_id = _resolve_playback_context_id(payload, current_client)
                if not isinstance(playback_context_id, str) or not playback_context_id:
                    raise ValueError("queue.session.sync requires a non-empty playbackContextId")
                device_session_id = _resolve_device_session_id(payload, current_client)
                if not isinstance(device_session_id, str) or not device_session_id:
                    raise ValueError("queue.session.sync requires a non-empty deviceSessionId")
                current_client_id = payload.get("clientId") or current_client.get("clientId")
                if not isinstance(current_client_id, str) or not current_client_id:
                    raise ValueError("queue.session.sync clientId must be a non-empty string")
                if payload.get("clientId"):
                    owner_client = state.get_client(
                        current_client_id,
                        user_name=current_user_name,
                    )
                    if owner_client is None:
                        raise LookupError("Queue owner client is offline")
                    if owner_client.get("userName") != current_user_name:
                        raise PermissionError("Cross-user queue sync is not allowed")
                    if _device_session_id(owner_client) != device_session_id:
                        raise ValueError("queue.session.sync clientId must belong to deviceSessionId")
                _ensure_not_follow_source_queue_update(
                    current_client,
                    device_session_id,
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

                is_context_payload = (
                    "playbackContextId" in payload or "deviceSessionId" in payload
                )
                base_queue_revision = _get_base_queue_revision(payload)
                existing_playback_context = _get_or_restore_playback_context(
                    playback_context_id
                )
                if existing_playback_context is not None:
                    _ensure_playback_context_for_user(
                        existing_playback_context,
                        current_user_name,
                    )
                try:
                    if is_context_payload:
                        playback_context = state.update_playback_context_queue(
                            playback_context_id,
                            device_session_id,
                            queue_song_ids,
                            current_index,
                            position_ms,
                            current_client_id,
                            user_name=current_user_name,
                            expected_queue_revision=base_queue_revision,
                        )
                        queue_state = state.update_queue(
                            playback_context_id,
                            queue_song_ids,
                            current_index,
                            position_ms,
                            current_client_id,
                        )
                    else:
                        queue_state = state.update_queue(
                            playback_context_id,
                            queue_song_ids,
                            current_index,
                            position_ms,
                            current_client_id,
                            expected_queue_revision=base_queue_revision,
                        )
                        playback_context = state.update_playback_context_queue(
                            playback_context_id,
                            device_session_id,
                            queue_song_ids,
                            current_index,
                            position_ms,
                            current_client_id,
                            user_name=current_user_name,
                        )
                except QueueRevisionMismatchError as exc:
                    raise QueueConflictError(
                        "Queue revision conflict",
                        current_queue_revision=exc.current_revision,
                    )
                supersede_timeline_id = (
                    playback_context.get("timelineId")
                    if is_context_payload
                    else queue_state.get("timelineId")
                )
                _supersede_prepares_for_timeline(
                    supersede_timeline_id
                    or _source_timeline_id(playback_context_id, current_client_id)
                )
                _save_playback_context_snapshot(current_user_name, playback_context)
                saveQueueState(
                    playback_context_id,
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
                        playback_context_id,
                        current_client_id,
                        queue_song_ids,
                        current_index,
                        position_ms,
                        device_session_id=device_session_id,
                        playback_context_id=playback_context_id,
                    ),
                )
                ack_queue = playback_context if is_context_payload else queue_state
                _send_ack(request_id, {"updated": True, "queue": ack_queue})
                if is_context_payload:
                    _broadcast_playback_context_queue(current_user_name, playback_context_id)
                else:
                    _broadcast_queue(current_user_name, playback_context_id)
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
        except PlaybackContextStaleVersionError as exc:
            playback_context = exc.playback_context or {}
            fields = {
                "playbackContextId": playback_context.get("playbackContextId"),
            }
            if exc.cursor_name == "controlVersion":
                fields["currentControlVersion"] = playback_context.get(
                    "controlVersion"
                )
            elif exc.cursor_name == "queueRevision":
                fields["currentQueueRevision"] = playback_context.get(
                    "queueRevision"
                )
            _send_error("stale_version", str(exc), request_id, **fields)
        except PlaybackContextClosedError as exc:
            playback_context = exc.playback_context or {}
            _send_error(
                "context_closed",
                str(exc),
                request_id,
                playbackContextId=playback_context.get("playbackContextId")
                or payload.get("playbackContextId"),
            )
        except PlaybackContextIntentConflictError as exc:
            playback_context = exc.playback_context or {}
            _send_error(
                "conflict",
                str(exc),
                request_id,
                playbackContextId=playback_context.get("playbackContextId"),
                currentControlVersion=playback_context.get("controlVersion"),
                currentQueueRevision=playback_context.get("queueRevision"),
                currentVersion=playback_context.get("version"),
            )
        except PlaybackContextEnsureConflictError as exc:
            playback_context = exc.playback_context or {}
            _send_error(
                "conflict",
                str(exc),
                request_id,
                playbackContextId=playback_context.get("playbackContextId"),
                currentControlVersion=playback_context.get("controlVersion"),
                currentQueueRevision=playback_context.get("queueRevision"),
                currentVersion=playback_context.get("version"),
            )
        except PlaybackContextQueueRequiredError as exc:
            playback_context = exc.playback_context or {}
            _send_error(
                "queue_required",
                str(exc),
                request_id,
                playbackContextId=playback_context.get("playbackContextId")
                or payload.get("playbackContextId"),
                currentControlVersion=playback_context.get("controlVersion"),
                currentQueueRevision=playback_context.get("queueRevision"),
                currentVersion=playback_context.get("version"),
            )
        except PlaybackContextAuthorityAmbiguousError as exc:
            playback_context = exc.playback_context or {}
            _send_error(
                "conflict",
                str(exc),
                request_id,
                playbackContextId=playback_context.get(
                    "playbackContextId"
                ) or payload.get("playbackContextId"),
                currentControlVersion=playback_context.get(
                    "controlVersion"
                ),
                currentQueueRevision=playback_context.get(
                    "queueRevision"
                ),
                currentVersion=playback_context.get("version"),
            )
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
            _emit_message(
                _build_message(
                    "system",
                    "system.error",
                    error_payload,
                    requestId=request_id,
                )
            )
        except ControlConflictError as exc:
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
            if exc.current_control_version is not None:
                error_payload["currentControlVersion"] = exc.current_control_version
            _emit_message(
                _build_message(
                    "system",
                    "system.error",
                    error_payload,
                    requestId=request_id,
                )
            )
        except FollowConflictError as exc:
            _send_error("conflict", str(exc), request_id)
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
            _emit_message(
                _build_message(
                    "system",
                    "system.error",
                    error_payload,
                    requestId=request_id,
                )
            )
        except ClientSeqStaleError as exc:
            strict_v2 = _is_strict_playback_context_v2(current_client)
            error_code = (
                "client_sequence_conflict" if strict_v2 else "stale_client_seq"
            )
            _log_emo_event(
                logging.WARNING,
                _get_action_event_name(action) or "bad_message",
                result=error_code,
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
            _emit_message(
                _build_message(
                    "system",
                    "system.error",
                    {
                        "code": error_code,
                        "message": str(exc),
                        "currentClientSeq": exc.current_seq,
                    },
                    requestId=request_id,
                )
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
        except PlaybackAuthorityOfflineError as exc:
            event_name = _get_action_event_name(action) or "bad_message"
            _log_emo_event(
                logging.WARNING,
                event_name,
                result="authority_offline",
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
            _send_error(
                "authority_offline",
                str(exc),
                request_id,
                playbackContextId=payload.get("playbackContextId"),
            )
        except CoreProfileNotReady as exc:
            _log_emo_event(
                logging.WARNING,
                "device_register",
                result="not_supported",
                reason=str(exc),
                action=action,
                client_request_id=request_id,
                sid=request.sid,
            )
            _send_error("not_supported", str(exc), request_id)
        except CapabilityRequiredError as exc:
            _send_error("capability_required", str(exc), request_id)
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
        except Exception as exc:
            _log_emo_event(
                logging.ERROR,
                _get_action_event_name(action) or "strict_v2_internal_error",
                result="internal_error",
                exception_type=type(exc).__name__,
                **_build_action_log_context(
                    action,
                    request_id,
                    current_user_name,
                    current_client,
                    payload,
                    message,
                ),
            )
            _send_error("internal_error", "Internal server error", request_id)
        finally:
            if strict_runtime_request:
                strict_v2_safety.finish_request()


socketio.on_namespace(EmoNamespace("/emo"))
