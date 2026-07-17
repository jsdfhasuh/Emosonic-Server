from copy import deepcopy
import re
from typing import Dict, NamedTuple, Optional, Set, Tuple


MAX_ID_BYTES = 128
MAX_ACTION_BYTES = 64
MAX_QUEUE_ITEMS = 1000
MAX_PARTICIPANTS = 100

BASE_STRICT_CAPABILITIES = (
    "playbackContextV2",
    "playbackPrepare",
    "effectiveAtPlayback",
    "canPlay",
    "canPause",
    "canSeek",
    "canSetVolume",
    "supportsFollow",
    "supportsBroadcast",
)
OPTIONAL_STRICT_CAPABILITIES = ("remoteVolumeControl",)
STRICT_CAPABILITIES = BASE_STRICT_CAPABILITIES + OPTIONAL_STRICT_CAPABILITIES


class ActionSchema(NamedTuple):
    message_type: str
    required: Tuple[str, ...]
    optional: Tuple[str, ...] = ()


class StrictRequestValidationError(ValueError):
    def __init__(self, message: str, code: str = "bad_request", correlatable: bool = True):
        super().__init__(message)
        self.code = code
        self.correlatable = correlatable


class StrictOutputValidationError(ValueError):
    pass


ACTION_SCHEMAS = {
    "auth.login": ActionSchema("auth", ("u", "p")),
    "device.register": ActionSchema(
        "device",
        ("clientId", "deviceSessionId", "deviceName", "roles", "capabilities"),
        ("alias",),
    ),
    "device.list": ActionSchema("state", ()),
    "device.setVolume": ActionSchema(
        "command", ("targetClientId", "targetDeviceSessionId", "volume")
    ),
    "device.volume.update": ActionSchema(
        "event", ("deviceSessionId", "volume", "clientSeq")
    ),
    "system.ping": ActionSchema("system", ()),
    "playback.context.list": ActionSchema(
        "state",
        ("authorityClientId", "authorityDeviceSessionId"),
    ),
    "playback.context.ensure": ActionSchema(
        "command",
        (
            "deviceSessionId",
            "queueSongIds",
            "positionMs",
            "state",
        ),
        ("currentIndex",),
    ),
    "playback.context.subscribe": ActionSchema("state", ("playbackContextId",)),
    "playback.context.unsubscribe": ActionSchema("state", ("playbackContextId",)),
    "playback.context.status": ActionSchema("state", ("playbackContextId",)),
    "playback.context.prepare": ActionSchema(
        "command",
        ("playbackContextId", "intentId", "baseControlVersion"),
        ("initialQueueSongIds", "currentIndex", "positionMs"),
    ),
    "playback.context.close": ActionSchema("command", ("playbackContextId",)),
    "queue.context.sync": ActionSchema(
        "state",
        (
            "playbackContextId",
            "deviceSessionId",
            "queueSongIds",
            "positionMs",
            "baseQueueRevision",
        ),
        ("currentIndex", "baseControlVersion"),
    ),
    "playback.context.prepared": ActionSchema(
        "event",
        ("playbackContextId", "deviceSessionId", "intentId", "ready"),
        ("errorCode", "errorMessage"),
    ),
    "playback.update": ActionSchema(
        "event",
        (
            "playbackContextId",
            "deviceSessionId",
            "origin",
            "state",
            "positionMs",
            "clientSeq",
        ),
        (
            "trackId",
            "volume",
            "muted",
            "executionStatus",
            "commandControlVersion",
            "appliedControlVersion",
            "intentId",
            "epoch",
            "observedControlVersion",
            "queueIndex",
            "errorCode",
            "errorMessage",
        ),
    ),
    "queue.playItem": ActionSchema(
        "command",
        ("playbackContextId", "queueIndex", "baseQueueRevision", "baseControlVersion"),
    ),
    "player.play": ActionSchema(
        "command", ("playbackContextId", "baseControlVersion"), ("positionMs",)
    ),
    "player.pause": ActionSchema(
        "command", ("playbackContextId", "baseControlVersion"), ("positionMs",)
    ),
    "player.seek": ActionSchema(
        "command", ("playbackContextId", "baseControlVersion", "positionMs")
    ),
    "player.next": ActionSchema("command", ("playbackContextId", "baseControlVersion")),
    "player.prev": ActionSchema("command", ("playbackContextId", "baseControlVersion")),
    "follow.start": ActionSchema(
        "command", ("sourcePlaybackContextId", "deviceSessionId")
    ),
    "follow.stop": ActionSchema("command", ("sourcePlaybackContextId",)),
    "playback.handoff.start": ActionSchema(
        "command", ("playbackContextId", "targetClientId", "baseControlVersion")
    ),
    "playback.ready": ActionSchema(
        "event",
        ("playbackContextId", "prepareId", "ready"),
        ("handoffId", "errorCode", "errorMessage"),
    ),
    "playback.handoff.complete": ActionSchema(
        "event", ("playbackContextId", "handoffId"), ("positionMs",)
    ),
    "playback.handoff.cancel": ActionSchema(
        "command", ("playbackContextId", "handoffId"), ("reason",)
    ),
    "broadcast.start": ActionSchema(
        "command",
        ("playbackContextId", "queueSongIds", "currentIndex", "positionMs"),
        ("participants", "autoPlay"),
    ),
    "broadcast.status": ActionSchema("state", ("playbackContextId", "broadcastId")),
    "broadcast.play": ActionSchema("command", ("playbackContextId", "broadcastId")),
    "broadcast.pause": ActionSchema("command", ("playbackContextId", "broadcastId")),
    "broadcast.seek": ActionSchema(
        "command", ("playbackContextId", "broadcastId", "positionMs")
    ),
    "broadcast.playItem": ActionSchema(
        "command", ("playbackContextId", "broadcastId", "queueIndex")
    ),
    "broadcast.queue.sync": ActionSchema(
        "state",
        ("playbackContextId", "broadcastId", "queueSongIds", "currentIndex", "positionMs"),
        ("baseQueueRevision", "baseControlVersion"),
    ),
    "broadcast.stop": ActionSchema("command", ("playbackContextId", "broadcastId")),
}  # type: Dict[str, ActionSchema]

_ID_FIELDS = {
    "clientId",
    "deviceSessionId",
    "authorityClientId",
    "authorityDeviceSessionId",
    "playbackContextId",
    "sourcePlaybackContextId",
    "handoffId",
    "prepareId",
    "broadcastId",
    "targetClientId",
    "targetDeviceSessionId",
    "trackId",
    "intentId",
}
_NON_NEGATIVE_INT_FIELDS = {
    "currentIndex",
    "positionMs",
    "queueIndex",
    "baseQueueRevision",
    "baseControlVersion",
}
_POSITIVE_INT_FIELDS = {
    "epoch",
    "commandControlVersion",
    "appliedControlVersion",
    "observedControlVersion",
}
_BOOLEAN_FIELDS = {"ready", "muted", "autoPlay"}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _normalize_string(value: object, field_name: str, max_bytes: int = MAX_ID_BYTES) -> str:
    if not isinstance(value, str):
        raise StrictRequestValidationError("%s must be a string" % field_name)
    normalized = value.strip()
    if not normalized:
        raise StrictRequestValidationError("%s must be non-empty" % field_name)
    if len(normalized.encode("utf-8")) > max_bytes:
        raise StrictRequestValidationError("%s exceeds %d UTF-8 bytes" % (field_name, max_bytes))
    return normalized


def _normalize_string_array(
    value: object, field_name: str, maximum: int
) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise StrictRequestValidationError("%s must be a non-empty array" % field_name)
    if len(value) > maximum:
        raise StrictRequestValidationError("%s exceeds %d items" % (field_name, maximum))
    normalized = tuple(_normalize_string(item, field_name) for item in value)
    if len(set(normalized)) != len(normalized):
        raise StrictRequestValidationError("%s must not contain duplicates" % field_name)
    return normalized


def _contains_key(value: object, forbidden: Set[str]) -> bool:
    if isinstance(value, dict):
        if forbidden.intersection(value):
            return True
        return any(_contains_key(item, forbidden) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, forbidden) for item in value)
    return False


