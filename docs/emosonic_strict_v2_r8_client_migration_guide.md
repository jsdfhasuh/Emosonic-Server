# EmoSonic strict-v2 2.3.0 / r8 客户端改造说明

本文面向 Android、Windows、Flutter 和其他 strict-v2 客户端工程师，说明服务端从
strict-v2 `2.2.0` / contract r7 升级到 `2.3.0` / contract r8 后，客户端需要新增或调整的行为。

权威 wire 定义以
[`specs/emosonic_strict_v2_socketio_server_contract.md`](../specs/emosonic_strict_v2_socketio_server_contract.md)
为准。本文是迁移和联调指南，不替代权威契约。

## 1. 交付标识

- 协议版本：`2.3.0`
- 契约版本：r8
- 契约 SHA-256：`5269b53a615ca97f820d3624510acb459e3d3031667a742bd50d1185af8d1e37`
- 当前 registration schemaHash：`77684c5689082b75eb2ac3daed06821cebdcae7c25536d0c977700c3bdd3d68e`
- 服务端实现提交：`21cd514`（`Add strict-v2 device volume control`）

`schemaHash` 和 `serverBuildCommit` 是连接时的部署观测值。客户端必须校验格式和同一物理连接内
的一致性，但不要把上面的具体值永久硬编码为唯一允许值。

## 2. 本次服务端改动概览

r8 新增“设备级远程音量”，用于控制同用户的在线 player，包括当前没有播放、没有创建
PlaybackContext 的设备。

新增内容：

1. controller 到服务端的新命令：`device.setVolume`；
2. player 到服务端的新实际状态反馈：`device.volume.update`；
3. 新的可选 capability：`remoteVolumeControl`；
4. `device.list` 设备对象新增可选 `volumeState`；
5. 服务端保存在线设备的瞬态音量状态，并在设备断线、被替换或超时移除时清除；
6. `device.setVolume` 纳入 strict control rate limit；
7. Web controller 使用 150ms debounce、单个 in-flight 请求和 latest-wins，客户端可采用相同策略。

没有发生的事情：

- 没有把设备音量放进 PlaybackContext；
- 没有新增数据库字段或迁移；
- 没有离线命令队列；
- 没有在设备重连后自动执行旧音量命令；
- 没有重新开放 legacy `player.setVolume`。

## 3. 核心资源模型

设备音量属于在线设备，不属于播放任务：

```text
Online device (clientId + deviceSessionId)
  └── volumeState（在线瞬态状态）

PlaybackContext
  ├── queue
  ├── playback state
  └── canonical cursors
```

因此：

- 目标设备只要在线且能力满足，就可以设置音量；
- 设备可以完全没有 PlaybackContext；
- `device.setVolume` 和 `device.volume.update` 不得创建、读取或修改 PlaybackContext；
- `epoch`、`version`、`queueRevision`、`controlVersion` 均不得因设备级音量发生变化；
- 播放控制仍必须走 PlaybackContext discovery、subscribe 和 status 流程，不能因为新增音量功能而跳过。

## 4. 注册与 capability 协商

### 4.1 兼容旧 2.2 客户端的 9/10 字段形状

基础 strict-v2 capabilities 仍然是原来的 9 个 bool。r8 增加可选第 10 个 bool：

```json
"remoteVolumeControl": true
```

服务端支持两种闭合形状：

- 旧 probe：请求 9 个字段，ACK 也只返回 9 个字段；
- r8 negotiated registration：请求 10 个字段，ACK 返回完整 10 个字段。

服务端不会向只发送 9 字段的旧客户端主动增加第 10 个字段，因此旧的 closed parser 不会因为
未知字段被破坏。

### 4.2 推荐的兼容协商流程

如果客户端需要同时兼容 2.2 和 2.3 服务端：

1. 首次物理连接用基础 9 字段完成 probe；
2. 从 register ACK 解析 numeric `protocolVersion`；
3. 只有 major 为 `2` 且 minor `>=3` 时，才断开并建立 negotiated reconnect；
4. negotiated reconnect 的 capabilities 增加 `remoteVolumeControl`；
5. 以 ACK 返回的 `negotiatedCapabilities` 作为唯一 capability gate。

不要用字符串字典序比较版本号。

### 4.3 controller 与 player 的声明差异

controller-only 示例：

```json
{
  "roles": ["controller"],
  "capabilities": {
    "playbackContextV2": true,
    "playbackPrepare": false,
    "effectiveAtPlayback": false,
    "canPlay": false,
    "canPause": false,
    "canSeek": false,
    "canSetVolume": false,
    "supportsFollow": false,
    "supportsBroadcast": false,
    "remoteVolumeControl": true
  }
}
```

