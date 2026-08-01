# 阶段 3：已知边界与联调验收

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-07-23-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 9 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
## 9. 已知边界与联调验收

这是服务端与 Flutter 客户端共同遵守的规范，不证明当前任何一端已经完成实现。双方完成后必须验证：

1. probe 注册成功后客户端保存 profile、主动重连、第二次 negotiated 注册成功；
2. roles 单角色/双角色、完整 negotiatedCapabilities、Core not_supported 和 optional profile 降级；
3. 只有 major `2`、minor `>=8` 且除可选 schemaHash 外其余必需字段完整的固定注册 shape 可进入本文
   strict Core；低于 `2.8.0`、其他 major、缺少必需字段、非法 serverBuildCommit 或 actionless ACK
   均 fail-closed。schemaHash 缺失、变化、空值、类型或格式异常不得限制注册、连接、Core 或 capability；
4. 每个 strict push 的 nonce/epoch 完全匹配该 Socket 的 register ACK，业务 push 不带 requestId；
5. `device.list` 不含 Context binding；`playback.context.list` 的 direct response、controller/user
   授权、双字段精确匹配、空结果、单结果，以及多结果强制进入 `ambiguous_playback_scope`、不得
   人工或自动选择；
6. 固定 10 字段 probe 与 negotiated reconnect 都能注册；`device.setVolume` 可控制在线
   空闲或播放中的精确 client/device pair，错误 pair/离线/能力不足 fail-closed，实际值由
   event-confirmed `device.volume.update` 和扩展 `device.list.volumeState` 观测，且 Context 完全不变；
7. list -> subscribe -> status -> conditional queue sync / player control 的唯一结算、完整 canonical
   queue/playback/cursors、canonical push 和 cursor conflict；并发 ensure/handoff 不能使同 pair 变为
   多 Context，异常多 Context 在失效事件到达前也必须由 pair-level 原子检查拒绝，且 cursor 不变；
8. ensure 同 stable client 重试返回同一 Context、同 requestId 60 秒缓存重放、内容冲突、断线清
   subscription 和重连后读取服务端当前状态；
9. Cursor 矩阵每一行的递增、不递增、旧值拒绝、等值去重、必需 clientSeq 和六个 event-confirmed action 的精确单 Socket 重放；
10. authority clientId/deviceSessionId 双重绑定、同 pair 重连可重新发现、相同 clientId 不同
   deviceSessionId 不匹配旧 Context、旧 sid 强制断开，以及永久离线后 close + 新 Context 恢复；
11. Handoff 的 prepare -> ready -> effective-at commit -> complete -> authority binding 原子迁移 -> release，
   以及 8 秒/5 秒超时和 source/target 断线；
12. Handoff complete 后旧 device pair 的 list 不再返回该 Context、新 pair 返回同一 Context ID；close
   后 list 立即排除旧 ID，缓存旧 ID 的 status/control 返回 `context_closed`；
13. ensure 新建/重绑、close、handoff complete 分别向所有同用户 strict controller（包括非 subscriber）发送
   正确 pair 的 `playback.context.bindings.changed`；Flutter 收到匹配事件后立即暂停控制，并用新
   requestId 重新 list/subscribe/status；重复事件幂等，旧/新 pair 的 handoff 通知均覆盖；测试
   “旧 list response 后到、失效 event 先到”和相反顺序，旧 discoveryGeneration 的响应都不得生效；
   invalidation 无法可靠排队时目标 controller Socket 被断开并通过重连恢复；
14. Follow、Handoff 与 Broadcast 仅在 negotiated capability 为 true 且 profile ready 时测试，包括 owner/authority 权限、
    participant 只允许 status/自己的 feedback 而禁止控制、Handoff errorCode 和 terminal 幂等；
15. Broadcast 初始 participantStates 为 target revision + 当前 targetDeliveryId + 同值 deadlineBroadcastRevision + pending，省略全部
    实际 track/state/position/rate；
    applied/failed/timedOut/lagging 的条件字段严格按第 6.10 节成组出现；
