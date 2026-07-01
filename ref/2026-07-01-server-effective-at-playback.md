# 2026-07-01 Server EffectiveAt Playback Rollout

## Summary

服务端同步播放方向保持不变：把播放控制从“客户端收到命令后立刻执行”升级为“服务端权威 timeline + future `effectiveAtServerMs` + optional two-phase prepare/ready/commit”。

纠正后的主路径：

```text
control request
-> server validates authority, target participants, base versions
-> server chooses protocol by target player capabilities
-> two-phase path:
   -> server creates prepareId/controlVersion/provisional commit data
   -> server sends playback.prepare to capable target players
   -> clients preload queue, seek position, stay paused
   -> clients send playback.ready
   -> server commits authoritative playback.update or broadcast.* state
   -> clients start at the same effectiveAtServerMs
-> single-phase future path:
   -> server commits authoritative state with effectiveAtServerMs
-> legacy path:
   -> server keeps current direct command behavior, no effectiveAt wait contract
```

Important repository baseline:

- `system.pong` already returns the original `requestId` and `payload.serverTimeMs`.
- `device.register` already stores `capabilities`.
- Timeline fields such as `timelineId`, `version`, `epoch`, `queueRevision`, `controlVersion`, and `serverUpdatedAtMs` already exist for playback/broadcast state.
- `CONTROL_ACTIONS` are still forwarded directly through `_route_command`; this is the main gap for server-mediated playback.
- Broadcast actions currently commit immediately.
- Web Player does not yet advertise `effectiveAtPlayback` or `playbackPrepare`, and should be treated as legacy until it is updated.

Flutter-side support assumed by this plan:

- device capability: `effectiveAtPlayback: true`
- device capability: `playbackPrepare: true`
- receive `playback.prepare`
- emit `playback.ready`
- consume `effectiveAtServerMs` in `playback.update`
- consume `effectiveAtServerMs` in `broadcast.*`
- consume `effectiveAtServerMs` in targeted `player.play`

## Goals

- New capable source clients wait for the server commit instead of playing immediately.
- Follow and broadcast participants start as close as practical to the same server time.
- Server remains the authority for source and broadcast timelines.
- Participant feedback records health/drift/execution state, but does not overwrite the authoritative source or broadcast timeline.
- Normal playback heartbeats must not increment `controlVersion`.
- User controls such as play, resume, play item, next, previous, seek, queue sync, and broadcast controls increment `controlVersion` only when the server commits a user-visible control.
- Preserve existing `queueRevision` meaning unless tests are intentionally migrated: queue-content/base-mutation revision, not a generic current-position counter.
- Pause remains fast and immediate by default.
- Old clients keep working through legacy fallback.

## Non-Goals

- No sample-level phase alignment.
- No speed-based drift correction in this round.
- No follower writes to source timeline.
- No requirement for the server to estimate decode time precisely. Client readiness is represented by `playback.ready`.
- No Web Player protocol upgrade in the server rollout unless explicitly scheduled.

## Repository Baseline

These are already present and should be verified, not reimplemented:

```text
system.ping -> system.pong with same requestId and payload.serverTimeMs
device.register persists payload.capabilities
playback state stores timelineId/version/epoch/queueRevision/controlVersion/serverUpdatedAtMs
broadcast state stores timelineId/version/epoch/queueRevision/controlVersion/serverUpdatedAtMs
broadcast participant state stores syncDriftMs and online/error fields
queue/base version conflicts already return system.error with current version fields
```

Main missing pieces:

```text
effectiveAtServerMs commit semantics
capability-based protocol selection
server-mediated handlers for CONTROL_ACTIONS
single-phase future commit path
playback.prepare targeted emit
playback.ready aggregation and timeout handling
prepare supersede/cancel state
participant feedback isolation for follow/broadcast execution reports
tests around new protocol selection and commit timing
```

## Client Capability

The server already stores arbitrary capability dictionaries from `device.register`.

New playback protocol capabilities:

```json
{
  "capabilities": {
    "supportsSessionQueue": true,
    "supportsFollow": true,
    "supportsBroadcast": true,
    "effectiveAtPlayback": true,
    "playbackPrepare": true
  }
}
```

