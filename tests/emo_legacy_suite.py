import unittest

from tests.base.test_emo_ws import EmoWebSocketTestCase


STRICT_TEST_NAME_MARKERS = ("strict", "_v2_")
STRICT_TEST_NAMES = {
    "test_duplicate_handoff_start_request_is_idempotent",
    "test_persisted_duplicate_handoff_start_rebuilds_missing_prepare",
    "test_persisted_ready_handoff_retry_resends_player_play",
}


def _is_legacy_test(test_name: str) -> bool:
    return test_name not in STRICT_TEST_NAMES and not any(
        marker in test_name for marker in STRICT_TEST_NAME_MARKERS
    )


def load_tests(loader, standard_tests, pattern):
    del standard_tests, pattern
    suite = unittest.TestSuite()
    for test_name in loader.getTestCaseNames(EmoWebSocketTestCase):
        if _is_legacy_test(test_name):
            suite.addTest(EmoWebSocketTestCase(test_name))
    return suite
