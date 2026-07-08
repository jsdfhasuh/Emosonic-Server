# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2013-2018 Alban 'spl0k' Féron
#
# Distributed under terms of the GNU AGPLv3 license.

import logging
import time
import uuid
from typing import Any, Iterable, List, Set

from flask import request

from ..db import (
    Artist,
    Playlist,
    StarredTrack,
    Track,
    User,
    User_Play_Activity,
    db,
    random,
)
from ..logging_utils import format_log_event
from ..mood_scene_playlist_service import get_system_mood_scene_playlist_display_name
from ..recommend import (
    buildRecommendationReasonMap,
    getLatestRecommendedPlaylist,
    getRecommendedPlaylistForDay,
    non_recommended_playlist_where,
)
from ..recommendation_feedback import (
    HOT_RECOMMENDED_SCOPE,
    get_recommendation_feedback_preferences,
    get_recommendation_feedback_state,
    set_recommendation_feedback,
    track_matches_negative_recommendation_feedback,
)
from . import get_entity, api_routing
from .exceptions import Forbidden, GenericError, MissingParameter

logger = logging.getLogger(__name__)


def _as_subsonic_playlist(playlist: object, user: object, tracks=None) -> dict:
    info = playlist.as_subsonic_playlist(user, tracks=tracks)
    mood_name = get_system_mood_scene_playlist_display_name(playlist)
    if mood_name:
        info["name"] = (
            mood_name
            if playlist.user.id == user.id
            else f"[{playlist.user.name}] {mood_name}"
        )
    return info


def _feedback_preference_log_counts(preferences: dict) -> dict:
    return {
        "disliked_song_count": len(preferences.get("disliked_song_ids", set())),
        "hidden_artist_count": len(preferences.get("hidden_artist_ids", set())),
        "hidden_album_count": len(preferences.get("hidden_album_ids", set())),
        "hidden_genre_count": len(preferences.get("hidden_genres", set())),
        "liked_more_count": len(preferences.get("liked_more_song_ids", set())),
    }


def _log_recommended_playlist_served(
    user,
    source: str,
    requested_count: int,
    returned_count: int,
    stats: dict,
) -> None:
    logger.info(
        format_log_event(
            "recommendation",
            "playlist_served",
            user=getattr(user, "name", ""),
            source=source,
            requested_count=requested_count,
            source_track_count=stats.get("source_track_count", 0),
            returned_count=returned_count,
            filtered_feedback_track_count=stats.get(
                "filtered_feedback_track_count",
                0,
            ),
            backfilled_track_count=stats.get("backfilled_track_count", 0),
            **_feedback_preference_log_counts(stats.get("preferences", {})),
        )
    )


def _log_recommendation_feedback_update(feedback) -> None:
    logger.info(
        format_log_event(
            "recommendation",
            "feedback_updated",
            user=getattr(getattr(feedback, "user", None), "name", ""),
            target_type=getattr(feedback, "target_type", ""),
            action=getattr(feedback, "action", ""),
            scope=getattr(feedback, "scope", ""),
            source=getattr(feedback, "source", ""),
            restored=bool(getattr(feedback, "deleted_at", None)),
        )
    )


def _get_recommended_count() -> int:
    try:
        count = int(request.values.get("count") or 50)
    except (TypeError, ValueError):
        raise GenericError("Invalid recommended playlist count")
    return max(0, min(count, 500))


def _get_json_body_value(name: str, default: Any = None) -> Any:
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload.get(name, default)
    return default


def _get_request_value(name: str, default: Any = None) -> Any:
    value = request.values.get(name)
    if value is not None:
        return value
    return _get_json_body_value(name, default)


def _collect_random_recommended_tracks(user, count: int) -> List[Track]:
    if count <= 0:
        return []
    preferences = get_recommendation_feedback_preferences(user, HOT_RECOMMENDED_SCOPE)
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


