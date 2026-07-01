# Flutter EffectiveAt Playback v2

This document describes the Flutter/player-side contract required by the
server effective-at playback rollout in
`ref/2026-07-01-server-effective-at-playback.md`.

It does not replace the v1 follow or broadcast documents for UI state and
queue ownership. It adds the timing contract for clients that opt in.

## Capability Opt-In

Only advertise these flags after the player can obey the timing contract:

```json
{
  "type": "device",
  "action": "device.register",
  "payload": {
    "clientId": "phone-1",
    "roles": ["player"],
    "sessionId": "root:phone",
    "capabilities": {
      "effectiveAtPlayback": true,
      "playbackPrepare": true
    }
  }
}
```

`effectiveAtPlayback` means the client can delay audible playback until
`effectiveAtServerMs`.

`playbackPrepare` means the client can receive `playback.prepare`, preload the
track or queue, seek to `positionMs`, remain paused, and then emit
`playback.ready`.

Do not set either flag in the Web Player until it implements this behavior.

## Server Clock

Keep sampling server time through `system.ping` / `system.pong` and state
messages that include `payload.serverTimeMs`.

```text
serverNowMs = localNowMs + serverClockOffsetMs
delayMs = effectiveAtServerMs - serverNowMs
```

If `delayMs` is positive, schedule playback for that delay. If it is already
near or below zero, start immediately and report the observed lateness in logs
or telemetry.

## Two-Phase Prepare

On `playback.prepare`:

1. Validate `prepareId`, `timelineId`, and `controlVersion`.
2. Load `queueSongIds`.
3. Set `currentIndex` and resolve `trackId`.
4. Seek to `positionMs`.
5. Stay paused.
6. Send `playback.ready`.

Ready example:

```json
{
  "type": "event",
  "action": "playback.ready",
  "requestId": "ready-prep-1",
  "payload": {
    "prepareId": "prep-123",
    "clientId": "phone-1",
    "ready": true,
    "positionMs": 0,
    "controlVersion": 12
  }
}
```

If loading or seeking fails, send `ready: false` with `errorCode` and
`errorMessage`.

Ignore old prepare data locally after a newer `controlVersion` or `timelineId`
commit has been applied.

## Commit Handling

The server commits with a normal state or command payload:

- `playback.update` for source/follow playback timelines.
- `broadcast.start`, `broadcast.play`, and `broadcast.playItem` for broadcast
  timelines.
- Targeted `player.play` with the same `effectiveAtServerMs` for source players
  that still need a command trigger.

When payload state is `playing` and `effectiveAtServerMs` is present:

1. Apply queue, track, position, version, epoch, and controlVersion locally.
2. Keep audio paused until the calculated server-time delay expires.
3. At that instant, start playback from `positionMs`.
4. Send normal `playback.update` feedback after playback starts.

When state is `paused` or `stopped`, act immediately and ignore any old pending
effective-at timer for the same timeline.

## Follow Playback

Followers are non-required prepare targets. They should still send
`playback.ready`, but the server may commit after the source is ready and the
prepare timeout expires.

Follower feedback remains local execution feedback. It must not be treated as
source authority:

```json
{
  "type": "event",
  "action": "playback.update",
  "payload": {
    "sessionId": "root:laptop",
    "mode": "follow",
    "followSourceClientId": "phone-1",
    "state": "playing",
    "trackId": "song-1",
    "positionMs": 60300,
    "syncDriftMs": -200
  }
}
```

## Broadcast Playback

Broadcast participants should apply `effectiveAtServerMs` from
`broadcast.start`, `broadcast.play`, and `broadcast.playItem`.

Participant feedback must include `broadcastId` so the server records it as
participant health/drift instead of a normal source timeline:

```json
{
  "type": "event",
  "action": "playback.update",
  "payload": {
    "sessionId": "root:pc",
    "broadcastId": "broadcast-123",
    "state": "playing",
    "trackId": "song-2",
    "positionMs": 12000,
    "syncDriftMs": -80
  }
}
```

Pause and stop remain immediate in this rollout. Cancel any queued delayed
start for that broadcast timeline when pause or stop is received.

## Manual Verification Checklist

Before using real Flutter devices, the server protocol can be smoke-tested with
the demo websocket client:

```bash
python script/emo_ws_demo.py player \
  --url http://127.0.0.1:5000 \
  --user alice \
  --password Alic3 \
  --client-id phone-1 \
  --device-name phone-1 \
  --session-id root:phone \
  --effective-at-playback \
  --playback-prepare
```

Run a second player with a different `clientId/sessionId`, then use a controller
terminal to trigger `player.play`, `queue.playItem`, or broadcast controls. The
demo player auto-sends `playback.ready` and delays feedback until
`effectiveAtServerMs`.

Run these checks with two Flutter player devices that advertise both
capabilities.

1. Follow start:
   - Device A is the source, device B follows A.
   - Trigger play or play item from a controller.
   - A receives `playback.prepare`; B receives `playback.prepare`.
   - A sends `playback.ready`.
   - B may send ready later; A readiness plus timeout must still commit.
   - Both apply the same `effectiveAtServerMs`.
   - A does not start before the server commit arrives.

2. Broadcast start:
   - Start broadcast with both devices selected and `autoPlay=true`.
   - Both devices prepare and ready.
   - Both receive `broadcast.start` with the same `effectiveAtServerMs`.
   - Both start from the same `trackId/currentIndex/positionMs`.

3. Seek while playing:
   - Send same-track seek.
   - Devices receive a future effective-at seek/commit.
   - Playback resumes from the requested position without repeated corrective
     seeks.

4. Pause:
   - Send pause during a pending prepare or delayed start.
   - Pause is immediate.
   - Any older ready or timer does not restart playback.

5. Long run:
   - Let follow or broadcast play for at least five minutes.
   - `syncDriftMs` should not grow without bound.
   - No device should repeatedly seek in a loop under normal network quality.
