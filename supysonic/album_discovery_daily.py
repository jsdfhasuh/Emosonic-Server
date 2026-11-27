"""Place verified discoveries in daily playlists and measure observed listening."""

import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Set
from uuid import UUID

from .album_discovery_delivery import verified_file_is_current
from .album_discovery_quota import SHANGHAI, STOREFRONT, SUBMITTED
from .db import (
    AlbumDiscoveryOrder,
    AlbumDiscoveryPlacement,
    AlbumDiscoveryRelease,
    AlbumDiscoverySelection,
    Playlist,
    StarredTrack,
    Track,
    User,
    User_Play_Activity,
    now as database_now,
)
from .recommendation_feedback import (
    get_recommendation_feedback_preferences,
    track_matches_negative_recommendation_feedback,
)
from .recommendation_selection import DEFAULT_MAX_ARTIST_RATIO


DISCOVERY_POSITION = 4
OBSERVATION_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class DailyDiscovery:
    selection: AlbumDiscoverySelection
    track: Track
    position: int
    tracks: List[Track]


def get_ready_discovery_selections(
    user: User, day: date
) -> List[AlbumDiscoverySelection]:
    if AlbumDiscoveryPlacement.select().where(
        (AlbumDiscoveryPlacement.user == user) & (AlbumDiscoveryPlacement.day == day)
    ).exists():
        return []

    cutoff = min(database_now(), datetime.combine(day, datetime.max.time()))
    placed = AlbumDiscoveryPlacement.select(AlbumDiscoveryPlacement.selection).where(
        AlbumDiscoveryPlacement.user == user
    )
    return list(
        AlbumDiscoverySelection.select(AlbumDiscoverySelection, AlbumDiscoveryRelease, Track)
        .join(AlbumDiscoveryRelease)
        .switch(AlbumDiscoverySelection)
        .join(Track, on=AlbumDiscoverySelection.representative_track)
        .where(
            (AlbumDiscoverySelection.user == user)
            & (AlbumDiscoveryRelease.ready_at <= cutoff)
            & AlbumDiscoverySelection.id.not_in(placed)
        )
        .order_by(
            AlbumDiscoverySelection.priority,
            AlbumDiscoverySelection.created_at,
            AlbumDiscoverySelection.id,
        )
    )


def _with_representative(
    tracks: Sequence[Track], representative: Track, requested_count: int, position: int,
) -> List[Track]:
    kept = list(tracks)
    for index, track in enumerate(kept):
        if track.id == representative.id:
            del kept[index]
            break
    else:
        counts = Counter(track.artist_id for track in kept)
        artist_cap = max(1, math.ceil(requested_count * DEFAULT_MAX_ARTIST_RATIO))
        remove_at = len(kept) - 1
        if counts[representative.artist_id] >= artist_cap:
            # Keep the ordinary artist count intact even when the original list
            # had to relax its cap to fill the available slots.
            remove_at = next(
                index for index in range(remove_at, -1, -1)
                if kept[index].artist_id == representative.artist_id
            )
        del kept[remove_at]

    kept.insert(position - 1, representative)
    return kept


def select_daily_discovery(
    user: User,
    selections: Sequence[AlbumDiscoverySelection],
    tracks: Sequence[Track],
    requested_count: int,
    recent_track_ids: Set[str],
) -> Optional[DailyDiscovery]:
    if not tracks or not selections:
        return None

    played_ids = {
        activity.track_id for activity in User_Play_Activity.select(User_Play_Activity.track)
        .where(
            (User_Play_Activity.user == user)
            & User_Play_Activity.track.in_(
                [selection.representative_track_id for selection in selections]
            )
        ).distinct()
    }
    preferences = get_recommendation_feedback_preferences(user)
    position = min(DISCOVERY_POSITION, len(tracks))
    for selection in selections:
        track = selection.representative_track
        if (
            track.id in played_ids
            or str(track.id) in recent_track_ids
            or track_matches_negative_recommendation_feedback(track, preferences)
            or not _matches_current_library(selection, track)
        ):
            continue
        return DailyDiscovery(
            selection, track, position,
            _with_representative(tracks, track, requested_count, position),
        )
    return None


