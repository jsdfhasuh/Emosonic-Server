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
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from flask import (
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    request,
    Response,
    render_template,
    send_file,
    session,
    stream_with_context,
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
    MusicRequest,
    Playlist,
    Track,
    User,
    User_Play_Activity,
    random,
)
from ..api.media import _get_cover_path
from ..emo.ws_state import DEFAULT_CLIENT_STALE_SECONDS, get_state
from ..managers.user import UserManager
from ..api.media import __new_get_cover_path
from ..cache import CacheMiss
from ..client_releases import get_latest_release
from ..recommend import (
    buildRecommendationReasonMap,
    getLatestRecommendedPlaylist,
    getRecommendedPlaylistForDay,
)
from ..recommendation_feedback import (
    HOT_RECOMMENDED_SCOPE,
    get_recommendation_feedback_preferences,
    set_recommendation_feedback,
    track_matches_negative_recommendation_feedback,
)
from ..recommendation_agent import (
    RecommendationAgentError,
    get_default_agent_message,
    get_recommendation_agent_health,
    get_recommendation_agent_language,
    get_recommendation_agent_prompts,
    request_recommendation_agent,
    stream_recommendation_agent,
)
from ..recommendation_agent_cache import clear_recommendation_agent_cache
from ..recommendation_agent_session import (
    clear_recommendation_agent_sessions,
    list_recommendation_agent_sessions,
)

logger = logging.getLogger(__name__)

frontend = Blueprint("frontend", __name__)
state = get_state()
DEFAULT_DEVICE_TIMEOUT_SECONDS = DEFAULT_CLIENT_STALE_SECONDS
DEFAULT_RECOMMENDATION_COUNT = 30
MAX_RECOMMENDATION_COUNT = 100
AGENT_STARTER_TRACK_LIMIT = 12
AGENT_STARTER_PLAYLIST_COMMENT = "Recommendation Agent starter playlist"
AGENT_STARTER_MUSIC_REQUEST_NOTE = (
    "Created from Recommendation Agent starter playlist action."
)
SCHEDULER_LOG_CAPTURE_NOTE = (
    "Run logs follow daemon log level and include only records emitted by the "
    "scheduled job thread."
)
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

    preferences = get_recommendation_feedback_preferences(
        user,
        scope=HOT_RECOMMENDED_SCOPE,
    )
    tracks = []
    seen_track_ids = set()
    query_limit = max(count * 3, 50)
    for track in Track.select().order_by(random()).limit(query_limit):
        track_id = str(track.id)
        if (
            track_id in seen_track_ids
            or track_matches_negative_recommendation_feedback(track, preferences)
        ):
            continue
        tracks.append(track)
        seen_track_ids.add(track_id)
        if len(tracks) >= count:
            break

    return tracks


def _add_unique_recommendation_tracks(
    tracks: List[Track],
    candidates,
    count: int,
    preferences: Dict[str, set],
    seen_track_ids: set,
) -> None:
    if len(tracks) >= count:
        return
    for track in candidates:
        track_id = str(track.id)
        if (
            track_id in seen_track_ids
            or track_matches_negative_recommendation_feedback(track, preferences)
        ):
            continue
        tracks.append(track)
        seen_track_ids.add(track_id)
        if len(tracks) >= count:
            break


def _get_user_listened_track_ids(user) -> set:
    return {
        activity.track_id
        for activity in User_Play_Activity.select(User_Play_Activity.track_id).where(
            User_Play_Activity.user == user
        )
    }


