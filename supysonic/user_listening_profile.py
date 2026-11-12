"""User listening profile aggregation from track metadata."""

from __future__ import annotations

import math

from datetime import datetime, timedelta
from typing import Dict, Iterable, Mapping, Optional

from peewee import OperationalError, fn

from .db import Track, TrackMetadata, User_Play_Activity, now
from .track_metadata_quality import (
    MIN_HIGH_QUALITY_TRACK_METADATA_CONFIDENCE,
    is_high_quality_track_metadata,
    is_llm_track_metadata,
)


TOP_LISTENING_PROFILE_ITEM_LIMIT = 5
RECENT_PROFILE_WINDOWS = (7, 30)
RECOMMENDATION_RECENT_DAYS = 7
RECOMMENDATION_RECENT_HALF_LIFE_DAYS = 3
RECOMMENDATION_RECENT_WEIGHT = 0.7
RECOMMENDATION_LANGUAGE_ALIASES = {
    "zh": "zh", "cmn": "zh", "zho": "zh", "chi": "zh",
    "mandarin": "zh", "chinese": "zh", "zh-cn": "zh", "zh-tw": "zh",
    "zh-hans": "zh", "zh-hant": "zh", "cmn-hans": "zh", "cmn-hant": "zh",
    "\u56fd\u8bed": "zh", "\u570b\u8a9e": "zh", "\u666e\u901a\u8bdd": "zh",
    "\u4e2d\u6587": "zh", "\u6c49\u8bed": "zh", "\u6f22\u8a9e": "zh",
    "yue": "yue", "cantonese": "yue", "zh-yue": "yue",
    "yue-hans": "yue", "yue-hant": "yue",
    "\u7ca4\u8bed": "yue", "\u7cb5\u8a9e": "yue", "\u5e7f\u4e1c\u8bdd": "yue",
    "ko": "ko", "kor": "ko", "ko-kr": "ko", "korean": "ko",
    "\u97e9\u8bed": "ko", "\u97d3\u8a9e": "ko",
    "ja": "ja", "jpn": "ja", "ja-jp": "ja", "japanese": "ja",
    "\u65e5\u8bed": "ja", "\u65e5\u8a9e": "ja",
    "en": "en", "eng": "en", "en-us": "en", "en-gb": "en",
    "english": "en", "\u82f1\u8bed": "en", "\u82f1\u8a9e": "en",
    "es": "es", "spa": "es", "spanish": "es",
    "pt": "pt", "por": "pt", "portuguese": "pt",
    "fr": "fr", "fra": "fr", "fre": "fr", "french": "fr",
    "de": "de", "deu": "de", "ger": "de", "german": "de",
    "it": "it", "ita": "it", "italian": "it",
    "ru": "ru", "rus": "ru", "russian": "ru",
    "th": "th", "tha": "th", "thai": "th",
}
RECOMMENDATION_GENRE_LANGUAGES = {
    "mandopop": "zh", "mandarin pop": "zh",
    "\u56fd\u8bed\u6d41\u884c": "zh", "\u570b\u8a9e\u6d41\u884c": "zh",
    "cantopop": "yue", "cantonese pop": "yue",
    "\u7ca4\u8bed\u6d41\u884c": "yue", "\u7cb5\u8a9e\u6d41\u884c": "yue",
}


