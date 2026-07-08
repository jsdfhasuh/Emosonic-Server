"""Persistent daily mood and scene playlist management."""

from __future__ import annotations

import logging

from datetime import datetime, timedelta
from typing import Dict, List, Mapping, Optional, Tuple

from peewee import OperationalError

from .db import Playlist, User, User_Play_Activity, db
from .logging_utils import format_log_event
from .mood_scene_playlists import (
    SCENE_PLAYLISTS,
    get_mood_scene_playlist,
    list_mood_scene_playlist_keys,
)
from .recommend import getRecommendationDay


logger = logging.getLogger(__name__)

MOOD_SCENE_PLAYLIST_COMMENT_PREFIX = "mood_scene_playlist:"
SAVED_MOOD_SCENE_PLAYLIST_COMMENT_PREFIX = "saved_mood_scene_playlist:"
DEFAULT_MOOD_SCENE_DAILY_PLAYLIST_LIMIT = 30
DEFAULT_MOOD_SCENE_PLAYLIST_RETENTION_DAYS = 1


def get_mood_scene_playlist_comment(scene_key: str, day: str) -> str:
    return f"{MOOD_SCENE_PLAYLIST_COMMENT_PREFIX}{scene_key}:{day}"


def get_saved_mood_scene_playlist_comment(scene_key: str, day: str) -> str:
    return f"{SAVED_MOOD_SCENE_PLAYLIST_COMMENT_PREFIX}{scene_key}:{day}"


def is_system_mood_scene_playlist(playlist) -> bool:
    return str(getattr(playlist, "comment", "") or "").startswith(
        MOOD_SCENE_PLAYLIST_COMMENT_PREFIX
    )


def system_mood_scene_playlist_where():
    return Playlist.comment.is_null(False) & Playlist.comment.startswith(
        MOOD_SCENE_PLAYLIST_COMMENT_PREFIX
    )


def non_system_mood_scene_playlist_where():
    return Playlist.comment.is_null(True) | ~Playlist.comment.startswith(
        MOOD_SCENE_PLAYLIST_COMMENT_PREFIX
    )


def get_daily_mood_scene_playlist_name(user: object, scene_key: str, day: str) -> str:
    return get_mood_scene_playlist_display_name(scene_key, day)


def get_mood_scene_playlist_display_name(scene_key: str, day: str) -> str:
    scene = SCENE_PLAYLISTS.get(scene_key, {})
    label = scene.get("label") or scene_key
    return f"{day} {label} 情绪歌单"


def get_saved_mood_scene_playlist_name(user: object, scene_key: str, day: str) -> str:
    return f"{get_mood_scene_playlist_display_name(scene_key, day)}（我的副本）"


def get_system_mood_scene_playlist_display_name(playlist: object) -> Optional[str]:
    scene_key, day = parse_mood_scene_playlist_comment(
        getattr(playlist, "comment", None)
    )
    if scene_key is None or day is None:
        return None
    return get_mood_scene_playlist_display_name(scene_key, day)


def get_daily_mood_scene_playlist_for_user(user, scene_key: str, day: Optional[str] = None):
    playlist_day = getRecommendationDay() if day is None else day
    comment = get_mood_scene_playlist_comment(scene_key, playlist_day)
    return (
        Playlist.select()
        .where((Playlist.user == user) & (Playlist.comment == comment))
        .first()
    )


def parse_mood_scene_playlist_comment(comment: object) -> Tuple[Optional[str], Optional[str]]:
    text = str(comment or "")
    if not text.startswith(MOOD_SCENE_PLAYLIST_COMMENT_PREFIX):
        return None, None
    remainder = text[len(MOOD_SCENE_PLAYLIST_COMMENT_PREFIX):]
    scene_key, separator, day = remainder.partition(":")
    if not separator:
        return None, None
    return scene_key or None, day or None


def create_or_update_daily_mood_scene_playlist_for_user(
    user,
    scene_key: str,
    limit: int = DEFAULT_MOOD_SCENE_DAILY_PLAYLIST_LIMIT,
    day: Optional[str] = None,
) -> Dict[str, object]:
    playlist_day = getRecommendationDay() if day is None else day
    key = str(scene_key or "").strip()
    if key not in list_mood_scene_playlist_keys():
        return _playlist_result(key, "skipped", None, 0, error="unknown_scene_key")

    results = get_mood_scene_playlist(key, limit, user)
    if not results:
        return _playlist_result(key, "skipped", None, 0)

    comment = get_mood_scene_playlist_comment(key, playlist_day)
    with db.atomic():
        playlist = (
            Playlist.select()
            .where((Playlist.user == user) & (Playlist.comment == comment))
            .first()
        )
        status = "updated" if playlist is not None else "created"
        if playlist is None:
            playlist = Playlist.create(
                user=user,
                name=get_daily_mood_scene_playlist_name(user, key, playlist_day),
                comment=comment,
                public=False,
            )
        else:
            playlist.name = get_daily_mood_scene_playlist_name(user, key, playlist_day)
            playlist.public = False

        original_playlist_id = playlist.id
        playlist.tracks = ",".join(str(result["track"].id) for result in results)
        playlist.save()
        playlist = _dedupe_system_mood_scene_playlist(user, comment, playlist)
        if status == "created" and playlist.id != original_playlist_id:
            status = "updated"
    playlist = _dedupe_system_mood_scene_playlist(user, comment, playlist)
    if status == "created" and playlist.id != original_playlist_id:
        status = "updated"
    return _playlist_result(key, status, playlist, len(results))


