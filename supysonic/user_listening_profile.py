"""User listening profile aggregation from track metadata."""

from __future__ import annotations

from datetime import timedelta
from typing import Dict, Iterable, Mapping, Optional

from peewee import OperationalError

from .db import TrackMetadata, User_Play_Activity, now
from .track_metadata_quality import is_high_quality_track_metadata


TOP_LISTENING_PROFILE_ITEM_LIMIT = 5
RECENT_PROFILE_WINDOWS = (7, 30)


def build_user_listening_profile(
    user: Optional[object],
    reference_time=None,
) -> Dict[str, object]:
    if user is None:
        return _empty_profile()

    reference_time = reference_time or now()
    track_play_counts: Dict[object, int] = {}
    recent_counts_by_window = {
        days: {}
        for days in RECENT_PROFILE_WINDOWS
    }

    try:
        activities = User_Play_Activity.select().where(User_Play_Activity.user == user)
        for activity in activities:
            track_play_counts[activity.track_id] = (
                track_play_counts.get(activity.track_id, 0) + 1
            )
            activity_time = getattr(activity, "time", None)
            if activity_time is None:
                continue
            for days, counts in recent_counts_by_window.items():
                if activity_time >= reference_time - timedelta(days=days):
                    counts[activity.track_id] = counts.get(activity.track_id, 0) + 1
    except OperationalError:
        track_play_counts = {}
        recent_counts_by_window = {days: {} for days in RECENT_PROFILE_WINDOWS}

    if not track_play_counts and getattr(user, "last_play_id", None):
        track_play_counts[user.last_play_id] = 1
        for counts in recent_counts_by_window.values():
            counts[user.last_play_id] = 1

    return build_track_count_listening_profile(
        track_play_counts,
        recent_counts_by_window=recent_counts_by_window,
    )


def build_track_count_listening_profile(
    track_play_counts: Mapping[object, int],
    recent_counts_by_window: Optional[Mapping[int, Mapping[object, int]]] = None,
) -> Dict[str, object]:
    profile = _finalize_aggregate(_aggregate_track_metadata(track_play_counts))
    recent_counts_by_window = recent_counts_by_window or {}
    for days in RECENT_PROFILE_WINDOWS:
        profile[f"recent{days}Days"] = _finalize_aggregate(
            _aggregate_track_metadata(recent_counts_by_window.get(days, {}))
        )
    return profile


def build_metadata_preference_profile_from_track_counts(
    track_play_counts: Mapping[object, int],
) -> Dict[str, object]:
    profile = build_track_count_listening_profile(track_play_counts)
    return {
        "mood_counts": dict(profile["moodCounts"]),
        "scene_counts": dict(profile["sceneCounts"]),
        "tag_counts": dict(profile["tagCounts"]),
        "average_energy": profile["averageEnergy"],
    }


def _aggregate_track_metadata(track_play_counts: Mapping[object, int]) -> Dict[str, object]:
    aggregate = _empty_aggregate()
    weighted_energy = 0.0
    weighted_valence = 0.0
    weighted_danceability = 0.0
    energy_weight = 0
    valence_weight = 0
    danceability_weight = 0

    clean_counts = {
        track_id: int(count or 0)
        for track_id, count in (track_play_counts or {}).items()
        if track_id and int(count or 0) > 0
    }
    if not clean_counts:
        return aggregate

    for metadata in TrackMetadata.select().where(
        TrackMetadata.track.in_(list(clean_counts.keys()))
    ):
        if not is_high_quality_track_metadata(metadata):
            continue
        play_count = clean_counts.get(metadata.track_id, 0)
        if play_count <= 0:
            continue

        aggregate["trackCount"] += 1
        aggregate["playCount"] += play_count
        _increment_counts(aggregate["moodCounts"], metadata.get_moods(), play_count)
        _increment_counts(aggregate["sceneCounts"], metadata.get_scenes(), play_count)
        _increment_counts(aggregate["tagCounts"], metadata.get_tags(), play_count)
        _increment_counts(
            aggregate["languageCounts"],
            [metadata.language] if metadata.language else [],
            play_count,
        )

        if metadata.energy is not None:
            weighted_energy += float(metadata.energy) * play_count
            energy_weight += play_count
        if metadata.valence is not None:
            weighted_valence += float(metadata.valence) * play_count
            valence_weight += play_count
        if metadata.danceability is not None:
            weighted_danceability += float(metadata.danceability) * play_count
            danceability_weight += play_count

    aggregate["averageEnergy"] = _weighted_average(weighted_energy, energy_weight)
    aggregate["averageValence"] = _weighted_average(weighted_valence, valence_weight)
    aggregate["averageDanceability"] = _weighted_average(
        weighted_danceability,
        danceability_weight,
    )
    return aggregate


def _empty_profile() -> Dict[str, object]:
    profile = _finalize_aggregate(_empty_aggregate())
    for days in RECENT_PROFILE_WINDOWS:
        profile[f"recent{days}Days"] = _finalize_aggregate(_empty_aggregate())
    return profile


def _empty_aggregate() -> Dict[str, object]:
    return {
        "trackCount": 0,
        "playCount": 0,
        "moodCounts": {},
        "sceneCounts": {},
        "tagCounts": {},
        "languageCounts": {},
        "averageEnergy": None,
        "averageValence": None,
        "averageDanceability": None,
    }


def _finalize_aggregate(aggregate: Mapping[str, object]) -> Dict[str, object]:
    mood_counts = dict(aggregate.get("moodCounts") or {})
    scene_counts = dict(aggregate.get("sceneCounts") or {})
    tag_counts = dict(aggregate.get("tagCounts") or {})
    language_counts = dict(aggregate.get("languageCounts") or {})
    return {
        "trackCount": int(aggregate.get("trackCount", 0) or 0),
        "playCount": int(aggregate.get("playCount", 0) or 0),
        "topMoods": _top_count_items(mood_counts),
        "topScenes": _top_count_items(scene_counts),
        "topTags": _top_count_items(tag_counts),
        "topLanguages": _top_count_items(language_counts),
        "moodCounts": mood_counts,
        "sceneCounts": scene_counts,
        "tagCounts": tag_counts,
        "languageCounts": language_counts,
        "averageEnergy": aggregate.get("averageEnergy"),
        "averageValence": aggregate.get("averageValence"),
        "averageDanceability": aggregate.get("averageDanceability"),
    }


def _increment_counts(
    counts: Dict[str, int],
    values: Iterable[object],
    amount: int,
) -> None:
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if not key:
            continue
        counts[key] = counts.get(key, 0) + amount


def _top_count_items(counts: Mapping[str, int]) -> list:
    return [
        {"value": value, "playCount": int(count)}
        for value, count in sorted(
            counts.items(),
            key=lambda item: (-int(item[1]), item[0]),
        )[:TOP_LISTENING_PROFILE_ITEM_LIMIT]
    ]


def _weighted_average(total: float, weight: int):
    if weight <= 0:
        return None
    return round(total / weight, 2)
