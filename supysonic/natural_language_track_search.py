"""Rule-based natural language search over track metadata."""

from __future__ import annotations

import unicodedata
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from .db import Track
from .mood_scene_playlists import get_mood_scene_playlist
from .track_metadata_filter import filter_tracks_by_metadata


DEFAULT_NATURAL_LANGUAGE_SEARCH_LIMIT = 20
MAX_NATURAL_LANGUAGE_SEARCH_LIMIT = 100

NATURAL_LANGUAGE_SEARCH_RULES = (
    {
        "key": "night",
        "terms": ("晚上", "夜晚", "深夜", "night", "late night"),
        "filters": {"scenes": ["深夜"]},
        "scene_keys": ("night",),
        "description": "scene: 深夜 / 夜晚聆听",
    },
    {
        "key": "quiet",
        "terms": ("安静", "静一点", "quiet", "calm"),
        "filters": {"moods": ["平静"], "energy_max": 45},
        "scene_keys": ("low_energy", "relax"),
        "description": "mood: 平静, energy <= 45",
    },
    {
        "key": "high_energy",
        "terms": ("燃", "高能量", "热血", "energetic", "workout"),
        "filters": {"energy_min": 70},
        "scene_keys": ("high_energy",),
        "description": "energy >= 70",
    },
    {
        "key": "cantonese",
        "terms": ("粤语", "cantonese", "cantopop"),
        "filters": {"language": "yue"},
        "scene_keys": ("cantonese",),
        "description": "language: yue",
    },
    {
        "key": "coding",
        "terms": ("写代码", "编程", "coding", "code"),
        "filters": {"scenes": ["专注"]},
        "scene_keys": ("study",),
        "description": "scene: 专注 / 学习",
    },
    {
        "key": "emo",
        "terms": ("emo", "忧郁", "丧", "难过"),
        "filters": {"moods": ["感伤"]},
        "scene_keys": ("emo",),
        "description": "mood: 感伤 / 忧郁",
    },
    {
        "key": "nostalgic",
        "terms": ("怀旧", "经典", "nostalgic", "classic"),
        "filters": {"tags": ["怀旧"]},
        "scene_keys": ("nostalgic",),
        "description": "tags: 怀旧 / 经典",
    },
)


def search_tracks_by_natural_language(
    query: str,
    limit: int = DEFAULT_NATURAL_LANGUAGE_SEARCH_LIMIT,
    page: int = 1,
    user: Optional[object] = None,
) -> Dict[str, object]:
    normalized_limit = min(
        MAX_NATURAL_LANGUAGE_SEARCH_LIMIT,
        max(1, _safe_int(limit, DEFAULT_NATURAL_LANGUAGE_SEARCH_LIMIT)),
    )
    parsed = parse_natural_language_track_query(query)
    filter_kwargs = _filter_kwargs(parsed)

    exact_page = {"items": [], "total": 0, "page": page, "page_size": normalized_limit, "pages": 0}
    if parsed["matchedRules"]:
        exact_page = filter_tracks_by_metadata(
            **filter_kwargs,
            page=page,
            page_size=normalized_limit,
        )
    exact_items = _metadata_page_items(exact_page.get("items", []), parsed)
    if exact_items or exact_page.get("total", 0) > 0:
        return {
            "query": query,
            "filters": parsed["filters"],
            "matchedRules": parsed["matchedRules"],
            "items": exact_items,
            "total": exact_page["total"],
            "page": exact_page["page"],
            "page_size": exact_page["page_size"],
            "pages": exact_page["pages"],
            "fallback": False,
        }

    fallback_items = _fallback_items(parsed, user, normalized_limit)
    return {
        "query": query,
        "filters": parsed["filters"],
        "matchedRules": parsed["matchedRules"],
        "items": fallback_items,
        "total": len(fallback_items),
        "page": 1,
        "page_size": normalized_limit,
        "pages": 1 if fallback_items else 0,
        "fallback": True,
    }


def parse_natural_language_track_query(query: str) -> Dict[str, object]:
    normalized_query = _normalized_query(query)
    filters = {
        "language": None,
        "moods": [],
        "scenes": [],
        "tags": [],
        "energy_min": None,
        "energy_max": None,
    }
    matched_rules = []
    scene_keys = []

    for rule in NATURAL_LANGUAGE_SEARCH_RULES:
        if not _query_matches_rule(normalized_query, rule):
            continue
        _merge_filters(filters, rule["filters"])
        matched_rules.append(
            {
                "key": rule["key"],
                "description": rule["description"],
            }
        )
        for scene_key in rule.get("scene_keys", ()):
            if scene_key not in scene_keys:
                scene_keys.append(scene_key)

    return {
        "query": query,
        "normalizedQuery": normalized_query,
        "filters": filters,
        "matchedRules": matched_rules,
        "fallbackSceneKeys": scene_keys,
    }


