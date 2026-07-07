"""Home page smart recommendation cards."""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set

from peewee import fn

from .db import Track, TrackMetadata, User_Play_Activity
from .mood_scene_playlists import get_mood_scene_playlist
from .recommend import (
    _getUserTrackPlayCounts,
    buildRecommendationReasonMap,
    getRecommendationDay,
    getRecommendationReason,
)
from .recommendation_feedback import (
    get_recommendation_feedback_preferences,
    track_matches_negative_recommendation_feedback,
)
from .similar_tracks import get_similar_tracks
from .track_metadata_quality import (
    MIN_HIGH_QUALITY_TRACK_METADATA_CONFIDENCE,
    is_high_quality_track_metadata,
)


DEFAULT_HOME_SMART_CARD_COUNT = 3
DEFAULT_HOME_SMART_CARD_TRACK_LIMIT = 6
MIN_HOME_SMART_CARD_TRACKS = 6
MAX_HOME_SMART_CARD_TRACKS = 10
HOME_SMART_CARD_METADATA_CANDIDATE_LIMIT = 500
HOME_SMART_CARD_FALLBACK_CANDIDATE_LIMIT = 200

HOME_SMART_CARD_DEFINITIONS = (
    {
        "key": "night",
        "title_en": "Tonight fits",
        "title_zh": "今晚适合听",
        "kind": "scene",
        "scene_key": "night",
    },
    {
        "key": "study",
        "title_en": "Study focus",
        "title_zh": "适合学习",
        "kind": "scene",
        "scene_key": "study",
    },
    {
        "key": "recent_similar",
        "title_en": "Near your recent vibe",
        "title_zh": "最近常听氛围的相似歌曲",
        "kind": "recent_similar",
    },
    {
        "key": "cantonese_nostalgic",
        "title_en": "Cantonese nostalgia",
        "title_zh": "粤语怀旧",
        "kind": "cantonese_nostalgic",
    },
    {
        "key": "high_energy",
        "title_en": "High energy reset",
        "title_zh": "高能量恢复一下",
        "kind": "scene",
        "scene_key": "high_energy",
    },
    {
        "key": "healing",
        "title_en": "Healing picks",
        "title_zh": "你可能喜欢的治愈歌曲",
        "kind": "scene",
        "scene_key": "relax",
    },
)


def build_home_smart_cards(
    user: Optional[object] = None,
    card_limit: int = DEFAULT_HOME_SMART_CARD_COUNT,
    track_limit: int = DEFAULT_HOME_SMART_CARD_TRACK_LIMIT,
) -> List[Dict[str, object]]:
    """Build user-visible smart cards for the home page."""

    if card_limit <= 0 or track_limit <= 0:
        return []

    normalized_track_limit = min(
        MAX_HOME_SMART_CARD_TRACKS,
        max(MIN_HOME_SMART_CARD_TRACKS, int(track_limit)),
    )
    context = _build_home_smart_card_context(user)
    used_track_ids: Set[str] = set()
    cards = []
    for definition in HOME_SMART_CARD_DEFINITIONS:
        if len(cards) >= card_limit:
            break

        results = _build_card_results(
            definition,
            user,
            normalized_track_limit,
            context,
            used_track_ids,
        )
        if len(results) < MIN_HOME_SMART_CARD_TRACKS:
            continue

        results = results[:normalized_track_limit]
        used_track_ids.update(str(item["track"].id) for item in results)
        cards.append(
            {
                "key": definition["key"],
                "title_en": definition["title_en"],
                "title_zh": definition["title_zh"],
                "tracks": results,
            }
        )

    return cards


def _build_card_results(
    definition: Mapping[str, object],
    user: Optional[object],
    track_limit: int,
    context: Mapping[str, object],
    excluded_track_ids: Set[str],
) -> List[Dict[str, object]]:
    kind = definition.get("kind")
    candidate_limit = track_limit + len(excluded_track_ids)
    if kind == "scene":
        results = _playlist_items(
            get_mood_scene_playlist(
                str(definition.get("scene_key") or ""),
                limit=candidate_limit,
                user=user,
                tracks=context["eligible_tracks"],
                metadata_by_track_id=context["metadata_by_track_id"],
                preferences=context["preferences"],
            )
        )
    elif kind == "recent_similar":
        results = _recent_similar_items(user, candidate_limit, context)
        if not results:
            return []
    elif kind == "cantonese_nostalgic":
        results = _cantonese_nostalgic_items(user, candidate_limit, context)
    else:
        results = []

    return _fill_with_recommendations(
        results,
        user,
        track_limit,
        context,
        excluded_track_ids,
    )


