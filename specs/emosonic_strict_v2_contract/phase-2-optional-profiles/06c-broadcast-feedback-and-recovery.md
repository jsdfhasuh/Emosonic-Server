# 阶段 2：Broadcast Feedback、屏障与重连恢复

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-07-23-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 5.5.2 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
#### 5.5.2 Ordinary participant feedback、占用屏障与 source 重连

`broadcast.feedback` 仅表示 ordinary participant 执行第 6.10 节镜像的结果。source authority 禁止发送
该 action，其实际状态只来自普通 PlaybackContext DevicePlaybackState。request 公共字段之外只允许下列
两个互斥闭合 shape：

| executionStatus | 必需字段 | 禁止/条件规则 |
| --- | --- | --- |
| `applied` | `appliedBroadcastRevision:int>=1`、`queueIndex:int>=0`、`trackId:string`、`state:playing\|paused\|stopped`、`positionMs:int>=0`、`playbackRate:number 0.5..2.0` | 禁止 failed/error 字段；revision/deliveryId/queueIndex/trackId/state/playbackRate 必须严格匹配该 delivery target。positionMs 只校验 int、非负和媒体有效时长范围并作为设备实际观测值保存，不做精确时间位置比对。terminal revision 还必须 `state:"stopped"`、`restoreCompleted:true`；非 terminal 禁止 restoreCompleted。 |
| `failed` | `failedBroadcastRevision:int>=1`、`lastAppliedBroadcastRevision:int>=0`、`errorCode:string` | `lastAppliedBroadcastRevision < failedBroadcastRevision`；`errorMessage:O string`；禁止 applied revision、queue/track/state/position/rate/restoreCompleted。 |

failed errorCode 必须是 `track_load_failed|queue_apply_failed|effective_at_missed|clock_unsynchronized|rate_unsupported|restore_failed|execution_failed` 之一；errorMessage 遵守第 4.2 节脱敏与长度限制。

服务端必须为 active/terminal Broadcast 保留至少最近 512 个 revision 的 queueIndex/trackId/state/position/
playbackRate target ledger，且任何仍可能在 10 分钟反馈窗口内到达的 revision 不得提前删除。对仍有
该 pair retained ledger 的 revision，applied revision 低于该 pair 已保存的最高成功 revision 时返回
`conflict`。failed.lastAppliedBroadcastRevision 必须等于服务端为该冻结 pair 保存的最高成功
revision（尚无成功时为 0），否则返回 `conflict`。failed feedback 不推进 applied revision；同一
revision 先 failed、随后以更高 clientSeq 成功 applied 是合法重试。
failedBroadcastRevision 在对应 ledger 仍存在时还必须等于 participantStates.targetBroadcastRevision；
更低但仍保留 ledger 的迟到 failure 返回 `conflict`，避免旧失败覆盖更新 target 的 pending/lagging 状态。
完整记录已压缩时，TerminalRecoveryRecord 代替已删除 participantStates/ledger：只接受同一冻结
pair 对 `terminalBroadcastRevision` 的 applied/failed feedback，queueIndex/trackId/state/playbackRate、
revision 与 deliveryId 必须按 compact record 校验；positionMs 只按下文类型/范围规则校验。此时上述 failedBroadcastRevision 等值条件改为等于
TerminalRecoveryRecord.terminalBroadcastRevision，failed.lastAppliedBroadcastRevision 必须等于 record 中
同名字段；不需要重建已删除的 participantStates。

每个 ordinary target ledger 还必须按 `(pair, broadcastRevision)` 保存该 revision 当前有效的
`deliveryId`、实际 push action、delivery effectiveAtServerMs/serverTimeMs（无值时省略）以及服务端从
Snapshot anchor 投影出的 `deliveryPositionMs`。这些都是 per-delivery ledger，不写回
BroadcastSnapshot，也不改变 revision identity；同 revision resync 只替换该 pair 的 delivery ledger。
feedback 必须匹配其所报告 revision 的该值；queueIndex/trackId/state/playbackRate 按该 delivery target
严格校验。feedback.positionMs 是设备应用后的实际观测值；服务端只校验它是 int、非负并位于服务端
已知的当前媒体有效时长范围内，然后原样保存。因 feedback 没有位置采样时间，服务端不得使用 delivery
计划时间、position 差值或网络到达时间做精确时间位置比对，也不得反过来改写 canonical anchor。
更高 revision 已分发时，仍可接受 retained
旧 revision 的有效 delivery feedback，并按 lagging/最早未确认规则推进。只有同一 revision 被
`broadcast.resync` 或 terminal replay 换成新 deliveryId 时，旧物理连接/旧 attempt 的结果才失效。若 Broadcast 或
TerminalRecoveryRecord 仍可确定 canonical/floor，但请求 revision 的 target ledger 已清理、缺失或超前，
服务端不得用含义不稳定的通用 conflict/bad_request 代替，必须按第 6.10 节发送一次
`broadcast.feedback.rejected`：

