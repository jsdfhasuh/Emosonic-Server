import unittest

from types import SimpleNamespace
from unittest.mock import Mock, patch

from supysonic.daemon.server import Daemon


class DaemonRecommendRefreshTestCase(unittest.TestCase):
    def createDaemon(self, recommendation_agent_config=None, **daemon_config):
        recommendation_agent = {
            "enabled": False,
            "api_base_url": "https://llm.example/v1",
            "api_key": "",
            "model": "",
        }
        if recommendation_agent_config:
            recommendation_agent.update(recommendation_agent_config)

        config = SimpleNamespace(
            DAEMON={
                "socket": "daemon.sock",
                "run_watcher": False,
                "jukebox_command": None,
                **daemon_config,
            },
            BASE={},
            RECOMMENDATION_AGENT=recommendation_agent,
        )
        return Daemon(config)

    def test_refresh_recommend_playlists_runs_once_per_day(self):
        daemon = self.createDaemon(recommend_playlist_size=25)

        with patch("supysonic.daemon.server.open_connection", return_value=True), patch(
            "supysonic.daemon.server.close_connection"
        ), patch(
            "supysonic.daemon.server.refreshDailyRecommendPlaylists", return_value=2
        ) as refresh_daily:
            self.assertTrue(
                daemon._Daemon__refresh_recommend_playlists_if_needed(current_day="2026-05-02")
            )
            self.assertFalse(
                daemon._Daemon__refresh_recommend_playlists_if_needed(current_day="2026-05-02")
            )

        refresh_daily.assert_called_once_with(
            num_songs=25,
            day="2026-05-02",
            config=daemon._Daemon__config,
        )

    def test_refresh_recommend_playlists_retries_same_day_after_failure(self):
        daemon = self.createDaemon(recommend_playlist_size=10)

        with patch("supysonic.daemon.server.open_connection", return_value=True), patch(
            "supysonic.daemon.server.close_connection"
        ), patch(
            "supysonic.daemon.server.refreshDailyRecommendPlaylists",
            side_effect=[RuntimeError("boom"), 1],
        ) as refresh_daily:
            self.assertFalse(
                daemon._Daemon__refresh_recommend_playlists_if_needed(current_day="2026-05-02")
            )
            self.assertTrue(
                daemon._Daemon__refresh_recommend_playlists_if_needed(current_day="2026-05-02")
            )

        self.assertEqual(refresh_daily.call_count, 2)

    def test_refresh_recommend_playlists_retries_when_open_connection_fails(self):
        daemon = self.createDaemon(recommend_playlist_size=10)

        with patch(
            "supysonic.daemon.server.open_connection",
            side_effect=[RuntimeError("db down"), True],
        ), patch("supysonic.daemon.server.close_connection"), patch(
            "supysonic.daemon.server.refreshDailyRecommendPlaylists",
            return_value=1,
        ) as refresh_daily:
            self.assertFalse(
                daemon._Daemon__refresh_recommend_playlists_if_needed(current_day="2026-05-02")
            )
            self.assertTrue(
                daemon._Daemon__refresh_recommend_playlists_if_needed(current_day="2026-05-02")
            )

        refresh_daily.assert_called_once_with(
            num_songs=10,
            day="2026-05-02",
            config=daemon._Daemon__config,
        )

    def test_refresh_mood_scene_playlists_runs_once_per_day(self):
        daemon = self.createDaemon(
            mood_scene_playlist_size=12,
            mood_scene_playlist_retention_days=2,
            mood_scene_playlists_active_users_only=False,
        )

        with patch("supysonic.daemon.server.open_connection", return_value=True), patch(
            "supysonic.daemon.server.close_connection"
        ), patch(
            "supysonic.daemon.server.refresh_daily_mood_scene_playlists",
            return_value={"created": 3, "updated": 2, "skipped": 1, "failed": 0},
        ) as refresh_daily, patch(
            "supysonic.daemon.server.cleanup_old_mood_scene_playlists",
            return_value={"deleted": 4, "skipped": 0},
        ) as cleanup_old, self.assertLogs(
            "supysonic.daemon.server",
            level="INFO",
        ) as logs:
            self.assertTrue(
                daemon._Daemon__refresh_mood_scene_playlists_if_needed(
                    current_day="2026-05-02"
                )
            )
            self.assertFalse(
                daemon._Daemon__refresh_mood_scene_playlists_if_needed(
                    current_day="2026-05-02"
                )
            )
            self.assertTrue(
                daemon._Daemon__refresh_mood_scene_playlists_if_needed(
                    current_day="2026-05-03"
                )
            )

        self.assertEqual(refresh_daily.call_count, 2)
        refresh_daily.assert_any_call(
            limit=12,
            day="2026-05-02",
            active_users_only=False,
        )
        refresh_daily.assert_any_call(
            limit=12,
            day="2026-05-03",
            active_users_only=False,
        )
        self.assertEqual(cleanup_old.call_count, 2)
        cleanup_old.assert_any_call(
            retention_days=2,
            current_day="2026-05-02",
        )
        cleanup_old.assert_any_call(
            retention_days=2,
            current_day="2026-05-03",
        )
        completed_logs = "\n".join(logs.output)
        self.assertIn("mood_scene_playlist_refresh_completed", completed_logs)
        self.assertIn("created=3", completed_logs)
        self.assertIn("updated=2", completed_logs)
        self.assertIn("skipped=1", completed_logs)
        self.assertIn("deleted=4", completed_logs)

    def test_refresh_mood_scene_playlists_retries_same_day_after_failure(self):
        daemon = self.createDaemon(mood_scene_playlist_size=8)

        with patch("supysonic.daemon.server.open_connection", return_value=True), patch(
            "supysonic.daemon.server.close_connection"
        ), patch(
            "supysonic.daemon.server.refresh_daily_mood_scene_playlists",
            return_value={"created": 1, "updated": 0, "skipped": 0, "failed": 0},
        ) as refresh_daily, patch(
            "supysonic.daemon.server.cleanup_old_mood_scene_playlists",
            side_effect=[RuntimeError("boom"), {"deleted": 0, "skipped": 0}],
        ) as cleanup_old:
            self.assertFalse(
                daemon._Daemon__refresh_mood_scene_playlists_if_needed(
                    current_day="2026-05-02"
                )
            )
            self.assertTrue(
                daemon._Daemon__refresh_mood_scene_playlists_if_needed(
                    current_day="2026-05-02"
                )
            )

        self.assertEqual(refresh_daily.call_count, 2)
        self.assertEqual(cleanup_old.call_count, 2)

    def test_refresh_mood_scene_playlists_retries_when_summary_has_failures(self):
        daemon = self.createDaemon(mood_scene_playlist_size=8)

        with patch("supysonic.daemon.server.open_connection", return_value=True), patch(
            "supysonic.daemon.server.close_connection"
        ), patch(
            "supysonic.daemon.server.refresh_daily_mood_scene_playlists",
            side_effect=[
                {"created": 1, "updated": 0, "skipped": 0, "failed": 1},
                {"created": 1, "updated": 0, "skipped": 0, "failed": 0},
            ],
        ) as refresh_daily, patch(
            "supysonic.daemon.server.cleanup_old_mood_scene_playlists",
            return_value={"deleted": 0, "skipped": 0},
        ) as cleanup_old, self.assertLogs(
            "supysonic.daemon.server",
            level="WARNING",
        ) as logs:
            self.assertFalse(
                daemon._Daemon__refresh_mood_scene_playlists_if_needed(
                    current_day="2026-05-02"
                )
            )
            self.assertTrue(
                daemon._Daemon__refresh_mood_scene_playlists_if_needed(
                    current_day="2026-05-02"
                )
            )

        self.assertEqual(refresh_daily.call_count, 2)
        cleanup_old.assert_called_once_with(
            retention_days=1,
            current_day="2026-05-02",
        )
        self.assertIn(
            "mood_scene_playlist_refresh_incomplete",
            "\n".join(logs.output),
        )

    def test_configure_scheduler_registers_maintenance_and_recommend_jobs(self):
        daemon = self.createDaemon(
            recommend_daily_refresh=True,
            recommend_refresh_interval=120,
            mood_scene_playlists_daily_refresh=True,
            mood_scene_playlists_refresh_interval=240,
            review_task_maintenance=True,
            review_task_maintenance_interval=900,
            track_metadata_enrichment=False,
            track_metadata_enrichment_interval=180,
        )
        scheduler = Mock()
        daemon._Daemon__scheduler = scheduler

        daemon._Daemon__configure_scheduler()

        first_call = scheduler.register.call_args_list[0]
        second_call = scheduler.register.call_args_list[1]
        third_call = scheduler.register.call_args_list[2]
        fourth_call = scheduler.register.call_args_list[3]

        self.assertEqual(first_call.args[0], "review-task-maintenance")
        self.assertEqual(first_call.args[2], 900)
        self.assertTrue(first_call.kwargs["enabled"])

        self.assertEqual(second_call.args[0], "recommend-refresh")
        self.assertEqual(second_call.args[2], 120)
        self.assertTrue(second_call.kwargs["enabled"])

        self.assertEqual(third_call.args[0], "daily-mood-scene-playlists")
        self.assertEqual(third_call.args[2], 240)
        self.assertTrue(third_call.kwargs["enabled"])

        self.assertEqual(fourth_call.args[0], "track-metadata-enrichment")
        self.assertEqual(fourth_call.args[2], 180)
        self.assertFalse(fourth_call.kwargs["enabled"])

    def test_track_metadata_enrichment_runs_local_provider(self):
        daemon = self.createDaemon(
            track_metadata_enrichment_provider="local",
            track_metadata_enrichment_batch_size=7,
            track_metadata_enrichment_stale_lock_seconds=1200,
        )

        with patch("supysonic.daemon.server.open_connection", return_value=True), patch(
            "supysonic.daemon.server.close_connection"
        ), patch(
            "supysonic.daemon.server.runTrackMetadataEnrichmentPass",
            return_value={"selected": 1, "enriched": 1, "failed": 0, "skipped": 0},
        ) as run_pass:
            self.assertTrue(daemon._Daemon__run_track_metadata_enrichment())

        run_pass.assert_called_once()
        self.assertEqual(run_pass.call_args.kwargs["limit"], 7)
        self.assertEqual(run_pass.call_args.kwargs["stale_lock_seconds"], 1200)
        self.assertEqual(run_pass.call_args.kwargs["provider"].name, "local")

    def test_track_metadata_enrichment_runs_llm_provider(self):
        daemon = self.createDaemon(
            recommendation_agent_config={
                "api_key": "secret",
                "model": "metadata-model",
            },
            track_metadata_enrichment_provider="llm",
            track_metadata_enrichment_batch_size=3,
        )

        with patch("supysonic.daemon.server.open_connection", return_value=True), patch(
            "supysonic.daemon.server.close_connection"
        ), patch(
            "supysonic.daemon.server.runTrackMetadataEnrichmentPass",
            return_value={"selected": 1, "enriched": 1, "failed": 0, "skipped": 0},
        ) as run_pass:
            self.assertTrue(daemon._Daemon__run_track_metadata_enrichment())

        run_pass.assert_called_once()
        self.assertEqual(run_pass.call_args.kwargs["limit"], 3)
        self.assertEqual(run_pass.call_args.kwargs["provider"].name, "llm")

    def test_track_metadata_enrichment_skips_unconfigured_llm_provider(self):
        daemon = self.createDaemon(track_metadata_enrichment_provider="llm")

        with patch("supysonic.daemon.server.runTrackMetadataEnrichmentPass") as run_pass:
            self.assertFalse(daemon._Daemon__run_track_metadata_enrichment())

        run_pass.assert_not_called()

    def test_track_metadata_enrichment_skips_unknown_provider(self):
        daemon = self.createDaemon(track_metadata_enrichment_provider="unknown")

        with patch("supysonic.daemon.server.runTrackMetadataEnrichmentPass") as run_pass:
            self.assertFalse(daemon._Daemon__run_track_metadata_enrichment())

        run_pass.assert_not_called()


if __name__ == "__main__":
    unittest.main()
