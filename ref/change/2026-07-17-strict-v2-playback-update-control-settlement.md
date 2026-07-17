# Strict-v2 `playback.update` 控制结算与本地优先计划

## 当前状态

状态：**阶段 0—1 已完成并完成服务端/Flutter 双方评审：strict-v2 `2.4.0` r10 契约、ADR-0022、
服务端更动说明和 Flutter 迁移说明已经冻结为最终实现输入。尚未修改 manifest、fixtures、服务端实现
或 Flutter 代码。**

协议版本继续使用 strict-v2 `2.4.0`：

- 不升级为 `3.0.0`；
- 不保留旧客户端兼容分支；
- 不改变 personal-lab policy；
- 不把真实服务端 `schemaHash` 或 build commit 固定进客户端；
- `test/fixtures/emo_protocol/strict_v2/manifest.json` 的 `server.strictV2Implemented` 继续保持
  `false`；
- Android/Windows 编译、安装和真机操作继续由用户完成。

本计划替代
`docs/plans/2026-07-16-strict-v2-remote-control-convergence-architecture.md` 中关于
`player.authorityIntent` 的设计，但不推翻其中已经确定的待机 Context、
`playback.context.ensure`、`playback.context.prepare`、Control/Queue cursor 分离和一次点击自动恢复
等决定。

## 一句话结论

删除 `player.authorityIntent`。远程命令执行结果、普通播放事实和 Windows 本地人工操作统一通过
`playback.update` 上报，但必须用不同 `origin` 和条件字段区分。

`controlVersion` 只能由服务端分配：

- 手机远程命令被接受时，服务端递增 `controlVersion`；
- 普通进度和远程命令执行结果不递增 `controlVersion`；
- Windows 本地人工操作成功后发送 `origin:"localUser"` 的 `playback.update`，服务端从当前
  canonical 值递增版本，并覆盖尚未 committed 的旧远程命令；
- Windows 只报告自己看到的 `observedControlVersion`，不得猜测新版本。

## 目标行为

1. 手机收到远程命令 ACK 时只认为“服务端已接受并转发”，不能认为电脑已经执行成功。
2. Windows 只有在音频操作真正完成后，才发送远程命令的 committed `playback.update`。
3. 服务端同时记录“最新接受版本”和“电脑实际执行到的版本”，允许出现：

   ```text
   controlVersion = 48
   appliedControlVersion = 47
   ```

   这表示命令 48 已被接受，但电脑只执行完成到 47。
   在这个窗口内，主 Context 可以表达版本 48 的控制目标，而 `deviceStates` / `playback.update` 表达
   Windows 在版本 47 的实际歌曲和状态；服务端不得再要求两者在 pending 期间强制相同。
4. Windows 本地人工操作优先于尚未完成的远程控制，但不能永久锁定手机。
5. 迟到的旧反馈不得覆盖更新的实际状态。
6. Windows 在连接服务端前已经播放时，不生成本地协议版本；首次 ensure 由服务端初始化版本。
7. 同一个本地操作重试不能重复增加版本或重复切歌。
8. 切歌类远程命令失败后，所有更高版本的 pending 命令停止执行，手机刷新实际状态后再发送。
9. 每条命令携带默认 15 秒 Windows 执行租约，服务端使用默认 17 秒 watchdog；只有 Windows 能证明
   租约失效时才报告 execution_timeout，无反馈时服务端只能报告 execution_unknown。
10. authority 断线或服务端重启后，结果不明的旧命令必须失败，不能自动重发。
11. 迟到旧状态不写入、不广播，但必须只向原 Windows 返回当前实际状态，让请求有界结束。
12. 服务端主动结束事务统一使用 `playback.control.settled`，不得生成或伪造 Windows playback.update。

## 不再采用的设计

本计划不再新增 `player.authorityIntent`。

原因不是本地操作不需要版本，而是本地操作完成后的实际状态本来就需要通过
`playback.update` 上报。把“本地操作登记”和“实际播放结果”拆成两个 action，会增加两套关联、ACK
和失败处理。新的方案在 `playback.update` 内明确区分事实反馈与本地控制 mutation，服务端仍然可以
安全分配版本和处理优先级。

现有 ADR-0021 在契约更新阶段必须被 supersede 或改写，不能继续同时声称
`player.authorityIntent` 是唯一方案。

## 版本字段

| 字段 | 发送方向 | 含义 |
| --- | --- | --- |
| `controlVersion` | 服务端 → 客户端 | 服务端当前最新接受的 canonical 控制版本 |
| `observedControlVersion` | Windows → 服务端 | 本地人工操作发生时，Windows 当时知道的版本；不是新版本申请值 |
| `commandControlVersion` | Windows → 服务端 | 当前正在结算的远程命令版本 |
| `appliedControlVersion` | 双向 | 当前实际播放状态已经成功执行到的版本 |
| `supersededThroughControlVersion` | 服务端 → 客户端 | 本地操作被接受时，可能被覆盖的旧远程版本上界；只有仍为 pending 的命令才转为 superseded |

固定规则：

- `controlVersion` 由服务端单写，客户端请求不得自行携带新的 canonical 值；
- `observedControlVersion <= canonical controlVersion` 的当前 authority 本地操作可以被接受；
- `observedControlVersion > canonical controlVersion` 是非法反馈；
- `appliedControlVersion > canonical controlVersion` 是协议错误；
- `appliedControlVersion < lastAppliedControlVersion` 是迟到反馈，不得覆盖当前设备状态；
- `appliedControlVersion == lastAppliedControlVersion` 可以用于新的进度、音量和状态事实；
- 服务端已经接受更新命令、但 Windows 仍在执行旧命令时，允许
  `appliedControlVersion < controlVersion`。

