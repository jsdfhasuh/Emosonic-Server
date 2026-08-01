# Strict-v2 r19 契约闭合与实施计划

> 日期：2026-08-01
> 目标分支：`agent/strict-v2-r5-server-adaptation`
> 当前权威基线：strict-v2 `2.8.0` / r18
> 本文性质：实施计划与决策记录，不替代 `specs/emosonic_strict_v2_socketio_server_contract.md` 及其分卷。

## 1. 范围与固定约束

本轮先修改权威契约，再修改服务端、Flutter、schema validator、fixtures 与测试。

以下内容明确不进入本轮：

- 不设计或修改协议版本协商。
- 不修改调试服务中 profile implementation readiness 默认 `true` 的便利行为。
- 不以 conformance manifest 的发布状态阻止本地 debug 路径运行。
- 不新增 legacy/session 兼容 shape。
- 不在本轮加入 Broadcast 动态 participant membership、repeat、shuffle 或 queue 中重复 songId。

本轮的核心目标是让以下五层使用同一套定义：

```text
权威 specs
  -> 服务端 strict_v2_contract.py
  -> 服务端 ws.py / ws_store.py / broadcast_store.py
  -> Flutter action policy / models / controllers
  -> 双端 fixtures、自动测试与真机验收
```

## 2. 已锁定产品与协议决策

### D1 — Core prepare

选择 A。`playback.context.prepare` 是 Core 行为，不依赖 Handoff profile，也不依赖 negotiated `playbackPrepare:true`。

`playbackPrepare` 只表示设备能够处理 Handoff target 的 server-routed `playback.prepare` 预加载。

### D2 — Follow

Follow 是 Context 驱动的一对一软同步音频跟随，不是只读观察。

- 每个 follower 同时只跟随一个 `sourcePlaybackContextId`。
- 一个 source Context 可以有多个 follower。
- follower 在本地捕获原任务，应用 source queue/state/position/rate，并做 drift 修正。
- follower 不取得 source Context 控制权。
- Follow 退出后恢复本机原任务。
- Follow 不建立 Broadcast revision、deliveryId、feedback deadline 或服务端 participant ledger。

### D3 — Remote control settlement

选择 A。正式纳入：

- server-routed control 的 `executionTimeoutMs`；
- server-only `playback.control.settled`；
- `dependency_failed`；
- `execution_unknown`。

播放器能够证明的 committed/failed 仍通过 `playback.update(origin:"remoteCommand")` 上报。

### D4 — Handoff source eligibility

选择 A。第一版只允许 fresh、settled、playing 的 source：

```text
active Context
non-empty queue
fresh actual DevicePlaybackState
state=playing
track/index match
appliedControlVersion == controlVersion
no pending control transaction
```

idle 返回 `queue_required`；paused/stopped/stale/unsettled 返回无副作用 `conflict`。

### D5 — Handoff continuity policy

选择 A。continuity-first：source 在 target 实际开始并 complete 前持续播放；target complete 后切 authority 并立即 release source。允许极短重叠，不承诺零重叠。

### D6 — Handoff complete proof

选择 A。`playback.handoff.complete` 必须携带 target 的完整实际播放事实，并成为 authority switch point。

### D7 — Broadcast restorePending

选择 A。`restorePending:true` 期间服务端冻结 suspended Context 的全部写操作，统一返回 `restore_in_progress`；Flutter 不再排队普通 Context command。

### D8 — Broadcast synchronization promise

选择 A。Broadcast 为软同步。`syncStatus:"applied"` 只表示 participant 已应用该 revision 的目标，不证明持续 drift 小于固定毫秒阈值。

### D9 — Close during Handoff

选择 A。存在非终态 Handoff 时 `playback.context.close` 返回 `conflict`；必须先 cancel，再 close。

### D10 — Queue boundary

- `queueSongIds` 不允许重复。
- 不支持 shuffle、repeat-one、repeat-all。
- 第一首 `prev`：重播第一首，position=0，state=playing。
- 最后一首 `next`：不循环，保留最后 index，position=0，state=stopped。
- 最后一首自然结束：保留最后 index，position=0，state=stopped。

### D11 — Recovery abandon

选择 A。增加管理端/调试 CLI 的 recovery abandon；不加入 Flutter 公共 strict realtime action。

### D12 — Follow disconnect/reconnect

选择 A。

