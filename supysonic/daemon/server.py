# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2019-2023 Alban 'spl0k' Féron
#
# Distributed under terms of the GNU AGPLv3 license.

import logging
import time

from multiprocessing.connection import Listener, Client
from threading import Thread, Event

from .client import DaemonCommand
from ..db import Folder, open_connection, close_connection
from ..jukebox import Jukebox
from ..logging_utils import format_log_event
from ..mood_scene_playlist_service import (
    DEFAULT_MOOD_SCENE_DAILY_PLAYLIST_LIMIT,
    DEFAULT_MOOD_SCENE_PLAYLIST_RETENTION_DAYS,
    cleanup_old_mood_scene_playlists,
    refresh_daily_mood_scene_playlists,
)
from ..recommend import getRecommendationDay, refreshDailyRecommendPlaylists
from ..scheduler import IntervalScheduler
from ..scanner import Scanner
from ..scanner_func.scanner_review_tasks import runReviewTaskMaintenance
from ..scanner_func.scanner_track_enrich import (
    LLMMetadataProvider,
    LocalMetadataProvider,
    TrackMetadataProviderError,
    runTrackMetadataEnrichmentPass,
)
from ..utils import get_secret_key
from ..watcher import SupysonicWatcher

__all__ = ["Daemon"]

logger = logging.getLogger(__name__)


