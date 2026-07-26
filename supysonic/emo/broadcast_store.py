import hashlib
import json
import threading
import time
from contextlib import contextmanager
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from peewee import IntegrityError, SqliteDatabase

from ..db import (
    EmoBroadcast,
    EmoBroadcastDelivery,
    EmoBroadcastFeedbackSettlement,
    EmoBroadcastFence,
    EmoBroadcastIntentOutcome,
    EmoBroadcastParticipant,
    EmoBroadcastRevision,
    EmoBroadcastTerminalRecovery,
    close_connection,
    db,
    now,
    open_connection,
)


MAX_BROADCAST_PARTICIPANTS = 20
MAX_USER_RECOVERY_SLOTS = 256
MIN_RETAINED_BROADCAST_REVISIONS = 512
MAX_CONTEXT_BROADCAST_INTENTS = 1024
BROADCAST_FEEDBACK_WINDOW_MS = 10 * 60 * 1000
BROADCAST_FULL_RETENTION_MS = 7 * 24 * 60 * 60 * 1000


class BroadcastStoreError(Exception):
    pass


class BroadcastNotFoundError(BroadcastStoreError):
    pass


class BroadcastIntentConflictError(BroadcastStoreError):
    def __init__(self, canonical_ack: Dict[str, object]):
        super().__init__("Broadcast intentId was reused with different content")
        self.canonical_ack = canonical_ack


class BroadcastResourceConflictError(BroadcastStoreError):
    pass


class BroadcastRevisionConflictError(BroadcastStoreError):
    def __init__(self, current_revision: int):
        super().__init__("Broadcast revision is stale")
        self.current_revision = current_revision


class BroadcastLimitError(BroadcastStoreError):
    def __init__(self, limit_name: str, limit: int):
        super().__init__("Broadcast %s limit reached" % limit_name)
        self.limit_name = limit_name
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


def _load_json(value: Optional[str], default: object) -> object:
    if value is None:
        return default
    return json.loads(value)


def _resource_key(kind: str, *parts: str) -> str:
    material = _canonical_json([kind] + list(parts)).encode("utf-8")
    return "%s:%s" % (kind, hashlib.sha256(material).hexdigest())


def broadcastContextResourceKey(user_name: str, playback_context_id: str) -> str:
    return _resource_key("context", user_name, playback_context_id)


def broadcastPairResourceKey(
    user_name: str,
    client_id: str,
    device_session_id: str,
) -> str:
    return _resource_key("pair", user_name, client_id, device_session_id)


@contextmanager
def broadcastResourceLock(resource_keys: Iterable[str]):
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
def broadcastTransaction():
    if isinstance(db.obj, SqliteDatabase):
        with db.atomic("IMMEDIATE"):
            yield
        return
    with db.atomic():
        yield


def _intent_expression(
    user_name: str,
    playback_context_id: str,
    owner_client_id: str,
    intent_id: str,
):
    return (
        (EmoBroadcastIntentOutcome.user_name == user_name)
        & (
            EmoBroadcastIntentOutcome.playback_context_id
            == playback_context_id
        )
        & (EmoBroadcastIntentOutcome.owner_client_id == owner_client_id)
        & (EmoBroadcastIntentOutcome.intent_id == intent_id)
    )


def _participant_expression(
    broadcast_id: str,
    client_id: str,
    device_session_id: str,
):
    return (
        (EmoBroadcastParticipant.broadcast_id == broadcast_id)
        & (EmoBroadcastParticipant.client_id == client_id)
        & (EmoBroadcastParticipant.device_session_id == device_session_id)
    )


def _broadcast_resource_keys(broadcast_id: str) -> Tuple[str, ...]:
    keys = {_resource_key("broadcast", broadcast_id)}
    keys.update(
        item.resource_key
        for item in EmoBroadcastFence.select(
            EmoBroadcastFence.resource_key
        ).where(EmoBroadcastFence.broadcast_id == broadcast_id)
    )
    return tuple(sorted(keys))


def _serialize_broadcast_record(record: EmoBroadcast) -> Dict[str, object]:
    return {
        "broadcastId": record.broadcast_id,
        "userName": record.user_name,
        "playbackContextId": record.playback_context_id,
        "intentId": record.intent_id,
        "ownerClientId": record.owner_client_id,
        "authorityClientId": record.authority_client_id,
        "authorityDeviceSessionId": record.authority_device_session_id,
        "lifecycleState": record.lifecycle_state,
        "broadcastRevision": record.broadcast_revision,
        "snapshot": _load_json(record.snapshot_json, {}),
        "authorityDisconnectDeadlineMs": (
            record.authority_disconnect_deadline_ms
        ),
        "terminalAtMs": record.terminal_at_ms,
        "fullExpiresAtMs": record.full_expires_at_ms,
    }


