"""Strict-v2 Handoff. The source owns the context until the server commits the transfer."""
import asyncio
from collections import deque
import copy
from dataclasses import dataclass, field
import math
import uuid

from .errors import PlaybackError


@dataclass
class Target:
    prepare: dict
    generation: int
    standby_id: str | None
    run: str = field(default_factory=lambda: uuid.uuid4().hex)
    phase: str = 'preparing'
    proof: dict | None = None


@dataclass
class Source:
    context_id: str
    handoff_id: str
    version: int
    generation: int
    new_pair: tuple | None = None


class Handoff:
    def __init__(self, player):
        self.player = player
        self.wire = player.wire
        self.target = None
        self.source = None
        self._timer = None
        self._terminal = deque(maxlen=32)

    @property
    def busy(self):
        return self.target is not None or bool(self.source and self.source.new_pair)

    def _current(self, target):
        return self.target is target and self.wire.ready and self.wire.generation == target.generation

    def _supports(self):
        caps = self.wire.negotiated
        return caps.get('playbackPrepare') is True and caps.get('effectiveAtPlayback') is True

    def _clear_timer(self):
        if self._timer and self._timer is not asyncio.current_task():
            self._timer.cancel()
        self._timer = None

    def _timeout(self, seconds, callback, argument):
        self._clear_timer()

        async def after_delay():
            await asyncio.sleep(seconds)
            await callback(argument)

        self._timer = asyncio.create_task(after_delay())

    async def _discard(self, target):
        if self.target is not target:
            return
        target.phase = 'cancelled'
        self._clear_timer()
        async with self.player.lock:
            if self.target is target:
                await self.player._safe_stop()
                self._terminal.append(target.prepare['handoffId'])
                self.target = None

    async def reset(self):
        self._clear_timer()
        self.source = None
        if self.target:
            await self._discard(self.target)
        self._terminal.clear()

    async def start(self, target_client_id):
        context = self.player.context
        if (not self._supports() or not context or not self.player._is_ours(context)
                or not target_client_id or target_client_id == self.wire.client_id):
            raise PlaybackError('handoff source or target unavailable')
        devices = await self.wire.request('device.list', {}, type='state', reply_actions={'device.list'})
        matches = [d['deviceSessionId'] for d in devices['payload']['devices']
                   if d.get('clientId') == target_client_id and d.get('deviceSessionId')]
        if len(matches) != 1:
            raise PlaybackError('handoff target is not connected')
        reply = await self.wire.request('playback.handoff.start', {
            'playbackContextId': context['playbackContextId'], 'targetClientId': target_client_id,
            'targetDeviceSessionId': matches[0],
            'baseControlVersion': context['controlVersion']})
        result = reply['payload']
        if self.source is None and result['handoffId'] not in self._terminal:
            self.source = Source(context['playbackContextId'], result['handoffId'],
                                 context['controlVersion'], self.wire.generation)
        return result

    async def cancel(self):
        if self.target:
            p = self.target.prepare
        elif self.source:
            p = {'playbackContextId': self.source.context_id, 'handoffId': self.source.handoff_id}
        else:
            raise PlaybackError('no current handoff')
        reply = await self.wire.request('playback.handoff.cancel',
                                        {k: p[k] for k in ('playbackContextId', 'handoffId')})
        return reply['payload']

    async def on_event(self, message):
        action = message['action']
        p = message.get('payload', {})
        if action in ('playback.context.status', 'queue.context.sync'):
            context = p.get('playbackContext', p)
            target = self.target
            if target and context.get('playbackContextId') == target.prepare['playbackContextId']:
                if target.phase == 'confirming' and self.player._is_ours(context):
                    await self._adopt(target, context)
                return True
            source = self.source
            if (source and source.new_pair and context.get('playbackContextId') == source.context_id
                    and (context.get('authorityClientId'), context.get('authorityDeviceSessionId')) == source.new_pair):
                return True  # release follows the transferred-context snapshot
            return False
        if action == 'playback.context.closed' and self.target:
            return p.get('playbackContextId') == self.target.standby_id
        if action == 'playback.prepare':
            if self._supports() and message.get('type') == 'command':
                await self._prepare(p)
        elif action == 'player.play' and 'handoffId' in p:
            if self._supports() and message.get('type') == 'command':
                await self._commit(p)
        elif action in ('playback.handoff.status', 'playback.handoff.cancel', 'playback.handoff.release'):
            if self._supports():
                await self._observe(action, p)
        else:
            return False
        return True

    async def _ready(self, p, ready):
        reply = {k: p[k] for k in ('playbackContextId', 'prepareId', 'handoffId', 'deviceSessionId')}
        reply['ready'] = ready
        if not ready:
            reply['errorCode'] = 'prepare_failed'
        await self.wire.emit('playback.ready', reply)

    async def _prepare(self, p):
        player = self.player
        if (p.get('deviceSessionId') != self.wire.device_session_id or not p.get('handoffId')
                or p['handoffId'] in self._terminal or self.target and self.target.prepare == p):
            return
        queue, index = p.get('queueSongIds'), p.get('currentIndex')
        context = player.context or {}
        if (self.busy or player.follow.active or player.broadcast.occupied
                or player.run_id or player.announcement or player._announcement_request
                or context and (context.get('state') != 'idle' or not player._is_ours(context))
                or p.get('sourceClientId') != p.get('authorityClientId') or not p.get('authorityDeviceSessionId')
                or not isinstance(queue, list) or type(index) is not int or not 0 <= index < len(queue)
                or p.get('trackId', queue[index]) != queue[index]):
            await self._ready(p, False)
            return
        t = self.target = Target(copy.deepcopy(p), self.wire.generation, context.get('playbackContextId'))
        self._timeout(8, self._discard, t)
        try:
            async with player.lock:
                if not self._current(t) or t.phase != 'preparing':
                    return
                await player.engine.load(player.media.stream_url(queue[index]), t.run, position_ms=p['positionMs'])
                sample = await player.engine.snapshot()
                if sample['run_id'] != t.run or sample['paused'] is not True or sample['position_ms'] is None:
                    raise ValueError('preload not confirmed')
                if not self._current(t) or t.phase != 'preparing':
                    return
                t.phase = 'ready'
            await self._ready(p, True)
        except Exception:
            if self._current(t) and t.phase != 'cancelled':
                await self._discard(t)
                await self._ready(p, False)

    async def _commit(self, commit):
        t = self.target
        if not t or t.phase != 'ready' or not self._current(t):
            return
        p = t.prepare
        if (any(p[k] != commit.get(k) for k in ('playbackContextId', 'handoffId', 'sourceClientId'))
                or commit.get('controlVersion') != p['controlVersion'] + 1):
            return
        t.phase = 'committing'
        self._timeout(5, self._discard, t)
        try:
            rate, at = commit['playbackRate'], commit['effectiveAtServerMs']
            if not 0.5 <= rate <= 2 or rate != p['playbackRate'] or at - commit['serverTimeMs'] < 250:
                raise ValueError('invalid commit anchor')
            while (remaining := at - self.wire.server_time_ms()) > 0:
                await asyncio.sleep(min(remaining, 250) / 1000)
                if not self._current(t) or t.phase != 'committing':
                    return
            async with self.player.lock:
                if not self._current(t) or t.phase != 'committing':
                    return
                engine = self.player.engine
                await engine.set_speed(rate)
                sample = await engine.snapshot()
                if sample['run_id'] != t.run or sample['paused'] is not True:
                    raise ValueError('preload replaced')
                position = self._project(commit, sample)
                await engine.seek(position)
                self._project(commit, sample)
                if not self._current(t) or t.phase != 'committing':
                    return
                await engine.play()
                sample = await engine.snapshot()
                expected = self._project(commit, sample)
                actual = sample.get('position_ms')
                if (sample['run_id'] != t.run or sample['paused'] is not False or sample['buffering'] is not False
                        or sample.get('speed') != rate or actual is None or not math.isfinite(actual)
                        or abs(actual - expected) > 1000):
                    raise ValueError('engine did not confirm handoff playback')
                if not self._current(t) or t.phase != 'committing':
                    return
                self.player.sequence += 1
                t.proof = {'playbackContextId': p['playbackContextId'], 'handoffId': p['handoffId'],
                           'deviceSessionId': self.wire.device_session_id, 'queueIndex': p['currentIndex'],
                           'trackId': p['queueSongIds'][p['currentIndex']], 'state': 'playing',
                           'positionMs': round(actual), 'positionSampledAtServerMs': self.wire.server_time_ms(),
                           'playbackRate': sample['speed'], 'appliedControlVersion': commit['controlVersion'],
                           'clientSeq': self.player.sequence}
                t.phase = 'confirming'
            await self.wire.send_confirmed('playback.handoff.complete', t.proof, 'playback.handoff.status',
                lambda m: (m['payload'].get('handoffId') == p['handoffId']
                           and m['payload'].get('status') == 'completed'
                           and m['payload'].get('newAuthorityClientId') == self.wire.client_id
                           and m['payload'].get('newAuthorityDeviceSessionId') == self.wire.device_session_id))
            if self._current(t) and t.phase == 'confirming':
                context = await self._status(p['playbackContextId'])
                await self._adopt(t, context)
        except Exception:
            if self._current(t) and t.phase != 'cancelled':
                await self._discard(t)
                await self.wire.request('playback.handoff.cancel', {
                    'playbackContextId': p['playbackContextId'], 'handoffId': p['handoffId'],
                    'reason': 'commit_failed', 'errorCode': 'commit_failed'})

    def _project(self, commit, sample):
        elapsed = self.wire.server_time_ms() - commit['effectiveAtServerMs']
        position = commit['positionMs'] + math.floor(elapsed * commit['playbackRate'])
        if not 0 <= elapsed <= 1000 or sample.get('duration_ms') is not None and position >= sample['duration_ms']:
            raise ValueError('handoff outside execution window')
        return position

    async def _status(self, context_id):
        result = await self.wire.request('playback.context.status', {'playbackContextId': context_id},
                                         type='state', reply_actions={'playback.context.status'})
        return result['payload']['playbackContext']

    async def _adopt(self, t, context):
        async with self.player.lock:
            p = t.prepare
            if (not self._current(t) or t.phase != 'confirming' or not self.player._is_ours(context)
                    or context.get('playbackContextId') != p['playbackContextId']
                    or context.get('epoch') != p['sourceEpoch'] + 1
                    or context['controlVersion'] < t.proof['appliedControlVersion']):
                return
            player = self.player
            player.context = copy.deepcopy(context)
            player.applied = t.proof['appliedControlVersion']
            player.executed = dict(copy.deepcopy(context), queueSongIds=p['queueSongIds'], currentIndex=p['currentIndex'],
                                   queueRevision=p['sourceQueueRevision'], controlVersion=player.applied,
                                   state='playing', positionMs=t.proof['positionMs'])
            player.run_id, player.error = t.run, None
            self._terminal.append(p['handoffId'])
            self._clear_timer()
            self.target = None

    async def _observe(self, action, p):
        handoff_id, context_id = p.get('handoffId'), p.get('playbackContextId')
        if not handoff_id or handoff_id in self._terminal:
            return
        t = self.target
        if t and (handoff_id, context_id) == (t.prepare['handoffId'], t.prepare['playbackContextId']):
            if action == 'playback.handoff.cancel' or p.get('status') in ('failed', 'cancelled', 'timedOut'):
                await self._discard(t)
            return
        context = self.player.context or {}
        source = self.source
        if (not source and action == 'playback.handoff.status' and p.get('sourceClientId') == self.wire.client_id
                and context.get('playbackContextId') == context_id and self.player._is_ours(context)
                and p.get('status') in ('preparing', 'ready', 'committing', 'completed')):
            source = self.source = Source(context_id, handoff_id, context['controlVersion'], self.wire.generation)
        if (not source or (handoff_id, context_id) != (source.handoff_id, source.context_id)
                or source.generation != self.wire.generation):
            return
        if action == 'playback.handoff.cancel' or p.get('status') in ('failed', 'cancelled', 'timedOut'):
            self._terminal.append(handoff_id)
            self._clear_timer()
            self.source = None
            return
        pair = (p.get('newAuthorityClientId'), p.get('newAuthorityDeviceSessionId'))
        if p.get('controlVersion') != source.version + 1 or not all(pair) or pair[0] == self.wire.client_id:
            return
        if p.get('status') == 'completed':
            source.new_pair = pair
            self._timeout(1, self._recover_release, source)
        elif action == 'playback.handoff.release' and p.get('instruction') == 'pause' and source.new_pair == pair:
            await self._release(source)

    async def _recover_release(self, source):
        # Completed is durable. If the one-shot release was lost, confirm the new owner first.
        try:
            context = await self._status(source.context_id)
            if (context.get('authorityClientId'), context.get('authorityDeviceSessionId')) == source.new_pair:
                await self._release(source)
        except Exception:
            if self.source is source and self.wire.ready:
                self._timeout(1, self._recover_release, source)

    async def _release(self, source):
        async with self.player.lock:
            if self.source is not source or self.wire.generation != source.generation:
                return
            self._clear_timer()
            self._terminal.append(source.handoff_id)
            self.source = None
            self.player.run_id = None
            self.player.executed = self.player.context = None
            await self.player._safe_stop()
        # The transferred context remains active on the target. Become an idle target for a return handoff.
        if self.wire.ready and self.wire.generation == source.generation:
            await self.player.reconcile()
