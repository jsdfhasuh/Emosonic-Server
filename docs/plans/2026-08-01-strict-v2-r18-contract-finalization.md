# Strict-v2 r18 契约闭合、定稿与冻结计划

> 日期：2026-08-01  
> 目标分支：`agent/strict-v2-r5-server-adaptation`  
> 当前权威基线：strict-v2 `2.8.0` / r18  
> 最终目标身份：`2026-08-01-r18` / `2.8.0`  
> 本文性质：实施计划与决策记录，不替代 `specs/emosonic_strict_v2_socketio_server_contract.md` 及其 19 个权威分卷。

## 1. 版本身份、冻结状态与工作边界

本轮不是创建 r19，也不升级 `protocolVersion`。当前 strict-v2 `2.8.0/r18` 尚未正式发布，本轮属于 r18 在首次冻结前的最终闭合、定稿和一致性修正。

本轮完成并通过最终一致性审计后，权威契约必须明确区分“契约已经冻结”和“实现尚待完成”：

```text
文档状态：Approved r18 authoritative contract
契约冻结状态：Frozen
实现状态：Contract defined / implementation pending
文档修订：2026-08-01-r18
协议版本：2.8.0
```

不得使用容易被误解为实现已经完成的 `Frozen implementation baseline`。

本次保留 `2.8.0` 是首次冻结前的单次闭合例外。权威入口的维护规则必须同步写明：

- 冻结前编译的旧 `2.8.0` 调试服务端与 Flutter 客户端，不保证兼容最终冻结后的 `2.8.0`；
- 服务端与 Flutter 必须按最终 r18 成组升级，不支持新旧 pre-freeze `2.8.0` 混跑；
- r18 冻结后，服务端或 Flutter 与 r18 不一致时，默认修改实现适配 r18；
- 纯错字、链接、示例或不改变行为的文字澄清可作为 r18 errata；
- 冻结后新增 action、字段、状态、错误码、持久化义务或改变客户端行为，必须进入下一修订 r19，并重新评估和更新 `protocolVersion`；
- 冻结后不得继续静默改变 `2.8.0` wire shape。

本轮先修改全部权威契约，再修改服务端、Flutter、schema validator、fixtures 与测试。

以下内容明确不进入本轮：

- 不设计或修改协议版本协商；
- 不修改调试环境中 profile implementation readiness 默认 `true` 的便利行为；
- 不以 conformance manifest 的发布状态阻止本地 debug 路径运行；
- 不新增 legacy/session 兼容 shape；
- 不增加 Broadcast 动态 participant membership、`broadcast.leave`、add/remove participant；
- 不支持 shuffle、repeat-one、repeat-all 或 `queueSongIds` 内重复 songId；
- 不增加 `follow.feedback` 或新的 Follow mirror action；
- 不增加 control transaction 查询 action；
- 不在 r18 中加入严格持续 drift SLA、paused/stopped source Handoff、live-stream 完整模型或新的 buffering/loading/error 枚举。

本轮目标是让以下五层使用同一套最终 r18 定义：

```text
权威 specs
  -> 服务端 strict_v2_contract.py / strict_v2_readiness.py
  -> 服务端 ws.py / ws_store.py / broadcast_store.py
  -> Flutter action policy / models / controllers
  -> 双端 fixtures、自动测试与真机验收
```

## 2. 已锁定的 D1—D17

### D1 — Core prepare

选择 A。`playback.context.prepare` 是 Core 行为，不依赖 Handoff profile，也不依赖 negotiated `playbackPrepare:true`。

`playbackPrepare` 只表示设备能够处理 Handoff target 的 server-routed `playback.prepare` 预加载。

### D2 — Follow

Follow 是 Context 驱动的一对一软同步音频跟随，不是只读观察。

- 每个 follower 同时只跟随一个 `sourcePlaybackContextId`；
- 一个 source Context 可以有多个 follower；
- follower 在本地捕获原任务，应用 source queue/state/position/rate，并做 drift 修正；
- follower 不取得 source Context 控制权；
- Follow 退出后恢复本机原任务；
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

选择 A。`restorePending:true` 期间服务端冻结 suspended Context 的写操作，统一返回 `restore_in_progress`；Flutter 不再排队普通 Context command。清理型 negative confirmation 与 cancel 按本计划的 action-aware 例外继续允许。

### D8 — Broadcast synchronization promise

选择 A。Broadcast 为软同步。`syncStatus:"applied"` 只表示 participant 已应用该 revision 的目标，不证明持续 drift 小于固定毫秒阈值。

### D9 — Close during Handoff

选择 A。存在非终态 Handoff 时 `playback.context.close` 返回 `conflict`；必须先 cancel，再 close。

### D10 — Queue boundary

- `queueSongIds` 不允许重复；
- 不支持 shuffle、repeat-one、repeat-all；
- 第一首 `prev`：重播第一首，position=0，state=playing；
- 最后一首 `next`：不循环，保留最后 index，position=0，state=stopped；
- 最后一首自然结束：保留最后 index，position=0，state=stopped，并通过唯一 automatic queue-terminal canonical mutation 收敛主 Context。

### D11 — Recovery abandon

选择 A。增加管理端/调试 CLI 的 recovery abandon；不加入 Flutter 公共 strict realtime action。abandon 必须与被冻结 pair 的 device decommission 原子绑定，不能让同一旧 `deviceSessionId` 以后重新注册而永远失去 terminal recovery。

### D12 — Follow disconnect/reconnect

选择 A。

- 只有最新 source state 为 `playing` 时，才使用 3 秒新鲜度门槛；
- 最新 source state 为 `paused|stopped` 时，该状态是稳定事实，不能只因没有 heartbeat 而判定 stale；
- idle Context 不允许新建 Follow；active Follow 的 source 后续转为 idle 时，follower 停止并清空 mirror audio、保持 relationship，静默等待 source 再次 queue-backed；
- source authority 明确离线、playing fact 过期或重新 `follow.start`/status 失败时，进入最多 30 秒 source 恢复窗口；
- 同一应用进程内 Socket 重连后可以重新 `follow.start`、subscribe/status 并继续；
- 30 秒仍无法恢复或 source Context closed 时退出 Follow，并恢复本机任务；closed 立即结束，不等待 30 秒；
- 应用进程重启后不自动恢复音频 Follow，只执行旧 relationship/fence 的安全清理。

### D13 — User controls while following

选择 A。Follow 模式禁用 play/pause/seek/next/prev 与 queue mutation；保留本机音量和“停止跟播”。服务端还必须用 Follow occupancy fence 阻止其他 controller 远程修改 follower 的 suspended Context。

### D14 — Follow feedback

选择 A。strict Follow overlay 不发送普通 `playback.update`，也不新增 `follow.feedback`。Follow 镜像不得写 source Context 或 follower 原 Context。退出 Follow、恢复原任务并释放 Follow fence 后，follower 才可为自己的正常 Context 发送普通 `playback.update`。

### D15 — Mode coexistence

允许同一个正常 source Context 同时被 Follow 与 Broadcast 使用。

同一设备本地执行覆盖层互斥：

```text
Follow follower
Broadcast ordinary participant
Handoff target preparing/committing
```

处于 Follow overlay 的 pair 自己的 suspended Context 不得被当作新的 Follow source、Broadcast source、Broadcast participant 或 Handoff source/target。正常 source Context 的 Handoff 仍受 active Broadcast source fence 阻止；Follow 绑定 Context ID，source Handoff 完成后继续跟随新 authority。

