import math
import time
from typing import Dict, Optional

from ..db import Track, close_connection, open_connection


MIN_CLOCK_PING_SAMPLES = 3
MAX_CLOCK_SAMPLE_AGE_MS = 15_000
MAX_PLAYING_STATE_AGE_MS = 2_000
MAX_EFFECTIVE_AT_SAMPLE_FUTURE_MS = 50
MIN_BROADCAST_PLAYBACK_RATE = 0.5
MAX_BROADCAST_PLAYBACK_RATE = 2.0

_REQUIRED_PLAYER_CAPABILITIES = (
    "playbackContextV2",
    "canPlay",
    "canPause",
    "canSeek",
    "effectiveAtPlayback",
)


class EffectiveAtEligibilityError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _server_time_ms(now_ms: Optional[int] = None) -> int:
    return int(time.time() * 1000 if now_ms is None else now_ms)


def isClockGateReady(
    clock_gate: Optional[Dict[str, object]],
    now_ms: Optional[int] = None,
) -> bool:
    if clock_gate is None:
        return False
    ping_count = clock_gate.get("clockPingCount")
    last_ping_ms = clock_gate.get("lastClockPingAtMs")
    if type(ping_count) is not int or ping_count < MIN_CLOCK_PING_SAMPLES:
        return False
    if type(last_ping_ms) is not int or last_ping_ms < 0:
        return False
    age_ms = _server_time_ms(now_ms) - last_ping_ms
    return 0 <= age_ms <= MAX_CLOCK_SAMPLE_AGE_MS


def requireEffectiveAtPlayer(
    websocket_state,
    user_name: str,
    client_id: str,
    now_ms: Optional[int] = None,
    require_broadcast: bool = True,
    required_capabilities=None,
) -> Dict[str, object]:
    client = websocket_state.get_client(client_id, user_name=user_name)
    if client is None:
        raise EffectiveAtEligibilityError(
            "offline",
            "Player is not connected",
        )
    if "player" not in set(client.get("roles") or ()):
        raise EffectiveAtEligibilityError(
            "capability_required",
            "Effective-at target must have the player role",
        )
    capabilities = client.get("capabilities") or {}
    required = list(
        _REQUIRED_PLAYER_CAPABILITIES
        if required_capabilities is None
        else required_capabilities
    )
    if require_broadcast:
        required.append("supportsBroadcast")
    if any(capabilities.get(name) is not True for name in required):
        raise EffectiveAtEligibilityError(
            "capability_required",
            "Player lacks required effective-at capabilities",
        )
    clock_gate = websocket_state.get_clock_gate_for_client(
        user_name,
        client_id,
    )
    if not isClockGateReady(clock_gate, now_ms=now_ms):
        raise EffectiveAtEligibilityError(
            "clock_unsynchronized",
            "Current connection has not completed the clock gate",
        )
    return {
        "client": client,
        "clockGate": clock_gate,
    }