Gating rules:

```text
participants = players that will execute audio for this control
controllers and passive subscribers do not need playback timing capabilities

if all required executing players support playbackPrepare and effectiveAtPlayback:
  use two-phase prepare/ready/commit
else if all required executing players support effectiveAtPlayback:
  use single-phase future commit
else:
  use legacy behavior for this control
```

First implementation should use whole-control fallback. If any required executing player is legacy, keep the entire control on the legacy path. Mixed per-client protocol splitting can be added later, but it complicates broadcast consistency.

Required participants:

- Source/owner player is required.
- Broadcast participants are target executing players.
- Follow participants can be non-blocking after prepare is sent. Timeout should not block commit if the source/owner is ready.

Web Player baseline:

- Current Web Player capabilities do not include `effectiveAtPlayback` or `playbackPrepare`.
- Do not send Web Player a delayed `player.play` that requires waiting.
- Treat Web Player as legacy until the frontend explicitly supports delayed execution and `playback.prepare`.

## Protocol Fields

Use common timeline fields, but do not force a single payload shape for source playback and broadcast.

```ts
type TimelineFields = {
  timelineId: string;
  authorityClientId?: string;
  originClientId?: string;
  state: "playing" | "paused" | "stopped" | "error";
  trackId?: string;
  positionMs: number;
  playbackRate: number;
  version: number;
  epoch: number;
  queueRevision?: number;
  controlVersion: number;
  serverUpdatedAtMs: number;
  serverTimeMs: number;
  effectiveAtServerMs?: number;
  followDelayMs?: number;
};

type SourcePlaybackPayload = TimelineFields & {
  sessionId: string;
  sourceClientId: string;
  queueSongIds?: string[];
  currentIndex?: number;
};

type BroadcastPayload = TimelineFields & {
  broadcastId: string;
  ownerClientId: string;
  participants: string[];
  queueSongIds: string[];
  currentIndex: number;
  controlPolicy?: string;
};
```

Field semantics:

- `authorityClientId`: playback owner/source identity for clients and UI. Server-side code still owns the authoritative commit and ordering rules.
- `serverTimeMs`: current server time when the message is sent, used by clients for clock offset sampling.
- `serverUpdatedAtMs`: server commit time for the authoritative timeline anchor.
- `effectiveAtServerMs`: future server time when a playing transition should become audible.
- `positionMs`: for `state=playing` with `effectiveAtServerMs`, this is the position anchor at `effectiveAtServerMs`.
- `version`: every authoritative timeline commit increments this.
- `controlVersion`: only user controls increment this.
- `epoch`: increments when current media identity changes, such as track or current item change.
- `queueRevision`: protects queue-content/base mutations. Keep current broadcast behavior where `playItem` changes `currentIndex` and increments `epoch/controlVersion/version`, but does not increment `queueRevision`.
- `playbackRate`: default `1.0`.
- `followDelayMs`: legacy/fallback hint. Default is `0`; clients must not assume an artificial 700ms lag when the field is absent.

Persistence rule:

- Do not persist stale `serverTimeMs`.
- Do not restore an expired `effectiveAtServerMs` as a future instruction. If persisted for diagnostics, strip it from restored snapshots unless it is still in the future and the timeline is still active.

## Server Clock And Ping/Pong

The minimum ping/pong contract is already implemented and should remain:

```json
{
  "action": "system.pong",
  "requestId": "same-request-id-from-ping",
  "payload": {
    "serverTimeMs": 1782869000000
  }
}
```

Keep:

- `system.pong` returns the same `requestId`.
- `payload.serverTimeMs` uses server milliseconds.
- Clients resample after reconnect.

Optional later server-side latency tracking:

```ts
type ClientLatency = {
  clientId: string;
  rttEwmaMs: number;
  jitterEwmaMs: number;
  sampleCount: number;
  updatedAtMs: number;
};
```

Initial rollout can use fixed lead times:

```text
twoPhaseCommitLeadMs = 350
singlePhaseFutureLeadMs = 700
```

