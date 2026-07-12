# EmoSonic strict-v2 Socket.IO 服务端契约

> 文档状态：Normative（服务端与 Flutter 联调的唯一完整 wire contract）  
> 文档修订：2026-07-12-r5  
> 协议主版本：`2.x`  
> 读者：EmoSonic 服务端工程师  
> 范围：PlaybackContext v2 strict-v2。本文是客户端当前会发送、会接受的协议契约；不是旧 `sessionId` 协议的兼容说明，也不授予 production rollout 权限。

本文件在仓库中的规范路径是
`specs/emosonic_strict_v2_socketio_server_contract.md`。当注册 metadata Goal、服务端变更说明、
实现代码、测试名称或历史文档与本文件冲突时，不得用它们静默改写本契约；应将冲突视为
conformance 缺陷并修正文档或实现。注册 metadata Goal 只定义握手描述符，服务端变更说明
只记录某次实现状态，二者都不是全量 wire contract。

本 r5 是 `2.1.0` 的冻结候选基线。此前实验性 `2.1.0` 部署不形成兼容性承诺；只有本文全部
conformance tests 和真实双客户端联调通过后才可标记 ready。r5 冻结后，任何破坏性 wire 变更
必须升级为 `3.x`。

## 1. 一次性结论

服务端应把 strict-v2 实现为一套以 `playbackContextId` 为唯一播放任务主键的 Socket.IO 协议：

- 传输使用 Socket.IO namespace `/emo`、事件名 `message`；
- 所有 `system.ack` / `system.error` 必须以同一 `requestId` 关联，且 `payload.action` 必须回显原 action；direct action response 使用第 2.2、4.3 节规定的同 requestId 关联方式；
- strict 注册成功后，所有发给 strict recipient 的入站 envelope 必须由统一发送工厂附加该 Socket 的顶层 `connectionNonce` 和 `connectionEpoch`；
- strict-v2 业务 payload 中不得出现 `sessionId`。客户端请求不得使用顶层 `targetClientId`；唯一允许的 payload 例外是 `playback.handoff.start.payload.targetClientId`。服务端业务推送的顶层和 payload 均不得带 `targetClientId`；
- `protocolVersion` 只需承诺向后兼容的 `2.x`。`schemaHash` 和 `serverBuildCommit` 是部署观测值，不是要求客户端每次打包固定的 pin；破坏性变更必须升为 `3.x`；
- Follow、Broadcast、Handoff 受注册时返回的 `negotiatedCapabilities` 控制；服务器不得直接信任客户端请求值，也不应向未协商成功的连接投递可选动作。

必须区分字段方向：客户端请求中的 `baseControlVersion` / `baseQueueRevision` 是并发前置条件；
服务端推送中的 `controlVersion` / `queueRevision` 是接受后的 canonical cursor。服务端推送不带
`baseControlVersion`，不代表服务端可以从 `2.x` 请求契约中删除该字段。

`player.setVolume`、`player.requestState`、`session.subscribe`、`queue.session.sync`、`queue.local.set`、`queue.ready.complete` 不属于当前 strict-v2 可用 surface。不要用它们替代本文动作。

## 2. 传输与通用 envelope

### 2.1 Socket.IO 端点

若用户配置的服务器 base URL 为：

```text
http(s)://host[:port][/base-path]
```

Flutter 使用：

| 项目 | 值 |
| --- | --- |
| Socket.IO namespace URL | `http(s)://host[:port][/base-path]/emo` |
| Engine.IO path | `[/base-path]/emo/ws` |
| Socket.IO event | `message` |
| 客户端 transports | `websocket`，其次 `polling` |

### 2.2 通用 envelope

客户端发送和服务端推送均使用对象：

```json
{
  "type": "command | state | event | device | auth | system",
  "action": "dot.separated.action",
  "requestId": "non-empty-per-request-id",
  "payload": {},
  "targetClientId": "strict 顶层禁止；handoff 目标仅放在指定请求 payload",
  "timestamp": 0
}
```

规则：

1. `type`、`action`、`payload` 是所有 envelope 的必需字段。客户端 request 以及其
   ACK/error/direct response 还必须有 `requestId`；未关联的 server push 必须省略
   `requestId`。`timestamp` 可选。strict 中 `targetClientId` 按第 5、6 条禁止。
2. 每个客户端请求都必须使用新的非空 `requestId`，包括 event/state-confirmed 请求。服务端
   reply 必须复用它。业务 push（包括发给 authority 的 control）一律省略 `requestId`。
3. `payload` 必须是 object。字段名大小写敏感。
4. strict-v2 业务 payload（包括嵌套对象）禁止出现 `sessionId`。`sourceSessionId` 也不得用于 Follow。
5. 客户端 → 服务端：strict 请求的顶层 `targetClientId` 一律禁止；payload 内也禁止，唯一例外是 `playback.handoff.start.payload.targetClientId`，它表示业务上的接管目标，不是 Socket 投递指令。
6. 服务端 → 客户端：strict 业务 push / direct response 的顶层和 payload 均不得有 `targetClientId`。`system.ack`、`system.error`、`system.pong`、`device.list` 的顶层 transport target 虽可被客户端容忍，但服务端仍应优先按实际 Socket 投递并省略该字段；payload 内始终禁止。
7. 所有时间戳均为 Unix epoch **milliseconds**，除非字段名是旧的 `timestamp`（客户端也能读秒）。服务端应优先使用 `serverTimeMs` 或 `serverUpdatedAtMs` 的毫秒值。
8. 缺失、空值、类型错误或超过长度限制的 `requestId` / `action` 无法形成合法 correlated
   error；服务端必须记录脱敏协议错误并立即断开该 Socket，不得自行生成 ID 或发送无关联 error。

### 2.3 strict 收件人的强制 provenance 字段

`device.register` strict ACK 成功后，服务端给该 strict Socket 的每一条入站 envelope 都必须在**顶层**添加：

```json
{
  "connectionNonce": "non-empty-per-physical-socket-random-string",
  "connectionEpoch": 1
}
```

- `connectionNonce` 必须由密码学安全随机数生成器产生，至少包含 128 bit 随机熵，并编码为非空
  string。禁止使用时间戳、递增数字、进程 ID 或其他可预测值。
- `connectionEpoch` 固定为整数 `1`；同一物理连接内保持不变。每个新物理连接必须生成新的随机 nonce，因此不依赖跨连接持久化 epoch 计数器。
- 不能把这两个字段塞进业务 `payload`；必须在 envelope 顶层。
- 必须通过服务端 Socket emit/ACK helper 集中注入，不能依赖各 action handler 手写。
- 不匹配、缺失或来自旧 Socket 的 strict push 会被 Flutter 隔离，不会写入播放状态或执行音频控制。

## 3. 登录、注册与 TOFU 协商

### 3.1 必经时序

```text
Socket connect
  -> auth.login (correlated system.ack)
  -> device.register (correlated system.ack + strictV2 metadata)
  -> probe：客户端保存 profile 后主动断开
  -> negotiated reconnect：再次 auth.login / device.register
  -> device.list
  -> PlaybackContext v2 actions and pushes
```

首次 probe 只为确认服务端 profile：它宣告 `playbackContextV2:true`，但不会发送任何 strict 业务动作。服务端必须支持这次完整注册；不要把 probe 解释成 legacy client。

### 3.2 `auth.login`

客户端请求：

```json
{
  "type": "auth",
  "action": "auth.login",
  "requestId": "auth-1",
  "payload": {
    "u": "<username>",
    "p": "<password>"
  }
}
```

成功 ACK：

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

`payload.action`、`authenticated:true` 和认证后的非空 `userName` 都不能省略。strict probe 和
negotiated client 会拒绝 actionless ACK，随后超时并断开。此 ACK 是第 4.1 节“无结果 ACK 只有
action”规则的 bootstrap 例外。

### 3.3 `device.register`

客户端 strict 请求：

