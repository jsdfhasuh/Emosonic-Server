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
- [x] Local and Linux validation, commit/push, immutable image publication.
- [x] Controlled deployment and before/after endpoint probes.
- [ ] User playback/control acceptance on Android and Windows (pending user).

Local validation: 13 isolated runtime tests passed; 230 playback-state/store and
handoff tests passed with early gevent preparation; existing strict-v2 safety
suite passed 17 tests with one environment skip. The original SQLite race failed
under gevent and passes with the SQLite writer lock. A handoff race fixture was
replaced with its existing valid commit helper so either legal scheduler winner
has a source proof, completion deadline, and enqueue state. Import-order and
connection/lock-isolation negative controls failed without the runtime fix.

## Deployment and Verification

- Implemented source: `f2521bca149e66e1132e12719c1dd97d9489dbdf` on
  `codex/gevent-io-20261115`, committed and pushed.
- Linux CI ran 2,428 tests successfully (3 skips), plus 5 network tests (1 skip).
  Wheel/sdist checks and AMD64/ARM64 image publication passed:
  https://github.com/jsdfhasuh/Emosonic-Server/actions/runs/43720664812
- Immutable image:
  `ghcr.io/jsdfhasuh/emosonic-server@sha256:b7a752ecb82d3670eb43e148b35227d9eaf71051c3dd19dbb1264153a51ee48f`.
- The 13 runtime regressions also passed on OpenWrt's actual ARM64 image in
  19.398 seconds, in an ephemeral network-isolated container with a read-only
  root filesystem, temporary test storage, and no production mounts.
- Image-only cutover completed around 22:01 Asia/Shanghai. Exact configuration
  bytes, host settings, mounts, and other environment values were preserved;
  only image, build-commit environment value, and revision label changed.
- Private rollback snapshot: `/mnt/django-emmc/supysonic/gevent-io-f2521bc`.
  Retained original container: `supysonic-openwrt-before-gevent-f2521bc`.
  Private snapshot contents must not be copied into source control or logs.
- The guarded service is running; replacement container had no restart/OOM.
  Existing Windows clients authenticated and registered after reconnect.
  No new database connection/transaction exception was seen in the bounded log
  check. Existing daily-recommendation backoff errors remain a separate issue.

The same nine host-local requests (login, CSS and JS, three rounds) returned
HTTP 200 before and after cutover:

| Observation | Minimum | Median | Maximum |
| --- | ---: | ---: | ---: |
| Immediately before cutover | 0.111 s | 0.219 s | 0.377 s |
| After cutover | 0.080 s | 0.173 s | 0.294 s |

Public HTTPS login after cutover returned HTTP 200 in 0.210 seconds. The immediate
baseline was already temporarily fast, so these samples do not establish a
permanent elimination of every earlier 10-16 second spike. The isolated slow-I/O
regression and live socket inspection provide the mechanism-specific evidence:
250 live main-thread samples showed 229 epoll waits and 21 running samples, with
25 observed database socket instances all nonblocking and none blocking.

No Android/Windows application was built, installed, stopped, or updated by this
repair. The known older Windows conflict-parser behavior may still recur and
requires a separate client artifact update. Reboot recovery, prolonged load,
subjective playback, and one-tap controls remain unverified.

## User Acceptance Steps

1. Open https://supysonic.19970219.xyz/ and refresh twice. Confirm the page and
   styles load without repeated multi-second blank waits; note the local time
   of any delay. A successful anonymous login page does not prove library or
   playback behavior.
2. On Android EmoSonic, refresh albums, choose one track, and use pause once and
   resume once. Record expected versus actual audio/track state, elapsed loading
   time, and whether each control succeeded with one tap.
3. Repeat on Windows EmoSonic, with the same expected/actual record. The old
   running client was not replaced by this server deployment.
4. Return Android controller logs and the unchanged Windows authority log at
   `C:/Users/jsdfhasuh/Documents/debug.log`, with platform and failure timestamps.
   If Android is connected and authorized, collection may use read-only ADB
   after `adb devices -l`; no device installation/restart is implied.

Expected diagnostic markers: server `auth_login result=success` and
`device_register result=success` after reconnect; client `EMO_HEARTBEAT` pong
responses without sustained tens-of-seconds gaps. Report any `strict_error_rejected`
retryable/conflict parser message separately, plus any database exception. Do
not edit/truncate either log or include credentials in the returned summary.
Device acceptance remains pending until both platforms' results and logs are
reviewed; success would be personal-lab acceptance, not production readiness.

Manual acceptance: refresh the web UI, load albums from both clients, and play
one track; test a single pause/resume action on each platform. Record expected
versus actual track/audio state and whether one tap succeeds. Return Android
controller logs and Windows `C:/Users/jsdfhasuh/Documents/debug.log`. Check for
prompt reconnect/registration, heartbeat replies, request latency, and absence
of transaction/connection errors. The known old Windows
`strict_error_rejected ... retryable does not match code=conflict` is a separate
client artifact issue and is not silently considered fixed by this deployment.
