import copy
import unittest

from jsonschema import Draft202012Validator

from supysonic.emo.protocol_metadata import (
    get_strict_v2_metadata,
    get_strict_v2_registration_descriptor,
)


class EmoRegistrationDescriptorTestCase(unittest.TestCase):
    def setUp(self):
        descriptor = get_strict_v2_registration_descriptor()
        self.schema = descriptor["schema"]
        Draft202012Validator.check_schema(self.schema)
        self.validator = Draft202012Validator(self.schema)

    def _strict_register_request(self):
        return {
            "type": "device",
            "action": "device.register",
            "requestId": "register-phone-1",
            "payload": {
                "clientId": "phone-1",
                "deviceSessionId": "device:phone-1",
                "deviceName": "Alice phone",
                "roles": ["player"],
                "capabilities": {
                    "playbackContextV2": True,
                    "playbackPrepare": True,
                    "effectiveAtPlayback": True,
                },
            },
        }

    def _strict_register_ack(self):
        return {
            "type": "system",
            "action": "system.ack",
            "requestId": "register-phone-1",
            "timestamp": 1000.0,
            "payload": {
                "client": {
                    "userName": "alice",
                    "clientId": "phone-1",
                    "deviceSessionId": "device:phone-1",
                    "roles": ["player"],
                    "capabilities": {
                        "playbackContextV2": True,
                    },
                },
                "strictV2": get_strict_v2_metadata(),
            },
        }

    def test_descriptor_is_a_valid_draft_2020_12_schema(self):
        self.assertIsInstance(self.schema, dict)

    def test_descriptor_accepts_a_strict_registration_request(self):
        self.assertTrue(self.validator.is_valid(self._strict_register_request()))

    def test_descriptor_rejects_a_session_id_in_strict_registration(self):
        request = self._strict_register_request()
        request["payload"]["sessionId"] = "legacy-room"

        self.assertFalse(self.validator.is_valid(request))

    def test_descriptor_accepts_a_strict_registration_ack(self):
        self.assertTrue(self.validator.is_valid(self._strict_register_ack()))

    def test_descriptor_rejects_unknown_strict_metadata(self):
        ack = self._strict_register_ack()
        ack["payload"]["strictV2"]["unexpected"] = True

        self.assertFalse(self.validator.is_valid(ack))

    def test_descriptor_rejects_a_legacy_ack_as_strict_ack(self):
        legacy_ack = self._strict_register_ack()
        del legacy_ack["payload"]["strictV2"]
        legacy_ack["payload"]["client"]["sessionId"] = "legacy-room"

        self.assertFalse(self.validator.is_valid(legacy_ack))

    def test_descriptor_accepts_a_registration_error(self):
        error = {
            "type": "system",
            "action": "system.error",
            "requestId": "register-phone-1",
            "timestamp": 1000.0,
            "payload": {
                "code": "bad_request",
                "message": "deviceSessionId must be a non-empty string",
            },
        }

        self.assertTrue(self.validator.is_valid(error))
