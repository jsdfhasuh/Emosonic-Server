"""Read the shared album discovery dispatch settings."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Tuple, Union

from .config import DefaultConfig


AlbumDiscoveryConfig = Union[DefaultConfig, Mapping[str, object]]


class AlbumDiscoverySettingsError(ValueError):
    """The settings for new album submissions could not be loaded."""


@dataclass(frozen=True)
class AlbumDiscoverySettings:
    max_albums_per_day: int = 1
    auto_submit: bool = False


@dataclass(frozen=True)
class AlbumDiscoveryRuntimeSettings:
    target_user: str
    library_key: str
    library_root: Path
    auto_submit_hour: int
    oracle_url: Optional[str]


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _section(config: AlbumDiscoveryConfig) -> Mapping[str, object]:
    section = (
        config.get("ALBUM_DISCOVERY", {})
        if isinstance(config, Mapping)
        else getattr(config, "ALBUM_DISCOVERY", {})
    )
    if not isinstance(section, Mapping):
        raise AlbumDiscoverySettingsError("[album_discovery] must contain settings.")
    return section


def album_discovery_target_user(config: AlbumDiscoveryConfig) -> Optional[str]:
    target = _section(config).get("target_user")
    return target.strip() if isinstance(target, str) and target.strip() else None


def _text(section: Mapping[str, object], key: str, default: str = "") -> str:
    value = section.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise AlbumDiscoverySettingsError(f"[album_discovery] {key} must be text.")
    return value.strip()


def _secret(section: Mapping[str, object], key: str, environment: str) -> Optional[str]:
    path = _text(section, key)
    if path:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            raise AlbumDiscoverySettingsError(f"[album_discovery] cannot read {key}.") from None
        if not value:
            raise AlbumDiscoverySettingsError(f"[album_discovery] {key} is empty.")
        return value
    return os.environ.get(environment, "").strip() or None


def load_album_discovery_apple_token(config: AlbumDiscoveryConfig) -> Optional[str]:
    return _secret(_section(config), "apple_music_developer_token_file", "APPLE_MUSIC_DEVELOPER_TOKEN")


def load_album_discovery_oracle_tokens(
    config: AlbumDiscoveryConfig,
) -> Tuple[Optional[str], Optional[str]]:
    section = _section(config)
    return (
        _secret(section, "oracle_access_token_file", "ALBUM_DISCOVERY_ORACLE_ACCESS_TOKEN"),
        _secret(section, "oracle_delivery_token_file", "ALBUM_DISCOVERY_ORACLE_DELIVERY_TOKEN"),
    )


def album_discovery_library_key(config: AlbumDiscoveryConfig) -> str:
    library_key = _text(_section(config), "library_key", "main")
    if not 1 <= len(library_key) <= 128 or "\0" in library_key:
        raise AlbumDiscoverySettingsError("[album_discovery] invalid library_key.")
    return library_key


def load_album_discovery_runtime(config: AlbumDiscoveryConfig) -> AlbumDiscoveryRuntimeSettings:
    section = _section(config)
    user = album_discovery_target_user(config)
    if not user:
        raise AlbumDiscoverySettingsError("[album_discovery] target_user is required.")
    library_key = album_discovery_library_key(config)
    library_root = Path(_text(section, "library_root"))
    if not library_root.is_absolute():
        raise AlbumDiscoverySettingsError("[album_discovery] library_root must be an absolute path.")
    hour = section.get("auto_submit_hour", 20)
    if type(hour) is not int or not 0 <= hour <= 23:
        raise AlbumDiscoverySettingsError("[album_discovery] auto_submit_hour must be 0–23.")
    return AlbumDiscoveryRuntimeSettings(
        target_user=user,
        library_key=library_key,
        library_root=library_root,
        auto_submit_hour=hour,
        oracle_url=_text(section, "oracle_url") or None,
    )


def load_album_discovery_settings(
    config: AlbumDiscoveryConfig,
) -> AlbumDiscoverySettings:
    """Read the current JSON configuration, propagating errors to the caller."""
    settings_file = _section(config).get("settings_file")
    defaults = AlbumDiscoverySettings()
    if settings_file is None:
        return defaults
    if not isinstance(settings_file, str) or not settings_file.strip():
        raise AlbumDiscoverySettingsError(
            "[album_discovery] settings_file must be a nonempty path."
        )

    try:
        contents = Path(settings_file).read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        raise AlbumDiscoverySettingsError(
            f"Cannot read album discovery settings file: {settings_file}"
        ) from None
    try:
        root = json.loads(contents, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError):
        raise AlbumDiscoverySettingsError(
            f"Invalid JSON in album discovery settings file: {settings_file}"
        ) from None

    if not isinstance(root, dict):
        raise AlbumDiscoverySettingsError(
            "Album discovery settings must be a JSON object."
        )
    discovery = root.get("aiDiscovery", {})
    if not isinstance(discovery, dict):
        raise AlbumDiscoverySettingsError("aiDiscovery must be a JSON object.")

    max_albums = discovery.get("maxAlbumsPerDay", defaults.max_albums_per_day)
    if type(max_albums) is not int or max_albums < 0:
        raise AlbumDiscoverySettingsError(
            "aiDiscovery.maxAlbumsPerDay must be a nonnegative integer."
        )
    auto_submit = discovery.get("autoSubmit", defaults.auto_submit)
    if not isinstance(auto_submit, bool):
        raise AlbumDiscoverySettingsError("aiDiscovery.autoSubmit must be a boolean.")
    return AlbumDiscoverySettings(max_albums_per_day=max_albums, auto_submit=auto_submit)
