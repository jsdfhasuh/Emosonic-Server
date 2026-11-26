"""Oracle control and delivery manifest client for verified CN releases."""

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Mapping, Optional, Tuple
from urllib.parse import unquote, urlsplit

import requests


_ALBUM_ID = re.compile(r"[1-9][0-9]{0,63}")
_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STATES = {"absent", "submitted", "associated", "unknown", "rejected"}
_TASK_STATUSES = {"queued", "running", "completed", "failed", "cancelled", "unknown"}


def cn_album_id(url: object) -> Optional[str]:
    """Return the ID of a whole CN release URL, with no single-track query."""
    if not isinstance(url, str) or url != url.strip() or any(ord(c) < 32 for c in url):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc.lower() != "music.apple.com":
        return None
    if "?" in url or "#" in url:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) not in (3, 4) or parts[:2] != ["cn", "album"]:
        return None
    if len(parts) == 4:
        slug = unquote(parts[2])
        if slug in ("", ".", "..") or "/" in slug or "\\" in slug:
            return None
    return parts[-1] if _ALBUM_ID.fullmatch(parts[-1]) else None


def parse_delivery_path(path: object) -> Optional[PurePosixPath]:
    if not isinstance(path, str) or "\\" in path or any(ord(c) < 32 for c in path):
        return None
    parts = path.split("/")
    if len(parts) < 3 or any(part in ("", ".", "..") or ":" in part for part in parts):
        return None
    return PurePosixPath(*parts)


@dataclass(frozen=True)
class DeliveryFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class DeliveryManifest:
    batch_id: str
    album_url: str
    state: str
    files: Tuple[DeliveryFile, ...]
    manifest_sha256: str

    @classmethod
    def from_payload(
        cls, payload: object, batch_id: str, album_id: str
    ) -> "DeliveryManifest":
        if not isinstance(payload, dict) or payload.get("id") != batch_id:
            raise ValueError("delivery batch ID mismatch")
        url, state, files, digest = (
            payload.get(key) for key in ("url", "state", "files", "manifestSha256")
        )
        if cn_album_id(url) != album_id:
            raise ValueError("delivery batch album mismatch")
        if state not in ("ready", "acknowledged", "cleaned"):
            raise ValueError("invalid delivery batch state")
        if not isinstance(files, list) or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError("invalid delivery manifest")
        canonical = json.dumps(
            {"id": batch_id, "url": url, "files": files},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        if not hmac.compare_digest(hashlib.sha256(canonical.encode()).hexdigest(), digest):
            raise ValueError("delivery manifest digest mismatch")
        validated, paths = [], set()
        for index, item in enumerate(files):
            if not isinstance(item, dict):
                raise ValueError("invalid delivery file entry")
            path, size, sha256 = (item.get(key) for key in ("path", "size", "sha256"))
            file_id = item.get("id")
            if (
                type(file_id) is not int or file_id != index
                or parse_delivery_path(path) is None or path in paths
                or type(size) is not int or size < 0
                or not isinstance(sha256, str) or not _SHA256.fullmatch(sha256)
            ):
                raise ValueError("invalid delivery file entry")
            paths.add(path)
            validated.append(DeliveryFile(path, size, sha256))
        return cls(batch_id, url, state, tuple(validated), digest)


@dataclass(frozen=True)
class OracleAlbumState:
    status: str
    task_id: Optional[str] = None
    deliveries: Tuple[DeliveryManifest, ...] = ()
    detail: str = ""
    task_status: Optional[str] = None


class OracleAlbumClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        delivery_token: str,
        *,
        timeout: float = 12,
        session: Optional[requests.Session] = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in ("https", "http") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
        ):
            raise ValueError("invalid Oracle URL")
        if not token or not delivery_token:
            raise ValueError("Oracle control and delivery credentials are required")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._delivery_token = delivery_token
        self._timeout = timeout
        self._session = session or requests.Session()

    def _request(
        self, method: str, path: str, token: str, **options: object
    ) -> Optional[Tuple[int, object]]:
        try:
            response = self._session.request(
                method, self._base_url + path,
                headers={"Authorization": "Bearer " + token},
                timeout=self._timeout, allow_redirects=False, **options,
            )
            if response.status_code not in (200, 201, 202, 404):
                return response.status_code, None
            return response.status_code, response.json()
        except (requests.RequestException, ValueError):
            return None

    def _state(
        self, response: Optional[Tuple[int, object]], album_id: str,
        order_id: Optional[str],
    ) -> OracleAlbumState:
        if response is None:
            return OracleAlbumState("unknown", detail="oracle_unavailable")
        code, payload = response
        if code not in (200, 201, 202) or not isinstance(payload, dict):
            return OracleAlbumState("unknown", detail="oracle_control_unavailable")
        status, task_id, task_status, batch_ids = (
            payload.get(field) for field in ("status", "taskId", "taskStatus", "batchIds")
        )
        if (
            payload.get("albumId") != album_id or payload.get("orderId") != order_id
            or not isinstance(status, str) or status not in _STATES
            or not isinstance(batch_ids, list)
            or (task_id is not None and (
                not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id)
            ))
            or (task_status is not None and (
                not isinstance(task_status, str) or task_status not in _TASK_STATUSES
            ))
            or (status in ("submitted", "associated") and task_id is None)
            or (status == "submitted" and order_id is None)
            or (status in ("absent", "rejected") and (task_id is not None or batch_ids))
        ):
            return OracleAlbumState("unknown", detail="oracle_response_invalid")
        manifests = []
        detail = ""
        seen = set()
        for batch_id in batch_ids:
            if not isinstance(batch_id, str) or not _TASK_ID.fullmatch(batch_id):
                detail = "delivery_manifest_invalid"
                continue
            if batch_id in seen:
                continue
            seen.add(batch_id)
            manifest = self._request(
                "GET", "/delivery/v1/batches/" + batch_id, self._delivery_token
            )
            if manifest is None or manifest[0] != 200:
                detail = "delivery_manifest_unavailable"
                continue
            try:
                manifests.append(DeliveryManifest.from_payload(manifest[1], batch_id, album_id))
            except ValueError:
                detail = "delivery_manifest_invalid"
        return OracleAlbumState(status, task_id, tuple(manifests), detail, task_status)

    def inspect(
        self, album_id: str, album_url: str, task_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> OracleAlbumState:
        if cn_album_id(album_url) != album_id:
            return OracleAlbumState("rejected", detail="invalid_cn_release_url")
        params = {}
        if order_id is not None:
            params["orderId"] = order_id
        if task_id is not None:
            params["taskId"] = task_id
        response = self._request(
            "GET", "/api/discovery/albums/" + album_id, self._token, params=params
        )
        return self._state(response, album_id, order_id)

    def submit(self, album_id: str, album_url: str, order_id: str) -> OracleAlbumState:
        if cn_album_id(album_url) != album_id or not isinstance(order_id, str) or not _TASK_ID.fullmatch(order_id):
            return OracleAlbumState("rejected", detail="invalid_cn_release_or_order")
        payload: Mapping[str, str] = {"url": album_url, "orderId": order_id}
        response = self._request(
            "POST", "/api/discovery/albums/" + album_id, self._token, json=payload
        )
        return self._state(response, album_id, order_id)
