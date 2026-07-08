import json
import logging
import re
import time
import unicodedata
import uuid
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import requests
from peewee import OperationalError

from .db import Artist, Track, TrackMetadata, User, User_Play_Activity, now
from .recommendation_agent_cache import (
    DEFAULT_AGENT_CACHE_TTL_SECONDS,
    build_recommendation_agent_context_hash,
    get_cached_recommendation_agent_payload,
    save_recommendation_agent_cache_payload,
)
from .recommendation_agent_session import (
    DEFAULT_AGENT_SESSION_LIMIT,
    latest_recommended_artists_from_sessions,
    list_recommendation_agent_sessions,
    save_recommendation_agent_session,
)
from .recommendation_feedback import get_recommendation_feedback_preferences
from .track_metadata_quality import is_high_quality_track_metadata
from .user_listening_profile import build_user_listening_profile


logger = logging.getLogger(__name__)

DEFAULT_AGENT_MESSAGE = {
    "en": "Recommend artists outside my local library from my full play context.",
    "zh": "根据我的完整播放上下文推荐曲库外歌手。",
}
DEFAULT_HISTORY_LIMIT = 200
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_OUTPUT_TOKENS = 900
DEFAULT_TEMPERATURE = 0.7
RECOMMENDED_ARTIST_LIMIT = 8
NEXT_ACTION_LIMIT = 4
AGENT_HIDDEN_ARTIST_LIMIT = 24
AGENT_SESSION_TRACK_LIMIT = 8
AGENT_CACHE_TRACK_LIMIT = 50
AGENT_CACHE_PLAY_HISTORY_LIMIT = 50
AGENT_METRIC_DEFAULTS = {
    "agent_request_count": 0,
    "agent_success_count": 0,
    "agent_error_count": 0,
    "agent_timeout_count": 0,
    "agent_cache_hit_count": 0,
    "agent_latency_ms": 0,
    "agent_average_latency_ms": 0,
    "agent_payload_size_bytes": 0,
    "agent_filtered_local_artist_count": 0,
    "agent_last_filtered_local_artist_count": 0,
    "agent_filtered_feedback_artist_count": 0,
    "agent_last_filtered_feedback_artist_count": 0,
    "agent_empty_result_count": 0,
}


def _default_agent_health_state() -> Dict[str, object]:
    metrics = dict(AGENT_METRIC_DEFAULTS)
    metrics["agent_total_latency_ms"] = 0
    return {
        "last_success_at": None,
        "last_error": None,
        "metrics": metrics,
    }


AGENT_HEALTH_STATE = _default_agent_health_state()
FOLLOW_UP_WORD_MARKERS = (
    "these",
    "those",
    "them",
    "previous",
    "earlier",
    "starter",
    "why",
)
FOLLOW_UP_PHRASE_MARKERS = (
    "different style",
    "another style",
    "change style",
    "change the style",
    "switch style",
    "switch styles",
    "more obscure",
    "obscure artist",
    "less mainstream",
    "deeper cut",
    "deeper cuts",
    "more underground",
    "more like this",
    "more like these",
    "last recommendation",
    "last recommendations",
    "last artist",
    "last artists",
    "last answer",
    "last response",
    "last session",
    "这些",
    "那些",
    "他们",
    "它们",
    "上次",
    "上一个",
    "上一轮",
    "上一批",
    "上轮推荐",
    "刚才",
    "刚刚",
    "之前",
    "为什么",
    "原因",
    "入门",
    "换一种风格",
    "换个风格",
    "换风格",
    "另一种风格",
    "别的风格",
    "不同风格",
    "不同的风格",
    "更冷门",
    "冷门一点",
    "再冷门",
    "更多类似",
    "多来点类似",
)
FOLLOW_UP_MARKERS = FOLLOW_UP_WORD_MARKERS + FOLLOW_UP_PHRASE_MARKERS


class RecommendationAgentError(Exception):
    status_code = 500
    error_code = "recommendation_agent_error"

    def __init__(
        self,
        message: str,
        details: Optional[Mapping[str, object]] = None,
    ) -> None:
        super().__init__(message)
        self.details = dict(details or {})

    def add_details(self, **details: object) -> None:
        for key, value in details.items():
            if value is not None and key not in self.details:
                self.details[key] = value


class RecommendationAgentConfigError(RecommendationAgentError):
    status_code = 503
    error_code = "recommendation_agent_not_configured"


class RecommendationAgentTimeoutError(RecommendationAgentError):
    status_code = 504
    error_code = "recommendation_agent_timeout"


class RecommendationAgentUpstreamError(RecommendationAgentError):
    status_code = 502
    error_code = "recommendation_agent_upstream_error"


class RecommendationAgentInvalidResponseError(RecommendationAgentError):
    status_code = 502
    error_code = "recommendation_agent_invalid_response"


def get_recommendation_agent_language(raw_language: str) -> str:
    return "zh" if raw_language == "zh" else "en"


def get_recommendation_agent_prompts(language: str) -> List[str]:
    if language == "zh":
        return [
            "根据我的播放记录推荐曲库外歌手",
            "为什么推荐这些歌手？",
            "给我一些可以入门的歌曲",
            "换一种风格",
            "推荐更冷门的",
        ]
    return [
        "Recommend artists outside my library",
        "Why these artists?",
        "Give me starter tracks",
        "Try a different style",
        "Recommend more obscure artists",
    ]


def get_default_agent_message(language: str) -> str:
    return DEFAULT_AGENT_MESSAGE.get(language, DEFAULT_AGENT_MESSAGE["en"])


def reset_recommendation_agent_health_state() -> None:
    AGENT_HEALTH_STATE.clear()
    AGENT_HEALTH_STATE.update(_default_agent_health_state())