### D16 — Broadcast membership

选择 A。r18 继续使用固定 membership；不增加 `broadcast.leave/add/removeParticipant`。

### D17 — Control settlement recipients

选择 A。`playback.control.settled` 发给当前原请求 Socket、全部当前 Context subscribers，并额外发给仍在线的原 authority 物理连接；按 sid 去重。非请求 controller 只更新全局 cursor/UI，不弹出本地操作错误。

## 3. 冻结前必须补齐的机械闭环规则

本节不改变 D1—D17 的产品方向，只消除能力依赖、时间线、清理、序号、并发、恢复和安全上的协议空洞。

### 3.1 Follow 与 Handoff 的复合能力、source 状态和时钟门禁

固定 10 字段 capability shape，不新增 playback-rate capability。`supportsFollow`、`playbackPrepare`、`effectiveAtPlayback` 和 Handoff eligibility 本身承担复合执行承诺。

`effectiveAtPlayback:true` 的协商条件必须覆盖 Follow、Handoff 或 Broadcast 任一已开启 profile，不能只由 Handoff/Broadcast readiness 触发。

`supportsFollow:true` 只可授予满足以下全部条件的 follower connection：

```text
role includes player
playbackContextV2:true
effectiveAtPlayback:true
canPlay:true
canPause:true
canSeek:true
能够设置并保持 0.5..2.0 playbackRate
```

`follow.start` 必须验证 follower 当前 nonce 的 clock gate 已通过。

Follow source 不要求 `supportsFollow:true`，但必须满足：

```text
当前 Context authority player
playbackContextV2:true
effectiveAtPlayback:true
当前 nonce clock gate 有效
playbackRate 合法且在 0.5..2.0
sourcePlaybackContextId != suspendedPlaybackContextId
source authority pair != follower exact pair
```

source 状态门禁分开定义：

```text
playing：
  queue-backed
  DevicePlaybackState settled
  track/index/rate 匹配
  serverUpdatedAtMs 与 positionSampledAtServerMs 年龄均 <= 2000ms

paused/stopped：
  queue-backed
  DevicePlaybackState settled
  track/index/rate 匹配
  不套用 playing 的 2000ms progress freshness 门槛

idle：
  follow.start 返回 queue_required，零副作用
```

active Follow 的 source 后续转为 idle 时，follower 必须停止并清空 mirror audio，保持 relationship 和 Follow fence，不恢复自己的原任务；source 再次 queue-backed 后继续 Follow。

Handoff target 必须满足：

```text
role includes player
playbackContextV2:true
playbackPrepare:true
effectiveAtPlayback:true
canPlay:true
canPause:true
canSeek:true
能够设置并保持 0.5..2.0 playbackRate
当前 nonce clock gate 有效
```

Handoff source 必须是当前在线 authority player，至少具备 `canPause:true`，并具有 fresh、settled、playing fact 和有效 clock gate。能力或 clock gate 不满足时，在任何事务创建前返回 `capability_required` 或带完整 cursors 的 `conflict`。

调试环境默认 capability 为 true 的便利行为保持不变；本节只定义 true 所代表的完整承诺。

### 3.2 Queue 边界与最后一首自然结束

#### 第一首 `player.prev`

目标固定为第一首从头播放：

```text
currentIndex 不变
trackId 不变
state = playing
positionMs = 0
epoch 不变
version += 1
queueRevision 不变
controlVersion += 1
```

#### 最后一首 `player.next`

目标固定为停止在最后一首：

```text
currentIndex 不变
trackId 不变
state = stopped
positionMs = 0
epoch 不变
version += 1
queueRevision 不变
controlVersion += 1
```

#### 最后一首自然结束

最后一首自然结束不能只更新 DevicePlaybackState，否则主 Context 会永久停留在 `playing`。

r18 固定使用以下最小例外，不新增 `origin:"automatic"`：

```text
playback.update(
  origin:"passive",
  state:"stopped",
  positionMs:0,
  trackId:当前最后一首,
  appliedControlVersion:controlVersion
)
```

只有同时满足以下条件时，服务端把它识别为 automatic queue terminal：

```text
canonical queue 非空
currentIndex == queueSongIds.length - 1
此前 canonical state == playing
reported trackId 等于当前最后一首
reported state == stopped
reported positionMs == 0
appliedControlVersion == canonical controlVersion
当前不存在 pending control transaction
当前 authority client/device/Socket 与 Context binding 精确匹配
```

原子效果固定为：

```text
DevicePlaybackState.state = stopped
DevicePlaybackState.positionMs = 0
Context.state = stopped
Context.positionMs = 0
Context.version += 1
Context.epoch 不变
Context.queueRevision 不变
Context.controlVersion 不变
appliedControlVersion 不变
不 supersede 任何事务
```

服务端提交后发送 canonical `playback.update` 与新的 Context status；不需要发送 queue sync。若 source Context 正在作为 Broadcast source，只从这一次 Context mutation 派生一个 stopped `broadcast.state.sync`；Follow follower 也只从该 canonical source fact 收敛一次。

本地人工 stop 仍必须走 `origin:"localUser"`；remote command 结果仍走 remote transaction。其他 passive update 不获得修改主 Context 的权限。

### 3.3 普通控制事务、dependency、timeout 与 settlement exact pair

普通 server-routed action：

```text
queue.playItem
player.play
player.pause
player.seek
player.next
player.prev
```

必须携带正整数 `executionTimeoutMs`。包括 active Broadcast source control 派生的普通 source command；Handoff commit 和 `device.setVolume` 不使用该字段。

部署默认：

```text
executionTimeoutMs = 15000
Windows execution lease deadline = 收到命令后 executionTimeoutMs
server watchdogDeadlineAtMs = acceptedAtMs + executionTimeoutMs + 2000
```

控制事务必须持久化：

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
accepted target
executionTimeoutMs
dependsOnControlVersion:O
```

track-changing action 固定为：

```text
queue.playItem
player.next
player.prev
```

服务端接受每个普通控制时，在当前 Context/epoch 内查找最高的、仍 pending 的 track-changing controlVersion：

```text
存在：
  新事务.dependsOnControlVersion = 该版本

不存在：
  省略 dependsOnControlVersion
