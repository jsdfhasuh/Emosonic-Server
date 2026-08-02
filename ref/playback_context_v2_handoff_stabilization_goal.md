# EmoSonic Server PlaybackContext v2 稳定化与 Handoff 强化 Goal

> **Superseded design Goal.** 本文保留为历史设计证据，不是 strict-v2 r5 wire
> contract 或当前验收清单。当前权威要求见
> `specs/emosonic_strict_v2_socketio_server_contract.md` 和
> `docs/goal/emosonic_strict_v2_r5_server_adaptation.md`。

## 1. Goal 背景

当前 EmoSonic Server 已经完成 PlaybackContext v2 的主体重构：

- 使用 `playbackContextId` 表示跨设备共享的播放任务；
- 使用 `authorityClientId` 表示当前实际拥有播放权的设备；
- 使用 `queueRevision` 管理队列版本；
- 使用 `controlVersion` 管理控制命令顺序；
- 使用 `version / epoch` 管理整体状态及歌曲时间线；
- 支持播放权在多个设备之间来回 handoff；
- 支持 prepare、ready、complete、cancel、timeout 和持久化恢复；
- strict-v2 客户端不再依赖 legacy `sessionId`。

现阶段主流程已经可运行，但在服务重启、并发控制、设备断线、context 关闭和 handoff 提交中断等场景下，仍可能出现状态分裂、旧快照覆盖新状态、目标设备误播放或幽灵设备状态。

本 Goal 保留 PlaybackContext、DevicePlaybackState 和 Handoff 三个核心实体，在其上新增
authority generation、明确的事务生效点和 stabilized strict-v2 capability。legacy 路径
继续兼容，但不与新协议字段混用。

---

## 2. 总体目标

在单 realtime owner 部署前提下，实现一个具备以下保证的 stabilized strict-v2
PlaybackContext：

1. 一次 handoff 最终只能产生一个 authority。
2. handoff 完成后，内存状态和数据库 canonical generation 必须一致。
3. 服务端在任意提交步骤崩溃后，重启都能恢复到可判定状态。
4. handoff 不得把新歌曲回退成 prepare 阶段的旧歌曲。
5. 暂停状态切换到其他设备后仍保持暂停。
6. context 关闭后，任何旧 prepare、ready 或 complete 都不能再次启动播放。
7. strict-v2 客户端和 legacy 客户端之间不存在隐式字段污染。
8. 设备断线后不会被误报为在线；逻辑 authority 的 reconnecting/offline 状态明确可见且可恢复。
9. 重试、重连和服务重启不得无限延长 handoff 超时时间。
10. Flutter 客户端可以使用统一、稳定的 v2 payload 解析状态。
11. 历史 completed handoff 不得覆盖更高 authority generation 的当前状态。
12. target 只有在 authority 事务提交后才会收到 activate。

---

## 3. 核心状态约束

### 3.1 PlaybackContext 状态约束

每个 PlaybackContext 必须满足：

```text
一个 playbackContextId
    对应一个 userName
    对应零个或一个 authorityClientId
    对应零个或一个活动 handoff
    对应一条单调递增的 controlVersion 时间线
    对应一条单调递增的 version 时间线
    对应一条单调递增的 authorityGeneration 时间线
    对应零个或一个 lastCommittedHandoffId
```

`authorityGeneration` 的语义：

```text
新建 context 时 authorityClientId 非空 -> authorityGeneration=1
新建 context 时 authorityClientId 为空 -> authorityGeneration=0
迁移旧 context 时按同一规则初始化
每次 authorityClientId 从一个值变为另一个值时增加 1
context close 将 authorityClientId 清空时也增加 1
普通 playback.update、pause、seek 和 queue sync 不增加 authorityGeneration
```

`lastCommittedHandoffId` 只指向最近一次成功改变 authority 的 handoff。

strict-v2 的所有 authoritative mutation 必须携带期望的 `authorityGeneration` 或
`baseAuthorityGeneration`，包括 playback.update、queue sync、player control、handoff
start/complete 和 context close。服务端必须在 per-context 边界内与 canonical generation
比较。

只比较 clientId 不足以 fencing：A -> B -> A 后，A 的 generation=1 迟到消息不能在
generation=3 时重新生效。generation 不匹配返回 `authority_generation_conflict`，并携带
当前 authority/generation。

历史 handoff 是不可变审计记录，不能用历史 handoff 的 source/target 直接约束当前
`context.authorityClientId`。例如 A -> B 完成后又 B -> A，第一条 handoff 仍然是
`completed`，但当前 authority 合法地再次成为 A。

只有同时满足以下条件时，completed handoff 才能用于校验或修复当前 context：

```text
context.lastCommittedHandoffId = handoff.handoffId
context.authorityGeneration = handoff.committedAuthorityGeneration
```

此时才要求：

```text
context.authorityClientId = handoff.committedAuthorityClientId
handoff.committedAuthorityClientId = handoff.targetClientId
```

禁止出现：

```text
context.lifecycleState = closed
context.authorityClientId != null
```

也禁止出现：

```text
context.lifecycleState = closed
handoff.status = preparing / ready / committed
```

DevicePlaybackState 是 context authority 的投影：

```text
active context + authorityClientId 非空
    -> canonical stable state 恰好一条 isAuthority=true
    -> 该记录必须属于 authorityClientId

closed context 或 authorityClientId 为空
    -> 所有 device state isAuthority=false
```

新 create、handoff complete 和 reconciliation 必须建立对应 authority device state；旧数据
缺失时 reconciliation 可以补齐，而不能让第二条 device state 同时为 authority。

同一个 context 的所有权威写入，包括 handoff start/complete/cancel/timeout、
context close、queue sync、playback control 和 authority playback.update，必须经过同一个
per-context 串行化边界。单 worker 不能替代该并发约束。

### 3.2 Handoff 状态机

新实现统一 handoff 状态为：

```text
preparing
    ├── ready
    │     ├── completed
    │     ├── canceled
    │     ├── timed_out
    │     ├── superseded
    │     └── aborted
    ├── canceled
    ├── timed_out
    ├── superseded
    └── aborted
```

只允许以下状态迁移：

```text
preparing -> ready
preparing -> canceled / timed_out / superseded / aborted

ready -> completed
ready -> canceled / timed_out / superseded / aborted
```

所有终态都不可重新进入 preparing 或 ready。

第一次进入任意终态时必须一次性写入 `terminalAtMs`；duplicate terminal 请求不得刷新该
时间。终态的 source/target/base snapshot/committed snapshot 不可变，只允许补充明确的
delivery/reconciliation 诊断字段。

所有 canceled/timed_out/superseded/aborted 转移必须先持久化终态和 release reason，再向
target 发送 release。目标离线时，device reconnect reconciliation 根据终态重放 release；
不得因为首次发送失败而把 handoff 留在 active 状态。

状态含义：

- `preparing`：目标设备尚未完成资源准备；
- `ready`：目标设备已准备好，但尚未开始播放，也尚未获得 authority；
- `completed`：数据库事务已经提交 authority 转移；它不表示 post-commit WebSocket
  命令已经成功送达，也不表示目标已经上报首次 authority playback.update；
- `canceled / timed_out / superseded / aborted`：authority 从未因该 handoff 改变。

新实现不再产生 `committed` handoff。部署升级时必须兼容旧持久化记录：

```text
旧 committed + context authority=target
    -> 迁移或 reconcile 为 completed

旧 committed + context authority=source + deadline 未过期
    -> 迁移或 reconcile 为 ready

旧 committed + context authority=source + deadline 已过期
    -> timed_out

旧 committed + context authority 为 null/第三方
    -> aborted，并记录 reconciliation error
```

旧记录转换完成后，运行时查询活动 handoff 时只使用 `preparing / ready`。

### 3.3 Context 生命周期

本 Goal 强制新增独立字段，不再把生命周期或 authority 可用性写入播放 `state`。

定义：

```text
lifecycleState = active | closed
state = playing | paused | stopped
authorityStatus = online | reconnecting | offline
```

规则：

- `state` 只描述期望播放状态；
- `lifecycleState` 是持久化生命周期；
- `authorityStatus` 根据当前 authority 的 presence 和持久化断线时间计算；
- `lifecycleState=closed` 或 authorityClientId 为空时，authorityStatus 固定为 offline；
- 不允许再出现 `state="closed"` 或 `state="suspended"`；
- close 在一个数据库事务中直接执行 `active -> closed`；
- `closing` 不作为持久化状态。网络通知全部属于事务后的可重放副作用。

