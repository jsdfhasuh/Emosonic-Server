'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const {
  ACTION_TYPES,
  PlayerOwnerLock,
  StrictProtocolError,
  StrictV2Client,
  containsForbiddenSessionField,
  stableStringify,
  validateQueue,
} = require('../../supysonic/static/js/emo_strict_v2_client.js');

const WEB_FIXTURES = JSON.parse(fs.readFileSync(
  path.join(__dirname, '..', 'fixtures', 'emo_web_strict_v2', 'requests.json'),
  'utf8',
));

const PLAYER_CAPABILITIES = {
  playbackContextV2: true,
  playbackPrepare: false,
  effectiveAtPlayback: false,
  canPlay: true,
  canPause: true,
  canSeek: true,
  canSetVolume: true,
  supportsFollow: false,
  supportsBroadcast: false,
};
const HANDOFF_CAPABILITIES = {
  ...PLAYER_CAPABILITIES,
  playbackPrepare: true,
  effectiveAtPlayback: true,
};
const SCHEMA_HASH = 'a'.repeat(64);
const BUILD_COMMIT = 'b'.repeat(40);

class FakeSocket {
  constructor() {
    this.handlers = new Map();
    this.sent = [];
    this.connected = false;
    this.disconnectCount = 0;
  }

  on(name, handler) {
    if (!this.handlers.has(name)) this.handlers.set(name, []);
    this.handlers.get(name).push(handler);
  }

  trigger(name, value) {
    for (const handler of this.handlers.get(name) || []) handler(value);
  }

  connect() {
    this.connected = true;
    this.trigger('connect');
  }

  disconnect() {
    const wasConnected = this.connected;
    this.connected = false;
    this.disconnectCount += 1;
    if (wasConnected) this.trigger('disconnect', 'client disconnect');
  }

  emit(name, value) {
    assert.equal(name, 'message');
    this.sent.push(JSON.parse(JSON.stringify(value)));
  }
}

function readyClient(options = {}) {
  const socket = new FakeSocket();
  socket.connected = true;
  const client = new StrictV2Client({
    registration: {
      clientId: 'web-player-1',
      deviceSessionId: 'web-player-device:1',
    },
    ...options,
  });
  client.socket = socket;
  client.state = 'ready';
  client.connectionNonce = 'nonce-1';
  client.connectionEpoch = 1;
  return { client, socket };
}

function ack(request, extra = {}) {
  return {
    type: 'system',
    action: 'system.ack',
    requestId: request.requestId,
    payload: { action: request.action, ...extra },
    timestamp: 1,
    connectionNonce: 'nonce-1',
    connectionEpoch: 1,
  };
}

function requestError(request, code, nonce = 'nonce-1') {
  return {
    type: 'system',
    action: 'system.error',
    requestId: request.requestId,
    payload: {
      action: request.action,
      code,
      message: `request failed: ${code}`,
      retryable: false,
    },
    timestamp: 1,
    connectionNonce: nonce,
    connectionEpoch: 1,
  };
}

function contextStatus(request, version = 1, epoch = 1) {
  return {
    type: 'state',
    action: 'playback.context.status',
    requestId: request.requestId,
    payload: {
      playbackContext: {
        playbackContextId: request.payload.playbackContextId,
        authorityClientId: 'web-player-1',
        queueSongIds: ['track-1'],
        currentIndex: 0,
        state: 'paused',
        positionMs: version * 100,
        queueRevision: version,
        controlVersion: version,
        version,
        epoch,
        serverUpdatedAtMs: version * 100,
      },
      deviceStates: [],
    },
    timestamp: 1,
    connectionNonce: 'nonce-1',
    connectionEpoch: 1,
  };
}

async function waitFor(predicate, message = 'condition') {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  throw new Error(`Timed out waiting for ${message}`);
}

