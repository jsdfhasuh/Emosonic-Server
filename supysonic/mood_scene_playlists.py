"""Rule-based mood and scene playlist generation."""

from __future__ import annotations

import hashlib
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .db import Track, TrackMetadata
from .recommendation_feedback import (
    get_recommendation_feedback_preferences,
    track_matches_negative_recommendation_feedback,
)
from .track_metadata_quality import is_high_quality_track_metadata


DEFAULT_MOOD_SCENE_PLAYLIST_LIMIT = 10
FALLBACK_SCORE_CEILING = 0.12

SCENE_PLAYLISTS = {
    "night": {
        "label": "夜晚",
        "aliases": ("夜晚", "night", "late_night"),
        "scene_terms": (
            "深夜",
            "夜晚聆听",
            "安静时刻",
            "late night",
            "night",
        ),
        "mood_terms": ("平静", "感伤", "沉思", "calm", "sad", "reflective"),
        "tag_terms": ("梦幻", "氛围", "dreamy", "ambient"),
        "energy_range": (None, 45),
        "fallback_genres": ("ambient", "dream pop", "classical", "lofi"),
    },
    "study": {
        "label": "学习",
        "aliases": ("学习", "study", "focus"),
        "scene_terms": ("专注", "学习", "安静时刻", "focus", "study", "work"),
        "mood_terms": ("平静", "沉思", "calm", "reflective"),
        "tag_terms": ("器乐", "氛围", "ambient", "instrumental", "lofi"),
        "energy_range": (20, 60),
        "danceability_range": (None, 60),
        "fallback_genres": ("ambient", "classical", "instrumental", "lofi"),
    },
    "commute": {
        "label": "通勤",
        "aliases": ("通勤", "commute"),
        "scene_terms": ("通勤", "路上", "出行", "commute", "driving", "travel"),
        "mood_terms": ("明亮", "轻松", "bright", "easy"),
        "tag_terms": ("流行", "节奏", "pop", "road"),
        "energy_range": (35, 80),
        "fallback_genres": ("pop", "rock", "electronic"),
    },
    "relax": {
        "label": "放松",
        "aliases": ("放松", "relax", "chill"),
        "scene_terms": ("放松", "安静时刻", "休息", "relax", "chill"),
        "mood_terms": ("放松", "平静", "治愈", "calm", "healing"),
        "tag_terms": ("氛围", "轻柔", "ambient", "soft"),
        "energy_range": (None, 60),
        "fallback_genres": ("ambient", "dream pop", "folk", "classical"),
    },
    "high_energy": {
        "label": "高能量",
        "aliases": ("高能量", "high_energy", "energetic"),
        "scene_terms": ("运动", "派对", "workout", "party"),
        "mood_terms": ("兴奋", "燃", "明亮", "energetic", "bright"),
        "tag_terms": ("摇滚", "电子", "rock", "electronic", "dance"),
        "energy_range": (70, None),
        "fallback_genres": ("rock", "electronic", "dance", "pop"),
    },
    "low_energy": {
        "label": "低能量",
        "aliases": ("低能量", "low_energy"),
        "scene_terms": ("安静时刻", "睡前", "quiet", "sleep"),
        "mood_terms": ("平静", "放松", "calm", "relaxed"),
        "tag_terms": ("氛围", "轻柔", "ambient", "soft"),
        "energy_range": (None, 35),
        "fallback_genres": ("ambient", "classical", "folk"),
    },
    "cantonese": {
        "label": "粤语",
        "aliases": ("粤语", "cantonese", "yue"),
        "languages": ("yue",),
        "tag_terms": ("粤语", "粤语流行", "cantonese"),
        "fallback_genres": ("cantonese", "粤语", "cantopop"),
    },
    "nostalgic": {
        "label": "怀旧",
        "aliases": ("怀旧", "nostalgic", "classic"),
        "mood_terms": ("怀旧", "nostalgic"),
        "tag_terms": ("怀旧", "经典金曲", "经典", "oldies", "classic"),
        "fallback_genres": ("oldies", "classic", "经典", "怀旧"),
    },
    "emo": {
        "label": "emo",
        "aliases": ("emo", "忧郁", "感伤"),
        "mood_terms": ("感伤", "忧郁", "emo", "melancholic", "sad"),
        "tag_terms": ("emo", "情绪摇滚", "抒情", "ballad"),
        "energy_range": (None, 65),
        "fallback_genres": ("emo", "rock", "ballad"),
    },
}

SCENE_PLAYLIST_KEY_BY_ALIAS = {
    str(alias).casefold(): key
    for key, config in SCENE_PLAYLISTS.items()
    for alias in (key, *config.get("aliases", ()))
}


def list_mood_scene_playlist_keys() -> List[str]:
    return list(SCENE_PLAYLISTS.keys())


