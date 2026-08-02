# Strict-v2 群播来源状态服务端改动说明

> 日期：2026-07-30
> 面向：EmoSonic 服务端工程师
> 协议：strict-v2 `2.8.0`
> 契约修订：`2026-07-23-r18`，2026-07-30 状态来源勘误
> 状态：契约已修改，服务端实现待核对和测试

## 1. 先说结论

这次不是协议大改，服务端通常属于**小到中等改动**。

| 范围 | 是否改动 | 预计大小 |
| --- | --- | --- |
| Socket action 和消息字段 | 不改 | 无 |
| 错误码 | 不改 | 无 |
| 协议版本和 contractRevision | 不改 | 无 |
| 数据库结构和历史数据迁移 | 不要求 | 无 |
| `broadcast.start` 来源状态判断 | 需要核对 | 小 |
| `queue.context.sync` 状态保存 | 需要核对 | 小到中等 |
| 自动测试 | 必须补充 | 中等 |

如果服务端目前已经只使用 `DevicePlaybackState.state` 判断来源设备是否正在播放，
`broadcast.start` 代码可能不需要修改，只需要补回归测试。

如果服务端会要求 `PlaybackContext.state`、Queue state 和 `DevicePlaybackState.state`
同时为 `playing`，或者队列同步会用 Context state 覆盖设备实际 state，就必须修改。

这次改动的风险主要在状态保存和事务顺序，不在消息协议。

## 2. 为什么要改

日志中出现过下面的状态：

```text
PlaybackContext.state = paused
Queue.state = paused
DevicePlaybackState.state = playing
controlVersion = 234
appliedControlVersion = 234
trackId = 当前队列歌曲
```

它表示：

- 服务器保存的控制目标是 paused；
- 设备目前实际正在 playing；
- 设备已经执行到服务器最新控制版本；
- 实际歌曲与当前队列歌曲一致。

按照改正后的契约，这个来源设备可以开始群播。

不能因为 PlaybackContext 或 Queue 还是 paused，就把它判断成“来源设备没有播放”。

相反，下面的状态不能开始群播：

```text
PlaybackContext.state = playing
Queue.state = playing
DevicePlaybackState.state = paused
```

是否真正播放，只看当前来源设备的 `DevicePlaybackState.state`。

## 3. 两类状态分别保存什么

| 状态 | 保存内容 | 用途 |
| --- | --- | --- |
| `PlaybackContext` | 队列、当前索引、当前歌曲、服务器接受的控制目标、Context 各版本号 | 确定群播复制哪一个播放任务 |
| `DevicePlaybackState` | 设备实际歌曲、实际播放状态、实际位置、采样时间、实际速度、音量和已执行版本 | 确定来源设备现在真正播放成什么样 |

这两类状态不要求每个字段一直相等。

`appliedControlVersion == controlVersion` 只表示设备已经执行到最新控制版本，不表示：

- 实际位置必须等于 Context 中的控制位置；
- 实际 state 必须一直等于 Context.state；
- 实际 playbackRate 必须一直等于 Context 中的控制值。

合法的 passive `playback.update` 可以在 applied 版本不变时继续更新设备实际状态、位置和速度。
它不能推进或偷偷修改 PlaybackContext 的版本和主状态。

Windows 本地人工操作仍必须使用 `origin:"localUser"`，不能伪装成 passive 更新。

## 4. `broadcast.start` 必须怎样判断

服务端必须在同一个 source Context/设备临界区读取 Context 和当前物理连接的
DevicePlaybackState。

### 4.1 必须检查

1. PlaybackContext 存在、未关闭、队列非空。
2. 当前 authority client/device 在线并绑定到该 Context。
3. 当前物理连接已经产生合法 DevicePlaybackState。
4. `serverUpdatedAtMs` 和 `positionSampledAtServerMs` 距离服务端当前时间都不超过 2000ms。
5. `positionSampledAtServerMs` 不得超过服务端当前时间 50ms 以上。
6. `deviceState.appliedControlVersion == playbackContext.controlVersion`。
7. `deviceState.trackId` 等于 Context 当前索引对应的歌曲。
8. 不存在未完成或失败后尚未完成处理的控制事务。
9. `deviceState.state == "playing"`。
10. 继续检查原有参与设备、能力、时钟和资源上限。

任一必要条件不满足时，使用现有的 `conflict`、`authority_offline`、
`capability_required` 等契约错误，不增加新错误码。

### 4.2 禁止增加的检查

以下判断必须删除，或者不得加入：

```text
playbackContext.state == "playing"
queue.state == "playing"
playbackContext.state == deviceState.state
queue.state == deviceState.state
```

Context 和 Queue 的 state 不能作为额外的群播 playing 门槛。

### 4.3 判断顺序伪代码

下面只是表达规则，不对应某一种服务端语言：

