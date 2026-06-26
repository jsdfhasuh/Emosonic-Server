from datetime import datetime
import unicodedata
import uuid
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .db import UserRecommendationFeedback, now

HOT_RECOMMENDED_SCOPE = "hot_recommended"
RECOMMENDATION_FEEDBACK_ACTION_DISLIKE = "dislike"
RECOMMENDATION_FEEDBACK_ACTION_RESTORE = "restore"
RECOMMENDATION_FEEDBACK_ACTION_DISLIKE_SONG = "dislike_song"
RECOMMENDATION_FEEDBACK_ACTION_RESTORE_SONG = "restore_song"
RECOMMENDATION_FEEDBACK_ACTION_HIDE_ARTIST = "hide_artist"
RECOMMENDATION_FEEDBACK_ACTION_RESTORE_ARTIST = "restore_artist"
RECOMMENDATION_FEEDBACK_ACTION_HIDE_ALBUM = "hide_album"
RECOMMENDATION_FEEDBACK_ACTION_RESTORE_ALBUM = "restore_album"
RECOMMENDATION_FEEDBACK_ACTION_LIKE_MORE = "like_more"
RECOMMENDATION_FEEDBACK_ACTION_NOT_THIS_STYLE = "not_this_style"
RECOMMENDATION_FEEDBACK_ACTION_RESTORE_STYLE = "restore_style"
RECOMMENDATION_TARGET_TYPE_SONG = "song"
RECOMMENDATION_TARGET_TYPE_ARTIST = "artist"
RECOMMENDATION_TARGET_TYPE_ALBUM = "album"
RECOMMENDATION_TARGET_TYPE_GENRE = "genre"
MAX_RECOMMENDATION_FEEDBACK_TARGET_ID_LENGTH = 128
VALID_RECOMMENDATION_FEEDBACK_ACTIONS = {
    RECOMMENDATION_FEEDBACK_ACTION_DISLIKE,
    RECOMMENDATION_FEEDBACK_ACTION_RESTORE,
    RECOMMENDATION_FEEDBACK_ACTION_DISLIKE_SONG,
    RECOMMENDATION_FEEDBACK_ACTION_RESTORE_SONG,
    RECOMMENDATION_FEEDBACK_ACTION_HIDE_ARTIST,
    RECOMMENDATION_FEEDBACK_ACTION_RESTORE_ARTIST,
    RECOMMENDATION_FEEDBACK_ACTION_HIDE_ALBUM,
    RECOMMENDATION_FEEDBACK_ACTION_RESTORE_ALBUM,
    RECOMMENDATION_FEEDBACK_ACTION_LIKE_MORE,
    RECOMMENDATION_FEEDBACK_ACTION_NOT_THIS_STYLE,
    RECOMMENDATION_FEEDBACK_ACTION_RESTORE_STYLE,
}
RESTORE_RECOMMENDATION_FEEDBACK_ACTIONS = {
    RECOMMENDATION_FEEDBACK_ACTION_RESTORE,
    RECOMMENDATION_FEEDBACK_ACTION_RESTORE_SONG,
    RECOMMENDATION_FEEDBACK_ACTION_RESTORE_ARTIST,
    RECOMMENDATION_FEEDBACK_ACTION_RESTORE_ALBUM,
    RECOMMENDATION_FEEDBACK_ACTION_RESTORE_STYLE,
}
DISLIKE_SONG_ACTIONS = {
    RECOMMENDATION_FEEDBACK_ACTION_DISLIKE,
    RECOMMENDATION_FEEDBACK_ACTION_DISLIKE_SONG,
}
TARGET_TYPE_BY_ACTION = {
    RECOMMENDATION_FEEDBACK_ACTION_DISLIKE: RECOMMENDATION_TARGET_TYPE_SONG,
    RECOMMENDATION_FEEDBACK_ACTION_RESTORE: RECOMMENDATION_TARGET_TYPE_SONG,
    RECOMMENDATION_FEEDBACK_ACTION_DISLIKE_SONG: RECOMMENDATION_TARGET_TYPE_SONG,
    RECOMMENDATION_FEEDBACK_ACTION_RESTORE_SONG: RECOMMENDATION_TARGET_TYPE_SONG,
    RECOMMENDATION_FEEDBACK_ACTION_LIKE_MORE: RECOMMENDATION_TARGET_TYPE_SONG,
    RECOMMENDATION_FEEDBACK_ACTION_HIDE_ARTIST: RECOMMENDATION_TARGET_TYPE_ARTIST,
    RECOMMENDATION_FEEDBACK_ACTION_RESTORE_ARTIST: RECOMMENDATION_TARGET_TYPE_ARTIST,
    RECOMMENDATION_FEEDBACK_ACTION_HIDE_ALBUM: RECOMMENDATION_TARGET_TYPE_ALBUM,
    RECOMMENDATION_FEEDBACK_ACTION_RESTORE_ALBUM: RECOMMENDATION_TARGET_TYPE_ALBUM,
    RECOMMENDATION_FEEDBACK_ACTION_NOT_THIS_STYLE: RECOMMENDATION_TARGET_TYPE_GENRE,
    RECOMMENDATION_FEEDBACK_ACTION_RESTORE_STYLE: RECOMMENDATION_TARGET_TYPE_GENRE,
}


