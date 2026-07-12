import hashlib
import json
import threading
import time
from contextlib import contextmanager
from functools import wraps

from peewee import IntegrityError

from ..db import (
    EmoDevicePlaybackState,
    EmoLocalQueue,
    EmoPlaybackContext,
    EmoPlaybackHandoff,
    EmoPlaybackState,
    EmoSessionQueue,
    close_connection,
    db,
    now,
    open_connection,
)


class PlaybackContextClosedError(Exception):
    def __init__(self, playback_context):
        super().__init__("Playback context is closed")
        self.playback_context = playback_context


class PlaybackContextIntentConflictError(Exception):
    def __init__(self, playback_context):
        super().__init__("playbackContextId already exists with different initial intent")
        self.playback_context = playback_context


class PlaybackContextStaleVersionError(Exception):
    def __init__(self, playback_context, cursor_name):
        super().__init__("Playback context %s is stale" % cursor_name)
        self.playback_context = playback_context
        self.cursor_name = cursor_name


_strict_playback_context_locks = {}
_strict_playback_context_locks_guard = threading.Lock()


@contextmanager
def _strict_playback_context_lock(playback_context_id):
    with _strict_playback_context_locks_guard:
        context_lock = _strict_playback_context_locks.setdefault(
            playback_context_id,
            threading.RLock(),
        )
    with context_lock:
        yield


def _serialize_strict_playback_context_mutation(function):
    @wraps(function)
    def serialized(playback_context_id, *args, **kwargs):
        with _strict_playback_context_lock(playback_context_id):
            return function(playback_context_id, *args, **kwargs)

    return serialized


def _strip_transient_playback_fields(payload):
    payload.pop("serverTimeMs", None)
    effective_at_server_ms = payload.get("effectiveAtServerMs")
    if not isinstance(effective_at_server_ms, (int, float)):
        payload.pop("effectiveAtServerMs", None)
        return
    if effective_at_server_ms <= int(time.time() * 1000):
        payload.pop("effectiveAtServerMs", None)


def _payload_value_or_default(payload, key, default):
    value = payload.get(key)
    return default if value is None else value


def serializePlaybackContextV2(playback_context):
    if playback_context is None:
        return None
    payload = {
        "playbackContextId": playback_context.get("playbackContextId"),
        "authorityClientId": playback_context.get("authorityClientId"),
        "queueSongIds": list(playback_context.get("queueSongIds") or []),
        "currentIndex": playback_context.get("currentIndex", 0),
        "state": playback_context.get("state") or "stopped",
        "positionMs": playback_context.get("positionMs", 0),
        "queueRevision": playback_context.get("queueRevision", 1),
        "controlVersion": playback_context.get("controlVersion", 1),
        "version": playback_context.get("version", 1),
        "epoch": playback_context.get("epoch", 1),
    }
    for field_name in ("trackId", "timelineId", "serverUpdatedAtMs"):
        value = playback_context.get(field_name)
        if value is not None:
            payload[field_name] = value
    return payload


def serializeDevicePlaybackStateV2(device_state):
    if device_state is None:
        return None
    client_id = (
        device_state.get("clientId")
        or device_state.get("ownerClientId")
        or device_state.get("sourceClientId")
    )
    payload = {
        "playbackContextId": device_state.get("playbackContextId"),
        "clientId": client_id,
        "deviceSessionId": device_state.get("deviceSessionId"),
        "state": device_state.get("state"),
        "positionMs": device_state.get("positionMs", 0),
        "clientSeq": device_state.get("clientSeq"),
        "serverUpdatedAtMs": device_state.get("serverUpdatedAtMs"),
    }
    if any(value is None for value in payload.values()):
        return None
    for field_name in ("trackId", "volume", "muted"):
        value = device_state.get(field_name)
        if value is not None:
            payload[field_name] = value
    return payload


def getQueueState(session_id):
    open_connection(reuse=True)
    try:
        record = EmoSessionQueue.get_or_none(EmoSessionQueue.session_id == session_id)
        if record is None:
            return None
        return {
            "sessionId": record.session_id,
            "userName": record.user_name,
            "queueSongIds": json.loads(record.queue_json),
            "currentIndex": record.current_index,
            "positionMs": record.position_ms,
            "sourceClientId": record.owner_client_id,
            "queueRevision": record.version,
            "version": record.version,
            "controlVersion": record.version,
            "serverUpdatedAtMs": int(record.updated_at.timestamp() * 1000),
            "updatedAt": record.updated_at.timestamp(),
        }
    finally:
        close_connection()