16. transport message-too-big 断开、business limit bad_request、非法 requestId/action 断开，以及未知字段/null/rate 限额；
17. nonce CSPRNG/128-bit 熵，以及 queueSongIds 在序列化、持久化和重启后的顺序保持；
18. 单 worker 启动保护、Context/tombstone 重启恢复，以及 Handoff/Broadcast 显式终止；重启必须同时
    覆盖 active 与 waitingForSource Broadcast。Follow 不恢复音频，但必须加载 SafetyLease 为
    reconnectGrace/cleanupRequired 并恢复 suspended Context fence，等待 same-pair start/stop cleanup；
19. Android 选择 Windows 后发现唯一 queue-backed Context，读取并应用 status 的队列/索引/播放状态/cursors，
   `player.pause` 在 Windows 执行，`queue.playItem` 切换正确歌曲；
20. Windows 启动时已有恢复队列和当前歌曲，ensure 在没有服务端 Context 时直接创建 queue-backed
   snapshot，queue/index/track/state/position 与请求一致，不先产生空 Context；
21. Windows 无队列启动后 ensure 返回一个 idle Context；Android 按 client/device pair list 得到唯一
   binding，status 中 queueSongIds 为空、state idle、currentIndex/trackId 均省略；
22. 同 client/device 重复 ensure 返回同一 Context 且 cursor 不变；相同 stable clientId 以新
   deviceSession 重连时，旧 session 已离线后 ensure 重绑同一 Context ID，并只按矩阵递增 cursor；
23. 已有 canonical queue-backed Context 时，ensure 携带不同本地队列不得无版本覆盖；response 返回
   canonical，authority 只有使用最新 cursors 的显式 queue.context.sync 才能替换；
24. idle Context 上直接发送 player.play、pause、seek、next、prev 或 queue.playItem 均返回
   queue_required，cursor 和 authority 执行次数为 0；
25. Android 在 idle Context 点一次 play，prepare 只路由一次；Windows 恢复本地队列或采用初始队列，
   同一 Context 通过 queue sync 变成 paused，Android 使用最新 controlVersion 只发送一次 play；
26. 两端均无队列时 prepared 返回 queue_required；prepare 超时、authority/deviceSession 改变和重复
   intentId 均按第 6.2.3、4.4 节有界结算，不永久 loading、不延迟误播；
27. negotiated register 返回低于 `2.8.0`、其他 major，或合规连接上 Core action 返回
   `not_supported` / `capability_required` 时，Flutter fail-closed、清理远控状态且绝不 session fallback；
28. passive playback.update 只更新实际进度/状态/播放速度，controlVersion 和全部 Context cursor 不变；
     request、canonical push 与 DevicePlaybackState 均含合法 playbackRate，并同时带最新 controlVersion
     和 device appliedControlVersion；合法速度固定为 0.5..2.0，每个 update 还必须携带
     positionSampledAtServerMs，canonical push/DevicePlaybackState 保留该值而 serverUpdatedAtMs 记录接收时间；
     playing authority 至少每秒上报一次；
29. 手机远程命令 47 ACK 后事务为 pending，Windows committed update 将其结算为 committed，applied
   推进到 47，但 controlVersion 不再次递增；
30. 远程命令 47 失败时 commandControlVersion=47、appliedControlVersion 保持 46，事务进入 failed；
   主 Context 如已写入预期 track/index，使用更新的 Context version/Queue revision 对账回实际状态，
   controlVersion 仍为 47；
31. 服务端已接受 48、Windows 只执行到 47 时，status/deviceStates 和 playback.update 合法表达
   controlVersion=48/appliedControlVersion=47，实际旧 track 按 applied snapshot 校验而不是被误拒绝；
32. lastApplied 已为 48 后到达 remote 47 feedback 时不覆盖歌曲、状态和位置；applied 高于 canonical
   时返回 bad_request；
33. Windows observed 46、服务端 canonical 47 时，合法 localUser committed 获得 48，并只把仍为
   pending 的 47 及更低事务标记为 superseded；committed/failed 历史不回滚；
34. localUser 先获得 47 后，手机仍用 base 46 的命令返回 stale_version 且不执行；
35. 同一 local intentId 相同内容重试重放原 confirmation，不增加到 49；相同 intentId 不同内容返回
   conflict；epoch/authority/deviceSession 改变后旧 intent 不能应用；
