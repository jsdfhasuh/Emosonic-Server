"""Use the existing CN release, quota, Oracle order and inventory records."""

import json
import logging
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Mapping, Optional, Sequence, Tuple
from uuid import UUID

from ..album_discovery_catalog import CatalogAlbum, CatalogError
from ..album_discovery_daily import _matched_track_ids
from ..album_discovery_delivery import AlbumReconciliation, verified_file_is_current
from ..album_discovery_quota import (
    AlbumReservationResult,
    get_album_download_order,
    record_album_discovery_reuse,
    reserve_album_download,
)
from ..album_discovery_service import (
    AlbumDiscoveryConfirmation,
    AlbumDiscoveryError,
    AlbumDiscoveryService,
    _catalog_for_config,
    _current_time,
)
from ..album_discovery_settings import (
    AlbumDiscoverySettingsError,
    load_album_discovery_settings,
)
from ..db import (
    AlbumDiscoveryRelease,
    BilibiliDiscoveryCandidate,
    BilibiliDiscoveryRun,
    Track,
    User,
    connection_scope,
)
from .settings import BilibiliDiscoverySettingsError, load_bilibili_discovery_settings


logger = logging.getLogger(__name__)
SOURCE_WINDOW = timedelta(days=14)


def _utc_naive(now: Optional[datetime]) -> datetime:
    return _current_time(now).astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def _switches_allow_submission(service: AlbumDiscoveryService) -> bool:
    try:
        settings = load_bilibili_discovery_settings(service.config)
    except BilibiliDiscoverySettingsError:
        return False
    return settings.enabled and settings.auto_download


def _fresh_source_day(source_day: date, today: date) -> bool:
    return source_day < today < source_day + SOURCE_WINDOW


def _song_evidence(
    release: AlbumDiscoveryRelease, song_id: str,
) -> Optional[Tuple[UUID, Mapping[str, object]]]:
    if release.ready_at is None:
        return None
    try:
        expected = json.loads(release.expected_tracks)["tracks"]
        inventory = json.loads(release.reconciliation)
        if not isinstance(expected, list) or not isinstance(inventory, dict):
            return None
        expected_ids = {track["song_id"] for track in expected}
        matched = _matched_track_ids(inventory)
        if (
            song_id not in matched or inventory.get("ready") is not True
            or len(expected_ids) != len(expected)
            or matched.keys() != expected_ids
            or len(set(matched.values())) != len(expected)
            or len(inventory["tracks"]) != len(expected)
        ):
            return None
        item = next(track for track in inventory["tracks"] if track["song_id"] == song_id)
        if item.get("album_id") != inventory.get("local_album_id"):
            return None
        return matched[song_id], item
    except (KeyError, TypeError, ValueError):
        return None


def release_track_is_current(
    candidate: BilibiliDiscoveryCandidate, track: Track, library_key: str,
) -> bool:
    """Check the downloaded target's mapping and file against verified inventory."""
    release = candidate.release
    if not release or release.library_key != library_key or not candidate.apple_song_id:
        return False
    match = _song_evidence(release, candidate.apple_song_id)
    return match is not None and (
        match[0] == track.id
        and match[1].get("album_id") == str(track.album_id)
        and verified_file_is_current(track.path, match[1])
    )


def _link_candidate(
    candidate: BilibiliDiscoveryCandidate, release: AlbumDiscoveryRelease,
    song_id: str, now: Optional[datetime],
) -> None:
    model = BilibiliDiscoveryCandidate
    if model.update(
        release=release, apple_song_id=song_id, status="waiting_album",
        reason="waiting_for_album", updated_at=_utc_naive(now),
    ).where(
        model.id == candidate.id,
        model.status == "pending",
        model.release.is_null(True),
        model.track.is_null(True),
    ).execute():
        return
    if not model.select().where(
        model.id == candidate.id,
        model.release == release,
        model.apple_song_id == song_id,
        model.status.in_(("waiting_album", "ready")),
    ).exists():
        raise AlbumDiscoveryError("The Bilibili target changed before order association.")


