#!/usr/bin/env python3
"""Build and verify the distributable strict-v2 metadata artifacts."""

import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, Mapping, Sequence


FROZEN_CONTRACT_SHA256 = (
    "ca069c6ad52447ea4f7ace7d795460c5ec759e5708b2f45acfbe50903aa4b3a3"
)
PACKAGE_FILES = (
    "supysonic/emo/strict_v2_conformance.json",
    "supysonic/emo/strict_v2_registration_descriptor.json",
)
DISABLED_PROFILES = {
    "core": False,
    "follow": False,
    "handoff": False,
    "broadcast": False,
}


class VerificationError(RuntimeError):
    """Raised when a built artifact does not satisfy the packaging contract."""


def _run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
    print("+", " ".join(str(item) for item in command), flush=True)
    completed = subprocess.run(
        list(command),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if completed.returncode:
        print(completed.stdout, file=sys.stderr)
        completed.check_returncode()
    return completed


def _single_artifact(dist_dir: Path, pattern: str) -> Path:
    artifacts = sorted(dist_dir.glob(pattern))
    if len(artifacts) != 1:
        raise VerificationError(
            "Expected exactly one %s artifact, found %d" % (pattern, len(artifacts))
        )
    return artifacts[0]


def _verify_archive_members(artifact: Path, members: Sequence[str]) -> None:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(str(artifact)) as archive:
            names = set(archive.namelist())
        missing = [member for member in members if member not in names]
    else:
        with tarfile.open(str(artifact), mode="r:gz") as archive:
            names = set(archive.getnames())
        missing = [
            member
            for member in members
            if not any(name.endswith("/" + member) for name in names)
        ]

    if missing:
        raise VerificationError(
            "%s is missing packaged files: %s"
            % (artifact.name, ", ".join(missing))
        )


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _probe_environment() -> Dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _probe_installed_runtime(python: Path, cwd: Path) -> Mapping[str, object]:
    probe = r"""
import json
from pathlib import Path

from supysonic.emo import protocol_metadata, strict_v2_conformance

conformance_path = Path(strict_v2_conformance.__file__).with_name(
    "strict_v2_conformance.json"
)
descriptor_path = Path(protocol_metadata.__file__).with_name(
    "strict_v2_registration_descriptor.json"
)
manifest_contract_sha256 = None
if conformance_path.exists():
    try:
        manifest_contract_sha256 = json.loads(
            conformance_path.read_text(encoding="utf-8")
        ).get("contractSha256")
    except (json.JSONDecodeError, AttributeError):
        pass

print(json.dumps({
    "conformancePath": str(conformance_path.resolve()),
    "descriptorPath": str(descriptor_path.resolve()),
    "manifestContractSha256": manifest_contract_sha256,
    "runtimeContractSha256": strict_v2_conformance.STRICT_V2_CONTRACT_SHA256,
    "readiness": strict_v2_conformance.get_code_conformance_readiness(),
    "protocolVersion": protocol_metadata.get_strict_v2_protocol_version(),
    "schemaHash": protocol_metadata.get_strict_v2_schema_hash(),
}))
"""
    completed = subprocess.run(
        [str(python), "-c", probe],
        cwd=str(cwd),
        env=_probe_environment(),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise VerificationError(
            "Installed runtime probe returned invalid JSON:\n%s\n%s"
            % (completed.stdout, completed.stderr)
        ) from exc


def _assert_installed_path(path: Path, venv_dir: Path, label: str) -> None:
    try:
        path.relative_to(venv_dir.resolve())
    except ValueError as exc:
        raise VerificationError(
            "%s was loaded outside the temporary installation: %s" % (label, path)
        ) from exc


def _assert_readiness_disabled(probe: Mapping[str, object], case: str) -> None:
    if probe.get("readiness") != DISABLED_PROFILES:
        raise VerificationError(
            "%s did not fail closed: %r" % (case, probe.get("readiness"))
        )


def _verify_installed_artifact(
    artifact: Path,
    work_dir: Path,
    expected_schema_hash: str,
) -> None:
    artifact_label = artifact.name
    venv_dir = work_dir / ("venv-" + artifact_label.replace(".", "-"))
    _run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(venv_dir)],
        cwd=work_dir,
    )
    python = _venv_python(venv_dir)
    install_command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--no-index",
    ]
    if artifact.name.endswith(".tar.gz"):
        install_command.append("--no-build-isolation")
    install_command.append(str(artifact))
    _run(install_command, cwd=work_dir)

    probe = _probe_installed_runtime(python, work_dir)
    conformance_path = Path(str(probe["conformancePath"]))
    descriptor_path = Path(str(probe["descriptorPath"]))
    _assert_installed_path(conformance_path, venv_dir, "Conformance manifest")
    _assert_installed_path(descriptor_path, venv_dir, "Registration descriptor")
    if not descriptor_path.is_file():
        raise VerificationError(
            "%s did not install the registration descriptor" % artifact_label
        )
    if probe.get("runtimeContractSha256") != FROZEN_CONTRACT_SHA256:
        raise VerificationError(
            "%s runtime contract SHA-256 does not match the frozen contract"
            % artifact_label
        )
    if probe.get("manifestContractSha256") != FROZEN_CONTRACT_SHA256:
        raise VerificationError(
            "%s installed manifest does not match the frozen contract" % artifact_label
        )
    if probe.get("schemaHash") != expected_schema_hash:
        raise VerificationError(
            "%s installed descriptor schema hash differs from the source descriptor"
            % artifact_label
        )
    _assert_readiness_disabled(probe, artifact_label + " pristine manifest")

    original_manifest = conformance_path.read_bytes()
    conformance_path.unlink()
    _assert_readiness_disabled(
        _probe_installed_runtime(python, work_dir),
        artifact_label + " missing manifest",
    )

    conformance_path.write_text("{", encoding="utf-8")
    _assert_readiness_disabled(
        _probe_installed_runtime(python, work_dir),
        artifact_label + " invalid manifest",
    )

    manifest = json.loads(original_manifest.decode("utf-8"))
    manifest["contractSha256"] = "0" * 64
    conformance_path.write_text(json.dumps(manifest), encoding="utf-8")
    _assert_readiness_disabled(
        _probe_installed_runtime(python, work_dir),
        artifact_label + " hash-mismatched manifest",
    )
    conformance_path.write_bytes(original_manifest)