- `revision_expired`：请求 revision 不高于 currentBroadcastRevision，但已经低于
  minimumRetainedBroadcastRevision，或其 pair ledger 已按合法 retention 清理；
- `revision_unknown`：revision 位于 retained 范围内却没有该 pair 对应 ledger/delivery，表示双方状态
  不一致；
- `revision_ahead`：revision 高于 currentBroadcastRevision。

该拒绝不修改 participantStates、deadline、Context/Broadcast cursor 或 restorePending。被拒绝的
`clientSeq` 作为该连接 feedback 序号作用域中的已结算 rejection 保存；相同 clientSeq/content 只重放
同一 rejected event，相同序号不同内容或倒退返回 `client_sequence_conflict`，后续 feedback 必须使用
更高 clientSeq。若连 Broadcast、terminal recovery 和可提供 current/floor 的 tombstone 都不存在，才
返回 correlated `system.error(code:"not_found")`。

成功结算 `broadcast.feedback.rejected` 后，服务端必须为同一请求 pair 持久化并推送一个新的可执行
delivery，不能只等待客户端查询 status：非终态 `active|waitingForSource` 使用新 deliveryId 的
`broadcast.resync`；完整 terminal record 使用新 deliveryId 的 `broadcast.stop`；compact terminal
record 更新 currentDeliveryId 后使用新 deliveryId 的 `broadcast.restore`。新 delivery 复用当前
canonical/terminal revision，不新增 broadcastRevision，并按本节 effective-at/创建时刻规则建立新的
feedback deadline。若 Socket 在推送前断开，既有 per-pair outbox/replay 义务必须在相同 pair 下次连接
继续完成。相同 clientSeq/content 的幂等 rejection 重放不得再生成第二个 delivery；首次结算创建的
delivery 未完成时只按原 deliveryId/outbox 重放。`broadcast.status` 始终是只读结果，单独查询或返回
status 不能代替上述新执行 delivery。

服务端必须为每个 pair 另行维护 `deadlineBroadcastRevision` 与
`feedbackDeadlineAtServerMs`。它们在 deadline 关闭后仍保留最后一次 deadline 的原值用于诊断，不得
置 null、归零或改成当前时间；计时器是否运行只由 syncStatus 决定。`deadlineBroadcastRevision` 是自上次新接受的合法 feedback 或物理
重连补发后最早尚未获得结果的 target revision，而 `targetBroadcastRevision` 仍是最新已分发
revision。每次创建新 delivery 时，有 effectiveAtServerMs 的 deadline 严格等于
`effectiveAtServerMs + 8000`；没有 effective-at 的 deadline 严格等于服务端创建本次 delivery 的时刻
`+ 8000`，不得使用旧 Snapshot.serverUpdatedAtMs。第一个 target 和物理重连/rejection 后的补发都使用
该规则建立 deadline。在该 deadline 闭合前：

- 后续进度或其他新 target 只更新 `targetBroadcastRevision`，不得改写
  deadlineBroadcastRevision、feedbackDeadlineAtServerMs 或把 timedOut 改回 pending；
- 新接受的合法 applied feedback 必须同时匹配所报告 revision ledger 的当前 deliveryId；等于当前 target 时原子取消计时器并设
  `syncStatus:"applied"`；低于当前 target 时
  保存实际状态并设为 `lagging`，再从最早高于该 applied revision 的已分发 target 重建
  deadline，新 deadline 为服务端接受该 feedback 的时间 + 8 秒；
- 当前 target 的新合法 failed feedback 原子取消计时器并使 `syncStatus:"failed"`；之后首个新 target 以自己为
  deadlineBroadcastRevision 重建 8 秒 deadline；
- feedbackDeadlineAtServerMs 到期时，只要 deadlineBroadcastRevision 仍未被新合法 applied/failed
  结果闭合，立即设 `syncStatus:"timedOut"`、
  `timedOutBroadcastRevision = deadlineBroadcastRevision`、`errorCode:"feedback_timeout"`；即使此时有更高
  targetBroadcastRevision 也不延期；
