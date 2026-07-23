import json
import unittest
from pathlib import Path

from supysonic.emo.strict_v2_contract import (
    ACTION_SCHEMAS,
    StrictRequestValidationError,
    validate_strict_request,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "emo_web_strict_v2" / "requests.json"


def _contains_key(value, forbidden):
    if isinstance(value, dict):
        return bool(set(value).intersection(forbidden)) or any(
            _contains_key(item, forbidden) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_key(item, forbidden) for item in value)
    return False


class EmoWebStrictV2FixtureTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_fixture_covers_every_strict_request_action(self):
        actions = [item["message"]["action"] for item in self.fixture["requests"]]
        self.assertEqual(set(actions), set(ACTION_SCHEMAS))
        self.assertEqual(len(actions), len(set(actions)) + 1)
        self.assertEqual(actions.count("device.register"), 2)

    def test_every_web_request_fixture_passes_authoritative_validator(self):
        for item in self.fixture["requests"]:
            with self.subTest(action=item["message"]["action"]):
                normalized = validate_strict_request(item["message"])
                self.assertEqual(normalized["action"], item["message"]["action"])

    def test_fixture_documents_settlement_for_every_action(self):
        allowed = {"ack", "direct", "event-confirmed"}
        for item in self.fixture["requests"]:
            self.assertIn(item["settlement"], allowed)

    def test_web_registration_fails_optional_profiles_closed_pending_browser_evidence(self):
        registrations = [
            item["message"]
            for item in self.fixture["requests"]
            if item["message"]["action"] == "device.register"
        ]
        for registration in registrations:
            capabilities = registration["payload"]["capabilities"]
            self.assertFalse(capabilities["supportsBroadcast"])
            self.assertFalse(capabilities["supportsFollow"])
            self.assertEqual(
                capabilities["playbackPrepare"],
                registration["payload"]["roles"] == ["player"],
            )
            self.assertFalse(capabilities["effectiveAtPlayback"])

    def test_fixture_has_no_forbidden_session_fields_or_actions(self):
        forbidden_fields = set(self.fixture["forbiddenFields"])
        forbidden_actions = set(self.fixture["forbiddenActions"])
        for item in self.fixture["requests"]:
            message = item["message"]
            self.assertFalse(_contains_key(message, forbidden_fields))
            self.assertNotIn(message["action"], forbidden_actions)

    def test_legacy_session_field_is_explicitly_rejected(self):
        with self.assertRaisesRegex(StrictRequestValidationError, "sessionId"):
            validate_strict_request(
                {
                    "type": "state",
                    "action": "device.list",
                    "requestId": "legacy-field-1",
                    "payload": {"sessionId": "legacy"},
                }
            )

    def test_forbidden_legacy_actions_are_not_supported(self):
        for action in self.fixture["forbiddenActions"]:
            with self.subTest(action=action), self.assertRaises(StrictRequestValidationError):
                validate_strict_request(
                    {
                        "type": "command",
                        "action": action,
                        "requestId": "legacy-action-1",
                        "payload": {},
                    }
                )


if __name__ == "__main__":
    unittest.main()
