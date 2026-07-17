# ADR-0022: Unify Playback Facts, Remote Results, and Local User Settlement in playback.update

## Status

Accepted for the unreleased personal-lab strict-v2 `2.4.0` Core update. This does not enable production capability or
complete Android/Windows device validation.

## Context

The server must distinguish four facts that were previously mixed together:

1. a controller command has been accepted and routed;
2. the authority device has actually committed that remote command;
3. the authority device failed to execute that remote command; and
4. a user physically operating the authority device committed a newer local result.

An ACK cannot prove audio execution. Passive progress must not advance `controlVersion`, but a committed local user
operation must be able to supersede remote work that is still pending. The earlier ADR-0021 proposed a separate
`player.authorityIntent` action followed by `playback.update`. That required two correlated protocols for one completed
local operation and created an avoidable gap between intent acceptance and the actual audio result.

The protocol also needs to represent a normal execution lag:

```text
canonical controlVersion = 48
authority appliedControlVersion = 47
```

During that interval the main Context can describe the newest accepted control target while the authority device state
still describes the older applied song, state, and position.

## Decision

Keep strict-v2 at the unreleased `2.4.0` single shape and remove `player.authorityIntent` from the action surface.

Use `playback.update` as a closed discriminated event with these forms:

- `origin:"passive"`: progress, volume, and other facts at the current `appliedControlVersion`; no Context cursor
  advances.
- `origin:"remoteCommand", executionStatus:"committed"`: settles one server-created pending control transaction,
  advances the authority device's applied cursor, and does not allocate another control version.
- `origin:"remoteCommand", executionStatus:"failed"`: terminates one pending transaction without advancing the
  applied cursor. If the accepted target was already materialized in the main Context, the server reconciles actual
  state with a new Context version and, when needed, Queue revision, but not a new Control version.
- `origin:"localUser", executionStatus:"committed"`: reports an already committed local human action as an absolute
  queue index, track, state, and position. The server allocates `canonical controlVersion + 1`, advances applied to the
  same value, and supersedes only older remote transactions that are still pending.

Keep all four `playback.update` forms authority-originated and backed by Windows `clientSeq`. Add a separate
server-only `playback.control.settled` event for transaction terminals that have no new Windows playback fact:

- `dependency_failed`: a prior track-changing command failed, so a later pending command must not execute against the
  old track;
- `execution_unknown`: disconnect, Socket replacement, server recovery, or watchdog expiry left the final execution
  result unprovable.

The server-only event uses `requestingClientId` for the original controller and never uses `sourceClientId`,
`deviceSessionId`, or `clientSeq` to impersonate authority feedback.

Only the server allocates canonical control versions. Windows sends `observedControlVersion`, which may be less than
or equal to the current canonical value because a remote command can be accepted before Windows observes it. An
observed value greater than canonical is invalid.

Persist each `(playbackContextId, epoch, controlVersion)` transaction in exactly one state:

```text
pending -> committed
pending -> failed
pending -> superseded
```

Persist a per-authority-device `lastAppliedControlVersion`. Feedback below it cannot overwrite current actual state;
equal-version passive feedback can update progress; a higher applied version must be proven by a pending remote
transaction or a newly accepted localUser transaction.

The server remains responsible for ordering, version allocation, idempotency, and terminal transaction state. Windows
must still maintain a short local execution barrier because a server cannot retract a remote command that has already
been delivered to the device. While a local user operation is being settled, Windows defers uncommitted remote work;
after the canonical localUser confirmation it drops transactions not greater than
`supersededThroughControlVersion`.

If `queue.playItem`, `player.next`, or `player.prev` fails, every higher-version remote transaction that is still
pending in the same Context epoch also becomes failed with `dependency_failed`. Windows drops those queued commands
instead of applying them to the previous track. After atomically persisting the cascade, the server sends one
`playback.control.settled` per affected command in ascending control-version order. Controllers settle exact versions
rather than inferring a range.

Every server-routed control carries `executionTimeoutMs`, default 15 seconds (15000ms). Windows starts an audio execution lease
when the command enters its local execution queue. It may report `execution_timeout` only after proving that the lease
is invalid and late callbacks cannot change audio or publish committed feedback. The server starts a watchdog when the
command is accepted/routed; the watchdog is always `executionTimeoutMs + 2 seconds`, default 17 seconds (17000ms).
Watchdog expiry without terminal feedback produces server-only `execution_unknown`, never a fabricated Windows
`playback.update`. Authority disconnect, connection replacement, graceful shutdown, or recovery also produces
`execution_unknown`. Unknown commands are never replayed automatically after reconnect.

