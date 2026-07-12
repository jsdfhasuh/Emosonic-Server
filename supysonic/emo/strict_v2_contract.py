from copy import deepcopy
from typing import Dict, NamedTuple, Optional, Set, Tuple


MAX_ID_BYTES = 128
MAX_ACTION_BYTES = 64
MAX_QUEUE_ITEMS = 1000
MAX_PARTICIPANTS = 100

STRICT_CAPABILITIES = (
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


class ActionSchema(NamedTuple):
    message_type: str
    required: Tuple[str, ...]
    optional: Tuple[str, ...] = ()


class StrictRequestValidationError(ValueError):
    def __init__(self, message: str, code: str = "bad_request", correlatable: bool = True):
        super().__init__(message)
        self.code = code
        self.correlatable = correlatable


ACTION_SCHEMAS = {
    "auth.login": ActionSchema("auth", ("u", "p")),
    "device.register": ActionSchema(
        "device",
        ("clientId", "deviceSessionId", "deviceName", "roles", "capabilities"),
        ("alias",),
    ),
    "device.list": ActionSchema("state", ()),
    "system.ping": ActionSchema("system", ()),
    "playback.context.create": ActionSchema(
        "command",
        (
            "playbackContextId",
            "deviceSessionId",
            "queueSongIds",
            "currentIndex",
            "positionMs",
            "state",
        ),
    ),
    "playback.context.subscribe": ActionSchema("state", ("playbackContextId",)),
    "playback.context.unsubscribe": ActionSchema("state", ("playbackContextId",)),
    "playback.context.status": ActionSchema("state", ("playbackContextId",)),
    "playback.context.close": ActionSchema("command", ("playbackContextId",)),
    "queue.context.sync": ActionSchema(
        "state",
        (
            "playbackContextId",
            "deviceSessionId",
            "queueSongIds",
            "currentIndex",
            "positionMs",
            "baseQueueRevision",
        ),
        ("baseControlVersion",),
    ),
    "playback.update": ActionSchema(
        "event",
        ("playbackContextId", "deviceSessionId", "state", "positionMs", "clientSeq"),
        ("trackId", "volume", "muted"),
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
    "playbackContextId",
    "sourcePlaybackContextId",
    "handoffId",
    "prepareId",
    "broadcastId",
    "targetClientId",
    "trackId",
}
_NON_NEGATIVE_INT_FIELDS = {
    "currentIndex",
    "positionMs",
    "queueIndex",
    "baseQueueRevision",
    "baseControlVersion",
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
    if not isinstance(value, dict) or set(value) != set(STRICT_CAPABILITIES):
        raise StrictRequestValidationError(
            "capabilities must contain exactly the 9 strict-v2 boolean fields"
        )
    normalized = {}  # type: Dict[str, bool]
    for capability in STRICT_CAPABILITIES:
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


def _validate_field(payload: Dict[str, object], field_name: str) -> None:
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
        if value not in {"playing", "paused", "stopped"}:
            raise StrictRequestValidationError("state is invalid")
    elif field_name == "queueSongIds":
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
    else:
        raise StrictRequestValidationError("No validator exists for %s" % field_name)


def _validate_action_combinations(action: str, payload: Dict[str, object]) -> None:
    if action in {"playback.context.create", "queue.context.sync", "broadcast.start", "broadcast.queue.sync"}:
        if payload["currentIndex"] >= len(payload["queueSongIds"]):
            raise StrictRequestValidationError("currentIndex is outside queueSongIds")
    if action == "playback.ready":
        if payload["ready"]:
            if "errorCode" in payload or "errorMessage" in payload:
                raise StrictRequestValidationError("ready:true forbids error fields")
        elif "errorCode" not in payload:
            raise StrictRequestValidationError("ready:false requires errorCode")


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
        _validate_field(payload, field_name)
    _validate_action_combinations(action, payload)

    normalized = dict(message)
    normalized["requestId"] = request_id
    normalized["action"] = action
    normalized["payload"] = payload
    return normalized


def is_strict_registration_request(message: object) -> bool:
    if not isinstance(message, dict) or message.get("action") != "device.register":
        return False
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return False
    capabilities = payload.get("capabilities")
    return isinstance(capabilities, dict) and capabilities.get("playbackContextV2") is True
