"""Reconcile an Apple release with the files OpenWrt received and indexed."""

import hashlib
import math
import re
import stat
import unicodedata
from dataclasses import asdict, dataclass, replace
from os import stat_result
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from mediafile import MediaFile, UnreadableFileError

from .album_discovery_catalog import CatalogAlbum, CatalogTrack
from .album_discovery_oracle import DeliveryManifest, cn_album_id, parse_delivery_path
from .db import Album, Artist, Track, connection_scope


_AUDIO_SUFFIXES = {
    ".aac", ".aif", ".aiff", ".ape", ".flac", ".m4a", ".mp3", ".mpc",
    ".oga", ".ogg", ".opus", ".wav", ".wv", ".wma",
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_NUMBERED = re.compile(r"(?:(\d{1,3})[-.])?(\d{1,3})(?=[\s._-])")
_DISC_FOLDER = re.compile(r"(?:cd|disc)\s*(\d{1,3})", re.IGNORECASE)
_MSEC_TOLERANCE = 2000


@dataclass(frozen=True)
class ReconciledTrack:
    song_id: str
    disc_number: int
    track_number: int
    status: str
    reason: str = ""
    track_id: Optional[str] = None
    album_id: Optional[str] = None
    path: Optional[str] = None
    sha256: Optional[str] = None
    size: Optional[int] = None
    batch_id: Optional[str] = None


@dataclass(frozen=True)
class AlbumReconciliation:
    ready: bool
    local_album_id: Optional[str]
    representative_track_id: Optional[str]
    tracks: Tuple[ReconciledTrack, ...]

    @property
    def verified(self) -> Tuple[ReconciledTrack, ...]:
        return tuple(track for track in self.tracks if track.status == "verified")

    @property
    def missing(self) -> Tuple[ReconciledTrack, ...]:
        return tuple(track for track in self.tracks if track.status != "verified")

    def to_payload(self) -> dict:
        return {
            "ready": self.ready,
            "local_album_id": self.local_album_id,
            "representative_track_id": self.representative_track_id,
            "tracks": [asdict(track) for track in self.tracks],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "AlbumReconciliation":
        return cls(
            bool(payload["ready"]), payload["local_album_id"],
            payload["representative_track_id"],
            tuple(ReconciledTrack(**track) for track in payload["tracks"]),
        )


@dataclass(frozen=True)
class _Receipt:
    sha256: str
    size: int
    batch_id: Optional[str]


def _normalize(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _is_album(title: object, artist: object, album: CatalogAlbum) -> bool:
    return _normalize(title) == _normalize(album.title) and _normalize(artist) == _normalize(
        album.artist
    )


def _position_from_path(path: PurePosixPath) -> Optional[Tuple[int, int]]:
    number = _NUMBERED.match(path.name)
    if not number:
        return None
    disc = _DISC_FOLDER.fullmatch(path.parts[2]) if len(path.parts) > 3 else None
    return int(number[1] or (disc[1] if disc else "1")), int(number[2])


def _delivery_paths(
    album: CatalogAlbum,
    deliveries: Iterable[DeliveryManifest],
    previous_tracks: Iterable[Mapping[str, object]],
) -> Dict[PurePosixPath, List[_Receipt]]:
    paths: Dict[PurePosixPath, List[_Receipt]] = {}
    for batch in deliveries:
        if cn_album_id(batch.album_url) != album.album_id:
            continue
        for item in batch.files:
            path = parse_delivery_path(item.path)
            if path is not None and path.suffix.casefold() in _AUDIO_SUFFIXES:
                paths.setdefault(path, []).append(_Receipt(item.sha256, item.size, batch.batch_id))
    song_ids = {track.song_id for track in album.tracks}
    for item in previous_tracks:
        path = parse_delivery_path(item.get("path"))
        digest, size, batch_id = (item.get(key) for key in ("sha256", "size", "batch_id"))
        if (
            item.get("song_id") in song_ids and path is not None
            and path.suffix.casefold() in _AUDIO_SUFFIXES
            and isinstance(digest, str) and _SHA256.fullmatch(digest)
            and type(size) is int and size > 0
            and (batch_id is None or isinstance(batch_id, str))
        ):
            entry = _Receipt(digest, size, batch_id)
            if entry not in paths.setdefault(path, []):
                paths[path].append(entry)
    return paths


def _indexed_paths(
    root: Path, album: CatalogAlbum, delivery_paths: Iterable[PurePosixPath]
) -> Dict[PurePosixPath, List[Track]]:
    paths: Dict[PurePosixPath, List[Track]] = {}
    seen = set()

    def collect(rows: Iterable[Track]) -> None:
        for track in rows:
            if track.id in seen:
                continue
            try:
                relative = Path(track.path).resolve().relative_to(root)
            except (OSError, RuntimeError, ValueError):
                continue
            path = parse_delivery_path(relative.as_posix())
            if (
                path is None or path.parts[0].startswith(".")
                or path.suffix.casefold() not in _AUDIO_SUFFIXES
            ):
                continue
            paths.setdefault(path, []).append(track)
            seen.add(track.id)

    with connection_scope():
        query = Track.select(Track, Album, Artist).join(Album).join(Artist)
        collect(query.where(Album.name == album.title))
        absolute_paths = [str(root.joinpath(*path.parts)) for path in delivery_paths]
        for offset in range(0, len(absolute_paths), 400):
            collect(query.where(Track.path.in_(absolute_paths[offset:offset + 400])))
    return paths


def _provider_id(media: MediaFile, *fields: str) -> Optional[str]:
    tags = getattr(getattr(media, "mgfile", None), "tags", None) or {}
    for name in fields:
        value = tags.get(name)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value is not None and re.fullmatch(r"[1-9][0-9]{0,63}", str(value)):
            return str(value)
    return None


def _same_duration(seconds: object, milliseconds: int) -> bool:
    return (
        isinstance(seconds, (int, float)) and math.isfinite(seconds) and seconds > 0
        and abs(seconds * 1000 - milliseconds) <= _MSEC_TOLERANCE
    )


def _sha256(file: Path) -> str:
    digest = hashlib.sha256()
    with file.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(value: stat_result) -> tuple:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _result(
    track: CatalogTrack,
    reason: str,
    path: PurePosixPath,
    record: Optional[Track] = None,
    receipt: Optional[_Receipt] = None,
) -> ReconciledTrack:
    return ReconciledTrack(
        track.song_id, track.disc_number, track.track_number,
        "verified" if record else "missing",
        reason, str(record.id) if record else None,
        str(record.album_id) if record else None, path.as_posix(),
        receipt.sha256 if receipt else None, receipt.size if receipt else None,
        receipt.batch_id if receipt else None,
    )


def _inspect_path(
    root: Path,
    path: PurePosixPath,
    album: CatalogAlbum,
    expected: Mapping[Tuple[int, int], CatalogTrack],
    records: List[Track],
    receipts: List[_Receipt],
    require_receipts: bool,
) -> Optional[ReconciledTrack]:
    guessed = expected.get(_position_from_path(path)) if _is_album(
        path.parts[1], path.parts[0], album
    ) else None
    expected_receipt = receipts[0] if receipts and len({
        (receipt.sha256, receipt.size) for receipt in receipts
    }) == 1 else None
    file = root.joinpath(*path.parts)
    try:
        if file.resolve() != file:
            return _result(guessed, "path_conflict", path) if guessed else None
        before = file.stat()
        if not stat.S_ISREG(before.st_mode):
            return _result(guessed, "path_conflict", path) if guessed else None
    except (OSError, RuntimeError, ValueError):
        return _result(guessed, "waiting_for_file", path, receipt=expected_receipt) if guessed else None

    try:
        media = MediaFile(str(file))
    except (UnreadableFileError, OSError, ValueError):
        return _result(guessed, "unreadable_audio", path) if guessed else None
    if not _is_album(media.album, media.albumartist or media.artist, album):
        return _result(guessed, "edition_conflict", path) if guessed else None
    track = expected.get((media.disc or 1, media.track))
    if track is None:
        return _result(guessed, "track_number_conflict", path) if guessed else None

    album_id = _provider_id(media, "plID", "itunesalbumid", "applemusic_album_id")
    song_id = _provider_id(media, "cnID", "itunestrackid", "applemusic_song_id")
    if (
        (album_id is not None and album_id != album.album_id)
        or (song_id is not None and song_id != track.song_id)
        or _normalize(media.title) != _normalize(track.title)
        or (track.isrc and media.isrc and track.isrc.upper() != media.isrc.upper())
        or not _same_duration(media.length, track.duration_ms)
    ):
        return _result(track, "recording_conflict", path)

    if require_receipts and not receipts:
        return _result(track, "waiting_for_receipt", path)
    try:
        digest = _sha256(file)
        if _file_identity(before) != _file_identity(file.stat()):
            return _result(track, "file_changed_during_check", path)
    except OSError:
        return _result(track, "waiting_for_file", path)
    receipt = next((
        r for r in receipts if r.sha256 == digest and r.size == before.st_size
    ), None)
    if receipts and receipt is None:
        return _result(track, "checksum_conflict", path, receipt=expected_receipt)
    if receipt is None:
        receipt = _Receipt(digest, before.st_size, None)

    if not records:
        return _result(track, "waiting_for_index", path, receipt=receipt)
    if len(records) != 1:
        return _result(track, "ambiguous_index", path, receipt=receipt)
    indexed = records[0]
    if (
        not _is_album(indexed.album.name, indexed.album.artist.name, album)
        or indexed.disc != track.disc_number or indexed.number != track.track_number
        or _normalize(indexed.title) != _normalize(track.title)
        or not _same_duration(indexed.duration, track.duration_ms)
    ):
        return _result(track, "index_conflict", path, receipt=receipt)
    return _result(track, "", path, indexed, receipt)


def _select_tracks(
    album: CatalogAlbum,
    reports: Mapping[str, List[ReconciledTrack]],
) -> Tuple[ReconciledTrack, ...]:
    checked = []
    for track in album.tracks:
        choices = reports.get(track.song_id, [])
        verified = [result for result in choices if result.track_id]
        if len(verified) == 1:
            checked.append(verified[0])
        elif len(verified) > 1:
            checked.append(replace(
                verified[0], status="missing", track_id=None, album_id=None,
                reason="ambiguous_recording",
            ))
        elif choices:
            checked.append(next(
                (result for result in choices if result.reason != "waiting_for_file"),
                choices[0],
            ))
        else:
            checked.append(ReconciledTrack(
                track.song_id, track.disc_number, track.track_number, "missing",
                "waiting_for_file",
            ))
    return tuple(checked)


def reconcile_album(
    album: CatalogAlbum,
    representative_song_id: str,
    library_root: Path,
    deliveries: Iterable[DeliveryManifest] = (),
    previous_tracks: Iterable[Mapping[str, object]] = (),
    *,
    require_receipts: bool = False,
) -> AlbumReconciliation:
    """Recheck file/receipt/edition identity and exact currently playable local IDs."""
    root = Path(library_root).resolve()
    expected = {(track.disc_number, track.track_number): track for track in album.tracks}
    delivery_paths = _delivery_paths(album, deliveries, previous_tracks)
    indexed_paths = _indexed_paths(root, album, delivery_paths)
    reports: Dict[str, List[ReconciledTrack]] = {}
    for path in sorted(set(delivery_paths) | set(indexed_paths)):
        result = _inspect_path(
            root, path, album, expected, indexed_paths.get(path, []),
            delivery_paths.get(path, []), require_receipts,
        )
        if result is not None:
            reports.setdefault(result.song_id, []).append(result)

    checked = _select_tracks(album, reports)
    matched = [track for track in checked if track.track_id]
    album_ids = {track.album_id for track in matched}
    folders = {PurePosixPath(track.path).parts[:2] for track in matched}
    track_ids = {track.track_id for track in matched}
    if len(album_ids) > 1 or len(folders) > 1 or len(track_ids) != len(matched):
        checked = tuple(
            replace(track, status="missing", reason="split_or_ambiguous_edition",
                    track_id=None, album_id=None) if track.track_id else track
            for track in checked
        )
        return AlbumReconciliation(False, None, None, checked)

    representative = next((
        track.track_id for track in matched if track.song_id == representative_song_id
    ), None)
    ready = len(matched) == len(checked) and representative is not None
    return AlbumReconciliation(ready, next(iter(album_ids), None), representative, checked)
