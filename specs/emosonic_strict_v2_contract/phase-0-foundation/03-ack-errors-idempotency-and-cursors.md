# 阶段 0：ACK、错误、幂等、Cursor 与闭合规则

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-07-23-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 4 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
## 4. 标准 ACK 与错误

### 4.1 成功 `system.ack`

所有在第 4.3 节标为 ACK 结算的 strict 请求必须收到同 `requestId` 的 ACK：

```json
{
  "type": "system",
  "action": "system.ack",
  "requestId": "original-request-id",
  "connectionNonce": "<registered nonce>",
  "connectionEpoch": 1,
  "payload": {
    "action": "original.action"
  }
}
```

动作需要返回结果字段时，只能增加本文对应 action 明示的字段；无结果字段的 ACK payload 只有
`action`。`auth.login` 是明确的 bootstrap 例外，额外返回 `authenticated:true` 和 `userName`。

`payload.action` 必须与请求 action 精确相同。当前 strict client 使用 8 秒 ACK timeout；action 不同、缺失或 requestId 不同都会被视为未关联。

`auth.login` ACK 和 `device.register` ACK 是 bootstrap 成功响应，可以省略顶层 provenance；
register ACK 仍必须在 `payload.strictV2` 给出将绑定到该 Socket 的 nonce/epoch。除此以外，
注册成功后的 ACK 必须有顶层 provenance。

只有第 4.3 节结算矩阵明确标为 ACK 的动作才使用本响应。一次请求只能结算一次；不得先 ACK
再发 direct response，或先发 direct response 再 ACK。

### 4.2 失败 `system.error`

```json
{
  "type": "system",
  "action": "system.error",
  "requestId": "original-request-id",
  "connectionNonce": "<registered nonce>",
  "connectionEpoch": 1,
  "payload": {
    "action": "original.action",
    "code": "stale_version",
    "message": "safe diagnostic string",
    "retryable": false,
    "playbackContextId": "可选",
    "currentControlVersion": 12,
    "currentQueueRevision": 8,
    "currentVersion": 33,
    "currentClientSeq": 99
  }
}
```

`action` 对 strict 请求也是必需的 correlation 字段。成功和失败响应互斥；一次处理只能结算
一次，不能再次执行副作用。缓存期内重复请求按第 4.4 节重放已结算结果。

允许的错误码全集如下；服务端不得在 `2.8.x` 中临时创造同义错误码：

| code | 含义 | `retryable` | 条件字段 |
| --- | --- | --- | --- |
| `bad_request` | envelope、字段、类型、枚举、范围或字段组合非法 | `false` | 无 |
| `unauthorized` | 尚未登录或凭据无效 | `false` | 无 |
| `forbidden` | 已登录但无 context/action 权限 | `false` | `playbackContextId` 可选 |
| `not_supported` | action 不属于服务器声明的 `2.8.x` surface | `false` | 无 |
| `not_found` | context、handoff、broadcast 或目标设备不存在 | `false` | `playbackContextId` 可选 |
| `context_closed` | context 已终止 | `false` | `playbackContextId` 必需 |
| `authority_offline` | 当前 authority 没有有效 Socket | `true` | `playbackContextId` 必需 |
| `queue_required` | 请求需要非空 canonical queue，但 Context 仍是 idle；prepare 失败时也表示两端没有可采用队列 | `false` | `playbackContextId` 必需；三个 canonical cursor 必需 |
| `restore_in_progress` | ordinary pair 的 terminal 原任务恢复尚未完成，当前请求不能穿透 restorePending gate | `true` | `playbackContextId` 及三个 `current*Version/Revision` 必需 |
| `conflict` | 同一逻辑 ID 被用于不同意图，或状态机不允许该动作 | `false` | Context/handoff/broadcast 冲突时必须有 `playbackContextId` 及三个 `current*Version/Revision`；仅 requestId 内容冲突时省略 |
| `stale_version` | base cursor 落后或超前于 canonical cursor | `false` | 对应 `current*` cursor 必需 |
| `client_sequence_conflict` | `clientSeq` 重复但内容不同或倒退 | `false` | `currentClientSeq` 必需 |
| `capability_required` | 连接未协商到动作所需 capability/角色依赖 | `false` | 无 |
| `rate_limited` | 超出连接、用户或 action 限额 | `true` | `retryAfterMs` 必需且为正整数 |
| `internal_error` | 未预期服务端错误 | `true` | 无；不得暴露堆栈、路径或数据库内容 |

