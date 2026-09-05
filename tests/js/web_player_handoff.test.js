'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createPage, deferred, prepare, commit } = require('./web_player_handoff_harness');

for (const [label, options, errorCode] of [
  ['success', {}, null], ['busy', { contextId: 'other-context' }, 'target_busy'],
  ['hidden', { hidden: true }, 'page_hidden'], ['autoplay', { gesture: false }, 'autoplay_blocked'],
  ['media', { mediaFailure: true }, 'media_load_failed'], ['restore', { restore: true }, 'restore_in_progress'],
]) {
  test(`actual page ready: ${label}`, async (t) => {
    const page = await createPage(options); t.after(() => page.close());
    page.push('playback.prepare', prepare()); await page.flush();
    const ready = page.requests('playback.ready');
    assert.equal(ready.length, 1);
    assert.equal(ready[0].payload.deviceSessionId, 'web-player-device:target');
    assert.equal(ready[0].payload.ready, !errorCode);
    if (errorCode) assert.equal(ready[0].payload.errorCode, errorCode);
    else assert.equal('errorCode' in ready[0].payload || 'errorMessage' in ready[0].payload, false);
    assert.doesNotMatch(JSON.stringify(ready), /private\/path|stack|secret/);
    if (options.restore) assert.ok(page.store.has('emosonic.webPlayer.broadcastRecovery.v2'));
  });
}

test('actual page complete proves non-default rate and keeps N+1 provisional', async (t) => {
  const page = await createPage(); t.after(() => page.close());
  const prepared = { ...prepare(), playbackRate: 1.5 };
  page.push('playback.prepare', prepared); await page.flush();
  const command = commit(prepared);
  page.client.nextClientSeq();
  page.push('player.play', command); await page.flush();
  assert.notEqual(page.client.cursor(prepared.playbackContextId)?.controlVersion, 6);
  await page.advance(350);
  const proof = page.requests('playback.handoff.complete')[0]?.payload;
  assert.ok(proof);
  assert.equal(proof.deviceSessionId, prepared.deviceSessionId);
  assert.equal(proof.queueIndex, 0); assert.equal(proof.trackId, 'song-1');
  assert.equal(proof.state, 'playing'); assert.equal(page.audio.paused, false);
  assert.equal(proof.playbackRate, 1.5); assert.equal(page.audio.playbackRate, 1.5);
  assert.equal(proof.positionMs, 1150); assert.equal(proof.positionSampledAtServerMs, page.now());
  assert.equal(proof.appliedControlVersion, 6); assert.equal(proof.clientSeq, 2);
  page.push('playback.handoff.status', { playbackContextId: prepared.playbackContextId,
    handoffId: prepared.handoffId, status: 'committing', controlVersion: 6 });
  await page.flush();
  assert.notEqual(page.snapshot().contextId, prepared.playbackContextId);
  assert.notEqual(page.document.getElementById('strict-handoff-state').textContent, 'completed');
  page.push('playback.handoff.status', { playbackContextId: prepared.playbackContextId,
    handoffId: prepared.handoffId, status: 'completed', controlVersion: 6,
    newAuthorityClientId: 'web-player-target', newAuthorityDeviceSessionId: prepared.deviceSessionId });
  await page.flush();
  assert.equal(page.snapshot().contextId, prepared.playbackContextId);
});

for (const event of ['cancel', 'disconnect', 'replacement']) {
  test(`prepare metadata await invalidated by ${event}`, async (t) => {
    const wait = deferred(); const page = await createPage({ metaWait: wait }); t.after(() => page.close());
    page.push('playback.prepare', prepare()); await page.flush();
    if (event === 'disconnect') page.client.disconnect();
    else if (event === 'replacement') page.push('playback.prepare', { ...prepare(), handoffId: 'new', prepareId: 'new-p' });
    else page.push('playback.handoff.cancel', { playbackContextId: 'ctx-handoff', handoffId: 'handoff-1', reason: 'user', controlVersion: 5 });
    wait.resolve(); await page.flush();
    assert.equal(page.requests('playback.ready').filter((m) => m.payload.handoffId === 'handoff-1').length, 0);
  });
}