低于 lastApplied 的迟到 passive 或与当前 terminal 一致的旧反馈不写入、不广播。服务端使用当前
保存的 DevicePlaybackState 生成 `origin:"passive"` canonical update，只发回原 Windows Socket；相同
requestId 重试重放同一结果。已经 terminal 的事务收到不同 terminal 结果仍返回 `conflict`。

## 服务端远程命令状态

服务端为每个 `(playbackContextId, epoch, controlVersion)` 记录唯一控制事务：

```text
pending
  ├─ committed
  ├─ failed
  └─ superseded
```

规则：

- 接受手机命令并完成可靠单播后进入 `pending`；
- Windows 报告远程执行成功后进入 `committed`；
- Windows 报告执行失败后进入 `failed`；
- `queue.playItem`、`player.next` 或 `player.prev` 失败时，所有更高版本且仍为 `pending` 的事务也进入
  `failed`，`errorCode:"dependency_failed"`，服务端为每个版本分别发送 `playback.control.settled`；
- Windows 收到 command 后使用 `executionTimeoutMs`（默认 15000ms）建立执行租约；只有确认租约失效
  时才通过 remoteCommand failed 上报 `execution_timeout`；
- 服务端 accepted/routed 后启动 `executionTimeoutMs + 2000ms` watchdog（默认 17000ms）；无 terminal
  feedback 时进入 `failed`，`errorCode:"execution_unknown"`，发送 `playback.control.settled`；
- authority 断线、连接被替换或服务端重启造成结果不明时进入 `failed`，
  `errorCode:"execution_unknown"`，服务端仍在线时逐条发送 `playback.control.settled`，不得重连重发；
- Windows 本地操作获得更新版本时，所有不高于
  `supersededThroughControlVersion` 且仍为 `pending` 的远程命令进入 `superseded`；
- terminal 状态不能再次变化；
- `committed` 命令不回滚。本地操作如果后来发生，使用更新版本成为最终状态；
- 服务端保留历史状态，不把“注销”实现为删除审计记录。
- 每条 pending 记录保存 `requestingClientId`、`acceptedAtMs`、`executionTimeoutMs` 和
  `watchdogDeadlineAtMs`。

## `playback.update` 公共字段

客户端请求公共 payload：

```json
{
  "playbackContextId": "context-1",
  "deviceSessionId": "windows-session-1",
  "origin": "passive | remoteCommand | localUser",
  "state": "idle | playing | paused | stopped",
  "positionMs": 1200,
  "clientSeq": 18
}
```

公共条件规则：

- `state:"idle"` 时必须省略 `trackId`，且 `positionMs=0`；
- queue-backed Context 的非 idle 状态必须携带与 `appliedControlVersion` 对应实际快照匹配的
  `trackId`。当 `controlVersion > appliedControlVersion` 时，该 track 可以暂时不同于主 Context 的
  最新控制目标；服务端必须按控制事务和 per-device applied snapshot 校验，不能只与最新 canonical
  current item 比较；
- `volume`、`muted` 是可选实际设备状态；
- `queueIndex` 只允许需要表达绝对当前歌曲的本地操作；
- `playback.update` 不能替换 `queueSongIds`；完整队列内容改变仍使用 `queue.context.sync`；
- 服务端从 authenticated Socket 获得 `sourceClientId`，客户端不得伪造；
- 每个请求继续携带合法 `requestId`，canonical push 省略 requestId；
- 服务端 canonical push 必须发送给源 Windows 和全部合法 Context recipients。

## Shape A：普通事实与进度

Windows → 服务端：

```json
{
  "type": "event",
  "action": "playback.update",
  "requestId": "update-passive-18",
  "payload": {
    "playbackContextId": "context-1",
    "deviceSessionId": "windows-session-1",
    "origin": "passive",
    "appliedControlVersion": 47,
    "state": "playing",
    "trackId": "song-2",
    "positionMs": 5000,
    "volume": 60,
    "muted": false,
    "clientSeq": 18
  }
}
```

服务端动作：

- 不推进 Context、Queue 或 Control cursor；
- 只更新 DevicePlaybackState 和 `clientSeq`；
- 继续使用该设备最后 committed 的 `appliedControlVersion`；
- 如果服务端已有更新 pending 命令，广播中可以同时出现更新的 `controlVersion` 和较旧的
  `appliedControlVersion`。

服务端 → 客户端：

```json
{
  "type": "event",
  "action": "playback.update",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "context-1",
    "sourceClientId": "windows-1",
    "deviceSessionId": "windows-session-1",
    "origin": "passive",
    "controlVersion": 48,
    "appliedControlVersion": 47,
    "state": "playing",
    "trackId": "song-2",
    "positionMs": 5000,
    "volume": 60,
    "muted": false,
    "clientSeq": 18,
    "serverUpdatedAtMs": 1780000001200
  }
}
```

## Shape B：远程命令执行成功

服务端接受手机命令时先分配版本并记录 `pending`，然后向 Windows 发送带 canonical
`controlVersion` 的 server-routed command。

Windows 只有拿到音频层 committed snapshot 后才发送：

```json
{
  "type": "event",
  "action": "playback.update",
  "requestId": "update-remote-47",
  "payload": {
    "playbackContextId": "context-1",
    "deviceSessionId": "windows-session-1",
    "origin": "remoteCommand",
    "commandControlVersion": 47,
    "appliedControlVersion": 47,
    "executionStatus": "committed",
    "state": "playing",
    "trackId": "song-2",
    "positionMs": 0,
    "clientSeq": 19
  }
}
```

