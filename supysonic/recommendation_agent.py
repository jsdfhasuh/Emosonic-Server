import json
import logging
from typing import Dict, List, Mapping, Sequence

import requests
from peewee import OperationalError

from .db import Artist, Track, User, User_Play_Activity


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


class RecommendationAgentError(Exception):
    status_code = 500
    error_code = "recommendation_agent_error"


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
        ]
    return [
        "Recommend artists outside my library",
        "Why these artists?",
        "Give me starter tracks",
    ]


def get_default_agent_message(language: str) -> str:
    return DEFAULT_AGENT_MESSAGE.get(language, DEFAULT_AGENT_MESSAGE["en"])


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


def _serialize_track(track: Track, played_at=None) -> Dict[str, object]:
    return {
        "id": str(track.id),
        "title": track.title,
        "artist": track.artist.get_artist_name() if track.artist else "",
        "album": track.album.name if track.album else "",
        "genre": track.genre or "",
        "duration": track.duration,
        "playCount": track.play_count,
        "playedAt": _serialize_datetime(played_at),
    }


def _normalize_artist_name(name: object) -> str:
    return str(name or "").strip().casefold()


def _collect_library_artist_names() -> List[str]:
    names = set()
    for artist in Artist.select():
        if artist.name:
            names.add(str(artist.name).strip())
        resolved_name = artist.get_artist_name()
        if resolved_name:
            names.add(str(resolved_name).strip())
    return sorted((name for name in names if name), key=str.casefold)


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
        activities = (
            User_Play_Activity.select()
            .where(User_Play_Activity.user == user)
            .order_by(User_Play_Activity.time.desc())
            .limit(history_limit)
        )
        for activity in activities:
            play_history.append(_serialize_track(activity.track, activity.time))
    except OperationalError:
        logger.debug("User play activity table is unavailable for recommendation agent")

    if not play_history and getattr(user, "last_play", None):
        play_history.append(_serialize_track(user.last_play, user.last_play_date))

    return play_history


def build_recommendation_agent_context(
    user: User,
    recommendation_tracks: Sequence[Track],
    recommendation_summary: Mapping[str, object],
    history_limit: int,
) -> Dict[str, object]:
    play_history = _collect_play_history(user, history_limit)
    history_summary = _summarize_play_history(play_history)

    return {
        "user": user.name,
        "playHistory": play_history,
        "history": {
            "activityCount": len(play_history),
            **history_summary,
        },
        "currentRecommendationTracks": [
            _serialize_track(track) for track in recommendation_tracks
        ],
        "recommendationSummary": dict(recommendation_summary),
        "libraryArtists": _collect_library_artist_names(),
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
    }


def _build_system_prompt(language: str) -> str:
    response_language = "Simplified Chinese" if language == "zh" else "English"
    return (
        "You are the Emosonic Recommendation Agent. Your job is to discover "
        "artists outside the user's local music library from their listening "
        "history and the current recommendation context. Do not recommend any "
        "artist whose name appears in context.libraryArtists, including spelling "
        "or capitalization variants. Use the user's full playHistory as the main "
        "signal, then topArtists, favoriteGenres, and currentRecommendationTracks. "
        f"Reply in {response_language}. Return only a JSON object with this exact "
        "shape: {\"reply\": string, \"recommendedArtists\": [{\"name\": string, "
        "\"reason\": string, \"genres\": [string], \"starterTracks\": [string]}]}. "
        "recommendedArtists must contain only outside-library artists."
    )


def _build_user_prompt(message: str, context: Mapping[str, object]) -> str:
    payload = {
        "userMessage": message,
        "context": context,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _build_repair_user_prompt(
    message: str,
    context: Mapping[str, object],
    previous_error: Exception,
) -> str:
    payload = {
        "userMessage": message,
        "context": context,
        "previousError": str(previous_error),
        "repairInstruction": (
            "The previous model response could not be parsed by the server. "
            "Generate a fresh response for the same user request and context. "
            "Return only one valid JSON object with reply and recommendedArtists."
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
                "name": artist["name"].strip(),
                "reason": artist["reason"].strip(),
                "genres": [
                    str(genre).strip()
                    for genre in artist["genres"]
                    if str(genre).strip()
                ],
                "starterTracks": [
                    str(track).strip()
                    for track in artist["starterTracks"]
                    if str(track).strip()
                ],
            }
        )

    parsed["recommendedArtists"] = [
        artist for artist in valid_artists if artist["name"]
    ]
    return parsed


def _filter_library_artists(
    model_payload: Dict[str, object],
    library_artist_names: Sequence[str],
) -> Dict[str, object]:
    existing_names = {
        _normalize_artist_name(name) for name in library_artist_names if name
    }
    filtered_artists = []
    for artist in model_payload["recommendedArtists"]:
        if _normalize_artist_name(artist["name"]) in existing_names:
            continue
        filtered_artists.append(artist)

    model_payload = dict(model_payload)
    model_payload["recommendedArtists"] = filtered_artists
    return model_payload


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


def _post_chat_completion(
    endpoint: str,
    llm_config: Mapping[str, object],
    request_payload: Mapping[str, object],
) -> Dict[str, object]:
    try:
        response = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {llm_config['api_key']}",
                "Content-Type": "application/json",
            },
            json=request_payload,
            timeout=llm_config["timeout_seconds"],
        )
        response.raise_for_status()
    except requests.exceptions.Timeout as exc:
        raise RecommendationAgentTimeoutError(
            "Recommendation model request timed out."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise RecommendationAgentUpstreamError(
            "Recommendation model request failed."
        ) from exc

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


def request_recommendation_agent(
    config: Mapping[str, object],
    user: User,
    message: str,
    language: str,
    recommendation_tracks: Sequence[Track],
    recommendation_summary: Mapping[str, object],
) -> Dict[str, object]:
    llm_config = _validate_config(config)
    agent_context = build_recommendation_agent_context(
        user,
        recommendation_tracks,
        recommendation_summary,
        int(llm_config["history_limit"]),
    )
    messages = [
        {"role": "system", "content": _build_system_prompt(language)},
        {"role": "user", "content": _build_user_prompt(message, agent_context)},
    ]
    request_payload = _build_chat_completion_payload(llm_config, messages)
    endpoint = f"{str(llm_config['api_base_url']).rstrip('/')}/chat/completions"

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

    model_payload = _filter_library_artists(
        model_payload,
        agent_context["libraryArtists"],
    )
    return {
        "ok": True,
        "agent": {
            "name": "Recommendation Agent",
            "mode": "llm",
            "model": llm_config["model"],
        },
        "reply": model_payload["reply"],
        "recommendedArtists": model_payload["recommendedArtists"],
        "history": agent_context["history"],
        "recommendationSummary": agent_context["recommendationSummary"],
    }
