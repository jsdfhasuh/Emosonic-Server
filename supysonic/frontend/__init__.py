# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2013-2022 Alban 'spl0k' Féron
#                    2017 Óscar García Amor
#
# Distributed under terms of the GNU AGPLv3 license.

import json
import logging
import os
import time
from typing import Dict, List

from flask import (
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    request,
    render_template,
    send_file,
    session,
    url_for,
)
from PIL import Image
from flask import Blueprint
from functools import wraps
from ..covers import EXTENSIONS
from .. import VERSION, DOWNLOAD_URL
from ..TaskManger import list_task_results
from ..daemon.client import DaemonClient
from ..daemon.exceptions import DaemonUnavailableError
from ..db import (
    Artist,
    Album,
    EmoLocalQueue,
    EmoPlaybackState,
    EmoSessionQueue,
    Track,
    User,
    random,
)
from ..api.media import _get_cover_path
from ..emo.ws_state import DEFAULT_CLIENT_STALE_SECONDS, get_state
from ..managers.user import UserManager
from ..api.media import __new_get_cover_path
from ..cache import CacheMiss
from ..client_releases import get_latest_release
from ..recommend import (
    getLatestRecommendedPlaylist,
    getRecommendedPlaylistForDay,
)
from ..recommendation_feedback import (
    HOT_RECOMMENDED_SCOPE,
    filter_disliked_recommended_tracks,
    get_disliked_recommended_song_ids,
)
from ..recommendation_agent import (
    RecommendationAgentError,
    get_default_agent_message,
    get_recommendation_agent_language,
    get_recommendation_agent_prompts,
    request_recommendation_agent,
)

logger = logging.getLogger(__name__)

frontend = Blueprint("frontend", __name__)
state = get_state()
DEFAULT_DEVICE_TIMEOUT_SECONDS = DEFAULT_CLIENT_STALE_SECONDS
DEFAULT_RECOMMENDATION_COUNT = 30
MAX_RECOMMENDATION_COUNT = 100
ANONYMOUS_FRONTEND_ENDPOINTS = {
    "frontend.login",
    "frontend.register",
    "frontend.register_json",
}


@frontend.context_processor
def inject_metadata():
    return {
        "version": VERSION,
        "download_url": DOWNLOAD_URL,
        "allow_user_registration": current_app.config["WEBAPP"].get(
            "allow_user_registration", True
        ),
    }


@frontend.before_request
def login_check():
    request.user = None 
    should_login = True
    if session.get("userid"):
        try:
            user = UserManager.get(session.get("userid"))
            request.user = user
            should_login = False
        except (ValueError, User.DoesNotExist):
            session.clear()

    if should_login and request.endpoint not in ANONYMOUS_FRONTEND_ENDPOINTS:
        flash("Please login")
        return redirect(
            url_for(
                "frontend.login",
                returnUrl=request.script_root
                + request.url[len(request.url_root) - 1 :],
            )
        )


@frontend.before_request
def scan_status():
    if not request.user or not request.user.admin:
        return

    try:
        scanned = DaemonClient(
            current_app.config["DAEMON"]["socket"]
        ).get_scanning_progress()
        if scanned is not None:
            flash(f"Scanning in progress, {scanned} files scanned.")
    except DaemonUnavailableError:
        pass


@frontend.route("/")
def index():
    device_rows = getDeviceMonitorRows() if request.user and request.user.admin else []
    device_summary = getDeviceMonitorSummary(device_rows) if request.user and request.user.admin else None
    client_release_downloads = None
    if current_app.config["WEBAPP"].get("mount_client_releases", True):
        client_release_downloads = {
            "android": get_latest_release("android"),
            "windows": get_latest_release("windows"),
        }
    stats = {
        "artists": Artist.select().count(),
        "albums": Album.select().count(),
        "tracks": Track.select().count(),
    }
    return render_template(
        "home.html",
        stats=stats,
        device_summary=device_summary,
        recent_devices=device_rows[:5],
        client_release_downloads=client_release_downloads,
    )


def admin_only(f):
    @wraps(f)
    def decorated_func(*args, **kwargs):
        if not request.user or not request.user.admin:
            return redirect(url_for("frontend.index"))
        return f(*args, **kwargs)

    return decorated_func


