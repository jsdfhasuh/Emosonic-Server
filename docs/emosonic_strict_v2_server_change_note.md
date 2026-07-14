# EmoSonic strict-v2 服务端联调变更说明

> 更新日期：2026-07-13
> 面向：Flutter / Web / 桌面客户端工程师
> 范围：Socket.IO namespace `/emo`、事件名 `message` 的 strict-v2 握手和 PlaybackContext v2。

本文说明服务端已经落地的 strict-v2 行为。客户端应以
`specs/emosonic_strict_v2_socketio_server_contract.md` 为唯一完整 wire contract；本文重点列出
现有客户端接入时必须注意的服务端变更。

## 1. 兼容性结论

- legacy 注册（未声明 `playbackContextV2: true`）保持原有兼容行为。
- strict-v2 注册现在是 fail-closed：字段不完整会收到 `system.error`，不会得到 strict
  metadata，也不能继续 strict-v2 会话。
- 已按 strict-v2 契约实现的客户端不需要降级；应按本文的完整 `device.register` payload
  注册。

## 2. `auth.login`：浏览器 OTP 与 ACK 关联

网页 `/player` 和 `/control` 不会接触用户真实账户密码。已登录的同源页面先使用带 CSRF header 的
`POST /emo/browser-auth-password` 获取短期、一次性的 `browser-otp:<opaque>` password
credential，再把返回的 `userName` 和 credential 分别放入 `payload.u`、`payload.p`。

该 OTP 由认证层显式验证，并绑定当前 authenticated user 与浏览器 Cookie session；它不是“存在
Flask session 就忽略 `u/p`”的例外。OTP 只保存摘要、短期有效、成功或失败消费后不能重放，响应带
`Cache-Control: no-store`。普通非浏览器客户端仍可在相同 `u/p` wire shape 中使用账户密码。

服务端成功登录会回显请求的 `requestId`，并在 ACK payload 中回显原 action：

```json
{
  "type": "system",
  "action": "system.ack",
  "requestId": "auth-1",
  "payload": {
    "action": "auth.login",
    "authenticated": true,
    "userName": "alice"
  }
}
```

同一规则适用于 strict 请求的 `system.error`：`payload.action` 始终等于原请求 action。
客户端不得只根据 `requestId` 判断成功，也应校验 `payload.action`。

## 3. `device.register`：严格请求形状

当 `capabilities.playbackContextV2` 为 `true` 时，以下字段都必须提供：

```json
{
  "type": "device",
  "action": "device.register",
  "requestId": "register-phone-1",
  "payload": {
    "clientId": "phone-1",
    "deviceSessionId": "device:phone-1",
    "deviceName": "Alice phone",
    "roles": ["player", "controller"],
    "capabilities": {
      "playbackContextV2": true,
      "playbackPrepare": false,
      "effectiveAtPlayback": false,
      "canPlay": true,
      "canPause": true,
      "canSeek": true,
      "canSetVolume": true,
      "supportsFollow": false,
      "supportsBroadcast": false
    }
  }
}
```

服务端校验规则：

- `clientId`、`deviceSessionId`、`deviceName` 必须是非空字符串；
- `roles` 必须是非空数组，只能包含 `player`、`controller`，且不得重复。客户端可以声明其中
  一个角色或同时声明两个；同时声明时服务端规范化为 `player`、`controller` 顺序；
- 上述 9 个 capability 必须全部存在，且均为 JSON boolean；
- strict payload 不得带 `sessionId`；
- `alias` 若提供，必须是非空字符串。

字段缺失、类型错误或 `sessionId` 混入时，服务端返回例如：

```json
{
  "type": "system",
  "action": "system.error",
  "requestId": "register-phone-1",
  "timestamp": 1783900000.0,
  "payload": {
    "action": "device.register",
    "code": "bad_request",
    "message": "strict-v2 capabilities.supportsBroadcast must be a boolean",
    "retryable": false
  }
}
```

成功 ACK：

