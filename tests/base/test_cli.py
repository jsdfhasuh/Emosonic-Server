# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2017-2022 Alban 'spl0k' Féron
#
# Distributed under terms of the GNU AGPLv3 license.

import os
import tempfile
import shlex
import unittest
import logging

from click.testing import CliRunner
from types import SimpleNamespace
from unittest.mock import patch

from supysonic.db import (
    EmoBroadcastRecoveryAbandon,
    EmoPermanentDeviceDecommission,
    Folder,
    User,
    init_database,
    release_database,
)
from supysonic.cli import cli

from ..testbase import TestConfig


class CLITestCase(unittest.TestCase):
    """Really basic tests. Some even don't check anything but are just there for coverage"""

    def setUp(self):
        self.__conf = TestConfig(False, False)
        self.__db = tempfile.mkstemp()
        self.__conf.BASE["database_uri"] = "sqlite:///" + self.__db[1]
        init_database(self.__conf.BASE["database_uri"])

        self.__runner = CliRunner()

    def tearDown(self):
        release_database()
        os.close(self.__db[0])
        os.remove(self.__db[1])

    def __invoke(self, cmd, expect_fail=False):
        rv = self.__runner.invoke(cli, shlex.split(cmd), obj=self.__conf)
        func = self.assertNotEqual if expect_fail else self.assertEqual
        func(rv.exit_code, 0)
        return rv

    def __add_folder(self, name, path, expect_fail=False):
        self.__invoke(f"folder add {name} {shlex.quote(path)}", expect_fail)

    def test_folder_add(self):
        with tempfile.TemporaryDirectory() as d:
            self.__add_folder("tmpfolder", d)

        f = Folder.select().first()
        self.assertIsNotNone(f)
        self.assertEqual(f.path, d)

    def test_folder_add_errors(self):
        with tempfile.TemporaryDirectory() as d:
            self.__add_folder("f1", d)
            self.__add_folder("f2", d, True)
        with tempfile.TemporaryDirectory() as d:
            self.__add_folder("f1", d, True)
        self.__invoke("folder add f3 /invalid/path", True)

        self.assertEqual(Folder.select().count(), 1)

    def test_folder_delete(self):
        with tempfile.TemporaryDirectory() as d:
            self.__add_folder("tmpfolder", d)
        self.__invoke("folder delete randomfolder", True)
        self.__invoke("folder delete tmpfolder")

        self.assertEqual(Folder.select().count(), 0)

    def test_folder_list(self):
        with tempfile.TemporaryDirectory() as d:
            self.__add_folder("tmpfolder", d)
            rv = self.__invoke("folder list")
            self.assertIn("tmpfolder", rv.output)
            self.assertIn(d, rv.output)

    def test_folder_scan(self):
        with tempfile.TemporaryDirectory() as d:
            self.__add_folder("tmpfolder", d)
            with tempfile.NamedTemporaryFile(dir=d):
                self.__invoke("folder scan")
                self.__invoke("folder scan tmpfolder nonexistent")

    def test_foreground_scan_initializes_managed_logging(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as log_dir:
            self.__conf.WEBAPP["log_dir"] = log_dir
            self.__conf.WEBAPP["log_level"] = "INFO"
            self.__add_folder("tmpfolder", d)

            fake_scanner = type(
                "FakeScanner",
                (),
                {
                    "queue_folder": lambda self, folder: None,
                    "run": lambda self: logging.getLogger("supysonic.scanner").info(
                        "scanner event=run_start force=false follow_symlinks=false"
                    ),
                    "stats": lambda self: SimpleNamespace(
                        added=SimpleNamespace(artists=0, albums=0, tracks=0),
                        deleted=SimpleNamespace(artists=0, albums=0, tracks=0),
                        errors=[],
                    ),
                },
            )()

            with patch("supysonic.cli.Scanner", return_value=fake_scanner), patch(
                "supysonic.cli.DaemonClient"
            ) as daemon_client:
                daemon_client.return_value.get_scanning_progress.return_value = None
                self.__invoke("folder scan --foreground tmpfolder")

            scanner_log = os.path.join(log_dir, "scanner.log")
            summary_log = os.path.join(log_dir, "supysonic.log")

            self.assertTrue(os.path.isfile(scanner_log))
            self.assertTrue(os.path.isfile(summary_log))

            with open(scanner_log, "r", encoding="utf-8") as f:
                scanner_content = f.read()

            self.assertIn("scanner event=run_start", scanner_content)

    def test_foreground_scan_uses_legacy_web_log_file_directory(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as log_dir:
            self.__conf.WEBAPP["log_dir"] = None
            self.__conf.WEBAPP["log_file"] = os.path.join(log_dir, "legacy.log")
            self.__conf.WEBAPP["log_level"] = "INFO"
            self.__add_folder("tmpfolder", d)

            fake_scanner = type(
                "FakeScanner",
                (),
                {
                    "queue_folder": lambda self, folder: None,
                    "run": lambda self: logging.getLogger("supysonic.scanner").info(
                        "scanner event=run_start force=false follow_symlinks=false"
                    ),
                    "stats": lambda self: SimpleNamespace(
                        added=SimpleNamespace(artists=0, albums=0, tracks=0),
                        deleted=SimpleNamespace(artists=0, albums=0, tracks=0),
                        errors=[],
                    ),
                },
            )()

            with patch("supysonic.cli.Scanner", return_value=fake_scanner), patch(
                "supysonic.cli.DaemonClient"
            ) as daemon_client:
                daemon_client.return_value.get_scanning_progress.return_value = None
                self.__invoke("folder scan --foreground tmpfolder")

            self.assertTrue(os.path.isfile(os.path.join(log_dir, "scanner.log")))

    def test_user_add(self):
        self.__invoke("user add -p Alic3 alice")
        self.__invoke("user add -p alice alice", True)

        self.assertEqual(User.select().count(), 1)

    def test_user_delete(self):
        self.__invoke("user add -p Alic3 alice")
        self.__invoke("user delete alice")
        self.__invoke("user delete bob", True)

        self.assertEqual(User.select().count(), 0)

    def test_user_delete_removes_emo_decommission_security_records(self):
        self.__invoke("user add -p Alic3 alice")
        EmoBroadcastRecoveryAbandon.create(
            user_name="alice",
            client_id="player-1",
            device_session_id="device:player-1",
            broadcast_id="broadcast-1",
            request_fingerprint="a" * 64,
            terminal_broadcast_revision=3,
            obligation_kind="compact",
            outcome_json="{}",
            abandoned_at_ms=1000,
        )
        EmoPermanentDeviceDecommission.create(
            user_name="alice",
            client_id="player-1",
            device_session_id="device:player-1",
            broadcast_id="broadcast-1",
            abandon_request_fingerprint="a" * 64,
            decommissioned_at_ms=1000,
        )

        self.__invoke("user delete alice")

        self.assertEqual(EmoBroadcastRecoveryAbandon.select().count(), 0)
        self.assertEqual(EmoPermanentDeviceDecommission.select().count(), 0)

    def test_emo_broadcast_abandon_is_a_management_entry_point(self):
        with patch(
            "supysonic.emo.ws.abandonBroadcastRecoveryAndDecommission",
            return_value={"abandoned": True, "onlineRevoked": False},
        ) as abandon:
            result = self.__invoke(
                "emo broadcast-abandon alice player-1 device:player-1 broadcast-1"
            )

        abandon.assert_called_once_with(
            "alice",
            "player-1",
            "device:player-1",
            "broadcast-1",
            request_fingerprint=None,
        )
        self.assertIn("Recovery abandoned for alice/player-1/device:player-1", result.output)

    def test_user_list(self):
        self.__invoke("user add -p Alic3 alice")
        rv = self.__invoke("user list")
        self.assertIn("alice", rv.output)

    def test_user_setadmin(self):
        self.__invoke("user add -p Alic3 alice")
        self.__invoke("user setroles -A alice")
        self.__invoke("user setroles -A bob", True)
        self.assertTrue(User.get(name="alice").admin)

    def test_user_unsetadmin(self):
        self.__invoke("user add -p Alic3 alice")
        self.__invoke("user setroles -A alice")
        self.__invoke("user setroles -a alice")
        self.assertFalse(User.get(name="alice").admin)

    def test_user_setjukebox(self):
        self.__invoke("user add -p Alic3 alice")
        self.__invoke("user setroles -J alice")
        self.assertTrue(User.get(name="alice").jukebox)

    def test_user_unsetjukebox(self):
        self.__invoke("user add -p Alic3 alice")
        self.__invoke("user setroles -J alice")
        self.__invoke("user setroles -j alice")
        self.assertFalse(User.get(name="alice").jukebox)

    def test_user_changepass(self):
        self.__invoke("user add -p Alic3 alice")
        self.__invoke("user changepass alice -p newpass")
        self.__invoke("user changepass bob -p B0b", True)

    def test_user_rename(self):
        self.__invoke("user add -p Alic3 alice")
        self.__invoke("user rename alice alice")
        self.__invoke("user rename bob charles", True)

        self.__invoke("user rename alice ''", True)
        self.assertEqual(User.select().first().name, "alice")

        self.__invoke("user rename alice bob")
        self.assertEqual(User.select().first().name, "bob")

        self.__invoke("user add -p Ch4rl3s charles")
        self.__invoke("user rename bob charles", True)
        self.assertEqual(User.select().where(User.name == "bob").count(), 1)
        self.assertEqual(User.select().where(User.name == "charles").count(), 1)


if __name__ == "__main__":
    unittest.main()
