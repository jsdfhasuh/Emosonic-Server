# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2021-2023 Alban 'spl0k' Féron
#
# Distributed under terms of the GNU AGPLv3 license.

import os
import os.path
import sys
import warnings

from importlib.util import find_spec

from gevent import monkey

from ._base import BaseServer


if find_spec("geventwebsocket") is None:
    raise ImportError("The gevent backend requires gevent-websocket")


class FlushingStream:
    def __init__(self, stream):
        self._stream = stream

    def write(self, data):
        self._stream.write(data)
        self._stream.flush()

    def flush(self):
        self._stream.flush()


class GeventServer(BaseServer):
    @classmethod
    def prepare_runtime(cls) -> None:
        """Make I/O, locks and connection locals cooperative before app imports."""
        required = ("socket", "ssl", "threading", "time", "select")
        if all(monkey.is_module_patched(name) for name in required):
            return

        unsafe = (
            "ssl",
            "peewee",
            "supysonic.web",
            "supysonic.db",
            "supysonic.db_layer",
            "supysonic.emo",
        )
        loaded = [name for name in unsafe if name in sys.modules]
        if loaded:
            raise RuntimeError(
                "Gevent runtime must be prepared before importing application, "
                "database or SSL state; already imported: " + ", ".join(loaded)
                + ". Start a fresh process with supysonic-server -S gevent."
            )

        # Network-only patching would leave Peewee locals and locks shared by
        # concurrent greenlets. Patch the complete runtime before creating them.
        with warnings.catch_warnings():
            warnings.simplefilter("error", monkey.MonkeyPatchWarning)
            monkey.patch_all()
        if not all(monkey.is_module_patched(name) for name in required):
            raise RuntimeError("Gevent runtime preparation was incomplete")

    def _build_kwargs(self):
        import logging

        from gevent import socket

        from ._gevent_handler import EmosonicWebSocketHandler

        rv = {
            "application": self._load_app(),
            "handler_class": EmosonicWebSocketHandler,
            "log": FlushingStream(sys.stdout),
            "error_log": FlushingStream(sys.stderr),
        }
        logging.getLogger(__name__).info(
            "server event=gevent_runtime_ready socket=cooperative "
            "connection_local=greenlet locks=cooperative"
        )

        if self._socket is not None:
            if os.path.exists(self._socket):
                os.remove(self._socket)

            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(self._socket)
            listener.listen()

            rv["listener"] = listener
        else:
            rv["listener"] = (self._host, self._port)

        return rv

    def _run(self, **kwargs):
        from gevent.pywsgi import WSGIServer

        return WSGIServer(**kwargs).serve_forever()


server = GeventServer
