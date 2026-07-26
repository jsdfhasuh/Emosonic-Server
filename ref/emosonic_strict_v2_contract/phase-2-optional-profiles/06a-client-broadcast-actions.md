# 阶段 2：客户端 Broadcast Actions

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-07-23-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 5.5 节 action 表与公共语义。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
### 5.5 Broadcast（仅 negotiated capability `supportsBroadcast:true`）

Broadcast 是独立可选 profile。participant 必须是同用户在线 player，并协商到
`supportsBroadcast:true`、`effectiveAtPlayback:true`、`canPlay:true`、`canPause:true`、`canSeek:true`。只有本节全部 schema、
权限、cursor、effective-at 和第 3 节 playbackRate 执行承诺的 conformance tests 已通过时才能开放，否则返回 `capability_required`，不得提供
部分实现。发起或控制 Broadcast 的连接也必须协商到 `supportsBroadcast:true`，但 controller-only
owner 不需要具备本地播放能力，也不会因此自动成为 participant。
source 与 ordinary participant 的当前 connectionNonce 还必须已通过第 3.5 节 3-sample/15-second
clock gate；未通过的显式 ordinary 目标进入 skippedClientIds，source 未通过时 start 返回
`conflict`。

唯一 capability 例外是已持久化的 terminal drain：该 client/device pair 已存在 restorePending 时，
服务端在其完成 strict `2.8.x` 注册后必须补发 `broadcast.stop` 或 `broadcast.restore`，
即使本次 `supportsBroadcast:false`。这只用于清理旧覆盖层，不允许 start/control/新 mirror；
Flutter strict `2.8.x` player 必须始终能解析 terminal drain shape。部署关闭 Broadcast profile 不得丢弃
已有 restorePending/recovery records。

Broadcast 的产品语义是把 `playbackContextId` 当前 source authority 正在执行的播放任务复制到其他
participants 并同步播放，不是为 source 创建或切换到另一份播放任务。source Context 始终是源设备
自己的任务；ordinary participants 才使用临时音频覆盖层。停止 Broadcast 只终止复制/同步关系，
不得对 source transport 隐式执行 pause、stop、seek 或 queue replacement。

本节中的 `participants` 只表示接收镜像的 ordinary participants，不包含 source authority。source 由
`authorityClientId` / `authorityDeviceSessionId` 唯一标识，并通过普通 PlaybackContext command 与
`playback.update` 执行/结算；不得用 Broadcast participant feedback 代替 source 的正常状态通道。
ownerClientId 的当前 Socket 在本机角色是 controllerOnly 时可以收到第 6.10 节 Broadcast push
观察副本，但 owner 不因此成为
participant、不进入 participantStates，也不发送 feedback。