36. 本地操作失败不产生 committed localUser update、不推进版本、不 supersede；
37. Windows 本地屏障保证已经送达但尚未完成的旧远程命令不会在 localUser confirmation 后晚执行；
   使用最新 48 的新远程命令 49 仍正常执行；
38. 首次连接前 Windows 已播放时，ensure 按实际 snapshot 创建版本 1，后续 passive update 仍为 applied
   1，不额外生成 localUser 版本 2；
39. 重连 status 恢复 canonical N 和 per-device applied M，不把 canonical N 误报为已执行；服务端
   restart 时 pending ordinary control 全部结算 execution_unknown，依赖后继结算 dependency_failed，
   再由 fresh actual fact 分配 reconciliation version，不重投旧 controlVersion；
40. 自然播完自动下一首不使用 origin localUser，不获得本地人工 supersede 权限；
    必须先用 queue.context.sync 更新唯一 Context，再发 passive update；
41. Android 与 Windows 各一台真实 Flutter 客户端联调，记录可复现日志；
42. `broadcast.start` 只接受 playbackContextId/intentId/participants；旧 queue/index/position/autoPlay
    字段返回 bad_request。source 未结算、没有当前连接 DevicePlaybackState、queue idle 或实际不是
    playing，或 serverUpdatedAtMs/positionSampledAtServerMs 任一超过 2000ms 时 fail-closed；成功
    snapshot 的 queue/index 来自 source Context，state/position/playbackRate 来自 source device，position 从
    sample time 投影。同 intent 跨重连重试返回原 broadcastId，不同 intent 在同一非终态 source 上
    conflict；
43. sourceAuthority 收到 start 只记录 lifecycle，不执行 queue apply/play/pause/seek/speed；同一 start
    到达 ordinary participant 时先完整持久化恢复记录，再暂停原任务并应用派生 snapshot；
    持久化失败保持原音频不变并报 restore_failed；controllerOnly 不操作音频；
44. 每个 broadcast.play/pause/seek/playItem 在 source Context 临界区使用 source base cursors，只生成
    一个普通 pending control transaction、只向 source 发送一个普通 Context command，并向 ordinary
    participants 发送同一 target；source command 与全部 mirror push 的 effectiveAtServerMs/serverTimeMs
    逐值相等且满足至少 250ms lead，snapshot source cursors 在非终态期间与 Context 相等，
    terminal 后冻结为历史值；每个计划 target 的 position 投影到 effective-at 且
    Snapshot.serverUpdatedAtMs=effectiveAtServerMs；
45. 客户端发送 broadcast.queue.sync 返回 not_supported；source authority 的 queue.context.sync 原子
    更新唯一 source Context 与 broadcastRevision，并只向 ordinary participants 生成完整
    broadcast.queue.sync mirror push；
46. source remote committed/failed、localUser 和 passive playback.update 分别按普通 Context 规则结算，
    再派生 Broadcast confirmation/correction；失败对账后 source Context 与 snapshot 不分叉，ordinary
    feedback 永不修改两者；
47. `broadcast.feedback` 使用 event-confirmed 唯一结算，且 source 发送 forbidden；applied 必须证明
    revision/deliveryId/queueIndex/trackId/state，failed 必须带 failed revision/last applied/errorCode。合法 request
    只收到同 Socket、无 requestId confirmation，clientSeq 重复/冲突按第 4.5 节处理，
    Context/source/broadcast cursors 不变；positionMs 只校验 int、非负和媒体范围并保存实际观测值，
    不按到达时间做精确位置比对，track/state/rate/revision/deliveryId 仍严格校验；
48. ordinary participant 进入 Broadcast 后，其 suspended Context/binding 的 queue、prepare、player、
    update、close、ensure create/init/rebind、全部 Handoff/ready/authority switch 和其他 binding mutation
    均返回 conflict 且无执行/无 mutation；已被其他 Broadcast 占用的 source/ordinary pair 不能再次
    start；active source 的 close/Handoff/binding mutation 同样 conflict，waitingForSource 继续保持屏障；
    terminal 后 restorePending pair 不能成为新 Broadcast/Handoff 任一角色；
