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
- `playback.update` 使用 `origin` 区分 passive、remoteCommand 和 localUser。被动事实与远程执行结果
  不推进 controlVersion；Windows 本地人工操作完成后发送 localUser update，由服务端从当前
  canonical 值分配新版本，并只覆盖尚未 committed 的旧远程控制；
- 服务端必须同时保存最新接受的 `controlVersion` 与 authority 实际执行到的
  `appliedControlVersion`。远程 ACK 只证明 accepted/routed，只有 remoteCommand committed
  `playback.update` 才证明电脑已经执行成功；
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
