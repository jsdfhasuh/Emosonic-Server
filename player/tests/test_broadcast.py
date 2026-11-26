"""Broadcast delivery ordering, recovery and feedback with isolated player state."""
import asyncio
import copy
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from emosonic_player.broadcast import Broadcast
from emosonic_player.mpv import MpvPlayer
from emosonic_player.playback import Player
from test_mpv import FakeMpv, MemoryWriter
from test_playback import FakeEngine, FakeMedia, FakeWire, context


class Wire(FakeWire):
    def server_time_ms(self):
        return round(time.monotonic() * 1000)

    async def send_confirmed(self, action, payload, confirmation_action, predicate, **kw):
        self.emitted.append(copy.deepcopy(payload))
        result = {'payload': {**payload, 'sourceClientId': self.client_id}}
        assert predicate(result)
        return result

    async def request(self, action, payload, **kwargs):
        if action == 'playback.context.status':
            return {'payload': {'playbackContext': copy.deepcopy(self.canonical), 'deviceStates': []}}
        if action == 'broadcast.status':
            return {'payload': {'participantStates': [], 'broadcast': {}}}
        raise AssertionError(action)


class Engine(FakeEngine):
    async def set_speed(self, rate):
        self.sample['speed'] = rate


class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.wire, self.engine = Wire(), Engine()
        self.player = Player(self.wire, self.engine, FakeMedia())
        self.b = Broadcast(self.player, self.temp.name)
        self.player.executed = self.player.context = context()
        self.player.applied = 0
        await self.player._load(self.player.context, paused=True)
        self.engine.sample.update(position_ms=980, speed=1.0)

    def event(self, action, revision, *, source=False, lead=0, state='playing', **fields):
        now = self.wire.server_time_ms()
        p = {'broadcastId': 'group', 'playbackContextId': 'source', 'intentId': 'intent',
             'authorityClientId': self.wire.client_id if source else 'remote',
             'authorityDeviceSessionId': self.wire.device_session_id if source else 'other-session',
             'ownerClientId': 'remote', 'participants': [] if source else [self.wire.client_id],
             'queueSongIds': ['mirror'], 'currentIndex': 0, 'trackId': 'mirror', 'state': state,
             'positionMs': 500, 'playbackRate': 1.25, 'sourceControlVersion': 1,
             'sourceQueueRevision': 1, 'sourceEpoch': 1, 'sourceVersion': 1,
             'broadcastRevision': revision, 'serverUpdatedAtMs': now + lead,
             'lifecycleState': 'stopped' if action == 'stop' else 'active', **fields}
        if not source:
            p['deliveryId'] = action + str(revision)
        if action not in ('stop', 'waiting'):
            p.update(effectiveAtServerMs=now + lead, serverTimeMs=now + lead - 300)
        return {'type': 'event', 'action': 'broadcast.' + action, 'payload': p}

    async def test_source_lifecycle_keeps_audio_untouched(self):
        run = self.player.run_id
        await self.b.on_event(self.event('start', 1, source=True))
        self.assertEqual(self.b.status()['role'], 'source')
        await self.b.on_event(self.event('stop', 2, source=True))
        self.assertEqual(self.player.run_id, run)
        self.assertEqual(len(self.engine.loads), 1)
        self.assertEqual(self.wire.emitted, [])

    async def test_progress_closes_measured_drift_and_restores_the_users_rate(self):
        stamp = 100000
        self.wire.server_time_ms = lambda: stamp
        loop = asyncio.get_running_loop()
        self.engine.seek = AsyncMock(wraps=self.engine.seek)
        revision = 0
        for initial, rate in [(-94, 1.), (-49, 1.25), (65, .75)]:
            revision += 1
            await self.b.on_event(self.event('play', revision, playbackRate=rate))
            error = initial
            pulses = []
            self.engine.seek.reset_mock()

            async def pass_time(waiter, seconds):
                nonlocal error
                waiter.close()
                speed = self.engine.sample['speed']
                self.assertLessEqual(abs(speed / rate - 1), .040001)
                self.assertLessEqual(seconds, .45)
                error += (speed - rate) * seconds * 1000
                pulses.append(seconds)
                raise asyncio.TimeoutError

            for i in range(12):
                stamp += 1000
                revision += 1
                target = 5000 + i * 1000 * rate
                # Position is the IPC midpoint, 60 ms earlier than snapshot completion.
                self.engine.sample.update(position_ms=target + error - 60 * rate,
                    position_sampled_monotonic=loop.time() - .060)
                with patch('asyncio.wait_for', pass_time):
                    await self.b.on_event(self.event('progress', revision, positionMs=target, playbackRate=rate))
                self.assertEqual(self.wire.emitted[-1]['appliedBroadcastRevision'], revision)
                self.assertEqual(self.wire.emitted[-1]['playbackRate'], rate)
                self.assertEqual(self.engine.sample['speed'], rate)
                if i == 0:
                    self.assertFalse(pulses)
                if i == 9:
                    count = len(pulses)
            self.assertTrue(pulses)
            self.assertEqual(len(pulses), count)
            self.assertLessEqual(abs(error), 20)
            self.engine.seek.assert_not_awaited()

    async def test_progress_ignores_jitter_and_does_not_exceed_rate_limits(self):
        stamp = 100000
        self.wire.server_time_ms = lambda: stamp
        revision = 0
        for rate, errors in [(1., (55, -55, 55, 12)), (2., (-95, -95)), (.5, (81, 81))]:
            revision += 1
            await self.b.on_event(self.event('play', revision, playbackRate=rate))
            for drift in errors:
                stamp += 1000
                revision += 1
                self.engine.sample.update(position_ms=500 + drift,
                    position_sampled_monotonic=asyncio.get_running_loop().time())
                with patch('asyncio.wait_for', side_effect=AssertionError('unneeded speed change')):
                    await self.b.on_event(self.event('progress', revision, playbackRate=rate))
                self.assertEqual(self.wire.emitted[-1]['executionStatus'], 'applied')
                self.assertEqual(self.engine.sample['speed'], rate)

    async def test_late_progress_leaves_time_to_restore_and_checks_actual_completion(self):
        stamp = 100000
        self.wire.server_time_ms = lambda: stamp
        loop = asyncio.get_running_loop()
        rate = 1.
        await self.b.on_event(self.event('start', 1, playbackRate=rate))
        for rev, extra_delay in [(2, 0), (4, 240)]:
            stamp += 1000
            self.engine.sample.update(position_ms=410, position_sampled_monotonic=loop.time())
            await self.b.on_event(self.event('progress', rev, playbackRate=rate))
            stamp += 1000
            self.engine.sample.update(position_ms=1210, position_sampled_monotonic=loop.time())
            async def pass_time(waiter, duration):
                nonlocal stamp
                waiter.close()
                self.assertEqual(self.engine.sample['speed'], 1.04)
                self.assertAlmostEqual(duration, .1)
                stamp += round(1000 * duration) + extra_delay
                raise asyncio.TimeoutError
            with patch('asyncio.wait_for', pass_time):
                await self.b.on_event(self.event('progress', rev + 1, lead=-800, playbackRate=rate))
            result = self.wire.emitted[-1]
            self.assertEqual(self.engine.sample['speed'], rate)
            if extra_delay:
                self.assertEqual((result['executionStatus'], result['errorCode']), ('failed', 'effective_at_missed'))
            else:
                self.assertEqual(result['appliedBroadcastRevision'], rev + 1)

    async def test_late_final_paused_observation_fails_delivery(self):
        stamp = 100000
        self.wire.server_time_ms = lambda: stamp
        await self.b.on_event(self.event('start', 1, state='paused'))
        original = self.engine.snapshot
        reads = 0
        async def slow_snapshot():
            nonlocal stamp, reads
            reads += 1
            if reads == 2:
                stamp += 1001
            return await original()
        self.engine.snapshot = slow_snapshot
        await self.b.on_event(self.event('pause', 2, state='paused'))
        result = self.wire.emitted[-1]
        self.assertEqual((result['executionStatus'], result['errorCode']), ('failed', 'effective_at_missed'))
        self.assertEqual(result['lastAppliedBroadcastRevision'], 1)

    async def test_terminal_interrupts_progress_slew_before_feedback(self):
        stamp = 100000
        self.wire.server_time_ms = lambda: stamp
        rate = 1.25
        await self.b.on_event(self.event('start', 1, playbackRate=rate))
        self.engine.sample.update(position_ms=410, position_sampled_monotonic=asyncio.get_running_loop().time())
        await self.b.on_event(self.event('progress', 2, playbackRate=rate))
        entered = asyncio.Event()
        set_speed = self.engine.set_speed
        async def observe(speed):
            await set_speed(speed)
            if speed > rate:
                entered.set()
        self.engine.set_speed = observe
        stamp += 1000
        applying = asyncio.create_task(self.b.on_event(self.event('progress', 3, playbackRate=rate)))
        async with asyncio.timeout(1):
            await entered.wait()
            await self.b.on_event(self.event('stop', 4))
            await applying
        self.assertEqual([p['appliedBroadcastRevision'] for p in self.wire.emitted], [1, 2, 4])
        self.assertTrue(self.engine.sample['paused'])
        self.assertEqual(self.engine.sample['speed'], 1.)

    async def test_late_pause_after_eof_keeps_mpv_for_the_next_queue_item(self):
        self.wire.server_time_ms = lambda: 100000
        fake = FakeMpv()
        reader, peer_reader = asyncio.StreamReader(), asyncio.StreamReader()
        writer = MemoryWriter(peer_reader)
        peer = asyncio.create_task(fake.serve(peer_reader, MemoryWriter(reader)))
        engine = MpvPlayer('alsa/test', self.temp.name, timeout=1)
        engine._writer = writer
        engine._generation = 1
        engine._reader_task = asyncio.create_task(engine._read(reader, 1))
        async def close_peer():
            await engine.close()
            await peer
        self.addAsyncCleanup(close_peer)
        self.player.engine = engine
        await engine.load('ordinary', self.player.run_id, 980)

        queue = ['mirror', 'next']
        await self.b.on_event(self.event('start', 119, queueSongIds=queue))
        self.assertEqual(self.wire.emitted[-1]['appliedBroadcastRevision'], 119)
        fake.props.pop('time-pos')
        fake.props['idle-active'] = True
        await fake.event('end-file', playlist_entry_id=fake.entry, reason='eof')
        ended = await asyncio.wait_for(engine.events.get(), 1)
        self.assertEqual((ended.kind, ended.run_id), ('eof', self.b.run))
        last_entry = fake.entry
        await self.b.on_event(self.event('progress', 120, queueSongIds=queue, positionMs=192720))
        self.assertEqual(self.wire.emitted[-1]['executionStatus'], 'failed')
        await self.b.on_event(self.event('pause', 121, state='paused', queueSongIds=queue,
                                         positionMs=192756))
        failed = self.wire.emitted[-1]
        self.assertEqual((failed['deliveryId'], failed['executionStatus'], failed['errorCode']),
                         ('pause121', 'failed', 'execution_failed'))
        self.assertEqual(failed['lastAppliedBroadcastRevision'], 119)
        self.assertFalse(self.player.needs_engine_restart)
        self.assertIs(engine._writer, writer)
        self.assertEqual(fake.entry, last_entry, 'late reports must not reload the ended occurrence')

        fake.props['time-pos'] = 0
        next_item = dict(queueSongIds=queue, currentIndex=1, trackId='next', positionMs=0)
        await self.b.on_event(self.event('queue.sync', 122, state='paused', **next_item))
        await self.b.on_event(self.event('play', 123, **next_item))
        applied = self.wire.emitted[-1]
        self.assertEqual((applied['appliedBroadcastRevision'], applied['trackId'], applied['state']),
                         (123, 'next', 'playing'))
        actual = await engine.snapshot()
        self.assertEqual((actual['run_id'], actual['paused']), (self.b.run, False))
        self.assertNotEqual(actual['run_id'], ended.run_id)
        self.assertIs(engine._writer, writer)

    async def test_participant_recovers_original_on_terminal_even_after_process_restart(self):
        await self.b.on_event(self.event('start', 1))
        self.assertTrue(self.b.blocks_core)
        self.assertEqual(self.wire.emitted[-1]['playbackRate'], 1.25)
        self.assertEqual(self.wire.emitted[-1]['appliedBroadcastRevision'], 1)
        self.assertEqual(self.b.status()['mirrorTrackId'], 'mirror')
        self.assertEqual(self.b.status()['mirrorQueueIndex'], 0)
        self.assertEqual(self.b.status()['mirrorState'], 'playing')
        self.assertFalse(self.engine.sample['paused'])
        self.assertEqual(self.player.executed['queueSongIds'], ['first', 'second', 'last'])
        restarted = Broadcast(self.player, self.temp.name)
        self.assertTrue(restarted.blocks_core)
        terminal = self.event('stop', 2)
        await restarted.on_event(terminal)
        self.assertTrue(self.wire.emitted[-1]['restoreCompleted'])
        self.assertEqual(self.engine.loads[-1][0], 'https://music.test/first')
        self.assertEqual(self.engine.sample['position_ms'], 980)
        self.assertTrue(self.engine.sample['paused'])
        self.assertEqual(self.engine.sample['speed'], 1.0)
        self.assertFalse(restarted.blocks_core)
        self.assertNotIn('mirrorTrackId', restarted.status())
        await restarted.on_event(terminal)
        self.assertEqual(len(self.wire.emitted), 2)

    async def test_frozen_pair_reuses_session_only_for_the_same_server_account_and_client(self):
        self.wire.server_url, self.wire.username = 'https://music.invalid', 'listener'
        self.b = Broadcast(self.player, self.temp.name)
        await self.b.on_event(self.event('start', 1))
        self.assertEqual(Broadcast.recovery_session(self.temp.name, self.wire.server_url,
            self.wire.username, self.wire.client_id), self.wire.device_session_id)
        self.assertIsNone(Broadcast.recovery_session(self.temp.name, self.wire.server_url,
            'another-user', self.wire.client_id))
        self.wire.device_session_id = 'intentionally-replaced-pair'
        replacement = Broadcast(self.player, self.temp.name)
        self.assertFalse(replacement.blocks_core)
        self.assertFalse(await replacement.recover())

    async def test_explicit_play_and_seek_can_reopen_the_same_ended_occurrence(self):
        await self.b.on_event(self.event('start', 1))
        for revision, action in [(2, 'seek'), (3, 'play')]:
            self.engine.eof_run_id = self.b.run
            self.engine.sample.update(run_id=None, position_ms=None)
            await self.b.on_event(self.event(action, revision, positionMs=0))
            self.assertEqual(self.wire.emitted[-1]['appliedBroadcastRevision'], revision)
            self.assertNotEqual(self.b.run, self.engine.eof_run_id)
            self.assertEqual(self.b.run, self.engine.run)

    async def test_next_occurrence_of_same_song_loads_a_new_run_after_eof(self):
        queue = ['mirror', 'mirror']
        await self.b.on_event(self.event('start', 1, queueSongIds=queue))
        ended = self.b.run
        self.engine.eof_run_id = ended
        self.engine.run = None
        self.engine.sample.update(run_id=None, position_ms=None)
        await self.b.on_event(self.event('queue.sync', 2, queueSongIds=queue, currentIndex=1,
                                         state='paused', positionMs=0))
        applied = self.wire.emitted[-1]
        self.assertEqual((applied['executionStatus'], applied.get('queueIndex')), ('applied', 1))
        self.assertNotEqual(self.b.run, ended)
        self.assertEqual(self.b.run, self.engine.run)

    async def test_same_revision_late_delivery_cannot_replace_the_newer_resync_attempt(self):
        first = self.event('start', 1)
        await self.b.on_event(first)
        new = self.event('resync', 1)
        await self.b.on_event(new)
        await self.b.on_event(first)
        self.assertEqual([v['deliveryId'] for v in self.wire.emitted], ['start1', 'resync1'])

    async def test_rejected_feedback_releases_the_next_delivery_immediately(self):
        send = self.wire.send_confirmed
        rejected_sent = asyncio.Event()
        async def reject_first(action, payload, confirmation_action, predicate):
            if payload['deliveryId'] == 'start1':
                message = {'action': 'broadcast.feedback.rejected', 'payload': payload}
                for expected, match, future in self.wire.waiters:
                    if expected == message['action'] and match(message):
                        future.set_result(message)
                        rejected_sent.set()
                await asyncio.Future()
            return await send(action, payload, confirmation_action, predicate)
        self.wire.send_confirmed = reject_first
        async with asyncio.timeout(1):
            await self.b.on_event(self.event('start', 1))
            await rejected_sent.wait()
            await self.b.on_event(self.event('resync', 1))
        self.assertEqual(self.wire.emitted[-1]['deliveryId'], 'resync1')
        self.assertEqual(self.wire.emitted[-1]['clientSeq'], 2)

    async def test_terminal_preempts_future_play_and_late_start_cannot_restart_it(self):
        start = self.event('start', 1, lead=500)
        play = asyncio.create_task(self.b.on_event(start))
        async with asyncio.timeout(1):
            while self.b.run is None:
                await asyncio.sleep(0)
        await self.b.on_event(self.event('stop', 2))
        await play
        await self.b.on_event(start)
        self.assertTrue(self.engine.sample['paused'])
        self.assertEqual(self.engine.loads[-1][0], 'https://music.test/first')
        self.assertEqual([p.get('appliedBroadcastRevision') for p in self.wire.emitted], [2])

    async def test_missed_delivery_never_plays_and_terminal_uses_newer_canonical(self):
        play = AsyncMock(wraps=self.engine.play)
        self.engine.play = play
        await self.b.on_event(self.event('start', 1, lead=-3000))
        self.assertEqual(self.wire.emitted[-1]['errorCode'], 'effective_at_missed')
        play.assert_not_awaited()
        self.wire.canonical.update(queueSongIds=['new'], trackId='new', currentIndex=0, state='paused',
                                   version=2, queueRevision=2, controlVersion=2, positionMs=33)
        await self.b.on_event(self.event('stop', 2))
        self.assertEqual(self.engine.loads[-1][0], 'https://music.test/new')
        self.assertEqual(self.player.applied, 2)

    async def test_compact_terminal_recovers_stopped_without_play(self):
        self.player.executed['state'] = self.wire.canonical['state'] = 'stopped'
        await self.b.on_event(self.event('start', 1))
        restore = {'type': 'event', 'action': 'broadcast.restore', 'payload': {
            'playbackContextId': 'source', 'broadcastId': 'group', 'deliveryId': 'compact',
            'deviceSessionId': self.wire.device_session_id, 'terminalBroadcastRevision': 3,
            'suspendedPlaybackContextId': 'ctx', 'suspendedEpoch': 1, 'suspendedControlVersion': 0,
            'lastAppliedBroadcastRevision': 1, 'queueIndex': 0, 'trackId': 'mirror',
            'state': 'stopped', 'positionMs': 500, 'playbackRate': 1.25}}
        await self.b.on_event(restore)
        self.assertEqual(self.player.executed['state'], 'stopped')
        self.assertIsNone(self.engine.run)
        self.assertEqual(self.wire.emitted[-1]['appliedBroadcastRevision'], 3)
        self.assertTrue(self.wire.emitted[-1]['restoreCompleted'])