test('helpers reject forbidden nested session fields and duplicate queues', () => {
  assert.equal(containsForbiddenSessionField({ nested: [{ sessionId: 'legacy' }] }), true);
  assert.equal(containsForbiddenSessionField({ deviceSessionId: 'strict-device' }), false);
  assert.throws(() => validateQueue(['track-1', 'track-1'], 0), /duplicate/);
  assert.throws(() => validateQueue([], 0), /must not be empty/);
  assert.doesNotThrow(() => validateQueue(['track-1', 'track-2'], 1));
  assert.equal(
    stableStringify({ b: 2, a: { d: 4, c: 3 } }),
    stableStringify({ a: { c: 3, d: 4 }, b: 2 }),
  );
});

test('shared client action inventory matches the authoritative web fixtures', () => {
  const fixtureActions = new Set(WEB_FIXTURES.requests.map((item) => item.message.action));
  assert.deepEqual(new Set(Object.keys(ACTION_TYPES)), fixtureActions);
  for (const action of WEB_FIXTURES.forbiddenActions) {
    assert.equal(Object.prototype.hasOwnProperty.call(ACTION_TYPES, action), false);
  }
});

test('business requests are gated until transport bootstrap is ready', async () => {
  const socket = new FakeSocket();
  socket.connected = true;
  const client = new StrictV2Client();
  client.socket = socket;
  client.state = 'registered';
  await assert.rejects(
    client.request('player.play', { playbackContextId: 'ctx-1', baseControlVersion: 1 }),
    /forbidden before ready/,
  );
  assert.equal(socket.sent.length, 0);
});

test('request retry reuses the byte-equivalent envelope and fingerprint', async () => {
  const { client, socket } = readyClient();
  const first = client.request(
    'player.play',
    { playbackContextId: 'ctx-1', baseControlVersion: 1 },
    { requestId: 'retry-1' },
  );
  const second = client.request(
    'player.play',
    { playbackContextId: 'ctx-1', baseControlVersion: 1 },
    { requestId: 'retry-1' },
  );
  assert.deepEqual(socket.sent[1], socket.sent[0]);
  await assert.rejects(
    client.request(
      'player.play',
      { playbackContextId: 'ctx-1', baseControlVersion: 2 },
      { requestId: 'retry-1' },
    ),
    /fingerprint changed/,
  );
  client._onMessage(ack(socket.sent[0]));
  assert.deepEqual(await first, await second);
});

test('ACK action mismatch closes strict mode without fallback', async () => {
  let protocolError = null;
  const { client, socket } = readyClient({
    onProtocolError(error) { protocolError = error; },
  });
  const pending = client.request(
    'player.pause',
    { playbackContextId: 'ctx-1', baseControlVersion: 2 },
    { requestId: 'mismatch-1' },
  );
  client._onMessage({
    ...ack(socket.sent[0]),
    payload: { action: 'player.play' },
  });
  await assert.rejects(pending, /Settlement action mismatch/);
  assert.ok(protocolError instanceof StrictProtocolError);
  assert.equal(client.state, 'protocol_error');
  assert.equal(socket.disconnectCount, 1);
});

test('conflicting settlement, bad provenance, and inbound session fields are fatal', async () => {
  for (const invalidMessage of [
    {
      type: 'state',
      action: 'device.list',
      payload: { devices: [], nested: { sessionId: 'legacy' } },
      timestamp: 1,
      connectionNonce: 'nonce-1',
      connectionEpoch: 1,
    },
    {
      type: 'state',
      action: 'device.list',
      payload: { devices: [] },
      timestamp: 1,
      connectionNonce: 'wrong-nonce',
      connectionEpoch: 1,
    },
  ]) {
    const { client, socket } = readyClient();
    client._onMessage(invalidMessage);
    assert.equal(client.state, 'protocol_error');
    assert.equal(socket.disconnectCount, 1);
  }

  const { client, socket } = readyClient();
  const pending = client.request(
    'player.play',
    { playbackContextId: 'ctx-1', baseControlVersion: 1 },
    { requestId: 'settlement-1' },
  );
  const firstAck = ack(socket.sent[0]);
  client._onMessage(firstAck);
  await pending;
  client._onMessage({ ...firstAck, timestamp: 2 });
  assert.equal(client.state, 'protocol_error');
});

