# 阶段 3：Broadcast 实现要求

> [返回 r19 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-09-04-r19`；协议版本：`2.9.0`
> 覆盖范围：原契约第 7 节 REQ-039—REQ-067、REQ-088—REQ-090。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
**REQ-039 — Source-derived Broadcast projection**
当服务端创建或更新 Broadcast 时，必须把 source PlaybackContext 作为唯一播放事实源；start 播放字段
只能从状态为 playing、`clientSeq>=1` 且 applied==control 的当前 authority exact-pair
DevicePlaybackState 派生，Context/Queue snapshot 或内部 baseline 不能替代；paused/stopped 不得 start。
start 必须用持久化 intentId 跨连接重放首次 broadcastId，且每个 source Context 最多一个非终态
Broadcast。Broadcast control 必须在 source Context 事务中创建普通 control transaction；
BroadcastSnapshot 的 source cursors 在 active/waitingForSource 期间必须精确等于 source Context，
只能另行递增 broadcastRevision；terminal 时它们冻结为历史值，不跟随后续 source mutation。
Broadcast 不得保存或接受第二套 queue/index/position/autoPlay/cursor。每个 Broadcast control 的 source 普通
command 必须使用普通 control 的 executionTimeoutMs/deterministic dependency/eligibility 规则，并与
ordinary mirror push 共享事务内一次生成的同一 effectiveAtServerMs/serverTimeMs，
不得按 recipient 分别取时或让 source 无计划地提前执行。每个带 effective-at 的新 revision 必须把
position 投影到 effectiveAtServerMs，并令 Snapshot.serverUpdatedAtMs 等于 effectiveAtServerMs。

**REQ-040 — Dedicated ordinary-participant feedback**
当 ordinary participant 发送合法 `broadcast.feedback` 时，服务端必须按第 5.5、6.10 节验证当前
client/device/Socket、membership/lifecycle、applied/failed 条件 revision、track/queue 证明和独立
clientSeq 及所报告 revision ledger 的当前 deliveryId，只更新该 participant 的 participantStates，并只向请求 Socket 发送 canonical confirmation。source authority
发送该 action 必须 forbidden；任何 feedback 都不得修改 BroadcastSnapshot、PlaybackContext、control
transaction 或 cursor。feedback.positionMs 只校验 int、非负和媒体有效时长范围并保存为设备实际
观测值；因无位置采样时间，不得做精确时间位置比对。revision、deliveryId、track/queue、state 与
playbackRate 仍必须严格匹配 delivery target。syncStatus=applied 只证明 revision target 已应用，
不得解释为持续 drift 小于固定毫秒阈值。

**REQ-041 — Broadcast participant Context barrier**
当服务端接受 Broadcast start 时，服务端必须为每个 ordinary participant 原子记录并占用其唯一原
authority Context；占用期间对该 suspended Context/binding 的 queue、prepare、player、playback.update、
close、ensure 创建/初始化/重绑、Handoff/ready/authority switch 和其他 binding mutation 必须返回
conflict 且无副作用。已经作为其他 Broadcast source/ordinary participant 或 Follow follower 的 Context
或 pair 不得再次
占用。terminal stop、普通 mutation 屏障释放、restorePending 安装和 terminal snapshot 必须同成同败；source authority 的 Broadcast
Context 不得被当作 suspended Context 拦截，但必须受第 5.5 节 source-ownership fence 约束。
同一正常 source Context 可以同时拥有 Follow followers 与一个 Broadcast；两种 profile 必须保持各自的
recovery/fence，不得相互清理。

**REQ-042 — Participant restore versus source retain**
当 Flutter ordinary participant 首次接受 Broadcast start 时，它必须先捕获一次入口恢复快照，并将
群播作为不可持久化、不可写原 Context 的临时 execution；terminal stop 后按入口 playing/paused/stopped/idle
精确恢复冻结位置和速度。冻结的 Context/applied cursors 只作恢复基线和版本比较，不得写回或回退
服务端 cursor；服务端存在更高版本时必须应用服务端当前状态。source authority 不恢复另一 Context，
而是保留源 Context/队列/当前位置并
保持当前实际 playing/paused transport 连续运行，不得执行额外 pause/stop/seek；controller-only 不
操作音频。ordinary participant 从 terminal 到恢复完成必须保持 restoringOriginalContext 门控，禁用
且不得排队普通 Context 写命令或本地人工 transport/queue 操作；binding 变化时改为读取并应用服务端
当前状态。恢复完成前不得发送 stopped feedback，恢复期用户意图不得在 gate 清除后重放。任何群播
内部操作均不得上报为 localUser。

**REQ-043 — Source reconnect follows actual state**
当 active Broadcast 的 source authority 断线时，服务端必须进入 waitingForSource、保持 source Context
cursors 不变并用 `broadcast.waiting` 暂停 ordinary mirrors。相同 client/device 重连后必须等待新连接的合法 playback.update
与 Context 对账；该真实状态必须来自当前 physical nonce/epoch、`clientSeq>=1` 且 applied==control。
无论实际 playing/paused/stopped 都用唯一 `broadcast.resume` 携带实际 state，不要求手工 broadcast.play。
不同 deviceSession 不继承，30 秒超时后 terminal stopped 且不可复活。

**REQ-044 — Broadcast terminal idempotency**
当服务端接受 broadcast.stop 时，必须只物化一个 terminal snapshot，并向 source 与每个 ordinary
participant 发送一个 canonical terminal push；terminal 只修改 lifecycle/broadcastRevision，不得把 source state 改成
stopped 或推进 source cursor。重复 stop 返回等价 ACK，不重新物化 terminal。Flutter 必须按
broadcastId 用同一个一次性 gate 合并 terminal push、重复消息和 status 补偿；ACK 本身不得触发
第二个 restore/retain 或 stopped feedback。服务端必须把 terminal tombstone/outbox 保留至少 7 天；
未确认 ordinary pair 重连时必须在普通 Context command 之前补发同一 terminal Snapshot，且补发不推进
broadcastRevision；Snapshot/revision 不变，但新物理连接使用新 deliveryId。7 天后可按第 5.5.2 节原子压缩完整记录，但每个未确认 pair 的
TerminalRecoveryRecord/restorePending 必须保留到恢复确认。terminal push/restore 无法可靠加入当前
Socket 发送路径时必须立即断开，下次注册先 replay；不得先开放普通业务。

**REQ-045 — Broadcast progress revision**
当服务端实际发送一次合并进度或其他新 Broadcast 内容时，必须在同一提交中严格执行一次
`broadcastRevision = previous + 1`；仅合并未发送时不得递增，多 recipient 分发和幂等重放不得重复
递增，同 revision 的持久化 Snapshot 不得承载不同内容；per-pair deliveryId/effective-at 元数据不属于
revision identity。新 revision 的计划 target 必须以 effectiveAtServerMs 作为 position/serverUpdatedAtMs
共同时间锚点；ordinary resync 只更新 per-delivery 投影，不改 Snapshot。

**REQ-046 — Broadcast playback-rate capability**
当连接协商 `supportsBroadcast:true` 并作为 source 或 ordinary participant 时，它必须具备 player、
playbackContextV2、canPlay/canPause/canSeek、effectiveAtPlayback，并能够按 effective-at 设置并
保持本文允许的任意 `0.5..2.0` playbackRate。不能满足的 player 必须协商 false；服务端必须跳过该 ordinary 目标并在
显式选择时列入 skippedClientIds，不得加入后忽略速度。

**REQ-047 — Restore-pending re-entry fence**
当 ordinary participant 进入 terminal 后，服务端必须保留 pair-level restorePending，直到接受 terminal
revision 的 applied feedback 且 restoreCompleted=true。该 fence 必须阻止 suspended Context 的全部
普通写、该 pair 成为新 Follow/Broadcast/Handoff source/target 及任何 binding mutation；被阻止动作以
可重放 `restore_in_progress` 和真正 suspended Context 完整四 cursor 零副作用结算，Flutter 不得排队。
failed/timedOut feedback 不得清除 fence。只读/订阅、设备音量、terminal feedback/replay、
follow.stop、handoff.cancel 与 matching raced ready/prepared negative cleanup 可以继续；negative 不得
初始化、推进 cursor、commit 或清 fence。

**REQ-048 — Fresh source state**
当 authority state=playing 时，客户端必须至少每 1000ms 发送 passive playback.update。Broadcast start
以及需要 playing anchor 的 control 只能使用 serverUpdatedAtMs 与 positionSampledAtServerMs
都不超过 2000ms 的 DevicePlaybackState；位置必须从采样时间投影，不得从接收时间投影；
状态过期返回 conflict，不生成 Broadcast target。该 DevicePlaybackState 必须属于当前 authority exact
pair/physical nonce/epoch、`clientSeq>=1` 且 applied==control；`clientSeq=0` baseline 与任一 Context/Queue
snapshot 都不能满足 freshness 或 readiness。

**REQ-049 — Participant outcome and deadline**
当服务端分发 Broadcast target 时，participantStates 必须从 pending 开始，并以最早未确认 target
的 `deadlineBroadcastRevision` 设置 8 秒 deadline；无 feedback 时后续进度 revision 只更新最新 target，
不得延后 deadline。不得用 source snapshot 冒充 participant 实际状态。applied feedback 必须证明
revision/queueIndex/trackId，failed feedback 必须携带 failed revision/last applied/errorCode；deadline 到期
进入 timedOut，后续成功仍可收敛。pending/lagging 才运行计时器；applied/failed/timedOut 必须停止
计时器并保留最后 deadline 字段值用于诊断。每个新 delivery 有 effective-at 时 deadline 为
effectiveAtServerMs+8 秒，无 effective-at 时为本次 delivery 创建时间+8 秒，禁止使用旧 Snapshot anchor。

**REQ-050 — Effective-at clock and late policy**
当客户端执行 effective-at action 时，system.pong.serverTimeMs 必须存在，且最近时钟样本满足第 3.5 节
数量、新鲜度和不确定度。迟到不超过 1000ms 时按 target state 追赶，超过时必须通过对应 feedback
通道报告 effective_at_missed，不得声称成功。

**REQ-051 — Broadcast intent lifetime**
当 Broadcast 完整 terminal record 达到删除条件时，服务端仍必须保留 start intentId/fingerprint/
broadcastId/final participants/skippedClientIds/terminalBroadcastRevision/stop ACK outcome 的 ACK outcome tombstone
到 source Context close；任何
跨连接、跨保留期的相同 intent 重试都不得创建
新 Broadcast。每 Context 最多 1024 个；达上限时拒绝新 intent，但旧 intent 仍必须幂等重放。

**REQ-052 — Crash-safe ordinary entry**
当 ordinary participant 首次接受 start 时，Flutter 必须先持久化完整原任务恢复记录，
再暂停原任务并应用 mirror。持久化失败时必须保持原音频不变并发送
`restore_failed` feedback；应用崩溃后必须使用该记录恢复，不得把 mirror 队列重新捕获为
原任务。

**REQ-053 — Frozen participant pair and reconnect target**
当服务端输出 participantStates 时，每项必须包含 start 时冻结的 clientId/deviceSessionId
pair，online 只表示该 pair 的当前 presence。相同 pair 重连 resync 最新 revision 时不增加
broadcastRevision；服务端必须只向该 pair 发送 `broadcast.resync`，保持 Snapshot/revision 不变，生成
新 deliveryId；active 无论 playing/paused/stopped 都生成新 effective-at，waitingForSource 可以省略。
服务端令 target/deadlineBroadcastRevision 等于补发 revision，并按新 effective-at 或本次 delivery
创建时间重置 pending/deadline。旧 deliveryId 反馈不得关闭新 deadline；不同 deviceSessionId 不得继承。
membership 从 start 到 terminal 固定，断线/重连只改变 online，不支持 leave/add/remove 或重新筛选。

**REQ-054 — Natural source track transition**
当 source authority 因自然播完进入下一首时，客户端必须先通过 `queue.context.sync`
提交 currentIndex/position，由服务端推导 track 并更新唯一 source Context，再发 passive 实际状态。活动 Broadcast
必须从该 Context mutation 只派生一个 `broadcast.queue.sync` target，不得让 passive track
变化单独创建第二套 currentIndex。
当 source 已在最后一首自然结束时，必须先用唯一 passive automatic terminal 例外把 source Context
收敛为 stopped/0/version+1，再只派生一个 broadcast.pause revision；不得 repeat 或生成第二个 fact。

**REQ-055 — Non-terminal Broadcast restart**
当服务端重启时，任何 `active` 或 `waitingForSource` Broadcast 都必须以同一原子 terminal
规则进入 stopped，持久化 terminal outbox、restorePending、target ledger 和 ACK outcome tombstone，
不得只终止 active 而遗留 waitingForSource 屏障。

**REQ-056 — Source becomes idle**
当 active Broadcast 的 source queue mutation 要把非空 Context 清为 idle 时，服务端必须在同一原子
串行提交中先以最后非空 target 终止 Broadcast 并安装 restorePending/outbox，再提交
source idle Context。terminal source cursors 冻结为 idle mutation 之前的历史值；不得构造空队列
BroadcastSnapshot，也不得在 terminal 后跟随 source cursor。

**REQ-057 — Controller-only observation**
当 owner 在当前 Broadcast 中是 controllerOnly 且在线时，服务端必须向其发送 Broadcast
start/play/pause/seek/playItem/queue.sync/progress/state.sync/waiting/resume/stop 观察副本，使 source 自动转 idle、断线超时或服务端
重启导致的 terminal 可立即收敛 UI。controllerOnly 不进入 participantStates、不生成 target/deadline/
feedback，也不操作音频；owner 同时是 source/ordinary 时不重复投递观察副本。

**REQ-058 — Effective-at clock warm-up and cadence**
当 player 完成注册时，客户端必须立即顺序取得当前 nonce 的 3 个 clock 样本。协商
effectiveAtPlayback:true 的连接从注册起必须始终最大每 10 秒 ping；服务端在处理满 3 个
ping 且最近 ping 不超 15 秒前不得将其用于任何 effective-at 角色或 action。

**REQ-059 — Position sample time**
当 authority 发送 playback.update 或 queue.context.sync 时，必须同时发送用 server clock 换算的
positionSampledAtServerMs。服务端必须分别保存该采样时间与接收时间；Broadcast 的
playing position 只能从采样时间投影，两个时间任一过期都必须 fail-closed。

**REQ-060 — Earliest-unconfirmed feedback deadline**
当 participant 存在未确认 target 时，deadlineBroadcastRevision 必须指向自上次新合法 feedback
后最早未确认 revision。后续进度/target 只更新 targetBroadcastRevision，不得延后 8 秒
deadline 或清除 timedOut；applied/failed/timedOut 后 deadline 字段保留、timer 停止。只有新合法
feedback 或带新 deliveryId 的物理重连补发可从 timedOut 重建 deadline。

**REQ-061 — Restore cursors are compare-only**
当 ordinary participant 恢复原任务时，冻结 Context/applied cursors 只能用于版本比较，不得写回、
不得构造旧 base request 或使服务端 cursor 回退。任一服务端更高版本必须优先，Flutter 应用
服务端当前状态。

**REQ-062 — Bounded terminal and intent retention**
当 terminal 完整记录达到 7 天时，服务端必须先为每个未确认 pair 原子生成
TerminalRecoveryRecord，再删除 full snapshot/outbox；相同 pair 重连使用 broadcast.restore 或 status
recovery one-of 补发，直到 terminal applied feedback 清除 restorePending。每 pair 最多一条 compact
record，每 user 最多 256 个 recovery slots，每 source Context 最多 1024 个 ACK outcome tombstones；
上限后拒绝新 intent/participant 但仍重放旧 intent。已存在 restorePending 的 strict 2.8 pair 必须在
当前 supportsBroadcast=false 时仍可接收 terminal drain。

**REQ-063 — Deterministic source-to-push action**
当 source/Broadcast 变化被提交时，服务端必须按第 5.5.1 节固定映射和优先级只选择一个
`start|play|pause|seek|playItem|queue.sync|progress|state.sync|waiting|resume|stop` action，并携带完整
Snapshot。显式 command 的等值 committed 只结算 source transaction，不得再增加 revision；实际修正
才生成一个 correction action。seek 只能来自已知 seek command，或 localUser 在同 track/index 下只改
position；progress 只能来自 passive 正常 playing 推进。不得以 position 差值阈值猜测 action。

**REQ-064 — Immutable status anchor**
当客户端查询 broadcast.status 时，服务端必须原样返回持久化 BroadcastSnapshot anchor，并在 payload
外层增加本次 `serverTimeMs`；不得为了动态位置改写 positionMs 或增加 revision。客户端只按
`positionMs + max(0, serverTimeMs - serverUpdatedAtMs) * playbackRate` 投影 active+playing 位置并限制到
媒体有效时长。

**REQ-065 — Delivery-scoped resync**
当 frozen ordinary pair 发生物理重连时，服务端必须以 `broadcast.resync` 只重新投递给该 pair，保持
canonical BroadcastSnapshot/revision 不变，并生成新 deliveryId；active 的 playing/paused/stopped 都
必须带新 delivery effective-at，waitingForSource 可以省略。feedback 必须匹配该 revision ledger 的新
deliveryId；旧 attempt 的迟到结果不得推进 participantStates 或关闭新 deadline。

**REQ-066 — Terminal state domains**
当 Broadcast terminal 时，Snapshot.lifecycleState 必须为 stopped，而 Snapshot.state 保留 terminal 前
最后 source/mirror anchor。ordinary applied feedback.state=stopped 只证明 mirror execution 已销毁；
服务端不得要求它等于 Snapshot.state。入口 stopped 的 ordinary 原任务必须恢复 queue/index/position/rate
并保持 stopped，不得自动播放或改成 paused。

**REQ-067 — Expired feedback recovery**
当 feedback revision 的 pair ledger 已清理、缺失或高于 canonical 时，服务端必须发送仅面向请求 Socket
的 `broadcast.feedback.rejected`，使用 revision_expired/revision_unknown/revision_ahead 之一且不修改任何
状态或 cursor。随后必须按 lifecycle 用新 deliveryId 发送 resync、完整 terminal stop 或 compact
terminal restore；status 只读，不能替代该执行 delivery。客户端停止旧 feedback，被拒绝 clientSeq 已
结算，后续 feedback 必须针对新 deliveryId 并使用更高序号。

**REQ-088 — Broadcast soft sync and fixed membership**
当 participant 报告 applied 时，该状态只证明 revision target 已应用，不证明持续 drift 小于固定毫秒
阈值；position 只做类型、非负和已知 duration 校验。start 原子提交的 exact-pair membership 必须冻结到
terminal；断线只改变 online，相同 pair 重连只 resync，不支持 leave/add/remove、deviceSession 替换或
重新筛选。

**REQ-089 — Terminal delivery before ordinary business**
当未确认 restorePending pair 注册或重连时，服务端必须先把带 current deliveryId 的 terminal stop 或
restore 可靠加入当前 Socket 发送路径，才可开放 suspended Context 普通业务。enqueue 失败必须立即
断开，下次注册先 replay；不得先发送普通 command。enqueue/status 不清 gate，只有 matching terminal
applied+restoreCompleted feedback 可以清除。

**REQ-090 — Atomic recovery abandon and permanent decommission**
当管理端/调试 CLI abandon recovery 时，事务必须原子确认 terminal/restorePending、删除 full/compact
obligation 与 fence、释放 slot、写 abandoned 和 `(user,clientId,deviceSessionId)` permanent decommission
tombstone、撤销并断开在线 exact pair、从 device.list 移除且停止路由；失败全部回滚。r18 不提供
Flutter realtime abandon action。tombstone 只能随账号数据整体删除；资源到上限时限制新
deviceSession，不能删旧 tombstone，同 clientId 的新 deviceSessionId 可重新开始。
