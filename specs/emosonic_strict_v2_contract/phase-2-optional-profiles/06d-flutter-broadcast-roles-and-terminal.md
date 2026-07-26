# 阶段 2：Flutter Broadcast 角色与 Terminal

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-07-23-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 5.5.3 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
#### 5.5.3 Flutter 本地执行角色与 terminal 语义

Flutter 必须按下列条件区分本机角色：

1. `authorityClientId` / `authorityDeviceSessionId` 等于 self 当前本地 authority binding：
   `sourceAuthority`；
2. ordinary participants 中存在 self 的冻结 client/device pair：`ordinaryParticipant`；
3. 其他 owner/controller：`controllerOnly`。

sourceAuthority 收到 `broadcast.start` 时只能记录 lifecycle/broadcastId/intentId 并开始正常 source 状态上报；
不得重新 apply queue、seek 到 snapshot position、设置 playbackRate、调用 play/pause 或创建恢复快照。
后续 source 音频变化只由普通 Context command、本地人工操作或播放器自然事件驱动，Broadcast mirror
push 对 source 永远是 lifecycle/UI 信息，不是第二条音频执行通道。

ordinaryParticipant 首次接受 start 时，必须在应用镜像前捕获一次原 Context binding、队列顺序、
currentIndex、positionMs、playing/paused/stopped/idle、playbackRate，以及原 Context 的
`epoch/version/queueRevision/controlVersion`、本设备 `appliedControlVersion` 和 authority client/device
pair。客户端必须先把完整恢复记录持久化并确认写入成功，然后才能隔离原任务（入口 playing 才执行
pause；paused/stopped/idle 不重复执行 transport command）、
获取镜像 execution generation 并应用镜像。持久化失败时不得触碰原音频，必须对 start revision
按第 5.5.2 节发送完整 failed `broadcast.feedback`（`errorCode:"restore_failed"`）。重复 start 不得覆盖快照；
入口 `stopped` 必须是保留非空 queue/index/position/rate 的 queue-backed 原任务；进入镜像前不对该
原任务再次调用 pause/stop。`idle` 才表示无队列空任务。
群播期间
原任务位置和上述 cursor 基线冻结，镜像状态不得进入原 Context
outbox、普通 playback.update、durable snapshot 或 localUser。waitingForSource 只暂停镜像并保留恢复
快照，不得提前恢复原任务；服务端自动恢复 active 后按新 snapshot 继续镜像。
冻结的 epoch/version/queueRevision/controlVersion/appliedControlVersion 只是恢复基线与版本比较依据，
客户端不得把它们写回服务端、不得使服务端 cursor 回退，也不得用冻结 cursor 构造
旧 base request。terminal 后读取到任一更高 Context/applied cursor 时，必须放弃旧快照中对应域的写入，
应用服务端当前状态。

ordinaryParticipant 首次应用 terminal stop 时，必须失效镜像 execution、丢弃迟到 callback，并恢复
入口队列、索引、冻结位置和 playbackRate：入口 playing 则恢复后继续播放，paused 保持暂停，idle
恢复 idle，stopped 则恢复 queue/index/position/rate 并严格保持 stopped，不自动 play，也不得降格为
paused。恢复失败时保持非播放（入口 stopped 继续保持 stopped）、保留快照并最多读取一次原 Context 的服务端当前状态，再应用服务端
返回的状态；不得持久化群播队列。

从接受 terminal push 到原任务恢复完成，Flutter 必须进入仅本地的 `restoringOriginalContext` 门控；
这是本契约对“服务端已经释放屏障、客户端仍在异步恢复”竞态的唯一解决方案。门控期间：

1. 所有发给 suspendedPlaybackContextId 的普通 `player.*` / `queue.playItem` command 必须按
   `(epoch, controlVersion)` 排队，不得与恢复队列并发执行；本地人工控制也必须排队或暂时禁用，
   Flutter 自身不得在门控期间发起 ensure、close、prepare、Handoff 或其他 binding mutation；
   若缺陷或竞态仍发送 ensure，服务端必须按第 5.5.2/6.2 节以 `restore_in_progress` 结算，Flutter 不得
   把该错误当成 Context 不存在或改用新本地快照重试；
2. 门控期间收到 `playback.prepare` 时不得预加载或延后到超时，必须立即发送
   `playback.ready(ready:false,errorCode:"restore_in_progress")`；不得进入 Handoff commit；
3. Flutter 不得发送由恢复动作产生的 localUser/passive `playback.update`，也不得用冻结快照覆盖已经
   收到的更新版本服务端状态；
4. 如果收到 `playback.context.closed`、`bindings.changed`、Handoff completed 或其他 authority/binding
   变化，必须取消冻结快照写入，读取服务端当前状态并应用服务端返回的状态；
5. 原任务恢复成功后，按 controlVersion 顺序执行仍适用的排队命令；发现版本缺口、Context 已变化或
   命令无法安全重放时，只读取一次服务端当前 status 并应用其状态，不猜测缺失命令；
6. 只有恢复完成且排队命令已执行或由最新服务端状态吸收后，才能退出门控并对 terminal revision
   按第 5.5.2 节发送单次完整 applied `broadcast.feedback`（`state:"stopped"`、
   `restoreCompleted:true`）。服务端接受该 feedback 时原子
   清除此 pair 的 restorePending；在此之前该 pair 不得成为新 Broadcast source/participant 或 Handoff
   source/target。恢复失败时保持门控和非播放状态（入口 stopped 继续保持 stopped），发送 failed feedback 但不清除 restorePending，由 terminal
   replay 在后续重连继续恢复。

sourceAuthority 的 terminal stop 只清除 lifecycle 标记，不调用 pause/stop/seek/queue apply，不发送
`broadcast.feedback`；源任务 playing 就连续播放，paused/stopped 就保持实际状态。controllerOnly 不
操作音频；其收到的任何 Broadcast start/play/pause/seek/playItem/queue.sync/progress/state.sync/
waiting/resume/stop push 都只更新
lifecycle/UI，不得 apply queue、play、pause、seek 或 playbackRate。stop ACK 不能作为第二次 terminal
命令；Flutter 必须按 broadcastId 用同一个一次性 gate
合并 terminal push、重复 stop 和有界 status 补偿。ACK 后 2 秒仍无 terminal push 时允许以新
requestId 查询一次 status；每个 ordinary participant 最多 restore 一次并发送一次 stopped feedback。
sourceAuthority 或 controllerOnly 在重启后对已持久化 broadcastId 查询 status 得到 `not_found`
时，只能清除本地 Broadcast lifecycle/UI 记录，不得改变 source Context 或 transport。
