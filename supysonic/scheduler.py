import logging
import time
import traceback

from dataclasses import dataclass, field
from threading import Event, Lock, Thread, get_ident
from typing import Callable

from .logging_utils import format_log_event

logger = logging.getLogger(__name__)
MAX_RUN_HISTORY = 20
MAX_RUN_LOGS = 200
MAX_LOG_MESSAGE_LENGTH = 2000
MAX_RESULT_LENGTH = 500


@dataclass
class _ScheduledJob:
    name: str
    func: Callable[[], object]
    interval: int
    run_immediately: bool = True
    enabled: bool = True
    initial_delay: int | None = None
    thread: Thread | None = None
    next_run_at: float | None = None
    running: bool = False
    last_started_at: float | None = None
    last_finished_at: float | None = None
    last_duration: float | None = None
    current_started_monotonic: float | None = None
    last_success: bool | None = None
    last_result: str | None = None
    last_error: str | None = None
    last_error_type: str | None = None
    last_logs: list[dict[str, object]] = field(default_factory=list)
    run_count: int = 0
    failure_count: int = 0
    history: list[dict[str, object]] = field(default_factory=list)


class IntervalScheduler:
    def __init__(self):
        self._jobs: dict[str, _ScheduledJob] = {}
        self._lock = Lock()
        self._stop_event = Event()
        self._started = False

    def register(
        self,
        name: str,
        func: Callable[[], object],
        interval: int,
        *,
        run_immediately: bool = True,
        enabled: bool = True,
        initial_delay: int | None = None,
    ) -> None:
        job = _ScheduledJob(
            name=name,
            func=func,
            interval=max(1, int(interval)),
            run_immediately=run_immediately,
            enabled=enabled,
            initial_delay=None if initial_delay is None else max(0, int(initial_delay)),
        )
        with self._lock:
            self._jobs[name] = job
            should_start = self._started and enabled

        if should_start:
            self._start_job(job)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            jobs = [job for job in self._jobs.values() if job.enabled]

        for job in jobs:
            self._start_job(job)

    def stop(self) -> None:
        self._stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        with self._lock:
            threads = [job.thread for job in self._jobs.values() if job.thread is not None]

        for thread in threads:
            thread.join(timeout=timeout)

    def list_jobs(self) -> list[dict[str, object]]:
        with self._lock:
            current_monotonic = time.monotonic()
            return [
                {
                    "name": job.name,
                    "interval": job.interval,
                    "enabled": job.enabled,
                    "run_immediately": job.run_immediately,
                    "initial_delay": job.initial_delay,
                    "thread_alive": bool(job.thread and job.thread.is_alive()),
                    "running": job.running,
                    "next_run_at": job.next_run_at,
                    "last_started_at": job.last_started_at,
                    "last_finished_at": job.last_finished_at,
                    "last_duration": job.last_duration,
                    "current_duration": _get_current_duration(job, current_monotonic),
                    "last_success": job.last_success,
                    "last_result": job.last_result,
                    "last_error": job.last_error,
                    "last_error_type": job.last_error_type,
                    "last_logs": list(job.last_logs),
                    "run_count": job.run_count,
                    "failure_count": job.failure_count,
                    "history": [_copy_run(run) for run in job.history],
                }
                for job in self._jobs.values()
            ]

    def _start_job(self, job: _ScheduledJob) -> None:
        with self._lock:
            if job.thread is not None and job.thread.is_alive():
                return
            job.thread = Thread(target=self._run_job, args=(job,), daemon=True)
            job.thread.start()

    def _run_job(self, job: _ScheduledJob) -> None:
        delay = 0 if job.run_immediately else job.initial_delay
        if delay is None:
            delay = job.interval

        while True:
            with self._lock:
                job.next_run_at = time.time() + delay

            if self._stop_event.wait(delay):
                with self._lock:
                    job.next_run_at = None
                    job.running = False
                return

            started_at = time.time()
            started_monotonic = time.monotonic()
            with self._lock:
                job.running = True
                job.next_run_at = None
                job.last_started_at = started_at
                job.last_finished_at = None
                job.last_duration = None
                job.current_started_monotonic = started_monotonic
                job.last_success = None
                job.last_result = None
                job.last_error = None
                job.last_error_type = None
                job.last_logs = []

            result = None
            success = False
            error = None
            error_type = None
            log_capture = _RunLogCapture(get_ident())
            root_logger = logging.getLogger()
            root_logger.addHandler(log_capture)
            try:
                result = job.func()
                success = True
            except Exception as exc:
                error = str(exc)
                error_type = exc.__class__.__name__
                logger.exception(
                    format_log_event(
                        "scheduler",
                        "job_failed",
                        job=job.name,
                        error_type=error_type,
                    )
                )
            finally:
                root_logger.removeHandler(log_capture)
                finished_at = time.time()
                duration = time.monotonic() - started_monotonic
                logs = [
                    _build_log_entry(
                        started_at,
                        "INFO",
                        __name__,
                        format_log_event("scheduler", "job_started", job=job.name),
                    )
                ]
                logs.extend(log_capture.records)
                logs.append(
                    _build_log_entry(
                        finished_at,
                        "INFO" if success else "ERROR",
                        __name__,
                        format_log_event(
                            "scheduler",
                            "job_completed" if success else "job_failed",
                            job=job.name,
                            duration=f"{duration:.6f}s",
                            error_type=error_type,
                        ),
                    )
                )
                run = {
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "duration": duration,
                    "success": success,
                    "result": _summarize_value(result, MAX_RESULT_LENGTH) if success else None,
                    "error": error,
                    "error_type": error_type,
                    "logs": logs[-MAX_RUN_LOGS:],
                }
                with self._lock:
                    job.running = False
                    job.current_started_monotonic = None
                    job.last_finished_at = finished_at
                    job.last_duration = duration
                    job.last_success = success
                    job.last_result = run["result"]
                    job.last_error = error
                    job.last_error_type = error_type
                    job.last_logs = list(run["logs"])
                    job.run_count += 1
                    if not success:
                        job.failure_count += 1
                    job.history.append(run)
                    if len(job.history) > MAX_RUN_HISTORY:
                        del job.history[: len(job.history) - MAX_RUN_HISTORY]
            delay = job.interval


