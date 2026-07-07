import hashlib
import logging
import os
import re
import time

from datetime import datetime, timedelta
from peewee import fn
from typing import Dict, Mapping, Optional, Sequence

from .config import DefaultConfig
from .db import Playlist, Track, TrackMetadata, User, User_Play_Activity
from .recommendation_feedback import (
    get_recommendation_feedback_preferences,
    track_matches_negative_recommendation_feedback,
)
from .track_metadata_quality import is_high_quality_track_metadata
from .user_listening_profile import build_metadata_preference_profile_from_track_counts
from .tool import write_dict_to_json


logger = logging.getLogger(__name__)

RECOMMENDED_PLAYLIST_COMMENT = "recommended"
LEGACY_RECOMMENDED_PLAYLIST_COMMENT = "recommend"
RECOMMENDED_PLAYLIST_COMMENTS = (
    RECOMMENDED_PLAYLIST_COMMENT,
    LEGACY_RECOMMENDED_PLAYLIST_COMMENT,
)
RECOMMENDED_PLAYLIST_DAY_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
SAFE_ARCHIVE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._@-]+")
DEFAULT_RECOMMEND_PLAYLIST_RETENTION_DAYS = 5
RECOMMENDATION_SCORE_WEIGHTS = {
    "genre_match": 0.24,
    "artist_affinity": 0.20,
    "album_affinity": 0.08,
    "freshness": 0.08,
    "popularity": 0.08,
    "not_played": 0.08,
    "feedback": 0.04,
    "mood_match": 0.08,
    "scene_match": 0.05,
    "tag_match": 0.05,
    "energy_match": 0.02,
}


def getRecommendationDay(currentTime=None) -> str:
    currentTime = time.localtime() if currentTime is None else currentTime
    return time.strftime("%Y-%m-%d", currentTime)


def getRecommendPlaylistName(user, day=None) -> str:
    recommendationDay = getRecommendationDay() if day is None else day
    return f"{user.name}'s {recommendationDay} recommend playlist"


def recommended_playlist_where():
    return Playlist.comment.in_(RECOMMENDED_PLAYLIST_COMMENTS)


def non_recommended_playlist_where():
    return Playlist.comment.is_null(True) | Playlist.comment.not_in(
        RECOMMENDED_PLAYLIST_COMMENTS
    )


def getRecommendedPlaylistForDay(user, day=None):
    return (
        Playlist.select()
        .where((Playlist.user == user) & (Playlist.name == getRecommendPlaylistName(user, day)))
        .first()
    )


def getLatestRecommendedPlaylist(user):
    return (
        Playlist.select()
        .where(
            (Playlist.user == user)
            & (Playlist.comment.in_(RECOMMENDED_PLAYLIST_COMMENTS))
        )
        .order_by(Playlist.created.desc())
        .first()
    )


def _getUsersWithPlayActivity():
    return User.select().join(User_Play_Activity).distinct()


def _getUserTrackPlayCounts(user):
    query = (
        User_Play_Activity.select(
            User_Play_Activity.track_id.alias("track_id"),
            fn.COUNT(User_Play_Activity.id).alias("play_count"),
        )
        .where(User_Play_Activity.user == user)
        .group_by(User_Play_Activity.track_id)
    )
    return {row.track_id: row.play_count for row in query}


def _getPreferenceCounts(trackPlayCounts):
    genreCounts = {}
    artistCounts = {}
    albumCounts = {}
    if not trackPlayCounts:
        return genreCounts, artistCounts, albumCounts

    listenedTracks = Track.select(Track.id, Track.genre, Track.artist, Track.album).where(
        Track.id.in_(list(trackPlayCounts.keys()))
    )
    for track in listenedTracks:
        playCount = trackPlayCounts.get(track.id, 0)
        if track.genre:
            genreCounts[track.genre] = genreCounts.get(track.genre, 0) + playCount
        artistCounts[track.artist_id] = artistCounts.get(track.artist_id, 0) + playCount
        albumCounts[track.album_id] = albumCounts.get(track.album_id, 0) + playCount

    return genreCounts, artistCounts, albumCounts