服务端动作：

- 验证版本 47 对应当前 authority 的有效 pending 命令；
- 将命令 47 改为 `committed`；
- 将该设备 `lastAppliedControlVersion` 推进到 47；
- 更新并广播实际 DevicePlaybackState；
- 不再增加 `controlVersion`。

切歌加载中的旧 track、loading、临时 pause 等回调不得发送 committed 结果。

## Shape C：远程命令执行失败

`commandControlVersion` 表示失败的命令，`appliedControlVersion` 表示播放器仍然实际停留的版本：

```json
{
  "type": "event",
  "action": "playback.update",
  "requestId": "update-remote-47-failed",
  "payload": {
    "playbackContextId": "context-1",
    "deviceSessionId": "windows-session-1",
    "origin": "remoteCommand",
    "commandControlVersion": 47,
    "appliedControlVersion": 46,
    "executionStatus": "failed",
    "errorCode": "track_load_failed",
    "errorMessage": "Unable to load requested track",
    "state": "paused",
    "trackId": "song-1",
    "positionMs": 32000,
    "clientSeq": 19
  }
}
```

服务端动作：

- 将命令 47 标记为 `failed`；
- canonical `controlVersion` 仍保持 47，不能倒退或复用；
- `lastAppliedControlVersion` 仍保持 46；
- 广播电脑实际停留的状态；如果接受命令时主 Context 已经写入预期 state/currentIndex，服务端必须
  使用一次明确的失败对账 mutation 把主 Context 恢复为实际状态。该对账可以推进 Context version，
  currentIndex 需要改回时也推进 Queue revision，但不得再次推进 Control version；
- 手机后续基于 canonical 47 发出的新命令成为 48。

稳定 `errorCode` 固定为：

```text
playback_failed
track_load_failed
seek_failed
execution_timeout
```

`dependency_failed` 和 `execution_unknown` 不是 Windows actual feedback，只通过 server-only
`playback.control.settled` 发送。

实现必须区分“accepted control target”和“applied device state”的写入时点。允许主 Context 在
命令 accepted 后先表达控制目标，但 DevicePlaybackState 必须始终表达实际状态；失败时必须通过更新的
Context version/Queue revision 完成对账，不能在同一个完整 cursor 下静默改写 snapshot。

如果失败命令是 `queue.playItem`、`player.next` 或 `player.prev`，服务端同时把所有更高版本的 pending
事务标记为 failed/dependency_failed，并按 controlVersion 递增为每个版本分别发送
playback.control.settled。Android 按具体 commandControlVersion 结束 pending，不能自行推断版本区间；
Windows 丢弃这些更高版本命令，不得把它们执行到旧歌曲上。

Windows 到达 executionTimeoutMs 且能够证明执行租约已失效时，才发送 remote failed/execution_timeout。
服务端 watchdog 到期无反馈时发送 settled/execution_unknown，不能生成 playback.update 或使用 Windows
clientSeq。

## Shape D：Windows 本地人工操作成功

Windows 本地 play/pause/seek/next/previous/选歌或系统媒体人工操作成功后发送：

```json
{
  "type": "event",
  "action": "playback.update",
  "requestId": "update-local-123",
  "payload": {
    "playbackContextId": "context-1",
    "deviceSessionId": "windows-session-1",
    "origin": "localUser",
    "intentId": "local-intent-123",
    "epoch": 1,
    "observedControlVersion": 46,
    "executionStatus": "committed",
    "queueIndex": 1,
    "trackId": "song-2",
    "state": "playing",
    "positionMs": 0,
    "clientSeq": 20
  }
}
```

规则：

- 只有当前 authority client/device 可以发送；
- 本地 next/previous 必须先解析成绝对 `queueIndex + trackId`；
- 本地操作只有真正完成后才能发送 `executionStatus:"committed"`；
- `observedControlVersion` 不是 base cursor，不要求与 canonical 严格相等；只要不大于 canonical，
  当前 authority 的有效本地操作就可以按服务端接收顺序获得新版本；
- 服务端从当前 canonical 值递增，不使用 Windows 猜测的版本；
- `intentId` 在 `(playbackContextId, epoch)` 内长期幂等；
- queueIndex 改变时递增 Context version、Queue revision 和 Control version；
- 单纯 play/pause/seek 递增 Context version 和 Control version，不递增 Queue revision；
- 完整 queue 内容改变不允许混在此 payload，仍走 `queue.context.sync`。

服务端 canonical confirmation：

```json
{
  "type": "event",
  "action": "playback.update",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "context-1",
    "sourceClientId": "windows-1",
    "deviceSessionId": "windows-session-1",
    "origin": "localUser",
    "intentId": "local-intent-123",
    "controlVersion": 48,
    "appliedControlVersion": 48,
    "supersededThroughControlVersion": 47,
    "executionStatus": "committed",
    "queueIndex": 1,
    "trackId": "song-2",
    "state": "playing",
    "positionMs": 0,
    "clientSeq": 20,
    "serverUpdatedAtMs": 1780000001200
  }
}
```

同一 `intentId`、同一内容重试必须重放原 confirmation，不得再次递增版本；相同 intentId、不同内容
返回 `conflict`。

## Server-only `playback.control.settled`

服务端结束事务但没有新的 Windows actual feedback 时发送：

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

固定规则：