def _validate_capabilities(value: object) -> Dict[str, bool]:
    fields = set(value) if isinstance(value, dict) else set()
    allowed_fields = (
        set(BASE_STRICT_CAPABILITIES),
        set(STRICT_CAPABILITIES),
    )
    if not isinstance(value, dict) or fields not in allowed_fields:
        raise StrictRequestValidationError(
            "capabilities must contain the 9 base strict-v2 booleans "
            "with optional remoteVolumeControl"
        )
    normalized = {}  # type: Dict[str, bool]
    for capability in STRICT_CAPABILITIES:
        if capability not in value:
            continue
        capability_value = value[capability]
        if not isinstance(capability_value, bool):
            raise StrictRequestValidationError("capabilities.%s must be a boolean" % capability)
        normalized[capability] = capability_value
    if not normalized["playbackContextV2"]:
        raise StrictRequestValidationError("capabilities.playbackContextV2 must be true")
    if normalized["effectiveAtPlayback"] and not normalized["playbackPrepare"]:
        raise StrictRequestValidationError(
            "effectiveAtPlayback requires playbackPrepare"
        )
    return normalized


def _validate_roles(value: object) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise StrictRequestValidationError("roles must be a non-empty array")
    if len(value) > 2 or len(set(value)) != len(value):
        raise StrictRequestValidationError("roles must contain distinct player/controller values")
    if not all(role in {"player", "controller"} for role in value):
        raise StrictRequestValidationError("roles contain an unsupported value")
    return tuple(role for role in ("player", "controller") if role in value)


def _validate_field(
    payload: Dict[str, object],
    field_name: str,
    action: str,
) -> None:
    value = payload[field_name]
    if field_name in _ID_FIELDS:
        payload[field_name] = _normalize_string(value, field_name)
    elif field_name in {"u", "deviceName", "alias", "reason", "errorCode", "errorMessage"}:
        payload[field_name] = _normalize_string(value, field_name, 512 if field_name == "errorMessage" else MAX_ID_BYTES)
    elif field_name == "p":
        if not isinstance(value, str) or not value:
            raise StrictRequestValidationError("p must be a non-empty string")
    elif field_name in _NON_NEGATIVE_INT_FIELDS:
        if not _is_int(value) or value < 0:
            raise StrictRequestValidationError("%s must be an integer >= 0" % field_name)
    elif field_name in _POSITIVE_INT_FIELDS:
        if not _is_int(value) or value < 1:
            raise StrictRequestValidationError("%s must be an integer >= 1" % field_name)
    elif field_name == "clientSeq":
        if not _is_int(value) or value < 1:
            raise StrictRequestValidationError("clientSeq must be an integer >= 1")
    elif field_name == "volume":
        if not _is_int(value) or value < 0 or value > 100:
            raise StrictRequestValidationError("volume must be an integer from 0 to 100")
    elif field_name in _BOOLEAN_FIELDS:
        if not isinstance(value, bool):
            raise StrictRequestValidationError("%s must be a boolean" % field_name)
    elif field_name == "state":
        if value not in {"idle", "playing", "paused", "stopped"}:
            raise StrictRequestValidationError("state is invalid")
    elif field_name == "queueSongIds":
        if not isinstance(value, list):
            raise StrictRequestValidationError("queueSongIds must be an array")
        if not value and action in {"playback.context.ensure", "queue.context.sync"}:
            payload[field_name] = []
        else:
            payload[field_name] = list(
                _normalize_string_array(value, field_name, MAX_QUEUE_ITEMS)
            )
    elif field_name == "initialQueueSongIds":
        payload[field_name] = list(
            _normalize_string_array(value, field_name, MAX_QUEUE_ITEMS)
        )
    elif field_name == "participants":
        payload[field_name] = list(
            _normalize_string_array(value, field_name, MAX_PARTICIPANTS)
        )
    elif field_name == "roles":
        payload[field_name] = list(_validate_roles(value))
    elif field_name == "capabilities":
        payload[field_name] = _validate_capabilities(value)
    elif field_name == "origin":
        if value not in {"passive", "remoteCommand", "localUser"}:
            raise StrictRequestValidationError("origin is invalid")
    elif field_name == "executionStatus":
        if value not in {"committed", "failed"}:
            raise StrictRequestValidationError("executionStatus is invalid")
    else:
        raise StrictRequestValidationError("No validator exists for %s" % field_name)


def _validate_action_combinations(action: str, payload: Dict[str, object]) -> None:
    if action in {"broadcast.start", "broadcast.queue.sync"}:
        if payload["currentIndex"] >= len(payload["queueSongIds"]):
            raise StrictRequestValidationError("currentIndex is outside queueSongIds")
    if action in {"playback.context.ensure", "queue.context.sync"}:
        queue = payload["queueSongIds"]
        if queue:
            if "currentIndex" not in payload:
                raise StrictRequestValidationError(
                    "non-empty queueSongIds requires currentIndex"
                )
            if payload["currentIndex"] >= len(queue):
                raise StrictRequestValidationError(
                    "currentIndex is outside queueSongIds"
                )
            if action == "playback.context.ensure" and payload["state"] == "idle":
                raise StrictRequestValidationError(
                    "non-empty ensure queue forbids idle state"
                )
        else:
            if "currentIndex" in payload:
                raise StrictRequestValidationError(
                    "empty queueSongIds forbids currentIndex"
                )
            if payload["positionMs"] != 0:
                raise StrictRequestValidationError(
                    "empty queueSongIds requires positionMs 0"
                )
            if action == "playback.context.ensure" and payload["state"] != "idle":
                raise StrictRequestValidationError(
                    "empty ensure queue requires idle state"
                )
    if action == "playback.context.prepare":
        has_initial_queue = "initialQueueSongIds" in payload
        has_index = "currentIndex" in payload
        has_position = "positionMs" in payload
        if has_initial_queue != has_index or has_initial_queue != has_position:
            raise StrictRequestValidationError(
                "initialQueueSongIds, currentIndex and positionMs must appear together"
            )
        if has_initial_queue and payload["currentIndex"] >= len(
            payload["initialQueueSongIds"]
        ):
            raise StrictRequestValidationError(
                "currentIndex is outside initialQueueSongIds"
            )
    if action == "playback.context.prepared":
        if payload["ready"]:
            if "errorCode" in payload or "errorMessage" in payload:
                raise StrictRequestValidationError("ready:true forbids error fields")
        elif "errorCode" not in payload:
            raise StrictRequestValidationError("ready:false requires errorCode")
        elif payload["errorCode"] not in {
            "queue_required",
            "restore_failed",
            "prepare_timeout",
            "authority_changed",
        }:
            raise StrictRequestValidationError(
                "playback.context.prepared errorCode is invalid"
            )
    if action == "playback.update":
        state_is_idle = payload["state"] == "idle"
        if state_is_idle:
            if payload["positionMs"] != 0 or "trackId" in payload:
                raise StrictRequestValidationError(
                    "idle playback.update requires positionMs 0 and forbids trackId"
                )
        elif "trackId" not in payload:
            raise StrictRequestValidationError(
                "non-idle playback.update requires trackId"
            )

        common_optional = {"trackId", "volume", "muted"}
        origin = payload["origin"]
        if origin == "passive":
            required = {"appliedControlVersion"}
            allowed = common_optional | required
        elif origin == "remoteCommand":
            required = {
                "executionStatus",
                "commandControlVersion",
                "appliedControlVersion",
            }
            allowed = common_optional | required | {"errorCode", "errorMessage"}
        else:
            required = {
                "executionStatus",
                "intentId",
                "epoch",
                "observedControlVersion",
                "queueIndex",
                "trackId",
            }
            allowed = common_optional | required

        shape_fields = set(payload) - {
            "playbackContextId",
            "deviceSessionId",
            "origin",
            "state",
            "positionMs",
            "clientSeq",
        }
        missing = required - shape_fields
        forbidden = shape_fields - allowed
        if missing:
            raise StrictRequestValidationError(
                "playback.update is missing fields: %s" % ", ".join(sorted(missing))
            )
        if forbidden:
            raise StrictRequestValidationError(
                "playback.update forbids fields: %s" % ", ".join(sorted(forbidden))
            )
        if origin == "remoteCommand":
            if payload["executionStatus"] == "committed":
                if payload["appliedControlVersion"] != payload["commandControlVersion"]:
                    raise StrictRequestValidationError(
                        "committed appliedControlVersion must equal commandControlVersion"
                    )
                if "errorCode" in payload or "errorMessage" in payload:
                    raise StrictRequestValidationError(
                        "committed playback.update forbids error fields"
                    )
            else:
                if payload["appliedControlVersion"] >= payload["commandControlVersion"]:
                    raise StrictRequestValidationError(
                        "failed appliedControlVersion must be below commandControlVersion"
                    )
                if payload.get("errorCode") not in {
                    "playback_failed",
                    "track_load_failed",
                    "seek_failed",
                    "execution_timeout",
                }:
                    raise StrictRequestValidationError(
                        "failed playback.update requires a stable errorCode"
                    )
        if origin == "localUser":
            if payload["executionStatus"] != "committed":
                raise StrictRequestValidationError(
                    "localUser playback.update must be committed"
                )
            if state_is_idle:
                raise StrictRequestValidationError(
                    "localUser playback.update cannot be idle"
                )
    if action == "playback.ready":
        if payload["ready"]:
            if "errorCode" in payload or "errorMessage" in payload:
                raise StrictRequestValidationError("ready:true forbids error fields")
        elif "errorCode" not in payload:
            raise StrictRequestValidationError("ready:false requires errorCode")
        elif re.fullmatch(r"[a-z][a-z0-9_]{0,63}", payload["errorCode"]) is None:
            raise StrictRequestValidationError("errorCode has an invalid format")


