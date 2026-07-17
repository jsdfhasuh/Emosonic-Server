# Strict-v2 `2.4.0` 服务端更动说明：`playback.update` 控制结算

> 日期：2026-07-17
> 面向：EmoSonic 服务端工程师
> 状态：待服务端评审和实现，不表示当前服务端已经完成
> 完整规范：`specs/emosonic_strict_v2_socketio_server_contract.md` r10

## 1. 本次为什么要改

旧设计只能知道服务端是否接受了手机命令，不能准确知道 Windows 是否真正执行完成，也不能安全处理
“手机命令刚发出时，用户又在 Windows 本地切歌”的冲突。

本次把以下四件事分开：

1. 服务端接受并转发远程命令；
2. Windows 真正执行成功；
3. Windows 执行失败；
4. Windows 本地人工操作成功并覆盖旧的未完成远程命令。

## 2. 最终决定

- 协议版本仍为 strict-v2 `2.4.0`；这是未发布 personal-lab 单一 shape，不保留旧客户端分支。
- 删除未发布的 `player.authorityIntent`。
- 所有实际播放结果统一使用 `playback.update`。
- `playback.update` 根据 `origin` 和 `executionStatus` 分成四种严格 shape。
- 只有服务端可以分配 canonical `controlVersion`。
- 手机命令 ACK 只表示 accepted/routed，不表示 Windows 已经执行成功。
- 服务端必须持久化远程控制事务状态和每台 authority 的 applied cursor。
- 切歌类命令失败时，所有更高版本且仍 pending 的命令一并 failed，不能继续执行到旧歌曲。
- 每个远程事务默认 15 秒执行期限，超时明确 failed；部署可以调整期限。
- authority 断线或服务端重启造成结果不明时 failed，禁止重连后自动重发旧命令。
- 迟到旧状态不写入、不广播，只向发送它的 Windows 返回当前实际状态。

## 3. 必须新增的数据

### 3.1 Control transaction

每次接受 `queue.playItem` 或 `player.*` 后创建：

```text
key = playbackContextId + epoch + controlVersion

status = pending | committed | failed | superseded
sourceClientId
authorityClientId
authorityDeviceSessionId
accepted target/action
acceptedAtMs
deadlineAtMs
terminalAtMs（terminal 后）
errorCode（failed 时）
```

状态只能：

```text
pending -> committed
pending -> failed
pending -> superseded
```

terminal 状态不能互相转换。

### 3.2 Authority applied state

每个 Context 当前 authority device 必须保存：

```text
lastAppliedControlVersion
state
trackId
positionMs
volume/muted（可选）
clientSeq
serverUpdatedAtMs
```

`lastAppliedControlVersion` 必须持久化，不能只存在 Socket 内存中。

### 3.3 Local intent dedupe

本地人工操作使用：

```text
playbackContextId + epoch + intentId
```

相同 ID、相同内容重试重放原结果；相同 ID、不同内容返回 `conflict`。

## 4. 四个版本字段

| 字段 | 含义 |
| --- | --- |
| `controlVersion` | 服务端当前最新接受的控制版本 |
| `observedControlVersion` | Windows 本地人工操作时看到的版本，不是新版本 |
| `commandControlVersion` | Windows 正在结算的远程命令版本 |
| `appliedControlVersion` | 当前实际播放状态已经执行到的版本 |

正常情况下允许：

```text
controlVersion = 48
appliedControlVersion = 47
```

含义是服务端已经接受命令 48，但 Windows 只执行完成到 47。

## 5. 服务端接受远程命令

服务端当前版本 46，手机发送命令并通过 base cursor 校验后：

1. 分配 `controlVersion=47`；
2. 创建 `status=pending` 的 control transaction；
3. 向当前 authority Socket 单播版本 47；
4. 给手机 correlated ACK；
5. ACK 不能被记录为 committed。

Windows 收到的命令继续使用现有 server-routed shape：

```json
{
  "type": "command",
  "action": "player.seek",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "context-1",
    "controlVersion": 47,
    "sourceClientId": "android-1",
    "positionMs": 42000
  }
}
```

## 6. Windows → 服务端的四种 `playback.update`