```json
{
  "type": "device",
  "action": "device.register",
  "requestId": "register-1",
  "payload": {
    "clientId": "phone-1",
    "deviceSessionId": "device:phone-1",
    "deviceName": "android Player",
    "alias": "可选，非空 string",
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

字段规则：

- `clientId`、`deviceSessionId`、`deviceName`：非空 string；
- `roles`：非空数组，只允许 `player`、`controller`，不可重复；可声明其中一个或两者；
- `capabilities`：必须有上表全部 9 个 bool 字段；
- strict 注册 payload 中不得有 `sessionId`；
- `playbackContextV2:true` 是要求服务端返回 strict metadata 的条件。
- `effectiveAtPlayback:true` 与 `playbackPrepare:false` 的组合非法。

请求中的 capabilities 描述客户端自身能力；服务端必须将它们与部署 readiness 求交集，并在 ACK
中返回完整 `negotiatedCapabilities`。Core profile 未 ready 时不得静默降级：服务端返回
`not_supported`，不返回 strict metadata，也不转入 legacy。可选 profile 未 ready 时注册仍可
成功，但对应 negotiated 值必须为 false。

协商结果还必须满足角色和能力依赖：`supportsFollow:true` 要求 player + canPlay；
`playbackPrepare:true` / `effectiveAtPlayback:true` 只可授予能作为 Handoff target 的 player，且
effective-at 必须与 prepare 同时成立。controller-only 连接可以协商 `supportsBroadcast:true` 以
创建和控制 Broadcast，但只有同时具备 player + canPlay + canPause + canSeek 的连接才可成为
participant。

成功 ACK 必须是：

```json
{
  "type": "system",
  "action": "system.ack",
  "requestId": "register-1",
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
      "schemaHash": "64-lowercase-hex-sha256",
      "serverBuildCommit": "40-lowercase-git-sha-or-unknown",
      "connectionNonce": "nonce-for-this-physical-socket",
      "connectionEpoch": 1
    }
  }
}
```

注册 metadata 规则：

| 字段 | 必须 | Flutter 校验 |
| --- | --- | --- |
| `payload.action` | 是 | 精确为 `device.register` |
| `strictV2.protocolVersion` | 是 | 正则 `2.<number>[.<number>][suffix]`；非 `2.x` fail-closed |
| `strictV2.schemaHash` | 是 | 64 位小写十六进制 SHA-256 |
| `strictV2.serverBuildCommit` | 是 | 40 位小写 Git SHA，或精确值 `unknown` |
| `strictV2.connectionNonce` | 是 | 非空 string |
| `strictV2.connectionEpoch` | 是 | 精确为整数 `1`，不能是 string |
| `negotiatedCapabilities` | 是 | 完整 9 个 bool 字段；后续 capability gate 的唯一依据 |

合规服务端只在 `payload.strictV2` 输出 metadata，且只使用 `serverBuildCommit`。Flutter 对
`serverCommit` 或 ACK payload 顶层 metadata 的读取仅是历史部署兼容，不属于合规服务端输出
schema。strict ACK 不返回 `client` 对象；设备详情统一通过 `device.list` 获取。

注册握手描述符必须覆盖 ACK 的 `payload.action`、`clientId`、`deviceSessionId`、完整 9 个 bool
的 `negotiatedCapabilities`，以及 `strictV2` 中的 protocolVersion、schemaHash、
serverBuildCommit、connectionNonce、connectionEpoch。上述字段的名称、类型、required、枚举或
约束发生变化时，必须更新描述符并重新计算 schemaHash；不得继续返回基于旧 ACK shape 的 hash。
`auth.login` 不属于注册描述符。

同一主版本的 `schemaHash` 与 build commit 发生变化时，客户端会更新该服务器的本地观测记录；不会因此降级或 fallback。若 wire contract 发生破坏性变化，服务端必须改为 `3.x`，让客户端关闭 strict。

### 3.4 `device.list`

注册后客户端发送：

```json
{
  "type": "state",
  "action": "device.list",
  "requestId": "device-list-1",
  "payload": {}
}
```

服务端推送/响应：

```json
{
  "type": "state",
  "action": "device.list",
  "requestId": "device-list-1",
  "connectionNonce": "<registered nonce>",
  "connectionEpoch": 1,
  "payload": {
    "devices": [
      {
        "clientId": "phone-1",
        "deviceSessionId": "device:phone-1",
        "deviceName": "Android Player",
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
        },
        "alias": "可选"
      }
    ]
  }
}
```

strict 设备对象必须且只允许 `clientId`、`deviceSessionId`、`deviceName`、`roles`、完整协商后
`capabilities`，以及可选非空 `alias`。不得返回 legacy `sessionId`、`userName`、连接时间或其他
内部字段。设备数组按 `clientId` 稳定排序；roles 输出采用 `player`、`controller` 的固定枚举顺序。

这是 direct response，不得先发 `system.ack`；`requestId` 必须与 `device.list` 请求相同。

### 3.5 Heartbeat：`system.ping` / `system.pong`

只有完成 `device.register` 后才允许应用层 heartbeat；未注册请求返回 `unauthorized`。ready 后
客户端约每 30 秒发送：

```json
{"type":"system","action":"system.ping","requestId":"ping-1","payload":{}}
```

服务端必须以同一 requestId 返回：

```json
{
  "type": "system",
  "action": "system.pong",
  "requestId": "ping-1",
  "connectionNonce": "<registered nonce>",
  "connectionEpoch": 1,
  "payload": {"serverTimeMs": 1780000000000}
}
```

`serverTimeMs` 非强制，但会提高 effective-at / Follow / Broadcast 的时钟同步精度。

## 4. 标准 ACK 与错误

### 4.1 成功 `system.ack`

所有在第 4.3 节标为 ACK 结算的 strict 请求必须收到同 `requestId` 的 ACK：

```json
{
  "type": "system",
  "action": "system.ack",
  "requestId": "original-request-id",
  "connectionNonce": "<registered nonce>",
  "connectionEpoch": 1,
  "payload": {
    "action": "original.action"
  }
}
```

动作需要返回结果字段时，只能增加本文对应 action 明示的字段；无结果字段的 ACK payload 只有
`action`。`auth.login` 是明确的 bootstrap 例外，额外返回 `authenticated:true` 和 `userName`。

`payload.action` 必须与请求 action 精确相同。当前 strict client 使用 8 秒 ACK timeout；action 不同、缺失或 requestId 不同都会被视为未关联。

`auth.login` ACK 和 `device.register` ACK 是 bootstrap 成功响应，可以省略顶层 provenance；
register ACK 仍必须在 `payload.strictV2` 给出将绑定到该 Socket 的 nonce/epoch。除此以外，
注册成功后的 ACK 必须有顶层 provenance。

只有第 4.3 节结算矩阵明确标为 ACK 的动作才使用本响应。一次请求只能结算一次；不得先 ACK
再发 direct response，或先发 direct response 再 ACK。

### 4.2 失败 `system.error`

```json
{
  "type": "system",
  "action": "system.error",
  "requestId": "original-request-id",
  "connectionNonce": "<registered nonce>",
  "connectionEpoch": 1,
  "payload": {
    "action": "original.action",
    "code": "stale_version",
    "message": "safe diagnostic string",
    "retryable": false,
    "playbackContextId": "可选",
    "currentControlVersion": 12,
    "currentQueueRevision": 8,
    "currentVersion": 33,
    "currentClientSeq": 99
  }
}
```

`action` 对 strict 请求也是必需的 correlation 字段。成功和失败响应互斥；一次处理只能结算
一次，不能再次执行副作用。缓存期内重复请求按第 4.4 节重放已结算结果。

允许的错误码全集如下；服务端不得在 `2.x` 中临时创造同义错误码：

| code | 含义 | `retryable` | 条件字段 |
| --- | --- | --- | --- |
| `bad_request` | envelope、字段、类型、枚举、范围或字段组合非法 | `false` | 无 |
| `unauthorized` | 尚未登录或凭据无效 | `false` | 无 |
| `forbidden` | 已登录但无 context/action 权限 | `false` | `playbackContextId` 可选 |
| `not_supported` | action 不属于服务器声明的 `2.x` surface | `false` | 无 |
| `not_found` | context、handoff、broadcast 或目标设备不存在 | `false` | `playbackContextId` 可选 |
| `context_closed` | context 已终止 | `false` | `playbackContextId` 必需 |
| `authority_offline` | 当前 authority 没有有效 Socket | `true` | `playbackContextId` 必需 |
| `conflict` | 同一逻辑 ID 被用于不同意图，或状态机不允许该动作 | `false` | Context/handoff/broadcast 冲突时必须有 `playbackContextId` 及三个 `current*Version/Revision`；仅 requestId 内容冲突时省略 |
| `stale_version` | base cursor 落后或超前于 canonical cursor | `false` | 对应 `current*` cursor 必需 |
| `client_sequence_conflict` | `clientSeq` 重复但内容不同或倒退 | `false` | `currentClientSeq` 必需 |
| `capability_required` | 连接未协商到动作所需 capability/角色依赖 | `false` | 无 |
| `rate_limited` | 超出连接、用户或 action 限额 | `true` | `retryAfterMs` 必需且为正整数 |
| `internal_error` | 未预期服务端错误 | `true` | 无；不得暴露堆栈、路径或数据库内容 |

除表中条件字段外，错误 payload 只允许 `action`、`code`、`message`、`retryable`、
`playbackContextId`、`currentControlVersion`、`currentQueueRevision`、`currentVersion`、
`currentClientSeq`、`retryAfterMs`。不适用的可选字段必须省略，不得写 JSON `null`。

登录或注册尚未确认 provenance 时发生的 `auth.login` / `device.register` 错误是唯一 bootstrap
例外：仍须使用同 `requestId` 的 `system.error` 和正确 `payload.action`，但顶层可以没有
`connectionNonce` / `connectionEpoch`。注册成功后的任何错误不再享有此例外。

### 4.3 请求结算矩阵

每个 action 的成功结算方式唯一如下：

| action | 唯一成功结算方式 |
| --- | --- |
| `auth.login`、`device.register` | correlated `system.ack` |
| `device.list` | 同 `requestId` 的 direct `device.list` response |
| `playback.context.create` | 同 `requestId` 的 direct `playback.context.create` response，payload 为第 6.1 节完整 snapshot |
| `playback.context.status` | 同 `requestId` 的 direct `playback.context.status` response，payload 为第 6.2 节完整 status |
| `playback.context.subscribe`、`unsubscribe`、`close` | correlated `system.ack` |
| `queue.context.sync`、`queue.playItem`、`player.play`、`player.pause`、`player.seek`、`player.next`、`player.prev` | correlated `system.ack` |
| `follow.start`、`follow.stop` | correlated `system.ack` |
| `playback.handoff.start`、`playback.handoff.cancel` | correlated `system.ack` |
| `broadcast.start`、`broadcast.status`、`broadcast.play`、`broadcast.pause`、`broadcast.seek`、`broadcast.playItem`、`broadcast.queue.sync`、`broadcast.stop` | correlated `system.ack` |
| `playback.update`、`playback.ready`、`playback.handoff.complete` | event/state-confirmed；服务端不回 ACK，按第 4.5、5.4、6.5、6.8 节状态推进 |
| `system.ping` | 同 `requestId` 的 direct `system.pong` response |

`playback.context.subscribe` 成功只 ACK。客户端随后显式发送
`playback.context.status` 完成水合；服务端不得把未关联的 status push 当成 subscribe 的结算。
Create 不允许用 `{created:true}` 一类简略 ACK 代替 direct response。

### 4.4 幂等、重复请求与重连

1. `playbackContextId` 是 create 的逻辑幂等键。同一用户、同一 authority/device、相同队列、
   index、position 和 state 的重试必须返回现有完整 snapshot；同一 ID 但意图不同必须返回
   `conflict`，不得覆盖既有 Context。服务端必须持久化 creation fingerprint，不能拿后续已变化的
   current snapshot 代替初始意图比较。Flutter 可能使用新 `requestId` 重试同一个 Context ID；
   若该 ID 已是 closed tombstone，则返回 `context_closed`。
2. 服务端必须缓存最近 `(connectionNonce, requestId)` 的 request fingerprint 与结算结果至少
   60 秒；Socket 断开时立即清理。客户端在同一连接中永不复用 requestId。缓存期内同一键重复
   到达时原样重放结果，不得再次产生副作用；同一键的 action 或 payload 不同则返回 `conflict`。
   缓存期外的长期幂等由 Context/Handoff/Broadcast 等逻辑 ID 与 creation fingerprint 保证。
3. 重复 subscribe/unsubscribe、close、`follow.start`/`stop`、handoff cancel/complete 和
   `broadcast.stop` 均必须幂等。资源已处于目标状态时返回与首次成功等价的 ACK 或 canonical
   confirmation，不得为 event-confirmed 动作补发 ACK。
4. 每个 Context 同时只能有一个非终态 handoff。同一 source/target 的 start 重试返回已有
   `handoffId`/`prepareId`；不同 target 返回 `conflict`。
5. 同一已认证用户以相同 `clientId` 完成新注册后，新 sid 原子替换旧 sid，并立即断开旧 sid。
   authority 路由还必须匹配持久化的 `deviceSessionId`；相同 clientId、不同 deviceSessionId 的
   新连接不得自动继承旧 authority。
6. Socket 断开必须清除该 sid 的临时订阅和 client→sid 映射，但不得删除持久化 Context。
   重连客户端会重新 subscribe，再显式请求 status；服务端不得假设 room membership 跨连接保留。
7. event/state-confirmed 请求重复且内容相同时，不重新 mutation 或全局广播；服务端只向当前请求
   Socket 重放以下无 requestId 的 canonical confirmation：`playback.update` 重放缓存的 canonical
   update；`playback.ready` 重放 Handoff 当前 status；`playback.handoff.complete` 重放 completed
   status 与当前 Context status。不得重发 prepare/commit/release、切换 authority 或递增 cursor。
8. Context close 后必须保留持久化 terminal tombstone，`playbackContextId` 不可复用。重复 close
   返回等价 ACK；其他 status/mutation 返回 `context_closed`。正常 Context snapshot 不输出
   `state:"closed"`。服务端先向当前 subscribers/followers 推送 closed，再清除该 Context 的全部
   临时订阅和 Follow relationship。

### 4.5 Cursor 的含义与递增矩阵

这些字段互不替代：

- 顶层 `connectionEpoch`：物理 Socket provenance，只用于隔离旧连接消息。
- Context `epoch`：播放时间线 generation；authority 原子切换时递增。
- `version`：任何被接受并物化到权威 Context snapshot 的 mutation 版本。
- `queueRevision`：canonical 队列内容或 `currentIndex` 变化版本。
- `controlVersion`：被接受且会执行播放控制或改变 authority 的操作版本。
- `clientSeq`：必需的设备 feedback 序号，作用域为
  `(playbackContextId, clientId, connectionNonce, connectionEpoch)`；该作用域内从 1 单调递增，新物理连接可从 1
  重新开始。

| 被接受的动作 | `epoch` | `version` | `queueRevision` | `controlVersion` |
| --- | --- | --- | --- | --- |
| context create | 初始化为 1 | 初始化为 1 | 初始化为 1 | 初始化为 1 |
| `queue.context.sync` | 不变 | +1 | +1 | 仅当 `currentIndex`、该 index 的 `trackId` 或 position 的 canonical 值改变时 +1；不得改变 state |
| `queue.playItem` | 不变 | +1 | +1 | +1 |
| `player.play` / `pause` / `seek` | 不变 | +1 | 不变 | +1 |
| `player.next` / `prev` | 不变 | +1 | +1（`currentIndex` 改变） | +1 |
| `playback.update` | 不变 | 不变 | 不变 | 不变；只更新对应 device state / `clientSeq` |
| handoff authority 原子切换 | +1 | +1 | 不变 | +1 |
| context close | 不变 | +1 后进入 terminal | 不变 | 不变 |

请求中的 base cursor 必须精确等于服务器当前值；不相等返回 `stale_version`，不执行副作用。
条件可选的 base cursor 只有在对应 canonical 域完全不改变时才可省略；实际变化却缺少对应 base
cursor 时返回 `bad_request`。例如 queue sync 中队列内容变化要求 `baseQueueRevision`，而 index、
该 index 的 trackId 或 position 变化还要求 `baseControlVersion`。
服务端 push 的比较顺序为 Context `(epoch, version)`、Queue `(epoch, queueRevision)` 和 Control
`(epoch, controlVersion)`。旧值必须拒绝；完全相等且内容相同视为重复并忽略；完全相等但内容
不同是 `conflict`，必须记录协议错误。`clientSeq` 重复且内容相同可忽略，重复但内容不同或倒退
返回 `client_sequence_conflict`。若相同 feedback 同时命中 requestId 缓存，服务端仍按第 4.4 节
向请求 Socket 重放 canonical confirmation，但不再次写状态。

### 4.6 严格 schema 闭合规则

- 每个 action 只允许本文对应表格、示例和共享 envelope 明示的字段；未知 request 字段返回
  `bad_request`。服务端 push 也不得添加未定义字段。
- 可选字段无值时省略，不得传 JSON `null`。数组不得含重复 ID，string 必须去除首尾空白后非空。
- 所有 ID（`requestId`、client/device/context/handoff/prepare/broadcast/timeline ID）最大 128 UTF-8
  bytes；`action` 最大 64 bytes；错误 `message` 最大 512 bytes。
- 单个 transport message 上限为 256 KiB。Engine.IO/WebSocket 层在进入业务 handler 前发现超限时，
  使用 message-too-big 行为关闭连接，不保证返回 system.error。消息进入业务 handler 后发现字段、
  队列（最多 1000 首）、participants（最多 100 个）或其他业务限制超限时，返回同 requestId 的
  `system.error(code:"bad_request")`。两种情况都不得静默截断。
- `timestamp` 只允许第 2.2 节的兼容用途；action schema 未列出的时间字段一律禁止。
- 所有集合语义数组必须去重。`queueSongIds` 必须保留 canonical 播放顺序，严禁排序；roles 固定按
  `player`、`controller` 顺序输出；participants、skippedClientIds、devices 及服务端生成的其他
  client 集合按 `clientId` 升序。

## 5. 客户端到服务端：全部 strict-v2 请求

除第 3 节的认证、注册、设备列表和 heartbeat 外，以下是当前 Flutter strict action allowlist。所有请求均不得含 `sessionId` 或顶层 `targetClientId`。payload 内 `targetClientId` 也禁止，唯一例外是第 5.4 节的 `playback.handoff.start`。

字段标记：`R` 必需；`O` 可选；`int>=0` 是 JSON number 且不小于零。

### 5.1 PlaybackContext 生命周期

| Action / type | payload | 服务端动作与响应 |
| --- | --- | --- |
| `playback.context.create` / `command` | `playbackContextId:R string`、`deviceSessionId:R string`、`queueSongIds:R non-empty distinct string[]`、`currentIndex:R int>=0`、`positionMs:R int>=0`、`state:R playing\|paused\|stopped` | 仅 player 可创建；deviceSessionId 必须匹配当前连接。创建后自动订阅当前 Socket；`currentIndex < queueSongIds.length`。只用同 requestId direct 返回第 6.1 的完整 snapshot。不得 ACK 或 direct-target。 |
| `playback.context.subscribe` / `state` | `playbackContextId:R` | 将当前 Socket 加入该 context recipient set并返回 ACK；客户端随后显式请求 status。 |
| `playback.context.unsubscribe` / `state` | `playbackContextId:R` | 移除 context recipient；返回 ACK。 |
| `playback.context.status` / `state` | `playbackContextId:R` | 返回第 6.2 的完整 status（同 requestId 直接 action response）。 |
| `playback.context.close` / `command` | `playbackContextId:R` | 当前 authority 或同用户 controller 可关闭。写入不可复用 tombstone，返回 ACK，并向所有订阅者推送 `playback.context.closed`。 |

Context 授权采用“用户域读取、角色控制”：同一 authenticated user 的已注册设备可 subscribe/status；
controller 可发起控制；当前 authority 必须是在线 player。跨用户访问统一返回 `forbidden`，不得用
`not_found` 泄露资源是否存在。Context authority 持久绑定 `authorityClientId` 与
`authorityDeviceSessionId`；只有两者均匹配的重连才恢复 authority 路由。

### 5.2 队列、播放状态和控制

`baseControlVersion` / `baseQueueRevision` 是客户端请求的乐观并发前置条件。服务端在 `2.x`
中必须继续接受并按下表校验这些字段；若 cursor 已过期，返回第 4.2 节的 correlated
`system.error`。服务端接受请求后向客户端推送 canonical `controlVersion` / `queueRevision`，
不得把请求字段 `baseControlVersion` / `baseQueueRevision` 原样转发给 authority。

| Action / type | payload | 服务端动作与响应 |
| --- | --- | --- |
| `queue.context.sync` / `state` | `playbackContextId:R`、`deviceSessionId:R`、`queueSongIds:R non-empty distinct string[]`、`currentIndex:R int>=0`、`positionMs:R int>=0`、`baseQueueRevision:R int>=0`、`baseControlVersion:O int>=0` | 仅当前 authority 可发送，deviceSessionId 必须匹配 authority 连接。队列内容变化校验 baseQueueRevision；index、该 index 的 trackId 或 position 变化时 baseControlVersion 变为必需。按第 4.5 节递增 cursor，ACK 并推送第 6.4 节 canonical queue state。 |
| `playback.update` / `event` | `playbackContextId:R`、`deviceSessionId:R`、`state:R playing\|paused\|stopped`、`positionMs:R int>=0`、`clientSeq:R int>=1`、`trackId:O`、`volume:O int 0..100`、`muted:O bool` | 记录 device feedback。服务端从 authenticated connection 得到 source client，验证 deviceSessionId 绑定并广播第 6.5 节 canonical update；不回 ACK，不修改 Context snapshot/cursor，也不得混入 queue/currentIndex。 |
| `queue.playItem` / `command` | `playbackContextId:R`、`queueIndex:R int>=0`、`baseQueueRevision:R int>=0`、`baseControlVersion:R int>=0` | 验证 cursors 后选择队列项，向 authority 发送第 6.6 的 server-routed control，返回 ACK；所有 recipients 接收 queue/context state 更新。不同 Socket 间不承诺到达顺序。 |
| `player.play` / `command` | `playbackContextId:R`、`baseControlVersion:R int>=0`、`positionMs:O int>=0` | 验证 authority/cursor 后递增控制版本，并向 authority 发送无 target 的第 6.6 control。 |
| `player.pause` / `command` | 同 `player.play` | 同上。 |
| `player.seek` / `command` | `playbackContextId:R`、`baseControlVersion:R int>=0`、`positionMs:R int>=0` | 同上，`positionMs` 必须有。 |
| `player.next` / `command` | `playbackContextId:R`、`baseControlVersion:R int>=0` | 同上。 |
| `player.prev` / `command` | 同 `player.next` | 同上。 |

所有 player control 的请求者必须具有 controller 角色。服务端按 action 校验当前 authority 的
negotiated `canPlay` / `canPause` / `canSeek`；请求 controller 不需要具备对应播放能力。能力不足
返回 `capability_required`，authority 离线返回 `authority_offline`，两者都不得改变 cursor。
`queue.playItem`、`player.next`、`player.prev` 要求 authority `canPlay:true`。

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

`effectiveAtPlayback:true` 与 `playbackPrepare:false` 的 capability 组合非法，注册必须返回
`bad_request`。Handoff 发起者必须具有 controller 角色；source 必须是当前在线 authority 且
negotiated `canPause:true`；target 必须是同用户在线 player，并协商到 `playbackPrepare:true`、
`effectiveAtPlayback:true`、`canPlay:true`。只有服务端 Handoff conformance tests 已通过时才
开放该 profile，否则 negotiated handoff capabilities 为 false，请求返回 `capability_required`。

`playback.handoff.start.payload.targetClientId` 是整个 strict-v2 客户端请求 surface 中唯一允许的
payload target。它只用于让服务端选择接管设备；服务端必须解析并授权该目标，然后按目标
Socket 投递无 `targetClientId` 的 `playback.prepare`。不得把该请求字段复制进任何业务 push。

| Action / type | payload | 服务端动作与响应 |
| --- | --- | --- |
| `playback.handoff.start` / `command` | `playbackContextId:R`、`targetClientId:R`、`baseControlVersion:R int>=0` | 原子创建 handoff 后返回 ACK，并给 target 发第 6.7 节 prepare；不同 Socket 间不承诺到达顺序。ACK payload **只允许且必须**有 `action`、`handoffId`、`prepareId`、`status:"preparing"`、`controlVersion`。 |
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
- `completed`、`failed`、`cancelled`、`timedOut` 是终态。终态重放不产生副作用。
- `playback.handoff.status` / `cancel` 发给全部当前 Context subscribers；prepare 与 commit 只发
  target，release 只发 completed 后的旧 authority。complete 后再广播新的 Context status。
- authority 永久离线时，2.x 不提供强制接管。controller 关闭旧 Context，再由目标 player 使用
  新的 playbackContextId 创建任务；旧 ID 因 tombstone 不可复用。

Handoff `errorCode` 必须匹配 `^[a-z][a-z0-9_]{0,63}$`。服务端标准值固定为
`prepare_failed`、`prepare_timeout`、`commit_timeout`、`target_disconnected`、
`source_disconnected`、`server_restart`。target 可在 `playback.ready.ready:false` 中返回符合相同
格式的稳定扩展码。`errorMessage` 不得包含凭据、文件路径、堆栈或内部数据库信息。

### 5.5 Broadcast（仅 negotiated capability `supportsBroadcast:true`）

Broadcast 是独立可选 profile。participant 必须是同用户在线 player，并协商到
`supportsBroadcast:true`、`canPlay:true`、`canPause:true`、`canSeek:true`。只有本节全部 schema、
权限、cursor 和 conformance tests 已通过时才能开放，否则返回 `capability_required`，不得提供
部分实现。发起或控制 Broadcast 的连接也必须协商到 `supportsBroadcast:true`，但 controller-only
owner 不需要具备本地播放能力，也不会因此自动成为 participant。

| Action / type | payload | 服务端动作与响应 |
| --- | --- | --- |
| `broadcast.start` / `command` | `playbackContextId:R`、`participants:O non-empty distinct string[]`、`queueSongIds:R non-empty distinct string[]`、`currentIndex:R int>=0`、`positionMs:R int>=0`、`autoPlay:O bool` | Context authority 或有控制权的 controller 创建；`currentIndex < queueSongIds.length`。省略 participants 时选择全部合格在线 player，并按 clientId 排序。ACK 只允许并必须返回 `action`、`started:true`、`broadcastId`、最终 `participants`、`skippedClientIds`。 |
| `broadcast.status` / `state` | `playbackContextId:R`、`broadcastId:R` | ACK 返回 `broadcast` 与 `participantStates`。 |
| `broadcast.play` / `command` | `playbackContextId:R`、`broadcastId:R` | ACK 并向 participants 推送 canonical `broadcast.play`。 |
| `broadcast.pause` / `command` | `playbackContextId:R`、`broadcastId:R` | 同上。 |
| `broadcast.seek` / `command` | `playbackContextId:R`、`broadcastId:R`、`positionMs:R int>=0` | 同上。 |
| `broadcast.playItem` / `command` | `playbackContextId:R`、`broadcastId:R`、`queueIndex:R int>=0` | 同上；服务端状态应含 canonical queue / index。 |
| `broadcast.queue.sync` / `state` | `playbackContextId:R`、`broadcastId:R`、`queueSongIds:R non-empty distinct string[]`、`currentIndex:R int>=0`、`positionMs:R int>=0`、`baseQueueRevision:O int>=0`、`baseControlVersion:O int>=0` | 队列内容变化时 baseQueueRevision 必需；index、该 index 的 trackId 或 position 变化时 baseControlVersion 必需。验证后 ACK 并广播 canonical queue state。 |
| `broadcast.stop` / `command` | `playbackContextId:R`、`broadcastId:R` | ACK 并向 participants 广播 terminal `broadcast.stop`。 |

start 的 authenticated client 是 `ownerClientId`。owner 和当前 Context authority 可以执行
play/pause/seek/playItem/queue.sync/stop；普通 participant 只能请求 status，不能控制。所有参与者
必须满足上述 negotiated 条件。显式列表中同用户但离线、非 player 或能力不足的目标放入
`skippedClientIds`；任何跨用户目标使整个请求返回 `forbidden`；最终没有可用 participant 时返回
`bad_request`。服务端必须串行化同一 broadcast 的 mutation；提供或按变化条件必须提供的 base cursor 必须精确校验，冲突返回
`stale_version`。stop 进入不可逆 terminal 状态；之后的 mutation 返回 `conflict`，重复 stop 幂等。
owner 断开不自动停止，只要 authority 在线；authority 断开时暂停并广播状态，30 秒内未恢复则
terminal stop。authority 在 30 秒内以相同 clientId/deviceSessionId 重连时取消终止定时器，
Broadcast 保持 active 但 paused，必须显式 `broadcast.play` 才恢复。所有 push 逐 sid 注入各自
provenance，不能把一个 participant 的 nonce 复用给其他人。

当前 Context authority 是 Broadcast 的强制 participant。省略 participants 时自然包含它；显式
participants 未列出时服务端将其加入最终列表。authority 离线返回 `authority_offline`；authority
未协商完整 Broadcast 执行能力返回 `capability_required`，不得创建 Broadcast。

## 6. 服务端到客户端：strict 推送与直接响应

下面所有业务消息都必须有顶层 `connectionNonce`、`connectionEpoch`，且不得有任何层级的 `sessionId`。除 `system.*` / `device.list` 外，strict 业务消息不应有 `targetClientId`。

### 6.1 `playback.context.create`：直接创建回执

同 `requestId` 的直接 response：

```json
{
  "type": "state",
  "action": "playback.context.create",
  "requestId": "create-1",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "authorityClientId": "phone-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "trackId": "song-1",
    "state": "playing",
    "positionMs": 0,
    "queueRevision": 1,
    "controlVersion": 1,
    "version": 1,
    "epoch": 1,
    "timelineId": "timeline-1",
    "serverUpdatedAtMs": 1780000000000
  }
}
```

snapshot 必需字段：`playbackContextId`、`authorityClientId`、非空 `queueSongIds`、合法 `currentIndex`、`state`、`positionMs`、`queueRevision`、`controlVersion`、`version`、`epoch`。`trackId` 可省略；若提供，必须等于 `queueSongIds[currentIndex]`。
创建时 authority 必须是请求 player，Context 同时持久化其 `authorityDeviceSessionId`（内部路由字段，
不属于本 snapshot）。创建 Socket 自动加入订阅；断线后仍按第 4.4 节清除临时订阅。

### 6.2 `playback.context.status`：水合和广播的权威快照

```json
{
  "type": "state",
  "action": "playback.context.status",
  "requestId": "status-1",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContext": {
      "playbackContextId": "playback:user:main",
      "authorityClientId": "phone-1",
      "queueSongIds": ["song-1", "song-2"],
      "currentIndex": 0,
      "trackId": "song-1",
      "state": "playing",
      "positionMs": 1200,
      "queueRevision": 1,
      "controlVersion": 1,
      "version": 1,
      "epoch": 1,
      "timelineId": "timeline-1",
      "serverUpdatedAtMs": 1780000001200
    },
    "deviceStates": [
      {
        "playbackContextId": "playback:user:main",
        "clientId": "phone-1",
        "deviceSessionId": "device:phone-1",
        "state": "playing",
        "trackId": "song-1",
        "positionMs": 1200,
        "volume": 60,
        "muted": false,
        "clientSeq": 7,
        "serverUpdatedAtMs": 1780000001200
      }
    ]
  }
}
```

`deviceStates` 必须是 object array，且每个项目必须且只允许 `playbackContextId`、`clientId`、
`deviceSessionId`、`state`、`positionMs`、`clientSeq`、`serverUpdatedAtMs`，以及可选 `trackId`、
`volume`、`muted`。每个项目的 playbackContextId 必须等于主 snapshot；一个 clientId 或
deviceSessionId 只能出现一次。作为 direct response 时顶层带原 requestId；作为后续 hydration
push 时必须省略 requestId。

### 6.3 `playback.context.closed`

```json
{
  "type": "event",
  "action": "playback.context.closed",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {"playbackContextId": "playback:user:main"}
}
```

payload 只能有 `playbackContextId`；不得有 `sessionId` 或 `targetClientId`。

### 6.4 `queue.context.sync`

```json
{
  "type": "state",
  "action": "queue.context.sync",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "authorityClientId": "phone-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 1,
    "positionMs": 0,
    "queueRevision": 8,
    "controlVersion": 12,
    "version": 33,
    "epoch": 1,
    "timelineId": "timeline-1",
    "serverUpdatedAtMs": 1780000000000
  }
}
```

必需：context、authority、非空 queue、合法 index、position、queueRevision、controlVersion、
`version`、`epoch`、`serverUpdatedAtMs`。`timelineId` 可选；未启用 timeline 时省略。

### 6.5 `playback.update`

服务端将客户端 feedback canonicalize 后广播：

```json
{
  "type": "event",
  "action": "playback.update",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "sourceClientId": "phone-1",
    "deviceSessionId": "device:phone-1",
    "state": "playing",
    "trackId": "song-1",
    "positionMs": 1200,
    "volume": 60,
    "muted": false,
    "clientSeq": 7,
    "serverUpdatedAtMs": 1780000001200
  }
}
```

必需：`playbackContextId`、`sourceClientId`、`deviceSessionId`、`state`、`positionMs`、`clientSeq`。
禁止：`sessionId`、`authorityClientId`、`queueSongIds`、`currentIndex`、`queueRevision`、
`controlVersion` 等 context snapshot 字段。即使 source 是 authority，该 feedback 也只更新
device state；服务端不得因此修改 Context 或额外广播一个内容未变化的 Context status。

### 6.6 server-routed 控制

服务端应先接受第 5.2 的 command，再向当前 authority Socket 发送**无 target** command。所有 context subscribers 另收 `status` / `queue.context.sync` / `playback.update` 事实状态。

本节描述的是服务端 → 客户端的 accepted control。这里禁止 `baseControlVersion`，只允许
canonical `controlVersion`；它不改变第 5.2 节客户端 → 服务端请求必须携带
`baseControlVersion` 的要求。

#### 6.6.1 多客户端下如何确定唯一执行者

`playbackContextId` 是 player control 的**业务路由地址**，不是要求所有客户端自行过滤的
广播主题。每个 PlaybackContext 必须由服务端维护唯一的当前 authority，并完成
以下映射：

```text
playbackContextId
  -> authorityClientId
  -> 当前有效 Socket.IO sid
  -> 该 sid 的 connectionNonce / connectionEpoch