test('event-confirmed playback feedback settles by context and clientSeq', async () => {
  const { client, socket } = readyClient();
  const pending = client.request('playback.update', {
    playbackContextId: 'ctx-1',
    deviceSessionId: 'web-player-device:1',
    state: 'playing',
    positionMs: 1200,
    clientSeq: 1,
    trackId: 'track-1',
    volume: 70,
    muted: false,
  });
  const request = socket.sent[0];
  assert.equal(request.type, 'event');
  assert.equal(containsForbiddenSessionField(request), false);
  client._onMessage({
    type: 'event',
    action: 'playback.update',
    payload: {
      playbackContextId: 'ctx-1',
      sourceClientId: 'web-player-1',
      deviceSessionId: 'web-player-device:1',
      state: 'playing',
      positionMs: 1200,
      clientSeq: 1,
      trackId: 'track-1',
      volume: 70,
      muted: false,
      serverUpdatedAtMs: 1000,
    },
    timestamp: 1,
    connectionNonce: 'nonce-1',
    connectionEpoch: 1,
  });
  const response = await pending;
  assert.equal(response.action, 'playback.update');
});

test('playback feedback settles only for the sending player identity', async () => {
  const { client } = readyClient();
  const pending = client.request('playback.update', {
    playbackContextId: 'ctx-1',
    deviceSessionId: 'web-player-device:1',
    state: 'playing',
    positionMs: 1200,
    clientSeq: 1,
    trackId: 'track-1',
    volume: 70,
    muted: false,
  });
  let settled = false;
  pending.then(() => { settled = true; });
  const feedback = {
    type: 'event',
    action: 'playback.update',
    payload: {
      playbackContextId: 'ctx-1',
      sourceClientId: 'other-player',
      deviceSessionId: 'other-device',
      state: 'playing',
      positionMs: 1200,
      clientSeq: 1,
      trackId: 'track-1',
      volume: 70,
      muted: false,
      serverUpdatedAtMs: 1000,
    },
    timestamp: 1,
    connectionNonce: 'nonce-1',
    connectionEpoch: 1,
  };
  client._onMessage(feedback);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(settled, false);

  feedback.payload.sourceClientId = 'web-player-1';
  feedback.payload.deviceSessionId = 'web-player-device:1';
  client._onMessage(feedback);
  const response = await pending;
  assert.equal(response.payload.sourceClientId, 'web-player-1');
});

test('late valid settlements after timeout are tolerated and settlement history is bounded', async () => {
  const { client, socket } = readyClient({ settledLimit: 2 });
  const timedOut = client.request(
    'device.list',
    {},
    { requestId: 'late-device-list', timeoutMs: 1 },
  );
  await assert.rejects(timedOut, /timed out/);
  client._onMessage({
    type: 'state',
    action: 'device.list',
    requestId: 'late-device-list',
    payload: { devices: [] },
    timestamp: 1,
    connectionNonce: 'nonce-1',
    connectionEpoch: 1,
  });
  assert.equal(client.state, 'ready');

  for (const requestIdValue of ['bounded-1', 'bounded-2']) {
    const pending = client.request('device.list', {}, { requestId: requestIdValue });
    const request = socket.sent.at(-1);
    client._onMessage({
      type: 'state',
      action: 'device.list',
      requestId: request.requestId,
      payload: { devices: [] },
      timestamp: 1,
      connectionNonce: 'nonce-1',
      connectionEpoch: 1,
    });
    await pending;
  }
  assert.equal(client.settled.size, 2);
  assert.equal(client.settled.has('late-device-list'), false);
});