def validate_strict_request(message: object) -> Dict[str, object]:
    if not isinstance(message, dict):
        raise StrictRequestValidationError(
            "Message must be a JSON object", correlatable=False
        )

    request_id_value = message.get("requestId")
    action_value = message.get("action")
    try:
        request_id = _normalize_string(request_id_value, "requestId")
        action = _normalize_string(action_value, "action", MAX_ACTION_BYTES)
    except StrictRequestValidationError as exc:
        raise StrictRequestValidationError(str(exc), correlatable=False)

    allowed_envelope_fields = {"type", "action", "requestId", "payload", "timestamp"}
    unknown_envelope_fields = set(message) - allowed_envelope_fields
    if unknown_envelope_fields:
        raise StrictRequestValidationError(
            "Unknown envelope fields: %s" % ", ".join(sorted(unknown_envelope_fields))
        )
    if "type" not in message or "payload" not in message:
        raise StrictRequestValidationError("type and payload are required")
    if "timestamp" in message and (
        not isinstance(message["timestamp"], (int, float))
        or isinstance(message["timestamp"], bool)
    ):
        raise StrictRequestValidationError("timestamp must be a number")

    schema = ACTION_SCHEMAS.get(action)
    if schema is None:
        raise StrictRequestValidationError(
            "Action is not supported by strict-v2", code="not_supported"
        )
    if message["type"] != schema.message_type:
        raise StrictRequestValidationError(
            "type does not match action %s" % action
        )
    if not isinstance(message["payload"], dict):
        raise StrictRequestValidationError("payload must be an object")
    if _contains_key(message["payload"], {"sessionId", "sourceSessionId"}):
        raise StrictRequestValidationError("sessionId is not allowed in strict-v2 payload")

    payload = deepcopy(message["payload"])
    allowed_payload_fields = set(schema.required).union(schema.optional)
    unknown_payload_fields = set(payload) - allowed_payload_fields
    if unknown_payload_fields:
        raise StrictRequestValidationError(
            "Unknown %s payload fields: %s"
            % (action, ", ".join(sorted(unknown_payload_fields)))
        )
    missing_payload_fields = set(schema.required) - set(payload)
    if missing_payload_fields:
        raise StrictRequestValidationError(
            "Missing %s payload fields: %s"
            % (action, ", ".join(sorted(missing_payload_fields)))
        )
    for field_name in payload:
        _validate_field(payload, field_name, action)
    _validate_action_combinations(action, payload)

    normalized = dict(message)
    normalized["requestId"] = request_id
    normalized["action"] = action
    normalized["payload"] = payload
    return normalized


STRICT_OUTPUT_ACTIONS = {
    "system.ack",
    "system.error",
    "system.pong",
    "device.list",
    "device.setVolume",
    "device.volume.update",
    "playback.context.list",
    "playback.context.ensure",
    "playback.context.prepare",
    "playback.context.prepared",
    "playback.context.status",
    "playback.context.closed",
    "playback.context.bindings.changed",
    "queue.context.sync",
    "playback.update",
    "playback.control.settled",
    "queue.playItem",
    "player.play",
    "player.pause",
    "player.seek",
    "player.next",
    "player.prev",
    "playback.prepare",
    "playback.handoff.release",
    "playback.handoff.status",
    "playback.handoff.cancel",
    "broadcast.start",
    "broadcast.play",
    "broadcast.pause",
    "broadcast.seek",
    "broadcast.playItem",
    "broadcast.queue.sync",
    "broadcast.stop",
}

_OUTPUT_ACTION_TYPES = {
    "system.ack": "system",
    "system.error": "system",
    "system.pong": "system",
    "device.list": "state",
    "device.setVolume": "command",
    "device.volume.update": "event",
    "playback.context.list": "state",
    "playback.context.ensure": "state",
    "playback.context.prepare": "command",
    "playback.context.prepared": "event",
    "playback.context.status": "state",
    "playback.context.closed": "event",
    "playback.context.bindings.changed": "event",
    "queue.context.sync": "state",
    "playback.update": "event",
    "playback.control.settled": "event",
    "queue.playItem": "command",
    "player.play": "command",
    "player.pause": "command",
    "player.seek": "command",
    "player.next": "command",
    "player.prev": "command",
    "playback.prepare": "command",
    "playback.handoff.release": "command",
    "playback.handoff.status": "state",
    "playback.handoff.cancel": "command",
    "broadcast.start": "command",
    "broadcast.play": "command",
    "broadcast.pause": "command",
    "broadcast.seek": "command",
    "broadcast.playItem": "command",
    "broadcast.queue.sync": "state",
    "broadcast.stop": "command",
}

_DIRECT_RESPONSE_ACTIONS = {
    "system.pong",
    "device.list",
    "playback.context.list",
    "playback.context.ensure",
}

_ACK_ONLY_REQUEST_ACTIONS = {
    "playback.context.subscribe",
    "playback.context.unsubscribe",
    "playback.context.close",
    "playback.context.prepare",
    "device.setVolume",
    "queue.context.sync",
    "queue.playItem",
    "player.play",
    "player.pause",
    "player.seek",
    "player.next",
    "player.prev",
    "follow.start",
    "follow.stop",
    "playback.handoff.cancel",
    "broadcast.play",
    "broadcast.pause",
    "broadcast.seek",
    "broadcast.playItem",
    "broadcast.queue.sync",
    "broadcast.stop",
}

_ERROR_CODES = {
    "bad_request",
    "unauthorized",
    "forbidden",
    "not_supported",
    "not_found",
    "context_closed",
    "authority_offline",
    "queue_required",
    "conflict",
    "stale_version",
    "client_sequence_conflict",
    "capability_required",
    "rate_limited",
    "internal_error",
}

_RETRYABLE_ERROR_CODES = {"authority_offline", "rate_limited", "internal_error"}


def _output_error(message: str) -> None:
    raise StrictOutputValidationError(message)


def _output_has_null(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_output_has_null(item) for item in value.values())
    if isinstance(value, list):
        return any(_output_has_null(item) for item in value)
    return False


def _output_object(
    value: object,
    required: Set[str],
    optional: Set[str],
    label: str,
) -> Dict[str, object]:
    if not isinstance(value, dict):
        _output_error("%s must be an object" % label)
    fields = set(value)
    missing = required - fields
    unknown = fields - required - optional
    if missing:
        _output_error("%s is missing fields: %s" % (label, ", ".join(sorted(missing))))
    if unknown:
        _output_error("%s has unknown fields: %s" % (label, ", ".join(sorted(unknown))))
    if _output_has_null(value):
        _output_error("%s must omit null values" % label)
    return value


def _output_string(value: object, label: str, maximum: int = MAX_ID_BYTES) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _output_error("%s must be a trimmed non-empty string" % label)
    if len(value.encode("utf-8")) > maximum:
        _output_error("%s exceeds %d UTF-8 bytes" % (label, maximum))
    return value


def _output_int(value: object, label: str, minimum: int = 0) -> int:
    if not _is_int(value) or value < minimum:
        _output_error("%s must be an integer >= %d" % (label, minimum))
    return value


def _output_number(value: object, label: str, positive: bool = False) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        _output_error("%s must be a number" % label)
    if positive and value <= 0:
        _output_error("%s must be greater than zero" % label)
    return value


