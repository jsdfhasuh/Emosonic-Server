# Strict-v2 r18 契约闭合、定稿与冻结计划

> 日期：2026-08-01  
> 目标分支：`agent/strict-v2-r5-server-adaptation`  
> 当前权威基线：strict-v2 `2.8.0` / r18  
> 最终目标身份：`2026-08-01-r18` / `2.8.0`  
> 本文性质：最终实施计划与决策记录，不替代 `specs/emosonic_strict_v2_socketio_server_contract.md` 及其 19 个权威分卷。

## 1. 版本身份、冻结状态与工作边界

本轮不是创建 r19，也不升级 `protocolVersion`。当前 strict-v2 `2.8.0/r18` 尚未正式发布，本轮属于 r18 首次冻结前的最终闭合、定稿和一致性修正。

全部权威分卷完成修改并通过机械一致性审计后，入口必须明确区分：

```text
文档状态：Approved r18 authoritative contract
契约冻结状态：Frozen
实现状态：Contract defined / implementation pending
文档修订：2026-08-01-r18
协议版本：2.8.0
```

不得使用 `Frozen implementation baseline`。

本次保留 `2.8.0` 是首次冻结前的单次闭合例外。权威入口必须写明：

- 冻结前构建的旧 `2.8.0` 调试服务端与 Flutter，不保证兼容最终冻结后的 `2.8.0`；
- 服务端与 Flutter 必须按最终 r18 成组升级，不支持新旧 pre-freeze `2.8.0` 混跑；
- r18 冻结后，服务端或 Flutter 与 r18 不一致时，默认修改实现适配 r18；
- 纯错字、链接、示例或不改变行为的文字澄清使用 r18 errata；
- 冻结后新增 action、字段、状态、错误码、持久化义务或改变客户端行为，必须进入 r19，并重新评估 `protocolVersion`；
- 冻结后不得继续静默改变 `2.8.0` wire shape。

本轮明确不做：

- 不设计或修改协议版本协商；
- 不修改调试环境中 profile implementation readiness 默认 `true` 的便利行为；
- 不以 conformance manifest 的发布状态阻止本地 debug 路径运行；
- 不新增 legacy/session 兼容 shape；
- 不增加 Broadcast 动态 membership、`broadcast.leave`、add/remove participant；
- 不支持 shuffle、repeat-one、repeat-all 或重复 `queueSongIds`；
- 不增加 `follow.feedback` 或新的 Follow mirror action；
- 不增加 control transaction 查询 action；
- 不加入严格持续 drift SLA、paused/stopped source Handoff、live-stream 完整模型或新的 buffering/loading/error 枚举。

契约阶段只改 Markdown 权威契约、验证映射和计划引用；禁止修改 Python、Dart、JSON fixtures 和测试。

## 2. 已锁定的 D1—D17

### D1 — Core prepare

选择 A。`playback.context.prepare` 是 Core 行为，不依赖 Handoff profile，也不依赖 negotiated `playbackPrepare:true`。`playbackPrepare` 只表示设备能够处理 Handoff target 的 server-routed `playback.prepare`。

### D2 — Follow

Follow 是 Context 驱动的一对一软同步音频跟随，不是只读观察：

- 每个 follower 同时只跟随一个 `sourcePlaybackContextId`；
- 一个 source Context 可以有多个 follower；
- follower 保存本机原任务，应用 source queue/state/position/rate，并在本地修正 drift；
- follower 不取得 source Context 控制权；
- Follow 退出后恢复本机原任务；
- Follow 不建立 Broadcast revision、deliveryId、feedback deadline 或服务端 participant ledger。

### D3 — Remote control settlement

选择 A。正式纳入：

- routed control 的 `executionTimeoutMs`；
- server-only `playback.control.settled`；
- `dependency_failed`；
- `execution_unknown`。

播放器能够证明的 committed/failed 仍通过 `playback.update(origin:"remoteCommand")` 上报。

### D4 — Handoff source eligibility

选择 A。第一版只允许 fresh、settled、playing source：

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

选择 A。`playback.handoff.complete` 携带 target 完整实际播放事实，并成为唯一 authority switch point。

### D7 — Broadcast restorePending

选择 A。`restorePending:true` 期间服务端冻结 suspended Context 写操作，统一返回 `restore_in_progress`；Flutter 不再排队普通 Context command。清理型 negative confirmation 与 cancel 依 action-aware 例外继续允许。

### D8 — Broadcast synchronization promise

选择 A。Broadcast 为软同步。`syncStatus:"applied"` 只表示 participant 已应用 revision target，不证明持续 drift 小于固定毫秒阈值。

### D9 — Close during Handoff

选择 A。存在非终态 Handoff 时 `playback.context.close` 返回 `conflict`；必须先 cancel，再 close。

### D10 — Queue boundary

- `queueSongIds` 不允许重复；
- 不支持 shuffle、repeat-one、repeat-all；
- 第一首 `prev`：重播第一首，position=0，state=playing；
- 最后一首 `next`：不循环，保留最后 index，position=0，state=stopped；
- 最后一首自然结束：保留最后 index，position=0，state=stopped，并通过唯一 automatic queue-terminal canonical mutation 收敛主 Context。

### D11 — Recovery abandon

选择 A。增加管理端/调试 CLI recovery abandon，不加入 Flutter 公共 strict realtime action。abandon 必须与 exact pair decommission 原子绑定。

### D12 — Follow disconnect/reconnect

选择 A：

