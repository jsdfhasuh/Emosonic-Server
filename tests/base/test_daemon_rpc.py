import os
import socket
import threading
import unittest
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import gevent
from gevent import socket as gevent_socket

from supysonic.daemon.client import DaemonClient, ScannerProgressCommand, ScannerProgressResult
from supysonic.daemon.exceptions import DaemonUnavailableError
from supysonic.daemon.server import Daemon


class DaemonRpcTestCase(unittest.TestCase):
    def test_listener_serves_next_command_after_client_failure(self):
        daemon = Daemon(SimpleNamespace())
        rejected = MagicMock()
        rejected.__enter__.return_value = rejected
        interrupted = MagicMock()
        interrupted.__enter__.return_value = interrupted
        interrupted.recv.side_effect = EOFError
        healthy = MagicMock()
        healthy.__enter__.return_value = healthy
        healthy.recv.return_value = ScannerProgressCommand()
        healthy.send.side_effect = lambda result: daemon._Daemon__stopped.set()
        listener = Mock()
        listener.accept.side_effect = [ConnectionResetError(), rejected, interrupted, healthy]
        daemon._Daemon__listener = listener

        with self.assertLogs("supysonic.daemon.server", level="WARNING"), patch(
            "supysonic.daemon.server.deliver_challenge", side_effect=[AuthenticationError(), None, None],
        ), patch("supysonic.daemon.server.answer_challenge"):
            daemon._Daemon__listen()

        rejected.recv.assert_not_called()
        rejected.__exit__.assert_called_once()
        self.assertIsInstance(healthy.send.call_args[0][0], ScannerProgressResult)
        interrupted.__exit__.assert_called_once()
        healthy.__exit__.assert_called_once()
        listener.close.assert_called_once()

    def test_daemon_keeps_compatibility_with_standard_authenticated_client(self):
        listener = Listener(("127.0.0.1", 0))
        daemon = Daemon(SimpleNamespace())
        daemon._Daemon__listener = listener
        daemon._Daemon__authkey = b"test-key"
        daemon._Daemon__scanner = SimpleNamespace(is_alive=lambda: True, scanned=12)
        worker = threading.Thread(target=daemon._Daemon__listen, daemon=True)
        worker.start()
        try:
            with Client(listener.address, authkey=b"test-key") as conn:
                conn.send(ScannerProgressCommand())
                self.assertTrue(conn.poll(1))
                self.assertEqual(conn.recv().scanned, 12)
        finally:
            with Client(listener.address, authkey=b"test-key") as conn:
                daemon._Daemon__stopped.set()
                conn.send(None)
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    def test_stalled_handshake_closes_socket_and_leaves_gevent_responsive(self):
        # An isolated socket pair with no challenge models a stalled read. No
        # listener or external server receives a partial authentication attempt.
        left, right = gevent_socket.socketpair()
        self.addCleanup(right.close)
        self.addCleanup(left.close)
        connected = Mock(wraps=left)
        connected.connect.side_effect = lambda address: None
        socket_api = SimpleNamespace(
            socket=Mock(return_value=connected), AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM,
        )
        with Listener(("127.0.0.1", 0), authkey=b"test-key") as listener, patch(
            "supysonic.daemon.client.get_secret_key", return_value=b"test-key",
        ):
            client = DaemonClient(listener.address)
            tick = gevent.spawn_later(0.005, lambda: True)
            with patch("supysonic.daemon.client.socket", socket_api):
                with self.assertRaises(DaemonUnavailableError) as failed:
                    client.get_scanning_progress(timeout=0.08)
            # socket.timeout is distinct from TimeoutError on Python 3.9.
            self.assertIsInstance(failed.exception.__cause__, (TimeoutError, socket.timeout))
            self.assertTrue(tick.ready())
            self.assertEqual(left.fileno(), -1)
            self.assertEqual(right.recv(1), b"")
            self._check_healthy_query(client, listener)

    def _check_healthy_query(self, client, listener):
        def respond():
            with listener.accept() as conn:
                if isinstance(conn.recv(), ScannerProgressCommand):
                    conn.send(ScannerProgressResult(41))

        worker = threading.Thread(target=respond, daemon=True)
        worker.start()
        self.assertEqual(client.get_scanning_progress(), 41)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    @unittest.skipUnless(os.name == "nt", "Windows named-pipe transport")
    def test_windows_named_pipe_keeps_standard_authentication(self):
        with Listener(family="AF_PIPE", authkey=b"test-key") as listener, patch(
            "supysonic.daemon.client.get_secret_key", return_value=b"test-key",
        ):
            self._check_healthy_query(DaemonClient(listener.address), listener)
