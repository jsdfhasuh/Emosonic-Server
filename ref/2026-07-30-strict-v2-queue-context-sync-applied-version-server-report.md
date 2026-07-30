# Strict-v2 `queue.context.sync` 未同步推进 `appliedControlVersion` 服务端缺陷报告

> 日期：2026-07-30
> 面向：EmoSonic 服务端工程师
> 协议：strict-v2 `2.8.0`，契约修订 `2026-07-23-r18`
> 状态：已由 Windows 客户端日志复现；服务端修复和自动测试已完成，待部署及双端复验
> 原始日志：`C:\Users\jsdfhasuh\Documents\debug.log`

## 1. 结论

本次群播超时的直接原因是服务端接受 `queue.context.sync` 后，只推进了
`playbackContext.controlVersion`，没有按契约在同一原子操作中同步推进当前 authority 设备的
`lastAppliedControlVersion`。

实际结果是：

```text
playbackContext.controlVersion = 215
Windows deviceState.appliedControlVersion = 196
```

群播开始要求这两个版本相等。Windows 客户端因此一直等待本机实际状态与服务端状态一致，最终以
`local_context_update_pending` 超时，没有进入正常的 `broadcast.start` 流程。

这不是 Android 以前没有播放过音乐导致的，也不是 `queueRevision` 缺失。服务端下发的
`queue.context.sync` 已包含并推进 `queueRevision`；错误发生在 authority 设备的
`appliedControlVersion` 没有随本次已完成的队列状态变化一起推进。

## 2. 四个字段的职责

| 字段 | 含义 | 本次是否异常 |
| --- | --- | --- |
| `queueRevision` | 队列内容和当前索引的版本 | 正常从 108 推进到 109、110 |
| `controlVersion` | 服务端已接受的最新播放控制版本 | 正常从 213 推进到 214、215 |
| `appliedControlVersion` | 当前 authority 设备实际已经执行到的控制版本 | 异常地一直停在 196 |
| `version` | PlaybackContext 总体状态版本 | 正常随队列同步推进 |

`appliedControlVersion` 不属于 `queue.context.sync` 的下发字段。它保存在服务端的 authority
DevicePlaybackState 中，并通过 `playback.context.status.deviceStates` 或 `playback.update` 下发。

客户端提交 `queue.context.sync` 时也不应自行指定新的 `appliedControlVersion`。服务端接受队列同步并
分配新的 `controlVersion` 后，应在同一事务内把当前 authority 的
`lastAppliedControlVersion` 更新为该新版本。

## 3. 日志证据

本次 Context：

```text
playbackContextId = playback:4dbf9a28-6322-484b-96e1-a2e9af4a1782
authorityClientId = flutter-windows-9dcc9687-7b3e-4772-b584-a0fc716ce86c
deviceSessionId = device:flutter-windows:59d68fb7-4fac-44d1-8638-33d4b0f3e642
```

### 3.1 服务端宣告已支持 strict-v2 Broadcast

日志第 634 行：

```text
protocolVersion=2.8.0
playbackContextV2=true
supportsBroadcast=true
```

### 3.2 初始状态已经存在版本差距

日志第 755 行：

```text
queueRevision=108
controlVersion=213
deviceState.appliedControlVersion=196
state=playing
```

这证明服务端当时保存的是 canonical control 213、Windows applied 196。

历史上 197 至 213 的差距可能也包含同类问题，但仅凭当前客户端日志无法确认每个历史版本的来源。
需要服务端事务记录才能判断，不能直接断言这 17 个版本全部由 `queue.context.sync` 造成。

### 3.3 第一次队列同步推进 control，但没有推进 applied

日志第 767 行，Windows authority 提交：

```text
baseQueueRevision=108
baseControlVersion=213
queueLength=50
currentIndex=28
positionMs=116105
```

日志第 796 行，服务端接受后下发：

```text
queueRevision=109
controlVersion=214
version=215
currentIndex=28
positionMs=116105
```

