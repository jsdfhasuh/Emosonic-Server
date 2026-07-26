# strict-v2 2.8.0 / r18 requirement mapping

本表追踪权威契约 REQ-001—REQ-067 的实施阶段和目标自动化测试。Goal 0 只建立追踪关系，不把尚未
完成的 Goal 标成通过；每个后续 Goal 提交时应把对应行的状态改为 `Verified`，并记录实际测试方法。

| REQ | 实施 Goal | 主要目标测试 | 状态 |
| --- | --- | --- | --- |
| REQ-001—REQ-009 | Goal 1 | `test_emo_strict_v2_contract`, `test_emo_protocol_metadata`, Core Socket and JS client tests | Verified |
| REQ-010—REQ-014 | Goal 1, Goal 3 | contract, timing persistence, effective-at and Core Socket tests | Verified |
| REQ-015—REQ-022 | Goal 2, Goal 11 | persistence/limit tests verified in `test_emo_broadcast_store`; readiness remains | In progress |
| REQ-023—REQ-038 | Goal 1, Goal 3, Goal 5, Goal 6 | Core context/control/update, barrier, Handoff and Broadcast-aware source transaction tests | Verified |
| REQ-039 | Goal 2, Goal 4, Goal 6 | persistent source-derived start/control/queue/update projection and cursor tests | Verified |
| REQ-040 | Goal 7 | feedback schema, settlement and Socket tests | Planned |
| REQ-041 | Goal 5, Goal 10 | Context/binding/source barriers verified; terminal transaction remains | In progress |
| REQ-042 | Goal 4, Goal 10 | start role fanout verified; terminal and restore remain | In progress |
| REQ-043 | Goal 9 | source reconnect and actual-state tests | Planned |
| REQ-044 | Goal 2, Goal 10 | store terminal transaction/idempotency verified; Socket/restart remains | In progress |
| REQ-045—REQ-046 | Goal 3, Goal 6 | rate/effective-at eligibility, anchored progress, throttle and single-revision tests | Verified |
| REQ-047 | Goal 5, Goal 10 | restorePending ensure and no-side-effect replay verified; terminal drain remains | In progress |
| REQ-048 | Goal 3, Goal 4 | fresh/settled/playing helper and atomic start integration verified | Verified |
| REQ-049 | Goal 7 | participant outcome and deadline tests | Planned |
| REQ-050 | Goal 3, Goal 6 | server clock gate verified; action late-policy remains | In progress |
| REQ-051 | Goal 2, Goal 4, Goal 10 | 1024 limit, rate-limited new intent and persistent replay verified; Context-close cleanup remains | In progress |
| REQ-052—REQ-053 | Goal 4, Goal 5 | crash-safe start, frozen pair and all Context/binding mutation barriers verified | Verified |
| REQ-054 | Goal 6 | Context-first natural source queue transition and single queue.sync mirror tests | Verified |
| REQ-055 | Goal 10 | restart terminal tests | Planned |
| REQ-056 | Goal 6 | source queue clear, pre-idle terminal Snapshot and atomic rollback tests | Verified |
| REQ-057 | Goal 4 | source/ordinary/controller-only priority and fanout tests | Verified |
| REQ-058—REQ-059 | Goal 1, Goal 3 | nonce clock warm-up/expiry, Handoff gate and sampled-position persistence tests | Verified |
| REQ-060 | Goal 7 | earliest-unconfirmed deadline tests | Planned |
| REQ-061—REQ-062 | Goal 2, Goal 10 | cursor-preserving compact recovery and 256/512/1024 bounds verified; Socket drain remains | In progress |
| REQ-063—REQ-064 | Goal 6 | deterministic action/equal-commit and exact persisted status anchor tests | Verified |
| REQ-065 | Goal 8 | per-pair delivery and resync tests | Planned |
| REQ-066 | Goal 7, Goal 10 | terminal state-domain tests | Planned |
| REQ-067 | Goal 8 | expired-feedback rejection and replacement delivery tests | Planned |