`playback.context.list` 的“没有匹配的 active Context”不是错误，必须返回同 requestId 的成功 direct
response，且 `payload.contexts` 为空数组。该查询不得用 `not_found`、`context_closed` 或
`authority_offline` 表示空结果；后两者只可能在客户端随后使用已发现或缓存的 Context ID 执行
status/control 时发生。调用方没有 controller 角色时返回 `forbidden`；未协商
`playbackContextV2:true` 时返回 `capability_required`。

除表中条件字段外，错误 payload 只允许 `action`、`code`、`message`、`retryable`、
`playbackContextId`、`currentControlVersion`、`currentQueueRevision`、`currentVersion`、
`currentClientSeq`、`retryAfterMs`。不适用的可选字段必须省略，不得写 JSON `null`。

`device.register` strict ACK 尚未成功、Socket 还没有绑定 provenance 时，任何具有合法
requestId/action 的失败请求都是 bootstrap error：仍须使用同 `requestId` 的 `system.error` 和正确
`payload.action`，但顶层必须省略 `connectionNonce` / `connectionEpoch`。这包括过早发送的
`device.list`、`playback.context.list` 或其他业务 action，它们返回 `unauthorized`。注册成功后的
任何错误不再享有此例外，必须带该 Socket 的 provenance。

### 4.3 请求结算矩阵

每个 action 的成功结算方式唯一如下：

| action | 唯一成功结算方式 |
| --- | --- |
| `auth.login`、`device.register` | correlated `system.ack` |
| `device.list` | 同 `requestId` 的 direct `device.list` response |
| `device.setVolume` | correlated `system.ack` |
| `playback.context.list` | 同 `requestId` 的 direct `playback.context.list` response，payload 为第 6.1 节 Context binding 列表 |
| `playback.context.ensure` | 成功为同 `requestId` 的 direct `playback.context.ensure` response、payload 为第 6.2 节完整 snapshot；matching restorePending 时为同 requestId `system.error(restore_in_progress)`，两者互斥且均进入 request cache |
| `playback.context.status` | 同 `requestId` 的 direct `playback.context.status` response，payload 为第 6.3 节完整 status |
| `playback.context.subscribe`、`unsubscribe`、`close`、`prepare` | correlated `system.ack` |
| `queue.context.sync`、`queue.playItem`、`player.play`、`player.pause`、`player.seek`、`player.next`、`player.prev` | correlated `system.ack` |
| `follow.start`、`follow.stop` | correlated `system.ack` |
| `playback.handoff.start`、`playback.handoff.cancel` | correlated `system.ack` |
| `broadcast.start`、`broadcast.status`、`broadcast.play`、`broadcast.pause`、`broadcast.seek`、`broadcast.playItem`、`broadcast.stop` | correlated `system.ack` |
| `device.volume.update`、`playback.update`、`playback.context.prepared`、`playback.ready`、`playback.handoff.complete`、`broadcast.feedback` | event/state-confirmed；服务端不回 ACK，按第 4.5、5.2、5.4、5.5、6.0、6.2.3、6.6、6.9、6.10 节状态推进；合法 feedback 成功结算为 canonical confirmation，revision ledger 无法匹配时结算为 `broadcast.feedback.rejected` 并按 lifecycle 补发新执行 delivery |
| `system.ping` | 同 `requestId` 的 direct `system.pong` response |

`playback.context.subscribe` 成功只 ACK。客户端随后显式发送
`playback.context.status` 完成服务端当前状态读取；服务端不得把未关联的 status push 当成 subscribe 的结算。
List/Ensure 不允许用简略 ACK 代替 direct response。

### 4.4 幂等、重复请求与重连

1. `playbackContextId` 由服务端在首次 ensure 时生成，客户端不得指定或复用。ID 一旦进入 closed
   tombstone 永久不可复用。重复 ensure 的长期幂等作用域是
   `(authenticated user, stable clientId)`，不是客户端提供的 Context ID。