class _RunLogCapture(logging.Handler):
    """Capture records emitted by the scheduler job thread for the current run.

    Logs emitted by child threads are not attributed to this run unless those
    threads explicitly log back through the scheduler job thread.
    """

    def __init__(self, thread_id: int):
        super().__init__(level=logging.NOTSET)
        self.thread_id = thread_id
        self.records: list[dict[str, object]] = []

    def emit(self, record):
        if record.thread != self.thread_id:
            return

        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        if record.exc_info:
            message += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()

        self.records.append(
            _build_log_entry(
                record.created,
                record.levelname,
                record.name,
                message,
            )
        )
        if len(self.records) > MAX_RUN_LOGS:
            del self.records[: len(self.records) - MAX_RUN_LOGS]


def _build_log_entry(timestamp: float, level: str, logger_name: str, message: str) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "level": level,
        "logger": logger_name,
        "message": _summarize_value(message, MAX_LOG_MESSAGE_LENGTH),
    }


def _get_current_duration(job: _ScheduledJob, current_monotonic: float) -> float | None:
    if not job.running or job.current_started_monotonic is None:
        return None
    return max(0.0, current_monotonic - job.current_started_monotonic)


def _copy_run(run: dict[str, object]) -> dict[str, object]:
    item = dict(run)
    item["logs"] = [dict(log) for log in run.get("logs", [])]
    return item


def _summarize_value(value: object, max_length: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) > max_length:
        return text[: max_length - 3] + "..."
    return text