现有 context expire 路径也必须改为 `lifecycleState=closed`，并用独立
`closeReason=expired` 表达原因；不得继续写入 `state="expired"`。

本 Goal 内 `closeReason` 至少支持 `user_closed | expired`，服务端内部调用必须使用
allowlist，不能持久化客户端任意字符串。

兼容输出期间，如旧客户端仍依赖 `state="closed"`，只能在 legacy serializer 中映射，
不得污染 strict-v2 的持久化模型和 payload。

### 3.4 线性化和锁顺序

每个 context 的写操作必须遵循：

```text
获取 playbackContextId 对应的进程内串行化锁
    -> 开启数据库事务
    -> 锁定或 CAS PlaybackContext
    -> 锁定或 CAS 相关 Handoff
    -> 更新 DevicePlaybackState
    -> 提交数据库事务
    -> 用事务返回的 canonical rows 覆盖内存
    -> 释放串行化锁
    -> 发送 post-commit 命令、ACK 和广播
```

所有路径使用相同锁顺序：

```text
PlaybackContext -> PlaybackHandoff -> DevicePlaybackState
```

不得把调用前构造的完整内存 context 直接覆盖回数据库。事务方法必须从事务内读到的
canonical context 计算 delta，并使用 `lifecycleState / authorityGeneration / version` 等
条件更新防止 stale write。

---

## 4. Goal 1：Handoff 原子提交与崩溃恢复

### 4.1 当前问题

handoff complete 当前会分别执行：

1. 内存中转移 authority；
2. 写入目标设备状态；
3. 内存中将 handoff 标记为 completed；
4. 持久化 handoff；
5. 持久化 PlaybackContext；
6. 持久化 DevicePlaybackState；
7. 向旧 authority 发送 release。

这些步骤不是同一个数据库事务。

进程可能在任意两个步骤之间退出，从而出现：

```text
数据库 handoff = completed
数据库 context.authorityClientId = source
数据库 context.authorityGeneration = handoff.baseAuthorityGeneration
数据库 context.lastCommittedHandoffId != handoffId
```

或者：

```text
数据库 context.authorityClientId = target
数据库 handoff = ready
数据库 context.lastCommittedHandoffId = handoffId（或等价旧提交证据）
```

这里描述的是同一次提交的分裂，不是历史 completed handoff 与后续 context 状态的合法
差异。

### 4.2 实现要求

handoff start 也必须是数据库原子操作，例如：

```python
createPlaybackHandoffAtomically(
    handoff_id,
    request_id,
    playback_context_id,
    user_name,
    source_client_id,
    source_device_session_id,
    target_client_id,
    target_device_session_id,
    snapshot,
    prepare_expires_at_ms,
)
```

该方法在 per-context 锁和数据库事务内锁定/CAS context，校验 lifecycle、source authority
和 authorityGeneration，查询不存在其他 preparing/ready handoff，然后插入 handoff。两个
并发 start 只能有一个成功；相同幂等键按 Goal 6 返回同一记录。

事务提交后才创建内存 pending prepare 并发送 `playback.prepare`。如果进程在插入 handoff
后、发送 prepare 前崩溃，启动 reconciliation 使用原 `prepareExpiresAtMs` 重建 prepare，
不得生成新窗口。

handoff complete 在 `ws_store.py` 新增原子方法，例如：

```python
commitPlaybackHandoffCompletion(
    handoff_id,
    user_name,
    target_client_id,
    target_device_session_id,
    now_ms,
)
```

该方法不得接收调用方构造的完整 `playback_context` 并回写。它必须在事务内读取
canonical rows，并在同一个数据库事务中：

1. 锁定或 CAS `EmoPlaybackContext`；
2. 锁定或 CAS `EmoPlaybackHandoff`；
3. 校验 handoff 的 user/context/source/target/deviceSessionId 与请求一致；
4. 校验 handoff 当前为 `ready`，且 `now_ms < completeExpiresAtMs`；
5. 校验 context 的 `lifecycleState=active`；
6. 校验 context 当前 authority 仍为 source；
7. 校验 context 当前 `authorityGeneration=handoff.baseAuthorityGeneration`；
8. 按 Goal 2 的字段级规则判断快照是否仍可 rebase；
9. 从当前 context 计算 canonical track、queue、state 和 position；
10. 生成并持久化统一的 `effectiveAtServerMs`；
11. 将 context authority 更新为 target；
12. 将 `authorityGeneration` 增加 1；
13. 设置 `lastCommittedHandoffId=handoffId`；
14. 将 `controlVersion` 设置为事务内当前值加 1；
15. 增加 `version` 和 `epoch`；
16. 将该 context 的所有 device state `is_authority` 清零；
17. 以 canonical commit state 保存目标设备 authority 状态；
18. 将 handoff 标记为 `completed`，保存 committed generation/version/snapshot，并将
    `activationConfirmedAtMs` 初始化为空；
19. 提交事务并返回 canonical context、handoff、device state 和 post-commit intent。

handoff start 不再预占未来的 context `controlVersion`。handoff 协议消息使用
`handoffId + baseAuthorityGeneration` 作为操作 fencing token；最终
`committedControlVersion` 只在事务提交时根据 canonical context 分配。

只有数据库事务提交成功后，才更新或覆盖内存状态，并向客户端发送：

```text
playback.handoff.activate -> target
playback.handoff.release  -> source
system.ack
playback.context.changed
```

activate 和 release 必须携带同一个 `effectiveAtServerMs`、`handoffId` 和
`committedAuthorityGeneration`。客户端必须按该 token 幂等处理；重复命令不得重复切歌、
重复 seek 或恢复旧 authority。

WebSocket 命令不属于数据库事务。数据库中的 completed handoff 和 commit snapshot
同时充当该 handoff 的 durable post-commit intent，供启动恢复和设备重连时重放；本 Goal
不要求建设通用 durable command outbox。

### 4.3 幂等恢复

新增：

```python
reconcilePlaybackHandoff(handoff_id)
```

恢复只能依据持久化 generation/commit marker，不能仅比较历史 source/target 与当前
authority。

#### completed 且是当前最后一次 committed handoff

满足：

```text
context.lastCommittedHandoffId = handoff.handoffId
context.authorityGeneration = handoff.committedAuthorityGeneration
context.authorityClientId = handoff.committedAuthorityClientId
```

视为正常完成。修正 device `is_authority` 投影，并按需向重连的 source/target 重放
release/activate。duplicate complete 返回原提交结果和当前 canonical context，不再次提升
任何版本。

#### completed 但 context 已有更高 authorityGeneration

该 handoff 是合法历史记录。不得用它覆盖当前 context。duplicate complete 返回：

```text
原 handoff 的 committedAuthorityClientId / committedAuthorityGeneration
当前 playbackContext
historical=true
```

#### completed 且声称是最后一次提交，但 commit marker 不一致

在 handoff error 字段记录 reconciliation error，输出 critical log，并拒绝该 context 的
后续 mutation，返回 `context_reconciliation_required`。新事务实现不应生成该状态；不得
在证据不足时猜测并覆盖更高版本 context。只读 status 仍允许返回 canonical context 和
诊断字段。

#### ready 且 context authority=source、generation=baseAuthorityGeneration

继续等待 target complete，或按原始 `completeExpiresAtMs` 超时。

#### ready 且 context 已由同一 handoff 提升到 target

仅用于兼容升级前的分裂记录。补齐 handoff 的 committed 字段、device authority 投影和
post-commit intent 后标记 completed。

#### active handoff 遇到 closed context 或更高 authorityGeneration

分别标记为 `aborted` 或 `superseded`，不得回退 context。

reconciliation 触发点必须包括：

1. realtime owner 启动时扫描全部 active handoff；
2. device.register 后扫描该设备参与的 active/latest handoff；
3. duplicate start/complete/cancel；
4. 为同一 context 创建新 handoff 之前；
5. 定时维护任务处理到期 active handoff。

### 4.4 验收标准

必须通过以下故障注入测试：

```text
崩溃点 1：start handoff 插入提交后、发送 prepare 前
崩溃点 2：complete 进入事务前
崩溃点 3：事务内更新 context 后
崩溃点 4：事务内更新 handoff 后
崩溃点 5：事务内更新 device state 后
崩溃点 6：事务提交后、覆盖内存前
崩溃点 7：覆盖内存后、发送 activate/release 前
崩溃点 8：只发送 activate、尚未发送 release
崩溃点 9：发送命令后、发送 ACK 前
```

