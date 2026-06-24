# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2014-2022 Alban 'spl0k' Féron
#
# Distributed under terms of the GNU AGPLv3 license.

import logging
import os
import time

from threading import Thread, Condition, Timer
from typing import Optional
from watchdog.observers import Observer
from watchdog.events import PatternMatchingEventHandler

from . import covers
from .db import Folder, Track, open_connection, close_connection
from .logging_utils import format_log_event
from .scanner import Scanner
from .scanner_func.scanner_review_tasks import createReviewTasks
from .nfo.nfo import NfoHandler

OP_SCAN = 1 # 1
OP_REMOVE = 2 # 10
OP_MOVE = 4 # 100
FLAG_CREATE = 8 # 1000
FLAG_COVER = 16 # 10000
FLAG_NFO = 32 # 100000
FLAG_DIRECTORY = 64 # 1000000

logger = logging.getLogger(__name__)


def _path_tree_candidates(path: str):
    candidates = []
    for candidate in (os.path.normpath(path), os.path.abspath(path)):
        candidate = candidate.rstrip(os.sep) or os.sep
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _path_tree_condition(field, path: str):
    condition = None
    for base_path in _path_tree_candidates(path):
        if base_path == os.sep:
            path_condition = field.startswith(os.sep)
        else:
            path_condition = (field == base_path) | field.startswith(
                base_path + os.sep
            )
        condition = path_condition if condition is None else condition | path_condition

    return condition


class SupysonicWatcherEventHandler(PatternMatchingEventHandler):
    def __init__(self, extensions):
        patterns = None
        if extensions:
            patterns = ["*." + e.lower() for e in extensions.split()] + [
                "*" + e for e in covers.EXTENSIONS
            ]
        super().__init__(patterns=patterns, ignore_directories=True)

    def dispatch(self, event):
        try:
            if getattr(event, "is_directory", False):
                if event.event_type in ("created", "deleted", "moved"):
                    getattr(self, "on_" + event.event_type)(event)
                return
            super().dispatch(event)
        except Exception as e:  # pragma: nocover
            logger.critical(e)

    def on_created(self, event):
        if getattr(event, "is_directory", False):
            logger.debug("Directory created: '%s'", event.src_path)
            self.queue.put(event.src_path, OP_SCAN | FLAG_CREATE | FLAG_DIRECTORY)
            return

        logger.debug("File created: '%s'", event.src_path)

        op = OP_SCAN | FLAG_CREATE
        if covers.is_valid_cover(event.src_path):
            op |= FLAG_COVER
        if NfoHandler.is_nfo_file(event.src_path):
            op |= FLAG_NFO
        self.queue.put(event.src_path, op)

    def on_deleted(self, event):
        if getattr(event, "is_directory", False):
            logger.debug("Directory deleted: '%s'", event.src_path)
            self.queue.put(event.src_path, OP_REMOVE | FLAG_DIRECTORY)
            return

        logger.debug("File deleted: '%s'", event.src_path)

        op = OP_REMOVE
        _, ext = os.path.splitext(event.src_path)
        if ext in covers.EXTENSIONS:
            op |= FLAG_COVER

        self.queue.put(event.src_path, op)

    def on_modified(self, event):
        logger.debug("File modified: '%s'", event.src_path)
        op = OP_SCAN
        if covers.is_valid_cover(event.src_path):
            op |= FLAG_COVER
        if NfoHandler.is_nfo_file(event.src_path):
            op |= FLAG_NFO
        self.queue.put(event.src_path, op)

    def on_moved(self, event):
        if getattr(event, "is_directory", False):
            logger.debug("Directory moved: '%s' -> '%s'", event.src_path, event.dest_path)
            self.queue.put(event.dest_path, OP_MOVE | FLAG_DIRECTORY, src_path=event.src_path)
            return

        logger.debug("File moved: '%s' -> '%s'", event.src_path, event.dest_path)
        op = OP_MOVE
        _, ext = os.path.splitext(event.src_path)
        if ext in covers.EXTENSIONS:
            op |= FLAG_COVER
        if NfoHandler.is_nfo_file(event.src_path):
            op |= FLAG_NFO
        self.queue.put(event.dest_path, op, src_path=event.src_path)