按照契约，这次 `queue.context.sync` 是 authority 已经完成的实际状态变化。既然服务端把
`controlVersion` 推进到 214，就必须在同一事务内把该 authority 的
`lastAppliedControlVersion` 推进到 214。

### 3.4 第二次队列同步再次推进 control，applied 仍未推进

日志第 1205 行，Windows authority 再次提交：

```text
baseQueueRevision=109
baseControlVersion=214
currentIndex=28
positionMs=119831
```

日志第 1220 行，服务端下发：

```text
queueRevision=110
controlVersion=215
version=216
currentIndex=28
positionMs=119831
```

此时正确状态应为：

```text
controlVersion=215
authority.lastAppliedControlVersion=215
```

但日志第 1265 行和第 2010 行显示，服务端仍返回：

```text
controlVersion=215
appliedControlVersion=196
```

### 3.5 后续 passive 消息被拒绝只是结果，不是根因

队列同步完成后，Windows 使用新版本 215 发送普通实际状态。日志第 1257 行：

```text
code=conflict
message=Passive update cannot advance appliedControlVersion
currentControlVersion=215
currentQueueRevision=110
```

服务端禁止 passive 消息主动提高 applied 版本，这条保护规则本身是正确的，不应删除或放宽。

真正的问题是：服务端在更早的 `queue.context.sync` 事务中没有先把
`lastAppliedControlVersion` 更新到 215。正确实现下，后续 passive 消息携带 215 时只是等值状态更新，
不应被视为越级推进。

### 3.6 群播因此连续超时

日志第 2359、2898、3475、3981 至 3982 行显示，四次群播尝试都等待 4 秒后失败：

```text
source_settlement_timeout
reason=local_context_update_pending
Timed out waiting for the local Broadcast source state to match the server target
```

strict-v2 r18 要求群播 source 必须满足：

```text
deviceState.appliedControlVersion == playbackContext.controlVersion
```

客户端没有绕过这个检查是正确行为。使用未结算的 source 状态启动群播会把错误的歌曲、位置或播放状态
复制给其他设备。

### 3.7 localUser 路径能够推进 applied

日志第 5600 行显示，Windows 后续本地暂停后，服务端返回：

```text
origin=localUser
controlVersion=216
appliedControlVersion=216
executionStatus=committed
supersededThroughControlVersion=215
```

这说明服务端的 localUser 事务会同步推进 control 和 applied。缺失的处理主要位于
`queue.context.sync` 接受路径。

## 4. 契约依据

权威入口：

- [`specs/emosonic_strict_v2_socketio_server_contract.md`](../specs/emosonic_strict_v2_socketio_server_contract.md)
  第 1 至 7 行：文档状态为 Approved r18 authoritative contract，修订 r18，协议版本 `2.8.0`。

直接规则：

- [`phase-0-foundation/03-ack-errors-idempotency-and-cursors.md`](../specs/emosonic_strict_v2_contract/phase-0-foundation/03-ack-errors-idempotency-and-cursors.md)
  第 247 行明确规定：`queue.context.sync` 是 authority 已提交的实际状态变化；当
  `controlVersion` 前进时，该 authority 的 applied cursor 必须同步前进。
- [`phase-1-core/04-client-core-actions.md`](../specs/emosonic_strict_v2_contract/phase-1-core/04-client-core-actions.md)
  第 78 至 87 行规定 `queue.context.sync` 的 base cursor、服务端 cursor 分配和 canonical push。
- [`phase-1-core/08-server-queue-playback-and-controls.md`](../specs/emosonic_strict_v2_contract/phase-1-core/08-server-queue-playback-and-controls.md)
  第 7 至 45 行规定 canonical `queue.context.sync` 字段和 cursor 变化条件。
- [`phase-2-optional-profiles/06b-broadcast-source-context.md`](../specs/emosonic_strict_v2_contract/phase-2-optional-profiles/06b-broadcast-source-context.md)
  第 9 至 17 行规定 `broadcast.start` 必须检查
  `deviceState.appliedControlVersion == playbackContext.controlVersion`。
