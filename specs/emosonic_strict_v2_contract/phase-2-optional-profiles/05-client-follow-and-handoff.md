# 阶段 2：客户端 Follow 与 Handoff 请求

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-07-23-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 5.3—5.4 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
### 5.3 Follow（仅 negotiated capability `supportsFollow:true`）

Follow 是 Context 驱动的一对一软同步音频跟随，不是只读观察。一个 follower 同时只跟随一个
`sourcePlaybackContextId`，一个 source Context 可以有多个 follower。follower 保存并暂时冻结本机原任务，
应用 source queue/state/position/playbackRate 并在本地修正 drift，但不取得 source Context 控制权；退出
后恢复本机原任务。Follow 不建立 Broadcast revision、deliveryId、feedback deadline 或 participant
ledger，也不发送 `follow.feedback`。

只有 follower 连接协商到 `supportsFollow:true` 且服务端该 profile 全部 conformance 已通过，服务端
才可接受 Follow。该 capability 是复合承诺，要求 follower 同时满足：

```text
role includes player
playbackContextV2:true
effectiveAtPlayback:true
canPlay:true
canPause:true
canSeek:true
支持并保持任意合法 0.5..2.0 playbackRate
```

固定十字段 capability shape 不增加 rate capability。不能满足任一项时 `supportsFollow` 必须协商为
false，请求返回 `capability_required`。

| Action / type | payload | 服务端动作与响应 |
| --- | --- | --- |
| `follow.start` / `command` | `sourcePlaybackContextId:R`、`deviceSessionId:R` | 当前 authenticated follower exact pair 建立唯一 relationship/subscription/SafetyLease/fence。相同 source 和相同 acquiring baseline 的重试幂等重放首次 ACK；已 Follow 另一 source 时返回 `conflict`，不得隐式切换。成功 ACK 使用下文闭合 baseline shape，客户端再显式读取 source status。 |
| `follow.stop` / `command` | `sourcePlaybackContextId:R` | 只有创建该 Follow 的 exact pair 可停止；重复 stop 幂等重放 ACK。服务端只在客户端已经安全恢复或明确 cleanup 时释放 SafetyLease/fence。 |

Follow ownership 绑定 `(authenticated user, followerClientId, followerDeviceSessionId,
sourcePlaybackContextId, suspendedPlaybackContextId)`。Follow 不授予 source 控制、Handoff 或
Broadcast participant 权限。

#### 5.3.1 Source eligibility 与 current physical fact

source 不要求 `supportsFollow:true`，但必须是 source Context 当前在线 authority player，并满足
`playbackContextV2:true`、`effectiveAtPlayback:true`、当前 Socket clock gate 有效、playbackRate 在
`0.5..2.0`，且 source Context 与 follower suspended Context 不同、source authority exact pair 与
follower exact pair 不同。source fact 无论 playing/paused/stopped 都必须属于当前物理连接：

```text
sourceClientId == authorityClientId
deviceSessionId == authorityDeviceSessionId
internal fact.connectionNonce == 当前 source Socket nonce
Context epoch 匹配
appliedControlVersion == controlVersion
```

状态门禁固定为：

```text
playing：
  queue-backed、settled、track/index/rate 匹配
  serverUpdatedAtMs 与 positionSampledAtServerMs 年龄都 <= 2000ms

paused/stopped：
  queue-backed、settled、track/index/rate 匹配
  不套用 playing 的 2000ms 进度 freshness

idle：
  follow.start -> queue_required，零副作用
```

服务端不得用旧 nonce、旧 authority session、未结算 target 或全局设备事实建立 Follow。playing fact
过期返回带 source 四 cursor 的 `conflict`。self-follow 返回 `conflict`，不得建立 relationship 或 fence。

#### 5.3.2 Preflight、RecoveryRecord 与 start ACK

Flutter 发送 start 前必须：解析自身 exact pair 的唯一 active suspended Context；确认
appliedControlVersion==controlVersion、无 pending control、无 active Core prepare/Handoff/Broadcast/
Follow/restore fence；确认本地 command lane 与 AudioExecutionLease 空闲；捕获原 queue/index/position/
state/rate、suspended Context exact authority pair 与 cursors。随后必须先持久化：