事务内任意异常必须整体回滚；事务提交后的任意崩溃必须通过 canonical rows 和
post-commit intent 恢复。

重启恢复后必须满足：

```text
最新 committed handoff generation 与 context authority 一致
历史 completed handoff 不覆盖更高 generation context
最多一个 authority
不会重复提升 controlVersion
不会重复切歌
不会产生第二条活动 handoff
source/target 重连时会收到幂等 fencing 命令
```

---

## 5. Goal 2：Handoff 快照新鲜度与旧歌曲回退保护

### 5.1 当前问题

handoff start 保存了当前 context 快照，但 complete 阶段主要依赖：

```text
baseControlVersion
controlVersion
```

在 prepare 到 complete 期间，authority 可能自动切到下一首歌曲。

普通 `playback.update` 可能更新：

```text
trackId
currentIndex
queueRevision
version
epoch
```

但不一定更新 `controlVersion`。

目标设备随后使用旧 prepare 快照 complete，可能把 context 覆盖回旧歌曲。

### 5.2 快照字段

handoff start 时必须保存：

```json
{
  "baseControlVersion": 10,
  "baseVersion": 25,
  "baseQueueRevision": 4,
  "baseEpoch": 7,
  "baseAuthorityGeneration": 3,
  "sourceClientId": "phone-1",
  "sourceDeviceSessionId": "device:phone-1",
  "targetClientId": "pc-1",
  "targetDeviceSessionId": "device:pc-1",
  "snapshotTrackId": "song-1",
  "snapshotCurrentIndex": 0,
  "snapshotState": "playing",
  "snapshotPositionMs": 30000,
  "snapshotServerUpdatedAtMs": 1780000000000,
  "prepareExpiresAtMs": 1780000008000,
  "protocolVersion": 2
}
```

`baseControlVersion` 和 `baseVersion` 用于审计、冲突响应和测试，不作为 complete 时的
简单相等条件。position/state 等允许变化时，version 合法地会前进。

handoff start 不预先写入或预占未来 context `controlVersion`。

### 5.3 新鲜度检查时机

必须在两个位置执行同一套字段级新鲜度检查：

1. target ready 后、handoff 进入 `ready` 前；
2. target complete 请求进入原子提交事务后、authority 转移前。

如果 ready 后、complete 前 source 又切歌或修改队列，第二次检查必须将 handoff
标记为 `superseded`，target 仍未收到 activate，因此不会误播放旧歌曲。

### 5.4 complete 校验与 rebase 规则

以事务内读取的当前 canonical context 为准：

#### 只发生 positionMs 变化

允许继续 handoff。

服务端从最新 context position 重新计算提交位置，而不是从 handoff start 的旧 position
计算：

```text
if currentContext.state == playing:
    rebasedPositionMs = currentContext.positionMs
        + max(0, commitNowMs - currentContext.serverUpdatedAtMs)
else:
    rebasedPositionMs = currentContext.positionMs
```

结果不得小于 0；如服务端已知歌曲时长，还必须 clamp 到合法范围。这样 source 在 handoff
期间上报的新 position 或执行 seek 后，不会被旧 snapshot 覆盖。

#### state 在 playing / paused / stopped 之间变化

允许继续 handoff。complete 使用当前 canonical `state` 作为 `desiredState`，并将
`committedControlVersion` 设置为事务内当前 controlVersion 加 1。

source pause、resume、stop 或 seek 不刷新 handoff deadline，也不复用 handoff start 时
假设的未来 controlVersion。

现有“任意 playback control 都 supersede timeline prepare”的通用逻辑必须拆分：

```text
pause / resume / stop / seek -> 不 supersede handoff，按最新 canonical state rebase
next / prev / playItem / queue identity change -> supersede handoff
```

#### trackId、currentIndex 或 epoch 变化

旧快照已经失效。

服务端应：

```text
handoff -> superseded
向 target 发送 playback.handoff.release
source 保留 authority
客户端重新发起新的 handoff
```

#### queueRevision 变化

视为 superseded。

不要在 handoff complete 中使用旧队列覆盖新队列。

#### authorityGeneration、authorityClientId 或 lifecycleState 变化

```text
lifecycleState=closed -> handoff aborted，返回 context_closed
authorityGeneration > baseAuthorityGeneration -> handoff superseded
authorityClientId != sourceClientId -> handoff superseded
```

不得通过旧 handoff 把 authority 从更新 generation 的设备抢回。

#### logicalVolume 或非权威展示字段变化

允许继续，使用当前 canonical context 值。DevicePlaybackState 中的设备本地 volume/muted
不得覆盖共享 context。

### 5.5 complete 请求字段白名单

strict-v2 `playback.handoff.complete` 只允许携带：

```json
{
  "handoffId": "handoff-...",
  "playbackContextId": "playback:alice:...",
  "deviceSessionId": "device:pc-1",
  "baseAuthorityGeneration": 3
}
```

不得接受 target 提供的以下字段作为 authoritative context 数据：

```text
queueSongIds
queueRevision
currentIndex
trackId
state
positionMs
controlVersion
version
epoch
authorityClientId
```

出现这些字段时 strict-v2 返回 `bad_request`，legacy 路径按原兼容规则处理。目标设备在
activate 后通过正常 authority `playback.update` 上报真实执行状态。

### 5.6 验收场景

```text
handoff preparing 时 source 自动切下一首
handoff ready 时 source 自动切下一首
handoff 期间 source seek，target 从新 position 激活
handoff 期间 source pause，target 保持 paused
handoff 期间 source resume，target 按 playing 激活
handoff 期间 source stop，target 保持 stopped
handoff 期间队列被修改
handoff 期间连续 next 两次
target complete 伪造 trackId/queue/state 被拒绝
旧 completed handoff 在 A -> B -> A 后重试不会覆盖 A
```

---

## 6. Goal 3：保持原播放状态，不强制目标播放

### 6.1 当前问题

目标 ready 后，服务端固定发送：

```text
command: player.play
state: playing
```

这会导致：

```text
源设备 paused
handoff 到目标设备
目标设备自动开始播放
```

### 6.2 改进方案

将“资源准备”“获得 authority”和“执行播放状态”拆开，并固定唯一生效顺序：

```text
target 收到 playback.prepare
    -> target 加载资源并发送 playback.ready
    -> server 将 handoff 标记为 ready，但不得发送 player.play/activate
    -> target 发送 playback.handoff.complete，表示接受 authority 转移
    -> server 原子提交 authority 和 completed handoff
    -> server post-commit 向 target 发送 activate
    -> server post-commit 向 source 发送 release
    -> target 执行后按普通 authority playback.update 上报
```

`playback.handoff.complete` 的 v2 语义是“目标已准备好并接受 authority 转移”，不是
“目标已经开始播放”。目标在收到 activate 前不得开始播放，也不得发送 authoritative
playback.update。

target 的 successful `playback.ready` ACK 必须包含 `handoffId / status=ready /
completeExpiresAtMs / baseAuthorityGeneration`，target 据此立即发送 complete；重复 ready
返回相同 deadline。

新增目标命令：

```text
playback.handoff.activate
```

payload：

```json
{
  "playbackContextId": "...",
  "handoffId": "...",
  "authorityClientId": "target",
  "authorityGeneration": 4,
  "desiredState": "playing | paused | stopped",
  "trackId": "song-1",
  "currentIndex": 0,
  "positionMs": 30000,
  "effectiveAtServerMs": 1780000000350,
  "controlVersion": 12,
  "version": 28,
  "epoch": 8,
  "commandToken": "handoff-...:4:activate"
}
```

客户端行为：

#### desiredState=playing

在 `effectiveAtServerMs` 开始播放。

#### desiredState=paused

加载歌曲并 seek 到指定位置，但保持暂停。

#### desiredState=stopped

保持资源已准备状态，但不得开始播放；后续只有新的 player.play 控制才能开始播放。

source 同时收到：

```json
{
  "action": "playback.handoff.release",
  "playbackContextId": "...",
  "handoffId": "...",
  "authorityClientId": "target",
  "authorityGeneration": 4,
  "effectiveAtServerMs": 1780000000350,
  "reason": "handoff_completed",
  "commandToken": "handoff-...:4:release"
}
```

source 在 `effectiveAtServerMs` 停止本地执行。该时间之后的 source playback.update 只能
记录为非 authority device feedback，不得修改 context。

### 6.3 能力协商和兼容边界

stabilized strict-v2 handoff 的 target 必须声明：