- 只有 source state 为 playing 时使用 3 秒 stale 门槛；
- paused/stopped 是稳定事实，不因没有进度 heartbeat 自动 stale；
- idle Context 不允许新建 Follow；
- active Follow 的 source 后续转 idle 时，follower 清空 mirror audio、保持 relationship，静默等待 source 再次 queue-backed；
- source authority 离线、playing fact 过期或 status 恢复失败时，进入最多 30 秒 source 恢复窗口；
- 同一应用进程内 Socket 重连后可以重新 `follow.start` 并继续；
- 30 秒仍无法恢复或 source Context closed 时退出并恢复本机任务；closed 立即结束；
- 应用进程重启后不自动恢复音频 Follow，只执行安全 cleanup。

### D13 — User controls while following

选择 A。Follow 模式禁用 play/pause/seek/next/prev 与 queue mutation；保留本机音量和“停止跟播”。服务端 Follow fence 同时阻止其他 controller 修改 follower suspended Context。

### D14 — Follow feedback

选择 A。strict Follow overlay 不发送普通 `playback.update`，也不新增 `follow.feedback`。Follow mirror 不得写 source Context 或 follower 原 Context。

### D15 — Mode coexistence

允许同一个正常 source Context 同时被 Follow 和 Broadcast 使用。同一设备本地执行覆盖层互斥：

```text
Follow follower
Broadcast ordinary participant
Handoff target preparing/ready/committing
```

### D16 — Broadcast membership

选择 A。r18 使用固定 membership；不增加 leave/add/remove participant。

### D17 — Control settlement recipients

选择 A。`playback.control.settled` 发给仍是同一物理连接的原请求 Socket、全部当前 Context subscribers，以及仍是最初 routed 物理连接的原 authority；按 sid 去重。

## 3. 公共 wire、身份、Cursor 与错误规则

### 3.1 Active Context snapshot 必须表达 exact authority pair

所有 active Context snapshot 必需包含：

```text
authorityClientId
authorityDeviceSessionId
```

至少覆盖：

- `playback.context.ensure` direct response；
- `playback.context.status.playbackContext`；
- canonical `queue.context.sync`；
- 其他复用 Context snapshot schema 的服务端状态消息；
- 服务端 serializer/validator；
- Flutter Context model 与 fixtures。

Context snapshot 不新增 `playbackRate`；rate 继续属于 DevicePlaybackState、`playback.update` 和 Broadcast/Handoff target。

### 3.2 Context-scoped error 必须带完整四 Cursor

`system.error` 闭合 allowlist 增加：

```text
currentEpoch
```

以下 Context-scoped 错误必须携带：

```text
playbackContextId
currentEpoch
currentVersion
currentQueueRevision
currentControlVersion
```

适用于：

- `stale_version`；
- `queue_required`；
- `restore_in_progress`；
- Context/Handoff/Broadcast/Follow fence 的 state-machine `conflict`；
- close tombstone 的 `context_closed`。

纯 requestId fingerprint conflict 可省略 Context cursor。涉及 source/target 两个 Context 时，error 中的 `playbackContextId/current*` 必须描述真正阻止操作的 Context；source Context 仍由原请求 payload 确定。

### 3.3 用户域解析和错误顺序

服务端不得为了选择错误码做全局存在性查询。统一顺序：

```text
envelope/schema
authentication
registration/capability
caller role
authenticated-user-scoped lookup
lifecycle/overlay/recovery fence
base cursor
mutation
```

规则：

- 其他用户资源与真正不存在资源使用相同 `not_found`；
- `forbidden` 只用于当前用户域内可见资源的角色/权限不足；
- Handoff target、volume target 只在当前用户域解析；
- Broadcast explicit participant 中跨用户、不存在、离线或不可用目标使用同类 `skippedClientIds`；
- 日志不得泄露全局查询获得的其他用户资源细节。

### 3.4 `device.list.volumeState`

只向请求连接满足：

```text
negotiatedCapabilities.remoteVolumeControl == true
```

时输出 `volumeState`。不得使用“capability shape 包含该字段”作为条件。

## 4. Core prepare、Queue、Control transaction 与 safe close

### 4.1 Core prepare

`playback.context.prepare`：

- 属于 Core；
- 不检查 Handoff profile；
- 不检查 `playbackPrepare:true`；
- 只检查同用户 controller、唯一 active Context、authority exact pair 在线、authority player/canPlay、binding/epoch/cursor 和 prepare intent 状态。

`playbackPrepare` 仅控制 Handoff target 的 server-routed `playback.prepare`。

### 4.2 Queue 边界 Cursor

第一首 `player.prev`：

```text
currentIndex 不变
trackId 不变
state=playing
positionMs=0
epoch 不变
version += 1
queueRevision 不变
controlVersion += 1
```

最后一首 `player.next`：

```text
currentIndex 不变
trackId 不变
state=stopped
positionMs=0
epoch 不变
version += 1
queueRevision 不变
controlVersion += 1
```

最后一首自然结束使用唯一 passive automatic terminal 例外：

```text
playback.update(
  origin:"passive",
  state:"stopped",
  positionMs:0,
  trackId:当前最后一首,
  appliedControlVersion:canonical controlVersion
)
```

仅当 queue 非空、当前为最后 index、此前 canonical playing、track 匹配、无 pending control、authority exact pair/Socket/epoch 匹配时接受。原子效果：

```text
Context.state=stopped
Context.positionMs=0
Context.version += 1
epoch/queueRevision/controlVersion 不变
DevicePlaybackState 保存 stopped/0
```

只派生一次 Follow fact 和一次 Broadcast stopped revision。

### 4.3 Routed control closed shape