- 幂等 feedback 重放不得重建 deadline。同 pair 在新物理连接用新 deliveryId 补发最新 snapshot 时，
  可以最新 target 为 deadlineBroadcastRevision 重建一次 8 秒 deadline。failed/timedOut 不是永久终态，
  后续合法 applied 可以收敛为 applied/lagging。

`pending|lagging` 表示计时器 active；`applied|failed|timedOut` 表示计时器已经停止。applied/failed 后的
首个新 target 可建立新 deadline 并转为 pending；timedOut 后普通 `broadcast.progress`、幂等重放或
status 读取都不得重建。timedOut pair 只有接受一个合法更高 clientSeq feedback，或在新物理连接发送
带新 deliveryId 的 `broadcast.resync`/terminal replay 后，才可建立新 deadline。客户端不得仅凭
feedbackDeadlineAtServerMs 是否晚于当前时间推断计时器仍运行。

任何 participantStates 更新都不得回写 BroadcastSnapshot 或 source Context，也不得增加
broadcastRevision。ordinary participant 在成功或失败应用
start/play/pause/seek/playItem/queue.sync/progress/state.sync/waiting/resume/resync 后立即反馈；playing
成功进度最多每秒 1 个。terminal 成功反馈的 queueIndex/trackId/positionMs/
playbackRate 描述被销毁前的 terminal mirror target，不描述已经恢复的原任务；`restoreCompleted:true`
只证明原任务恢复门控已完成。相同 clientSeq/content 幂等，相同 clientSeq 不同内容或倒退返回
`client_sequence_conflict`。非 terminal applied/failed feedback 的 `state` 描述镜像执行结果；terminal
Snapshot 使用 `lifecycleState:"stopped"` 且 `Snapshot.state` 保留 terminal 前最后 source/mirror anchor，
而 terminal applied feedback 的 `state:"stopped"` 只表示镜像 execution 已销毁。terminal 校验不得要求
feedback.state 等于 terminal Snapshot.state。

服务端接受 start 时，必须为每个 ordinary participant 记录其当前唯一 authority PlaybackContext 为
`suspendedPlaybackContextId`，并在 active 与 waitingForSource 期间建立覆盖 Context 与
`(authorityClientId, authorityDeviceSessionId)` binding 的 pair-level 写屏障。没有唯一 Context、已被
prepare/Handoff/其他 Broadcast 等瞬态执行占用、存在 `restorePending`，或无法原子建立屏障的目标进入
skippedClientIds。

该屏障必须由所有可能修改、关闭、初始化、重绑或转移 suspended Context/binding 的事务在同一临界区
检查，至少包括 `queue.context.sync`、`playback.context.prepare` / `prepared`、全部普通 `player.*`、
`queue.playItem`、`playback.update`、`playback.context.close`、`playback.context.ensure` 的创建/初始化/
重绑/快照修改、全部 `playback.handoff.*` / `playback.ready`，以及任何其他改变
authorityClientId/authorityDeviceSessionId 或 device→Context binding 的内部操作。命中屏障必须返回
带 suspended Context canonical cursors 的 `conflict`，不得产生 mutation、command、binding invalidation
或 push。`playback.context.list/status/subscribe/unsubscribe` 等只读或订阅操作可以继续；ensure 即使请求
内容看似相同也不得被当成隐式写通道，participant 重连按本节的 Broadcast replay 恢复，不重新 ensure
或捕获原 Context。

Broadcast 已 terminal 且该 pair 仍为 `restorePending:true` 时，`playback.context.ensure` 使用更精确的
结算：服务端返回同 requestId 的 `system.error(code:"restore_in_progress",retryable:true)`，携带
suspendedPlaybackContextId 及当前 `currentVersion/currentQueueRevision/currentControlVersion`。该请求
作为无副作用的已拒绝结果进入 requestId cache；相同 requestId 重放同一错误，不创建、初始化、重绑、
修改 snapshot 或递增任一 cursor，也不清除 restorePending。terminal applied feedback 被接受并清除
restorePending 后，客户端必须以新 requestId 重试 ensure。active/waitingForSource 占用屏障仍使用上一段
`conflict`，不得与 terminal restorePending 错误码混用。若 suspended Context 已在恢复竞态中 closed，
三个 current cursor 取 closed tombstone 的最终值，restore_in_progress 在 gate 清除前优先于
context_closed；Flutter 必须先按第 5.5.3 节吸收 closed/binding 变化、完成 terminal feedback，再用新
requestId ensure，不得靠 ensure 穿透 gate 创建替代 Context。

