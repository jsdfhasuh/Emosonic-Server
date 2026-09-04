import hashlib
import json
import math
import threading
from contextlib import contextmanager
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from peewee import IntegrityError, SqliteDatabase

from ..db import (
    EmoBroadcastFence,
    EmoDevicePlaybackState,
    EmoFollowSafetyLease,
    EmoPlaybackContext,
    EmoPlaybackControlTransaction,
    EmoPlaybackHandoff,
    EmoPlaybackPrepareTransaction,
    close_connection,
    db,
    now,
    open_connection,
)
from .ws_store import strictAuthorityPairLockSet, strictPlaybackContextLockSet


MAX_USER_FOLLOW_SAFETY_LEASES = 256
FOLLOW_RECONNECT_GRACE_MS = 30_000
FOLLOW_SOURCE_RECOVERY_MS = 30_000
FOLLOW_SOURCE_PLAYING_FRESHNESS_MS = 2_000
FOLLOW_NONTERMINAL_PHASES = ("active", "reconnectGrace", "cleanupRequired")
HANDOFF_OCCUPANCY_STATUSES = ("preparing", "ready", "committed", "committing")


class FollowSafetyLeaseError(Exception):
    pass


class FollowSafetyLeaseConflictError(FollowSafetyLeaseError):
    def __init__(
        self,
        message: str,
        playback_context_id: Optional[str] = None,
    ):
        super().__init__(message)
        self.playback_context_id = playback_context_id


class FollowSafetyLeaseLimitError(FollowSafetyLeaseError):
    def __init__(self, limit: int = MAX_USER_FOLLOW_SAFETY_LEASES):
        super().__init__("Follow safety lease limit reached")
        self.limit = limit


_resource_locks: Dict[str, threading.RLock] = {}
_resource_locks_guard = threading.Lock()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _resource_key(kind: str, *parts: str) -> str:
    return "%s:%s" % (kind, _fingerprint([kind] + list(parts)))


def followPairResourceKey(
    user_name: str,
    client_id: str,
    device_session_id: str,
) -> str:
    return _resource_key("follow-pair", user_name, client_id, device_session_id)


def followContextResourceKey(user_name: str, playback_context_id: str) -> str:
    return _resource_key("follow-context", user_name, playback_context_id)


def followUserResourceKey(user_name: str) -> str:
    return _resource_key("follow-user", user_name)


@contextmanager
def followResourceLock(resource_keys: Iterable[str]) -> Iterator[None]:
    keys = sorted(set(resource_keys))
    locks = []
    with _resource_locks_guard:
        for key in keys:
            locks.append(_resource_locks.setdefault(key, threading.RLock()))
    for lock in locks:
        lock.acquire()
    try:
        yield
    finally:
        for lock in reversed(locks):
            lock.release()


@contextmanager
def _follow_transaction() -> Iterator[None]:
    if isinstance(db.obj, SqliteDatabase):
        with db.atomic("IMMEDIATE"):
            yield
        return
    with db.atomic():
        yield


def _require_non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a non-empty string" % field_name)
    return value


def _require_epoch(value: object, field_name: str) -> int:
    if type(value) is not int or value != 1:
        raise ValueError("%s must be the exact integer 1" % field_name)
    return value


def _require_integer(value: object, field_name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("%s must be an integer >= %d" % (field_name, minimum))
    return value


def _require_fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("%s must be a SHA-256 fingerprint" % field_name)
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("%s must be a SHA-256 fingerprint" % field_name) from exc
    return value


def _context_payload(record: EmoPlaybackContext) -> Dict[str, object]:
    queue_song_ids = json.loads(record.queue_json)
    payload: Dict[str, object] = {
        "playbackContextId": record.playback_context_id,
        "userName": record.user_name,
        "authorityClientId": record.authority_client_id,
        "authorityDeviceSessionId": record.authority_device_session_id,
        "lifecycle": record.lifecycle,
        "queueSongIds": queue_song_ids,
        "state": record.state,
        "positionMs": record.position_ms,
        "epoch": record.epoch,
        "version": record.version,
        "queueRevision": record.queue_revision,
        "controlVersion": record.control_version,
    }
    if queue_song_ids:
        payload["currentIndex"] = record.current_index
        payload["trackId"] = record.track_id
    return payload


def _device_payload(record: EmoDevicePlaybackState) -> Dict[str, object]:
    playback = json.loads(record.playback_json) if record.playback_json else {}
    payload: Dict[str, object] = {
        "playbackContextId": record.playback_context_id,
        "userName": record.user_name,
        "sourceClientId": record.owner_client_id,
        "deviceSessionId": record.device_session_id,
        "state": record.state,
        "trackId": record.track_id,
        "positionMs": record.position_ms,
        "contextEpoch": record.context_epoch,
        "appliedControlVersion": record.applied_control_version,
        "clientSeq": record.client_seq,
        "isAuthority": bool(record.is_authority),
        "connectionNonce": playback.get("_connectionNonce"),
        "serverUpdatedAtMs": playback.get(
            "serverUpdatedAtMs",
            int(record.updated_at.timestamp() * 1000),
        ),
        "positionSampledAtServerMs": playback.get(
            "positionSampledAtServerMs",
            int(record.updated_at.timestamp() * 1000),
        ),
        "playbackRate": playback.get("playbackRate", 1.0),
    }
    for field_name in ("volume", "muted"):
        value = playback.get(field_name)
        if value is not None:
            payload[field_name] = value
    return payload


def serializeFollowSafetyLease(record: EmoFollowSafetyLease) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "leaseId": str(record.id),
        "userName": record.user_name,
        "followerClientId": record.follower_client_id,
        "followerDeviceSessionId": record.follower_device_session_id,
        "followerConnectionNonce": record.follower_connection_nonce,
        "followerConnectionEpoch": record.follower_connection_epoch,
        "sourcePlaybackContextId": record.source_playback_context_id,
        "sourceAuthorityClientId": record.source_authority_client_id,
        "sourceAuthorityDeviceSessionId": (
            record.source_authority_device_session_id
        ),
        "sourceConnectionNonce": record.source_connection_nonce,
        "sourceConnectionEpoch": record.source_connection_epoch,
        "suspendedPlaybackContextId": record.suspended_playback_context_id,
        "suspendedAuthorityClientId": record.suspended_authority_client_id,
        "suspendedAuthorityDeviceSessionId": (
            record.suspended_authority_device_session_id
        ),
        "suspendedConnectionNonce": record.suspended_connection_nonce,
        "suspendedConnectionEpoch": record.suspended_connection_epoch,
        "suspendedEpoch": record.suspended_epoch,
        "suspendedVersion": record.suspended_version,
        "suspendedQueueRevision": record.suspended_queue_revision,
        "suspendedControlVersion": record.suspended_control_version,
        "suspendedAppliedControlVersion": (
            record.suspended_applied_control_version
        ),
        "phase": record.phase,
        "leaseFingerprint": record.lease_fingerprint,
        "startRequestFingerprint": record.start_request_fingerprint,
        "startAck": json.loads(record.start_ack_json),
        "createdAtMs": record.created_at_ms,
        "updatedAtMs": record.updated_at_ms,
    }
    optional_fields = (
        (
            "followReconnectGraceExpiresAtMs",
            record.follow_reconnect_grace_expires_at_ms,
        ),
        ("sourceRecoveryDeadlineAtMs", record.source_recovery_deadline_at_ms),
        ("stopRequestFingerprint", record.stop_request_fingerprint),
        ("cleanupFingerprint", record.cleanup_fingerprint),
    )
    for field_name, value in optional_fields:
        if value is not None:
            payload[field_name] = value
    return payload


