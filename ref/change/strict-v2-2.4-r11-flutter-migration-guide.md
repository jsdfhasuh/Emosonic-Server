# EmoSonic strict-v2 `2.4.0` r11 Flutter 改造说明

> 日期：2026-07-17
> 面向：Flutter Android、Flutter Windows 和共享 realtime 层工程师
> 状态：最终 r11 客户端实现输入，不表示服务端或 Flutter 已经实现完成
> 权威契约：`specs/emosonic_strict_v2_socketio_server_contract.md` r11

本文说明 Flutter 客户端从当前 strict-v2 `2.3.0` / contract r8 升级到 `2.4.0` / r11 时必须修改的
行为。本文是迁移指南，不替代权威 wire contract；字段或行为冲突时始终以 r11 主契约为准。

旧文件 `strict-v2-2.4-r10-flutter-migration-guide.md` 已被废弃，不得继续作为实现输入。

## 1. 本次升级改变了什么

r11 是一套不兼容 r8 的完整 strict-v2 shape，主要变化是：

1. `playback.context.create` 改为 player 启动后的 `playback.context.ensure`；
2. 没有队列的在线 player 也必须拥有 idle Context；
3. Android 对 idle Context 播放前必须执行 prepare；
4. ACK 只表示服务端已接受并路由，不能表示 Windows 已执行成功；
5. `playback.update` 分为 passive、remote committed、remote failed 和 localUser committed 四种闭合形状；
6. 服务端增加 server-only `playback.control.settled`，结束没有新 Windows 实际反馈的控制事务；
7. server-routed control 增加 `executionTimeoutMs`，默认 15000ms；
8. Windows 负责 15 秒 audio execution lease，服务端 watchdog 固定多 2000ms；
9. dependency failure、unknown、断线、重启和迟到反馈都有明确恢复规则；
10. Windows 必须接收 settled 并取消对应本地事务，防止服务端已经失败、电脑随后仍执行。

Flutter 不得同时保留 r8 create、旧 playback.update、`player.authorityIntent` 或 session fallback 分支。

## 2. 版本和注册

- 协议版本继续是 `2.4.0`；
- 契约修订是 r11；
- major 必须为 2，minor 必须 `>=4`；
- `2.3.x` 或其他 major 必须 fail-closed；
- `schemaHash` 和 `serverBuildCommit` 只记录当前部署，不永久固定到客户端安装包。

### 2.1 capabilities 固定为 10 个 bool

probe 和 negotiated reconnect 都发送完整字段：

```json
{
  "playbackContextV2": true,
  "playbackPrepare": false,
  "effectiveAtPlayback": false,
  "canPlay": true,
  "canPause": true,
  "canSeek": true,
  "canSetVolume": true,
  "supportsFollow": false,
  "supportsBroadcast": false,
  "remoteVolumeControl": true
}
```

删除 r8 的 9/10 字段双 shape。严格解析器应拒绝缺字段、额外字段、非 bool，以及
`effectiveAtPlayback:true + playbackPrepare:false`。

### 2.2 两次连接流程不变

```text
probe auth/register
  -> 保存服务器 profile
  -> 主动断开
  -> negotiated reconnect
  -> 再次 auth/register
```

每个新物理连接有新的 `connectionNonce`，`connectionEpoch` 为 1。注册后的所有入站消息必须匹配当前
nonce/epoch；旧连接消息不得写入状态、结束事务或执行音频命令。

## 3. Windows 启动：create 改为 ensure

删除客户端 action：

```text
playback.context.create
```

Windows 完成 negotiated register 后：

1. 读取本地可恢复队列和实际播放状态；
2. 立即发送 `playback.context.ensure`；
3. 保存服务端生成的 playbackContextId 和 cursors；
4. 在线期间保持一个唯一 active Context。

已有队列示例：

```json
{
  "type": "command",
  "action": "playback.context.ensure",
  "requestId": "ensure-1",
  "payload": {
    "deviceSessionId": "windows-session-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "state": "paused",
    "positionMs": 1200
  }
}
```

确实没有队列时发送 idle：

```json
{
  "type": "command",
  "action": "playback.context.ensure",
  "requestId": "ensure-idle-1",
  "payload": {
    "deviceSessionId": "windows-session-1",
    "queueSongIds": [],
    "state": "idle",
    "positionMs": 0
  }
}
```

空队列禁止 currentIndex/trackId。非空队列必须有合法 currentIndex，state 不能是 idle。

首次 ensure 初始化：

```text
controlVersion = 1
appliedControlVersion = 1
```

Windows 不得再发送额外 localUser update 把初始版本增加到 2。

