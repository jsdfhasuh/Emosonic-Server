# 阶段 2：服务端 Broadcast 推送与 Status

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-07-23-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 6.10 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
### 6.10 Broadcast 推送与 status 结果

本节所有 `broadcast.*` server push 的 envelope `type` 固定为 `event` 并省略 requestId；同名客户端
request 仍使用第 5.5 节规定的 `command|state|event` type，不得因名称相同混淆方向。

`broadcast.start` 与 `broadcast.stop` lifecycle push 发给 source authority、controllerOnly owner 当前 Socket 和全部
ordinary participants；`broadcast.play`、`broadcast.pause`、`broadcast.seek`、`broadcast.playItem`、
`broadcast.queue.sync`、`broadcast.progress`、`broadcast.state.sync`、`broadcast.waiting`、
`broadcast.resume` 发给 ordinary participants，并向 controllerOnly owner 当前 Socket 发送观察副本。
`broadcast.resync` 只发给非终态 Broadcast 中发生物理重连的单个 frozen ordinary client/device pair，不发给其他
participants、source 或 controllerOnly；`broadcast.feedback.rejected` 只发给提交无法匹配 ledger 的
ordinary 请求 Socket。source 重连时收到的 `broadcast.resume` 只恢复 lifecycle/UI，不执行音频。
owner 不在线时不延迟 mirror 分发；重连后由已持久化 broadcastId 查询
status 恢复 UI。owner 观察副本不创建 targetBroadcastRevision/deadline，不要求 feedback。source
authority 的音频执行只接收第 6.7 节普通 Context command，绝不能同时收到 Broadcast mirror
control。owner 与 source 或 ordinary participant 为同一 Socket 时不再投递 controllerOnly 观察副本，
并按 sourceAuthority > ordinaryParticipant > controllerOnly 的顺序决定音频处理分支。所有 push 使用逐
recipient 构造的无 target envelope。上述 lifecycle/mirror/observation push 的 payload 必须以完整
`BroadcastSnapshot` 为基础，并按收件角色增加下文 per-delivery 元数据；`broadcast.restore`、
canonical `broadcast.feedback` confirmation 与 `broadcast.feedback.rejected` 分别使用本节后文的独立
闭合 shape：

`participants` 与 participantStates 的 exact client/device membership 在 start 提交时冻结，到 terminal
保持不变。断线只改变 online；resync 不增加、删除或替换成员。r18 没有 leave/add/remove push 或
status mutation。

```json
{
  "playbackContextId": "playback:user:main",
  "broadcastId": "broadcast-1",
  "intentId": "broadcast-start-1",
  "ownerClientId": "controller-1",
  "authorityClientId": "phone-1",
  "authorityDeviceSessionId": "device:phone-1",
  "lifecycleState": "active",
  "broadcastRevision": 14,
  "queueSongIds": ["song-1", "song-2"],
  "currentIndex": 0,
  "trackId": "song-1",
  "positionMs": 1200,
  "state": "playing",
  "playbackRate": 1.0,
  "sourceVersion": 10,
  "sourceQueueRevision": 8,
  "sourceControlVersion": 13,
  "sourceEpoch": 1,
  "serverUpdatedAtMs": 1780000001200,
  "participants": ["desktop-1"]
}
```

上述基础字段全部必需；`trackId` 必须等于 queueSongIds[currentIndex]。Broadcast 只允许 queue-backed source，
因此 queueSongIds 非空、currentIndex/trackId 必需，state 只允许 `playing|paused|stopped`，playbackRate
必须是有限 number 且在 `0.5..2.0`。participants 只含 ordinary clientId，按 clientId 升序，不得包含
authorityClientId。`intentId` 必须等于首次成功 start request 的 intentId，并在 active、
waitingForSource、terminal status 和所有 push 中保持不变。

`lifecycleState` 只允许 `active|waitingForSource|stopped`：

