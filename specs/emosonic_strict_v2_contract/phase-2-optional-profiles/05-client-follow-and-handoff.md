# 阶段 2：客户端 Follow 与 Handoff 请求

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-07-23-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 5.3—5.4 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
### 5.3 Follow（仅 negotiated capability `supportsFollow:true`）

Follow 是可选 profile。只有连接协商到 `supportsFollow:true`、角色包含 player、
`canPlay:true`，且服务端该 profile 的全部 schema、权限、清理和 conformance tests 已通过，
服务端才可接受或推送 Follow；否则请求返回 `capability_required`。

| Action / type | payload | 服务端动作与响应 |
| --- | --- | --- |
| `follow.start` / `command` | `sourcePlaybackContextId:R`、`deviceSessionId:R` | 当前 authenticated client/device 建立对 source context 的唯一 Follow subscription；要求其有读取权限。相同 source 重复 start 幂等 ACK；已 Follow 另一 source 时先返回 `conflict`，不得隐式切换。成功只返回 action ACK，客户端随后显式请求 source status。 |
| `follow.stop` / `command` | `sourcePlaybackContextId:R` | 只有创建该 Follow 的 client/device 可停止；重复 stop 幂等 ACK。 |

Follow ownership 绑定 `(authenticated user, clientId, deviceSessionId, sourcePlaybackContextId)`。
Socket 断开时清除临时 Follow subscription；Context close 时服务端必须终止其全部 Follow，并向仍
在线的 follower 推送 `playback.context.closed`。Follow 不授予控制、handoff 或 broadcast 权限。

### 5.4 Handoff（target 需 negotiated `playbackPrepare:true` 且 `effectiveAtPlayback:true`）

Handoff 发起者必须具有 controller 角色；source 必须是当前在线 authority 且
negotiated `canPause:true`；target 必须是同用户在线 player，并协商到 `playbackPrepare:true`、
`effectiveAtPlayback:true`、`canPlay:true`。只有服务端 Handoff conformance tests 已通过时才
开放该 profile，否则 negotiated handoff capabilities 为 false，请求返回 `capability_required`。

`playback.handoff.start.payload.targetClientId` 是 Context/Handoff surface 的 payload target 例外；
另一个设备级例外是第 5.2 节 `device.setVolume` 的精确 client/device pair。handoff target 只用于让服务端选择接管设备；服务端必须解析并授权该目标，然后按目标
Socket 投递无 `targetClientId` 的 `playback.prepare`。不得把该请求字段复制进任何业务 push。

| Action / type | payload | 服务端动作与响应 |
| --- | --- | --- |
| `playback.handoff.start` / `command` | `playbackContextId:R`、`targetClientId:R`、`baseControlVersion:R int>=0` | 原子创建 handoff 前检查 target 的唯一 Context：没有 Context 可继续；只有 idle Context 时记录为待退休 standby；非 idle Context 或 active prepare 返回 conflict。成功后 ACK 并给 target 发第 6.8 节 prepare；ACK payload **只允许且必须**有 `action`、`handoffId`、`prepareId`、`status:"preparing"`、`controlVersion`。 |
| `playback.ready` / `event` | `playbackContextId:R`、`prepareId:R`、`handoffId:O`、`ready:R bool`、`errorCode:O`、`errorMessage:O` | target 预加载结果，不回 ACK。`ready:true` 时禁止 error 字段；`ready:false` 时 `errorCode:R`、`errorMessage:O`。成功进入 `ready`，失败进入 `failed`。 |
| `playback.handoff.complete` / `event` | `playbackContextId:R`、`handoffId:R`、`positionMs:O int>=0` | target commit 后确认，不回 ACK。服务端在这里原子切换 authority/cursors，广播 completed status 和 context status，再向 source 发 release。 |
| `playback.handoff.cancel` / `command` | `playbackContextId:R`、`handoffId:R`、`reason:O non-empty string` | 取消 idempotent；ACK 并向相关成员发 `playback.handoff.cancel` / `status`。 |

Handoff 状态机固定为：

```text
preparing -> ready -> committing -> completed
     |         |          |
     +---------+----------+-> failed | cancelled | timedOut
```

- `preparing` 从 start ACK 起最多 8 秒；未收到有效 ready 时进入 `timedOut`。
- `ready` 后服务端必须给 target 发送含正整数 `effectiveAtServerMs` 的 commit，并进入
  `committing`；`effectiveAtServerMs - serverTimeMs >= 250`。
- `committing` 最多 5 秒；未收到 complete 时进入 `timedOut`，authority 不变。
- target 在 complete 前断开：`failed`；source 在 complete 前断开：`cancelled`；两者都不得
  切 authority。complete 的原子事务才是 authority switch point。
- target 在 start 时拥有 idle standby Context 时，complete 原子事务必须先把 standby 写入 terminal
  tombstone，再把 source Context 绑定到 target；两步必须同成同败，并为 standby close、旧 source pair
  和新 target pair 发送对应 invalidation。target Context 已非 idle 或存在 active prepare 时不得进入
  authority switch。
- `completed`、`failed`、`cancelled`、`timedOut` 是终态。终态重放不产生副作用。
- `playback.handoff.status` / `cancel` 发给全部当前 Context subscribers；prepare 与 commit 只发
  target，release 只发 completed 后的旧 authority。complete 后再广播新的 Context status。
- authority 永久离线时，2.8.x 不提供强制接管。controller 关闭旧 Context，目标 player 继续使用自己
  ensure 得到的唯一 Context；旧 ID 因 tombstone 不可复用。

Handoff `errorCode` 必须匹配 `^[a-z][a-z0-9_]{0,63}$`。服务端标准值固定为
`prepare_failed`、`prepare_timeout`、`commit_timeout`、`target_disconnected`、
`source_disconnected`、`server_restart`。target 可在 `playback.ready.ready:false` 中返回符合相同
格式的稳定扩展码。`errorMessage` 不得包含凭据、文件路径、堆栈或内部数据库信息。

Handoff start、ready、complete、cancel 的每次状态推进都必须先检查第 5.5 节 Broadcast 写屏障：source
Context 正在作为非终态 Broadcast source，或 source/target pair 的 Context 正在作为 ordinary
participant suspended Context，或任一 source/target pair 存在 restorePending 时返回 `conflict`；不得
准备、切 authority、退休 standby 或发送 release。