def login_only(f):
    @wraps(f)
    def decorated_func(*args, **kwargs):
        if not request.user:
            return redirect(url_for("frontend.login"))
        return f(*args, **kwargs)

    return decorated_func


def getConnectedDevices(user_name=None):
    device_timeout = current_app.config["WEBAPP"].get(
        "emo_client_timeout", DEFAULT_DEVICE_TIMEOUT_SECONDS
    )
    try:
        device_timeout = float(device_timeout)
    except (TypeError, ValueError):
        device_timeout = DEFAULT_DEVICE_TIMEOUT_SECONDS
    devices = state.list_clients(
        user_name=user_name,
        stale_after_seconds=device_timeout if device_timeout and device_timeout > 0 else None,
    )
    devices.sort(key=lambda item: (item.get("userName", ""), item.get("deviceName", "")))
    return devices


def getDeviceMonitorRows(user_name=None):
    devices = getConnectedDevices(user_name=user_name)
    session_ids = sorted({d.get("sessionId") for d in devices if d.get("sessionId")})
    device_keys = {
        (d.get("sessionId"), d.get("clientId"))
        for d in devices
        if d.get("sessionId") and d.get("clientId")
    }

    playback_by_device = {}
    queue_by_session = {}
    local_queue_by_device = {}
    song_meta = {}

    if session_ids or device_keys:
        all_song_ids = set()

        playback_query = EmoPlaybackState.select()
        if session_ids:
            playback_query = playback_query.where(EmoPlaybackState.session_id.in_(session_ids))

        for record in playback_query:
            playback_key = (record.session_id, record.owner_client_id)
            if playback_key not in device_keys:
                continue
            if record.track_id:
                all_song_ids.add(str(record.track_id))
            playback_by_device[playback_key] = {
                "playbackSourceClientId": record.owner_client_id,
                "state": record.state,
                "trackId": record.track_id,
                "positionMs": record.position_ms,
                "volume": record.volume,
                "playbackUpdatedAt": record.updated_at.timestamp(),
            }

        for record in EmoSessionQueue.select().where(
            EmoSessionQueue.session_id.in_(session_ids)
        ):
            queue_song_ids = json.loads(record.queue_json)
            all_song_ids.update(str(song_id) for song_id in queue_song_ids)
            queue_by_session[record.session_id] = {
                "sourceClientId": record.owner_client_id,
                "queueSongIds": queue_song_ids,
                "queueCount": len(queue_song_ids),
                "currentIndex": record.current_index,
                "queuePositionMs": record.position_ms,
                "currentQueueSongId": (
                    queue_song_ids[record.current_index]
                    if queue_song_ids and 0 <= record.current_index < len(queue_song_ids)
                    else None
                ),
                "updatedAt": record.updated_at.timestamp(),
            }

        local_queue_query = EmoLocalQueue.select()
        if session_ids:
            local_queue_query = local_queue_query.where(EmoLocalQueue.session_id.in_(session_ids))

        for record in local_queue_query:
            local_queue_key = (record.session_id, record.owner_client_id)
            if local_queue_key not in device_keys:
                continue
            queue_song_ids = json.loads(record.queue_json)
            all_song_ids.update(str(song_id) for song_id in queue_song_ids)
            local_queue_by_device[local_queue_key] = {
                "localQueueSourceClientId": record.owner_client_id,
                "localQueueSongIds": queue_song_ids,
                "localQueueCount": len(queue_song_ids),
                "localCurrentIndex": record.current_index,
                "localQueuePositionMs": record.position_ms,
                "localCurrentQueueSongId": (
                    queue_song_ids[record.current_index]
                    if queue_song_ids and 0 <= record.current_index < len(queue_song_ids)
                    else None
                ),
                "localQueueUpdatedAt": record.updated_at.timestamp(),
            }

        if all_song_ids:
            for track in Track.select(Track, Artist).join(Artist).where(Track.id.in_(all_song_ids)):
                song_meta[str(track.id)] = {
                    "title": track.title,
                    "artist": track.artist.get_artist_name(),
                    "durationMs": (track.duration or 0) * 1000,
                }

    rows = []
    for device in devices:
        session_id = device.get("sessionId")
        client_id = device.get("clientId")
        playback = playback_by_device.get((session_id, client_id), {})
        queue = queue_by_session.get(session_id, {})
        local_queue = local_queue_by_device.get((session_id, client_id), {})
        row = dict(device)
        row.update(
            {
                "playbackSourceClientId": playback.get("playbackSourceClientId"),
                "state": playback.get("state"),
                "trackId": playback.get("trackId"),
                "positionMs": playback.get("positionMs"),
                "volume": playback.get("volume"),
                "playbackUpdatedAt": playback.get("playbackUpdatedAt"),
                "queueSourceClientId": queue.get("sourceClientId"),
                "queueSongIds": queue.get("queueSongIds") or [],
                "queueCount": queue.get("queueCount"),
                "currentIndex": queue.get("currentIndex"),
                "queuePositionMs": queue.get("queuePositionMs"),
                "currentQueueSongId": queue.get("currentQueueSongId"),
                "queueUpdatedAt": queue.get("updatedAt"),
                "localQueueSourceClientId": local_queue.get("localQueueSourceClientId"),
                "localQueueSongIds": local_queue.get("localQueueSongIds") or [],
                "localQueueCount": local_queue.get("localQueueCount"),
                "localCurrentIndex": local_queue.get("localCurrentIndex"),
                "localQueuePositionMs": local_queue.get("localQueuePositionMs"),
                "localCurrentQueueSongId": local_queue.get("localCurrentQueueSongId"),
                "localQueueUpdatedAt": local_queue.get("localQueueUpdatedAt"),
            }
        )
        if row.get("trackId"):
            row["trackLabel"] = song_meta.get(str(row["trackId"]), {}).get("title")
            artist_name = song_meta.get(str(row["trackId"]), {}).get("artist")
            row["durationMs"] = song_meta.get(str(row["trackId"]), {}).get("durationMs")
            if row.get("trackLabel") and artist_name:
                row["trackLabel"] = f"{row['trackLabel']} - {artist_name}"
            row["trackCoverUrl"] = url_for("frontend.device_cover", eid=row["trackId"])
        queue_labels = []
        queue_entries = []
        for song_id in row.get("queueSongIds") or []:
            meta = song_meta.get(str(song_id))
            if meta is None:
                label = str(song_id)
            elif meta.get("artist"):
                label = f"{meta['title']} - {meta['artist']}"
            else:
                label = meta["title"]
            queue_labels.append(label)
            queue_entries.append(
                {
                    "songId": str(song_id),
                    "label": label,
                    "isCurrent": str(song_id) == str(row.get("currentQueueSongId")),
                }
            )
        row["queueSongLabels"] = queue_labels
        row["queueEntries"] = queue_entries
        if row.get("currentQueueSongId"):
            current_meta = song_meta.get(str(row["currentQueueSongId"]))
            if current_meta is not None:
                row["currentQueueSongLabel"] = (
                    f"{current_meta['title']} - {current_meta['artist']}"
                    if current_meta.get("artist")
                    else current_meta["title"]
                )
            row["currentQueueSongCoverUrl"] = url_for(
                "frontend.device_cover", eid=row["currentQueueSongId"]
            )
        local_queue_labels = []
        local_queue_entries = []
        for song_id in row.get("localQueueSongIds") or []:
            meta = song_meta.get(str(song_id))
            if meta is None:
                label = str(song_id)
            elif meta.get("artist"):
                label = f"{meta['title']} - {meta['artist']}"
            else:
                label = meta["title"]
            local_queue_labels.append(label)
            local_queue_entries.append(
                {
                    "songId": str(song_id),
                    "label": label,
                    "isCurrent": str(song_id) == str(row.get("localCurrentQueueSongId")),
                }
            )
        row["localQueueSongLabels"] = local_queue_labels
        row["localQueueEntries"] = local_queue_entries
        if row.get("localCurrentQueueSongId"):
            current_meta = song_meta.get(str(row["localCurrentQueueSongId"]))
            if current_meta is not None:
                row["localCurrentQueueSongLabel"] = (
                    f"{current_meta['title']} - {current_meta['artist']}"
                    if current_meta.get("artist")
                    else current_meta["title"]
                )
            row["localCurrentQueueSongCoverUrl"] = url_for(
                "frontend.device_cover", eid=row["localCurrentQueueSongId"]
            )
        update_times = [
            row.get("playbackUpdatedAt"),
            row.get("queueUpdatedAt"),
            row.get("localQueueUpdatedAt"),
        ]
        row["lastUpdatedAt"] = max((value for value in update_times if value), default=None)
        rows.append(row)

    rows.sort(
        key=lambda item: (
            item.get("userName", ""),
            item.get("sessionId", ""),
            item.get("deviceName", ""),
        )
    )
    return rows


