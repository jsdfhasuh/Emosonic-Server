# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2017-2018 Alban 'spl0k' Féron
#
# Distributed under terms of the GNU AGPLv3 license.

import os
import unittest
from tempfile import NamedTemporaryFile

from supysonic.config import DefaultConfig, IniConfig


class ConfigTestCase(unittest.TestCase):
    def test_sections(self):
        conf = IniConfig("tests/assets/sample.ini")
        for attr in ("TYPES", "BOOLEANS"):
            self.assertTrue(hasattr(conf, attr))
            self.assertIsInstance(getattr(conf, attr), dict)

    def test_types(self):
        conf = IniConfig("tests/assets/sample.ini")

        self.assertIsInstance(conf.TYPES["float"], float)
        self.assertIsInstance(conf.TYPES["int"], int)
        self.assertIsInstance(conf.TYPES["string"], str)

        for t in ("bool", "switch", "yn"):
            self.assertIsInstance(conf.BOOLEANS[t + "_false"], bool)
            self.assertIsInstance(conf.BOOLEANS[t + "_true"], bool)
            self.assertFalse(conf.BOOLEANS[t + "_false"])
            self.assertTrue(conf.BOOLEANS[t + "_true"])

    def test_no_interpolation(self):
        conf = IniConfig("tests/assets/sample.ini")

        self.assertEqual(conf.ISSUE84["variable"], "value")
        self.assertEqual(conf.ISSUE84["key"], "some value with a %variable")

    def test_ini_config_does_not_mutate_defaults(self):
        original_webapp = DefaultConfig.WEBAPP.copy()
        original_lastfm = DefaultConfig.LASTFM.copy()

        try:
            config_file_path = None
            with NamedTemporaryFile("w", delete=False) as config_file:
                config_file.write(
                    "[webapp]\n"
                    "registration_invite_code = KPOP\n"
                    "[lastfm]\n"
                    "api_key = test-key\n"
                    "secret = test-secret\n"
                )
                config_file.flush()
                config_file_path = config_file.name

            conf = IniConfig(config_file_path)

            self.assertEqual(conf.WEBAPP["registration_invite_code"], "KPOP")
            self.assertEqual(conf.LASTFM["api_key"], "test-key")
            self.assertEqual(DefaultConfig.WEBAPP, original_webapp)
            self.assertEqual(DefaultConfig.LASTFM, original_lastfm)
        finally:
            if config_file_path:
                os.remove(config_file_path)
            DefaultConfig.WEBAPP = original_webapp
            DefaultConfig.LASTFM = original_lastfm

    def test_recommendation_agent_config_defaults_and_ini_values(self):
        original_agent = DefaultConfig.RECOMMENDATION_AGENT.copy()

        try:
            config_file_path = None
            self.assertFalse(DefaultConfig.RECOMMENDATION_AGENT["enabled"])
            self.assertEqual(
                DefaultConfig.RECOMMENDATION_AGENT["api_base_url"],
                "https://api.openai.com/v1",
            )
            self.assertEqual(DefaultConfig.RECOMMENDATION_AGENT["api_key"], "")
            self.assertEqual(DefaultConfig.RECOMMENDATION_AGENT["model"], "")
            self.assertEqual(DefaultConfig.RECOMMENDATION_AGENT["max_output_tokens"], 900)

            with NamedTemporaryFile("w", delete=False) as config_file:
                config_file.write(
                    "[recommendation_agent]\n"
                    "enabled = on\n"
                    "api_base_url = https://llm.example/v1\n"
                    "api_key = test-key\n"
                    "model = test-model\n"
                    "timeout_seconds = 9\n"
                    "history_limit = 33\n"
                    "max_output_tokens = 0\n"
                    "temperature = 0.2\n"
                )
                config_file.flush()
                config_file_path = config_file.name

            conf = IniConfig(config_file_path)

            self.assertTrue(conf.RECOMMENDATION_AGENT["enabled"])
            self.assertEqual(
                conf.RECOMMENDATION_AGENT["api_base_url"],
                "https://llm.example/v1",
            )
            self.assertEqual(conf.RECOMMENDATION_AGENT["api_key"], "test-key")
            self.assertEqual(conf.RECOMMENDATION_AGENT["model"], "test-model")
            self.assertEqual(conf.RECOMMENDATION_AGENT["timeout_seconds"], 9)
            self.assertEqual(conf.RECOMMENDATION_AGENT["history_limit"], 33)
            self.assertEqual(conf.RECOMMENDATION_AGENT["max_output_tokens"], 0)
            self.assertEqual(conf.RECOMMENDATION_AGENT["temperature"], 0.2)
            self.assertEqual(DefaultConfig.RECOMMENDATION_AGENT, original_agent)
        finally:
            if config_file_path:
                os.remove(config_file_path)
            DefaultConfig.RECOMMENDATION_AGENT = original_agent


if __name__ == "__main__":
    unittest.main()
