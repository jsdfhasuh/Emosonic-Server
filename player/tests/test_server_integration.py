"""Actual server handler integration, in-process transport and fake audio only.

This exercises unmodified Flask/Socket.IO/database code. It does not establish
network, ARM64 container, USB audio, or physical output acceptance.
"""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from emosonic_player.playback import Player
from emosonic_player.protocol import ProtocolClient, ProtocolError

try:
    from supysonic.db import release_database
    from supysonic.emo.ws import socketio
    from supysonic.emo.ws_state import get_state
    from supysonic.managers.user import UserManager
    from supysonic.web import create_application
    from tests.testbase import TestConfig
    SERVER_AVAILABLE = True
except ImportError:
    SERVER_AVAILABLE = False


class Hub:
    def __init__(self, app):
        self.app = app
        self.connections = []

    def factory(self):
        transport = Bridge(self)
        self.connections.append(transport)
        return transport

    async def pump(self):
        for connection in self.connections:
            if connection.connected:
                for event in connection.client.get_received('/emo'):
                    if event['name'] == 'message':
                        args = event['args']
                        message = args[0] if isinstance(args, list) else args
                        await connection.handlers['message'](message)
        await asyncio.sleep(0)


class Bridge:
    """Socket factory adapter; only transport is replaced, not server handlers."""
    def __init__(self, hub):
        self.hub = hub
        self.handlers = {}
        self.sent = []
        self.connected = False

    def on(self, event, handler, namespace):
        self.handlers[event] = handler

    async def connect(self, url, **kwargs):
        self.client = socketio.test_client(self.hub.app, namespace='/emo',
                                          flask_test_client=self.hub.app.test_client())
        self.connected = True
        await self.hub.pump()

    async def emit(self, event, data, namespace):
        self.sent.append(data)
        self.client.emit(event, data, namespace=namespace)
        await self.hub.pump()

    async def disconnect(self):
        if self.connected:
            self.connected = False
            if self.client.is_connected('/emo'):
                self.client.disconnect(namespace='/emo')
            await self.handlers['disconnect']()


class FakeEngine:
    def __init__(self):
        self.events = asyncio.Queue()
        self.run = None
        self.loads = []
        self.position = 0
        self.paused = True
        self.volume = 25

    async def load(self, url, run_id, position_ms=0):
        self.run = run_id
        self.loads.append((url, run_id))
        self.position, self.paused = position_ms, True

    async def play(self):
        self.paused = False

    async def pause(self):
        self.paused = True

    async def seek(self, position_ms):
        self.position = position_ms

    async def set_volume(self, volume):
        self.volume = volume

    async def snapshot(self):
        return {'paused': self.paused, 'buffering': False,
                'position_ms': self.position, 'volume': self.volume}

    async def stop(self):
        self.paused = True
        self.run = None

    async def close(self):
        await self.stop()


class FakeMedia:
    def stream_url(self, song):
        return 'https://example.invalid/' + song

    async def playlist(self, playlist):
        return ['track-a', 'track-b', 'track-c']

    async def search(self, query):
        return [{'id': 'track-a'}]