test('failed Context subscribe and status do not leave stale reconnect subscriptions', async () => {
  {
    const { client, socket } = readyClient();
    const pending = client.subscribe('ctx-closed');
    client._onMessage(requestError(socket.sent[0], 'context_closed'));
    await assert.rejects(pending, (error) => error.code === 'context_closed');
    assert.equal(client.subscriptions.has('ctx-closed'), false);
  }

  {
    const { client, socket } = readyClient();
    const pending = client.subscribe('ctx-status-failed');
    client._onMessage(ack(socket.sent[0]));
    await waitFor(() => socket.sent.length === 2, 'Context status request');
    client._onMessage(requestError(socket.sent[1], 'not_found'));
    await waitFor(() => socket.sent.length === 3, 'subscribe rollback');
    assert.equal(socket.sent[2].action, 'playback.context.unsubscribe');
    client._onMessage(ack(socket.sent[2]));
    await assert.rejects(pending, (error) => error.code === 'not_found');
    assert.equal(client.subscriptions.has('ctx-status-failed'), false);
  }
});

test('concurrent Context subscribes serialize and transient refresh failure keeps subscription', async () => {
  const { client, socket } = readyClient();
  const first = client.subscribe('ctx-1');
  const second = client.subscribe('ctx-1');

  assert.equal(socket.sent.length, 1);
  assert.equal(socket.sent[0].action, 'playback.context.subscribe');
  client._onMessage(ack(socket.sent[0]));
  await waitFor(() => socket.sent.length === 2, 'first Context status');
  client._onMessage(contextStatus(socket.sent[1]));
  await waitFor(() => socket.sent.length === 3, 'serialized Context refresh');
  assert.equal(socket.sent[2].action, 'playback.context.status');
  client._onMessage(requestError(socket.sent[2], 'internal_error'));

  await first;
  await assert.rejects(second, (error) => error.code === 'internal_error');
  assert.equal(client.subscriptions.has('ctx-1'), true);
  assert.deepEqual(
    socket.sent.map((request) => request.action),
    [
      'playback.context.subscribe',
      'playback.context.status',
      'playback.context.status',
    ],
  );
});

test('Context subscribe and unsubscribe crossover is serialized with unsubscribe winning', async () => {
  const { client, socket } = readyClient();
  const subscribing = client.subscribe('ctx-1');
  const unsubscribing = client.unsubscribe('ctx-1');

  assert.equal(socket.sent.length, 1);
  client._onMessage(ack(socket.sent[0]));
  await waitFor(() => socket.sent.length === 2, 'Context status before unsubscribe');
  client._onMessage(contextStatus(socket.sent[1]));
  await waitFor(() => socket.sent.length === 3, 'serialized Context unsubscribe');
  assert.equal(socket.sent[2].action, 'playback.context.unsubscribe');
  client._onMessage(ack(socket.sent[2]));

  await subscribing;
  await unsubscribing;
  assert.equal(client.subscriptions.has('ctx-1'), false);
});

test('unsubscribe failures and terminal Context events always clear local subscription state', async () => {
  const { client, socket } = readyClient();
  client.rememberSubscription('ctx-unsubscribe');
  const pending = client.unsubscribe('ctx-unsubscribe');
  client._onMessage(requestError(socket.sent[0], 'context_closed'));
  await assert.rejects(pending, (error) => error.code === 'context_closed');
  assert.equal(client.subscriptions.has('ctx-unsubscribe'), false);

  client.rememberSubscription('ctx-closed-event');
  client._onMessage({
    type: 'event',
    action: 'playback.context.closed',
    payload: { playbackContextId: 'ctx-closed-event' },
    timestamp: 1,
    connectionNonce: 'nonce-1',
    connectionEpoch: 1,
  });
  assert.equal(client.subscriptions.has('ctx-closed-event'), false);

  client.rememberSubscription('ctx-released');
  client._onMessage({
    type: 'event',
    action: 'playback.handoff.release',
    payload: {
      playbackContextId: 'ctx-released',
      newAuthorityClientId: 'web-player-2',
    },
    timestamp: 1,
    connectionNonce: 'nonce-1',
    connectionEpoch: 1,
  });
  assert.equal(client.subscriptions.has('ctx-released'), false);
});

