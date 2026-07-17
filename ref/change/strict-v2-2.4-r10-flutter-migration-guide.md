# EmoSonic strict-v2 `2.4.0` r10 Flutter 改造说明

> 日期：2026-07-17
> 面向：Flutter Android、Flutter Windows 和共享 realtime 层工程师
> 状态：客户端改造输入，不表示服务端或 Flutter 已经实现完成
> 权威契约：`specs/emosonic_strict_v2_socketio_server_contract.md` r10

本文说明 Flutter 客户端从当前 strict-v2 `2.3.0` / contract r8 升级到 `2.4.0` / r10 时需要修改的
行为。本文是迁移指南，不替代完整 wire contract；字段冲突时以 r10 主契约为准。

## 1. 一句话结论

这不是只增加几个字段的小升级。

r10 同时改变了：

1. player 启动时如何建立 PlaybackContext；
2. 没有队列时如何表示待机状态；
3. 手机第一次播放待机设备时如何准备队列；
4. 手机命令“服务端已收到”和“Windows 已执行成功”如何区分；
5. Windows 本地人工操作如何覆盖尚未完成的手机命令；
6. 执行失败、超时、断线、重启和迟到状态如何收敛。

Flutter 必须把 r10 当成单一新 shape。不要同时保留 r8 `playback.context.create`、旧
`playback.update` 或 `player.authorityIntent` 分支。

## 2. 版本和兼容性

- 协议版本：`2.4.0`
- 契约修订：r10
- strict-v2 最低允许版本：major 必须为 `2`，minor 必须 `>=4`
- `2.3.x` 和更低版本不得进入 r10 Core
- 不提供 2.3/2.4 双 shape
- `schemaHash` 和 `serverBuildCommit` 只用于观察当前部署，不得永久固定到客户端安装包

如果 negotiated register 返回低于 `2.4.0`、其他 major、缺少固定 capability 字段，或者 Core action
返回 `not_supported` / `capability_required`，Flutter 必须关闭 strict 远控状态，不得退回
`sessionId` 方案。

## 3. 注册改动

### 3.1 capabilities 固定为 10 个字段

r8 允许 probe 使用 9 字段、negotiated reconnect 使用 10 字段。r10 删除这个双形状，probe 和正式连接
都必须发送完整 10 个 bool：

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

register ACK 必须返回相同固定字段集合的 `negotiatedCapabilities`。Flutter 严格解析器应拒绝缺字段、
未知字段、非 bool 和 `effectiveAtPlayback:true + playbackPrepare:false`。

### 3.2 probe 和正式连接

流程保持两次连接：

```text
probe auth/register
  -> 保存服务器 profile
  -> 主动断开
  -> negotiated reconnect
  -> 再次 auth/register
```

每个新物理连接使用新的 `connectionNonce`，`connectionEpoch` 为 1。注册成功后的每条入站消息都必须
匹配当前连接的 nonce/epoch，否则隔离，不写入状态也不执行音频命令。

## 4. Player 启动：create 改为 ensure

### 4.1 删除 playback.context.create

r10 不再允许客户端指定 `playbackContextId` 创建 Context。删除：

```text
playback.context.create
```

改用：

```text
playback.context.ensure
```

`playbackContextId` 由服务端生成。长期幂等身份是 authenticated user + stable clientId。

### 4.2 Windows 启动顺序

Windows 完成 negotiated register 后：

1. 读取本地可恢复队列和当前播放状态；
2. 立即发送 `playback.context.ensure`；
3. 保存 direct response 返回的 Context ID 和 cursors；
4. 在线期间保持唯一 active Context。

本地已有队列时：

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

本地确实没有队列时必须发送待机 shape：

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

空队列时禁止 `currentIndex` 和 `trackId`。非空队列时 `currentIndex` 必需，state 只能是
`playing|paused|stopped`。

首次 ensure 已经把实际快照初始化为：

```text
controlVersion = 1
appliedControlVersion = 1
```

Windows 不得再额外发送一次 localUser update，把版本错误增加到 2。

## 5. Controller 发现和控制流程

controller 选择 Windows 后必须依次执行：

```text
device.list
  -> playback.context.list(authorityClientId + authorityDeviceSessionId)
  -> playback.context.subscribe
  -> playback.context.status
```

`device.list` 只发现设备，不携带 Context binding。`playback.context.list` 返回 0 个是短暂未准备状态，
返回 1 个才能自动选择，返回多个必须进入本地 `ambiguous_playback_scope` 并停止控制。

