"""Run the queued scanner lifecycle and library cleanup stages."""

from __future__ import annotations

import logging

from ..db import Album, Artist, Folder, close_connection, open_connection
from ..logging_utils import format_log_event
from .scanner_review_tasks import createReviewTasks

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..scanner import Scanner

def _scanQueuedFolders(scanner: Scanner) -> None:
    while not scanner.stop_requested:
        folderName = scanner.next_queued_folder()
        if folderName is None:
            break

        try:
            folder = Folder.get(name=folderName, root=True)
        except Folder.DoesNotExist:
            continue

        scanner.scan_folder(folder)


def pruneLibrary(scanner: Scanner) -> None:
    if scanner.stop_requested:
        return

    scanner.stats().deleted.albums += Album.prune()
    scanner.stats().deleted.artists += Artist.prune()
    Folder.prune()


def runScanner(scanner: Scanner, logger: logging.Logger) -> None:
    opened = open_connection(True)
    try:
        logger.info(
            format_log_event(
                "scanner",
                "run_start",
                force=getattr(scanner, "force_scan", "-"),
                follow_symlinks=getattr(scanner, "follow_symlinks", "-"),
            )
        )
        _scanQueuedFolders(scanner)
        if scanner.stop_requested:
            stats_getter = getattr(scanner, "stats", None)
            stats = stats_getter() if callable(stats_getter) else None
            logger.info(
                format_log_event(
                    "scanner",
                    "run_stopped",
                    scanned=getattr(stats, "scanned", "-"),
                    existing_tracks=getattr(stats, "existing_tracks", "-"),
                    result="stopped",
                )
            )
            return

        # Queue processing finishes first. The remaining steps depend on
        # having a complete view of the library state after traversal.
        scanner.decideAllPositions()
        pruneLibrary(scanner)
        logger.info(format_log_event("scanner", "repair_start"))
        scanner.find_lost_information()
        pruneLibrary(scanner)
        created_review_tasks = createReviewTasks(scanner)
        logger.info(
            format_log_event("scanner", "review_tasks_created", count=created_review_tasks)
        )
        stats = scanner.stats()
        logger.info(
            format_log_event(
                "scanner",
                "run_end",
                scanned=stats.scanned,
                existing_tracks=stats.existing_tracks,
                added_artists=stats.added.artists,
                added_albums=stats.added.albums,
                added_tracks=stats.added.tracks,
                deleted_artists=stats.deleted.artists,
                deleted_albums=stats.deleted.albums,
                deleted_tracks=stats.deleted.tracks,
                errors=len(stats.errors),
                result="completed",
            )
        )
        scanner.handle_done()
    finally:
        if opened:
            close_connection()