def _source_schema_hash(repository: Path) -> str:
    descriptor_path = (
        repository / "supysonic" / "emo" / "strict_v2_registration_descriptor.json"
    )
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    fingerprint = {
        "protocolName": descriptor["protocolName"],
        "coveredActions": descriptor["coveredActions"],
        "schema": descriptor["schema"],
    }
    canonical = json.dumps(
        fingerprint,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    contract_path = repository / "specs" / "emosonic_strict_v2_socketio_server_contract.md"
    observed_contract_hash = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    if observed_contract_hash != FROZEN_CONTRACT_SHA256:
        raise VerificationError(
            "Frozen contract SHA-256 changed: %s" % observed_contract_hash
        )

    expected_schema_hash = _source_schema_hash(repository)
    with tempfile.TemporaryDirectory(prefix="emosonic-strict-v2-packaging-") as directory:
        work_dir = Path(directory)
        dist_dir = work_dir / "dist"
        _run(
            [
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--outdir",
                str(dist_dir),
            ],
            cwd=repository,
        )
        wheel = _single_artifact(dist_dir, "*.whl")
        sdist = _single_artifact(dist_dir, "*.tar.gz")
        for artifact in (wheel, sdist):
            _verify_archive_members(artifact, PACKAGE_FILES)
            _verify_installed_artifact(artifact, work_dir, expected_schema_hash)

    print(
        "Strict-v2 packaging verification passed for wheel and sdist; "
        "installed missing/invalid/hash-mismatched manifests fail closed."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, subprocess.CalledProcessError, VerificationError) as exc:
        print("Strict-v2 packaging verification failed: %s" % exc, file=sys.stderr)
        sys.exit(1)