收到匹配设备的 `playback.context.bindings.changed`、Context closed、Handoff authority 变化或
deviceSessionId 变化时：

1. 立即暂停控制；
2. 清除旧 Context 水合和 cursors；
3. 使用新 requestId 重新 list；
4. 重新 subscribe/status 后恢复 UI。

## 6. 待机 Context 和第一次播放

idle Context 是合法且必需的 Context，不等于“设备不存在”。其状态是：

```text
queueSongIds = []
state = idle
positionMs = 0
currentIndex/trackId 不存在
```

Android 在 idle Context 上点播放时不能直接发送 `player.play`。固定流程：

1. 发送 `playback.context.prepare`，携带唯一 `intentId` 和最新 `baseControlVersion`；
2. Windows 收到 server-routed prepare 后恢复本地队列，或采用请求中的初始队列；
3. Windows 使用 `queue.context.sync` 把同一个 Context 变成非空 paused 状态；
4. Windows 发送 `playback.context.prepared`；
5. Android 观察到 canonical queue 非空；
6. Android 使用最新 controlVersion 只发送一次 `player.play`。

两端都没有队列时显示 `queue_required`，不得永久 loading，也不得要求用户再次点击才能恢复。

## 7. 命令 ACK 不再表示播放成功

Android 发送 `queue.playItem` 或 `player.*` 后：

- `system.ack` 只表示服务端通过校验、分配版本并把命令送入 Windows Socket；
- ACK 后 UI 状态是“正在执行”，不是“成功”；
- 请求使用 baseControlVersion=N 且收到成功 ACK 时，本次远程命令版本是 N+1；
- 只有后续 remoteCommand committed `playback.update` 才表示 Windows 真正执行成功；
- remoteCommand failed、dependency failure、timeout、unknown 或 localUser supersede 都会结束 pending。

Flutter 建议用以下键保存等待命令：

```text
playbackContextId + epoch + controlVersion
```

不要仅用 requestId 保存长期命令状态，因为 requestId 只在当前物理连接内用于请求结算。

## 8. controlVersion 和 appliedControlVersion

必须同时保存两个值：

- `controlVersion`：服务端最新接受到哪条控制命令；
- `appliedControlVersion`：Windows 实际成功执行到哪条命令。

合法状态：

```text
controlVersion = 48
appliedControlVersion = 47
```

这表示命令 48 正在执行。此时 Windows 的实际歌曲可能还是版本 47 的歌曲，不能因为它与主 Context
最新目标不同而拒绝消息或回滚 UI。

Android 展示实际歌曲、状态和位置时以 `deviceStates` / `playback.update` 的 applied snapshot 为准；
发送新命令时仍以主 Context 最新 controlVersion 为 base。

## 9. Windows 上报的四种 playback.update

所有请求都必须带：

```text
playbackContextId
deviceSessionId
origin
state
positionMs
clientSeq
```

### 9.1 普通进度和状态

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

不推进任何 Context cursor，只更新实际状态。

### 9.2 远程命令成功

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

只有音频层返回最终 committed snapshot 后才能发送。loading、buffering、旧 track 回调或临时 pause
不能冒充成功。

### 9.3 远程命令失败

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

失败不推进 appliedControlVersion，实际歌曲和位置必须是播放器仍然停留的状态。

### 9.4 Windows 本地人工操作成功

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

Windows 不生成新 controlVersion。服务端从当前值加一，并在 confirmation 中返回：

```text
controlVersion
appliedControlVersion
supersededThroughControlVersion
```

本地 next/previous 必须转换为绝对 queueIndex/trackId。自然播完自动下一首不能使用 localUser；完整
队列变化继续使用 `queue.context.sync`。

## 10. 新的失败和恢复规则

### 10.1 切歌失败会结束后续等待命令

如果失败动作是：

```text
queue.playItem
player.next
player.prev
```

服务端把同一 Context/epoch 内所有更高版本且仍 pending 的远程命令标记为
failed/dependency_failed。

例如：

```text
47 queue.playItem -> track_load_failed
48 player.pause  -> dependency_failed
49 player.seek   -> dependency_failed
```

Windows 在 47 失败后必须丢弃本地的 48、49，不能把 pause/seek 执行到旧歌曲。服务端 failed push 中：

```text
commandControlVersion = 47
controlVersion = 49
appliedControlVersion = 46
```

Android 收到后，把 `(47, 49]` 内仍 pending 的命令结束为 dependency_failed，立即请求最新 status，并在
水合完成前禁止自动重发。

普通 seek 失败不会自动把不依赖歌曲变化的后续命令算成 dependency_failed。

