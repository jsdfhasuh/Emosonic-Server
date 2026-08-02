# Strict-v2 群播来源状态：服务端结论与 Flutter 交接

> 日期：2026-07-31
>
> 服务端仓库：`/workspace/supysonic`
>
> 服务端基线提交：`caff62c`
>
> 适用协议：strict-v2 `2.8.0`
>
> 契约修订：`2026-07-23-r18`，包含 2026-07-30 来源状态勘误

## 1. 结论

当前 Supysonic 服务端对本次来源状态勘误已经符合契约，不需要继续修改服务端
运行时逻辑。

本轮服务端工作只补充了专项自动测试和审计文档，没有修改协议版本、
`contractRevision`、Socket action、消息字段、错误码、数据库结构或 Flutter
代码。

这个结论不表示双端问题已经修复。最新超时仍包含 Flutter 在发送
`broadcast.start` 前错误要求 PlaybackContext、Queue 和本机播放状态同时为
`playing` 的问题。Flutter 仍需独立修改，Windows 来源设备和 Android 接收设备
的真机验证保持 `pending user validation`。

## 2. 服务端已经确认的语义

### 2.1 群播来源是否正在播放

`broadcast.start` 是否正在播放，只看当前来源设备的
`DevicePlaybackState.state`。

| PlaybackContext/Queue state | DevicePlaybackState.state | 其他安全条件 | 服务端结果 |
| --- | --- | --- | --- |
| `paused` 或 `stopped` | `playing` | 全部满足 | 允许创建 Broadcast |
| `playing` | `paused` 或 `stopped` | 全部满足 | `conflict`，不创建 Broadcast |
| 任意 | `playing` | 设备状态缺失或不完整 | `conflict` |
| 任意 | `playing` | 任一时间超过 2000ms 或采样时间非法 | `conflict` |
| 任意 | `playing` | applied/control 不相等 | `conflict` |
| 任意 | `playing` | 实际歌曲与 Context 当前歌曲不匹配 | `conflict` |
| 任意 | `playing` | 存在未结控制事务 | `conflict` |

服务端不会因为 `PlaybackContext.state` 或客户端 Queue state 为
`paused/stopped` 而单独拒绝 `broadcast.start`，但原有 authority、绑定、
freshness、歌曲匹配、applied/control 和 pending control 检查全部保留。

### 2.2 Context 和设备状态允许不同

`PlaybackContext` 表示规范控制目标；`DevicePlaybackState` 表示设备实际播放
事实。两者允许暂时不同。

`appliedControlVersion == controlVersion` 只表示设备已经执行到最新控制版本，
不表示 Context 和设备的 `state`、位置、速度、音量等字段必须逐项相等。

### 2.3 queue.context.sync

服务端已经确认：

1. 非空队列同步到另一非空队列时，保留原 `PlaybackContext.state`。
2. 空/非空边界仍按契约处理 Context state。
3. queue sync 推进 applied cursor 时，在同一连接反馈作用域内保留设备已有的
   `state`、`playbackRate`、`volume`、`muted` 和 `clientSeq`。
4. 不会用 `PlaybackContext.state` 覆盖已有的设备实际 state。
5. 重连后的旧反馈不会被复用为当前物理连接事实；没有新鲜完整反馈时不能成为
   Broadcast 来源。

### 2.4 passive playback.update

已有 applied baseline 后，`origin=passive` 的 `playback.update`：

- 可以在 applied 版本不变时更新设备实际 state、位置和速度；
- 不会修改 `PlaybackContext.state` 或控制 cursor；
- 不能任意提高 `appliedControlVersion`；
- 真正的 remote pending control 仍允许
  `appliedControlVersion < controlVersion`。

## 3. 服务端代码证据

以下路径和行号基于服务端提交 `caff62c`：

- `supysonic/emo/ws.py:4381`：读取当前 authority 的
  `DevicePlaybackState`，然后调用来源资格检查。
- `supysonic/emo/strict_v2_effective_at.py:97`：
  `validateBroadcastSourceState`。
- `supysonic/emo/strict_v2_effective_at.py:125`：设备状态缺失、authority、
  epoch、clientSeq、applied/control 和歌曲匹配检查。
- `supysonic/emo/strict_v2_effective_at.py:177`：playing 判断只检查
  `device_state["state"]`。
- `supysonic/emo/strict_v2_effective_at.py:194`：两项时间 freshness 和未来采样
  检查。
