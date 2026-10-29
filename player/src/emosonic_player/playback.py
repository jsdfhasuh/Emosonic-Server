"""Single authority executor shared by App controls and the local CLI.

Canonical messages are observed outside the execution lock so acknowledgements
can settle an in-flight operation. Audio executions remain strictly serialized.
"""
import asyncio
import copy
import math
import uuid


class PlaybackError(RuntimeError):
    pass


class Player:
    def __init__(self, wire, engine, media):
        self.wire, self.engine, self.media = wire, engine, media
        self.context = None
        self.executed = None
        self.applied = 0
        self.sequence = 0
        self.volume_sequence = 0
        self.run_id = None
        self.lock = asyncio.Lock()
        self.commands = asyncio.Queue(maxsize=128)
        self.invalidated = set()
        self.error = None
        self._tasks = []
        self.last_sample = None
        self.last_sample_time = None
        self.needs_engine_restart = False

    async def start(self):
        self._tasks = [asyncio.create_task(self._commands()), asyncio.create_task(self._engine_events()),
                       asyncio.create_task(self._progress())]

    async def close(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.engine.close()

    async def _safe_stop(self):
        try:
            await self.engine.stop()
        except Exception:
            # close() terminates the owned process even after IPC loss.
            await self.engine.close()
            self.needs_engine_restart = True

    def _is_ours(self, context):
        return (context.get('authorityClientId') == self.wire.client_id and
                context.get('authorityDeviceSessionId') == self.wire.device_session_id)

    async def reconcile(self):
        """New physical connection: establish idle/paused authority, never replay."""
        async with self.lock:
            await self._safe_stop()
            self.run_id = None
            self.applied = self.sequence = self.volume_sequence = 0
            self.invalidated.clear()
            response = await self.wire.request('playback.context.ensure', {
                'deviceSessionId': self.wire.device_session_id, 'queueSongIds': [],
                'positionMs': 0, 'state': 'idle'}, reply_actions={'playback.context.ensure'})
            context = response['payload']
            if not self._is_ours(context):
                raise PlaybackError('authority mismatch during reconciliation')
            self.context = copy.deepcopy(context)
            self.executed = copy.deepcopy(context)
            self.applied = context['controlVersion']
            # Existing same-instance context may contain a queue: load paused,
            # then canonical pause before permitting user commands.
            if context.get('queueSongIds'):
                await self._load(context, paused=True)
                await self._report('passive', state='paused')
            self.error = None

    async def disconnected(self):
        self.run_id = None
        self.error = 'server_disconnected'
        await self._safe_stop()
        while not self.commands.empty():
            try:
                self.commands.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def on_event(self, message):
        action = message.get('action')
        p = message.get('payload', {})
        if action in ('queue.context.sync', 'playback.context.status', 'playback.context.ensure'):
            context = p.get('playbackContext', p)
            if not self._is_ours(context):
                if self.context and context.get('playbackContextId') == self.context['playbackContextId']:
                    self.run_id = None
                    self.error = 'authority_changed'
                    self.context = copy.deepcopy(context)
                    self.executed = None
                    await self._safe_stop()
                return
            if self.context and context['playbackContextId'] == self.context['playbackContextId']:
                if context.get('version', 0) < self.context.get('version', 0):
                    return
                if context.get('epoch') != self.context.get('epoch'):
                    self.run_id = None
                    self.executed = None
                    self.error = 'context_epoch_changed'
                    await self._safe_stop()
            self.context = copy.deepcopy(context)
        elif action == 'playback.context.closed':
            if self.context and p.get('playbackContextId') == self.context['playbackContextId']:
                self.run_id = None
                self.executed = None
                self.error = 'context_closed'
                await self._safe_stop()
        elif action == 'playback.control.settled':
            version = p.get('commandControlVersion')
            self.invalidated.add((p.get('playbackContextId'), p.get('epoch'), version))
            if (self.context and p.get('playbackContextId') == self.context['playbackContextId']
                    and p.get('epoch') == self.context['epoch']
                    and version > self.applied):
                self.run_id = None
                self.error = 'execution_settled'
                await self._safe_stop()
        elif message.get('type') == 'command':
            if action in ('player.play', 'player.pause', 'player.seek', 'player.next',
                          'player.prev', 'queue.playItem', 'device.setVolume', 'playback.context.prepare'):
                try:
                    self.commands.put_nowait((self.wire.generation, copy.deepcopy(message)))
                except asyncio.QueueFull:
                    await self.disconnected()
                    await self.wire.close()

    async def _commands(self):
        while True:
            generation, message = await self.commands.get()
            try:
                async with self.lock:
                    if generation != self.wire.generation or not self.wire.ready:
                        continue
                    await self._execute(message, generation)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error = 'execution_failed'
                self.run_id = None
                await self._safe_stop()

    async def _execute(self, message, generation):
        action, p = message['action'], message['payload']
        if action == 'playback.context.prepare':
            await self._prepare_context(p)
            return
        if action == 'device.setVolume':
            await self.engine.set_volume(p['volume'])
            self.volume_sequence += 1
            actual = (await self.engine.snapshot())['volume']
            if actual is None or not math.isfinite(actual) or abs(actual - p['volume']) > .1:
                raise PlaybackError('volume confirmation mismatch')
            seq = self.volume_sequence
            await self.wire.send_confirmed('device.volume.update', {
                'deviceSessionId': self.wire.device_session_id,
                'volume': round(actual), 'clientSeq': seq},
                'device.volume.update', lambda m: (
                    m.get('payload', {}).get('deviceSessionId') == self.wire.device_session_id and
                    m['payload'].get('sourceClientId') == self.wire.client_id and
                    m['payload'].get('clientSeq') == seq and
                    m['payload'].get('volume') == round(actual)))
            return
        if not self.executed or not self.context or not self._is_ours(self.context):
            raise PlaybackError('no confirmed authority')
        if p['playbackContextId'] != self.executed['playbackContextId']:
            raise PlaybackError('different context')
        version = p['controlVersion']
        key = (p['playbackContextId'], self.executed['epoch'], version)
        if version <= self.applied or key in self.invalidated:
            return
        if p.get('dependsOnControlVersion', self.applied) != self.applied or version != self.applied + 1:
            raise PlaybackError('control dependency is not committed')
        target = copy.deepcopy(self.executed)
        if action == 'queue.playItem':
            target.update(queueSongIds=p['queueSongIds'], currentIndex=p['queueIndex'],
                          queueRevision=p['queueRevision'], positionMs=0, state='playing')
        elif action in ('player.next', 'player.prev'):
            old = target['currentIndex']
            new = old + (1 if action == 'player.next' else -1)
            target['currentIndex'] = max(0, min(new, len(target['queueSongIds']) - 1))
            target['positionMs'] = 0
            target['state'] = 'stopped' if new >= len(target['queueSongIds']) else 'playing'
        else:
            target['positionMs'] = p.get('positionMs', target.get('positionMs', 0))
            if action == 'player.play':
                target['state'] = 'playing'
            elif action == 'player.pause':
                target['state'] = 'paused'
        target['controlVersion'] = version
        previous = self.executed
        try:
            async with asyncio.timeout(min(30, max(.1, p['executionTimeoutMs'] / 1000))):
                if target['state'] == 'stopped':
                    self.run_id = None
                    await self._safe_stop()
                elif (not self.run_id or action in ('player.next', 'player.prev', 'queue.playItem')):
                    await self._load(target, paused=target['state'] != 'playing')
                else:
                    if action == 'player.seek':
                        await self.engine.seek(target['positionMs'])
                    elif action == 'player.play':
                        await self.engine.play()
                    else:
                        await self.engine.pause()
                if generation != self.wire.generation or key in self.invalidated:
                    raise PlaybackError('execution invalidated')
                self.executed = target
                await self._report('remoteCommand', command_version=version,
                                   state=target['state'])
                if generation != self.wire.generation or key in self.invalidated:
                    raise PlaybackError('confirmation invalidated')
                self.applied = version
                self.error = None
        except Exception:
            self.run_id = None
            await self._safe_stop()
            self.executed = previous
            if generation == self.wire.generation and self.wire.ready and key not in self.invalidated:
                try:
                    await self._report('remoteCommand', command_version=version,
                                       state='paused' if previous.get('queueSongIds') else 'idle',
                                       failed=True)
                except Exception:
                    pass
            raise

    async def _prepare_context(self, payload):
        # Core idle-context initialization is NOT Handoff playback.prepare.
        context = self.context
        if (not context or not self._is_ours(context) or
                payload['playbackContextId'] != context['playbackContextId'] or
                payload['controlVersion'] != context['controlVersion']):
            raise PlaybackError('prepare context/version mismatch')
        try:
            if not context.get('queueSongIds'):
                queue = payload.get('initialQueueSongIds', [])
                if not queue or len(queue) != len(set(queue)):
                    raise PlaybackError('valid initial queue required')
                await self._sync_queue(queue, payload['currentIndex'], payload.get('positionMs', 0))
                # Canonical queue initialization automatically settles Core prepare.
                # Do not start audio until a subsequent routed player command.
            else:
                await self.wire.send_confirmed('playback.context.prepared', {
                    'playbackContextId': context['playbackContextId'],
                    'deviceSessionId': self.wire.device_session_id,
                    'intentId': payload['intentId'], 'ready': True},
                    'playback.context.prepared', lambda m: (
                        m.get('payload', {}).get('intentId') == payload['intentId']))
        except Exception:
            await self._safe_stop()
            await self.wire.send_confirmed('playback.context.prepared', {
                'playbackContextId': context['playbackContextId'],
                'deviceSessionId': self.wire.device_session_id,
                'intentId': payload['intentId'], 'ready': False,
                'errorCode': 'queue_required' if not payload.get('initialQueueSongIds') else 'restore_failed'}, 'playback.context.prepared',
                lambda m: m.get('payload', {}).get('intentId') == payload['intentId'])
            raise

    async def _load(self, context, paused):
        queue = context['queueSongIds']
        index = context['currentIndex']
        run = uuid.uuid4().hex
        self.run_id = None
        await self.engine.load(self.media.stream_url(queue[index]), run,
                               position_ms=context.get('positionMs', 0))
        self.run_id = run
        if not paused:
            await self.engine.play()

    async def _report(self, origin, command_version=None, state=None, failed=False):
        context = self.executed
        if not context:
            raise PlaybackError('missing execution context')
        try:
            sample = await self.engine.snapshot()
            self.last_sample = sample
            self.last_sample_time = self.wire.server_time_ms()
        except Exception:
            if not failed and state != 'stopped':
                raise
            sample = self.last_sample or {'position_ms': context.get('positionMs', 0), 'paused': True, 'buffering': False}
        self.sequence += 1
        state = state or ('paused' if sample['paused'] or sample['buffering'] else 'playing')
        position = sample.get('position_ms')
        if position is None:
            if state == 'playing':
                raise PlaybackError('no actual position sample')
            position = context.get('positionMs', 0)
        payload = {'playbackContextId': context['playbackContextId'],
                   'deviceSessionId': self.wire.device_session_id, 'origin': origin,
                   'state': state, 'positionMs': round(position) if state != 'idle' else 0,
                   'positionSampledAtServerMs': self.last_sample_time or self.wire.server_time_ms(),
                   'playbackRate': 1.0, 'clientSeq': self.sequence,
                   'appliedControlVersion': self.applied}
        if state != 'idle':
            payload['trackId'] = context['queueSongIds'][context['currentIndex']]
        if origin == 'remoteCommand':
            payload.update(executionStatus='failed' if failed else 'committed',
                           commandControlVersion=command_version,
                           appliedControlVersion=self.applied if failed else command_version)
            if failed:
                payload['errorCode'] = 'playback_failed'
        expected_seq = self.sequence
        confirmation = await self.wire.send_confirmed('playback.update', payload,
            'playback.update', lambda m: (
                m.get('payload', {}).get('playbackContextId') == context['playbackContextId'] and
                m['payload'].get('deviceSessionId') == self.wire.device_session_id and
                m['payload'].get('clientSeq') == expected_seq and
                (m['payload'].get('commandControlVersion') == command_version and
                 m['payload'].get('executionStatus') == 'failed' if failed else
                 m['payload'].get('appliedControlVersion') == payload['appliedControlVersion'])))
        proof = confirmation['payload']
        # The confirmation continues the SAME audio run. Do not replace run_id.
        # Fetch canonical cursors; concurrent later accepted controls may be ahead
        # of this applied cursor and must not replace the executed snapshot.
        response = await self.wire.request('playback.context.status',
            {'playbackContextId': context['playbackContextId']}, type='state',
            reply_actions={'playback.context.status'})
        canonical = response['payload']['playbackContext']
        if not self._is_ours(canonical) or canonical['epoch'] != context['epoch']:
            raise PlaybackError('confirmation authority changed')
        self.context = copy.deepcopy(canonical)
        if failed:
            self.applied = proof['appliedControlVersion']
            self.executed = copy.deepcopy(canonical)
            self.executed['state'] = proof['state']
            self.executed['positionMs'] = proof['positionMs']
        else:
            self.executed['positionMs'] = proof['positionMs']
            self.executed['state'] = proof['state']
            if canonical['controlVersion'] == payload['appliedControlVersion']:
                self.executed.update(copy.deepcopy(canonical))


    async def _engine_events(self):
        while True:
            event = await self.engine.events.get()
            try:
                async with self.lock:
                    if event.run_id != self.run_id or not self.run_id:
                        continue
                    if event.kind != 'eof':
                        self.needs_engine_restart = True
                        self.error = 'audio_unavailable'
                        self.run_id = None
                        await self._safe_stop()
                        if self.wire.ready and self.executed:
                            await self._report('passive', state='stopped')
                        continue
                    self.run_id = None  # consume once, including duplicate song IDs
                    if not self.wire.ready or not self.context or self.context['controlVersion'] != self.applied:
                        continue
                    queue = self.executed['queueSongIds']
                    next_index = self.executed['currentIndex'] + 1
                    if next_index == len(queue):
                        await self._report('passive', state='stopped')
                        continue
                    await self._sync_queue(queue, next_index)
                    await self._load(self.executed, paused=False)
                    await self._report('passive', state='playing')
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error = 'queue_advance_failed'
                await self._safe_stop()

    async def _sync_queue(self, queue, index, position_ms=0):
        context = copy.deepcopy(self.context)
        expected_revision = context['queueRevision'] + 1
        payload = {
            'playbackContextId': context['playbackContextId'],
            'deviceSessionId': self.wire.device_session_id,
            'queueSongIds': queue, 'currentIndex': index, 'positionMs': position_ms,
            'positionSampledAtServerMs': self.wire.server_time_ms(),
            'baseQueueRevision': context['queueRevision'],
            'baseControlVersion': context['controlVersion']}
        result = await self.wire.send_confirmed('queue.context.sync', payload,
            'queue.context.sync', lambda m: (
                m.get('payload', {}).get('playbackContextId') == context['playbackContextId'] and
                m['payload'].get('queueRevision') == expected_revision and
                m['payload'].get('queueSongIds') == queue and
                m['payload'].get('currentIndex') == index), type='state')
        canonical = result['payload']
        if not self._is_ours(canonical) or canonical['epoch'] != context['epoch']:
            raise PlaybackError('queue authority changed')
        self.context = copy.deepcopy(canonical)
        self.executed = copy.deepcopy(canonical)
        self.applied = canonical['controlVersion']


    async def _progress(self):
        while True:
            await asyncio.sleep(2)
            if self.lock.locked() or not self.run_id or not self.wire.ready:
                continue
            try:
                async with self.lock:
                    await self._report('passive')
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error = 'state_confirmation_failed'
                self.run_id = None
                await self._safe_stop()

    async def cli(self, action, params):
        if action == 'status':
            return {'connected': self.wire.ready, 'context': self.context,
                    'appliedControlVersion': self.applied, 'error': self.error,
                    'audio': await self.engine.snapshot()}
        if action == 'queue':
            return {'songs': (self.context or {}).get('queueSongIds', []),
                    'index': (self.context or {}).get('currentIndex')}
        if not self.wire.ready:
            raise PlaybackError('server not connected')
        if action == 'search':
            return await self.media.search(params['query'])
        if action == 'playlists':
            return await self.media.playlists()
        if action == 'play':
            if 'playlist' in params:
                queue = await self.media.playlist(params['playlist'])
            elif 'album' in params:
                queue = await self.media.album(params['album'])
            else:
                queue = [params['song']]
            if not queue:
                raise PlaybackError('empty selection')
            if len(queue) != len(set(queue)):
                raise PlaybackError('current server contract rejects duplicate song IDs')
            async with self.lock:
                self.run_id = None
                await self._safe_stop()
                await self._sync_queue(queue, 0)
            # A routed queue.playItem establishes the execution transaction.
            return await self._control('queue.playItem', queueIndex=0,
                                       baseQueueRevision=self.context['queueRevision'])
        if action in ('pause', 'resume', 'next', 'prev', 'seek'):
            extra = {}
            if action == 'seek':
                value = params['seconds']
                if isinstance(value, str) and value.startswith(('+', '-')):
                    sample = await self.engine.snapshot()
                    value = (sample.get('position_ms') or 0) / 1000 + float(value)
                extra['positionMs'] = max(0, round(float(value) * 1000))
            return await self._control('player.' + ('play' if action == 'resume' else action), **extra)
        if action == 'volume':
            volume = float(params['value'])
            if not math.isfinite(volume) or not volume.is_integer() or not 0 <= volume <= 100:
                raise PlaybackError('volume outside 0..100')
            await self.wire.request('device.setVolume', {'targetClientId': self.wire.client_id,
                'targetDeviceSessionId': self.wire.device_session_id, 'volume': int(volume)})
            return {'accepted': True}
        if action == 'stop':
            # Protocol has no player.stop. Closing context is the canonical stop.
            context = self.context
            await self.wire.request('playback.context.close', {
                'playbackContextId': context['playbackContextId'], 'expectedEpoch': context['epoch'],
                'baseVersion': context['version']})
            await self._safe_stop()
            self.run_id = None
            await self.reconcile()
            return {'stopped': True}
        raise PlaybackError('unsupported CLI action')

    async def _control(self, action, **extra):
        context = self.context
        if not context or not context.get('queueSongIds'):
            raise PlaybackError('queue is empty')
        await self.wire.request(action, {'playbackContextId': context['playbackContextId'],
                               'baseControlVersion': context['controlVersion'], **extra})
        return {'accepted': True, 'note': 'use status to observe committed execution'}
