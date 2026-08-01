# 阶段 0：协议总览

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-07-23-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 1 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
## 1. 一次性结论

服务端应把 strict-v2 `2.8.0` 实现为一套以 `playbackContextId` 为唯一播放任务主键的 Socket.IO 协议：

- 传输使用 Socket.IO namespace `/emo`、事件名 `message`；
- 所有 `system.ack` / `system.error` 必须以同一 `requestId` 关联，且 `payload.action` 必须回显原 action；direct action response 使用第 2.2、4.3 节规定的同 requestId 关联方式；
- strict 注册成功后，所有发给 strict recipient 的入站 envelope 必须由统一发送工厂附加该 Socket 的顶层 `connectionNonce` 和 `connectionEpoch`；
- strict-v2 业务 payload 中不得出现 `sessionId`。客户端请求不得使用顶层 `targetClientId`；payload target 只允许 `playback.handoff.start.targetClientId`，以及 `device.setVolume` 的 `targetClientId` / `targetDeviceSessionId`。服务端业务推送的顶层和 payload 均不得带 target 字段；
- `device.list` 只发现设备；从目标 `authorityClientId` / `authorityDeviceSessionId` 到 active Context
  binding 的映射只能通过 `playback.context.list` 查询，客户端不得从任何 session/device ID 推导
  Context。多结果必须 fail-closed；binding 集合变化由 `playback.context.bindings.changed` 使客户端
  缓存失效；
- 每个完成注册且 `player + canPlay` 的设备必须先读取可用的本地恢复快照，再立即调用
  `playback.context.ensure`，并在在线期间保持一个唯一 active Context；唯一例外是第 5.5 节已有
  Broadcast 恢复记录的 ordinary pair，它必须先完成 active/terminal replay，不能用 ensure 穿透屏障。
  本地已有队列时 ensure 必须
  携带队列、当前 index、播放状态和位置；只有本地确实没有队列时才发送
  `queueSongIds:[]`、`state:"idle"`、`positionMs:0` 并省略 `currentIndex`；
- controller 在 idle Context 上不能直接发送普通 player control；一次播放点击先发送
  `playback.context.prepare`，Context 变为非空后再使用最新 controlVersion 发送一次 `player.play`；
- `playback.context.prepare` 是 Core 行为，只要求 controller、当前 Context 与 authority player 的
  Core 条件；它不依赖 Handoff profile，也不检查 target-only capability `playbackPrepare`；
- 所有 active Context snapshot 必须同时携带 `authorityClientId` 与
  `authorityDeviceSessionId`。Context-scoped stale/queue/fence/closed error 必须携带
  `playbackContextId` 和 `currentEpoch/currentVersion/currentQueueRevision/currentControlVersion`；
- `playback.update` 使用 `origin` 区分 passive、remoteCommand 和 localUser。被动事实与远程执行结果
  不推进 controlVersion；Windows 本地人工操作完成后发送 localUser update，由服务端从当前
  canonical 值分配新版本，并只覆盖尚未 committed 的旧远程控制；
- 服务端必须同时保存最新接受的 `controlVersion` 与 authority 实际执行到的
  `appliedControlVersion`。远程 ACK 只证明 accepted/routed，只有 remoteCommand committed
  `playback.update` 才证明电脑已经执行成功；
- 每个普通 routed control 都携带 `executionTimeoutMs`，并在需要时携带确定性分配的
  `dependsOnControlVersion`。依赖成功后才开始执行租约与 watchdog；依赖失败必须传递结算为
  `dependency_failed`，断线、Socket replacement、重启或 watchdog 无法证明结果时只能结算为
  `execution_unknown`；
- failed/unknown/dependency terminal gap 不得伪造旧命令成功；服务端必须从当前
  `controlVersion` 分配新的内部 reconciliation version，把 canonical Context 与 fresh actual fact
  收敛，并只发送一次对应 wire confirmation；
- `queueSongIds` 必须 distinct，且 r18 不支持 shuffle、repeat-one 或 repeat-all。第一首 `prev`
  重播第一首；最后一首 `next` 和最后一首自然结束都停在最后 index、position 0，后者只允许通过
  passive automatic terminal 例外推进一次 Context version；
- Follow 是 Context 驱动的一对一软同步音频 overlay：follower 必须在 start 前持久化原任务与 frozen
  baseline，服务端以 persistent FollowSafetyLease 冻结 suspended Context；start ACK baseline 完全匹配
  后才能触碰音频。Follow mirror 不发送 playback.update 或独立 feedback，退出时按服务端当前 cursor
  比较安全恢复；
- 同一正常 source Context 可以同时服务多个 Follow follower 并作为 Broadcast source，但同一设备的
  Follow follower、Broadcast ordinary participant 与 Handoff target execution overlay 互斥；
- Broadcast 对普通 participant 是临时音频覆盖层：进入时冻结其原本地任务，terminal stop 后恢复
  原队列、索引、位置、速度和 playing/paused/stopped/idle 状态；source authority 的 Broadcast Context 就是
  自己正在执行并向其他设备复制的播放任务，结束时只解除复制关系，不 pause、不 seek、不切换或
  恢复 Context；源任务当时 playing 就继续播放，当时 paused 就保持暂停。source 继续使用普通
  `playback.update`，BroadcastSnapshot 只派生 source cursors；只有 ordinary participants 使用
  `broadcast.feedback`，且镜像执行不得污染其原 PlaybackContext。start 时 source 实际状态必须是
  playing；start intent、terminal 补发与客户端恢复门控必须跨断线保持；
- 本契约要求 `protocolVersion` 的 major 为 `2` 且 minor 至少为 `8`。待机 Context、
  `playback.context.ensure`、`playback.context.prepare`、Context discovery 和设备级远程音量全部属于
  Core，不设置旧版本兼容 capability。低于 `2.8.0` 或 major 不为 `2` 时 Flutter 必须 fail-closed；
  `schemaHash` 和 `serverBuildCommit` 是部署观测值，不是要求客户端每次打包固定的 pin；
- Follow、Broadcast、Handoff 受注册时返回的 `negotiatedCapabilities` 控制；服务器不得直接信任客户端请求值，也不应向未协商成功的连接投递可选动作。

必须区分字段方向：客户端请求中的 `baseControlVersion` / `baseQueueRevision` 是并发前置条件；
服务端推送中的 `controlVersion` / `queueRevision` 是接受后的 canonical cursor。服务端推送不带
`baseControlVersion`，不代表服务端可以从 `2.8.x` 请求契约中删除该字段。

`player.setVolume`、`player.requestState`、`session.subscribe`、`queue.session.sync`、`queue.local.set`、`queue.ready.complete` 不属于当前 strict-v2 可用 surface。远程音量必须使用本文定义的设备级 `device.setVolume`，不要复用 legacy player action。
