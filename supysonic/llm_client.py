import json
import time
from typing import Dict, Mapping, Optional, Sequence

import requests


DEFAULT_RATE_LIMIT_RETRY_DELAY_SECONDS = 5.0
MAX_RATE_LIMIT_RETRY_DELAY_SECONDS = 60.0


class LlmClientError(Exception):
    def __init__(
        self,
        message: str,
        details: Optional[Mapping[str, object]] = None,
    ) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class LlmConfigError(LlmClientError):
    pass


class LlmTimeoutError(LlmClientError):
    pass


class LlmUpstreamError(LlmClientError):
    pass


class LlmQuotaError(LlmUpstreamError):
    pass


class LlmInvalidResponseError(LlmClientError):
    pass


def build_chat_completion_payload(
    model: str,
    messages: Sequence[Mapping[str, str]],
    *,
    max_output_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    json_object: bool = True,
) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "model": model,
        "messages": [dict(message) for message in messages],
    }
    if max_output_tokens is not None and max_output_tokens > 0:
        payload["max_tokens"] = int(max_output_tokens)
    if temperature is not None:
        payload["temperature"] = float(temperature)
    if json_object:
        payload["response_format"] = {"type": "json_object"}
    return payload


def post_chat_completion(
    api_base_url: str,
    api_key: str,
    request_payload: Mapping[str, object],
    *,
    timeout_seconds: float = 20,
) -> Dict[str, object]:
    api_base_url = str(api_base_url or "").strip()
    api_key = str(api_key or "").strip()
    if not api_base_url or not api_key:
        raise LlmConfigError("LLM API base URL and API key are required.")

    endpoint = f"{api_base_url.rstrip('/')}/chat/completions"
    try:
        return _post_chat_completion_once(
            endpoint,
            api_key,
            request_payload,
            timeout_seconds=timeout_seconds,
        )
    except LlmTimeoutError:
        return _post_chat_completion_once(
            endpoint,
            api_key,
            request_payload,
            timeout_seconds=timeout_seconds,
        )
    except LlmUpstreamError as first_error:
        if _upstream_response_format_error(first_error.details):
            fallback_payload = dict(request_payload)
            fallback_payload.pop("response_format", None)
            return _post_chat_completion_once(
                endpoint,
                api_key,
                fallback_payload,
                timeout_seconds=timeout_seconds,
            )
        if first_error.details.get("retryable"):
            _sleep_before_retry(first_error.details)
            return _post_chat_completion_once(
                endpoint,
                api_key,
                request_payload,
                timeout_seconds=timeout_seconds,
            )
        raise


def parse_json_object_response(response_json: Mapping[str, object]) -> Dict[str, object]:
    try:
        choices = response_json["choices"]
        message = choices[0]["message"]
        content = message["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmInvalidResponseError("LLM response did not include message content.") from exc

    try:
        parsed = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise LlmInvalidResponseError("LLM response content was not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise LlmInvalidResponseError("LLM JSON response must be an object.")
    return parsed


def _post_chat_completion_once(
    endpoint: str,
    api_key: str,
    request_payload: Mapping[str, object],
    *,
    timeout_seconds: float,
) -> Dict[str, object]:
    try:
        response = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Connection": "close",
                "Content-Type": "application/json",
            },
            json=request_payload,
            timeout=timeout_seconds,
        )
    except requests.exceptions.Timeout as exc:
        raise LlmTimeoutError(
            "LLM request timed out.",
            details={"retryable": True},
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise LlmUpstreamError(
            "LLM request failed.",
            details={
                "upstreamErrorCode": exc.__class__.__name__,
                "upstreamMessage": str(exc),
                "retryable": True,
            },
        ) from exc

    if getattr(response, "status_code", 200) >= 400:
        details = _extract_upstream_error(response)
        if details.get("quotaExhausted"):
            raise LlmQuotaError(
                "LLM quota exhausted.",
                details=details,
            )
        raise LlmUpstreamError(
            "LLM request failed.",
            details=details,
        )

    response.encoding = "utf-8"
    try:
        response_json = response.json()
    except ValueError as exc:
        raise LlmInvalidResponseError("LLM returned non-JSON response data.") from exc
    if not isinstance(response_json, dict):
        raise LlmInvalidResponseError("LLM returned invalid response data.")
    return response_json


def _extract_upstream_error(response: object) -> Dict[str, object]:
    status_code = getattr(response, "status_code", None)
    details: Dict[str, object] = {
        "upstreamStatus": status_code,
        "retryable": isinstance(status_code, int) and status_code >= 500,
    }
    message = ""
    error_code = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
            error_code = str(error.get("code") or error.get("type") or "")
        elif error is not None:
            message = str(error)
    if not message:
        message = str(getattr(response, "text", "") or getattr(response, "reason", ""))
    if message:
        details["upstreamMessage"] = message
    if error_code:
        details["upstreamErrorCode"] = error_code
    if _is_quota_exhausted_error(status_code, message, error_code):
        details["quotaExhausted"] = True
        details["retryable"] = False
    elif status_code == 429:
        details["rateLimited"] = True
        details["retryable"] = True
        retry_after = _retry_after_seconds(response)
        if retry_after is not None:
            details["retryAfterSeconds"] = retry_after
    return details


def _sleep_before_retry(details: Mapping[str, object]) -> None:
    if details.get("upstreamStatus") != 429:
        return
    time.sleep(_bounded_retry_delay(details.get("retryAfterSeconds")))


def _bounded_retry_delay(value: object) -> float:
    try:
        retry_after = float(value)
    except (TypeError, ValueError):
        retry_after = DEFAULT_RATE_LIMIT_RETRY_DELAY_SECONDS
    if retry_after <= 0:
        retry_after = DEFAULT_RATE_LIMIT_RETRY_DELAY_SECONDS
    return min(retry_after, MAX_RATE_LIMIT_RETRY_DELAY_SECONDS)


def _retry_after_seconds(response: object) -> Optional[float]:
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw_value = None
    for name in ("Retry-After", "retry-after"):
        try:
            raw_value = headers.get(name)
        except AttributeError:
            raw_value = None
        if raw_value:
            break
    try:
        retry_after = float(raw_value)
    except (TypeError, ValueError):
        return None
    return retry_after if retry_after > 0 else None


def _is_quota_exhausted_error(
    status_code: object,
    message: str,
    error_code: str,
) -> bool:
    if status_code == 402:
        return True
    haystack = f"{message} {error_code}".casefold()
    quota_markers = (
        "insufficient_quota",
        "insufficient quota",
        "insufficient_balance",
        "quota exceeded",
        "quota_exceeded",
        "exceeded your current quota",
        "out of quota",
        "billing",
        "credit",
        "credits",
        "payment required",
        "余额",
        "额度",
        "欠费",
    )
    return any(marker in haystack for marker in quota_markers)


def _upstream_response_format_error(details: Mapping[str, object]) -> bool:
    status = details.get("upstreamStatus")
    message = str(details.get("upstreamMessage") or "").lower()
    code = str(details.get("upstreamErrorCode") or "").lower()
    return status in (400, 422) and "response_format" in f"{message} {code}"
