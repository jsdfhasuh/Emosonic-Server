import unittest
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from supysonic.cli import metadata_enrich
from supysonic.daemon.server import Daemon
from supysonic.track_metadata_background import BudgetedMetadataProvider


class TrackMetadataBudgetRoutingTestCase(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(DAEMON={"track_metadata_enrichment_provider": "llm"})
        self.settings = {
            "api_base_url": "https://api.openai.com/v1", "api_key": "fake-key",
            "model": "gpt-6-luna", "monthly_budget_usd": 60,
            "token_limit_parameter": "max_completion_tokens", "web_search_enabled": True,
        }
        self.summary = {"selected": 0, "enriched": 0, "failed": 0, "skipped": 0,
                        "provider": "llm", "dry_run": False, "tracks": []}

    def daemon(self):
        daemon = Daemon.__new__(Daemon)
        daemon._Daemon__config = self.config
        return daemon

    def test_daemon_budgeted_configuration_uses_automatic_budgeted_provider(self):
        with patch("supysonic.daemon.server.get_track_metadata_llm_config", return_value=self.settings), \
                patch("supysonic.daemon.server.open_connection", return_value=False), \
                patch("supysonic.daemon.server.runAutomaticMetadataPass", return_value=self.summary) as automatic, \
                patch("supysonic.daemon.server.runTrackMetadataEnrichmentPass") as legacy_pass, \
                patch("supysonic.daemon.server.LLMMetadataProvider") as legacy_provider:
            self.assertTrue(self.daemon()._Daemon__run_track_metadata_enrichment())
        self.assertIsInstance(automatic.call_args.kwargs["provider"], BudgetedMetadataProvider)
        legacy_provider.assert_not_called()
        legacy_pass.assert_not_called()

    def test_daemon_invalid_budget_fails_closed_without_legacy_fallback(self):
        self.settings["monthly_budget_usd"] = "invalid"
        with patch("supysonic.daemon.server.get_track_metadata_llm_config", return_value=self.settings), \
                patch("supysonic.daemon.server.open_connection") as open_connection, \
                patch("supysonic.daemon.server.runAutomaticMetadataPass") as automatic, \
                patch("supysonic.daemon.server.runTrackMetadataEnrichmentPass") as legacy_pass, \
                patch("supysonic.daemon.server.LLMMetadataProvider") as legacy_provider:
            self.assertFalse(self.daemon()._Daemon__run_track_metadata_enrichment())
        open_connection.assert_not_called()
        automatic.assert_not_called()
        legacy_pass.assert_not_called()
        legacy_provider.assert_not_called()

    def test_daemon_provider_pause_is_not_reported_as_completed(self):
        self.summary.update(provider_paused=True, selected=1, failed=1)
        with patch("supysonic.daemon.server.get_track_metadata_llm_config", return_value=self.settings), \
                patch("supysonic.daemon.server.open_connection", return_value=False), \
                patch("supysonic.daemon.server.runAutomaticMetadataPass", return_value=self.summary) as automatic, \
                self.assertLogs("supysonic.daemon.server", level="INFO") as captured:
            daemon = self.daemon()
            self.assertFalse(daemon._Daemon__run_track_metadata_enrichment())
            self.assertEqual(daemon._Daemon__get_track_metadata_enrichment_interval(), 300)
        automatic.assert_called_once()
        output = "\n".join(captured.output)
        self.assertIn("event=metadata_provider_paused", output)
        self.assertNotIn("event=track_metadata_enrichment_completed", output)
        self.assertNotIn("event=track_metadata_enrichment_budget_paused", output)

    def test_daemon_budget_pause_keeps_existing_log_and_priority(self):
        self.summary.update(budget_paused=True, provider_paused=True)
        with patch("supysonic.daemon.server.get_track_metadata_llm_config", return_value=self.settings), \
                patch("supysonic.daemon.server.open_connection", return_value=False), \
                patch("supysonic.daemon.server.runAutomaticMetadataPass", return_value=self.summary), \
                self.assertLogs("supysonic.daemon.server", level="INFO") as captured:
            self.assertFalse(self.daemon()._Daemon__run_track_metadata_enrichment())
        output = "\n".join(captured.output)
        self.assertIn("event=track_metadata_enrichment_budget_paused", output)
        self.assertNotIn("event=metadata_provider_paused", output)
        self.assertNotIn("event=track_metadata_enrichment_completed", output)

    def test_cli_uses_budgeted_provider_even_for_forced_enrichment(self):
        with patch("supysonic.cli.get_track_metadata_llm_config", return_value=self.settings), \
                patch("supysonic.cli.runTrackMetadataEnrichmentPass", return_value=self.summary) as run_pass, \
                patch("supysonic.cli.LLMMetadataProvider") as legacy_provider:
            result = CliRunner().invoke(metadata_enrich, ["--provider", "llm", "--force"], obj=self.config)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIsInstance(run_pass.call_args.kwargs["provider"], BudgetedMetadataProvider)
        self.assertTrue(run_pass.call_args.kwargs["force"])
        legacy_provider.assert_not_called()

    def test_cli_invalid_budget_fails_closed_without_legacy_fallback(self):
        self.settings["monthly_budget_usd"] = "invalid"
        with patch("supysonic.cli.get_track_metadata_llm_config", return_value=self.settings), \
                patch("supysonic.cli.runTrackMetadataEnrichmentPass") as run_pass, \
                patch("supysonic.cli.LLMMetadataProvider") as legacy_provider:
            result = CliRunner().invoke(metadata_enrich, ["--provider", "llm"], obj=self.config)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Invalid metadata budget amount", result.output)
        run_pass.assert_not_called()
        legacy_provider.assert_not_called()

    def test_web_enabled_without_budget_is_not_an_unmetered_fallback(self):
        del self.settings["monthly_budget_usd"]
        with patch("supysonic.cli.get_track_metadata_llm_config", return_value=self.settings), \
                patch("supysonic.cli.runTrackMetadataEnrichmentPass") as run_pass, \
                patch("supysonic.cli.LLMMetadataProvider") as legacy_provider:
            result = CliRunner().invoke(metadata_enrich, ["--provider", "llm"], obj=self.config)
        self.assertNotEqual(result.exit_code, 0)
        run_pass.assert_not_called()
        legacy_provider.assert_not_called()