def _backfill_recommendation_tracks(
    user,
    tracks: List[Track],
    seed_tracks: List[Track],
    count: int,
    preferences: Dict[str, set],
    seen_track_ids: set,
) -> None:
    if len(tracks) >= count:
        return

    seed_tracks = list(seed_tracks)
    liked_more_song_ids = preferences.get("liked_more_song_ids", set())
    if liked_more_song_ids:
        seed_tracks.extend(
            Track.select().where(Track.id.in_(list(liked_more_song_ids)))
        )

    genres = []
    artist_ids = []
    for track in seed_tracks:
        if track.genre and track.genre not in genres:
            genres.append(track.genre)
        if track.artist_id and track.artist_id not in artist_ids:
            artist_ids.append(track.artist_id)

    query_limit = max((count - len(tracks)) * 5, 50)
    for genre in genres:
        _add_unique_recommendation_tracks(
            tracks,
            Track.select()
            .where(Track.genre == genre)
            .order_by(Track.play_count.desc(), Track.id)
            .limit(query_limit),
            count,
            preferences,
            seen_track_ids,
        )

    for artist_id in artist_ids:
        _add_unique_recommendation_tracks(
            tracks,
            Track.select()
            .where(Track.artist == artist_id)
            .order_by(Track.play_count.desc(), Track.id)
            .limit(query_limit),
            count,
            preferences,
            seen_track_ids,
        )

    listened_track_ids = _get_user_listened_track_ids(user)
    popular_query = Track.select()
    if listened_track_ids:
        popular_query = popular_query.where(Track.id.not_in(list(listened_track_ids)))
    _add_unique_recommendation_tracks(
        tracks,
        popular_query.order_by(Track.play_count.desc(), Track.id).limit(query_limit),
        count,
        preferences,
        seen_track_ids,
    )

    _add_unique_recommendation_tracks(
        tracks,
        Track.select().order_by(random()).limit(query_limit),
        count,
        preferences,
        seen_track_ids,
    )


def _filter_and_backfill_recommendation_tracks(
    user,
    playlist_tracks: List[Track],
    count: int,
) -> List[Track]:
    preferences = get_recommendation_feedback_preferences(
        user,
        scope=HOT_RECOMMENDED_SCOPE,
    )
    tracks: List[Track] = []
    seen_track_ids = set()
    _add_unique_recommendation_tracks(
        tracks,
        playlist_tracks,
        count,
        preferences,
        seen_track_ids,
    )
    _backfill_recommendation_tracks(
        user,
        tracks,
        playlist_tracks,
        count,
        preferences,
        seen_track_ids,
    )
    return tracks


