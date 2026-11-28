"""Resume one bounded history, extraction, exploration or matching step per pass."""

from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import logging
from typing import List, Optional

from peewee import Case, fn

from ..album_discovery_catalog import CatalogError, CatalogTrack, normalize_music_name
from ..album_discovery_quota import SHANGHAI
from ..album_discovery_service import AlbumDiscoveryError, AlbumDiscoveryService
from ..album_discovery_settings import (
    AlbumDiscoverySettingsError,
    album_discovery_target_user,
    load_album_discovery_runtime,
)
from ..db import (
    BilibiliDiscoveryCandidate as Candidate,
    BilibiliDiscoveryRun as Run,
    User,
    connection_scope,
)
from ..openai_recommendations import OpenAIRecommendationError, recommendation_lease
from .exploration import VerifiedSuggestion, build_exploration_plan, explore_task
from .extractor import BATCH_SIZE, Extraction, extract_history
from .history import BilibiliHistoryClient, HistoryError, HistoryObservation
from .matcher import (
    MatchError,
    catalog_target_allowed,
    find_local_recording,
    local_track_matches_candidate,
    playable_local_track,
    recording_version,
    resolve_catalog_recording,
)
from .settings import BilibiliDiscoverySettings, load_bilibili_discovery_settings
from .subscriptions import (
    SubscriptionUnavailable, build_subscription_plan, record_performance, subscribe_performer,
)

logger = logging.getLogger(__name__)
SOURCE_WINDOW_DAYS = 14
_RETRY = timedelta(minutes=5)
_LONG_RETRY = timedelta(minutes=30)
_MAX_EVIDENCE = 20