def _serialize_participant_record(
    record: EmoBroadcastParticipant,
) -> Dict[str, object]:
    return {
        "broadcastId": record.broadcast_id,
        "userName": record.user_name,
        "clientId": record.client_id,
        "deviceSessionId": record.device_session_id,
        "suspendedPlaybackContextId": record.suspended_playback_context_id,
        "suspendedEpoch": record.suspended_epoch,
        "suspendedVersion": record.suspended_version,
        "suspendedQueueRevision": record.suspended_queue_revision,
        "suspendedControlVersion": record.suspended_control_version,
        "suspendedAppliedControlVersion": (
            record.suspended_applied_control_version
        ),
        "restorePending": bool(record.restore_pending),
        "terminalConfirmed": bool(record.terminal_confirmed),
        "targetBroadcastRevision": record.target_broadcast_revision,
        "targetDeliveryId": record.target_delivery_id,
        "deadlineBroadcastRevision": record.deadline_broadcast_revision,
        "feedbackDeadlineAtServerMs": (
            record.feedback_deadline_at_server_ms
        ),
        "syncStatus": record.sync_status,
        "appliedBroadcastRevision": record.applied_broadcast_revision,
        "appliedQueueIndex": record.applied_queue_index,
        "appliedTrackId": record.applied_track_id,
        "appliedPositionMs": record.applied_position_ms,
        "appliedState": record.applied_state,
        "appliedPlaybackRate": record.applied_playback_rate,
        "failedBroadcastRevision": record.failed_broadcast_revision,
        "failedLastAppliedBroadcastRevision": (
            record.failed_last_applied_broadcast_revision
        ),
        "failedErrorCode": record.failed_error_code,
        "lastFeedbackClientSeq": record.last_feedback_client_seq,
        "lastFeedbackAtMs": record.last_feedback_at_ms,
        "restoreCompleted": bool(record.restore_completed),
    }


def _serialize_delivery_record(
    record: EmoBroadcastDelivery,
) -> Dict[str, object]:
    return {
        "deliveryId": record.delivery_id,
        "broadcastId": record.broadcast_id,
        "broadcastRevision": record.broadcast_revision,
        "clientId": record.client_id,
        "deviceSessionId": record.device_session_id,
        "action": record.action,
        "effectiveAtServerMs": record.effective_at_server_ms,
        "serverTimeMs": record.server_time_ms,
        "deliveryPositionMs": record.delivery_position_ms,
        "payload": _load_json(record.payload_json, {}),
        "connectionNonce": record.connection_nonce,
        "isCurrent": bool(record.is_current),
        "deliveryStatus": record.delivery_status,
        "createdAtMs": record.created_at_ms,
    }


def _serialize_intent_record(
    record: EmoBroadcastIntentOutcome,
) -> Dict[str, object]:
    return {
        "userName": record.user_name,
        "playbackContextId": record.playback_context_id,
        "ownerClientId": record.owner_client_id,
        "intentId": record.intent_id,
        "requestFingerprint": record.request_fingerprint,
        "broadcastId": record.broadcast_id,
        "finalParticipants": _load_json(
            record.final_participants_json,
            [],
        ),
        "skippedClientIds": _load_json(record.skipped_client_ids_json, []),
        "startAck": _load_json(record.start_ack_json, {}),
        "terminalBroadcastRevision": record.terminal_broadcast_revision,
        "stopAck": _load_json(record.stop_ack_json, None),
    }


def _normalize_participant(
    participant: Dict[str, object],
) -> Dict[str, object]:
    required = (
        "clientId",
        "deviceSessionId",
        "suspendedPlaybackContextId",
        "suspendedEpoch",
        "suspendedVersion",
        "suspendedQueueRevision",
        "suspendedControlVersion",
        "suspendedAppliedControlVersion",
    )
    missing = [name for name in required if participant.get(name) is None]
    if missing:
        raise ValueError("Broadcast participant is missing %s" % missing[0])
    return dict(participant)


def _normalize_delivery(delivery: Dict[str, object]) -> Dict[str, object]:
    required = (
        "deliveryId",
        "clientId",
        "deviceSessionId",
        "action",
        "deliveryPositionMs",
        "payload",
        "createdAtMs",
    )
    missing = [name for name in required if delivery.get(name) is None]
    if missing:
        raise ValueError("Broadcast delivery is missing %s" % missing[0])
    return dict(delivery)


def _create_delivery(
    broadcast_id: str,
    broadcast_revision: int,
    delivery: Dict[str, object],
) -> EmoBroadcastDelivery:
    payload = _normalize_delivery(delivery)
    client_id = str(payload["clientId"])
    device_session_id = str(payload["deviceSessionId"])
    EmoBroadcastDelivery.update(
        is_current=0,
        delivery_status="superseded",
        updated_at=now(),
    ).where(
        (EmoBroadcastDelivery.broadcast_id == broadcast_id)
        & (EmoBroadcastDelivery.broadcast_revision == broadcast_revision)
        & (EmoBroadcastDelivery.client_id == client_id)
        & (EmoBroadcastDelivery.device_session_id == device_session_id)
        & (EmoBroadcastDelivery.is_current == 1)
    ).execute()
    record = EmoBroadcastDelivery.create(
        delivery_id=payload["deliveryId"],
        broadcast_id=broadcast_id,
        broadcast_revision=broadcast_revision,
        client_id=client_id,
        device_session_id=device_session_id,
        action=payload["action"],
        effective_at_server_ms=payload.get("effectiveAtServerMs"),
        server_time_ms=payload.get("serverTimeMs"),
        delivery_position_ms=payload["deliveryPositionMs"],
        payload_json=_canonical_json(payload["payload"]),
        connection_nonce=payload.get("connectionNonce"),
        is_current=1,
        delivery_status=payload.get("deliveryStatus") or "pending",
        created_at_ms=payload["createdAtMs"],
    )
    participant = EmoBroadcastParticipant.get_or_none(
        _participant_expression(
            broadcast_id,
            client_id,
            device_session_id,
        )
    )
    if participant is None:
        raise BroadcastResourceConflictError(
            "Delivery target is not a frozen ordinary participant"
        )
    participant.target_broadcast_revision = broadcast_revision
    participant.target_delivery_id = record.delivery_id
    if payload.get("resetDeadline", True):
        participant.deadline_broadcast_revision = broadcast_revision
        participant.feedback_deadline_at_server_ms = payload.get(
            "feedbackDeadlineAtServerMs"
        )
        participant.sync_status = "pending"
    participant.updated_at = now()
    participant.save()
    return record