test('registration evidence requires closed metadata, provenance, and paired handoff capabilities', () => {
  const client = new StrictV2Client({
    registration: {
      clientId: 'web-player-1',
      deviceSessionId: 'web-player-device:1',
      capabilities: HANDOFF_CAPABILITIES,
    },
  });
  const message = {
    action: 'system.ack',
    payload: {
      action: 'device.register',
      clientId: 'web-player-1',
      deviceSessionId: 'web-player-device:1',
      negotiatedCapabilities: HANDOFF_CAPABILITIES,
      strictV2: {
        protocolVersion: '2.1.0',
        schemaHash: SCHEMA_HASH,
        serverBuildCommit: BUILD_COMMIT,
        connectionNonce: 'nonce-1',
        connectionEpoch: 1,
      },
    },
    connectionNonce: 'nonce-1',
    connectionEpoch: 1,
  };
  assert.doesNotThrow(() => client._acceptRegistration(message));
  const unpaired = JSON.parse(JSON.stringify(message));
  unpaired.payload.negotiatedCapabilities.effectiveAtPlayback = false;
  assert.throws(() => client._acceptRegistration(unpaired), /negotiated together/);
  const wrongMajor = JSON.parse(JSON.stringify(message));
  wrongMajor.payload.strictV2.protocolVersion = '3.0.0';
  assert.throws(() => client._acceptRegistration(wrongMajor), /Unsupported/);
  const openMetadata = JSON.parse(JSON.stringify(message));
  openMetadata.payload.strictV2.extra = true;
  assert.throws(() => client._acceptRegistration(openMetadata), /shape is not closed/);
  const badSchemaHash = JSON.parse(JSON.stringify(message));
  badSchemaHash.payload.strictV2.schemaHash = 'schema';
  assert.throws(() => client._acceptRegistration(badSchemaHash), /schemaHash is invalid/);
  const badBuildCommit = JSON.parse(JSON.stringify(message));
  badBuildCommit.payload.strictV2.serverBuildCommit = 'build';
  assert.throws(() => client._acceptRegistration(badBuildCommit), /serverBuildCommit is invalid/);
  const elevated = JSON.parse(JSON.stringify(message));
  elevated.payload.negotiatedCapabilities.supportsBroadcast = true;
  elevated.payload.negotiatedCapabilities.playbackPrepare = false;
  elevated.payload.negotiatedCapabilities.effectiveAtPlayback = false;
  assert.throws(() => client._acceptRegistration(elevated), /elevated unrequested capability/);
});