```

因此，在未确定切歌成功前接受的 play/pause/seek/next/prev/playItem 都保守依赖当前最高 pending track-changing version。

依赖结算规则：

```text
依赖版本 committed：后续事务正常执行
依赖版本 failed / execution_unknown：后续仍 pending 事务按版本升序 dependency_failed
localUser supersede：仍按 superseded 处理，不转换为 dependency_failed
```

watchdog、authority disconnect、authority Socket replacement 或服务端重启只能生成：

```text
playback.control.settled(
  status:"failed",
  errorCode:"execution_unknown"
)
```

不得伪造 authority `playback.update`。

`playback.control.settled` 的闭合 payload：

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

幂等键：

```text
(playbackContextId, epoch, commandControlVersion)
```

收件人是：

```text
当前原请求 Socket（requesting client/device/nonce/epoch 仍全部匹配时）
全部当前 Context subscribers
原 authority Socket（仍为最初 routed physical connection 时）
```

按 sid 去重。原请求 controller 已断线时，不向 replacement Socket 自动补历史 settlement；controller 断线后本地 pending UI 立即转为 unknown，重连通过 `list -> subscribe -> status` 收敛。

### 3.4 `execution_unknown` 后的 server reconciliation version

`execution_unknown` 终态本身不猜测设备实际结果，也不立即改写 Context。相同 authority pair 重新在线或重新提供 fresh passive actual fact 后，服务端允许一次有界 reconciliation。

前置条件：

```text
当前 authority pair 与 Context 精确匹配
fact 的 track/state/position/rate 合法且 fresh
fact.appliedControlVersion <= canonical controlVersion
fact.appliedControlVersion 之后直到 canonical controlVersion 的事务均已 terminal
至少存在一个 execution_unknown gap
不存在新的 pending transaction
实际 trackId 在 distinct canonical queue 中唯一可解析
同一 unknown gap 尚未 reconciliation
```

服务端不得把旧 unknown command 的版本伪装成已成功 applied。无论 actual 是否已经等于当前 canonical target，都必须分配新的 server reconciliation control version：

```text
N = reconciliation 前 canonical controlVersion
R = N + 1
```

原子规则：

```text
创建内部 reconciliation transaction：
  controlVersion = R
  kind = serverReconciliation（内部字段，不新增客户端 action）
  status = committed

Context 按 fresh actual fact 收敛：
  state/currentIndex/trackId/position/playbackRate = actual
  epoch 不变
  controlVersion = R
  version += 1
  queueRevision 仅 currentIndex 改变时 += 1

DevicePlaybackState：
  保存 actual fact
  appliedControlVersion = R

原 execution_unknown transaction：
  保持原终态，不改成 committed
```

服务端向 authority 与合法 recipients 发送 canonical `playback.update` / Context status，使客户端看到 control/applied 已在 R 收敛。内部持久化：

```text
reconciledFromAppliedControlVersion
reconciledThroughControlVersion
reconciliationControlVersion = R
```

这些是内部审计字段，不增加 r18 wire 字段。

若 actual track 不在 canonical queue、事实不 fresh 或仍有 pending gap，拒绝 reconciliation，并要求通过正常 `queue.context.sync` / `localUser` 路径收敛。

active Broadcast 只从该 canonical correction 派生一个 correction revision；Follow 只从新的 canonical fact 收敛一次。

unknown settlement 之后到达的迟到 remoteCommand committed/failed 不得改变事务终态或 Context；相同内容只重放 unknown settlement，不同 terminal 结果返回 `conflict`。authority 收到 settlement 时必须失效对应 execution lease。

### 3.5 `playback.context.close` 的并发前置条件、错误 shape 和 tombstone

普通 strict close 请求改为：

```text
playbackContextId:R
expectedEpoch:R int>=1
baseVersion:R int>=1
```

首次 close 必须在同一 Context/authority-pair 临界区精确验证 `expectedEpoch` 与 `baseVersion`。不匹配返回 `stale_version`；非终态 Handoff、active Broadcast source/ordinary fence、Follow fence 或 restorePending 按各自 `conflict` / `restore_in_progress` 规则优先拒绝。

合法 close：

```text
lifecycle -> closed
version += 1
其他 cursor 按现有 close 规则保持
```

closed tombstone 必须保存：

```text
closedFromEpoch
closedFromVersion
finalEpoch
finalVersion
finalQueueRevision
finalControlVersion
close ACK outcome
```

使用新的 requestId 重复 close 时：

- `expectedEpoch/baseVersion` 与 tombstone 的 `closedFrom*` 完全相同：幂等重放等价 ACK；
- 内容不同：返回 `context_closed`；
- 不再次递增任何 cursor。

`playback.context.close` 命中 tombstone时，`context_closed` error 必须携带：

```text
playbackContextId
currentVersion = finalVersion
currentQueueRevision = finalQueueRevision
currentControlVersion = finalControlVersion
```

其他 action 的 `context_closed` 是否携带 cursors，按其 action-aware error 表定义；不得与 close tombstone 的必需 shape 冲突。

管理端强制关闭不加入普通 strict Socket action。

### 3.6 Follow settled entry、occupancy fence 与 source/self gate

`follow.start` 必须按 follower 当前注册的 exact client/device pair 解析其唯一 active authority Context，并记录：

```text
suspendedPlaybackContextId
suspendedEpoch
suspendedVersion
suspendedQueueRevision
suspendedControlVersion
suspendedAppliedControlVersion
followerClientId
followerDeviceSessionId
sourcePlaybackContextId
```

服务端在零副作用阶段验证：

```text
follower exact pair 只有一个 active Context
suspendedAppliedControlVersion == suspendedControlVersion
不存在 pending control transaction
不存在非终态 Core prepare
不存在非终态 Handoff
不存在 Broadcast/Follow/restore fence
follower/source capability 与 clock gate 满足第 3.1 节
source Context 可见且 active
source queue-backed 且 actual fact settled
sourcePlaybackContextId != suspendedPlaybackContextId
source authority pair != follower exact pair
```

Flutter 在发送 `follow.start` 前还必须保证本机 strict command lane 与 AudioExecutionLease 没有未完成的 suspended-Context command。客户端前置条件失败时不得发送 start；服务端只能验证持久化 pending 状态，不能假装知道本地尚未回调的音频操作。

任一条件不满足时返回带相关 Context 完整 cursors 的 `conflict` / `capability_required` / `queue_required`，不得建立 relationship、subscription、safety lease 或 fence。

Follow relationship active、terminating、reconnect-grace 或 cleanup-required 期间，服务端对 follower suspended Context 建立 pair-level 写屏障。

阻止：

```text
player.*
queue.playItem
queue.context.sync
playback.context.prepare
playback.context.prepared（清理型 negative confirmation 除外）
playback.update
playback.context.close
playback.context.ensure 的创建、初始化、重绑或快照修改
playback.handoff.start/complete
成为 Handoff source/target
成为 Broadcast source/ordinary participant
成为另一个 Follow source
```

允许：

```text
playback.context.list/status
playback.context.subscribe/unsubscribe
follow.stop
device.setVolume / device.volume.update
device.list
system.ping
其他纯读取和 clock action
```

被阻止的 suspended Context mutation 返回 `conflict`，携带 suspended Context 的完整 canonical cursors，不产生 mutation、command 或 binding invalidation。

同一个正常 source Context 可以同时存在 Follow followers 和 active Broadcast；但 source authority pair 自己不得正处于任何本地 overlay。

### 3.7 FollowRecoveryRecord、持久化 FollowSafetyLease 与安全 cleanup

Follow 不持久化 mirror audio/profile 状态，但必须持久化最小安全门禁。

Flutter 必须保存专用 `FollowRecoveryRecord`，它不写普通 PlaybackContext durable snapshot，至少包含：

```text
sourcePlaybackContextId
suspendedPlaybackContextId
followerClientId
followerDeviceSessionId
进入 Follow 前的 queue/index/position/state/playbackRate
relationshipAcquired
stopPending
```

服务端必须保存最小 `FollowSafetyLease`：

```text
userName
followerClientId
followerDeviceSessionId
sourcePlaybackContextId
suspendedPlaybackContextId
phase: active | reconnectGrace | cleanupRequired
followReconnectGraceExpiresAtMs
createdAtMs
updatedAtMs
```

`FollowSafetyLease` 只用于恢复 fence 和清理义务，不代表服务端在重启后自动恢复 Follow 音频。

安全建立顺序：

1. 服务端原子建立 relationship、subscription、FollowSafetyLease 和 fence，再 ACK `follow.start`；
2. Flutter 收到 ACK 后、触碰本地音频前持久化 `FollowRecoveryRecord`；
3. 记录持久化成功后才允许进入 Follow overlay。

若 `FollowRecoveryRecord` 持久化失败：

```text
不得触碰本地音频
立即发送幂等 follow.stop
follow.stop 无法确定结算时断开当前 Socket
不得进入 Follow overlay
```

因为本地音频尚未切换，服务端 lease 可以在 reconnect grace/cleanup 规则下安全释放；不得假装 Follow 已经正常开始。

客户端本地维护：

```text
followResumeIntent：仅内存、仅当前应用进程可用
followStopPending：持久化、表示只能继续退出，禁止重新 Follow
```

规则：

1. 正常 Follow 中同进程 Socket 断线：保留 `followResumeIntent`；重连后优先重发相同 `follow.start`；
2. 一旦开始停止 Follow：清除 resume intent，设置 `followStopPending=true`；
3. `follow.stop` ACK 丢失后重连：只能幂等重试 `follow.stop`，不得重新 `follow.start`；
4. 应用进程重启：不恢复音频 Follow；读取 FollowRecoveryRecord，把状态视为 cleanup pending，先恢复 suspended Context，再发送幂等 `follow.stop`；
5. cleanup marker 只用于安全退出，不代表 app restart 自动 Follow；
6. Follow cleanup 必须在普通 startup ensure、Context outbox 和音频 command 恢复之前完成；
7. 本地恢复失败：保持本机非播放、保留 recovery record 和服务端 fence，不发送 follow.stop；有界重试，无法恢复时断开当前 Socket，不得提前放行远程命令。

停止 Follow 的固定顺序：

```text
进入 restoringFollowContext
忽略后续 source mirror
取消 drift timer/seek/rate correction
恢复进入 Follow 前的原任务；必要时读取一次 suspended Context status
恢复成功后发送 follow.stop
服务端 ACK 并原子释放 relationship/subscription/FollowSafetyLease/fence
清除 FollowRecoveryRecord
fence 释放后才允许为正常 Context 发送 playback.update
```

source Context closed 时，服务端推 closed 并将 lease 标记为 `cleanupRequired`；follower 在线时保持 fence，直到安全恢复并发送幂等 `follow.stop`。

两个 30 秒计时器必须使用不同名称和状态机：

```text
followSourceRecoveryDeadlineAtMs
  source playing fact/authority 恢复窗口