49. ordinary participant 入口分别为 playing、paused、stopped、idle 时，terminal 后恢复完全相同的队列、索引、
    冻结位置和 playbackRate，并分别继续播放、保持暂停、保持 stopped、恢复 idle；恢复期间后来普通 Context command
    按 controlVersion 排队，playback.prepare 立即以 restore_in_progress 拒绝，binding 变化改为读取并
    应用服务端当前状态；恢复记录的 Context/applied cursor 只用于比较，不写回、不回退、
    不构造旧 base request，退出门控后才发送
    applied+restoreCompleted stopped feedback；失败反馈不清 restorePending，durable snapshot/outbox 从未出现群播队列；
50. source authority 结束群播后 source Context/cursors、队列、索引和实际 transport 不变；terminal
    只递增 broadcastRevision/lifecycleState，snapshot state 可继续为 playing，source 不发送
    broadcast.feedback；
51. source 断线进入 waitingForSource 时只发 broadcast.waiting、ordinary mirrors 暂停但 source Context 不变；相同 pair 新连接
    上报 fresh playing/paused/stopped 后只发 broadcast.resume 并按实际 state 恢复；不需要手工 broadcast.play，
    不同 deviceSession 不继承，30 秒超时后不可复活；
52. stop ACK、source/ordinary terminal push、重复 terminal 和 status 补偿交错时，每个 ordinary
    client/broadcastId 只 restore 一次并发送一次 stopped feedback；ordinary 离线时 tombstone/outbox
    完整保留 7 天，之后未确认 pair 原子压缩为 TerminalRecoveryRecord；相同 pair 重连先收到
    同 revision terminal/broadcast.restore 再收到普通 Context command，重复补发不增 revision；source 只清
    lifecycle，一切迟到 execution callback 被 generation/lease fence 丢弃；
53. participant restore 首次失败时保持暂停、保留快照并只读取和应用一次服务端当前 canonical 状态；失败路径不得
    保存群播队列。真实设备日志必须同时记录 source-derived start、source/ordinary 角色、source
    cursors、broadcastRevision、waiting/resume、terminal gate、恢复结果和 feedback revision/clientSeq；
54. source playing progress 合并但未发送时 broadcastRevision 不变；每次实际 broadcast.progress 严格 +1，多
    participant 共用同 revision，重复 delivery 不加号，同 revision 不同 Snapshot.position 被拒绝；
    新 revision 的 position/serverUpdatedAtMs 都锚定 effectiveAtServerMs，resync 的新投影只进 delivery ledger；
55. ordinary player 只有在能设置 0.5..2.0 全部合法 playbackRate 且协商
    effectiveAtPlayback:true 时才能成为 participant；能力为 false 的隐式目标被排除，显式目标进入
    skippedClientIds，且从未接收 start/mirror push；
56. Broadcast target 分发后 participantStates 带冻结 clientId/deviceSessionId pair、targetDeliveryId 并从 pending
    开始，deadlineBroadcastRevision 绑定最早未确认 target，后续进度不延期；8 秒内 applied
    进入 applied，低 revision 进入 lagging，failed 保存错误，未反馈进入 timedOut；后续成功可从
    failed/timedOut 收敛；同 pair 重连只向该 pair 发 broadcast.resync，以新 deliveryId 重置
    target/deadline base/pending 但不增 revision；有 effective-at 从 effective-at+8 秒计时，无值从本次
    delivery 创建时间+8 秒计时，绝不使用旧 Snapshot.serverUpdatedAtMs；旧 delivery feedback 无效，不同
    deviceSessionId 不继承；
57. system.pong 必含 serverTimeMs；player 注册后先取 3 样本，effectiveAtPlayback:true 连接始终最大
    10 秒 ping 间隔；样本门槛满足后才可分发 effective-at。effective-at 前执行、迟到不超过 1000ms
    追赶、超过时 source/ordinary
    分别通过 remoteCommand failed/broadcast failed feedback 报 effective_at_missed，时钟不确定度超过
    50ms 报 clock_unsynchronized；
58. start ACK 丢失并跨越 terminal 全量记录保留期后，相同 intentId 仍返回首次 broadcastId 或已终止
    outcome，不创建新 Broadcast；ACK outcome tombstone 直到 source Context close 才清理，每 Context
    达到 1024 个后新 intent 返回 rate_limited，旧 intent 继续重放。