class Event:
    def __init__(self, path, operation, **kwargs):
        if operation & (OP_SCAN | OP_REMOVE) == (OP_SCAN | OP_REMOVE):
            raise Exception("Flags SCAN and REMOVE both set")  # pragma: nocover

        self.__path = path
        self.__time = time.time()
        self.__op = operation
        self.__src = kwargs.get("src_path")

    def set(self, operation, **kwargs):
        if operation & (OP_SCAN | OP_REMOVE) == (OP_SCAN | OP_REMOVE):
            raise Exception("Flags SCAN and REMOVE both set")  # pragma: nocover

        self.__time = time.time()
        if operation & OP_SCAN:
            self.__op &= ~OP_REMOVE
        if operation & OP_REMOVE:
            self.__op &= ~OP_SCAN
        if operation & FLAG_CREATE:
            self.__op &= ~OP_MOVE
        if operation & FLAG_NFO:
            self.__op &= ~OP_MOVE
        self.__op |= operation

        src_path = kwargs.get("src_path")
        if src_path:
            self.__src = src_path

    @property
    def path(self):
        return self.__path

    @property
    def time(self):
        return self.__time

    @property
    def operation(self):
        return self.__op

    @property
    def src_path(self):
        return self.__src


class ScannerProcessingQueue(Thread):
    def __init__(self, delay):
        super().__init__()

        self.__timeout = delay
        self.__cond = Condition()
        self.__timer = None
        self.__queue = {}
        self.__running = True
        self.__suppressed_nfo_paths = {}

    def run(self):
        while self.__running:
            try:
                self.__run_next_batch()
            except Exception as exc:  # pragma: nocover
                logger.exception(
                    format_log_event(
                        "watcher",
                        "queue_batch_failed",
                        error_type=exc.__class__.__name__,
                    )
                )
                self.__wait_after_failure()

    def __wait_after_failure(self):
        with self.__cond:
            if self.__running:
                self.__cond.wait(min(max(self.__timeout, 0.1), 5))

    def __run_next_batch(self):
        time.sleep(0.1)
        with self.__cond:
            # If a timer fired while the thread was still processing the previous
            # batch, the notify is gone but the queue is already populated.
            while self.__running and not self.__queue:
                self.__cond.wait()

            if not self.__queue:
                return

        logger.debug("Instantiating scanner")
        connection_ready = False
        scanner = None
        try:
            connection_ready = open_connection(True)
            scanner = Scanner()
            self.__process_batch(scanner)
            scanner.prune()
        finally:
            if connection_ready:
                close_connection()
            if scanner is not None:
                logger.debug("Freeing scanner")

    def __process_batch(self, scanner):
        find_lost_information_flag = False
        while True:
            item = self.__next_item()
            if item is None:
                with self.__cond:
                    queue_empty = not self.__queue

                if not queue_empty:
                    time.sleep(0.05)
                    continue

                if find_lost_information_flag:
                    scanner.prune()
                    logger.info("Beginging cover scan")
                    scanner.find_lost_information()
                    scanner.prune()
                    createReviewTasks(scanner)
                    logger.info("Cover scan finished")
                    find_lost_information_flag = False
                    stats = scanner.stats()
                    logger.info(
                        "Cover scan completed,results: lost artists: %d, lost albums: %d",
                        stats.lost_covers.artists,
                        stats.lost_covers.albums,
                    )
                    for album in stats.lost_covers_albums:
                        logger.info(
                            f"album lost cover: {album} - {stats.lost_covers_albums[album]}"
                        )
                    for artist in stats.lost_covers_artists:
                        logger.info(f"artist lost cover: {artist}")
                    for album in stats.lost_year_albums:
                        logger.info(
                            f"album lost year: {album} - {stats.lost_year_albums[album]}"
                        )
                break

            try:
                if item.operation & FLAG_DIRECTORY:
                    self.__process_directory_item(scanner, item)
                    find_lost_information_flag = True
                elif item.operation & FLAG_COVER:
                    self.__process_cover_item(scanner, item)
                elif item.operation & FLAG_NFO:
                    self.__process_nfo_item(scanner, item)
                else:
                    self.__process_regular_item(scanner, item)
                    find_lost_information_flag = True
            except Exception as exc:
                logger.exception(
                    format_log_event(
                        "watcher",
                        "queue_item_failed",
                        path=item.path,
                        operation=item.operation,
                        error_type=exc.__class__.__name__,
                    )
                )

    def __process_regular_item(self, scanner, item):
        if item.operation & OP_MOVE:
            logger.info("Moving: '%s' -> '%s'", item.src_path, item.path)
            scanner.move_file(item.src_path, item.path)
            scanner.find_cover(os.path.dirname(item.src_path))
            scanner.find_cover(os.path.dirname(item.path))

        if item.operation & OP_SCAN:
            logger.info("Scanning: '%s'", item.path)
            scanner.scan_file(item.path)
            scanner.find_cover(os.path.dirname(item.path))

        if item.operation & OP_REMOVE:
            logger.info("Removing: '%s'", item.path)
            scanner.remove_file(item.path)

    def __find_root_folder_for_path(self, path: str) -> Optional[Folder]:
        path = os.path.abspath(path)
        matched_folder = None
        matched_path_length = -1

        for folder in Folder.select().where(Folder.root):
            folder_path = os.path.abspath(folder.path)
            try:
                if os.path.commonpath([path, folder_path]) != folder_path:
                    continue
            except ValueError:
                continue

            if len(folder_path) > matched_path_length:
                matched_folder = folder
                matched_path_length = len(folder_path)

        return matched_folder

    def __find_folder_for_path(self, path: str) -> Optional[Folder]:
        folder = Folder.get_or_none(Folder.path.in_(_path_tree_candidates(path)))
        if folder is not None:
            return folder

        path = os.path.abspath(path)
        for folder in Folder.select():
            if os.path.abspath(folder.path) == path:
                return folder

        return None

    def __remove_directory(self, scanner: Scanner, path: str) -> None:
        folder = self.__find_folder_for_path(path)
        if folder is not None:
            if folder.root:
                return
            scanner.stats().deleted.tracks += folder.delete_hierarchy()
            return

        for track in list(Track.select().where(_path_tree_condition(Track.path, path))):
            scanner.remove_file(track.path)

    def __scan_directory(self, scanner: Scanner, path: str) -> None:
        if not os.path.isdir(path):
            return
        if self.__find_root_folder_for_path(path) is None:
            return

        def on_error(error: OSError) -> None:
            if getattr(error, "filename", None):
                scanner.stats().errors.append(error.filename)

        for dirpath, dirnames, filenames in os.walk(
            path,
            topdown=True,
            onerror=on_error,
            followlinks=scanner.follow_symlinks,
        ):
            if scanner.stop_requested:
                break

            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            for filename in filenames:
                if filename.startswith("."):
                    continue

                filepath = os.path.join(dirpath, filename)
                if scanner.should_scan_extension(filepath):
                    scanner.scan_file(filepath)
                    scanner.stats().scanned += 1

            scanner.find_cover(dirpath)

    def __process_directory_item(self, scanner: Scanner, item: Event) -> None:
        if item.operation & OP_MOVE:
            logger.info("Moving directory: '%s' -> '%s'", item.src_path, item.path)
            self.__remove_directory(scanner, item.src_path)
            self.__scan_directory(scanner, item.path)

        if item.operation & OP_SCAN:
            logger.info("Scanning directory: '%s'", item.path)
            self.__scan_directory(scanner, item.path)

        if item.operation & OP_REMOVE:
            logger.info("Removing directory: '%s'", item.path)
            self.__remove_directory(scanner, item.path)

    def __process_cover_item(self, scanner, item):
        if item.operation & OP_SCAN:
            if os.path.isdir(item.path):
                logger.info("Looking for covers: '%s'", item.path)
                scanner.find_cover(item.path)
            else:
                logger.info("Potentially adding cover: '%s'", item.path)
                scanner.add_cover(item.path)

        if item.operation & OP_REMOVE:
            logger.info("Removing cover: '%s'", item.path)
            scanner.find_cover(os.path.dirname(item.path))

        if item.operation & OP_MOVE:
            logger.info("Moving cover: '%s' -> '%s'", item.src_path, item.path)
            scanner.find_cover(os.path.dirname(item.src_path))
            scanner.add_cover(item.path)

    def __process_nfo_item(self, scanner, item):
        if item.operation & OP_SCAN:
            if os.path.isdir(item.path):
                logger.info("Looking for nfo: '%s'", item.path)
                scanner.renow_album_by_nfo(item.path)
            else:
                logger.info("Potentially adding nfo: '%s'", item.path)
                scanner.renow_album_by_nfo(item.path)

        if item.operation & OP_REMOVE:
            logger.info("Removing nfo: '%s'", item.path)
            scanner.renow_album_by_nfo(os.path.dirname(item.path))

        if item.operation & OP_MOVE:
            logger.info("Moving nfo: '%s' -> '%s'", item.src_path, item.path)
            scanner.find_cover(os.path.dirname(item.src_path))
            scanner.renow_album_by_nfo(item.path)

    def stop(self):
        with self.__cond:
            self.__running = False
            self.__cond.notify()

    def __prune_suppressed_nfo_paths(self, now=None):
        current_time = time.time() if now is None else now
        for path, expires_at in list(self.__suppressed_nfo_paths.items()):
            if expires_at < current_time:
                del self.__suppressed_nfo_paths[path]

    def suppress_nfo_path(self, path, ttl):
        with self.__cond:
            self.__prune_suppressed_nfo_paths()
            self.__suppressed_nfo_paths[path] = time.time() + ttl

    def __is_suppressed_nfo_path(self, path):
        expires_at = self.__suppressed_nfo_paths.get(path)
        if expires_at is None:
            return False
        if expires_at < time.time():
            del self.__suppressed_nfo_paths[path]
            return False
        return True

    def put(self, path, operation, **kwargs):
        if not self.__running:
            raise RuntimeError("Trying to put an item in a stopped queue")

        with self.__cond:
            self.__prune_suppressed_nfo_paths()
            if operation & FLAG_NFO and self.__is_suppressed_nfo_path(path):
                return
            if path in self.__queue:
                event = self.__queue[path]
                event.set(operation, **kwargs)
            else:
                event = Event(path, operation, **kwargs)
                self.__queue[path] = event

            if operation & OP_MOVE and kwargs["src_path"] in self.__queue:
                previous = self.__queue[kwargs["src_path"]]
                event.set(previous.operation, src_path=previous.src_path)
                del self.__queue[kwargs["src_path"]]

            if self.__timer:
                self.__timer.cancel()
            self.__timer = Timer(self.__timeout, self.__wakeup)
            self.__timer.start()

    def unschedule_paths(self, basepath):
        with self.__cond:
            for path in list(self.__queue):
                if path.startswith(basepath):
                    del self.__queue[path]

    def __wakeup(self):
        with self.__cond:
            self.__cond.notify()
            self.__timer = None

    def __next_item(self):
        with self.__cond:
            if not self.__queue:
                return None

            next = min(self.__queue.items(), key=lambda i: i[1].time)
            if not self.__running or next[1].time + self.__timeout <= time.time():
                del self.__queue[next[0]]
                return next[1]

            return None