def _normalize_target_type(action: str, target_type: Optional[str]) -> str:
    expected_target_type = TARGET_TYPE_BY_ACTION[action]
    target_type = str(target_type or "").strip().lower()
    if target_type:
        if target_type not in {
            RECOMMENDATION_TARGET_TYPE_SONG,
            RECOMMENDATION_TARGET_TYPE_ARTIST,
            RECOMMENDATION_TARGET_TYPE_ALBUM,
            RECOMMENDATION_TARGET_TYPE_GENRE,
        }:
            raise ValueError("invalid recommendation feedback target type")
        if action == RECOMMENDATION_FEEDBACK_ACTION_RESTORE:
            return target_type
        if target_type != expected_target_type:
            raise ValueError(
                "recommendation feedback target type does not match action"
            )
        return target_type
    return expected_target_type


def _feedback_is_restore_action(action: str) -> bool:
    return action in RESTORE_RECOMMENDATION_FEEDBACK_ACTIONS


def _normalize_target_id(target_id: str, target_type: str) -> str:
    if target_type == RECOMMENDATION_TARGET_TYPE_GENRE:
        return target_id.casefold()
    return target_id


def _target_is_uuid(target_id: str) -> bool:
    try:
        uuid.UUID(str(target_id))
    except (TypeError, ValueError):
        return False
    return True


def _normalize_artist_feedback_name(name: object) -> str:
    text = unicodedata.normalize("NFKC", str(name or ""))
    return " ".join(text.strip().casefold().split())


def _hidden_artist_name_targets(preferences: Dict[str, Set[str]]) -> Set[str]:
    names = set()
    for target_id in preferences.get("hidden_artist_ids", set()):
        target_id = str(target_id or "").strip()
        if target_id and not _target_is_uuid(target_id):
            normalized = _normalize_artist_feedback_name(target_id)
            if normalized:
                names.add(normalized)
    return names


def _track_artist_name_variants(track) -> Set[str]:
    artist = getattr(track, "artist", None)
    if not artist:
        return set()

    names = set()
    raw_name = getattr(artist, "name", None)
    if raw_name:
        names.add(raw_name)
    get_artist_name = getattr(artist, "get_artist_name", None)
    if get_artist_name:
        resolved_name = get_artist_name()
        if resolved_name:
            names.add(resolved_name)

    return {
        normalized
        for normalized in (_normalize_artist_feedback_name(name) for name in names)
        if normalized
    }


def _active_feedback_query(user, scope: str = HOT_RECOMMENDED_SCOPE):
    return UserRecommendationFeedback.select().where(
        UserRecommendationFeedback.user == user,
        UserRecommendationFeedback.scope == scope,
        UserRecommendationFeedback.deleted_at.is_null(True),
    )


def set_recommendation_feedback(
    user,
    song_id: str,
    action: str,
    scope: str = HOT_RECOMMENDED_SCOPE,
    reason: str = "user_dislike",
    source: str = "api",
    target_type: Optional[str] = None,
) -> UserRecommendationFeedback:
    target_id = str(song_id or "").strip()
    action = str(action or "").strip().lower()
    scope = str(scope or "").strip() or HOT_RECOMMENDED_SCOPE
    reason = str(reason or "").strip() or "user_dislike"
    source = str(source or "").strip() or "api"

    if not target_id:
        raise ValueError("recommendation feedback id is required")
    if len(target_id) > MAX_RECOMMENDATION_FEEDBACK_TARGET_ID_LENGTH:
        raise ValueError("recommendation feedback id is too long")
    if action not in VALID_RECOMMENDATION_FEEDBACK_ACTIONS:
        raise ValueError("invalid recommendation feedback action")
    if scope != HOT_RECOMMENDED_SCOPE:
        raise ValueError("invalid recommendation feedback scope")
    if len(reason) > 64:
        raise ValueError("recommendation feedback reason is too long")
    if len(source) > 64:
        raise ValueError("recommendation feedback source is too long")

    normalized_target_type = _normalize_target_type(action, target_type)
    target_id = _normalize_target_id(target_id, normalized_target_type)
    current_time = now()
    feedback, _ = UserRecommendationFeedback.get_or_create(
        user=user,
        target_type=normalized_target_type,
        target_id=target_id,
        scope=scope,
        defaults={
            "song_id": target_id,
            "action": action,
            "reason": reason,
            "source": source,
            "created_at": current_time,
            "updated_at": current_time,
            "deleted_at": None
            if not _feedback_is_restore_action(action)
            else current_time,
        },
    )
    feedback.song_id = target_id
    feedback.target_type = normalized_target_type
    feedback.target_id = target_id
    feedback.action = action
    feedback.reason = reason
    feedback.source = source
    feedback.updated_at = current_time
    feedback.deleted_at = (
        None if not _feedback_is_restore_action(action) else current_time
    )
    feedback.save()
    return feedback


