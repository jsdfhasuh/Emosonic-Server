import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Dict, Mapping, Tuple


logger = logging.getLogger(__name__)

STRICT_V2_CONTRACT_SHA256 = (
    "4bf1a099fd3c060514215c202b7bb3c82b80e9c73959c39782541d8cda9dea96"
)
STRICT_V2_PROFILES = ("core", "follow", "handoff", "broadcast")  # type: Tuple[str, ...]

_CONFORMANCE_PATH = Path(__file__).with_name("strict_v2_conformance.json")
_LOCAL_TEST_EVIDENCE_PREFIX = "local-test-only:"


def _disabled_profiles() -> Dict[str, bool]:
    return {profile: False for profile in STRICT_V2_PROFILES}


def _validate_manifest(
    value: object,
    allow_local_test_evidence: bool = False,
) -> Dict[str, bool]:
    if not isinstance(value, dict):
        raise ValueError("root must be an object")
    if set(value) != {"schemaVersion", "contractSha256", "profiles"}:
        raise ValueError("root fields do not match the conformance schema")
    if value["schemaVersion"] != 1 or isinstance(value["schemaVersion"], bool):
        raise ValueError("schemaVersion must be integer 1")
    if value["contractSha256"] != STRICT_V2_CONTRACT_SHA256:
        raise ValueError("contractSha256 does not match the frozen contract")

    profiles = value["profiles"]
    if not isinstance(profiles, dict) or set(profiles) != set(STRICT_V2_PROFILES):
        raise ValueError("profiles must contain exactly core, follow, handoff, broadcast")

    readiness = {}  # type: Dict[str, bool]
    for profile in STRICT_V2_PROFILES:
        profile_value = profiles[profile]
        if not isinstance(profile_value, dict):
            raise ValueError("profile %s must be an object" % profile)
        if set(profile_value) != {"codeConformanceReady", "evidence"}:
            raise ValueError("profile %s fields do not match the schema" % profile)

        ready = profile_value["codeConformanceReady"]
        evidence = profile_value["evidence"]
        if not isinstance(ready, bool):
            raise ValueError("profile %s readiness must be a boolean" % profile)
        if not isinstance(evidence, list) or not all(
            isinstance(item, str) and item for item in evidence
        ):
            raise ValueError("profile %s evidence must contain non-empty strings" % profile)
        if ready and not evidence:
            raise ValueError("profile %s cannot be ready without evidence" % profile)
        if not ready and evidence:
            raise ValueError("profile %s cannot cite evidence while disabled" % profile)
        if ready and not allow_local_test_evidence and any(
            item.strip().casefold().startswith(_LOCAL_TEST_EVIDENCE_PREFIX)
            for item in evidence
        ):
            raise ValueError(
                "profile %s uses local-test-only evidence outside test mode" % profile
            )
        readiness[profile] = ready
    return readiness


@lru_cache(maxsize=2)
def _load_conformance_readiness(
    allow_local_test_evidence: bool = False,
) -> Dict[str, bool]:
    try:
        with _CONFORMANCE_PATH.open(encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
        return _validate_manifest(manifest, allow_local_test_evidence)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.error("Strict-v2 conformance manifest is unavailable: %s", exc)
        return _disabled_profiles()


def get_code_conformance_readiness(
    allow_local_test_evidence: bool = False,
) -> Dict[str, bool]:
    return dict(_load_conformance_readiness(allow_local_test_evidence))


def is_profile_code_conformance_ready(
    profile: str,
    allow_local_test_evidence: bool = False,
) -> bool:
    if profile not in STRICT_V2_PROFILES:
        return False
    return _load_conformance_readiness(allow_local_test_evidence)[profile]


def validate_conformance_manifest(
    value: Mapping[str, object],
    allow_local_test_evidence: bool = False,
) -> Dict[str, bool]:
    return _validate_manifest(dict(value), allow_local_test_evidence)