- active：state/position/playbackRate 是当前 source target 或最近已结算实际 anchor；
- waitingForSource：source Context/cursors 不变，state 必须为 paused，positionMs 是 source 断线时计算的
  安全 pause anchor；它只暂停 ordinary mirror，不声称 source Context 已暂停；
- stopped：terminal lifecycle。state/position/playbackRate 保留停止前最后 mirror anchor；手工 stop 时
  source 仍可能是 playing。禁止用 `state:"stopped"` 代替 lifecycle terminal，也禁止因 stop 修改
  source Context。ordinary terminal applied feedback 的 `state:"stopped"` 是镜像 execution 已销毁的
  结果字段，不要求等于此处保留的 Snapshot.state。

`sourceVersion/sourceQueueRevision/sourceControlVersion/sourceEpoch` 在 active/waitingForSource 期间必须精确
等于 snapshot 所引用 source Context 的 canonical cursors。接受 source playback mutation 时，服务端在
同一事务更新 source Context、这些带名副本和 broadcastRevision；waiting/resume 只递增
broadcastRevision，不改变 source cursors。stop 将这些字段冻结为 terminal 提交时所引用的
source cursor 历史值；后续 source Context mutation 不更新 terminal snapshot。任何 push 禁止旧的无前缀
`version/queueRevision/controlVersion/epoch`，避免把
Broadcast 误实现为第二套播放时间线。

发给 ordinary participant 的每个 execution target 必须在上述基础 snapshot 外增加服务端生成的
`deliveryId:non-empty string`；相同 target 发给不同 pair 的 deliveryId 不同。sourceAuthority lifecycle
副本和 controllerOnly 观察副本必须省略 deliveryId，不能产生 participant target/deadline。`broadcast.start`、
`play`、`pause`、`seek`、`playItem`、`queue.sync`、`progress`、`state.sync`、source 重连后的
`resume` 以及 ordinary `resync` active mirror push，还必须增加 `effectiveAtServerMs:int>0` 与
`serverTimeMs:int>=0`，且 `effectiveAtServerMs - serverTimeMs >= 250`。deliveryId 和两个时间字段都是
本次分发的 push-only 元数据，不写入持久化 BroadcastSnapshot，也不出现在
`broadcast.status.broadcast` 中。同一新 broadcastRevision 首次发给所有 ordinary participants 的两个
时间值必须逐值相等；若该 revision 来自
`broadcast.play/pause/seek/playItem`，还必须逐值等于发给 source authority 的第 6.7 节普通 command。
source 已在播放的 start 仍不得让 source 重放 snapshot；服务端应把 playing 的 `positionMs` 投影为
effective-at 时刻的源位置，ordinary participants 在该时刻应用该位置和 playbackRate。对于每个新
broadcastRevision 的计划 target，playing position 必须投影到 effectiveAtServerMs；paused/stopped
position 不推进，但 Snapshot.serverUpdatedAtMs 仍必须严格等于 effectiveAtServerMs。也就是说，所有
带 effective-at 的新 Snapshot 都以 effectiveAtServerMs 作为 position 时间锚点。`broadcast.waiting`
和 terminal stop 可省略 effective-at；此时 serverUpdatedAtMs 使用该次 canonical 提交时间。
`broadcast.resync` 在 active 状态无论 state 为 playing/paused/stopped 都必须重新生成两个时间字段；
waitingForSource 可以省略。resync 只针对单个 pair 重新计算 deliveryPositionMs 和本次时间值，不要求
等于该 revision 的首次分发时间，且不得改写 Snapshot.positionMs 或 Snapshot.serverUpdatedAtMs。
BroadcastSnapshot 中的 `serverUpdatedAtMs` 是 positionMs 所对应的 server-time anchor。它不是 source
DevicePlaybackState 的接收时间；status 返回该持久化 anchor，不得把投影位置写回或替换它。每个较新的业务/
lifecycle push 必须
在持久化内容并准备实际分发时严格执行一次 `broadcastRevision = previousBroadcastRevision + 1`；start
固定为 1，合并进度、控制目标、source 实际修正、waiting/resume 和 terminal 各自每次实际 push 都只
加 1，不得跳号、空增或复用旧 revision 承载新内容。向多个 recipients 分发同一次提交不重复加号；
同 revision 的持久化 Snapshot 必须逐字段相同；ordinary 重连时 push action 改为
`broadcast.resync`，且只允许 per-pair deliveryId 与 delivery 计划时间改变。纯传输重发同一个 delivery
attempt 时必须复用原 action、deliveryId 和计划时间。

