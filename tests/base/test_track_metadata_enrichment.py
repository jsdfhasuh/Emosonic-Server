import json
import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner
from peewee import IntegrityError

from supysonic import db
from supysonic.cli import cli
from supysonic.llm_client import parse_json_object_response
from supysonic.scanner_func.scanner_records import removeFile
from supysonic.scanner_func.scanner_review_tasks import (
    createLowConfidenceTrackMetadataReviewTask,
    createLowConfidenceTrackMetadataReviewTasks,
    getTrackMetadataReviewIssues,
)
from supysonic.scanner_func import scanner_track_enrich
from supysonic.scanner_func.scanner_track_enrich import (
    LLMMetadataProvider,
    LocalMetadataProvider,
    buildTrackMetadataInput,
    collectTracksNeedingEnrichment,
    runTrackMetadataEnrichmentPass,
)

from ..testbase import TestConfig


class StaticLLMMetadataProvider:
    name = "llm"
    model = "test-model"
    source = "llm"

    def __init__(self, confidence=0.8):
        self.confidence = confidence

    def enrich(self, track_input):
        return {
            "language": "en",
            "mood": ["calm"],
            "scene": ["late night"],
            "tags": ["dreamy"],
            "summary": "A calm late night track.",
            "energy": 30,
            "valence": 60,
            "danceability": 40,
            "confidence": self.confidence,
            "provider": self.name,
            "model": self.model,
            "source": self.source,
            "raw": {"provider": self.name},
        }


