# OpenWrt mpv player, phase 1

Baseline: Emosonic-Server master d599461e43527b6094031164505bab6fc159c1f9.
Branch: feat/openwrt-mpv-player-20261029. Source: user handoff dated 2026-10-29.

## Scope
Independent Python asyncio player daemon and SSH CLI, same canonical server
context/queue as App remote controls. One mpv subprocess, private Unix sockets,
ALSA USB output. Ten Core-only capabilities; no Handoff, Follow or Broadcast.
No production server/client engine changes, no deployment, no host driver changes.

## Sequence
1. Read contract and workflow boundaries; current baseline matches handoff.
2. Build IPC adapter and deterministic tests; isolate request/process/load lifetimes.
3. Build real Socket.IO registration/context/control and CLI, stream authentication.
4. Test new identity -> context -> queue -> execution -> confirmation -> EOF.
5. Build independent ARM64 image with build-only workflow; document deployment,
   physical acceptance and rollback for the user to execute separately.

## Rules
CLI sends canonical protocol operations via playerd, never direct mpv commands.
Timeouts/disconnects stop or pause audio; no stale request replay. Queue mutations
require base revisions; duplicate track IDs are distinct indexed occurrences.
Playback results, canonical acknowledgements and physical sound are different.
Credentials stay in restricted files and private IPC, never argv or logs.
No privileged containers, exposed ports or Docker socket. Stable device identity,
new physical connection provenance; reconnect remains paused until reconciliation.

## Baseline checks
Production conformance manifest is false. Existing explicit development mode and
local-test-evidence switches permit isolated integration tests; do not modify the
manifest or invent deployment readiness. Actual user's server mode is unknown.
Only player/, docs/plans/ and a separate workflow are in scope: current server
Docker publication paths must not be touched.

## Acceptance
Unit: framing, timeout, EOF races, authority/version checks, reconnect, sanitization.
Integration: actual local server using its documented development configuration;
real mpv silent-output checks are not USB physical acceptance. App and CLI commands
must share canonical versions. Device sound, three-track continuity, mobile UI,
resource measurements and hotplug on OpenWrt are NOT_RUN until user deployment.

## Reporting
Record exact commands, results, commit, image build and CI independently. Do not
label a partial scaffold a working product; unsupported behaviors must fail closed.

## Discovered handoff difference
Real server integration rejects duplicate `queueSongIds`. Preserve server
contract; reject duplicate playlist selections explicitly, do not deduplicate.
No server schema expansion is included in this branch.
