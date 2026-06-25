import json
import logging
import re
import unicodedata
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

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
        "If context.previousRecommendedArtists is present and the user asks a "
        "follow-up such as starter songs, starter tracks, reasons, comparisons, "
        "or why these artists were recommended, answer about those previous "
        "artists and keep them in recommendedArtists unless the user explicitly "
        "asks for different or new artists. For starter-track follow-ups, improve "
        "or expand starterTracks for the previous artists instead of discovering "
        "a fresh set. "
        f"Reply in {response_language}. Return only a JSON object with this exact "
        "shape: {\"reply\": string, \"recommendedArtists\": [{\"name\": string, "
        "\"reason\": string, \"genres\": [string], \"starterTracks\": [string]}]}. "
        "recommendedArtists must contain only outside-library artists."
    )


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
                "genres": [
                    str(genre).strip()[:80]
                    for genre in genres
                    if str(genre).strip()
                ][:6],
                "starterTracks": [
                    str(track).strip()[:120]
                    for track in starter_tracks
                    if str(track).strip()
                ][:8],
            }
        )
    return sanitized


def _build_agent_prompt_context(
    context: Mapping[str, object],
    previous_recommended_artists: Optional[Sequence[Mapping[str, object]]] = None,
) -> Dict[str, object]:
    prompt_context = dict(context)
    previous_artists = _sanitize_recommended_artists(previous_recommended_artists)
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
        "context": _build_agent_prompt_context(context, previous_recommended_artists),
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
        "context": _build_agent_prompt_context(context, previous_recommended_artists),
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


def _filter_notice_reply(language: str, filtered_artists: Sequence[object]) -> str:
    if language == "zh":
        if filtered_artists:
            return "模型返回的部分歌手已经在曲库中，我已过滤，只保留下列曲库外歌手。"
        return "模型返回的歌手都已经在曲库中，因此没有可展示的曲库外歌手。请换一个方向继续问我。"

    if filtered_artists:
        return (
            "Some artists from the model response were already in your library, "
            "so I filtered them out and kept only outside-library artists below."
        )
    return (
        "Every artist from the model response was already in your library, "
        "so there are no outside-library artists to show. Try a different angle."
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
    return {
        "playHistoryCount": len(play_history) if isinstance(play_history, list) else 0,
        "libraryArtistCount": (
            len(library_artists) if isinstance(library_artists, list) else 0
        ),
        "recommendationTrackCount": len(recommendation_tracks),
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
    agent_context = build_recommendation_agent_context(
        user,
        recommendation_tracks,
        recommendation_summary,
        int(llm_config["history_limit"]),
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


def _finalize_recommendation_agent_payload(
    llm_config: Mapping[str, object],
    agent_context: Mapping[str, object],
    model_payload: Dict[str, object],
    language: str,
) -> Dict[str, object]:
    original_artist_count = len(model_payload["recommendedArtists"])
    original_reply = model_payload["reply"]
    model_payload = _filter_library_artists(
        model_payload,
        agent_context["libraryArtists"],
    )
    if len(model_payload["recommendedArtists"]) != original_artist_count:
        model_payload["reply"] = _reply_with_filter_notice(
            language,
            original_reply,
            model_payload["recommendedArtists"],
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


def request_recommendation_agent(
    config: Mapping[str, object],
    user: User,
    message: str,
    language: str,
    recommendation_tracks: Sequence[Track],
    recommendation_summary: Mapping[str, object],
    previous_recommended_artists: Optional[Sequence[Mapping[str, object]]] = None,
) -> Dict[str, object]:
    prepared = _prepare_recommendation_agent_request(
        config,
        user,
        message,
        language,
        recommendation_tracks,
        recommendation_summary,
        previous_recommended_artists,
    )
    llm_config = prepared["llm_config"]
    agent_context = prepared["agent_context"]
    request_payload = prepared["request_payload"]
    endpoint = prepared["endpoint"]
    context_stats = prepared["context_stats"]
    previous_recommended_artists = prepared["previous_recommended_artists"]
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
        logger.warning(
            "recommendation_agent_failed user=%s error_code=%s upstream_status=%s context_stats=%s",
            user.name,
            exc.error_code,
            exc.details.get("upstreamStatus"),
            exc.details.get("contextStats"),
        )
        raise

    return _finalize_recommendation_agent_payload(
        llm_config,
        agent_context,
        model_payload,
        language,
    )


def stream_recommendation_agent(
    config: Mapping[str, object],
    user: User,
    message: str,
    language: str,
    recommendation_tracks: Sequence[Track],
    recommendation_summary: Mapping[str, object],
    previous_recommended_artists: Optional[Sequence[Mapping[str, object]]] = None,
) -> Iterator[Tuple[str, Dict[str, object]]]:
    prepared = _prepare_recommendation_agent_request(
        config,
        user,
        message,
        language,
        recommendation_tracks,
        recommendation_summary,
        previous_recommended_artists,
    )
    llm_config = prepared["llm_config"]
    agent_context = prepared["agent_context"]
    endpoint = prepared["endpoint"]
    request_payload = _build_stream_chat_completion_payload(
        prepared["request_payload"],
    )
    previous_recommended_artists = prepared["previous_recommended_artists"]
    context_stats = _build_context_stats(
        agent_context,
        recommendation_tracks,
        request_payload,
    )

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
        logger.warning(
            "recommendation_agent_failed user=%s error_code=%s upstream_status=%s context_stats=%s",
            user.name,
            exc.error_code,
            exc.details.get("upstreamStatus"),
            exc.details.get("contextStats"),
        )
        raise

    payload = _finalize_recommendation_agent_payload(
        llm_config,
        agent_context,
        model_payload,
        language,
    )
    yield ("final", payload)
    yield ("status", {"status": "ready"})
