'use strict';

// Run the complete production page script and shared request/message handling.
// DOM, media, time, ownership and network bootstrap use deterministic doubles;
// the page's Handoff builders and execution functions are unmodified.
const fs = require('node:fs/promises');
const path = require('node:path');
const vm = require('node:vm');
const ROOT = path.resolve(__dirname, '../..');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function createPage(options = {}) {
  let now = options.now ?? 1780000000000;
  let timerId = 0;
  const timers = new Map();
  const sent = [];
  const nodes = new Map();
  class Element {
    constructor() {
      this.dataset = {};
      this.listeners = new Map();
      this.classList = { add() {}, remove() {} };
      this.value = '';
      this.textContent = '';
    }
    addEventListener(name, fn, settings = {}) {
      if (!this.listeners.has(name)) this.listeners.set(name, new Map());
      this.listeners.get(name).set(fn, settings.once);
    }
    removeEventListener(name, fn) { this.listeners.get(name)?.delete(fn); }
    dispatch(name) {
      for (const [fn, once] of [...(this.listeners.get(name) || [])]) {
        if (once) this.removeEventListener(name, fn);
        fn({ target: this });
      }
    }
    append() {}
    getAttribute(key) { return this[key] || null; }
    removeAttribute(key) { delete this[key]; }
  }
  const audio = new Element();
  Object.assign(audio, {
    readyState: options.readyState ?? 4, currentTime: 0, duration: options.duration ?? 120,
    playbackRate: 1, volume: .7, paused: true, ended: false, playCalls: 0,
    load() {},
    pause() { this.paused = true; this.dispatch('pause'); },
    async play() {
      this.playCalls += 1;
      if (options.playWait) await options.playWait.promise;
      if (options.playFailure) throw new Error('private/path secret stack');
      this.paused = false;
      this.dispatch('play');
    },
  });
  Object.defineProperty(audio, 'currentSrc', { get() { return this.src || ''; } });
  nodes.set('strict-player-audio', audio);
  const document = {
    visibilityState: options.hidden ? 'hidden' : 'visible',
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, new Element());
      return nodes.get(id);
    },
    createElement() { return new Element(); }, createTextNode(value) { return value; },
  };
  document.getElementById('strict-player-root').dataset = { metaUrl: '/meta', searchUrl: '/search' };
  const store = new Map([
    ['emosonic.webPlayer.clientId.v2', options.clientId || 'web-player-target'],
    ['emosonic.webPlayer.deviceSessionId.v2', options.deviceSessionId || 'web-player-device:target'],
  ]);
  if (options.contextId) store.set('emosonic.webPlayer.playbackContextId.v2', options.contextId);
  if (options.restore) store.set('emosonic.webPlayer.broadcastRecovery.v2', JSON.stringify({
    broadcastId: 'old-broadcast', playbackContextId: 'broadcast-context', restorePending: true,
  }));
  const localStorage = {
    getItem: (key) => store.get(key) || null,
    setItem: (key, value) => store.set(key, value), removeItem: (key) => store.delete(key),
  };
  const context = vm.createContext({
    console, document, localStorage, sessionStorage: localStorage, URL, URLSearchParams,
    Date: class extends Date { static now() { return now; } },
    performance: { now: () => now },
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, { fn, at: now + delay }); return id; },
    clearTimeout(id) { timers.delete(id); }, setInterval() { return ++timerId; }, clearInterval() {},
    fetch: async (url) => {
      if (String(url).startsWith('/search')) return { ok: true, json: async () => ({ tracks: [] }) };
      if (options.metaWait) await options.metaWait.promise;
      if (options.mediaFailure) return { ok: false, status: 404 };
      return { ok: true, json: async () => Object.fromEntries(
        new URL(String(url), 'http://localhost').searchParams.getAll('ids')
          .map((id) => [id, { streamUrl: `http://localhost/media/${id}`, durationMs: 120000 }]),
      ) };
    },
  });
  context.window = context;
  context.location = { origin: 'http://localhost', href: 'http://localhost/player' };
  context.addEventListener = () => {};
  await vm.runInContext(await fs.readFile(path.join(ROOT, 'supysonic/static/js/emo_strict_v2_client.js'), 'utf8'), context);
  let client;
  const RealClient = context.EmoStrictV2.StrictV2Client;
  context.EmoStrictV2.PlayerOwnerLock = class { async acquire() { return true; } release() {} };
  context.EmoStrictV2.StrictV2Client = class extends RealClient {
    connect() {
      client = this;
      this.state = 'ready';
      this.connectionNonce = 'test-physical-nonce';
      this.connectionEpoch = 1;
      this.negotiatedCapabilities = this.registration.capabilities;
      this.socket = {
        connected: true,
        disconnect: () => { this.socket.connected = false; this._onDisconnect('test'); },
        emit: (_name, envelope) => {
          sent.push(JSON.parse(JSON.stringify(envelope)));
          queueMicrotask(() => {
            if (!this.socket.connected) return;
            if (envelope.action === 'system.ping') {
              this._onMessage({ action: 'system.pong', requestId: envelope.requestId,
                connectionNonce: this.connectionNonce, connectionEpoch: 1, payload: { serverTimeMs: now } });
            } else if (envelope.action === 'playback.ready' && options.confirmReady !== false) {
              push('playback.handoff.status', {
                playbackContextId: envelope.payload.playbackContextId,
                handoffId: envelope.payload.handoffId,
                status: envelope.payload.ready ? 'ready' : 'failed', controlVersion: 5,
                ...(envelope.payload.ready ? {} : { errorCode: envelope.payload.errorCode }),
              });
            }
          });
        },
      };
      for (let i = 0; i < 3; i += 1) this.request('system.ping', {}).catch(() => {});
      return this;
    }
  };
  const html = await fs.readFile(path.join(ROOT, 'supysonic/templates/player_strict_v2.html'), 'utf8');
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  if (scripts.length !== 1) throw new Error('Expected one complete inline player script');
  const script = scripts[0][1].replace('{{ emo_web_bootstrap | tojson }}', JSON.stringify({
    strictV2Profiles: { handoff: true, broadcast: true }, acceptanceMode: true,
  }));
  vm.runInContext(script, context, { filename: 'player_strict_v2.html' });
  async function flush() { for (let i = 0; i < 40; i += 1) await Promise.resolve(); }
  function push(action, payload) {
    client._onMessage({ type: 'command', action, payload,
      connectionNonce: client.connectionNonce, connectionEpoch: client.connectionEpoch });
  }
  async function advance(ms) {
    now += ms;
    for (const [id, timer] of [...timers]) {
      if (timer.at <= now && timers.delete(id)) timer.fn();
    }
    await flush();
  }
  await flush();
  if (options.gesture !== false) document.getElementById('strict-play').dispatch('click');
  await flush();
  return {
    audio, client, sent, store, document, push, flush, advance,
    now: () => now,
    snapshot: () => context.__emoStrictV2Acceptance.snapshot(),
    requests: (action) => sent.filter((message) => message.action === action),
    close() { client.disconnect(); timers.clear(); },
  };
}

