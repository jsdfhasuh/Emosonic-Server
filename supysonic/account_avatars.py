"""Persistent account portraits shared by the web editor and daily scheduler."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from .metadata_budget import SHANGHAI
from .openai_recommendations import recommendation_lease

AVATAR_MAX_BYTES = 4 * 1024 * 1024
AVATAR_SIZE = (384, 384)


def account_avatar_path(config, user) -> Path:
    base = config["BASE"] if isinstance(config, Mapping) else config.BASE
    web = config["WEBAPP"] if isinstance(config, Mapping) else config.WEBAPP
    folder = base.get("tempdatafolder") or web["cache_dir"]
    return (Path(folder) / "account-avatars" / f"{user.id}.png").absolute()


def avatar_png(data: bytes) -> bytes:
    if not data or len(data) > AVATAR_MAX_BYTES:
        raise ValueError("Avatar upload has an invalid size")
    with Image.open(io.BytesIO(data), formats=["JPEG", "PNG", "WEBP"]) as source:
        if max(source.size) > 4096 or getattr(source, "n_frames", 1) > 1:
            raise ValueError("Avatar must be a bounded, static image")
        with ImageOps.exif_transpose(source) as oriented, oriented.convert("RGBA") as rgba:
            with ImageOps.fit(rgba, AVATAR_SIZE, Image.Resampling.LANCZOS) as image:
                image.info.clear()
                with io.BytesIO() as output:
                    image.save(output, format="PNG")
                    return output.getvalue()


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publish_avatar(path: Path, *, data: bytes | None = None, pending: Path | None = None) -> None:
    """Archive the exact previous file before applying, removing or consuming a staged image."""
    with recommendation_lease(f"account-avatar-write:{path.stem}") as acquired:
        if not acquired:
            raise BlockingIOError("Avatar is being updated")
        if pending is not None and not pending.exists():
            return
        if path.is_file():
            previous = path.read_bytes()
            if previous == data:
                return
            stamp = datetime.fromtimestamp(path.stat().st_mtime, SHANGHAI).strftime("%Y-%m-%d_%H%M%S")
            digest = hashlib.sha256(previous).hexdigest()[:16]
            history = path.parent / "history" / path.stem / f"{stamp}-{digest}.png"
            write_atomic(history, previous)
        if pending is not None:
            os.replace(pending, path)
        elif data is not None:
            write_atomic(path, data)
        else:
            path.unlink(missing_ok=True)