test('bootstrap fetches a fresh browser OTP and reaches ready with exact registration', async () => {
  const socket = new FakeSocket();
  let credentialCalls = 0;
  const states = [];
  const debug = [];
  const client = new StrictV2Client({
    ioFactory: () => socket,
    fetchFn: async () => ({
      ok: true,
      async json() {
        credentialCalls += 1;
        return {
          userName: 'alice',
          oneTimePassword: `browser-otp:credential-${credentialCalls}`,
          expiresAtMs: Date.now() + 60000,
        };
      },
    }),
    socketUrl: 'http://localhost/emo',
    authPasswordUrl: '/emo/browser-auth-password',
    contextBindingsUrl: '/emo/web-context-bindings',
    csrfToken: 'csrf',
    registration: {
      clientId: 'web-player-1',
      deviceSessionId: 'web-player-device:1',
      deviceName: 'Web Player',
      roles: ['player'],
      capabilities: PLAYER_CAPABILITIES,
    },
    onState(state) { states.push(state); },
    onDebug(summary) { debug.push(summary); },
  });
  client.connect();
  await waitFor(() => socket.sent.length === 1, 'auth request');
  const authRequest = socket.sent[0];
  assert.deepEqual(authRequest.payload, {
    u: 'alice',
    p: 'browser-otp:credential-1',
  });
  client._onMessage({
    type: 'system',
    action: 'system.ack',
    requestId: authRequest.requestId,
    payload: { action: 'auth.login', authenticated: true, userName: 'alice' },
    timestamp: 1,
  });
  await waitFor(() => socket.sent.length === 2, 'registration request');
  const registrationRequest = socket.sent[1];
  assert.deepEqual(registrationRequest.payload.capabilities, PLAYER_CAPABILITIES);
  assert.equal(containsForbiddenSessionField(registrationRequest), false);
  client._onMessage({
    type: 'system',
    action: 'system.ack',
    requestId: registrationRequest.requestId,
    payload: {
      action: 'device.register',
      clientId: 'web-player-1',
      deviceSessionId: 'web-player-device:1',
      negotiatedCapabilities: PLAYER_CAPABILITIES,
      strictV2: {
        protocolVersion: '2.1.0',
        schemaHash: SCHEMA_HASH,
        serverBuildCommit: BUILD_COMMIT,
        connectionNonce: 'nonce-1',
        connectionEpoch: 1,
      },
    },
    timestamp: 1,
    connectionNonce: 'nonce-1',
    connectionEpoch: 1,
  });
  client._onMessage({
    type: 'state',
    action: 'device.list',
    payload: { devices: [] },
    timestamp: 1,
    connectionNonce: 'nonce-1',
    connectionEpoch: 1,
  });
  await waitFor(() => socket.sent.length === 3, 'device list request');
  const listRequest = socket.sent[2];
  client._onMessage({
    type: 'state',
    action: 'device.list',
    requestId: listRequest.requestId,
    payload: { devices: [] },
    timestamp: 1,
    connectionNonce: 'nonce-1',
    connectionEpoch: 1,
  });
  await waitFor(() => client.state === 'ready', 'ready state');
  assert.deepEqual(states.slice(0, 8), [
    'connected',
    'authenticating',
    'authenticated',
    'registering',
    'registered',
    'synchronizing',
    'ready',
  ]);
  assert.equal(credentialCalls, 1);
  client.rememberSubscription('ctx-closed');
  client.rememberSubscription('ctx-1');
  assert.equal(client.nextClientSeq(), 1);
  socket.connected = false;
  socket.trigger('disconnect', 'network');
  socket.connect();
  await waitFor(() => socket.sent.length === 4, 'reconnect auth request');
  const reconnectAuth = socket.sent[3];
  assert.equal(reconnectAuth.payload.p, 'browser-otp:credential-2');
  client._onMessage({
    type: 'system',
    action: 'system.ack',
    requestId: reconnectAuth.requestId,
    payload: { action: 'auth.login', authenticated: true, userName: 'alice' },
    timestamp: 2,
  });
  await waitFor(() => socket.sent.length === 5, 'reconnect registration');
  const reconnectRegistration = socket.sent[4];
  client._onMessage({
    type: 'system',
    action: 'system.ack',
    requestId: reconnectRegistration.requestId,
    payload: {
      action: 'device.register',
      clientId: 'web-player-1',
      deviceSessionId: 'web-player-device:1',
      negotiatedCapabilities: PLAYER_CAPABILITIES,
      strictV2: {
        protocolVersion: '2.1.0',
        schemaHash: 'c'.repeat(64),
        serverBuildCommit: 'd'.repeat(40),
        connectionNonce: 'nonce-2',
        connectionEpoch: 1,
      },
    },
    timestamp: 2,
    connectionNonce: 'nonce-2',
    connectionEpoch: 1,
  });
  await waitFor(() => socket.sent.length === 6, 'reconnect device list');
  const reconnectList = socket.sent[5];
  client._onMessage({
    type: 'state',
    action: 'device.list',
    requestId: reconnectList.requestId,
    payload: { devices: [] },
    timestamp: 2,
    connectionNonce: 'nonce-2',
    connectionEpoch: 1,
  });
  await waitFor(() => socket.sent.length === 7, 'closed subscription restore');
  const closedSubscribe = socket.sent[6];
  assert.equal(closedSubscribe.action, 'playback.context.subscribe');
  assert.equal(closedSubscribe.payload.playbackContextId, 'ctx-closed');
  client._onMessage(requestError(closedSubscribe, 'context_closed', 'nonce-2'));
  await waitFor(() => socket.sent.length === 8, 'live subscription restore');
  const subscribe = socket.sent[7];
  assert.equal(subscribe.action, 'playback.context.subscribe');
  assert.equal(subscribe.payload.playbackContextId, 'ctx-1');
  client._onMessage({
    ...ack(subscribe),
    timestamp: 2,
    connectionNonce: 'nonce-2',
  });
  await waitFor(() => socket.sent.length === 9, 'status restore');
  const status = socket.sent[8];
  assert.equal(status.action, 'playback.context.status');
  client._onMessage({
    type: 'state',
    action: 'playback.context.status',
    requestId: status.requestId,
    payload: {
      playbackContext: {
        playbackContextId: 'ctx-1',
        authorityClientId: 'web-player-1',
        queueSongIds: ['track-1'],
        currentIndex: 0,
        state: 'paused',
        positionMs: 0,
        queueRevision: 1,
        controlVersion: 1,
        version: 1,
        epoch: 1,
        serverUpdatedAtMs: 2,
      },
      deviceStates: [],
    },
    timestamp: 2,
    connectionNonce: 'nonce-2',
    connectionEpoch: 1,
  });
  await waitFor(() => client.state === 'ready', 'reconnect ready state');
  assert.equal(client.subscriptions.has('ctx-closed'), false);
  assert.equal(client.subscriptions.has('ctx-1'), true);
  assert.equal(credentialCalls, 2);
  assert.equal(client.metadata.schemaHash, 'c'.repeat(64));
  assert.equal(client.nextClientSeq(), 1);
  assert.equal(JSON.stringify(debug).includes('browser-otp:'), false);
  client.disconnect();
});

