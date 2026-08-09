# Emosonic Strict-v2 r18 服务端 Sol Max 一次性收口总计划

> 面向执行模型：Sol Max
>
> 计划性质：冻结 r18 契约下的服务端剩余实现、验证与证据收口总 Goal
>
> 目标仓库：`jsdfhasuh/Emosonic-Server`
>
> 工作分支：`agent/strict-v2-r5-server-adaptation`
>
> 代码实现基线：`5ed3d8804397af0ebc122530bb843434df1d71df`
>
> 冻结契约：`2026-08-01-r18` / `protocolVersion 2.8.0`
>
> 关联 Draft PR：`#6`
>
> 本计划路径：`docs/plans/2026-08-09-strict-v2-r18-sol-max-one-shot-completion.md`

## 1. 总目标

从代码实现基线 `5ed3d8804397af0ebc122530bb843434df1d71df` 开始，在**一个连续 Sol Max 会话**中完成 Strict-v2 r18 服务端全部剩余实现，直到达到：

```text
server implementation candidate complete
```

本计划替代旧主计划中“每次 Luna 会话只执行一个 Phase”的执行方式，但不改变冻结契约、阶段依赖、事务不变量或验收标准。

“一次性”只表示：

- 不在每个阶段之间等待用户确认；
- 前一阶段本地与远端门禁绿色后，自动进入下一阶段；
- 由同一个 Sol Max 会话持续审计、实现、测试、提交、推送和检查 CI；
- 普通实现错误、测试失败和 CI 失败应在当前阶段内自行诊断、修复、重新验证，不应立即把工作退回给用户。

“一次性”不表示：

- 一个巨型提交；
- 跳过中间测试或 CI；
- 多个状态机在一个不可审查 diff 中混改；
- 红色状态下继续后续阶段；
- 自动合并 PR；
- 把尚未获得的客户端或真机证据伪装成已完成。

必须使用**一个连续 Goal、多个原子提交、多个独立 CI checkpoint**的方式完成。

## 2. 当前已核实基线

### 2.1 GitHub 状态

- 分支：`agent/strict-v2-r5-server-adaptation`
- 代码实现基线：`5ed3d8804397af0ebc122530bb843434df1d71df`
- PR：`#6`
- PR 状态：Draft、open、未合并
- 已核实 CI：run `31252756826`
- 该 run 绑定 `5ed3d8804397af0ebc122530bb843434df1d71df`
- Python Tests 3.9、Python Tests 3.12、Database Migrations、Docker Build 均为 success
- 基线完整测试：`Ran 1727, OK (skipped=3)`
- net suite：`Ran 5, OK (skipped=1)`
- coverage：83%
- `python -m build`：成功

本计划文档提交后，实际启动 HEAD 会高于 `5ed3d880...`。开始实施时必须验证：

- `5ed3d880...` 是当前 HEAD 的祖先；
- 从 `5ed3d880...` 到启动 HEAD 之间只有本计划等明确的文档提交，或由用户之后明确授权的提交；
- 不得 reset、rebase、force-push 或丢弃后来提交；
- 若 HEAD 已有新的实现提交，先逐项审计其真实完成范围，再从第一个未闭合阶段继续。

### 2.2 已完成并保留的实现

`5ed3d880...` 已完成普通 control 的第一轮 dispatch/feedback serialization：

- 按 `playbackContextId` 建立进程内 ordinary dispatch barrier；
- 覆盖 mutate、emit、execution eligibility、watchdog 与普通 feedback terminalization；
- ordinary `playback.update` 显式要求 transaction 已具备 `executionEligibleAtMs` 与 watchdog deadline；
- immediate committed/failed feedback 不被丢弃；
- eligibility failure 与 emit failure 尝试 `execution_unknown` 补偿并保留原始异常；
- clientSeq 在防御拒绝路径中零消耗；
- emit 不在 Context、authority-pair 或数据库 transaction 内执行。

这些成果不得回退或另写第二套路径。

### 2.3 当前第一个阻断问题

现有 Context dispatch barrier 没有覆盖 physical generation mapping 的修改：

- `device.register` 当前可能先调用 `state.register_client()` 切换 client→SID，再等待 old transaction settlement；
- `on_disconnect` 当前可能先调用 `state.unregister_session()` 删除 current generation，再等待 Context barrier；
- `prune_stale_clients` 也可能在 dispatch 中途移除 generation；
- `reserve_emit()` 只是发送缓冲计数，不是 generation lease。

因此仍存在：

```text
final generation recheck 已成功
→ replacement/disconnect 先修改 current mapping
→ command 仍发送到旧 SID
```