def _lease_for_follower_query(
    user_name: str,
    follower_client_id: str,
    follower_device_session_id: str,
):
    return EmoFollowSafetyLease.get_or_none(
        (EmoFollowSafetyLease.user_name == user_name)
        & (EmoFollowSafetyLease.follower_client_id == follower_client_id)
        & (
            EmoFollowSafetyLease.follower_device_session_id
            == follower_device_session_id
        )
        & (EmoFollowSafetyLease.phase.in_(FOLLOW_NONTERMINAL_PHASES))
    )


def _is_durable_start_replay(
    record: EmoFollowSafetyLease,
    user_name: str,
    follower_client_id: str,
    follower_device_session_id: str,
    source_playback_context_id: str,
    suspended_playback_context_id: str,
    start_request_fingerprint: str,
) -> bool:
    return bool(
        record.user_name == user_name
        and record.follower_client_id == follower_client_id
        and record.follower_device_session_id == follower_device_session_id
        and record.source_playback_context_id == source_playback_context_id
        and record.suspended_playback_context_id == suspended_playback_context_id
        and record.start_request_fingerprint == start_request_fingerprint
    )


def _lease_lock_values(
    leases: Iterable[Dict[str, object]],
) -> Tuple[Tuple[str, ...], Tuple[Tuple[str, str, str], ...], Tuple[str, ...]]:
    context_ids = set()
    authority_pairs = set()
    resource_keys = set()
    for lease in leases:
        user_name = str(lease["userName"])
        source_context_id = str(lease["sourcePlaybackContextId"])
        suspended_context_id = str(lease["suspendedPlaybackContextId"])
        context_ids.update((source_context_id, suspended_context_id))
        authority_pairs.update(
            (
                (
                    user_name,
                    str(lease["sourceAuthorityClientId"]),
                    str(lease["sourceAuthorityDeviceSessionId"]),
                ),
                (
                    user_name,
                    str(lease["suspendedAuthorityClientId"]),
                    str(lease["suspendedAuthorityDeviceSessionId"]),
                ),
            )
        )
        resource_keys.update(
            (
                followUserResourceKey(user_name),
                followPairResourceKey(
                    user_name,
                    str(lease["followerClientId"]),
                    str(lease["followerDeviceSessionId"]),
                ),
                followContextResourceKey(user_name, source_context_id),
                followContextResourceKey(user_name, suspended_context_id),
            )
        )
    return (
        tuple(sorted(context_ids)),
        tuple(sorted(authority_pairs)),
        tuple(sorted(resource_keys)),
    )


@contextmanager
def _follow_lease_lock_set(
    leases: Iterable[Dict[str, object]],
) -> Iterator[None]:
    context_ids, authority_pairs, resource_keys = _lease_lock_values(leases)
    with strictPlaybackContextLockSet(context_ids), strictAuthorityPairLockSet(
        authority_pairs
    ), followResourceLock(resource_keys):
        yield


def _list_follow_source_leases(
    playback_context_ids: Iterable[str],
) -> List[Dict[str, object]]:
    context_ids = tuple(sorted(set(playback_context_ids)))
    open_connection(reuse=True)
    try:
        records = EmoFollowSafetyLease.select().where(
            (
                EmoFollowSafetyLease.source_playback_context_id.in_(
                    context_ids
                )
            )
            & (
                EmoFollowSafetyLease.phase.in_(
                    FOLLOW_NONTERMINAL_PHASES
                )
            )
        )
        return [serializeFollowSafetyLease(record) for record in records]
    finally:
        close_connection()


@contextmanager
def followSourceContextCloseLock(
    playback_context_ids: Iterable[str],
) -> Iterator[None]:
    context_ids = tuple(sorted(set(playback_context_ids)))
    for playback_context_id in context_ids:
        _require_non_empty_string(playback_context_id, "playbackContextId")
    leases = _list_follow_source_leases(context_ids)
    lease_context_ids, authority_pairs, resource_keys = _lease_lock_values(
        leases
    )
    all_context_ids = tuple(sorted(set(context_ids) | set(lease_context_ids)))
    with strictPlaybackContextLockSet(
        all_context_ids
    ), strictAuthorityPairLockSet(authority_pairs), followResourceLock(
        resource_keys
    ):
        yield


def _lease_fingerprint_material(
    record: EmoFollowSafetyLease,
) -> Dict[str, object]:
    return {
        "userName": record.user_name,
        "followerClientId": record.follower_client_id,
        "followerDeviceSessionId": record.follower_device_session_id,
        "followerConnectionNonce": record.follower_connection_nonce,
        "followerConnectionEpoch": record.follower_connection_epoch,
        "sourcePlaybackContextId": record.source_playback_context_id,
        "sourceAuthorityClientId": record.source_authority_client_id,
        "sourceAuthorityDeviceSessionId": (
            record.source_authority_device_session_id
        ),
        "sourceConnectionNonce": record.source_connection_nonce,
        "sourceConnectionEpoch": record.source_connection_epoch,
        "suspendedPlaybackContextId": record.suspended_playback_context_id,
        "suspendedAuthorityClientId": record.suspended_authority_client_id,
        "suspendedAuthorityDeviceSessionId": (
            record.suspended_authority_device_session_id
        ),
        "suspendedConnectionNonce": record.suspended_connection_nonce,
        "suspendedConnectionEpoch": record.suspended_connection_epoch,
        "startAck": json.loads(record.start_ack_json),
    }


def _refresh_lease_fingerprint(record: EmoFollowSafetyLease) -> None:
    record.lease_fingerprint = _fingerprint(_lease_fingerprint_material(record))


def _save_lease(record: EmoFollowSafetyLease, server_time_ms: int) -> None:
    record.updated_at_ms = server_time_ms
    record.updated_at = now()
    record.save()


def getFollowSafetyLeaseForFollower(
    user_name: str,
    follower_client_id: str,
    follower_device_session_id: str,
) -> Optional[Dict[str, object]]:
    _require_non_empty_string(user_name, "userName")
    _require_non_empty_string(follower_client_id, "followerClientId")
    _require_non_empty_string(
        follower_device_session_id,
        "followerDeviceSessionId",
    )
    open_connection(reuse=True)
    try:
        record = _lease_for_follower_query(
            user_name,
            follower_client_id,
            follower_device_session_id,
        )
        return None if record is None else serializeFollowSafetyLease(record)
    finally:
        close_connection()


