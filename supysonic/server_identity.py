"""Persistent logical deployment identity; not a secret or an authentication key."""

import base64
import binascii
import secrets

from .db import Meta


SERVER_IDENTITY_KEY = "server_identity_v1"


def get_server_identity() -> str:
    # The primary key and get_or_create handle simultaneous first requests from
    # multiple workers. Keep identity with the database across container rebuilds.
    identity, _ = Meta.get_or_create(
        key=SERVER_IDENTITY_KEY,
        defaults={"value": base64.b64encode(secrets.token_bytes(32)).decode("ascii")},
    )
    value = identity.value
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise RuntimeError(
            "Stored server identity is invalid; refusing to rotate it"
        ) from error
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        raise RuntimeError("Stored server identity is invalid; refusing to rotate it")
    return value
