#!/usr/bin/env python3
"""Collect fail-closed automated evidence for one committed strict-v2 r8 build."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple


FROZEN_CONTRACT_SHA256 = (
    "5269b53a615ca97f820d3624510acb459e3d3031667a742bd50d1185af8d1e37"
)
FROZEN_PROTOCOL_VERSION = "2.3.0"
PROFILES = ("core", "follow", "handoff", "broadcast")
PROVIDER_ENVIRONMENT = (
    "SUPYSONIC_TEST_POSTGRES_URI",
    "SUPYSONIC_TEST_MYSQL_URI",
)


class EvidenceError(RuntimeError):
    """Raised when evidence cannot be bound safely to one build."""


def _git(repository: Path, arguments: Sequence[str]) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=str(repository),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError("Unable to read %s: %s" % (path, exc)) from exc
    if not isinstance(value, dict):
        raise EvidenceError("%s must contain a JSON object" % path)
    return value


def _schema_hash(descriptor: Mapping[str, object]) -> str:
    fingerprint = {
        "protocolName": descriptor.get("protocolName"),
        "coveredActions": descriptor.get("coveredActions"),
        "schema": descriptor.get("schema"),
    }
    canonical = json.dumps(
        fingerprint,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_metadata(
    contract_hash: str,
    descriptor: Mapping[str, object],
    conformance: Mapping[str, object],
    manifest: Mapping[str, object],
) -> Dict[str, object]:
    if contract_hash != FROZEN_CONTRACT_SHA256:
        raise EvidenceError("Contract SHA-256 does not match frozen r8")
    if descriptor.get("protocolVersion") != FROZEN_PROTOCOL_VERSION:
        raise EvidenceError("Registration descriptor is not protocol 2.3.0")
    for label, value in (
        ("conformance", conformance.get("contractSha256")),
        ("manifest", manifest.get("contractSha256")),
    ):
        if value != FROZEN_CONTRACT_SHA256:
            raise EvidenceError("%s is bound to a different contract" % label)
    if manifest.get("protocolVersion") != FROZEN_PROTOCOL_VERSION:
        raise EvidenceError("Executable manifest is not protocol 2.3.0")

    requirements = manifest.get("requirements")
    expected_requirements = {
        "REQ-%03d" % number for number in range(1, 27)
    }
    if not isinstance(requirements, dict) or set(requirements) != expected_requirements:
        raise EvidenceError("Executable manifest must map REQ-001 through REQ-026")

    profiles = conformance.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != set(PROFILES):
        raise EvidenceError("Conformance profiles are not closed")
    for profile in PROFILES:
        value = profiles.get(profile)
        if not isinstance(value, dict) or set(value) != {
            "codeConformanceReady",
            "evidence",
        }:
            raise EvidenceError(
                "%s conformance profile is not closed" % profile
            )
        evidence = value.get("evidence")
        local_test_candidate = bool(
            value.get("codeConformanceReady") is True
            and isinstance(evidence, list)
            and evidence
            and all(
                isinstance(item, str)
                and item.strip().casefold().startswith("local-test-only:")
                for item in evidence
            )
        )
        disabled = value == {"codeConformanceReady": False, "evidence": []}
        if not (disabled or local_test_candidate):
            raise EvidenceError(
                "%s must remain disabled or use only local-test-only evidence"
                % profile
            )

    return {
        "protocolVersion": FROZEN_PROTOCOL_VERSION,
        "contractSha256": contract_hash,
        "schemaHash": _schema_hash(descriptor),
        "requirements": sorted(expected_requirements),
        "readiness": {profile: False for profile in PROFILES},
    }


def collect_identity(
    repository: Path,
    server_build_commit: Optional[str],
) -> Dict[str, object]:
    repository = repository.resolve()
    head = _git(repository, ("rev-parse", "HEAD"))
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise EvidenceError("git HEAD is not a full lowercase commit SHA")
    if re.fullmatch(r"[0-9a-f]{40}", server_build_commit or "") is None:
        raise EvidenceError("A full EMO_SERVER_BUILD_COMMIT is required")
    if server_build_commit != head:
        raise EvidenceError("EMO_SERVER_BUILD_COMMIT does not match git HEAD")

    dirty = _git(
        repository,
        ("status", "--porcelain", "--untracked-files=all"),
    )
    if dirty:
        raise EvidenceError("Final evidence requires a clean working tree")

    contract_path = (
        repository / "specs" / "emosonic_strict_v2_socketio_server_contract.md"
    )
    contract_hash = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    descriptor = _read_json(
        repository
        / "supysonic"
        / "emo"
        / "strict_v2_registration_descriptor.json"
    )
    conformance = _read_json(
        repository / "supysonic" / "emo" / "strict_v2_conformance.json"
    )
    manifest = _read_json(
        repository / "tests" / "fixtures" / "emo_strict_v2" / "manifest.json"
    )
    identity = _validate_metadata(
        contract_hash,
        descriptor,
        conformance,
        manifest,
    )
    identity["serverBuildCommit"] = head
    return identity


def _command_specs(repository: Path) -> List[Tuple[str, Sequence[str], Path]]:
    python = sys.executable
    return [
        (
            "ears",
            (python, "script/verify_emo_strict_v2_ears.py"),
            repository,
        ),
        (
            "packaging",
            (python, "script/verify_emo_strict_v2_packaging.py"),
            repository,
        ),
        (
            "postgres_migration",
            (
                python,
                "-m",
                "unittest",
                "tests.base.test_emo_schema_migration.EmoSchemaMigrationTestCase."
                "test_postgres_runtime_clean_schema_and_20260708_upgrade",
            ),
            repository,
        ),
        (
            "mysql_migration",
            (
                python,
                "-m",
                "unittest",
                "tests.base.test_emo_schema_migration.EmoSchemaMigrationTestCase."
                "test_mysql_runtime_clean_schema_and_20260708_upgrade",
            ),
            repository,
        ),
        (
            "javascript",
            ("node", "--test", "tests/js/emo_strict_v2_client.test.js"),
            repository,
        ),
        ("docs", ("make", "html"), repository / "docs"),
        ("full_unittest", (python, "-m", "unittest"), repository),
    ]


def _run_command(
    name: str,
    command: Sequence[str],
    cwd: Path,
    log_path: Path,
) -> Dict[str, object]:
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log_file:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
    duration = time.monotonic() - started
    return {
        "name": name,
        "command": list(command),
        "cwd": str(cwd),
        "exitCode": completed.returncode,
        "durationSeconds": round(duration, 3),
        "log": log_path.name,
        "logSha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
    }


def _write_summary(
    output_directory: Path,
    identity: Mapping[str, object],
    results: Sequence[Mapping[str, object]],
) -> None:
    success = all(result.get("exitCode") == 0 for result in results)
    report = {
        "identity": dict(identity),
        "success": success,
        "results": [dict(result) for result in results],
    }
    (output_directory / "automation.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# EmoSonic strict-v2 r8 automated evidence",
        "",
        "- Server build commit: `%s`" % identity["serverBuildCommit"],
        "- Protocol version: `%s`" % identity["protocolVersion"],
        "- Contract SHA-256: `%s`" % identity["contractSha256"],
        "- Schema hash: `%s`" % identity["schemaHash"],
        "- Overall result: **%s**" % ("PASS" if success else "FAIL"),
        "",
        "| Check | Exit | Seconds | Log SHA-256 |",
        "| --- | ---: | ---: | --- |",
    ]
    for result in results:
        lines.append(
            "| %s | %s | %s | `%s` |"
            % (
                result["name"],
                result["exitCode"],
                result["durationSeconds"],
                result["logSha256"],
            )
        )
    lines.extend(
        (
            "",
            "This report covers automated evidence only. Android/Windows acceptance and the final",
            "readiness decision remain separate signed artifacts.",
            "",
        )
    )
    (output_directory / "automation.md").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("docs/verification/emosonic_strict_v2_r8"),
    )
    parser.add_argument(
        "--server-build-commit",
        default=os.environ.get("EMO_SERVER_BUILD_COMMIT"),
    )
    parser.add_argument("--identity-only", action="store_true")
    args = parser.parse_args()

    repository = args.repository.resolve()
    try:
        identity = collect_identity(repository, args.server_build_commit)
        print(json.dumps(identity, indent=2, sort_keys=True))
        if args.identity_only:
            return 0

        missing_environment = [
            name for name in PROVIDER_ENVIRONMENT if not os.environ.get(name)
        ]
        if missing_environment:
            raise EvidenceError(
                "Final evidence requires provider configuration: %s"
                % ", ".join(missing_environment)
            )

        output_root = args.output_root
        if not output_root.is_absolute():
            output_root = repository / output_root
        output_directory = output_root / str(identity["serverBuildCommit"])
        if output_directory.exists():
            raise EvidenceError(
                "Evidence directory already exists: %s" % output_directory
            )
        output_directory.mkdir(parents=True)

        results = []
        for index, (name, command, cwd) in enumerate(
            _command_specs(repository),
            start=1,
        ):
            print("[%d] %s" % (index, name), flush=True)
            results.append(
                _run_command(
                    name,
                    command,
                    cwd,
                    output_directory / ("%02d-%s.log" % (index, name)),
                )
            )
        _write_summary(output_directory, identity, results)
        return 0 if all(result["exitCode"] == 0 for result in results) else 1
    except (EvidenceError, OSError, subprocess.CalledProcessError) as exc:
        print("Strict-v2 r8 evidence collection failed: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