```json
{
  "type": "system",
  "action": "system.ack",
  "requestId": "register-phone-1",
  "timestamp": 1783900000.0,
  "connectionNonce": "example-physical-socket-nonce",
  "connectionEpoch": 1,
  "payload": {
    "action": "device.register",
    "clientId": "phone-1",
    "deviceSessionId": "device:phone-1",
    "negotiatedCapabilities": {
      "playbackContextV2": true,
      "playbackPrepare": false,
      "effectiveAtPlayback": false,
      "canPlay": true,
      "canPause": true,
      "canSeek": true,
      "canSetVolume": true,
      "supportsFollow": false,
      "supportsBroadcast": false
    },
    "strictV2": {
      "protocolVersion": "2.1.0",
      "schemaHash": "0000000000000000000000000000000000000000000000000000000000000000",
      "serverBuildCommit": "0000000000000000000000000000000000000000",
      "connectionNonce": "example-physical-socket-nonce",
      "connectionEpoch": 1
    }
  }
}
```

## 4. 关联、provenance 与禁止字段

严格客户端注册成功后：

- 服务端的每一条 envelope 都会带顶层 `connectionNonce` 和 `connectionEpoch`；客户端应
  校验它们与注册 ACK 中的 `strictV2` 值一致。
- 所有响应使用原请求的 `requestId`。显式 `device.list` 响应也会回显其 `requestId`。
- strict 业务 payload 不会带 `sessionId` 或 `targetClientId`，业务 envelope 也不会带
  顶层 `targetClientId`。服务端已按实际 Socket 收件人路由。

## 5. PlaybackContext 推送

### 5.1 `playback.update` 是设备 feedback，不是 context snapshot

客户端上报 `playback.update` 后，订阅者会收到 canonical feedback：

```json
{
  "type": "event",
  "action": "playback.update",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:alice:main",
    "sourceClientId": "phone-1",
    "deviceSessionId": "device:phone-1",
    "state": "playing",
    "trackId": "song-1",
    "positionMs": 1200,
    "clientSeq": 7,
    "serverUpdatedAtMs": 1780000001200
  }
}
```

`playback.update` 只确认设备 feedback，不修改 PlaybackContext snapshot 或四个 Context
cursor。客户端需要 Context snapshot 时应使用显式 `playback.context.status` 或已有订阅产生的
canonical Context push；不要把 `playback.update` 当成队列或 authority snapshot。

### 5.2 服务端路由的 player control

服务端已移除 strict control 的 direct target 和请求时的 `baseControlVersion`。例如：

```json
{
  "type": "command",
  "action": "player.seek",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:alice:main",
    "controlVersion": 13,
    "sourceClientId": "controller-1",
    "positionMs": 42000
  }
}
```

`player.next` / `player.prev` 不携带 `positionMs` 或 queue item 字段。`queue.playItem`
则包含 canonical `queueSongIds`、`queueIndex`、`queueRevision`、`controlVersion` 和
`sourceClientId`。

### 5.3 Context 关闭

关闭 context 后，订阅者会收到：

```json
{
  "type": "event",
  "action": "playback.context.closed",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:alice:main"
  }
}
```

客户端收到后应清理该 context 的本地状态、pending command 和订阅状态。

## 6. Follow 与 Broadcast capability gate

- `supportsFollow: false`：客户端发送 `follow.start` 或 `follow.stop` 会得到 correlated
  `system.error`，`payload.code` 为 `capability_required`。
- `supportsBroadcast: false`：客户端发送任意 `broadcast.*` 会得到 correlated
  `system.error`，`payload.code` 为 `capability_required`。
- strict 广播收件人未声明 `supportsBroadcast: true` 时，服务端不会向该 Socket 投递
  Broadcast 业务消息。

因此，客户端必须按实际实现能力设置这两个字段；不要为了绕过注册校验一律填 `true`。

## 7. 客户端接入检查清单

1. 每次连接生成新的、非空的 `requestId`。
2. `auth.login` 和 `device.register` 都等待同 ID 且同 `payload.action` 的 ACK。
3. strict 注册使用第 3 节完整字段；收到 `system.error` 时不要继续发送 strict 业务 action。
4. 保存 register ACK 中的 nonce/epoch，并丢弃不匹配的后续消息。
5. 严格区分 `playback.update` feedback 与 `playback.context.status` snapshot。
6. 仅在相应 capability 为 `true` 时显示或发送 Follow / Broadcast 操作。