for (const event of ['cancel', 'disconnect']) {
  test(`scheduled commit invalidated by ${event}`, async (t) => {
    const page = await createPage(); t.after(() => page.close());
    page.push('playback.prepare', prepare()); await page.flush();
    page.push('player.play', commit(prepare())); await page.flush();
    if (event === 'disconnect') page.client.disconnect();
    else page.push('playback.handoff.cancel', { playbackContextId: 'ctx-handoff', handoffId: 'handoff-1', reason: 'user', controlVersion: 5 });
    await page.advance(250);
    assert.equal(page.audio.playCalls, 0);
    assert.equal(page.requests('playback.handoff.complete').length, 0);
    assert.equal(page.audio.paused, true);
  });
  test(`pending play cannot escape ${event}`, async (t) => {
    const wait = deferred(); const page = await createPage({ playWait: wait }); t.after(() => page.close());
    page.push('playback.prepare', prepare()); await page.flush();
    page.push('player.play', commit(prepare())); await page.flush(); await page.advance(250);
    if (event === 'disconnect') page.client.disconnect();
    else page.push('playback.handoff.cancel', { playbackContextId: 'ctx-handoff', handoffId: 'handoff-1', reason: 'user', controlVersion: 5 });
    wait.resolve(); await page.flush();
    assert.equal(page.requests('playback.handoff.complete').length, 0);
    assert.equal(page.audio.paused, true);
  });
}

for (const [label, options, change] of [
  ['play rejected', { playFailure: true }, {}], ['wrong version', {}, { controlVersion: 7 }],
  ['bad lead', {}, { effectiveAtServerMs: 1780000000200 }],
  ['wrong source', {}, { sourceClientId: 'other' }], ['rate changed', {}, { playbackRate: 1.5 }],
  ['duration exceeded', { duration: 1 }, {}],
]) {
  test(`commit failure cleanup: ${label}`, async (t) => {
    const page = await createPage(options); t.after(() => page.close());
    page.push('playback.prepare', prepare()); await page.flush();
    page.push('player.play', { ...commit(prepare()), ...change }); await page.flush(); await page.advance(250);
    assert.equal(page.requests('playback.handoff.complete').length, 0);
    const failure = page.requests('playback.handoff.cancel')[0]?.payload;
    assert.ok(failure); assert.equal(failure.reason, 'commit_failed'); assert.equal(failure.errorCode, 'commit_failed');
    assert.doesNotMatch(failure.errorMessage || '', /private\/path|stack|secret/);
    assert.equal(page.audio.paused, true);
  });
}

test('duplicate commit executes once', async (t) => {
  const page = await createPage(); t.after(() => page.close());
  page.push('playback.prepare', prepare()); await page.flush();
  page.push('player.play', commit(prepare())); page.push('player.play', commit(prepare()));
  await page.flush(); await page.advance(250);
  assert.equal(page.requests('playback.handoff.complete').length, 1);
  assert.equal(page.audio.playCalls, 1);
});

test('wrong prepare device cannot load media or emit ready', async (t) => {
  const page = await createPage(); t.after(() => page.close());
  page.push('playback.prepare', { ...prepare(), deviceSessionId: 'different-device' });
  await page.flush();
  assert.equal(page.requests('playback.ready').length, 0);
  assert.equal(page.audio.src, undefined);
});

for (const event of ['cancel', 'disconnect', 'timeout']) {
  test(`late media callback cannot escape ${event}`, async (t) => {
    const page = await createPage({ readyState: 0 }); t.after(() => page.close());
    page.push('playback.prepare', prepare()); await page.flush();
    if (event === 'disconnect') page.client.disconnect();
    else if (event === 'timeout') await page.advance(8000);
    else page.push('playback.handoff.cancel', { playbackContextId: 'ctx-handoff', handoffId: 'handoff-1', reason: 'user', controlVersion: 5 });
    page.audio.readyState = 4; page.audio.dispatch('canplay'); page.audio.dispatch('loadedmetadata');
    await page.flush();
    assert.equal(page.requests('playback.ready').filter((m) => m.payload.ready).length, 0);
    assert.equal(page.snapshot().pendingPrepareId, null);
    assert.equal(page.audio.listeners.get('canplay')?.size || 0, 0);
    assert.equal(page.audio.listeners.get('error')?.size || 0, 0);
  });
}

test('delayed metadata cannot send ready after prepare deadline', async (t) => {
  const wait = deferred(); const page = await createPage({ metaWait: wait }); t.after(() => page.close());
  page.push('playback.prepare', prepare()); await page.flush(); await page.advance(8000);
  wait.resolve(); await page.flush();
  assert.equal(page.requests('playback.ready').filter((m) => m.payload.ready).length, 0);
  assert.equal(page.snapshot().pendingPrepareId, null);
  assert.equal(page.audio.src, undefined);
});

test('late commit, stale clock and actual rate mismatch cannot report success', async (t) => {
  for (const reason of ['late', 'clock', 'rate', 'track', 'paused']) {
    const wait = deferred(); const page = await createPage({ playWait: wait }); t.after(() => page.close());
    page.push('playback.prepare', prepare()); await page.flush();
    if (reason === 'clock') page.client.clockSamples = [];
    page.push('player.play', commit(prepare())); await page.flush();
    await page.advance(reason === 'late' ? 1251 : 250);
    if (reason === 'rate') page.audio.playbackRate = 1.25;
    if (reason === 'track') page.audio.dataset.trackId = 'wrong';
    if (reason === 'paused') page.audio.addEventListener('play', () => page.audio.pause());
    wait.resolve(); await page.flush();
    assert.equal(page.requests('playback.handoff.complete').length, 0, reason);
    assert.equal(page.requests('playback.handoff.cancel').length, 1, reason);
    assert.equal(page.audio.paused, true, reason);
  }
});

