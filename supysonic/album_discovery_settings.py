"""Read the shared album discovery dispatch settings."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Union

from .config import DefaultConfig


AlbumDiscoveryConfig = Union[DefaultConfig, Mapping[str, object]]


class AlbumDiscoverySettingsError(ValueError):
    """The settings for new album submissions could not be loaded."""


@dataclass(frozen=True)
class AlbumDiscoverySettings:
    max_albums_per_day: int = 1
    auto_submit: bool = False


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def load_album_discovery_settings(
    config: AlbumDiscoveryConfig,
) -> AlbumDiscoverySettings:
    """Read the current JSON configuration, propagating errors to the caller."""
    section = (
        config.get("ALBUM_DISCOVERY", {})
        if isinstance(config, Mapping)
        else config.ALBUM_DISCOVERY
    )
    if not isinstance(section, Mapping):
        raise AlbumDiscoverySettingsError("[album_discovery] must contain settings.")
    settings_file = section.get("settings_file")
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