2. 服务端必须缓存最近 `(connectionNonce, requestId)` 的 request fingerprint 与结算结果至少
   60 秒；Socket 断开时立即清理。客户端在同一连接中永不复用 requestId。缓存期内同一键重复
   到达时原样重放结果，不得再次产生副作用；同一键的 action 或 payload 不同则返回 `conflict`。
   缓存期外的长期幂等由 stable client ensure、Context/Handoff/Broadcast 等逻辑 ID 保证。
3. 重复 subscribe/unsubscribe、close、`follow.start`/`stop`、handoff cancel/complete 和
   `broadcast.stop` 均必须幂等。资源已处于目标状态时返回与首次成功等价的 ACK 或 canonical
   confirmation，不得为 event-confirmed 动作补发 ACK。
4. 每个 Context 同时只能有一个非终态 handoff。同一 source/target 的 start 重试返回已有
   `handoffId`/`prepareId`；不同 target 返回 `conflict`。
5. 同一已认证用户以相同 `clientId` 完成新注册后，新 sid 原子替换旧 sid，并立即断开旧 sid。
   authority 路由还必须匹配持久化的 `deviceSessionId`；相同 clientId、不同 deviceSessionId 的
   新连接不能仅凭注册自动继承旧 authority，只有随后合法的 `playback.context.ensure` 可以按第 11
   条原子重绑离线旧 session。
6. Socket 断开必须清除该 sid 的临时订阅和 client→sid 映射，但不得删除持久化 Context。
   重连 controller 必须使用新的 requestId 重新发送 `playback.context.list`；发现 Context 后重新
   subscribe，再显式请求 status。服务端不得假设 Context binding 查询结果或 room membership
   跨连接保留。同一 clientId/deviceSessionId 重连可以重新发现原 Context；相同 clientId 但不同
   deviceSessionId 的连接在 ensure 完成重绑前不得匹配旧 binding。
7. event/state-confirmed 请求重复且内容相同时，不重新 mutation 或全局广播；服务端只向当前请求
   Socket 重放以下无 requestId 的 canonical confirmation：`device.volume.update` 与 `playback.update`
   重放缓存的 canonical update；`playback.context.prepared` 重放当前 prepare 结算；`playback.ready`
   重放 Handoff 当前 status；`playback.handoff.complete` 重放 completed status 与当前 Context status；
   `broadcast.feedback` 重放已接受的 participant confirmation 或已结算的
   `broadcast.feedback.rejected`。不得重发 prepare/commit/release、切换
   authority、覆盖 participantStates 或递增任一 cursor。
8. Context close 后必须保留持久化 terminal tombstone，`playbackContextId` 不可复用。重复 close
   返回等价 ACK；其他 status/mutation 返回 `context_closed`。正常 Context snapshot 不输出
   `state:"closed"`。服务端先向当前 subscribers/followers 推送 closed，再清除该 Context 的全部
   临时订阅和 Follow relationship。关闭的 Context 必须立即从新的 `playback.context.list` 查询
   中消失，并按第 6.1.2 节向该 authority/device pair 发送 binding 失效通知。
9. `playback.context.list` 是无副作用读取。同一连接、同一 requestId 的重复请求按第 2 条重放
   缓存结果；客户端要观察 close、handoff、authority 或 device registration 变化时必须使用新的
   requestId 重新查询。服务端不得用旧查询缓存覆盖新的 canonical binding。
10. Handoff complete 保持 `playbackContextId` 不变，并原子更新 Context 的
   `authorityClientId` / `authorityDeviceSessionId`。完成后，旧 authority/device pair 的新 list
   查询不得再返回该 Context，新 pair 的查询必须返回同一个 Context ID。Context-level controller
   可以继续使用该 ID；以“控制所选设备”为语义的客户端必须解除旧设备绑定并重新发现。服务端
   必须按第 6.1.2 节同时使旧 pair 和新 pair 的 discovery cache 失效。
11. `playback.context.ensure` 必须在 `(authenticated user, stable clientId)` 作用域与 close、
    handoff authority switch 串行化。当前或可重绑的 canonical Context 为 idle 且 ensure 携带非空
    本地快照时，可以在同一原子操作中初始化该 Context；canonical 已为 queue-backed 时 ensure 只返回
    它，不执行无 base cursor 覆盖。没有可恢复 Context 时按请求快照创建 queue-backed 或 idle Context。
    任何分支都不得产生第二个 active Context。存在多个候选、旧 deviceSession 仍在线或同 clientId
    被不同逻辑设备复用时返回 `conflict`。
