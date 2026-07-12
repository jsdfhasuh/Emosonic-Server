# EmoSonic strict-v2 r5 Goal 0 baseline

- Captured: 2026-07-12
- Repository HEAD before the baseline-only commit: `a451555`
- Protocol version: `2.1.0`
- Contract SHA-256: `ca069c6ad52447ea4f7ace7d795460c5ec759e5708b2f45acfbe50903aa4b3a3`
- Status: protocol conversion is in progress; this is not conformance evidence.

## Commands and results

```text
python -m unittest \
  tests.base.test_emo_registration_descriptor \
  tests.base.test_emo_protocol_metadata \
  tests.base.test_emo_ws_state \
  tests.base.test_emo_ws_store

Ran 68 tests in 1.791s
FAILED (failures=2)
```

```text
python -m unittest tests.base.test_emo_ws

Ran 138 tests in 13.896s
FAILED (failures=2, errors=38)
```

## Failure attribution

| Scope | Count | Classification | Required resolution |
| --- | ---: | --- | --- |
| Registration descriptor tests | 2 failures | Current uncommitted descriptor and fixtures disagree: the ACK fixture still contains the removed `client` object, and the error fixture omits required `retryable`. | Make descriptor, real ACK/error builders, and r5 fixtures agree. Do not restore the legacy `client` object. |
| WebSocket tests waiting for ACK after create/status | 33 errors | r5 new expectation versus stale test helper. The implementation has begun using direct responses, while `create_playback_context` and dependent tests still call `get_ack`. | Add settlement-aware helpers and assert the unique r5 settlement for each action. |
| Persisted/duplicate Handoff tests | 3 errors | Historical ACK fields (`originClientId`, `duplicate`) are not part of the r5 five-field start ACK. | Replace assertions with the r5 ACK and canonical replay rules. |
| Device registration compatibility test | 1 error | Historical test reads `payload.client`; r5 explicitly forbids this object in strict ACK. | Assert `clientId`, `deviceSessionId`, negotiated capabilities, and strict metadata instead. |
| Queue sync compatibility test | 1 error | Historical test reads queue state from ACK; r5 ACK payload contains only `action`. | Assert canonical `queue.context.sync` push separately from the ACK. |
| Legacy Follow control test | 1 failure | Real regression: the shared strict error remap changes a legacy `follow_control_forbidden` response to `forbidden`. | Restrict r5 error mapping to the negotiated strict dispatcher and preserve legacy behavior. |
| Strict register metadata test | 1 failure | Real implementation/descriptor mismatch: the emitted ACK does not validate against the current descriptor and lacks the final negotiated shape. | Close the descriptor and implement complete readiness-based `negotiatedCapabilities`. |

All failures are assigned to an r5 expectation update, a legacy regression, or an implementation defect. Removing
the failing assertions is not an accepted resolution.

## Test ownership

| Test surface | Ownership |
| --- | --- |
| Existing non-v2 actions in `tests.base.test_emo_ws` | legacy regression suite |
| Core strict actions | `tests.base.test_emo_strict_v2_core` |
| Follow | `tests.base.test_emo_strict_v2_follow` |
| Handoff | `tests.base.test_emo_strict_v2_handoff` |
| Broadcast | `tests.base.test_emo_strict_v2_broadcast` |
| Contract/action/REQ inventory | `tests.base.test_emo_strict_v2_manifest` |
| Code conformance fail-closed behavior | `tests.base.test_emo_strict_v2_conformance` |
| Database migrations | `tests.base.test_emo_schema_migration` |