def getFollowSafetyLeaseForSuspendedContext(
    user_name: str,
    playback_context_id: str,
) -> Optional[Dict[str, object]]:
    _require_non_empty_string(user_name, "userName")
    _require_non_empty_string(playback_context_id, "playbackContextId")
    open_connection(reuse=True)
    try:
        record = EmoFollowSafetyLease.get_or_none(
            (EmoFollowSafetyLease.user_name == user_name)
            & (
                EmoFollowSafetyLease.suspended_playback_context_id
                == playback_context_id
            )
            & (EmoFollowSafetyLease.phase.in_(FOLLOW_NONTERMINAL_PHASES))
        )
        return None if record is None else serializeFollowSafetyLease(record)
    finally:
        close_connection()


def _require_follow_profile_resources_available(
    user_name: str,
    source_playback_context_id: str,
    suspended_playback_context_id: str,
    follower_client_id: str,
    follower_device_session_id: str,
    suspended_epoch: int,
) -> None:
    source_is_suspended = EmoFollowSafetyLease.get_or_none(
        (EmoFollowSafetyLease.user_name == user_name)
        & (
            EmoFollowSafetyLease.suspended_playback_context_id
            == source_playback_context_id
        )
        & (EmoFollowSafetyLease.phase.in_(FOLLOW_NONTERMINAL_PHASES))
    )
    if source_is_suspended is not None:
        raise FollowSafetyLeaseConflictError(
            "Follow source Context is occupied by another Follow",
            source_playback_context_id,
        )

    active_prepare = EmoPlaybackPrepareTransaction.get_or_none(
        (
            EmoPlaybackPrepareTransaction.playback_context_id
            == suspended_playback_context_id
        )
        & (EmoPlaybackPrepareTransaction.epoch == suspended_epoch)
        & (EmoPlaybackPrepareTransaction.status == "preparing")
    )
    if active_prepare is not None:
        raise FollowSafetyLeaseConflictError(
            "Follow suspended Context has an active prepare",
            suspended_playback_context_id,
        )

    active_handoffs = EmoPlaybackHandoff.select().where(
        (EmoPlaybackHandoff.user_name == user_name)
        & (EmoPlaybackHandoff.status.in_(HANDOFF_OCCUPANCY_STATUSES))
        & (
            (
                EmoPlaybackHandoff.playback_context_id
                == suspended_playback_context_id
            )
            | (EmoPlaybackHandoff.target_client_id == follower_client_id)
        )
    )
    for active_handoff in active_handoffs:
        snapshot = (
            json.loads(active_handoff.snapshot_json)
            if active_handoff.snapshot_json
            else {}
        )
        target_device_session_id = snapshot.get("targetDeviceSessionId")
        target_standby_context_id = snapshot.get(
            "targetStandbyPlaybackContextId"
        )
        occupies_suspended_context = (
            active_handoff.playback_context_id
            == suspended_playback_context_id
            or target_standby_context_id == suspended_playback_context_id
            or (
                active_handoff.target_client_id == follower_client_id
                and (
                    target_device_session_id is None
                    or target_device_session_id
                    == follower_device_session_id
                )
            )
        )
        if occupies_suspended_context:
            raise FollowSafetyLeaseConflictError(
                "Follow suspended Context has an active Handoff",
                suspended_playback_context_id,
            )

    broadcast_fence = EmoBroadcastFence.get_or_none(
        (EmoBroadcastFence.user_name == user_name)
        & (
            (
                EmoBroadcastFence.playback_context_id
                == suspended_playback_context_id
            )
            | (
                (EmoBroadcastFence.client_id == follower_client_id)
                & (
                    EmoBroadcastFence.device_session_id
                    == follower_device_session_id
                )
            )
        )
    )
    if broadcast_fence is not None:
        raise FollowSafetyLeaseConflictError(
            "Follow suspended Context has a Broadcast fence",
            suspended_playback_context_id,
        )

    source_ordinary_fence = EmoBroadcastFence.get_or_none(
        (EmoBroadcastFence.user_name == user_name)
        & (
            EmoBroadcastFence.playback_context_id
            == source_playback_context_id
        )
        & (EmoBroadcastFence.role == "ordinary")
    )
    if source_ordinary_fence is not None:
        raise FollowSafetyLeaseConflictError(
            "Follow source Context is a Broadcast ordinary participant",
            source_playback_context_id,
        )


def listFollowSafetyLeases(
    user_name: Optional[str] = None,
) -> List[Dict[str, object]]:
    if user_name is not None:
        _require_non_empty_string(user_name, "userName")
    open_connection(reuse=True)
    try:
        query = EmoFollowSafetyLease.select().where(
            EmoFollowSafetyLease.phase.in_(FOLLOW_NONTERMINAL_PHASES)
        )
        if user_name is not None:
            query = query.where(EmoFollowSafetyLease.user_name == user_name)
        query = query.order_by(
            EmoFollowSafetyLease.user_name,
            EmoFollowSafetyLease.follower_client_id,
            EmoFollowSafetyLease.follower_device_session_id,
        )
        return [serializeFollowSafetyLease(record) for record in query]
    finally:
        close_connection()


def listFollowSafetyLeasesForPhysicalGeneration(
    user_name: str,
    client_id: str,
    device_session_id: str,
    connection_nonce: str,
    connection_epoch: int,
) -> List[Dict[str, object]]:
    for field_name, value in (
        ("userName", user_name),
        ("clientId", client_id),
        ("deviceSessionId", device_session_id),
        ("connectionNonce", connection_nonce),
    ):
        _require_non_empty_string(value, field_name)
    _require_epoch(connection_epoch, "connectionEpoch")
    open_connection(reuse=True)
    try:
        follower_match = (
            (EmoFollowSafetyLease.follower_client_id == client_id)
            & (
                EmoFollowSafetyLease.follower_device_session_id
                == device_session_id
            )
            & (
                EmoFollowSafetyLease.follower_connection_nonce
                == connection_nonce
            )
            & (
                EmoFollowSafetyLease.follower_connection_epoch
                == connection_epoch
            )
        )
        source_match = (
            (EmoFollowSafetyLease.source_authority_client_id == client_id)
            & (
                EmoFollowSafetyLease.source_authority_device_session_id
                == device_session_id
            )
            & (EmoFollowSafetyLease.source_connection_nonce == connection_nonce)
            & (EmoFollowSafetyLease.source_connection_epoch == connection_epoch)
        )
        records = (
            EmoFollowSafetyLease.select()
            .where(
                (EmoFollowSafetyLease.user_name == user_name)
                & (EmoFollowSafetyLease.phase.in_(FOLLOW_NONTERMINAL_PHASES))
                & (follower_match | source_match)
            )
            .order_by(EmoFollowSafetyLease.id)
        )
        return [serializeFollowSafetyLease(record) for record in records]
    finally:
        close_connection()