ordinary pair 重连的 server-only resync 示例：

```json
{
  "type": "event",
  "action": "broadcast.resync",
  "connectionNonce": "recipient-current-nonce",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "broadcastId": "broadcast-1",
    "intentId": "broadcast-start-1",
    "ownerClientId": "controller-1",
    "authorityClientId": "phone-1",
    "authorityDeviceSessionId": "device:phone-1",
    "lifecycleState": "active",
    "broadcastRevision": 14,
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "trackId": "song-1",
    "positionMs": 1200,
    "state": "playing",
    "playbackRate": 1.0,
    "sourceVersion": 10,
    "sourceQueueRevision": 8,
    "sourceControlVersion": 13,
    "sourceEpoch": 1,
    "serverUpdatedAtMs": 1780000001200,
    "participants": ["desktop-1"],
    "deliveryId": "delivery:desktop-1:14:2",
    "effectiveAtServerMs": 1780000010250,
    "serverTimeMs": 1780000010000
  }
}
```

客户端必须用 Snapshot 的 positionMs/serverUpdatedAtMs/playbackRate 投影到本次 effective-at；不得把
新计划时间当成 revision 内容，也不得重新捕获原任务。`broadcast.resync` 是 server-only action，客户端
发送同名请求返回 `not_supported`。

完整 terminal record 按第 5.5.2 节压缩后，服务端只向尚未确认的相同 ordinary
client/device pair 发送以下 server-only recovery push：

```json
{
  "type": "event",
  "action": "broadcast.restore",
  "connectionNonce": "recipient-current-nonce",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "broadcastId": "broadcast-1",
    "deviceSessionId": "device:desktop-1",
    "terminalBroadcastRevision": 20,
    "deliveryId": "delivery:desktop-1:terminal:2",
    "suspendedPlaybackContextId": "playback:user:desktop",
    "suspendedEpoch": 1,
    "suspendedVersion": 9,
    "suspendedQueueRevision": 7,
    "suspendedControlVersion": 11,
    "suspendedAppliedControlVersion": 11,
    "lastAppliedBroadcastRevision": 19,
    "queueIndex": 0,
    "trackId": "song-1",
    "state": "stopped",
    "positionMs": 32000,
    "playbackRate": 1.0,
    "terminalAtServerMs": 1780000100000
  }
}
```

`broadcast.restore` payload 必须且只允许上述字段；deliveryId 必须等于 TerminalRecoveryRecord 当前
delivery attempt，state 固定为 `stopped`，queueIndex/trackId/
positionMs/playbackRate 是 terminal mirror target，不是原任务实际状态。它不含 queueSongIds、owner/source
身份或 participantStates，不增加 broadcastRevision，不重新解除屏障。Flutter 必须把它当作同一
broadcastId/terminal revision 的 terminal gate 输入，恢复原任务后使用 payload 中的 terminal target
发送第 5.5.2 节完整 applied feedback。客户端向服务端发送 `broadcast.restore` 必须返回
`not_supported`。

未确认 pair 的 terminal `broadcast.stop` / `broadcast.restore` 必须在任何 suspended Context 普通
command 之前可靠加入当前 Socket 发送路径。enqueue 失败时服务端立即断开该 Socket，并在下次注册
用新 deliveryId 先 replay；不得绕过 terminal delivery gate。成功 enqueue 或 status 读取都不清
restorePending，只有 matching terminal applied feedback 可以清除。