普通 routed action：

```text
queue.playItem
player.play
player.pause
player.seek
player.next
player.prev
```

必须携带：

```text
playbackContextId
controlVersion
sourceClientId
executionTimeoutMs:int>=1
dependsOnControlVersion:O int>=1
action-specific fields
```

active Broadcast source 派生的普通 command 同样携带 `executionTimeoutMs` 和可选 dependency，并按既有规则额外携带成组 effective-at 字段。Handoff commit 使用独立闭合 shape，不使用 `executionTimeoutMs`。

部署默认：

```text
executionTimeoutMs = 15000
```

### 4.4 Deterministic dependency assignment

track-changing action 固定为：

```text
queue.playItem
player.next
player.prev
```

接受每个普通控制时，服务端查找当前 Context/epoch 内最高的、仍 pending 的较低 track-changing version：

```text
存在：
  新事务.dependsOnControlVersion = 该版本
不存在：
  省略
```

新 track-changing transaction 本身也可以依赖此前 pending track-changing transaction，因此依赖链可传递。

Windows 收到带 dependency 的 command 后：

1. 登记事务，但不得执行；
2. 等待 dependency 的 canonical committed confirmation；
3. dependency committed 后才进入 execution-eligible；
4. dependency failed/unknown/dependency_failed 时丢弃本地待执行事务；
5. dependency superseded 时按 supersede 丢弃；
6. 不得在错误歌曲/队列基线上执行 seek/pause/play 等后续操作。

服务端 cascade：

```text
dependency failed / execution_unknown / dependency_failed
→ 直接依赖它的 pending transaction 按 controlVersion 升序变为 dependency_failed
→ 再递归结算其后继
```

每条 `dependency_failed` 的 `dependsOnControlVersion` 指向其直接依赖，不一律指向根失败版本。

### 4.5 Execution eligibility、lease 与 watchdog

控制事务内部必须保存：

```text
requestingClientId
requestingDeviceSessionId
requestingConnectionNonce
requestingConnectionEpoch
authorityClientId
authorityDeviceSessionId
routedConnectionNonce
routedConnectionEpoch
action
acceptedTarget
executionTimeoutMs
dependsOnControlVersion:O
executionEligibleAtMs:O
watchdogDeadlineAtMs:O
```

时间规则：

```text
无依赖、无 effective-at：
  command 可靠加入 authority 发送路径时 executionEligibleAtMs

有依赖：
  dependency canonical committed 时 executionEligibleAtMs 才可建立

有 effective-at：
  executionEligibleAtMs = max(dependency committed time, effectiveAtServerMs)
```

Windows AudioExecutionLease 从 execution eligibility 开始计时；服务端：

```text
watchdogDeadlineAtMs =
  executionEligibleAtMs + executionTimeoutMs + 2000
```

等待 dependency 的时间不计入 execution timeout。dependency 在 eligibility 前失败时，直接 dependency_failed，不建立 watchdog。

若 dependency 直到 effective-at 后才成功：

- 迟到不超过 1000ms：按 effective-at late policy 追赶；
- 超过 1000ms：不得执行成功，按 `effective_at_missed` 结算。

### 4.6 `playback.control.settled`

闭合 payload：

```text
playbackContextId
epoch
commandControlVersion
status:"failed"
errorCode:"dependency_failed"|"execution_unknown"
controlVersion
appliedControlVersion
requestingClientId
requestingDeviceSessionId
serverUpdatedAtMs
dependsOnControlVersion:C
errorMessage:O
```

规则：

- `dependsOnControlVersion` 在 `dependency_failed` 时必需，`execution_unknown` 时禁止；
- 幂等键 `(playbackContextId, epoch, commandControlVersion)`；
- authority disconnect、Socket replacement、服务端 restart、watchdog 到期只能生成 `execution_unknown`，不得伪造 authority playback.update；
- 原请求 replacement Socket 不自动补历史 settlement；
- authority 收到 settlement 必须失效相应 execution lease；
- settlement recipients 按 D17，按 sid 去重。

### 4.7 Terminal control gap reconciliation

不仅 `execution_unknown`，以下 terminal gap 都必须最终收敛：

```text
remoteCommand failed
execution_unknown
dependency_failed 链结束后留下的 terminal gap
```

前置条件：

```text
authority exact pair/epoch/current physical Socket 匹配
fresh actual fact 合法
fact.appliedControlVersion <= canonical controlVersion
fact.appliedControlVersion 后至 canonical controlVersion 的事务全部 terminal
不存在新的 pending transaction
actual track 在 distinct canonical queue 中唯一解析
该 gap 尚未 reconciliation
```

服务端不得把旧 failed/unknown command 伪装成 applied。设：

```text
N = reconciliation 前 canonical controlVersion
R = N + 1
```

原子执行：

```text
创建内部 serverReconciliation record R（无客户端 request/action，无 AudioExecutionLease）
Context 按 actual 收敛 state/currentIndex/trackId/position
Context.controlVersion = R
Context.version += 1
currentIndex 改变时 queueRevision += 1
Context.epoch 不变
DevicePlaybackState 保存 actual playbackRate 和其他事实
DevicePlaybackState.appliedControlVersion = R
旧 failed/unknown/dependency transaction 保持原 terminal
```

Context snapshot不增加 playbackRate。

唯一 wire confirmation：

- 由 passive fact 触发：同一 clientSeq 返回一份 canonical `playback.update(origin:"passive", controlVersion:R, appliedControlVersion:R, actual...)`，再推 Context status；
- remoteCommand failed 且无后续 pending、可在同一事务立即 reconciliation：只发送一份 canonical failed confirmation：

