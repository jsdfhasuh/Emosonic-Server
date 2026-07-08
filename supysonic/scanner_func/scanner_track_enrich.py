"""Track-level recommendation metadata enrichment helpers."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import timedelta
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from peewee import IntegrityError

from ..db import (
    Track,
    TrackMetadata,
    TrackMetadataEnrichmentTask,
    db,
    now,
)
from ..llm_client import (
    LlmClientError,
    LlmInvalidResponseError,
    LlmQuotaError,
    build_chat_completion_payload,
    parse_json_object_response,
    post_chat_completion,
)
from ..logging_utils import format_log_event
from .scanner_review_tasks import createLowConfidenceTrackMetadataReviewTask


logger = logging.getLogger(__name__)

DEFAULT_ENRICHMENT_LIMIT = 20
DEFAULT_STALE_LOCK_SECONDS = 900
DEFAULT_RETRY_DELAY_SECONDS = 300
MIN_ENRICHMENT_SCAN_BATCH_SIZE = 100
DEFAULT_LLM_TIMEOUT_SECONDS = 20
DEFAULT_LLM_MAX_OUTPUT_TOKENS = 900
DEFAULT_LLM_TEMPERATURE = 0.2
SAFE_PATH_TOKEN_RE = re.compile(r"[^A-Za-z0-9._@ -]+")
CJK_RE = re.compile(r"[\u3400-\u9fff]")
LANGUAGE_TAG_KEYS = {
    "arabic",
    "cantonese",
    "chinese",
    "english",
    "french",
    "german",
    "japanese",
    "korean",
    "mandarin",
    "spanish",
    "yue",
    "zh",
    "粤语",
    "粵語",
    "广东话",
    "廣東話",
    "华语",
    "華語",
    "国语",
    "國語",
    "普通话",
    "普通話",
    "中文",
    "英语",
    "英語",
}
LANGUAGE_ALIASES = {
    "arabic": "ar",
    "cantonese": "yue",
    "chinese": "zh",
    "deutsch": "de",
    "english": "en",
    "french": "fr",
    "german": "de",
    "guangdonghua": "yue",
    "guoyu": "zh",
    "italian": "it",
    "japanese": "ja",
    "korean": "ko",
    "mandarin": "zh",
    "mandarin chinese": "zh",
    "portuguese": "pt",
    "putonghua": "zh",
    "spanish": "es",
    "unknown": None,
    "yue": "yue",
    "zh": "zh",
    "zh cn": "zh",
    "zh hans": "zh",
    "zh hant": "zh",
    "zh hk": "yue",
    "zh tw": "zh",
    "粤语": "yue",
    "粵語": "yue",
    "广东话": "yue",
    "廣東話": "yue",
    "华语": "zh",
    "華語": "zh",
    "国语": "zh",
    "國語": "zh",
    "普通话": "zh",
    "普通話": "zh",
    "中文": "zh",
    "日语": "ja",
    "日語": "ja",
    "韩语": "ko",
    "韓語": "ko",
    "英语": "en",
    "英語": "en",
}
LANGUAGE_CODES = {
    "ar",
    "de",
    "en",
    "es",
    "fr",
    "hi",
    "id",
    "it",
    "ja",
    "ko",
    "pt",
    "ru",
    "th",
    "vi",
    "yue",
    "zh",
}
LLM_LABEL_ALIASES = {
    "aggressive": "激烈",
    "alone": "独处",
    "alone time": "独处",
    "alt pop": "另类流行",
    "alternative": "另类",
    "ballad": "抒情",
    "bittersweet": "苦乐参半",
    "bright": "明亮",
    "calm": "平静",
    "cantopop": "粤语流行",
    "classic hit": "经典金曲",
    "classic mandopop": "经典华语流行",
    "commute": "通勤",
    "commuting": "通勤",
    "contemplation": "沉思",
    "dance": "舞曲",
    "dream pop": "梦幻流行",
    "dreamy": "梦幻",
    "driving": "驾驶",
    "driving alone": "独自驾驶",
    "emotional": "深情",
    "emotional vocals": "深情演唱",
    "family": "亲情",
    "focused": "专注",
    "heartbreak": "失恋",
    "introspective": "内省",
    "jazz": "爵士",
    "late night": "深夜",
    "late night listening": "深夜聆听",
    "long drive": "长途驾驶",
    "longing": "思念",
    "love": "爱情",
    "love ballad": "情歌",
    "mandopop": "华语流行",
    "melancholic": "忧郁",
    "nostalgic": "怀旧",
    "night listening": "夜晚聆听",
    "poetic": "诗意",
    "pop": "流行",
    "quiet moment": "安静时刻",
    "quiet moments": "安静时刻",
    "reflecting on past relationships": "回望旧情",
    "reflecting on the past": "回忆往事",
    "reflective": "沉思",
    "relaxed": "放松",
    "reunion": "重逢",
    "romantic": "浪漫",
    "sentimental": "感伤",
    "urban life": "都市生活",
    "workout": "运动",
}


class TrackMetadataProviderError(Exception):
    def __init__(
        self,
        message: str,
        details: Optional[Mapping[str, object]] = None,
    ) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class TrackMetadataInvalidResponseError(TrackMetadataProviderError):
    pass


class TrackMetadataQuotaError(TrackMetadataProviderError):
    pass


def _formatProviderError(message: str, details: Mapping[str, object]) -> str:
    text = str(message or "LLM request failed.").strip()
    detail_parts = []
    status = details.get("upstreamStatus")
    code = str(details.get("upstreamErrorCode") or "").strip()
    upstream_message = str(details.get("upstreamMessage") or "").strip()
    if status is not None:
        detail_parts.append(f"status={status}")
    if code:
        detail_parts.append(f"code={code}")
    if detail_parts:
        text = f"{text} ({', '.join(detail_parts)})"
    if upstream_message:
        text = f"{text}: {upstream_message}"
    return text[:500]


class LocalMetadataProvider:
    name = "local"
    model = None
    source = "local"

    def enrich(self, track_input: Mapping[str, object]) -> Dict[str, object]:
        title = str(track_input.get("title") or "").strip()
        artist = str(track_input.get("artist") or "").strip()
        album = str(track_input.get("album") or "").strip()
        genre = str(track_input.get("genre") or "").strip()
        year = track_input.get("year")

        tags = []
        if genre:
            tags.append(genre)
        if year:
            tags.append(str(year))

        summary_parts = []
        if title:
            summary_parts.append(title)
        if artist:
            summary_parts.append(f"by {artist}")
        if album:
            summary_parts.append(f"from {album}")

        return {
            "language": None,
            "mood": [],
            "scene": [],
            "tags": tags,
            "summary": " ".join(summary_parts) or None,
            "energy": None,
            "valence": None,
            "danceability": None,
            "confidence": 0.25 if tags else 0.1,
            "provider": self.name,
            "model": self.model,
            "source": self.source,
            "raw": {
                "provider": self.name,
                "tags": tags,
                "path_hints": track_input.get("path_hints") or [],
            },
        }


class LLMMetadataProvider:
    name = "llm"
    source = "llm"

    def __init__(self, config: Mapping[str, object]) -> None:
        self.config = dict(config or {})
        self.api_base_url = str(self.config.get("api_base_url") or "").strip()
        self.api_key = str(self.config.get("api_key") or "").strip()
        self.model = str(self.config.get("model") or "").strip()
        if not self.api_base_url or not self.api_key or not self.model:
            raise TrackMetadataProviderError(
                "LLM metadata provider requires recommendation_agent.api_base_url, "
                "api_key, and model."
            )
        self.timeout_seconds = _positiveFloat(
            self.config.get("timeout_seconds"),
            DEFAULT_LLM_TIMEOUT_SECONDS,
        )
        self.max_output_tokens = _nonNegativeInt(
            self.config.get("max_output_tokens"),
            DEFAULT_LLM_MAX_OUTPUT_TOKENS,
        )
        self.temperature = _nonNegativeFloat(
            self.config.get("temperature"),
            DEFAULT_LLM_TEMPERATURE,
        )

    def enrich(self, track_input: Mapping[str, object]) -> Dict[str, object]:
        messages = [
            {
                "role": "system",
                "content": (
                    "You enrich local music tracks with recommendation semantics. "
                    "Return strict JSON only. Do not invent factual metadata."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "Infer recommendation metadata for this local track.",
                        "allowed_fields": [
                            "language",
                            "mood",
                            "scene",
                            "tags",
                            "summary",
                            "energy",
                            "valence",
                            "danceability",
                            "confidence",
                        ],
                        "track": dict(track_input),
                        "constraints": {
                            "language": (
                                "null or one short code: zh for Mandarin/Chinese, "
                                "yue for Cantonese, en, ja, ko, fr, es, de, or other"
                            ),
                            "mood": "1-5 short Simplified Chinese emotion labels",
                            "scene": "1-5 short Simplified Chinese listening contexts",
                            "tags": (
                                "1-8 short Simplified Chinese semantic style/theme "
                                "tags; do not include artist names, track titles, "
                                "album titles, years, or language-only labels"
                            ),
                            "summary": (
                                "one concise Simplified Chinese sentence, no more "
                                "than 80 Chinese characters"
                            ),
                            "energy": "integer 0-100 or null",
                            "valence": "integer 0-100 or null",
                            "danceability": "integer 0-100 or null",
                            "confidence": "number 0.0-1.0",
                            "format": "Return strict JSON only. No Markdown.",
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
        ]
        payload = build_chat_completion_payload(
            self.model,
            messages,
            max_output_tokens=self.max_output_tokens,
            temperature=self.temperature,
            json_object=True,
        )
        try:
            response_json = post_chat_completion(
                self.api_base_url,
                self.api_key,
                payload,
                timeout_seconds=self.timeout_seconds,
            )
            parsed = parse_json_object_response(response_json)
        except LlmInvalidResponseError as exc:
            raise TrackMetadataInvalidResponseError(str(exc)) from exc
        except LlmQuotaError as exc:
            raise TrackMetadataQuotaError(
                _formatProviderError("LLM quota exhausted.", exc.details),
                details=exc.details,
            ) from exc
        except LlmClientError as exc:
            raise TrackMetadataProviderError(
                _formatProviderError(str(exc), exc.details),
                details=exc.details,
            ) from exc

        parsed["provider"] = self.name
        parsed["model"] = self.model
        parsed["source"] = self.source
        parsed["raw"] = parsed.copy()
        return parsed


def collectTracksNeedingEnrichment(
    limit: int = DEFAULT_ENRICHMENT_LIMIT,
    force: bool = False,
    track_ids: Optional[Sequence[object]] = None,
    failed_only: bool = False,
    stale_lock_seconds: int = DEFAULT_STALE_LOCK_SECONDS,
    include_reasons: bool = False,
):
    if limit <= 0:
        return []

    current_time = now()
    _recoverStaleTrackMetadataTasks(current_time, stale_lock_seconds)

    track_query = Track.select().order_by(Track.created, Track.id)
    if track_ids:
        track_query = track_query.where(Track.id.in_(list(track_ids)))

    candidates = []
    offset = 0
    batch_size = max(limit * 10, MIN_ENRICHMENT_SCAN_BATCH_SIZE)
    while len(candidates) < limit:
        tracks = list(track_query.limit(batch_size).offset(offset))
        if not tracks:
            break
        offset += len(tracks)

        page_track_ids = [track.id for track in tracks]
        metadata_by_track = _loadTrackMetadataByTrack(page_track_ids)
        task_by_track = _loadTrackMetadataTaskByTrack(page_track_ids)

        for track in tracks:
            metadata = metadata_by_track.get(track.id)
            task = task_by_track.get(track.id)
            reason = _getCandidateReason(
                track,
                metadata,
                task,
                force=force,
                failed_only=failed_only,
                current_time=current_time,
            )
            if not reason:
                continue
            candidates.append((track, reason) if include_reasons else track)
            if len(candidates) >= limit:
                break

        if len(tracks) < batch_size:
            break

    return candidates


def countTracksNeedingEnrichment(
    force: bool = False,
    track_ids: Optional[Sequence[object]] = None,
    failed_only: bool = False,
    stale_lock_seconds: int = DEFAULT_STALE_LOCK_SECONDS,
) -> int:
    current_time = now()
    _recoverStaleTrackMetadataTasks(current_time, stale_lock_seconds)

    track_query = Track.select().order_by(Track.created, Track.id)
    if track_ids:
        track_query = track_query.where(Track.id.in_(list(track_ids)))

    count = 0
    offset = 0
    batch_size = MIN_ENRICHMENT_SCAN_BATCH_SIZE
    while True:
        tracks = list(track_query.limit(batch_size).offset(offset))
        if not tracks:
            break
        offset += len(tracks)

        page_track_ids = [track.id for track in tracks]
        metadata_by_track = _loadTrackMetadataByTrack(page_track_ids)
        task_by_track = _loadTrackMetadataTaskByTrack(page_track_ids)

        for track in tracks:
            if _getCandidateReason(
                track,
                metadata_by_track.get(track.id),
                task_by_track.get(track.id),
                force=force,
                failed_only=failed_only,
                current_time=current_time,
            ):
                count += 1

        if len(tracks) < batch_size:
            break

    return count


def countTracksMissingCurrentMetadata(
    track_ids: Optional[Sequence[object]] = None,
) -> int:
    track_query = Track.select().order_by(Track.created, Track.id)
    if track_ids:
        track_query = track_query.where(Track.id.in_(list(track_ids)))

    count = 0
    offset = 0
    batch_size = MIN_ENRICHMENT_SCAN_BATCH_SIZE
    while True:
        tracks = list(track_query.limit(batch_size).offset(offset))
        if not tracks:
            break
        offset += len(tracks)

        metadata_by_track = _loadTrackMetadataByTrack([track.id for track in tracks])
        for track in tracks:
            metadata = metadata_by_track.get(track.id)
            if (
                metadata is None
                or metadata.track_last_modification != track.last_modification
            ):
                count += 1

        if len(tracks) < batch_size:
            break

    return count


def _loadTrackMetadataByTrack(
    track_ids: Sequence[object],
) -> Dict[object, TrackMetadata]:
    if not track_ids:
        return {}
    return {
        metadata.track_id: metadata
        for metadata in TrackMetadata.select().where(TrackMetadata.track.in_(track_ids))
    }


def _loadTrackMetadataTaskByTrack(
    track_ids: Sequence[object],
) -> Dict[object, TrackMetadataEnrichmentTask]:
    if not track_ids:
        return {}
    return {
        task.track_id: task
        for task in TrackMetadataEnrichmentTask.select().where(
            TrackMetadataEnrichmentTask.track.in_(track_ids)
        )
    }


def runTrackMetadataEnrichmentPass(
    limit: int = DEFAULT_ENRICHMENT_LIMIT,
    force: bool = False,
    track_ids: Optional[Sequence[object]] = None,
    provider: Optional[object] = None,
    failed_only: bool = False,
    dry_run: bool = False,
    stale_lock_seconds: int = DEFAULT_STALE_LOCK_SECONDS,
    include_path_hints: bool = False,
    log_payload: bool = False,
) -> Dict[str, object]:
    provider = provider or LocalMetadataProvider()
    provider_name = getattr(provider, "name", provider.__class__.__name__)
    candidates = collectTracksNeedingEnrichment(
        limit=limit,
        force=force,
        track_ids=track_ids,
        failed_only=failed_only,
        stale_lock_seconds=stale_lock_seconds,
        include_reasons=True,
    )
    pending_total = countTracksNeedingEnrichment(
        force=force,
        track_ids=track_ids,
        failed_only=failed_only,
        stale_lock_seconds=stale_lock_seconds,
    )
    unenriched_total = countTracksMissingCurrentMetadata(track_ids=track_ids)
    summary = {
        "selected": len(candidates),
        "pending": pending_total,
        "remaining": pending_total,
        "unenriched": unenriched_total,
        "enriched": 0,
        "failed": 0,
        "skipped": 0,
        "dry_run": dry_run,
        "provider": provider_name,
        "tracks": [],
        "quota_exhausted": False,
        "rate_limited": False,
        "error": None,
    }

    logger.info(
        format_log_event(
            "track_metadata_enrichment",
            "track_metadata_enrichment_pass_start",
            selected=len(candidates),
            pending=pending_total,
            unenriched=unenriched_total,
            dry_run=dry_run,
            provider=provider_name,
        )
    )

    for track, reason in candidates:
        track_info = {
            "id": str(track.id),
            "title": track.title,
            "reason": reason,
            "status": "selected" if dry_run else "pending",
        }
        if dry_run:
            summary["tracks"].append(track_info)
            continue

        task = _claimTrackMetadataEnrichmentTask(track, reason, force=force)
        if task is None:
            track_info["status"] = "skipped"
            summary["skipped"] += 1
            summary["tracks"].append(track_info)
            continue

        stop_after_current = False
        try:
            track_input = buildTrackMetadataInput(
                track,
                include_path_hints=include_path_hints,
            )
            enrichment = provider.enrich(track_input)
            metadata = applyTrackMetadataEnrichment(track, enrichment)
            createLowConfidenceTrackMetadataReviewTask(track, metadata)
            recordTrackEnrichmentAttempt(
                track,
                provider,
                TrackMetadataEnrichmentTask.STATUS_COMPLETED,
                reason=reason,
            )
            track_info["status"] = "completed"
            summary["enriched"] += 1
            log_fields = {
                "track_id": str(track.id),
                "provider": provider_name,
            }
            if log_payload:
                log_fields.update(_metadataLogPayload(metadata))
            logger.info(
                format_log_event(
                    "track_metadata_enrichment",
                    "track_metadata_enrichment_applied",
                    **log_fields,
                )
            )
        except TrackMetadataInvalidResponseError as exc:
            recordTrackEnrichmentAttempt(
                track,
                provider,
                TrackMetadataEnrichmentTask.STATUS_FAILED,
                reason=TrackMetadataEnrichmentTask.REASON_INVALID_RESPONSE,
                error=str(exc),
            )
            track_info["status"] = "failed"
            track_info["reason"] = TrackMetadataEnrichmentTask.REASON_INVALID_RESPONSE
            track_info["error"] = str(exc)
            summary["failed"] += 1
        except TrackMetadataQuotaError as exc:
            recordTrackEnrichmentAttempt(
                track,
                provider,
                TrackMetadataEnrichmentTask.STATUS_FAILED,
                reason=TrackMetadataEnrichmentTask.REASON_PROVIDER_QUOTA,
                error=str(exc),
            )
            track_info["status"] = "failed"
            track_info["reason"] = TrackMetadataEnrichmentTask.REASON_PROVIDER_QUOTA
            track_info["error"] = str(exc)
            summary["failed"] += 1
            summary["quota_exhausted"] = True
            summary["error"] = str(exc)
            stop_after_current = True
            logger.error(
                format_log_event(
                    "track_metadata_enrichment",
                    "track_metadata_enrichment_quota_exhausted",
                    track_id=str(track.id),
                    provider=provider_name,
                )
            )
        except TrackMetadataProviderError as exc:
            retry_delay_seconds = _providerRetryDelaySeconds(exc)
            recordTrackEnrichmentAttempt(
                track,
                provider,
                TrackMetadataEnrichmentTask.STATUS_RETRY,
                reason=TrackMetadataEnrichmentTask.REASON_PROVIDER_ERROR,
                error=str(exc),
                next_retry_at=now() + timedelta(seconds=retry_delay_seconds),
            )
            track_info["status"] = "retry"
            track_info["reason"] = TrackMetadataEnrichmentTask.REASON_PROVIDER_ERROR
            track_info["error"] = str(exc)
            summary["failed"] += 1
            if _isRateLimitedProviderError(exc):
                summary["rate_limited"] = True
                summary["error"] = str(exc)
                stop_after_current = True
                logger.warning(
                    format_log_event(
                        "track_metadata_enrichment",
                        "track_metadata_enrichment_rate_limited",
                        track_id=str(track.id),
                        provider=provider_name,
                        retry_after_seconds=retry_delay_seconds,
                    )
                )
            else:
                logger.warning(
                    format_log_event(
                        "track_metadata_enrichment",
                        "track_metadata_enrichment_provider_retry",
                        track_id=str(track.id),
                        provider=provider_name,
                        error_type=exc.__class__.__name__,
                        retry_after_seconds=retry_delay_seconds,
                    )
                )
        except Exception as exc:
            recordTrackEnrichmentAttempt(
                track,
                provider,
                TrackMetadataEnrichmentTask.STATUS_RETRY,
                reason=TrackMetadataEnrichmentTask.REASON_PROVIDER_ERROR,
                error=str(exc),
                next_retry_at=now() + timedelta(seconds=DEFAULT_RETRY_DELAY_SECONDS),
            )
            track_info["status"] = "retry"
            track_info["error"] = str(exc)
            summary["failed"] += 1
            logger.exception(
                format_log_event(
                    "track_metadata_enrichment",
                    "track_metadata_enrichment_track_failed",
                    track_id=str(track.id),
                    provider=provider_name,
                    error_type=exc.__class__.__name__,
                )
            )
        summary["tracks"].append(track_info)
        if stop_after_current:
            break

    summary["remaining"] = countTracksNeedingEnrichment(
        force=force,
        track_ids=track_ids,
        failed_only=failed_only,
        stale_lock_seconds=stale_lock_seconds,
    )
    summary["unenriched"] = countTracksMissingCurrentMetadata(track_ids=track_ids)
    logger.info(
        format_log_event(
            "track_metadata_enrichment",
            "track_metadata_enrichment_pass_end",
            selected=summary["selected"],
            pending=summary["pending"],
            remaining=summary["remaining"],
            unenriched=summary["unenriched"],
            enriched=summary["enriched"],
            failed=summary["failed"],
            skipped=summary["skipped"],
            dry_run=dry_run,
            provider=provider_name,
        )
    )
    return summary


def _isRateLimitedProviderError(exc: TrackMetadataProviderError) -> bool:
    return bool(exc.details.get("rateLimited")) or exc.details.get("upstreamStatus") == 429


def _providerRetryDelaySeconds(exc: TrackMetadataProviderError) -> int:
    try:
        retry_after = int(float(exc.details.get("retryAfterSeconds")))
    except (TypeError, ValueError):
        retry_after = DEFAULT_RETRY_DELAY_SECONDS
    return retry_after if retry_after > 0 else DEFAULT_RETRY_DELAY_SECONDS


def _metadataLogPayload(metadata: TrackMetadata) -> Dict[str, object]:
    return {
        "language": metadata.language,
        "moods": metadata.get_moods(),
        "scenes": metadata.get_scenes(),
        "tags": metadata.get_tags(),
        "summary": _truncateLogText(metadata.summary),
        "energy": metadata.energy,
        "valence": metadata.valence,
        "danceability": metadata.danceability,
        "confidence": metadata.confidence,
        "model": metadata.model,
    }


def _truncateLogText(value: Optional[object], max_length: int = 240) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


def buildTrackMetadataInput(
    track: Track,
    include_path_hints: bool = False,
) -> Dict[str, object]:
    artist_name = ""
    if track.artist:
        artist_name = track.artist.get_artist_name() or track.artist.name or ""

    album_name = track.album.name if track.album else ""
    payload = {
        "track_id": str(track.id),
        "title": track.title,
        "artist": artist_name,
        "album": album_name,
        "genre": track.genre,
        "year": track.year,
        "file_name": os.path.basename(track.path or ""),
    }
    if include_path_hints:
        payload["path_hints"] = _buildSafePathHints(track.path or "")
    return payload


def applyTrackMetadataEnrichment(track: Track, enrichment: Mapping[str, object]) -> TrackMetadata:
    clean_enrichment = _validateEnrichment(enrichment)
    clean_enrichment["tags"] = _filterSemanticTags(clean_enrichment["tags"], track)
    metadata = TrackMetadata.get_or_none(TrackMetadata.track == track)
    is_new = metadata is None
    if metadata is None:
        metadata = TrackMetadata(track=track, track_last_modification=track.last_modification)

    metadata.track_last_modification = track.last_modification
    metadata.language = clean_enrichment["language"]
    metadata.mood_json = _jsonList(clean_enrichment["mood"])
    metadata.scene_json = _jsonList(clean_enrichment["scene"])
    metadata.tags_json = _jsonList(clean_enrichment["tags"])
    metadata.summary = clean_enrichment["summary"]
    metadata.energy = clean_enrichment["energy"]
    metadata.valence = clean_enrichment["valence"]
    metadata.danceability = clean_enrichment["danceability"]
    metadata.confidence = clean_enrichment["confidence"]
    metadata.provider = clean_enrichment["provider"]
    metadata.model = clean_enrichment["model"]
    metadata.source = clean_enrichment["source"]
    metadata.raw_json = clean_enrichment["raw_json"]
    metadata.save(force_insert=is_new)
    return metadata


def recordTrackEnrichmentAttempt(
    track: Track,
    provider: object,
    status: str,
    reason: Optional[str] = None,
    error: Optional[str] = None,
    next_retry_at=None,
) -> TrackMetadataEnrichmentTask:
    task = TrackMetadataEnrichmentTask.get_or_none(
        TrackMetadataEnrichmentTask.track == track
    )
    is_new = task is None
    if task is None:
        task = TrackMetadataEnrichmentTask(
            track=track,
            status=status,
            reason=reason or TrackMetadataEnrichmentTask.REASON_METADATA_MISSING,
        )

    task.status = status
    task.reason = reason or task.reason
    task.last_error = error
    task.locked_at = None
    task.next_retry_at = next_retry_at
    task.completed_at = now() if status in (
        TrackMetadataEnrichmentTask.STATUS_COMPLETED,
        TrackMetadataEnrichmentTask.STATUS_SKIPPED,
    ) else None
    task.save(force_insert=is_new)
    return task


def _getCandidateReason(
    track: Track,
    metadata: Optional[TrackMetadata],
    task: Optional[TrackMetadataEnrichmentTask],
    force: bool,
    failed_only: bool,
    current_time,
) -> Optional[str]:
    if force:
        return TrackMetadataEnrichmentTask.REASON_MANUAL_FORCE

    if failed_only:
        if task and task.status in (
            TrackMetadataEnrichmentTask.STATUS_FAILED,
            TrackMetadataEnrichmentTask.STATUS_RETRY,
        ):
            return TrackMetadataEnrichmentTask.REASON_FAILED_RETRY
        return None

    if task:
        if task.status == TrackMetadataEnrichmentTask.STATUS_PENDING:
            return task.reason
        if task.status == TrackMetadataEnrichmentTask.STATUS_RETRY:
            if task.next_retry_at is None or task.next_retry_at <= current_time:
                return task.reason or TrackMetadataEnrichmentTask.REASON_FAILED_RETRY
            return None
        if task.status == TrackMetadataEnrichmentTask.STATUS_RUNNING:
            return None
        if task.status == TrackMetadataEnrichmentTask.STATUS_FAILED:
            return None

    if metadata is None:
        return TrackMetadataEnrichmentTask.REASON_METADATA_MISSING
    if metadata.track_last_modification != track.last_modification:
        return TrackMetadataEnrichmentTask.REASON_TAG_UPDATED
    return None


def _recoverStaleTrackMetadataTasks(current_time, stale_lock_seconds: int) -> int:
    if stale_lock_seconds <= 0:
        return 0
    stale_before = current_time - timedelta(seconds=stale_lock_seconds)
    return (
        TrackMetadataEnrichmentTask.update(
            status=TrackMetadataEnrichmentTask.STATUS_RETRY,
            reason=TrackMetadataEnrichmentTask.REASON_FAILED_RETRY,
            locked_at=None,
            next_retry_at=current_time,
            updated_at=current_time,
        )
        .where(
            TrackMetadataEnrichmentTask.status
            == TrackMetadataEnrichmentTask.STATUS_RUNNING,
            TrackMetadataEnrichmentTask.locked_at.is_null(False),
            TrackMetadataEnrichmentTask.locked_at < stale_before,
        )
        .execute()
    )


def _claimTrackMetadataEnrichmentTask(
    track: Track,
    reason: str,
    force: bool = False,
) -> Optional[TrackMetadataEnrichmentTask]:
    current_time = now()
    task = _get_or_create_track_metadata_enrichment_task(track, reason, force)
    with db.atomic():
        task = TrackMetadataEnrichmentTask.get_by_id(task.id)
        if task.status == TrackMetadataEnrichmentTask.STATUS_RUNNING:
            return None

        updated = (
            TrackMetadataEnrichmentTask.update(
                status=TrackMetadataEnrichmentTask.STATUS_RUNNING,
                reason=reason,
                attempt_count=TrackMetadataEnrichmentTask.attempt_count + 1,
                last_error=None,
                locked_at=current_time,
                next_retry_at=None,
                force=force,
                completed_at=None,
                updated_at=current_time,
            )
            .where(
                TrackMetadataEnrichmentTask.id == task.id,
                TrackMetadataEnrichmentTask.status
                != TrackMetadataEnrichmentTask.STATUS_RUNNING,
            )
            .execute()
        )
        if not updated:
            return None
        return TrackMetadataEnrichmentTask.get_by_id(task.id)


def _get_or_create_track_metadata_enrichment_task(
    track: Track,
    reason: str,
    force: bool = False,
) -> TrackMetadataEnrichmentTask:
    try:
        with db.atomic():
            task, _ = TrackMetadataEnrichmentTask.get_or_create(
                track=track,
                defaults={
                    "status": TrackMetadataEnrichmentTask.STATUS_PENDING,
                    "reason": reason,
                    "force": force,
                },
            )
            return task
    except IntegrityError:
        task = TrackMetadataEnrichmentTask.get_or_none(
            TrackMetadataEnrichmentTask.track == track
        )
        if task is None:
            raise
        return task


def _validateEnrichment(enrichment: Mapping[str, object]) -> Dict[str, object]:
    if not isinstance(enrichment, Mapping):
        raise TrackMetadataInvalidResponseError("Track metadata enrichment must be an object.")

    provider = _optionalText(enrichment.get("provider"), max_length=64)
    source = _optionalText(enrichment.get("source"), max_length=64)
    normalize_llm_labels = provider == "llm" or source == "llm"
    confidence = _optionalFloat(enrichment.get("confidence"), "confidence")
    if confidence is not None and not 0 <= confidence <= 1:
        raise TrackMetadataInvalidResponseError("confidence must be between 0 and 1.")

    raw_value = enrichment.get("raw", enrichment)
    raw_json = raw_value if isinstance(raw_value, str) else json.dumps(
        raw_value,
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "language": _normalizeLanguage(enrichment.get("language")),
        "mood": _stringList(
            enrichment.get("mood"),
            max_items=5,
            normalize_llm_labels=normalize_llm_labels,
        ),
        "scene": _stringList(
            enrichment.get("scene"),
            max_items=5,
            normalize_llm_labels=normalize_llm_labels,
        ),
        "tags": _stringList(
            enrichment.get("tags"),
            max_items=8,
            normalize_llm_labels=normalize_llm_labels,
        ),
        "summary": _optionalText(enrichment.get("summary")),
        "energy": _optionalPercent(enrichment.get("energy"), "energy"),
        "valence": _optionalPercent(enrichment.get("valence"), "valence"),
        "danceability": _optionalPercent(enrichment.get("danceability"), "danceability"),
        "confidence": confidence,
        "provider": provider,
        "model": _optionalText(enrichment.get("model"), max_length=128),
        "source": source,
        "raw_json": raw_json,
    }


def _optionalText(value: object, max_length: Optional[int] = None) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_length] if max_length else text


def _normalizeLanguage(value: object) -> Optional[str]:
    text = _optionalText(value, max_length=16)
    if text is None:
        return None
    key = _labelKey(text)
    alias = LANGUAGE_ALIASES.get(key)
    if alias is not None or key in LANGUAGE_ALIASES:
        return alias
    code = key.replace(" ", "-")
    if code in LANGUAGE_CODES:
        return code
    return "other"


def _stringList(
    value: object,
    *,
    max_items: Optional[int] = None,
    normalize_llm_labels: bool = False,
) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TrackMetadataInvalidResponseError("list metadata fields must be arrays.")
    clean_values = []
    seen = set()
    for item in value:
        text = _normalizeLlmLabel(item) if normalize_llm_labels else _optionalText(item)
        if not text:
            continue
        dedupe_key = _labelKey(text)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        clean_values.append(text)
        if max_items is not None and len(clean_values) >= max_items:
            break
    return clean_values


def _normalizeLlmLabel(value: object) -> Optional[str]:
    text = _optionalText(value, max_length=32)
    if text is None:
        return None
    return LLM_LABEL_ALIASES.get(_labelKey(text), text)


def _labelKey(value: object) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"[-_/]+", " ", text)
    return " ".join(text.split())


def _filterSemanticTags(tags: Iterable[str], track: Track) -> List[str]:
    excluded = {_labelKey(value) for value in _trackSpecificTagExclusions(track)}
    clean_tags = []
    seen = set()
    for tag in tags:
        key = _labelKey(tag)
        if (
            not key
            or key in LANGUAGE_TAG_KEYS
            or _isTrackSpecificTagKey(key, excluded)
        ):
            continue
        if key in seen:
            continue
        seen.add(key)
        clean_tags.append(tag)
    return clean_tags


def _isTrackSpecificTagKey(key: str, excluded_keys: Iterable[str]) -> bool:
    for excluded_key in excluded_keys:
        if not excluded_key:
            continue
        if key == excluded_key:
            return True
        if CJK_RE.search(excluded_key) and excluded_key in key:
            return True
    return False


def _trackSpecificTagExclusions(track: Track) -> List[object]:
    values: List[object] = [
        getattr(track, "title", None),
        getattr(track, "year", None),
    ]
    artist = getattr(track, "artist", None)
    if artist is not None:
        values.append(getattr(artist, "name", None))
    album = getattr(track, "album", None)
    if album is not None:
        values.append(getattr(album, "name", None))
        album_artist = getattr(album, "artist", None)
        if album_artist is not None:
            values.append(getattr(album_artist, "name", None))
    return values


def _optionalPercent(value: object, field_name: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise TrackMetadataInvalidResponseError(f"{field_name} must be an integer.") from exc
    if not 0 <= number <= 100:
        raise TrackMetadataInvalidResponseError(f"{field_name} must be between 0 and 100.")
    return number


def _optionalFloat(value: object, field_name: str) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise TrackMetadataInvalidResponseError(f"{field_name} must be a number.") from exc


def _positiveFloat(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _nonNegativeFloat(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _nonNegativeInt(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _jsonList(values: Iterable[object]) -> Optional[str]:
    clean_values = [str(value).strip() for value in values if str(value).strip()]
    return json.dumps(clean_values, ensure_ascii=False) if clean_values else None


def _buildSafePathHints(path: str) -> List[str]:
    if not path:
        return []
    normalized = os.path.normpath(path)
    hints = []
    basename = os.path.basename(normalized)
    if basename:
        hints.append(_safePathToken(basename))

    directory = os.path.dirname(normalized)
    for _ in range(2):
        token = os.path.basename(directory)
        if token:
            hints.append(_safePathToken(token))
        next_directory = os.path.dirname(directory)
        if not next_directory or next_directory == directory:
            break
        directory = next_directory

    return [hint for hint in hints if hint]


def _safePathToken(value: str) -> str:
    return SAFE_PATH_TOKEN_RE.sub("", value).strip()