```text
FollowRecoveryRecord
  sourcePlaybackContextId
  suspendedPlaybackContextId
  suspendedAuthorityClientId
  suspendedAuthorityDeviceSessionId
  suspendedEpoch
  suspendedVersion
  suspendedQueueRevision
  suspendedControlVersion
  suspendedAppliedControlVersion
  original queue/index/position/state/playbackRate
  phase: acquiring|active|stopPending|restoring
  relationshipAcquired
```

acquiring record 写成功后才允许发送 `follow.start`。服务端在一个事务中冻结 baseline，建立
relationship、subscription、`FollowSafetyLease` 和 occupancy fence，然后 ACK：

```json
{
  "type": "system",
  "action": "system.ack",
  "requestId": "follow-start-1",
  "payload": {
    "action": "follow.start",
    "status": "active",
    "sourcePlaybackContextId": "playback:user:source",
    "suspendedPlaybackContextId": "playback:user:follower",
    "suspendedAuthorityClientId": "follower-1",
    "suspendedAuthorityDeviceSessionId": "device:follower-1",
    "suspendedEpoch": 1,
    "suspendedVersion": 9,
    "suspendedQueueRevision": 7,
    "suspendedControlVersion": 11,
    "suspendedAppliedControlVersion": 11
  }
}
```

除通用 provenance 外，ACK payload 必需且只允许上述字段。Flutter 必须逐字段比较 ACK baseline 与
acquiring record；完全匹配才把 record 改为 active 并触碰音频。不匹配时不进入 overlay，立即幂等
`follow.stop`，再读取服务端当前 status 收敛。acquiring record 写失败时不发送 start；ACK 已到但 active
record 更新失败时不触碰音频并立即 stop，stop 结算未知时写 stopPending 后断开 Socket。

#### 5.3.3 Persistent FollowSafetyLease、fence 与资源上限

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

SafetyLease 只恢复安全 fence，不代表服务端重启后自动恢复音频 Follow。一个 exact follower pair 最多一
条非终态 lease，一个 suspended Context 最多被一个 Follow overlay 占用，每 user 最多 256 条
active/reconnectGrace/cleanupRequired record。达到上限时新 start 返回 `rate_limited`；已有 relationship
重试和 stop/cleanup 始终允许。不得删除旧未完成 lease 来接受新 lease。cleanupRequired tombstone 保留
到 exact pair 完成 follow.stop 或管理端 decommission，不按普通 TTL 静默删除。

active、terminating、reconnectGrace、cleanupRequired 阶段必须阻止 suspended Context 的：

```text
player.* / queue.playItem / queue.context.sync
playback.context.prepare
playback.context.prepared（cleanup negative confirmation 除外）
playback.update
playback.context.close
会创建、初始化、重绑或修改 snapshot 的 ensure
Handoff source/target
Broadcast source/ordinary
另一个 Follow source
```

命中时返回 `conflict` 与 suspended Context 四 cursor，零副作用。list/status/subscribe/unsubscribe、
follow.stop、device volume/list、system.ping 和其他纯读取/clock 可以继续。

#### 5.3.4 Mirror execution、local failure 与 source idle

Follow overlay 消费 source canonical queue/context/device facts并在本地软同步。Follow 模式禁用本机
play/pause/seek/next/prev 与 queue mutation，只保留本机音量和“停止跟播”；服务端 fence 同时阻止其他
controller 修改 follower suspended Context。mirror execution 不发送普通 `playback.update`，不写 source
Context 或 follower 原 Context，也不新增 Follow feedback action。

source 可以同时被多个 follower 跟随，也可以同时作为一个 Broadcast source；Follow follower、
Broadcast ordinary participant 与 Handoff target preparing/ready/committing 是同一设备上的互斥 overlay。

active source 转为 idle 时，follower 停止并清空 mirror audio，但保持 relationship/SafetyLease/fence，
不恢复原任务，静默等待 source 再次 queue-backed。source 恢复 queue-backed 后按新 canonical fact 继续。

本地 queue load、seek、rate、play/pause 或媒体应用失败时不得发送 normal playback.update：触碰音频前
失败则不进入 overlay、保持/恢复原任务并 follow.stop；已经进入 overlay 后失败则停止 mirror、恢复原
任务，恢复成功后 stop。恢复失败时保持非播放，保留 RecoveryRecord/SafetyLease/fence，不发送 stop，
只做有界重试或断开 Socket。