def saveQueueState(session_id, user_name, client_id, queue_song_ids, current_index, position_ms):
    payload = json.dumps(list(queue_song_ids), ensure_ascii=True)
    open_connection(reuse=True)
    try:
        record = EmoSessionQueue.get_or_none(EmoSessionQueue.session_id == session_id)
        if record is None:
            EmoSessionQueue.create(
                session_id=session_id,
                user_name=user_name,
                owner_client_id=client_id,
                queue_json=payload,
                current_index=current_index,
                position_ms=position_ms,
            )
            return

        record.user_name = user_name
        record.owner_client_id = client_id
        record.queue_json = payload
        record.current_index = current_index
        record.position_ms = position_ms
        record.version += 1
        record.updated_at = now()
        record.save()
    finally:
        close_connection()


def getLocalQueueState(session_id, client_id):
    open_connection(reuse=True)
    try:
        record = EmoLocalQueue.get_or_none(
            (EmoLocalQueue.session_id == session_id)
            & (EmoLocalQueue.owner_client_id == client_id)
        )
        if record is None:
            return None
        return {
            "sessionId": record.session_id,
            "sourceClientId": record.owner_client_id,
            "queueSongIds": json.loads(record.queue_json),
            "currentIndex": record.current_index,
            "positionMs": record.position_ms,
            "serverUpdatedAtMs": int(record.updated_at.timestamp() * 1000),
            "updatedAt": record.updated_at.timestamp(),
        }
    finally:
        close_connection()


def getLocalQueueStates(session_id):
    open_connection(reuse=True)
    try:
        payloads = []
        query = EmoLocalQueue.select().where(EmoLocalQueue.session_id == session_id)
        for record in query:
            payloads.append(
                {
                    "sessionId": record.session_id,
                    "sourceClientId": record.owner_client_id,
                    "queueSongIds": json.loads(record.queue_json),
                    "currentIndex": record.current_index,
                    "positionMs": record.position_ms,
                    "serverUpdatedAtMs": int(record.updated_at.timestamp() * 1000),
                    "updatedAt": record.updated_at.timestamp(),
                }
            )
        return payloads
    finally:
        close_connection()


def saveLocalQueueState(session_id, client_id, queue_song_ids, current_index, position_ms):
    payload = json.dumps(list(queue_song_ids), ensure_ascii=True)
    open_connection(reuse=True)
    try:
        record = EmoLocalQueue.get_or_none(
            (EmoLocalQueue.session_id == session_id)
            & (EmoLocalQueue.owner_client_id == client_id)
        )
        if record is None:
            EmoLocalQueue.create(
                session_id=session_id,
                owner_client_id=client_id,
                queue_json=payload,
                current_index=current_index,
                position_ms=position_ms,
            )
            return

        record.queue_json = payload
        record.current_index = current_index
        record.position_ms = position_ms
        record.updated_at = now()
        record.save()
    finally:
        close_connection()


def getPlaybackState(session_id, client_id):
    open_connection(reuse=True)
    try:
        record = EmoPlaybackState.get_or_none(
            (EmoPlaybackState.session_id == session_id)
            & (EmoPlaybackState.owner_client_id == client_id)
        )
        if record is None:
            return None

        payload = json.loads(record.playback_json) if record.playback_json else {}
        _strip_transient_playback_fields(payload)
        payload.update(
            {
                "sessionId": record.session_id,
                "sourceClientId": record.owner_client_id,
                "state": record.state,
                "trackId": record.track_id,
                "positionMs": record.position_ms,
                "volume": record.volume,
                "updatedAt": record.updated_at.timestamp(),
            }
        )
        payload.setdefault("serverUpdatedAtMs", int(record.updated_at.timestamp() * 1000))
        return payload
    finally:
        close_connection()


def getPlaybackStates(session_id):
    open_connection(reuse=True)
    try:
        payloads = []
        query = EmoPlaybackState.select().where(EmoPlaybackState.session_id == session_id)
        for record in query:
            payload = json.loads(record.playback_json) if record.playback_json else {}
            _strip_transient_playback_fields(payload)
            payload.update(
                {
                    "sessionId": record.session_id,
                    "sourceClientId": record.owner_client_id,
                    "state": record.state,
                    "trackId": record.track_id,
                    "positionMs": record.position_ms,
                    "volume": record.volume,
                    "updatedAt": record.updated_at.timestamp(),
                }
            )
            payload.setdefault("serverUpdatedAtMs", int(record.updated_at.timestamp() * 1000))
            payloads.append(payload)
        return payloads
    finally:
        close_connection()


