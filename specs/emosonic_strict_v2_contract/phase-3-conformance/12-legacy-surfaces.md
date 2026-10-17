# 阶段 3：非 strict-v2 旧 Surface

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-08-01-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 8 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
## 8. 当前不应实现为 strict-v2 的旧 surface

| 禁止作为 strict 方案 | 原因 |
| --- | --- |
| `session.subscribe` / `session.unsubscribe` | strict 改用 `playback.context.subscribe` / `unsubscribe`。 |
| `queue.session.sync` / `queue.local.set` / `queue.ready.complete` | strict queue 是 `queue.context.sync`，旧队列消息会被 router quarantine。 |
| `sessionId`、`sourceSessionId` | strict playback 主键只能是 `playbackContextId`；设备稳定身份是 `deviceSessionId`。 |
| 服务端业务 push / direct response 的 target 字段 | strict router 拒绝，服务端应按 Socket recipient 分发。客户端请求例外只有 `playback.handoff.start.targetClientId/targetDeviceSessionId` 与 `device.setVolume` 的精确 target pair。 |
| `player.setVolume` / `player.requestState` | strict 音量是设备级 `device.setVolume`；不要复用 legacy player action。`player.requestState` 仍未纳入。 |
| `auth.login` / `device.register` actionless ACK | probe/negotiated client 会拒绝。 |

`canSetVolume` 只表示 player 能执行本地音量设置；`remoteVolumeControl` 表示连接理解 strict-v2 `2.8.0`
设备级 action。目标 player 必须两者都为 true；controller 自身可以 `canSetVolume:false`。