followReconnectGraceExpiresAtMs
  follower 自身 Socket 重连并重新 acquire relationship 的窗口
```

最新 source state 为 playing 时，`serverUpdatedAtMs` 或 `positionSampledAtServerMs` 任一超过 3000ms，进入 source recovery；paused/stopped 不因没有 heartbeat 自动 stale。

follower Socket 断线时，服务端把 lease 置为 `reconnectGrace` 并保持 fence：

- 相同 pair 在 grace 内重发相同 `follow.start`：幂等恢复 active；
- stopPending 重连：只接受 follow.stop cleanup；
- grace 到期且 pair 离线：可释放当前 active fence，但保留轻量 `cleanupRequired` tombstone，阻止同一旧 pair 下次注册后直接写 suspended Context；
- 同一旧 pair 未来注册时，必须先完成幂等 `follow.stop` cleanup 或管理端 decommission；
- grace 到期后旧 relationship 不得被新连接自动继承。

服务端重启时：

```text
不自动恢复 Follow mirror audio
重新加载 FollowSafetyLease
所有非终态 lease 进入 cleanupRequired/reconnectGrace
恢复 suspended Context fence
相同 pair 完成 follow.start 或 follow.stop 前，不开放普通 Context 写操作
```

这取代旧的“服务端重启时直接清除全部 Follow 并放开写操作”规则。

### 3.8 Handoff 全生命周期 source/target fence 与 target 本地 UI gate

Handoff start 成功创建事务时，服务端同时建立：

```text
source Context fence
target exact pair/standby Context fence
```

持续到 Handoff completed/failed/cancelled/timedOut。

#### preparing / ready

允许：

```text
source passive playing progress
positionMs/positionSampledAtServerMs/serverUpdatedAtMs 正常更新
只读 status/subscribe
playback.ready
handoff cancel
```

普通 controller 对 source 的以下操作返回 `conflict`：

```text
player.*
queue.playItem
queue.context.sync
playback.context.close
第二个 Handoff
Broadcast start
会改变 authority/binding 的 ensure
```

source 本地真实操作已经发生时不能简单拒绝。`localUser`、automatic queue terminal、自然切歌 queue sync，或 passive actual fact 显示 state/track/rate 发生实质变化时，服务端必须在同一 Context 临界区：

```text
先把 Handoff 终止为 failed/source_changed
向 target 推 cancel/status，失效 scheduled execution
再按正常 Core 规则接受并提交 source 的真实 mutation/fact
```

#### committing

authority 仍是 source，但所有新的普通远程控制和 binding mutation都被 source fence 阻止。

source 本地用户操作、自然结束或真实 state/track/rate 改变时，本地实际事实优先：先 source_changed 终止 Handoff，target 取消 timer/lease且已起播则暂停，再提交 source actual mutation。

#### target standby 和本地 UI

从 start/prepare 到 terminal，target standby Context 的 queue/player/update/close/prepare/Handoff/Broadcast/Follow mutation全部被阻止。仅允许 list/status/subscribe 和不创建、不初始化、不重绑、不改 snapshot 的纯 no-op ensure。任何 target deviceSession/standby binding 变化使 Handoff failed/source_changed 或 target_disconnected。

Flutter target 在 `preparing|ready|committing` 期间必须禁用本机：

```text
play/pause/seek/next/prev
queue mutation
新的 Follow/Broadcast/Handoff
```

只保留设备音量以及 Handoff cancel/failure cleanup。服务端 fence 不能替代客户端对本机 UI 和 AudioPlayerService 的执行门禁。

### 3.9 Handoff source revalidation、provisional N+1 与固定 cursor

target ready 后、commit 前重新读取 source actual state。

以下是正常 playing 时间线推进，不触发 `source_changed`：

```text
positionMs 正常前进
positionSampledAtServerMs 更新
serverUpdatedAtMs 更新
```

服务端使用最新 fresh sample 重新投影 commit position。

以下变化终止为 `failed/errorCode:"source_changed"`：

```text
trackId 改变
state 不再 playing
playbackRate 改变
authority client/device binding 改变
sourceEpoch 改变
sourceVersion 改变
sourceQueueRevision 改变
prepare.controlVersion 对应的 source controlVersion 改变
appliedControlVersion 不再等于 source controlVersion
出现新的 pending control transaction
```

重新读取时 `serverUpdatedAtMs` 与 `positionSampledAtServerMs` 任一年龄不得超过 2000ms，source 当前物理连接必须继续满足 clock gate。

不增加与现有字段重复的 `sourceControlVersion`。

```text
prepare.controlVersion = N
commit.controlVersion = N + 1
complete.appliedControlVersion = N + 1
```

prepare 增加：

```text
playbackRate
positionSampledAtServerMs
sourceEpoch
sourceVersion
sourceQueueRevision
```

commit 增加：

```text
serverTimeMs
playbackRate
```

并满足：

```text
effectiveAtServerMs - serverTimeMs >= 250
```

Handoff commit 的 `N+1` 是 `(playbackContextId, epoch, handoffId)` 作用域的 provisional version，不是 canonical controlVersion，只有 complete 原子提交时才进入 Context。

complete 成功后的 Context cursor 固定为：

```text
epoch = old epoch + 1
version = old version + 1
queueRevision = old queueRevision
controlVersion = commit.controlVersion
```

Handoff failed/cancelled/timedOut 且 target execution lease 已失效后，Context 仍停留在 canonical N；下一次普通 Context mutation可以再次从 N 分配 N+1。所有旧 target ready/complete/cancel 必须同时匹配 handoffId、exact pair、physical Socket 和非终态 Handoff，不能借数字复用穿透。

### 3.10 Handoff wire shape、expected-position proof、clientSeq 和 exact pair terminal

`playback.handoff.start` 请求增加必需：

```text
targetDeviceSessionId
```

目标冻结为 exact `(targetClientId, targetDeviceSessionId)`。

`playback.handoff.complete` 改为：

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

complete 必须来自 frozen target 的当前 exact Socket，并验证：

```text
deviceSessionId 精确匹配 frozen target
state == playing
queueIndex/trackId 精确匹配 prepare/commit target
playbackRate 精确等于 commit target
appliedControlVersion == commit.controlVersion
positionMs >= 0，且已知媒体时长时不超过 duration
positionSampledAtServerMs <= serverNowMs + 1000
serverNowMs - positionSampledAtServerMs <= 2000
positionSampledAtServerMs >= effectiveAtServerMs
positionSampledAtServerMs - effectiveAtServerMs <= 1000
target 当前 nonce clock gate 仍有效
```

服务端计算：

```text
expectedPositionMs =
  commit.positionMs
  + (positionSampledAtServerMs - effectiveAtServerMs) * playbackRate