def refresh_daily_mood_scene_playlists_for_user(
    user,
    limit: int = DEFAULT_MOOD_SCENE_DAILY_PLAYLIST_LIMIT,
    day: Optional[str] = None,
) -> Dict[str, object]:
    playlist_day = getRecommendationDay() if day is None else day
    summary: Dict[str, object] = {
        "day": playlist_day,
        "user": getattr(user, "name", ""),
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "track_count": 0,
        "results": [],
    }
    for scene_key in list_mood_scene_playlist_keys():
        try:
            result = create_or_update_daily_mood_scene_playlist_for_user(
                user,
                scene_key,
                limit=limit,
                day=playlist_day,
            )
        except Exception as exc:
            logger.exception(
                format_log_event(
                    "mood_scene_playlist",
                    "refresh_scene_failed",
                    user=getattr(user, "name", ""),
                    scene_key=scene_key,
                    day=playlist_day,
                    error_type=exc.__class__.__name__,
                )
            )
            summary["failed"] = int(summary["failed"]) + 1
            continue

        status = str(result.get("status") or "skipped")
        if status in ("created", "updated", "skipped"):
            summary[status] = int(summary[status]) + 1
        summary["track_count"] = int(summary["track_count"]) + int(
            result.get("track_count", 0) or 0
        )
        summary["results"].append(result)
    return summary


def refresh_daily_mood_scene_playlists(
    limit: int = DEFAULT_MOOD_SCENE_DAILY_PLAYLIST_LIMIT,
    day: Optional[str] = None,
    active_users_only: bool = True,
) -> Dict[str, object]:
    playlist_day = getRecommendationDay() if day is None else day
    users = _get_active_mood_scene_playlist_users() if active_users_only else list(User.select())
    summary: Dict[str, object] = {
        "day": playlist_day,
        "users": len(users),
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "track_count": 0,
        "results": [],
    }
    for user in users:
        user_result = refresh_daily_mood_scene_playlists_for_user(
            user,
            limit=limit,
            day=playlist_day,
        )
        for key in ("created", "updated", "skipped", "failed", "track_count"):
            summary[key] = int(summary[key]) + int(user_result.get(key, 0) or 0)
        summary["results"].append(user_result)
    return summary


def cleanup_old_mood_scene_playlists(
    retention_days: int = DEFAULT_MOOD_SCENE_PLAYLIST_RETENTION_DAYS,
    current_day: Optional[str] = None,
) -> Dict[str, int]:
    current_date = _parse_day(current_day or getRecommendationDay())
    if current_date is None:
        current_date = _parse_day(getRecommendationDay())
    retention_days = _positive_int(
        retention_days,
        DEFAULT_MOOD_SCENE_PLAYLIST_RETENTION_DAYS,
    )
    cutoff_date = current_date - timedelta(days=retention_days - 1)
    deleted = 0
    skipped = 0

    query = Playlist.select().where(system_mood_scene_playlist_where())
    for playlist in query:
        _, day = parse_mood_scene_playlist_comment(playlist.comment)
        playlist_date = _parse_day(day)
        if playlist_date is None:
            skipped += 1
            logger.warning(
                format_log_event(
                    "mood_scene_playlist",
                    "cleanup_skipped_invalid_comment",
                    playlist_id=playlist.id,
                    comment=playlist.comment,
                )
            )
            continue
        if playlist_date >= cutoff_date:
            continue
        playlist.delete_instance()
        deleted += 1
    return {"deleted": deleted, "skipped": skipped}


def save_mood_scene_playlist_copy_for_user(user, source_playlist):
    scene_key, day = parse_mood_scene_playlist_comment(
        getattr(source_playlist, "comment", None)
    )
    if scene_key is None or day is None:
        return None

    tracks = source_playlist.get_tracks()
    if not tracks:
        return None

    playlist = Playlist.create(
        user=user,
        name=get_saved_mood_scene_playlist_name(user, scene_key, day),
        comment=get_saved_mood_scene_playlist_comment(scene_key, day),
        public=False,
    )
    playlist.tracks = ",".join(str(track.id) for track in tracks)
    playlist.save()
    return playlist


def _playlist_result(
    scene_key: str,
    status: str,
    playlist,
    track_count: int,
    **extra,
) -> Dict[str, object]:
    result = {
        "scene_key": scene_key,
        "status": status,
        "playlist": playlist,
        "track_count": int(track_count or 0),
    }
    result.update(extra)
    return result


def _dedupe_system_mood_scene_playlist(user, comment: str, preferred_playlist):
    duplicates = list(
        Playlist.select()
        .where((Playlist.user == user) & (Playlist.comment == comment))
        .order_by(Playlist.created, Playlist.id)
    )
    if not duplicates:
        return preferred_playlist
    if len(duplicates) == 1:
        return duplicates[0]

    keeper = duplicates[0]
    if keeper.id != preferred_playlist.id:
        keeper.name = preferred_playlist.name
        keeper.tracks = preferred_playlist.tracks
        keeper.public = False
        keeper.save()

    for playlist in duplicates:
        if playlist.id == keeper.id:
            continue
        playlist.delete_instance()
    return keeper


def _get_active_mood_scene_playlist_users() -> List[User]:
    users_by_id = {}
    try:
        for user in User.select().join(User_Play_Activity).distinct():
            users_by_id[user.id] = user
    except OperationalError:
        pass

    for user in User.select().where(User.last_play.is_null(False)):
        users_by_id[user.id] = user
    return list(users_by_id.values())


def _parse_day(day: object):
    try:
        return datetime.strptime(str(day or ""), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, parsed)
