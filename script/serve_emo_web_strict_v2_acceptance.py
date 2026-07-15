#!/usr/bin/env python3

import argparse
import logging
import math
import os
import re
import struct
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("EMO_SOCKETIO_ASYNC_MODE", "threading")
server_build_commit = os.environ.get("EMO_SERVER_BUILD_COMMIT", "")
if re.fullmatch(r"[0-9a-f]{40}", server_build_commit) is None:
    raise RuntimeError(
        "EMO_SERVER_BUILD_COMMIT must identify the exact 40-character acceptance build"
    )

from supysonic.db import Album, Artist, Folder, Track, User, close_connection
from supysonic.managers.user import UserManager
from supysonic.web import create_application
from tests.testbase import TestConfig


def _write_tone(path: Path, frequency: float, duration_seconds: int = 90) -> None:
    if path.exists():
        return
    sample_rate = 8000
    amplitude = 5000
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        for offset in range(0, sample_rate * duration_seconds, 1024):
            frame_count = min(1024, sample_rate * duration_seconds - offset)
            frames = bytearray()
            for index in range(frame_count):
                sample_index = offset + index
                value = int(
                    amplitude
                    * math.sin(2 * math.pi * frequency * sample_index / sample_rate)
                )
                frames.extend(struct.pack("<h", value))
            audio.writeframes(frames)


def _seed_library(state_dir: Path) -> None:
    media_dir = state_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    first_path = media_dir / "strict-one.wav"
    second_path = media_dir / "strict-two.wav"
    _write_tone(first_path, 440)
    _write_tone(second_path, 660)

    if not User.select().where(User.name == "alice").exists():
        UserManager.add("alice", "Alic3", admin=True)
    if Track.select().exists():
        return

    root = Folder.create(root=True, name="Strict Browser Root", path=str(media_dir))
    artist = Artist.create(name="Strict Browser Artist")
    album = Album.create(name="Strict Browser Album", artist=artist)
    for number, (title, media_path) in enumerate(
        (("Strict One", first_path), ("Strict Two", second_path)),
        start=1,
    ):
        Track.create(
            disc=1,
            number=number,
            title=title,
            duration=90,
            has_art=False,
            album=album,
            artist=artist,
            genre="test",
            bitrate=128,
            path=str(media_path),
            last_modification=1,
            root_folder=root,
            folder=root,
        )


def create_acceptance_application(state_dir: Path, port: int):
    state_dir.mkdir(parents=True, exist_ok=True)
    config = TestConfig(True, True)
    config.BASE["database_uri"] = "sqlite:///" + str(state_dir / "acceptance.db")
    config.WEBAPP.update(
        {
            "cache_dir": str(state_dir / "cache"),
            "mount_emosonic": True,
            "mount_client_releases": False,
            "emo_allowed_origins": f"http://127.0.0.1:{port}",
            "emo_socketio_ping_interval": 2,
            "emo_socketio_ping_timeout": 2,
            "emo_strict_requests_per_connection_per_minute": 600,
            "emo_strict_handoff_starts_per_connection_per_minute": 60,
            "emo_strict_rate_limit_load_test_evidence": (
                "local-test-only: 30-sample strict web Handoff acceptance"
            ),
            "emo_strict_v2_core_enabled": True,
            "emo_strict_v2_follow_enabled": True,
            "emo_strict_v2_handoff_enabled": True,
            "emo_strict_v2_broadcast_enabled": True,
            "emo_web_realtime_protocol": "strict_v2",
            "emo_web_strict_v2_follow_enabled": True,
            "emo_web_strict_v2_handoff_enabled": True,
            "emo_web_strict_v2_broadcast_enabled": True,
            "emo_web_strict_v2_acceptance_mode": True,
        }
    )
    config.MIMETYPES["wav"] = "audio/wav"
    app = create_application(config)
    with app.app_context():
        _seed_library(state_dir)
    close_connection()
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=5081)
    args = parser.parse_args()

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app = create_acceptance_application(args.state_dir, args.port)
    from supysonic.emo.ws import socketio

    socketio.run(
        app,
        host="127.0.0.1",
        port=args.port,
        allow_unsafe_werkzeug=True,
        use_reloader=False,
        log_output=False,
    )


if __name__ == "__main__":
    main()
