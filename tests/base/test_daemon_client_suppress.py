import unittest

from unittest.mock import patch

from supysonic.daemon.client import (
    DaemonClient,
    SchedulerStatusCommand,
    SuppressNfoPathCommand,
)


class FakeWatcher:
    def __init__(self):
        self.calls = []

    def suppress_nfo_path(self, path, ttl):
        self.calls.append((path, ttl))


class FakeScheduler:
    def list_jobs(self):
        return [{"name": "job", "next_run_at": 123.0}]


class FakeConnection:
    def __init__(self, recvValue=True):
        self.recvValue = recvValue
        self.recvCalled = False
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, excType, exc, tb):
        return False

    def send(self, value):
        self.sent.append(value)

    def recv(self):
        self.recvCalled = True
        return self.recvValue


class FakeDaemon:
    def __init__(self, watcher=None, scheduler=None):
        self.watcher = watcher
        self.scheduler = scheduler


class SuppressNfoPathCommandTestCase(unittest.TestCase):
    def test_command_applies_suppression_and_sends_ack(self):
        connection = FakeConnection()
        watcher = FakeWatcher()

        SuppressNfoPathCommand("/music/Album/album.nfo", 7).apply(connection, FakeDaemon(watcher))

        self.assertEqual(watcher.calls, [("/music/Album/album.nfo", 7)])
        self.assertEqual(connection.sent, [True])

    def test_command_sends_false_when_watcher_is_missing(self):
        connection = FakeConnection()

        SuppressNfoPathCommand("/music/Album/album.nfo", 7).apply(connection, FakeDaemon(None))

        self.assertEqual(connection.sent, [False])

    def test_client_waits_for_ack(self):
        connection = FakeConnection(recvValue=True)

        with patch("supysonic.daemon.client.get_secret_key", return_value=b"key"), patch.object(
            DaemonClient,
            "_DaemonClient__get_connection",
            return_value=connection,
        ):
            result = DaemonClient(address="/tmp/fake-socket").suppress_nfo_path("/music/Album/album.nfo", 7)

        self.assertTrue(result)
        self.assertTrue(connection.recvCalled)
        self.assertEqual(len(connection.sent), 1)
        self.assertIsInstance(connection.sent[0], SuppressNfoPathCommand)

    def test_scheduler_status_command_sends_jobs(self):
        connection = FakeConnection()

        SchedulerStatusCommand().apply(connection, FakeDaemon(scheduler=FakeScheduler()))

        self.assertEqual(len(connection.sent), 1)
        self.assertEqual(connection.sent[0].jobs, [{"name": "job", "next_run_at": 123.0}])

    def test_client_waits_for_scheduler_jobs(self):
        connection = FakeConnection()
        connection.recvValue = type(
            "Result",
            (),
            {"jobs": [{"name": "job", "run_count": 1}]},
        )()

        with patch("supysonic.daemon.client.get_secret_key", return_value=b"key"), patch.object(
            DaemonClient,
            "_DaemonClient__get_connection",
            return_value=connection,
        ):
            result = DaemonClient(address="/tmp/fake-socket").get_scheduler_jobs()

        self.assertEqual(result, [{"name": "job", "run_count": 1}])
        self.assertTrue(connection.recvCalled)
        self.assertEqual(len(connection.sent), 1)
        self.assertIsInstance(connection.sent[0], SchedulerStatusCommand)


if __name__ == "__main__":
    unittest.main()