def createBroadcastState(
    snapshot: Dict[str, object],
    participants: Sequence[Dict[str, object]],
    request_fingerprint: str,
    start_ack: Dict[str, object],
    skipped_client_ids: Sequence[str] = (),
    initial_deliveries: Sequence[Dict[str, object]] = (),
) -> Dict[str, object]:
    participant_payloads = [_normalize_participant(item) for item in participants]
    if len(participant_payloads) > MAX_BROADCAST_PARTICIPANTS:
        raise BroadcastLimitError(
            "participants",
            MAX_BROADCAST_PARTICIPANTS,
        )
    pairs = {
        (item["clientId"], item["deviceSessionId"])
        for item in participant_payloads
    }
    if len(pairs) != len(participant_payloads):
        raise BroadcastResourceConflictError(
            "Duplicate ordinary participant pair"
        )
    client_ids = {item["clientId"] for item in participant_payloads}
    if len(client_ids) != len(participant_payloads):
        raise BroadcastResourceConflictError(
            "A stable clientId can only select one frozen device pair"
        )

    required = (
        "broadcastId",
        "userName",
        "playbackContextId",
        "intentId",
        "ownerClientId",
        "authorityClientId",
        "authorityDeviceSessionId",
        "lifecycleState",
        "broadcastRevision",
    )
    missing = [name for name in required if snapshot.get(name) is None]
    if missing:
        raise ValueError("Broadcast snapshot is missing %s" % missing[0])
    if snapshot["lifecycleState"] not in ("active", "waitingForSource"):
        raise ValueError("New Broadcast must be nonterminal")

    user_name = str(snapshot["userName"])
    playback_context_id = str(snapshot["playbackContextId"])
    owner_client_id = str(snapshot["ownerClientId"])
    intent_id = str(snapshot["intentId"])
    broadcast_id = str(snapshot["broadcastId"])
    revision = int(snapshot["broadcastRevision"])
    authority_client_id = str(snapshot["authorityClientId"])
    authority_device_session_id = str(
        snapshot["authorityDeviceSessionId"]
    )
    resource_keys = {
        broadcastContextResourceKey(user_name, playback_context_id),
        broadcastPairResourceKey(
            user_name,
            authority_client_id,
            authority_device_session_id,
        ),
    }
    for participant in participant_payloads:
        resource_keys.add(
            broadcastContextResourceKey(
                user_name,
                str(participant["suspendedPlaybackContextId"]),
            )
        )
        resource_keys.add(
            broadcastPairResourceKey(
                user_name,
                str(participant["clientId"]),
                str(participant["deviceSessionId"]),
            )
        )

    open_connection(reuse=True)
    try:
        with broadcastResourceLock(resource_keys):
            try:
                with broadcastTransaction():
                    existing_intent = EmoBroadcastIntentOutcome.get_or_none(
                        _intent_expression(
                            user_name,
                            playback_context_id,
                            owner_client_id,
                            intent_id,
                        )
                    )
                    if existing_intent is not None:
                        if (
                            existing_intent.request_fingerprint
                            != request_fingerprint
                        ):
                            raise BroadcastIntentConflictError(
                                _load_json(existing_intent.start_ack_json, {})
                            )
                        existing = EmoBroadcast.get_or_none(
                            EmoBroadcast.broadcast_id
                            == existing_intent.broadcast_id
                        )
                        return {
                            "created": False,
                            "broadcast": (
                                None
                                if existing is None
                                else _serialize_broadcast_record(existing)
                            ),
                            "intentOutcome": _serialize_intent_record(
                                existing_intent
                            ),
                        }

                    intent_count = (
                        EmoBroadcastIntentOutcome.select()
                        .where(
                            EmoBroadcastIntentOutcome.playback_context_id
                            == playback_context_id
                        )
                        .count()
                    )
                    if intent_count >= MAX_CONTEXT_BROADCAST_INTENTS:
                        raise BroadcastLimitError(
                            "context_intents",
                            MAX_CONTEXT_BROADCAST_INTENTS,
                        )
                    active = EmoBroadcast.get_or_none(
                        (EmoBroadcast.user_name == user_name)
                        & (
                            EmoBroadcast.playback_context_id
                            == playback_context_id
                        )
                        & (EmoBroadcast.lifecycle_state != "stopped")
                    )
                    if active is not None:
                        raise BroadcastResourceConflictError(
                            "Source Context already has a nonterminal Broadcast"
                        )
                    reserved = (
                        EmoBroadcastFence.select()
                        .where(
                            (EmoBroadcastFence.user_name == user_name)
                            & (EmoBroadcastFence.recovery_slot_reserved == 1)
                        )
                        .count()
                    )
                    if (
                        reserved + len(participant_payloads)
                        > MAX_USER_RECOVERY_SLOTS
                    ):
                        raise BroadcastLimitError(
                            "user_recovery_slots",
                            MAX_USER_RECOVERY_SLOTS,
                        )

                    intent = EmoBroadcastIntentOutcome.create(
                        user_name=user_name,
                        playback_context_id=playback_context_id,
                        owner_client_id=owner_client_id,
                        intent_id=intent_id,
                        request_fingerprint=request_fingerprint,
                        broadcast_id=broadcast_id,
                        final_participants_json=_canonical_json(
                            sorted(item["clientId"] for item in participant_payloads)
                        ),
                        skipped_client_ids_json=_canonical_json(
                            sorted(skipped_client_ids)
                        ),
                        start_ack_json=_canonical_json(start_ack),
                    )
                    record = EmoBroadcast.create(
                        broadcast_id=broadcast_id,
                        user_name=user_name,
                        playback_context_id=playback_context_id,
                        intent_id=intent_id,
                        owner_client_id=owner_client_id,
                        authority_client_id=authority_client_id,
                        authority_device_session_id=(
                            authority_device_session_id
                        ),
                        lifecycle_state=snapshot["lifecycleState"],
                        broadcast_revision=revision,
                        snapshot_json=_canonical_json(snapshot),
                        authority_disconnect_deadline_ms=snapshot.get(
                            "authorityDisconnectDeadlineMs"
                        ),
                    )
                    EmoBroadcastRevision.create(
                        broadcast_id=broadcast_id,
                        broadcast_revision=revision,
                        snapshot_json=_canonical_json(snapshot),
                        canonical_action="start",
                        created_at_ms=int(
                            snapshot.get("serverUpdatedAtMs")
                            or time.time() * 1000
                        ),
                    )
                    EmoBroadcastFence.create(
                        resource_key=broadcastContextResourceKey(
                            user_name,
                            playback_context_id,
                        ),
                        broadcast_id=broadcast_id,
                        user_name=user_name,
                        role="source",
                        phase="nonterminal",
                        playback_context_id=playback_context_id,
                    )
                    EmoBroadcastFence.create(
                        resource_key=broadcastPairResourceKey(
                            user_name,
                            authority_client_id,
                            authority_device_session_id,
                        ),
                        broadcast_id=broadcast_id,
                        user_name=user_name,
                        role="source",
                        phase="nonterminal",
                        playback_context_id=playback_context_id,
                        client_id=authority_client_id,
                        device_session_id=authority_device_session_id,
                    )
                    for participant in participant_payloads:
                        client_id = str(participant["clientId"])
                        device_session_id = str(
                            participant["deviceSessionId"]
                        )
                        suspended_context_id = str(
                            participant["suspendedPlaybackContextId"]
                        )
                        EmoBroadcastParticipant.create(
                            broadcast_id=broadcast_id,
                            user_name=user_name,
                            client_id=client_id,
                            device_session_id=device_session_id,
                            suspended_playback_context_id=(
                                suspended_context_id
                            ),
                            suspended_epoch=participant["suspendedEpoch"],
                            suspended_version=participant[
                                "suspendedVersion"
                            ],
                            suspended_queue_revision=participant[
                                "suspendedQueueRevision"
                            ],
                            suspended_control_version=participant[
                                "suspendedControlVersion"
                            ],
                            suspended_applied_control_version=participant[
                                "suspendedAppliedControlVersion"
                            ],
                        )
                        EmoBroadcastFence.create(
                            resource_key=broadcastContextResourceKey(
                                user_name,
                                suspended_context_id,
                            ),
                            broadcast_id=broadcast_id,
                            user_name=user_name,
                            role="ordinary",
                            phase="nonterminal",
                            playback_context_id=suspended_context_id,
                            client_id=client_id,
                            device_session_id=device_session_id,
                            recovery_slot_reserved=0,
                        )
                        EmoBroadcastFence.create(
                            resource_key=broadcastPairResourceKey(
                                user_name,
                                client_id,
                                device_session_id,
                            ),
                            broadcast_id=broadcast_id,
                            user_name=user_name,
                            role="ordinary",
                            phase="nonterminal",
                            playback_context_id=suspended_context_id,
                            client_id=client_id,
                            device_session_id=device_session_id,
                            recovery_slot_reserved=1,
                        )
                    for delivery in initial_deliveries:
                        _create_delivery(broadcast_id, revision, delivery)
                    return {
                        "created": True,
                        "broadcast": _serialize_broadcast_record(record),
                        "intentOutcome": _serialize_intent_record(intent),
                    }
            except IntegrityError as exc:
                raise BroadcastResourceConflictError(
                    "Broadcast resource is already occupied"
                ) from exc
    finally:
        close_connection()


