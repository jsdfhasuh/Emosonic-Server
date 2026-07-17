import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from script import collect_emo_strict_v2_r7_evidence
from script import verify_emo_strict_v2_ears
from script import verify_emo_strict_v2_packaging
from supysonic.emo.strict_v2_conformance import STRICT_V2_CONTRACT_SHA256


ROOT = Path(__file__).resolve().parents[2]


class StrictV2VerificationScriptsTestCase(unittest.TestCase):
    def _metadata_documents(self):
        contract_path = (
            ROOT / "specs" / "emosonic_strict_v2_socketio_server_contract.md"
        )
        descriptor = json.loads(
            (
                ROOT
                / "supysonic"
                / "emo"
                / "strict_v2_registration_descriptor.json"
            ).read_text(encoding="utf-8")
        )
        conformance = json.loads(
            (
                ROOT / "supysonic" / "emo" / "strict_v2_conformance.json"
            ).read_text(encoding="utf-8")
        )
        manifest = json.loads(
            (
                ROOT
                / "tests"
                / "fixtures"
                / "emo_strict_v2"
                / "manifest.json"
            ).read_text(encoding="utf-8")
        )
        return (
            hashlib.sha256(contract_path.read_bytes()).hexdigest(),
            descriptor,
            conformance,
            manifest,
        )

    def test_evidence_collector_binds_clean_tree_to_exact_commit(self):
        build_commit = "a" * 40
        with mock.patch.object(
            collect_emo_strict_v2_r7_evidence,
            "_git",
            side_effect=(build_commit, ""),
        ):
            identity = collect_emo_strict_v2_r7_evidence.collect_identity(
                ROOT,
                build_commit,
            )

        self.assertEqual(identity["serverBuildCommit"], build_commit)
        self.assertEqual(identity["protocolVersion"], "2.4.0")
        self.assertEqual(identity["contractSha256"], STRICT_V2_CONTRACT_SHA256)
        self.assertEqual(len(identity["requirements"]), 45)
        self.assertFalse(any(identity["readiness"].values()))

    def test_evidence_collector_rejects_dirty_or_mismatched_build(self):
        build_commit = "a" * 40
        with mock.patch.object(
            collect_emo_strict_v2_r7_evidence,
            "_git",
            side_effect=(build_commit, " M supysonic/emo/ws.py"),
        ), self.assertRaisesRegex(
            collect_emo_strict_v2_r7_evidence.EvidenceError,
            "clean working tree",
        ):
            collect_emo_strict_v2_r7_evidence.collect_identity(
                ROOT,
                build_commit,
            )

        with mock.patch.object(
            collect_emo_strict_v2_r7_evidence,
            "_git",
            return_value=build_commit,
        ), self.assertRaisesRegex(
            collect_emo_strict_v2_r7_evidence.EvidenceError,
            "does not match git HEAD",
        ):
            collect_emo_strict_v2_r7_evidence.collect_identity(
                ROOT,
                "b" * 40,
            )

    def test_evidence_collector_accepts_local_test_only_candidates(self):
        metadata = self._metadata_documents()

        identity = collect_emo_strict_v2_r7_evidence._validate_metadata(
            *metadata
        )

        self.assertFalse(any(identity["readiness"].values()))

    def test_evidence_collector_rejects_premature_formal_readiness(self):
        contract_hash, descriptor, conformance, manifest = (
            self._metadata_documents()
        )
        conformance["profiles"]["core"] = {
            "codeConformanceReady": True,
            "evidence": ["ci:premature-formal-readiness"],
        }

        with self.assertRaisesRegex(
            collect_emo_strict_v2_r7_evidence.EvidenceError,
            "disabled or use only local-test-only evidence",
        ):
            collect_emo_strict_v2_r7_evidence._validate_metadata(
                contract_hash,
                descriptor,
                conformance,
                manifest,
            )

    def test_evidence_collector_writes_machine_and_human_summaries(self):
        identity = {
            "serverBuildCommit": "a" * 40,
            "protocolVersion": "2.4.0",
            "contractSha256": STRICT_V2_CONTRACT_SHA256,
            "schemaHash": "b" * 64,
        }
        results = [
            {
                "name": "ears",
                "command": ["python", "script/verify_emo_strict_v2_ears.py"],
                "cwd": str(ROOT),
                "exitCode": 0,
                "durationSeconds": 1.25,
                "log": "01-ears.log",
                "logSha256": "c" * 64,
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            collect_emo_strict_v2_r7_evidence._write_summary(
                output,
                identity,
                results,
            )
            report = json.loads(
                (output / "automation.json").read_text(encoding="utf-8")
            )
            markdown = (output / "automation.md").read_text(encoding="utf-8")

        self.assertTrue(report["success"])
        self.assertEqual(report["identity"], identity)
        self.assertIn("Overall result: **PASS**", markdown)
        self.assertIn("Android/Windows acceptance", markdown)

    def test_ears_runner_uses_complete_r11_requirement_inventory(self):
        methods = verify_emo_strict_v2_ears._mapped_test_methods()

        self.assertIn(
            "tests.base.test_emo_strict_v2_core.StrictV2CoreTestCase."
            "test_context_list_discovers_exact_persisted_pair_and_replays_by_request_id",
            methods,
        )
        self.assertIn(
            "tests.base.test_emo_ws_store.EmoWebSocketStoreTestCase."
            "test_handoff_and_control_are_linearized_by_authority_pair",
            methods,
        )
        self.assertIn(
            "tests.base.test_emo_strict_v2_core.StrictV2CoreTestCase."
            "test_remote_control_persists_pending_deadline_and_watchdog_settles_unknown",
            methods,
        )

    def test_ears_runner_rejects_pre_r11_requirement_inventory(self):
        manifest = json.loads(
            verify_emo_strict_v2_ears.MANIFEST_PATH.read_text(encoding="utf-8")
        )
        manifest["requirements"].pop("REQ-045")

        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with mock.patch.object(
                verify_emo_strict_v2_ears,
                "MANIFEST_PATH",
                manifest_path,
        ), self.assertRaisesRegex(ValueError, "REQ-001 through REQ-045"):
                verify_emo_strict_v2_ears._mapped_test_methods()

    def test_packaging_verifier_is_bound_to_r11_protocol_identity(self):
        contract_path = (
            ROOT / "specs" / "emosonic_strict_v2_socketio_server_contract.md"
        )
        descriptor_path = (
            ROOT
            / "supysonic"
            / "emo"
            / "strict_v2_registration_descriptor.json"
        )
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        observed_hash = hashlib.sha256(contract_path.read_bytes()).hexdigest()

        self.assertEqual(
            verify_emo_strict_v2_packaging.FROZEN_CONTRACT_SHA256,
            STRICT_V2_CONTRACT_SHA256,
        )
        self.assertEqual(observed_hash, STRICT_V2_CONTRACT_SHA256)
        self.assertEqual(
            verify_emo_strict_v2_packaging.FROZEN_PROTOCOL_VERSION,
            "2.4.0",
        )
        self.assertEqual(descriptor["protocolVersion"], "2.4.0")

    def test_packaging_verifier_rejects_protocol_identity_mismatch(self):
        canonical = {
            "runtimeContractSha256": STRICT_V2_CONTRACT_SHA256,
            "manifestContractSha256": STRICT_V2_CONTRACT_SHA256,
            "protocolVersion": "2.4.0",
        }
        verify_emo_strict_v2_packaging._assert_protocol_identity(
            canonical,
            "canonical",
        )

        for field_name, invalid_value in (
            ("runtimeContractSha256", "0" * 64),
            ("manifestContractSha256", "0" * 64),
            ("protocolVersion", "2.1.0"),
        ):
            with self.subTest(field=field_name), self.assertRaises(
                verify_emo_strict_v2_packaging.VerificationError
            ):
                verify_emo_strict_v2_packaging._assert_protocol_identity(
                    dict(canonical, **{field_name: invalid_value}),
                    "invalid",
                )

    def test_browser_acceptance_server_requires_exact_build_commit(self):
        command = [
            sys.executable,
            str(ROOT / "script" / "serve_emo_web_strict_v2_acceptance.py"),
            "--help",
        ]
        environment = os.environ.copy()
        environment.pop("EMO_SERVER_BUILD_COMMIT", None)
        missing = subprocess.run(
            command,
            cwd=str(ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("exact 40-character acceptance build", missing.stdout)

        environment["EMO_SERVER_BUILD_COMMIT"] = "e" * 40
        configured = subprocess.run(
            command,
            cwd=str(ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.assertEqual(configured.returncode, 0, configured.stdout)
        self.assertIn("usage:", configured.stdout)


if __name__ == "__main__":
    unittest.main()
