"""Quality gates for track recommendation metadata."""

from __future__ import annotations

from typing import Optional


MIN_HIGH_QUALITY_TRACK_METADATA_CONFIDENCE = 0.5


def is_llm_track_metadata(metadata: object) -> bool:
    if metadata is None:
        return False
    return (
        _normalized_label(getattr(metadata, "provider", None)) == "llm"
        or _normalized_label(getattr(metadata, "source", None)) == "llm"
    )


def has_semantic_track_metadata(metadata: object) -> bool:
    if metadata is None:
        return False
    if _metadata_list_values(metadata, "get_moods"):
        return True
    if _metadata_list_values(metadata, "get_scenes"):
        return True
    if _metadata_list_values(metadata, "get_tags"):
        return True
    return any(
        getattr(metadata, field_name, None) is not None
        for field_name in ("energy", "valence", "danceability")
    )


def is_high_quality_track_metadata(
    metadata: object,
    min_confidence: float = MIN_HIGH_QUALITY_TRACK_METADATA_CONFIDENCE,
) -> bool:
    if not is_llm_track_metadata(metadata):
        return False
    confidence = _metadata_confidence(metadata)
    if confidence is None or confidence < min_confidence:
        return False
    return has_semantic_track_metadata(metadata)


def should_review_track_metadata_confidence(
    metadata: object,
    confidence_threshold: float = MIN_HIGH_QUALITY_TRACK_METADATA_CONFIDENCE,
) -> bool:
    if not is_llm_track_metadata(metadata):
        return False
    confidence = _metadata_confidence(metadata)
    return confidence is None or confidence < confidence_threshold


def _normalized_label(value: object) -> str:
    return str(value or "").strip().casefold()


def _metadata_confidence(metadata: object) -> Optional[float]:
    try:
        value = getattr(metadata, "confidence", None)
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _metadata_list_values(metadata: object, method_name: str) -> list:
    method = getattr(metadata, method_name, None)
    if not callable(method):
        return []
    try:
        values = method()
    except (TypeError, ValueError):
        return []
    if not values:
        return []
    return [str(value).strip() for value in values if str(value).strip()]