def getBroadcastState(
    broadcast_id: str,
    include_ledger: bool = True,
) -> Optional[Dict[str, object]]:
    open_connection(reuse=True)
    try:
        record = EmoBroadcast.get_or_none(
            EmoBroadcast.broadcast_id == broadcast_id
        )
        if record is None:
            return None
        result = _serialize_broadcast_record(record)
        participants = (
            EmoBroadcastParticipant.select()
            .where(EmoBroadcastParticipant.broadcast_id == broadcast_id)
            .order_by(
                EmoBroadcastParticipant.client_id,
                EmoBroadcastParticipant.device_session_id,
            )
        )
        result["participantStates"] = [
            _serialize_participant_record(item) for item in participants
        ]
        if include_ledger:
            revisions = (
                EmoBroadcastRevision.select()
                .where(EmoBroadcastRevision.broadcast_id == broadcast_id)
                .order_by(EmoBroadcastRevision.broadcast_revision)
            )
            result["revisions"] = [
                {
                    "broadcastRevision": item.broadcast_revision,
                    "snapshot": _load_json(item.snapshot_json, {}),
                    "action": item.canonical_action,
                    "createdAtMs": item.created_at_ms,
                }
                for item in revisions
            ]
            deliveries = (
                EmoBroadcastDelivery.select()
                .where(EmoBroadcastDelivery.broadcast_id == broadcast_id)
                .order_by(
                    EmoBroadcastDelivery.broadcast_revision,
                    EmoBroadcastDelivery.created_at_ms,
                )
            )
            result["deliveries"] = [
                _serialize_delivery_record(item) for item in deliveries
            ]
        return result
    finally:
        close_connection()