def _agent_metrics() -> Dict[str, object]:
    metrics = AGENT_HEALTH_STATE.setdefault("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
        AGENT_HEALTH_STATE["metrics"] = metrics

    for key, value in AGENT_METRIC_DEFAULTS.items():
        metrics.setdefault(key, value)
    metrics.setdefault("agent_total_latency_ms", 0)
    return metrics


def _public_agent_metrics() -> Dict[str, object]:
    metrics = _agent_metrics()
    return {
        key: metrics.get(key, value)
        for key, value in AGENT_METRIC_DEFAULTS.items()
    }


def _elapsed_ms(started_at: float) -> int:
    return max(0, int(round((time.monotonic() - started_at) * 1000)))


def _increment_metric(metrics: Dict[str, object], key: str, amount: int = 1) -> None:
    metrics[key] = int(metrics.get(key, 0) or 0) + amount


def _record_recommendation_agent_request() -> None:
    metrics = _agent_metrics()
    _increment_metric(metrics, "agent_request_count")


def _update_completion_metrics(
    latency_ms: int,
    context_stats: Optional[Mapping[str, object]] = None,
    filtered_local_artist_count: int = 0,
    filtered_feedback_artist_count: int = 0,
    empty_result: bool = False,
    cache_hit: bool = False,
) -> Dict[str, object]:
    metrics = _agent_metrics()
    metrics["agent_latency_ms"] = latency_ms
    metrics["agent_total_latency_ms"] = (
        int(metrics.get("agent_total_latency_ms", 0) or 0) + latency_ms
    )
    completed_count = int(metrics.get("agent_success_count", 0) or 0) + int(
        metrics.get("agent_error_count", 0) or 0
    )
    metrics["agent_average_latency_ms"] = (
        int(round(int(metrics["agent_total_latency_ms"]) / completed_count))
        if completed_count
        else 0
    )

    payload_size_bytes = 0
    if context_stats:
        payload_size_bytes = int(context_stats.get("requestPayloadBytes", 0) or 0)
    metrics["agent_payload_size_bytes"] = payload_size_bytes
    metrics["agent_last_filtered_local_artist_count"] = filtered_local_artist_count
    _increment_metric(
        metrics,
        "agent_filtered_local_artist_count",
        filtered_local_artist_count,
    )
    metrics["agent_last_filtered_feedback_artist_count"] = (
        filtered_feedback_artist_count
    )
    _increment_metric(
        metrics,
        "agent_filtered_feedback_artist_count",
        filtered_feedback_artist_count,
    )
    if empty_result:
        _increment_metric(metrics, "agent_empty_result_count")
    if cache_hit:
        _increment_metric(metrics, "agent_cache_hit_count")
    return metrics


def _log_recommendation_agent_metrics(
    status: str,
    metrics: Mapping[str, object],
    cache_hit: bool = False,
) -> None:
    logger.info(
        "recommendation_agent_metrics status=%s cache_hit=%s "
        "agent_request_count=%s agent_success_count=%s agent_error_count=%s "
        "agent_timeout_count=%s agent_cache_hit_count=%s agent_latency_ms=%s "
        "agent_average_latency_ms=%s agent_payload_size_bytes=%s "
        "agent_filtered_local_artist_count=%s "
        "agent_last_filtered_local_artist_count=%s "
        "agent_filtered_feedback_artist_count=%s "
        "agent_last_filtered_feedback_artist_count=%s "
        "agent_empty_result_count=%s",
        status,
        cache_hit,
        metrics.get("agent_request_count"),
        metrics.get("agent_success_count"),
        metrics.get("agent_error_count"),
        metrics.get("agent_timeout_count"),
        metrics.get("agent_cache_hit_count"),
        metrics.get("agent_latency_ms"),
        metrics.get("agent_average_latency_ms"),
        metrics.get("agent_payload_size_bytes"),
        metrics.get("agent_filtered_local_artist_count"),
        metrics.get("agent_last_filtered_local_artist_count"),
        metrics.get("agent_filtered_feedback_artist_count"),
        metrics.get("agent_last_filtered_feedback_artist_count"),
        metrics.get("agent_empty_result_count"),
    )


def _agent_config_status(config: Mapping[str, object]) -> Dict[str, object]:
    api_base_url = str(config.get("api_base_url") or "").strip()
    api_key = str(config.get("api_key") or "").strip()
    model = str(config.get("model") or "").strip()
    enabled = _enabled(config.get("enabled"))
    return {
        "enabled": enabled,
        "model": model,
        "apiBaseUrl": api_base_url,
        "configured": bool(enabled and api_base_url and api_key and model),
    }


def get_recommendation_agent_health(config: Mapping[str, object]) -> Dict[str, object]:
    health = _agent_config_status(config)
    last_success_at = AGENT_HEALTH_STATE.get("last_success_at")
    last_error = AGENT_HEALTH_STATE.get("last_error")
    health["lastSuccessAt"] = _serialize_datetime(last_success_at)
    health["lastError"] = dict(last_error) if isinstance(last_error, dict) else None
    health["metrics"] = _public_agent_metrics()
    return health


def _record_recommendation_agent_success(
    latency_ms: int,
    context_stats: Optional[Mapping[str, object]] = None,
    result_metrics: Optional[Mapping[str, object]] = None,
    cache_hit: bool = False,
) -> None:
    AGENT_HEALTH_STATE["last_success_at"] = now()
    AGENT_HEALTH_STATE["last_error"] = None
    metrics = _agent_metrics()
    _increment_metric(metrics, "agent_success_count")
    result_metrics = result_metrics or {}
    metrics = _update_completion_metrics(
        latency_ms,
        context_stats,
        int(result_metrics.get("filteredLocalArtistCount", 0) or 0),
        int(result_metrics.get("filteredFeedbackArtistCount", 0) or 0),
        bool(result_metrics.get("emptyResult")),
        cache_hit,
    )
    _log_recommendation_agent_metrics("success", metrics, cache_hit)


def _record_recommendation_agent_error(
    exc: RecommendationAgentError,
    latency_ms: int = 0,
    context_stats: Optional[Mapping[str, object]] = None,
) -> None:
    AGENT_HEALTH_STATE["last_error"] = {
        "errorCode": exc.error_code,
        "message": str(exc),
        "details": dict(exc.details or {}),
        "at": _serialize_datetime(now()),
    }
    metrics = _agent_metrics()
    _increment_metric(metrics, "agent_error_count")
    if isinstance(exc, RecommendationAgentTimeoutError):
        _increment_metric(metrics, "agent_timeout_count")
    metrics = _update_completion_metrics(latency_ms, context_stats)
    _log_recommendation_agent_metrics("error", metrics)


def _enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "yes", "true", "on")


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _positive_float(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_float(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _serialize_datetime(value) -> str:
    if value is None or not hasattr(value, "isoformat"):
        return ""
    return value.isoformat()


def _serialize_track_metadata(metadata) -> Dict[str, object]:
    if not is_high_quality_track_metadata(metadata):
        return {}
    return {
        "language": metadata.language or "",
        "mood": metadata.get_moods(),
        "scene": metadata.get_scenes(),
        "tags": metadata.get_tags(),
        "summary": metadata.summary or "",
        "energy": metadata.energy,
        "valence": metadata.valence,
        "danceability": metadata.danceability,
        "confidence": metadata.confidence,
        "provider": metadata.provider or "",
    }


def _serialize_track(track: Track, played_at=None, metadata=None) -> Dict[str, object]:
    data = {
        "id": str(track.id),
        "title": track.title,
        "artist": track.artist.get_artist_name() if track.artist else "",
        "album": track.album.name if track.album else "",
        "genre": track.genre or "",
        "duration": track.duration,
        "playCount": track.play_count,
        "playedAt": _serialize_datetime(played_at),
    }
    semantic_metadata = _serialize_track_metadata(metadata)
    if semantic_metadata:
        data["semanticMetadata"] = semantic_metadata
    return data


def _normalize_artist_name(name: object) -> str:
    text = unicodedata.normalize("NFKC", str(name or ""))
    return " ".join(text.strip().casefold().split())


def _compact_artist_name(name: object) -> str:
    return "".join(
        char for char in _normalize_artist_name(name) if char.isalnum()
    )


def _artist_name_variants(name: object) -> List[str]:
    normalized = _normalize_artist_name(name)
    if not normalized:
        return []

    variants = {normalized}
    bracket_parts = re.findall(r"[\(\[\{（【](.*?)[\)\]\}）】]", normalized)
    variants.update(_normalize_artist_name(part) for part in bracket_parts)

    without_brackets = _normalize_artist_name(
        re.sub(r"[\(\[\{（【].*?[\)\]\}）】]", " ", normalized)
    )
    if without_brackets:
        variants.add(without_brackets)

    variants.update(
        _normalize_artist_name(match)
        for match in re.findall(r"[\u3400-\u9fff]{2,}", normalized)
    )

    non_cjk = re.sub(r"[\u3400-\u9fff]+", " ", normalized)
    if len(_compact_artist_name(non_cjk)) >= 2:
        variants.add(_normalize_artist_name(non_cjk))
    variants.update(
        _normalize_artist_name(part)
        for part in re.split(r"[,/;|、，]+", non_cjk)
        if len(_compact_artist_name(part)) >= 2
    )

    compact_variants = {_compact_artist_name(variant) for variant in variants}
    variants.update(variant for variant in compact_variants if variant)
    return sorted((variant for variant in variants if variant), key=len)


def _collect_library_artist_names() -> List[str]:
    names = set()
    for artist in Artist.select():
        if artist.name:
            names.add(str(artist.name).strip())
        resolved_name = artist.get_artist_name()
        if resolved_name:
            names.add(str(resolved_name).strip())
    return sorted((name for name in names if name), key=str.casefold)


def _hidden_feedback_artist_names(preferences: Mapping[str, object]) -> List[str]:
    hidden_names = []
    local_artist_ids = []
    seen = set()

    for target in preferences.get("hidden_artist_ids", set()) or set():
        target = str(target or "").strip()
        if not target:
            continue
        try:
            uuid.UUID(target)
        except ValueError:
            key = target.casefold()
            if key not in seen:
                seen.add(key)
                hidden_names.append(target)
            continue
        local_artist_ids.append(target)

    if local_artist_ids:
        for artist in Artist.select().where(Artist.id.in_(local_artist_ids)):
            name = (artist.get_artist_name() or artist.name or "").strip()
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                hidden_names.append(name)

    return sorted(hidden_names, key=str.casefold)[:AGENT_HIDDEN_ARTIST_LIMIT]


def _summarize_play_history(play_history: Sequence[Dict[str, object]]) -> Dict[str, object]:
    artist_counts: Dict[str, int] = {}
    genre_counts: Dict[str, int] = {}

    for entry in play_history:
        artist_name = str(entry.get("artist") or "").strip()
        genre_name = str(entry.get("genre") or "").strip()
        if artist_name:
            artist_counts[artist_name] = artist_counts.get(artist_name, 0) + 1
        if genre_name:
            genre_counts[genre_name] = genre_counts.get(genre_name, 0) + 1

    top_artists = [
        {"name": name, "playCount": count}
        for name, count in sorted(
            artist_counts.items(),
            key=lambda item: (item[1], item[0].casefold()),
            reverse=True,
        )[:RECOMMENDED_ARTIST_LIMIT]
    ]
    favorite_genres = [
        {"name": name, "playCount": count}
        for name, count in sorted(
            genre_counts.items(),
            key=lambda item: (item[1], item[0].casefold()),
            reverse=True,
        )[:5]
    ]
    return {
        "topArtists": top_artists,
        "favoriteGenres": favorite_genres,
        "recentTracks": list(play_history[:5]),
    }


def _collect_play_history(user: User, history_limit: int) -> List[Dict[str, object]]:
    play_history: List[Dict[str, object]] = []

    try:
        activities = list(
            User_Play_Activity.select()
            .where(User_Play_Activity.user == user)
            .order_by(User_Play_Activity.time.desc())
            .limit(history_limit)
        )
        metadata_by_track = _load_track_metadata(
            [activity.track_id for activity in activities]
        )
        for activity in activities:
            play_history.append(
                _serialize_track(
                    activity.track,
                    activity.time,
                    metadata_by_track.get(activity.track_id),
                )
            )
    except OperationalError:
        logger.debug("User play activity table is unavailable for recommendation agent")

    if not play_history and getattr(user, "last_play", None):
        metadata_by_track = _load_track_metadata([user.last_play_id])
        play_history.append(
            _serialize_track(
                user.last_play,
                user.last_play_date,
                metadata_by_track.get(user.last_play_id),
            )
        )

    return play_history


def _load_track_metadata(track_ids: Sequence[object]) -> Dict[object, TrackMetadata]:
    ids = [track_id for track_id in track_ids if track_id]
    if not ids:
        return {}
    return {
        metadata.track_id: metadata
        for metadata in TrackMetadata.select().where(TrackMetadata.track.in_(ids))
    }


def build_recommendation_agent_context(
    user: User,
    recommendation_tracks: Sequence[Track],
    recommendation_summary: Mapping[str, object],
    history_limit: int,
    recent_agent_sessions: Optional[Sequence[Mapping[str, object]]] = None,
) -> Dict[str, object]:
    play_history = _collect_play_history(user, history_limit)
    history_summary = _summarize_play_history(play_history)
    listening_profile = build_user_listening_profile(user)
    feedback_preferences = get_recommendation_feedback_preferences(user)
    hidden_artist_names = _hidden_feedback_artist_names(feedback_preferences)
    recommendation_metadata = _load_track_metadata(
        [track.id for track in recommendation_tracks]
    )

    return {
        "user": user.name,
        "playHistory": play_history,
        "history": {
            "activityCount": len(play_history),
            **history_summary,
        },
        "listeningProfile": listening_profile,
        "currentRecommendationTracks": [
            _serialize_track(track, metadata=recommendation_metadata.get(track.id))
            for track in recommendation_tracks
        ],
        "recommendationSummary": dict(recommendation_summary),
        "libraryArtists": _collect_library_artist_names(),
        "recommendationFeedback": {
            "hiddenArtistNames": hidden_artist_names,
        },
        "recentAgentSessions": list(recent_agent_sessions or []),
    }


def _validate_config(config: Mapping[str, object]) -> Dict[str, object]:
    if not _enabled(config.get("enabled")):
        raise RecommendationAgentConfigError("Recommendation agent model is disabled.")

    api_base_url = str(config.get("api_base_url") or "").strip()
    api_key = str(config.get("api_key") or "").strip()
    model = str(config.get("model") or "").strip()
    if not api_base_url or not api_key or not model:
        raise RecommendationAgentConfigError(
            "Recommendation agent model is not configured."
        )

    return {
        "api_base_url": api_base_url,
        "api_key": api_key,
        "model": model,
        "timeout_seconds": _positive_float(
            config.get("timeout_seconds"),
            DEFAULT_TIMEOUT_SECONDS,
        ),
        "history_limit": _positive_int(
            config.get("history_limit"),
            DEFAULT_HISTORY_LIMIT,
        ),
        "max_output_tokens": _non_negative_int(
            config.get("max_output_tokens"),
            DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        "temperature": _non_negative_float(
            config.get("temperature"),
            DEFAULT_TEMPERATURE,
        ),
        "cache_ttl_seconds": _non_negative_int(
            config.get("cache_ttl_seconds"),
            DEFAULT_AGENT_CACHE_TTL_SECONDS,
        ),
    }


def _build_system_prompt(language: str) -> str:
    response_language = "Simplified Chinese" if language == "zh" else "English"
    return (
        "You are the Emosonic Recommendation Agent. Your job is to discover "
        "artists outside the user's local music library from their listening "
        "history and the current recommendation context. Do not recommend any "
        "artist whose name appears in context.libraryArtists, including spelling "
        "or capitalization variants. Do not recommend artists listed in "
        "context.recommendationFeedback.hiddenArtistNames; those artists were "
        "hidden by the user. Use the user's full playHistory as the main "
        "signal, then listeningProfile, topArtists, favoriteGenres, and "
        "currentRecommendationTracks. When recommending artists, use "
        "context.listeningProfile as an explicit semantic signal: topMoods, "
        "topScenes, topTags, topLanguages, averageEnergy, averageValence, and "
        "averageDanceability. If the user asks why an artist is recommended, "
        "explain how the artist relates to those profile fields while also "
        "using playHistory, currentRecommendationTracks, and feedback. Do not "
        "overfit to one profile field, and do not invent semantic metadata that "
        "is not present in context. "
        "If context.previousRecommendedArtists is present and the user asks a "
        "follow-up such as starter songs, starter tracks, reasons, comparisons, "
        "or why these artists were recommended, answer about those previous "
        "artists and keep them in recommendedArtists unless the user explicitly "
        "asks for different or new artists. For starter-track follow-ups, improve "
        "or expand starterTracks for the previous artists instead of discovering "
        "a fresh set. If context.recentAgentSessions is present, use it as durable "
        "conversation memory across page refreshes; when the user says these, them, "
        "previous, earlier, or prompt-button follow-up wording such as changing "
        "style, asking for more obscure artists, or asking for more like this, "
        "resolve that reference against the most recent relevant session. "
        f"Reply in {response_language}. Return only a JSON object with this exact "
        "shape: {\"reply\": string, \"recommendedArtists\": [{\"name\": string, "
        "\"reason\": string, \"genres\": [string], \"starterTracks\": [string], "
        "\"similarTo\": [string], \"confidence\": number, \"mood\": [string]}], "
        "\"nextActions\": [string]}. confidence must be between 0 and 1. "
        "nextActions should contain short follow-up actions such as generating "
        "starter tracks, changing style, or recommending more obscure artists. "
        "recommendedArtists must contain only outside-library artists."
    )


def _sanitize_string_list(
    value: object,
    limit: int,
    item_limit: int,
) -> List[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item).strip()[:item_limit]
        for item in value
        if str(item).strip()
    ][:limit]


def _sanitize_confidence(value: object) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, confidence))