#### 5.3.5 Stop、断线、重连与服务端重启

Flutter 只在当前进程内存保存 `followResumeIntent`；`followStopPending` 必须持久化且只允许继续退出。
停止时先将 phase 写为 restoring，忽略 source mirror，取消 drift timer/seek/rate correction，读取服务端
当前 suspended Context status，并比较 SafetyLease 冻结 baseline：

- binding 与 cursors 仍完全相等时，可以恢复本地原快照；
- 任一 cursor 更高、binding 改变或 Context closed 时，丢弃旧本地快照并采用服务端当前 status，绝不
  把旧 queue/cursor 写回。

恢复成功后才发送 follow.stop；服务端 ACK 后原子删除 relationship/subscription/SafetyLease/fence，
最后清除 RecoveryRecord。fence 释放后才允许 normal playback.update。

必须区分 `followSourceRecoveryDeadlineAtMs` 与 `followReconnectGraceExpiresAtMs`。source authority
离线、playing fact 过期或 status 恢复失败时进入最多 30 秒 source recovery window；source Context
closed 时立即结束并恢复原任务。follower Socket 断开时 lease 进入 reconnectGrace：同一应用进程、同一
exact pair 重连后可以复用 acquiring baseline 重发 start；stopPending 只能重试 stop。grace 到期且 pair
离线时可释放 active in-memory fence，但必须保留 cleanupRequired tombstone，阻止旧 pair 下次注册后
直接写 suspended Context。

服务端重启必须重新加载 SafetyLease，把非终态 lease 恢复为 reconnectGrace/cleanupRequired 并恢复
suspended Context fence；不得自动恢复 mirror audio。应用进程重启后也不得自动恢复音频 Follow，只能
从 RecoveryRecord 执行安全 cleanup/stop。相同 pair 的 start 或 stop 完成前，普通写保持关闭。

### 5.4 Handoff（target 需 negotiated `playbackPrepare:true` 且 `effectiveAtPlayback:true`）

Handoff 发起者必须具有 controller 角色；source 必须是当前在线 authority 且
协商 `canPause:true`、`effectiveAtPlayback:true`，通过 current Socket clock gate，并拥有 fresh、
settled、queue-backed、state=playing 的 current physical fact：track/index 匹配、
appliedControlVersion==controlVersion、没有 pending control。idle source 返回 `queue_required`；
paused/stopped/stale/unsettled source 返回无副作用 `conflict`。target 必须是同用户在线 exact pair 的
player，并协商 `playbackContextV2:true`、`playbackPrepare:true`、`effectiveAtPlayback:true`、
`canPlay:true`、`canPause:true`、`canSeek:true`，支持并保持 0.5..2.0 rate，且通过 current Socket clock
gate。只有服务端 Handoff conformance tests 已通过时才
开放该 profile，否则 negotiated handoff capabilities 为 false，请求返回 `capability_required`。

`playback.handoff.start.payload.targetClientId/targetDeviceSessionId` 是 Context/Handoff surface 的 payload target 例外；
另一个设备级例外是第 5.2 节 `device.setVolume` 的精确 client/device pair。handoff target 只用于让服务端选择接管设备；服务端必须解析并授权该目标，然后按目标
Socket 投递无 `targetClientId` 的 `playback.prepare`。不得把该请求字段复制进任何业务 push。