class Daemon:
    def __init__(self, config):
        self.__config = config
        self.__listener = None
        self.__watcher = None
        self.__scanner = None
        self.__jukebox = None
        self.__scheduler = IntervalScheduler()
        self.__lastRecommendRefreshDay = None
        self.__lastMoodSceneRefreshDay = None
        self.__stopped = Event()

    watcher = property(lambda self: self.__watcher)
    scanner = property(lambda self: self.__scanner)
    jukebox = property(lambda self: self.__jukebox)
    scheduler = property(lambda self: self.__scheduler)

    def __handle_connection(self, connection):
        cmd = connection.recv()
        logger.debug("Received %s", cmd)
        if cmd is None:
            pass
        elif isinstance(cmd, DaemonCommand):
            cmd.apply(connection, self)
        else:
            logger.warning(
                format_log_event(
                    "daemon",
                    "unknown_command",
                    command_type=type(cmd).__name__,
                )
            )

    def run(self):
        self.__listener = Listener(
            address=self.__config.DAEMON["socket"], authkey=get_secret_key("daemon_key")
        )
        logger.info(format_log_event("daemon", "listening", socket=self.__listener.address))

        if self.__config.DAEMON["run_watcher"]:
            self.__watcher = SupysonicWatcher(self.__config)
            self.__watcher.start()
            logger.info(format_log_event("daemon", "watcher_started"))

        if self.__config.DAEMON["jukebox_command"]:
            self.__jukebox = Jukebox(self.__config.DAEMON["jukebox_command"])

        close_connection()

        Thread(target=self.__listen).start()
        self.__configure_scheduler()
        jobs = [job for job in self.__scheduler.list_jobs() if job["enabled"]]
        if jobs:
            self.__scheduler.start()
            logger.info(
                format_log_event(
                    "daemon",
                    "scheduler_started",
                    jobs=len(jobs),
                )
            )
        while not self.__stopped.is_set():
            time.sleep(1)

    def __listen(self):
        while not self.__stopped.is_set():
            conn = self.__listener.accept()
            self.__handle_connection(conn)

        self.__listener.close()

    def __get_recommend_refresh_interval(self):
        return max(60, int(self.__config.DAEMON.get("recommend_refresh_interval", 300)))

    def __get_recommend_playlist_size(self):
        return max(1, int(self.__config.DAEMON.get("recommend_playlist_size", 50)))

    def __get_mood_scene_playlists_refresh_interval(self):
        return max(
            60,
            int(self.__config.DAEMON.get("mood_scene_playlists_refresh_interval", 300)),
        )

    def __get_mood_scene_playlist_size(self):
        return max(
            1,
            int(
                self.__config.DAEMON.get(
                    "mood_scene_playlist_size",
                    DEFAULT_MOOD_SCENE_DAILY_PLAYLIST_LIMIT,
                )
            ),
        )

    def __get_mood_scene_playlist_retention_days(self):
        return max(
            1,
            int(
                self.__config.DAEMON.get(
                    "mood_scene_playlist_retention_days",
                    DEFAULT_MOOD_SCENE_PLAYLIST_RETENTION_DAYS,
                )
            ),
        )

    def __get_review_task_maintenance_interval(self):
        return max(60, int(self.__config.DAEMON.get("review_task_maintenance_interval", 300)))

    def __get_track_metadata_enrichment_interval(self):
        return max(60, int(self.__config.DAEMON.get("track_metadata_enrichment_interval", 300)))

    def __get_track_metadata_enrichment_batch_size(self):
        return max(1, int(self.__config.DAEMON.get("track_metadata_enrichment_batch_size", 10)))

    def __get_track_metadata_enrichment_stale_lock_seconds(self):
        return max(
            60,
            int(self.__config.DAEMON.get("track_metadata_enrichment_stale_lock_seconds", 900)),
        )

    def __configure_scheduler(self):
        self.__scheduler.register(
            "review-task-maintenance",
            self.__run_review_task_maintenance,
            self.__get_review_task_maintenance_interval(),
            enabled=self.__config.DAEMON.get("review_task_maintenance", True),
        )
        self.__scheduler.register(
            "recommend-refresh",
            self.__refresh_recommend_playlists_if_needed,
            self.__get_recommend_refresh_interval(),
            enabled=self.__config.DAEMON.get("recommend_daily_refresh", True),
        )
        self.__scheduler.register(
            "daily-mood-scene-playlists",
            self.__refresh_mood_scene_playlists_if_needed,
            self.__get_mood_scene_playlists_refresh_interval(),
            enabled=self.__config.DAEMON.get(
                "mood_scene_playlists_daily_refresh",
                True,
            ),
        )
        self.__scheduler.register(
            "track-metadata-enrichment",
            self.__run_track_metadata_enrichment,
            self.__get_track_metadata_enrichment_interval(),
            enabled=self.__config.DAEMON.get("track_metadata_enrichment", False),
        )

    def __run_review_task_maintenance(self):
        return runReviewTaskMaintenance()

    def __run_track_metadata_enrichment(self):
        provider_name = str(
            self.__config.DAEMON.get("track_metadata_enrichment_provider", "local")
        ).strip().lower()

        if provider_name == "local":
            provider = LocalMetadataProvider()
        elif provider_name == "llm":
            try:
                provider = LLMMetadataProvider(
                    getattr(self.__config, "RECOMMENDATION_AGENT", {})
                )
            except TrackMetadataProviderError as exc:
                logger.warning(
                    format_log_event(
                        "daemon",
                        "track_metadata_enrichment_provider_unavailable",
                        provider=provider_name,
                        error_type=exc.__class__.__name__,
                    )
                )
                return False
        else:
            logger.warning(
                format_log_event(
                    "daemon",
                    "track_metadata_enrichment_provider_unavailable",
                    provider=provider_name,
                )
            )
            return False

        opened = False
        try:
            opened = open_connection(True)
            logger.info(
                format_log_event(
                    "daemon",
                    "track_metadata_enrichment_started",
                    provider=provider_name,
                )
            )
            summary = runTrackMetadataEnrichmentPass(
                limit=self.__get_track_metadata_enrichment_batch_size(),
                provider=provider,
                stale_lock_seconds=self.__get_track_metadata_enrichment_stale_lock_seconds(),
                include_path_hints=bool(
                    self.__config.DAEMON.get(
                        "track_metadata_enrichment_send_path_hints",
                        False,
                    )
                ),
            )
            if summary.get("quota_exhausted"):
                logger.warning(
                    format_log_event(
                        "daemon",
                        "track_metadata_enrichment_quota_exhausted",
                        provider=provider_name,
                        selected=summary["selected"],
                        enriched=summary["enriched"],
                        failed=summary["failed"],
                        skipped=summary["skipped"],
                    )
                )
                return False
            logger.info(
                format_log_event(
                    "daemon",
                    "track_metadata_enrichment_completed",
                    provider=provider_name,
                    selected=summary["selected"],
                    enriched=summary["enriched"],
                    failed=summary["failed"],
                    skipped=summary["skipped"],
                )
            )
            return True
        except Exception as exc:
            logger.exception(
                format_log_event(
                    "daemon",
                    "track_metadata_enrichment_failed",
                    provider=provider_name,
                    error_type=exc.__class__.__name__,
                )
            )
            return False
        finally:
            if opened:
                close_connection()

    def __refresh_recommend_playlists_if_needed(self, current_day=None):
        recommendationDay = getRecommendationDay() if current_day is None else current_day
        if recommendationDay == self.__lastRecommendRefreshDay:
            return False

        opened = False
        try:
            opened = open_connection(True)
            logger.info(
                format_log_event(
                    "daemon",
                    "recommend_refresh_started",
                    day=recommendationDay,
                )
            )
            createdCount = refreshDailyRecommendPlaylists(
                num_songs=self.__get_recommend_playlist_size(),
                day=recommendationDay,
                config=self.__config,
            )
            self.__lastRecommendRefreshDay = recommendationDay
            logger.info(
                format_log_event(
                    "daemon",
                    "recommend_refresh_completed",
                    day=recommendationDay,
                    created=createdCount,
                )
            )
            return True
        except Exception as exc:
            logger.exception(
                format_log_event(
                    "daemon",
                    "recommend_refresh_failed",
                    day=recommendationDay,
                    error_type=exc.__class__.__name__,
                )
            )
            return False
        finally:
            if opened:
                close_connection()

    def __refresh_mood_scene_playlists_if_needed(self, current_day=None):
        playlistDay = getRecommendationDay() if current_day is None else current_day
        if playlistDay == self.__lastMoodSceneRefreshDay:
            return False

        opened = False
        try:
            opened = open_connection(True)
            logger.info(
                format_log_event(
                    "daemon",
                    "mood_scene_playlist_refresh_started",
                    day=playlistDay,
                )
            )
            refreshSummary = refresh_daily_mood_scene_playlists(
                limit=self.__get_mood_scene_playlist_size(),
                day=playlistDay,
                active_users_only=bool(
                    self.__config.DAEMON.get(
                        "mood_scene_playlists_active_users_only",
                        True,
                    )
                ),
            )
            failedCount = int(refreshSummary.get("failed", 0) or 0)
            if failedCount:
                logger.warning(
                    format_log_event(
                        "daemon",
                        "mood_scene_playlist_refresh_incomplete",
                        day=playlistDay,
                        created=refreshSummary.get("created", 0),
                        updated=refreshSummary.get("updated", 0),
                        skipped=refreshSummary.get("skipped", 0),
                        failed=failedCount,
                    )
                )
                return False

            cleanupSummary = cleanup_old_mood_scene_playlists(
                retention_days=self.__get_mood_scene_playlist_retention_days(),
                current_day=playlistDay,
            )
            self.__lastMoodSceneRefreshDay = playlistDay
            logger.info(
                format_log_event(
                    "daemon",
                    "mood_scene_playlist_refresh_completed",
                    day=playlistDay,
                    created=refreshSummary.get("created", 0),
                    updated=refreshSummary.get("updated", 0),
                    skipped=refreshSummary.get("skipped", 0),
                    failed=refreshSummary.get("failed", 0),
                    deleted=cleanupSummary.get("deleted", 0),
                    cleanup_skipped=cleanupSummary.get("skipped", 0),
                )
            )
            return True
        except Exception as exc:
            logger.exception(
                format_log_event(
                    "daemon",
                    "mood_scene_playlist_refresh_failed",
                    day=playlistDay,
                    error_type=exc.__class__.__name__,
                )
            )
            return False
        finally:
            if opened:
                close_connection()

    def start_scan(self, folders=[], force=False):
        logger.info(
            format_log_event(
                "daemon",
                "scan_requested",
                folders=len(folders) if folders else "all",
                force=force,
            )
        )
        if not folders:
            open_connection()
            folders = [
                t[0] for t in Folder.select(Folder.name).where(Folder.root).tuples()
            ]
            close_connection()

        if self.__scanner is not None and self.__scanner.is_alive():
            for f in folders:
                self.__scanner.queue_folder(f)
            logger.info(
                format_log_event(
                    "daemon",
                    "scan_queued",
                    folders=len(folders),
                    force=force,
                    reason="scanner_already_running",
                )
            )
            return

        extensions = self.__config.BASE["scanner_extensions"]
        if extensions:
            extensions = extensions.split(" ")

        self.__scanner = Scanner(
            force=force,
            extensions=extensions,
            follow_symlinks=self.__config.BASE["follow_symlinks"],
            on_folder_start=self.__unwatch,
            on_folder_end=self.__watch,
        )
        for f in folders:
            self.__scanner.queue_folder(f)

        self.__scanner.start()
        logger.info(
            format_log_event(
                "daemon",
                "scan_started",
                folders=len(folders),
                force=force,
            )
        )

    def __watch(self, folder):
        if self.__watcher is not None:
            self.__watcher.add_folder(folder.path)

    def __unwatch(self, folder):
        if self.__watcher is not None:
            self.__watcher.remove_folder(folder.path)

    def terminate(self):
        with Client(self.__listener.address, authkey=self.__listener._authkey) as c:
            self.__stopped.set()
            c.send(None)

        if self.__scanner is not None:
            self.__scanner.stop()
            self.__scanner.join()
        if self.__watcher is not None:
            self.__watcher.stop()
        self.__scheduler.stop()
        self.__scheduler.join()
        if self.__jukebox is not None:
            self.__jukebox.terminate()