def validateBroadcastSourceState(
    playback_context: Dict[str, object],
    device_state: Optional[Dict[str, object]],
    now_ms: Optional[int] = None,
    has_unsettled_controls: bool = False,
    require_playing: bool = True,
) -> Dict[str, object]:
    current_ms = _server_time_ms(now_ms)
    if playback_context.get("lifecycle") != "active":
        raise EffectiveAtEligibilityError(
            "context_closed",
            "Source PlaybackContext is not active",
        )
    queue_song_ids = playback_context.get("queueSongIds")
    current_index = playback_context.get("currentIndex")
    track_id = playback_context.get("trackId")
    if (
        not isinstance(queue_song_ids, list)
        or not queue_song_ids
        or type(current_index) is not int
        or current_index < 0
        or current_index >= len(queue_song_ids)
        or queue_song_ids[current_index] != track_id
    ):
        raise EffectiveAtEligibilityError(
            "queue_required",
            "Broadcast source requires a canonical non-empty queue",
        )
    if device_state is None:
        raise EffectiveAtEligibilityError(
            "source_state_missing",
            "Source has not reported a DevicePlaybackState",
        )
    authority_client_id = playback_context.get("authorityClientId")
    authority_device_session_id = playback_context.get(
        "authorityDeviceSessionId"
    )
    if (
        device_state.get("sourceClientId") != authority_client_id
        or device_state.get("deviceSessionId")
        != authority_device_session_id
    ):
        raise EffectiveAtEligibilityError(
            "authority_changed",
            "DevicePlaybackState does not belong to the current source pair",
        )
    context_epoch = device_state.get(
        "contextEpoch",
        device_state.get("epoch"),
    )
    if context_epoch != playback_context.get("epoch"):
        raise EffectiveAtEligibilityError(
            "source_state_unsettled",
            "DevicePlaybackState belongs to another Context epoch",
        )
    if device_state.get("isAuthority") is False:
        raise EffectiveAtEligibilityError(
            "authority_changed",
            "DevicePlaybackState is not authoritative",
        )
    client_seq = device_state.get("clientSeq")
    if type(client_seq) is not int or client_seq < 1:
        raise EffectiveAtEligibilityError(
            "source_state_unsettled",
            "Source has not reported a complete DevicePlaybackState",
        )
    if (
        device_state.get("appliedControlVersion")
        != playback_context.get("controlVersion")
        or has_unsettled_controls
    ):
        raise EffectiveAtEligibilityError(
            "source_state_unsettled",
            "Source has not settled the current control target",
        )
    if device_state.get("trackId") != track_id:
        raise EffectiveAtEligibilityError(
            "source_track_mismatch",
            "Source actual track does not match the canonical queue item",
        )
    if require_playing and device_state.get("state") != "playing":
        raise EffectiveAtEligibilityError(
            "source_not_playing",
            "Broadcast source must currently be playing",
        )
    playback_rate = device_state.get("playbackRate")
    if (
        isinstance(playback_rate, bool)
        or not isinstance(playback_rate, (int, float))
        or not math.isfinite(playback_rate)
        or playback_rate < MIN_BROADCAST_PLAYBACK_RATE
        or playback_rate > MAX_BROADCAST_PLAYBACK_RATE
    ):
        raise EffectiveAtEligibilityError(
            "rate_unsupported",
            "Source playbackRate is outside 0.5..2.0",
        )
    position_ms = device_state.get("positionMs")
    sampled_at_ms = device_state.get("positionSampledAtServerMs")
    received_at_ms = device_state.get("serverUpdatedAtMs")
    if type(position_ms) is not int or position_ms < 0:
        raise EffectiveAtEligibilityError(
            "source_state_invalid",
            "Source positionMs is invalid",
        )
    if type(sampled_at_ms) is not int or type(received_at_ms) is not int:
        raise EffectiveAtEligibilityError(
            "source_state_invalid",
            "Source state is missing its sample or receive time",
        )
    if sampled_at_ms > current_ms + MAX_EFFECTIVE_AT_SAMPLE_FUTURE_MS:
        raise EffectiveAtEligibilityError(
            "clock_unsynchronized",
            "Source position sample is too far in the future",
        )
    sampled_age_ms = current_ms - sampled_at_ms
    received_age_ms = current_ms - received_at_ms
    if (
        sampled_age_ms < -MAX_EFFECTIVE_AT_SAMPLE_FUTURE_MS
        or received_age_ms < 0
        or sampled_age_ms > MAX_PLAYING_STATE_AGE_MS
        or received_age_ms > MAX_PLAYING_STATE_AGE_MS
    ):
        raise EffectiveAtEligibilityError(
            "source_state_stale",
            "Source sample and receive times must both be fresh",
        )
    return {
        "queueSongIds": list(queue_song_ids),
        "currentIndex": current_index,
        "trackId": track_id,
        "state": device_state.get("state"),
        "positionMs": position_ms,
        "positionSampledAtServerMs": sampled_at_ms,
        "serverUpdatedAtMs": received_at_ms,
        "playbackRate": float(playback_rate),
        "sourceVersion": playback_context.get("version"),
        "sourceQueueRevision": playback_context.get("queueRevision"),
        "sourceControlVersion": playback_context.get("controlVersion"),
        "sourceEpoch": playback_context.get("epoch"),
    }


def projectBroadcastPositionMs(
    position_ms: int,
    position_sampled_at_server_ms: int,
    target_server_ms: int,
    playback_rate: float,
    duration_ms: Optional[int] = None,
) -> int:
    if type(position_ms) is not int or position_ms < 0:
        raise ValueError("positionMs must be a non-negative integer")
    if type(position_sampled_at_server_ms) is not int:
        raise ValueError("positionSampledAtServerMs must be an integer")
    if type(target_server_ms) is not int:
        raise ValueError("targetServerMs must be an integer")
    if (
        isinstance(playback_rate, bool)
        or not isinstance(playback_rate, (int, float))
        or not math.isfinite(playback_rate)
        or not (
            MIN_BROADCAST_PLAYBACK_RATE
            <= playback_rate
            <= MAX_BROADCAST_PLAYBACK_RATE
        )
    ):
        raise ValueError("playbackRate must be within 0.5..2.0")
    projected = position_ms + int(
        max(0, target_server_ms - position_sampled_at_server_ms)
        * playback_rate
    )
    if duration_ms is not None:
        if type(duration_ms) is not int or duration_ms < 0:
            raise ValueError("durationMs must be a non-negative integer")
        projected = min(projected, duration_ms)
    return projected


def getTrackDurationMs(track_id: str) -> Optional[int]:
    if not isinstance(track_id, str) or not track_id:
        return None
    open_connection(reuse=True)
    try:
        try:
            track = Track.get_or_none(Track.id == track_id)
        except (TypeError, ValueError):
            return None
        if track is None or track.duration is None:
            return None
        return max(0, int(track.duration) * 1000)
    finally:
        close_connection()


def validateFeedbackPositionMs(
    position_ms: int,
    duration_ms: Optional[int],
) -> None:
    if type(position_ms) is not int or position_ms < 0:
        raise ValueError("positionMs must be a non-negative integer")
    if duration_ms is not None and position_ms > duration_ms:
        raise ValueError("positionMs exceeds the known media duration")
