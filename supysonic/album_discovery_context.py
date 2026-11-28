"""Read-only, evidence-based listening context for album discovery ranking."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
import json
from uuid import UUID

from peewee import JOIN, fn

from .album_discovery_catalog import normalize_discovery_style, normalize_music_name
from .album_discovery_quota import SHANGHAI
from .db import (
    Album, AlbumDiscoveryRelease, AlbumDiscoverySelection, Artist, Playlist, StarredTrack,
    Track, User_Play_Activity, connection_scope,
)
from .recommendation_feedback import (
    get_recommendation_feedback_preferences, track_matches_negative_recommendation_feedback,
)


_WINDOWS = ("last7", "last30", "last90", "previous23")
_EVENT_LIMIT = 100000
_TRACK_DETAIL_LIMIT = 3000
_TREND_LIMIT = 160
_INDEX_ROW_LIMIT = 10000


def _local_naive(when):
    return when.astimezone().replace(tzinfo=None)


def _chunks(values, size=400):
    values = sorted(set(values))
    for start in range(0, len(values), size):
        yield values[start:start + size]


@dataclass
class _Counts:
    plays: int = 0
    days: set = field(default_factory=set)
    tracks: int = 0

    def add_track(self, count, dates):
        if count:
            self.plays += count
            self.days.update(dates)
            self.tracks += 1

    def summary(self):
        return {"plays": self.plays, "playDays": len(self.days), "distinctTracks": self.tracks}


def _windows():
    return [_Counts() for _ in _WINDOWS]


def _activity(user, reference):
    """Keep day counts per track, never individual event payloads in the prompt."""
    week, month, quarter = (reference - timedelta(days=n) for n in (7, 30, 90))
    events = (User_Play_Activity.select(User_Play_Activity.track, User_Play_Activity.time)
              .where((User_Play_Activity.user == user)
                     & (User_Play_Activity.time >= _local_naive(quarter))
                     & (User_Play_Activity.time < _local_naive(reference)))
              .order_by(User_Play_Activity.time.desc(), User_Play_Activity.id)
              .limit(_EVENT_LIMIT + 1).tuples())
    activity = {}
    last = {}
    truncated = False
    for index, (track_id, local_time) in enumerate(events.iterator()):
        if index == _EVENT_LIMIT:
            truncated = True
            break
        at = local_time.astimezone(SHANGHAI)
        # Store each sample once in a non-overlapping band. Calendar dates
        # straddling a rolling-window boundary are deduplicated when combined.
        band = 0 if at >= week else 1 if at >= month else 2
        if track_id not in activity:
            activity[track_id] = [Counter(), Counter(), Counter()]
            last[track_id] = at
        activity[track_id][band][at.date()] += 1
    return activity, last, truncated


def _playlist_dates(user, reference):
    from .recommend import getRecommendPlaylistName, recommended_playlist_where

    dates = [reference.date() - timedelta(days=day) for day in range(30)]
    names = {getRecommendPlaylistName(user, day.isoformat()): day for day in dates}
    query = (Playlist.select(Playlist.name, Playlist.tracks)
             .where((Playlist.user == user) & recommended_playlist_where()
                    & Playlist.name.in_(list(names)) & (Playlist.created <= _local_naive(reference))))
    by_day = defaultdict(set)
    for name, tracks in query.tuples():
        indexed = by_day[names[name]]
        for value in (tracks or "").split(","):
            try:
                indexed.add(UUID(value.strip()))
            except ValueError:
                continue
    return by_day


def _indexed_tracks(user, ids):
    canonical = Artist.alias()
    query = (Track.select(
        Track.id, Track.title, Track.artist, Track.album, Track.genre, Track.duration,
        Artist.id, Artist.name, Artist.real_artist, canonical.id, canonical.name,
        Album.id, Album.name, StarredTrack.user, StarredTrack.starred,
    ).join(Artist).join(canonical, JOIN.LEFT_OUTER, on=(Artist.real_artist == canonical.id), attr="real_artist")
        .switch(Track).join(Album).switch(Track)
        .join(StarredTrack, JOIN.LEFT_OUTER, on=((StarredTrack.starred == Track.id)
              & (StarredTrack.user == user)), attr="current_star"))
    tracks = {}
    feedback = get_recommendation_feedback_preferences(user)
    hidden_styles = {normalize_discovery_style(value) for value in feedback["hidden_genres"]} - {None}
    for batch in _chunks(ids):
        for track in query.where(Track.id.in_(batch)):
            if (track_matches_negative_recommendation_feedback(track, feedback)
                    or str(track.artist.real_artist_id) in feedback["hidden_artist_ids"]
                    or normalize_discovery_style(track.genre) in hidden_styles):
                continue
            tracks[track.id] = track
    return tracks


def _trends(groups, preferred):
    ordered = sorted(groups, key=lambda name: (
        normalize_music_name(name) not in preferred, -groups[name][2].plays,
        -groups[name][0].plays, name,
    ))[:_TREND_LIMIT]
    return [{"name": name, "plays": [item.plays for item in groups[name]],
             "playDays": [len(item.days) for item in groups[name]],
             "distinctTracks": [item.tracks for item in groups[name]]} for name in ordered]


def _listening(activity, last, tracks, seeds, proposals, truncated):
    coverage, labelled = _windows(), _windows()
    artists, genres = defaultdict(_windows), defaultdict(_windows)
    detail = []
    seed_ids = {seed.track_id for seed in seeds}
    for identifier, bands in activity.items():
        track = tracks.get(identifier)
        if track is None:
            continue
        week, prior_month, earlier = bands
        month = week + prior_month
        windows = (week, month, month + earlier, prior_month)
        artist = track.artist.get_artist_name()
        genre = normalize_discovery_style(track.genre) or (track.genre or "").strip()
        buckets = [coverage, artists[artist]] if artist else [coverage]
        if genre:
            buckets += [labelled, genres[genre]]
        for index, counts in enumerate(windows):
            plays = sum(counts.values())
            for bucket in buckets:
                bucket[index].add_track(plays, counts.keys())
        counts = windows[2]
        detail.append({
            "title": track.title, "artist": artist, "album": track.album.name, "genre": genre,
            "plays": sum(counts.values()), "playDays": len(counts), "lastPlayed": last[identifier].isoformat(),
            "starred": bool(getattr(track, "current_star", None)), "seed": str(identifier) in seed_ids,
        })
    detail.sort(key=lambda row: (not row["seed"], -row["playDays"], -row["plays"],
                                 not row["starred"], row["artist"], row["title"], row["album"], row["lastPlayed"]))
    preferred = {normalize_music_name(proposal.track.artist) for proposal in proposals}
    preferred.update(normalize_music_name(seed.artist) for seed in seeds)
    rows_read = sum(sum(band.values()) for bands in activity.values() for band in bands)
    return {
        "windowOrder": list(_WINDOWS), "windowUnit": "rolling days; previous23 excludes last7",
        "coverage": [item.summary() for item in coverage],
        "withGenre": [item.summary() for item in labelled],
        "source": "submitted scrobbles after feedback exclusions; playback source and completion unknown",
        "submittedRowsRead90": rows_read, "excludedOrUnindexedRows90": rows_read - coverage[2].plays,
        "activityReadLimitReached": truncated, "artistsTotal": len(artists), "genresTotal": len(genres),
        "artistTrends": _trends(artists, preferred), "genreTrends": _trends(genres, set()),
        "tracks": detail[:_TRACK_DETAIL_LIMIT], "trackDetailsOmitted": max(0, len(detail) - _TRACK_DETAIL_LIMIT),
    }


def _exposure(playlists, tracks, proposals):
    by_artist, by_song = defaultdict(Counter), defaultdict(list)
    eligible_occurrences = 0
    for day, ids in sorted(playlists.items()):
        for identifier in ids:
            track = tracks.get(identifier)
            if track is None:
                continue
            eligible_occurrences += 1
            names = {normalize_music_name(name) for name in (track.artist.name, track.artist.get_artist_name())}
            for name in names:
                by_artist[name][day] += 1
                key = name, normalize_music_name(track.title), normalize_music_name(track.album.name)
                by_song[key].append((day, track.duration))
    rows = []
    for number, proposal in enumerate(proposals, 1):
        artist = normalize_music_name(proposal.track.artist)
        songs = by_song[(artist, normalize_music_name(proposal.track.title),
                         normalize_music_name(proposal.album.title))]
        matched_days = {day for day, seconds in songs if abs(seconds * 1000 - proposal.track.duration_ms) <= 2000}
        rows.append({"candidate": number, "artistOccurrences": sum(by_artist[artist].values()),
                     "artistPlaylistDays": len(by_artist[artist]), "matchingSongPlaylistDays": len(matched_days)})
    return {"requestedCalendarDays": 30, "observedPlaylistDates": [d.isoformat() for d in sorted(playlists)],
            "retainedTrackOccurrences": sum(map(len, playlists.values())),
            "eligibleIndexedTrackOccurrences": eligible_occurrences,
            "basis": "retained dated playlists; indexed primary artist names; song requires title, artist, "
                     "album and duration match; appearances do not establish playback or recording identity",
            "candidates": rows}


def _inventory_indices(albums):
    names = [album.title for album in albums.values()]
    local = defaultdict(lambda: defaultdict(list))
    if not names:
        return local, False
    primary = Artist.alias()
    album_artist = Artist.alias()
    canonical = Artist.alias()
    query = (Track.select(Album.id, Album.name, album_artist.name, canonical.name, Track.disc,
                          Track.number, Track.title, primary.name, Track.duration)
             .join(Album).join(album_artist, on=(Album.artist == album_artist.id))
             .join(canonical, JOIN.LEFT_OUTER, on=(album_artist.real_artist == canonical.id))
             .switch(Track).join(primary, on=(Track.artist == primary.id))
             .where(Album.name.in_(names) | fn.LOWER(Album.name).in_([name.lower() for name in names]))
             .order_by(Album.id, Track.id).limit(_INDEX_ROW_LIMIT + 1))
    for index, (album_id, title, artist, alias, disc, number, song, credit, seconds) in enumerate(query.tuples()):
        if index == _INDEX_ROW_LIMIT:
            return local, True
        for name in {artist, alias} - {None}:
            local[(normalize_music_name(title), normalize_music_name(name))][album_id].append(
                (disc, number, normalize_music_name(song), normalize_music_name(credit), seconds))
    return local, False


def _receipt_checks(user, reference, albums, library_key):
    checks = defaultdict(list)
    if not albums:
        return checks
    query = (AlbumDiscoveryRelease.select(AlbumDiscoveryRelease.album_id, AlbumDiscoveryRelease.checked_at,
                                          AlbumDiscoveryRelease.reconciliation)
             .join(AlbumDiscoverySelection).where((AlbumDiscoverySelection.user == user)
                    & (AlbumDiscoveryRelease.library_key == library_key)
                    & AlbumDiscoveryRelease.album_id.in_(list(albums))
                    & (AlbumDiscoveryRelease.checked_at <= _local_naive(reference))))
    for key, checked, payload in query.tuples():
        try:
            entries = json.loads(payload)["tracks"]
            expected = {(t.song_id, t.disc_number, t.track_number) for t in albums[key].tracks}
            actual = {(t["song_id"], t["disc_number"], t["track_number"]) for t in entries}
            if actual != expected or len(entries) != len(expected):
                continue
            count = sum(1 for row in entries if row["status"] == "verified" and row.get("track_id"))
            checks[key].append({"checkedAt": checked.astimezone(SHANGHAI).isoformat(), "verifiedTracks": count})
        except (ValueError, KeyError, TypeError):
            continue
    return checks


def _release_context(user, reference, proposals, library_key):
    albums = {item.album.album_id: item.album for item in proposals}
    indexed, cut = _inventory_indices(albums)
    checks = _receipt_checks(user, reference, albums, library_key)
    result = []
    for key, album in albums.items():
        editions = indexed.get((normalize_music_name(album.title), normalize_music_name(album.artist)), {})
        slots = {(t.disc_number, t.track_number, normalize_music_name(t.title), normalize_music_name(t.artist)):
                 t.duration_ms for t in album.tracks}
        best = 0
        for rows in editions.values():
            matched = Counter()
            for disc, number, title, artist, seconds in rows:
                slot = disc, number, title, artist
                if slot in slots and abs(seconds * 1000 - slots[slot]) <= 2000:
                    matched[slot] += 1
            best = max(best, sum(1 for count in matched.values() if count == 1))
        result.append({
            "cnAlbumId": key, "title": album.title, "artist": album.artist, "releaseDate": album.release_date,
            "candidateIds": [n for n, item in enumerate(proposals, 1) if item.album.album_id == key],
            "totalPlayable": len(album.tracks),
            "tracklist": [[t.disc_number, t.track_number, t.title, t.artist] for t in album.tracks],
            "indexEditionsByAlbumArtistAndTitle": len(editions), "maxIndexSlotMatchesInOneEdition": best,
            "indexReadLimitReached": cut,
            "lastSameReleaseReceiptCheck": checks[key][0] if len(checks[key]) == 1 else None,
        })
    return {"inventoryBasis": "Receipt count is historical for this library's checked CN release; null means unknown. "
            "Supysonic-wide index hints match album artist/title and disc, number, track title/artist, duration. "
            "They do not establish files in the target library directory, recording identity or readiness.",
            "releases": result}


def build_discovery_listening_context(user, reference, seeds, proposals, *, library_key="main") -> dict:
    if reference.utcoffset() is None:
        raise ValueError("reference must be timezone-aware")
    reference = reference.astimezone(SHANGHAI)
    with connection_scope():
        activity, last, truncated = _activity(user, reference)
        playlists = _playlist_dates(user, reference)
        ids = set(activity).union(*playlists.values())
        tracks = _indexed_tracks(user, ids)
        return {"shanghaiDay": reference.date().isoformat(),
                "listening": _listening(activity, last, tracks, seeds, proposals, truncated),
                "dailyExposure": _exposure(playlists, tracks, proposals),
                "catalog": _release_context(user, reference, proposals, library_key)}
