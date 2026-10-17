# EmoSonic strict-v2 r5 implementation audit — 2026-07-12

This audit records repository evidence before continuing the r5 server
adaptation. It does not declare any conformance profile ready and does not
authorize deployment.

## Frozen inputs

- Contract: `specs/emosonic_strict_v2_socketio_server_contract.md`
- Expected and observed SHA-256:
  `ca069c6ad52447ea4f7ace7d795460c5ec759e5708b2f45acfbe50903aa4b3a3`
- Audited commit: `02688d1` plus the existing uncommitted Goal 5 worktree
  changes.
- `supysonic/emo/strict_v2_conformance.json` still has Core, Follow, Handoff,
  and Broadcast `codeConformanceReady` set to `false`.

## Do not reimplement

The following foundations already exist and have focused passing tests:

- frozen contract, Goal document, action/settlement fixture, and requirement
  mapping;
- fail-closed packaged conformance manifest loader;
- strict request allowlist, closed payload validation, role normalization,
  capability validation, nested `sessionId` rejection, and executable closed
  output validation;
- request fingerprinting and the per-connection request result cache;
- code/deployment/client/role capability negotiation;
- recipient-specific connection nonce and epoch injection;
- strict registration, replacement of an old same-user `clientId` socket, and
  strict device-list serialization;
- persistent PlaybackContext creation intent, authority device identity,
  terminal tombstone, cursor persistence, and SQLite migration;
- strict Core Context, queue, control routing, and device feedback paths
  covered by `tests.base.test_emo_strict_v2_core`;
- the isolated legacy suite in `tests.emo_legacy_suite`.

## Goal-by-goal status

