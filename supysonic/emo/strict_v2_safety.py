import logging
import math
import threading
import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict, Mapping, Optional, Tuple


logger = logging.getLogger(__name__)

DEFAULT_LIMITS = {
    "unauthenticated_connections_per_ip": 10,
    "authenticated_connections_per_user": 20,
    "requests_per_connection_per_minute": 120,
    "controls_per_connection_per_second": 20,
    "creates_per_connection_per_minute": 10,
    "handoff_starts_per_connection_per_minute": 10,
    "broadcast_starts_per_connection_per_minute": 10,
}

_CONFIG_KEYS = {
    "unauthenticated_connections_per_ip": "emo_unauthenticated_connections_per_ip",
    "authenticated_connections_per_user": "emo_authenticated_connections_per_user",
    "requests_per_connection_per_minute": "emo_strict_requests_per_connection_per_minute",
    "controls_per_connection_per_second": "emo_strict_controls_per_connection_per_second",
    "creates_per_connection_per_minute": "emo_strict_creates_per_connection_per_minute",
    "handoff_starts_per_connection_per_minute": "emo_strict_handoff_starts_per_connection_per_minute",
    "broadcast_starts_per_connection_per_minute": "emo_strict_broadcast_starts_per_connection_per_minute",
}

_CONTROL_ACTIONS = {
    "device.setVolume",
    "queue.playItem",
    "player.play",
    "player.pause",
    "player.seek",
    "player.next",
    "player.prev",
}


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def resolve_allowed_origins(
    webapp_config: Mapping[str, object],
    development: bool = False,
):
    configured = webapp_config.get("emo_allowed_origins")
    if configured is None or configured == "":
        return None
    if isinstance(configured, str):
        origins = [item.strip() for item in configured.split(",") if item.strip()]
    elif isinstance(configured, (list, tuple, set)):
        origins = [str(item).strip() for item in configured if str(item).strip()]
    else:
        raise ValueError("emo_allowed_origins must be a string or sequence")
    if "*" in origins:
        if len(origins) != 1:
            raise ValueError("wildcard Origin cannot be combined with an allowlist")
        if not development:
            raise ValueError("wildcard Emo Origin is allowed only in development mode")
        logger.warning("Emo Socket.IO development mode allows every Origin")
        return "*"
    return origins or None


def validate_strict_v2_worker_count(
    processes: Optional[int],
    webapp_config: Mapping[str, object],
    code_readiness: Optional[Mapping[str, bool]] = None,
) -> None:
    if processes is None or processes <= 1:
        return
    from .strict_v2_readiness import get_effective_profile_readiness

    readiness = get_effective_profile_readiness(webapp_config, code_readiness)
    if readiness["core"]:
        raise RuntimeError(
            "strict-v2 realtime Core requires exactly one server process"
        )


class StrictV2Safety:
    def __init__(self, time_fn: Callable[[], float] = time.monotonic):
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._time_fn = time_fn
        self._limits = dict(DEFAULT_LIMITS)
        self._buckets = defaultdict(deque)  # type: Dict[Tuple[str, str], Deque[float]]
        self._pending_emits = defaultdict(int)  # type: Dict[str, int]
        self._max_pending_emits = 100
        self._shutting_down = False
        self._active_requests = 0

    def configure(
        self,
        webapp_config: Mapping[str, object],
        time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        evidence = webapp_config.get("emo_strict_rate_limit_load_test_evidence")
        limits = {}
        for name, default in DEFAULT_LIMITS.items():
            configured = _positive_int(webapp_config.get(_CONFIG_KEYS[name]), default)
            if configured > default and not (
                isinstance(evidence, str) and evidence.strip()
            ):
                logger.warning(
                    "Ignoring raised strict-v2 limit %s without load-test evidence",
                    name,
                )
                configured = default
            limits[name] = configured
        with self._condition:
            self._limits = limits
            self._buckets.clear()
            self._pending_emits.clear()
            self._max_pending_emits = _positive_int(
                webapp_config.get("emo_socketio_max_pending_emits_per_connection"),
                100,
            )
            self._shutting_down = False
            self._active_requests = 0
            if time_fn is not None:
                self._time_fn = time_fn

    def limit(self, name: str) -> int:
        with self._lock:
            return self._limits[name]

    def accepts_connections(self) -> bool:
        with self._lock:
            return not self._shutting_down

    def begin_request(self) -> bool:
        with self._condition:
            if self._shutting_down:
                return False
            self._active_requests += 1
            return True

    def finish_request(self) -> None:
        with self._condition:
            if self._active_requests > 0:
                self._active_requests -= 1
            if self._active_requests == 0:
                self._condition.notify_all()

    def begin_shutdown(self, timeout_seconds: float) -> bool:
        deadline = self._time_fn() + max(0.0, timeout_seconds)
        with self._condition:
            self._shutting_down = True
            while self._active_requests:
                remaining = deadline - self._time_fn()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def check_rate_limit(self, connection_nonce: str, action: str) -> Optional[int]:
        checks = [
            (
                "requests",
                self._limits["requests_per_connection_per_minute"],
                60.0,
            )
        ]
        if action in _CONTROL_ACTIONS:
            checks.append(
                (
                    "controls",
                    self._limits["controls_per_connection_per_second"],
                    1.0,
                )
            )
        action_limits = {
            "playback.context.ensure": (
                "creates",
                "creates_per_connection_per_minute",
            ),
            "playback.handoff.start": (
                "handoff_starts",
                "handoff_starts_per_connection_per_minute",
            ),
            "broadcast.start": (
                "broadcast_starts",
                "broadcast_starts_per_connection_per_minute",
            ),
        }
        action_limit = action_limits.get(action)
        if action_limit is not None:
            bucket_name, limit_name = action_limit
            checks.append((bucket_name, self._limits[limit_name], 60.0))

        now = self._time_fn()
        with self._lock:
            retry_after_ms = 0
            buckets = []
            for bucket_name, limit, window_seconds in checks:
                bucket = self._buckets[(connection_nonce, bucket_name)]
                cutoff = now - window_seconds
                while bucket and bucket[0] <= cutoff:
                    bucket.popleft()
                buckets.append(bucket)
                if len(bucket) >= limit:
                    retry_after_ms = max(
                        retry_after_ms,
                        max(1, math.ceil((bucket[0] + window_seconds - now) * 1000)),
                    )
            if retry_after_ms:
                return retry_after_ms
            for bucket in buckets:
                bucket.append(now)
            return None

    def clear_connection(self, connection_nonce: str) -> None:
        with self._lock:
            for key in list(self._buckets):
                if key[0] == connection_nonce:
                    self._buckets.pop(key, None)

    def reserve_emit(self, sid: str) -> bool:
        with self._lock:
            if self._pending_emits[sid] >= self._max_pending_emits:
                return False
            self._pending_emits[sid] += 1
            return True

    def release_emit(self, sid: str) -> None:
        with self._lock:
            pending = self._pending_emits.get(sid, 0)
            if pending <= 1:
                self._pending_emits.pop(sid, None)
            else:
                self._pending_emits[sid] = pending - 1


strict_v2_safety = StrictV2Safety()
