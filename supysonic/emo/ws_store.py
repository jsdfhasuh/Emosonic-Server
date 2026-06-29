import json

from ..db import (
    EmoLocalQueue,
    EmoPlaybackState,
    EmoSessionQueue,
    close_connection,
    now,
    open_connection,
)


def getQueueState(session_id):
    open_connection(reuse=True)
    try:
        record = EmoSessionQueue.get_or_none(EmoSessionQueue.session_id == session_id)
        if record is None:
            return None
        return {
            "sessionId": record.session_id,
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
        payload.pop("serverTimeMs", None)
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
            payload.pop("serverTimeMs", None)
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