def getBroadcastFenceForContext(
    user_name: str,
    playback_context_id: str,
) -> Optional[Dict[str, object]]:
    return _get_broadcast_fence(
        broadcastContextResourceKey(user_name, playback_context_id)
    )


def getBroadcastFenceForPair(
    user_name: str,
    client_id: str,
    device_session_id: str,
) -> Optional[Dict[str, object]]:
    return _get_broadcast_fence(
        broadcastPairResourceKey(user_name, client_id, device_session_id)
    )


def _get_broadcast_fence(
    resource_key: str,
) -> Optional[Dict[str, object]]:
    open_connection(reuse=True)
    try:
        record = EmoBroadcastFence.get_or_none(
            EmoBroadcastFence.resource_key == resource_key
        )
        if record is None:
            return None
        return {
            "resourceKey": record.resource_key,
            "broadcastId": record.broadcast_id,
            "userName": record.user_name,
            "role": record.role,
            "phase": record.phase,
            "playbackContextId": record.playback_context_id,
            "clientId": record.client_id,
            "deviceSessionId": record.device_session_id,
            "recoverySlotReserved": bool(record.recovery_slot_reserved),
        }
    finally:
        close_connection()


def listBroadcastStates(
    user_name: Optional[str] = None,
    lifecycle_states: Optional[Sequence[str]] = None,
) -> List[Dict[str, object]]:
    open_connection(reuse=True)
    try:
        query = EmoBroadcast.select()
        if user_name is not None:
            query = query.where(EmoBroadcast.user_name == user_name)
        if lifecycle_states is not None:
            query = query.where(
                EmoBroadcast.lifecycle_state.in_(tuple(lifecycle_states))
            )
        return [
            _serialize_broadcast_record(item)
            for item in query.order_by(EmoBroadcast.created_at)
        ]
    finally:
        close_connection()


def getBroadcastIntentOutcome(
    user_name: str,
    playback_context_id: str,
    owner_client_id: str,
    intent_id: str,
) -> Optional[Dict[str, object]]:
    open_connection(reuse=True)
    try:
        record = EmoBroadcastIntentOutcome.get_or_none(
            _intent_expression(
                user_name,
                playback_context_id,
                owner_client_id,
                intent_id,
            )
        )
        return None if record is None else _serialize_intent_record(record)
    finally:
        close_connection()


def _prune_revision_ledger(
    broadcast_id: str,
    current_revision: int,
    now_ms: int,
) -> None:
    maximum_prunable_revision = (
        current_revision - MIN_RETAINED_BROADCAST_REVISIONS
    )
    if maximum_prunable_revision < 1:
        return
    cutoff_ms = now_ms - BROADCAST_FEEDBACK_WINDOW_MS
    rows = list(
        EmoBroadcastRevision.select(
            EmoBroadcastRevision.broadcast_revision
        ).where(
            (EmoBroadcastRevision.broadcast_id == broadcast_id)
            & (
                EmoBroadcastRevision.broadcast_revision
                <= maximum_prunable_revision
            )
            & (EmoBroadcastRevision.created_at_ms < cutoff_ms)
        )
    )
    revisions = [item.broadcast_revision for item in rows]
    if not revisions:
        return
    EmoBroadcastDelivery.delete().where(
        (EmoBroadcastDelivery.broadcast_id == broadcast_id)
        & (EmoBroadcastDelivery.broadcast_revision.in_(revisions))
    ).execute()
    EmoBroadcastRevision.delete().where(
        (EmoBroadcastRevision.broadcast_id == broadcast_id)
        & (EmoBroadcastRevision.broadcast_revision.in_(revisions))
    ).execute()


def commitBroadcastRevision(
    broadcast_id: str,
    expected_broadcast_revision: int,
    snapshot: Dict[str, object],
    canonical_action: str,
    deliveries: Sequence[Dict[str, object]] = (),
    created_at_ms: Optional[int] = None,
) -> Dict[str, object]:
    open_connection(reuse=True)
    try:
        resource_keys = _broadcast_resource_keys(broadcast_id)
        with broadcastResourceLock(resource_keys):
            with broadcastTransaction():
                record = EmoBroadcast.get_or_none(
                    EmoBroadcast.broadcast_id == broadcast_id
                )
                if record is None:
                    raise BroadcastNotFoundError(broadcast_id)
                if record.broadcast_revision != expected_broadcast_revision:
                    raise BroadcastRevisionConflictError(
                        record.broadcast_revision
                    )
                if record.lifecycle_state == "stopped":
                    raise BroadcastResourceConflictError(
                        "Terminal Broadcast cannot advance"
                    )
                next_revision = expected_broadcast_revision + 1
                if (
                    snapshot.get("broadcastId") != broadcast_id
                    or snapshot.get("broadcastRevision") != next_revision
                ):
                    raise ValueError(
                        "Snapshot must carry the next Broadcast revision"
                    )
                lifecycle_state = snapshot.get("lifecycleState")
                if lifecycle_state not in ("active", "waitingForSource"):
                    raise ValueError(
                        "Nonterminal revision has invalid lifecycleState"
                    )
                ledger_time_ms = int(
                    created_at_ms
                    if created_at_ms is not None
                    else snapshot.get("serverUpdatedAtMs")
                    or time.time() * 1000
                )
                EmoBroadcastRevision.create(
                    broadcast_id=broadcast_id,
                    broadcast_revision=next_revision,
                    snapshot_json=_canonical_json(snapshot),
                    canonical_action=canonical_action,
                    created_at_ms=ledger_time_ms,
                )
                for delivery in deliveries:
                    _create_delivery(
                        broadcast_id,
                        next_revision,
                        delivery,
                    )
                record.lifecycle_state = lifecycle_state
                record.broadcast_revision = next_revision
                record.snapshot_json = _canonical_json(snapshot)
                record.authority_disconnect_deadline_ms = snapshot.get(
                    "authorityDisconnectDeadlineMs"
                )
                record.updated_at = now()
                record.save()
                _prune_revision_ledger(
                    broadcast_id,
                    next_revision,
                    ledger_time_ms,
                )
                return _serialize_broadcast_record(record)
    finally:
        close_connection()


