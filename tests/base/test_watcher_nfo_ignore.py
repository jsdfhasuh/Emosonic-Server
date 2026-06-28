import time
import unittest

from types import SimpleNamespace
from unittest.mock import Mock, patch

from supysonic.watcher import FLAG_NFO, OP_SCAN, ScannerProcessingQueue


class DummyTimer:
    def __init__(self, timeout, callback):
        self.timeout = timeout
        self.callback = callback

    def start(self):
        return None

    def cancel(self):
        return None


class WatcherNfoIgnoreTestCase(unittest.TestCase):
    def setUp(self):
        self.timerPatch = patch("supysonic.watcher.Timer", DummyTimer)
        self.timerPatch.start()
        self.queue = ScannerProcessingQueue(1)

    def tearDown(self):
        self.queue.stop()
        self.timerPatch.stop()

    def test_queue_ignores_suppressed_nfo_path(self):
        self.queue.suppress_nfo_path("/music/Album/album.nfo", ttl=5)

        self.queue.put("/music/Album/album.nfo", OP_SCAN | FLAG_NFO)

        self.assertEqual(list(self.queue._ScannerProcessingQueue__queue), [])

    def test_queue_allows_suppressed_nfo_path_after_ttl(self):
        with patch("supysonic.watcher.time.time", side_effect=[10, 10, 20, 20]):
            self.queue.suppress_nfo_path("/music/Album/album.nfo", ttl=5)
            self.queue.put("/music/Album/album.nfo", OP_SCAN | FLAG_NFO)

        self.assertEqual(
            list(self.queue._ScannerProcessingQueue__queue),
            ["/music/Album/album.nfo"],
        )

    def test_queue_does_not_ignore_other_nfo_paths(self):
        self.queue.suppress_nfo_path("/music/Album/album.nfo", ttl=5)

        self.queue.put("/music/Other/album.nfo", OP_SCAN | FLAG_NFO)

        self.assertEqual(
            list(self.queue._ScannerProcessingQueue__queue),
            ["/music/Other/album.nfo"],
        )

    def test_suppress_nfo_path_prunes_expired_entries(self):
        with patch("supysonic.watcher.time.time", side_effect=[10, 10, 20, 20]):
            self.queue.suppress_nfo_path("/music/Album/album.nfo", ttl=5)
            self.queue.suppress_nfo_path("/music/New/album.nfo", ttl=5)

        self.assertEqual(
            self.queue._ScannerProcessingQueue__suppressed_nfo_paths,
            {"/music/New/album.nfo": 25},
        )


class WatcherQueueRecoveryTestCase(unittest.TestCase):
    @staticmethod
    def _wait_until(predicate, timeout=3, interval=0.05):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()

    @staticmethod
    def _fake_scanner(scan_file):
        return SimpleNamespace(
            scan_file=scan_file,
            find_cover=Mock(),
            find_lost_information=Mock(),
            prune=Mock(),
            stats=lambda: SimpleNamespace(
                lost_covers=SimpleNamespace(artists=0, albums=0),
                lost_covers_albums={},
                lost_covers_artists=[],
                lost_year_albums={},
            ),
        )

    def test_queue_retries_after_database_connection_failure(self):
        queue = ScannerProcessingQueue(0.01)
        scan_file = Mock()
        scanner = self._fake_scanner(scan_file)
        queue.put("/music/Album/track.flac", OP_SCAN)

        with patch(
            "supysonic.watcher.open_connection",
            side_effect=[RuntimeError("too many connections"), True],
        ) as open_connection, patch(
            "supysonic.watcher.close_connection",
        ) as close_connection, patch(
            "supysonic.watcher.Scanner",
            return_value=scanner,
        ), patch(
            "supysonic.watcher.createReviewTasks",
            return_value=0,
        ), patch("supysonic.watcher.logger.exception") as log_exception:
            queue.start()
            try:
                self.assertTrue(self._wait_until(lambda: scan_file.called))
                self.assertTrue(queue.is_alive())
            finally:
                queue.stop()
                queue.join(2)

        self.assertFalse(queue.is_alive())
        self.assertGreaterEqual(open_connection.call_count, 2)
        close_connection.assert_called_once()
        log_exception.assert_called_once()
        scan_file.assert_called_once_with("/music/Album/track.flac")

    def test_queue_continues_after_item_failure(self):
        queue = ScannerProcessingQueue(0.01)
        scan_file = Mock(side_effect=[RuntimeError("scan failed"), None])
        scanner = self._fake_scanner(scan_file)
        queue.put("/music/Album/bad.flac", OP_SCAN)
        queue.put("/music/Album/good.flac", OP_SCAN)

        with patch(
            "supysonic.watcher.open_connection",
            return_value=True,
        ), patch(
            "supysonic.watcher.close_connection",
        ), patch(
            "supysonic.watcher.Scanner",
            return_value=scanner,
        ), patch(
            "supysonic.watcher.createReviewTasks",
            return_value=0,
        ), patch("supysonic.watcher.logger.exception") as log_exception:
            queue.start()
            try:
                self.assertTrue(self._wait_until(lambda: scan_file.call_count >= 2))
            finally:
                queue.stop()
                queue.join(2)

        self.assertFalse(queue.is_alive())
        self.assertEqual(scan_file.call_count, 2)
        scan_file.assert_any_call("/music/Album/bad.flac")
        scan_file.assert_any_call("/music/Album/good.flac")
        log_exception.assert_called_once()

    def test_queue_does_not_close_reused_database_connection(self):
        queue = ScannerProcessingQueue(0.01)
        scan_file = Mock()
        scanner = self._fake_scanner(scan_file)
        queue.put("/music/Album/track.flac", OP_SCAN)

        with patch(
            "supysonic.watcher.open_connection",
            return_value=False,
        ) as open_connection, patch(
            "supysonic.watcher.close_connection",
        ) as close_connection, patch(
            "supysonic.watcher.Scanner",
            return_value=scanner,
        ), patch(
            "supysonic.watcher.createReviewTasks",
            return_value=0,
        ):
            queue.start()
            try:
                self.assertTrue(self._wait_until(lambda: scan_file.called))
            finally:
                queue.stop()
                queue.join(2)

        self.assertFalse(queue.is_alive())
        open_connection.assert_called_once_with(True)
        close_connection.assert_not_called()
        scan_file.assert_called_once_with("/music/Album/track.flac")


if __name__ == "__main__":
    unittest.main()
