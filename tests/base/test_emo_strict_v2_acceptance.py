import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supysonic.emo.strict_v2_acceptance import (
    FAULT_DIRECTORY_ENV,
    arm_binding_emit_failure,
    clear_binding_emit_failure,
    consume_binding_emit_failure,
    main,
)


class StrictV2AcceptanceFaultTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(
            os.environ,
            {FAULT_DIRECTORY_ENV: self.directory.name},
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def test_binding_emit_fault_is_one_shot(self):
        marker = arm_binding_emit_failure(
            "alice",
            "controller-1",
            "device:controller-1",
            ttl_seconds=60,
        )

        self.assertTrue(marker.is_file())
        self.assertTrue(
            consume_binding_emit_failure(
                "alice",
                "controller-1",
                "device:controller-1",
            )
        )
        self.assertFalse(marker.exists())
        self.assertFalse(
            consume_binding_emit_failure(
                "alice",
                "controller-1",
                "device:controller-1",
            )
        )

    def test_expired_or_invalid_fault_fails_closed(self):
        marker = arm_binding_emit_failure(
            "alice",
            "controller-1",
            "device:controller-1",
            ttl_seconds=1,
        )
        payload = json.loads(marker.read_text(encoding="utf-8"))
        self.assertFalse(
            consume_binding_emit_failure(
                "alice",
                "controller-1",
                "device:controller-1",
                now_ms=payload["expiresAtMs"] + 1,
            )
        )

        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{", encoding="utf-8")
        self.assertFalse(
            consume_binding_emit_failure(
                "alice",
                "controller-1",
                "device:controller-1",
            )
        )
        self.assertFalse(marker.exists())

    def test_clear_and_cli_commands(self):
        self.assertEqual(
            main(
                [
                    "arm-binding-emit",
                    "--user",
                    "alice",
                    "--client-id",
                    "controller-1",
                    "--device-session-id",
                    "device:controller-1",
                ]
            ),
            0,
        )
        markers = list(Path(self.directory.name).glob("binding-emit-*.json"))
        self.assertEqual(len(markers), 1)
        self.assertTrue(
            clear_binding_emit_failure(
                "alice",
                "controller-1",
                "device:controller-1",
            )
        )
        self.assertFalse(
            clear_binding_emit_failure(
                "alice",
                "controller-1",
                "device:controller-1",
            )
        )

    def test_rejects_invalid_identifiers_and_ttl(self):
        with self.assertRaises(ValueError):
            arm_binding_emit_failure("", "controller-1", "device:controller-1")
        with self.assertRaises(ValueError):
            arm_binding_emit_failure(
                "alice",
                "controller-1",
                "device:controller-1",
                ttl_seconds=0,
            )


if __name__ == "__main__":
    unittest.main()