def getDeviceMonitorSummary(rows):
    session_ids = {row.get("sessionId") for row in rows if row.get("sessionId")}
    playing_devices = sum(1 for row in rows if row.get("state") == "playing")
    recently_updated = sum(1 for row in rows if row.get("lastUpdatedAt"))
    return {
        "onlineDevices": len(rows),
        "activeSessions": len(session_ids),
        "playingDevices": playing_devices,
        "recentlyUpdated": recently_updated,
    }


def _format_duration(total_seconds: int) -> str:
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _get_recommendation_count() -> int:
    try:
        count = int(request.args.get("count") or DEFAULT_RECOMMENDATION_COUNT)
    except (TypeError, ValueError):
        count = DEFAULT_RECOMMENDATION_COUNT
    return max(1, min(count, MAX_RECOMMENDATION_COUNT))


def _collect_random_recommendation_tracks(user, count: int) -> List[Track]:
    if count <= 0:
        return []

    disliked_song_ids = get_disliked_recommended_song_ids(
        user,
        scope=HOT_RECOMMENDED_SCOPE,
    )
    tracks = []
    seen_track_ids = set()
    query_limit = max(count * 3, 50)
    for track in Track.select().order_by(random()).limit(query_limit):
        track_id = str(track.id)
        if track_id in disliked_song_ids or track_id in seen_track_ids:
            continue
        tracks.append(track)
        seen_track_ids.add(track_id)
        if len(tracks) >= count:
            break

    return tracks


