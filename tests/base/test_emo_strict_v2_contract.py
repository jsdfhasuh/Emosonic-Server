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
                    "remoteVolumeControl": False,
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
                "intentId": "intent-1",
                "participants": ["client-%d" % index for index in range(22)],
            },
        }
        with self.assertRaisesRegex(StrictRequestValidationError, "21"):
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
            "positionSampledAtServerMs": 1000,
            "playbackRate": 1.0,
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

    def test_validates_broadcast_feedback_request_shapes(self):
        common = {
            "playbackContextId": "context-1",
            "broadcastId": "broadcast-1",
            "deviceSessionId": "device:participant-1",
            "deliveryId": "delivery-1",
            "clientSeq": 1,
        }
        applied = dict(
            common,
            executionStatus="applied",
            appliedBroadcastRevision=2,
            queueIndex=0,
            trackId="song-1",
            state="playing",
            positionMs=1250,
            playbackRate=1.0,
        )
        failed = dict(
            common,
            executionStatus="failed",
            failedBroadcastRevision=2,
            lastAppliedBroadcastRevision=1,
            errorCode="track_load_failed",
            errorMessage="Unable to load target",
        )
        for index, payload in enumerate((applied, failed), 1):
            request = {
                "type": "event",
                "action": "broadcast.feedback",
                "requestId": "broadcast-feedback-%d" % index,
                "payload": payload,
            }
            self.assertEqual(validate_strict_request(request), request)

        invalid = {
            "type": "event",
            "action": "broadcast.feedback",
            "requestId": "broadcast-feedback-invalid-1",
            "payload": dict(applied, errorCode="execution_failed"),
        }
        with self.assertRaises(StrictRequestValidationError):
            validate_strict_request(invalid)
        invalid["payload"] = dict(
            failed,
            lastAppliedBroadcastRevision=2,
        )
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
                        "positionSampledAtServerMs": 950,
                        "playbackRate": 1.0,
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
                "positionSampledAtServerMs": 950,
                "playbackRate": 1.0,
                "clientSeq": 1,
                "serverUpdatedAtMs": 1000,
            },
        )

        self.assertEqual(validate_strict_output(status), status)
        self.assertEqual(validate_strict_output(feedback), feedback)

    def test_registration_schema_hash_is_optional_and_non_gating(self):
        metadata = {
            "protocolVersion": "2.8.0",
            "serverBuildCommit": "unknown",
            "connectionNonce": "nonce-1",
            "connectionEpoch": 1,
        }
        capabilities = self._register_request()["payload"]["capabilities"]
        ack = self._output(
            "system",
            "system.ack",
            {
                "action": "device.register",
                "clientId": "phone-1",
                "deviceSessionId": "device:phone-1",
                "negotiatedCapabilities": capabilities,
                "strictV2": metadata,
            },
            "register-1",
        )

        self.assertEqual(validate_strict_output(ack), ack)
        for value in ("", "changed", 7, None, {"any": "shape"}):
            observed = copy.deepcopy(ack)
            observed["payload"]["strictV2"]["schemaHash"] = value
            with self.subTest(schema_hash=value):
                self.assertEqual(validate_strict_output(observed), observed)

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
            "intentId": "intent-1",
            "ownerClientId": "controller-1",
            "authorityClientId": "player-1",
            "authorityDeviceSessionId": "device:player-1",
            "lifecycleState": "active",
            "broadcastRevision": 2,
            "queueSongIds": ["song-1"],
            "currentIndex": 0,
            "trackId": "song-1",
            "positionMs": 0,
            "state": "playing",
            "sourceVersion": 2,
            "sourceQueueRevision": 1,
            "sourceControlVersion": 2,
            "sourceEpoch": 1,
            "serverUpdatedAtMs": 1000,
            "playbackRate": 1.0,
            "participants": ["participant-1"],
        }
        status = self._output(
            "system",
            "system.ack",
            {
                "action": "broadcast.status",
                "serverTimeMs": 1100,
                "broadcast": snapshot,
                "participantStates": [
                    {
                        "broadcastId": "broadcast-1",
                        "clientId": "participant-1",
                        "deviceSessionId": "device:participant-1",
                        "targetBroadcastRevision": 2,
                        "targetDeliveryId": "delivery-1",
                        "deadlineBroadcastRevision": 2,
                        "syncStatus": "pending",
                        "feedbackDeadlineAtServerMs": 9250,
                        "online": True,
                    }
                ],
            },
            "broadcast-status-1",
        )
        timed_snapshot = dict(
            snapshot,
            deliveryId="delivery-1",
            effectiveAtServerMs=1250,
            serverTimeMs=1000,
        )
        pushes = [
            self._output("event", action, timed_snapshot)
            for action in (
                "broadcast.play",
                "broadcast.progress",
                "broadcast.state.sync",
            )
        ]
        terminal_snapshot = dict(
            snapshot,
            lifecycleState="stopped",
            broadcastRevision=3,
            deliveryId="terminal-delivery-1",
        )
        terminal_push = self._output(
            "event",
            "broadcast.stop",
            terminal_snapshot,
        )
        active_resync = self._output(
            "event",
            "broadcast.resync",
            dict(timed_snapshot, deliveryId="resync-delivery-1"),
        )
        waiting_resync = self._output(
            "event",
            "broadcast.resync",
            dict(
                snapshot,
                lifecycleState="waitingForSource",
                deliveryId="resync-delivery-2",
            ),
        )
        waiting_push = self._output(
            "event",
            "broadcast.waiting",
            dict(
                snapshot,
                lifecycleState="waitingForSource",
                state="paused",
                deliveryId="waiting-delivery-1",
            ),
        )
        resume_push = self._output(
            "event",
            "broadcast.resume",
            dict(timed_snapshot, deliveryId="resume-delivery-1"),
        )
        untimed_progress = self._output(
            "event",
            "broadcast.progress",
            dict(snapshot, deliveryId="untimed-delivery-1"),
        )

        self.assertEqual(validate_strict_output(status), status)
        failed_status = copy.deepcopy(status)
        failed_status["payload"]["participantStates"][0].update(
            {
                "syncStatus": "failed",
                "failedBroadcastRevision": 2,
                "errorCode": "track_load_failed",
                "errorMessage": "Unable to load target",
            }
        )
        self.assertEqual(validate_strict_output(failed_status), failed_status)
        invalid_failed_status = copy.deepcopy(failed_status)
        invalid_failed_status["payload"]["participantStates"][0][
            "failedBroadcastRevision"
        ] = True
        with self.assertRaises(StrictOutputValidationError):
            validate_strict_output(invalid_failed_status)
        invalid_failed_status = copy.deepcopy(failed_status)
        invalid_failed_status["payload"]["participantStates"][0][
            "errorCode"
        ] = "database_error"
        with self.assertRaises(StrictOutputValidationError):
            validate_strict_output(invalid_failed_status)
        for push in pushes:
            self.assertEqual(validate_strict_output(push), push)
        self.assertEqual(validate_strict_output(terminal_push), terminal_push)
        self.assertEqual(validate_strict_output(active_resync), active_resync)
        self.assertEqual(validate_strict_output(waiting_resync), waiting_resync)
        self.assertEqual(validate_strict_output(waiting_push), waiting_push)
        self.assertEqual(validate_strict_output(resume_push), resume_push)
        with self.assertRaises(StrictOutputValidationError):
            validate_strict_output(untimed_progress)
        untimed_active_resync = copy.deepcopy(active_resync)
        del untimed_active_resync["payload"]["effectiveAtServerMs"]
        del untimed_active_resync["payload"]["serverTimeMs"]
        with self.assertRaises(StrictOutputValidationError):
            validate_strict_output(untimed_active_resync)

    def test_validates_broadcast_feedback_confirmation_output(self):
        applied = self._output(
            "event",
            "broadcast.feedback",
            {
                "playbackContextId": "context-1",
                "broadcastId": "broadcast-1",
                "sourceClientId": "participant-1",
                "deviceSessionId": "device:participant-1",
                "deliveryId": "delivery-1",
                "executionStatus": "applied",
                "clientSeq": 1,
                "serverUpdatedAtMs": 1000,
                "appliedBroadcastRevision": 2,
                "queueIndex": 0,
                "trackId": "song-1",
                "state": "stopped",
                "positionMs": 1250,
                "playbackRate": 1.0,
                "restoreCompleted": True,
            },
        )
        failed = self._output(
            "event",
            "broadcast.feedback",
            {
                "playbackContextId": "context-1",
                "broadcastId": "broadcast-1",
                "sourceClientId": "participant-1",
                "deviceSessionId": "device:participant-1",
                "deliveryId": "delivery-1",
                "executionStatus": "failed",
                "clientSeq": 2,
                "serverUpdatedAtMs": 1100,
                "failedBroadcastRevision": 2,
                "lastAppliedBroadcastRevision": 1,
                "errorCode": "track_load_failed",
                "errorMessage": "Unable to load target",
            },
        )
        rejected = self._output(
            "event",
            "broadcast.feedback.rejected",
            {
                "playbackContextId": "context-1",
                "broadcastId": "broadcast-1",
                "deviceSessionId": "device:participant-1",
                "clientSeq": 3,
                "deliveryId": "delivery-old",
                "rejectedBroadcastRevision": 3,
                "currentBroadcastRevision": 2,
                "minimumRetainedBroadcastRevision": 1,
                "errorCode": "revision_ahead",
                "serverUpdatedAtMs": 1200,
            },
        )

        self.assertEqual(validate_strict_output(applied), applied)
        self.assertEqual(validate_strict_output(failed), failed)
        self.assertEqual(validate_strict_output(rejected), rejected)
        invalid_applied = copy.deepcopy(applied)
        invalid_applied["payload"]["restoreCompleted"] = False
        with self.assertRaises(StrictOutputValidationError):
            validate_strict_output(invalid_applied)
        invalid_failed = copy.deepcopy(failed)
        invalid_failed["payload"]["errorCode"] = "database_error"
        with self.assertRaises(StrictOutputValidationError):
            validate_strict_output(invalid_failed)
        invalid_rejected = copy.deepcopy(rejected)
        invalid_rejected["payload"]["errorCode"] = "revision_expired"
        with self.assertRaises(StrictOutputValidationError):
            validate_strict_output(invalid_rejected)

    def test_validates_compact_broadcast_restore_output(self):
        restore = self._output(
            "event",
            "broadcast.restore",
            {
                "playbackContextId": "context-source",
                "broadcastId": "broadcast-1",
                "deviceSessionId": "device:participant-1",
                "terminalBroadcastRevision": 3,
                "deliveryId": "delivery-restore-1",
                "suspendedPlaybackContextId": "context-original",
                "suspendedEpoch": 1,
                "suspendedVersion": 2,
                "suspendedQueueRevision": 2,
                "suspendedControlVersion": 2,
                "suspendedAppliedControlVersion": 2,
                "lastAppliedBroadcastRevision": 2,
                "queueIndex": 0,
                "trackId": "song-1",
                "state": "stopped",
                "positionMs": 1200,
                "playbackRate": 1.0,
                "terminalAtServerMs": 30000,
            },
        )

        self.assertEqual(validate_strict_output(restore), restore)
        status = self._output(
            "system",
            "system.ack",
            {
                "action": "broadcast.status",
                "serverTimeMs": 31000,
                "recovery": dict(restore["payload"]),
            },
            "broadcast-status-recovery-1",
        )
        self.assertEqual(validate_strict_output(status), status)
        invalid = copy.deepcopy(restore)
        invalid["payload"]["state"] = "paused"
        with self.assertRaises(StrictOutputValidationError):
            validate_strict_output(invalid)

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
        self.assertEqual(len(STRICT_OUTPUT_ACTIONS), 41)
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
        self.assertIn("broadcast.progress", STRICT_OUTPUT_ACTIONS)
        self.assertIn("broadcast.state.sync", STRICT_OUTPUT_ACTIONS)
        self.assertIn("broadcast.feedback", STRICT_OUTPUT_ACTIONS)
        self.assertIn("broadcast.feedback.rejected", STRICT_OUTPUT_ACTIONS)
        self.assertIn("broadcast.resync", STRICT_OUTPUT_ACTIONS)
        self.assertIn("broadcast.waiting", STRICT_OUTPUT_ACTIONS)
        self.assertIn("broadcast.resume", STRICT_OUTPUT_ACTIONS)
        self.assertIn("broadcast.restore", STRICT_OUTPUT_ACTIONS)
        self.assertIn("broadcast.stop", STRICT_OUTPUT_ACTIONS)


if __name__ == "__main__":
    unittest.main()