## 4. Android 发现 Context

选择 Windows 后固定执行：

```text
device.list
  -> playback.context.list(authorityClientId + authorityDeviceSessionId)
  -> playback.context.subscribe
  -> playback.context.status
```

`device.list` 只发现设备，不携带 Context。list 返回 1 个才能自动控制；多个结果进入本地
`ambiguous_playback_scope` 并停止控制。

收到 binding changed、Context closed、Handoff authority 变化、deviceSessionId 变化或 Socket reconnect
时，清除旧选择和 cursors，用新 requestId 重新 list/subscribe/status。

## 5. idle Context 第一次播放

idle Context 的固定状态是：

```text
queueSongIds = []
state = idle
positionMs = 0
currentIndex/trackId 不存在
```

Android 不能直接发送 `player.play`。固定流程：

1. 发送 `playback.context.prepare`，携带 intentId 和最新 baseControlVersion；
2. Windows 恢复本地队列或采用 initialQueueSongIds；
3. Windows 使用 `queue.context.sync` 把同一个 Context 变成非空 paused；
4. Windows 发送 `playback.context.prepared`；
5. Android 等待 canonical queue 非空；
6. 使用最新 controlVersion 只发送一次 `player.play`。

两端都没有队列时显示 `queue_required`，不得永久 loading，也不得要求用户再次点击恢复。

## 6. 远程命令和 ACK

Android 发送 `queue.playItem` 或 `player.*` 后：

- ACK 只表示 accepted/routed；
- ACK 后 UI 进入 pending_execution，不显示成功；
- 请求 baseControlVersion=N 且成功 ACK 时，该命令版本是 N+1；
- Windows committed playback.update 才表示实际成功；
- Windows failed playback.update、server settled 或 localUser supersede 都能结束 pending。

等待命令建议使用：

```text
playbackContextId + epoch + controlVersion
```

requestId 只用于当前连接请求关联，不作为长期事务主键。

## 7. server-routed command 增加 executionTimeoutMs

普通 `queue.playItem` 和全部 `player.*` server-routed command 都必须带：

```json
{
  "executionTimeoutMs": 15000
}
```

完整 seek 示例：

```json
{
  "type": "command",
  "action": "player.seek",
  "connectionNonce": "<windows nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "context-1",
    "controlVersion": 47,
    "sourceClientId": "android-1",
    "executionTimeoutMs": 15000,
    "positionMs": 42000
  }
}
```

这里 `sourceClientId` 仍是 server-routed command 中的原控制端。它不等于 settled 的字段名；
`playback.control.settled` 必须使用 `requestingClientId`。

Handoff commit 继续使用自己的 effective-at 和 complete timeout，不混入本执行租约。

## 8. 两个播放版本必须同时保存

- `controlVersion`：服务端最新接受到哪条控制命令；
- `appliedControlVersion`：Windows 实际成功执行到哪条命令。

合法状态：

```text
controlVersion = 48
appliedControlVersion = 47
```

Android 显示实际歌曲、状态、位置时使用 deviceStates/playback.update 的 applied snapshot；发送新命令
仍使用主 Context 最新 controlVersion。pending 期间实际歌曲可以与最新控制目标不同。

## 9. Windows 的四种 playback.update

所有请求都来自当前 authority，并携带 Windows 自己的 clientSeq。

### 9.1 passive

```json
{
  "origin": "passive",
  "appliedControlVersion": 47,
  "state": "playing",
  "trackId": "song-2",
  "positionMs": 5000,
  "clientSeq": 18
}
```

只更新实际状态，不推进 Context cursor。

### 9.2 remoteCommand committed

```json
{
  "origin": "remoteCommand",
  "executionStatus": "committed",
  "commandControlVersion": 47,
  "appliedControlVersion": 47,
  "state": "playing",
  "trackId": "song-2",
  "positionMs": 0,
  "clientSeq": 19
}
```

只有音频层最终 committed snapshot 才能发送。loading、buffering、旧 track 回调不能冒充成功。

### 9.3 remoteCommand failed

```json
{
  "origin": "remoteCommand",
  "executionStatus": "failed",
  "commandControlVersion": 47,
  "appliedControlVersion": 46,
  "errorCode": "track_load_failed",
  "state": "paused",
  "trackId": "song-1",
  "positionMs": 32000,
  "clientSeq": 20
}
```

Windows 可发送的固定错误码只有：

```text
playback_failed
track_load_failed
seek_failed
execution_timeout
```

### 9.4 localUser committed

