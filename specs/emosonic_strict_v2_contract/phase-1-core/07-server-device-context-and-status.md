# 阶段 1：服务端设备、Context 与 Status 消息

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-08-01-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 6 节引言及第 6.0—6.4 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
## 6. 服务端到客户端：strict 推送与直接响应

下面所有业务消息都必须有顶层 `connectionNonce`、`connectionEpoch`，且不得有任何层级的 `sessionId`。服务端 strict 业务消息不得有 target 字段；收件人由 Socket.IO sid 决定。

存在 permanent decommission tombstone 的 `(user, clientId, deviceSessionId)` 不得形成当前 Socket、出现
在 `device.list` 或成为任何 command recipient。管理端 recovery abandon 提交该 tombstone 时，必须在
同一事务撤销并断开仍在线的 exact pair、清除其在线音量状态和 device-list presence；同一 clientId
只有换用新的 deviceSessionId 才能建立新生命周期。

### 6.0 设备级音量 command 与实际状态

服务端接受 `device.setVolume` 后，只向精确匹配 `targetClientId` / `targetDeviceSessionId` 的当前
Socket 投递以下无 requestId command：

```json
{
  "type": "command",
  "action": "device.setVolume",
  "connectionNonce": "<target socket nonce>",
  "connectionEpoch": 1,
  "payload": {
    "sourceClientId": "controller-1",
    "volume": 65
  }
}
```

payload 必须且只允许 `sourceClientId`、`volume`，不得复制 target 字段。收件 player 应立即设置
设备音量并上报实际结果；即使请求值与当前值相同，也必须发送 confirmation，使 controller 可以
区分“命令已路由”和“设备实际状态已确认”。

服务端 canonical `device.volume.update`：

```json
{
  "type": "event",
  "action": "device.volume.update",
  "connectionNonce": "<recipient nonce>",
  "connectionEpoch": 1,
  "payload": {
    "sourceClientId": "player-1",
    "deviceSessionId": "device:player-1",
    "volume": 64,
    "clientSeq": 4,
    "serverUpdatedAtMs": 1780000001200
  }
}
```

服务端必须把 canonical update 发回 source Socket 作为 event confirmation，并发送给同用户所有
协商 `remoteVolumeControl:true` 的 controller。序号作用域为
`(user, clientId, deviceSessionId, connectionNonce, connectionEpoch)`；相同序号相同内容只向重复
请求 Socket 重放，相同序号不同内容或倒退返回 `client_sequence_conflict`。断线后在线 volume
状态和序号作用域一并清除，新物理连接可从 1 开始。

### 6.1 Context discovery 与 binding 失效通知

#### 6.1.1 `playback.context.list`：按 authority/device 发现 Context

客户端从 `device.list` 选择目标 player 后发送：

```json
{
  "type": "state",
  "action": "playback.context.list",
  "requestId": "context-list-1",
  "payload": {
    "authorityClientId": "flutter-windows-9dcc9687-7b3e-4772-b584-a0fc716ce86c",
    "authorityDeviceSessionId": "device:flutter-windows:9dcc9687"
  }
}
```

服务端用同 `requestId` 直接响应，不得先发送 `system.ack`：

```json
{
  "type": "state",
  "action": "playback.context.list",
  "requestId": "context-list-1",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "contexts": [
      {
        "playbackContextId": "playback:aa9202f4-d39d-49e4-b7c0-376708c0efc7",
        "authorityClientId": "flutter-windows-9dcc9687-7b3e-4772-b584-a0fc716ce86c",
        "authorityDeviceSessionId": "device:flutter-windows:9dcc9687"
      }
    ]
  }
}
```

`payload` 必须且只允许 `contexts`。`contexts` 必须是 array；每个项目必须且只允许非空 string
`playbackContextId`、`authorityClientId`、`authorityDeviceSessionId`，并且后两个值必须与请求完全
相同。结果只包含当前 authenticated user 的 active Context，按 `playbackContextId` 升序，且不得
重复 Context ID。

空结果使用相同成功 shape：

```json
{
  "type": "state",
  "action": "playback.context.list",
  "requestId": "context-list-empty-1",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {"contexts": []}
}
```