def _output_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        _output_error("%s must be a boolean" % label)
    return value


def _output_string_array(
    value: object,
    label: str,
    non_empty: bool = False,
    sorted_values: bool = False,
) -> Tuple[str, ...]:
    if not isinstance(value, list) or (non_empty and not value):
        _output_error("%s must be %sa string array" % (label, "a non-empty " if non_empty else ""))
    normalized = tuple(_output_string(item, label) for item in value)
    if len(set(normalized)) != len(normalized):
        _output_error("%s must not contain duplicates" % label)
    if sorted_values and list(normalized) != sorted(normalized):
        _output_error("%s must be sorted" % label)
    return normalized


def _validate_output_capabilities(value: object, label: str) -> None:
    fields = set(value) if isinstance(value, dict) else set()
    if fields not in (set(BASE_STRICT_CAPABILITIES), set(STRICT_CAPABILITIES)):
        _output_error(
            "%s must contain the 9 base capabilities with optional "
            "remoteVolumeControl" % label
        )
    capabilities = _output_object(value, fields, set(), label)
    for capability in STRICT_CAPABILITIES:
        if capability not in capabilities:
            continue
        _output_bool(capabilities[capability], "%s.%s" % (label, capability))


def _validate_output_roles(value: object, label: str) -> None:
    roles = _output_string_array(value, label, non_empty=True)
    if roles not in (("player",), ("controller",), ("player", "controller")):
        _output_error("%s must use the canonical player/controller order" % label)


def _validate_output_device(value: object, label: str) -> None:
    device = _output_object(
        value,
        {"clientId", "deviceSessionId", "deviceName", "roles", "capabilities"},
        {"alias", "volumeState"},
        label,
    )
    for field_name in ("clientId", "deviceSessionId", "deviceName"):
        _output_string(device[field_name], "%s.%s" % (label, field_name))
    _validate_output_roles(device["roles"], label + ".roles")
    _validate_output_capabilities(device["capabilities"], label + ".capabilities")
    if "alias" in device:
        _output_string(device["alias"], label + ".alias")
    if "volumeState" in device:
        volume_state = _output_object(
            device["volumeState"],
            {"volume", "clientSeq", "serverUpdatedAtMs"},
            set(),
            label + ".volumeState",
        )
        volume = _output_int(volume_state["volume"], label + ".volumeState.volume")
        if volume > 100:
            _output_error("%s.volumeState.volume must be <= 100" % label)
        _output_int(volume_state["clientSeq"], label + ".volumeState.clientSeq", 1)
        _output_int(
            volume_state["serverUpdatedAtMs"],
            label + ".volumeState.serverUpdatedAtMs",
        )


def _validate_context_binding(value: object, label: str) -> Tuple[str, str, str]:
    binding = _output_object(
        value,
        {
            "playbackContextId",
            "authorityClientId",
            "authorityDeviceSessionId",
        },
        set(),
        label,
    )
    return tuple(
        _output_string(binding[field_name], "%s.%s" % (label, field_name))
        for field_name in (
            "playbackContextId",
            "authorityClientId",
            "authorityDeviceSessionId",
        )
    )


def _validate_context_snapshot(
    value: object,
    label: str,
    require_server_updated: bool = False,
) -> Dict[str, object]:
    required = {
        "playbackContextId",
        "authorityClientId",
        "queueSongIds",
        "state",
        "positionMs",
        "queueRevision",
        "controlVersion",
        "version",
        "epoch",
    }
    optional = {"currentIndex", "trackId", "timelineId", "serverUpdatedAtMs"}
    if require_server_updated:
        required.add("serverUpdatedAtMs")
        optional.remove("serverUpdatedAtMs")
    snapshot = _output_object(value, required, optional, label)
    for field_name in ("playbackContextId", "authorityClientId"):
        _output_string(snapshot[field_name], "%s.%s" % (label, field_name))
    queue = _output_string_array(
        snapshot["queueSongIds"],
        label + ".queueSongIds",
    )
    _output_int(snapshot["positionMs"], label + ".positionMs")
    if queue:
        if "currentIndex" not in snapshot or "trackId" not in snapshot:
            _output_error(
                "%s queue-backed snapshot requires currentIndex and trackId" % label
            )
        current_index = _output_int(
            snapshot["currentIndex"], label + ".currentIndex"
        )
        if current_index >= len(queue):
            _output_error("%s.currentIndex is outside queueSongIds" % label)
        if snapshot["state"] not in {"playing", "paused", "stopped"}:
            _output_error("%s.state is invalid" % label)
        _output_string(snapshot["trackId"], label + ".trackId")
        if snapshot["trackId"] != queue[current_index]:
            _output_error("%s.trackId must match the current queue item" % label)
    else:
        if "currentIndex" in snapshot or "trackId" in snapshot:
            _output_error(
                "%s idle snapshot forbids currentIndex and trackId" % label
            )
        if snapshot["state"] != "idle" or snapshot["positionMs"] != 0:
            _output_error(
                "%s idle snapshot requires state idle and positionMs 0" % label
            )
    for field_name in ("queueRevision", "controlVersion", "version", "epoch"):
        _output_int(snapshot[field_name], "%s.%s" % (label, field_name), 1)
    if "timelineId" in snapshot:
        _output_string(snapshot["timelineId"], label + ".timelineId")
    if "serverUpdatedAtMs" in snapshot:
        _output_int(snapshot["serverUpdatedAtMs"], label + ".serverUpdatedAtMs")
    return snapshot


def _validate_device_state(value: object, playback_context_id: str, label: str) -> str:
    state = _output_object(
        value,
        {
            "playbackContextId",
            "clientId",
            "deviceSessionId",
            "state",
            "positionMs",
            "appliedControlVersion",
            "clientSeq",
            "serverUpdatedAtMs",
        },
        {"trackId", "volume", "muted"},
        label,
    )
    for field_name in ("playbackContextId", "clientId", "deviceSessionId"):
        _output_string(state[field_name], "%s.%s" % (label, field_name))
    if state["playbackContextId"] != playback_context_id:
        _output_error("%s.playbackContextId does not match the Context" % label)
    if state["state"] not in {"idle", "playing", "paused", "stopped"}:
        _output_error("%s.state is invalid" % label)
    _output_int(state["positionMs"], label + ".positionMs")
    _output_int(
        state["appliedControlVersion"],
        label + ".appliedControlVersion",
        1,
    )
    _output_int(state["clientSeq"], label + ".clientSeq", 1)
    _output_int(state["serverUpdatedAtMs"], label + ".serverUpdatedAtMs")
    if "trackId" in state:
        _output_string(state["trackId"], label + ".trackId")
    if state["state"] == "idle":
        if state["positionMs"] != 0 or "trackId" in state:
            _output_error(
                "%s idle state requires positionMs 0 and forbids trackId" % label
            )
    elif "trackId" not in state:
        _output_error("%s non-idle state requires trackId" % label)
    if "volume" in state:
        volume = _output_int(state["volume"], label + ".volume")
        if volume > 100:
            _output_error("%s.volume must be <= 100" % label)
    if "muted" in state:
        _output_bool(state["muted"], label + ".muted")
    return state["clientId"]


def _validate_registration_ack(payload: Dict[str, object]) -> None:
    ack = _output_object(
        payload,
        {
            "action",
            "clientId",
            "deviceSessionId",
            "negotiatedCapabilities",
            "strictV2",
        },
        set(),
        "system.ack payload",
    )
    for field_name in ("clientId", "deviceSessionId"):
        _output_string(ack[field_name], "system.ack payload.%s" % field_name)
    _validate_output_capabilities(
        ack["negotiatedCapabilities"],
        "system.ack payload.negotiatedCapabilities",
    )
    metadata = _output_object(
        ack["strictV2"],
        {
            "protocolVersion",
            "schemaHash",
            "serverBuildCommit",
            "connectionNonce",
            "connectionEpoch",
        },
        set(),
        "system.ack payload.strictV2",
    )
    _output_string(metadata["protocolVersion"], "strictV2.protocolVersion")
    schema_hash = _output_string(metadata["schemaHash"], "strictV2.schemaHash")
    if re.fullmatch(r"[0-9a-f]{64}", schema_hash) is None:
        _output_error("strictV2.schemaHash must be lowercase SHA-256")
    _output_string(metadata["serverBuildCommit"], "strictV2.serverBuildCommit")
    _output_string(metadata["connectionNonce"], "strictV2.connectionNonce")
    if metadata["connectionEpoch"] != 1 or isinstance(metadata["connectionEpoch"], bool):
        _output_error("strictV2.connectionEpoch must be integer 1")