第一实施阶段必须先关闭此 P1 竞态。完成前不得开始后续 dependency、startup recovery 或最终 settlement producer。

### 2.4 当前明确仍未完成

至少包括：

- Phase 2C2-A-R2 physical-generation lifecycle linearization；
- Phase 2C2-B dependency 与 eligibility durable semantics；
- Phase 2C3 startup recovery；
- Phase 2C4 bounded retention；
- Phase 1B live serializer/runtime policy activation；
- Phase 3A safe close/four-cursor/exact-pair closure；
- Phase 3B 最终 ordinary control dependency、watchdog 与 `playback.control.settled`；
- Phase 3C reconciliation 与 queue terminal boundary；
- Phase 4 Follow 最终 r18 闭环；
- Phase 5 Handoff 独立 provisional lane；
- Phase 6 Broadcast restore/terminal/decommission 最终闭环；
- Phase 7 跨 profile、重启、readiness、mapping、fixture 和证据收口。

## 3. 权威来源

实施前必须完整阅读并以其为唯一产品语义来源：

1. `AGENTS.md`
2. 本计划
3. `docs/plans/2026-08-02-strict-v2-r18-server-implementation.md`
4. `docs/plans/2026-08-01-strict-v2-r18-contract-finalization.md`
5. `docs/verification/emosonic_strict_v2_r18_requirement_mapping.md`
6. `specs/emosonic_strict_v2_socketio_server_contract.md`
7. 入口引用的 `specs/emosonic_strict_v2_contract/**` 全部 19 个权威分卷

优先级：

```text
冻结契约
> finalization 决策
> 本计划的执行编排
> 旧实施计划
> 当前实现
> 旧测试、旧 fixture、旧 ADR、旧文档
```

若旧实现或测试与冻结契约冲突，修改实现、测试或派生材料；不得反向修改冻结契约来迁就旧代码。

## 4. 永久禁止事项

整个连续执行过程中始终禁止：

- 修改 `specs/**` 的冻结入口或 19 个权威分卷；
- 创建 r19、2.9.0 或协议版本协商；
- 修改 Flutter/Dart 客户端仓库；
- 修改 `master`；
- 合并 PR #6、标记 Ready、启用 auto-merge；
- force-push、rebase、amend、重写历史；
- 使用或删除以下保留对象：
  - stash `bc7c734546898f1c801fcfc7f32a7f175a7c4392`
  - tag `wip-phase1-full-7abed609`
  - checkpoint `12edfeb0d2adeb1a9bd31721fe17809729e54128`
- `git add .`、`git add -A`、`git commit -a`；
- 删除测试、加 skip/expectedFailure/xfail、减少 REQ 范围；
- 放宽 closed schema、`additionalProperties`、显式 null 或 unknown field 规则；
- 用 `None`、空串、默认 UUID、`unknown`、0、当前 Socket、当前 authority、latest session 或 pair-only fallback 补造 canonical 字段；
- 持久化 raw Socket SID；
- 用 clientId-only 路由替代 exact `clientId + deviceSessionId + nonce + epoch`；
- 在数据库 transaction、`ws_state._lock`、Context store lock、authority-pair lock 或 Broadcast mutation lock 内 emit；
- 在红色本地测试或红色远端 CI 下进入下一阶段；
- 修改 CI 命令、依赖版本或 coverage 配置来掩盖失败；
- 伪造 formal evidence、`schemaHash`、`serverBuildCommit`、真机证据或 `Verified` 状态；
- 连接、扫描或修改用户真实媒体库与生产数据库。

## 5. Sol Max 连续执行协议

### 5.1 启动检查

开始前执行并记录：

```bash
git branch --show-current
git rev-parse HEAD
git rev-parse origin/agent/strict-v2-r5-server-adaptation
git merge-base --is-ancestor 5ed3d8804397af0ebc122530bb843434df1d71df HEAD
git status --short
git diff --check
git log --oneline --decorate -20
```

要求：

- 当前分支精确匹配；
- 本地与远端 tracking 一致；
- working tree、index、untracked 为空；
- `5ed3d880...` 是祖先；
- PR #6 仍为 Draft。

若发现与本任务无关的用户改动，禁止覆盖，进入硬停止并报告。

若 HEAD 已前进且是已提交的本任务实现，不得 reset；必须通过 diff、测试和 CI 识别已经闭合的阶段，跳过时给出证据。

### 5.2 每个阶段的自动循环

每个阶段均执行：

