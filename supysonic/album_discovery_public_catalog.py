"""Read public, storefront-specific Apple Music page metadata without credentials."""

from dataclasses import dataclass
from html.parser import HTMLParser
import json
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests

from .album_discovery_catalog import (
    CatalogAlbum, CatalogArtist, CatalogError, CatalogSong, CatalogTrack,
    catalog_identifier, discovery_style_curator_url, normalize_cn_album_url,
    normalize_discovery_style, normalize_music_name,
)


_ORIGIN = "https://music.apple.com"
_SONG_COUNT = re.compile(r"(\d[\d,]*)\s*(?:首歌曲|首歌|songs?\b)", re.IGNORECASE)
_STYLE_SONG_LIMIT = 3


def _object(value):
    return value if isinstance(value, dict) else {}


def _items(value):
    return value if isinstance(value, list) else []


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _identity(url, kind):
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    path = parts.path.rstrip("/").split("/")
    if (parts.scheme != "https" or parts.netloc != "music.apple.com"
            or len(path) not in (4, 5) or path[:3] != ["", "cn", kind]):
        return None
    return catalog_identifier(path[-1])


def _descriptor(row, kind):
    desc = _object(_object(row).get("contentDescriptor"))
    identifier = catalog_identifier(_object(desc.get("identifiers")).get("storeAdamID"))
    if not identifier or desc.get("kind") != kind:
        return None
    url = desc.get("url")
    if _identity(url, kind) == identifier:
        return identifier
    if (kind == "song" and _identity(url, "album")
            and parse_qs(urlsplit(url).query).get("i") == [identifier]):
        return identifier
    return None


def _artists(links):
    artists = []
    for link in _items(links):
        link = _object(link)
        destination = _object(_object(link.get("segue")).get("destination"))
        identifier = _descriptor(destination, "artist")
        name = _text(link.get("title"))
        if not identifier or not name:
            return ()
        artists.append(CatalogArtist(identifier, name))
    return tuple(artists)


class _PageData(HTMLParser):
    """Only the public page model and structured album metadata are inputs."""

    def __init__(self):
        super().__init__()
        self.documents = {}
        self.active = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.active = None
            attrs = dict(attrs)
            if (attrs.get("id"), attrs.get("type")) in {
                ("serialized-server-data", "application/json"),
                ("schema:music-album", "application/ld+json"),
                ("schema:song", "application/ld+json"),
            }:
                self.active = attrs["id"]

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = None

    def handle_data(self, data):
        if self.active:
            self.documents[self.active] = self.documents.get(self.active, "") + data


@dataclass
class _Page:
    canonical: str
    sections: dict
    schema: dict

    def rows(self, section):
        return _items(_object(self.sections.get(section)).get("items"))


def _similar_section(section):
    identifier = _text(section.get("id"))
    title = _text(_object(section.get("header")).get("title"))
    return (".SimilarArtists-" in identifier or identifier == "similar-artists - similar-artists"
            or title in {"相似艺人", "类似艺人", "Similar Artists"})


def _parse_page(html, kind, identifier):
    parser = _PageData()
    parser.feed(html)
    try:
        data = json.loads(parser.documents.get("serialized-server-data", "{}"))
        content = data["data"][0]["data"]
        canonical = content["canonicalURL"]
        if not isinstance(canonical, str):
            return None
        if kind == "search":
            if (urlsplit(canonical)._replace(query="", fragment="").geturl() != _ORIGIN + "/cn/search"
                    or parse_qs(urlsplit(canonical).query).get("term") != [identifier]):
                return None
        elif _identity(canonical, kind) != identifier:
            return None
        def required(section):
            identifier = section["id"]
            if kind == "song":
                return identifier == "songDetail"
            if kind == "search":
                return identifier == "track-section - song"
            if kind == "artist":
                return ".TopSongs-" in identifier or _similar_section(section)
            if kind == "curator":
                return identifier.startswith("copper-track-swoosh - ") and section.get("itemKind") == "trackLockup"
            return identifier.startswith(("track-list -", "track-list-section -", "other-versions -"))

        sections = {section["id"]: section for section in content["sections"] if required(section)}
        schema_key = {"song": "schema:song", "album": "schema:music-album"}.get(kind)
        schema = _object(json.loads(parser.documents.get(schema_key, "{}")))
        if kind == "song":
            audio = _object(schema.get("audio"))
            schema = {"audio": {key: audio.get(key) for key in ("@type", "url", "genre", "inAlbum")}}
        else:
            schema = {key: schema.get(key) for key in ("@type", "url", "byArtist", "name", "datePublished")}
        return _Page(canonical, sections, schema)
    except (TypeError, ValueError, KeyError, IndexError):
        return None