def _validate_broadcast_snapshot(
    value: object,
    label: str,
    timed: bool = False,
) -> Dict[str, object]:
    required = {
        "playbackContextId",
        "broadcastId",
        "ownerClientId",
        "authorityClientId",
        "queueSongIds",
        "currentIndex",
        "positionMs",
        "state",
        "version",
        "queueRevision",
        "controlVersion",
        "epoch",
        "serverUpdatedAtMs",
        "playbackRate",
        "participants",
    }
    optional = {"trackId"}
    if timed:
        required.update({"effectiveAtServerMs", "serverTimeMs"})
    snapshot = _output_object(value, required, optional, label)
    for field_name in (
        "playbackContextId",
        "broadcastId",
        "ownerClientId",
        "authorityClientId",
    ):
        _output_string(snapshot[field_name], "%s.%s" % (label, field_name))
    queue = _output_string_array(
        snapshot["queueSongIds"],
        label + ".queueSongIds",
        non_empty=True,
    )
    current_index = _output_int(snapshot["currentIndex"], label + ".currentIndex")
    if current_index >= len(queue):
        _output_error("%s.currentIndex is outside queueSongIds" % label)
    if snapshot["state"] not in {"playing", "paused", "stopped"}:
        _output_error("%s.state is invalid" % label)
    _output_int(snapshot["positionMs"], label + ".positionMs")
    for field_name in ("version", "queueRevision", "controlVersion", "epoch"):
        _output_int(snapshot[field_name], "%s.%s" % (label, field_name), 1)
    _output_int(snapshot["serverUpdatedAtMs"], label + ".serverUpdatedAtMs")
    _output_number(snapshot["playbackRate"], label + ".playbackRate", positive=True)
    participants = _output_string_array(
        snapshot["participants"],
        label + ".participants",
        non_empty=True,
        sorted_values=True,
    )
    if snapshot["authorityClientId"] not in participants:
        _output_error("%s.participants must include authorityClientId" % label)
    if "trackId" in snapshot:
        _output_string(snapshot["trackId"], label + ".trackId")
        if snapshot["trackId"] != queue[current_index]:
            _output_error("%s.trackId must match the current queue item" % label)
    if timed:
        _output_int(snapshot["effectiveAtServerMs"], label + ".effectiveAtServerMs", 1)
        _output_int(snapshot["serverTimeMs"], label + ".serverTimeMs", 1)
    return snapshot


def _validate_broadcast_status_ack(payload: Dict[str, object]) -> None:
    status = _output_object(
        payload,
        {"action", "broadcast", "participantStates"},
        set(),
        "broadcast.status ACK payload",
    )
    broadcast = _validate_broadcast_snapshot(
        status["broadcast"],
        "broadcast.status ACK payload.broadcast",
    )
    participant_states = status["participantStates"]
    if not isinstance(participant_states, list):
        _output_error("broadcast.status participantStates must be an array")
    client_ids = []
    for index, value in enumerate(participant_states):
        label = "broadcast.status participantStates[%d]" % index
        participant = _output_object(
            value,
            {"broadcastId", "clientId", "state", "positionMs", "online"},
            {"clientSeq", "serverUpdatedAtMs"},
            label,
        )
        _output_string(participant["broadcastId"], label + ".broadcastId")
        client_id = _output_string(participant["clientId"], label + ".clientId")
        if participant["broadcastId"] != broadcast["broadcastId"]:
            _output_error("%s.broadcastId does not match" % label)
        if participant["state"] not in {"playing", "paused", "stopped"}:
            _output_error("%s.state is invalid" % label)
        _output_int(participant["positionMs"], label + ".positionMs")
        _output_bool(participant["online"], label + ".online")
        has_client_seq = "clientSeq" in participant
        has_updated = "serverUpdatedAtMs" in participant
        if has_client_seq != has_updated:
            _output_error("%s feedback fields must appear together" % label)
        if has_client_seq:
            _output_int(participant["clientSeq"], label + ".clientSeq", 1)
            _output_int(participant["serverUpdatedAtMs"], label + ".serverUpdatedAtMs")
        client_ids.append(client_id)
    if client_ids != list(broadcast["participants"]):
        _output_error("broadcast.status participantStates must cover sorted participants")


def _validate_output_ack(payload: object) -> str:
    if not isinstance(payload, dict):
        _output_error("system.ack payload must be an object")
    request_action = _output_string(payload.get("action"), "system.ack payload.action", MAX_ACTION_BYTES)
    if request_action == "auth.login":
        ack = _output_object(
            payload,
            {"action", "authenticated", "userName"},
            set(),
            "auth.login ACK payload",
        )
        if ack["authenticated"] is not True:
            _output_error("auth.login ACK authenticated must be true")
        _output_string(ack["userName"], "auth.login ACK userName")
    elif request_action == "device.register":
        _validate_registration_ack(payload)
    elif request_action == "playback.context.prepare":
        ack = _output_object(
            payload,
            {"action", "intentId", "status", "controlVersion"},
            set(),
            "playback.context.prepare ACK payload",
        )
        _output_string(ack["intentId"], "playback.context.prepare ACK intentId")
        if ack["status"] not in {"preparing", "ready"}:
            _output_error("playback.context.prepare ACK status is invalid")
        _output_int(
            ack["controlVersion"],
            "playback.context.prepare ACK controlVersion",
            1,
        )
    elif request_action == "playback.handoff.start":
        ack = _output_object(
            payload,
            {"action", "handoffId", "prepareId", "status", "controlVersion"},
            set(),
            "playback.handoff.start ACK payload",
        )
        _output_string(ack["handoffId"], "handoff start handoffId")
        _output_string(ack["prepareId"], "handoff start prepareId")
        if ack["status"] != "preparing":
            _output_error("handoff start status must be preparing")
        _output_int(ack["controlVersion"], "handoff start controlVersion", 1)
    elif request_action == "broadcast.start":
        ack = _output_object(
            payload,
            {"action", "started", "broadcastId", "participants", "skippedClientIds"},
            set(),
            "broadcast.start ACK payload",
        )
        if ack["started"] is not True:
            _output_error("broadcast.start ACK started must be true")
        _output_string(ack["broadcastId"], "broadcast.start ACK broadcastId")
        _output_string_array(
            ack["participants"],
            "broadcast.start ACK participants",
            non_empty=True,
            sorted_values=True,
        )
        _output_string_array(
            ack["skippedClientIds"],
            "broadcast.start ACK skippedClientIds",
            sorted_values=True,
        )
    elif request_action == "broadcast.status":
        _validate_broadcast_status_ack(payload)
    elif request_action in _ACK_ONLY_REQUEST_ACTIONS:
        _output_object(payload, {"action"}, set(), "%s ACK payload" % request_action)
    else:
        _output_error("No strict ACK schema exists for %s" % request_action)
    return request_action


def _validate_output_error(payload: object) -> str:
    error = _output_object(
        payload,
        {"action", "code", "message", "retryable"},
        {
            "playbackContextId",
            "currentControlVersion",
            "currentQueueRevision",
            "currentVersion",
            "currentClientSeq",
            "retryAfterMs",
        },
        "system.error payload",
    )
    request_action = _output_string(error["action"], "system.error payload.action", MAX_ACTION_BYTES)
    code = _output_string(error["code"], "system.error payload.code")
    if code not in _ERROR_CODES:
        _output_error("system.error code is not part of strict-v2")
    _output_string(error["message"], "system.error payload.message", 512)
    retryable = _output_bool(error["retryable"], "system.error payload.retryable")
    if retryable != (code in _RETRYABLE_ERROR_CODES):
        _output_error("system.error retryable does not match code")
    if "playbackContextId" in error:
        _output_string(error["playbackContextId"], "system.error playbackContextId")
    for field_name in (
        "currentControlVersion",
        "currentQueueRevision",
        "currentVersion",
        "currentClientSeq",
    ):
        if field_name in error:
            _output_int(error[field_name], "system.error %s" % field_name, 1)
    if "retryAfterMs" in error:
        _output_int(error["retryAfterMs"], "system.error retryAfterMs", 1)
    if code in {"context_closed", "authority_offline"} and "playbackContextId" not in error:
        _output_error("%s requires playbackContextId" % code)
    if code == "queue_required":
        required = {
            "playbackContextId",
            "currentControlVersion",
            "currentQueueRevision",
            "currentVersion",
        }
        if not required.issubset(error):
            _output_error("queue_required requires Context and all canonical cursors")
    if code == "rate_limited" and "retryAfterMs" not in error:
        _output_error("rate_limited requires retryAfterMs")
    if code == "client_sequence_conflict" and "currentClientSeq" not in error:
        _output_error("client_sequence_conflict requires currentClientSeq")
    if code == "stale_version" and not any(
        field_name in error
        for field_name in ("currentControlVersion", "currentQueueRevision", "currentVersion")
    ):
        _output_error("stale_version requires a current cursor")
    return request_action


