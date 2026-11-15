import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class GeventRuntimeTestCase(unittest.TestCase):
    def run_isolated(self, source: str, timeout: int = 30) -> str:
        env = os.environ.copy()
        env["NO_PROXY"] = "127.0.0.1,localhost"
        try:
            result = subprocess.run(
                [sys.executable, "-B", "-c", textwrap.dedent(source)],
                cwd=Path(__file__).resolve().parents[2],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(
                "Isolated runtime timed out:\n"
                + repr(exc.stdout) + "\n" + repr(exc.stderr)
            )
        self.assertEqual(
            result.returncode,
            0,
            "Isolated runtime failed:\n" + result.stdout + result.stderr,
        )
        return result.stdout

    def test_backend_discovery_does_not_import_application_or_patch_runtime(self):
        self.run_isolated(
            """
            import sys
            import supysonic.server as cli
            from gevent import monkey

            backend = cli.get_server("gevent")
            assert backend.__name__ == "GeventServer"
            for name in ("peewee", "supysonic.web", "supysonic.emo", "ssl"):
                assert name not in sys.modules, name
            assert not monkey.is_module_patched("socket")
            assert not monkey.is_module_patched("threading")
            """
        )

    def test_help_does_not_import_application_or_patch_runtime(self):
        self.run_isolated(
            """
            import sys
            from supysonic.server import main
            from gevent import monkey

            main.main(args=["--help"], standalone_mode=False)
            assert "peewee" not in sys.modules
            assert "supysonic.web" not in sys.modules
            assert not monkey.is_module_patched("socket")
            """
        )

    def test_base_runtime_preparation_does_not_patch_other_backends(self):
        self.run_isolated(
            """
            import sys
            from supysonic.server._base import BaseServer
            from gevent import monkey

            BaseServer.prepare_runtime()
            assert not monkey.is_module_patched("socket")
            assert not monkey.is_module_patched("threading")
            assert "peewee" not in sys.modules
            assert "supysonic.web" not in sys.modules
            """
        )

    def test_missing_gevent_websocket_dependency_allows_backend_fallback(self):
        self.run_isolated(
            """
            import importlib.util
            import sys
            import supysonic.server as cli
            from gevent import monkey

            original_spec = importlib.util.find_spec
            importlib.util.find_spec = lambda name, *args: (
                None if name == "geventwebsocket" else original_spec(name, *args)
            )
            try:
                cli.get_server("gevent")
            except ImportError as exc:
                assert "gevent-websocket" in str(exc)
            else:
                raise AssertionError("Missing backend dependency was accepted")

            fallback = object()
            original_get = cli.get_server
            cli.get_server = lambda name: (
                fallback if name == "fallback" else original_get(name)
            )
            cli._servers = ["gevent", "fallback"]
            assert cli.find_first_available_server() is fallback
            assert "ssl" not in sys.modules
            assert "peewee" not in sys.modules
            assert not monkey.is_module_patched("socket")
            """
        )

    def test_cli_prepares_explicit_and_automatic_gevent_before_application(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                self.run_isolated(
                    """
                    import sys
                    import supysonic.server as cli
                    from supysonic.server.gevent import GeventServer
                    from gevent import monkey

                    assert "peewee" not in sys.modules
                    calls = []
                    original_prepare = GeventServer.prepare_runtime

                    def prepare(cls):
                        assert "supysonic.emo.strict_v2_safety" not in sys.modules
                        original_prepare()
                        assert monkey.is_module_patched("socket")
                        assert monkey.is_module_patched("threading")
                        calls.append("prepare")

                    class Config:
                        WEBAPP = {}

                    def load_config():
                        assert calls == ["prepare"]
                        return Config()

                    def run(self):
                        assert calls == ["prepare"]
                        assert "supysonic.emo.strict_v2_safety" in sys.modules
                        assert monkey.is_module_patched("socket")
                        calls.append("run")

                    GeventServer.prepare_runtime = classmethod(prepare)
                    GeventServer.run = run
                    cli.IniConfig.from_common_locations = staticmethod(load_config)
                    cli._servers = ["gevent"]
                    cli.main.main(args=ARGS, standalone_mode=False)
                    assert calls == ["prepare", "run"], calls
                    """.replace("ARGS", repr(["--server", "gevent"] if explicit else []))
                )

    def test_direct_server_run_prepares_before_building_application(self):
        self.run_isolated(
            """
            from supysonic.server.gevent import GeventServer
            from gevent import monkey

            class ProbeServer(GeventServer):
                def _build_kwargs(self):
                    assert monkey.is_module_patched("socket")
                    assert monkey.is_module_patched("threading")
                    return {"prepared": True}

                def _run(self, **kwargs):
                    assert kwargs == {"prepared": True}

            ProbeServer().run()
            """
        )

    def test_runtime_preparation_is_complete_and_idempotent(self):
        self.run_isolated(
            """
            from supysonic.server.gevent import GeventServer
            from gevent import monkey

            GeventServer.prepare_runtime()
            import socket
            import ssl
            import threading
            import peewee

            first_socket = socket.socket
            first_state = peewee.SqliteDatabase(":memory:")._state
            GeventServer.prepare_runtime()
            assert socket.socket is first_socket
            for module in ("socket", "ssl", "threading", "time"):
                assert monkey.is_module_patched(module), module
            assert isinstance(first_state, threading.local)
            assert socket.socket.__module__.startswith("gevent")
            """
        )

    def test_preloaded_native_database_or_ssl_state_is_rejected(self):
        for module in ("peewee", "ssl"):
            with self.subTest(module=module):
                self.run_isolated(
                    """
                    import MODULE
                    from supysonic.server.gevent import GeventServer
                    from gevent import monkey

                    try:
                        GeventServer.prepare_runtime()
                    except RuntimeError as exc:
                        assert "gevent" in str(exc).lower(), str(exc)
                    else:
                        raise AssertionError("Unsafe late preparation was accepted")
                    assert not monkey.is_module_patched("socket")
                    """.replace("MODULE", module)
                )

    def test_database_connections_and_transactions_are_greenlet_local(self):
        self.run_isolated(
            """
            from supysonic.server.gevent import GeventServer
            GeventServer.prepare_runtime()

            import gevent
            from gevent.event import Event
            from peewee import SqliteDatabase

            database = SqliteDatabase(":memory:")
            first_ready = Event()
            second_done = Event()
            connections = []

            def first_request():
                with database.connection_context():
                    connection = database.connection()
                    connections.append(connection)
                    with database.atomic():
                        assert database.transaction_depth() == 1
                        first_ready.set()
                        assert second_done.wait(timeout=3)
                        assert database.connection() is connection
                        assert database.transaction_depth() == 1
                    assert database.transaction_depth() == 0
                assert database.is_closed()

            def second_request():
                assert first_ready.wait(timeout=3)
                assert database.is_closed(), "Connection leaked between requests"
                with database.connection_context():
                    connections.append(database.connection())
                    assert connections[0] is not connections[1]
                    assert database.transaction_depth() == 0
                    with database.atomic():
                        assert database.transaction_depth() == 1
                    assert database.transaction_depth() == 0
                assert database.is_closed()
                second_done.set()

            first = gevent.spawn(first_request)
            second = gevent.spawn(second_request)
            second.get(timeout=5)
            first.get(timeout=5)
            assert database.is_closed()
            assert database.transaction_depth() == 0
            """
        )

    def test_partial_socket_patch_cannot_hide_native_database_state(self):
        self.run_isolated(
            """
            from gevent import monkey
            monkey.patch_socket()
            import peewee
            from supysonic.server.gevent import GeventServer

            try:
                GeventServer.prepare_runtime()
            except RuntimeError as exc:
                assert "gevent" in str(exc).lower(), str(exc)
            else:
                raise AssertionError("Partially patched native state was accepted")
            assert not monkey.is_module_patched("threading")
            """
        )

    def test_reentrant_lock_does_not_treat_other_greenlet_as_owner(self):
        self.run_isolated(
            """
            from supysonic.server.gevent import GeventServer
            GeventServer.prepare_runtime()

            import gevent
            import threading

            lock = threading.RLock()
            with lock:
                with lock:
                    contender = gevent.spawn(lambda: lock.acquire(blocking=False))
                    assert contender.get(timeout=2) is False
            assert lock.acquire(blocking=False)
            lock.release()
            """
        )

    def test_sqlite_write_upgrade_and_handoff_close_races_under_gevent(self):
        self.run_isolated(
            """
            from supysonic.server.gevent import GeventServer
            GeventServer.prepare_runtime()

            import unittest
            from tests.base.test_emo_ws_store import EmoWebSocketStoreTestCase

            names = (
                "test_strict_context_creates_serialize_sqlite_write_upgrade",
                "test_close_and_complete_handoff_are_linearized_by_context_tombstone",
            )
            suite = unittest.TestSuite(
                EmoWebSocketStoreTestCase(name) for name in names
            )
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            assert result.wasSuccessful()
            """
        )

    def test_slow_socket_read_does_not_stall_http_or_socketio(self):
        self.run_isolated(
            """
            from supysonic.server.gevent import GeventServer
            GeventServer.prepare_runtime()

            import http.client
            import socket
            import gevent
            import socketio
            from flask import Flask
            from flask_socketio import SocketIO
            from gevent.event import Event
            from gevent.pywsgi import WSGIServer
            from geventwebsocket.handler import WebSocketHandler

            app = Flask(__name__)
            realtime = SocketIO(app, async_mode="gevent")
            entered_slow_read = Event()
            reader, writer = socket.socketpair()
            reader.settimeout(6)

            @app.get("/slow")
            def slow():
                entered_slow_read.set()
                assert reader.recv(1) == b"x"
                return "slow-complete"

            @app.get("/fast")
            def fast():
                return "fast-complete"

            @realtime.on("diagnostic_ping")
            def ping(payload):
                return {"pong": payload["sequence"]}

            server = WSGIServer(
                ("127.0.0.1", 0), app,
                handler_class=WebSocketHandler, log=None,
            )
            client = socketio.Client(reconnection=False)
            slow_request = None

            def request(path):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=8,
                )
                try:
                    connection.request("GET", path)
                    response = connection.getresponse()
                    assert response.status == 200
                    return response.read()
                finally:
                    connection.close()

            try:
                server.start()
                client.connect(
                    "http://127.0.0.1:" + str(server.server_port),
                    transports=["polling"], wait_timeout=3,
                )
                slow_request = gevent.spawn(request, "/slow")
                assert entered_slow_read.wait(timeout=3)
                assert not slow_request.ready()
                fast_request = gevent.spawn(request, "/fast")
                assert fast_request.get(timeout=3) == b"fast-complete"
                assert client.call(
                    "diagnostic_ping", {"sequence": 7}, timeout=3,
                ) == {"pong": 7}
                assert not slow_request.ready(), "Slow read was not still pending"
                writer.sendall(b"x")
                assert slow_request.get(timeout=3) == b"slow-complete"
            finally:
                writer.close()
                reader.close()
                client.disconnect()
                server.stop(timeout=1)
                if slow_request is not None and not slow_request.ready():
                    slow_request.kill()
            """
        )


if __name__ == "__main__":
    unittest.main()