```text
读取对应契约与当前实现
→ 建立最窄可复现基线
→ 实现当前阶段
→ 运行定向测试
→ 修复当前阶段内的失败并重复测试
→ 运行完整 unittest/net/coverage/build/diff-check
→ 精确 stage
→ 原子 commit
→ push 当前分支
→ 等待并核实绑定该 SHA 的四项 CI
→ CI 失败则在当前阶段继续修复并提交 follow-up
→ 四项绿色后自动进入下一阶段
```

普通测试失败、实现 bug、自己引入的回归或可在当前 Goal 内解决的跨文件依赖，不是向用户提问的理由。

### 5.3 只有以下情况允许硬停止

- working tree 存在无法安全区分的非本任务用户改动；
- 冻结权威分卷之间出现无法通过代码解释的真实语义矛盾；
- 必须做冻结契约未决定的重大产品选择；
- 必须执行 destructive git、真实生产数据库操作或账号整体删除；
- push/CI 权限真实缺失；
- GitHub 外部服务持续故障，无法获得绑定 SHA 的 CI 结果；
- 剩余验收只能由 Android/Windows 真机或用户人工操作完成。

硬停止时必须保留现场，不得 reset 或清理，并报告精确 blocker、命令、文件、测试和可继续位置。

## 6. 全局并发、锁与事务不变量

所有后续普通 control 与 profile 状态改变必须遵循固定顺序：

```text
排序后的 physical-generation lifecycle key
→ 排序后的 playbackContext dispatch barrier
→ 排序后的 Context IDs
→ 排序后的 exact device pairs / stable-client / authority-pair locks
→ profile resource key
→ DB transaction
→ commit
→ emit/enqueue
→ execution eligibility / watchdog（按契约需要）
→ ACK / canonical push
```

要求：

- physical-generation lifecycle key 至少按 `(userName, clientId)` 建立；同一 stable client 更换 `deviceSessionId` 时仍命中同一锁；
- requester 与 authority 同时受保护；相同 key 去重，多个 key 排序获取、逆序释放；
- `device.register` replacement、current disconnect、stale prune、ordinary control 和普通 `playback.update` 必须使用同一 lifecycle serialization；
- live lock 只用于进程内并发，不是 durable canonical source；
- durable source仍来自数据库 transaction；
- commit 前不得发出业务成功事实；
- emit 成功不是 DB commit 的证明；
- emit 失败不得回滚已提交 canonical cursor，只能按契约写 terminal/reconciliation；
- error path 除契约明确要求的 terminal/outcome 外零副作用；
- idempotent replay 不推进 cursor、不二次释放 fence、不二次退休 Context。

## 7. 执行图

严格按以下顺序连续执行：

```text
S0 基线重审计
→ S1 Phase 2C2-A-R2 physical-generation lifecycle
→ S2 Phase 2C2-B dependency durable semantics
→ S3 Phase 2C3 startup recovery
→ S4 Phase 2C4 bounded retention
→ S5 Phase 1B live serializer/runtime policy
→ S6 Phase 3A exact pair/four cursor/safe close
→ S7 Phase 3B final control dependency/watchdog/settled
→ S8 Phase 3C reconciliation/queue terminal
→ S9 Phase 4A/4B/4C Follow
→ S10 Phase 5A/5B/5C Handoff
→ S11 Phase 6A/6B/6C Broadcast
→ S12 Phase 7 final integration/evidence closure
```

不得跳过前置阶段。若后续阶段暴露前置实现缺陷，应回到相应层修复并重新跑当前阶段门禁，不得另写平行状态机。

## 8. S0：当前 HEAD 重审计

本阶段不修改实现，目标是把静态计划与实际 HEAD 对齐。

必须：

- 列出 `5ed3d880...` 之后的全部提交；
- 对每个提交记录修改文件、真实测试、CI 与完成范围；
- 检查本计划提交是否是纯文档；
- 验证当前完整 unittest/net/coverage/build 基线；
- 搜索 TODO、未激活 producer、旧 placeholder、`clientId`-only route、startup recovery、retention 和 final settled 路径；
- 形成内部执行清单，但无需为清单单独询问用户。

若基线测试意外红色，先判断是否由纯文档之外的新提交导致。可在本 Goal 范围修复的，纳入第一个相关阶段；不可安全归属的，硬停止。

## 9. S1：Phase 2C2-A-R2 physical-generation lifecycle

### 9.1 目标

关闭 final generation recheck 与 emit 之间的 replacement/disconnect/prune 竞态，使 ordinary control、普通 authority feedback 和 physical generation mapping 具有唯一线性顺序。

### 9.2 主要范围

- `supysonic/emo/ws.py`
- `supysonic/emo/ws_state.py`
- 必要时 `supysonic/emo/ws_store.py`
- `tests/base/test_emo_strict_v2_core.py`
- `tests/base/test_emo_ws_state.py`
- `tests/base/test_emo_ws_store.py`

