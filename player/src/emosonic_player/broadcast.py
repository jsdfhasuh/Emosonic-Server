"""Strict Broadcast lifecycle, mirror execution, feedback and original-task recovery."""
import asyncio
import copy
import json
import math
import os
from pathlib import Path
import uuid

from .config import recovery_scope
from .errors import PlaybackError


class BroadcastError(PlaybackError):
    pass


_MIRROR = {
    'broadcast.start', 'broadcast.play', 'broadcast.pause', 'broadcast.seek',
    'broadcast.playItem', 'broadcast.queue.sync', 'broadcast.progress',
    'broadcast.state.sync', 'broadcast.waiting', 'broadcast.resume', 'broadcast.resync',
}
_TERMINAL = {'broadcast.stop', 'broadcast.restore'}
# Only correct repeatable phase error, for less than half a progress interval.
_PROGRESS_DEADBAND_MS = 20
_PROGRESS_SLEW_LIMIT = .04
_PROGRESS_SLEW_SECONDS = .45
_CURSORS = ('playbackContextId', 'authorityClientId', 'authorityDeviceSessionId',
            'epoch', 'version', 'queueRevision', 'controlVersion')


class Broadcast:
    @staticmethod
    def recovery_session(state_dir, server_url, username, client_id):
        """Resume the frozen pair only when this account has an unfinished mirror."""
        if state_dir is None:
            return None
        path = Path(state_dir) / 'broadcast.json'
        if not path.exists():
            return None
        record = json.loads(path.read_text(encoding='utf-8'))
        if (record.get('role') == 'ordinary' and record.get('original')
                and record.get('scope') == recovery_scope(server_url, username, client_id)):
            return record.get('localSession')
        return None

    def __init__(self, player, state_dir=None):
        self.player = player
        self.wire = player.wire
        self.path = Path(state_dir) / 'broadcast.json' if state_dir is not None else None
        self.scope = recovery_scope(getattr(self.wire, 'server_url', ''), getattr(self.wire, 'username', ''),
                                    self.wire.client_id) if self.path else None
        self.record = {}
        if self.path and self.path.exists():
            self.record = json.loads(self.path.read_text(encoding='utf-8'))
            if self.record and (self.record.get('scope') != self.scope or (
                    self.record.get('role') == 'ordinary' and
                    self.record.get('localSession') != self.wire.device_session_id)):
                self.record = {}
        self.pending_event = None
        self.delivery = None
        self.attempts = set()
        self.changed = asyncio.Event()
        self.revision = 0
        self.terminal = False
        self.restored = False
        self.retired = set()
        self._queue_item: tuple[int, str] | None = None
        self.run = None
        self.mirror = {}
        self._previous_progress = None
        self.sequence = 0
        self.error = None
        self._cli_lock = asyncio.Lock()
        self._feedback_lock = asyncio.Lock()

    @property
    def blocks_core(self):
        return self.record.get('role') == 'ordinary' or (
            self.pending_event is not None and bool(self.pending_event['payload'].get('deliveryId')))

    @property
    def occupied(self):
        return bool(self.record or self.pending_event)

    def status(self):
        return {k: self.record[k] for k in (
            'role', 'playbackContextId', 'broadcastId', 'intentId', 'lastApplied'
        ) if k in self.record} | {'revision': self.revision, 'restoring': self.terminal,
                                  'error': self.error} | self.mirror

    def _persist(self):
        if self.path is None:
            return
        if self.record:
            self.record.update(scope=self.scope, localSession=self.wire.device_session_id)
        self.path.parent.mkdir(exist_ok=True, parents=True)
        tmp = self.path.with_name(self.path.name + '.tmp')
        with tmp.open('w', encoding='utf-8') as target:
            json.dump(self.record, target, separators=(',', ':'))
            target.flush()
            os.fsync(target.fileno())
        os.replace(tmp, self.path)

    def _clear(self):
        if self.record.get('broadcastId'):
            self.retired.add(self.record['broadcastId'])
        self.record = {}
        self._persist()
        self.revision = 0
        self.attempts.clear()
        self.terminal = self.restored = False
        self.delivery = self._queue_item = self.run = self.error = None
        self.mirror = {}

    def _replace_delivery(self, key):
        self.changed.set()
        self.changed = asyncio.Event()
        self.delivery = key

    async def disconnected(self):
        self._replace_delivery(None)
        self.retired.clear()
        self.pending_event = None
        self.revision = self.sequence = 0
        self.attempts.clear()
        self._queue_item = self.run = None
        self.mirror = {}
        self.terminal = self.restored = False
        if self.blocks_core:
            async with self.player.lock:
                await self.player._safe_stop()

    async def _context(self, context_id):
        reply = await self.wire.request('playback.context.status',
            {'playbackContextId': context_id}, type='state', reply_actions={'playback.context.status'})
        return reply['payload']

    async def _status(self):
        fields = {k: self.record[k] for k in ('playbackContextId', 'broadcastId')}
        return (await self.wire.request('broadcast.status', fields, type='state'))['payload']

    async def recover(self):
        had_overlay = self.blocks_core
        pending, self.pending_event = self.pending_event, None
        if pending is not None:
            await self.on_event(pending)
        elif self.record.get('broadcastId'):
            try:
                status = await self._status()
            except Exception as exc:
                if str(exc) == 'not_found' and not self.blocks_core:
                    self._clear()
                elif str(exc) == 'not_found' and self.record.get('restoredTerminal'):
                    async with self.player.lock:
                        await self._restore()
                    self._clear()
                else:
                    self.error = 'broadcast_status_unavailable'
            else:
                snap = status.get('broadcast', {})
                if snap.get('lifecycleState') == 'stopped':
                    pair = self._participant(status)
                    if not self.blocks_core:
                        self._clear()
                    elif (self.record.get('restoredTerminal') and pair.get('syncStatus') == 'applied'
                          and pair.get('appliedBroadcastRevision') == snap['broadcastRevision']):
                        async with self.player.lock:
                            await self._restore()
                        self._clear()
                # Ordinary audio requires a provenance-bearing delivery. A status
                # result alone must never start audio or clear a restore fence.
        return self.blocks_core or had_overlay

    def _participant(self, status):
        return next((p for p in status.get('participantStates', [])
                     if p['clientId'] == self.wire.client_id and
                     p['deviceSessionId'] == self.wire.device_session_id), {})

    def _role(self, p):
        if 'terminalBroadcastRevision' in p:
            return 'ordinary' if p.get('deviceSessionId') == self.wire.device_session_id else None
        if (p.get('authorityClientId'), p.get('authorityDeviceSessionId')) == (
                self.wire.client_id, self.wire.device_session_id):
            return 'source'
        if self.wire.client_id in p.get('participants', ()) and p.get('deliveryId'):
            return 'ordinary'
        if p.get('ownerClientId') == self.wire.client_id and not p.get('deliveryId'):
            return 'controller'
        return None

    async def on_event(self, message):
        action, p = message.get('action', ''), message.get('payload', {})
        if not action.startswith('broadcast.'):
            return False
        if message.get('type') != 'event' or action not in _MIRROR | _TERMINAL:
            return True
        role = self._role(p)
        if role is None or not p.get('broadcastId') or p['broadcastId'] in self.retired:
            return True
        if self.record.get('broadcastId') not in (None, p['broadcastId']):
            return True
        if not self.wire.ready:
            previous = (self.pending_event or {}).get('payload', {})
            previous_revision = previous.get('broadcastRevision', previous.get('terminalBroadcastRevision', 0))
            revision = p.get('broadcastRevision', p.get('terminalBroadcastRevision', 0))
            if revision >= previous_revision:
                self.pending_event = message
            return True
        revision = p.get('terminalBroadcastRevision', p.get('broadcastRevision'))
        is_terminal = action in _TERMINAL
        if type(revision) is not int or revision < self.revision or (self.terminal and not is_terminal):
            return True
        if not self.record.get('broadcastId'):
            self.record.update(role=role, playbackContextId=p['playbackContextId'],
                               broadcastId=p['broadcastId'], intentId=p.get('intentId'), lastApplied=0)
        self.record['role'] = role
        if revision > self.revision:
            self.attempts.clear()
        self.revision = revision
        if role != 'ordinary':
            if is_terminal:
                self._clear()
            elif action in {'broadcast.start', 'broadcast.resume'}:
                self._persist()
            return True
        delivery = p.get('deliveryId')
        if not delivery or delivery in self.attempts:
            return True
        if self.attempts and action not in _TERMINAL | {'broadcast.resync'}:
            return True
        self.attempts.add(delivery)
        self._replace_delivery(delivery)
        if action != 'broadcast.progress':
            self._previous_progress = None
        self.terminal = is_terminal
        if is_terminal:
            self.mirror = {}
        generation = self.wire.generation

        def current():
            return self.delivery == delivery and self.wire.generation == generation

        try:
            async with self.player.lock:
                if not current():
                    return True
                if action in {'broadcast.resync', 'broadcast.restore'}:
                    if action == 'broadcast.restore':
                        self.record['lastApplied'] = p['lastAppliedBroadcastRevision']
                    else:
                        status = await self._status()
                        self.record['lastApplied'] = self._participant(status).get('appliedBroadcastRevision', 0)
                    if not current():
                        return True
                if not self.record.get('original'):
                    await self._capture(p, is_terminal)
                if not current():
                    return True
                if is_terminal:
                    if not self.restored:
                        await self._restore()
                        self.restored = True
                        self.record['restoredTerminal'] = revision
                        self._persist()
                    result = self._terminal_proof(p)
                else:
                    result = await self._mirror(p, current, action)
                    if current() and result:
                        self.mirror = {'mirrorTrackId': result['trackId'],
                                       'mirrorContext': {
                                           'playbackContextId': p['playbackContextId'],
                                           'epoch': p.get('sourceEpoch'),
                                           'queueSongIds': list(p['queueSongIds']),
                                           'queueRevision': p.get('sourceQueueRevision'),
                                           'controlVersion': p.get('sourceControlVersion'),
                                           'currentIndex': result['queueIndex'],
                                           'trackId': result['trackId'], 'state': result['state'],
                                       },
                                       'mirrorQueueIndex': result['queueIndex'],
                                       'mirrorState': result['state'],
                                       'mirrorPositionMs': result['positionMs'],
                                       'mirrorPlaybackRate': result['playbackRate']}
        except Exception as exc:
            self.error = str(exc) if isinstance(exc, BroadcastError) else (
                'restore_failed' if is_terminal or not self.record.get('original') else 'execution_failed')
            if current():
                self.mirror = {}
                if self.record.get('original'):
                    async with self.player.lock:
                        if current():
                            await self.player._safe_stop()
                            self._queue_item = self.run = None
                result = {'executionStatus': 'failed', 'failedBroadcastRevision': revision,
                          'lastAppliedBroadcastRevision': self.record.get('lastApplied', 0),
                          'errorCode': self.error}
            else:
                return True
        if current() and result is not None:
            try:
                async with self._feedback_lock:
                    if not current():
                        return True
                    await self._feedback(p, result)
                    if not self.record or self.record['broadcastId'] != p['broadcastId']:
                        return True
                    if result['executionStatus'] == 'applied':
                        self.record['lastApplied'] = revision
                        self.error = None
                        if is_terminal:
                            self._clear()
            except Exception:
                self.error = 'broadcast_feedback_unconfirmed'
        return True

    async def _capture(self, p, terminal):
        context = self.player.executed
        applied = self.player.applied
        if context is None or not self.player._is_ours(context):
            context_id = p.get('suspendedPlaybackContextId')
            if context_id is None:
                listed = await self.wire.request('playback.context.list', {
                    'authorityClientId': self.wire.client_id,
                    'authorityDeviceSessionId': self.wire.device_session_id},
                    type='state', reply_actions={'playback.context.list'})
                bindings = listed['payload']['contexts']
                if len(bindings) != 1:
                    raise BroadcastError('restore_failed')
                context_id = bindings[0]['playbackContextId']
            status = await self._context(context_id)
            context = status['playbackContext']
            applied = context['controlVersion']
        if not self.player._is_ours(context) or applied != context['controlVersion']:
            raise BroadcastError('restore_failed')
        original = copy.deepcopy(context)
        if (self.player.run_id or self.player.announcement) and not terminal:
            sample = await self.player._music_sample()
            if sample.get('position_ms') is not None:
                original['positionMs'] = max(0, round(sample['position_ms']))
            if original['state'] in ('playing', 'paused'):
                original['state'] = 'paused' if sample['paused'] else 'playing'
            original['playbackRate'] = sample.get('speed', 1.0)
        self.record['original'] = original
        try:
            self._persist()
        except OSError:
            self.record.pop('original', None)
            raise BroadcastError('restore_failed') from None
        self.player.run_id = None

    async def _restore(self):
        original = self.record['original']
        status = await self._context(original['playbackContextId'])
        canonical = status['playbackContext']
        if not self.player._is_ours(canonical):
            raise BroadcastError('restore_failed')
        baseline = all(original.get(k) == canonical.get(k) for k in _CURSORS)
        context = copy.deepcopy(original if baseline else canonical)
        state = context['state']
        self.player._interrupt_announcement()
        if state in ('playing', 'paused'):
            await self.player._load(context, paused=True)
            await self.player.engine.set_speed(context.get('playbackRate', 1.0))
            if state == 'playing':
                await self.player.engine.play()
        else:
            await self.player.engine.stop()
            self.player.run_id = None
        self.player.context = canonical
        self.player.executed = context
        self.player.applied = canonical['controlVersion']
        self.run = self._queue_item = None

    @staticmethod
    def _terminal_proof(p):
        return {'executionStatus': 'applied', 'restoreCompleted': True,
                'appliedBroadcastRevision': p.get('terminalBroadcastRevision', p.get('broadcastRevision')),
                'queueIndex': p.get('queueIndex', p.get('currentIndex')), 'trackId': p['trackId'],
                'positionMs': p['positionMs'], 'state': 'stopped', 'playbackRate': p['playbackRate']}

    async def _correct_progress(self, p, sample, drift, stamp):
        rate = p['playbackRate']
        scope = (self.run, self.wire.generation, p['sourceEpoch'], p['sourceControlVersion'], rate)
        previous = self._previous_progress
        self._previous_progress = None
        if (abs(drift) <= _PROGRESS_DEADBAND_MS or sample.get('buffering') or sample.get('paused')
                or sample.get('position_sampled_monotonic') is None or abs(sample['speed'] - rate) > .001):
            return
        self._previous_progress = (scope, drift, stamp)
        if (not previous or previous[0] != scope or drift * previous[1] <= 0
                or not 0 < stamp - previous[2] <= 5000):
            return

        # Move toward the deadband while staying within the supported speed range.
        speed = max(.5, min(2., rate * (1 - math.copysign(_PROGRESS_SLEW_LIMIT, drift))))
        change = abs(speed - rate)
        if change <= .001:
            return
        # Leave at least half of the remaining execution window to restore and verify the rate.
        remaining_ms = p['effectiveAtServerMs'] + 1000 - stamp
        duration = min(_PROGRESS_SLEW_SECONDS, remaining_ms / 2000,
                       (abs(drift) - _PROGRESS_DEADBAND_MS / 2) / (1000 * change))
        if duration * change * 1000 < 1:
            return
        changed = self.changed
        engine = self.player.engine
        await engine.set_speed(speed)
        try:
            await asyncio.wait_for(changed.wait(), duration)
        except asyncio.TimeoutError:
            pass
        finally:
            # Restore under the same player lock, even with no subsequent delivery.
            await engine.set_speed(rate)

    async def _mirror(self, p, current, action):
        rate, idx, song, state = p['playbackRate'], p['currentIndex'], p['trackId'], p['state']
        if type(rate) not in (int, float) or not 0.5 <= rate <= 2.0:
            raise BroadcastError('rate_unsupported')
        if type(idx) is not int or not 0 <= idx < len(p['queueSongIds']) or p['queueSongIds'][idx] != song:
            raise BroadcastError('queue_apply_failed')
        at = p.get('effectiveAtServerMs')
        if p['lifecycleState'] == 'active' and (
                type(at) is not int or type(p.get('serverTimeMs')) is not int or at-p['serverTimeMs'] < 250):
            raise BroadcastError('clock_unsynchronized')
        def now():
            stamp = self.wire.server_time_ms()
            if at is not None and stamp - at > 1000:
                raise BroadcastError('effective_at_missed')
            return stamp
        def position(stamp):
            delta = max(0, stamp - p['serverUpdatedAtMs']) * rate if state == 'playing' else 0
            return round(p['positionMs'] + delta)
        clock = now()
        engine = self.player.engine
        item = (idx, song)
        prepared = None
        self.player._interrupt_announcement()
        if state != 'stopped' and (self._queue_item != item or self.run is None):
            run = uuid.uuid4().hex
            try:
                await engine.load(self.player.media.stream_url(song), run, position_ms=position(at or clock))
            except Exception:
                raise BroadcastError('track_load_failed') from None
            self._queue_item, self.run = item, run
            if current():
                await engine.set_speed(rate)
                prepared = await engine.snapshot()
        if not current():
            return None
        if at is not None:
            delay = (at - now()) / 1000
            if delay > 0:
                try:
                    await asyncio.wait_for(self.changed.wait(), delay)
                    return None
                except asyncio.TimeoutError:
                    pass
        if not current():
            return None
        if state == 'stopped':
            target = position(now())
            await engine.stop()
            self._queue_item = self.run = None
            actual = target
        else:
            sample = prepared if prepared is not None else await engine.snapshot()
            if not current():
                return None
            if sample['run_id'] != self.run:
                raise BroadcastError('execution_failed')
            if state == 'paused' and not sample['paused']:
                await engine.pause()
                sample = await engine.snapshot()
            if not current():
                return None
            stamp = now()
            target = position(stamp)
            observed = sample.get('position_ms')
            if observed is None or not math.isfinite(observed):
                raise BroadcastError('execution_failed')
            sampled_at = sample.get('position_sampled_monotonic')
            if not sample['paused'] and not sample.get('buffering') and sampled_at is not None:
                age = max(0., asyncio.get_running_loop().time() - sampled_at)
                observed += age * 1000 * sample['speed']
            drift = observed - target
            if abs(drift) > (200 if state == 'playing' else 40):
                self._previous_progress = None
                await engine.seek(target)
            elif action == 'broadcast.progress' and state == 'playing':
                await self._correct_progress(p, sample, drift, stamp)
            if not current():
                return None
            if abs(sample['speed'] - rate) > .001:
                await engine.set_speed(rate)
            if not current():
                return None
            now()
            if state == 'playing' and sample['paused']:
                await engine.play()
            sample = await engine.snapshot()
            if (sample['paused'] != (state == 'paused') or sample.get('buffering') or
                    sample.get('position_ms') is None or abs(sample['speed'] - rate) > .001 or
                    sample.get('run_id') != self.run):
                raise BroadcastError('execution_failed')
            actual = max(0, round(sample['position_ms']))
        now()
        return {'executionStatus': 'applied', 'appliedBroadcastRevision': p['broadcastRevision'],
                'queueIndex': idx, 'trackId': song, 'state': state,
                'positionMs': actual, 'playbackRate': rate}

    async def _feedback(self, p, result):
        self.sequence += 1
        fields = {'broadcastId': p['broadcastId'], 'playbackContextId': p['playbackContextId'],
                  'deviceSessionId': self.wire.device_session_id, 'deliveryId': p['deliveryId'],
                  'clientSeq': self.sequence, **result}
        scope = ('playbackContextId', 'broadcastId', 'deviceSessionId', 'deliveryId', 'clientSeq')
        rejected = self.wire.expect_event('broadcast.feedback.rejected',
            lambda m: all(m['payload'].get(k) == fields[k] for k in scope))
        confirmed = asyncio.create_task(self.wire.send_confirmed(
            'broadcast.feedback', fields, 'broadcast.feedback',
            lambda m: m['payload'].get('sourceClientId') == self.wire.client_id and
                all(m['payload'].get(k) == v for k, v in fields.items())))
        try:
            done, _ = await asyncio.wait((rejected, confirmed), return_when=asyncio.FIRST_COMPLETED)
            if rejected in done:
                rejected.result()
                raise BroadcastError('broadcast_feedback_rejected')
            await confirmed
        finally:
            for future in (confirmed, rejected):
                if not future.done():
                    future.cancel()
            await asyncio.gather(confirmed, rejected, return_exceptions=True)

    async def cli(self, action, params):
        async with self._cli_lock:
            return await self._cli(action, params)

    async def _cli(self, action, params):
        if action == 'broadcast-start':
            ctx = params.get('context') or (self.player.context or {}).get('playbackContextId')
            if not ctx:
                raise BroadcastError('context_required')
            members = params.get('participants')
            if members is not None and (not isinstance(members, list) or not members or
                    not all(isinstance(v, str) and v for v in members) or len(set(members)) != len(members)):
                raise BroadcastError('invalid_participants')
            if self.occupied and (
                    not self.record.get('request') or self.record['request'] != [ctx, members]):
                raise BroadcastError('broadcast_busy')
            if not self.record.get('intentId'):
                self.record = {'role': 'starting', 'intentId': uuid.uuid4().hex,
                               'playbackContextId': ctx, 'request': [ctx, members]}
                self._persist()
            if ctx == (self.player.context or {}).get('playbackContextId') and not self.blocks_core:
                async with self.player.lock:
                    if self.player.executed and self.player.executed.get('state') == 'playing':
                        await self.player._report('passive')
            fields = {'playbackContextId': ctx, 'intentId': self.record['intentId']}
            if members is not None:
                fields['participants'] = members
            try:
                ack = (await self.wire.request('broadcast.start', fields))['payload']
            except Exception as exc:
                if str(exc) in {'bad_request', 'conflict', 'capability_required', 'forbidden', 'not_found'}:
                    if self.record.get('role') == 'starting':
                        self._clear()
                raise
            if self.record and self.record.get('intentId') == fields['intentId']:
                self.record['broadcastId'] = ack['broadcastId']
                if self.record['role'] == 'starting':
                    self.record['role'] = ('source' if ctx == (self.player.context or {}).get('playbackContextId')
                                           else 'controller')
                self._persist()
            return ack
        if not self.record.get('broadcastId'):
            if action == 'broadcast-status':
                return self.status()
            raise BroadcastError('no_broadcast')
        if action == 'broadcast-status':
            return await self._status()
        fields = {k: self.record[k] for k in ('playbackContextId', 'broadcastId')}
        if action == 'broadcast-stop':
            return (await self.wire.request('broadcast.stop', fields))['payload']
        commands = {'broadcast-play': 'broadcast.play', 'broadcast-pause': 'broadcast.pause',
                    'broadcast-seek': 'broadcast.seek', 'broadcast-play-item': 'broadcast.playItem'}
        if action not in commands:
            raise BroadcastError('unknown_broadcast_action')
        status = await self._status()
        snap = status['broadcast']
        fields['baseControlVersion'] = snap['sourceControlVersion']
        if action == 'broadcast-seek':
            fields['positionMs'] = params['position_ms']
        if action == 'broadcast-play-item':
            fields.update(queueIndex=params['index'], baseQueueRevision=snap['sourceQueueRevision'])
        return (await self.wire.request(commands[action], fields))['payload']
