import uuid

from ..db import AlbumArtist, ReviewTask, TrackArtist, now


def listMetadataReviewTasks(status=None):
    query = ReviewTask.select().order_by(ReviewTask.created.desc())
    if status:
        query = query.where(ReviewTask.status == status)
    return query


def getMetadataReviewTask(taskId):
    try:
        task_uuid = uuid.UUID(str(taskId))
    except ValueError as exc:
        raise ValueError("Invalid review task id") from exc

    task = ReviewTask.get_or_none(ReviewTask.id == task_uuid)
    if task is None:
        raise LookupError("Review task not found")
    return task


def _resolveMetadataReviewTask(task, nextStatus):
    if task.status != "pending":
        raise ValueError("Only pending review tasks can be updated")

    task.status = nextStatus
    task.updated = now()
    task.resolved_at = task.updated
    task.save()
    return task


def confirmMetadataReviewTask(task):
    return _resolveMetadataReviewTask(task, "confirmed")


def dismissMetadataReviewTask(task):
    return _resolveMetadataReviewTask(task, "dismissed")


def reopenMetadataReviewTask(task):
    if task.status not in ("confirmed", "dismissed"):
        raise ValueError("Only confirmed or dismissed review tasks can be reopened")

    task.status = "pending"
    task.updated = now()
    task.resolved_at = None
    task.save()
    return task


def getTaskRelatedArtists(task):
    if task.is_artist_task():
        artist = task.artist
        return [artist] if artist is not None else []
    if task.is_track_task():
        track = task.track
        if track is None:
            return []
        artists = []
        seen_artist_ids = set()
        for artist in [track.artist, track.album.artist if track.album is not None else None]:
            if artist is None or artist.id in seen_artist_ids:
                continue
            seen_artist_ids.add(artist.id)
            artists.append(artist)
        return artists

    artists = []
    seen_artist_ids = set()
    track_rows = list(task.album.tracks)
    related_album_artists = [relation.artist_id for relation in task.album.album_artists.order_by(AlbumArtist.position, AlbumArtist.id)]
    related_track_artists = []
    for track in track_rows:
        related_track_artists.extend(
            relation.artist_id for relation in track.track_artists.order_by(TrackArtist.position, TrackArtist.id)
        )

    for artist in [task.album.artist] + [track.artist for track in track_rows] + related_album_artists + related_track_artists:
        if artist.id in seen_artist_ids:
            continue
        seen_artist_ids.add(artist.id)
        artists.append(artist)
    return artists
