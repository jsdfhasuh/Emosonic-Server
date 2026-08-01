# 阶段 1：服务端 Queue、Playback 与 Routed Control 消息

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-07-23-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 6.5—6.7.1 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。

任何 queue/playback reducer 或 routed-control admission 在处理 exact pair 的 suspended Context 前，都
必须先检查 Broadcast terminal `restorePending`。命中时 `queue.context.sync`、`queue.playItem`、全部
`player.*` 与 `playback.update` 统一以 `restore_in_progress` 和完整四 cursor 零副作用拒绝；不得创建
ControlTransaction、分配版本、发送 command 或把请求留在服务端队列。Flutter 同样不得在
`restoringOriginalContext` 期间排队普通命令。未确认 terminal delivery 必须先可靠加入当前 pair 的
Socket 发送路径；enqueue 失败立即断开，下次注册先 replay，不能先发送本节普通 control。

### 6.5 `queue.context.sync`

```json
{
  "type": "state",
  "action": "queue.context.sync",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "authorityClientId": "phone-1",
    "authorityDeviceSessionId": "device:phone-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 1,
    "trackId": "song-2",
    "state": "paused",
    "positionMs": 0,
    "positionSampledAtServerMs": 1779999999950,
    "queueRevision": 8,
    "controlVersion": 12,
    "version": 33,
    "epoch": 1,
    "timelineId": "timeline-1",
    "serverUpdatedAtMs": 1780000000000
  }
}
```

queue push 使用与 Context snapshot 相同的条件 schema：公共必需字段为 playbackContextId、
authorityClientId、authorityDeviceSessionId、queueSongIds、state、positionMs、
positionSampledAtServerMs、queueRevision、controlVersion、version、epoch；
serverUpdatedAtMs/timelineId 可选。
非空 queue 必须包含合法 currentIndex、匹配 trackId 和 `playing|paused|stopped` state；空 queue 必须
省略 currentIndex/trackId、positionMs 为 0、state 为 idle。

`queue.context.sync` 从 idle 进入非空时，服务端把 canonical state 设为 paused；从非空清为空时设为
idle。两种边界变化都递增 version、queueRevision 和 controlVersion。普通非空队列内容变化始终递增
version/queueRevision；只有当前 index、该 index 对应的 track 或 idle/non-empty 边界变化时才递增
controlVersion。position 自然前进不是控制变化；服务端保存 positionMs 和采样时间作为事实锚点，
明确 seek 必须使用 player.seek 或 playback.update(origin:"localUser")。请求不得携带 state 或 trackId，
二者由服务端根据队列推导。
`positionSampledAtServerMs` 必须与 positionMs 同时采样，canonical push 原样保留该合法采样时间；
`serverUpdatedAtMs` 仍是服务端接受/提交时间，不得代替位置采样时间。

`queueSongIds` 必须 distinct，并保持原播放顺序；r18 不支持 shuffle 或任何 repeat mode。第一首
`player.prev` 重播第一首，index/track/queueRevision 不变、state=playing、positionMs=0；最后一首
`player.next` 不循环，index/track/queueRevision 不变、state=stopped、positionMs=0。两者均只推进一次
version/controlVersion。最后一首自然结束按第 5.2.1 节 passive automatic terminal 例外只推进一次
Context version，不推进 queueRevision/controlVersion。

### 6.6 `playback.update`

playback.update 是 authority 实际状态和控制事务的 event-confirmed 通道。客户端请求使用第 5.2.1 节
shape；服务端不回 ACK，而是向请求 Socket 和全部合法 Context recipients 广播无 requestId canonical
confirmation。服务端 push 公共必需字段为：

```text
playbackContextId
sourceClientId
deviceSessionId
origin
controlVersion
appliedControlVersion
state
positionMs
positionSampledAtServerMs
playbackRate
clientSeq
serverUpdatedAtMs
```