## EffectiveAt Commit

Commit lead calculation:

```text
commitLeadMs = clamp(
  max(minCommitLeadMs, maxParticipantOneWayBudgetMs + commitJitterMs),
  minCommitLeadMs,
  maxCommitLeadMs
)
```

Initial constants:

```text
commitJitterMs = 120
minCommitLeadMs = 250
maxCommitLeadMs = 1500
prepareTimeoutMs = 1200
fallbackSinglePhaseLeadMs = 700
```

If RTT/jitter is not implemented yet:

```text
two-phase commitLeadMs = 350
single-phase future commitLeadMs = 700
```

Playback commit example:

```json
{
  "type": "state",
  "action": "playback.update",
  "payload": {
    "sessionId": "root:phone",
    "sourceClientId": "phone-1",
    "timelineId": "session:root:phone:client:phone-1",
    "authorityClientId": "phone-1",
    "originClientId": "phone-1",
    "state": "playing",
    "trackId": "song-1",
    "positionMs": 0,
    "serverUpdatedAtMs": 1782869000000,
    "serverTimeMs": 1782869000001,
    "effectiveAtServerMs": 1782869000350,
    "version": 456,
    "epoch": 8,
    "queueRevision": 20,
    "controlVersion": 123,
    "playbackRate": 1.0,
    "followDelayMs": 0
  }
}
```

For clients that do not apply their own `playback.update` to local playback, the server may also send a targeted command with the same effective time:

```json
{
  "type": "command",
  "action": "player.play",
  "targetClientId": "phone-1",
  "payload": {
    "sessionId": "root:phone",
    "effectiveAtServerMs": 1782869000350,
    "controlVersion": 123
  }
}
```

Only send this delayed command to clients that advertise `effectiveAtPlayback`.

## Two-Phase Protocol

### Prepare

On a two-phase control, server creates pending prepare state:

```ts
type PlaybackPrepareState = {
  prepareId: string;
  action: string;
  sessionId?: string;
  broadcastId?: string;
  timelineId: string;
  sourceClientId?: string;
  ownerClientId?: string;
  targetClientIds: string[];
  requiredClientIds: string[];
  readyClientIds: Set<string>;
  failedClientIds: Set<string>;
  controlVersion: number;
  commitPayload: object;
  createdAtMs: number;
  expiresAtMs: number;
  status: "preparing" | "committed" | "aborted" | "superseded";
};
```

Targeted prepare message:

```json
{
  "type": "command",
  "action": "playback.prepare",
  "requestId": "prepare-123-phone-1",
  "targetClientId": "phone-1",
  "payload": {
    "prepareId": "prep-123",
    "sessionId": "root:phone",
    "sourceClientId": "phone-1",
    "timelineId": "session:root:phone:client:phone-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "trackId": "song-1",
    "positionMs": 0,
    "controlVersion": 123
  }
}
```

Client behavior:

- Load `queueSongIds`.
- Move to `currentIndex`.
- Seek to `positionMs`.
- Stay paused.
- Emit `playback.ready`.

Ready payload:

```json
{
  "type": "event",
  "action": "playback.ready",
  "requestId": "playback-ready-1",
  "payload": {
    "prepareId": "prep-123",
    "sessionId": "root:phone",
    "clientId": "phone-1",
    "ready": true,
    "positionMs": 0,
    "sourceClientId": "phone-1",
    "controlVersion": 123
  }
}
```

Failure payload:

```json
{
  "type": "event",
  "action": "playback.ready",
  "payload": {
    "prepareId": "prep-123",
    "sessionId": "root:phone",
    "clientId": "desktop-1",
    "ready": false,
    "positionMs": 0,
    "sourceClientId": "phone-1",
    "controlVersion": 123,
    "errorCode": "prepare_failed",
    "errorMessage": "failed to resolve song"
  }
}
```

Aggregation rules:

- `ready.prepareId` must match an active pending prepare.
- `ready.controlVersion` must match the pending prepare.
- Required source/owner failure aborts the prepare.
- Non-required participant failure or timeout does not block commit.
- New user control for the same timeline supersedes older pending prepare.
- Old ready messages for committed, aborted, or superseded prepares are ignored.
- Commit when all required participants are ready, or when timeout expires and source/owner is ready.