- source facts 超过 3 秒不新鲜时，follower 暂停且停止位置外推。
- 同一应用进程内 Socket 重连后重新 `follow.start`、status 并继续。
- 30 秒仍无法恢复或 source Context closed 时退出 Follow，并恢复本机任务。
- 应用进程重启后不自动恢复 Follow。

### D13 — User controls while following

选择 A。Follow 模式禁用 play/pause/seek/next/prev 与 queue mutation；保留本机音量和“停止跟播”。

### D14 — Follow feedback

选择 A。strict Follow overlay 不发送普通 `playback.update`，也不新增 `follow.feedback`。Follow 镜像不得写 source Context 或 follower 原 Context。

### D15 — Mode coexistence

允许同一个 source Context 同时被 Follow 与 Broadcast 使用。

同一设备本地执行覆盖层互斥：

```text
Follow follower
Broadcast ordinary participant
Handoff target preparing/committing
```

source Context 的 Handoff 仍受 active Broadcast source fence 阻止；Follow 绑定 Context ID，source Handoff 完成后继续跟随新 authority。

### D16 — Broadcast membership

选择 A。r19 继续使用固定 membership；不增加 `broadcast.leave/add/removeParticipant`。

### D17 — Control settlement recipients

选择 A。`playback.control.settled` 发给全部当前 Context subscribers，并额外发给仍在线的原 authority 物理连接。非请求 controller 只更新全局 cursor/UI，不弹出本地操作错误。

## 3. 契约优先修改批次

## Batch A — 公共错误、幂等与 Core control settlement

### 修改文件

- `phase-0-foundation/03-ack-errors-idempotency-and-cursors.md`
- `phase-1-core/04-client-core-actions.md`
- `phase-1-core/07-server-device-context-and-status.md`
- `phase-1-core/08-server-queue-playback-and-controls.md`
- `phase-3-conformance/11a-common-and-core-requirements.md`
- `phase-3-conformance/13-integration-acceptance.md`

### 必须写入的规则

1. `playback.context.prepare` 不再检查 `playbackPrepare`；只检查 Core authority、player、canPlay、唯一 Context、binding 和 cursor。
2. server-routed `player.*` / `queue.playItem` 必需携带正整数 `executionTimeoutMs`。
3. 正式定义 server-only `playback.control.settled`：

```text
playbackContextId
 epoch
 commandControlVersion
 status:"failed"
 errorCode:"dependency_failed"|"execution_unknown"
 controlVersion
 appliedControlVersion
 requestingClientId
 serverUpdatedAtMs
 dependsOnControlVersion:C
 errorMessage:O
```

4. settlement 幂等键：

```text
(playbackContextId, epoch, commandControlVersion)
```

5. ACK 只证明 accepted/routed；Socket emit 成功不能证明 audio success。
6. authority disconnect、Socket replacement、restart、watchdog expiry 只能生成 `execution_unknown`，不得伪造 authority `playback.update`。
7. track-changing command failed 时，按 controlVersion 升序将仍 pending 的后续事务逐条结算为 `dependency_failed`。
8. 所有 base cursor mismatch 统一 `stale_version`。
9. Context/Handoff/Broadcast state conflict 统一带完整 canonical cursors。
10. 写入 D10 queue boundary 和 natural-end 行为。

### 契约验收条件

- request/ACK/event matrix 能唯一回答每个 control 的最终结果来源。
- `playback.control.settled`、`executionTimeoutMs` 在正文、字段表、allowlist、EARS 和验收项中同时出现。
- Core prepare 不再引用 Handoff capability。

## Batch B — Follow 音频语义闭合

### 修改文件

- `phase-2-optional-profiles/05-client-follow-and-handoff.md`
- `phase-1-core/07-server-device-context-and-status.md`
- `phase-1-core/08-server-queue-playback-and-controls.md`
- `phase-3-conformance/11a-common-and-core-requirements.md`
- `phase-3-conformance/11c-security-persistence-and-readiness.md`
- `phase-3-conformance/13-integration-acceptance.md`

### Wire shape

保持现有请求 shape：

```text
follow.start:
  sourcePlaybackContextId
  deviceSessionId

follow.stop:
  sourcePlaybackContextId
```

暂不新增 Follow server push 或 feedback action。

### 服务端职责