59. active Broadcast 中 source 自然切到下一首时，先用一次 queue.context.sync 提交
    currentIndex/position，服务端推导 track 并更新 source Context，只派生一个新 broadcastRevision 和一次
    broadcast.queue.sync；passive update 不能越过 Context 单独切歌；
60. 服务端分别在 active 和 waitingForSource Broadcast 期间重启，两者都只产生一个 terminal
    revision，安装 restorePending 并在相同 pair 重连时补发，不遗留 source/participant 占用屏障。
61. active Broadcast 中 source 用 queue.context.sync 把队列清空时，一个原子事务先用最后
    非空 mirror target 生成 terminal revision/restorePending/outbox，再把 source Context 更新为 idle；
    ordinary 收到 terminal 而不是空队列 broadcast.queue.sync，terminal source cursors 不跟随
    后续 source mutation。
62. controllerOnly owner 在线时收到 start/play/pause/seek/playItem/queue.sync/progress/state.sync/
    waiting/resume/stop 观察副本，
    source 清空队列、30 秒断线超时和服务端重启的自动 terminal 都只清理群播 UI，它的
    自有播放任务不变、不发 feedback、不出现在 participantStates；owner 与 source/ordinary 重合时
    不重复投递。
63. source 与 ordinary player 刚注册但未完成 3 个 ping 时，broadcast.start 跳过该 ordinary
    或对 source 返回 conflict；3 个顺序 pong 后首次 start 正常执行，不因 30 秒普通 cadence
    报 clock_unsynchronized；
64. source 每秒生成一个进度 revision、participant 完全不反馈时，
    targetBroadcastRevision 持续增加但 deadlineBroadcastRevision/deadline 不变，首个 target 后 8 秒
    必须进入 timedOut；进入 applied/failed/timedOut 时 timer 停止但两个 deadline 字段保留最后值，
    合法 feedback 或带新 deliveryId 的物理重连补发才重建 deadline；
65. 人工注入 300ms 上行延迟时，serverUpdatedAtMs 比 positionSampledAtServerMs 晚约 300ms；
    Broadcast 位置从 sample time 投影，不把这 300ms 重复算作播放进度；未携带采样时间或
    采样/接收任一过期时 fail-closed；
66. ordinary 开始恢复后服务端 Context 收到更高 controlVersion/queueRevision，Flutter 只用冻结
    cursors 比较并应用服务端当前状态，不发旧 base request、不写回或回退任一 cursor；
67. terminal 满 7 天且 ordinary 仍离线时，先原子生成唯一 pair-level
    TerminalRecoveryRecord 再删除 full record；相同 pair 重连先收 broadcast.restore，status 返回
    recovery one-of，恢复成功后两者与 restorePending 同时清除；压缩中途失败时 full record
    仍完整可用；压缩后 owner/source 重复 stop 从 ACK outcome tombstone 重放原 stop ACK，不重建
    snapshot 或重发 recovery。
68. 用户已占满 256 个 recovery slots 时，新 Broadcast 目标全部进入 skippedClientIds 且 start
    返回 rate_limited；任一旧 pair 恢复确认释放 slot 后新 target 可加入。单 Context 已有 1024 个
    ACK outcome tombstones 时新 intent 被 rate_limited，但 1024 个旧 intent 均仍可重放原 outcome。
69. 部署把 supportsBroadcast 关闭后，新 start/control 均被拒绝，但已有 restorePending 的同一
    strict 2.8 pair 重连仍在普通 Context command 前收到 broadcast.stop/broadcast.restore，完成恢复并
    清除 recovery slot；当前 capability=false 不得使旧覆盖层永久占用。
70. ordinary pair 在 revision 14 后断线重连时，只有该 pair 收到 broadcast.resync；Snapshot 与 revision
    14 逐字段不变。active 的 playing/paused/stopped 都更新 deliveryId/effective-at，waitingForSource 可
    省略 effective-at；旧 deliveryId 的迟到 feedback 不关闭新 deadline，
    新 deliveryId applied 才完成该 attempt；其他在线 participants 不收到重复 target。