| Goal | Status | Evidence | Remaining gate |
| --- | --- | --- | --- |
| Goal 0 | Implemented | Contract hash matches; conformance/manifest tests pass; fixture and baseline evidence are tracked. The 31 manifest actions map exactly to the 31 executable `ACTION_SCHEMAS`, including message type and ordered required/optional payload fields, while each manifest entry declares one settlement. REQ-001 through REQ-022 now map to 74 exact dotted unittest methods rather than module-only placeholders; the manifest test imports and verifies every mapped method and module. Historical Follow, Broadcast, Handoff, and descriptor Goals are explicitly marked superseded. | No known code gap. Keep manifest profiles false until their later freeze gates pass. |
| Goal 1 | Substantially implemented, not frozen | `strict_v2_contract.py`, `strict_v2_runtime.py`, correlated disconnect/error behavior, request cache, and event-confirmation tests pass. A real Socket.IO `device.register` request is sent 100 times with one request ID; every replay is byte-for-byte structurally equal to the first ACK and `_register_device` executes exactly once. `validate_strict_output()` has closed schemas for the full strict output action inventory and runs at `_message_for_recipient()` before every strict send or cached event confirmation. Correlated ACK/error/direct-response settlement is now cached before send-buffer reservation and Socket.IO delivery; requester-side backpressure or emit failure is logged without replacing the committed result with `internal_error`. The send boundary also records the settled request ID and suppresses any later correlated message to the same requester, so an unisolated post-ACK helper failure cannot emit a second `internal_error`; a registration fault test proves one ACK, identical retry replay, and one execution each of registration and its failing post-ACK client-list push. All three event-confirmed actions cache their canonical confirmation before their first live business push. `script/verify_emo_strict_v2_ears.py` executes the 74 deduplicated tests mapped to all 22 EARS requirements. | Complete the final fixed-build evidence review before freeze. |
| Goal 2 | Substantially implemented, not frozen | Single-role registration, complete negotiated capability shape, fail-closed Core readiness, device isolation/replacement, heartbeat gate, and device-list tests pass. Strict-recipient `device.list` now projects every strict or legacy device to exactly the nine required boolean capability fields, defaulting absent/non-boolean legacy values to false, while legacy recipients retain their legacy serializer. The exact valid request sent to the Socket.IO server and the server's real ACK and registration error are checked by the packaged Draft 2020-12 registration descriptor. The client integration note's request, error, and ACK JSON examples are also parsed and validated by the same descriptor, preventing the removed legacy `client` field or dual-role-only rule from returning. | The complete fixed-commit Core freeze evidence bundle is still missing. |
| Goal 3 | Substantially implemented, not frozen | Context creation fingerprint, tombstone, authority device persistence, restart hydration, three provider schema files, and migrations exist. Context close and Handoff complete share the per-Context serialization boundary; close atomically writes the tombstone and changes active persisted Handoffs to `failed/context_closed`, while a late complete cannot transfer authority. Strict status/subscribe now return the same `forbidden` payload for another user's existing Context and an unknown ID, closing the cross-user enumeration channel. The single migration unittest entry covers SQLite plus environment-driven PostgreSQL/MySQL clean schemas and frozen `20260708` upgrades. | A copied production-database rehearsal remains external deployment evidence. |
| Goal 4 | Substantially implemented, not frozen | Targeted strict Core tests cover cursor matrices, authority routing, feedback isolation, stale cursor errors, device replacement, and post-commit recovery. Store concurrency proves same-base controls have one accepted winner and one stale loser, plus linearized close-vs-complete outcomes. Strict feedback now proves same-sequence/same-content idempotency under a new requestId, same-sequence conflict, decreasing-sequence conflict, and sequence restart at 1 after a new connection nonce. Strict `playback.update`, `playback.ready`, and `playback.handoff.complete` persist canonical replay confirmation before the first live push. | No known Core state-machine code gap; fixed-build freeze evidence remains outstanding. |
| Goal 5 | Substantially implemented, not frozen | Current worktree implements same-origin/default Origin policy, development wildcard guard, transport/ping settings, connection/action limits, authority emit reservation, single-process validation, shutdown drain, and deployment docs. A real polling client proves that a 257 KiB payload is disconnected before the application handler. An isolated Gunicorn 26 environment exposed and fixed the `worker_int` callback arity; the full safety module then passed with the real hook registered. A concurrent send-buffer integration test proves only one `_emit_message` reaches `socketio.emit` at capacity and the reservation is released afterward. | Fixed-commit evidence and the final freeze review remain open. |
| Goal 6A Follow | Substantially implemented, not frozen | The fixed `tests.base.test_emo_strict_v2_follow` entry covers ACK-only start/stop, same-source retry, different-source conflict, negotiated capability/role/device/user gates, disconnect cleanup, Context-close cleanup, and a dual-role follower being forbidden from controlling its source Context. The latter exposed and fixed a strict dispatcher bypass of the Follow isolation guard. A real registration test derives optional readiness from the packaged all-false manifest and proves Follow still negotiates false. | Collect fixed-build dual-client evidence before changing readiness. |
| Goal 6B Handoff | Substantially implemented, not frozen | The fixed Handoff entry covers controller/source/target gates, strict start/prepare/commit/release/status schemas, retry, failure, cancel, timeouts, disconnect, restart reconciliation, event-confirmation replay, atomic authority transfer, and Context-close fencing. Store races prove complete-vs-cancel, complete-vs-timeout, and close-vs-complete outcomes. Injected prepare, commit, ready-false, completed-status, cancel, timeout, and disconnect push failures preserve the ACK/event-confirmed settlement, terminal DB state, subsequent status delivery, and canonical replay rather than caching `internal_error` after commit. A cross-socket fault test proves a failed origin start ACK still leaves exactly one target prepare and a replayable ACK without a second prepare. Ready success, ready failure, and complete tests also assert their canonical confirmations are cached before commit/cancel/status delivery starts. Normal cancel delivery is fixed as ACK then cancel/status for the origin/source and cancel-only for the target. | No known Handoff state-machine or multi-recipient fault gap; collect the fixed-build freeze bundle before changing readiness. |
| Goal 6C Broadcast | Substantially implemented, not frozen | The fixed Broadcast entry covers authority/controller permissions, participant filtering, all-requested-skipped behavior with forced authority, cross-user failure, exact schemas, replay, cursor matrices, 0/partial/all feedback status shapes, terminal idempotency, owner-disconnect survival, authority disconnect/reconnect/timeout, restart stop, and simultaneous start. A real background task proves disconnect scheduling, sleep, terminal stop, and push; multi-participant fanout proves sorted sid delivery with distinct nonces, and partial push failure leaves canonical state recoverable through status. | Collect fixed-build dual-client evidence before changing readiness. |
| Goal 7 | Partially implemented | Registration metadata, schema hash, package manifest entries, deployment documentation, Goal 0 evidence, a reproducible three-database migration job, packaging/EARS evidence runners, descriptor-validated integration examples, superseded historical Goal banners, and canonical-document redirect tests exist. Packaging verification covers wheel/sdist installation and fail-closed metadata. | Fixed-build Android/Windows verification, copied-database rehearsal, human release-candidate review, and the final `<serverBuildCommit>/` evidence directory are missing. |