def _filter_kwargs(parsed: Mapping[str, object]) -> Dict[str, object]:
    filters = parsed.get("filters") or {}
    return {
        "language": filters.get("language"),
        "moods": filters.get("moods") or None,
        "scenes": filters.get("scenes") or None,
        "tags": filters.get("tags") or None,
        "energy_min": filters.get("energy_min"),
        "energy_max": filters.get("energy_max"),
    }


def _merge_filters(filters: Dict[str, object], rule_filters: Mapping[str, object]) -> None:
    for key in ("moods", "scenes", "tags"):
        values = rule_filters.get(key)
        if not values:
            continue
        for value in values:
            if value not in filters[key]:
                filters[key].append(value)

    if rule_filters.get("language"):
        filters["language"] = rule_filters["language"]
    for key in ("energy_min", "energy_max"):
        if rule_filters.get(key) is None:
            continue
        current_value = filters.get(key)
        new_value = int(rule_filters[key])
        if current_value is None:
            filters[key] = new_value
        elif key.endswith("_min"):
            filters[key] = max(int(current_value), new_value)
        else:
            filters[key] = min(int(current_value), new_value)


def _metadata_page_items(
    page_items: Sequence[Mapping[str, object]],
    parsed: Mapping[str, object],
) -> List[Dict[str, object]]:
    items = []
    for item in page_items:
        track = item.get("track")
        metadata = item.get("metadata")
        if track is None or metadata is None:
            continue
        items.append(
            {
                "track": track,
                "metadata": metadata,
                "reasons": _metadata_match_reasons(metadata, parsed),
            }
        )
    return items


def _metadata_match_reasons(metadata: object, parsed: Mapping[str, object]) -> List[str]:
    filters = parsed.get("filters") or {}
    reasons = []
    language = filters.get("language")
    if language and _normalized_text(getattr(metadata, "language", None)) == language:
        reasons.append(f"language: {language}")

    for label, getter_name, values in (
        ("mood", "get_moods", filters.get("moods") or []),
        ("scene", "get_scenes", filters.get("scenes") or []),
        ("tags", "get_tags", filters.get("tags") or []),
    ):
        metadata_values = getattr(metadata, getter_name)()
        for value in values:
            if _list_contains(metadata_values, value):
                reasons.append(f"{label}: {value}")

    energy = getattr(metadata, "energy", None)
    if energy is not None:
        energy_min = filters.get("energy_min")
        energy_max = filters.get("energy_max")
        if energy_min is not None and float(energy) >= float(energy_min):
            reasons.append(f"energy: {energy} >= {energy_min}")
        if energy_max is not None and float(energy) <= float(energy_max):
            reasons.append(f"energy: {energy} <= {energy_max}")

    return reasons or ["metadata match"]


def _fallback_items(
    parsed: Mapping[str, object],
    user: Optional[object],
    limit: int,
) -> List[Dict[str, object]]:
    items = []
    seen_track_ids = set()
    for scene_key in parsed.get("fallbackSceneKeys") or []:
        for result in get_mood_scene_playlist(scene_key, limit=limit, user=user):
            track = result.get("track")
            if track is None or track.id in seen_track_ids:
                continue
            seen_track_ids.add(track.id)
            items.append(
                {
                    "track": track,
                    "metadata": None,
                    "reasons": _fallback_reasons(result.get("reasons")),
                }
            )
            if len(items) >= limit:
                return items

    if items:
        return items

    for item in filter_tracks_by_metadata(page_size=limit)["items"]:
        track = item["track"]
        if track.id in seen_track_ids:
            continue
        seen_track_ids.add(track.id)
        items.append(
            {
                "track": track,
                "metadata": item.get("metadata"),
                "reasons": ["near match: broad metadata recommendation"],
            }
        )
        if len(items) >= limit:
            return items

    for track in Track.select().order_by(
        Track.play_count.desc(),
        Track.title.asc(),
        Track.id.asc(),
    ):
        if track.id in seen_track_ids:
            continue
        seen_track_ids.add(track.id)
        items.append(
            {
                "track": track,
                "metadata": None,
                "reasons": ["near match: library fallback"],
            }
        )
        if len(items) >= limit:
            break
    return items


def _fallback_reasons(reasons: object) -> List[str]:
    if isinstance(reasons, str):
        reasons = [reasons]
    return [
        "near match: " + str(reason)
        for reason in (reasons or [])
        if str(reason or "").strip()
    ] or ["near match: scene playlist fallback"]


def _query_matches_rule(query: str, rule: Mapping[str, object]) -> bool:
    return any(_normalized_query(term) in query for term in rule.get("terms", ()))


def _list_contains(values: Iterable[object], term: object) -> bool:
    normalized_term = _normalized_text(term)
    return any(normalized_term in _normalized_text(value) for value in values)


def _safe_int(value: object, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _normalized_query(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.strip().casefold().split())


def _normalized_text(value: object) -> str:
    return str(value or "").strip().casefold()
