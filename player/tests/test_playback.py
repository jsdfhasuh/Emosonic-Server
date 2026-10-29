"""State machine tests using fake protocol and engine, not physical acceptance."""
import asyncio
import copy
import unittest

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
            await self.deliver(action, canonical)

    async def send_confirmed(self, action, payload, confirmation_action, predicate, **kwargs):
        future = self.expect_event(confirmation_action, predicate)
        await self.emit(action, payload)
        return await asyncio.wait_for(future, 1)

    async def request(self, action, payload, **kwargs):
        self.requests.append((action, copy.deepcopy(payload)))
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
