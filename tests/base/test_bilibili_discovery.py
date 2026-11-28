import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from supysonic import db
from supysonic.album_discovery_catalog import CatalogAlbum, CatalogArtist, CatalogSong, CatalogTrack
from supysonic.album_discovery_quota import SHANGHAI
from supysonic.album_discovery_service import AlbumDiscoveryService
from supysonic.album_discovery_settings import load_album_discovery_runtime
from supysonic.bilibili_discovery.extractor import BATCH_SIZE, extract_history
from supysonic.bilibili_discovery.history import BilibiliHistoryClient, HistoryError, HistoryObservation
from supysonic.bilibili_discovery.matcher import (
    MatchError, find_local_recording, local_track_matches_candidate, resolve_catalog_recording,
)
from supysonic.bilibili_discovery.service import run_bilibili_discovery
from supysonic.openai_recommendations import OpenAIRecommendationError
from supysonic.recommendation_feedback import set_recommendation_feedback


DAY = date(2026, 11, 27)
MIDNIGHT = datetime(2026, 11, 27, tzinfo=SHANGHAI)
WAKE = datetime(2026, 11, 27, 20, 30, tzinfo=timezone.utc)
REQUEST = "supysonic.bilibili_discovery.extractor.request_recommendation_json"
HISTORY = "supysonic.bilibili_discovery.service.BilibiliHistoryClient"
ALBUM_SERVICE = "supysonic.bilibili_discovery.service.AlbumDiscoveryService.from_config"


def observation(identifier: int, title: str, *, tag: str = "音乐") -> HistoryObservation:
    return HistoryObservation(str(identifier), "archive", f"BVFixture{identifier}", "666",
                              title, "Fixture Uploader", tag, int(MIDNIGHT.timestamp()) + identifier)


def result(identifier: int, kind: str, **data: str) -> dict:
    return {
        "id": identifier, "kind": kind, "title": "", "artist": "", "version": "unknown",
        "style": "", "collaborator": "", "album_hint": "", "evidence": "", **data,
    }


def catalog_song(identifier: str, title: str, artist: str, *credits: str,
                 album: str = "Demo Album", genre: str = "Rock") -> CatalogSong:
    return CatalogSong(
        CatalogTrack(identifier, title, artist, 1, 1, 180000),
        album, ("900",), credits, (genre,),
    )


class HistoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.session = Mock()
        self.client = BilibiliHistoryClient("https://fixture.invalid", None, session=self.session)

    @staticmethod
    def response(payload: dict, status: int = 200) -> Mock:
        result = Mock(status_code=status)
        result.iter_content.return_value = [json.dumps(payload).encode("utf-8")]
        context = Mock()
        context.__enter__ = Mock(return_value=result)
        context.__exit__ = Mock(return_value=False)
        return context

    def row(self, identifier: int, offset: int = 0, **fields: object) -> dict:
        return {
            "platform": "bilibili", "id": identifier, "business": "archive", "cid": 9,
            "title": "Fixture title", "view_time": int(MIDNIGHT.timestamp()) + offset,
            "timestamp": 0, **fields,
        }

    def page(self, rows: list, total: int, more: bool) -> Mock:
        return self.response({"items": rows, "total": total, "hasMore": more})

    def test_shanghai_boundaries_and_stable_visible_event_identity(self) -> None:
        with patch("supysonic.bilibili_discovery.history.PAGE_SIZE", 2):
            self.session.request.side_effect = [
                self.page([self.row(1, 0), self.row(1, 85900)], 4, True),
                self.page([self.row(1, 85900), self.row(2, 86399)], 4, False),
            ]
            rows = self.client.read_day(DAY)
        self.assertEqual([row.id for row in rows], ["1", "1", "2"])
        self.assertEqual([row.view_time for row in rows], [
            int(MIDNIGHT.timestamp()) + n for n in (0, 85900, 86399)
        ])
        call = self.session.request.call_args
        self.assertEqual(call.args, ("GET", "https://fixture.invalid/api/history"))
        self.assertEqual(call.kwargs["params"]["date"], "2026-11-27")
        self.assertFalse(call.kwargs["allow_redirects"])
        self.session.request.side_effect = None
        for invalid in (-1, 86400, int(MIDNIGHT.timestamp()) * 999):
            self.session.request.return_value = self.page([self.row(1, invalid)], 1, False)
            with self.assertRaisesRegex(HistoryError, "source_day_mismatch"):
                self.client.read_day(DAY)

    def test_repeated_page_conflicting_count_and_authenticated_empty_input(self) -> None:
        with patch("supysonic.bilibili_discovery.history.PAGE_SIZE", 1):
            self.session.request.side_effect = [
                self.page([self.row(1)], 2, True),
                self.page([self.row(1)], 2, False),
            ]
            with self.assertRaisesRegex(HistoryError, "history_repeated_page"):
                self.client.read_day(DAY)
            self.session.request.side_effect = [
                self.page([self.row(1)], 2, True),
                self.page([], 0, False),
            ]
            with self.assertRaisesRegex(HistoryError, "inconsistent_pagination"):
                self.client.read_day(DAY)
        self.session.request.side_effect = [
            self.response({}, status=401),
            self.response({"success": True, "details": {}}),
            self.response({"success": True, "details": {"bilibili": {"newCount": 0, "updateCount": 0}}}),
            self.page([], 0, False),
        ]
        with self.assertRaisesRegex(HistoryError, "authentication_failed"):
            self.client.sync()
        with self.assertRaisesRegex(HistoryError, "bilibili_unconfirmed"):
            self.client.sync()
        self.client.sync()
        self.assertEqual(self.client.read_day(DAY), ())

    def test_source_overwrites_are_only_observable_in_current_snapshot(self) -> None:
        self.session.request.return_value = self.page([
            self.row(33, 20, event_id="fixture-001"),
            self.row(33, 25, event_id="fixture-001"),
        ], 2, False)
        rows = self.client.read_day(DAY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].view_time, int(MIDNIGHT.timestamp()) + 20)


class ExtractionTestCase(unittest.TestCase):
    def test_uses_literal_title_ids_and_excludes_uploader_bvid_and_urls(self) -> None:
        first = "Aria《Night》现场版"
        inputs = [
            observation(1, first + " https://fixture.invalid/secret"),
            observation(2, "《Road》录音室版"),
            observation(3, "R&B编曲赏析"),
            observation(4, "Aria《Night》翻唱版合集"),
        ]
        rows = [
            result(1, "song", title="Night", artist="Aria", version="live", evidence=first),
            result(2, "song", title="Road", artist="Fixture Uploader", version="studio",
                   evidence="《Road》录音室版"),
            result(3, "style_lead", style="R&B", evidence="R&B编曲赏析"),
            result(4, "song", title="Night", artist="Aria", version="cover", evidence=inputs[3].title),
        ]
        with patch(REQUEST, return_value={"results": list(reversed(rows))}) as llm:
            extracted = extract_history({}, inputs)
        message = llm.call_args.args[1][1]["content"]
        for forbidden in ("BVFixture", "Fixture Uploader", "fixture.invalid"):
            self.assertNotIn(forbidden, message)
        self.assertEqual(llm.call_args.kwargs["workload"], "album_discovery")
        self.assertEqual([row.kind for row in extracted], ["song", "uncertain", "style_lead", "uncertain"])
        self.assertEqual(extracted[0].version, "live")

    def test_duplicate_numbers_cannot_advance_the_batch(self) -> None:
        batch = [observation(1, "fixture"), observation(2, "fixture")]
        payload = {"results": [result(1, "non_music")] * 2}
        with patch(REQUEST, return_value=payload), self.assertRaises(OpenAIRecommendationError):
            extract_history({}, batch)


class DiscoveryServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.uri = "sqlite:///" + (self.root / "test.sqlite").as_posix()
        db.init_database(self.uri)
        self.addCleanup(db.release_database)
        self.config = {
            "BILIBILI_DISCOVERY": {"enabled": True, "history_api_url": "https://fixture.invalid"},
            "ALBUM_DISCOVERY": {"target_user": "fixture", "library_root": str(self.root)},
        }
        self.user = db.User.create(name="fixture", password="test", salt="test")
        self.folder = db.Folder.create(path=str(self.root), name="Fixture", root=True)
        self.number = 0

    def track(self, name: str, title: str, album: str, *, genre: str = "Rock") -> db.Track:
        artist, _ = db.Artist.get_or_create(name=name)
        release, _ = db.Album.get_or_create(name=album, artist=artist)
        self.number += 1
        path = self.root / f"fixture-{self.number}.flac"
        path.write_bytes(b"offline fixture")
        return db.Track.create(
            title=title, disc=1, number=self.number, duration=180, genre=genre,
            artist=artist, album=release, bitrate=512, path=str(path), last_modification=0,
            folder=self.folder, root_folder=self.folder,
        )

    def test_restarts_resume_frozen_input_and_committed_paid_batch(self) -> None:
        studio = self.track("Aria", "Night", "Original")
        live = self.track("Aria", "Night (Live)", "Concert")
        title = "Aria《Night》现场版"
        observations = [observation(1, title)]
        observations += [observation(n, "Fixture unclassified") for n in range(2, BATCH_SIZE + 2)]
        source = Mock()
        source.read_day.side_effect = [HistoryError("history_request_unavailable"), observations]
        first = [result(1, "song", title="Night", artist="Aria", version="live", evidence=title)]
        first += [result(n, "non_music") for n in range(2, BATCH_SIZE + 1)]
        payloads = [
            {"results": first},
            OpenAIRecommendationError("unavailable in fixture", reason="budget"),
            {"results": [result(1, "non_music")]},
        ]
        # Local readiness should not depend on optional CN credentials.
        self.config["ALBUM_DISCOVERY"]["apple_music_developer_token_file"] = str(self.root / "absent")
        with patch(HISTORY, return_value=source), patch(REQUEST, side_effect=payloads) as llm:
            self.assertEqual(run_bilibili_discovery(self.config, now=WAKE - timedelta(seconds=1))["status"], "idle")
            self.assertEqual(run_bilibili_discovery(self.config, now=WAKE)["reason"], "history_request_unavailable")
            run = db.BilibiliDiscoveryRun.get()
            self.assertIsNotNone(run.sync_confirmed_at)
            self.assertIsNone(run.snapshot_json)
            later = WAKE + timedelta(minutes=6)
            self.assertEqual(run_bilibili_discovery(self.config, now=later)["observations"], len(observations))
            self.assertEqual(run_bilibili_discovery(self.config, now=later)["extract_offset"], BATCH_SIZE)
            self.assertEqual(run_bilibili_discovery(self.config, now=later)["reason"], "extract_budget")
            self.assertEqual(db.BilibiliDiscoveryRun.get().extract_offset, BATCH_SIZE)
            self.assertEqual(run_bilibili_discovery(self.config, now=later)["status"], "ready")
            later += timedelta(minutes=6)
            self.assertEqual(run_bilibili_discovery(self.config, now=later)["extract_offset"], BATCH_SIZE + 1)
            self.assertEqual(run_bilibili_discovery(self.config, now=later)["status"], "done")
            run_bilibili_discovery(self.config, now=later)
            self.assertEqual(run_bilibili_discovery(self.config, now=later)["status"], "idle")
        self.assertEqual(source.sync.call_count, 1)
        self.assertEqual(source.read_day.call_count, 2)
        self.assertEqual(llm.call_count, 3)
        self.assertEqual(db.BilibiliDiscoveryRun.select().count(), 1)
        candidate = db.BilibiliDiscoveryCandidate.get()
        self.assertEqual(candidate.track_id, live.id)
        self.assertNotEqual(candidate.track_id, studio.id)
        self.assertEqual(json.loads(candidate.source_evidence_json)[0]["kind"], "direct")
        self.assertEqual(db.User_Play_Activity.select().count(), 0)
        self.assertEqual(db.AlbumDiscoverySelection.select().count(), 0)

    def test_valid_empty_day_does_not_call_model_or_produce_candidates(self) -> None:
        source = Mock()
        source.read_day.return_value = ()
        with patch(HISTORY, return_value=source), patch(REQUEST) as request:
            self.assertEqual(run_bilibili_discovery(self.config, now=WAKE)["observations"], 0)
            self.assertEqual(run_bilibili_discovery(self.config, now=WAKE)["status"], "idle")
        request.assert_not_called()
        self.assertEqual(db.BilibiliDiscoveryRun.get().status, "done")
        self.assertEqual(db.BilibiliDiscoveryCandidate.select().count(), 0)

    def test_scheduler_repairs_stale_ready_release_and_records_deleted_link(self) -> None:
        run = db.BilibiliDiscoveryRun.create(user=self.user, source_day=DAY, status="done")
        track = self.track("Aria", "Sea", "Studio Sessions")
        ready_at = WAKE.astimezone(timezone.utc).replace(tzinfo=None)
        release = db.AlbumDiscoveryRelease.create(
            library_key="main", album_id="900", album_url="https://music.apple.com/cn/album/900",
            title="Studio Sessions", artist="Aria", expected_tracks="{}", reconciliation="{}",
            ready_at=ready_at,
        )
        row = db.BilibiliDiscoveryCandidate.create(
            run=run, identity_key="cn:101", title="Sea", artist="Aria", version="studio",
            track=track, ready_at=ready_at, release=release, apple_song_id="101", status="ready",
        )
        result = run_bilibili_discovery(self.config, now=WAKE)
        self.assertEqual(result["reason"], "verified_track_unavailable")
        self.assertIsNone(db.AlbumDiscoveryRelease.get_by_id(release.id).ready_at)
        row = db.BilibiliDiscoveryCandidate.get_by_id(row.id)
        self.assertEqual(row.status, "waiting_album")
        self.assertIsNone(row.track_id)
        release.delete_instance()
        result = run_bilibili_discovery(self.config, now=WAKE + timedelta(minutes=6))
        self.assertEqual(result["reason"], "album_link_missing")
        self.assertEqual(db.BilibiliDiscoveryCandidate.get_by_id(row.id).status, "skipped")

    def test_verified_unversioned_joint_credit_and_supported_style_deduplicate(self) -> None:
        for name in ("Seed A", "Seed B"):
            repeated = self.track(name, "Seed", name + " Album")
            for days_ago in (0, 1, 2):
                played = (MIDNIGHT + timedelta(hours=18) - timedelta(days=days_ago)).astimezone()
                db.User_Play_Activity.create(track=repeated, user=self.user, time=played.replace(tzinfo=None))
        target = self.track("Nora", "River", "Demo Album")
        original = catalog_song("101", "Together", "Aria & Nora", "10", "11")
        related = catalog_song("102", "River", "Nora", "11")
        wrong_style = catalog_song("103", "Wrong", "Nora", "11", genre="K-Pop")
        collaboration_text = "Aria 与 Nora 合作《Together》"
        style_text = "Rock赏析"
        unsupported_text = "K-Pop编曲"
        source = Mock()
        source.read_day.return_value = [
            observation(1, collaboration_text), observation(2, style_text),
            observation(3, unsupported_text),
        ]
        catalog = Mock()
        catalog.search_seed_songs.return_value = (original,)
        catalog.get_song.side_effect = {"101": original, "102": related}.get
        catalog.get_artists.return_value = (CatalogArtist("10", "Aria"), CatalogArtist("11", "Nora"))
        catalog.top_songs.return_value = (related, original)
        catalog.style_songs.return_value = (wrong_style, related)
        service = AlbumDiscoveryService(
            self.config, load_album_discovery_runtime(self.config), catalog=catalog, oracle=None,
        )
        rows = [
            result(1, "collaboration_lead", title="Together", artist="Aria", collaborator="Nora",
                   evidence=collaboration_text),
            result(2, "style_lead", style="Rock", evidence=style_text),
            result(3, "style_lead", style="K-Pop", evidence=unsupported_text),
        ]
        with patch(HISTORY, return_value=source), patch(REQUEST, return_value={"results": rows}):
            with patch(ALBUM_SERVICE, return_value=service):
                for _ in range(6):
                    run_bilibili_discovery(self.config, now=WAKE)
        candidate = db.BilibiliDiscoveryCandidate.get()
        self.assertEqual(candidate.track_id, target.id)
        self.assertEqual(candidate.status, "ready")
        self.assertIsNone(candidate.apple_song_id)
        evidence = {item["kind"]: item for item in json.loads(candidate.source_evidence_json)}
        self.assertEqual(set(evidence), {"collaboration", "style"})
        self.assertEqual(evidence["collaboration"]["basis"]["credit_song_id"], "101")
        self.assertEqual(evidence["collaboration"]["basis"]["related_artist_id"], "11")
        self.assertEqual(len(evidence["style"]["basis"]["listening_support"]), 2)
        catalog.top_songs.assert_called_once_with("11")
        catalog.style_songs.assert_called_once_with("摇滚")
        catalog.get_release.assert_not_called()
        progress = json.loads(db.BilibiliDiscoveryRun.get().discovery_progress_json)
        self.assertEqual(progress["explored_song_ids"], ["102"])
        self.assertEqual(progress["outcomes"]["style_unsupported_or_over_limit"], 1)

    def test_unreadable_cover_feedback_and_live_establish_distinct_local_results(self) -> None:
        live = self.track("Aria", "Sea (Live)", "Concert")
        studio = self.track("Aria", "Sea", "Studio Sessions")
        self.track("Another Performer", "Sea (Cover)", "Cover Collection")
        self.assertEqual(find_local_recording(self.user, self.root, "Sea", "Aria", "live").track.id, live.id)
        self.assertEqual(find_local_recording(self.user, self.root, "Sea", "Aria", "cover").reason, "local_missing")
        set_recommendation_feedback(self.user, str(studio.id), "dislike")
        self.assertEqual(find_local_recording(self.user, self.root, "Sea", "Aria", "studio").reason, "negative_feedback")
        Path(live.path).unlink()
        self.assertEqual(find_local_recording(self.user, self.root, "Sea", "Aria", "live").reason, "local_unreadable")

    def test_sibling_artist_alias_survives_ready_revalidation(self) -> None:
        track = self.track("Recorded Alias", "Sea", "Album")
        canonical = db.Artist.create(name="Canonical Artist")
        db.Artist.update(real_artist=canonical).where(db.Artist.id == track.artist_id).execute()
        db.Artist.create(name="Source Alias", real_artist=canonical)
        run = db.BilibiliDiscoveryRun.create(user=self.user, source_day=DAY, status="done")
        row = db.BilibiliDiscoveryCandidate.create(
            run=run, identity_key="test-alias", title="Sea", artist="Source Alias", version="studio",
        )
        outcome = find_local_recording(self.user, self.root, row.title, row.artist, row.version)
        self.assertEqual(outcome.track.id, track.id)
        self.assertTrue(local_track_matches_candidate(outcome.track, row))

    def test_cn_catalog_target_requires_recording_and_complete_release(self) -> None:
        studio = catalog_song("101", "Sea", "Aria", "10", album="Concert")
        live = catalog_song("102", "Sea (Live)", "Aria", "10", album="Concert")
        filler = CatalogTrack("103", "Echo (Live)", "Aria", 1, 2, 199000)
        release = CatalogAlbum("900", "Concert", "Aria", "https://music.apple.com/cn/album/concert/900",
                               (live.track, filler))
        catalog = Mock()
        catalog.search_seed_songs.return_value = (studio, live)
        catalog.get_song.side_effect = {"101": studio, "102": live}.get
        catalog.get_release.return_value = release
        self.assertEqual(resolve_catalog_recording(catalog, "Sea", "Aria", "live"), (live, release))
        catalog.get_release.assert_called_once_with("900", refresh=True)
        for invalid in (
            replace(release, url="https://music.apple.com/hk/album/concert/900"),
            replace(release, tracks=(live.track, replace(filler, track_number=3))),
            replace(release, tracks=(studio.track, filler)),
        ):
            with self.subTest(release=invalid):
                catalog.get_release.return_value = invalid
                with self.assertRaisesRegex(MatchError, "catalog_release_unverified"):
                    resolve_catalog_recording(catalog, "Sea", "Aria", "live")


if __name__ == "__main__":
    unittest.main()
