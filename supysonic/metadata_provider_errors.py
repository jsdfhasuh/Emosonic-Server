"""Allowlisted provider failure details for persistent metadata backoff."""

import math
from typing import Dict

import requests

from .llm_client import _extract_upstream_error, _retry_after_seconds


MAX_RETRY_AFTER_SECONDS = 86400


def safe_metadata_error_details(response: object, *, api_key: str = "") -> Dict[str, object]:
    extracted = _extract_upstream_error(response, api_key=api_key)
    status = extracted.get("upstreamStatus")
    valid_status = type(status) is int and 100 <= status <= 599
    quota_exhausted = extracted.get("quotaExhausted") is True
    retryable = bool(valid_status and not quota_exhausted
                     and (status in (408, 429) or 500 <= status <= 599))
    details: Dict[str, object] = {
        "retryable": retryable,
        "quotaExhausted": quota_exhausted,
        "rateLimited": valid_status and status == 429 and not quota_exhausted,
    }
    if valid_status:
        details["upstreamStatus"] = status
    retry_after = _retry_after_seconds(response) if retryable else None
    if retry_after is not None and math.isfinite(retry_after):
        details["retryAfterSeconds"] = min(retry_after, MAX_RETRY_AFTER_SECONDS)
    return details


def transport_error_details(error: requests.exceptions.RequestException) -> Dict[str, object]:
    if isinstance(error, requests.exceptions.SSLError):
        return {"retryable": False}
    if isinstance(error, requests.exceptions.Timeout):
        return {"retryable": True, "transportError": "timeout"}
    if isinstance(error, (requests.exceptions.ConnectionError,
                          requests.exceptions.ChunkedEncodingError)):
        return {"retryable": True, "transportError": "network"}
    return {"retryable": False}
