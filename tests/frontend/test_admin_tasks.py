import json
import time
import unittest

from unittest.mock import patch

from supysonic import db
from supysonic.TaskManger import get_task_manager

from .frontendtestbase import FrontendTestBase
from ..testbase import TestConfig


class AdminTasksTestCase(FrontendTestBase):
    def setUp(self):
        TestConfig.WEBAPP = TestConfig.WEBAPP.copy()
        TestConfig.WEBAPP["log_dir"] = ""
        super().setUp()

    def _login_as_admin(self):
        alice = db.User.get(db.User.name == "alice")
        with self.client.session_transaction() as session:
            session["userid"] = str(alice.id)

    def _login_as_normal(self):
        bob = db.User.get(db.User.name == "bob")
        with self.client.session_transaction() as session:
            session["userid"] = str(bob.id)

    def test_admin_can_access_tasks_page(self):
        self._login_as_admin()
        rv = self.client.get("/admin/tasks")
        self.assertEqual(rv.status_code, 200)
        self.assertIn("Background Tasks", rv.data)
        self.assertIn("Auto-refresh every", rv.data)

    def test_admin_tasks_page_shows_empty_state(self):
        self._login_as_admin()
        rv = self.client.get("/admin/tasks")
        self.assertIn("No background tasks recorded", rv.data)

    def test_admin_tasks_page_shows_task_rows(self):
        tm = get_task_manager()
        tm.submit_task("sample-task", lambda: "done")
        # wait briefly for the task to complete
        time.sleep(0.1)

        self._login_as_admin()
        rv = self.client.get("/admin/tasks")
        self.assertIn("sample-task", rv.data)

    def test_admin_can_access_tasks_data_json(self):
        self._login_as_admin()
        rv = self.client.get("/admin/tasks/data")
        self.assertEqual(rv.status_code, 200)
        self.assertTrue("application/json" in rv.mimetype)

        data = rv.json
        self.assertIn("tasks", data)
        self.assertIn("summary", data)
        self.assertIn("total", data["summary"])
        self.assertIn("pending", data["summary"])
        self.assertIn("completed", data["summary"])
        self.assertIn("failed", data["summary"])
        self.assertEqual(data["summary"]["total"], len(data["tasks"]))

    def test_admin_tasks_data_returns_tasks_with_expected_keys(self):
        tm = get_task_manager()
        tm.submit_task("api-task", lambda: "ok")

        self._login_as_admin()
        rv = self.client.get("/admin/tasks/data")

        tasks = rv.json["tasks"]
        self.assertGreaterEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t["task_id"], "api-task")
        self.assertIn(t["status"], ("pending", "completed"))
        self.assertIn("timestamp", t)
        self.assertIn("timestamp_display", t)
        self.assertIn("result", t)
        self.assertIn("error", t)

    def test_admin_tasks_page_shows_daemon_scheduler_jobs(self):
        scheduler_jobs = [
            {
                "name": "recommend-refresh",
                "interval": 300,
                "enabled": True,
                "thread_alive": True,
                "running": False,
                "next_run_at": 1000.0,
                "last_started_at": 900.0,
                "last_finished_at": 901.25,
                "last_duration": 1.25,
                "current_duration": None,
                "last_success": True,
                "last_result": "created=2",
                "last_error": None,
                "last_error_type": None,
                "run_count": 1,
                "failure_count": 0,
                "history": [
                    {
                        "started_at": 900.0,
                        "finished_at": 901.25,
                        "duration": 1.25,
                        "success": True,
                        "result": "created=2",
                        "error": None,
                        "error_type": None,
                        "logs": [
                            {
                                "timestamp": 900.1,
                                "level": "INFO",
                                "logger": "supysonic.scheduler",
                                "message": "scheduler event=job_started job=recommend-refresh",
                            }
                        ],
                    }
                ],
            }
        ]

        self._login_as_admin()
        with patch("supysonic.frontend.DaemonClient") as daemon_client:
            daemon = daemon_client.return_value
            daemon.get_scanning_progress.return_value = None
            daemon.get_scheduler_jobs.return_value = scheduler_jobs
            rv = self.client.get("/admin/tasks")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Daemon scheduled jobs", rv.data)
        self.assertIn("recommend-refresh", rv.data)
        self.assertIn("Recent scheduled runs", rv.data)
        self.assertIn("created=2", rv.data)
        self.assertIn("scheduler event=job_started", rv.data)
        self.assertIn("Logs", rv.data)
        self.assertIn("Run logs follow daemon log level", rv.data)
        self.assertIn("1 / 0", rv.data)

    def test_admin_tasks_data_includes_daemon_scheduler_jobs(self):
        self._login_as_admin()
        with patch("supysonic.frontend.DaemonClient") as daemon_client:
            daemon = daemon_client.return_value
            daemon.get_scanning_progress.return_value = None
            daemon.get_scheduler_jobs.return_value = [
                {
                    "name": "review-task-maintenance",
                    "current_duration": 2.5,
                    "history": [
                        {
                            "started_at": 1.0,
                            "finished_at": 2.0,
                            "duration": 1.0,
                            "success": False,
                            "result": None,
                            "error": "boom",
                            "error_type": "RuntimeError",
                            "logs": [
                                {
                                    "timestamp": 1.1,
                                    "level": "ERROR",
                                    "logger": "supysonic.scheduler",
                                    "message": "failed",
                                }
                            ],
                        }
                    ],
                }
            ]
            rv = self.client.get("/admin/tasks/data")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("scheduler", rv.json)
        self.assertIsNone(rv.json["scheduler"]["error"])
        self.assertEqual(rv.json["scheduler"]["jobs"][0]["name"], "review-task-maintenance")
        self.assertEqual(rv.json["scheduler"]["jobs"][0]["current_duration_display"], "2.500s")
        self.assertEqual(rv.json["scheduler"]["runs"][0]["job_name"], "review-task-maintenance")
        self.assertEqual(rv.json["scheduler"]["runs"][0]["error"], "boom")
        self.assertIn("started_at_display", rv.json["scheduler"]["runs"][0])
        self.assertIn("duration_display", rv.json["scheduler"]["runs"][0])
        self.assertEqual(rv.json["scheduler"]["runs"][0]["logs"][0]["message"], "failed")
        self.assertIn("timestamp_display", rv.json["scheduler"]["runs"][0]["logs"][0])
        self.assertIn("daemon log level", rv.json["scheduler"]["log_note"])

    def test_admin_tasks_data_survives_daemon_ipc_failure(self):
        self._login_as_admin()
        with patch("supysonic.frontend.DaemonClient") as daemon_client:
            daemon = daemon_client.return_value
            daemon.get_scanning_progress.return_value = None
            daemon.get_scheduler_jobs.side_effect = EOFError("closed")
            rv = self.client.get("/admin/tasks/data")

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.json["scheduler"]["jobs"], [])
        self.assertEqual(rv.json["scheduler"]["runs"], [])
        self.assertEqual(rv.json["scheduler"]["error"], "closed")

    def test_admin_tasks_data_survives_malformed_scheduler_payload(self):
        self._login_as_admin()
        with patch("supysonic.frontend.DaemonClient") as daemon_client:
            daemon = daemon_client.return_value
            daemon.get_scanning_progress.return_value = None
            daemon.get_scheduler_jobs.return_value = "not-a-job-list"
            rv = self.client.get("/admin/tasks/data")

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.json["scheduler"]["jobs"], [])
        self.assertEqual(rv.json["scheduler"]["runs"], [])
        self.assertEqual(rv.json["scheduler"]["error"], "Invalid scheduler jobs payload")

    def test_admin_tasks_data_tolerates_missing_job_name_and_extreme_timestamp(self):
        self._login_as_admin()
        with patch("supysonic.frontend.DaemonClient") as daemon_client:
            daemon = daemon_client.return_value
            daemon.get_scanning_progress.return_value = None
            daemon.get_scheduler_jobs.return_value = [
                {
                    "next_run_at": 10**400,
                    "history": [
                        {
                            "started_at": {"bad": "timestamp"},
                            "finished_at": 10**400,
                            "duration": -1,
                            "success": True,
                        }
                    ],
                }
            ]
            rv = self.client.get("/admin/tasks/data")

        self.assertEqual(rv.status_code, 200)
        self.assertIsNone(rv.json["scheduler"]["error"])
        self.assertEqual(rv.json["scheduler"]["jobs"][0]["name"], "unknown")
        self.assertEqual(rv.json["scheduler"]["jobs"][0]["next_run_at_display"], "—")
        self.assertEqual(rv.json["scheduler"]["runs"][0]["job_name"], "unknown")
        self.assertEqual(rv.json["scheduler"]["runs"][0]["started_at_display"], "—")
        self.assertEqual(rv.json["scheduler"]["runs"][0]["finished_at_display"], "—")
        self.assertEqual(rv.json["scheduler"]["runs"][0]["duration_display"], "0ms")

    def test_non_admin_redirected_from_tasks_page(self):
        self._login_as_normal()
        rv = self.client.get("/admin/tasks", follow_redirects=False)
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/", rv.location)

    def test_non_admin_redirected_from_tasks_data(self):
        self._login_as_normal()
        rv = self.client.get("/admin/tasks/data", follow_redirects=False)
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/", rv.location)

    def test_tasks_page_summary_cards_present(self):
        self._login_as_admin()
        rv = self.client.get("/admin/tasks")
        self.assertIn("summary-pending", rv.data)
        self.assertIn("summary-completed", rv.data)
        self.assertIn("summary-failed", rv.data)
        self.assertIn("summary-total", rv.data)

    def test_tasks_page_contains_polling_script(self):
        self._login_as_admin()
        rv = self.client.get("/admin/tasks")
        self.assertIn("admin/tasks/data", rv.data)
        self.assertIn("setInterval", rv.data)
        self.assertIn("fetch(dataUrl)", rv.data)
        self.assertIn('"refresh-now"', rv.data)


if __name__ == "__main__":
    unittest.main()