```

已知 duration 时对 expectedPositionMs 做 clamp。r18 固定：

```text
HANDOFF_COMPLETE_POSITION_TOLERANCE_MS = 1000
abs(reportedPositionMs - expectedPositionMs) <= 1000
```

超出位置容差、超过 1000ms late policy、clock 失效、track/rate 不匹配或执行失败时，不得发送/接受成功 complete，必须走 commit failure report，authority 保持 source。

目标必须从实际确认 playing 的第一份音频快照立即构造 complete。

`playback.handoff.complete.clientSeq` 复用 target 后续普通 `playback.update` 的序号作用域：

```text
(playbackContextId, targetClientId, connectionNonce, connectionEpoch)
```

complete 消耗该序号并写入完整 target DevicePlaybackState；后续 target passive update 必须使用更高序号。

```text
相同 clientSeq + 相同 complete 内容 -> 幂等重放 completed status + current Context status
相同 clientSeq + 不同内容 -> client_sequence_conflict
clientSeq 倒退 -> client_sequence_conflict
```

completed status 和 release 必须完整表达 exact 新 authority pair：

```text
newAuthorityClientId
newAuthorityDeviceSessionId
```

旧 source 收到 release、completed status、Context status 或 bindings.changed 任一权威事实，都必须停止旧 authority audio lease。

扩展 `playback.handoff.cancel` 的 target failure 条件 shape：

```text
playbackContextId
handoffId
reason:"commit_failed"
errorCode:"commit_failed"
errorMessage:O
```

只有 frozen target 当前 Socket 在 committing 中可发送。服务端结算为 `failed/commit_failed`。普通 controller/source cancel 仍为 cancelled；target failure 与 hard timeout竞争时只允许一个终态。

### 3.11 restorePending 的 action-aware 清理矩阵

`restorePending:true` 不是无条件拒绝所有 Handoff/Follow/Core event。

必须阻止：

```text
playback.context.ensure
playback.context.prepare
playback.context.close
queue.context.sync
queue.playItem
player.*
playback.update
playback.handoff.start
playback.handoff.complete
playback.ready(ready:true)
playback.context.prepared(ready:true)
follow.start
broadcast.start
任何 authority/device binding mutation
```

必须允许只用于清理、无新执行副作用的请求：

```text
playback.ready(
  ready:false,
  errorCode:"restore_in_progress"
)
playback.context.prepared(
  ready:false,
  errorCode:"restore_in_progress"
)
playback.handoff.cancel
follow.stop
terminal broadcast.feedback
broadcast.status
playback.context.list/status/subscribe/unsubscribe
device.setVolume / device.volume.update
device.list
system.ping
terminal stop/restore replay
```

negative `playback.ready` 只结算匹配的 raced Handoff prepare；negative `playback.context.prepared` 只结算匹配的 raced Core prepare。两者均不得初始化队列、推进 Context cursor、进入 commit 或清除 restorePending；重复结果幂等重放 canonical failed confirmation。

Core prepared errorCode 集合增加 `restore_in_progress`，仅允许上述 cleanup shape。

`playback.handoff.cancel` 只允许终止匹配非终态 Handoff。`follow.stop` 只释放已存在 relationship/fence。

当请求 source Context 与实际 restore fence 的 suspended Context 不同，`system.error.playbackContextId` 和三个 `current*` cursor 必须描述真正被 fence 占用的 suspended Context。

所有被阻止的写请求返回：

```text
restore_in_progress
retryable:true
playbackContextId
currentVersion
currentQueueRevision
currentControlVersion
```

不得推进 cursor、发送新执行 command、产生 canonical mutation 或 binding invalidation。

### 3.12 用户域资源解析、确定性验证顺序与 volumeState

服务端不得为了选择错误码做全局资源存在性查询。

统一验证顺序：

```text
envelope/schema
authentication
registration/capability
caller role
authenticated-user-scoped resource lookup
lifecycle/overlay/recovery fence
base cursor
mutation
```

规则：

- direct ID action 中，其他用户资源与真正不存在资源使用相同 `not_found`；
- `forbidden` 只表示调用者对当前用户域内可见资源缺少角色或操作权限；
- `playback.context.list` 只返回 user-scoped 结果；
- Handoff target、device.setVolume target 只在当前 user scope 解析，跨用户/不存在均为 `not_found`；
- Broadcast explicit participant 中，跨用户、不存在、离线或不可用目标均使用同一种 `skippedClientIds` 结果，不得区分存在性；
- 最终无 participant 时按既有 `bad_request`/recovery-slot `rate_limited` 规则结算；
- 日志不得输出通过全局查询获得的其他用户资源细节。

`device.list.volumeState` 只向请求连接的：

```text
negotiatedCapabilities.remoteVolumeControl == true
```

时输出。不能再使用“capability shape 包含 remoteVolumeControl 字段”作为条件，因为 strict 固定 10 字段 shape 对所有连接都包含该字段。

### 3.13 Recovery abandon 与 exact pair decommission

管理端 recovery abandon 必须原子执行：

```text
确认 Broadcast 已 terminal
确认 exact pair 仍 restorePending
删除 full/compact recovery obligation
删除 ordinary fence
释放 recovery slot
写 abandoned audit tombstone
写 device-pair decommission tombstone
禁止相同 clientId + deviceSessionId 再次注册继承旧身份
```

同一 stable clientId 以后可以使用新的 `deviceSessionId` 正常注册和 ensure，但不得复用被 decommission 的旧 pair。

只有满足 exact pair decommission，才允许永久停止向旧 pair terminal replay。

## 4. REQ 与验证映射规则

- 保持现有 REQ-001—REQ-067 编号稳定，不做全量重编号；
- 已有规则发生语义修正时，直接修改原 REQ；
- 无法合理归入旧 REQ 的新职责从 REQ-068 开始追加；
- 新增或改变但尚未实现的要求标记为 `Contract defined / implementation pending`，不得提前标记 `Verified`；
- 任何被 D1—D17 或第 3 节机械闭环改变语义的旧 REQ，都必须重新评估；旧测试未覆盖新语义时，从 `Verified` 降为 `Contract defined / implementation pending`；
- 不新建 r19 requirement mapping；
- 继续更新 `docs/verification/emosonic_strict_v2_r18_requirement_mapping.md`；
- mapping 必须分别记录：服务端 schema、服务端状态机、Flutter parser/controller、自动测试和 Android + Windows 真机证据；
- 契约 Frozen 不等于 profile ready；真机证据只决定 implementation readiness。

## 5. 契约优先修改批次

下列路径均相对于仓库根目录。列出的是最低必改集合；每个 Batch 完成后仍必须全仓搜索交叉引用。

## Batch A — Core、控制结算、close 与 queue terminal

### 最低必改文件

- `specs/emosonic_strict_v2_contract/phase-0-foundation/01-overview.md`
- `specs/emosonic_strict_v2_contract/phase-0-foundation/02-transport-registration-and-clock.md`
- `specs/emosonic_strict_v2_contract/phase-0-foundation/03-ack-errors-idempotency-and-cursors.md`
- `specs/emosonic_strict_v2_contract/phase-1-core/04-client-core-actions.md`
- `specs/emosonic_strict_v2_contract/phase-1-core/07-server-device-context-and-status.md`
- `specs/emosonic_strict_v2_contract/phase-1-core/08-server-queue-playback-and-controls.md`
- `specs/emosonic_strict_v2_contract/phase-2-optional-profiles/06b-broadcast-source-context.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/11a-common-and-core-requirements.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/11b-broadcast-requirements.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/11c-security-persistence-and-readiness.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/13-integration-acceptance.md`

### 必须落实

- Core prepare 与 Handoff capability 解耦；
- 普通 routed control 的 `executionTimeoutMs`、lease、watchdog；
- transaction exact requester pair 与 deterministic dependency assignment；
- `playback.control.settled` action、payload、幂等、dependency cascade、收件人和跨连接行为；
- `execution_unknown` 后分配新 server reconciliation version，不伪造旧 applied；
- `playback.context.close(expectedEpoch,baseVersion)`、action-aware `context_closed` cursors 与 tombstone 幂等；
- first-prev、last-next 和 automatic final queue terminal 的精确 cursor；
- user-scoped lookup、volumeState 条件与确定性错误优先级。

### 契约验收条件

- ACK、actual committed/failed、unknown 和 dependency failure 有唯一来源；
- unknown 后能够最终消除 canonical/applied 永久分叉，同时保留旧 unknown 审计终态；
- 没有订阅 Context 的原请求 controller 仍可在同一物理连接收到 settlement；
- settlement 可精确归属 requesting client/device pair；
- close 不能穿透 authority/Context 变化，重复 close 仍幂等；
- 最后一首结束只产生一次 canonical mutation，Follow/Broadcast 各派生一次。

## Batch B — Follow 音频、能力、持久安全租约与 crash-safe cleanup

### 最低必改文件

- `specs/emosonic_strict_v2_contract/phase-0-foundation/01-overview.md`
- `specs/emosonic_strict_v2_contract/phase-0-foundation/02-transport-registration-and-clock.md`
- `specs/emosonic_strict_v2_contract/phase-0-foundation/03-ack-errors-idempotency-and-cursors.md`
- `specs/emosonic_strict_v2_contract/phase-1-core/04-client-core-actions.md`
- `specs/emosonic_strict_v2_contract/phase-1-core/07-server-device-context-and-status.md`
- `specs/emosonic_strict_v2_contract/phase-1-core/08-server-queue-playback-and-controls.md`
- `specs/emosonic_strict_v2_contract/phase-2-optional-profiles/05-client-follow-and-handoff.md`
- `specs/emosonic_strict_v2_contract/phase-2-optional-profiles/06a-client-broadcast-actions.md`
- `specs/emosonic_strict_v2_contract/phase-2-optional-profiles/06c-broadcast-feedback-and-recovery.md`
- `specs/emosonic_strict_v2_contract/phase-2-optional-profiles/06d-flutter-broadcast-roles-and-terminal.md`
- `specs/emosonic_strict_v2_contract/phase-2-optional-profiles/09-server-handoff.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/11a-common-and-core-requirements.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/11b-broadcast-requirements.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/11c-security-persistence-and-readiness.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/13-integration-acceptance.md`

### Wire shape

保持请求：

```text
follow.start: sourcePlaybackContextId + deviceSessionId
follow.stop: sourcePlaybackContextId
```

不新增 Follow mirror push 或 feedback action。

### 必须落实

- `supportsFollow:true` 的复合能力，`effectiveAtPlayback` 协商必须覆盖 Follow profile；
- follower/source clock gate 与 source state-specific eligibility；
- idle start 拒绝、自 Follow 拒绝，active sourceIdle 保持 relationship；
- settled entry：applied==control、无 pending/prepare/Handoff/overlay，Flutter local lane idle；
- exact pair + suspended Context relationship；
- Follow occupancy fence 和 mode eligibility；
- playing-only stale；paused/stopped 稳定事实；
- Flutter `FollowRecoveryRecord`、resume/stop intent；
- server `FollowSafetyLease`、restart fence recovery 与 cleanupRequired；
- recovery record 写失败时立即 stop/disconnect，不进入 overlay；
- restore-before-stop，ACK 后释放 fence；
- source recovery deadline 与 follower reconnect grace 两个独立 timer；
- app restart 只 cleanup、不自动恢复 Follow；
- source Handoff 后 relationship 继续绑定 Context ID。

### 契约验收条件

- Follow 是实际音频执行且不会污染任何正常 Context；
- 已送达旧命令不能在 Follow start 后晚执行；
- 其他 controller 不能穿透 suspended Context fence；
- 服务端重启不会短暂丢失 Follow 安全门禁；
- stop ACK 丢失、Socket 重连和 app restart 都不会错误重新 Follow；
- recovery record 写失败或本地恢复失败不会提前释放 fence；
- idle/self-follow 行为唯一且可测试。

## Batch C — Handoff 能力、全生命周期 fence 与完整执行证明

### 最低必改文件

- `specs/emosonic_strict_v2_contract/phase-0-foundation/01-overview.md`
- `specs/emosonic_strict_v2_contract/phase-0-foundation/02-transport-registration-and-clock.md`
- `specs/emosonic_strict_v2_contract/phase-0-foundation/03-ack-errors-idempotency-and-cursors.md`
- `specs/emosonic_strict_v2_contract/phase-1-core/04-client-core-actions.md`
- `specs/emosonic_strict_v2_contract/phase-1-core/07-server-device-context-and-status.md`
- `specs/emosonic_strict_v2_contract/phase-1-core/08-server-queue-playback-and-controls.md`
- `specs/emosonic_strict_v2_contract/phase-2-optional-profiles/05-client-follow-and-handoff.md`
- `specs/emosonic_strict_v2_contract/phase-2-optional-profiles/09-server-handoff.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/11a-common-and-core-requirements.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/11c-security-persistence-and-readiness.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/13-integration-acceptance.md`

### 必须落实

- source/target composite capability 与双方 clock gate；
- start exact target pair 和 playing/settled/fresh gate；
- source 与 target standby fence 覆盖 preparing/ready/committing 全生命周期；
- target 本机 UI/AudioPlayerService gate；
- source local actual mutation 先 source_changed 终止 Handoff，再提交实际事实；
- prepare/commit/complete N/N+1 关系、provisional reuse 和固定 cursor；
- normal position/sample 前进不触发 source_changed；
- complete 的完整 fact、sample/late/clock/media/expected-position 校验；
- 1000ms complete position tolerance；
- shared playback `clientSeq` scope；
- completed status/release 的 exact new authority pair；
- commit_failed 主动报告入口；
- duplicate start/ready/complete 和可靠 enqueue failure；
- active Handoff 时 close conflict。

### 契约验收条件

- commit 到 complete 之间没有普通控制穿透窗口；
- target standby 和本机 UI 不会被其他操作修改；
- late/mismatched/wrong-position target 不能伪造成功 complete；
- provisional N+1 失败后可安全复用且旧消息不能穿透；
- release 丢失时旧 source 仍由 status/binding 停止。

## Batch D — Broadcast restore action-aware gate、软同步与 decommission

### 最低必改文件

- `specs/emosonic_strict_v2_contract/phase-0-foundation/01-overview.md`
- `specs/emosonic_strict_v2_contract/phase-0-foundation/03-ack-errors-idempotency-and-cursors.md`
- `specs/emosonic_strict_v2_contract/phase-1-core/04-client-core-actions.md`
- `specs/emosonic_strict_v2_contract/phase-1-core/07-server-device-context-and-status.md`
- `specs/emosonic_strict_v2_contract/phase-1-core/08-server-queue-playback-and-controls.md`
- `specs/emosonic_strict_v2_contract/phase-2-optional-profiles/05-client-follow-and-handoff.md`
- `specs/emosonic_strict_v2_contract/phase-2-optional-profiles/06a-client-broadcast-actions.md`
- `specs/emosonic_strict_v2_contract/phase-2-optional-profiles/06c-broadcast-feedback-and-recovery.md`
- `specs/emosonic_strict_v2_contract/phase-2-optional-profiles/06d-flutter-broadcast-roles-and-terminal.md`
- `specs/emosonic_strict_v2_contract/phase-2-optional-profiles/09-server-handoff.md`
- `specs/emosonic_strict_v2_contract/phase-2-optional-profiles/10-server-broadcast.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/11a-common-and-core-requirements.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/11b-broadcast-requirements.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/11c-security-persistence-and-readiness.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/13-integration-acceptance.md`

### 必须落实

- 删除“服务端放行、Flutter 排队”的旧 restore 规则；
- `restore_in_progress` action-aware 阻止矩阵；
- Handoff negative ready 和 Core negative prepared 清理例外；
- follow.start 禁止、follow.stop 允许；
- device volume/list/ping 不受 Context restore fence 影响；
- error cursor 指向真正 suspended Context；
- Broadcast `applied` 仅表示 revision target 已应用；
- membership 固定到 terminal；
- terminal delivery gate；
- recovery abandon + exact pair decommission。

### 契约验收条件

- raced Handoff 和 Core prepare 都可立即 negative cleanup，不等待 timeout；
- restore gate 不阻止设备级音量、时钟和读取；
- abandon 后旧 pair 注册失败，新 deviceSession 可建立新生命周期。

## Batch E — r18 权威入口、mapping 与冻结收口

### 最低必改文件

- `specs/emosonic_strict_v2_socketio_server_contract.md`
- 全部 19 个权威分卷页首
- `docs/verification/emosonic_strict_v2_r18_requirement_mapping.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/13-integration-acceptance.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/14-authority-and-deployment-evidence.md`

### 必须落实

- 权威身份保持 r18 / `2.8.0`，修订更新为 `2026-08-01-r18`；
- 使用三项状态：Approved、契约 Frozen、实现 pending；
- 入口摘要覆盖 Follow safety lease/recovery、control settlement/reconciliation、safe close、queue terminal、Handoff full proof、restore gate、volumeState 与 user-scoped security；
- 全部 19 个分卷页首同步；
- mapping 对语义变化的旧 Verified 重新评估并降级；
- 写入 pre-freeze 例外、成组升级、冻结后 r19/errata 纪律；
- 契约冻结不以真机证据为前置；Android + Windows 真机只决定 implementation ready。

## 6. 契约完成后的代码实施顺序

### Phase 1 — capability、schema validator 与双方 fixtures

服务端：

```text
supysonic/emo/strict_v2_contract.py
supysonic/emo/strict_v2_readiness.py
```

Flutter：

```text
lib/services/emo_action_contract_policy.dart
lib/services/emo_strict_v2_models.dart
StrictV2CapabilityPolicy 所在文件
test/fixtures/emo_protocol/strict_v2/...
```

先同步：

- `supportsFollow` / `effectiveAtPlayback` 复合协商；
- close request 与 context_closed cursors；
- settlement `requestingDeviceSessionId`；
- Handoff start/prepare/commit/complete/cancel/status/release；
- expected-position proof 与 clientSeq；
- Core negative prepared；
- restore errors；
- `device.list.volumeState` 条件。

### Phase 2 — Core settlement、dependency、reconciliation、safe close 与 queue terminal

服务端：`ws.py`、`ws_store.py`。

Flutter：control transaction coordinator、execution lease、command lane、audio end callback、close sender。

### Phase 3 — Follow

服务端：persistent `FollowSafetyLease`/fence/reconnect cleanup store、mode eligibility、restart recovery。

Flutter：`FollowRecoveryRecord`、resume/stop intent、restore-before-stop、playing stale、sourceIdle、app restart cleanup。

### Phase 4 — Handoff

服务端：full lifecycle fences、exact pair、source revalidation、provisional version、complete transaction、position proof、commit failure。

Flutter：new schema、target local UI gate、complete proof/clientSeq、source fallback stop、target terminal lease invalidation。

### Phase 5 — Broadcast restore 与管理清理

服务端：restore action matrix、negative cleanup、terminal delivery gate、recovery abandon/decommission。

Flutter：删除恢复期间普通命令队列，处理 restore errors 和 raced prepare cleanup。

## 7. 测试计划

### Core

- Handoff profile off / `playbackPrepare:false` 时 Core prepare 仍成功；
- executionTimeout 默认/配置、Windows lease 和 server +2000ms watchdog；
- deterministic `dependsOnControlVersion` 分配；
- committed、failed、dependency_failed、execution_unknown；
- settlement 包含 requesting exact pair；
- 原请求者未 subscribe 仍收到同连接 settlement；
- disconnect 后不向 replacement 自动补历史 settlement；
- unknown actual matches/differs canonical 时均分配新的 reconciliation version；
- 原 unknown transaction 不被改成 committed；
- late remote result 不能改写 unknown terminal；
- close stale epoch/version、active fence、重复 close tombstone与 context_closed cursors；
- first-prev、last-next 的精确 cursor；
- natural last-end 只产生一次 version+1，queue/control/epoch 不变；
- volumeState 只向 negotiated remoteVolumeControl=true 输出。

### Follow

- Follow profile 开启时 `effectiveAtPlayback` 可以正确协商；
- capability 缺任一 can/effective-at/rate 承诺时不协商或 start fail-closed；
- follower/source clock gate 不满足时 start 无副作用失败；
- playing source 使用 2 秒 fresh gate，paused/stopped source 可稳定开始；
- idle source start 返回 queue_required；
- self-follow 返回 conflict；
- active source 转 idle 时清空 mirror、保持 relationship，恢复 queue 后继续；
- pending control、active prepare 或 local execution lane 未空闲时不能 start；
- 实际应用 source queue/play/pause/seek/natural transition；
- playing 3 秒 stale；paused/stopped 长时间稳定不退出；
- Follow fence 阻止其他 controller；
- FollowSafetyLease 在服务端重启后恢复 fence；
- recovery record 写失败时不触碰音频并立即 cleanup；
- stop 先恢复后释放 fence；
- stop ACK 丢失重连只重试 stop，不重新 start；
- app restart 使用 cleanup marker，不自动 Follow；
- restore 失败保持非播放和 fence；
- source Handoff 后继续 Follow；
- source 同时有 Follow 和 Broadcast 时互不污染。

### Handoff

- source/target capability 和双方 clock gate；
- idle/paused/stopped/stale/unsettled source fail-fast；
- exact target deviceSession replacement；
- source/target standby fences覆盖全部阶段；
- target 本机 transport/queue/mode UI gate；
- prepare 期间仅 position/sample 更新继续；track/state/rate/cursor/binding变化 source_changed；
- committing 期间普通 remote control 被拒绝；
- source localUser/natural-end 优先终止 Handoff 后提交 actual fact；
- prepare N、provisional commit N+1、complete N+1；
- failed provisional N+1 可被下一普通 mutation安全复用；
- 1.5x rate 和最新 sample projection；
- complete sample/late/clock/media bounds；
- reported position 在 expectedPosition ±1000ms 内才可 complete；
- complete clientSeq 与后续 passive 连续；
- completed status/release 包含 exact new authority pair；
- duplicate start/ready/complete；
- prepare/commit enqueue failure；
- commit_failed 主动报告；
- release 丢失但 status/binding 仍停止 source；
- active Handoff close conflict。

### Broadcast restore

- 每个受阻写 action 返回 `restore_in_progress`；
- Handoff ready:false、Core prepared:false、handoff.cancel、follow.stop 和 terminal feedback 可清理；
- device volume/list/ping/read 不受阻；
- error cursor 指向真正 suspended Context；
- terminal feedback 清 fence 后新 requestId 可执行；
- applied 只证明 revision target；
- terminal enqueue failure 断开并重连 replay；
- abandon 原子清 fence/record/slot并 decommission exact pair；
- old pair 注册失败，新 deviceSession 正常。

### Security

- 跨用户与不存在 Context/target 得到不可区分结果；
- 不执行全局存在性查询来选择错误码；
- Broadcast explicit 跨用户与不存在目标均进入同类 skipped 结果；
- 验证顺序和错误优先级确定。

## 8. 全量一致性审计

契约修改后必须机械搜索：

1. `r19` / `2.9.0`：当前 normative 身份不得误写；只允许未来变更纪律；
2. `2026-07-23-r18`：当前页首统一更新为 `2026-08-01-r18`；
3. `Frozen implementation baseline`：不得残留；
4. `supportsFollow` / `effectiveAtPlayback`：Follow 复合协商必须完整，不得只写 player+canPlay；
5. Follow source：playing、paused/stopped、idle 和 self-follow 必须有唯一门禁；
6. Follow lifecycle：必须有 settled entry、fence、FollowRecoveryRecord、persistent FollowSafetyLease、restart cleanup、resume/stop intent、两个独立 timer；
7. Handoff fence：必须覆盖 preparing/ready/committing、target standby 和 target 本机 UI；
8. Handoff shape：不得残留仅 targetClientId start、仅 positionMs complete、重复 sourceControlVersion 或仅 clientId terminal identity；
9. Handoff complete：必须有 sample/late/clock/media/expected-position proof、1000ms tolerance 和 shared clientSeq；
10. Handoff provisional N+1：失败后的复用和旧消息拒绝规则必须存在；
11. restorePending：不得残留服务端放行/Flutter 排队；不得误删 Handoff/Core negative cleanup；
12. control settlement：必须有 requester exact pair、dependency assignment、timeout、new-version reconciliation 和 late result规则；
13. appliedControlVersion：不得把原 execution_unknown command 伪装为成功 applied；
14. close：必须有 expectedEpoch/baseVersion、tombstone幂等和 context_closed final cursors；
15. queue boundary：first-prev/last-next/natural-end cursor 一致；
16. security：跨用户与不存在不可区分，不得全局探测；
17. `volumeState`：必须检查 negotiated remoteVolumeControl==true；
18. recovery abandon：必须与 exact pair decommission绑定；
19. mapping：所有旧证据失效的 Verified 已降级；
20. Markdown 链接、章节引用、REQ 引用无断链；
21. 运行 `git diff --check`。

契约阶段不得为了让旧代码测试通过而修改 Python、Dart、fixtures 或测试。

## 9. 纯文档提交顺序

```text
1. spec: close r18 core settlement and lifecycle gaps
2. spec: define r18 follow capability safety lease and recovery
3. spec: harden r18 handoff lifecycle and execution proof
4. spec: finalize r18 broadcast restore and security gates
5. spec: freeze r18 contract authority and mapping
```

## 10. r18 契约完成标准

只有以下条件全部满足，才能宣布 r18 契约定稿并冻结：

- D1—D17 全部进入权威分卷和 REQ/acceptance；
- 第 3 节全部机械闭环进入权威分卷，不只停留在本计划；
- 每个 action 的请求、结算、错误、幂等、断线、重启和超时有唯一解释；
- Follow 的能力、source state、self gate、settled entry、fence、恢复记录、安全租约、restart cleanup、resume/stop intent 和 timer 无歧义；
- Handoff source/target fence覆盖完整生命周期，target 本机 UI 受控，complete具有可验证的位置和实际事实；
- `execution_unknown` 后通过新的 reconciliation controlVersion 收敛，且不伪造旧 command applied；
- dependency assignment 和 cascade 完全确定；
- settlement 可以精确归属 requesting client/device pair；
- close 具备并发前置条件、action-aware error cursors 且跨 requestId 幂等；
- 最后一首自然结束有唯一 canonical mutation；
- restorePending 无相反规则，Handoff/Core negative cleanup 不被阻止；
- user-scoped security、volumeState 条件和错误优先级无存在性侧信道；
- recovery abandon 与 exact pair decommission 原子绑定；
- r18 mapping 对全部新/改要求有实施状态，失效旧证据已降级；
- 全部 19 个分卷页首、入口、修订和版本一致；
- 权威入口写明 pre-freeze 例外、成组升级和冻结后 r19/errata 纪律；
- 最终机械审计与 `git diff --check` 通过。

冻结后进入“实现 r18”阶段，不再重新讨论 D1—D17。未来真正改变冻结行为的新功能进入 r19。
