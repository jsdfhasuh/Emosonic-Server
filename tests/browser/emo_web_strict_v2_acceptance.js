'use strict';

const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { chromium, firefox } = require('playwright');

const ROOT = path.resolve(__dirname, '..', '..');
const PORT = Number(process.env.EMO_BROWSER_PORT || 5081);
const BASE_URL = `http://127.0.0.1:${PORT}`;
const PYTHON = process.env.PYTHON || 'python';
const STATE_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'emosonic-web-strict-v2-'));
const serverLogs = [];

function logStep(step) {
  process.stdout.write(`[browser-acceptance] ${step}\n`);
}

function observePageErrors(page, label, errors) {
  page.on('pageerror', (error) => {
    const detail = {
      page: label,
      message: error.message,
      stack: error.stack || null,
    };
    errors.push(detail);
    logStep(`page error on ${label}: ${error.message}`);
  });
}

function startServer() {
  const child = spawn(PYTHON, [
    'script/serve_emo_web_strict_v2_acceptance.py',
    '--state-dir',
    STATE_DIR,
    '--port',
    String(PORT),
  ], {
    cwd: ROOT,
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  for (const stream of [child.stdout, child.stderr]) {
    stream.on('data', (chunk) => {
      serverLogs.push(chunk.toString());
      if (serverLogs.length > 200) serverLogs.shift();
    });
  }
  return child;
}

async function stopServer(child) {
  if (!child || child.exitCode !== null) return;
  child.kill('SIGTERM');
  await Promise.race([
    new Promise((resolve) => child.once('exit', resolve)),
    new Promise((resolve) => setTimeout(resolve, 5000)),
  ]);
  if (child.exitCode === null) child.kill('SIGKILL');
}

async function waitForServer(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const status = await new Promise((resolve, reject) => {
        const request = http.get(`${BASE_URL}/user/login`, (response) => {
          response.resume();
          resolve(response.statusCode);
        });
        request.setTimeout(1000, () => request.destroy(new Error('timeout')));
        request.on('error', reject);
      });
      if (status < 500) return;
    } catch (error) {
      // The server is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error(`Acceptance server did not start:\n${serverLogs.join('').slice(-4000)}`);
}

async function login(page) {
  await page.goto(`${BASE_URL}/user/login`, { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="user"]').fill('alice');
  await page.locator('input[name="password"]').fill('Alic3');
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'domcontentloaded' }),
    page.locator('button[type="submit"]').click(),
  ]);
}

async function waitReady(page, selector, label = selector) {
  try {
    await page.waitForFunction((id) => document.querySelector(id)?.textContent === 'ready', selector, {
      timeout: 30000,
    });
  } catch (error) {
    const detail = await page.evaluate((id) => ({
      state: document.querySelector(id)?.textContent || null,
      playerError: document.querySelector('#strict-player-error')?.textContent || null,
      controlError: document.querySelector('#strict-control-error')?.textContent || null,
      url: location.href,
    }), selector).catch(() => ({ url: page.url() }));
    throw new Error(`${label} did not reach ready: ${JSON.stringify(detail)}`);
  }
  await page.waitForFunction(() => Boolean(window.__emoStrictV2Acceptance), null, {
    timeout: 10000,
  });
}

async function openStrictPage(page, route, stateSelector, label) {
  await login(page);
  await page.goto(`${BASE_URL}${route}`, { waitUntil: 'domcontentloaded' });
  await waitReady(page, stateSelector, label);
}

async function acceptanceSnapshot(page) {
  return page.evaluate(() => window.__emoStrictV2Acceptance.snapshot());
}

async function clickEnabled(page, selector) {
  await page.waitForFunction((value) => {
    const element = document.querySelector(value);
    return Boolean(element && !element.disabled);
  }, selector, { timeout: 15000 });
  await page.locator(selector).evaluate((element) => element.click());
}