- [`phase-3-conformance/11c-security-persistence-and-readiness.md`](../specs/emosonic_strict_v2_contract/phase-3-conformance/11c-security-persistence-and-readiness.md)
  第 71 至 82 行规定 Core 的 control/applied 分离与 Broadcast 状态机全部符合后，部署才能协商
  `supportsBroadcast:true`。

## 5. 服务端必须修改的行为

服务端处理当前 authority 发来的合法 `queue.context.sync` 时，应在同一个 Context 串行事务中：

1. 验证 authority client/device、epoch、`baseQueueRevision` 和需要时的
   `baseControlVersion`。
2. 计算新的 queue、currentIndex、trackId、position 和各项 cursor。
3. 当本次变化使 `controlVersion` 前进到 `N` 时，同时把当前 authority 的
   `lastAppliedControlVersion` 更新为 `N`。
4. 如果当前 epoch 已有该 authority 的 DevicePlaybackState，同时更新本次队列同步能够证明的实际状态：
   track、position、positionSampledAtServerMs 和 serverUpdatedAtMs；idle/non-empty 边界按契约更新
   state，普通非空队列同步保留此前已验证的 state 和 playbackRate，不得伪造播放速度。
5. 原子持久化 Context、Queue cursor、Control cursor 和 authority DevicePlaybackState。
6. 提交成功后再发送 ACK、canonical `queue.context.sync` 和相关状态通知。
7. 对于已有 DevicePlaybackState 的 authority，随后的 `playback.context.status` 必须立即返回
   `deviceStates[].appliedControlVersion=N`。如果此前从未产生过 DevicePlaybackState，服务端应持久化
   内部 applied baseline，使首次 passive update 只能以 N 为 applied 基线；在取得合法 clientSeq 和
   完整实际状态前，不得伪造一个可见的 deviceStates 项。
8. 随后的 passive `playback.update(appliedControlVersion=N)` 必须按“等于 lastApplied”接受。

如果存在仍未结算且会被新 applied cursor 跨越的 pending control，或 failed control 的实际状态对账尚未
完成，服务端应在任何 cursor 改变前返回明确的 `conflict`，除非 r18 后续明确规定了另一种原子结算语义。
已经完成实际状态对账的历史 failed/superseded terminal 记录本身不是永久阻塞条件。不能先接受
`queue.context.sync`、推进 canonical control，再把 authority applied 留在旧版本，也不能把
`queue.context.sync` 擅自赋予只有 localUser 才明确拥有的 supersede 语义。

`queue.context.sync` 表示 authority 已完成的实际变化。服务端不能为它新建一条等待 authority 再次执行的
普通远程 pending control transaction。

## 6. 不应采用的修复方式

- 不要允许 passive `playback.update` 任意提高 `appliedControlVersion`。
- 不要删除群播开始时 `appliedControlVersion == controlVersion` 的前置检查。
- 不要在客户端收到 canonical queue push 后自行假定服务端 applied 已经推进。
- 不要把 `queueRevision` 当成 `appliedControlVersion` 使用。
- 不要无条件把所有历史数据的 `appliedControlVersion` 直接改成当前 `controlVersion`。
- 不要只延长客户端群播等待时间；当前状态不会自行收敛，延长时间只会更晚超时。

无条件修复历史数据有风险，因为真实的
`appliedControlVersion < controlVersion` 也可能表示尚未完成的远程控制。历史修复前必须检查该 Context
是否存在未结算 pending、未完成的 failed 实际状态对账和对应实际状态快照；已经完整对账的历史
failed/superseded 记录不应被误判为永久未结算。

## 7. 服务端回归测试

至少增加以下自动测试：

1. 初始 `controlVersion=10`、`lastAppliedControlVersion=10`，authority 的队列同步改变 index/position；
   事务后两者必须同时为 11。
2. 队列内容变化但 currentIndex、当前 track、position 和 idle/non-empty 边界均不变时，按契约推进
   `queueRevision/version`，不错误增加 `controlVersion/appliedControlVersion`。
