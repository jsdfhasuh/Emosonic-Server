"""State machine tests using fake protocol and engine, not physical acceptance."""
import asyncio
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from emosonic_player.mpv import MpvError, PlaybackEvent
from emosonic_player.playback import Player, PlaybackError


def context():
    return {'playbackContextId': 'ctx', 'authorityClientId': 'client',
            'authorityDeviceSessionId': 'session', 'epoch': 1, 'version': 1,
            'controlVersion': 0, 'queueRevision': 1, 'queueSongIds': ['first', 'second', 'last'],
            'currentIndex': 0, 'positionMs': 0, 'state': 'paused'}


class FakeEngine:
    def __init__(self):
        self.events = asyncio.Queue()
        self.run = None
        self.loads = []
        self.closed = False
        self.fail_play = False
        self.sample = {'run_id': None, 'paused': True, 'buffering': False,
                       'position_ms': 100, 'duration_ms': 10000, 'volume': 50}

    async def load(self, url, run, position_ms=0):
        if self.closed:
            raise MpvError('mpv_not_connected')
        self.run = run
        self.loads.append((url, run))
        self.sample.update(run_id=run, paused=True, position_ms=position_ms)

    async def play(self):
        if self.fail_play:
            self.closed = True
            raise MpvError('mpv_command_interrupted')
        self.sample['paused'] = False

    async def pause(self):
        self.sample['paused'] = True

    async def seek(self, position):
        self.sample['position_ms'] = position

    async def set_volume(self, volume):
        self.sample['volume'] = volume

    async def stop(self):
        if self.closed:
            raise MpvError('mpv_not_connected')
        self.run = None
        self.sample.update(run_id=None, paused=True, position_ms=None)

    async def snapshot(self):
        if self.closed:
            raise MpvError('mpv_not_connected')
        return dict(self.sample)

    async def close(self):
        self.closed = True

    async def start(self):
        self.closed = False


class FakeMedia:
    def stream_url(self, song):
        return 'https://music.test/' + song