test('context cursors are canonical and physical reconnect resets clientSeq', () => {
  let monotonicMs = 100;
  const { client } = readyClient({ monotonicNow: () => monotonicMs });
  client._updateServerClock({ payload: { serverTimeMs: 5000 }, timestamp: 1 });
  monotonicMs = 250;
  assert.equal(client.serverNowMs(), 5150);
  client._ingestContext({
    action: 'playback.context.status',
    payload: {
      playbackContext: {
        playbackContextId: 'ctx-1',
        queueSongIds: ['track-1'],
        currentIndex: 0,
        state: 'paused',
        positionMs: 20,
        queueRevision: 3,
        controlVersion: 5,
        version: 6,
        epoch: 1,
      },
    },
  });
  assert.deepEqual(client.cursor('ctx-1'), {
    queueSongIds: ['track-1'],
    currentIndex: 0,
    state: 'paused',
    positionMs: 20,
    queueRevision: 3,
    controlVersion: 5,
    version: 6,
    epoch: 1,
  });
  client._ingestContext({
    action: 'playback.context.status',
    payload: {
      playbackContext: {
        playbackContextId: 'ctx-1',
        queueSongIds: ['track-old'],
        currentIndex: 0,
        state: 'playing',
        positionMs: 10,
        queueRevision: 2,
        controlVersion: 4,
        version: 5,
        epoch: 1,
      },
    },
  });
  assert.equal(client.cursor('ctx-1').version, 6);
  assert.deepEqual(client.cursor('ctx-1').queueSongIds, ['track-1']);
  client._ingestContext({
    action: 'playback.context.status',
    payload: {
      playbackContext: {
        playbackContextId: 'ctx-1',
        queueSongIds: ['track-new-epoch'],
        currentIndex: 0,
        state: 'paused',
        positionMs: 30,
        queueRevision: 1,
        controlVersion: 1,
        version: 1,
        epoch: 2,
      },
    },
  });
  assert.equal(client.cursor('ctx-1').epoch, 2);
  assert.equal(client.cursor('ctx-1').version, 1);
  assert.equal(client.cursor('ctx-1').queueRevision, 1);
  assert.equal(client.cursor('ctx-1').controlVersion, 1);
  assert.deepEqual(client.cursor('ctx-1').queueSongIds, ['track-new-epoch']);
  client._ingestContext({
    action: 'player.seek',
    payload: {
      playbackContextId: 'ctx-1',
      controlVersion: 0,
      positionMs: 999,
    },
  });
  assert.equal(client.cursor('ctx-1').controlVersion, 1);
  assert.equal(client.cursor('ctx-1').positionMs, 30);
  client._ingestContext({
    action: 'player.seek',
    payload: {
      playbackContextId: 'ctx-1',
      controlVersion: 2,
      positionMs: 40,
    },
  });
  assert.equal(client.cursor('ctx-1').controlVersion, 2);
  assert.equal(client.cursor('ctx-1').version, 1);
  assert.equal(client.cursor('ctx-1').positionMs, 40);
  client._ingestContext({
    action: 'broadcast.status',
    payload: {
      playbackContextId: 'ctx-1',
      version: 99,
      positionMs: 999,
    },
  });
  assert.equal(client.cursor('ctx-1').version, 1);
  assert.equal(client.cursor('ctx-1').positionMs, 40);
  assert.equal(client.nextClientSeq(), 1);
  assert.equal(client.nextClientSeq(), 2);
  client._onDisconnect('network');
  assert.equal(client.nextClientSeq(), 1);
});

