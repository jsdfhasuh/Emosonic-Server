# 阶段 1：客户端 Core 请求

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-07-23-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 5 节引言及第 5.1—5.2.2 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
## 5. 客户端到服务端：全部 strict-v2 请求

除第 3 节的认证、注册、设备列表和 heartbeat 外，以下是 Flutter 必须实现的 normative strict
action allowlist。所有请求均不得含 `sessionId` 或顶层 `targetClientId`。payload 内
target 字段也禁止，例外只有第 5.2 节 `device.setVolume` 与第 5.4 节 `playback.handoff.start`。

字段标记：`R` 必需；`O` 可选；`int>=0` 是 JSON number 且不小于零。

### 5.1 PlaybackContext 生命周期

| Action / type | payload | 服务端动作与响应 |
| --- | --- | --- |
| `playback.context.list` / `state` | `authorityClientId:R string`、`authorityDeviceSessionId:R string` | 仅 controller 可查询。服务端只在当前 authenticated user 的 active Context 中按两个字段精确匹配，并用同 requestId direct 返回第 6.1 节的 0/1/多个 binding；不得 ACK、不得自动订阅、不得返回 queue/playback snapshot。 |
| `playback.context.ensure` / `command` | `deviceSessionId:R string`、`queueSongIds:R distinct string[]`、`currentIndex:C int>=0`、`positionMs:R int>=0`、`state:R idle\|playing\|paused\|stopped` | 仅当前注册 player 可对自己调用；要求 `canPlay:true`，deviceSessionId 必须匹配当前连接。空队列要求 state=idle、positionMs=0 并省略 currentIndex；非空队列要求合法 currentIndex 且 state 非 idle。服务端按第 6.2 节原子返回、重绑、以本地快照初始化或创建该 stable clientId 的唯一 active Context，自动订阅当前 Socket，并使用同 requestId direct 返回完整 snapshot；不得 ACK，不接收 playbackContextId、trackId 或 target 字段。matching pair 为 restorePending 时是唯一失败结算例外：返回 `restore_in_progress` 且零副作用。 |
| `playback.context.subscribe` / `state` | `playbackContextId:R` | 将当前 Socket 加入该 context recipient set并返回 ACK；客户端随后显式请求 status。 |
| `playback.context.unsubscribe` / `state` | `playbackContextId:R` | 移除 context recipient；返回 ACK。 |
| `playback.context.status` / `state` | `playbackContextId:R` | 返回第 6.3 的完整 status（同 requestId 直接 action response）。 |
| `playback.context.prepare` / `command` | `playbackContextId:R`、`intentId:R string`、`baseControlVersion:R int>=0`、`initialQueueSongIds:O non-empty distinct string[]`、`currentIndex:C int>=0`、`positionMs:C int>=0` | 仅 controller 可调用。只允许对 idle Context 准备队列；可选初始队列存在时 currentIndex 与 positionMs 必需且 index 必须合法，不存在时两者必须省略。服务端按第 6.2.1—6.2.3 节 ACK、路由并结算。 |
| `playback.context.close` / `command` | `playbackContextId:R` | 当前 authority 或同用户 controller 可关闭。写入不可复用 tombstone，返回 ACK，并向所有订阅者推送 `playback.context.closed`。authority 若仍在线且没有进入应用退出流程，收到 closed 后必须立即重新 ensure 一个 idle Context。 |

Context 授权采用“用户域读取、角色控制”：同一 authenticated user 的已注册设备可 subscribe/status；
controller 可发起控制；当前 authority 必须是在线 player。对携带 `playbackContextId` 的跨用户
访问统一返回 `forbidden`，不得用 `not_found` 泄露资源是否存在；list 不接收 Context ID，按下述
规则先限定用户域并对不可见 binding 返回成功空数组。Context authority 持久绑定 `authorityClientId` 与
`authorityDeviceSessionId`；只有两者均匹配的重连才恢复 authority 路由。

本节表格中的一般授权不覆盖第 5.5 节 Broadcast source-ownership/suspended-Context 写屏障。任何
ensure、close、prepare、queue/player/update 或 binding mutation 在获得角色授权后仍必须检查该屏障；
命中时按第 5.5 节返回 `conflict`。