服务端不得返回 queue、currentIndex、playback state、deviceStates 或 cursor；这些 canonical 数据只
由客户端选定 Context 后的 `playback.context.status` 提供。服务端也不得添加 `active:true`，因为
所有返回项按定义已经是 lifecycle active，而“一个设备的唯一当前 Context”并非数据模型约束。
因此 `contexts.length > 1` 必须由 Flutter 视为歧义并 fail-closed；lifecycle active 不等于“正在驱动
真实音频”，客户端不得把其中任一项解释为当前播放任务。

该 action 的结算规则：

| 条件 | 结算 |
| --- | --- |
| 合法请求，匹配 0/1/多个 active Context | 上述 direct response；包括空数组；多个结果由客户端强制进入 `ambiguous_playback_scope` |
| 缺字段、空字段、类型错误、未知字段或任何 `sessionId` | `system.error(code:"bad_request")` |
| 未登录或未完成设备注册 | `system.error(code:"unauthorized")` |
| 调用方没有 `controller` 角色 | `system.error(code:"forbidden")` |
| 调用连接未协商 `playbackContextV2:true` | `system.error(code:"capability_required")` |
| 部署未实现该 action | `system.error(code:"not_supported")` |

任何失败都使用原 requestId，且 `system.error.payload.action` 必须为 `playback.context.list`。

若已经完成 negotiated strict 注册的连接收到 `not_supported` 或 `capability_required`，说明服务端
违反第 7.3 节的 Core readiness。Flutter 必须立即停止该连接上的远程控制、清除已发现 Context，
并将服务器 profile 标记为不合规；不得回退到 `sessionId`、`deviceSessionId` 或 `clientId` 猜测。

#### 6.1.2 `playback.context.bindings.changed`：失效通知

Context binding 发生变化后，服务端向同一 authenticated user 的所有已注册 strict controller
Socket 推送：

```json
{
  "type": "event",
  "action": "playback.context.bindings.changed",
  "connectionNonce": "<recipient nonce>",
  "connectionEpoch": 1,
  "payload": {
    "authorityClientId": "flutter-windows-9dcc9687-7b3e-4772-b584-a0fc716ce86c",
    "authorityDeviceSessionId": "device:flutter-windows:9dcc9687"
  }
}
```

payload 必须且只允许非空 string `authorityClientId`、`authorityDeviceSessionId`。这是无 requestId
的 invalidation event，不是 list response，不携带 Context ID、数量、queue、状态或 cursor，也不
建立 subscription。重复事件允许，客户端处理必须幂等。服务端不持久化或重放该事件；断线期间
遗漏的变化由客户端重连后的强制 `device.list` / `playback.context.list` 获取 canonical 结果。

服务端必须在以下 canonical mutation 原子提交后发送：

1. `playback.context.ensure` 新建 idle Context 或重绑旧 deviceSession：为受影响的新/旧
   authority/device pair 发送；
2. `playback.context.close` 关闭 active Context：为关闭前的 authority/device pair 发送；
3. `playback.handoff.complete` 原子切换 authority：分别为旧 pair 和新 pair 各发送一次；如果 pair
   完全相同则只发送一次；
4. 其他任何改变 active Context 的 `authorityClientId` 或 `authorityDeviceSessionId` 的操作：为所有
   受影响的旧/新 pair 发送。

事件必须发送给同用户所有 negotiated `playbackContextV2:true` 且具有 `controller` 角色的当前
Socket，而不只发送给该 Context 的 subscribers。跨用户 Socket、player-only Socket 和 legacy
Socket 不得收到。mutation 必须先持久化提交；请求本身按第 4.3 节完成 direct response/ACK 或
event confirmation 后，再发送本事件。事件发送失败不得回滚已提交 mutation。
如果服务端无法把该关键 invalidation 可靠加入某个目标 controller Socket 的发送队列，必须断开
该 Socket，迫使其按重连流程重新发现；不得保持连接并允许它继续使用可能过期的 binding。

Flutter 收到与当前所选设备 pair 匹配的事件后必须立即暂停该设备的所有新控制请求，使缓存的
device→Context binding、playback/queue snapshot 和 cursors 失效，并使用新的 requestId 重新执行
`playback.context.list`。只有重新得到唯一 Context，并通过 subscribe/status 读取服务端当前状态后才能恢复控制；
空结果保持禁用，多结果进入 `ambiguous_playback_scope`。事件不匹配当前所选 pair 时可以忽略。

