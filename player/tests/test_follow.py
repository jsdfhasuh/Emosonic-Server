"""Strict Follow lifecycle, source provenance and native position correction."""
import asyncio
import copy
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from emosonic_player.errors import PlaybackError
from emosonic_player.follow import Follow


def context(name, songs, *, index=0, state='playing'):
    return {'playbackContextId': name, 'authorityClientId': name, 'authorityDeviceSessionId': name + '-dev',
            'queueSongIds': songs, 'currentIndex': index, 'trackId': songs[index], 'state': state,
            'positionMs': 0, 'version': 4, 'epoch': 2, 'controlVersion': 3, 'queueRevision': 2}


def fact(ctx, seq=1, **changes):
    sample = {'playbackContextId': ctx['playbackContextId'], 'clientId': ctx['authorityClientId'],
              'deviceSessionId': ctx['authorityDeviceSessionId'], 'trackId': ctx['trackId'],
              'appliedControlVersion': ctx['controlVersion'], 'clientSeq': seq, 'state': ctx['state'],
              'positionMs': 5000, 'positionSampledAtServerMs': 100000,
              'serverUpdatedAtMs': 100000, 'playbackRate': 1.0}
    return dict(sample, **changes)


class Engine:
    def __init__(self):
        self.run = 'ordinary'
        self.paused, self.position, self.speed = False, 1801, 1.0
        self.loads, self.seeks = [], []

    async def load(self, url, run_id, position_ms=0):
        self.loads.append(url)
        self.run, self.position, self.paused = run_id, position_ms, True

    async def snapshot(self):
        return {'run_id': self.run, 'position_ms': self.position, 'paused': self.paused,
                'speed': self.speed, 'buffering': False}

    async def seek(self, position_ms):
        self.position = position_ms
        self.seeks.append(position_ms)

    async def set_speed(self, rate):
        self.speed = rate

    async def stop(self):
        self.run, self.paused = None, True

    async def pause(self):
        self.paused = True

    async def play(self):
        self.paused = False


class Wire:
    def __init__(self):
        self.client_id, self.device_session_id = 'local', 'local-dev'
        self.server_url, self.username = 'https://fixture.test', 'fixture'
        self.negotiated = {'supportsFollow': True, 'effectiveAtPlayback': True}
        self.ready, self.generation, self.clock = True, 1, 100000
        self.own = context('local', ['original'])
        self.source = context('speaker', ['first', 'second'])
        self.source_fact = fact(self.source)
        self.commands = []
        self.wrong_ack = self.fail_stop = False

    def server_time_ms(self):
        return self.clock

    async def request(self, action, payload, **kwargs):
        self.commands.append((action, payload))
        if action == 'playback.context.status':
            source = payload['playbackContextId'] == 'speaker'
            return {'payload': {'playbackContext': copy.deepcopy(self.source if source else self.own),
                                'deviceStates': [copy.deepcopy(self.source_fact if source else fact(self.own))]}}
        if action == 'follow.start':
            return {'payload': {'action': action, 'status': 'active', 'sourcePlaybackContextId': 'speaker',
                                'suspendedAuthorityClientId': 'local', 'suspendedPlaybackContextId': 'local',
                                'suspendedAuthorityDeviceSessionId': 'local-dev', 'suspendedEpoch': 2,
                                'suspendedVersion': 4, 'suspendedQueueRevision': 2,
                                'suspendedControlVersion': 30 if self.wrong_ack else 3,
                                'suspendedAppliedControlVersion': 3}}
        if action == 'follow.stop':
            if self.fail_stop:
                raise PlaybackError('request_timeout')
            assert payload == {'sourcePlaybackContextId': 'speaker'}
            return {'payload': {'action': action}}
        raise AssertionError(action)


class FollowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wire, self.engine = Wire(), Engine()
        player = SimpleNamespace(wire=self.wire, engine=self.engine, media=SimpleNamespace(stream_url=lambda s: s),
                                 lock=asyncio.Lock(), run_id=self.engine.run, context=self.wire.own,
                                 executed=copy.deepcopy(self.wire.own), applied=3,
                                 _is_ours=lambda ctx: ctx['authorityClientId'] == 'local',
                                 announcement=None, _announcement_request=None, _safe_stop=self.engine.stop)

        async def load(ctx, paused):
            await self.engine.load(ctx['queueSongIds'][ctx['currentIndex']], 'restored', ctx['positionMs'])
            player.run_id = 'restored'
            if not paused:
                await self.engine.play()

        player._load = load
        self.player, self.follow = player, Follow(player, self.tmp.name)
        self.addAsyncCleanup(self.follow.close)

    async def emit(self, seq, **changes):
        f = fact(self.wire.source, seq, **changes)
        f['sourceClientId'] = f.pop('clientId')
        await self.follow.on_event({'action': 'playback.update', 'payload': f})

    async def test_start_native_position_and_stop_restore_ordinary_queue(self):
        self.wire.clock += 240
        await self.follow.start('speaker')
        self.assertEqual((self.follow.phase, self.engine.loads, self.engine.position), ('playing', ['first'], 5240))
        self.assertIsNone(self.player.run_id)
        self.assertEqual(self.player.context['queueSongIds'], ['original'])
        self.assertTrue(Path(self.tmp.name, 'follow.json').is_file())
        await self.follow.stop()
        self.assertEqual(self.engine.loads, ['first', 'original'])
        self.assertEqual((self.engine.position, self.engine.paused), (1801, False))
        self.assertFalse(self.follow.active)
        self.assertFalse(Path(self.tmp.name, 'follow.json').exists())
        self.assertNotIn('playback.update', [a for a, _ in self.wire.commands])

    async def test_slew_seek_nominal_and_ignore_out_of_order_or_foreign_facts(self):
        await self.follow.start('speaker')
        self.engine.position = 4800
        await self.emit(2)
        self.assertGreater(self.engine.speed, 1)
        self.engine.position = 5007
        await self.emit(3)
        self.assertEqual(self.engine.speed, 1)
        self.engine.position = 100
        await self.emit(4)
        self.assertEqual(self.engine.seeks, [5000])
        await self.emit(3, state='paused')
        self.assertEqual(self.follow.phase, 'playing')
        await self.emit(5, deviceSessionId='other', state='paused')
        self.assertEqual(self.follow.phase, 'playing')
        await self.emit(5, appliedControlVersion=2, state='paused')
        self.assertEqual(self.follow.phase, 'playing')
        await self.emit(6, state='paused', positionMs=7000)
        self.assertTrue(self.engine.paused)
        self.assertEqual(self.engine.position, 7000)

    async def test_fifo_queue_revision_track_change_and_source_close(self):
        await self.follow.start('speaker')
        old = copy.deepcopy(self.wire.source)
        src = dict(old, queueRevision=3, controlVersion=4, version=5, currentIndex=1, trackId='second')
        self.wire.source, self.wire.source_fact = src, fact(src, 2, positionMs=100)
        await self.emit(2, positionMs=100)
        await self.follow.on_event({'action': 'queue.context.sync', 'payload': src})
        self.assertEqual(self.engine.loads, ['first', 'second'])
        await self.follow.on_event({'action': 'queue.context.sync', 'payload': old})
        self.assertEqual(self.follow.track, 'second')
        await self.follow.on_event({'action': 'playback.context.closed', 'payload': {'playbackContextId': 'speaker'}})
        self.assertFalse(self.follow.active)
        self.assertEqual(self.engine.loads[-1], 'original')

    async def test_stale_playing_anchor_is_silent_and_fresh_fact_can_resume(self):
        await self.follow.start('speaker')
        self.wire.clock += 3000
        await self.emit(2)
        self.assertTrue(self.engine.paused)
        self.assertEqual(self.follow.phase, 'waiting_source')
        await self.emit(3, positionMs=8000, positionSampledAtServerMs=self.wire.clock, serverUpdatedAtMs=self.wire.clock)
        self.assertFalse(self.engine.paused)
        self.assertEqual(self.engine.position, 8000)

    async def test_slow_native_load_holds_file_until_new_source_fact(self):
        load = self.engine.load
        async def slow(*args, **kwargs):
            await load(*args, **kwargs)
            self.wire.clock += 2500
        self.engine.load = slow
        await self.follow.start('speaker')
        self.assertTrue(self.engine.paused)
        self.assertEqual(self.follow.phase, 'waiting_source')
        await self.emit(2, positionMs=7500, positionSampledAtServerMs=self.wire.clock,
                        serverUpdatedAtMs=self.wire.clock)
        self.assertEqual(self.engine.loads, ['first'])
        self.assertEqual(self.engine.position, 7500)
        self.assertFalse(self.engine.paused)

    async def test_lost_source_member_restores_authorized_queue_cursor(self):
        await self.follow.start('speaker')
        self.wire.own.update(queueSongIds=['new'], trackId='new', queueRevision=3,
                             controlVersion=4, version=5, state='paused')
        await self.follow.on_event({'action': 'device.list', 'payload': {'devices': []}})
        self.assertFalse(self.follow.active)
        self.assertEqual((self.engine.loads[-1], self.engine.position, self.engine.paused), ('new', 5000, True))

    async def test_reboot_recovers_pair_only_for_same_server_account_and_stable_device(self):
        await self.follow.start('speaker')
        self.wire.ready = False
        await self.follow.disconnected()
        self.wire.device_session_id = 'replacement'
        self.wire.generation += 1
        self.wire.ready = True
        lookup = lambda server, user, client: Follow.recovery_session(self.tmp.name, server, user, client)
        self.assertEqual(lookup(self.wire.server_url, self.wire.username, 'local'), 'local-dev')
        self.assertIsNone(lookup('https://other.test', self.wire.username, 'local'))
        self.assertIsNone(lookup(self.wire.server_url, 'another', 'local'))
        self.assertIsNone(lookup(self.wire.server_url, self.wire.username, 'different'))
        with self.assertRaises(PlaybackError):
            await self.follow.reconcile()
        self.assertTrue(self.follow.active)
        self.assertNotIn('follow.stop', [a for a, _ in self.wire.commands])
        self.assertTrue(self.engine.paused)
        self.wire.device_session_id = 'local-dev'

    async def test_failed_ack_never_starts_mirror_and_uncertain_stop_retains_recovery_record(self):
        self.wire.wrong_ack = True
        with self.assertRaises(PlaybackError):
            await self.follow.start('speaker')
        self.assertFalse(self.follow.active)
        self.assertEqual(self.engine.loads, [])
        self.wire.wrong_ack = False
        await self.follow.start('speaker')
        self.wire.fail_stop = True
        with self.assertRaises(PlaybackError):
            await self.follow.stop()
        self.assertEqual(self.follow.phase, 'release_pending')
        self.assertTrue(self.engine.paused)
        self.wire.fail_stop = False
        await self.follow.stop()
        self.assertFalse(self.follow.active)

    async def test_reconnect_releases_pending_lease_without_replaying_mirror(self):
        await self.follow.start('speaker')
        self.wire.ready = False
        await self.follow.disconnected()
        self.assertTrue(self.engine.paused)
        self.assertEqual(self.follow.phase, 'release_pending')
        recovered = Follow(self.player, self.tmp.name)
        self.wire.ready = True
        self.wire.generation += 1
        await recovered.reconcile()
        self.assertFalse(recovered.active)
        self.assertEqual(self.engine.loads, ['first'])
        self.wire.ready = False

    async def test_lost_stop_reply_is_retried_and_unblocks_ordinary_controls(self):
        await self.follow.start('speaker')
        self.wire.fail_stop = True
        with self.assertRaises(PlaybackError):
            await self.follow.stop()
        self.assertTrue(self.follow.active)
        self.wire.fail_stop = False
        await asyncio.wait_for(self.follow.release_retry, 3)
        self.assertFalse(self.follow.active)
        self.assertEqual(self.engine.loads, ['first', 'original'])
