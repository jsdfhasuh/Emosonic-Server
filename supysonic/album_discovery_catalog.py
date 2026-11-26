"""Read playable recordings and complete release manifests in the CN catalog."""

from dataclasses import asdict, dataclass
import re
from typing import Dict, Iterable, Mapping, Optional, Tuple
import unicodedata
from urllib.parse import unquote, urlsplit, urlunsplit

import requests

CATALOG_HOST = "https://api.music.apple.com"
CATALOG_PREFIX = "/v1/catalog/cn/"
_CATALOG_ID = re.compile(r"[1-9][0-9]{0,63}")
_ISRC = re.compile(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}")
_SPECIAL_EDITION = re.compile(
    r"\b(deluxe|expanded|anniversary|special edition|collector.s edition|"
    r"remaster(?:ed)?|live|acoustic|unplugged|remix(?:es)?)\b"
    r"|豪華|豪华|特別版|特别版|纪念版|紀念版|现场版|現場版",
    flags=re.IGNORECASE,
)


class CatalogError(RuntimeError):
    """An Apple response could not establish the requested CN catalog data."""


def catalog_identifier(value: object) -> Optional[str]:
    if isinstance(value, str) and _CATALOG_ID.fullmatch(value):
        return value
    return None


def normalize_isrc(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    code = value.strip().replace("-", "").upper()
    return code if _ISRC.fullmatch(code) else None


def normalize_music_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def normalize_cn_album_url(value: str, album_id: str) -> str:
    """Validate a catalog-provided share URL and remove all single-song parameters."""
    if not isinstance(value, str) or not catalog_identifier(album_id):
        raise ValueError("Invalid Chinese Apple Music album URL")
    url = urlsplit(value)
    parts = url.path.rstrip("/").split("/")
    if (
        url.scheme != "https"
        or url.netloc != "music.apple.com"
        or parts[:3] != ["", "cn", "album"]
        or len(parts) not in (4, 5)
        or parts[-1] != album_id
        or any(
            not unquote(part) or unquote(part) in (".", "..")
            or "/" in unquote(part) or "\\" in unquote(part)
            for part in parts[3:-1]
        )
        or any(ord(character) <= 32 for character in value)
    ):
        raise ValueError("Invalid Chinese Apple Music album URL")
    return urlunsplit(("https", url.netloc, url.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class CatalogTrack:
    song_id: str
    title: str
    artist: str
    disc_number: int
    track_number: int
    duration_ms: int
    isrc: Optional[str] = None

    def to_payload(self) -> Dict[str, object]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "CatalogTrack":
        track = cls(**payload)
        if (
            not catalog_identifier(track.song_id)
            or not _text(track.title)
            or not _text(track.artist)
            or not all(_positive_int(value) for value in (
                track.disc_number, track.track_number, track.duration_ms,
            ))
            or (track.isrc is not None and normalize_isrc(track.isrc) != track.isrc)
        ):
            raise ValueError("Invalid catalog track")
        return track


@dataclass(frozen=True)
class CatalogAlbum:
    album_id: str
    title: str
    artist: str
    url: str
    tracks: Tuple[CatalogTrack, ...]
    release_date: str = ""
    is_compilation: bool = False
    is_single: bool = False

    def to_payload(self) -> Dict[str, object]:
        payload = asdict(self)
        payload["tracks"] = [track.to_payload() for track in self.tracks]
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "CatalogAlbum":
        fields = dict(payload)
        fields["tracks"] = tuple(CatalogTrack.from_payload(track) for track in fields["tracks"])
        album = cls(**fields)
        if (
            normalize_cn_album_url(album.url, album.album_id) != album.url
            or not _text(album.title)
            or not _text(album.artist)
            or not _valid_track_sequence(album.tracks)
        ):
            raise ValueError("Invalid catalog release")
        return album


@dataclass(frozen=True)
class CatalogArtist:
    artist_id: str
    name: str
    relation: str = "similar"
    source_song_id: Optional[str] = None
    source_song_title: str = ""


@dataclass(frozen=True)
class CatalogSong:
    track: CatalogTrack
    album_name: str
    album_ids: Tuple[str, ...]
    artist_ids: Tuple[str, ...]
    genres: Tuple[str, ...]


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _object(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _resources(payload: Mapping[str, object]) -> list:
    data = payload.get("data")
    if not isinstance(data, list):
        raise CatalogError("Invalid Apple Music catalog resource list")
    return data


def _catalog_resource(item: Mapping[str, object], kind: str) -> Optional[str]:
    identifier = catalog_identifier(item.get("id"))
    if not identifier or item.get("type") != kind:
        return None
    if item.get("href") not in (None, CATALOG_PREFIX + kind + "/" + identifier):
        return None
    return identifier


def _relationship(item: Mapping[str, object], name: str) -> Mapping[str, object]:
    return _object(_object(item.get("relationships")).get(name))


def _resource_ids(rows: Iterable[object], kind: str) -> Tuple[str, ...]:
    identifiers = (_catalog_resource(_object(row), kind) for row in rows)
    return tuple(identifier for identifier in identifiers if identifier is not None)


def _relation_ids(item: Mapping[str, object], name: str) -> Tuple[str, ...]:
    rows = _relationship(item, name).get("data")
    return _resource_ids(rows, name) if isinstance(rows, list) else ()


def _valid_track_sequence(tracks: Tuple[CatalogTrack, ...]) -> bool:
    return bool(tracks) and len(tracks) == len({
        (track.disc_number, track.track_number) for track in tracks
    }) == len({track.song_id for track in tracks})


def _catalog_track(item: Mapping[str, object]) -> Optional[CatalogTrack]:
    song_id = _catalog_resource(item, "songs")
    attributes = _object(item.get("attributes"))
    play = _object(attributes.get("playParams"))
    if not song_id or play.get("id") != song_id or play.get("kind") != "song":
        return None
    share_url = _text(attributes.get("url"))
    if share_url:
        try:
            share = urlsplit(share_url)
        except ValueError:
            return None
        if (
            share.scheme != "https" or share.netloc != "music.apple.com"
            or not share.path.startswith("/cn/")
        ):
            return None
    try:
        return CatalogTrack.from_payload({
            "song_id": song_id,
            "title": attributes.get("name"),
            "artist": attributes.get("artistName"),
            "disc_number": attributes.get("discNumber"),
            "track_number": attributes.get("trackNumber"),
            "duration_ms": attributes.get("durationInMillis"),
            "isrc": normalize_isrc(attributes.get("isrc")),
        })
    except ValueError:
        return None


def _catalog_song(item: Mapping[str, object]) -> Optional[CatalogSong]:
    track = _catalog_track(item)
    if track is None:
        return None
    attributes = _object(item.get("attributes"))
    album_ids = _relation_ids(item, "albums")
    url = urlsplit(_text(attributes.get("url")))
    album_id = catalog_identifier(url.path.rstrip("/").split("/")[-1])
    if not album_ids and url.path.startswith("/cn/album/") and album_id:
        album_ids = (album_id,)
    genres = attributes.get("genreNames")
    return CatalogSong(
        track=track,
        album_name=_text(attributes.get("albumName")),
        album_ids=album_ids,
        artist_ids=_relation_ids(item, "artists"),
        genres=tuple(genre for genre in genres if _text(genre)) if isinstance(genres, list) else (),
    )


def _catalog_songs(rows: Iterable[object]) -> Tuple[CatalogSong, ...]:
    songs = (_catalog_song(_object(row)) for row in rows)
    return tuple(song for song in songs if song is not None)


class AppleMusicCatalog:
    def __init__(
        self, developer_token: str, *, session: Optional[requests.Session] = None
    ) -> None:
        if not isinstance(developer_token, str) or not developer_token.strip():
            raise CatalogError("Apple Music developer token is missing")
        self._token = developer_token.strip()
        self._session = session or requests
        self._responses = {}
        self._albums = {}

    def _get(self, path: str, **parameters: object) -> Optional[Mapping[str, object]]:
        parsed = urlsplit(path)
        if (
            parsed.scheme or parsed.netloc or parsed.fragment
            or not parsed.path.startswith(CATALOG_PREFIX)
            or any(segment in (".", "..") for segment in unquote(parsed.path).split("/"))
        ):
            raise CatalogError("Apple Music catalog tried to leave the CN storefront")
        cache_key = (path, tuple(sorted(parameters.items())))
        if cache_key in self._responses:
            return self._responses[cache_key]
        try:
            with self._session.get(
                CATALOG_HOST + path,
                headers={"Authorization": "Bearer " + self._token},
                params=parameters,
                timeout=(5, 12),
                allow_redirects=False,
            ) as response:
                if response.status_code == 404:
                    payload = None
                elif response.status_code == 200:
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise CatalogError("Invalid Apple Music catalog response")
                else:
                    raise CatalogError("Apple Music CN catalog request failed")
        except (requests.RequestException, ValueError):
            raise CatalogError("Apple Music CN catalog could not be read") from None
        self._responses[cache_key] = payload
        return payload

    def _pages(
        self, first: Mapping[str, object], path: str, *, max_pages: int = 30
    ) -> Iterable[Mapping[str, object]]:
        current = first
        seen = set()
        for _ in range(max_pages):
            for item in _resources(current):
                yield _object(item)
            cursor = current.get("next")
            if not cursor:
                return
            if (
                not isinstance(cursor, str) or urlsplit(cursor).path != path or cursor in seen
            ):
                raise CatalogError("Invalid Apple Music catalog page cursor")
            seen.add(cursor)
            current = self._get(cursor)
            if current is None:
                raise CatalogError("Missing Apple Music catalog page")
        raise CatalogError("Apple Music catalog pagination limit exceeded")

    def get_song(self, song_id: str) -> Optional[CatalogSong]:
        if not catalog_identifier(song_id):
            return None
        payload = self._get(CATALOG_PREFIX + "songs/" + song_id, include="artists,albums")
        if payload is not None:
            for row in _resources(payload):
                song = _catalog_song(_object(row))
                if song is not None and song.track.song_id == song_id:
                    return song
        return None

    def songs_by_isrc(self, isrc: str) -> Tuple[CatalogSong, ...]:
        code = normalize_isrc(isrc)
        if code is None:
            return ()
        path = CATALOG_PREFIX + "songs"
        payload = self._get(path, **{"filter[isrc]": code, "include": "artists,albums"})
        if payload is None:
            return ()
        return tuple(
            song for song in _catalog_songs(self._pages(payload, path, max_pages=10))
            if song.track.isrc == code
        )

    def search_seed_songs(self, title: str, artist: str) -> Tuple[CatalogSong, ...]:
        payload = self._get(
            CATALOG_PREFIX + "search", types="songs", term=f"{title} {artist}", limit=25,
        )
        if payload is None:
            return ()
        songs = _object(_object(payload.get("results")).get("songs"))
        if not songs:
            return ()
        return _catalog_songs(_resources(songs))

    def get_artists(self, song_id: str) -> Tuple[CatalogArtist, ...]:
        if not catalog_identifier(song_id):
            return ()
        path = CATALOG_PREFIX + "songs/" + song_id + "/artists"
        return self._artist_collection(self._get(path))

    def similar_artists(self, artist_id: str) -> Tuple[CatalogArtist, ...]:
        if not catalog_identifier(artist_id):
            return ()
        path = CATALOG_PREFIX + "artists/" + artist_id + "/view/similar-artists"
        return self._artist_collection(self._get(path, limit=5))[:5]

    @staticmethod
    def _artist_collection(payload: Optional[Mapping[str, object]]) -> Tuple[CatalogArtist, ...]:
        if payload is None:
            return ()
        result = []
        for value in _resources(payload):
            row = _object(value)
            identifier = _catalog_resource(row, "artists")
            name = _text(_object(row.get("attributes")).get("name"))
            if identifier and name:
                result.append(CatalogArtist(identifier, name))
        return tuple(result)

    def top_songs(self, artist_id: str) -> Tuple[CatalogSong, ...]:
        if not catalog_identifier(artist_id):
            return ()
        path = CATALOG_PREFIX + "artists/" + artist_id + "/view/top-songs"
        payload = self._get(path, limit=2, include="artists,albums")
        if payload is None:
            return ()
        return _catalog_songs(_resources(payload))[:2]

    def other_versions(self, album_id: str) -> Tuple[str, ...]:
        if not catalog_identifier(album_id):
            return ()
        path = CATALOG_PREFIX + "albums/" + album_id + "/view/other-versions"
        payload = self._get(path, limit=10)
        if payload is None:
            return ()
        return _resource_ids(_resources(payload), "albums")

    def get_release(self, album_id: str, *, refresh: bool = False) -> Optional[CatalogAlbum]:
        if not catalog_identifier(album_id):
            return None
        if refresh:
            self._responses.clear()
            self._albums.pop(album_id, None)
        if album_id not in self._albums:
            self._albums[album_id] = self._read_release(album_id)
        return self._albums[album_id]

    def _read_release(self, album_id: str) -> Optional[CatalogAlbum]:
        path = CATALOG_PREFIX + "albums/" + album_id
        payload = self._get(path, include="tracks")
        if payload is None:
            return None
        item = next(
            (row for row in map(_object, _resources(payload))
             if _catalog_resource(row, "albums") == album_id),
            None,
        )
        if not item:
            return None
        attributes = _object(item.get("attributes"))
        title, artist = _text(attributes.get("name")), _text(attributes.get("artistName"))
        if not title or not artist:
            return None
        try:
            url = normalize_cn_album_url(attributes.get("url"), album_id)
        except ValueError:
            return None

        tracks_path = path + "/tracks"
        relation = _relationship(item, "tracks")
        if not relation:
            relation = self._get(tracks_path)
        if relation is None:
            return None
        tracks = []
        positions = set()
        ids = set()
        for row in self._pages(relation, tracks_path):
            if row.get("type") != "songs":
                continue
            attributes_playback = _object(row.get("attributes")).get("playParams")
            if attributes_playback is None:
                continue
            track = _catalog_track(row)
            if track is None:
                # A playable row with invalid or non-CN identity cannot be silently dropped.
                return None
            position = (track.disc_number, track.track_number)
            if position in positions or track.song_id in ids:
                return None
            positions.add(position)
            ids.add(track.song_id)
            tracks.append(track)
        if not tracks:
            return None
        return CatalogAlbum(
            album_id, title, artist, url,
            tuple(sorted(tracks, key=lambda track: (track.disc_number, track.track_number))),
            release_date=_text(attributes.get("releaseDate")),
            is_compilation=attributes.get("isCompilation") is True,
            is_single=attributes.get("isSingle") is True,
        )


def release_version_penalty(album: CatalogAlbum, recording_artist: str) -> tuple:
    return (
        album.is_compilation or normalize_music_name(album.artist) != normalize_music_name(recording_artist),
        bool(_SPECIAL_EDITION.search(album.title)),
        album.is_single,
        album.release_date or "9999",
    )
