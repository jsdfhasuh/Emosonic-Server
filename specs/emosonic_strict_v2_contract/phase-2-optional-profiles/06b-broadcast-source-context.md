# 阶段 2：Broadcast Source Context

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-08-01-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 5.5.1 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
#### 5.5.1 Source Context 单一事实源与派生 cursor

`broadcast.start` 不接受任何客户端播放快照。服务端必须在 source Context/pair 临界区读取并验证：

1. source Context 未关闭、queue-backed 且 authority 当前在线；
2. 当前物理 source Socket 已产生至少一个合法 DevicePlaybackState，且服务端当前时间减去该状态的
   serverUpdatedAtMs 与 positionSampledAtServerMs 都不得超过 2000ms；否则返回
   `conflict` 并等待新 `playback.update`。positionSampledAtServerMs 不得超过当前 server time + 50ms；
3. `deviceState.appliedControlVersion == playbackContext.controlVersion`，deviceState.trackId 等于
   canonical current item，且不存在 pending/failed 对账；否则返回带 source canonical cursors 的
   `conflict`，不得用未结算 target 或旧实际状态创建 Broadcast；
4. 当前 DevicePlaybackState.state 必须严格等于 `playing`；paused/stopped 返回带 source canonical
   cursors 的 `conflict`，不得创建“已暂停的群播”；
5. queue/currentIndex/trackId 来自 source Context；state/positionMs/positionSampledAtServerMs/playbackRate 来自
   当前 source DevicePlaybackState。服务端必须按 `positionSampledAtServerMs`、本次 start 的
   `effectiveAtServerMs` 和 playbackRate 把 positionMs 投影到计划生效时刻；不得使用
   `serverUpdatedAtMs` 代替采样时间。初始 Snapshot.serverUpdatedAtMs 必须等于本次
   effectiveAtServerMs。请求者提供的同名字段一律视为未知字段并返回
   `bad_request`。

成功 start 将 `broadcastRevision` 初始化为 1，并令 BroadcastSnapshot 的
`sourceEpoch/sourceVersion/sourceQueueRevision/sourceControlVersion` 精确等于同一原子读取中的 source
Context cursors。它们只是源 cursor 的带名副本，不是第二套可独立递增的播放版本。只有
`broadcastRevision` 表示群播分发/lifecycle 顺序。

`broadcast.play/pause/seek/playItem` 是绑定到 source Context 的控制入口，不是独立播放器命令。服务端
必须在与普通 player/queue control 相同的 source Context 事务中验证 base cursor、推进 source cursor、
创建 pending control transaction、持久化新的派生 BroadcastSnapshot 并令
`broadcastRevision = previousBroadcastRevision + 1`；随后
只向 source authority 发送第 6.7 节普通 `player.*` / `queue.playItem` command，向 ordinary participants
发送第 6.10 节对应 Broadcast mirror push。source 不得收到会再次执行音频的 Broadcast control push。
该普通 source command 必须携带 `executionTimeoutMs`，并按第 6.7 节的最高 pending lower
track-changing transaction 规则可选携带 `dependsOnControlVersion`。dependency 等待不消耗 execution
timeout，source command 与 ordinary mirror 的 effective-at 仍属于同一计划。

当控制目标位置需要从 state=playing 的 source DevicePlaybackState 投影时，该状态也必须满足 2000ms
freshness；过期返回 `conflict`，不得用旧 position 生成 source command 或 mirror push。paused/stopped
anchor 在对应 control transaction 已结算且 track/cursor 匹配时可以长期保持，不要求周期性刷新。

每个由 active Broadcast control 产生的 source 普通 command 与对应 ordinary mirror push 必须在上述
事务中只生成一次相同的 `effectiveAtServerMs` / `serverTimeMs` 计划时刻；所有收件人取得的两个值
必须逐值相等，且 `effectiveAtServerMs - serverTimeMs >= 250`。服务端不得先立即命令 source、再另行
计算 participant 的执行时刻，也不得为每个 recipient 分别取时。`broadcast.start` 不给 source 发送
音频 command；source 收到的 start lifecycle push 可以携带该计划元数据但只能忽略其音频含义，所有
ordinary participants 必须收到同一 start 计划时刻。

source authority 继续用普通 `playback.update` 结算 remote command、passive 事实和 localUser 操作：

服务端必须按一次已结算 source 变化中实际改变的最高优先级字段选择唯一 mirror action，并随该 action
发送完整新 Snapshot，不得由不同实现自由选择同义 action：

