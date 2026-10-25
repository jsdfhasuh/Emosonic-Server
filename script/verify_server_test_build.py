#!/usr/bin/env python3
"""Small packaging and isolated runtime checks for the fixed TEST build."""

import argparse
import json
import os
import queue
import re
import secrets
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


SOURCE_COMMIT = "5adc23da173c5f20188d79aafe8fcc1e656eb38a"


def verify_packages(directory: Path) -> None:
    wheels = list(directory.glob("*.whl"))
    sdists = list(directory.glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == 1, "Expected one wheel and one sdist"
    required = (
        "supysonic/emo/strict_v2_registration_descriptor.json",
        "supysonic/emo/strict_v2_conformance.json",
        "supysonic/schema/sqlite.sql",
        "supysonic/schema/mysql.sql",
        "supysonic/schema/postgres.sql",
    )
    for artifact in wheels + sdists:
        if artifact.suffix == ".whl":
            with zipfile.ZipFile(artifact) as archive:
                names = archive.namelist()
        else:
            with tarfile.open(artifact) as archive:
                names = archive.getnames()
        for path in required:
            assert any(name == path or name.endswith("/" + path) for name in names), path
        for part in ("supysonic/templates/", "supysonic/static/", "supysonic/schema/migration/"):
            assert any(part in name and not name.endswith("/") for name in names), part
        assert any(name.endswith(".1") and "/man" in name for name in names), "Missing generated man pages"
        print("Package resources verified:", artifact.name)


def metadata() -> dict:
    import supysonic
    from supysonic import VERSION
    from supysonic.config import DefaultConfig
    from supysonic.emo.protocol_metadata import get_strict_v2_metadata
    from supysonic.emo.strict_v2_readiness import get_effective_profile_readiness

    result = get_strict_v2_metadata()
    assert result["serverBuildCommit"] == SOURCE_COMMIT, result
    assert re.fullmatch(r"[0-9a-f]{64}", result["schemaHash"]), result
    assert VERSION == "0.8.0", VERSION
    package_root = os.environ.get("EMOSONIC_EXPECTED_PACKAGE_ROOT")
    if package_root:
        assert Path(package_root).resolve() in Path(supysonic.__file__).resolve().parents
    defaults = get_effective_profile_readiness(DefaultConfig.WEBAPP)
    assert not any(defaults.values()), defaults
    assert not DefaultConfig.WEBAPP["mount_emosonic"]
    return {"packageVersion": VERSION, **result, "defaultProfiles": defaults}


def smoke() -> None:
    """Run inside the network-isolated TEST container, never against a supplied URL."""
    from supysonic.config import IniConfig
    from supysonic.db import close_connection, init_database
    from supysonic.emo.strict_v2_readiness import get_effective_profile_readiness
    from supysonic.managers.user import UserManager
    import socketio

    config = IniConfig.from_common_locations()
    assert config.BASE["database_uri"] == "sqlite:////tmp/emosonic-test/server.db"
    assert config.WEBAPP["cache_dir"] == "/tmp/emosonic-test/cache"
    assert config.WEBAPP["mount_emosonic"] is True
    assert config.WEBAPP["emo_allowed_origins"] == "http://127.0.0.1:5000"
    profiles = get_effective_profile_readiness(config.WEBAPP)
    assert profiles == {"core": True, "follow": True, "handoff": True, "broadcast": False}, profiles

    base_url = "http://127.0.0.1:5000"
    deadline = time.monotonic() + 90
    while True:
        try:
            with urllib.request.urlopen(base_url + "/emo/ws/?EIO=4&transport=polling", timeout=3) as response:
                body = response.read().decode()
            assert body.startswith("0"), "Engine.IO handshake missing"
            handshake = json.loads(body[1:])
            assert handshake["sid"] and isinstance(handshake["upgrades"], list)
            assert handshake["pingInterval"] > 0 and handshake["pingTimeout"] > 0
            break
        except (OSError, urllib.error.URLError):
            if time.monotonic() >= deadline:
                raise RuntimeError("TEST server did not start within 90 seconds") from None
            time.sleep(1)

    # This account exists only in the disposable container tmpfs. Never print its password.
    username = "test-smoke-" + secrets.token_hex(6)
    password = secrets.token_urlsafe(32)
    init_database(config.BASE["database_uri"])
    UserManager.add(username, password)
    close_connection()

    messages = queue.Queue()
    client = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
    client.on("message", lambda message: messages.put(message), namespace="/emo")

    def exchange(message: dict) -> dict:
        client.emit("message", message, namespace="/emo")
        end = time.monotonic() + 15
        while time.monotonic() < end:
            try:
                received = messages.get(timeout=max(0.01, end - time.monotonic()))
            except queue.Empty:
                break
            if received.get("requestId") != message["requestId"]:
                continue
            assert received.get("action") == "system.ack", "Auth/registration rejected"
            return received["payload"]
        raise RuntimeError("Timed out waiting for correlated TEST response")

    try:
        client.connect(base_url, namespaces=["/emo"], socketio_path="emo/ws", transports=["polling"], wait_timeout=15)
        authenticated = exchange({
            "type": "auth", "action": "auth.login", "requestId": "test-auth",
            "payload": {"u": username, "p": password},
        })
        assert authenticated["authenticated"] is True
        requested = {name: True for name in (
            "playbackContextV2", "playbackPrepare", "effectiveAtPlayback", "canPlay",
            "canPause", "canSeek", "canSetVolume", "supportsFollow", "supportsBroadcast",
            "remoteVolumeControl",
        )}
        registered = exchange({
            "type": "device", "action": "device.register", "requestId": "test-register",
            "payload": {
                "clientId": "test-smoke-player", "deviceSessionId": "test-smoke-session",
                "deviceName": "Isolated TEST player", "roles": ["player"],
                "capabilities": requested,
            },
        })
        negotiated = registered["negotiatedCapabilities"]
        assert negotiated == {**requested, "supportsBroadcast": False}, negotiated
        strict = registered["strictV2"]
        expected = metadata()
        for key in ("protocolVersion", "schemaHash", "serverBuildCommit"):
            assert strict[key] == expected[key], key
        assert strict["connectionNonce"] and strict["connectionEpoch"] == 1
        print(json.dumps({
            **expected, "testProfiles": profiles, "negotiatedCapabilities": negotiated,
            "engineIoHandshake": "passed", "authentication": "passed",
            "strictRegistration": "passed", "playbackOrClientEndToEnd": "not tested",
        }, indent=2, sort_keys=True))
    finally:
        if client.connected:
            client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", choices=("packages", "metadata", "smoke"))
    parser.add_argument("directory", nargs="?", type=Path, default=Path("dist"))
    args = parser.parse_args()
    if args.check == "packages":
        verify_packages(args.directory)
    elif args.check == "metadata":
        print(json.dumps(metadata(), indent=2, sort_keys=True))
    else:
        smoke()


if __name__ == "__main__":
    main()
