"""Loopback-only Follow acceptance with two independent real mpv null audio processes."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from emosonic_player.follow import Follow
from emosonic_player.mpv import MpvPlayer
from emosonic_player.playback import Player
from emosonic_player.protocol import ProtocolClient
from emosonic_player.stream import MediaClient


@unittest.skipUnless(os.environ.get('EMOSONIC_QUEUE_FIXTURE'), 'requires disposable strict loopback fixture')
class FollowLoopbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = json.loads(Path(os.environ['EMOSONIC_QUEUE_FIXTURE']).read_text())
        self.credentials = (f"http://127.0.0.1:{self.fixture['port']}", self.fixture['username'],
                            self.fixture['password'])

    async def new_player(self, name, runtime=None, client=None, session=None):
        if runtime is None:
            temp = tempfile.TemporaryDirectory(prefix='fsync-')
            self.addCleanup(temp.cleanup)
            runtime = temp.name
        identity = client or name + '-' + uuid.uuid4().hex[:8]
        wire = ProtocolClient(*self.credentials, identity, session or identity + '-session', name)
        engine = MpvPlayer('null', runtime, allow_null_output_for_tests=True)
        media = MediaClient(*self.credentials)
        self.addAsyncCleanup(media.close)
        self.addAsyncCleanup(wire.close)
        player = Player(wire, engine, media, runtime)
        self.addAsyncCleanup(player.close)
        wire.on_event = player.on_event
        wire.on_disconnect = player.disconnected
        await engine.start()
        await wire.connect()
        await player.start()
        await player.reconcile()
        return player

    async def test_restart_releases_lease_for_recovered_pair_and_new_connection_nonce(self):
        source, follower = await self.new_player('restart-source'), await self.new_player('restart-follower')
        original, first, _ = self.fixture['songs']
        for player, song in ((source, first), (follower, original)):
            await player.cli('play', {'song': song})
            async with asyncio.timeout(8):
                while player.applied != player.context['controlVersion'] or not player.run_id:
                    await asyncio.sleep(.08)
        await follower.cli('follow', {'context': source.context['playbackContextId']})
        path = follower.follow.path
        client, nonce = follower.wire.client_id, follower.wire.nonce
        old_context = follower.context['playbackContextId']
        await asyncio.wait_for(follower.wire.close(), 6)
        await follower.disconnected()
        await follower.close()
        self.assertTrue(path.exists())
        session = Follow.recovery_session(path.parent, self.credentials[0], self.credentials[1], client)
        self.assertEqual(session, follower.wire.device_session_id)
        recovered = await self.new_player('restart-follower', path.parent, client, session)
        self.assertNotEqual(recovered.wire.nonce, nonce)
        self.assertFalse(path.exists())
        self.assertFalse(recovered.follow.active)
        self.assertEqual(recovered.context['playbackContextId'], old_context)
        self.assertEqual(recovered.context['queueSongIds'], [original])
        self.assertTrue((await recovered.engine.snapshot())['paused'])
        await recovered.cli('resume', {})
        async with asyncio.timeout(8):
            while (await recovered.engine.snapshot())['paused']:
                await asyncio.sleep(.08)

    async def test_selected_source_track_seek_pause_restore_on_two_real_players(self):
        source, follower = await self.new_player('source'), await self.new_player('follower')
        original, first, second = self.fixture['songs']

        async def wait_settled(player, track, paused):
            deadline = time.monotonic() + 10
            while True:
                sample = await player.engine.snapshot()
                ctx = player.context
                if (ctx.get('trackId') == track and sample['run_id']
                        and sample['paused'] is paused and player.applied == ctx['controlVersion']):
                    return sample
                if time.monotonic() >= deadline:
                    self.fail(f'ordinary: expected track={track} paused={paused}; actual track={ctx.get("trackId")} '
                              f'native={sample} applied={player.applied} control={ctx["controlVersion"]}')
                await asyncio.sleep(.08)

        async def wait_mirror(track, paused, *, minimum=0):
            deadline = time.monotonic() + 12
            while True:
                a, b = await asyncio.gather(source.engine.snapshot(), follower.engine.snapshot())
                if (follower.follow.track == track and b['paused'] is paused and a['paused'] is paused
                        and b['position_ms'] is not None and a['position_ms'] is not None
                        and b['position_ms'] >= minimum and abs(a['position_ms'] - b['position_ms']) <= 50):
                    return b['position_ms']
                if time.monotonic() >= deadline:
                    fact = follower.follow.fact or {}
                    self.fail(f'mirror: expected track={track} paused={paused} min={minimum} source={a} follower={b} '
                              f'status={follower.follow.status()} sourceFactState={fact.get("state")} '
                              f'sourceFactPosition={fact.get("positionMs")} sourceFactSeq={fact.get("clientSeq")}')
                await asyncio.sleep(.1)

        async def timed_position(engine):
            start = time.monotonic()
            # Time only the single raw mpv clock read; volume, duration and
            # output checks in the full application snapshot have independent IPC.
            position = await engine._command(['get_property', 'audio-pts'])
            end = time.monotonic()
            snapshot = await engine.snapshot()
            self.assertFalse(snapshot['paused'] or snapshot['buffering'])
            return position * 1000, snapshot['speed'], (start + end) / 2, (end - start) / 2

        async def stable_drift(track):
            # Compare positions at a common monotonic instant. Include the full
            # timestamp uncertainty for both native IPC reads in the error bound.
            readings = []
            async def measure():
                self.assertEqual(follower.follow.track, track)
                a, b = await asyncio.gather(timed_position(source.engine), timed_position(follower.engine))
                when = max(a[2], b[2])
                positions = [s[0] + (when - s[2]) * 1000 * s[1] for s in (a, b)]
                uncertainty = sum(s[3] * 1000 * s[1] for s in (a, b))
                drift = positions[1] - positions[0]
                return abs(drift) + uncertainty, round(drift), round(uncertainty), follower.follow.drift

            # Startup may need a seek and a speed adjustment; once settled, every
            # sample over the following nine seconds must meet the same 50 ms limit.
            settled = 0
            deadline = time.monotonic() + 12
            while settled < 4:
                sample = await measure()
                settled = settled + 1 if sample[0] <= 50 else 0
                if time.monotonic() >= deadline:
                    self.fail(f'Follow did not settle: last sample={sample}, status={follower.follow.status()}')
                await asyncio.sleep(.25)
            start = time.monotonic()
            while True:
                readings.append(await measure())
                if time.monotonic() - start >= 9:
                    break
                await asyncio.sleep(.25)
            maximum = max(r[0] for r in readings)
            print(f'Follow real mpv: {len(readings)} samples over {time.monotonic() - start:.2f}s, '
                  f'max error including IPC sampling {maximum:.1f} ms', flush=True)
            self.assertGreaterEqual(len(readings), 30)
            self.assertLessEqual(maximum, 50, [(r[1], r[2], r[3]) for r in readings])

        await follower.cli('play', {'song': original})
        await wait_settled(follower, original, False)
        await follower.cli('pause', {})
        old = await wait_settled(follower, original, True)
        await source.cli('play', {'song': first})
        await wait_settled(source, first, False)
        ctx_id = source.context['playbackContextId']
        await follower.cli('follow', {'context': ctx_id})
        position = await wait_mirror(first, False)
        await wait_mirror(first, False, minimum=position + 500)
        await source.cli('seek', {'seconds': 9})
        await wait_mirror(first, False, minimum=8500)
        await source.cli('pause', {})
        await wait_settled(source, first, True)
        await wait_mirror(first, True, minimum=8500)
        await source.cli('play', {'song': second})
        await wait_settled(source, second, False)
        await wait_mirror(second, False)
        await stable_drift(second)
        self.assertEqual((await follower.cli('follow-status', {}))['sourcePlaybackContextId'], ctx_id)
        self.assertEqual(follower.context['queueSongIds'], [original])
        await follower.cli('follow-stop', {})
        restored = await wait_settled(follower, original, True)
        self.assertLess(abs(restored['position_ms'] - old['position_ms']), 350)
        self.assertFalse(follower.follow.active)