### 10.2 默认 15 秒执行超时

服务端为每条远程命令设置执行期限，默认 15000ms，部署可以调整。Flutter 不通过 capability 获取该
值，也不硬编码它作为成功判断。

超时后会收到：

```text
executionStatus = failed
errorCode = execution_timeout
```

服务端同时发送最后保存的实际歌曲、状态、位置和 appliedControlVersion。Android 显示“执行超时，
已恢复实际状态”，刷新 status，不自动重发。切歌类 timeout 同样终止后续 pending。

### 10.3 断线或重启结果不明

authority 断线、连接被替换、服务端 graceful shutdown 或重启恢复时，所有尚未结束的远程命令进入：

```text
executionStatus = failed
errorCode = execution_unknown
```

含义是服务端无法证明命令到底执行没有。服务端和 Windows 都不得在重连后自动重发。

Flutter 收到 execution_unknown 或检测到 Socket 重连时：

1. 清除旧连接上的 pending command UI；
2. 重新 auth/register；
3. player 重新 ensure；
4. controller 重新 list/subscribe/status；
5. 以新 status 为准，等待用户重新操作。

推荐提示：“执行结果无法确认，请刷新后重试”。

### 10.4 Windows 发来旧状态

当 Windows 发送的 appliedControlVersion 小于服务端 lastApplied 时：

- 服务端不写入旧状态；
- 不广播给 Android 或其他 subscriber；
- 只向原 Windows Socket 返回当前保存的 passive canonical `playback.update`；
- Windows 用返回状态校正本地认知，不再次上报同一旧 snapshot；
- 相同 requestId 重试会收到同一 source-only confirmation。

这个 source-only passive update 不是新的全局状态变化。共享 Flutter realtime 层必须允许 Windows 处理，
但 Android/controller 状态层不应把它当作一次新的 Context mutation。

如果同一命令已经成功，后来又报告失败，或已经失败后来又报告成功，服务端返回 `conflict`，不能按
普通迟到状态忽略。

## 11. 固定失败原因

远程命令失败原因固定为：

```text
playback_failed
track_load_failed
seek_failed
execution_timeout
dependency_failed
execution_unknown
```

客户端逻辑依据 `errorCode`，`errorMessage` 只用于安全诊断和日志展示。

推荐 UI：

| errorCode | 用户提示 | 客户端动作 |
| --- | --- | --- |
| `track_load_failed` | 歌曲加载失败 | 显示实际旧歌曲，刷新 status |
| `seek_failed` | 跳转失败 | 恢复实际位置，允许再次操作 |
| `playback_failed` | 播放操作失败 | 显示实际状态，刷新 status |
| `execution_timeout` | 执行超时，已恢复实际状态 | 清除对应 pending，刷新 status，不自动重发 |
| `dependency_failed` | 前一条切歌失败，后续操作已取消 | 清除后续 pending，刷新 status |
| `execution_unknown` | 执行结果无法确认，请刷新后重试 | 清除全部旧 pending，重新水合，不自动重发 |

## 12. Windows 本地执行队列

Windows 必须以 `(playbackContextId, epoch, controlVersion)` 保存远程执行事务，并按 controlVersion
严格串行执行。

必须做到：

- 收到命令只进入本地 pending，不立即报告成功；
- 音频层真正完成后发送 committed；
- 音频层失败后发送 failed 和实际旧状态；
- 切歌类失败后丢弃所有更高版本未执行命令；
- localUser 操作期间暂缓未完成远程命令；
- 收到 localUser confirmation 后丢弃不高于 supersededThroughControlVersion 的旧 pending；
- nonce、epoch、authority、deviceSession 或 Socket 改变时清除旧执行队列；
- 重连后绝不恢复旧 command 执行。

## 13. Android/controller pending 状态

建议每个远程命令使用：

```text
accepted -> pending_execution -> committed | failed | superseded
```

处理规则：

- system.ack：进入 pending_execution；
- remoteCommand committed：显示成功并更新 applied state；
- remoteCommand failed：显示实际旧状态和错误；
- localUser confirmation：清除不高于 supersededThroughControlVersion 的 pending；
- dependency failure：清除失败 command 之后到当前 controlVersion 的 pending；
- execution_timeout：清除对应命令，刷新 status；
- execution_unknown 或 Socket reconnect：清除全部旧连接 pending，重新水合；
- stale_version：使用现有有界流程刷新 status，并最多重发当前用户仍然明确要求的最新操作；
- 不得自动重发 dependency_failed、execution_timeout 或 execution_unknown 命令。

## 14. 删除和禁止的旧行为