```text
context = readPlaybackContext(playbackContextId)
device = readCurrentAuthorityDeviceState(context)

reject if context is closed or queue is empty
reject if authority is offline or binding does not match
reject if device does not exist
reject if device.serverUpdatedAtMs is older than 2000ms
reject if device.positionSampledAtServerMs is older than 2000ms
reject if device.positionSampledAtServerMs is over 50ms in the future
reject if device.appliedControlVersion != context.controlVersion
reject if device.trackId != context.currentTrackId
reject if control reconciliation is incomplete
reject if device.state != "playing"

do not reject because context.state != "playing"
do not reject because queue.state != "playing"

continue participant, capability, clock and resource checks
```

## 5. 创建群播快照时从哪里取字段

| 字段 | 来源 |
| --- | --- |
| `queueSongIds` | PlaybackContext |
| `currentIndex` | PlaybackContext |
| `trackId` | PlaybackContext 当前索引对应的歌曲 |
| `sourceEpoch` | PlaybackContext |
| `sourceVersion` | PlaybackContext |
| `sourceQueueRevision` | PlaybackContext |
| `sourceControlVersion` | PlaybackContext |
| `state` | 当前 DevicePlaybackState |
| `positionMs` | 当前 DevicePlaybackState |
| `positionSampledAtServerMs` | 当前 DevicePlaybackState |
| `playbackRate` | 当前 DevicePlaybackState |

playing 状态的位置必须从 `positionSampledAtServerMs` 推算到本次
`effectiveAtServerMs`，不能从服务端收到消息的时间开始计算。

## 6. `queue.context.sync` 必须怎样保存状态

### 6.1 PlaybackContext.state 规则

规则固定如下：

1. idle 队列变成非空队列：Context.state 设为 paused。
2. 非空队列变成 idle：Context.state 设为 idle。
3. 非空队列同步成另一非空队列：保留原 PlaybackContext.state。
4. 非空队列的 trackId 始终由 currentIndex 对应的 queueSongIds 项得到。

队列本身不能判断设备当前是 playing、paused 还是 stopped。

### 6.2 controlVersion 前进时

如果本次队列同步使 `controlVersion` 前进到 `N`，服务端必须在同一事务中：

1. 把当前 authority 的 applied cursor 推进到 `N`。
2. 更新 DevicePlaybackState 的实际歌曲和位置：
   - `trackId`
   - `positionMs`
   - `positionSampledAtServerMs`
3. 保留 DevicePlaybackState 已有的设备实际字段：
   - `state`
   - `playbackRate`
   - `volume`
   - `muted`
4. 原子保存 Context、Queue cursor、control cursor 和 applied cursor。
5. 提交成功后再发送 ACK、状态通知和群播派生消息。

禁止用 `PlaybackContext.state` 覆盖 `DevicePlaybackState.state`。

如果当前还没有 DevicePlaybackState：

- 可以按现有模型推进独立保存的 authority applied cursor；
- 不得伪造 clientSeq、实际 state、实际速度或其他设备事实；
- 不得只为了允许群播而创建假的 DevicePlaybackState；
- `broadcast.start` 必须等待后续合法 `playback.update`。

### 6.3 passive 更新规则不放宽

不要允许 passive `playback.update` 任意提高 `appliedControlVersion`。

正确顺序是：

1. `queue.context.sync` 事务负责推进 control 和 applied。
2. 后续相同 applied 版本的 passive update 更新设备实际状态。
3. passive update 不增加任何 Context cursor。

## 7. 服务端不要这样修

- 不要要求 Context、Queue 和 DevicePlaybackState 三个 state 同时为 playing。
- 不要用 Context.state 覆盖设备实际 state。
- 不要删除 `appliedControlVersion == controlVersion` 检查。
- 不要删除实际歌曲与当前队列歌曲一致的检查。
- 不要允许 passive update 任意推进 applied 版本。
- 不要新增 `source_not_playing` 服务端错误码；服务端继续使用现有 `conflict`。
- 不要增加消息字段或修改 Socket action。
- 不要升级 protocolVersion 或 contractRevision。
- 不要无条件把历史数据的 applied 全部改成当前 control。
- 不要只延长客户端等待时间。

`source_not_playing` 是 Flutter 本地等待失败时使用的原因，不是本次新增的服务端协议错误码。

## 8. 服务端自动测试

至少增加以下测试。

### 8.1 群播开始判断

下表默认 authority、ordinary participants、capability、clock 和资源条件都合格，只改变表中列出的
来源状态条件。

| 用例 | Context/Queue state | Device state | 其他条件 | 结果 |
| --- | --- | --- | --- | --- |
| A | paused | playing | 两项时间未过期、版本相等、歌曲匹配 | start 成功 |
| B | stopped | playing | 两项时间未过期、版本相等、歌曲匹配 | start 成功 |
| C | playing | paused | 其他条件合格 | `conflict`，不创建 Broadcast |
| D | playing | stopped | 其他条件合格 | `conflict`，不创建 Broadcast |
| E | playing | 缺失 | 没有 DevicePlaybackState | `conflict` |
| F | playing | playing | 任一时间超过 2000ms | `conflict` |
| G | playing | playing | applied 小于 control | `conflict` |
| H | playing | playing | trackId 不匹配 | `conflict` |
| I | paused | playing | 存在未完成或失败对账 | `conflict` |

