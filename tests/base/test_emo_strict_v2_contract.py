import copy
import unittest

from supysonic.emo.strict_v2_contract import (
    StrictRequestValidationError,
    is_strict_registration_request,
    validate_strict_request,
)


class StrictV2ContractTestCase(unittest.TestCase):
    def _register_request(self):
        return {
            "type": "device",
            "action": "device.register",
            "requestId": "register-1",
            "payload": {
                "clientId": "phone-1",
                "deviceSessionId": "device:phone-1",
                "deviceName": "Phone",
                "roles": ["player"],
                "capabilities": {
                    "playbackContextV2": True,
                    "playbackPrepare": False,
                    "effectiveAtPlayback": False,
                    "canPlay": True,
                    "canPause": True,
                    "canSeek": True,
                    "canSetVolume": True,
                    "supportsFollow": False,
                    "supportsBroadcast": False,
                },
            },
        }

    def test_accepts_single_role_and_normalizes_role_order(self):
        request = self._register_request()
        request["payload"]["roles"] = ["controller", "player"]

        normalized = validate_strict_request(request)

        self.assertEqual(normalized["payload"]["roles"], ["player", "controller"])

    def test_rejects_uncorrelatable_request_id_and_action(self):
        for field_name, value in (("requestId", ""), ("requestId", None), ("action", "x" * 65)):
            request = self._register_request()
            request[field_name] = value
            with self.subTest(field_name=field_name, value=value):
                with self.assertRaises(StrictRequestValidationError) as context:
                    validate_strict_request(request)
                self.assertFalse(context.exception.correlatable)

    def test_rejects_unknown_envelope_and_payload_fields(self):
        unknown_envelope = self._register_request()
        unknown_envelope["targetClientId"] = "phone-2"
        unknown_payload = self._register_request()
        unknown_payload["payload"]["unexpected"] = True

        for request in (unknown_envelope, unknown_payload):
            with self.assertRaises(StrictRequestValidationError) as context:
                validate_strict_request(request)
            self.assertTrue(context.exception.correlatable)
            self.assertEqual(context.exception.code, "bad_request")

    def test_rejects_nested_session_id(self):
        request = self._register_request()
        request["payload"]["capabilities"]["sessionId"] = "legacy"

        with self.assertRaisesRegex(StrictRequestValidationError, "sessionId"):
            validate_strict_request(request)

    def test_handoff_target_is_the_only_payload_target(self):
        request = {
            "type": "command",
            "action": "playback.handoff.start",
            "requestId": "handoff-1",
            "payload": {
                "playbackContextId": "context-1",
                "targetClientId": "phone-2",
                "baseControlVersion": 1,
            },
        }

        self.assertEqual(
            validate_strict_request(request)["payload"]["targetClientId"],
            "phone-2",
        )

        other_action = copy.deepcopy(request)
        other_action["action"] = "player.play"
        with self.assertRaises(StrictRequestValidationError):
            validate_strict_request(other_action)

    def test_rejects_business_and_transport_limits(self):
        request = {
            "type": "command",
            "action": "playback.context.create",
            "requestId": "create-1",
            "payload": {
                "playbackContextId": "context-1",
                "deviceSessionId": "device-1",
                "queueSongIds": ["song-%d" % index for index in range(1001)],
                "currentIndex": 0,
                "positionMs": 0,
                "state": "stopped",
            },
        }

        with self.assertRaisesRegex(StrictRequestValidationError, "1000"):
            validate_strict_request(request)

        request["payload"]["queueSongIds"] = ["song-1", "song-1"]
        with self.assertRaisesRegex(StrictRequestValidationError, "duplicates"):
            validate_strict_request(request)

    def test_rejects_invalid_ready_field_combinations(self):
        request = {
            "type": "event",
            "action": "playback.ready",
            "requestId": "ready-1",
            "payload": {
                "playbackContextId": "context-1",
                "prepareId": "prepare-1",
                "ready": False,
            },
        }

        with self.assertRaisesRegex(StrictRequestValidationError, "errorCode"):
            validate_strict_request(request)

    def test_unknown_action_is_correlated_not_supported(self):
        request = {
            "type": "command",
            "action": "player.setVolume",
            "requestId": "unsupported-1",
            "payload": {},
        }

        with self.assertRaises(StrictRequestValidationError) as context:
            validate_strict_request(request)

        self.assertTrue(context.exception.correlatable)
        self.assertEqual(context.exception.code, "not_supported")

    def test_identifies_strict_registration_without_accepting_other_messages(self):
        self.assertTrue(is_strict_registration_request(self._register_request()))
        login = {
            "type": "auth",
            "action": "auth.login",
            "requestId": "auth-1",
            "payload": {"u": "alice", "p": "secret"},
        }
        self.assertFalse(is_strict_registration_request(login))


if __name__ == "__main__":
    unittest.main()