服务端接受 ordinary participant 的 `broadcast.feedback` 后，只向请求 participant 的当前 Socket 发送以下 canonical
confirmation，不向 owner、其他 participants 或 Context subscribers 广播：

```json
{
  "type": "event",
  "action": "broadcast.feedback",
  "connectionNonce": "recipient-current-nonce",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "broadcastId": "broadcast-1",
    "sourceClientId": "desktop-1",
    "deviceSessionId": "device:desktop-1",
    "deliveryId": "delivery:desktop-1:14:1",
    "executionStatus": "applied",
    "appliedBroadcastRevision": 14,
    "queueIndex": 0,
    "trackId": "song-1",
    "state": "playing",
    "positionMs": 1200,
    "playbackRate": 1.0,
    "clientSeq": 7,
    "serverUpdatedAtMs": 1780000001200
  }
}
```

failed confirmation 示例：

```json
{
  "type": "event",
  "action": "broadcast.feedback",
  "connectionNonce": "recipient-current-nonce",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "broadcastId": "broadcast-1",
    "sourceClientId": "desktop-1",
    "deviceSessionId": "device:desktop-1",
    "deliveryId": "delivery:desktop-1:15:1",
    "executionStatus": "failed",
    "failedBroadcastRevision": 15,
    "lastAppliedBroadcastRevision": 14,
    "errorCode": "track_load_failed",
    "errorMessage": "Unable to load target track",
    "clientSeq": 8,
    "serverUpdatedAtMs": 1780000002200
  }
}
```

confirmation 必须省略 requestId，并在第 5.5.2 节 applied/failed 条件字段之外增加服务端生成的
`sourceClientId` 与 `serverUpdatedAtMs:int>=0`，并原样回显请求的 `deliveryId`。`sourceClientId` 来自已认证 Socket，不得信任或接受
请求 payload 提供该字段；`deviceSessionId` 必须与当前注册连接及请求值完全一致。confirmation 的
executionStatus、revision、条件字段和 clientSeq 必须等于已原子写入 participantStates 的值；相同
clientSeq/content 重试只向请求 Socket 重放首次 confirmation，不重复写
participantStates。source authority 发送该 action 返回 forbidden。controller 只能用新的
`broadcast.status` request 观察 ordinary participant 状态。

当 feedback 已通过 envelope/schema/auth/pair/clientSeq 校验、但第 5.5.2 节的 revision/delivery ledger
分类失败时，服务端只向请求 Socket 发送以下 server-only rejection，
不再为同一请求发送 canonical `broadcast.feedback` confirmation 或 correlated system.error：

```json
{
  "type": "event",
  "action": "broadcast.feedback.rejected",
  "connectionNonce": "recipient-current-nonce",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "broadcastId": "broadcast-1",
    "deviceSessionId": "device:desktop-1",
    "clientSeq": 9,
    "deliveryId": "delivery:desktop-1:9:1",
    "rejectedBroadcastRevision": 9,
    "currentBroadcastRevision": 14,
    "minimumRetainedBroadcastRevision": 10,
    "errorCode": "revision_expired",
    "serverUpdatedAtMs": 1780000011000
  }
}
```

payload 必须且只允许上述字段；errorCode 只允许
`revision_expired|revision_unknown|revision_ahead`。currentBroadcastRevision 来自完整 Snapshot 或 terminal
recovery/tombstone，minimumRetainedBroadcastRevision 是该 pair 仍可验证的最小 revision，不得猜测为
客户端上报值；rejectedBroadcastRevision 分别取请求的 appliedBroadcastRevision 或
failedBroadcastRevision，deliveryId/clientSeq 原样回显。客户端收到 rejection 后必须停止重试旧
revision/delivery，并等待服务端紧随其后发送的新 `broadcast.resync|stop|restore` delivery；只有该新
delivery 是可执行输入。客户端可以用新 requestId 查询 `broadcast.status` 做诊断，但不得把 status 当作
新 delivery 或据此发送 applied feedback；若 status 返回 `not_found`，按第 5.5.3 节清理本地 lifecycle
或记录恢复数据丢失。下一条 feedback 必须针对新 deliveryId 并使用更高 clientSeq。客户端发送
`broadcast.feedback.rejected` 返回 `not_supported`。

