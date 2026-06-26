import json
from typing import Dict, List, Mapping, Optional, Sequence

from .db import RecommendationAgentSession

DEFAULT_AGENT_SESSION_LIMIT = 6


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: object, default: object) -> object:
    if not value:
        return default
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed is not None else default


def _serialize_session(session: RecommendationAgentSession) -> Dict[str, object]:
    recommended_artists = _json_loads(session.recommended_artists_json, [])
    context_summary = _json_loads(session.context_summary_json, {})
    if not isinstance(recommended_artists, list):
        recommended_artists = []
    if not isinstance(context_summary, dict):
        context_summary = {}
    return {
        "id": str(session.id),
        "message": session.message,
        "reply": session.reply,
        "recommendedArtists": recommended_artists,
        "contextSummary": context_summary,
        "model": session.model,
        "language": session.language,
        "createdAt": session.created_at.isoformat() if session.created_at else "",
    }


def list_recommendation_agent_sessions(
    user,
    limit: int = DEFAULT_AGENT_SESSION_LIMIT,
) -> List[Dict[str, object]]:
    if user is None:
        return []
    limit = max(0, int(limit or 0))
    if limit <= 0:
        return []
    sessions = (
        RecommendationAgentSession.select()
        .where(RecommendationAgentSession.user == user)
        .order_by(RecommendationAgentSession.created_at.desc())
        .limit(limit)
    )
    return list(reversed([_serialize_session(session) for session in sessions]))


def save_recommendation_agent_session(
    user,
    message: str,
    reply: str,
    recommended_artists: Sequence[Mapping[str, object]],
    context_summary: Mapping[str, object],
    model: str,
    language: str,
) -> Dict[str, object]:
    session = RecommendationAgentSession.create(
        user=user,
        message=str(message or ""),
        reply=str(reply or ""),
        recommended_artists_json=_json_dumps(list(recommended_artists or [])),
        context_summary_json=_json_dumps(dict(context_summary or {})),
        model=str(model or "")[:128],
        language=str(language or "")[:8],
    )
    return _serialize_session(session)


def clear_recommendation_agent_sessions(user) -> int:
    if user is None:
        return 0
    return (
        RecommendationAgentSession.delete()
        .where(RecommendationAgentSession.user == user)
        .execute()
    )


def latest_recommended_artists_from_sessions(
    sessions: Sequence[Mapping[str, object]],
) -> Optional[Sequence[Mapping[str, object]]]:
    for session in reversed(list(sessions or [])):
        artists = session.get("recommendedArtists")
        if isinstance(artists, list) and artists:
            valid_artists = [
                artist
                for artist in artists
                if isinstance(artist, Mapping) and str(artist.get("name") or "").strip()
            ]
            if valid_artists:
                return valid_artists
    return None
