"""Budgeted OpenAI calls shared by music profiles, daily selection and chat."""

from contextlib import contextmanager
import hashlib
import json
import logging
import math
import time
from types import SimpleNamespace
from typing import Mapping
from uuid import uuid4

import requests
from peewee import DatabaseError

from .db import Meta
from .llm_client import LlmConfigError, _extract_upstream_error
from .metadata_budget import BudgetExceeded, BudgetUnavailable, MetadataBudget
from .metadata_pricing import (
    MODEL, reserve_request_micro_usd, response_cost_micro_usd,
)
from .track_metadata_config import get_track_metadata_llm_config


logger = logging.getLogger(__name__)
STATE_KEY = "openai_recommendation_backoff"
BUDGET_FIELDS = (
    "monthly_budget_usd", "budget_safety_buffer_usd", "daily_budget_usd",
    "budget_timezone",
)


class OpenAIRecommendationError(Exception):
    def __init__(self, message: str, *, reason: str = "unavailable",
                 retry_at: int = 0) -> None:
        super().__init__(message)
        self.reason = reason
        self.retry_at = retry_at


def recommendation_settings(config: object) -> dict:
    """Carry the authoritative shared budget without copying metadata secrets."""
    if isinstance(config, Mapping):
        if "RECOMMENDATION_AGENT" not in config:
            return dict(config)
        settings = dict(config.get("RECOMMENDATION_AGENT") or {})
        metadata = config.get("TRACK_METADATA_LLM") or {}
    else:
        settings = dict(getattr(config, "RECOMMENDATION_AGENT", {}) or {})
        metadata = getattr(config, "TRACK_METADATA_LLM", {}) or {}
    settings["_budget_config"] = {
        key: metadata[key] for key in BUDGET_FIELDS if key in metadata
    }
    return settings


def validate_recommendation_settings(config: object) -> dict:
    settings = recommendation_settings(config)
    if settings.get("provider") != "openai" or settings.get("enabled") is not True:
        raise OpenAIRecommendationError("OpenAI recommendations are disabled.", reason="config")
    if str(settings.get("api_base_url", "")).rstrip("/") != "https://api.openai.com/v1":
        raise OpenAIRecommendationError("OpenAI recommendations require the official endpoint.", reason="config")
    if settings.get("model") != MODEL:
        raise OpenAIRecommendationError("Unsupported budgeted recommendation model.", reason="config")
    try:
        settings = get_track_metadata_llm_config(SimpleNamespace(TRACK_METADATA_LLM=settings))
        MetadataBudget(settings.get("_budget_config") or {})
        timeout = float(settings.get("timeout_seconds", 120))
        if not math.isfinite(timeout) or not 1 <= timeout <= 120:
            raise ValueError
        settings["timeout_seconds"] = timeout
        if settings.get("reasoning_effort", "low") not in ("none", "low", "medium", "high"):
            raise ValueError
    except (LlmConfigError, BudgetUnavailable, TypeError, ValueError):
        raise OpenAIRecommendationError("OpenAI recommendation settings are invalid.", reason="config") from None
    return settings


@contextmanager
def recommendation_lease(key: str, ttl_seconds: int = 600):
    """Short DB lease; no transaction is held across paid I/O."""
    if Meta._meta.database.in_transaction():
        raise OpenAIRecommendationError("Recommendation lease requires its own transaction.", reason="storage")
    if type(ttl_seconds) is not int or not 180 <= ttl_seconds <= 3600:
        raise OpenAIRecommendationError("Invalid recommendation lease duration.", reason="config")
    storage_key = "or:l:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:27]
    token = uuid4().hex
    encoded = json.dumps({"t": token, "u": int(time.time()) + ttl_seconds}, separators=(",", ":"))
    acquired = False
    try:
        with Meta._meta.database.atomic():
            row = Meta.get_or_none(Meta.key == storage_key)
            if row is None:
                Meta.insert(key=storage_key, value=encoded).on_conflict_ignore().execute()
                row = Meta.get_or_none(Meta.key == storage_key)
                acquired = row is not None and row.value == encoded
            else:
                state = json.loads(row.value)
                if type(state.get("u")) is not int or not isinstance(state.get("t"), str):
                    raise ValueError
                if state["u"] <= int(time.time()):
                    acquired = Meta.update(value=encoded).where(
                        Meta.key == storage_key, Meta.value == row.value,
                    ).execute() == 1
    except (DatabaseError, ValueError, TypeError, AttributeError):
        raise OpenAIRecommendationError("Recommendation lease storage is unavailable.", reason="storage") from None
    try:
        yield acquired
    finally:
        if acquired:
            try:
                Meta.delete().where(Meta.key == storage_key, Meta.value == encoded).execute()
            except DatabaseError:
                logger.warning("openai_recommendation_lease_release_failed")


def _read_state() -> dict:
    row = Meta.get_or_none(Meta.key == STATE_KEY)
    if row is None:
        return {"n": 0, "u": 0, "hard": False}
    try:
        state = json.loads(row.value)
        if (set(state) != {"n", "u", "hard"} or type(state["n"]) is not int
                or not 0 <= state["n"] <= 4 or type(state["u"]) is not int
                or type(state["hard"]) is not bool):
            raise ValueError
        return state
    except (TypeError, ValueError):
        raise OpenAIRecommendationError("Recommendation backoff state is invalid.", reason="storage") from None