```json
{
  "capabilities": {
    "playbackContextV2": true,
    "playbackPrepare": true,
    "effectiveAtPlayback": true,
    "playbackHandoffV2": true,
    "playbackHandoffActivate": true
  }
}
```

只声明旧 `playbackContextV2`、但不支持 activate 的客户端不能作为 strict-v2 stabilized
handoff target，返回 `unsupported_capability`。不得使用
`playing -> player.play / paused -> activate` 这种混合协议猜测客户端能力。

legacy handoff 继续走独立 legacy handler 和 legacy payload；本 Goal 的原子、generation
和 post-commit replay 保证只对声明 `playbackHandoffV2` 的路径生效。

### 6.4 崩溃与重复命令

completed handoff 必须持久化完整 activate/release intent。服务重启或设备重连后：

- 只有 `activationConfirmedAtMs` 为空、authorityGeneration 仍匹配，并且当前 context
  `version/epoch` 仍等于 handoff 的 committed version/epoch 时，才允许重放原 activate；
- 上述条件满足且 `effectiveAtServerMs` 尚未来到：使用原时间重放；
- 上述条件满足但原时间已过去：根据 committed snapshot 和经过时间计算当前位置，生成
  立即执行命令；
- context 已有更高 version/epoch 时，严禁重放旧 committed snapshot；如 target 仍是当前
  authority，服务端使用当前 canonical context 生成 `recovery=true` 的新 activate，token
  由 `contextId + authorityGeneration + currentVersion` 派生；
- 同一个 `commandToken` 重放时客户端必须幂等；
- target 已经按更高 generation 播放时，旧 activate 必须忽略；
- source 已经观察到更高 generation 时，旧 release 视为幂等成功。

target 收到 activate 后的第一次有效 authority `playback.update` 必须携带对应
`authorityGeneration`，可同时携带 `commandToken`。服务端接受后一次性写入
`activationConfirmedAtMs`；重复 update 不刷新该时间。

### 6.5 验收标准

```text
playing -> handoff -> target playing
paused -> handoff -> target paused
stopped -> handoff -> target stopped
target complete 事务提交前不得播放
目标 complete 提交后 target 收到 activate
目标 complete 提交后旧 source 收到同 effectiveAt 的 release
事务提交后服务崩溃，重连可幂等补发 activate/release
旧 generation activate 不能覆盖新 authority
```

---

## 7. Goal 4：Context Close 完整生命周期

### 7.1 当前问题

`playback.context.close` 当前仅关闭 context 并广播状态。

它没有同步处理：

```text
活动 handoff
pending prepare
目标已 armed 的 player.play
当前 authority 的本地播放
持久化 DevicePlaybackState
```

### 7.2 Close 执行顺序

实现统一方法：

```python
closePlaybackContextAtomically(
    playback_context_id,
    closed_by_client_id,
    now_ms,
    close_reason="user_closed",
)
```

该方法在 per-context 锁内执行一个数据库事务：

1. 锁定或 CAS context，要求 `lifecycleState=active`；
2. 记录原 authorityClientId 和 authorityGeneration；
3. 将所有 `preparing / ready` handoff 标记为 `aborted`；
4. 保存每个 target 的 release intent；
5. 将所有 device state 的 `is_authority` 清零；
6. 设置 `context.authorityClientId=NULL`；
7. 设置 `context.lifecycleState=closed`；
8. 设置 `context.state=stopped`；
9. 设置 `context.lastCommittedHandoffId=NULL`；
10. 增加 `authorityGeneration / controlVersion / version / epoch`；
11. 设置 `closedAtMs`、`closedByClientId` 和已校验的 `closeReason`；
12. 提交并返回 canonical closed context、原 authority 和 aborted handoff 列表。

数据库提交后才执行：

1. 用 canonical rows 覆盖内存 context/handoff/device state；
2. 将对应 pending prepare 在内存中标记为 aborted；
3. 向所有 handoff target 发送幂等 release；
4. 向原 authority 发送 `playback.context.closed`/release；
5. 停止或解除使用该 context 的 follow relationship；
6. 广播最终 `playback.context.changed(changeType=closed)`；
7. 清理订阅和其他 runtime-only 状态。

`playback.context.closed` 命令至少携带：

```text
playbackContextId
lifecycleState=closed
authorityClientId=null
authorityGeneration
effectiveAtServerMs
reason=context_closed
commandToken=context:<id>:<authorityGeneration>:closed
```

客户端按 commandToken 幂等停止该 context 的本地执行和预备资源。

WebSocket 发送失败不得回滚 closed context。启动恢复或设备重连时，服务端必须根据
closed context、aborted handoff 和 generation 再次发送 fencing/release。

重复 close 是幂等成功：返回现有 closed snapshot，不再次提升任何版本。

如果 context 当前属于 active broadcast，`playback.context.close` 返回
`context_in_use`，要求先执行 `broadcast.stop`。禁止仅关闭底层 PlaybackContext 后遗留
活动 broadcast。

### 7.3 Closed context 行为

closed 后以下 action 必须拒绝：

```text
playback.update
queue.context.sync
player.play
player.pause
player.seek
player.next
player.prev
queue.playItem
playback.handoff.start
broadcast.start using this context
follow.start using this context
```

统一返回：

```json
{
  "code": "context_closed",
  "message": "Playback context is closed"
}
```

以下迟到 action 使用 handoff 的 terminal 幂等语义，而不是重新启动流程：

```text
playback.ready
playback.handoff.complete
playback.handoff.cancel
```

如果相关 handoff 已因 close 变为 aborted，返回该 terminal status，并向 sender 重发
release；绝不能发送 activate。

`playback.context.status` 仍允许读取 closed tombstone。对 closed context 的 subscribe
返回一次 closed snapshot，但不建立长期 subscription。

### 7.4 Context ID 复用策略

close 定义为终态。

关闭后不允许使用相同 ID 重新 create。

客户端每次创建新的播放任务时生成新 ID，例如：

```text
playback:<user>:<uuid>
```

不要长期复用：

```text
playback:alice:main
```

若未来确实需要固定槽位，再单独增加：

```text
playback.context.reset
```

本 Goal 不让 `create` 隐式重开 closed context，避免版本号回退。

为保证“永不复用”与数据清理同时成立：

- closed context 的 queue/playback/device 明细可按保留策略清理；
- `playbackContextId / userName / lifecycleState / authorityGeneration / closedAtMs /
  closeReason` 作为
  轻量 tombstone 永久保留；
- `playback.context.create` 必须同时检查 active row 和 tombstone；
- 本 Goal 不实现删除 tombstone 或 reset。

使用 tombstone ID 再次 create 返回 `context_closed`，并携带最小 closed context 信息；
不得创建新 version=1 的记录。

新 context 创建也必须进入 per-context 边界，在一个事务中完成 tombstone/duplicate 检查、
context insert、初始 authorityGeneration 和初始 authority DevicePlaybackState；并发 create
只能有一个成功。

---

## 8. Goal 5：Handoff 超时与重启恢复

### 8.1 当前问题

恢复 preparing 或 ready handoff 时，代码可能重新生成：

```text
now + HANDOFF_PREPARE_TIMEOUT_MS
now + HANDOFF_COMPLETE_TIMEOUT_MS
```

这会延长原始 deadline。

重复重试或反复重启可能让同一 handoff 长时间保持活动状态。

### 8.2 实现要求

handoff 创建时一次性持久化：

```text
prepareExpiresAtMs
```

第一次从 preparing 成功迁移为 ready 时，一次性持久化：

```text
readyAtMs
completeExpiresAtMs = readyAtMs + configuredCompleteTimeoutMs
```

`completeExpiresAtMs` 不得在 start 阶段提前生成，也不得在 duplicate ready、恢复或重连时
覆盖。

恢复时：

```python
remaining = persistedExpiresAtMs - currentServerTimeMs
```

规则：

- `remaining <= 0`：立即 timeout；
- `remaining > 0`：继续使用原 deadline；
- 不得重新生成完整超时窗口；
- duplicate start 不得刷新 deadline；
- duplicate ready 不得刷新 complete deadline；
- 重启不得刷新 deadline。

边界规则固定为：

```text
serverNowMs < expiresAtMs  -> 操作仍可提交
serverNowMs >= expiresAtMs -> timeout 获胜
```

complete、cancel 和 timeout 必须通过同一 handoff status CAS 决定唯一赢家。失败方读取终态
并返回幂等响应，不得覆盖赢家。