class TrackMetadataEnrichmentTestCase(unittest.TestCase):
    def setUp(self):
        self._db_file = tempfile.mkstemp()
        self.config = TestConfig(False, False)
        self.config.BASE["database_uri"] = "sqlite:///" + self._db_file[1]
        db.init_database(self.config.BASE["database_uri"])

    def tearDown(self):
        db.release_database()
        os.close(self._db_file[0])
        os.remove(self._db_file[1])

    def _create_track(self, path="music/album/track.flac", folder=None, root=None):
        if root is None:
            root = db.Folder.create(root=True, name="Root", path="music")
        if folder is None:
            folder = db.Folder.create(
                root=False,
                name="Album",
                path="music/album",
                parent=root,
            )
        artist = db.Artist.create(name="Track Metadata Artist")
        album = db.Album.create(name="Track Metadata Album", artist=artist, year="2024")
        return db.Track.create(
            disc=1,
            number=1,
            title="Track Metadata Song",
            album=album,
            artist=artist,
            duration=180,
            has_art=False,
            bitrate=256,
            path=path,
            genre="Dream Pop",
            year=2024,
            last_modification=100,
            root_folder=root,
            folder=folder,
        )

    def test_schema_and_models_create_track_metadata_rows(self):
        columns = {
            row[1]
            for row in db.db.execute_sql("PRAGMA table_info(track_metadata)").fetchall()
        }
        self.assertIn("track_last_modification", columns)
        self.assertIn("confidence", columns)

        track = self._create_track()
        metadata = db.TrackMetadata.create(
            track=track,
            track_last_modification=track.last_modification,
            provider="test",
        )
        task = db.TrackMetadataEnrichmentTask.create(
            track=track,
            status=db.TrackMetadataEnrichmentTask.STATUS_PENDING,
            reason=db.TrackMetadataEnrichmentTask.REASON_METADATA_MISSING,
        )

        self.assertEqual(metadata.track_id, track.id)
        self.assertEqual(task.track_id, track.id)
        with self.assertRaises(IntegrityError):
            db.TrackMetadata.create(
                track=track,
                track_last_modification=track.last_modification,
            )

    def test_collect_tracks_needing_enrichment_uses_metadata_and_task_state(self):
        track = self._create_track()

        self.assertEqual(collectTracksNeedingEnrichment(), [track])

        db.TrackMetadata.create(
            track=track,
            track_last_modification=track.last_modification,
            provider="test",
        )
        self.assertEqual(collectTracksNeedingEnrichment(), [])

        track.last_modification = 200
        track.save()
        self.assertEqual(collectTracksNeedingEnrichment(), [track])

        metadata = db.TrackMetadata.get(db.TrackMetadata.track == track)
        metadata.track_last_modification = track.last_modification
        metadata.save()
        task = db.TrackMetadataEnrichmentTask.create(
            track=track,
            status=db.TrackMetadataEnrichmentTask.STATUS_RETRY,
            reason=db.TrackMetadataEnrichmentTask.REASON_FAILED_RETRY,
            next_retry_at=db.now() + timedelta(hours=1),
        )
        self.assertEqual(collectTracksNeedingEnrichment(), [])

        task.next_retry_at = db.now() - timedelta(seconds=1)
        task.save()
        self.assertEqual(collectTracksNeedingEnrichment(), [track])

    def test_running_stale_task_is_recovered_for_collection(self):
        track = self._create_track()
        db.TrackMetadata.create(
            track=track,
            track_last_modification=track.last_modification,
        )
        task = db.TrackMetadataEnrichmentTask.create(
            track=track,
            status=db.TrackMetadataEnrichmentTask.STATUS_RUNNING,
            reason=db.TrackMetadataEnrichmentTask.REASON_MANUAL,
            locked_at=db.now() - timedelta(hours=1),
        )

        self.assertEqual(
            collectTracksNeedingEnrichment(stale_lock_seconds=1),
            [track],
        )
        task = db.TrackMetadataEnrichmentTask.get_by_id(task.id)
        self.assertEqual(task.status, db.TrackMetadataEnrichmentTask.STATUS_RETRY)

    def test_collect_tracks_needing_enrichment_limit_zero_has_no_side_effects(self):
        track = self._create_track()
        task = db.TrackMetadataEnrichmentTask.create(
            track=track,
            status=db.TrackMetadataEnrichmentTask.STATUS_RUNNING,
            reason=db.TrackMetadataEnrichmentTask.REASON_MANUAL,
            locked_at=db.now() - timedelta(hours=1),
        )

        self.assertEqual(collectTracksNeedingEnrichment(limit=0), [])
        task = db.TrackMetadataEnrichmentTask.get_by_id(task.id)
        self.assertEqual(task.status, db.TrackMetadataEnrichmentTask.STATUS_RUNNING)

    def test_run_track_metadata_enrichment_dry_run_does_not_write_metadata(self):
        track = self._create_track()

        summary = runTrackMetadataEnrichmentPass(dry_run=True)

        self.assertEqual(summary["selected"], 1)
        self.assertEqual(summary["tracks"][0]["id"], str(track.id))
        self.assertEqual(db.TrackMetadata.select().count(), 0)
        self.assertEqual(db.TrackMetadataEnrichmentTask.select().count(), 0)

    def test_run_track_metadata_enrichment_local_provider_writes_metadata(self):
        track = self._create_track()

        summary = runTrackMetadataEnrichmentPass(provider=LocalMetadataProvider())

        self.assertEqual(summary["enriched"], 1)
        metadata = db.TrackMetadata.get(db.TrackMetadata.track == track)
        self.assertEqual(metadata.provider, "local")
        self.assertIn("Dream Pop", metadata.get_tags())
        task = db.TrackMetadataEnrichmentTask.get(
            db.TrackMetadataEnrichmentTask.track == track
        )
        self.assertEqual(task.status, db.TrackMetadataEnrichmentTask.STATUS_COMPLETED)
        self.assertEqual(task.attempt_count, 1)

    def test_local_provider_does_not_create_track_review_task(self):
        track = self._create_track()

        runTrackMetadataEnrichmentPass(provider=LocalMetadataProvider())

        metadata = db.TrackMetadata.get(db.TrackMetadata.track == track)
        self.assertEqual(metadata.provider, "local")
        self.assertEqual(getTrackMetadataReviewIssues(metadata), [])
        self.assertIsNone(
            db.ReviewTask.get_or_none(
                db.ReviewTask.entity_type == "track",
                db.ReviewTask.entity_id == str(track.id),
                db.ReviewTask.reason == "low_confidence",
            )
        )

    def test_low_confidence_llm_enrichment_creates_track_review_task(self):
        track = self._create_track()

        runTrackMetadataEnrichmentPass(
            provider=StaticLLMMetadataProvider(confidence=0.2)
        )

        task = db.ReviewTask.get(
            db.ReviewTask.entity_type == "track",
            db.ReviewTask.entity_id == str(track.id),
            db.ReviewTask.reason == "low_confidence",
        )
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.pending_key, f"track:{track.id}:pending:low_confidence")
        snapshot = json.loads(task.snapshot_json)
        self.assertEqual(snapshot["track_title"], "Track Metadata Song")
        self.assertEqual(snapshot["issues"], ["low_confidence"])
        self.assertEqual(snapshot["metadata"]["confidence"], 0.2)

    def test_high_confidence_track_metadata_confirms_pending_review_task(self):
        track = self._create_track()
        metadata = db.TrackMetadata.create(
            track=track,
            track_last_modification=track.last_modification,
            confidence=0.2,
            provider="llm",
            source="llm",
        )
        task = db.ReviewTask.create(
            entity_type="track",
            entity_id=str(track.id),
            task_type="metadata_review",
            status="pending",
            reason="low_confidence",
            snapshot_json="{}",
        )
        metadata.confidence = 0.9
        metadata.save()

        updated_count = createLowConfidenceTrackMetadataReviewTask(track, metadata)

        self.assertEqual(updated_count, 1)
        refreshed_task = db.ReviewTask.get_by_id(task.id)
        self.assertEqual(refreshed_task.status, "confirmed")
        self.assertIsNotNone(refreshed_task.resolved_at)

    def test_local_track_metadata_confirms_existing_pending_review_task(self):
        track = self._create_track()
        metadata = db.TrackMetadata.create(
            track=track,
            track_last_modification=track.last_modification,
            confidence=0.2,
            provider="local",
            source="local",
        )
        task = db.ReviewTask.create(
            entity_type="track",
            entity_id=str(track.id),
            task_type="metadata_review",
            status="pending",
            reason="low_confidence",
            snapshot_json="{}",
        )

        updated_count = createLowConfidenceTrackMetadataReviewTask(track, metadata)

        self.assertEqual(updated_count, 1)
        refreshed_task = db.ReviewTask.get_by_id(task.id)
        self.assertEqual(refreshed_task.status, "confirmed")
        snapshot = json.loads(refreshed_task.snapshot_json)
        self.assertEqual(snapshot["issues"], [])

    def test_bulk_low_confidence_track_review_skips_local_metadata(self):
        root = db.Folder.create(root=True, name="Root", path="music")
        folder = db.Folder.create(
            root=False,
            name="Album",
            path="music/album",
            parent=root,
        )
        local_track = self._create_track(
            path="music/album/local.flac",
            root=root,
            folder=folder,
        )
        llm_track = self._create_track(
            path="music/album/llm.flac",
            root=root,
            folder=folder,
        )
        db.TrackMetadata.create(
            track=local_track,
            track_last_modification=local_track.last_modification,
            confidence=0.1,
            provider="local",
            source="local",
        )
        db.TrackMetadata.create(
            track=llm_track,
            track_last_modification=llm_track.last_modification,
            confidence=0.1,
            provider="llm",
            source="llm",
        )

        created = createLowConfidenceTrackMetadataReviewTasks()

        self.assertEqual(created, 1)
        self.assertIsNone(
            db.ReviewTask.get_or_none(
                db.ReviewTask.entity_type == "track",
                db.ReviewTask.entity_id == str(local_track.id),
            )
        )
        self.assertIsNotNone(
            db.ReviewTask.get_or_none(
                db.ReviewTask.entity_type == "track",
                db.ReviewTask.entity_id == str(llm_track.id),
                db.ReviewTask.reason == "low_confidence",
            )
        )

    def test_build_track_metadata_input_does_not_include_absolute_path(self):
        track = self._create_track(path="/private/music/Album Name/Track Name.flac")

        payload = buildTrackMetadataInput(track)

        self.assertEqual(payload["file_name"], "Track Name.flac")
        self.assertNotIn("/private/music", str(payload))
        self.assertNotIn("path_hints", payload)

        payload = buildTrackMetadataInput(track, include_path_hints=True)

        self.assertNotIn("/private/music", str(payload))
        self.assertIn("Track Name.flac", payload["path_hints"])
        self.assertIn("Album Name", payload["path_hints"])

    def test_collect_tracks_needing_enrichment_paginates_until_limit_is_filled(self):
        root = db.Folder.create(root=True, name="Root", path="music")
        folder = db.Folder.create(
            root=False,
            name="Album",
            path="music/album",
            parent=root,
        )
        for index in range(105):
            track = self._create_track(
                path=f"music/album/current-{index}.flac",
                root=root,
                folder=folder,
            )
            db.TrackMetadata.create(
                track=track,
                track_last_modification=track.last_modification,
            )
        candidate = self._create_track(
            path="music/album/candidate.flac",
            root=root,
            folder=folder,
        )

        self.assertEqual(collectTracksNeedingEnrichment(limit=1), [candidate])

    def test_remove_file_cleans_track_metadata_rows(self):
        track = self._create_track()
        db.TrackMetadata.create(
            track=track,
            track_last_modification=track.last_modification,
        )
        db.TrackMetadataEnrichmentTask.create(
            track=track,
            status=db.TrackMetadataEnrichmentTask.STATUS_PENDING,
            reason=db.TrackMetadataEnrichmentTask.REASON_METADATA_MISSING,
        )
        stats = SimpleNamespace(deleted=SimpleNamespace(tracks=0))
        scanner = SimpleNamespace(stats=lambda: stats)

        removeFile(scanner, track.path)

        self.assertEqual(stats.deleted.tracks, 1)
        self.assertEqual(db.TrackMetadata.select().count(), 0)
        self.assertEqual(db.TrackMetadataEnrichmentTask.select().count(), 0)

    def test_delete_hierarchy_cleans_metadata_for_child_folder_id_deletion(self):
        root = db.Folder.create(root=True, name="Root", path="library")
        child = db.Folder.create(
            root=False,
            name="Child",
            path="library/child",
            parent=root,
        )
        grandchild = db.Folder.create(
            root=False,
            name="Grandchild",
            path="library/child/grandchild",
            parent=child,
        )
        track = self._create_track(
            path="outside-path/song.flac",
            root=root,
            folder=grandchild,
        )
        db.TrackMetadata.create(
            track=track,
            track_last_modification=track.last_modification,
        )
        db.TrackMetadataEnrichmentTask.create(
            track=track,
            status=db.TrackMetadataEnrichmentTask.STATUS_PENDING,
            reason=db.TrackMetadataEnrichmentTask.REASON_METADATA_MISSING,
        )
        db.ReviewTask.create(
            entity_type="track",
            entity_id=str(track.id),
            task_type="metadata_review",
            status="pending",
            reason="low_confidence",
        )

        child.delete_hierarchy()

        self.assertEqual(db.TrackMetadata.select().count(), 0)
        self.assertEqual(db.TrackMetadataEnrichmentTask.select().count(), 0)
        self.assertEqual(db.ReviewTask.select().count(), 0)

    def test_cli_metadata_enrich_dry_run_and_local_provider(self):
        track = self._create_track()
        runner = CliRunner()

        dry_run = runner.invoke(
            cli,
            ["metadata", "enrich", "--dry-run", "--limit", "10"],
            obj=self.config,
        )
        self.assertEqual(dry_run.exit_code, 0)
        self.assertIn(str(track.id), dry_run.output)
        self.assertEqual(db.TrackMetadata.select().count(), 0)

        enriched = runner.invoke(
            cli,
            ["metadata", "enrich", "--provider", "local", "--limit", "10"],
            obj=self.config,
        )
        self.assertEqual(enriched.exit_code, 0)
        self.assertIn("enriched: 1", enriched.output)
        self.assertEqual(db.TrackMetadata.select().count(), 1)

    def test_cli_metadata_enrich_requires_llm_credentials(self):
        runner = CliRunner()

        result = runner.invoke(
            cli,
            ["metadata", "enrich", "--provider", "llm"],
            obj=self.config,
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("recommendation_agent.api_base_url", result.output)

    def test_llm_provider_uses_defaults_for_invalid_numeric_config(self):
        provider = LLMMetadataProvider(
            {
                "api_base_url": "https://llm.example/v1",
                "api_key": "secret",
                "model": "metadata-model",
                "timeout_seconds": "not-a-number",
                "max_output_tokens": "not-a-number",
                "temperature": "not-a-number",
            }
        )

        self.assertEqual(provider.timeout_seconds, 20)
        self.assertEqual(provider.max_output_tokens, 900)
        self.assertEqual(provider.temperature, 0.2)

    def test_cli_metadata_enrich_llm_provider_writes_metadata(self):
        track = self._create_track(path="/private/music/Album Name/Track Name.flac")
        self.config.RECOMMENDATION_AGENT.update(
            {
                "api_base_url": "https://llm.example/v1",
                "api_key": "secret",
                "model": "metadata-model",
                "timeout_seconds": 5,
                "max_output_tokens": 200,
                "temperature": 0.1,
            }
        )
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"language":"en","mood":["calm"],'
                                '"scene":["late night"],"tags":["dreamy"],'
                                '"summary":"A calm late night track.",'
                                '"energy":30,"valence":60,'
                                '"danceability":40,"confidence":0.8}'
                            )
                        }
                    }
                ]
            },
        )
        runner = CliRunner()

        with patch("supysonic.llm_client.requests.post", return_value=response) as post:
            result = runner.invoke(
                cli,
                ["metadata", "enrich", "--provider", "llm", "--limit", "10"],
                obj=self.config,
            )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("enriched: 1", result.output)
        metadata = db.TrackMetadata.get(db.TrackMetadata.track == track)
        self.assertEqual(metadata.provider, "llm")
        self.assertEqual(metadata.model, "metadata-model")
        self.assertEqual(metadata.language, "en")
        self.assertEqual(metadata.get_moods(), ["平静"])
        request_payload = post.call_args.kwargs["json"]
        self.assertIn("response_format", request_payload)
        prompt_contract = json.loads(request_payload["messages"][1]["content"])
        self.assertIn("Simplified Chinese", prompt_contract["constraints"]["summary"])
        self.assertIn("zh for Mandarin/Chinese", prompt_contract["constraints"]["language"])
        self.assertIn("do not include artist names", prompt_contract["constraints"]["tags"])
        self.assertNotIn("/private/music", str(request_payload))
        self.assertNotIn("Album Name", str(request_payload))
        self.assertNotIn("path_hints", str(request_payload))

    def test_cli_metadata_enrich_llm_normalizes_output_fields(self):
        track = self._create_track()
        track.artist.name = "陈奕迅"
        track.artist.save()
        self.config.RECOMMENDATION_AGENT.update(
            {
                "api_base_url": "https://llm.example/v1",
                "api_key": "secret",
                "model": "metadata-model",
            }
        )
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"language":"Mandarin",'
                                '"mood":["melancholic"," melancholic ","reflective"],'
                                '"scene":["late night listening","alone time"],'
                                '"tags":["陈奕迅经典",'
                                '"Track Metadata Song","Mandarin","2024",'
                                '"Mandopop","ballad","classic hit"],'
                                '"summary":"A reflective ballad.",'
                                '"energy":30,"valence":25,'
                                '"danceability":10,"confidence":0.9}'
                            )
                        }
                    }
                ]
            },
        )
        runner = CliRunner()

        with patch("supysonic.llm_client.requests.post", return_value=response):
            result = runner.invoke(
                cli,
                ["metadata", "enrich", "--provider", "llm"],
                obj=self.config,
            )

        self.assertEqual(result.exit_code, 0)
        metadata = db.TrackMetadata.get(db.TrackMetadata.track == track)
        self.assertEqual(metadata.language, "zh")
        self.assertEqual(metadata.get_moods(), ["忧郁", "沉思"])
        self.assertEqual(metadata.get_scenes(), ["深夜聆听", "独处"])
        self.assertEqual(metadata.get_tags(), ["华语流行", "抒情", "经典金曲"])

    def test_cli_metadata_enrich_llm_sends_path_hints_only_when_enabled(self):
        self._create_track(path="/private/music/Album Name/Track Name.flac")
        self.config.DAEMON["track_metadata_enrichment_send_path_hints"] = True
        self.config.RECOMMENDATION_AGENT.update(
            {
                "api_base_url": "https://llm.example/v1",
                "api_key": "secret",
                "model": "metadata-model",
            }
        )
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [
                    {
                        "message": {
                            "content": '{"tags":["fallback"],"confidence":0.7}'
                        }
                    }
                ]
            },
        )
        runner = CliRunner()

        with patch("supysonic.llm_client.requests.post", return_value=response) as post:
            result = runner.invoke(
                cli,
                ["metadata", "enrich", "--provider", "llm"],
                obj=self.config,
            )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Album Name", str(post.call_args.kwargs["json"]))
        self.assertIn("path_hints", str(post.call_args.kwargs["json"]))

    def test_cli_metadata_enrich_llm_retries_without_response_format(self):
        self._create_track()
        self.config.RECOMMENDATION_AGENT.update(
            {
                "api_base_url": "https://llm.example/v1",
                "api_key": "secret",
                "model": "metadata-model",
            }
        )
        bad_response = SimpleNamespace(
            status_code=400,
            json=lambda: {
                "error": {
                    "message": "response_format is not supported",
                    "code": "invalid_request",
                }
            },
        )
        good_response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [
                    {
                        "message": {
                            "content": '{"tags":["fallback"],"confidence":0.7}'
                        }
                    }
                ]
            },
        )
        runner = CliRunner()

        with patch(
            "supysonic.llm_client.requests.post",
            side_effect=[bad_response, good_response],
        ) as post:
            result = runner.invoke(
                cli,
                ["metadata", "enrich", "--provider", "llm"],
                obj=self.config,
            )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("response_format", post.call_args_list[0].kwargs["json"])
        self.assertNotIn("response_format", post.call_args_list[1].kwargs["json"])

    def test_parse_json_object_response_accepts_fenced_json(self):
        parsed = parse_json_object_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "```json\n"
                                '{"tags":["dreamy"],"confidence":0.7}'
                                "\n```"
                            )
                        }
                    }
                ]
            }
        )

        self.assertEqual(parsed["tags"], ["dreamy"])
        self.assertEqual(parsed["confidence"], 0.7)

    def test_parse_json_object_response_accepts_text_wrapped_json(self):
        parsed = parse_json_object_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "Here is the metadata: "
                                '{"summary":"Uses {braces} in text","confidence":0.8}'
                                " Done."
                            )
                        }
                    }
                ]
            }
        )

        self.assertEqual(parsed["summary"], "Uses {braces} in text")
        self.assertEqual(parsed["confidence"], 0.8)

    def test_track_metadata_task_claim_recovers_from_unique_key_race(self):
        track = self._create_track()
        existing_task = db.TrackMetadataEnrichmentTask.create(
            track=track,
            status=db.TrackMetadataEnrichmentTask.STATUS_PENDING,
            reason=db.TrackMetadataEnrichmentTask.REASON_METADATA_MISSING,
        )

        with patch.object(
            scanner_track_enrich.TrackMetadataEnrichmentTask,
            "get_or_create",
            side_effect=IntegrityError("duplicate track task"),
        ):
            claimed = scanner_track_enrich._claimTrackMetadataEnrichmentTask(
                track,
                db.TrackMetadataEnrichmentTask.REASON_METADATA_MISSING,
            )

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, existing_task.id)
        self.assertEqual(
            claimed.status,
            db.TrackMetadataEnrichmentTask.STATUS_RUNNING,
        )
        self.assertEqual(claimed.attempt_count, 1)

    def test_cli_metadata_enrich_llm_retries_rate_limit_once(self):
        self._create_track()
        self.config.RECOMMENDATION_AGENT.update(
            {
                "api_base_url": "https://llm.example/v1",
                "api_key": "secret",
                "model": "metadata-model",
            }
        )
        rate_limited = SimpleNamespace(
            status_code=429,
            json=lambda: {
                "error": {
                    "message": "rate limited",
                    "code": "rate_limit",
                }
            },
        )
        recovered = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [
                    {
                        "message": {
                            "content": '{"tags":["fallback"],"confidence":0.7}'
                        }
                    }
                ]
            },
        )
        runner = CliRunner()

        with patch("supysonic.llm_client.time.sleep") as sleep, patch(
            "supysonic.llm_client.requests.post",
            side_effect=[rate_limited, recovered],
        ) as post:
            result = runner.invoke(
                cli,
                ["metadata", "enrich", "--provider", "llm"],
                obj=self.config,
            )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(5.0)
        self.assertIn("enriched: 1", result.output)

    def test_cli_metadata_enrich_llm_rate_limit_aborts_batch_after_retry(self):
        root = db.Folder.create(root=True, name="Root", path="music")
        folder = db.Folder.create(
            root=False,
            name="Album",
            path="music/album",
            parent=root,
        )
        first_track = self._create_track(
            path="music/album/rate-limit-1.flac",
            root=root,
            folder=folder,
        )
        second_track = self._create_track(
            path="music/album/rate-limit-2.flac",
            root=root,
            folder=folder,
        )
        self.config.RECOMMENDATION_AGENT.update(
            {
                "api_base_url": "https://llm.example/v1",
                "api_key": "secret",
                "model": "metadata-model",
            }
        )
        rate_limited = SimpleNamespace(
            status_code=429,
            json=lambda: {
                "error": {
                    "message": "Too many requests",
                    "code": "429",
                }
            },
        )
        runner = CliRunner()

        with patch("supysonic.llm_client.time.sleep") as sleep, patch(
            "supysonic.llm_client.requests.post",
            return_value=rate_limited,
        ) as post:
            result = runner.invoke(
                cli,
                ["metadata", "enrich", "--provider", "llm", "--limit", "2"],
                obj=self.config,
            )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(5.0)
        self.assertIn("failed: 1", result.output)
        self.assertIn("stopped: rate_limited", result.output)
        self.assertIn("provider_error", result.output)
        self.assertNotIn("Traceback", result.output)
        tasks = list(db.TrackMetadataEnrichmentTask.select())
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertIn(task.track_id, {first_track.id, second_track.id})
        unprocessed_track = (
            second_track if task.track_id == first_track.id else first_track
        )
        self.assertEqual(task.status, db.TrackMetadataEnrichmentTask.STATUS_RETRY)
        self.assertEqual(
            task.reason,
            db.TrackMetadataEnrichmentTask.REASON_PROVIDER_ERROR,
        )
        self.assertIsNotNone(task.next_retry_at)
        self.assertIn("Too many requests", task.last_error)
        self.assertIsNone(
            db.TrackMetadataEnrichmentTask.get_or_none(
                db.TrackMetadataEnrichmentTask.track == unprocessed_track
            )
        )

    def test_cli_metadata_enrich_llm_quota_failure_aborts_batch(self):
        root = db.Folder.create(root=True, name="Root", path="music")
        folder = db.Folder.create(
            root=False,
            name="Album",
            path="music/album",
            parent=root,
        )
        first_track = self._create_track(
            path="music/album/quota-1.flac",
            root=root,
            folder=folder,
        )
        second_track = self._create_track(
            path="music/album/quota-2.flac",
            root=root,
            folder=folder,
        )
        self.config.RECOMMENDATION_AGENT.update(
            {
                "api_base_url": "https://llm.example/v1",
                "api_key": "secret",
                "model": "metadata-model",
            }
        )
        quota_response = SimpleNamespace(
            status_code=429,
            json=lambda: {
                "error": {
                    "message": "You exceeded your current quota.",
                    "code": "insufficient_quota",
                }
            },
        )
        runner = CliRunner()

        with patch(
            "supysonic.llm_client.requests.post",
            return_value=quota_response,
        ) as post:
            result = runner.invoke(
                cli,
                ["metadata", "enrich", "--provider", "llm", "--limit", "2"],
                obj=self.config,
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(post.call_count, 1)
        self.assertIn("failed: 1", result.output)
        self.assertIn("provider_quota", result.output)
        self.assertIn("LLM quota exhausted", result.output)
        self.assertEqual(db.TrackMetadata.select().count(), 0)
        tasks = list(db.TrackMetadataEnrichmentTask.select())
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertIn(task.track_id, {first_track.id, second_track.id})
        unprocessed_track = (
            second_track if task.track_id == first_track.id else first_track
        )
        self.assertEqual(task.status, db.TrackMetadataEnrichmentTask.STATUS_FAILED)
        self.assertEqual(
            task.reason,
            db.TrackMetadataEnrichmentTask.REASON_PROVIDER_QUOTA,
        )
        self.assertIsNone(task.next_retry_at)
        self.assertIn("LLM quota exhausted", task.last_error)
        self.assertIsNone(
            db.TrackMetadataEnrichmentTask.get_or_none(
                db.TrackMetadataEnrichmentTask.track == unprocessed_track
            )
        )

    def test_cli_metadata_enrich_llm_invalid_json_marks_task_failed(self):
        track = self._create_track()
        self.config.RECOMMENDATION_AGENT.update(
            {
                "api_base_url": "https://llm.example/v1",
                "api_key": "secret",
                "model": "metadata-model",
            }
        )
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [
                    {
                        "message": {
                            "content": "not json"
                        }
                    }
                ]
            },
        )
        runner = CliRunner()

        with patch("supysonic.llm_client.requests.post", return_value=response):
            result = runner.invoke(
                cli,
                ["metadata", "enrich", "--provider", "llm"],
                obj=self.config,
            )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("failed: 1", result.output)
        self.assertEqual(db.TrackMetadata.select().count(), 0)
        task = db.TrackMetadataEnrichmentTask.get(
            db.TrackMetadataEnrichmentTask.track == track
        )
        self.assertEqual(task.status, db.TrackMetadataEnrichmentTask.STATUS_FAILED)
        self.assertEqual(
            task.reason,
            db.TrackMetadataEnrichmentTask.REASON_INVALID_RESPONSE,
        )


if __name__ == "__main__":
    unittest.main()
