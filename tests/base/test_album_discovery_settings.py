import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import Flask

from supysonic.album_discovery_settings import (
    AlbumDiscoverySettings,
    AlbumDiscoverySettingsError,
    load_album_discovery_settings,
)
from supysonic.config import DefaultConfig, IniConfig


class AlbumDiscoverySettingsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings_file = Path(directory.name) / "settings.json"
        ini_path = Path(directory.name) / "supysonic.conf"
        ini_path.write_text(
            f"[album_discovery]\nsettings_file = {self.settings_file}\n",
            encoding="utf-8",
        )
        self.config = IniConfig(str(ini_path))

    def write_settings(self, data: object) -> None:
        self.settings_file.write_text(json.dumps(data), encoding="utf-8")

    def test_defaults_when_unconfigured_or_fields_are_omitted(self) -> None:
        self.assertEqual(
            load_album_discovery_settings(DefaultConfig()),
            AlbumDiscoverySettings(1, False),
        )
        for root in ({}, {"aiDiscovery": {}}):
            with self.subTest(root=root):
                self.write_settings(root)
                self.assertEqual(
                    load_album_discovery_settings(self.config),
                    AlbumDiscoverySettings(1, False),
                )

    def test_web_and_daemon_see_replaced_settings(self) -> None:
        app = Flask(__name__)
        app.config.from_object(self.config)
        self.write_settings({"aiDiscovery": {"maxAlbumsPerDay": 0}})
        for config in (app.config, self.config):
            self.assertEqual(
                load_album_discovery_settings(config), AlbumDiscoverySettings(0, False)
            )

        replacement = self.settings_file.with_suffix(".tmp")
        replacement.write_text(
            '{"aiDiscovery":{"maxAlbumsPerDay":3,"autoSubmit":true}}',
            encoding="utf-8-sig",
        )
        replacement.replace(self.settings_file)
        for config in (app.config, self.config):
            self.assertEqual(
                load_album_discovery_settings(config), AlbumDiscoverySettings(3, True)
            )

    def test_invalid_limits_raise(self) -> None:
        for limit in (-1, 1.0, 1.5, "3", True, False, None, [], {}):
            with self.subTest(limit=limit):
                self.write_settings({"aiDiscovery": {"maxAlbumsPerDay": limit}})
                with self.assertRaisesRegex(
                    AlbumDiscoverySettingsError, "maxAlbumsPerDay"
                ):
                    load_album_discovery_settings(self.config)

    def test_auto_submit_requires_a_boolean(self) -> None:
        for value in (1, "true", None):
            with self.subTest(value=value):
                self.write_settings({"aiDiscovery": {"autoSubmit": value}})
                with self.assertRaisesRegex(AlbumDiscoverySettingsError, "autoSubmit"):
                    load_album_discovery_settings(self.config)

    def test_invalid_json_and_object_shapes_raise(self) -> None:
        for contents in ("{", '{"elsewhere": NaN}'):
            with self.subTest(contents=contents):
                self.settings_file.write_text(contents, encoding="utf-8")
                with self.assertRaisesRegex(
                    AlbumDiscoverySettingsError, "Invalid JSON"
                ):
                    load_album_discovery_settings(self.config)
        for root in ([], None, {"aiDiscovery": None}, {"aiDiscovery": "1"}):
            with self.subTest(root=root):
                self.write_settings(root)
                with self.assertRaisesRegex(AlbumDiscoverySettingsError, "JSON object"):
                    load_album_discovery_settings(self.config)

    def test_configured_unreadable_path_raises(self) -> None:
        with self.assertRaisesRegex(AlbumDiscoverySettingsError, "Cannot read"):
            load_album_discovery_settings(self.config)
        self.settings_file.write_bytes(b"\xff")
        with self.assertRaisesRegex(AlbumDiscoverySettingsError, "Cannot read"):
            load_album_discovery_settings(self.config)

    def test_path_is_kept_as_text_and_empty_setting_raises(self) -> None:
        ini_path = self.settings_file.with_suffix(".conf")
        ini_path.write_text("[album_discovery]\nsettings_file = 0\n", encoding="utf-8")
        config = IniConfig(str(ini_path))
        self.assertEqual(config.ALBUM_DISCOVERY["settings_file"], "0")
        ini_path.write_text("[album_discovery]\nsettings_file =\n", encoding="utf-8")
        config = IniConfig(str(ini_path))
        with self.assertRaisesRegex(AlbumDiscoverySettingsError, "nonempty path"):
            load_album_discovery_settings(config)
