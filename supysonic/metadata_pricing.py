"""Conservative software accounting for supported OpenAI workloads."""

import json
from decimal import Decimal, ROUND_CEILING
from typing import Mapping, Optional

from .llm_client import LlmConfigError


MODEL = "gpt-6-luna"
PROFILE_MODEL = "gpt-6.2-sol"
MAX_REQUEST_BYTES = 65536
MAX_OUTPUT_TOKENS = 4096
SOL_CONTEXT_TOKENS = 1_050_000
SOL_MAX_INPUT_TOKENS = 922_000
SOL_MAX_OUTPUT_TOKENS = 128_000
LONG_INPUT_THRESHOLD = 272_000
WEB_RESERVATION_MICRO_USD = 1000000
PRICE_VERSION = "gpt-6-luna-standard-2026-11-12"
# "discovery" remains valid for callers using the original Sol pricing entry point.
_SOL_RESPONSES_KINDS = ("responses", "discovery")

# Standard rates in micro-USD per token. Input accounts for the most
# expensive cache-write rate documented for each model.
_TOKEN_RATES = {
    MODEL: ((Decimal("0.25"), Decimal("0.50")),
            (Decimal("0.50"), Decimal("0.75"))),
    PROFILE_MODEL: ((Decimal("5"), Decimal("10")),
                    (Decimal("10"), Decimal("15"))),
}


def _integer(value: object, minimum: int = 0) -> Optional[int]:
    if type(value) is not int or value < minimum:
        return None
    return value


def _token_rates(model: str, inputs: int):
    short_rates, long_rates = _TOKEN_RATES[model]
    return long_rates if inputs > LONG_INPUT_THRESHOLD else short_rates


def _supports_request(kind: str, model: str) -> bool:
    if kind in _SOL_RESPONSES_KINDS:
        return model == PROFILE_MODEL
    if kind == "web":
        return model == MODEL
    return kind == "text" and model in _TOKEN_RATES


class ContextLimitExceeded(LlmConfigError):
    pass


def validated_request_size(payload: Mapping[str, object], kind: str, model: str) -> int:
    if not _supports_request(kind, model) or payload.get("model") != model:
        raise LlmConfigError("Unsupported budgeted model or request type.")
    if payload.get("service_tier") != "default":
        raise LlmConfigError("Budgeted metadata requires standard processing.")
    try:
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise LlmConfigError("Invalid budgeted metadata request.") from None
    if kind not in _SOL_RESPONSES_KINDS and size > MAX_REQUEST_BYTES:
        raise LlmConfigError("Budgeted metadata request is too large.")
    name = "max_completion_tokens" if kind == "text" else "max_output_tokens"
    maximum = _integer(payload.get(name), 1)
    output_limit = SOL_MAX_OUTPUT_TOKENS if kind in _SOL_RESPONSES_KINDS else MAX_OUTPUT_TOKENS
    if maximum is None or maximum > output_limit:
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
    payload: Mapping[str, object], kind: str = "text", *, model: str = MODEL,
    measured_input_tokens: Optional[int] = None,
) -> int:
    """Reserve before I/O; web allowance is deliberately above sampled costs.

    The web allowance is a conservative application limit, not an assertion
    about a provider invoice. Missing usage must never release this allowance.
    """
    size = validated_request_size(payload, kind, model)
    if kind == "web":
        return WEB_RESERVATION_MICRO_USD
    # UTF-8 bytes overestimate visible input tokens; leave room for framing and
    # bill all input at the higher 12-hour cache-write price, without discounts.
    tokens = size + 4096
    output = payload["max_completion_tokens" if kind == "text" else "max_output_tokens"]
    if kind in _SOL_RESPONSES_KINDS:
        if measured_input_tokens is not None:
            tokens = _integer(measured_input_tokens, 1)
            if tokens is None:
                raise LlmConfigError("Invalid input token count.")
        elif size > MAX_REQUEST_BYTES:
            raise LlmConfigError("Long Responses input requires the model's token count.")
        if tokens > SOL_MAX_INPUT_TOKENS or tokens + output > SOL_CONTEXT_TOKENS:
            raise ContextLimitExceeded("Recommendation exceeds the Sol context window.")
    input_rate, output_rate = _token_rates(model, tokens)
    return int((tokens * input_rate + output * output_rate).to_integral_value(
        rounding=ROUND_CEILING
    ))


def response_cost_micro_usd(
    response: Mapping[str, object], model: str = MODEL, kind: str = "text"
) -> Optional[int]:
    """Return a conservative charge, or None when usage cannot be reconciled.

    Amounts are micro-USD. Discounted cache reads are intentionally charged as
    ordinary input at the maximum cache-write rate to avoid under-accounting.
    """
    if (
        not _supports_request(kind, model)
        or response.get("model") != model
        or response.get("service_tier") != "default"
    ):
        return None
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return None
    input_name = "prompt_tokens" if kind == "text" else "input_tokens"
    output_name = "completion_tokens" if kind == "text" else "output_tokens"
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
    input_rate, output_rate = _token_rates(model, inputs)
    return int((Decimal(inputs) * input_rate + Decimal(outputs) * output_rate +
                Decimal(searches) * 10000).to_integral_value(rounding=ROUND_CEILING))
