"""Maintain Track rows when files are removed or moved by the watcher."""

from __future__ import annotations

import logging

from ..db import Track, delete_track_metadata_for_tracks
from ..tool import get_file_md5
from .scanner_lookup import findFolder, findRootFolder

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..scanner import Scanner

def removeFile(scanner: Scanner, path: str) -> None:
    if not isinstance(path, str):
        raise TypeError("Expecting string, got " + str(type(path)))

    try:
        track = Track.get(path=path)
        delete_track_metadata_for_tracks([track.id])
        track.delete_instance(recursive=True)
        scanner.stats().deleted.tracks += 1
    except Track.DoesNotExist:
        pass


def moveFile(scanner: Scanner, src_path: str, dst_path: str) -> None:
    if not isinstance(src_path, str):
        raise TypeError("Expecting string, got " + str(type(src_path)))
    if not isinstance(dst_path, str):
        raise TypeError("Expecting string, got " + str(type(dst_path)))

    if src_path == dst_path:
        return

    try:
        track = Track.get(path=src_path)
    except Track.DoesNotExist:
        return

    try:
        dst_track = Track.get(path=dst_path)
        # Reuse the destination ownership before replacing that stale row.
        root = dst_track.root_folder
        folder = dst_track.folder
        removeFile(scanner, dst_path)
        track.root_folder = root
        track.folder = folder
    except Track.DoesNotExist:
        root = findRootFolder(dst_path)
        folder = findFolder(dst_path)
        track.root_folder = root
        track.folder = folder

    track.path = dst_path
    track.save()


def renowTrackHash(logger: logging.Logger) -> None:
    # Keep this utility near other Track row maintenance helpers.
    for track in Track.select():
        path = track.path
        if track.content_hash == "NULL":
            hashValue = get_file_md5(path)
            track.content_hash = hashValue
            track.save()
            logger.info(f"renow track {track.title} hash to {hashValue}")
