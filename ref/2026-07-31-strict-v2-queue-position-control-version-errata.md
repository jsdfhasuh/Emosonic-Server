# Strict-v2 `queue.context.sync` 控制版本规则勘误与日志复核

> 日期：2026-07-31
> 面向：EmoSonic 服务端工程师
> 协议：strict-v2 `2.8.0`，契约修订 `2026-07-23-r18`
> 状态：规则已勘误；服务端需按新规则复核，双端真机仍待验证
> 原始日志：`C:\Users\jsdfhasuh\Documents\debug.log`

## 1. 简单结论

播放时，`positionMs` 会一直自然增加。两次位置不同，不代表用户执行了 seek。

因此：

- 普通非空队列内容变化只增加 `version` 和 `queueRevision`；
- 自然播放导致的 `positionMs` 变化不增加 `controlVersion`；
- `currentIndex`、当前歌曲或 idle/non-empty 边界变化时，`queue.context.sync` 才增加
  `controlVersion`；
- 明确 seek 必须走 `player.seek` 或 `playback.update(origin:"localUser")`；
- 服务端不能根据位置差值猜测发生了 seek。

旧报告把位置变化也当成控制变化，这条结论是错误的，本文件已纠正。

## 2. 四个版本分别表示什么

| 字段 | 含义 |
| --- | --- |
| `version` | PlaybackContext 总体状态版本 |
| `queueRevision` | 队列内容和当前索引的版本 |
| `controlVersion` | 服务端已接受的最新播放控制版本 |
| `appliedControlVersion` | 某台设备已经执行到的控制版本 |

`queueRevision` 和 `controlVersion` 不需要一直相等。追加或删除非当前歌曲时，前者可以增加，后者保持
不变。

`appliedControlVersion` 是设备状态的一部分。没有新控制时，它应与原来的 `controlVersion` 一起保持
不变，而不是跟着 `queueRevision` 增加。

## 3. `queue.context.sync` 的正确规则

### 3.1 只改普通队列内容

例如当前仍播放 `song-1`，只在队尾增加 `song-50`：

```text
before: queueRevision=10, controlVersion=7, appliedControlVersion=7
after:  queueRevision=11, controlVersion=7, appliedControlVersion=7
```

请求必须携带 `baseQueueRevision=10`，并省略 `baseControlVersion`。即使发送时的 `positionMs` 比旧快照
更大，也仍按这条规则处理。

### 3.2 切换当前索引或当前歌曲

例如 currentIndex 从 3 变为 4，或者同一索引对应的 trackId 改变：

```text
before: queueRevision=10, controlVersion=7, appliedControlVersion=7
after:  queueRevision=11, controlVersion=8, appliedControlVersion=8
```

请求必须同时携带 `baseQueueRevision=10` 和 `baseControlVersion=7`。这里控制版本增加的原因是当前索引
或歌曲变化，不是随请求一起携带的新位置。

如果服务端存在不能被覆盖的 pending/failed 控制事务，必须在修改任何版本前返回明确错误，不能只改
一部分状态。

### 3.3 空队列边界变化

idle 变为非空队列，或非空队列变为 idle 时，`version`、`queueRevision` 和 `controlVersion` 都增加。
当前 authority 已经完成这次变化时，它的 `appliedControlVersion` 同步到新的控制版本。

### 3.4 明确 seek

seek 不应伪装成 `queue.context.sync`：

- 远程 seek 使用 `player.seek`；
- Windows 本机人工 seek 使用 `playback.update(origin:"localUser")`；
- 普通进度上报使用 `playback.update(origin:"passive")`，不增加控制版本。

## 4. 旧日志现在能证明什么

日志中出现过：

```text
playbackContext.controlVersion = 215
Windows deviceState.appliedControlVersion = 196
```

这个差距会让 `broadcast.start` 的来源检查一直等待，最终出现
`local_context_update_pending` / timeout。这一点仍然成立。

日志还记录了两次 `queue.context.sync`：

```text
第一次：currentIndex=28, positionMs=116105, controlVersion 213 -> 214
第二次：currentIndex=28, positionMs=119831, controlVersion 214 -> 215
```

但仅凭这些字段，不能断言两次控制版本都应该增加。必须继续比较每次同步前后的：

- currentIndex；
- currentIndex 对应的 trackId；
- idle/non-empty 边界。