```

服务端必须保存 `authorityDeviceSessionId`，解析出的 Socket 必须同时属于该 clientId 与
deviceSessionId；不能只因 `clientId` 文本相同就把命令发给旧连接或另一设备实例。

服务端收到第 5.2 节的 player control 后，必须按以下顺序处理：

1. 使用请求的 `playbackContextId` 读取未关闭且属于当前用户的 Context。
2. 校验请求者具有 controller 权限、属于允许的控制范围，并验证请求中的
   `baseControlVersion`。
3. 从 Context 读取当前 `authorityClientId`，必要时同时读取
   `authorityDeviceSessionId`。
4. 从服务端连接注册表解析该 authority 当前唯一有效的 Socket.IO `sid`，并确认该
   `sid` 仍绑定到同一用户、client 和 device session。
5. 若 authority 离线、映射缺失或已经被新连接替换，向请求者返回 correlated
   `system.error`（code 必须为 `authority_offline`），不得广播控制命令，也不得回退
   legacy/session 路由。
6. 接受命令并生成新的 canonical `controlVersion`；`sourceClientId` 写入发起控制的
   controller，而不是收件人。
7. 使用 authority `sid` 对应的 nonce/epoch 构造 envelope，并通过 Socket.IO
   `to=<authority sid>` 单播；不得向 Context room 或 namespace 广播这条执行命令。
8. 向原请求者返回 action-correlated ACK。另行向 Context subscribers 广播
   `playback.context.status` 等事实状态；状态广播不负责触发音频执行。

示意代码中的函数名不是服务端公共 API 要求，但投递行为必须等价：

```python
context = get_context(request_payload["playbackContextId"])
authority_sid = get_current_sid(context["authorityClientId"])

