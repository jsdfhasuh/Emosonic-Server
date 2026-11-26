"""Persistent quota for new China storefront album orders."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterator, Optional, Tuple
from uuid import UUID

from peewee import MySQLDatabase, SqliteDatabase, fn

from .db import AlbumDiscoveryOrder, AlbumDiscoveryQuotaLock, connection_scope, db


STOREFRONT = "cn"
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
RESERVED = "reserved"
SUBMITTED = "submitted"
ASSOCIATED = "associated"


@dataclass(frozen=True)
class AlbumDownloadQuota:
    library_key: str
    quota_date: date
    limit: int
    reserved: int
    submitted: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.reserved - self.submitted)


@dataclass(frozen=True)
class AlbumDownloadOrder:
    id: UUID
    library_key: str
    album_id: str
    quota_date: date
    status: str
    oracle_task_id: Optional[str]


@dataclass(frozen=True)
class AlbumReservationResult:
    status: str
    order: Optional[AlbumDownloadOrder]
    quota: AlbumDownloadQuota


def _validate_text(value: str, field: str, max_length: int) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= max_length
        or value != value.strip()
        or "\0" in value
    ):
        raise ValueError("Invalid " + field)


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or limit < 0:
        raise ValueError("max_albums_per_day must be a nonnegative integer")


def _shanghai_date(now: Optional[datetime]) -> date:
    if now is None:
        return datetime.now(SHANGHAI).date()
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(SHANGHAI).date()


@contextmanager
def _write_transaction(library_key: str) -> Iterator[None]:
    if db.in_transaction():
        raise RuntimeError("Album discovery quota requires an outermost transaction")
    sqlite = isinstance(db.obj, SqliteDatabase)
    transaction = db.atomic("IMMEDIATE") if sqlite else db.atomic()
    with connection_scope(), transaction:
        if not sqlite:
            # The upsert exclusively locks the library across dates and processes.
            lock = AlbumDiscoveryQuotaLock
            query = lock.insert(library_key=library_key)
            update = {lock.library_key: library_key}
            if isinstance(db.obj, MySQLDatabase):
                query.on_conflict(update=update).execute()
            else:
                query.on_conflict(
                    conflict_target=[lock.library_key], update=update
                ).execute()
        yield


def _get_quota(library_key: str, quota_date: date, limit: int) -> AlbumDownloadQuota:
    orders = AlbumDiscoveryOrder
    counts = dict(
        orders.select(orders.status, fn.COUNT(orders.id))
        .where(
            (orders.library_key == library_key)
            & (orders.quota_date == quota_date)
            & orders.status.in_((RESERVED, SUBMITTED))
        )
        .group_by(orders.status)
        .tuples()
    )
    return AlbumDownloadQuota(
        library_key,
        quota_date,
        limit,
        counts.get(RESERVED, 0),
        counts.get(SUBMITTED, 0),
    )


def get_album_download_quota(
    library_key: str, max_albums_per_day: int, *, now: Optional[datetime] = None
) -> AlbumDownloadQuota:
    """Read today's count without reserving a slot."""
    _validate_text(library_key, "library_key", 128)
    _validate_limit(max_albums_per_day)
    with connection_scope():
        return _get_quota(library_key, _shanghai_date(now), max_albums_per_day)


def _order_from_row(row: AlbumDiscoveryOrder) -> AlbumDownloadOrder:
    return AlbumDownloadOrder(
        row.id,
        row.library_key,
        row.album_id,
        row.quota_date,
        row.status,
        row.oracle_task_id,
    )


def reserve_album_download(
    library_key: str,
    album_id: str,
    load_policy: Callable[[], Tuple[int, bool]],
    *,
    now: Optional[datetime] = None,
) -> AlbumReservationResult:
    """Reserve a slot before dispatch. Only a new reservation permits dispatch."""
    _validate_text(library_key, "library_key", 128)
    _validate_text(album_id, "album_id", 64)
    if not album_id.isascii() or not album_id.isdecimal() or album_id.startswith("0"):
        raise ValueError("album_id must be a canonical Apple Music album ID")

    with _write_transaction(library_key):
        max_albums_per_day, allow_new = load_policy()
        _validate_limit(max_albums_per_day)
        if type(allow_new) is not bool:
            raise ValueError("allow_new must be a boolean")
        quota_date = _shanghai_date(now)
        quota = _get_quota(library_key, quota_date, max_albums_per_day)
        existing = AlbumDiscoveryOrder.get_or_none(
            (AlbumDiscoveryOrder.library_key == library_key)
            & (AlbumDiscoveryOrder.storefront == STOREFRONT)
            & (AlbumDiscoveryOrder.album_id == album_id)
        )
        if existing is not None:
            return AlbumReservationResult("existing", _order_from_row(existing), quota)
        if not allow_new:
            return AlbumReservationResult("submission_disabled", None, quota)
        if quota.remaining == 0:
            return AlbumReservationResult("limit_reached", None, quota)

        order = AlbumDiscoveryOrder.create(
            library_key=library_key,
            storefront=STOREFRONT,
            album_id=album_id,
            quota_date=quota_date,
            status=RESERVED,
        )
        return AlbumReservationResult(
            RESERVED,
            _order_from_row(order),
            AlbumDownloadQuota(
                library_key,
                quota_date,
                quota.limit,
                quota.reserved + 1,
                quota.submitted,
            ),
        )


def _finish_reservation(
    order: AlbumDownloadOrder, status: str, oracle_task_id: str
) -> bool:
    _validate_text(oracle_task_id, "oracle_task_id", 128)
    with _write_transaction(order.library_key):
        row = AlbumDiscoveryOrder.get_or_none(
            (AlbumDiscoveryOrder.id == order.id)
            & (AlbumDiscoveryOrder.library_key == order.library_key)
        )
        if row is None:
            return False
        if row.status != RESERVED:
            return row.status == status and row.oracle_task_id == oracle_task_id
        row.status = status
        row.oracle_task_id = oracle_task_id
        row.save(only=[AlbumDiscoveryOrder.status, AlbumDiscoveryOrder.oracle_task_id])
        return True


def mark_album_download_submitted(
    order: AlbumDownloadOrder, oracle_task_id: str
) -> bool:
    """Convert the original day's reservation into a confirmed new order."""
    return _finish_reservation(order, SUBMITTED, oracle_task_id)


def associate_album_download(order: AlbumDownloadOrder, oracle_task_id: str) -> bool:
    """Link an existing Oracle task and free this order's quota slot."""
    return _finish_reservation(order, ASSOCIATED, oracle_task_id)


def release_album_download_reservation(order: AlbumDownloadOrder) -> bool:
    """Free a slot only after confirming Oracle did not accept this reservation."""
    with _write_transaction(order.library_key):
        return bool(
            AlbumDiscoveryOrder.delete()
            .where(
                (AlbumDiscoveryOrder.id == order.id)
                & (AlbumDiscoveryOrder.library_key == order.library_key)
                & (AlbumDiscoveryOrder.status == RESERVED)
            )
            .execute()
        )