player 要接收远程音量命令，必须同时声明并协商：

```json
{
  "canSetVolume": true,
  "remoteVolumeControl": true
}
```

含义不同：

- `canSetVolume`：player 能实际设置本地设备或播放器音量；
- `remoteVolumeControl`：连接理解 r8 的设备级音量 action 和确认流程。

controller 自身不需要 `canSetVolume:true`。

## 5. controller 端需要实现的改动

### 5.1 从 device.list 选择精确设备

controller 必须使用 `device.list` 返回的：

- `clientId`
- `deviceSessionId`
- `roles`
- `capabilities.remoteVolumeControl`
- `capabilities.canSetVolume`
- 可选 `volumeState`

只有在线设备同时满足以下条件时，才启用音量控件：

```text
roles contains player
remoteVolumeControl == true
canSetVolume == true
```

不要使用 PlaybackContext ID、旧 `sessionId` 或只使用 `clientId` 来定位音量目标。

扩展 controller 可能收到：

```json
{
  "clientId": "player-1",
  "deviceSessionId": "device:player-1",
  "deviceName": "Living Room",
  "roles": ["player"],
  "capabilities": {
    "playbackContextV2": true,
    "playbackPrepare": false,
    "effectiveAtPlayback": false,
    "canPlay": true,
    "canPause": true,
    "canSeek": true,
    "canSetVolume": true,
    "supportsFollow": false,
    "supportsBroadcast": false,
    "remoteVolumeControl": true
  },
  "volumeState": {
    "volume": 64,
    "clientSeq": 4,
    "serverUpdatedAtMs": 1780000001200
  }
}
```

`volumeState` 是可选字段。设备刚连接但还没有上报音量时，该字段可以不存在。

### 5.2 发送 device.setVolume

请求必须使用 payload 内的精确 client/device pair，不能使用顶层 target：

```json
{
  "type": "command",
  "action": "device.setVolume",
  "requestId": "device-volume-1",
  "payload": {
    "targetClientId": "player-1",
    "targetDeviceSessionId": "device:player-1",
    "volume": 65
  }
}
```

`volume` 必须是 JSON integer，范围为 `0..100`。

成功结算是 correlated `system.ack`：

```json
{
  "type": "system",
  "action": "system.ack",
  "requestId": "device-volume-1",
  "connectionNonce": "<controller socket nonce>",
  "connectionEpoch": 1,
  "payload": {
    "action": "device.setVolume"
  }
}
```

这个 ACK 只表示：

- 服务端完成授权和能力检查；
- 精确目标 Socket 仍在线；
- command 已进入该 Socket 的发送路径。

ACK 不表示设备已经达到请求值。controller 不能仅凭 ACK 把 UI 标记为“已确认 65%”。

### 5.3 以 device.volume.update 为实际值

controller 最终应以服务端广播的 canonical event 为准：

```json
{
  "type": "event",
  "action": "device.volume.update",
  "connectionNonce": "<controller socket nonce>",
  "connectionEpoch": 1,
  "payload": {
    "sourceClientId": "player-1",
    "deviceSessionId": "device:player-1",
    "volume": 64,
    "clientSeq": 4,
    "serverUpdatedAtMs": 1780000001200
  }
}
```

注意请求值可以是 65，而实际确认值是 64。硬件音量步进、系统限制或平台换算都可能导致差异。

收到事件后：

1. 按 `sourceClientId + deviceSessionId` 更新对应设备；
2. 使用 `payload.volume` 更新已确认值；
3. 清除该设备的 pending/preview 状态；
4. 如果设备已不在最新 `device.list` 中，丢弃该事件并刷新设备列表。

### 5.4 UI 请求合并建议

滑块可能在短时间产生大量值。建议采用：

- 150ms debounce；
- 同一目标最多一个 in-flight `device.setVolume`；
- in-flight 期间只保存最新待发送值；
- 当前请求完成后立即发送最新值，丢弃中间值；
- `rate_limited` 时按 `retryAfterMs` 延迟，并继续 latest-wins。

该策略可以避免陈旧请求在用户停止拖动后覆盖最新意图。

## 6. player 端需要实现的改动

### 6.1 接收 device.setVolume command

服务端发给目标 player 的 command 没有 `requestId`，也不会复制 target 字段：

```json
{
  "type": "command",
  "action": "device.setVolume",
  "connectionNonce": "<player socket nonce>",
  "connectionEpoch": 1,
  "payload": {
    "sourceClientId": "controller-1",
    "volume": 65
  }
}
```

