# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2013-2022 Alban 'spl0k' Féron
#               2018-2019 Carey 'pR0Ps' Metcalfe
#
# Distributed under terms of the GNU AGPLv3 license.

import hashlib
import json
import logging
import mimetypes
import os.path
import shlex
import shutil
import subprocess
import zlib
from io import BytesIO
from typing import Dict, Optional, Tuple
from xml.etree import ElementTree

import mediafile
import requests
from flask import current_app
from flask import request, Response, send_file
from mutagen import MutagenError
from mutagen.flac import FLAC
from PIL import Image
from zipstream import ZipStream

from ..cache import CacheMiss
from ..db import Track, Album, Artist, Folder, now
from ..db import Image as db_image
from ..covers import EXTENSIONS
from ..tool import read_dict_from_json

from . import api_routing, get_entity, get_entity_id, log_api_event
from .exceptions import (
    GenericError,
    NotFound,
    ServerError,
    UnsupportedParameter,
)

logger = logging.getLogger(__name__)

STREAM_VARIANT_ORIGINAL = "original"
STREAM_VARIANT_FLAC_NO_PICTURE = "flac_no_picture"
STREAM_VARIANT_TRANSCODE = "transcode"


class MediaInfo:
    def __init__(
        self,
        container: Optional[str],
        audio_codec: Optional[str],
        sample_rate: Optional[int],
        bit_depth: Optional[int],
        channels: Optional[int],
        bitrate: Optional[int],
        duration_ms: Optional[int],
        stream_count: Optional[int],
        audio_stream_count: Optional[int],
        attached_picture_count: Optional[int],
        attached_picture_codec: Optional[str],
        attached_picture_width: Optional[int],
        attached_picture_height: Optional[int],
        content_length: Optional[int],
    ):
        self.container = container
        self.audio_codec = audio_codec
        self.sample_rate = sample_rate
        self.bit_depth = bit_depth
        self.channels = channels
        self.bitrate = bitrate
        self.duration_ms = duration_ms
        self.stream_count = stream_count
        self.audio_stream_count = audio_stream_count
        self.attached_picture_count = attached_picture_count
        self.attached_picture_codec = attached_picture_codec
        self.attached_picture_width = attached_picture_width
        self.attached_picture_height = attached_picture_height
        self.content_length = content_length


def _log_media_event(level, event, **fields):
    log_api_event(level, event, **fields)