`broadcast.status` ACK payload 是 full 或 recovery 两个互斥 one-of。完整 Broadcast 记录存在时只允许
以下 full 字段：

```json
{
  "action": "broadcast.status",
  "serverTimeMs": 1780000010000,
  "broadcast": {
    "playbackContextId": "playback:user:main",
    "broadcastId": "broadcast-1",
    "intentId": "broadcast-start-1",
    "ownerClientId": "controller-1",
    "authorityClientId": "phone-1",
    "authorityDeviceSessionId": "device:phone-1",
    "lifecycleState": "active",
    "broadcastRevision": 14,
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "trackId": "song-1",
    "positionMs": 1200,
    "state": "playing",
    "playbackRate": 1.0,
    "sourceVersion": 10,
    "sourceQueueRevision": 8,
    "sourceControlVersion": 13,
    "sourceEpoch": 1,
    "serverUpdatedAtMs": 1780000001200,
    "participants": ["desktop-1"]
  },
  "participantStates": [
    {
      "broadcastId": "broadcast-1",
      "clientId": "desktop-1",
      "deviceSessionId": "device:desktop-1",
      "targetBroadcastRevision": 14,
      "targetDeliveryId": "delivery:desktop-1:14:1",
      "deadlineBroadcastRevision": 14,
      "syncStatus": "applied",
      "feedbackDeadlineAtServerMs": 1780000009000,
      "online": true,
      "appliedBroadcastRevision": 14,
      "queueIndex": 0,
      "trackId": "song-1",
      "state": "playing",
      "positionMs": 1200,
      "playbackRate": 1.0,
      "appliedAtServerMs": 1780000001200,
      "lastFeedbackClientSeq": 7,
      "lastFeedbackAtServerMs": 1780000001200
    }
  ]
}
```

`broadcast` 必须是完整 `BroadcastSnapshot`。`participantStates` 必须覆盖最终 participants，并按
`clientId` 升序；同一 client 只能出现一次。每项基础必需且只允许：`broadcastId`、`clientId`、
`deviceSessionId`、`online`、`targetBroadcastRevision:int>=1`、
`targetDeliveryId:non-empty string`、
`deadlineBroadcastRevision:int>=1`、
`syncStatus:pending|applied|lagging|failed|timedOut`、`feedbackDeadlineAtServerMs:int>=0`，以及以下闭合
条件组：

- 曾有成功 feedback 时，成组输出 `appliedBroadcastRevision`、`queueIndex`、`trackId`、`state`、
  `positionMs`、`playbackRate`、`appliedAtServerMs`；applied revision 不得高于 target，queue/track 必须
  匹配该 revision ledger；appliedAtServerMs 等于服务端接受该成功 feedback 的 serverUpdatedAtMs；
- 收到任一 feedback 后，成组输出 `lastFeedbackClientSeq`、`lastFeedbackAtServerMs`；
- syncStatus=failed 时必需 `failedBroadcastRevision`、`errorCode`，`errorMessage` 可选；failed revision
  必须等于 target；
- syncStatus=timedOut 时必需 `timedOutBroadcastRevision == deadlineBroadcastRevision`、
  `errorCode:"feedback_timeout"`，并禁止 errorMessage；
- terminal applied 且 restorePending 已清除时额外输出 `restoreCompleted:true`；此时成组的
  queue/track/`state:"stopped"`/position/rate 是已销毁的 terminal mirror target，不是恢复后原任务的
  实际 transport。其他状态禁止 `restoreCompleted`。