complete 和 cancel 的条件 UPDATE 必须同时包含 `serverNowMs < expiresAtMs`；到期边界上
不能让迟到 cancel 把应为 timed_out 的 handoff 改成 canceled。

### 8.3 验收测试

```text
prepare 已经过 7 秒，重启后只能再等约 1 秒
ready 已经过 4 秒，重启后只能再等约 1 秒
重复发送同一 requestId 不延长 deadline
连续重启不延长 deadline
过期 handoff 不发送 playback.handoff.activate
complete 与 timeout 同时发生时只有一个 CAS 成功
```

测试应使用 fake clock，避免真实 sleep。

---

## 9. Goal 6：身份、权限和协议安全

### 9.1 防止 handoffId 覆盖

客户端可以提供 `handoffId`。内存/数据库预查询只能作为快速路径，不能作为并发安全
保证：

```python
existing = state.get_playback_handoff(handoff_id)
    or getPlaybackHandoff(handoff_id)
```

如果已存在：

#### 完全属于同一幂等请求

返回原 handoff。

#### 属于其他 request、context 或 user

返回：

```json
{
  "code": "handoff_id_conflict",
  "message": "handoffId already exists"
}
```

严禁覆盖已有记录。

数据库必须保留 `handoff_id` 唯一约束。创建时使用 insert 并捕获唯一约束冲突，然后
重新读取和比较完整幂等键：

```text
userName
originClientId
requestId
playbackContextId
sourceClientId
sourceDeviceSessionId
targetClientId
targetDeviceSessionId
protocolVersion
```

完全一致才返回原 handoff，否则返回 `handoff_id_conflict`。

同时为 `(user_name, origin_client_id, request_id)` 增加数据库唯一约束。相同 requestId 的
并发 start 只能创建一条 handoff；约束冲突后按相同规则读取并返回 duplicate 或
`handoff_request_conflict`。

`handoffId` 和 `requestId` 必须为非空字符串，并在写数据库前校验最大长度与字段定义
一致。

### 9.2 deviceSessionId 绑定校验

strict-v2 mutation 必须携带非空 `deviceSessionId`，且必须等于注册设备的：

```text
currentClient.deviceSessionId
```

涉及：

```text
playback.context.create
playback.context.close
queue.context.sync
playback.update
player.play / player.pause / player.seek / player.next / player.prev
queue.playItem
playback.ready
playback.handoff.start
playback.handoff.complete
playback.handoff.cancel
broadcast participant feedback
```

不匹配时返回：

```text
device_session_mismatch
```

避免客户端伪造其他设备的 device session。

handoff start 时必须将 target 当时注册的 `targetDeviceSessionId` 持久化。target 的 ready
和 complete 必须同时匹配 `targetClientId` 与 `targetDeviceSessionId`。相同 clientId 以
不同 deviceSessionId 重连时，旧 handoff 不得自动转移到新设备实例，应返回
`device_session_mismatch` 并由 source/controller 重新发起。

sourceClientId 与 targetClientId 必须不同；如果 source 和 target 解析为同一个
deviceSessionId，也必须拒绝，避免同一设备实例通过多个 clientId 进行伪 handoff。

对于已有 context 的 strict-v2 mutation：

```text
playback.update -> authorityGeneration 必须等于当前 generation
queue.context.sync -> baseAuthorityGeneration 必须等于当前 generation
player.* / queue.playItem -> baseAuthorityGeneration 必须等于当前 generation
playback.handoff.start -> baseAuthorityGeneration 必须等于当前 generation
playback.handoff.complete -> 必须等于 handoff.baseAuthorityGeneration
playback.context.close -> baseAuthorityGeneration 必须等于当前 generation
```

即使 sender clientId 当前再次成为 authority，也不能省略 generation 检查。
唯一例外是 context 已经 closed 的 duplicate close：在完成用户权限校验后直接返回当前
tombstone，不再执行 generation CAS。

### 9.3 v2 Handoff 目标能力

strict-v2 handoff 的 target 必须满足：

```json
{
  "roles": ["player"],
  "capabilities": {
    "playbackContextV2": true,
    "playbackPrepare": true,
    "effectiveAtPlayback": true,
    "playbackHandoffV2": true,
    "playbackHandoffActivate": true
  }
}
```

缺少任意能力时返回 `unsupported_capability`，不得进入 preparing。

在线 source 也必须声明 `playbackContextV2=true` 和 `playbackHandoffV2=true`，保证它能按
authorityGeneration 处理 release/fencing。controller 对离线 source 发起 recovery handoff
时可以例外继续，但服务端只能保证旧 source 的后续写入被拒绝；旧设备本地音频要等它
重连并处理 release 后才能确认停止。

### 9.4 Controller 取消权限

目前 controller 可以发起 handoff，但取消阶段应同时允许：

```text
sourceClientId
targetClientId
originClientId
具备 controller role 的同用户设备
```

至少要保证 handoff 原始发起者可以取消自己发起的交接。

权限固定为：source、target、origin，或同 userName 且当前声明 controller role 的设备。
跨用户设备、没有 controller role 的旁观设备，以及仅伪造 originClientId 的设备必须返回
`forbidden`。服务端只能使用注册会话身份判断，不能信任 payload 声明的角色或 userName。

### 9.5 strict-v2 与 legacy context 边界

规则：

- strict-v2 action 继续拒绝 `sessionId`；
- `EmoPlaybackContext.playback_json` 每次保存前都必须经过 canonical sanitizer，删除
  `sessionId / sourceClientId / playback` 等 legacy wrapper；
- `EmoPlaybackHandoff.snapshot_json / commit_snapshot_json` 也必须使用显式字段白名单，
  不得直接序列化完整 runtime context；
- legacy handler 如需更新同一 context，必须先通过 compatibility adapter 映射到 canonical
  字段，再进入统一 per-context mutation boundary；不得把原 legacy payload 整体写入；
- legacy 客户端不能以 legacy payload 成为 stabilized handoff target；
- strict-v2 serializer 与 legacy serializer 分离，不能修改同一个 dict 后复用给不同接收方。

这项边界与 per-recipient serializer 一起保证 legacy `sessionId/sourceClientId/playback`
字段不会重新写入 strict-v2 canonical context。

---

## 10. Goal 7：设备断线和 Authority Offline

### 10.1 DevicePlaybackState 在线状态

为 DevicePlaybackState 增加：

```text
online
lastSeenAtMs
disconnectedAtMs
```

`online` 是当前 realtime owner 的 presence 投影，不是跨进程永久事实。

realtime owner 每次启动、在接受新 Socket 连接之前，必须批量将持久化 device state
设置为：

```text
online=false
```

不得继承上一次进程留下的 `online=true`。`lastSeenAtMs` 和 `disconnectedAtMs` 保留，用于
恢复宽限期和展示历史。

如果记录在崩溃前仍为 online 且没有 `disconnectedAtMs`，启动 reset 使用
`lastSeenAtMs` 作为断线时间下界，不得使用当前启动时间重新赠送完整 grace window。

设备注册时，服务端将该 userName/clientId 对应的已有 device state 标为在线；后续任意
有效设备消息更新 `lastSeenAtMs`。`playback.update` 仍会更新播放字段。

```text
online=true
```

Socket 断开或 stale prune 时：

```text
online=false
isAuthority 不直接篡改
disconnectedAtMs 只在 online -> offline 时写入
```

如果相同 clientId 已由新 sid 接管，旧 sid 迟到的 disconnect 不得把新连接标为 offline。

context status 返回设备状态时，客户端可以明确知道：

```text
设备曾参与过
设备当前是否在线
设备是否仍是逻辑 authority
```

### 10.2 Authority offline 策略

authority 断线时不立即自动把 authority 改给其他设备。

采用宽限期：

```text
authorityGracePeriodMs = 10000
```

期间：

```text
authorityStatus = reconnecting
```

同一个设备重连后，只有同时满足以下条件才继续作为原 authority：

```text
context.authorityClientId 仍等于该 clientId
context.authorityGeneration 未被后续 handoff/close 改变
注册 deviceSessionId 与 authority DevicePlaybackState 一致
context.lifecycleState = active
stabilized context 所需 capability 仍然存在
```

满足时：

```text
authorityStatus = online
请求设备重新上报 playback.update
```

不满足时，该连接只能作为非 authority device 参与，并收到当前 generation 的
release/fencing 状态；不得因复用 clientId 抢回 authority。

超过宽限期：

```text
authorityStatus = offline
```

context 的 `state` 继续保留原 `playing / paused / stopped`，不得写成 suspended。需要控制
authority 时返回 `authority_offline`。