`controlVersion` 是服务端广播时的最新 canonical 控制版本，`appliedControlVersion` 是该实际设备状态
已经执行到的版本；二者可以不同。`positionSampledAtServerMs` 必须等于已验证请求中的
采样时间，`serverUpdatedAtMs` 由服务端按接受时间生成。服务端 push 禁止
sessionId、authorityClientId、queueSongIds、
currentIndex、queueRevision、version、baseControlVersion、observedControlVersion 和 target 字段。

source Context 的 Follow subscribers 是合法 recipients，但只能把 canonical queue/status/update 作为
本地 mirror 输入。follower overlay 不得把执行结果回送为普通 playback.update；服务端也不得从 Follow
本地状态改写 source Context、suspended Context、control transaction 或 DevicePlaybackState。source
进入 idle 时仍先提交唯一 source Context fact，follower 随后清空 mirror audio并保持 relationship。

#### 6.6.1 Passive canonical update

```json
{
  "type": "event",
  "action": "playback.update",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "sourceClientId": "windows-1",
    "deviceSessionId": "device:windows-1",
    "origin": "passive",
    "controlVersion": 48,
    "appliedControlVersion": 47,
    "state": "playing",
    "trackId": "song-1",
    "positionMs": 1200,
    "positionSampledAtServerMs": 1780000001150,
    "playbackRate": 1.0,
    "volume": 60,
    "muted": false,
    "clientSeq": 7,
    "serverUpdatedAtMs": 1780000001200
  }
}
```

passive push 只允许公共字段和可选 trackId/volume/muted，不得出现 executionStatus、
commandControlVersion、intentId、queueIndex、supersededThroughControlVersion 或 error 字段。

#### 6.6.2 Remote command committed

```json
{
  "type": "event",
  "action": "playback.update",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "sourceClientId": "windows-1",
    "deviceSessionId": "device:windows-1",
    "origin": "remoteCommand",
    "executionStatus": "committed",
    "commandControlVersion": 47,
    "controlVersion": 48,
    "appliedControlVersion": 47,
    "state": "playing",
    "trackId": "song-2",
    "positionMs": 0,
    "positionSampledAtServerMs": 1780000001150,
    "playbackRate": 1.0,
    "clientSeq": 18,
    "serverUpdatedAtMs": 1780000001200
  }
}
```

commandControlVersion 与 appliedControlVersion 必须相等。controlVersion 可以更高，表示后续命令已经
accepted 但尚未 applied。服务端先持久化 command committed 和 lastApplied，再广播 confirmation。

#### 6.6.3 Remote command failed

```json
{
  "type": "event",
  "action": "playback.update",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "sourceClientId": "windows-1",
    "deviceSessionId": "device:windows-1",
    "origin": "remoteCommand",
    "executionStatus": "failed",
    "commandControlVersion": 47,
    "controlVersion": 47,
    "appliedControlVersion": 46,
    "errorCode": "track_load_failed",
    "errorMessage": "Unable to load requested track",
    "state": "paused",
    "trackId": "song-1",
    "positionMs": 32000,
    "positionSampledAtServerMs": 1780000001150,
    "playbackRate": 1.0,
    "clientSeq": 18,
    "serverUpdatedAtMs": 1780000001200
  }
}
```

failed 不把旧 command 推进为 applied，也不回退 controlVersion。actual 与 canonical target 的 gap
必须按第 5.2.2、6.7.3 节分配新的 internal reconciliation version；不得继续在旧 command version 下
用新 Context/Queue cursor 静默改写事实。若同一事务可 inline reconciliation，唯一 failed confirmation
同时携带新 canonical `controlVersion/appliedControlVersion=R`；否则先发普通 failed，再由 fresh passive
fact 产生唯一 reconciliation confirmation。

#### 6.6.4 Local user committed

