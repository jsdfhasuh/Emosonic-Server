#!/usr/bin/env python3
"""Run the exact automated evidence mapped to strict-v2 EARS requirements."""

import json
import sys
import unittest
from pathlib import Path
from typing import Dict, List


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
MANIFEST_PATH = (
    REPOSITORY_ROOT / "tests" / "fixtures" / "emo_strict_v2" / "manifest.json"
)


def _mapped_test_methods() -> List[str]:
    manifest: Dict[str, object] = json.loads(
        MANIFEST_PATH.read_text(encoding="utf-8")
    )
    requirements = manifest.get("requirements")
    if not isinstance(requirements, dict):
        raise ValueError("strict-v2 manifest requirements must be an object")

    expected = {"REQ-%03d" % number for number in range(1, 46)}
    if set(requirements) != expected:
        raise ValueError("strict-v2 manifest must map REQ-001 through REQ-045")

    methods = set()
    for requirement, mapping in requirements.items():
        if not isinstance(mapping, dict):
            raise ValueError("%s mapping must be an object" % requirement)
        test_methods = mapping.get("testMethods")
        if not isinstance(test_methods, list) or not test_methods:
            raise ValueError("%s must map at least one test method" % requirement)
        for test_method in test_methods:
            if not isinstance(test_method, str) or not test_method.startswith("tests."):
                raise ValueError("%s contains an invalid test method" % requirement)
            methods.add(test_method)
    return sorted(methods)


def main() -> int:
    test_methods = _mapped_test_methods()
    print(
        "Running %d strict-v2 EARS evidence tests" % len(test_methods),
        flush=True,
    )
    suite = unittest.defaultTestLoader.loadTestsFromNames(test_methods)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
