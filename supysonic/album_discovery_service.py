"""Preview, confirm and reconcile discovery through the same release records."""

import json
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Dict, Optional, Tuple
from uuid import UUID

from peewee import SqliteDatabase

from .album_discovery import (
    AlbumDiscoveryAvailability,
    get_album_discovery_availability,
    reserve_discovered_album,
)
from .album_discovery_candidates import (
    AlbumDiscoveryCandidate,
    AlbumDiscoveryCandidates,
    album_discovery_candidate_allowed,
    discover_album_candidates,
)
from .album_discovery_catalog import AppleMusicCatalog, CatalogAlbum, CatalogError
from .album_discovery_delivery import AlbumReconciliation, reconcile_album
from .album_discovery_oracle import OracleAlbumClient, OracleAlbumState, cn_album_id
from .album_discovery_quota import (
    RESERVED,
    SHANGHAI,
    AlbumDownloadOrder,
    associate_album_download,
    get_album_download_order,
    mark_album_download_submitted,
    record_album_discovery_reuse,
    release_album_download_reservation,
    update_album_download_task,
)
from .album_discovery_settings import (
    AlbumDiscoveryConfig,
    AlbumDiscoveryRuntimeSettings,
    AlbumDiscoverySettingsError,
    load_album_discovery_apple_token,
    load_album_discovery_oracle_tokens,
    load_album_discovery_runtime,
    load_album_discovery_settings,
)
from .db import (
    AlbumDiscoveryOrder,
    AlbumDiscoveryPlacement,
    AlbumDiscoveryPreview,
    AlbumDiscoveryRelease,
    AlbumDiscoverySelection,
    User,
    connection_scope,
    db,
)


logger = logging.getLogger(__name__)
_STABLE_PREVIEW = {None, "no_candidates", "no_selection"}
_REUSABLE_REMOTE = {"associated", "submitted"}


class AlbumDiscoveryError(ValueError):
    """Discovery cannot complete the requested action."""


@dataclass(frozen=True)
class AlbumDiscoveryAssessment:
    candidate: AlbumDiscoveryCandidate
    action: str
    reconciliation: AlbumReconciliation
    remote: Optional[OracleAlbumState]


@dataclass(frozen=True)
class AlbumDiscoveryPreviewView:
    preview_id: Optional[UUID]
    day: date
    discovery: Optional[AlbumDiscoveryCandidates]
    assessments: Tuple[AlbumDiscoveryAssessment, ...]
    availability: AlbumDiscoveryAvailability


@dataclass(frozen=True)
class AlbumDiscoveryConfirmation:
    album_id: str
    status: str
    order_id: Optional[UUID] = None
    detail: str = ""


@dataclass(frozen=True)
class AlbumDiscoveryTrackedRelease:
    album: CatalogAlbum
    order: Optional[AlbumDownloadOrder]
    status: str
    detail: str
    reconciliation: Optional[AlbumReconciliation]


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _current_time(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).astimezone(SHANGHAI)
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(SHANGHAI)


def _release_album(release: AlbumDiscoveryRelease) -> CatalogAlbum:
    return CatalogAlbum.from_payload({
        "album_id": release.album_id,
        "title": release.title,
        "artist": release.artist,
        "url": release.album_url,
        **json.loads(release.expected_tracks),
    })


