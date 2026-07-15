# EmoSonic strict-v2 r7 Android/Windows acceptance template

Copy this file to
`docs/verification/emosonic_strict_v2_r7/<serverBuildCommit>/android-windows.md` for the final run.
Do not fill or sign it with an uncommitted build or `serverBuildCommit: unknown`.

Before device testing, collect the automated candidate evidence from a clean checkout. Both real
provider URIs are required and are read from the environment without being written into the report:

```bash
export EMO_SERVER_BUILD_COMMIT=<40-character-git-head>
export SUPYSONIC_TEST_POSTGRES_URI=<isolated-postgres-uri>
export SUPYSONIC_TEST_MYSQL_URI=<isolated-mysql-uri>
python script/collect_emo_strict_v2_r7_evidence.py
```

The collector refuses dirty trees, build/HEAD mismatches, stale contract or protocol metadata,
incomplete REQ-001…025 inventory, premature readiness, missing provider configuration, and an
existing evidence directory.

## Run identity

| Field | Value |
| --- | --- |
| Date/time and timezone | |
| Server build commit | |
| Flutter build ID | |
| Protocol version | `2.2.0` |
| Contract SHA-256 | `7e5402a4c32fb366c3755239e4993ef5634177e7db9748bff83b32926cbd2b1f` |
| Android device/app version | |
| Windows device/app version | |
| Deployment configuration reference | |

For every step, preserve the relevant client and server log lines with `requestId`,
`playbackContextId`, `controlVersion`, and `queueRevision`. Redact credentials and tokens.

## Scenario A: original remote-control path

| Step | Expected evidence | Result/log reference |
| --- | --- | --- |
| Windows exposes queue length 50, index 39 | Canonical Context snapshot | |
| Android selects Windows from `device.list` | Exact client/device pair selected | |
| Android sends `playback.context.list` | One binding, no session fallback | |
| Android subscribes and requests status | ACK plus index 39/cursors | |
| Android sends pause | Windows executes once | |
| Android sends `queue.playItem` | Correct queue item executes once | |
| Windows sends `playback.update` | Subscribers receive canonical feedback | |

## Scenario B: reconnect

| Step | Expected evidence | Result/log reference |
| --- | --- | --- |
| Windows reconnects with the same client/device pair | New physical nonce | |
| Android sends list with a new request ID | Canonical binding rediscovered | |
| Android re-subscribes and hydrates status | Control resumes | |
| Query same client with a different device session | Successful empty list | |

## Scenario C: multiple Contexts

| Step | Expected evidence | Result/log reference |
| --- | --- | --- |
| Windows creates a second active Context | Mutation committed | |
| Non-subscriber controller receives invalidation | No request ID or Context ID in event | |
| Android re-lists | Multiple sorted bindings | |
| Control races ahead of UI invalidation | Full cursor `conflict`, no player command | |
| One Context closes | Invalidation followed by one binding | |
| Android re-lists/status and controls | Unique scope recovers | |

## Scenario D: Handoff

| Step | Expected evidence | Result/log reference |
| --- | --- | --- |
| Context hands off from Windows to another player | One Context ID, new authority | |
| Controllers receive old/new pair invalidations | Two deterministic events or one if pairs equal | |
| Old Windows pair is listed | Context absent | |
| New player pair is listed | Same Context ID present | |
| Existing subscribers converge | Canonical authority/cursors | |
| Replay completion | No duplicate invalidation | |

## Scenario E: invalidation failure and recovery

| Step | Expected evidence | Result/log reference |
| --- | --- | --- |
| Inject one controller send-buffer/emit failure | Failure is isolated to that sid | |
| Commit binding mutation | Mutation remains committed | |
| Failed controller disconnects | Physical Socket closes | |
| Other controller receives event | Normal delivery with its provenance | |
| Failed controller reconnects and lists | Canonical binding recovery | |

## Arrival-order checks

Record both cases and the local `discoveryGeneration` before/after each delivery. The generation is
client-local and must never appear on the wire.

| Case | Expected disposition | Result/log reference |
| --- | --- | --- |
| Invalidation arrives before an older list response | Discard older response | |
| List response arrives before invalidation | Accept temporarily, then invalidate and requery | |

## Final decision

| Gate | Pass/fail | Reviewer |
| --- | --- | --- |
| Scenarios A-E complete | | |
| No session/client/device-derived Context ID | | |
| Logs align across server, Android, and Windows | | |
| Build IDs and contract hash are exact | | |
| Ready for final conformance evidence freeze | | |