test('stale Context snapshots and commands settle requests without reaching UI events', async () => {
  const events = [];
  const { client, socket } = readyClient({
    onEvent(message) { events.push(message.action); },
  });
  const initial = contextStatus({
    requestId: null,
    payload: { playbackContextId: 'ctx-1' },
  }, 5);
  delete initial.requestId;
  client._onMessage(initial);

  const staleStatus = contextStatus({
    requestId: null,
    payload: { playbackContextId: 'ctx-1' },
  }, 4);
  delete staleStatus.requestId;
  client._onMessage(staleStatus);
  client._onMessage({
    type: 'command',
    action: 'player.pause',
    payload: {
      playbackContextId: 'ctx-1',
      controlVersion: 4,
      positionMs: 100,
    },
    timestamp: 1,
    connectionNonce: 'nonce-1',
    connectionEpoch: 1,
  });

  assert.deepEqual(events, ['playback.context.status']);
  assert.equal(client.cursor('ctx-1').controlVersion, 5);
  assert.equal(
    client.isCurrentContextSnapshot(staleStatus.payload.playbackContext),
    false,
  );

  const pending = client.refreshContext('ctx-1');
  client._onMessage(contextStatus(socket.sent[0], 4));
  const response = await pending;
  assert.equal(response.payload.playbackContext.version, 4);
  assert.deepEqual(events, ['playback.context.status']);
  assert.equal(client.cursor('ctx-1').version, 5);
});

test('fallback owner lease prevents replacement loops and allows takeover after release', async () => {
  const values = new Map();
  const storage = {
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
  const first = new PlayerOwnerLock({ storage, navigator: {}, ownerToken: 'tab-1' });
  const second = new PlayerOwnerLock({ storage, navigator: {}, ownerToken: 'tab-2' });
  assert.equal(await first.acquire(), true);
  assert.equal(await second.acquire(), false);
  first.release();
  assert.equal(await second.acquire(), true);
  second.release();
});
