"""Validate output of the complete page JS against the actual workspace contract."""
import copy
import json
import os
from pathlib import Path
import subprocess
import unittest
from typing import Dict, List, Optional

from supysonic.emo import strict_v2_contract


ROOT = Path(__file__).resolve().parents[2]


def capture_page_messages(
    *,
    prepare: Optional[Dict[str, object]] = None,
    commit: Optional[Dict[str, object]] = None,
    options: Optional[Dict[str, object]] = None,
) -> List[Dict[str, object]]:
    result = subprocess.run(
        [os.environ.get("NODE", "node"), str(ROOT / "tests/js/web_player_handoff_harness.js")],
        input=json.dumps({"prepare": prepare, "commit": commit, "options": options or {}}),
        text=True, encoding="utf-8", capture_output=True, cwd=ROOT, timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


class WebHandoffWireTestCase(unittest.TestCase):
    def test_actual_page_handoff_lifecycle_regressions(self):
        result = subprocess.run(
            [os.environ.get("NODE", "node"), "--test",
             "tests/js/emo_strict_v2_client.test.js", "tests/js/web_player_handoff.test.js"],
            text=True, encoding="utf-8", capture_output=True, cwd=ROOT, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_actual_page_ready_branches_use_complete_workspace_contract(self):
        self.assertEqual(
            Path(strict_v2_contract.__file__).resolve(),
            ROOT / "supysonic/emo/strict_v2_contract.py",
        )
        for options in ({}, {"contextId": "busy"}, {"hidden": True},
                        {"gesture": False}, {"mediaFailure": True}, {"restore": True}):
            with self.subTest(options=options):
                ready = [m for m in capture_page_messages(options=options)
                         if m["action"] == "playback.ready"]
                self.assertEqual(len(ready), 1)
                strict_v2_contract.validate_strict_request(ready[0])

    def test_actual_complete_and_all_required_field_rejections(self):
        command = {
            "playbackContextId": "ctx-handoff", "handoffId": "handoff-1",
            "sourceClientId": "web-player-source", "controlVersion": 6,
            "serverTimeMs": 1780000000000, "effectiveAtServerMs": 1780000000250,
            "positionMs": 1000, "playbackRate": 1,
        }
        complete = [m for m in capture_page_messages(commit=command)
                    if m["action"] == "playback.handoff.complete"]
        self.assertEqual(len(complete), 1)
        strict_v2_contract.validate_strict_request(complete[0])
        for field in ("playbackContextId", "handoffId", "deviceSessionId", "queueIndex",
                      "trackId", "state", "positionMs", "positionSampledAtServerMs",
                      "playbackRate", "appliedControlVersion", "clientSeq"):
            for invalid in ("missing", None, [], False):
                with self.subTest(field=field, invalid=invalid):
                    message = copy.deepcopy(complete[0])
                    if invalid == "missing":
                        message["payload"].pop(field)
                    else:
                        message["payload"][field] = invalid
                    with self.assertRaises(strict_v2_contract.StrictRequestValidationError):
                        strict_v2_contract.validate_strict_request(message)