```json
{
  "type": "event",
  "action": "playback.update",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "sourceClientId": "windows-1",
    "deviceSessionId": "device:windows-1",
    "origin": "localUser",
    "intentId": "local-intent-123",
    "executionStatus": "committed",
    "controlVersion": 48,
    "appliedControlVersion": 48,
    "supersededThroughControlVersion": 47,
    "queueIndex": 1,
    "trackId": "song-2",
    "state": "playing",
    "positionMs": 0,
    "positionSampledAtServerMs": 1780000001150,
    "playbackRate": 1.0,
    "clientSeq": 20,
    "serverUpdatedAtMs": 1780000001200
  }
}
```

localUser push 禁止 observedControlVersion 和 commandControlVersion。supersededThroughControlVersion
是接受前的 canonical 上界，不表示其中每条命令都被撤销：只有仍为 pending 的事务进入 superseded，
committed/failed 历史保持不变。queueIndex 必须匹配 canonical queue 中的 trackId；服务端同时发送
更新后的 queue/context canonical state，使 controller 获得新的 queueRevision/version。

`state:"idle"` 只允许用于 canonical queue 为空的 Context，并要求省略 trackId、positionMs 为 0。
canonical queue 非空时 feedback state 不得为 idle。trackId 必须由 appliedControlVersion 对应的事务
目标或已持久化 applied snapshot 证明；低于 lastApplied 的迟到 feedback 不得广播或覆盖，高于
canonical controlVersion 的 feedback 返回 bad_request。相同 applied 版本的 passive update 可以用
更高 clientSeq 更新位置；相同版本不同 terminal 结果返回 conflict。

### 6.7 server-routed 控制

服务端应先接受第 5.2 的 command，再向当前 authority Socket 发送**无 target** command。所有 context subscribers 另收 `status` / `queue.context.sync` / `playback.update` 事实状态。

本节描述的是服务端 → 客户端的 accepted control。这里禁止 `baseControlVersion`，只允许
canonical `controlVersion`；它不改变第 5.2 节客户端 → 服务端请求必须携带
`baseControlVersion` 的要求。每个普通 routed control 必须携带
`executionTimeoutMs:int>=1`，并按 deterministic dependency admission 可选携带
`dependsOnControlVersion:int>=1`。第 6.9 节 Handoff commit 使用自己的闭合 shape，不携带这两个普通
control 字段；除此之外，只有由
active Broadcast control 产生的普通 command 才允许并必须成组携带 `effectiveAtServerMs` 与
`serverTimeMs`，普通非 Broadcast command 禁止这两个字段。

包含 `handoffId` 的 `player.play` 是唯一方向性例外：其 N+1 只进入 Handoff execution lane，不创建
普通 ControlTransaction、不参与 dependency/watchdog/reconciliation、不推进 Context reducer，也不使用
remoteCommand playback.update 结算。target 只通过完整 `playback.handoff.complete` actual proof 或
cancel/timeout 结算该 provisional lease；failed/cancelled/timedOut 后普通 control 可以安全复用 N+1。

#### 6.7.1 多客户端下如何确定唯一执行者

`playbackContextId` 是 player control 的**业务路由地址**，不是要求所有客户端自行过滤的
广播主题。每个 PlaybackContext 必须由服务端维护唯一的当前 authority，并完成
以下映射：

```text
playbackContextId
  -> authorityClientId
  -> 当前有效 Socket.IO sid
  -> 该 sid 的 connectionNonce / connectionEpoch
```

服务端必须保存 `authorityDeviceSessionId`，解析出的 Socket 必须同时属于该 clientId 与
deviceSessionId；不能只因 `clientId` 文本相同就把命令发给旧连接或另一设备实例。

服务端收到第 5.2 节的 player control 后，必须按以下顺序处理：

1. 使用请求的 `playbackContextId` 读取未关闭且属于当前用户的 Context。
2. 校验请求者具有 controller 权限、属于允许的控制范围，并验证请求中的
   `baseControlVersion`。
3. 从 Context 读取当前 `authorityClientId`，必要时同时读取
   `authorityDeviceSessionId`。
