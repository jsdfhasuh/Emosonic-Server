"""Read a bounded snapshot of the history server's currently visible Shanghai day."""

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
from time import monotonic
from typing import Optional, Tuple
from urllib.parse import urlsplit

import requests

from ..album_discovery_quota import SHANGHAI

PAGE_SIZE = 100
MAX_PAGES = 10
MAX_RESPONSE_BYTES = 1024 * 1024


class HistoryError(RuntimeError):
    """History could not be established. The reason contains no source data."""


@dataclass(frozen=True)
class HistoryObservation:
    id: str
    business: str
    bvid: str
    cid: str
    title: str
    author_name: str
    tag_name: str
    view_time: int
    event_id: str = ""

    @property
    def event_key(self) -> tuple:
        if self.event_id:
            return ("event", self.event_id)
        return ("observation", self.business, self.id, self.cid, self.view_time)


def _identifier(value: object) -> str:
    if type(value) is int and value >= 0:
        value = str(value)
    if isinstance(value, str) and len(value) <= 128:
        return value.strip()
    return ""


def _bounded_text(value: object, size: int) -> str:
    return value.strip()[:size] if isinstance(value, str) else ""


def _observation(row: object, from_seconds: int, to_seconds: int) -> HistoryObservation:
    if not isinstance(row, dict) or row.get("platform") != "bilibili":
        raise HistoryError("history_invalid_platform")
    watched_at = row.get("view_time")
    if type(watched_at) is not int or not from_seconds <= watched_at < to_seconds:
        raise HistoryError("history_source_day_mismatch")
    identifier = _identifier(row.get("id"))
    business = _identifier(row.get("business"))
    event = _identifier(row.get("event_id"))
    if not event and (not identifier or not business):
        raise HistoryError("history_missing_observation_identity")
    return HistoryObservation(
        identifier, business, _identifier(row.get("bvid")), _identifier(row.get("cid")),
        _bounded_text(row.get("title"), 500), _bounded_text(row.get("author_name"), 100),
        _bounded_text(row.get("tag_name"), 80), watched_at, event,
    )


def _load_token(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        with Path(path).open(encoding="utf-8-sig") as token_file:
            token = token_file.read(2049).strip()
        # requests requires Latin-1 header values and forbids embedded newlines.
        token.encode("latin-1")
        if not 1 <= len(token) <= 2048 or any(ord(character) < 32 for character in token):
            raise ValueError
        return token
    except (OSError, ValueError):
        raise HistoryError("history_token_unavailable") from None


class BilibiliHistoryClient:
    def __init__(self, url: str, token_file: Optional[str], *, session: object = requests) -> None:
        if not url:
            raise HistoryError("history_url_missing")
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.query or parsed.fragment
            ):
                raise ValueError
        except ValueError:
            raise HistoryError("history_url_invalid") from None
        self.url = url.rstrip("/")
        token = _load_token(token_file)
        self.headers = {"X-Auth-Token": token} if token is not None else {}
        self.session = session

    def _request(self, method: str, path: str, **kwargs: object) -> dict:
        read_timeout = 25 if method == "POST" else 8
        deadline = monotonic() + read_timeout + 12
        try:
            with self.session.request(
                method, self.url + path, headers=self.headers,
                timeout=(4, read_timeout), allow_redirects=False, stream=True, **kwargs,
            ) as response:
                if response.status_code in (401, 403):
                    raise HistoryError("history_authentication_failed")
                if response.status_code == 429:
                    raise HistoryError("history_rate_limited")
                if response.status_code != 200:
                    raise HistoryError("history_request_failed")
                data = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    if monotonic() > deadline:
                        raise HistoryError("history_response_timeout")
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise HistoryError("history_response_limit")
        except requests.RequestException:
            raise HistoryError("history_request_unavailable") from None
        try:
            payload = json.loads(data)
        except (ValueError, RecursionError):
            raise HistoryError("history_invalid_response") from None
        if not isinstance(payload, dict):
            raise HistoryError("history_invalid_response")
        return payload

    def sync(self) -> None:
        payload = self._request("POST", "/api/history/sync", json={"platform": "bilibili"})
        details = payload.get("details")
        provider = details.get("bilibili") if isinstance(details, dict) else None
        if (
            payload.get("success") is not True or not isinstance(provider, dict)
            or "error" in provider
            or any(
                type(provider.get(key)) is not int or provider[key] < 0
                for key in ("newCount", "updateCount")
            )
        ):
            raise HistoryError("history_sync_bilibili_unconfirmed")

    def read_day(self, source_day: date) -> Tuple[HistoryObservation, ...]:
        """Read all visible pages and deduplicate observations, keeping source order."""
        midnight = datetime.combine(source_day, time.min, SHANGHAI)
        start, end = int(midnight.timestamp()), int((midnight + timedelta(days=1)).timestamp())
        total = None
        observations = {}
        seen_pages = set()
        for page in range(1, MAX_PAGES + 1):
            payload = self._request("GET", "/api/history", params={
                "platform": "bilibili", "date": source_day.isoformat(),
                "page": page, "pageSize": PAGE_SIZE,
            })
            reported, rows, more = payload.get("total"), payload.get("items"), payload.get("hasMore")
            if type(reported) is not int or reported < 0 or not isinstance(rows, list) or type(more) is not bool:
                raise HistoryError("history_invalid_pagination")
            if reported > MAX_PAGES * PAGE_SIZE:
                raise HistoryError("history_page_limit")
            if total is None:
                total = reported
            expected = min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE))
            if reported != total or len(rows) != expected or more != (page * PAGE_SIZE < total):
                raise HistoryError("history_inconsistent_pagination")
            entries = [_observation(row, start, end) for row in rows]
            keys = tuple(sorted(set(entry.event_key for entry in entries)))
            if keys in seen_pages:
                raise HistoryError("history_repeated_page")
            seen_pages.add(keys)
            for entry in entries:
                token = self.headers.get("X-Auth-Token")
                if token:
                    entry = replace(
                        entry, title=entry.title.replace(token, "[redacted]"),
                        author_name=entry.author_name.replace(token, "[redacted]"),
                        tag_name=entry.tag_name.replace(token, "[redacted]"),
                    )
                observations.setdefault(entry.event_key, entry)
            if not more:
                return tuple(observations.values())
        raise HistoryError("history_page_limit")