### Commit

On commit:

```text
serverNowMs = current server ms
effectiveAtServerMs = serverNowMs + commitLeadMs
serverUpdatedAtMs = serverNowMs
version += 1
controlVersion = reserved controlVersion
```

Then:

- Save/update authoritative source or broadcast timeline.
- Broadcast state to observers and executing participants.
- Send targeted delayed commands only to clients that need them and support `effectiveAtPlayback`.
- Send one `system.ack` for the original request. For two-phase controls, this can acknowledge `status: "preparing"` before commit; the later commit should arrive as normal state/command events, not as a second ack for the same request.

## Command Mapping

### Player Play / Resume

Current behavior: direct forwarding through `_route_command`.

Target behavior:

```text
if target player supports playbackPrepare + effectiveAtPlayback:
  server creates prepare from current authoritative queue/playback state
  commit state=playing with effectiveAtServerMs
elif target player supports effectiveAtPlayback:
  server commits state=playing with future effectiveAtServerMs
else:
  legacy direct player.play
```

The source client must also cooperate: new Flutter source should send a server control request and wait for commit. Legacy clients may still play locally immediately.

### Queue Play Item / Next / Previous

If the command changes current media and starts playback:

```text
prepare target queue/currentIndex/positionMs=0
commit state=playing
epoch += 1
controlVersion += 1
version += 1
queueRevision changes only if queue content/base queue mutation changes
```

For broadcast `playItem`, preserve current semantic unless intentionally migrated:

```text
currentIndex change -> epoch/controlVersion/version increment
queueRevision unchanged
```

### Seek

Same track while paused:

```text
immediate commit
state=paused
positionMs=requestedPositionMs
effectiveAtServerMs absent
controlVersion += 1
version += 1
```

Same track while playing:

```text
single-phase future commit is enough
state=playing
positionMs=requestedPositionMs
effectiveAtServerMs=serverNowMs + 150..350ms
controlVersion += 1
version += 1
```

Track or queue change:

```text
use two-phase when all required participants support it
otherwise fallback by capability
```

### Pause

Pause remains immediate:

```text
state=paused
positionMs=current authoritative position at serverNowMs
effectiveAtServerMs absent
controlVersion += 1
version += 1
```

Optional later behavior for highly synchronized broadcast pause:

```text
effectiveAtServerMs = serverNowMs + 80..150ms
```

Do not enable this in the first server rollout.

### Broadcast Start

Current behavior: create broadcast and immediately emit `broadcast.start`.

Target behavior:

```text
resolve participants
choose capability path from executing participants
legacy:
  keep current create_broadcast + broadcast.start behavior
single-phase future:
  create broadcast authoritative state with state=playing/stopped
  include effectiveAtServerMs when autoPlay=true
two-phase:
  create pending prepare using a provisional broadcastId
  send playback.prepare to participants
  commit by creating/updating authoritative broadcast state
  emit broadcast.start with effectiveAtServerMs
```

Broadcast payload example:

```json
{
  "type": "command",
  "action": "broadcast.start",
  "payload": {
    "broadcastId": "broadcast-1",
    "timelineId": "broadcast:broadcast-1",
    "authorityClientId": "server",
    "originClientId": "phone-1",
    "ownerClientId": "phone-1",
    "participants": ["phone-1", "desktop-1"],
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "trackId": "song-1",
    "positionMs": 0,
    "state": "playing",
    "autoPlay": true,
    "serverUpdatedAtMs": 1782869030000,
    "serverTimeMs": 1782869030001,
    "effectiveAtServerMs": 1782869030400,
    "version": 1,
    "epoch": 1,
    "queueRevision": 1,
    "controlVersion": 1,
    "playbackRate": 1.0
  }
}
```

## Authoritative Timeline Rules

### Source Timeline