为处理 event 与在途 list response 的交错，Flutter 必须为每个 authority/device pair 维护仅本地使用
的单调 `discoveryGeneration`：发送 list 时记录当前 generation；收到匹配的 bindings.changed、
deviceSession 变化或其他第 5.1 节定义的失效信号时先递增 generation；list response 到达时，只有
其请求记录的 generation 仍等于当前值才允许采用。否则必须丢弃整个响应并用新 requestId 重查。
`discoveryGeneration` 不是 wire 字段，服务端不得回显或持久化。

### 6.2 `playback.context.ensure`：启动时确保唯一 Context

player 完成 negotiated 注册后，必须先读取当时可用的本地播放恢复快照，再立即发送 ensure。第 5.5
节已有 Broadcast 恢复记录的 ordinary pair 是启动流程例外：它先进入 restoringOriginalContext 门控并等待
服务端 active/terminal replay；只有 terminal 恢复完成或 tombstone 过期回退流程要求时才可发送 ensure。
已有第 5.3 节非终态 FollowSafetyLease/cleanupRequired tombstone 的 exact pair 也不得用 ensure 穿透
suspended Context fence；同进程 resume 重发 follow.start，stopPending/app restart 只重试 follow.stop。
其他会创建、初始化、重绑或修改 snapshot 的 ensure 返回 suspended Context `conflict` 与四 cursor。
若 matching ordinary pair 在 `restorePending:true` 期间仍发送 ensure，服务端必须以同 requestId 的
`system.error(code:"restore_in_progress",retryable:true)` 终态结算该请求并缓存该拒绝；错误携带
suspendedPlaybackContextId 与 `currentEpoch/currentVersion/currentQueueRevision/currentControlVersion`。
该结算不得创建、初始化、重绑、修改或关闭 Context，
不得递增 cursor、发送 bindings.changed 或清除 restorePending。恢复完成后必须使用新 requestId 重试，
旧 requestId 永远重放原错误。
相同 action-aware gate 也冻结该 suspended Context 的 prepare/close/queue/player/update、
Follow/Broadcast/Handoff start/complete 和 binding mutation；它们都必须在路由或 reducer 前以
`restore_in_progress` 与同一完整四 cursor 拒绝。该规则不是 ensure-only 特例。
本地已有队列时：

```json
{
  "type": "command",
  "action": "playback.context.ensure",
  "requestId": "context-ensure-1",
  "payload": {
    "deviceSessionId": "device:windows-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 1,
    "positionMs": 12400,
    "state": "paused"
  }
}
```

本地没有任何队列时也必须发送固定 idle shape：

```json
{
  "type": "command",
  "action": "playback.context.ensure",
  "requestId": "context-ensure-idle-1",
  "payload": {
    "deviceSessionId": "device:windows-1",
    "queueSongIds": [],
    "positionMs": 0,
    "state": "idle"
  }
}
```

ensure 请求不携带 trackId；服务端从 `queueSongIds[currentIndex]` 推导。空队列必须省略 currentIndex，
state 必须为 idle，positionMs 必须为 0；非空队列必须有合法 currentIndex，state 只允许
`playing|paused|stopped`。

首次没有服务端 Context 且请求携带非空本地队列时，同 `requestId` 直接创建并返回 queue-backed
Context：

```json
{
  "type": "state",
  "action": "playback.context.ensure",
  "requestId": "context-ensure-1",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:windows-1",
    "authorityClientId": "windows-1",
    "authorityDeviceSessionId": "device:windows-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 1,
    "trackId": "song-2",
    "state": "paused",
    "positionMs": 12400,
    "queueRevision": 1,
    "controlVersion": 1,
    "version": 1,
    "epoch": 1,
    "timelineId": "timeline-1",
    "serverUpdatedAtMs": 1780000000000
  }
}
```

首次没有服务端 Context 且请求是 idle shape 时，返回同样 cursors 的 idle snapshot：

```json
{
  "playbackContextId": "playback:user:windows-1",
  "authorityClientId": "windows-1",
  "authorityDeviceSessionId": "device:windows-1",
  "queueSongIds": [],
  "state": "idle",
  "positionMs": 0,
  "queueRevision": 1,
  "controlVersion": 1,
  "version": 1,
  "epoch": 1
}
```

服务端必须在 `(authenticated user, stable clientId)` 原子临界区内执行：