def record_daily_discovery(
    user: User, playlist: Playlist, day: date, discovery: DailyDiscovery,
) -> None:
    AlbumDiscoveryPlacement.create(
        selection=discovery.selection,
        user=user,
        playlist_id=playlist.id,
        day=day,
        track_id=discovery.track.id,
        position=discovery.position,
    )


def _read_json(value: str) -> object:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def get_playlist_discovery_reason(
    user: Optional[User], playlist: Playlist,
) -> Dict[str, str]:
    if user is None or playlist.user_id != user.id:
        return {}
    placement = (
        AlbumDiscoveryPlacement.select(AlbumDiscoveryPlacement, AlbumDiscoverySelection)
        .join(AlbumDiscoverySelection)
        .where(
            (AlbumDiscoveryPlacement.user == user)
            & (AlbumDiscoveryPlacement.playlist_id == playlist.id)
            & (AlbumDiscoverySelection.user == user)
        )
        .first()
    )
    if placement is None:
        return {}

    selection = placement.selection
    evidence = _read_json(selection.evidence)
    titles = []
    if isinstance(evidence, list):
        for item in evidence:
            if (
                isinstance(item, dict)
                and item.get("selected") is True
                and item.get("song_id") == selection.representative_song_id
                and isinstance(item.get("seed_title"), str)
            ):
                if (
                    item.get("relation") == "style"
                    and item.get("style") and item.get("supporting_seed_title")
                ):
                    reason = (
                        f"「为你新发现」你最近常听《{item['seed_title']}》和《{item['supporting_seed_title']}》，"
                        f"这首也收录在 Apple Music 国区的{item['style']}编辑精选中。"
                    )
                    return {str(placement.track_id): reason}
                if item["seed_title"] not in titles:
                    titles.append(item["seed_title"])

    if titles:
        seeds = "和".join(f"《{title}》" for title in titles[:2])
        reason = f"「为你新发现」从你最近常听的{seeds}出发，在国区目录找到这张专辑。"
    else:
        reason = "「为你新发现」这张专辑已核对国区发行和本地音轨，可以接着听整张。"
    return {str(placement.track_id): reason}


def _matched_track_ids(data: object) -> Dict[str, UUID]:
    matches = data.get("tracks") if isinstance(data, dict) else None
    if not isinstance(matches, list):
        return {}
    ids = {}
    for match in matches:
        if not isinstance(match, dict) or match.get("status") != "verified":
            continue
        song_id, track_id = match.get("song_id"), match.get("track_id")
        if not isinstance(song_id, str) or not isinstance(track_id, str):
            continue
        if song_id in ids:
            return {}
        try:
            ids[song_id] = UUID(track_id)
        except ValueError:
            continue
    return ids


def _matches_current_library(selection: AlbumDiscoverySelection, representative: Track) -> bool:
    release = selection.release
    data = _read_json(release.expected_tracks)
    expected = data.get("tracks") if isinstance(data, dict) else None
    if not isinstance(expected, list) or not expected:
        return False
    expected_ids = {
        item["song_id"] for item in expected
        if isinstance(item, dict) and isinstance(item.get("song_id"), str)
    }
    evidence = _read_json(release.reconciliation)
    matched = _matched_track_ids(evidence)
    if (
        len(expected_ids) != len(expected)
        or matched.keys() != expected_ids
        or len(evidence["tracks"]) != len(expected)
        or matched.get(selection.representative_song_id) != representative.id
        or len(set(matched.values())) != len(expected)
    ):
        return False
    indexed = dict(
        Track.select(Track.id, Track.path).where(
            (Track.album == representative.album_id) & Track.id.in_(tuple(matched.values()))
        ).tuples()
    )
    if len(indexed) != len(expected):
        return False
    return all(
        verified_file_is_current(indexed[matched[item["song_id"]]], item)
        for item in evidence["tracks"]
    )


