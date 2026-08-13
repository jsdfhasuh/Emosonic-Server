import hashlib
import heapq
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from typing import Dict, Iterable, Iterator, List, Optional, Tuple
from uuid import uuid4

from peewee import IntegrityError, SqliteDatabase, fn

from ..db import (
    EmoBroadcastFence,
    EmoCoreStartupRecovery,
    EmoDevicePlaybackState,
    EmoFollowSafetyLease,
    EmoLocalQueue,
    EmoPlaybackControlTransaction,
    EmoPlaybackControlReconciliation,
    EmoPlaybackContext,
    EmoPlaybackHandoff,
    EmoPlaybackLocalIntent,
    EmoPlaybackPrepareTransaction,
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


class PlaybackContextAuthorityAmbiguousError(Exception):
    def __init__(self, playback_context):
        super().__init__(
            "Playback context authority/device pair is ambiguous"
        )
        self.playback_context = playback_context


class PlaybackContextEnsureConflictError(Exception):
    def __init__(self, playback_context=None):
        super().__init__("Stable client has a conflicting active playback context")
        self.playback_context = playback_context


class PlaybackContextBroadcastBarrierError(PlaybackContextEnsureConflictError):
    def __init__(self, playback_context=None):
        Exception.__init__(
            self,
            "Playback context is occupied by an active Broadcast",
        )
        self.playback_context = playback_context


class PlaybackContextFollowBarrierError(PlaybackContextEnsureConflictError):
    def __init__(self, playback_context=None):
        Exception.__init__(
            self,
            "Playback context is occupied by an active Follow safety lease",
        )
        self.playback_context = playback_context


class PlaybackContextHandoffBarrierError(PlaybackContextEnsureConflictError):
    def __init__(self, playback_context=None):
        Exception.__init__(
            self,
            "Playback context is occupied by an active Handoff",
        )
        self.playback_context = playback_context


class PlaybackContextRestoreInProgressError(Exception):
    def __init__(self, playback_context):
        super().__init__("Original playback context restore is in progress")
        self.playback_context = playback_context


class PlaybackContextQueueRequiredError(Exception):
    def __init__(self, playback_context):
        super().__init__("Playback context requires a non-empty queue")
        self.playback_context = playback_context


class PlaybackContextCloseConflictError(Exception):
    def __init__(self, playback_context, message):
        super().__init__(message)
        self.playback_context = playback_context


class PlaybackContextCloseInvariantError(RuntimeError):
    pass


class PlaybackControlTransactionConflictError(Exception):
    pass


class PlaybackControlReconciliationConflictError(Exception):
    pass


class PlaybackPrepareTransactionConflictError(Exception):
    pass


class PlaybackPrepareAlreadyActiveError(Exception):
    pass


class PlaybackHandoffTargetConflictError(Exception):
    pass


class PlaybackLocalIntentConflictError(Exception):
    pass


class PlaybackClientSequenceConflictError(Exception):
    def __init__(self, current_client_seq):
        super().__init__("Playback clientSeq is stale or conflicts")
        self.current_client_seq = current_client_seq


AuthorityPair = Tuple[str, str, str]

_ORDINARY_CONTROL_ACTIONS = frozenset(
    {
        "queue.playItem",
        "player.play",
        "player.pause",
        "player.seek",
        "player.next",
        "player.prev",
    }
)
_TRACK_CHANGING_CONTROL_ACTIONS = frozenset(
    {
        "queue.playItem",
        "player.next",
        "player.prev",
    }
)
_TERMINAL_CONTROL_STATUSES = ("committed", "failed", "superseded")
HANDOFF_NONTERMINAL_STATUSES = (
    "preparing",
    "ready",
    "committed",
    "committing",
)
HANDOFF_TERMINAL_STATUSES = (
    "completed",
    "cancelled",
    "failed",
    "timed_out",
    # Historical rows used transport- and runtime-facing spellings.
    "canceled",
    "timedOut",
    "aborted",
    "superseded",
)

STRICT_CORE_RETENTION_LIMIT = 512
STRICT_CORE_RETRY_WINDOW_MS = 10 * 60 * 1000
STRICT_CORE_CLEANUP_BATCH_SIZE = 128


class PlaybackControlTransactionSettlementResult(tuple):
    def __new__(
        cls,
        transaction: Optional[Dict[str, object]],
        mutated: bool,
        execution_eligible_transactions: Iterable[Dict[str, object]] = (),
        dependency_settlements: Iterable[Dict[str, object]] = (),
    ):
        result = super().__new__(cls, (transaction, bool(mutated)))
        result.transaction = transaction
        result.mutated = bool(mutated)
        result.execution_eligible_transactions = tuple(
            execution_eligible_transactions
        )
        result.dependency_settlements = tuple(dependency_settlements)
        return result


def _distinct_authority_pairs(
    authority_pairs: Iterable[AuthorityPair],
) -> Tuple[AuthorityPair, ...]:
    return tuple(sorted(set(authority_pairs)))


class PlaybackContextCreateResult(tuple):
    def __new__(
        cls,
        playback_context: Dict[str, object],
        mutated: bool,
        affected_authority_pairs: Iterable[AuthorityPair],
    ):
        result = super().__new__(
            cls,
            (playback_context, bool(mutated)),
        )
        result.mutated = bool(mutated)
        result.affected_authority_pairs = _distinct_authority_pairs(
            affected_authority_pairs
        )
        result.canonical_context = dict(playback_context)
        return result


class PlaybackContextEnsureResult(tuple):
    def __new__(
        cls,
        playback_context: Dict[str, object],
        mutated: bool,
        affected_authority_pairs: Iterable[AuthorityPair],
    ):
        result = super().__new__(cls, (playback_context, bool(mutated)))
        result.mutated = bool(mutated)
        result.affected_authority_pairs = _distinct_authority_pairs(
            affected_authority_pairs
        )
        result.binding_mutated = bool(result.affected_authority_pairs)
        result.canonical_context = dict(playback_context)
        return result


class PlaybackContextCloseResult(dict):
    def __init__(
        self,
        playback_context: Dict[str, object],
        mutated: bool,
        affected_authority_pairs: Iterable[AuthorityPair],
        close_outcome: Optional[Dict[str, object]] = None,
        tombstone: Optional[Dict[str, object]] = None,
    ) -> None:
        super().__init__(playback_context)
        self.mutated = bool(mutated)
        self.affected_authority_pairs = _distinct_authority_pairs(
            affected_authority_pairs
        )
        self.canonical_context = dict(playback_context)
        self.close_outcome = (
            None if close_outcome is None else dict(close_outcome)
        )
        self.tombstone = None if tombstone is None else dict(tombstone)


class PlaybackHandoffCompleteResult(tuple):
    def __new__(
        cls,
        playback_context: Dict[str, object],
        handoff: Dict[str, object],
        device_state: Optional[Dict[str, object]],
        mutated: bool,
        affected_authority_pairs: Iterable[AuthorityPair],
        retired_context: Optional[Dict[str, object]] = None,
    ):
        result = super().__new__(
            cls,
            (
                playback_context,
                handoff,
                device_state,
                bool(mutated),
            ),
        )
        result.mutated = bool(mutated)
        result.affected_authority_pairs = _distinct_authority_pairs(
            affected_authority_pairs
        )
        result.canonical_context = dict(playback_context)
        result.retired_context = (
            None if retired_context is None else dict(retired_context)
        )
        return result


_strict_playback_context_locks = {}
_strict_playback_context_locks_guard = threading.Lock()
_strict_authority_pair_locks = {}
_strict_authority_pair_locks_guard = threading.Lock()
_strict_stable_client_locks = {}
_strict_stable_client_locks_guard = threading.Lock()
_core_startup_recovery_lock = threading.RLock()


@contextmanager
def _strict_playback_context_lock(
    playback_context_id: str,
) -> Iterator[None]:
    with _strict_playback_context_lock_set((playback_context_id,)):
        yield


@contextmanager
def _strict_playback_context_lock_set(
    playback_context_ids: Iterable[str],
) -> Iterator[None]:
    normalized_ids = sorted(set(playback_context_ids))
    locks = []
    with _strict_playback_context_locks_guard:
        for playback_context_id in normalized_ids:
            locks.append(
                _strict_playback_context_locks.setdefault(
                    playback_context_id,
                    threading.RLock(),
                )
            )
    for context_lock in locks:
        context_lock.acquire()
    try:
        yield
    finally:
        for context_lock in reversed(locks):
            context_lock.release()


@contextmanager
def strictPlaybackContextLockSet(
    playback_context_ids: Iterable[str],
) -> Iterator[None]:
    """Serialize a cross-store mutation over a deterministic Context set."""
    with _strict_playback_context_lock_set(playback_context_ids):
        yield


@contextmanager
def strictAuthorityPairLockSet(
    authority_pairs: Iterable[AuthorityPair],
) -> Iterator[None]:
    with _strict_authority_pair_lock(authority_pairs):
        yield


_SOURCE_BROADCAST_ALLOWED_MUTATIONS = {
    "applyStrictPlaybackUpdate",
    "createPlaybackControlTransaction",
    "mutateStrictPlaybackContextControl",
    "mutateStrictPlaybackContextQueue",
    "markPlaybackControlTransactionExecutionEligible",
    "savePlaybackLocalIntent",
    "settlePlaybackControlTransaction",
}

_HANDOFF_ALLOWED_CONTEXT_MUTATIONS = {
    "createStrictPlaybackHandoff",
    "commitStrictPlaybackHandoff",
    "completeStrictPlaybackHandoff",
    "markStrictPlaybackHandoffCommitEnqueued",
    "terminateStrictPlaybackHandoff",
}


def _broadcast_fences_for_context(playback_context_id):
    return list(
        EmoBroadcastFence.select().where(
            EmoBroadcastFence.playback_context_id == playback_context_id
        )
    )


def _broadcast_fence_for_pair(
    user_name,
    client_id,
    device_session_id,
):
    return EmoBroadcastFence.get_or_none(
        (EmoBroadcastFence.user_name == user_name)
        & (EmoBroadcastFence.client_id == client_id)
        & (EmoBroadcastFence.device_session_id == device_session_id)
    )


def _follow_safety_lease_for_context(playback_context_id):
    return EmoFollowSafetyLease.get_or_none(
        (
            EmoFollowSafetyLease.suspended_playback_context_id
            == playback_context_id
        )
        & (
            EmoFollowSafetyLease.phase.in_(
                ("active", "reconnectGrace", "cleanupRequired")
            )
        )
    )


def _follow_safety_lease_for_pair(
    user_name,
    client_id,
    device_session_id,
):
    return EmoFollowSafetyLease.get_or_none(
        (EmoFollowSafetyLease.user_name == user_name)
        & (EmoFollowSafetyLease.follower_client_id == client_id)
        & (
            EmoFollowSafetyLease.follower_device_session_id
            == device_session_id
        )
        & (
            EmoFollowSafetyLease.phase.in_(
                ("active", "reconnectGrace", "cleanupRequired")
            )
        )
    )


def _canonical_context_for_barrier(playback_context_id):
    record = EmoPlaybackContext.get_or_none(
        EmoPlaybackContext.playback_context_id == playback_context_id
    )
    return None if record is None else _playback_context_payload(record)


def _raise_broadcast_fence(
    fence,
    playback_context_id,
    restore_in_progress=False,
):
    playback_context = _canonical_context_for_barrier(playback_context_id)
    if restore_in_progress and fence.phase == "restorePending":
        raise PlaybackContextRestoreInProgressError(playback_context or {})
    raise PlaybackContextBroadcastBarrierError(playback_context)


def _raise_follow_fence(lease, playback_context_id=None):
    context_id = playback_context_id or lease.suspended_playback_context_id
    playback_context = _canonical_context_for_barrier(context_id)
    raise PlaybackContextFollowBarrierError(playback_context)


def _handoff_snapshot(record):
    snapshot = _load_json_object(record.snapshot_json) or {}
    return _sanitize_handoff_snapshot(snapshot)


def _sanitize_handoff_snapshot(value):
    if isinstance(value, dict):
        return {
            key: _sanitize_handoff_snapshot(item)
            for key, item in value.items()
            if key not in {"sid", "sourceSid", "targetSid", "requestSid"}
        }
    if isinstance(value, list):
        return [_sanitize_handoff_snapshot(item) for item in value]
    return value


def serializePlaybackHandoff(record):
    if record is None:
        return None
    snapshot = _handoff_snapshot(record)
    provisional_control_version = record.provisional_control_version
    if provisional_control_version is None:
        provisional_control_version = snapshot.get("handoffControlVersion")
    payload = {
        "handoffId": record.handoff_id,
        "requestId": record.request_id,
        "playbackContextId": record.playback_context_id,
        "userName": record.user_name,
        "sourceClientId": record.source_client_id,
        "targetClientId": record.target_client_id,
        "originClientId": record.origin_client_id,
        "status": record.status,
        "baseControlVersion": record.base_control_version,
        "contextEpoch": record.context_epoch,
        "controlVersion": provisional_control_version,
        "prepareId": snapshot.get("prepareId"),
        "prepareExpiresAtMs": snapshot.get("prepareExpiresAtMs"),
        "completeExpiresAtMs": snapshot.get("completeExpiresAtMs"),
        "snapshot": snapshot,
        "errorCode": record.error_code,
        "errorMessage": record.error_message,
        "createdAt": record.created_at.timestamp(),
        "updatedAt": record.updated_at.timestamp(),
    }
    generation = {
        "sourceDeviceSessionId": record.source_device_session_id,
        "sourceConnectionNonce": record.source_connection_nonce,
        "sourceConnectionEpoch": record.source_connection_epoch,
        "targetDeviceSessionId": record.target_device_session_id,
        "targetConnectionNonce": record.target_connection_nonce,
        "targetConnectionEpoch": record.target_connection_epoch,
    }
    payload.update(
        {field_name: value for field_name, value in generation.items() if value is not None}
    )
    return payload


def _require_handoff_provisional_lane(record):
    if type(record.context_epoch) is not int or record.context_epoch < 1:
        raise PlaybackHandoffTargetConflictError(
            "Playback handoff context epoch is incomplete"
        )
    if (
        type(record.provisional_control_version) is not int
        or record.provisional_control_version != record.base_control_version + 1
    ):
        raise PlaybackHandoffTargetConflictError(
            "Playback handoff provisional control version is invalid"
        )
    snapshot = _handoff_snapshot(record)
    for field_name, expected in (
        ("sourceEpoch", record.context_epoch),
        ("sourceControlVersion", record.base_control_version),
        ("handoffControlVersion", record.provisional_control_version),
    ):
        if snapshot.get(field_name) != expected:
            raise PlaybackHandoffTargetConflictError(
                "Playback handoff provisional lane snapshot conflicts"
            )


_HANDOFF_PROVISIONAL_SNAPSHOT_FIELDS = (
    "sourceEpoch",
    "sourceVersion",
    "sourceQueueRevision",
    "sourceControlVersion",
    "handoffControlVersion",
    "sourceContextTrackId",
    "sourceContextState",
    "trackId",
    "state",
    "playbackRate",
    "targetStandbyPlaybackContextId",
    "targetStandbyEpoch",
    "targetStandbyAuthorityClientId",
    "targetStandbyAuthorityDeviceSessionId",
)


def _handoff_source_changed_before_commit(
    handoff_record,
    context_record,
    source_device_record,
):
    snapshot = _handoff_snapshot(handoff_record)
    if (
        context_record.lifecycle != "active"
        or context_record.authority_client_id
        != handoff_record.source_client_id
        or context_record.authority_device_session_id
        != handoff_record.source_device_session_id
        or context_record.epoch != handoff_record.context_epoch
        or context_record.version != snapshot.get("sourceVersion")
        or context_record.queue_revision
        != snapshot.get("sourceQueueRevision")
        or context_record.control_version
        != handoff_record.base_control_version
        or context_record.control_version
        != snapshot.get("sourceControlVersion")
        or json.loads(context_record.queue_json)
        != snapshot.get("queueSongIds")
        or context_record.current_index != snapshot.get("currentIndex")
        or context_record.track_id != snapshot.get("sourceContextTrackId")
        or context_record.state != snapshot.get("sourceContextState")
    ):
        return True
    if (
        source_device_record is None
        or source_device_record.owner_client_id
        != handoff_record.source_client_id
        or source_device_record.device_session_id
        != handoff_record.source_device_session_id
        or source_device_record.context_epoch != context_record.epoch
        or source_device_record.applied_control_version
        != context_record.control_version
    ):
        return True
    source_device = _device_playback_state_payload(source_device_record)
    if (
        source_device.get("trackId") != snapshot.get("trackId")
        or source_device.get("state") != snapshot.get("state")
        or source_device.get("playbackRate", 1.0)
        != snapshot.get("playbackRate", 1.0)
        or source_device.get("positionMs", 0) < snapshot.get("positionMs", 0)
        or source_device.get("positionSampledAtServerMs", 0)
        < snapshot.get("positionSampledAtServerMs", 0)
    ):
        return True
    return EmoPlaybackControlTransaction.select().where(
        (
            EmoPlaybackControlTransaction.playback_context_id
            == context_record.playback_context_id
        )
        & (EmoPlaybackControlTransaction.epoch == context_record.epoch)
        & (EmoPlaybackControlTransaction.status == "pending")
    ).exists()


def _fail_handoff_source_changed(record):
    record.status = "failed"
    record.error_code = "source_changed"
    record.error_message = "Handoff source changed before commit"
    record.updated_at = now()
    record.save(
        only=(
            EmoPlaybackHandoff.status,
            EmoPlaybackHandoff.error_code,
            EmoPlaybackHandoff.error_message,
            EmoPlaybackHandoff.updated_at,
        )
    )


def _handoff_has_complete_generation(record):
    return all(
        isinstance(value, str) and value.strip()
        for value in (
            record.source_device_session_id,
            record.source_connection_nonce,
            record.target_device_session_id,
            record.target_connection_nonce,
        )
    ) and all(
        type(value) is int and value == 1
        for value in (
            record.source_connection_epoch,
            record.target_connection_epoch,
        )
    )


def _require_handoff_generation(record):
    if not _handoff_has_complete_generation(record):
        raise PlaybackHandoffTargetConflictError(
            "Playback handoff physical generation is incomplete"
        )


def _active_handoff_records(user_name=None):
    expression = EmoPlaybackHandoff.status.in_(HANDOFF_NONTERMINAL_STATUSES)
    if user_name is not None:
        expression &= EmoPlaybackHandoff.user_name == user_name
    return list(
        EmoPlaybackHandoff.select()
        .where(expression)
        .order_by(EmoPlaybackHandoff.created_at, EmoPlaybackHandoff.handoff_id)
    )


def _active_handoff_for_context(playback_context_id, user_name=None):
    for record in _active_handoff_records(user_name=user_name):
        if record.playback_context_id == playback_context_id:
            return record, "source"
        if _handoff_snapshot(record).get(
            "targetStandbyPlaybackContextId"
        ) == playback_context_id:
            return record, "standby"
    return None, None


def _handoff_occupies_context(record, playback_context_id):
    if record.playback_context_id == playback_context_id:
        return True
    return _handoff_snapshot(record).get(
        "targetStandbyPlaybackContextId"
    ) == playback_context_id


def _handoff_occupies_pair(record, user_name, client_id, device_session_id):
    if record.user_name != user_name:
        return False
    return (
        record.target_client_id == client_id
        and record.target_device_session_id == device_session_id
    ) or (
        record.source_client_id == client_id
        and record.source_device_session_id == device_session_id
    )


def _handoff_source_update_changes_actual(record, payload, existing=None):
    baseline = (
        _device_playback_state_payload(existing)
        if existing is not None
        else _handoff_snapshot(record)
    )
    expected_track_id = baseline.get("trackId")
    expected_state = baseline.get("state")
    expected_rate = baseline.get("playbackRate", 1.0)
    expected_position_ms = baseline.get("positionMs", 0)
    expected_sample_ms = baseline.get("positionSampledAtServerMs", 0)
    return (
        payload.get("trackId") != expected_track_id
        or payload.get("state") != expected_state
        or payload.get("playbackRate", 1.0) != expected_rate
        or payload.get("positionMs", 0) < expected_position_ms
        or payload.get("positionSampledAtServerMs", 0) < expected_sample_ms
    )


def _settle_handoff_source_changed(record):
    record.status = "failed"
    record.error_code = "source_changed"
    record.error_message = "Handoff source playback changed"
    record.updated_at = now()
    record.save(
        only=(
            EmoPlaybackHandoff.status,
            EmoPlaybackHandoff.error_code,
            EmoPlaybackHandoff.error_message,
            EmoPlaybackHandoff.updated_at,
        )
    )
    return serializePlaybackHandoff(record)


def _handoff_generation_matches(
    record,
    role,
    user_name,
    client_id,
    device_session_id,
    connection_nonce,
    connection_epoch,
):
    return (
        record.user_name == user_name
        and getattr(record, "%s_client_id" % role) == client_id
        and getattr(record, "%s_device_session_id" % role)
        == device_session_id
        and getattr(record, "%s_connection_nonce" % role)
        == connection_nonce
        and getattr(record, "%s_connection_epoch" % role)
        == connection_epoch
    )


def _validate_handoff_generation_arguments(
    user_name,
    client_id,
    device_session_id,
    connection_nonce,
    connection_epoch,
):
    for field_name, value in (
        ("userName", user_name),
        ("clientId", client_id),
        ("deviceSessionId", device_session_id),
        ("connectionNonce", connection_nonce),
    ):
        _require_non_empty_string(value, field_name, 128)
    _require_integer(connection_epoch, "connectionEpoch", 1)
    if connection_epoch != 1:
        raise ValueError("connectionEpoch must be exactly 1")


def _raise_handoff_fence(record, playback_context_id=None):
    context_id = playback_context_id or record.playback_context_id
    playback_context = _canonical_context_for_barrier(context_id)
    raise PlaybackContextHandoffBarrierError(playback_context)


def requirePlaybackHandoffResourceAvailable(
    playback_context_id=None,
    user_name=None,
    client_id=None,
    device_session_id=None,
    mutation_name=None,
    allowed_handoff_id=None,
):
    records = _active_handoff_records(user_name=user_name)
    for record in records:
        if (
            allowed_handoff_id is not None
            and record.handoff_id == allowed_handoff_id
        ):
            continue
        if playback_context_id is not None and _handoff_occupies_context(
            record,
            playback_context_id,
        ):
            _raise_handoff_fence(record, playback_context_id)
        if user_name is not None and _handoff_occupies_pair(
            record,
            user_name,
            client_id,
            device_session_id,
        ):
            _raise_handoff_fence(record)


def requireFollowSafetyLeaseResourceAvailable(
    playback_context_id=None,
    user_name=None,
    client_id=None,
    device_session_id=None,
):
    if playback_context_id is not None:
        lease = _follow_safety_lease_for_context(playback_context_id)
        if lease is not None:
            _raise_follow_fence(lease, playback_context_id)
    if user_name is not None:
        lease = _follow_safety_lease_for_pair(
            user_name,
            client_id,
            device_session_id,
        )
        if lease is not None:
            _raise_follow_fence(lease)


def _require_broadcast_context_mutation_allowed(
    playback_context_id,
    mutation_name,
    restore_in_progress=False,
):
    fences = _broadcast_fences_for_context(playback_context_id)
    ordinary = next(
        (fence for fence in fences if fence.role == "ordinary"),
        None,
    )
    if ordinary is not None:
        _raise_broadcast_fence(
            ordinary,
            playback_context_id,
            restore_in_progress=restore_in_progress,
        )
    source = next(
        (fence for fence in fences if fence.role == "source"),
        None,
    )
    if (
        source is not None
        and mutation_name not in _SOURCE_BROADCAST_ALLOWED_MUTATIONS
    ):
        _raise_broadcast_fence(
            source,
            playback_context_id,
            restore_in_progress=restore_in_progress,
        )


@contextmanager
def _strict_stable_client_lock(user_name, client_id):
    key = (user_name, client_id)
    with _strict_stable_client_locks_guard:
        stable_lock = _strict_stable_client_locks.setdefault(
            key,
            threading.RLock(),
        )
    with stable_lock:
        yield


def _serialize_strict_playback_context_mutation(function):
    @wraps(function)
    def serialized(playback_context_id, *args, **kwargs):
        with _strict_playback_context_lock(playback_context_id):
            open_connection(reuse=True)
            try:
                requireFollowSafetyLeaseResourceAvailable(
                    playback_context_id=playback_context_id,
                )
                if function.__name__ != "applyStrictPlaybackUpdate":
                    allowed_handoff_id = None
                    if function.__name__ == "createStrictPlaybackHandoff" and args:
                        handoff = args[0]
                        if isinstance(handoff, dict):
                            allowed_handoff_id = handoff.get("handoffId")
                    elif (
                        function.__name__
                        in {
                            "commitStrictPlaybackHandoff",
                            "markStrictPlaybackHandoffCommitEnqueued",
                            "terminateStrictPlaybackHandoff",
                        }
                        and args
                    ):
                        allowed_handoff_id = args[0]
                    requirePlaybackHandoffResourceAvailable(
                        playback_context_id=playback_context_id,
                        mutation_name=function.__name__,
                        allowed_handoff_id=allowed_handoff_id,
                    )
                _require_broadcast_context_mutation_allowed(
                    playback_context_id,
                    function.__name__,
                )
            finally:
                close_connection()
            return function(playback_context_id, *args, **kwargs)

    return serialized


def _serialize_strict_playback_context_close(function):
    @wraps(function)
    def serialized(playback_context_id, user_name=None, *args, **kwargs):
        with _strict_playback_context_lock(playback_context_id):
            open_connection(reuse=True)
            try:
                if user_name is None:
                    requireFollowSafetyLeaseResourceAvailable(
                        playback_context_id=playback_context_id,
                    )
                    _require_broadcast_context_mutation_allowed(
                        playback_context_id,
                        function.__name__,
                        restore_in_progress=True,
                    )
                else:
                    visible_record = EmoPlaybackContext.get_or_none(
                        (
                            EmoPlaybackContext.playback_context_id
                            == playback_context_id
                        )
                        & (EmoPlaybackContext.user_name == user_name)
                    )
                    if visible_record is not None:
                        requireFollowSafetyLeaseResourceAvailable(
                            playback_context_id=playback_context_id,
                        )
                        _require_broadcast_context_mutation_allowed(
                            playback_context_id,
                            function.__name__,
                            restore_in_progress=True,
                        )
            finally:
                close_connection()
            return function(playback_context_id, user_name, *args, **kwargs)

    return serialized


def _strict_authority_pair_key(
    user_name,
    authority_client_id,
    authority_device_session_id,
):
    if not isinstance(user_name, str) or not user_name:
        raise ValueError("Authority pair userName must be non-empty")
    if authority_client_id is None:
        authority_client_id = ""
    if authority_device_session_id is None:
        authority_device_session_id = ""
    if not isinstance(authority_client_id, str):
        raise ValueError("Authority pair clientId must be a string")
    if not isinstance(authority_device_session_id, str):
        raise ValueError("Authority pair deviceSessionId must be a string")
    return (
        user_name,
        authority_client_id,
        authority_device_session_id,
    )


def _record_authority_pair(record):
    return _strict_authority_pair_key(
        record.user_name,
        record.authority_client_id,
        record.authority_device_session_id,
    )


@contextmanager
def _strict_authority_pair_lock(authority_pairs):
    normalized_pairs = sorted(set(authority_pairs))
    locks = []
    with _strict_authority_pair_locks_guard:
        for authority_pair in normalized_pairs:
            locks.append(
                _strict_authority_pair_locks.setdefault(
                    authority_pair,
                    threading.RLock(),
                )
            )
    for pair_lock in locks:
        pair_lock.acquire()
    try:
        yield
    finally:
        for pair_lock in reversed(locks):
            pair_lock.release()


@contextmanager
def _strict_playback_context_transaction() -> Iterator[None]:
    if isinstance(db.obj, SqliteDatabase):
        with db.atomic("IMMEDIATE"):
            yield
        return
    with db.atomic():
        yield


@contextmanager
def _strict_authority_pair_transaction(authority_pairs) -> Iterator[None]:
    with _strict_authority_pair_lock(authority_pairs):
        with _strict_playback_context_transaction():
            yield


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


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_fingerprint(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_terminal_time_after_eligibility(record, terminal_at_ms):
    if (
        record.execution_eligible_at_ms is not None
        and terminal_at_ms < record.execution_eligible_at_ms
    ):
        raise PlaybackControlTransactionConflictError(
            "terminalAtMs precedes executionEligibleAtMs"
        )


def _load_json_object(value, required=False):
    if value is None:
        if required:
            raise ValueError("Persisted JSON object is missing")
        return None
    if not value:
        if required:
            raise ValueError("Persisted JSON object is empty")
        return None
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("Persisted transaction JSON must be an object")
    return loaded


def _require_non_empty_string(value, field_name, max_length=None):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty string" % field_name)
    if max_length is not None and len(value.encode("utf-8")) > max_length:
        raise ValueError("%s exceeds maximum length" % field_name)
    return value


def _require_integer(value, field_name, minimum=None):
    if type(value) is not int:
        raise ValueError("%s must be an integer" % field_name)
    if minimum is not None and value < minimum:
        raise ValueError("%s must be >= %d" % (field_name, minimum))
    return value


def _validate_control_transaction_inputs(
    user_name,
    epoch,
    command_control_version,
    requesting_client_id,
    authority_client_id,
    authority_device_session_id,
    routed_connection_nonce,
    routed_connection_epoch,
    accepted_target,
    accepted_at_ms,
    execution_timeout_ms,
    requesting_device_session_id=None,
    requesting_connection_nonce=None,
    requesting_connection_epoch=None,
    effective_at_server_ms=None,
):
    _require_non_empty_string(user_name, "userName", 64)
    _require_integer(epoch, "epoch", 1)
    _require_integer(command_control_version, "commandControlVersion", 1)
    _require_non_empty_string(requesting_client_id, "requestingClientId", 128)
    _require_non_empty_string(authority_client_id, "authorityClientId", 128)
    _require_non_empty_string(
        authority_device_session_id,
        "authorityDeviceSessionId",
        128,
    )
    _require_non_empty_string(
        routed_connection_nonce,
        "routedConnectionNonce",
        128,
    )
    _require_integer(routed_connection_epoch, "routedConnectionEpoch", 1)
    _require_integer(accepted_at_ms, "acceptedAtMs", 0)
    _require_integer(execution_timeout_ms, "executionTimeoutMs", 1)
    if not isinstance(accepted_target, dict):
        raise ValueError("acceptedTarget must be an object")

    generation = (
        requesting_device_session_id,
        requesting_connection_nonce,
        requesting_connection_epoch,
    )
    if any(value is None for value in generation) and not all(
        value is None for value in generation
    ):
        raise ValueError(
            "Requester physical generation must be provided as a complete tuple"
        )
    if all(value is not None for value in generation):
        _require_non_empty_string(
            requesting_device_session_id,
            "requestingDeviceSessionId",
            128,
        )
        _require_non_empty_string(
            requesting_connection_nonce,
            "requestingConnectionNonce",
            128,
        )
        _require_integer(requesting_connection_epoch, "requestingConnectionEpoch", 1)

    if effective_at_server_ms is not None:
        _require_integer(effective_at_server_ms, "effectiveAtServerMs", 0)


def _control_transaction_identity(
    user_name,
    requesting_client_id,
    requesting_device_session_id,
    requesting_connection_nonce,
    requesting_connection_epoch,
    authority_client_id,
    authority_device_session_id,
    routed_connection_nonce,
    routed_connection_epoch,
    action,
    accepted_target_json,
    accepted_at_ms,
    execution_timeout_ms,
    effective_at_server_ms,
):
    return (
        user_name,
        requesting_client_id,
        requesting_device_session_id,
        requesting_connection_nonce,
        requesting_connection_epoch,
        authority_client_id,
        authority_device_session_id,
        routed_connection_nonce,
        routed_connection_epoch,
        action,
        accepted_target_json,
        accepted_at_ms,
        execution_timeout_ms,
        effective_at_server_ms,
    )


def serializePlaybackControlTransaction(record):
    if record is None:
        return None
    payload = {
        "playbackContextId": record.playback_context_id,
        "userName": record.user_name,
        "epoch": record.epoch,
        "commandControlVersion": record.command_control_version,
        "requestingClientId": record.requesting_client_id,
        "authorityClientId": record.authority_client_id,
        "authorityDeviceSessionId": record.authority_device_session_id,
        "routedConnectionNonce": record.routed_connection_nonce,
        "routedConnectionEpoch": record.routed_connection_epoch,
        "action": record.action,
        "acceptedTarget": _load_json_object(
            record.accepted_target_json,
            required=True,
        ),
        "status": record.status,
        "acceptedAtMs": record.accepted_at_ms,
        "executionTimeoutMs": record.execution_timeout_ms,
    }
    optional = {
        "requestingDeviceSessionId": record.requesting_device_session_id,
        "requestingConnectionNonce": record.requesting_connection_nonce,
        "requestingConnectionEpoch": record.requesting_connection_epoch,
        "effectiveAtServerMs": record.effective_at_server_ms,
        "executionEligibleAtMs": record.execution_eligible_at_ms,
        "watchdogDeadlineAtMs": record.watchdog_deadline_at_ms,
        "errorCode": record.error_code,
        "errorMessage": record.error_message,
        "dependsOnControlVersion": record.depends_on_control_version,
        "appliedControlVersion": record.applied_control_version,
        "terminalFingerprint": record.terminal_fingerprint,
        "terminalAtMs": record.terminal_at_ms,
        "reconciledByControlVersion": record.reconciled_by_control_version,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    return payload


def _pending_track_changing_dependency_record(
    playback_context_id,
    epoch,
    command_control_version,
):
    return (
        EmoPlaybackControlTransaction.select()
        .where(
            (
                EmoPlaybackControlTransaction.playback_context_id
                == playback_context_id
            )
            & (EmoPlaybackControlTransaction.epoch == epoch)
            & (EmoPlaybackControlTransaction.status == "pending")
            & (
                EmoPlaybackControlTransaction.command_control_version
                < command_control_version
            )
            & (
                EmoPlaybackControlTransaction.action.in_(
                    tuple(sorted(_TRACK_CHANGING_CONTROL_ACTIONS))
                )
            )
        )
        .order_by(EmoPlaybackControlTransaction.command_control_version.desc())
        .first()
    )


def _create_playback_control_transaction_record(
    playback_context_id,
    user_name,
    epoch,
    command_control_version,
    requesting_client_id,
    authority_client_id,
    authority_device_session_id,
    routed_connection_nonce,
    routed_connection_epoch,
    action,
    accepted_target,
    accepted_at_ms,
    execution_timeout_ms,
    requesting_device_session_id=None,
    requesting_connection_nonce=None,
    requesting_connection_epoch=None,
    effective_at_server_ms=None,
    deterministic_dependency_admission=False,
):
    if type(deterministic_dependency_admission) is not bool:
        raise ValueError("deterministicDependencyAdmission must be a boolean")
    if (
        deterministic_dependency_admission
        and action not in _ORDINARY_CONTROL_ACTIONS
    ):
        raise ValueError(
            "Deterministic dependency admission requires an ordinary control action"
        )
    _validate_control_transaction_inputs(
        user_name,
        epoch,
        command_control_version,
        requesting_client_id,
        authority_client_id,
        authority_device_session_id,
        routed_connection_nonce,
        routed_connection_epoch,
        accepted_target,
        accepted_at_ms,
        execution_timeout_ms,
        requesting_device_session_id,
        requesting_connection_nonce,
        requesting_connection_epoch,
        effective_at_server_ms,
    )
    accepted_target_json = _canonical_json(accepted_target)
    identity = _control_transaction_identity(
        user_name,
        requesting_client_id,
        requesting_device_session_id,
        requesting_connection_nonce,
        requesting_connection_epoch,
        authority_client_id,
        authority_device_session_id,
        routed_connection_nonce,
        routed_connection_epoch,
        action,
        accepted_target_json,
        accepted_at_ms,
        execution_timeout_ms,
        effective_at_server_ms,
    )
    existing = EmoPlaybackControlTransaction.get_or_none(
        (EmoPlaybackControlTransaction.playback_context_id == playback_context_id)
        & (EmoPlaybackControlTransaction.epoch == epoch)
        & (
            EmoPlaybackControlTransaction.command_control_version
            == command_control_version
        )
    )
    if existing is not None:
        existing_identity = _control_transaction_identity(
            existing.user_name,
            existing.requesting_client_id,
            existing.requesting_device_session_id,
            existing.requesting_connection_nonce,
            existing.requesting_connection_epoch,
            existing.authority_client_id,
            existing.authority_device_session_id,
            existing.routed_connection_nonce,
            existing.routed_connection_epoch,
            existing.action,
            existing.accepted_target_json,
            existing.accepted_at_ms,
            existing.execution_timeout_ms,
            existing.effective_at_server_ms,
        )
        if existing_identity != identity:
            raise PlaybackControlTransactionConflictError(
                "Control transaction identity conflict"
            )
        return existing, False

    dependency_record = None
    if deterministic_dependency_admission:
        dependency_record = _pending_track_changing_dependency_record(
            playback_context_id,
            epoch,
            command_control_version,
        )
    watchdog_deadline_at_ms = None
    if requesting_device_session_id is None:
        watchdog_deadline_at_ms = accepted_at_ms + execution_timeout_ms + 2000
    record = EmoPlaybackControlTransaction.create(
        playback_context_id=playback_context_id,
        user_name=user_name,
        epoch=epoch,
        command_control_version=command_control_version,
        requesting_client_id=requesting_client_id,
        authority_client_id=authority_client_id,
        authority_device_session_id=authority_device_session_id,
        routed_connection_nonce=routed_connection_nonce,
        routed_connection_epoch=routed_connection_epoch,
        requesting_device_session_id=requesting_device_session_id,
        requesting_connection_nonce=requesting_connection_nonce,
        requesting_connection_epoch=requesting_connection_epoch,
        action=action,
        accepted_target_json=accepted_target_json,
        status="pending",
        accepted_at_ms=accepted_at_ms,
        execution_timeout_ms=execution_timeout_ms,
        watchdog_deadline_at_ms=watchdog_deadline_at_ms,
        effective_at_server_ms=effective_at_server_ms,
        depends_on_control_version=(
            dependency_record.command_control_version
            if dependency_record is not None
            else None
        ),
    )
    return record, True


@_serialize_strict_playback_context_mutation
def createPlaybackControlTransaction(
    playback_context_id,
    user_name,
    epoch,
    command_control_version,
    requesting_client_id,
    authority_client_id,
    authority_device_session_id,
    routed_connection_nonce,
    routed_connection_epoch,
    action,
    accepted_target,
    accepted_at_ms,
    execution_timeout_ms,
    requesting_device_session_id=None,
    requesting_connection_nonce=None,
    requesting_connection_epoch=None,
    effective_at_server_ms=None,
    deterministic_dependency_admission=False,
):
    open_connection(reuse=True)
    try:
        with _strict_playback_context_transaction():
            record, created = _create_playback_control_transaction_record(
                playback_context_id=playback_context_id,
                user_name=user_name,
                epoch=epoch,
                command_control_version=command_control_version,
                requesting_client_id=requesting_client_id,
                authority_client_id=authority_client_id,
                authority_device_session_id=authority_device_session_id,
                routed_connection_nonce=routed_connection_nonce,
                routed_connection_epoch=routed_connection_epoch,
                action=action,
                accepted_target=accepted_target,
                accepted_at_ms=accepted_at_ms,
                execution_timeout_ms=execution_timeout_ms,
                requesting_device_session_id=requesting_device_session_id,
                requesting_connection_nonce=requesting_connection_nonce,
                requesting_connection_epoch=requesting_connection_epoch,
                effective_at_server_ms=effective_at_server_ms,
                deterministic_dependency_admission=(
                    deterministic_dependency_admission
                ),
            )
            return serializePlaybackControlTransaction(record), created
    finally:
        close_connection()


def getPlaybackControlTransaction(
    playback_context_id,
    epoch,
    command_control_version,
):
    open_connection(reuse=True)
    try:
        record = EmoPlaybackControlTransaction.get_or_none(
            (EmoPlaybackControlTransaction.playback_context_id == playback_context_id)
            & (EmoPlaybackControlTransaction.epoch == epoch)
            & (
                EmoPlaybackControlTransaction.command_control_version
                == command_control_version
            )
        )
        return serializePlaybackControlTransaction(record)
    finally:
        close_connection()


def listPendingPlaybackControlTransactions(playback_context_id, epoch):
    open_connection(reuse=True)
    try:
        query = (
            EmoPlaybackControlTransaction.select()
            .where(
                (EmoPlaybackControlTransaction.playback_context_id == playback_context_id)
                & (EmoPlaybackControlTransaction.epoch == epoch)
                & (EmoPlaybackControlTransaction.status == "pending")
            )
            .order_by(EmoPlaybackControlTransaction.command_control_version)
        )
        return [serializePlaybackControlTransaction(record) for record in query]
    finally:
        close_connection()


def listPendingPlaybackControlTransactionsForAuthorityConnection(
    user_name: str,
    authority_client_id: str,
    authority_device_session_id: str,
    routed_connection_nonce: str,
    routed_connection_epoch: Optional[int] = None,
) -> List[Dict[str, object]]:
    _require_non_empty_string(user_name, "userName", 64)
    _require_non_empty_string(authority_client_id, "authorityClientId", 128)
    _require_non_empty_string(
        authority_device_session_id,
        "authorityDeviceSessionId",
        128,
    )
    _require_non_empty_string(routed_connection_nonce, "routedConnectionNonce", 128)
    if routed_connection_epoch is not None:
        _require_integer(routed_connection_epoch, "routedConnectionEpoch", 1)
    open_connection(reuse=True)
    try:
        conditions = [
            EmoPlaybackControlTransaction.user_name == user_name,
            EmoPlaybackControlTransaction.authority_client_id
            == authority_client_id,
            EmoPlaybackControlTransaction.authority_device_session_id
            == authority_device_session_id,
            EmoPlaybackControlTransaction.routed_connection_nonce
            == routed_connection_nonce,
            EmoPlaybackControlTransaction.status == "pending",
        ]
        if routed_connection_epoch is not None:
            conditions.append(
                EmoPlaybackControlTransaction.routed_connection_epoch
                == routed_connection_epoch
            )
        query = (
            EmoPlaybackControlTransaction.select()
            .where(*conditions)
            .order_by(
                EmoPlaybackControlTransaction.playback_context_id,
                EmoPlaybackControlTransaction.epoch,
                EmoPlaybackControlTransaction.command_control_version,
            )
        )
        return [serializePlaybackControlTransaction(record) for record in query]
    finally:
        close_connection()


def listAllPendingPlaybackControlTransactions() -> List[Dict[str, object]]:
    open_connection(reuse=True)
    try:
        query = (
            EmoPlaybackControlTransaction.select()
            .where(EmoPlaybackControlTransaction.status == "pending")
            .order_by(
                EmoPlaybackControlTransaction.playback_context_id,
                EmoPlaybackControlTransaction.epoch,
                EmoPlaybackControlTransaction.command_control_version,
            )
        )
        return [serializePlaybackControlTransaction(record) for record in query]
    finally:
        close_connection()


def listExpiredPlaybackControlTransactions(deadline_at_ms):
    _require_integer(deadline_at_ms, "deadlineAtMs", 0)
    open_connection(reuse=True)
    try:
        query = (
            EmoPlaybackControlTransaction.select()
            .where(
                (EmoPlaybackControlTransaction.status == "pending")
                & (
                    EmoPlaybackControlTransaction.watchdog_deadline_at_ms
                    .is_null(False)
                )
                & (
                    EmoPlaybackControlTransaction.watchdog_deadline_at_ms
                    <= deadline_at_ms
                )
            )
            .order_by(
                EmoPlaybackControlTransaction.watchdog_deadline_at_ms,
                EmoPlaybackControlTransaction.playback_context_id,
                EmoPlaybackControlTransaction.epoch,
                EmoPlaybackControlTransaction.command_control_version,
            )
        )
        return [serializePlaybackControlTransaction(record) for record in query]
    finally:
        close_connection()


def _control_transaction_record(
    playback_context_id,
    epoch,
    command_control_version,
):
    return EmoPlaybackControlTransaction.get_or_none(
        (
            EmoPlaybackControlTransaction.playback_context_id
            == playback_context_id
        )
        & (EmoPlaybackControlTransaction.epoch == epoch)
        & (
            EmoPlaybackControlTransaction.command_control_version
            == command_control_version
        )
    )


def _mark_control_transaction_record_execution_eligible(
    record,
    execution_eligible_at_ms,
):
    if record.status != "pending":
        raise PlaybackControlTransactionConflictError(
            "Terminal control transaction cannot become eligible"
        )
    if record.depends_on_control_version is not None:
        dependency = _control_transaction_record(
            record.playback_context_id,
            record.epoch,
            record.depends_on_control_version,
        )
        if dependency is None:
            raise PlaybackControlTransactionConflictError(
                "Control transaction dependency is missing"
            )
        if dependency.status != "committed" or dependency.terminal_at_ms is None:
            raise PlaybackControlTransactionConflictError(
                "Control transaction dependency is not committed"
            )
        required_eligible_at_ms = dependency.terminal_at_ms
        if record.effective_at_server_ms is not None:
            required_eligible_at_ms = max(
                required_eligible_at_ms,
                record.effective_at_server_ms,
            )
        if execution_eligible_at_ms != required_eligible_at_ms:
            raise ValueError(
                "executionEligibleAtMs does not match dependency eligibility"
            )
    elif (
        record.effective_at_server_ms is not None
        and execution_eligible_at_ms < record.effective_at_server_ms
    ):
        raise ValueError("executionEligibleAtMs precedes effectiveAtServerMs")
    if record.execution_eligible_at_ms is not None:
        if record.execution_eligible_at_ms != execution_eligible_at_ms:
            raise PlaybackControlTransactionConflictError(
                "Execution eligibility conflicts"
            )
        return serializePlaybackControlTransaction(record), False
    record.execution_eligible_at_ms = execution_eligible_at_ms
    record.watchdog_deadline_at_ms = (
        execution_eligible_at_ms + record.execution_timeout_ms + 2000
    )
    record.updated_at = now()
    record.save(
        only=(
            EmoPlaybackControlTransaction.execution_eligible_at_ms,
            EmoPlaybackControlTransaction.watchdog_deadline_at_ms,
            EmoPlaybackControlTransaction.updated_at,
        )
    )
    return serializePlaybackControlTransaction(record), True


def _settle_playback_control_transaction_record(
    record,
    status,
    terminal_at_ms,
    error_code=None,
    depends_on_control_version=None,
    applied_control_version=None,
    error_message=None,
    terminal_identity=None,
):
    persisted_dependency = record.depends_on_control_version
    if depends_on_control_version is not None:
        _require_integer(
            depends_on_control_version,
            "dependsOnControlVersion",
            1,
        )
        if depends_on_control_version >= record.command_control_version:
            raise ValueError(
                "dependsOnControlVersion must precede commandControlVersion"
            )
        if (
            persisted_dependency is not None
            and persisted_dependency != depends_on_control_version
        ):
            raise PlaybackControlTransactionConflictError(
                "Control transaction direct dependency conflicts"
            )
        persisted_dependency = depends_on_control_version

    if terminal_identity is None:
        terminal_identity = {
            "status": status,
            "errorCode": error_code,
            "dependsOnControlVersion": persisted_dependency,
            "appliedControlVersion": applied_control_version,
        }
        if error_message is not None:
            terminal_identity["errorMessage"] = error_message
    terminal_fingerprint = _json_fingerprint(terminal_identity)
    if record.status != "pending":
        if record.terminal_fingerprint != terminal_fingerprint:
            raise PlaybackControlTransactionConflictError(
                "Control transaction terminal conflict"
            )
        return record, False

    _require_terminal_time_after_eligibility(record, terminal_at_ms)
    updated = (
        EmoPlaybackControlTransaction.update(
            status=status,
            error_code=error_code,
            error_message=error_message,
            depends_on_control_version=persisted_dependency,
            applied_control_version=applied_control_version,
            terminal_fingerprint=terminal_fingerprint,
            terminal_at_ms=terminal_at_ms,
            updated_at=now(),
        )
        .where(
            (
                EmoPlaybackControlTransaction.playback_context_id
                == record.playback_context_id
            )
            & (EmoPlaybackControlTransaction.epoch == record.epoch)
            & (
                EmoPlaybackControlTransaction.command_control_version
                == record.command_control_version
            )
            & (EmoPlaybackControlTransaction.status == "pending")
        )
        .execute()
    )
    if updated != 1:
        raise PlaybackControlTransactionConflictError(
            "Control transaction changed concurrently"
        )
    return _control_transaction_record(
        record.playback_context_id,
        record.epoch,
        record.command_control_version,
    ), True


def _pending_direct_control_dependents(record):
    return list(
        EmoPlaybackControlTransaction.select()
        .where(
            (
                EmoPlaybackControlTransaction.playback_context_id
                == record.playback_context_id
            )
            & (EmoPlaybackControlTransaction.epoch == record.epoch)
            & (EmoPlaybackControlTransaction.status == "pending")
            & (
                EmoPlaybackControlTransaction.depends_on_control_version
                == record.command_control_version
            )
        )
        .order_by(EmoPlaybackControlTransaction.command_control_version)
    )


def _legacy_pending_control_dependents(record):
    if record.action not in _TRACK_CHANGING_CONTROL_ACTIONS:
        return []
    return list(
        EmoPlaybackControlTransaction.select()
        .where(
            (
                EmoPlaybackControlTransaction.playback_context_id
                == record.playback_context_id
            )
            & (EmoPlaybackControlTransaction.epoch == record.epoch)
            & (EmoPlaybackControlTransaction.status == "pending")
            & (
                EmoPlaybackControlTransaction.command_control_version
                > record.command_control_version
            )
            & (
                EmoPlaybackControlTransaction.depends_on_control_version.is_null(
                    True
                )
            )
        )
        .order_by(EmoPlaybackControlTransaction.command_control_version)
    )


def _resolve_playback_control_dependency_outcome(
    record,
    terminal_at_ms,
    allow_legacy_track_change_fallback=False,
):
    eligible_transactions = []
    dependency_settlements = []
    direct_dependents = _pending_direct_control_dependents(record)

    if record.status == "committed":
        for dependent in direct_dependents:
            eligible_at_ms = terminal_at_ms
            if dependent.effective_at_server_ms is not None:
                eligible_at_ms = max(
                    eligible_at_ms,
                    dependent.effective_at_server_ms,
                )
            eligible, changed = _mark_control_transaction_record_execution_eligible(
                dependent,
                eligible_at_ms,
            )
            if changed:
                eligible_transactions.append(eligible)
        return eligible_transactions, dependency_settlements

    if record.status not in {"failed", "superseded"}:
        return eligible_transactions, dependency_settlements

    if not direct_dependents and allow_legacy_track_change_fallback:
        direct_dependents = _legacy_pending_control_dependents(record)

    pending = []
    for dependent in direct_dependents:
        heapq.heappush(
            pending,
            (
                dependent.command_control_version,
                record.command_control_version,
                dependent,
            ),
        )
    while pending:
        _command_version, direct_dependency_version, dependent = heapq.heappop(
            pending
        )
        dependent, changed = _settle_playback_control_transaction_record(
            dependent,
            "failed",
            terminal_at_ms,
            error_code="dependency_failed",
            depends_on_control_version=direct_dependency_version,
            applied_control_version=record.applied_control_version,
        )
        if not changed:
            continue
        dependency_settlements.append(
            serializePlaybackControlTransaction(dependent)
        )
        for child in _pending_direct_control_dependents(dependent):
            heapq.heappush(
                pending,
                (
                    child.command_control_version,
                    dependent.command_control_version,
                    child,
                ),
            )

    dependency_settlements.sort(
        key=lambda transaction: transaction["commandControlVersion"]
    )
    return eligible_transactions, dependency_settlements


@_serialize_strict_playback_context_mutation
def markPlaybackControlTransactionExecutionEligible(
    playback_context_id,
    epoch,
    command_control_version,
    execution_eligible_at_ms,
):
    _require_integer(epoch, "epoch", 1)
    _require_integer(command_control_version, "commandControlVersion", 1)
    _require_integer(execution_eligible_at_ms, "executionEligibleAtMs", 0)
    open_connection(reuse=True)
    try:
        with _strict_playback_context_transaction():
            record = _control_transaction_record(
                playback_context_id,
                epoch,
                command_control_version,
            )
            if record is None:
                return None, False
            return _mark_control_transaction_record_execution_eligible(
                record,
                execution_eligible_at_ms,
            )
    finally:
        close_connection()


@_serialize_strict_playback_context_mutation
def settlePlaybackControlTransaction(
    playback_context_id,
    epoch,
    command_control_version,
    status,
    terminal_at_ms,
    error_code=None,
    depends_on_control_version=None,
    applied_control_version=None,
    error_message=None,
):
    if status not in {"committed", "failed", "superseded"}:
        raise ValueError("Invalid control transaction terminal status")
    _require_integer(terminal_at_ms, "terminalAtMs", 0)
    if error_message is not None and not isinstance(error_message, str):
        raise ValueError("errorMessage must be a string")
    if status != "failed" and error_message is not None:
        raise ValueError("Only failed transactions may contain errorMessage")
    open_connection(reuse=True)
    try:
        with _strict_playback_context_transaction():
            record = _control_transaction_record(
                playback_context_id,
                epoch,
                command_control_version,
            )
            if record is None:
                return PlaybackControlTransactionSettlementResult(None, False)
            record, changed = _settle_playback_control_transaction_record(
                record,
                status,
                terminal_at_ms,
                error_code=error_code,
                depends_on_control_version=depends_on_control_version,
                applied_control_version=applied_control_version,
                error_message=error_message,
            )
            eligible_transactions = []
            dependency_settlements = []
            if changed:
                (
                    eligible_transactions,
                    dependency_settlements,
                ) = _resolve_playback_control_dependency_outcome(
                    record,
                    terminal_at_ms,
                )
            return PlaybackControlTransactionSettlementResult(
                serializePlaybackControlTransaction(record),
                changed,
                eligible_transactions,
                dependency_settlements,
            )
    finally:
        close_connection()


def serializeCoreStartupRecovery(record):
    if record is None:
        return None
    return {
        "recoveryFingerprint": record.recovery_fingerprint,
        "status": record.status,
        "startedAtMs": record.started_at_ms,
        "completedAtMs": record.completed_at_ms,
        "pendingCount": record.pending_count,
        "incompleteGenerationCount": record.incomplete_generation_count,
        "recoveredRootCount": record.recovered_root_count,
        "recoveredDependencyCount": record.recovered_dependency_count,
        "outcomeFingerprint": record.outcome_fingerprint,
    }


def getCoreStartupRecovery(recovery_fingerprint):
    _require_non_empty_string(
        recovery_fingerprint,
        "recoveryFingerprint",
        64,
    )
    open_connection(reuse=True)
    try:
        record = EmoCoreStartupRecovery.get_or_none(
            EmoCoreStartupRecovery.recovery_fingerprint
            == recovery_fingerprint
        )
        return serializeCoreStartupRecovery(record)
    finally:
        close_connection()


def listCoreStartupRecoveries():
    open_connection(reuse=True)
    try:
        query = EmoCoreStartupRecovery.select().order_by(
            EmoCoreStartupRecovery.completed_at_ms,
            EmoCoreStartupRecovery.recovery_fingerprint,
        )
        return [serializeCoreStartupRecovery(record) for record in query]
    finally:
        close_connection()


def _require_retention_limit(value, field_name):
    _require_integer(value, field_name, 0)
    if value > STRICT_CORE_RETENTION_LIMIT:
        raise ValueError(
            "%s must not exceed %d" % (field_name, STRICT_CORE_RETENTION_LIMIT)
        )


def _core_retention_event_key(record_kind, record):
    if record_kind == "control":
        return (
            record.terminal_at_ms,
            record.epoch,
            record.command_control_version,
            0,
            str(record.id),
        )
    return (
        record.server_updated_at_ms,
        record.epoch,
        record.reconciliation_control_version,
        1,
        str(record.id),
    )


def _recent_core_retention_record_ids(playback_context_id, retention_limit):
    if retention_limit == 0:
        return set(), set()
    controls = list(
        EmoPlaybackControlTransaction.select(
            EmoPlaybackControlTransaction.id,
            EmoPlaybackControlTransaction.terminal_at_ms,
            EmoPlaybackControlTransaction.epoch,
            EmoPlaybackControlTransaction.command_control_version,
        )
        .where(
            (EmoPlaybackControlTransaction.playback_context_id
             == playback_context_id)
            & (EmoPlaybackControlTransaction.status.in_(_TERMINAL_CONTROL_STATUSES))
            & (EmoPlaybackControlTransaction.terminal_at_ms.is_null(False))
        )
        .order_by(
            EmoPlaybackControlTransaction.terminal_at_ms.desc(),
            EmoPlaybackControlTransaction.epoch.desc(),
            EmoPlaybackControlTransaction.command_control_version.desc(),
            EmoPlaybackControlTransaction.id.desc(),
        )
        .limit(retention_limit)
    )
    reconciliations = list(
        EmoPlaybackControlReconciliation.select(
            EmoPlaybackControlReconciliation.id,
            EmoPlaybackControlReconciliation.server_updated_at_ms,
            EmoPlaybackControlReconciliation.epoch,
            EmoPlaybackControlReconciliation.reconciliation_control_version,
        )
        .where(
            EmoPlaybackControlReconciliation.playback_context_id
            == playback_context_id
        )
        .order_by(
            EmoPlaybackControlReconciliation.server_updated_at_ms.desc(),
            EmoPlaybackControlReconciliation.epoch.desc(),
            EmoPlaybackControlReconciliation.reconciliation_control_version.desc(),
            EmoPlaybackControlReconciliation.id.desc(),
        )
        .limit(retention_limit)
    )
    recent = [
        (_core_retention_event_key("control", record), "control", record.id)
        for record in controls
    ]
    recent.extend(
        (
            _core_retention_event_key("reconciliation", record),
            "reconciliation",
            record.id,
        )
        for record in reconciliations
    )
    recent.sort(key=lambda item: item[0], reverse=True)
    recent = recent[:retention_limit]
    return (
        {record_id for _key, kind, record_id in recent if kind == "control"},
        {
            record_id
            for _key, kind, record_id in recent
            if kind == "reconciliation"
        },
    )


def _oldest_unreferenced_terminal_control(
    playback_context_id,
    cutoff_at_ms,
    retained_control_ids,
):
    dependent = EmoPlaybackControlTransaction.alias("dependent_control")
    audit = EmoPlaybackControlReconciliation.alias("control_audit")
    dependency_reference = fn.EXISTS(
        dependent.select(dependent.id).where(
            (dependent.playback_context_id
             == EmoPlaybackControlTransaction.playback_context_id)
            & (dependent.epoch == EmoPlaybackControlTransaction.epoch)
            & (
                dependent.depends_on_control_version
                == EmoPlaybackControlTransaction.command_control_version
            )
        )
    )
    audit_reference = fn.EXISTS(
        audit.select(audit.id).where(
            (audit.playback_context_id
             == EmoPlaybackControlTransaction.playback_context_id)
            & (audit.epoch == EmoPlaybackControlTransaction.epoch)
            & (
                (
                    audit.trigger_command_control_version
                    == EmoPlaybackControlTransaction.command_control_version
                )
                | (
                    (
                        audit.from_applied_control_version
                        < EmoPlaybackControlTransaction.command_control_version
                    )
                    & (
                        audit.through_control_version
                        >= EmoPlaybackControlTransaction.command_control_version
                    )
                )
                | (
                    (
                        EmoPlaybackControlTransaction.reconciled_by_control_version
                        .is_null(False)
                    )
                    & (
                        audit.reconciliation_control_version
                        == EmoPlaybackControlTransaction.reconciled_by_control_version
                    )
                )
            )
        )
    )
    where = (
        (EmoPlaybackControlTransaction.playback_context_id
         == playback_context_id)
        & (EmoPlaybackControlTransaction.status.in_(_TERMINAL_CONTROL_STATUSES))
        & (EmoPlaybackControlTransaction.terminal_at_ms.is_null(False))
        & (EmoPlaybackControlTransaction.terminal_at_ms <= cutoff_at_ms)
        & (EmoPlaybackControlTransaction.terminal_fingerprint.is_null(False))
        & (EmoPlaybackControlTransaction.terminal_fingerprint != "")
        & (
            EmoPlaybackControlTransaction.requesting_device_session_id
            .is_null(False)
        )
        & (EmoPlaybackControlTransaction.requesting_device_session_id != "")
        & (
            EmoPlaybackControlTransaction.requesting_connection_nonce
            .is_null(False)
        )
        & (EmoPlaybackControlTransaction.requesting_connection_nonce != "")
        & (EmoPlaybackControlTransaction.requesting_connection_epoch == 1)
        & (EmoPlaybackControlTransaction.authority_client_id != "")
        & (EmoPlaybackControlTransaction.authority_device_session_id != "")
        & (EmoPlaybackControlTransaction.routed_connection_nonce != "")
        & (EmoPlaybackControlTransaction.routed_connection_epoch == 1)
        & ~dependency_reference
        & ~audit_reference
    )
    if retained_control_ids:
        where &= ~EmoPlaybackControlTransaction.id.in_(retained_control_ids)
    return (
        EmoPlaybackControlTransaction.select()
        .where(where)
        .order_by(
            EmoPlaybackControlTransaction.terminal_at_ms,
            EmoPlaybackControlTransaction.epoch,
            EmoPlaybackControlTransaction.command_control_version,
            EmoPlaybackControlTransaction.id,
        )
        .first()
    )


def cleanupStrictPlaybackContextRetention(
    playback_context_id,
    now_ms,
    batch_size=STRICT_CORE_CLEANUP_BATCH_SIZE,
    terminal_retention_limit=STRICT_CORE_RETENTION_LIMIT,
    local_intent_retention_limit=STRICT_CORE_RETENTION_LIMIT,
):
    """Delete only retry-safe Core history for one durable Context."""
    _require_non_empty_string(playback_context_id, "playbackContextId", 128)
    _require_integer(now_ms, "nowMs", 0)
    _require_integer(batch_size, "batchSize", 1)
    if batch_size > STRICT_CORE_RETENTION_LIMIT:
        raise ValueError(
            "batchSize must not exceed %d" % STRICT_CORE_RETENTION_LIMIT
        )
    _require_retention_limit(terminal_retention_limit, "terminalRetentionLimit")
    _require_retention_limit(
        local_intent_retention_limit,
        "localIntentRetentionLimit",
    )
    cutoff_at_ms = max(0, now_ms - STRICT_CORE_RETRY_WINDOW_MS)
    cutoff_at = datetime.fromtimestamp(cutoff_at_ms / 1000.0)
    result = {
        "playbackContextId": playback_context_id,
        "cutoffAtMs": cutoff_at_ms,
        "batchSize": batch_size,
        "deletedControlTransactions": 0,
        "deletedReconciliations": 0,
        "deletedLocalIntents": 0,
        "deletedTotal": 0,
        "blockedByFence": False,
        "closeTombstonePreserved": False,
    }
    with _strict_playback_context_lock(playback_context_id):
        open_connection(reuse=True)
        try:
            with _strict_playback_context_transaction():
                context = EmoPlaybackContext.get_or_none(
                    EmoPlaybackContext.playback_context_id
                    == playback_context_id
                )
                if context is None:
                    return result
                result["closeTombstonePreserved"] = context.lifecycle == "closed"
                if _broadcast_fences_for_context(playback_context_id):
                    result["blockedByFence"] = True
                    return result

                (
                    retained_control_ids,
                    retained_reconciliation_ids,
                ) = _recent_core_retention_record_ids(
                    playback_context_id,
                    terminal_retention_limit,
                )

                reconciliation_where = (
                    (EmoPlaybackControlReconciliation.playback_context_id
                     == playback_context_id)
                    & (
                        EmoPlaybackControlReconciliation.server_updated_at_ms
                        <= cutoff_at_ms
                    )
                )
                if retained_reconciliation_ids:
                    reconciliation_where &= ~(
                        EmoPlaybackControlReconciliation.id.in_(
                            retained_reconciliation_ids
                        )
                    )
                reconciliation_ids = [
                    record.id
                    for record in (
                        EmoPlaybackControlReconciliation.select(
                            EmoPlaybackControlReconciliation.id
                        )
                        .where(reconciliation_where)
                        .order_by(
                            EmoPlaybackControlReconciliation.server_updated_at_ms,
                            EmoPlaybackControlReconciliation.epoch,
                            EmoPlaybackControlReconciliation.reconciliation_control_version,
                            EmoPlaybackControlReconciliation.id,
                        )
                        .limit(batch_size)
                    )
                ]
                if reconciliation_ids:
                    result["deletedReconciliations"] = (
                        EmoPlaybackControlReconciliation.delete()
                        .where(
                            EmoPlaybackControlReconciliation.id.in_(
                                reconciliation_ids
                            )
                        )
                        .execute()
                    )

                while (
                    result["deletedReconciliations"]
                    + result["deletedControlTransactions"]
                    < batch_size
                ):
                    candidate = _oldest_unreferenced_terminal_control(
                        playback_context_id,
                        cutoff_at_ms,
                        retained_control_ids,
                    )
                    if candidate is None:
                        break
                    result["deletedControlTransactions"] += (
                        EmoPlaybackControlTransaction.delete()
                        .where(EmoPlaybackControlTransaction.id == candidate.id)
                        .execute()
                    )

                remaining = batch_size - (
                    result["deletedReconciliations"]
                    + result["deletedControlTransactions"]
                )
                if remaining:
                    retained_intent_ids = {
                        record.id
                        for record in (
                            EmoPlaybackLocalIntent.select(
                                EmoPlaybackLocalIntent.id
                            )
                            .where(
                                EmoPlaybackLocalIntent.playback_context_id
                                == playback_context_id
                            )
                            .order_by(
                                EmoPlaybackLocalIntent.created_at.desc(),
                                EmoPlaybackLocalIntent.epoch.desc(),
                                EmoPlaybackLocalIntent.control_version.desc(),
                                EmoPlaybackLocalIntent.id.desc(),
                            )
                            .limit(local_intent_retention_limit)
                        )
                    }
                    intent_where = (
                        (EmoPlaybackLocalIntent.playback_context_id
                         == playback_context_id)
                        & (EmoPlaybackLocalIntent.created_at <= cutoff_at)
                    )
                    if retained_intent_ids:
                        intent_where &= ~EmoPlaybackLocalIntent.id.in_(
                            retained_intent_ids
                        )
                    intent_ids = [
                        record.id
                        for record in (
                            EmoPlaybackLocalIntent.select(
                                EmoPlaybackLocalIntent.id
                            )
                            .where(intent_where)
                            .order_by(
                                EmoPlaybackLocalIntent.created_at,
                                EmoPlaybackLocalIntent.epoch,
                                EmoPlaybackLocalIntent.control_version,
                                EmoPlaybackLocalIntent.id,
                            )
                            .limit(remaining)
                        )
                    ]
                    if intent_ids:
                        result["deletedLocalIntents"] = (
                            EmoPlaybackLocalIntent.delete()
                            .where(EmoPlaybackLocalIntent.id.in_(intent_ids))
                            .execute()
                        )

                result["deletedTotal"] = (
                    result["deletedControlTransactions"]
                    + result["deletedReconciliations"]
                    + result["deletedLocalIntents"]
                )
                return result
        finally:
            close_connection()


def cleanupCoreStartupRecoveryRetention(
    now_ms,
    batch_size=STRICT_CORE_CLEANUP_BATCH_SIZE,
    retention_limit=STRICT_CORE_RETENTION_LIMIT,
):
    """Bound completed recovery audit history without touching live recovery."""
    _require_integer(now_ms, "nowMs", 0)
    _require_integer(batch_size, "batchSize", 1)
    if batch_size > STRICT_CORE_RETENTION_LIMIT:
        raise ValueError(
            "batchSize must not exceed %d" % STRICT_CORE_RETENTION_LIMIT
        )
    _require_retention_limit(retention_limit, "retentionLimit")
    cutoff_at_ms = max(0, now_ms - STRICT_CORE_RETRY_WINDOW_MS)
    with _core_startup_recovery_lock:
        open_connection(reuse=True)
        try:
            with _strict_playback_context_transaction():
                retained_ids = {
                    record.id
                    for record in (
                        EmoCoreStartupRecovery.select(EmoCoreStartupRecovery.id)
                        .where(EmoCoreStartupRecovery.status == "completed")
                        .order_by(
                            EmoCoreStartupRecovery.completed_at_ms.desc(),
                            EmoCoreStartupRecovery.recovery_fingerprint.desc(),
                            EmoCoreStartupRecovery.id.desc(),
                        )
                        .limit(retention_limit)
                    )
                }
                recovered = EmoPlaybackControlTransaction.alias(
                    "startup_recovered_control"
                )
                referenced_terminal = fn.EXISTS(
                    recovered.select(recovered.id).where(
                        (
                            recovered.terminal_at_ms
                            == EmoCoreStartupRecovery.completed_at_ms
                        )
                        & (recovered.status.in_(_TERMINAL_CONTROL_STATUSES))
                        & (
                            recovered.error_code.in_(
                                ("execution_unknown", "dependency_failed")
                            )
                        )
                    )
                )
                where = (
                    (EmoCoreStartupRecovery.status == "completed")
                    & (EmoCoreStartupRecovery.completed_at_ms <= cutoff_at_ms)
                    & ~referenced_terminal
                )
                if retained_ids:
                    where &= ~EmoCoreStartupRecovery.id.in_(retained_ids)
                recovery_ids = [
                    record.id
                    for record in (
                        EmoCoreStartupRecovery.select(EmoCoreStartupRecovery.id)
                        .where(where)
                        .order_by(
                            EmoCoreStartupRecovery.completed_at_ms,
                            EmoCoreStartupRecovery.recovery_fingerprint,
                            EmoCoreStartupRecovery.id,
                        )
                        .limit(batch_size)
                    )
                ]
                deleted = 0
                if recovery_ids:
                    deleted = (
                        EmoCoreStartupRecovery.delete()
                        .where(EmoCoreStartupRecovery.id.in_(recovery_ids))
                        .execute()
                    )
                return {
                    "cutoffAtMs": cutoff_at_ms,
                    "batchSize": batch_size,
                    "deletedStartupRecoveries": deleted,
                    "deletedTotal": deleted,
                }
        finally:
            close_connection()


def _startup_recovery_pending_records():
    return list(
        EmoPlaybackControlTransaction.select()
        .where(EmoPlaybackControlTransaction.status == "pending")
        .order_by(
            EmoPlaybackControlTransaction.playback_context_id,
            EmoPlaybackControlTransaction.epoch,
            EmoPlaybackControlTransaction.command_control_version,
        )
    )


def _startup_recovery_generation_is_complete(record):
    string_values = (
        record.requesting_client_id,
        record.requesting_device_session_id,
        record.requesting_connection_nonce,
        record.authority_client_id,
        record.authority_device_session_id,
        record.routed_connection_nonce,
    )
    return bool(
        all(isinstance(value, str) and value.strip() for value in string_values)
        and type(record.requesting_connection_epoch) is int
        and record.requesting_connection_epoch == 1
        and type(record.routed_connection_epoch) is int
        and record.routed_connection_epoch == 1
    )


def _startup_recovery_batch_identity(records):
    return [
        {
            "playbackContextId": record.playback_context_id,
            "userName": record.user_name,
            "epoch": record.epoch,
            "commandControlVersion": record.command_control_version,
            "requestingClientId": record.requesting_client_id,
            "requestingDeviceSessionId": record.requesting_device_session_id,
            "requestingConnectionNonce": record.requesting_connection_nonce,
            "requestingConnectionEpoch": record.requesting_connection_epoch,
            "authorityClientId": record.authority_client_id,
            "authorityDeviceSessionId": record.authority_device_session_id,
            "routedConnectionNonce": record.routed_connection_nonce,
            "routedConnectionEpoch": record.routed_connection_epoch,
            "action": record.action,
            "dependsOnControlVersion": record.depends_on_control_version,
            "executionEligibleAtMs": record.execution_eligible_at_ms,
            "watchdogDeadlineAtMs": record.watchdog_deadline_at_ms,
        }
        for record in records
    ]


def _startup_recovery_terminal_identity(record):
    return {
        "playbackContextId": record.playback_context_id,
        "epoch": record.epoch,
        "commandControlVersion": record.command_control_version,
        "status": record.status,
        "errorCode": record.error_code,
        "dependsOnControlVersion": record.depends_on_control_version,
        "appliedControlVersion": record.applied_control_version,
        "terminalAtMs": record.terminal_at_ms,
    }


def recoverPendingPlaybackControlsForStartup(started_at_ms):
    """Atomically terminalize every control that survived a server restart."""
    _require_integer(started_at_ms, "startedAtMs", 0)
    with _core_startup_recovery_lock:
        while True:
            open_connection(reuse=True)
            try:
                context_ids = {
                    record.playback_context_id
                    for record in _startup_recovery_pending_records()
                }
            finally:
                close_connection()

            retry_with_more_locks = False
            with _strict_playback_context_lock_set(context_ids):
                open_connection(reuse=True)
                try:
                    with _strict_playback_context_transaction():
                        records = _startup_recovery_pending_records()
                        current_context_ids = {
                            record.playback_context_id for record in records
                        }
                        if not current_context_ids.issubset(context_ids):
                            retry_with_more_locks = True
                            continue

                        recovery_fingerprint = _json_fingerprint(
                            _startup_recovery_batch_identity(records)
                        )
                        existing = EmoCoreStartupRecovery.get_or_none(
                            EmoCoreStartupRecovery.recovery_fingerprint
                            == recovery_fingerprint
                        )
                        if existing is not None:
                            if records:
                                raise PlaybackControlTransactionConflictError(
                                    "Completed startup recovery still has pending controls"
                                )
                            return {
                                "recovery": serializeCoreStartupRecovery(existing),
                                "recoveredTransactions": [],
                                "mutated": False,
                            }

                        terminal_at_ms = max(
                            started_at_ms,
                            int(time.time() * 1000),
                            max(
                                (
                                    record.execution_eligible_at_ms or 0
                                    for record in records
                                ),
                                default=0,
                            ),
                        )
                        initial_pending_count = len(records)
                        incomplete_generation_count = sum(
                            not _startup_recovery_generation_is_complete(record)
                            for record in records
                        )
                        recovered = []
                        recovered_root_count = 0
                        recovered_dependency_count = 0

                        pending_keys = {
                            (
                                record.playback_context_id,
                                record.epoch,
                                record.command_control_version,
                            )
                            for record in records
                        }
                        roots = [
                            record
                            for record in records
                            if record.depends_on_control_version is None
                            or (
                                record.playback_context_id,
                                record.epoch,
                                record.depends_on_control_version,
                            )
                            not in pending_keys
                        ]

                        for initial_record in roots:
                            record = _control_transaction_record(
                                initial_record.playback_context_id,
                                initial_record.epoch,
                                initial_record.command_control_version,
                            )
                            if record is None or record.status != "pending":
                                continue
                            dependency = None
                            if record.depends_on_control_version is not None:
                                dependency = _control_transaction_record(
                                    record.playback_context_id,
                                    record.epoch,
                                    record.depends_on_control_version,
                                )
                            dependency_failed = bool(
                                record.depends_on_control_version is not None
                                and (
                                    dependency is None
                                    or dependency.status != "committed"
                                )
                            )
                            error_code = (
                                "dependency_failed"
                                if dependency_failed
                                else "execution_unknown"
                            )
                            applied_control_version = (
                                None
                                if dependency is None
                                else dependency.applied_control_version
                            )
                            record, changed = _settle_playback_control_transaction_record(
                                record,
                                "failed",
                                terminal_at_ms,
                                error_code=error_code,
                                depends_on_control_version=(
                                    record.depends_on_control_version
                                    if dependency_failed
                                    else None
                                ),
                                applied_control_version=applied_control_version,
                            )
                            if not changed:
                                continue
                            recovered_root_count += 1
                            if dependency_failed:
                                recovered_dependency_count += 1
                            recovered.append(record)
                            (
                                _eligible,
                                dependency_settlements,
                            ) = _resolve_playback_control_dependency_outcome(
                                record,
                                terminal_at_ms,
                            )
                            recovered_dependency_count += len(
                                dependency_settlements
                            )
                            for settlement in dependency_settlements:
                                recovered_record = _control_transaction_record(
                                    settlement["playbackContextId"],
                                    settlement["epoch"],
                                    settlement["commandControlVersion"],
                                )
                                if recovered_record is not None:
                                    recovered.append(recovered_record)

                        for initial_record in records:
                            record = _control_transaction_record(
                                initial_record.playback_context_id,
                                initial_record.epoch,
                                initial_record.command_control_version,
                            )
                            if record is None or record.status != "pending":
                                continue
                            record, changed = _settle_playback_control_transaction_record(
                                record,
                                "failed",
                                terminal_at_ms,
                                error_code="execution_unknown",
                            )
                            if changed:
                                recovered_root_count += 1
                                recovered.append(record)

                        recovered.sort(
                            key=lambda record: (
                                record.playback_context_id,
                                record.epoch,
                                record.command_control_version,
                            )
                        )
                        outcome_fingerprint = _json_fingerprint(
                            [
                                _startup_recovery_terminal_identity(record)
                                for record in recovered
                            ]
                        )
                        marker = EmoCoreStartupRecovery.create(
                            recovery_fingerprint=recovery_fingerprint,
                            status="completed",
                            started_at_ms=started_at_ms,
                            completed_at_ms=terminal_at_ms,
                            pending_count=initial_pending_count,
                            incomplete_generation_count=(
                                incomplete_generation_count
                            ),
                            recovered_root_count=recovered_root_count,
                            recovered_dependency_count=(
                                recovered_dependency_count
                            ),
                            outcome_fingerprint=outcome_fingerprint,
                        )
                        return {
                            "recovery": serializeCoreStartupRecovery(marker),
                            "recoveredTransactions": [
                                serializePlaybackControlTransaction(record)
                                for record in recovered
                            ],
                            "mutated": bool(recovered),
                        }
                finally:
                    close_connection()
            if not retry_with_more_locks:
                raise PlaybackControlTransactionConflictError(
                    "Startup recovery lock set changed unexpectedly"
                )


def serializePlaybackControlReconciliation(record):
    if record is None:
        return None
    payload = {
        "playbackContextId": record.playback_context_id,
        "userName": record.user_name,
        "epoch": record.epoch,
        "reconciliationControlVersion": record.reconciliation_control_version,
        "fromAppliedControlVersion": record.from_applied_control_version,
        "throughControlVersion": record.through_control_version,
        "triggerKind": record.trigger_kind,
        "actualFactFingerprint": record.actual_fact_fingerprint,
        "actualFact": _load_json_object(record.actual_fact_json, required=True),
        "canonicalUpdate": _load_json_object(
            record.canonical_update_json,
            required=True,
        ),
        "serverUpdatedAtMs": record.server_updated_at_ms,
    }
    if record.trigger_command_control_version is not None:
        payload[
            "triggerCommandControlVersion"
        ] = record.trigger_command_control_version
    return payload


def _validate_reconciliation_inputs(
    playback_context_id,
    user_name,
    epoch,
    reconciliation_control_version,
    from_applied_control_version,
    through_control_version,
    trigger_kind,
    actual_fact,
    canonical_update,
    server_updated_at_ms,
    trigger_command_control_version,
):
    _require_non_empty_string(playback_context_id, "playbackContextId", 128)
    _require_non_empty_string(user_name, "userName", 64)
    _require_integer(epoch, "epoch", 1)
    _require_integer(
        reconciliation_control_version,
        "reconciliationControlVersion",
        1,
    )
    _require_integer(
        from_applied_control_version,
        "fromAppliedControlVersion",
        0,
    )
    _require_integer(through_control_version, "throughControlVersion", 1)
    if reconciliation_control_version <= through_control_version:
        raise ValueError(
            "reconciliationControlVersion must exceed throughControlVersion"
        )
    _require_non_empty_string(trigger_kind, "triggerKind", 64)
    if trigger_command_control_version is not None:
        _require_integer(
            trigger_command_control_version,
            "triggerCommandControlVersion",
            1,
        )
    _require_integer(server_updated_at_ms, "serverUpdatedAtMs", 0)
    if not isinstance(actual_fact, dict):
        raise ValueError("actualFact must be an object")
    if not isinstance(canonical_update, dict):
        raise ValueError("canonicalUpdate must be an object")


def _reconciliation_identity(
    record_or_playback_context_id,
    user_name=None,
    epoch=None,
    reconciliation_control_version=None,
    from_applied_control_version=None,
    through_control_version=None,
    trigger_kind=None,
    trigger_command_control_version=None,
    actual_fact_fingerprint=None,
    actual_fact_json=None,
    canonical_update_json=None,
    server_updated_at_ms=None,
):
    if isinstance(record_or_playback_context_id, EmoPlaybackControlReconciliation):
        record = record_or_playback_context_id
        return _reconciliation_identity(
            record.playback_context_id,
            record.user_name,
            record.epoch,
            record.reconciliation_control_version,
            record.from_applied_control_version,
            record.through_control_version,
            record.trigger_kind,
            record.trigger_command_control_version,
            record.actual_fact_fingerprint,
            record.actual_fact_json,
            record.canonical_update_json,
            record.server_updated_at_ms,
        )
    return (
        record_or_playback_context_id,
        user_name,
        epoch,
        reconciliation_control_version,
        from_applied_control_version,
        through_control_version,
        trigger_kind,
        trigger_command_control_version,
        actual_fact_fingerprint,
        actual_fact_json,
        canonical_update_json,
        server_updated_at_ms,
    )


def createPlaybackControlReconciliation(
    playback_context_id,
    user_name,
    epoch,
    reconciliation_control_version,
    from_applied_control_version,
    through_control_version,
    trigger_kind,
    actual_fact,
    canonical_update,
    server_updated_at_ms,
    trigger_command_control_version=None,
):
    _validate_reconciliation_inputs(
        playback_context_id,
        user_name,
        epoch,
        reconciliation_control_version,
        from_applied_control_version,
        through_control_version,
        trigger_kind,
        actual_fact,
        canonical_update,
        server_updated_at_ms,
        trigger_command_control_version,
    )
    actual_fact_json = _canonical_json(actual_fact)
    canonical_update_json = _canonical_json(canonical_update)
    actual_fact_fingerprint = _json_fingerprint(actual_fact)
    identity = _reconciliation_identity(
        playback_context_id,
        user_name,
        epoch,
        reconciliation_control_version,
        from_applied_control_version,
        through_control_version,
        trigger_kind,
        trigger_command_control_version,
        actual_fact_fingerprint,
        actual_fact_json,
        canonical_update_json,
        server_updated_at_ms,
    )
    with _strict_playback_context_lock(playback_context_id):
        open_connection(reuse=True)
        try:
            with _strict_playback_context_transaction():
                record = EmoPlaybackControlReconciliation.get_or_none(
                    (EmoPlaybackControlReconciliation.playback_context_id
                     == playback_context_id)
                    & (EmoPlaybackControlReconciliation.epoch == epoch)
                    & (
                        EmoPlaybackControlReconciliation.reconciliation_control_version
                        == reconciliation_control_version
                    )
                )
                if record is not None:
                    if _reconciliation_identity(record) != identity:
                        raise PlaybackControlReconciliationConflictError(
                            "Reconciliation record identity conflict"
                        )
                    return serializePlaybackControlReconciliation(record), False
                record = EmoPlaybackControlReconciliation.create(
                    playback_context_id=playback_context_id,
                    user_name=user_name,
                    epoch=epoch,
                    reconciliation_control_version=reconciliation_control_version,
                    from_applied_control_version=from_applied_control_version,
                    through_control_version=through_control_version,
                    trigger_kind=trigger_kind,
                    trigger_command_control_version=(
                        trigger_command_control_version
                    ),
                    actual_fact_fingerprint=actual_fact_fingerprint,
                    actual_fact_json=actual_fact_json,
                    canonical_update_json=canonical_update_json,
                    server_updated_at_ms=server_updated_at_ms,
                    created_at=now(),
                    updated_at=now(),
                )
                return serializePlaybackControlReconciliation(record), True
        finally:
            close_connection()


def getPlaybackControlReconciliation(
    playback_context_id,
    epoch,
    reconciliation_control_version,
):
    _require_non_empty_string(playback_context_id, "playbackContextId", 128)
    _require_integer(epoch, "epoch", 1)
    _require_integer(
        reconciliation_control_version,
        "reconciliationControlVersion",
        1,
    )
    open_connection(reuse=True)
    try:
        record = EmoPlaybackControlReconciliation.get_or_none(
            (EmoPlaybackControlReconciliation.playback_context_id
             == playback_context_id)
            & (EmoPlaybackControlReconciliation.epoch == epoch)
            & (
                EmoPlaybackControlReconciliation.reconciliation_control_version
                == reconciliation_control_version
            )
        )
        return serializePlaybackControlReconciliation(record)
    finally:
        close_connection()


def listPlaybackControlReconciliations(playback_context_id, epoch):
    _require_non_empty_string(playback_context_id, "playbackContextId", 128)
    _require_integer(epoch, "epoch", 1)
    open_connection(reuse=True)
    try:
        query = (
            EmoPlaybackControlReconciliation.select()
            .where(
                (EmoPlaybackControlReconciliation.playback_context_id
                 == playback_context_id)
                & (EmoPlaybackControlReconciliation.epoch == epoch)
            )
            .order_by(
                EmoPlaybackControlReconciliation.reconciliation_control_version
            )
        )
        return [serializePlaybackControlReconciliation(record) for record in query]
    finally:
        close_connection()


def serializePlaybackContextCloseTombstone(record):
    if record is None:
        return None
    payload = {
        "playbackContextId": record.playback_context_id,
        "userName": record.user_name,
        "lifecycle": record.lifecycle,
    }
    optional = {
        "closeAction": record.close_action,
        "closeRequestFingerprint": record.close_request_fingerprint,
        "closeExpectedEpoch": record.close_expected_epoch,
        "closeBaseVersion": record.close_base_version,
        "closedFromEpoch": record.closed_from_epoch,
        "closedFromVersion": record.closed_from_version,
        "finalEpoch": record.final_epoch,
        "finalVersion": record.final_version,
        "finalQueueRevision": record.final_queue_revision,
        "finalControlVersion": record.final_control_version,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    if record.close_outcome_json is not None:
        payload["closeOutcome"] = _load_json_object(
            record.close_outcome_json,
            required=True,
        )
    return payload


_PLAYBACK_CONTEXT_CLOSE_TOMBSTONE_FIELDS = (
    "close_action",
    "close_request_fingerprint",
    "close_expected_epoch",
    "close_base_version",
    "closed_from_epoch",
    "closed_from_version",
    "final_epoch",
    "final_version",
    "final_queue_revision",
    "final_control_version",
    "close_outcome_json",
)


def _has_playback_context_close_tombstone_data(record):
    return any(
        getattr(record, field_name) is not None
        for field_name in _PLAYBACK_CONTEXT_CLOSE_TOMBSTONE_FIELDS
    )


def _has_complete_playback_context_close_tombstone(record):
    return all(
        getattr(record, field_name) is not None
        for field_name in _PLAYBACK_CONTEXT_CLOSE_TOMBSTONE_FIELDS
    )


def _closed_playback_context_payload(record):
    payload = _playback_context_payload(record)
    final_fields = {
        "epoch": record.final_epoch,
        "version": record.final_version,
        "queueRevision": record.final_queue_revision,
        "controlVersion": record.final_control_version,
    }
    payload.update(
        {
            field_name: value
            for field_name, value in final_fields.items()
            if value is not None
        }
    )
    return payload


def _playback_context_close_fingerprint(
    playback_context_id,
    expected_epoch,
    base_version,
    close_action,
):
    return _json_fingerprint(
        {
            "action": close_action,
            "payload": {
                "playbackContextId": playback_context_id,
                "expectedEpoch": expected_epoch,
                "baseVersion": base_version,
            },
        }
    )


def getPlaybackContextCloseTombstone(playback_context_id, user_name):
    _require_non_empty_string(playback_context_id, "playbackContextId", 128)
    _require_non_empty_string(user_name, "userName", 64)
    open_connection(reuse=True)
    try:
        record = EmoPlaybackContext.get_or_none(
            (EmoPlaybackContext.playback_context_id == playback_context_id)
            & (EmoPlaybackContext.user_name == user_name)
            & (EmoPlaybackContext.lifecycle == "closed")
        )
        return serializePlaybackContextCloseTombstone(record)
    finally:
        close_connection()


def _strict_playback_update_canonical(
    record,
    client_id,
    device_session_id,
    origin,
    payload,
    applied_control_version,
    client_seq,
    server_updated_at_ms,
    command_control_version=None,
    superseded_through_control_version=None,
):
    canonical = {
        "playbackContextId": record.playback_context_id,
        "sourceClientId": client_id,
        "deviceSessionId": device_session_id,
        "origin": origin,
        "controlVersion": record.control_version,
        "appliedControlVersion": applied_control_version,
        "state": payload["state"],
        "positionMs": payload["positionMs"],
        "positionSampledAtServerMs": payload["positionSampledAtServerMs"],
        "playbackRate": payload["playbackRate"],
        "clientSeq": client_seq,
        "serverUpdatedAtMs": server_updated_at_ms,
    }
    for field_name in ("trackId", "volume", "muted"):
        if field_name in payload:
            canonical[field_name] = payload[field_name]
    if origin == "remoteCommand":
        canonical["executionStatus"] = payload["executionStatus"]
        canonical["commandControlVersion"] = command_control_version
        if payload["executionStatus"] == "failed":
            canonical["errorCode"] = payload["errorCode"]
            if "errorMessage" in payload:
                canonical["errorMessage"] = payload["errorMessage"]
    elif origin == "localUser":
        canonical["executionStatus"] = "committed"
        canonical["intentId"] = payload["intentId"]
        canonical["supersededThroughControlVersion"] = (
            superseded_through_control_version
        )
        canonical["queueIndex"] = payload["queueIndex"]
    return canonical


def _save_strict_device_state_record(
    record,
    existing,
    client_id,
    device_session_id,
    user_name,
    connection_nonce,
    request_fingerprint,
    canonical_update,
):
    playback_json = dict(canonical_update)
    playback_json.update(
        {
            "_connectionNonce": connection_nonce,
            "_settledClientSeq": canonical_update["clientSeq"],
            "_requestFingerprint": request_fingerprint,
            "_canonicalUpdate": canonical_update,
        }
    )
    values = {
        "device_session_id": device_session_id,
        "owner_client_id": client_id,
        "user_name": user_name,
        "state": canonical_update["state"],
        "track_id": canonical_update.get("trackId"),
        "position_ms": canonical_update["positionMs"],
        "volume": canonical_update.get("volume"),
        "is_authority": 1,
        "mode": "normal",
        "context_epoch": record.epoch,
        "applied_control_version": canonical_update[
            "appliedControlVersion"
        ],
        "client_seq": canonical_update["clientSeq"],
        "playback_json": json.dumps(playback_json, ensure_ascii=True),
        "updated_at": now(),
    }
    if existing is None:
        return EmoDevicePlaybackState.create(
            playback_context_id=record.playback_context_id,
            **values,
        )
    for field_name, value in values.items():
        setattr(existing, field_name, value)
    existing.save()
    return existing


def _passive_correction_from_device(
    record,
    device_state,
    client_seq=None,
):
    persisted = _device_playback_state_payload(device_state)
    correction = {
        "playbackContextId": record.playback_context_id,
        "sourceClientId": device_state.owner_client_id,
        "deviceSessionId": device_state.device_session_id,
        "origin": "passive",
        "controlVersion": record.control_version,
        "appliedControlVersion": device_state.applied_control_version,
        "state": device_state.state,
        "positionMs": device_state.position_ms,
        "positionSampledAtServerMs": persisted[
            "positionSampledAtServerMs"
        ],
        "playbackRate": persisted["playbackRate"],
        "clientSeq": (
            device_state.client_seq
            if client_seq is None
            else client_seq
        ),
        "serverUpdatedAtMs": persisted["serverUpdatedAtMs"],
    }
    for field_name in ("trackId", "volume", "muted"):
        if persisted.get(field_name) is not None:
            correction[field_name] = persisted[field_name]
    return correction


def _settle_stale_playback_correction(
    record,
    existing,
    existing_json,
    connection_nonce,
    request_fingerprint,
    client_seq,
):
    canonical = _passive_correction_from_device(
        record,
        existing,
        client_seq=client_seq,
    )
    playback_json = dict(existing_json)
    playback_json.update(canonical)
    playback_json.update(
        {
            "_connectionNonce": connection_nonce,
            "_settledClientSeq": client_seq,
            "_requestFingerprint": request_fingerprint,
            "_canonicalUpdate": canonical,
        }
    )
    if existing.client_seq >= 1:
        existing.client_seq = client_seq
    existing.playback_json = json.dumps(playback_json, ensure_ascii=True)
    existing.save(
        only=(
            EmoDevicePlaybackState.client_seq,
            EmoDevicePlaybackState.playback_json,
        )
    )
    return canonical


def _reconcile_terminal_control_gap(
    record,
    client_id,
    device_session_id,
    origin,
    payload,
    applied_control_version,
    client_seq,
    server_updated_at_ms,
    trigger_command_control_version=None,
):
    through_control_version = record.control_version
    if applied_control_version >= through_control_version:
        return None

    pending_exists = (
        EmoPlaybackControlTransaction.select()
        .where(
            (
                EmoPlaybackControlTransaction.playback_context_id
                == record.playback_context_id
            )
            & (EmoPlaybackControlTransaction.epoch == record.epoch)
            & (EmoPlaybackControlTransaction.status == "pending")
        )
        .exists()
    )
    if pending_exists:
        return None

    gap_records = list(
        EmoPlaybackControlTransaction.select()
        .where(
            (
                EmoPlaybackControlTransaction.playback_context_id
                == record.playback_context_id
            )
            & (EmoPlaybackControlTransaction.epoch == record.epoch)
            & (
                EmoPlaybackControlTransaction.command_control_version
                > applied_control_version
            )
            & (
                EmoPlaybackControlTransaction.command_control_version
                <= through_control_version
            )
        )
        .order_by(EmoPlaybackControlTransaction.command_control_version)
    )
    if (
        not gap_records
        or any(
            gap_record.status not in {"committed", "failed", "superseded"}
            for gap_record in gap_records
        )
        or not any(gap_record.status == "failed" for gap_record in gap_records)
        or any(
            gap_record.reconciled_by_control_version is not None
            for gap_record in gap_records
        )
    ):
        return None

    overlapping_reconciliation = (
        EmoPlaybackControlReconciliation.select()
        .where(
            (
                EmoPlaybackControlReconciliation.playback_context_id
                == record.playback_context_id
            )
            & (EmoPlaybackControlReconciliation.epoch == record.epoch)
            & (
                EmoPlaybackControlReconciliation.through_control_version
                > applied_control_version
            )
            & (
                EmoPlaybackControlReconciliation.through_control_version
                <= through_control_version
            )
        )
        .exists()
    )
    if overlapping_reconciliation:
        return None

    queue_song_ids = json.loads(record.queue_json)
    actual_track_id = payload.get("trackId")
    if (
        not isinstance(actual_track_id, str)
        or not actual_track_id
        or queue_song_ids.count(actual_track_id) != 1
    ):
        return None
    actual_index = queue_song_ids.index(actual_track_id)

    reconciliation_control_version = through_control_version + 1
    if record.current_index != actual_index:
        record.queue_revision += 1
    record.current_index = actual_index
    record.track_id = actual_track_id
    record.state = payload["state"]
    record.position_ms = payload["positionMs"]
    record.origin_client_id = client_id
    record.control_version = reconciliation_control_version
    record.version += 1
    playback_json = (
        json.loads(record.playback_json) if record.playback_json else {}
    )
    playback_json["positionSampledAtServerMs"] = payload[
        "positionSampledAtServerMs"
    ]
    record.playback_json = json.dumps(playback_json, ensure_ascii=True)
    record.updated_at = now()
    record.save()

    canonical = _strict_playback_update_canonical(
        record,
        client_id,
        device_session_id,
        origin,
        payload,
        reconciliation_control_version,
        client_seq,
        server_updated_at_ms,
        command_control_version=trigger_command_control_version,
    )
    actual_fact = {
        "playbackContextId": record.playback_context_id,
        "sourceClientId": client_id,
        "deviceSessionId": device_session_id,
        "origin": origin,
        "appliedControlVersion": applied_control_version,
        "state": payload["state"],
        "trackId": actual_track_id,
        "positionMs": payload["positionMs"],
        "positionSampledAtServerMs": payload["positionSampledAtServerMs"],
        "playbackRate": payload["playbackRate"],
        "clientSeq": client_seq,
        "serverUpdatedAtMs": server_updated_at_ms,
    }
    for field_name in ("volume", "muted"):
        if field_name in payload:
            actual_fact[field_name] = payload[field_name]
    reconciliation = EmoPlaybackControlReconciliation.create(
        playback_context_id=record.playback_context_id,
        user_name=record.user_name,
        epoch=record.epoch,
        reconciliation_control_version=reconciliation_control_version,
        from_applied_control_version=applied_control_version,
        through_control_version=through_control_version,
        trigger_kind=(
            "remote_failed"
            if trigger_command_control_version is not None
            else "passive_terminal_gap"
        ),
        trigger_command_control_version=trigger_command_control_version,
        actual_fact_fingerprint=_json_fingerprint(actual_fact),
        actual_fact_json=_canonical_json(actual_fact),
        canonical_update_json=_canonical_json(canonical),
        server_updated_at_ms=server_updated_at_ms,
        created_at=now(),
        updated_at=now(),
    )
    reconciled_at = now()
    for gap_record in gap_records:
        gap_record.reconciled_by_control_version = (
            reconciliation_control_version
        )
        gap_record.updated_at = reconciled_at
        gap_record.save(
            only=(
                EmoPlaybackControlTransaction.reconciled_by_control_version,
                EmoPlaybackControlTransaction.updated_at,
            )
        )
    return canonical, serializePlaybackControlReconciliation(reconciliation)


def _apply_passive_natural_terminal(
    record,
    client_id,
    payload,
    applied_control_version,
):
    queue_song_ids = json.loads(record.queue_json)
    if (
        applied_control_version != record.control_version
        or not queue_song_ids
        or record.current_index != len(queue_song_ids) - 1
        or record.track_id != queue_song_ids[-1]
        or record.state != "playing"
        or payload["state"] != "stopped"
        or payload.get("trackId") != queue_song_ids[-1]
        or payload["positionMs"] != 0
    ):
        return False
    if (
        EmoPlaybackControlTransaction.select()
        .where(
            (
                EmoPlaybackControlTransaction.playback_context_id
                == record.playback_context_id
            )
            & (EmoPlaybackControlTransaction.epoch == record.epoch)
            & (EmoPlaybackControlTransaction.status == "pending")
        )
        .exists()
    ):
        return False

    record.state = "stopped"
    record.position_ms = 0
    record.origin_client_id = client_id
    record.version += 1
    playback_json = (
        json.loads(record.playback_json) if record.playback_json else {}
    )
    playback_json["positionSampledAtServerMs"] = payload[
        "positionSampledAtServerMs"
    ]
    record.playback_json = json.dumps(playback_json, ensure_ascii=True)
    record.updated_at = now()
    record.save()
    return True


@_serialize_strict_playback_context_mutation
def applyStrictPlaybackUpdate(
    playback_context_id,
    user_name,
    client_id,
    device_session_id,
    connection_nonce,
    payload,
    server_updated_at_ms,
    post_mutation_hook=None,
    require_execution_eligible=False,
):
    payload = dict(payload)
    payload.setdefault("positionSampledAtServerMs", 0)
    payload.setdefault("playbackRate", 1.0)
    request_payload = dict(payload)
    request_fingerprint = _json_fingerprint(request_payload)
    open_connection(reuse=True)
    try:
        with _strict_playback_context_transaction():
            record = _getStrictPlaybackContextRecord(
                playback_context_id,
                user_name,
            )
            if record is None:
                return None
            current = _playback_context_payload(record)
            previous_context = dict(current)
            if (
                record.authority_client_id != client_id
                or record.authority_device_session_id != device_session_id
            ):
                raise PermissionError("Playback update authority binding mismatch")

            existing = EmoDevicePlaybackState.get_or_none(
                (
                    EmoDevicePlaybackState.playback_context_id
                    == playback_context_id
                )
                & (EmoDevicePlaybackState.owner_client_id == client_id)
            )
            existing_json = (
                json.loads(existing.playback_json)
                if existing is not None and existing.playback_json
                else {}
            )
            same_scope = bool(
                existing is not None
                and existing.context_epoch == record.epoch
                and existing.device_session_id == device_session_id
                and existing_json.get("_connectionNonce") == connection_nonce
            )
            settled_client_seq = existing_json.get(
                "_settledClientSeq",
                existing.client_seq if existing is not None else 0,
            )
            if (
                type(settled_client_seq) is not int
                or settled_client_seq < 0
            ):
                settled_client_seq = (
                    existing.client_seq if existing is not None else 0
                )
            elif existing is not None:
                settled_client_seq = max(
                    existing.client_seq,
                    settled_client_seq,
                )
            current_client_seq = settled_client_seq if same_scope else 0
            incoming_client_seq = payload["clientSeq"]
            if payload["positionSampledAtServerMs"] > server_updated_at_ms + 1000:
                raise ValueError(
                    "positionSampledAtServerMs is too far in the future"
                )
            if incoming_client_seq < current_client_seq:
                raise PlaybackClientSequenceConflictError(current_client_seq)
            if incoming_client_seq == current_client_seq and current_client_seq > 0:
                if existing_json.get("_requestFingerprint") != request_fingerprint:
                    raise PlaybackClientSequenceConflictError(current_client_seq)
                return {
                    "playbackContext": current,
                    "deviceState": _device_playback_state_payload(existing),
                    "canonicalUpdate": existing_json["_canonicalUpdate"],
                    "created": False,
                    "sourceOnly": True,
                    "dependencySettlements": [],
                    "terminalControlVersions": [],
                }

            has_applied_baseline = bool(
                existing is not None
                and existing.context_epoch == record.epoch
                and existing.applied_control_version >= 1
            )
            last_applied = (
                existing.applied_control_version
                if has_applied_baseline
                else None
            )
            origin = payload["origin"]
            dependency_records = []
            eligible_dependency_records = []
            terminal_control_versions = []
            control_reconciliation = None
            natural_terminal = False
            handoff_settlement = None
            handoff_source_changes = False
            handoff_record, handoff_role = _active_handoff_for_context(
                playback_context_id,
                user_name=user_name,
            )
            if handoff_record is not None:
                if handoff_role != "source":
                    _raise_handoff_fence(
                        handoff_record,
                        playback_context_id,
                    )
                _require_handoff_generation(handoff_record)
                if not _handoff_generation_matches(
                    handoff_record,
                    "source",
                    user_name,
                    client_id,
                    device_session_id,
                    connection_nonce,
                    handoff_record.source_connection_epoch,
                ):
                    _raise_handoff_fence(
                        handoff_record,
                        playback_context_id,
                    )
                if origin == "remoteCommand":
                    _raise_handoff_fence(
                        handoff_record,
                        playback_context_id,
                    )
                handoff_source_changes = origin == "localUser" or (
                    origin == "passive"
                    and _handoff_source_update_changes_actual(
                        handoff_record,
                        payload,
                        existing=existing if same_scope else None,
                    )
                )

            if origin == "passive":
                applied = payload["appliedControlVersion"]
                if applied > record.control_version:
                    raise ValueError(
                        "appliedControlVersion exceeds canonical controlVersion"
                    )
                if last_applied is not None and applied < last_applied:
                    canonical = _settle_stale_playback_correction(
                        record,
                        existing,
                        existing_json,
                        connection_nonce,
                        request_fingerprint,
                        incoming_client_seq,
                    )
                    return {
                        "playbackContext": current,
                        "deviceState": _device_playback_state_payload(existing),
                        "canonicalUpdate": canonical,
                        "created": False,
                        "sourceOnly": True,
                        "dependencySettlements": [],
                        "terminalControlVersions": [],
                    }
                if last_applied is not None and applied != last_applied:
                    raise PlaybackControlTransactionConflictError(
                        "Passive update cannot advance appliedControlVersion"
                    )
                if handoff_source_changes:
                    handoff_settlement = _settle_handoff_source_changed(
                        handoff_record
                    )
                reconciliation_result = _reconcile_terminal_control_gap(
                    record,
                    client_id,
                    device_session_id,
                    origin,
                    payload,
                    applied,
                    incoming_client_seq,
                    server_updated_at_ms,
                )
                if reconciliation_result is not None:
                    canonical, control_reconciliation = reconciliation_result
                else:
                    natural_terminal = _apply_passive_natural_terminal(
                        record,
                        client_id,
                        payload,
                        applied,
                    )
                    canonical = _strict_playback_update_canonical(
                        record,
                        client_id,
                        device_session_id,
                        origin,
                        payload,
                        applied,
                        incoming_client_seq,
                        server_updated_at_ms,
                    )

            elif origin == "remoteCommand":
                command_version = payload["commandControlVersion"]
                applied = payload["appliedControlVersion"]
                if command_version > record.control_version:
                    raise ValueError(
                        "commandControlVersion exceeds canonical controlVersion"
                    )
                transaction = EmoPlaybackControlTransaction.get_or_none(
                    (
                        EmoPlaybackControlTransaction.playback_context_id
                        == playback_context_id
                    )
                    & (EmoPlaybackControlTransaction.epoch == record.epoch)
                    & (
                        EmoPlaybackControlTransaction.command_control_version
                        == command_version
                    )
                )
                if transaction is None:
                    raise PlaybackControlTransactionConflictError(
                        "Remote control transaction not found"
                    )
                _require_terminal_time_after_eligibility(
                    transaction,
                    server_updated_at_ms,
                )
                if require_execution_eligible and (
                    transaction.execution_eligible_at_ms is None
                    or transaction.watchdog_deadline_at_ms is None
                ):
                    raise PlaybackControlTransactionConflictError(
                        "Remote control transaction is not execution eligible"
                    )
                if last_applied is not None and applied < last_applied:
                    expected_status = (
                        "committed"
                        if payload["executionStatus"] == "committed"
                        else "failed"
                    )
                    if (
                        transaction.status != expected_status
                        or transaction.error_code != payload.get("errorCode")
                    ):
                        raise PlaybackControlTransactionConflictError(
                            "Stale remote terminal conflicts with persisted result"
                        )
                    return {
                        "playbackContext": current,
                        "deviceState": _device_playback_state_payload(existing),
                        "canonicalUpdate": _settle_stale_playback_correction(
                            record,
                            existing,
                            existing_json,
                            connection_nonce,
                            request_fingerprint,
                            incoming_client_seq,
                        ),
                        "created": False,
                        "sourceOnly": True,
                        "dependencySettlements": [],
                        "terminalControlVersions": [],
                    }
                if (
                    transaction.authority_client_id != client_id
                    or transaction.authority_device_session_id
                    != device_session_id
                    or transaction.routed_connection_nonce != connection_nonce
                ):
                    raise PlaybackControlTransactionConflictError(
                        "Remote control transaction authority changed"
                    )
                lower_pending = EmoPlaybackControlTransaction.get_or_none(
                    (
                        EmoPlaybackControlTransaction.playback_context_id
                        == playback_context_id
                    )
                    & (EmoPlaybackControlTransaction.epoch == record.epoch)
                    & (EmoPlaybackControlTransaction.status == "pending")
                    & (
                        EmoPlaybackControlTransaction.command_control_version
                        < command_version
                    )
                )
                if lower_pending is not None:
                    raise PlaybackControlTransactionConflictError(
                        "Lower control transaction is still pending"
                    )
                if (
                    payload["executionStatus"] == "failed"
                    and last_applied is not None
                    and applied != last_applied
                ):
                    raise PlaybackControlTransactionConflictError(
                        "Failed control feedback changed appliedControlVersion"
                    )
                terminal_status = (
                    "committed"
                    if payload["executionStatus"] == "committed"
                    else "failed"
                )
                terminal_error = payload.get("errorCode")
                terminal_error_message = (
                    payload.get("errorMessage")
                    if terminal_status == "failed"
                    else None
                )
                terminal_identity = {
                    "status": terminal_status,
                    "errorCode": terminal_error,
                    "errorMessage": terminal_error_message,
                    "appliedControlVersion": applied,
                    "state": payload["state"],
                    "trackId": payload.get("trackId"),
                    "positionMs": payload["positionMs"],
                }
                transaction, terminal_changed = (
                    _settle_playback_control_transaction_record(
                        transaction,
                        terminal_status,
                        server_updated_at_ms,
                        error_code=terminal_error,
                        applied_control_version=applied,
                        error_message=terminal_error_message,
                        terminal_identity=terminal_identity,
                    )
                )
                terminal_control_versions.append(command_version)

                if terminal_changed:
                    (
                        eligible_dependencies,
                        settled_dependencies,
                    ) = _resolve_playback_control_dependency_outcome(
                        transaction,
                        server_updated_at_ms,
                        allow_legacy_track_change_fallback=True,
                    )
                    eligible_dependency_records.extend(eligible_dependencies)
                    dependency_records.extend(settled_dependencies)
                    terminal_control_versions.extend(
                        dependency["commandControlVersion"]
                        for dependency in settled_dependencies
                    )

                reconciliation_result = None
                if terminal_changed and terminal_status == "failed":
                    reconciliation_result = _reconcile_terminal_control_gap(
                        record,
                        client_id,
                        device_session_id,
                        origin,
                        payload,
                        applied,
                        incoming_client_seq,
                        server_updated_at_ms,
                        trigger_command_control_version=command_version,
                    )
                if reconciliation_result is not None:
                    canonical, control_reconciliation = reconciliation_result
                else:
                    canonical = _strict_playback_update_canonical(
                        record,
                        client_id,
                        device_session_id,
                        origin,
                        payload,
                        applied,
                        incoming_client_seq,
                        server_updated_at_ms,
                        command_control_version=command_version,
                    )

            else:
                if payload["epoch"] != record.epoch:
                    raise PlaybackControlTransactionConflictError(
                        "Local intent Context epoch changed"
                    )
                if payload["observedControlVersion"] > record.control_version:
                    raise ValueError(
                        "observedControlVersion exceeds canonical controlVersion"
                    )
                queue = json.loads(record.queue_json)
                queue_index = payload["queueIndex"]
                if (
                    queue_index >= len(queue)
                    or queue[queue_index] != payload["trackId"]
                ):
                    raise PlaybackControlTransactionConflictError(
                        "Local intent queue item does not match canonical queue"
                    )
                intent_payload = dict(payload)
                intent_payload.pop("clientSeq", None)
                intent_fingerprint = _json_fingerprint(intent_payload)
                existing_intent = EmoPlaybackLocalIntent.get_or_none(
                    (
                        EmoPlaybackLocalIntent.playback_context_id
                        == playback_context_id
                    )
                    & (EmoPlaybackLocalIntent.epoch == record.epoch)
                    & (EmoPlaybackLocalIntent.intent_id == payload["intentId"])
                )
                if existing_intent is not None:
                    if (
                        existing_intent.authority_client_id != client_id
                        or existing_intent.authority_device_session_id
                        != device_session_id
                        or existing_intent.request_fingerprint
                        != intent_fingerprint
                    ):
                        raise PlaybackLocalIntentConflictError(
                            "Local intent content or binding conflict"
                        )
                    return {
                        "playbackContext": current,
                        "deviceState": (
                            _device_playback_state_payload(existing)
                            if existing is not None
                            else None
                        ),
                        "canonicalUpdate": _load_json_object(
                            existing_intent.canonical_update_json
                        ),
                        "created": False,
                        "sourceOnly": True,
                        "dependencySettlements": [],
                        "terminalControlVersions": [],
                    }

                if handoff_source_changes:
                    handoff_settlement = _settle_handoff_source_changed(
                        handoff_record
                    )
                superseded_through = record.control_version
                record.control_version += 1
                record.version += 1
                if record.current_index != queue_index:
                    record.queue_revision += 1
                record.current_index = queue_index
                record.track_id = payload["trackId"]
                record.state = payload["state"]
                record.position_ms = payload["positionMs"]
                record.updated_at = now()
                record.save()
                current = _playback_context_payload(record)
                applied = record.control_version

                pending_query = EmoPlaybackControlTransaction.select().where(
                    (
                        EmoPlaybackControlTransaction.playback_context_id
                        == playback_context_id
                    )
                    & (EmoPlaybackControlTransaction.epoch == record.epoch)
                    & (EmoPlaybackControlTransaction.status == "pending")
                    & (
                        EmoPlaybackControlTransaction.command_control_version
                        <= superseded_through
                    )
                )
                for pending in pending_query:
                    _require_terminal_time_after_eligibility(
                        pending,
                        server_updated_at_ms,
                    )
                    pending.status = "superseded"
                    pending.applied_control_version = applied
                    pending.terminal_fingerprint = _json_fingerprint(
                        {
                            "status": "superseded",
                            "errorMessage": None,
                            "appliedControlVersion": applied,
                        }
                    )
                    pending.terminal_at_ms = server_updated_at_ms
                    pending.updated_at = now()
                    pending.save()
                    terminal_control_versions.append(
                        pending.command_control_version
                    )

                canonical = _strict_playback_update_canonical(
                    record,
                    client_id,
                    device_session_id,
                    origin,
                    payload,
                    applied,
                    incoming_client_seq,
                    server_updated_at_ms,
                    superseded_through_control_version=superseded_through,
                )
                EmoPlaybackLocalIntent.create(
                    playback_context_id=playback_context_id,
                    user_name=user_name,
                    epoch=record.epoch,
                    intent_id=payload["intentId"],
                    authority_client_id=client_id,
                    authority_device_session_id=device_session_id,
                    request_fingerprint=intent_fingerprint,
                    canonical_update_json=_canonical_json(canonical),
                    control_version=record.control_version,
                    superseded_through_control_version=superseded_through,
                )

            previous_device_state = (
                _device_playback_state_payload(existing)
                if existing is not None
                else None
            )
            saved_device = _save_strict_device_state_record(
                record,
                existing,
                client_id,
                device_session_id,
                user_name,
                connection_nonce,
                request_fingerprint,
                canonical,
            )
            result = {
                "playbackContext": _playback_context_payload(record),
                "deviceState": _device_playback_state_payload(saved_device),
                "canonicalUpdate": canonical,
                "created": True,
                "sourceOnly": False,
                "executionEligibleTransactions": eligible_dependency_records,
                "dependencySettlements": dependency_records,
                "terminalControlVersions": terminal_control_versions,
            }
            if control_reconciliation is not None:
                result["controlReconciliation"] = control_reconciliation
            if handoff_settlement is not None:
                result["handoffSettlement"] = handoff_settlement
            if natural_terminal:
                result["naturalTerminal"] = True
            if post_mutation_hook is not None:
                result["_broadcastMutation"] = post_mutation_hook(
                    record,
                    result,
                    previous_context,
                    previous_device_state,
                )
            return result
    finally:
        close_connection()


def serializePlaybackPrepareTransaction(record):
    if record is None:
        return None
    payload = {
        "playbackContextId": record.playback_context_id,
        "userName": record.user_name,
        "epoch": record.epoch,
        "intentId": record.intent_id,
        "requestingClientId": record.requesting_client_id,
        "authorityClientId": record.authority_client_id,
        "authorityDeviceSessionId": record.authority_device_session_id,
        "routedConnectionNonce": record.routed_connection_nonce,
        "routedConnectionEpoch": record.routed_connection_epoch,
        "requestFingerprint": record.request_fingerprint,
        "controlVersion": record.control_version,
        "status": record.status,
        "deadlineAtMs": record.deadline_at_ms,
    }
    if record.initial_queue_json is not None:
        payload["initialQueue"] = _load_json_object(record.initial_queue_json)
    if record.canonical_result_json is not None:
        payload["canonicalResult"] = _load_json_object(record.canonical_result_json)
    optional = {
        "errorCode": record.error_code,
        "errorMessage": record.error_message,
        "terminalAtMs": record.terminal_at_ms,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    return payload


@_serialize_strict_playback_context_mutation
def createPlaybackPrepareTransaction(
    playback_context_id,
    user_name,
    epoch,
    intent_id,
    requesting_client_id,
    authority_client_id,
    authority_device_session_id,
    routed_connection_nonce,
    routed_connection_epoch,
    request_payload,
    control_version,
    deadline_at_ms,
    validate_context=False,
):
    request_fingerprint = _json_fingerprint(request_payload)
    initial_queue = request_payload.get("initialQueue")
    initial_queue_json = (
        _canonical_json(initial_queue) if initial_queue is not None else None
    )
    open_connection(reuse=True)
    try:
        with _strict_playback_context_transaction():
            existing = EmoPlaybackPrepareTransaction.get_or_none(
                (EmoPlaybackPrepareTransaction.playback_context_id == playback_context_id)
                & (EmoPlaybackPrepareTransaction.epoch == epoch)
                & (EmoPlaybackPrepareTransaction.intent_id == intent_id)
            )
            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    raise PlaybackPrepareTransactionConflictError(
                        "Prepare intent content conflict"
                    )
                return serializePlaybackPrepareTransaction(existing), False
            context_ready = False
            if validate_context:
                context_record = _getStrictPlaybackContextRecord(
                    playback_context_id,
                    user_name,
                )
                if context_record is None:
                    return None, False
                current = _playback_context_payload(context_record)
                if context_record.epoch != epoch:
                    raise PlaybackPrepareTransactionConflictError(
                        "Prepare Context epoch changed"
                    )
                if (
                    context_record.authority_client_id != authority_client_id
                    or context_record.authority_device_session_id
                    != authority_device_session_id
                ):
                    raise PlaybackPrepareTransactionConflictError(
                        "Prepare authority binding changed"
                    )
                active_context_ids = [
                    item.playback_context_id
                    for item in (
                        EmoPlaybackContext.select(
                            EmoPlaybackContext.playback_context_id
                        )
                        .where(
                            (EmoPlaybackContext.user_name == user_name)
                            & (EmoPlaybackContext.lifecycle == "active")
                            & (
                                EmoPlaybackContext.authority_client_id
                                == authority_client_id
                            )
                            & (
                                EmoPlaybackContext.authority_device_session_id
                                == authority_device_session_id
                            )
                        )
                        .order_by(EmoPlaybackContext.playback_context_id)
                        .limit(2)
                    )
                ]
                if active_context_ids != [playback_context_id]:
                    raise PlaybackContextAuthorityAmbiguousError(current)
                if context_record.control_version != control_version:
                    raise PlaybackContextStaleVersionError(
                        current,
                        "controlVersion",
                    )
                context_ready = bool(json.loads(context_record.queue_json))
            active = EmoPlaybackPrepareTransaction.get_or_none(
                (EmoPlaybackPrepareTransaction.playback_context_id == playback_context_id)
                & (EmoPlaybackPrepareTransaction.epoch == epoch)
                & (EmoPlaybackPrepareTransaction.status == "preparing")
            )
            if active is not None:
                raise PlaybackPrepareAlreadyActiveError(
                    "Another prepare transaction is active"
                )
            canonical_result = None
            status = "preparing"
            terminal_at_ms = None
            if context_ready:
                status = "ready"
                terminal_at_ms = max(0, deadline_at_ms - 10000)
                canonical_result = {
                    "playbackContextId": playback_context_id,
                    "intentId": intent_id,
                    "ready": True,
                    "controlVersion": control_version,
                }
            record = EmoPlaybackPrepareTransaction.create(
                playback_context_id=playback_context_id,
                user_name=user_name,
                epoch=epoch,
                intent_id=intent_id,
                requesting_client_id=requesting_client_id,
                authority_client_id=authority_client_id,
                authority_device_session_id=authority_device_session_id,
                routed_connection_nonce=routed_connection_nonce,
                routed_connection_epoch=routed_connection_epoch,
                request_fingerprint=request_fingerprint,
                initial_queue_json=initial_queue_json,
                control_version=control_version,
                status=status,
                deadline_at_ms=deadline_at_ms,
                canonical_result_json=(
                    _canonical_json(canonical_result)
                    if canonical_result is not None
                    else None
                ),
                terminal_at_ms=terminal_at_ms,
            )
            return serializePlaybackPrepareTransaction(record), True
    finally:
        close_connection()


def getPlaybackPrepareTransaction(playback_context_id, epoch, intent_id):
    open_connection(reuse=True)
    try:
        record = EmoPlaybackPrepareTransaction.get_or_none(
            (EmoPlaybackPrepareTransaction.playback_context_id == playback_context_id)
            & (EmoPlaybackPrepareTransaction.epoch == epoch)
            & (EmoPlaybackPrepareTransaction.intent_id == intent_id)
        )
        return serializePlaybackPrepareTransaction(record)
    finally:
        close_connection()


def listActivePlaybackPrepareTransactions(playback_context_id, epoch=None):
    open_connection(reuse=True)
    try:
        expression = (
            EmoPlaybackPrepareTransaction.playback_context_id
            == playback_context_id
        ) & (EmoPlaybackPrepareTransaction.status == "preparing")
        if epoch is not None:
            expression &= EmoPlaybackPrepareTransaction.epoch == epoch
        query = (
            EmoPlaybackPrepareTransaction.select()
            .where(expression)
            .order_by(
                EmoPlaybackPrepareTransaction.epoch,
                EmoPlaybackPrepareTransaction.intent_id,
            )
        )
        return [serializePlaybackPrepareTransaction(record) for record in query]
    finally:
        close_connection()


def listExpiredPlaybackPrepareTransactions(deadline_at_ms):
    open_connection(reuse=True)
    try:
        query = (
            EmoPlaybackPrepareTransaction.select()
            .where(
                (EmoPlaybackPrepareTransaction.status == "preparing")
                & (EmoPlaybackPrepareTransaction.deadline_at_ms <= deadline_at_ms)
            )
            .order_by(
                EmoPlaybackPrepareTransaction.deadline_at_ms,
                EmoPlaybackPrepareTransaction.playback_context_id,
                EmoPlaybackPrepareTransaction.epoch,
                EmoPlaybackPrepareTransaction.intent_id,
            )
        )
        return [serializePlaybackPrepareTransaction(record) for record in query]
    finally:
        close_connection()


@_serialize_strict_playback_context_mutation
def settlePlaybackPrepareTransaction(
    playback_context_id,
    epoch,
    intent_id,
    status,
    canonical_result,
    terminal_at_ms,
    error_code=None,
    error_message=None,
):
    if status not in {"ready", "failed"}:
        raise ValueError("Invalid prepare terminal status")
    canonical_result_json = _canonical_json(canonical_result)
    open_connection(reuse=True)
    try:
        with _strict_playback_context_transaction():
            record = EmoPlaybackPrepareTransaction.get_or_none(
                (EmoPlaybackPrepareTransaction.playback_context_id == playback_context_id)
                & (EmoPlaybackPrepareTransaction.epoch == epoch)
                & (EmoPlaybackPrepareTransaction.intent_id == intent_id)
            )
            if record is None:
                return None, False
            terminal_identity = (
                status,
                canonical_result_json,
                error_code,
                error_message,
            )
            if record.status != "preparing":
                existing_identity = (
                    record.status,
                    record.canonical_result_json,
                    record.error_code,
                    record.error_message,
                )
                if existing_identity != terminal_identity:
                    raise PlaybackPrepareTransactionConflictError(
                        "Prepare terminal conflict"
                    )
                return serializePlaybackPrepareTransaction(record), False
            updated = (
                EmoPlaybackPrepareTransaction.update(
                    status=status,
                    error_code=error_code,
                    error_message=error_message,
                    canonical_result_json=canonical_result_json,
                    terminal_at_ms=terminal_at_ms,
                    updated_at=now(),
                )
                .where(
                    (EmoPlaybackPrepareTransaction.id == record.id)
                    & (EmoPlaybackPrepareTransaction.status == "preparing")
                )
                .execute()
            )
            if updated != 1:
                raise PlaybackPrepareTransactionConflictError(
                    "Prepare transaction changed concurrently"
                )
            record = EmoPlaybackPrepareTransaction.get_by_id(record.id)
            return serializePlaybackPrepareTransaction(record), True
    finally:
        close_connection()


def serializePlaybackLocalIntent(record):
    if record is None:
        return None
    return {
        "playbackContextId": record.playback_context_id,
        "userName": record.user_name,
        "epoch": record.epoch,
        "intentId": record.intent_id,
        "authorityClientId": record.authority_client_id,
        "authorityDeviceSessionId": record.authority_device_session_id,
        "requestFingerprint": record.request_fingerprint,
        "canonicalUpdate": _load_json_object(record.canonical_update_json),
        "controlVersion": record.control_version,
        "supersededThroughControlVersion": (
            record.superseded_through_control_version
        ),
    }


@_serialize_strict_playback_context_mutation
def savePlaybackLocalIntent(
    playback_context_id,
    user_name,
    epoch,
    intent_id,
    authority_client_id,
    authority_device_session_id,
    request_payload,
    canonical_update,
    control_version,
    superseded_through_control_version,
):
    request_fingerprint = _json_fingerprint(request_payload)
    canonical_update_json = _canonical_json(canonical_update)
    open_connection(reuse=True)
    try:
        with _strict_playback_context_transaction():
            existing = EmoPlaybackLocalIntent.get_or_none(
                (EmoPlaybackLocalIntent.playback_context_id == playback_context_id)
                & (EmoPlaybackLocalIntent.epoch == epoch)
                & (EmoPlaybackLocalIntent.intent_id == intent_id)
            )
            if existing is not None:
                identity = (
                    existing.user_name,
                    existing.authority_client_id,
                    existing.authority_device_session_id,
                    existing.request_fingerprint,
                )
                expected = (
                    user_name,
                    authority_client_id,
                    authority_device_session_id,
                    request_fingerprint,
                )
                if identity != expected:
                    raise PlaybackLocalIntentConflictError(
                        "Local intent content or binding conflict"
                    )
                return serializePlaybackLocalIntent(existing), False
            record = EmoPlaybackLocalIntent.create(
                playback_context_id=playback_context_id,
                user_name=user_name,
                epoch=epoch,
                intent_id=intent_id,
                authority_client_id=authority_client_id,
                authority_device_session_id=authority_device_session_id,
                request_fingerprint=request_fingerprint,
                canonical_update_json=canonical_update_json,
                control_version=control_version,
                superseded_through_control_version=(
                    superseded_through_control_version
                ),
            )
            return serializePlaybackLocalIntent(record), True
    finally:
        close_connection()


def serializePlaybackContextV2(playback_context):
    if playback_context is None:
        return None
    payload = {
        "playbackContextId": playback_context.get("playbackContextId"),
        "authorityClientId": playback_context.get("authorityClientId"),
        "authorityDeviceSessionId": playback_context.get(
            "authorityDeviceSessionId"
        ),
        "queueSongIds": list(playback_context.get("queueSongIds") or []),
        "state": playback_context.get("state") or "idle",
        "positionMs": playback_context.get("positionMs", 0),
        "queueRevision": playback_context.get("queueRevision", 1),
        "controlVersion": playback_context.get("controlVersion", 1),
        "version": playback_context.get("version", 1),
        "epoch": playback_context.get("epoch", 1),
    }
    queue_song_ids = payload["queueSongIds"]
    if queue_song_ids:
        payload["currentIndex"] = playback_context.get("currentIndex", 0)
    for field_name in ("trackId", "timelineId", "serverUpdatedAtMs"):
        value = playback_context.get(field_name)
        if value is not None and (field_name != "trackId" or queue_song_ids):
            payload[field_name] = value
    return payload


def serializePlaybackContextBindingV2(playback_context):
    if playback_context is None:
        return None
    return {
        "playbackContextId": playback_context.get("playbackContextId"),
        "authorityClientId": playback_context.get("authorityClientId"),
        "authorityDeviceSessionId": playback_context.get(
            "authorityDeviceSessionId"
        ),
    }


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
        "positionSampledAtServerMs": device_state.get(
            "positionSampledAtServerMs",
            device_state.get("serverUpdatedAtMs"),
        ),
        "playbackRate": device_state.get("playbackRate", 1.0),
        "appliedControlVersion": device_state.get("appliedControlVersion"),
        "clientSeq": device_state.get("clientSeq"),
        "serverUpdatedAtMs": device_state.get("serverUpdatedAtMs"),
    }
    if any(value is None for value in payload.values()):
        return None
    if payload["appliedControlVersion"] < 1 or payload["clientSeq"] < 1:
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


def getPlaybackContextStateForUser(playback_context_id, user_name):
    _require_non_empty_string(playback_context_id, "playbackContextId", 128)
    _require_non_empty_string(user_name, "userName", 64)
    open_connection(reuse=True)
    try:
        record = EmoPlaybackContext.get_or_none(
            (EmoPlaybackContext.playback_context_id == playback_context_id)
            & (EmoPlaybackContext.user_name == user_name)
        )
        return None if record is None else _playback_context_payload(record)
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


def _resolveStrictPlaybackContextCreate(
    record,
    user_name,
    creation_fingerprint,
) -> PlaybackContextCreateResult:
    playback_context = _playback_context_payload(record)
    if record.user_name != user_name:
        raise PermissionError("Playback context belongs to another user")
    if record.lifecycle == "closed":
        raise PlaybackContextClosedError(playback_context)
    if record.creation_fingerprint != creation_fingerprint:
        raise PlaybackContextIntentConflictError(playback_context)
    return PlaybackContextCreateResult(playback_context, False, ())


def _active_context_records_for_stable_client(user_name, client_id):
    return list(
        EmoPlaybackContext.select()
        .where(
            (EmoPlaybackContext.user_name == user_name)
            & (EmoPlaybackContext.lifecycle == "active")
            & (EmoPlaybackContext.authority_client_id == client_id)
        )
        .order_by(EmoPlaybackContext.playback_context_id)
        .limit(3)
    )


def _active_context_records_for_authority_pair(
    user_name,
    client_id,
    device_session_id,
):
    return list(
        EmoPlaybackContext.select()
        .where(
            (EmoPlaybackContext.user_name == user_name)
            & (EmoPlaybackContext.lifecycle == "active")
            & (EmoPlaybackContext.authority_client_id == client_id)
            & (
                EmoPlaybackContext.authority_device_session_id
                == device_session_id
            )
        )
        .order_by(EmoPlaybackContext.playback_context_id)
        .limit(3)
    )


def _active_context_records_for_ensure(
    user_name,
    client_id,
    device_session_id,
):
    exact_pair_candidates = _active_context_records_for_authority_pair(
        user_name,
        client_id,
        device_session_id,
    )
    if len(exact_pair_candidates) > 1:
        raise PlaybackContextEnsureConflictError(
            _playback_context_payload(exact_pair_candidates[0])
        )
    if exact_pair_candidates:
        return exact_pair_candidates

    candidates = _active_context_records_for_stable_client(
        user_name,
        client_id,
    )
    if len(candidates) > 1:
        raise PlaybackContextEnsureConflictError(
            _playback_context_payload(candidates[0])
        )
    return candidates


def _initialize_idle_context_from_ensure(
    record,
    queue_song_ids,
    current_index,
    position_ms,
    state_name,
):
    existing_queue = json.loads(record.queue_json)
    if existing_queue or not queue_song_ids:
        return False
    record.queue_json = json.dumps(queue_song_ids, ensure_ascii=True)
    record.current_index = current_index
    record.track_id = queue_song_ids[current_index]
    record.state = state_name
    record.position_ms = position_ms
    return True


def _new_playback_context_id():
    return "playback:%s" % uuid4().hex


def ensureStrictPlaybackContextState(
    user_name,
    authority_client_id,
    authority_device_session_id,
    queue_song_ids,
    current_index,
    position_ms,
    state_name,
    allow_rebind=True,
):
    queue_song_ids = list(queue_song_ids or [])
    if queue_song_ids:
        if current_index is None or current_index < 0 or current_index >= len(queue_song_ids):
            raise ValueError("currentIndex is outside queueSongIds")
        if state_name not in {"playing", "paused", "stopped"}:
            raise ValueError("Queue-backed ensure state is invalid")
    else:
        if current_index is not None or position_ms != 0 or state_name != "idle":
            raise ValueError("Idle ensure shape is invalid")
        current_index = 0

    target_pair = _strict_authority_pair_key(
        user_name,
        authority_client_id,
        authority_device_session_id,
    )
    generated_context_id = _new_playback_context_id()
    with _strict_stable_client_lock(user_name, authority_client_id):
        open_connection(reuse=True)
        try:
            while True:
                candidates = _active_context_records_for_ensure(
                    user_name,
                    authority_client_id,
                    authority_device_session_id,
                )
                selected_context_id = (
                    candidates[0].playback_context_id
                    if candidates
                    else generated_context_id
                )
                with _strict_playback_context_lock(selected_context_id):
                    candidates = _active_context_records_for_ensure(
                        user_name,
                        authority_client_id,
                        authority_device_session_id,
                    )
                    if candidates and candidates[0].playback_context_id != selected_context_id:
                        continue
                    if not candidates and selected_context_id != generated_context_id:
                        continue
                    authority_pairs = [target_pair]
                    if candidates:
                        authority_pairs.append(_record_authority_pair(candidates[0]))
                    with _strict_authority_pair_lock(authority_pairs):
                        with _strict_playback_context_transaction():
                            candidates = _active_context_records_for_ensure(
                                user_name,
                                authority_client_id,
                                authority_device_session_id,
                            )
                            if candidates and candidates[0].playback_context_id != selected_context_id:
                                continue
                            pair_fence = _broadcast_fence_for_pair(
                                user_name,
                                authority_client_id,
                                authority_device_session_id,
                            )
                            if not candidates:
                                requireFollowSafetyLeaseResourceAvailable(
                                    user_name=user_name,
                                    client_id=authority_client_id,
                                    device_session_id=(
                                        authority_device_session_id
                                    ),
                                )
                                requirePlaybackHandoffResourceAvailable(
                                    user_name=user_name,
                                    client_id=authority_client_id,
                                    device_session_id=(
                                        authority_device_session_id
                                    ),
                                )
                                if pair_fence is not None:
                                    _raise_broadcast_fence(
                                        pair_fence,
                                        pair_fence.playback_context_id
                                        or selected_context_id,
                                        restore_in_progress=True,
                                    )
                                existing = EmoPlaybackContext.get_or_none(
                                    EmoPlaybackContext.playback_context_id
                                    == generated_context_id
                                )
                                if existing is not None:
                                    playback_context = _playback_context_payload(
                                        existing
                                    )
                                    if (
                                        existing.user_name == user_name
                                        and existing.lifecycle == "closed"
                                    ):
                                        raise PlaybackContextClosedError(
                                            playback_context
                                        )
                                    raise PlaybackContextEnsureConflictError(
                                        playback_context
                                    )
                                record = EmoPlaybackContext.create(
                                    playback_context_id=generated_context_id,
                                    user_name=user_name,
                                    authority_client_id=authority_client_id,
                                    authority_device_session_id=(
                                        authority_device_session_id
                                    ),
                                    origin_client_id=authority_client_id,
                                    timeline_id="timeline:%s" % uuid4().hex,
                                    lifecycle="active",
                                    queue_json=json.dumps(
                                        queue_song_ids,
                                        ensure_ascii=True,
                                    ),
                                    current_index=current_index,
                                    track_id=(
                                        queue_song_ids[current_index]
                                        if queue_song_ids
                                        else None
                                    ),
                                    state=state_name,
                                    position_ms=position_ms,
                                    queue_revision=1,
                                    control_version=1,
                                    version=1,
                                    epoch=1,
                                    playback_json=json.dumps({}, ensure_ascii=True),
                                )
                                return PlaybackContextEnsureResult(
                                    _playback_context_payload(record),
                                    True,
                                    (target_pair,),
                                )

                            record = candidates[0]
                            old_pair = _record_authority_pair(record)
                            rebind = (
                                record.authority_device_session_id
                                != authority_device_session_id
                            )
                            would_initialize = bool(queue_song_ids) and not bool(
                                json.loads(record.queue_json)
                            )
                            if rebind or would_initialize:
                                requireFollowSafetyLeaseResourceAvailable(
                                    playback_context_id=(
                                        record.playback_context_id
                                    ),
                                    user_name=user_name,
                                    client_id=authority_client_id,
                                    device_session_id=(
                                        authority_device_session_id
                                    ),
                                )
                                requirePlaybackHandoffResourceAvailable(
                                    playback_context_id=(
                                        record.playback_context_id
                                    ),
                                    user_name=user_name,
                                    client_id=authority_client_id,
                                    device_session_id=(
                                        authority_device_session_id
                                    ),
                                )
                            context_fences = _broadcast_fences_for_context(
                                record.playback_context_id
                            )
                            ordinary_fence = next(
                                (
                                    fence
                                    for fence in context_fences
                                    if fence.role == "ordinary"
                                ),
                                None,
                            )
                            if ordinary_fence is not None:
                                _raise_broadcast_fence(
                                    ordinary_fence,
                                    record.playback_context_id,
                                    restore_in_progress=True,
                                )
                            source_fence = next(
                                (
                                    fence
                                    for fence in context_fences
                                    if fence.role == "source"
                                ),
                                None,
                            )
                            if source_fence is not None and (
                                rebind or would_initialize
                            ):
                                _raise_broadcast_fence(
                                    source_fence,
                                    record.playback_context_id,
                                )
                            if pair_fence is not None and not (
                                source_fence is not None
                                and pair_fence.broadcast_id
                                == source_fence.broadcast_id
                                and not rebind
                                and not would_initialize
                            ):
                                _raise_broadcast_fence(
                                    pair_fence,
                                    pair_fence.playback_context_id
                                    or record.playback_context_id,
                                    restore_in_progress=True,
                                )
                            if rebind and not allow_rebind:
                                raise PlaybackContextEnsureConflictError(
                                    _playback_context_payload(record)
                                )
                            initialized = _initialize_idle_context_from_ensure(
                                record,
                                queue_song_ids,
                                current_index,
                                position_ms,
                                state_name,
                            )
                            affected_pairs = ()
                            if rebind:
                                record.authority_device_session_id = (
                                    authority_device_session_id
                                )
                                record.epoch += 1
                                record.version += 1
                                record.control_version += 1
                                if initialized:
                                    record.queue_revision += 1
                                affected_pairs = (old_pair, target_pair)
                            elif initialized:
                                record.version += 1
                                record.queue_revision += 1
                                record.control_version += 1
                            if rebind or initialized:
                                record.origin_client_id = authority_client_id
                                record.updated_at = now()
                                record.save()
                            return PlaybackContextEnsureResult(
                                _playback_context_payload(record),
                                rebind or initialized,
                                affected_pairs,
                            )
        finally:
            close_connection()


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
) -> PlaybackContextCreateResult:
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
    authority_pair = _strict_authority_pair_key(
        user_name,
        authority_client_id,
        authority_device_session_id,
    )
    open_connection(reuse=True)
    try:
        with _strict_authority_pair_transaction((authority_pair,)):
            pair_fence = _broadcast_fence_for_pair(
                user_name,
                authority_client_id,
                authority_device_session_id,
            )
            if pair_fence is not None:
                _raise_broadcast_fence(
                    pair_fence,
                    pair_fence.playback_context_id or playback_context_id,
                )
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
            playback_context = _playback_context_payload(record)
            return PlaybackContextCreateResult(
                playback_context,
                True,
                (authority_pair,),
            )
    finally:
        close_connection()


@_serialize_strict_playback_context_close
def closeStrictPlaybackContextState(
    playback_context_id,
    user_name,
    expected_epoch=None,
    base_version=None,
    requesting_client_id=None,
    requesting_device_session_id=None,
    requester_is_controller=False,
    pre_close_validator=None,
    post_close_hook=None,
    close_action="playback.context.close",
    close_outcome=None,
) -> Optional[PlaybackContextCloseResult]:
    _require_non_empty_string(playback_context_id, "playbackContextId", 128)
    _require_non_empty_string(user_name, "userName", 64)
    safe_close = expected_epoch is not None or base_version is not None
    if safe_close:
        _require_integer(expected_epoch, "expectedEpoch", 1)
        _require_integer(base_version, "baseVersion", 1)
        _require_non_empty_string(requesting_client_id, "requestingClientId", 128)
        _require_non_empty_string(
            requesting_device_session_id,
            "requestingDeviceSessionId",
            128,
        )
        if type(requester_is_controller) is not bool:
            raise ValueError("requesterIsController must be a boolean")
        _require_non_empty_string(close_action, "closeAction", 64)
        if close_action != "playback.context.close":
            raise ValueError("closeAction is not a strict Context close action")
        if close_outcome is None:
            close_outcome = {"action": close_action}
        if close_outcome != {"action": close_action}:
            raise ValueError("closeOutcome must be the canonical close ACK payload")
        request_fingerprint = _playback_context_close_fingerprint(
            playback_context_id,
            expected_epoch,
            base_version,
            close_action,
        )
    else:
        if expected_epoch is not None or base_version is not None:
            raise ValueError("expectedEpoch and baseVersion must appear together")
        request_fingerprint = None

    open_connection(reuse=True)
    try:
        initial_record = EmoPlaybackContext.get_or_none(
            (EmoPlaybackContext.playback_context_id == playback_context_id)
            & (EmoPlaybackContext.user_name == user_name)
        )
        if initial_record is None:
            return None
        authority_pair = _record_authority_pair(initial_record)
        with _strict_authority_pair_transaction((authority_pair,)):
            record = EmoPlaybackContext.get_or_none(
                (EmoPlaybackContext.playback_context_id == playback_context_id)
                & (EmoPlaybackContext.user_name == user_name)
            )
            if record is None:
                return None

            if safe_close and not requester_is_controller and (
                record.authority_client_id != requesting_client_id
                or record.authority_device_session_id
                != requesting_device_session_id
            ):
                raise PermissionError(
                    "Only the exact playback authority pair or a controller can close context"
                )

            if record.lifecycle == "closed":
                closed_context = _closed_playback_context_payload(record)
                if not safe_close:
                    return PlaybackContextCloseResult(
                        closed_context,
                        False,
                        (),
                    )
                if not _has_complete_playback_context_close_tombstone(record):
                    raise PlaybackContextClosedError(closed_context)
                tombstone_matches = (
                    record.close_action == close_action
                    and record.close_request_fingerprint == request_fingerprint
                    and record.close_expected_epoch == expected_epoch
                    and record.close_base_version == base_version
                    and record.closed_from_epoch == expected_epoch
                    and record.closed_from_version == base_version
                )
                if not tombstone_matches:
                    raise PlaybackContextClosedError(closed_context)
                persisted_outcome = _load_json_object(
                    record.close_outcome_json,
                    required=True,
                )
                return PlaybackContextCloseResult(
                    closed_context,
                    False,
                    (),
                    close_outcome=persisted_outcome,
                    tombstone=serializePlaybackContextCloseTombstone(record),
                )

            if record.lifecycle != "active":
                raise PlaybackContextCloseInvariantError(
                    "Playback context has an invalid lifecycle"
                )
            if _has_playback_context_close_tombstone_data(record):
                raise PlaybackContextCloseInvariantError(
                    "Active playback context already contains close tombstone data"
                )

            current = _playback_context_payload(record)
            if safe_close:
                if pre_close_validator is not None:
                    pre_close_validator(dict(current))
                has_pending_control = (
                    EmoPlaybackControlTransaction.select()
                    .where(
                        (
                            EmoPlaybackControlTransaction.playback_context_id
                            == playback_context_id
                        )
                        & (EmoPlaybackControlTransaction.status == "pending")
                    )
                    .exists()
                )
                if has_pending_control:
                    raise PlaybackContextCloseConflictError(
                        current,
                        "Playback context has a pending control transaction",
                    )
                has_nonterminal_handoff = any(
                    _handoff_occupies_context(
                        handoff_record,
                        playback_context_id,
                    )
                    for handoff_record in _active_handoff_records(
                        user_name=user_name
                    )
                )
                if has_nonterminal_handoff:
                    raise PlaybackContextCloseConflictError(
                        current,
                        "Playback context has a nonterminal Handoff",
                    )
                if expected_epoch != record.epoch:
                    raise PlaybackContextStaleVersionError(current, "epoch")
                if base_version != record.version:
                    raise PlaybackContextStaleVersionError(current, "version")

            mutated = record.lifecycle != "closed"
            if mutated:
                closed_at = now()
                closed_from_epoch = record.epoch
                closed_from_version = record.version
                record.lifecycle = "closed"
                record.closed_at = closed_at
                record.version = max(1, record.version) + 1
                record.updated_at = closed_at
                fields = [
                    EmoPlaybackContext.lifecycle,
                    EmoPlaybackContext.closed_at,
                    EmoPlaybackContext.version,
                    EmoPlaybackContext.updated_at,
                ]
                if safe_close:
                    record.close_action = close_action
                    record.close_request_fingerprint = request_fingerprint
                    record.close_expected_epoch = expected_epoch
                    record.close_base_version = base_version
                    record.closed_from_epoch = closed_from_epoch
                    record.closed_from_version = closed_from_version
                    record.final_epoch = record.epoch
                    record.final_version = record.version
                    record.final_queue_revision = record.queue_revision
                    record.final_control_version = record.control_version
                    record.close_outcome_json = _canonical_json(close_outcome)
                    fields.extend(
                        (
                            EmoPlaybackContext.close_action,
                            EmoPlaybackContext.close_request_fingerprint,
                            EmoPlaybackContext.close_expected_epoch,
                            EmoPlaybackContext.close_base_version,
                            EmoPlaybackContext.closed_from_epoch,
                            EmoPlaybackContext.closed_from_version,
                            EmoPlaybackContext.final_epoch,
                            EmoPlaybackContext.final_version,
                            EmoPlaybackContext.final_queue_revision,
                            EmoPlaybackContext.final_control_version,
                            EmoPlaybackContext.close_outcome_json,
                        )
                    )
                record.save(
                    only=tuple(fields)
                )
                if post_close_hook is not None:
                    post_close_hook(dict(_playback_context_payload(record)))
                if not safe_close:
                    (
                        EmoPlaybackHandoff.update(
                            status="failed",
                            error_code="context_closed",
                            error_message="Playback context is closed",
                            updated_at=closed_at,
                        )
                        .where(
                            (
                                EmoPlaybackHandoff.playback_context_id
                                == playback_context_id
                            )
                            & EmoPlaybackHandoff.status.in_(
                                ("preparing", "ready", "committed", "committing")
                            )
                        )
                        .execute()
                    )
            playback_context = _playback_context_payload(record)
            return PlaybackContextCloseResult(
                playback_context,
                mutated,
                (authority_pair,) if mutated else (),
                close_outcome=close_outcome if safe_close else None,
                tombstone=(
                    serializePlaybackContextCloseTombstone(record)
                    if safe_close
                    else None
                ),
            )
    finally:
        close_connection()


def _getStrictPlaybackContextRecord(playback_context_id, user_name):
    record = EmoPlaybackContext.get_or_none(
        (EmoPlaybackContext.playback_context_id == playback_context_id)
        & (EmoPlaybackContext.user_name == user_name)
    )
    if record is None:
        return None
    if record.lifecycle == "closed":
        raise PlaybackContextClosedError(_playback_context_payload(record))
    return record


def _advance_queue_sync_authority_device_state(
    record: EmoPlaybackContext,
    authority_client_id: str,
    authority_device_session_id: str,
    connection_nonce: Optional[str],
    position_sampled_at_server_ms: int,
    updated_at: datetime,
    advance_applied_control_version: bool = True,
    refresh_same_feedback_scope: bool = True,
) -> Optional[EmoDevicePlaybackState]:
    existing = EmoDevicePlaybackState.get_or_none(
        (
            EmoDevicePlaybackState.playback_context_id
            == record.playback_context_id
        )
        & (
            EmoDevicePlaybackState.owner_client_id
            == authority_client_id
        )
    )
    same_authority_scope = bool(
        existing is not None
        and existing.context_epoch == record.epoch
        and existing.device_session_id == authority_device_session_id
    )
    playback_json = {}
    if same_authority_scope and existing.playback_json:
        loaded = json.loads(existing.playback_json)
        if isinstance(loaded, dict):
            playback_json = loaded

    same_feedback_scope = bool(
        same_authority_scope
        and isinstance(connection_nonce, str)
        and connection_nonce
        and playback_json.get("_connectionNonce") == connection_nonce
    )
    if not advance_applied_control_version and not same_authority_scope:
        return existing
    if (
        not advance_applied_control_version
        and not refresh_same_feedback_scope
        and same_feedback_scope
    ):
        return existing
    if not same_feedback_scope:
        playback_json = {}

    if same_feedback_scope and record.state != "idle":
        state_name = existing.state
        if state_name not in {"playing", "paused", "stopped"}:
            state_name = record.state
    else:
        state_name = record.state

    server_updated_at_ms = int(updated_at.timestamp() * 1000)
    applied_control_version = (
        record.control_version
        if advance_applied_control_version
        else existing.applied_control_version
    )
    playback_json.update(
        {
            "state": state_name,
            "positionMs": record.position_ms,
            "positionSampledAtServerMs": position_sampled_at_server_ms,
            "appliedControlVersion": applied_control_version,
            "serverUpdatedAtMs": server_updated_at_ms,
        }
    )
    if record.track_id is None:
        playback_json.pop("trackId", None)
    else:
        playback_json["trackId"] = record.track_id

    values = {
        "device_session_id": authority_device_session_id,
        "owner_client_id": authority_client_id,
        "user_name": record.user_name,
        "state": state_name,
        "track_id": record.track_id,
        "position_ms": record.position_ms,
        "volume": existing.volume if same_feedback_scope else None,
        "is_authority": 1,
        "mode": existing.mode if same_feedback_scope else "normal",
        "context_epoch": record.epoch,
        "applied_control_version": applied_control_version,
        "client_seq": existing.client_seq if same_feedback_scope else 0,
        "playback_json": json.dumps(playback_json, ensure_ascii=True),
        "updated_at": updated_at,
    }
    if existing is None:
        return EmoDevicePlaybackState.create(
            playback_context_id=record.playback_context_id,
            **values,
        )
    for field_name, value in values.items():
        setattr(existing, field_name, value)
    existing.save()
    return existing


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
    position_sampled_at_server_ms=None,
    connection_nonce=None,
    post_mutation_hook=None,
):
    queue_song_ids = list(queue_song_ids)
    open_connection(reuse=True)
    try:
        with _strict_playback_context_transaction():
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

            previous_queue = json.loads(record.queue_json)
            previous_index = record.current_index if previous_queue else None
            previous_track = record.track_id
            next_index = current_index if queue_song_ids else None
            next_track = (
                queue_song_ids[next_index]
                if queue_song_ids and next_index is not None
                else None
            )
            index_changed = previous_index != next_index
            boundary_changed = bool(previous_queue) != bool(queue_song_ids)
            position_changed = record.position_ms != position_ms
            control_changed = (
                index_changed
                or previous_track != next_track
                or boundary_changed
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
            if control_changed:
                has_pending_control = (
                    EmoPlaybackControlTransaction.select()
                    .where(
                        (
                            EmoPlaybackControlTransaction.playback_context_id
                            == playback_context_id
                        )
                        & (
                            EmoPlaybackControlTransaction.epoch
                            == record.epoch
                        )
                        & (
                            EmoPlaybackControlTransaction.status
                            == "pending"
                        )
                    )
                    .exists()
                )
                if has_pending_control:
                    raise PlaybackControlTransactionConflictError(
                        "Queue sync cannot advance appliedControlVersion "
                        "across a pending control"
                    )

            record.queue_json = json.dumps(queue_song_ids, ensure_ascii=True)
            record.current_index = next_index or 0
            record.track_id = next_track
            record.position_ms = position_ms
            updated_at = now()
            if position_sampled_at_server_ms is None:
                position_sampled_at_server_ms = int(
                    updated_at.timestamp() * 1000
                )
            playback_json = (
                json.loads(record.playback_json)
                if record.playback_json
                else {}
            )
            playback_json["positionSampledAtServerMs"] = (
                position_sampled_at_server_ms
            )
            record.playback_json = json.dumps(
                playback_json,
                ensure_ascii=True,
            )
            if not queue_song_ids:
                record.state = "idle"
                record.position_ms = 0
            elif not previous_queue or record.state == "idle":
                record.state = "paused"
            record.version += 1
            record.queue_revision += 1
            if control_changed:
                record.control_version += 1
            record.updated_at = updated_at
            record.save()
            if (
                control_changed
                or position_changed
                or (isinstance(connection_nonce, str) and connection_nonce)
            ):
                _advance_queue_sync_authority_device_state(
                    record,
                    authority_client_id,
                    authority_device_session_id,
                    connection_nonce,
                    position_sampled_at_server_ms,
                    updated_at,
                    advance_applied_control_version=control_changed,
                    refresh_same_feedback_scope=(
                        control_changed or position_changed
                    ),
                )
            result = _playback_context_payload(record)
            if post_mutation_hook is not None:
                result["_broadcastMutation"] = post_mutation_hook(
                    record,
                    result,
                    current,
                )
            return result
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
    requesting_client_id=None,
    authority_client_id=None,
    authority_device_session_id=None,
    routed_connection_nonce=None,
    routed_connection_epoch=1,
    accepted_at_ms=None,
    execution_timeout_ms=None,
    accepted_target_extra=None,
    post_mutation_hook=None,
    requesting_device_session_id=None,
    requesting_connection_nonce=None,
    requesting_connection_epoch=None,
    effective_at_server_ms=None,
    pre_mutation_validator=None,
    deterministic_dependency_admission=False,
):
    if type(deterministic_dependency_admission) is not bool:
        raise ValueError("deterministicDependencyAdmission must be a boolean")
    if (
        deterministic_dependency_admission
        and action not in _ORDINARY_CONTROL_ACTIONS
    ):
        raise ValueError(
            "Deterministic dependency admission requires an ordinary control action"
        )
    if requesting_client_id is None and any(
        value is not None
        for value in (
            requesting_device_session_id,
            requesting_connection_nonce,
            requesting_connection_epoch,
            effective_at_server_ms,
        )
    ):
        raise ValueError(
            "Requester transaction fields require requestingClientId"
        )
    open_connection(reuse=True)
    try:
        initial_record = _getStrictPlaybackContextRecord(
            playback_context_id,
            user_name,
        )
        if initial_record is None:
            return None
        authority_pair = _record_authority_pair(initial_record)
        with _strict_authority_pair_transaction((authority_pair,)):
            record = _getStrictPlaybackContextRecord(playback_context_id, user_name)
            if record is None:
                return None
            current = _playback_context_payload(record)
            active_context_ids = [
                active_record.playback_context_id
                for active_record in (
                    EmoPlaybackContext.select(
                        EmoPlaybackContext.playback_context_id
                    )
                    .where(
                        (EmoPlaybackContext.user_name == user_name)
                        & (EmoPlaybackContext.lifecycle == "active")
                        & (
                            EmoPlaybackContext.authority_client_id
                            == record.authority_client_id
                        )
                        & (
                            EmoPlaybackContext.authority_device_session_id
                            == record.authority_device_session_id
                        )
                    )
                    .order_by(
                        EmoPlaybackContext.playback_context_id.asc()
                    )
                    .limit(2)
                )
            ]
            if active_context_ids != [playback_context_id]:
                raise PlaybackContextAuthorityAmbiguousError(current)
            if not json.loads(record.queue_json):
                raise PlaybackContextQueueRequiredError(current)
            if base_control_version != record.control_version:
                raise PlaybackContextStaleVersionError(current, "controlVersion")
            if (
                action == "queue.playItem"
                and base_queue_revision != record.queue_revision
            ):
                raise PlaybackContextStaleVersionError(current, "queueRevision")

            queue_song_ids = json.loads(record.queue_json)
            previous_current_index = record.current_index
            terminal_next = False
            if action == "queue.playItem":
                if current_index is None or current_index >= len(queue_song_ids):
                    raise ValueError("queue.playItem queueIndex is out of bounds")
            elif action == "player.next":
                current_index = record.current_index + 1
                if current_index >= len(queue_song_ids):
                    current_index = record.current_index
                    terminal_next = True
            elif action == "player.prev":
                current_index = max(0, record.current_index - 1)
            if requesting_client_id is not None:
                _validate_control_transaction_inputs(
                    user_name,
                    record.epoch,
                    record.control_version + 1,
                    requesting_client_id,
                    authority_client_id,
                    authority_device_session_id,
                    routed_connection_nonce,
                    routed_connection_epoch,
                    {},
                    accepted_at_ms,
                    execution_timeout_ms,
                    requesting_device_session_id,
                    requesting_connection_nonce,
                    requesting_connection_epoch,
                    effective_at_server_ms,
                )
            if pre_mutation_validator is not None:
                pre_mutation_validator(dict(current))
            if current_index is not None:
                record.current_index = current_index
                record.track_id = queue_song_ids[current_index]
            if terminal_next:
                record.state = "stopped"
            elif action in {
                "player.play",
                "queue.playItem",
                "player.next",
                "player.prev",
            }:
                record.state = "playing"
            elif action == "player.pause":
                record.state = "paused"
            if position_ms is not None:
                record.position_ms = position_ms
            record.origin_client_id = updated_by_client_id
            record.version += 1
            record.control_version += 1
            if action == "queue.playItem" or (
                action in {"player.next", "player.prev"}
                and record.current_index != previous_current_index
            ):
                record.queue_revision += 1
            record.updated_at = now()
            record.save()
            result = _playback_context_payload(record)
            if requesting_client_id is not None:
                accepted_target = {
                    "action": action,
                    "state": record.state,
                    "positionMs": record.position_ms,
                }
                if record.track_id is not None:
                    accepted_target["trackId"] = record.track_id
                if action in {
                    "queue.playItem",
                    "player.next",
                    "player.prev",
                }:
                    accepted_target["queueIndex"] = record.current_index
                    accepted_target["queueRevision"] = record.queue_revision
                if action == "queue.playItem":
                    accepted_target["queueSongIds"] = list(queue_song_ids)
                if accepted_target_extra:
                    accepted_target.update(dict(accepted_target_extra))
                transaction_record, _created = _create_playback_control_transaction_record(
                    playback_context_id=playback_context_id,
                    user_name=user_name,
                    epoch=record.epoch,
                    command_control_version=record.control_version,
                    requesting_client_id=requesting_client_id,
                    authority_client_id=authority_client_id,
                    authority_device_session_id=authority_device_session_id,
                    routed_connection_nonce=routed_connection_nonce,
                    routed_connection_epoch=routed_connection_epoch,
                    action=action,
                    accepted_target=accepted_target,
                    accepted_at_ms=accepted_at_ms,
                    execution_timeout_ms=execution_timeout_ms,
                    requesting_device_session_id=requesting_device_session_id,
                    requesting_connection_nonce=requesting_connection_nonce,
                    requesting_connection_epoch=requesting_connection_epoch,
                    effective_at_server_ms=effective_at_server_ms,
                    deterministic_dependency_admission=(
                        deterministic_dependency_admission
                    ),
                )
                result["_controlTransaction"] = (
                    serializePlaybackControlTransaction(transaction_record)
                )
            if post_mutation_hook is not None:
                result["_broadcastMutation"] = post_mutation_hook(
                    record,
                    result,
                    current,
                )
            return result
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


def _active_handoff_target_context_records(
    user_name: str,
    target_client_id: str,
    target_device_session_id: str,
) -> List[EmoPlaybackContext]:
    return list(
        EmoPlaybackContext.select()
        .where(
            (EmoPlaybackContext.user_name == user_name)
            & (EmoPlaybackContext.lifecycle == "active")
            & (EmoPlaybackContext.authority_client_id == target_client_id)
            & (
                EmoPlaybackContext.authority_device_session_id
                == target_device_session_id
            )
        )
        .order_by(EmoPlaybackContext.playback_context_id)
        .limit(3)
    )


def _require_idle_handoff_standby(
    record: EmoPlaybackContext,
    target_device_session_id: str,
) -> None:
    if record.authority_device_session_id != target_device_session_id:
        raise PlaybackHandoffTargetConflictError(
            "Handoff target Context belongs to another device session"
        )
    if (
        json.loads(record.queue_json)
        or record.state != "idle"
        or record.position_ms != 0
        or record.track_id is not None
    ):
        raise PlaybackHandoffTargetConflictError(
            "Handoff target already has a non-idle Context"
        )
    active_prepare = EmoPlaybackPrepareTransaction.get_or_none(
        (EmoPlaybackPrepareTransaction.playback_context_id == record.playback_context_id)
        & (EmoPlaybackPrepareTransaction.epoch == record.epoch)
        & (EmoPlaybackPrepareTransaction.status == "preparing")
    )
    if active_prepare is not None:
        raise PlaybackHandoffTargetConflictError(
            "Handoff target idle Context has an active prepare"
        )


@_serialize_strict_playback_context_mutation
def createStrictPlaybackHandoff(
    playback_context_id: str,
    handoff: Dict[str, object],
    target_device_session_id: str,
) -> Tuple[Dict[str, object], bool]:
    user_name = handoff["userName"]
    source_client_id = handoff["sourceClientId"]
    target_client_id = handoff["targetClientId"]
    source_device_session_id = handoff.get("sourceDeviceSessionId")
    source_connection_nonce = handoff.get("sourceConnectionNonce")
    source_connection_epoch = handoff.get("sourceConnectionEpoch")
    durable_target_device_session_id = handoff.get("targetDeviceSessionId")
    target_connection_nonce = handoff.get("targetConnectionNonce")
    target_connection_epoch = handoff.get("targetConnectionEpoch")
    for field_name, value in (
        ("sourceDeviceSessionId", source_device_session_id),
        ("sourceConnectionNonce", source_connection_nonce),
        ("targetDeviceSessionId", durable_target_device_session_id),
        ("targetConnectionNonce", target_connection_nonce),
    ):
        _require_non_empty_string(value, field_name, 128)
    for field_name, value in (
        ("sourceConnectionEpoch", source_connection_epoch),
        ("targetConnectionEpoch", target_connection_epoch),
    ):
        _require_integer(value, field_name, 1)
        if value != 1:
            raise ValueError("%s must be exactly 1" % field_name)
    if durable_target_device_session_id != target_device_session_id:
        raise ValueError("Handoff target device generation does not match")
    open_connection(reuse=True)
    try:
        initial_source = EmoPlaybackContext.get_or_none(
            EmoPlaybackContext.playback_context_id == playback_context_id
        )
        if initial_source is None:
            raise LookupError("Playback context not found")
        old_authority_pair = _record_authority_pair(initial_source)
        target_pair = _strict_authority_pair_key(
            user_name,
            target_client_id,
            target_device_session_id,
        )
        with _strict_authority_pair_transaction(
            (old_authority_pair, target_pair)
        ):
            requireFollowSafetyLeaseResourceAvailable(
                user_name=user_name,
                client_id=target_client_id,
                device_session_id=target_device_session_id,
            )
            requirePlaybackHandoffResourceAvailable(
                user_name=user_name,
                client_id=target_client_id,
                device_session_id=target_device_session_id,
                allowed_handoff_id=handoff["handoffId"],
            )
            target_fence = _broadcast_fence_for_pair(
                user_name,
                target_client_id,
                target_device_session_id,
            )
            if target_fence is not None:
                _raise_broadcast_fence(
                    target_fence,
                    target_fence.playback_context_id or playback_context_id,
                )
            source_record = _getStrictPlaybackContextRecord(
                playback_context_id,
                user_name,
            )
            if source_record is None:
                raise LookupError("Playback context not found")
            if source_record.authority_client_id != source_client_id:
                raise PermissionError(
                    "Playback handoff source is no longer authority"
                )
            if (
                source_record.authority_device_session_id
                != source_device_session_id
            ):
                raise PermissionError(
                    "Playback handoff source device is no longer authority"
                )
            if source_record.control_version != handoff["baseControlVersion"]:
                raise PlaybackContextStaleVersionError(
                    _playback_context_payload(source_record),
                    "controlVersion",
                )
            active_handoff = EmoPlaybackHandoff.get_or_none(
                (EmoPlaybackHandoff.playback_context_id == playback_context_id)
                & EmoPlaybackHandoff.status.in_(
                    ("preparing", "ready", "committed", "committing")
                )
            )
            if active_handoff is not None:
                if active_handoff.handoff_id == handoff["handoffId"]:
                    _require_handoff_provisional_lane(active_handoff)
                    existing = serializePlaybackHandoff(active_handoff)
                    if any(
                        existing.get(field_name) != handoff.get(field_name)
                        for field_name in (
                            "requestId",
                            "playbackContextId",
                            "userName",
                            "sourceClientId",
                            "sourceDeviceSessionId",
                            "sourceConnectionNonce",
                            "sourceConnectionEpoch",
                            "targetClientId",
                            "targetDeviceSessionId",
                            "targetConnectionNonce",
                            "targetConnectionEpoch",
                            "originClientId",
                            "baseControlVersion",
                            "controlVersion",
                        )
                    ):
                        raise PlaybackHandoffTargetConflictError(
                            "Playback handoff immutable binding conflicts"
                        )
                    return existing, False
                raise PlaybackHandoffTargetConflictError(
                    "Playback handoff already in progress"
                )

            target_contexts = _active_handoff_target_context_records(
                user_name,
                target_client_id,
                target_device_session_id,
            )
            if len(target_contexts) > 1:
                raise PlaybackHandoffTargetConflictError(
                    "Handoff target has multiple active Contexts"
                )
            snapshot = dict(handoff.get("snapshot") or {})
            for field_name in (
                "targetStandbyPlaybackContextId",
                "targetStandbyEpoch",
                "targetStandbyAuthorityClientId",
                "targetStandbyAuthorityDeviceSessionId",
            ):
                snapshot.pop(field_name, None)
            if target_contexts:
                standby = target_contexts[0]
                requireFollowSafetyLeaseResourceAvailable(
                    playback_context_id=standby.playback_context_id,
                )
                requirePlaybackHandoffResourceAvailable(
                    playback_context_id=standby.playback_context_id,
                    allowed_handoff_id=handoff["handoffId"],
                )
                standby_fences = _broadcast_fences_for_context(
                    standby.playback_context_id
                )
                if standby_fences:
                    _raise_broadcast_fence(
                        standby_fences[0],
                        standby.playback_context_id,
                    )
                _require_idle_handoff_standby(
                    standby,
                    target_device_session_id,
                )
                snapshot.update(
                    {
                        "targetStandbyPlaybackContextId": (
                            standby.playback_context_id
                        ),
                        "targetStandbyEpoch": standby.epoch,
                        "targetStandbyAuthorityClientId": (
                            standby.authority_client_id
                        ),
                        "targetStandbyAuthorityDeviceSessionId": (
                            standby.authority_device_session_id
                        ),
                    }
                )
            source_device_record = EmoDevicePlaybackState.get_or_none(
                (
                    EmoDevicePlaybackState.playback_context_id
                    == playback_context_id
                )
                & (
                    EmoDevicePlaybackState.owner_client_id
                    == source_client_id
                )
                & (
                    EmoDevicePlaybackState.device_session_id
                    == source_device_session_id
                )
            )
            source_device_payload = (
                _device_playback_state_payload(source_device_record)
                if source_device_record is not None
                else {}
            )
            provisional_control_version = handoff.get("controlVersion")
            _require_integer(
                provisional_control_version,
                "controlVersion",
                1,
            )
            if provisional_control_version != source_record.control_version + 1:
                raise PlaybackHandoffTargetConflictError(
                    "Playback handoff provisional control version conflicts"
                )
            snapshot.update(
                {
                    "sourceEpoch": source_record.epoch,
                    "sourceVersion": source_record.version,
                    "sourceQueueRevision": source_record.queue_revision,
                    "sourceControlVersion": source_record.control_version,
                    "handoffControlVersion": provisional_control_version,
                    "queueSongIds": json.loads(source_record.queue_json),
                    "currentIndex": source_record.current_index,
                    "sourceContextTrackId": source_record.track_id,
                    "sourceContextState": source_record.state,
                    "trackId": source_device_payload.get(
                        "trackId",
                        source_record.track_id,
                    ),
                    "state": source_device_payload.get(
                        "state",
                        source_record.state,
                    ),
                    "positionMs": source_device_payload.get(
                        "positionMs",
                        source_record.position_ms,
                    ),
                    "positionSampledAtServerMs": source_device_payload.get(
                        "positionSampledAtServerMs",
                        int(source_record.updated_at.timestamp() * 1000),
                    ),
                    "playbackRate": source_device_payload.get(
                        "playbackRate",
                        1.0,
                    ),
                }
            )
            record = EmoPlaybackHandoff.create(
                handoff_id=handoff["handoffId"],
                request_id=handoff.get("requestId"),
                playback_context_id=playback_context_id,
                user_name=user_name,
                source_client_id=source_client_id,
                source_device_session_id=source_device_session_id,
                source_connection_nonce=source_connection_nonce,
                source_connection_epoch=source_connection_epoch,
                target_client_id=target_client_id,
                target_device_session_id=target_device_session_id,
                target_connection_nonce=target_connection_nonce,
                target_connection_epoch=target_connection_epoch,
                origin_client_id=handoff.get("originClientId"),
                status="preparing",
                base_control_version=handoff["baseControlVersion"],
                context_epoch=source_record.epoch,
                provisional_control_version=provisional_control_version,
                snapshot_json=json.dumps(
                    _sanitize_handoff_snapshot(snapshot),
                    ensure_ascii=True,
                ),
            )
            return serializePlaybackHandoff(record), True
    finally:
        close_connection()


def completeStrictPlaybackHandoff(
    playback_context_id: str,
    handoff_id: str,
    user_name: str,
    target_client_id: str,
    target_device_session_id: str,
    position_ms: Optional[int] = None,
    *,
    expected_source_client_id: str,
    expected_source_device_session_id: str,
    expected_source_connection_nonce: str,
    expected_source_connection_epoch: int,
    expected_target_connection_nonce: str,
    expected_target_connection_epoch: int,
) -> Optional[PlaybackHandoffCompleteResult]:
    _validate_handoff_generation_arguments(
        user_name,
        expected_source_client_id,
        expected_source_device_session_id,
        expected_source_connection_nonce,
        expected_source_connection_epoch,
    )
    _validate_handoff_generation_arguments(
        user_name,
        target_client_id,
        target_device_session_id,
        expected_target_connection_nonce,
        expected_target_connection_epoch,
    )
    open_connection(reuse=True)
    try:
        handoff_record = EmoPlaybackHandoff.get_or_none(
            EmoPlaybackHandoff.handoff_id == handoff_id
        )
        if handoff_record is None:
            return None
        snapshot = (
            json.loads(handoff_record.snapshot_json)
            if handoff_record.snapshot_json
            else {}
        )
        standby_context_id = snapshot.get(
            "targetStandbyPlaybackContextId"
        )
    finally:
        close_connection()
    context_ids = [playback_context_id]
    if isinstance(standby_context_id, str) and standby_context_id:
        context_ids.append(standby_context_id)
    with _strict_playback_context_lock_set(context_ids):
        open_connection(reuse=True)
        try:
            requireFollowSafetyLeaseResourceAvailable(
                playback_context_id=playback_context_id,
                user_name=user_name,
                client_id=target_client_id,
                device_session_id=target_device_session_id,
            )
            requirePlaybackHandoffResourceAvailable(
                playback_context_id=playback_context_id,
                user_name=user_name,
                client_id=target_client_id,
                device_session_id=target_device_session_id,
                mutation_name="completeStrictPlaybackHandoff",
                allowed_handoff_id=handoff_id,
            )
            _require_broadcast_context_mutation_allowed(
                playback_context_id,
                "completeStrictPlaybackHandoff",
            )
            target_fence = _broadcast_fence_for_pair(
                user_name,
                target_client_id,
                target_device_session_id,
            )
            if target_fence is not None:
                _raise_broadcast_fence(
                    target_fence,
                    target_fence.playback_context_id or playback_context_id,
                )
            if isinstance(standby_context_id, str) and standby_context_id:
                requireFollowSafetyLeaseResourceAvailable(
                    playback_context_id=standby_context_id,
                )
                requirePlaybackHandoffResourceAvailable(
                    playback_context_id=standby_context_id,
                    mutation_name="completeStrictPlaybackHandoff",
                    allowed_handoff_id=handoff_id,
                )
                standby_fences = _broadcast_fences_for_context(
                    standby_context_id
                )
                if standby_fences:
                    _raise_broadcast_fence(
                        standby_fences[0],
                        standby_context_id,
                    )
        finally:
            close_connection()
        return _completeStrictPlaybackHandoffLocked(
            playback_context_id,
            handoff_id,
            user_name,
            target_client_id,
            target_device_session_id,
            position_ms=position_ms,
            expected_source_client_id=expected_source_client_id,
            expected_source_device_session_id=(
                expected_source_device_session_id
            ),
            expected_source_connection_nonce=(
                expected_source_connection_nonce
            ),
            expected_source_connection_epoch=(
                expected_source_connection_epoch
            ),
            expected_target_connection_nonce=(
                expected_target_connection_nonce
            ),
            expected_target_connection_epoch=(
                expected_target_connection_epoch
            ),
        )


def _completeStrictPlaybackHandoffLocked(
    playback_context_id: str,
    handoff_id: str,
    user_name: str,
    target_client_id: str,
    target_device_session_id: str,
    position_ms: Optional[int] = None,
    *,
    expected_source_client_id: str,
    expected_source_device_session_id: str,
    expected_source_connection_nonce: str,
    expected_source_connection_epoch: int,
    expected_target_connection_nonce: str,
    expected_target_connection_epoch: int,
) -> Optional[PlaybackHandoffCompleteResult]:
    open_connection(reuse=True)
    try:
        initial_context_record = EmoPlaybackContext.get_or_none(
            EmoPlaybackContext.playback_context_id == playback_context_id
        )
        initial_handoff_record = EmoPlaybackHandoff.get_or_none(
            EmoPlaybackHandoff.handoff_id == handoff_id
        )
        if initial_context_record is None or initial_handoff_record is None:
            return None
        initial_snapshot = (
            json.loads(initial_handoff_record.snapshot_json)
            if initial_handoff_record.snapshot_json
            else {}
        )
        standby_context_id = initial_snapshot.get(
            "targetStandbyPlaybackContextId"
        )
        standby_record = (
            EmoPlaybackContext.get_or_none(
                EmoPlaybackContext.playback_context_id
                == standby_context_id
            )
            if isinstance(standby_context_id, str) and standby_context_id
            else None
        )
        old_authority_pair = _record_authority_pair(
            initial_context_record
        )
        new_authority_pair = _strict_authority_pair_key(
            user_name,
            target_client_id,
            target_device_session_id,
        )
        authority_pairs = [old_authority_pair, new_authority_pair]
        if standby_record is not None:
            authority_pairs.append(_record_authority_pair(standby_record))
        with _strict_authority_pair_transaction(
            authority_pairs
        ):
            context_record = EmoPlaybackContext.get_or_none(
                EmoPlaybackContext.playback_context_id == playback_context_id
            )
            handoff_record = EmoPlaybackHandoff.get_or_none(
                EmoPlaybackHandoff.handoff_id == handoff_id
            )
            if context_record is None or handoff_record is None:
                return None
            if (
                context_record.user_name != user_name
                or handoff_record.user_name != user_name
            ):
                raise PermissionError("Playback handoff belongs to another user")
            if context_record.lifecycle == "closed":
                raise PlaybackContextClosedError(
                    _playback_context_payload(context_record)
                )
            if handoff_record.playback_context_id != playback_context_id:
                raise ValueError("Playback handoff context does not match")
            if handoff_record.target_client_id != target_client_id:
                raise PermissionError("Playback handoff target does not match")
            _require_handoff_generation(handoff_record)
            if not _handoff_generation_matches(
                handoff_record,
                "source",
                user_name,
                expected_source_client_id,
                expected_source_device_session_id,
                expected_source_connection_nonce,
                expected_source_connection_epoch,
            ):
                raise PlaybackHandoffTargetConflictError(
                    "Playback handoff source physical generation changed"
                )
            if not _handoff_generation_matches(
                handoff_record,
                "target",
                user_name,
                target_client_id,
                target_device_session_id,
                expected_target_connection_nonce,
                expected_target_connection_epoch,
            ):
                raise PlaybackHandoffTargetConflictError(
                    "Playback handoff target physical generation changed"
                )

            snapshot = (
                json.loads(handoff_record.snapshot_json)
                if handoff_record.snapshot_json
                else {}
            )
            if handoff_record.status == "completed":
                target_record = EmoDevicePlaybackState.get_or_none(
                    (
                        EmoDevicePlaybackState.playback_context_id
                        == playback_context_id
                    )
                    & (
                        EmoDevicePlaybackState.owner_client_id
                        == target_client_id
                    )
                    & (
                        EmoDevicePlaybackState.device_session_id
                        == target_device_session_id
                    )
                )
                handoff_payload = serializePlaybackHandoff(handoff_record)
                return PlaybackHandoffCompleteResult(
                    _playback_context_payload(context_record),
                    handoff_payload,
                    None
                    if target_record is None
                    else _device_playback_state_payload(target_record),
                    False,
                    (),
                )
            if handoff_record.status not in ("committed", "committing"):
                raise ValueError("Playback handoff is not committing")
            _require_handoff_provisional_lane(handoff_record)
            if context_record.authority_client_id != handoff_record.source_client_id:
                raise PermissionError("Playback handoff source is no longer authority")
            if (
                context_record.authority_device_session_id
                != handoff_record.source_device_session_id
            ):
                raise PermissionError(
                    "Playback handoff source device is no longer authority"
                )
            if context_record.control_version != handoff_record.base_control_version:
                raise PlaybackContextStaleVersionError(
                    _playback_context_payload(context_record),
                    "controlVersion",
                )
            if context_record.epoch != handoff_record.context_epoch:
                raise PlaybackContextStaleVersionError(
                    _playback_context_payload(context_record),
                    "epoch",
                )

            expected_standby_id = snapshot.get(
                "targetStandbyPlaybackContextId"
            )
            target_contexts = _active_handoff_target_context_records(
                user_name,
                target_client_id,
                target_device_session_id,
            )
            if len(target_contexts) > 1:
                raise PlaybackHandoffTargetConflictError(
                    "Handoff target has multiple active Contexts"
                )
            retired_context = None
            if expected_standby_id is None:
                if target_contexts:
                    raise PlaybackHandoffTargetConflictError(
                        "Handoff target created a Context after prepare started"
                    )
            else:
                if (
                    len(target_contexts) != 1
                    or target_contexts[0].playback_context_id
                    != expected_standby_id
                ):
                    raise PlaybackHandoffTargetConflictError(
                        "Handoff target standby Context changed"
                    )
                standby_record = target_contexts[0]
                if (
                    standby_record.epoch
                    != snapshot.get("targetStandbyEpoch")
                    or standby_record.authority_client_id
                    != snapshot.get("targetStandbyAuthorityClientId")
                    or standby_record.authority_device_session_id
                    != snapshot.get(
                        "targetStandbyAuthorityDeviceSessionId"
                    )
                ):
                    raise PlaybackHandoffTargetConflictError(
                        "Handoff target standby binding changed"
                    )
                _require_idle_handoff_standby(
                    standby_record,
                    target_device_session_id,
                )
                closed_at = now()
                standby_record.lifecycle = "closed"
                standby_record.closed_at = closed_at
                standby_record.version = max(1, standby_record.version) + 1
                standby_record.updated_at = closed_at
                standby_record.save(
                    only=(
                        EmoPlaybackContext.lifecycle,
                        EmoPlaybackContext.closed_at,
                        EmoPlaybackContext.version,
                        EmoPlaybackContext.updated_at,
                    )
                )
                retired_context = _playback_context_payload(standby_record)

            next_control_version = handoff_record.provisional_control_version
            context_record.authority_client_id = target_client_id
            context_record.authority_device_session_id = target_device_session_id
            context_record.origin_client_id = handoff_record.origin_client_id
            context_record.state = "playing"
            if position_ms is not None:
                context_record.position_ms = position_ms
            context_record.control_version = next_control_version
            context_record.version = max(1, context_record.version) + 1
            context_record.epoch = max(1, context_record.epoch) + 1
            context_record.updated_at = now()
            context_payload = _playback_context_payload(context_record)
            context_record.playback_json = json.dumps(
                context_payload,
                ensure_ascii=True,
            )
            context_record.save()

            (
                EmoDevicePlaybackState.update(is_authority=0)
                .where(
                    EmoDevicePlaybackState.playback_context_id
                    == playback_context_id
                )
                .execute()
            )
            target_record = EmoDevicePlaybackState.get_or_none(
                (
                    EmoDevicePlaybackState.playback_context_id
                    == playback_context_id
                )
                & (
                    EmoDevicePlaybackState.owner_client_id == target_client_id
                )
            )
            device_payload = {
                "playbackContextId": playback_context_id,
                "deviceSessionId": target_device_session_id,
                "sourceClientId": target_client_id,
                "state": context_record.state,
                "trackId": context_record.track_id,
                "positionMs": context_record.position_ms,
                "isAuthority": True,
                "mode": "handoff",
            }
            if target_record is None:
                target_record = EmoDevicePlaybackState.create(
                    playback_context_id=playback_context_id,
                    device_session_id=target_device_session_id,
                    owner_client_id=target_client_id,
                    user_name=user_name,
                    state=context_record.state,
                    track_id=context_record.track_id,
                    position_ms=context_record.position_ms,
                    is_authority=1,
                    mode="handoff",
                    playback_json=json.dumps(device_payload, ensure_ascii=True),
                )
            else:
                target_record.device_session_id = target_device_session_id
                target_record.user_name = user_name
                target_record.state = context_record.state
                target_record.track_id = context_record.track_id
                target_record.position_ms = context_record.position_ms
                target_record.is_authority = 1
                target_record.mode = "handoff"
                target_record.playback_json = json.dumps(
                    device_payload,
                    ensure_ascii=True,
                )
                target_record.updated_at = now()
                target_record.save()

            handoff_record.status = "completed"
            snapshot["handoffControlVersion"] = next_control_version
            handoff_record.snapshot_json = json.dumps(snapshot, ensure_ascii=True)
            handoff_record.error_code = None
            handoff_record.error_message = None
            handoff_record.updated_at = now()
            handoff_record.save()
            handoff_payload = serializePlaybackHandoff(handoff_record)
            return PlaybackHandoffCompleteResult(
                context_payload,
                handoff_payload,
                _device_playback_state_payload(target_record),
                True,
                authority_pairs,
                retired_context=retired_context,
            )
    finally:
        close_connection()


@_serialize_strict_playback_context_mutation
def terminateStrictPlaybackHandoff(
    playback_context_id: str,
    handoff_id: str,
    user_name: str,
    status: str,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    expected_generation_role: Optional[str] = None,
    expected_device_session_id: Optional[str] = None,
    expected_connection_nonce: Optional[str] = None,
    expected_connection_epoch: Optional[int] = None,
) -> Optional[Tuple[Dict[str, object], bool]]:
    if status not in ("cancelled", "failed", "timed_out"):
        raise ValueError("Unsupported handoff terminal status")
    open_connection(reuse=True)
    try:
        observed = EmoPlaybackHandoff.get_or_none(
            EmoPlaybackHandoff.handoff_id == handoff_id
        )
        if observed is None:
            return None
        _observed_contexts, authority_pairs = _handoff_lock_keys((observed,))
        with _strict_authority_pair_transaction(authority_pairs):
            record = EmoPlaybackHandoff.get_or_none(
                EmoPlaybackHandoff.handoff_id == handoff_id
            )
            if record is None:
                return None
            if record.playback_context_id != playback_context_id:
                raise ValueError("Playback handoff context does not match")
            if record.user_name != user_name:
                raise PermissionError("Playback handoff belongs to another user")
            if expected_generation_role is not None:
                if expected_generation_role not in {"source", "target"}:
                    raise ValueError(
                        "expectedGenerationRole must be source or target"
                    )
                _validate_handoff_generation_arguments(
                    user_name,
                    getattr(
                        record,
                        "%s_client_id" % expected_generation_role,
                    ),
                    expected_device_session_id,
                    expected_connection_nonce,
                    expected_connection_epoch,
                )
                if not _handoff_generation_matches(
                    record,
                    expected_generation_role,
                    user_name,
                    getattr(
                        record,
                        "%s_client_id" % expected_generation_role,
                    ),
                    expected_device_session_id,
                    expected_connection_nonce,
                    expected_connection_epoch,
                ):
                    raise PlaybackHandoffTargetConflictError(
                        "Playback handoff physical generation changed"
                    )
            transitioned = False
            if record.status in HANDOFF_NONTERMINAL_STATUSES:
                record.status = status
                record.error_code = error_code
                record.error_message = error_message
                record.updated_at = now()
                record.save()
                transitioned = True
            return serializePlaybackHandoff(record), transitioned
    finally:
        close_connection()


@_serialize_strict_playback_context_mutation
def commitStrictPlaybackHandoff(
    playback_context_id: str,
    handoff_id: str,
    user_name: str,
    complete_expires_at_ms: int,
) -> Optional[Tuple[Dict[str, object], bool]]:
    open_connection(reuse=True)
    try:
        initial_context = EmoPlaybackContext.get_or_none(
            EmoPlaybackContext.playback_context_id == playback_context_id
        )
        initial_handoff = EmoPlaybackHandoff.get_or_none(
            EmoPlaybackHandoff.handoff_id == handoff_id
        )
        if initial_context is None or initial_handoff is None:
            return None
        authority_pairs = (
            _record_authority_pair(initial_context),
            _strict_authority_pair_key(
                initial_handoff.user_name,
                initial_handoff.target_client_id,
                initial_handoff.target_device_session_id,
            ),
        )
        with _strict_authority_pair_transaction(authority_pairs):
            record = EmoPlaybackHandoff.get_or_none(
                EmoPlaybackHandoff.handoff_id == handoff_id
            )
            if record is None:
                return None
            if record.playback_context_id != playback_context_id:
                raise ValueError("Playback handoff context does not match")
            if record.user_name != user_name:
                raise PermissionError("Playback handoff belongs to another user")
            _require_handoff_generation(record)
            _require_handoff_provisional_lane(record)
            if record.status != "preparing":
                return serializePlaybackHandoff(record), False
            context_record = _getStrictPlaybackContextRecord(
                playback_context_id,
                user_name,
            )
            if context_record is None:
                return None
            source_device_record = EmoDevicePlaybackState.get_or_none(
                (
                    EmoDevicePlaybackState.playback_context_id
                    == playback_context_id
                )
                & (
                    EmoDevicePlaybackState.owner_client_id
                    == record.source_client_id
                )
                & (
                    EmoDevicePlaybackState.device_session_id
                    == record.source_device_session_id
                )
            )
            if _handoff_source_changed_before_commit(
                record,
                context_record,
                source_device_record,
            ):
                _fail_handoff_source_changed(record)
                return serializePlaybackHandoff(record), True

            snapshot = _handoff_snapshot(record)
            source_device = _device_playback_state_payload(
                source_device_record
            )
            snapshot.update(
                {
                    "positionMs": source_device["positionMs"],
                    "positionSampledAtServerMs": source_device[
                        "positionSampledAtServerMs"
                    ],
                    "completeExpiresAtMs": complete_expires_at_ms,
                }
            )
            record.status = "committing"
            record.snapshot_json = json.dumps(snapshot, ensure_ascii=True)
            record.updated_at = now()
            record.save()
            return serializePlaybackHandoff(record), True
    finally:
        close_connection()


@_serialize_strict_playback_context_mutation
def markStrictPlaybackHandoffCommitEnqueued(
    playback_context_id: str,
    handoff_id: str,
    user_name: str,
) -> Optional[Tuple[Dict[str, object], bool]]:
    open_connection(reuse=True)
    try:
        with _strict_playback_context_transaction():
            record = EmoPlaybackHandoff.get_or_none(
                EmoPlaybackHandoff.handoff_id == handoff_id
            )
            if record is None:
                return None
            if record.playback_context_id != playback_context_id:
                raise ValueError("Playback handoff context does not match")
            if record.user_name != user_name:
                raise PermissionError("Playback handoff belongs to another user")
            _require_handoff_generation(record)
            _require_handoff_provisional_lane(record)
            if record.status == "committed":
                return serializePlaybackHandoff(record), False
            if record.status != "committing":
                raise PlaybackHandoffTargetConflictError(
                    "Playback handoff commit is not awaiting enqueue"
                )
            record.status = "committed"
            record.updated_at = now()
            record.save(
                only=(
                    EmoPlaybackHandoff.status,
                    EmoPlaybackHandoff.updated_at,
                )
            )
            return serializePlaybackHandoff(record), True
    finally:
        close_connection()


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


def listActivePlaybackContextBindings(
    user_name: str,
    authority_client_id: str,
    authority_device_session_id: str,
) -> List[Dict[str, object]]:
    open_connection(reuse=True)
    try:
        query = (
            EmoPlaybackContext.select(
                EmoPlaybackContext.playback_context_id,
                EmoPlaybackContext.authority_client_id,
                EmoPlaybackContext.authority_device_session_id,
            )
            .where(
                (EmoPlaybackContext.user_name == user_name)
                & (EmoPlaybackContext.lifecycle == "active")
                & (
                    EmoPlaybackContext.authority_client_id
                    == authority_client_id
                )
                & (
                    EmoPlaybackContext.authority_device_session_id
                    == authority_device_session_id
                )
            )
            .order_by(EmoPlaybackContext.playback_context_id.asc())
        )
        return [
            serializePlaybackContextBindingV2(
                {
                    "playbackContextId": record.playback_context_id,
                    "authorityClientId": record.authority_client_id,
                    "authorityDeviceSessionId": (
                        record.authority_device_session_id
                    ),
                }
            )
            for record in query
        ]
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
    while True:
        open_connection(reuse=True)
        try:
            observed = list(
                EmoPlaybackHandoff.select().where(
                    EmoPlaybackHandoff.status.in_(HANDOFF_NONTERMINAL_STATUSES)
                )
            )
            context_ids, authority_pairs = _handoff_lock_keys(observed)
        finally:
            close_connection()

        retry = False
        with _strict_playback_context_lock_set(context_ids):
            with _strict_authority_pair_lock(authority_pairs):
                open_connection(reuse=True)
                try:
                    with _strict_playback_context_transaction():
                        records = list(
                            EmoPlaybackHandoff.select()
                            .where(
                                EmoPlaybackHandoff.status.in_(
                                    HANDOFF_NONTERMINAL_STATUSES
                                )
                            )
                            .order_by(
                                EmoPlaybackHandoff.playback_context_id,
                                EmoPlaybackHandoff.handoff_id,
                            )
                        )
                        current_context_ids, current_pairs = _handoff_lock_keys(
                            records
                        )
                        if not (
                            current_context_ids <= context_ids
                            and current_pairs <= authority_pairs
                        ):
                            retry = True
                        else:
                            reconciled = []
                            for record in records:
                                record.status = "failed"
                                record.error_code = "server_restart"
                                record.error_message = (
                                    "Server restarted before handoff completed"
                                )
                                record.updated_at = now()
                                record.save()
                                reconciled.append(record.handoff_id)
                finally:
                    close_connection()
        if not retry:
            return reconciled


def _handoff_lock_keys(records):
    context_ids = set()
    authority_pairs = set()
    for record in records:
        context_ids.add(record.playback_context_id)
        standby_context_id = _handoff_snapshot(record).get(
            "targetStandbyPlaybackContextId"
        )
        if isinstance(standby_context_id, str) and standby_context_id:
            context_ids.add(standby_context_id)
        authority_pairs.add(
            _strict_authority_pair_key(
                record.user_name,
                record.source_client_id,
                record.source_device_session_id,
            )
        )
        authority_pairs.add(
            _strict_authority_pair_key(
                record.user_name,
                record.target_client_id,
                record.target_device_session_id,
            )
        )
    return frozenset(context_ids), frozenset(authority_pairs)


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
            "contextEpoch": record.context_epoch,
            "appliedControlVersion": record.applied_control_version,
            "clientSeq": record.client_seq,
            "updatedAt": record.updated_at.timestamp(),
        }
    )
    payload.setdefault("serverUpdatedAtMs", int(record.updated_at.timestamp() * 1000))
    payload.setdefault(
        "positionSampledAtServerMs",
        payload["serverUpdatedAtMs"],
    )
    payload.setdefault("playbackRate", 1.0)
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
        context_record = EmoPlaybackContext.get_or_none(
            EmoPlaybackContext.playback_context_id == playback_context_id
        )
        if context_record is None:
            return []
        payloads = []
        query = EmoDevicePlaybackState.select().where(
            (EmoDevicePlaybackState.playback_context_id == playback_context_id)
            & (EmoDevicePlaybackState.context_epoch == context_record.epoch)
            & (EmoDevicePlaybackState.applied_control_version >= 1)
            & (EmoDevicePlaybackState.client_seq >= 1)
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
            (EmoDevicePlaybackState.playback_context_id == playback_context_id)
            & (EmoDevicePlaybackState.context_epoch == context_record.epoch)
            & (EmoDevicePlaybackState.applied_control_version >= 1)
            & (EmoDevicePlaybackState.client_seq >= 1)
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
    context_epoch = payload.get("epoch") or payload.get("contextEpoch") or 1
    applied_control_version = payload.get("appliedControlVersion") or 0
    client_seq = payload.get("clientSeq") or 0
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
                context_epoch=context_epoch,
                applied_control_version=applied_control_version,
                client_seq=client_seq,
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
        record.context_epoch = context_epoch
        record.applied_control_version = applied_control_version
        record.client_seq = client_seq
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
        return serializePlaybackHandoff(record)
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
        return serializePlaybackHandoff(record)
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
                & EmoPlaybackHandoff.status.in_(HANDOFF_NONTERMINAL_STATUSES)
            )
            .order_by(EmoPlaybackHandoff.created_at.asc())
        )
        return [serializePlaybackHandoff(record) for record in query]
    finally:
        close_connection()


