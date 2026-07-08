import logging
import os
import shutil
import tempfile
import unittest

from types import SimpleNamespace
from unittest.mock import Mock, patch

from supysonic.config import DefaultConfig
from supysonic.daemon import setup_logging
from supysonic.daemon import logger as daemon_logger


class DaemonLoggingTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._previous_handlers = list(daemon_logger.handlers)
        self._previous_level = daemon_logger.level
        self._previous_propagate = daemon_logger.propagate

        daemon_logger.handlers = []
        daemon_logger.setLevel(logging.NOTSET)
        daemon_logger.propagate = False

    def tearDown(self):
        for handler in list(daemon_logger.handlers):
            handler.close()
        daemon_logger.handlers = self._previous_handlers
        daemon_logger.setLevel(self._previous_level)
        daemon_logger.propagate = self._previous_propagate
        shutil.rmtree(self._tmp_dir)

    def test_info_level_does_not_create_debug_log_file(self):
        log_file = os.path.join(self._tmp_dir, "supysonic-daemon.log")

        setup_logging(
            {
                "log_file": log_file,
                "log_rotate": False,
                "log_level": "INFO",
            }
        )

        daemon_logger.debug("debug hidden")
        daemon_logger.info("info visible")

        with open(log_file, "r", encoding="utf-8") as f:
            log_content = f.read()

        self.assertIn("info visible", log_content)
        self.assertNotIn("debug hidden", log_content)
        self.assertFalse(os.path.exists(os.path.join(self._tmp_dir, "supysonic-daemon.debug.log")))

    def test_debug_level_writes_to_separate_debug_log_file(self):
        log_file = os.path.join(self._tmp_dir, "supysonic-daemon.log")
        debug_log_file = os.path.join(self._tmp_dir, "supysonic-daemon.debug.log")

        setup_logging(
            {
                "log_file": log_file,
                "log_rotate": False,
                "log_level": "DEBUG",
            }
        )

        daemon_logger.debug("debug visible")
        daemon_logger.info("info visible")

        with open(log_file, "r", encoding="utf-8") as f:
            main_log_content = f.read()

        with open(debug_log_file, "r", encoding="utf-8") as f:
            debug_log_content = f.read()

        self.assertIn("info visible", main_log_content)
        self.assertNotIn("debug visible", main_log_content)
        self.assertIn("debug visible", debug_log_content)
        self.assertIn("info visible", debug_log_content)

    def test_repeated_setup_logging_does_not_duplicate_file_output(self):
        log_file = os.path.join(self._tmp_dir, "supysonic-daemon.log")

        config = {
            "log_file": log_file,
            "log_rotate": False,
            "log_level": "INFO",
        }

        setup_logging(config)
        setup_logging(config)

        daemon_logger.info("written once")

        with open(log_file, "r", encoding="utf-8") as f:
            log_lines = [line for line in f.readlines() if "written once" in line]

        self.assertEqual(len(log_lines), 1)

    def test_default_daemon_config_includes_log_dir_and_backup_count(self):
        self.assertIn("log_dir", DefaultConfig.DAEMON)
        self.assertIn("log_backup_count", DefaultConfig.DAEMON)

    def test_log_dir_creates_daemon_category_files(self):
        from supysonic.logging_manager import configure_daemon_logging

        configure_daemon_logging(
            {
                "log_dir": self._tmp_dir,
                "log_rotate": False,
                "log_level": "INFO",
                "log_backup_count": 7,
            },
            logger_name="supysonic",
        )

        logging.getLogger("supysonic.daemon.server").info("daemon event=start")
        logging.getLogger("supysonic.watcher").info("watcher event=start")
        logging.getLogger("supysonic.scanner").info("scanner event=run_start")

        with open(os.path.join(self._tmp_dir, "daemon.log"), "r", encoding="utf-8") as f:
            daemon_content = f.read()
        with open(os.path.join(self._tmp_dir, "watcher.log"), "r", encoding="utf-8") as f:
            watcher_content = f.read()
        with open(os.path.join(self._tmp_dir, "scanner.log"), "r", encoding="utf-8") as f:
            scanner_content = f.read()
        with open(os.path.join(self._tmp_dir, "supysonic.log"), "r", encoding="utf-8") as f:
            summary_content = f.read()

        self.assertIn("daemon event=start", daemon_content)
        self.assertNotIn("watcher event=start", daemon_content)
        self.assertIn("watcher event=start", watcher_content)
        self.assertIn("scanner event=run_start", scanner_content)
        self.assertIn("daemon event=start", summary_content)
        self.assertIn("watcher event=start", summary_content)
        self.assertIn("scanner event=run_start", summary_content)

    def test_log_dir_debug_level_creates_separate_debug_log(self):
        from supysonic.logging_manager import configure_daemon_logging

        configure_daemon_logging(
            {
                "log_dir": self._tmp_dir,
                "log_rotate": False,
                "log_level": "DEBUG",
                "log_backup_count": 7,
            },
            logger_name="supysonic",
        )

        daemon_logger.debug("debug visible")
        daemon_logger.info("info visible")

        with open(os.path.join(self._tmp_dir, "daemon.log"), "r", encoding="utf-8") as f:
            main_log_content = f.read()
        with open(os.path.join(self._tmp_dir, "daemon.debug.log"), "r", encoding="utf-8") as f:
            debug_log_content = f.read()

        self.assertIn("info visible", main_log_content)
        self.assertNotIn("debug visible", main_log_content)
        self.assertIn("debug visible", debug_log_content)
        self.assertIn("info visible", debug_log_content)

    def test_daemon_run_logs_listening_and_watcher_started(self):
        from supysonic.daemon.server import Daemon
        from supysonic.logging_manager import configure_daemon_logging

        configure_daemon_logging(
            {
                "log_dir": self._tmp_dir,
                "log_rotate": False,
                "log_level": "INFO",
                "log_backup_count": 7,
            },
            logger_name="supysonic",
        )

        config = SimpleNamespace(
            DAEMON={
                "socket": "daemon.sock",
                "run_watcher": True,
                "jukebox_command": None,
                "recommend_daily_refresh": False,
                "mood_scene_playlists_daily_refresh": False,
                "review_task_maintenance": False,
            },
            BASE={},
        )
        fake_listener = SimpleNamespace(address="daemon.sock", close=Mock())
        fake_watcher = Mock()
        fake_thread = Mock(start=Mock())

        daemon = Daemon(config)

        with patch("supysonic.daemon.server.Listener", return_value=fake_listener), patch(
            "supysonic.daemon.server.SupysonicWatcher", return_value=fake_watcher
        ), patch("supysonic.daemon.server.Thread", return_value=fake_thread), patch(
            "supysonic.daemon.server.close_connection"
        ), patch(
            "supysonic.daemon.server.time.sleep", side_effect=lambda _seconds: daemon._Daemon__stopped.set()
        ), patch(
            "supysonic.daemon.server.get_secret_key", return_value=b"secret"
        ):
            daemon.run()

        with open(os.path.join(self._tmp_dir, "daemon.log"), "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("daemon event=listening socket=daemon.sock", content)
        self.assertIn("daemon event=watcher_started", content)

    def test_start_scan_logs_requested_and_started(self):
        from supysonic.daemon.server import Daemon
        from supysonic.logging_manager import configure_daemon_logging

        configure_daemon_logging(
            {
                "log_dir": self._tmp_dir,
                "log_rotate": False,
                "log_level": "INFO",
                "log_backup_count": 7,
            },
            logger_name="supysonic",
        )

        config = SimpleNamespace(
            DAEMON={"run_watcher": False},
            BASE={"scanner_extensions": None, "follow_symlinks": False},
        )
        fake_scanner = Mock()
        fake_scanner.is_alive.return_value = False

        daemon = Daemon(config)

        with patch("supysonic.daemon.server.Scanner", return_value=fake_scanner):
            daemon.start_scan(["music"], force=True)

        with open(os.path.join(self._tmp_dir, "daemon.log"), "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("daemon event=scan_requested folders=1 force=true", content)
        self.assertIn("daemon event=scan_started folders=1 force=true", content)

    def test_start_scan_logs_queued_when_scanner_is_running(self):
        from supysonic.daemon.server import Daemon
        from supysonic.logging_manager import configure_daemon_logging

        configure_daemon_logging(
            {
                "log_dir": self._tmp_dir,
                "log_rotate": False,
                "log_level": "INFO",
                "log_backup_count": 7,
            },
            logger_name="supysonic",
        )

        config = SimpleNamespace(
            DAEMON={"run_watcher": False},
            BASE={"scanner_extensions": None, "follow_symlinks": False},
        )
        running_scanner = Mock()
        running_scanner.is_alive.return_value = True

        daemon = Daemon(config)
        daemon._Daemon__scanner = running_scanner
        daemon.start_scan(["music", "podcasts"], force=False)

        with open(os.path.join(self._tmp_dir, "daemon.log"), "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("daemon event=scan_requested folders=2 force=false", content)
        self.assertIn(
            "daemon event=scan_queued folders=2 force=false reason=scanner_already_running",
            content,
        )

    def test_watcher_add_and_remove_folder_logs_structured_events(self):
        from supysonic.logging_manager import configure_daemon_logging
        from supysonic.watcher import SupysonicWatcher

        configure_daemon_logging(
            {
                "log_dir": self._tmp_dir,
                "log_rotate": False,
                "log_level": "INFO",
                "log_backup_count": 7,
            },
            logger_name="supysonic",
        )

        config = SimpleNamespace(DAEMON={"wait_delay": 5}, BASE={"scanner_extensions": None})
        watcher = SupysonicWatcher(config)
        observer = Mock()
        observer.schedule.return_value = object()
        watcher._SupysonicWatcher__observer = observer
        watcher._SupysonicWatcher__queue = Mock()

        watcher.add_folder("/music")
        watcher.remove_folder("/music")

        with open(os.path.join(self._tmp_dir, "watcher.log"), "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("watcher event=folder_scheduled path=/music", content)
        self.assertIn("watcher event=folder_unscheduled path=/music", content)