def _getTopGenresAndArtists(trackPlayCounts):
    genreCounts, artistCounts, _ = _getPreferenceCounts(trackPlayCounts)
    if not trackPlayCounts:
        return [], []

    topGenres = [
        genre for genre, _ in sorted(genreCounts.items(), key=lambda item: item[1], reverse=True)[:3]
    ]
    topArtists = [
        artistId for artistId, _ in sorted(artistCounts.items(), key=lambda item: item[1], reverse=True)[:3]
    ]
    return topGenres, topArtists


def _artist_display_name(track) -> str:
    if not track or not track.artist:
        return ""
    return track.artist.get_artist_name() or track.artist.name or ""


def _buildRecommendationReasonProfile(user) -> Dict[str, object]:
    trackPlayCounts = _getUserTrackPlayCounts(user)
    topGenres, topArtists = _getTopGenresAndArtists(trackPlayCounts)
    preferences = get_recommendation_feedback_preferences(user)
    likedMoreProfile = _buildLikedMoreProfile(preferences)
    return {
        "listenedTrackIds": {str(trackId) for trackId in trackPlayCounts},
        "topGenres": set(topGenres),
        "topArtistIds": set(topArtists),
        "likedMoreGenres": likedMoreProfile["genres"],
        "likedMoreArtistIds": likedMoreProfile["artist_ids"],
    }


def _buildTrackMetadataReason(metadata) -> str:
    if not metadata:
        return ""

    moods = metadata.get_moods()
    scenes = metadata.get_scenes()
    tags = metadata.get_tags()
    if moods and scenes:
        return (
            "Because it matches a "
            + " / ".join(moods[:2])
            + " mood and suits "
            + " / ".join(scenes[:2])
            + " listening."
        )
    if moods:
        return "Because it carries a " + " / ".join(moods[:2]) + " mood."
    if scenes:
        return "Because it suits " + " / ".join(scenes[:2]) + " listening."
    if metadata.summary:
        summary = str(metadata.summary).strip()
        if summary:
            return f"Because {summary[0].lower()}{summary[1:]}"
    if tags:
        return "Because its tags add " + " / ".join(tags[:3]) + " variety."
    return ""


def getRecommendationReason(
    user,
    track,
    profile: Optional[Mapping[str, object]] = None,
) -> str:
    profile = profile or _buildRecommendationReasonProfile(user)
    listenedTrackIds = profile.get("listenedTrackIds") or set()
    topGenres = profile.get("topGenres") or set()
    topArtistIds = profile.get("topArtistIds") or set()
    likedMoreGenres = profile.get("likedMoreGenres") or set()
    likedMoreArtistIds = profile.get("likedMoreArtistIds") or set()
    metadataByTrackId = profile.get("trackMetadataById")

    trackId = str(track.id)
    genre = str(track.genre or "").strip()
    artistName = _artist_display_name(track).strip()

    if (
        (genre and genre in likedMoreGenres)
        or (track.artist_id and track.artist_id in likedMoreArtistIds)
    ):
        return "Because you asked for more songs like a previous recommendation."

    if genre and genre in topGenres:
        return f"Because you often listen to {genre}, and this track matches that style."

    if track.artist_id and track.artist_id in topArtistIds and artistName:
        return f"Because you often listen to {artistName}, and this is another track from that artist."

    if metadataByTrackId is None:
        metadata = TrackMetadata.get_or_none(TrackMetadata.track == track)
    else:
        metadata = metadataByTrackId.get(trackId)
    if is_high_quality_track_metadata(metadata):
        metadataReason = _buildTrackMetadataReason(metadata)
        if metadataReason:
            return metadataReason

    if trackId not in listenedTrackIds and int(track.play_count or 0) > 0:
        return "Because it is a popular library track you have not played yet."

    if genre:
        return f"Because it adds {genre} variety alongside your recent listening."

    if artistName:
        return f"Because it broadens your recommendations with another track from {artistName}."

    return "Because it adds variety beyond your recent listening."