def _pause(state: dict, *, hard: bool = False, retry_after: int = 0) -> int:
    count = min(4, state["n"] + 1)
    deadline = int(time.time()) + max(min(300 * 2 ** (count - 1), 1800), retry_after)
    value = json.dumps({"n": count, "u": deadline, "hard": hard}, separators=(",", ":"))
    row, created = Meta.get_or_create(key=STATE_KEY, defaults={"value": value})
    if not created:
        Meta.update(value=value).where(Meta.key == STATE_KEY).execute()
    return deadline


def _validate_json(value: object, schema: Mapping[str, object]) -> None:
    """Validate the deliberately small schema vocabulary used by these prompts."""
    kind = schema.get("type")
    valid = {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "integer": type(value) is int,
        "number": type(value) is int or (type(value) is float and math.isfinite(value)),
        "boolean": type(value) is bool, "null": value is None,
    }.get(kind, False)
    if not valid or ("enum" in schema and value not in schema["enum"]):
        raise ValueError("Invalid structured recommendation output.")
    if kind == "object":
        properties = schema.get("properties", {})
        if not set(schema.get("required", [])).issubset(value):
            raise ValueError("Missing structured output fields.")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError("Unexpected structured output fields.")
        for key, child in value.items():
            if key in properties:
                _validate_json(child, properties[key])
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 256):
            raise ValueError("Invalid structured array length.")
        for child in value:
            _validate_json(child, schema["items"])
    elif kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 16000):
            raise ValueError("Invalid structured text length.")
    elif kind in ("number", "integer"):
        if not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf):
            raise ValueError("Invalid structured number.")


def request_recommendation_json(config: object, messages: list, schema: dict, *,
                                workload: str, max_output_tokens: int = 4096) -> dict:
    """Authorize exactly one HTTP call; settle before parsing or exposing content."""
    if workload not in ("profile", "daily", "chat"):
        raise OpenAIRecommendationError("Invalid recommendation workload.", reason="config")
    settings = validate_recommendation_settings(config)
    wrapped = dict(schema) if "schema" in schema else {
        "name": "music_" + workload, "strict": True, "schema": schema,
    }
    payload = {
        "model": settings["model"], "service_tier": "default", "store": False,
        "messages": messages, "max_completion_tokens": max_output_tokens,
        "reasoning_effort": settings.get("reasoning_effort", "low"),
        "response_format": {"type": "json_schema", "json_schema": wrapped},
    }
    try:
        allowance = reserve_request_micro_usd(payload)
        budget = MetadataBudget(settings["_budget_config"])
        with recommendation_lease("provider") as acquired:
            if not acquired:
                raise OpenAIRecommendationError("OpenAI recommendations are busy.", reason="busy")
            state = _read_state()
            if state["hard"] or state["u"] > int(time.time()):
                raise OpenAIRecommendationError("OpenAI recommendations are paused.", reason="paused", retry_at=state["u"])
            reservation = budget.reserve("recommendation_" + workload, allowance)
            try:
                response = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": "Bearer " + settings["api_key"],
                             "Content-Type": "application/json"},
                    json=payload, timeout=settings["timeout_seconds"],
                    allow_redirects=False,
                )
            except requests.RequestException:
                deadline = _pause(state)
                raise OpenAIRecommendationError("OpenAI request failed; its reservation was retained.", reason="network", retry_at=deadline) from None
            try:
                if response.status_code != 200:
                    details = _extract_upstream_error(response, api_key=settings["api_key"])
                    # Never infer zero billable usage from an HTTP status alone.
                    hard = details.get("quotaExhausted", False) or not details.get("retryable", False)
                    retry_hint = details.get("retryAfterSeconds", 0)
                    retry_hint = min(86400, max(0, int(retry_hint))) if isinstance(retry_hint, (int, float)) and math.isfinite(retry_hint) else 0
                    deadline = _pause(state, hard=hard, retry_after=retry_hint)
                    raise OpenAIRecommendationError("OpenAI rejected the recommendation request.", reason="upstream", retry_at=deadline)
                try:
                    result = response.json()
                except ValueError:
                    result = None
            finally:
                response.close()
            cost = response_cost_micro_usd(result) if isinstance(result, dict) else None
            if cost is None:
                _pause(state, hard=True)
                raise OpenAIRecommendationError("OpenAI usage could not be verified; reservation retained.", reason="accounting")
            try:
                budget.settle(reservation, cost)
            except BudgetUnavailable:
                _pause(state, hard=True)
                raise
            if cost > allowance:
                _pause(state, hard=True)
                raise OpenAIRecommendationError("OpenAI usage exceeded its reservation.", reason="accounting")
            Meta.delete().where(Meta.key == STATE_KEY).execute()
            logger.info("openai_recommendation_accounted workload=%s cost_micro_usd=%s", workload, cost)
            try:
                choice = result["choices"][0]
                if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
                    raise ValueError
                parsed = json.loads(choice["message"]["content"])
                _validate_json(parsed, wrapped["schema"])
                if not isinstance(parsed, dict):
                    raise ValueError
                return parsed
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                raise OpenAIRecommendationError("OpenAI returned invalid recommendation JSON.", reason="invalid_response") from None
    except BudgetExceeded as exc:
        raise OpenAIRecommendationError("The shared OpenAI spending budget is exhausted.", reason="budget", retry_at=int(exc.reset_at.timestamp())) from None
    except (BudgetUnavailable, DatabaseError, LlmConfigError):
        raise OpenAIRecommendationError("OpenAI recommendation accounting is unavailable.", reason="accounting") from None