71. 对 source localUser/passive/remote correction 逐项改变 state、position、queue/index/track、rate，验证
    playing↔非 playing 为 play/pause、paused↔stopped 与 rate 为 state.sync、位置跳转为 seek、
    queue/index/track 为 queue.sync；已知 seek command 或 localUser 同 track/index 只改位置才是 seek，
    passive 正常 playing 推进才是 progress，不使用位置差值阈值；断线/重连为
    waiting/resume，多字段提交按优先级只发一个 action 和一个 revision。等值 remote committed 不重复 push。
72. 同一 playing revision 连续查询两次 broadcast.status，两个 broadcast object 逐字段相同且 revision
    不变，只有 response-level serverTimeMs 前进；客户端使用 max(0, serverTimeMs-serverUpdatedAtMs)
    投影并限制媒体时长，future anchor 不倒退。paused/waiting/terminal 查询不推进位置。
73. terminal Snapshot.lifecycleState=stopped 而 Snapshot.state 保留最后 anchor；ordinary feedback.state
    固定 stopped 并可与 Snapshot.state 不同。入口 queue-backed stopped 的 participant 恢复相同
    queue/index/position/rate，保持 stopped 且没有自动 play/pause 调用。
74. restorePending 期间发送 playback.context.ensure，得到同 requestId 的 restore_in_progress 和三个当前
    cursors；Context/binding/cursor/push 均不变，相同 requestId 重放相同错误。terminal applied 清 gate 后，
    新 requestId ensure 才可正常结算。
75. source 缺少 player、playbackContextV2、supportsBroadcast、effectiveAtPlayback、canPlay/canPause/
    canSeek、全速率能力、时钟门禁、在线 authority binding 或 fresh settled playing 任一条件时 start
    fail-closed；playbackPrepare/音量能力缺失不影响合格 source。
76. 显式 ordinary participants 去除 source 后为 21 个时 start 返回 bad_request 且不创建 Broadcast；
    省略列表时只取按 clientId 排序的前 20 个，其余进入 skippedClientIds，source/controllerOnly 不计数。
77. 反馈 retained ledger 已清理、保留范围内 ledger 缺失、revision 超前三种情况分别收到
    revision_expired/revision_unknown/revision_ahead 的 broadcast.feedback.rejected；状态、deadline 和
    cursors 不变，被拒绝 clientSeq 不能复用。随后非终态收到新 deliveryId resync、完整 terminal 收到
    新 deliveryId stop、compact terminal 收到新 deliveryId restore；单独 status 不能代替该执行 delivery。
78. ensure direct response、status.playbackContext、queue.context.sync 和所有复用 Context snapshot 的
    消息都同时含 authorityClientId/authorityDeviceSessionId，且不含 playbackRate；DevicePlaybackState
    仍保留实际 rate。
79. stale_version、queue_required、restore_in_progress、Context/Handoff/Broadcast/Follow fence conflict
    和 context_closed 都携带真正阻止操作的 playbackContextId 与 currentEpoch/version/queue/control；
    user-scoped lookup 对跨用户与不存在返回相同结果，Handoff/volume target 不产生存在性侧信道；
    device.list.volumeState 只对 negotiated remoteVolumeControl=true 的请求连接输出。
80. Core playback.context.prepare 在 playbackPrepare=false、Handoff profile 关闭时仍正常 ACK/route/settle；
    authority exact pair、player/canPlay 或 base/intents 不满足时零副作用拒绝。
81. 普通 routed control 全部带 executionTimeoutMs；存在 pending lower track-changing transaction 时按最高
    version 写 dependsOnControlVersion，传递依赖等待 canonical committed 后才执行，等待时间不消耗
    execution timeout，effective-at 后迟到超过 1000ms 结算 effective_at_missed。
82. dependency failure 按直接依赖和 controlVersion 顺序递归生成 dependency_failed；authority
    disconnect、Socket replacement、restart 与 watchdog 只生成 execution_unknown。settled payload 含
    requesting exact pair，并只发给仍匹配的原请求 Socket、当前 subscribers 与原 routed authority，按
    sid 去重；replacement requester 不获历史 settlement。