def buildRecommendationReasonMap(
    user,
    tracks: Sequence[object],
) -> Dict[str, str]:
    profile = _buildRecommendationReasonProfile(user)
    trackIds = [track.id for track in tracks]
    profile["trackMetadataById"] = {
        str(metadata.track_id): metadata
        for metadata in TrackMetadata.select().where(TrackMetadata.track.in_(trackIds))
    } if trackIds else {}
    return {
        str(track.id): getRecommendationReason(user, track, profile)
        for track in tracks
    }


def _collectTracks(query, limit, excludedTrackIds, preferences=None):
    if limit <= 0:
        return []

    preferences = preferences or {}
    selectedTracks = []
    for track in query:
        trackId = str(track.id)
        if trackId in excludedTrackIds or track_matches_negative_recommendation_feedback(
            track,
            preferences,
        ):
            continue
        selectedTracks.append(track)
        excludedTrackIds.add(trackId)
        if len(selectedTracks) >= limit:
            break
    return selectedTracks


def _normalized_count(count: int, max_count: int) -> float:
    if max_count <= 0:
        return 0.0
    return min(1.0, float(count or 0) / float(max_count))


def _buildLikedMoreProfile(preferences) -> Dict[str, set]:
    likedMoreSongIds = preferences.get("liked_more_song_ids", set())
    profile = {"genres": set(), "artist_ids": set(), "album_ids": set()}
    if not likedMoreSongIds:
        return profile

    for track in Track.select().where(Track.id.in_(list(likedMoreSongIds))):
        if track.genre:
            profile["genres"].add(track.genre)
        if track.artist_id:
            profile["artist_ids"].add(track.artist_id)
        if track.album_id:
            profile["album_ids"].add(track.album_id)
    return profile


def _recommendationFeedbackScore(track, likedMoreProfile: Mapping[str, set]) -> float:
    if track.artist_id and track.artist_id in likedMoreProfile.get("artist_ids", set()):
        return 1.0
    if track.genre and track.genre in likedMoreProfile.get("genres", set()):
        return 0.8
    if track.album_id and track.album_id in likedMoreProfile.get("album_ids", set()):
        return 0.6
    return 0.0


def _buildMetadataPreferenceProfile(trackPlayCounts) -> Dict[str, object]:
    return build_metadata_preference_profile_from_track_counts(trackPlayCounts)


def _metadataListScore(values, counts: Mapping[str, int]) -> float:
    if not values or not counts:
        return 0.0
    maxCount = max(counts.values()) if counts else 0
    if maxCount <= 0:
        return 0.0
    matched = sum(counts.get(str(value).strip().casefold(), 0) for value in values)
    return min(1.0, float(matched) / float(maxCount))


def _metadataEnergyScore(metadata, averageEnergy) -> float:
    if not metadata or metadata.energy is None or averageEnergy is None:
        return 0.0
    distance = abs(float(metadata.energy) - float(averageEnergy))
    return max(0.0, 1.0 - min(1.0, distance / 100.0))


def _trackMetadataScore(metadata, metadataProfile: Mapping[str, object]) -> Dict[str, float]:
    if not is_high_quality_track_metadata(metadata):
        return {
            "mood_match": 0.0,
            "scene_match": 0.0,
            "tag_match": 0.0,
            "energy_match": 0.0,
        }
    return {
        "mood_match": _metadataListScore(
            metadata.get_moods(),
            metadataProfile.get("mood_counts", {}),
        ),
        "scene_match": _metadataListScore(
            metadata.get_scenes(),
            metadataProfile.get("scene_counts", {}),
        ),
        "tag_match": _metadataListScore(
            metadata.get_tags(),
            metadataProfile.get("tag_counts", {}),
        ),
        "energy_match": _metadataEnergyScore(
            metadata,
            metadataProfile.get("average_energy"),
        ),
    }


