import argparse
import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional, Sequence


FAULT_DIRECTORY_ENV = "EMO_STRICT_V2_ACCEPTANCE_FAULT_DIR"
DEFAULT_FAULT_DIRECTORY = Path("/tmp/emosonic-strict-v2-r7-faults")
FAULT_BINDING_EMIT = "binding_emit_failure"

_FAULT_LOCK = threading.RLock()


def _require_identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty string" % label)
    return value.strip()


def _fault_directory() -> Path:
    configured = os.environ.get(FAULT_DIRECTORY_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_FAULT_DIRECTORY


def _binding_emit_fault_path(
    user_name: str,
    client_id: str,
    device_session_id: str,
) -> Path:
    identity = "\0".join((user_name, client_id, device_session_id)).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return _fault_directory() / ("binding-emit-%s.json" % digest)


def arm_binding_emit_failure(
    user_name: str,
    client_id: str,
    device_session_id: str,
    ttl_seconds: int = 300,
) -> Path:
    user_name = _require_identifier(user_name, "user_name")
    client_id = _require_identifier(client_id, "client_id")
    device_session_id = _require_identifier(
        device_session_id,
        "device_session_id",
    )
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise ValueError("ttl_seconds must be a positive integer")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive integer")

    directory = _fault_directory()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    target = _binding_emit_fault_path(user_name, client_id, device_session_id)
    payload = {
        "schemaVersion": 1,
        "fault": FAULT_BINDING_EMIT,
        "userName": user_name,
        "clientId": client_id,
        "deviceSessionId": device_session_id,
        "expiresAtMs": int((time.time() + ttl_seconds) * 1000),
    }

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".binding-emit-",
        suffix=".json",
        dir=str(directory),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as fault_file:
            json.dump(payload, fault_file, sort_keys=True)
            fault_file.write("\n")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, target)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return target


def clear_binding_emit_failure(
    user_name: str,
    client_id: str,
    device_session_id: str,
) -> bool:
    path = _binding_emit_fault_path(
        _require_identifier(user_name, "user_name"),
        _require_identifier(client_id, "client_id"),
        _require_identifier(device_session_id, "device_session_id"),
    )
    with _FAULT_LOCK:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
    return True


def consume_binding_emit_failure(
    user_name: str,
    client_id: str,
    device_session_id: str,
    now_ms: Optional[int] = None,
) -> bool:
    user_name = _require_identifier(user_name, "user_name")
    client_id = _require_identifier(client_id, "client_id")
    device_session_id = _require_identifier(
        device_session_id,
        "device_session_id",
    )
    path = _binding_emit_fault_path(user_name, client_id, device_session_id)
    with _FAULT_LOCK:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            return False

        path.unlink(missing_ok=True)
        expected = {
            "schemaVersion": 1,
            "fault": FAULT_BINDING_EMIT,
            "userName": user_name,
            "clientId": client_id,
            "deviceSessionId": device_session_id,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            return False
        expires_at_ms = payload.get("expiresAtMs")
        if isinstance(expires_at_ms, bool) or not isinstance(expires_at_ms, int):
            return False
        current_ms = int(time.time() * 1000) if now_ms is None else now_ms
        return current_ms <= expires_at_ms


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Arm one-shot strict-v2 R7 acceptance faults",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    arm = subparsers.add_parser(
        "arm-binding-emit",
        help="Fail the next binding invalidation emit for one strict controller",
    )
    clear = subparsers.add_parser(
        "clear-binding-emit",
        help="Clear an armed binding invalidation emit failure",
    )
    for command in (arm, clear):
        command.add_argument("--user", required=True)
        command.add_argument("--client-id", required=True)
        command.add_argument("--device-session-id", required=True)
    arm.add_argument("--ttl-seconds", type=int, default=300)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "arm-binding-emit":
        path = arm_binding_emit_failure(
            args.user,
            args.client_id,
            args.device_session_id,
            args.ttl_seconds,
        )
        print(
            json.dumps(
                {
                    "armed": True,
                    "fault": FAULT_BINDING_EMIT,
                    "clientId": args.client_id,
                    "deviceSessionId": args.device_session_id,
                    "marker": str(path),
                },
                sort_keys=True,
            )
        )
        return 0

    cleared = clear_binding_emit_failure(
        args.user,
        args.client_id,
        args.device_session_id,
    )
    print(json.dumps({"cleared": cleared}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
