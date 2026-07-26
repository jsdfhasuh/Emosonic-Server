# strict-v2 2.8.0 / r18 requirement mapping

本表追踪权威契约 REQ-001—REQ-067 的实施阶段和目标自动化测试。Goal 0 只建立追踪关系，不把尚未
完成的 Goal 标成通过；每个后续 Goal 提交时应把对应行的状态改为 `Verified`，并记录实际测试方法。

| REQ | 实施 Goal | 主要目标测试 | 状态 |
| --- | --- | --- | --- |
| REQ-001—REQ-009 | Goal 1 | `test_emo_strict_v2_contract`, `test_emo_protocol_metadata`, Core Socket and JS client tests | Verified |
| REQ-010—REQ-014 | Goal 1, Goal 3 | contract, timing persistence, effective-at and Core Socket tests | Verified |
| REQ-015—REQ-022 | Goal 2, Goal 11 | persistence/limit tests verified in `test_emo_broadcast_store`; readiness remains | In progress |
| REQ-023—REQ-038 | Goal 1, Goal 3, Goal 5, Goal 6 | Core Context, control and handoff tests | Planned |
| REQ-039 | Goal 2, Goal 4, Goal 6 | persistent snapshot/revision store verified; source projection/Socket remains | In progress |
| REQ-040 | Goal 7 | feedback schema, settlement and Socket tests | Planned |
| REQ-041 | Goal 5, Goal 10 | Context/binding barrier and terminal transaction tests | Planned |
| REQ-042 | Goal 4, Goal 10 | role-specific terminal and restore tests | Planned |
| REQ-043 | Goal 9 | source reconnect and actual-state tests | Planned |
| REQ-044 | Goal 2, Goal 10 | store terminal transaction/idempotency verified; Socket/restart remains | In progress |
| REQ-045—REQ-046 | Goal 3, Goal 6 | rate/effective-at eligibility verified; progress revision remains | In progress |
| REQ-047 | Goal 5, Goal 10 | restorePending ensure and mutation barrier tests | Planned |
| REQ-048 | Goal 3, Goal 4 | fresh/settled/playing helper verified; start integration remains | In progress |
| REQ-049 | Goal 7 | participant outcome and deadline tests | Planned |
| REQ-050 | Goal 3, Goal 6 | server clock gate verified; action late-policy remains | In progress |
| REQ-051 | Goal 2, Goal 4, Goal 10 | 1024 limit and persistent intent replay verified; Context-close cleanup remains | In progress |
| REQ-052—REQ-053 | Goal 4, Goal 5 | crash-safe entry and frozen pair tests | Planned |
| REQ-054 | Goal 6 | natural source track transition tests | Planned |
| REQ-055—REQ-056 | Goal 9, Goal 10 | restart, waiting, idle terminal and timeout tests | Planned |
| REQ-057 | Goal 4 | controller-only fanout tests | Planned |
| REQ-058—REQ-059 | Goal 1, Goal 3 | nonce clock warm-up/expiry, Handoff gate and sampled-position persistence tests | Verified |
| REQ-060 | Goal 7 | earliest-unconfirmed deadline tests | Planned |
| REQ-061—REQ-062 | Goal 2, Goal 10 | cursor-preserving compact recovery and 256/512/1024 bounds verified; Socket drain remains | In progress |
| REQ-063—REQ-064 | Goal 6 | deterministic action and immutable status anchor tests | Planned |
| REQ-065 | Goal 8 | per-pair delivery and resync tests | Planned |
| REQ-066 | Goal 7, Goal 10 | terminal state-domain tests | Planned |
| REQ-067 | Goal 8 | expired-feedback rejection and replacement delivery tests | Planned |
