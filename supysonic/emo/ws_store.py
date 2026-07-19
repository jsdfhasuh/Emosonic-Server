import hashlib
import json
import threading
import time
from uuid import uuid4
from contextlib import contextmanager
from functools import wraps
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from peewee import IntegrityError, SqliteDatabase

from ..db import (
    EmoDevicePlaybackState,
    EmoLocalQueue,
    EmoPlaybackControlTransaction,
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


class PlaybackContextQueueRequiredError(Exception):
    def __init__(self, playback_context):
        super().__init__("Playback context requires a non-empty queue")
        self.playback_context = playback_context


class PlaybackControlTransactionConflictError(Exception):
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
    ) -> None:
        super().__init__(playback_context)
        self.mutated = bool(mutated)
        self.affected_authority_pairs = _distinct_authority_pairs(
            affected_authority_pairs
        )
        self.canonical_context = dict(playback_context)


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
            return function(playback_context_id, *args, **kwargs)

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


def _load_json_object(value):
    if not value:
        return None
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("Persisted transaction JSON must be an object")
    return loaded


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
        "acceptedTarget": _load_json_object(record.accepted_target_json),
        "status": record.status,
        "acceptedAtMs": record.accepted_at_ms,
        "executionTimeoutMs": record.execution_timeout_ms,
        "watchdogDeadlineAtMs": record.watchdog_deadline_at_ms,
    }
    optional = {
        "errorCode": record.error_code,
        "dependsOnControlVersion": record.depends_on_control_version,
        "appliedControlVersion": record.applied_control_version,
        "terminalFingerprint": record.terminal_fingerprint,
        "terminalAtMs": record.terminal_at_ms,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    return payload


def _control_transaction_identity(
    user_name,
    requesting_client_id,
    authority_client_id,
    authority_device_session_id,
    routed_connection_nonce,
    routed_connection_epoch,
    action,
    accepted_target_json,
    accepted_at_ms,
    execution_timeout_ms,
):
    return (
        user_name,
        requesting_client_id,
        authority_client_id,
        authority_device_session_id,
        routed_connection_nonce,
        routed_connection_epoch,
        action,
        accepted_target_json,
        accepted_at_ms,
        execution_timeout_ms,
    )


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
):
    if execution_timeout_ms < 1:
        raise ValueError("executionTimeoutMs must be positive")
    accepted_target_json = _canonical_json(accepted_target)
    identity = _control_transaction_identity(
        user_name,
        requesting_client_id,
        authority_client_id,
        authority_device_session_id,
        routed_connection_nonce,
        routed_connection_epoch,
        action,
        accepted_target_json,
        accepted_at_ms,
        execution_timeout_ms,
    )
    open_connection(reuse=True)
    try:
        with _strict_playback_context_transaction():
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
                    existing.authority_client_id,
                    existing.authority_device_session_id,
                    existing.routed_connection_nonce,
                    existing.routed_connection_epoch,
                    existing.action,
                    existing.accepted_target_json,
                    existing.accepted_at_ms,
                    existing.execution_timeout_ms,
                )
                if existing_identity != identity:
                    raise PlaybackControlTransactionConflictError(
                        "Control transaction identity conflict"
                    )
                return serializePlaybackControlTransaction(existing), False
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
                action=action,
                accepted_target_json=accepted_target_json,
                status="pending",
                accepted_at_ms=accepted_at_ms,
                execution_timeout_ms=execution_timeout_ms,
                watchdog_deadline_at_ms=(accepted_at_ms + execution_timeout_ms + 2000),
            )
            return serializePlaybackControlTransaction(record), True
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
) -> List[Dict[str, object]]:
    open_connection(reuse=True)
    try:
        query = (
            EmoPlaybackControlTransaction.select()
            .where(
                (EmoPlaybackControlTransaction.user_name == user_name)
                & (
                    EmoPlaybackControlTransaction.authority_client_id
                    == authority_client_id
                )
                & (
                    EmoPlaybackControlTransaction.authority_device_session_id
                    == authority_device_session_id
                )
                & (
                    EmoPlaybackControlTransaction.routed_connection_nonce
                    == routed_connection_nonce
                )
                & (EmoPlaybackControlTransaction.status == "pending")
            )
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
    open_connection(reuse=True)
    try:
        query = (
            EmoPlaybackControlTransaction.select()
            .where(
                (EmoPlaybackControlTransaction.status == "pending")
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
):
    if status not in {"committed", "failed", "superseded"}:
        raise ValueError("Invalid control transaction terminal status")
    terminal = {
        "status": status,
        "errorCode": error_code,
        "dependsOnControlVersion": depends_on_control_version,
        "appliedControlVersion": applied_control_version,
    }
    terminal_fingerprint = _json_fingerprint(terminal)
    open_connection(reuse=True)
    try:
        with _strict_playback_context_transaction():
            record = EmoPlaybackControlTransaction.get_or_none(
                (EmoPlaybackControlTransaction.playback_context_id == playback_context_id)
                & (EmoPlaybackControlTransaction.epoch == epoch)
                & (
                    EmoPlaybackControlTransaction.command_control_version
                    == command_control_version
                )
            )
            if record is None:
                return None, False
            if record.status != "pending":
                if record.terminal_fingerprint != terminal_fingerprint:
                    raise PlaybackControlTransactionConflictError(
                        "Control transaction terminal conflict"
                    )
                return serializePlaybackControlTransaction(record), False
            updated = (
                EmoPlaybackControlTransaction.update(
                    status=status,
                    error_code=error_code,
                    depends_on_control_version=depends_on_control_version,
                    applied_control_version=applied_control_version,
                    terminal_fingerprint=terminal_fingerprint,
                    terminal_at_ms=terminal_at_ms,
                    updated_at=now(),
                )
                .where(
                    (EmoPlaybackControlTransaction.id == record.id)
                    & (EmoPlaybackControlTransaction.status == "pending")
                )
                .execute()
            )
            if updated != 1:
                raise PlaybackControlTransactionConflictError(
                    "Control transaction changed concurrently"
                )
            record = EmoPlaybackControlTransaction.get_by_id(record.id)
            return serializePlaybackControlTransaction(record), True
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


def _passive_correction_from_device(record, device_state):
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
        "clientSeq": device_state.client_seq,
        "serverUpdatedAtMs": persisted["serverUpdatedAtMs"],
    }
    for field_name in ("trackId", "volume", "muted"):
        if persisted.get(field_name) is not None:
            correction[field_name] = persisted[field_name]
    return correction


@_serialize_strict_playback_context_mutation
def applyStrictPlaybackUpdate(
    playback_context_id,
    user_name,
    client_id,
    device_session_id,
    connection_nonce,
    payload,
    server_updated_at_ms,
):
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
            current_client_seq = existing.client_seq if same_scope else 0
            incoming_client_seq = payload["clientSeq"]
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
            terminal_control_versions = []

            if origin == "passive":
                applied = payload["appliedControlVersion"]
                if applied > record.control_version:
                    raise ValueError(
                        "appliedControlVersion exceeds canonical controlVersion"
                    )
                if last_applied is not None and applied < last_applied:
                    canonical = _passive_correction_from_device(record, existing)
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
                        "canonicalUpdate": _passive_correction_from_device(
                            record,
                            existing,
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
                terminal_identity = {
                    "status": terminal_status,
                    "errorCode": terminal_error,
                    "appliedControlVersion": applied,
                    "state": payload["state"],
                    "trackId": payload.get("trackId"),
                    "positionMs": payload["positionMs"],
                }
                terminal_fingerprint = _json_fingerprint(terminal_identity)
                if transaction.status != "pending":
                    if transaction.terminal_fingerprint != terminal_fingerprint:
                        raise PlaybackControlTransactionConflictError(
                            "Remote control terminal conflict"
                        )
                    terminal_control_versions.append(command_version)
                else:
                    transaction.status = terminal_status
                    transaction.error_code = terminal_error
                    transaction.applied_control_version = applied
                    transaction.terminal_fingerprint = terminal_fingerprint
                    transaction.terminal_at_ms = server_updated_at_ms
                    transaction.updated_at = now()
                    transaction.save()
                    terminal_control_versions.append(command_version)

                    if terminal_status == "failed":
                        queue = json.loads(record.queue_json)
                        actual_index = (
                            queue.index(payload["trackId"])
                            if payload.get("trackId") in queue
                            else record.current_index
                        )
                        snapshot_changed = (
                            record.state != payload["state"]
                            or record.position_ms != payload["positionMs"]
                            or record.current_index != actual_index
                            or record.track_id != payload.get("trackId")
                        )
                        if snapshot_changed:
                            if record.current_index != actual_index:
                                record.queue_revision += 1
                            record.current_index = actual_index
                            record.track_id = payload.get("trackId")
                            record.state = payload["state"]
                            record.position_ms = payload["positionMs"]
                            record.version += 1
                            record.updated_at = now()
                            record.save()
                            current = _playback_context_payload(record)

                        if transaction.action in {
                            "queue.playItem",
                            "player.next",
                            "player.prev",
                        }:
                            dependent_query = (
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
                                    & (
                                        EmoPlaybackControlTransaction.command_control_version
                                        > command_version
                                    )
                                )
                                .order_by(
                                    EmoPlaybackControlTransaction.command_control_version
                                )
                            )
                            for dependent in dependent_query:
                                dependent.status = "failed"
                                dependent.error_code = "dependency_failed"
                                dependent.depends_on_control_version = command_version
                                dependent.applied_control_version = applied
                                dependent.terminal_fingerprint = _json_fingerprint(
                                    {
                                        "status": "failed",
                                        "errorCode": "dependency_failed",
                                        "dependsOnControlVersion": command_version,
                                        "appliedControlVersion": applied,
                                    }
                                )
                                dependent.terminal_at_ms = server_updated_at_ms
                                dependent.updated_at = now()
                                dependent.save()
                                dependency_records.append(
                                    serializePlaybackControlTransaction(dependent)
                                )
                                terminal_control_versions.append(
                                    dependent.command_control_version
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
                    pending.status = "superseded"
                    pending.applied_control_version = applied
                    pending.terminal_fingerprint = _json_fingerprint(
                        {
                            "status": "superseded",
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
            return {
                "playbackContext": _playback_context_payload(record),
                "deviceState": _device_playback_state_payload(saved_device),
                "canonicalUpdate": canonical,
                "created": True,
                "sourceOnly": False,
                "dependencySettlements": dependency_records,
                "terminalControlVersions": terminal_control_versions,
            }
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
                            if not candidates:
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


@_serialize_strict_playback_context_mutation
def closeStrictPlaybackContextState(
    playback_context_id,
    user_name,
) -> Optional[PlaybackContextCloseResult]:
    open_connection(reuse=True)
    try:
        initial_record = EmoPlaybackContext.get_or_none(
            EmoPlaybackContext.playback_context_id == playback_context_id
        )
        if initial_record is None:
            return None
        if initial_record.user_name != user_name:
            raise PermissionError("Playback context belongs to another user")
        authority_pair = _record_authority_pair(initial_record)
        with _strict_authority_pair_transaction((authority_pair,)):
            record = EmoPlaybackContext.get_or_none(
                EmoPlaybackContext.playback_context_id == playback_context_id
            )
            if record is None:
                return None
            if record.user_name != user_name:
                raise PermissionError("Playback context belongs to another user")
            mutated = record.lifecycle != "closed"
            if mutated:
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
            )
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
            control_changed = (
                index_changed
                or previous_track != next_track
                or record.position_ms != position_ms
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

            record.queue_json = json.dumps(queue_song_ids, ensure_ascii=True)
            record.current_index = next_index or 0
            record.track_id = next_track
            record.position_ms = position_ms
            if not queue_song_ids:
                record.state = "idle"
                record.position_ms = 0
            elif not previous_queue or record.state == "idle":
                record.state = "paused"
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
    requesting_client_id=None,
    authority_client_id=None,
    authority_device_session_id=None,
    routed_connection_nonce=None,
    routed_connection_epoch=1,
    accepted_at_ms=None,
    execution_timeout_ms=None,
):
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
            if action == "queue.playItem":
                if current_index is None or current_index >= len(queue_song_ids):
                    raise ValueError("queue.playItem queueIndex is out of bounds")
            elif action == "player.next":
                current_index = record.current_index + 1
                if current_index >= len(queue_song_ids):
                    raise ValueError("player.next queueIndex is out of bounds")
            elif action == "player.prev":
                if record.current_index <= 0:
                    raise ValueError("player.prev queueIndex is out of bounds")
                current_index = record.current_index - 1
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
            result = _playback_context_payload(record)
            if requesting_client_id is not None:
                if (
                    not authority_client_id
                    or not authority_device_session_id
                    or not routed_connection_nonce
                    or accepted_at_ms is None
                    or execution_timeout_ms is None
                    or execution_timeout_ms < 1
                ):
                    raise ValueError(
                        "Strict control transaction routing fields are required"
                    )
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
                transaction_record = EmoPlaybackControlTransaction.create(
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
                    accepted_target_json=_canonical_json(accepted_target),
                    status="pending",
                    accepted_at_ms=accepted_at_ms,
                    execution_timeout_ms=execution_timeout_ms,
                    watchdog_deadline_at_ms=(
                        accepted_at_ms + execution_timeout_ms + 2000
                    ),
                )
                result["_controlTransaction"] = (
                    serializePlaybackControlTransaction(transaction_record)
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
) -> List[EmoPlaybackContext]:
    return list(
        EmoPlaybackContext.select()
        .where(
            (EmoPlaybackContext.user_name == user_name)
            & (EmoPlaybackContext.lifecycle == "active")
            & (EmoPlaybackContext.authority_client_id == target_client_id)
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
                    existing_snapshot = (
                        json.loads(active_handoff.snapshot_json)
                        if active_handoff.snapshot_json
                        else {}
                    )
                    existing = dict(handoff)
                    existing["snapshot"] = existing_snapshot
                    existing["status"] = active_handoff.status
                    return existing, False
                raise PlaybackHandoffTargetConflictError(
                    "Playback handoff already in progress"
                )

            target_contexts = _active_handoff_target_context_records(
                user_name,
                target_client_id,
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
            record = EmoPlaybackHandoff.create(
                handoff_id=handoff["handoffId"],
                request_id=handoff.get("requestId"),
                playback_context_id=playback_context_id,
                user_name=user_name,
                source_client_id=source_client_id,
                target_client_id=target_client_id,
                origin_client_id=handoff.get("originClientId"),
                status="preparing",
                base_control_version=handoff["baseControlVersion"],
                snapshot_json=json.dumps(snapshot, ensure_ascii=True),
            )
            payload = dict(handoff)
            payload["snapshot"] = snapshot
            payload["status"] = record.status
            return payload, True
    finally:
        close_connection()


def completeStrictPlaybackHandoff(
    playback_context_id: str,
    handoff_id: str,
    user_name: str,
    target_client_id: str,
    target_device_session_id: str,
    position_ms: Optional[int] = None,
) -> Optional[PlaybackHandoffCompleteResult]:
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
        return _completeStrictPlaybackHandoffLocked(
            playback_context_id,
            handoff_id,
            user_name,
            target_client_id,
            target_device_session_id,
            position_ms=position_ms,
        )


def _completeStrictPlaybackHandoffLocked(
    playback_context_id: str,
    handoff_id: str,
    user_name: str,
    target_client_id: str,
    target_device_session_id: str,
    position_ms: Optional[int] = None,
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
                )
                handoff_payload = {
                    "handoffId": handoff_record.handoff_id,
                    "requestId": handoff_record.request_id,
                    "playbackContextId": handoff_record.playback_context_id,
                    "userName": handoff_record.user_name,
                    "sourceClientId": handoff_record.source_client_id,
                    "targetClientId": handoff_record.target_client_id,
                    "originClientId": handoff_record.origin_client_id,
                    "status": handoff_record.status,
                    "baseControlVersion": handoff_record.base_control_version,
                    "controlVersion": snapshot.get("handoffControlVersion"),
                    "prepareId": snapshot.get("prepareId"),
                    "snapshot": snapshot,
                }
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
            if context_record.authority_client_id != handoff_record.source_client_id:
                raise PermissionError("Playback handoff source is no longer authority")
            if context_record.control_version != handoff_record.base_control_version:
                raise PlaybackContextStaleVersionError(
                    _playback_context_payload(context_record),
                    "controlVersion",
                )

            expected_standby_id = snapshot.get(
                "targetStandbyPlaybackContextId"
            )
            target_contexts = _active_handoff_target_context_records(
                user_name,
                target_client_id,
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

            next_control_version = snapshot.get("handoffControlVersion")
            if type(next_control_version) is not int or next_control_version < 1:
                next_control_version = context_record.control_version + 1
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
            handoff_payload = {
                "handoffId": handoff_record.handoff_id,
                "requestId": handoff_record.request_id,
                "playbackContextId": handoff_record.playback_context_id,
                "userName": handoff_record.user_name,
                "sourceClientId": handoff_record.source_client_id,
                "targetClientId": handoff_record.target_client_id,
                "originClientId": handoff_record.origin_client_id,
                "status": handoff_record.status,
                "baseControlVersion": handoff_record.base_control_version,
                "controlVersion": next_control_version,
                "prepareId": snapshot.get("prepareId"),
                "snapshot": snapshot,
            }
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
) -> Optional[Tuple[Dict[str, object], bool]]:
    if status not in ("cancelled", "failed", "timed_out"):
        raise ValueError("Unsupported handoff terminal status")
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
            snapshot = json.loads(record.snapshot_json) if record.snapshot_json else {}
            transitioned = False
            if record.status in ("preparing", "ready", "committed", "committing"):
                record.status = status
                record.error_code = error_code
                record.error_message = error_message
                record.updated_at = now()
                record.save()
                transitioned = True
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
                "controlVersion": snapshot.get("handoffControlVersion"),
                "prepareId": snapshot.get("prepareId"),
                "completeExpiresAtMs": snapshot.get("completeExpiresAtMs"),
                "snapshot": snapshot,
                "errorCode": record.error_code,
                "errorMessage": record.error_message,
            }
            return payload, transitioned
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
            snapshot = json.loads(record.snapshot_json) if record.snapshot_json else {}
            transitioned = False
            if record.status == "preparing":
                record.status = "committed"
                snapshot["completeExpiresAtMs"] = complete_expires_at_ms
                record.snapshot_json = json.dumps(snapshot, ensure_ascii=True)
                record.updated_at = now()
                record.save()
                transitioned = True
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
                "controlVersion": snapshot.get("handoffControlVersion"),
                "prepareId": snapshot.get("prepareId"),
                "completeExpiresAtMs": snapshot.get("completeExpiresAtMs"),
                "snapshot": snapshot,
                "errorCode": record.error_code,
                "errorMessage": record.error_message,
            }
            return payload, transitioned
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
            "contextEpoch": record.context_epoch,
            "appliedControlVersion": record.applied_control_version,
            "clientSeq": record.client_seq,
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
