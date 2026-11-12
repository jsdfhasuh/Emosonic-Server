import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from supysonic import db
from supysonic import track_metadata_background as background
from supysonic.metadata_budget import BudgetExceeded, BudgetUnavailable
from supysonic.scanner_func import scanner_track_enrich

from ..testbase import TestConfig
from .test_track_metadata_web import response as web_response


def metadata_result(confidence=0.3, language="zh"):
    return {"language": language, "mood": ["calm"], "scene": ["reading"],
            "tags": ["dreamy"], "summary": "Existing description.",
            "energy": 25, "valence": 40, "danceability": 30,
            "confidence": confidence, "provider": "llm", "source": "llm",
            "model": "gpt-6-luna", "raw": {"original": "retained"}}


def inconclusive_web_result():
    result = metadata_result(0, None)
    result.update(mood=[], scene=[], tags=[], summary=None,
                  energy=None, valence=None, danceability=None)
    result["raw"] = {"web_lookup": {
        "status": "inconclusive", "identity_verified": False,
    }}
    return result


class FakeProvider:
    name = "llm"
    source = "llm"
    model = "gpt-6-luna"

    def __init__(self, web_enabled=True):
        self.web_enabled = web_enabled
        self.text_calls = []
        self.web_calls = []
        self.text_result = metadata_result()
        self.web_result = metadata_result(0.8, "yue")
        self.web_result["raw"] = {"web_lookup": {
            "status": "completed", "identity_verified": True,
            "sources": [{"url": "https://music.example.com/album/1"}],
        }}
        self.before_web_error = None
        self.text_hook = None
        self.web_hook = None

    def enrich(self, track_input):
        self.text_calls.append(track_input)
        if self.text_hook:
            self.text_hook(track_input)
        return copy.deepcopy(self.text_result)

    def enrich_web(self, track_input, before_request):
        if self.before_web_error:
            raise self.before_web_error
        before_request()
        self.web_calls.append(track_input)
        if self.web_hook:
            self.web_hook(track_input)
        return copy.deepcopy(self.web_result)