不得修改 Broadcast domain semantics、schema/migration、冻结契约或最终 settled wire shape。

### 9.3 必须实现

- 进程内可重入、确定性排序的 stable-client generation lifecycle lock set；
- ordinary control 同时取得 requester 与 authority stable key；
- 在 lifecycle lock 内重新解析 exact current physical generation；
- `device.register` 在 mapping swap 前取得同一锁；
- `on_disconnect` 在 unregister current generation 前取得同一锁；
- stale prune 与同一锁协调；
- 普通非 Broadcast `playback.update` 使用 generation lifecycle lock → Context barrier → store apply；
- replacement 已淘汰的旧 SID disconnect 只能清理旧 session，不得移除新 mapping或结算新 generation transaction；
- actual Socket disconnect 可在释放 lifecycle lock 后执行，避免同步回调重入死锁；
- 不向 replacement SID 重试旧 command；
- `_settle_control_transactions_unknown()` 在取得 Context barrier 后再计算每项 terminal time；
- store 防御 `terminalAtMs >= executionEligibleAtMs`。

### 9.4 必测线性结果

1. replacement-first：control 零 mutation、零 emit、零 ACK，返回 authority/requester generation error；
2. control-first：旧 exact SID 只收一次 command，完成 eligibility/watchdog/ACK 后 replacement 才切 mapping；
3. final recheck 后、emit 前启动 replacement：只能得到上述两种结果之一；
4. authority disconnect-first/control-first；
5. requester replacement-first/control-first；
6. stale prune 不在 active dispatch 中途移除 generation；
7. `terminalAtMs >= executionEligibleAtMs`；
8. immediate committed/failed feedback、waiting feedback、eligibility failure 继续通过；
9. 不同 client/不同 Context 可并行，无全局无关串行和死锁；
10. emit 时不得持有 `ws_state._lock`、DB/store/authority-pair/Broadcast lock。

确定性测试使用 `threading.Event`、Barrier 或等价同步，禁止依赖 sleep 猜竞态。

建议提交：

```text
fix: linearize r18 physical generation lifecycle
```

## 10. S2：Phase 2C2-B dependency durable semantics

### 10.1 边界

本阶段完成 ordinary control dependency 的**持久化基础和 store API**，不提前激活最终 `playback.control.settled` producer。

### 10.2 必须实现

- deterministic direct dependency admission；
- 只有 `queue.playItem`、`player.next`、`player.prev` 属于 track-changing dependency boundary；
- 持久化 `dependsOnControlVersion`；
- dependent transaction 在 dependency terminal 前不具备 eligibility；
- dependency committed 后原子建立 `executionEligibleAtMs` 与 watchdog deadline；
- dependency failed/unknown 时，直接 dependent 为 `dependency_failed`，递归 cascade 保存每项 direct dependency；
- 等待 dependency 的时间不消耗 execution timeout；
- duplicate transition 幂等；
- concurrent dependency terminal、feedback、disconnect、watchdog 只产生唯一 terminal；
- store API 在 restart 后可恢复完整 dependency graph；
- 不使用内存 queue 作为 durable source；
- 不发最终 settled wire output。

### 10.3 测试

- A→B→C success、failed、unknown、recursive dependency_failed；
- dependency 等待超过 execution timeout 但未 eligible 不超时；
- duplicate commit/failure/cascade；
- restart round-trip；
- DB rollback 无部分 eligibility/cascade；
- dependency 与 ordinary feedback、disconnect、watchdog 并发；
- Broadcast、legacy/non-strict 回归。

建议提交：

```text
feat: persist r18 control dependency eligibility
```

## 11. S3：Phase 2C3 startup recovery

### 11.1 目标

在任何新 admission、dispatch、watchdog、profile recovery、outbox drain 或 readiness 暴露前完成 restart-safe Core recovery。

### 11.2 必须实现

- 单进程启动 recovery gate；
- recovery 在 Socket.IO business admission 前完成；
- 完整 physical generation 缺失的旧 pending 根 transaction → `execution_unknown`；
- 其依赖链递归 → `dependency_failed`；
- 已 terminal 行原样保留；
- 旧 pending 不重新 dispatch、不新建 eligibility、不挂新 watchdog、不推进 Context cursor；
- recovery marker/outcome durable 且重复启动幂等；
- startup recovery 失败时 Core/readiness fail closed；
- 不从 current client、最新 session、DeviceState 或唯一在线设备推断旧 identity；
- 无 requester exact pair 的 legacy terminal 不生成非法 settled wire output；
- startup recovery 与 Broadcast existing restart recovery 有明确顺序，不互相绕过 safety gate。