`playback.context.list` 是 remote-control scope discovery，不是模糊搜索：

1. `authorityClientId` 取自所选 `device.list` 项的 `clientId`；
2. `authorityDeviceSessionId` 取自同一项的 `deviceSessionId`；
3. 两个字段都必须精确匹配持久化 Context binding。服务端不得只匹配 clientId，不得使用请求者
   自己的 deviceSessionId，也不得回退到任何 `sessionId`；
4. 查询始终先按当前 authenticated user 限定数据域，再筛选 `lifecycle=active` 和 authority/device
   pair。其他用户的设备或 Context 对该查询不可见，并产生成功空数组；
5. authority 暂时离线不改变 active binding，list 可以返回该 Context；客户端随后发控制命令时
   再由服务端检查当前 Socket，离线则返回 `authority_offline`；
6. 完成注册和 ensure 的在线 player 应精确返回 1 个 Context；返回 0 个只允许发生在 ensure 尚未完成、
   Context 已关闭但新 ensure 尚未完成或服务端异常的短暂窗口，controller 必须等待
   `playback.context.bindings.changed` 并有界重查，不能把“未播放”解释成空结果；返回 1 个时可自动
   subscribe/status；
   返回多个时服务端不得按更新时间、播放状态、队列内容或数组第一项替客户端选择。Flutter 必须
   清除所选设备现有的 Context 状态与控制 cursor，必要时 unsubscribe 旧 Context，并进入本地
   `ambiguous_playback_scope` 状态；不得自动选择，也不得允许用户从 opaque Context ID 中任选一个
   继续控制。该状态不是 wire error code；只有未来契约提供服务端权威的真实音频 active 标记后，
   才能在多结果中选取一个 Context；
7. list 不建立 subscription，也不返回可直接应用的播放状态。客户端选定唯一 Context 后必须依次执行
   `playback.context.subscribe` 和 `playback.context.status`，并只使用 status 返回的 canonical
   `controlVersion` / `queueRevision` 发送控制请求；
8. 客户端在收到匹配 pair 的 `playback.context.bindings.changed`、`playback.context.closed`、
   Handoff completed 后 authority 变化，或新的 `device.list` 显示目标 deviceSessionId 变化/重连
   时，必须立即暂停控制，使旧 device→Context binding 失效，并用新 requestId 重新 list。若 status
   中的 `authorityClientId` 已不等于所选设备，也必须停止以该设备为目标的控制；不得继续沿用旧选择。

### 5.2 队列、播放状态和控制

设备级音量不属于 PlaybackContext mutation，可在目标在线但没有 Context 时使用：

| Action / type | payload | 服务端动作与响应 |
| --- | --- | --- |
| `device.setVolume` / `command` | `targetClientId:R`、`targetDeviceSessionId:R`、`volume:R int 0..100` | 请求者必须是同用户 controller 且协商 `remoteVolumeControl:true`。服务端按 client/device pair 精确解析当前 Socket；目标必须是 player，并同时协商 `remoteVolumeControl:true`、`canSetVolume:true`。成功向目标单播第 6.0 节 command 并 correlated ACK；目标不存在、离线或 device session 已替换时返回 `not_found`。不得创建、读取或修改 PlaybackContext。 |
| `device.volume.update` / `event` | `deviceSessionId:R`、`volume:R int 0..100`、`clientSeq:R int>=1` | player 上报当前物理连接的实际音量。服务端验证 source client/device/nonce，更新在线瞬态状态，并按第 6.0 节发送 canonical event confirmation；不回 ACK、不持久化、不修改 Context。 |

`device.setVolume` 的 ACK 只证明服务端已验证并把 command 加入目标当前 Socket 的发送路径，不证明
硬件已经达到请求值。controller 必须以随后 `device.volume.update` 的实际值为准。设备本地音量变化
也应主动发送 `device.volume.update`；player 若同时有活动 Context，还应在正常 `playback.update`
中包含相同实际 volume，以维持该 Context 的 DevicePlaybackState。

`baseControlVersion` / `baseQueueRevision` 是客户端请求的乐观并发前置条件。服务端在 `2.8.x`
中必须继续接受并按下表校验这些字段；若 cursor 已过期，返回第 4.2 节的 correlated
`system.error`。服务端接受请求后向客户端推送 canonical `controlVersion` / `queueRevision`，
不得把请求字段 `baseControlVersion` / `baseQueueRevision` 原样转发给 authority。

