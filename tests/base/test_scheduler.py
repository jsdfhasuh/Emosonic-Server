import logging
import time
import unittest

from threading import Event
from unittest.mock import patch

from supysonic.scheduler import IntervalScheduler


class IntervalSchedulerTestCase(unittest.TestCase):
    def waitForCondition(self, predicate, timeout=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_runs_job_immediately_and_repeatedly(self):
        scheduler = IntervalScheduler()
        calls = []
        done = Event()

        def runJob():
            calls.append(time.monotonic())
            if len(calls) >= 2:
                done.set()

        scheduler.register("job", runJob, 1, initial_delay=5)
        scheduler.start()

        try:
            self.assertTrue(done.wait(2))
            self.assertEqual(len(calls), 2)
        finally:
            scheduler.stop()
            scheduler.join(timeout=1)

    def test_list_jobs_reports_run_history_and_next_run(self):
        scheduler = IntervalScheduler()

        job_logger = logging.getLogger("supysonic.tests.scheduler_job")

        def runJob():
            job_logger.warning("job log %s", "captured")
            return "ok"

        scheduler.register("job", runJob, 60)
        scheduler.start()

        try:
            self.assertTrue(
                self.waitForCondition(
                    lambda: scheduler.list_jobs()[0]["run_count"] == 1
                    and scheduler.list_jobs()[0]["next_run_at"] is not None
                )
            )
            job = scheduler.list_jobs()[0]
            self.assertFalse(job["running"])
            self.assertTrue(job["thread_alive"])
            self.assertTrue(job["last_success"])
            self.assertEqual(job["last_result"], "ok")
            self.assertIsNone(job["last_error"])
            self.assertEqual(job["run_count"], 1)
            self.assertEqual(job["failure_count"], 0)
            self.assertEqual(len(job["history"]), 1)
            self.assertEqual(job["history"][0]["result"], "ok")
            self.assertGreaterEqual(job["history"][0]["duration"], 0)
            logs = job["history"][0]["logs"]
            self.assertGreaterEqual(len(logs), 3)
            self.assertTrue(
                any(log["message"] == "job log captured" for log in logs)
            )
        finally:
            scheduler.stop()
            scheduler.join(timeout=1)

    def test_run_logs_include_tracebacks(self):
        scheduler = IntervalScheduler()
        job_logger = logging.getLogger("supysonic.tests.scheduler_job")

        def runJob():
            try:
                raise RuntimeError("inner boom")
            except RuntimeError:
                job_logger.exception("handled job failure")
            return "ok"

        scheduler.register("job", runJob, 60)
        scheduler.start()

        try:
            self.assertTrue(
                self.waitForCondition(lambda: scheduler.list_jobs()[0]["run_count"] == 1)
            )
            logs = scheduler.list_jobs()[0]["history"][0]["logs"]
            self.assertTrue(
                any(
                    "Traceback" in log["message"]
                    and "RuntimeError: inner boom" in log["message"]
                    for log in logs
                )
            )
        finally:
            scheduler.stop()
            scheduler.join(timeout=1)

    def test_duration_uses_monotonic_clock(self):
        scheduler = IntervalScheduler()
        fake_times = iter([1000.0, 900.0, 800.0, 700.0])

        scheduler.register("job", lambda: "ok", 60)
        with patch("supysonic.scheduler.time.time", side_effect=lambda: next(fake_times, 700.0)):
            scheduler.start()

            try:
                self.assertTrue(
                    self.waitForCondition(lambda: scheduler.list_jobs()[0]["run_count"] == 1)
                )
                job = scheduler.list_jobs()[0]
                self.assertLess(job["last_finished_at"], job["last_started_at"])
                self.assertGreaterEqual(job["last_duration"], 0)
                self.assertGreaterEqual(job["history"][0]["duration"], 0)
            finally:
                scheduler.stop()
                scheduler.join(timeout=1)

    def test_list_jobs_reports_failed_run_history(self):
        scheduler = IntervalScheduler()

        def runJob():
            raise ValueError("bad job")

        scheduler.register("job", runJob, 60)
        with patch("supysonic.scheduler.logger.exception") as log_exception:
            scheduler.start()

            try:
                self.assertTrue(
                    self.waitForCondition(lambda: scheduler.list_jobs()[0]["run_count"] == 1)
                )
                job = scheduler.list_jobs()[0]
                self.assertFalse(job["last_success"])
                self.assertEqual(job["last_error"], "bad job")
                self.assertEqual(job["last_error_type"], "ValueError")
                self.assertEqual(job["run_count"], 1)
                self.assertEqual(job["failure_count"], 1)
                self.assertEqual(job["history"][0]["error"], "bad job")
                log_exception.assert_called_once()
            finally:
                scheduler.stop()
                scheduler.join(timeout=1)

    def test_disabled_job_does_not_run(self):
        scheduler = IntervalScheduler()
        ran = Event()

        scheduler.register("job", lambda: ran.set(), 1, enabled=False)
        scheduler.start()

        try:
            self.assertFalse(ran.wait(0.2))
        finally:
            scheduler.stop()
            scheduler.join(timeout=1)

    def test_list_jobs_reports_running_state(self):
        scheduler = IntervalScheduler()
        started = Event()
        release = Event()

        def runJob():
            started.set()
            release.wait(1)

        scheduler.register("job", runJob, 60)
        scheduler.start()

        try:
            self.assertTrue(started.wait(1))
            jobs = scheduler.list_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["name"], "job")
            self.assertTrue(jobs[0]["running"])
            self.assertIsNotNone(jobs[0]["current_duration"])
            self.assertGreaterEqual(jobs[0]["current_duration"], 0)
            self.assertTrue(jobs[0]["run_immediately"])
            self.assertEqual(jobs[0]["interval"], 60)
        finally:
            release.set()
            scheduler.stop()
            scheduler.join(timeout=1)