def get_mood_scene_playlist(
    scene_key: str,
    limit: int = DEFAULT_MOOD_SCENE_PLAYLIST_LIMIT,
    user: Optional[object] = None,
    *,
    tracks: Optional[Sequence[Track]] = None,
    metadata_by_track_id: Optional[Mapping[object, TrackMetadata]] = None,
    preferences: Optional[Mapping[str, object]] = None,
) -> List[Dict[str, object]]:
    if limit <= 0:
        return []

    key = _resolve_scene_key(scene_key)
    if key is None:
        return []
    config = SCENE_PLAYLISTS[key]
    preferences = (
        preferences
        if preferences is not None
        else get_recommendation_feedback_preferences(user)
    )

    candidate_tracks = list(tracks) if tracks is not None else list(Track.select())
    tracks = [
        track
        for track in candidate_tracks
        if not track_matches_negative_recommendation_feedback(track, preferences)
    ]
    if not tracks:
        return []

    if metadata_by_track_id is None:
        metadata_by_track_id = _load_metadata_by_track_id([track.id for track in tracks])
    selected_track_ids = set()
    scored_results = []
    for track in tracks:
        metadata = metadata_by_track_id.get(track.id)
        if not is_high_quality_track_metadata(metadata):
            continue
        score, reasons = _score_scene_metadata(config, metadata)
        if score <= 0:
            continue
        selected_track_ids.add(track.id)
        scored_results.append(_result(track, score, reasons))

    scored_results.sort(key=_playlist_result_sort_key)
    if len(scored_results) >= limit:
        return scored_results[:limit]

    fallback_results = _fallback_results(
        key,
        config,
        tracks,
        selected_track_ids,
        limit - len(scored_results),
    )
    return (scored_results + fallback_results)[:limit]


def _load_metadata_by_track_id(
    track_ids: Sequence[object],
) -> Dict[object, TrackMetadata]:
    if not track_ids:
        return {}
    return {
        metadata.track_id: metadata
        for metadata in TrackMetadata.select().where(TrackMetadata.track.in_(track_ids))
    }


def _score_scene_metadata(
    config: Mapping[str, object],
    metadata: TrackMetadata,
) -> tuple:
    score = 0.0
    reasons = []
    for reason_label, values, terms, weight in (
        ("mood", metadata.get_moods(), config.get("mood_terms", ()), 0.26),
        ("scene", metadata.get_scenes(), config.get("scene_terms", ()), 0.32),
        ("tags", metadata.get_tags(), config.get("tag_terms", ()), 0.22),
    ):
        matches = _matched_terms(values, terms)
        if not matches:
            continue
        score += weight * min(1.0, len(matches) / 2.0)
        reasons.append(f"{reason_label}: " + " / ".join(matches[:3]))

    language = _normalized_text(metadata.language)
    languages = {_normalized_text(value) for value in config.get("languages", ())}
    if language and language in languages:
        score += 0.34
        reasons.append(f"language: {metadata.language}")

    for field_name, reason_label in (
        ("energy", "energy"),
        ("valence", "valence"),
        ("danceability", "danceability"),
    ):
        field_range = config.get(f"{field_name}_range")
        if field_range is None:
            continue
        if _value_in_range(getattr(metadata, field_name, None), field_range):
            score += 0.18
            reasons.append(f"{reason_label}: {getattr(metadata, field_name)}")

    return score, reasons


def _fallback_results(
    scene_key: str,
    config: Mapping[str, object],
    tracks: Sequence[Track],
    selected_track_ids: set,
    limit: int,
) -> List[Dict[str, object]]:
    if limit <= 0:
        return []

    max_play_count = max((int(track.play_count or 0) for track in tracks), default=0)
    results = []
    for track in tracks:
        if track.id in selected_track_ids:
            continue
        score, reasons = _fallback_score(scene_key, config, track, max_play_count)
        if score <= 0:
            continue
        results.append(_result(track, score, reasons))

    results.sort(key=_playlist_result_sort_key)
    return results[:limit]


def _fallback_score(
    scene_key: str,
    config: Mapping[str, object],
    track: Track,
    max_play_count: int,
) -> tuple:
    score = 0.0
    reasons = []
    genre = str(track.genre or "").strip()
    if _matches_any(genre, config.get("fallback_genres", ())):
        score += 0.08
        reasons.append(f"fallback genre: {genre}")

    if max_play_count > 0 and int(track.play_count or 0) > 0:
        score += min(0.035, int(track.play_count or 0) / max_play_count * 0.035)
        reasons.append("popular fallback")

    if score <= 0:
        return 0.0, []

    score += _stable_jitter(scene_key, track.id)
    return min(FALLBACK_SCORE_CEILING, score), reasons


def _result(track: Track, score: float, reasons: Iterable[str]) -> Dict[str, object]:
    return {
        "track": track,
        "score": round(float(score), 4),
        "reasons": [reason for reason in reasons if reason],
    }


def _playlist_result_sort_key(result: Mapping[str, object]) -> tuple:
    track = result["track"]
    return (
        -float(result["score"]),
        -int(getattr(track, "play_count", 0) or 0),
        str(getattr(track, "title", "") or "").casefold(),
        str(getattr(track, "id", "")),
    )


def _resolve_scene_key(scene_key: str) -> Optional[str]:
    return SCENE_PLAYLIST_KEY_BY_ALIAS.get(_normalized_text(scene_key))


def _matched_terms(values: Iterable[object], terms: Iterable[object]) -> List[str]:
    matches = []
    for value in values:
        text = str(value or "").strip()
        if text and _matches_any(text, terms):
            matches.append(text)
    return matches


def _matches_any(value: object, terms: Iterable[object]) -> bool:
    normalized_value = _normalized_text(value)
    if not normalized_value:
        return False
    for term in terms:
        normalized_term = _normalized_text(term)
        if normalized_term and normalized_term in normalized_value:
            return True
    return False


def _value_in_range(value: object, value_range: Tuple[object, object]) -> bool:
    if value is None:
        return False
    low, high = value_range
    numeric_value = float(value)
    if low is not None and numeric_value < float(low):
        return False
    if high is not None and numeric_value > float(high):
        return False
    return True


def _stable_jitter(scene_key: str, track_id: object) -> float:
    digest = hashlib.sha1(f"{scene_key}:{track_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF * 0.0001


def _normalized_text(value: object) -> str:
    return str(value or "").strip().casefold()