| Action / type | payload | 服务端动作与响应 |
| --- | --- | --- |
| `playback.handoff.start` / `command` | `playbackContextId:R`、`targetClientId:R`、`targetDeviceSessionId:R`、`baseControlVersion:R int>=1` | 原子创建前按当前用户域解析 exact target pair，验证 source/target eligibility、全部 fence、target command lane/lease 与唯一 Context。target 没有 Context 可继续；只有 idle Context 时记录待退休 standby；非 idle/active prepare 返回 conflict。成功后 ACK 并给 target 发第 6.8 节 prepare；ACK payload **只允许且必须**有 `action`、`handoffId`、`prepareId`、`status:"preparing"`、`controlVersion:N`。 |
| `playback.ready` / `event` | `playbackContextId:R`、`prepareId:R`、`handoffId:R`、`deviceSessionId:R`、`ready:R bool`、`errorCode:C string`、`errorMessage:O string` | 只有 frozen target 当前 physical Socket 可确认，不回 ACK。`ready:true` 禁止 error 字段；`ready:false` 要求 errorCode。成功进入 ready，失败进入 failed；duplicate/late 只重放 canonical status。 |
| `playback.handoff.complete` / `event` | `playbackContextId:R`、`handoffId:R`、`deviceSessionId:R`、`queueIndex:R int>=0`、`trackId:R`、`state:R "playing"`、`positionMs:R int>=0`、`positionSampledAtServerMs:R int>=0`、`playbackRate:R number 0.5..2.0`、`appliedControlVersion:R int>=1`、`clientSeq:R int>=1` | frozen target commit 后提供完整 actual proof，不回 ACK。服务端通过第 5.4.3 节 proof 后在这里原子退休 standby、写 target DevicePlaybackState、切 authority/cursors，广播 completed status 和 Context status，再立即向旧 source release。 |
| `playback.handoff.cancel` / `command` | `playbackContextId:R`、`handoffId:R`、`reason:O non-empty string`、`errorCode:C string`、`errorMessage:O string` | controller cancel 幂等；target commit 执行失败必须发送 `reason:"commit_failed"`、`errorCode:"commit_failed"`，errorMessage 可选。ACK 并向相关成员发送 canonical cancel/status。 |

#### 5.4.1 Full lifecycle fence 与 continuity-first policy

start 成功必须同时建立 source Context fence、target exact pair fence 和 target standby Context fence，
从 preparing 覆盖 ready/committing 到 terminal。source 在 complete 前继续播放，正常 position/sample
前进允许；remote player/queue/close、第二个 Handoff、Broadcast start/control 和 authority/binding
mutation 返回 source 四 cursor `conflict`。source localUser、自然结束/切歌或实际 track/state/rate 变化
时，服务端必须在同一临界区先把 Handoff 结算为 `failed/source_changed`、通知 target 取消 timer/lease，
再提交 source actual mutation。

target 本地 UI 在 preparing/ready/committing 禁用 play/pause/seek/next/prev、queue mutation 和新的
Follow/Broadcast/Handoff，只保留 device volume 与 Handoff cancel/failure cleanup。target complete 前
source 保持 authority 和音频；complete 原子成功后 authority 才切换，并立即向 source 发送 release。
该 continuity-first 策略允许极短重叠，不承诺零重叠。

#### 5.4.2 Independent provisional execution lane

prepare 成功后、commit 前，服务端重新读取 source。正常 position/sample 前进不算 source_changed；
track/state/rate/binding/cursor/pending 变化必须先结算 source_changed。commit 的 `controlVersion=N+1`
是 `(playbackContextId, epoch, handoffId)` provisional version，不是 canonical cursor。它必须进入独立
Handoff execution lane，不进入普通 ControlTransactionCoordinator/reducer，不发送 remoteCommand
playback.update，也不携带普通 `executionTimeoutMs/dependsOnControlVersion`；只由 complete/cancel/
timeout 结算。

Flutter 只有在 completed status/Context status 后才把 N+1 当 canonical。failed/cancelled/timedOut 且
target lease 失效后 canonical 仍为 N，下一普通 mutation 可以重新分配 N+1。任何旧 ready/commit/
complete 必须同时匹配 handoffId、frozen exact pair、原 physical Socket 和非终态 lifecycle。

#### 5.4.3 Complete proof、position projection 与 authority switch

服务端用最新 source sample 生成 commit position。已知 duration 且 projectedPositionMs>=durationMs 时，
必须在发送 commit 前 fail-fast 为 `failed/source_changed`，authority 保持 source。

complete 必须验证 frozen target current Socket/pair、queueIndex/track/rate、
`appliedControlVersion==provisional N+1`、state=playing、known duration bounds、target clock gate，以及：