```json
{
  "origin": "localUser",
  "executionStatus": "committed",
  "intentId": "local-intent-123",
  "epoch": 1,
  "observedControlVersion": 46,
  "queueIndex": 1,
  "trackId": "song-2",
  "state": "playing",
  "positionMs": 0,
  "clientSeq": 21
}
```

Windows 不生成新 controlVersion。服务端返回新 controlVersion、appliedControlVersion 和
supersededThroughControlVersion。本地 next/previous 转成绝对 queueIndex/trackId；自然自动下一首不能
使用 localUser。

## 10. Server-only playback.control.settled

服务端能够结束控制事务、但没有新的 Windows 实际播放反馈时使用：

```json
{
  "type": "event",
  "action": "playback.control.settled",
  "connectionNonce": "<recipient nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "context-1",
    "epoch": 1,
    "commandControlVersion": 48,
    "status": "failed",
    "errorCode": "dependency_failed",
    "dependsOnControlVersion": 47,
    "controlVersion": 49,
    "appliedControlVersion": 46,
    "requestingClientId": "android-1",
    "serverUpdatedAtMs": 1780000001200
  }
}
```

### 10.1 严格字段规则

| 字段 | 规则 |
| --- | --- |
| `playbackContextId` | 必需，事务所属 Context |
| `epoch` | 必需，事务所属 generation |
| `commandControlVersion` | 必需，被结束的具体命令版本 |
| `status` | 必需，当前只能是 `failed` |
| `errorCode` | 必需，只允许 `dependency_failed|execution_unknown` |
| `dependsOnControlVersion` | dependency_failed 必需；execution_unknown 禁止 |
| `controlVersion` | 必需，服务端当前最新控制版本 |
| `appliedControlVersion` | 必需，Windows 最后确认的实际执行版本 |
| `requestingClientId` | 必需，最初发送命令的 controller |
| `errorMessage` | 可选安全诊断字符串 |
| `serverUpdatedAtMs` | 必需，毫秒时间戳 |

禁止字段：

```text
requestId
clientSeq
deviceSessionId
sourceClientId
trackId/state/positionMs/volume/muted
targetClientId
```

该事件只能由服务端发送。客户端发送同名 action 必须失败。settled 只把 transaction 从 pending 改为
failed，不能修改 DevicePlaybackState、歌曲、状态、位置、队列或 Windows clientSeq。

`requestingClientId` 始终是原始控制端。例如 Android 发出命令、Windows 执行时，它等于 Android，不能
改回容易混淆的 sourceClientId。

### 10.2 服务端写入和发送顺序

```text
验证事务仍为 pending
  -> 原子写入 failed + errorCode
  -> 持久化成功
  -> 按 commandControlVersion 从小到大逐条发送 settled
```

不能先 push 后写数据库。一条命令对应一条 settled，不能用一个区间让 Flutter 自己猜。

### 10.3 settled 幂等

唯一键固定为：

```text
playbackContextId + epoch + commandControlVersion
```

- 第一次收到：把对应命令改成 terminal；
- 相同键、相同内容：忽略，不重复提示、不重复刷新、不重复改变 UI；
- 相同键、不同 status/errorCode：协议冲突，记录错误并请求最新 status，不覆盖第一次 terminal；
- 旧版本 settled 可以补齐旧 pending，但不能回滚更新版本实际播放状态。

客户端不能只依赖消息到达顺序，必须保存上述 terminal 主键。

### 10.4 settled 必须发送给 Windows

最初接收该 transaction 的 authority Windows Socket 若仍在线，必须收到 settled。Windows 收到后：

1. 按 playbackContextId + epoch + commandControlVersion 找到本地事务；
2. 立即使对应 audio execution lease 失效；
3. 从本地执行队列删除事务；
4. 禁止迟到 Future/callback 切歌；
5. 禁止迟到 callback 发送 committed；
6. 禁止迟到 callback 修改 Context、Queue projection 或系统媒体状态。

settled 不生成新的实际播放状态。Windows 的实际歌曲、状态和位置仍通过自己的 passive
`playback.update` 上报。

## 11. 切歌失败和后续命令

依赖取消只由以下根命令触发：

```text
queue.playItem
player.next
player.prev
```

例子：

```text
47 queue.playItem -> Windows playback.update failed / track_load_failed
48 player.pause   -> server settled / dependency_failed / dependsOn=47
49 player.seek    -> server settled / dependency_failed / dependsOn=47
```

服务端必须先原子持久化 48、49 的 failed，再按 48、49 顺序分别发送 settled。Windows 丢弃 48、49；
Android 按具体版本结束 pending，合并请求一次 status，不自动重发。

普通 play/pause/seek 自身失败只结束自己，不自动创建依赖图。

## 12. Windows 15 秒执行租约