def _build_home_smart_card_context(user: Optional[object]) -> Dict[str, object]:
    preferences = get_recommendation_feedback_preferences(user)
    metadata_tracks, metadata_by_track_id = _load_high_quality_metadata_candidates(
        HOME_SMART_CARD_METADATA_CANDIDATE_LIMIT
    )
    fallback_tracks = _load_fallback_tracks(HOME_SMART_CARD_FALLBACK_CANDIDATE_LIMIT)
    candidate_tracks = _dedupe_tracks([*metadata_tracks, *fallback_tracks])
    eligible_tracks = [
        track
        for track in candidate_tracks
        if not track_matches_negative_recommendation_feedback(track, preferences)
    ]
    track_play_counts = _getUserTrackPlayCounts(user) if user is not None else {}
    return {
        "preferences": preferences,
        "eligible_tracks": eligible_tracks,
        "fallback_tracks": [
            track
            for track in fallback_tracks
            if not track_matches_negative_recommendation_feedback(track, preferences)
        ],
        "metadata_by_track_id": metadata_by_track_id,
        "recommendation_candidates": _rank_recommendation_candidates(
            eligible_tracks,
            track_play_counts,
        ),
        "track_play_counts": track_play_counts,
        "recommendation_day": getRecommendationDay(),
    }


def _load_high_quality_metadata_candidates(limit: int) -> tuple:
    query = (
        TrackMetadata.select(TrackMetadata, Track)
        .join(Track)
        .where(
            (
                (fn.LOWER(TrackMetadata.provider) == "llm")
                | (fn.LOWER(TrackMetadata.source) == "llm")
            ),
            TrackMetadata.confidence
            >= MIN_HIGH_QUALITY_TRACK_METADATA_CONFIDENCE,
        )
        .order_by(
            Track.play_count.desc(),
            Track.title.asc(),
            Track.id.asc(),
        )
        .limit(max(1, int(limit)))
    )
    tracks = []
    metadata_by_track_id = {}
    for metadata in query:
        if not is_high_quality_track_metadata(metadata):
            continue
        tracks.append(metadata.track)
        metadata_by_track_id[metadata.track_id] = metadata
    return tracks, metadata_by_track_id


def _load_fallback_tracks(limit: int) -> List[Track]:
    return list(
        Track.select()
        .order_by(
            Track.play_count.desc(),
            Track.title.asc(),
            Track.id.asc(),
        )
        .limit(max(1, int(limit)))
    )


def _dedupe_tracks(tracks: Sequence[Track]) -> List[Track]:
    seen_track_ids = set()
    deduped = []
    for track in tracks:
        if track.id in seen_track_ids:
            continue
        seen_track_ids.add(track.id)
        deduped.append(track)
    return deduped


def _rank_recommendation_candidates(
    tracks: Sequence[Track],
    track_play_counts: Mapping[object, int],
) -> List[Track]:
    listened_track_ids = {str(track_id) for track_id in track_play_counts}
    return sorted(
        tracks,
        key=lambda track: (
            str(track.id) in listened_track_ids,
            -int(track.play_count or 0),
            str(track.title or "").casefold(),
            str(track.id),
        ),
    )


def _playlist_items(results: Sequence[Mapping[str, object]]) -> List[Dict[str, object]]:
    items = []
    for result in results:
        track = result.get("track")
        if track is None:
            continue
        items.append(
            {
                "track": track,
                "reason": _reason_summary(result.get("reasons")),
                "source": "metadata",
            }
        )
    return _dedupe_items(items)


def _recent_similar_items(
    user: Optional[object],
    track_limit: int,
    context: Mapping[str, object],
) -> List[Dict[str, object]]:
    if user is None:
        return []

    items = []
    for seed in _recent_seed_tracks(user):
        for result in get_similar_tracks(
            seed.id,
            limit=track_limit * 2,
            user=user,
            target=seed,
            candidates=context["eligible_tracks"],
            metadata_by_track_id=context["metadata_by_track_id"],
            preferences=context["preferences"],
        ):
            track = result.get("track")
            if track is None:
                continue
            reason = _reason_summary(result.get("reasons"))
            if reason:
                reason = f"Similar to {seed.title}: {reason}"
            else:
                reason = f"Similar to {seed.title}."
            items.append(
                {
                    "track": track,
                    "reason": reason,
                    "source": "similar",
                }
            )
            if len(_dedupe_items(items)) >= track_limit:
                return _dedupe_items(items)[:track_limit]

    return _dedupe_items(items)[:track_limit]