def terminalBroadcastState(
    broadcast_id: str,
    snapshot: Dict[str, object],
    stop_ack: Dict[str, object],
    terminal_deliveries: Sequence[Dict[str, object]] = (),
    expected_broadcast_revision: Optional[int] = None,
    terminal_at_ms: Optional[int] = None,
) -> Dict[str, object]:
    open_connection(reuse=True)
    try:
        resource_keys = _broadcast_resource_keys(broadcast_id)
        with broadcastResourceLock(resource_keys):
            with broadcastTransaction():
                record = EmoBroadcast.get_or_none(
                    EmoBroadcast.broadcast_id == broadcast_id
                )
                if record is None:
                    raise BroadcastNotFoundError(broadcast_id)
                intent = EmoBroadcastIntentOutcome.get(
                    EmoBroadcastIntentOutcome.broadcast_id == broadcast_id
                )
                if record.lifecycle_state == "stopped":
                    return {
                        "created": False,
                        "broadcast": _serialize_broadcast_record(record),
                        "stopAck": _load_json(intent.stop_ack_json, {}),
                    }
                if (
                    expected_broadcast_revision is not None
                    and record.broadcast_revision
                    != expected_broadcast_revision
                ):
                    raise BroadcastRevisionConflictError(
                        record.broadcast_revision
                    )
                next_revision = record.broadcast_revision + 1
                if (
                    snapshot.get("broadcastId") != broadcast_id
                    or snapshot.get("broadcastRevision") != next_revision
                    or snapshot.get("lifecycleState") != "stopped"
                ):
                    raise ValueError(
                        "Terminal snapshot must carry stopped and next revision"
                    )
                committed_at_ms = int(
                    terminal_at_ms
                    if terminal_at_ms is not None
                    else time.time() * 1000
                )
                EmoBroadcastRevision.create(
                    broadcast_id=broadcast_id,
                    broadcast_revision=next_revision,
                    snapshot_json=_canonical_json(snapshot),
                    canonical_action="stop",
                    created_at_ms=committed_at_ms,
                )
                for delivery in terminal_deliveries:
                    _create_delivery(
                        broadcast_id,
                        next_revision,
                        delivery,
                    )
                EmoBroadcastFence.delete().where(
                    (EmoBroadcastFence.broadcast_id == broadcast_id)
                    & (EmoBroadcastFence.role == "source")
                ).execute()
                EmoBroadcastFence.update(
                    phase="restorePending",
                    updated_at=now(),
                ).where(
                    (EmoBroadcastFence.broadcast_id == broadcast_id)
                    & (EmoBroadcastFence.role == "ordinary")
                ).execute()
                EmoBroadcastParticipant.update(
                    restore_pending=1,
                    terminal_confirmed=0,
                    updated_at=now(),
                ).where(
                    EmoBroadcastParticipant.broadcast_id == broadcast_id
                ).execute()
                record.lifecycle_state = "stopped"
                record.broadcast_revision = next_revision
                record.snapshot_json = _canonical_json(snapshot)
                record.authority_disconnect_deadline_ms = None
                record.terminal_at_ms = committed_at_ms
                record.full_expires_at_ms = (
                    committed_at_ms + BROADCAST_FULL_RETENTION_MS
                )
                record.updated_at = now()
                record.save()
                intent.terminal_broadcast_revision = next_revision
                intent.stop_ack_json = _canonical_json(stop_ack)
                intent.updated_at = now()
                intent.save()
                return {
                    "created": True,
                    "broadcast": _serialize_broadcast_record(record),
                    "stopAck": dict(stop_ack),
                }
    finally:
        close_connection()


