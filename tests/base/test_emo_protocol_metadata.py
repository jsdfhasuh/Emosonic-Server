import copy
import json
import os
import re
import unittest
from unittest import mock

from supysonic.emo import protocol_metadata


class EmoProtocolMetadataTestCase(unittest.TestCase):
    def _fingerprint_source(self):
        return {
            "protocolName": "emosonic-playback-context-v2-registration",
            "coveredActions": {
                "clientToServer": ["device.register"],
                "serverToClient": ["system.ack(device.register)"],
            },
            "schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": ["phone"],
                    }
                },
                "required": ["name"],
            },
        }

    def test_descriptor_exposes_initial_protocol_version(self):
        descriptor = protocol_metadata.get_strict_v2_registration_descriptor()

        self.assertEqual(
            descriptor["protocolName"],
            "emosonic-playback-context-v2-registration",
        )
        self.assertEqual(descriptor["protocolVersion"], "2.3.0")
        self.assertEqual(
            descriptor["coveredActions"]["clientToServer"],
            ["device.register"],
        )

    def test_descriptor_returns_a_defensive_copy(self):
        descriptor = protocol_metadata.get_strict_v2_registration_descriptor()
        descriptor["coveredActions"]["clientToServer"].append("unexpected.action")

        current_descriptor = protocol_metadata.get_strict_v2_registration_descriptor()

        self.assertEqual(
            current_descriptor["coveredActions"]["clientToServer"],
            ["device.register"],
        )

    def test_schema_hash_is_lowercase_sha256(self):
        schema_hash = protocol_metadata.get_strict_v2_schema_hash()

        self.assertRegex(schema_hash, r"^[0-9a-f]{64}$")

    def test_schema_hash_is_stable_for_key_order_and_whitespace(self):
        fingerprint_source = self._fingerprint_source()
        reordered_source = {
            "schema": {
                "required": ["name"],
                "properties": {
                    "name": {
                        "enum": ["phone"],
                        "type": "string",
                    }
                },
                "type": "object",
            },
            "coveredActions": {
                "serverToClient": ["system.ack(device.register)"],
                "clientToServer": ["device.register"],
            },
            "protocolName": "emosonic-playback-context-v2-registration",
        }
        indented_source = json.loads(json.dumps(fingerprint_source, indent=2))
        compact_source = json.loads(
            json.dumps(fingerprint_source, separators=(",", ":"))
        )

        expected_hash = protocol_metadata.calculate_strict_v2_schema_hash(
            fingerprint_source
        )

        self.assertEqual(
            protocol_metadata.calculate_strict_v2_schema_hash(reordered_source),
            expected_hash,
        )
        self.assertEqual(
            protocol_metadata.calculate_strict_v2_schema_hash(indented_source),
            expected_hash,
        )
        self.assertEqual(
            protocol_metadata.calculate_strict_v2_schema_hash(compact_source),
            expected_hash,
        )

    def test_schema_hash_changes_for_descriptor_semantics(self):
        fingerprint_source = self._fingerprint_source()
        expected_hash = protocol_metadata.calculate_strict_v2_schema_hash(
            fingerprint_source
        )

        changed_protocol_name = copy.deepcopy(fingerprint_source)
        changed_protocol_name["protocolName"] = "another-registration-profile"
        changed_coverage = copy.deepcopy(fingerprint_source)
        changed_coverage["coveredActions"]["serverToClient"].append(
            "system.error(device.register)"
        )
        changed_type = copy.deepcopy(fingerprint_source)
        changed_type["schema"]["properties"]["name"]["type"] = "integer"
        changed_required = copy.deepcopy(fingerprint_source)
        changed_required["schema"]["required"] = []
        changed_enum = copy.deepcopy(fingerprint_source)
        changed_enum["schema"]["properties"]["name"]["enum"].append("tablet")

        for changed_source in (
            changed_protocol_name,
            changed_coverage,
            changed_type,
            changed_required,
            changed_enum,
        ):
            self.assertNotEqual(
                protocol_metadata.calculate_strict_v2_schema_hash(changed_source),
                expected_hash,
            )

    def test_schema_hash_does_not_include_protocol_version(self):
        descriptor = protocol_metadata.get_strict_v2_registration_descriptor()
        changed_version_descriptor = copy.deepcopy(descriptor)
        changed_version_descriptor["protocolVersion"] = "2.2.0"

        self.assertEqual(
            protocol_metadata.calculate_strict_v2_schema_hash(
                protocol_metadata._fingerprint_source(descriptor)
            ),
            protocol_metadata.calculate_strict_v2_schema_hash(
                protocol_metadata._fingerprint_source(changed_version_descriptor)
            ),
        )

    def test_descriptor_validation_identifies_missing_or_invalid_fields(self):
        descriptor = protocol_metadata.get_strict_v2_registration_descriptor()
        missing_version = copy.deepcopy(descriptor)
        del missing_version["protocolVersion"]
        invalid_version = copy.deepcopy(descriptor)
        invalid_version["protocolVersion"] = 2
        invalid_actions = copy.deepcopy(descriptor)
        invalid_actions["coveredActions"]["clientToServer"] = ["", 1]

        with self.assertRaisesRegex(ValueError, "protocolVersion"):
            protocol_metadata._validate_descriptor(missing_version)
        with self.assertRaisesRegex(ValueError, "protocolVersion"):
            protocol_metadata._validate_descriptor(invalid_version)
        with self.assertRaisesRegex(ValueError, "clientToServer"):
            protocol_metadata._validate_descriptor(invalid_actions)

    def test_server_build_commit_returns_a_full_sha(self):
        commit = "a" * 40

        with mock.patch.dict(
            os.environ,
            {"EMO_SERVER_BUILD_COMMIT": commit},
            clear=True,
        ):
            self.assertEqual(protocol_metadata.get_server_build_commit(), commit)

    def test_server_build_commit_returns_unknown_when_missing_or_invalid(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(protocol_metadata.get_server_build_commit(), "unknown")
        for invalid_commit in ("", "not-a-full-sha", "A" * 40, "a" * 40 + " "):
            with mock.patch.dict(
                os.environ,
                {"EMO_SERVER_BUILD_COMMIT": invalid_commit},
                clear=True,
            ):
                self.assertEqual(protocol_metadata.get_server_build_commit(), "unknown")

    def test_server_build_commit_is_not_cached(self):
        first_commit = "a" * 40
        second_commit = "b" * 40

        with mock.patch.dict(
            os.environ,
            {"EMO_SERVER_BUILD_COMMIT": first_commit},
            clear=True,
        ):
            self.assertEqual(
                protocol_metadata.get_server_build_commit(),
                first_commit,
            )
        with mock.patch.dict(
            os.environ,
            {"EMO_SERVER_BUILD_COMMIT": second_commit},
            clear=True,
        ):
            self.assertEqual(
                protocol_metadata.get_server_build_commit(),
                second_commit,
            )

    def test_metadata_uses_current_values_and_returns_a_new_mapping(self):
        commit = "c" * 40

        with mock.patch.dict(
            os.environ,
            {"EMO_SERVER_BUILD_COMMIT": commit},
            clear=True,
        ):
            metadata = protocol_metadata.get_strict_v2_metadata()
            metadata["protocolVersion"] = "changed"
            current_metadata = protocol_metadata.get_strict_v2_metadata()

        self.assertEqual(current_metadata["protocolVersion"], "2.3.0")
        self.assertEqual(current_metadata["serverBuildCommit"], commit)
        self.assertRegex(current_metadata["schemaHash"], r"^[0-9a-f]{64}$")

    def test_registration_metadata_adds_connection_evidence(self):
        nonce = "nonce-for-current-socket"

        static_metadata = protocol_metadata.get_strict_v2_metadata()
        metadata = protocol_metadata.get_strict_v2_registration_metadata(nonce)

        self.assertNotIn("connectionNonce", static_metadata)
        self.assertNotIn("connectionEpoch", static_metadata)
        self.assertEqual(
            {
                "protocolVersion": metadata["protocolVersion"],
                "schemaHash": metadata["schemaHash"],
                "serverBuildCommit": metadata["serverBuildCommit"],
            },
            static_metadata,
        )
        self.assertEqual(metadata["connectionNonce"], nonce)
        self.assertEqual(
            metadata["connectionEpoch"],
            protocol_metadata.STRICT_V2_CONNECTION_EPOCH,
        )

        with self.assertRaisesRegex(ValueError, "connectionNonce"):
            protocol_metadata.get_strict_v2_registration_metadata("")