4. 从服务端连接注册表解析该 authority 当前唯一有效的 Socket.IO `sid`，并确认该
   `sid` 仍绑定到同一用户、client 和 device session。
5. 若 authority 离线、映射缺失或已经被新连接替换，向请求者返回 correlated
   `system.error`（code 必须为 `authority_offline`），不得广播控制命令，也不得回退
   legacy/session 路由。
6. 接受命令并生成新的 canonical `controlVersion`，同时创建持久化
   `(playbackContextId, epoch, controlVersion, status:"pending")` 控制事务；`sourceClientId` 写入发起
   控制的 controller，而不是收件人。若入口是 active Broadcast control，还必须在同一事务中生成
   第 5.5.1 节的唯一计划时刻，并把它绑定到该控制事务与对应 broadcastRevision。
7. 使用 authority `sid` 对应的 nonce/epoch 构造 envelope，并通过 Socket.IO
   `to=<authority sid>` 单播；不得向 Context room 或 namespace 广播这条执行命令。Broadcast 来源的
   command 必须携带事务中已经固定的计划时刻，不得在 emit 时重新计算。
8. 向原请求者返回 action-correlated ACK。ACK 只表示 accepted/routed，不表示 Windows 已执行；只有
   第 6.6 节 remoteCommand committed 才把事务结算为实际成功。另行向 Context subscribers广播
   `playback.context.status` 等 canonical 控制目标；状态广播不负责触发音频执行。

示意代码中的函数名不是服务端公共 API 要求，但投递行为必须等价：

```python
context = get_context(request_payload["playbackContextId"])
authority_sid = get_current_sid(context["authorityClientId"])

command = build_control(
    playback_context_id=context["playbackContextId"],
    control_version=accepted_control_version,
    source_client_id=requesting_client_id,
)
command = bind_recipient_provenance(authority_sid, command)

socketio.emit("message", command, to=authority_sid, namespace="/emo")
```

例如同一 Context 中存在手机 A、平板 B、Windows C：A 与 B 都是 controller，C 是当前
authority。A 发出 `player.seek` 后，服务端只把第 6.7 节的 command 单播给 C；B 不接收，
A 也不会因为自己是请求者而执行该命令。A 收到 ACK，A/B/C 随后都可以收到新的
`playback.context.status`（前提是对应 Socket 已订阅该 Context）。因此 command payload
不需要也不得携带收件人 `targetClientId`：收件人已经由 Socket.IO 的 `to=<sid>` 决定。

`connectionNonce` / `connectionEpoch` 必须属于**收件 authority 当前物理连接**，不能沿用
请求者的值。Handoff 完成并更新 `authorityClientId` 后，后续相同 Context 的控制命令必须
自动解析并单播给新的 authority；旧 authority 不再接收执行命令。

普通非 Broadcast player control：

```json
{
  "type": "command",
  "action": "player.seek",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "controlVersion": 13,
    "sourceClientId": "controller-1",
    "executionTimeoutMs": 15000,
    "positionMs": 42000
  }
}
```

`player.play` / `pause` 可有 `positionMs`；`seek` 必须有；`next` / `prev` 不得有。所有普通 control 必有
`playbackContextId`、正 `controlVersion`、`sourceClientId`、正 `executionTimeoutMs`，可选
`dependsOnControlVersion`，并禁止 `baseControlVersion`、
`targetClientId` 与 `broadcastId`。

当普通 `player.*` command 由 active Broadcast control 产生时，payload 在上述 action 字段之外必须且
只可额外增加成组的 `effectiveAtServerMs:int>0`、`serverTimeMs:int>=0`；两者必须满足
`effectiveAtServerMs - serverTimeMs >= 250`，并逐值等于同一 broadcastRevision 的 ordinary mirror
push。两字段只能同时出现或同时省略。source authority 必须按 effective-at 调度该普通 Context command，
不得因 payload 没有 `broadcastId` 就立即执行；`broadcastId` 仍只属于 lifecycle/mirror 通道。此条件下
`player.play` / `player.pause` 的 `positionMs` 从普通可选收紧为必需，并必须等于对应 mirror snapshot
在 effective-at 时刻的目标位置；`player.seek` 仍使用请求目标，`queue.playItem` 的目标位置隐含为 0。