```ts
type SourcePlaybackTimeline = {
  sessionId: string;
  sourceClientId: string;
  timelineId: string;
  state: "playing" | "paused" | "stopped" | "error";
  trackId?: string;
  queueSongIds?: string[];
  currentIndex?: number;
  positionMs: number;
  playbackRate: number;
  version: number;
  epoch: number;
  queueRevision: number;
  controlVersion: number;
  serverUpdatedAtMs: number;
  effectiveAtServerMs?: number;
};
```

### Broadcast Timeline

```ts
type BroadcastTimeline = {
  broadcastId: string;
  timelineId: string;
  ownerClientId: string;
  participantClientIds: string[];
  state: "playing" | "paused" | "stopped" | "error";
  trackId?: string;
  queueSongIds: string[];
  currentIndex: number;
  positionMs: number;
  playbackRate: number;
  version: number;
  epoch: number;
  queueRevision: number;
  controlVersion: number;
  serverUpdatedAtMs: number;
  effectiveAtServerMs?: number;
};
```

### Participant Feedback

Current broadcast participant state exists, but current `playback.update` also writes normal per-device playback state. Tighten this before relying on it for sync.

Recommended first server behavior:

```text
if action == playback.update and payload.broadcastId is active for sender:
  update broadcast participant state
  do not overwrite broadcast authoritative timeline
  do not increment broadcast version/controlVersion
  optionally do not persist this as normal per-device playback state

if action == playback.update from a follow participant for a source it follows:
  update participant feedback/drift
  do not write source timeline
  do not broadcast it as source truth

if action == playback.update from the source client itself:
  update source playback heartbeat/state
  increment version if stored state changes
  do not increment controlVersion for ordinary heartbeat
```

Optional protocol cleanup:

```text
add playback.feedback for participant execution reports
reserve playback.update for authoritative source-owned state
```

This is cleaner, but requires client changes. Server-side special-casing can be the first step.

## Staleness And Ordering

Ordering rules:

```text
timelineId stable per source or broadcast timeline
version monotonic per timelineId
controlVersion monotonic per timelineId for user controls
queueRevision monotonic for queue content/base mutations
epoch monotonic when current media identity changes
```

Reject or ignore:

- `baseControlVersion` lower than current control version: reject with conflict.
- `baseQueueRevision` lower than current queue revision: reject queue mutation with conflict.
- Ready for missing/superseded prepare: ignore.
- Feedback from old connections: record only as participant feedback if still relevant.
- Delayed effectiveAt commit older than current `controlVersion`: ignore.

Conflict error shape:

```json
{
  "action": "system.error",
  "requestId": "request-id",
  "payload": {
    "code": "conflict",
    "message": "Broadcast control version conflict",
    "currentControlVersion": 124
  }
}
```

## Fallback Behavior

### All Required Players Support Prepare And EffectiveAt

```text
play/resume/playItem/broadcast start -> two-phase
seek while playing -> single future commit unless media changes
pause -> immediate commit
```

### Players Support EffectiveAt But Not Prepare

```text
skip prepare
commit with effectiveAtServerMs = serverNowMs + largerLeadMs
largerLeadMs = clamp(maxParticipantBudget + prepareBudgetMs, 700, 2000)
```

### At Least One Required Player Is Legacy

```text
use current legacy direct commands
do not send playback.prepare
do not send delayed player.play that requires waiting
do not require source to wait for commit
followers rely on followDelay/drift correction
```

## Rollout Plan

1. Baseline verification
   - Add or strengthen tests for `system.pong` requestId/serverTimeMs.
   - Add tests proving `device.register` stores capability flags.
   - Add tests proving Web Player remains legacy unless new capabilities are declared.

2. Feedback isolation
   - Ensure broadcast/follow participant feedback cannot overwrite source or broadcast timelines.
   - Decide whether to add `playback.feedback` now or server-side special-case `playback.update`.

3. EffectiveAt payload support
   - Add `effectiveAtServerMs` to in-memory authoritative commit payloads.
   - Strip expired `effectiveAtServerMs` from restored persisted snapshots.
   - Add helper to compute future commit lead.