class SupysonicWatcher:
    def __init__(self, config):
        self.__delay = config.DAEMON["wait_delay"]
        self.__handler = SupysonicWatcherEventHandler(config.BASE["scanner_extensions"])

        self.__folders = {}
        self.__queue = None
        self.__observer = None

    def add_folder(self, folder):
        if isinstance(folder, Folder):
            path = folder.path
        elif isinstance(folder, str):
            path = folder
        else:
            raise TypeError("Expecting string or Folder, got " + str(type(folder)))

        logger.info(format_log_event("watcher", "folder_scheduled", path=path))
        watch = self.__observer.schedule(self.__handler, path, recursive=True)
        self.__folders[path] = watch

    def remove_folder(self, folder):
        if isinstance(folder, Folder):
            path = folder.path
        elif isinstance(folder, str):
            path = folder
        else:
            raise TypeError("Expecting string or Folder, got " + str(type(folder)))

        logger.info(format_log_event("watcher", "folder_unscheduled", path=path))
        self.__observer.unschedule(self.__folders[path])
        del self.__folders[path]
        self.__queue.unschedule_paths(path)

    def first_scanner(self):
        logger.info("Running first scanner")
        scanner = Scanner()
        scanner.find_lost_information()
        stats = scanner.stats()
        logger.info(
            "Cover scan completed,results: lost artists: %d, lost albums: %d",
            stats.lost_covers.artists,
            stats.lost_covers.albums,
        )
        for album in stats.lost_covers_albums:
            logger.info(
                f"album lost cover: {album} - {stats.lost_covers_albums[album]}"
            )
        for artist in stats.lost_covers_artists:
            logger.info(f"artist lost cover: {artist}")
        for album in stats.lost_year_albums:
            logger.info(
                f"album lost year: {album} - {stats.lost_year_albums[album]}"
            )
        logger.info("first scanner completed")
        scanner.prune()

    def start(self):
        # self.first_scanner()
        self.__queue = ScannerProcessingQueue(self.__delay)
        self.__observer = Observer()
        self.__handler.queue = self.__queue

        root_folders = list(Folder.select().where(Folder.root))
        for folder in root_folders:
            self.add_folder(folder)

        logger.info(
            format_log_event(
                "watcher",
                "start",
                delay=self.__delay,
                root_folders=len(root_folders),
            )
        )
        self.__queue.start()
        self.__observer.start()

    def stop(self):
        logger.info(format_log_event("watcher", "stop"))
        if self.__observer is not None:
            self.__observer.stop()
            self.__observer.join()
        if self.__queue is not None:
            self.__queue.stop()
            self.__queue.join()

        self.__observer = None
        self.__queue = None
        self.__handler.queue = None

    def suppress_nfo_path(self, path, ttl):
        if self.__queue is not None:
            self.__queue.suppress_nfo_path(path, ttl)

    @property
    def running(self):
        return (
            self.__queue is not None
            and self.__observer is not None
            and self.__queue.is_alive()
            and self.__observer.is_alive()
        )