12. `playback.context.prepare.intentId` 是准备事务的长期幂等键。同一 Context、同一 intentId、相同
    可选初始队列的重试返回当前 prepare 结算，不重复路由；同一 intentId 内容不同返回 `conflict`。
    每个 Context 同时最多一个非终态 prepare。authority deviceSession、Context epoch 或 authority
    client 改变时旧 prepare 立即失败，不得在新设备上继续执行延迟播放意图。
13. `playback.update(origin:"localUser").intentId` 是 authority 本地人工控制的长期幂等键。同一
    Context/epoch、同一 intentId、相同绝对结果的重试必须重放首次 canonical playback.update，不得
    重复递增 cursor或再次 supersede；同一 intentId 内容不同返回 `conflict`。Context close、epoch
    变化、authority client 或 deviceSession 变化后，旧 intentId 不得应用到新 binding。
14. 服务端必须持久化每个 `(playbackContextId, epoch, controlVersion)` 的远程控制事务状态
    `pending|committed|failed|superseded`。同一事务 terminal 后不能变成另一 terminal；相同
    remoteCommand 结果重试只重放 canonical confirmation，不得重复推进 applied cursor 或 Context
    对账。
15. `broadcast.start.intentId` 是跨连接长期幂等键。服务端必须按第 5.5 节重放首次 ACK/broadcastId，
    完整 Broadcast 与 terminal tombstone 一起按第 5.5 节保留；完整状态可清理后，包含
    intentId/fingerprint/broadcastId/final participants/skippedClientIds 的 ACK outcome tombstone 仍保留到
    source Context close。requestId cache 到期或
    Socket 断开不得使已提交 start 失去可发现性或允许旧 intent 创建新 Broadcast。
    Broadcast 进入 terminal 时，该 tombstone 还必须原子追加 terminalBroadcastRevision 和规范化
    `broadcast.stop` ACK outcome，使 full record 压缩后的重复 stop 仍可幂等重放；这些字段不保存
    queue/position 或替代 per-pair recovery。
16. 每个 active source Context 最多保留 1024 个 Broadcast ACK outcome tombstone。达到上限后，
    新 intentId 的 `broadcast.start` 返回 `rate_limited`，不创建 Broadcast；已存在 intentId 仍必须
    重放首次 outcome，不得删除旧 tombstone 后接受旧 intent。只有 source Context close 才可清理
    该 Context 的 ACK outcome tombstones；关闭前必须先终止非终态 Broadcast，compact per-pair
    recovery 记录仍保留到各自恢复确认。

### 4.5 Cursor 的含义与递增矩阵

这些字段互不替代：

- 顶层 `connectionEpoch`：物理 Socket provenance，只用于隔离旧连接消息。
- Context `epoch`：播放时间线 generation；authority 原子切换时递增。
- `version`：任何被接受并物化到权威 Context snapshot 的 mutation 版本。
- `queueRevision`：canonical 队列内容或 `currentIndex` 变化版本。
- `controlVersion`：被接受且会执行播放控制或改变 authority 的操作版本。
- `observedControlVersion`：authority 本地人工操作发生时已观察到的控制版本，不是严格 base cursor，
  也不是客户端申请的新版本。
- `commandControlVersion`：remoteCommand feedback 正在结算的服务端控制事务版本。
- `appliedControlVersion`：DevicePlaybackState 所描述的实际状态已经成功执行到的控制版本。
- `lastAppliedControlVersion`：服务端为当前 authority device 持久化的最高已确认 applied 版本；普通
  进度允许等值更新，低值反馈不得覆盖，高于 canonical controlVersion 的反馈是协议错误。
- `sourceEpoch/sourceVersion/sourceQueueRevision/sourceControlVersion`：BroadcastSnapshot 对 source
  PlaybackContext 四个 canonical cursor 的精确带名副本。在 active/waitingForSource 期间不得独立
  递增或与 source 分叉；进入 terminal 时冻结为 terminal 提交所引用的 source cursor 历史值，
  后续 source Context mutation 不得再改写已终止 Broadcast。