def markFollowConnectionUnavailable(
    user_name: str,
    client_id: str,
    device_session_id: str,
    connection_nonce: str,
    connection_epoch: int,
    server_time_ms: int,
    reconnect_grace_ms: int = FOLLOW_RECONNECT_GRACE_MS,
    source_recovery_ms: int = FOLLOW_SOURCE_RECOVERY_MS,
) -> List[Dict[str, object]]:
    leases = listFollowSafetyLeasesForPhysicalGeneration(
        user_name,
        client_id,
        device_session_id,
        connection_nonce,
        connection_epoch,
    )
    _require_integer(server_time_ms, "serverTimeMs")
    _require_integer(reconnect_grace_ms, "reconnectGraceMs", 1)
    _require_integer(source_recovery_ms, "sourceRecoveryMs", 1)
    if not leases:
        return []
    lease_ids = [lease["leaseId"] for lease in leases]
    updated = []
    with _follow_lease_lock_set(leases):
        open_connection(reuse=True)
        try:
            with _follow_transaction():
                records = list(
                    EmoFollowSafetyLease.select()
                    .where(
                        (EmoFollowSafetyLease.id.in_(lease_ids))
                        & (
                            EmoFollowSafetyLease.phase.in_(
                                FOLLOW_NONTERMINAL_PHASES
                            )
                        )
                    )
                    .order_by(EmoFollowSafetyLease.id)
                )
                for record in records:
                    changed = False
                    follower_match = (
                        record.user_name == user_name
                        and record.follower_client_id == client_id
                        and record.follower_device_session_id
                        == device_session_id
                        and record.follower_connection_nonce
                        == connection_nonce
                        and record.follower_connection_epoch
                        == connection_epoch
                    )
                    source_match = (
                        record.user_name == user_name
                        and record.source_authority_client_id == client_id
                        and record.source_authority_device_session_id
                        == device_session_id
                        and record.source_connection_nonce == connection_nonce
                        and record.source_connection_epoch == connection_epoch
                    )
                    if follower_match and record.phase == "active":
                        record.phase = "reconnectGrace"
                        record.follow_reconnect_grace_expires_at_ms = (
                            server_time_ms + reconnect_grace_ms
                        )
                        changed = True
                    if (
                        source_match
                        and record.phase != "cleanupRequired"
                        and record.source_recovery_deadline_at_ms is None
                    ):
                        record.source_recovery_deadline_at_ms = (
                            server_time_ms + source_recovery_ms
                        )
                        changed = True
                    if changed:
                        _save_lease(record, server_time_ms)
                    updated.append(serializeFollowSafetyLease(record))
        finally:
            close_connection()
    return updated


def resumeFollowSafetyLease(
    user_name: str,
    follower_client_id: str,
    follower_device_session_id: str,
    follower_connection_nonce: str,
    follower_connection_epoch: int,
    source_playback_context_id: str,
    start_request_fingerprint: str,
    server_time_ms: int,
    pre_mutation_validator: Optional[
        Callable[[Dict[str, object]], object]
    ] = None,
) -> Optional[Dict[str, object]]:
    for field_name, value in (
        ("userName", user_name),
        ("followerClientId", follower_client_id),
        ("followerDeviceSessionId", follower_device_session_id),
        ("followerConnectionNonce", follower_connection_nonce),
        ("sourcePlaybackContextId", source_playback_context_id),
    ):
        _require_non_empty_string(value, field_name)
    _require_epoch(follower_connection_epoch, "followerConnectionEpoch")
    _require_fingerprint(start_request_fingerprint, "startRequestFingerprint")
    _require_integer(server_time_ms, "serverTimeMs")
    existing = getFollowSafetyLeaseForFollower(
        user_name,
        follower_client_id,
        follower_device_session_id,
    )
    if existing is None:
        return None
    expired_context_id = None
    result = None
    with _follow_lease_lock_set((existing,)):
        open_connection(reuse=True)
        try:
            with _follow_transaction():
                record = _lease_for_follower_query(
                    user_name,
                    follower_client_id,
                    follower_device_session_id,
                )
                if record is None:
                    return None
                if not _is_durable_start_replay(
                    record,
                    user_name,
                    follower_client_id,
                    follower_device_session_id,
                    source_playback_context_id,
                    record.suspended_playback_context_id,
                    start_request_fingerprint,
                ):
                    raise FollowSafetyLeaseConflictError(
                        "Follow start does not match the durable safety lease",
                        record.suspended_playback_context_id,
                    )
                if record.phase == "cleanupRequired":
                    raise FollowSafetyLeaseConflictError(
                        "Follow safety lease requires cleanup",
                        record.suspended_playback_context_id,
                    )
                if (
                    record.phase == "reconnectGrace"
                    and record.follow_reconnect_grace_expires_at_ms is not None
                    and record.follow_reconnect_grace_expires_at_ms
                    <= server_time_ms
                ):
                    record.phase = "cleanupRequired"
                    record.follow_reconnect_grace_expires_at_ms = None
                    record.source_recovery_deadline_at_ms = None
                    _save_lease(record, server_time_ms)
                    expired_context_id = record.suspended_playback_context_id
                else:
                    current = serializeFollowSafetyLease(record)
                    if pre_mutation_validator is not None:
                        pre_mutation_validator(dict(current))
                    if record.phase == "active":
                        if (
                            record.follower_connection_nonce
                            != follower_connection_nonce
                            or record.follower_connection_epoch
                            != follower_connection_epoch
                        ):
                            raise FollowSafetyLeaseConflictError(
                                "Active Follow belongs to another physical generation",
                                record.suspended_playback_context_id,
                            )
                        result = current
                    else:
                        if record.stop_request_fingerprint is not None:
                            raise FollowSafetyLeaseConflictError(
                                "Follow stop cleanup is pending",
                                record.suspended_playback_context_id,
                            )
                        record.follower_connection_nonce = (
                            follower_connection_nonce
                        )
                        record.follower_connection_epoch = (
                            follower_connection_epoch
                        )
                        record.phase = "active"
                        record.follow_reconnect_grace_expires_at_ms = None
                        _refresh_lease_fingerprint(record)
                        _save_lease(record, server_time_ms)
                        result = serializeFollowSafetyLease(record)
        finally:
            close_connection()
    if expired_context_id is not None:
        raise FollowSafetyLeaseConflictError(
            "Follow reconnect grace expired; cleanup is required",
            expired_context_id,
        )
    return result