def listActivePlaybackHandoffsForPhysicalGeneration(
    user_name,
    client_id,
    device_session_id,
    connection_nonce,
    connection_epoch,
):
    _validate_handoff_generation_arguments(
        user_name,
        client_id,
        device_session_id,
        connection_nonce,
        connection_epoch,
    )
    open_connection(reuse=True)
    try:
        source_match = (
            (EmoPlaybackHandoff.source_client_id == client_id)
            & (
                EmoPlaybackHandoff.source_device_session_id
                == device_session_id
            )
            & (
                EmoPlaybackHandoff.source_connection_nonce
                == connection_nonce
            )
            & (
                EmoPlaybackHandoff.source_connection_epoch
                == connection_epoch
            )
        )
        target_match = (
            (EmoPlaybackHandoff.target_client_id == client_id)
            & (
                EmoPlaybackHandoff.target_device_session_id
                == device_session_id
            )
            & (
                EmoPlaybackHandoff.target_connection_nonce
                == connection_nonce
            )
            & (
                EmoPlaybackHandoff.target_connection_epoch
                == connection_epoch
            )
        )
        query = (
            EmoPlaybackHandoff.select()
            .where(
                (EmoPlaybackHandoff.user_name == user_name)
                & EmoPlaybackHandoff.status.in_(
                    HANDOFF_NONTERMINAL_STATUSES
                )
                & (source_match | target_match)
            )
            .order_by(
                EmoPlaybackHandoff.playback_context_id,
                EmoPlaybackHandoff.handoff_id,
            )
        )
        return [serializePlaybackHandoff(record) for record in query]
    finally:
        close_connection()