- 只用于 dependency_failed 和 execution_unknown；
- requestingClientId 是最初发送该命令的 controller，禁止使用 sourceClientId；
- 不带 requestId、clientSeq、deviceSessionId 或实际 track/state/position；
- dependency cascade 原子提交后按 commandControlVersion 从小到大逐条发送；
- Flutter 去重主键是 `(playbackContextId, epoch, commandControlVersion)`；
- 相同内容重复忽略，同一主键不同 status/errorCode 记录协议冲突并刷新 status；
- 旧 settled 可以补齐旧 pending，但不能回滚更新 applied state。

## 本地操作失败

本地操作失败时不能伪造 committed localUser update，也不能注销远程命令。

计划采用以下边界：

1. Windows 在本地操作开始时建立临时执行屏障；
2. 本地音频操作失败时解除屏障；
3. Windows 使用普通实际状态反馈或本地 UI 错误完成对账；
4. 服务端不分配新 `controlVersion`，不产生 `supersededThroughControlVersion`；
5. 本 r10 不增加 `localUser + failed` wire shape；本地失败只解除屏障并使用 passive 实际状态或本地 UI
   错误恢复，不推进版本或 supersede。

## 并发处理流程

### 远程命令先被接受，本地操作随后发生

```text
服务端 canonical = 46
手机命令被接受为 47，状态 pending
Windows 用户本地操作，Windows observed = 46
服务端收到 committed localUser update
服务端从当前 canonical 47 分配本地版本 48
服务端把所有 <=47 且仍 pending 的远程命令标记为 superseded
Windows 收到 confirmation 后丢弃所有 <=47 的未完成远程事务
```

### 本地操作先到服务端

```text
服务端 canonical = 46
Windows 本地操作先被接受为 47
手机仍用 baseControlVersion=46 发送命令
服务端返回 stale_version，不执行手机旧命令
```

### 远程命令已经 committed

```text
远程 47 已经实际执行
Windows 用户随后本地操作
本地操作成为 48
远程 47 保留 committed 历史，不回滚
最终实际状态以本地 48 为准
```

### 连续远程命令

服务端可以先后接受 47、48；Windows 按序执行并分别上报。服务端允许在 48 pending 时接受
47 的 committed feedback，因为此时 `lastAppliedControlVersion` 仍低于 47。

如果本地 48 已经 committed，之后才到达远程 47 feedback，则 47 低于
`lastAppliedControlVersion=48`，必须忽略并记录协议/执行顺序异常。

如果 47 是切歌类命令且执行失败，而 48、49 仍 pending，服务端把 48、49 一并标记为
failed/dependency_failed，并依次发送版本 48、49 的 playback.control.settled。Windows 不执行它们；
Android 按具体版本清除 pending，合并重新读取一次 status 后再允许用户操作。普通 seek 失败不会自动
把不依赖歌曲变化的后续命令算成 dependency_failed。

## Windows 必须实现的本地屏障

冲突判断和版本分配由服务端负责，但服务端无法收回已经发到 Windows Socket 的消息，因此 Windows
仍必须保证旧命令不会在本地操作完成后重新执行：

1. 本地用户操作开始时，记录一个 pending local intent；
2. 暂缓尚未 committed 的新远程命令；
3. 本地音频操作成功后发送 localUser playback.update；
4. 收到匹配 intentId 的 canonical confirmation；
5. 丢弃 `controlVersion <= supersededThroughControlVersion` 的未完成远程事务；
6. 解除屏障，继续执行更高版本的新远程命令；
7. Context epoch、authority、deviceSessionId 或连接 provenance 改变时，旧本地 intent 失败，不能应用到
   新 binding。

这个屏障不是永久本地锁。手机刷新到版本 48 后发送的新命令 49 必须正常执行。

## 首次连接与重连

### 首次连接，服务端没有 Context

Windows 不生成本地协议版本。注册后先读取本地队列/状态，再通过 `playback.context.ensure` 上传实际
snapshot。服务端创建 Context，并初始化：

```text
controlVersion = 1
appliedControlVersion = 1
```

ensure 已经建立 canonical 初始状态，不再额外生成一次 localUser 版本。后续位置变化使用 passive
update，版本保持 1。

### 重连，服务端已有 Context

ensure/status 必须让 Windows 和 controller 得到：

```text
controlVersion = N
lastAppliedControlVersion = M
```

- 没有离线人工操作：Windows 使用 `origin:"passive"` 和 `appliedControlVersion=M` 上报实际状态；
- 离线期间发生本地人工切歌、seek、play 或 pause：ensure 完成后使用
  `origin:"localUser"`、`observedControlVersion=N` 上报最终 committed 状态，服务端分配 `N+1`；
- 离线期间完整 queue 内容改变：先使用最新 Queue/Control cursors 进行 `queue.context.sync`；
- 服务端持久化 pending/committed/failed/superseded 和 lastApplied，重连时不得让 Windows把 canonical N
  误报成已经实际执行到 N；
- authority 断线、连接被替换或服务端重启时，所有 pending 命令进入
  failed/execution_unknown；服务端仍在线时逐条发送 settled，完整重启时客户端按新 nonce 清理旧
  pending，服务端和 Windows 都不得自动重投；
- Windows 重连后重新 ensure 并上报实际状态，controller 清除旧 pending，重新
  list/subscribe/status 后才能继续控制。

`lastAppliedControlVersion` / `appliedControlVersion` 必须加入 status `deviceStates` 和相关严格模型，
否则控制端无法区分“命令已接受”和“电脑已执行”。