def refreshFollowSourceGeneration(
    user_name: str,
    source_playback_context_id: str,
    source_authority_client_id: str,
    source_authority_device_session_id: str,
    source_connection_nonce: str,
    source_connection_epoch: int,
    server_time_ms: int,
) -> List[Dict[str, object]]:
    for field_name, value in (
        ("userName", user_name),
        ("sourcePlaybackContextId", source_playback_context_id),
        ("sourceAuthorityClientId", source_authority_client_id),
        (
            "sourceAuthorityDeviceSessionId",
            source_authority_device_session_id,
        ),
        ("sourceConnectionNonce", source_connection_nonce),
    ):
        _require_non_empty_string(value, field_name)
    _require_epoch(source_connection_epoch, "sourceConnectionEpoch")
    _require_integer(server_time_ms, "serverTimeMs")
    open_connection(reuse=True)
    try:
        leases = [
            serializeFollowSafetyLease(record)
            for record in EmoFollowSafetyLease.select().where(
                (EmoFollowSafetyLease.user_name == user_name)
                & (
                    EmoFollowSafetyLease.source_playback_context_id
                    == source_playback_context_id
                )
                & (
                    EmoFollowSafetyLease.phase.in_(
                        FOLLOW_NONTERMINAL_PHASES
                    )
                )
            )
        ]
    finally:
        close_connection()
    if not leases:
        return []
    lease_ids = [lease["leaseId"] for lease in leases]
    refreshed = []
    with _follow_lease_lock_set(leases):
        open_connection(reuse=True)
        try:
            with _follow_transaction():
                records = list(
                    EmoFollowSafetyLease.select()
                    .where(
                        (EmoFollowSafetyLease.id.in_(lease_ids))
                        & (
                            EmoFollowSafetyLease.phase.in_(
                                FOLLOW_NONTERMINAL_PHASES
                            )
                        )
                    )
                    .order_by(EmoFollowSafetyLease.id)
                )
                for record in records:
                    if (
                        record.source_authority_client_id
                        != source_authority_client_id
                        or record.source_authority_device_session_id
                        != source_authority_device_session_id
                    ):
                        continue
                    if record.phase != "cleanupRequired":
                        changed = (
                            record.source_connection_nonce
                            != source_connection_nonce
                            or record.source_connection_epoch
                            != source_connection_epoch
                            or record.source_recovery_deadline_at_ms is not None
                        )
                        if changed:
                            record.source_connection_nonce = (
                                source_connection_nonce
                            )
                            record.source_connection_epoch = (
                                source_connection_epoch
                            )
                            record.source_recovery_deadline_at_ms = None
                            _refresh_lease_fingerprint(record)
                            _save_lease(record, server_time_ms)
                    refreshed.append(serializeFollowSafetyLease(record))
        finally:
            close_connection()
    return refreshed


def markFollowSourceContextsClosed(
    playback_context_ids: Iterable[str],
    server_time_ms: int,
) -> List[Dict[str, object]]:
    context_ids = tuple(sorted(set(playback_context_ids)))
    for playback_context_id in context_ids:
        _require_non_empty_string(playback_context_id, "playbackContextId")
    _require_integer(server_time_ms, "serverTimeMs")
    if not context_ids:
        return []
    with followSourceContextCloseLock(context_ids):
        open_connection(reuse=True)
        try:
            with _follow_transaction():
                return markFollowSourceContextsClosedInTransaction(
                    context_ids,
                    server_time_ms,
                )
        finally:
            close_connection()


def markFollowSourceContextsClosedInTransaction(
    playback_context_ids: Iterable[str],
    server_time_ms: int,
) -> List[Dict[str, object]]:
    context_ids = tuple(sorted(set(playback_context_ids)))
    for playback_context_id in context_ids:
        _require_non_empty_string(playback_context_id, "playbackContextId")
    _require_integer(server_time_ms, "serverTimeMs")
    if not db.in_transaction():
        raise RuntimeError(
            "Follow source close mutation requires an active database transaction"
        )
    records = list(
        EmoFollowSafetyLease.select()
        .where(
            (
                EmoFollowSafetyLease.source_playback_context_id.in_(
                    context_ids
                )
            )
            & (
                EmoFollowSafetyLease.phase.in_(
                    FOLLOW_NONTERMINAL_PHASES
                )
            )
        )
        .order_by(EmoFollowSafetyLease.id)
    )
    closed = []
    for record in records:
        record.phase = "cleanupRequired"
        record.follow_reconnect_grace_expires_at_ms = None
        record.source_recovery_deadline_at_ms = None
        _save_lease(record, server_time_ms)
        closed.append(serializeFollowSafetyLease(record))
    return closed


def stopFollowSafetyLease(
    user_name: str,
    follower_client_id: str,
    follower_device_session_id: str,
    source_playback_context_id: str,
    stop_request_fingerprint: str,
    server_time_ms: int,
    pre_mutation_validator: Optional[
        Callable[[Dict[str, object]], object]
    ] = None,
) -> Optional[Dict[str, object]]:
    for field_name, value in (
        ("userName", user_name),
        ("followerClientId", follower_client_id),
        ("followerDeviceSessionId", follower_device_session_id),
        ("sourcePlaybackContextId", source_playback_context_id),
    ):
        _require_non_empty_string(value, field_name)
    _require_fingerprint(stop_request_fingerprint, "stopRequestFingerprint")
    _require_integer(server_time_ms, "serverTimeMs")
    existing = getFollowSafetyLeaseForFollower(
        user_name,
        follower_client_id,
        follower_device_session_id,
    )
    if existing is None:
        return None
    with _follow_lease_lock_set((existing,)):
        open_connection(reuse=True)
        try:
            with _follow_transaction():
                record = _lease_for_follower_query(
                    user_name,
                    follower_client_id,
                    follower_device_session_id,
                )
                if record is None:
                    return None
                if (
                    record.source_playback_context_id
                    != source_playback_context_id
                ):
                    raise FollowSafetyLeaseConflictError(
                        "Follow stop targets another source Context",
                        record.suspended_playback_context_id,
                    )
                current = serializeFollowSafetyLease(record)
                if pre_mutation_validator is not None:
                    pre_mutation_validator(dict(current))
                bound_fingerprint = _fingerprint(
                    {
                        "action": "follow.stop",
                        "leaseFingerprint": record.lease_fingerprint,
                        "requestFingerprint": stop_request_fingerprint,
                    }
                )
                if (
                    record.stop_request_fingerprint is not None
                    and record.stop_request_fingerprint != bound_fingerprint
                ):
                    raise FollowSafetyLeaseConflictError(
                        "Follow stop fingerprint conflicts with pending cleanup",
                        record.suspended_playback_context_id,
                    )
                record.stop_request_fingerprint = bound_fingerprint
                if record.phase == "cleanupRequired":
                    record.cleanup_fingerprint = bound_fingerprint
                _save_lease(record, server_time_ms)
                stopped = serializeFollowSafetyLease(record)
                record.delete_instance()
                return stopped
        finally:
            close_connection()


