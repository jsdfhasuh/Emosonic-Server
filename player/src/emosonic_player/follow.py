"""Strict Follow receiver. Mirrored audio is never reported as local queue playback."""
import asyncio
import copy
import json
import math
import os
from pathlib import Path
import tempfile
import time
import uuid

from .config import recovery_scope
from .errors import PlaybackError


def _binding(context):
    return tuple(context.get(k) for k in
                 ('playbackContextId', 'epoch', 'authorityClientId', 'authorityDeviceSessionId'))


def _cursor(context):
    return tuple(context[k] for k in ('controlVersion', 'queueRevision'))


class Follow:
    @staticmethod
    def recovery_session(state_dir, server_url, username, client_id):
        """An interrupted Follow must authenticate as the same account and device pair to release its lease."""
        path = Path(state_dir) / 'follow.json' if state_dir is not None else None
        if path is None or not path.exists():
            return None
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('scope') == recovery_scope(server_url, username, client_id) and record.get('source'):
            return record['bound'][3]
        return None

    def __init__(self, player, state_dir=None):
        self.player = player
        self.wire = player.wire
        self.path = Path(state_dir) / 'follow.json' if state_dir else None
        self.scope = recovery_scope(getattr(self.wire, 'server_url', ''), getattr(self.wire, 'username', ''),
                                    self.wire.client_id) if self.path else None
        self.record = None
        if self.path and self.path.exists():
            self.record = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.record, dict) or not self.record.get('source'):
                raise PlaybackError('invalid Follow recovery record')
            if self.record.get('scope') != self.scope:
                self.record = None
        self.phase = 'release_pending' if self.record else 'idle'
        self.source = self.fact = self.track = self.run = None
        self.speed = 1.0
        self.drift = None
        self.watch = None
        self.last_valid = self.last_refresh = 0.0
        self.generation = None
        self.error = None

    @property
    def active(self):
        return self.record is not None

    def status(self):
        return {'phase': self.phase, 'sourcePlaybackContextId': (self.record or {}).get('source'),
                'trackId': self.track, 'syncDriftMs': self.drift, 'playbackRate': self.speed,
                'error': self.error}

    def _save(self, record):
        if self.path:
            if record is None:
                self.path.unlink(missing_ok=True)
            else:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                                 delete=False) as f:
                    temp = Path(f.name)
                    try:
                        json.dump(record, f, separators=(',', ':'))
                        f.flush()
                        os.fsync(f.fileno())
                    except BaseException:
                        f.close()
                        temp.unlink(missing_ok=True)
                        raise
                try:
                    os.replace(temp, self.path)
                finally:
                    temp.unlink(missing_ok=True)
        self.record = record

    async def _status(self, context_id):
        reply = await self.wire.request('playback.context.status', {'playbackContextId': context_id},
                                        type='state', reply_actions={'playback.context.status'})
        value = reply['payload']
        context = value['playbackContext']
        if context['playbackContextId'] != context_id:
            raise PlaybackError('wrong Follow context')
        fact = next((s for s in value['deviceStates']
                     if s.get('clientId') == context['authorityClientId']
                     and s.get('deviceSessionId') == context['authorityDeviceSessionId']), None)
        return context, fact

    async def start(self, source_context_id):
        async with self.player.lock:
            own = self.player.context
            if (self.active or not self.wire.ready or not self.wire.negotiated.get('supportsFollow')
                    or not self.wire.negotiated.get('effectiveAtPlayback')):
                raise PlaybackError('Follow unavailable or already active')
            if (not isinstance(source_context_id, str) or not source_context_id or not own
                    or not self.player._is_ours(own) or source_context_id == own['playbackContextId']):
                raise PlaybackError('a different active source context is required')
            if self.player.announcement or self.player._announcement_request:
                raise PlaybackError('announcement in progress')
            source, _ = await self._status(source_context_id)
            own, own_fact = await self._status(own['playbackContextId'])
            if (not self.player._is_ours(own) or not own_fact
                    or own['controlVersion'] != self.player.applied
                    or own_fact.get('appliedControlVersion') != self.player.applied):
                raise PlaybackError('suspended authority is not settled')
            sample = await self.player.engine.snapshot()
            old_state = 'stopped' if own['queueSongIds'] else 'idle'
            if self.player.run_id:
                old_state = 'paused' if sample.get('paused') else 'playing'
            position = sample.get('position_ms')
            position = round(position) if position is not None else own_fact['positionMs']
            record = {'source': source_context_id, 'scope': self.scope, 'bound': _binding(own), 'cursor': _cursor(own),
                      'originalState': old_state, 'positionMs': position}
            self._save(record)
            self.phase = 'acquiring'
            self.source, self.fact = source, None
            self.generation = self.wire.generation
            touched_audio = False
            try:
                ack = (await self.wire.request('follow.start', {
                    'sourcePlaybackContextId': source_context_id,
                    'deviceSessionId': self.wire.device_session_id}))['payload']
                expected = {'action': 'follow.start', 'status': 'active',
                            'sourcePlaybackContextId': source_context_id,
                            'suspendedPlaybackContextId': own['playbackContextId'],
                            'suspendedAuthorityClientId': self.wire.client_id,
                            'suspendedAuthorityDeviceSessionId': self.wire.device_session_id,
                            'suspendedEpoch': own['epoch'], 'suspendedVersion': own['version'],
                            'suspendedQueueRevision': own['queueRevision'],
                            'suspendedControlVersion': own['controlVersion'],
                            'suspendedAppliedControlVersion': self.player.applied}
                if any(ack.get(k) != v for k, v in expected.items()) or not self._current():
                    raise PlaybackError('Follow start acknowledgement mismatch')
                touched_audio = True
                self.player.run_id = None
                await self.player._safe_stop()
                self.phase = 'waiting_source'
                self.last_valid = time.monotonic()
                await self._refresh()
                self.watch = asyncio.create_task(self._monitor())
                return self.status()
            except Exception:
                await self._stop(restore=touched_audio)
                raise

    def _current(self):
        return self.active and self.wire.ready and self.generation == self.wire.generation

    async def _refresh(self):
        self.last_refresh = time.monotonic()
        context, fact = await self._status(self.record['source'])
        if not self._current():
            raise PlaybackError('Follow connection changed')
        if _binding(context) != _binding(self.source):
            await self._stop(restore=True)
            raise PlaybackError('Follow source authority changed')
        self._queue(context)
        if fact:
            self._fact(fact)
        await self._apply()

    def _queue(self, value):
        old = self.source
        if (_binding(value) == _binding(old) and value['queueRevision'] >= old['queueRevision']
                and value['controlVersion'] >= old['controlVersion']
                and value.get('version', 0) >= old.get('version', 0)):
            self.source = value

    def _fact(self, value):
        source = self.source
        client = value.get('sourceClientId', value.get('clientId'))
        if (value.get('playbackContextId') != source['playbackContextId']
                or client != source['authorityClientId']
                or value.get('deviceSessionId') != source['authorityDeviceSessionId']
                or type(value.get('clientSeq')) is not int
                or type(value.get('appliedControlVersion')) is not int
                or value['appliedControlVersion'] < source['controlVersion']
                or (self.fact and (value['clientSeq'] <= self.fact['clientSeq']
                                   or value['appliedControlVersion'] < self.fact['appliedControlVersion']))):
            return
        self.fact = value

    def _anchor(self):
        fact = self.fact
        source = self.source
        if not fact or not self._current() or fact.get('appliedControlVersion') != source['controlVersion']:
            return None
        if fact['state'] == 'idle' and not source['queueSongIds']:
            return ('idle', None, 0, 1.0)
        index = source.get('currentIndex')
        if (type(index) is not int or not 0 <= index < len(source['queueSongIds'])
                or source['queueSongIds'][index] != fact.get('trackId')):
            return None
        state = fact.get('state')
        position, rate = fact.get('positionMs'), fact.get('playbackRate')
        now = self.wire.server_time_ms()
        age = now - fact['positionSampledAtServerMs']
        received = now - fact['serverUpdatedAtMs']
        if (type(position) is not int or position < 0 or type(rate) not in (int, float)
                or not math.isfinite(rate) or not .5 <= rate <= 2 or state not in ('playing', 'paused', 'stopped')
                or age < -50 or received < -50 or (state == 'playing' and max(age, received) > 2000)):
            return None
        return state, fact['trackId'], position + (round(max(0, age) * rate) if state == 'playing' else 0), rate

    async def _set_speed(self, speed):
        if abs(self.speed - speed) > .001:
            await self.player.engine.set_speed(speed)
            self.speed = speed

    async def _quiet(self):
        if self.run:
            await self.player._safe_stop()
        try:
            if not getattr(self.player, 'needs_engine_restart', False):
                await self._set_speed(1.0)
        except Exception:
            await self.player.engine.close()
            self.player.needs_engine_restart = True
        self.speed = 1.0
        self.track = self.run = self.drift = None

    async def _hold(self):
        # A slow load may outlive its first anchor. Retain the paused native run
        # so the next fresh sample can resume it without another network load.
        if self.run and not (await self.player.engine.snapshot()).get('paused'):
            await self.player.engine.pause()
        await self._set_speed(1.0)
        self.drift = None
        self.phase = 'waiting_source'

    async def _align_paused(self, target):
        # A native audio-only seek can settle before the requested PTS by an
        # output-buffer length. Use readback to correct the residual while muted.
        engine = self.player.engine
        aim = target
        for _ in range(3):
            await engine.seek(aim)
            await asyncio.sleep(.08)
            sample = await engine.snapshot()
            self.drift = round(sample['position_ms'] - target)
            if abs(self.drift) <= 25:
                break
            aim = max(0, aim - self.drift)
            if sample.get('duration_ms'):
                aim = min(sample['duration_ms'] - 1, aim)

    async def _apply(self):
        anchor = self._anchor()
        if not anchor:
            await self._hold()
            return
        state, track, target, rate = anchor
        self.last_valid = time.monotonic()
        if state in ('stopped', 'idle'):
            await self._quiet()
            self.phase = state
            return
        engine = self.player.engine
        sample = await engine.snapshot()
        if self.track != track or self.run is None or sample.get('run_id') != self.run:
            self.run = uuid.uuid4().hex
            await engine.load(self.player.media.stream_url(track), self.run, position_ms=target)
            self.track = track
            sample = await engine.snapshot()
        # Project again after native loading. A delayed load cannot start from an expired source sample.
        anchor = self._anchor()
        if not anchor or anchor[:2] != (state, track):
            await self._hold()
            return
        _, _, target, rate = anchor
        if state == 'paused' and not sample.get('paused'):
            await engine.pause()
            sample = await engine.snapshot()
        position = sample.get('position_ms')
        if position is None or not math.isfinite(position):
            raise PlaybackError('Follow audio position unavailable')
        sampled_at = sample.get('position_sampled_monotonic')
        if sampled_at is not None and not sample.get('paused') and not sample.get('buffering'):
            elapsed = max(0, asyncio.get_running_loop().time() - sampled_at)
            position += elapsed * 1000 * (sample.get('speed') or 1)
        self.drift = round(position - target)
        if sample.get('speed') is not None:
            self.speed = sample['speed']
        if state == 'paused' and abs(self.drift) > 25:
            await self._align_paused(target)
            await self._set_speed(rate)
        elif state == 'playing' and abs(self.drift) > 300:
            await engine.seek(target)
            await self._set_speed(rate)
        elif state == 'playing' and abs(self.drift) > 12:
            adjustment = max(-.10, min(.10, self.drift / 1200))
            await self._set_speed(max(.5, min(2.0, rate - adjustment)))
        else:
            await self._set_speed(rate)
        if state == 'playing' and sample.get('paused'):
            await engine.play()
        self.phase = state
        self.error = None

    async def on_event(self, message):
        if not self.active or self.phase in ('acquiring', 'release_pending'):
            return False
        action, p = message.get('action'), message['payload']
        context = p.get('playbackContext', p)
        lease = self.record
        generation = self.wire.generation
        if action == 'device.list' and isinstance(p.get('devices'), list):
            present = any(d.get('clientId') == self.source['authorityClientId'] and
                          d.get('deviceSessionId') == self.source['authorityDeviceSessionId']
                          and 'player' in d.get('roles', []) for d in p['devices'])
            if not present:
                async with self.player.lock:
                    if self.record is lease and self.generation == generation and self._current():
                        await self._stop(restore=True)
            return False
        if context.get('playbackContextId') != lease['source']:
            return False
        if action not in ('playback.update', 'queue.context.sync', 'playback.context.status', 'playback.context.closed'):
            return False
        async with self.player.lock:
            if self.record is not lease or not self._current() or self.phase == 'release_pending':
                return True
            try:
                if action == 'playback.context.closed':
                    await self._stop(restore=True)
                elif action == 'playback.update':
                    self._fact(p)
                    await self._apply()
                elif context.get('epoch', 0) >= self.source['epoch'] and _binding(context) != _binding(self.source):
                    await self._stop(restore=True)
                else:
                    self._queue(context)
                    for fact in p.get('deviceStates', []):
                        self._fact(fact)
                    await self._apply()
            except Exception as exc:
                self.error = type(exc).__name__
                if self.active and self.phase != 'release_pending':
                    self.phase = 'waiting_source'
                await self._quiet()
        return True

    async def _monitor(self):
        while self._current():
            await asyncio.sleep(.5)
            async with self.player.lock:
                if not self._current() or self.phase == 'release_pending':
                    return
                try:
                    if time.monotonic() - self.last_valid > 30:
                        await self._stop(restore=True)
                        return
                    if not self._anchor() or time.monotonic() - self.last_refresh > 10:
                        if time.monotonic() - self.last_refresh >= 1:
                            await self._refresh()
                        elif not self._anchor():
                            await self._hold()
                    elif self.phase == 'playing':
                        await self._apply()
                except Exception as exc:
                    self.error = type(exc).__name__
                    if self.active and self.phase != 'release_pending':
                        self.phase = 'waiting_source'
                    await self._quiet()

    async def _stop(self, restore):
        if not self.active:
            return self.status()
        self.phase = 'release_pending'
        if self.watch and self.watch is not asyncio.current_task():
            self.watch.cancel()
        self.watch = None
        await self._quiet()
        record = self.record
        if tuple(record['bound'][2:]) != (self.wire.client_id, self.wire.device_session_id):
            raise PlaybackError('Follow recovery requires its original device session')
        ack = (await self.wire.request('follow.stop', {'sourcePlaybackContextId': record['source']}))['payload']
        if ack.get('action') != 'follow.stop':
            raise PlaybackError('Follow stop acknowledgement mismatch')
        if restore and record['bound'][3] == self.wire.device_session_id:
            own, fact = await self._status(record['bound'][0])
            if (tuple(record['bound']) == _binding(own) and self.player._is_ours(own)
                    and fact and fact.get('appliedControlVersion') == own['controlVersion']):
                self.player.context = copy.deepcopy(own)
                self.player.executed = copy.deepcopy(own)
                self.player.applied = own['controlVersion']
                if own['queueSongIds']:
                    same = tuple(record['cursor']) == _cursor(own)
                    state = record['originalState'] if same else fact['state']
                    position = record['positionMs'] if same else fact['positionMs']
                    if state in ('playing', 'paused'):
                        await self.player._load(dict(own, positionMs=position), paused=state == 'paused')
        self._save(None)
        self.phase = 'idle'
        self.error = None
        self.source = self.fact = self.generation = None
        return self.status()

    async def stop(self):
        async with self.player.lock:
            return await self._stop(restore=True)

    async def disconnected(self):
        async with self.player.lock:
            if self.active:
                self.phase = 'release_pending'
                if self.watch and self.watch is not asyncio.current_task():
                    self.watch.cancel()
                self.watch = None
                await self._quiet()

    async def reconcile(self):
        async with self.player.lock:
            if self.active:
                # Reconnect with a fresh socket nonce and the journaled pair.
                if self.record['bound'][3] == self.wire.device_session_id:
                    await self._stop(restore=False)
                else:
                    raise PlaybackError('Follow recovery requires its original device session')

    async def close(self):
        if self.active and self.wire.ready:
            async with self.player.lock:
                await self._stop(restore=False)
        else:
            await self.disconnected()