def _add_unique_recommended_tracks(
    tracks: List[Track],
    candidates: Iterable[Track],
    count: int,
    preferences: dict,
    seen_track_ids: Set[str],
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


def _get_user_listened_track_ids(user) -> Set[uuid.UUID]:
    return {
        activity.track_id
        for activity in User_Play_Activity.select(User_Play_Activity.track_id).where(
            User_Play_Activity.user == user
        )
    }


def _backfill_recommended_tracks(
    user,
    tracks: List[Track],
    seed_tracks: Iterable[Track],
    count: int,
    preferences: dict,
    seen_track_ids: Set[str],
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
        _add_unique_recommended_tracks(
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
        _add_unique_recommended_tracks(
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
    _add_unique_recommended_tracks(
        tracks,
        popular_query.order_by(Track.play_count.desc(), Track.id).limit(query_limit),
        count,
        preferences,
        seen_track_ids,
    )

    _add_unique_recommended_tracks(
        tracks,
        Track.select().order_by(random()).limit(query_limit),
        count,
        preferences,
        seen_track_ids,
    )


def _get_filtered_recommended_tracks(
    user,
    playlist_tracks: List[Track],
    count: int,
    stats: dict = None,
) -> List[Track]:
    preferences = get_recommendation_feedback_preferences(user, HOT_RECOMMENDED_SCOPE)
    recommended_tracks: List[Track] = []
    seen_track_ids: Set[str] = set()
    filtered_feedback_track_count = sum(
        1
        for track in playlist_tracks
        if track_matches_negative_recommendation_feedback(track, preferences)
    )
    _add_unique_recommended_tracks(
        recommended_tracks,
        playlist_tracks,
        count,
        preferences,
        seen_track_ids,
    )
    before_backfill_count = len(recommended_tracks)
    _backfill_recommended_tracks(
        user,
        recommended_tracks,
        playlist_tracks,
        count,
        preferences,
        seen_track_ids,
    )
    if stats is not None:
        stats.update(
            {
                "source_track_count": len(playlist_tracks),
                "filtered_feedback_track_count": filtered_feedback_track_count,
                "backfilled_track_count": max(
                    0,
                    len(recommended_tracks) - before_backfill_count,
                ),
                "preferences": preferences,
            }
        )
    return recommended_tracks


def _as_recommended_track_entries(user, client, tracks: List[Track]) -> List[dict]:
    reason_map = buildRecommendationReasonMap(user, tracks)
    entries = []
    for track in tracks:
        entry = track.as_subsonic_child(user, client)
        entry["recommendReason"] = reason_map.get(str(track.id), "")
        entries.append(entry)
    return entries


def _existing_artist_ids(candidate_ids: Iterable[str]) -> List[str]:
    parsed_ids = []
    for candidate_id in candidate_ids:
        candidate_id = str(candidate_id or "").strip()
        if not candidate_id:
            continue
        try:
            uuid.UUID(candidate_id)
        except ValueError:
            continue
        parsed_ids.append(candidate_id)

    if not parsed_ids:
        return []

    return sorted(
        str(artist.id)
        for artist in Artist.select(Artist.id).where(Artist.id.in_(parsed_ids))
    )


def _external_artist_names(candidate_ids: Iterable[str]) -> List[str]:
    names = []
    seen = set()
    for candidate_id in candidate_ids:
        candidate_id = str(candidate_id or "").strip()
        if not candidate_id:
            continue
        try:
            uuid.UUID(candidate_id)
        except ValueError:
            key = candidate_id.casefold()
            if key not in seen:
                seen.add(key)
                names.append(candidate_id)
    return sorted(names, key=str.casefold)


@api_routing("/getPlaylists")
def list_playlists():
    query = (
        Playlist.select()
        .where(
            (Playlist.user == request.user) | Playlist.public,
            non_recommended_playlist_where(),
        )
        .order_by(Playlist.name)
    )

    username = request.values.get("username")
    if username:
        if not request.user.admin:
            raise Forbidden()

        # get rather than join in the following query to raise an exception if the
        # requested user doesn't exist
        user = User.get(name=username)
        query = (
            Playlist.select()
            .where(
                Playlist.user == user,
                non_recommended_playlist_where(),
            )
            .order_by(Playlist.name)
        )
    temp = [_as_subsonic_playlist(p, request.user) for p in query]
    return request.formatter(
        "playlists",
        {"playlist": temp},
    )


@api_routing("/getPlaylist")
def show_playlist():
    res = get_entity(Playlist)
    if (
        isinstance(res, Playlist)
        and res.user != request.user
        and not res.public
        and not request.user.admin
    ):
        raise Forbidden()
    if res == "default" and request.user:
        trq = (
            StarredTrack.select(StarredTrack.starred)
            .join(Track)
            .where(StarredTrack.user == request.user)
            .order_by(-StarredTrack.date)
        )
        first_album = trq[0].starred.album if trq else None
        info = {
            "id": "default",
            "name": "my Starred Tracks",
            "owner": request.user.name,
            "public": False,
            "comment": "Tracks you have starred",
            "songCount": len(trq),
            "duration": sum(t.starred.duration for t in trq),
            "created": time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime()),
        }
        if first_album:
            info["coverArt"] = f"al-{first_album.id}"
        entry = [
            st.starred.as_subsonic_child(request.user, request.client) for st in trq
        ]
        info["entry"] = entry
        return request.formatter("playlist", info)
    info = _as_subsonic_playlist(res, request.user)
    info["entry"] = [
        t.as_subsonic_child(request.user, request.client) for t in res.get_tracks()
    ]
    return request.formatter("playlist", info)


@api_routing("/createPlaylist")
@db.atomic()
def create_playlist():
    playlist_id, name = map(request.values.get, ("playlistId", "name"))
    # songId actually doesn't seem to be required
    songs = request.values.getlist("songId")
    playlist_id = uuid.UUID(playlist_id) if playlist_id else None

    if playlist_id:
        playlist = Playlist[playlist_id]

        if playlist.user != request.user and not request.user.admin:
            raise Forbidden()

        playlist.clear()
        if name:
            playlist.name = name
    elif name:
        playlist = Playlist.create(user=request.user, name=name)
    else:
        raise MissingParameter("playlistId or name")

    for sid in songs:
        sid = uuid.UUID(sid)
        track = Track[sid]
        playlist.add(track)
    playlist.save()
    return request.formatter.empty


@api_routing("/deletePlaylist")
def delete_playlist():
    res = get_entity(Playlist)
    if res.user != request.user and not request.user.admin:
        raise Forbidden()

    res.delete_instance()
    return request.formatter.empty


@api_routing("/updatePlaylist")
def update_playlist():
    res = get_entity(Playlist, "playlistId")
    if res.user != request.user and not request.user.admin:
        raise Forbidden()

    playlist = res
    name, comment, public = map(request.values.get, ("name", "comment", "public"))
    to_add, to_remove = map(
        request.values.getlist, ("songIdToAdd", "songIndexToRemove")
    )

    if name:
        playlist.name = name
    if comment:
        playlist.comment = comment
    if public:
        playlist.public = public in (True, "True", "true", 1, "1")

    to_add = map(uuid.UUID, to_add)
    to_remove = map(int, to_remove)

    for sid in to_add:
        track = Track[sid]
        playlist.add(track)

    playlist.remove_at_indexes(to_remove)
    playlist.save()

    return request.formatter.empty


@api_routing("/getRecommendedPlaylists")
def get_recommended_playlists():
    user = request.user
    if not user:
        raise Forbidden()
    count = _get_recommended_count()
    recommended_playlist = getRecommendedPlaylistForDay(user)
    if recommended_playlist is None:
        recommended_playlist = getLatestRecommendedPlaylist(user)
    if recommended_playlist:
        stats = {}
        recommended_tracks = _get_filtered_recommended_tracks(
            user,
            recommended_playlist.get_tracks(),
            count,
            stats,
        )
        info = recommended_playlist.as_subsonic_playlist(
            request.user,
            tracks=recommended_tracks,
        )
        info["entry"] = _as_recommended_track_entries(
            request.user,
            request.client,
            recommended_tracks,
        )
        _log_recommended_playlist_served(
            user,
            "playlist",
            count,
            len(recommended_tracks),
            stats,
        )
        return request.formatter("playlist", info)
    else:
        # temp return a random playlist for the user if not exist
        trs = _collect_random_recommended_tracks(user, count)
        _log_recommended_playlist_served(
            user,
            "random",
            count,
            len(trs),
            {
                "preferences": get_recommendation_feedback_preferences(
                    user,
                    HOT_RECOMMENDED_SCOPE,
                ),
            },
        )
        info = {
            "id": "Recommended",
            "name": "Recommended Playlist",
            "owner": request.user.name,
            "public": False,
            "comment": "recommended playlist for you",
            "songCount": len(trs),
            "duration": sum(t.duration for t in trs),
            "created": time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime()),
        }
        first_track = trs[0] if trs else None
        if first_track:
            info["coverArt"] = f"al-{first_track.album.id}"
        info["entry"] = _as_recommended_track_entries(
            request.user,
            request.client,
            trs,
        )
        return request.formatter("playlist", info)


@api_routing("/getRecommendationFeedback")
def get_recommendation_feedback_api():
    user = request.user
    if not user:
        raise Forbidden()

    scope = str(_get_request_value("scope", HOT_RECOMMENDED_SCOPE) or "").strip()
    scope = scope or HOT_RECOMMENDED_SCOPE
    if scope != HOT_RECOMMENDED_SCOPE:
        raise GenericError("invalid recommendation feedback scope")

    disliked_song_ids, updated_at = get_recommendation_feedback_state(user, scope)
    preferences = get_recommendation_feedback_preferences(user, scope)
    return request.formatter(
        "recommendationFeedback",
        {
            "scope": scope,
            "dislikedSongIds": sorted(disliked_song_ids),
            "hiddenArtistIds": _existing_artist_ids(
                preferences["hidden_artist_ids"]
            ),
            "hiddenArtistNames": _external_artist_names(
                preferences["hidden_artist_ids"]
            ),
            "hiddenAlbumIds": sorted(preferences["hidden_album_ids"]),
            "hiddenGenres": sorted(preferences["hidden_genres"]),
            "likedMoreSongIds": sorted(preferences["liked_more_song_ids"]),
            "updatedAt": updated_at.isoformat() if updated_at else "",
        },
    )


@api_routing("/setRecommendationFeedback")
def set_recommendation_feedback_api():
    user = request.user
    if not user:
        raise Forbidden()

    song_id = (
        _get_request_value("id")
        or _get_request_value("targetId")
        or _get_request_value("target_id")
    )
    action = _get_request_value("action")
    scope = _get_request_value("scope", HOT_RECOMMENDED_SCOPE)
    reason = _get_request_value("reason", "user_dislike")
    source = _get_request_value("source", "api")
    target_type = (
        _get_request_value("targetType")
        or _get_request_value("target_type")
    )

    if not song_id:
        raise MissingParameter("id")
    if not action:
        raise MissingParameter("action")

    try:
        feedback = set_recommendation_feedback(
            user,
            song_id=song_id,
            action=action,
            scope=scope,
            reason=reason,
            source=source,
            target_type=target_type,
        )
    except ValueError as exc:
        raise GenericError(str(exc))

    _log_recommendation_feedback_update(feedback)
    return request.formatter(
        "recommendationFeedback",
        {
            "id": feedback.target_id,
            "targetType": feedback.target_type,
            "targetId": feedback.target_id,
            "target_id": feedback.target_id,
            "action": feedback.action,
            "scope": feedback.scope,
        },
    )