async function startHandoffTo(control, targetClientId) {
  await control.waitForFunction((clientId) => (
    Array.from(document.querySelectorAll('#strict-handoff-target option'))
      .some((option) => option.value === clientId)
  ), targetClientId, { timeout: 15000 });
  await control.evaluate((clientId) => {
    const select = document.querySelector('#strict-handoff-target');
    const button = document.querySelector('#strict-handoff-start');
    select.value = clientId;
    select.dispatchEvent(new Event('change', { bubbles: true }));
    if (button.disabled) throw new Error('Handoff start remained disabled');
    button.click();
  }, targetClientId);
}

async function startFollowTo(player, sourceContextId) {
  await player.waitForFunction((contextId) => (
    Array.from(document.querySelectorAll('#strict-follow-source option'))
      .some((option) => option.value === contextId)
  ), sourceContextId, { timeout: 15000 });
  await player.evaluate((contextId) => {
    const select = document.querySelector('#strict-follow-source');
    const button = document.querySelector('#strict-follow-start');
    select.value = contextId;
    select.dispatchEvent(new Event('change', { bubbles: true }));
    if (button.disabled) throw new Error('Follow start remained disabled');
    button.click();
  }, sourceContextId);
}

async function runBroadcastMutation(control, selector, priorVersion) {
  await clickEnabled(control, selector);
  await control.waitForFunction(() => (
    !window.__emoStrictV2Acceptance.snapshot().broadcastBusy
  ), null, { timeout: 15000 });
  const snapshot = await acceptanceSnapshot(control);
  assert.ok(
    snapshot.broadcastControlVersion > priorVersion,
    JSON.stringify({
      selector,
      priorVersion,
      snapshot,
      error: await control.textContent('#strict-control-error'),
    }),
  );
  return snapshot.broadcastControlVersion;
}

async function addTracks(page, count) {
  await page.waitForSelector('#strict-search-results li button', { timeout: 15000 });
  for (let index = 0; index < count; index += 1) {
    await page.locator('#strict-search-results li button').nth(index).click();
    await page.waitForTimeout(150);
  }
  await page.waitForFunction(
    (expected) => document.querySelectorAll('#strict-player-queue li').length === expected,
    count,
    { timeout: 15000 },
  );
  await page.waitForFunction(() => (
    window.__emoStrictV2Acceptance.snapshot().contextId
  ), null, { timeout: 15000 });
}

async function selectControlPlayer(control, clientId) {
  await control.waitForFunction((value) => (
    Array.from(document.querySelectorAll('#strict-device-list .strict-device'))
      .some((button) => button.textContent.includes(value))
  ), clientId, { timeout: 20000 });
  const bindings = await contextBindings(control);
  const binding = bindings.find((candidate) => candidate.clientId === clientId);
  assert.ok(binding, `No browser Context binding for ${clientId}`);
  const device = control.locator('#strict-device-list .strict-device').filter({ hasText: clientId });
  await device.evaluate((button) => button.click());
  await control.waitForFunction(({ expectedClientId, expectedContextId }) => {
    const snapshot = window.__emoStrictV2Acceptance.snapshot();
    return snapshot.selectedClientId === expectedClientId
      && snapshot.selectedContextId === expectedContextId
      && Number.isInteger(snapshot.controlVersion);
  }, {
    expectedClientId: clientId,
    expectedContextId: binding.playbackContextId,
  }, { timeout: 15000 });
}

async function waitHandoff(control, status, previousHandoffId = null) {
  try {
    await control.waitForFunction((previous) => {
      const snapshot = window.__emoStrictV2Acceptance.snapshot();
      return ['completed', 'failed', 'cancelled', 'timedOut'].includes(snapshot.handoffStatus)
        && (!previous || snapshot.handoffId !== previous);
    }, previousHandoffId, { timeout: 20000 });
  } catch (error) {
    throw new Error(`Handoff did not become terminal: ${JSON.stringify({
      expected: status,
      previousHandoffId,
      snapshot: await acceptanceSnapshot(control),
      summary: await control.textContent('#strict-handoff-summary'),
      error: await control.textContent('#strict-control-error'),
    })}`);
  }
  const snapshot = await acceptanceSnapshot(control);
  assert.equal(
    snapshot.handoffStatus,
    status,
    JSON.stringify({
      snapshot,
      summary: await control.textContent('#strict-handoff-summary'),
      error: await control.textContent('#strict-control-error'),
    }),
  );
  return snapshot;
}