成功用例还必须证明 BroadcastSnapshot：

- queue/index/track 和 source cursors 来自 PlaybackContext；
- state/position/sample time/rate 来自 DevicePlaybackState；
- position 从 sample time 推算。

### 8.2 队列同步

1. idle 到非空后，Context.state 为 paused。
2. 非空到 idle 后，Context.state 为 idle。
3. 非空到非空后，Context.state 保持原值。
4. DevicePlaybackState.state 为 playing 时执行非空到非空同步，事务后仍为 playing。
5. DevicePlaybackState.state 为 paused/stopped 时，同步后仍保留原实际 state。
6. control 前进时，authority applied 在同一事务中前进。
7. 事务失败时，Context、Queue cursor、control 和 applied 全部回滚。
8. 没有 DevicePlaybackState 时，不创建假的设备状态，群播 start 继续失败。
9. 同版本 passive update 可以更新实际 state/position/rate，但 Context cursor 不变。
10. 真正的远程 pending control 仍允许 `appliedControlVersion < controlVersion`。

## 9. 建议日志

处理 `broadcast.start` 时建议记录：

```text
event=broadcast_start_source_check
playbackContextId=...
contextState=paused
queueState=paused
deviceState=playing
controlVersion=234
appliedControlVersion=234
contextTrackId=...
deviceTrackId=...
serverStateAgeMs=...
sampleAgeMs=...
decision=allow
```

拒绝时记录具体检查项，例如：

```text
decision=reject
reason=device_not_playing
```

这里的 `reason` 是服务端内部日志字段，对外仍返回契约已有的 `conflict`。

处理 `queue.context.sync` 时建议记录事务前后：

```text
contextStateBefore=paused
contextStateAfter=paused
deviceStateBefore=playing
deviceStateAfter=playing
controlVersionBefore=233
controlVersionAfter=234
appliedControlVersionBefore=233
appliedControlVersionAfter=234
```

## 10. 完成标准

服务端完成以下事项后，才算符合本次契约：

1. 自动测试 A 至 I 全部通过。
2. 队列同步 10 项测试全部通过。
3. Context paused、DevicePlaybackState playing 的请求能够创建 Broadcast。
4. Context playing、DevicePlaybackState paused 的请求不会创建 Broadcast。
5. 队列同步后设备实际 state 不被 Context.state 覆盖。
6. status 返回的 control/applied、track 和设备实际状态互相符合本说明。
7. 未修改 action、字段、错误码和协议版本。

如果服务端对上述行为本来就已经正确，只需补测试和日志证据，不要为了“跟随文档”重复改代码。

## 11. 与 Flutter 的关系

本次最新超时日志显示，Flutter 曾在发送 `broadcast.start` 前同时要求 Context、Queue 和本机都为
playing，因此请求可能还没有到达服务端。

所以：

- 服务端必须按本说明核对实现；
- Flutter 也必须删除 Context/Queue 的额外 playing 检查；
- 只修改服务端，不能保证本次客户端本地超时一定消失；
- 双端都符合契约后，再做 Windows 来源设备和 Android 接收设备的真机验证。

真机验证在双端日志复核前保持 `pending user validation`。

## 12. 契约依据

- [`specs/emosonic_strict_v2_socketio_server_contract.md`](../specs/emosonic_strict_v2_socketio_server_contract.md)
  的“2026-07-30 r18 状态来源勘误”。
- [`phase-1-core/08-server-queue-playback-and-controls.md`](../specs/emosonic_strict_v2_contract/phase-1-core/08-server-queue-playback-and-controls.md)
  的 `queue.context.sync` state 和 DevicePlaybackState 保存规则。
- [`phase-2-optional-profiles/06b-broadcast-source-context.md`](../specs/emosonic_strict_v2_contract/phase-2-optional-profiles/06b-broadcast-source-context.md)
  的 `broadcast.start` 来源检查。
- [`phase-3-conformance/11a-common-and-core-requirements.md`](../specs/emosonic_strict_v2_contract/phase-3-conformance/11a-common-and-core-requirements.md)
  的 REQ-033。
- [`phase-3-conformance/11b-broadcast-requirements.md`](../specs/emosonic_strict_v2_contract/phase-3-conformance/11b-broadcast-requirements.md)
  的 REQ-039 和 REQ-048。
- [`phase-3-conformance/13-integration-acceptance.md`](../specs/emosonic_strict_v2_contract/phase-3-conformance/13-integration-acceptance.md)
  的验收用例 42、75 和 78。