def _clean_header_value(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _set_optional_header(
    headers: Dict[str, str], name: str, value: object
) -> None:
    header_value = _clean_header_value(value)
    if header_value is not None:
        headers[name] = header_value


def _image_details(image) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    mime_type = getattr(image, "mime_type", None)
    codec = None
    if mime_type and "/" in mime_type:
        codec = mime_type.rsplit("/", 1)[1].lower()

    try:
        with Image.open(BytesIO(image.data)) as img:
            width, height = img.size
    except (AttributeError, OSError, ValueError):
        width = None
        height = None

    return codec, width, height


def _flac_picture_details(path: str) -> Tuple[int, Optional[str], Optional[int], Optional[int]]:
    try:
        pictures = FLAC(path).pictures
    except (OSError, MutagenError):
        return 0, None, None, None

    if not pictures:
        return 0, None, None, None

    picture = pictures[0]
    codec = None
    if picture.mime and "/" in picture.mime:
        codec = picture.mime.rsplit("/", 1)[1].lower()

    return len(pictures), codec, picture.width or None, picture.height or None


def _file_content_length(path: str) -> Optional[int]:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _stat_fingerprint(path: str) -> Optional[str]:
    try:
        stat = os.stat(path)
    except OSError:
        return None

    fingerprint = "{}:{}:{}".format(
        os.path.abspath(path), stat.st_mtime_ns, stat.st_size
    )
    return "sha256:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def _track_bitrate_bps(track: Track) -> Optional[int]:
    if track.bitrate is None:
        return None
    return track.bitrate * 1000


def _track_duration_ms(track: Track) -> Optional[int]:
    if track.duration is None:
        return None
    return track.duration * 1000


def _read_media_info(track: Track, path: Optional[str] = None) -> MediaInfo:
    media_path = path or track.path
    container = track.suffix() or None
    audio_codec = container
    sample_rate = None
    bit_depth = None
    channels = None
    bitrate = _track_bitrate_bps(track)
    duration_ms = _track_duration_ms(track)
    attached_picture_count = None
    attached_picture_codec = None
    attached_picture_width = None
    attached_picture_height = None

    try:
        tag = mediafile.MediaFile(media_path)
    except mediafile.UnreadableFileError:
        tag = None

    if tag is not None:
        container = (tag.type or container or "").lower() or None
        audio_codec = container
        sample_rate = tag.samplerate or None
        bit_depth = tag.bitdepth or None
        channels = tag.channels or None
        if tag.bitrate:
            bitrate = tag.bitrate if tag.bitrate >= 10000 else tag.bitrate * 1000
        if tag.length:
            duration_ms = int(tag.length * 1000)
        images = tag.images or []
        attached_picture_count = len(images)
        if images:
            (
                attached_picture_codec,
                attached_picture_width,
                attached_picture_height,
            ) = _image_details(images[0])

    if container == "flac":
        (
            attached_picture_count,
            attached_picture_codec,
            attached_picture_width,
            attached_picture_height,
        ) = _flac_picture_details(media_path)

    if attached_picture_count is None:
        attached_picture_count = 1 if track.has_art else 0

    audio_stream_count = 1 if audio_codec else None
    stream_count = None
    if audio_stream_count is not None:
        stream_count = audio_stream_count + attached_picture_count

    return MediaInfo(
        container=container,
        audio_codec=audio_codec,
        sample_rate=sample_rate,
        bit_depth=bit_depth,
        channels=channels,
        bitrate=bitrate,
        duration_ms=duration_ms,
        stream_count=stream_count,
        audio_stream_count=audio_stream_count,
        attached_picture_count=attached_picture_count,
        attached_picture_codec=attached_picture_codec,
        attached_picture_width=attached_picture_width,
        attached_picture_height=attached_picture_height,
        content_length=_file_content_length(media_path),
    )


def _info_headers(prefix: str, info: MediaInfo) -> Dict[str, str]:
    headers = {}
    _set_optional_header(headers, f"EmoSonic-{prefix}-Container", info.container)
    _set_optional_header(headers, f"EmoSonic-{prefix}-Audio-Codec", info.audio_codec)
    _set_optional_header(
        headers, f"EmoSonic-{prefix}-Audio-Sample-Rate", info.sample_rate
    )
    _set_optional_header(
        headers, f"EmoSonic-{prefix}-Audio-Bit-Depth", info.bit_depth
    )
    _set_optional_header(
        headers, f"EmoSonic-{prefix}-Audio-Channels", info.channels
    )
    _set_optional_header(
        headers, f"EmoSonic-{prefix}-Audio-Bitrate", info.bitrate
    )
    _set_optional_header(
        headers, f"EmoSonic-{prefix}-Audio-Duration-Ms", info.duration_ms
    )
    if prefix == "Output":
        _set_optional_header(
            headers, "EmoSonic-Output-Content-Length", info.content_length
        )
    _set_optional_header(headers, f"EmoSonic-{prefix}-Stream-Count", info.stream_count)
    _set_optional_header(
        headers,
        f"EmoSonic-{prefix}-Audio-Stream-Count",
        info.audio_stream_count,
    )
    _set_optional_header(
        headers,
        f"EmoSonic-{prefix}-Attached-Picture-Count",
        info.attached_picture_count,
    )
    _set_optional_header(
        headers,
        f"EmoSonic-{prefix}-Attached-Picture-Codec",
        info.attached_picture_codec,
    )
    _set_optional_header(
        headers,
        f"EmoSonic-{prefix}-Attached-Picture-Width",
        info.attached_picture_width,
    )
    _set_optional_header(
        headers,
        f"EmoSonic-{prefix}-Attached-Picture-Height",
        info.attached_picture_height,
    )
    return headers


def _can_sanitize_flac(source_info: MediaInfo) -> bool:
    return (
        source_info.container == "flac"
        and bool(source_info.attached_picture_count)
    )


def _sanitized_output_info(source_info: MediaInfo, path: str) -> MediaInfo:
    return MediaInfo(
        container="flac",
        audio_codec=source_info.audio_codec,
        sample_rate=source_info.sample_rate,
        bit_depth=source_info.bit_depth,
        channels=source_info.channels,
        bitrate=source_info.bitrate,
        duration_ms=source_info.duration_ms,
        stream_count=source_info.audio_stream_count,
        audio_stream_count=source_info.audio_stream_count,
        attached_picture_count=0,
        attached_picture_codec=None,
        attached_picture_width=None,
        attached_picture_height=None,
        content_length=_file_content_length(path),
    )


def _transcode_output_info(
    track: Track,
    output_format: str,
    output_bitrate: int,
    content_length: Optional[int] = None,
) -> MediaInfo:
    return MediaInfo(
        container=output_format,
        audio_codec=output_format,
        sample_rate=None,
        bit_depth=None,
        channels=None,
        bitrate=output_bitrate * 1000,
        duration_ms=_track_duration_ms(track),
        stream_count=None,
        audio_stream_count=None,
        attached_picture_count=None,
        attached_picture_codec=None,
        attached_picture_width=None,
        attached_picture_height=None,
        content_length=content_length,
    )


def _add_stream_headers(
    response: Response,
    source_info: MediaInfo,
    output_info: MediaInfo,
    stream_variant: str,
    sanitized_available: bool,
    transcode_available: bool,
    source_path: str,
    output_path: Optional[str] = None,
) -> Response:
    headers = {}
    headers.update(_info_headers("Source", source_info))
    headers.update(_info_headers("Output", output_info))
    _set_optional_header(
        headers, "EmoSonic-Sanitized-Available", sanitized_available
    )
    _set_optional_header(headers, "EmoSonic-Transcode-Available", transcode_available)
    if sanitized_available:
        headers["EmoSonic-Preferred-Compatible-Variant"] = (
            STREAM_VARIANT_FLAC_NO_PICTURE
        )
    headers["EmoSonic-Stream-Variant"] = stream_variant
    _set_optional_header(
        headers, "EmoSonic-Media-Fingerprint", _stat_fingerprint(source_path)
    )
    if output_path and stream_variant == STREAM_VARIANT_FLAC_NO_PICTURE:
        _set_optional_header(
            headers,
            "EmoSonic-Sanitized-Fingerprint",
            _stat_fingerprint(output_path),
        )

    for name, value in headers.items():
        response.headers[name] = value

    if (
        output_info.content_length is not None
        and "Content-Range" not in response.headers
    ):
        response.headers["EmoSonic-Output-Content-Length"] = str(
            output_info.content_length
        )
    return response


def _variant_error(message: str, status_code: int) -> Response:
    response = request.formatter.error(0, message)
    response.status_code = status_code
    return response


def _sanitized_cache_key(track: Track) -> str:
    try:
        stat = os.stat(track.path)
        source_identity = "{}:{}:{}".format(track.id, stat.st_mtime_ns, stat.st_size)
    except OSError:
        source_identity = "{}:missing".format(track.id)

    digest = hashlib.sha256(source_identity.encode("utf-8")).hexdigest()
    return "{}-{}.flac".format(track.id, digest[:16])


def _get_or_create_flac_no_picture(track: Track) -> str:
    cache = current_app.transcode_cache
    cache_key = _sanitized_cache_key(track)

    try:
        return cache.get(cache_key)
    except CacheMiss:
        pass

    try:
        with cache.set_fileobj(cache_key) as output:
            with open(track.path, "rb") as source:
                shutil.copyfileobj(source, output)
            output.flush()

            sanitized = FLAC(output.name)
            sanitized.clear_pictures()
            sanitized.save()
    except (OSError, MutagenError) as exc:
        _log_media_event(
            logging.ERROR,
            "media_variant_failed",
            track_id=track.id,
            variant=STREAM_VARIANT_FLAC_NO_PICTURE,
            reason=exc.__class__.__name__,
        )
        raise

    return cache.get(cache_key)


def _is_range_probe() -> bool:
    return request.headers.get("Range", "").strip().lower() == "bytes=0-0"


def _should_record_playback() -> bool:
    return request.method != "HEAD" and not _is_range_probe()


def _record_playback(track: Track) -> None:
    track.play_count = track.play_count + 1
    track.last_play = now()
    track.save()

    user = request.user
    user.last_play = track
    user.last_play_date = now()
    user.save()


def prepare_transcoding_cmdline(
    base_cmdline, res, input_format, output_format, output_bitrate
):
    if not base_cmdline:
        return None
    ret = shlex.split(base_cmdline)
    ret = [
        part.replace("%srcpath", res.path)
        .replace("%srcfmt", input_format)
        .replace("%outfmt", output_format)
        .replace("%outrate", str(output_bitrate))
        .replace("%title", res.title)
        .replace("%album", res.album.name)
        .replace("%artist", res.artist.name)
        .replace("%tracknumber", str(res.number))
        .replace("%totaltracks", str(res.album.tracks.count()))
        .replace("%discnumber", str(res.disc))
        .replace("%genre", res.genre if res.genre else "")
        .replace("%year", str(res.year) if res.year else "")
        for part in ret
    ]
    return ret


@api_routing("/stream")
def stream_media():
    res = get_entity(Track)
    timeoffset = request.values.get("timeOffset")
    if "timeOffset" in request.values:
        raise UnsupportedParameter("timeOffset")
    if "size" in request.values:
        raise UnsupportedParameter("size")

    maxBitRate, request_format, estimateContentLength = map(
        request.values.get, ("maxBitRate", "format", "estimateContentLength")
    )
    if request_format:
        request_format = request_format.lower()

    variant = request.values.get("variant")
    if variant:
        variant = variant.lower()
    if variant not in (
        None,
        STREAM_VARIANT_ORIGINAL,
        STREAM_VARIANT_FLAC_NO_PICTURE,
    ):
        return _variant_error("Unsupported stream variant '{}'".format(variant), 400)

    source_info = _read_media_info(res)
    src_suffix = res.suffix()
    dst_suffix = res.suffix()
    dst_bitrate = res.bitrate
    dst_mimetype = res.mimetype

    config = current_app.config["TRANSCODING"]
    prefs = request.client
    sanitized_available = _can_sanitize_flac(source_info)
    transcode_available = bool(config)

    if variant == STREAM_VARIANT_FLAC_NO_PICTURE:
        if not sanitized_available:
            return _variant_error(
                "Stream variant '{}' not found for track".format(variant), 404
            )

        try:
            sanitized_path = _get_or_create_flac_no_picture(res)
        except (OSError, MutagenError):
            return _variant_error(
                "Failed to generate stream variant '{}'".format(variant), 500
            )

        output_info = _sanitized_output_info(source_info, sanitized_path)
        response = send_file(
            sanitized_path, mimetype="audio/flac", conditional=True
        )
        response.headers["Accept-Ranges"] = "bytes"
        _add_stream_headers(
            response,
            source_info,
            output_info,
            STREAM_VARIANT_FLAC_NO_PICTURE,
            sanitized_available,
            transcode_available,
            res.path,
            output_path=sanitized_path,
        )

        if _should_record_playback():
            _record_playback(res)

        return response

    using_default_format = False
    if request_format:
        dst_suffix = src_suffix if request_format == "raw" else request_format
    elif prefs.format:
        dst_suffix = prefs.format
    else:
        using_default_format = True
        dst_suffix = src_suffix

    if prefs.bitrate and prefs.bitrate < dst_bitrate:
        dst_bitrate = prefs.bitrate

    if maxBitRate:
        maxBitRate = int(maxBitRate)

        if dst_bitrate > maxBitRate and maxBitRate != 0:
            dst_bitrate = maxBitRate
            if using_default_format:
                dst_suffix = config.get("default_transcode_target") or dst_suffix

    # Find new mimetype if we're changing formats
    if dst_suffix != src_suffix:
        dst_mimetype = (
            mimetypes.guess_type("dummyname." + dst_suffix, False)[0]
            or "application/octet-stream"
        )

    if dst_suffix != src_suffix or dst_bitrate != res.bitrate:
        # Requires transcoding
        cache = current_app.transcode_cache
        cache_key = f"{res.id}-{dst_bitrate}.{dst_suffix}"
        output_info = _transcode_output_info(res, dst_suffix, dst_bitrate)

        try:
            cached_path = cache.get(cache_key)
            response = send_file(
                cached_path, mimetype=dst_mimetype, conditional=True
            )
            response.headers["Accept-Ranges"] = "bytes"
            output_info.content_length = _file_content_length(cached_path)
        except CacheMiss:
            transcoder = config.get(f"transcoder_{src_suffix}_{dst_suffix}")
            decoder = config.get("decoder_" + src_suffix) or config.get("decoder")
            encoder = config.get("encoder_" + dst_suffix) or config.get("encoder")
            if not transcoder and (not decoder or not encoder):
                transcoder = config.get("transcoder")
                if not transcoder:
                    message = "No way to transcode from {} to {}".format(
                        src_suffix, dst_suffix
                    )
                    _log_media_event(
                        logging.WARNING,
                        "media_stream_failed",
                        track_id=res.id,
                        source_format=src_suffix or "-",
                        target_format=dst_suffix,
                        target_bitrate=dst_bitrate,
                        reason="no_transcoder",
                    )
                    logger.info(message)
                    raise GenericError(message)

            if estimateContentLength == "true":
                estimate = dst_bitrate * 1000 * res.duration // 8
            else:
                estimate = None

            transcoder, decoder, encoder = (
                prepare_transcoding_cmdline(x, res, src_suffix, dst_suffix, dst_bitrate)
                for x in (transcoder, decoder, encoder)
            )

            if request.method == "HEAD":
                response = Response(mimetype=dst_mimetype)
                if estimate is not None:
                    response.headers.add("Content-Length", estimate)
            else:
                try:
                    if transcoder:
                        dec_proc = None
                        proc = subprocess.Popen(transcoder, stdout=subprocess.PIPE)
                    else:
                        dec_proc = subprocess.Popen(decoder, stdout=subprocess.PIPE)
                        proc = subprocess.Popen(
                            encoder, stdin=dec_proc.stdout, stdout=subprocess.PIPE
                        )
                except OSError:
                    _log_media_event(
                        logging.ERROR,
                        "media_stream_failed",
                        track_id=res.id,
                        source_format=src_suffix or "-",
                        target_format=dst_suffix,
                        target_bitrate=dst_bitrate,
                        reason="transcoder_process_error",
                    )
                    raise ServerError("Error while running the transcoding process")

                def transcode():
                    while True:
                        data = proc.stdout.read(8192)
                        if not data:
                            break
                        yield data

                def kill_processes():
                    if dec_proc is not None:
                        dec_proc.kill()
                    proc.kill()

                def handle_transcoding():
                    try:
                        sent = 0
                        for data in transcode():
                            sent += len(data)
                            yield data
                    except (Exception, SystemExit, KeyboardInterrupt):
                        # Make sure child processes are always killed
                        kill_processes()
                        raise
                    except GeneratorExit:
                        # Try to transcode/send more data if we're close to the end.
                        # The calling code have to support this as yielding more data
                        # after a GeneratorExit would normally raise a RuntimeError.
                        # Hopefully this generator is only used by the cache which
                        # handles this.
                        if estimate and sent >= estimate * 0.95:
                            yield from transcode()
                        else:
                            kill_processes()
                            raise
                    finally:
                        if dec_proc is not None:
                            dec_proc.stdout.close()
                            dec_proc.wait()
                        proc.stdout.close()
                        proc.wait()

                resp_content = cache.set_generated(cache_key, handle_transcoding)

                _log_media_event(
                    logging.INFO,
                    "transcode_started",
                    track_id=res.id,
                    source_format=src_suffix or "-",
                    target_format=dst_suffix,
                    target_bitrate=dst_bitrate,
                    cache_key=cache_key,
                )
                logger.info(
                    "Transcoding track {0.id} for user {1.id}. Source: {2} at {0.bitrate}kbps. Dest: {3} at {4}kbps".format(
                        res, request.user, src_suffix, dst_suffix, dst_bitrate
                    )
                )
                response = Response(resp_content, mimetype=dst_mimetype)
                if estimate is not None:
                    response.headers.add("Content-Length", estimate)
        _add_stream_headers(
            response,
            source_info,
            output_info,
            STREAM_VARIANT_TRANSCODE,
            sanitized_available,
            transcode_available,
            res.path,
        )
    else:
        response = send_file(res.path, mimetype=dst_mimetype, conditional=True)
        response.headers["Accept-Ranges"] = "bytes"
        _add_stream_headers(
            response,
            source_info,
            source_info,
            STREAM_VARIANT_ORIGINAL,
            sanitized_available,
            transcode_available,
            res.path,
        )

    if _should_record_playback():
        _record_playback(res)

    return response


@api_routing("/download")
def download_media():
    id = request.values["id"]

    try:
        uid = get_entity_id(Track, id)
    except GenericError:
        uid = None
    try:
        fid = get_entity_id(Folder, id)
    except GenericError:
        fid = None

    if uid is None and fid is None:
        _log_media_event(logging.WARNING, "download_failed", entity_id=id, reason="invalid_id")
        raise GenericError("Invalid ID")

    if uid is not None:
        try:
            rv = Track[uid]
            return send_file(rv.path, mimetype=rv.mimetype, conditional=True)
        except Track.DoesNotExist:
            try:  # Album -> stream zipped tracks
                rv = Album[uid]
            except Album.DoesNotExist as e:
                _log_media_event(
                    logging.WARNING,
                    "download_failed",
                    entity_id=id,
                    reason="track_or_album_not_found",
                )
                raise NotFound("Track or Album") from e
    else:
        try:  # Folder -> stream zipped tracks, non recursive
            rv = Folder[fid]
        except Folder.DoesNotExist as e:
            _log_media_event(
                logging.WARNING,
                "download_failed",
                entity_id=id,
                reason="folder_not_found",
            )
            raise NotFound("Folder") from e

    # Stream a zip of multiple files to the client
    z = ZipStream(sized=True)
    if isinstance(rv, Folder):
        # Add the entire folder tree to the zip
        z.add_path(rv.path, recurse=True)
    else:
        # Add tracks + cover art to the zip, preventing potential naming collisions
        seen = set()
        for track in rv.tracks:
            filename = os.path.basename(track.path)
            name, ext = os.path.splitext(filename)
            index = 0
            while filename in seen:
                index += 1
                filename = f"{name} ({index})"
                if ext:
                    filename += ext

            z.add_path(track.path, filename)
            seen.add(filename)

        cover_path = _cover_from_collection(rv, extract=False)
        if cover_path:
            z.add_path(cover_path)

    if not z:
        _log_media_event(logging.WARNING, "download_failed", entity_id=id, reason="empty_archive")
        raise GenericError("Nothing to download")

    resp = Response(z, mimetype="application/zip")
    resp.headers["Content-Disposition"] = f"attachment; filename={rv.name}.zip"
    resp.headers["Content-Length"] = len(z)
    return resp


def _cover_from_track(obj):
    """Extract and return a path to a track's cover art

    Returns None if no cover art is available.
    """
    cache = current_app.cache
    cache_key = f"{obj.id}-cover"
    try:
        return cache.get(cache_key)
    except CacheMiss:
        try:
            return cache.set(cache_key, mediafile.MediaFile(obj.path).art)
        except mediafile.UnreadableFileError:
            return None


def _cover_from_collection(obj, extract=True):
    """Get a path to cover art from a collection (Album, Folder)

    If `extract` is True, will fall back to extracting cover art from tracks
    Returns None if no cover art is available.
    """
    cover_path = None

    if isinstance(obj, Folder) and obj.cover_art:
        cover_path = os.path.join(obj.path, obj.cover_art)

    elif isinstance(obj, Album):
        track_with_folder_cover = (
            obj.tracks.join(Folder, on=Track.folder)
            .where(Folder.cover_art.is_null(False))
            .first()
        )
        if track_with_folder_cover is not None:
            cover_path = _cover_from_collection(track_with_folder_cover.folder)

        if not cover_path and extract:
            track_with_embedded = obj.tracks.where(Track.has_art).first()
            if track_with_embedded is not None:
                cover_path = _cover_from_track(track_with_embedded)

    if not cover_path or not os.path.isfile(cover_path):
        return None
    return cover_path


def _get_cover_path(eid):
    try:
        fid = get_entity_id(Folder, eid)
    except GenericError:
        fid = None
    try:
        uid = get_entity_id(Track, eid)
    except GenericError:
        uid = None

    if not fid and not uid:
        raise GenericError("Invalid ID")

    if fid:
        try:
            return _cover_from_collection(Folder[fid])
        except Folder.DoesNotExist:
            pass
    elif uid:
        try:
            return _cover_from_track(Track[uid])
        except Track.DoesNotExist:
            pass

        try:
            return _cover_from_collection(Album[uid])
        except Album.DoesNotExist:
            pass

    raise NotFound("Entity")


def __new_get_cover_path(eid, input_size):
    """Get a path to cover art from a collection (Album, Folder)

    If `extract` is True, will fall back to extracting cover art from tracks
    Returns None if no cover art is available.
    """

    if 'al-' in eid:
        id = eid.replace('al-', '')
        cover_image = db_image.get_or_none(image_type="album", related_id=id)
        if cover_image:
            if os.path.exists(cover_image.path):
                return cover_image.path
            else:
                db_image.delete().where(
                    db_image.image_type == "album", db_image.related_id == id
                ).execute()
                return None
        else:
            return None
    elif 'ar-' in eid:
        id = eid.replace('ar-', '')
        artist = Artist.get_or_none(id=id)
        if not artist:
            return None
        if artist.artist_info_json:
            result = read_dict_from_json(artist.artist_info_json)
            if 'image' in result:
                if input_size:
                    return result['image'][input_size]
                for size in result['image']:
                    return result['image'][size]
        # 情况2: 使用艺术家的第一个专辑封面
        temp_album = Album.select().where(Album.artist == artist).first()
        if temp_album:
            cover_image = db_image.get_or_none(
                image_type="album", related_id=temp_album.id
            )
            return cover_image.path if cover_image else None
    else:
        return _get_cover_path(eid)

    return None


@api_routing("/getCoverArt")
def cover_art():
    cache = current_app.cache

    eid = request.values["id"]
    logger.debug("Fetching cover art for entity %s", eid)
    input_size = request.values.get("input_size", "")
    cover_path = __new_get_cover_path(eid, input_size)

    if not cover_path:
        _log_media_event(logging.WARNING, "cover_art_failed", entity_id=eid, reason="not_found")
        raise NotFound("Cover art")
    elif not os.path.isfile(cover_path):
        _log_media_event(logging.WARNING, "cover_art_failed", entity_id=eid, reason="missing_file")
        raise NotFound("Cover art file does not exist")

    size = request.values.get("size")
    if size:
        size = int(size)
    else:
        # If the cover was extracted from a track it won't have an accurate
        # extension for Flask to derive the mimetype from - derive it from the
        # contents instead.
        mimetype = None
        if os.path.splitext(cover_path)[1].lower() not in EXTENSIONS:
            with Image.open(cover_path) as im:
                mimetype = f"image/{im.format.lower()}"
        return send_file(cover_path, mimetype=mimetype)

    with Image.open(cover_path) as im:
        mimetype = f"image/{im.format.lower()}"
        if size > im.width and size > im.height:
            return send_file(cover_path, mimetype=mimetype)

        cache_key = f"{eid}-cover-{size}"
        try:
            return send_file(cache.get(cache_key), mimetype=mimetype)
        except CacheMiss:
            im.thumbnail([size, size], Image.Resampling.LANCZOS)
            with cache.set_fileobj(cache_key) as fp:
                im.save(fp, im.format)
            return send_file(cache.get(cache_key), mimetype=mimetype)


def lyrics_response_for_track(track, lyrics):
    return request.formatter(
        "lyrics",
        {"artist": track.album.artist.name, "title": track.title, "value": lyrics},
    )


def _chartlyrics_text(root: ElementTree.Element, name: str) -> str:
    ns = {"cl": "http://api.chartlyrics.com/"}
    elem = root.find(f"cl:{name}", namespaces=ns)
    if elem is None or elem.text is None:
        return ""
    return elem.text


@api_routing("/getLyrics")
def lyrics():

    id = request.values.get("id", "")
    if id:
        try:
            query = Track.select().where(Track.id == id)
        except Track.DoesNotExist:
            pass
    else:
        artist = request.values["artist"]
        title = request.values["title"]
        query = (
            Track.select()
            .join(Artist)
            .where(Track.title.contains(title), Artist.name.contains(artist))
        )
    for track in query:
        # Read from track metadata
        lyrics = mediafile.MediaFile(track.path).lyrics
        if lyrics is not None:
            lyrics = lyrics.replace("\x00", "").strip()
            if lyrics:
                logger.debug("Found lyrics in file metadata: " + track.path)
                return lyrics_response_for_track(track, lyrics)

        # Look for a text file with the same name of the track
        lyrics_path = os.path.splitext(track.path)[0] + ".txt"
        if os.path.exists(lyrics_path):
            logger.debug("Found lyrics file: " + lyrics_path)

            try:
                with open(lyrics_path) as f:
                    lyrics = f.read()
            except UnicodeError:
                # Lyrics file couldn't be decoded. Rather than displaying an error, try
                # with the potential next files or return no lyrics. Log it anyway.
                logger.warning("Unsupported encoding for lyrics file " + lyrics_path)
                continue

            return lyrics_response_for_track(track, lyrics)

    if not current_app.config["WEBAPP"]["online_lyrics"]:
        return request.formatter("lyrics", {})

    # Create a stable, unique, filesystem-compatible identifier for the artist+title
    unique = hashlib.md5(
        json.dumps([x.lower() for x in (artist, title)]).encode("utf-8")
    ).hexdigest()
    cache_key = f"lyrics-{unique}"

    lyrics = {}
    try:
        lyrics = json.loads(
            zlib.decompress(current_app.cache.get_value(cache_key)).decode("utf-8")
        )
    except (CacheMiss, zlib.error, TypeError, ValueError):
        try:
            r = requests.get(
                "http://api.chartlyrics.com/apiv1.asmx/SearchLyricDirect",
                params={"artist": artist, "song": title},
                timeout=5,
            )
            root = ElementTree.fromstring(r.content)
            lyrics = {
                "artist": _chartlyrics_text(root, "LyricArtist"),
                "title": _chartlyrics_text(root, "LyricSong"),
                "value": _chartlyrics_text(root, "Lyric"),
            }
            if not lyrics["value"]:
                lyrics = {}

            current_app.cache.set(
                cache_key, zlib.compress(json.dumps(lyrics).encode("utf-8"), 9)
            )
        except (
            requests.exceptions.RequestException,
            ElementTree.ParseError,
        ) as e:  # pragma: nocover
            _log_media_event(
                logging.WARNING,
                "lyrics_external_failed",
                artist=artist,
                title=title,
                reason=e.__class__.__name__,
            )
            logger.warning("Error while requesting the ChartLyrics API: " + str(e))

    return request.formatter("lyrics", lyrics)
