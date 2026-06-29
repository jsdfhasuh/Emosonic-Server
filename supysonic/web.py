# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2013-2023 Alban 'spl0k' Féron
#
# Distributed under terms of the GNU AGPLv3 license.

import logging
import mimetypes

from flask import Flask
from os import makedirs, path
from .    import TaskManger 
from .config import IniConfig
from .cache import Cache
from .db import init_database, open_connection, close_connection, Folder
from .logging_manager import build_web_logging_config, configure_web_logging, register_access_logging
from .utils import get_secret_key
from .recommend import create_recommend_playlist
logger = logging.getLogger(__package__)


def create_application(config=None):
    global app

    # Flask!
    app = Flask(__name__)
    app.config.from_object("supysonic.config.DefaultConfig")
    if not config:  # pragma: nocover
        config = IniConfig.from_common_locations()
    app.config.from_object(config)

    configure_web_logging(build_web_logging_config(app.config["WEBAPP"]), logger_name=logger.name)
    register_access_logging(app, logger_name=logger.name)

    # Initialize database
    init_database(app.config["BASE"]["database_uri"])
    if not app.testing:

        def open_conn():  # Just to discard the return value
            open_connection()

        app.before_request(open_conn)
        app.teardown_request(lambda exc: close_connection())

    # Insert unknown mimetypes
    for k, v in app.config["MIMETYPES"].items():
        extension = "." + k.lower()
        if extension not in mimetypes.types_map:
            mimetypes.add_type(v, extension, False)

    # Initialize Cache objects
    # Max size is MB in the config file but Cache expects bytes
    cache_dir = app.config["WEBAPP"]["cache_dir"]
    max_size_cache = app.config["WEBAPP"]["cache_size"] * 1024**2
    max_size_transcodes = app.config["WEBAPP"]["transcode_cache_size"] * 1024**2
    app.cache = Cache(path.join(cache_dir, "cache"), max_size_cache)
    app.transcode_cache = Cache(path.join(cache_dir, "transcodes"), max_size_transcodes)

    # Test for the cache directory
    cache_path = app.config["WEBAPP"]["cache_dir"]
    if not path.exists(cache_path):
        makedirs(cache_path)  # pragma: nocover
    # Read or create secret key
    app.secret_key = get_secret_key("cookies_secret")

    # Import app sections
    if app.config["WEBAPP"]["mount_webui"]:
        from .frontend import frontend
        from .frontend.player import player
        from .frontend.share import share

        app.register_blueprint(frontend)
        app.register_blueprint(player)
        app.register_blueprint(share)
    if app.config["WEBAPP"]["mount_api"]:
        from .api import api

        app.register_blueprint(api, url_prefix="/rest")

    if app.config["WEBAPP"].get("mount_client_releases", True):
        from .frontend.client_releases import client_releases

        app.register_blueprint(client_releases, url_prefix="/client-releases")
        
    # import emosonic API
    if app.config["WEBAPP"].get("mount_emosonic", False):
        from .emo import api as emo_api
        from .emo.ws import init_socketio

        app.register_blueprint(emo_api, url_prefix="/emo")
        init_socketio(app)

    if not app.testing:
        close_connection()
        
    # init TaskManger 

    TaskManger_instance =TaskManger.get_task_manager()
    logger.info("Task Manager initialized: %s", TaskManger_instance)

    if not app.testing:
        from .scanner_func.scanner_review_tasks import runReviewTaskBootstrap

        TaskManger_instance.submit_task(
            "bootstrap-review-tasks",
            runReviewTaskBootstrap,
        )
    
    
    # match mode setting
    match_mode = app.config["BASE"].get("match_mode","strict").lower()
    if match_mode not in ["strict","relaxed"]:
        match_mode = "strict"
        
    # # create recommend music playlist
    # create_recommend_playlist()
    return app