| Action / type | payload | 服务端动作与响应 |
| --- | --- | --- |
| `broadcast.start` / `command` | `playbackContextId:R`、`intentId:R non-empty string`、`participants:O non-empty distinct string[]` | Context authority 或有控制权的 controller 创建。source 当前已结算 DevicePlaybackState 必须是 playing，且 serverUpdatedAtMs/positionSampledAtServerMs 任一年龄都不超 2000ms；paused/stopped/stale 返回 conflict。服务端从 source Context/DevicePlaybackState 派生全部播放字段；请求禁止 queue/index/position/state/playbackRate/autoPlay 和 base cursor。省略 participants 时选择除 source 外全部合格在线 player。ACK 只允许并必须返回 `action`、`started:true`、`intentId`、`broadcastId`、最终 ordinary `participants`、`skippedClientIds`。 |
| `broadcast.status` / `state` | `playbackContextId:R`、`broadcastId:R` | 完整记录存在时 ACK 返回不可变 anchor `broadcast`、`participantStates` 与 response-level `serverTimeMs`；7 天后只存在请求 ordinary pair 的 compact record 时返回 `recovery` one-of 与 `serverTimeMs`。读取不修改 snapshot/revision。 |
| `broadcast.play` / `command` | `playbackContextId:R`、`broadcastId:R`、`baseControlVersion:R int>=1` | 在 source Context 临界区按 `player.play` 规则接受并路由给 source authority；同一提交更新派生 BroadcastSnapshot，再向 ordinary participants 推送 canonical `broadcast.play`。 |
| `broadcast.pause` / `command` | `playbackContextId:R`、`broadcastId:R`、`baseControlVersion:R int>=1` | 同上，按 `player.pause` 规则处理。 |
| `broadcast.seek` / `command` | `playbackContextId:R`、`broadcastId:R`、`positionMs:R int>=0`、`baseControlVersion:R int>=1` | 同上，按 `player.seek` 规则处理。 |
| `broadcast.playItem` / `command` | `playbackContextId:R`、`broadcastId:R`、`queueIndex:R int>=0`、`baseQueueRevision:R int>=1`、`baseControlVersion:R int>=1` | 在 source Context 临界区按 `queue.playItem` 规则接受，推进 source cursors、路由 source authority，并向 ordinary participants 推送同一派生目标。 |
| `broadcast.feedback` / `event` | 公共字段：`playbackContextId:R`、`broadcastId:R`、`deviceSessionId:R`、`deliveryId:R non-empty string`、`executionStatus:R applied\|failed`、`clientSeq:R int>=1`；条件字段见第 5.5.2 节 | ordinary participant 上报某个 mirror revision/delivery 的成功或失败结果。服务端验证 client/device/nonce、membership、lifecycle、revision、该 revision ledger 当前 deliveryId、track 证明和独立 clientSeq，原子更新 participantStates，并按第 6.10 节只向请求 Socket 发送 canonical confirmation；不回 ACK，不修改 source Context、Broadcast projection 或任何 cursor。ledger 无法匹配时改发 `broadcast.feedback.rejected`。 |
| `broadcast.stop` / `command` | `playbackContextId:R`、`broadcastId:R` | 原子终止 mirror lifecycle、释放 ordinary Context mutation 屏障并安装 restorePending eligibility fence；ACK 并向 source 与 ordinary participants 推送 terminal `broadcast.stop`，不得修改 source Context/transport。terminal full tombstone/outbox 保留 7 天，后续未确认 pair 压缩为 TerminalRecoveryRecord 并以 `broadcast.restore` 补发到恢复确认。 |

start 的 authenticated client 是 `ownerClientId`。owner 和当前 Context authority 可以执行
play/pause/seek/playItem/stop；ordinary participant 只能请求 status 和发送自己的 feedback，
不能控制。controller-only、未列入 participants 的连接或冒用其他 deviceSessionId 的连接不得发送
feedback。所有参与者必须满足上述 negotiated 条件。显式列表中同用户但离线、非 player 或能力不足的目标放入
`skippedClientIds`；任何跨用户目标使整个请求返回 `forbidden`；最终没有可用 participant 时返回
`bad_request`，但若至少一个其他方面合格的目标只因 recovery slot 上限被跳过，则返回
`rate_limited`。最终 ordinary participants 为空时不得创建 Broadcast。服务端必须在 source Context
临界区串行化 playback mutation，并精确校验 source base cursor；冲突返回 `stale_version`。stop 进入
不可逆 terminal lifecycle；之后的 playback mutation 返回 `conflict`，重复 stop 幂等。
`broadcast.progress`、`broadcast.state.sync`、`broadcast.waiting`、`broadcast.resume`、
`broadcast.resync`、`broadcast.restore`、`broadcast.feedback.rejected` 均为 server-only action；客户端
发送任一个都返回 `not_supported`，不得把它们误当作新的控制入口。
每个 ordinary pair 在 start 成功时必须预留一个 per-user recovery slot，从 terminal 后恢复确认时释放；
每 authenticated user 同时最多 256 个 slot。无可用 slot 的目标进入 skippedClientIds，不得先加入
后在 terminal 时丢失 recovery record。

