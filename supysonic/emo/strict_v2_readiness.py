from typing import Dict, Mapping, Optional, Sequence

from .strict_v2_contract import STRICT_CAPABILITIES


PROFILE_CONFIG_KEYS = {
    "core": "emo_strict_v2_core_enabled",
    "follow": "emo_strict_v2_follow_enabled",
    "handoff": "emo_strict_v2_handoff_enabled",
    "broadcast": "emo_strict_v2_broadcast_enabled",
}
PROFILE_IMPLEMENTATION_READY = {
    "core": True,
    "follow": True,
    "handoff": True,
    "broadcast": True,
}
BROADCAST_IMPLEMENTATION_READY = True


class CoreProfileNotReady(Exception):
    pass


def _enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def is_local_test_evidence_requested(
    webapp_config: Mapping[str, object],
) -> bool:
    return _enabled(
        webapp_config.get(
            "emo_strict_v2_allow_local_test_evidence",
            False,
        )
    )


def is_local_test_evidence_allowed(
    webapp_config: Mapping[str, object],
    app_testing: bool = False,
) -> bool:
    """Allow local evidence only in tests or an explicit development deployment."""
    if app_testing:
        return True
    return bool(
        _enabled(webapp_config.get("emo_development_mode", False))
        and is_local_test_evidence_requested(webapp_config)
    )


def get_deployment_readiness(webapp_config: Mapping[str, object]) -> Dict[str, bool]:
    return {
        profile: _enabled(webapp_config.get(config_key, False))
        for profile, config_key in PROFILE_CONFIG_KEYS.items()
    }


def get_code_conformance_readiness(
    allow_local_test_evidence: bool = False,
) -> Dict[str, bool]:
    """Return implementation support without metadata or evidence gates."""
    readiness = dict(PROFILE_IMPLEMENTATION_READY)
    readiness["broadcast"] = bool(BROADCAST_IMPLEMENTATION_READY)
    return readiness


def get_effective_profile_readiness(
    webapp_config: Mapping[str, object],
    code_readiness: Optional[Mapping[str, bool]] = None,
    allow_local_test_evidence: Optional[bool] = None,
) -> Dict[str, bool]:
    code = dict(
        code_readiness
        or get_code_conformance_readiness(
            bool(allow_local_test_evidence)
        )
    )
    deployment = get_deployment_readiness(webapp_config)
    return {
        profile: bool(code.get(profile, False) and deployment[profile])
        for profile in PROFILE_CONFIG_KEYS
    }


def negotiate_capabilities(
    client_capabilities: Mapping[str, bool],
    roles: Sequence[str],
    webapp_config: Mapping[str, object],
    code_readiness: Optional[Mapping[str, bool]] = None,
    allow_local_test_evidence: Optional[bool] = None,
) -> Dict[str, bool]:
    capability_fields = set(client_capabilities)
    if capability_fields != set(STRICT_CAPABILITIES) or not all(
        isinstance(client_capabilities[name], bool)
        for name in capability_fields
    ):
        raise ValueError(
            "client capabilities must contain exactly the 10 booleans"
        )

    role_set = set(roles)
    readiness = get_effective_profile_readiness(
        webapp_config,
        code_readiness,
        allow_local_test_evidence,
    )
    if not readiness["core"]:
        raise CoreProfileNotReady("strict-v2 Core profile is not ready")

    negotiated = {
        capability: bool(client_capabilities[capability])
        for capability in STRICT_CAPABILITIES
    }
    negotiated["playbackContextV2"] = True

    can_follow = "player" in role_set and negotiated["canPlay"]
    negotiated["supportsFollow"] = bool(
        readiness["follow"] and negotiated["supportsFollow"] and can_follow
    )

    can_handoff_target = "player" in role_set and negotiated["canPlay"]
    negotiated["playbackPrepare"] = bool(
        readiness["handoff"]
        and negotiated["playbackPrepare"]
        and can_handoff_target
    )
    negotiated["effectiveAtPlayback"] = bool(
        (readiness["handoff"] or readiness["broadcast"])
        and negotiated["effectiveAtPlayback"]
        and can_handoff_target
    )

    can_execute_broadcast_audio = bool(
        "player" in role_set
        and negotiated["effectiveAtPlayback"]
        and negotiated["canPlay"]
        and negotiated["canPause"]
        and negotiated["canSeek"]
    )
    can_use_broadcast = bool(
        "controller" in role_set or can_execute_broadcast_audio
    )
    negotiated["supportsBroadcast"] = bool(
        readiness["broadcast"]
        and negotiated["supportsBroadcast"]
        and can_use_broadcast
    )
    can_use_remote_volume = bool(
        "controller" in role_set
        or ("player" in role_set and negotiated["canSetVolume"])
    )
    negotiated["remoteVolumeControl"] = bool(
        negotiated["remoteVolumeControl"] and can_use_remote_volume
    )
    return negotiated
