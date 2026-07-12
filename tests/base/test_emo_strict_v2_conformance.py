import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supysonic.emo import strict_v2_conformance


class StrictV2ConformanceTestCase(unittest.TestCase):
    def tearDown(self):
        strict_v2_conformance._load_conformance_readiness.cache_clear()

    def _read_manifest(self):
        with strict_v2_conformance._CONFORMANCE_PATH.open(encoding="utf-8") as manifest_file:
            return json.load(manifest_file)

    def _load_from_temporary_manifest(self, value):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "strict_v2_conformance.json"
            if isinstance(value, str):
                manifest_path.write_text(value, encoding="utf-8")
            else:
                manifest_path.write_text(json.dumps(value), encoding="utf-8")
            with mock.patch.object(
                strict_v2_conformance,
                "_CONFORMANCE_PATH",
                manifest_path,
            ):
                strict_v2_conformance._load_conformance_readiness.cache_clear()
                return strict_v2_conformance.get_code_conformance_readiness()

    def test_shipped_manifest_starts_with_every_profile_disabled(self):
        self.assertEqual(
            strict_v2_conformance.get_code_conformance_readiness(),
            {
                "core": False,
                "follow": False,
                "handoff": False,
                "broadcast": False,
            },
        )

    def test_frozen_contract_matches_code_and_manifest_hash(self):
        contract_path = (
            Path(__file__).resolve().parents[2]
            / "specs"
            / "emosonic_strict_v2_socketio_server_contract.md"
        )
        contract_hash = hashlib.sha256(contract_path.read_bytes()).hexdigest()
        manifest = self._read_manifest()

        self.assertEqual(
            contract_hash,
            strict_v2_conformance.STRICT_V2_CONTRACT_SHA256,
        )
        self.assertEqual(manifest["contractSha256"], contract_hash)

    def test_missing_manifest_fails_closed(self):
        missing_path = Path(tempfile.gettempdir()) / "missing-strict-v2-conformance.json"
        with mock.patch.object(strict_v2_conformance, "_CONFORMANCE_PATH", missing_path):
            strict_v2_conformance._load_conformance_readiness.cache_clear()
            self.assertFalse(any(strict_v2_conformance.get_code_conformance_readiness().values()))

    def test_invalid_json_fails_closed(self):
        self.assertFalse(any(self._load_from_temporary_manifest("{").values()))

    def test_contract_hash_mismatch_fails_closed(self):
        manifest = self._read_manifest()
        manifest["contractSha256"] = "0" * 64

        self.assertFalse(any(self._load_from_temporary_manifest(manifest).values()))

    def test_ready_profile_requires_evidence(self):
        manifest = self._read_manifest()
        manifest["profiles"]["core"]["codeConformanceReady"] = True

        self.assertFalse(any(self._load_from_temporary_manifest(manifest).values()))

    def test_unknown_profile_is_never_ready(self):
        self.assertFalse(strict_v2_conformance.is_profile_code_conformance_ready("unknown"))


if __name__ == "__main__":
    unittest.main()
