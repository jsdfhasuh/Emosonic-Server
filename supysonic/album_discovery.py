from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from .album_discovery_quota import (
    AlbumDownloadQuota,
    AlbumReservationResult,
    get_album_download_quota,
    reserve_album_download,
)
from .album_discovery_settings import (
    AlbumDiscoveryConfig,
    AlbumDiscoverySettingsError,
    load_album_discovery_settings,
)


@dataclass(frozen=True)
class AlbumDiscoveryAvailability:
    quota: AlbumDownloadQuota
    auto_submit: bool
    settings_error: Optional[str] = None


def get_album_discovery_availability(
    config: AlbumDiscoveryConfig, library_key: str, *, now: Optional[datetime] = None
) -> AlbumDiscoveryAvailability:
    """Read the quota for a preview without reserving a slot."""
    try:
        settings = load_album_discovery_settings(config)
    except AlbumDiscoverySettingsError as exc:
        return AlbumDiscoveryAvailability(
            get_album_download_quota(library_key, 0, now=now), False, str(exc)
        )
    return AlbumDiscoveryAvailability(
        get_album_download_quota(library_key, settings.max_albums_per_day, now=now),
        settings.auto_submit,
    )


def reserve_discovered_album(
    config: AlbumDiscoveryConfig,
    library_key: str,
    album_id: str,
    *,
    automatic: bool,
    now: Optional[datetime] = None,
) -> AlbumReservationResult:
    """Reload settings and reserve a slot before submitting a verified album.

    Only a result with status ``reserved`` permits a new Oracle request.
    Invalid settings raise ``AlbumDiscoverySettingsError`` and leave the quota intact.
    """
    def load_policy() -> Tuple[int, bool]:
        settings = load_album_discovery_settings(config)
        return settings.max_albums_per_day, not automatic or settings.auto_submit

    return reserve_album_download(library_key, album_id, load_policy, now=now)