### 11.3 测试

- 各种旧 pending 根/依赖链；
- 部分 identity 缺失；
- 已 terminal/已 recovered；
- 连续启动两次、并发启动；
- recovery transaction rollback；
- recovery 未完成时 admission/readiness 被拒绝；
- 初始化顺序与 watchdog/outbox/profile recovery；
- SQLite/MySQL/PostgreSQL migration 后 recovery 语义一致。

建议提交：

```text
feat: recover r18 pending controls at startup
```

## 12. S4：Phase 2C4 bounded retention

### 12.1 必须实现

- 为 Core transaction、reconciliation、close outcome、startup marker 建立有界保留和必要索引；
- 只删除已经无 replay、安全、dependency、fence、outbox 或审计含义的记录；
- pending、仍被 dependency 引用、cleanupRequired lease、restorePending terminal、永久 decommission tombstone不得被普通 TTL 删除；
- cleanup 幂等、批量有界、可中断重试；
- cleanup 失败不阻断现有 terminal 的安全语义；
- 不发明冻结契约未规定的 permanent tombstone TTL；
- 三数据库行为一致；
- 新安装 base schema 与 forward migration 结果等价。

### 12.2 测试

- 时间边界前后；
- 被引用 terminal 与未引用 terminal；
- pending/cleanupRequired/restorePending/permanent tombstone 保留；
- 批量上限和多轮清理；
- restart 与 cleanup 并发；
- rollback、重复执行；
- 三后端索引与查询计划的功能性验证。

建议提交：

```text
feat: bound r18 control retention safely
```

## 13. S5：Phase 1B live serializer 与 runtime policy

只激活已经具有 canonical source 的共享 live behavior：

- active Context 的 exact `authorityClientId + authorityDeviceSessionId`；
- live Context four-cursor error enrichment；
- recipient-aware negotiated capability trimming；
- `remoteVolumeControl` recipient 才能看到 `device.list.volumeState`；
- Core prepare 不依赖 Handoff profile capability，但仍要求自身完整 current physical、role、clock、freshness/rate gate；
- Phase 2 已持久化且 producer 在本阶段可完整闭合的共享 serializer。

不得提前激活：

- closed/tombstone error replay；
- final `playback.control.settled`；
- Follow frozen ACK；
- Handoff full proof/status/release；
- Broadcast restore/terminal/decommission output。

每个 active output 必须同时具备 canonical source、producer、recipient routing、closed validator 和对称测试。

建议提交：

```text
feat: activate strict-v2 live serializers
```

## 14. S6：Phase 3A exact pair、four cursor 与 safe close

必须完成：

- exact authority pair snapshot/routing；
- user-scoped live/closed error lookup；
- `playback.context.close` 的 expected epoch/base version admission；
- pending control、Follow/Handoff/Broadcast fence 冲突；
- durable close tombstone：user scope、Context ID、action domain、request fingerprint、expected cursor、closedFrom*、final four cursor、ACK/error outcome；
- 原子区分 live、exact tombstone、mismatched tombstone、neither、live+tombstone invariant breach；
- exact fingerprint/cursor duplicate replay 零写入；
- foreign user 不暴露存在性或 cursor；
- close tombstone 与永久 device decommission tombstone分离；
- restart replay 与 bounded retention 配合；
- 不伪造信息不足旧 row 的 replayable success。

建议提交：

```text
feat: enforce r18 context identity and safe close
```

## 15. S7：Phase 3B final control、watchdog 与 settled

本阶段在 S2 durable dependency、S3 recovery、S4 retention 基础上激活最终 runtime producer。

必须完成：

- control admission 的 deterministic dependency；
- dependent hold 与 dependency terminal 后 eligibility；
- watchdog deadline = eligibility + execution timeout + 2000；
- effective-at late policy；
- disconnect/replacement/restart/watchdog unknown；
- recursive dependency_failed；
- ordinary remote committed/failed 仍通过 `playback.update`；
- `playback.control.settled` 只用于 wire `status:"failed"` 且 errorCode 仅 `execution_unknown|dependency_failed`；
- settled payload 使用 durable `requestingClientId + requestingDeviceSessionId`；
- requester nonce/epoch、routed nonce/epoch 仅用于服务端 recipient matching，不进入 payload；
- recipients 独立来自：
  1. 当前 Context subscribers；
  2. 完整匹配原 requester physical generation；
  3. 完整匹配原 authority routed physical generation；
- 最终按 SID 去重；
- replacement Socket 不因相同 client/device 自动补收旧 settlement；
- emit/enqueue failure 保留 durable terminal 并可由合法路径 replay；
- startup recovered legacy row 若缺 wire 必需 identity，不发送非法 settled。

