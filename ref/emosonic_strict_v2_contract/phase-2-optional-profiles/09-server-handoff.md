# 阶段 2：服务端 Handoff 消息

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-07-23-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 6.8—6.9 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
### 6.8 Handoff prepare

服务端以 context membership 选出 target Socket 后，发送无 target 的：

```json
{
  "type": "command",
  "action": "playback.prepare",
  "connectionNonce": "<target nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "handoffId": "handoff-1",
    "prepareId": "prepare-1",
    "sourceClientId": "phone-1",
    "authorityClientId": "phone-1",
    "deviceSessionId": "device:desktop-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "trackId": "song-1",
    "positionMs": 1200,
    "controlVersion": 13
  }
}
```

prepare payload 必需且只允许：`playbackContextId`、`handoffId`、`prepareId`、
`sourceClientId`、`authorityClientId`、`deviceSessionId`、非空 distinct `queueSongIds`、合法
`currentIndex`、`positionMs`、正整数 `controlVersion`。`trackId`、`timelineId` 可选；若有
`trackId` 必须等于当前队列项。prepare 中禁止 `effectiveAtServerMs`，该字段只属于 commit。

### 6.9 Handoff commit、release、status、cancel

commit 使用 `player.play` 无 target，且包含 handoff 字段：

```json
{
  "type": "command",
  "action": "player.play",
  "connectionNonce": "<target nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "handoffId": "handoff-1",
    "controlVersion": 14,
    "sourceClientId": "phone-1",
    "effectiveAtServerMs": 1780000005000,
    "positionMs": 1200
  }
}
```

commit payload 必需且只允许：`playbackContextId`、`handoffId`、正整数 `controlVersion`、
`sourceClientId`、正整数 `effectiveAtServerMs`、`positionMs`。服务端发 commit 时进入
`committing`，但此时 authority 仍是 source。

随后相关成员收到的 schema 固定为：

| action | 必需字段 | 条件可选字段 | 禁止规则 |
| --- | --- | --- | --- |
| `playback.handoff.release` | `playbackContextId`、`handoffId`、`instruction:"pause"`、`controlVersion`、`newAuthorityClientId` | 无 | 只在 completed 后发给旧 authority |
| `playback.handoff.status` | `playbackContextId`、`handoffId`、`status`、`controlVersion` | `sourceClientId`；completed 时 `newAuthorityClientId:R`；failed/timedOut 时 `errorCode:R`、`errorMessage:O` | `status` 只能是第 5.4 节枚举 |
| `playback.handoff.cancel` | `playbackContextId`、`handoffId`、`reason`、`controlVersion` | `errorCode`、`errorMessage` | 只对应 cancelled/timedOut/failed，不得用于 completed |

这些 strict handoff push 都禁止 target / session，并广播给全部 Context subscribers（release 除外，
它只发旧 authority）。服务端切 authority 后必须立即发 `playback.context.status`，令所有客户端
收敛到新的 `authorityClientId` 与 epoch/cursor。
