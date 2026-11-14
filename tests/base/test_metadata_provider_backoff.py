import json
import time
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

import requests
from peewee import OperationalError

from supysonic import db, metadata_provider_backoff as backoff
from supysonic import track_metadata_background as background
from supysonic.metadata_budget import BudgetUnavailable
from supysonic.scanner_func.scanner_track_enrich import TrackMetadataInvalidResponseError

from . import test_track_metadata_background as fixtures


def text_response(content=None):
    response = Mock(status_code=200)
    response.json.return_value = {
        "model": "gpt-6-luna", "service_tier": "default",
        "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(fixtures.metadata_result(0.8)) if content is None else content,
        }}],
    }
    return response


class MetadataProviderBackoffTestCase(unittest.TestCase):
    setUp = fixtures.TrackMetadataBackgroundTestCase.setUp
    tearDown = fixtures.TrackMetadataBackgroundTestCase.tearDown
    create_track = fixtures.TrackMetadataBackgroundTestCase.create_track
    budgeted_provider = fixtures.TrackMetadataBackgroundTestCase.budgeted_provider

    def test_historical_attempts_do_not_inflate_first_transient_pause(self):
        track = self.create_track()
        db.TrackMetadataEnrichmentTask.create(track=track, status="retry", reason="provider_error",
                                             attempt_count=19533)
        provider = self.budgeted_provider(web_search_enabled=False)
        started = int(time.time())
        with patch.object(background.requests, "post", side_effect=requests.Timeout("secret")) as post:
            summary = background.runAutomaticMetadataPass(3, provider)
            following = background.runAutomaticMetadataPass(3, provider)
        state = backoff.decode_backoff(backoff.read_backoff())
        self.assertEqual(state["n"], 1)
        self.assertGreaterEqual(state["u"] - started, 300)
        self.assertLessEqual(state["u"] - started, 305)
        self.assertTrue(summary["provider_paused"])
        self.assertTrue(following["provider_paused"])
        self.assertFalse(summary["budget_paused"])
        self.assertIsNone(db.Meta.get_or_none(db.Meta.key == background.PAUSE_KEY))
        task = db.TrackMetadataEnrichmentTask.get()
        self.assertEqual(task.reason, "provider_backoff")
        self.assertEqual(task.attempt_count, 19534)
        post.assert_called_once()
        self.assertGreater(provider.budget.snapshot()["month_reserved_micro_usd"], 0)
        self.assertEqual(provider.budget.snapshot()["month_spent_micro_usd"], 0)

    def test_consecutive_failures_survive_reopen_and_cap_at_thirty_minutes(self):
        for index, expected in enumerate((300, 600, 1200, 1800, 1800, 1800)):
            current = 1_800_000_000 + index * 2000
            with patch.object(backoff.time, "time", return_value=current):
                deadline, count = backoff.record_transient_failure()
            self.assertEqual(int(deadline.timestamp()) - current, expected)
            self.assertEqual(count, min(index + 1, 4))
            db.release_database()
            db.init_database(self.config.BASE["database_uri"])
            self.assertEqual(backoff.decode_backoff(backoff.read_backoff())["n"], count)

    def test_retry_after_can_extend_but_not_shorten_exponential_delay(self):
        current = 1_800_000_000
        with patch.object(backoff.time, "time", return_value=current):
            deadline, unused = backoff.record_transient_failure(7200)
            self.assertEqual(int(deadline.timestamp()), current + 7200)
            deadline, unused = backoff.record_transient_failure(1)
            self.assertEqual(int(deadline.timestamp()), current + 7200)

    def test_successful_accounted_response_resets_old_failures(self):
        with patch.object(backoff.time, "time", return_value=int(time.time()) - 4000):
            backoff.record_transient_failure()
            backoff.record_transient_failure()
        provider = self.budgeted_provider(web_search_enabled=False)
        with patch.object(background.requests, "post", return_value=text_response()):
            provider.enrich({"title": "Song", "artist": "Artist"})
        self.assertIsNone(backoff.read_backoff())
        self.assertEqual(provider.budget.snapshot()["month_reserved_micro_usd"], 0)
        self.assertGreater(provider.budget.snapshot()["month_spent_micro_usd"], 0)
        unused, count = backoff.record_transient_failure()
        self.assertEqual(count, 1)

    def test_settled_invalid_text_resets_service_backoff_without_global_pause(self):
        self.create_track()
        with patch.object(backoff.time, "time", return_value=int(time.time()) - 4000):
            backoff.record_transient_failure()
        provider = self.budgeted_provider(web_search_enabled=False)
        with patch.object(background.requests, "post", return_value=text_response("not JSON")):
            summary = background.runAutomaticMetadataPass(1, provider)
        self.assertEqual(summary["failed"], 1)
        self.assertIsNone(backoff.read_backoff())
        self.assertIsNone(db.Meta.get_or_none(db.Meta.key == background.PAUSE_KEY))
        self.assertEqual(db.TrackMetadataEnrichmentTask.get().reason, "invalid_response")
        self.assertGreater(provider.budget.snapshot()["month_spent_micro_usd"], 0)
        self.assertEqual(provider.budget.snapshot()["month_reserved_micro_usd"], 0)

    def test_invalid_song_result_does_not_block_next_song_in_same_batch(self):
        valid = self.create_track()
        invalid = self.create_track()
        invalid.created = db.now() + timedelta(seconds=1)
        invalid.save()
        provider = fixtures.FakeProvider(web_enabled=False)

        def reject_one(track):
            if track["title"] == invalid.title:
                raise TrackMetadataInvalidResponseError("Unusable song result")

        provider.text_hook = reject_one
        summary = background.runAutomaticMetadataPass(2, provider)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["enriched"], 1)
        self.assertEqual(len(provider.text_calls), 2)
        self.assertIsNotNone(db.TrackMetadata.get_or_none(db.TrackMetadata.track == valid))
        self.assertIsNone(backoff.read_backoff())
        self.assertIsNone(db.Meta.get_or_none(db.Meta.key == background.PAUSE_KEY))

    def test_delayed_success_cannot_clear_newer_concurrent_failure(self):
        backoff.record_transient_failure()
        observed = backoff.read_backoff()
        backoff.record_transient_failure()
        newer = backoff.read_backoff()
        backoff.reset_backoff(observed)
        self.assertEqual(backoff.read_backoff(), newer)
        backoff.reset_backoff(None)
        self.assertEqual(backoff.read_backoff(), newer)

    def test_corrupt_state_aborts_before_claim_or_http(self):
        self.create_track()
        db.Meta.create(key=backoff.BACKOFF_KEY, value='{"n":1}')
        provider = self.budgeted_provider()
        with patch.object(background.requests, "post") as post:
            with self.assertRaises(BudgetUnavailable):
                background.runAutomaticMetadataPass(3, provider)
        post.assert_not_called()
        self.assertEqual(db.TrackMetadataEnrichmentTask.select().count(), 0)

    def test_backoff_storage_failure_stops_batch_and_retains_reservation(self):
        self.create_track()
        self.create_track()
        provider = self.budgeted_provider(web_search_enabled=False)
        original = backoff.record_transient_failure

        def fail_storage(*args):
            with patch.object(backoff.Meta, "insert", side_effect=OperationalError("storage down")):
                return original(*args)

        with patch.object(background.requests, "post", side_effect=requests.Timeout()) as post, \
                patch.object(background, "record_transient_failure", side_effect=fail_storage):
            with self.assertRaises(BudgetUnavailable):
                background.runAutomaticMetadataPass(3, provider)
        post.assert_called_once()
        self.assertGreater(provider.budget.snapshot()["month_reserved_micro_usd"], 0)

    def test_auth_quota_and_bad_request_keep_hard_pause(self):
        for status, code in ((401, "invalid_api_key"), (429, "insufficient_quota"), (400, "invalid_model")):
            with self.subTest(status=status, code=code):
                self.create_track()
                response = Mock(status_code=status, headers={})
                response.json.return_value = {"error": {"code": code, "message": "secret"}}
                provider = self.budgeted_provider(web_search_enabled=False)
                with patch.object(background.requests, "post", return_value=response) as post:
                    summary = background.runAutomaticMetadataPass(1, provider)
                self.assertEqual(summary["failed"], 1)
                self.assertTrue(summary["provider_paused"])
                self.assertIsNone(backoff.read_backoff())
                pause = db.Meta.get(db.Meta.key == background.PAUSE_KEY)
                self.assertGreater(int(pause.value), time.time() + 86000)
                self.assertGreater(provider.budget.snapshot()["month_reserved_micro_usd"], 0)
                post.assert_called_once()
                db.Meta.delete().where(db.Meta.key == background.PAUSE_KEY).execute()

    def test_valid_hard_pause_is_not_overridden_by_transient_state(self):
        deadline = int(time.time()) + 7200
        db.Meta.create(key=background.PAUSE_KEY, value=str(deadline))
        backoff.record_transient_failure()
        provider = self.budgeted_provider()
        with patch.object(background.requests, "post") as post:
            with self.assertRaises(background.MetadataProviderPaused):
                provider.enrich({"title": "Song", "artist": "Artist"})
        post.assert_not_called()
        self.assertEqual(db.Meta.get(db.Meta.key == background.PAUSE_KEY).value, str(deadline))

    def test_reset_storage_failure_stops_after_settlement_without_refund(self):
        self.create_track()
        self.create_track()
        provider = self.budgeted_provider(web_search_enabled=False)
        with patch.object(background.requests, "post", return_value=text_response()) as post, \
                patch.object(background, "reset_backoff", side_effect=OperationalError("storage down")):
            summary = background.runAutomaticMetadataPass(3, provider)
        self.assertEqual(summary["failed"], 1)
        self.assertTrue(summary["provider_paused"])
        post.assert_called_once()
        self.assertGreater(provider.budget.snapshot()["month_spent_micro_usd"], 0)
        self.assertEqual(provider.budget.snapshot()["month_reserved_micro_usd"], 0)

    def test_transient_http_error_logs_safe_status_but_not_provider_text(self):
        self.create_track()
        provider = self.budgeted_provider(web_search_enabled=False)
        response = Mock(status_code=503, headers={"Retry-After": "900"})
        response.json.return_value = {"error": {"message": "private-provider-body"}}
        with patch.object(background.requests, "post", return_value=response) as post, \
                self.assertLogs(background.logger, level="WARNING") as captured:
            summary = background.runAutomaticMetadataPass(3, provider)
        self.assertTrue(summary["provider_paused"])
        self.assertIn("upstream_status=503", captured.output[0])
        self.assertNotIn("private-provider-body", captured.output[0])
        self.assertNotIn("fake-key", captured.output[0])
        self.assertGreaterEqual(backoff.decode_backoff(backoff.read_backoff())["u"], int(time.time()) + 895)
        post.assert_called_once()