class TrackMetadataBackgroundTestCase(unittest.TestCase):
    def setUp(self):
        self._db_file = tempfile.mkstemp()
        self.config = TestConfig(False, False)
        self.config.BASE["database_uri"] = "sqlite:///" + self._db_file[1]
        db.init_database(self.config.BASE["database_uri"])
        self.root = db.Folder.create(root=True, name="Root", path="music")
        self.folder = db.Folder.create(root=False, name="Album", path="music/album", parent=self.root)
        self.artist = db.Artist.create(name="Background Test Artist")
        self.album = db.Album.create(name="Background Test Album", artist=self.artist, year="2024")
        self.counter = 0

    def tearDown(self):
        db.release_database()
        os.close(self._db_file[0])
        os.remove(self._db_file[1])

    def create_track(self):
        self.counter += 1
        return db.Track.create(
            disc=1, number=self.counter, title="Background Song " + str(self.counter),
            album=self.album, artist=self.artist, duration=180, has_art=False,
            bitrate=256, path="music/album/track%d.flac" % self.counter,
            genre="Dream Pop", year=2024, last_modification=100,
            root_folder=self.root, folder=self.folder,
        )

    def apply(self, track, confidence=0.3, language="zh"):
        metadata = scanner_track_enrich.applyTrackMetadataEnrichment(
            track, metadata_result(confidence, language),
        )
        scanner_track_enrich.recordTrackEnrichmentAttempt(
            track, FakeProvider(), "completed", reason="metadata_missing",
        )
        return metadata

    def metadata(self, track):
        return db.TrackMetadata.get(db.TrackMetadata.track == track)

    def budgeted_provider(self, **overrides):
        config = {
            "api_base_url": "https://api.openai.com/v1", "api_key": "fake-key",
            "model": "gpt-6-luna", "monthly_budget_usd": 60,
            "token_limit_parameter": "max_completion_tokens", "web_search_enabled": True,
        }
        config.update(overrides)
        return background.BudgetedMetadataProvider(config)

    def test_completed_low_confidence_is_automatically_selected_for_web(self):
        track = self.create_track()
        self.apply(track)
        provider = FakeProvider()
        summary = background.runAutomaticMetadataPass(3, provider)
        self.assertEqual(summary["web_enriched"], 1)
        self.assertEqual(len(provider.web_calls), 1)
        self.assertEqual(provider.text_calls, [])
        self.assertEqual(self.metadata(track).language, "yue")
        self.assertEqual(db.TrackMetadataEnrichmentTask.get().status, "completed")

    def test_high_quality_current_metadata_is_skipped(self):
        track = self.create_track()
        self.apply(track, 0.9)
        provider = FakeProvider()
        summary = background.runAutomaticMetadataPass(3, provider)
        self.assertEqual(summary["selected"], 0)
        self.assertEqual(provider.text_calls + provider.web_calls, [])

    def test_missing_track_can_run_text_then_web_in_one_pass(self):
        track = self.create_track()
        provider = FakeProvider()
        summary = background.runAutomaticMetadataPass(2, provider)
        self.assertEqual(summary["enriched"], 2)
        self.assertEqual(len(provider.text_calls), 1)
        self.assertEqual(len(provider.web_calls), 1)
        self.assertEqual(self.metadata(track).confidence, 0.8)
        self.assertNotIn("path_hints", provider.web_calls[0])

    def test_terminal_web_marker_survives_new_provider_and_database_reopen(self):
        track = self.create_track()
        self.apply(track)
        first = FakeProvider()
        first.web_result = inconclusive_web_result()
        first_summary = background.runAutomaticMetadataPass(2, first)
        self.assertEqual(first_summary["failed"], 0)
        self.assertEqual(len(first.web_calls), 1)
        db.release_database()
        db.init_database(self.config.BASE["database_uri"])
        second = FakeProvider()
        summary = background.runAutomaticMetadataPass(2, second)
        self.assertEqual(summary["selected"], 0)
        self.assertEqual(second.web_calls, [])
        self.assertIn("web_lookup", json.loads(self.metadata(track).raw_json))

    def test_budget_rejection_before_marker_remains_eligible_after_reset(self):
        track = self.create_track()
        self.apply(track)
        first = FakeProvider()
        first.before_web_error = BudgetExceeded("day", datetime.now(timezone.utc) + timedelta(hours=1))
        summary = background.runAutomaticMetadataPass(2, first)
        self.assertTrue(summary["budget_paused"])
        self.assertNotIn("web_lookup", json.loads(self.metadata(track).raw_json))
        self.assertEqual(first.web_calls, [])
        db.Meta.update(value="0").where(db.Meta.key == background.PAUSE_KEY).execute()
        db.TrackMetadataEnrichmentTask.update(next_retry_at=db.now() - timedelta(seconds=1)).execute()
        second = FakeProvider()
        background.runAutomaticMetadataPass(2, second)
        self.assertEqual(len(second.web_calls), 1)

    def test_started_marker_is_durable_before_network(self):
        track = self.create_track()
        self.apply(track)
        provider = FakeProvider()
        observed = []

        def inspect_marker(unused):
            observed.append(json.loads(self.metadata(track).raw_json)["web_lookup"]["status"])

        provider.web_hook = inspect_marker
        background.runAutomaticMetadataPass(2, provider)
        self.assertEqual(observed, ["started"])

    def test_worse_or_inconclusive_result_does_not_overwrite_good_fields(self):
        track = self.create_track()
        original = self.apply(track, confidence=0.4)
        provider = FakeProvider()
        provider.web_result = inconclusive_web_result()
        summary = background.runAutomaticMetadataPass(2, provider)
        self.assertEqual(summary["failed"], 0)
        current = self.metadata(track)
        for name in ("language", "mood_json", "summary", "confidence", "energy", "valence", "danceability"):
            self.assertEqual(getattr(current, name), getattr(original, name))
        self.assertEqual(json.loads(current.raw_json)["original"], "retained")
        self.assertEqual(json.loads(current.raw_json)["web_lookup"]["status"], "inconclusive")

    def test_improved_candidate_preserves_high_quality_base_abstained_fields(self):
        track = self.create_track()
        original = self.apply(track, confidence=0.8, language=None)
        provider = FakeProvider()
        provider.web_result.update(confidence=0.9, language="yue", mood=[], summary=None,
                                   energy=None, valence=None, danceability=None)
        background.runAutomaticMetadataPass(2, provider)
        current = self.metadata(track)
        self.assertEqual(current.language, "yue")
        self.assertEqual(current.get_moods(), original.get_moods())
        self.assertEqual(current.summary, "Existing description.")
        self.assertEqual(current.energy, 25)
        self.assertEqual(current.valence, 40)
        self.assertEqual(current.danceability, 30)

    def test_improved_candidate_does_not_promote_old_low_confidence_numbers(self):
        track = self.create_track()
        self.apply(track, confidence=0.3)
        provider = FakeProvider()
        provider.web_result.update(energy=None, valence=None, danceability=None)
        background.runAutomaticMetadataPass(2, provider)
        current = self.metadata(track)
        self.assertEqual(current.confidence, 0.8)
        self.assertIsNone(current.energy)
        self.assertIsNone(current.valence)
        self.assertIsNone(current.danceability)

    def test_tag_change_during_network_skips_result(self):
        track = self.create_track()
        self.apply(track)
        provider = FakeProvider()
        provider.web_hook = lambda unused: db.Track.update(last_modification=200).where(db.Track.id == track.id).execute()
        summary = background.runAutomaticMetadataPass(2, provider)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["web_enriched"], 0)
        self.assertEqual(self.metadata(track).track_last_modification, 100)
        self.assertEqual(self.metadata(track).confidence, 0.3)
        self.assertEqual(db.TrackMetadataEnrichmentTask.get().status, "pending")

    def test_global_pause_blocks_all_provider_calls(self):
        self.create_track()
        db.Meta.create(key=background.PAUSE_KEY, value=str(int((datetime.now() + timedelta(hours=1)).timestamp())))
        provider = FakeProvider()
        with patch.object(background, "_recoverStaleTrackMetadataTasks") as recover:
            summary = background.runAutomaticMetadataPass(3, provider)
        self.assertTrue(summary["budget_paused"])
        self.assertEqual(provider.text_calls + provider.web_calls, [])
        recover.assert_not_called()

    def test_background_never_uses_full_library_count_helpers(self):
        self.create_track()
        with patch.object(scanner_track_enrich, "countTracksNeedingEnrichment", side_effect=AssertionError("full scan")), \
                patch.object(scanner_track_enrich, "countTracksMissingCurrentMetadata", side_effect=AssertionError("full scan")), \
                patch.object(scanner_track_enrich, "collectTracksNeedingEnrichment", side_effect=AssertionError("full scan")):
            summary = background.runAutomaticMetadataPass(1, FakeProvider(web_enabled=False))
        self.assertEqual(summary["enriched"], 1)

    def test_stale_candidate_is_rechecked_after_claim(self):
        track = self.create_track()
        original_claim = background._claimTrackMetadataEnrichmentTask

        def concurrent_completion(*args, **kwargs):
            task = original_claim(*args, **kwargs)
            scanner_track_enrich.applyTrackMetadataEnrichment(track, metadata_result(0.9))
            return task

        provider = FakeProvider()
        with patch.object(background, "_claimTrackMetadataEnrichmentTask", side_effect=concurrent_completion):
            summary = background.runAutomaticMetadataPass(1, provider)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(provider.text_calls + provider.web_calls, [])

    def test_stale_candidate_does_not_bypass_new_retry_deadline(self):
        track = self.create_track()
        scanner_track_enrich.recordTrackEnrichmentAttempt(track, FakeProvider(), "pending")
        original_claim = background._claimTrackMetadataEnrichmentTask

        def concurrent_backoff(*args, **kwargs):
            db.TrackMetadataEnrichmentTask.update(
                status="retry", next_retry_at=db.now() + timedelta(hours=1),
            ).where(db.TrackMetadataEnrichmentTask.track == track).execute()
            return original_claim(*args, **kwargs)

        provider = FakeProvider()
        with patch.object(background, "_claimTrackMetadataEnrichmentTask", side_effect=concurrent_backoff):
            background.runAutomaticMetadataPass(1, provider)
        self.assertEqual(provider.text_calls + provider.web_calls, [])

    def test_web_failure_does_not_repeat_after_pause_expires(self):
        track = self.create_track()
        self.apply(track)
        first = FakeProvider()

        def fail(unused):
            raise RuntimeError("Uncertain network outcome")

        first.web_hook = fail
        summary = background.runAutomaticMetadataPass(2, first)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(json.loads(self.metadata(track).raw_json)["web_lookup"]["status"], "failed")
        db.Meta.update(value="0").where(db.Meta.key == background.PAUSE_KEY).execute()
        db.TrackMetadataEnrichmentTask.update(next_retry_at=db.now() - timedelta(seconds=1)).execute()
        second = FakeProvider()
        background.runAutomaticMetadataPass(2, second)
        self.assertEqual(second.web_calls, [])

    def test_real_wrapper_reserves_marks_posts_and_settles_in_order(self):
        track = self.create_track()
        metadata = self.apply(track)
        provider = background.BudgetedMetadataProvider({
            "api_base_url": "https://api.openai.com/v1", "api_key": "fake-key",
            "model": "gpt-6-luna", "monthly_budget_usd": 60,
            "token_limit_parameter": "max_completion_tokens", "web_search_enabled": True,
        })
        events = []
        provider.budget = Mock()
        provider.budget.reserve.side_effect = lambda *args: events.append("reserve") or "reservation"
        provider.budget.settle.side_effect = lambda *args: events.append("settle")

        def mark():
            background._mark_web_attempt(metadata)
            events.append("marker")

        def post(*args, **kwargs):
            self.assertEqual(json.loads(self.metadata(track).raw_json)["web_lookup"]["status"], "started")
            events.append("post")
            return web_response()

        with patch.object(background, "post_web_response", side_effect=post):
            provider.enrich_web(scanner_track_enrich.buildTrackMetadataInput(track), mark)
        self.assertEqual(events, ["reserve", "marker", "post", "settle"])

    def test_real_wrapper_budget_denial_never_marks_or_posts(self):
        provider = background.BudgetedMetadataProvider({
            "api_base_url": "https://api.openai.com/v1", "api_key": "fake-key",
            "model": "gpt-6-luna", "monthly_budget_usd": 60,
            "token_limit_parameter": "max_completion_tokens", "web_search_enabled": True,
        })
        provider.budget = Mock()
        provider.budget.reserve.side_effect = BudgetExceeded("day", datetime.now(timezone.utc) + timedelta(days=1))
        mark = Mock()
        with patch.object(background, "post_web_response") as post:
            with self.assertRaises(BudgetExceeded):
                provider.enrich_web({"title": "Song", "artist": "Artist"}, mark)
        mark.assert_not_called()
        post.assert_not_called()
        provider.budget.settle.assert_not_called()

    def test_real_monthly_limit_denies_text_and_web_before_http(self):
        provider = self.budgeted_provider(daily_budget_usd=100)
        provider.budget.reserve("text", 57_000_000)
        before = provider.budget.snapshot()
        mark = Mock()
        with patch.object(background.requests, "post") as text_post, \
                patch.object(background, "post_web_response") as web_post:
            with self.assertRaises(BudgetExceeded) as text_error:
                provider.enrich({"title": "Song", "artist": "Artist"})
            with self.assertRaises(BudgetExceeded) as web_error:
                provider.enrich_web({"title": "Song", "artist": "Artist"}, mark)
        self.assertEqual(text_error.exception.reason, "month_limit")
        self.assertEqual(web_error.exception.reason, "month_limit")
        self.assertEqual(provider.budget.snapshot(), before)
        text_post.assert_not_called()
        web_post.assert_not_called()
        mark.assert_not_called()

    def test_unknown_web_accounting_retains_persistent_reservation(self):
        provider = self.budgeted_provider()
        response = web_response()
        response.pop("tool_usage", None)
        mark = Mock()
        with patch.object(background, "post_web_response", return_value=response):
            with self.assertRaises(BudgetUnavailable):
                provider.enrich_web({"title": "Song", "artist": "Artist"}, mark)
        mark.assert_called_once_with()
        self.assertEqual(provider.budget.snapshot()["month_reserved_micro_usd"], 1_000_000)
        self.assertEqual(self.budgeted_provider().budget.snapshot()["month_reserved_micro_usd"], 1_000_000)

    def test_unknown_text_accounting_retains_reservation_and_closes_response(self):
        provider = self.budgeted_provider()
        response = Mock(status_code=200)
        response.json.return_value = {"model": "gpt-6-luna", "service_tier": "default"}
        payload = {"model": "gpt-6-luna", "max_completion_tokens": 1536,
                   "messages": [{"role": "user", "content": "Song"}]}
        with patch.object(background.requests, "post", return_value=response):
            with self.assertRaises(BudgetUnavailable):
                provider._request(payload, "text")
        response.close.assert_called_once_with()
        self.assertGreater(provider.budget.snapshot()["month_reserved_micro_usd"], 0)
        self.assertEqual(provider.budget.snapshot()["month_spent_micro_usd"], 0)

    def test_both_request_paths_explicitly_force_default_service_tier(self):
        provider = self.budgeted_provider()
        provider.budget = Mock()
        text_response = Mock(status_code=200)
        text_response.json.return_value = {
            "model": "gpt-6-luna", "service_tier": "default",
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        }
        text_payload = {"model": "gpt-6-luna", "service_tier": "fast",
                        "max_completion_tokens": 1536,
                        "messages": [{"role": "user", "content": "Song"}]}
        with patch.object(background.requests, "post", return_value=text_response) as text_post, \
                patch.object(background, "post_web_response", return_value=web_response()) as web_post:
            provider._request(text_payload, "text")
            provider.enrich_web({"title": "Song", "artist": "Artist"}, Mock())
        self.assertEqual(text_post.call_args.kwargs["json"]["service_tier"], "default")
        self.assertEqual(web_post.call_args.args[1]["service_tier"], "default")
        self.assertFalse(text_post.call_args.kwargs["allow_redirects"])
        self.assertEqual(provider.budget.settle.call_count, 2)

    def test_unverified_response_tier_never_releases_reservation(self):
        for tier in (None, "fast", "flex"):
            with self.subTest(tier=tier):
                provider = self.budgeted_provider()
                provider.budget = Mock()
                response = web_response()
                if tier is None:
                    response.pop("service_tier", None)
                else:
                    response["service_tier"] = tier
                with patch.object(background, "post_web_response", return_value=response):
                    with self.assertRaises(BudgetUnavailable):
                        provider.enrich_web({"title": "Song", "artist": "Artist"}, Mock())
                provider.budget.reserve.assert_called_once()
                provider.budget.settle.assert_not_called()

    def test_pre_network_marker_failure_refunds_reservation_without_http(self):
        provider = self.budgeted_provider()
        mark = Mock(side_effect=BudgetUnavailable("Marker changed."))
        with patch.object(background, "post_web_response") as post:
            with self.assertRaises(BudgetUnavailable):
                provider.enrich_web({"title": "Song", "artist": "Artist"}, mark)
        post.assert_not_called()
        snapshot = provider.budget.snapshot()
        self.assertEqual(snapshot["month_reserved_micro_usd"], 0)
        self.assertEqual(snapshot["month_spent_micro_usd"], 0)