### 6.1 Passive：普通进度和状态

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
    "clientSeq": 18
  }
}
```

服务端：

- `appliedControlVersion` 必须等于当前 lastApplied；
- 可以更新位置、状态、音量；
- 不推进任何 Context cursor；
- 不改变 control transaction 状态。

### 6.2 Remote committed：远程命令执行成功

```json
{
  "type": "event",
  "action": "playback.update",
  "requestId": "update-remote-47",
  "payload": {
    "playbackContextId": "context-1",
    "deviceSessionId": "windows-session-1",
    "origin": "remoteCommand",
    "executionStatus": "committed",
    "commandControlVersion": 47,
    "appliedControlVersion": 47,
    "state": "playing",
    "trackId": "song-2",
    "positionMs": 0,
    "clientSeq": 19
  }
}
```

服务端：

- 版本 47 必须是该 authority 的 pending transaction；
- 将 47 改为 committed；
- 将 lastApplied 推进到 47；
- 不再增加 controlVersion。

### 6.3 Remote failed：远程命令执行失败

```json
{
  "type": "event",
  "action": "playback.update",
  "requestId": "update-remote-47-failed",
  "payload": {
    "playbackContextId": "context-1",
    "deviceSessionId": "windows-session-1",
    "origin": "remoteCommand",
    "executionStatus": "failed",
    "commandControlVersion": 47,
    "appliedControlVersion": 46,
    "errorCode": "track_load_failed",
    "errorMessage": "Unable to load requested track",
    "state": "paused",
    "trackId": "song-1",
    "positionMs": 32000,
    "clientSeq": 19
  }
}
```

服务端：

- 将 47 改为 failed；
- lastApplied 保持 46；
- controlVersion 保持 47，不能回退或复用；
- 如果接受命令时主 Context 已经变成预期歌曲/状态，使用新的 Context version 和必要的 Queue revision
  对账回实际状态；不能再次增加 controlVersion，也不能在相同 cursor 下静默改内容。

固定错误码：

```text
playback_failed
track_load_failed
seek_failed
execution_timeout
dependency_failed
execution_unknown
```

### 6.3.1 切歌失败后的后续命令

如果失败的命令是 `queue.playItem`、`player.next` 或 `player.prev`，服务端必须在同一个 Context 串行
事务中把所有更高 controlVersion 且仍为 pending 的远程事务标记为：

```text
status = failed
errorCode = dependency_failed
```

服务端不回退 controlVersion。原 failed canonical update 的 `controlVersion` 使用当前最高 canonical
版本，因此手机可以把 `(commandControlVersion, controlVersion]` 内仍在等待的命令全部结束为
dependency_failed，然后重新请求 status。

Windows 必须按版本顺序执行。切歌类命令失败后，立即丢弃本地队列中的所有更高版本远程命令，不能把
后续 pause、seek、play 或切歌执行到旧歌曲。

### 6.3.2 服务端执行超时

每个 pending transaction 必须保存 `acceptedAtMs` 和 `deadlineAtMs`。默认期限是 15000ms，部署可以
调整，但不增加 capability 或 wire 字段。

到期仍没有 committed/failed/localUser supersede 时：

1. 服务端把该事务标记为 failed；
2. `errorCode="execution_timeout"`；
3. controlVersion 不增加也不回退；
4. appliedControlVersion 保持 lastApplied；
5. 使用最后持久化的实际歌曲、状态、位置和 clientSeq 广播 canonical failed update；
6. 如果超时的是切歌类命令，同时执行 dependency_failed 规则。

### 6.4 Local committed：Windows 本地人工操作成功

```json
{
  "type": "event",
  "action": "playback.update",
  "requestId": "update-local-123",
  "payload": {
    "playbackContextId": "context-1",
    "deviceSessionId": "windows-session-1",
    "origin": "localUser",
    "executionStatus": "committed",
    "intentId": "local-intent-123",
    "epoch": 1,
    "observedControlVersion": 46,
    "queueIndex": 1,
    "trackId": "song-2",
    "state": "playing",
    "positionMs": 0,
    "clientSeq": 20
  }
}
```

服务端在同一 Context 串行区：

1. 验证当前 authority client/device、epoch、queueIndex/trackId 和 intentId；
2. `observedControlVersion <= canonical` 可以接受；大于 canonical 返回 `bad_request`；
3. 保存接受前的 canonical 为 `supersededThroughControlVersion`；
4. 从当前 canonical 加一生成新 controlVersion；
5. 更新 Context 实际 state/position/currentIndex；
6. currentIndex 改变时增加 queueRevision；
7. 把旧的 pending remote transaction 标记为 superseded；
8. 把 authority lastApplied 推进到新 controlVersion；
9. 持久化后广播 canonical localUser confirmation。

本地 next/previous 必须先转换成绝对 queueIndex/trackId。完整 queueSongIds 改变仍使用
`queue.context.sync`。自然播放结束后的自动下一首不能使用 `origin:"localUser"`。

## 7. 服务端 → 客户端 canonical update

服务端 push 必须同时带 canonical 和 applied：

```json
{
  "type": "event",
  "action": "playback.update",
  "connectionNonce": "<recipient nonce>",
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
    "clientSeq": 21,
    "serverUpdatedAtMs": 1780000001200
  }
}
```

localUser confirmation 额外包含：

```json
{
  "origin": "localUser",
  "intentId": "local-intent-123",
  "executionStatus": "committed",
  "controlVersion": 48,
  "appliedControlVersion": 48,
  "supersededThroughControlVersion": 47,
  "queueIndex": 1,
  "trackId": "song-2",
  "state": "playing",
  "positionMs": 0
}
```

canonical push 不带 requestId。服务端使用相同 intentId 让 Windows 关联本地操作确认。

## 8. Track 校验必须改变

旧规则“playback.update.trackId 必须等于主 Context 最新 current item”不再正确。

例如：

```text
主 Context 控制目标：版本 48 / song-3
Windows 实际执行：版本 47 / song-2
```

Windows 合法上报 song-2。服务端必须按 `appliedControlVersion=47` 对应 transaction 或 applied snapshot
校验，而不是拿 song-2 与版本 48 的 song-3 比较。

status `deviceStates` 必须新增必需字段：

```json
{
  "clientId": "windows-1",
  "deviceSessionId": "windows-session-1",
  "appliedControlVersion": 47,
  "state": "playing",
  "trackId": "song-2",
  "positionMs": 5000,
  "clientSeq": 21,
  "serverUpdatedAtMs": 1780000001200
}
```

## 9. 迟到和乱序规则

以服务端保存的 lastApplied 为准：

- `applied < lastApplied`：迟到反馈，不改变状态；记录日志。
- `applied == lastApplied`：允许 passive 进度/音量、匹配 pending command 的 failed 结果或同内容 terminal
  幂等重放。
- `applied > lastApplied`：必须能按序匹配 pending remote committed 或本次 localUser 分配结果；不得跨过
  仍为 pending 的更低版本。
- `applied > canonical controlVersion`：`bad_request`。
- remote transaction 已 terminal 后收到不同 terminal 结果：`conflict`。

服务端 canonical 已为 48、lastApplied 为 46 时，可以先接受 committed 47，再接受 committed 48。

如果 localUser 48 已经 committed，之后收到 remote 47，remote 47 不能覆盖任何实际状态。

低于 lastApplied 的迟到 passive 或与当前 terminal 一致的旧反馈不能静默丢弃。服务端必须使用当前
保存的 DevicePlaybackState 构造 `origin:"passive"` canonical update，只发回原 Windows Socket：

- 不写数据库；
- 不推进 clientSeq；
- 不向其他 Context recipient 广播；
- 相同 requestId 重试时重放同一 source-only confirmation。

同一 remote transaction 已经 terminal 后收到不同 terminal 结果，仍返回 `conflict`。

## 10. 本地优先的完整竞态

```text
服务端 canonical = 46
手机命令被接受为 47 / pending
Windows 用户本地切歌，Windows 当时 observed = 46
Windows 完成本地切歌并发送 localUser update
服务端从当前 47 分配 48
服务端把 pending 47 标记为 superseded
服务端广播 localUser 48 / applied 48 / supersededThrough 47
Windows 丢弃本地尚未完成的远程 47
手机刷新到 48 后发送的新命令 49 仍然有效
```

服务端负责版本和 supersede 决定；Windows 仍需要短暂执行屏障，因为服务端不能撤回已经发送到
Windows 的 Socket 消息。

## 11. 首次连接和重连

### 首次没有服务端 Context

Windows 已经本地播放时，先通过 `playback.context.ensure` 上传队列、index、state 和 position。
服务端初始化：

```text
controlVersion = 1
appliedControlVersion = 1
```

不要要求 Windows 再发送 localUser 版本 2。

### 已有 Context 的重连

服务端必须持久化并通过 status 恢复：

```text
canonical controlVersion = N
device appliedControlVersion = M
```

不能因为 canonical 是 N 就假设 Windows 已经执行到 N。authority 断线、连接被替换、服务端 graceful
shutdown 或重启恢复时，所有 pending remote 必须进入：

```text
status = failed
errorCode = execution_unknown
```

服务端和 Windows 均不得在重连后自动重发这些旧命令。Windows 重新 ensure 并上报实际状态；手机清除
旧 pending，重新 list/subscribe/status 后才能继续控制。

## 12. 幂等与保留时间

- connection requestId cache：继续至少 60 秒，断线清理。
- local intent dedupe：至少保留 10 分钟；同一 active Context 建议保留最近 512 条 terminal 记录。
- control transaction：每个 active Context 建议保留最近 512 条 terminal；pending 不能被裁剪。
- terminal 历史可以压缩，但必须保留足以拒绝旧 feedback 和重复 intent 的 cursor/tombstone。

## 13. 服务端实施清单

- [ ] 删除 `player.authorityIntent` handler/schema/allowlist。
- [ ] 增加四种 playback.update 严格验证。
- [ ] 增加 pending/committed/failed/superseded control transaction store。
- [ ] 增加 per-device lastAppliedControlVersion 持久化。
- [ ] 每个 pending transaction 保存 acceptedAtMs/deadlineAtMs，默认期限 15000ms。
- [ ] status deviceStates 输出 appliedControlVersion。
- [ ] playback.update canonical push 同时输出 controlVersion/appliedControlVersion。
- [ ] ACK 不再被业务逻辑当作实际成功。
- [ ] localUser 原子分配新版本并 supersede pending remote。
- [ ] 远程失败完成 Context/Queue 对账，但不增加 controlVersion。
- [ ] 切歌类失败原子终止所有更高 pending，并记录 dependency_failed。
- [ ] execution_timeout 使用最后 actual snapshot 产生 canonical failure。
- [ ] authority 断线、连接替换和重启恢复使用 execution_unknown，绝不自动重投。
- [ ] trackId 按 applied transaction/snapshot 校验。
- [ ] local intentId 长期幂等。
- [ ] 迟到反馈不写入/不广播，只向源 Windows 重放当前 passive canonical update。
- [ ] 非法高版本和 terminal 冲突日志。
- [ ] 单 worker 原子串行要求继续保持。

## 14. 服务端必须通过的测试

1. passive applied 47、canonical 48：接受并广播，不推进 cursor。
2. pending 47 + committed 47：变 committed，lastApplied=47，control 不增加。
3. pending 47 + failed 47/applied 46：变 failed，lastApplied 保持 46。
4. accepted 48 但 applied 47：允许实际旧 track。
5. lastApplied 48 后到达 remote 47：不覆盖。
6. local observed 46、canonical 47：本地得到 48，pending 47 superseded。
7. local observed 大于 canonical：bad_request，无副作用。
8. remote 47 已 committed，再 local：local 得到 48，47 历史不回滚。
9. local 先得到 47，手机 base 46：stale_version。
10. 同 intentId 相同内容重试：仍返回原版本；不同内容：conflict。
11. remote failed 后主 Context/Queue 使用更新 version/revision 对账，control 保持已分配值。
12. 首次 ensure 创建版本 1，不产生额外 local 版本。
13. 切歌 47 failed、48/49 pending：48/49 进入 failed/dependency_failed，Windows 不执行，手机刷新。
14. pending 超过默认 15000ms：execution_timeout，广播最后 actual，不推进 applied/control。
15. authority 断线或服务端重启：pending 进入 failed/execution_unknown，不自动重发。
16. applied 已为 48 时收到旧 passive 47：不写入、不广播，只向源 Windows 返回当前 passive update。
17. 重启恢复 N/M，不把 N 误报为 applied。
18. 自动下一首不能使用 localUser supersede。

## 15. 与旧 r9 草案的差异

| r9 草案 | r10 最终方案 |
| --- | --- |
| 本地操作使用 `player.authorityIntent` | 删除该 action，使用 localUser playback.update |
| playback.update 只是无版本 feedback | 增加 origin、control/applied 和远程结算 |
| ACK 后缺少明确执行 terminal | 增加 pending/committed/failed/superseded |
| track 必须总等于主 Context 当前项 | pending 时按 applied transaction/snapshot 校验 |
| status deviceStates 没有 applied cursor | appliedControlVersion 成为必需字段 |

## 16. 评审时需要服务端工程师确认

1. 当前服务端在哪个原子存储中保存 Context、Control transaction 和 lastApplied。
2. 远程命令 accepted 时是否已经修改主 Context state/currentIndex；如果是，failed 对账必须按本文实现。
3. transaction/intent 的 512 条和 10 分钟默认保留是否满足现有存储条件。
4. 将 15000ms 默认执行期限接入现有服务端配置体系；具体配置键按项目现有命名方式确定，但不得关闭
   超时结算。
5. 服务端能够确认存储落点和保留能力后，再开始 Flutter 客户端实现。

本文是方便服务端评审的变更摘要。字段冲突或遗漏时，始终以
`specs/emosonic_strict_v2_socketio_server_contract.md` r10 为准；服务端不应只实现本文摘要而跳过完整
契约的权限、provenance、幂等、资源限制和 fail-closed 要求。
