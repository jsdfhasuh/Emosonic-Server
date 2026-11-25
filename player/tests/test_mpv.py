import asyncio
import json
import os
import shutil
import struct
import sys
import wave
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emosonic_player.mpv import MpvError, MpvPlayer


class MemoryWriter:
    """In-memory stream transport; not a Unix socket/real mpv acceptance test."""
    def __init__(self, peer):
        self.peer = peer
        self.closed = False

    def write(self, data):
        if self.closed:
            raise BrokenPipeError()
        self.peer.feed_data(data)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        if not self.closed:
            self.closed = True
            self.peer.feed_eof()

    async def wait_closed(self):
        pass

    def is_closing(self):
        return self.closed


class FakeMpv:
    def __init__(self):
        self.props = {"pause": True, "paused-for-cache": False, "core-idle": False,
                      "seeking": False, "idle-active": True, "current-ao": "alsa", "time-pos": 0, "duration": 10, "volume": 100}
        self.entry = 0
        self.writer = None
        self.ignore = False
        self.commands = []

    async def serve(self, reader, writer):
        self.writer = writer
        try:
            while line := await reader.readline():
                req = json.loads(line)
                cmd = req["command"]
                self.commands.append(cmd)
                if self.ignore:
                    continue
                result = None
                error = "success"
                if cmd[0] == "loadfile":
                    self.entry += 1
                    self.props["idle-active"] = False
                    await self.event("start-file", playlist_entry_id=self.entry)
                    await self.event("file-loaded")
                elif cmd[:2] == ["get_property", "playlist"]:
                    result = [{"id": self.entry, "current": True}]
                elif cmd[0] == "get_property":
                    if cmd[1] not in self.props:
                        error = "property unavailable"
                    else:
                        result = self.props[cmd[1]]
                elif cmd[0] == "set_property":
                    self.props[cmd[1]] = cmd[2]
                elif cmd[0] == "stop":
                    self.props["idle-active"] = True
                elif cmd[0] == "seek":
                    self.props["time-pos"] = cmd[1]
                # Fragment response; parser must accept arbitrary stream framing.
                reply = json.dumps({"request_id": req["request_id"], "error": error, "data": result}).encode() + b"\n"
                writer.write(reply[:7])
                await writer.drain()
                await asyncio.sleep(0)
                writer.write(reply[7:])
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()

    async def event(self, name, **fields):
        self.writer.write(json.dumps({"event": name, **fields}).encode() + b"\n")
        await self.writer.drain()


class MpvTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fake = FakeMpv()
        reader, fake_reader = asyncio.StreamReader(), asyncio.StreamReader()
        writer, fake_writer = MemoryWriter(fake_reader), MemoryWriter(reader)
        self.fake_task = asyncio.create_task(self.fake.serve(fake_reader, fake_writer))
        self.player = MpvPlayer("alsa/test", self.temp.name, timeout=0.15)
        self.player._writer = writer
        self.player._generation = 1
        self.player._reader_task = asyncio.create_task(self.player._read(reader, 1))

    async def asyncTearDown(self):
        await self.player.close()
        await self.fake_task
        self.temp.cleanup()

    async def test_load_paused_controls_and_snapshot(self):
        await self.player.load("https://example/secret?token=hidden", "run1", 2000)
        self.assertTrue(self.fake.props["pause"])
        await self.player.play()
        await self.player.seek(5000)
        await self.player.set_volume(25)
        await self.player.pause()
        status = await self.player.snapshot()
        self.assertEqual(status["position_ms"], 5000)
        self.assertEqual(status["volume"], 25)
        self.assertNotIn("secret", str(status))

    async def test_play_rejects_wrong_audio_backend_and_closes_process(self):
        await self.player.load("local.wav", "run1")
        self.fake.props["current-ao"] = "pulse"
        with self.assertRaisesRegex(MpvError, "mpv_audio_output_mismatch"):
            await self.player.play()
        self.assertIsNone(self.player._writer)
        self.assertIsNone(self.player._run_id)

    async def test_eof_once_and_same_song_new_occurrence(self):
        await self.player.load("https://example/song", "occurrence1")
        old = self.fake.entry
        await self.player.load("https://example/song", "occurrence2")
        await self.fake.event("end-file", playlist_entry_id=old, reason="eof")
        await self.fake.event("end-file", playlist_entry_id=self.fake.entry, reason="eof")
        await self.fake.event("end-file", playlist_entry_id=self.fake.entry, reason="eof")
        event = await asyncio.wait_for(self.player.events.get(), 1)
        self.assertEqual((event.kind, event.run_id), ("eof", "occurrence2"))
        await asyncio.sleep(0.01)
        self.assertTrue(self.player.events.empty())

    async def test_stopped_and_error_ends_not_natural_eof(self):
        await self.player.load("url", "r")
        entry = self.fake.entry
        await self.player.stop()
        await self.fake.event("end-file", playlist_entry_id=entry, reason="eof")
        await asyncio.sleep(0.01)
        self.assertTrue(self.player.events.empty())
        await self.player.load("url", "r2")
        await self.fake.event("end-file", playlist_entry_id=self.fake.entry, reason="error", error="SECRET URL")
        event = await asyncio.wait_for(self.player.events.get(), 1)
        self.assertEqual(event.kind, "error")
        self.assertNotIn("SECRET", repr(event))
        with self.assertRaisesRegex(MpvError, 'mpv_no_active_run'):
            await self.player.seek(1000)
        self.assertIsNone(self.player._writer)

    async def test_mpv_rejects_seek_reply_after_reporting_eof(self):
        await self.player.load('old', 'first')
        self.fake.ignore = True
        seek = asyncio.create_task(self.player.seek(2200))
        async with asyncio.timeout(1):
            while self.fake.commands[-1][0] != 'seek':
                await asyncio.sleep(0)
        await self.fake.event('end-file', playlist_entry_id=self.fake.entry, reason='eof')
        await self.fake.event('command-reply', request_id=self.player._request_id, error='error')
        with self.assertRaisesRegex(MpvError, 'mpv_command_failed'):
            await seek
        self.fake.ignore = False
        await self.player.load('new', 'second')
        self.assertEqual((await self.player.snapshot())['run_id'], 'second')

    async def test_eof_arrives_just_after_mpv_rejects_seek(self):
        await self.player.load('old', 'first')
        self.fake.ignore = True
        seek = asyncio.create_task(self.player.seek(2200))
        async with asyncio.timeout(1):
            while self.fake.commands[-1][0] != 'seek':
                await asyncio.sleep(0)
        await self.fake.event('command-reply', request_id=self.player._request_id, error='error')
        async with asyncio.timeout(1):
            while self.fake.commands[-1] != ['get_property', 'idle-active'] and not seek.done():
                await asyncio.sleep(0)
        self.assertFalse(seek.done(), 'unclassified seek failure closed the IPC before checking EOF')
        await self.fake.event('end-file', playlist_entry_id=self.fake.entry, reason='eof')
        await self.fake.event('command-reply', request_id=self.player._request_id, error='success', data=True)
        with self.assertRaisesRegex(MpvError, 'mpv_command_failed'):
            await seek
        self.fake.ignore = False
        await self.player.load('new', 'second')
        self.assertEqual((await self.player.snapshot())['run_id'], 'second')

    async def test_rejected_seek_without_matching_eof_still_closes_mpv(self):
        await self.player.load('old', 'first')
        self.fake.props['idle-active'] = True
        command = self.player._command
        async def reject_seek(args):
            if args[0] == 'seek':
                raise MpvError('mpv_command_failed')
            return await command(args)

        with patch.object(self.player, '_command', reject_seek):
            with self.assertRaisesRegex(MpvError, 'mpv_command_failed'):
                await self.player.seek(2200)
        self.assertIsNone(self.player._writer)

    async def test_eof_during_seek_or_snapshot_rejects_operation_but_keeps_idle_mpv(self):
        command = self.player._command

        for trigger, operation, expected_error in (
                (['seek', 2.2, 'absolute+exact'], lambda: self.player.seek(2200), 'mpv_no_active_run'),
                (['get_property', 'duration'], self.player.snapshot, 'mpv_snapshot_superseded')):
            with self.subTest(error=expected_error):
                await self.player.load('old', 'first')
                async def end_during_command(args):
                    result = await command(args)
                    if args == trigger:
                        await self.fake.event('end-file', playlist_entry_id=self.fake.entry, reason='eof')
                        event = await asyncio.wait_for(self.player.events.get(), 1)
                        self.assertEqual((event.kind, event.run_id), ('eof', 'first'))
                    return result

                with patch.object(self.player, '_command', end_during_command):
                    with self.assertRaisesRegex(MpvError, expected_error):
                        await operation()
                await self.player.load('new', 'second')
                self.assertEqual((await self.player.snapshot())['run_id'], 'second')

    async def test_timeout_closes_channel_and_clears_pending(self):
        await self.player.load("url", "r")
        self.fake.ignore = True
        with self.assertRaisesRegex(MpvError, "mpv_command_interrupted"):
            await self.player.pause()
        self.assertIsNone(self.player._writer)
        self.assertFalse(self.player._pending)
        self.assertIsNone(self.player._run_id)

    async def test_cancel_fences_player(self):
        await self.player.load("url", "r")
        self.fake.props["core-idle"] = True
        task = asyncio.create_task(self.player.play())
        await asyncio.sleep(0.03)
        task.cancel()
        with self.assertRaises((asyncio.CancelledError, MpvError)):
            await task
        self.assertIsNone(self.player._writer)
        self.assertIsNone(self.player._run_id)

    async def test_unknown_response_and_event_interleaving(self):
        await self.player.load("url", "r")
        self.fake.writer.write(b'{"request_id":9999,"error":"success"}\n{"event":"irrelevant"}\n')
        await self.fake.writer.drain()
        await self.player.pause()
        self.assertTrue((await self.player.snapshot())["paused"])

    async def test_disconnect_is_not_eof(self):
        await self.player.load("url", "r")
        self.fake.writer.close()
        event = await asyncio.wait_for(self.player.events.get(), 1)
        self.assertEqual((event.kind, event.run_id), ("disconnected", "r"))
        self.assertIsNone(self.player._run_id)

    async def test_old_generation_reader_does_not_commit_events(self):
        self.player._generation = 2
        await self.fake.event("start-file", playlist_entry_id=99)
        await asyncio.sleep(0.01)
        self.assertIsNone(self.player._event_entry)

    async def test_invalid_volume_and_position_rejected(self):
        with self.assertRaises(ValueError):
            await self.player.set_volume(101)
        with self.assertRaises(ValueError):
            await self.player.load("url", "r", float("nan"))

    def test_requires_explicit_output(self):
        for value in ("", "auto", "null", "pulse/default", "alsa/", "alsa/default",
                      "alsa/null", "alsa/sysdefault"):
            with self.assertRaises(ValueError):
                MpvPlayer(value, self.temp.name)

    async def test_float_output_only_for_plughw(self):
        devices = (
            ("alsa/plughw:CARD=Audio,DEV=0", True),
            ("alsa/plughw", True),
            ("alsa/hw:CARD=Audio,DEV=0", False),
            ("alsa/test", False),
            ("null", False),
        )
        for device, expect_float in devices:
            with self.subTest(device=device):
                player = MpvPlayer(device, self.temp.name, allow_null_output_for_tests=True)
                with patch("asyncio.create_subprocess_exec", side_effect=OSError) as launch:
                    with self.assertRaisesRegex(MpvError, "mpv_start_failed"):
                        await player.start()
                arguments = launch.await_args.args
                route = "--ao=null" if device == "null" else "--audio-device=" + device
                self.assertIn(route, arguments)
                self.assertEqual("--audio-format=float" in arguments, expect_float)


