# 阶段 3：公共与 Core 实现要求

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-07-23-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 7 节 REQ-001—REQ-038、REQ-068—REQ-075。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
## 7. 服务端与 Flutter 实现要求（EARS）

**REQ-001 — ACK correlation**
当服务端收到具有合法 requestId/action 的 strict request 时，服务端必须按第 4.3 节使用相同
`requestId` 结算，并在 `system.ack` / `system.error` 的 `payload.action` 写入原 action。业务 push
不得复用该 requestId。缺失合法 requestId/action 时记录并断开。

**REQ-002 — 注册协商**
当 `device.register.capabilities.playbackContextV2` 为 `true`、请求合法且 Core ready 时，服务端必须
在 ACK 中提供完整 strict-v2 metadata 与 `negotiatedCapabilities`。Core 未 ready 时返回
`not_supported`，不得静默转 legacy。

**REQ-003 — 主版本兼容**
当服务端声称实现本 r18 契约时，`protocolVersion` 必须是 major `2`、minor `>=8`。服务端不得按客户端
差异返回非空-only 与 idle 两套 Context schema，也不得把 `2.5.x` 或更早 shape 当作本文完成。当 wire
shape 再次变化时必须同步更新 protocolVersion 和本文。

**REQ-004 — 收件人 provenance**
在 strict 注册成功后，服务端每次向该 Socket emit 任一 envelope 时，服务端必须注入与该物理 Socket 对应的 `connectionNonce` 和 `connectionEpoch`。

**REQ-005 — Context routing**
当服务端处理 context command 时，服务端必须以 `playbackContextId`、membership、authority 和 cursor 决定授权与收件人；服务端不得使用 `sessionId` 作为 strict 播放主键。

**REQ-006 — 无 direct-target strict 业务 push**
当服务端向 strict client 推送业务动作时，服务端必须按 recipient Socket 逐个发送无 `targetClientId` 的规范 envelope，而不是把 action payload 变成 direct-target command。

**REQ-007 — 失败闭合**
当请求 payload、capability、context membership 或 cursor 无效时，服务端必须返回 correlated `system.error`，且不得退回 legacy/session action。
缺失合法 requestId/action 的请求按第 2.2 节直接断开，是唯一无法 correlated error 的例外。
strict register ACK 前的 correlated error 按第 4.2 节省略 provenance；register ACK 后不得省略。
Context-scoped stale/queue/fence/closed error 必须带 playbackContextId 与 epoch/version/queue/control 四
cursor；资源解析必须先限定 authenticated user，跨用户与不存在统一 not_found。

**REQ-008 — 可选模式**
当连接未在 `negotiatedCapabilities` 获得 Follow、Broadcast 或 Handoff 所需能力时，服务端不得
向该连接执行相应可选模式动作，并对请求返回 `capability_required`。

**REQ-009 — Handoff target 的方向性例外**
当客户端发送 `playback.handoff.start` 时，服务端必须接受并验证 payload 内的
`targetClientId`；当服务端向目标 Socket 或其他 context 成员推送 Handoff 消息时，服务端
不得在 envelope 或 payload 中复制该字段。

**REQ-010 — 2.8.x cursor 契约**
当客户端发送第 5.2 节定义的控制或队列请求时，服务端必须继续接受其
`baseControlVersion` / `baseQueueRevision` 前置条件；服务端不得因为 accepted push 只使用
`controlVersion` / `queueRevision`，就在 `2.8.x` 内删除请求字段。

**REQ-011 — 唯一结算**
当服务端成功处理 strict request 时，服务端必须使用第 4.3 节为该 action 指定的唯一结算
方式；服务端不得用 ACK 和 direct response 重复结算。

**REQ-012 — 幂等重放**
当同一物理连接重复发送同一 requestId 时，服务端必须重放缓存结果且不重复副作用；当
requestId 相同但内容不同时，服务端必须返回 `conflict`。缓存至少保留 60 秒，断开时清除；
event-confirmed 重放只发给请求 Socket。

**REQ-013 — Cursor 单调性**
当服务端接受 Context mutation 时，服务端必须只按第 4.5 节矩阵递增对应 cursor；当 base
cursor 不等于 canonical cursor 时，服务端必须返回 `stale_version` 且不执行 mutation。

**REQ-014 — Schema 闭合**
当 strict request 含 action schema 未声明的字段、错误类型或超限值时，服务端必须返回
`bad_request`；服务端发出的 strict envelope 也不得含未声明字段或 JSON null 占位。