def createFollowSafetyLease(
    user_name: str,
    follower_client_id: str,
    follower_device_session_id: str,
    follower_connection_nonce: str,
    follower_connection_epoch: int,
    source_playback_context_id: str,
    source_authority_client_id: str,
    source_authority_device_session_id: str,
    source_connection_nonce: str,
    source_connection_epoch: int,
    suspended_playback_context_id: str,
    suspended_authority_client_id: str,
    suspended_authority_device_session_id: str,
    suspended_connection_nonce: str,
    suspended_connection_epoch: int,
    start_request_fingerprint: str,
    server_time_ms: int,
    pre_mutation_validator: Optional[
        Callable[
            [
                Dict[str, object],
                Dict[str, object],
                Dict[str, object],
                Dict[str, object],
            ],
            object,
        ]
    ] = None,
) -> Tuple[Dict[str, object], bool]:
    for field_name, value in (
        ("userName", user_name),
        ("followerClientId", follower_client_id),
        ("followerDeviceSessionId", follower_device_session_id),
        ("followerConnectionNonce", follower_connection_nonce),
        ("sourcePlaybackContextId", source_playback_context_id),
        ("sourceAuthorityClientId", source_authority_client_id),
        (
            "sourceAuthorityDeviceSessionId",
            source_authority_device_session_id,
        ),
        ("sourceConnectionNonce", source_connection_nonce),
        ("suspendedPlaybackContextId", suspended_playback_context_id),
        ("suspendedAuthorityClientId", suspended_authority_client_id),
        (
            "suspendedAuthorityDeviceSessionId",
            suspended_authority_device_session_id,
        ),
        ("suspendedConnectionNonce", suspended_connection_nonce),
    ):
        _require_non_empty_string(value, field_name)
    for field_name, value in (
        ("followerConnectionEpoch", follower_connection_epoch),
        ("sourceConnectionEpoch", source_connection_epoch),
        ("suspendedConnectionEpoch", suspended_connection_epoch),
    ):
        _require_epoch(value, field_name)
    _require_fingerprint(start_request_fingerprint, "startRequestFingerprint")
    _require_integer(server_time_ms, "serverTimeMs")
    if source_playback_context_id == suspended_playback_context_id:
        raise FollowSafetyLeaseConflictError(
            "Follow source and suspended Context must differ",
            source_playback_context_id,
        )

    context_ids = (
        source_playback_context_id,
        suspended_playback_context_id,
    )
    authority_pairs = (
        (
            user_name,
            source_authority_client_id,
            source_authority_device_session_id,
        ),
        (
            user_name,
            suspended_authority_client_id,
            suspended_authority_device_session_id,
        ),
    )
    resource_keys = (
        followUserResourceKey(user_name),
        followPairResourceKey(
            user_name,
            follower_client_id,
            follower_device_session_id,
        ),
        followContextResourceKey(user_name, source_playback_context_id),
        followContextResourceKey(user_name, suspended_playback_context_id),
    )
    with strictPlaybackContextLockSet(context_ids), strictAuthorityPairLockSet(
        authority_pairs
    ), followResourceLock(resource_keys):
        open_connection(reuse=True)
        try:
            with _follow_transaction():
                existing = _lease_for_follower_query(
                    user_name,
                    follower_client_id,
                    follower_device_session_id,
                )
                if existing is not None:
                    if _is_durable_start_replay(
                        existing,
                        user_name,
                        follower_client_id,
                        follower_device_session_id,
                        source_playback_context_id,
                        suspended_playback_context_id,
                        start_request_fingerprint,
                    ):
                        return serializeFollowSafetyLease(existing), False
                    raise FollowSafetyLeaseConflictError(
                        "Follower already has another Follow safety lease",
                        existing.suspended_playback_context_id,
                    )

                source_record = EmoPlaybackContext.get_or_none(
                    EmoPlaybackContext.playback_context_id
                    == source_playback_context_id
                )
                suspended_record = EmoPlaybackContext.get_or_none(
                    EmoPlaybackContext.playback_context_id
                    == suspended_playback_context_id
                )
                if source_record is None or suspended_record is None:
                    raise FollowSafetyLeaseConflictError(
                        "Follow Context is unavailable",
                        (
                            source_playback_context_id
                            if source_record is None
                            else suspended_playback_context_id
                        ),
                    )
                for record in (source_record, suspended_record):
                    if record.user_name != user_name or record.lifecycle != "active":
                        raise FollowSafetyLeaseConflictError(
                            "Follow Context is unavailable",
                            record.playback_context_id,
                        )
                if (
                    source_record.authority_client_id
                    != source_authority_client_id
                    or source_record.authority_device_session_id
                    != source_authority_device_session_id
                ):
                    raise FollowSafetyLeaseConflictError(
                        "Follow source authority changed",
                        source_playback_context_id,
                    )
                if (
                    suspended_record.authority_client_id
                    != suspended_authority_client_id
                    or suspended_record.authority_device_session_id
                    != suspended_authority_device_session_id
                    or suspended_authority_client_id != follower_client_id
                    or suspended_authority_device_session_id
                    != follower_device_session_id
                ):
                    raise FollowSafetyLeaseConflictError(
                        "Follow suspended authority changed",
                        suspended_playback_context_id,
                    )

                _require_follow_profile_resources_available(
                    user_name,
                    source_playback_context_id,
                    suspended_playback_context_id,
                    follower_client_id,
                    follower_device_session_id,
                    suspended_record.epoch,
                )

                source_device = EmoDevicePlaybackState.get_or_none(
                    (
                        EmoDevicePlaybackState.playback_context_id
                        == source_playback_context_id
                    )
                    & (
                        EmoDevicePlaybackState.owner_client_id
                        == source_authority_client_id
                    )
                )
                suspended_device = EmoDevicePlaybackState.get_or_none(
                    (
                        EmoDevicePlaybackState.playback_context_id
                        == suspended_playback_context_id
                    )
                    & (
                        EmoDevicePlaybackState.owner_client_id
                        == suspended_authority_client_id
                    )
                )
                if source_device is None or suspended_device is None:
                    raise FollowSafetyLeaseConflictError(
                        "Follow authority actual fact is unavailable",
                        (
                            source_playback_context_id
                            if source_device is None
                            else suspended_playback_context_id
                        ),
                    )
                source_playback = (
                    json.loads(source_device.playback_json)
                    if source_device.playback_json
                    else {}
                )
                suspended_playback = (
                    json.loads(suspended_device.playback_json)
                    if suspended_device.playback_json
                    else {}
                )
                if (
                    source_device.device_session_id
                    != source_authority_device_session_id
                    or source_device.context_epoch != source_record.epoch
                    or source_device.client_seq < 1
                    or source_device.applied_control_version
                    != source_record.control_version
                    or source_playback.get("_connectionNonce")
                    != source_connection_nonce
                ):
                    raise FollowSafetyLeaseConflictError(
                        "Follow source actual fact is not settled",
                        source_playback_context_id,
                    )
                if (
                    suspended_device.device_session_id
                    != suspended_authority_device_session_id
                    or suspended_device.context_epoch != suspended_record.epoch
                    or suspended_device.client_seq < 1
                    or suspended_device.applied_control_version
                    != suspended_record.control_version
                    or suspended_playback.get("_connectionNonce")
                    != suspended_connection_nonce
                ):
                    raise FollowSafetyLeaseConflictError(
                        "Follow suspended actual fact is not settled",
                        suspended_playback_context_id,
                    )
                for context_record in (source_record, suspended_record):
                    pending_exists = (
                        EmoPlaybackControlTransaction.select()
                        .where(
                            (
                                EmoPlaybackControlTransaction.playback_context_id
                                == context_record.playback_context_id
                            )
                            & (
                                EmoPlaybackControlTransaction.epoch
                                == context_record.epoch
                            )
                            & (
                                EmoPlaybackControlTransaction.status
                                == "pending"
                            )
                        )
                        .exists()
                    )
                    if pending_exists:
                        raise FollowSafetyLeaseConflictError(
                            "Follow Context has pending control",
                            context_record.playback_context_id,
                        )

                start_ack = {
                    "action": "follow.start",
                    "status": "active",
                    "sourcePlaybackContextId": source_playback_context_id,
                    "suspendedPlaybackContextId": suspended_playback_context_id,
                    "suspendedAuthorityClientId": (
                        suspended_record.authority_client_id
                    ),
                    "suspendedAuthorityDeviceSessionId": (
                        suspended_record.authority_device_session_id
                    ),
                    "suspendedEpoch": suspended_record.epoch,
                    "suspendedVersion": suspended_record.version,
                    "suspendedQueueRevision": suspended_record.queue_revision,
                    "suspendedControlVersion": suspended_record.control_version,
                    "suspendedAppliedControlVersion": (
                        suspended_device.applied_control_version
                    ),
                }
                lease_material = {
                    "userName": user_name,
                    "followerClientId": follower_client_id,
                    "followerDeviceSessionId": follower_device_session_id,
                    "followerConnectionNonce": follower_connection_nonce,
                    "followerConnectionEpoch": follower_connection_epoch,
                    "sourcePlaybackContextId": source_playback_context_id,
                    "sourceAuthorityClientId": source_authority_client_id,
                    "sourceAuthorityDeviceSessionId": (
                        source_authority_device_session_id
                    ),
                    "sourceConnectionNonce": source_connection_nonce,
                    "sourceConnectionEpoch": source_connection_epoch,
                    "suspendedPlaybackContextId": (
                        suspended_playback_context_id
                    ),
                    "suspendedAuthorityClientId": (
                        suspended_authority_client_id
                    ),
                    "suspendedAuthorityDeviceSessionId": (
                        suspended_authority_device_session_id
                    ),
                    "suspendedConnectionNonce": suspended_connection_nonce,
                    "suspendedConnectionEpoch": suspended_connection_epoch,
                    "startAck": start_ack,
                }
                lease_fingerprint = _fingerprint(lease_material)

                occupied = EmoFollowSafetyLease.get_or_none(
                    (
                        EmoFollowSafetyLease.user_name == user_name
                    )
                    & (
                        EmoFollowSafetyLease.suspended_playback_context_id
                        == suspended_playback_context_id
                    )
                    & (
                        EmoFollowSafetyLease.phase.in_(
                            FOLLOW_NONTERMINAL_PHASES
                        )
                    )
                )
                if occupied is not None:
                    raise FollowSafetyLeaseConflictError(
                        "Suspended Context already has a Follow safety lease",
                        suspended_playback_context_id,
                    )
                lease_count = (
                    EmoFollowSafetyLease.select()
                    .where(
                        (EmoFollowSafetyLease.user_name == user_name)
                        & (
                            EmoFollowSafetyLease.phase.in_(
                                FOLLOW_NONTERMINAL_PHASES
                            )
                        )
                    )
                    .count()
                )
                if lease_count >= MAX_USER_FOLLOW_SAFETY_LEASES:
                    raise FollowSafetyLeaseLimitError()

                source_context = _context_payload(source_record)
                suspended_context = _context_payload(suspended_record)
                source_fact = _device_payload(source_device)
                suspended_fact = _device_payload(suspended_device)
                if pre_mutation_validator is not None:
                    pre_mutation_validator(
                        dict(source_context),
                        dict(source_fact),
                        dict(suspended_context),
                        dict(suspended_fact),
                    )
                try:
                    created = EmoFollowSafetyLease.create(
                        user_name=user_name,
                        follower_client_id=follower_client_id,
                        follower_device_session_id=(
                            follower_device_session_id
                        ),
                        follower_connection_nonce=follower_connection_nonce,
                        follower_connection_epoch=follower_connection_epoch,
                        source_playback_context_id=source_playback_context_id,
                        source_authority_client_id=source_authority_client_id,
                        source_authority_device_session_id=(
                            source_authority_device_session_id
                        ),
                        source_connection_nonce=source_connection_nonce,
                        source_connection_epoch=source_connection_epoch,
                        suspended_playback_context_id=(
                            suspended_playback_context_id
                        ),
                        suspended_authority_client_id=(
                            suspended_authority_client_id
                        ),
                        suspended_authority_device_session_id=(
                            suspended_authority_device_session_id
                        ),
                        suspended_connection_nonce=suspended_connection_nonce,
                        suspended_connection_epoch=suspended_connection_epoch,
                        suspended_epoch=suspended_record.epoch,
                        suspended_version=suspended_record.version,
                        suspended_queue_revision=(
                            suspended_record.queue_revision
                        ),
                        suspended_control_version=(
                            suspended_record.control_version
                        ),
                        suspended_applied_control_version=(
                            suspended_device.applied_control_version
                        ),
                        phase="active",
                        lease_fingerprint=lease_fingerprint,
                        start_request_fingerprint=start_request_fingerprint,
                        start_ack_json=_canonical_json(start_ack),
                        created_at_ms=server_time_ms,
                        updated_at_ms=server_time_ms,
                    )
                except IntegrityError as exc:
                    existing = _lease_for_follower_query(
                        user_name,
                        follower_client_id,
                        follower_device_session_id,
                    )
                    if existing is not None and _is_durable_start_replay(
                        existing,
                        user_name,
                        follower_client_id,
                        follower_device_session_id,
                        source_playback_context_id,
                        suspended_playback_context_id,
                        start_request_fingerprint,
                    ):
                        return serializeFollowSafetyLease(existing), False
                    raise FollowSafetyLeaseConflictError(
                        "Follow safety lease conflicts with current occupancy",
                        suspended_playback_context_id,
                    ) from exc
                return serializeFollowSafetyLease(created), True
        finally:
            close_connection()


