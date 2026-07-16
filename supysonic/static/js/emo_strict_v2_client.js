(function (root, factory) {
  const exported = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = exported;
  } else {
    root.EmoStrictV2 = exported;
  }
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const BASE_CAPABILITY_NAMES = Object.freeze([
    'playbackContextV2',
    'playbackPrepare',
    'effectiveAtPlayback',
    'canPlay',
    'canPause',
    'canSeek',
    'canSetVolume',
    'supportsFollow',
    'supportsBroadcast',
  ]);
  const OPTIONAL_CAPABILITY_NAMES = Object.freeze(['remoteVolumeControl']);
  const CAPABILITY_NAMES = Object.freeze([
    ...BASE_CAPABILITY_NAMES,
    ...OPTIONAL_CAPABILITY_NAMES,
  ]);

  const ACTION_TYPES = Object.freeze({
    'auth.login': 'auth',
    'device.register': 'device',
    'device.list': 'state',
    'device.setVolume': 'command',
    'device.volume.update': 'event',
    'system.ping': 'system',
    'playback.context.list': 'state',
    'playback.context.create': 'command',
    'playback.context.subscribe': 'state',
    'playback.context.unsubscribe': 'state',
    'playback.context.status': 'state',
    'playback.context.close': 'command',
    'queue.context.sync': 'state',
    'playback.update': 'event',
    'queue.playItem': 'command',
    'player.play': 'command',
    'player.pause': 'command',
    'player.seek': 'command',
    'player.next': 'command',
    'player.prev': 'command',
    'follow.start': 'command',
    'follow.stop': 'command',
    'playback.handoff.start': 'command',
    'playback.ready': 'event',
    'playback.handoff.complete': 'event',
    'playback.handoff.cancel': 'command',
    'broadcast.start': 'command',
    'broadcast.status': 'state',
    'broadcast.play': 'command',
    'broadcast.pause': 'command',
    'broadcast.seek': 'command',
    'broadcast.playItem': 'command',
    'broadcast.queue.sync': 'state',
    'broadcast.stop': 'command',
  });

  const DIRECT_RESPONSE_ACTIONS = new Set([
    'device.list',
    'system.ping',
    'playback.context.list',
    'playback.context.create',
    'playback.context.status',
  ]);

  const EVENT_CONFIRMED_ACTIONS = Object.freeze({
    'device.volume.update': 'device.volume.update',
    'playback.update': 'playback.update',
    'playback.ready': 'playback.handoff.status',
    'playback.handoff.complete': 'playback.handoff.status',
  });

  const READY_BYPASS_ACTIONS = new Set([
    'auth.login',
    'device.register',
    'device.list',
    'system.ping',
    'playback.context.subscribe',
    'playback.context.status',
  ]);

  const FORBIDDEN_SESSION_FIELDS = new Set(['sessionId', 'sourceSessionId']);
  const CONTEXT_CURSOR_FIELDS = [
    'queueRevision',
    'controlVersion',
    'version',
    'epoch',
  ];

  class StrictProtocolError extends Error {
    constructor(message, detail) {
      super(message);
      this.name = 'StrictProtocolError';
      this.detail = detail || null;
    }
  }

  class StrictRequestError extends Error {
    constructor(payload, requestId) {
      super(payload && payload.message ? payload.message : 'Strict-v2 request failed');
      this.name = 'StrictRequestError';
      this.requestId = requestId;
      this.action = payload && payload.action;
      this.code = payload && payload.code;
      this.retryable = Boolean(payload && payload.retryable);
      this.payload = payload || {};
    }
  }

  function stableStringify(value) {
    if (Array.isArray(value)) {
      return `[${value.map(stableStringify).join(',')}]`;
    }
    if (value && typeof value === 'object') {
      return `{${Object.keys(value).sort().map((key) => (
        `${JSON.stringify(key)}:${stableStringify(value[key])}`
      )).join(',')}}`;
    }
    return JSON.stringify(value);
  }

  function cloneJson(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function containsForbiddenSessionField(value) {
    if (Array.isArray(value)) {
      return value.some(containsForbiddenSessionField);
    }
    if (!value || typeof value !== 'object') {
      return false;
    }
    return Object.keys(value).some((key) => (
      FORBIDDEN_SESSION_FIELDS.has(key) || containsForbiddenSessionField(value[key])
    ));
  }

  function uuid() {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  }

  function requestId(action) {
    return `${action}-${Date.now()}-${uuid()}`;
  }

  function requireClosedCapabilities(value, label, includeExtensions) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new StrictProtocolError(`${label} must be an object`);
    }
    const fields = Object.keys(value).sort();
    const expected = (includeExtensions ? CAPABILITY_NAMES : BASE_CAPABILITY_NAMES)
      .slice()
      .sort();
    if (fields.length !== expected.length || fields.some((field, index) => field !== expected[index])) {
      throw new StrictProtocolError(`${label} must contain exactly the strict-v2 capabilities`);
    }
    expected.forEach((capability) => {
      if (typeof value[capability] !== 'boolean') {
        throw new StrictProtocolError(`${label}.${capability} must be boolean`);
      }
    });
    if (value.playbackPrepare !== value.effectiveAtPlayback) {
      throw new StrictProtocolError('Handoff capabilities must be negotiated together');
    }
    return cloneJson(value);
  }

  function validateQueue(queueSongIds, currentIndex) {
    if (!Array.isArray(queueSongIds) || queueSongIds.length === 0) {
      throw new StrictProtocolError('strict-v2 queue must not be empty');
    }
    if (queueSongIds.length > 1000) {
      throw new StrictProtocolError('strict-v2 queue exceeds 1000 tracks');
    }
    if (queueSongIds.some((trackId) => typeof trackId !== 'string' || !trackId.trim())) {
      throw new StrictProtocolError('strict-v2 queue contains an invalid track ID');
    }
    if (new Set(queueSongIds).size !== queueSongIds.length) {
      throw new StrictProtocolError('strict-v2 queue does not support duplicate tracks');
    }
    if (!Number.isInteger(currentIndex) || currentIndex < 0 || currentIndex >= queueSongIds.length) {
      throw new StrictProtocolError('strict-v2 currentIndex is outside the queue');
    }
  }

  class StrictV2Client {
    constructor(options) {
      this.options = options || {};
      this.ioFactory = this.options.ioFactory || (typeof io === 'function' ? io : null);
      this.fetchFn = this.options.fetchFn || (typeof fetch === 'function' ? fetch.bind(globalThis) : null);
      this.socket = null;
      this.state = 'disconnected';
      this.registration = cloneJson(this.options.registration || {});
      this.pending = new Map();
      this.settled = new Map();
      this.settledLimit = Number.isInteger(this.options.settledLimit)
        ? Math.max(1, this.options.settledLimit)
        : 2048;
      this.contextCursors = new Map();
      this.subscriptions = new Set();
      this.contextMutations = new Map();
      this.negotiatedCapabilities = null;
      this.metadata = null;
      this.connectionNonce = null;
      this.connectionEpoch = null;
      this.clientSeq = 0;
      this.bootstrapGeneration = 0;
      this.protocolFailed = false;
      this.serverClockOffsetMs = 0;
      this.serverClockSample = null;
      this.monotonicNow = this.options.monotonicNow || (() => (
        typeof performance !== 'undefined' && typeof performance.now === 'function'
          ? performance.now()
          : Date.now()
      ));
      this.heartbeatTimer = null;
      this._boundSocket = false;
    }

    _notifyState(state, detail) {
      this.state = state;
      this._debug('connection_state', { state });
      if (typeof this.options.onState === 'function') {
        this.options.onState(state, detail || null);
      }
    }

    _notifyEvent(message) {
      if (typeof this.options.onEvent === 'function') {
        this.options.onEvent(cloneJson(message));
      }
    }

    _debug(event, fields) {
      if (typeof this.options.onDebug !== 'function') return;
      this.options.onDebug(Object.assign({ event }, fields || {}));
    }

    _protocolError(message, detail) {
      if (this.protocolFailed) {
        return;
      }
      this.protocolFailed = true;
      const error = message instanceof StrictProtocolError
        ? message
        : new StrictProtocolError(message, detail);
      this._notifyState('protocol_error', error);
      this._debug('protocol_error', { message: error.message });
      for (const pending of this.pending.values()) {
        clearTimeout(pending.timer);
        pending.reject(error);
      }
      this.pending.clear();
      if (typeof this.options.onProtocolError === 'function') {
        this.options.onProtocolError(error);
      }
      if (this.socket && this.socket.connected) {
        this.socket.disconnect();
      }
    }

    connect() {
      if (!this.ioFactory) {
        throw new Error('Socket.IO client factory is unavailable');
      }
      if (!this.fetchFn) {
        throw new Error('fetch is unavailable');
      }
      if (!this.socket) {
        this.socket = this.ioFactory(this.options.socketUrl, {
          path: this.options.socketPath || '/emo/ws',
          transports: this.options.transports || ['polling'],
          autoConnect: false,
        });
      }
      this._bindSocket();
      this.protocolFailed = false;
      this.socket.connect();
      return this;
    }

    disconnect() {
      this.bootstrapGeneration += 1;
      this._stopHeartbeat();
      if (this.socket) {
        this.socket.disconnect();
      }
      this._notifyState('disconnected');
    }

    _bindSocket() {
      if (this._boundSocket) {
        return;
      }
      this._boundSocket = true;
      this.socket.on('connect', () => this._onConnect());
      this.socket.on('disconnect', (reason) => this._onDisconnect(reason));
      this.socket.on('connect_error', (error) => {
        this._notifyState('offline', error);
      });
      this.socket.on('message', (message) => this._onMessage(message));
    }

    async _onConnect() {
      const generation = ++this.bootstrapGeneration;
      this.connectionNonce = null;
      this.connectionEpoch = null;
      this.negotiatedCapabilities = null;
      this.clientSeq = 0;
      this.settled.clear();
      this.protocolFailed = false;
      this._notifyState('connected');
      try {
        this._notifyState('authenticating');
        const credential = await this._fetchCredential();
        if (generation !== this.bootstrapGeneration) return;
        await this.request('auth.login', {
          u: credential.userName,
          p: credential.oneTimePassword,
        }, { allowBeforeReady: true });
        if (generation !== this.bootstrapGeneration) return;
        this._notifyState('authenticated');
        this._notifyState('registering');
        await this.request(
          'device.register',
          this.registration,
          { allowBeforeReady: true },
        );
        if (generation !== this.bootstrapGeneration) return;
        this._notifyState('registered', {
          metadata: this.metadata,
          negotiatedCapabilities: this.negotiatedCapabilities,
        });
        this._notifyState('synchronizing');
        await this.request('device.list', {}, { allowBeforeReady: true });
        for (const playbackContextId of Array.from(this.subscriptions)) {
          try {
            await this.request(
              'playback.context.subscribe',
              { playbackContextId },
              { allowBeforeReady: true },
            );
            await this.request(
              'playback.context.status',
              { playbackContextId },
              { allowBeforeReady: true },
            );
          } catch (error) {
            if (!this._isTerminalContextError(error)) throw error;
            this.forgetSubscription(playbackContextId);
          }
        }
        if (generation !== this.bootstrapGeneration) return;
        this._notifyState('ready');
        this._startHeartbeat();
        if (typeof this.options.onReady === 'function') {
          this.options.onReady(this);
        }
      } catch (error) {
        if (generation !== this.bootstrapGeneration) return;
        if (error instanceof StrictProtocolError) {
          this._protocolError(error);
          return;
        }
        this._notifyState('offline', error);
        if (typeof this.options.onBootstrapError === 'function') {
          this.options.onBootstrapError(error);
        }
        if (this.socket && this.socket.connected) {
          this.socket.disconnect();
        }
      }
    }

    _onDisconnect(reason) {
      this.bootstrapGeneration += 1;
      this._stopHeartbeat();
      this.connectionNonce = null;
      this.connectionEpoch = null;
      this.clientSeq = 0;
      for (const pending of this.pending.values()) {
        clearTimeout(pending.timer);
        pending.reject(new Error(`Socket disconnected: ${reason || 'unknown'}`));
      }
      this.pending.clear();
      if (!this.protocolFailed) {
        this._notifyState('disconnected', reason || null);
      }
    }

    async _fetchCredential() {
      const response = await this.fetchFn(this.options.authPasswordUrl, {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {
          'X-Emo-CSRF-Token': this.options.csrfToken,
          'X-Requested-With': 'fetch',
        },
      });
      if (!response.ok) {
        throw new Error(`Browser credential request failed (${response.status})`);
      }
      const credential = await response.json();
      if (
        !credential
        || typeof credential.userName !== 'string'
        || !credential.userName
        || typeof credential.oneTimePassword !== 'string'
        || !credential.oneTimePassword.startsWith('browser-otp:')
      ) {
        throw new StrictProtocolError('Browser credential response is malformed');
      }
      return credential;
    }

    _acceptRegistration(message) {
      const payload = message && message.payload;
      if (!payload || payload.action !== 'device.register') {
        throw new StrictProtocolError('Registration ACK action is invalid');
      }
      if (payload.clientId !== this.registration.clientId) {
        throw new StrictProtocolError('Registration ACK clientId does not match');
      }
      if (payload.deviceSessionId !== this.registration.deviceSessionId) {
        throw new StrictProtocolError('Registration ACK deviceSessionId does not match');
      }
      const requestedCapabilities = this.registration.capabilities || {};
      const includeCapabilityExtensions = Object.prototype.hasOwnProperty.call(
        requestedCapabilities,
        'remoteVolumeControl',
      );
      this.negotiatedCapabilities = requireClosedCapabilities(
        payload.negotiatedCapabilities,
        'negotiatedCapabilities',
        includeCapabilityExtensions,
      );
      Object.keys(this.negotiatedCapabilities).forEach((capability) => {
        if (this.negotiatedCapabilities[capability] && requestedCapabilities[capability] !== true) {
          throw new StrictProtocolError(`Server elevated unrequested capability: ${capability}`);
        }
      });
      if (!this.negotiatedCapabilities.playbackContextV2) {
        throw new StrictProtocolError('Registration did not negotiate PlaybackContext strict-v2');
      }
      const metadata = payload.strictV2;
      if (!metadata || typeof metadata !== 'object') {
        throw new StrictProtocolError('Registration ACK strictV2 metadata is missing');
      }
      const metadataFields = Object.keys(metadata).sort();
      const expectedMetadataFields = [
        'connectionEpoch',
        'connectionNonce',
        'protocolVersion',
        'schemaHash',
        'serverBuildCommit',
      ];
      if (
        metadataFields.length !== expectedMetadataFields.length
        || metadataFields.some((field, index) => field !== expectedMetadataFields[index])
      ) {
        throw new StrictProtocolError('Registration ACK strictV2 metadata shape is not closed');
      }
      const version = metadata.protocolVersion;
      const versionMatch = typeof version === 'string'
        ? /^(\d+)\.(\d+)\.(\d+)$/.exec(version)
        : null;
      if (
        !versionMatch
        || Number(versionMatch[1]) !== 2
        || Number(versionMatch[2]) < 2
      ) {
        throw new StrictProtocolError(`Unsupported strict-v2 protocol version: ${String(version)}`);
      }
      if (typeof metadata.schemaHash !== 'string' || !/^[0-9a-f]{64}$/.test(metadata.schemaHash)) {
        throw new StrictProtocolError('Registration metadata schemaHash is invalid');
      }
      if (
        typeof metadata.serverBuildCommit !== 'string'
        || !/^(unknown|[0-9a-f]{40})$/.test(metadata.serverBuildCommit)
      ) {
        throw new StrictProtocolError('Registration metadata serverBuildCommit is invalid');
      }
      if (typeof metadata.connectionNonce !== 'string' || !metadata.connectionNonce) {
        throw new StrictProtocolError('Registration metadata connectionNonce is invalid');
      }
      if (!Number.isInteger(metadata.connectionEpoch) || metadata.connectionEpoch < 1) {
        throw new StrictProtocolError('Registration metadata connectionEpoch is invalid');
      }
      if (
        message.connectionNonce !== metadata.connectionNonce
        || message.connectionEpoch !== metadata.connectionEpoch
      ) {
        throw new StrictProtocolError('Registration provenance does not match metadata');
      }
      this.metadata = cloneJson(metadata);
      this.connectionNonce = metadata.connectionNonce;
      this.connectionEpoch = metadata.connectionEpoch;
      this._debug('registration_metadata', {
        protocolVersion: metadata.protocolVersion,
        schemaHash: metadata.schemaHash,
        serverBuildCommit: metadata.serverBuildCommit,
        connectionNonce: metadata.connectionNonce,
        connectionEpoch: metadata.connectionEpoch,
        clientId: this.registration.clientId,
        deviceSessionId: this.registration.deviceSessionId,
      });
    }

    _assertProvenance(message) {
      const payload = message && message.payload;
      const bootstrap = (message.action === 'system.ack' || message.action === 'system.error')
        && payload
        && (payload.action === 'auth.login' || payload.action === 'device.register');
      if (bootstrap && !this.connectionNonce) {
        return;
      }
      if (!this.connectionNonce) {
        throw new StrictProtocolError('Strict output arrived before registration evidence');
      }
      if (
        message.connectionNonce !== this.connectionNonce
        || message.connectionEpoch !== this.connectionEpoch
      ) {
        throw new StrictProtocolError('Strict output provenance does not match registration');
      }
    }

    _onMessage(message) {
      try {
        if (!message || typeof message !== 'object' || Array.isArray(message)) {
          throw new StrictProtocolError('Strict output envelope must be an object');
        }
        if (containsForbiddenSessionField(message)) {
          throw new StrictProtocolError('Strict output contains a forbidden session field');
        }
        if (typeof message.action !== 'string' || !message.action) {
          throw new StrictProtocolError('Strict output action is missing');
        }
        this._updateServerClock(message);
        this._assertProvenance(message);
        const contextAccepted = this._ingestContext(message);
        this._ingestSubscriptionLifecycle(message);
        if (message.requestId) {
          this._settleCorrelated(message);
        } else {
          this._settleEventConfirmed(message);
        }
        if (contextAccepted) this._notifyEvent(message);
      } catch (error) {
        this._protocolError(error);
      }
    }

    _updateServerClock(message) {
      const serverTimeMs = Number(message.payload && message.payload.serverTimeMs);
      if (Number.isFinite(serverTimeMs)) {
        this.serverClockOffsetMs = serverTimeMs - Date.now();
        this.serverClockSample = {
          serverTimeMs,
          monotonicMs: this.monotonicNow(),
        };
      } else if (Number.isFinite(Number(message.timestamp))) {
        const timestampMs = Number(message.timestamp) * 1000;
        this.serverClockOffsetMs = timestampMs - Date.now();
        this.serverClockSample = {
          serverTimeMs: timestampMs,
          monotonicMs: this.monotonicNow(),
        };
      }
    }

    serverNowMs() {
      if (this.serverClockSample) {
        return this.serverClockSample.serverTimeMs
          + Math.max(0, this.monotonicNow() - this.serverClockSample.monotonicMs);
      }
      return Date.now() + this.serverClockOffsetMs;
    }

    _startHeartbeat() {
      this._stopHeartbeat();
      const heartbeatMs = Number.isFinite(this.options.heartbeatMs)
        ? Math.max(1000, this.options.heartbeatMs)
        : 30000;
      this.heartbeatTimer = setInterval(() => {
        if (this.state !== 'ready' || !this.socket || !this.socket.connected) return;
        this.request('system.ping', {}).catch(() => {});
      }, heartbeatMs);
    }

    _stopHeartbeat() {
      if (this.heartbeatTimer) clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }

    _rememberSettlement(requestIdValue, settlement) {
      this.settled.delete(requestIdValue);
      this.settled.set(requestIdValue, settlement);
      while (this.settled.size > this.settledLimit) {
        this.settled.delete(this.settled.keys().next().value);
      }
    }

    _validateSettlementAction(message, expectedAction) {
      if (message.action === 'system.ack' || message.action === 'system.error') {
        const payloadAction = message.payload && message.payload.action;
        if (payloadAction !== expectedAction) {
          throw new StrictProtocolError(
            `Settlement action mismatch: expected ${expectedAction}, received ${String(payloadAction)}`,
          );
        }
        return payloadAction;
      }
      const expected = expectedAction === 'system.ping' ? 'system.pong' : expectedAction;
      if (message.action !== expected) {
        throw new StrictProtocolError(
          `Direct response mismatch: expected ${expected}, received ${message.action}`,
        );
      }
      return message.action;
    }

    _settleCorrelated(message) {
      const pending = this.pending.get(message.requestId);
      if (!pending) {
        const prior = this.settled.get(message.requestId);
        if (!prior) {
          throw new StrictProtocolError(`Unknown correlated requestId: ${message.requestId}`);
        }
        const signature = stableStringify(message);
        if (prior.signature === null) {
          const settlementAction = this._validateSettlementAction(message, prior.action);
          this._rememberSettlement(message.requestId, {
            action: settlementAction,
            signature,
            timedOut: true,
          });
          return;
        }
        if (prior.signature !== signature) {
          throw new StrictProtocolError(`Conflicting settlement for requestId: ${message.requestId}`);
        }
        return;
      }

      const settlementAction = this._validateSettlementAction(message, pending.action);

      clearTimeout(pending.timer);
      this.pending.delete(message.requestId);
      if (pending.action === 'device.register' && message.action === 'system.ack') {
        this._acceptRegistration(message);
      }
      this._rememberSettlement(message.requestId, {
        action: settlementAction,
        signature: stableStringify(message),
      });
      if (message.action === 'system.error') {
        this._debug('request_settlement', {
          requestId: message.requestId,
          action: pending.action,
          settlement: 'error',
          errorCode: message.payload && message.payload.code,
          playbackContextId: pending.payload.playbackContextId,
        });
        pending.reject(new StrictRequestError(message.payload, message.requestId));
      } else {
        this._debug('request_settlement', {
          requestId: message.requestId,
          action: pending.action,
          settlement: message.action === 'system.ack' ? 'ack' : 'direct',
          playbackContextId: pending.payload.playbackContextId,
        });
        pending.resolve(cloneJson(message));
      }
    }

    _settleEventConfirmed(message) {
      for (const [pendingRequestId, pending] of this.pending.entries()) {
        if (EVENT_CONFIRMED_ACTIONS[pending.action] !== message.action) {
          continue;
        }
        if (
          pending.payload.playbackContextId
          && message.payload
          && message.payload.playbackContextId !== pending.payload.playbackContextId
        ) {
          continue;
        }
        if (
          pending.action === 'device.volume.update'
          && (
            !message.payload
            || message.payload.clientSeq !== pending.payload.clientSeq
            || message.payload.sourceClientId !== this.registration.clientId
            || message.payload.deviceSessionId !== pending.payload.deviceSessionId
          )
        ) {
          continue;
        }
        if (
          pending.action === 'playback.update'
          && (
            !message.payload
            || message.payload.clientSeq !== pending.payload.clientSeq
            || message.payload.sourceClientId !== this.registration.clientId
            || message.payload.deviceSessionId !== pending.payload.deviceSessionId
          )
        ) {
          continue;
        }
        if (
          pending.action === 'playback.ready'
          && (
            !message.payload
            || message.payload.handoffId !== pending.payload.handoffId
          )
        ) {
          continue;
        }
        if (
          pending.action === 'playback.handoff.complete'
          && (
            !message.payload
            || message.payload.handoffId !== pending.payload.handoffId
          )
        ) {
          continue;
        }
        clearTimeout(pending.timer);
        this.pending.delete(pendingRequestId);
        this._rememberSettlement(pendingRequestId, {
          action: pending.action,
          signature: stableStringify(message),
        });
        this._debug('request_settlement', {
          requestId: pendingRequestId,
          action: pending.action,
          settlement: 'event-confirmed',
          playbackContextId: pending.payload.playbackContextId,
        });
        pending.resolve(cloneJson(message));
        break;
      }
    }

    _ingestContext(message) {
      let context = null;
      let completeSnapshot = false;
      if (message.action === 'playback.context.create') {
        context = message.payload;
        completeSnapshot = true;
      } else if (message.action === 'playback.context.status') {
        context = message.payload && message.payload.playbackContext;
        completeSnapshot = true;
      } else if (message.action === 'queue.context.sync') {
        context = message.payload;
        completeSnapshot = true;
      } else if (
        ['player.play', 'player.pause', 'player.seek', 'player.next', 'player.prev', 'queue.playItem'].includes(message.action)
      ) {
        context = message.payload;
      }
      if (!context || typeof context.playbackContextId !== 'string') {
        return true;
      }
      const current = this.contextCursors.get(context.playbackContextId) || {};
      const hasSnapshotVersion = Number.isInteger(context.epoch)
        && Number.isInteger(context.version);
      const hasCurrentVersion = Number.isInteger(current.epoch)
        && Number.isInteger(current.version);
      if (completeSnapshot && hasSnapshotVersion && hasCurrentVersion && (
        context.epoch < current.epoch
        || (context.epoch === current.epoch && context.version < current.version)
      )) {
        return false;
      }
      if (!completeSnapshot && (
        (Number.isInteger(context.controlVersion)
          && Number.isInteger(current.controlVersion)
          && context.controlVersion < current.controlVersion)
        || (Number.isInteger(context.queueRevision)
          && Number.isInteger(current.queueRevision)
          && context.queueRevision < current.queueRevision)
        || (hasSnapshotVersion && hasCurrentVersion && (
          context.epoch < current.epoch
          || (context.epoch === current.epoch && context.version < current.version)
        ))
      )) {
        return false;
      }
      const next = Object.assign({}, current);
      const advancesEpoch = hasSnapshotVersion && (
        !hasCurrentVersion || context.epoch > current.epoch
      );
      CONTEXT_CURSOR_FIELDS.forEach((field) => {
        if (
          Number.isInteger(context[field])
          && (advancesEpoch
            || (completeSnapshot && ['epoch', 'version'].includes(field))
            || !Number.isInteger(current[field])
            || context[field] >= current[field])
        ) {
          next[field] = context[field];
        }
      });
      if (Array.isArray(context.queueSongIds)) next.queueSongIds = context.queueSongIds.slice();
      if (Number.isInteger(context.currentIndex)) next.currentIndex = context.currentIndex;
      if (typeof context.state === 'string') next.state = context.state;
      if (Number.isInteger(context.positionMs)) next.positionMs = context.positionMs;
      if (typeof context.trackId === 'string') next.trackId = context.trackId;
      if (typeof context.authorityClientId === 'string') next.authorityClientId = context.authorityClientId;
      if (Number.isInteger(context.serverUpdatedAtMs)) next.serverUpdatedAtMs = context.serverUpdatedAtMs;
      this.contextCursors.set(context.playbackContextId, next);
      return true;
    }

    _ingestSubscriptionLifecycle(message) {
      if (!['playback.context.closed', 'playback.handoff.release'].includes(message.action)) {
        return;
      }
      const playbackContextId = message.payload && message.payload.playbackContextId;
      if (typeof playbackContextId === 'string') {
        this.forgetSubscription(playbackContextId);
      }
    }

    _isTerminalContextError(error) {
      return error instanceof StrictRequestError
        && ['context_closed', 'not_found'].includes(error.code);
    }

    request(action, payload, options) {
      const requestOptions = options || {};
      if (!Object.prototype.hasOwnProperty.call(ACTION_TYPES, action)) {
        return Promise.reject(new StrictProtocolError(`Unsupported strict-v2 action: ${action}`));
      }
      if (
        this.state !== 'ready'
        && !requestOptions.allowBeforeReady
        && !READY_BYPASS_ACTIONS.has(action)
      ) {
        return Promise.reject(new StrictProtocolError(`Action ${action} is forbidden before ready`));
      }
      if (!this.socket || !this.socket.connected) {
        return Promise.reject(new Error('Socket is not connected'));
      }
      const normalizedPayload = cloneJson(payload || {});
      const envelope = {
        type: ACTION_TYPES[action],
        action,
        requestId: requestOptions.requestId || requestId(action),
        payload: normalizedPayload,
        timestamp: Date.now() / 1000,
      };
      if (containsForbiddenSessionField(envelope)) {
        return Promise.reject(new StrictProtocolError('Strict request contains a forbidden session field'));
      }
      const fingerprint = stableStringify({
        type: envelope.type,
        action: envelope.action,
        payload: envelope.payload,
      });
      const existing = this.pending.get(envelope.requestId);
      if (existing) {
        if (existing.fingerprint !== fingerprint) {
          return Promise.reject(new StrictProtocolError('requestId retry payload fingerprint changed'));
        }
        this.socket.emit('message', cloneJson(existing.envelope));
        return existing.promise;
      }
      if (this.settled.has(envelope.requestId)) {
        return Promise.reject(new StrictProtocolError('requestId has already settled'));
      }

      let resolvePromise;
      let rejectPromise;
      const promise = new Promise((resolve, reject) => {
        resolvePromise = resolve;
        rejectPromise = reject;
      });
      const timeoutMs = Number.isFinite(requestOptions.timeoutMs)
        ? Math.max(1, requestOptions.timeoutMs)
        : 8000;
      const timer = setTimeout(() => {
        const pending = this.pending.get(envelope.requestId);
        if (!pending) return;
        this.pending.delete(envelope.requestId);
        this._rememberSettlement(envelope.requestId, {
          action,
          signature: null,
          timedOut: true,
        });
        pending.reject(new Error(`Strict-v2 request timed out: ${action}`));
      }, timeoutMs);
      this.pending.set(envelope.requestId, {
        action,
        payload: normalizedPayload,
        envelope: cloneJson(envelope),
        fingerprint,
        promise,
        resolve: resolvePromise,
        reject: rejectPromise,
        timer,
      });
      this._debug('request_sent', {
        requestId: envelope.requestId,
        action,
        playbackContextId: normalizedPayload.playbackContextId
          || normalizedPayload.sourcePlaybackContextId,
      });
      this.socket.emit('message', envelope);
      return promise;
    }

    retry(requestIdValue) {
      const pending = this.pending.get(requestIdValue);
      if (!pending) {
        return Promise.reject(new Error(`No pending request: ${requestIdValue}`));
      }
      this.socket.emit('message', cloneJson(pending.envelope));
      return pending.promise;
    }

    nextClientSeq() {
      this.clientSeq += 1;
      return this.clientSeq;
    }

    capability(name) {
      return Boolean(this.negotiatedCapabilities && this.negotiatedCapabilities[name]);
    }

    cursor(playbackContextId) {
      const value = this.contextCursors.get(playbackContextId);
      return value ? cloneJson(value) : null;
    }

    isCurrentContextSnapshot(context) {
      if (!context || typeof context.playbackContextId !== 'string') return false;
      const current = this.contextCursors.get(context.playbackContextId);
      if (
        !current
        || !Number.isInteger(context.epoch)
        || !Number.isInteger(context.version)
        || !Number.isInteger(current.epoch)
        || !Number.isInteger(current.version)
      ) {
        return true;
      }
      return context.epoch > current.epoch
        || (context.epoch === current.epoch && context.version >= current.version);
    }

    rememberSubscription(playbackContextId) {
      if (playbackContextId) this.subscriptions.add(playbackContextId);
    }

    forgetSubscription(playbackContextId) {
      this.subscriptions.delete(playbackContextId);
      this.contextCursors.delete(playbackContextId);
    }

    _mutateContext(playbackContextId, operation) {
      const previous = this.contextMutations.get(playbackContextId);
      let current;
      if (previous) {
        current = previous.catch(() => {}).then(operation);
      } else {
        try {
          current = Promise.resolve(operation());
        } catch (error) {
          current = Promise.reject(error);
        }
      }
      this.contextMutations.set(playbackContextId, current);
      return current.finally(() => {
        if (this.contextMutations.get(playbackContextId) === current) {
          this.contextMutations.delete(playbackContextId);
        }
      });
    }

    subscribe(playbackContextId) {
      return this._mutateContext(playbackContextId, async () => {
        if (this.subscriptions.has(playbackContextId)) {
          try {
            return await this.request('playback.context.status', { playbackContextId });
          } catch (error) {
            if (this._isTerminalContextError(error)) {
              this.forgetSubscription(playbackContextId);
            }
            throw error;
          }
        }

        let subscribed = false;
        try {
          await this.request('playback.context.subscribe', { playbackContextId });
          subscribed = true;
          const response = await this.request('playback.context.status', { playbackContextId });
          this.rememberSubscription(playbackContextId);
          return response;
        } catch (error) {
          this.forgetSubscription(playbackContextId);
          if (subscribed && this.socket && this.socket.connected) {
            await this.request('playback.context.unsubscribe', { playbackContextId }).catch(() => {});
          }
          throw error;
        }
      });
    }

    unsubscribe(playbackContextId) {
      return this._mutateContext(playbackContextId, async () => {
        if (!this.subscriptions.has(playbackContextId)) {
          this.forgetSubscription(playbackContextId);
          return null;
        }
        try {
          return await this.request('playback.context.unsubscribe', { playbackContextId });
        } finally {
          this.forgetSubscription(playbackContextId);
        }
      });
    }

    async refreshContext(playbackContextId) {
      try {
        return await this.request('playback.context.status', { playbackContextId });
      } catch (error) {
        if (this._isTerminalContextError(error)) {
          this.forgetSubscription(playbackContextId);
        }
        throw error;
      }
    }

    async fetchContextBindings() {
      const response = await this.fetchFn(this.options.contextBindingsUrl, {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { 'X-Requested-With': 'fetch' },
      });
      if (!response.ok) {
        throw new Error(`Context binding request failed (${response.status})`);
      }
      const payload = await response.json();
      const bindings = Array.isArray(payload && payload.bindings) ? payload.bindings : [];
      if (containsForbiddenSessionField(bindings)) {
        throw new StrictProtocolError('Context binding response contains a forbidden session field');
      }
      return bindings.filter((binding) => (
        binding
        && typeof binding.clientId === 'string'
        && typeof binding.deviceSessionId === 'string'
        && typeof binding.playbackContextId === 'string'
      ));
    }
  }

  class PlayerOwnerLock {
    constructor(options) {
      this.options = options || {};
      this.name = this.options.name || 'emosonic-web-player-owner';
      this.storage = this.options.storage || (typeof localStorage !== 'undefined' ? localStorage : null);
      this.navigator = this.options.navigator || (typeof navigator !== 'undefined' ? navigator : null);
      this.BroadcastChannel = this.options.BroadcastChannel
        || (typeof BroadcastChannel !== 'undefined' ? BroadcastChannel : null);
      this.now = this.options.now || (() => Date.now());
      this.leaseMs = this.options.leaseMs || 8000;
      this.heartbeatMs = this.options.heartbeatMs || 2500;
      this.acquireSettleMs = this.options.acquireSettleMs === undefined
        ? 25
        : Math.max(0, this.options.acquireSettleMs);
      this.ownerToken = this.options.ownerToken || uuid();
      this.acquired = false;
      this.releaseResolver = null;
      this.timer = null;
      this.channel = null;
    }

    async acquire() {
      if (this.navigator && this.navigator.locks && typeof this.navigator.locks.request === 'function') {
        return new Promise((resolve, reject) => {
          this.navigator.locks.request(this.name, { ifAvailable: true }, async (lock) => {
            if (!lock) {
              resolve(false);
              return;
            }
            this.acquired = true;
            resolve(true);
            await new Promise((release) => { this.releaseResolver = release; });
          }).catch(reject);
        });
      }
      return this._acquireLease();
    }

    async _acquireLease() {
      if (!this.storage) {
        this.acquired = true;
        return true;
      }
      const key = `${this.name}.lease`;
      let existing = null;
      try {
        existing = JSON.parse(this.storage.getItem(key) || 'null');
      } catch (error) {
        existing = null;
      }
      if (
        existing
        && existing.ownerToken !== this.ownerToken
        && Number(existing.expiresAt) > this.now()
      ) {
        return false;
      }
      this.storage.setItem(key, JSON.stringify({
        ownerToken: this.ownerToken,
        expiresAt: this.now() + this.leaseMs,
      }));
      if (this.acquireSettleMs) {
        await new Promise((resolve) => setTimeout(resolve, this.acquireSettleMs));
      }
      let confirmed = null;
      try {
        confirmed = JSON.parse(this.storage.getItem(key) || 'null');
      } catch (error) {
        confirmed = null;
      }
      if (!confirmed || confirmed.ownerToken !== this.ownerToken) {
        return false;
      }
      this.acquired = true;
      if (this.BroadcastChannel) {
        this.channel = new this.BroadcastChannel(this.name);
        this.channel.onmessage = (event) => {
          const incoming = event && event.data;
          if (
            this.acquired
            && incoming
            && incoming.type === 'owner-heartbeat'
            && incoming.ownerToken !== this.ownerToken
            && Number(incoming.expiresAt) > this.now()
          ) {
            this._verifyLeaseOwnership();
          }
        };
      }
      this._heartbeat();
      this.timer = setInterval(() => this._heartbeat(), this.heartbeatMs);
      return true;
    }

    _heartbeat() {
      if (!this.acquired || !this.storage) return;
      if (!this._verifyLeaseOwnership()) return;
      const value = {
        ownerToken: this.ownerToken,
        expiresAt: this.now() + this.leaseMs,
      };
      this.storage.setItem(`${this.name}.lease`, JSON.stringify(value));
      if (this.channel) this.channel.postMessage({ type: 'owner-heartbeat', ...value });
    }

    _verifyLeaseOwnership() {
      if (!this.storage) return true;
      let existing = null;
      try {
        existing = JSON.parse(this.storage.getItem(`${this.name}.lease`) || 'null');
      } catch (error) {
        existing = null;
      }
      if (
        existing
        && existing.ownerToken !== this.ownerToken
        && Number(existing.expiresAt) > this.now()
      ) {
        this.release();
        if (typeof this.options.onLost === 'function') this.options.onLost(existing);
        return false;
      }
      return true;
    }

    release() {
      if (!this.acquired) return;
      this.acquired = false;
      if (this.timer) clearInterval(this.timer);
      this.timer = null;
      if (this.storage) {
        const key = `${this.name}.lease`;
        try {
          const existing = JSON.parse(this.storage.getItem(key) || 'null');
          if (existing && existing.ownerToken === this.ownerToken) {
            this.storage.removeItem(key);
          }
        } catch (error) {
          this.storage.removeItem(key);
        }
      }
      if (this.channel) this.channel.close();
      this.channel = null;
      if (this.releaseResolver) this.releaseResolver();
      this.releaseResolver = null;
    }
  }

  function stableIdentity(storage, key, prefix) {
    const existing = storage.getItem(key);
    if (existing) return existing;
    const created = `${prefix}${uuid()}`;
    storage.setItem(key, created);
    return created;
  }

  return {
    ACTION_TYPES,
    CAPABILITY_NAMES,
    DIRECT_RESPONSE_ACTIONS,
    EVENT_CONFIRMED_ACTIONS,
    PlayerOwnerLock,
    StrictProtocolError,
    StrictRequestError,
    StrictV2Client,
    containsForbiddenSessionField,
    stableIdentity,
    stableStringify,
    validateQueue,
    uuid,
  };
}));
