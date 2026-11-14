"""Durable transient provider backoff, independent of per-track history."""

import json
import time
from datetime import datetime
from typing import Optional
from uuid import uuid4

from peewee import DatabaseError

from .db import Meta
from .metadata_budget import BudgetUnavailable


BACKOFF_KEY = "metadata_provider_backoff"
MAX_FAILURE_COUNT = 4
MAX_RETRY_HINT_SECONDS = 86400


class _RetryTransaction(Exception):
    pass


def decode_backoff(raw: Optional[str]) -> dict:
    if raw is None:
        return {"v": 1, "n": 0, "u": 0, "g": ""}
    try:
        value = json.loads(raw)
        valid = (isinstance(value, dict) and set(value) == {"v", "n", "u", "g"}
                 and type(value["v"]) is int and value["v"] == 1
                 and type(value["n"]) is int and 1 <= value["n"] <= MAX_FAILURE_COUNT
                 and type(value["u"]) is int and 0 <= value["u"] <= 253402300799
                 and isinstance(value["g"], str) and len(value["g"]) == 32
                 and all(char in "0123456789abcdef" for char in value["g"]))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise BudgetUnavailable("Metadata provider backoff state is invalid.")
    return value


def read_backoff() -> Optional[str]:
    row = Meta.get_or_none(Meta.key == BACKOFF_KEY)
    raw = row.value if row else None
    decode_backoff(raw)
    return raw


def record_transient_failure(retry_after: int = 0) -> tuple:
    """Commit a shared consecutive failure before authorizing another batch."""
    if Meta._meta.database.in_transaction():
        raise BudgetUnavailable("Metadata provider backoff requires its own transaction.")
    if type(retry_after) is not int or not 0 <= retry_after <= MAX_RETRY_HINT_SECONDS:
        raise BudgetUnavailable("Invalid metadata provider retry hint.")
    generation = uuid4().hex
    for attempt in range(8):
        try:
            with Meta._meta.database.atomic():
                raw = read_backoff()
                previous = decode_backoff(raw)
                if previous["g"] == generation:
                    return datetime.fromtimestamp(previous["u"]), previous["n"]
                count = min(previous["n"] + 1, MAX_FAILURE_COUNT)
                delay = max(min(300 * 2 ** (count - 1), 1800), retry_after)
                deadline = max(int(time.time()) + delay, previous["u"])
                value = {"v": 1, "n": count, "u": deadline, "g": generation}
                encoded = json.dumps(value, separators=(",", ":"), sort_keys=True)
                if raw is None:
                    Meta.insert(key=BACKOFF_KEY, value=encoded).on_conflict_ignore().execute()
                    if read_backoff() != encoded:
                        raise _RetryTransaction
                elif Meta.update(value=encoded).where(
                        Meta.key == BACKOFF_KEY, Meta.value == raw).execute() != 1:
                    raise _RetryTransaction
                return datetime.fromtimestamp(deadline), count
        except (_RetryTransaction, DatabaseError):
            if attempt < 7:
                time.sleep(0.002 * (attempt + 1))
    raise BudgetUnavailable("Metadata provider backoff could not be persisted.")


def reset_backoff(observed: Optional[str]) -> None:
    """A successful call cannot erase a failure committed by a newer call."""
    decode_backoff(observed)
    if observed is not None:
        Meta.delete().where(Meta.key == BACKOFF_KEY, Meta.value == observed).execute()