Windows 收到命令并进入本地执行队列后，以 `executionTimeoutMs` 启动 lease。

在期限内：

- 成功：发送 committed；
- 确认实际失败：发送对应 failed；
- 到达期限且能证明 lease 已失效：发送 failed/execution_timeout。

启用 execution_timeout 前，Windows 必须证明：

- lease 到期后已失效；
- 仍在加载的音频操作可停止、取消或隔离；
- 迟到 Future/callback 不能切歌；
- 迟到 callback 不能发送 committed；
- 不能修改 Context、Queue 或系统媒体状态；
- 更高版本命令仍能正常执行。

如果音频后端做不到，不得发送 execution_timeout，也不能声称命令已经安全取消。

## 13. 服务端 17 秒 watchdog

```text
Windows execution lease = executionTimeoutMs，默认 15000ms
server watchdog = executionTimeoutMs + 2000ms，默认 17000ms
```

服务端 accepted/routed 后启动 watchdog。17 秒仍没有 Windows terminal feedback 时，只能：

```text
playback.control.settled(errorCode="execution_unknown")
```

服务端不能生成 failed playback.update、不能复用 Windows clientSeq，也不能声称电脑已经安全取消。

`execution_unknown` 只表示服务端不知道结果。每条事务按自己的 feedback/watchdog 独立结算，不能把
unknown 根命令的后续事务自动写成 dependency_failed。

## 14. 断线、Socket 替换和重启

authority 断线、Socket 被替换或服务端仍在线时发生连接失效：

1. 所有相关 pending 写为 failed/execution_unknown；
2. 每条命令分别发送 settled；
3. settled 发给仍在线 controller；旧 authority Socket 若尚未断开，也必须收到并取消事务；
4. Windows 清除对应 lease 和队列项；
5. 不自动重发 next、previous、seek 或其他旧命令。

Socket 已断开或被替换后，不向新物理连接补发历史 settled。完整服务端重启后所有物理 Socket 都变化，
也不补发历史 settled。Flutter 必须因新 nonce 清除旧
pending，然后重新：

```text
auth/register
player ensure
controller list/subscribe/status
```

以新 status 为准，等待用户重新操作。

## 15. 迟到旧 playback.update

当：

```text
incoming appliedControlVersion < server.lastAppliedControlVersion
```

服务端：

- 不写入旧状态；
- 不广播给 Android；
- 只向原 Windows Socket 返回当前保存的 canonical passive update；
- 不产生新 clientSeq；
- 不把它当成新的 Context mutation；
- 相同 requestId 重试返回同一 source-only correction。

Windows 看到服务端 applied 更高后更新本地认知，停止重复发送旧 snapshot。

同一 command 已 committed 后又报告 failed，或 failed 后又报告 committed，仍是 `conflict`。

## 16. Android/controller 状态机

```text
accepted
  -> pending_execution
      -> committed              # Windows playback.update
      -> failed                 # Windows playback.update
      -> dependency_failed      # server settled
      -> execution_unknown      # server settled / reconnect
      -> superseded             # localUser confirmation
```

处理规则：

- ACK：进入 pending_execution；
- committed：显示实际成功；
- Windows failed：显示实际旧状态并刷新；
- dependency_failed：按具体 settled version 清除 pending，合并刷新一次 status；
- execution_unknown：清除对应或旧连接全部 pending，重新水合，等待用户重新操作；
- localUser：按 supersededThroughControlVersion 清除旧 pending；
- settled 重复：按幂等键忽略；
- settled 冲突：保留第一次 terminal，记录错误并刷新 status；
- 不自动重发 dependency_failed、execution_timeout 或 execution_unknown 命令。

推荐提示：

| 结果 | 提示 |
| --- | --- |
| `track_load_failed` | 歌曲加载失败 |
| `seek_failed` | 跳转失败 |
| `playback_failed` | 播放操作失败 |
| `execution_timeout` | 执行超时，已安全取消 |
| `dependency_failed` | 前一条切歌失败，后续操作已取消 |
| `execution_unknown` | 执行结果无法确认，请刷新后重试 |

## 17. 删除的旧行为

必须删除或关闭：

- `playback.context.create`；
- 非空队列专用 Context parser；
- `player.authorityIntent`；
- 无 origin/applied cursor 的旧 playback.update；
- 服务端生成或伪造 Windows failed playback.update；
- 服务端复用 Windows clientSeq；
- Flutter 根据版本区间猜 dependency failure；
- ACK 后立即显示成功；
- 重连后自动恢复或重放未确认命令；
- 把迟到旧状态写入 Android 全局 UI；
- strict sessionId/sourceSessionId fallback；
- r8 的 9/10 capability 双 shape。