同用户 controller 可以执行 recovery handoff，将播放权转移到其他在线 player。

authority grace deadline 固定为：

```text
authorityGraceExpiresAtMs = disconnectedAtMs + authorityGracePeriodMs
```

服务重启、重复 disconnect 或 stale prune 不得重新生成完整宽限期。
当 authorityStatus=reconnecting 时，strict-v2 context serializer 必须输出
`authorityGraceExpiresAtMs`；其他状态可省略该字段。

authority disconnect、reconnect 和 grace expiry 都必须发送
`playback.context.changed(changeType=availability)`。这些事件不强制提升播放 context
`version`；客户端按 device presence 时间字段和 authorityGeneration 合并。

### 10.3 Active handoff 遇到断线

规则固定为：

```text
target 在 preparing/ready 期间离线
    -> 保持到原 deadline；重连且 deviceSessionId 匹配可继续
    -> deadline 到期 timed_out，不转移 authority

source 在 preparing/ready 期间离线
    -> source 仍是逻辑 authority
    -> target 可在原 deadline 内 complete
    -> complete 成功后 authority 转移到 target

source 和 target 都离线
    -> 不自动选择第三方
    -> 按原 deadline timeout，或由有权限 controller cancel

target complete 提交后立即离线
    -> context 仍以 target 为逻辑 authority
    -> authorityStatus 进入 reconnecting/offline
    -> 不自动回滚给 source
```

所有断线规则必须使用原 handoff deadline 和 authority generation。

### 10.4 幽灵状态清理

增加定时清理策略：

```text
closed context 的重 payload/device 明细保留 7 天
closed context 的 ID tombstone 永久保留
普通非 authority device state 离线 24 小时后清除
未被任何 active context 的 lastCommittedHandoffId 引用的 terminal handoff 保留 7 天
active context 当前引用的 last committed handoff 不得清理
context close 后，最后一条 committed handoff 至少保留到 closed payload 保留期结束
活动 handoff 到期后先 CAS 为 timed_out，再由 terminal 策略清理
活动 handoff 永远不由普通清理任务直接删除
```

实际时间通过配置项控制。清理任务必须按 `closedAtMs / terminalAtMs /
disconnectedAtMs` 和索引执行，不得使用会被普通读取刷新语义的时间字段。

handoffId/requestId 的服务端幂等查询保证至少覆盖 terminal retention window。客户端仍必须
使用不可复用 UUID；清理后的旧 ID 不应被主动重用。

---

## 11. Goal 8：统一 v2 输出协议

### 11.1 Handoff complete ACK

当前 complete ACK 中的 context 必须改为统一字段：

```json
{
  "completed": true,
  "duplicate": false,
  "historical": false,
  "handoffId": "...",
  "playbackContextId": "...",
  "authorityClientId": "pc-1",
  "authorityGeneration": 4,
  "committedAuthorityClientId": "pc-1",
  "committedAuthorityGeneration": 4,
  "committedControlVersion": 12,
  "committedContextVersion": 28,
  "committedEpoch": 8,
  "effectiveAtServerMs": 1780000000350,
  "playbackContext": {
    "...": "serializePlaybackContextV2 result"
  }
}
```

`authorityClientId / authorityGeneration / playbackContext` 始终描述响应时的当前 canonical
context；`committed*` 描述该 handoff 当时的不可变提交结果。旧 completed handoff 在后续
handoff 后重试时设置 `historical=true`，不得把旧 target 冒充为当前 authority。

不再返回未经清理的：

```text
playback
sessionId
sourceClientId
```

该 ACK 形状只用于声明 `playbackHandoffV2` 的客户端。legacy handoff 保留原 ACK serializer，
不得在一次响应中混合 legacy `playback/sessionId` 和 strict-v2 context 字段。

### 11.2 Context 推送统一

strict-v2 统一使用：

```text
state / playback.context.changed
```

统一 payload：

```json
{
  "changeType": "created | playback | queue | authority | device | availability | closed",
  "serverTimeMs": 1780000000000,
  "playbackContext": {},
  "deviceStates": []
}
```

`playbackContext` 必须包含 `version / controlVersion / epoch / authorityGeneration /
lifecycleState / authorityStatus`，供 Flutter 丢弃旧事件。

客户端合并规则必须写入 Flutter 文档：

- context 状态按 `version` 合并，authority 额外按 `authorityGeneration` fencing；
- device presence 按每个 device 的 `lastSeenAtMs / disconnectedAtMs` 合并；
- `serverTimeMs` 只用于时间换算，不作为唯一的单调事件序号；
- 收到较低 authorityGeneration 的 activate/release/context event 必须忽略。

旧 action 在兼容期继续发送给未声明 `playbackContextEventsV2` 的客户端：

```text
playback.update
queue.context.sync
playback.context.status
```

新增能力协商：

```json
{
  "playbackContextEventsV2": true
}
```

声明新能力的客户端只接收统一事件。

事件选择必须按每个接收方 capability 单独执行。混合新旧客户端参与同一 context 时：

- 新客户端只收到 `playback.context.changed`；
- 旧客户端只收到旧 action；
- 同一个客户端不得同时收到两套等价事件；
- serializer 不能按 context 全局选择，避免一个 legacy participant 污染所有 strict-v2
  subscriber。

### 11.3 错误码补充

新增：

```text
context_closed
handoff_superseded
handoff_expired
handoff_id_conflict
handoff_request_conflict
device_session_mismatch
context_version_conflict
authority_generation_conflict
context_reconciliation_required
unsupported_capability
authority_offline
context_in_use
```

`handoff_expired` 对应持久化状态 `timed_out`。上述已定义的具体错误不得再退化为泛化的
`conflict` 或 `forbidden`；legacy handler 可保留旧错误映射。

错误响应继续携带：

```text
currentControlVersion
currentQueueRevision
currentVersion
currentEpoch
currentAuthorityGeneration
currentAuthorityClientId
currentLifecycleState
```

只携带当前资源实际存在且与错误相关的字段；不得用 `null` 冒充未知版本。

---

## 12. Goal 9：部署约束和多进程安全

当前 WebSocket 状态保存在进程内内存结构中，包括：

```text
clients
playback contexts
device states
pending prepares
handoffs
subscriptions
```

因此当前版本应明确：

```text
Emo Socket.IO namespace 只能由一个实时状态 worker 承载
```

本 Goal 的原子性、presence 和内存一致性保证只在“单 realtime owner”部署前提下成立。
该前提必须写入部署和运维文档，不能仅作为性能建议。

本 Goal 暂不实现 Redis 分布式状态机。

需要完成：

1. 在部署文档中明确单 worker 约束；
2. 当应用启用 `/emo` 且已知 Gunicorn/processes 配置大于 1 时启动失败；
3. 对无法自动检测的多容器/外部编排场景输出明显 warning，并标记为 unsupported；
4. Docker/Gunicorn 示例保持单 WebSocket worker；
5. HTTP 服务如需独立扩展，普通 HTTP replica 必须关闭 `mount_emosonic`，由单独实例
   承载 `/emo`；反向代理必须把全部 `/emo/ws` 流量路由到该 owner；
6. realtime owner 启动时先执行 active handoff reconciliation 和 presence offline reset，
   完成后才接受连接。

仅输出 warning 但继续以已知多 worker 配置运行，不满足本 Goal 的 Definition of Done。

未来需要横向扩展时，再独立建设：

```text
Redis presence
Redis pub/sub
distributed lock
durable command outbox
handoff lease
```

其中“通用 durable command outbox”不在本 Goal 内；Goal 1 要求的 handoff-specific
activate/release intent 必须随 completed handoff 持久化，不能以 outbox 非目标为由省略。

---

## 13. 代码改动范围

### `supysonic/emo/ws.py`

负责：

- handoff 状态机入口；
- context close 编排；
- strict-v2 身份校验；
- authority offline 判断；
- 统一错误码；
- 统一消息输出；
- release 和 activate 命令；
- startup/device reconnect recovery handler；
- capability-based per-recipient serializer；
- periodic deadline/presence/cleanup maintenance；
- 所有 context mutation 进入统一 per-context operation boundary。

### `supysonic/emo/ws_state.py`

负责：

- 严格状态迁移；
- 禁止终态回退；
- handoffId 冲突检查；
- context lifecycle；
- device online/offline；
- 内存状态恢复和覆盖；
- clear context runtime state；
- per-context lock registry 和统一锁顺序；
- authorityGeneration 和 commandToken fencing。