async function contextBindings(page) {
  return page.evaluate(async () => {
    const response = await fetch('/emo/web-context-bindings', {
      credentials: 'same-origin',
      cache: 'no-store',
    });
    return (await response.json()).bindings;
  });
}

async function acceptanceServerState(page) {
  return page.evaluate(async () => {
    const response = await fetch('/emo/web-strict-v2-acceptance-state', {
      credentials: 'same-origin',
      cache: 'no-store',
    });
    return response.json();
  });
}

async function waitAuthority(page, expectedClientId) {
  await page.waitForFunction(async (clientId) => {
    const response = await fetch('/emo/web-context-bindings', {
      credentials: 'same-origin',
      cache: 'no-store',
    });
    const payload = await response.json();
    return payload.bindings.length === 1 && payload.bindings[0].clientId === clientId;
  }, expectedClientId, { timeout: 20000 });
}

async function run() {
  let server = startServer();
  let chromiumBrowser;
  let firefoxBrowser;
  const pageErrors = [];
  const completedSteps = [];
  try {
    await waitForServer();
    chromiumBrowser = await chromium.launch({
      headless: true,
      args: ['--autoplay-policy=user-gesture-required'],
    });
    firefoxBrowser = await firefox.launch({
      headless: true,
      firefoxUserPrefs: { 'media.autoplay.default': 1 },
    });

    const playerOneContext = await chromiumBrowser.newContext();
    const playerTwoContext = await firefoxBrowser.newContext();
    const mobileControlContext = await chromiumBrowser.newContext({
      viewport: { width: 390, height: 844 },
      userAgent: 'EmoSonicAcceptanceMobile/1.0',
      isMobile: true,
      hasTouch: true,
    });
    const playerOne = await playerOneContext.newPage();
    const playerTwo = await playerTwoContext.newPage();
    const control = await mobileControlContext.newPage();
    observePageErrors(playerOne, 'Chromium player', pageErrors);
    observePageErrors(playerTwo, 'Firefox player', pageErrors);
    observePageErrors(control, 'mobile Chromium control', pageErrors);

    logStep('open Chromium player, Firefox player, and mobile Chromium control');
    await openStrictPage(playerOne, '/player', '#strict-connection-state', 'Chromium player');
    await openStrictPage(playerTwo, '/player', '#strict-connection-state', 'Firefox player');
    await openStrictPage(control, '/control', '#strict-control-connection', 'mobile Chromium control');
    const [playerOneIdentity, playerTwoIdentity, controlIdentity] = await Promise.all([
      playerOne.evaluate(() => ({
        clientId: window.__emoStrictV2Acceptance.clientId,
        deviceSessionId: window.__emoStrictV2Acceptance.deviceSessionId,
      })),
      playerTwo.evaluate(() => ({
        clientId: window.__emoStrictV2Acceptance.clientId,
        deviceSessionId: window.__emoStrictV2Acceptance.deviceSessionId,
      })),
      control.evaluate(() => ({
        clientId: window.__emoStrictV2Acceptance.clientId,
        deviceSessionId: window.__emoStrictV2Acceptance.deviceSessionId,
      })),
    ]);
    assert.notEqual(playerOneIdentity.clientId, playerTwoIdentity.clientId);
    const mobileLayout = await control.evaluate(() => ({
      innerWidth: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      offenders: Array.from(document.querySelectorAll('body *'))
        .map((element) => ({
          tag: element.tagName,
          id: element.id || null,
          className: typeof element.className === 'string' ? element.className : null,
          right: Math.round(element.getBoundingClientRect().right),
          width: Math.round(element.getBoundingClientRect().width),
        }))
        .filter((item) => item.right > window.innerWidth + 2)
        .sort((left, right) => right.right - left.right)
        .slice(0, 12),
    }));
    assert.ok(
      mobileLayout.scrollWidth <= mobileLayout.innerWidth + 2,
      JSON.stringify(mobileLayout),
    );
    completedSteps.push('chromium-firefox-mobile-bootstrap');

    logStep('select an online player without a Context and verify diagnostics');
    await control.waitForFunction((clientId) => (
      document.querySelector('#strict-device-list')?.textContent.includes(clientId)
    ), playerOneIdentity.clientId, { timeout: 20000 });
    const contextlessDevice = control.locator('#strict-device-list .strict-device')
      .filter({ hasText: playerOneIdentity.clientId });
    await contextlessDevice.evaluate((button) => button.click());
    await control.waitForFunction((clientId) => {
      const snapshot = window.__emoStrictV2Acceptance.snapshot();
      return snapshot.selectedClientId === clientId && snapshot.selectedContextId === null;
    }, playerOneIdentity.clientId, { timeout: 10000 });
    assert.match(await control.textContent('#strict-context-empty-message'), /PlaybackContext/);
    assert.equal(await control.locator('#strict-context-empty').isVisible(), true);
    assert.equal(await control.locator('#strict-context-active').isVisible(), false);
    assert.equal(await control.locator('[data-control="player.play"]').isDisabled(), true);
    completedSteps.push('contextless-player-diagnostics');

    logStep('create source Context and verify duplicate-queue UI');
    await addTracks(playerOne, 2);
    const sourceContextId = (await acceptanceSnapshot(playerOne)).contextId;
    await playerOne.locator('#strict-search-results li button').first().click();
    await playerOne.waitForFunction(() => (
      document.querySelector('#strict-player-error')?.textContent.includes('不支持重复曲目')
    ));
    completedSteps.push('context-create-duplicate-policy');

    logStep('verify same-browser secondary tab owner lock');
    const secondaryPlayer = await playerOneContext.newPage();
    observePageErrors(secondaryPlayer, 'secondary Chromium player', pageErrors);
    await secondaryPlayer.goto(`${BASE_URL}/player`, { waitUntil: 'domcontentloaded' });
    await secondaryPlayer.waitForFunction(() => (
      document.querySelector('#strict-connection-state')?.textContent === 'secondary tab'
    ));
    await secondaryPlayer.close();
    completedSteps.push('single-tab-owner-lock');

    logStep('verify refresh preserves player identity and Context binding');
    await playerOne.reload({ waitUntil: 'domcontentloaded' });
    await waitReady(playerOne, '#strict-connection-state');
    assert.equal(windowValue(await playerOne.textContent('#strict-client-id')), playerOneIdentity.clientId);
    assert.equal((await acceptanceSnapshot(playerOne)).contextId, sourceContextId);
    completedSteps.push('refresh-identity-context-restore');

    await control.waitForFunction((clientId) => (
      document.querySelector('#strict-device-list')?.textContent.includes(clientId)
    ), playerTwoIdentity.clientId, { timeout: 20000 });
    await selectControlPlayer(control, playerOneIdentity.clientId);

    logStep('verify Handoff rejects target without a user gesture');
    const noGesturePrior = (await acceptanceSnapshot(control)).handoffId;
    await startHandoffTo(control, playerTwoIdentity.clientId);
    const noGesture = await waitHandoff(control, 'failed', noGesturePrior);
    assert.match(await control.textContent('#strict-handoff-summary'), /autoplay_blocked/);
    assert.equal(noGesture.handoffStatus, 'failed');
    completedSteps.push('handoff-no-gesture-rejection');

    logStep('create second player Context and authorize real audio playback');
    await addTracks(playerTwo, 2);
    const playerTwoOriginalContext = (await acceptanceSnapshot(playerTwo)).contextId;
    await playerOne.locator('#strict-play').click();
    await playerTwo.locator('#strict-play').click();
    await Promise.all([
      playerOne.waitForFunction(() => !document.querySelector('#strict-player-audio').paused),
      playerTwo.waitForFunction(() => !document.querySelector('#strict-player-audio').paused),
    ]);
    completedSteps.push('local-audio-user-gesture');

    logStep('verify remote control and stale cursor refresh/retry');
    await selectControlPlayer(control, playerOneIdentity.clientId);
    await clickEnabled(control, '[data-control="player.pause"]');
    await playerOne.waitForFunction(() => document.querySelector('#strict-player-audio').paused);
    const priorRemotePlayError = await playerOne.textContent('#strict-player-error');
    await clickEnabled(control, '[data-control="player.play"]');
    try {
      await playerOne.waitForFunction((priorError) => {
        const audioElement = document.querySelector('#strict-player-audio');
        const errorElement = document.querySelector('#strict-player-error');
        return !audioElement.paused
          || (!errorElement.classList.contains('d-none') && errorElement.textContent !== priorError);
      }, priorRemotePlayError, { timeout: 15000 });
      assert.equal(await playerOne.evaluate(() => (
        document.querySelector('#strict-player-audio').paused
      )), false);
    } catch (error) {
      const detail = await playerOne.evaluate(() => ({
        snapshot: window.__emoStrictV2Acceptance.snapshot(),
        playerError: document.querySelector('#strict-player-error')?.textContent || null,
        audio: {
          paused: document.querySelector('#strict-player-audio').paused,
          readyState: document.querySelector('#strict-player-audio').readyState,
          currentSrc: document.querySelector('#strict-player-audio').currentSrc,
        },
        userActivation: navigator.userActivation ? {
          hasBeenActive: navigator.userActivation.hasBeenActive,
          isActive: navigator.userActivation.isActive,
        } : null,
      }));
      throw new Error(`Remote play did not start: ${JSON.stringify({
        detail,
        control: await acceptanceSnapshot(control),
        controlError: await control.textContent('#strict-control-error'),
      })}`);
    }
    const controlBeforeStale = await acceptanceSnapshot(control);
    await control.evaluate((version) => {
      window.__emoStrictV2Acceptance.setControlVersion(Math.max(0, version - 1));
    }, controlBeforeStale.controlVersion);
    await clickEnabled(control, '[data-control="player.pause"]');
    await playerOne.waitForFunction(() => document.querySelector('#strict-player-audio').paused);
    assert.match(await control.textContent('#strict-control-log'), /player.pause accepted/);
    completedSteps.push('remote-control-stale-cursor-recovery');

    logStep('verify real capability_required error UI');
    const capabilityError = await control.evaluate(async (playbackContextId) => (
      window.__emoStrictV2Acceptance.requestExpectError('follow.start', {
        sourcePlaybackContextId: playbackContextId,
        deviceSessionId: window.__emoStrictV2Acceptance.deviceSessionId,
      })
    ), sourceContextId);
    assert.equal(capabilityError.code, 'capability_required');
    assert.match(await control.textContent('#strict-control-error'), /capability_required/);
    completedSteps.push('capability-required-ui');

    logStep('verify player-owned Follow continuity and source network loss cleanup');
    await playerOne.locator('#strict-play').click();
    await playerOne.waitForFunction(() => !document.querySelector('#strict-player-audio').paused);
    await playerTwo.locator('#strict-follow-refresh').click();
    await startFollowTo(playerTwo, sourceContextId);
    await playerTwo.waitForFunction((contextId) => (
      window.__emoStrictV2Acceptance.snapshot().followingContextId === contextId
      && !document.querySelector('#strict-player-audio').paused
    ), sourceContextId, { timeout: 15000 });
    await playerTwo.waitForTimeout(600);
    const followStartPosition = await playerTwo.evaluate(() => {
      const audioElement = document.querySelector('#strict-player-audio');
      window.__emoFollowSeekEvents = [];
      audioElement.addEventListener('seeking', () => {
        window.__emoFollowSeekEvents.push(audioElement.currentTime);
      });
      return audioElement.currentTime;
    });
    await playerTwo.waitForTimeout(4500);
    const followTrace = await playerTwo.evaluate(() => ({
      currentTime: document.querySelector('#strict-player-audio').currentTime,
      seekEvents: window.__emoFollowSeekEvents.slice(),
    }));
    assert.ok(followTrace.currentTime > followStartPosition + 3, JSON.stringify({
      followStartPosition,
      followTrace,
    }));
    assert.deepEqual(followTrace.seekEvents, []);
    const authorityCursor = (await acceptanceSnapshot(control)).controlVersion;
    await playerOneContext.setOffline(true);
    await playerTwo.waitForFunction(() => (
      document.querySelector('#strict-follow-state')?.textContent.includes('source offline')
    ), null, { timeout: 25000 });
    const offlineError = await control.evaluate(async ({ playbackContextId, controlVersion }) => (
      window.__emoStrictV2Acceptance.requestExpectError('player.play', {
        playbackContextId,
        baseControlVersion: controlVersion,
      })
    ), { playbackContextId: sourceContextId, controlVersion: authorityCursor });
    assert.equal(offlineError.code, 'authority_offline');
    assert.match(await control.textContent('#strict-control-error'), /authority_offline/);
    await playerOneContext.setOffline(false);
    await waitReady(playerOne, '#strict-connection-state');
    assert.equal((await acceptanceSnapshot(playerOne)).contextId, sourceContextId);
    completedSteps.push('follow-source-offline-authority-error');

    logStep('restart server and verify automatic strict reconnect without identity changes');
    await stopServer(server);
    await Promise.all([
      playerOne.waitForFunction(() => (
        document.querySelector('#strict-connection-state')?.textContent !== 'ready'
      )),
      playerTwo.waitForFunction(() => (
        document.querySelector('#strict-connection-state')?.textContent !== 'ready'
      )),
      control.waitForFunction(() => (
        document.querySelector('#strict-control-connection')?.textContent !== 'ready'
      )),
    ]);
    server = startServer();
    await waitForServer();
    await Promise.all([
      waitReady(playerOne, '#strict-connection-state'),
      waitReady(playerTwo, '#strict-connection-state'),
      waitReady(control, '#strict-control-connection'),
    ]);
    assert.equal(await playerOne.textContent('#strict-client-id'), playerOneIdentity.clientId);
    assert.equal(await playerTwo.textContent('#strict-client-id'), playerTwoIdentity.clientId);
    assert.equal(await control.textContent('#strict-control-client-id'), controlIdentity.clientId);
    await Promise.all([
      playerOne.waitForFunction((contextId) => (
        window.__emoStrictV2Acceptance.snapshot().contextId === contextId
      ), sourceContextId),
      playerTwo.waitForFunction((contextId) => (
        window.__emoStrictV2Acceptance.snapshot().contextId === contextId
      ), playerTwoOriginalContext),
      waitAuthority(playerOne, playerOneIdentity.clientId),
      waitAuthority(playerTwo, playerTwoIdentity.clientId),
    ]);
    completedSteps.push('server-restart-reconnect');

    logStep('verify strict Broadcast with two real browser players');
    await selectControlPlayer(control, playerTwoIdentity.clientId);
    await selectControlPlayer(control, playerOneIdentity.clientId);
    await control.waitForFunction(async ({ playbackContextId, clientId }) => {
      const response = await fetch('/emo/web-strict-v2-acceptance-state', {
        credentials: 'same-origin',
        cache: 'no-store',
      });
      const payload = await response.json();
      return payload.contexts.some((context) => (
        context.playbackContextId === playbackContextId
        && context.authorityClientId === clientId
        && context.authorityClientPresent
        && context.authoritySidPresent
      ));
    }, {
      playbackContextId: sourceContextId,
      clientId: playerOneIdentity.clientId,
    }, { timeout: 20000 });
    const priorBroadcastError = await control.textContent('#strict-control-error');
    await clickEnabled(control, '#strict-broadcast-start');
    try {
      await control.waitForFunction((priorError) => {
        const snapshot = window.__emoStrictV2Acceptance.snapshot();
        const errorText = document.querySelector('#strict-control-error')?.textContent || '';
        return Boolean(snapshot.broadcastId) || errorText !== priorError;
      }, priorBroadcastError, { timeout: 15000 });
      assert.ok((await acceptanceSnapshot(control)).broadcastId);
      await Promise.all([
        playerOne.waitForFunction(() => window.__emoStrictV2Acceptance.snapshot().activeBroadcastId),
        playerTwo.waitForFunction(() => window.__emoStrictV2Acceptance.snapshot().activeBroadcastId),
      ]);
    } catch (error) {
      const detail = {
        control: await acceptanceSnapshot(control),
        playerOne: await acceptanceSnapshot(playerOne),
        playerTwo: await acceptanceSnapshot(playerTwo),
        server: await acceptanceServerState(control),
        controlError: await control.textContent('#strict-control-error'),
        broadcastSummary: await control.textContent('#strict-broadcast-summary'),
      };
      throw new Error(`Broadcast did not reach both players: ${JSON.stringify(detail)}`);
    }
    let broadcastVersion = (await acceptanceSnapshot(control)).broadcastControlVersion;
    broadcastVersion = await runBroadcastMutation(
      control, '[data-broadcast="broadcast.play"]', broadcastVersion,
    );
    broadcastVersion = await runBroadcastMutation(
      control, '[data-broadcast="broadcast.pause"]', broadcastVersion,
    );
    broadcastVersion = await runBroadcastMutation(
      control, '[data-broadcast="broadcast.play"]', broadcastVersion,
    );
    broadcastVersion = await runBroadcastMutation(
      control, '#strict-broadcast-seek-forward', broadcastVersion,
    );
    broadcastVersion = await runBroadcastMutation(
      control, '#strict-broadcast-queue-sync', broadcastVersion,
    );
    await clickEnabled(control, '[data-broadcast="broadcast.stop"]');
    await Promise.all([
      playerOne.waitForFunction(() => !window.__emoStrictV2Acceptance.snapshot().activeBroadcastId),
      playerTwo.waitForFunction(() => !window.__emoStrictV2Acceptance.snapshot().activeBroadcastId),
    ]);
    completedSteps.push('two-browser-broadcast');

    logStep('verify clear queue creates a new Context, then prepare Handoff target');
    await playerTwo.locator('#strict-clear-queue').click();
    await playerTwo.waitForFunction(() => !window.__emoStrictV2Acceptance.snapshot().contextId);
    await addTracks(playerTwo, 1);
    const replacementContext = (await acceptanceSnapshot(playerTwo)).contextId;
    assert.notEqual(replacementContext, playerTwoOriginalContext);
    await playerTwo.locator('#strict-play').click();
    await playerTwo.waitForFunction(() => (
      !document.querySelector('#strict-player-audio').paused
    ), null, { timeout: 15000 });
    await playerTwo.locator('#strict-clear-queue').click();
    await playerTwo.waitForFunction(() => (
      !window.__emoStrictV2Acceptance.snapshot().contextId
      && document.querySelector('#strict-player-audio').paused
    ));
    completedSteps.push('close-and-new-context-id');

    logStep('verify hidden target rejects Handoff before committing');
    await playerTwo.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true,
        get: () => 'hidden',
      });
    });
    await selectControlPlayer(control, playerOneIdentity.clientId);
    const hiddenPrior = (await acceptanceSnapshot(control)).handoffId;
    await startHandoffTo(control, playerTwoIdentity.clientId);
    await waitHandoff(control, 'failed', hiddenPrior);
    assert.match(await control.textContent('#strict-handoff-summary'), /page_hidden/);
    await playerTwo.evaluate(() => { delete document.visibilityState; });
    assert.equal(await playerTwo.evaluate(() => document.visibilityState), 'visible');
    completedSteps.push('handoff-hidden-rejection');

    logStep('collect 30 foreground Handoff timing samples');
    for (let sample = 0; sample < 30; sample += 1) {
      logStep(`handoff timing sample ${sample + 1}/30`);
      const bindings = await contextBindings(control);
      assert.equal(bindings.length, 1);
      const authorityClientId = bindings[0].clientId;
      const targetClientId = authorityClientId === playerOneIdentity.clientId
        ? playerTwoIdentity.clientId
        : playerOneIdentity.clientId;
      const targetPage = targetClientId === playerOneIdentity.clientId ? playerOne : playerTwo;
      await selectControlPlayer(control, authorityClientId);
      const previous = (await acceptanceSnapshot(control)).handoffId;
      await startHandoffTo(control, targetClientId);
      try {
        await waitHandoff(control, 'completed', previous);
      } catch (error) {
        const targetDetail = await targetPage.evaluate(() => ({
          snapshot: window.__emoStrictV2Acceptance.snapshot(),
          visibilityState: document.visibilityState,
          playerError: document.querySelector('#strict-player-error')?.textContent || null,
          audio: {
            paused: document.querySelector('#strict-player-audio').paused,
            readyState: document.querySelector('#strict-player-audio').readyState,
            currentSrc: document.querySelector('#strict-player-audio').currentSrc,
          },
        }));
        throw new Error(`${error.message}; target=${JSON.stringify(targetDetail)}`);
      }
      await waitAuthority(control, targetClientId);
      const oldSourcePage = targetPage === playerOne ? playerTwo : playerOne;
      await targetPage.waitForFunction((expected) => (
        window.__emoStrictV2Acceptance.handoffSamples.length >= expected
      ), Math.floor(sample / 2) + 1, { timeout: 15000 });
      assert.equal((await acceptanceSnapshot(oldSourcePage)).contextId, null);
    }
    const handoffSamples = [
      ...(await playerOne.evaluate(() => window.__emoStrictV2Acceptance.handoffSamples)),
      ...(await playerTwo.evaluate(() => window.__emoStrictV2Acceptance.handoffSamples)),
    ];
    assert.equal(handoffSamples.length, 30);
    assert.ok(handoffSamples.every((sample) => sample.absoluteErrorMs <= 200));
    completedSteps.push('handoff-30-sample-timing');

    logStep('verify protocol error UI remains strict and never falls back');
    await control.evaluate(() => window.__emoStrictV2Acceptance.forceProtocolError());
    await control.waitForFunction(() => (
      document.querySelector('#strict-control-error')?.textContent.includes('protocol error')
    ));
    assert.match(await control.textContent('body'), /PlaybackContext strict-v2 2\.1\.0/);
    assert.doesNotMatch(await control.textContent('body'), /Local queue editor/);
    completedSteps.push('protocol-error-no-fallback');

    assert.deepEqual(pageErrors, []);
    const errors = handoffSamples.map((sample) => sample.absoluteErrorMs).sort((a, b) => a - b);
    const mean = errors.reduce((sum, value) => sum + value, 0) / errors.length;
    const result = {
      baseUrl: BASE_URL,
      chromiumVersion: await chromiumBrowser.version(),
      firefoxVersion: await firefoxBrowser.version(),
      mobileViewport: mobileLayout,
      identities: {
        playerOne: playerOneIdentity,
        playerTwo: playerTwoIdentity,
        control: controlIdentity,
      },
      completedSteps,
      handoffTiming: {
        sampleCount: errors.length,
        maximumAbsoluteErrorMs: Math.max(...errors),
        meanAbsoluteErrorMs: Number(mean.toFixed(3)),
        p95AbsoluteErrorMs: errors[Math.ceil(errors.length * 0.95) - 1],
        allWithin200Ms: errors.every((value) => value <= 200),
        samples: handoffSamples,
      },
    };
    process.stdout.write(`BROWSER_ACCEPTANCE_RESULT=${JSON.stringify(result)}\n`);
  } finally {
    await Promise.allSettled([
      chromiumBrowser ? chromiumBrowser.close() : Promise.resolve(),
      firefoxBrowser ? firefoxBrowser.close() : Promise.resolve(),
    ]);
    await stopServer(server);
    fs.rmSync(STATE_DIR, { recursive: true, force: true });
  }
}

function windowValue(value) {
  return String(value || '').trim();
}

run().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  if (serverLogs.length) {
    process.stderr.write(`\nLast server output:\n${serverLogs.join('').slice(-8000)}\n`);
  }
  process.exitCode = 1;
});
