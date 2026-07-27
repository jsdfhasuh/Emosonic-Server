import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from peewee import IntegrityError, SqliteDatabase, fn

from ..db import (
    EmoBroadcast,
    EmoBroadcastDelivery,
    EmoBroadcastFeedbackSettlement,
    EmoBroadcastFence,
    EmoBroadcastIntentOutcome,
    EmoBroadcastParticipant,
    EmoBroadcastRevision,
    EmoBroadcastTerminalRecovery,
    EmoDevicePlaybackState,
    EmoPlaybackHandoff,
    EmoPlaybackControlTransaction,
    EmoPlaybackPrepareTransaction,
    EmoPlaybackContext,
    close_connection,
    db,
    now,
    open_connection,
)
from .ws_store import strictAuthorityPairLockSet, strictPlaybackContextLockSet
from .strict_v2_effective_at import projectBroadcastPositionMs


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


class BroadcastFeedbackSequenceConflictError(BroadcastStoreError):
    def __init__(self, current_seq: int):
        super().__init__("broadcast.feedback clientSeq conflicts")
        self.current_seq = current_seq


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


def broadcastUserRecoveryResourceKey(user_name: str) -> str:
    return _resource_key("recovery-slots", user_name)


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
        "appliedAtServerMs": record.applied_at_server_ms,
        "failedBroadcastRevision": record.failed_broadcast_revision,
        "failedLastAppliedBroadcastRevision": (
            record.failed_last_applied_broadcast_revision
        ),
        "failedErrorCode": record.failed_error_code,
        "failedErrorMessage": record.failed_error_message,
        "timedOutBroadcastRevision": record.timed_out_broadcast_revision,
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
        "authorityClientId": record.authority_client_id,
        "authorityDeviceSessionId": record.authority_device_session_id,
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


def _validate_start_source_records(
    snapshot: Dict[str, object],
    source_device_state: Dict[str, object],
) -> None:
    context = EmoPlaybackContext.get_or_none(
        EmoPlaybackContext.playback_context_id
        == snapshot["playbackContextId"]
    )
    if context is None or context.user_name != snapshot["userName"]:
        raise BroadcastResourceConflictError(
            "Source PlaybackContext changed before Broadcast start"
        )
    expected_context = {
        "authority_client_id": snapshot["authorityClientId"],
        "authority_device_session_id": snapshot[
            "authorityDeviceSessionId"
        ],
        "lifecycle": "active",
        "version": snapshot["sourceVersion"],
        "queue_revision": snapshot["sourceQueueRevision"],
        "control_version": snapshot["sourceControlVersion"],
        "epoch": snapshot["sourceEpoch"],
        "current_index": snapshot["currentIndex"],
        "track_id": snapshot["trackId"],
    }
    if any(
        getattr(context, field_name) != expected
        for field_name, expected in expected_context.items()
    ) or json.loads(context.queue_json) != snapshot["queueSongIds"]:
        raise BroadcastResourceConflictError(
            "Source PlaybackContext changed before Broadcast start"
        )
    device = EmoDevicePlaybackState.get_or_none(
        (
            EmoDevicePlaybackState.playback_context_id
            == snapshot["playbackContextId"]
        )
        & (
            EmoDevicePlaybackState.owner_client_id
            == snapshot["authorityClientId"]
        )
    )
    if device is None:
        raise BroadcastResourceConflictError(
            "Source DevicePlaybackState is no longer available"
        )
    persisted = json.loads(device.playback_json) if device.playback_json else {}
    current_time_ms = int(time.time() * 1000)
    for field_name in ("serverUpdatedAtMs", "positionSampledAtServerMs"):
        timestamp_ms = persisted.get(field_name)
        if (
            not isinstance(timestamp_ms, int)
            or timestamp_ms > current_time_ms + 50
            or current_time_ms - timestamp_ms > 2000
        ):
            raise BroadcastResourceConflictError(
                "Source DevicePlaybackState is no longer fresh"
            )
    if (
        EmoPlaybackControlTransaction.select()
        .where(
            (
                EmoPlaybackControlTransaction.playback_context_id
                == snapshot["playbackContextId"]
            )
            & (EmoPlaybackControlTransaction.epoch == snapshot["sourceEpoch"])
            & (EmoPlaybackControlTransaction.status == "pending")
        )
        .exists()
    ):
        raise BroadcastResourceConflictError(
            "Source PlaybackContext has unsettled controls"
        )
    comparisons = {
        "deviceSessionId": device.device_session_id,
        "contextEpoch": device.context_epoch,
        "appliedControlVersion": device.applied_control_version,
        "state": device.state,
        "trackId": device.track_id,
        "positionMs": device.position_ms,
        "positionSampledAtServerMs": persisted.get(
            "positionSampledAtServerMs"
        ),
        "serverUpdatedAtMs": persisted.get("serverUpdatedAtMs"),
        "playbackRate": persisted.get("playbackRate"),
    }
    if any(
        source_device_state.get(field_name) != actual
        for field_name, actual in comparisons.items()
    ):
        raise BroadcastResourceConflictError(
            "Source DevicePlaybackState changed before Broadcast start"
        )


def _validate_start_participant_records(
    user_name: str,
    participants: Sequence[Dict[str, object]],
) -> None:
    for participant in participants:
        context = EmoPlaybackContext.get_or_none(
            EmoPlaybackContext.playback_context_id
            == participant["suspendedPlaybackContextId"]
        )
        if (
            context is None
            or context.user_name != user_name
            or context.lifecycle != "active"
            or context.authority_client_id != participant["clientId"]
            or context.authority_device_session_id
            != participant["deviceSessionId"]
            or context.epoch != participant["suspendedEpoch"]
            or context.version != participant["suspendedVersion"]
            or context.queue_revision
            != participant["suspendedQueueRevision"]
            or context.control_version
            != participant["suspendedControlVersion"]
        ):
            raise BroadcastResourceConflictError(
                "Ordinary participant Context changed before Broadcast start"
            )
        device = EmoDevicePlaybackState.get_or_none(
            (
                EmoDevicePlaybackState.playback_context_id
                == participant["suspendedPlaybackContextId"]
            )
            & (
                EmoDevicePlaybackState.owner_client_id
                == participant["clientId"]
            )
        )
        applied = (
            0
            if device is None or device.context_epoch != context.epoch
            else device.applied_control_version
        )
        if applied != participant["suspendedAppliedControlVersion"]:
            raise BroadcastResourceConflictError(
                "Ordinary participant applied cursor changed before start"
            )