def _scoreRecommendationCandidate(
    track,
    listenedTrackIds,
    genreCounts,
    artistCounts,
    albumCounts,
    maxPopularity: int,
    likedMoreProfile: Mapping[str, set],
    metadata=None,
    metadataProfile: Optional[Mapping[str, object]] = None,
) -> float:
    maxGenreCount = max(genreCounts.values()) if genreCounts else 0
    maxArtistCount = max(artistCounts.values()) if artistCounts else 0
    maxAlbumCount = max(albumCounts.values()) if albumCounts else 0

    genreScore = _normalized_count(genreCounts.get(track.genre, 0), maxGenreCount)
    artistScore = _normalized_count(
        artistCounts.get(track.artist_id, 0),
        maxArtistCount,
    )
    albumScore = _normalized_count(albumCounts.get(track.album_id, 0), maxAlbumCount)
    freshnessScore = 1.0 if getattr(track, "last_play", None) is None else 0.5
    popularityScore = _normalized_count(int(track.play_count or 0), maxPopularity)
    notPlayedScore = 1.0 if track.id not in listenedTrackIds else 0.0
    feedbackScore = _recommendationFeedbackScore(track, likedMoreProfile)
    metadataScores = _trackMetadataScore(metadata, metadataProfile or {})

    return (
        genreScore * RECOMMENDATION_SCORE_WEIGHTS["genre_match"]
        + artistScore * RECOMMENDATION_SCORE_WEIGHTS["artist_affinity"]
        + albumScore * RECOMMENDATION_SCORE_WEIGHTS["album_affinity"]
        + freshnessScore * RECOMMENDATION_SCORE_WEIGHTS["freshness"]
        + popularityScore * RECOMMENDATION_SCORE_WEIGHTS["popularity"]
        + notPlayedScore * RECOMMENDATION_SCORE_WEIGHTS["not_played"]
        + feedbackScore * RECOMMENDATION_SCORE_WEIGHTS["feedback"]
        + metadataScores["mood_match"] * RECOMMENDATION_SCORE_WEIGHTS["mood_match"]
        + metadataScores["scene_match"] * RECOMMENDATION_SCORE_WEIGHTS["scene_match"]
        + metadataScores["tag_match"] * RECOMMENDATION_SCORE_WEIGHTS["tag_match"]
        + metadataScores["energy_match"] * RECOMMENDATION_SCORE_WEIGHTS["energy_match"]
    )


def _dailyRecommendationJitter(track, recommendationDay) -> float:
    if not recommendationDay:
        return 0.0

    digest = hashlib.sha1(
        f"{recommendationDay}:{track.id}".encode("utf-8")
    ).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF * 0.000001


def _buildRecommendedTracks(
    trackPlayCounts,
    numSongs,
    excludedTrackIds=None,
    preferences=None,
    recommendationDay=None,
):
    if numSongs <= 0:
        return []

    listenedTrackIds = set(trackPlayCounts.keys())
    selectedTrackIds = {str(trackId) for trackId in listenedTrackIds}
    if excludedTrackIds:
        selectedTrackIds.update(str(trackId) for trackId in excludedTrackIds)
    preferences = preferences or {}
    genreCounts, artistCounts, albumCounts = _getPreferenceCounts(trackPlayCounts)
    likedMoreProfile = _buildLikedMoreProfile(preferences)
    metadataProfile = _buildMetadataPreferenceProfile(trackPlayCounts)

    candidateQuery = Track.select()
    if listenedTrackIds:
        candidateQuery = candidateQuery.where(Track.id.not_in(list(listenedTrackIds)))

    candidates = []
    for track in candidateQuery:
        trackId = str(track.id)
        if trackId in selectedTrackIds:
            continue
        if track_matches_negative_recommendation_feedback(track, preferences):
            continue
        candidates.append(track)

    maxPopularity = max((int(track.play_count or 0) for track in candidates), default=0)
    candidateMetadata = {
        metadata.track_id: metadata
        for metadata in TrackMetadata.select().where(
            TrackMetadata.track.in_([track.id for track in candidates])
        )
    } if candidates else {}
    scoredCandidates = [
        (
            _scoreRecommendationCandidate(
                track,
                listenedTrackIds,
                genreCounts,
                artistCounts,
                albumCounts,
                maxPopularity,
                likedMoreProfile,
                metadata=candidateMetadata.get(track.id),
                metadataProfile=metadataProfile,
            ),
            int(track.play_count or 0),
            _dailyRecommendationJitter(track, recommendationDay),
            str(track.title or "").casefold(),
            str(track.id),
            track,
        )
        for track in candidates
    ]
    scoredCandidates.sort(
        key=lambda item: (-item[0], -item[1], -item[2], item[3], item[4])
    )
    return [track for _, _, _, _, _, track in scoredCandidates[:numSongs]]