def _build_recommendation_context(user, count: int) -> Dict[str, object]:
    source = "daily"
    playlist = getRecommendedPlaylistForDay(user)
    if playlist is None:
        playlist = getLatestRecommendedPlaylist(user)
        source = "latest" if playlist is not None else "random"

    if playlist is not None:
        tracks = filter_disliked_recommended_tracks(
            user,
            playlist.get_tracks(),
            scope=HOT_RECOMMENDED_SCOPE,
        )[:count]
    else:
        tracks = _collect_random_recommendation_tracks(user, count)

    artist_names = {
        track.artist.get_artist_name()
        for track in tracks
        if getattr(track, "artist", None) is not None
    }
    album_ids = {
        track.album_id
        for track in tracks
        if getattr(track, "album_id", None) is not None
    }
    total_duration = sum(track.duration or 0 for track in tracks)

    return {
        "playlist": playlist,
        "tracks": tracks,
        "summary": {
            "source": source,
            "trackCount": len(tracks),
            "artistCount": len(artist_names),
            "albumCount": len(album_ids),
            "totalDuration": _format_duration(total_duration),
            "limit": count,
        },
    }


@frontend.route("/recommendations")
@login_only
def recommendation_index():
    context = _build_recommendation_context(
        request.user,
        _get_recommendation_count(),
    )
    return render_template("recommendations.html", **context)


@frontend.route("/recommendations/agent", methods=["GET", "POST"])
@login_only
def recommendation_agent():
    raw_data = {}
    if request.method == "POST":
        raw_data = request.get_json(silent=True) or {}

    raw_language = raw_data.get("language") or request.args.get("lang") or "en"
    language = get_recommendation_agent_language(str(raw_language))
    message = str(raw_data.get("message") or "").strip()[:400]
    if not message:
        message = get_default_agent_message(language)

    context = _build_recommendation_context(
        request.user,
        _get_recommendation_count(),
    )
    try:
        payload = request_recommendation_agent(
            current_app.config.get("RECOMMENDATION_AGENT", {}),
            request.user,
            message,
            language,
            context["tracks"],
            context["summary"],
        )
    except RecommendationAgentError as exc:
        return jsonify(
            {
                "ok": False,
                "error": str(exc),
                "errorCode": exc.error_code,
            }
        ), exc.status_code

    payload["suggestedPrompts"] = get_recommendation_agent_prompts(language)
    return jsonify(payload)


@frontend.route("/devices")
@admin_only
def device_index():
    rows = getDeviceMonitorRows()
    return render_template(
        "devices.html",
        devices=rows,
        summary=getDeviceMonitorSummary(rows),
    )