- `supysonic/emo/ws_store.py:1028`：passive update applied cursor 约束。
- `supysonic/emo/ws_store.py:2665`：queue sync 推进设备状态并保留同一 feedback
  scope 的实际字段。
- `supysonic/emo/ws_store.py:2858`：只有空/非空边界会改变 Context state；
  合法非空到非空变化保留原 state。

## 4. 服务端回归测试证据

- `tests/base/test_emo_strict_v2_broadcast.py:499`：
  Context paused/stopped、设备 playing 时完整 `broadcast.start` 成功路径。
- `tests/base/test_emo_strict_v2_broadcast.py:505`：
  Context playing、设备 paused/stopped 时拒绝且不创建 Broadcast。
- `tests/base/test_emo_strict_v2_broadcast.py:543`：
  两项 freshness 分别过期时拒绝。
- `tests/base/test_emo_strict_v2_broadcast.py:577`：
  applied 不等、歌曲不匹配和未来采样时拒绝。
- `tests/base/test_emo_strict_v2_broadcast.py:612`：
  pending control 时拒绝。
- `tests/base/test_emo_ws_store.py:2254`：
  非空到非空保留每一种合法 Context state。
- `tests/base/test_emo_ws_store.py:2370`：
  queue sync 保留设备 state、playbackRate、volume 和 muted。
- `tests/base/test_emo_ws_store.py:2443`：
  applied 相等时 Context 与设备实际字段允许不同。
- `tests/base/test_emo_ws_store.py:2526`：
  passive update 不能推进已有 applied cursor。

服务端验证结果：

```text
python -m unittest \
  tests.base.test_emo_strict_v2_effective_at \
  tests.base.test_emo_ws_store \
  tests.base.test_emo_strict_v2_core \
  tests.base.test_emo_strict_v2_broadcast

Ran 449 tests in 58.055s
OK
```

```text
python -m unittest

Ran 1661 tests in 485.986s
OK (skipped=3)
```

## 5. Flutter 必须检查和修改的内容

Flutter Codex 应从 `broadcast.start` 的本地发送前条件开始检查：

1. 删除仅因 `PlaybackContext.state != playing` 而阻止请求的条件。
2. 删除仅因 Queue state 不是 `playing` 而阻止请求的条件。
3. 本地如需预检“是否实际播放”，只能依据当前来源设备的真实播放器状态或
   对应 `DevicePlaybackState.state`，不能使用 Context/Queue state 替代。
4. 播放器事实发生变化时，先按现有协议发送真实的 passive
   `playback.update`；不得为了通过检查伪造 playing、歌曲、采样时间或
   `appliedControlVersion`。
5. passive update 必须继续报告设备真实的 applied cursor，不得自行提升到
   Context controlVersion。
6. 不要新增协议字段、Socket action 或错误码，也不要只延长本地等待超时。
7. 请求发出后继续以现有服务端 ACK 或 `system.error` 作为最终结果。

建议给 Flutter 增加以下自动测试：

1. Context/Queue paused、来源设备实际 playing、歌曲和 cursor 匹配时，确认
   客户端确实发送 `broadcast.start`。
2. Context/Queue stopped、来源设备实际 playing 时，同样确认请求会发送。
3. Context/Queue playing、来源设备实际 paused/stopped 时，不得把 Context
   playing 误判为设备正在播放。
4. applied/control 相等但 Context state 与设备 state 不同时，不得强制把两者
   同步成相同值。
5. queue sync 或 status hydration 后，设备真实 state、playbackRate、volume
   和 muted 不被 Context state 覆盖。
6. passive update 不会为了满足 readiness 人为提高 applied cursor。

## 6. Flutter 完成后的联合验证

Flutter 修改后仍需执行双端真机验证：

1. Windows 来源设备实际播放，Context/Queue 为 paused 或 stopped。
2. 确认 Flutter 日志中真实发出了 `broadcast.start`，而不是本地
   `source_not_playing` 超时。
3. 确认服务端返回成功 ACK 并持久化 Broadcast。
4. 确认 Android 接收设备收到 start delivery 并开始播放。
5. 复核来源与接收端的位置、歌曲、控制版本和 freshness。

在完成上述日志复核前，不得把双端修复或真机验证标记为通过。

## 7. 相关文档

- `ref/2026-07-30-strict-v2-broadcast-source-state-contract-errata.md`
- `ref/2026-07-30-strict-v2-broadcast-source-state-server-change-guide.md`
- `docs/goal/emosonic_strict_v2_r18_broadcast_source_state_errata.md`