1. ownership 固定为 `(user, followerClientId, followerDeviceSessionId, sourcePlaybackContextId)`。
2. 相同 source 重试幂等；不同 source 返回 conflict，不隐式切换。
3. 自动订阅 source Context，但 start ACK 之后仍显式请求 status。
4. follower 不得控制 source Context。
5. source authority Handoff 后 relationship 保持，继续按同一 Context 跟随新 authority。
6. source Context close 时终止 relationship 并推 closed。
7. follower disconnect 时清除临时 relationship；同进程重连由客户端重新 start。
8. mode gate：Follow follower、Broadcast ordinary、Handoff target 互斥。
9. source Context 可同时存在 Follow followers 与 active Broadcast。

### Flutter 职责

1. start 前保存本机 queue/index/position/state/rate。
2. Follow overlay 不修改原 Context cursor、queue 或 durable snapshot。
3. 应用 canonical source queue/state/position/rate。
4. 3 秒 stale 时暂停并停止外推；新鲜状态到达后继续。
5. 30 秒恢复失败或 Context closed 时退出并恢复原任务。
6. Follow 时禁用 transport/queue 控制，只保留本机音量与 stop Follow。
7. drift correction 仅为本地执行；strict 路径不得发送普通 `playback.update` 描述 mirror。
8. app restart 不自动 Follow，以本机正常 Context 为准。

### 契约验收条件

- Follow 明确是实际音频执行，而不是观察订阅。
- Follow 与 Broadcast 的差异以“有无服务端 delivery/feedback ledger”清楚区分。
- authority Handoff 不终止 Context Follow。

## Batch C — Handoff 时间线与完整执行证明

### 修改文件

- `phase-2-optional-profiles/05-client-follow-and-handoff.md`
- `phase-2-optional-profiles/09-server-handoff.md`
- `phase-3-conformance/11a-common-and-core-requirements.md`
- `phase-3-conformance/11c-security-persistence-and-readiness.md`
- `phase-3-conformance/13-integration-acceptance.md`

### 请求 shape 修改

`playback.handoff.start` 增加必需：

```text
targetDeviceSessionId
```

`playback.handoff.complete` 改为完整闭合 shape：

```text
playbackContextId
handoffId
deviceSessionId
queueIndex
trackId
state:"playing"
positionMs
positionSampledAtServerMs
playbackRate
appliedControlVersion
clientSeq
```

### prepare push 修改

`playback.prepare` 增加：

```text
playbackRate
positionSampledAtServerMs
sourceEpoch
sourceVersion
sourceQueueRevision
sourceControlVersion
```

其余 queue/index/track/position/controlVersion 保留。

### commit push 修改

Handoff `player.play` 增加：

```text
serverTimeMs
playbackRate
```

并要求 `effectiveAtServerMs - serverTimeMs >= 250`。

### 状态机规则

1. start 前原子执行 D4 eligibility；失败不创建 handoffId/prepareId。
2. target 按 exact client/device pair 冻结；替换 Socket 或 deviceSession 后旧 ready/complete 无效。
3. ready 后、commit 前重新读取 source actual state。track、state、rate、sample、applied cursor 或 source cursors 改变时，终止为 `failed/errorCode:"source_changed"`。
4. continuity-first：source 在 complete 前继续播放。
5. complete 验证完整 actual fact，在一个事务中退休 target standby、写 target DevicePlaybackState、切 authority、推进 cursor、完成 Handoff。
6. completed 后旧 source 通过 release、completed status、Context status 或 binding invalidation 任一事实都必须停止旧 authority audio lease。
7. target 收到 failed/cancelled/timedOut 时必须取消 timer、失效 execution lease，若已起播则立即暂停，且不得迟到 complete。
8. 非终态 Handoff 存在时 close 返回 conflict。
9. duplicate start 始终重放首次 `status:"preparing"` ACK；当前进度另发 canonical status。
10. duplicate/late ready 只重放当前 canonical status；不得保持 request cache in-flight。
11. duplicate complete 重放 completed status + current Context status，不重复 switch/release。
12. prepare/commit 不能可靠加入目标发送路径时立即 failed，不静默只等 timeout。

### 标准 Handoff errorCode

保留：

```text
prepare_failed
prepare_timeout
commit_timeout
target_disconnected
source_disconnected
server_restart
```

新增：

```text
source_changed
restore_in_progress
commit_failed
```

`context_closed` 不再作为隐式 close side effect 产生；active Handoff 时 close 被拒绝。

## Batch D — Broadcast restore 与软同步语义

### 修改文件

