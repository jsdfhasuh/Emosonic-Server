import copy
import unittest

from jsonschema import Draft202012Validator

from supysonic.emo.protocol_metadata import (
    get_strict_v2_registration_metadata,
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
                "roles": ["player", "controller"],
                "capabilities": {
                    "playbackContextV2": True,
                    "playbackPrepare": True,
                    "effectiveAtPlayback": True,
                    "canPlay": True,
                    "canPause": True,
                    "canSeek": True,
                    "canSetVolume": True,
                    "supportsFollow": True,
                    "supportsBroadcast": True,
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
                "action": "device.register",
                "clientId": "phone-1",
                "deviceSessionId": "device:phone-1",
                "negotiatedCapabilities": {
                    "playbackContextV2": True,
                    "playbackPrepare": True,
                    "effectiveAtPlayback": True,
                    "canPlay": True,
                    "canPause": True,
                    "canSeek": True,
                    "canSetVolume": True,
                    "supportsFollow": True,
                    "supportsBroadcast": True,
                },
                "strictV2": get_strict_v2_registration_metadata(
                    "nonce-for-descriptor-test"
                ),
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

    def test_descriptor_requires_connection_evidence(self):
        missing_nonce = self._strict_register_ack()
        del missing_nonce["payload"]["strictV2"]["connectionNonce"]
        invalid_epoch = self._strict_register_ack()
        invalid_epoch["payload"]["strictV2"]["connectionEpoch"] = 2
        boolean_epoch = self._strict_register_ack()
        boolean_epoch["payload"]["strictV2"]["connectionEpoch"] = True

        self.assertFalse(self.validator.is_valid(missing_nonce))
        self.assertFalse(self.validator.is_valid(invalid_epoch))
        self.assertFalse(self.validator.is_valid(boolean_epoch))

    def test_descriptor_requires_all_capabilities_and_accepts_single_role(self):
        missing_capability = self._strict_register_request()
        del missing_capability["payload"]["capabilities"]["supportsBroadcast"]
        single_role = self._strict_register_request()
        single_role["payload"]["roles"] = ["player"]
        duplicate_roles = self._strict_register_request()
        duplicate_roles["payload"]["roles"] = ["player", "player"]

        self.assertFalse(self.validator.is_valid(missing_capability))
        self.assertTrue(self.validator.is_valid(single_role))
        self.assertFalse(self.validator.is_valid(duplicate_roles))

    def test_descriptor_rejects_a_legacy_ack_as_strict_ack(self):
        legacy_ack = self._strict_register_ack()
        del legacy_ack["payload"]["strictV2"]
        legacy_ack["payload"]["client"] = {"sessionId": "legacy-room"}

        self.assertFalse(self.validator.is_valid(legacy_ack))

    def test_descriptor_requires_the_correlated_request_action(self):
        ack = self._strict_register_ack()
        del ack["payload"]["action"]

        self.assertFalse(self.validator.is_valid(ack))

    def test_descriptor_accepts_a_registration_error(self):
        error = {
            "type": "system",
            "action": "system.error",
            "requestId": "register-phone-1",
            "timestamp": 1000.0,
            "payload": {
                "action": "device.register",
                "code": "bad_request",
                "message": "deviceSessionId must be a non-empty string",
                "retryable": False,
            },
        }

        self.assertTrue(self.validator.is_valid(error))
