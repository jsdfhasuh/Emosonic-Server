"""Persistent, fail-closed spending reservations for metadata API calls."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re
import time
from typing import Callable, Mapping, Optional
from uuid import uuid4

from peewee import DatabaseError

from .db_layer.core import Meta


MICRO_USD = 1_000_000
MAX_MONEY = 10 ** 15
MAX_ATTEMPTS = 12
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
_KIND = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_TOKEN = re.compile(r"[0-9a-f]{26}\Z")


class BudgetUnavailable(Exception):
    """Accounting/configuration could not safely authorize a paid request."""


class BudgetExceeded(Exception):
    def __init__(self, reason: str, reset_at: datetime) -> None:
        super().__init__("Metadata API spending budget is exhausted or blocked.")
        self.reason = reason
        self.reset_at = reset_at
        self.next_retry_at = reset_at


@dataclass(frozen=True)
class BudgetReservation:
    token: str
    month_key: str
    day_key: str
    amount_micro_usd: int


class _RetryTransaction(Exception):
    pass


def _money(value: object, *, positive: bool = False) -> int:
    if isinstance(value, bool):
        raise BudgetUnavailable("Invalid metadata budget amount.")
    try:
        amount = Decimal(str(value)) * MICRO_USD
        if not amount.is_finite() or amount != amount.to_integral_value():
            raise ValueError
        result = int(amount)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise BudgetUnavailable("Invalid metadata budget amount.") from None
    _check_amount(result, positive=positive)
    return result


def _check_amount(value: object, *, positive: bool = False) -> None:
    if type(value) is not int or not (int(positive) <= value <= MAX_MONEY):
        raise BudgetUnavailable("Invalid metadata budget amount.")


def _encode(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 256:
        raise BudgetUnavailable("Metadata budget record exceeds storage bounds.")
    return encoded


def _decode(raw: str, fields: set) -> dict:
    try:
        result = json.loads(raw)
    except (ValueError, TypeError):
        raise BudgetUnavailable("Metadata budget record is invalid.") from None
    if (not isinstance(result, dict) or set(result) != fields
            or type(result.get("v")) is not int or result["v"] != 1):
        raise BudgetUnavailable("Metadata budget record is invalid.")
    return result


def _bucket(raw: str) -> dict:
    result = _decode(raw, {"v", "s", "r", "l", "b"})
    for field in ("s", "r", "l"):
        _check_amount(result[field], positive=field == "l")
    if type(result["b"]) is not bool:
        raise BudgetUnavailable("Metadata budget record is invalid.")
    return result


def _request(raw: str) -> dict:
    result = _decode(raw, {"v", "m", "d", "k", "r", "a"})
    try:
        date = datetime.strptime(result["d"], "%Y%m%d")
        valid = (
            date.strftime("%Y%m%d") == result["d"]
            and date.strftime("%Y%m") == result["m"]
            and bool(_KIND.fullmatch(result["k"]))
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise BudgetUnavailable("Metadata budget reservation is invalid.")
    _check_amount(result["r"], positive=True)
    if result["a"] is not None:
        _check_amount(result["a"])
    return result


class MetadataBudget:
    """Share monthly/daily caps across processes through transactional Meta rows.

    Limits may decrease, but never automatically increase within an existing
    period. Unsettled reservations are deliberately not expired or refunded.
    """

    def __init__(
        self,
        config: Mapping[str, object],
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.monthly_limit = _money(config.get("monthly_budget_usd"), positive=True)
        self.safety_buffer = _money(config.get("budget_safety_buffer_usd", 3))
        self.daily_limit = _money(config.get("daily_budget_usd", 2), positive=True)
        if self.safety_buffer >= self.monthly_limit:
            raise BudgetUnavailable("Metadata budget safety buffer must be below its limit.")
        if config.get("budget_timezone", "Asia/Shanghai") != "Asia/Shanghai":
            raise BudgetUnavailable("Unsupported metadata budget timezone.")
        self.effective_monthly_limit = self.monthly_limit - self.safety_buffer
        self.clock = clock or (lambda: datetime.now(SHANGHAI))

    def _period(self) -> tuple:
        current = self.clock()
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise BudgetUnavailable("Metadata budget clock must be timezone-aware.")
        current = current.astimezone(SHANGHAI)
        day_reset = (current + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        month_reset = (current.replace(day=28) + timedelta(days=4)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0,
        )
        return current.strftime("%Y%m"), current.strftime("%Y%m%d"), month_reset, day_reset

    @staticmethod
    def _read(key: str) -> Optional[str]:
        row = Meta.get_or_none(Meta.key == key)
        return None if row is None else row.value

    @staticmethod
    def _cas(key: str, old: str, new: dict) -> None:
        encoded = _encode(new)
        if encoded == old:
            return
        changed = Meta.update(value=encoded).where(Meta.key == key, Meta.value == old).execute()
        if changed != 1:
            raise _RetryTransaction

    def _run_transaction(self, operation: Callable) -> object:
        if Meta._meta.database.in_transaction():
            raise BudgetUnavailable(
                "Metadata budget requires its own committed transaction.",
            )
        for attempt in range(MAX_ATTEMPTS):
            try:
                with Meta._meta.database.atomic():
                    return operation()
            except (_RetryTransaction, DatabaseError):
                if attempt + 1 < MAX_ATTEMPTS:
                    time.sleep(min(0.002 * (attempt + 1), 0.02))
        raise BudgetUnavailable(
            "Metadata budget storage is unavailable; no request authorized.",
        ) from None

    def reserve(self, kind: str, amount_micro_usd: int) -> BudgetReservation:
        """Atomically charge both period counters before returning authorization."""
        _check_amount(amount_micro_usd, positive=True)
        if not isinstance(kind, str) or not _KIND.fullmatch(kind):
            raise BudgetUnavailable("Invalid metadata budget request kind.")
        month, day, month_reset, day_reset = self._period()
        reservation = BudgetReservation(uuid4().hex[:26], "mb:m:" + month, "mb:d:" + day,
                                        amount_micro_usd)
        request_key = "mb:r:" + reservation.token
        request = {"v": 1, "m": month, "d": day, "k": kind, "r": amount_micro_usd, "a": None}

        def operation() -> object:
            previous = self._read(request_key)
            if previous is not None:
                # A lost commit acknowledgement must not reserve the same call twice.
                if _request(previous) != request:
                    raise BudgetUnavailable("Metadata budget reservation collision.")
                return reservation
            periods = (
                (reservation.month_key, self.effective_monthly_limit, month_reset, "month"),
                (reservation.day_key, self.daily_limit, day_reset, "day"),
            )
            states = []
            denied = None
            for key, limit, reset_at, label in periods:
                initial = {"v": 1, "s": 0, "r": 0, "l": limit, "b": False}
                Meta.insert(key=key, value=_encode(initial)).on_conflict_ignore().execute()
                raw = self._read(key)
                if raw is None:
                    # A concurrent insert can be invisible in MySQL's current snapshot.
                    raise _RetryTransaction
                state = _bucket(raw)
                state["l"] = min(state["l"], limit)
                self._cas(key, raw, state)
                raw = _encode(state)
                if state["b"] or state["s"] + state["r"] + amount_micro_usd > state["l"]:
                    denied = denied or BudgetExceeded(
                        label + ("_blocked" if state["b"] else "_limit"), reset_at,
                    )
                states.append((key, raw, state))
            if denied is not None:
                # Persist tighter caps even when they deny this reservation.
                return denied
            for key, raw, state in states:
                state["r"] += amount_micro_usd
                self._cas(key, raw, state)
            Meta.create(key=request_key, value=_encode(request))
            return reservation

        result = self._run_transaction(operation)
        if isinstance(result, BudgetExceeded):
            raise result
        return result

    def settle(self, reservation: BudgetReservation, amount_micro_usd: int) -> None:
        """Settle once; keep reservations unchanged if accounting cannot commit."""
        _check_amount(amount_micro_usd)
        if (not isinstance(reservation, BudgetReservation)
                or not _TOKEN.fullmatch(reservation.token)):
            raise BudgetUnavailable("Invalid metadata budget reservation.")
        request_key = "mb:r:" + reservation.token

        def operation() -> None:
            raw_request = self._read(request_key)
            if raw_request is None:
                raise BudgetUnavailable("Metadata budget reservation is missing.")
            request = _request(raw_request)
            if (reservation.month_key != "mb:m:" + request["m"]
                    or reservation.day_key != "mb:d:" + request["d"]
                    or reservation.amount_micro_usd != request["r"]):
                raise BudgetUnavailable("Metadata budget reservation does not match.")
            if request["a"] is not None:
                if request["a"] != amount_micro_usd:
                    raise BudgetUnavailable(
                        "Metadata budget settlement already has another amount.",
                    )
                return
            for key in (reservation.month_key, reservation.day_key):
                raw = self._read(key)
                if raw is None:
                    raise BudgetUnavailable("Metadata budget period is missing.")
                state = _bucket(raw)
                if state["r"] < request["r"]:
                    raise BudgetUnavailable("Metadata budget reserved total is inconsistent.")
                state["r"] -= request["r"]
                state["s"] += amount_micro_usd
                state["b"] = state["b"] or amount_micro_usd > request["r"]
                _check_amount(state["s"])
                self._cas(key, raw, state)
            request["a"] = amount_micro_usd
            self._cas(request_key, raw_request, request)

        self._run_transaction(operation)

    def snapshot(self) -> dict:
        """Read safe accounting totals without resetting or creating counters."""
        month, day, month_reset, day_reset = self._period()

        def operation() -> dict:
            result = {
                "month": month, "day": day, "timezone": "Asia/Shanghai",
                "monthly_limit_micro_usd": self.monthly_limit,
                "safety_buffer_micro_usd": self.safety_buffer,
                "month_reset_at": month_reset.isoformat(),
                "day_reset_at": day_reset.isoformat(),
                "blocked": False,
            }
            for label, key, limit in (
                ("month", "mb:m:" + month, self.effective_monthly_limit),
                ("day", "mb:d:" + day, self.daily_limit),
            ):
                raw = self._read(key)
                state = (
                    _bucket(raw) if raw is not None
                    else {"s": 0, "r": 0, "l": limit, "b": False}
                )
                effective = min(state["l"], limit)
                result[label + "_limit_micro_usd"] = effective
                result[label + "_spent_micro_usd"] = state["s"]
                result[label + "_reserved_micro_usd"] = state["r"]
                result[label + "_remaining_micro_usd"] = max(0, effective - state["s"] - state["r"])
                result["blocked"] = result["blocked"] or state["b"]
            return result

        return self._run_transaction(operation)
