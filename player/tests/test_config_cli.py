import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from emosonic_player.config import load_config, read_password, client_identity
from emosonic_player.cli import parser, main
from emosonic_player.stream import MediaClient


class ConfigTests(unittest.TestCase):
    def test_password_owner_only_and_no_follow(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'secret'
            path.write_text('test-only-password\n')
            os.chmod(path, 0o600)
            self.assertEqual(read_password(str(path)), 'test-only-password')
            os.chmod(path, 0o644)
            with self.assertRaises(ValueError):
                read_password(str(path))
            link = Path(root) / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                read_password(str(link))

    def test_config_and_identity(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'config'
            config = {'server_url': 'https://music.example.test', 'username': 'test',
                      'password_file': '/private/password', 'audio_device': 'alsa/hw:CARD=USB,DEV=0'}
            path.write_text(json.dumps(config))
            self.assertEqual(load_config(str(path))['initial_volume'], 20)
            config['server_url'] = 'https://user:secret@music.example.test'
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                load_config(str(path))
            identity = str(Path(root) / 'id')
            self.assertEqual(client_identity(identity), client_identity(identity))

    def test_cli_no_credential_argument(self):
        self.assertEqual(parser().parse_args(['play', '--playlist', 'id']).playlist, 'id')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(['--password', 'secret'])

    def test_stream_url_authentication_not_plain_password(self):
        client = MediaClient('https://music.example.test', 'test', 'private-test')
        first = client.stream_url('song')
        self.assertNotIn('private-test', first)
        self.assertNotEqual(first, client.stream_url('song'))
        self.assertTrue(first.startswith('https://music.example.test/rest/stream.view?'))