def _source_fact_requires_recovery(
    record: EmoFollowSafetyLease,
    server_time_ms: int,
    freshness_ms: int,
) -> Tuple[bool, bool]:
    context = EmoPlaybackContext.get_or_none(
        EmoPlaybackContext.playback_context_id
        == record.source_playback_context_id
    )
    if (
        context is None
        or context.user_name != record.user_name
        or context.lifecycle != "active"
    ):
        return True, True
    if (
        context.authority_client_id != record.source_authority_client_id
        or context.authority_device_session_id
        != record.source_authority_device_session_id
    ):
        return True, False
    device = EmoDevicePlaybackState.get_or_none(
        (
            EmoDevicePlaybackState.playback_context_id
            == record.source_playback_context_id
        )
        & (
            EmoDevicePlaybackState.owner_client_id
            == record.source_authority_client_id
        )
    )
    if (
        device is None
        or device.device_session_id
        != record.source_authority_device_session_id
        or device.context_epoch != context.epoch
        or device.applied_control_version != context.control_version
    ):
        return True, False
    try:
        queue_song_ids = json.loads(context.queue_json)
        context_playback = (
            json.loads(context.playback_json) if context.playback_json else {}
        )
        playback = (
            json.loads(device.playback_json) if device.playback_json else {}
        )
    except (TypeError, ValueError):
        return True, False
    if playback.get("_connectionNonce") != record.source_connection_nonce:
        return True, False
    if context.state == "idle":
        return False, False
    if (
        context.state not in {"playing", "paused", "stopped"}
        or not isinstance(queue_song_ids, list)
        or not queue_song_ids
        or type(context.current_index) is not int
        or context.current_index < 0
        or context.current_index >= len(queue_song_ids)
        or queue_song_ids[context.current_index] != context.track_id
        or not bool(device.is_authority)
        or device.state != context.state
        or device.track_id != context.track_id
        or type(device.position_ms) is not int
        or device.position_ms < 0
    ):
        return True, False
    context_rate = context_playback.get("playbackRate", 1.0)
    device_rate = playback.get("playbackRate", 1.0)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0.5
        or value > 2.0
        for value in (context_rate, device_rate)
    ) or float(context_rate) != float(device_rate):
        return True, False
    for field_name in ("serverUpdatedAtMs", "positionSampledAtServerMs"):
        value = playback.get(field_name)
        if type(value) is not int or value > server_time_ms:
            return True, False
        if context.state == "playing" and server_time_ms - value > freshness_ms:
            return True, False
    return False, False