建议提交：

```text
feat: implement r18 control settlement
```

## 16. S8：Phase 3C reconciliation 与 queue terminal

必须完成：

- failed/unknown actual 与 canonical target gap 使用新 reconciliation record R；
- 不改写旧 terminal；
- 原子更新 canonical Context 与 Device actual；
- reconciliation 不进入普通 AudioExecutionLease；
- inline failed reconciliation 和后续 passive reconciliation；
- localUser supersede 与 authority change 并发；
- first-prev 重播第一首，不改变 index/track/queueRevision，推进一次 version/control；
- last-next 进入 stopped，保留最后 index、position 0，不循环；
- natural terminal 只推进一次 Context version，不推进 queue/control cursor；
- single-item queue 与重复自然结束 callback 幂等；
- 不引入 repeat/shuffle。

建议提交：

```text
feat: reconcile r18 terminal control gaps
```

## 17. S9：Phase 4 Follow 服务端闭环

必须按 4A→4B→4C，各自独立 commit、完整门禁和 CI，但无需等待用户确认。

### 17.1 4A Lease、fence、限制与 restart reload

- durable `FollowSafetyLease`；
- source/follower/suspended exact pair 与 physical generation；
- immutable frozen suspended baseline；
- phase/deadline/fence、start/stop/cleanup fingerprint；
- capacity limit 与 cleanup 例外；
- restart reload；
- Follow mirror 不写 source/suspended Context；
- `FollowRecoveryRecord` 仍是 Flutter 本地对象，服务端不得冒充。

提交：

```text
feat: persist r18 follow safety leases
```

### 17.2 4B Start preflight、source fact 与 frozen ACK

- composite capability；
- source current physical fact、clock、freshness、rate；
- lease、fence、frozen baseline 同一锁序与 DB transaction；
- commit 后才 ACK/push；
- ACK 所有 baseline 字段来自同一冻结快照；
- duplicate replay 只读 durable outcome；
- source 可以同时是 Broadcast source，但 follower overlay 与 Broadcast ordinary/Handoff target 互斥。

提交：

```text
feat: enforce r18 follow start baseline
```

### 17.3 4C Idle、disconnect、reconnect、cleanup 与 replay

- source idle 保留 relationship/fence；
- source recovery window；
- follower disconnect、30 秒 grace、reconnect；
- cleanupRequired 不按普通 TTL 删除；
- 未安全恢复不得提前释放 suspended fence；
- stop/cleanup 幂等与上限例外；
- acquiring/active/stopPending/cleanupRequired 各阶段 restart；
- 不新增 `follow.feedback`；
- follower mirror 不使用 ordinary `playback.update` 回写。

提交：

```text
feat: implement r18 follow recovery cleanup
```

## 18. S10：Phase 5 Handoff 独立 provisional lane

必须按 5A→5B→5C，各自独立 commit、门禁和 CI。

### 18.1 5A Exact target pair 与全生命周期 fence

- exact target `clientId + deviceSessionId + nonce + epoch`；
- source/target/standby 从 start 到 terminal 全程 fenced；
- start/cancel/restart/disconnect durable outcome；
- 禁止 pair-only、raw SID、latest session fallback。

提交：

```text
feat: fence r18 handoff exact pairs
```

### 18.2 5B 独立 provisional N+1 lane

- `(playbackContextId, epoch, handoffId)` 独立 provisional lane；
- 不创建普通 control transaction；
- 不参加 dependency/watchdog/reconciliation；
- source complete 前继续播放；
- failed/cancel/restart 后 canonical 仍为 N，普通 transaction 可使用 N+1；
- prepare/commit enqueue failure fail closed。

提交：

```text
feat: add isolated r18 handoff provisional lane
```

### 18.3 5C Proof、原子 complete、disconnect 与 replay

- full actual proof、future/sample-age/late/position-error 边界；
- complete.clientSeq 消耗 target 普通 playback.update 序列空间；
- 同一 DB transaction 写 target DeviceState proof、切 exact authority、推进契约 cursor、写 durable outcome、终结 fence；
- complete 是唯一 authority switch；
- duplicate replay 不二次切 authority、退休 standby、推进 cursor或释放 fence；
- source actual 改变按 `source_changed` 处理；
- commit/emit/ACK-loss/restart/disconnect 测试。

提交：

```text
feat: complete r18 handoff atomically
```

## 19. S11：Phase 6 Broadcast final-r18 闭环

必须按 6A→6B→6C，各自独立 commit、门禁和 CI。必须重新验证 pre-freeze acceptance，不得只实现新增 REQ-087—090。