- `phase-2-optional-profiles/06a-client-broadcast-actions.md`
- `phase-2-optional-profiles/06c-broadcast-feedback-and-recovery.md`
- `phase-2-optional-profiles/06d-flutter-broadcast-roles-and-terminal.md`
- `phase-2-optional-profiles/10-server-broadcast.md`
- `phase-3-conformance/11b-broadcast-requirements.md`
- `phase-3-conformance/11c-security-persistence-and-readiness.md`
- `phase-3-conformance/13-integration-acceptance.md`

### restorePending 规则替换

删除“服务端允许普通 command、Flutter 在恢复门控排队”的旧规则，改为：

```text
restorePending=true
  -> 所有 suspended Context 写 action 返回 restore_in_progress
  -> 不推进 cursor
  -> 不产生 command/push/binding mutation
  -> 读取/status/subscribe/terminal feedback 继续允许
```

受阻写 action 至少包括：

```text
playback.context.ensure/prepare/prepared/close
queue.context.sync
queue.playItem
player.*
playback.update
playback.handoff.*
playback.ready
broadcast.start
任何 authority/device binding mutation
```

错误必须携带 suspended Context 的：

```text
playbackContextId
currentVersion
currentQueueRevision
currentControlVersion
retryable:true
```

Flutter terminal 恢复完成后发送 terminal applied feedback，服务端原子清 fence；用户意图用新 requestId 重试。

### 软同步定义

正文明确：

- `applied` 表示 revision target 已应用。
- positionMs 是 participant 实际观测，仍只做范围验证。
- 不承诺持续 drift 小于固定阈值。
- 本轮不增加 `actualEffectiveAtServerMs`、`estimatedDriftMs` 或 sync SLA。

### 固定 membership

正文明确：

- start 后 participant membership 固定到 terminal。
- ordinary participant 没有 leave/remove/add action。
- 本轮不支持动态 membership。

### terminal delivery gate

- terminal stop/restore 未成功加入当前 pair 的发送路径时，该 Socket 不得先收到 suspended Context 的普通 write command。
- enqueue 失败时断开当前 Socket；下次注册先 terminal replay，再开放普通业务。

### recovery abandon

定义为管理 surface，不加入 strict Socket action。事务要求：

```text
terminal Broadcast only
restorePending pair only
remove full/compact recovery obligation
remove ordinary fence
release recovery slot
persist abandoned audit tombstone
never replay terminal to that pair again
```

## Batch E — 权威入口、映射与验收收口

### 修改文件

- `specs/emosonic_strict_v2_socketio_server_contract.md`
- `docs/verification/emosonic_strict_v2_r18_requirement_mapping.md`（新一轮可重命名/复制为 r19）
- `phase-3-conformance/13-integration-acceptance.md`
- `phase-3-conformance/14-authority-and-deployment-evidence.md`

### 内容

1. 入口摘要增加 Follow 音频语义、control settlement、Handoff 完整 fact、restore freeze 与 queue boundary。
2. 每项新/修改 REQ 建立目标测试映射，不提前标 Verified。
3. 明确调试 capability 默认 true 不等于 production rollout，但不是实现缺陷。
4. 最终 readiness 证据继续要求 Android + Windows 双端真机。

## 4. 代码实施顺序（契约完成后）

## Phase 1 — Schema validator 与 fixtures

### 服务端

- `supysonic/emo/strict_v2_contract.py`

修改：

- Handoff start/complete request schema。
- Handoff prepare/commit output schema。
- 正式 control settlement output schema 与 routed timeout。
- restore_in_progress 条件字段和 action-aware 校验。
- queue boundary fixtures。

### Flutter

- `lib/services/emo_action_contract_policy.dart`
- `lib/services/emo_strict_v2_models.dart`
- `test/fixtures/emo_protocol/strict_v2/...`

必须先同步 parser/validator，再启用服务端新 push，避免闭合 schema 直接 quarantine。

## Phase 2 — Core control settlement

### 服务端

- `ws.py`
- `ws_store.py`

修改：

- Core prepare 去掉 Handoff capability 依赖。
- settlement 收件人、watchdog、dependency cascade、restart/disconnect unknown。
- queue boundary。
- stale/conflict error builder。

### Flutter

- `emo_control_transaction_coordinator.dart`
- `audio_execution_lease.dart`
- command lane / router

修改：

- 正式消费 `playback.control.settled`。
- 失效对应 execution lease。
- 非请求 subscriber 只更新状态，不弹本地错误。

## Phase 3 — Follow

### 服务端

- `ws.py`
- `ws_state.py`