def savePlaybackHandoff(handoff):
    payload = dict(handoff)
    handoff_id = payload.get("handoffId")
    snapshot = _sanitize_handoff_snapshot(dict(payload.get("snapshot") or {}))
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
            _validate_optional_handoff_generation_payload(payload)
            _validate_optional_handoff_provisional_payload(payload, snapshot)
            context_epoch = payload.get("contextEpoch")
            provisional_control_version = (
                payload.get("controlVersion")
                if context_epoch is not None
                else None
            )
            EmoPlaybackHandoff.create(
                handoff_id=handoff_id,
                request_id=payload.get("requestId"),
                playback_context_id=payload.get("playbackContextId"),
                user_name=payload.get("userName"),
                source_client_id=payload.get("sourceClientId"),
                source_device_session_id=payload.get("sourceDeviceSessionId"),
                source_connection_nonce=payload.get("sourceConnectionNonce"),
                source_connection_epoch=payload.get("sourceConnectionEpoch"),
                target_client_id=payload.get("targetClientId"),
                target_device_session_id=payload.get("targetDeviceSessionId"),
                target_connection_nonce=payload.get("targetConnectionNonce"),
                target_connection_epoch=payload.get("targetConnectionEpoch"),
                origin_client_id=payload.get("originClientId"),
                status=payload.get("status") or "preparing",
                base_control_version=payload.get("baseControlVersion") or 0,
                context_epoch=context_epoch,
                provisional_control_version=provisional_control_version,
                snapshot_json=json.dumps(snapshot, ensure_ascii=True),
                error_code=payload.get("errorCode"),
                error_message=payload.get("errorMessage"),
            )
            return

        incoming_status = payload.get("status")
        if incoming_status is None:
            incoming_status = record.status
        _require_immutable_handoff_binding(record, payload)
        if record.status in HANDOFF_TERMINAL_STATUSES:
            if incoming_status != record.status:
                raise PlaybackHandoffTargetConflictError(
                    "Playback handoff terminal status is immutable"
                )
            return

        persisted_snapshot = _handoff_snapshot(record)
        if record.context_epoch is not None:
            for field_name in _HANDOFF_PROVISIONAL_SNAPSHOT_FIELDS:
                if (
                    field_name in snapshot
                    and snapshot[field_name]
                    != persisted_snapshot.get(field_name)
                ):
                    raise PlaybackHandoffTargetConflictError(
                        "Playback handoff provisional snapshot is immutable"
                    )
        persisted_snapshot.update(snapshot)
        record.status = incoming_status
        record.snapshot_json = json.dumps(
            persisted_snapshot,
            ensure_ascii=True,
        )
        record.error_code = payload.get("errorCode")
        record.error_message = payload.get("errorMessage")
        _validate_optional_handoff_generation_record(record)
        record.updated_at = now()
        record.save()
    finally:
        close_connection()