1. 当前 clientId/deviceSessionId 已绑定唯一 active Context：
   - canonical Context 为 idle 且请求携带非空队列时，以请求快照初始化同一个 Context，递增
     version、queueRevision、controlVersion 后返回；
   - canonical Context 已为 queue-backed 时返回现有完整 snapshot，不在 ensure 内用无 base cursor
     的本地快照覆盖 canonical queue；
   - 两边都为 idle 时直接返回，不递增 cursor；
2. 当前 pair 没有 Context，但相同 clientId 只有一个绑定到离线旧 deviceSession 的 active Context：
   保持 playbackContextId，原子重绑到当前 deviceSessionId；旧 canonical 为 idle 且请求非空时同时
   使用请求快照初始化，否则保留旧 canonical queue；cursor 按第 4.5 节组合规则递增后返回；
3. 没有 active Context：由服务端生成不可复用的 playbackContextId，按请求快照直接创建
   queue-backed 或 idle Context；
4. 存在多个候选、旧 deviceSession 仍在线、当前连接不是 player、`canPlay:false` 或请求
   deviceSessionId 不匹配时 fail-closed，不得创建第二个 Context。

ensure 成功后当前 Socket 自动订阅该 Context。新建或重绑必须在 direct response 结算后按第 6.1.2
节发送 bindings.changed；返回现有未变 Context 不发送失效通知。`playback.context.create` 不属于
strict-v2 `2.8.0` action surface，服务端收到时返回 `not_supported`。

如果 ensure 返回的 queue-backed canonical snapshot 与本地快照不同，ensure response 是当前服务端
基线。authority 必须先读取并应用服务端返回的状态；确实需要以本地队列替换时，再使用 response 中最新
queueRevision/controlVersion 发送显式 `queue.context.sync`，不得在 ensure 内无版本覆盖。无论队列
是否相同，设备都应请求 `playback.context.status` 读取持久化 DevicePlaybackState，再按第 5.2.1 节
使用 playback.update 上报实际播放状态和位置。首次按本地 snapshot 创建 Context 时，该 snapshot
本身视为 authority 已执行的版本 1；服务端可以在首次 passive update 前保持 deviceStates 为空，
但不得要求客户端再生成一次 localUser 版本 2。

所有 Context snapshot 使用一套条件闭合 schema：

- 公共必需字段：`playbackContextId`、`authorityClientId`、`authorityDeviceSessionId`、
  `queueSongIds`、`state`、`positionMs`、
  `queueRevision`、`controlVersion`、`version`、`epoch`；`timelineId`、`serverUpdatedAtMs` 可选；
- idle：`queueSongIds` 必须为空，`state` 必须为 `idle`，`positionMs` 必须为 0，`currentIndex` 与
  `trackId` 必须省略；
- queue-backed：`queueSongIds` 必须非空，`state` 只允许 `playing|paused|stopped`，`currentIndex`
  和 `trackId` 必需，且 `currentIndex < queueSongIds.length`、
  `trackId == queueSongIds[currentIndex]`；
- 不得使用 JSON null、负数 index、假歌曲或 sentinel track 表示 idle。
- Context snapshot 不含 `playbackRate`；实际速度只属于 DevicePlaybackState、`playback.update` 与
  Broadcast/Handoff execution target。

### 6.2.1 `playback.context.prepare`：让 idle Context 准备队列

controller 在 idle Context 上收到一次用户播放意图后发送：

```json
{
  "type": "command",
  "action": "playback.context.prepare",
  "requestId": "context-prepare-1",
  "payload": {
    "playbackContextId": "playback:user:windows-1",
    "intentId": "remote-play-intent-1",
    "baseControlVersion": 1,
    "initialQueueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "positionMs": 0
  }
}
```

`initialQueueSongIds` 可省略。省略时 `currentIndex`、`positionMs` 也必须省略，authority 只恢复自己的
本地队列；提供时数组必须非空、去重且不超过 1000 首，currentIndex/positionMs 必需且合法。该队列
只是待验证输入，只有 authority 成功执行 `queue.context.sync` 后才成为 canonical queue。

服务端验证同用户 controller、唯一 Context、当前 authority exact pair 在线、authority 是 player 且
`canPlay:true`、Context epoch/binding 未变化、baseControlVersion 精确匹配且 Context 仍为 idle，然后
建立最多 10 秒的 prepare 事务，并 ACK。该 Core action 不检查 Handoff profile 或
`playbackPrepare:true`；后者只适用于 Handoff target：

