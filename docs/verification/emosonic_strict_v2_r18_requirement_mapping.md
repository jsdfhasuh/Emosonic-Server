# strict-v2 2.8.0 / 2026-08-01-r18 requirement mapping

> 契约状态：Contract Frozen
> 实现状态：Contract defined / implementation pending

本表覆盖权威契约 REQ-001—REQ-090。`Verified` 只允许在服务端 schema、服务端状态机、Flutter
parser/controller、自动化测试、Android+Windows 真机证据五个维度分别记录当前最终 r18 证据后使用。
pre-freeze `2.8.0` 的历史测试可以复用为实现线索，但不能单独证明冻结后的成组兼容或 profile ready。

| REQ | 服务端 schema | 服务端状态机 | Flutter parser/controller | 自动化测试 | Android+Windows 真机证据 | 冻结后状态 |
| --- | --- | --- | --- | --- | --- | --- |
| REQ-001—REQ-006 | pre-freeze validator/metadata 证据存在；按最终入口重跑 manifest/schema diff 待完成 | ACK、注册、provenance、Context routing 旧实现存在；成组兼容复验待完成 | pre-freeze envelope/router 存在；最终 r18 reconnect/route 复验待完成 | 历史 contract/metadata/Core tests 存在；最终 revision 重跑待完成 | 未提供最终 r18 Android+Windows 成组日志 | Contract defined / implementation pending |
| REQ-007—REQ-014 | 待对齐 four-cursor error、exact Handoff target、settlement/replay 与 closed allowlist | 待复验 user-scoped error order、幂等与 cursor/schema 闭合 | 待更新/复验 error parser、request settlement 与 exact-pair request shape | 旧测试不覆盖全部新增字段和 replay 分支 | 未提供 | Contract defined / implementation pending |
| REQ-015—REQ-022 | readiness/limit/persistence schema 需按最终 r18 复验 | profile fail-closed、restart、delivery order、event replay 待全量闭合 | optional profile gate、restart cleanup 与 push grouping 待复验 | 原 mapping 已为 In progress；最终 cases 未闭合 | 未提供 | Contract defined / implementation pending |
| REQ-023—REQ-031 | 待对齐 Context exact pair、volumeState、Core prepare、safe close/Handoff standby shape | discovery/invalidation、pair serialization、ensure/prepare/close 与 standby fence 待实现或复验 | Context discovery/controller、startup ensure、prepare 与 target UI gate 待对齐 | pre-freeze Core/Handoff tests 不覆盖最终 exact-pair/fence/close 语义 | 未提供 | Contract defined / implementation pending |
| REQ-032—REQ-038 | 待加入 executionTimeout/dependency、settlement exact pair 与 reconciliation schema | dependency admission/eligibility/cascade/watchdog、terminal-gap reconciliation 待实现 | Windows command lane/lease、localUser/supersede 与 reconciliation consumption 待对齐 | 新 dependency、unknown/cascade/reconciliation 矩阵待新增 | 未提供 | Contract defined / implementation pending |
| REQ-039 | routed Broadcast source command schema 需加入最终 dependency/timeout 规则 | source transaction 与 derived revision 对 final settlement 的耦合待复验 | source controller 对最终 routed shape 待对齐 | 历史 projection tests 不覆盖最终 dependency/eligibility | 未提供 | Contract defined / implementation pending |
| REQ-040 | feedback shape 基础已有；soft-sync 语义复验待完成 | applied/position 只能保存实际观测、不产生 drift SLA | parser/controller 不得把 applied 解读为持续 drift 证明 | soft-sync acceptance 待新增 | 未提供 | Contract defined / implementation pending |
| REQ-041 | active occupancy 与 terminal restore error shape 待区分 | active conflict、terminal action-aware restore fence 待实现 | ordinary overlay/fence error handling 待对齐 | 全 action barrier matrix 待新增 | 未提供 | Contract defined / implementation pending |
| REQ-042 | wire 基础沿用；restore gate result 需对齐 | 服务端冻结普通写，不能在恢复期放行 | 删除恢复期普通 command queue，negative cleanup 待实现 | playing/paused/stopped/idle 恢复与 no-queue cases 待更新 | 未提供 | Contract defined / implementation pending |
| REQ-043 | pre-freeze schema 证据可复用 | source exact-pair reconnect/waiting/resume 需按最终组合复验 | source lifecycle/controller 复验待完成 | 历史 tests 需在最终 revision 重跑 | 未提供 | Contract defined / implementation pending |
| REQ-044 | terminal deliveryId/outbox shape 基础已有 | enqueue-fail disconnect、注册先 replay 的 delivery gate 待实现 | terminal once gate 与 reconnect replay 待复验 | terminal enqueue failure/replay cases 待新增 | 未提供 | Contract defined / implementation pending |
| REQ-045—REQ-046 | rate/effective-at schema 历史证据可复用 | revision/rate eligibility 需与最终 dependency/soft-sync 组合复验 | rate/effective-at execution 复验待完成 | 历史 tests 需在最终 revision 重跑 | 未提供 | Contract defined / implementation pending |
| REQ-047 | restore_in_progress four-cursor 与 negative error shape 待对齐 | action-aware allow/block matrix 待实现 | 删除普通 queue；ready/prepared negative cleanup 待实现 | restore action matrix 待新增 | 未提供 | Contract defined / implementation pending |
| REQ-048—REQ-049 | fresh fact/participant state schema 基础已有 | fresh settled source 与 applied soft-sync outcome 组合复验待完成 | source/participant controller 复验待完成 | 历史 tests 不覆盖最终 soft-sync/readiness 组合 | 未提供 | Contract defined / implementation pending |
| REQ-050—REQ-051 | late-policy/intent tombstone schema 需最终复验 | effective-at late action 与 close cleanup 原 mapping 已未完成 | clock/intent retry controller 待最终复验 | 原 mapping 已为 In progress | 未提供 | Contract defined / implementation pending |
| REQ-052—REQ-053 | recovery/frozen pair schema 基础已有 | crash-safe entry保留；membership 现固定到 terminal | RecoveryRecord 与 fixed membership/resync 待对齐 | fixed-membership/no add-remove cases 待新增 | 未提供 | Contract defined / implementation pending |
| REQ-054—REQ-057 | pre-freeze schema 证据可复用 | natural transition/restart/idle/controller observation 需最终组合复验 | source/observer controller 复验待完成 | 历史 tests 需在最终 revision 重跑 | 未提供 | Contract defined / implementation pending |
| REQ-058—REQ-060 | clock/sample/deadline schema 需包含最终 Handoff gates | clock warm-up、Handoff proof 与 deadline 组合复验待完成 | current Socket clock gate与 deadline controller 待对齐 | Handoff 50/1000/1000 与 existing deadline tests 待组合 | 未提供 | Contract defined / implementation pending |
| REQ-061—REQ-062 | compact recovery/limit schema 基础已有 | cursor compare、full-to-compact 与永久资源策略待最终复验 | restore compare/controller 待复验 | 历史 compaction/bounds tests 需最终重跑 | 未提供 | Contract defined / implementation pending |
| REQ-063—REQ-067 | push/status/rejection schema 历史证据可复用 | deterministic action、immutable status、resync/state domains/rejection 待最终组合复验 | push parser/feedback recovery controller 待复验 | 历史 tests 需在最终 revision 重跑 | 未提供 | Contract defined / implementation pending |
| REQ-068—REQ-069 | 待实现 authorityDeviceSessionId snapshot 与 currentEpoch error allowlist/serializer | exact-pair snapshot 与 user-scoped fence error 待实现 | Context/error model与 fixtures 待更新 | schema/error acceptance 待新增 | 未提供 | Contract defined / implementation pending |
| REQ-070—REQ-075 | 待实现 routed dependency/timeout、settlement pair、close/tombstone schema | dependency/cascade/reconciliation/safe close/queue terminal 待实现 | Windows dependency lane、settlement与边界 controller 待实现 | acceptance 78—84 对应自动化待新增 | 未提供 | Contract defined / implementation pending |
| REQ-076—REQ-081 | 待实现 Follow composite capability、baseline ACK 与 SafetyLease persistence schema | Follow current fact、lease/fence、failure/reconnect/restart cleanup 待实现 | RecoveryRecord prewrite、baseline compare、mirror failure、cursor-safe stop 待实现 | acceptance 85—94 对应自动化待新增 | 未提供 | Contract defined / implementation pending |
| REQ-082—REQ-086 | 待实现 Handoff exact-pair prepare/commit/complete/cancel/status schema | full fence、provisional lane、proof、atomic switch、disconnect/replay 待实现 | target UI gate、independent lane、proof/clientSeq、immediate cleanup 待实现 | acceptance 95—103 对应自动化待新增 | 未提供 | Contract defined / implementation pending |
| REQ-087 | 待实现 restore_in_progress four-cursor 与 ready/prepared negative schema | action-aware restore gate 待实现 | no-queue 与 negative cleanup 待实现 | acceptance 104—105 待新增 | 未提供 | Contract defined / implementation pending |
| REQ-088 | wire 不新增动态 membership action；status语义需复验 | soft sync/fixed exact-pair membership 待实现或复验 | applied 语义与固定 membership controller 待对齐 | acceptance 107—108 待新增 | 未提供 | Contract defined / implementation pending |
| REQ-089 | terminal delivery shape 沿用 current deliveryId | enqueue-fail disconnect、register-first replay 待实现 | reconnect terminal-first handling 待复验 | acceptance 106 待新增 | 未提供 | Contract defined / implementation pending |
| REQ-090 | 无 Flutter realtime action；管理/persistence key 待实现 | atomic abandon、online disconnect/device removal、permanent decommission 待实现 | 只需拒绝不存在的 realtime action并处理断开 | acceptance 109—110 待新增 | 未提供 | Contract defined / implementation pending |

## Pre-freeze Verified 降级

旧表中标为 `Verified` 的 REQ-001—REQ-014、REQ-023—REQ-049、REQ-052—REQ-067 全部降级为
`Contract defined / implementation pending`。REQ-015—REQ-022、REQ-050—REQ-051 原本已经是
`In progress`，继续保持 pending；新增 REQ-068—REQ-090 也全部 pending。

降级原因有两层：其一，最终 r18 新增或改变了 exact-pair/four-cursor schema、dependency/settlement/
reconciliation、Follow SafetyLease、Handoff provisional proof、restore action matrix、soft-sync fixed
membership 和 decommission 等语义，旧测试不能覆盖；其二，旧表把实现与自动化合并为一个状态，未
分别记录 Flutter parser/controller 和 Android+Windows 最终 r18 真机证据。完成五个维度并记录可复现
证据前，不得把任何行重新标为 `Verified`。
