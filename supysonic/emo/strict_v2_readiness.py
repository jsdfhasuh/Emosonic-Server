from typing import Dict, Mapping, Optional, Sequence

from .strict_v2_conformance import get_code_conformance_readiness
from .strict_v2_contract import STRICT_CAPABILITIES


PROFILE_CONFIG_KEYS = {
    "core": "emo_strict_v2_core_enabled",
    "follow": "emo_strict_v2_follow_enabled",
    "handoff": "emo_strict_v2_handoff_enabled",
    "broadcast": "emo_strict_v2_broadcast_enabled",
}


class CoreProfileNotReady(Exception):
    pass


def _enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def get_deployment_readiness(webapp_config: Mapping[str, object]) -> Dict[str, bool]:
    return {
        profile: _enabled(webapp_config.get(config_key, False))
        for profile, config_key in PROFILE_CONFIG_KEYS.items()
    }


def get_effective_profile_readiness(
    webapp_config: Mapping[str, object],
    code_readiness: Optional[Mapping[str, bool]] = None,
) -> Dict[str, bool]:
    code = dict(code_readiness or get_code_conformance_readiness())
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
) -> Dict[str, bool]:
    if set(client_capabilities) != set(STRICT_CAPABILITIES) or not all(
        isinstance(client_capabilities[name], bool) for name in STRICT_CAPABILITIES
    ):
        raise ValueError("client capabilities must contain exactly 9 booleans")

    role_set = set(roles)
    readiness = get_effective_profile_readiness(webapp_config, code_readiness)
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
        readiness["handoff"]
        and negotiated["effectiveAtPlayback"]
        and negotiated["playbackPrepare"]
        and can_handoff_target
    )

    can_use_broadcast = bool(role_set.intersection({"player", "controller"}))
    negotiated["supportsBroadcast"] = bool(
        readiness["broadcast"]
        and negotiated["supportsBroadcast"]
        and can_use_broadcast
    )
    return negotiated
