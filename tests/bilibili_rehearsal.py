"""Pool-only R3 rehearsal using a temporary library and simulated providers."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID
import wave

from supysonic import db
from supysonic.album_discovery_catalog import CatalogAlbum, CatalogArtist, CatalogSong, CatalogTrack
from supysonic.album_discovery_oracle import OracleAlbumState
from supysonic.album_discovery_service import AlbumDiscoveryService
from supysonic.album_discovery_settings import load_album_discovery_runtime
from supysonic.bilibili_discovery.history import BilibiliHistoryClient
from supysonic.bilibili_discovery.recommendation import build_bilibili_daily_pool
from supysonic.bilibili_discovery.service import run_bilibili_discovery
from supysonic.config import DefaultConfig
from supysonic.recommend import (
    _buildRecommendedTracks, _get_minimum_fresh_track_count,
    create_recommend_playlist, getRecommendPlaylistName,
)
from supysonic.recommendation_feedback import (
    get_recommendation_feedback_preferences, set_recommendation_feedback,
)
from supysonic.recommendation_openai_daily import daily_candidate_limit
from supysonic.user_listening_profile import build_recommendation_play_weights


AT = datetime(2026, 11, 28, 1, tzinfo=timezone.utc)
DAY = "2026-11-28"
SOURCE_DAY = "2026-11-27"


def _fixture_track(
    root: db.Folder, number: int, title: str, artist_name: str, genre: str, popularity: int = 0,
) -> db.Track:
    artist, _ = db.Artist.get_or_create(name=artist_name)
    album, _ = db.Album.get_or_create(name=f"{artist_name}示例发行", artist=artist)
    path = Path(root.path) / f"{number}.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * 8000)
    return db.Track.create(
        id=UUID(int=number), title=title, genre=genre, artist=artist, album=album,
        path=str(path), folder=root, root_folder=root, disc=1, number=1,
        bitrate=128, duration=1, last_modification=1, play_count=popularity,
    )


def _fixture_extraction(title: str, artist: str, version: str) -> dict:
    marker = {"studio": "录音室版", "live": "现场版", "remix": "混音版"}[version]
    return dict(kind="song", title=title, artist=artist, version=version,
                evidence=f"{artist}《{title}》{marker}")


class _HistoryFixture:
    def __init__(self, results: list[dict]) -> None:
        defaults = dict(kind="non_music", title="", artist="", version="unknown",
                        style="", collaborator="", album_hint="", evidence="")
        self.results = [{**defaults, **result} for result in results]
        self.calls = []

    def request(self, method: str, url: str, **kwargs: object) -> Mock:
        self.calls.append((method, url))
        if (method, url) == ("POST", "https://fixture.invalid/api/history/sync"):
            payload = {"success": True, "details": {"bilibili": {"newCount": 0, "updateCount": 0}}}
        elif (method, url) == ("GET", "https://fixture.invalid/api/history"):
            assert kwargs["params"]["date"] == SOURCE_DAY and kwargs["params"]["page"] == 1
            rows = [
                {
                    "id": str(index), "platform": "bilibili", "business": "archive",
                    "bvid": f"fixture-{index}", "cid": index + 1, "author_name": "fixture uploader",
                    "title": row["evidence"] or "更换滤芯", "tag_name": "音乐" if row["evidence"] else "生活",
                    "view_time": int((AT - timedelta(hours=12)).timestamp()) + index,
                }
                for index, row in enumerate(self.results)
            ]
            payload = {"items": rows, "total": len(rows), "hasMore": False}
        else:
            raise AssertionError("Unexpected history fixture request")
        response = Mock(status_code=200)
        response.iter_content.return_value = [json.dumps(payload).encode()]
        result = MagicMock()
        result.__enter__.return_value = response
        return result

    def extract(self, config: object, messages: list[dict], schema: dict, **kwargs: object) -> dict:
        assert kwargs["workload"] == "album_discovery"
        content = messages[1]["content"]
        assert "fixture uploader" not in content and "fixture-1" not in content
        items = json.loads(content)["items"]
        assert len(items) == len(self.results)
        return {"results": [dict(result, id=item["id"]) for result, item in zip(self.results, items)]}


class _CatalogFixture:
    def __init__(self, style_track: db.Track) -> None:
        style_song = CatalogTrack("9201", style_track.title, style_track.artist.name, 1, 1, 1000)
        self.style_song = CatalogSong(style_song, style_track.album.name, ("9200",), ("92",), ("R&B",))
        self.style_release = CatalogAlbum(
            "9200", self.style_song.album_name, style_song.artist,
            "https://music.apple.com/cn/album/fixture-style/9200", (style_song,),
        )
        tracks = tuple(
            CatalogTrack(str(9100 + number), "北窗的灯" if number == 1 else f"样本发行第{number}曲",
                         "北窗乐队", 1, number, 1000)
            for number in range(1, 13)
        )
        self.pending_song = CatalogSong(tracks[0], "北窗样本发行", ("9100",), ("91",), ("Pop",))
        self.pending_release = CatalogAlbum(
            "9100", self.pending_song.album_name, "北窗乐队",
            "https://music.apple.com/cn/album/fixture-pending/9100", tracks,
        )

    def style_songs(self, style: str) -> tuple[CatalogSong, ...]:
        assert style == "R&B"
        return (self.style_song,)

    def search_seed_songs(self, title: str, artist: str) -> tuple[CatalogSong, ...]:
        return (self.pending_song,) if title == "北窗的灯" and artist == "北窗乐队" else ()

    def get_song(self, song_id: str) -> CatalogSong | None:
        return {song.track.song_id: song for song in (self.style_song, self.pending_song)}.get(song_id)

    def get_artists(self, song_id: str) -> tuple[CatalogArtist, ...]:
        song = self.get_song(song_id)
        return (CatalogArtist(song.artist_ids[0], song.track.artist),) if song else ()

    def get_release(self, album_id: str, *, refresh: bool = False) -> CatalogAlbum | None:
        return {
            self.style_release.album_id: self.style_release, self.pending_release.album_id: self.pending_release,
        }.get(album_id)


def _describe_track(track: db.Track) -> dict:
    return dict(uuid=str(track.id), title=track.title, artist=track.artist.name, genre=track.genre)


def _describe_pool(tracks: list[db.Track] | tuple[db.Track, ...]) -> dict:
    return {
        "count": len(tracks),
        "artists": dict(sorted(Counter(track.artist.name for track in tracks).items())),
        "genres": dict(sorted(Counter(track.genre or "(unlabeled)" for track in tracks).items())),
    }


def _playlist_fingerprint(playlist: db.Playlist) -> dict:
    row = db.Playlist[playlist.id]
    return {"uuid": str(row.id), "ordered_tracks_sha256": sha256(row.tracks.encode()).hexdigest()}


def _exercise(root: Path) -> dict:
    config = DefaultConfig()
    config.ALBUM_DISCOVERY = {"target_user": "fixture", "library_key": "rehearsal", "library_root": str(root)}
    config.BILIBILI_DISCOVERY = {"enabled": True, "auto_download": False, "history_api_url": "https://fixture.invalid"}
    config.RECOMMENDATION_AGENT = {"provider": "openai", "daily_selection_enabled": True}
    user = db.User.create(id=UUID(int=701), name="fixture", password="unused", salt="fixture")
    folder = db.Folder.create(root=True, name="Fixture library", path=str(root))
    for number in range(1, 151):
        _fixture_track(folder, number, f"示例曲{number:03d}", f"示例艺人{number % 30:02d}", "国语流行", 100)
    style = _fixture_track(folder, 161, "夜渡潮汐", "渡口乐团", "R&B")
    live = _fixture_track(folder, 162, "潮痕 (Live)", "海岸乐团", "另类音乐")
    _fixture_track(folder, 163, "潮痕", "海岸乐团", "")
    blocked = _fixture_track(folder, 164, "隐藏样本", "样本乐团", "")
    _fixture_track(folder, 165, "灯塔", "灯塔乐团", "")
    for index in range(3):
        genre = "R&B" if index else "国语流行"
        seed = _fixture_track(folder, 180 + index, f"真实复听夹具{index}", f"复听艺人{index}", genre)
        for offset in range(1, 12 if index == 0 else 4):
            db.User_Play_Activity.create(
                user=user, track=seed, time=AT.astimezone().replace(tzinfo=None) - timedelta(days=offset),
            )
    set_recommendation_feedback(user, str(blocked.id), "dislike_song")
    weights = build_recommendation_play_weights(user, AT.astimezone().replace(tzinfo=None))
    preferences = get_recommendation_feedback_preferences(user)
    capacity = daily_candidate_limit(50)
    baseline = _buildRecommendedTracks(
        weights, capacity, excludedTrackIds=preferences["disliked_song_ids"], preferences=preferences,
        recommendationDay=DAY, minimumFreshTracks=_get_minimum_fresh_track_count(capacity, config),
    )
    overlap = (baseline[0], baseline[1], baseline[-1])
    history = _HistoryFixture([
        *(_fixture_extraction(track.title, track.artist.name, "studio") for track in overlap),
        _fixture_extraction("潮痕", live.artist.name, "live"),
        {"kind": "style_lead", "style": "R&B", "evidence": "R&B 编曲赏析"},
        _fixture_extraction(blocked.title, blocked.artist.name, "studio"),
        _fixture_extraction("灯塔", "灯塔乐团", "remix"),
        _fixture_extraction("北窗的灯", "北窗乐队", "studio"),
        {},
    ])
    client = BilibiliHistoryClient(config.BILIBILI_DISCOVERY["history_api_url"], None, session=history)
    oracle = Mock()
    oracle.inspect.return_value = OracleAlbumState("absent")
    oracle.submit.side_effect = AssertionError("The rehearsal cannot submit an Oracle order")
    catalog = _CatalogFixture(style)
    album_service = AlbumDiscoveryService(
        config, load_album_discovery_runtime(config), catalog=catalog, oracle=oracle,
    )
    playlist = db.Playlist.create(
        id=UUID(int=702), user=user, name=getRecommendPlaylistName(user, DAY), comment="recommended",
        tracks=",".join(str(track.id) for track in baseline[:50]),
    )
    before = _playlist_fingerprint(playlist)
    plays_before = db.User_Play_Activity.select().count()
    with patch("supysonic.bilibili_discovery.service.BilibiliHistoryClient", return_value=client), patch(
        "supysonic.bilibili_discovery.extractor.request_recommendation_json", side_effect=history.extract,
    ) as extract, patch.object(AlbumDiscoveryService, "from_config", return_value=album_service), patch(
        "supysonic.recommendation_openai_daily.request_recommendation_json",
        side_effect=AssertionError("The pool-only rehearsal cannot rank"),
    ) as rank:
        for step in range(12):
            run_bilibili_discovery(config, now=AT + timedelta(minutes=step))
        assert create_recommend_playlist(user=user, day=DAY, config=config) == 0
        assert extract.call_count == 1
        assert rank.call_count == 0
    run = db.BilibiliDiscoveryRun.get()
    assert run.status == "done"
    pool = build_bilibili_daily_pool(
        config, user, baseline, capacity, listened_ids=weights, preferences=preferences,
        at=AT + timedelta(minutes=12),
    )
    assert pool is not None
    by_id = {str(track.id): track for track in (*baseline, *pool.tracks)}
    assert len(baseline) == len(pool.tracks) == 150
    assert set(pool.signals) - set(pool.added_ids) == {str(track.id) for track in overlap}
    assert set(pool.added_ids) == {str(live.id), str(style.id)}
    assert len(pool.displaced_ids) == 2
    after = _playlist_fingerprint(playlist)
    orders = db.AlbumDiscoveryOrder.select().count()
    plays_after = db.User_Play_Activity.select().count()
    assert before == after and orders == oracle.submit.call_count == 0
    assert plays_before == plays_after
    assert db.AlbumDiscoverySelection.select().count() == 0
    assert db.AlbumDiscoveryPlacement.select().count() == 0
    candidates = db.BilibiliDiscoveryCandidate.select().order_by(
        db.BilibiliDiscoveryCandidate.title, db.BilibiliDiscoveryCandidate.version,
    )
    return {
        "data": "synthetic history, catalog and temporary SQLite library",
        "fixture_user_uuid": str(user.id), "source_day_shanghai": SOURCE_DAY, "playlist_day": DAY,
        "observations": len(json.loads(run.snapshot_json)),
        "snapshot_sha256": sha256(run.snapshot_json.encode()).hexdigest(),
        "discovery_outcomes": json.loads(run.discovery_progress_json)["outcomes"],
        "history_fixture_methods": [method for method, _ in history.calls],
        "mock_extraction_calls": extract.call_count, "run_status": run.status,
        "baseline": _describe_pool(baseline),
        "annotated_inside_baseline": [_describe_track(track) for track in overlap],
        "added": [_describe_track(by_id[identifier]) for identifier in pool.added_ids],
        "displaced": [_describe_track(by_id[identifier]) for identifier in pool.displaced_ids],
        "merged": _describe_pool(pool.tracks),
        "candidates": [
            {
                "title": item.title, "artist": item.artist, "version": item.version,
                "status": item.status, "reason": item.reason,
                "local_uuid": str(item.track_id) if item.track_id else None,
                "evidence": json.loads(item.source_evidence_json),
            }
            for item in candidates
        ],
        "missing_release_fixture": {"album_id": "9100", "song_id": "9101", "tracks": 12, "indexed": 0},
        "playlist_before": before, "playlist_after": after,
        "fixture_oracle_submissions": oracle.submit.call_count, "fixture_orders": orders,
        "plays_before": plays_before, "plays_after": plays_after,
        "production_touched": False, "ranked_selection": "unmeasured", "model_tokens": "unmeasured",
    }


def main() -> None:
    with TemporaryDirectory(prefix="emosonic-bilibili-r3-") as directory, patch(
        "socket.socket.connect", side_effect=AssertionError("The fixture rehearsal cannot use the network"),
    ), patch(
        "socket.socket.connect_ex", side_effect=AssertionError("The fixture rehearsal cannot use the network"),
    ):
        root = Path(directory) / "library"
        root.mkdir()
        db.init_database("sqlite:///" + (Path(directory) / "rehearsal.sqlite").as_posix())
        try:
            print(json.dumps(_exercise(root), ensure_ascii=False, indent=2, sort_keys=True))
        finally:
            db.release_database()


if __name__ == "__main__":
    main()