def _recent_seed_tracks(user: object, limit: int = 5) -> List[Track]:
    seen_track_ids: Set[object] = set()
    tracks = []
    query = (
        User_Play_Activity.select()
        .where(User_Play_Activity.user == user)
        .order_by(User_Play_Activity.time.desc(), User_Play_Activity.id.desc())
        .limit(25)
    )
    for activity in query:
        track = activity.track
        if track.id in seen_track_ids:
            continue
        seen_track_ids.add(track.id)
        tracks.append(track)
        if len(tracks) >= limit:
            break
    return tracks


def _cantonese_nostalgic_items(
    user: Optional[object],
    track_limit: int,
    context: Mapping[str, object],
) -> List[Dict[str, object]]:
    tracks = list(context["eligible_tracks"])
    if not tracks:
        return []

    metadata_by_track_id = context["metadata_by_track_id"]
    scored = []
    for track in tracks:
        metadata = metadata_by_track_id.get(track.id)
        if not is_high_quality_track_metadata(metadata):
            continue
        score, reasons = _score_cantonese_nostalgic_metadata(metadata)
        if score <= 0:
            continue
        scored.append(
            (
                score,
                int(track.play_count or 0),
                str(track.title or "").casefold(),
                str(track.id),
                track,
                reasons,
            )
        )

    scored.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
    items = [
        {
            "track": track,
            "reason": _reason_summary(reasons),
            "source": "metadata",
        }
        for _, _, _, _, track, reasons in scored[:track_limit]
    ]
    if len(items) >= track_limit:
        return items

    items.extend(
        _playlist_items(
            get_mood_scene_playlist(
                "cantonese",
                limit=track_limit,
                user=user,
                tracks=tracks,
                metadata_by_track_id=metadata_by_track_id,
                preferences=context["preferences"],
            )
        )
    )
    items.extend(
        _playlist_items(
            get_mood_scene_playlist(
                "nostalgic",
                limit=track_limit,
                user=user,
                tracks=tracks,
                metadata_by_track_id=metadata_by_track_id,
                preferences=context["preferences"],
            )
        )
    )
    return _dedupe_items(items)[:track_limit]


def _score_cantonese_nostalgic_metadata(metadata: TrackMetadata) -> tuple:
    score = 0.0
    reasons = []
    language = _normalized_text(metadata.language)
    moods = metadata.get_moods()
    tags = metadata.get_tags()

    if language == "yue":
        score += 0.45
        reasons.append("language: yue")

    nostalgic_terms = ("怀旧", "nostalgic", "classic", "经典")
    nostalgic_matches = _matched_terms([*moods, *tags], nostalgic_terms)
    if nostalgic_matches:
        score += 0.35
        reasons.append("nostalgia: " + " / ".join(nostalgic_matches[:3]))

    cantonese_matches = _matched_terms(tags, ("粤语", "cantonese", "cantopop"))
    if cantonese_matches:
        score += 0.25
        reasons.append("tags: " + " / ".join(cantonese_matches[:3]))

    return score, reasons


def _fill_with_recommendations(
    items: Sequence[Mapping[str, object]],
    user: Optional[object],
    track_limit: int,
    context: Mapping[str, object],
    excluded_track_ids: Set[str],
) -> List[Dict[str, object]]:
    selected_items = _dedupe_items(items, excluded_track_ids)
    if len(selected_items) >= track_limit:
        return selected_items[:track_limit]

    excluded_track_ids = {
        *excluded_track_ids,
        *(str(item["track"].id) for item in selected_items),
    }
    selected_items.extend(
        _recommendation_items(
            user,
            track_limit - len(selected_items),
            excluded_track_ids,
            context,
        )
    )
    selected_items = _dedupe_items(selected_items)
    if len(selected_items) >= track_limit:
        return selected_items[:track_limit]

    excluded_track_ids = {
        *excluded_track_ids,
        *(str(item["track"].id) for item in selected_items),
    }
    selected_items.extend(
        _popular_fallback_items(
            user,
            track_limit - len(selected_items),
            excluded_track_ids,
            context,
        )
    )
    return _dedupe_items(selected_items)[:track_limit]


