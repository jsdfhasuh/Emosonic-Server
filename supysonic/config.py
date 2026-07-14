# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2013-2020 Alban 'spl0k' Féron
#                    2017 Óscar García Amor
#
# Distributed under terms of the GNU AGPLv3 license.

import os
import sys
import tempfile

from configparser import RawConfigParser

current_config = None


def get_current_config():
    return current_config or DefaultConfig()


class DefaultConfig:
    DEBUG = False

    tempdir = os.path.join(tempfile.gettempdir(), "supysonic")
    BASE = {
        "database_uri": "sqlite:///" + os.path.join(tempdir, "supysonic.db"),
        "scanner_extensions": None,
        "follow_symlinks": False,
        "tempdatafolder": tempdir,
    }
    WEBAPP = {
        "cache_dir": tempdir,
        "cache_size": 1024,
        "transcode_cache_size": 512,
        "log_dir": None,
        "log_file": None,
        "log_backup_count": 7,
        "log_level": "WARNING",
        "log_rotate": True,
        "mount_webui": True,
        "mount_api": True,
        "mount_emosonic": False,
        "emo_ws_enabled": True,
        "emo_client_timeout": 90,
        "emo_log_upload_dir": "./logs",
        "emo_allowed_origins": "",
        "emo_development_mode": False,
        "emo_socketio_ping_interval": 25,
        "emo_socketio_ping_timeout": 20,
        "emo_socketio_max_pending_emits_per_connection": 100,
        "emo_unauthenticated_connections_per_ip": 10,
        "emo_authenticated_connections_per_user": 20,
        "emo_strict_requests_per_connection_per_minute": 120,
        "emo_strict_controls_per_connection_per_second": 20,
        "emo_strict_creates_per_connection_per_minute": 10,
        "emo_strict_handoff_starts_per_connection_per_minute": 10,
        "emo_strict_broadcast_starts_per_connection_per_minute": 10,
        "emo_strict_rate_limit_load_test_evidence": "",
        "emo_strict_shutdown_grace_seconds": 5,
        "emo_strict_v2_core_enabled": False,
        "emo_strict_v2_follow_enabled": False,
        "emo_strict_v2_handoff_enabled": False,
        "emo_strict_v2_broadcast_enabled": False,
        "emo_web_realtime_protocol": "legacy",
        "emo_browser_otp_ttl_seconds": 60,
        "emo_browser_otp_issues_per_session_per_minute": 12,
        "emo_browser_otp_outstanding_per_session": 4,
        "emo_browser_otp_global_capacity": 10000,
        "emo_web_strict_v2_follow_enabled": False,
        "emo_web_strict_v2_handoff_enabled": False,
        "emo_web_strict_v2_broadcast_enabled": False,
        "emo_web_strict_v2_acceptance_mode": False,
        "allow_user_registration": True,
        "registration_invite_code": "",
        "index_ignored_prefixes": "El La Le Las Les Los The",
        "online_lyrics": False,
        "mount_client_releases": True,
        "release_upload_dir": os.path.join(tempdir, "client-releases"),
        "release_api_token": "",
        "release_max_upload_size": 512 * 1024 * 1024,
    }
    DAEMON = {
        "socket": (
            r"\\.\pipe\supysonic"
            if sys.platform == "win32"
            else os.path.join(tempdir, "supysonic.sock")
        ),
        "run_watcher": True,
        "wait_delay": 5,
        "jukebox_command": None,
        "log_dir": None,
        "log_file": None,
        "log_backup_count": 7,
        "log_level": "WARNING",
        "log_rotate": True,
        "recommend_daily_refresh": True,
        "recommend_refresh_interval": 300,
        "recommend_playlist_size": 50,
        "recommend_playlist_archive_enabled": True,
        "recommend_playlist_retention_days": 5,
        "mood_scene_playlists_daily_refresh": True,
        "mood_scene_playlists_refresh_interval": 300,
        "mood_scene_playlist_size": 30,
        "mood_scene_playlist_retention_days": 1,
        "mood_scene_playlists_active_users_only": True,
        "review_task_maintenance": True,
        "review_task_maintenance_interval": 300,
        "track_metadata_enrichment": False,
        "track_metadata_enrichment_provider": "local",
        "track_metadata_enrichment_interval": 300,
        "track_metadata_enrichment_batch_size": 10,
        "track_metadata_enrichment_stale_lock_seconds": 900,
        "track_metadata_enrichment_send_path_hints": False,
        "track_metadata_enrichment_log_payload": False,
    }
    MUSICBRAINZ = {
        "api_url": "https://musicbrainz.org/ws/2",
        "cover_art_api_url": "https://coverartarchive.org",
        "user_agent": "Supysonic/1.0",
        "request_delay_seconds": 1.0,
    }
    DISCOGS = {
        "enabled": False,
        "api_url": "https://api.discogs.com",
        "token": "",
        "user_agent": "Supysonic/1.0",
        "request_delay_seconds": 1.0,
    }
    LASTFM = {"api_key": None, "secret": None}
    LISTENBRAINZ = {"api_url": "https://api.listenbrainz.org"}
    SPOTIFY = {"client_id": None, "client_secret": None}
    RECOMMENDATION_AGENT = {
        "enabled": False,
        "api_base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "",
        "timeout_seconds": 20,
        "history_limit": 200,
        "max_output_tokens": 900,
        "temperature": 0.7,
        "cache_ttl_seconds": 900,
    }
    TRANSCODING = {}
    MIMETYPES = {}

    def __init__(self):
        current_config = self


class IniConfig(DefaultConfig):
    common_paths = [
        "/etc/supysonic",
        os.path.expanduser("~/.supysonic"),
        os.path.expanduser("~/.config/supysonic/supysonic.conf"),
        "supysonic.conf",
    ]

    def __init__(self, paths):
        super().__init__()

        for attr, value in DefaultConfig.__dict__.items():
            if attr.startswith("_") or attr != attr.upper():
                continue

            if isinstance(value, dict):
                setattr(self, attr, value.copy())
            else:
                setattr(self, attr, value)

        parser = RawConfigParser()
        parser.read(paths)

        for section in parser.sections():
            options = {k: self.__try_parse(v) for k, v in parser.items(section)}
            section = section.upper()

            if hasattr(self, section):
                getattr(self, section).update(options)
            else:
                setattr(self, section, options)

    @staticmethod
    def __try_parse(value):
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                lv = value.lower()
                if lv in ("yes", "true", "on"):
                    return True
                if lv in ("no", "false", "off"):
                    return False
                return value

    @classmethod
    def from_common_locations(cls):
        return IniConfig(cls.common_paths)
