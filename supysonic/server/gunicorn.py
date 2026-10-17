# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2021-2023 Alban 'spl0k' Féron
#
# Distributed under terms of the GNU AGPLv3 license.

from gunicorn.app.base import BaseApplication

from ._base import BaseServer


def _drain_emo_realtime(worker):
    application = getattr(worker, "wsgi", None)
    if application is None:
        return
    application_config = getattr(application, "config", {})
    webapp_config = application_config.get("WEBAPP", {})
    if not webapp_config.get("mount_emosonic", False):
        return
    app_context = getattr(application, "app_context", None)
    if app_context is None:
        return
    from ..emo.ws import begin_strict_v2_shutdown

    with app_context():
        begin_strict_v2_shutdown(
            webapp_config.get("emo_strict_shutdown_grace_seconds", 5)
        )


class GunicornApp(BaseApplication):
    def __init__(self, **config):
        self.__config = config

        super().__init__()

    def load_config(self):
        socket = self.__config["socket"]
        host = self.__config["host"]
        port = self.__config["port"]
        processes = self.__config["processes"]
        threads = self.__config["threads"]

        if socket is not None:
            self.cfg.set("bind", f"unix:{socket}")
        else:
            self.cfg.set("bind", f"{host}:{port}")

        if processes is not None:
            self.cfg.set("workers", processes)
        if threads is not None:
            self.cfg.set("threads", threads)
        self.cfg.set("worker_int", _drain_emo_realtime)


class GunicornServer(BaseServer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.__server = GunicornApp(**kwargs)
        self.__server.load = self._load_app

    def _build_kwargs(self):
        return {}

    def _run(self, **kwargs):
        return self.__server.run()


server = GunicornServer