当 `controlVersion > appliedControlVersion` 时，status 中 device state 的 track/state/position 允许与主
Context 控制目标不同。控制端应显示实际 device state，并把差值解释为 pending；服务端只在对应
applied transaction、历史 applied snapshot 或失败对账能证明该实际状态时接受这种差异。

## Android 控制端行为

- 发送远程命令时继续使用最新 canonical `controlVersion` 作为 base；
- correlated ACK 只结束“服务端接受”阶段，不结束实际执行阶段；
- 命令在收到 committed/failed/superseded 结算前保持明确 pending 状态；
- `controlVersion > appliedControlVersion` 时可以显示“电脑正在执行”，但不能禁用所有后续恢复路径或
  永久显示同步中；
- 收到 localUser confirmation 后更新 canonical cursor、实际歌曲和命令结算状态；
- 使用最新版本发送的新远程命令不得因之前的本地操作被永久拒绝；
- 切歌失败时，按每条 playback.control.settled 的 commandControlVersion 精确结束
  dependency_failed，并合并刷新一次 status；
- execution_timeout 显示“执行超时，已恢复实际状态”；execution_unknown 显示“执行结果无法确认，
  请刷新后重试”；
- 迟到、重复和低 applied version 的 playback.update 不得回滚 UI。低 applied 请求的 source-only
  passive correction 只用于校正 Windows，不得被 Android 当成新的全局状态 mutation。

## 服务端原子性要求

以下操作必须在同一个 Context 控制串行区内处理：

- 接受手机 player/queue control；
- 接受 localUser playback.update 并分配新版本；
- 结算 remoteCommand playback.update；
- 生成 server-only playback.control.settled；
- 处理 executionTimeoutMs、Windows lease 和服务端 watchdog；
- 切歌类 failed 后原子终止所有更高 pending；
- authority 断线/重启时把 pending 结算为 execution_unknown；
- authority handoff、Context close、ensure rebind；
- 更新命令状态和 per-device `lastAppliedControlVersion`。

服务端必须先完成验证、状态提交和持久化，再发送 ACK、error 或 canonical push。不同 Socket 之间不
承诺到达顺序，但同一 Context 的持久化结果必须只有一个明确顺序。

## 自动切到下一首的边界

歌曲自然播放结束后的自动下一首不是 Windows 人工操作，不能直接标记为 `origin:"localUser"`，否则
它可能无条件注销用户刚发送的远程命令。

契约更新前先审计当前自动切歌路径，并在以下两种方案中选择一种：

1. 使用独立 `origin:"playerAutomatic"`，明确它是否推进 cursor、如何与 pending 远程命令排序；
2. 保持现有 Queue transition/`queue.context.sync` 路径，但补齐 applied version 和命令结算规则。

推荐先采用第 2 种，避免本轮为了本地人工优先再新增第五种 playback.update shape。自动切歌不得复用
localUser 的 supersede 权限。

## 实施阶段

### 阶段 0：审核并冻结本计划

- [x] 用户确认四种 playback.update shape。
- [x] 用户确认远程 ACK 只表示 accepted/routed，实际成功由 committed update 表示。
- [x] 用户确认服务端单写 controlVersion。
- [x] 用户确认 localUser 可以覆盖 pending remote，但不回滚 committed remote。
- [x] 用户确认切歌类命令失败后，后续 pending 全部 failed/dependency_failed，Windows 不再执行。
- [x] 用户确认 server-only `playback.control.settled`，使用 requestingClientId，不使用 sourceClientId。
- [x] 用户确认 Windows executionTimeoutMs 默认 15000ms、服务端 watchdog 固定加 2000ms。
- [x] 用户确认只有 Windows 证明 lease 失效时使用 execution_timeout；watchdog 无反馈只使用
  execution_unknown。
- [x] 用户确认 settled 按 commandControlVersion 递增发送，并按 Context/epoch/command version 幂等。
- [x] 用户确认 Windows 阻止迟到执行是启用硬超时的 readiness 门槛。
- [x] 用户确认断线/重启结果不明使用 execution_unknown，重连后绝不自动重发。
- [x] 用户确认迟到旧状态不写入、不广播，只向原 Windows 返回当前实际状态。
- [x] 确认自动下一首继续使用现有 Queue transition 路径，不获得 localUser 优先级。

### 阶段 1：只更新契约与 ADR

- [x] 从 strict-v2 `2.4.0` 契约删除 `player.authorityIntent`。
- [x] 将 playback.update 改成 passive、remoteCommand committed/failed、localUser committed 四种条件 shape。
- [x] 写入 control/applied/observed/command/superseded 字段方向和闭合 schema。
- [x] 写入 pending/committed/failed/superseded 状态机。
- [x] 写入 lastAppliedControlVersion、迟到反馈和重连规则。
- [x] 改写 playback.update/status 的 track 校验：按 applied snapshot 校验，不再无条件要求等于最新
  canonical current item。
- [x] 固定远程失败后的 Context/Queue 对账规则，禁止同 cursor 静默改写 snapshot。
- [x] 更新 status `deviceStates`、ensure/status 示例和 canonical playback.update push。
- [x] 将 ADR-0021 标记为 superseded，并新增 ADR-0022 记录统一 playback.update 方案。
- [x] 编写面向服务端工程师的 `2.4.0` r10 更动说明和实施清单。
- [x] 固定 playback.control.settled、dependency_failed、execution_timeout、execution_unknown 和
  source-only stale correction。