function prepare(now = 1780000000000) {
  return {
    playbackContextId: 'ctx-handoff', handoffId: 'handoff-1', prepareId: 'prepare-1',
    sourceClientId: 'web-player-source', authorityClientId: 'web-player-source',
    authorityDeviceSessionId: 'web-player-device:source', deviceSessionId: 'web-player-device:target',
    queueSongIds: ['song-1', 'song-2'], currentIndex: 0, trackId: 'song-1', positionMs: 1000,
    positionSampledAtServerMs: now, playbackRate: 1, controlVersion: 5,
    sourceEpoch: 1, sourceVersion: 5, sourceQueueRevision: 1,
  };
}
function commit(prepared, now = 1780000000000) {
  return {
    playbackContextId: prepared.playbackContextId, handoffId: prepared.handoffId,
    sourceClientId: prepared.sourceClientId, controlVersion: prepared.controlVersion + 1,
    serverTimeMs: now, effectiveAtServerMs: now + 250,
    positionMs: prepared.positionMs, playbackRate: prepared.playbackRate,
  };
}

module.exports = { createPage, deferred, prepare, commit };

if (require.main === module) {
  (async () => {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    const input = JSON.parse(Buffer.concat(chunks).toString() || '{}');
    const page = await createPage(input.options);
    const prepared = input.prepare || prepare();
    page.push('playback.prepare', prepared);
    await page.flush();
    if (input.commit) {
      page.push('player.play', input.commit);
      await page.flush();
      await page.advance(Math.max(0, input.commit.effectiveAtServerMs - page.now()));
    }
    process.stdout.write(JSON.stringify(page.sent));
    page.close();
    await page.flush();
  })().catch((error) => { console.error(error); process.exitCode = 1; });
}
