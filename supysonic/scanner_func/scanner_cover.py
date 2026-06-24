"""Repair missing album cover assets from local files and external services."""

from __future__ import annotations

import logging
import os
from typing import List, Optional, TYPE_CHECKING

import mediafile

from ..covers import find_cover_in_folder
from ..db import Album, Folder, Image, Track
from ..lastfm import LastFm
from ..tool import download_image
from ..MusicBrainz import get_musicbrainz_album_image_info, search_musicbrainz_album
from .scanner_trace import logTrace

if TYPE_CHECKING:
    from ..scanner import Scanner


module_logger = logging.getLogger(__name__)


def _getAlbumCoverQuery(album: Album):
    return Image.select().where(Image.related_id == album.id, Image.image_type == "album").order_by(Image.id)


def _syncFolderCoverArt(folder: Folder, cover_name: Optional[str]) -> None:
    if folder.cover_art == cover_name:
        return

    folder.cover_art = cover_name
    folder.save()


def _syncAlbumCoverImage(album: Album, image_path: Optional[str]) -> None:
    covers = list(_getAlbumCoverQuery(album))
    primary_cover = covers[0] if covers else None

    for stale_cover in covers[1:]:
        stale_cover.delete_instance()

    if image_path is None:
        if primary_cover is not None:
            primary_cover.delete_instance()
        return

    if primary_cover is None:
        Image.create(image_type="album", related_id=album.id, path=image_path)
        return

    if primary_cover.path != image_path:
        primary_cover.path = image_path
        primary_cover.save()


def _syncFolderCover(folder: Folder, album: Optional[Album]) -> Optional[str]:
    album_name = album.name if album is not None else None
    cover = find_cover_in_folder(folder.path, album_name)
    cover_name = cover.name if cover else None
    _syncFolderCoverArt(folder, cover_name)

    if album is not None:
        image_path = os.path.join(folder.path, cover_name) if cover_name else None
        _syncAlbumCoverImage(album, image_path)

    return cover_name


def collectAlbumsMissingCover(scanner: Scanner) -> List[Album]:
    lost_cover_album: List[Album] = []
    for album in Album.select():
        if (
            Image.select()
            .where(Image.related_id == album.id, Image.image_type == "album")
            .exists()
        ):
            continue
        lost_cover_album.append(album)
        scanner.stats().lost_covers_albums[album.name] = ""
        scanner.stats().lost_covers.albums += 1
    return lost_cover_album


def findCover(scanner: Scanner, dirpath: str) -> None:
    if not isinstance(dirpath, str):  # pragma: nocover
        raise TypeError("Expecting string, got " + str(type(dirpath)))
    if not os.path.exists(dirpath):
        return

    try:
        folder = Folder.get(path=dirpath)
    except Folder.DoesNotExist:
        return

    track = folder.tracks.select().first()
    album = track.album if track is not None else None

    # This path handles folder-level cover discovery during scans and watcher updates.
    _syncFolderCover(folder, album)


def addCover(path: str, logger: logging.Logger) -> None:
    if not isinstance(path, str):  # pragma: nocover
        raise TypeError("Expecting string, got " + str(type(path)))

    try:
        folder = Folder.get(path=os.path.dirname(path))
    except Folder.DoesNotExist:
        return

    track = folder.tracks.select().first()
    album = None
    if track is not None:
        album = track.album
        if not album:
            logger.error(f"Track {track.path} has no album, cannot add cover")
            return

    # This path handles a specific cover file being created or updated.
    if not os.path.exists(path):
        logger.error(f"Cover file {path} does not exist, cannot add cover")
        return

    _syncFolderCover(folder, album)


def markAlbumCoverRestored(scanner: Scanner, album: Album) -> None:
    scanner.stats().lost_covers.albums -= 1
    scanner.stats().lost_covers_albums.pop(album.name, None)