```json
{
  "type": "system",
  "action": "system.ack",
  "requestId": "context-prepare-1",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "action": "playback.context.prepare",
    "intentId": "remote-play-intent-1",
    "status": "preparing",
    "controlVersion": 1
  }
}
```

如果 ACK 前 Context 已经变为非空，服务端不路由准备命令，ACK 使用 `status:"ready"` 和当时最新
controlVersion。prepare 本身不修改 Context snapshot 或任何 cursor；同一 Context 同时只允许一个
非终态 prepare。

### 6.2.2 server-routed prepare

服务端向当前 authority Socket 发送无 requestId、无 target 字段的 command：

```json
{
  "type": "command",
  "action": "playback.context.prepare",
  "connectionNonce": "<authority nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:windows-1",
    "intentId": "remote-play-intent-1",
    "controlVersion": 1,
    "sourceClientId": "android-controller-1",
    "initialQueueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "positionMs": 0
  }
}
```

authority 优先恢复自己的本地队列；本地没有队列时才验证并采用可选 initialQueue。成功时必须先用
`queue.context.sync` 把同一个 Context 从 idle 变为 queue-backed paused，不能创建新 Context。

### 6.2.3 `playback.context.prepared`：准备结果

authority 可以发送 event-confirmed 结果：

```json
{
  "type": "event",
  "action": "playback.context.prepared",
  "requestId": "prepared-feedback-1",
  "payload": {
    "playbackContextId": "playback:user:windows-1",
    "deviceSessionId": "device:windows-1",
    "intentId": "remote-play-intent-1",
    "ready": false,
    "errorCode": "queue_required",
    "errorMessage": "No recoverable or supplied queue"
  }
}
```

服务端向 subscribers 广播的 canonical result 必须省略 requestId 和 deviceSessionId，并包含
controlVersion：

```json
{
  "type": "event",
  "action": "playback.context.prepared",
  "connectionNonce": "<recipient nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:windows-1",
    "intentId": "remote-play-intent-1",
    "ready": false,
    "errorCode": "queue_required",
    "errorMessage": "No recoverable or supplied queue",
    "controlVersion": 1
  }
}
```

`ready:true` 时 error 字段禁止，并且服务端必须验证 Context 已为非空；`ready:false` 时 errorCode 必需，
只允许 `queue_required|restore_failed|prepare_timeout|authority_changed|restore_in_progress`。
`restore_in_progress` 只允许当前 authority 对 matching raced prepare 做 negative cleanup；它只结算该
prepare，不初始化队列、不推进 cursor、不提交 ready，也不清 restore fence。服务端不回 ACK，而是向当前
Context subscribers 广播无 requestId 的同 action canonical result，并增加当时最新
`controlVersion`。若 prepare 期间任一合法 queue sync 把 Context 变为非空，服务端可直接将该 prepare
结算为 ready 并广播成功；之后到达的同 intentId `ready:true` 是幂等重复。10 秒到期时，Context 已
非空则结算 ready，否则结算 `prepare_timeout`。authority/binding/epoch 变化时结算
`authority_changed`。prepare 已结算 ready 后再到达的同 intentId `ready:false` 不得覆盖成功结果；
服务端向该 authority Socket 重放 canonical ready 结算并记录迟到诊断。

controller 收到 ready 或观察到同一 Context 已变为非空后，必须重新读取最新 canonical
controlVersion，再发送最初的 `player.play` 一次。prepare ACK 或 prepared ready 不能直接显示为已经
播放。

### 6.3 `playback.context.status`：服务端当前状态与广播的权威快照

```json
{
  "type": "state",
  "action": "playback.context.status",
  "requestId": "status-1",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContext": {
      "playbackContextId": "playback:user:main",
      "authorityClientId": "phone-1",
      "authorityDeviceSessionId": "device:phone-1",
      "queueSongIds": ["song-1", "song-2"],
      "currentIndex": 0,
      "trackId": "song-1",
      "state": "playing",
      "positionMs": 1200,
      "queueRevision": 1,
      "controlVersion": 1,
      "version": 1,
      "epoch": 1,
      "timelineId": "timeline-1",
      "serverUpdatedAtMs": 1780000001200
    },
    "deviceStates": [
      {
        "playbackContextId": "playback:user:main",
        "clientId": "phone-1",
        "deviceSessionId": "device:phone-1",
        "state": "playing",
        "trackId": "song-1",
        "positionMs": 1200,
        "positionSampledAtServerMs": 1780000001150,
        "playbackRate": 1.0,
        "volume": 60,
        "muted": false,
        "appliedControlVersion": 1,
        "clientSeq": 7,
        "serverUpdatedAtMs": 1780000001200
      }
    ]
  }
}
```