def savePlaybackState(session_id, user_name, client_id, playback_state):
    payload = dict(playback_state)
    state_name = payload.get("state") or "unknown"
    track_id = payload.get("trackId")
    position_ms = payload.get("positionMs") or 0
    volume = payload.get("volume")
    payload.pop("sessionId", None)
    payload.pop("updatedAt", None)
    payload.pop("serverTimeMs", None)

    open_connection(reuse=True)
    try:
        record = EmoPlaybackState.get_or_none(
            (EmoPlaybackState.session_id == session_id)
            & (EmoPlaybackState.owner_client_id == client_id)
        )
        if record is None:
            EmoPlaybackState.create(
                session_id=session_id,
                user_name=user_name,
                owner_client_id=client_id,
                state=state_name,
                track_id=track_id,
                position_ms=position_ms,
                volume=volume,
                playback_json=json.dumps(payload, ensure_ascii=True),
            )
            return

        record.user_name = user_name
        record.owner_client_id = client_id
        record.state = state_name
        record.track_id = track_id
        record.position_ms = position_ms
        record.volume = volume
        record.playback_json = json.dumps(payload, ensure_ascii=True)
        record.updated_at = now()
        record.save()
    finally:
        close_connection()


def _playback_context_payload(record):
    payload = json.loads(record.playback_json) if record.playback_json else {}
    _strip_transient_playback_fields(payload)
    queue_song_ids = json.loads(record.queue_json)
    payload.update(
        {
            "playbackContextId": record.playback_context_id,
            "sessionId": record.playback_context_id,
            "userName": record.user_name,
            "authorityClientId": record.authority_client_id,
            "authorityDeviceSessionId": record.authority_device_session_id,
            "originClientId": record.origin_client_id,
            "sourceClientId": record.authority_client_id,
            "timelineId": record.timeline_id,
            "creationFingerprint": record.creation_fingerprint,
            "lifecycle": record.lifecycle,
            "queueSongIds": queue_song_ids,
            "currentIndex": record.current_index,
            "trackId": record.track_id,
            "state": record.state,
            "positionMs": record.position_ms,
            "volume": record.volume,
            "queueRevision": record.queue_revision,
            "controlVersion": record.control_version,
            "version": record.version,
            "epoch": record.epoch,
            "serverUpdatedAtMs": int(record.updated_at.timestamp() * 1000),
            "updatedAt": record.updated_at.timestamp(),
            "authoritative": True,
        }
    )
    if record.closed_at is not None:
        payload["closedAtMs"] = int(record.closed_at.timestamp() * 1000)
    return payload


def getPlaybackContextState(playback_context_id):
    open_connection(reuse=True)
    try:
        record = EmoPlaybackContext.get_or_none(
            EmoPlaybackContext.playback_context_id == playback_context_id
        )
        if record is None:
            return None
        return _playback_context_payload(record)
    finally:
        close_connection()