command = build_control(
    playback_context_id=context["playbackContextId"],
    control_version=accepted_control_version,
    source_client_id=requesting_client_id,
)
command = bind_recipient_provenance(authority_sid, command)

socketio.emit("message", command, to=authority_sid, namespace="/emo")
```

例如同一 Context 中存在手机 A、平板 B、Windows C：A 与 B 都是 controller，C 是当前
authority。A 发出 `player.seek` 后，服务端只把第 6.6 节的 command 单播给 C；B 不接收，
A 也不会因为自己是请求者而执行该命令。A 收到 ACK，A/B/C 随后都可以收到新的
`playback.context.status`（前提是对应 Socket 已订阅该 Context）。因此 command payload
不需要也不得携带收件人 `targetClientId`：收件人已经由 Socket.IO 的 `to=<sid>` 决定。

`connectionNonce` / `connectionEpoch` 必须属于**收件 authority 当前物理连接**，不能沿用
请求者的值。Handoff 完成并更新 `authorityClientId` 后，后续相同 Context 的控制命令必须
自动解析并单播给新的 authority；旧 authority 不再接收执行命令。

普通 player control：

```json
{
  "type": "command",
  "action": "player.seek",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "controlVersion": 13,
    "sourceClientId": "controller-1",
    "positionMs": 42000
  }
}
```

`player.play` / `pause` 可有 `positionMs`；`seek` 必须有；`next` / `prev` 不得有。所有普通 control 必有 `playbackContextId`、正 `controlVersion`、`sourceClientId`，并禁止 `baseControlVersion` 与 `targetClientId`。

队列选项控制：

```json
{
  "type": "command",
  "action": "queue.playItem",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "queueSongIds": ["song-1", "song-2"],
    "queueIndex": 1,
    "queueRevision": 8,
    "controlVersion": 13,
    "sourceClientId": "controller-1"
  }
}
```

所有这些字段必需，revision/version 均须 `>= 1`；禁止 legacy fields、`positionMs`、`trackId`、`currentIndex`、`targetClientId`。

### 6.7 Handoff prepare

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

### 6.8 Handoff commit、release、status、cancel

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

### 6.9 Broadcast 推送与 status 结果

服务端发给 participants 的 `broadcast.start`、`broadcast.play`、`broadcast.pause`、
`broadcast.seek`、`broadcast.playItem`、`broadcast.queue.sync`、`broadcast.stop` 均使用无 target
envelope。除下文列出的条件字段外，payload 必须是完整 `BroadcastSnapshot`：

```json
{
  "playbackContextId": "playback:user:main",
  "broadcastId": "broadcast-1",
  "ownerClientId": "controller-1",
  "authorityClientId": "phone-1",
  "queueSongIds": ["song-1", "song-2"],
  "currentIndex": 0,
  "trackId": "song-1",
  "positionMs": 1200,
  "state": "playing",
  "version": 10,
  "queueRevision": 8,
  "controlVersion": 13,
  "epoch": 1,
  "serverUpdatedAtMs": 1780000001200,
  "playbackRate": 1.0,
  "participants": ["phone-1", "desktop-1"]
}
```

上述字段全部必需；`trackId` 可选且若提供必须等于当前队列项。`state` 只能是
`playing|paused|stopped`，`playbackRate` 必须大于 0。`broadcast.play`、`pause`、`seek`、
`playItem` 还必须增加正整数 `effectiveAtServerMs` 和 `serverTimeMs`；其他 broadcast push 禁止
这两个字段。每次 mutation 令 `version +1`；queue.sync/playItem 令 `queueRevision +1`，
play/pause/seek/playItem 令 `controlVersion +1`。`broadcast.stop` 必须返回完整 snapshot 且
`state:"stopped"`，之后 cursor 冻结。

`broadcast.status` ACK payload 只允许以下字段：

```json
{
  "action": "broadcast.status",
  "broadcast": {
    "playbackContextId": "playback:user:main",
    "broadcastId": "broadcast-1",
    "ownerClientId": "controller-1",
    "authorityClientId": "phone-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "trackId": "song-1",
    "positionMs": 1200,
    "state": "playing",
    "version": 10,
    "queueRevision": 8,
    "controlVersion": 13,
    "epoch": 1,
    "serverUpdatedAtMs": 1780000001200,
    "playbackRate": 1.0,
    "participants": ["phone-1", "desktop-1"]
  },
  "participantStates": [
    {
      "broadcastId": "broadcast-1",
      "clientId": "phone-1",
      "state": "playing",
      "positionMs": 1200,
      "online": true,
      "clientSeq": 7,
      "serverUpdatedAtMs": 1780000001200
    }
  ]
}
```

`broadcast` 必须是完整 `BroadcastSnapshot`。`participantStates` 必须覆盖最终 participants，并按
`clientId` 升序；同一 client 只能出现一次。每项必须且只允许 `broadcastId`、`clientId`、
`state`、`positionMs`、`online`，以及成对可选的 `clientSeq`、`serverUpdatedAtMs`。尚未收到该
participant feedback 时，省略这两个可选字段，state/position 使用 BroadcastSnapshot 的初始
canonical 值；收到首个 feedback 后两字段必须同时出现且 clientSeq >= 1。不得用 clientSeq:0 或
JSON null 表示“尚无 feedback”。push action 的 payload 不含 `action`；correlated status ACK 的
payload 必须以 `action:"broadcast.status"` 开头。

## 7. 服务端实现要求（EARS）

**REQ-001 — ACK correlation**  
当服务端收到具有合法 requestId/action 的 strict request 时，服务端必须按第 4.3 节使用相同
`requestId` 结算，并在 `system.ack` / `system.error` 的 `payload.action` 写入原 action。业务 push
不得复用该 requestId。缺失合法 requestId/action 时记录并断开。

**REQ-002 — 注册协商**  
当 `device.register.capabilities.playbackContextV2` 为 `true`、请求合法且 Core ready 时，服务端必须
在 ACK 中提供完整 strict-v2 metadata 与 `negotiatedCapabilities`。Core 未 ready 时返回
`not_supported`，不得静默转 legacy。

**REQ-003 — 主版本兼容**  
当服务端继续声称 `protocolVersion` 为 `2.x` 时，服务端必须保持已有 strict-v2 wire contract 向后兼容；当存在破坏性变更时，服务端必须升主版本。

**REQ-004 — 收件人 provenance**  
在 strict 注册成功后，服务端每次向该 Socket emit 任一 envelope 时，服务端必须注入与该物理 Socket 对应的 `connectionNonce` 和 `connectionEpoch`。

**REQ-005 — Context routing**  
当服务端处理 context command 时，服务端必须以 `playbackContextId`、membership、authority 和 cursor 决定授权与收件人；服务端不得使用 `sessionId` 作为 strict 播放主键。

**REQ-006 — 无 direct-target strict 业务 push**  
当服务端向 strict client 推送业务动作时，服务端必须按 recipient Socket 逐个发送无 `targetClientId` 的规范 envelope，而不是把 action payload 变成 direct-target command。

**REQ-007 — 失败闭合**  
当请求 payload、capability、context membership 或 cursor 无效时，服务端必须返回 correlated `system.error`，且不得退回 legacy/session action。
缺失合法 requestId/action 的请求按第 2.2 节直接断开，是唯一无法 correlated error 的例外。

**REQ-008 — 可选模式**  
当连接未在 `negotiatedCapabilities` 获得 Follow、Broadcast 或 Handoff 所需能力时，服务端不得
向该连接执行相应可选模式动作，并对请求返回 `capability_required`。

**REQ-009 — Handoff target 的方向性例外**  
当客户端发送 `playback.handoff.start` 时，服务端必须接受并验证 payload 内的
`targetClientId`；当服务端向目标 Socket 或其他 context 成员推送 Handoff 消息时，服务端
不得在 envelope 或 payload 中复制该字段。

**REQ-010 — 2.x cursor 兼容**  
当客户端发送第 5.2 节定义的控制或队列请求时，服务端必须继续接受其
`baseControlVersion` / `baseQueueRevision` 前置条件；服务端不得因为 accepted push 只使用
`controlVersion` / `queueRevision`，就在 `2.x` 内删除请求字段。

**REQ-011 — 唯一结算**  
当服务端成功处理 strict request 时，服务端必须使用第 4.3 节为该 action 指定的唯一结算
方式；服务端不得用 ACK 和 direct response 重复结算。

**REQ-012 — 幂等重放**  
当同一物理连接重复发送同一 requestId 时，服务端必须重放缓存结果且不重复副作用；当
requestId 相同但内容不同时，服务端必须返回 `conflict`。缓存至少保留 60 秒，断开时清除；
event-confirmed 重放只发给请求 Socket。

**REQ-013 — Cursor 单调性**  
当服务端接受 Context mutation 时，服务端必须只按第 4.5 节矩阵递增对应 cursor；当 base
cursor 不等于 canonical cursor 时，服务端必须返回 `stale_version` 且不执行 mutation。

**REQ-014 — Schema 闭合**  
当 strict request 含 action schema 未声明的字段、错误类型或超限值时，服务端必须返回
`bad_request`；服务端发出的 strict envelope 也不得含未声明字段或 JSON null 占位。

**REQ-015 — Profile readiness**  
当某个 optional capability profile 尚未通过完整 conformance 时，服务端必须把它保持关闭并
返回 `capability_required`，不得以部分 handler 声称支持。

**REQ-016 — 角色与执行能力**  
当服务端授权 Context action 时，服务端必须按同用户域、player/controller 角色和收件 authority
的 negotiated `can*` 能力共同判断；不得用请求者的播放能力替代执行 authority 的能力。

**REQ-017 — 持久化终态与重启**  
当 Context close 或服务重启时，服务端必须按第 4.4、7.2 节持久化 tombstone/cursor 并显式终止
瞬态 profile，不得恢复半完成 Handoff、Follow 或 Broadcast。

**REQ-018 — 接受与发送顺序**  
当服务端接受 authoritative mutation 时，必须先解析并验证全部收件人，然后完成原子状态与
持久化提交，才能发送 ACK 或 canonical business push。不同 Socket 之间不承诺全局到达顺序；
发送前发现 authority 无有效绑定时不得 mutation。

**REQ-019 — Nonce 强度**  
当服务端建立新物理 Socket 时，必须使用 CSPRNG 生成至少 128 bit 随机熵的 connectionNonce，
不得复用或使用可预测输入构造。

**REQ-020 — Event confirmation 重放**  
当 event-confirmed request 命中相同 fingerprint 的 request cache 时，服务端必须只向重复请求
Socket 发送第 4.4 节规定的 confirmation，且不得重复任何状态机或广播副作用。

**REQ-021 — Handoff 与 Broadcast 边界**  
当服务端输出 Handoff errorCode 或 Broadcast participantStates 时，必须分别遵守第 5.4 节的稳定
码格式和第 6.9 节的初始/已反馈 paired-field 规则。

**REQ-022 — 超限分层与顺序保持**  
当 transport message 超限时服务端必须关闭连接；当已解析业务字段超限时返回 correlated
bad_request。任何序列化、持久化或重启恢复都不得排序 queueSongIds，集合字段必须按第 4.6 节
确定性输出。

### 7.1 安全与资源限制

1. 除仅绑定 loopback/LAN 且由开发者明确接受风险的本地 lab 外，端点必须使用 TLS
   (`https`/`wss`)；`auth.login` 含明文密码字段，公网明文 HTTP/WebSocket 禁止部署。
2. 服务端不得记录 `auth.login.payload`、密码、完整凭据或包含它们的原始 envelope。审计日志
   只记录 requestId、action、认证后的 user/client、结果 code、延迟和脱敏端点。
3. 生产环境 Engine.IO/Socket.IO Origin 必须使用部署 allowlist；不得在携带凭据时使用通配符
   `*`。只有显式开发模式可启用 `*`，且启动日志必须输出安全警告。非浏览器客户端没有 Origin
   时，必须依靠认证和网络策略，不能伪造 Origin 放行。
4. 默认资源上限：每 IP 同时 10 条未认证连接、每用户 20 条已认证连接；每连接每分钟 120 个
   strict 请求，其中 player control 每秒最多 20 个、create/handoff/broadcast start 每分钟各
   10 个。部署可调低，调高必须有负载测试证据。超限返回 `rate_limited` 或在握手阶段拒绝连接。
5. 必须设置 Engine.IO payload 上限不高于 256 KiB；transport 超限使用 message-too-big 行为断开，
   已进入 handler 的业务限制超限返回 correlated `bad_request`。malformed JSON、非 object
   envelope 不得进入 handler；格式合法但不在 allowlist 的 action 返回 `not_supported`；缺失或
   非法 action/requestId 时按第 2.2 节断开。
6. 每个 action 在执行前重新校验 authenticated user、Context membership、角色、capability 和
   当前 sid 绑定；不能只在 subscribe 时授权一次。错误消息不得泄露其他用户资源是否存在。
7. 必须配置 ping/pong dead-connection cleanup、发送缓冲上限和背压策略。控制命令不可作为
   volatile broadcast；无法可靠单播给 authority 时返回 `authority_offline`。

### 7.2 单实例、持久化与重启

- strict-v2 2.x 当前只支持单 realtime worker。服务端在能够识别 Gunicorn/processes/workers 等
  多进程配置时必须启动失败，不得仅警告后继续运行。多 worker 支持必须另立工程目标，并同时
  提供 sticky sessions、跨 worker broker、共享原子 Context、共享 sid/subscription/dedupe store。
- active Context 与 closed tombstone 必须持久化。重启恢复时保留
  `epoch/version/queueRevision/controlVersion` 与 authority client/device identity；清空全部 sid、
  nonce、connectionEpoch、request cache 和 subscription，等待客户端重新注册、subscribe/status。
- graceful restart 必须先停止接收新连接，完成或明确失败正在结算的请求，停止创建新
  handoff/broadcast，再关闭 Socket。不能把未完成 handoff 恢复为 completed。
- 重启时非终态 Handoff 进入 `failed`，`errorCode:"server_restart"`；Follow 全部清除；active
  Broadcast 进入 terminal stopped 并冻结 cursor。基础 Context 继续保留。
- `connectionEpoch` 每个新物理连接固定为 1；不得持久化或复用旧 nonce。

### 7.3 Capability profile readiness

Core profile 等于握手/注册、PlaybackContext、Queue 和 Player Control，缺一不可；服务端只有在
这些 action 的 request/response、cursor、routing、dedupe 和 error conformance 全部通过后才可
接受 `playbackContextV2:true`，否则注册返回 `not_supported`。Handoff、Follow、Broadcast 是三个
独立 profile，部署默认关闭；每个 profile 只有在本文对应状态机和双客户端 conformance 测试完成
后才能在 `negotiatedCapabilities` 返回 true。metadata/profile 的 TOFU 成功不自动证明或开启
任一可选 capability。

## 8. 当前不应实现为 strict-v2 的旧 surface

| 禁止作为 strict 方案 | 原因 |
| --- | --- |
| `session.subscribe` / `session.unsubscribe` | strict 改用 `playback.context.subscribe` / `unsubscribe`。 |
| `queue.session.sync` / `queue.local.set` / `queue.ready.complete` | strict queue 是 `queue.context.sync`，旧队列消息会被 router quarantine。 |
| `sessionId`、`sourceSessionId` | strict playback 主键只能是 `playbackContextId`；设备稳定身份是 `deviceSessionId`。 |
| 服务端业务 push / direct response 的 `targetClientId` | strict router 拒绝，服务端应按 Socket recipient 分发。客户端请求的唯一例外是 `playback.handoff.start.payload.targetClientId`。 |
| `player.setVolume` / `player.requestState` | 当前 strict action allowlist 未纳入；不要为“补齐所有旧 action”擅自设计 strict shape。 |
| `auth.login` / `device.register` actionless ACK | probe/negotiated client 会拒绝。 |

`canSetVolume` 仍是设备能力描述和未来兼容位，但在 2.1.0 中没有对应 strict action；它为 true
不表示客户端或服务端可以发送 `player.setVolume`。

## 9. 已知边界与联调验收

这是从 Flutter 客户端提取的协议，不证明当前任何服务端部署已经实现。服务端完成后必须验证：

1. probe 注册成功后客户端保存 profile、主动重连、第二次 negotiated 注册成功；
2. roles 单角色/双角色、完整 negotiatedCapabilities、Core not_supported 和 optional profile 降级；
3. `2.1.x` 的兼容部署只更新 hash/commit 观测；`3.x`、缺字段、非法 hash/commit、actionless ACK 均关闭 strict；
4. 每个 strict push 的 nonce/epoch 完全匹配该 Socket 的 register ACK，业务 push 不带 requestId；
5. context create / subscribe / status / conditional queue sync / player control 的唯一结算、canonical push 和 cursor conflict；
6. create 同 ID 新 requestId 重试、同 requestId 60 秒缓存重放、内容冲突、断线清 subscription 和重连水合；
7. Cursor 矩阵每一行的递增、不递增、旧值拒绝、等值去重、必需 clientSeq 和三个 event-confirmed action 的精确单 Socket 重放；
8. authority clientId/deviceSessionId 双重绑定、旧 sid 强制断开，以及永久离线后 close + 新 Context 恢复；
9. Handoff 的 prepare -> ready -> effective-at commit -> complete -> authority change -> release，
   以及 8 秒/5 秒超时和 source/target 断线；
10. Follow、Handoff 与 Broadcast 仅在 negotiated capability 为 true 且 profile ready 时测试，包括 owner/authority 权限、
   participant 禁止控制、Handoff errorCode 和 terminal 幂等；
11. Broadcast 初始 participantStates 省略 paired feedback 字段，首个 feedback 后两字段同时出现；
12. transport message-too-big 断开、business limit bad_request、非法 requestId/action 断开，以及未知字段/null/rate 限额；
13. nonce CSPRNG/128-bit 熵，以及 queueSongIds 在序列化、持久化和重启后的顺序保持；
14. 单 worker 启动保护、Context/tombstone 重启恢复，以及 Handoff/Follow/Broadcast 显式终止；
15. Android 与 Windows 各一台真实 Flutter 客户端联调，记录可复现日志。

## 10. 协议权威性、证据来源与部署证据

本文件是服务端工程师与 Flutter 工程师共同核对的唯一完整 wire contract。其他材料的职责为：

- `ref/emosonic_strict_v2_protocol_metadata_goal.md`：只定义 `device.register` metadata 描述符及其 hash 语义；不覆盖业务 action；
- `ref/emosonic_strict_v2_server_change_note.md`：只记录某个服务端版本声称完成的行为；不得覆盖本文件；
- Flutter `test/fixtures/emo_protocol/strict_v2/manifest.json`：从本文派生的 machine-readable
  conformance inventory；不得覆盖本文，必须由 Flutter conformance tests 保持字段清单一致；
- Flutter `test/fixtures/emo_protocol/strict_v2/`：canonical fixture 根目录；fixture 必须存在且
  SHA-256 匹配。服务端仓库可维护等价测试数据，但不得改变 wire shape。

本文最初从以下 Flutter 代码位置提取，后续实现应通过 conformance tests 保持一致：

- `lib/services/emo_action_contract_policy.dart`：strict 出站 allowlist、envelope type、字段验证；
- `lib/services/emo_realtime_client.dart`、`emo_socket_connection.dart`：Socket transport、握手、ACK、runtime provenance；
- `lib/services/emo_message_router.dart`：入站 action 路由与 quarantine；
- `lib/services/emo_strict_v2_models.dart`：context、queue、control、feedback 的严格入站解析；
- `lib/services/emo_handoff_controller.dart`、`emo_broadcast_controller.dart`：Handoff / Broadcast 时序和 result 读取；
- Flutter `test/fixtures/emo_protocol/strict_v2/manifest.json` 与同目录 fixtures：客户端 target
  inventory 与可验证证据，不是服务端部署实现或 production readiness 证明。

仍需由服务端部署和真实设备提供的非代码证据是：授权规则的 conformance 结果、单 worker
启动保护、Context 持久化与重启结果、Android/Windows 双客户端日志、发布与回滚演练记录。协议规则
本身已在本文固定；缺少部署证据时必须保持对应 capability 未就绪，不得自行改写 envelope、
correlation、provenance、cursor、routing 或 fail-closed 语义。
