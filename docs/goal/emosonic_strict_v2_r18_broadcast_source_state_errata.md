# Goal: strict-v2 r18 Broadcast 来源状态勘误落地

> 状态：Completed
>
> 制定日期：2026-07-31
>
> 协议：strict-v2 `2.8.0`
>
> 契约修订：`2026-07-23-r18`，2026-07-30 状态来源勘误

## 一、目标

核对 Supysonic 当前服务端是否按来源设备的 `DevicePlaybackState.state`
判断 `broadcast.start`，并用自动测试锁定 Context 控制目标与设备实际状态允许
不同的契约语义。

本 Goal 不修改协议版本、contractRevision、Socket action、消息字段、错误码、
数据库结构或 Flutter 代码。

## 二、实施前审计

当前代码已经具备下列行为：

1. `validateBroadcastSourceState` 只用当前来源 `DevicePlaybackState.state`
   判断是否正在播放，不要求 `PlaybackContext.state` 为 `playing`。
2. Broadcast start 继续检查当前 authority pair、两项 2000ms freshness、
   未来采样上限、control/applied 相等、歌曲匹配和 pending control。
3. `mutateStrictPlaybackContextQueue` 对合法非空 Context 保留原
   `playing|paused|stopped` state；idle/non-empty 边界按契约切换。
4. queue sync 推进 control/applied 时，在当前 connectionNonce 的 feedback
   scope 内保留设备 state、playbackRate、volume、muted 和 clientSeq。
5. 重连后的旧 feedback scope 不会被当作当前物理连接事实复用；内部
   `clientSeq=0` applied baseline 不出现在 status 中，也不能通过 Broadcast
   source 检查。
6. passive `playback.update` 在已有 applied baseline 后不能提高
   `appliedControlVersion`。

因此本次不为制造代码差异重写运行时逻辑。实施重点是补齐状态交叉矩阵和字段
保留测试；如果新增测试发现上述结论不成立，再做最小运行时修复。

实施前专项基线：

```text
python -m unittest \
  tests.base.test_emo_strict_v2_effective_at \
  tests.base.test_emo_ws_store \
  tests.base.test_emo_strict_v2_core \
  tests.base.test_emo_strict_v2_broadcast

Ran 436 tests in 57.386s
OK
```

## 三、实施计划

1. 补充 `broadcast.start` 来源矩阵：
   - Context paused/stopped、设备 playing 时允许 start；
   - Context playing、设备 paused/stopped 时返回现有 `conflict`；
   - DevicePlaybackState 缺失、任一 freshness 过期、未来采样、applied/control
     不等、歌曲不匹配或存在未结控制时拒绝；
   - 成功 Snapshot 的 Context 字段、设备字段和位置投影来源正确。
2. 补充 `queue.context.sync` 状态保存测试：
   - idle/non-empty 边界状态正确；
   - 非空到非空保留 playing/paused/stopped Context state；
   - control/applied 同步前进时保留设备 state、playbackRate、volume 和 muted；
   - 事务失败全部回滚，没有完整设备反馈时保持隐藏 baseline。
3. 补充 applied 语义测试：
   - 相同 applied 版本的 passive update 可以更新设备实际状态、位置和速度，
     但不改变 Context state 或 cursor；
   - passive update 不能任意提高已有 applied cursor；
   - 真实 remote pending control 仍允许 applied 小于 control。
4. 运行专项和完整 unittest，执行 `git diff --check`，记录实际结果。

## 四、完成标准

- [x] 状态交叉矩阵和原有安全门槛都有自动测试。
- [x] queue sync 保留 Context 与设备实际状态的测试通过。
- [x] passive applied cursor 的正反路径测试通过。
- [x] 未修改协议 metadata、wire surface、错误码或数据库结构。
- [x] 专项测试、完整 unittest 和 `git diff --check` 通过。
- [x] 最终报告明确 Flutter 仍需独立修复，本次不标记真机验证通过。

## 五、真机状态

Windows 来源设备和 Android 接收设备的双端验证保持
`pending user validation`。最新超时包含 Flutter 在发送 `broadcast.start`
之前错误要求 Context、Queue 和本机同时为 playing；服务端自动测试通过不能
替代 Flutter 修复或双端真机日志复核。

## 六、实施结果

新增测试确认实施前审计结论成立，服务端运行时代码无需修改：

- Context 为 paused/stopped、当前来源设备为 playing 时可以创建 Broadcast；
- Context 为 playing、当前来源设备为 paused/stopped 时返回现有 `conflict`，
  且没有 Broadcast 记录；
- 缺失、过期、未来采样、applied/control 不等、歌曲不匹配和 pending control
  仍会拒绝 start；
- queue sync 保留合法非空 Context state，并在当前 feedback scope 内保留设备
  state、playbackRate、volume 和 muted；
- 相同 applied 版本的 passive update 可以更新设备事实而不改变 Context；
- passive update 不能提高已有 applied cursor，远程 pending control 继续保留
  control/applied 差距。

实际验证：

```text
新增和受改专项：
Ran 17 tests in 2.677s
OK

Strict-v2 Effective-at / Store / Core / Broadcast：
Ran 449 tests in 58.766s
OK

完整测试：
Ran 1661 tests in 485.986s
OK (skipped=3)

git diff --check：
通过
```
