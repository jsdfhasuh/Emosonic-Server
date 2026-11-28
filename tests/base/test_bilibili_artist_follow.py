from dataclasses import replace
from datetime import timedelta
import json
import unittest
from unittest.mock import Mock, patch

from supysonic import db
from supysonic.album_discovery_catalog import CatalogArtist
from supysonic.bilibili_discovery.matcher import explicit_source_version
from supysonic.bilibili_discovery.service import run_bilibili_discovery
from supysonic.bilibili_discovery.subscriptions import (
    build_subscription_plan, record_performance, subscribe_performer,
)
from tests.base.test_bilibili_discovery import ALBUM_SERVICE, DAY, HISTORY, REQUEST, WAKE, observation


class PerformerFollowTest(unittest.TestCase):
    def setUp(self):
        db.init_database("sqlite:///:memory:")
        self.addCleanup(db.release_database)
        self.user = db.User.create(name="fixture", password="pw", salt="test")
        self.artist = CatalogArtist("655853528", "CL")

    def test_distinct_videos_date_boundary_and_user_isolation(self):
        previous = {}
        video = observation(1, "CL演唱")
        record_performance(previous, "CL", video)
        db.BilibiliDiscoveryRun.create(user=self.user, source_day=DAY-timedelta(days=1),
                                      discovery_progress_json=json.dumps(previous))
        run = db.BilibiliDiscoveryRun.create(user=self.user, source_day=DAY)
        today = {}
        record_performance(today, "CL", replace(video, cid="another part"))
        self.assertEqual(build_subscription_plan(run, today, 14), [])
        record_performance(today, "CL", observation(2, "CL演唱"))
        self.assertEqual(build_subscription_plan(run, today, 14), [
            {"kind": "subscribe_artist", "performer": "CL", "distinct_videos": 2}])
        old = {"prior_performer_days": {(DAY-timedelta(days=14)).isoformat(): previous["performers"]}}
        db.BilibiliDiscoveryRun.update(discovery_progress_json=json.dumps(old)).where(
            db.BilibiliDiscoveryRun.source_day < DAY).execute()
        today = {}
        record_performance(today, "CL", observation(2, "CL演唱"))
        self.assertEqual(build_subscription_plan(run, today, 14), [])
        other = db.User.create(name="another", password="pw", salt="test")
        db.BilibiliDiscoveryRun.create(user=other, source_day=DAY-timedelta(days=1),
                                      discovery_progress_json=json.dumps(previous))
        self.assertEqual(build_subscription_plan(run, today, 14), [])

    def test_rehearsed_medley_creates_one_performer_follow_and_two_separate_work_probes(self):
        title = "CL 再唱 2NE1 神曲《FIRE + I AM THE BEST》"
        history = Mock()
        history.read_day.return_value = [observation(1, title), observation(2, "CL 演唱现场")]
        config = {
            "BILIBILI_DISCOVERY": {"enabled": True, "auto_subscribe_artists": True},
            "ALBUM_DISCOVERY": {"target_user": "fixture"},
        }
        result = {"id": 1, "evidence": title, "works": [
            {"title": name, "artist": "2NE1", "performer": "CL"} for name in ("FIRE", "I AM THE BEST")]}
        artist = {"id": 2, "kind": "artist_lead", "title": "", "artist": "", "version": "unknown",
                  "performer": "CL", "collaborator": "", "album_hint": "", "style": "",
                  "evidence": "CL 演唱现场"}
        service = Mock()
        service.catalog.search_seed_songs.return_value = []
        service.oracle.search_subscription_artists.return_value = [self.artist]
        service.oracle.ensure_artist_subscription.return_value = "artist_subscription_created"
        with patch(HISTORY, return_value=history), patch(REQUEST, return_value={"results": [result, artist]}), \
             patch(ALBUM_SERVICE, return_value=service), \
             patch("supysonic.bilibili_discovery.service.explore_task", return_value=([], "catalog_work_unverified")):
            for _ in range(6):
                run_bilibili_discovery(config, now=WAKE)
        run = db.BilibiliDiscoveryRun.get()
        self.assertEqual(run.status, "done")
        plan = json.loads(run.discovery_progress_json)["plan"]
        self.assertEqual([t["leads"][0]["artist"] for t in plan if t["kind"] == "work"], ["2NE1"]*2)
        self.assertEqual(plan[-1]["performer"], "CL")
        service.oracle.ensure_artist_subscription.assert_called_once_with(self.artist)
        self.assertEqual(subscribe_performer(self.user, service.oracle, plan[-1]), "artist_subscription_handled")
        service.oracle.ensure_artist_subscription.assert_called_once()
        self.assertEqual(db.AlbumDiscoverySelection.select().count(), 0)

    def test_unresolved_or_ambiguous_performer_keeps_subscription_untouched(self):
        oracle = Mock()
        for hits in ([], [self.artist, CatalogArtist("111", "CL")]):
            oracle.search_subscription_artists.return_value = hits
            self.assertIn(subscribe_performer(self.user, oracle, {"performer": "CL"}),
                          ("artist_subscription_unverified", "artist_subscription_ambiguous"))
        oracle.ensure_artist_subscription.assert_not_called()

    def test_live_version_next_to_chinese_text(self):
        self.assertTrue(explicit_source_version("BESTie 电台Live版", "live"))
        self.assertFalse(explicit_source_version("BESTie Alive", "live"))

    def test_explicit_joint_performance_counts_each_named_performer(self):
        progress = {}
        video = observation(1, "CL+ANNIE+BAILEY+李荣书 合作Fire+我最红 舞台")
        record_performance(progress, "CL+ANNIE+BAILEY+李荣书", video)
        record_performance(progress, "CL", video)
        self.assertEqual(len(progress["performers"]["cl"]["videos"]), 1)
        self.assertEqual(len(progress["performers"]), 4)