def _start_participant_is_available(
    user_name: str,
    participant: Dict[str, object],
) -> bool:
    context_id = str(participant["suspendedPlaybackContextId"])
    client_id = str(participant["clientId"])
    context_key = broadcastContextResourceKey(user_name, context_id)
    pair_key = broadcastPairResourceKey(
        user_name,
        client_id,
        str(participant["deviceSessionId"]),
    )
    if (
        EmoBroadcastFence.select()
        .where(EmoBroadcastFence.resource_key.in_((context_key, pair_key)))
        .exists()
    ):
        return False
    if (
        EmoPlaybackPrepareTransaction.select()
        .where(
            (EmoPlaybackPrepareTransaction.playback_context_id == context_id)
            & (EmoPlaybackPrepareTransaction.status == "preparing")
        )
        .exists()
    ):
        return False
    return not (
        EmoPlaybackHandoff.select()
        .where(
            (EmoPlaybackHandoff.user_name == user_name)
            & EmoPlaybackHandoff.status.in_(
                ("preparing", "ready", "committed", "committing")
            )
            & (
                (EmoPlaybackHandoff.playback_context_id == context_id)
                | (EmoPlaybackHandoff.target_client_id == client_id)
            )
        )
        .exists()
    )


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
    reset_deadline = payload.get("resetDeadline")
    if reset_deadline is None:
        reset_deadline = (
            participant.deadline_broadcast_revision is None
            or participant.sync_status in ("applied", "failed")
        )
    if reset_deadline:
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
    source_device_state: Optional[Dict[str, object]] = None,
    skip_unavailable_participants: bool = False,
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
    snapshot_payload = dict(snapshot)
    start_ack_payload = dict(start_ack)
    skipped_ids = set(skipped_client_ids)
    resource_keys = {
        broadcastContextResourceKey(user_name, playback_context_id),
        broadcastPairResourceKey(
            user_name,
            authority_client_id,
            authority_device_session_id,
        ),
        broadcastUserRecoveryResourceKey(user_name),
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

    context_ids = [playback_context_id]
    context_ids.extend(
        str(item["suspendedPlaybackContextId"])
        for item in participant_payloads
    )
    authority_pairs = [
        (user_name, authority_client_id, authority_device_session_id)
    ]
    authority_pairs.extend(
        (
            user_name,
            str(item["clientId"]),
            str(item["deviceSessionId"]),
        )
        for item in participant_payloads
    )

    open_connection(reuse=True)
    try:
        with strictPlaybackContextLockSet(context_ids), strictAuthorityPairLockSet(
            authority_pairs
        ), broadcastResourceLock(resource_keys):
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
                    selected_participants = list(participant_payloads)
                    if skip_unavailable_participants:
                        selected_participants = []
                        for participant in participant_payloads:
                            if _start_participant_is_available(
                                user_name,
                                participant,
                            ):
                                selected_participants.append(participant)
                            else:
                                skipped_ids.add(str(participant["clientId"]))
                    if source_device_state is not None:
                        _validate_start_source_records(
                            snapshot,
                            source_device_state,
                        )
                        _validate_start_participant_records(
                            user_name,
                            selected_participants,
                        )
                    reserved = (
                        EmoBroadcastFence.select()
                        .where(
                            (EmoBroadcastFence.user_name == user_name)
                            & (EmoBroadcastFence.recovery_slot_reserved == 1)
                        )
                        .count()
                    )
                    available_slots = max(0, MAX_USER_RECOVERY_SLOTS - reserved)
                    if len(selected_participants) > available_slots:
                        if not skip_unavailable_participants:
                            raise BroadcastLimitError(
                                "user_recovery_slots",
                                MAX_USER_RECOVERY_SLOTS,
                            )
                        for participant in selected_participants[available_slots:]:
                            skipped_ids.add(str(participant["clientId"]))
                        selected_participants = selected_participants[:available_slots]
                    if not selected_participants:
                        if participant_payloads and available_slots == 0:
                            raise BroadcastLimitError(
                                "user_recovery_slots",
                                MAX_USER_RECOVERY_SLOTS,
                            )
                        raise ValueError(
                            "Broadcast start requires at least one eligible ordinary participant"
                        )

                    participant_payloads = selected_participants
                    selected_client_ids = sorted(
                        str(item["clientId"]) for item in participant_payloads
                    )
                    snapshot_payload["participants"] = selected_client_ids
                    if "participants" in start_ack_payload:
                        start_ack_payload["participants"] = selected_client_ids
                    if "skippedClientIds" in start_ack_payload:
                        start_ack_payload["skippedClientIds"] = sorted(skipped_ids)
                    selected_pairs = {
                        (str(item["clientId"]), str(item["deviceSessionId"]))
                        for item in participant_payloads
                    }
                    selected_deliveries = []
                    for delivery in initial_deliveries:
                        pair = (
                            str(delivery["clientId"]),
                            str(delivery["deviceSessionId"]),
                        )
                        if pair not in selected_pairs:
                            continue
                        selected_delivery = dict(delivery)
                        selected_payload = dict(selected_delivery["payload"])
                        selected_payload["participants"] = selected_client_ids
                        selected_delivery["payload"] = selected_payload
                        selected_deliveries.append(selected_delivery)
                    stored_snapshot = dict(snapshot_payload)
                    stored_snapshot.pop("userName", None)

                    intent = EmoBroadcastIntentOutcome.create(
                        user_name=user_name,
                        playback_context_id=playback_context_id,
                        owner_client_id=owner_client_id,
                        authority_client_id=authority_client_id,
                        authority_device_session_id=(
                            authority_device_session_id
                        ),
                        intent_id=intent_id,
                        request_fingerprint=request_fingerprint,
                        broadcast_id=broadcast_id,
                        final_participants_json=_canonical_json(
                            selected_client_ids
                        ),
                        skipped_client_ids_json=_canonical_json(
                            sorted(skipped_ids)
                        ),
                        start_ack_json=_canonical_json(start_ack_payload),
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
                        lifecycle_state=snapshot_payload["lifecycleState"],
                        broadcast_revision=revision,
                        snapshot_json=_canonical_json(stored_snapshot),
                        authority_disconnect_deadline_ms=snapshot_payload.get(
                            "authorityDisconnectDeadlineMs"
                        ),
                    )
                    EmoBroadcastRevision.create(
                        broadcast_id=broadcast_id,
                        broadcast_revision=revision,
                        snapshot_json=_canonical_json(stored_snapshot),
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
                    for delivery in selected_deliveries:
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


def getNonterminalBroadcastStateForContext(
    user_name: str,
    playback_context_id: str,
) -> Optional[Dict[str, object]]:
    open_connection(reuse=True)
    try:
        record = (
            EmoBroadcast.select(EmoBroadcast.broadcast_id)
            .where(
                (EmoBroadcast.user_name == user_name)
                & (
                    EmoBroadcast.playback_context_id
                    == playback_context_id
                )
                & (EmoBroadcast.lifecycle_state != "stopped")
            )
            .order_by(EmoBroadcast.created_at.desc())
            .first()
        )
        broadcast_id = None if record is None else record.broadcast_id
    finally:
        close_connection()
    if broadcast_id is None:
        return None
    return getBroadcastState(broadcast_id)


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


def getBroadcastStopOutcome(
    user_name: str,
    playback_context_id: str,
    broadcast_id: str,
) -> Optional[Dict[str, object]]:
    open_connection(reuse=True)
    try:
        record = EmoBroadcastIntentOutcome.get_or_none(
            (EmoBroadcastIntentOutcome.user_name == user_name)
            & (
                EmoBroadcastIntentOutcome.playback_context_id
                == playback_context_id
            )
            & (EmoBroadcastIntentOutcome.broadcast_id == broadcast_id)
        )
        if (
            record is None
            or record.terminal_broadcast_revision is None
            or record.stop_ack_json is None
        ):
            return None
        return _serialize_intent_record(record)
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


@contextmanager
def broadcastMutationLock(broadcast_id: str) -> Iterator[None]:
    open_connection(reuse=True)
    try:
        resource_keys = _broadcast_resource_keys(broadcast_id)
        fence_rows = list(
            EmoBroadcastFence.select().where(
                EmoBroadcastFence.broadcast_id == broadcast_id
            )
        )
    finally:
        close_connection()
    context_ids = [
        row.playback_context_id
        for row in fence_rows
        if row.playback_context_id
    ]
    authority_pairs = [
        (row.user_name, row.client_id, row.device_session_id)
        for row in fence_rows
        if row.client_id and row.device_session_id
    ]
    with strictPlaybackContextLockSet(context_ids), strictAuthorityPairLockSet(
        authority_pairs
    ), broadcastResourceLock(resource_keys):
        yield


def commitBroadcastRevisionInTransaction(
    broadcast_id: str,
    expected_broadcast_revision: int,
    snapshot: Dict[str, object],
    canonical_action: str,
    deliveries: Sequence[Dict[str, object]] = (),
    created_at_ms: Optional[int] = None,
) -> Dict[str, object]:
    record = EmoBroadcast.get_or_none(
        EmoBroadcast.broadcast_id == broadcast_id
    )
    if record is None:
        raise BroadcastNotFoundError(broadcast_id)
    if record.broadcast_revision != expected_broadcast_revision:
        raise BroadcastRevisionConflictError(record.broadcast_revision)
    if record.lifecycle_state == "stopped":
        raise BroadcastResourceConflictError(
            "Terminal Broadcast cannot advance"
        )
    next_revision = expected_broadcast_revision + 1
    if (
        snapshot.get("broadcastId") != broadcast_id
        or snapshot.get("broadcastRevision") != next_revision
    ):
        raise ValueError("Snapshot must carry the next Broadcast revision")
    lifecycle_state = snapshot.get("lifecycleState")
    if lifecycle_state not in ("active", "waitingForSource"):
        raise ValueError("Nonterminal revision has invalid lifecycleState")
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
        _create_delivery(broadcast_id, next_revision, delivery)
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
                return commitBroadcastRevisionInTransaction(
                    broadcast_id,
                    expected_broadcast_revision,
                    snapshot,
                    canonical_action,
                    deliveries=deliveries,
                    created_at_ms=created_at_ms,
                )
    finally:
        close_connection()


def buildTerminalBroadcastSnapshot(
    previous: Dict[str, object],
    terminal_at_ms: int,
) -> Dict[str, object]:
    position_ms = int(previous["positionMs"])
    if previous["state"] == "playing":
        position_ms = projectBroadcastPositionMs(
            position_ms,
            int(previous["serverUpdatedAtMs"]),
            terminal_at_ms,
            float(previous["playbackRate"]),
        )
    snapshot = dict(previous)
    snapshot.update(
        {
            "lifecycleState": "stopped",
            "broadcastRevision": int(previous["broadcastRevision"]) + 1,
            "positionMs": position_ms,
            "serverUpdatedAtMs": terminal_at_ms,
        }
    )
    return snapshot


def terminalBroadcastStateInTransaction(
    broadcast_id: str,
    snapshot: Dict[str, object],
    stop_ack: Dict[str, object],
    terminal_deliveries: Sequence[Dict[str, object]] = (),
    expected_broadcast_revision: Optional[int] = None,
    terminal_at_ms: Optional[int] = None,
) -> Dict[str, object]:
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
        and record.broadcast_revision != expected_broadcast_revision
    ):
        raise BroadcastRevisionConflictError(record.broadcast_revision)
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
        _create_delivery(broadcast_id, next_revision, delivery)
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
    intent.authority_client_id = record.authority_client_id
    intent.authority_device_session_id = (
        record.authority_device_session_id
    )
    intent.updated_at = now()
    intent.save()
    return {
        "created": True,
        "broadcast": _serialize_broadcast_record(record),
        "stopAck": dict(stop_ack),
    }


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
        fence_rows = list(
            EmoBroadcastFence.select().where(
                EmoBroadcastFence.broadcast_id == broadcast_id
            )
        )
        context_ids = [
            row.playback_context_id
            for row in fence_rows
            if row.playback_context_id
        ]
        authority_pairs = [
            (row.user_name, row.client_id, row.device_session_id)
            for row in fence_rows
            if row.client_id and row.device_session_id
        ]
        with strictPlaybackContextLockSet(context_ids), strictAuthorityPairLockSet(
            authority_pairs
        ), broadcastResourceLock(resource_keys):
            with broadcastTransaction():
                return terminalBroadcastStateInTransaction(
                    broadcast_id,
                    snapshot,
                    stop_ack,
                    terminal_deliveries=terminal_deliveries,
                    expected_broadcast_revision=expected_broadcast_revision,
                    terminal_at_ms=terminal_at_ms,
                )
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


def _minimum_retained_broadcast_revision(broadcast_id: str) -> int:
    minimum = (
        EmoBroadcastRevision.select(
            fn.MIN(EmoBroadcastRevision.broadcast_revision)
        )
        .where(EmoBroadcastRevision.broadcast_id == broadcast_id)
        .scalar()
    )
    return 1 if minimum is None else int(minimum)


def _build_pair_replacement_delivery(
    broadcast: EmoBroadcast,
    participant: EmoBroadcastParticipant,
    connection_nonce: str,
    server_time_ms: int,
) -> Dict[str, object]:
    snapshot = _load_json(broadcast.snapshot_json, {})
    revision = int(broadcast.broadcast_revision)
    lifecycle = broadcast.lifecycle_state
    delivery_id = "delivery:%s" % uuid.uuid4()
    payload = dict(snapshot)
    payload["deliveryId"] = delivery_id
    effective_at_server_ms = None
    delivery_position_ms = int(snapshot["positionMs"])
    action = "stop" if lifecycle == "stopped" else "resync"
    if lifecycle == "active":
        effective_at_server_ms = server_time_ms + 250
        payload.update(
            {
                "effectiveAtServerMs": effective_at_server_ms,
                "serverTimeMs": server_time_ms,
            }
        )
        if snapshot["state"] == "playing":
            delivery_position_ms = projectBroadcastPositionMs(
                int(snapshot["positionMs"]),
                int(snapshot["serverUpdatedAtMs"]),
                effective_at_server_ms,
                float(snapshot["playbackRate"]),
            )
    elif lifecycle not in {"waitingForSource", "stopped"}:
        raise BroadcastResourceConflictError(
            "Broadcast lifecycle cannot produce a replacement delivery"
        )
    return {
        "deliveryId": delivery_id,
        "clientId": participant.client_id,
        "deviceSessionId": participant.device_session_id,
        "action": action,
        "effectiveAtServerMs": effective_at_server_ms,
        "serverTimeMs": (
            server_time_ms if effective_at_server_ms is not None else None
        ),
        "deliveryPositionMs": delivery_position_ms,
        "feedbackDeadlineAtServerMs": (
            (effective_at_server_ms or server_time_ms) + 8000
        ),
        "payload": payload,
        "connectionNonce": connection_nonce,
        "createdAtMs": server_time_ms,
        "resetDeadline": True,
        "broadcastRevision": revision,
    }


def _feedback_settlement_result(
    settlement: EmoBroadcastFeedbackSettlement,
) -> Dict[str, object]:
    stored = _load_json(settlement.canonical_result_json, {})
    if stored.get("action") == "broadcast.feedback.rejected":
        canonical = stored.get("payload") or {}
        action = "broadcast.feedback.rejected"
    else:
        canonical = stored
        action = "broadcast.feedback"
    follow_up = None
    if settlement.follow_up_delivery_id is not None:
        delivery = EmoBroadcastDelivery.get_or_none(
            EmoBroadcastDelivery.delivery_id
            == settlement.follow_up_delivery_id
        )
        if delivery is not None:
            follow_up = _serialize_delivery_record(delivery)
        else:
            recovery = EmoBroadcastTerminalRecovery.get_or_none(
                (
                    EmoBroadcastTerminalRecovery.broadcast_id
                    == settlement.broadcast_id
                )
                & (
                    EmoBroadcastTerminalRecovery.client_id
                    == settlement.client_id
                )
                & (
                    EmoBroadcastTerminalRecovery.device_session_id
                    == settlement.device_session_id
                )
                & (
                    EmoBroadcastTerminalRecovery.current_delivery_id
                    == settlement.follow_up_delivery_id
                )
            )
            if recovery is not None:
                follow_up = _serialize_compact_recovery_delivery(
                    recovery,
                    settlement.follow_up_delivery_id,
                    settlement.connection_nonce,
                    settlement.created_at_ms,
                )
    return {
        "created": False,
        "action": action,
        "canonicalResult": canonical,
        "followUpDelivery": follow_up,
    }


def _serialize_compact_recovery_delivery(
    recovery: EmoBroadcastTerminalRecovery,
    delivery_id: str,
    connection_nonce: str,
    created_at_ms: int,
) -> Dict[str, object]:
    return {
        "deliveryId": delivery_id,
        "broadcastId": recovery.broadcast_id,
        "broadcastRevision": recovery.terminal_broadcast_revision,
        "clientId": recovery.client_id,
        "deviceSessionId": recovery.device_session_id,
        "action": "restore",
        "payload": {
            "playbackContextId": recovery.playback_context_id,
            "broadcastId": recovery.broadcast_id,
            "deviceSessionId": recovery.device_session_id,
            "terminalBroadcastRevision": recovery.terminal_broadcast_revision,
            "deliveryId": delivery_id,
            "suspendedPlaybackContextId": (
                recovery.suspended_playback_context_id
            ),
            "suspendedEpoch": recovery.suspended_epoch,
            "suspendedVersion": recovery.suspended_version,
            "suspendedQueueRevision": recovery.suspended_queue_revision,
            "suspendedControlVersion": recovery.suspended_control_version,
            "suspendedAppliedControlVersion": (
                recovery.suspended_applied_control_version
            ),
            "lastAppliedBroadcastRevision": (
                recovery.last_applied_broadcast_revision or 0
            ),
            "queueIndex": recovery.terminal_queue_index,
            "trackId": recovery.terminal_track_id,
            "state": "stopped",
            "positionMs": recovery.terminal_position_ms,
            "playbackRate": recovery.terminal_playback_rate,
            "terminalAtServerMs": recovery.terminal_at_server_ms,
        },
        "connectionNonce": connection_nonce,
        "isCurrent": True,
        "deliveryStatus": "pending",
        "createdAtMs": created_at_ms,
    }


def _settle_compact_broadcast_feedback(
    recovery: EmoBroadcastTerminalRecovery,
    payload: Dict[str, object],
    request_fingerprint: str,
    connection_nonce: str,
    connection_epoch: int,
    server_time_ms: int,
    track_duration_ms: Optional[int],
) -> Dict[str, object]:
    execution_status = str(payload["executionStatus"])
    revision = int(
        payload[
            "appliedBroadcastRevision"
            if execution_status == "applied"
            else "failedBroadcastRevision"
        ]
    )
    expected_revision = int(recovery.terminal_broadcast_revision)
    client_seq = int(payload["clientSeq"])
    if (
        revision != expected_revision
        or payload["deliveryId"] != recovery.current_delivery_id
    ):
        if revision > expected_revision:
            error_code = "revision_ahead"
        elif revision < expected_revision:
            error_code = "revision_expired"
        else:
            error_code = "revision_unknown"
        rejection = {
            "playbackContextId": recovery.playback_context_id,
            "broadcastId": recovery.broadcast_id,
            "deviceSessionId": recovery.device_session_id,
            "clientSeq": client_seq,
            "deliveryId": payload["deliveryId"],
            "rejectedBroadcastRevision": revision,
            "currentBroadcastRevision": expected_revision,
            "minimumRetainedBroadcastRevision": expected_revision,
            "errorCode": error_code,
            "serverUpdatedAtMs": server_time_ms,
        }
        delivery_id = "delivery:%s" % uuid.uuid4()
        recovery.current_delivery_id = delivery_id
        recovery.updated_at = now()
        recovery.save()
        settlement = EmoBroadcastFeedbackSettlement.create(
            playback_context_id=recovery.playback_context_id,
            broadcast_id=recovery.broadcast_id,
            client_id=recovery.client_id,
            device_session_id=recovery.device_session_id,
            connection_nonce=connection_nonce,
            connection_epoch=connection_epoch,
            client_seq=client_seq,
            request_fingerprint=request_fingerprint,
            canonical_result_json=_canonical_json(
                {
                    "action": "broadcast.feedback.rejected",
                    "payload": rejection,
                }
            ),
            follow_up_delivery_id=delivery_id,
            created_at_ms=server_time_ms,
        )
        result = _feedback_settlement_result(settlement)
        result["created"] = True
        return result
    last_applied = recovery.last_applied_broadcast_revision or 0
    if execution_status == "applied":
        if (
            payload["queueIndex"] != recovery.terminal_queue_index
            or payload["trackId"] != recovery.terminal_track_id
            or payload["playbackRate"]
            != recovery.terminal_playback_rate
            or payload["state"] != "stopped"
            or payload.get("restoreCompleted") is not True
        ):
            raise BroadcastResourceConflictError(
                "Compact terminal feedback does not match recovery target"
            )
        if (
            track_duration_ms is not None
            and payload["positionMs"] > track_duration_ms
        ):
            raise ValueError(
                "Broadcast feedback positionMs exceeds media duration"
            )
    elif payload["lastAppliedBroadcastRevision"] != last_applied:
        raise BroadcastRevisionConflictError(last_applied)
    canonical = {
        "playbackContextId": recovery.playback_context_id,
        "broadcastId": recovery.broadcast_id,
        "sourceClientId": recovery.client_id,
        "deviceSessionId": recovery.device_session_id,
        "deliveryId": payload["deliveryId"],
        "executionStatus": execution_status,
        "clientSeq": client_seq,
        "serverUpdatedAtMs": server_time_ms,
    }
    fields = (
        (
            "appliedBroadcastRevision",
            "queueIndex",
            "trackId",
            "state",
            "positionMs",
            "playbackRate",
            "restoreCompleted",
        )
        if execution_status == "applied"
        else (
            "failedBroadcastRevision",
            "lastAppliedBroadcastRevision",
            "errorCode",
            "errorMessage",
        )
    )
    for field_name in fields:
        if field_name in payload:
            canonical[field_name] = payload[field_name]
    EmoBroadcastFeedbackSettlement.create(
        playback_context_id=recovery.playback_context_id,
        broadcast_id=recovery.broadcast_id,
        client_id=recovery.client_id,
        device_session_id=recovery.device_session_id,
        connection_nonce=connection_nonce,
        connection_epoch=connection_epoch,
        client_seq=client_seq,
        request_fingerprint=request_fingerprint,
        canonical_result_json=_canonical_json(canonical),
        created_at_ms=server_time_ms,
    )
    if execution_status == "applied":
        EmoBroadcastFence.delete().where(
            (EmoBroadcastFence.broadcast_id == recovery.broadcast_id)
            & (EmoBroadcastFence.role == "ordinary")
            & (EmoBroadcastFence.client_id == recovery.client_id)
            & (
                EmoBroadcastFence.device_session_id
                == recovery.device_session_id
            )
        ).execute()
        recovery.delete_instance()
    return {
        "created": True,
        "action": "broadcast.feedback",
        "canonicalResult": canonical,
        "followUpDelivery": None,
    }


def settleBroadcastFeedback(
    user_name: str,
    playback_context_id: str,
    broadcast_id: str,
    client_id: str,
    device_session_id: str,
    connection_nonce: str,
    connection_epoch: int,
    payload: Dict[str, object],
    request_fingerprint: str,
    server_time_ms: int,
    track_duration_ms: Optional[int] = None,
) -> Dict[str, object]:
    client_seq = int(payload["clientSeq"])
    scope = (
        (EmoBroadcastFeedbackSettlement.playback_context_id == playback_context_id)
        & (EmoBroadcastFeedbackSettlement.broadcast_id == broadcast_id)
        & (EmoBroadcastFeedbackSettlement.client_id == client_id)
        & (
            EmoBroadcastFeedbackSettlement.device_session_id
            == device_session_id
        )
        & (
            EmoBroadcastFeedbackSettlement.connection_nonce
            == connection_nonce
        )
        & (
            EmoBroadcastFeedbackSettlement.connection_epoch
            == connection_epoch
        )
    )
    open_connection(reuse=True)
    try:
        with broadcastMutationLock(broadcast_id):
            with broadcastTransaction():
                existing = EmoBroadcastFeedbackSettlement.get_or_none(
                    scope
                    & (
                        EmoBroadcastFeedbackSettlement.client_seq
                        == client_seq
                    )
                )
                if existing is not None:
                    if existing.request_fingerprint != request_fingerprint:
                        raise BroadcastFeedbackSequenceConflictError(
                            client_seq
                        )
                    return _feedback_settlement_result(existing)
                latest = (
                    EmoBroadcastFeedbackSettlement.select()
                    .where(scope)
                    .order_by(
                        EmoBroadcastFeedbackSettlement.client_seq.desc()
                    )
                    .first()
                )
                if latest is not None and client_seq <= latest.client_seq:
                    raise BroadcastFeedbackSequenceConflictError(
                        latest.client_seq
                    )

                broadcast = EmoBroadcast.get_or_none(
                    EmoBroadcast.broadcast_id == broadcast_id
                )
                if broadcast is None:
                    recovery = EmoBroadcastTerminalRecovery.get_or_none(
                        (
                            EmoBroadcastTerminalRecovery.broadcast_id
                            == broadcast_id
                        )
                        & (
                            EmoBroadcastTerminalRecovery.user_name
                            == user_name
                        )
                        & (
                            EmoBroadcastTerminalRecovery.client_id
                            == client_id
                        )
                        & (
                            EmoBroadcastTerminalRecovery.device_session_id
                            == device_session_id
                        )
                    )
                    if recovery is None:
                        raise BroadcastNotFoundError(broadcast_id)
                    if recovery.playback_context_id != playback_context_id:
                        raise BroadcastResourceConflictError(
                            "Broadcast feedback context does not match"
                        )
                    return _settle_compact_broadcast_feedback(
                        recovery,
                        payload,
                        request_fingerprint,
                        connection_nonce,
                        connection_epoch,
                        server_time_ms,
                        track_duration_ms,
                    )
                if (
                    broadcast.user_name != user_name
                    or broadcast.playback_context_id
                    != playback_context_id
                ):
                    raise BroadcastResourceConflictError(
                        "Broadcast feedback context does not match"
                    )
                if (
                    broadcast.authority_client_id == client_id
                    and broadcast.authority_device_session_id
                    == device_session_id
                ):
                    raise PermissionError(
                        "Broadcast source cannot send participant feedback"
                    )
                participant = EmoBroadcastParticipant.get_or_none(
                    _participant_expression(
                        broadcast_id,
                        client_id,
                        device_session_id,
                    )
                )
                if participant is None:
                    raise PermissionError(
                        "Broadcast feedback requires a frozen ordinary participant"
                    )

                execution_status = str(payload["executionStatus"])
                revision = int(
                    payload[
                        "appliedBroadcastRevision"
                        if execution_status == "applied"
                        else "failedBroadcastRevision"
                    ]
                )
                delivery = EmoBroadcastDelivery.get_or_none(
                    (EmoBroadcastDelivery.broadcast_id == broadcast_id)
                    & (
                        EmoBroadcastDelivery.broadcast_revision
                        == revision
                    )
                    & (EmoBroadcastDelivery.client_id == client_id)
                    & (
                        EmoBroadcastDelivery.device_session_id
                        == device_session_id
                    )
                    & (EmoBroadcastDelivery.is_current == 1)
                )
                if (
                    delivery is None
                    or delivery.delivery_id != payload["deliveryId"]
                ):
                    current_revision = int(broadcast.broadcast_revision)
                    minimum_revision = _minimum_retained_broadcast_revision(
                        broadcast_id
                    )
                    if revision > current_revision:
                        rejection_code = "revision_ahead"
                    elif revision < minimum_revision:
                        rejection_code = "revision_expired"
                    else:
                        rejection_code = "revision_unknown"
                    rejection = {
                        "playbackContextId": playback_context_id,
                        "broadcastId": broadcast_id,
                        "deviceSessionId": device_session_id,
                        "clientSeq": client_seq,
                        "deliveryId": payload["deliveryId"],
                        "rejectedBroadcastRevision": revision,
                        "currentBroadcastRevision": current_revision,
                        "minimumRetainedBroadcastRevision": minimum_revision,
                        "errorCode": rejection_code,
                        "serverUpdatedAtMs": server_time_ms,
                    }
                    replacement = _build_pair_replacement_delivery(
                        broadcast,
                        participant,
                        connection_nonce,
                        server_time_ms,
                    )
                    replacement_record = _create_delivery(
                        broadcast_id,
                        current_revision,
                        replacement,
                    )
                    settlement = EmoBroadcastFeedbackSettlement.create(
                        playback_context_id=playback_context_id,
                        broadcast_id=broadcast_id,
                        client_id=client_id,
                        device_session_id=device_session_id,
                        connection_nonce=connection_nonce,
                        connection_epoch=connection_epoch,
                        client_seq=client_seq,
                        request_fingerprint=request_fingerprint,
                        canonical_result_json=_canonical_json(
                            {
                                "action": "broadcast.feedback.rejected",
                                "payload": rejection,
                            }
                        ),
                        follow_up_delivery_id=replacement_record.delivery_id,
                        created_at_ms=server_time_ms,
                    )
                    result = _feedback_settlement_result(settlement)
                    result["created"] = True
                    return result
                target = _load_json(delivery.payload_json, {})
                terminal = (
                    broadcast.lifecycle_state == "stopped"
                    and revision == broadcast.broadcast_revision
                )

                if execution_status == "applied":
                    previous_applied = participant.applied_broadcast_revision or 0
                    if revision < previous_applied:
                        raise BroadcastRevisionConflictError(previous_applied)
                    if payload["queueIndex"] != target.get("currentIndex"):
                        raise BroadcastResourceConflictError(
                            "Broadcast feedback queueIndex does not match target"
                        )
                    if payload["trackId"] != target.get("trackId"):
                        raise BroadcastResourceConflictError(
                            "Broadcast feedback trackId does not match target"
                        )
                    if payload["playbackRate"] != target.get("playbackRate"):
                        raise BroadcastResourceConflictError(
                            "Broadcast feedback playbackRate does not match target"
                        )
                    if terminal:
                        if (
                            payload["state"] != "stopped"
                            or payload.get("restoreCompleted") is not True
                        ):
                            raise BroadcastResourceConflictError(
                                "Terminal feedback must confirm stopped restore"
                            )
                    else:
                        if "restoreCompleted" in payload:
                            raise BroadcastResourceConflictError(
                                "Nonterminal feedback cannot complete restore"
                            )
                        if payload["state"] != target.get("state"):
                            raise BroadcastResourceConflictError(
                                "Broadcast feedback state does not match target"
                            )
                    if (
                        track_duration_ms is not None
                        and payload["positionMs"] > track_duration_ms
                    ):
                        raise ValueError(
                            "Broadcast feedback positionMs exceeds media duration"
                        )

                    participant.applied_broadcast_revision = revision
                    participant.applied_queue_index = payload["queueIndex"]
                    participant.applied_track_id = payload["trackId"]
                    participant.applied_position_ms = payload["positionMs"]
                    participant.applied_state = payload["state"]
                    participant.applied_playback_rate = payload[
                        "playbackRate"
                    ]
                    participant.applied_at_server_ms = server_time_ms
                    participant.failed_broadcast_revision = None
                    participant.failed_last_applied_broadcast_revision = None
                    participant.failed_error_code = None
                    participant.failed_error_message = None
                    participant.timed_out_broadcast_revision = None
                    if revision == participant.target_broadcast_revision:
                        participant.sync_status = "applied"
                    else:
                        next_delivery = (
                            EmoBroadcastDelivery.select()
                            .where(
                                (EmoBroadcastDelivery.broadcast_id == broadcast_id)
                                & (EmoBroadcastDelivery.client_id == client_id)
                                & (
                                    EmoBroadcastDelivery.device_session_id
                                    == device_session_id
                                )
                                & (
                                    EmoBroadcastDelivery.broadcast_revision
                                    > revision
                                )
                                & (EmoBroadcastDelivery.is_current == 1)
                            )
                            .order_by(
                                EmoBroadcastDelivery.broadcast_revision.asc()
                            )
                            .first()
                        )
                        if next_delivery is None:
                            raise BroadcastResourceConflictError(
                                "Lagging feedback has no later target"
                            )
                        participant.sync_status = "lagging"
                        participant.deadline_broadcast_revision = (
                            next_delivery.broadcast_revision
                        )
                        participant.feedback_deadline_at_server_ms = (
                            server_time_ms + 8000
                        )
                    if terminal:
                        participant.restore_pending = 0
                        participant.terminal_confirmed = 1
                        participant.restore_completed = 1
                        EmoBroadcastFence.delete().where(
                            (EmoBroadcastFence.broadcast_id == broadcast_id)
                            & (EmoBroadcastFence.role == "ordinary")
                            & (EmoBroadcastFence.client_id == client_id)
                            & (
                                EmoBroadcastFence.device_session_id
                                == device_session_id
                            )
                        ).execute()
                else:
                    if revision != participant.target_broadcast_revision:
                        raise BroadcastRevisionConflictError(
                            participant.target_broadcast_revision or 0
                        )
                    previous_applied = participant.applied_broadcast_revision or 0
                    if payload["lastAppliedBroadcastRevision"] != previous_applied:
                        raise BroadcastRevisionConflictError(previous_applied)
                    participant.sync_status = "failed"
                    participant.failed_broadcast_revision = revision
                    participant.failed_last_applied_broadcast_revision = (
                        previous_applied
                    )
                    participant.failed_error_code = payload["errorCode"]
                    participant.failed_error_message = payload.get(
                        "errorMessage"
                    )
                    participant.timed_out_broadcast_revision = None

                participant.last_feedback_client_seq = client_seq
                participant.last_feedback_at_ms = server_time_ms
                participant.updated_at = now()
                participant.save()
                delivery.delivery_status = (
                    "settled"
                    if execution_status == "applied"
                    else "failed"
                )
                delivery.updated_at = now()
                delivery.save()

                canonical = {
                    "playbackContextId": playback_context_id,
                    "broadcastId": broadcast_id,
                    "sourceClientId": client_id,
                    "deviceSessionId": device_session_id,
                    "deliveryId": payload["deliveryId"],
                    "executionStatus": execution_status,
                    "clientSeq": client_seq,
                    "serverUpdatedAtMs": server_time_ms,
                }
                if execution_status == "applied":
                    for field_name in (
                        "appliedBroadcastRevision",
                        "queueIndex",
                        "trackId",
                        "state",
                        "positionMs",
                        "playbackRate",
                        "restoreCompleted",
                    ):
                        if field_name in payload:
                            canonical[field_name] = payload[field_name]
                else:
                    for field_name in (
                        "failedBroadcastRevision",
                        "lastAppliedBroadcastRevision",
                        "errorCode",
                        "errorMessage",
                    ):
                        if field_name in payload:
                            canonical[field_name] = payload[field_name]
                EmoBroadcastFeedbackSettlement.create(
                    playback_context_id=playback_context_id,
                    broadcast_id=broadcast_id,
                    client_id=client_id,
                    device_session_id=device_session_id,
                    connection_nonce=connection_nonce,
                    connection_epoch=connection_epoch,
                    client_seq=client_seq,
                    request_fingerprint=request_fingerprint,
                    canonical_result_json=_canonical_json(canonical),
                    created_at_ms=server_time_ms,
                )
                return {
                    "created": True,
                    "action": "broadcast.feedback",
                    "canonicalResult": canonical,
                    "followUpDelivery": None,
                    "participantState": _serialize_participant_record(
                        participant
                    ),
                }
    finally:
        close_connection()


def createBroadcastRegistrationReplay(
    user_name: str,
    client_id: str,
    device_session_id: str,
    connection_nonce: str,
    server_time_ms: int,
    allow_nonterminal: bool = True,
) -> Optional[Dict[str, object]]:
    open_connection(reuse=True)
    try:
        candidate_id = None
        participants = (
            EmoBroadcastParticipant.select()
            .where(
                (EmoBroadcastParticipant.user_name == user_name)
                & (EmoBroadcastParticipant.client_id == client_id)
                & (
                    EmoBroadcastParticipant.device_session_id
                    == device_session_id
                )
            )
            .order_by(EmoBroadcastParticipant.updated_at.desc())
        )
        for participant in participants:
            broadcast = EmoBroadcast.get_or_none(
                EmoBroadcast.broadcast_id == participant.broadcast_id
            )
            if broadcast is None:
                continue
            if (
                broadcast.lifecycle_state == "stopped"
                and participant.restore_pending == 1
                and participant.terminal_confirmed == 0
            ) or (
                allow_nonterminal
                and broadcast.lifecycle_state
                in {"active", "waitingForSource"}
            ):
                candidate_id = participant.broadcast_id
                break
        if candidate_id is None:
            recovery = EmoBroadcastTerminalRecovery.get_or_none(
                (EmoBroadcastTerminalRecovery.user_name == user_name)
                & (EmoBroadcastTerminalRecovery.client_id == client_id)
                & (
                    EmoBroadcastTerminalRecovery.device_session_id
                    == device_session_id
                )
            )
            if recovery is None:
                return None
            resource_key = broadcastPairResourceKey(
                user_name,
                client_id,
                device_session_id,
            )
            with strictAuthorityPairLockSet(
                ((user_name, client_id, device_session_id),)
            ), broadcastResourceLock((resource_key,)):
                with broadcastTransaction():
                    recovery = EmoBroadcastTerminalRecovery.get_or_none(
                        (EmoBroadcastTerminalRecovery.user_name == user_name)
                        & (EmoBroadcastTerminalRecovery.client_id == client_id)
                        & (
                            EmoBroadcastTerminalRecovery.device_session_id
                            == device_session_id
                        )
                    )
                    if recovery is None:
                        return None
                    delivery_id = "delivery:%s" % uuid.uuid4()
                    recovery.current_delivery_id = delivery_id
                    recovery.updated_at = now()
                    recovery.save()
                    return {
                        "created": True,
                        "broadcast": None,
                        "delivery": {
                            "deliveryId": delivery_id,
                            "broadcastId": recovery.broadcast_id,
                            "broadcastRevision": (
                                recovery.terminal_broadcast_revision
                            ),
                            "clientId": client_id,
                            "deviceSessionId": device_session_id,
                            "action": "restore",
                            "payload": {
                                "playbackContextId": (
                                    recovery.playback_context_id
                                ),
                                "broadcastId": recovery.broadcast_id,
                                "deviceSessionId": device_session_id,
                                "terminalBroadcastRevision": (
                                    recovery.terminal_broadcast_revision
                                ),
                                "deliveryId": delivery_id,
                                "suspendedPlaybackContextId": (
                                    recovery.suspended_playback_context_id
                                ),
                                "suspendedEpoch": recovery.suspended_epoch,
                                "suspendedVersion": recovery.suspended_version,
                                "suspendedQueueRevision": (
                                    recovery.suspended_queue_revision
                                ),
                                "suspendedControlVersion": (
                                    recovery.suspended_control_version
                                ),
                                "suspendedAppliedControlVersion": (
                                    recovery.suspended_applied_control_version
                                ),
                                "lastAppliedBroadcastRevision": (
                                    recovery.last_applied_broadcast_revision
                                    or 0
                                ),
                                "queueIndex": recovery.terminal_queue_index,
                                "trackId": recovery.terminal_track_id,
                                "state": "stopped",
                                "positionMs": recovery.terminal_position_ms,
                                "playbackRate": (
                                    recovery.terminal_playback_rate
                                ),
                                "terminalAtServerMs": (
                                    recovery.terminal_at_server_ms
                                ),
                            },
                            "connectionNonce": connection_nonce,
                            "isCurrent": True,
                            "deliveryStatus": "pending",
                            "createdAtMs": server_time_ms,
                        },
                    }
        with broadcastMutationLock(candidate_id):
            with broadcastTransaction():
                broadcast = EmoBroadcast.get_or_none(
                    EmoBroadcast.broadcast_id == candidate_id
                )
                participant = EmoBroadcastParticipant.get_or_none(
                    _participant_expression(
                        candidate_id,
                        client_id,
                        device_session_id,
                    )
                )
                if (
                    broadcast is None
                    or participant is None
                    or broadcast.user_name != user_name
                    or (
                        broadcast.lifecycle_state == "stopped"
                        and (
                            participant.restore_pending != 1
                            or participant.terminal_confirmed == 1
                        )
                    )
                    or (
                        broadcast.lifecycle_state
                        in {"active", "waitingForSource"}
                        and not allow_nonterminal
                    )
                ):
                    return None
                current = EmoBroadcastDelivery.get_or_none(
                    (EmoBroadcastDelivery.broadcast_id == candidate_id)
                    & (
                        EmoBroadcastDelivery.broadcast_revision
                        == broadcast.broadcast_revision
                    )
                    & (EmoBroadcastDelivery.client_id == client_id)
                    & (
                        EmoBroadcastDelivery.device_session_id
                        == device_session_id
                    )
                    & (EmoBroadcastDelivery.is_current == 1)
                )
                if (
                    current is not None
                    and current.connection_nonce == connection_nonce
                ):
                    return {
                        "created": False,
                        "broadcast": _serialize_broadcast_record(broadcast),
                        "delivery": _serialize_delivery_record(current),
                    }
                replacement = _build_pair_replacement_delivery(
                    broadcast,
                    participant,
                    connection_nonce,
                    server_time_ms,
                )
                record = _create_delivery(
                    candidate_id,
                    int(broadcast.broadcast_revision),
                    replacement,
                )
                return {
                    "created": True,
                    "broadcast": _serialize_broadcast_record(broadcast),
                    "delivery": _serialize_delivery_record(record),
                }
    finally:
        close_connection()


def suspendBroadcastForAuthorityDisconnect(
    user_name: str,
    client_id: str,
    device_session_id: str,
    server_time_ms: int,
    deadline_ms: int,
) -> Optional[Dict[str, object]]:
    open_connection(reuse=True)
    try:
        candidate = (
            EmoBroadcast.select(EmoBroadcast.broadcast_id)
            .where(
                (EmoBroadcast.user_name == user_name)
                & (EmoBroadcast.authority_client_id == client_id)
                & (
                    EmoBroadcast.authority_device_session_id
                    == device_session_id
                )
                & (EmoBroadcast.lifecycle_state == "active")
            )
            .first()
        )
        if candidate is None:
            return None
        broadcast_id = candidate.broadcast_id
        with broadcastMutationLock(broadcast_id):
            with broadcastTransaction():
                record = EmoBroadcast.get_or_none(
                    EmoBroadcast.broadcast_id == broadcast_id
                )
                if (
                    record is None
                    or record.lifecycle_state != "active"
                    or record.authority_client_id != client_id
                    or record.authority_device_session_id
                    != device_session_id
                ):
                    return None
                previous = _load_json(record.snapshot_json, {})
                position_ms = int(previous["positionMs"])
                if previous["state"] == "playing":
                    position_ms = projectBroadcastPositionMs(
                        position_ms,
                        int(previous["serverUpdatedAtMs"]),
                        server_time_ms,
                        float(previous["playbackRate"]),
                    )
                snapshot = dict(previous)
                snapshot.update(
                    {
                        "lifecycleState": "waitingForSource",
                        "broadcastRevision": int(record.broadcast_revision) + 1,
                        "positionMs": position_ms,
                        "state": "paused",
                        "serverUpdatedAtMs": server_time_ms,
                    }
                )
                deliveries = []
                participants = EmoBroadcastParticipant.select().where(
                    EmoBroadcastParticipant.broadcast_id == broadcast_id
                )
                for participant in participants:
                    delivery_id = "delivery:%s" % uuid.uuid4()
                    payload = dict(snapshot, deliveryId=delivery_id)
                    deliveries.append(
                        {
                            "deliveryId": delivery_id,
                            "clientId": participant.client_id,
                            "deviceSessionId": participant.device_session_id,
                            "action": "waiting",
                            "deliveryPositionMs": position_ms,
                            "feedbackDeadlineAtServerMs": server_time_ms + 8000,
                            "payload": payload,
                            "connectionNonce": None,
                            "createdAtMs": server_time_ms,
                        }
                    )
                committed = commitBroadcastRevisionInTransaction(
                    broadcast_id,
                    int(record.broadcast_revision),
                    snapshot,
                    "waiting",
                    deliveries,
                    server_time_ms,
                )
                EmoBroadcast.update(
                    authority_disconnect_deadline_ms=deadline_ms,
                    updated_at=now(),
                ).where(
                    EmoBroadcast.broadcast_id == broadcast_id
                ).execute()
                committed["authorityDisconnectDeadlineMs"] = deadline_ms
                return {
                    "broadcast": committed,
                    "snapshot": snapshot,
                    "deliveries": deliveries,
                    "action": "broadcast.waiting",
                    "serverTimeMs": server_time_ms,
                }
    finally:
        close_connection()


def sweepBroadcastAuthorityDisconnectDeadlines(
    now_ms: Optional[int] = None,
) -> List[Dict[str, object]]:
    sweep_time_ms = int(
        now_ms if now_ms is not None else time.time() * 1000
    )
    open_connection(reuse=True)
    try:
        candidates = list(
            EmoBroadcast.select(
                EmoBroadcast.broadcast_id,
                EmoBroadcast.authority_disconnect_deadline_ms,
            ).where(
                (EmoBroadcast.lifecycle_state == "waitingForSource")
                & (
                    EmoBroadcast.authority_disconnect_deadline_ms
                    <= sweep_time_ms
                )
            )
        )
    finally:
        close_connection()
    terminal_mutations = []
    for candidate in candidates:
        open_connection(reuse=True)
        try:
            with broadcastMutationLock(candidate.broadcast_id):
                with broadcastTransaction():
                    record = EmoBroadcast.get_or_none(
                        EmoBroadcast.broadcast_id == candidate.broadcast_id
                    )
                    if (
                        record is None
                        or record.lifecycle_state != "waitingForSource"
                        or record.authority_disconnect_deadline_ms is None
                        or record.authority_disconnect_deadline_ms
                        > sweep_time_ms
                    ):
                        continue
                    previous = _load_json(record.snapshot_json, {})
                    snapshot = buildTerminalBroadcastSnapshot(
                        previous,
                        sweep_time_ms,
                    )
                    deliveries = []
                    participants = EmoBroadcastParticipant.select().where(
                        EmoBroadcastParticipant.broadcast_id
                        == candidate.broadcast_id
                    )
                    for participant in participants:
                        delivery_id = "delivery:%s" % uuid.uuid4()
                        payload = dict(snapshot, deliveryId=delivery_id)
                        deliveries.append(
                            {
                                "deliveryId": delivery_id,
                                "clientId": participant.client_id,
                                "deviceSessionId": participant.device_session_id,
                                "action": "stop",
                                "deliveryPositionMs": snapshot["positionMs"],
                                "feedbackDeadlineAtServerMs": sweep_time_ms + 8000,
                                "payload": payload,
                                "connectionNonce": None,
                                "createdAtMs": sweep_time_ms,
                            }
                        )
                    terminal = terminalBroadcastStateInTransaction(
                        candidate.broadcast_id,
                        snapshot,
                        {},
                        terminal_deliveries=deliveries,
                        expected_broadcast_revision=int(
                            record.broadcast_revision
                        ),
                        terminal_at_ms=sweep_time_ms,
                    )
                    terminal_mutations.append(
                        {
                            "broadcast": terminal["broadcast"],
                            "snapshot": snapshot,
                            "deliveries": deliveries,
                            "action": "broadcast.stop",
                            "serverTimeMs": sweep_time_ms,
                            "includeSource": True,
                        }
                    )
        finally:
            close_connection()
    return terminal_mutations


def stopNonterminalBroadcastsForRestart(
    now_ms: Optional[int] = None,
) -> List[str]:
    stopped_at_ms = int(
        now_ms if now_ms is not None else time.time() * 1000
    )
    open_connection(reuse=True)
    try:
        candidate_ids = [
            item.broadcast_id
            for item in EmoBroadcast.select(
                EmoBroadcast.broadcast_id
            ).where(EmoBroadcast.lifecycle_state != "stopped")
        ]
    finally:
        close_connection()
    stopped = []
    for broadcast_id in candidate_ids:
        open_connection(reuse=True)
        try:
            with broadcastMutationLock(broadcast_id):
                with broadcastTransaction():
                    record = EmoBroadcast.get_or_none(
                        EmoBroadcast.broadcast_id == broadcast_id
                    )
                    if record is None or record.lifecycle_state == "stopped":
                        continue
                    previous = _load_json(record.snapshot_json, {})
                    snapshot = buildTerminalBroadcastSnapshot(
                        previous,
                        stopped_at_ms,
                    )
                    deliveries = []
                    for participant in EmoBroadcastParticipant.select().where(
                        EmoBroadcastParticipant.broadcast_id == broadcast_id
                    ):
                        delivery_id = "delivery:%s" % uuid.uuid4()
                        deliveries.append(
                            {
                                "deliveryId": delivery_id,
                                "clientId": participant.client_id,
                                "deviceSessionId": participant.device_session_id,
                                "action": "stop",
                                "deliveryPositionMs": snapshot["positionMs"],
                                "feedbackDeadlineAtServerMs": stopped_at_ms + 8000,
                                "payload": dict(
                                    snapshot,
                                    deliveryId=delivery_id,
                                ),
                                "connectionNonce": None,
                                "createdAtMs": stopped_at_ms,
                            }
                        )
                    terminalBroadcastStateInTransaction(
                        broadcast_id,
                        snapshot,
                        {},
                        terminal_deliveries=deliveries,
                        expected_broadcast_revision=int(
                            record.broadcast_revision
                        ),
                        terminal_at_ms=stopped_at_ms,
                    )
                    stopped.append(broadcast_id)
        finally:
            close_connection()
    return stopped


def sweepBroadcastFeedbackDeadlines(
    now_ms: Optional[int] = None,
) -> List[Dict[str, object]]:
    sweep_time_ms = int(
        now_ms if now_ms is not None else time.time() * 1000
    )
    open_connection(reuse=True)
    try:
        candidates = list(
            EmoBroadcastParticipant.select(
                EmoBroadcastParticipant.broadcast_id,
                EmoBroadcastParticipant.client_id,
                EmoBroadcastParticipant.device_session_id,
            ).where(
                EmoBroadcastParticipant.sync_status.in_(
                    ("pending", "lagging")
                )
                & (
                    EmoBroadcastParticipant.feedback_deadline_at_server_ms
                    <= sweep_time_ms
                )
            )
        )
    finally:
        close_connection()
    timed_out = []
    for candidate in candidates:
        open_connection(reuse=True)
        try:
            with broadcastMutationLock(candidate.broadcast_id):
                with broadcastTransaction():
                    participant = EmoBroadcastParticipant.get_or_none(
                        _participant_expression(
                            candidate.broadcast_id,
                            candidate.client_id,
                            candidate.device_session_id,
                        )
                    )
                    if (
                        participant is None
                        or participant.sync_status
                        not in ("pending", "lagging")
                        or participant.feedback_deadline_at_server_ms is None
                        or participant.feedback_deadline_at_server_ms
                        > sweep_time_ms
                    ):
                        continue
                    participant.sync_status = "timedOut"
                    participant.timed_out_broadcast_revision = (
                        participant.deadline_broadcast_revision
                    )
                    participant.failed_broadcast_revision = None
                    participant.failed_last_applied_broadcast_revision = None
                    participant.failed_error_code = "feedback_timeout"
                    participant.failed_error_message = None
                    participant.updated_at = now()
                    participant.save()
                    timed_out.append(
                        _serialize_participant_record(participant)
                    )
        finally:
            close_connection()
    return timed_out


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
        participant_hint = EmoBroadcastParticipant.get_or_none(
            _participant_expression(
                broadcast_id,
                client_id,
                device_session_id,
            )
        )
        recovery_hint = EmoBroadcastTerminalRecovery.get_or_none(
            (EmoBroadcastTerminalRecovery.broadcast_id == broadcast_id)
            & (EmoBroadcastTerminalRecovery.user_name == user_name)
            & (EmoBroadcastTerminalRecovery.client_id == client_id)
            & (
                EmoBroadcastTerminalRecovery.device_session_id
                == device_session_id
            )
        )
        suspended_context_id = (
            participant_hint.suspended_playback_context_id
            if participant_hint is not None
            else (
                recovery_hint.suspended_playback_context_id
                if recovery_hint is not None
                else None
            )
        )
        context_ids = (
            () if suspended_context_id is None else (suspended_context_id,)
        )
        authority_pair = ((user_name, client_id, device_session_id),)
        with strictPlaybackContextLockSet(context_ids), strictAuthorityPairLockSet(
            authority_pair
        ), broadcastResourceLock((resource_key,)):
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
                    intent = EmoBroadcastIntentOutcome.get(
                        EmoBroadcastIntentOutcome.broadcast_id
                        == record.broadcast_id
                    )
                    intent.authority_client_id = record.authority_client_id
                    intent.authority_device_session_id = (
                        record.authority_device_session_id
                    )
                    intent.updated_at = now()
                    intent.save()
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


def listFullTerminalBroadcastsForSource(
    user_name: str,
    client_id: str,
    device_session_id: str,
) -> List[Dict[str, object]]:
    open_connection(reuse=True)
    try:
        records = EmoBroadcast.select().where(
            (EmoBroadcast.user_name == user_name)
            & (EmoBroadcast.authority_client_id == client_id)
            & (
                EmoBroadcast.authority_device_session_id
                == device_session_id
            )
            & (EmoBroadcast.lifecycle_state == "stopped")
        )
        return [_serialize_broadcast_record(record) for record in records]
    finally:
        close_connection()