**REQ-015 — Profile readiness**
当某个 optional capability profile 尚未通过完整 conformance 时，服务端必须把它保持关闭并
返回 `capability_required`，不得以部分 handler 声称支持。

**REQ-016 — 角色与执行能力**
当服务端授权 Context action 时，服务端必须按同用户域、player/controller 角色和收件 authority
的 negotiated `can*` 能力共同判断；不得用请求者的播放能力替代执行 authority 的能力。

**REQ-017 — 持久化终态与重启**
当 Context close 或服务重启时，服务端必须按第 4.4、7.2 节持久化 tombstone/cursor 并显式终止
瞬态 profile，不得恢复半完成 Handoff、Follow 或 Broadcast。

**REQ-018 — 接受与发送顺序**
当服务端接受 authoritative mutation 时，必须先解析并验证全部收件人，然后完成原子状态与
持久化提交，才能发送 ACK 或 canonical business push。不同 Socket 之间不承诺全局到达顺序；
发送前发现 authority 无有效绑定时不得 mutation。

**REQ-019 — Nonce 强度**
当服务端建立新物理 Socket 时，必须使用 CSPRNG 生成至少 128 bit 随机熵的 connectionNonce，
不得复用或使用可预测输入构造。

**REQ-020 — Event confirmation 重放**
当 event-confirmed request 命中相同 fingerprint 的 request cache 时，服务端必须只向重复请求
Socket 发送第 4.4 节规定的 confirmation，且不得重复任何状态机或广播副作用。

**REQ-021 — Handoff 与 Broadcast 边界**
当服务端输出 Handoff errorCode 或 Broadcast participantStates 时，必须分别遵守第 5.4 节的稳定
码格式和第 6.10 节的 target/deadlineBroadcastRevision/syncStatus、applied/failure、deadline 与
last-feedback 成组字段规则。

**REQ-022 — 超限分层与顺序保持**
当 transport message 超限时服务端必须关闭连接；当已解析业务字段超限时返回 correlated
bad_request。任何序列化、持久化或重启恢复都不得排序 queueSongIds，集合字段必须按第 4.6 节
确定性输出。

**REQ-023 — Context discovery 闭环**
当已注册 controller 按 authority client/device pair 发送 `playback.context.list` 时，服务端必须只在
当前 authenticated user 的 active Context 中精确匹配并返回全部 binding。服务端不得扩展
`device.list` 携带 Context，不得只按 clientId 解析，不得使用 session fallback，不得在多结果时
自行选择，也不得把 list 当作 subscription 或可直接应用的服务端当前状态。

**REQ-024 — Binding invalidation**
当 ensure、close、handoff complete 或其他 mutation 改变某个 authority/device pair 的 active
Context binding 集合时，服务端必须在提交和请求结算后，按第 6.1.2 节向同用户全部 strict
controller 推送 `playback.context.bindings.changed`。不得只通知 Context subscribers，不得跨用户
发送，也不得在事件中携带猜测的 active Context。

**REQ-025 — Pair-level control serialization**
当服务端接受普通 player control 时，服务端必须在 authority/device pair 级别与 ensure、close、
handoff authority switch 串行化，并原子验证请求 Context 是该 pair 唯一 active Context。出现多
Context 时必须返回带 canonical cursors 的 `conflict`，不得发送命令或 mutation；不得依赖客户端
先收到 binding invalidation 才保证安全。

**REQ-026 — Device-scoped remote volume**
当协商 `remoteVolumeControl:true` 的 controller 对同用户在线 player 发送 `device.setVolume` 时，
服务端必须按 `targetClientId` / `targetDeviceSessionId` 精确解析当前 Socket，并只向该 Socket 发送
无 target 字段的 command。目标必须同时协商 `remoteVolumeControl:true` 与 `canSetVolume:true`；
错误 pair、离线目标或能力不足必须 fail-closed。设备的 `device.volume.update` 必须按连接级
clientSeq event-confirmed、只保存在在线瞬态状态中，并且上述请求、执行与反馈均不得创建或修改
PlaybackContext 及其任何 cursor。
`device.list.volumeState` 只有请求连接自身协商的 remoteVolumeControl 精确为 true 时才能输出；固定
capability shape 中存在该字段不构成授权。

