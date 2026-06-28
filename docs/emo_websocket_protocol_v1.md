# Emo Socket.IO Protocol v2

## Goals

- Keep HTTP endpoints for login, media metadata, and bootstrapping.
- Use a dedicated Socket.IO channel with namespace `/emo` and path `/emo/ws`.
- Let the server own authoritative device presence, session queue state, and playback state.
- Route device-specific commands by `clientId`.
- Share formal playback state by `sessionId`.

## Mental Model

- REST is request/response.
- Socket.IO is a long-lived event channel.
- `clientId` identifies a device instance.
- `sessionId` identifies a shared playback session.
- Commands target a device, but state belongs to a session.

## Connection Flow

1. Client connects to Socket.IO using namespace `/emo` and path `/emo/ws`.
2. Client sends `auth.login` as the first business message.
3. Server returns `system.ack` on successful authentication.
4. Client sends `device.register`.
5. Client optionally requests `device.list`.
6. Client starts sending commands, events, and state updates.

## Envelope

All business messages are JSON objects carried inside the Socket.IO `message` event.

```json
{
  "type": "command",
  "action": "player.pause",
  "requestId": "req-001",
  "targetClientId": "player-001",
  "payload": {},
  "timestamp": 1710000000
}
```

## Common Fields

- `type`: `auth`, `system`, `device`, `command`, `event`, or `state`
- `action`: concrete operation name
- `requestId`: client-generated id for request/response correlation
- `targetClientId`: target device for command routing
- `payload`: business payload
- `timestamp`: optional sender timestamp

## Core Identity Model

### `clientId`

- Stable device instance identifier
- Used by the server to route commands to one device
- Must be unique across active devices

### `sessionId`

- Stable playback session identifier
- Used by the server to scope queue state and playback state
- Multiple devices may share one `sessionId`

## Roles

- `player`: device that actually plays audio
- `controller`: device that sends playback commands
- `observer`: device that only watches state

One device may hold multiple roles.

## Queue Model

The protocol distinguishes device routing from session state.

- Device routing uses `clientId`
- Shared playback state uses `sessionId`

The authoritative session queue is represented by:

- `queueSongIds`: ordered array of server-side media ids
- `currentIndex`: active queue index
- `positionMs`: current playback offset inside the active track

This protocol does not use the old object-array queue format. The canonical queue payload is song-id based.

## Supported Actions

### Authentication

- `auth.login`

Accepted payload:

```json
{
  "u": "alice",
  "p": "Alic3"
}
```

Session cookies may also be used if the connection originates from the web UI.

### System

- `system.ping`
- `system.pong`
- `system.ack`
- `system.error`

Registered clients should send `system.ping` every 30 seconds. The server
uses the last received business message or ping to decide whether a registered
client is still online; by default a client is removed from `device.list` after
90 seconds without activity.

### Devices

- `device.register`
- `device.list`

Register payload:

```json
{
  "clientId": "desktop-001",
  "deviceName": "Desktop Player",
  "roles": ["player", "controller"],
  "sessionId": "root:living-room"
}
```

### Playback Commands

- `player.play`
- `player.pause`
- `player.next`
- `player.prev`
- `player.seek`
- `player.requestState`
- `queue.playItem`

Pause example:

```json
{
  "type": "command",
  "action": "player.pause",
  "requestId": "req-100",
  "targetClientId": "desktop-001",
  "payload": {}
}
```

Seek example:

```json
{
  "type": "command",
  "action": "player.seek",
  "requestId": "req-101",
  "targetClientId": "desktop-001",
  "payload": {
    "positionMs": 15000
  }
}
```

Queue play example:

```json
{
  "type": "command",
  "action": "queue.playItem",
  "requestId": "req-102",
  "targetClientId": "desktop-001",
  "payload": {
    "sessionId": "root:living-room",
    "queueIndex": 2
  }
}
```

`queue.playItem` payload rules:

- `sessionId` is required
- `queueIndex` is required and must be an integer >= 0
- optional `clientId` switches the lookup from the shared session queue to that device's local queue
- if `clientId` is present, it must match `targetClientId`
- if `clientId` is omitted, the selected item is resolved from the shared session queue

Player state request example:

```json
{
  "type": "command",
  "action": "player.requestState",
  "requestId": "req-103",
  "targetClientId": "desktop-001",
  "payload": {
    "sessionId": "root:living-room",
    "includePlayback": true,
    "includeSessionQueue": true,
    "includeLocalQueue": true,
    "includeReadyState": false
  }
}
```

`player.requestState` payload rules:

- `sessionId` is optional, but if present it must be a non-empty string
- `includePlayback`, `includeSessionQueue`, `includeLocalQueue`, and `includeReadyState` are optional booleans
- when omitted, the target player may treat the missing flags as its default upload policy
- the target player should respond by publishing the requested state updates back to the server

### Playback Events

- `playback.update`

This event reports the actual player state after local execution.