def _observe_listening(
    user: User, placement: AlbumDiscoveryPlacement, observed_at: datetime,
) -> Dict[str, object]:
    activity = User_Play_Activity
    first_window_end = min(observed_at, placement.created_at + OBSERVATION_WINDOW)
    representative_plays = activity.select(activity.time).where(
        (activity.user == user) & (activity.track == placement.track_id)
    )
    first = representative_plays.where(
        (activity.time >= placement.created_at) & (activity.time <= first_window_end)
    ).order_by(activity.time).first()

    first_time = first.time if first is not None else None
    repeat = False
    other_ids = []
    if first_time is not None:
        last_time = min(observed_at, first_time + OBSERVATION_WINDOW)
        repeat = representative_plays.where(
            (activity.time > first_time) & (activity.time <= last_time)
        ).exists()
        eligible_ids = set(_matched_track_ids(_read_json(placement.selection.release.reconciliation)).values())
        eligible_ids.discard(placement.track_id)
        if eligible_ids:
            other_ids = sorted(
                str(row.track_id) for row in activity.select(activity.track).where(
                    (activity.user == user)
                    & activity.track.in_(eligible_ids)
                    & (activity.time > first_time)
                    & (activity.time <= last_time)
                ).distinct()
            )

    retained_favorite = StarredTrack.select().where(
        (StarredTrack.user == user)
        & (StarredTrack.starred == placement.track_id)
        & (StarredTrack.date >= placement.created_at)
        & (StarredTrack.date <= first_window_end)
    ).exists()
    return {
        "albumId": placement.selection.release.album_id,
        "playlistId": str(placement.playlist_id),
        "trackId": str(placement.track_id),
        "day": placement.day.isoformat(),
        "position": placement.position,
        "placedAt": placement.created_at.isoformat(),
        "firstObservedAt": first_time.isoformat() if first_time is not None else None,
        "representativeReplayObserved": repeat,
        "otherAlbumTracksObserved": other_ids,
        "retainedNewFavorite": retained_favorite,
        "source": "unknown",
    }


def album_discovery_metrics(
    user: User, library_key: str, *, days: int = 21, now: Optional[datetime] = None,
) -> Dict[str, object]:
    """Report persisted discovery results and bounded observational play metrics."""
    if type(days) is not int or not 1 <= days <= 90:
        raise ValueError("days must be an integer between 1 and 90")
    observed_at = database_now() if now is None else now
    from_time = observed_at - timedelta(days=days)
    local_day = datetime.now(SHANGHAI).date() if now is None else now.date()
    release = AlbumDiscoveryRelease
    selection = AlbumDiscoverySelection
    submitted = AlbumDiscoveryOrder
    placement = AlbumDiscoveryPlacement

    ready = (
        release.select().join(selection)
        .where(
            (release.library_key == library_key)
            & (selection.user == user)
            & (release.ready_at >= from_time)
            & (release.ready_at <= observed_at)
        )
        .count()
    )
    submissions = (
        submitted.select().join(
            release,
            on=(submitted.library_key == release.library_key)
            & (submitted.album_id == release.album_id),
        ).join(selection)
        .where(
            (submitted.library_key == library_key)
            & (submitted.storefront == STOREFRONT)
            & (submitted.status == SUBMITTED)
            & (submitted.quota_date >= local_day - timedelta(days=days - 1))
            & (submitted.quota_date <= local_day)
            & (selection.user == user)
        )
        .count()
    )
    placements = (
        placement.select(placement, selection, release).join(selection).join(release)
        .where(
            (placement.user == user)
            & (selection.user == user)
            & (release.library_key == library_key)
            & (placement.created_at >= from_time)
            & (placement.created_at <= observed_at)
        )
        .order_by(placement.created_at.desc(), placement.id)
        .limit(days + 1)
    )
    return {
        "days": days,
        "submittedAlbums": submissions,
        "readyAlbums": ready,
        "placements": [_observe_listening(user, item, observed_at) for item in placements],
    }