```text
origin:"remoteCommand"
executionStatus:"failed"
commandControlVersion:N
controlVersion:R
appliedControlVersion:R
errorCode:原执行错误
actual state/track/position/rate
```

此时 `appliedControlVersion:R` 表示 actual 被内部 reconciliation R 吸收，不表示命令 N 成功。若仍有 pending，则先发送普通 failed，等 gap 全 terminal 后由 fresh passive fact分配 R。

迟到 remote committed/failed 不得改写旧 terminal；相同结果重放既有 canonical outcome，不同结果 `conflict`。active Broadcast 只从 R 派生一个 correction revision；Follow 只消费一次 R canonical fact。

### 4.8 Safe close

请求闭合为：

```text
playbackContextId:R
expectedEpoch:R int>=1
baseVersion:R int>=1
```

首次 close 在 Context/authority-pair 临界区验证。stale 返回完整四 Cursor。非终态 Handoff、Broadcast/Follow fence、restorePending 按对应优先错误拒绝。

closed tombstone 保存：

```text
closedFromEpoch
closedFromVersion
finalEpoch
finalVersion
finalQueueRevision
finalControlVersion
close ACK outcome
```

新 requestId 重复 close：

- expected/base 与 `closedFrom*` 相同：幂等重放等价 ACK；
- 不同：`context_closed` + final 四 Cursor；
- 不再次递增任何 cursor。

管理端强制关闭不加入普通 strict Socket action。

## 5. Follow 完整闭环

### 5.1 Capability 与 source eligibility

固定十字段 capability shape不新增 rate capability。

`effectiveAtPlayback:true` 的协商条件必须覆盖 Follow、Handoff 或 Broadcast 任一已开启 profile。

`supportsFollow:true` 代表 follower 同时满足：

```text
role includes player
playbackContextV2:true
effectiveAtPlayback:true
canPlay:true
canPause:true
canSeek:true
支持并保持 0.5..2.0 playbackRate
```

Follow source 不要求 supportsFollow，但必须：

```text
当前 Context authority player
playbackContextV2:true
effectiveAtPlayback:true
当前 source Socket clock gate 有效
playbackRate 0.5..2.0
source Context != follower suspended Context
source authority pair != follower exact pair
```

source fact 无论 playing/paused/stopped 都必须属于当前物理连接：

```text
sourceClientId == authorityClientId
deviceSessionId == authorityDeviceSessionId
内部 fact.connectionNonce == 当前 source Socket nonce
Context epoch 匹配
appliedControlVersion == controlVersion
```

状态门禁：

```text
playing：
  queue-backed、settled、track/index/rate 匹配
  serverUpdatedAtMs 与 positionSampledAtServerMs 年龄均 <= 2000ms

paused/stopped：
  queue-backed、settled、track/index/rate 匹配
  不套用 playing 的 2000ms进度 freshness

idle：
  follow.start -> queue_required，零副作用
```

### 5.2 Preflight、RecoveryRecord 与 Follow start ACK

Flutter 在发送 `follow.start` 前：

1. 解析自身 exact pair 的唯一 active suspended Context；
2. 确认 applied==control、无 pending control、无 active Core prepare/Handoff/Broadcast/Follow/restore fence；
3. 确认本地 command lane 和 AudioExecutionLease 空闲；
4. 捕获原 queue/index/position/state/rate 与 suspended Context exact authority/cursors；
5. **先持久化** `FollowRecoveryRecord phase=acquiring`，再发送 `follow.start`。

RecoveryRecord 至少保存：

```text
sourcePlaybackContextId
suspendedPlaybackContextId
suspendedAuthorityClientId
suspendedAuthorityDeviceSessionId
suspendedEpoch
suspendedVersion
suspendedQueueRevision
suspendedControlVersion
suspendedAppliedControlVersion
原 queue/index/position/state/playbackRate
phase: acquiring|active|stopPending|restoring
relationshipAcquired
```

服务端在一个事务中冻结当前 baseline，建立 relationship、subscription、`FollowSafetyLease` 和 fence，然后 ACK：

```text
action:"follow.start"
status:"active"
sourcePlaybackContextId
suspendedPlaybackContextId
suspendedAuthorityClientId
suspendedAuthorityDeviceSessionId
suspendedEpoch
suspendedVersion
suspendedQueueRevision
suspendedControlVersion
suspendedAppliedControlVersion
```

Flutter 必须逐字段比较 ACK baseline 与本地 acquiring record。完全匹配才改为 phase=active 并触碰音频；不匹配则不进入 overlay，立即幂等 `follow.stop`，然后按服务端当前 status 收敛。

如果 acquiring record 写失败，不发送 start。若 ACK 已收到但 active record 更新失败，不触碰音频，立即 stop；stop 结算未知时保存 stopPending 并断开 Socket。

### 5.3 Persistent FollowSafetyLease、fence 与上限

服务端持久化：

```text
userName
followerClientId
followerDeviceSessionId
sourcePlaybackContextId
suspendedPlaybackContextId
suspendedAuthorityClientId
suspendedAuthorityDeviceSessionId
suspendedEpoch
suspendedVersion
suspendedQueueRevision
suspendedControlVersion
suspendedAppliedControlVersion
phase: active|reconnectGrace|cleanupRequired
followReconnectGraceExpiresAtMs
createdAtMs
updatedAtMs
```

它只恢复安全 fence，不代表服务端重启后自动恢复音频 Follow。

资源上限：