def _require_immutable_handoff_binding(record, payload):
    immutable_fields = (
        ("requestId", "request_id"),
        ("playbackContextId", "playback_context_id"),
        ("userName", "user_name"),
        ("sourceClientId", "source_client_id"),
        ("sourceDeviceSessionId", "source_device_session_id"),
        ("sourceConnectionNonce", "source_connection_nonce"),
        ("sourceConnectionEpoch", "source_connection_epoch"),
        ("targetClientId", "target_client_id"),
        ("targetDeviceSessionId", "target_device_session_id"),
        ("targetConnectionNonce", "target_connection_nonce"),
        ("targetConnectionEpoch", "target_connection_epoch"),
        ("originClientId", "origin_client_id"),
        ("baseControlVersion", "base_control_version"),
        ("contextEpoch", "context_epoch"),
        ("controlVersion", "provisional_control_version"),
    )
    for payload_name, record_name in immutable_fields:
        if payload_name not in payload:
            continue
        incoming = payload[payload_name]
        persisted = getattr(record, record_name)
        if persisted is None and payload_name == "controlVersion":
            persisted = _handoff_snapshot(record).get(
                "handoffControlVersion"
            )
        if incoming != persisted or (
            payload_name.endswith("Epoch")
            and type(incoming) is not type(persisted)
        ):
            raise PlaybackHandoffTargetConflictError(
                "Playback handoff %s is immutable" % payload_name
            )