@unittest.skipUnless(SERVER_AVAILABLE, 'server requirements unavailable')
class ServerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config = TestConfig(False, False)
        config.TESTING = False  # exercise explicit development mode, not TESTING escape hatch
        config.BASE['database_uri'] = 'sqlite:///' + str(Path(self.temp.name) / 'test.sqlite')
        config.WEBAPP.update(cache_dir=self.temp.name, mount_emosonic=True,
                             emo_development_mode=True,
                             emo_strict_v2_allow_local_test_evidence=True,
                             emo_strict_v2_core_enabled=True,
                             emo_strict_v2_follow_enabled=False,
                             emo_strict_v2_handoff_enabled=False,
                             emo_strict_v2_broadcast_enabled=False)
        self.app = create_application(config)
        UserManager.add('player-test-user', 'local-test-password', admin=True)
        state = get_state()
        for name in ('_sessions', '_client_to_sid', '_clients', '_playback_contexts',
                     '_device_playback_states', '_strict_feedback_sequences',
                     '_device_volume_states', '_strict_device_volume_sequences',
                     '_playback_context_subscriptions', '_handoffs', '_pending_prepares'):
            getattr(state, name).clear()
        self.hub = Hub(self.app)
        self.wire = ProtocolClient('http://localhost', 'player-test-user', 'local-test-password',
                                   'new-headless-box', 'new-instance', 'OpenWrt',
                                   socket_factory=self.hub.factory, timeout=1)
        self.engine = FakeEngine()
        self.player = Player(self.wire, self.engine, FakeMedia())
        self.wire.on_event = self.player.on_event
        self.wire.on_disconnect = self.player.disconnected
        self.controller = None
        await self.wire.connect()
        await self.player.start()
        await self.player.reconcile()

    async def asyncTearDown(self):
        if self.controller:
            await self.controller.close()
        await self.player.close()
        await self.wire.close()
        release_database()
        self.temp.cleanup()

    async def until(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await self.hub.pump()
                await asyncio.sleep(.01)

    async def test_new_identity_cli_and_app_share_canonical_queue(self):
        self.assertTrue(self.wire.ready)
        self.assertEqual(self.player.context['queueSongIds'], [])
        await self.player.cli('play', {'playlist': 'list'})
        await self.until(lambda: self.player.run_id is not None and not self.engine.paused)
        await self.until(lambda: self.player.error is None and self.player.applied == self.player.context['controlVersion'])
        self.assertEqual(len(self.engine.loads), 1)
        self.assertEqual(self.player.executed['currentIndex'], 0)
        self.controller = ProtocolClient('http://localhost', 'player-test-user', 'local-test-password',
                                         'phone-controller', 'phone-instance', 'Phone',
                                         socket_factory=self.hub.factory, timeout=1)
        await self.controller.connect()
        devices = await self.controller.request('device.list', {}, type='state',
                                                reply_actions={'device.list'})
        self.assertIn('new-headless-box', [d['clientId'] for d in devices['payload']['devices']])
        await self.controller.request('player.pause', {
            'playbackContextId': self.player.context['playbackContextId'],
            'baseControlVersion': self.player.context['controlVersion']})
        await self.until(lambda: self.engine.paused)
        await self.until(lambda: self.player.applied == self.player.context['controlVersion'])
        await self.player.cli('resume', {})
        await self.until(lambda: not self.engine.paused)
        await self.until(lambda: self.player.applied == self.player.context['controlVersion'])
        previous = self.player.run_id
        await self.engine.events.put(SimpleNamespace(kind='eof', run_id=previous))
        await self.until(lambda: len(self.engine.loads) == 2)
        await self.until(lambda: self.player.executed['currentIndex'] == 1)
        self.assertEqual(self.player.context['currentIndex'], 1)
        await self.engine.events.put(SimpleNamespace(kind='eof', run_id=previous))
        await asyncio.sleep(.05)
        self.assertEqual(len(self.engine.loads), 2)

    async def test_cli_seek_next_volume_stop_real_contract(self):
        await self.player.cli('play', {'playlist': 'list'})
        await self.until(lambda: self.player.run_id is not None and not self.engine.paused)
        await self.until(lambda: self.player.applied == self.player.context['controlVersion'])
        await self.player.cli('seek', {'seconds': '12'})
        await self.until(lambda: self.engine.position == 12000)
        await self.until(lambda: self.player.applied == self.player.context['controlVersion'])
        await self.player.cli('next', {})
        await self.until(lambda: self.player.executed['currentIndex'] == 1)
        await self.until(lambda: self.player.applied == self.player.context['controlVersion'])
        await self.player.cli('volume', {'value': '37'})
        await self.until(lambda: self.engine.volume == 37)
        await asyncio.sleep(.1)
        self.assertIsNone(self.player.error)
        await self.player.cli('stop', {})
        await self.until(lambda: self.player.context['queueSongIds'] == [])
        self.assertTrue(self.engine.paused)

    async def test_production_readiness_rejected_without_test_switch(self):
        self.app.config['WEBAPP'].update(emo_development_mode=False,
                                         emo_strict_v2_allow_local_test_evidence=False)
        # Application testing alone is deliberately turned off here; no mock.
        self.app.config['TESTING'] = False
        rejected = ProtocolClient('http://localhost', 'player-test-user', 'local-test-password',
                                  'production-check', 'production-instance', 'Rejected',
                                  socket_factory=self.hub.factory, timeout=1)
        try:
            with self.assertRaises(ProtocolError):
                await rejected.connect()
            self.assertFalse(rejected.ready)
        finally:
            await rejected.close()

    async def test_cross_account_control_rejected(self):
        await self.player.cli('play', {'playlist': 'list'})
        await self.until(lambda: self.player.run_id is not None and not self.engine.paused)
        await self.until(lambda: self.player.applied == self.player.context['controlVersion'])
        UserManager.add('other-test-account', 'other-test-password')
        self.controller = ProtocolClient('http://localhost', 'other-test-account', 'other-test-password',
                                         'other-phone', 'other-instance', 'Other',
                                         socket_factory=self.hub.factory, timeout=1)
        await self.controller.connect()
        devices = await self.controller.request('device.list', {}, type='state',
                                                reply_actions={'device.list'})
        self.assertNotIn('new-headless-box', [d['clientId'] for d in devices['payload']['devices']])
        with self.assertRaises(ProtocolError):
            await self.controller.request('player.pause', {
                'playbackContextId': self.player.context['playbackContextId'],
                'baseControlVersion': self.player.context['controlVersion']})
        self.assertFalse(self.engine.paused)

    async def app_controller(self):
        self.controller = ProtocolClient('http://localhost', 'player-test-user', 'local-test-password',
                                         'app-cold-start', 'app-instance', 'Phone',
                                         socket_factory=self.hub.factory, timeout=1)
        await self.controller.connect()
        await self.controller.request('playback.context.subscribe', {
            'playbackContextId': self.player.context['playbackContextId']}, type='state')
        return self.controller

    async def test_app_prepares_empty_context_then_plays_without_cli(self):
        controller = await self.app_controller()
        context_id = self.player.context['playbackContextId']
        prepared = controller.expect_event('playback.context.prepared', lambda m: (
            m['payload'].get('intentId') == 'cold-app-intent'))
        await controller.request('playback.context.prepare', {
            'playbackContextId': context_id, 'intentId': 'cold-app-intent',
            'baseControlVersion': self.player.context['controlVersion'],
            'initialQueueSongIds': ['first-song', 'next-song'],
            'currentIndex': 0, 'positionMs': 700})
        canonical = (await prepared)['payload']
        self.assertTrue(canonical['ready'])
        await self.until(lambda: self.player.context['queueSongIds'] == ['first-song', 'next-song'])
        self.assertEqual(self.engine.loads, [])
        self.assertTrue(self.engine.paused)
        # Queue canonical settlement suffices: player did not invent prepare-ready.
        self.assertFalse(any(m['action'] == 'playback.context.prepared'
                             for m in self.wire._socket.sent))
        await controller.request('player.play', {
            'playbackContextId': context_id,
            'baseControlVersion': canonical['controlVersion']})
        await self.until(lambda: len(self.engine.loads) == 1 and not self.engine.paused)
        await self.until(lambda: self.player.applied == self.player.context['controlVersion'])
        self.assertEqual(self.player.executed['positionMs'], 700)
        self.assertIsNone(self.player.error)

    async def test_app_prepare_without_initial_queue_reports_failed(self):
        controller = await self.app_controller()
        prepared = controller.expect_event('playback.context.prepared', lambda m: (
            m['payload'].get('intentId') == 'empty-app-intent'))
        await controller.request('playback.context.prepare', {
            'playbackContextId': self.player.context['playbackContextId'],
            'intentId': 'empty-app-intent',
            'baseControlVersion': self.player.context['controlVersion']})
        canonical = (await prepared)['payload']
        self.assertFalse(canonical['ready'])
        self.assertEqual(self.engine.loads, [])
        self.assertTrue(self.engine.paused)
        sent = [m for m in self.wire._socket.sent if m['action'] == 'playback.context.prepared']
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]['payload']['ready'])

    async def test_app_invalid_duplicate_initial_queue_is_rejected(self):
        controller = await self.app_controller()
        with self.assertRaises(ProtocolError):
            await controller.request('playback.context.prepare', {
                'playbackContextId': self.player.context['playbackContextId'],
                'intentId': 'invalid-app-intent',
                'baseControlVersion': self.player.context['controlVersion'],
                'initialQueueSongIds': ['same', 'same'], 'currentIndex': 0,
                'positionMs': 0})
        self.assertEqual(self.engine.loads, [])
        self.assertEqual(self.player.context['queueSongIds'], [])

    async def test_engine_failure_reports_canonical_failed_not_committed(self):
        controller = await self.app_controller()
        failed = controller.expect_event('playback.update', lambda m: (
            m['payload'].get('origin') == 'remoteCommand' and
            m['payload'].get('executionStatus') == 'failed'))
        original_play = self.engine.play
        async def fail_play():
            raise RuntimeError('synthetic audio failure')
        self.engine.play = fail_play
        await self.player.cli('play', {'playlist': 'list'})
        canonical = (await failed)['payload']
        self.assertEqual(canonical['executionStatus'], 'failed')
        self.assertEqual(canonical['appliedControlVersion'], canonical['controlVersion'])
        await self.until(lambda: self.player.error is not None)
        self.assertTrue(self.engine.paused)
        self.assertIsNone(self.player.run_id)
        self.engine.play = original_play
        await self.player.cli('resume', {})
        await self.until(lambda: not self.engine.paused and self.player.error is None)