```text
一个 exact follower pair 最多一条非终态 lease
一个 suspended Context 最多被一个 Follow overlay 占用
每 user 最多 256 条 active/reconnectGrace/cleanupRequired safety records
```

达到上限：

- 新 `follow.start` -> `rate_limited`；
- 已有关系重试仍可重放；
- `follow.stop`/cleanup 永远允许；
- 不得删除旧未完成 lease 接受新 lease。

cleanupRequired tombstone保留到 exact pair `follow.stop` 或管理端 decommission，不能按普通 TTL 静默删除。

### 5.4 Follow occupancy fence

active、terminating、reconnectGrace、cleanupRequired 阶段阻止 suspended Context：

```text
player.*
queue.playItem
queue.context.sync
playback.context.prepare
playback.context.prepared（cleanup negative 除外）
playback.update
playback.context.close
会创建/初始化/重绑/改 snapshot 的 ensure
Handoff source/target
Broadcast source/ordinary
另一个 Follow source
```

允许：

```text
list/status/subscribe/unsubscribe
follow.stop
device volume/list
system.ping
其他纯读取/clock
```

阻止结果 `conflict` + suspended Context 四 Cursor，零副作用。

### 5.5 Follow source state 与本地失败

active source 转 idle：

```text
停止并清空 mirror audio
保持 relationship/SafetyLease/fence
不恢复原任务
等待 source 再次 queue-backed
```

Follow 本地 queue load、seek、rate、play/pause 或媒体应用失败时，不发送 normal playback.update：

```text
触碰音频前失败：
  不进入 overlay
  恢复/保持原任务
  follow.stop

已进入 overlay 后失败：
  停止 mirror
  恢复原任务
  恢复成功后 follow.stop

恢复失败：
  保持非播放
  保留 RecoveryRecord/SafetyLease/fence
  不发送 follow.stop
  有界重试或断开 Socket
```

### 5.6 Stop、断线、重连与服务端重启

本地 intent：

```text
followResumeIntent：仅当前进程内存
followStopPending：持久化，只允许继续退出
```

停止顺序：

```text
phase=restoring
忽略 source mirror
取消 drift timer/seek/rate correction
读取服务端当前 suspended Context status
比较 SafetyLease 冻结 baseline
```

恢复规则：

- 服务端 binding/cursors仍等于冻结 baseline：可恢复本地原快照；
- 服务端 cursor 更高、binding 改变或 Context closed：丢弃旧本地快照，采用服务端当前 status，绝不能把旧 queue/cursor 写回。

恢复成功后发送 `follow.stop`；服务端 ACK 后原子删除 relationship/subscription/SafetyLease/fence；最后清 RecoveryRecord。fence 释放后才允许 normal playback.update。

两个不同 timer：

```text
followSourceRecoveryDeadlineAtMs
followReconnectGraceExpiresAtMs
```

服务端重启：

```text
不自动恢复 mirror audio
重新加载 SafetyLease
非终态 lease -> cleanupRequired/reconnectGrace
恢复 suspended Context fence
相同 pair follow.start 或 follow.stop 完成前不开放普通写
```

Socket disconnect 时 lease进入 reconnectGrace。同进程 resume 重发 start；stopPending 只重试 stop。grace 到期且 pair 离线可释放 active in-memory fence，但保留 cleanupRequired tombstone，阻止旧 pair 下次注册直接写 suspended Context。

## 6. Handoff 完整闭环

### 6.1 Capability 与 exact pair

source 必须：

```text
当前 authority player
canPause:true
effectiveAtPlayback:true
有效 clock gate
fresh/settled/playing fact
```

target 必须：

```text
player
playbackContextV2:true
playbackPrepare:true
effectiveAtPlayback:true
canPlay/canPause/canSeek:true
支持 0.5..2.0 rate
有效 clock gate
```

`playback.handoff.start` 必需：

```text
playbackContextId
targetClientId
targetDeviceSessionId
baseControlVersion
```

target 冻结为 exact pair；start 前 queue/fact/cursor/pending/overlay 全部零副作用验证。

### 6.2 Full lifecycle fences 与 target local UI

start 成功同时建立：

```text
source Context fence
target exact pair fence
target standby Context fence
```

覆盖 preparing/ready/committing 到 terminal。

source 正常 position/sample 前进允许；remote player/queue/close/第二个 Handoff/Broadcast/binding mutation 被阻止。source localUser、自然结束、自然切歌或实际 track/state/rate 变化时：

```text
同一临界区先 failed/source_changed
通知 target 取消 timer/lease
再提交 source actual mutation
```

target 本机在 preparing/ready/committing 禁用：

```text
play/pause/seek/next/prev
queue mutation
新的 Follow/Broadcast/Handoff
```

只保留设备音量和 Handoff cancel/failure cleanup。

### 6.3 Prepare/commit/provisional version

prepare：

```text
controlVersion=N
playbackRate
positionSampledAtServerMs
sourceEpoch
sourceVersion
sourceQueueRevision
其他既有 snapshot 字段
```

ready 后、commit 前重新读 source。position/sample 正常推进不 source_changed；track/state/rate/binding/cursor/pending变化则 source_changed。

commit 是独立 Handoff `player.play` closed shape，至少：

```text
playbackContextId
handoffId
controlVersion=N+1
sourceClientId
serverTimeMs
effectiveAtServerMs
positionMs
playbackRate
```

`N+1` 是 `(playbackContextId, epoch, handoffId)` provisional version，不是 canonical cursor。

包含 `handoffId` 的 commit：

