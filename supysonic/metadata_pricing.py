"""Conservative software accounting for the supported metadata model only."""

import json
from decimal import Decimal, ROUND_CEILING
from typing import Mapping, Optional

from .llm_client import LlmConfigError


MODEL = "gpt-6-luna"
MAX_REQUEST_BYTES = 65536
MAX_OUTPUT_TOKENS = 4096
WEB_RESERVATION_MICRO_USD = 1000000
PRICE_VERSION = "gpt-6-luna-standard-2026-11-12"


def _integer(value: object, minimum: int = 0) -> Optional[int]:
    if type(value) is not int or value < minimum:
        return None
    return value


def _validate_request(payload: Mapping[str, object], kind: str) -> int:
    if payload.get("model") != MODEL or kind not in ("text", "web"):
        raise LlmConfigError("Metadata pricing supports gpt-6-luna only.")
    if payload.get("service_tier") != "default":
        raise LlmConfigError("Budgeted metadata requires standard processing.")
    try:
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise LlmConfigError("Invalid budgeted metadata request.") from None
    if size > MAX_REQUEST_BYTES:
        raise LlmConfigError("Budgeted metadata request is too large.")
    name = "max_output_tokens" if kind == "web" else "max_completion_tokens"
    maximum = _integer(payload.get(name), 1)
    if maximum is None or maximum > MAX_OUTPUT_TOKENS:
        raise LlmConfigError("Budgeted metadata requires a bounded output limit.")
    if kind == "web":
        calls = _integer(payload.get("max_tool_calls"), 1)
        tools = payload.get("tools")
        if calls is None or calls > 3 or tools != [
            {"type": "web_search", "search_context_size": "low"}
        ]:
            raise LlmConfigError("Budgeted web metadata requires bounded search.")
    elif payload.get("tools") or payload.get("n", 1) != 1:
        raise LlmConfigError("Text metadata pricing requires one tool-free completion.")
    return size


def reserve_request_micro_usd(
    payload: Mapping[str, object], kind: str = "text"
) -> int:
    """Reserve before I/O; web allowance is deliberately above sampled costs.

    The web allowance is a conservative application limit, not an assertion
    about a provider invoice. Missing usage must never release this allowance.
    """
    size = _validate_request(payload, kind)
    if kind == "web":
        return WEB_RESERVATION_MICRO_USD
    # UTF-8 bytes overestimate visible input tokens; leave room for framing and
    # bill all input at the higher 12-hour cache-write price, without discounts.
    tokens = size + 4096
    output = payload["max_completion_tokens"]
    return int((Decimal(tokens) * Decimal("0.25") +
                Decimal(output) * Decimal("0.50")).to_integral_value(
                    rounding=ROUND_CEILING))


def response_cost_micro_usd(
    response: Mapping[str, object], model: str = MODEL, kind: str = "text"
) -> Optional[int]:
    """Return a conservative charge, or None when usage cannot be reconciled.

    Amounts are micro-USD. Discounted cache reads are intentionally charged as
    ordinary input at the maximum cache-write rate to avoid under-accounting.
    """
    if (
        model != MODEL or kind not in ("text", "web")
        or response.get("model") != model
        or response.get("service_tier") != "default"
    ):
        return None
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return None
    input_name = "input_tokens" if kind == "web" else "prompt_tokens"
    output_name = "output_tokens" if kind == "web" else "completion_tokens"
    inputs = _integer(usage.get(input_name))
    outputs = _integer(usage.get(output_name))
    if inputs is None or outputs is None:
        return None
    total = usage.get("total_tokens")
    if total is not None and (_integer(total) is None or total != inputs + outputs):
        return None
    details = usage.get(input_name.replace("tokens", "tokens_details"), {})
    if not isinstance(details, Mapping):
        return None
    known = []
    for name in ("cached_tokens", "cache_write_tokens"):
        value = details.get(name, 0)
        if _integer(value) is None:
            return None
        known.append(value)
    if sum(known) > inputs:
        return None
    searches = 0
    if kind == "web":
        tool_usage = response.get("tool_usage")
        if not isinstance(tool_usage, Mapping):
            return None
        search_usage = tool_usage.get("web_search")
        if not isinstance(search_usage, Mapping):
            return None
        searches = _integer(search_usage.get("num_requests"))
        if searches is None:
            return None
    long_context = inputs > 272000
    input_rate = Decimal("0.50" if long_context else "0.25")
    output_rate = Decimal("0.75" if long_context else "0.50")
    return int((Decimal(inputs) * input_rate + Decimal(outputs) * output_rate +
                Decimal(searches) * 10000).to_integral_value(rounding=ROUND_CEILING))
