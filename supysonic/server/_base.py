# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2021-2023 Alban 'spl0k' Féron
#
# Distributed under terms of the GNU AGPLv3 license.

from abc import ABCMeta, abstractmethod

class BaseServer(metaclass=ABCMeta):
    def __init__(
        self, *, host=None, port=None, socket=None, processes=None, threads=None
    ):
        self._host = host
        self._port = port
        self._socket = socket
        self._processes = processes
        self._threads = threads

    @classmethod
    def prepare_runtime(cls) -> None:
        """Prepare the backend before application state is imported."""

    @abstractmethod
    def _build_kwargs(self): ...

    @abstractmethod
    def _run(self, **kwargs): ...

    def _load_app(self):
        from ..web import create_application

        return create_application()

    def run(self):
        self.prepare_runtime()
        self._run(**self._build_kwargs())