class _StaleStep(RuntimeError):
    """Another worker committed this step after its lease expired."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def _run_update(run: Run, now: datetime, **fields: object) -> None:
    count = Run.update(updated_at=now, **fields).where(
        Run.id == run.id, Run.status == run.status,
        Run.sync_confirmed_at == run.sync_confirmed_at,
        Run.extract_offset == run.extract_offset,
        Run.discovery_progress_json == run.discovery_progress_json,
        Run.next_attempt_at == run.next_attempt_at,
    ).execute()
    if count != 1:
        raise _StaleStep


def _reason_count(progress: dict, reason: str, count: int = 1) -> None:
    counts = progress.setdefault("outcomes", {})
    counts[reason] = counts.get(reason, 0) + count


def _record_evidence(
    row: Extraction, observation: HistoryObservation, number: int,
) -> dict:
    return {
        "source": {
            "number": number, "id": observation.id, "business": observation.business,
            "bvid": observation.bvid, "cid": observation.cid, "view_time": observation.view_time,
            "event_id": observation.event_id,
        },
        "evidence": row.evidence,
    }


def _identity(row: Extraction) -> str:
    key = [normalize_music_name(value) for value in (row.title, row.artist, row.version, row.album_hint)]
    return "recording:" + hashlib.sha256(_json(key).encode("utf-8")).hexdigest()


def _merged_evidence(first: List[dict], second: List[dict]) -> str:
    retained = {_json(item): item for item in (*first, *second)}
    # Always keep a representative of each evidence type before the per-row cap.
    by_kind = {}
    for key, item in retained.items():
        by_kind.setdefault(item["kind"], key)
    order = dict.fromkeys((*by_kind.values(), *retained.keys()))
    return _json([retained[key] for key in list(order)[:_MAX_EVIDENCE]])


def _save_candidate(
    run: Run, now: datetime, key: str, title: str, artist: str, version: str, album_hint: str,
    evidence: List[dict],
) -> None:
    candidate, created = Candidate.get_or_create(
        run=run, identity_key=key,
        defaults={
            "title": title, "artist": artist, "version": version, "album_hint": album_hint,
            "source_evidence_json": _merged_evidence([], evidence),
            "created_at": now, "updated_at": now,
        },
    )
    if not created:
        combined = _merged_evidence(json.loads(candidate.source_evidence_json), evidence)
        if combined != candidate.source_evidence_json:
            Candidate.update(source_evidence_json=combined, updated_at=now).where(Candidate.id == candidate.id).execute()


def _fetch(
    run: Run, settings: BilibiliDiscoverySettings, now: datetime,
) -> dict:
    client = BilibiliHistoryClient(settings.history_api_url, settings.history_api_token_file)
    if run.sync_confirmed_at is None:
        client.sync()
        _run_update(run, now, sync_confirmed_at=now, next_attempt_at=None, last_error="")
        run.sync_confirmed_at, run.next_attempt_at = now, None
    observations = client.read_day(run.source_day)
    _run_update(
        run, now, snapshot_json=_json([asdict(row) for row in observations]),
        extract_offset=0, status=("extracting" if observations else "done"),
        next_attempt_at=None, last_error="",
    )
    logger.info("bilibili_history_snapshot day=%s observations=%s", run.source_day, len(observations))
    return {"status": "extracting" if observations else "done", "observations": len(observations)}


def _extract(run: Run, config: object, now: datetime, snapshot: list, progress: dict) -> dict:
    offset = run.extract_offset
    batch = [HistoryObservation(**row) for row in snapshot[offset:offset + BATCH_SIZE]]
    extracted = extract_history(config, batch)
    direct = []
    for row in extracted:
        reference = _record_evidence(row, batch[row.number - 1], offset + row.number)
        _reason_count(progress, row.reason)
        if row.performer:
            record_performance(progress, row.performer, batch[row.number - 1])
        if row.kind == "song":
            direct.append((row, {"kind": "direct", **reference}))
        if row.kind in ("style_lead", "work_lead", "collaboration_lead") or row.collaborator:
            progress.setdefault("leads", []).append({**asdict(row), **reference})
    with connection_scope() as database, database.atomic():
        _run_update(
            run, now, extract_offset=offset + len(batch),
            discovery_progress_json=_json(progress), next_attempt_at=None, last_error="",
        )
        for row, evidence in direct:
            _save_candidate(run, now, _identity(row), row.title, row.artist,
                            row.version, row.album_hint, [evidence])
    logger.info("bilibili_extraction day=%s offset=%s identified=%s",
                run.source_day, offset + len(batch), len(direct))
    return {"status": "extracting", "extract_offset": offset + len(batch), "identified": len(direct)}


def _save_suggestion(run: Run, now: datetime, suggestion: VerifiedSuggestion) -> None:
    song = suggestion.song
    if len(song.track.title) > 256 or len(song.track.artist) > 256 or len(song.album_name) > 256:
        return
    evidence = [
        {
            "kind": suggestion.kind, "source": lead["source"], "evidence": lead["evidence"],
            "basis": suggestion.basis, "recording": song.track.to_payload(),
        }
        for lead in suggestion.leads
    ]
    _save_candidate(
        run, now, "cn:" + song.track.song_id, song.track.title, song.track.artist,
        recording_version(song.track.title, song.album_name), song.album_name, evidence,
    )


def _explore(run: Run, config: object, now: datetime, progress: dict) -> dict:
    if "plan" not in progress:
        cutoff = datetime.combine(run.source_day + timedelta(days=1), time.min, SHANGHAI) - timedelta(microseconds=1)
        plan, skipped = build_exploration_plan(run.user, progress.pop("leads", []), cutoff)
        if load_bilibili_discovery_settings(config).auto_subscribe_artists:
            plan.extend(build_subscription_plan(run, progress, SOURCE_WINDOW_DAYS))
        for reason, count in skipped.items():
            _reason_count(progress, reason, count)
        progress["plan"], progress["exploration_offset"] = plan, 0
        status = "extracting" if plan else "done"
        _run_update(
            run, now, status=status, discovery_progress_json=_json(progress),
            next_attempt_at=None, last_error="",
        )
        return {"status": status, "planned_probes": len(plan)}
    plan = progress["plan"]
    offset = progress["exploration_offset"]
    task = plan[offset]
    if task["kind"] == "subscribe_artist":
        suggestions = []
        if load_bilibili_discovery_settings(config).auto_subscribe_artists:
            oracle = AlbumDiscoveryService.from_config(config, with_catalog=False).oracle
            reason = subscribe_performer(run.user, oracle, task)
        else:
            reason = "artist_subscriptions_disabled"
        task["outcome"] = reason
    else:
        service = AlbumDiscoveryService.from_config(config)
        suggestions, reason = explore_task(service.catalog, task, progress)
        if task["kind"] == "work" and task.get("retryable") and not task.get("retry"):
            # Give other works their turn before retrying an unavailable catalog page.
            plan.append({**task, "retry": True})
    if reason:
        _reason_count(progress, reason)
    else:
        _reason_count(progress, "verified_" + plan[offset]["kind"], len(suggestions))
    progress["exploration_offset"] = offset + 1
    status = "done" if offset + 1 == len(plan) else "extracting"
    with connection_scope() as database, database.atomic():
        _run_update(
            run, now, status=status, discovery_progress_json=_json(progress),
            next_attempt_at=None, last_error="",
        )
        for suggestion in suggestions:
            _save_suggestion(run, now, suggestion)
    logger.info("bilibili_exploration day=%s probes=%s found=%s reason=%s",
                run.source_day, offset + 1, len(suggestions), reason)
    return {"status": status, "processed_probes": offset + 1, "found": len(suggestions)}


def _resume_run(
    run: Run, config: object, settings: BilibiliDiscoverySettings, now: datetime,
) -> dict:
    try:
        if run.status == "fetch_pending":
            return _fetch(run, settings, now)
        snapshot = json.loads(run.snapshot_json)
        progress = json.loads(run.discovery_progress_json)
        if run.extract_offset < len(snapshot):
            return _extract(run, config, now, snapshot, progress)
        return _explore(run, config, now, progress)
    except HistoryError as exc:
        reason = str(exc)
        deadline = now + (_LONG_RETRY if reason in (
            "history_authentication_failed", "history_rate_limited",
        ) else _RETRY)
    except OpenAIRecommendationError as exc:
        reason = "extract_" + exc.reason
        duration = _LONG_RETRY if exc.reason in ("config", "invalid_response", "accounting") else _RETRY
        deadline = now + duration
        if exc.retry_at:
            deadline = max(deadline, _utc_naive(datetime.fromtimestamp(exc.retry_at, tz=timezone.utc)))
    except CatalogError:
        reason, deadline = "catalog_unavailable", now + _RETRY
    except SubscriptionUnavailable:
        reason, deadline = "artist_subscription_unavailable", now + _RETRY
    except AlbumDiscoverySettingsError:
        reason, deadline = "album_discovery_configuration_unavailable", now + _LONG_RETRY
    _run_update(run, now, next_attempt_at=deadline, last_error=reason)
    logger.info("bilibili_discovery_deferred day=%s stage=%s reason=%s", run.source_day, run.status, reason)
    return {"status": run.status, "reason": reason}


def _candidate_result(
    row: Candidate, now: datetime, status: str, reason: str, **values: object,
) -> dict:
    Candidate.update(status=status, reason=reason, updated_at=now, **values).where(
        Candidate.id == row.id, Candidate.status == row.status,
        Candidate.release == row.release_id, Candidate.track == row.track_id,
        Candidate.next_attempt_at == row.next_attempt_at,
    ).execute()
    logger.info("bilibili_candidate id=%s day=%s status=%s reason=%s",
                row.id, row.run.source_day, status, reason)
    return {"status": status, "reason": reason}


def _combine_rows(retained: Candidate, other: Candidate, now: datetime) -> None:
    merged = _merged_evidence(
        json.loads(retained.source_evidence_json), json.loads(other.source_evidence_json),
    )
    Candidate.update(source_evidence_json=merged, updated_at=now).where(Candidate.id == retained.id).execute()
    Candidate.delete().where(Candidate.id == other.id, Candidate.release.is_null()).execute()


def _mark_local(row: Candidate, track_id: object, now: datetime) -> dict:
    with connection_scope() as database, database.atomic():
        existing = Candidate.get_or_none(
            Candidate.run == row.run_id, Candidate.status == "ready",
            Candidate.track == track_id, Candidate.id != row.id,
        )
        if existing:
            _combine_rows(existing, row, now)
            return {"status": "ready", "reason": "local_duplicate"}
        return _candidate_result(
            row, now, "ready", "local_verified", track=track_id, ready_at=now,
            next_attempt_at=None,
        )


def _canonicalize_catalog(row: Candidate, song_id: str, now: datetime) -> Optional[Candidate]:
    key = "cn:" + song_id
    if row.identity_key == key:
        return row
    with connection_scope() as database, database.atomic():
        existing = Candidate.get_or_none(Candidate.run == row.run_id, Candidate.identity_key == key)
        if existing is not None:
            _combine_rows(existing, row, now)
            return None
        Candidate.update(identity_key=key, updated_at=now).where(Candidate.id == row.id).execute()
    row.identity_key = key
    return row


def _process_candidate(row: Candidate, config: object, current: datetime, now: datetime) -> dict:
    from .orders import confirm_bilibili_album, refresh_bilibili_candidate

    try:
        runtime = load_album_discovery_runtime(config)
        if row.release_id:
            service = AlbumDiscoveryService.from_config(config, with_catalog=False)
            ready = refresh_bilibili_candidate(service, row, now=current)
            fresh = Candidate.get_by_id(row.id)
            if fresh.status in ("ready", "waiting_album"):
                delay = _LONG_RETRY if ready else _RETRY
                return _candidate_result(fresh, now, fresh.status, fresh.reason, next_attempt_at=now + delay)
            return {"status": fresh.status, "reason": fresh.reason}
        if row.apple_song_id or row.status == "waiting_album":
            return _candidate_result(
                row, now, "skipped", "album_link_missing",
                track=None, ready_at=None, next_attempt_at=None,
            )
        if row.status == "ready":
            track = row.track
            if track and playable_local_track(track, runtime.library_root) and local_track_matches_candidate(track, row):
                return _candidate_result(
                    row, now, "ready", row.reason, next_attempt_at=now + _LONG_RETRY,
                )
            return _candidate_result(
                row, now, "skipped", "local_recording_no_longer_current",
                track=None, ready_at=None, next_attempt_at=None,
            )
        expected = next((
            CatalogTrack.from_payload(item["recording"])
            for item in json.loads(row.source_evidence_json) if "recording" in item
        ), None)
        local = find_local_recording(
            row.run.user, runtime.library_root, row.title, row.artist,
            row.version, row.album_hint, expected=expected,
        )
        if local.track is not None:
            return _mark_local(row, local.track.id, now)
        if local.reason != "local_missing":
            temporary = local.reason in ("local_unreadable", "library_unavailable")
            return _candidate_result(
                row, now, "pending" if temporary else "skipped", local.reason,
                next_attempt_at=now + _RETRY if temporary else None,
            )
        service = AlbumDiscoveryService.from_config(config)
        song_id = row.identity_key.split(":", 1)[1] if row.identity_key.startswith("cn:") else None
        song, album = resolve_catalog_recording(
            service.catalog, row.title, row.artist, row.version, row.album_hint, song_id,
        )
        if not catalog_target_allowed(row.run.user, service.catalog, song, album):
            return _candidate_result(row, now, "skipped", "negative_feedback", next_attempt_at=None)
        row = _canonicalize_catalog(row, song.track.song_id, now)
        if row is None:
            return {"status": "duplicate", "reason": "same_cn_recording"}
        outcome = confirm_bilibili_album(
            service, row.run.user, row, album, song.track.song_id, now=current,
        )
        fresh = Candidate.get_by_id(row.id)
        if fresh.status == "pending":
            return _candidate_result(fresh, now, "pending", outcome.status, next_attempt_at=now + _LONG_RETRY)
        return {"status": fresh.status, "reason": outcome.status}
    except MatchError as exc:
        return _candidate_result(row, now, "skipped", str(exc), next_attempt_at=None)
    except CatalogError:
        reason = "catalog_unavailable"
    except AlbumDiscoverySettingsError:
        reason = "album_discovery_configuration_unavailable"
    except AlbumDiscoveryError:
        reason = "album_confirmation_deferred"
    return _candidate_result(row, now, row.status, reason, next_attempt_at=now + _RETRY)


def run_bilibili_discovery(config: object, *, now: Optional[datetime] = None) -> dict:
    settings = load_bilibili_discovery_settings(config)
    if not settings.enabled:
        return {"status": "disabled"}
    if now is not None and now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current = datetime.now(SHANGHAI) if now is None else now.astimezone(SHANGHAI)
    utc_now = _utc_naive(current)
    yesterday = current.date() - timedelta(days=1)
    earliest = current.date() - timedelta(days=SOURCE_WINDOW_DAYS - 1)
    ready_after = utc_now - timedelta(days=SOURCE_WINDOW_DAYS)
    target = album_discovery_target_user(config)
    with connection_scope():
        user = User.get_or_none(User.name == target) if target else None
        if user is None:
            return {"status": "target_user_missing"}
        if current.time() >= settings.start_time:
            _, created = Run.get_or_create(
                user=user, source_day=yesterday, defaults={"created_at": utc_now, "updated_at": utc_now},
            )
            if created:
                previous = (
                    Run.select(Run.source_day).where(Run.user == user, Run.source_day < yesterday)
                    .order_by(Run.source_day.desc()).first()
                )
                if previous and previous.source_day < yesterday - timedelta(days=1):
                    logger.info(
                        "bilibili_discovery_source_day_gap first=%s last=%s",
                        previous.source_day + timedelta(days=1), yesterday - timedelta(days=1),
                    )
        run = Run.select().where(
            Run.user == user, Run.source_day >= earliest, Run.source_day <= yesterday,
            Run.status.in_(("fetch_pending", "extracting")),
            Run.next_attempt_at.is_null() | (Run.next_attempt_at <= utc_now),
        ).order_by(Run.source_day.desc()).first()
        if run is not None:
            with recommendation_lease(f"bilibili-discovery:{user.id}:{run.source_day}") as acquired:
                if not acquired:
                    return {"status": "busy"}
                run = Run.get_by_id(run.id)
                if run.status == "done" or (run.next_attempt_at and run.next_attempt_at > utc_now):
                    return {"status": run.status}
                try:
                    result = _resume_run(run, config, settings, utc_now)
                except _StaleStep:
                    return {"status": "advanced_elsewhere"}
                return {"source_day": run.source_day.isoformat(), **result}
        Candidate.update(
            status="skipped", reason="source_window_expired", updated_at=utc_now, next_attempt_at=None,
        ).where(
            Candidate.run.in_(Run.select(Run.id).where(Run.user == user, Run.source_day < earliest)),
            Candidate.status == "pending", Candidate.release.is_null(),
        ).execute()
        row = Candidate.select(Candidate, Run).join(Run).where(
            Run.user == user,
            ((Candidate.status == "pending") & (Run.source_day >= earliest))
            | (Candidate.status == "waiting_album")
            | ((Candidate.status == "ready") & (Candidate.ready_at > ready_after)),
            Candidate.next_attempt_at.is_null() | (Candidate.next_attempt_at <= utc_now),
        ).order_by(
            Case(None, ((Candidate.status == "ready", 1),), 0),
            fn.COALESCE(Candidate.next_attempt_at, Candidate.created_at), Candidate.created_at, Candidate.id,
        ).first()
        if row is not None:
            with recommendation_lease(f"bilibili-discovery:{user.id}:{row.run.source_day}") as acquired:
                if not acquired:
                    return {"status": "busy"}
                row = Candidate.get_or_none(Candidate.id == row.id)
                if row is None or row.status not in ("pending", "waiting_album", "ready") or (
                    row.next_attempt_at and row.next_attempt_at > utc_now
                ):
                    return {"status": "advanced_elsewhere"}
                result = _process_candidate(row, config, current, utc_now)
                return {"source_day": row.run.source_day.isoformat(), **result}
    return {"status": "idle"}
