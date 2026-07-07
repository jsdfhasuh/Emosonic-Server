"""Browse and filter tracks by recommendation metadata."""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from .db import Track, TrackMetadata
from .track_metadata_quality import (
    is_high_quality_track_metadata,
    is_llm_track_metadata,
)


DEFAULT_METADATA_FILTER_PAGE_SIZE = 50
MAX_METADATA_FILTER_PAGE_SIZE = 200
LANGUAGE_ALIASES = {
    "中文": "zh",
    "国语": "zh",
    "普通话": "zh",
    "粤语": "yue",
    "cantonese": "yue",
    "english": "en",
    "英文": "en",
    "日语": "ja",
    "韩语": "ko",
}


def filter_tracks_by_metadata(
    *,
    language: Optional[str] = None,
    moods: Optional[Iterable[object]] = None,
    scenes: Optional[Iterable[object]] = None,
    tags: Optional[Iterable[object]] = None,
    energy_min: Optional[int] = None,
    energy_max: Optional[int] = None,
    valence_min: Optional[int] = None,
    valence_max: Optional[int] = None,
    danceability_min: Optional[int] = None,
    danceability_max: Optional[int] = None,
    confidence_min: Optional[float] = None,
    confidence_max: Optional[float] = None,
    provider: Optional[str] = None,
    include_local: bool = False,
    include_low_confidence: bool = False,
    page: int = 1,
    page_size: int = DEFAULT_METADATA_FILTER_PAGE_SIZE,
) -> Dict[str, object]:
    page = max(1, _safe_int(page, 1))
    page_size = min(
        MAX_METADATA_FILTER_PAGE_SIZE,
        max(1, _safe_int(page_size, DEFAULT_METADATA_FILTER_PAGE_SIZE)),
    )
    filters = {
        "language": _normalize_language(language),
        "moods": _normalized_filter_terms(moods),
        "scenes": _normalized_filter_terms(scenes),
        "tags": _normalized_filter_terms(tags),
        "energy": (energy_min, energy_max),
        "valence": (valence_min, valence_max),
        "danceability": (danceability_min, danceability_max),
        "confidence": (confidence_min, confidence_max),
        "provider": _normalized_text(provider),
        "include_local": bool(include_local),
        "include_low_confidence": bool(include_low_confidence),
    }

    matched_items = []
    for metadata in TrackMetadata.select(TrackMetadata, Track).join(Track):
        if not _metadata_visible(metadata, filters):
            continue
        if not _metadata_matches_filters(metadata, filters):
            continue
        matched_items.append({"track": metadata.track, "metadata": metadata})

    matched_items.sort(key=_metadata_filter_sort_key)
    total = len(matched_items)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "items": matched_items[start:end],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


def _metadata_visible(metadata: TrackMetadata, filters: Dict[str, object]) -> bool:
    provider = filters["provider"]
    metadata_provider = _normalized_text(metadata.provider)
    metadata_source = _normalized_text(metadata.source)
    if provider:
        if provider not in (metadata_provider, metadata_source):
            return False
        if provider == "local":
            return True
        if provider == "llm" or is_llm_track_metadata(metadata):
            return bool(
                filters["include_low_confidence"]
                or is_high_quality_track_metadata(metadata)
            )
        return bool(
            filters["include_low_confidence"]
            or is_high_quality_track_metadata(metadata)
        )

    if is_high_quality_track_metadata(metadata):
        return True
    if filters["include_local"] and "local" in (metadata_provider, metadata_source):
        return True
    if filters["include_low_confidence"] and is_llm_track_metadata(metadata):
        return True
    return False


def _metadata_matches_filters(
    metadata: TrackMetadata,
    filters: Dict[str, object],
) -> bool:
    language = filters["language"]
    if language and _normalize_language(metadata.language) != language:
        return False
    if not _list_matches_all(metadata.get_moods(), filters["moods"]):
        return False
    if not _list_matches_all(metadata.get_scenes(), filters["scenes"]):
        return False
    if not _list_matches_all(metadata.get_tags(), filters["tags"]):
        return False
    if not _value_in_range(metadata.energy, filters["energy"]):
        return False
    if not _value_in_range(metadata.valence, filters["valence"]):
        return False
    if not _value_in_range(metadata.danceability, filters["danceability"]):
        return False
    if not _value_in_range(metadata.confidence, filters["confidence"]):
        return False
    return True


def _metadata_filter_sort_key(item: Dict[str, object]) -> tuple:
    track = item["track"]
    metadata = item["metadata"]
    return (
        str(getattr(track, "title", "") or "").casefold(),
        str(getattr(metadata, "updated_at", "") or ""),
        str(getattr(track, "id", "")),
    )


def _list_matches_all(values: Iterable[object], required_terms: List[str]) -> bool:
    if not required_terms:
        return True
    normalized_values = [_normalized_text(value) for value in values]
    for term in required_terms:
        if not any(term in value for value in normalized_values):
            return False
    return True


def _value_in_range(value: object, value_range: tuple) -> bool:
    low, high = value_range
    if low is None and high is None:
        return True
    if value is None:
        return False
    numeric_value = float(value)
    if low is not None and numeric_value < float(low):
        return False
    if high is not None and numeric_value > float(high):
        return False
    return True


def _normalized_filter_terms(values: Optional[Iterable[object]]) -> List[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    return [
        normalized
        for normalized in (_normalized_text(value) for value in values)
        if normalized
    ]


def _normalize_language(value: object) -> str:
    normalized = _normalized_text(value)
    return LANGUAGE_ALIASES.get(normalized, normalized)


def _safe_int(value: object, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _normalized_text(value: object) -> str:
    return str(value or "").strip().casefold()