- 不进入普通 ControlTransactionCoordinator；
- 不推进普通 Context reducer；
- 不发送 remoteCommand playback.update；
- 只进入 Handoff execution lane；
- 只由 complete/cancel/timeout结算。

只有 completed status / Context status 后 Flutter 才把 N+1视为 canonical。Handoff failed/cancelled/timedOut 且 target lease失效后，Context仍为N，下一普通 mutation可重新分配N+1；旧消息必须匹配 handoffId、exact pair、physical Socket和非终态状态。

### 6.4 Position projection、near-end 与 complete proof

服务端使用最新 sample 生成 commit position。若已知 duration 且：

```text
projectedPositionMs >= durationMs
```

commit 前终止为 `failed/source_changed`，authority保持 source，不发送必然失败的 commit。

complete request：

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

必须验证：

```text
exact target Socket/pair
queueIndex/track/rate匹配
appliedControlVersion == provisional N+1
state=playing
known duration bounds
target clock gate仍有效
sample future <= 50ms
sample age <= 2000ms
sample >= effectiveAtServerMs
sample-effectiveAt <= 1000ms
```

统一整数投影：

```text
deltaMs = max(0, positionSampledAtServerMs - effectiveAtServerMs)
expectedPositionMs =
  commit.positionMs + floor(deltaMs * playbackRate)
known duration 时 min(expectedPositionMs, durationMs)
```

r18 固定：

```text
HANDOFF_COMPLETE_POSITION_TOLERANCE_MS = 1000
abs(reportedPositionMs - expectedPositionMs) <= 1000
```

未来时间容差 50ms、执行迟到容差 1000ms、位置容差 1000ms 是三种不同门槛。

complete.clientSeq 复用 target 后续普通 playback.update 的 `(playbackContextId,targetClientId,connectionNonce,connectionEpoch)` 作用域并消耗序号。

complete 原子：

```text
退休 standby
写完整 target DevicePlaybackState
authority exact pair切换
epoch += 1
version += 1
queueRevision不变
controlVersion=N+1
Handoff completed
```

completed status/release 必须包含：

```text
newAuthorityClientId
newAuthorityDeviceSessionId
```

### 6.5 Failure、disconnect 与幂等

target committing 执行失败通过 target-only：

```text
playback.handoff.cancel
reason:"commit_failed"
errorCode:"commit_failed"
errorMessage:O
```

服务端结算 failed/commit_failed。target Socket disconnect 或服务端 restart 时，Flutter立即：

```text
取消 scheduled timer
失效 Handoff lease
已起播则暂停
不得发送迟到 complete
```

旧 source 收到 release、completed status、Context status 或 bindings.changed 任一权威事实都必须停止旧 authority lease。

duplicate start重放首次 preparing ACK；duplicate/late ready只重放 canonical status；duplicate complete重放 completed status+Context status，不重复 switch/release。prepare/commit不能可靠 enqueue时立即 failed。

## 7. Broadcast restore、soft sync 与管理清理

### 7.1 restorePending action-aware gate

阻止：

```text
会创建/初始化/重绑/改 snapshot 的 ensure
playback.context.prepare
playback.context.close
queue.context.sync
queue.playItem
player.*
playback.update
playback.handoff.start/complete
playback.ready(ready:true)
playback.context.prepared(ready:true)
follow.start
broadcast.start
authority/device binding mutation
```

允许清理/读取：

```text
playback.ready(ready:false,errorCode:"restore_in_progress")
playback.context.prepared(ready:false,errorCode:"restore_in_progress")
playback.handoff.cancel
follow.stop
terminal broadcast.feedback
broadcast.status
list/status/subscribe/unsubscribe
device.setVolume/device.volume.update/device.list
system.ping
terminal replay
```

negative confirmation只结算匹配 raced prepare，不初始化队列、不推进 cursor、不 commit、不清 restore fence。

被阻止错误：

```text
restore_in_progress
retryable:true
playbackContextId
currentEpoch
currentVersion
currentQueueRevision
currentControlVersion
```

描述真正被 fence 占用的 suspended Context，零副作用。

### 7.2 Soft sync 与固定 membership

`applied` 只证明 revision target已应用，不证明持续 drift SLA。feedback position只做类型、非负和已知 duration范围校验。

membership从start到terminal固定；不支持leave/add/remove。

### 7.3 Terminal delivery gate

terminal stop/restore未成功加入当前 pair发送路径时：

- 该 Socket不得先收到 suspended Context普通写 command；
- enqueue失败立即断开；
- 下次注册先terminal replay，再开放普通业务。

### 7.4 Recovery abandon、decommission 与资源

管理事务原子：

```text
确认 Broadcast terminal
确认 exact pair restorePending
删除 full/compact recovery obligation
删除 ordinary fence
释放 recovery slot
写 abandoned tombstone
写 exact pair decommission tombstone
撤销当前注册并断开在线 exact pair Socket
从 device.list 移除
停止路由 command
```

decommission key：

```text
(user, clientId, deviceSessionId)
```

永久拒绝复用；可压缩为最小 tombstone，仅账号/用户数据整体删除时清除。达到部署上限时限制新 deviceSession 创建，不能删除旧 decommission tombstone。

同一 clientId 可使用新的 deviceSessionId 建立新生命周期。

## 8. 持久化、重启和资源上限

### 8.1 Pending controls on restart

服务端 restart 时所有 pending ordinary control 必须结算为 `execution_unknown`，不再保留“有界重投或 failed/superseded 任意选择”的旧规则。随后依 terminal-gap reconciliation 收敛。

### 8.2 Follow resources