`syncStatus:"applied"` 只表示该 frozen participant 已应用所报告 revision 的 target；Broadcast 是
soft sync，不保证随后与 source 的 drift 持续小于固定毫秒数。由于 feedback 没有 position sample
time，positionMs 只能验证 int、非负和已知 duration 范围，不能用于构造 drift SLA。

full ACK 顶层 payload 的 `serverTimeMs:int>=0` 是本次 status 读取的服务端时刻，不属于
BroadcastSnapshot，也不增加 broadcastRevision。`broadcast.positionMs` 与
`broadcast.serverUpdatedAtMs` 必须原样返回持久化 anchor；服务端不得为了 status 动态改写 positionMs。
客户端只在 lifecycleState=active 且 state=playing 时计算
`positionMs + max(0, serverTimeMs - serverUpdatedAtMs) * playbackRate`，再限制到媒体有效时长；这保证
计划尚未生效、serverTimeMs 小于 future anchor 时不会把位置向后推。paused、stopped 或
waitingForSource 不推进。相同 revision 的两次 status 因 serverTimeMs 不同仍不构成 Snapshot 内容变化。

初始 target 刚分发时使用 pending；没有合法 feedback 时，后续 target 不得重置已建 deadline
或 timedOut。没有成功 feedback 时必须省略全部实际 state/position/rate/track 字段，不得复制
source snapshot 冒充 participant 已执行。lagging 要求存在低于 target 的成功 applied 组。
failed/timedOut 后收到合法 applied 可改为 applied/lagging，并清除对应 failure/timeout 字段。
`deadlineBroadcastRevision` 与 `feedbackDeadlineAtServerMs` 在 applied/failed/timedOut 后继续输出最后关闭
值；此时计时器必须已停止，只有 pending/lagging 表示正在计时。
participantStates 不包含 source；source 实际状态通过 broadcast snapshot 与正常
playback.context.status.deviceStates 观察。

完整记录已压缩且请求 Socket 精确匹配尚未确认 ordinary pair 时，status ACK 必须改用
recovery one-of：

```json
{
  "action": "broadcast.status",
  "serverTimeMs": 1780000110000,
  "recovery": {
    "playbackContextId": "playback:user:main",
    "broadcastId": "broadcast-1",
    "deviceSessionId": "device:desktop-1",
    "terminalBroadcastRevision": 20,
    "deliveryId": "delivery:desktop-1:terminal:2",
    "suspendedPlaybackContextId": "playback:user:desktop",
    "suspendedEpoch": 1,
    "suspendedVersion": 9,
    "suspendedQueueRevision": 7,
    "suspendedControlVersion": 11,
    "suspendedAppliedControlVersion": 11,
    "lastAppliedBroadcastRevision": 19,
    "queueIndex": 0,
    "trackId": "song-1",
    "state": "stopped",
    "positionMs": 32000,
    "playbackRate": 1.0,
    "terminalAtServerMs": 1780000100000
  }
}
```

`recovery` object 必须与同 pair `broadcast.restore.payload` 逐字段相同；该 ACK 在 action/recovery 外
只允许 response-level `serverTimeMs`，禁止 `broadcast`、`participantStates` 或其他字段。owner、source、已确认 pair、不同 clientId/deviceSessionId 不得读取
他人的 compact recovery；完整记录已删除时对它们返回 `not_found`。

push action 的 payload 不含 `action`；correlated status ACK 的 payload 必须以
`action:"broadcast.status"` 开头。online 来自冻结 client/device pair 当前 Socket presence，不得由
feedback 控制。`deviceSessionId` 必须是 start 时冻结的 pair 值，不得随同 clientId 的新设备注册
改写。lastFeedbackClientSeq 只在第 4.5 节同一连接作用域内比较；participant 重连后可从 1 开始，但
appliedBroadcastRevision 仍不能低于该 participant 先前已确认 revision。旧 Socket feedback 因
nonce/epoch 不匹配不得覆盖。
