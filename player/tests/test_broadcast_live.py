"""Two real player clients + two null-output mpv instances against the disposable server."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
import uuid

from emosonic_player.mpv import MpvPlayer
from emosonic_player.playback import Player
from emosonic_player.protocol import ProtocolClient
from emosonic_player.stream import MediaClient


@unittest.skipUnless(os.environ.get('EMOSONIC_QUEUE_FIXTURE'), 'requires the disposable broadcast server')
class LiveBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_controls_and_restore_both_players(self):
        fixture = json.loads(Path(os.environ['EMOSONIC_QUEUE_FIXTURE']).read_text())
        credentials = (f"http://127.0.0.1:{fixture['port']}", fixture['username'], fixture['password'])
        original, first, second = fixture['songs']
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        peers = []
        for index in range(2):
            runtime = Path(tmp.name) / str(index)
            runtime.mkdir(mode=0o700)
            media = MediaClient(*credentials)
            self.addAsyncCleanup(media.close)
            engine = MpvPlayer('null', runtime, allow_null_output_for_tests=True)
            await engine.start()
            client = 'test-broadcast-' + uuid.uuid4().hex
            wire = ProtocolClient(*credentials, client, 'device:' + client, client)
            player = Player(wire, engine, media, state_dir=runtime)
            wire.on_event = player.on_event
            wire.on_disconnect = player.disconnected
            await wire.connect()
            self.addAsyncCleanup(wire.close)
            await player.start()
            self.addAsyncCleanup(player.close)
            await player.reconcile()
            self.assertTrue(wire.negotiated['supportsBroadcast'])
            peers.append(player)
        source, mirror = peers

        async def applied(player, state):
            async with asyncio.timeout(15):
                while True:
                    info = (await player.wire.request('playback.context.status',
                        {'playbackContextId': player.context['playbackContextId']}, type='state',
                        reply_actions={'playback.context.status'}))['payload']
                    ctx = info['playbackContext']
                    if (player.executed and player.executed['state'] == state and ctx['state'] == state
                            and player.applied == player.context['controlVersion'] == ctx['controlVersion']
                            and any(d.get('state') == state and d['appliedControlVersion'] == player.applied
                                    for d in info['deviceStates'])):
                        return
                    await asyncio.sleep(.05)

        async def status():
            return await source.cli('broadcast-status', {})

        async def mirrored(track, state, minimum=0, minimum_control=0):
            async with asyncio.timeout(20):
                while True:
                    summary = await status()
                    self.assertIn('broadcast', summary,
                        f'broadcast absent: ready={source.wire.ready} local={source.broadcast.status()} '
                        f'participant={mirror.broadcast.status()} keys={list(summary)}')
                    snap = summary['broadcast']
                    pair = summary['participantStates'][0]
                    if (pair.get('syncStatus') == 'applied' and pair.get('trackId') == track
                            and pair.get('state') == state and snap['state'] == state and
                            snap['sourceControlVersion'] >= minimum_control and
                            pair['appliedBroadcastRevision'] >= minimum):
                        return snap, pair
                    if pair.get('syncStatus') == 'failed':
                        self.fail('mirror feedback failed: ' + str(pair.get('errorCode')))
                    await asyncio.sleep(.35)

        async def song(player):
            path = await player.engine._command(['get_property', 'path'])
            # Authenticated URLs must never appear in test output.
            return parse_qs(urlsplit(path).query)['id'][0]

        await mirror.cli('play', {'song': original})
        await applied(mirror, 'playing')
        await mirror.cli('pause', {})
        await applied(mirror, 'paused')
        await mirror.cli('seek', {'seconds': 5.6})
        try:
            async with asyncio.timeout(10):
                while (mirror.applied != mirror.context['controlVersion'] or
                       abs((await mirror.engine.snapshot())['position_ms'] - 5600) > 250):
                    await asyncio.sleep(.05)
        except TimeoutError:
            sample = await mirror.engine.snapshot()
            self.fail(f"initial seek: position={sample['position_ms']} paused={sample['paused']} "
                      f"applied={mirror.applied} canonical={mirror.context['controlVersion']} error={mirror.error}")
        frozen_position = (await mirror.engine.snapshot())['position_ms']
        async with source.lock:
            await source._sync_queue([first, second], 0)
        await source.cli('resume', {})
        await applied(source, 'playing')
        source_run = source.run_id
        start = await source.cli('broadcast-start', {'participants': [mirror.wire.client_id]})
        self.assertEqual(start['participants'], [mirror.wire.client_id])
        self.assertEqual(start['skippedClientIds'], [])
        snap, pair = await mirrored(first, 'playing')
        self.assertEqual(source.run_id, source_run)
        self.assertEqual(await song(mirror), first)
        self.assertFalse((await mirror.engine.snapshot())['paused'])
        self.assertEqual(mirror.executed['queueSongIds'], [original])
        await source.cli('broadcast-pause', {})
        snap, pair = await mirrored(first, 'paused', snap['broadcastRevision']+1,
                                    snap['sourceControlVersion']+1)
        self.assertTrue((await source.engine.snapshot())['paused'])
        self.assertTrue((await mirror.engine.snapshot())['paused'])
        await source.cli('broadcast-seek', {'position_ms': 4100})
        snap, pair = await mirrored(first, 'paused', snap['broadcastRevision']+1,
                                    snap['sourceControlVersion']+1)
        # Each real mpv seek permits a 250 ms demuxer tolerance. A subsequent
        # source correction can project its observed position to the participant.
        self.assertLessEqual(abs(snap['positionMs'] - 4100), 250)
        self.assertLessEqual(abs(pair['positionMs'] - snap['positionMs']), 250)
        await source.cli('broadcast-play-item', {'index': 1})
        snap, pair = await mirrored(second, 'playing', snap['broadcastRevision']+1)
        self.assertEqual(await song(mirror), second)
        self.assertEqual(await song(source), second)
        await source.cli('broadcast-play-item', {'index': 0})
        snap, pair = await mirrored(first, 'playing', snap['broadcastRevision']+1)
        await source.cli('broadcast-seek', {'position_ms': 55000})
        snap, pair = await mirrored(first, 'playing', snap['broadcastRevision']+1)
        # The source reaches EOF and publishes its own ordinary queue transition.
        # The participant follows that projection without advancing its own queue.
        snap, pair = await mirrored(second, 'playing', snap['broadcastRevision']+1)
        self.assertEqual(await song(source), second)
        self.assertEqual(await song(mirror), second)
        continuing_run = source.run_id
        await source.cli('broadcast-stop', {})
        async with asyncio.timeout(15):
            while mirror.broadcast.blocks_core or source.broadcast.occupied:
                await asyncio.sleep(.05)
        self.assertTrue((await mirror.engine.snapshot())['paused'])
        self.assertEqual(await song(mirror), original)
        self.assertLessEqual(abs((await mirror.engine.snapshot())['position_ms'] - frozen_position), 250)
        self.assertEqual(continuing_run, source.run_id)
        self.assertEqual(await song(source), second)
        self.assertFalse((await source.engine.snapshot())['paused'])