| Action / type | payload | 服务端动作与响应 |
| --- | --- | --- |
| `queue.context.sync` / `state` | `playbackContextId:R`、`deviceSessionId:R`、`queueSongIds:R distinct string[]`、`currentIndex:C int>=0`、`positionMs:R int>=0`、`positionSampledAtServerMs:R int>=0`、`baseQueueRevision:R int>=0`、`baseControlVersion:C int>=0` | 仅当前 authority 可发送，deviceSessionId 必须匹配 authority 连接。空队列时 currentIndex 必须省略且 positionMs 必须为 0；非空队列时 currentIndex 必需且合法。采样时间按第 5.2.1 节验证；队列内容变化校验 baseQueueRevision；index、当前 track、position 或 idle/non-empty 边界变化时 baseControlVersion 必需。按第 4.5 节递增 cursor，ACK 并推送第 6.5 节 canonical queue state。 |
| `playback.context.prepared` / `event` | `playbackContextId:R`、`deviceSessionId:R`、`intentId:R`、`ready:R bool`、`errorCode:C queue_required\|restore_failed\|prepare_timeout\|authority_changed`、`errorMessage:O string` | 当前 authority 对 prepare 给出 event-confirmed 结果。`ready:true` 时 Context 必须已通过 queue sync 变成非空，error 字段禁止；`ready:false` 时 errorCode 必需。服务端不回 ACK，按第 6.2.3 节广播结果。 |
| `playback.update` / `event` | 第 5.2.1 节的公共字段，以及由 `origin` / `executionStatus` 决定的闭合条件字段 | 仅当前 authority player 可发送。passive 只更新事实；remoteCommand 结算服务端已接受的控制事务；localUser 表示已经完成的 Windows 本地人工操作并由服务端分配新版本。三种 shape 都使用第 6.6 节无 requestId canonical confirmation 结算，不回 ACK。 |
| `queue.playItem` / `command` | `playbackContextId:R`、`queueIndex:R int>=0`、`baseQueueRevision:R int>=0`、`baseControlVersion:R int>=0` | 验证 cursors 后选择队列项，向 authority 发送第 6.7 的 server-routed control，返回 ACK；所有 recipients 接收 queue/context state 更新。不同 Socket 间不承诺到达顺序。 |
| `player.play` / `command` | `playbackContextId:R`、`baseControlVersion:R int>=0`、`positionMs:O int>=0` | 验证 authority/cursor 后递增控制版本，并向 authority 发送无 target 的第 6.7 control。 |
| `player.pause` / `command` | 同 `player.play` | 同上。 |
| `player.seek` / `command` | `playbackContextId:R`、`baseControlVersion:R int>=0`、`positionMs:R int>=0` | 同上，`positionMs` 必须有。 |
| `player.next` / `command` | `playbackContextId:R`、`baseControlVersion:R int>=0` | 同上。 |
| `player.prev` / `command` | 同 `player.next` | 同上。 |

所有 player control 的请求者必须具有 controller 角色。服务端按 action 校验当前 authority 的
negotiated `canPlay` / `canPause` / `canSeek`；请求 controller 不需要具备对应播放能力。能力不足
返回 `capability_required`，authority 离线返回 `authority_offline`，两者都不得改变 cursor。
`queue.playItem`、`player.next`、`player.prev` 要求 authority `canPlay:true`。

Context 为 idle 时，`queue.playItem` 和全部 `player.*` 请求返回 `queue_required`，不得递增 cursor、
不得向 authority 路由普通控制。controller 必须先完成 `playback.context.prepare`，观察同一个 Context
变为非空，然后使用最新 canonical controlVersion 发送原始控制意图。

为使多 Context fail-closed 不依赖 invalidation event 的到达速度，服务端接受任一
`queue.playItem` / `player.*` 控制前，必须在与该控制 mutation 相同的原子临界区内验证：当前
Context 是其 `authorityClientId` / `authorityDeviceSessionId` pair 唯一的 active Context。若同一
pair 存在其他 active Context，返回 `conflict`，并在 error payload 中带请求的
`playbackContextId`、`currentControlVersion`、`currentQueueRevision`、`currentVersion`；不得向
authority 发送命令，也不得修改任何 cursor。ensure、close、handoff authority switch 与普通
player control 必须按 authority/device pair 串行化，不能在“检查唯一性”和“提交控制”之间插入
binding mutation。

