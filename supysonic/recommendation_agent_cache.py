import hashlib
import json

from datetime import timedelta
from typing import Dict, Mapping, Optional

from peewee import IntegrityError

from .db import RecommendationAgentCache, now

DEFAULT_AGENT_CACHE_TTL_SECONDS = 900


def build_recommendation_agent_context_hash(parts: Mapping[str, object]) -> str:
    canonical = json.dumps(
        dict(parts),
        default=str,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_cached_recommendation_agent_payload(
    user,
    context_hash: str,
) -> Optional[Dict[str, object]]:
    if user is None:
        return None
    cache_entry = (
        RecommendationAgentCache.select()
        .where(
            RecommendationAgentCache.user == user,
            RecommendationAgentCache.context_hash == context_hash,
            RecommendationAgentCache.expires_at > now(),
        )
        .first()
    )
    if cache_entry is None:
        return None
    try:
        payload = json.loads(cache_entry.payload_json)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def save_recommendation_agent_cache_payload(
    user,
    context_hash: str,
    message: str,
    language: str,
    model: str,
    payload: Mapping[str, object],
    ttl_seconds: int = DEFAULT_AGENT_CACHE_TTL_SECONDS,
) -> None:
    if user is None:
        return

    ttl_seconds = int(ttl_seconds or 0)
    if ttl_seconds <= 0:
        return

    current_time = now()
    values = {
        "user": user,
        "context_hash": context_hash,
        "message": str(message or ""),
        "language": str(language or "")[:8],
        "model": str(model or "")[:128],
        "payload_json": json.dumps(
            dict(payload),
            default=str,
            ensure_ascii=False,
            sort_keys=True,
        ),
        "updated_at": current_time,
        "expires_at": current_time + timedelta(seconds=ttl_seconds),
    }
    try:
        cache_entry, created = RecommendationAgentCache.get_or_create(
            user=user,
            context_hash=context_hash,
            defaults={
                **values,
                "created_at": current_time,
            },
        )
    except IntegrityError:
        cache_entry = (
            RecommendationAgentCache.select()
            .where(
                RecommendationAgentCache.user == user,
                RecommendationAgentCache.context_hash == context_hash,
            )
            .first()
        )
        if cache_entry is None:
            raise
        created = False

    if created:
        return
    for field, value in values.items():
        setattr(cache_entry, field, value)
    cache_entry.save()


def clear_recommendation_agent_cache(user) -> int:
    if user is None:
        return 0
    return (
        RecommendationAgentCache.delete()
        .where(RecommendationAgentCache.user == user)
        .execute()
    )