def _build_recommendation_context(user, count: int) -> Dict[str, object]:
    source = "daily"
    playlist = getRecommendedPlaylistForDay(user)
    if playlist is None:
        playlist = getLatestRecommendedPlaylist(user)
        source = "latest" if playlist is not None else "random"

    if playlist is not None:
        tracks = _filter_and_backfill_recommendation_tracks(
            user,
            playlist.get_tracks(),
            count,
        )
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
    track_reasons = buildRecommendationReasonMap(user, tracks)

    return {
        "playlist": playlist,
        "tracks": tracks,
        "trackReasons": track_reasons,
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
    context["agentSessions"] = list_recommendation_agent_sessions(request.user)
    return render_template("recommendations.html", **context)


@frontend.route("/recommendations/feedback", methods=["POST"])
@login_only
def recommendation_feedback():
    raw_data = request.get_json(silent=True) or {}
    target_id = str(
        raw_data.get("id")
        or raw_data.get("targetId")
        or raw_data.get("target_id")
        or ""
    ).strip()
    action = str(raw_data.get("action") or "").strip()
    target_type = (
        raw_data.get("targetType")
        or raw_data.get("target_type")
    )
    scope = str(raw_data.get("scope") or HOT_RECOMMENDED_SCOPE).strip()
    reason = str(raw_data.get("reason") or "web_feedback").strip()
    source = str(raw_data.get("source") or "web").strip()

    if not target_id:
        return jsonify({"ok": False, "error": "feedback target id is required"}), 400
    if not action:
        return jsonify({"ok": False, "error": "feedback action is required"}), 400

    try:
        feedback = set_recommendation_feedback(
            request.user,
            target_id,
            action,
            scope=scope,
            reason=reason,
            source=source,
            target_type=target_type,
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify(
        {
            "ok": True,
            "feedback": {
                "id": feedback.target_id,
                "targetType": feedback.target_type,
                "targetId": feedback.target_id,
                "target_id": feedback.target_id,
                "action": feedback.action,
                "scope": feedback.scope,
            },
        }
    )


def _clean_agent_text(value: object, limit: int = 256) -> str:
    return " ".join(str(value or "").split())[:limit]


def _normalize_agent_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _clean_agent_starter_tracks(value: object) -> List[str]:
    if not isinstance(value, list):
        return []
    tracks = []
    seen = set()
    for item in value:
        title = _clean_agent_text(item, 256)
        key = _normalize_agent_text(title)
        if not title or key in seen:
            continue
        seen.add(key)
        tracks.append(title)
        if len(tracks) >= AGENT_STARTER_TRACK_LIMIT:
            break
    return tracks


def _match_local_agent_starter_tracks(
    artist_name: str,
    starter_tracks: Sequence[str],
) -> List[Track]:
    artist_key = _normalize_agent_text(artist_name)
    title_order = {
        _normalize_agent_text(title): index
        for index, title in enumerate(starter_tracks)
    }
    matched = []
    matched_ids = set()
    if not artist_key or not title_order:
        return matched

    for track in Track.select(Track, Artist).join(Artist):
        title_key = _normalize_agent_text(track.title)
        if title_key not in title_order or track.id in matched_ids:
            continue
        track_artist = track.artist.get_artist_name() if track.artist else ""
        if _normalize_agent_text(track_artist) != artist_key:
            continue
        matched.append(track)
        matched_ids.add(track.id)

    return sorted(
        matched,
        key=lambda track: title_order.get(_normalize_agent_text(track.title), 0),
    )


def _agent_track_id_sequence(tracks: Sequence[Track]) -> List[str]:
    return [str(track.id) for track in tracks]


def _find_existing_agent_starter_playlist(
    user: User,
    playlist_name: str,
    tracks: Sequence[Track],
) -> Optional[Playlist]:
    target_track_ids = _agent_track_id_sequence(tracks)
    if not target_track_ids:
        return None

    query = Playlist.select().where(
        Playlist.user == user,
        Playlist.name == playlist_name,
        Playlist.comment == AGENT_STARTER_PLAYLIST_COMMENT,
    )
    for playlist in query:
        if _agent_track_id_sequence(playlist.get_tracks()) == target_track_ids:
            return playlist
    return None


def _find_existing_agent_starter_music_request(
    user: User,
    artist_name: str,
    starter_tracks: Sequence[str],
) -> Optional[MusicRequest]:
    target_artist = _normalize_agent_text(artist_name)
    target_tracks = [_normalize_agent_text(track) for track in starter_tracks]
    if not target_artist or not target_tracks:
        return None

    query = MusicRequest.select().where(
        MusicRequest.user == user,
        MusicRequest.status == MusicRequest.STATUS_PENDING,
        MusicRequest.album_name.is_null(True),
        MusicRequest.note == AGENT_STARTER_MUSIC_REQUEST_NOTE,
    )
    for music_request in query:
        if _normalize_agent_text(music_request.artist_name) != target_artist:
            continue
        if [
            _normalize_agent_text(track)
            for track in music_request.get_track_titles()
        ] == target_tracks:
            return music_request
    return None


@frontend.route("/recommendations/agent/starter-playlist", methods=["POST"])
@login_only
def recommendation_agent_starter_playlist() -> Response:
    raw_data = request.get_json(silent=True) or {}
    artist_name = _clean_agent_text(
        raw_data.get("artistName") or raw_data.get("artist") or "",
        256,
    )
    starter_tracks = _clean_agent_starter_tracks(raw_data.get("starterTracks"))

    if not artist_name:
        return jsonify({"ok": False, "error": "artist name is required"}), 400
    if not starter_tracks:
        return jsonify({"ok": False, "error": "starter tracks are required"}), 400

    matched_tracks = _match_local_agent_starter_tracks(artist_name, starter_tracks)
    if matched_tracks:
        playlist_name = _clean_agent_text(
            f"{artist_name} starter playlist",
            240,
        )
        playlist = _find_existing_agent_starter_playlist(
            request.user,
            playlist_name,
            matched_tracks,
        )
        reused = playlist is not None
        if playlist is None:
            playlist = Playlist.create(
                user=request.user,
                name=playlist_name,
                comment=AGENT_STARTER_PLAYLIST_COMMENT,
            )
            for track in matched_tracks:
                playlist.add(track)
            playlist.save()
        return jsonify(
            {
                "ok": True,
                "mode": "playlist",
                "reused": reused,
                "playlist": {
                    "id": str(playlist.id),
                    "name": playlist.name,
                    "trackCount": len(playlist.get_tracks()),
                    "url": url_for(
                        "frontend.playlist_details",
                        uid=str(playlist.id),
                    ),
                },
            }
        )

    music_request = _find_existing_agent_starter_music_request(
        request.user,
        artist_name,
        starter_tracks,
    )
    reused = music_request is not None
    if music_request is None:
        music_request = MusicRequest.create(
            user=request.user,
            artist_name=artist_name,
            album_name=None,
            note=AGENT_STARTER_MUSIC_REQUEST_NOTE,
        )
        music_request.set_track_titles(starter_tracks)
        music_request.save()
    return jsonify(
        {
            "ok": True,
            "mode": "music_request",
            "reused": reused,
            "musicRequest": {
                "id": str(music_request.id),
                "artistName": music_request.artist_name,
                "trackCount": len(music_request.get_track_titles()),
                "url": url_for("frontend.music_request_index"),
            },
        }
    )


@frontend.route("/recommendations/agent", methods=["GET", "POST"])
@login_only
def recommendation_agent():
    request_id = getattr(g, "supysonic_request_id", None)
    (
        language,
        message,
        previous_recommended_artists,
        force_refresh,
    ) = _recommendation_agent_request_values()

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
            previous_recommended_artists,
            force_refresh=force_refresh,
        )
    except RecommendationAgentError as exc:
        error_payload = {
            "ok": False,
            "error": str(exc),
            "errorCode": exc.error_code,
            "requestId": request_id,
        }
        if getattr(exc, "details", None):
            error_payload["details"] = exc.details
        logger.warning(
            "recommendation_agent_error request_id=%s method=%s user=%s error_code=%s details=%s",
            request_id,
            request.method,
            getattr(request.user, "name", "-"),
            exc.error_code,
            getattr(exc, "details", {}),
        )
        return jsonify(
            error_payload
        ), exc.status_code

    payload["requestId"] = request_id
    payload["suggestedPrompts"] = get_recommendation_agent_prompts(language)
    return jsonify(payload)


@frontend.route("/recommendations/agent/health")
@login_only
def recommendation_agent_health():
    return jsonify(
        get_recommendation_agent_health(
            current_app.config.get("RECOMMENDATION_AGENT", {})
        )
    )


@frontend.route("/recommendations/agent/session/clear", methods=["POST"])
@login_only
def recommendation_agent_session_clear():
    deleted_count = clear_recommendation_agent_sessions(request.user)
    deleted_cache_count = clear_recommendation_agent_cache(request.user)
    return jsonify(
        {
            "ok": True,
            "deleted": deleted_count,
            "deletedCache": deleted_cache_count,
            "agentSessions": [],
        }
    )


def _recommendation_agent_error_payload(exc, request_id):
    error_payload = {
        "ok": False,
        "error": str(exc),
        "errorCode": exc.error_code,
        "requestId": request_id,
    }
    if getattr(exc, "details", None):
        error_payload["details"] = exc.details
    return error_payload


def _recommendation_agent_request_values():
    raw_data = {}
    if request.method == "POST":
        raw_data = request.get_json(silent=True) or {}

    raw_language = raw_data.get("language") or request.args.get("lang") or "en"
    language = get_recommendation_agent_language(str(raw_language))
    message = str(raw_data.get("message") or "").strip()[:400]
    if not message:
        message = get_default_agent_message(language)
    force_refresh = _truthy_request_value(
        raw_data.get("forceRefresh")
        or raw_data.get("force_refresh")
        or request.args.get("forceRefresh")
        or request.args.get("force_refresh")
    )
    return language, message, _request_recommended_artists(raw_data), force_refresh


def _truthy_request_value(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "yes", "true", "on")


def _request_recommended_artists(raw_data):
    artists = raw_data.get("previousRecommendedArtists")
    if not isinstance(artists, list):
        return []

    sanitized = []
    for artist in artists[:8]:
        if not isinstance(artist, dict):
            continue
        name = str(artist.get("name") or "").strip()
        if not name:
            continue
        genres = artist.get("genres")
        if not isinstance(genres, list):
            genres = []
        starter_tracks = artist.get("starterTracks")
        if not isinstance(starter_tracks, list):
            starter_tracks = []
        similar_to = artist.get("similarTo")
        if not isinstance(similar_to, list):
            similar_to = []
        mood = artist.get("mood")
        if not isinstance(mood, list):
            mood = []
        try:
            confidence = float(artist.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        sanitized.append(
            {
                "name": name[:120],
                "reason": str(artist.get("reason") or "").strip()[:500],
                "genres": [
                    str(genre).strip()[:80]
                    for genre in genres
                    if str(genre).strip()
                ][:6],
                "starterTracks": [
                    str(track).strip()[:120]
                    for track in starter_tracks
                    if str(track).strip()
                ][:8],
                "similarTo": [
                    str(similar).strip()[:120]
                    for similar in similar_to
                    if str(similar).strip()
                ][:6],
                "confidence": min(1.0, max(0.0, confidence)),
                "mood": [
                    str(value).strip()[:80]
                    for value in mood
                    if str(value).strip()
                ][:6],
            }
        )
    return sanitized


def _sse_event(event_name, payload):
    return (
        f"event: {event_name}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


def _sse_prelude():
    return ": " + (" " * 4096) + "\n\n"


@frontend.route("/recommendations/agent/stream", methods=["GET", "POST"])
@login_only
def recommendation_agent_stream():
    request_id = getattr(g, "supysonic_request_id", None)
    (
        language,
        message,
        previous_recommended_artists,
        force_refresh,
    ) = _recommendation_agent_request_values()
    context = _build_recommendation_context(
        request.user,
        _get_recommendation_count(),
    )

    @stream_with_context
    def generate():
        yield _sse_prelude()
        yield _sse_event("status", {"status": "thinking"})
        try:
            for event_name, payload in stream_recommendation_agent(
                current_app.config.get("RECOMMENDATION_AGENT", {}),
                request.user,
                message,
                language,
                context["tracks"],
                context["summary"],
                previous_recommended_artists,
                force_refresh=force_refresh,
            ):
                payload = dict(payload)
                if event_name == "final":
                    payload["requestId"] = request_id
                    payload["suggestedPrompts"] = get_recommendation_agent_prompts(language)
                yield _sse_event(event_name, payload)
        except RecommendationAgentError as exc:
            error_payload = _recommendation_agent_error_payload(exc, request_id)
            logger.warning(
                "recommendation_agent_stream_error request_id=%s method=%s user=%s error_code=%s details=%s",
                request_id,
                request.method,
                getattr(request.user, "name", "-"),
                exc.error_code,
                getattr(exc, "details", {}),
            )
            yield _sse_event("error", error_payload)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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


def _format_epoch_seconds(value):
    if value is None:
        return "—"
    try:
        timestamp = float(value)
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"


def _format_duration(value):
    if value is None:
        return "—"
    try:
        duration = max(0.0, float(value))
    except (TypeError, ValueError):
        return "—"
    if duration < 1:
        return f"{round(duration * 1000)}ms"
    return f"{duration:.3f}s"


def _format_task_rows(tasks):
    rows = []
    for task in tasks:
        row = dict(task)
        row["timestamp_display"] = _format_epoch_seconds(row.get("timestamp"))
        rows.append(row)
    return rows


def _format_scheduler_log(log):
    if not isinstance(log, dict):
        raise TypeError("Invalid scheduler log entry")
    item = dict(log)
    item["timestamp_display"] = _format_epoch_seconds(item.get("timestamp"))
    return item


def _format_scheduler_run(run):
    if not isinstance(run, dict):
        raise TypeError("Invalid scheduler run entry")
    item = dict(run)
    item["started_at_display"] = _format_epoch_seconds(item.get("started_at"))
    item["finished_at_display"] = _format_epoch_seconds(item.get("finished_at"))
    item["duration_display"] = _format_duration(item.get("duration"))
    logs = item.get("logs", [])
    if logs is None:
        logs = []
    if not isinstance(logs, (list, tuple)):
        raise TypeError("Invalid scheduler run logs")
    item["logs"] = [_format_scheduler_log(log) for log in logs]
    return item


def _format_scheduler_jobs(jobs):
    if jobs is None:
        return []
    if not isinstance(jobs, (list, tuple)):
        raise TypeError("Invalid scheduler jobs payload")
    rows = []
    for job in jobs:
        if not isinstance(job, dict):
            raise TypeError("Invalid scheduler job entry")
        row = dict(job)
        row["name"] = str(row.get("name") or "unknown")
        row["next_run_at_display"] = _format_epoch_seconds(row.get("next_run_at"))
        row["last_started_at_display"] = _format_epoch_seconds(row.get("last_started_at"))
        row["last_finished_at_display"] = _format_epoch_seconds(row.get("last_finished_at"))
        row["last_duration_display"] = _format_duration(row.get("last_duration"))
        row["current_duration_display"] = _format_duration(row.get("current_duration"))
        last_logs = row.get("last_logs", [])
        history = row.get("history", [])
        if last_logs is None:
            last_logs = []
        if history is None:
            history = []
        if not isinstance(last_logs, (list, tuple)):
            raise TypeError("Invalid scheduler job logs")
        if not isinstance(history, (list, tuple)):
            raise TypeError("Invalid scheduler job history")
        row["last_logs"] = [_format_scheduler_log(log) for log in last_logs]
        row["history"] = [_format_scheduler_run(run) for run in history]
        rows.append(row)
    return rows


def _summarize_task_results(tasks):
    status_counts = {"pending": 0, "completed": 0, "failed": 0}
    for task in tasks:
        status = task["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "pending": status_counts["pending"],
        "completed": status_counts["completed"],
        "failed": status_counts["failed"],
        "total": len(tasks),
    }


def _get_scheduler_jobs():
    try:
        return (
            DaemonClient(current_app.config["DAEMON"]["socket"]).get_scheduler_jobs(),
            None,
        )
    except (DaemonUnavailableError, EOFError, OSError, AttributeError, TypeError) as exc:
        return [], str(exc)


def _flatten_scheduler_runs(jobs):
    runs = []
    for job in jobs:
        for run in job.get("history", []):
            item = dict(run)
            item["job_name"] = job.get("name") or "unknown"
            runs.append(item)
    runs.sort(key=lambda item: _epoch_sort_key(item.get("started_at")), reverse=True)
    return runs


def _epoch_sort_key(value):
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _get_formatted_scheduler_jobs():
    scheduler_jobs, scheduler_error = _get_scheduler_jobs()
    try:
        return _format_scheduler_jobs(scheduler_jobs), scheduler_error
    except (TypeError, ValueError) as exc:
        return [], str(exc)


@frontend.route("/admin/tasks")
@admin_only
def admin_tasks():
    tasks = list_task_results()
    scheduler_jobs, scheduler_error = _get_formatted_scheduler_jobs()
    return render_template(
        "admin-tasks.html",
        tasks=_format_task_rows(tasks),
        summary=_summarize_task_results(tasks),
        scheduler_jobs=scheduler_jobs,
        scheduler_error=scheduler_error,
        scheduler_log_capture_note=SCHEDULER_LOG_CAPTURE_NOTE,
        scheduler_runs=_flatten_scheduler_runs(scheduler_jobs),
    )


@frontend.route("/admin/tasks/data")
@admin_only
def admin_tasks_data():
    tasks = list_task_results()
    scheduler_jobs, scheduler_error = _get_formatted_scheduler_jobs()
    return jsonify(
        {
            "tasks": _format_task_rows(tasks),
            "summary": _summarize_task_results(tasks),
            "scheduler": {
                "jobs": scheduler_jobs,
                "runs": _flatten_scheduler_runs(scheduler_jobs),
                "error": scheduler_error,
                "log_note": SCHEDULER_LOG_CAPTURE_NOTE,
            },
        }
    )


@frontend.route("/control")
@login_only
def control_index():
    return render_template("control.html")


PLAYER_SEARCH_LIMIT = 50
PLAYER_MAX_SEARCH_LIMIT = 100


def _player_stream_url(track_id):
    return f"{request.script_root}/rest/stream.view?id={track_id}&c=web-player"


def _player_track_payload(track):
    track_id = str(track.id)
    artist_name = track.artist.get_artist_name()
    album_name = track.album.name
    return {
        "id": track_id,
        "title": track.title,
        "artist": artist_name,
        "album": album_name,
        "label": f"{track.title} - {artist_name}",
        "durationMs": (track.duration or 0) * 1000,
        "coverUrl": url_for("frontend.player_cover", eid=track_id),
        "streamUrl": _player_stream_url(track_id),
    }


def _player_query_limit(default=PLAYER_SEARCH_LIMIT):
    raw_limit = request.args.get("limit", default)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, PLAYER_MAX_SEARCH_LIMIT))


def _valid_track_ids(raw_ids):
    valid_ids = []
    for track_id in raw_ids:
        try:
            uuid.UUID(track_id)
            valid_ids.append(track_id)
        except ValueError:
            continue
    return valid_ids


@frontend.route("/player")
@login_only
def player_index():
    return render_template("player.html")


@frontend.route("/player/search")
@login_only
def player_search():
    query = (request.args.get("q") or request.args.get("query") or "").strip()
    limit = _player_query_limit()

    tracks = (
        Track.select(Track, Artist, Album)
        .join(Artist, on=(Track.artist == Artist.id))
        .switch(Track)
        .join(Album, on=(Track.album == Album.id))
    )
    if query:
        tracks = tracks.where(
            (Track.title.contains(query))
            | (Artist.name.contains(query))
            | (Album.name.contains(query))
        )
    tracks = tracks.order_by(Track.created.desc()).limit(limit)

    return jsonify({"tracks": [_player_track_payload(track) for track in tracks]})


@frontend.route("/player/track-meta")
@login_only
def player_track_meta():
    valid_ids = _valid_track_ids(request.args.getlist("ids"))
    result = {}
    if valid_ids:
        tracks = (
            Track.select(Track, Artist, Album)
            .join(Artist, on=(Track.artist == Artist.id))
            .switch(Track)
            .join(Album, on=(Track.album == Album.id))
            .where(Track.id.in_(valid_ids))
        )
        result = {str(track.id): _player_track_payload(track) for track in tracks}
    return jsonify(result)


@frontend.route("/player/cover/<eid>")
@login_only
def player_cover(eid):
    return control_cover(eid)


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
    valid_ids = _valid_track_ids(request.args.getlist("ids"))

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
