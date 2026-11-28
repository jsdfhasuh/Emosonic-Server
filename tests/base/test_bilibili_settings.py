from datetime import time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from supysonic.bilibili_discovery.settings import (
    BilibiliDiscoverySettingsError,
    load_bilibili_discovery_settings,
)
from supysonic.config import DefaultConfig, IniConfig


class BilibiliSettingsTestCase(unittest.TestCase):
    def test_defaults_are_off_and_time_is_shanghai_local(self) -> None:
        settings = load_bilibili_discovery_settings(DefaultConfig())
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.auto_download)
        self.assertEqual(settings.start_time, time(4, 30))

    def test_ini_preserves_text_and_parses_switches(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.ini"
            path.write_text(
                "[bilibili_discovery]\nenabled=on\nauto_download=off\n"
                "history_api_url=https://history.example.invalid\n"
                "history_api_token_file=0412\nstart_time=06:20\n",
                encoding="utf-8",
            )
            settings = load_bilibili_discovery_settings(IniConfig([path]))
        self.assertTrue(settings.enabled)
        self.assertFalse(settings.auto_download)
        self.assertEqual(settings.history_api_url, "https://history.example.invalid")
        self.assertEqual(settings.history_api_token_file, "0412")
        self.assertEqual(settings.start_time, time(6, 20))

    def test_ambiguous_config_does_not_enable_download(self) -> None:
        config = {"BILIBILI_DISCOVERY": {"enabled": True, "auto_download": "maybe"}}
        self.assertFalse(load_bilibili_discovery_settings(config).auto_download)
        config["BILIBILI_DISCOVERY"]["start_time"] = "04:30Z"
        with self.assertRaises(BilibiliDiscoverySettingsError):
            load_bilibili_discovery_settings(config)
