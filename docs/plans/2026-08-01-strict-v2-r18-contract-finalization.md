# Strict-v2 r18 契约闭合、定稿与冻结计划

> 日期：2026-08-01
> 目标分支：`agent/strict-v2-r5-server-adaptation`
> 当前权威基线：strict-v2 `2.8.0` / r18
> 最终目标身份：`2026-08-01-r18` / `2.8.0`
> 本文性质：实施计划与决策记录，不替代 `specs/emosonic_strict_v2_socketio_server_contract.md` 及其 19 个权威分卷。

## 1. 版本身份、冻结状态与工作边界

本轮不是创建 r19，也不升级 `protocolVersion`。当前 strict-v2 `2.8.0/r18` 尚未正式发布，本轮属于 r18 在首次冻结前的最终闭合、定稿和一致性修正。

本轮完成并通过最终一致性审计后，权威契约必须明确区分“契约已经冻结”与“实现尚待完成”：

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

- 不设计或修改协议版本协商。
- 不修改调试服务中 profile implementation readiness 默认 `true` 的便利行为。
- 不以 conformance manifest 的发布状态阻止本地 debug 路径运行。
- 不新增 legacy/session 兼容 shape。
- 不增加 Broadcast 动态 participant membership、`broadcast.leave`、add/remove participant。
- 不支持 shuffle、repeat-one、repeat-all 或 `queueSongIds` 内重复 songId。
- 不增加 `follow.feedback` 或新的 Follow mirror action。

本轮目标是让以下五层使用同一套最终 r18 定义：