- [x] 编写完整 Flutter r8→r10 迁移说明。
- [x] 不修改 manifest、fixtures 或 Dart。
- [x] 完成服务端/Flutter 双方评审并冻结最终 r10 文档。

### 阶段 2：服务端实现和契约反馈

- [ ] 服务端实现单一 Context control transaction store。
- [ ] 服务端持久化命令 terminal 状态和 per-device lastAppliedControlVersion。
- [ ] 服务端实现 localUser 原子版本分配和 pending remote supersede。
- [ ] 服务端实现同 intentId 幂等重放。
- [ ] 服务端实现 committed/failed remote result 校验。
- [ ] 服务端实现 requestingClientId、executionTimeoutMs 和 watchdogDeadlineAtMs 持久化。
- [ ] server-routed control 增加 executionTimeoutMs，watchdog 固定加 2000ms。
- [ ] 服务端实现 playback.control.settled 严格 push 和按版本递增发送。
- [ ] 服务端实现切歌类失败后的逐命令 dependency_failed 结算。
- [ ] 服务端实现 watchdog/断线/重启 execution_unknown settled，删除 pending 自动重投路径。
- [ ] 服务端禁止生成 remote failed playback.update 或复用 Windows clientSeq。
- [ ] 服务端实现迟到 feedback source-only 当前状态重放和协议错误日志。
- [ ] 用户回传服务端对契约字段、错误码和持久化行为的确认。

### 阶段 3：Flutter 严格模型与消息路由

- [ ] 删除 authorityIntent 模型、发送器和未发布代码路径。
- [ ] 为 playback.update 增加严格 discriminated shape 解析。
- [ ] 区分 canonical controlVersion 与 appliedControlVersion。
- [ ] status/deviceStates 保存 applied cursor。
- [ ] 增加 playback.control.settled 严格解析和 requestingClientId。
- [ ] 按 `(playbackContextId, epoch, commandControlVersion)` 幂等保存 terminal。
- [ ] 识别 dependency_failed、execution_timeout、execution_unknown 并刷新实际状态。
- [ ] 低 applied source-only passive correction 不进入全局 UI mutation。
- [ ] 拒绝非法字段组合、未知 origin、非法 terminal transition 和版本倒退。

### 阶段 4：Windows 执行与本地屏障

- [ ] server-routed command 建立以 controlVersion 为键的执行事务。
- [ ] 使用 command.executionTimeoutMs 建立 audio execution lease，不另写客户端固定值。
- [ ] AudioPlayerService/PlaybackActor 只在 committed snapshot 后发送 remoteCommand result。
- [ ] lease 超时后能停止、取消或隔离迟到音频操作。
- [ ] 迟到 Future/callback 不切歌、不 committed、不修改 Context/Queue/系统媒体状态。
- [ ] 失败发送稳定错误码和实际旧状态，不伪造成功。
- [ ] 切歌类失败后丢弃全部更高版本未完成远程事务。
- [ ] 断线重连后不恢复或重发旧远程事务。
- [ ] 本地人工操作成功后发送 localUser update。
- [ ] 本地 next/previous 转成绝对 queueIndex/trackId。
- [ ] local intent in-flight 时暂缓未完成远程事务。
- [ ] confirmation 后按 supersededThroughControlVersion 丢弃旧事务。
- [ ] 断线、handoff、Context epoch 或 deviceSession 变化时清理旧屏障。

### 阶段 5：Android 控制结算与 UI

- [ ] 命令 ACK 后保持执行 pending，而不是立即显示成功。
- [ ] committed/failed/superseded 分别结束对应命令状态。
- [ ] dependency_failed 按每条 settled 的具体 commandControlVersion 清理，不推断版本区间。
- [ ] settled 相同主键重复忽略，不同 terminal 记录冲突并刷新 status。
- [ ] execution_timeout/execution_unknown 显示明确提示并刷新 status，不自动重发。
- [ ] controlVersion/appliedControlVersion 差值只显示有界执行状态，不永久禁用控制。
- [ ] localUser canonical update 覆盖旧 UI 状态。
- [ ] stale_version 继续按已有计划有界刷新和重发一次。

### 阶段 6：自动化检查

- [ ] 契约所有 JSON fenced block 可解析。
- [ ] 契约 Markdown fence 数量为偶数。
- [ ] 契约、模型和测试不存在 `player.authorityIntent` 残留。
- [ ] 没有 `strict-v3`、`3.0.0` 或旧客户端兼容分支。
- [ ] manifest 的 `strictV2Implemented` 保持 false。
- [ ] 定向 service/provider/model/widget tests 通过。
- [ ] 完整 `flutter test` 通过。
- [ ] `flutter analyze` 通过。
- [ ] `git diff --check` 通过。

### 阶段 7：用户真机验收

- [ ] 用户编译、安装 Android 和 Windows。
- [ ] 用户执行下列真机矩阵并保存双端日志。
- [ ] Codex 复核日志后才能关闭计划。

## 必须新增的自动化测试

### 服务端契约/conformance

- [ ] passive update 不推进任何 Context cursor。
- [ ] remote committed 将 pending 47 变为 committed，applied 推进到 47，control 不再次递增。
- [ ] remote failed 将 pending 47 变为 failed，applied 保持旧版本 46。
- [ ] 切歌 48 pending、applied 47 时允许 device state 继续报告版本 47 的旧 track。
- [ ] 远程切歌失败后，实际旧 track 通过更新的 Context version/Queue revision 收敛，但 controlVersion
  不重复递增。