### `supysonic/emo/ws_store.py`

负责：

- 原子 handoff complete；
- 原子 context close；
- recovery reconciliation；
- deadline 持久化；
- lifecycle 和 online 状态持久化；
- terminal data cleanup；
- CAS/row-count conflict handling；
- 启动时 presence reset；
- handoff-specific post-commit intent 读取。

### `supysonic/db_layer/emo.py`

必须新增或正式建模：

```text
EmoPlaybackContext.lifecycle_state
EmoPlaybackContext.authority_generation
EmoPlaybackContext.last_committed_handoff_id
EmoPlaybackContext.closed_at_ms
EmoPlaybackContext.closed_by_client_id
EmoPlaybackContext.close_reason

EmoDevicePlaybackState.online
EmoDevicePlaybackState.last_seen_at_ms
EmoDevicePlaybackState.disconnected_at_ms
EmoDevicePlaybackState.authority_generation

EmoPlaybackHandoff.source_device_session_id
EmoPlaybackHandoff.target_device_session_id
EmoPlaybackHandoff.prepare_expires_at_ms
EmoPlaybackHandoff.ready_at_ms
EmoPlaybackHandoff.complete_expires_at_ms
EmoPlaybackHandoff.base_version
EmoPlaybackHandoff.base_queue_revision
EmoPlaybackHandoff.base_epoch
EmoPlaybackHandoff.base_authority_generation
EmoPlaybackHandoff.committed_authority_client_id
EmoPlaybackHandoff.committed_authority_generation
EmoPlaybackHandoff.committed_control_version
EmoPlaybackHandoff.committed_context_version
EmoPlaybackHandoff.committed_epoch
EmoPlaybackHandoff.committed_at_ms
EmoPlaybackHandoff.effective_at_server_ms
EmoPlaybackHandoff.activation_confirmed_at_ms
EmoPlaybackHandoff.terminal_at_ms
EmoPlaybackHandoff.commit_snapshot_json
EmoPlaybackHandoff.protocol_version
```

queue/track/position 等大 payload 可以继续放在 JSON 中，但所有参与事务条件、deadline、
generation、状态迁移、清理和索引的字段必须使用正式列。不得先塞进 JSON 再宣称满足本
Goal 的原子恢复要求。

### Schema 和 migration

必须同步修改：

```text
supysonic/schema/sqlite.sql
supysonic/schema/mysql.sql
supysonic/schema/postgres.sql
supysonic/schema/migration/sqlite/<new-version>.sql 或 .py
supysonic/schema/migration/mysql/<new-version>.sql 或 .py
supysonic/schema/migration/postgres/<new-version>.sql 或 .py
supysonic/db_layer/schema.py 的 SCHEMA_VERSION
```

必须增加：

```text
UNIQUE(handoff_id)
UNIQUE(user_name, origin_client_id, request_id)
INDEX(playback_context_id, status)
INDEX(playback_context_id, authority_generation)
INDEX(lifecycle_state, closed_at_ms)
INDEX(online, disconnected_at_ms)
INDEX(status, terminal_at_ms)
```

如某数据库对 nullable unique 或索引语法不同，迁移必须按 provider 实现等价语义。

所有 `*AtMs` 字段必须使用可容纳 Unix epoch milliseconds 的 BIGINT/等价字段，不能使用
32-bit INTEGER。

旧数据迁移规则必须确定：

```text
旧 context.state=closed
    -> lifecycleState=closed, state=stopped, authorityClientId=null,
       lastCommittedHandoffId=null, closeReason=legacy_closed

旧 context.state=expired
    -> lifecycleState=closed, state=stopped, authorityClientId=null,
       lastCommittedHandoffId=null, closeReason=expired

其他旧 context
    -> lifecycleState=active

authorityClientId 非空的旧 context
    -> authorityGeneration=1

authorityClientId 为空的旧 context
    -> authorityGeneration=0
```

对于 active context 下缺少 generation 元数据的历史 handoff：

- 只选择“最新且 target 等于当前 authority”的 completed/committed handoff 作为候选
  `lastCommittedHandoffId`；
- 为该候选补齐当前 generation/版本 commit marker；
- 更早的 protocolVersion<2 completed handoff 保留为 historical，committed generation
  可以为空，永远不能用于修复当前 context；
- 无法确定的旧 committed 按 3.2 的规则归一化并记录 migration warning。

创建 request unique 约束前必须扫描旧重复键。每组保留一条 canonical 记录；其余记录
清空 request_id、记录 `legacy_duplicate_request`，若仍为 active 则转为 superseded。迁移
不得在存在重复数据时直接失败并留下半升级 schema。

### 数据库兼容策略

项目支持 SQLite、MySQL 和 PostgreSQL，事务实现不得假设三者都有相同的行锁语法：

- PostgreSQL/MySQL：可使用 row lock，并继续使用 generation/version 条件 UPDATE；
- SQLite：使用写事务序列化能力和条件 UPDATE/CAS，不得调用不支持的 `FOR UPDATE`；
- 所有 provider：检查 UPDATE row count，0 行表示冲突并重新读取 canonical state；
- 原子方法不得调用会在中途独立 open/close connection 的旧 save helper；
- start、complete、cancel、timeout、close 使用相同的 context-first 锁顺序。

### 配置和部署入口

必须同步检查或修改：

```text
supysonic/config.py
config.sample
supysonic/server/__init__.py
supysonic/server/gunicorn.py
setup.sh
Dockerfile（仅在示例或启动约束需要时）
```

新增配置至少包括：

```text
emo_handoff_prepare_timeout_ms
emo_handoff_complete_timeout_ms
emo_authority_grace_period_ms
emo_closed_context_payload_retention_days
emo_terminal_handoff_retention_days
emo_offline_device_state_retention_hours
emo_playback_maintenance_interval_seconds
```

默认值分别为 `8000 / 5000 / 10000 / 7 / 7 / 24 / 30`。所有值必须为正整数；只有已有
明确“0 表示关闭”语义的配置才允许 0，本组可靠性维护配置不允许通过 0 关闭。

prepare/complete/grace 使用精确 deadline timer；maintenance interval 只是启动恢复和漏网
任务的安全网，不能让 5 秒 complete timeout 延迟到下一个 30 秒维护周期。

测试必须覆盖默认值、非法值和重启后 deadline 不变。

### 文档

必须同步：

```text
docs/plans/flutter_emo_realtime_playback_v2.md
realtime 单 owner 部署说明
strict-v2 capability / ACK / event / error code 文档
```

### 测试

主要修改：

```text
tests/base/test_emo_ws.py
tests/base/test_emo_ws_state.py
tests/base/test_emo_ws_store.py
tests/base/test_emo_playback_context_schema.py
tests/base/test_emo_playback_context_migration.py
provider-specific transaction integration tests
```

---

## 14. 测试矩阵

### 14.1 正常流程

```text
A -> B
B -> A
controller 发起 A -> B
source 发起 A -> B
target complete
start 不预占 context controlVersion，complete 只增加一次
重复 complete
重复 start
重复 ready
A -> B -> A 后重试第一条 completed handoff
重复 context close 不提升版本
closed tombstone ID create 被拒绝
同 ID 并发 context create 只有一个成功
```

### 14.2 播放状态

```text
playing handoff
paused handoff
stopped handoff
seek during handoff
pause/resume during handoff
next during handoff
queue change during handoff
target complete 携带伪造 track/queue/state
```

### 14.3 设备异常

```text
source 在 preparing 期间断线
source 在 ready 期间断线
target 在 preparing 期间断线
target 在 ready 期间断线
target complete 后立即断线
source release 前断线
authority 断线后重连
authority 超过宽限期未重连
旧 sid disconnect 不得覆盖新 sid online
服务重启后 persisted online=true 被重置
同 clientId 不同 deviceSessionId 重连
```

### 14.4 服务异常

```text
prepare 后服务重启
ready 后服务重启
complete 事务提交前重启
complete 事务提交后、发消息前重启
activate 已发送、release 未发送时重启
close 处理中重启
close 后迟到 ready/complete 只能收到 aborted/release
active broadcast context close 返回 context_in_use
启动扫描到过期 preparing/ready handoff
```

### 14.5 并发

```text
两个 controller 同时 handoff
handoff 与 queue sync 同时发生
handoff 与 context close 同时发生
handoff 与 player.next 同时发生
handoff complete 与 timeout 同时发生
cancel 与 complete 同时发生
同 requestId 并发创建不同 handoffId
旧 source playback.update 与 complete 同时发生
```