source authority 断线时，服务端不得修改 source Context 或 source cursors；应原子地将
`lifecycleState` 从 active 改为 waitingForSource、计算最后安全 pause anchor、令
`broadcastRevision = previousBroadcastRevision + 1`，
并向 ordinary participants 推送 lifecycle 为 waitingForSource 的 `broadcast.waiting`。从这一提交起启动
30 秒 timer；waiting 期间除 status/stop 外的 Broadcast mutation 返回 `conflict`。

owner/controller 断线本身不改变 lifecycle，只要 source authority 仍在线，镜像继续；ordinary
participant 断线只令该冻结 pair 的 online=false，屏障和 membership 保留到 terminal。相同 ordinary
pair 在 `active|waitingForSource` 重连并完成 register 后，服务端必须在接受该 pair 的普通 Context mutation 前只向该 pair 推送
server-only `broadcast.resync`，其中持久化 BroadcastSnapshot 与 broadcastRevision 原样不变，同时生成
新 `deliveryId`。lifecycleState=active 时，无论 Snapshot.state 为 playing、paused 还是 stopped，都必须
生成新的 `effectiveAtServerMs/serverTimeMs`；只有 lifecycleState=waitingForSource 时可以省略这两个
字段。这些 per-delivery 字段不属于 Snapshot 或 revision identity；playing mirror 从 snapshot 的
positionMs/serverUpdatedAtMs/playbackRate 投影到新 effective-at，paused/stopped 保持 snapshot.positionMs，
结果只写入该 pair 的 deliveryPositionMs；Snapshot anchor 本身不得被改写。服务端必须
把 `targetBroadcastRevision` 保持为该 revision、`targetDeliveryId` 改为新 deliveryId、设为 `pending`，
令 `deadlineBroadcastRevision` 等于该 revision，并按上文 effective-at/本次 delivery 创建时刻规则重新
启动 8 秒 feedback deadline。旧 deliveryId 的
迟到 feedback 不得关闭新 deadline。terminal lifecycle 不使用 resync，必须走下文完整
`broadcast.stop` replay 或 compact `broadcast.restore`。
客户端继续使用既有入口恢复快照，不得重新 ensure
suspended Context，也不得重新捕获已被镜像覆盖的当前队列作为恢复基线。若 register 后 ensure 与 replay
竞态，服务端以占用屏障返回
`conflict`，不能让 ensure 先修改原 Context。

相同 `(authorityClientId, authorityDeviceSessionId)` 在 30 秒内重连时，服务端必须等待该新物理连接
完成 register/ensure 并发送新的合法 `playback.update`。只有 source Context 已完成 pending 对账且
appliedControlVersion 追平 controlVersion 后，服务端才能按新 DevicePlaybackState 原子恢复 active、
令 `broadcastRevision = previousBroadcastRevision + 1`，并无论实际 state 为 playing、paused 还是
stopped 都向 ordinary participants 推送唯一 `broadcast.resume`；完整 Snapshot.state 携带实际状态，
playing 使用新 effective-at 计划，paused/stopped 保持不播放。这是服务端自动恢复，不要求或接受一次固定的手工
`broadcast.play`；source 客户端只恢复 lifecycle 标记，不重复执行音频。不同 deviceSessionId 不继承，
30 秒到期则进入 terminal stopped 并释放全部屏障，之后的 source 重连不得复活旧 broadcastId。

`broadcast.stop` 的 terminal lifecycle、`broadcastRevision = previousBroadcastRevision + 1`、普通
Context mutation 屏障释放、每个 ordinary pair 的 `restorePending:true` 和 terminal snapshot 持久化
必须原子提交；服务端完成提交后才可发送 ACK/terminal push。服务端重启或异常终止 Broadcast 时也
必须执行同一提交，基础 PlaybackContext 内容和 cursor 保持不变。

服务端必须把 terminal Broadcast 作为可重放 tombstone/outbox 自 terminal 提交时起完整保留 7 天。
完整记录必须包含 intentId、terminal BroadcastSnapshot、source 与 ordinary 的冻结 client/device pair、各
`suspendedPlaybackContextId`、入口 `epoch/version/queueRevision/controlVersion/appliedControlVersion`
基线、每个 pair 当前 `targetDeliveryId`，以及每个 pair 是否已经确认 terminal broadcastRevision。前 7 天内：

