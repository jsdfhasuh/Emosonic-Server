import hashlib
import heapq
import secrets
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple


BROWSER_OTP_PREFIX = "browser-otp:"
DEFAULT_BROWSER_OTP_TTL_SECONDS = 60
DEFAULT_BROWSER_OTP_ISSUES_PER_SESSION_PER_MINUTE = 12
DEFAULT_BROWSER_OTP_OUTSTANDING_PER_SESSION = 4
DEFAULT_BROWSER_OTP_GLOBAL_CAPACITY = 10000
_ISSUE_WINDOW_MS = 60 * 1000


class BrowserOneTimePasswordRateLimited(Exception):
    def __init__(self, retry_after_ms: int) -> None:
        super().__init__("Browser one-time password issuance is rate limited")
        self.retry_after_ms = retry_after_ms


class BrowserOneTimePasswordCapacityExceeded(Exception):
    pass


class BrowserOneTimePasswordStore:
    """In-memory, session-bound credentials for same-origin browser sockets."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, Tuple[str, str, int]] = {}
        self._session_digests: Dict[Tuple[str, str], Deque[str]] = {}
        self._expiry_heap: List[Tuple[int, str]] = []
        self._session_issues: Dict[Tuple[str, str], Deque[int]] = {}
        self._issue_expiry_heap: List[Tuple[int, Tuple[str, str]]] = []

    @staticmethod
    def _digest(password: str) -> str:
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    def issue(
        self,
        user_name: str,
        browser_session_id: str,
        ttl_seconds: int = DEFAULT_BROWSER_OTP_TTL_SECONDS,
        now_ms: Optional[int] = None,
        max_issues_per_minute: int = DEFAULT_BROWSER_OTP_ISSUES_PER_SESSION_PER_MINUTE,
        outstanding_per_session: int = DEFAULT_BROWSER_OTP_OUTSTANDING_PER_SESSION,
        global_capacity: int = DEFAULT_BROWSER_OTP_GLOBAL_CAPACITY,
    ) -> Tuple[str, int]:
        if not user_name or not browser_session_id:
            raise ValueError("Browser credentials require user and session identity")
        ttl_seconds = max(1, int(ttl_seconds))
        max_issues_per_minute = max(1, int(max_issues_per_minute))
        outstanding_per_session = max(1, int(outstanding_per_session))
        global_capacity = max(1, int(global_capacity))
        issued_at_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        expires_at_ms = issued_at_ms + ttl_seconds * 1000
        password = BROWSER_OTP_PREFIX + secrets.token_urlsafe(32)
        digest = self._digest(password)
        session_key = (user_name, browser_session_id)
        with self._lock:
            self._discard_expired_locked(issued_at_ms)
            issue_times = self._session_issues.get(session_key)
            if issue_times is None:
                if len(self._session_issues) >= global_capacity:
                    raise BrowserOneTimePasswordCapacityExceeded()
                issue_times = deque()
            cutoff_ms = issued_at_ms - _ISSUE_WINDOW_MS
            while issue_times and issue_times[0] <= cutoff_ms:
                issue_times.popleft()
            if len(issue_times) >= max_issues_per_minute:
                raise BrowserOneTimePasswordRateLimited(
                    max(1, issue_times[0] + _ISSUE_WINDOW_MS - issued_at_ms)
                )

            session_digests = self._session_digests.get(session_key)
            will_evict = bool(
                session_digests
                and len(session_digests) >= outstanding_per_session
            )
            if len(self._records) >= global_capacity and not will_evict:
                raise BrowserOneTimePasswordCapacityExceeded()

            if session_digests is None:
                session_digests = deque()
                self._session_digests[session_key] = session_digests
            while len(session_digests) >= outstanding_per_session:
                self._records.pop(session_digests.popleft(), None)

            self._records[digest] = (user_name, browser_session_id, expires_at_ms)
            session_digests.append(digest)
            heapq.heappush(self._expiry_heap, (expires_at_ms, digest))
            self._session_issues[session_key] = issue_times
            issue_times.append(issued_at_ms)
            heapq.heappush(
                self._issue_expiry_heap,
                (issued_at_ms + _ISSUE_WINDOW_MS, session_key),
            )
            self._compact_expiry_heap_locked()
        return password, expires_at_ms

    def consume(
        self,
        user_name: str,
        browser_session_id: str,
        password: object,
        now_ms: Optional[int] = None,
    ) -> bool:
        if (
            not user_name
            or not browser_session_id
            or not isinstance(password, str)
            or not password.startswith(BROWSER_OTP_PREFIX)
        ):
            return False
        current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        digest = self._digest(password)
        with self._lock:
            self._discard_expired_locked(current_ms)
            record = self._records.pop(digest, None)
            if record is not None:
                session_key = (record[0], record[1])
                self._remove_session_digest_locked(session_key, digest)
        if record is None:
            return False
        return (
            record[0] == user_name
            and record[1] == browser_session_id
            and record[2] >= current_ms
        )

    def _discard_expired_locked(self, now_ms: int) -> None:
        while self._expiry_heap and self._expiry_heap[0][0] < now_ms:
            expires_at_ms, digest = heapq.heappop(self._expiry_heap)
            record = self._records.get(digest)
            if record is None or record[2] != expires_at_ms:
                continue
            self._records.pop(digest, None)
            session_key = (record[0], record[1])
            self._remove_session_digest_locked(session_key, digest)

        cutoff_ms = now_ms - _ISSUE_WINDOW_MS
        while self._issue_expiry_heap and self._issue_expiry_heap[0][0] <= now_ms:
            _expires_at_ms, session_key = heapq.heappop(self._issue_expiry_heap)
            issue_times = self._session_issues.get(session_key)
            if issue_times is None:
                continue
            while issue_times and issue_times[0] <= cutoff_ms:
                issue_times.popleft()
            if not issue_times:
                self._session_issues.pop(session_key, None)

    def _remove_session_digest_locked(
        self,
        session_key: Tuple[str, str],
        digest: str,
    ) -> None:
        session_digests = self._session_digests.get(session_key)
        if session_digests is None:
            return
        try:
            session_digests.remove(digest)
        except ValueError:
            return
        if not session_digests:
            self._session_digests.pop(session_key, None)

    def _compact_expiry_heap_locked(self) -> None:
        if len(self._expiry_heap) <= max(64, len(self._records) * 2):
            return
        self._expiry_heap = [
            (record[2], digest) for digest, record in self._records.items()
        ]
        heapq.heapify(self._expiry_heap)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._session_digests.clear()
            self._expiry_heap.clear()
            self._session_issues.clear()
            self._issue_expiry_heap.clear()


browser_one_time_passwords = BrowserOneTimePasswordStore()
