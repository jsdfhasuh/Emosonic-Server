import json
import time

from ..db import (
    EmoDevicePlaybackState,
    EmoLocalQueue,
    EmoPlaybackContext,
    EmoPlaybackHandoff,
    EmoPlaybackState,
    EmoSessionQueue,
    close_connection,
    now,
    open_connection,
)


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
    payload = dict(playback_context)
    payload["queueSongIds"] = list(payload.get("queueSongIds") or [])
    logical_volume = payload.pop("logicalVolume", None)
    stored_volume = payload.pop("volume", None)
    if logical_volume is None:
        logical_volume = stored_volume
    if logical_volume is not None:
        payload["logicalVolume"] = logical_volume
    payload.pop("sessionId", None)
    payload.pop("sourceClientId", None)
    return payload


def serializeDevicePlaybackStateV2(device_state):
    if device_state is None:
        return None
    payload = dict(device_state)
    client_id = (
        payload.get("clientId")
        or payload.get("ownerClientId")
        or payload.get("sourceClientId")
    )
    if client_id is not None:
        payload["clientId"] = client_id
    payload.pop("sessionId", None)
    payload.pop("sourceClientId", None)
    payload.pop("ownerClientId", None)
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
            "originClientId": record.origin_client_id,
            "sourceClientId": record.authority_client_id,
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
                origin_client_id=payload.get("originClientId"),
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
            )
            return True

        if not update_existing:
            return False
        if record.user_name != user_name:
            raise PermissionError("Playback context belongs to another user")
        record.user_name = user_name
        record.authority_client_id = payload.get("authorityClientId")
        record.origin_client_id = payload.get("originClientId")
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