def confirm_bilibili_album(
    service: AlbumDiscoveryService, user: User, candidate: BilibiliDiscoveryCandidate,
    album: CatalogAlbum, song_id: str, *, now: Optional[datetime] = None,
) -> AlbumDiscoveryConfirmation:
    """Associate a validated CN target, reserving an order only for a new album."""
    from .matcher import bilibili_target_allowed

    service._check_user(user)
    with connection_scope():
        run = candidate.run
        if run.user_id != user.id:
            raise AlbumDiscoveryError("The Bilibili target belongs to another account.")
        release = service._get_release(album.album_id)
    # Recheck structural guarantees even when an adapter supplied a CatalogAlbum.
    try:
        album = CatalogAlbum.from_payload(album.to_payload())
    except ValueError as exc:
        raise AlbumDiscoveryError("The China release is invalid or incomplete.") from exc
    if song_id not in {track.song_id for track in album.tracks}:
        raise AlbumDiscoveryError("The Bilibili target is not on the confirmed China release.")

    def persist(inventory: AlbumReconciliation) -> None:
        linked_release = service._save_release(album, inventory)
        _link_candidate(candidate, linked_release, song_id, now)

    def reserve(persist_release: Callable[[], None]) -> AlbumReservationResult:
        def load_policy() -> Tuple[int, bool]:
            limit = load_album_discovery_settings(service.config).max_albums_per_day
            return (
                limit, _switches_allow_submission(service)
                and _fresh_source_day(run.source_day, _current_time(now).date()),
            )

        return reserve_album_download(
            service.library_key, album.album_id, load_policy,
            now=now, persist_selection=persist_release,
        )

    if release and _song_evidence(release, song_id):
        # A shared, ready release already carries the whole verified inventory.
        def reuse() -> None:
            current = service._get_release(album.album_id)
            match = _song_evidence(current, song_id) if current else None
            if not match:
                raise AlbumDiscoveryError("The China release requires reconciliation again.")
            inventory = AlbumReconciliation.from_payload(json.loads(current.reconciliation))
            persist(inventory)

        order = record_album_discovery_reuse(service.library_key, album.album_id, reuse, now=now)
        result = AlbumDiscoveryConfirmation(album.album_id, "pending", order.id if order else None)
    else:
        result = service._confirm_release(
            album, song_id, persist, reserve, now=now,
            can_submit=lambda: bilibili_target_allowed(service, user, candidate, album, song_id),
        )

    if refresh_bilibili_candidate(service, candidate, now=now):
        return replace(result, status="ready")
    # The core confirms whole-album readiness, but only the current target's
    # verified Track can mark this Bilibili candidate ready.
    return replace(result, status="pending") if result.status == "ready" else result


def bilibili_recovery_allowed(
    service: AlbumDiscoveryService,
    frozen: CatalogAlbum,
    candidates: Sequence[BilibiliDiscoveryCandidate],
    *,
    now: Optional[datetime] = None,
) -> Optional[bool]:
    """True permits replaying this reservation, None indicates a temporary failure."""
    from .matcher import bilibili_target_allowed

    if not candidates or not _switches_allow_submission(service):
        return False
    today = _current_time(now).date()
    eligible = []
    with connection_scope():
        for candidate in candidates:
            if candidate.status != "waiting_album":
                continue
            run = candidate.run
            if _fresh_source_day(run.source_day, today) and run.user.name == service.runtime.target_user:
                eligible.append((candidate, run.user))
    if not eligible:
        return False
    try:
        if load_album_discovery_settings(service.config).max_albums_per_day == 0:
            return False
        if service.catalog is None:
            service.catalog = _catalog_for_config(service.config)
        live = service.catalog.get_release(frozen.album_id, refresh=True)
        if live is None or live.album_id != frozen.album_id or live.tracks != frozen.tracks:
            return False
        CatalogAlbum.from_payload(live.to_payload())
        for candidate, user in eligible:
            if bilibili_target_allowed(service, user, candidate, live, candidate.apple_song_id):
                return _switches_allow_submission(service)
    except (AlbumDiscoverySettingsError, CatalogError) as exc:
        logger.info("Bilibili reserved album %s recheck: %s", frozen.album_id, type(exc).__name__)
        return None
    except ValueError:
        return False
    return False


def refresh_bilibili_candidate(
    service: AlbumDiscoveryService, candidate: BilibiliDiscoveryCandidate,
    *, now: Optional[datetime] = None,
) -> bool:
    """Project the current per-song proof from a committed whole-album inventory."""
    model = BilibiliDiscoveryCandidate
    release_model = AlbumDiscoveryRelease
    with connection_scope():
        current = (
            model.select(model, BilibiliDiscoveryRun, release_model)
            .join(BilibiliDiscoveryRun)
            .switch(model)
            .join(release_model)
            .where(
                model.id == candidate.id,
                model.status.in_(("waiting_album", "ready")),
                release_model.library_key == service.library_key,
            ).first()
        )
        if current is None or not current.apple_song_id:
            return False
        release = current.release
        match = _song_evidence(release, current.apple_song_id)
        track = Track.get_or_none(Track.id == match[0]) if match else None
    is_ready = track is not None and release_track_is_current(current, track, service.library_key)
    updated = _utc_naive(now)
    values = None
    if is_ready and (current.status != "ready" or current.track_id != track.id or not current.ready_at):
        values = dict(status="ready", reason="", track=track, ready_at=updated, next_attempt_at=None)
    elif not is_ready and (current.status == "ready" or release.ready_at is not None):
        values = dict(
            status="waiting_album", reason="verified_track_unavailable", track=None, ready_at=None,
        )
    elif not is_ready and release.oracle_status == "rejected" and not get_album_download_order(
        service.library_key, release.album_id,
    ):
        values = dict(status="skipped", reason="oracle_rejected", next_attempt_at=None)
    if values is None:
        return is_ready
    same_inventory = (
        (release_model.id == release.id) & (release_model.ready_at == release.ready_at)
        & (release_model.reconciliation == release.reconciliation)
        & (release_model.checked_at == release.checked_at)
    )
    still_verified = release_model.select(release_model.id).where(same_inventory)
    with connection_scope():
        saved = model.update(**values, updated_at=updated).where(
            model.id == current.id, model.release.in_(still_verified),
            model.status == current.status, model.track == current.track_id,
            model.apple_song_id == current.apple_song_id,
        ).execute()
        if saved and not is_ready and release.ready_at is not None:
            release_model.update(ready_at=None).where(same_inventory).execute()
    return bool(saved and is_ready)