**REQ-027 — Player startup ensure**
当具有 player 角色且 canPlay:true 的设备完成 negotiated 注册时，它必须立即发送
`playback.context.ensure`，并携带当时可用的本地队列、currentIndex、播放状态和位置；只有本机确实
没有队列时才发送 idle shape。已有第 5.5 节 Broadcast 恢复记录的 ordinary pair 必须先等待
active/terminal replay，不得用启动 ensure 穿透 suspended/restore 屏障。服务端必须原子返回当前唯一 Context、重绑同 stable clientId 的离线
旧 Context，或按该快照创建/初始化 Context。服务端不得要求设备先实际发声或由 controller 创建
Context，也不得产生第二个 active Context。

**REQ-028 — Idle Context closed shape**
当 Context 队列为空时，服务端必须输出 `queueSongIds:[]`、`state:"idle"`、`positionMs:0`，并省略
currentIndex/trackId；当队列非空时必须输出合法 currentIndex 与匹配 trackId，且 state 不得为 idle。
任何 request、response、push、持久化恢复和重启后状态恢复都必须保持该条件 schema。
每个 active Context snapshot 还必须同时携带 authorityClientId/authorityDeviceSessionId exact pair，
且不得增加 playbackRate。

**REQ-029 — Prepare before play**
当 controller 对 idle Context 发起 `playback.context.prepare` 时，服务端必须验证 intentId、最新
controlVersion、唯一 authority 和可选初始队列，最多建立一个 10 秒 prepare，并只向当前 authority
路由一次。authority 必须把队列写入同一 Context；controller 只有在 canonical queue 非空后才能使用
最新 controlVersion 发送原始 player.play。
该 prepare 是 Core action，不依赖 Handoff profile 或 playbackPrepare capability；playbackPrepare 只表示
Handoff target 能处理 server-routed playback.prepare。

**REQ-030 — Idle control fail-closed**
当 Context 为 idle 时，服务端收到 queue.playItem 或任一 player.* 请求必须返回 queue_required，
且不得递增 cursor、路由普通控制或伪造 playing/paused 状态。

**REQ-031 — Standby Context and Handoff**
当 Handoff 准备把 Context authority 切换到已经拥有 idle Context 的 target player 时，服务端必须在
同一原子提交中先把 target idle Context 写入 terminal tombstone，再安装 transferred Context binding；
如果 target Context 非 idle、存在非终态 prepare 或无法原子退休，则 Handoff 必须在 authority 切换前
返回 conflict。任何分支都不得让 target pair 暴露两个 active Context。

**REQ-032 — Remote control settlement**
当服务端接受 queue.playItem 或 player.* 时，必须为新 controlVersion 创建 pending 事务；correlated
ACK 只表示 accepted/routed。只有当前 authority 的 remoteCommand committed playback.update 可以把
匹配 pending 事务结算为 committed；failed update 结算为 failed；不得以 Socket emit 成功、ACK 或
canonical target snapshot 代替实际执行结果。
每个 routed command 必须携带 executionTimeoutMs，并按 deterministic dependency admission 可选携带
dependsOnControlVersion；dependency committed 前不得执行或开始 execution lease。

**REQ-033 — Canonical versus applied cursor**
当 authority 尚未执行最新控制时，服务端必须允许 `appliedControlVersion < controlVersion`，并在
status deviceStates 和 playback.update 中同时表达两个值。服务端必须按 applied transaction/snapshot
校验实际 track/state/position，不得要求 pending 期间实际 track 永远等于主 Context 最新控制目标。

**REQ-034 — Applied monotonicity**
当 playback.update 的 appliedControlVersion 低于该 device 的 lastAppliedControlVersion 时，服务端
必须忽略其状态副作用并记录迟到反馈；等值允许 passive 事实、匹配 pending command 的 failed 结果
或相同 terminal 幂等重放；高值必须由按序 remote committed 或 localUser transaction 证明。高于
canonical controlVersion 的 feedback 必须返回 bad_request。

**REQ-035 — Local user control allocation**
当当前 authority 发送合法 localUser committed playback.update 时，服务端必须在 Context 串行区从
当前 canonical controlVersion 加一，推进 applied cursor，按绝对 queueIndex/track/state/position
更新实际状态，并把旧 pending remote 标记为 superseded。observedControlVersion 小于或等于 canonical
可接受，大于 canonical 必须拒绝；服务端不得使用客户端猜测的新版本。

**REQ-036 — Local intent idempotency**
当同一 Context/epoch 的 localUser intentId 和内容重复时，服务端必须重放首次 canonical confirmation，
不得再次递增版本或 supersede；相同 intentId 内容不同返回 conflict。authority、deviceSession、epoch
或 Context lifecycle 改变时，旧 intent 不得应用到新 binding。