Flutter 必须在首次发送 start 前持久化非空 `intentId`，并在未取得确定结算前跨 requestId、Socket
断线和进程重启复用它。服务端必须持久化
`(authenticated user, playbackContextId, ownerClientId, intentId)` 与规范化请求 fingerprint：同一 intent
和相同内容的任何重试必须返回首次 ACK，其中 `intentId`、`broadcastId`、participants 与
skippedClientIds 均原样不变，不重新筛选在线设备或产生 push；同一 intent 不同内容返回 `conflict`。
完整 Broadcast 状态至少保留到对应 terminal tombstone 可删除时；其后仍必须把 intentId、fingerprint
hash、首次 broadcastId、最终 participants 与 skippedClientIds 的 ACK outcome tombstone 保留到 source
PlaybackContext close。terminal 后 tombstone 还必须包含 terminalBroadcastRevision 与 stop ACK outcome。
Context 生命周期内旧 intentId 永远不得创建第二个 Broadcast。

每个 Broadcast 最多 20 个 ordinary participants，source 与 controllerOnly owner 均不计入 20。显式
`participants` 在去重和移除 source 之后仍超过 20 时，整个 start 返回 `bad_request`，不得截断；省略
participants 时，服务端按 clientId 升序从合格 ordinary players 中取前 20 个，其余列入
`skippedClientIds`。该规则与第 7.1 节资源上限必须使用同一个常量。

每个 source PlaybackContext 同时最多存在一个 `active|waitingForSource` Broadcast。start 临界区必须
同时检查 source Context 和 source client/device pair 是否已被任何 Broadcast 作为 source 或 ordinary
participant 占用，或是否存在尚未完成的 `restorePending` fence：命中同一已提交 intent 时按上一段
重放；其他 intent 一律返回 `conflict`，不得创建第二个 broadcastId、嵌套镜像、覆盖已有 membership，
也不得把仍在恢复原任务的 pair 重新选为 source 或 ordinary participant。

非终态 Broadcast 还必须为 source Context 建立 source-ownership fence：普通 source
queue/player/playback.update 继续按本节执行，但 `playback.context.close`、Handoff start/complete、会改变
authority/device binding 的 ensure 或其他 mutation 必须返回 `conflict`。相同 source pair 重连时，只允许
不创建、不初始化、不重绑、不改 snapshot 的 ensure 原样返回现有 Context；不同 deviceSessionId 或任何
authority 转移必须先 terminal stop 当前 Broadcast。

source authority 必须在线、属于 source Context 的当前 client/device pair，角色包含 `player`，并完整
协商 `playbackContextV2:true`、`supportsBroadcast:true`、`effectiveAtPlayback:true`、`canPlay:true`、
`canPause:true`、`canSeek:true`，能够设置全部合法 `0.5..2.0` playbackRate，且当前 nonce 已通过第
3.5 节 3-sample/15-second 时钟门禁；否则按缺失项返回 `authority_offline`、`capability_required` 或
`conflict`。`playbackPrepare`、`canSetVolume`、`remoteVolumeControl` 不属于 source 前置条件。
source clientId 出现在显式
participants 时服务端必须去除它，不得把 source 当作 ordinary participant，也不得把它列入
skippedClientIds。所有 push 逐 sid 注入各自 provenance，不能把 source 或任一 participant 的 nonce
复用给其他人。

participants 的 wire shape 仍是 clientId 数组，但服务端创建 Broadcast 时必须在内部把每项冻结为
当时已验证的 `(clientId, deviceSessionId)` pair。只有相同 pair 的当前有效 Socket 可以接收该
participant 的 push、发送 feedback 或被 `online:true` 表示；相同 clientId 以不同 deviceSessionId
注册不得继承 active Broadcast membership。相同 pair 重连后可以继续接收 active Broadcast，clientSeq
按新的 nonce/epoch 作用域重新开始。
