"""Rule-based similar track scoring."""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from .db import Track, TrackMetadata
from .recommendation_feedback import (
    get_recommendation_feedback_preferences,
    track_matches_negative_recommendation_feedback,
)
from .track_metadata_quality import is_high_quality_track_metadata


DEFAULT_SIMILAR_TRACK_LIMIT = 10
SIMILAR_TRACK_SCORE_WEIGHTS = {
    "mood": 0.28,
    "scene": 0.25,
    "tags": 0.16,
    "language": 0.08,
    "energy": 0.12,
    "valence": 0.08,
    "danceability": 0.05,
    "genre": 0.12,
    "different_artist": 0.02,
}


def get_similar_tracks(
    track_id: object,
    limit: int = DEFAULT_SIMILAR_TRACK_LIMIT,
    user: Optional[object] = None,
    *,
    target: Optional[Track] = None,
    candidates: Optional[Sequence[Track]] = None,
    metadata_by_track_id: Optional[Mapping[object, TrackMetadata]] = None,
    preferences: Optional[Mapping[str, object]] = None,
) -> List[Dict[str, object]]:
    if limit <= 0:
        return []

    if target is not None and str(target.id) != str(track_id):
        return []

    target = target or Track.get_or_none(Track.id == track_id)
    if target is None:
        return []

    candidates = (
        list(candidates)
        if candidates is not None
        else list(Track.select().where(Track.id != target.id))
    )
    candidates = [candidate for candidate in candidates if candidate.id != target.id]
    if not candidates:
        return []

    preferences = (
        preferences
        if preferences is not None
        else get_recommendation_feedback_preferences(user)
    )
    if metadata_by_track_id is None:
        metadata_by_track_id = _load_metadata_by_track_id(
            [target.id] + [track.id for track in candidates]
        )
    target_metadata = metadata_by_track_id.get(target.id)

    scored_results = []
    for candidate in candidates:
        if track_matches_negative_recommendation_feedback(candidate, preferences):
            continue
        score, reasons = _score_candidate(
            target,
            target_metadata,
            candidate,
            metadata_by_track_id.get(candidate.id),
        )
        if score <= 0:
            continue
        scored_results.append(
            {
                "track": candidate,
                "score": round(score, 4),
                "reasons": reasons,
            }
        )

    scored_results.sort(key=_similar_result_sort_key)
    return _select_artist_diverse_results(scored_results, limit)


def _load_metadata_by_track_id(
    track_ids: Sequence[object],
) -> Dict[object, TrackMetadata]:
    if not track_ids:
        return {}
    return {
        metadata.track_id: metadata
        for metadata in TrackMetadata.select().where(TrackMetadata.track.in_(track_ids))
    }


def _score_candidate(
    target: Track,
    target_metadata: Optional[TrackMetadata],
    candidate: Track,
    candidate_metadata: Optional[TrackMetadata],
) -> tuple:
    score = 0.0
    reasons = []
    if (
        is_high_quality_track_metadata(target_metadata)
        and is_high_quality_track_metadata(candidate_metadata)
    ):
        semantic_score, semantic_reasons = _semantic_metadata_score(
            target_metadata,
            candidate_metadata,
        )
        score += semantic_score
        reasons.extend(semantic_reasons)

    genre_score, genre_reason = _genre_score(target, candidate)
    if genre_score:
        score += genre_score
        reasons.append(genre_reason)

    if score > 0 and target.artist_id and candidate.artist_id != target.artist_id:
        score += SIMILAR_TRACK_SCORE_WEIGHTS["different_artist"]
        reasons.append("artist variety")

    return score, reasons


def _semantic_metadata_score(
    target_metadata: TrackMetadata,
    candidate_metadata: TrackMetadata,
) -> tuple:
    score = 0.0
    reasons = []
    for label, getter_name, weight in (
        ("mood", "get_moods", SIMILAR_TRACK_SCORE_WEIGHTS["mood"]),
        ("scene", "get_scenes", SIMILAR_TRACK_SCORE_WEIGHTS["scene"]),
        ("tags", "get_tags", SIMILAR_TRACK_SCORE_WEIGHTS["tags"]),
    ):
        overlap = _list_overlap(
            getattr(target_metadata, getter_name)(),
            getattr(candidate_metadata, getter_name)(),
        )
        if not overlap:
            continue
        score += weight * min(1.0, len(overlap) / 2.0)
        reasons.append(f"{label}: " + " / ".join(overlap[:3]))

    target_language = _normalized_text(target_metadata.language)
    candidate_language = _normalized_text(candidate_metadata.language)
    if target_language and target_language == candidate_language:
        score += SIMILAR_TRACK_SCORE_WEIGHTS["language"]
        reasons.append(f"language: {candidate_metadata.language}")

    for field_name, label in (
        ("energy", "energy close"),
        ("valence", "valence close"),
        ("danceability", "danceability close"),
    ):
        field_score, reason = _numeric_distance_score(
            getattr(target_metadata, field_name, None),
            getattr(candidate_metadata, field_name, None),
            SIMILAR_TRACK_SCORE_WEIGHTS[field_name],
            label,
        )
        if field_score:
            score += field_score
            reasons.append(reason)

    return score, reasons


def _list_overlap(
    left_values: Iterable[object],
    right_values: Iterable[object],
) -> List[str]:
    left_by_key = {
        _normalized_text(value): str(value).strip()
        for value in left_values
        if _normalized_text(value)
    }
    right_keys = {
        _normalized_text(value)
        for value in right_values
        if _normalized_text(value)
    }
    return [
        original_value
        for key, original_value in left_by_key.items()
        if key in right_keys
    ]


def _numeric_distance_score(
    target_value: object,
    candidate_value: object,
    weight: float,
    reason_label: str,
) -> tuple:
    if target_value is None or candidate_value is None:
        return 0.0, ""
    distance = abs(float(candidate_value) - float(target_value))
    closeness = max(0.0, 1.0 - min(1.0, distance / 100.0))
    if closeness <= 0:
        return 0.0, ""
    return (
        weight * closeness,
        f"{reason_label}: {int(candidate_value)} vs {int(target_value)}",
    )


def _genre_score(target: Track, candidate: Track) -> tuple:
    target_genre = _normalized_text(target.genre)
    candidate_genre = _normalized_text(candidate.genre)
    if not target_genre or target_genre != candidate_genre:
        return 0.0, ""
    return SIMILAR_TRACK_SCORE_WEIGHTS["genre"], f"genre: {candidate.genre}"


def _similar_result_sort_key(result: Mapping[str, object]) -> tuple:
    track = result["track"]
    return (
        -float(result["score"]),
        -int(getattr(track, "play_count", 0) or 0),
        str(getattr(track, "title", "") or "").casefold(),
        str(getattr(track, "id", "")),
    )


def _select_artist_diverse_results(
    scored_results: Sequence[Dict[str, object]],
    limit: int,
) -> List[Dict[str, object]]:
    max_per_artist = max(1, limit // 2) if limit > 1 else 1
    selected = []
    deferred = []
    artist_counts: Dict[object, int] = {}

    for result in scored_results:
        track = result["track"]
        artist_id = getattr(track, "artist_id", None)
        if artist_counts.get(artist_id, 0) >= max_per_artist:
            deferred.append(result)
            continue
        selected.append(result)
        artist_counts[artist_id] = artist_counts.get(artist_id, 0) + 1
        if len(selected) >= limit:
            return selected

    for result in deferred:
        if len(selected) >= limit:
            break
        selected.append(result)
    return selected


def _normalized_text(value: object) -> str:
    return str(value or "").strip().casefold()
