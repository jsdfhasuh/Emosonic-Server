# Strict-v2 `2.4.0` r10 客户端对服务端迁移建议的回复

> 日期：2026-07-17
> 面向：EmoSonic 服务端工程师
> 状态：评审记录；决定已被 strict-v2 `2.4.0` r11 权威契约采纳
> 服务端反馈输入：`strict-v2-2.4-r10-flutter-migration-guide.md`

本文件保留双方评审过程，不替代 r11 主契约或 r11 Flutter 迁移指南。

## 1. 总体结论

服务端迁移指南第 1—9、12—17 节的大部分方向与当前 r10 一致，可以作为 Flutter 改造基础。

特别认可以下补充：

- 切歌类命令失败后，不能继续把后续 pause/seek 执行到旧歌曲；
- 断线或重启后，不应自动重放结果不明的旧远程命令；
- Windows 上报低于服务端 lastApplied 的旧状态时，应只向 Windows 返回当前 canonical correction，
  不能广播旧状态给 Android；
- timeout、dependency failure 和 unknown 都必须有明确终态，不能永久 pending。

但是这些补充目前不能直接塞进现有 `playback.update`，因为 timeout、dependency failure 和 unknown
可能由服务端产生，不是 Windows 的实际 feedback。当前 playback.update 必须来自 authority，并带
Windows 的 clientSeq；服务端不能伪造 Windows clientSeq 或假装知道电脑已经执行/未执行。

建议在保留四种 Windows playback.update shape 的基础上，增加一个仅由服务端推送的控制结算事件：

```text
playback.control.settled
```

## 2. 继续保留的 r10 边界

以下规则不变：

1. `playback.update(origin:"passive")` 只报告实际进度/状态。
2. `playback.update(origin:"remoteCommand", committed)` 由 Windows 确认远程命令真正执行成功。
3. `playback.update(origin:"remoteCommand", failed)` 由 Windows 确认命令失败且没有应用。
4. `playback.update(origin:"localUser", committed)` 由 Windows 报告本地人工操作最终结果，服务端分配
   新 controlVersion。
5. playback.update 的所有 client request 都必须来自当前 authority，并使用 Windows clientSeq。
6. `controlVersion` 只由服务端分配，`appliedControlVersion` 表示 Windows 实际执行进度。

服务端自行判定的 dependency failure、disconnect unknown 和 watchdog unknown 不属于 Windows
playback.update。

## 3. 建议新增 `playback.control.settled`

### 3.1 用途

该事件只用于服务端能够结束控制事务、但没有新的 Windows 实际 feedback 的情况：

- 前序切歌失败导致的 `dependency_failed`；
- authority 断线、Socket 被替换或服务端重启导致的 `execution_unknown`；
- 服务端 watchdog 到期但无法证明 Windows 是否执行完成的 `execution_unknown`；
- 后续如有其他服务端主动取消原因，也必须先进入固定 errorCode 清单。

Windows 自己确认并成功中止执行超时时，仍发送 remoteCommand failed playback.update，错误码为
`execution_timeout`。

### 3.2 建议 push shape

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

字段规则建议：

| 字段 | 规则 |
| --- | --- |
| `playbackContextId` | 必需，事务所属 Context |
| `epoch` | 必需，防止旧 generation 结算污染新 authority |
| `commandControlVersion` | 必需，被结束的具体远程命令版本 |
| `status` | 当前只允许 `failed`；未来增加其他状态必须更新契约 |
| `errorCode` | `dependency_failed` 或 `execution_unknown` |
| `dependsOnControlVersion` | 仅 dependency_failed 必需，指出失败的前序切歌命令 |
| `controlVersion` | 服务端当前 canonical control cursor |
| `appliedControlVersion` | authority 最后已确认的实际 applied cursor |
| `requestingClientId` | 原远程命令请求者，便于各 controller 匹配自己的 pending；禁止使用 sourceClientId |
| `errorMessage` | 可选安全诊断字符串 |
| `serverUpdatedAtMs` | 必需 |

该事件：

- 不带 clientSeq，因为它不是 Windows feedback；
- 不带 deviceSessionId 冒充设备消息；
- 不修改 DevicePlaybackState；
- 只结束 control transaction；
- 使用 recipient 当前 nonce/epoch，无 requestId；
- 发给当前 Context recipients；断开的客户端在重连时直接清理旧连接 pending，不要求补发历史事件。