## Definition-of-Done evidence matrix

This matrix distinguishes repository-complete behavior from freeze or external
evidence. `Implemented` does not mean a profile is ready or approved for
deployment.

| DoD | Current evidence | Status |
| --- | --- | --- |
| 1. Normative spec unchanged | Frozen contract SHA-256 still equals `ca069c6ad52447ea4f7ace7d795460c5ec759e5708b2f45acfbe50903aa4b3a3`; legacy reference path is a redirect. | Implemented |
| 2. Core request/response/push schemas closed | Action manifest maps all 31 client actions to executable validators; production send boundary validates the closed output inventory. | Implemented |
| 3. One settlement per request | ACK/direct/error settlement is stored before delivery; event-confirmed actions suppress ACK and cache one canonical confirmation. The generic send boundary suppresses a later correlated settlement after the first requester result, including a post-registration-ACK helper exception. | Implemented |
| 4. Dedupe and event replay | TTL, fingerprint conflict, disconnect cleanup, 100-repeat registration, replay-only confirmation, post-commit delivery failures, and identical ACK replay after a suppressed post-settlement exception are tested. | Implemented |
| 5. Auth/register/device/heartbeat | Bootstrap correlation, complete register negotiation, replacement isolation, device projection, and post-register heartbeat gate pass. | Implemented |
| 6. Capability intersection | Code readiness, deployment switches, client booleans, role dependencies, and packaged all-false override are tested. | Implemented |
| 7. Context identity and tombstone persistence | Creation fingerprint, authority client/device, tombstone, restart hydration, and intent conflict are persisted and tested. | Implemented |
| 8. Cursor matrix | Queue, controls, close, Handoff, and Broadcast cursor assertions cover accepted and stale paths. | Implemented |
| 9. Unique authority control routing | Authority client/device/sid is resolved before mutation; emit capacity is reserved before commit; recipient-only control tests pass. | Implemented |
| 10. Feedback isolation and no ACK | Context cursors remain unchanged; sequence idempotency/conflict/reconnect and event confirmation are tested. | Implemented |
| 11. No strict sessionId/targetClientId leakage | Recursive input rejection and closed output validation cover request, direct response, and fanout. | Implemented |
| 12. Transport/security/rate/backpressure/logging | 257 KiB transport close, Origin policy, exact rate boundaries, emit reservation, TLS docs, and strict log filtering pass. | Implemented |
| 13. Multi-worker fail-fast | CLI and Gunicorn configuration tests reject more than one process only when strict Core is effectively ready. | Implemented |
| 14. Restart reconciliation | Context/tombstone hydration and explicit Follow/Handoff/Broadcast transient termination are tested. | Implemented |
| 15. Three-provider schema/migration sync | SQLite and pinned PostgreSQL/MySQL clean/upgrade automation passed; CI and Compose use the same unittest entry. | Automated requirement implemented; copied-production-DB rehearsal still external |
| 16. Optional values false before readiness | Shipped conformance manifest remains all false; negotiation tests prove optional values remain false. | Implemented |
| 17. Optional profile self-gates | Follow, Handoff, and Broadcast fixed suites are separate and fail closed when not negotiated. No readiness bit is enabled. | Implemented gate; freeze not executed |
| 18. Descriptor/artifact/real ACK consistency | Descriptor validates real request/ACK/error, client-note JSON examples, and installed wheel/sdist fail-closed probes. | Implemented |
| 19. Full unittest, coverage, conformance recorded | Latest coverage run executed all 1188 tests plus the network suite; 74-test EARS and fixed profile entries are recorded below. | Implemented for current worktree |
| 20. Android and Windows real-client verification | No owner/build IDs/fixed-server-commit evidence exists in the workspace. | Missing external evidence |
| 21. Implementation vs readiness vs rollout documented | Deployment guide, audit, integration note, and Goal scope distinguish all three states. | Implemented |
| 22. Deployment switches default off | Config defaults and sample remain off; conformance profiles remain false. | Implemented |