```text
权威 specs
  -> 服务端 strict_v2_contract.py
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

选择 A。`restorePending:true` 期间服务端冻结 suspended Context 的写操作，统一返回 `restore_in_progress`；Flutter 不再排队普通 Context command。清理型 negative confirmation 与 cancel 按第 4.4 节的 action-aware 例外继续允许。

### D8 — Broadcast synchronization promise

选择 A。Broadcast 为软同步。`syncStatus:"applied"` 只表示 participant 已应用该 revision 的目标，不证明持续 drift 小于固定毫秒阈值。

### D9 — Close during Handoff

选择 A。存在非终态 Handoff 时 `playback.context.close` 返回 `conflict`；必须先 cancel，再 close。

### D10 — Queue boundary

- `queueSongIds` 不允许重复。
- 不支持 shuffle、repeat-one、repeat-all。
- 第一首 `prev`：重播第一首，position=0，state=playing。
- 最后一首 `next`：不循环，保留最后 index，position=0，state=stopped。
- 最后一首自然结束：保留最后 index，position=0，state=stopped，并通过第 3.2 节唯一的 automatic queue-terminal canonical mutation 收敛主 Context。

### D11 — Recovery abandon

选择 A。增加管理端/调试 CLI 的 recovery abandon；不加入 Flutter 公共 strict realtime action。abandon 必须与被冻结 pair 的 device decommission 原子绑定，不能让同一旧 `deviceSessionId` 以后重新注册而永远失去 terminal recovery。

### D12 — Follow disconnect/reconnect

选择 A。

- 只有最新 source state 为 `playing` 时，才使用 3 秒新鲜度门槛；`serverUpdatedAtMs` 或 `positionSampledAtServerMs` 任一超过 3 秒，follower 暂停并停止位置外推。
- 最新 source state 为 `paused|stopped|idle` 时，该状态是稳定事实，不能只因 3 秒没有 heartbeat 而判定 stale。
- source authority 明确离线、playing fact 过期或重新 `follow.start`/status 失败时，进入最多 30 秒恢复窗口。
- 同一应用进程内 Socket 重连后重新 `follow.start`、subscribe/status 并继续。
- 30 秒仍无法恢复或 source Context closed 时退出 Follow，并恢复本机任务；closed 立即结束，不等待 30 秒。
- 应用进程重启后不自动恢复 Follow。

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

选择 A。`playback.control.settled` 发给全部当前 Context subscribers，并额外发给仍在线的原 authority 物理连接。非请求 controller 只更新全局 cursor/UI，不弹出本地操作错误。

## 3. 冻结前必须补齐的机械闭环规则

本节不改变 D1—D17 的产品方向，只消除时间线、清理、序号和恢复上的协议空洞。

### 3.1 Handoff revalidation 允许正常位置前进

target ready 后、commit 前必须重新读取 source 当前实际状态。

下列变化是正常 playing 时间线推进，不能触发 `source_changed`：

```text
positionMs 正常前进
positionSampledAtServerMs 更新
serverUpdatedAtMs 更新
```

服务端必须使用最新 fresh sample 重新投影 commit 的 `positionMs`。

只有以下变化才终止为 `failed/errorCode:"source_changed"`：

```text
trackId 改变
state 不再是 playing
playbackRate 改变
authority client/device binding 改变
sourceEpoch 改变
sourceVersion 改变
sourceQueueRevision 改变
prepare.controlVersion 对应的 source controlVersion 改变
appliedControlVersion 不再等于 source controlVersion
出现新的 pending control transaction
```

重新读取时 `serverUpdatedAtMs` 与 `positionSampledAtServerMs` 任一年龄不得超过 2000ms，且 source 当前物理连接必须继续满足 effective-at clock gate。

### 3.2 最后一首自然结束的唯一 canonical mutation

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

### 3.3 Follow occupancy fence 与安全退出顺序

`follow.start` 必须按 follower 当前注册的 exact client/device pair 解析其唯一 active authority Context，并记录：

```text
suspendedPlaybackContextId
followerClientId
followerDeviceSessionId
sourcePlaybackContextId
```

没有唯一 active Context、pair 已被其他 overlay/fence 占用或 source pair 自身处于 overlay 时，`follow.start` 返回带相关 Context cursors 的 `conflict`，不得只建立订阅。

Follow relationship active 或 reconnect-grace 期间，服务端对 follower suspended Context 建立 pair-level 写屏障。

阻止：

```text
player.*
queue.playItem
queue.context.sync
playback.context.prepare
playback.context.prepared
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
只读 device/list 与 clock action
```

被阻止的 suspended Context mutation 返回 `conflict`，携带 suspended Context 的完整 canonical cursors，不产生 mutation、command 或 binding invalidation。

为了避免 stop 后本地仍在恢复而服务端已经放行远程命令，Flutter 必须按以下顺序停止 Follow：

1. 本地进入 `restoringFollowContext`，忽略后续 source mirror，取消 drift timer/seek/rate correction；
2. 恢复进入 Follow 前的原任务；快照不可用时读取一次 suspended Context status 并按服务端当前状态恢复；
3. 恢复成功后发送 `follow.stop`；
4. 服务端 ACK `follow.stop` 并原子释放 relationship、subscription 与 Follow fence；
5. fence 释放后，Flutter 才为自己的正常 Context 发送普通 `playback.update`。

source Context closed 时，服务端推送 closed 并把 relationship 标记为 terminating，但在 follower 在线时保持 fence，直到 follower 完成恢复并发送幂等 `follow.stop`。

follower Socket 短暂断线时，服务端不立即删除 relationship/fence，而是进入最多 30 秒 reconnect grace：

- 相同 pair 在 grace 内重新注册并重发相同 `follow.start`，幂等恢复 active；
- app restart 不重发 start 时，grace 到期释放 relationship/fence；此时 follower 已离线，不存在远程命令与本地恢复并发；
- grace 到期后旧 source relationship 不得被新连接自动继承。

### 3.4 restorePending 的 action-aware 清理例外

`restorePending:true` 不是无条件拒绝所有 Handoff/Follow event。

必须阻止：

```text
playback.context.ensure
playback.context.prepare
playback.context.prepared
playback.context.close
queue.context.sync
queue.playItem
player.*
playback.update
playback.handoff.start
playback.handoff.complete
playback.ready(ready:true)
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
playback.handoff.cancel
follow.stop
terminal broadcast.feedback
broadcast.status
playback.context.list/status/subscribe/unsubscribe
terminal stop/restore replay
```

negative `playback.ready` 只允许结算匹配的 raced prepare，不得进入 commit。`playback.handoff.cancel` 只允许终止匹配的非终态 Handoff。

当请求中的 source Context 与实际 restore fence 的 suspended Context 不同，`system.error.playbackContextId` 和三个 `current*` cursor 必须描述真正被 fence 占用的 suspended Context，而不是复制请求中的另一个 Context ID。

### 3.5 Handoff prepare、commit 与 complete 的版本关系

不增加与现有字段重复的 `sourceControlVersion`。

Handoff prepare 使用：

```text
controlVersion = N
sourceEpoch
sourceVersion
sourceQueueRevision
positionSampledAtServerMs
playbackRate
```

其中 `controlVersion` 就是 source baseline control version。

Handoff commit 固定使用：

```text
controlVersion = N + 1
effectiveAtServerMs
serverTimeMs
positionMs（由最新 fresh source sample 投影）
playbackRate
```

Handoff complete 必须满足：

```text
appliedControlVersion = N + 1
```

complete 原子提交后的 Context cursor 固定为：

```text
epoch = old epoch + 1
version = old version + 1
queueRevision = old queueRevision
controlVersion = commit.controlVersion
```

不得只写含义不明确的“推进 cursor”。

### 3.6 Handoff complete clientSeq 作用域

`playback.handoff.complete.clientSeq` 复用 target 后续普通 `playback.update` 的序号作用域：

```text
(playbackContextId, targetClientId, connectionNonce, connectionEpoch)
```

complete 消耗该 `clientSeq`，并把完整 actual fact 保存为 target DevicePlaybackState。后续 target passive `playback.update` 必须使用更高序号。

```text
相同 clientSeq + 相同 complete 内容 -> 幂等重放 completed status + current Context status
相同 clientSeq + 不同内容 -> client_sequence_conflict
clientSeq 倒退 -> client_sequence_conflict
```

该规则必须同时进入公共 clientSeq 章节、Handoff 请求、服务端 complete、EARS 和 acceptance。

### 3.7 commit_failed 的报告通道

`commit_failed` 不能只作为无入口的标准错误码。

扩展客户端 `playback.handoff.cancel` 请求的条件 shape：

普通 controller/source 取消：

```text
playbackContextId
handoffId
reason:O
```

冻结 target 在 committing 中实际起播失败：

```text
playbackContextId
handoffId
reason:"commit_failed"
errorCode:"commit_failed"
errorMessage:O
```

服务端必须验证发送者是该 Handoff 冻结 target 的当前 exact pair/Socket，并把 Handoff 结算为：

```text
status:"failed"
errorCode:"commit_failed"
```

普通 cancel 仍进入 `cancelled`。target 失败报告与 5 秒 hard timeout 竞争时只能有一个终态，迟到结果幂等重放当前 terminal status。

### 3.8 executionTimeoutMs 的适用范围与 deadline

`executionTimeoutMs` 必须出现在所有普通 server-routed：

```text
queue.playItem
player.play
player.pause
player.seek
player.next
player.prev
```

包括由 active Broadcast source control 派生的普通 source command。

不适用于：

```text
Handoff commit player.play
device.setVolume
```

部署默认值为 `15000`ms；服务端可以使用正整数配置值，但必须把本次实际值写入 routed command。

```text
Windows execution lease deadline = 收到命令后 executionTimeoutMs
服务端 watchdogDeadlineAtMs = acceptedAtMs + executionTimeoutMs + 2000
```

watchdog 到期只能生成 `playback.control.settled(errorCode:"execution_unknown")`，不得伪造 authority failed update。

### 3.9 settlement 跨连接行为

`playback.control.settled` 持久化用于事务终态、幂等和审计，但 r18 不向 replacement controller 或 replacement authority Socket 自动重放历史 settlement。

- settlement 产生时发送给当时全部 Context subscribers；
- 原 authority 仍是同一物理连接时额外发送给它；
- 原请求 controller 已断线时，不向其后来的 replacement Socket补发历史 event；
- controller 断线时本地未决 UI 立即转为 unknown；
- 重连后通过 `playback.context.list -> subscribe -> status` 收敛 canonical/applied 状态；
- r18 不新增 control transaction query action。

### 3.10 recovery abandon 必须同时 decommission exact pair

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

只有满足上述 decommission 条件，才允许“永远不再向旧 pair terminal replay”。

## 4. REQ 与验证映射规则

- 保持现有 REQ-001—REQ-067 编号稳定，不做全量重编号。
- 已有规则发生语义修正时，直接修改原 REQ。
- 无法合理归入旧 REQ 的新职责从 REQ-068 开始追加。
- 新增或改变但尚未实现的要求标记为 `Contract defined / implementation pending`，不得提前标记 `Verified`。
- 任何被 D1—D17 或第 3 节机械闭环改变语义的旧 REQ，都必须重新评估；旧测试未覆盖新语义时，必须从 `Verified` 降为 `Contract defined / implementation pending`。
- 不新建 r19 requirement mapping。
- 继续更新：`docs/verification/emosonic_strict_v2_r18_requirement_mapping.md`。
- r18 mapping 必须同时记录服务端测试、Flutter 测试和最终 Android + Windows 真机证据状态。

## 5. 契约优先修改批次

下列文件路径均相对于仓库根目录。列出的文件是最低必改集合，不得用“其他相关分卷”代替全仓交叉搜索。

## Batch A — 公共错误、Core control settlement 与 queue terminal

### 最低必改文件

- `specs/emosonic_strict_v2_contract/phase-0-foundation/01-overview.md`
- `specs/emosonic_strict_v2_contract/phase-0-foundation/03-ack-errors-idempotency-and-cursors.md`
- `specs/emosonic_strict_v2_contract/phase-1-core/04-client-core-actions.md`
- `specs/emosonic_strict_v2_contract/phase-1-core/07-server-device-context-and-status.md`
- `specs/emosonic_strict_v2_contract/phase-1-core/08-server-queue-playback-and-controls.md`
- `specs/emosonic_strict_v2_contract/phase-2-optional-profiles/06b-broadcast-source-context.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/11a-common-and-core-requirements.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/11b-broadcast-requirements.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/11c-security-persistence-and-readiness.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/13-integration-acceptance.md`

### 必须写入的规则

1. `playback.context.prepare` 不再检查 `playbackPrepare`；只检查 Core authority、player、canPlay、唯一 Context、binding 和 cursor。
2. `playbackPrepare` 只属于 Handoff target 的 `playback.prepare`。
3. 按第 3.8 节定义普通 routed control 的 `executionTimeoutMs`、默认值、lease 和 watchdog。
4. 正式定义 server-only `playback.control.settled`：

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

5. settlement 幂等键为 `(playbackContextId, epoch, commandControlVersion)`。
6. ACK 只证明 accepted/routed；Socket emit 成功不能证明 audio success。
7. authority disconnect、Socket replacement、restart、watchdog expiry 只能生成 `execution_unknown`，不得伪造 authority `playback.update`。
8. track-changing command failed 时，按 controlVersion 升序将仍 pending 的后续事务逐条结算为 `dependency_failed`。
9. `playback.control.settled` 的收件人与跨连接行为按 D17 和第 3.9 节。
10. 所有 base cursor mismatch 统一 `stale_version`。
11. Context/Handoff/Broadcast state conflict 统一带完整 canonical cursors。
12. 写入 D10 的 first-prev、last-next 和第 3.2 节 automatic queue terminal。
13. active Broadcast 与 Follow 必须各自只从一次 canonical queue-terminal mutation派生一次 stopped 状态，不得重复推进 revision/cursor。

### 契约验收条件

- request/ACK/event matrix 能唯一回答每个 control 的最终结果来源。
- `playback.control.settled`、`executionTimeoutMs` 在正文、字段表、output allowlist、EARS 和验收项中同时出现。
- Core prepare 不再引用 Handoff capability。
- 没有任何文字把 ACK、emit 或 canonical target snapshot 解释成实际音频成功。
- 最后一首自然结束能够使 Context、DevicePlaybackState、Follow 和 Broadcast 收敛，不存在 Context 仍 playing 的分叉。

## Batch B — Follow 音频语义、occupancy fence 与恢复

### 最低必改文件

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

保持现有请求 shape：

```text
follow.start:
  sourcePlaybackContextId
  deviceSessionId