def _sanitize_next_actions(value: object) -> List[str]:
    return _sanitize_string_list(value, NEXT_ACTION_LIMIT, 80)


def _sanitize_recommended_artists(
    artists: Optional[Sequence[Mapping[str, object]]],
) -> List[Dict[str, object]]:
    sanitized = []
    for artist in list(artists or [])[:RECOMMENDED_ARTIST_LIMIT]:
        if not isinstance(artist, Mapping):
            continue
        name = str(artist.get("name") or "").strip()
        if not name:
            continue
        genres = artist.get("genres")
        if not isinstance(genres, list):
            genres = []
        starter_tracks = artist.get("starterTracks")
        if not isinstance(starter_tracks, list):
            starter_tracks = []
        sanitized.append(
            {
                "name": name[:120],
                "reason": str(artist.get("reason") or "").strip()[:500],
                "genres": _sanitize_string_list(genres, 6, 80),
                "starterTracks": _sanitize_string_list(starter_tracks, 8, 120),
                "similarTo": _sanitize_string_list(artist.get("similarTo"), 6, 120),
                "confidence": _sanitize_confidence(artist.get("confidence")),
                "mood": _sanitize_string_list(artist.get("mood"), 6, 80),
            }
        )
    return sanitized


def _message_looks_like_followup(message: str) -> bool:
    normalized = str(message or "").casefold()
    if any(marker in normalized for marker in FOLLOW_UP_PHRASE_MARKERS):
        return True
    words = set(re.findall(r"[a-z0-9']+", normalized))
    return any(marker in words for marker in FOLLOW_UP_WORD_MARKERS)


