import copy
import json
import unittest
from pathlib import Path

from supysonic.emo.strict_v2_contract import (
    STRICT_OUTPUT_ACTIONS,
    StrictOutputValidationError,
    StrictRequestValidationError,
    is_strict_registration_request,
    validate_strict_output,
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

    def test_target_fields_are_closed_to_handoff_and_device_volume(self):
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

        volume = {
            "type": "command",
            "action": "device.setVolume",
            "requestId": "volume-1",
            "payload": {
                "targetClientId": "phone-2",
                "targetDeviceSessionId": "device:phone-2",
                "volume": 65,
            },
        }
        self.assertEqual(validate_strict_request(volume), volume)

    def test_accepts_optional_remote_volume_capability(self):
        request = self._register_request()
        request["payload"]["capabilities"]["remoteVolumeControl"] = True

        normalized = validate_strict_request(request)

        self.assertTrue(
            normalized["payload"]["capabilities"]["remoteVolumeControl"]
        )

    def test_rejects_business_and_transport_limits(self):
        request = {
            "type": "command",
            "action": "playback.context.ensure",
            "requestId": "ensure-1",
            "payload": {
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

        broadcast = {
            "type": "command",
            "action": "broadcast.start",
            "requestId": "broadcast-1",
            "payload": {
                "playbackContextId": "context-1",
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "positionMs": 0,
                "participants": ["client-%d" % index for index in range(101)],
            },
        }
        with self.assertRaisesRegex(StrictRequestValidationError, "100"):
            validate_strict_request(broadcast)

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

        request["payload"]["errorCode"] = "INVALID-CODE"
        with self.assertRaisesRegex(StrictRequestValidationError, "invalid format"):
            validate_strict_request(request)

    def test_unknown_action_is_correlated_not_supported(self):
        request = {
            "type": "command",
            "action": "device.setBalance",
            "requestId": "unsupported-1",
            "payload": {},
        }

        with self.assertRaises(StrictRequestValidationError) as context:
            validate_strict_request(request)

        self.assertTrue(context.exception.correlatable)
        self.assertEqual(context.exception.code, "not_supported")

    def test_validates_device_volume_request_and_feedback(self):
        command = {
            "type": "command",
            "action": "device.setVolume",
            "requestId": "volume-command-1",
            "payload": {
                "targetClientId": "player-1",
                "targetDeviceSessionId": "device:player-1",
                "volume": 100,
            },
        }
        feedback = {
            "type": "event",
            "action": "device.volume.update",
            "requestId": "volume-feedback-1",
            "payload": {
                "deviceSessionId": "device:player-1",
                "volume": 0,
                "clientSeq": 1,
            },
        }

        self.assertEqual(validate_strict_request(command), command)
        self.assertEqual(validate_strict_request(feedback), feedback)

        for invalid_volume in (-1, 101, True, "65"):
            invalid = copy.deepcopy(command)
            invalid["payload"]["volume"] = invalid_volume
            with self.subTest(invalid_volume=invalid_volume):
                with self.assertRaises(StrictRequestValidationError):
                    validate_strict_request(invalid)

    def test_validates_closed_context_list_request_schema(self):
        request = {
            "type": "state",
            "action": "playback.context.list",
            "requestId": "context-list-1",
            "payload": {
                "authorityClientId": "player-1",
                "authorityDeviceSessionId": "device:player-1",
            },
        }

        self.assertEqual(validate_strict_request(request), request)

        for field_name, value in (
            ("authorityClientId", ""),
            ("authorityDeviceSessionId", None),
            ("authorityClientId", "x" * 129),
        ):
            invalid = copy.deepcopy(request)
            invalid["payload"][field_name] = value
            with self.subTest(field_name=field_name, value=value):
                with self.assertRaises(StrictRequestValidationError):
                    validate_strict_request(invalid)

        unknown = copy.deepcopy(request)
        unknown["payload"]["sessionId"] = "legacy"
        with self.assertRaises(StrictRequestValidationError):
            validate_strict_request(unknown)

    def test_validates_ensure_idle_and_queue_backed_shapes(self):
        idle = {
            "type": "command",
            "action": "playback.context.ensure",
            "requestId": "ensure-idle-1",
            "payload": {
                "deviceSessionId": "device:player-1",
                "queueSongIds": [],
                "positionMs": 0,
                "state": "idle",
            },
        }
        queue_backed = copy.deepcopy(idle)
        queue_backed["requestId"] = "ensure-queue-1"
        queue_backed["payload"].update(
            {
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "state": "paused",
            }
        )

        self.assertEqual(validate_strict_request(idle), idle)
        self.assertEqual(validate_strict_request(queue_backed), queue_backed)

        invalid = copy.deepcopy(idle)
        invalid["payload"]["currentIndex"] = 0
        with self.assertRaises(StrictRequestValidationError):
            validate_strict_request(invalid)

        invalid = copy.deepcopy(queue_backed)
        invalid["payload"]["state"] = "idle"
        with self.assertRaises(StrictRequestValidationError):
            validate_strict_request(invalid)

    def test_validates_prepare_and_prepared_request_shapes(self):
        prepare = {
            "type": "command",
            "action": "playback.context.prepare",
            "requestId": "prepare-1",
            "payload": {
                "playbackContextId": "context-1",
                "intentId": "intent-1",
                "baseControlVersion": 1,
                "initialQueueSongIds": ["song-1"],
                "currentIndex": 0,
                "positionMs": 0,
            },
        }
        prepared = {
            "type": "event",
            "action": "playback.context.prepared",
            "requestId": "prepared-1",
            "payload": {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "intentId": "intent-1",
                "ready": False,
                "errorCode": "queue_required",
            },
        }

        self.assertEqual(validate_strict_request(prepare), prepare)
        self.assertEqual(validate_strict_request(prepared), prepared)

        missing_pair = copy.deepcopy(prepare)
        del missing_pair["payload"]["positionMs"]
        with self.assertRaises(StrictRequestValidationError):
            validate_strict_request(missing_pair)

        invalid_error = copy.deepcopy(prepared)
        invalid_error["payload"]["errorCode"] = "execution_unknown"
        with self.assertRaises(StrictRequestValidationError):
            validate_strict_request(invalid_error)

    def test_validates_all_playback_update_request_shapes(self):
        common = {
            "playbackContextId": "context-1",
            "deviceSessionId": "device:player-1",
            "state": "playing",
            "trackId": "song-1",
            "positionMs": 100,
            "clientSeq": 1,
        }
        payloads = [
            dict(common, origin="passive", appliedControlVersion=1),
            dict(
                common,
                origin="remoteCommand",
                executionStatus="committed",
                commandControlVersion=2,
                appliedControlVersion=2,
            ),
            dict(
                common,
                origin="remoteCommand",
                executionStatus="failed",
                commandControlVersion=2,
                appliedControlVersion=1,
                errorCode="track_load_failed",
            ),
            dict(
                common,
                origin="localUser",
                executionStatus="committed",
                intentId="local-1",
                epoch=1,
                observedControlVersion=1,
                queueIndex=0,
            ),
        ]

        for index, payload in enumerate(payloads):
            message = {
                "type": "event",
                "action": "playback.update",
                "requestId": "update-%d" % index,
                "payload": payload,
            }
            with self.subTest(index=index):
                self.assertEqual(validate_strict_request(message), message)

        invalid = {
            "type": "event",
            "action": "playback.update",
            "requestId": "update-invalid",
            "payload": dict(
                payloads[2],
                errorCode="dependency_failed",
            ),
        }
        with self.assertRaises(StrictRequestValidationError):
            validate_strict_request(invalid)

    def test_client_cannot_send_server_only_settled(self):
        request = {
            "type": "event",
            "action": "playback.control.settled",
            "requestId": "settled-1",
            "payload": {},
        }

        with self.assertRaises(StrictRequestValidationError) as context:
            validate_strict_request(request)

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

    def _output(self, msg_type, action, payload, request_id=None):
        message = {
            "type": msg_type,
            "action": action,
            "payload": payload,
            "timestamp": 1000.0,
            "connectionNonce": "nonce-1",
            "connectionEpoch": 1,
        }
        if request_id is not None:
            message["requestId"] = request_id
        return message

    def test_validates_closed_ack_error_and_direct_response_outputs(self):
        messages = [
            self._output(
                "system",
                "system.ack",
                {"action": "player.pause"},
                "pause-1",
            ),
            self._output(
                "system",
                "system.error",
                {
                    "action": "player.seek",
                    "code": "stale_version",
                    "message": "control cursor is stale",
                    "retryable": False,
                    "playbackContextId": "context-1",
                    "currentControlVersion": 2,
                },
                "seek-1",
            ),
            self._output(
                "system",
                "system.pong",
                {"serverTimeMs": 1000},
                "ping-1",
            ),
        ]

        for message in messages:
            with self.subTest(action=message["action"]):
                self.assertEqual(validate_strict_output(message), message)

    def test_validates_context_status_and_feedback_outputs(self):
        context = {
            "playbackContextId": "context-1",
            "authorityClientId": "player-1",
            "queueSongIds": ["song-1"],
            "currentIndex": 0,
            "trackId": "song-1",
            "state": "playing",
            "positionMs": 1200,
            "queueRevision": 1,
            "controlVersion": 1,
            "version": 1,
            "epoch": 1,
            "timelineId": "timeline-1",
            "serverUpdatedAtMs": 1000,
        }
        status = self._output(
            "state",
            "playback.context.status",
            {
                "playbackContext": context,
                "deviceStates": [
                    {
                        "playbackContextId": "context-1",
                        "clientId": "player-1",
                        "deviceSessionId": "device:player-1",
                        "state": "playing",
                        "trackId": "song-1",
                        "positionMs": 1200,
                        "appliedControlVersion": 1,
                        "clientSeq": 1,
                        "serverUpdatedAtMs": 1000,
                    }
                ],
            },
            "status-1",
        )
        feedback = self._output(
            "event",
            "playback.update",
            {
                "playbackContextId": "context-1",
                "sourceClientId": "player-1",
                "deviceSessionId": "device:player-1",
                "origin": "passive",
                "controlVersion": 1,
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 1200,
                "clientSeq": 1,
                "serverUpdatedAtMs": 1000,
            },
        )

        self.assertEqual(validate_strict_output(status), status)
        self.assertEqual(validate_strict_output(feedback), feedback)

    def test_validates_idle_context_prepare_and_settled_outputs(self):
        idle = self._output(
            "state",
            "playback.context.ensure",
            {
                "playbackContextId": "context-1",
                "authorityClientId": "player-1",
                "queueSongIds": [],
                "state": "idle",
                "positionMs": 0,
                "queueRevision": 1,
                "controlVersion": 1,
                "version": 1,
                "epoch": 1,
            },
            "ensure-1",
        )
        prepare = self._output(
            "command",
            "playback.context.prepare",
            {
                "playbackContextId": "context-1",
                "intentId": "intent-1",
                "controlVersion": 1,
                "sourceClientId": "controller-1",
            },
        )
        prepared = self._output(
            "event",
            "playback.context.prepared",
            {
                "playbackContextId": "context-1",
                "intentId": "intent-1",
                "ready": False,
                "errorCode": "queue_required",
                "controlVersion": 1,
            },
        )
        settled = self._output(
            "event",
            "playback.control.settled",
            {
                "playbackContextId": "context-1",
                "epoch": 1,
                "commandControlVersion": 2,
                "status": "failed",
                "errorCode": "dependency_failed",
                "dependsOnControlVersion": 1,
                "controlVersion": 3,
                "appliedControlVersion": 1,
                "requestingClientId": "controller-1",
                "serverUpdatedAtMs": 1000,
            },
        )

        for message in (idle, prepare, prepared, settled):
            with self.subTest(action=message["action"]):
                self.assertEqual(validate_strict_output(message), message)

        invalid = copy.deepcopy(settled)
        invalid["payload"]["sourceClientId"] = "player-1"
        with self.assertRaises(StrictOutputValidationError):
            validate_strict_output(invalid)

        invalid = copy.deepcopy(settled)
        invalid["payload"]["errorCode"] = "execution_unknown"
        with self.assertRaises(StrictOutputValidationError):
            validate_strict_output(invalid)

    def test_validates_device_volume_outputs_and_extended_device_list(self):
        command = self._output(
            "command",
            "device.setVolume",
            {"sourceClientId": "controller-1", "volume": 65},
        )
        feedback = self._output(
            "event",
            "device.volume.update",
            {
                "sourceClientId": "player-1",
                "deviceSessionId": "device:player-1",
                "volume": 65,
                "clientSeq": 1,
                "serverUpdatedAtMs": 1000,
            },
        )
        capabilities = self._register_request()["payload"]["capabilities"]
        capabilities["remoteVolumeControl"] = True
        device_list = self._output(
            "state",
            "device.list",
            {
                "devices": [
                    {
                        "clientId": "player-1",
                        "deviceSessionId": "device:player-1",
                        "deviceName": "Player",
                        "roles": ["player"],
                        "capabilities": capabilities,
                        "volumeState": {
                            "volume": 65,
                            "clientSeq": 1,
                            "serverUpdatedAtMs": 1000,
                        },
                    }
                ]
            },
            "device-list-1",
        )

        self.assertEqual(validate_strict_output(command), command)
        self.assertEqual(validate_strict_output(feedback), feedback)
        self.assertEqual(validate_strict_output(device_list), device_list)

    def test_device_list_output_requires_request_id(self):
        device_list = self._output(
            "state",
            "device.list",
            {"devices": []},
        )

        with self.assertRaisesRegex(
            StrictOutputValidationError,
            "requires requestId",
        ):
            validate_strict_output(device_list)

    def test_validates_context_list_and_binding_event_outputs(self):
        response = self._output(
            "state",
            "playback.context.list",
            {
                "contexts": [
                    {
                        "playbackContextId": "context-1",
                        "authorityClientId": "player-1",
                        "authorityDeviceSessionId": "device:player-1",
                    },
                    {
                        "playbackContextId": "context-2",
                        "authorityClientId": "player-1",
                        "authorityDeviceSessionId": "device:player-1",
                    },
                ]
            },
            "context-list-1",
        )
        empty = self._output(
            "state",
            "playback.context.list",
            {"contexts": []},
            "context-list-empty-1",
        )
        changed = self._output(
            "event",
            "playback.context.bindings.changed",
            {
                "authorityClientId": "player-1",
                "authorityDeviceSessionId": "device:player-1",
            },
        )

        for message in (response, empty, changed):
            with self.subTest(action=message["action"]):
                self.assertEqual(validate_strict_output(message), message)

    def test_canonical_discovery_fixtures_match_executable_validators(self):
        fixture_path = (
            Path(__file__).resolve().parents[2]
            / "tests"
            / "fixtures"
            / "emo_strict_v2"
            / "discovery.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

        self.assertEqual(
            validate_strict_request(fixture["request"]),
            fixture["request"],
        )
        for response in fixture["responses"].values():
            self.assertEqual(
                validate_strict_output(response, registered=True),
                response,
            )
        self.assertEqual(
            validate_strict_output(
                fixture["bindingEvent"],
                registered=True,
            ),
            fixture["bindingEvent"],
        )
        ordering = fixture["ordering"]
        self.assertFalse(ordering["discoveryGenerationIsWireField"])
        self.assertIn("discard any list response", ordering["clientRule"])
        self.assertEqual(
            [scenario["name"] for scenario in ordering["scenarios"]],
            [
                "event_before_stale_list_response",
                "list_response_before_binding_event",
            ],
        )
        self.assertEqual(
            ordering["scenarios"][0]["timeline"][-1][
                "expectedDisposition"
            ],
            "discard",
        )
        self.assertEqual(
            ordering["scenarios"][1]["timeline"][-1][
                "expectedDisposition"
            ],
            "invalidate_and_requery",
        )
        for message in (
            fixture["request"],
            *fixture["responses"].values(),
            fixture["bindingEvent"],
        ):
            self.assertNotIn("discoveryGeneration", json.dumps(message))

    def test_rejects_invalid_context_list_and_binding_event_outputs(self):
        canonical_binding = {
            "playbackContextId": "context-1",
            "authorityClientId": "player-1",
            "authorityDeviceSessionId": "device:player-1",
        }
        invalid_messages = []
        for contexts in (
            [dict(canonical_binding, unexpected=True)],
            [dict(canonical_binding), dict(canonical_binding)],
            [
                dict(canonical_binding, playbackContextId="context-2"),
                dict(canonical_binding, playbackContextId="context-1"),
            ],
            [
                dict(canonical_binding),
                dict(
                    canonical_binding,
                    playbackContextId="context-2",
                    authorityDeviceSessionId="device:player-2",
                ),
            ],
        ):
            invalid_messages.append(
                self._output(
                    "state",
                    "playback.context.list",
                    {"contexts": contexts},
                    "context-list-invalid",
                )
            )
        missing_request_id = self._output(
            "state",
            "playback.context.list",
            {"contexts": []},
        )
        event_with_request_id = self._output(
            "event",
            "playback.context.bindings.changed",
            {
                "authorityClientId": "player-1",
                "authorityDeviceSessionId": "device:player-1",
            },
            "changed-1",
        )
        invalid_messages.extend((missing_request_id, event_with_request_id))

        for message in invalid_messages:
            with self.subTest(message=message):
                with self.assertRaises(StrictOutputValidationError):
                    validate_strict_output(message)

    def test_output_provenance_uses_explicit_registration_state(self):
        pre_register_error = self._output(
            "system",
            "system.error",
            {
                "action": "playback.context.list",
                "code": "unauthorized",
                "message": "Register first",
                "retryable": False,
            },
            "context-list-early",
        )
        del pre_register_error["connectionNonce"]
        del pre_register_error["connectionEpoch"]

        self.assertEqual(
            validate_strict_output(pre_register_error, registered=False),
            pre_register_error,
        )
        with self.assertRaises(StrictOutputValidationError):
            validate_strict_output(pre_register_error, registered=True)

        registered_error = self._output(
            "system",
            "system.error",
            {
                "action": "playback.context.list",
                "code": "forbidden",
                "message": "Controller required",
                "retryable": False,
            },
            "context-list-forbidden",
        )
        self.assertEqual(
            validate_strict_output(registered_error, registered=True),
            registered_error,
        )
        with self.assertRaises(StrictOutputValidationError):
            validate_strict_output(registered_error, registered=False)

    def test_validates_broadcast_status_and_timed_push_outputs(self):
        snapshot = {
            "playbackContextId": "context-1",
            "broadcastId": "broadcast-1",
            "ownerClientId": "controller-1",
            "authorityClientId": "player-1",
            "queueSongIds": ["song-1"],
            "currentIndex": 0,
            "trackId": "song-1",
            "positionMs": 0,
            "state": "playing",
            "version": 2,
            "queueRevision": 1,
            "controlVersion": 2,
            "epoch": 1,
            "serverUpdatedAtMs": 1000,
            "playbackRate": 1.0,
            "participants": ["player-1"],
        }
        status = self._output(
            "system",
            "system.ack",
            {
                "action": "broadcast.status",
                "broadcast": snapshot,
                "participantStates": [
                    {
                        "broadcastId": "broadcast-1",
                        "clientId": "player-1",
                        "state": "playing",
                        "positionMs": 0,
                        "online": True,
                    }
                ],
            },
            "broadcast-status-1",
        )
        timed_snapshot = dict(
            snapshot,
            effectiveAtServerMs=1250,
            serverTimeMs=1000,
        )
        push = self._output(
            "command",
            "broadcast.play",
            timed_snapshot,
        )

        self.assertEqual(validate_strict_output(status), status)
        self.assertEqual(validate_strict_output(push), push)

    def test_rejects_unknown_null_and_forbidden_output_fields(self):
        messages = [
            self._output(
                "system",
                "system.ack",
                {"action": "player.play", "unexpected": True},
                "play-1",
            ),
            self._output(
                "event",
                "playback.context.closed",
                {"playbackContextId": None},
            ),
            self._output(
                "event",
                "playback.context.closed",
                {"playbackContextId": "context-1", "sessionId": "legacy"},
            ),
        ]

        for message in messages:
            with self.subTest(message=message):
                with self.assertRaises(StrictOutputValidationError):
                    validate_strict_output(message)

    def test_rejects_request_id_on_push_and_missing_registered_provenance(self):
        push_with_request_id = self._output(
            "event",
            "playback.context.closed",
            {"playbackContextId": "context-1"},
            "close-1",
        )
        missing_provenance = self._output(
            "event",
            "playback.context.closed",
            {"playbackContextId": "context-1"},
        )
        del missing_provenance["connectionNonce"]
        del missing_provenance["connectionEpoch"]

        with self.assertRaises(StrictOutputValidationError):
            validate_strict_output(push_with_request_id)
        with self.assertRaises(StrictOutputValidationError):
            validate_strict_output(missing_provenance)

    def test_output_action_inventory_is_closed(self):
        self.assertEqual(len(STRICT_OUTPUT_ACTIONS), 33)
        self.assertIn("system.ack", STRICT_OUTPUT_ACTIONS)
        self.assertIn("device.setVolume", STRICT_OUTPUT_ACTIONS)
        self.assertIn("device.volume.update", STRICT_OUTPUT_ACTIONS)
        self.assertIn("playback.context.list", STRICT_OUTPUT_ACTIONS)
        self.assertIn("playback.context.ensure", STRICT_OUTPUT_ACTIONS)
        self.assertIn("playback.context.prepare", STRICT_OUTPUT_ACTIONS)
        self.assertIn("playback.context.prepared", STRICT_OUTPUT_ACTIONS)
        self.assertIn("playback.control.settled", STRICT_OUTPUT_ACTIONS)
        self.assertNotIn("playback.context.create", STRICT_OUTPUT_ACTIONS)
        self.assertIn(
            "playback.context.bindings.changed",
            STRICT_OUTPUT_ACTIONS,
        )
        self.assertIn("playback.context.status", STRICT_OUTPUT_ACTIONS)
        self.assertIn("playback.handoff.status", STRICT_OUTPUT_ACTIONS)
        self.assertIn("broadcast.stop", STRICT_OUTPUT_ACTIONS)


if __name__ == "__main__":
    unittest.main()