follow.stop:
  sourcePlaybackContextId
```

不新增 Follow mirror push 或 feedback action。

### 服务端职责

1. ownership 固定为 `(user, followerClientId, followerDeviceSessionId, sourcePlaybackContextId)`。
2. 按第 3.3 节记录 suspended Context、建立 fence，并定义安全 stop/reconnect grace。
3. 相同 source 重试幂等；不同 source 返回 conflict，不隐式切换。
4. 自动订阅 source Context，但 start ACK 之后仍显式请求 status。
5. follower connection 不得控制 source Context；其他有权限 controller 仍可正常控制 source。
6. source authority Handoff 后 relationship 保持，继续按同一 Context 跟随新 authority。
7. source Context close 时进入 terminating、推 closed，并在 follower restore/stop 后释放 fence。
8. mode gate 由服务端实际执行，不只是 Flutter UI：Follow follower、Broadcast ordinary、Handoff target 互斥。
9. source Context 可同时存在 Follow followers 与 active Broadcast，但 source authority pair 自身不得处于 overlay。
10. Broadcast 隐式/显式 participant 筛选、Handoff target/source eligibility 与 Follow source eligibility 必须检查 Follow fence。

### Flutter 职责

1. start 前保存本机 queue/index/position/state/rate。
2. Follow overlay 不修改 suspended Context cursor、queue 或 durable snapshot。
3. 应用 canonical source queue/state/position/rate。
4. 按 D12 区分 playing stale 与 paused/stopped/idle 稳定事实。
5. 按第 3.3 节先恢复本机任务，再发送 `follow.stop` 释放 fence。
6. Follow 时禁用 transport/queue 控制，只保留本机音量与 stop Follow。
7. drift correction 仅为本地执行；strict 路径不得发送普通 `playback.update` 描述 mirror。
8. app restart 不自动 Follow；同进程 Socket 重连在 30 秒 grace 内 re-start。

### 契约验收条件

- Follow 明确是实际音频执行，而不是观察订阅。
- Follow 与 Broadcast 的差异以“有无服务端 delivery/feedback ledger”清楚区分。
- paused/stopped/idle source 不因缺少 playing heartbeat 被错误退出。
- authority Handoff 不终止 Context Follow。
- Follow overlay 不会被其他 controller 的命令穿透，也不会污染任何正常 Context。
- stop/closed/reconnect 时 fence 与本地恢复不存在竞态窗口。

## Batch C — Handoff 时间线、clientSeq 与完整执行证明

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

### 请求与 push shape

`playback.handoff.start` 增加必需：

```text
targetDeviceSessionId
```

`playback.prepare` 在现有字段上增加：

```text
playbackRate
positionSampledAtServerMs
sourceEpoch
sourceVersion
sourceQueueRevision
```

不得再增加与 prepare `controlVersion` 重复的 `sourceControlVersion`。

Handoff commit `player.play` 增加：

```text
serverTimeMs
playbackRate
```

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

`playback.handoff.cancel` 增加 target commit failure 的条件字段：

```text
errorCode:C
errorMessage:O
```

### 状态机规则

1. start 前原子执行 D4 eligibility；失败不创建 handoffId/prepareId。
2. target 按 exact client/device pair 冻结；替换 Socket 或 deviceSession 后旧 ready/complete 无效。
3. ready 后 revalidation 按第 3.1 节允许正常 position/sample 前进，只对真正 source mutation 使用 `source_changed`。
4. commit 满足 `effectiveAtServerMs - serverTimeMs >= 250`，并使用最新 fresh source sample 投影位置。
5. prepare/commit/complete 的版本关系与 complete 后 cursor 按第 3.5 节固定。
6. complete 的 clientSeq 作用域、消费和重放按第 3.6 节。
7. continuity-first：source 在 complete 前继续播放。
8. complete 验证完整 actual fact，在一个事务中退休 target standby、写 target DevicePlaybackState、切 authority、更新 exact device binding、推进固定 cursors、完成 Handoff。
9. completed 后旧 source 通过 release、completed status、Context status 或 binding invalidation 任一事实都必须停止旧 authority audio lease。
10. target 收到 failed/cancelled/timedOut 时必须取消 timer、失效 execution lease；若已起播则立即暂停，且不得迟到 complete。
11. 非终态 Handoff 存在时 close 返回 conflict。
12. duplicate start 始终重放首次 `status:"preparing"` ACK；当前进度另发 canonical status。
13. duplicate/late ready 只重放当前 canonical status；不得保持 request cache in-flight。
14. duplicate complete 重放 completed status + current Context status，不重复 switch/release。
15. prepare/commit 不能可靠加入目标发送路径时立即 failed，不静默只等 timeout。
16. commit 实际失败按第 3.7 节由 frozen target 报告，不只依赖 5 秒 timeout。

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

### 契约验收条件

- 正常 playing position/sample 更新不会导致 `source_changed`。
- prepare、commit、complete 字段和 N/N+1 版本关系在客户端请求、服务端 push、公共 cursor、EARS 与 acceptance 中逐字段一致。
- complete 后 status 立即包含合法 target DevicePlaybackState。
- complete clientSeq 与后续 playback.update 连续。
- commit_failed 有确定报告入口和唯一终态。

## Batch D — Broadcast restore、action-aware cleanup 与软同步

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

### restorePending 规则替换

删除“服务端允许普通 command、Flutter 在恢复门控排队”的旧规则，改为第 3.4 节 action-aware matrix：

```text
restorePending=true
  -> ordinary suspended Context mutation 返回 restore_in_progress
  -> 不推进 cursor
  -> 不产生新执行 command/canonical mutation/binding invalidation
  -> read、terminal recovery 与 negative cleanup 继续允许