`playback.control.settled` is idempotent by `(playbackContextId, epoch, commandControlVersion)`. Equal duplicates are
ignored. Different status/error values for the same key are protocol conflicts and cannot overwrite the first
terminal. Older settlements can close older pending UI but cannot roll back a newer applied playback state.

Feedback below `lastAppliedControlVersion` cannot be silently dropped because `playback.update` is event-confirmed.
The server ignores its state side effects and sends the current passive canonical update only to the reporting Socket;
it does not broadcast the stale report. A contradictory terminal result still returns `conflict`.

Natural end-of-track auto-advance is not `localUser` and does not receive human-priority supersede semantics. It remains
on the existing Queue transition path until a separate automatic-transition contract is justified.

## Consequences

### Positive

- ACK, actual audio success, actual failure, and local human override have explicit meanings.
- One completed local action uses one wire action instead of authorityIntent plus feedback.
- Passive progress cannot make remote commands stale.
- Controllers can see accepted-versus-applied lag without assuming the latest command already executed.
- Local priority is deterministic but bounded; a later remote command using the fresh version remains valid.
- Late older feedback cannot roll back a newer applied device state.
- A failed track change cannot cause later controls to run against the wrong song.
- Pending work always reaches a terminal result after timeout, disconnect, or restart.
- Stale feedback receives a bounded source-only correction instead of hanging or disturbing other clients.
- Server-generated transaction terminals no longer impersonate Windows playback feedback or reuse a device sequence.
- Per-command settlement removes client-side guessing about dependency-failed version ranges.

### Negative

- `playback.update` becomes a strict discriminated union and requires more validation than the old feedback-only shape.
- The server must persist control transaction terminal state, local intent dedupe, and per-device applied cursors.
- Windows needs a local execution barrier for already-delivered remote commands.
- Status projections must allow the latest Context target and older applied device track to differ while work is pending.
- The server needs watchdog scheduling, a new server-only event, and per-command settlement delivery.
- Windows must implement cancellable/isolated audio execution leases before hard timeout can be declared ready.

### Neutral

- The protocol remains `2.4.0` because this is an unreleased personal-lab single-shape update with no old-client
  compatibility branch.
- Full queue replacement continues to use `queue.context.sync`.
- Android/Windows compilation and real-device validation remain user-owned.
- The default Windows execution lease is 15 seconds and the server watchdog adds a fixed 2-second grace. The command
  carries the lease duration; the watchdog duration is server-side and not negotiated.

## Alternatives Considered

### Let the server synthesize failed playback.update

Rejected because playback.update is authority feedback and requires the Windows clientSeq. Reusing a persisted
clientSeq would make a server transaction decision look like a new device fact.

### Let controllers infer dependency failures from a version range

Rejected because each pending command is a distinct transaction, may belong to a different requesting controller,
and requires its own idempotent terminal result.

### Let the server watchdog report execution_timeout

Rejected because lack of feedback only proves an unknown result. Only Windows can report execution_timeout after its
audio layer proves that the execution lease is invalid and late work cannot apply.

### Keep player.authorityIntent plus playback.update

Rejected because a committed local operation would require two state machines and two correlations while still needing
the final playback.update for actual state.

### Let Windows allocate the next controlVersion

Rejected because Windows can be behind a remote command already accepted by the server. Two writers could allocate the
same version for different operations.

### Use the same version and resolve by source priority

Rejected because the same cursor would then identify different payloads. Every consumer would need an additional
priority and local sequence comparison, and two rapid local actions would still need another ordering field.

### Advance controlVersion for every playback.update

Rejected because progress and buffering would continuously invalidate valid controller commands.

### Treat server ACK as successful playback

Rejected because ACK only proves acceptance and routing; audio loading or seeking can still fail afterward.

## References

- `specs/emosonic_strict_v2_socketio_server_contract.md`
- `docs/plans/2026-07-17-strict-v2-playback-update-control-settlement.md`
- ADR-0019: Separate Control Transactions from Playback Facts and Audio Leases
- ADR-0020: Ensure One Standby PlaybackContext per Player
- ADR-0021: Prioritize Local Authority User Intents over Uncommitted Remote Controls (superseded)