### 19.1 6A Core 集成与 action-aware restore matrix

- active/waiting occupancy conflict；
- terminal restorePending action allow/deny 矩阵；
- `restore_in_progress` four cursor output；
- matching negative ready/prepared cleanup 不初始化播放、不推进 cursor、不 commit、不清 gate；
- final Core exact identity/capability/lock order 集成。

提交：

```text
feat: integrate r18 broadcast restore gates
```

### 19.2 6B Negative cleanup 与 terminal delivery gate

- terminal replay 先于 ordinary Context business delivery；
- critical terminal enqueue failure 立即断开，不能继续普通业务；
- capability 关闭不阻断已有 terminal drain；
- full-to-compact recovery、deliveryId、outbox replay；
- 通用会吞 emit 错误的 post-commit helper 不得用于 critical terminal delivery。

提交：

```text
feat: enforce r18 broadcast terminal delivery
```

### 19.3 6C Fixed membership、soft sync、abandon 与 decommission

- fixed exact-pair membership，不动态增删；
- soft-sync applied 不是持续 drift SLA；
- recovery abandon 与 permanent exact-pair decommission 同一 transaction；
- durable recovery reference、fingerprint/outcome、永久 `(user, clientId, deviceSessionId)` tombstone；
- presence/SID 创建前拒绝 decommission pair；
- 在线旧 Socket 同步断开；
- 普通重连不能复活；
- permanent tombstone 不受普通 TTL 或 capacity cleanup；
- 只能随账号整体删除；
- rollback 保持原状态。

提交：

```text
feat: decommission r18 broadcast pairs atomically
```

## 20. S12：Phase 7 最终集成、readiness 与证据收口

### 20.1 跨 profile 与重启矩阵

必须验证：

- 单 realtime worker；
- graceful shutdown；
- pending control restart → unknown/cascade；
- Follow 各 phase restart → reload/cleanupRequired；
- Handoff 各 phase restart → failed/cleanup；
- Broadcast active/waiting restart → terminal stopped；
- permanent decommission restart reload；
- Core/Follow/Handoff/Broadcast 占用矩阵；
- production fail-closed；
- explicit dev/test 便利开关仍可用；
- capability 关闭不阻断 terminal drain；
- bounded storage 与永久安全记录保留；
- clientId 相同但 deviceSession/nonce/epoch 不同的全路径隔离。

### 20.2 Schema、manifest、mapping 与 evidence

最终才允许：

- 激活所有已闭合 output validator；
- 更新 fixture manifest 与真实测试方法；
- 扩展 EARS verifier 到 REQ-001—REQ-090；
- 更新 requirement mapping 的服务端 parser/state/persistence/test 列；
- 更新派生文档中的真实服务端完成状态。

必须保持 pending：

- Flutter parser/controller；
- Android + Windows 双端真机；
- 真实跨端联调；
- 任何未实际运行的客户端/设备证据。

不得把总体状态标为 `Verified`。最终只能报告：

```text
server implementation candidate complete
client/device acceptance pending
```

建议最终实现提交：

```text
feat: complete strict-v2 r18 server candidate
```

若证据与实现需要分开，允许额外原子提交：

```text
docs: record strict-v2 r18 server candidate evidence
```

## 21. 每阶段统一本地门禁

先运行最窄定向测试，再运行：

```bash
python -m unittest
python -m unittest tests.net.suite
coverage erase
coverage run -m unittest
coverage run -a -m unittest tests.net.suite
coverage report -m
python -m build
git diff --check
git status --short
git diff --name-only
git diff --stat
```

要求：

- 完整 unittest 测试数不得低于阶段起始基线；
- 全程不得新增 skip，最终 skipped 不超过 3；
- net suite 保持 `Ran 5, OK (skipped=1)`，除非新增真实 net test 导致数量增加；
- coverage 不低于 83%；
- build 成功；
- diff-check 成功；
- 无无关文件、无 untracked 遗留。

涉及 schema/migration 的阶段还必须运行仓库现有 migration 测试和三后端验证，不得只依赖 SQLite。

不得自行添加 Ruff、Black、Mypy 等仓库未定义的门禁。

## 22. Git 与远端 CI 协议

每个子阶段：

1. `git diff --name-only` 核对范围；
2. 只精确 `git add <files...>`；
3. 创建原子提交；
4. `git push origin agent/strict-v2-r5-server-adaptation`；
5. 核实 origin tracking 与本地 HEAD 一致；
6. 获取绑定该 SHA 的 GitHub Actions run；
7. 等待并记录：
   - Python Tests 3.9
   - Python Tests 3.12
   - Database Migrations
   - Docker Build