test('commit timeout stops provisional audio without canonical promotion', async (t) => {
  const page = await createPage(); t.after(() => page.close());
  page.push('playback.prepare', prepare()); await page.flush();
  page.push('player.play', commit(prepare())); await page.flush(); await page.advance(250);
  assert.equal(page.requests('playback.handoff.complete').length, 1);
  await page.advance(4750);
  assert.equal(page.audio.paused, true);
  assert.equal(page.requests('playback.handoff.cancel').length, 1);
  assert.notEqual(page.snapshot().contextId, 'ctx-handoff');
});

test('unrelated cancel and forged completion cannot settle current handoff', async (t) => {
  const page = await createPage(); t.after(() => page.close());
  page.push('playback.prepare', prepare()); await page.flush();
  page.push('playback.handoff.cancel', { playbackContextId: 'old', handoffId: 'old', reason: 'user', controlVersion: 5 });
  assert.equal(page.snapshot().pendingPrepareId, 'prepare-1');
  page.push('player.play', commit(prepare())); await page.flush(); await page.advance(250);
  page.push('playback.handoff.status', { playbackContextId: 'ctx-handoff', handoffId: 'handoff-1',
    status: 'completed', controlVersion: 6, newAuthorityClientId: 'other', newAuthorityDeviceSessionId: 'other' });
  await page.flush();
  assert.equal(page.snapshot().pendingPrepareId, 'prepare-1');
  assert.notEqual(page.snapshot().contextId, 'ctx-handoff');
});

test('new prepare waits for invalidated pending play before touching media', async (t) => {
  const wait = deferred(); const page = await createPage({ playWait: wait }); t.after(() => page.close());
  page.push('playback.prepare', prepare()); await page.flush();
  page.push('player.play', commit(prepare())); await page.flush(); await page.advance(250);
  const next = { ...prepare(), handoffId: 'next-handoff', prepareId: 'next-prepare', queueSongIds: ['new-track'], trackId: 'new-track' };
  page.push('playback.prepare', next); await page.flush();
  assert.notEqual(page.audio.dataset.trackId, 'new-track');
  wait.resolve(); await page.flush();
  assert.equal(page.audio.dataset.trackId, 'new-track'); assert.equal(page.audio.paused, true);
  assert.equal(page.requests('playback.handoff.complete').length, 0);
  assert.equal(page.requests('playback.ready').filter((m) => m.payload.handoffId === 'next-handoff').length, 1);
});

test('Handoff target UI and media events cannot leak ordinary playback feedback', async (t) => {
  const page = await createPage(); t.after(() => page.close());
  page.push('playback.prepare', prepare()); await page.flush();
  for (const id of ['strict-play', 'strict-next', 'strict-prev', 'strict-pause', 'strict-clear-queue']) {
    assert.equal(page.document.getElementById(id).disabled, true);
    page.document.getElementById(id).dispatch('click');
  }
  page.push('player.play', commit(prepare())); await page.flush(); await page.advance(250);
  assert.equal(page.audio.playCalls, 1);
  assert.equal(page.requests('playback.update').length, 0);
  assert.equal(page.requests('queue.context.sync').length, 0);
});

test('changed frozen prepare cannot replace the accepted task', async (t) => {
  const page = await createPage(); t.after(() => page.close());
  page.push('playback.prepare', prepare()); await page.flush();
  page.push('playback.prepare', { ...prepare(), prepareId: 'different', positionMs: 8000 }); await page.flush();
  assert.equal(page.snapshot().pendingPrepareId, 'prepare-1');
  assert.equal(page.requests('playback.ready').length, 1);
  assert.equal(page.audio.currentTime, 1);
});

test('canonical source change invalidates scheduled commit', async (t) => {
  const page = await createPage(); t.after(() => page.close());
  page.push('playback.prepare', prepare()); await page.flush();
  page.push('player.play', commit(prepare())); await page.flush();
  page.push('playback.context.status', { playbackContext: {
    playbackContextId: 'ctx-handoff', authorityClientId: 'web-player-source',
    authorityDeviceSessionId: 'web-player-device:source', epoch: 1, version: 6,
    controlVersion: 6, queueRevision: 1, queueSongIds: ['song-1', 'song-2'], currentIndex: 0,
    state: 'paused', positionMs: 1000,
  }, deviceStates: [] });
  await page.advance(250);
  assert.equal(page.audio.playCalls, 0);
  assert.equal(page.requests('playback.handoff.complete').length, 0);
});