4. Capability gating helpers
   - Resolve executing player participants for each control.
   - Choose `two_phase`, `single_future`, or `legacy`.
   - Keep whole-control fallback for mixed participant capability sets.

5. Single-phase future commit
   - Start with broadcast start/play/playItem because broadcast timeline is already server-owned.
   - Emit the same `effectiveAtServerMs` to all capable participants.
   - Keep legacy behavior for Web Player and other old clients.

6. Prepare/ready state machine
   - Add pending prepare storage in websocket state.
   - Implement targeted `playback.prepare`.
   - Implement `playback.ready` validation, timeout, abort, supersede, and commit.

7. Two-phase broadcast controls
   - Switch capable broadcast start/playItem/play to prepare/ready/commit.
   - Keep pause immediate.
   - Keep stale `baseControlVersion` conflict behavior.

8. Server-mediated source controls
   - Replace direct forwarding for capable `player.play`, `queue.playItem`, next, previous, and playing seek.
   - Source-side new clients must wait for commit.
   - Legacy direct forwarding remains fallback.

9. Optional RTT/jitter metrics
   - Track RTT samples from ping/pong if useful.
   - Replace fixed lead constants with EWMA-based lead calculation.

10. Optional Web Player upgrade
   - Add `effectiveAtPlayback` and `playbackPrepare`.
   - Implement `playback.prepare`.
   - Implement delayed execution for `player.play` and broadcast payloads.

## Metrics And Logs

Recommended metrics:

```text
prepare.created count
prepare.ready count
prepare.failed count
prepare.timeout count
prepare.superseded count
commit.leadMs histogram
commit.lateByMs histogram reported from client if available
participant.driftMs histogram
client.rttEwmaMs histogram
client.jitterEwmaMs histogram
```

Recommended log fields:

```text
prepareId
timelineId
sessionId
broadcastId
sourceClientId
ownerClientId
targetClientIds
readyClientIds
failedClientIds
controlVersion
version
epoch
queueRevision
effectiveAtServerMs
serverUpdatedAtMs
leadMs
protocolPath
```

## Test Plan

### Baseline Tests

- `system.pong` returns original `requestId`.
- `system.pong` returns `payload.serverTimeMs`.
- `device.register` persists `effectiveAtPlayback` and `playbackPrepare`.
- Web Player registration does not accidentally opt into new protocol.
- Existing timeline fields remain present in playback and broadcast payloads.

### Capability Gating

- All required players support both flags -> two-phase.
- Required players support only `effectiveAtPlayback` -> single future commit.
- Any required player lacks `effectiveAtPlayback` -> legacy whole-control fallback.
- Controllers/subscribers do not affect playback protocol selection.
- Offline/skipped broadcast targets do not affect gating after participant resolution.

### Lead Calculation

- Lead is not below minimum.
- Lead is not above maximum.
- Fixed fallback lead is used when no RTT data exists.
- RTT/jitter increases lead when latency tracking is enabled.
- Expired latency samples are ignored.

### Prepare Aggregation

- Source/owner ready allows commit.
- Follower or non-required participant timeout does not block commit.
- Source/owner failure aborts.
- New control supersedes old prepare.
- Old ready does not commit.
- Ready with wrong `controlVersion` is ignored or rejected.
- Ready from non-target client is rejected.

### Timeline Ordering

- Play/resume increments `controlVersion/version`.
- PlayItem increments `controlVersion/version/epoch`.
- Broadcast playItem does not increment `queueRevision` unless queue content changes.
- Queue content change increments `queueRevision`.
- Pause increments `controlVersion/version` and omits `effectiveAtServerMs`.
- Ordinary playback heartbeat does not increment `controlVersion`.
- Stale `baseControlVersion` returns conflict with `currentControlVersion`.
- Stale `baseQueueRevision` returns conflict with `currentQueueRevision`.

### Feedback Isolation

- Follower feedback does not overwrite source timeline.
- Broadcast participant feedback does not overwrite broadcast timeline.
- Participant `syncDriftMs` updates participant state only.
- Participant feedback does not increment source or broadcast `controlVersion`.
- Old connection feedback cannot revive stopped broadcast state.