def _validate_playback_update_output(payload: object) -> None:
    required = {
        "playbackContextId",
        "sourceClientId",
        "deviceSessionId",
        "origin",
        "controlVersion",
        "appliedControlVersion",
        "state",
        "positionMs",
        "clientSeq",
        "serverUpdatedAtMs",
    }
    optional = {
        "trackId",
        "volume",
        "muted",
        "executionStatus",
        "commandControlVersion",
        "intentId",
        "supersededThroughControlVersion",
        "queueIndex",
        "errorCode",
        "errorMessage",
    }
    update = _output_object(
        payload,
        required,
        optional,
        "playback.update payload",
    )
    for field_name in ("playbackContextId", "sourceClientId", "deviceSessionId"):
        _output_string(update[field_name], "playback.update %s" % field_name)
    if update["origin"] not in {"passive", "remoteCommand", "localUser"}:
        _output_error("playback.update origin is invalid")
    control_version = _output_int(
        update["controlVersion"], "playback.update controlVersion", 1
    )
    applied_version = _output_int(
        update["appliedControlVersion"],
        "playback.update appliedControlVersion",
        1,
    )
    if applied_version > control_version:
        _output_error("playback.update appliedControlVersion exceeds controlVersion")
    if update["state"] not in {"idle", "playing", "paused", "stopped"}:
        _output_error("playback.update state is invalid")
    _output_int(update["positionMs"], "playback.update positionMs")
    _output_int(update["clientSeq"], "playback.update clientSeq", 1)
    _output_int(update["serverUpdatedAtMs"], "playback.update serverUpdatedAtMs")
    if "volume" in update:
        volume = _output_int(update["volume"], "playback.update volume")
        if volume > 100:
            _output_error("playback.update volume must be <= 100")
    if "muted" in update:
        _output_bool(update["muted"], "playback.update muted")
    if update["state"] == "idle":
        if update["positionMs"] != 0 or "trackId" in update:
            _output_error(
                "idle playback.update requires positionMs 0 and forbids trackId"
            )
    else:
        if "trackId" not in update:
            _output_error("non-idle playback.update requires trackId")
        _output_string(update["trackId"], "playback.update trackId")

    shape_fields = set(update) - required - {"trackId", "volume", "muted"}
    if update["origin"] == "passive":
        if shape_fields:
            _output_error("passive playback.update contains transaction fields")
        return

    if update["origin"] == "remoteCommand":
        remote_required = {"executionStatus", "commandControlVersion"}
        if not remote_required.issubset(update):
            _output_error("remoteCommand playback.update is missing transaction fields")
        if shape_fields - remote_required - {"errorCode", "errorMessage"}:
            _output_error("remoteCommand playback.update contains forbidden fields")
        if update["executionStatus"] not in {"committed", "failed"}:
            _output_error("remoteCommand executionStatus is invalid")
        command_version = _output_int(
            update["commandControlVersion"],
            "playback.update commandControlVersion",
            1,
        )
        if command_version > control_version:
            _output_error("commandControlVersion exceeds controlVersion")
        if update["executionStatus"] == "committed":
            if applied_version != command_version:
                _output_error(
                    "committed appliedControlVersion must equal commandControlVersion"
                )
            if "errorCode" in update or "errorMessage" in update:
                _output_error("committed playback.update forbids error fields")
        else:
            if applied_version >= command_version:
                _output_error(
                    "failed appliedControlVersion must be below commandControlVersion"
                )
            if update.get("errorCode") not in {
                "playback_failed",
                "track_load_failed",
                "seek_failed",
                "execution_timeout",
            }:
                _output_error("failed playback.update errorCode is invalid")
            if "errorMessage" in update:
                _output_string(
                    update["errorMessage"],
                    "playback.update errorMessage",
                    512,
                )
        return

    local_required = {
        "executionStatus",
        "intentId",
        "supersededThroughControlVersion",
        "queueIndex",
    }
    if not local_required.issubset(update):
        _output_error("localUser playback.update is missing transaction fields")
    if shape_fields != local_required:
        _output_error("localUser playback.update contains forbidden fields")
    if update["executionStatus"] != "committed":
        _output_error("localUser playback.update must be committed")
    if update["state"] == "idle" or applied_version != control_version:
        _output_error(
            "localUser playback.update must be non-idle and fully applied"
        )
    _output_string(update["intentId"], "playback.update intentId")
    superseded = _output_int(
        update["supersededThroughControlVersion"],
        "playback.update supersededThroughControlVersion",
    )
    if superseded >= control_version:
        _output_error("supersededThroughControlVersion must be below controlVersion")
    _output_int(update["queueIndex"], "playback.update queueIndex")


def _validate_control_settled_output(payload: object) -> None:
    settled = _output_object(
        payload,
        {
            "playbackContextId",
            "epoch",
            "commandControlVersion",
            "status",
            "errorCode",
            "controlVersion",
            "appliedControlVersion",
            "requestingClientId",
            "serverUpdatedAtMs",
        },
        {"dependsOnControlVersion", "errorMessage"},
        "playback.control.settled payload",
    )
    for field_name in ("playbackContextId", "requestingClientId"):
        _output_string(settled[field_name], "playback.control.settled %s" % field_name)
    if settled["status"] != "failed":
        _output_error("playback.control.settled status must be failed")
    if settled["errorCode"] not in {"dependency_failed", "execution_unknown"}:
        _output_error("playback.control.settled errorCode is invalid")
    _output_int(settled["epoch"], "playback.control.settled epoch", 1)
    command_version = _output_int(
        settled["commandControlVersion"],
        "playback.control.settled commandControlVersion",
        1,
    )
    control_version = _output_int(
        settled["controlVersion"],
        "playback.control.settled controlVersion",
        1,
    )
    applied_version = _output_int(
        settled["appliedControlVersion"],
        "playback.control.settled appliedControlVersion",
        1,
    )
    if command_version > control_version or applied_version > control_version:
        _output_error("playback.control.settled cursors are inconsistent")
    if settled["errorCode"] == "dependency_failed":
        if "dependsOnControlVersion" not in settled:
            _output_error("dependency_failed requires dependsOnControlVersion")
        dependency = _output_int(
            settled["dependsOnControlVersion"],
            "playback.control.settled dependsOnControlVersion",
            1,
        )
        if dependency >= command_version:
            _output_error("dependsOnControlVersion must be below commandControlVersion")
    elif "dependsOnControlVersion" in settled:
        _output_error("execution_unknown forbids dependsOnControlVersion")
    if "errorMessage" in settled:
        _output_string(
            settled["errorMessage"],
            "playback.control.settled errorMessage",
            512,
        )
    _output_int(
        settled["serverUpdatedAtMs"],
        "playback.control.settled serverUpdatedAtMs",
    )


