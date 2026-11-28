from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class BilibiliDiscoverySettings:
    enabled: bool
    auto_download: bool
    history_api_url: str
    history_api_token_file: str
    start_time: time


class BilibiliDiscoverySettingsError(ValueError):
    pass


def _settings_section(config: object) -> Mapping[str, object]:
    if isinstance(config, Mapping):
        return config.get("BILIBILI_DISCOVERY", {})
    return getattr(config, "BILIBILI_DISCOVERY", {})


def bilibili_discovery_enabled(config: object) -> bool:
    return _settings_section(config).get("enabled") is True


def load_bilibili_discovery_settings(config: object) -> BilibiliDiscoverySettings:
    section = _settings_section(config)
    try:
        start_time = time.fromisoformat(str(section.get("start_time", "04:30")))
        if start_time.tzinfo is not None:
            raise ValueError
    except ValueError as exc:
        raise BilibiliDiscoverySettingsError("Invalid Bilibili Shanghai start time") from exc
    return BilibiliDiscoverySettings(
        enabled=bilibili_discovery_enabled(config),
        auto_download=section.get("auto_download") is True,
        history_api_url=str(section.get("history_api_url") or "").strip(),
        history_api_token_file=str(section.get("history_api_token_file") or "").strip(),
        start_time=start_time,
    )
