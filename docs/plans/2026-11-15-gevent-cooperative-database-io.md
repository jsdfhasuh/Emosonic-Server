# Cooperative Database I/O for the Gevent Server

## Problem and Baseline

User authorized repairing repeated web/client stalls on 2026-11-15.
Current deployed source is `34f08079bc9643800259a8776a13ad22c46f4a73`;
this isolated worktree starts at its documentation successor `beba2fb`.
Other worktrees and their uncommitted metadata changes are out of scope.

The deployed gevent web process blocks its native main thread on ordinary
MariaDB socket reads. Requests may wait 10-16 seconds before Flask starts its
approximately 0.1-second handling. An older Windows client also repeats stale
playback reports, but that is not proven to be the only trigger.

## Intended Behavior

- Database/network waits yield to other HTTP and Socket.IO greenlets.
- Database connection/transaction locals and application locks are initialized
  after cooperative runtime preparation, preserving per-greenlet isolation.
- The single-process strict-v2 safety policy, authentication, version/lease
  checks, playback semantics, and API payloads remain unchanged.
- Non-gevent backends, CLI commands, and the separate daemon remain unchanged.
- Backend discovery and `--help` do not eagerly import the entire application.
- Unsafe late gevent initialization fails explicitly rather than silently
  sharing existing native-thread locals or incorrectly initialized locks.

## Implementation

1. Defer application and strict-v2 safety imports until after backend runtime
   preparation. Keep the existing console entrypoint and backend selection.
2. Add an idempotent runtime preparation hook to the base/gevent server. Prepare
   gevent before Peewee/application imports and before loading the web app.
3. Add isolated subprocess regression tests so monkey patching cannot alter the
   ordinary unit-test runner. Cover explicit/default backend selection, late
   preparation rejection, slow network I/O versus unrelated HTTP/WebSocket
   work, and database/lock isolation.
   Serialize strict SQLite write transactions before entering its native busy
   wait; this preserves the single-writer contract under greenlets and does not
   change the MySQL/MariaDB transaction path. Other SQLite I/O is not claimed to
   become cooperative.
4. Test targeted behavior, existing strict-v2 safety and broad regression suites;
   run Linux CI and publish an immutable image through the existing workflow.
5. Deploy only after passing verification, preserving the old container/image,
   configuration, secret mounts, media mounts, database, metadata budgets, and
   unrelated services. Recheck the live baseline before cutover.
6. Compare login/static response latency and check existing client reconnect
   logs without issuing playback controls. Record limits of the acceptance.

## Safety and Assumptions

No database schema/data migration, media scan, LLM request, client build/install,
or disabling of protocol checks is part of this repair. Do not weaken stale
version rejection to accommodate the old Windows binary. A brief server restart
is expected at cutover; database and music files must not be modified by the
deployment procedure. Preserve rollback and stop if runtime drift is found.

## Validation Status

- [x] Read-only root-cause investigation and deployed revision identified.
- [x] Isolated worktree and implementation plan created.
- [x] Cooperative startup implementation and regression coverage.
- [ ] Local and Linux validation, commit/push, immutable image publication.
- [ ] Controlled deployment and before/after endpoint probes.
- [ ] User playback/control acceptance on Android and Windows (pending user).

Local validation: 13 isolated runtime tests passed; 230 playback-state/store and
handoff tests passed with early gevent preparation; existing strict-v2 safety
suite passed 17 tests with one environment skip. The original SQLite race failed
under gevent and passes with the SQLite writer lock. A handoff race fixture was
replaced with its existing valid commit helper so either legal scheduler winner
has a source proof, completion deadline, and enqueue state. Import-order and
connection/lock-isolation negative controls failed without the runtime fix.

Manual acceptance: refresh the web UI, load albums from both clients, and play
one track; test a single pause/resume action on each platform. Record expected
versus actual track/audio state and whether one tap succeeds. Return Android
controller logs and Windows `C:/Users/jsdfhasuh/Documents/debug.log`. Check for
prompt reconnect/registration, heartbeat replies, request latency, and absence
of transaction/connection errors. The known old Windows
`strict_error_rejected ... retryable does not match code=conflict` is a separate
client artifact issue and is not silently considered fixed by this deployment.