def _setPlaylistTracks(playlist, tracks):
    uniqueTrackIds = []
    seenTrackIds = set()
    for track in tracks:
        trackId = str(track.id)
        if trackId in seenTrackIds:
            continue
        seenTrackIds.add(trackId)
        uniqueTrackIds.append(trackId)

    playlist.tracks = ",".join(uniqueTrackIds)


def _get_recommend_playlist_retention_days(config: Optional[object] = None) -> int:
    daemonConfig = getattr(config, "DAEMON", None)
    retentionDays = DEFAULT_RECOMMEND_PLAYLIST_RETENTION_DAYS
    if isinstance(daemonConfig, dict):
        retentionDays = daemonConfig.get(
            "recommend_playlist_retention_days", retentionDays
        )

    try:
        retentionDays = int(retentionDays)
    except (TypeError, ValueError):
        retentionDays = DEFAULT_RECOMMEND_PLAYLIST_RETENTION_DAYS

    return max(1, retentionDays)


def _recommend_playlist_archive_enabled(config: Optional[object] = None) -> bool:
    daemonConfig = getattr(config, "DAEMON", None)
    if isinstance(daemonConfig, dict):
        return bool(daemonConfig.get("recommend_playlist_archive_enabled", True))
    return True


def _get_recommend_playlist_archive_root(config: Optional[object] = None) -> str:
    webappConfig = getattr(config, "WEBAPP", None)
    cacheDir = DefaultConfig.WEBAPP["cache_dir"]
    if isinstance(webappConfig, dict) and webappConfig.get("cache_dir"):
        cacheDir = webappConfig["cache_dir"]
    return os.path.join(cacheDir, "recommend-playlists")