### 14.6 安全

```text
跨用户 context
跨用户 target
伪造 deviceSessionId
复用其他用户的 handoffId
controller 越权取消
target 伪造 complete
旧 authority 在完成后继续 playback.update
A -> B -> A 后 A 的旧 generation playback.update 被拒绝
旧 generation activate/release 重放
不支持 playbackHandoffActivate 的 strict-v2 target
```

### 14.7 并发结果判定

测试不能只验证“没有异常”，必须验证以下唯一结果：

#### 两个 start 同时发生

- 相同幂等键：一条 handoff，两个请求得到相同 handoffId；
- 不同幂等键：一个进入 preparing，另一个返回 active handoff conflict。

#### complete 与 timeout 同时发生

- `serverNowMs >= completeExpiresAtMs` 时 timeout 获胜；
- 否则第一个成功 CAS `ready` 状态的操作获胜；
- 最终不能同时出现 completed 和 timed_out 副作用。

#### cancel 与 complete 同时发生

- 第一个成功 CAS `ready` 的操作获胜；
- complete 获胜则 authority=target；
- cancel 获胜则 authority 保持 source，target 不得收到 activate。

#### close 与 complete 同时发生

- close 先提交：handoff=aborted，context=closed，authority=null；
- complete 先提交：handoff 可保持 completed，随后 close 使 context=closed、authority=null；
- 历史 completed handoff 不得因 context 已 closed 被错误回滚。

#### queue/next 与 complete 同时发生

- queue/next 先提交：handoff=superseded；
- complete 先提交：authority=target，旧 source 后续写入仅为 device feedback；
- 任何顺序都不能让旧 queue/track 覆盖新状态。

#### source pause/seek 与 complete 同时发生

- 通过同一 per-context 边界串行化；
- pause/seek 先提交：complete 使用最新 state/position；
- complete 先提交：旧 source 的迟到控制因 authority generation 不匹配被拒绝。

### 14.8 数据库和 migration

必须验证：

```text
全新 SQLite/MySQL/PostgreSQL schema 包含全部列、约束和索引
从当前 SCHEMA_VERSION 升级后数据保留且旧 committed 被正确归一化
事务内每个故障注入点均整体回滚
SQLite CAS 与 MySQL/PostgreSQL row-lock 路径得到相同业务结果
handoffId/requestId 唯一约束竞态返回稳定错误码
closed tombstone 阻止 ID 重建
```

### 14.9 协议兼容

必须验证混合客户端：

```text
playbackContextEventsV2 客户端只收到统一事件
legacy 客户端只收到旧事件
strict-v2 complete ACK 不含 playback/sessionId/sourceClientId
legacy complete ACK 保持兼容形状
legacy update 经 adapter 后持久化 JSON 不含 legacy wrapper
handoff snapshot/commit snapshot 不含 runtime/legacy wrapper
缺少 activate capability 时在 preparing 前拒绝
duplicate commandToken 不重复切歌或 seek
```

---

## 15. 建议提交拆分

### Commit 1

```text
Add handoff lifecycle invariant tests
```

先增加 authority generation、A -> B -> A 历史 handoff、终态不可回退和并发胜负规则的
失败测试，不改业务逻辑。

### Commit 2

```text
Add durable PlaybackContext handoff generations and schema
```

增加三种数据库 schema/migration、正式列、约束、索引和 legacy committed 归一化。

### Commit 3

```text
Serialize PlaybackContext mutations and add database CAS
```

实现 per-context operation boundary、统一锁顺序和 provider-compatible CAS。

### Commit 4

```text
Make handoff completion durable and recoverable
```

实现 canonical DB transaction、authorityGeneration、lastCommittedHandoffId 和
reconciliation。

### Commit 5

```text
Preserve handoff deadlines across restart
```

修复 prepare/complete deadline 延长，并增加 startup scan/fake clock 测试。

### Commit 6

```text
Rebase handoffs without replacing newer playback state
```

实现字段级 freshness、pause/seek rebase、complete 白名单和 superseded 规则。

### Commit 7

```text
Activate handoff targets only after authority commit
```

实现 post-commit activate/release、commandToken 和重连重放。

### Commit 8

```text
Close PlaybackContext atomically and preserve tombstones
```

实现 closed lifecycle、abort active handoff、authority 清空和幂等 close。

### Commit 9

```text
Harden PlaybackContext v2 identity and capabilities
```

修复 handoffId/requestId 竞态、deviceSessionId、controller 权限和 capability gate。

### Commit 10

```text
Track playback presence and authority availability
```

实现启动 offline reset、固定 grace deadline、断线 handoff 规则和 cleanup。

### Commit 11

```text
Normalize PlaybackContext v2 responses and events
```

实现 capability-based serializer、统一 ACK、事件和具体错误码。

### Commit 12

```text
Enforce realtime single-owner deployment and update client docs
```

增加已知多 worker fail-fast、部署说明、Flutter 文档、provider integration verification 和
Docker build 验证。

---

## 16. Definition of Done

本 Goal 完成必须满足：

- stabilized strict-v2 handoff complete 使用 canonical 数据库事务；
- 所有 context mutation 使用同一个 per-context 串行化/CAS 边界；
- authorityGeneration 和 lastCommittedHandoffId 可以区分当前提交与历史 handoff；
- strict-v2 authoritative mutation 使用 authorityGeneration fencing；
- A -> B -> A 后重试第一条 handoff 不会把 authority 修回 B；
- handoff 与最新 committed context generation 不会持久化为矛盾状态；
- 事务提交前 target 不会收到 activate，也不会自认为 authority；
- 事务提交后的 activate/release 可以在重启和重连后幂等重放；
- context 已前进到更高 version/epoch 时不会重放旧 committed activate snapshot；
- 重启不刷新 handoff deadline；
- authority grace deadline 也不会因重启或重复 disconnect 刷新；
- paused/stopped handoff 不会自动播放，pause/seek 使用最新 canonical 状态；
- context close 原子 abort 活动 handoff、清空 authority，并在事务后释放设备；
- context create 原子建立 generation 和 authority device projection；
- duplicate close 不提升版本；
- closed context tombstone 阻止 ID 复用；
- 旧歌曲快照不能覆盖新歌曲；
- target complete 不能提交 queue/track/state 等 authoritative context 字段；
- handoffId/requestId 并发创建不能覆盖或产生第二条幂等记录；
- strict-v2 的 deviceSessionId 必须与注册设备一致；
- handoff ready/complete 必须匹配持久化 targetDeviceSessionId；
- source、target、origin 和同用户 controller 的取消权限有明确测试；
- strict-v2 target 必须声明 playbackHandoffV2 和 playbackHandoffActivate；
- 在线 source 必须支持 playbackHandoffV2 generation fencing；
- authority 离线状态对客户端可见；
- 服务启动不会把旧进程留下的 online=true 当作当前在线；
- state、lifecycleState、authorityStatus 不再混用；
- legacy expired/closed context 正确迁移到 lifecycleState=closed；
- complete ACK 可以同时表达原提交结果和当前 canonical authority；
- mixed legacy/strict-v2 客户端按接收方 capability 获得唯一事件形状；
- legacy mutation 必须先映射到 canonical 字段，不能把原始 payload 写入 v2 context；
- SQLite、MySQL、PostgreSQL schema/migration 和事务路径均验证；
- 已知 `/emo` 多 worker 配置启动失败；
- 新增全部异常、竞态和恢复测试；
- Python 支持版本的 CI 全部通过；
- Docker Build 通过；
- Flutter 接入文档与最终服务端实现完全一致。

---

## 17. 非本 Goal 范围

本次不处理：

- Redis 分布式实时状态；
- Socket.IO 多 worker 横向扩展；
- 自动检测所有外部多容器误部署；这类部署明确标记为 unsupported；
- 跨账号共享播放；
- 云端永久播放历史；
- 自动选择最佳 handoff 目标；
- 音频流级无缝拼接；
- 多设备毫秒级同步群播算法重写；
- Flutter UI 大规模重构。

本 Goal 也不建设通用 durable command outbox，但必须持久化 handoff-specific
activate/release intent，用于完成事务后的命令重放。

legacy handoff 继续兼容现有行为，但新的原子 generation、activate-after-commit 和命令
重放保证只适用于声明 `playbackHandoffV2` 的 stabilized strict-v2 路径。

本 Goal 只负责把现有 PlaybackContext v2 和 Handoff 做到状态可靠、可恢复、可验证。