def build_recommendation_play_weights(
    user: Optional[object],
    reference_time: Optional[datetime] = None,
) -> Dict[object, float]:
    """Blend lifetime affinity with a normalized, decaying recent history."""
    if user is None:
        return {}
    reference_time = reference_time or now()
    lifetime = {
        row.track_id: row.play_count
        for row in User_Play_Activity.select(
            User_Play_Activity.track_id,
            fn.COUNT(User_Play_Activity.id).alias("play_count"),
        ).where(User_Play_Activity.user == user).group_by(User_Play_Activity.track_id)
    }
    if not lifetime:
        return {}

    recent: Dict[object, float] = {}
    activities = User_Play_Activity.select(
        User_Play_Activity.track_id,
        User_Play_Activity.time,
    ).where(
        (User_Play_Activity.user == user)
        & (User_Play_Activity.time >= reference_time - timedelta(days=RECOMMENDATION_RECENT_DAYS))
        & (User_Play_Activity.time <= reference_time)
    )
    for activity in activities:
        # Keep both queries on the same track set if a new play arrives between them.
        if activity.track_id not in lifetime:
            continue
        age_days = (reference_time - activity.time).total_seconds() / 86400.0
        weight = 2.0 ** (-age_days / RECOMMENDATION_RECENT_HALF_LIFE_DAYS)
        recent[activity.track_id] = recent.get(activity.track_id, 0.0) + weight

    lifetime_total = sum(lifetime.values())
    recent_total = sum(recent.values())
    if recent_total <= 0:
        return {track_id: count / lifetime_total for track_id, count in lifetime.items()}
    return {
        track_id: (
            (1.0 - RECOMMENDATION_RECENT_WEIGHT) * count / lifetime_total
            + RECOMMENDATION_RECENT_WEIGHT * recent.get(track_id, 0.0) / recent_total
        )
        for track_id, count in lifetime.items()
    }


def get_recommendation_language(track: object, metadata: Optional[object]) -> Optional[str]:
    """Use reliable language metadata or an explicitly language-specific genre."""
    if is_llm_track_metadata(metadata):
        try:
            confidence = float(getattr(metadata, "confidence", None))
        except (TypeError, ValueError):
            confidence = 0.0
        if math.isfinite(confidence) and confidence >= MIN_HIGH_QUALITY_TRACK_METADATA_CONFIDENCE:
            label = str(getattr(metadata, "language", None) or "").strip().casefold()
            language = RECOMMENDATION_LANGUAGE_ALIASES.get(label.replace("_", "-"))
            if language:
                return language
    genre = str(getattr(track, "genre", None) or "").strip().casefold()
    return RECOMMENDATION_GENRE_LANGUAGES.get(genre)


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
    track_play_counts: Mapping[object, float],
) -> Dict[str, object]:
    metadata_by_track = {
        metadata.track_id: metadata
        for metadata in TrackMetadata.select().where(
            TrackMetadata.track.in_(list(track_play_counts))
        )
    } if track_play_counts else {}
    profile = _aggregate_track_metadata(
        track_play_counts,
        fractional=True,
        metadata_by_track=metadata_by_track,
    )
    language_counts: Dict[str, float] = {}
    tracks = Track.select(Track.id, Track.genre).where(Track.id.in_(list(track_play_counts)))
    for track in tracks if track_play_counts else ():
        weight = _positive_weight(track_play_counts.get(track.id, 0))
        language = get_recommendation_language(track, metadata_by_track.get(track.id))
        if language and weight > 0:
            language_counts[language] = language_counts.get(language, 0.0) + weight
    return {
        "mood_counts": dict(profile["moodCounts"]),
        "scene_counts": dict(profile["sceneCounts"]),
        "tag_counts": dict(profile["tagCounts"]),
        "average_energy": profile["averageEnergy"],
        "language_counts": language_counts,
    }


def _positive_weight(value: object) -> float:
    try:
        weight = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return weight if math.isfinite(weight) and weight > 0 else 0.0


def _aggregate_track_metadata(
    track_play_counts: Mapping[object, float],
    *,
    fractional: bool = False,
    metadata_by_track: Optional[Mapping[object, TrackMetadata]] = None,
) -> Dict[str, object]:
    aggregate = _empty_aggregate()
    weighted_energy = 0.0
    weighted_valence = 0.0
    weighted_danceability = 0.0
    energy_weight = 0
    valence_weight = 0
    danceability_weight = 0

    clean_counts = {}
    for track_id, count in (track_play_counts or {}).items():
        weight = _positive_weight(count)
        if track_id and weight > 0:
            clean_counts[track_id] = weight if fractional else int(weight)
    if not clean_counts:
        return aggregate

    metadata_rows = (
        metadata_by_track.values()
        if metadata_by_track is not None
        else TrackMetadata.select().where(TrackMetadata.track.in_(list(clean_counts)))
    )
    for metadata in metadata_rows:
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
    counts: Dict[str, float],
    values: Iterable[object],
    amount: float,
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


def _weighted_average(total: float, weight: float) -> Optional[float]:
    if weight <= 0:
        return None
    return round(total / weight, 2)
