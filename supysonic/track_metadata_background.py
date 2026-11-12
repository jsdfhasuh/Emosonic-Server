"""Unattended metadata processing with persistent pre-request spending checks."""

import json
import logging
from datetime import datetime, timedelta
from typing import Mapping

import requests
from peewee import JOIN

from .db import Meta, Track, TrackMetadata, TrackMetadataEnrichmentTask, now
from .llm_client import LlmConfigError, LlmUpstreamError, _extract_upstream_error
from .logging_utils import format_log_event
from .metadata_budget import BudgetExceeded, BudgetUnavailable, MetadataBudget
from .metadata_pricing import reserve_request_micro_usd, response_cost_micro_usd
from .scanner_func.scanner_track_enrich import (
    LLMMetadataProvider, _claimTrackMetadataEnrichmentTask, _recoverStaleTrackMetadataTasks,
    applyTrackMetadataEnrichment, buildTrackMetadataInput, recordTrackEnrichmentAttempt,
)
from .scanner_func.scanner_review_tasks import createLowConfidenceTrackMetadataReviewTask
from .track_metadata_quality import is_high_quality_track_metadata, is_llm_track_metadata
from .track_metadata_web import WebMetadataProvider, post_web_response


logger = logging.getLogger(__name__)
PAUSE_KEY = "metadata_paid_pause"
WEB_REASON = "automatic_web_lookup"


def budget_is_configured(config: Mapping[str, object]) -> bool:
    return "monthly_budget_usd" in config or bool(config.get("web_search_enabled", False))


class BudgetedMetadataProvider:
    name = "llm"
    source = "llm"

    def __init__(self, config: Mapping[str, object]) -> None:
        self.config = dict(config)
        if self.config.get("api_base_url", "").rstrip("/") != "https://api.openai.com/v1":
            raise LlmConfigError("Budgeted metadata requires the official OpenAI endpoint.")
        if self.config.get("model") != "gpt-6-luna":
            raise LlmConfigError("No verified metadata price configuration for this model.")
        self.model = self.config["model"]
        self.api_key = str(self.config.get("api_key") or "")
        if not self.api_key:
            raise LlmConfigError("Metadata API key is required.")
        self.web_enabled = self.config.get("web_search_enabled", False) is True
        self.budget = MetadataBudget(self.config)
        self.text_provider = LLMMetadataProvider(self.config, self._text_request)

    def _request(self, payload: dict, kind: str, before_request=None) -> dict:
        payload = dict(payload, service_tier="default")
        reservation = self.budget.reserve(kind, reserve_request_micro_usd(payload, kind=kind))
        # A durable attempt marker precedes network I/O, but follows budget approval.
        if before_request is not None:
            try:
                before_request()
            except Exception:
                # Nothing was sent. A failed refund still fails closed in the ledger.
                self.budget.settle(reservation, 0)
                raise
        if kind == "web":
            response = post_web_response(self.api_key, payload, timeout_seconds=60)
        else:
            try:
                result = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": "Bearer " + self.api_key},
                    json=payload, timeout=60, allow_redirects=False,
                )
            except requests.RequestException:
                raise LlmUpstreamError("Metadata request outcome is unknown; allowance retained.") from None
            try:
                if result.status_code != 200:
                    raise LlmUpstreamError("Metadata API request failed; allowance retained.",
                                           _extract_upstream_error(result, api_key=self.api_key))
                try:
                    response = result.json()
                except ValueError:
                    raise LlmUpstreamError("Metadata API returned invalid JSON; allowance retained.") from None
            finally:
                result.close()
        cost = response_cost_micro_usd(response, model=self.model, kind=kind)
        if cost is None:
            raise BudgetUnavailable("Metadata usage is unavailable; request allowance retained.")
        self.budget.settle(reservation, cost)
        logger.info(format_log_event("track_metadata_enrichment", "metadata_budget_settled",
                                     kind=kind, cost_micro_usd=cost))
        return response

    def _text_request(self, payload: dict) -> dict:
        return self._request(payload, "text")

    def enrich(self, track_input: Mapping[str, object]) -> dict:
        bounded = {key: value[:1024] if isinstance(value, str) else value
                   for key, value in track_input.items() if key != "path_hints"}
        return self.text_provider.enrich(bounded)

    def enrich_web(self, track_input: Mapping[str, object], before_request) -> dict:
        return WebMetadataProvider(
            self.config, request_callback=lambda payload: self._request(payload, "web", before_request),
        ).enrich(track_input)