### Integration Tests

- Capable source play:
  - Source receives `playback.prepare`.
  - Followers or broadcast participants receive `playback.prepare`.
  - Server receives ready.
  - Commit uses one `effectiveAtServerMs`.
  - Source and participants wait for commit time.

- Broadcast start:
  - Owner and participants prepare.
  - Commit payload includes queue, currentIndex, and `effectiveAtServerMs`.
  - Web legacy participant forces legacy fallback in first implementation.

- Late ready:
  - Commit is not duplicated.
  - Timeline is not rolled back.

- Seek while playing:
  - Same-track seek uses future commit.
  - Track-change seek uses prepare when possible.

- Pause:
  - Does not wait for prepare timeout.
  - Payload has no future `effectiveAtServerMs`.

- Old client fallback:
  - No `playback.prepare`.
  - No delayed `player.play` wait contract.
  - Existing direct command tests continue to pass.

### Manual Verification

- Two capable Flutter devices start follow playback nearly together.
- Source device no longer plays before server commit.
- Broadcast start gives all capable devices the same `effectiveAtServerMs`.
- Weak network uses a larger or fixed-safe lead and does not repeatedly seek.
- Seek while playing starts both devices from the requested position.
- Pause remains responsive.
- Five-minute playback does not show unbounded drift growth.

## Server Checklist

- [x] `system.pong` returns original `requestId`.
- [x] `system.pong` returns `payload.serverTimeMs`.
- [x] `device.register` stores `capabilities`.
- [x] Timeline payloads already include `timelineId/version/epoch/queueRevision/controlVersion/serverUpdatedAtMs`.
- [x] Add baseline tests for capability flags and server time if missing.
- [x] Treat Web Player as legacy unless it declares new capabilities.
- [x] Implement participant feedback isolation.
- [x] Add `effectiveAtServerMs` commit support.
- [x] Strip expired `effectiveAtServerMs` on restore.
- [x] Implement capability gating helpers.
- [x] Implement fixed commit lead calculation.
- [x] Implement single-phase future commit.
- [x] Implement pending prepare state.
- [x] Implement targeted `playback.prepare`.
- [x] Implement `playback.ready` action.
- [x] Implement prepare timeout.
- [x] Implement prepare supersede/cancel.
- [x] Switch capable broadcast start/play/playItem to two-phase.
- [x] Keep broadcast pause immediate.
- [x] Switch capable source play/resume/playItem/next/prev to server-mediated controls.
- [x] Keep legacy direct forwarding fallback.
- [x] Add unit and integration tests.
- [ ] Run dual-device manual verification.

## Verification Status

Automated verification completed:

- `python -m py_compile supysonic/emo/ws.py supysonic/emo/ws_state.py supysonic/emo/ws_store.py`
- `python -m py_compile script/emo_ws_demo.py`
- `python script/emo_ws_demo.py --help`
- `git diff --check`
- `python -m unittest tests.base.test_emo_ws_state tests.base.test_emo_ws_store tests.base.test_emo_ws` (`87` tests)
- `python -m unittest` (`805` tests)

Additional server audit fixes covered by automated tests:

- Source server-mediated controls reject stale `baseControlVersion`.
- Broadcast two-phase controls reject stale base control versions before sending `playback.prepare`.
- Immediate source controls supersede older pending prepare state so late ready cannot commit a stale control.

Client/manual verification support added:

- `docs/flutter_effective_at_playback_v2.md` defines the Flutter capability,
  `playback.prepare` / `playback.ready`, delayed `effectiveAtServerMs`
  execution, feedback, and dual-device manual verification contract.
- Existing follow/broadcast Flutter v1 docs now point clients to the v2 timing
  contract before advertising the new capabilities.
- `script/emo_ws_demo.py` can register `effectiveAtPlayback` /
  `playbackPrepare`, auto-respond to `playback.prepare`, and delay playback
  feedback until `effectiveAtServerMs` for server protocol smoke tests.

Manual verification not completed in this workspace:

- Dual-device Flutter follow/broadcast timing.
- Weak-network timing behavior.
- Long-running drift observation.
