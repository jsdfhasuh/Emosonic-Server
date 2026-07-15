import multiprocessing
import os
import shutil
import tempfile
import threading
import time
import unittest
from multiprocessing.queues import Queue
from types import SimpleNamespace
from unittest import mock

import socketio as socketio_client
from click.testing import CliRunner
from flask import Flask
from werkzeug.serving import make_server

from supysonic.db import release_database
from supysonic.emo.strict_v2_safety import (
    StrictV2Safety,
    resolve_allowed_origins,
    validate_strict_v2_worker_count,
)
from supysonic.server import main as server_main
from supysonic.web import create_application

from tests.testbase import TestConfig


def _exercise_oversized_transport_payload(result_queue: Queue) -> None:
    database_fd, database_path = tempfile.mkstemp()
    cache_dir = tempfile.mkdtemp()
    http_server = None
    server_thread = None
    client = None
    try:
        from supysonic.emo.ws import EmoNamespace, socketio

        socketio.server_options["async_mode"] = "threading"
        config = TestConfig(False, False)
        config.BASE["database_uri"] = "sqlite:///" + database_path
        config.WEBAPP["cache_dir"] = cache_dir
        config.WEBAPP["mount_emosonic"] = True
        application = create_application(config)

        http_server = make_server(
            "127.0.0.1",
            0,
            application,
            threaded=True,
        )
        server_thread = threading.Thread(
            target=http_server.serve_forever,
            daemon=True,
        )
        server_thread.start()

        client = socketio_client.Client(
            reconnection=False,
            request_timeout=2,
        )
        client.connect(
            f"http://127.0.0.1:{http_server.server_port}",
            namespaces=["/emo"],
            transports=["polling"],
            socketio_path="emo/ws",
            wait_timeout=2,
        )
        with mock.patch.object(EmoNamespace, "on_message") as on_message:
            client.emit(
                "message",
                {"oversized": "x" * (257 * 1024)},
                namespace="/emo",
            )
            deadline = time.monotonic() + 3
            while client.connected and time.monotonic() < deadline:
                time.sleep(0.05)

            result_queue.put(
                {
                    "configured_limit": (
                        socketio.server.eio.max_http_buffer_size
                    ),
                    "connected_after": client.connected,
                    "handler_called": on_message.called,
                }
            )
    except Exception as exc:  # pragma: nocover - reported to the parent
        result_queue.put(
            {"exception": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        if client is not None and client.connected:
            client.disconnect()
        if http_server is not None:
            http_server.shutdown()
        if server_thread is not None:
            server_thread.join(2)
        release_database()
        shutil.rmtree(cache_dir)
        os.close(database_fd)
        os.remove(database_path)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class StrictV2SafetyTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.safety = StrictV2Safety(self.clock)
        self.safety.configure({})

    def test_default_request_rate_limit_boundary_and_retry(self):
        for _ in range(120):
            self.assertIsNone(
                self.safety.check_rate_limit("nonce-request", "system.ping")
            )

        retry_after_ms = self.safety.check_rate_limit(
            "nonce-request",
            "system.ping",
        )
        self.assertIsInstance(retry_after_ms, int)
        self.assertGreater(retry_after_ms, 0)

        self.clock.advance(60)
        self.assertIsNone(
            self.safety.check_rate_limit("nonce-request", "system.ping")
        )

    def test_default_control_rate_limit_boundary(self):
        for _ in range(20):
            self.assertIsNone(
                self.safety.check_rate_limit("nonce-control", "player.seek")
            )
        self.assertEqual(
            self.safety.check_rate_limit("nonce-control", "player.seek"),
            1000,
        )

    def test_default_start_rate_limit_boundaries_are_independent(self):
        for nonce, action in (
            ("nonce-create", "playback.context.create"),
            ("nonce-handoff", "playback.handoff.start"),
            ("nonce-broadcast", "broadcast.start"),
        ):
            with self.subTest(action=action):
                for _ in range(10):
                    self.assertIsNone(self.safety.check_rate_limit(nonce, action))
                retry_after_ms = self.safety.check_rate_limit(nonce, action)
                self.assertIsInstance(retry_after_ms, int)
                self.assertGreater(retry_after_ms, 0)

    def test_raised_limits_require_load_test_evidence(self):
        self.safety.configure({"emo_strict_requests_per_connection_per_minute": 121})
        self.assertEqual(
            self.safety.limit("requests_per_connection_per_minute"),
            120,
        )

        self.safety.configure(
            {
                "emo_strict_requests_per_connection_per_minute": 121,
                "emo_strict_rate_limit_load_test_evidence": "load-test-20260712",
            }
        )
        self.assertEqual(
            self.safety.limit("requests_per_connection_per_minute"),
            121,
        )

    def test_pending_emit_limit_rejects_before_reservation(self):
        self.safety.configure(
            {"emo_socketio_max_pending_emits_per_connection": 1}
        )

        self.assertTrue(self.safety.reserve_emit("sid-1"))
        self.assertFalse(self.safety.reserve_emit("sid-1"))
        self.safety.release_emit("sid-1")
        self.assertTrue(self.safety.reserve_emit("sid-1"))
        self.safety.release_emit("sid-1")

    def test_origin_policy_rejects_production_wildcard(self):
        self.assertIsNone(resolve_allowed_origins({}))
        self.assertEqual(
            resolve_allowed_origins(
                {"emo_allowed_origins": "https://one.example, https://two.example"}
            ),
            ["https://one.example", "https://two.example"],
        )
        with self.assertRaises(ValueError):
            resolve_allowed_origins({"emo_allowed_origins": "*"})

    def test_origin_policy_allows_explicit_development_wildcard_with_warning(self):
        with self.assertLogs(
            "supysonic.emo.strict_v2_safety",
            level="WARNING",
        ):
            origins = resolve_allowed_origins(
                {"emo_allowed_origins": "*"},
                development=True,
            )
        self.assertEqual(origins, "*")

    def test_actual_polling_transport_rejects_257_kib_payload(self):
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        process = context.Process(
            target=_exercise_oversized_transport_payload,
            args=(result_queue,),
        )
        process.start()
        process.join(12)
        if process.is_alive():
            process.terminate()
            process.join(2)
            self.fail("Oversized transport test subprocess did not finish")

        self.assertEqual(process.exitcode, 0)
        result = result_queue.get(timeout=2)
        self.assertNotIn("exception", result)
        self.assertEqual(result["configured_limit"], 256 * 1024)
        self.assertFalse(result["connected_after"])
        self.assertFalse(result["handler_called"])

    def test_multi_process_fails_only_when_strict_core_is_effectively_ready(self):
        enabled = {"emo_strict_v2_core_enabled": True}
        ready = {
            "core": True,
            "follow": False,
            "handoff": False,
            "broadcast": False,
        }
        with self.assertRaises(RuntimeError):
            validate_strict_v2_worker_count(2, enabled, ready)
        validate_strict_v2_worker_count(1, enabled, ready)
        validate_strict_v2_worker_count(2, enabled, dict(ready, core=False))
        validate_strict_v2_worker_count(
            2,
            {"emo_strict_v2_core_enabled": False},
            ready,
        )

    def test_multi_process_check_honors_development_local_evidence_gate(self):
        config = {
            "emo_strict_v2_core_enabled": True,
            "emo_development_mode": False,
            "emo_strict_v2_allow_local_test_evidence": True,
        }
        with mock.patch(
            "supysonic.emo.strict_v2_readiness.get_code_conformance_readiness",
            side_effect=lambda allow_local_test_evidence=False: {
                "core": bool(allow_local_test_evidence),
                "follow": False,
                "handoff": False,
                "broadcast": False,
            },
        ):
            validate_strict_v2_worker_count(2, config)

            config["emo_development_mode"] = True
            with self.assertRaises(RuntimeError):
                validate_strict_v2_worker_count(2, config)

    def test_server_cli_fails_before_starting_multiple_ready_workers(self):
        readiness = {
            "core": True,
            "follow": False,
            "handoff": False,
            "broadcast": False,
        }
        configured_server = mock.Mock()
        with mock.patch(
            "supysonic.server.get_server",
            return_value=configured_server,
        ), mock.patch(
            "supysonic.server.IniConfig.from_common_locations",
            return_value=SimpleNamespace(
                WEBAPP={"emo_strict_v2_core_enabled": True}
            ),
        ), mock.patch(
            "supysonic.emo.strict_v2_readiness.get_code_conformance_readiness",
            return_value=readiness,
        ):
            result = CliRunner().invoke(
                server_main,
                ["--server", "gunicorn", "--processes", "2"],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("requires exactly one server process", result.output)
        configured_server.assert_not_called()

    def test_gunicorn_worker_interrupt_drains_mounted_emo_app(self):
        try:
            from supysonic.server.gunicorn import (
                GunicornApp,
                _drain_emo_realtime,
            )
        except ImportError as exc:  # pragma: nocover - dependency is in requirements
            self.skipTest(str(exc))

        gunicorn_app = GunicornApp(
            socket=None,
            host="127.0.0.1",
            port=5722,
            processes=1,
            threads=2,
        )
        self.assertIs(gunicorn_app.cfg.worker_int, _drain_emo_realtime)

        application = Flask(__name__)
        application.config["WEBAPP"] = {
            "mount_emosonic": True,
            "emo_strict_shutdown_grace_seconds": 7,
        }
        with mock.patch(
            "supysonic.emo.ws.begin_strict_v2_shutdown"
        ) as begin_shutdown:
            _drain_emo_realtime(
                SimpleNamespace(wsgi=application),
            )

        begin_shutdown.assert_called_once_with(7)

    def test_graceful_shutdown_waits_for_in_flight_request(self):
        safety = StrictV2Safety()
        safety.configure({})
        self.assertTrue(safety.begin_request())
        result = []

        def shutdown():
            result.append(safety.begin_shutdown(1))

        thread = threading.Thread(target=shutdown)
        thread.start()
        time.sleep(0.01)
        self.assertFalse(safety.accepts_connections())
        self.assertFalse(safety.begin_request())
        safety.finish_request()
        thread.join(1)
        self.assertEqual(result, [True])

    def test_graceful_shutdown_timeout_reports_incomplete_drain(self):
        safety = StrictV2Safety()
        safety.configure({})
        self.assertTrue(safety.begin_request())

        self.assertFalse(safety.begin_shutdown(0))
        self.assertFalse(safety.accepts_connections())

        safety.finish_request()


if __name__ == "__main__":
    unittest.main()
