"""Server integration, plus opt-in loopback Socket.IO and real mpv coverage."""
import sys

if __name__ == '__main__' and sys.argv[1:2] == ['--serve-queue-fixture']:
    from gevent import monkey
    monkey.patch_all()

import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit
import wave

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


def serve_queue_fixture(manifest):
    """Start a disposable loopback server for validating the two source revisions together."""
    import secrets
    from gevent.pywsgi import WSGIServer
    from geventwebsocket.handler import WebSocketHandler
    from supysonic.db import Album, Artist, Folder, Track

    root = manifest.parent
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as work:
        config = TestConfig(False, True)
        config.TESTING = False
        config.BASE['database_uri'] = 'sqlite:///' + str(Path(work) / 'music.sqlite')
        config.WEBAPP.update(cache_dir=work, mount_emosonic=True, emo_development_mode=True,
                             emo_strict_v2_allow_local_test_evidence=True, emo_strict_v2_core_enabled=True,
                             emo_strict_v2_follow_enabled=True, emo_strict_v2_handoff_enabled=True,
                             emo_strict_v2_broadcast_enabled=True)
        app = create_application(config)
        username, password = 'queue-test-user', secrets.token_urlsafe(20)
        UserManager.add(username, password)
        folder = Folder.create(name='Fixture', path=work, root=True)
        artist = Artist.create(name='Fixture')
        album = Album.create(name='Fixture', artist=artist)
        ids = []
        for number, title in enumerate(('Original', 'New first', 'New selection'), 1):
            path = Path(work) / f'{number}.wav'
            with wave.open(str(path), 'wb') as wav:
                wav.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0\0' * (8000 * 60))
            track = Track.create(title=title, number=number, disc=1, artist=artist, album=album,
                                 path=str(path), root_folder=folder, folder=folder,
                                 duration=60, bitrate=128, last_modification=0)
            ids.append(str(track.id))
        server = WSGIServer(('127.0.0.1', 0), app, handler_class=WebSocketHandler, log=None)
        server.init_socket()
        fixture = {'port': server.server_port, 'username': username, 'password': password, 'songs': ids}
        temporary = manifest.with_suffix('.tmp')
        with temporary.open('x', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(fixture, stream)
        temporary.replace(manifest)
        try:
            server.serve_forever()
        finally:
            manifest.unlink(missing_ok=True)
            server.stop()
            release_database()


@unittest.skipUnless(os.environ.get('EMOSONIC_QUEUE_FIXTURE'), 'requires the disposable cross-branch server')
class LiveRemoteQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_replace_nonempty_queue_then_pause_from_independent_controller(self):
        from emosonic_player.mpv import MpvPlayer
        from emosonic_player.stream import MediaClient

        fixture = json.loads(Path(os.environ['EMOSONIC_QUEUE_FIXTURE']).read_text())
        url = f"http://127.0.0.1:{fixture['port']}"
        credentials = (url, fixture['username'], fixture['password'])
        original, first, selected = fixture['songs']
        media = MediaClient(*credentials)
        self.addAsyncCleanup(media.close)
        runtime = tempfile.TemporaryDirectory()
        self.addCleanup(runtime.cleanup)
        engine = MpvPlayer('null', runtime.name, allow_null_output_for_tests=True)
        await engine.start()
        self.addAsyncCleanup(engine.close)
        speaker, desktop, remote = [
            ProtocolClient(*credentials, name, name + '-session', name)
            for name in ('speaker', 'desktop', 'independent')
        ]
        player = Player(speaker, engine, media)
        speaker.on_event = player.on_event
        proofs = []

        async def observe(message):
            payload = message.get('payload', {})
            if message.get('action') == 'playback.update' and payload.get('executionStatus') == 'committed':
                proofs.append(payload)

        remote.on_event = observe
        for client in (speaker, desktop, remote):
            self.addAsyncCleanup(client.close)
            await client.connect()
        await player.start()
        self.addAsyncCleanup(player.close)
        await player.reconcile()
        context_id = player.context['playbackContextId']
        desktop_own = (await desktop.request('playback.context.ensure', {
            'deviceSessionId': desktop.device_session_id, 'queueSongIds': [],
            'positionMs': 0, 'state': 'idle'}, reply_actions={'playback.context.ensure'}))['payload']
        self.assertNotEqual(context_id, desktop_own['playbackContextId'])
        for client in (desktop, remote):
            await client.request('playback.context.subscribe', {'playbackContextId': context_id}, type='state')

        async def status(client, target=context_id):
            reply = await client.request('playback.context.status', {'playbackContextId': target},
                                         type='state', reply_actions={'playback.context.status'})
            return reply['payload']

        async def committed(queue, index, wanted_state):
            async with asyncio.timeout(15):
                while not any(
                    item.get('playbackContextId') == context_id and item.get('trackId') == queue[index]
                    and item.get('state') == wanted_state and item.get('appliedControlVersion') == player.applied
                    for item in proofs
                ):
                    await asyncio.sleep(.05)
            snapshot = await status(remote)
            context = snapshot['playbackContext']
            self.assertEqual(context['queueSongIds'], queue)
            self.assertEqual(context['currentIndex'], index)
            device = next(item for item in snapshot['deviceStates'] if item['clientId'] == speaker.client_id)
            peer = await status(desktop)
            peer_device = next(item for item in peer['deviceStates'] if item['clientId'] == speaker.client_id)
            for observed in (peer_device, device):
                self.assertEqual(observed['state'], wanted_state)
                self.assertEqual(observed['trackId'], queue[index])
                self.assertEqual(observed['appliedControlVersion'], context['controlVersion'])
            for authority in (peer['playbackContext'], player.context, player.executed):
                for key in ('playbackContextId', 'queueSongIds', 'currentIndex', 'queueRevision', 'controlVersion'):
                    self.assertEqual(authority[key], context[key], key)
            self.assertIsNone(player.error)
            info = await engine.snapshot()
            self.assertEqual(info['paused'], wanted_state == 'paused')
            self.assertEqual(await engine._command(['get_property', 'current-ao']), 'null')
            # Inspect just the track ID. The authenticated stream URL stays private.
            uri = urlsplit(await engine._command(['get_property', 'path']))
            self.assertEqual(uri.path, '/rest/stream.view')
            self.assertEqual(parse_qs(uri.query)['id'], [queue[index]])
            print(f"canonical queueRevision={context['queueRevision']} controlVersion={context['controlVersion']} "
                  f"index={index} item={queue[index]} state={wanted_state} mpvPaused={info['paused']}", flush=True)
            return context

        await player.cli('play', {'song': original})
        before = await committed([original], 0, 'playing')
        replacement = [first, selected]
        self.assertNotIn(selected, before['queueSongIds'])
        await desktop.request('queue.playItem', {
            'playbackContextId': context_id, 'baseQueueRevision': before['queueRevision'],
            'baseControlVersion': before['controlVersion'], 'queueSongIds': replacement, 'queueIndex': 1,
        })
        playing = await committed(replacement, 1, 'playing')
        self.assertEqual(playing['queueRevision'], before['queueRevision'] + 1)
        self.assertEqual(playing['controlVersion'], before['controlVersion'] + 1)
        await remote.request('player.pause', {
            'playbackContextId': context_id, 'baseControlVersion': playing['controlVersion'],
        })
        paused = await committed(replacement, 1, 'paused')
        self.assertEqual(paused['queueRevision'], playing['queueRevision'])
        self.assertEqual(paused['controlVersion'], playing['controlVersion'] + 1)
        own = (await status(desktop, desktop_own['playbackContextId']))['playbackContext']
        self.assertEqual(own['queueSongIds'], [])
        self.assertEqual(own['queueRevision'], desktop_own['queueRevision'])

        # Next on the final track leaves the queue selected and stops the decoder.
        await remote.request('player.next', {
            'playbackContextId': context_id, 'baseControlVersion': paused['controlVersion'],
        })
        async with asyncio.timeout(15):
            while player.applied != paused['controlVersion'] + 1:
                await asyncio.sleep(.05)
        stopped = (await status(remote))['playbackContext']
        self.assertEqual(stopped['state'], 'stopped')
        self.assertTrue(await engine._command(['get_property', 'idle-active']))

        await player.disconnected()
        await speaker.close()
        await speaker.connect()
        await player.reconcile()
        restored = await status(remote)
        self.assertEqual(restored['playbackContext'], stopped)
        actual = next(item for item in restored['deviceStates'] if item['clientId'] == speaker.client_id)
        self.assertEqual((actual['state'], actual['trackId'], actual['positionMs'],
                          actual['appliedControlVersion']),
                         ('stopped', selected, stopped['positionMs'], stopped['controlVersion']))
        self.assertIsNone(player.run_id)
        self.assertTrue(await engine._command(['get_property', 'idle-active']))
        proofs.clear()
        await remote.request('player.play', {
            'playbackContextId': context_id, 'baseControlVersion': stopped['controlVersion'],
        })
        resumed = await committed(replacement, 1, 'playing')
        self.assertEqual(resumed['queueRevision'], stopped['queueRevision'])
        self.assertEqual(resumed['controlVersion'], stopped['controlVersion'] + 1)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--serve-queue-fixture']:
        serve_queue_fixture(Path(sys.argv[2]).resolve())
    else:
        unittest.main()
