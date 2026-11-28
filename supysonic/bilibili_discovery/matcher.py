"""Resolve evidenced versions against indexed music and the mainland Apple catalog."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import TYPE_CHECKING, Optional, Tuple

from ..album_discovery_candidates import (
    DURATION_TOLERANCE_MS,
    _DiscoveryPreferences,
    _recording_ids,
)
from ..album_discovery_catalog import (
    CatalogAlbum,
    CatalogSong,
    CatalogTrack,
    catalog_identifier,
    normalize_music_name,
)
from ..db import Album, Artist, Track, TrackArtist, User, User_Play_Activity, connection_scope
from ..recommendation_feedback import track_matches_negative_recommendation_feedback

if TYPE_CHECKING:
    from ..album_discovery_service import AlbumDiscoveryService
    from ..db import BilibiliDiscoveryCandidate


_VERSION_PATTERNS = {
    "studio": re.compile(r"录音室(?:版|录音)|錄音室(?:版|錄音)|原(?:始)?录音|原(?:始)?錄音|"
                         r"\bstudio(?: version| recording)?\b|\boriginal recording\b", re.I),
    "live": re.compile(r"现场(?:版|录音)?|現場(?:版|錄音)?|演唱会版|演唱會版|\blive\b", re.I),
    "cover": re.compile(r"翻唱|\bcover(?: version)?\b", re.I),
    "remix": re.compile(r"混音(?:版)?|\bremix(?:ed)?\b", re.I),
    "acoustic": re.compile(r"原声版|原聲版|\bacoustic\b", re.I),
    "unplugged": re.compile(r"不插电|不插電|\bunplugged\b", re.I),
    "remaster": re.compile(r"重制版|重製版|\bremaster(?:ed)?\b", re.I),
}
VERSIONS = tuple(_VERSION_PATTERNS) + ("unknown",)
_QUALIFIER = re.compile(r"\s*(?:[（(\[【]|\s[-–—:/]\s)\s*")
_CHINESE_SUFFIX = re.compile(r"(?:现场版|現場版|演唱会版|演唱會版|翻唱版|混音版|原声版|原聲版|不插电版|不插電版|重制版|重製版|录音室版|錄音室版)$")
_SEARCH_VARIANTS = {
    "live": ("Live", "现场版", "現場版"),
    "cover": ("Cover", "翻唱版"),
    "remix": ("Remix", "混音版"),
    "acoustic": ("Acoustic", "原声版"),
    "unplugged": ("Unplugged", "不插电版"),
    "remaster": ("Remastered", "重制版"),
}


class MatchError(ValueError):
    """Recording or release identity was not safe to act on."""


@dataclass(frozen=True)
class LocalMatch:
    reason: str
    track: Optional[Track] = None


def _versions(text: str) -> set:
    return {name for name, pattern in _VERSION_PATTERNS.items() if pattern.search(text)}


def explicit_source_version(text: str, version: str) -> bool:
    return version != "unknown" and _versions(text) == {version}


def _title_parts(title: str) -> Tuple[str, str]:
    for separator in _QUALIFIER.finditer(title):
        suffix = title[separator.end():]
        if _versions(suffix):
            return title[:separator.start()].strip(), suffix
    match = _CHINESE_SUFFIX.search(title)
    if match and match.start() > 0:
        return title[:match.start()].strip(), match.group()
    return title, ""


def recording_version(title: str, album: str = "") -> str:
    _, qualifier = _title_parts(title)
    # A song titled "Cover" is not itself a cover version. An explicitly
    # labelled track version overrides a generic album edition label.
    versions = _versions(qualifier) if qualifier else _versions(album)
    if not versions:
        return "studio"
    return next(iter(versions)) if len(versions) == 1 else "unknown"


def _same_title(first: str, second: str) -> bool:
    return (
        normalize_music_name(first) == normalize_music_name(second)
        or normalize_music_name(_title_parts(first)[0]) == normalize_music_name(_title_parts(second)[0])
    )


def _track_credits(track: Track) -> Tuple[Artist, ...]:
    credits = (
        Artist.select()
        .join(TrackArtist)
        .where(TrackArtist.track_id == track.id)
        .order_by(TrackArtist.position, Artist.id)
    )
    return tuple({row.id: row for row in (track.artist, *credits)}.values())


def _artist_matching_ids(artist: str) -> set:
    wanted = normalize_music_name(artist)
    rows = {row.id: row for row in Artist.select(Artist.id, Artist.name, Artist.real_artist)}
    canonical = {
        row.real_artist_id or row.id for row in rows.values()
        if normalize_music_name(row.name) == wanted
    }
    return {row.id for row in rows.values() if (row.real_artist_id or row.id) in canonical}


def _same_credit(track: Track, artist: str, matching_artist_ids: Optional[set] = None) -> bool:
    wanted = normalize_music_name(artist)
    if matching_artist_ids is not None and track.artist_id in matching_artist_ids:
        return True
    if wanted in map(normalize_music_name, (track.artist.name, track.artist.get_artist_name())):
        return True
    credits = _track_credits(track)
    if matching_artist_ids and any(credit.id in matching_artist_ids for credit in credits):
        return True
    if any(
        wanted in map(normalize_music_name, (credit.name, credit.get_artist_name()))
        for credit in credits
    ):
        return True
    names = dict.fromkeys(credit.get_artist_name() for credit in credits)
    if any(
        wanted == normalize_music_name(joiner.join(names))
        for joiner in (" & ", ", ", "、", " / ")
    ):
        return True
    if matching_artist_ids is not None:
        return False
    aliases = _artist_matching_ids(artist)
    return any(credit.id in aliases for credit in credits)


def _track_recording_matches(
    track: Track, title: str, version: str, album_hint: str,
) -> bool:
    return (
        _same_title(track.title, title)
        and version != "unknown"
        and recording_version(track.title, track.album.name) == version
        and (not album_hint or normalize_music_name(track.album.name) == normalize_music_name(album_hint))
    )


def local_track_matches_candidate(track: Track, candidate: BilibiliDiscoveryCandidate) -> bool:
    """Verify identity again for a local Track immediately before daily use."""
    return (
        _track_recording_matches(track, candidate.title, candidate.version, candidate.album_hint)
        and _same_credit(track, candidate.artist)
    )


def _in_library(path: Path, root: Path) -> bool:
    try:
        if not path.is_absolute():
            return False
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _indexed_in_library(track: Track, root: Path) -> bool:
    path = Path(track.path)
    try:
        if path.is_absolute():
            path.relative_to(root)
            return True
    except ValueError:
        pass
    # A broken symlink or nonabsolute path still blocks a new order when the
    # index identifies the recording as belonging to the configured library.
    return _in_library(path, root) or _in_library(Path(track.root_folder.path), root)


def playable_local_track(track: Track, library_root: Path) -> bool:
    """Require an indexed duration and a readable regular file in this library."""
    path = Path(track.path)
    if track.duration <= 0 or not _in_library(path, library_root):
        return False
    try:
        if not path.is_file():
            return False
        with path.open("rb") as file:
            return bool(file.read(1))
    except (OSError, ValueError):
        return False


def _same_catalog_recording(track: Track, expected: CatalogTrack) -> bool:
    if abs(track.duration * 1000 - expected.duration_ms) > DURATION_TOLERANCE_MS:
        return False
    local_song_id, local_isrc = _recording_ids(track.path)
    if local_song_id and local_song_id == expected.song_id:
        return True
    if local_isrc and expected.isrc:
        return local_isrc == expected.isrc
    return local_song_id is None


def find_local_recording(
    user: User,
    library_root: Path,
    title: str,
    artist: str,
    version: str,
    album_hint: str = "",
    *,
    expected: Optional[CatalogTrack] = None,
) -> LocalMatch:
    if not library_root.is_dir():
        return LocalMatch("library_unavailable")
    if version == "unknown":
        return LocalMatch("version_unverified")
    with connection_scope():
        artists = _artist_matching_ids(artist)
        # Tracks with composite catalog artists may carry individual indexed credits.
        components = re.split(r"\s[&,/]\s*|、|,\s*", artist)
        possible_artists = artists.union(*(_artist_matching_ids(name) for name in components if name != artist))
        credited = TrackArtist.select(TrackArtist.track_id).where(TrackArtist.artist_id.in_(possible_artists))
        query = Track.select(Track, Artist, Album).join(Artist).switch(Track).join(Album).where(
            (Track.artist.in_(possible_artists)) | (Track.id.in_(credited))
        )
        matches = [
            track for track in query.iterator()
            if _track_recording_matches(track, title, version, album_hint)
            and _same_credit(track, artist, artists)
            and _indexed_in_library(track, library_root)
        ]
        if not matches:
            return LocalMatch("local_missing")
        if any(not playable_local_track(track, library_root) for track in matches):
            return LocalMatch("local_unreadable")
        if expected is not None:
            matches = [track for track in matches if _same_catalog_recording(track, expected)]
            if not matches:
                return LocalMatch("local_recording_conflict")
        if len(matches) != 1:
            return LocalMatch("local_ambiguous")
        track = matches[0]
        preferences = _DiscoveryPreferences(user)
        if (
            track_matches_negative_recommendation_feedback(track, preferences.feedback)
            or normalize_music_name(track.artist.get_artist_name()) in preferences.hidden_artist_names
            or any(str(credit.id) in preferences.feedback["hidden_artist_ids"] for credit in _track_credits(track))
            or normalize_music_name(track.genre or "") in preferences.feedback["hidden_genres"]
        ):
            return LocalMatch("negative_feedback")
        if User_Play_Activity.select().where(
            User_Play_Activity.user == user, User_Play_Activity.track == track,
        ).exists():
            return LocalMatch("already_played")
        return LocalMatch("local_ready", track)


def matching_catalog_songs(
    catalog: object, title: str, artist: str, version: str, album_hint: str = "",
) -> Tuple[CatalogSong, ...]:
    """Read bounded CN search hits and independently confirm their artist credits."""
    searches = (title, *(f"{title} ({label})" for label in _SEARCH_VARIANTS.get(version, ())[:2]))
    hits = {}
    for term in dict.fromkeys(searches):
        for row in catalog.search_seed_songs(term, artist)[:25]:
            if (
                catalog_identifier(row.track.song_id)
                and _same_title(row.track.title, title)
                and (version == "unknown" or recording_version(row.track.title, row.album_name) == version)
                and (not album_hint or normalize_music_name(row.album_name) == normalize_music_name(album_hint))
            ):
                hits.setdefault(row.track.song_id, row)
    if len(hits) > 10:
        raise MatchError("catalog_recording_ambiguous")
    verified = []
    for identifier in sorted(hits):
        song = catalog.get_song(identifier)
        if song and song.track.song_id == identifier and catalog_song_matches(
            catalog, song, title, artist,
            recording_version(song.track.title, song.album_name) if version == "unknown" else version,
            album_hint,
        ):
            verified.append(song)
    return tuple(verified)


def catalog_song_matches(
    catalog: object, song: CatalogSong, title: str, artist: str, version: str, album_hint: str,
) -> bool:
    if (
        version == "unknown" or not song.artist_ids or not _same_title(song.track.title, title)
        or recording_version(song.track.title, song.album_name) != version
        or (album_hint and normalize_music_name(song.album_name) != normalize_music_name(album_hint))
    ):
        return False
    wanted = normalize_music_name(artist)
    if normalize_music_name(song.track.artist) == wanted:
        return True
    return any(
        credit.artist_id in song.artist_ids and normalize_music_name(credit.name) == wanted
        for credit in catalog.get_artists(song.track.song_id)
    )


def _verified_release(
    album: CatalogAlbum, song: CatalogSong, album_hint: str, version: str,
) -> Optional[CatalogTrack]:
    try:
        # Reuse the catalog's identity, URL and manifest invariants.
        verified_album = CatalogAlbum.from_payload(album.to_payload())
    except (TypeError, ValueError, KeyError):
        return None
    if (
        verified_album.album_id not in song.album_ids
        or (album_hint and normalize_music_name(verified_album.title) != normalize_music_name(album_hint))
    ):
        return None
    # A playable release manifest with missing positions is not a complete album.
    previous_disc, previous_track = 0, 0
    target = None
    for track in verified_album.tracks:
        if track.disc_number == previous_disc:
            if track.track_number != previous_track + 1:
                return None
        elif track.disc_number != previous_disc + 1 or track.track_number != 1:
            return None
        previous_disc, previous_track = track.disc_number, track.track_number
        if track.song_id == song.track.song_id:
            target = track
    if (
        target is None
        or normalize_music_name(target.title) != normalize_music_name(song.track.title)
        or normalize_music_name(target.artist) != normalize_music_name(song.track.artist)
        or (target.disc_number, target.track_number) != (song.track.disc_number, song.track.track_number)
        or recording_version(target.title, verified_album.title) != version
        or target.duration_ms != song.track.duration_ms
        or (target.isrc and song.track.isrc and target.isrc != song.track.isrc)
    ):
        return None
    return target


def resolve_catalog_recording(
    catalog: object,
    title: str,
    artist: str,
    version: str,
    album_hint: str = "",
    song_id: Optional[str] = None,
    *,
    refresh: bool = True,
) -> Tuple[CatalogSong, CatalogAlbum]:
    if version == "unknown":
        raise MatchError("version_unverified")
    if song_id is not None:
        song = catalog.get_song(song_id) if catalog_identifier(song_id) else None
        songs = (song,) if song and song.track.song_id == song_id and catalog_song_matches(
            catalog, song, title, artist, version, album_hint,
        ) else ()
    else:
        songs = matching_catalog_songs(catalog, title, artist, version, album_hint)
    if not songs:
        raise MatchError("catalog_recording_unverified")
    if len(songs) != 1:
        raise MatchError("catalog_recording_ambiguous")
    song = songs[0]
    releases = []
    if len(song.album_ids) > 5:
        raise MatchError("catalog_release_ambiguous")
    for album_id in sorted(set(song.album_ids)):
        album = catalog.get_release(album_id, refresh=refresh) if catalog_identifier(album_id) else None
        if album and album.album_id == album_id and _verified_release(album, song, album_hint, version):
            releases.append(album)
    if len(releases) != 1:
        raise MatchError("catalog_release_ambiguous" if releases else "catalog_release_unverified")
    return song, releases[0]


def catalog_target_allowed(
    user: User, catalog: object, song: CatalogSong, album: CatalogAlbum,
) -> bool:
    with connection_scope():
        preferences = _DiscoveryPreferences(user)
        if not preferences.song_allowed(album, song.track, song.genres):
            return False
        if set(song.artist_ids) & preferences.feedback["hidden_artist_ids"]:
            return False
        return not preferences.hidden_artist_names or all(
            preferences.artist_allowed(credit) for credit in catalog.get_artists(song.track.song_id)
        )


def bilibili_target_allowed(
    service: AlbumDiscoveryService, user: User, candidate: BilibiliDiscoveryCandidate,
    album: CatalogAlbum, song_id: str,
) -> bool:
    """Recheck a CN target's independent evidence immediately before dispatch."""
    catalog = service.catalog
    if catalog is None:
        return False
    song = catalog.get_song(song_id)
    if (
        song is None or song.track.song_id != song_id
        or not catalog_song_matches(catalog, song, candidate.title, candidate.artist,
                                    candidate.version, candidate.album_hint)
        or not _verified_release(album, song, candidate.album_hint, candidate.version)
        or not catalog_target_allowed(user, catalog, song, album)
    ):
        return False
    local = find_local_recording(
        user, service.runtime.library_root, candidate.title, candidate.artist,
        candidate.version, candidate.album_hint, expected=song.track,
    )
    return local.reason == "local_missing"