如果三项都没变，服务端不应把 controlVersion 从 213 提高到 214/215；位置自然前进不是理由。

如果当前歌曲实际发生了变化，那么增加 controlVersion 是正确的，此时当前 authority 的
appliedControlVersion 也必须在同一事务中前进。

所以旧日志证明了“版本不相等导致群播超时”，但不能单独证明“服务端漏改 applied”这一种原因。原报告
对此判断过早。

## 5. 服务端必须实现的判断

处理合法 `queue.context.sync` 时：

1. 验证 authority、deviceSession、epoch 和 `baseQueueRevision`。
2. 计算新旧 currentIndex、当前 trackId 和 idle/non-empty 边界。
3. 只有上述控制事实变化时才要求并验证 `baseControlVersion`。
4. 队列内容变化时增加 `version/queueRevision`。
5. 控制事实变化时增加 `controlVersion`，并按现有规则同步结算 authority applied cursor。
6. 只有位置不同而控制事实没变时，不增加 `controlVersion/appliedControlVersion`。
7. 原子提交后再发送 ACK 和 canonical `queue.context.sync`。

服务端可以保存 `positionMs` 和 `positionSampledAtServerMs` 作为事实锚点，但不能把位置差值转换成控制
操作。

## 6. 不要这样修

- 不要让每次队列同步都提高 `controlVersion`。
- 不要因为 `positionMs` 与旧值不同就提高 `controlVersion`。
- 不要要求普通内容变化携带 `baseControlVersion`。
- 不要让 passive `playback.update` 任意提高 `appliedControlVersion`。
- 不要把 `queueRevision` 当成 `appliedControlVersion`。
- 不要删除群播开始时 `appliedControlVersion == controlVersion` 的检查。
- 不要用一次内容变化的队列同步修补历史版本差距。

历史数据中真实存在的 `appliedControlVersion < controlVersion` 可能来自未完成的远程控制，也可能来自
旧实现错误。必须先检查控制事务记录，不能无条件把 applied 改成 control。

## 7. 服务端自动测试

至少覆盖：

1. 播放中追加非当前歌曲，同时 `positionMs` 自然前进：只增加 `version/queueRevision`，请求不需要
   `baseControlVersion`，`controlVersion/appliedControlVersion` 不变。
2. 只改变 `positionMs` 和采样时间：不得把它判断为 seek，不增加控制版本。
3. currentIndex 改变：同时增加 queue/control，authority applied 同步到新 control。
4. currentIndex 不变但该位置的 trackId 改变：同时增加 queue/control，authority applied 同步前进。
5. idle/non-empty 边界变化：queue/control/applied 按契约一起前进。
6. `player.seek` 和合法 `localUser` seek：增加控制版本；passive progress 不增加。
7. stale base cursor：返回契约错误，所有状态和版本都不变。
8. 事务失败：Context、Queue、control 和 applied 全部回滚。
9. 相同 requestId、相同内容重试：重放原结果，不重复增加版本。
10. 真正未完成的远程控制仍允许 `appliedControlVersion < controlVersion`，不能被队列同步误清除。

## 8. 验收标准

先使用新建或已经正确结算的 Context 测试：

```text
before: queueRevision=108, controlVersion=213, appliedControlVersion=213
content-only queue sync with natural position advance
after:  queueRevision=109, controlVersion=213, appliedControlVersion=213
broadcast.start: accepted
```

再测试真实切歌：

```text
before: queueRevision=109, controlVersion=213, appliedControlVersion=213
currentIndex/track changes
after:  queueRevision=110, controlVersion=214, appliedControlVersion=214
broadcast.start: accepted
```

如果旧 Context 一开始就是 `controlVersion=215`、`appliedControlVersion=196`，本次规则修正不会假装该
差距已经结算。需要先查清旧控制事务，或重新建立干净 Context，再做群播验收。

需要收集服务端、Windows 和 Android 同一时间段的完整日志，并记录实际歌曲、索引、位置和播放状态。
真机状态保持 `pending user validation`。

## 9. 当前审计状态

服务端提交 `caff62c` 的旧审计是在原规则下完成的，不能单独证明已经满足本次位置规则勘误。服务端需
增加第 7 节测试并重新审计。完成前，不应把旧的 449 项/1661 项测试结果当成本项已通过的证据。

本次勘误不修改 action、字段、错误码、数据库 shape 或协议版本，只修正版本增长条件。
