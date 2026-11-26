import asyncio
import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from emosonic_player.handoff import Handoff


class Engine:
    def __init__(self):
        self.run = None
        self.paused = True
        self.position = 0
        self.rate = 1.0
        self.play_calls = 0

    async def load(self, url, run, position_ms):
        self.run, self.position, self.paused = run, position_ms, True

    async def stop(self):
        self.run = None
        self.paused = True

    async def seek(self, position):
        self.position = position

    async def set_speed(self, rate):
        self.rate = rate

    async def play(self):
        self.paused = False
        self.play_calls += 1

    async def snapshot(self):
        return {'run_id': self.run, 'paused': self.paused, 'buffering': False, 'speed': self.rate,
                'position_ms': self.position, 'duration_ms': 60_000}


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = Engine()
        self.wire = SimpleNamespace(
            client_id='speaker', device_session_id='speaker-session', generation=1, ready=True,
            negotiated={'effectiveAtPlayback': True, 'playbackPrepare': True},
            server_time_ms=lambda: int(time.monotonic() * 1000), emit=AsyncMock(), request=AsyncMock(),
            send_confirmed=AsyncMock())
        self.idle = {'playbackContextId': 'standby', 'state': 'idle', 'authorityClientId': 'speaker',
                     'authorityDeviceSessionId': 'speaker-session', 'controlVersion': 1}
        self.player = SimpleNamespace(
            wire=self.wire, engine=self.engine, media=SimpleNamespace(stream_url=lambda track: track),
            lock=asyncio.Lock(), run_id=None, context=self.idle, executed=self.idle, sequence=2, applied=1,
            announcement=None, _announcement_request=None, error=None, reconcile=AsyncMock(),
            follow=SimpleNamespace(active=False), broadcast=SimpleNamespace(occupied=False),
            _is_ours=lambda c: (c.get('authorityClientId'), c.get('authorityDeviceSessionId')) ==
                               ('speaker', 'speaker-session'), _safe_stop=self.engine.stop)
        self.h = Handoff(self.player)
        self.addAsyncCleanup(self.h.reset)
        self.prepare = {
            'playbackContextId': 'music', 'prepareId': 'prepared', 'handoffId': 'transfer',
            'sourceClientId': 'desktop', 'authorityClientId': 'desktop', 'authorityDeviceSessionId': 'desktop-session',
            'deviceSessionId': 'speaker-session', 'queueSongIds': ['one', 'two'], 'currentIndex': 1,
            'trackId': 'two', 'positionMs': 1500, 'positionSampledAtServerMs': self.wire.server_time_ms(),
            'playbackRate': 1.25, 'controlVersion': 5, 'sourceEpoch': 4, 'sourceVersion': 10, 'sourceQueueRevision': 3}
        self.adopted = dict(self.idle, playbackContextId='music', epoch=5, version=11, controlVersion=6,
                            state='playing', queueSongIds=['one', 'two'], currentIndex=1, queueRevision=3)
        self.wire.request.return_value = {'payload': {'playbackContext': self.adopted}}

    async def dispatch(self, action, p, kind='command'):
        return await self.h.on_event({'action': action, 'type': kind, 'payload': p})

    def commit(self, delay=0):
        at = self.wire.server_time_ms() + delay
        return {'playbackContextId': 'music', 'handoffId': 'transfer', 'sourceClientId': 'desktop',
                'controlVersion': 6, 'effectiveAtServerMs': at, 'serverTimeMs': at-300,
                'positionMs': 2500, 'playbackRate': 1.25}

    async def test_preload_is_silent_and_adopts_only_with_measured_complete_and_new_owner(self):
        await self.dispatch('playback.prepare', self.prepare)
        self.assertTrue(self.engine.paused)
        self.assertEqual(self.engine.play_calls, 0)
        self.wire.emit.assert_awaited_once_with('playback.ready', {
            'playbackContextId': 'music', 'prepareId': 'prepared', 'handoffId': 'transfer',
            'deviceSessionId': 'speaker-session', 'ready': True})
        await self.dispatch('playback.prepare', copy.deepcopy(self.prepare))
        self.assertTrue(self.h.busy)
        self.assertEqual(self.player.context['playbackContextId'], 'standby')
        commit = self.commit(50)
        await self.dispatch('player.play', commit)
        payload = self.wire.send_confirmed.await_args.args[1]
        self.assertEqual(payload['appliedControlVersion'], 6)
        self.assertEqual(payload['positionMs'], self.engine.position)
        self.assertGreaterEqual(payload['positionSampledAtServerMs'], commit['effectiveAtServerMs'])
        self.assertEqual((payload['queueIndex'], payload['trackId'], payload['playbackRate']), (1, 'two', 1.25))
        self.assertEqual(self.player.run_id, self.engine.run)
        self.assertEqual(self.player.context, self.adopted)
        self.assertFalse(self.h.busy)
        await self.dispatch('player.play', commit)
        await self.dispatch('playback.prepare', self.prepare)
        self.assertEqual(self.engine.play_calls, 1)

    async def test_cancel_during_effective_at_wait_does_not_play_or_complete(self):
        await self.dispatch('playback.prepare', self.prepare)
        task = asyncio.create_task(self.dispatch('player.play', self.commit(170)))
        await asyncio.sleep(.01)
        await self.dispatch('playback.handoff.cancel', dict(self.prepare, reason='cancelled'))
        await task
        self.assertIsNone(self.engine.run)
        self.assertFalse(self.h.busy)
        self.assertEqual(self.engine.play_calls, 0)
        self.wire.send_confirmed.assert_not_awaited()
        self.assertEqual(self.player.context, self.idle)

    async def test_prepare_failure_stale_target_and_busy_engine(self):
        await self.dispatch('playback.prepare', dict(self.prepare, deviceSessionId='old-session'))
        self.wire.emit.assert_not_awaited()
        self.player.run_id = 'other-run'
        await self.dispatch('playback.prepare', self.prepare)
        self.assertFalse(self.wire.emit.await_args.args[1]['ready'])
        self.assertEqual(self.player.run_id, 'other-run')
        self.player.run_id = None
        self.engine.load = AsyncMock(side_effect=OSError())
        await self.dispatch('playback.prepare', self.prepare)
        self.assertEqual(self.wire.emit.await_args.args[1]['errorCode'], 'prepare_failed')
        self.assertFalse(self.h.busy)

    async def test_stale_commit_invalid_version_and_disconnect(self):
        await self.dispatch('playback.prepare', self.prepare)
        await self.dispatch('player.play', dict(self.commit(), controlVersion=7))
        self.assertTrue(self.h.busy)
        task = asyncio.create_task(self.dispatch('player.play', self.commit(70)))
        await asyncio.sleep(.01)
        self.wire.generation += 1
        await self.h.reset()
        await task
        self.assertEqual(self.engine.play_calls, 0)
        self.wire.send_confirmed.assert_not_awaited()

    async def test_late_commit_or_unconfirmed_audio_cannot_transfer_authority(self):
        for failure in ('too_late', 'audio_paused'):
            with self.subTest(failure=failure):
                await self.h.reset()
                await self.dispatch('playback.prepare', self.prepare)
                if failure == 'audio_paused':
                    self.engine.play = AsyncMock()
                await self.dispatch('player.play', self.commit(-1700 if failure == 'too_late' else 0))
                self.wire.send_confirmed.assert_not_awaited()
                self.assertEqual(self.wire.request.await_args.args[0], 'playback.handoff.cancel')
                self.assertEqual(self.wire.request.await_args.args[1]['errorCode'], 'commit_failed')
                self.assertEqual(self.player.context, self.idle)
                self.assertIsNone(self.engine.run)

    async def test_source_release_checks_new_pair_version_and_duplicate(self):
        self.player.run_id = self.engine.run = 'existing'
        self.engine.paused = False
        self.player.context = dict(self.idle, playbackContextId='music', controlVersion=5, state='playing')
        base = {'playbackContextId': 'music', 'handoffId': 'transfer', 'sourceClientId': 'speaker'}
        await self.dispatch('playback.handoff.status', dict(base, status='preparing', controlVersion=5), 'state')
        new = dict(base, controlVersion=6, newAuthorityClientId='desktop', newAuthorityDeviceSessionId='desktop-session')
        await self.dispatch('playback.handoff.status', dict(new, status='completed'), 'state')
        await self.dispatch('playback.handoff.release', dict(new, instruction='pause', controlVersion=5))
        await self.dispatch('playback.handoff.release', dict(new, instruction='pause', newAuthorityDeviceSessionId='old'))
        self.assertFalse(self.engine.paused)
        await self.dispatch('playback.handoff.release', dict(new, instruction='pause'))
        await self.dispatch('playback.handoff.release', dict(new, instruction='pause'))
        self.assertTrue(self.engine.paused)
        self.assertIsNone(self.player.run_id)
        self.player.reconcile.assert_awaited_once()

    async def test_source_cancellation_and_target_timeout_leave_original_context(self):
        self.player.context = dict(self.idle, state='playing')
        p = {'playbackContextId': 'standby', 'handoffId': 'abandoned', 'controlVersion': 1,
             'sourceClientId': 'speaker'}
        await self.dispatch('playback.handoff.status', dict(p, status='preparing'), 'state')
        await self.dispatch('playback.handoff.status', dict(p, status='timedOut'), 'state')
        self.assertIsNone(self.h.source)
        self.player.reconcile.assert_not_awaited()
        self.player.context = self.idle
        await self.dispatch('playback.prepare', self.prepare)
        await self.dispatch('playback.handoff.status', dict(self.prepare, status='timedOut'), 'state')
        self.assertFalse(self.h.busy)
        self.assertIsNone(self.engine.run)
