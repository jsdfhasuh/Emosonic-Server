"""Local HA socket, audio file handling and optional real mpv IPC."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import wave

from emosonic_player.announcement import prepare_wave
from emosonic_player.cli import call
from emosonic_player.main import control_socket, serve_cli
from emosonic_player.mpv import MpvPlayer
from emosonic_player.playback import Player


def write_wave(path, frames):
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\0\0' * frames)


class AnnouncementFileTests(unittest.TestCase):
    def test_accepts_pcm_and_rejects_links_nonfiles_and_invalid_input(self):
        with tempfile.TemporaryDirectory() as root:
            audio = Path(root) / 'audio'
            audio.mkdir()
            write_wave(audio / 'notice.wav', 8000)
            (audio / 'link.wav').symlink_to(audio / 'notice.wav')
            os.mkfifo(audio / 'pipe.wav')
            (audio / 'bad.wav').write_bytes(b'not audio')
            output = Path(root) / 'private.wav'
            self.assertEqual(prepare_wave(audio, 'notice.wav', output), 1)
            self.assertEqual(output.read_bytes(), (audio / 'notice.wav').read_bytes())
            for name in ['../notice.wav', 'https://host/notice.wav', 'link.wav', 'pipe.wav', 'bad.wav']:
                with self.subTest(name=name), self.assertRaises((OSError, ValueError, EOFError, wave.Error)):
                    prepare_wave(audio, name, output)


class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_allowed_only_on_ha_socket(self):
        for uid, ha, allowed in ((0, True, True), (0, False, False),
                                 (10001, False, True), (10002, True, False)):
            with self.subTest(uid=uid, ha=ha):
                player = AsyncMock()
                player.cli.return_value = player.ha_status.return_value = {'connected': True}
                reader = asyncio.StreamReader()
                reader.feed_data(b'{"action":"status","params":{}}\n')
                reader.feed_eof()
                writer = MagicMock()
                writer.drain, writer.wait_closed = AsyncMock(), AsyncMock()
                writer.get_extra_info.return_value.getsockopt.return_value = struct.pack('3i', 99, uid, uid)
                with patch('os.getuid', return_value=10001):
                    await serve_cli(reader, writer, player, Path('/ha/audio') if ha else None)
                result = json.loads(writer.write.call_args.args[0])
                self.assertEqual(result['ok'], allowed)

    @unittest.skipUnless(shutil.which('mpv') and os.environ.get('EMOSONIC_REAL_MPV_TESTS') == '1',
                         'requires real mpv and explicit opt-in')
    async def test_wav_over_socket_restores_actual_mpv_music(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            runtime, shared = directory / 'mpv', directory / 'ha'
            audio = shared / 'audio'
            audio.mkdir(parents=True)
            music = directory / 'song.wav'
            write_wave(music, 8000 * 10)
            write_wave(audio / 'notice.wav', 8000)
            engine = MpvPlayer('null', runtime, timeout=5, allow_null_output_for_tests=True)
            wire = MagicMock(ready=True, generation=1, client_id='box', device_session_id='speaker')
            media = MagicMock()
            media.stream_url.return_value = str(music)
            player = Player(wire, engine, media)
            context = dict(playbackContextId='one', authorityClientId='box', authorityDeviceSessionId='speaker',
                           queueRevision=1, controlVersion=1, epoch=1, positionMs=1500,
                           currentIndex=0, state='playing', queueSongIds=['music'])
            player.context, player.executed = dict(context), dict(context)
            await engine.start()
            event_task = asyncio.create_task(player._engine_events())
            try:
                await player._load(context, paused=False)
                before = (await engine.snapshot())['position_ms']
                endpoint = shared / 'control.sock'
                async with control_socket(endpoint, player, audio):
                    self.assertEqual(stat.S_IMODE(endpoint.stat().st_mode), 0o600)
                    result = await call(endpoint, 'announce', {'file': 'notice.wav'})
                    self.assertEqual(result, {'completed': True})
                self.assertFalse(endpoint.exists())
                actual = await engine.snapshot()
                self.assertFalse(actual['paused'])
                self.assertAlmostEqual(actual['position_ms'], before, delta=500)
                self.assertEqual(player.context, context)
            finally:
                event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)
                await player.close()