@frontend.route("/devices/data")
@admin_only
def device_data():
    rows = getDeviceMonitorRows()
    return jsonify({"devices": rows, "summary": getDeviceMonitorSummary(rows)})


@frontend.route("/admin/tasks")
@admin_only
def admin_tasks():
    tasks = list_task_results()
    status_counts = {"pending": 0, "completed": 0, "failed": 0}
    for t in tasks:
        status_counts[t["status"]] = status_counts.get(t["status"], 0) + 1
    summary = {
        "pending": status_counts["pending"],
        "completed": status_counts["completed"],
        "failed": status_counts["failed"],
        "total": len(tasks),
    }
    return render_template("admin-tasks.html", tasks=tasks, summary=summary)


@frontend.route("/admin/tasks/data")
@admin_only
def admin_tasks_data():
    tasks = list_task_results()
    status_counts = {"pending": 0, "completed": 0, "failed": 0}
    for t in tasks:
        status_counts[t["status"]] = status_counts.get(t["status"], 0) + 1
    return jsonify(
        {
            "tasks": tasks,
            "summary": {
                "pending": status_counts["pending"],
                "completed": status_counts["completed"],
                "failed": status_counts["failed"],
                "total": len(tasks),
            },
        }
    )


@frontend.route("/control")
@login_only
def control_index():
    return render_template("control.html")


@frontend.route("/control/cover/<eid>")
@login_only
def control_cover(eid):
    cache = current_app.cache

    target_track = Track.select().where(Track.id == eid).first()
    album = target_track.album if target_track else None
    new_eid = f"al-{album.id}" if album else None
    input_size = request.values.get("input_size", "")
    cover_path = __new_get_cover_path(new_eid, input_size)

    if not cover_path:
        abort(404, description="Cover art not found")
    elif not os.path.isfile(cover_path):
        abort(404, description="Cover art file not found")

    size = request.values.get("size")
    if size:
        size = int(size)
    else:
        mimetype = None
        if os.path.splitext(cover_path)[1].lower() not in EXTENSIONS:
            with Image.open(cover_path) as im:
                mimetype = f"image/{im.format.lower()}"
        return send_file(cover_path, mimetype=mimetype)

    with Image.open(cover_path) as im:
        mimetype = f"image/{im.format.lower()}"
        if size > im.width and size > im.height:
            return send_file(cover_path, mimetype=mimetype)

        cache_key = f"control-{eid}-cover-{size}"
        try:
            return send_file(cache.get(cache_key), mimetype=mimetype)
        except CacheMiss:
            im.thumbnail([size, size], Image.Resampling.LANCZOS)
            with cache.set_fileobj(cache_key) as fp:
                im.save(fp, im.format)
            return send_file(cache.get(cache_key), mimetype=mimetype)


@frontend.route("/control/track-meta")
@login_only
def control_track_meta():
    ids = request.args.getlist("ids")
    valid_ids = []
    for track_id in ids:
        try:
            uuid.UUID(track_id)
            valid_ids.append(track_id)
        except ValueError:
            continue

    result = {}
    if valid_ids:
        for track in Track.select(Track, Artist).join(Artist).where(Track.id.in_(valid_ids)):
            result[str(track.id)] = {
                "label": f"{track.title} - {track.artist.get_artist_name()}",
                "title": track.title,
                "artist": track.artist.get_artist_name(),
                "coverUrl": url_for("frontend.control_cover", eid=str(track.id)),
                "durationMs": (track.duration or 0) * 1000,
            }

    return jsonify(result)


@frontend.route("/devices/cover/<eid>")
@admin_only
def device_cover(eid):
    
    cache = current_app.cache

    target_track = Track.select().where(Track.id == eid).first()
    album = target_track.album if target_track else None
    new_eid = f"al-{album.id}" if album else None
    logger.debug("Fetching device cover for track %s", eid)
    input_size = request.values.get("input_size", "")
    cover_path = __new_get_cover_path(new_eid, input_size)

    if not cover_path:
        abort(404, description="Cover art not found")
    elif not os.path.isfile(cover_path):
        abort(404, description="Cover art file not found")

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


from .user import *
from .folder import *
from .playlist import *
from .music_requests import *
from .metadata import *
from .share import *