def _read_raw(metadata: TrackMetadata) -> dict:
    try:
        raw = json.loads(metadata.raw_json or "{}")
    except (ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _needs_web(metadata: TrackMetadata, track: Track) -> bool:
    return bool(metadata is not None
                and metadata.track_last_modification == track.last_modification
                and is_llm_track_metadata(metadata)
                and (not is_high_quality_track_metadata(metadata) or not metadata.language)
                and "web_lookup" not in _read_raw(metadata))


def _candidate_query(web: bool):
    task, metadata = TrackMetadataEnrichmentTask, TrackMetadata
    query = (Track.select(Track)
             .join(metadata, JOIN.LEFT_OUTER, on=(metadata.track == Track.id))
             .switch(Track).join(task, JOIN.LEFT_OUTER, on=(task.track == Track.id)))
    due = (task.id.is_null() | ((task.status != task.STATUS_RUNNING)
           & (task.next_retry_at.is_null() | (task.next_retry_at <= now()))
           & ((task.status != task.STATUS_FAILED) | (task.reason == task.REASON_PROVIDER_QUOTA))))
    if web:
        no_semantics = ((metadata.mood_json.is_null() | (metadata.mood_json == "[]"))
                        & (metadata.scene_json.is_null() | (metadata.scene_json == "[]"))
                        & (metadata.tags_json.is_null() | (metadata.tags_json == "[]"))
                        & metadata.energy.is_null() & metadata.valence.is_null()
                        & metadata.danceability.is_null())
        eligible = ((metadata.track_last_modification == Track.last_modification)
                    & ((metadata.provider == "llm") | (metadata.source == "llm"))
                    & (metadata.confidence.is_null() | (metadata.confidence < 0.5)
                       | metadata.language.is_null() | (metadata.language == "") | no_semantics)
                    & (metadata.raw_json.is_null() | ~metadata.raw_json.contains('"web_lookup"')))
    else:
        eligible = metadata.id.is_null() | (metadata.track_last_modification != Track.last_modification)
    return query.where(due, eligible).order_by(Track.created.desc(), Track.id)


def _metadata_dict(metadata: TrackMetadata) -> dict:
    result = {name: getattr(metadata, name) for name in (
        "language", "summary", "energy", "valence", "danceability", "confidence",
        "provider", "source", "model",
    )}
    result.update(mood=metadata.get_moods(), scene=metadata.get_scenes(), tags=metadata.get_tags())
    return result


def merge_web_result(metadata: TrackMetadata, candidate: dict) -> dict:
    """Keep useful existing metadata unless an evidence-backed candidate improves it."""
    base = _metadata_dict(metadata)
    quality_probe = TrackMetadata(
        provider="llm", source="llm", confidence=candidate.get("confidence"),
        mood_json=json.dumps(candidate.get("mood") or []),
        scene_json=json.dumps(candidate.get("scene") or []),
        tags_json=json.dumps(candidate.get("tags") or []),
        **{name: candidate.get(name) for name in ("energy", "valence", "danceability")},
    )
    improved = (is_high_quality_track_metadata(quality_probe)
                and float(candidate.get("confidence") or 0) >= float(metadata.confidence or 0))
    result = {**base, **candidate} if improved else dict(base)
    if improved and is_high_quality_track_metadata(metadata):
        for field in ("language", "summary", "mood", "scene", "tags",
                      "energy", "valence", "danceability"):
            if result.get(field) is None or result.get(field) == [] or result.get(field) == "":
                result[field] = base[field]
    evidence = dict(candidate.get("raw", {}).get("web_lookup", {}))
    if not result.get("language") and candidate.get("language"):
        result["language"] = candidate["language"]
    raw = _read_raw(metadata)
    raw["web_lookup"] = {
        **evidence, "status": "completed" if improved else "inconclusive",
        "track_last_modification": metadata.track_last_modification,
        "finished_at": now().isoformat(), "previous_metadata": base,
    }
    result["raw"] = raw
    return result


def _mark_web_attempt(metadata: TrackMetadata) -> None:
    original = metadata.raw_json
    raw = _read_raw(metadata)
    if "web_lookup" in raw:
        raise BudgetUnavailable("This recording already has a web lookup attempt.")
    raw["web_lookup"] = {"status": "started", "started_at": now().isoformat(),
                         "track_last_modification": metadata.track_last_modification}
    updated = TrackMetadata.update(raw_json=json.dumps(raw, ensure_ascii=False)).where(
        TrackMetadata.id == metadata.id,
        TrackMetadata.raw_json.is_null() if original is None else TrackMetadata.raw_json == original,
        TrackMetadata.track_last_modification == metadata.track_last_modification,
    ).execute()
    if updated != 1:
        raise BudgetUnavailable("Metadata changed before its web request.")
    metadata.raw_json = json.dumps(raw, ensure_ascii=False)


def _finish_web_error(metadata: TrackMetadata, error: Exception) -> None:
    raw = _read_raw(metadata)
    marker = raw.get("web_lookup")
    if isinstance(marker, dict) and marker.get("status") == "started":
        marker.update(status="failed", error_type=type(error).__name__, finished_at=now().isoformat())
        TrackMetadata.update(raw_json=json.dumps(raw, ensure_ascii=False)).where(
            TrackMetadata.id == metadata.id,
            TrackMetadata.track_last_modification == metadata.track_last_modification,
            TrackMetadata.raw_json == metadata.raw_json,
        ).execute()


def _pause_until(deadline: datetime) -> None:
    value = str(int(deadline.timestamp()))
    for _ in range(8):
        row, created = Meta.get_or_create(key=PAUSE_KEY, defaults={"value": value})
        if created or int(row.value) >= int(value):
            return
        if Meta.update(value=value).where(Meta.key == PAUSE_KEY, Meta.value == row.value).execute():
            return
    raise BudgetUnavailable("Could not persist metadata pause.")


def runAutomaticMetadataPass(limit: int, provider: BudgetedMetadataProvider,
                             stale_lock_seconds: int = 900, **unused) -> dict:
    summary = {"selected": 0, "enriched": 0, "web_enriched": 0, "failed": 0,
               "skipped": 0, "budget_paused": False, "tracks": []}
    pause = Meta.get_or_none(Meta.key == PAUSE_KEY)
    if pause and int(pause.value) > datetime.now().timestamp():
        summary["budget_paused"] = True
        return summary
    _recoverStaleTrackMetadataTasks(now(), stale_lock_seconds)
    text_limit = max(1, limit - 1) if provider.web_enabled else limit
    for web, batch_limit in ((False, text_limit), (True, limit)):
        if web and not provider.web_enabled:
            continue
        remaining = min(batch_limit, max(0, limit - summary["selected"]))
        if not remaining:
            continue
        for track in list(_candidate_query(web).limit(remaining)):
            reason = WEB_REASON if web else "automatic_metadata"
            task = _claimTrackMetadataEnrichmentTask(track, reason, respect_schedule=True)
            if task is None:
                summary["skipped"] += 1
                continue
            # Recheck after claiming: another worker may have finished this candidate.
            track = Track.get_by_id(track.id)
            metadata = TrackMetadata.get_or_none(TrackMetadata.track == track)
            current = metadata is not None and metadata.track_last_modification == track.last_modification
            if (web and not _needs_web(metadata, track)) or (not web and current):
                recordTrackEnrichmentAttempt(track, provider, "completed", reason=reason)
                summary["skipped"] += 1
                continue
            summary["selected"] += 1
            original_mtime = track.last_modification
            try:
                track_input = buildTrackMetadataInput(track, include_path_hints=False)
                if web:
                    result = provider.enrich_web(track_input, lambda: _mark_web_attempt(metadata))
                    result = merge_web_result(metadata, result)
                else:
                    result = provider.enrich(track_input)
                if Track.get_by_id(track.id).last_modification != original_mtime:
                    recordTrackEnrichmentAttempt(track, provider, "pending", reason="tag_updated")
                    summary["skipped"] += 1
                    continue
                applied = applyTrackMetadataEnrichment(track, result)
                createLowConfidenceTrackMetadataReviewTask(track, applied)
                recordTrackEnrichmentAttempt(track, provider, "completed", reason=reason)
                summary["enriched"] += 1
                summary["web_enriched"] += int(web)
                summary["tracks"].append({"id": str(track.id), "stage": "web" if web else "text"})
            except (BudgetExceeded, BudgetUnavailable) as error:
                if web:
                    _finish_web_error(metadata, error)
                reset = getattr(error, "reset_at", None)
                deadline = reset if reset is not None else datetime.now().astimezone() + timedelta(hours=24)
                _pause_until(deadline)
                recordTrackEnrichmentAttempt(track, provider, "retry", reason="metadata_budget_paused",
                                             next_retry_at=datetime.fromtimestamp(deadline.timestamp()))
                summary["budget_paused"] = True
                logger.warning(format_log_event("track_metadata_enrichment", "metadata_budget_paused",
                                                error_type=type(error).__name__))
                return summary
            except Exception as error:
                if web:
                    _finish_web_error(metadata, error)
                delay = min(86400, 300 * 2 ** min(task.attempt_count, 8))
                deadline = datetime.now().astimezone() + timedelta(seconds=delay)
                _pause_until(deadline)
                recordTrackEnrichmentAttempt(track, provider, "retry", reason="provider_error",
                                             error=type(error).__name__,
                                             next_retry_at=datetime.fromtimestamp(deadline.timestamp()))
                summary["failed"] += 1
                logger.warning(format_log_event("track_metadata_enrichment", "metadata_background_retry",
                                                error_type=type(error).__name__, retry_after_seconds=delay))
                return summary
    return summary