def _effective_previous_recommended_artists(
    message: str,
    context: Mapping[str, object],
    previous_recommended_artists: Optional[Sequence[Mapping[str, object]]] = None,
) -> List[Dict[str, object]]:
    previous_artists = _sanitize_recommended_artists(previous_recommended_artists)
    if not previous_artists and _message_looks_like_followup(message):
        recent_sessions = context.get("recentAgentSessions")
        if isinstance(recent_sessions, list):
            previous_artists = _sanitize_recommended_artists(
                latest_recommended_artists_from_sessions(recent_sessions)
            )
    return previous_artists


def _build_agent_prompt_context(
    message: str,
    context: Mapping[str, object],
    previous_recommended_artists: Optional[Sequence[Mapping[str, object]]] = None,
) -> Dict[str, object]:
    prompt_context = dict(context)
    previous_artists = _effective_previous_recommended_artists(
        message,
        context,
        previous_recommended_artists,
    )
    if previous_artists:
        prompt_context["previousRecommendedArtists"] = previous_artists
    return prompt_context


def _build_user_prompt(
    message: str,
    context: Mapping[str, object],
    previous_recommended_artists: Optional[Sequence[Mapping[str, object]]] = None,
) -> str:
    payload = {
        "userMessage": message,
        "context": _build_agent_prompt_context(
            message,
            context,
            previous_recommended_artists,
        ),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _build_repair_user_prompt(
    message: str,
    context: Mapping[str, object],
    previous_error: Exception,
    previous_recommended_artists: Optional[Sequence[Mapping[str, object]]] = None,
) -> str:
    payload = {
        "userMessage": message,
        "context": _build_agent_prompt_context(
            message,
            context,
            previous_recommended_artists,
        ),
        "previousError": str(previous_error),
        "repairInstruction": (
            "The previous model response could not be parsed by the server. "
            "Generate a fresh response for the same user request and context. "
            "Return only one valid JSON object with reply, recommendedArtists, "
            "and nextActions."
        ),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _extract_json_text(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            parsed, end = decoder.raw_decode(stripped[index:])
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return stripped[index : index + end]
    return stripped


def _parse_model_content(content: object) -> Dict[str, object]:
    if not isinstance(content, str) or not content.strip():
        raise RecommendationAgentInvalidResponseError(
            "Recommendation model returned an empty response."
        )

    try:
        parsed = json.loads(_extract_json_text(content))
    except ValueError as exc:
        raise RecommendationAgentInvalidResponseError(
            "Recommendation model returned invalid JSON."
        ) from exc

    if not isinstance(parsed, dict):
        raise RecommendationAgentInvalidResponseError(
            "Recommendation model response must be a JSON object."
        )
    if not isinstance(parsed.get("reply"), str):
        raise RecommendationAgentInvalidResponseError(
            "Recommendation model response is missing reply."
        )
    if not isinstance(parsed.get("recommendedArtists"), list):
        raise RecommendationAgentInvalidResponseError(
            "Recommendation model response is missing recommendedArtists."
        )

    valid_artists = []
    for artist in parsed["recommendedArtists"]:
        if not isinstance(artist, dict) or not isinstance(artist.get("name"), str):
            raise RecommendationAgentInvalidResponseError(
                "Recommendation model returned an invalid artist entry."
            )
        if not isinstance(artist.get("reason"), str):
            raise RecommendationAgentInvalidResponseError(
                "Recommendation model artist entry is missing reason."
            )
        if not isinstance(artist.get("genres"), list):
            raise RecommendationAgentInvalidResponseError(
                "Recommendation model artist entry is missing genres."
            )
        if not isinstance(artist.get("starterTracks"), list):
            raise RecommendationAgentInvalidResponseError(
                "Recommendation model artist entry is missing starterTracks."
            )
        valid_artists.append(
            {
                "name": artist["name"].strip()[:120],
                "reason": artist["reason"].strip()[:500],
                "genres": _sanitize_string_list(artist["genres"], 6, 80),
                "starterTracks": _sanitize_string_list(
                    artist["starterTracks"],
                    8,
                    120,
                ),
                "similarTo": _sanitize_string_list(artist.get("similarTo"), 6, 120),
                "confidence": _sanitize_confidence(artist.get("confidence")),
                "mood": _sanitize_string_list(artist.get("mood"), 6, 80),
            }
        )

    parsed["recommendedArtists"] = [
        artist for artist in valid_artists if artist["name"]
    ]
    parsed["nextActions"] = _sanitize_next_actions(parsed.get("nextActions"))
    return parsed


def _filter_library_artists(
    model_payload: Dict[str, object],
    library_artist_names: Sequence[str],
) -> Dict[str, object]:
    existing_names = set()
    for name in library_artist_names:
        existing_names.update(_artist_name_variants(name))

    filtered_artists = []
    for artist in model_payload["recommendedArtists"]:
        if existing_names.intersection(_artist_name_variants(artist["name"])):
            continue
        filtered_artists.append(artist)

    model_payload = dict(model_payload)
    model_payload["recommendedArtists"] = filtered_artists
    return model_payload


def _filter_hidden_feedback_artists(
    model_payload: Dict[str, object],
    hidden_artist_names: Sequence[str],
) -> Dict[str, object]:
    hidden_names = set()
    for name in hidden_artist_names:
        hidden_names.update(_artist_name_variants(name))

    filtered_artists = []
    for artist in model_payload["recommendedArtists"]:
        if hidden_names.intersection(_artist_name_variants(artist["name"])):
            continue
        filtered_artists.append(artist)

    model_payload = dict(model_payload)
    model_payload["recommendedArtists"] = filtered_artists
    return model_payload


def _filter_notice_reply(language: str, filtered_artists: Sequence[object]) -> str:
    if language == "zh":
        if filtered_artists:
            return "模型返回的部分歌手已经在曲库中或被你标记为不感兴趣，我已过滤，只保留下列可展示歌手。"
        return "模型返回的歌手都已经在曲库中或被你标记为不感兴趣，因此没有可展示的曲库外歌手。请换一个方向继续问我。"

    if filtered_artists:
        return (
            "Some artists from the model response were already in your library "
            "or hidden by your feedback, so I filtered them out and kept only "
            "eligible outside-library artists below."
        )
    return (
        "Every artist from the model response was already in your library or "
        "hidden by your feedback, so there are no outside-library artists to "
        "show. Try a different angle."
    )


def _reply_with_filter_notice(
    language: str,
    reply: object,
    filtered_artists: Sequence[object],
) -> str:
    notice = _filter_notice_reply(language, filtered_artists)
    reply_text = str(reply or "").strip()
    if not filtered_artists or not reply_text:
        return notice
    return f"{reply_text}\n\n{notice}"


def _build_chat_completion_payload(
    llm_config: Mapping[str, object],
    messages: Sequence[Mapping[str, str]],
) -> Dict[str, object]:
    request_payload: Dict[str, object] = {
        "model": llm_config["model"],
        "messages": list(messages),
        "temperature": llm_config["temperature"],
        "response_format": {"type": "json_object"},
    }
    max_output_tokens = int(llm_config["max_output_tokens"])
    if max_output_tokens > 0:
        request_payload["max_tokens"] = max_output_tokens
    return request_payload


def _build_stream_chat_completion_payload(
    request_payload: Mapping[str, object],
) -> Dict[str, object]:
    stream_payload = dict(request_payload)
    stream_payload["stream"] = True
    return stream_payload


def _safe_text(value: object, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_optional_number(value: object):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compact_semantic_metadata(value: object) -> Dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    compact = {
        "language": _safe_text(value.get("language"), 16),
        "mood": _sanitize_string_list(value.get("mood"), 6, 80),
        "scene": _sanitize_string_list(value.get("scene"), 6, 80),
        "tags": _sanitize_string_list(value.get("tags"), 8, 80),
        "summary": _safe_text(value.get("summary"), 240),
        "energy": _safe_optional_number(value.get("energy")),
        "valence": _safe_optional_number(value.get("valence")),
        "danceability": _safe_optional_number(value.get("danceability")),
        "confidence": _safe_optional_number(value.get("confidence")),
        "provider": _safe_text(value.get("provider"), 64),
    }
    return {
        key: val
        for key, val in compact.items()
        if val not in ("", [], None)
    }


def _compact_agent_track_summary(track: Mapping[str, object]) -> Dict[str, object]:
    summary = {
        "id": _safe_text(track.get("id"), 64),
        "title": _safe_text(track.get("title"), 160),
        "artist": _safe_text(track.get("artist"), 160),
        "album": _safe_text(track.get("album"), 160),
        "genre": _safe_text(track.get("genre"), 80),
        "playCount": _safe_int(track.get("playCount")),
    }
    semantic_metadata = _compact_semantic_metadata(track.get("semanticMetadata"))
    if semantic_metadata:
        summary["semanticMetadata"] = semantic_metadata
    return summary


def _compact_agent_play_history_summary(
    track: Mapping[str, object],
) -> Dict[str, object]:
    summary = _compact_agent_track_summary(track)
    summary["duration"] = _safe_int(track.get("duration"))
    summary["playedAt"] = _safe_text(track.get("playedAt"), 64)
    return summary


def _extract_upstream_error(response: object) -> Dict[str, object]:
    status_code = getattr(response, "status_code", None)
    details: Dict[str, object] = {
        "upstreamStatus": status_code,
        "retryable": status_code == 429
        or (isinstance(status_code, int) and status_code >= 500),
    }

    message = ""
    error_code = ""
    try:
        body = response.json()  # type: ignore[attr-defined]
    except ValueError:
        body = None

    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
            error_code = str(error.get("code") or error.get("type") or "")
        elif error is not None:
            message = str(error)
    if not message:
        message = getattr(response, "text", "") or getattr(response, "reason", "")

    if message:
        details["upstreamMessage"] = _safe_text(message)
    if error_code:
        details["upstreamErrorCode"] = _safe_text(error_code, 120)
    return details


def _upstream_response_format_error(details: Mapping[str, object]) -> bool:
    message = str(details.get("upstreamMessage") or "").casefold()
    code = str(details.get("upstreamErrorCode") or "").casefold()
    return (
        details.get("upstreamStatus") == 400
        and "response_format" in f"{message} {code}"
    )


def _raise_upstream_error_from_response(response: object) -> None:
    details = _extract_upstream_error(response)
    raise RecommendationAgentUpstreamError(
        "Recommendation model request failed.",
        details=details,
    )


def _post_chat_completion_once(
    endpoint: str,
    llm_config: Mapping[str, object],
    request_payload: Mapping[str, object],
) -> Dict[str, object]:
    try:
        response = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {llm_config['api_key']}",
                "Accept": "application/json",
                "Connection": "close",
                "Content-Type": "application/json",
            },
            json=request_payload,
            timeout=llm_config["timeout_seconds"],
        )
    except requests.exceptions.Timeout as exc:
        raise RecommendationAgentTimeoutError(
            "Recommendation model request timed out.",
            details={"retryable": True},
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise RecommendationAgentUpstreamError(
            "Recommendation model request failed.",
            details={
                "upstreamErrorCode": exc.__class__.__name__,
                "upstreamMessage": _safe_text(exc),
                "retryable": True,
            },
        ) from exc

    if getattr(response, "status_code", 200) >= 400:
        _raise_upstream_error_from_response(response)

    response.encoding = "utf-8"
    try:
        response_json = response.json()
    except ValueError as exc:
        raise RecommendationAgentInvalidResponseError(
            "Recommendation model returned non-JSON data."
        ) from exc

    if not isinstance(response_json, dict):
        raise RecommendationAgentInvalidResponseError(
            "Recommendation model returned invalid response data."
        )
    return response_json


def _post_chat_completion_stream_once(
    endpoint: str,
    llm_config: Mapping[str, object],
    request_payload: Mapping[str, object],
) -> requests.Response:
    try:
        response = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {llm_config['api_key']}",
                "Accept": "text/event-stream",
                "Connection": "close",
                "Content-Type": "application/json",
            },
            json=request_payload,
            stream=True,
            timeout=llm_config["timeout_seconds"],
        )
    except requests.exceptions.Timeout as exc:
        raise RecommendationAgentTimeoutError(
            "Recommendation model request timed out.",
            details={"retryable": True},
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise RecommendationAgentUpstreamError(
            "Recommendation model request failed.",
            details={
                "upstreamErrorCode": exc.__class__.__name__,
                "upstreamMessage": _safe_text(exc),
                "retryable": True,
            },
        ) from exc

    if getattr(response, "status_code", 200) >= 400:
        _raise_upstream_error_from_response(response)
    return response


def _without_response_format(request_payload: Mapping[str, object]) -> Dict[str, object]:
    fallback_payload = dict(request_payload)
    fallback_payload.pop("response_format", None)
    return fallback_payload


def _post_chat_completion(
    endpoint: str,
    llm_config: Mapping[str, object],
    request_payload: Mapping[str, object],
) -> Dict[str, object]:
    try:
        return _post_chat_completion_once(endpoint, llm_config, request_payload)
    except RecommendationAgentTimeoutError:
        return _post_chat_completion_once(endpoint, llm_config, request_payload)
    except RecommendationAgentUpstreamError as first_error:
        if _upstream_response_format_error(first_error.details):
            return _post_chat_completion_once(
                endpoint,
                llm_config,
                _without_response_format(request_payload),
            )
        if first_error.details.get("retryable"):
            return _post_chat_completion_once(endpoint, llm_config, request_payload)
        raise


def _post_chat_completion_stream(
    endpoint: str,
    llm_config: Mapping[str, object],
    request_payload: Mapping[str, object],
) -> requests.Response:
    try:
        return _post_chat_completion_stream_once(endpoint, llm_config, request_payload)
    except RecommendationAgentTimeoutError:
        return _post_chat_completion_stream_once(endpoint, llm_config, request_payload)
    except RecommendationAgentUpstreamError as first_error:
        if _upstream_response_format_error(first_error.details):
            return _post_chat_completion_stream_once(
                endpoint,
                llm_config,
                _without_response_format(request_payload),
            )
        if first_error.details.get("retryable"):
            return _post_chat_completion_stream_once(endpoint, llm_config, request_payload)
        raise


def _iter_chat_completion_stream_content(response: requests.Response) -> Iterator[str]:
    try:
        for raw_line in response.iter_lines(decode_unicode=False):
            if not raw_line:
                continue
            line = (
                raw_line.decode("utf-8", errors="replace")
                if isinstance(raw_line, bytes)
                else str(raw_line)
            ).strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                payload = json.loads(data)
                first_choice = payload["choices"][0]
                delta = first_choice.get("delta") or {}
                content = delta.get("content")
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if isinstance(content, str) and content:
                yield content
    except requests.exceptions.RequestException as exc:
        raise RecommendationAgentUpstreamError(
            "Recommendation model stream failed.",
            details={
                "upstreamErrorCode": exc.__class__.__name__,
                "upstreamMessage": _safe_text(exc),
                "retryable": True,
            },
        ) from exc
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _extract_partial_json_string(content: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}"\s*:', content)
    if not match:
        return ""

    index = match.end()
    while index < len(content) and content[index].isspace():
        index += 1
    if index >= len(content) or content[index] != '"':
        return ""

    index += 1
    chars: List[str] = []
    escapes = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }
    while index < len(content):
        char = content[index]
        if char == '"':
            break
        if char != "\\":
            chars.append(char)
            index += 1
            continue

        if index + 1 >= len(content):
            break
        escape = content[index + 1]
        if escape == "u":
            hex_value = content[index + 2 : index + 6]
            if len(hex_value) < 4 or not re.fullmatch(r"[0-9a-fA-F]{4}", hex_value):
                break
            chars.append(chr(int(hex_value, 16)))
            index += 6
            continue
        chars.append(escapes.get(escape, escape))
        index += 2
    return "".join(chars)


def _extract_partial_reply(content: str) -> str:
    return _extract_partial_json_string(content, "reply")


def _parse_chat_completion_response(
    response_json: Mapping[str, object],
) -> Dict[str, object]:
    try:
        first_choice = response_json["choices"][0]  # type: ignore[index]
        content = first_choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RecommendationAgentInvalidResponseError(
            "Recommendation model response did not include message content."
        ) from exc
    return _parse_model_content(content)


def _payload_size_bytes(payload: Mapping[str, object]) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _build_context_stats(
    agent_context: Mapping[str, object],
    recommendation_tracks: Sequence[Track],
    request_payload: Mapping[str, object],
) -> Dict[str, object]:
    play_history = agent_context.get("playHistory") or []
    library_artists = agent_context.get("libraryArtists") or []
    recommendation_feedback = agent_context.get("recommendationFeedback") or {}
    hidden_artist_names = []
    if isinstance(recommendation_feedback, Mapping):
        hidden_artist_names = recommendation_feedback.get("hiddenArtistNames") or []
    return {
        "playHistoryCount": len(play_history) if isinstance(play_history, list) else 0,
        "libraryArtistCount": (
            len(library_artists) if isinstance(library_artists, list) else 0
        ),
        "hiddenAgentArtistCount": (
            len(hidden_artist_names) if isinstance(hidden_artist_names, list) else 0
        ),
        "recommendationTrackCount": len(recommendation_tracks),
        "agentSessionCount": len(agent_context.get("recentAgentSessions") or []),
        "listeningProfilePlayCount": int(
            (agent_context.get("listeningProfile") or {}).get("playCount", 0)
            if isinstance(agent_context.get("listeningProfile"), Mapping)
            else 0
        ),
        "requestPayloadBytes": _payload_size_bytes(request_payload),
    }


def _attach_context_stats(
    exc: RecommendationAgentError,
    context_stats: Mapping[str, object],
) -> None:
    exc.add_details(contextStats=dict(context_stats))


def _prepare_recommendation_agent_request(
    config: Mapping[str, object],
    user: User,
    message: str,
    language: str,
    recommendation_tracks: Sequence[Track],
    recommendation_summary: Mapping[str, object],
    previous_recommended_artists: Optional[Sequence[Mapping[str, object]]] = None,
) -> Dict[str, object]:
    llm_config = _validate_config(config)
    recent_agent_sessions = list_recommendation_agent_sessions(
        user,
        DEFAULT_AGENT_SESSION_LIMIT,
    )
    agent_context = build_recommendation_agent_context(
        user,
        recommendation_tracks,
        recommendation_summary,
        int(llm_config["history_limit"]),
        recent_agent_sessions,
    )
    messages = [
        {"role": "system", "content": _build_system_prompt(language)},
        {
            "role": "user",
            "content": _build_user_prompt(
                message,
                agent_context,
                previous_recommended_artists,
            ),
        },
    ]
    request_payload = _build_chat_completion_payload(llm_config, messages)
    endpoint = f"{str(llm_config['api_base_url']).rstrip('/')}/chat/completions"
    context_stats = _build_context_stats(
        agent_context,
        recommendation_tracks,
        request_payload,
    )
    return {
        "llm_config": llm_config,
        "agent_context": agent_context,
        "request_payload": request_payload,
        "endpoint": endpoint,
        "context_stats": context_stats,
        "previous_recommended_artists": _sanitize_recommended_artists(
            previous_recommended_artists,
        ),
    }


def _build_agent_session_context_summary(
    agent_context: Mapping[str, object],
) -> Dict[str, object]:
    recommendation_tracks = agent_context.get("currentRecommendationTracks") or []
    if not isinstance(recommendation_tracks, list):
        recommendation_tracks = []
    return {
        "history": dict(agent_context.get("history") or {}),
        "recommendationSummary": dict(
            agent_context.get("recommendationSummary") or {}
        ),
        "currentRecommendationTracks": [
            _compact_agent_track_summary(track)
            for track in recommendation_tracks[:AGENT_SESSION_TRACK_LIMIT]
            if isinstance(track, Mapping) and str(track.get("id") or "")
        ],
    }


def _build_agent_cache_context_hash(
    user: User,
    message: str,
    language: str,
    model: str,
    agent_context: Mapping[str, object],
    previous_recommended_artists: Optional[Sequence[Mapping[str, object]]] = None,
) -> str:
    previous_artists = _effective_previous_recommended_artists(
        message,
        agent_context,
        previous_recommended_artists,
    )
    recommendation_tracks = agent_context.get("currentRecommendationTracks") or []
    if not isinstance(recommendation_tracks, list):
        recommendation_tracks = []
    play_history = agent_context.get("playHistory") or []
    if not isinstance(play_history, list):
        play_history = []
    return build_recommendation_agent_context_hash(
        {
            "userId": str(user.id),
            "message": " ".join(str(message or "").split()),
            "language": language,
            "model": model,
            "history": agent_context.get("history") or {},
            "listeningProfile": agent_context.get("listeningProfile") or {},
            "playHistory": [
                _compact_agent_play_history_summary(track)
                for track in play_history[:AGENT_CACHE_PLAY_HISTORY_LIMIT]
                if isinstance(track, Mapping) and track.get("id")
            ],
            "recommendationFeedback": agent_context.get("recommendationFeedback") or {},
            "recommendationSummary": agent_context.get("recommendationSummary") or {},
            "recommendationTracks": sorted(
                (
                    _compact_agent_track_summary(track)
                    for track in recommendation_tracks[:AGENT_CACHE_TRACK_LIMIT]
                    if isinstance(track, Mapping) and track.get("id")
                ),
                key=lambda track: str(track["id"]),
            ),
            "previousRecommendedArtists": previous_artists,
        }
    )


def _finalize_recommendation_agent_payload(
    llm_config: Mapping[str, object],
    agent_context: Mapping[str, object],
    model_payload: Dict[str, object],
    language: str,
) -> Tuple[Dict[str, object], Dict[str, object]]:
    original_artist_count = len(model_payload["recommendedArtists"])
    original_reply = model_payload["reply"]
    model_payload = _filter_library_artists(
        model_payload,
        agent_context["libraryArtists"],
    )
    filtered_local_artist_count = (
        original_artist_count - len(model_payload["recommendedArtists"])
    )
    recommendation_feedback = agent_context.get("recommendationFeedback") or {}
    hidden_artist_names = []
    if isinstance(recommendation_feedback, Mapping):
        hidden_artist_names = recommendation_feedback.get("hiddenArtistNames") or []
    visible_artist_count = len(model_payload["recommendedArtists"])
    model_payload = _filter_hidden_feedback_artists(
        model_payload,
        hidden_artist_names if isinstance(hidden_artist_names, list) else [],
    )
    filtered_feedback_artist_count = (
        visible_artist_count - len(model_payload["recommendedArtists"])
    )
    if (
        filtered_local_artist_count > 0
        or filtered_feedback_artist_count > 0
    ):
        model_payload["reply"] = _reply_with_filter_notice(
            language,
            original_reply,
            model_payload["recommendedArtists"],
        )
    payload = {
        "ok": True,
        "agent": {
            "name": "Recommendation Agent",
            "mode": "llm",
            "model": llm_config["model"],
        },
        "reply": model_payload["reply"],
        "recommendedArtists": model_payload["recommendedArtists"],
        "nextActions": _sanitize_next_actions(model_payload.get("nextActions")),
        "history": agent_context["history"],
        "recommendationSummary": agent_context["recommendationSummary"],
    }
    return payload, {
        "filteredLocalArtistCount": filtered_local_artist_count,
        "filteredFeedbackArtistCount": filtered_feedback_artist_count,
        "emptyResult": not bool(payload["recommendedArtists"]),
    }


def _attach_agent_session(
    payload: Dict[str, object],
    user: User,
    message: str,
    language: str,
    model: str,
    agent_context: Mapping[str, object],
) -> Dict[str, object]:
    session = save_recommendation_agent_session(
        user,
        message,
        str(payload.get("reply") or ""),
        payload.get("recommendedArtists") or [],
        _build_agent_session_context_summary(agent_context),
        model,
        language,
    )
    payload["agentSession"] = session
    payload["agentSessions"] = list_recommendation_agent_sessions(
        user,
        DEFAULT_AGENT_SESSION_LIMIT,
    )
    return payload


def request_recommendation_agent(
    config: Mapping[str, object],
    user: User,
    message: str,
    language: str,
    recommendation_tracks: Sequence[Track],
    recommendation_summary: Mapping[str, object],
    previous_recommended_artists: Optional[Sequence[Mapping[str, object]]] = None,
    force_refresh: bool = False,
) -> Dict[str, object]:
    started_at = time.monotonic()
    _record_recommendation_agent_request()
    try:
        prepared = _prepare_recommendation_agent_request(
            config,
            user,
            message,
            language,
            recommendation_tracks,
            recommendation_summary,
            previous_recommended_artists,
        )
    except RecommendationAgentError as exc:
        _record_recommendation_agent_error(exc, _elapsed_ms(started_at))
        raise
    llm_config = prepared["llm_config"]
    agent_context = prepared["agent_context"]
    request_payload = prepared["request_payload"]
    endpoint = prepared["endpoint"]
    context_stats = prepared["context_stats"]
    previous_recommended_artists = prepared["previous_recommended_artists"]
    cache_ttl_seconds = int(llm_config["cache_ttl_seconds"])
    cache_context_hash = _build_agent_cache_context_hash(
        user,
        message,
        language,
        str(llm_config["model"]),
        agent_context,
        previous_recommended_artists,
    )
    if cache_ttl_seconds > 0 and not force_refresh:
        cached_payload = get_cached_recommendation_agent_payload(
            user,
            cache_context_hash,
        )
        if cached_payload is not None:
            payload = dict(cached_payload)
            payload["agent"] = dict(payload.get("agent") or {})
            payload["agent"]["cached"] = True
            payload["cache"] = {"hit": True, "contextHash": cache_context_hash}
            _record_recommendation_agent_success(
                _elapsed_ms(started_at),
                context_stats,
                {"emptyResult": not bool(payload.get("recommendedArtists"))},
                cache_hit=True,
            )
            return _attach_agent_session(
                payload,
                user,
                message,
                language,
                str(llm_config["model"]),
                agent_context,
            )

    try:
        try:
            response_json = _post_chat_completion(endpoint, llm_config, request_payload)
            model_payload = _parse_chat_completion_response(response_json)
        except RecommendationAgentInvalidResponseError as first_error:
            repair_messages = [
                {"role": "system", "content": _build_system_prompt(language)},
                {
                    "role": "user",
                    "content": _build_repair_user_prompt(
                        message,
                        agent_context,
                        first_error,
                        previous_recommended_artists,
                    ),
                },
            ]
            repair_payload = _build_chat_completion_payload(llm_config, repair_messages)
            repair_response_json = _post_chat_completion(
                endpoint,
                llm_config,
                repair_payload,
            )
            try:
                model_payload = _parse_chat_completion_response(repair_response_json)
            except RecommendationAgentInvalidResponseError as second_error:
                raise second_error from first_error
    except RecommendationAgentError as exc:
        _attach_context_stats(exc, context_stats)
        _record_recommendation_agent_error(
            exc,
            _elapsed_ms(started_at),
            context_stats,
        )
        logger.warning(
            "recommendation_agent_failed user=%s error_code=%s upstream_status=%s context_stats=%s",
            user.name,
            exc.error_code,
            exc.details.get("upstreamStatus"),
            exc.details.get("contextStats"),
        )
        raise

    payload, result_metrics = _finalize_recommendation_agent_payload(
        llm_config,
        agent_context,
        model_payload,
        language,
    )
    _record_recommendation_agent_success(
        _elapsed_ms(started_at),
        context_stats,
        result_metrics,
    )
    payload["cache"] = {"hit": False, "contextHash": cache_context_hash}
    save_recommendation_agent_cache_payload(
        user,
        cache_context_hash,
        message,
        language,
        str(llm_config["model"]),
        payload,
        cache_ttl_seconds,
    )
    return _attach_agent_session(
        payload,
        user,
        message,
        language,
        str(llm_config["model"]),
        agent_context,
    )


def stream_recommendation_agent(
    config: Mapping[str, object],
    user: User,
    message: str,
    language: str,
    recommendation_tracks: Sequence[Track],
    recommendation_summary: Mapping[str, object],
    previous_recommended_artists: Optional[Sequence[Mapping[str, object]]] = None,
    force_refresh: bool = False,
) -> Iterator[Tuple[str, Dict[str, object]]]:
    started_at = time.monotonic()
    _record_recommendation_agent_request()
    try:
        prepared = _prepare_recommendation_agent_request(
            config,
            user,
            message,
            language,
            recommendation_tracks,
            recommendation_summary,
            previous_recommended_artists,
        )
    except RecommendationAgentError as exc:
        _record_recommendation_agent_error(exc, _elapsed_ms(started_at))
        raise
    llm_config = prepared["llm_config"]
    agent_context = prepared["agent_context"]
    endpoint = prepared["endpoint"]
    request_payload = _build_stream_chat_completion_payload(
        prepared["request_payload"],
    )
    previous_recommended_artists = prepared["previous_recommended_artists"]
    cache_ttl_seconds = int(llm_config["cache_ttl_seconds"])
    cache_context_hash = _build_agent_cache_context_hash(
        user,
        message,
        language,
        str(llm_config["model"]),
        agent_context,
        previous_recommended_artists,
    )
    context_stats = _build_context_stats(
        agent_context,
        recommendation_tracks,
        request_payload,
    )

    if cache_ttl_seconds > 0 and not force_refresh:
        cached_payload = get_cached_recommendation_agent_payload(
            user,
            cache_context_hash,
        )
        if cached_payload is not None:
            yield ("status", {"status": "cached"})
            payload = dict(cached_payload)
            payload["agent"] = dict(payload.get("agent") or {})
            payload["agent"]["cached"] = True
            payload["cache"] = {"hit": True, "contextHash": cache_context_hash}
            _record_recommendation_agent_success(
                _elapsed_ms(started_at),
                context_stats,
                {"emptyResult": not bool(payload.get("recommendedArtists"))},
                cache_hit=True,
            )
            yield (
                "final",
                _attach_agent_session(
                    payload,
                    user,
                    message,
                    language,
                    str(llm_config["model"]),
                    agent_context,
                ),
            )
            yield ("status", {"status": "ready"})
            return

    try:
        yield ("status", {"status": "thinking"})
        yield ("status", {"status": "receiving"})
        response = _post_chat_completion_stream(
            endpoint,
            llm_config,
            request_payload,
        )

        content = ""
        visible_reply = ""
        for delta in _iter_chat_completion_stream_content(response):
            content += delta
            next_reply = _extract_partial_reply(content)
            if len(next_reply) <= len(visible_reply):
                continue
            reply_delta = next_reply[len(visible_reply) :]
            visible_reply = next_reply
            if reply_delta:
                yield ("reply_delta", {"delta": reply_delta})

        yield ("status", {"status": "filtering"})
        try:
            model_payload = _parse_model_content(content)
        except RecommendationAgentInvalidResponseError as first_error:
            yield ("status", {"status": "repairing"})
            repair_messages = [
                {"role": "system", "content": _build_system_prompt(language)},
                {
                    "role": "user",
                    "content": _build_repair_user_prompt(
                        message,
                        agent_context,
                        first_error,
                        previous_recommended_artists,
                    ),
                },
            ]
            repair_payload = _build_chat_completion_payload(llm_config, repair_messages)
            repair_response_json = _post_chat_completion(
                endpoint,
                llm_config,
                repair_payload,
            )
            try:
                model_payload = _parse_chat_completion_response(repair_response_json)
            except RecommendationAgentInvalidResponseError as second_error:
                raise second_error from first_error
    except RecommendationAgentError as exc:
        _attach_context_stats(exc, context_stats)
        _record_recommendation_agent_error(
            exc,
            _elapsed_ms(started_at),
            context_stats,
        )
        logger.warning(
            "recommendation_agent_failed user=%s error_code=%s upstream_status=%s context_stats=%s",
            user.name,
            exc.error_code,
            exc.details.get("upstreamStatus"),
            exc.details.get("contextStats"),
        )
        raise

    payload, result_metrics = _finalize_recommendation_agent_payload(
        llm_config,
        agent_context,
        model_payload,
        language,
    )
    _record_recommendation_agent_success(
        _elapsed_ms(started_at),
        context_stats,
        result_metrics,
    )
    payload["cache"] = {"hit": False, "contextHash": cache_context_hash}
    save_recommendation_agent_cache_payload(
        user,
        cache_context_hash,
        message,
        language,
        str(llm_config["model"]),
        payload,
        cache_ttl_seconds,
    )
    payload = _attach_agent_session(
        payload,
        user,
        message,
        language,
        str(llm_config["model"]),
        agent_context,
    )
    yield ("final", payload)
    yield ("status", {"status": "ready"})