见 5.3：

- exact pair最多一条非终态 lease；
- suspended Context最多一个 Follow overlay；
- 每user最多256条 active/reconnectGrace/cleanupRequired record；
- cleanup不受 rate limit；
- 未完成旧 lease不得为接收新 lease而删除。

### 8.3 Existing limits

保留现有 control/local intent/Broadcast ledger、participant、recovery slot、payload和connection上限。新增内部 reconciliation record计入 control transaction持久化上限；不得在其仍被 gap审计引用时提前清理。

## 9. REQ、mapping 与状态

- 保持 REQ-001—REQ-067 稳定；
- 已有语义变化直接修改原 REQ；
- 新职责从 REQ-068 继续追加；
- 新增或改变但尚未实现的要求标记 `Contract defined / implementation pending`；
- 旧测试不覆盖新语义时，旧 `Verified` 必须降级；
- 不新建 r19 mapping；
- 更新 `docs/verification/emosonic_strict_v2_r18_requirement_mapping.md`；
- mapping分别记录服务端 schema、服务端状态机、Flutter parser/controller、自动测试、Android+Windows真机证据；
- Contract Frozen不等于 profile implementation ready。

## 10. 契约修改批次

每个 Batch 完成后必须全仓搜索交叉引用。

### Batch A — 公共 schema、Core、settlement 与 safe close

最低必改：

```text
01-overview.md
02-transport-registration-and-clock.md
03-ack-errors-idempotency-and-cursors.md
04-client-core-actions.md
07-server-device-context-and-status.md
08-server-queue-playback-and-controls.md
06b-broadcast-source-context.md
11a-common-and-core-requirements.md
11b-broadcast-requirements.md
11c-security-persistence-and-readiness.md
13-integration-acceptance.md
```

落实：

- Context snapshot exact authority pair；
- currentEpoch error；
- Core prepare解耦；
- routed dependency/timeout/eligibility；
- settlement exact requester pair；
- transitive dependency cascade；
- terminal-gap reconciliation与唯一 wire confirmation；
- queue边界/natural terminal；
- safe close；
- user-scoped lookup与volumeState。

### Batch B — Follow capability、baseline ACK、安全租约与恢复

最低必改：

```text
01,02,03,04,07,08
05-client-follow-and-handoff.md
06a,06c,06d
09-server-handoff.md
11a,11b,11c
13-integration-acceptance.md
```

落实：

- Follow复合能力/effectiveAt；
- source current physical fact；
- state-specific source门禁、idle/self gate；
- RecoveryRecord prewrite；
- start ACK冻结 baseline；
- persistent SafetyLease、fence、resource limit；
- local failure；
- sourceIdle；
- reconnect/restart/cleanup cursor comparison。

### Batch C — Handoff provisional lane、position proof 与全生命周期 fence

最低必改：

```text
01,02,03,04,07,08
05-client-follow-and-handoff.md
09-server-handoff.md
11a,11c
13-integration-acceptance.md
```

落实：

- source/target capability和exact pair；
- full lifecycle fences；
- target UI gate；
- independent Handoff execution lane；
- provisional N+1；
- prepare/commit/complete shape；
- 50ms future、1000ms late、1000ms position tolerance；
- near-end fail-fast；
- clientSeq；
- disconnect immediate cancel；
- terminal exact pair、duplicate和enqueue failure。

### Batch D — Broadcast restore、terminal gate 与 decommission

最低必改：

```text
01,03,04,07,08
05
06a,06c,06d
09
10-server-broadcast.md
11a,11b,11c
13-integration-acceptance.md
```

落实：

- action-aware restore gate；
- Handoff/Core negative cleanup；
- soft sync；
- fixed membership；
- terminal delivery gate；
- online abandon disconnect；
- permanent decommission tombstone。

### Batch E — 权威入口、mapping 与冻结

最低必改：

```text
specs/emosonic_strict_v2_socketio_server_contract.md
全部19个权威分卷页首
docs/verification/emosonic_strict_v2_r18_requirement_mapping.md
13-integration-acceptance.md
14-authority-and-deployment-evidence.md
```

落实：

- `2026-08-01-r18` / `2.8.0`；
- Approved + Contract Frozen + implementation pending；
- pre-freeze例外和成组升级；
- mapping降级；
- r19/errata纪律；
- 真机只决定 implementation readiness。

## 11. 契约完成后的实现顺序

### Phase 1 — Capability、schema validator 与 fixtures

服务端：

```text
supysonic/emo/strict_v2_contract.py
supysonic/emo/strict_v2_readiness.py
```

Flutter：

```text
emo_action_contract_policy.dart
emo_strict_v2_models.dart
StrictV2CapabilityPolicy所在文件
双方 strict-v2 fixtures
```

先同步：

- Context snapshot `authorityDeviceSessionId`；
- `currentEpoch` error；
- routed `dependsOnControlVersion` / `executionTimeoutMs`；
- settlement requesting exact pair；
- close；
- Follow start ACK baseline；
- Handoff所有新shape；
- restore errors；
- volumeState条件。

### Phase 2 — Core

服务端：`ws.py`、`ws_store.py`、数据库模型/迁移。  
Flutter：control coordinator、execution lease、command lane、audio end callback、close sender。

实现 dependency admission/hold/cascade、eligibility watchdog、terminal-gap reconciliation、安全 close、queue terminal。

### Phase 3 — Follow

服务端：persistent SafetyLease store、fence、restart cleanup、limit。  
Flutter：RecoveryRecord、prewrite/ACK compare、resume/stop intent、local failure、sourceIdle、cleanup。