| source/Broadcast 变化 | 唯一 server push action |
| --- | --- |
| 成功创建 Broadcast | `broadcast.start` |
| 显式 Broadcast play/pause/seek/playItem target | 对应 `broadcast.play` / `broadcast.pause` / `broadcast.seek` / `broadcast.playItem` |
| source 的实际状态从 paused/stopped 变为 playing | `broadcast.play` |
| source 的实际状态从 playing 变为 paused/stopped | `broadcast.pause` |
| 已知 `player.seek` / `broadcast.seek` command，或 source `localUser` 在相同 track/currentIndex 下只改变 position | `broadcast.seek` |
| queueSongIds、currentIndex 或 trackId 任一变化，包括自然切歌和 localUser 选歌 | `broadcast.queue.sync` |
| `origin:"passive"` 且只有正常 playing position 推进 | `broadcast.progress` |
| playbackRate 改变、paused 与 stopped 之间切换，或 remote result 与既有 target 不同但不属于 queue/play/pause/seek 的实际修正 | `broadcast.state.sync` |
| source 断线并进入 waitingForSource | `broadcast.waiting` |
| 相同 source pair 重连并按 fresh 实际状态恢复 active | `broadcast.resume` |
| 相同 ordinary pair 物理重连后重新投递当前 Snapshot | `broadcast.resync` |
| Broadcast 进入 terminal stopped | `broadcast.stop` |

同一 source 提交同时改变多个字段时，选择顺序必须是
`stop > queue/track/index > waiting/resume > play/pause > seek > playbackRate/state correction > progress`；
一次提交只增加一次 broadcastRevision 并发送一个 action。已提前分发的显式 Broadcast command 在
source remote committed 结果与 target 逐字段相等时不产生第二个 revision/push；只有实际结果不同才按
上表产生一个 correction action。seek/progress 必须由已知 command transaction 或 playback.update.origin
确定；服务端禁止通过 position 差值、时间阈值或漂移大小猜测 action。remote result 只有 position 修正
但既不是已知 seek target，也不是 localUser/passive 规则时，使用 `broadcast.state.sync`。

- committed/failed 必须先按第 6.6 节结算或对账 source Context；与已分发 target 完全相同的 committed
  只结算 source transaction，不增加 broadcastRevision。产生实际差异时，才在同一提交中更新 source
  cursor 副本、令 `broadcastRevision = previousBroadcastRevision + 1` 并按上表推送唯一修正镜像；
- source remote failed、execution_unknown 或 dependency_failed gap 必须先按第 6.7.3 节分配新的
  internal reconciliation version R。Broadcast 只从 R 的 canonical actual fact 派生一次 correction，
  不得从旧 terminal command 或 settlement 额外生成 revision；
- source 的 localUser 状态/选歌以及 source authority 的 `queue.context.sync` 必须先修改唯一 source
  Context，再派生 Broadcast push；
- source 播放器自然播完后进入下一首时，必须先按第 5.2.1 节用 `queue.context.sync`
  提交新 currentIndex/position，trackId 由服务端从 queue 推导；不得仅用 trackId 不同的
  passive `playback.update`
  越过 source Context。活动 Broadcast 中该 queue mutation 必须只派生一个新
  `broadcastRevision` 和一次 `broadcast.queue.sync` mirror target；该 target 的位置必须从请求中的
  `positionSampledAtServerMs` 投影到 effectiveAtServerMs，不得从服务端接收时间起算；
- source 已位于最后一首并自然结束时，必须走第 5.2.1 节唯一 passive automatic terminal 例外，先把
  source Context 原子收敛为 stopped/0、version +1，再只派生一个 `broadcast.pause` revision；不得
  构造 next/repeat 或第二个 terminal fact；
- active Broadcast 中 source `queue.context.sync` 若要把非空 queue 清为 idle，服务端必须在同一
  原子串行提交中先用最后合法的非空 mirror target 生成唯一 terminal Broadcast revision、
  释放 ordinary mutation 屏障、安装 restorePending/outbox，再按普通 Context 规则把 source queue
  提交为 idle。terminal snapshot 的 source cursors 冻结为 idle mutation 之前的历史值；不得
  生成空 queue 的 `broadcast.queue.sync` push。任一子操作失败时整个事务无副作用；
- 客户端不得发送 `broadcast.queue.sync` request。该 action 仅是服务端在 source
  `queue.context.sync` 或 source 对账后发给 ordinary participants 的完整镜像 push；客户端方向收到
  同名 action 返回 `not_supported`；
- playing passive position 可在服务端合并，镜像进度 push 每个 Broadcast 最多每秒 1 个。每次实际发送
  `broadcast.progress` 时，服务端必须在同一提交中从最新 positionSampledAtServerMs 投影
  position 到该 target 的 effectiveAtServerMs，并令 Snapshot.serverUpdatedAtMs 严格等于
  effectiveAtServerMs，再写入派生 snapshot，严格
  执行一次 `broadcastRevision = previousBroadcastRevision + 1`，再以该新 revision 向全部 ordinary
  participants 分发；仅在内存合并但尚未发送时不得递增。state、track 或 playbackRate 变化也必须立即
  按上表选择 `broadcast.play/pause/queue.sync/state.sync` 并以相同的单次 +1 规则提交。任何合并都不得
  改写 source Context cursor。