例如由 `broadcast.seek` 生成并发给 source authority 的仍是普通 Context command：

```json
{
  "type": "command",
  "action": "player.seek",
  "connectionNonce": "<source nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "controlVersion": 14,
    "sourceClientId": "controller-1",
    "executionTimeoutMs": 15000,
    "dependsOnControlVersion": 13,
    "positionMs": 42000,
    "effectiveAtServerMs": 1780000005000,
    "serverTimeMs": 1780000004500
  }
}
```

对应 ordinary participants 的 `broadcast.seek` mirror push 必须使用完全相同的
`effectiveAtServerMs:1780000005000` 与 `serverTimeMs:1780000004500`，但携带自己的完整
BroadcastSnapshot 和 broadcastRevision；source command 不得增加 `broadcastId`。

Windows 必须把收到的 `(playbackContextId, controlVersion)` 作为远程执行事务键。执行成功发送
remoteCommand committed；执行失败发送 remoteCommand failed；在 localUser confirmation 中被覆盖的
未完成事务不得继续执行。服务端不得把 ACK、accepted status 或 Socket emit 成功当作 committed。

队列选项控制：

```json
{
  "type": "command",
  "action": "queue.playItem",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "queueSongIds": ["song-1", "song-2"],
    "queueIndex": 1,
    "queueRevision": 8,
    "controlVersion": 13,
    "sourceClientId": "controller-1",
    "executionTimeoutMs": 15000
  }
}
```

除条件可选 `dependsOnControlVersion` 外上述字段必需，revision/version/timeout 均须 `>= 1`；禁止 legacy fields、`positionMs`、`trackId`、
`currentIndex`、`targetClientId` 与 `broadcastId`。若该 `queue.playItem` 由 active Broadcast control 产生，
还必须按上一段成组增加相同的 `effectiveAtServerMs` / `serverTimeMs`；普通非 Broadcast
`queue.playItem` 禁止两字段。

#### Dependency admission、execution eligibility 与 watchdog

track-changing action 固定为 `queue.playItem`、`player.next`、`player.prev`。服务端接受每个普通控制
时，必须在当前 Context/epoch 查找最高的、仍 pending 的较低 track-changing version：存在时把它写为
`dependsOnControlVersion`，不存在时省略。该规则适用于 track-changing transaction 本身，因此依赖链
可以传递；active Broadcast source 派生的普通 command 使用同一规则。

Windows 收到 dependency command 后先登记但不得执行。dependency canonical committed 后才进入
execution-eligible；dependency failed、execution_unknown、dependency_failed 或 superseded 时，Windows
丢弃本地后继。服务端从直接后继开始按 controlVersion 升序递归结算 dependency_failed，每条
settlement 的 dependsOnControlVersion 指向直接依赖。

```text
无依赖、无 effective-at：可靠加入 authority 发送路径时 executionEligibleAtMs
有依赖：dependency canonical committed 时才建立 executionEligibleAtMs
有 effective-at：executionEligibleAtMs = max(command enqueue/dependency committed time, effectiveAtServerMs)
watchdogDeadlineAtMs = executionEligibleAtMs + executionTimeoutMs + 2000
```

Windows AudioExecutionLease 也从 eligibility 开始；依赖等待不消耗 execution timeout。依赖在
eligibility 前失败时不建立 watchdog。依赖在 effective-at 后成功且迟到不超过 1000ms 时按 late policy
追赶，超过 1000ms 时不得执行成功，使用 remoteCommand failed `effective_at_missed` 结算。

### 6.7.2 Server-only `playback.control.settled`