### Phase 4 — Handoff

服务端：fences、exact pair、provisional lane、source revalidation、complete transaction、position proof。  
Flutter：independent Handoff lane、target UI gate、complete proof/clientSeq、disconnect cancel、source fallback stop。

### Phase 5 — Broadcast / management

服务端：restore matrix、negative cleanup、terminal gate、abandon/decommission。  
Flutter：删除恢复期普通命令队列，处理 restore error和raced prepare cleanup。

## 12. 必须新增的验收

### Core

- Core prepare不依赖 playbackPrepare；
- routed dependency字段和transitive chain；
- dependent command等待 committed后才开始lease；
- dependency等待不消耗execution timeout；
- effective-at+dependency迟到策略；
- requester exact pair settlement；
- restart/watchdog/disconnect unknown；
- failed/unknown/dependency gap均分配新 reconciliation version；
- remote failed inline reconciliation唯一confirmation；
- Context无playbackRate但DeviceState保留rate；
- close currentEpoch/version和tombstone；
- first-prev、last-next、natural terminal；
- Context snapshot exact authority pair；
- currentEpoch error；
- volumeState capability。

### Follow

- Follow profile正确协商effectiveAt；
- source fact必须来自当前nonce；
- playing/paused/stopped/idle/self-follow；
- RecoveryRecord在start前写入；
- start ACK baseline匹配/不匹配；
- crash在ACK前后均可cleanup；
- SafetyLease重启恢复fence；
- 每user/pair/context资源上限；
- local mirror执行失败；
- sourceIdle保持relationship；
- stop恢复时cursor未变/已变/closed三种分支；
- stop ACK丢失、app restart不自动Follow；
- Follow+Broadcast source共存。

### Handoff

- provisional commit不进入普通Control coordinator；
- source/target effectiveAt与clock gate；
- full fences和target UI gate；
- prepare正常position推进；
- near-end commit前source_changed；
- sample future>50ms拒绝；
- late>1000ms拒绝；
- wrong position>1000ms拒绝；
- floor投影一致；
- target disconnect立即取消timer；
- provisional N+1失败后安全复用；
- complete clientSeq连续；
- exact pair terminal；
- duplicate和enqueue failure。

### Broadcast / management / security

- restore action矩阵；
- negative ready/prepared cleanup；
- currentEpoch+真正fenced Context cursor；
- terminal enqueue失败断开/replay；
- soft sync applied语义；
- fixed membership；
- abandon在线pair立即断开并从device.list移除；
- decommission tombstone永久拒绝旧pair；
- user-scoped lookup无侧信道。

## 13. 最终机械审计

必须搜索并验证：

1. 当前 normative 身份没有误写 r19/2.9.0；
2. 全部页首为 `2026-08-01-r18`；
3. 不残留 `Frozen implementation baseline`；
4. Context snapshot全部含 `authorityDeviceSessionId`；
5. Context-scoped error全部含 `currentEpoch`；
6. Follow capability/source nonce/idle/self/baseline ACK/SafetyLease完整；
7. Follow RecoveryRecord必须在start前写入；
8. Follow cleanup有cursor comparison和local failure；
9. routed control有executionTimeout和dependency；
10. dependency支持传递且eligibility后才计时；
11. failed/unknown/dependency terminal gap统一reconciliation；
12. reconciliation不把playbackRate写入Context；
13. settlement含requestingDeviceSessionId；
14. close有expectedEpoch/baseVersion/currentEpoch tombstone；
15. Handoff commit不进入普通control reducer；
16. Handoff future=50ms、late=1000ms、position=1000ms；
17. near-end和disconnect规则存在；
18. restore无服务端放行/Flutter排队旧规则；
19. volumeState检查negotiated true；
20. FollowSafetyLease和decommission资源有界；
21. abandon在线pair断开；
22. mapping失效Verified已降级；
23. Markdown链接、REQ引用无断链；
24. `git diff --check`通过。

契约阶段不得为了让旧代码测试通过而修改代码、validator、fixtures或测试。

## 14. 纯文档提交顺序

```text
1. spec: close r18 core dependency settlement and lifecycle gaps
2. spec: define r18 follow baseline safety lease and recovery
3. spec: harden r18 handoff provisional execution and proof
4. spec: finalize r18 broadcast restore and decommission gates
5. spec: freeze r18 contract authority and mapping
```

## 15. r18 契约完成标准

只有以下全部满足，才能宣布 r18 契约 Frozen：

- D1—D17 全部进入权威分卷和 REQ/acceptance；
- 本计划第3—8节全部进入权威分卷，不只停留在计划；
- 每个 action 的请求、ACK/direct response、push、错误、幂等、断线、重启和超时有唯一解释；
- Context snapshot与error都能表达exact pair/epoch；
- dependency admission、execution eligibility、cascade和timeout无歧义；
- failed/unknown/dependency gap通过新 reconciliation version收敛，不伪造旧command成功；
- Follow prewrite baseline、ACK、SafetyLease、fence、local failure、restart cleanup无崩溃窗口；
- Handoff provisional lane与普通control reducer隔离，complete有时间、位置、clock和actual proof；
- restorePending无相反规则；
- user-scoped security、volumeState、资源上限和decommission闭合；
- mapping全部更新，未实现项不伪造Verified；
- 全部19个分卷、入口、修订和版本一致；
- 最终机械审计与`git diff --check`通过。

冻结后进入“实现 r18”阶段，不再重新讨论D1—D17；未来真正改变冻结行为的新功能进入r19。