player 必须：

1. 先按通用 strict 规则校验当前 Socket 的 `connectionNonce` 和 `connectionEpoch`；
2. 校验 payload 闭合且只有 `sourceClientId`、`volume`；
3. 把请求值应用到实际设备或播放器音量；
4. 读取应用后的实际值；
5. 即使实际值与原值相同，也发送一次 `device.volume.update` confirmation。

不要等待或尝试回复 controller 的 requestId，因为业务 command 不包含该字段。

### 6.2 上报 device.volume.update

player 请求：

```json
{
  "type": "event",
  "action": "device.volume.update",
  "requestId": "device-volume-feedback-4",
  "payload": {
    "deviceSessionId": "device:player-1",
    "volume": 64,
    "clientSeq": 4
  }
}
```

规则：

- `deviceSessionId` 必须与当前 register payload 完全一致；
- `volume` 是实际值，范围 `0..100`；
- `clientSeq` 是设备音量专用序号，不与 `playback.update.clientSeq` 共用；
- 同一物理连接内从 1 单调递增；
- 物理重连后可以从 1 重新开始；
- 同一个 `clientSeq` 只能重放完全相同的 payload；
- 本地用户修改音量时也应主动发送该事件。

这是 event-confirmed 请求，服务端不会发送 `system.ack`。成功结算是服务端发回 canonical
`device.volume.update`，其中 `sourceClientId`、`deviceSessionId`、`volume`、`clientSeq` 与本次反馈匹配，
并新增 `serverUpdatedAtMs`。canonical event 不回显 requestId，因此客户端库应按 action、设备身份和
`clientSeq` 完成 event confirmation。

如果 player 当前还有 active PlaybackContext，则正常的 `playback.update` 仍应包含同一个实际
`volume`，用于维护该 Context 下的 DevicePlaybackState。两种反馈用途不同，不能互相替代。

## 7. 完整时序

```text
Controller                Server                       Player
    |                        |                            |
    | device.setVolume      |                            |
    | client/device + 65    |                            |
    |---------------------->|                            |
    |                        | validate exact pair        |
    |                        |--------------------------->|
    |                        | command: volume 65         |
    | system.ack            |                            |
    |<----------------------|                            |
    | routed, not confirmed |                            |
    |                        |              apply volume  |
    |                        |              actual = 64   |
    |                        | device.volume.update seq=4 |
    |                        |<---------------------------|
    |                        | store online state         |
    |                        | canonical update           |
    |<-----------------------|--------------------------->|
    | UI confirmed = 64     | source event confirmation  |
```

这个流程与目标是否正在播放无关，也不需要创建 PlaybackContext。

## 8. 错误处理

客户端至少应针对以下错误码实现明确行为：

| code | 常见原因 | 客户端行为 |
| --- | --- | --- |
| `bad_request` | 字段缺失、类型错误、volume 越界、额外字段 | 视为客户端 bug，不盲目重试 |
| `unauthorized` | 尚未完成登录或注册 | 重新执行 bootstrap，不发送业务请求 |
| `forbidden` | 请求端没有 controller 角色，或目标不是 player | 禁用操作并记录配置错误 |
| `not_supported` | 服务端不是 r8 surface | 关闭远程音量功能，不回退到 `player.setVolume` |
| `not_found` | 目标离线、clientId 错误或 deviceSessionId 已替换 | 清除选择并重新请求 `device.list` |
| `capability_required` | controller 或 player 未协商所需 capability | 禁用功能，重新检查 registration |
| `client_sequence_conflict` | player 的音量 clientSeq 倒退或同序号不同内容 | fail-closed；修复序号状态或物理重连 |
| `rate_limited` | 滑块请求过密 | 等待 `retryAfterMs`，只重试最新意图 |
| `internal_error` | 服务端未预期错误 | 有界退避；不要建立离线命令队列 |

所有 correlated error 的 `payload.action` 必须是原请求 action。注册完成后的响应和 push 必须带当前
controller/player Socket 自己的 `connectionNonce` 与 `connectionEpoch`。

## 9. 断线、替换和空闲设备行为

- 设备断线后，服务端删除对应 `volumeState`；
- 同一 `clientId` 被新 Socket 替换时，旧连接的音量状态失效；
- `deviceSessionId` 变化表示目标设备实例已变化，旧 pair 不得继续使用；
- 离线时发送 `device.setVolume` 返回 `not_found`；
- 服务端不会保存“期望音量”，也不会在重连后补发旧命令；
- player 重连后应主动上报当前实际音量，使新的 `device.list.volumeState` 可被水合；
- controller 重连后应重新 `device.list`，不能假设在线音量状态跨连接保留。