3. 已存在当前 epoch authority DevicePlaybackState 时，`queue.context.sync` 使 control 前进后立即查询
   status，device state 的 applied 必须等于新 control，track/position/采样时间与同步结果一致，
   clientSeq 和既有合法 playbackRate 不得被伪造或错误推进。
4. 上一步之后发送相同版本的 passive update，必须被接受且不得再次增加任何 cursor。
5. 模拟事务持久化失败，Context、Queue cursor、control 和 applied 必须全部回滚，不能部分提交。
6. 相同 requestId、相同内容重试必须重放原结果，不能二次增加 cursor。
7. stale base cursor 必须返回契约规定的错误，且 control/applied/queue/version 均不改变。
8. 如果存在会被新 applied cursor 跨越的 pending control，或 failed control 的实际状态对账尚未完成，
   队列同步必须在 mutation 前失败，不能产生 `controlVersion > appliedControlVersion` 的半完成结果；
   已完成对账的历史 failed/superseded 记录不得被误判为阻塞。
9. 合法队列同步完成后立即 `broadcast.start`，source 前置检查应通过，不再出现
   `local_context_update_pending`。
10. 真正由远程命令形成的 pending control 仍必须允许
    `appliedControlVersion < controlVersion`，不能为了本次修复破坏 control/applied 分离。
11. 当前 epoch 尚无 DevicePlaybackState 时，队列同步不得伪造 clientSeq、playbackRate 或可见状态项；
    首次合法 passive update 使用新 applied baseline 后，status 才返回完整 device state。

## 8. 修复验收标准

用 Windows authority 重新执行相同场景：

```text
queue.context.sync before: queueRevision=108, controlVersion=213
queue.context.sync after:  queueRevision=109, controlVersion=214
status after:              appliedControlVersion=214
```

再次同步后：

```text
queue.context.sync after:  queueRevision=110, controlVersion=215
status after:              appliedControlVersion=215
passive update 215:        accepted
broadcast.start:           accepted，且不再发生 local_context_update_pending
```

需要收集：

- 服务端从两次 `queue.context.sync` 进入事务到提交完成的完整日志；
- 每次事务前后的 Context、Queue cursor、control transaction 和 authority device state；
- Windows 群播开始前 5 秒至成功播放后 10 秒的日志；
- Android 同一时段日志；
- 实际歌曲、索引、位置和播放状态。

在服务端修复、Windows 与 Android 双端日志完成复核前，真机验证状态保持
`pending user validation`。

## 9. Capability 说明

本次日志对应的服务端部署在注册响应中宣告 `supportsBroadcast=true`，但日志证明 source Context 的
control/applied 原子结算尚未符合 r18。

在修复部署并通过对应 conformance tests 前，严格做法是暂时协商：

```json
{
  "supportsBroadcast": false
}
```

这不会修复数据，只是避免旧部署对外宣告尚未完整实现的 Broadcast profile。修复部署并通过完整
readiness 检查后再恢复为 `true`。

## 10. 服务端落地结果

本仓库已经按上述边界完成修复：

- `mutateStrictPlaybackContextQueue` 在同一数据库事务中推进 Context control cursor 和 authority
  DevicePlaybackState applied cursor；
- 已有当前 epoch DevicePlaybackState 时，同步更新 track、position、采样/接收时间，保留 clientSeq、
  playbackRate 和普通非空队列的既有实际 state；
- 尚无完整 DevicePlaybackState 时写入 `clientSeq=0` 的内部 applied baseline；status 不暴露该条目，
  Broadcast source 校验也拒绝将其作为完整实际状态；
- 会被新 applied cursor 跨越的 pending control 在任何 cursor mutation 前返回 conflict；已经完成实际
  状态对账的 failed 历史不形成永久阻塞；
- queue sync 不创建远程 pending control transaction，后续等值 passive update 可以正常结算。

自动验证结果：

```text
专项回归：7 tests OK
Core/Store/Effective-at：133 tests OK
完整测试：Ran 1647 tests ... OK (skipped=3)
git diff --check：通过
py_compile：通过
```

真机验收仍应按第 8 节重新建立或清理个人测试 Context 后执行，避免旧的 213/196 历史差距干扰判断。