def get_recommendation_feedback_preferences(
    user,
    scope: str = HOT_RECOMMENDED_SCOPE,
) -> Dict[str, Set[str]]:
    preferences = {
        "disliked_song_ids": set(),
        "hidden_artist_ids": set(),
        "hidden_album_ids": set(),
        "hidden_genres": set(),
        "liked_more_song_ids": set(),
    }
    if user is None:
        return preferences

    for feedback in _active_feedback_query(user, scope):
        target_type = (
            getattr(feedback, "target_type", None)
            or RECOMMENDATION_TARGET_TYPE_SONG
        )
        target_id = str(getattr(feedback, "target_id", None) or feedback.song_id)
        if not target_id:
            continue
        if (
            target_type == RECOMMENDATION_TARGET_TYPE_SONG
            and feedback.action in DISLIKE_SONG_ACTIONS
        ):
            preferences["disliked_song_ids"].add(target_id)
        elif (
            target_type == RECOMMENDATION_TARGET_TYPE_ARTIST
            and feedback.action == RECOMMENDATION_FEEDBACK_ACTION_HIDE_ARTIST
        ):
            preferences["hidden_artist_ids"].add(target_id)
        elif (
            target_type == RECOMMENDATION_TARGET_TYPE_ALBUM
            and feedback.action == RECOMMENDATION_FEEDBACK_ACTION_HIDE_ALBUM
        ):
            preferences["hidden_album_ids"].add(target_id)
        elif (
            target_type == RECOMMENDATION_TARGET_TYPE_GENRE
            and feedback.action == RECOMMENDATION_FEEDBACK_ACTION_NOT_THIS_STYLE
        ):
            preferences["hidden_genres"].add(target_id.casefold())
        elif (
            target_type == RECOMMENDATION_TARGET_TYPE_SONG
            and feedback.action == RECOMMENDATION_FEEDBACK_ACTION_LIKE_MORE
        ):
            preferences["liked_more_song_ids"].add(target_id)

    return preferences


def track_matches_negative_recommendation_feedback(
    track,
    preferences: Dict[str, Set[str]],
) -> bool:
    if str(track.id) in preferences.get("disliked_song_ids", set()):
        return True
    if track.artist_id and str(track.artist_id) in preferences.get(
        "hidden_artist_ids",
        set(),
    ):
        return True
    hidden_artist_names = _hidden_artist_name_targets(preferences)
    if hidden_artist_names and _track_artist_name_variants(track) & hidden_artist_names:
        return True
    if track.album_id and str(track.album_id) in preferences.get(
        "hidden_album_ids",
        set(),
    ):
        return True
    genre = str(track.genre or "").strip().casefold()
    if genre and genre in preferences.get("hidden_genres", set()):
        return True
    return False


def get_disliked_recommended_song_ids(
    user,
    scope: str = HOT_RECOMMENDED_SCOPE,
) -> Set[str]:
    return get_recommendation_feedback_preferences(user, scope)[
        "disliked_song_ids"
    ]


def get_recommendation_feedback_state(
    user,
    scope: str = HOT_RECOMMENDED_SCOPE,
) -> Tuple[Set[str], Optional[datetime]]:
    disliked_song_ids = get_disliked_recommended_song_ids(user, scope=scope)
    latest_feedback = (
        UserRecommendationFeedback.select(UserRecommendationFeedback.updated_at)
        .where(
            UserRecommendationFeedback.user == user,
            UserRecommendationFeedback.scope == scope,
        )
        .order_by(UserRecommendationFeedback.updated_at.desc())
        .first()
    )
    updated_at = latest_feedback.updated_at if latest_feedback else None
    return disliked_song_ids, updated_at


def filter_disliked_recommended_tracks(
    user,
    tracks: Iterable[object],
    scope: str = HOT_RECOMMENDED_SCOPE,
) -> List[object]:
    preferences = get_recommendation_feedback_preferences(user, scope=scope)
    return [
        track
        for track in tracks
        if not track_matches_negative_recommendation_feedback(track, preferences)
    ]