def _recommendation_items(
    user: Optional[object],
    limit: int,
    excluded_track_ids: Iterable[str],
    context: Mapping[str, object],
) -> List[Dict[str, object]]:
    if limit <= 0:
        return []

    tracks = _limited_recommendation_tracks(
        context["recommendation_candidates"],
        limit,
        excluded_track_ids,
    )
    return _items_with_recommendation_reasons(
        user,
        tracks,
        "recommendation",
        context["metadata_by_track_id"],
    )


def _popular_fallback_items(
    user: Optional[object],
    limit: int,
    excluded_track_ids: Iterable[str],
    context: Mapping[str, object],
) -> List[Dict[str, object]]:
    if limit <= 0:
        return []

    excluded = set(excluded_track_ids)
    tracks = []
    for track in context["fallback_tracks"]:
        if str(track.id) in excluded:
            continue
        tracks.append(track)
        if len(tracks) >= limit:
            break
    return _items_with_recommendation_reasons(
        user,
        tracks,
        "library",
        context["metadata_by_track_id"],
    )


def _items_with_recommendation_reasons(
    user: Optional[object],
    tracks: Sequence[Track],
    source: str,
    metadata_by_track_id: Optional[Mapping[object, TrackMetadata]] = None,
) -> List[Dict[str, object]]:
    if not tracks:
        return []

    if user is not None:
        reason_by_track_id = buildRecommendationReasonMap(user, tracks)
    else:
        reason_by_track_id = _anonymous_recommendation_reason_map(
            tracks,
            metadata_by_track_id,
        )

    return [
        {
            "track": track,
            "reason": reason_by_track_id.get(str(track.id), "")
            or "Because it adds variety to this smart card.",
            "source": source,
        }
        for track in tracks
    ]


def _anonymous_recommendation_reason_map(
    tracks: Sequence[Track],
    metadata_by_track_id: Optional[Mapping[object, TrackMetadata]] = None,
) -> Dict[str, str]:
    track_ids = [track.id for track in tracks]
    if metadata_by_track_id is None:
        metadata_by_track_id = {
            str(metadata.track_id): metadata
            for metadata in TrackMetadata.select().where(
                TrackMetadata.track.in_(track_ids)
            )
        } if track_ids else {}
    else:
        source_metadata_by_track_id = metadata_by_track_id
        metadata_by_track_id = {
            str(track_id): source_metadata_by_track_id.get(track_id)
            for track_id in track_ids
            if source_metadata_by_track_id.get(track_id) is not None
        }
    profile = {
        "listenedTrackIds": set(),
        "topGenres": set(),
        "topArtistIds": set(),
        "likedMoreGenres": set(),
        "likedMoreArtistIds": set(),
        "trackMetadataById": metadata_by_track_id,
    }
    return {
        str(track.id): getRecommendationReason(None, track, profile)
        for track in tracks
    }


def _limited_recommendation_tracks(
    tracks: Sequence[Track],
    limit: int,
    excluded_track_ids: Iterable[str],
) -> List[Track]:
    excluded = set(excluded_track_ids)
    selected = []
    for track in tracks:
        if str(track.id) in excluded:
            continue
        selected.append(track)
        if len(selected) >= limit:
            break
    return selected


def _dedupe_items(
    items: Sequence[Mapping[str, object]],
    excluded_track_ids: Optional[Iterable[str]] = None,
) -> List[Dict[str, object]]:
    excluded = set(excluded_track_ids or ())
    seen_track_ids = set()
    deduped = []
    for item in items:
        track = item.get("track")
        if track is None:
            continue
        track_id = str(track.id)
        if track_id in excluded or track.id in seen_track_ids:
            continue
        seen_track_ids.add(track.id)
        deduped.append(
            {
                "track": track,
                "reason": str(item.get("reason") or "").strip()
                or "Because it fits this smart card.",
                "source": str(item.get("source") or "metadata"),
            }
        )
    return deduped


def _reason_summary(reasons: object, limit: int = 3) -> str:
    if isinstance(reasons, str):
        return reasons.strip()
    if not reasons:
        return ""

    values = []
    for reason in reasons:
        text = str(reason or "").strip()
        if not text:
            continue
        values.append(text)
        if len(values) >= limit:
            break
    return "; ".join(values)


def _matched_terms(values: Iterable[object], terms: Iterable[object]) -> List[str]:
    matches = []
    normalized_terms = [_normalized_text(term) for term in terms]
    for value in values:
        text = str(value or "").strip()
        normalized_value = _normalized_text(text)
        if not text or not normalized_value:
            continue
        if any(term and term in normalized_value for term in normalized_terms):
            matches.append(text)
    return matches


def _normalized_text(value: object) -> str:
    return str(value or "").strip().casefold()
