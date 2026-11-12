import json
import re
import time
from typing import Dict, Mapping, Optional, Sequence

import requests


DEFAULT_RATE_LIMIT_RETRY_DELAY_SECONDS = 5.0
MAX_RATE_LIMIT_RETRY_DELAY_SECONDS = 60.0
SUPPORTED_REASONING_EFFORTS = (
    "none", "minimal", "low", "medium", "high", "xhigh", "max",
)
FENCED_JSON_RE = re.compile(r"```\s*(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


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
    token_limit_parameter: str = "max_tokens",
    json_schema: Optional[Mapping[str, object]] = None,
    reasoning_effort: Optional[str] = None,
) -> Dict[str, object]:
    if token_limit_parameter not in ("max_tokens", "max_completion_tokens"):
        raise LlmConfigError("Unsupported LLM token limit parameter.")
    payload: Dict[str, object] = {
        "model": model,
        "messages": [dict(message) for message in messages],
    }
    if max_output_tokens is not None and max_output_tokens > 0:
        payload[token_limit_parameter] = int(max_output_tokens)
    if temperature is not None:
        payload["temperature"] = float(temperature)
    if reasoning_effort is not None:
        reasoning_effort = str(reasoning_effort).strip()
        if reasoning_effort and reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
            raise LlmConfigError("Unsupported LLM reasoning effort.")
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
    if json_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": dict(json_schema),
        }
    elif json_object:
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
        response_format = request_payload.get("response_format")
        legacy_json_mode = (
            isinstance(response_format, Mapping)
            and response_format.get("type") == "json_object"
        )
        if legacy_json_mode and _upstream_response_format_error(first_error.details):
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
        choice = choices[0]
        message = choice["message"]
        if not isinstance(choice, Mapping) or not isinstance(message, Mapping):
            raise TypeError("Invalid completion choice")
        if choice.get("finish_reason") == "length":
            raise LlmInvalidResponseError("LLM response was truncated by its token limit.")
        if choice.get("finish_reason") == "content_filter" or message.get("refusal"):
            raise LlmInvalidResponseError("LLM refused to provide the requested JSON response.")
        content = message["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmInvalidResponseError("LLM response did not include message content.") from exc

    parsed = _parse_json_object_content(content)
    if not isinstance(parsed, dict):
        raise LlmInvalidResponseError("LLM JSON response must be an object.")
    return parsed


def _parse_json_object_content(content: object) -> object:
    for candidate in _json_content_candidates(content):
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            continue
    raise LlmInvalidResponseError("LLM response content was not valid JSON.")


def _json_content_candidates(content: object) -> Sequence[object]:
    candidates = [content]
    if not isinstance(content, str):
        return candidates

    for match in FENCED_JSON_RE.finditer(content):
        candidate = match.group(1).strip()
        if candidate:
            candidates.append(candidate)
    candidates.extend(_json_object_candidates(content))
    return candidates


def _json_object_candidates(content: str) -> Sequence[str]:
    candidates = []
    for index, char in enumerate(content):
        if char != "{":
            continue
        candidate = _balanced_json_object_from(content, index)
        if candidate:
            candidates.append(candidate)
    return candidates


def _balanced_json_object_from(content: str, start: int) -> Optional[str]:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(content)):
        char = content[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[start : index + 1]
    return None


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
    except requests.exceptions.Timeout:
        raise LlmTimeoutError(
            "LLM request timed out.",
            details={"retryable": True},
        ) from None
    except requests.exceptions.RequestException as exc:
        raise LlmUpstreamError(
            "LLM request failed.",
            details={
                "upstreamErrorCode": exc.__class__.__name__,
                "upstreamMessage": _redact_upstream_message(str(exc), api_key),
                "retryable": True,
            },
        ) from None

    if getattr(response, "status_code", 200) >= 400:
        details = _extract_upstream_error(response, api_key=api_key)
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


def _redact_upstream_message(message: str, api_key: str = "") -> str:
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    message = re.sub(
        r"(?i)\bBearer\s+[^\s,;\"'<>]+",
        "Bearer [REDACTED]",
        message,
    )
    return message


def _extract_upstream_error(
    response: object,
    *,
    api_key: str = "",
) -> Dict[str, object]:
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
        details["upstreamMessage"] = _redact_upstream_message(message, api_key)
    if error_code:
        details["upstreamErrorCode"] = _redact_upstream_message(error_code, api_key)
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
