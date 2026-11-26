import unittest

from types import SimpleNamespace
from unittest.mock import Mock, patch

from supysonic.daemon.server import Daemon


class DaemonOpenAIProfileTestCase(unittest.TestCase):
    def daemon(self):
        return Daemon(SimpleNamespace(
            DAEMON={"socket": "test.sock", "run_watcher": False, "jukebox_command": None,
                    "recommend_daily_refresh": False},
            BASE={}, RECOMMENDATION_AGENT={"enabled": True, "provider": "openai",
                                           "profile_enabled": True},
        ))

    def test_separate_job_runs_even_when_daily_playlist_refresh_is_disabled(self):
        daemon = self.daemon()
        scheduler = Mock()
        daemon._Daemon__scheduler = scheduler
        daemon._Daemon__configure_scheduler()
        calls = {call.args[0]: call for call in scheduler.register.call_args_list}
        self.assertTrue(calls["openai-profile-refresh"].kwargs["enabled"])
        self.assertFalse(calls["recommend-refresh"].kwargs["enabled"])
        self.assertFalse(calls["daily-profile-avatar"].kwargs["enabled"])

    def test_daily_avatar_runs_for_the_configured_account(self):
        daemon = self.daemon()
        daemon._Daemon__config.RECOMMENDATION_AGENT["daily_avatar_username"] = "bob"
        scheduler = Mock()
        daemon._Daemon__scheduler = scheduler
        daemon._Daemon__configure_scheduler()
        job = next(c for c in scheduler.register.call_args_list if c.args[0] == "daily-profile-avatar")
        self.assertTrue(job.kwargs["enabled"])
        self.assertEqual(job.args[2], 300)
        with patch("supysonic.daemon.server.open_connection", return_value=True), patch(
            "supysonic.daemon.server.close_connection",
        ) as close, patch("supysonic.daemon.server.rotate_daily_avatar", return_value="applied"):
            self.assertEqual(job.args[1](), "applied")
            close.assert_called_once()

    def test_profile_job_has_no_daily_guard(self):
        daemon = self.daemon()
        with patch("supysonic.daemon.server.open_connection", return_value=True), patch(
            "supysonic.daemon.server.close_connection",
        ) as close, patch("supysonic.daemon.server.refresh_openai_profiles", return_value={
            "selected": 1, "updated": 1, "failed": 0, "skipped": 0,
        }) as refresh:
            self.assertTrue(daemon._Daemon__refresh_openai_profiles())
            self.assertTrue(daemon._Daemon__refresh_openai_profiles())
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(close.call_count, 2)

    def test_failure_is_logged_without_exception_text(self):
        daemon = self.daemon()
        with patch("supysonic.daemon.server.open_connection", return_value=True), patch(
            "supysonic.daemon.server.close_connection",
        ), patch("supysonic.daemon.server.refresh_openai_profiles",
                 side_effect=RuntimeError("private details")), self.assertLogs(
                     "supysonic.daemon.server", level="WARNING",
                 ) as logs:
            self.assertFalse(daemon._Daemon__refresh_openai_profiles())
        self.assertNotIn("private details", str(logs.output))
        self.assertIn("error_type=RuntimeError", str(logs.output))
