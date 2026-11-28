"""Keep Bilibili provenance and its bounded pool merge local to daily ranking."""

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Mapping, Optional, Sequence, Tuple

from peewee import DatabaseError, DoesNotExist, JOIN

from ..album_discovery_quota import SHANGHAI
from ..album_discovery_settings import (
    AlbumDiscoverySettingsError,
    album_discovery_target_user,
    load_album_discovery_runtime,
)
from ..db import AlbumDiscoveryRelease, Track, User
from ..recommendation_feedback import track_matches_negative_recommendation_feedback

logger = logging.getLogger(__name__)
READY_WINDOW = timedelta(days=14)
SOURCE_KINDS = ("direct", "collaboration", "style")


@dataclass(frozen=True)
class BilibiliReadyRecording:
    track: Track
    ready_at: datetime  # Naive UTC, like the persisted value.
    source_day: date
    kinds: Tuple[str, ...]


@dataclass(frozen=True)
class BilibiliSignal:
    kinds: Tuple[str, ...]
    age_days: int


@dataclass(frozen=True)
class BilibiliDailyPool:
    tracks: Tuple[Track, ...]
    signals: Mapping[str, BilibiliSignal]
    added_ids: Tuple[str, ...]
    displaced_ids: Tuple[str, ...]


def _evidence_kinds(value: str) -> Tuple[str, ...]:
    try:
        entries = json.loads(value)
    except (TypeError, ValueError, RecursionError):
        return ()
    if not isinstance(entries, list):
        return ()
    evidenced = {
        entry["kind"] for entry in entries
        if isinstance(entry, dict)
        and entry.get("kind") in SOURCE_KINDS
        and isinstance(entry.get("source"), dict) and entry["source"]
        and isinstance(entry.get("evidence"), str) and entry["evidence"].strip()
    }
    return tuple(
        kind for kind in SOURCE_KINDS if kind in evidenced
    )


def load_bilibili_ready_recordings(
    config: object,
    user: User,
    *,
    listened_ids: Iterable[object],
    preferences: Mapping[str, object],
    track_ids: Optional[Sequence[str]] = None,
    at: Optional[datetime] = None,
) -> Tuple[BilibiliReadyRecording, ...]:
    """Read only current source evidence for the configured user's library."""
    from .settings import bilibili_discovery_enabled

    if not bilibili_discovery_enabled(config) or (track_ids is not None and not track_ids):
        return ()
    try:
        if album_discovery_target_user(config) != user.name:
            return ()
        runtime = load_album_discovery_runtime(config)
    except AlbumDiscoverySettingsError:
        logger.warning("Bilibili daily source skipped because album discovery settings are invalid")
        return ()

    from ..db import BilibiliDiscoveryCandidate, BilibiliDiscoveryRun
    from .matcher import local_track_matches_candidate, playable_local_track
    from .orders import release_track_is_current

    current = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    query = (
        BilibiliDiscoveryCandidate
        .select(BilibiliDiscoveryCandidate, Track, BilibiliDiscoveryRun, AlbumDiscoveryRelease)
        .join(Track)
        .switch(BilibiliDiscoveryCandidate)
        .join(BilibiliDiscoveryRun)
        .switch(BilibiliDiscoveryCandidate)
        .join(AlbumDiscoveryRelease, JOIN.LEFT_OUTER)
        .where(
            BilibiliDiscoveryRun.user == user,
            BilibiliDiscoveryCandidate.status == "ready",
            BilibiliDiscoveryCandidate.ready_at <= current,
            BilibiliDiscoveryCandidate.ready_at > current - READY_WINDOW,
        )
    )
    if track_ids is not None:
        query = query.where(BilibiliDiscoveryCandidate.track.in_(track_ids))

    listened = {str(track_id) for track_id in listened_ids}
    output = []
    try:
        for candidate in query:
            try:
                track = candidate.track
                if str(track.id) in listened or track_matches_negative_recommendation_feedback(track, preferences):
                    continue
                kinds = _evidence_kinds(candidate.source_evidence_json)
                if not kinds or not playable_local_track(track, runtime.library_root):
                    continue
                linked = candidate.release_id is not None or bool(candidate.apple_song_id)
                current_match = (
                    release_track_is_current(candidate, track, runtime.library_key)
                    if linked else local_track_matches_candidate(track, candidate)
                )
                if current_match:
                    output.append(BilibiliReadyRecording(track, candidate.ready_at, candidate.run.source_day, kinds))
            except (DoesNotExist, OSError):
                # A rescan or unreadable file can invalidate a source recording at any time.
                continue
    except DatabaseError:
        logger.warning("Bilibili daily source evidence was unavailable")
        return ()
    return tuple(output)


def merge_bilibili_candidates(
    baseline: Sequence[Track],
    recordings: Iterable[BilibiliReadyRecording],
    capacity: int,
    *,
    at: datetime,
) -> BilibiliDailyPool:
    """Fill unused room, then replace only unannotated tracks from the tail."""
    if capacity <= 0:
        return BilibiliDailyPool((), {}, (), ())

    base = {}
    for track in baseline:
        base.setdefault(str(track.id), track)
        if len(base) >= capacity:
            break

    ordered = sorted(recordings, key=lambda item: (
        datetime.max - item.ready_at, -item.source_day.toordinal(), str(item.track.id), item.kinds,
    ))
    shanghai_day = at.astimezone(SHANGHAI).date()
    signals = {}
    outside = {}
    for item in ordered:
        track_id = str(item.track.id)
        if track_id in signals:
            continue
        signals[track_id] = BilibiliSignal(item.kinds, max(0, (shanghai_day - item.source_day).days))
        if track_id not in base:
            outside[track_id] = item.track

    spare = capacity - len(base)
    replaceable = [track_id for track_id in reversed(base) if track_id not in signals]
    count = min(len(outside), min(10, max(1, capacity // 10)), spare + len(replaceable))
    added_ids = tuple(outside)[:count]
    displaced = set(replaceable[:max(0, count - spare)])
    merged = {
        track_id: track for track_id, track in base.items() if track_id not in displaced
    }
    merged.update((track_id, outside[track_id]) for track_id in added_ids)
    return BilibiliDailyPool(
        tuple(merged.values()),
        {track_id: signals[track_id] for track_id in merged if track_id in signals},
        added_ids,
        tuple(track_id for track_id in base if track_id in displaced),
    )


def build_bilibili_daily_pool(
    config: object,
    user: User,
    baseline: Sequence[Track],
    capacity: int,
    *,
    listened_ids: Iterable[object],
    preferences: Mapping[str, object],
    at: Optional[datetime] = None,
) -> Optional[BilibiliDailyPool]:
    """Use the actual source eligibility checks for both generation and preview."""
    if capacity <= 0:
        return None
    current = at or datetime.now(timezone.utc)
    recordings = load_bilibili_ready_recordings(
        config, user, listened_ids=listened_ids, preferences=preferences, at=current,
    )
    if not recordings:
        return None
    pool = merge_bilibili_candidates(baseline, recordings, capacity, at=current)
    return pool if pool.signals else None
