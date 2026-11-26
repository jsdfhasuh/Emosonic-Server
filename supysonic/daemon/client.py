# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2019 Alban 'spl0k' Féron
#
# Distributed under terms of the GNU AGPLv3 license.

import socket
import sys
import time
from contextlib import contextmanager
from multiprocessing import AuthenticationError
from multiprocessing.connection import (
    Client, Connection, address_type, answer_challenge, deliver_challenge,
)

from .exceptions import DaemonUnavailableError
from ..config import get_current_config
from ..utils import get_secret_key

__all__ = ["DaemonClient"]


class DaemonCommand:
    def apply(self, connection, daemon):
        raise NotImplementedError()


class WatcherCommand(DaemonCommand):
    def __init__(self, folder):
        self._folder = folder


class AddWatchedFolderCommand(WatcherCommand):
    def apply(self, connection, daemon):
        if daemon.watcher is not None:
            daemon.watcher.add_folder(self._folder)


class RemoveWatchedFolder(WatcherCommand):
    def apply(self, connection, daemon):
        if daemon.watcher is not None:
            daemon.watcher.remove_folder(self._folder)


class SuppressNfoPathCommand(DaemonCommand):
    def __init__(self, path, ttl):
        self.__path = path
        self.__ttl = ttl

    def apply(self, connection, daemon):
        if daemon.watcher is not None:
            daemon.watcher.suppress_nfo_path(self.__path, self.__ttl)
            connection.send(True)
            return
        connection.send(False)


class ScannerCommand(DaemonCommand):
    pass


class ScannerProgressCommand(ScannerCommand):
    def apply(self, connection, daemon):
        scanner = daemon.scanner
        rv = scanner.scanned if scanner is not None and scanner.is_alive() else None
        connection.send(ScannerProgressResult(rv))


class ScannerStartCommand(ScannerCommand):
    def __init__(self, folders=[], force=False):
        self.__folders = folders
        self.__force = force

    def apply(self, connection, daemon):
        daemon.start_scan(self.__folders, self.__force)


class SchedulerStatusCommand(DaemonCommand):
    def apply(self, connection, daemon):
        connection.send(SchedulerStatusResult(daemon.scheduler.list_jobs()))


class JukeboxCommand(DaemonCommand):
    def __init__(self, action, args):
        self.__action = action
        self.__args = args

    def apply(self, connection, daemon):
        if daemon.jukebox is None:
            connection.send(JukeboxResult(None))
            return

        playlist = None
        if self.__action == "get":
            playlist = daemon.jukebox.playlist
        elif self.__action == "status":
            pass
        else:
            func = None

            if self.__action == "set":
                func = daemon.jukebox.set
            elif self.__action == "start":
                func = daemon.jukebox.start
            elif self.__action == "stop":
                func = daemon.jukebox.stop
            elif self.__action == "skip":
                func = daemon.jukebox.skip
            elif self.__action == "add":
                func = daemon.jukebox.add
            elif self.__action == "clear":
                func = daemon.jukebox.clear
            elif self.__action == "remove":
                func = daemon.jukebox.remove
            elif self.__action == "shuffle":
                func = daemon.jukebox.shuffle
            elif self.__action == "setGain":
                func = daemon.jukebox.setgain

            func(*self.__args)

        rv = JukeboxResult(daemon.jukebox)
        rv.playlist = playlist
        connection.send(rv)


class DaemonCommandResult:
    pass


class ScannerProgressResult(DaemonCommandResult):
    def __init__(self, scanned):
        self.__scanned = scanned

    scanned = property(lambda self: self.__scanned)


class JukeboxResult(DaemonCommandResult):
    def __init__(self, jukebox):
        if jukebox is None:
            self.playing = False
            self.index = -1
            self.gain = 1.0
            self.position = 0
        else:
            self.playing = jukebox.playing
            self.index = jukebox.index
            self.gain = jukebox.gain
            self.position = jukebox.position
        self.playlist = ()


class SchedulerStatusResult(DaemonCommandResult):
    def __init__(self, jobs):
        self.jobs = jobs


def _remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("Daemon scan status timed out")
    return seconds