def _validate_optional_handoff_provisional_payload(payload, snapshot):
    context_epoch = payload.get("contextEpoch")
    if context_epoch is None:
        return
    base_control_version = payload.get("baseControlVersion")
    provisional_control_version = payload.get("controlVersion")
    if type(context_epoch) is not int or context_epoch < 1:
        raise PlaybackHandoffTargetConflictError(
            "Playback handoff context epoch is invalid"
        )
    if type(base_control_version) is not int or base_control_version < 1:
        raise PlaybackHandoffTargetConflictError(
            "Playback handoff base control version is invalid"
        )
    if (
        type(provisional_control_version) is not int
        or provisional_control_version != base_control_version + 1
    ):
        raise PlaybackHandoffTargetConflictError(
            "Playback handoff provisional control version is invalid"
        )
    for field_name, expected in (
        ("sourceEpoch", context_epoch),
        ("sourceControlVersion", base_control_version),
        ("handoffControlVersion", provisional_control_version),
    ):
        if snapshot.get(field_name) != expected:
            raise PlaybackHandoffTargetConflictError(
                "Playback handoff provisional lane snapshot conflicts"
            )


def _validate_optional_handoff_generation_payload(payload):
    generation_fields = (
        "sourceDeviceSessionId",
        "sourceConnectionNonce",
        "sourceConnectionEpoch",
        "targetDeviceSessionId",
        "targetConnectionNonce",
        "targetConnectionEpoch",
    )
    supplied = [payload.get(field_name) for field_name in generation_fields]
    if not any(value is not None for value in supplied):
        return
    if any(value is None for value in supplied):
        raise PlaybackHandoffTargetConflictError(
            "Playback handoff physical generation is incomplete"
        )
    for field_name in ("source", "target"):
        _validate_handoff_generation_arguments(
            payload.get("userName"),
            payload.get("%sClientId" % field_name),
            payload.get("%sDeviceSessionId" % field_name),
            payload.get("%sConnectionNonce" % field_name),
            payload.get("%sConnectionEpoch" % field_name),
        )


def _validate_optional_handoff_generation_record(record):
    values = (
        record.source_device_session_id,
        record.source_connection_nonce,
        record.source_connection_epoch,
        record.target_device_session_id,
        record.target_connection_nonce,
        record.target_connection_epoch,
    )
    if not any(value is not None for value in values):
        return
    _require_handoff_generation(record)