def _validate_output_payload(action: str, payload: object) -> Optional[str]:
    if action == "system.ack":
        return _validate_output_ack(payload)
    if action == "system.error":
        return _validate_output_error(payload)
    if action == "system.pong":
        pong = _output_object(payload, set(), {"serverTimeMs"}, "system.pong payload")
        if "serverTimeMs" in pong:
            _output_int(pong["serverTimeMs"], "system.pong serverTimeMs", 1)
        return None
    if action == "device.list":
        response = _output_object(payload, {"devices"}, set(), "device.list payload")
        if not isinstance(response["devices"], list):
            _output_error("device.list devices must be an array")
        client_ids = []
        for index, device in enumerate(response["devices"]):
            _validate_output_device(device, "device.list devices[%d]" % index)
            client_ids.append(device["clientId"])
        if client_ids != sorted(client_ids) or len(set(client_ids)) != len(client_ids):
            _output_error("device.list devices must be uniquely sorted by clientId")
        return None
    if action == "device.setVolume":
        command = _output_object(
            payload,
            {"sourceClientId", "volume"},
            set(),
            "device.setVolume payload",
        )
        _output_string(command["sourceClientId"], "device.setVolume sourceClientId")
        volume = _output_int(command["volume"], "device.setVolume volume")
        if volume > 100:
            _output_error("device.setVolume volume must be <= 100")
        return None
    if action == "device.volume.update":
        update = _output_object(
            payload,
            {
                "sourceClientId",
                "deviceSessionId",
                "volume",
                "clientSeq",
                "serverUpdatedAtMs",
            },
            set(),
            "device.volume.update payload",
        )
        _output_string(
            update["sourceClientId"],
            "device.volume.update sourceClientId",
        )
        _output_string(
            update["deviceSessionId"],
            "device.volume.update deviceSessionId",
        )
        volume = _output_int(update["volume"], "device.volume.update volume")
        if volume > 100:
            _output_error("device.volume.update volume must be <= 100")
        _output_int(update["clientSeq"], "device.volume.update clientSeq", 1)
        _output_int(
            update["serverUpdatedAtMs"],
            "device.volume.update serverUpdatedAtMs",
        )
        return None
    if action == "playback.context.list":
        response = _output_object(
            payload,
            {"contexts"},
            set(),
            "playback.context.list payload",
        )
        if not isinstance(response["contexts"], list):
            _output_error("playback.context.list contexts must be an array")
        bindings = [
            _validate_context_binding(
                binding,
                "playback.context.list contexts[%d]" % index,
            )
            for index, binding in enumerate(response["contexts"])
        ]
        context_ids = [binding[0] for binding in bindings]
        if context_ids != sorted(context_ids) or len(set(context_ids)) != len(context_ids):
            _output_error(
                "playback.context.list contexts must be uniquely sorted by playbackContextId"
            )
        authority_pairs = {(binding[1], binding[2]) for binding in bindings}
        if len(authority_pairs) > 1:
            _output_error(
                "playback.context.list contexts must use one authority/device pair"
            )
        return None
    if action == "playback.context.ensure":
        _validate_context_snapshot(payload, "playback.context.ensure payload")
        return None
    if action == "playback.context.prepare":
        prepare = _output_object(
            payload,
            {"playbackContextId", "intentId", "controlVersion", "sourceClientId"},
            {"initialQueueSongIds", "currentIndex", "positionMs"},
            "playback.context.prepare payload",
        )
        for field_name in ("playbackContextId", "intentId", "sourceClientId"):
            _output_string(prepare[field_name], "playback.context.prepare %s" % field_name)
        _output_int(
            prepare["controlVersion"],
            "playback.context.prepare controlVersion",
            1,
        )
        has_queue = "initialQueueSongIds" in prepare
        if has_queue != ("currentIndex" in prepare) or has_queue != (
            "positionMs" in prepare
        ):
            _output_error(
                "playback.context.prepare initial queue fields must appear together"
            )
        if has_queue:
            queue = _output_string_array(
                prepare["initialQueueSongIds"],
                "playback.context.prepare initialQueueSongIds",
                non_empty=True,
            )
            current_index = _output_int(
                prepare["currentIndex"],
                "playback.context.prepare currentIndex",
            )
            if current_index >= len(queue):
                _output_error(
                    "playback.context.prepare currentIndex is outside initialQueueSongIds"
                )
            _output_int(
                prepare["positionMs"],
                "playback.context.prepare positionMs",
            )
        return None
    if action == "playback.context.prepared":
        prepared = _output_object(
            payload,
            {"playbackContextId", "intentId", "ready", "controlVersion"},
            {"errorCode", "errorMessage"},
            "playback.context.prepared payload",
        )
        _output_string(
            prepared["playbackContextId"],
            "playback.context.prepared playbackContextId",
        )
        _output_string(prepared["intentId"], "playback.context.prepared intentId")
        _output_bool(prepared["ready"], "playback.context.prepared ready")
        _output_int(
            prepared["controlVersion"],
            "playback.context.prepared controlVersion",
            1,
        )
        if prepared["ready"]:
            if "errorCode" in prepared or "errorMessage" in prepared:
                _output_error("prepared ready:true forbids error fields")
        else:
            if prepared.get("errorCode") not in {
                "queue_required",
                "restore_failed",
                "prepare_timeout",
                "authority_changed",
            }:
                _output_error("prepared ready:false errorCode is invalid")
            if "errorMessage" in prepared:
                _output_string(
                    prepared["errorMessage"],
                    "playback.context.prepared errorMessage",
                    512,
                )
        return None
    if action == "playback.context.status":
        status = _output_object(
            payload,
            {"playbackContext", "deviceStates"},
            set(),
            "playback.context.status payload",
        )
        context = _validate_context_snapshot(
            status["playbackContext"],
            "playback.context.status playbackContext",
        )
        if not isinstance(status["deviceStates"], list):
            _output_error("playback.context.status deviceStates must be an array")
        client_ids = []
        device_session_ids = []
        for index, device_state in enumerate(status["deviceStates"]):
            client_ids.append(
                _validate_device_state(
                device_state,
                context["playbackContextId"],
                "playback.context.status deviceStates[%d]" % index,
            )
            )
            device_session_ids.append(device_state["deviceSessionId"])
            if device_state["appliedControlVersion"] > context["controlVersion"]:
                _output_error(
                    "deviceState appliedControlVersion exceeds Context controlVersion"
                )
        if client_ids != sorted(client_ids) or len(set(client_ids)) != len(client_ids):
            _output_error("playback.context.status deviceStates must be uniquely sorted")
        if len(set(device_session_ids)) != len(device_session_ids):
            _output_error(
                "playback.context.status deviceSessionId values must be unique"
            )
        return None
    if action == "playback.context.closed":
        closed = _output_object(
            payload,
            {"playbackContextId"},
            set(),
            "playback.context.closed payload",
        )
        _output_string(closed["playbackContextId"], "closed playbackContextId")
        return None
    if action == "playback.context.bindings.changed":
        changed = _output_object(
            payload,
            {"authorityClientId", "authorityDeviceSessionId"},
            set(),
            "playback.context.bindings.changed payload",
        )
        _output_string(
            changed["authorityClientId"],
            "bindings changed authorityClientId",
        )
        _output_string(
            changed["authorityDeviceSessionId"],
            "bindings changed authorityDeviceSessionId",
        )
        return None
    if action == "queue.context.sync":
        _validate_context_snapshot(
            payload,
            "queue.context.sync payload",
        )
        return None
    if action == "playback.update":
        _validate_playback_update_output(payload)
        return None
    if action == "playback.control.settled":
        _validate_control_settled_output(payload)
        return None
    if action in {"player.play", "player.pause", "player.seek", "player.next", "player.prev"}:
        if action == "player.play" and isinstance(payload, dict) and "handoffId" in payload:
            control = _output_object(
                payload,
                {
                    "playbackContextId",
                    "handoffId",
                    "controlVersion",
                    "sourceClientId",
                    "effectiveAtServerMs",
                    "positionMs",
                },
                set(),
                "handoff commit payload",
            )
            _output_string(control["handoffId"], "handoff commit handoffId")
            _output_int(control["effectiveAtServerMs"], "handoff commit effectiveAtServerMs", 1)
        else:
            required = {
                "playbackContextId",
                "controlVersion",
                "sourceClientId",
                "executionTimeoutMs",
            }
            optional = {"positionMs"} if action in {"player.play", "player.pause"} else set()
            if action == "player.seek":
                required.add("positionMs")
            control = _output_object(payload, required, optional, "%s payload" % action)
        _output_string(control["playbackContextId"], "%s playbackContextId" % action)
        _output_string(control["sourceClientId"], "%s sourceClientId" % action)
        _output_int(control["controlVersion"], "%s controlVersion" % action, 1)
        if "executionTimeoutMs" in control:
            _output_int(
                control["executionTimeoutMs"],
                "%s executionTimeoutMs" % action,
                1,
            )
        if "positionMs" in control:
            _output_int(control["positionMs"], "%s positionMs" % action)
        return None
    if action == "queue.playItem":
        control = _output_object(
            payload,
            {
                "playbackContextId",
                "queueSongIds",
                "queueIndex",
                "queueRevision",
                "controlVersion",
                "sourceClientId",
                "executionTimeoutMs",
            },
            set(),
            "queue.playItem payload",
        )
        _output_string(control["playbackContextId"], "queue.playItem playbackContextId")
        queue = _output_string_array(
            control["queueSongIds"],
            "queue.playItem queueSongIds",
            non_empty=True,
        )
        queue_index = _output_int(control["queueIndex"], "queue.playItem queueIndex")
        if queue_index >= len(queue):
            _output_error("queue.playItem queueIndex is outside queueSongIds")
        _output_int(control["queueRevision"], "queue.playItem queueRevision", 1)
        _output_int(control["controlVersion"], "queue.playItem controlVersion", 1)
        _output_int(
            control["executionTimeoutMs"],
            "queue.playItem executionTimeoutMs",
            1,
        )
        _output_string(control["sourceClientId"], "queue.playItem sourceClientId")
        return None
    if action == "playback.prepare":
        prepare = _output_object(
            payload,
            {
                "playbackContextId",
                "handoffId",
                "prepareId",
                "sourceClientId",
                "authorityClientId",
                "deviceSessionId",
                "queueSongIds",
                "currentIndex",
                "positionMs",
                "controlVersion",
            },
            {"trackId", "timelineId"},
            "playback.prepare payload",
        )
        for field_name in (
            "playbackContextId",
            "handoffId",
            "prepareId",
            "sourceClientId",
            "authorityClientId",
            "deviceSessionId",
        ):
            _output_string(prepare[field_name], "playback.prepare %s" % field_name)
        queue = _output_string_array(
            prepare["queueSongIds"],
            "playback.prepare queueSongIds",
            non_empty=True,
        )
        current_index = _output_int(prepare["currentIndex"], "playback.prepare currentIndex")
        if current_index >= len(queue):
            _output_error("playback.prepare currentIndex is outside queueSongIds")
        _output_int(prepare["positionMs"], "playback.prepare positionMs")
        _output_int(prepare["controlVersion"], "playback.prepare controlVersion", 1)
        if "trackId" in prepare and prepare["trackId"] != queue[current_index]:
            _output_error("playback.prepare trackId must match current queue item")
        if "timelineId" in prepare:
            _output_string(prepare["timelineId"], "playback.prepare timelineId")
        return None
    if action == "playback.handoff.release":
        release = _output_object(
            payload,
            {
                "playbackContextId",
                "handoffId",
                "instruction",
                "controlVersion",
                "newAuthorityClientId",
            },
            set(),
            "playback.handoff.release payload",
        )
        if release["instruction"] != "pause":
            _output_error("playback.handoff.release instruction must be pause")
        for field_name in ("playbackContextId", "handoffId", "newAuthorityClientId"):
            _output_string(release[field_name], "handoff release %s" % field_name)
        _output_int(release["controlVersion"], "handoff release controlVersion", 1)
        return None
    if action == "playback.handoff.status":
        status = _output_object(
            payload,
            {"playbackContextId", "handoffId", "status", "controlVersion"},
            {"sourceClientId", "newAuthorityClientId", "errorCode", "errorMessage"},
            "playback.handoff.status payload",
        )
        for field_name in ("playbackContextId", "handoffId"):
            _output_string(status[field_name], "handoff status %s" % field_name)
        _output_int(status["controlVersion"], "handoff status controlVersion", 1)
        state_name = status["status"]
        if state_name not in {
            "preparing",
            "ready",
            "committing",
            "completed",
            "failed",
            "cancelled",
            "timedOut",
        }:
            _output_error("playback.handoff.status status is invalid")
        if "sourceClientId" in status:
            _output_string(status["sourceClientId"], "handoff status sourceClientId")
        if state_name == "completed":
            if "newAuthorityClientId" not in status:
                _output_error("completed handoff status requires newAuthorityClientId")
            _output_string(status["newAuthorityClientId"], "handoff status newAuthorityClientId")
        elif "newAuthorityClientId" in status:
            _output_error("newAuthorityClientId is only allowed for completed status")
        if state_name in {"failed", "timedOut"}:
            if "errorCode" not in status:
                _output_error("failed/timedOut handoff status requires errorCode")
        elif "errorCode" in status or "errorMessage" in status:
            _output_error("handoff error fields are only allowed for failed/timedOut")
        if "errorCode" in status:
            error_code = _output_string(status["errorCode"], "handoff status errorCode")
            if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) is None:
                _output_error("handoff status errorCode is invalid")
        if "errorMessage" in status:
            _output_string(status["errorMessage"], "handoff status errorMessage", 512)
        return None
    if action == "playback.handoff.cancel":
        cancel = _output_object(
            payload,
            {"playbackContextId", "handoffId", "reason", "controlVersion"},
            {"errorCode", "errorMessage"},
            "playback.handoff.cancel payload",
        )
        for field_name in ("playbackContextId", "handoffId", "reason"):
            _output_string(cancel[field_name], "handoff cancel %s" % field_name)
        _output_int(cancel["controlVersion"], "handoff cancel controlVersion", 1)
        if "errorCode" in cancel:
            _output_string(cancel["errorCode"], "handoff cancel errorCode")
        if "errorMessage" in cancel:
            _output_string(cancel["errorMessage"], "handoff cancel errorMessage", 512)
        return None
    if action in {
        "broadcast.start",
        "broadcast.play",
        "broadcast.pause",
        "broadcast.seek",
        "broadcast.playItem",
        "broadcast.queue.sync",
        "broadcast.stop",
    }:
        _validate_broadcast_snapshot(
            payload,
            "%s payload" % action,
            timed=action in {
                "broadcast.play",
                "broadcast.pause",
                "broadcast.seek",
                "broadcast.playItem",
            },
        )
        if action == "broadcast.stop" and payload["state"] != "stopped":
            _output_error("broadcast.stop state must be stopped")
        return None
    _output_error("No strict output payload schema exists for %s" % action)
    return None


