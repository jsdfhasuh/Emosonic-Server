import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from supysonic.cli import cli
from supysonic.config import DefaultConfig, IniConfig
from supysonic.llm_client import LlmConfigError
from supysonic.track_metadata_config import get_track_metadata_llm_config


class TrackMetadataConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.legacy = {
            "api_base_url": "https://legacy.example/v1",
            "api_key": "legacy-test-key",
            "model": "legacy-model",
        }

    def config(self, dedicated=None):
        return SimpleNamespace(
            RECOMMENDATION_AGENT=self.legacy,
            TRACK_METADATA_LLM=dedicated or {},
            DAEMON={},
        )

    def test_legacy_fallback_returns_copy(self):
        result = get_track_metadata_llm_config(self.config())
        self.assertEqual(result, self.legacy)
        result["api_key"] = "changed"
        self.assertEqual(self.legacy["api_key"], "legacy-test-key")

    def test_dedicated_config_never_inherits_legacy_values(self):
        dedicated = {"model": "metadata-model", "api_key": "metadata-test-key"}
        result = get_track_metadata_llm_config(self.config(dedicated))
        self.assertEqual(result["api_base_url"], "https://api.openai.com/v1")
        self.assertEqual(result["api_key"], "metadata-test-key")
        self.assertEqual(result["model"], "metadata-model")
        self.assertNotIn("api_base_url", dedicated)
        self.assertEqual(self.legacy["model"], "legacy-model")

    def test_partial_config_fails_instead_of_using_legacy_key(self):
        for dedicated in ({"model": "metadata-model"}, {"api_key": "new-key"}):
            with self.subTest(dedicated=dedicated), self.assertRaises(LlmConfigError):
                get_track_metadata_llm_config(self.config(dedicated))

    def test_private_key_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key.txt"
            path.write_text("\ufeffmetadata-test-key\n", encoding="utf-8")
            result = get_track_metadata_llm_config(self.config({
                "model": "metadata-model", "api_key_file": str(path),
            }))
        self.assertEqual(result["api_key"], "metadata-test-key")
        self.assertNotIn("api_key_file", result)

    def test_key_file_errors_do_not_include_private_path(self):
        with patch("supysonic.track_metadata_config.Path.open", side_effect=OSError("private detail")):
            with self.assertRaises(LlmConfigError) as caught:
                get_track_metadata_llm_config(self.config({
                    "model": "metadata-model", "api_key_file": str(Path.cwd() / "private-key"),
                }))
        self.assertNotIn("private", str(caught.exception))

    def test_key_file_rejects_relative_path(self):
        with self.assertRaises(LlmConfigError):
            get_track_metadata_llm_config(self.config({
                "model": "metadata-model", "api_key_file": "relative-key.txt",
            }))

    def test_environment_key_is_explicit(self):
        with patch.dict(os.environ, {"METADATA_TEST_KEY": "env-test-key"}):
            result = get_track_metadata_llm_config(self.config({
                "model": "metadata-model", "api_key_env": "METADATA_TEST_KEY",
            }))
        self.assertEqual(result["api_key"], "env-test-key")
        self.assertNotIn("api_key_env", result)

    def test_missing_environment_key_does_not_fall_back(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(LlmConfigError):
            get_track_metadata_llm_config(self.config({
                "model": "metadata-model", "api_key_env": "METADATA_TEST_KEY",
            }))

    def test_ambiguous_key_sources_rejected_before_reading(self):
        with patch("supysonic.track_metadata_config.Path.open") as read_file:
            with self.assertRaises(LlmConfigError):
                get_track_metadata_llm_config(self.config({
                    "model": "metadata-model", "api_key": "test-key",
                    "api_key_file": "private-key",
                }))
        read_file.assert_not_called()

    def test_endpoint_cannot_embed_secrets(self):
        for endpoint in (
            "https://user:secret@example.com/v1", "https://example.com/v1?key=secret",
            "https://example.com/v1#secret", "file:///private-key", "https://[",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(LlmConfigError) as caught:
                get_track_metadata_llm_config(self.config({
                    "model": "metadata-model", "api_key": "test-key", "api_base_url": endpoint,
                }))
            self.assertNotIn("secret", str(caught.exception))

    def test_ini_keeps_independent_defaults_and_blank_temperature(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.ini"
            path.write_text(
                "[track_metadata_llm]\nmodel = metadata-model\napi_key = metadata-test-key\n"
                "temperature =\ntoken_limit_parameter = max_completion_tokens\n"
                "[recommendation_agent]\nmodel = agent-model\napi_key = agent-test-key\n",
                encoding="utf-8",
            )
            config = IniConfig(str(path))
        self.assertEqual(config.TRACK_METADATA_LLM["temperature"], "")
        self.assertEqual(config.RECOMMENDATION_AGENT["model"], "agent-model")
        self.assertEqual(DefaultConfig.TRACK_METADATA_LLM, {})
        self.assertEqual(get_track_metadata_llm_config(config)["model"], "metadata-model")

    def test_cli_uses_dedicated_config(self):
        config = self.config({"model": "metadata-model", "api_key": "metadata-test-key"})
        summary = {"selected": 0, "enriched": 0, "failed": 0, "skipped": 0,
                   "provider": "llm", "dry_run": False, "tracks": []}
        with patch("supysonic.cli.LLMMetadataProvider") as provider, patch(
            "supysonic.cli.runTrackMetadataEnrichmentPass", return_value=summary,
        ):
            result = CliRunner().invoke(cli, ["metadata", "enrich", "--provider", "llm"], obj=config)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(provider.call_args.args[0]["api_key"], "metadata-test-key")

    def test_cli_invalid_dedicated_config_stops_before_processing(self):
        with patch("supysonic.cli.runTrackMetadataEnrichmentPass") as run_pass:
            result = CliRunner().invoke(
                cli, ["metadata", "enrich", "--provider", "llm"],
                obj=self.config({"model": "metadata-model"}),
            )
        self.assertNotEqual(result.exit_code, 0)
        run_pass.assert_not_called()
        self.assertNotIn("legacy-test-key", result.output)