@unittest.skipUnless(shutil.which("mpv") and os.environ.get("EMOSONIC_REAL_MPV_TESTS") == "1",
                     "requires mpv and explicit non-physical integration opt-in")
class RealMpvTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "requires ALSA plug/file plugins")
    async def test_plughw_low_volume_reaches_s16_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "signal.wav"
            capture = root / "capture.s16"
            config = root / "alsa.conf"
            config.write_text(
                "pcm.null { type null }\n"
                "pcm.plughw { type plug slave { pcm capture format S16_LE rate 48000 channels 2 } }\n"
                f'pcm.capture {{ type file slave.pcm null file "{capture}" format raw }}\n'
            )
            with wave.open(str(media), "wb") as stream:
                stream.setparams((2, 2, 44100, 0, "NONE", ""))
                stream.writeframes((b"\x00\x40" * 128 + b"\x00\xc0" * 128) * 384)
            player = MpvPlayer("alsa/plughw", directory, timeout=5)
            try:
                with patch.dict(os.environ, ALSA_CONFIG_PATH=str(config)):
                    await player.start()
                await player.set_volume(10)
                await player.load(str(media), "low-volume")
                # ALSA's null slave can drain the file before play() samples the live state.
                await player._command(["set_property", "pause", False])
                event = await asyncio.wait_for(player.events.get(), 8)
                self.assertEqual((event.kind, event.run_id), ("eof", "low-volume"))
            finally:
                await player.close()
            samples = struct.iter_unpack("<h", capture.read_bytes())
            peak = max(abs(sample) for (sample,) in samples)
            # Half-scale signal with mpv's cubic gain at 10% is about 16 S16 counts.
            self.assertGreater(peak, 8)
            self.assertLess(peak, 32)

    async def test_real_process_load_seek_and_eof(self):
        # Null audio validates process/IPC, never claims USB output or audible sound.
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "tone.wav"
            with wave.open(str(media), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(8000)
                stream.writeframes(b"\x00\x00" * 16000)
            player = MpvPlayer("null", directory, timeout=5,
                               allow_null_output_for_tests=True)
            try:
                await player.start()
                await player.load(str(media), "real-run")
                self.assertTrue((await player.snapshot())["paused"])
                await player.seek(500)
                await player.play()
                process = player._process
                event = await asyncio.wait_for(player.events.get(), 8)
                self.assertEqual((event.kind, event.run_id), ("eof", "real-run"))
                with self.assertRaisesRegex(MpvError, "mpv_no_active_run"):
                    await player.seek(1500)
                await player.load(str(media), "next-run")
                self.assertIs(player._process, process)
                self.assertEqual((await player.snapshot())["run_id"], "next-run")
            finally:
                await player.close()


if __name__ == "__main__":
    unittest.main()