- [ ] canonical 48 pending 时允许按序接受 committed 47。
- [ ] lastApplied 已为 48 时拒绝/忽略迟到 committed 47。
- [ ] lastApplied 已为 48 时收到旧 passive 47，不写入、不广播，只向源 Windows 返回当前 passive update。
- [ ] applied 版本高于 canonical 返回协议错误。
- [ ] 切歌 47 failed、48/49 pending 时，48/49 原子进入 failed/dependency_failed，并按 48、49 逐条
  settled。
- [ ] server-routed command 带 executionTimeoutMs=15000，服务端 watchdog=17000。
- [ ] Windows lease 安全失效时发送 remote failed/execution_timeout。
- [ ] 服务端 17000ms 无 terminal 时发送 settled/execution_unknown，不生成 playback.update/clientSeq。
- [ ] authority 断线/服务端重启将 pending 结算为 failed/execution_unknown，重连不重投。
- [ ] settled 相同主键重复幂等；同键不同 terminal 记录冲突且不覆盖。
- [ ] local observed 46、canonical 47 时，本地获得 48，并 supersede pending 47。
- [ ] local observed 高于 canonical 时拒绝且不推进版本。
- [ ] 相同 intentId 重试不重复递增；内容不同返回 conflict。
- [ ] committed remote 不被改写为 superseded；后续 local 使用新版本成为最终状态。
- [ ] 本地 queueIndex 改变递增 queueRevision；本地 seek 不递增 queueRevision。
- [ ] status/deviceStates 同时表达 canonical control 和 per-device applied cursor。

### Windows

- [ ] 远程 play/pause/seek/切歌只有 committed snapshot 才发送 committed update。
- [ ] 切歌 transient 旧 track 不发送远程成功结果。
- [ ] 远程失败携带 commandControlVersion 和旧 appliedControlVersion。
- [ ] 使用 command.executionTimeoutMs 建立执行 lease。
- [ ] 加载超过 15 秒后 lease 失效，第 17 秒迟到 callback 不切歌、不 committed、不修改投影。
- [ ] 切歌类失败后本地丢弃所有更高 controlVersion 远程命令。
- [ ] 断线重连不恢复旧 command 执行队列。
- [ ] 收到 source-only 当前 passive correction 后校正本地状态，不重新上报旧 snapshot。
- [ ] localUser 请求不携带客户端生成的新 controlVersion。
- [ ] 本地屏障期间远程命令 deferred，confirmation 后旧 pending 被丢弃。
- [ ] 本地失败不分配新版本、不 supersede 远程命令。
- [ ] 首次离线播放后连接，ensure 初始化版本 1，不额外生成本地版本 2。
- [ ] 重连返回 N/M 时不把 canonical N 误报成已 applied N。

### Android

- [ ] ACK 后显示执行中，committed 后才显示成功。
- [ ] failed 后显示实际旧状态并允许下一次控制。
- [ ] dependency_failed 按每条 settled 的 commandControlVersion 清理并合并请求 status，不自动重发。
- [ ] settled 按 Context/epoch/command version 幂等，相同键不同结果进入协议冲突恢复。
- [ ] execution_timeout/execution_unknown 后显示明确提示并刷新。
- [ ] superseded 后不要求用户再次点击来解除永久 loading。
- [ ] control 48/applied 47 不回滚 canonical cursor，也不误报电脑已执行 48。
- [ ] localUser 48 到达后，迟到 remote 47 不覆盖歌曲和状态。
- [ ] 使用最新 48 发送的新远程命令 49 正常执行。

## 真机验收矩阵

| 场景 | 操作 | 必须结果 |
| --- | --- | --- |
| 远程 play | Android 点一次 play | Windows 执行后发送 remoteCommand committed；手机不需第二次点击 |
| 远程 next | Android 点一次 next | Windows 只切一首；新 track committed 后才结算成功 |
| 远程失败 | 制造不可播放歌曲或可控失败 | 服务端记录 failed；手机显示实际旧状态，不假成功 |
| 切歌失败 + 后续命令 | 快速发送切歌、pause、seek，切歌失败 | 48/49 分别收到 dependency_failed settled；Windows 不执行到旧歌；手机合并刷新 |
| Windows 安全超时 | 音频加载超过 15 秒且 lease 可失效 | Windows 发送 execution_timeout；第 17 秒迟到 callback 不切歌、不 committed |
| 服务端 watchdog | 17 秒仍无 Windows terminal | 服务端发送 execution_unknown settled，不生成 playback.update/clientSeq |
| 断线结果不明 | 命令 pending 时断开 Windows 或重启服务端 | execution_unknown；旧命令不重发；重连后重新水合 |
| 迟到状态 | lastApplied=48 后发送 applied=47 passive | 其他客户端收不到旧状态；原 Windows 收到当前 passive correction |
| remote pending + local | 手机命令刚发出时 Windows 本地切歌 | 本地获得更新版本；旧 pending remote 不再晚执行 |
| remote committed + local | 手机命令已经完成后 Windows 本地 pause | 本地成为更新版本和最终状态，不回滚历史 |
| local first | Windows 本地操作后手机仍发送旧 base | 手机旧命令 stale，不覆盖本地结果 |
| 连续命令 | Android 快速 play → pause | Windows 顺序提交两个版本，最终 paused |
| applied 落后 | 服务端接受下一命令但 Windows 尚未完成 | 手机可看到 control > applied 的有界执行状态 |
| 首次离线播放 | Windows 未连服务端已播放，再连接 | ensure 创建版本 1，状态和歌曲正确 |
| 重连离线操作 | Windows 断网期间本地切歌后重连 | ensure 水合后本地最终状态获得 N+1 |
| 本地后继续远控 | 本地操作完成后 Android 刷新再操作 | 新远程命令正常执行，本地优先不形成锁 |