83. remote failed、execution_unknown 与 dependency_failed terminal gap 在 fresh actual fact 后各分配
    一个 internal reconciliation R；旧 terminal 不变，Context version/必要 queueRevision 与 applied
    推进到 R，Context 不含 playbackRate，inline failed 或 passive 路径都只有一份 canonical
    confirmation，Follow/Broadcast 各只消费一次。
84. close 缺少 expectedEpoch/baseVersion 为 bad_request，stale 返回四 cursor，非终态 Handoff 与所有
    overlay/recovery fence 零副作用拒绝；首次 close 保存 closedFrom/final cursors，匹配重试重放 ACK，
    不匹配 tombstone 返回 context_closed 且不递增。第一首 prev、最后一首 next 与最后一首自然结束按
    distinct/no-repeat 边界矩阵执行，重复 terminal fact 不再次推进或派生。
85. supportsFollow 只有在 player/playbackContextV2/effectiveAt/canPlay/canPause/canSeek/全速率能力同时
    满足时协商 true；任一开启 Follow/Handoff/Broadcast 的 player 路径都完成 clock warm-up，固定十字段
    capability shape 不增加 rate 字段。
86. Follow source fact 必须来自 authority exact pair 当前 nonce/epoch且 applied==control；playing 的两个
    时间都 fresh，paused/stopped 不因无进度 heartbeat stale，idle start 返回 queue_required，self-follow
    返回 conflict，全部失败零 relationship/fence 副作用。
87. Flutter 在 follow.start 前验证 suspended Context/command lane/lease/overlay 并先持久化 acquiring
    RecoveryRecord；服务端 start ACK 返回完整 frozen exact pair/cursors/applied baseline。逐字段匹配才
    进入 audio overlay；baseline mismatch、prewrite 失败、ACK 前后 crash 与 active-record 写失败都只走
    stop/stopPending cleanup，不触碰或误恢复镜像音频。
88. FollowSafetyLease 在服务端重启后恢复 suspended Context fence；active/reconnectGrace/
    cleanupRequired 都阻止 player/queue/prepare/update/close/ensure mutation、Handoff/Broadcast/另一个
    Follow。每 pair 一条、每 Context 一个 overlay、每 user 256 条上限均 fail-closed，已有 start replay
    和 stop/cleanup 不受上限影响。
89. Follow overlay 禁用 play/pause/seek/next/prev/queue mutation，仅允许本机 volume 与 stop；mirror 不
    发送 playback.update/follow.feedback、不写 source 或 suspended Context。source 转 idle 时清空 mirror
    audio但保持 relationship/fence，重新 queue-backed 后继续同一 relationship。
90. mirror queue load、seek、rate、play/pause 与媒体 apply 分别在触碰音频前后注入失败；前者保持原
    任务并 stop，后者先恢复原任务再 stop。恢复失败保持非播放、RecoveryRecord/SafetyLease/fence，
    不发送 stop，只做有界重试或断线。
91. follow.stop 恢复覆盖 frozen binding/cursors 未变、任一 cursor 更高、binding 改变、Context closed
    四分支；只有完全相同才采用原快照，其他分支采用服务端当前状态，不写回旧 queue/cursor。恢复后
    stop ACK 原子释放 fence，最后清本地 record，之前不发送 normal playback.update。
92. source authority 离线、playing fact stale 或 status 恢复失败进入独立 30 秒 source recovery，source
    closed 立即退出；follower disconnect 进入 reconnectGrace，同进程 same pair 可重发 start，grace
    到期转 cleanupRequired tombstone，旧 pair 下次注册不能直接写 suspended Context。
93. stop ACK 丢失只重试 stop；app restart 不自动恢复 Follow audio，只读取 RecoveryRecord cleanup；
    server restart 不自动恢复 mirror，只加载 SafetyLease/fence。相同 pair start/stop 完成前普通写保持
    fail-closed。
94. 同一正常 source Context 同时拥有多个 Follow follower 和一个 Broadcast source时两个 profile 都
    正常派生同一 canonical fact；同一设备尝试 Follow follower、Broadcast ordinary、Handoff target
    任意两种 overlay 并发时第二个入口被拒绝，且两侧 recovery/fence 不互相删除。