def saveBroadcastFeedbackSettlement(
    playback_context_id: str,
    broadcast_id: str,
    client_id: str,
    device_session_id: str,
    connection_nonce: str,
    connection_epoch: int,
    client_seq: int,
    request_fingerprint: str,
    canonical_result: Dict[str, object],
    follow_up_delivery_id: Optional[str] = None,
    created_at_ms: Optional[int] = None,
) -> Dict[str, object]:
    scope = (
        (EmoBroadcastFeedbackSettlement.playback_context_id == playback_context_id)
        & (EmoBroadcastFeedbackSettlement.broadcast_id == broadcast_id)
        & (EmoBroadcastFeedbackSettlement.client_id == client_id)
        & (EmoBroadcastFeedbackSettlement.device_session_id == device_session_id)
        & (EmoBroadcastFeedbackSettlement.connection_nonce == connection_nonce)
        & (EmoBroadcastFeedbackSettlement.connection_epoch == connection_epoch)
    )
    open_connection(reuse=True)
    try:
        with broadcastResourceLock(
            (broadcastPairResourceKey("feedback:" + broadcast_id, client_id, device_session_id),)
        ):
            with broadcastTransaction():
                existing = EmoBroadcastFeedbackSettlement.get_or_none(
                    scope
                    & (EmoBroadcastFeedbackSettlement.client_seq == client_seq)
                )
                if existing is not None:
                    if existing.request_fingerprint != request_fingerprint:
                        raise BroadcastResourceConflictError(
                            "broadcast.feedback clientSeq conflicts"
                        )
                    return {
                        "created": False,
                        "canonicalResult": _load_json(
                            existing.canonical_result_json,
                            {},
                        ),
                        "followUpDeliveryId": existing.follow_up_delivery_id,
                    }
                latest = (
                    EmoBroadcastFeedbackSettlement.select()
                    .where(scope)
                    .order_by(EmoBroadcastFeedbackSettlement.client_seq.desc())
                    .first()
                )
                if latest is not None and client_seq <= latest.client_seq:
                    raise BroadcastResourceConflictError(
                        "broadcast.feedback clientSeq is stale"
                    )
                record = EmoBroadcastFeedbackSettlement.create(
                    playback_context_id=playback_context_id,
                    broadcast_id=broadcast_id,
                    client_id=client_id,
                    device_session_id=device_session_id,
                    connection_nonce=connection_nonce,
                    connection_epoch=connection_epoch,
                    client_seq=client_seq,
                    request_fingerprint=request_fingerprint,
                    canonical_result_json=_canonical_json(canonical_result),
                    follow_up_delivery_id=follow_up_delivery_id,
                    created_at_ms=int(
                        created_at_ms
                        if created_at_ms is not None
                        else time.time() * 1000
                    ),
                )
                return {
                    "created": True,
                    "canonicalResult": dict(canonical_result),
                    "followUpDeliveryId": record.follow_up_delivery_id,
                }
    finally:
        close_connection()


def confirmBroadcastRestore(
    broadcast_id: str,
    user_name: str,
    client_id: str,
    device_session_id: str,
    terminal_broadcast_revision: int,
    delivery_id: Optional[str],
) -> bool:
    resource_key = broadcastPairResourceKey(
        user_name,
        client_id,
        device_session_id,
    )
    open_connection(reuse=True)
    try:
        with broadcastResourceLock((resource_key,)):
            with broadcastTransaction():
                participant = EmoBroadcastParticipant.get_or_none(
                    _participant_expression(
                        broadcast_id,
                        client_id,
                        device_session_id,
                    )
                )
                recovery = EmoBroadcastTerminalRecovery.get_or_none(
                    (EmoBroadcastTerminalRecovery.broadcast_id == broadcast_id)
                    & (EmoBroadcastTerminalRecovery.user_name == user_name)
                    & (EmoBroadcastTerminalRecovery.client_id == client_id)
                    & (
                        EmoBroadcastTerminalRecovery.device_session_id
                        == device_session_id
                    )
                )
                expected_revision = (
                    participant.target_broadcast_revision
                    if participant is not None
                    else (
                        None
                        if recovery is None
                        else recovery.terminal_broadcast_revision
                    )
                )
                expected_delivery = (
                    participant.target_delivery_id
                    if participant is not None
                    else (
                        None
                        if recovery is None
                        else recovery.current_delivery_id
                    )
                )
                if expected_revision is None:
                    return False
                if expected_revision != terminal_broadcast_revision:
                    raise BroadcastRevisionConflictError(expected_revision)
                if expected_delivery is not None and expected_delivery != delivery_id:
                    raise BroadcastResourceConflictError(
                        "Terminal deliveryId does not match current ledger"
                    )
                if participant is not None:
                    participant.restore_pending = 0
                    participant.terminal_confirmed = 1
                    participant.restore_completed = 1
                    participant.sync_status = "applied"
                    participant.updated_at = now()
                    participant.save()
                if delivery_id is not None:
                    EmoBroadcastDelivery.update(
                        delivery_status="settled",
                        updated_at=now(),
                    ).where(
                        EmoBroadcastDelivery.delivery_id == delivery_id
                    ).execute()
                EmoBroadcastFence.delete().where(
                    (EmoBroadcastFence.broadcast_id == broadcast_id)
                    & (EmoBroadcastFence.role == "ordinary")
                    & (EmoBroadcastFence.client_id == client_id)
                    & (EmoBroadcastFence.device_session_id == device_session_id)
                ).execute()
                if recovery is not None:
                    recovery.delete_instance()
                return True
    finally:
        close_connection()