保持不变：Socket.IO `/emo`、event `message`、requestId/action 关联、nonce/epoch provenance、设备级音量
和 optional Follow/Broadcast/Handoff capability gate。

## 18. Flutter 实施清单

### 共享 realtime/model

- [ ] 最低版本改为 2.4.0，按 r11 固定解析 10 capabilities。
- [ ] 删除 create 和 authorityIntent。
- [ ] 增加 ensure、idle、prepare/prepared。
- [ ] playback.update 改成四种严格 shape。
- [ ] 增加 server-only playback.control.settled 严格解析。
- [ ] settled 要求 requestingClientId，禁止 sourceClientId/clientSeq/deviceSessionId。
- [ ] 保存 controlVersion 和 appliedControlVersion。
- [ ] 按 Context/epoch/commandVersion 幂等保存 terminal。
- [ ] 支持 source-only stale correction。

### Windows

- [ ] register 后读取本地快照并立即 ensure。
- [ ] 按 controlVersion 串行执行。
- [ ] 使用 command.executionTimeoutMs 建立 lease。
- [ ] committed/failed 只使用音频层最终实际状态。
- [ ] 证明并测试 lease 失效和迟到 callback 隔离。
- [ ] 收到 settled 后取消 lease、删除队列项、禁止迟到 committed。
- [ ] 切歌根失败后丢弃全部更高 pending。
- [ ] disconnect/reconnect 不恢复旧事务。
- [ ] 处理 source-only correction。

### Android/controller

- [ ] 使用 list -> subscribe -> status 水合 Context。
- [ ] idle 播放使用 prepare -> queue ready -> 单次 play。
- [ ] ACK 后显示 pending。
- [ ] committed/failed/superseded 正确结束命令。
- [ ] dependency_failed 按每条 settled 具体版本清理。
- [ ] execution_unknown 清理和重新水合。
- [ ] settled 重复和冲突按固定幂等规则处理。
- [ ] control > applied 时显示有界执行中，不回滚实际歌曲。

## 19. 必须通过的测试

1. 固定 10 capability register 成功；2.3 profile fail-closed。
2. Windows 有队列/无队列启动都通过 ensure 得到唯一 Context。
3. idle Context 一次点击完成 prepare + 单次 play。
4. ACK 后 pending，Windows committed 后才成功。
5. control=48/applied=47 时显示实际旧歌曲和执行中。
6. remote 47 failed/applied 46 后恢复实际旧状态。
7. 切歌 47 failed 时，48、49 分别收到 dependency_failed settled，不通过区间猜测。
8. settled 的 requestingClientId 指向各命令原 controller。
9. settled 先持久化后发送，并按版本升序。
10. 相同 settled 重复无副作用；同键不同结果进入冲突恢复。
11. command 带 executionTimeoutMs=15000，Windows 安全失效 lease 后才发送 execution_timeout。
12. 原加载任务第 17 秒以后返回时不切歌、不 committed、不修改任何投影。
13. 服务端 17000ms 无 feedback 时发送 execution_unknown settled，不生成 playback.update/clientSeq。
14. Windows 收到 settled 后取消对应 lease 和队列项，迟到 callback 无效。
15. authority 断线/Socket 替换不重发旧命令。
16. 服务端完整重启后客户端按新 nonce 清理旧 pending 并重新水合。
17. lastApplied=48 后发送 applied=47 passive，只向 Windows 返回当前 correction。
18. localUser observed 46/canonical 47 获得 48，并 supersede pending 47。
19. Windows 无法隔离迟到执行时不发送 execution_timeout，部署保持 Core not ready。
20. 自动下一首不使用 localUser。

## 20. 完成标准

只有同时满足以下条件才算完成 r11 Flutter 改造：

- Android、Windows 和共享 parser 均拒绝 r8/r10 旧 shape；
- playback.update 只表示 Windows 实际 feedback；
- dependency_failed/execution_unknown 只由 server settled 表达；
- settled 使用 requestingClientId，不含 clientSeq/deviceSessionId/sourceClientId；
- settled 先持久化、一命令一消息、按版本发送并幂等；
- Windows 收到 settled 后确实取消本地事务和迟到执行；
- execution_timeout 只在 Windows 证明 lease 失效后发送；
- 17 秒 watchdog 只产生 execution_unknown；
- 没有 ACK 即成功、永久 pending、断线重放、区间猜测或旧状态回滚；
- Android/Windows 真机日志能对齐 controlVersion、appliedControlVersion、commandControlVersion、
  requestingClientId、terminal 状态和实际歌曲。