**REQ-037 — Failed command state correction**
当 remote command failed 且 actual 与 canonical target 出现 terminal gap 时，服务端不得在旧
commandControlVersion 下改写 Context 或把失败命令标记为 applied；必须按 REQ-073 分配新的 internal
reconciliation controlVersion 后收敛实际 snapshot。

**REQ-038 — Supersede execution barrier**
当 localUser update 被接受时，服务端必须持久化 supersededThroughControlVersion 并只将对应 pending
事务改为 superseded。Windows 必须暂缓本地 intent 期间的未完成远程命令，收到 canonical localUser
confirmation 后丢弃不高于该上界的未完成事务；更新版本的后续远程命令仍必须可执行。

**REQ-068 — Exact authority Context snapshot**
当服务端输出任一 active Context snapshot 时，必须同时输出 authorityClientId 与
authorityDeviceSessionId；ensure、status.playbackContext、queue.context.sync 及所有复用 snapshot schema
的消息必须一致。Context snapshot 不得包含 playbackRate，实际速度只保存在 DevicePlaybackState 或
对应 execution target。

**REQ-069 — Four-cursor error and scoped lookup**
当服务端结算 Context-scoped stale_version、queue_required、restore_in_progress、state-machine
conflict 或 context_closed 时，必须输出 playbackContextId 与
currentEpoch/currentVersion/currentQueueRevision/currentControlVersion，并描述真正阻止操作的 Context。
服务端必须按 schema/auth/registration/role/user-scoped lookup/fence/base/mutation 顺序处理，其他用户
资源与不存在资源统一 not_found，不得用全局查询或日志泄露资源存在性。

**REQ-070 — Deterministic control dependency**
当服务端接受普通 routed control 时，必须查找当前 Context/epoch 中最高的 pending lower
track-changing transaction，并在存在时把它写为 dependsOnControlVersion；track-changing action 只包括
queue.playItem/player.next/player.prev，依赖链允许传递。Windows 必须等直接依赖 canonical committed 后
才执行，依赖 terminal failure 或 supersede 时丢弃后继。

**REQ-071 — Execution eligibility and watchdog**
当 routed control 等待 dependency 或 effective-at 时，服务端与 Windows 都必须从 execution eligibility
而不是 accepted/routed 时刻开始 timeout。watchdog 必须等于 eligibleAt + executionTimeoutMs + 2000；
依赖等待不消耗 timeout，effective-at 后超过 1000ms 才 eligible 的命令必须以 effective_at_missed
失败。

**REQ-072 — Server-only control settlement cascade**
当 authority disconnect、Socket replacement、restart 或 watchdog 使 pending control 结果不可证明时，
服务端必须结算 execution_unknown；当 dependency failed/unknown/dependency_failed 时，必须按版本顺序
递归把直接后继结算为 dependency_failed，并在每条 playback.control.settled 中携带 requesting exact
pair，dependency_failed 指向直接依赖。settlement 按原物理 requester、当前 subscribers 和仍匹配的原
authority 去重发送，不得伪造 playback.update 或补给 replacement requester Socket。

**REQ-073 — Terminal-gap reconciliation**
当 failed/unknown/dependency terminal gap 已无 pending 且当前 authority 提供合法 fresh actual fact
时，服务端必须从 canonical controlVersion 分配新的 internal serverReconciliation version R，原子更新
Context version、必要时 queueRevision 和 DevicePlaybackState.appliedControlVersion。旧 terminal 结果
保持不变，Context 不增加 playbackRate，wire 只发送一次 passive 或 inline failed canonical
confirmation；Follow/Broadcast 只消费一次 R fact。

**REQ-074 — Safe Context close**
当客户端关闭 Context 时，请求必须携带 expectedEpoch/baseVersion；服务端在 Context/authority-pair
临界区验证所有 Handoff/Follow/Broadcast/restore fence，并保存 closedFrom、final 四 cursor 与 ACK
outcome。只有与 closedFrom 相同的新 requestId 重试重放 ACK，其他 tombstone close 返回
context_closed/final cursors，不再次推进版本。

**REQ-075 — Distinct queue terminal boundaries**
当服务端处理 queue 或边界控制时，queueSongIds 必须 distinct，且不得实现 shuffle/repeat。第一首 prev
重播第一首；最后一首 next 停在最后 index/stopped/0；最后一首自然结束只能使用严格的 passive
automatic terminal 例外推进一次 Context version，不推进 queue/control cursor，并只派生一次
Follow/Broadcast fact。