### 5.2.1 `playback.update` 请求的闭合 shape

所有 playback.update 请求公共必需字段：

```text
playbackContextId:R string
deviceSessionId:R string
origin:R passive|remoteCommand|localUser
state:R idle|playing|paused|stopped
positionMs:R int>=0
positionSampledAtServerMs:R int>=0
playbackRate:R number 0.5..2.0
clientSeq:R int>=1
trackId:C string
volume:O int 0..100
muted:O bool
```

只有当前 Context authority 的当前 client/device/Socket 可以发送。服务端必须验证 authenticated user、
authorityClientId、authorityDeviceSessionId、connectionNonce/connectionEpoch 和 clientSeq 作用域；
controller-only 或旧 authority 返回 `forbidden`，旧 device session 返回 `conflict`。

公共状态规则：

- `playbackRate` 必须是有限 number 且 `0.5 <= playbackRate <= 2.0`；idle Context 仍必须上报播放器当前有效速度；
- `positionSampledAtServerMs` 是客户端读取 positionMs 的同一瞬间，用第 3.5 节 clock offset
  换算的 server epoch milliseconds；它不是发送时间或预测的接收时间。服务端接收时
  若该值超过当前 server time + 1000ms 则返回 `bad_request`；要用于 effective-at 投影时还必须
  满足第 3.5 节 50ms clock uncertainty 与下文 freshness 门槛；
- idle Context 只允许 `state:"idle"`、`positionMs:0` 并省略 trackId；
- queue-backed Context 不允许 state idle，trackId 必需；
- passive/remoteCommand 的 trackId 按 `appliedControlVersion` 对应的已提交事务或已保存 applied
  snapshot 校验；localUser 的 trackId 按绝对 queueIndex 校验。当 `controlVersion >
  appliedControlVersion` 时，实际 track 可以暂时不同于主 Context 最新控制目标；不得继续无条件与
  最新 canonical current item 比较；
- 请求禁止 `sourceClientId`、`controlVersion`、`supersededThroughControlVersion`、queueSongIds、
  currentIndex、queueRevision、version、serverUpdatedAtMs、target 字段和 JSON null。
- state、trackId 或 playbackRate 变化必须立即发送；当前 authority 处于 playing 时还必须至少每
  1000ms 发送一次 passive `playback.update`。DevicePlaybackState.serverUpdatedAtMs 由服务端按接受
  时间生成，客户端不得提供；positionSampledAtServerMs 保留客户端上报的合法采样时间。
  playing state 的 serverUpdatedAtMs 或 positionSampledAtServerMs 任一超过 2000ms 时仍可用于历史
  显示，但不得作为 Broadcast start 或需要 playing position anchor 的 Broadcast control 依据。

四种有效组合如下：

| origin / status | 额外必需字段 | 条件/禁止字段 | 服务端语义 |
| --- | --- | --- | --- |
| `passive` | `appliedControlVersion:int>=1` | 禁止 executionStatus、commandControlVersion、intentId、epoch、observedControlVersion、queueIndex、error 字段 | 更新进度、状态、音量等事实；applied 必须等于 lastApplied，不推进任何 Context cursor |
| `remoteCommand` / `committed` | `executionStatus:"committed"`、`commandControlVersion:int>=1`、`appliedControlVersion:int>=1` | applied 必须等于 command；禁止 intentId、epoch、observedControlVersion、queueIndex、error 字段 | 只允许按序结算匹配的 pending command；推进 lastApplied，不再次推进 controlVersion |
| `remoteCommand` / `failed` | `executionStatus:"failed"`、`commandControlVersion:int>=1`、`appliedControlVersion:int>=1`、`errorCode` | applied 必须小于 command；`errorMessage:O string`；禁止 intentId、epoch、observedControlVersion、queueIndex | 将 pending command 结算为 failed；必要时使用新 Context version/Queue revision恢复实际 snapshot，但 controlVersion 不变 |
| `localUser` / `committed` | `executionStatus:"committed"`、`intentId:string`、`epoch:int>=1`、`observedControlVersion:int>=1`、`queueIndex:int>=0`、`trackId:string` | state 只能 playing/paused/stopped；queueIndex 必须在 canonical queue 内且 trackId 匹配；禁止 appliedControlVersion、commandControlVersion、error 字段 | 接受本地人工最终结果，从当前 canonical controlVersion +1，并 supersede 旧 pending remote |

