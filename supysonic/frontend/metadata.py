import json
import logging
from functools import wraps

from flask import (
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from peewee import fn

from ..config import get_current_config
from ..daemon.client import DaemonClient
from ..daemon.exceptions import DaemonUnavailableError
from ..db import (
    Album,
    AlbumArtist,
    Artist,
    ClientPrefs,
    Track,
    TrackArtist,
    TrackMetadata,
    User,
    db,
)
from ..lastfm import LastFm
from ..listenbrainz import ListenBrainz
from ..logging_utils import format_log_event
from ..managers.user import UserManager
from ..scanner_func.scanner_review_tasks import (
    ABNORMAL_TRACK_METADATA_REASON,
    CONFLICTING_TRACK_METADATA_REASON,
    DEFAULT_TRACK_METADATA_CONFIDENCE_THRESHOLD,
    LOW_CONFIDENCE_TRACK_METADATA_REASON,
    getTrackMetadataReviewIssues,
)
from .metadata_actions import assignPrimaryArtist, resolvePrimaryArtist, updateArtistMetadata
from .metadata_nfo import writeReviewAlbumNfo
from .metadata_review import (
    confirmMetadataReviewTask,
    dismissMetadataReviewTask,
    getMetadataReviewTask,
    getTaskRelatedArtists,
    listMetadataReviewTasks,
    reopenMetadataReviewTask,
)

from . import admin_only, frontend

logger = logging.getLogger(__name__)

TRACK_METADATA_CONFIDENCE_THRESHOLD = DEFAULT_TRACK_METADATA_CONFIDENCE_THRESHOLD
TRACK_METADATA_REVIEW_REASONS = {
    LOW_CONFIDENCE_TRACK_METADATA_REASON,
    ABNORMAL_TRACK_METADATA_REASON,
    CONFLICTING_TRACK_METADATA_REASON,
}


def logMetadataEvent(level, event, **fields):
    user = getattr(getattr(request, "user", None), "name", "-") or "-"
    context = {
        "request_id": getattr(g, "supysonic_request_id", "-"),
        "user": user,
        "path": request.path,
    }
    context.update(fields)
    logger.log(level, format_log_event("metadata", event, **context))


def formatDurationLabel(totalSeconds):
    hours, remainder = divmod(int(totalSeconds or 0), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def syncAlbumArtists(album, previousArtistId=None):
    if previousArtistId and previousArtistId != album.artist_id:
        AlbumArtist.delete().where(
            AlbumArtist.album_id == album,
            AlbumArtist.artist_id == previousArtistId,
        ).execute()

    primary_relation, _ = AlbumArtist.get_or_create(
        album_id=album,
        artist_id=album.artist,
        defaults={"position": 1},
    )
    if primary_relation.position != 1:
        primary_relation.position = 1
        primary_relation.save()


def syncTrackArtists(track, previousArtistId=None):
    if previousArtistId and previousArtistId != track.artist_id:
        TrackArtist.delete().where(
            TrackArtist.track_id == track,
            TrackArtist.artist_id == previousArtistId,
        ).execute()

    primary_relation, _ = TrackArtist.get_or_create(
        track_id=track,
        artist_id=track.artist,
        defaults={"position": 1},
    )
    if primary_relation.position != 1:
        primary_relation.position = 1
        primary_relation.save()


def getReviewSection(defaultSection="album"):
    section = request.args.get("section", defaultSection)
    if section not in ("album", "tracks", "artists"):
        return defaultSection
    return section


def getReviewRedirectResponse(task):
    if request.args.get("redirect") != "1":
        return None
    return redirect(
        url_for(
            "frontend.metadata_review_task",
            task_id=task.id,
            section=getReviewSection(),
        )
    )


def getMetadataCoverArtUrl(entityId):
    return f"/rest/getCoverArt?id={entityId}&v=1.15.0&c=web"


def parseAlbumInfoJson(value):
    if not value:
        return {}
    try:
        album_info = json.loads(value)
    except (TypeError, ValueError):
        return {}
    if isinstance(album_info, dict):
        return album_info
    return {}


def getOptionalText(value):
    if value is None:
        return ""
    return str(value).strip()


def normalizeStringList(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def formatProviderName(provider):
    provider_names = {
        "musicbrainz": "MusicBrainz",
        "discogs": "Discogs",
    }
    provider_key = str(provider).strip().lower()
    return provider_names.get(provider_key, provider_key.replace("_", " ").title())


def buildAlbumEnrichmentData(album=None, snapshot=None):
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    snapshot_enrichment = snapshot.get("enrichment") or {}
    if not isinstance(snapshot_enrichment, dict):
        snapshot_enrichment = {}

    album_info = parseAlbumInfoJson(getattr(album, "album_info_json", None))
    providers = normalizeStringList(
        album_info.get("providers_used") or snapshot_enrichment.get("providers_used")
    )
    styles = normalizeStringList(
        album_info.get("styles") or snapshot_enrichment.get("styles")
    )
    release_date = getOptionalText(
        getattr(album, "release_date", None) or snapshot.get("release_date")
    )
    release_type = getOptionalText(
        getattr(album, "release_type", None) or snapshot.get("release_type")
    )
    primary_genre = getOptionalText(
        album_info.get("primary_genre") or snapshot_enrichment.get("primary_genre")
    )
    musicbrainz_id = getOptionalText(
        album_info.get("musicbrainz_id") or snapshot_enrichment.get("musicbrainz_id")
    )
    discogs_id = getOptionalText(
        album_info.get("discogs_id") or snapshot_enrichment.get("discogs_id")
    )
    providers_label = ", ".join(formatProviderName(provider) for provider in providers)
    styles_label = ", ".join(styles)
    has_enrichment = any(
        [
            release_date,
            release_type,
            styles,
            providers,
            primary_genre,
            musicbrainz_id,
            discogs_id,
        ]
    )
    inbox_lines = []
    if providers_label:
        inbox_lines.append(f"External enrichment: {providers_label}")
    elif has_enrichment:
        inbox_lines.append("External enrichment")
    if release_type:
        inbox_lines.append(f"Release type: {release_type}")
    if primary_genre:
        inbox_lines.append(f"Primary genre: {primary_genre}")

    return {
        "has_enrichment": has_enrichment,
        "release_date": release_date,
        "release_type": release_type,
        "styles": styles,
        "styles_label": styles_label,
        "providers_used": providers,
        "providers_label": providers_label,
        "primary_genre": primary_genre,
        "musicbrainz_id": musicbrainz_id,
        "discogs_id": discogs_id,
        "inbox_lines": inbox_lines,
    }


def buildTrackMetadataReviewData(track=None, snapshot=None):
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    snapshot_metadata = snapshot.get("metadata") or {}
    if not isinstance(snapshot_metadata, dict):
        snapshot_metadata = {}

    metadata = None
    if track is not None:
        metadata = TrackMetadata.get_or_none(TrackMetadata.track == track)

    moods = (
        metadata.get_moods()
        if metadata is not None
        else normalizeStringList(snapshot_metadata.get("mood"))
    )
    scenes = (
        metadata.get_scenes()
        if metadata is not None
        else normalizeStringList(snapshot_metadata.get("scene"))
    )
    tags = (
        metadata.get_tags()
        if metadata is not None
        else normalizeStringList(snapshot_metadata.get("tags"))
    )
    confidence = (
        metadata.confidence
        if metadata is not None
        else snapshot_metadata.get("confidence")
    )
    try:
        confidence_value = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence_value = None

    provider = getOptionalText(
        getattr(metadata, "provider", None) or snapshot_metadata.get("provider")
    )
    model = getOptionalText(
        getattr(metadata, "model", None) or snapshot_metadata.get("model")
    )
    language = getOptionalText(
        getattr(metadata, "language", None) or snapshot_metadata.get("language")
    )
    summary = getOptionalText(
        getattr(metadata, "summary", None) or snapshot_metadata.get("summary")
    )
    energy = (
        getattr(metadata, "energy", None)
        if metadata is not None
        else snapshot_metadata.get("energy")
    )
    valence = (
        getattr(metadata, "valence", None)
        if metadata is not None
        else snapshot_metadata.get("valence")
    )
    danceability = (
        getattr(metadata, "danceability", None)
        if metadata is not None
        else snapshot_metadata.get("danceability")
    )

    inbox_lines = []
    if confidence_value is not None:
        inbox_lines.append(f"Confidence: {confidence_value:.2f}")
    if tags:
        inbox_lines.append(f"Tags: {', '.join(tags[:5])}")
    if moods:
        inbox_lines.append(f"Mood: {', '.join(moods[:5])}")
    if scenes:
        inbox_lines.append(f"Scene: {', '.join(scenes[:5])}")

    return {
        "has_metadata": metadata is not None or bool(snapshot_metadata),
        "language": language,
        "moods": moods,
        "moods_label": ", ".join(moods),
        "scenes": scenes,
        "scenes_label": ", ".join(scenes),
        "tags": tags,
        "tags_label": ", ".join(tags),
        "summary": summary,
        "energy": energy,
        "valence": valence,
        "danceability": danceability,
        "confidence": confidence_value,
        "provider": provider,
        "model": model,
        "inbox_lines": inbox_lines,
    }


def getReviewArtistImageUrl(artist, artistInfo):
    for key in ("largeImageUrl", "mediumImageUrl", "smallImageUrl"):
        imageUrl = artistInfo.get(key) or ""
        if imageUrl.startswith(("http://", "https://", "/rest/", "/static/")):
            return imageUrl
    return getMetadataCoverArtUrl(f"ar-{artist.id}")


def hasDedicatedArtistImage(artistInfo):
    return any(
        artistInfo.get(key)
        for key in ("largeImageUrl", "mediumImageUrl", "smallImageUrl")
    )


def hasAlbumCoverFallback(artist):
    if artist is None:
        return False
    return Album.select().where(Album.artist == artist).exists()


def buildMetadataArtistCardData(artist):
    return {
        "id": str(artist.id),
        "name": artist.name,
        "image": getMetadataCoverArtUrl(f"ar-{artist.id}"),
    }


def getCurrentTrackMetadataIssueCodes(track):
    if track is None:
        return []
    metadata = TrackMetadata.get_or_none(TrackMetadata.track == track)
    return getTrackMetadataReviewIssues(metadata, TRACK_METADATA_CONFIDENCE_THRESHOLD)


def getTaskIssueCodes(task):
    snapshot = json.loads(task.snapshot_json or "{}")
    issue_codes = snapshot.get("issues") or []
    if issue_codes:
        return issue_codes

    if task.is_artist_task():
        if task.reason == "missing_image":
            return ["missing_image"]
        return []

    if task.is_track_task():
        issue_codes = getCurrentTrackMetadataIssueCodes(task.track)
        if issue_codes:
            return issue_codes
        if task.reason == LOW_CONFIDENCE_TRACK_METADATA_REASON:
            return [task.reason]
        return []

    issue_codes = []
    album = task.album
    if album is not None:
        if not album.year:
            issue_codes.append("missing_year")
        album_primary_id = resolvePrimaryArtist(album.artist).id
        if any(resolvePrimaryArtist(track.artist).id != album_primary_id for track in album.tracks):
            issue_codes.append("track_artist_mapping_needs_review")
    if issue_codes:
        return issue_codes

    if task.reason == "missing_year":
        return ["missing_year"]
    return []


def getInboxTaskPrefix(task):
    if task.is_artist_task():
        return "Artist metadata review"
    if task.is_track_task():
        return "Track metadata review"
    album_reason_map = {
        "new_album": "New album metadata review",
    }
    return album_reason_map.get(task.reason, "Album metadata review")


def getInboxTaskDescription(task):
    created_at = task.created.strftime("%Y-%m-%d %H:%M:%S")
    issues = getTaskIssueLabels(task)
    prefix = getInboxTaskPrefix(task)
    if len(issues) == 1:
        return f"Current issue: {issues[0]}. {prefix} created at {created_at}."
    if len(issues) > 1:
        return f"Current issues: {', '.join(issues)}. {prefix} created at {created_at}."
    return f"{prefix} created at {created_at}."


def getTaskIssueLabels(task):
    issue_codes = getTaskIssueCodes(task)
    return [getIssueLabel(issue) for issue in issue_codes]


def getIssueLabel(issueCode):
    issue_map = {
        "missing_year": "Missing release year",
        "track_artist_mapping_needs_review": "Track artist mapping needs review",
        "missing_image": "Missing artist image",
        LOW_CONFIDENCE_TRACK_METADATA_REASON: "Low metadata confidence",
        ABNORMAL_TRACK_METADATA_REASON: "Abnormal metadata result",
        CONFLICTING_TRACK_METADATA_REASON: "Conflicting metadata result",
    }
    return issue_map.get(issueCode, issueCode.replace("_", " "))


def getCurrentTaskIssueCodes(task):
    if task.is_artist_task():
        artist = task.artist
        if artist is None:
            return []
        artist_info = artist.get_info()
        if not hasDedicatedArtistImage(artist_info):
            return ["missing_image"]
        return []

    if task.is_track_task():
        return getCurrentTrackMetadataIssueCodes(task.track)

    issue_codes = []
    album = task.album
    if album is not None:
        if not album.year:
            issue_codes.append("missing_year")
        album_primary_id = resolvePrimaryArtist(album.artist).id
        if any(resolvePrimaryArtist(track.artist).id != album_primary_id for track in album.tracks):
            issue_codes.append("track_artist_mapping_needs_review")
    return issue_codes


def getTrackedTaskIssueCodes(task):
    snapshot = json.loads(task.snapshot_json or "{}")
    issue_codes = snapshot.get("issues") or []
    if issue_codes:
        return issue_codes

    if task.is_artist_task():
        if task.reason == "missing_image":
            return ["missing_image"]
        return getCurrentTaskIssueCodes(task)

    if task.is_track_task():
        if task.reason in TRACK_METADATA_REVIEW_REASONS:
            return [task.reason]
        return getCurrentTaskIssueCodes(task)

    if task.reason == "missing_year":
        return ["missing_year"]
    return getCurrentTaskIssueCodes(task)


def buildTaskIssueStatus(task):
    tracked_issue_codes = getTrackedTaskIssueCodes(task)
    current_issue_codes = set(getCurrentTaskIssueCodes(task))
    issue_status = [
        {
            "code": issue_code,
            "label": getIssueLabel(issue_code),
            "resolved": issue_code not in current_issue_codes,
        }
        for issue_code in tracked_issue_codes
    ]
    return {
        "issue_status": issue_status,
        "ready_to_confirm": len(current_issue_codes) == 0,
    }


def getAlbumInboxTaskPriority(task_data):
    issue_codes = task_data.get("issue_codes", [])
    if len(issue_codes) > 1:
        return 0
    if "missing_year" in issue_codes:
        return 1
    if "track_artist_mapping_needs_review" in issue_codes:
        return 2
    if issue_codes:
        return 3
    return 4


def getTaskDisplayIssues(task, tracks=None):
    issues = getTaskIssueLabels(task)
    if issues:
        return issues
    if task.is_artist_task():
        return ["Artist metadata review requested"]
    if task.is_track_task():
        return ["Track metadata review requested"]
    return ["Metadata review requested for new album"]


def buildInboxTaskData(task):
    snapshot = json.loads(task.snapshot_json or "{}")
    if task.is_artist_task():
        artist = task.artist
        artist_name = snapshot.get("artist_name") or (
            artist.get_artist_name() if artist is not None else "Unknown artist"
        )
        artist_id = getattr(artist, "id", task.entity_id)
        artist_info = artist.get_info() if artist is not None else {}
        is_cover_art_fallback = (
            not hasDedicatedArtistImage(artist_info)
        ) and hasAlbumCoverFallback(artist)
        issue_codes = getTaskIssueCodes(task)
        return {
            "id": str(task.id),
            "entity_type": "artist",
            "title": artist_name,
            "subtitle": task.reason.replace("_", " "),
            "description": getInboxTaskDescription(task),
            "status": task.status,
            "created": task.created,
            "expires_at": task.expires_at,
            "issue_codes": issue_codes,
            "cover_art_url": getMetadataCoverArtUrl(f"ar-{artist_id}"),
            "is_cover_art_fallback": is_cover_art_fallback,
        }

    if task.is_track_task():
        track = task.track
        issue_codes = getTaskIssueCodes(task)
        track_title = snapshot.get("track_title") or (
            track.title if track is not None else "Unknown track"
        )
        artist_name = snapshot.get("artist_name") or (
            track.artist.get_artist_name()
            if track is not None and track.artist is not None
            else ""
        )
        album_name = snapshot.get("album_name") or (
            track.album.name if track is not None and track.album is not None else ""
        )
        subtitle_parts = [value for value in (artist_name, album_name) if value]
        track_metadata = buildTrackMetadataReviewData(track=track, snapshot=snapshot)
        if track_metadata["confidence"] is not None:
            subtitle_parts.append(f"confidence {track_metadata['confidence']:.2f}")

        cover_art_id = str(task.entity_id)
        if track is not None:
            if track.has_art:
                cover_art_id = str(track.id)
            elif track.album_id is not None:
                cover_art_id = f"al-{track.album_id}"
        elif snapshot.get("album_id"):
            cover_art_id = f"al-{snapshot['album_id']}"

        return {
            "id": str(task.id),
            "entity_type": "track",
            "title": track_title,
            "subtitle": " · ".join(subtitle_parts) or task.reason.replace("_", " "),
            "description": getInboxTaskDescription(task),
            "status": task.status,
            "created": task.created,
            "expires_at": task.expires_at,
            "issue_codes": issue_codes,
            "cover_art_url": getMetadataCoverArtUrl(cover_art_id),
            "track_metadata": track_metadata,
        }

    album = task.album
    issue_codes = getTaskIssueCodes(task)
    if album is None:
        return {
            "id": str(task.id),
            "entity_type": "album",
            "title": snapshot.get("album_name") or "Unknown album",
            "subtitle": snapshot.get("artist_name") or "Album no longer exists",
            "description": getInboxTaskDescription(task),
            "status": task.status,
            "created": task.created,
            "expires_at": task.expires_at,
            "issue_codes": issue_codes,
            "cover_art_url": getMetadataCoverArtUrl(f"al-{task.entity_id}"),
            "album_enrichment": buildAlbumEnrichmentData(snapshot=snapshot),
        }

    year = snapshot.get("year") or album.year
    subtitle = f"{snapshot.get('artist_name') or album.artist.get_artist_name()}"
    if year:
        subtitle += f" · {year}"
    subtitle += f" · {snapshot.get('track_count', album.tracks.count())} tracks"
    return {
        "id": str(task.id),
        "entity_type": "album",
        "title": snapshot.get("album_name") or album.name,
        "subtitle": subtitle,
        "description": getInboxTaskDescription(task),
        "status": task.status,
        "created": task.created,
        "expires_at": task.expires_at,
        "issue_codes": issue_codes,
        "cover_art_url": getMetadataCoverArtUrl(f"al-{album.id}"),
        "album_enrichment": buildAlbumEnrichmentData(album=album, snapshot=snapshot),
    }


def buildArtistReviewCard(artist):
    artist_info = artist.get_info()
    return {
        "id": str(artist.id),
        "display_name": artist.get_artist_name(),
        "name": artist.name,
        "biography": artist_info.get("biography", ""),
        "image_url": getReviewArtistImageUrl(artist, artist_info),
        "is_cover_art_fallback": (
            not hasDedicatedArtistImage(artist_info)
        ) and hasAlbumCoverFallback(artist),
        "can_remove": False,
    }


def buildAlbumReviewArtistPanelPayload(task):
    related_artists = getTaskRelatedArtists(task)
    review_artist_cards = [buildArtistReviewCard(artist) for artist in related_artists]
    for review_artist_card in review_artist_cards:
        review_artist_card["can_remove"] = review_artist_card["id"] != str(task.album.artist_id)
    return {
        "review_artist_cards": review_artist_cards,
        "related_artist_count": len(related_artists),
        "artists_panel_html": render_template(
            "partials/metadata-review-artists-panel.html",
            reviewTask=task,
            reviewArtistCards=review_artist_cards,
            reviewIsEditable=(task.status == "pending"),
        ),
    }


def filterSupersededInboxTasks(tasks):
    artist_ids_with_missing_image = {
        str(task.entity_id)
        for task in tasks
        if task.is_artist_task() and task.status == "pending" and task.reason == "missing_image"
    }
    filtered_tasks = []
    for task in tasks:
        if (
            task.is_artist_task()
            and task.status == "pending"
            and task.reason == "new_artist"
            and str(task.entity_id) in artist_ids_with_missing_image
        ):
            continue
        filtered_tasks.append(task)
    return filtered_tasks


def ensurePendingReviewTask(task):
    if task.status != "pending":
        raise ValueError("Only pending review tasks can be updated")


def getArtistMetadataRequestData():
    if request.is_json:
        return request.get_json(silent=True) or {}, None

    artistPhoto = request.files.get("artist_photo")
    if artistPhoto and not artistPhoto.filename:
        artistPhoto = None
    return request.form.to_dict(), artistPhoto


@frontend.route("/metadata")
@admin_only
def metadata():
    activeTab = request.args.get("tab", "inbox")
    if activeTab not in ("inbox", "artists", "albums"):
        activeTab = "inbox"

    inbox_tasks = []
    album_inbox_tasks = []
    track_inbox_tasks = []
    artist_inbox_tasks = []
    inbox_summary = None
    selected_status = request.args.get("status", "pending")
    if activeTab == "inbox":
        query_status = None if selected_status == "all" else selected_status
        raw_tasks = filterSupersededInboxTasks(list(listMetadataReviewTasks(status=query_status)))
        for task in raw_tasks:
            inbox_tasks.append(buildInboxTaskData(task))

        album_inbox_tasks = [task for task in inbox_tasks if task["entity_type"] == "album"]
        track_inbox_tasks = [task for task in inbox_tasks if task["entity_type"] == "track"]
        artist_inbox_tasks = [task for task in inbox_tasks if task["entity_type"] == "artist"]
        album_inbox_tasks.sort(key=lambda task: (getAlbumInboxTaskPriority(task), task["created"]))
        track_inbox_tasks.sort(key=lambda task: task["created"])
        artist_inbox_tasks.sort(key=lambda task: task["created"])

        inbox_summary = {
            "pending": listMetadataReviewTasks(status="pending").count(),
            "confirmed": listMetadataReviewTasks(status="confirmed").count(),
            "dismissed": listMetadataReviewTasks(status="dismissed").count(),
            "all": listMetadataReviewTasks().count(),
        }

    return render_template(
        "metadata-workspace.html",
        activeTab=activeTab,
        inboxTasks=inbox_tasks,
        albumInboxTasks=album_inbox_tasks,
        trackInboxTasks=track_inbox_tasks,
        artistInboxTasks=artist_inbox_tasks,
        inboxSummary=inbox_summary,
        selectedInboxStatus=selected_status,
    )


@frontend.route("/metadata/review-tasks/<task_id>")
@admin_only
def metadata_review_task(task_id):
    try:
        task = getMetadataReviewTask(task_id)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}, 400
    except LookupError as exc:
        return {"status": "error", "message": str(exc)}, 404

    active_review_tab = getReviewSection()
    if task.is_artist_task():
        artist = task.artist
        if artist is None:
            return {"status": "error", "message": "Review task artist not found"}, 404

        review_artist_cards = [buildArtistReviewCard(artist)]
        issue_summary = buildTaskIssueStatus(task)
        review_summary = {
            "status": task.status,
            "created": task.created,
            "expires_at": task.expires_at,
            "track_count": 0,
            "related_artist_count": 1,
            "total_duration": formatDurationLabel(0),
            "cover_art_url": getMetadataCoverArtUrl(f"ar-{artist.id}"),
            "issues": getTaskDisplayIssues(task),
            "issue_status": issue_summary["issue_status"],
            "ready_to_confirm": issue_summary["ready_to_confirm"],
            "is_cover_art_fallback": review_artist_cards[0]["is_cover_art_fallback"],
        }
        return render_template(
            "metadata-review-artist-task.html",
            reviewTask=task,
            reviewArtist=artist,
            reviewArtists=[artist],
            reviewArtistCards=review_artist_cards,
            reviewSummary=review_summary,
            reviewIsEditable=(task.status == "pending"),
        )

    if task.is_track_task():
        track = task.track
        if track is None:
            return {"status": "error", "message": "Review task track not found"}, 404

        snapshot = json.loads(task.snapshot_json or "{}")
        issue_summary = buildTaskIssueStatus(task)
        track_metadata = buildTrackMetadataReviewData(track=track, snapshot=snapshot)
        cover_art_id = str(track.id) if track.has_art else f"al-{track.album_id}"
        review_summary = {
            "status": task.status,
            "created": task.created,
            "expires_at": task.expires_at,
            "duration": formatDurationLabel(track.duration),
            "cover_art_url": getMetadataCoverArtUrl(cover_art_id),
            "issues": getTaskDisplayIssues(task),
            "issue_status": issue_summary["issue_status"],
            "ready_to_confirm": issue_summary["ready_to_confirm"],
            "track_metadata": track_metadata,
        }
        return render_template(
            "metadata-review-track-task.html",
            reviewTask=task,
            reviewTrack=track,
            reviewSummary=review_summary,
            reviewIsEditable=(task.status == "pending"),
        )

    album = task.album
    if album is None:
        return {"status": "error", "message": "Review task album not found"}, 404
    tracks = list(album.tracks.order_by(Track.disc, Track.number, Track.title))
    artist_panel_payload = buildAlbumReviewArtistPanelPayload(task)
    related_artists = getTaskRelatedArtists(task)
    review_artist_cards = artist_panel_payload["review_artist_cards"]

    issue_summary = buildTaskIssueStatus(task)
    review_summary = {
        "status": task.status,
        "created": task.created,
        "expires_at": task.expires_at,
        "track_count": len(tracks),
        "related_artist_count": artist_panel_payload["related_artist_count"],
        "total_duration": formatDurationLabel(sum(track.duration for track in tracks)),
        "cover_art_url": getMetadataCoverArtUrl(f"al-{album.id}"),
        "issues": getTaskDisplayIssues(task, tracks=tracks),
        "issue_status": issue_summary["issue_status"],
        "ready_to_confirm": issue_summary["ready_to_confirm"],
        "album_enrichment": buildAlbumEnrichmentData(
            album=album,
            snapshot=json.loads(task.snapshot_json or "{}"),
        ),
    }

    return render_template(
        "metadata-review-task.html",
        reviewTask=task,
        reviewAlbum=album,
        reviewTracks=tracks,
        reviewArtists=related_artists,
        reviewArtistCards=review_artist_cards,
        reviewSummary=review_summary,
        activeReviewTab=active_review_tab,
        reviewIsEditable=(task.status == "pending"),
    )


@frontend.route("/metadata/artist-suggestions")
@admin_only
def metadata_artist_suggestions():
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return {"status": "success", "artists": []}

    normalized_query = query.lower()
    artist_rows = list(
        Artist.select()
        .where(Artist.real_artist.is_null(), fn.Lower(Artist.name).contains(normalized_query))
        .order_by(fn.Lower(Artist.name))
        .limit(20)
    )
    artist_names = sorted(
        [artist.name for artist in artist_rows],
        key=lambda name: (not name.lower().startswith(normalized_query), name.lower()),
    )
    return {"status": "success", "artists": artist_names[:10]}


@frontend.route("/metadata/review-tasks/<task_id>/album", methods=["POST"])
@admin_only
def update_metadata_review_task_album(task_id):
    task = None
    try:
        task = getMetadataReviewTask(task_id)
        ensurePendingReviewTask(task)
        if not task.is_album_task():
            raise ValueError("Album updates are only available for album review tasks")
    except ValueError as exc:
        logMetadataEvent(logging.WARNING, "review_album_update", result="bad_request", task_id=task_id)
        return {"status": "error", "message": str(exc)}, 400
    except LookupError as exc:
        logMetadataEvent(logging.WARNING, "review_album_update", result="not_found", task_id=task_id)
        return {"status": "error", "message": str(exc)}, 404
    raw_data = request.get_json(silent=True) or {}
    album = task.album
    previous_artist_id = album.artist_id
    changed_fields = []
    resolved_artist_id = None

    if raw_data.get("name") and raw_data.get("name") != album.name:
        changed_fields.append("name")
    album.name = raw_data.get("name") or album.name
    if raw_data.get("year") and raw_data.get("year") != album.year:
        changed_fields.append("year")
    album.year = raw_data.get("year") or album.year
    artist_name = raw_data.get("artist_name", "").strip()
    if artist_name:
        album_artist, _ = Artist.get_or_create(name=artist_name)
        album.artist = resolvePrimaryArtist(album_artist)
        resolved_artist_id = album.artist.id
        if previous_artist_id != album.artist_id:
            changed_fields.append("artist")
    album.save()
    syncAlbumArtists(album, previousArtistId=previous_artist_id)

    logMetadataEvent(
        logging.INFO,
        "review_album_update",
        result="success",
        task_id=task.id,
        album_id=album.id,
        changed_fields=changed_fields,
        resolved_artist_id=resolved_artist_id,
    )

    issue_summary = buildTaskIssueStatus(task)
    artist_panel_payload = buildAlbumReviewArtistPanelPayload(task)
    return {"status": "success", "message": "album updated", **issue_summary, **artist_panel_payload}


@frontend.route("/metadata/review-tasks/<task_id>/tracks", methods=["POST"])
@admin_only
def update_metadata_review_task_tracks(task_id):
    task = None
    try:
        task = getMetadataReviewTask(task_id)
        ensurePendingReviewTask(task)
        if not task.is_album_task():
            raise ValueError("Track list updates are only available for album review tasks")
    except ValueError as exc:
        logMetadataEvent(logging.WARNING, "review_tracks_update", result="bad_request", task_id=task_id)
        return {"status": "error", "message": str(exc)}, 400
    except LookupError as exc:
        logMetadataEvent(logging.WARNING, "review_tracks_update", result="not_found", task_id=task_id)
        return {"status": "error", "message": str(exc)}, 404
    raw_data = request.get_json(silent=True) or {}
    track_updates = []
    for track_payload in raw_data.get("tracks", []):
        track = Track.get_or_none(Track.id == track_payload["id"])
        if track is None:
            logMetadataEvent(
                logging.WARNING,
                "review_tracks_update",
                result="not_found",
                task_id=task.id,
                album_id=task.album.id,
                track_id=track_payload["id"],
                processed_count=len(track_updates),
            )
            return {"status": "error", "message": "track not found"}, 404
        previous_artist_id = track.artist_id
        if track.album_id != task.album.id:
            logMetadataEvent(
                logging.WARNING,
                "review_tracks_update",
                result="bad_request",
                task_id=task.id,
                album_id=task.album.id,
                track_id=track.id,
                processed_count=len(track_updates),
            )
            return {"status": "error", "message": "track is outside this review task"}, 400

        track_updates.append((track, track_payload, previous_artist_id))

    with db.atomic():
        for track, track_payload, previous_artist_id in track_updates:
            if "number" in track_payload:
                track.number = track_payload["number"]
            if "title" in track_payload:
                track.title = track_payload["title"]
            artist_name = (track_payload.get("artist_name") or "").strip()
            if artist_name:
                track_artist, _ = Artist.get_or_create(name=artist_name)
                track.artist = resolvePrimaryArtist(track_artist)
            track.save()
            syncTrackArtists(track, previousArtistId=previous_artist_id)

    logMetadataEvent(
        logging.INFO,
        "review_tracks_update",
        result="success",
        task_id=task.id,
        album_id=task.album.id,
        track_count=len(track_updates),
    )

    issue_summary = buildTaskIssueStatus(task)
    return {"status": "success", "message": "tracks updated", **issue_summary}


@frontend.route("/metadata/review-tasks/<task_id>/artists/<artist_id>", methods=["POST"])
@admin_only
def update_metadata_review_task_artist(task_id, artist_id):
    task = None
    try:
        task = getMetadataReviewTask(task_id)
        ensurePendingReviewTask(task)
        if task.is_track_task():
            raise ValueError("Artist updates are not available for track review tasks")
    except ValueError as exc:
        logMetadataEvent(
            logging.WARNING,
            "review_artist_update",
            result="bad_request",
            task_id=task_id,
            requested_artist_id=artist_id,
        )
        return {"status": "error", "message": str(exc)}, 400
    except LookupError as exc:
        logMetadataEvent(
            logging.WARNING,
            "review_artist_update",
            result="not_found",
            task_id=task_id,
            requested_artist_id=artist_id,
        )
        return {"status": "error", "message": str(exc)}, 404
    allowed_artist_ids = {artist.id for artist in getTaskRelatedArtists(task)}
    target_artist = Artist.get_or_none(Artist.id == artist_id)
    if target_artist is None:
        logMetadataEvent(
            logging.WARNING,
            "review_artist_update",
            result="not_found",
            task_id=task.id,
            album_id=task.album.id if task.album is not None else "-",
            requested_artist_id=artist_id,
        )
        return {"status": "error", "message": "artist not found"}, 404
    if target_artist.id not in allowed_artist_ids:
        logMetadataEvent(
            logging.WARNING,
            "review_artist_update",
            result="bad_request",
            task_id=task.id,
            album_id=task.album.id if task.album is not None else "-",
            requested_artist_id=artist_id,
            target_artist_id=target_artist.id,
        )
        return {"status": "error", "message": "artist is outside this review task"}, 400

    raw_data, artist_photo = getArtistMetadataRequestData()
    biography = raw_data.get("biography")
    primary_name = (raw_data.get("primary_name") or "").strip()
    primary_changed = bool(primary_name)
    biography_updated = biography is not None
    photo_updated = artist_photo is not None

    try:
        if primary_name:
            primary_artist, _ = Artist.get_or_create(name=primary_name)
            target_artist = assignPrimaryArtist(target_artist, primary_artist)
        if biography is not None or artist_photo is not None:
            updateArtistMetadata(
                current_app.config,
                target_artist,
                biography=biography,
                imageFile=artist_photo,
            )
    except ValueError as exc:
        logMetadataEvent(
            logging.WARNING,
            "review_artist_update",
            result="bad_request",
            task_id=task.id,
            album_id=task.album.id if task.album is not None else "-",
            requested_artist_id=artist_id,
            target_artist_id=target_artist.id,
            primary_changed=primary_changed,
            biography_updated=biography_updated,
            photo_updated=photo_updated,
        )
        return {"status": "error", "message": str(exc)}, 400

    logMetadataEvent(
        logging.INFO,
        "review_artist_update",
        result="success",
        task_id=task.id,
        album_id=task.album.id if task.album is not None else "-",
        requested_artist_id=artist_id,
        target_artist_id=target_artist.id,
        primary_changed=primary_changed,
        biography_updated=biography_updated,
        photo_updated=photo_updated,
    )

    issue_summary = buildTaskIssueStatus(task)
    return {"status": "success", "message": "artist updated", **issue_summary}


@frontend.route("/metadata/review-tasks/<task_id>/artists/<artist_id>/remove", methods=["POST"])
@admin_only
def remove_metadata_review_task_artist(task_id, artist_id):
    task = None
    try:
        task = getMetadataReviewTask(task_id)
        ensurePendingReviewTask(task)
    except ValueError as exc:
        logMetadataEvent(
            logging.WARNING,
            "review_artist_remove",
            result="bad_request",
            task_id=task_id,
            requested_artist_id=artist_id,
        )
        return {"status": "error", "message": str(exc)}, 400
    except LookupError as exc:
        logMetadataEvent(
            logging.WARNING,
            "review_artist_remove",
            result="not_found",
            task_id=task_id,
            requested_artist_id=artist_id,
        )
        return {"status": "error", "message": str(exc)}, 404

    if task.is_artist_task() or task.album is None:
        logMetadataEvent(
            logging.WARNING,
            "review_artist_remove",
            result="bad_request",
            task_id=task.id,
            album_id="-",
            requested_artist_id=artist_id,
        )
        return {"status": "error", "message": "artist removal is only available for album review tasks"}, 400

    allowed_artist_ids = {artist.id for artist in getTaskRelatedArtists(task)}
    target_artist = Artist.get_or_none(Artist.id == artist_id)
    if target_artist is None:
        logMetadataEvent(
            logging.WARNING,
            "review_artist_remove",
            result="not_found",
            task_id=task.id,
            album_id=task.album.id,
            requested_artist_id=artist_id,
        )
        return {"status": "error", "message": "artist not found"}, 404
    if target_artist.id not in allowed_artist_ids:
        logMetadataEvent(
            logging.WARNING,
            "review_artist_remove",
            result="bad_request",
            task_id=task.id,
            album_id=task.album.id,
            requested_artist_id=artist_id,
            target_artist_id=target_artist.id,
        )
        return {"status": "error", "message": "artist is outside this review task"}, 400
    if target_artist.id == task.album.artist_id:
        logMetadataEvent(
            logging.WARNING,
            "review_artist_remove",
            result="bad_request",
            task_id=task.id,
            album_id=task.album.id,
            requested_artist_id=artist_id,
            target_artist_id=target_artist.id,
        )
        return {"status": "error", "message": "album primary artist cannot be removed"}, 400

    album_tracks = list(task.album.tracks)
    reassigned_tracks = [track for track in album_tracks if track.artist_id == target_artist.id]

    with db.atomic():
        AlbumArtist.delete().where(
            AlbumArtist.album_id == task.album,
            AlbumArtist.artist_id == target_artist,
        ).execute()
        TrackArtist.delete().where(
            TrackArtist.track_id.in_([track.id for track in album_tracks]),
            TrackArtist.artist_id == target_artist,
        ).execute()
        for track in reassigned_tracks:
            previous_artist_id = track.artist_id
            track.artist = task.album.artist
            track.save()
            syncTrackArtists(track, previousArtistId=previous_artist_id)

    logMetadataEvent(
        logging.INFO,
        "review_artist_remove",
        result="success",
        task_id=task.id,
        album_id=task.album.id,
        requested_artist_id=artist_id,
        target_artist_id=target_artist.id,
        reassigned_track_count=len(reassigned_tracks),
    )
    return {
        "status": "success",
        "message": "artist removed from album review task",
        "removed_artist_id": str(target_artist.id),
        "reassigned_track_count": len(reassigned_tracks),
    }


@frontend.route("/metadata/review-tasks/<task_id>/confirm", methods=["POST"])
@admin_only
def confirm_metadata_review_task(task_id):
    task = None
    nfo_path = "-"
    suppression_mode = "daemon"
    try:
        task = getMetadataReviewTask(task_id)
        previous_status = task.status
    except ValueError as exc:
        logMetadataEvent(logging.WARNING, "review_task_confirm", result="bad_request", task_id=task_id)
        return {"status": "error", "message": str(exc)}, 400
    except LookupError as exc:
        logMetadataEvent(logging.WARNING, "review_task_confirm", result="not_found", task_id=task_id)
        return {"status": "error", "message": str(exc)}, 404

    try:
        if task.is_album_task():
            if task.album is None:
                logMetadataEvent(
                    logging.WARNING,
                    "review_task_confirm",
                    result="not_found",
                    task_id=task.id,
                    album_id="-",
                    entity_type=task.entity_type,
                )
                return {"status": "error", "message": "Review task album not found"}, 404
            suppress_ttl = max(2, int(get_current_config().DAEMON["wait_delay"]) + 2)
            try:
                nfo_path = writeReviewAlbumNfo(
                    task,
                    daemonClient=DaemonClient(),
                    suppressTtl=suppress_ttl,
                )
            except DaemonUnavailableError:
                suppression_mode = "daemon_unavailable"
                nfo_path = writeReviewAlbumNfo(
                    task,
                    daemonClient=None,
                    suppressTtl=suppress_ttl,
                )
        else:
            suppression_mode = "not_applicable"
        confirmMetadataReviewTask(task)
    except ValueError as exc:
        logMetadataEvent(
            logging.WARNING,
            "review_task_confirm",
            result="bad_request",
            task_id=task.id,
            album_id=task.album.id if task.album is not None else "-",
            entity_type=task.entity_type,
            failure_reason=str(exc),
        )
        return {"status": "error", "message": str(exc)}, 400
    except Exception as exc:
        logMetadataEvent(
            logging.ERROR,
            "review_task_confirm",
            result="failure",
            task_id=task.id,
            album_id=task.album.id if task.album is not None else "-",
            entity_type=task.entity_type,
            writeback=task.is_album_task(),
            suppression_mode=suppression_mode,
            nfo_path=nfo_path,
            failure_reason=str(exc),
        )
        if task.is_album_task():
            return {"status": "error", "message": f"Failed to write album.nfo: {exc}"}, 500
        return {"status": "error", "message": str(exc)}, 500

    logMetadataEvent(
        logging.INFO,
        "review_task_confirm",
        result="success",
        task_id=task.id,
        album_id=task.album.id if task.album is not None else "-",
        entity_type=task.entity_type,
        writeback=task.is_album_task(),
        suppression_mode=suppression_mode,
        nfo_path=nfo_path,
        from_status=previous_status,
        to_status=task.status,
    )

    redirectResponse = getReviewRedirectResponse(task)
    if redirectResponse is not None:
        return redirectResponse

    return {"status": "success", "message": "review task confirmed", "task_status": task.status}


@frontend.route("/metadata/review-tasks/<task_id>/dismiss", methods=["POST"])
@admin_only
def dismiss_metadata_review_task(task_id):
    task = None
    try:
        task = getMetadataReviewTask(task_id)
        previous_status = task.status
        dismissMetadataReviewTask(task)
    except ValueError as exc:
        logMetadataEvent(logging.WARNING, "review_task_dismiss", result="bad_request", task_id=task_id)
        return {"status": "error", "message": str(exc)}, 400
    except LookupError as exc:
        logMetadataEvent(logging.WARNING, "review_task_dismiss", result="not_found", task_id=task_id)
        return {"status": "error", "message": str(exc)}, 404

    logMetadataEvent(
        logging.INFO,
        "review_task_dismiss",
        result="success",
        task_id=task.id,
        album_id=task.album.id if task.album is not None else "-",
        entity_type=task.entity_type,
        from_status=previous_status,
        to_status=task.status,
    )

    redirectResponse = getReviewRedirectResponse(task)
    if redirectResponse is not None:
        return redirectResponse

    return {"status": "success", "message": "review task dismissed", "task_status": task.status}


@frontend.route("/metadata/review-tasks/<task_id>/reopen", methods=["POST"])
@admin_only
def reopen_metadata_review_task(task_id):
    task = None
    try:
        task = getMetadataReviewTask(task_id)
        previous_status = task.status
        reopenMetadataReviewTask(task)
    except ValueError as exc:
        logMetadataEvent(logging.WARNING, "review_task_reopen", result="bad_request", task_id=task_id)
        return {"status": "error", "message": str(exc)}, 400
    except LookupError as exc:
        logMetadataEvent(logging.WARNING, "review_task_reopen", result="not_found", task_id=task_id)
        return {"status": "error", "message": str(exc)}, 404

    logMetadataEvent(
        logging.INFO,
        "review_task_reopen",
        result="success",
        task_id=task.id,
        album_id=task.album.id if task.album is not None else "-",
        entity_type=task.entity_type,
        from_status=previous_status,
        to_status=task.status,
    )

    redirectResponse = getReviewRedirectResponse(task)
    if redirectResponse is not None:
        return redirectResponse

    return {"status": "success", "message": "review task reopened", "task_status": task.status}


@frontend.route("/artists", methods=["GET", "POST"])
@admin_only
def metadata_artists():
    if request.method == "POST":
        raw_data, artist_photo = getArtistMetadataRequestData()
        action = raw_data.get("action")
        if action in ("change_primary_artist", "change_real_artist"):
            artist_id = raw_data.get("id")
            new_name = raw_data.get("primary_name") or raw_data.get("real_name")
            old_artist = Artist.get_by_id(artist_id)
            target_artist = resolvePrimaryArtist(old_artist)
            primary_changed = bool(new_name and new_name != raw_data['name'])
            biography = raw_data.get("biography")
            biography_updated = biography is not None
            photo_updated = artist_photo is not None

            if primary_changed:
                new_artist, status = Artist.get_or_create(name=new_name)
                # fill new_artist with old_artist's artist_info_json
                if status and old_artist.artist_info_json and not new_artist.artist_info_json:
                    new_artist.artist_info_json = old_artist.artist_info_json
                    new_artist.save()
            elif not (biography_updated or photo_updated):
                logMetadataEvent(
                    logging.INFO,
                    "artist_form_update",
                    result="no_change",
                    action=action,
                    artist_id=old_artist.id,
                    target_artist_id=target_artist.id,
                    primary_changed=primary_changed,
                    biography_updated=biography_updated,
                    photo_updated=photo_updated,
                )
                return {"status": "success", "message": "no change needed"}

            try:
                if primary_changed:
                    target_artist = assignPrimaryArtist(old_artist, new_artist)
                if biography is not None or artist_photo is not None:
                    updateArtistMetadata(
                        current_app.config,
                        target_artist,
                        biography=biography,
                        imageFile=artist_photo,
                    )
            except ValueError as exc:
                logMetadataEvent(
                    logging.WARNING,
                    "artist_form_update",
                    result="bad_request",
                    action=action,
                    artist_id=old_artist.id,
                    target_artist_id=target_artist.id,
                    primary_changed=primary_changed,
                    biography_updated=biography_updated,
                    photo_updated=photo_updated,
                )
                return {"status": "error", "message": str(exc)}, 400

            logMetadataEvent(
                logging.INFO,
                "artist_form_update",
                result="success",
                action=action,
                artist_id=old_artist.id,
                target_artist_id=target_artist.id,
                primary_changed=primary_changed,
                biography_updated=biography_updated,
                photo_updated=photo_updated,
            )

            responsePayload = {
                "status": "success",
                "artist": buildMetadataArtistCardData(target_artist),
                "removed_artist_id": str(old_artist.id) if primary_changed else None,
            }

            if primary_changed and (biography is not None or artist_photo is not None):
                responsePayload["message"] = "primary artist and metadata updated"
                return responsePayload
            if primary_changed:
                responsePayload["message"] = "primary artist updated"
                return responsePayload
            responsePayload["message"] = "artist metadata updated"
            return responsePayload
        return {"status": "success", "message": "artist metadata updated"}

    elif request.method == "GET":
        if request.args.get("embed"):
            return render_template("metadata-embedded.html")
        return render_template("metadata.html")


@frontend.route("/albums", methods=["GET", "POST"])
@admin_only
def metadata_album():
    if request.method == "POST":
        pass
    elif request.method == "GET":
        if request.args.get("embed"):
            return render_template("metadata-album-embedded.html")
        return render_template("metadata-album.html")