## 4. 切歌失败的依赖取消

服务端工程师提出的这个问题成立：

```text
47 queue.playItem / next / prev
48 pause
49 seek
```

如果 47 加载新歌曲失败，48、49 不能继续作用到旧歌曲。

建议固定规则：

1. 根命令必须是 `queue.playItem`、`player.next` 或 `player.prev`；
2. Windows 发送版本 47 remoteCommand failed playback.update；
3. 服务端在同一事务中将所有高于 47、同 Context/epoch、仍为 pending 的远程命令标记为 failed，
   errorCode=`dependency_failed`；
4. Windows 本地执行队列同时丢弃这些未执行命令；
5. 服务端为每个受影响版本分别发送一个 playback.control.settled，而不是让 Android 仅凭
   `controlVersion=49` 猜测整个 `(47,49]` 区间；
6. 主 Context/Queue 按版本 47 的 failed 实际状态完成对账，controlVersion 保持最新已分配值；
7. controller 收到所有结算后请求一次最新 status，不自动重发失败链。

示例：

```text
playback.update(remoteCommand failed, command=47, error=track_load_failed)
playback.control.settled(command=48, error=dependency_failed, dependsOn=47)
playback.control.settled(command=49, error=dependency_failed, dependsOn=47)
```

普通 seek/play/pause 自身失败默认只结束自己，不自动取消后续命令。若服务端以后要增加更细的依赖图，
应另立契约，不在客户端根据 action 文本自行猜测。

## 5. 超时建议

### 5.1 不能只由服务端单方面设置 15 秒

如果服务端第 15 秒把命令标记为 failed，但 Windows 的音频加载仍在继续，第 17 秒仍可能真正切歌。
这会造成服务端认为失败、电脑却已经执行成功。

因此超时必须同时约束 Windows 执行事务。

### 5.2 server-routed command 增加执行期限

建议所有 `queue.playItem` 和 `player.*` server-routed command 增加：

```json
{
  "executionTimeoutMs": 15000
}
```

规则：

- 服务端默认 15000ms，部署可调；
- Windows 不在代码中另写一个不同固定值，只使用命令字段；
- Windows 从收到命令时启动期限；
- 到期时 Windows 使该 controlVersion 的 audio execution lease 失效，停止/隔离仍在运行的音频操作；
- 失效事务的迟到 callback 不得改变 Context、发送 committed 或恢复音频执行；
- Windows 能确认命令未应用时发送：

  ```text
  playback.update(
    origin=remoteCommand,
    executionStatus=failed,
    errorCode=execution_timeout
  )
  ```

- 服务端 watchdog 建议比 executionTimeoutMs 多一个小的传输宽限，例如 2000ms；
- watchdog 到期仍没有 Windows terminal feedback 时，服务端只能标记 `execution_unknown`，不能假装
  Windows 已确认 `execution_timeout`。

如果当前 Windows 音频后端无法可靠停止或隔离迟到执行，应先解决 execution lease，再启用服务端
硬超时；否则宁可报告 unknown，也不要伪造 failed。

## 6. 断线、Socket 替换和服务端重启

建议采纳服务端工程师的“不自动重放”方案，并替代当前 r10 中“可以重投一次”的描述。

固定流程：

1. authority 断线或 deviceSession/Socket 被替换；
2. 服务端把该 authority 所有 pending remote transaction 标记为 failed/execution_unknown；
3. 如果 controller Socket 仍在线，为每个事务发送 playback.control.settled；
4. Windows 重连后重新 register -> ensure -> status，不恢复旧 command 执行队列；
5. Android/controller 发生任何物理 Socket 重连时，无条件清理旧 nonce 下的 pending UI，再重新
   list/subscribe/status；
6. 服务端完整重启时不要求补发旧 settlement push，因为所有客户端物理连接都会变化，客户端必须按
   nonce 变化清理旧 pending；服务端仍持久化 transaction terminal 供审计和拒绝迟到 feedback。

这样可以避免 next/previous、切歌和 seek 在结果未知时重复执行。

## 7. 迟到旧 feedback 的 source-only correction

建议采纳，但不增加新的字段或全局广播。

当 Windows 请求：

```text
appliedControlVersion < server.lastAppliedControlVersion
```

服务端：

1. 不写入旧状态；
2. 不广播给 Android/controller；
3. 只向请求 Windows Socket 重放当前保存的 canonical passive playback.update；
4. canonical correction 使用服务端当前保存的 controlVersion、appliedControlVersion、实际 state、track、
   position 和 clientSeq；