def validate_strict_output(
    message: object,
    registered: Optional[bool] = None,
) -> Dict[str, object]:
    envelope = _output_object(
        message,
        {"type", "action", "payload", "timestamp"},
        {"requestId", "connectionNonce", "connectionEpoch"},
        "strict output envelope",
    )
    action = _output_string(envelope["action"], "strict output action", MAX_ACTION_BYTES)
    if action not in STRICT_OUTPUT_ACTIONS:
        _output_error("Action is not part of the strict-v2 server output surface")
    if envelope["type"] != _OUTPUT_ACTION_TYPES[action]:
        _output_error("strict output type does not match action %s" % action)
    _output_number(envelope["timestamp"], "strict output timestamp")
    request_action = _validate_output_payload(action, envelope["payload"])

    correlated = action in {"system.ack", "system.error"} or action in _DIRECT_RESPONSE_ACTIONS
    if action == "playback.context.status":
        correlated = "requestId" in envelope
    if correlated:
        if "requestId" not in envelope:
            _output_error("correlated strict output requires requestId")
        _output_string(envelope["requestId"], "strict output requestId")
    elif "requestId" in envelope:
        _output_error("strict business push must omit requestId")

    has_nonce = "connectionNonce" in envelope
    has_epoch = "connectionEpoch" in envelope
    if has_nonce != has_epoch:
        _output_error("connectionNonce and connectionEpoch must appear together")
    if registered is True and not has_nonce:
        _output_error("registered strict output requires connection provenance")
    if registered is False and has_nonce:
        _output_error("pre-register strict output must omit connection provenance")
    if registered is None:
        bootstrap = action in {"system.ack", "system.error"} and request_action in {
            "auth.login",
            "device.register",
        }
        if not bootstrap and not has_nonce:
            _output_error("registered strict output requires connection provenance")
    if has_nonce:
        _output_string(envelope["connectionNonce"], "strict output connectionNonce")
        if envelope["connectionEpoch"] != 1 or isinstance(envelope["connectionEpoch"], bool):
            _output_error("strict output connectionEpoch must be integer 1")
    if _contains_key(envelope, {"sessionId", "sourceSessionId", "targetClientId"}):
        _output_error("strict output contains a forbidden transport field")
    return deepcopy(envelope)


def is_strict_registration_request(message: object) -> bool:
    if not isinstance(message, dict) or message.get("action") != "device.register":
        return False
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return False
    capabilities = payload.get("capabilities")
    return isinstance(capabilities, dict) and capabilities.get("playbackContextV2") is True