- `broadcastRevision`：Broadcast 自己的单调 canonical snapshot/lifecycle revision；start 初始化为 1，
  每次实际提交并发送新的 source-derived snapshot、waiting/resume 或 terminal 内容时严格递增 1。
  仅合并但未发送、同 snapshot 的 ordinary pair resync/delivery replay、feedback 和 status 读取不递增。
  同 revision 的持久化 `BroadcastSnapshot` 必须逐字段相同；per-delivery 的 `deliveryId`、
  `effectiveAtServerMs`、`serverTimeMs` 和 action 不属于 snapshot/revision identity。
- `deliveryId`：服务端为某个 frozen ordinary `(clientId, deviceSessionId)` 的一次 mirror 投递生成的
  不透明非空 ID，同一 Broadcast 内不得复用。首次 target 和每次物理重连 `broadcast.resync` 都生成
  新值；同一投递的纯传输重放必须复用原值。它不是 cursor，不参与 broadcastRevision 排序，必须由
  ordinary feedback 原样回显；旧 deliveryId 的迟到 feedback 不得关闭当前投递 deadline。
- `appliedBroadcastRevision`：ordinary participant 已实际应用到的 Broadcast revision；不得高于 canonical
  broadcastRevision，同一 participant/connection 内不得回退。
- `failedBroadcastRevision`：ordinary participant 本次尝试但未能应用的 target revision；不得高于
  canonical broadcastRevision，也不推进 appliedBroadcastRevision。`lastAppliedBroadcastRevision` 是该
  failed feedback 发生时设备已成功执行的最高 revision，可以为 0。
- `clientSeq`：必需的设备 feedback 序号，普通 `playback.update` 作用域为
  `(playbackContextId, clientId, connectionNonce, connectionEpoch)`；该作用域内从 1 单调递增，新物理
  连接可从 1 重新开始。`device.volume.update` 使用独立的
  `(user, clientId, deviceSessionId, connectionNonce, connectionEpoch)` 作用域；
  `broadcast.feedback` 使用独立的
  `(playbackContextId, broadcastId, clientId, deviceSessionId, connectionNonce, connectionEpoch)` 作用域；
  canonical confirmation 与 `broadcast.feedback.rejected` 都结算并消耗该作用域的 clientSeq。

| 被接受的动作 | `epoch` | `version` | `queueRevision` | `controlVersion` |
| --- | --- | --- | --- | --- |
| 首次 ensure 按请求快照创建 idle 或 queue-backed Context | 初始化为 1 | 初始化为 1 | 初始化为 1 | 初始化为 1 |
| ensure 返回当前 pair 的既有 Context | 不变 | 不变 | 不变 | 不变 |
| ensure 用非空本地快照初始化当前 idle Context | 不变 | +1 | +1 | +1 |
| ensure 将同 clientId 的离线旧 deviceSession 重新绑定到当前 session | +1 | +1 | 仅同时将 idle 初始化为非空时 +1 | +1 |
| `device.setVolume` / `device.volume.update` | 不变 | 不变 | 不变 | 不变；不访问 Context |
| `playback.context.prepare` / `playback.context.prepared` | 不变 | 不变 | 不变 | 不变；prepare 使用独立 intentId 状态机 |
| `queue.context.sync` | 不变 | +1 | +1 | 当 currentIndex、该 index 的 trackId 或 idle/non-empty 边界改变时 +1；position 自然前进不是控制变化；这是 authority 已提交的实际 state mutation，control 前进时该 authority 的 applied cursor 同步前进；idle→non-empty 将 state 设为 paused，non-empty→idle 将 state 设为 idle |
| `queue.playItem` | 不变 | +1 | +1 | +1 |
| `player.play` / `pause` / `seek` | 不变 | +1 | 不变 | +1 |
| `player.next` / `prev` | 不变 | +1 | +1（`currentIndex` 改变） | +1 |
| `playback.update(origin:"passive")` | 不变 | 不变 | 不变 | 不变；只更新对应 device state / `clientSeq` |
| `playback.update(origin:"remoteCommand", executionStatus:"committed")` | 不变 | 不变 | 不变 | 不变；将 pending command 结算为 committed，并推进该 device 的 applied cursor |
| `playback.update(origin:"remoteCommand", executionStatus:"failed")` | 不变 | 仅需要把预期 Context 恢复为实际状态时 +1 | 仅需要恢复 currentIndex 时 +1 | 不变；command 版本已占用但 applied cursor 不推进 |
| `playback.update(origin:"localUser", executionStatus:"committed")` | 不变 | +1 | queueIndex 改变时 +1 | +1；服务端从当前 canonical 值递增，并 supersede 旧 pending remote |
| `broadcast.feedback` | 不变 | 不变 | 不变 | 不变；只更新匹配 Broadcast 的 participantStates / `clientSeq` |
| `broadcast.start` / `status` / `stop` | 不变 | 不变 | 不变 | 不变；只初始化、读取或终止派生 Broadcast lifecycle |
| `broadcast.play` / `pause` / `seek` | 不变 | 与对应 source `player.*` 相同 | 不变 | 与对应 source `player.*` 相同；不得维护第二套播放 cursor |
| `broadcast.playItem` | 不变 | 与 source `queue.playItem` 相同 | 与 source `queue.playItem` 相同 | 与 source `queue.playItem` 相同 |
| handoff authority 原子切换 | +1 | +1 | 不变 | +1 |
| context close | 不变 | +1 后进入 terminal | 不变 | 不变 |

