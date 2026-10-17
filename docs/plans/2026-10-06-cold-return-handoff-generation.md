# Cold return Handoff generation

## Reproduced issue

The real threaded Flask/Socket.IO server completes A → B. B then completes six
ordinary controls to canonical/applied control version 8. A disconnects and
registers the same stable client ID with a new device session, creates an idle
standby Context, and is selected for B → A. Start ACK and prepare both succeed,
but completion rejects the old nonauthoritative A DevicePlaybackState because
its device session belongs to A's retired Context epoch.

The equivalent same-device-session reconnect completes. This is distinct from
the still-investigated hosted client's second-start ACK timeout.

The ACK timeout was then traced separately: stale-source start emits a conflict
with only currentControlVersion. The strict client correctly rejects a cursor
without its Context ID and complete cursor set. Keep that client schema strict.
Construct Handoff-start conflicts from the already loaded and authorized Context,
including epoch, version, queue revision and control version. Apply this bounded
helper to the related source/target effective-at and existing start conflicts;
never infer scope from untrusted payload in the generic error handler.

## Minimal change

Keep all frozen Handoff source/target identity, nonce, connection epoch, current
registration, Context lifecycle/authority/cursors, standby, proof and replay
checks. A mismatched prior target device session may be replaced only when its
row belongs to the same user, is explicitly nonauthoritative, and its Context
epoch is strictly older than the current source Context epoch. Same/newer-epoch,
authoritative, or foreign-user rows remain conflicts. Replace the retired row
atomically using the already validated completion proof.

## Verification

- Real SQLite schema/store tests for cold return and exact replay
- Negative old source/target generation and altered replay proofs
- Current/future-epoch, authoritative and foreign-user row conflicts
- Real network Socket.IO A → B → cold A, including actual ordinary controls
- Existing strict Handoff and Follow continuity suites
- Correlated stale-source/source-clock/target-clock/control-baseline error shapes,
  with all trusted Context cursors and no cross-user cursor disclosure

No production access, deployment, publication, or fixture server-pin change.
The network harness is server protocol evidence, not native audio acceptance.