5. 相同 requestId 重试重放同一个 source-only confirmation；
6. Windows 看到服务端 applied 高于自己发送的值后，更新本地认知并停止重复发送旧 snapshot。

source-only correction 是已有实际 DevicePlaybackState 的重放，不是新的设备 mutation，因此不需要新
clientSeq，也不能触发 Android UI 更新。

如果同一 command 已 committed 后又报告 failed，或 failed 后又报告 committed，属于 terminal 冲突，
返回 correlated `system.error(code:"conflict")`，不能按普通迟到状态处理。

## 8. 建议错误码分层

### Windows 确认的实际执行失败

通过 remoteCommand failed playback.update：

```text
playback_failed
track_load_failed
seek_failed
execution_timeout
```

### 服务端主动结束但没有新的 Windows feedback

通过 playback.control.settled：

```text
dependency_failed
execution_unknown
```

这一区分很重要：

- `execution_timeout` 表示 Windows 已确认期限到达且执行租约失效，没有应用成功；
- `execution_unknown` 表示服务端无法证明最终是否执行，客户端只能重新水合，不能自动重发。

## 9. Android/controller 建议状态机

```text
accepted
  -> pending_execution
      -> committed               # Windows committed playback.update
      -> failed                  # Windows failed playback.update
      -> dependency_failed       # server playback.control.settled
      -> execution_unknown       # server playback.control.settled / socket reconnect
      -> superseded              # localUser confirmation
```

处理规则：

- ACK 只进入 pending_execution；
- committed 后显示实际新状态；
- failed/dependency_failed 后刷新 status，不自动重发；
- execution_unknown 清理旧 pending、重新水合并等待用户重新操作；
- localUser confirmation 继续使用 supersededThroughControlVersion 清理旧 pending；
- controlVersion 用于发送新命令，appliedControlVersion 用于显示电脑实际执行进度。

## 10. Windows 建议状态机

```text
received
  -> executing
      -> committed
      -> failed
      -> lease_expired
      -> superseded
      -> connection_invalidated
```

Windows 必须：

- 按 `(playbackContextId, epoch, controlVersion)` 严格串行；
- 命令携带的 executionTimeoutMs 是执行租约的一部分；
- 切歌根命令失败后丢弃更高 pending；
- localUser intent 期间暂缓 remote；
- localUser confirmation 后丢弃旧 superseded remote；
- 断线/nonce/epoch/deviceSession/authority 改变时使全部旧 lease 失效；
- 任何失效 lease 的迟到 callback 都不能发送 committed 或改变 canonical projection。

## 11. 建议的契约更新范围

双方确认后，建议保持协议版本 `2.4.0`，文档修订提升到下一 revision，并修改：

1. 新增服务端 push action `playback.control.settled`；
2. 增加 dependency_failed 和 execution_unknown 固定错误码；
3. server-routed control 增加 `executionTimeoutMs`；
4. 删除重连后可重投一次的规则，改成 unknown + 不重放；
5. 增加切歌失败的后续 pending 依赖终止；
6. 增加 stale applied source-only correction；
7. 更新请求结算矩阵、schema 闭合、幂等、持久化、EARS 和验收矩阵；
8. Flutter migration guide 等契约更新后再作为正式实现输入。

## 12. 请服务端工程师确认的问题

1. 是否同意 `playback.control.settled` 作为 server-only transaction terminal，而不是服务端伪造
   playback.update？
2. 是否可以为每个 dependency_failed 版本分别推送 settled，而不是让客户端推断版本区间？
3. 当前 server-routed command 是否可以增加 `executionTimeoutMs`？
4. Windows 当前音频执行层是否能在 lease 失效后阻止迟到 commit 和实际音频切换？
5. 服务端 watchdog 是否同意：Windows 确认失败用 execution_timeout，无反馈只用 execution_unknown？
6. 是否同意断线/重启后不自动重放任何旧 remote command？
7. source-only passive correction 是否可以复用服务端已保存的 clientSeq 和 actual snapshot？
8. dependency cascade 是否只限定 `queue.playItem`、`player.next`、`player.prev` 三种根命令？

如果上述问题达成一致，再更新 normative contract。当前 r10 契约仍然有效，这份文件只用于双方讨论，
不得单独作为服务端或 Flutter 已完成实现的证明。