修改：

- Context-owned relationship。
- authority switch continuation。
- mode occupancy checks。
- reconnect re-acquire support。

### Flutter

- `emo_follow_sync_controller.dart`
- `emo_realtime_client.dart`
- mode providers/router

修改：

- strict overlay 不 emit normal playback.update。
- 3 秒 stale / 30 秒 exit。
- app reconnect re-start；app restart不恢复。
- mode mutual exclusion。

## Phase 4 — Handoff

### 服务端

- `ws.py`
- `ws_store.py`

修改：

- exact target pair。
- source actual revalidation。
- full complete transaction。
- reliable prepare/commit settlement。
- close conflict。

### Flutter

- `emo_handoff_controller.dart`
- models/router/action policy

修改：

- new prepare/commit schema。
- full complete fact。
- completed fallback stop source。
- failed target execution lease invalidation。

## Phase 5 — Broadcast restore freeze 与管理清理

### 服务端

- `ws.py`
- `ws_store.py`
- `broadcast_store.py`
- 管理 CLI/API

修改：

- restorePending action-aware write freeze。
- terminal delivery gate。
- recovery abandon transaction。

### Flutter

- `emo_broadcast_controller.dart`
- `emo_broadcast_recovery_repository.dart`

修改：

- 删除恢复期间普通 command 排队。
- restore_in_progress UI/retry。
- 保持 terminal feedback 完整性。

## 5. 测试计划

## Core

- Handoff profile off / playbackPrepare false 时 Core prepare 仍成功。
- accepted command 的 committed、failed、dependency_failed、execution_unknown 全部分支。
- disconnect/replacement/restart/watchdog 不永久 pending。
- settlement duplicate 不重复副作用。
- queue first-prev、last-next、natural-last-end。

## Follow

- 实际应用 source queue、play/pause/seek/natural transition。
- 小 drift 调速、大 drift seek。
- Follow overlay 不写任何正常 Context。
- 3 秒 stale pause，恢复后继续；30 秒退出。
- Socket 重连自动 re-start；app restart 不自动恢复。
- source Handoff 后继续 Follow。
- follower control disabled。
- Follow/Broadcast/Handoff-target 互斥。
- source 同时有 Follow 与 Broadcast 时两条投影互不污染。

## Handoff

- idle/paused/stopped/stale/unsettled source fail-fast。
- target exact deviceSession replacement。
- 1.5x rate 与 position projection。
- prepare 期间 source seek/track/state/cursor 变化 -> source_changed。
- full complete fact 后 status 立即含 target DevicePlaybackState。
- duplicate start/ready/complete。
- prepare/commit enqueue failure。
- old source release 丢失但 status/binding 仍停止音频。
- target terminal 后不迟到 complete。
- active Handoff close conflict。

## Broadcast

- restorePending 下所有写 action `restore_in_progress` 且零副作用。
- terminal feedback 清 gate 后新 requestId 成功。
- applied 不做精确 drift 判定。
- membership 固定。
- terminal delivery enqueue failure -> disconnect -> replay first。
- recovery abandon 清 fence/record/slot 并停止 replay。

## 6. 建议提交拆分

为避免一笔巨大修改难以审阅，建议按以下顺序提交：

1. `spec: close core control settlement`
2. `spec: define strict follow audio semantics`
3. `spec: harden handoff execution proof`
4. `spec: simplify broadcast restore gate`
5. `spec: align acceptance and requirement mapping`
6. `contract: align strict validators and fixtures`
7. `core: implement final control settlement contract`
8. `follow: align context audio following`
9. `handoff: implement complete execution proof`
10. `broadcast: enforce restore freeze and recovery abandon`

每个 spec commit 都必须先保证同一 action 在字段表、状态机、EARS 和 acceptance 中没有互相矛盾，再进入代码 commit。

## 7. 完成定义

本轮契约闭合完成必须同时满足：

- D1—D17 均已写入权威分卷，不只存在于本计划。
- 所有新增字段在 request、push、validator、fixtures 中一一对应。
- Follow、Broadcast、Handoff 三种音频模式的本地覆盖层与服务端职责不重叠。
- 所有 accepted control 有明确终态。
- Handoff authority switch 同时拥有完整 target actual fact。
- restorePending 不再依赖 Flutter 命令排队。
- queue 末端行为可由服务端和 Flutter 得出同一个结果。
- 自动测试通过后，仍完成 Android/Windows 双端真机验证。
