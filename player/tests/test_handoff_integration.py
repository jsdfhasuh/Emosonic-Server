"""Run against the opt-in loopback fixture in test_server_integration.py (all profiles enabled)."""
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


@unittest.skipUnless(os.environ.get('EMOSONIC_QUEUE_FIXTURE'), 'requires disposable advanced-profile server and mpv')
class LiveHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = json.loads(Path(os.environ['EMOSONIC_QUEUE_FIXTURE']).read_text())
        self.edges = []
        self.hold_commit = asyncio.Event()
        self.hold_commit.set()
        self.commit_received = asyncio.Event()
        self.song = self.fixture['songs'][0]
        credentials = (f"http://127.0.0.1:{self.fixture['port']}", self.fixture['username'], self.fixture['password'])
        self.players = []
        for role in ('first', 'second'):
            runtime = tempfile.TemporaryDirectory()
            self.addCleanup(runtime.cleanup)
            engine = MpvPlayer('null', runtime.name, allow_null_output_for_tests=True)
            await engine.start()
            self.addAsyncCleanup(engine.close)
            media = MediaClient(*credentials)
            self.addAsyncCleanup(media.close)
            name = role + '-' + uuid.uuid4().hex[:10]
            wire = ProtocolClient(*credentials, name, name + '-session', role)
            self.addAsyncCleanup(wire.close)
            player = Player(wire, engine, media)

            async def receive(message, local=player):
                action, payload = message['action'], message['payload']
                if action == 'playback.handoff.status':
                    self.edges.append((local.wire.client_id, payload.get('handoffId'), payload.get('status')))
                if action == 'player.play' and payload.get('handoffId'):
                    self.commit_received.set()
                    await self.hold_commit.wait()
                await local.on_event(message)

            wire.on_event = receive
            await wire.connect()
            self.assertTrue(wire.negotiated['effectiveAtPlayback'])
            self.assertTrue(wire.negotiated['playbackPrepare'])
            await player.start()
            self.addAsyncCleanup(player.close)
            await player.reconcile()
            self.players.append(player)
        self.addCleanup(self.hold_commit.set)
        self.source, self.target = self.players
        await self.source.cli('play', {'song': self.song})
        await self.until(lambda: self.source.run_id and (self.source.executed or {}).get('state') == 'playing')
        self.context_id = self.source.context['playbackContextId']

    async def until(self, test):
        async with asyncio.timeout(12):
            while not test():
                await asyncio.sleep(.03)

    async def check_transfer(self, old, new):
        before = old.context.copy()
        ack = await old.cli('handoff', {'target': new.wire.client_id})
        hid = ack['handoffId']
        await self.until(lambda: (new.wire.client_id, hid, 'completed') in self.edges)
        await self.until(lambda: new.run_id and new.executed.get('playbackContextId') == self.context_id
                         and old.run_id is None and (old.context or {}).get('state') == 'idle')
        for peer in self.players:
            reply = await peer.wire.request('playback.context.status', {'playbackContextId': self.context_id},
                                            type='state', reply_actions={'playback.context.status'})
            context = reply['payload']['playbackContext']
            self.assertEqual(context['authorityClientId'], new.wire.client_id)
            self.assertEqual(context['authorityDeviceSessionId'], new.wire.device_session_id)
            self.assertEqual(context['epoch'], before['epoch'] + 1)
            self.assertEqual(context['controlVersion'], before['controlVersion'] + 1)
            self.assertEqual(context['queueRevision'], before['queueRevision'])
            self.assertEqual(context['queueSongIds'], [self.song])
        sample = await new.engine.snapshot()
        self.assertEqual(sample['run_id'], new.run_id)
        self.assertIs(sample['paused'], False)
        self.assertIs(sample['buffering'], False)
        self.assertGreater(sample['position_ms'], 0)
        self.assertEqual(await new.engine._command(['get_property', 'current-ao']), 'null')
        uri = urlsplit(await new.engine._command(['get_property', 'path']))
        self.assertEqual(parse_qs(uri.query)['id'], [self.song])
        self.assertIsNone((await old.engine.snapshot())['run_id'])
        print(f"Handoff confirmed, epoch={context['epoch']} version={context['controlVersion']} "
              f"queueRevision={context['queueRevision']} mpvPositionMs={round(sample['position_ms'])}", flush=True)

    async def test_real_mpv_round_trip(self):
        await self.check_transfer(self.source, self.target)
        await self.check_transfer(self.target, self.source)

    async def test_real_cancel_before_commit_keeps_original_mpv(self):
        self.hold_commit.clear()
        original_run = self.source.run_id
        ack = await self.source.cli('handoff', {'target': self.target.wire.client_id})
        await asyncio.wait_for(self.commit_received.wait(), 6)
        preloaded = await self.target.engine.snapshot()
        self.assertIsNotNone(preloaded['run_id'])
        self.assertTrue(preloaded['paused'])
        await self.source.cli('handoff-cancel', {})
        await self.until(lambda: (self.target.wire.client_id, ack['handoffId'], 'cancelled') in self.edges)
        self.hold_commit.set()
        await self.until(lambda: not self.target.handoff.busy)
        self.assertIsNone((await self.target.engine.snapshot())['run_id'])
        source = await self.source.engine.snapshot()
        self.assertEqual(source['run_id'], original_run)
        self.assertFalse(source['paused'])
        reply = await self.target.wire.request('playback.context.status', {'playbackContextId': self.context_id},
                                               type='state', reply_actions={'playback.context.status'})
        self.assertEqual(reply['payload']['playbackContext']['authorityClientId'], self.source.wire.client_id)