Flutter 必须删除或关闭：

- `playback.context.create`；
- 非空队列专用的旧 Context parser；
- `player.authorityIntent`；
- 无 `origin` / 无 applied cursor 的旧 `playback.update`；
- ACK 后立即显示音频执行成功；
- 重连后自动恢复或重放未确认远程命令；
- 把迟到旧状态写入全局播放 UI；
- strict `sessionId` / `sourceSessionId` fallback；
- 2.3 的 9/10 capability 双 shape。

以下行为保持不变：

- Socket.IO namespace `/emo`、event `message`；
- requestId 和 payload.action 关联；
- 顶层 connectionNonce/connectionEpoch provenance；
- `device.setVolume` 仍是设备级功能，不修改 PlaybackContext；
- Follow、Broadcast、Handoff 仍按 negotiated capability 开关；
- 自动下一首继续使用 Queue transition/`queue.context.sync`，不使用 localUser。

## 15. Flutter 实施清单

### 共享 realtime/model 层

- [ ] 最低版本改为 2.4.0，固定解析 10 个 capability。
- [ ] 删除 playback.context.create 和 player.authorityIntent action/model。
- [ ] 增加 ensure、idle Context、prepare/prepared 的严格模型。
- [ ] playback.update 改为四种闭合 shape。
- [ ] status deviceStates 要求 appliedControlVersion。
- [ ] 保存 controlVersion 和 appliedControlVersion，不用一个值代替两个含义。
- [ ] 增加六个固定远程失败原因。
- [ ] 支持迟到反馈的 source-only passive correction。

### Windows

- [ ] register 后读取本地快照并立即 ensure。
- [ ] 建立按 controlVersion 串行的远程执行队列。
- [ ] committed/failed 只使用音频层最终实际快照。
- [ ] 切歌失败丢弃全部更高 pending。
- [ ] 实现 localUser intent 和执行屏障。
- [ ] disconnect/reconnect 清理旧事务，不重放。
- [ ] 处理 source-only correction，停止重复发送旧状态。

### Android/controller

- [ ] 使用 list -> subscribe -> status 发现和水合 Context。
- [ ] idle 播放使用 prepare -> queue ready -> 单次 play。
- [ ] ACK 后显示 pending，而不是成功。
- [ ] committed/failed/superseded 正确结束命令。
- [ ] dependency_failed 清理后续 pending 并刷新。
- [ ] execution_timeout/execution_unknown 提示和恢复。
- [ ] control > applied 时显示有界执行中，不回滚实际歌曲。

## 16. 必须通过的测试

1. 固定 10 capability 的 probe 和 negotiated register 成功；2.3 profile fail-closed。
2. Windows 有队列启动，ensure 创建版本 1；无队列启动得到 idle Context。
3. Android 对 idle Context 点击一次播放，prepare 一次、play 一次，不永久 loading。
4. ACK 后保持 pending，Windows committed 后才成功。
5. controlVersion=48、appliedControlVersion=47 时显示实际旧歌曲和“正在执行”。
6. remote 47 failed/applied 46 后恢复实际旧状态，controlVersion 不回退。
7. 切歌 47 failed、48/49 pending 时，Windows 不执行 48/49，Android 清除为 dependency_failed。
8. 远程命令超过默认 15 秒得到 execution_timeout 和最后实际状态。
9. pending 时断开 Windows 或重启服务端，得到 execution_unknown，重连不自动重发。
10. lastApplied=48 后发送 applied=47 passive，Android 收不到旧状态，Windows 收到当前 source-only
    correction。
11. Windows localUser observed 46、服务端 canonical 47 时获得 48，并 supersede pending 47。
12. 同 intentId 相同内容重试不增加版本，不同内容 conflict。
13. Socket reconnect 后重新 ensure/list/subscribe/status，旧 nonce 和旧 pending 均不生效。
14. 自然播完自动下一首不使用 localUser，也不覆盖 pending remote。

## 17. 联调完成标准

只有同时满足以下条件才可认为 Flutter r10 改造完成：

- Android、Windows 和共享 strict parser 均不再接受 r8 shape；
- 每个远程命令最终进入 committed、failed 或 superseded；
- 没有 ACK 即成功、永久 pending、断线重放或旧状态回滚；
- 切歌失败不会把后续命令执行到旧歌曲；
- timeout、unknown 和 dependency failure 均有明确 UI 和恢复路径；
- Android/Windows 真机双端日志能够对齐 commandControlVersion、controlVersion、
  appliedControlVersion、实际歌曲和 terminal 状态。