```text
positionSampledAtServerMs <= currentServerTimeMs + 50
currentServerTimeMs - positionSampledAtServerMs <= 2000
positionSampledAtServerMs >= effectiveAtServerMs
positionSampledAtServerMs - effectiveAtServerMs <= 1000

deltaMs = max(0, positionSampledAtServerMs - effectiveAtServerMs)
expectedPositionMs = commit.positionMs + floor(deltaMs * playbackRate)
known duration 时 expectedPositionMs = min(expectedPositionMs, durationMs)
abs(positionMs - expectedPositionMs) <= 1000
```

50ms future、1000ms execution late、1000ms position tolerance 是三个独立门槛。complete.clientSeq 复用
target 后续普通 playback.update 的 `(playbackContextId,targetClientId,connectionNonce,connectionEpoch)`
作用域并消耗序号。

proof 成功时原子退休 target standby、写完整 target DevicePlaybackState、把 authority exact pair 切到
target、令 `epoch += 1`、`version += 1`、`queueRevision` 不变、`controlVersion=N+1` 并将 Handoff 标记
completed。completed status/release 必须同时包含 newAuthorityClientId 与
newAuthorityDeviceSessionId；complete 是唯一 authority switch point。

Handoff 状态机固定为：

```text
preparing -> ready -> committing -> completed
     |         |          |
     +---------+----------+-> failed | cancelled | timedOut
```

- `preparing` 从 start ACK 起最多 8 秒；未收到有效 ready 时进入 `timedOut`。
- `ready` 后服务端重新验证 source 并给 target 发送第 6.9 节完整 provisional commit，随后进入
  `committing`；`effectiveAtServerMs - serverTimeMs >= 250`。
- `committing` 最多 5 秒；未收到 complete 时进入 `timedOut`，authority 不变。
- target 在 complete 前断开：`failed`；source 在 complete 前断开：`cancelled`；两者都不得
  切 authority。complete 的原子事务才是 authority switch point。
- target Socket disconnect 或服务端 restart 时，target Flutter 必须立即取消 scheduled timer、失效
  Handoff execution lease，已经起播则暂停，并禁止迟到 complete；不得等待 5 秒 timeout 才清理。
- target 在 start 时拥有 idle standby Context 时，complete 原子事务必须先把 standby 写入 terminal
  tombstone，再把 source Context 绑定到 target；两步必须同成同败，并为 standby close、旧 source pair
  和新 target pair 发送对应 invalidation。target Context 已非 idle 或存在 active prepare 时不得进入
  authority switch。
- `completed`、`failed`、`cancelled`、`timedOut` 是终态。终态重放不产生副作用。
- duplicate start 重放首次 preparing ACK；duplicate/late ready 只重放 canonical status；duplicate
  complete 重放 completed status 与当前 Context status，不重复退休 standby、切 authority 或 release。
  prepare/commit 不能可靠加入 frozen target 当前发送路径时必须立即 failed，不等待超时。
- `playback.handoff.status` / `cancel` 发给全部当前 Context subscribers；prepare 与 commit 只发
  target，release 只发 completed 后的旧 authority。complete 后再广播新的 Context status。
- 旧 source 收到 release、completed status、新 Context status 或 bindings.changed 任一权威事实时，
  必须立即失效旧 authority/audio execution lease并停止；不能依赖单一消息必达。
- authority 永久离线时，2.8.x 不提供强制接管。controller 关闭旧 Context，目标 player 继续使用自己
  ensure 得到的唯一 Context；旧 ID 因 tombstone 不可复用。

Handoff `errorCode` 必须匹配 `^[a-z][a-z0-9_]{0,63}$`。服务端标准值固定为
`prepare_failed`、`prepare_timeout`、`commit_timeout`、`target_disconnected`、
`source_disconnected`、`server_restart`、`source_changed`、`commit_failed`。target 可在 `playback.ready.ready:false` 中返回符合相同
格式的稳定扩展码。`errorMessage` 不得包含凭据、文件路径、堆栈或内部数据库信息。

Handoff start、ready、complete、cancel 的每次状态推进都必须先检查第 5.3、5.5 节 Follow/Broadcast 写屏障：source
Context 正在作为非终态 Broadcast source，或 source/target pair 的 Context 正在作为 ordinary
participant/Follow follower suspended Context，或任一 source/target pair 存在 restorePending 时返回 `conflict`；不得
准备、切 authority、退休 standby 或发送 release。