请求中的 base cursor 必须精确等于服务器当前值；不相等返回 `stale_version`，不执行副作用。
条件可选的 base cursor 只有在对应 canonical 域完全不改变时才可省略；实际变化却缺少对应 base
cursor 时返回 `bad_request`。例如 queue sync 中队列内容变化要求 `baseQueueRevision`，而 index、
该 index 的 trackId 或 idle/non-empty 边界变化还要求 `baseControlVersion`；position 自然前进不要求
`baseControlVersion`。
服务端 push 的比较顺序为 Context `(epoch, version)`、Queue `(epoch, queueRevision)` 和 Control
`(epoch, controlVersion)`。旧值必须拒绝；完全相等且内容相同视为重复并忽略；完全相等但内容
不同是 `conflict`，必须记录协议错误。`clientSeq` 重复且内容相同可忽略，重复但内容不同或倒退
返回 `client_sequence_conflict`。若相同 feedback 同时命中 requestId 缓存，服务端仍按第 4.4 节
向请求 Socket 重放 canonical confirmation，但不再次写状态。

`observedControlVersion` 不使用普通 base cursor 的精确相等规则。当前 authority 的 localUser update
在 `observedControlVersion <= canonical controlVersion` 时可以按服务端接收顺序被接受；大于
canonical 返回 `bad_request`。服务端接受后必须从当前 canonical controlVersion 加一，不能使用
客户端猜测值。`appliedControlVersion < lastAppliedControlVersion` 的反馈不得覆盖 DevicePlaybackState；
等于 lastApplied 时允许 passive 事实、匹配 pending command 的 failed 结果或相同 terminal 幂等重放；
高于 lastApplied 时必须由按序有效的 pending remote committed 或新接受的 localUser transaction 证明。

### 4.6 严格 schema 闭合规则

- 每个 action 只允许本文对应表格、示例和共享 envelope 明示的字段；未知 request 字段返回
  `bad_request`。服务端 push 也不得添加未定义字段。
- 可选字段无值时省略，不得传 JSON `null`。数组不得含重复 ID，string 必须去除首尾空白后非空。
- 所有 ID（`requestId`、client/device/context/handoff/prepare/intent/broadcast/delivery/timeline ID）最大 128 UTF-8
  bytes；`action` 最大 64 bytes；错误 `message` 最大 512 bytes。
- 单个 transport message 上限为 256 KiB。Engine.IO/WebSocket 层在进入业务 handler 前发现超限时，
  使用 message-too-big 行为关闭连接，不保证返回 system.error。消息进入业务 handler 后发现字段、
  队列（最多 1000 首）、ordinary participants（最多 20 个）或其他业务限制超限时，返回同 requestId 的
  `system.error(code:"bad_request")`。两种情况都不得静默截断。
- `timestamp` 只允许第 2.2 节的兼容用途；action schema 未列出的时间字段一律禁止。
- 所有集合语义数组必须去重。`queueSongIds` 必须保留 canonical 播放顺序，严禁排序；roles 固定按
  `player`、`controller` 顺序输出；participants、skippedClientIds、devices 及服务端生成的其他
  client 集合按 `clientId` 升序；`playback.context.list.payload.contexts` 按
  `playbackContextId` 升序。