8. 四项全绿后进入下一阶段。

若远端失败：

- 检查失败 job 与原始日志；
- 若属于当前实现，继续修复并创建 follow-up commit；
- 重新运行全部本地门禁并推送；
- 只以新 SHA 的四项结果为准；
- 不删除失败历史、不 amend、不 force-push。

PR #6 始终保持 Draft，不合并。

## 23. 文件范围原则

本计划列出的文件是主要范围，不是允许通过复制逻辑绕过架构的借口。

允许在以下条件下扩展文件：

- 冻结契约明确要求；
- 当前 canonical source 位于相邻模块；
- 不扩展会造成重复状态机或破坏原子性；
- 变更与当前阶段直接相关；
- 最终报告说明扩展原因。

禁止无关重构、全仓格式化、批量改名、依赖整理或 UI 改造。

新 store/module 只有在现有 `ws_store.py`/`broadcast_store.py` 无法保持清晰 domain boundary 时允许创建；不得为了绕过现有 CAS/lock 路径另写第二套 canonical store。

## 24. 最终完成标准

只有全部满足，才可宣布 Goal 完成：

- S1—S12 全部完成；
- 每个子阶段有独立可审查提交；
- 每个提交或最终 follow-up 均有绑定 SHA 的四项绿色 CI；
- 最终完整 unittest/net/coverage/build/migration 全绿；
- 测试数不低于基线且新增测试真实覆盖新语义；
- coverage ≥ 83%；
- 三数据库新安装与 forward migration 等价；
- startup recovery 在 admission/readiness 前完成；
- bounded retention 不删除安全记录；
- exact physical generation 全路径闭合；
- final settled、Follow、Handoff、Broadcast producer 与 validator 同阶段闭合；
- requirement mapping 只记录真实服务端证据；
- 客户端和真机状态仍如实 pending；
- working tree clean；
- 本地 HEAD = origin branch HEAD；
- PR #6 仍为 Draft、未合并。

## 25. 最终报告格式

```text
GOAL = COMPLETED / BLOCKED

分支：
代码起始基线：5ed3d880...
实际启动 HEAD：
最终 HEAD：
PR #6：Draft / open / unmerged

阶段结果：
S1 Phase 2C2-A-R2：
S2 Phase 2C2-B：
S3 Phase 2C3：
S4 Phase 2C4：
S5 Phase 1B：
S6 Phase 3A：
S7 Phase 3B：
S8 Phase 3C：
S9 Follow 4A/4B/4C：
S10 Handoff 5A/5B/5C：
S11 Broadcast 6A/6B/6C：
S12 Phase 7：

提交清单：
- SHA / message / stage

最终验证：
- targeted：
- python -m unittest：
- net suite：
- coverage：
- python -m build：
- migration SQLite：
- migration MySQL：
- migration PostgreSQL：
- git diff --check：
- working tree：

远端 CI：
- run URL / SHA
- Python 3.9：
- Python 3.12：
- Database Migrations：
- Docker Build：

关键不变量证明：
- physical generation lifecycle：
- dependency eligibility：
- startup recovery：
- bounded retention：
- safe close：
- settlement recipients：
- Follow lease/fence：
- Handoff provisional lane：
- Broadcast terminal/decommission：

已完成服务端 REQ：
仍 pending 客户端/真机 REQ：
未执行或未验证事项：
允许进入客户端对齐：yes / no
```

若 `BLOCKED`，必须额外报告：

- 最后一个绿色阶段/提交/CI；
- 当前未提交改动；
- blocker 的精确测试、堆栈、文件和契约条款；
- 下一次从哪个 stage/commit 恢复；
- 不得把部分完成描述为全部完成。

## 26. 给 Sol Max 的最短启动提示词

```text
在 jsdfhasuh/Emosonic-Server 的
agent/strict-v2-r5-server-adaptation 分支上，完整执行：

docs/plans/2026-08-09-strict-v2-r18-sol-max-one-shot-completion.md

从当前实际 HEAD 开始，先验证 5ed3d880... 是祖先并重审计后续提交。
在一个连续会话中按 S0→S12 完成全部剩余服务端工作；每个子阶段独立提交、推送并等待绑定 SHA 的四项 CI 全绿后自动继续，不要在阶段间等待我的确认。

保留 PR #6 Draft；禁止修改 specs/**、客户端仓库、master，禁止 amend/rebase/force-push/自动合并，禁止使用保留 stash/tag/checkpoint。
普通测试或 CI 失败应在当前阶段自行修复并继续；只有计划定义的硬 blocker 才停止并按最终格式报告。
```