idle status 使用相同 action，但主 snapshot 为：

```json
{
  "playbackContext": {
    "playbackContextId": "playback:user:windows-1",
    "authorityClientId": "windows-1",
    "authorityDeviceSessionId": "device:windows-1",
    "queueSongIds": [],
    "state": "idle",
    "positionMs": 0,
    "queueRevision": 1,
    "controlVersion": 1,
    "version": 1,
    "epoch": 1,
    "serverUpdatedAtMs": 1780000001200
  },
  "deviceStates": []
}
```

主 snapshot 必须遵守第 6.2 节的 idle/queue-backed 条件 schema。idle 不是缺少 Context、loading、
stopped 或播放成功；它只表示控制范围已存在但当前没有歌曲。

`deviceStates` 必须是 object array，且每个项目必须且只允许 `playbackContextId`、`clientId`、
`deviceSessionId`、`state`、`positionMs`、`positionSampledAtServerMs`、`playbackRate`、`appliedControlVersion`、
`clientSeq`、`serverUpdatedAtMs`，以及
可选 `trackId`、`volume`、`muted`。每个项目的 playbackContextId 必须等于主 snapshot；一个 clientId
或 deviceSessionId 只能出现一次。appliedControlVersion 必须为正整数且不得高于主 snapshot 的
controlVersion。作为 direct response 时顶层带原 requestId；作为后续服务端状态 push 时必须省略
requestId。playbackRate 必须是有限 number 且在 `0.5..2.0`；device state 为 idle 时 trackId 必须省略且
positionMs 为 0。

当主 snapshot `controlVersion > deviceState.appliedControlVersion` 时，device state 的 track/state/
position 表示 authority 仍在执行旧版本，可以暂时不同于主 snapshot 的最新控制目标。服务端必须按
对应 applied transaction 或已持久化 applied snapshot 验证，不能只与主 snapshot 当前 track 比较。
当 appliedControlVersion 追平 controlVersion 且没有 failed/superseded 对账时，两者必须收敛。

active Follow relationship 自动把 follower Socket 加入 source Context recipient set。follower 必须先
显式读取本节 status，再消费后续 source queue/status/playback.update canonical facts。服务端只能使用
source authority exact pair 当前物理 Socket 的 DevicePlaybackState；旧 nonce、旧 epoch、未结算 fact
不得进入 Follow mirror。Follow mirror 不作为新的 deviceStates 项写回 source 或 suspended Context。

Handoff complete 成功后的首个 Context status 必须已经包含新的
authorityClientId/authorityDeviceSessionId、递增后的 epoch/version、canonical provisional N+1
controlVersion，以及由 complete proof 写入的 target 完整 DevicePlaybackState（queue track/state/position/
sample time/playbackRate/applied N+1/clientSeq/serverUpdatedAtMs）。proof 前的 preparing/ready/committing
status 不得提前改 authority pair、Context cursor 或把 target 临时起播事实写成 canonical。

### 6.4 `playback.context.closed`

```json
{
  "type": "event",
  "action": "playback.context.closed",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {"playbackContextId": "playback:user:main"}
}
```

payload 只能有 `playbackContextId`；不得有 `sessionId` 或 `targetClientId`。

close 表示终止旧 Context ID，不表示让在线 player 永久失去控制范围。仍在线、具备 canPlay 的旧
authority 必须在处理 closed 后立即发送新的 `playback.context.ensure`；服务端不得复用 tombstone ID。
应用退出或设备已经离线时不要求创建替代 Context。

close 首次提交必须在 Context/authority-pair 临界区验证请求的 `expectedEpoch/baseVersion`，拒绝任何
非终态 Handoff、Follow/Broadcast occupancy 或 restorePending fence，然后令 Context version 只递增
一次。持久化 tombstone 必须保存：

```text
closedFromEpoch
closedFromVersion
finalEpoch
finalVersion
finalQueueRevision
finalControlVersion
close ACK outcome
```

新 requestId 的重复 close 只有在 expected/base 与 closedFrom 值相同才重放等价 ACK；其他值返回
`context_closed` 与 final 四 cursor，不再次发送 closed、bindings.changed 或递增 cursor。