## 10. 禁止的兼容捷径

客户端不要采用以下实现：

- 不要发送 strict `player.setVolume`；
- 不要把设备音量写入 PlaybackContext snapshot 或 queue/control cursor；
- 不要为了控制音量临时创建 PlaybackContext；
- 不要使用 `sessionId` 或 `sourceSessionId`；
- 不要在请求顶层放 `targetClientId`；
- 不要只用 `clientId`，必须同时发送 `targetDeviceSessionId`；
- 不要把 `device.setVolume` ACK 当成实际音量确认；
- 不要假设 command 会带 controller 的 requestId；
- 不要向不含 `remoteVolumeControl` 的 9 字段 ACK 强行补默认第 10 字段并声称已协商；
- 不要为离线设备排队并在未来自动执行音量命令。

## 11. 客户端改造清单

### Controller

- [ ] 支持 9 字段 probe 和 10 字段 negotiated registration；
- [ ] 只以 `negotiatedCapabilities.remoteVolumeControl` 开启功能；
- [ ] 解析 `device.list` 的可选 `volumeState`；
- [ ] 使用 `targetClientId + targetDeviceSessionId` 发送 `device.setVolume`；
- [ ] 允许选择没有 PlaybackContext 的在线 player；
- [ ] 区分 routed ACK 与 canonical actual update；
- [ ] 收到 `device.volume.update` 后按 source client/device pair 更新 UI；
- [ ] 实现 debounce、单 in-flight 和 latest-wins；
- [ ] `not_found` 后刷新设备列表并使旧选择失效；
- [ ] 不回退到 legacy `player.setVolume`。

### Player

- [ ] `canSetVolume:true` 且 `remoteVolumeControl:true`；
- [ ] 处理无 requestId、无 target 字段的 `device.setVolume` command；
- [ ] 实际设置音量并读取平台最终值；
- [ ] 即使请求值没有造成变化，也发送 confirmation；
- [ ] 使用独立且单调递增的 device-volume `clientSeq`；
- [ ] 等待 canonical event，而不是等待 ACK；
- [ ] 本地音量变化也主动发送 `device.volume.update`；
- [ ] active Context 存在时，同时保持 `playback.update.volume` 一致；
- [ ] 物理重连后从新序号作用域开始并主动上报当前音量。

### 通用 strict 客户端库

- [ ] action allowlist 增加 `device.setVolume` 和 `device.volume.update`；
- [ ] request/action type 映射分别为 `command` 和 `event`；
- [ ] `device.setVolume` 使用 ACK 结算；
- [ ] `device.volume.update` 使用 event confirmation 结算；
- [ ] output parser 接受扩展 recipient 的 10 capability shape 和可选 `volumeState`；
- [ ] 旧 9 字段 recipient 继续要求闭合 9 字段，不接受服务端主动扩展；
- [ ] 继续拒绝业务 payload 中的 `sessionId` 和非法 target 字段；
- [ ] 继续校验每个入站 strict 消息的 connection provenance。

## 12. 联调验收用例

交付前至少验证：

1. 9 字段 probe 能连接 r8 服务端，ACK 仍是 9 字段；
2. 10 字段 negotiated reconnect 能协商 `remoteVolumeControl:true`；
3. controller 可控制在线但没有 PlaybackContext 的 player；
4. controller 可控制正在播放且有 active Context 的 player，Context snapshot 和四个 cursor 完全不变；
5. 请求 65、平台实际得到 64 时，controller 最终显示 64；
6. 请求值与当前值相同，player 仍发送 confirmation；
7. player 本地修改音量后，controller 收到主动 update；
8. 错误 `targetDeviceSessionId` 返回 `not_found`，不会投递到同 clientId 的其他连接；
9. player 离线后命令返回 `not_found`，重连时不会自动执行旧命令；
10. player 没有 `canSetVolume` 或任一端未协商 `remoteVolumeControl` 时返回 `capability_required`；
11. device-volume `clientSeq` 同序号同内容可安全重放，同序号不同内容或倒退返回
    `client_sequence_conflict`；
12. 快速拖动滑块时最终只保留最新意图，并能处理 `rate_limited.retryAfterMs`；
13. 设备断开或被替换后，旧 `volumeState` 被清除；
14. 所有 strict push 的 nonce/epoch 与当前收件 Socket 的 register ACK 一致。

完成以上改造后，客户端即可统一控制播放中和空闲在线设备的实际音量，而不需要为音量控制创建或
绑定 PlaybackContext。