def playbackContextCreationFingerprint(
    user_name,
    authority_client_id,
    authority_device_session_id,
    queue_song_ids,
    current_index,
    position_ms,
    state_name,
):
    canonical = json.dumps(
        {
            "userName": user_name,
            "authorityClientId": authority_client_id,
            "authorityDeviceSessionId": authority_device_session_id,
            "queueSongIds": list(queue_song_ids),
            "currentIndex": current_index,
            "positionMs": position_ms,
            "state": state_name,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _resolveStrictPlaybackContextCreate(record, user_name, creation_fingerprint):
    playback_context = _playback_context_payload(record)
    if record.user_name != user_name:
        raise PermissionError("Playback context belongs to another user")
    if record.lifecycle == "closed":
        raise PlaybackContextClosedError(playback_context)
    if record.creation_fingerprint != creation_fingerprint:
        raise PlaybackContextIntentConflictError(playback_context)
    return playback_context, False


@_serialize_strict_playback_context_mutation
def createStrictPlaybackContextState(
    playback_context_id,
    user_name,
    authority_client_id,
    authority_device_session_id,
    queue_song_ids,
    current_index,
    position_ms,
    state_name,
    timeline_id=None,
):
    queue_song_ids = list(queue_song_ids)
    track_id = queue_song_ids[current_index]
    timeline_id = timeline_id or "playback:%s" % playback_context_id
    fingerprint = playbackContextCreationFingerprint(
        user_name,
        authority_client_id,
        authority_device_session_id,
        queue_song_ids,
        current_index,
        position_ms,
        state_name,
    )
    open_connection(reuse=True)
    try:
        with db.atomic():
            record = EmoPlaybackContext.get_or_none(
                EmoPlaybackContext.playback_context_id == playback_context_id
            )
            if record is not None:
                return _resolveStrictPlaybackContextCreate(
                    record,
                    user_name,
                    fingerprint,
                )
            try:
                with db.atomic():
                    record = EmoPlaybackContext.create(
                        playback_context_id=playback_context_id,
                        user_name=user_name,
                        authority_client_id=authority_client_id,
                        authority_device_session_id=authority_device_session_id,
                        origin_client_id=authority_client_id,
                        timeline_id=timeline_id,
                        creation_fingerprint=fingerprint,
                        lifecycle="active",
                        queue_json=json.dumps(queue_song_ids, ensure_ascii=True),
                        current_index=current_index,
                        track_id=track_id,
                        state=state_name,
                        position_ms=position_ms,
                        queue_revision=1,
                        control_version=1,
                        version=1,
                        epoch=1,
                        playback_json=json.dumps({}, ensure_ascii=True),
                    )
            except IntegrityError:
                record = EmoPlaybackContext.get(
                    EmoPlaybackContext.playback_context_id == playback_context_id
                )
                return _resolveStrictPlaybackContextCreate(
                    record,
                    user_name,
                    fingerprint,
                )
            return _playback_context_payload(record), True
    finally:
        close_connection()


@_serialize_strict_playback_context_mutation
def closeStrictPlaybackContextState(playback_context_id, user_name):
    open_connection(reuse=True)
    try:
        with db.atomic():
            record = EmoPlaybackContext.get_or_none(
                EmoPlaybackContext.playback_context_id == playback_context_id
            )
            if record is None:
                return None
            if record.user_name != user_name:
                raise PermissionError("Playback context belongs to another user")
            if record.lifecycle != "closed":
                closed_at = now()
                record.lifecycle = "closed"
                record.closed_at = closed_at
                record.version = max(1, record.version) + 1
                record.updated_at = closed_at
                record.save(
                    only=(
                        EmoPlaybackContext.lifecycle,
                        EmoPlaybackContext.closed_at,
                        EmoPlaybackContext.version,
                        EmoPlaybackContext.updated_at,
                    )
                )
            return _playback_context_payload(record)
    finally:
        close_connection()


def _getStrictPlaybackContextRecord(playback_context_id, user_name):
    record = EmoPlaybackContext.get_or_none(
        EmoPlaybackContext.playback_context_id == playback_context_id
    )
    if record is None:
        return None
    if record.user_name != user_name:
        raise PermissionError("Playback context belongs to another user")
    if record.lifecycle == "closed":
        raise PlaybackContextClosedError(_playback_context_payload(record))
    return record


@_serialize_strict_playback_context_mutation
def mutateStrictPlaybackContextQueue(
    playback_context_id,
    user_name,
    authority_client_id,
    authority_device_session_id,
    queue_song_ids,
    current_index,
    position_ms,
    base_queue_revision,
    base_control_version=None,
):
    queue_song_ids = list(queue_song_ids)
    open_connection(reuse=True)
    try:
        with db.atomic():
            record = _getStrictPlaybackContextRecord(playback_context_id, user_name)
            if record is None:
                return None
            if (
                record.authority_client_id != authority_client_id
                or record.authority_device_session_id != authority_device_session_id
            ):
                raise PermissionError("Playback context authority identity mismatch")
            current = _playback_context_payload(record)
            if base_queue_revision != record.queue_revision:
                raise PlaybackContextStaleVersionError(current, "queueRevision")

            previous_index = record.current_index
            previous_track = record.track_id
            next_track = queue_song_ids[current_index]
            control_changed = (
                previous_index != current_index
                or previous_track != next_track
                or record.position_ms != position_ms
            )
            if control_changed:
                if base_control_version is None:
                    raise ValueError(
                        "baseControlVersion is required when canonical playback changes"
                    )
                if base_control_version != record.control_version:
                    raise PlaybackContextStaleVersionError(current, "controlVersion")
            elif (
                base_control_version is not None
                and base_control_version != record.control_version
            ):
                raise PlaybackContextStaleVersionError(current, "controlVersion")

            record.queue_json = json.dumps(queue_song_ids, ensure_ascii=True)
            record.current_index = current_index
            record.track_id = next_track
            record.position_ms = position_ms
            record.version += 1
            record.queue_revision += 1
            if control_changed:
                record.control_version += 1
            record.updated_at = now()
            record.save()
            return _playback_context_payload(record)
    finally:
        close_connection()


@_serialize_strict_playback_context_mutation
def mutateStrictPlaybackContextControl(
    playback_context_id,
    user_name,
    updated_by_client_id,
    action,
    base_control_version,
    base_queue_revision=None,
    position_ms=None,
    current_index=None,
):
    open_connection(reuse=True)
    try:
        with db.atomic():
            record = _getStrictPlaybackContextRecord(playback_context_id, user_name)
            if record is None:
                return None
            current = _playback_context_payload(record)
            if base_control_version != record.control_version:
                raise PlaybackContextStaleVersionError(current, "controlVersion")
            if (
                action == "queue.playItem"
                and base_queue_revision != record.queue_revision
            ):
                raise PlaybackContextStaleVersionError(current, "queueRevision")

            queue_song_ids = json.loads(record.queue_json)
            if current_index is not None:
                record.current_index = current_index
                record.track_id = queue_song_ids[current_index]
            if action in {"player.play", "queue.playItem", "player.next", "player.prev"}:
                record.state = "playing"
            elif action == "player.pause":
                record.state = "paused"
            if position_ms is not None:
                record.position_ms = position_ms
            record.origin_client_id = updated_by_client_id
            record.version += 1
            record.control_version += 1
            if action in {"queue.playItem", "player.next", "player.prev"}:
                record.queue_revision += 1
            record.updated_at = now()
            record.save()
            return _playback_context_payload(record)
    finally:
        close_connection()


def _writePlaybackContextState(
    playback_context_id,
    user_name,
    playback_context,
    create_missing=True,
    update_existing=True,
):
    payload = dict(playback_context)
    queue_song_ids = list(payload.get("queueSongIds") or [])
    queue_json = json.dumps(queue_song_ids, ensure_ascii=True)
    payload.pop("serverTimeMs", None)
    payload.pop("updatedAt", None)

    open_connection(reuse=True)
    try:
        record = EmoPlaybackContext.get_or_none(
            EmoPlaybackContext.playback_context_id == playback_context_id
        )
        if record is None:
            if not create_missing:
                return False
            EmoPlaybackContext.create(
                playback_context_id=playback_context_id,
                user_name=user_name,
                authority_client_id=payload.get("authorityClientId"),
                authority_device_session_id=payload.get("authorityDeviceSessionId")
                or payload.get("deviceSessionId"),
                origin_client_id=payload.get("originClientId"),
                timeline_id=payload.get("timelineId"),
                creation_fingerprint=payload.get("creationFingerprint"),
                lifecycle=payload.get("lifecycle") or "active",
                queue_json=queue_json,
                current_index=payload.get("currentIndex", 0),
                track_id=payload.get("trackId"),
                state=payload.get("state") or "stopped",
                position_ms=payload.get("positionMs") or 0,
                volume=payload.get("volume"),
                queue_revision=_payload_value_or_default(payload, "queueRevision", 1),
                control_version=_payload_value_or_default(payload, "controlVersion", 1),
                version=_payload_value_or_default(payload, "version", 1),
                epoch=_payload_value_or_default(payload, "epoch", 1),
                playback_json=json.dumps(payload, ensure_ascii=True),
                closed_at=payload.get("closedAt"),
            )
            return True

        if not update_existing:
            return False
        if record.user_name != user_name:
            raise PermissionError("Playback context belongs to another user")
        record.user_name = user_name
        record.authority_client_id = payload.get("authorityClientId")
        record.authority_device_session_id = payload.get(
            "authorityDeviceSessionId",
            record.authority_device_session_id,
        )
        record.origin_client_id = payload.get("originClientId")
        record.timeline_id = payload.get("timelineId", record.timeline_id)
        record.creation_fingerprint = payload.get(
            "creationFingerprint",
            record.creation_fingerprint,
        )
        record.lifecycle = payload.get("lifecycle", record.lifecycle)
        record.queue_json = queue_json
        record.current_index = payload.get("currentIndex", 0)
        record.track_id = payload.get("trackId")
        record.state = payload.get("state") or "stopped"
        record.position_ms = payload.get("positionMs") or 0
        record.volume = payload.get("volume")
        record.queue_revision = _payload_value_or_default(payload, "queueRevision", 1)
        record.control_version = _payload_value_or_default(payload, "controlVersion", 1)
        record.version = _payload_value_or_default(payload, "version", 1)
        record.epoch = _payload_value_or_default(payload, "epoch", 1)
        record.playback_json = json.dumps(payload, ensure_ascii=True)
        if payload.get("closedAt") is not None:
            record.closed_at = payload["closedAt"]
        record.updated_at = now()
        record.save()
        return True
    finally:
        close_connection()


def savePlaybackContextState(playback_context_id, user_name, playback_context):
    _writePlaybackContextState(
        playback_context_id,
        user_name,
        playback_context,
        create_missing=True,
        update_existing=True,
    )


def createPlaybackContextState(playback_context_id, user_name, playback_context):
    return _writePlaybackContextState(
        playback_context_id,
        user_name,
        playback_context,
        create_missing=True,
        update_existing=False,
    )


def updatePlaybackContextState(playback_context_id, user_name, playback_context):
    return _writePlaybackContextState(
        playback_context_id,
        user_name,
        playback_context,
        create_missing=False,
        update_existing=True,
    )


def listUserPlaybackContexts(user_name):
    open_connection(reuse=True)
    try:
        query = (
            EmoPlaybackContext.select()
            .where(EmoPlaybackContext.user_name == user_name)
            .order_by(EmoPlaybackContext.updated_at.desc())
        )
        return [_playback_context_payload(record) for record in query]
    finally:
        close_connection()


def listPlaybackContexts():
    open_connection(reuse=True)
    try:
        query = EmoPlaybackContext.select().order_by(
            EmoPlaybackContext.playback_context_id.asc()
        )
        return [_playback_context_payload(record) for record in query]
    finally:
        close_connection()


def failActivePlaybackHandoffsForRestart():
    open_connection(reuse=True)
    try:
        with db.atomic():
            query = EmoPlaybackHandoff.select().where(
                EmoPlaybackHandoff.status.in_(
                    ("preparing", "ready", "committed", "committing")
                )
            )
            reconciled = []
            for record in query:
                record.status = "failed"
                record.error_code = "server_restart"
                record.error_message = "Server restarted before handoff completed"
                record.updated_at = now()
                record.save()
                reconciled.append(record.handoff_id)
            return reconciled
    finally:
        close_connection()


def _device_playback_state_payload(record):
    payload = json.loads(record.playback_json) if record.playback_json else {}
    _strip_transient_playback_fields(payload)
    payload.update(
        {
            "playbackContextId": record.playback_context_id,
            "deviceSessionId": record.device_session_id,
            "sessionId": record.device_session_id,
            "userName": record.user_name,
            "sourceClientId": record.owner_client_id,
            "state": record.state,
            "trackId": record.track_id,
            "positionMs": record.position_ms,
            "volume": record.volume,
            "isAuthority": bool(record.is_authority),
            "mode": record.mode,
            "updatedAt": record.updated_at.timestamp(),
        }
    )
    payload.setdefault("serverUpdatedAtMs", int(record.updated_at.timestamp() * 1000))
    return payload


def getDevicePlaybackState(playback_context_id, client_id):
    open_connection(reuse=True)
    try:
        record = EmoDevicePlaybackState.get_or_none(
            (EmoDevicePlaybackState.playback_context_id == playback_context_id)
            & (EmoDevicePlaybackState.owner_client_id == client_id)
        )
        if record is None:
            return None
        return _device_playback_state_payload(record)
    finally:
        close_connection()


def getDevicePlaybackStates(playback_context_id):
    open_connection(reuse=True)
    try:
        payloads = []
        query = EmoDevicePlaybackState.select().where(
            EmoDevicePlaybackState.playback_context_id == playback_context_id
        )
        for record in query:
            payloads.append(_device_playback_state_payload(record))
        return payloads
    finally:
        close_connection()


def getPlaybackContextWithDeviceStates(playback_context_id):
    open_connection(reuse=True)
    try:
        context_record = EmoPlaybackContext.get_or_none(
            EmoPlaybackContext.playback_context_id == playback_context_id
        )
        if context_record is None:
            return None
        device_records = EmoDevicePlaybackState.select().where(
            EmoDevicePlaybackState.playback_context_id == playback_context_id
        )
        return {
            "playbackContext": _playback_context_payload(context_record),
            "deviceStates": [
                _device_playback_state_payload(record)
                for record in device_records
            ],
        }
    finally:
        close_connection()


def saveDevicePlaybackState(
    playback_context_id,
    device_session_id,
    user_name,
    client_id,
    playback_state,
    is_authority=False,
    mode="normal",
):
    payload = dict(playback_state)
    state_name = payload.get("state") or "unknown"
    track_id = payload.get("trackId")
    position_ms = payload.get("positionMs") or 0
    volume = payload.get("volume")
    payload.pop("updatedAt", None)
    payload.pop("serverTimeMs", None)

    open_connection(reuse=True)
    try:
        if is_authority:
            (
                EmoDevicePlaybackState.update(is_authority=0)
                .where(
                    (EmoDevicePlaybackState.playback_context_id == playback_context_id)
                    & (EmoDevicePlaybackState.owner_client_id != client_id)
                )
                .execute()
            )
        record = EmoDevicePlaybackState.get_or_none(
            (EmoDevicePlaybackState.playback_context_id == playback_context_id)
            & (EmoDevicePlaybackState.owner_client_id == client_id)
        )
        if record is None:
            EmoDevicePlaybackState.create(
                playback_context_id=playback_context_id,
                device_session_id=device_session_id,
                owner_client_id=client_id,
                user_name=user_name,
                state=state_name,
                track_id=track_id,
                position_ms=position_ms,
                volume=volume,
                is_authority=1 if is_authority else 0,
                mode=mode,
                playback_json=json.dumps(payload, ensure_ascii=True),
            )
            return

        record.device_session_id = device_session_id
        record.owner_client_id = client_id
        record.user_name = user_name
        record.state = state_name
        record.track_id = track_id
        record.position_ms = position_ms
        record.volume = volume
        record.is_authority = 1 if is_authority else 0
        record.mode = mode
        record.playback_json = json.dumps(payload, ensure_ascii=True)
        record.updated_at = now()
        record.save()
    finally:
        close_connection()


def deletePlaybackContext(playback_context_id):
    open_connection(reuse=True)
    try:
        deleted = (
            EmoPlaybackContext.delete()
            .where(EmoPlaybackContext.playback_context_id == playback_context_id)
            .execute()
        )
        if deleted:
            (
                EmoDevicePlaybackState.delete()
                .where(
                    EmoDevicePlaybackState.playback_context_id == playback_context_id
                )
                .execute()
            )
        return bool(deleted)
    finally:
        close_connection()


def expirePlaybackContext(playback_context_id, state_name="expired"):
    open_connection(reuse=True)
    try:
        record = EmoPlaybackContext.get_or_none(
            EmoPlaybackContext.playback_context_id == playback_context_id
        )
        if record is None:
            return None
        payload = json.loads(record.playback_json) if record.playback_json else {}
        record.state = state_name
        record.version += 1
        record.updated_at = now()
        payload["state"] = state_name
        payload["version"] = record.version
        record.playback_json = json.dumps(payload, ensure_ascii=True)
        record.save()
        return _playback_context_payload(record)
    finally:
        close_connection()


def getPlaybackHandoff(handoff_id):
    open_connection(reuse=True)
    try:
        record = EmoPlaybackHandoff.get_or_none(
            EmoPlaybackHandoff.handoff_id == handoff_id
        )
        if record is None:
            return None
        payload = json.loads(record.snapshot_json) if record.snapshot_json else {}
        return {
            "handoffId": record.handoff_id,
            "requestId": record.request_id,
            "playbackContextId": record.playback_context_id,
            "userName": record.user_name,
            "sourceClientId": record.source_client_id,
            "targetClientId": record.target_client_id,
            "originClientId": record.origin_client_id,
            "status": record.status,
            "baseControlVersion": record.base_control_version,
            "controlVersion": payload.get("handoffControlVersion"),
            "prepareId": payload.get("prepareId"),
            "completeExpiresAtMs": payload.get("completeExpiresAtMs"),
            "snapshot": payload,
            "errorCode": record.error_code,
            "errorMessage": record.error_message,
            "createdAt": record.created_at.timestamp(),
            "updatedAt": record.updated_at.timestamp(),
        }
    finally:
        close_connection()


def getPlaybackHandoffByRequest(user_name, origin_client_id, request_id):
    if not user_name or not origin_client_id or not request_id:
        return None
    open_connection(reuse=True)
    try:
        record = (
            EmoPlaybackHandoff.select()
            .where(
                (EmoPlaybackHandoff.user_name == user_name)
                & (EmoPlaybackHandoff.origin_client_id == origin_client_id)
                & (EmoPlaybackHandoff.request_id == request_id)
            )
            .order_by(EmoPlaybackHandoff.created_at.desc())
            .first()
        )
        if record is None:
            return None
        payload = json.loads(record.snapshot_json) if record.snapshot_json else {}
        return {
            "handoffId": record.handoff_id,
            "requestId": record.request_id,
            "playbackContextId": record.playback_context_id,
            "userName": record.user_name,
            "sourceClientId": record.source_client_id,
            "targetClientId": record.target_client_id,
            "originClientId": record.origin_client_id,
            "status": record.status,
            "baseControlVersion": record.base_control_version,
            "controlVersion": payload.get("handoffControlVersion"),
            "prepareId": payload.get("prepareId"),
            "completeExpiresAtMs": payload.get("completeExpiresAtMs"),
            "snapshot": payload,
            "errorCode": record.error_code,
            "errorMessage": record.error_message,
            "createdAt": record.created_at.timestamp(),
            "updatedAt": record.updated_at.timestamp(),
        }
    finally:
        close_connection()


def getActivePlaybackHandoffs(playback_context_id):
    if not playback_context_id:
        return []
    open_connection(reuse=True)
    try:
        query = (
            EmoPlaybackHandoff.select()
            .where(
                (EmoPlaybackHandoff.playback_context_id == playback_context_id)
                & EmoPlaybackHandoff.status.in_(("preparing", "ready", "committed"))
            )
            .order_by(EmoPlaybackHandoff.created_at.asc())
        )
        handoffs = []
        for record in query:
            payload = json.loads(record.snapshot_json) if record.snapshot_json else {}
            handoffs.append(
                {
                    "handoffId": record.handoff_id,
                    "requestId": record.request_id,
                    "playbackContextId": record.playback_context_id,
                    "userName": record.user_name,
                    "sourceClientId": record.source_client_id,
                    "targetClientId": record.target_client_id,
                    "originClientId": record.origin_client_id,
                    "status": record.status,
                    "baseControlVersion": record.base_control_version,
                    "controlVersion": payload.get("handoffControlVersion"),
                    "prepareId": payload.get("prepareId"),
                    "prepareExpiresAtMs": payload.get("prepareExpiresAtMs"),
                    "completeExpiresAtMs": payload.get("completeExpiresAtMs"),
                    "snapshot": payload,
                    "errorCode": record.error_code,
                    "errorMessage": record.error_message,
                    "createdAt": record.created_at.timestamp(),
                    "updatedAt": record.updated_at.timestamp(),
                }
            )
        return handoffs
    finally:
        close_connection()


def savePlaybackHandoff(handoff):
    payload = dict(handoff)
    handoff_id = payload.get("handoffId")
    snapshot = dict(payload.get("snapshot") or {})
    if payload.get("controlVersion") is not None:
        snapshot.setdefault("handoffControlVersion", payload.get("controlVersion"))
    if payload.get("prepareId") is not None:
        snapshot["prepareId"] = payload.get("prepareId")
    if payload.get("completeExpiresAtMs") is not None:
        snapshot["completeExpiresAtMs"] = payload.get("completeExpiresAtMs")
    open_connection(reuse=True)
    try:
        record = EmoPlaybackHandoff.get_or_none(
            EmoPlaybackHandoff.handoff_id == handoff_id
        )
        if record is None:
            EmoPlaybackHandoff.create(
                handoff_id=handoff_id,
                request_id=payload.get("requestId"),
                playback_context_id=payload.get("playbackContextId"),
                user_name=payload.get("userName"),
                source_client_id=payload.get("sourceClientId"),
                target_client_id=payload.get("targetClientId"),
                origin_client_id=payload.get("originClientId"),
                status=payload.get("status") or "preparing",
                base_control_version=payload.get("baseControlVersion") or 0,
                snapshot_json=json.dumps(snapshot, ensure_ascii=True),
                error_code=payload.get("errorCode"),
                error_message=payload.get("errorMessage"),
            )
            return

        record.request_id = payload.get("requestId")
        record.playback_context_id = payload.get("playbackContextId")
        record.user_name = payload.get("userName")
        record.source_client_id = payload.get("sourceClientId")
        record.target_client_id = payload.get("targetClientId")
        record.origin_client_id = payload.get("originClientId")
        record.status = payload.get("status") or "preparing"
        record.base_control_version = payload.get("baseControlVersion") or 0
        record.snapshot_json = json.dumps(snapshot, ensure_ascii=True)
        record.error_code = payload.get("errorCode")
        record.error_message = payload.get("errorMessage")
        record.updated_at = now()
        record.save()
    finally:
        close_connection()