authority disconnect、Socket replacement、服务端 restart 或 watchdog 到期造成结果不可证明时，服务端
不得伪造 authority `playback.update`，只能把事务结算为 `execution_unknown`。某个 dependency 进入
failed/unknown/dependency_failed 后，其 pending 后继按上述传递规则结算为 `dependency_failed`。每个
terminal version 发送一条：

```json
{
  "type": "event",
  "action": "playback.control.settled",
  "connectionNonce": "<recipient nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "epoch": 1,
    "commandControlVersion": 14,
    "status": "failed",
    "errorCode": "dependency_failed",
    "controlVersion": 15,
    "appliedControlVersion": 12,
    "requestingClientId": "controller-1",
    "requestingDeviceSessionId": "device:controller-1",
    "dependsOnControlVersion": 13,
    "serverUpdatedAtMs": 1780000009000
  }
}
```

payload 必需且只允许 `playbackContextId`、`epoch`、`commandControlVersion`、`status:"failed"`、
`errorCode`、`controlVersion`、`appliedControlVersion`、`requestingClientId`、
`requestingDeviceSessionId`、`serverUpdatedAtMs`，以及条件字段 `dependsOnControlVersion` 和可选
`errorMessage`。`errorCode` 只允许 `dependency_failed|execution_unknown`；dependency_failed 必须携带
正整数 dependsOnControlVersion，execution_unknown 禁止该字段。

settlement 幂等键为 `(playbackContextId, epoch, commandControlVersion)`。收件人是：仍为同一物理连接
的原请求 Socket、全部当前 Context subscribers，以及仍为最初 routed 物理连接的原 authority；按 sid
去重。原 requester 的 replacement Socket 不自动补历史 settlement。authority 收到 settlement 必须使
对应 AudioExecutionLease 失效；相同 terminal 重放不得再次 cascade、reconciliation 或推进 cursor。

### 6.7.3 Terminal-gap reconciliation

`remoteCommand failed`、`execution_unknown` 和 dependency_failed 链结束后留下的 gap 都必须收敛。
服务端只有在 authority exact pair/epoch/当前物理 Socket 匹配、fresh actual fact 合法、
`fact.appliedControlVersion <= canonical controlVersion`、该 applied 之后到 canonical 的事务全部
terminal、没有新 pending、actual track 在 distinct canonical queue 中唯一解析且该 gap 尚未处理时
执行。令 `N` 为处理前 canonical controlVersion、`R=N+1`，原子：

```text
创建 internal serverReconciliation record R
按 actual 收敛 Context state/currentIndex/trackId/position
Context.controlVersion = R
Context.version += 1
actual currentIndex 改变时 Context.queueRevision += 1
Context.epoch 不变
DevicePlaybackState 保存 actual playbackRate 和其他事实
DevicePlaybackState.appliedControlVersion = R
旧 failed/unknown/dependency transaction 保持原 terminal
```

internal record 没有客户端 request/action，也不创建 AudioExecutionLease。Context snapshot 不增加
playbackRate。active Broadcast 只从 R 派生一个 correction revision；Follow 只消费一次 R canonical
fact。

wire confirmation 必须唯一。由 passive fact 触发时，同一 clientSeq 返回一份 canonical
`playback.update(origin:"passive",controlVersion:R,appliedControlVersion:R,actual...)` 后再推 Context
status。remoteCommand failed 且无后续 pending、可在同一事务立即 reconciliation 时，只发送一份 failed
confirmation，其中 `commandControlVersion=N`、`controlVersion=R`、`appliedControlVersion=R`、原执行
errorCode 和 actual facts；这里 applied R 表示 reconciliation 已吸收 actual，不表示 command N 成功。
若仍有 pending，先发普通 failed，等 gap 全 terminal 后由 fresh passive fact 分配 R。迟到
remote committed/failed 不得改写旧 terminal；相同结果重放既有 outcome，不同结果返回 `conflict`。