def sweepFollowSafetyLeaseDeadlines(
    server_time_ms: int,
    source_recovery_ms: int = FOLLOW_SOURCE_RECOVERY_MS,
    source_freshness_ms: int = FOLLOW_SOURCE_PLAYING_FRESHNESS_MS,
) -> List[Dict[str, object]]:
    _require_integer(server_time_ms, "serverTimeMs")
    _require_integer(source_recovery_ms, "sourceRecoveryMs", 1)
    _require_integer(source_freshness_ms, "sourceFreshnessMs", 1)
    leases = listFollowSafetyLeases()
    transitions = []
    for lease in leases:
        with _follow_lease_lock_set((lease,)):
            open_connection(reuse=True)
            try:
                with _follow_transaction():
                    record = EmoFollowSafetyLease.get_or_none(
                        (EmoFollowSafetyLease.id == lease["leaseId"])
                        & (
                            EmoFollowSafetyLease.phase.in_(
                                FOLLOW_NONTERMINAL_PHASES
                            )
                        )
                    )
                    if record is None:
                        continue
                    reason = None
                    if (
                        record.phase == "reconnectGrace"
                        and (
                            record.follow_reconnect_grace_expires_at_ms is None
                            or record.follow_reconnect_grace_expires_at_ms
                            <= server_time_ms
                        )
                    ):
                        record.phase = "cleanupRequired"
                        record.follow_reconnect_grace_expires_at_ms = None
                        record.source_recovery_deadline_at_ms = None
                        reason = "followerReconnectExpired"
                    elif record.phase != "cleanupRequired":
                        requires_recovery, source_closed = (
                            _source_fact_requires_recovery(
                                record,
                                server_time_ms,
                                source_freshness_ms,
                            )
                        )
                        if source_closed:
                            record.phase = "cleanupRequired"
                            record.source_recovery_deadline_at_ms = None
                            reason = "sourceClosed"
                        elif record.source_recovery_deadline_at_ms is not None:
                            if (
                                record.source_recovery_deadline_at_ms
                                <= server_time_ms
                            ):
                                record.phase = "cleanupRequired"
                                record.source_recovery_deadline_at_ms = None
                                reason = "sourceRecoveryExpired"
                        elif requires_recovery:
                            record.source_recovery_deadline_at_ms = (
                                server_time_ms + source_recovery_ms
                            )
                            reason = "sourceRecoveryStarted"
                    if reason is None:
                        continue
                    _save_lease(record, server_time_ms)
                    transitions.append(
                        {
                            "reason": reason,
                            "lease": serializeFollowSafetyLease(record),
                        }
                    )
            finally:
                close_connection()
    return transitions


def recoverFollowSafetyLeasesForStartup(
    server_time_ms: int,
    reconnect_grace_ms: int = FOLLOW_RECONNECT_GRACE_MS,
) -> List[Dict[str, object]]:
    _require_integer(server_time_ms, "serverTimeMs")
    _require_integer(reconnect_grace_ms, "reconnectGraceMs", 1)
    open_connection(reuse=True)
    try:
        with _follow_transaction():
            records = list(
                EmoFollowSafetyLease.select()
                .where(
                    EmoFollowSafetyLease.phase.in_(FOLLOW_NONTERMINAL_PHASES)
                )
                .order_by(
                    EmoFollowSafetyLease.user_name,
                    EmoFollowSafetyLease.follower_client_id,
                    EmoFollowSafetyLease.follower_device_session_id,
                )
            )
            for record in records:
                changed = False
                if record.stop_request_fingerprint is not None:
                    if (
                        record.phase != "cleanupRequired"
                        or record.follow_reconnect_grace_expires_at_ms
                        is not None
                        or record.source_recovery_deadline_at_ms is not None
                    ):
                        record.phase = "cleanupRequired"
                        record.follow_reconnect_grace_expires_at_ms = None
                        record.source_recovery_deadline_at_ms = None
                        changed = True
                elif record.phase == "active":
                    if (
                        record.source_recovery_deadline_at_ms is not None
                        and record.source_recovery_deadline_at_ms
                        <= server_time_ms
                    ):
                        record.phase = "cleanupRequired"
                        record.source_recovery_deadline_at_ms = None
                    else:
                        record.phase = "reconnectGrace"
                        record.follow_reconnect_grace_expires_at_ms = (
                            server_time_ms + reconnect_grace_ms
                        )
                    changed = True
                elif record.phase == "reconnectGrace" and (
                    record.follow_reconnect_grace_expires_at_ms is None
                    or record.follow_reconnect_grace_expires_at_ms
                    <= server_time_ms
                ):
                    record.phase = "cleanupRequired"
                    record.follow_reconnect_grace_expires_at_ms = None
                    changed = True
                elif record.phase == "cleanupRequired" and (
                    record.follow_reconnect_grace_expires_at_ms is not None
                    or record.source_recovery_deadline_at_ms is not None
                ):
                    record.follow_reconnect_grace_expires_at_ms = None
                    record.source_recovery_deadline_at_ms = None
                    changed = True
                if changed:
                    _save_lease(record, server_time_ms)
            return [serializeFollowSafetyLease(record) for record in records]
    finally:
        close_connection()