def repairAlbumCover(
    scanner: Scanner,
    album: Album,
    get_cover_interner: bool = False,
    lfm: Optional[LastFm] = None,
    logger: Optional[logging.Logger] = None,
) -> None:
    trace_logger = logger or module_logger
    tracks = list(
        Track.select()
        .where(Track.album == album.id)
        .order_by(Track.disc, Track.number, Track.path)
    )
    scanner.stats().lost_covers_albums[album.name] = os.path.dirname(tracks[0].path) if tracks else ""
    if not tracks:
        logTrace(
            trace_logger,
            "ALBUM_COVER_TRACE",
            {"album": album.name},
            ["cover repair result: no track found"],
        )
        return

    track = None
    cover_file = None
    trace_details = []
    trace_header = {"album": album.name}
    for candidate in tracks:
        track_dir = os.path.dirname(candidate.path)
        scanner.stats().lost_covers_albums[album.name] = track_dir
        trace_header = {"album": album.name, "track_path": candidate.path}
        try:
            cover_file = find_cover_in_folder(path=track_dir, album_name=album.name)
        except ValueError:
            scanner.remove_file(candidate.path)
            logTrace(
                trace_logger,
                "ALBUM_COVER_TRACE",
                trace_header,
                [
                    "folder cover lookup: invalid path",
                    "stale track path removed",
                    "cover repair result: invalid track path",
                ],
            )
            continue

        track = candidate
        break

    if track is None:
        return

    track_dir = os.path.dirname(track.path)

    if cover_file:
        image_path = os.path.join(track_dir, cover_file.name)
        Image.get_or_create(image_type="album", related_id=album.id, path=image_path)
        markAlbumCoverRestored(scanner, album)
        logTrace(
            trace_logger,
            "ALBUM_COVER_TRACE",
            trace_header,
            [
                "cover source: folder file",
                f"selected cover file: {cover_file.name}",
            ],
        )
        return

    if not trace_details:
        trace_details.append("folder cover lookup: miss")

    if not os.path.exists(track.path):
        logTrace(
            trace_logger,
            "ALBUM_COVER_TRACE",
            trace_header,
            trace_details
            + [
                "embedded artwork: track path missing",
                "cover repair result: no source succeeded",
            ],
        )
        return

    try:
        cover = mediafile.MediaFile(track.path).art
    except mediafile.UnreadableFileError:
        logTrace(
            trace_logger,
            "ALBUM_COVER_TRACE",
            trace_header,
            trace_details + ["embedded artwork: unreadable track file"],
        )
        return
    if cover:
        image_path = os.path.join(
            scanner.scan_config.BASE['tempdatafolder'], "album", f"{album.name}.png"
        )
        os.makedirs(os.path.dirname(image_path), exist_ok=True)
        with open(image_path, "wb") as f:
            f.write(cover)
        Image.create(image_type="album", related_id=album.id, path=image_path)
        markAlbumCoverRestored(scanner, album)
        logTrace(
            trace_logger,
            "ALBUM_COVER_TRACE",
            trace_header,
            trace_details
            + [
                "embedded artwork: hit",
                f"saved embedded artwork: {image_path}",
            ],
        )
        return

    trace_details.append("embedded artwork: miss")

    if get_cover_interner and not cover and lfm:
        dl_status = False
        album_artist_name = album.artist.get_artist_name()
        search_musicbrainz_album(artist_name=album_artist_name, album_name=album.name)
        lastfm_album = lfm.get_albuminfo(artist_name=album_artist_name, album_name=album.name)
        album_mbid = lastfm_album.get('album', {}).get('mbid', "")
        if album_mbid:
            musicbrainz_image_json = get_musicbrainz_album_image_info(mb_album_id=album_mbid)
            if musicbrainz_image_json:
                for image in musicbrainz_image_json.get('images', []):
                    if image.get('front', False):
                        dl_url = image['image']
                        image_path = download_image(
                            save_folder=os.path.dirname(track.path),
                            save_name="cover.png",
                            url=dl_url,
                            logger=logger,
                        )
                        if image_path:
                            Image.create(image_type="album", related_id=album.id, path=image_path)
                            if logger:
                                logger.info("download %s cover image", album.name)
                            markAlbumCoverRestored(scanner, album)
                            dl_status = True
                            logTrace(
                                trace_logger,
                                "ALBUM_COVER_TRACE",
                                trace_header,
                                trace_details
                                + [
                                    "remote cover source: musicbrainz",
                                    f"downloaded cover path: {image_path}",
                                ],
                            )
                            return
                        if logger:
                            logger.error("Download image failed")
        if lastfm_album and not dl_status and lastfm_album['album']['image']:
            save_folder = os.path.dirname(track.path)
            for image in lastfm_album['album']['image']:
                if image['size'] not in ["large"]:
                    continue
                dl_url = image['#text']
                if not dl_url:
                    continue
                image_path = download_image(
                    save_folder=save_folder,
                    save_name="cover.png",
                    url=dl_url,
                    logger=logger,
                )
                if image_path:
                    Image.create(image_type="album", related_id=album.id, path=image_path)
                    if logger:
                        logger.info("download %s cover image", album.name)
                    markAlbumCoverRestored(scanner, album)
                    logTrace(
                        trace_logger,
                        "ALBUM_COVER_TRACE",
                        trace_header,
                        trace_details
                        + [
                            "remote cover source: lastfm",
                            f"downloaded cover path: {image_path}",
                        ],
                    )
                    return
                if logger:
                    logger.error("Download image failed")

    logTrace(
        trace_logger,
        "ALBUM_COVER_TRACE",
        trace_header,
        trace_details + ["cover repair result: no source succeeded"],
    )
