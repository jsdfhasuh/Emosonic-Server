"""Verify song, style and joint-credit clues against the Chinese catalog."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from ..album_discovery_candidates import discovery_style_support
from ..album_discovery_catalog import (
    CatalogAlbum,
    CatalogError,
    CatalogSong,
    catalog_identifier,
    discovery_style_curator_url,
    normalize_discovery_style,
    normalize_isrc,
    normalize_music_name,
    release_version_penalty,
)
from ..album_discovery_public_catalog import PublicAppleMusicCatalog
from ..db import User
from .matcher import (
    MatchError, catalog_song_matches, matching_catalog_songs, recording_version, resolve_catalog_recording,
)

MAX_STYLES = 3
MAX_SONGS_PER_STYLE = 3
MAX_COLLABORATORS = 5
MAX_SONGS_PER_COLLABORATOR = 2
MAX_EXPLORED_SONGS = 30


@dataclass(frozen=True)
class VerifiedSuggestion:
    song: CatalogSong
    kind: str
    basis: dict
    leads: Tuple[dict, ...]


def build_exploration_plan(
    user: User, leads: List[dict], reference: datetime,
) -> Tuple[List[dict], dict]:
    collaboration_groups = defaultdict(list)
    styles = defaultdict(list)
    works = defaultdict(list)
    unidentified = 0
    for lead in leads:
        if lead["kind"] == "work_lead":
            title, artist = (normalize_music_name(lead[key]) for key in ("title", "artist"))
            if title and artist:
                works[title, artist].append(lead)
            else:
                unidentified += 1
        elif lead["kind"] == "style_lead":
            styles[lead["style"]].append(lead)
        elif lead["collaborator"]:
            identity = tuple(normalize_music_name(lead[key]) for key in (
                "title", "artist", "collaborator", "version", "album_hint",
            ))
            collaboration_groups[identity].append(lead)
    plan = [{"kind": "work", "leads": group} for group in works.values()]
    for group in list(collaboration_groups.values())[:MAX_COLLABORATORS]:
        plan.append({"kind": "collaboration", "leads": group})
    supported = discovery_style_support(user, set(styles), now=reference) if styles else ()
    for style, seeds in supported[:MAX_STYLES]:
        plan.append({
            "kind": "style", "style": style, "leads": styles[style],
            "support": [
                {"track_id": seed.track_id, "artist": seed.artist, "play_dates": list(seed.play_dates)}
                for seed in seeds
            ],
        })
    skipped = {
        "work_artist_unverified": unidentified,
        "style_unsupported_or_over_limit": len(set(styles) - {style for style, _ in supported}),
        "collaboration_probe_limit": max(0, len(collaboration_groups) - MAX_COLLABORATORS),
    }
    return plan, {reason: count for reason, count in skipped.items() if count}


def _work_song_ids(catalog: object, title: str, artist: str) -> tuple:
    if isinstance(catalog, PublicAppleMusicCatalog):
        # Public search resolves each ID through several potentially unavailable pages.
        # Let the work probe handle those failures independently.
        return catalog.search_seed_song_ids(title, artist)
    return tuple(dict.fromkeys(
        hit.track.song_id for hit in catalog.search_seed_songs(title, artist)
        if catalog_identifier(hit.track.song_id)
        and normalize_music_name(hit.track.title) == normalize_music_name(title)
    ))


def _work_edition(
    catalog: object, identifier: str, title: str, artist: str,
) -> Optional[Tuple[CatalogSong, CatalogAlbum, str]]:
    song = catalog.get_song(identifier)
    if not song or song.track.song_id != identifier:
        raise MatchError("catalog_work_unverified")
    if not catalog_song_matches(catalog, song, title, artist, "studio", ""):
        return None
    credits = {
        credit.artist_id for credit in catalog.get_artists(identifier)
        if catalog_identifier(credit.artist_id) and credit.artist_id in song.artist_ids
        and normalize_music_name(credit.name) == normalize_music_name(artist)
    }
    if len(credits) != 1:
        raise MatchError("catalog_work_artist_unverified")
    song, album = resolve_catalog_recording(
        catalog, title, artist, "studio", song.album_name, identifier, refresh=False,
    )
    return song, album, credits.pop()


def _work_suggestions(catalog: object, task: dict) -> Tuple[List[VerifiedSuggestion], str]:
    if not all(callable(getattr(catalog, method, None)) for method in (
        "search_seed_songs", "get_song", "get_artists", "get_release",
    )):
        return [], "catalog_work_unavailable"
    leads = task["leads"]
    title, artist = leads[0]["title"], leads[0]["artist"]
    if not title or not artist:
        return [], "work_artist_unverified"
    try:
        identifiers = _work_song_ids(catalog, title, artist)
    except CatalogError:
        task["retryable"] = True
        return [], "catalog_work_search_unavailable"
    # Search ordering can change between attempts. An earlier failed edition
    # must not disappear from the checks and make a partial match look unique.
    previous = {row["song_id"]: row for row in task.get("verified_releases", ())}
    identifiers = dict.fromkeys((*identifiers, *task.get("failed_recordings", ()), *previous))
    failures = {}
    verified = []
    for identifier in identifiers:
        try:
            edition = _work_edition(catalog, identifier, title, artist)
        except CatalogError:
            failures[identifier] = "catalog_page_unavailable"
        except MatchError as exc:
            failures[identifier] = str(exc)
        else:
            if edition:
                verified.append(edition)
    # Keep independently verified editions available for review, even when
    # their recording identities cannot be equated and none can be chosen.
    summaries = {sid: previous[sid] for sid in failures if sid in previous}
    for item, release, _ in verified:
        summaries[item.track.song_id] = {
            "song_id": item.track.song_id, "album_id": release.album_id, "album_title": release.title,
        }
    task["verified_releases"] = list(summaries.values())
    task["failed_recordings"] = failures
    task["retryable"] = "catalog_page_unavailable" in failures.values()
    if failures:
        return [], "catalog_work_partial" if verified else next(iter(failures.values()))
    if not verified:
        return [], "catalog_work_unverified"
    if len({artist_id for _, _, artist_id in verified}) != 1:
        return [], "catalog_work_artist_ambiguous"
    if len(verified) > 1:
        codes = {normalize_isrc(item.track.isrc) for item, _, _ in verified}
        if None in codes or len(codes) != 1:
            return [], "catalog_work_recording_ambiguous"
    song, album, artist_id = min(verified, key=lambda pair: (
        release_version_penalty(pair[1], artist), pair[1].album_id, pair[0].track.song_id,
    ))
    basis = {
        "work_title": title, "original_artist": artist, "original_artist_id": artist_id,
        "performers": sorted({lead["performer"] for lead in leads if lead.get("performer")}),
        "song_id": song.track.song_id, "album_id": album.album_id, "album_title": album.title,
        "catalog_version": "studio",
    }
    return [VerifiedSuggestion(song, "work", basis, tuple(leads))], ""


def _style_suggestions(catalog: object, task: dict) -> Tuple[List[VerifiedSuggestion], str]:
    lookup = getattr(catalog, "style_songs", None)
    if not callable(lookup):
        return [], "catalog_style_unavailable"
    style = task["style"]
    songs = {
        song.track.song_id: song for song in lookup(style)
        if catalog_identifier(song.track.song_id) and song.artist_ids and song.album_ids
        and style in map(normalize_discovery_style, song.genres)
        and recording_version(song.track.title, song.album_name) != "unknown"
    }
    basis = {
        "style": style, "editorial_source": discovery_style_curator_url(style),
        "listening_support": task["support"],
    }
    return [
        VerifiedSuggestion(songs[identifier], "style", basis, tuple(task["leads"]))
        for identifier in sorted(songs)[:MAX_SONGS_PER_STYLE]
    ], "" if songs else "catalog_style_no_recordings"


def _collaboration_suggestions(
    catalog: object, task: dict, progress: dict,
) -> Tuple[List[VerifiedSuggestion], str]:
    if not all(callable(getattr(catalog, method, None)) for method in (
        "search_seed_songs", "get_song", "get_artists", "top_songs",
    )):
        return [], "catalog_collaboration_unavailable"
    lead = task["leads"][0]
    songs = matching_catalog_songs(
        catalog, lead["title"], lead["artist"], lead["version"], lead["album_hint"],
    )
    relationships = {}
    original_name = normalize_music_name(lead["artist"])
    related_name = normalize_music_name(lead["collaborator"])
    for song in songs:
        credits = {
            normalize_music_name(credit.name): credit.artist_id for credit in catalog.get_artists(song.track.song_id)
            if credit.artist_id in song.artist_ids and catalog_identifier(credit.artist_id)
        }
        first, second = credits.get(original_name), credits.get(related_name)
        if first and second and first != second:
            relationships.setdefault((first, second), song)
    if len(relationships) != 1:
        return [], "collaboration_credit_unverified"
    (first, second), source = next(iter(relationships.items()))
    looked_up = progress.setdefault("collaborators", {})
    if second not in looked_up:
        if len(looked_up) >= MAX_COLLABORATORS:
            return [], "collaboration_artist_limit"
        looked_up[second] = sorted({
            row.track.song_id for row in catalog.top_songs(second)
            if catalog_identifier(row.track.song_id)
        })[:MAX_SONGS_PER_COLLABORATOR]
    basis = {
        "source_artist_id": first, "related_artist_id": second,
        "credit_song_id": source.track.song_id, "credit_song_title": source.track.title,
    }
    result = []
    for song_id in looked_up[second]:
        if song_id == source.track.song_id:
            continue
        song = catalog.get_song(song_id)
        if (
            song is None or song.track.song_id != song_id or second not in song.artist_ids
            or not song.album_ids or recording_version(song.track.title, song.album_name) == "unknown"
            or (source.track.isrc and song.track.isrc == source.track.isrc)
        ):
            continue
        result.append(VerifiedSuggestion(song, "collaboration", basis, tuple(task["leads"])))
    return result, "" if result else "catalog_collaborator_no_recordings"


def explore_task(catalog: object, task: dict, progress: dict) -> Tuple[List[VerifiedSuggestion], str]:
    try:
        if task["kind"] == "style":
            suggestions, reason = _style_suggestions(catalog, task)
        elif task["kind"] == "work":
            suggestions, reason = _work_suggestions(catalog, task)
        else:
            suggestions, reason = _collaboration_suggestions(catalog, task, progress)
    except MatchError as exc:
        suggestions, reason = [], str(exc)
    if task["kind"] == "work":
        # Named works have resumable tasks independent of the speculative
        # style and collaboration expansion budget.
        task["outcome"] = reason or "verified_work"
        return suggestions, reason
    known = set(progress.get("explored_song_ids", []))
    admitted = []
    for suggestion in suggestions:
        identifier = suggestion.song.track.song_id
        if identifier in known or len(known) < MAX_EXPLORED_SONGS:
            admitted.append(suggestion)
            known.add(identifier)
    progress["explored_song_ids"] = sorted(known)
    return admitted, reason or ("exploration_song_limit" if not admitted else "")