1. 重复 `broadcast.stop` 返回与首次等价的 ACK，`broadcast.status` 返回同一 terminal snapshot；
2. ordinary participant 离线不视为已投递或已恢复；
3. 相同 `(clientId, deviceSessionId)` 完成新连接 register 后，若尚未接受该 pair 对 terminal revision 的
   `state:"stopped"` feedback，服务端必须在向该 Socket 投递任何普通 Context command 之前补发一次
   `broadcast.stop` terminal push；Snapshot/revision 原样不变，但为该物理连接生成新 `deliveryId` 并
   原子更新 targetDeliveryId；每条物理连接自动补发至多一次，后续重连继续补发；
4. 补发只是同一 terminal snapshot 的新 delivery attempt，不得增加 broadcastRevision、重新释放屏障或
   再次修改任何 Context；不同 deviceSessionId 不继承该恢复义务，旧 deliveryId feedback 不得关闭新 attempt；
5. source pair 在保留期内重连时也必须补发一次原样 terminal lifecycle push，使其清理本地 lifecycle；
   source 不发送 feedback，该补发不影响 ordinary restorePending；
6. 已确认 ordinary pair 不再自动补发。

完整记录达到 7 天时，服务端必须在一个原子压缩事务中：

1. 对每个尚未确认的 ordinary pair 生成一条小型 `TerminalRecoveryRecord`，必需且只允许
   authenticated user key、source `playbackContextId`、broadcastId、冻结 clientId/deviceSessionId、
   terminalBroadcastRevision、currentDeliveryId、suspendedPlaybackContextId、冻结
   epoch/version/queueRevision/controlVersion/appliedControlVersion 基线、terminal queueIndex/trackId/
   positionMs/playbackRate、lastAppliedBroadcastRevision 和 terminalAtServerMs；
2. 保留该 pair 的 `restorePending:true`；已确认 pair 不生成 recovery record；
3. 只有全部必需 recovery record 持久化成功后，才删除完整 BroadcastSnapshot、full outbox、
   participantStates 和非 terminal target ledger；任一写入失败时保留完整记录，不得出现
   restorePending 无可补发记录的状态；
4. 第 4.4 节 ACK outcome tombstone 继续保留到 source Context close。

压缩后 owner/source 重复 `broadcast.stop` 时，服务端必须从 ACK outcome tombstone 重放规范化
stop ACK，不重建 Broadcast、不增加 revision、不重发 ordinary recovery。`broadcast.status` 对 owner/source
仍可返回 `not_found`：幂等 stop ACK 不等于恢复已删除的 full snapshot。

每个冻结 pair 因 restorePending 不得加入新 Broadcast，因此同时最多存在一条未闭合
TerminalRecoveryRecord。该 pair 以同一 deviceSessionId 重连时，服务端必须在普通 Context command 前
生成新 currentDeliveryId 并发送第 6.10 节 `broadcast.restore`；重连补发不增加 broadcastRevision。接受匹配
currentDeliveryId 的完整 terminal
applied feedback 后原子清除 restorePending 与 recovery record。source/controller 在完整记录已压缩后查询
status 可得到 `not_found`，并按第 5.5.3 节只清 lifecycle/UI。

Flutter 必须持久化最小恢复记录（broadcastId、source playbackContextId、
suspendedPlaybackContextId、原任务恢复 generation 与原任务快照），但不得持久化镜像队列。应用启动发现该记录时必须在 register 前先进入
`restoringOriginalContext` 门控，暂不接受本地播放 mutation，等待 terminal replay、
`broadcast.restore` 或 status 结算。
若服务端返回同 broadcastId 的 active/waitingForSource snapshot，客户端转回对应 mirror execution 并
保留恢复记录；只有 terminal 或下面的 not_found 回退才恢复原任务。
对未确认 ordinary pair，完整 terminal 或 compact recovery 必须至少存在一种。若已持久化
broadcastId 查询 status 既没有完整 terminal，也没有返回第 6.10 节 recovery one-of，而是
`not_found`，表示服务端数据丢失或不合规。Flutter 必须记录协议错误，
再先读取 suspendedPlaybackContextId 的服务端当前状态；
若该 Context 已关闭或 binding 已改变，则用原任务快照调用 ensure，并应用服务端返回的当前状态。
完成后清除恢复记录；不得继续停留在 Broadcast 覆盖层，也不得把镜像队列作为 ensure 输入。