class PublicAppleMusicCatalog:
    def __init__(self, *, session=None):
        self._session = session or requests
        self._pages = {}
        self._albums = {}

    def _page(self, kind, identifier, *, canonical_url=None):
        key = kind, identifier
        if key in self._pages:
            return self._pages[key]
        if canonical_url and _identity(canonical_url, kind) == identifier:
            url = canonical_url
        elif kind == "search":
            url = _ORIGIN + "/cn/search?" + urlencode({"term": identifier})
        elif catalog_identifier(identifier):
            url = f"{_ORIGIN}/cn/{kind}/{identifier}"
        else:
            return None
        for _ in range(3):
            try:
                with self._session.get(
                    url, headers={"Accept-Language": "zh-CN"}, timeout=(5, 12),
                    allow_redirects=False, auth=lambda request: request,
                ) as response:
                    if response.is_redirect:
                        target = urljoin(url, response.headers.get("Location", ""))
                        if kind == "search" or _identity(target, kind) != identifier:
                            return None
                        url = target
                        continue
                    if response.status_code == 404:
                        page = None
                    elif response.status_code == 200:
                        page = _parse_page(response.content.decode("utf-8"), kind, identifier)
                        if page is None:
                            raise CatalogError("Unrecognized Apple Music CN public page")
                    else:
                        raise CatalogError("Apple Music CN public page unavailable")
                    self._pages[key] = page
                    return page
            except (requests.RequestException, UnicodeError, ValueError):
                raise CatalogError("Apple Music CN public page could not be read") from None
        return None

    def get_release(self, album_id, *, refresh=False):
        if refresh:
            self._pages.pop(("album", album_id), None)
            self._albums.pop(album_id, None)
        if album_id not in self._albums:
            page = self._page("album", album_id)
            self._albums[album_id] = self._release(page, album_id) if page else None
        return self._albums[album_id]

    @staticmethod
    def _release(page, album_id):
        schema = page.schema
        if schema.get("@type") != "MusicAlbum" or _identity(schema.get("url"), "album") != album_id:
            return None
        artists = [_text(row.get("name")) for row in _items(schema.get("byArtist"))
                   if _identity(row.get("url"), "artist")]
        title = _text(schema.get("name"))
        if not title or not artists or not all(artists):
            return None
        tracks = []
        for row in page.rows("track-list - " + album_id):
            row = _object(row)
            kind = _object(row.get("contentDescriptor")).get("kind")
            if kind == "musicVideo" or row.get("isDisabled") is True:
                continue
            song_id = _descriptor(row, "song")
            play = _object(row.get("playAction"))
            play_ids = [_descriptor(entry, "song") for entry in _items(play.get("items"))]
            share = _object(row.get("contentDescriptor")).get("url")
            if (not song_id or _identity(share, "album") != album_id
                    or play.get("$kind") != "playAction" or play_ids != [song_id]):
                return None
            try:
                tracks.append(CatalogTrack.from_payload({
                    "song_id": song_id, "title": row.get("title"), "artist": row.get("artistName"),
                    "disc_number": row.get("discNumber"), "track_number": row.get("trackNumber"),
                    "duration_ms": row.get("duration"),
                }))
            except ValueError:
                return None
        footer = page.rows("track-list-section - " + album_id)
        count = _SONG_COUNT.search(_text(_object(footer[0]).get("description"))) if footer else None
        # The header may include songs withheld in CN. The CN footer counts the visible tracks.
        if not tracks or not count or int(count[1].replace(",", "")) != len(tracks):
            return None
        if (len({track.song_id for track in tracks}) != len(tracks)
                or len({(track.disc_number, track.track_number) for track in tracks}) != len(tracks)):
            return None
        return CatalogAlbum(
            album_id, title, " & ".join(artists), normalize_cn_album_url(page.canonical, album_id),
            tuple(sorted(tracks, key=lambda track: (track.disc_number, track.track_number))),
            release_date=_text(schema.get("datePublished")),
            is_compilation=any(normalize_music_name(name) in {"various artists", "群星"} for name in artists),
            is_single=title.casefold().endswith(" - single"),
        )

    def _song_detail(self, song_id):
        page = self._page("song", song_id)
        rows = page.rows("songDetail") if page else []
        if len(rows) != 1 or _descriptor(rows[0], "song") != song_id:
            return None, {}
        return page, rows[0]

    def get_artists(self, song_id):
        _, detail = self._song_detail(song_id)
        return _artists(detail.get("artistLinks"))

    def get_song(self, song_id):
        page, detail = self._song_detail(song_id)
        if not page:
            return None
        artists = _artists(detail.get("artistLinks"))
        recording = _object(page.schema.get("audio"))
        if (not artists or _identity(recording.get("url"), "song") != song_id
                or recording.get("@type") != "MusicRecording"):
            return None
        album_url = _object(recording.get("inAlbum")).get("url")
        album_id = _identity(album_url, "album")
        if not album_id:
            return None
        # The song already supplies the canonical CN release URL, saving a redirect.
        try:
            album_url = normalize_cn_album_url(album_url, album_id)
        except ValueError:
            return None
        self._page("album", album_id, canonical_url=album_url)
        release = self.get_release(album_id)
        if release is None:
            return None
        track = next((track for track in release.tracks if track.song_id == song_id), None)
        if track is None or track.title != detail.get("title"):
            return None
        genres = tuple(value for value in _items(recording.get("genre")) if _text(value))
        return CatalogSong(track, release.title, (album_id,), tuple(a.artist_id for a in artists), genres)

    def songs_by_isrc(self, isrc):
        # Public storefront search does not supply an ISRC-identity lookup.
        return ()

    def style_songs(self, style, *, resolve_song=None):
        style = normalize_discovery_style(style)
        if not style:
            return ()
        url = discovery_style_curator_url(style)
        page = self._page("curator", _identity(url, "curator"), canonical_url=url)
        if not page:
            return ()

        # The public curator page supplies playable song IDs and the CN album
        # they belong to. Plain-text names, search hits and playlist cards do not.
        recordings = {}
        for section in page.sections:
            for row in page.rows(section):
                row = _object(row)
                song_id = _descriptor(row, "song")
                album_id = _identity(_object(row.get("contentDescriptor")).get("url"), "album")
                play = _object(row.get("playAction"))
                if (not song_id or not album_id or row.get("isDisabled") is True
                        or play.get("$kind") != "playAction"
                        or not any(_descriptor(item, "song") == song_id for item in _items(play.get("items")))):
                    continue
                recordings.setdefault(song_id, album_id)
        songs = []
        error = None
        resolve_song = resolve_song or self.get_song
        for song_id, album_id in list(recordings.items())[:_STYLE_SONG_LIMIT]:
            try:
                song = resolve_song(song_id)
            except CatalogError as exc:
                error = exc
                continue
            if (song and song.track.song_id == song_id and album_id in song.album_ids and song.artist_ids
                    and any(normalize_discovery_style(genre) == style for genre in song.genres)):
                songs.append(song)
        if not songs and error:
            raise error
        return tuple(songs)

    def search_seed_song_ids(self, title: str, artist: str) -> tuple:
        page = self._page("search", f"{title} {artist}")
        rows = page.rows("track-section - song") if page else []
        matches = [
            row for row in rows
            if normalize_music_name(_text(row.get("title"))) == normalize_music_name(title)
        ]
        if len(matches) > 8:
            matches = [row for row in matches if any(
                normalize_music_name(credit.name) == normalize_music_name(artist)
                for credit in _artists(row.get("subtitleLinks"))
            )]
        ids = dict.fromkeys(_descriptor(row, "song") for row in matches)
        if len(ids) > 8:
            return ()
        return tuple(identifier for identifier in ids if identifier)

    def search_seed_songs(self, title, artist):
        return tuple(song for identifier in self.search_seed_song_ids(title, artist)
                     if (song := self.get_song(identifier)))

    def _top_rows(self, artist_id):
        page = self._page("artist", artist_id)
        if page:
            for section in page.sections:
                if ".TopSongs-ArtistFeaturedContentAndTracks" in section:
                    for group in page.rows(section):
                        return _items(_object(group).get("tracks"))
        return []

    def top_songs(self, artist_id):
        songs = []
        for row in self._top_rows(artist_id)[:5]:
            song_id = _descriptor(row, "song")
            song = self.get_song(song_id) if song_id else None
            if song and artist_id in song.artist_ids:
                songs.append(song)
                if len(songs) == 2:
                    break
        return tuple(songs)

    def similar_artists(self, artist_id):
        page = self._page("artist", artist_id)
        artists = []
        if page:
            for section in page.sections:
                if not _similar_section(page.sections[section]):
                    continue
                for row in page.rows(section):
                    identifier = _descriptor(row, "artist")
                    name = _text(row.get("title"))
                    if identifier and name:
                        artists.append(CatalogArtist(identifier, name))
        return tuple(artists)

    def collaborating_artists(self, artist_id):
        result = {}
        for row in self._top_rows(artist_id)[:8]:
            song_id = _descriptor(row, "song")
            if not song_id:
                continue
            _, detail = self._song_detail(song_id)
            credits = _artists(detail.get("artistLinks"))
            title = _text(detail.get("title"))
            if not title or artist_id not in {artist.artist_id for artist in credits}:
                continue
            for other in credits:
                if other.artist_id != artist_id:
                    result.setdefault(other.artist_id, CatalogArtist(
                        other.artist_id, other.name, "collaborator", song_id, title,
                    ))
            if len(result) >= 5:
                break
        return tuple(result.values())[:5]

    def other_versions(self, album_id):
        page = self._page("album", album_id)
        versions = []
        if page:
            for section in page.sections:
                if section.startswith("other-versions -"):
                    for row in page.rows(section):
                        identifier = _descriptor(row, "album")
                        if identifier and identifier != album_id:
                            versions.append(identifier)
        return tuple(versions)