remoteCommand failed 的稳定 `errorCode` 固定为：

```text
playback_failed
track_load_failed
seek_failed
execution_timeout
effective_at_missed
clock_unsynchronized
rate_unsupported
```

`errorMessage` 只用于诊断，客户端逻辑必须依据 errorCode。commandControlVersion 不存在、不是该
authority 收到的命令、已经 terminal，或与 action 预期结果冲突时返回 `conflict`，不得改写事务。

localUser 仅表示已经由 Windows 音频层 committed 的人工 play/pause/seek/选歌结果。Windows 本地
next/previous 必须在上报前转换成绝对 queueIndex/trackId。自然播完后的自动下一首不得伪装成
localUser，也不得获得 supersede 权限；authority 必须先以同一 queue、新
currentIndex 和实际 positionMs 发送 `queue.context.sync`，在 canonical queue/context 更新后才可发送
对应 passive `playback.update`。完整 queue 内容改变同样必须先走显式
`queue.context.sync`。

服务端接受 localUser update 时必须与普通 control、ensure、close、handoff 和 remote result 结算在
同一 Context 串行区内执行：

1. 验证当前 authority binding、epoch、intentId 和绝对 queueIndex/trackId；
2. 允许 observedControlVersion 小于或等于当前 canonical；大于 canonical 返回 bad_request；
3. 令 `supersededThroughControlVersion` 等于接受前的 canonical controlVersion；
4. 从当前 canonical 值加一，写入新的 controlVersion；
5. 更新实际 state/position/currentIndex；currentIndex 改变时递增 queueRevision；
6. 将所有版本不高于 supersededThrough 且仍为 pending 的远程事务标记为 superseded；已经 committed
   或 failed 的历史不回滚；
7. 将该 authority 的 appliedControlVersion 同步推进到新的 controlVersion；
8. 持久化后按第 6.6 节向源 Socket 和全部合法 recipients 推送 canonical localUser confirmation。

本地操作失败不得发送 committed localUser shape，不得推进版本或 supersede。Windows 必须解除本地
执行屏障并通过 passive 实际状态或本地 UI 错误完成恢复；本 r18 不定义 localUser failed wire shape。

### 5.2.2 远程控制事务与实际执行

服务端接受 queue.playItem 或 player.* 后，必须创建
`(playbackContextId, epoch, controlVersion)` 唯一事务并设置 `status:"pending"`。ACK 只表示请求已
验证、版本已分配且命令已可靠加入当前 authority Socket 的发送路径，不表示音频已经执行。

事务状态机固定为：

```text
pending -> committed
pending -> failed
pending -> superseded
```

terminal 状态不可互换。Windows 收到 server-routed command 后必须按 controlVersion 串行执行，只有
AudioPlayerService/PlaybackActor 返回实际 song/index/state/position 的 committed snapshot 时才发送
remoteCommand committed。加载中的旧 track、临时 pause、buffering 和播放器回调不能作为结算。

服务端接受 remoteCommand committed/failed 前还必须验证：commandControlVersion 不低于当前
lastApplied，且它之前不存在仍为 pending 的更低控制事务；已经 failed/superseded 的低版本可以跨过。
如果更低版本仍 pending，返回 conflict 并记录 Windows 执行乱序，不能提前把 applied 跳到更高版本。

Windows 本地人工操作开始后必须暂缓尚未 committed 的远程事务；收到匹配 intentId 的 localUser
canonical confirmation 后，丢弃所有 `controlVersion <= supersededThroughControlVersion` 且仍未完成
的本地远程事务。服务端负责判定和持久化 supersede，Windows 的屏障负责阻止已经送达的旧命令晚于
本地结果执行。更新版本的新远程命令仍可正常执行。