def _parse_recommendation_date(dayText):
    try:
        return datetime.strptime(dayText, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _get_recommendation_date_for_playlist(playlist):
    match = RECOMMENDED_PLAYLIST_DAY_RE.search(playlist.name or "")
    if match is not None:
        parsedDate = _parse_recommendation_date(match.group(1))
        if parsedDate is not None:
            return parsedDate

    created = getattr(playlist, "created", None)
    if created is not None and hasattr(created, "date"):
        return created.date()

    return _parse_recommendation_date(getRecommendationDay())


def _serialize_recommend_playlist_track(track, reason: str = ""):
    return {
        "id": str(track.id),
        "title": track.title,
        "artist": track.artist.name if track.artist else "",
        "album": track.album.name if track.album else "",
        "duration": track.duration,
        "path": track.path,
        "recommend_reason": reason,
    }


def _safe_archive_segment(value, fallback) -> str:
    segment = SAFE_ARCHIVE_SEGMENT_RE.sub("_", str(value or "")).strip("._")
    if not segment:
        segment = str(fallback)
    return segment[:128]


def _build_recommend_playlist_archive_path(playlist, archiveRoot):
    recommendationDate = _get_recommendation_date_for_playlist(playlist)
    recommendationDay = recommendationDate.isoformat()
    userDir = os.path.join(
        archiveRoot,
        _safe_archive_segment(playlist.user.name, playlist.user.id),
    )
    archivePath = os.path.join(userDir, f"{recommendationDay}.json")
    if os.path.exists(archivePath):
        archivePath = os.path.join(userDir, f"{recommendationDay}_{playlist.id}.json")
    return archivePath, recommendationDay


def _archive_recommended_playlist(playlist, archiveRoot):
    tracks = playlist.get_tracks()
    reasonMap = buildRecommendationReasonMap(playlist.user, tracks)
    archivePath, recommendationDay = _build_recommend_playlist_archive_path(
        playlist, archiveRoot
    )
    payload = {
        "playlist_id": str(playlist.id),
        "user": playlist.user.name,
        "name": playlist.name,
        "comment": playlist.comment,
        "created": playlist.created.isoformat() if playlist.created else None,
        "archived_at": datetime.utcnow().isoformat(),
        "recommendation_day": recommendationDay,
        "track_ids": [str(track.id) for track in tracks],
        "tracks": [
            _serialize_recommend_playlist_track(
                track,
                reasonMap.get(str(track.id), ""),
            )
            for track in tracks
        ],
    }
    write_dict_to_json(payload, archivePath)
    return archivePath


def _archive_old_recommended_playlists_for_user(
    user, currentDay, config: Optional[object] = None
) -> int:
    if not _recommend_playlist_archive_enabled(config):
        return 0

    currentDate = _parse_recommendation_date(currentDay) or _parse_recommendation_date(
        getRecommendationDay()
    )
    retentionDays = _get_recommend_playlist_retention_days(config)
    cutoffDate = currentDate - timedelta(days=retentionDays - 1)
    archiveRoot = _get_recommend_playlist_archive_root(config)
    archivedCount = 0

    query = (
        Playlist.select()
        .where((Playlist.user == user) & recommended_playlist_where())
        .order_by(Playlist.created.asc(), Playlist.id.asc())
    )
    for playlist in query:
        playlistDate = _get_recommendation_date_for_playlist(playlist)
        if playlistDate is None or playlistDate >= cutoffDate:
            continue

        try:
            archivePath = _archive_recommended_playlist(playlist, archiveRoot)
        except Exception:
            logger.exception(
                "Failed to archive recommended playlist %s for %s",
                playlist.id,
                user.name,
            )
            continue

        playlist.delete_instance()
        archivedCount += 1
        logger.info(
            "Archived recommended playlist %s for %s to %s",
            playlist.name,
            user.name,
            archivePath,
        )

    return archivedCount


def _createRecommendPlaylistForUser(
    user, numSongs=50, day=None, config: Optional[object] = None
):
    if user is None:
        return None, False
    if User.get_or_none(User.id == user.id) is None:
        return None, False

    recommendationDay = getRecommendationDay() if day is None else day
    _archive_old_recommended_playlists_for_user(
        user, recommendationDay, config=config
    )
    existingPlaylist = getRecommendedPlaylistForDay(user, recommendationDay)
    if existingPlaylist is not None:
        return existingPlaylist, False

    trackPlayCounts = _getUserTrackPlayCounts(user)
    if not trackPlayCounts:
        return None, False

    preferences = get_recommendation_feedback_preferences(user)
    recommendedTracks = _buildRecommendedTracks(
        trackPlayCounts,
        numSongs,
        excludedTrackIds=preferences["disliked_song_ids"],
        preferences=preferences,
        recommendationDay=recommendationDay,
    )
    if not recommendedTracks:
        return None, False

    playlist = Playlist.create(
        user=user,
        name=getRecommendPlaylistName(user, recommendationDay),
        comment=RECOMMENDED_PLAYLIST_COMMENT,
    )
    _setPlaylistTracks(playlist, recommendedTracks)
    playlist.save()
    logger.info(
        "Created recommended playlist %s for %s with %d tracks",
        playlist.name,
        user.name,
        len(recommendedTracks),
    )
    return playlist, True


def refreshDailyRecommendPlaylists(
    num_songs=50, day=None, config: Optional[object] = None
) -> int:
    createdCount = 0
    recommendationDay = getRecommendationDay() if day is None else day
    for user in _getUsersWithPlayActivity():
        try:
            _, wasCreated = _createRecommendPlaylistForUser(
                user,
                numSongs=num_songs,
                day=recommendationDay,
                config=config,
            )
            if wasCreated:
                createdCount += 1
        except Exception:
            logger.exception(
                "Failed to create recommended playlist for user %s on %s",
                user.name,
                recommendationDay,
            )
    return createdCount


def create_recommend_playlist(
    num_songs=50, user=None, day=None, config: Optional[object] = None
) -> int:
    if user is not None:
        _, wasCreated = _createRecommendPlaylistForUser(
            user,
            numSongs=num_songs,
            day=day,
            config=config,
        )
        return 1 if wasCreated else 0
    return refreshDailyRecommendPlaylists(
        num_songs=num_songs, day=day, config=config
    )
