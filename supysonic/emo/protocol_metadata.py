import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Dict, Set


logger = logging.getLogger(__name__)

_DESCRIPTOR_PATH = Path(__file__).with_name("strict_v2_registration_descriptor.json")
_BUILD_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_UNKNOWN_BUILD_COMMIT = "unknown"
STRICT_V2_CONNECTION_EPOCH = 1
_WARNED_BUILD_COMMIT_VALUES = set()  # type: Set[str]


def _require_non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(
            "Strict-v2 registration descriptor field "
            f"{field_name} must be a non-empty string"
        )
    return value


def _require_action_list(value: object, field_name: str) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError(
            "Strict-v2 registration descriptor field "
            f"{field_name} must be a non-empty list"
        )
    if not all(isinstance(action, str) and action for action in value):
        raise ValueError(
            "Strict-v2 registration descriptor field "
            f"{field_name} must contain non-empty strings"
        )


def _require_object(value: object, field_name: str) -> Dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(
            "Strict-v2 registration descriptor field "
            f"{field_name} must be an object"
        )
    return value


def _validate_descriptor(value: object) -> Dict[str, object]:
    descriptor = _require_object(value, "root")
    _require_non_empty_string(descriptor.get("protocolName"), "protocolName")
    _require_non_empty_string(descriptor.get("protocolVersion"), "protocolVersion")

    covered_actions = _require_object(descriptor.get("coveredActions"), "coveredActions")
    _require_action_list(
        covered_actions.get("clientToServer"),
        "coveredActions.clientToServer",
    )
    _require_action_list(
        covered_actions.get("serverToClient"),
        "coveredActions.serverToClient",
    )

    schema = _require_object(descriptor.get("schema"), "schema")
    _require_non_empty_string(schema.get("$schema"), "schema.$schema")
    return descriptor


@lru_cache(maxsize=1)
def _load_descriptor() -> Dict[str, object]:
    try:
        with _DESCRIPTOR_PATH.open(encoding="utf-8") as descriptor_file:
            descriptor = json.load(descriptor_file)
    except OSError as exc:
        raise RuntimeError(
            f"Unable to read strict-v2 registration descriptor at {_DESCRIPTOR_PATH}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid strict-v2 registration descriptor JSON at {_DESCRIPTOR_PATH}: {exc}"
        ) from exc

    try:
        return _validate_descriptor(descriptor)
    except ValueError as exc:
        raise ValueError(
            f"Invalid strict-v2 registration descriptor at {_DESCRIPTOR_PATH}: {exc}"
        ) from exc


def get_strict_v2_registration_descriptor() -> Dict[str, object]:
    return deepcopy(_load_descriptor())


def get_strict_v2_protocol_version() -> str:
    return _require_non_empty_string(
        _load_descriptor().get("protocolVersion"),
        "protocolVersion",
    )


def _fingerprint_source(descriptor: Mapping[str, object]) -> Dict[str, object]:
    return {
        "protocolName": descriptor["protocolName"],
        "coveredActions": descriptor["coveredActions"],
        "schema": descriptor["schema"],
    }


def calculate_strict_v2_schema_hash(fingerprint_source: Mapping[str, object]) -> str:
    if not isinstance(fingerprint_source, Mapping):
        raise ValueError("Strict-v2 schema hash input must be an object")

    try:
        canonical = json.dumps(
            dict(fingerprint_source),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Strict-v2 schema hash input must contain JSON-compatible values"
        ) from exc

    return hashlib.sha256(canonical).hexdigest()


@lru_cache(maxsize=1)
def _get_cached_schema_hash() -> str:
    return calculate_strict_v2_schema_hash(_fingerprint_source(_load_descriptor()))


def get_strict_v2_schema_hash() -> str:
    return _get_cached_schema_hash()


def _warn_unknown_build_commit(value: str) -> None:
    if value in _WARNED_BUILD_COMMIT_VALUES:
        return
    _WARNED_BUILD_COMMIT_VALUES.add(value)
    if value:
        logger.warning(
            "Invalid EMO_SERVER_BUILD_COMMIT value; reporting server build commit as unknown"
        )
    else:
        logger.warning(
            "EMO_SERVER_BUILD_COMMIT is not configured; reporting server build commit as unknown"
        )


def get_server_build_commit() -> str:
    value = os.environ.get("EMO_SERVER_BUILD_COMMIT", "")
    if _BUILD_COMMIT_PATTERN.fullmatch(value):
        return value

    _warn_unknown_build_commit(value)
    return _UNKNOWN_BUILD_COMMIT


def get_strict_v2_metadata() -> Dict[str, str]:
    return {
        "protocolVersion": get_strict_v2_protocol_version(),
        "schemaHash": get_strict_v2_schema_hash(),
        "serverBuildCommit": get_server_build_commit(),
    }


def get_strict_v2_registration_metadata(connection_nonce: str) -> Dict[str, object]:
    connection_nonce = _require_non_empty_string(
        connection_nonce,
        "connectionNonce",
    )
    metadata = get_strict_v2_metadata()
    metadata.update(
        {
            "connectionNonce": connection_nonce,
            "connectionEpoch": STRICT_V2_CONNECTION_EPOCH,
        }
    )
    return metadata
