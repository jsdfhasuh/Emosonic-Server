import hashlib
import json
import threading
import time
from copy import deepcopy
from typing import Callable, Dict, NamedTuple, Optional, Tuple


class RequestFingerprintConflict(Exception):
    pass


class CachedRequest(NamedTuple):
    fingerprint: str
    created_at: float
    result: Optional[object]


class RequestLookup(NamedTuple):
    status: str
    result: Optional[object]


def request_fingerprint(message_type: str, action: str, payload: object) -> str:
    canonical = json.dumps(
        {
            "type": message_type,
            "action": action,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class StrictRequestCache:
    def __init__(
        self,
        ttl_seconds: float = 60.0,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 60:
            raise ValueError("strict request cache TTL must be at least 60 seconds")
        self._ttl_seconds = ttl_seconds
        self._time_fn = time_fn
        self._lock = threading.RLock()
        self._entries = {}  # type: Dict[Tuple[str, str], CachedRequest]

    def _prune_locked(self, now: float) -> None:
        for key, entry in list(self._entries.items()):
            if now - entry.created_at >= self._ttl_seconds:
                self._entries.pop(key, None)

    def lookup_or_reserve(
        self,
        connection_nonce: str,
        request_id: str,
        fingerprint: str,
    ) -> RequestLookup:
        key = (connection_nonce, request_id)
        now = self._time_fn()
        with self._lock:
            self._prune_locked(now)
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = CachedRequest(fingerprint, now, None)
                return RequestLookup("new", None)
            if entry.fingerprint != fingerprint:
                raise RequestFingerprintConflict(
                    "requestId was already used with different content"
                )
            if entry.result is None:
                return RequestLookup("in_flight", None)
            return RequestLookup("cached", deepcopy(entry.result))

    def store_result(
        self,
        connection_nonce: str,
        request_id: str,
        fingerprint: str,
        result: object,
    ) -> None:
        key = (connection_nonce, request_id)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.fingerprint != fingerprint:
                raise RequestFingerprintConflict(
                    "cannot settle an unreserved or conflicting request"
                )
            self._entries[key] = CachedRequest(
                fingerprint,
                entry.created_at,
                deepcopy(result),
            )

    def release_reservation(
        self,
        connection_nonce: str,
        request_id: str,
        fingerprint: str,
    ) -> None:
        key = (connection_nonce, request_id)
        with self._lock:
            entry = self._entries.get(key)
            if (
                entry is not None
                and entry.fingerprint == fingerprint
                and entry.result is None
            ):
                self._entries.pop(key, None)

    def clear_connection(self, connection_nonce: str) -> None:
        with self._lock:
            for key in list(self._entries):
                if key[0] == connection_nonce:
                    self._entries.pop(key, None)

    def clear_all(self) -> None:
        with self._lock:
            self._entries.clear()

    def size(self) -> int:
        with self._lock:
            self._prune_locked(self._time_fn())
            return len(self._entries)
