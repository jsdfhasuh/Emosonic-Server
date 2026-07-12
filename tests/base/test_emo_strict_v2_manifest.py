import hashlib
import json
import unittest
from pathlib import Path

from supysonic.emo.strict_v2_conformance import STRICT_V2_CONTRACT_SHA256


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
        self.assertEqual(self.manifest["protocolVersion"], "2.1.0")

    def test_manifest_covers_every_strict_client_action(self):
        expected_actions = {
            "auth.login",
            "device.register",
            "device.list",
            "system.ping",
            "playback.context.create",
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

    def test_manifest_maps_every_ears_requirement(self):
        expected_requirements = {"REQ-%03d" % number for number in range(1, 23)}

        self.assertEqual(set(self.manifest["requirements"]), expected_requirements)
        for requirement, mapping in self.manifest["requirements"].items():
            with self.subTest(requirement=requirement):
                self.assertTrue(mapping["profiles"])
                self.assertTrue(mapping["testModules"])
                self.assertTrue(
                    all(module.startswith("tests.") for module in mapping["testModules"])
                )


if __name__ == "__main__":
    unittest.main()