```json
{
  "type": "event",
  "action": "playback.update",
  "requestId": "playback-1",
  "payload": {
    "sessionId": "root:living-room",
    "state": "playing",
    "trackId": "songId3",
    "positionMs": 8000,
    "volume": 70
  }
}
```

Required payload fields:

- `sessionId`
- `state`
- `positionMs`

Recommended payload fields:

- `trackId`
- `volume`
- `sourceClientId`
- `queueType`: `session` or `local`, when the player can report the active queue surface
- `queueClientId`: owner `clientId` for `local` queue playback

Server broadcast note:

- When the server rebroadcasts `playback.update`, it includes `sourceClientId` to identify which device last reported the session state.

### Session Queue State

- `queue.session.sync`

This is the canonical session queue update action.

```json
{
  "type": "state",
  "action": "queue.session.sync",
  "requestId": "queue-1",
  "payload": {
    "sessionId": "root:living-room",
    "queueSongIds": ["songId1", "songId2", "songId3"],
    "currentIndex": 2,
    "positionMs": 8000
  }
}
```

Required payload fields:

- `sessionId`
- `queueSongIds`
- `currentIndex`
- `positionMs`

Validation rules:

- `queueSongIds` must be an array of strings
- `currentIndex` must be an integer
- `positionMs` must be an integer
- if `queueSongIds` is non-empty, `currentIndex` must be within bounds
- if `queueSongIds` is empty, use `currentIndex = 0` and `positionMs = 0`

Server broadcast note:

- When the server rebroadcasts `queue.session.sync`, it includes `sourceClientId` to identify which device last pushed the session queue.

### Local Queue State

- `queue.local.get`
- `queue.local.set`

These actions operate on the device-local draft queue keyed by `sessionId + clientId`.
For `queue.local.set`, `payload.clientId` is the local queue owner. If it is
omitted, the server uses the current registered client's `clientId`. When
`payload.clientId` names another device, the server verifies that device belongs
to the same user and the requested `sessionId`.

`queue.local.get` request example:

```json
{
  "type": "state",
  "action": "queue.local.get",
  "requestId": "local-get-1",
  "payload": {
    "sessionId": "root:living-room",
    "clientId": "player-1"
  }
}
```

`queue.local.set` request example:

```json
{
  "type": "state",
  "action": "queue.local.set",
  "requestId": "local-set-1",
  "payload": {
    "sessionId": "root:living-room",
    "clientId": "player-1",
    "queueSongIds": ["songId1", "songId2", "songId3"],
    "currentIndex": 1,
    "positionMs": 0
  }
}
```

Server response behavior:

- `system.ack` confirms the request
- the server emits a `state / queue.local.set` snapshot to devices in the same session and active session subscribers
- `session.subscribe` snapshots also include the current local queues for that session
- the emitted snapshot uses `payload.sourceClientId` as the local queue owner, not necessarily the sender of the request

### Device List State

- `device.list`

This is both a requestable state snapshot and a server broadcast when device presence changes.

## Server Rules

- Unauthenticated connections may only send `auth.login` and `system.ping`.
- Commands are always routed through the server.
- Command routing uses `targetClientId`.
- Shared queue state is authoritative at the `sessionId` level.
- Local queue state is authoritative at the `sessionId + clientId` level; for `queue.local.set`, `payload.clientId` chooses that `clientId`.
- Playback state is authoritative at the `sessionId + sourceClientId` level.
- Every request-style message should end with either `system.ack` or `system.error`.
- Playback devices should send `playback.update` after they execute commands.
- `queue.session.sync` replaces the full authoritative queue for that session.
- `queue.local.set` replaces the full local draft queue for the specified device.
- `queue.playItem` asks a target device to resolve an item from either the session queue or its local queue and start playback.
- `player.requestState` asks a target device to publish some or all of its current state back to the server.

## Example Flow: Client A Pauses Client B

1. A sends `player.pause` targeting B's `clientId`.
2. Server validates A and finds B's connection from `clientId`.
3. Server forwards the command to B.
4. B pauses playback.
5. B sends `playback.update` with the shared `sessionId`.
6. Server updates session playback state.
7. Server broadcasts the updated state to devices in the same session.

## Example Flow: Controller Publishes Session Queue

1. A controller composes a formal queue.
2. A sends `queue.session.sync` with `queueSongIds`, `currentIndex`, and `positionMs`.
3. Server stores the new queue as authoritative session state.
4. Server broadcasts the new queue to devices in the same session.

## Non-Goals For v2

- Offline message delivery
- Cross-process pub/sub
- Multi-worker synchronization
- Device-to-device direct connections
- Fine-grained ACL beyond same-user control
- Local draft queue persistence on the server

## Upgrade Path

Later versions can add:

- `queue.local.set`
- `queue.session.append`
- `queue.session.remove`
- `queue.session.move`
- `session.join`
- `session.leave`
- stronger tokens instead of raw username/password
- Redis-backed shared state for multi-process deployment
