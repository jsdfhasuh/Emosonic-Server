import hashlib
import importlib
import json
import unittest
from pathlib import Path

from supysonic.emo.strict_v2_conformance import STRICT_V2_CONTRACT_SHA256
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

    def test_manifest_is_bound_to_the_frozen_contract(self):
        contract_hash = hashlib.sha256(self.contract_path.read_bytes()).hexdigest()

        self.assertEqual(contract_hash, STRICT_V2_CONTRACT_SHA256)
        self.assertEqual(self.manifest["contractSha256"], contract_hash)
        self.assertEqual(self.manifest["protocolVersion"], "2.4.0")

    def test_manifest_covers_every_strict_client_action(self):
        expected_actions = {
            "auth.login",
            "device.register",
            "device.list",
            "device.setVolume",
            "device.volume.update",
            "system.ping",
            "playback.context.list",
            "playback.context.ensure",
            "playback.context.prepare",
            "playback.context.prepared",
            "playback.context.subscribe",
            "playback.context.unsubscribe",
            "playback.context.status",
            "playback.context.close",
            "queue.context.sync",
            "playback.update",
            "queue.playItem",
            "player.play",
            "player.pause",
            "player.seek",
            "player.next",
            "player.prev",
            "follow.start",
            "follow.stop",
            "playback.handoff.start",
            "playback.ready",
            "playback.handoff.complete",
            "playback.handoff.cancel",
            "broadcast.start",
            "broadcast.status",
            "broadcast.play",
            "broadcast.pause",
            "broadcast.seek",
            "broadcast.playItem",
            "broadcast.queue.sync",
            "broadcast.stop",
        }

        self.assertEqual(set(self.manifest["actions"]), expected_actions)

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

    def test_manifest_maps_every_ears_requirement(self):
        expected_requirements = {"REQ-%03d" % number for number in range(1, 27)}

        self.assertEqual(set(self.manifest["requirements"]), expected_requirements)
        for requirement, mapping in self.manifest["requirements"].items():
            with self.subTest(requirement=requirement):
                self.assertTrue(mapping["profiles"])
                self.assertTrue(mapping["testModules"])
                self.assertTrue(mapping["testMethods"])
                self.assertTrue(
                    all(module.startswith("tests.") for module in mapping["testModules"])
                )
                method_modules = set()
                for dotted_method in mapping["testMethods"]:
                    module_name, class_name, method_name = dotted_method.rsplit(
                        ".",
                        2,
                    )
                    method_modules.add(module_name)
                    test_module = importlib.import_module(module_name)
                    test_case = getattr(test_module, class_name)
                    self.assertTrue(issubclass(test_case, unittest.TestCase))
                    self.assertTrue(callable(getattr(test_case, method_name, None)))
                self.assertEqual(set(mapping["testModules"]), method_modules)

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