用户保存 Android/Windows 连续日志，覆盖每次操作前 10 秒到状态稳定后 10 秒，并记录：

- 用户实际点击；
- 服务端分配的 controlVersion；
- Windows commandControlVersion；
- Windows appliedControlVersion；
- 命令 terminal 状态；
- 实际歌曲、播放状态和位置；
- 是否一次点击成功；
- 是否出现迟到反馈或永久 pending。
- 是否出现断线后旧命令自动重放。

## 验收硬条件

- `player.authorityIntent` 不再属于最终 `2.4.0` surface。
- 客户端不能生成 canonical controlVersion。
- ACK 不等于音频执行成功。
- 每个远程命令最终只能是 committed、failed 或 superseded 之一。
- passive playback.update 不推进 controlVersion。
- remoteCommand playback.update 不重复推进 controlVersion。
- localUser committed playback.update 由服务端推进 controlVersion。
- 迟到低 applied feedback 不得覆盖高 applied 状态。
- 迟到低 applied feedback 必须只向源 Windows 返回当前状态，不得让 event-confirmed 请求悬空。
- 切歌类 failed 必须终止所有更高 pending，不能执行到旧歌曲。
- dependency_failed/execution_unknown 必须使用 playback.control.settled 和 requestingClientId，不得伪造
  Windows playback.update。
- Windows 默认 executionTimeoutMs=15000，服务端 watchdog 固定加 2000ms；authority 断线和服务重启
  不得自动重发旧命令。
- Windows 未证明迟到执行可被阻止时不得发送 execution_timeout，部署不得标为完整 r10 Core ready。
- 本地操作覆盖 pending remote，但不回滚 committed remote。
- 本地操作后使用最新版本的新远程命令必须正常执行。
- Windows 首次连接前的本地播放不需要本地猜测版本。
- 自动下一首不能冒充 localUser 获得人工优先级。
- Android/Windows 真机结果在用户完成并由 Codex 复核前只能标记为 pending user validation。
- 最终结果只表示 personal-lab client acceptance，不表示 production readiness。

## 风险与缓解

### 风险 1：把 accepted 当作 committed

缓解：服务端命令状态机和 Android UI 都必须等待 remoteCommand committed update。

### 风险 2：失败命令错误推进 applied version

缓解：失败 shape 分开 `commandControlVersion` 与 `appliedControlVersion`，失败只终止命令，不推进
lastApplied。

### 风险 3：服务端已 supersede，Windows 仍执行已送达旧命令

缓解：Windows 本地屏障和 supersededThroughControlVersion 丢弃规则属于必做，不把全部责任错误地
理解成服务端可以撤回网络消息。

### 风险 4：重连把 canonical 误当 applied

缓解：持久化并水合 per-device lastAppliedControlVersion；pending 命令一律 execution_unknown 终止，
重连后禁止自动重投。

### 风险 5：自动下一首误用本地人工优先级

缓解：本轮默认保留现有 Queue transition 路径，契约禁止自动事件使用 `origin:"localUser"`。

### 风险 6：一个 action 的条件 shape 过于宽松

缓解：按 origin + executionStatus 使用严格闭合字段表和 discriminated parser；非法字段组合直接
`bad_request`，不得静默忽略。

### 风险 7：最新控制目标与实际歌曲暂时不同

缓解：status/deviceStates 增加 applied cursor；playback.update 按 applied transaction 校验 track，
不再只比较最新 canonical current item。远程失败必须使用新的 Context version/Queue revision 对账，
不得在同 cursor 下改写状态。

### 风险 8：切歌失败后继续执行后续命令

缓解：服务端原子把所有更高 pending 标记为 dependency_failed；Windows 本地执行队列同步丢弃这些
命令，服务端按版本逐条发送 settled，Android 精确结束 pending 并合并刷新 status。

### 风险 9：迟到反馈被静默丢弃导致 Windows 永久等待

缓解：服务端不接受旧状态，但只向源 Windows 返回当前 passive canonical update，并缓存该
event-confirmed 结果供相同 requestId 重放。

### 风险 10：命令或重启恢复形成永久 pending

缓解：Windows 使用默认 15000ms lease，服务端使用默认 17000ms watchdog；只有 Windows 能证明 lease
失效时使用 execution_timeout，无反馈、断线和重启统一 settled/execution_unknown，所有结果不明的旧
命令进入 terminal failed 且禁止重投。

### 风险 11：服务端冒充 Windows 实际反馈

缓解：dependency_failed 和 execution_unknown 只能使用 playback.control.settled；事件禁止 clientSeq、
deviceSessionId 和 sourceClientId，并使用 requestingClientId 表示原控制端。

### 风险 12：超时后迟到音频任务仍然切歌

缓解：Windows 必须通过自动化测试证明 audio execution lease 可取消或隔离。证明前不得发送
execution_timeout，服务端 watchdog 只能 unknown，部署保持 r10 Core not ready。

## 不做的事

- 不在本计划中升级协议主版本。
- 不保留 `player.authorityIntent` 与 playback.update 两套本地控制通道。
- 不让 progress update 自动增加版本。
- 不允许 Windows 使用本地时间或本地计数猜测 canonical 版本。
- 不用 ACK 或乐观 UI 假装音频已经执行成功。
- 不由 Codex 构建或安装 Android/Windows。
- 不在用户服务端确认契约前继续修改 Flutter 业务实现。