def compactExpiredBroadcastStates(
    now_ms: Optional[int] = None,
    limit: int = 100,
) -> List[str]:
    current_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    open_connection(reuse=True)
    try:
        candidates = list(
            EmoBroadcast.select(EmoBroadcast.broadcast_id)
            .where(
                (EmoBroadcast.lifecycle_state == "stopped")
                & (EmoBroadcast.full_expires_at_ms <= current_ms)
            )
            .order_by(EmoBroadcast.full_expires_at_ms)
            .limit(limit)
        )
        compacted = []
        for candidate in candidates:
            resource_keys = _broadcast_resource_keys(candidate.broadcast_id)
            with broadcastResourceLock(resource_keys):
                with broadcastTransaction():
                    record = EmoBroadcast.get_or_none(
                        (EmoBroadcast.broadcast_id == candidate.broadcast_id)
                        & (EmoBroadcast.lifecycle_state == "stopped")
                        & (EmoBroadcast.full_expires_at_ms <= current_ms)
                    )
                    if record is None:
                        continue
                    snapshot = _load_json(record.snapshot_json, {})
                    participants = list(
                        EmoBroadcastParticipant.select().where(
                            (EmoBroadcastParticipant.broadcast_id == record.broadcast_id)
                            & (EmoBroadcastParticipant.restore_pending == 1)
                            & (EmoBroadcastParticipant.terminal_confirmed == 0)
                        )
                    )
                    for participant in participants:
                        existing_recovery = (
                            EmoBroadcastTerminalRecovery.get_or_none(
                                (
                                    EmoBroadcastTerminalRecovery.user_name
                                    == record.user_name
                                )
                                & (
                                    EmoBroadcastTerminalRecovery.client_id
                                    == participant.client_id
                                )
                                & (
                                    EmoBroadcastTerminalRecovery.device_session_id
                                    == participant.device_session_id
                                )
                            )
                        )
                        if (
                            existing_recovery is not None
                            and existing_recovery.broadcast_id
                            != record.broadcast_id
                        ):
                            raise BroadcastResourceConflictError(
                                "Recovery pair is occupied by another Broadcast"
                            )
                        recovery_values = {
                            "user_name": record.user_name,
                            "playback_context_id": record.playback_context_id,
                            "broadcast_id": record.broadcast_id,
                            "client_id": participant.client_id,
                            "device_session_id": participant.device_session_id,
                            "terminal_broadcast_revision": (
                                record.broadcast_revision
                            ),
                            "current_delivery_id": (
                                participant.target_delivery_id
                            ),
                            "suspended_playback_context_id": (
                                participant.suspended_playback_context_id
                            ),
                            "suspended_epoch": participant.suspended_epoch,
                            "suspended_version": participant.suspended_version,
                            "suspended_queue_revision": (
                                participant.suspended_queue_revision
                            ),
                            "suspended_control_version": (
                                participant.suspended_control_version
                            ),
                            "suspended_applied_control_version": (
                                participant.suspended_applied_control_version
                            ),
                            "last_applied_broadcast_revision": (
                                participant.applied_broadcast_revision
                            ),
                            "terminal_queue_index": snapshot["currentIndex"],
                            "terminal_track_id": snapshot["trackId"],
                            "terminal_position_ms": snapshot["positionMs"],
                            "terminal_playback_rate": snapshot["playbackRate"],
                            "terminal_at_server_ms": (
                                record.terminal_at_ms
                                or snapshot["serverUpdatedAtMs"]
                            ),
                        }
                        if existing_recovery is None:
                            EmoBroadcastTerminalRecovery.create(
                                **recovery_values
                            )
                        else:
                            for key, value in recovery_values.items():
                                setattr(existing_recovery, key, value)
                            existing_recovery.updated_at = now()
                            existing_recovery.save()
                    EmoBroadcastDelivery.delete().where(
                        EmoBroadcastDelivery.broadcast_id == record.broadcast_id
                    ).execute()
                    EmoBroadcastRevision.delete().where(
                        EmoBroadcastRevision.broadcast_id == record.broadcast_id
                    ).execute()
                    EmoBroadcastFeedbackSettlement.delete().where(
                        EmoBroadcastFeedbackSettlement.broadcast_id
                        == record.broadcast_id
                    ).execute()
                    EmoBroadcastParticipant.delete().where(
                        EmoBroadcastParticipant.broadcast_id == record.broadcast_id
                    ).execute()
                    EmoBroadcastFence.delete().where(
                        (EmoBroadcastFence.broadcast_id == record.broadcast_id)
                        & (EmoBroadcastFence.phase != "restorePending")
                    ).execute()
                    record.delete_instance()
                    compacted.append(candidate.broadcast_id)
        return compacted
    finally:
        close_connection()


def listTerminalRecoveries(
    user_name: str,
    client_id: Optional[str] = None,
    device_session_id: Optional[str] = None,
) -> List[Dict[str, object]]:
    open_connection(reuse=True)
    try:
        query = EmoBroadcastTerminalRecovery.select().where(
            EmoBroadcastTerminalRecovery.user_name == user_name
        )
        if client_id is not None:
            query = query.where(
                EmoBroadcastTerminalRecovery.client_id == client_id
            )
        if device_session_id is not None:
            query = query.where(
                EmoBroadcastTerminalRecovery.device_session_id
                == device_session_id
            )
        return [
            {
                "userName": item.user_name,
                "playbackContextId": item.playback_context_id,
                "broadcastId": item.broadcast_id,
                "clientId": item.client_id,
                "deviceSessionId": item.device_session_id,
                "terminalBroadcastRevision": (
                    item.terminal_broadcast_revision
                ),
                "currentDeliveryId": item.current_delivery_id,
                "suspendedPlaybackContextId": (
                    item.suspended_playback_context_id
                ),
                "suspendedEpoch": item.suspended_epoch,
                "suspendedVersion": item.suspended_version,
                "suspendedQueueRevision": item.suspended_queue_revision,
                "suspendedControlVersion": item.suspended_control_version,
                "suspendedAppliedControlVersion": (
                    item.suspended_applied_control_version
                ),
                "lastAppliedBroadcastRevision": (
                    item.last_applied_broadcast_revision
                ),
                "terminalQueueIndex": item.terminal_queue_index,
                "terminalTrackId": item.terminal_track_id,
                "terminalPositionMs": item.terminal_position_ms,
                "terminalPlaybackRate": item.terminal_playback_rate,
                "terminalAtServerMs": item.terminal_at_server_ms,
            }
            for item in query.order_by(
                EmoBroadcastTerminalRecovery.terminal_at_server_ms
            )
        ]
    finally:
        close_connection()
