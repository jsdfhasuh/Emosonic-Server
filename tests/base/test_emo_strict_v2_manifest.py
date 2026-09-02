import json
import re
import unittest
from pathlib import Path

from supysonic.emo.strict_v2_contract import ACTION_SCHEMAS


class StrictV2ManifestTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repository_root = Path(__file__).resolve().parents[2]
        cls.contract_path = (
            repository_root / "specs" / "emosonic_strict_v2_socketio_server_contract.md"
        )
        cls.manifest_path = (
            repository_root / "tests" / "fixtures" / "emo_strict_v2" / "manifest.json"
        )
        cls.manifest = json.loads(cls.manifest_path.read_text(encoding="utf-8"))

    def test_manifest_tracks_r18_without_runtime_metadata_pinning(self):
        self.assertEqual(self.manifest["protocolVersion"], "2.8.0")
        self.assertIsInstance(self.manifest.get("contractSha256"), str)

    def test_manifest_covers_every_strict_client_action(self):
        actions = set(self.manifest["actions"])
        self.assertEqual(actions, set(ACTION_SCHEMAS))
        self.assertIn("broadcast.feedback", actions)
        self.assertNotIn("broadcast.queue.sync", actions)

    def test_each_action_has_a_closed_schema_and_execution_contract(self):
        required_fields = {
            "profile",
            "type",
            "contractSection",
            "payloadSchema",
            "settlement",
            "serverPushes",
            "roleCapabilityGate",
            "cursorMutation",
            "errorConditionFields",
        }
        for action, action_manifest in self.manifest["actions"].items():
            with self.subTest(action=action):
                self.assertEqual(set(action_manifest), required_fields)
                self.assertTrue(action_manifest["payloadSchema"]["closed"])
                self.assertIn(action_manifest["profile"], {"core", "follow", "handoff", "broadcast"})
                self.assertIn(
                    action_manifest["settlement"],
                    {
                        "correlated_ack",
                        "correlated_direct_response",
                        "correlated_system_pong",
                        "event_confirmed",
                    },
                )

    def test_manifest_actions_map_exactly_to_executable_request_validators(self):
        self.assertEqual(set(self.manifest["actions"]), set(ACTION_SCHEMAS))

        for action, action_manifest in self.manifest["actions"].items():
            with self.subTest(action=action):
                validator = ACTION_SCHEMAS[action]
                payload_schema = action_manifest["payloadSchema"]
                required = tuple(
                    field.split(":", 1)[0]
                    for field in payload_schema["required"]
                )
                optional = tuple(
                    field.split(":", 1)[0]
                    for field in payload_schema["optional"]
                )

                self.assertEqual(validator.message_type, action_manifest["type"])
                self.assertEqual(validator.required, required)
                self.assertEqual(validator.optional, optional)

    def test_authoritative_contract_covers_every_r18_requirement(self):
        entry = self.contract_path.read_text(encoding="utf-8")
        authoritative_sources = (
            "emosonic_strict_v2_contract/phase-3-conformance/"
            "11a-common-and-core-requirements.md",
            "emosonic_strict_v2_contract/phase-3-conformance/"
            "11b-broadcast-requirements.md",
        )
        for source in authoritative_sources:
            with self.subTest(source=source):
                self.assertIn(source, entry)

        requirements = set()
        contract_root = self.contract_path.parent / "emosonic_strict_v2_contract"
        for source in authoritative_sources:
            filename = source.split("emosonic_strict_v2_contract/", 1)[1]
            requirements.update(
                re.findall(
                    r"\*\*(REQ-\d{3})\s+—",
                    (contract_root / filename).read_text(encoding="utf-8"),
                )
            )
        self.assertEqual(
            requirements,
            {"REQ-%03d" % number for number in range(1, 91)},
        )
        self.assertEqual(
            set(self.manifest["requirements"]),
            {"REQ-%03d" % number for number in range(1, 91)},
        )

    def test_legacy_context_candidate_errata_is_reflected_in_contract(self):
        repository_root = Path(__file__).resolve().parents[2]
        contract_root = self.contract_path.parent / "emosonic_strict_v2_contract"
        sources = {
            contract_root
            / "phase-1-core"
            / "07-server-device-context-and-status.md": (
                "authority_device_session_id IS NULL",
                "不构成 strict-v2 active Context 候选",
                "不参与多个候选 conflict",
            ),
            contract_root
            / "phase-3-conformance"
            / "11a-common-and-core-requirements.md": (
                "authority_device_session_id IS NULL",
                "不得参与返回、重绑或多个候选 conflict",
                "多个有效非空候选",
            ),
            contract_root
            / "phase-3-conformance"
            / "13-integration-acceptance.md": (
                "authority_device_session_id IS NULL",
                "legacy active 行",
                "不创建、重绑或修改 Context",
            ),
        }
        for source, expected_fragments in sources.items():
            content = source.read_text(encoding="utf-8")
            with self.subTest(source=str(source.relative_to(repository_root))):
                for fragment in expected_fragments:
                    self.assertIn(fragment, content)

        errata_path = (
            repository_root
            / "ref"
            / "2026-09-02-strict-v2-legacy-context-candidate-errata.md"
        )
        self.assertTrue(errata_path.is_file())
        errata = errata_path.read_text(encoding="utf-8")
        for source in sources:
            relative_source = str(source.relative_to(repository_root)).replace(
                "\\", "/"
            )
            self.assertIn(relative_source, errata)

    def test_historical_realtime_goals_are_marked_superseded(self):
        repository_root = Path(__file__).resolve().parents[2]
        historical_goals = (
            repository_root / "docs" / "goal" / "follow_play.md",
            repository_root / "docs" / "goal" / "broadcast.md",
            repository_root
            / "ref"
            / "playback_context_v2_handoff_stabilization_goal.md",
            repository_root / "ref" / "emosonic_strict_v2_protocol_metadata_goal.md",
        )

        for goal_path in historical_goals:
            with self.subTest(goal=str(goal_path.relative_to(repository_root))):
                header = "\n".join(
                    goal_path.read_text(encoding="utf-8").splitlines()[:12]
                )
                self.assertIn("Superseded", header)
                self.assertIn(
                    "specs/emosonic_strict_v2_socketio_server_contract.md",
                    header,
                )

    def test_legacy_reference_paths_redirect_to_canonical_documents(self):
        repository_root = Path(__file__).resolve().parents[2]
        redirects = {
            repository_root
            / "ref"
            / "emosonic_strict_v2_socketio_server_contract.md": (
                "specs/emosonic_strict_v2_socketio_server_contract.md"
            ),
            repository_root / "ref" / "emosonic_strict_v2_server_change_note.md": (
                "docs/emosonic_strict_v2_server_change_note.md"
            ),
        }

        for redirect_path, canonical_path in redirects.items():
            with self.subTest(path=str(redirect_path.relative_to(repository_root))):
                redirect = redirect_path.read_text(encoding="utf-8")
                self.assertIn(canonical_path, redirect)
                self.assertLessEqual(len(redirect.splitlines()), 20)


if __name__ == "__main__":
    unittest.main()