class _ScanSocketConnection(Connection):
    """Keep multiprocessing framing, but use deadline-aware socket I/O."""

    def __init__(self, sock, deadline: float):
        self._socket = sock
        self._deadline = deadline
        super().__init__(sock.fileno())

    def _close(self):
        self._socket.close()

    def _send(self, buf):
        self._socket.settimeout(_remaining(self._deadline))
        self._socket.sendall(buf)

    def _read_socket(self, _handle, size):
        self._socket.settimeout(_remaining(self._deadline))
        return self._socket.recv(size)

    def _recv(self, size):
        return super()._recv(size, read=self._read_socket)


if sys.platform == "win32":
    import _winapi
    from multiprocessing.connection import PipeConnection

    class _ScanPipeConnection(PipeConnection):
        """Named pipes deliver whole messages. Wait up to the same RPC deadline."""

        def __init__(self, address, deadline: float):
            self._deadline = deadline
            while True:
                try:
                    handle = _winapi.CreateFile(
                        address, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0,
                        _winapi.NULL, _winapi.OPEN_EXISTING,
                        _winapi.FILE_FLAG_OVERLAPPED, _winapi.NULL,
                    )
                    break
                except OSError as exc:
                    if exc.winerror != _winapi.ERROR_PIPE_BUSY:
                        raise
                    time.sleep(min(0.01, _remaining(deadline)))
            super().__init__(handle)
            try:
                _winapi.SetNamedPipeHandleState(handle, _winapi.PIPE_READMODE_MESSAGE, None, None)
            except OSError:
                self.close()
                raise

        def _recv_bytes(self, maxsize=None):
            while not self.poll(0):
                time.sleep(min(0.01, _remaining(self._deadline)))
            return super()._recv_bytes(maxsize)


@contextmanager
def _scan_connection(address, authkey: bytes, deadline: float):
    family = address_type(address)
    if family == "AF_PIPE" and sys.platform == "win32":
        connection = _ScanPipeConnection(address, deadline)
    else:
        sock = socket.socket(getattr(socket, family), socket.SOCK_STREAM)
        connection = _ScanSocketConnection(sock, deadline)
    with connection:
        if family != "AF_PIPE":
            sock.settimeout(_remaining(deadline))
            sock.connect(address)
        # Same mutual authentication and order as multiprocessing.connection.Client.
        answer_challenge(connection, authkey)
        deliver_challenge(connection, authkey)
        yield connection


class DaemonClient:
    def __init__(self, address=None):
        self.__address = address or get_current_config().DAEMON["socket"]
        self.__key = get_secret_key("daemon_key")

    def __get_connection(self):
        if not self.__address:
            raise DaemonUnavailableError("No daemon address set")
        try:
            return Client(address=self.__address, authkey=self.__key)
        except OSError:
            raise DaemonUnavailableError(
                f"Couldn't connect to daemon at {self.__address}"
            )

    def add_watched_folder(self, folder):
        if not isinstance(folder, str):
            raise TypeError("Expecting string, got " + str(type(folder)))
        with self.__get_connection() as c:
            c.send(AddWatchedFolderCommand(folder))

    def remove_watched_folder(self, folder):
        if not isinstance(folder, str):
            raise TypeError("Expecting string, got " + str(type(folder)))
        with self.__get_connection() as c:
            c.send(RemoveWatchedFolder(folder))

    def suppress_nfo_path(self, path, ttl):
        if not isinstance(path, str):
            raise TypeError("Expecting string, got " + str(type(path)))
        with self.__get_connection() as c:
            c.send(SuppressNfoPathCommand(path, ttl))
            return c.recv()

    def get_scanning_progress(self, timeout: float = 1.0):
        if not self.__address:
            raise DaemonUnavailableError("No daemon address set")
        try:
            with _scan_connection(self.__address, self.__key, time.monotonic() + timeout) as c:
                c.send(ScannerProgressCommand())
                return c.recv().scanned
        except (OSError, EOFError, AuthenticationError) as exc:
            raise DaemonUnavailableError("Daemon scan status unavailable") from exc

    def scan(self, folders=[], force=False):
        if not isinstance(folders, (list, tuple)):
            raise TypeError("Expecting list, got " + str(type(folders)))
        with self.__get_connection() as c:
            c.send(ScannerStartCommand(folders, force))

    def get_scheduler_jobs(self):
        with self.__get_connection() as c:
            c.send(SchedulerStatusCommand())
            return c.recv().jobs

    def jukebox_control(self, action, *args):
        if not isinstance(action, str):
            raise TypeError("Expecting string, got " + str(type(action)))
        with self.__get_connection() as c:
            c.send(JukeboxCommand(action, args))
            return c.recv()