The goal therefore remains active. Repository work can close DoD 19, but DoD
20 and the fixed-build/human-review portions of Goal 7 require external evidence
and must not be synthesized.

## Verification collected in this audit

Passing commands:

```text
python -m unittest \
  tests.base.test_emo_strict_v2_conformance \
  tests.base.test_emo_strict_v2_manifest \
  tests.base.test_emo_strict_v2_contract \
  tests.base.test_emo_strict_v2_readiness \
  tests.base.test_emo_strict_v2_runtime \
  tests.base.test_emo_strict_v2_safety \
  tests.base.test_emo_strict_v2_core \
  tests.base.test_emo_strict_v2_follow \
  tests.base.test_emo_ws_state \
  tests.base.test_emo_ws_store \
  tests.base.test_emo_schema_migration \
  tests.base.test_config \
  tests.base.test_emo_logging \
  tests.emo_legacy_suite

253 tests, OK, 3 skipped: one because Gunicorn is not installed and two
because external database URIs were not configured for the in-process run

python -m unittest \
  tests.base.test_emo_strict_v2_conformance \
  tests.base.test_emo_strict_v2_manifest \
  tests.base.test_emo_strict_v2_contract \
  tests.base.test_emo_strict_v2_readiness \
  tests.base.test_emo_strict_v2_runtime

31 tests, OK

python -m unittest tests.base.test_emo_strict_v2_core

36 tests, OK

python -m unittest \
  tests.base.test_emo_ws_store \
  tests.base.test_emo_schema_migration

28 tests, OK, 2 skipped because external database URIs were not configured

sh tests/emo_migrations/run.sh

4 tests, OK on:

- `postgres:16.9-alpine@sha256:7c688148e5e156d0e86df7ba8ae5a05a2386aaec1e2ad8e6d11bdf10504b1fb7`
- `mysql:8.4.5@sha256:679e7e924f38a3cbb62a3d7df32924b83f7321a602d3f9f967c01b3df18495d6`

The workspace has no Docker Compose plugin, so `run.sh` used its isolated
Docker-network fallback while preserving the required unittest entry.

python -m unittest tests.emo_legacy_suite

83 tests, OK

python -m unittest tests.base.test_emo_strict_v2_safety

13 tests, OK, 1 skipped because Gunicorn is not installed in this workspace

python -m unittest tests.base.test_config

6 tests, OK

python -m unittest tests.base.test_emo_logging

22 tests, OK

python -m unittest tests.base.test_emo_strict_v2_follow

4 tests, OK

python -m unittest tests.base.test_emo_strict_v2_handoff

13 tests, OK

python -m unittest tests.base.test_emo_strict_v2_broadcast

10 tests, OK

python -m unittest \
  tests.base.test_emo_strict_v2_contract \
  tests.base.test_emo_strict_v2_core \
  tests.base.test_emo_strict_v2_follow \
  tests.base.test_emo_strict_v2_handoff \
  tests.base.test_emo_strict_v2_broadcast \
  tests.base.test_emo_strict_v2_safety \
  tests.base.test_emo_ws \
  tests.base.test_emo_ws_state \
  tests.base.test_emo_ws_store \
  tests.emo_legacy_suite

348 tests, OK, 1 skipped because Gunicorn is not installed

python -m unittest

1145 tests, OK, 3 skipped

python script/verify_emo_strict_v2_packaging.py

Passed for wheel and sdist. Both archives contain
`strict_v2_conformance.json` and `strict_v2_registration_descriptor.json`.
Each artifact was separately installed into a temporary virtual environment;
the installed runtime contract SHA-256 and descriptor schemaHash matched the
source, and pristine, missing, invalid-JSON, and contract-hash-mismatched
manifest probes all kept Core, Follow, Handoff, and Broadcast disabled.

python -m unittest \
  tests.base.test_emo_registration_descriptor \
  tests.base.test_emo_protocol_metadata \
  tests.base.test_emo_ws

145 tests, OK. This includes descriptor validation of the exact valid
`device.register` request sent to the Socket.IO test server and its real ACK,
plus descriptor validation of a real correlated registration error emitted on
the same strict connection.

python -m unittest tests.net.suite

5 tests, OK, 1 skipped.

coverage erase
coverage run -m unittest

1145 tests, OK, 3 skipped.

coverage run -a -m unittest tests.net.suite

5 tests, OK, 1 skipped.

coverage report -m

20,410 statements, 3,531 missed, 83% total coverage. Relevant strict-v2
modules include `strict_v2_runtime.py` at 99%, `strict_v2_safety.py` at 95%,
`ws_store.py` at 92%, `ws_state.py` at 86%, and `ws.py` at 75%.

python -m unittest \
  tests.base.test_emo_strict_v2_core.StrictV2CoreTestCase.\
test_register_repeated_100_times_replays_without_second_mutation

1 test, OK. One real registration mutation and 99 cached replays produced
identical ACKs; `_register_device` was called exactly once.

python -m unittest \
  tests.base.test_emo_strict_v2_manifest \
  tests.base.test_emo_strict_v2_contract

14 tests, OK. Every manifest action maps to one executable request validator
with the same type and closed required/optional payload field inventory.

python -m unittest \
  tests.base.test_emo_strict_v2_core.StrictV2CoreTestCase.\
test_packaged_optional_false_manifest_overrides_enabled_deployment

1 test, OK. With every deployment switch enabled and only Core simulated as
ready, the packaged false values still force Follow, Handoff, and Broadcast
negotiated capabilities to false.

python -m unittest \
  tests.base.test_emo_strict_v2_core \
  tests.base.test_emo_strict_v2_manifest \
  tests.base.test_emo_strict_v2_readiness

47 tests, OK after the 100-replay, executable-manifest-mapping, and packaged
optional-readiness additions.

python -m unittest \
  tests.base.test_emo_strict_v2_contract \
  tests.base.test_emo_strict_v2_handoff \
  tests.base.test_emo_ws_store \
  tests.emo_legacy_suite

131 tests, OK

python -m unittest \
  tests.base.test_emo_strict_v2_follow \
  tests.base.test_emo_strict_v2_core \
  tests.emo_legacy_suite

123 tests, OK

python -m unittest \
  tests.base.test_emo_strict_v2_safety \
  tests.base.test_emo_strict_v2_follow \
  tests.base.test_emo_strict_v2_core \
  tests.emo_legacy_suite

136 tests, OK, 1 skipped because Gunicorn is not installed in this workspace

python -m unittest \
  tests.base.test_emo_strict_v2_core.StrictV2CoreTestCase.\
test_strict_emit_rejects_invalid_output_before_socketio_send \
  tests.base.test_emo_strict_v2_contract

16 tests, OK. The send-boundary test proves a malformed strict canonical push
raises `StrictOutputValidationError` before `socketio.emit`; the validator
module covers the closed output inventory, provenance, settlement correlation,
ACK/error conditionals, and all canonical push/direct-response shapes.

python -m unittest \
  tests.base.test_emo_ws_store.EmoWebSocketStoreTestCase.\
test_close_and_complete_handoff_are_linearized_by_context_tombstone \
  tests.base.test_emo_ws_state.EmoWebSocketStateTestCase.\
test_broadcast_deadline_and_explicit_stop_have_one_terminal_winner \
  tests.base.test_emo_strict_v2_handoff.StrictV2HandoffTestCase.\
test_context_close_fences_late_handoff_complete

3 tests, OK. These assert both legal close/complete orderings, the loser
`context_closed` wire error when close commits first, persisted and in-memory
terminal Handoff state, canonical Context cursor/authority values, and exactly
one Broadcast terminal mutation for deadline-vs-explicit-stop.

python -m unittest \
  tests.base.test_emo_ws_store \
  tests.base.test_emo_ws_state

59 tests, OK

python -m unittest \
  tests.base.test_emo_strict_v2_core \
  tests.base.test_emo_strict_v2_follow \
  tests.base.test_emo_strict_v2_handoff \
  tests.base.test_emo_strict_v2_broadcast

66 tests, OK. Every strict output emitted by these fixed profile suites is
validated again inside the production send boundary.

python -m unittest

1157 tests, OK, 3 skipped. This is the latest full-suite result after strict
device-list normalization, runtime output validation, Context-close/Handoff
fencing, and Broadcast deadline cleanup.

python -m unittest tests.net.suite

5 tests, OK, 1 skipped

git diff --check

Passed. The frozen contract SHA-256 was rechecked as
`ca069c6ad52447ea4f7ace7d795460c5ec759e5708b2f45acfbe50903aa4b3a3`,
and all four conformance profiles remained false.

python script/verify_emo_strict_v2_ears.py

74 tests, OK. The manifest maps every EARS requirement REQ-001 through
REQ-022 to exact dotted unittest methods. This fixed entry includes real
Socket.IO behavior, contract/output validation, cache TTL and disconnect
cleanup, CSPRNG nonce generation, transport overflow, persistence/migration,
restart termination, per-recipient Broadcast provenance, live timer behavior,
post-Handoff authority, post-commit fault recovery, and optional-profile
evidence. The added Handoff cross-socket case proves a failed origin ACK is
already replayable while the target receives exactly one prepare.

python -m unittest \
  tests.base.test_emo_strict_v2_manifest \
  tests.base.test_emo_strict_v2_runtime \
  tests.base.test_emo_strict_v2_core \
  tests.base.test_emo_ws_state

84 tests, OK

python -m venv --system-site-packages /tmp/supysonic-gunicorn-venv
/tmp/supysonic-gunicorn-venv/bin/python -m pip install gunicorn
/tmp/supysonic-gunicorn-venv/bin/python -m unittest \
  tests.base.test_emo_strict_v2_safety

Gunicorn 26.0.0; 13 tests, OK with no skip. Constructing `GunicornApp`
proves the one-argument `worker_int` callback registers successfully, and the
interrupt callback enters the mounted Flask application context and invokes
the configured strict-v2 graceful drain.

python -m unittest \
  tests.base.test_emo_strict_v2_core \
  tests.base.test_emo_ws_state \
  tests.base.test_emo_strict_v2_manifest

79 tests, OK. This includes the concurrent send-buffer test: while the first
strict emit holds the only configured per-connection slot, a second emit is
rejected before `socketio.emit`; completion releases the slot.

sphinx-build -M html . _build

build succeeded

python -m unittest \
  tests.base.test_emo_strict_v2_core \
  tests.base.test_emo_strict_v2_follow \
  tests.base.test_emo_strict_v2_handoff \
  tests.base.test_emo_strict_v2_broadcast \
  tests.base.test_emo_ws

198 tests, OK. This includes the live Broadcast disconnect timer, canonical
post-Handoff Broadcast authority, sorted per-recipient nonce fanout, partial
Broadcast delivery failure recovery, and Handoff start/ready/complete
post-commit failure recovery. `git diff --check` also passed.

python -m unittest \
  tests.base.test_emo_strict_v2_core \
  tests.base.test_emo_strict_v2_follow \
  tests.base.test_emo_strict_v2_handoff \
  tests.base.test_emo_strict_v2_broadcast \
  tests.base.test_emo_ws

205 tests, OK after extending post-commit failure handling to strict
`playback.update`, Handoff ready-false/cancel/timeout/disconnect paths, and
Broadcast pause-failure timer scheduling. `git diff --check` passed.

python -m unittest

1168 tests, OK, 3 skipped. This was the full-suite result after the live
Broadcast timer/provenance/fault tests and Handoff post-commit settlement
hardening.

python -m unittest tests.net.suite

5 tests, OK, 1 skipped.

python -m unittest \
  tests.base.test_emo_strict_v2_core \
  tests.base.test_emo_strict_v2_follow \
  tests.base.test_emo_strict_v2_handoff \
  tests.base.test_emo_strict_v2_broadcast \
  tests.base.test_emo_ws

208 tests, OK. This includes settlement-before-delivery assertions for
`playback.update`, both strict `playback.ready` terminal directions, and
`playback.handoff.complete`, plus the origin-ACK/target-prepare Handoff fault
case.

python -m unittest

1178 tests, OK, 3 skipped. This was the full-suite result after
correlated settlement-before-delivery and event-confirmation-before-push
hardening.

python -m unittest tests.net.suite

5 tests, OK, 1 skipped.

git diff --check

Passed.

python -m unittest \
  tests.base.test_emo_strict_v2_core \
  tests.base.test_emo_strict_v2_follow \
  tests.base.test_emo_strict_v2_handoff \
  tests.base.test_emo_strict_v2_broadcast \
  tests.base.test_emo_ws

214 tests, OK after the requirement-level audit added strict feedback
sequence restart/idempotency, anti-enumeration Context reads, Follow source
control isolation, Broadcast owner-disconnect/all-skipped/all-feedback
coverage, descriptor-backed documentation checks, and generic duplicate-
settlement suppression. The registration fault injection proves that a
post-ACK client-list push exception emits no second correlated error, while a
retry replays the identical ACK without repeating either registration or the
failed push.

python -m unittest \
  tests.base.test_emo_registration_descriptor \
  tests.base.test_emo_strict_v2_manifest

19 tests, OK. Client integration JSON examples validate against the packaged
descriptor; historical Goal banners and canonical-document redirects are
enforced.

sphinx-build -M html . _build

Build succeeded.

coverage erase
coverage run -m unittest

1188 tests, OK, 3 skipped.

coverage run -a -m unittest tests.net.suite

5 tests, OK, 1 skipped.

coverage report -m

20,898 statements, 3,576 missed, 83% total coverage. Relevant strict-v2
modules: `strict_v2_runtime.py` 99%, `strict_v2_safety.py` 95%,
`ws_store.py` 92%, `ws_state.py` 86%, `strict_v2_contract.py` 87%, and
`ws.py` 76%.

python script/verify_emo_strict_v2_packaging.py

Passed for the current wheel and sdist. Each artifact was installed into a
separate temporary environment; installed missing, invalid, and
contract-hash-mismatched manifests all failed closed.
```

Mixed transition suite after fixed r5 Broadcast replacement:

```text
python -m unittest tests.base.test_emo_ws

123 tests, OK
```

The four pre-r5 Broadcast transition methods covering Broadcast-owned Context
creation, existing-Context rejection, restart restoration, and mixed legacy
prepare payloads are now named `superseded_r4_*`. Their r5 replacements live in
the fixed Broadcast entry and prove that Broadcast uses an existing Context,
does not restore as active after restart, and emits closed strict payloads.
The isolated legacy suite remains green.

## Next implementation order

1. Continue the remaining fault-injection and multi-recipient push-order audit;
   live Broadcast timer, post-Handoff authority, per-sid provenance, partial
   Broadcast fanout failure, and Handoff post-commit failure paths are covered.
2. Collect a fixed-commit verification bundle, rerun the full suite/coverage,
   and keep every readiness/deployment gate false until all internal gates pass.
3. Treat copied-production-database rehearsal and fixed-build Android/Windows
   conformance as external evidence; do not synthesize them or enable a profile
   before they are supplied and reviewed.