```

错误必须携带真正 suspended Context 的：

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
- `positionMs` 是 participant 实际观测，仍只做范围验证。
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

按第 3.10 节定义为 management surface，并与 exact pair decommission 原子绑定；不加入 strict Socket action。

### 契约验收条件

- restorePending 不再存在“服务端放行、Flutter 排队”的旧规则。
- raced Handoff 可以用 ready:false/cancel 有界清理，不会因 restore gate 卡到 timeout。
- follow.start 被阻止而 follow.stop 继续可用。
- `applied` 不被描述为固定 drift SLA。
- abandon 后旧 pair 无法再次注册复用；新 deviceSession 可正常开始新的 Context 生命周期。

## Batch E — r18 权威入口、映射与冻结收口

### 最低必改文件

- `specs/emosonic_strict_v2_socketio_server_contract.md`
- 全部 19 个权威分卷页首
- `docs/verification/emosonic_strict_v2_r18_requirement_mapping.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/13-integration-acceptance.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/14-authority-and-deployment-evidence.md`

### 内容

1. 保持权威身份为 r18 / `2.8.0`，将文档修订更新为 `2026-08-01-r18`。
2. 使用本计划第 1 节的三项状态：Approved、契约 Frozen、实现 pending。
3. 入口摘要增加 Follow 音频/fence、control settlement、automatic queue terminal、Handoff 完整 fact、restore freeze 与 queue boundary。
4. 全部 19 个分卷页首同步为 `2026-08-01-r18` / `2.8.0`。
5. 每项新/修改 REQ 建立目标测试映射；按第 4 节重新评估并降级失效的旧 Verified。
6. 明确调试 capability 默认 true 不等于 production rollout，但不是实现缺陷。
7. 最终 implementation readiness 证据继续要求 Android + Windows 双端真机。
8. 写入首次冻结前例外、成组升级规则、冻结后 r19/errata 纪律。
9. 契约 Frozen 不以真机实现证据为前置；真机证据只决定 profile implementation ready。

## 6. 契约完成后的代码实施顺序

## Phase 1 — Schema validator 与双方 fixtures

### 服务端

- `supysonic/emo/strict_v2_contract.py`

修改：

- Handoff start/complete/cancel request schema。
- Handoff prepare/commit output schema。
- complete clientSeq scope。
- 正式 control settlement output schema 与 routed timeout。
- `restore_in_progress` action-aware 校验。
- automatic queue-terminal passive shape。
- Follow occupancy error/cursor fixtures。

### Flutter

- `lib/services/emo_action_contract_policy.dart`
- `lib/services/emo_strict_v2_models.dart`
- `test/fixtures/emo_protocol/strict_v2/...`

必须先同步 parser/validator，再启用服务端新 push，避免闭合 schema 直接 quarantine。

## Phase 2 — Core control settlement 与 automatic queue terminal

### 服务端

- `ws.py`
- `ws_store.py`

修改：

- Core prepare 去掉 Handoff capability 依赖。
- settlement 收件人、watchdog、dependency cascade、restart/disconnect unknown。
- automatic final queue terminal。
- queue boundary。
- stale/conflict error builder。

### Flutter

- `emo_control_transaction_coordinator.dart`
- `audio_execution_lease.dart`
- command lane / router / audio end callback

修改：

- 正式消费 `playback.control.settled`。
- 失效对应 execution lease。
- 非请求 subscriber 只更新状态，不弹本地错误。
- 最后一首结束发唯一 passive queue-terminal fact。

## Phase 3 — Follow

### 服务端

- `ws.py`
- `ws_state.py`
- 必要的持久/瞬态 relationship store

修改：

- Context-owned relationship 与 suspended Context identity。
- Follow occupancy fence。
- 30 秒 reconnect grace。
- source authority switch continuation。
- safe stop/closed release。
- mode eligibility checks。

### Flutter

- `emo_follow_sync_controller.dart`
- `emo_realtime_client.dart`
- mode providers/router

修改：

- strict overlay 不 emit normal playback.update。
- playing-only 3 秒 stale / 30 秒 recovery。
- paused/stopped/idle 稳定状态。
- restore-before-stop。
- app reconnect re-start；app restart 不恢复。
- mode mutual exclusion。

## Phase 4 — Handoff

### 服务端

- `ws.py`
- `ws_store.py`

修改：

- exact target pair。
- source actual revalidation，允许正常位置前进。
- N/N+1 cursor relationship。
- full complete transaction 与 shared clientSeq。
- commit_failed target report。
- reliable prepare/commit settlement。
- close conflict。

### Flutter

- `emo_handoff_controller.dart`
- models/router/action policy

修改：

- new prepare/commit/complete/cancel schema。
- full complete fact/clientSeq。
- completed fallback stop source。
- failed target execution lease invalidation。
- commit_failed 主动报告。

## Phase 5 — Broadcast restore freeze 与管理清理

### 服务端

- `ws.py`
- `ws_store.py`
- `broadcast_store.py`
- 管理 CLI/API 与 device decommission store

修改：

- restorePending action-aware write freeze。
- negative ready/cancel cleanup。
- terminal delivery gate。
- recovery abandon + exact pair decommission transaction。

### Flutter

- `emo_broadcast_controller.dart`
- `emo_broadcast_recovery_repository.dart`

修改：

- 删除恢复期间普通 command 排队。
- `restore_in_progress` UI/retry。
- raced Handoff negative cleanup。
- 保持 terminal feedback 完整性。

## 7. 测试计划

## Core

- Handoff profile off / `playbackPrepare:false` 时 Core prepare 仍成功。
- accepted command 的 committed、failed、dependency_failed、execution_unknown 全部分支。
- `executionTimeoutMs` 默认/配置值、Windows lease 和服务端 +2000ms watchdog。
- disconnect/replacement/restart/watchdog 不永久 pending。
- settlement duplicate 不重复副作用，断线后不向 replacement Socket 自动补历史 event。
- queue first-prev、last-next、natural-last-end。
- final natural end 只产生一次 version+1，queue/control/epoch 不变。

## Follow

- 实际应用 source queue、play/pause/seek/natural transition。
- playing 3 秒 stale pause，恢复后继续；30 秒失败退出。
- paused/stopped/idle 超过 30 秒不会被错误退出。
- 小 drift 调速、大 drift seek。
- Follow overlay 不写任何正常 Context。
- 其他 controller 对 suspended Context 的写操作被 fence 拒绝。
- stop Follow 先恢复、后释放 fence；恢复期间无远程命令穿透。
- Socket 重连在 grace 内自动 re-start；app restart 不自动恢复且 grace 到期释放。
- source Handoff 后继续 Follow。
- follower control disabled。
- Follow/Broadcast/Handoff-target 互斥。
- source 同时有 Follow 与 Broadcast 时两条投影互不污染。

## Handoff

- idle/paused/stopped/stale/unsettled source fail-fast。
- target exact deviceSession replacement。
- 1.5x rate 与最新 sample position projection。
- prepare 期间只有 position/sample 正常前进时继续 commit。
- prepare 期间 source seek/track/state/rate/cursor/binding 变化 -> source_changed。
- prepare.controlVersion=N、commit/complete=N+1。
- full complete fact 后 status 立即含 target DevicePlaybackState。
- complete clientSeq 被后续 passive update 继续使用。
- duplicate start/ready/complete。
- prepare/commit enqueue failure。
- commit execution failed 主动报告 commit_failed。
- old source release 丢失但 status/binding 仍停止音频。
- target terminal 后不迟到 complete。
- active Handoff close conflict。

## Broadcast

- restorePending 时每个受阻写 action 都返回 `restore_in_progress`。
- ready:false restore_in_progress、handoff.cancel、follow.stop 和 terminal feedback 继续可用。
- error cursor 指向真正 suspended Context。
- 拒绝结果不推进 cursor、不发送新执行 command。
- terminal feedback 清 fence 后新 requestId 可正常执行。
- `applied` 只证明 revision target 已应用。
- terminal enqueue 失败后先断开，再通过重连 replay。
- recovery abandon 原子清 fence、record、slot 并 decommission exact pair。
- 被弃用旧 pair 注册失败，新 deviceSession 可正常注册。
- membership 在整个 lifecycle 固定。

## 8. 全量一致性审计

契约修改完成后必须进行机械审计：

1. 搜索所有 `r19`、`2.9.0`：本轮权威契约和计划中不得把当前版本误写成 r19/2.9.0；仅“冻结后新能力进入 r19”的未来规则可以保留。
2. 搜索所有 `2026-07-23-r18`：当前 normative 页首统一更新为 `2026-08-01-r18`；历史说明可保留旧日期。
3. 搜索 `Frozen implementation baseline`：当前权威状态不得使用该歧义表述，必须分成契约 Frozen 与实现 pending。
4. 搜索所有 `restorePending`、`restoringOriginalContext`、排队、controlVersion queue：不得残留“服务端允许命令、Flutter 排队执行”的旧 normative 规则；同时不得误删 negative ready/cancel 清理例外。
5. 搜索 Follow：不得再把 strict Follow 定义成只读观察；不得要求 strict Follow mirror 发送普通 `playback.update`；必须存在 suspended Context fence、restore-before-stop 和 reconnect grace。
6. 搜索 Handoff：不得残留仅 `targetClientId` 的 start shape、仅 `positionMs` 的 complete shape或重复 `sourceControlVersion`；正常 position/sample 前进不得被列为 `source_changed`。
7. 搜索 `playback.control.settled` 和 `executionTimeoutMs`：必须在 action surface、字段表、结算矩阵、幂等、timeout、EARS、acceptance 中完整出现。
8. 搜索 natural end/queue terminal：必须存在唯一 canonical mutation、精确 cursor 变化和 Broadcast/Follow 派生规则。
9. 搜索 `clientSeq`：Handoff complete 必须进入普通 playback sequence 作用域。
10. 搜索 `commit_failed`：必须存在 frozen target 的请求入口和 failed terminal 规则。
11. 搜索 queue 边界：prev/next/natural-end 在所有章节中不得互相矛盾。
12. 检查全部 Markdown 链接、章节引用、REQ 引用和 r18 mapping。
13. 逐项检查被改变语义的旧 Verified 是否已经降级。
14. 运行 `git diff --check`。
15. 允许运行纯文档或静态检查，但契约阶段不得为了让旧代码测试通过而修改代码、validator、fixtures 或客户端。

## 9. 纯文档提交顺序

契约改动拆为以下提交，禁止混入运行时代码：

```text
1. spec: close r18 core settlement and queue terminal
2. spec: define r18 follow audio fence and recovery
3. spec: harden r18 handoff execution proof
4. spec: simplify r18 broadcast restore gate
5. spec: finalize and freeze r18 contract authority
```

## 10. r18 契约完成标准

只有以下条件全部满足，才能宣布 r18 契约定稿并冻结：

- D1—D17 全部进入权威分卷和 REQ/acceptance。
- 第 3 节全部机械闭环进入权威分卷，不只停留在本计划。
- 每个 action 的请求、ACK/direct response、业务 push、错误、幂等、断线、重启和超时行为均有唯一解释。
- Follow、Broadcast、Handoff 的角色、occupancy fence 与本地覆盖层边界无歧义。
- Follow stop/closed/reconnect 不存在服务端提前放行与本地恢复并发。
- Handoff start/prepare/commit/complete/cancel 字段、clientSeq 和 cursor 关系在所有章节完全一致。
- Handoff 正常位置前进不会被误判为 source_changed。
- 最后一首自然结束有唯一 canonical mutation，Context 与 DevicePlaybackState 能够收敛。
- `restorePending` 不再存在两套相反的命令处理规则，且 negative cleanup 不会被错误阻止。
- `playback.control.settled` 和 `executionTimeoutMs` 不再只存在于实现或历史 ADR。
- recovery abandon 与 exact pair decommission 原子绑定。
- r18 mapping 对全部新/改要求有明确实施状态，旧证据失效的 REQ 已降级，未实现项没有伪造 Verified。
- 全部 19 个分卷页首和权威入口保持 r18 / `2.8.0`，文档修订统一为 `2026-08-01-r18`。
- 权威入口明确本次 pre-freeze 例外、成组升级和冻结后 r19/errata 纪律。
- 最终机械审计和 `git diff --check` 通过。

冻结后进入“实现 r18”阶段，不再重新讨论 D1—D17。未来真正改变冻结行为的新功能进入 r19。