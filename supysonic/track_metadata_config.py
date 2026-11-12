"""Resolve isolated LLM credentials and settings for track enrichment."""

import os
from pathlib import Path
from typing import Dict
from urllib.parse import urlsplit

from .llm_client import LlmConfigError


MAX_API_KEY_FILE_SIZE = 16384


def get_track_metadata_llm_config(config: object) -> Dict[str, object]:
    dedicated = getattr(config, "TRACK_METADATA_LLM", {})
    if not dedicated:
        return dict(getattr(config, "RECOMMENDATION_AGENT", {}))

    # Never combine a new endpoint with credentials from the legacy provider.
    resolved = dict(dedicated)
    endpoint = str(resolved.get("api_base_url") or "https://api.openai.com/v1").strip()
    try:
        parsed = urlsplit(endpoint)
        valid_endpoint = (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid_endpoint = False
    if not valid_endpoint:
        raise LlmConfigError("Track metadata API URL must not contain credentials or query parameters.")
    resolved["api_base_url"] = endpoint.rstrip("/")
    if not str(resolved.get("model") or "").strip():
        raise LlmConfigError("Track metadata LLM model is required.")

    sources = [
        name for name in ("api_key", "api_key_file", "api_key_env")
        if str(resolved.get(name) or "").strip()
    ]
    if len(sources) != 1:
        raise LlmConfigError("Configure exactly one track metadata API key source.")
    source = sources[0]
    value = str(resolved[source]).strip()
    if source == "api_key_file":
        path = Path(value)
        if not path.is_absolute():
            raise LlmConfigError("Track metadata API key file must use an absolute path.")
        try:
            with path.open("r", encoding="utf-8-sig") as key_file:
                value = key_file.read(MAX_API_KEY_FILE_SIZE + 1).strip()
        except (OSError, UnicodeError):
            raise LlmConfigError("Cannot read the track metadata API key file.") from None
    elif source == "api_key_env":
        value = os.environ.get(value, "").strip()
    if not value or len(value) > MAX_API_KEY_FILE_SIZE or any(c.isspace() for c in value):
        raise LlmConfigError("Track metadata API key source is empty or invalid.")
    resolved["api_key"] = value
    resolved.pop("api_key_file", None)
    resolved.pop("api_key_env", None)
    return resolved