class FakeWire:
    client_id = 'client'
    device_session_id = 'session'
    generation = 1
    ready = True

    def __init__(self):
        self.player = None
        self.waiters = []
        self.emitted = []
        self.requests = []
        self.auto_confirm = True
        self.canonical = context()

    def server_time_ms(self):
        return 1234567890

    def expect_event(self, action, predicate, **kwargs):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((action, predicate, future))
        return future

    async def deliver(self, action, payload):
        message = {'type': 'event', 'action': action, 'payload': copy.deepcopy(payload)}
        if action == "playback.update":
            self.canonical = copy.deepcopy(self.player.executed)
            self.canonical.update(controlVersion=payload["appliedControlVersion"],
                                  state=payload["state"], positionMs=payload["positionMs"])
        await self.player.on_event(message)
        for expected, predicate, future in self.waiters:
            if not future.done() and expected == action and predicate(message):
                future.set_result(message)
        return message

    async def emit(self, action, payload, **kwargs):
        if action == "queue.context.sync":
            await self.request(action, payload)
            return
        self.emitted.append((action, copy.deepcopy(payload)))
        if self.auto_confirm:
            canonical = dict(payload, controlVersion=payload.get('appliedControlVersion', 0))
            if action == 'device.volume.update':
                canonical['sourceClientId'] = self.client_id
            await self.deliver(action, canonical)

    async def send_confirmed(self, action, payload, confirmation_action, predicate, **kwargs):
        future = self.expect_event(confirmation_action, predicate)
        await self.emit(action, payload)
        return await asyncio.wait_for(future, 1)

    async def request(self, action, payload, **kwargs):
        self.requests.append((action, copy.deepcopy(payload)))
        if action == 'playback.context.ensure':
            return {'payload': copy.deepcopy(self.canonical)}
        if action == 'playback.context.status':
            return {'payload': {'playbackContext': copy.deepcopy(self.canonical)}}
        if action == 'queue.context.sync':
            canonical = copy.deepcopy(self.player.context)
            canonical.update(queueSongIds=payload['queueSongIds'], currentIndex=payload['currentIndex'],
                             queueRevision=payload['baseQueueRevision'] + 1,
                             controlVersion=payload['baseControlVersion'] + 1,
                             version=canonical['version'] + 1, positionMs=0)
            self.canonical = copy.deepcopy(canonical)
            await self.deliver(action, canonical)
        return {'payload': {}}


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_pause_waits_and_uses_group_position(self):
        clock = self.wire.server_time_ms()
        loop = asyncio.get_running_loop()
        start = loop.time()
        self.wire.server_time_ms = lambda: clock + int((loop.time() - start) * 1000)
        await self.engine.play()
        due = clock + 65
        task = asyncio.create_task(self.player._execute(self.command(
            'player.pause', effectiveAtServerMs=due, serverTimeMs=due-300, positionMs=350), 1))
        self.tasks.append(task)
        await asyncio.sleep(.01)
        self.assertFalse(self.engine.sample['paused'])
        await task
        self.assertTrue(self.engine.sample['paused'])
        self.assertEqual(self.engine.sample['position_ms'], 350)
        self.assertGreaterEqual(self.wire.server_time_ms(), due)

    async def test_expired_group_play_reports_effective_at_missed(self):
        due = self.wire.server_time_ms() - 1100
        with self.assertRaisesRegex(PlaybackError, 'deadline'):
            await self.player._execute(self.command(
                'player.play', effectiveAtServerMs=due, serverTimeMs=due-300, positionMs=240), 1)
        failed = [p for action, p in self.wire.emitted if action == 'playback.update']
        self.assertEqual(failed[-1]['errorCode'], 'effective_at_missed')
        self.assertEqual(self.player.applied, 0)

    async def asyncSetUp(self):
        self.wire, self.engine = FakeWire(), FakeEngine()
        self.player = Player(self.wire, self.engine, FakeMedia())
        self.wire.player = self.player
        self.player.context = context()
        self.player.executed = context()
        await self.player._load(context(), paused=True)
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def test_confirmed_device_volume_survives_engine_and_daemon_restarts(self):
        with tempfile.TemporaryDirectory() as state:
            player = Player(self.wire, self.engine, FakeMedia(), state)
            await player.start_engine(10)
            self.assertEqual(self.engine.sample['volume'], 10)
            await player.cli('volume', {'value': 47})
            self.assertFalse((Path(state) / 'volume.json').exists())
            self.assertEqual(self.wire.requests[-1][0], 'device.setVolume')
            await player._execute(self.command('device.setVolume', volume=47), 1)
            await self.engine.close()
            await player.start_engine(10)
            self.assertEqual(self.engine.sample['volume'], 47)

            restarted = Player(self.wire, FakeEngine(), FakeMedia(), state)
            await restarted.start_engine(10)
            self.assertEqual(restarted.engine.sample['volume'], 47)
            await restarted._execute(self.command('device.setVolume', volume=0), 1)
            muted = Player(self.wire, FakeEngine(), FakeMedia(), state)
            await muted.start_engine(20)
            self.assertEqual(muted.engine.sample['volume'], 0)

    async def test_failed_readback_leaves_saved_volume_and_disk_failure_retries(self):
        with tempfile.TemporaryDirectory() as state:
            path = Path(state) / 'volume.json'
            path.write_text('36\n')
            player = Player(self.wire, self.engine, FakeMedia(), state)
            await player.start_engine(10)
            with patch.object(self.engine, 'set_volume', new_callable=AsyncMock):
                with self.assertRaisesRegex(PlaybackError, 'confirmation mismatch'):
                    await player._execute(self.command('device.setVolume', volume=47), 1)
            self.assertEqual(path.read_text(), '36\n')

            with patch('emosonic_player.volume.os.replace', side_effect=OSError('disk full')):
                with self.assertLogs('emosonic_player.volume', 'WARNING'):
                    await player._execute(self.command('device.setVolume', volume=0), 1)
            self.assertEqual(path.read_text(), '36\n')
            await player.start_engine(10)
            self.assertEqual(self.engine.sample['volume'], 0)
            await player._execute(self.command('device.setVolume', volume=0), 1)
            self.assertEqual(path.read_text(), '0\n')

    async def test_invalid_volume_file_uses_configured_default(self):
        with tempfile.TemporaryDirectory() as state:
            for invalid in ('broken', 'true', '[]', '101', '-1'):
                with self.subTest(invalid=invalid):
                    (Path(state) / 'volume.json').write_text(invalid)
                    with self.assertLogs('emosonic_player.volume', 'WARNING'):
                        player = Player(self.wire, self.engine, FakeMedia(), state)
                    await player.start_engine(25)
                    self.assertEqual(self.engine.sample['volume'], 25)

    async def test_same_session_reconnect_preserves_stopped_queue_until_play(self):
        self.wire.canonical.update(state='stopped', currentIndex=1, positionMs=420,
                                   queueRevision=4, controlVersion=15)
        stopped = copy.deepcopy(self.wire.canonical)
        await self.player.disconnected()
        self.wire.generation += 1
        self.engine.loads.clear()
        await self.player.reconcile()

        self.assertIsNone(self.player.run_id)
        self.assertIsNone(self.engine.run)
        self.assertEqual(self.engine.loads, [])
        self.assertEqual(self.player.context, stopped)
        self.assertEqual(self.player.executed, stopped)
        self.assertEqual(self.player.applied, 15)
        [(action, fact)] = self.wire.emitted
        self.assertEqual(action, 'playback.update')
        self.assertEqual((fact['state'], fact['positionMs'], fact['trackId'],
                          fact['appliedControlVersion'], fact['origin']),
                         ('stopped', 420, 'second', 15, 'passive'))

        await self.player._execute(self.command('player.play', version=16), self.wire.generation)
        self.assertEqual(self.engine.loads, [('https://music.test/second', self.engine.run)])
        self.assertFalse(self.engine.sample['paused'])
        self.assertEqual(self.engine.sample['position_ms'], 420)
        self.assertEqual(self.player.executed['queueRevision'], 4)
        self.assertEqual(self.player.applied, 16)

    async def test_reconnect_other_queue_states_do_not_start_audio(self):
        self.wire.negotiated = {'effectiveAtPlayback': True}
        for state in ('paused', 'playing', 'idle'):
            with self.subTest(state=state):
                self.wire.canonical = context()
                self.wire.canonical.update(state=state, controlVersion=2)
                idle = state == 'idle'
                if idle:
                    self.wire.canonical['queueSongIds'] = []
                    self.wire.canonical.pop('currentIndex')
                self.engine.loads.clear()
                self.wire.emitted.clear()
                await self.player.reconcile()
                self.assertEqual(len(self.engine.loads), 0 if idle else 1)
                self.assertTrue(self.engine.sample['paused'])
                self.assertEqual(self.player.run_id is None, idle)
                [(action, fact)] = self.wire.emitted
                self.assertEqual(action, 'playback.update')
                self.assertEqual(fact['state'], 'idle' if idle else 'paused')
                self.assertEqual(fact['appliedControlVersion'], 2)

    async def test_failed_idle_confirmation_restarts_engine_without_stopped_fact(self):
        self.wire.canonical.update(state='stopped', controlVersion=15)
        with patch.object(self.engine, 'stop', side_effect=MpvError('idle confirmation failed')):
            with self.assertRaisesRegex(MpvError, 'idle confirmation failed'):
                await self.player.reconcile()
        self.assertIsNone(self.player.run_id)
        self.assertTrue(self.engine.closed)
        self.assertTrue(self.player.needs_engine_restart)
        self.assertFalse(self.wire.requests)
        self.assertFalse(self.wire.emitted)

    def command(self, action='player.play', version=1, **extra):
        return {'type': 'command', 'action': action, 'payload': {
            'playbackContextId': 'ctx', 'controlVersion': version,
            'dependsOnControlVersion': version - 1, 'executionTimeoutMs': 500,
            'sourceClientId': 'controller', **extra}}

    async def wait_for(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(.005)
        self.fail('condition did not settle')

    def start_events(self):
        self.tasks.append(asyncio.create_task(self.player._engine_events()))

    async def start_announcement(self):
        self.start_events()
        request = asyncio.create_task(self.player.announce('/tmp/speech.wav', 5))
        self.tasks.append(request)
        await self.wait_for(lambda: self.engine.loads[-1][0] == '/tmp/speech.wav' and not self.engine.sample['paused'])
        return request, self.engine.run

    async def test_announcement_restores_exact_music_cursor_and_never_advances_queue(self):
        self.engine.sample.update(paused=False, position_ms=3456)
        old_queue = copy.deepcopy(self.player.context)
        request, speech = await self.start_announcement()
        status = await self.player.cli('status', {})
        self.assertTrue(status['announcing'])
        self.assertEqual(status['audio']['position_ms'], 3456)
        await self.engine.events.put(PlaybackEvent('eof', speech, 1))
        self.assertEqual(await request, {'completed': True})
        self.assertEqual(self.engine.loads[-1][0], 'https://music.test/first')
        self.assertEqual(self.engine.sample['position_ms'], 3456)
        self.assertFalse(self.engine.sample['paused'])
        await self.engine.events.put(PlaybackEvent('eof', speech, 1))
        await asyncio.sleep(0)
        self.assertEqual(old_queue, self.player.context)
        self.assertEqual(self.wire.requests, [])

    async def test_announcement_failure_restores_original_pause(self):
        self.engine.sample.update(paused=True, position_ms=987)
        request, speech = await self.start_announcement()
        await self.player._execute(self.command('device.setVolume', volume=0), 1)
        await self.engine.events.put(PlaybackEvent('error', speech, 1))
        with self.assertRaisesRegex(PlaybackError, 'announcement playback failed'):
            await request
        self.assertEqual(self.engine.sample['volume'], 0)
        await self.player.start_engine(10)
        self.assertEqual(self.engine.sample['volume'], 0)
        self.assertEqual(self.engine.loads[-1][0], 'https://music.test/first')
        self.assertEqual(self.engine.sample['position_ms'], 987)
        self.assertTrue(self.engine.sample['paused'])

    async def test_incoming_pause_preempts_announcement_with_frozen_music_position(self):
        self.engine.sample.update(paused=False, position_ms=1234)
        request, speech = await self.start_announcement()
        await self.player._execute(self.command('player.pause'), 1)
        self.assertEqual(await request, {'completed': False, 'reason': 'interrupted'})
        await self.engine.events.put(PlaybackEvent('eof', speech, 1))
        await asyncio.sleep(0)
        self.assertTrue(self.engine.sample['paused'])
        self.assertEqual(self.player.executed['positionMs'], 1234)
        self.assertEqual(self.player.executed['currentIndex'], 0)
        self.assertEqual(self.player.applied, 1)

    async def test_idle_announcement_and_away_pause(self):
        self.player.run_id = None
        self.player.context = self.player.executed = dict(context(), queueSongIds=[], state='idle')
        await self.engine.stop()
        request, speech = await self.start_announcement()
        self.assertEqual(await self.player.cli('pause', {}), {'accepted': True})
        self.assertFalse((await request)['completed'])
        await self.engine.events.put(PlaybackEvent('eof', speech, 1))
        await asyncio.sleep(0)
        self.assertIsNone(self.player.run_id)
        self.assertIsNone(self.engine.run)
        self.assertEqual(self.wire.requests, [])

    async def test_away_pause_during_file_preparation_prevents_late_speech(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def prepare(*_args):
            entered.set()
            await release.wait()
            return 1
        self.player.context = self.player.executed = dict(context(), queueSongIds=[], state='idle')
        self.player.run_id = None
        with patch('emosonic_player.playback.asyncio.to_thread', prepare):
            request = asyncio.create_task(self.player.announce_file('/ha/audio', 'test.wav'))
            await entered.wait()
            await self.player.cli('pause', {})
            release.set()
            self.assertEqual(await request, {'completed': False, 'reason': 'interrupted'})
        self.assertEqual(len(self.engine.loads), 1)

    async def test_new_track_preempts_announcement_and_late_eof(self):
        request, speech = await self.start_announcement()
        await self.player._execute(self.command('player.next'), 1)
        self.assertFalse((await request)['completed'])
        await self.engine.events.put(PlaybackEvent('eof', speech, 1))
        await asyncio.sleep(0)
        self.assertEqual(self.engine.loads[-1][0], 'https://music.test/second')
        self.assertEqual(self.player.executed['currentIndex'], 1)
        self.assertFalse(self.engine.sample['paused'])

    async def test_ha_status_caches_only_the_matching_track(self):
        self.player.media.get = AsyncMock(return_value={'song': {'id': 'first', 'title': 'One', 'path': 'private'}})
        self.assertEqual((await self.player.ha_status())['track'], {'id': 'first', 'title': 'One'})
        await self.player.ha_status()
        self.player.media.get.assert_awaited_once_with('getSong', id='first')
        self.player.context['currentIndex'] = 1
        self.assertIsNone((await self.player.ha_status())['track'])

    async def test_idle_engine_disconnect_requests_restart(self):
        self.start_events()
        self.player.run_id = None
        self.engine.process_generation = 7
        from emosonic_player.mpv import PlaybackEvent
        await self.engine.events.put(PlaybackEvent('disconnected', None, 6))
        await asyncio.sleep(.02)
        self.assertFalse(self.player.needs_engine_restart)
        await self.engine.events.put(PlaybackEvent('disconnected', None, 7))
        await asyncio.sleep(.02)
        self.assertTrue(self.player.needs_engine_restart)

    async def test_committed_pause_reuses_same_run_and_duplicate_command_is_noop(self):
        run = self.player.run_id
        await self.player._execute(self.command('player.pause'), 1)
        self.assertEqual(self.player.applied, 1)
        self.assertEqual(self.player.run_id, run)
        count = len(self.wire.emitted)
        await self.player._execute(self.command('player.pause'), 1)
        self.assertEqual(len(self.wire.emitted), count)
        self.assertEqual(len(self.engine.loads), 1)

    async def test_canonical_required_before_applied_cursor(self):
        self.wire.auto_confirm = False
        task = asyncio.create_task(self.player._execute(self.command('player.pause'), 1))
        self.tasks.append(task)
        await self.wait_for(lambda: bool(self.wire.emitted))
        self.assertEqual(self.player.applied, 0)
        action, payload = self.wire.emitted[-1]
        await self.wire.deliver(action, dict(payload, controlVersion=1))
        await task
        self.assertEqual(self.player.applied, 1)

    async def test_missing_dependency_does_not_execute_audio(self):
        with self.assertRaises(PlaybackError):
            await self.player._execute(self.command(version=2), 1)
        self.assertTrue(self.engine.sample['paused'])
        self.assertEqual(self.player.applied, 0)

    async def test_late_eof_after_next_does_not_double_advance(self):
        old_run = self.player.run_id
        await self.player._execute(self.command('player.next'), 1)
        self.start_events()
        await self.engine.events.put(PlaybackEvent('eof', old_run, 1))
        await asyncio.sleep(.02)
        self.assertEqual(self.player.executed['currentIndex'], 1)
        self.assertEqual(len(self.engine.loads), 2)

    async def test_natural_eof_advances_once_with_new_run_identity(self):
        old_run = self.player.run_id
        self.start_events()
        await self.engine.events.put(PlaybackEvent('eof', old_run, 1))
        await self.wait_for(lambda: len(self.engine.loads) == 2)
        self.assertEqual(self.player.executed['currentIndex'], 1)
        self.assertNotEqual(self.player.run_id, old_run)
        await self.engine.events.put(PlaybackEvent('eof', old_run, 1))
        await asyncio.sleep(.02)
        self.assertEqual(len(self.engine.loads), 2)

    async def test_canonical_continuation_preserves_future_eof(self):
        run = self.player.run_id
        await self.player._execute(self.command('player.play'), 1)
        self.start_events()
        await self.engine.events.put(PlaybackEvent('eof', run, 1))
        await self.wait_for(lambda: len(self.engine.loads) == 2)
        self.assertEqual(self.player.executed['currentIndex'], 1)

    async def test_terminal_eof_reports_stopped_without_loading(self):
        self.player.context['currentIndex'] = 2
        self.player.executed['currentIndex'] = 2
        self.start_events()
        await self.engine.events.put(PlaybackEvent('eof', self.player.run_id, 1))
        await self.wait_for(lambda: bool(self.wire.emitted))
        self.assertEqual(self.wire.emitted[-1][1]['state'], 'stopped')
        self.assertEqual(len(self.engine.loads), 1)

    async def test_closed_engine_failure_still_reports_failed_execution(self):
        self.engine.fail_play = True
        with self.assertRaises(Exception):
            await self.player._execute(self.command(), 1)
        failed = [p for a, p in self.wire.emitted if p.get('executionStatus') == 'failed']
        self.assertEqual(len(failed), 1)
        self.assertEqual(self.player.applied, 0)

    async def test_disconnect_after_engine_failure_does_not_raise(self):
        self.engine.closed = True
        await self.player.disconnected()
        self.assertIsNone(self.player.run_id)
        self.assertEqual(self.player.error, 'server_disconnected')

    async def test_authority_change_stops_and_never_advances(self):
        old_run = self.player.run_id
        other = dict(context(), authorityClientId='someone-else', version=2)
        await self.player.on_event({'action': 'playback.context.status', 'payload': other})
        self.start_events()
        await self.engine.events.put(PlaybackEvent('eof', old_run, 1))
        await asyncio.sleep(.02)
        self.assertIsNone(self.player.run_id)
        self.assertEqual(len(self.engine.loads), 1)
        with self.assertRaises(PlaybackError):
            await self.player._execute(self.command(), 1)

    async def test_stale_epoch_settlement_does_not_stop_current_run(self):
        self.player.context['epoch'] = 2
        self.player.executed['epoch'] = 2
        run = self.player.run_id
        await self.player.on_event({'action': 'playback.control.settled', 'payload': {
            'playbackContextId': 'ctx', 'epoch': 1, 'commandControlVersion': 1}})
        self.assertEqual(self.player.run_id, run)
        self.assertEqual(self.engine.run, run)

    async def test_context_closed_stops_audio(self):
        await self.player.on_event({'action': 'playback.context.closed',
                                    'payload': {'playbackContextId': 'ctx'}})
        self.assertIsNone(self.player.run_id)
        self.assertIsNone(self.engine.run)
        self.assertIsNone(self.player.executed)
        with self.assertRaises(PlaybackError):
            await self.player._execute(self.command(), 1)

    async def test_engine_error_never_advances_queue(self):
        self.start_events()
        await self.engine.events.put(PlaybackEvent('error', self.player.run_id, 1))
        await self.wait_for(lambda: self.player.error == 'audio_unavailable')
        self.assertEqual(len(self.engine.loads), 1)
        self.assertFalse(any(action == 'queue.context.sync' for action, _ in self.wire.requests))