class AlbumDiscoveryService:
    def __init__(
        self,
        config: AlbumDiscoveryConfig,
        runtime: AlbumDiscoveryRuntimeSettings,
        *,
        catalog: Optional[AppleMusicCatalog],
        oracle: Optional[OracleAlbumClient],
    ) -> None:
        self.config = config
        self.runtime = runtime
        self.catalog = catalog
        self.oracle = oracle

    @classmethod
    def from_config(
        cls, config: AlbumDiscoveryConfig, *, with_catalog: bool = True
    ) -> "AlbumDiscoveryService":
        runtime = load_album_discovery_runtime(config)
        catalog = None
        if with_catalog:
            token = load_album_discovery_apple_token(config)
            if token:
                catalog = AppleMusicCatalog(token)
        oracle = None
        if runtime.oracle_url:
            try:
                access, delivery = load_album_discovery_oracle_tokens(config)
                if access and delivery:
                    oracle = OracleAlbumClient(runtime.oracle_url, access, delivery)
            except (ValueError, AlbumDiscoverySettingsError) as exc:
                logger.warning("Album discovery Oracle configuration unavailable: %s", type(exc).__name__)
        return cls(config, runtime, catalog=catalog, oracle=oracle)

    @property
    def library_key(self) -> str:
        return self.runtime.library_key

    def _check_user(self, user: User) -> None:
        if user.name != self.runtime.target_user:
            raise AlbumDiscoveryError("This account is not configured for album discovery.")

    def _get_release(self, album_id: str) -> Optional[AlbumDiscoveryRelease]:
        return AlbumDiscoveryRelease.get_or_none(
            AlbumDiscoveryRelease.library_key == self.library_key,
            AlbumDiscoveryRelease.album_id == album_id,
        )

    def _previous_tracks(self, release: Optional[AlbumDiscoveryRelease]) -> tuple:
        return tuple(json.loads(release.reconciliation).get("tracks", ())) if release else ()

    def _read_oracle(
        self, album: CatalogAlbum, order: Optional[AlbumDownloadOrder]
    ) -> Optional[OracleAlbumState]:
        if self.oracle is None or cn_album_id(album.url) != album.album_id:
            return None
        return self.oracle.inspect(
            album.album_id, album.url,
            task_id=order.oracle_task_id if order else None,
            order_id=str(order.id) if order and order.status != "associated" else None,
        )

    def _inventory(
        self,
        album: CatalogAlbum,
        representative: str,
        release: Optional[AlbumDiscoveryRelease],
        remote: Optional[OracleAlbumState],
        *,
        require_receipts: bool,
    ) -> AlbumReconciliation:
        return reconcile_album(
            album,
            representative,
            self.runtime.library_root,
            deliveries=remote.deliveries if remote else (),
            previous_tracks=self._previous_tracks(release),
            require_receipts=require_receipts,
        )

    def _assess(self, candidate: AlbumDiscoveryCandidate) -> AlbumDiscoveryAssessment:
        album = candidate.album
        with connection_scope():
            release = self._get_release(album.album_id)
        order = get_album_download_order(self.library_key, album.album_id)
        remote = self._read_oracle(album, order) if order else None
        inventory = self._inventory(
            album, candidate.representative_song_id, release, remote,
            require_receipts=order is not None,
        )
        if not inventory.ready and order is None:
            remote = self._read_oracle(album, None)
            if remote and remote.deliveries:
                inventory = self._inventory(
                    album, candidate.representative_song_id, release, remote,
                    require_receipts=False,
                )
        if inventory.ready:
            action = "reuse_local"
        elif order or (remote and remote.status in _REUSABLE_REMOTE):
            action = "reuse_oracle"
        elif remote and remote.status == "absent":
            action = "new_order"
        else:
            action = "wait_oracle"
        return AlbumDiscoveryAssessment(candidate, action, inventory, remote)

    def _load_preview(
        self, user: User, current: datetime, *, generate: bool
    ) -> Tuple[Optional[AlbumDiscoveryPreview], Optional[AlbumDiscoveryCandidates]]:
        selection_key = {
            "user": user,
            "library_key": self.library_key,
            "day": current.date(),
        }
        with connection_scope():
            row = AlbumDiscoveryPreview.get_or_none(**selection_key)
        if row is not None:
            return row, AlbumDiscoveryCandidates.from_payload(json.loads(row.payload))
        if not generate:
            return None, None
        if self.catalog is None:
            raise AlbumDiscoveryError("Apple Music developer token is required for discovery.")
        discovery = discover_album_candidates(self.config, user, self.catalog, now=current)
        if discovery.reason not in _STABLE_PREVIEW:
            return None, discovery
        with connection_scope():
            row, created = AlbumDiscoveryPreview.get_or_create(
                **selection_key, defaults={"payload": _json(discovery.to_payload())}
            )
        if not created:
            discovery = AlbumDiscoveryCandidates.from_payload(json.loads(row.payload))
        return row, discovery

    def preview(
        self, user: User, *, generate: bool = False, now: Optional[datetime] = None
    ) -> AlbumDiscoveryPreviewView:
        self._check_user(user)
        current = _current_time(now)
        row, discovery = self._load_preview(user, current, generate=generate)
        assessments = tuple(self._assess(item) for item in discovery.candidates) if discovery else ()
        return AlbumDiscoveryPreviewView(
            row.id if row else None, current.date(), discovery, assessments,
            get_album_discovery_availability(self.config, self.library_key, now=current),
        )

    def _save_selection(
        self, user: User, candidate: AlbumDiscoveryCandidate, priority: int,
        inventory: AlbumReconciliation,
    ) -> None:
        album = candidate.album
        catalog_payload = album.to_payload()
        for field in ("album_id", "title", "artist", "url"):
            del catalog_payload[field]
        expected = _json(catalog_payload)
        release, _ = AlbumDiscoveryRelease.get_or_create(
            library_key=self.library_key,
            album_id=album.album_id,
            defaults={
                "album_url": album.url, "title": album.title, "artist": album.artist,
                "expected_tracks": expected,
                "reconciliation": _json(inventory.to_payload()),
            },
        )
        if json.loads(release.expected_tracks)["tracks"] != catalog_payload["tracks"]:
            raise AlbumDiscoveryError("The confirmed China release track list has changed.")
        locked_user = User.select(User.id).where(User.id == user.id)
        if not isinstance(db.obj, SqliteDatabase):
            locked_user = locked_user.for_update()
        if locked_user.first() is None:
            raise AlbumDiscoveryError("This account no longer exists.")
        evidence = _json([item.to_payload() for item in candidate.evidence])
        selection, created = AlbumDiscoverySelection.get_or_create(
            release=release,
            user=user,
            defaults={
                "representative_song_id": candidate.representative_song_id,
                "evidence": evidence,
                "priority": priority,
            },
        )
        if not created and selection.representative_song_id != candidate.representative_song_id:
            if AlbumDiscoveryPlacement.select().where(
                AlbumDiscoveryPlacement.selection == selection
            ).exists():
                raise AlbumDiscoveryError("This release already appeared in daily recommendations.")
            selection.representative_song_id = candidate.representative_song_id
            selection.representative_track = None
            selection.evidence = evidence
            selection.priority = priority
            selection.save(only=[
                AlbumDiscoverySelection.representative_song_id,
                AlbumDiscoverySelection.representative_track,
                AlbumDiscoverySelection.evidence,
                AlbumDiscoverySelection.priority,
            ])

    def _save_inventory(
        self, release: AlbumDiscoveryRelease, inventory: AlbumReconciliation
    ) -> bool:
        payload = inventory.to_payload()
        matched = {
            track["song_id"]: UUID(track["track_id"])
            for track in payload["tracks"]
            if inventory.ready and track.get("track_id")
        }
        checked = datetime.now()
        with connection_scope(), db.atomic():
            updated = AlbumDiscoveryRelease.update(
                reconciliation=_json(payload),
                checked_at=checked,
                ready_at=(release.ready_at or checked) if inventory.ready else None,
            ).where(
                AlbumDiscoveryRelease.id == release.id,
                AlbumDiscoveryRelease.reconciliation == release.reconciliation,
                AlbumDiscoveryRelease.checked_at == release.checked_at,
            ).execute()
            if not updated:
                return False
            for selection in AlbumDiscoverySelection.select().where(AlbumDiscoverySelection.release == release):
                local_track = matched.get(selection.representative_song_id)
                if selection.representative_track_id != local_track:
                    (AlbumDiscoverySelection.update(representative_track=local_track)
                     .where(AlbumDiscoverySelection.id == selection.id).execute())
        return True

    def _save_remote_state(
        self, release: AlbumDiscoveryRelease, remote: OracleAlbumState,
        order: Optional[AlbumDownloadOrder],
    ) -> None:
        if order and remote.task_id:
            if order.status == RESERVED and remote.status == "submitted":
                mark_album_download_submitted(order, remote.task_id)
            elif order.status == RESERVED and remote.status == "associated":
                associate_album_download(order, remote.task_id)
            elif order.status != RESERVED and remote.task_id != order.oracle_task_id:
                update_album_download_task(order, remote.task_id)
        if order and order.status == RESERVED and remote.status == "rejected":
            release_album_download_reservation(order)
        with connection_scope():
            AlbumDiscoveryRelease.update(
                oracle_status=remote.status,
                oracle_day=order.quota_date if order else None,
                oracle_detail=remote.detail or remote.task_status or "",
            ).where(AlbumDiscoveryRelease.id == release.id).execute()

    def _recheck(self, user: User, candidate: AlbumDiscoveryCandidate) -> AlbumDiscoveryCandidate:
        if self.catalog is None:
            raise AlbumDiscoveryError("Apple Music developer token is required for confirmation.")
        try:
            live = self.catalog.get_release(candidate.album.album_id, refresh=True)
        except CatalogError as exc:
            raise AlbumDiscoveryError("China Apple Music catalog is temporarily unavailable.") from exc
        song_ids = {track.song_id for track in live.tracks} if live else set()
        if (
            live is None
            or live.album_id != candidate.album.album_id
            or cn_album_id(live.url) != live.album_id
            or candidate.representative_song_id not in song_ids
        ):
            raise AlbumDiscoveryError("The selected representative is unavailable on this China release.")
        verified = replace(
            candidate, album=live,
            evidence=tuple(item for item in candidate.evidence if item.song_id in song_ids),
        )
        with connection_scope():
            if not album_discovery_candidate_allowed(user, verified):
                raise AlbumDiscoveryError("The selected album no longer matches current feedback.")
        return verified

    def _confirm_one(
        self, user: User, candidate: AlbumDiscoveryCandidate,
        priority: int, *, automatic: bool, preview_day: date, now: Optional[datetime],
    ) -> AlbumDiscoveryConfirmation:
        verified = self._recheck(user, candidate)
        album = verified.album
        assessment = self._assess(verified)

        def persist() -> None:
            if _current_time(now).date() != preview_day:
                raise AlbumDiscoveryError("The Shanghai date changed. Run a new preview.")
            self._save_selection(user, verified, priority, assessment.reconciliation)

        order = get_album_download_order(self.library_key, album.album_id)
        remote = assessment.remote
        if order or assessment.action in ("reuse_local", "reuse_oracle"):
            task_id = remote.task_id if remote and remote.status in _REUSABLE_REMOTE else None
            order = record_album_discovery_reuse(
                self.library_key, album.album_id, persist, oracle_task_id=task_id, now=now
            )
        elif assessment.action == "new_order":
            reservation = reserve_discovered_album(
                self.config, self.library_key, album.album_id, automatic=automatic,
                now=now, persist_selection=persist,
            )
            order = reservation.order
            if order is None:
                return AlbumDiscoveryConfirmation(album.album_id, reservation.status)
            if reservation.status == RESERVED and self.oracle is not None:
                remote = self.oracle.submit(album.album_id, album.url, str(order.id))
        else:
            return AlbumDiscoveryConfirmation(
                album.album_id, "wait_oracle", detail=(remote.detail or "") if remote else "",
            )
        with connection_scope():
            release = self._get_release(album.album_id)
        if remote is not None:
            self._save_remote_state(release, remote, order)
        inventory = self._inventory(
            album, verified.representative_song_id, release, remote,
            require_receipts=order is not None,
        )
        self._save_inventory(release, inventory)
        status = "ready" if inventory.ready else (remote.status if remote else "pending")
        if status == "absent":
            status = "pending"
        return AlbumDiscoveryConfirmation(
            album.album_id, status, order.id if order else None,
            (remote.detail or "") if remote else "",
        )

    def confirm(
        self, user: User, preview_id: UUID, album_id: str, *,
        representative_song_id: Optional[str] = None, now: Optional[datetime] = None,
    ) -> AlbumDiscoveryConfirmation:
        self._check_user(user)
        current = _current_time(now)
        with connection_scope():
            row = AlbumDiscoveryPreview.get_or_none(
                AlbumDiscoveryPreview.id == preview_id,
                AlbumDiscoveryPreview.user == user,
                AlbumDiscoveryPreview.library_key == self.library_key,
                AlbumDiscoveryPreview.day == current.date(),
            )
        if row is None:
            raise AlbumDiscoveryError("Run a new preview for this account and date.")
        discovery = AlbumDiscoveryCandidates.from_payload(json.loads(row.payload))
        for priority, candidate in enumerate(discovery.candidates):
            if candidate.album.album_id == album_id:
                chosen = representative_song_id or candidate.representative_song_id
                if chosen != candidate.representative_song_id:
                    if not any(item.selected and item.song_id == chosen for item in candidate.evidence):
                        raise AlbumDiscoveryError("The alternate representative is not in this preview.")
                    candidate = replace(candidate, representative_song_id=chosen)
                return self._confirm_one(
                    user, candidate, priority, automatic=False, preview_day=current.date(), now=now,
                )
        raise AlbumDiscoveryError("The selected China release is not in this preview.")

    def tracked_releases(self, user: User) -> Tuple[AlbumDiscoveryTrackedRelease, ...]:
        self._check_user(user)
        with connection_scope():
            releases = list(
                AlbumDiscoveryRelease.select().join(AlbumDiscoverySelection).where(
                    AlbumDiscoverySelection.user == user,
                    AlbumDiscoveryRelease.library_key == self.library_key,
                ).order_by(AlbumDiscoverySelection.created_at.desc()).limit(30)
            )
        return tuple(
            AlbumDiscoveryTrackedRelease(
                _release_album(release),
                get_album_download_order(self.library_key, release.album_id),
                "ready" if release.ready_at else release.oracle_status or "pending",
                release.oracle_detail,
                AlbumReconciliation.from_payload(json.loads(release.reconciliation))
                if release.reconciliation != "{}" else None,
            )
            for release in releases
        )

    def run_nightly(self, *, now: Optional[datetime] = None) -> dict:
        current = _current_time(now)
        if current.hour < self.runtime.auto_submit_hour:
            return {"status": "waiting"}
        settings = load_album_discovery_settings(self.config)
        if not settings.auto_submit:
            return {"status": "paused"}
        with connection_scope():
            user = User.get_or_none(User.name == self.runtime.target_user)
        if user is None:
            return {"status": "target_user_missing"}
        _, discovery = self._load_preview(user, current, generate=True)
        if not discovery or not discovery.candidates:
            return {"status": discovery.reason if discovery else "no_candidates"}
        counts: Dict[str, int] = {}
        for priority, candidate in enumerate(discovery.candidates):
            if _current_time(now).date() != current.date():
                break
            with connection_scope():
                selected_release = (AlbumDiscoveryRelease.select().join(AlbumDiscoverySelection)
                    .where(
                        AlbumDiscoverySelection.user == user,
                        AlbumDiscoveryRelease.library_key == self.library_key,
                        AlbumDiscoveryRelease.album_id == candidate.album.album_id,
                    ).first())
            if selected_release is not None:
                order = get_album_download_order(self.library_key, candidate.album.album_id)
                if (
                    order is not None or selected_release.ready_at is not None
                    or selected_release.oracle_day == current.date()
                ):
                    continue
            try:
                outcome = self._confirm_one(
                    user, candidate, priority, automatic=True, preview_day=current.date(), now=now,
                )
            except AlbumDiscoveryError as exc:
                logger.info("Album discovery skipped album %s: %s", candidate.album.album_id, exc)
                continue
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
            # Later candidates may already be local or in flight and need no new slot.
            if outcome.status == "submission_disabled":
                break
        return {"status": "complete", **counts}

    def reconcile_orders(self) -> dict:
        with connection_scope():
            reserved = AlbumDiscoveryOrder.select(AlbumDiscoveryOrder.album_id).where(
                AlbumDiscoveryOrder.library_key == self.library_key,
                AlbumDiscoveryOrder.status == RESERVED,
            )
            releases = list(
                AlbumDiscoveryRelease.select()
                .where(
                    AlbumDiscoveryRelease.library_key == self.library_key,
                    AlbumDiscoveryRelease.ready_at.is_null(True)
                    | AlbumDiscoveryRelease.album_id.in_(reserved),
                )
                .order_by(
                    AlbumDiscoveryRelease.checked_at.is_null(True).desc(),
                    AlbumDiscoveryRelease.checked_at.asc(), AlbumDiscoveryRelease.created_at.asc(),
                )
                .limit(30)
            )
        checked = ready = 0
        for release in releases:
            with connection_scope():
                selection = (AlbumDiscoverySelection.select()
                             .where(AlbumDiscoverySelection.release == release)
                             .order_by(AlbumDiscoverySelection.created_at).first())
            if selection is None:
                continue
            album = _release_album(release)
            order = get_album_download_order(self.library_key, album.album_id)
            remote = self._read_oracle(album, order) if order else None
            if order and order.status == RESERVED and remote and remote.status == "absent":
                if release.oracle_status == "unknown":
                    remote = self.oracle.close_if_unaccepted(album.album_id, album.url, str(order.id))
                else:
                    # Recover a reservation interrupted before an outcome was recorded.
                    remote = self.oracle.submit(album.album_id, album.url, str(order.id))
            if remote:
                self._save_remote_state(release, remote, order)
            inventory = self._inventory(
                album, selection.representative_song_id, release, remote,
                require_receipts=order is not None,
            )
            checked += 1
            ready += int(self._save_inventory(release, inventory) and inventory.ready)
        return {"checked": checked, "ready": ready}
