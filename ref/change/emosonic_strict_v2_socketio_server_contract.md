# EmoSonic strict-v2 Socket.IO 服务端契约

> 文档状态：Normative（服务端与 Flutter 联调的唯一完整 wire contract）
> 文档修订：2026-07-17-r11
> 协议版本：`2.4.0`
> 读者：EmoSonic 服务端与 Flutter 工程师
> 范围：PlaybackContext v2 strict-v2。本文定义客户端与服务端应发送、应接受的协议契约；本轮不保留 `2.3.x` 客户端 shape 或旧 `sessionId` 协议兼容，也不授予 production rollout 权限。

本文件在仓库中的规范路径是
`specs/emosonic_strict_v2_socketio_server_contract.md`。当注册 metadata Goal、服务端变更说明、
实现代码、测试名称或历史文档与本文件冲突时，不得用它们静默改写本契约；应将冲突视为
conformance 缺陷并修正文档或实现。注册 metadata Goal 只定义握手描述符，服务端变更说明
只记录某次实现状态，二者都不是全量 wire contract。

本 r11 继续使用尚未发布的 strict-v2 `2.4.0` 单一 shape：待机 Context、启动 ensure 和 prepare
规则保持不变；Windows 实际事实与远程执行结果继续使用条件闭合的 `playback.update`，服务端主动结束
且没有新设备事实的事务改用 server-only `playback.control.settled`。r11 替代从未发布的 r10，不形成
r10 兼容承诺；同时删除未发布的 `player.authorityIntent`，不保留旧的非空 Context-only snapshot 或
兼容分支。只有本文全部 conformance tests 和真实双客户端联调通过后才可标记 ready。r11 冻结或发布
后的 wire shape 变化必须同步更新 protocolVersion 和本文，不得静默漂移。

## 1. 一次性结论

服务端应把 strict-v2 `2.4.0` 实现为一套以 `playbackContextId` 为唯一播放任务主键的 Socket.IO 协议：

- 传输使用 Socket.IO namespace `/emo`、事件名 `message`；
- 所有 `system.ack` / `system.error` 必须以同一 `requestId` 关联，且 `payload.action` 必须回显原 action；direct action response 使用第 2.2、4.3 节规定的同 requestId 关联方式；
- strict 注册成功后，所有发给 strict recipient 的入站 envelope 必须由统一发送工厂附加该 Socket 的顶层 `connectionNonce` 和 `connectionEpoch`；
- strict-v2 业务 payload 中不得出现 `sessionId`。客户端请求不得使用顶层 `targetClientId`；payload target 只允许 `playback.handoff.start.targetClientId`，以及 `device.setVolume` 的 `targetClientId` / `targetDeviceSessionId`。服务端业务推送的顶层和 payload 均不得带 target 字段；
- `device.list` 只发现设备；从目标 `authorityClientId` / `authorityDeviceSessionId` 到 active Context
  binding 的映射只能通过 `playback.context.list` 查询，客户端不得从任何 session/device ID 推导
  Context。多结果必须 fail-closed；binding 集合变化由 `playback.context.bindings.changed` 使客户端
  缓存失效；
- 每个完成注册且 `player + canPlay` 的设备必须先读取可用的本地恢复快照，再立即调用
  `playback.context.ensure`，并在在线期间保持一个唯一 active Context。本地已有队列时 ensure 必须
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
- `queue.playItem`、`player.next`、`player.prev` 等切换实际歌曲的远程命令失败时，服务端必须把同一
  Context/epoch 中所有更高版本且仍为 pending 的远程命令一并终止为 failed，固定原因为
  `dependency_failed`；服务端为每个受影响版本发送 server-only `playback.control.settled`，Windows
  必须丢弃这些后续命令，controller 刷新实际状态后才能重新发送；
- 每个 server-routed control 必须携带 `executionTimeoutMs`，默认 15000ms。Windows 收到命令并进入
  本地执行队列后启动执行租约；只有 Windows 能证明租约已失效时才用 remoteCommand failed
  `playback.update(errorCode:"execution_timeout")`。服务端在 accepted/routed 后启动
  `executionTimeoutMs + 2000ms` watchdog；到期仍无 terminal feedback 只能使用 server-only
  `playback.control.settled(errorCode:"execution_unknown")`，不得伪造 Windows feedback；
- authority 断线、Socket 被替换或服务端重启造成结果不明时，服务端以
  `playback.control.settled(errorCode:"execution_unknown")` 逐条结束 pending，旧命令不得在重连后
  自动重发；
- 低于 `lastAppliedControlVersion` 的迟到状态不得写入或广播；服务端必须只向发送该旧状态的
  authority Socket 返回当前已保存的 passive canonical update，使该请求有界结束并让 Windows
  校正本地认知；
- 本契约要求 `protocolVersion` 的 major 为 `2` 且 minor 至少为 `4`。待机 Context、
  `playback.context.ensure`、`playback.context.prepare`、Context discovery 和设备级远程音量全部属于
  Core，不设置旧版本兼容 capability。低于 `2.4.0` 或 major 不为 `2` 时 Flutter 必须 fail-closed；
  `schemaHash` 和 `serverBuildCommit` 是部署观测值，不是要求客户端每次打包固定的 pin；
- Follow、Broadcast、Handoff 受注册时返回的 `negotiatedCapabilities` 控制；服务器不得直接信任客户端请求值，也不应向未协商成功的连接投递可选动作。

必须区分字段方向：客户端请求中的 `baseControlVersion` / `baseQueueRevision` 是并发前置条件；
服务端推送中的 `controlVersion` / `queueRevision` 是接受后的 canonical cursor。服务端推送不带
`baseControlVersion`，不代表服务端可以从 `2.4.x` 请求契约中删除该字段。

`player.setVolume`、`player.requestState`、`session.subscribe`、`queue.session.sync`、`queue.local.set`、`queue.ready.complete` 不属于当前 strict-v2 可用 surface。远程音量必须使用本文定义的设备级 `device.setVolume`，不要复用 legacy player action。

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
5. 客户端 → 服务端：strict 请求的顶层 `targetClientId` 一律禁止；payload target 只允许 `playback.handoff.start.targetClientId`，以及 `device.setVolume.targetClientId` / `targetDeviceSessionId`。这些字段表示业务目标，不是 Socket 投递指令。
6. 服务端 → 客户端：strict 业务 push / direct response 的顶层和 payload 均不得有 target 字段。`system.ack`、`system.error`、`system.pong`、`device.list` 的顶层 transport target 虽可被客户端容忍，但服务端仍应优先按实际 Socket 投递并省略该字段；payload 内始终禁止。
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
  -> player：playback.context.ensure（确保当前逻辑设备只有一个 active Context）
  -> device.list
  -> playback.context.list（按目标 authority/device 发现 Context）
  -> playback.context.subscribe
  -> playback.context.status（水合 canonical queue/playback/cursors）
  -> PlaybackContext v2 commands and pushes
  -> playback.context.bindings.changed 时暂停控制并重新 list/subscribe/status
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
      "supportsBroadcast": false,
      "remoteVolumeControl": true
    }
  }
}
```

字段规则：

- `clientId`、`deviceSessionId`、`deviceName`：非空 string；
- `roles`：非空数组，只允许 `player`、`controller`，不可重复；可声明其中一个或两者；
- `capabilities`：必须且只允许本文示例中的 10 个 bool 字段；不得省略、增加旧版本字段或返回
  多种 capability shape；
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

`remoteVolumeControl:true` 对 controller 表示可发送设备音量命令；对 player 还要求
`canSetVolume:true`，否则协商结果必须为 false。

待机 Context 是 Core，不通过 capability 开关降级。player 执行 ensure 要求 `player` 角色和
`canPlay:true`；controller-only 连接不因 `canPlay:false` 而失去解析 idle Context 和发起 prepare 的
资格。

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
      "supportsBroadcast": false,
      "remoteVolumeControl": true
    },
    "strictV2": {
      "protocolVersion": "2.4.0",
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
| `strictV2.protocolVersion` | 是 | 解析 numeric major/minor；major 必须为 `2` 且 minor 必须 `>=4`，不得用字符串字典序比较；其他版本 fail-closed |
| `strictV2.schemaHash` | 是 | 64 位小写十六进制 SHA-256 |
| `strictV2.serverBuildCommit` | 是 | 40 位小写 Git SHA，或精确值 `unknown` |
| `strictV2.connectionNonce` | 是 | 非空 string |
| `strictV2.connectionEpoch` | 是 | 精确为整数 `1`，不能是 string |
| `negotiatedCapabilities` | 是 | 与请求一致的完整固定 10 个 bool 字段；后续 capability gate 的唯一依据 |

合规服务端只在 `payload.strictV2` 输出 metadata，且只使用 `serverBuildCommit`。Flutter 对
`serverCommit` 或 ACK payload 顶层 metadata 的读取仅是历史部署兼容，不属于合规服务端输出
schema。strict ACK 不返回 `client` 对象；设备详情统一通过 `device.list` 获取。

注册握手描述符必须覆盖 ACK 的 `payload.action`、`clientId`、`deviceSessionId`、固定 10 个 bool
的 `negotiatedCapabilities`，以及 `strictV2` 中的 protocolVersion、schemaHash、
serverBuildCommit、connectionNonce、connectionEpoch。上述字段的名称、类型、required、枚举或
约束发生变化时，必须更新描述符并重新计算 schemaHash；不得继续返回基于旧 ACK shape 的 hash。
`auth.login` 不属于注册描述符。

同一主版本的 `schemaHash` 与 build commit 发生变化时，客户端会更新该服务器的本地观测记录；不会因此降级或 fallback。wire contract 变化时必须同步更新 protocolVersion 和 schema 描述，不得只改实现。

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
          "supportsBroadcast": false,
          "remoteVolumeControl": true
        },
        "alias": "可选",
        "volumeState": {
          "volume": 65,
          "clientSeq": 4,
          "serverUpdatedAtMs": 1780000001200
        }
      }
    ]
  }
}
```

strict 设备对象必须且只允许 `clientId`、`deviceSessionId`、`deviceName`、`roles`、完整协商后
`capabilities`，以及可选非空 `alias`、可选闭合 `volumeState`。`volumeState` 只向请求 capability
形状包含 `remoteVolumeControl` 的 recipient 输出，且只允许 `volume:int 0..100`、
`clientSeq:int>=1`、`serverUpdatedAtMs:int>=0`。不得返回 legacy `sessionId`、`userName`、连接时间或其他
内部字段。设备数组按 `clientId` 稳定排序；roles 输出采用 `player`、`controller` 的固定枚举顺序。

`device.list` 不得返回 `activePlaybackContext`、`playbackContextId` 或其他 Context binding。
设备在线性与持久化播放任务是不同资源；客户端选择 player 后必须按第 5.1 节发送
`playback.context.list`，不得使用 `clientId`、`deviceSessionId` 或旧 `sessionId` 猜测 Context ID。

这是 direct response，不得先发 `system.ack`；`requestId` 必须与 `device.list` 请求相同。

在线设备音量是连接级瞬态状态：设备断开、被同 clientId 的新连接替换或被 stale pruning 移除后，
服务端必须删除其 `volumeState`。本契约不要求离线保存期望音量或在重连时自动执行旧命令。

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

允许的错误码全集如下；服务端不得在 `2.4.x` 中临时创造同义错误码：

| code | 含义 | `retryable` | 条件字段 |
| --- | --- | --- | --- |
| `bad_request` | envelope、字段、类型、枚举、范围或字段组合非法 | `false` | 无 |
| `unauthorized` | 尚未登录或凭据无效 | `false` | 无 |
| `forbidden` | 已登录但无 context/action 权限 | `false` | `playbackContextId` 可选 |
| `not_supported` | action 不属于服务器声明的 `2.4.x` surface | `false` | 无 |
| `not_found` | context、handoff、broadcast 或目标设备不存在 | `false` | `playbackContextId` 可选 |
| `context_closed` | context 已终止 | `false` | `playbackContextId` 必需 |
| `authority_offline` | 当前 authority 没有有效 Socket | `true` | `playbackContextId` 必需 |
| `queue_required` | 请求需要非空 canonical queue，但 Context 仍是 idle；prepare 失败时也表示两端没有可采用队列 | `false` | `playbackContextId` 必需；三个 canonical cursor 必需 |
| `conflict` | 同一逻辑 ID 被用于不同意图，或状态机不允许该动作 | `false` | Context/handoff/broadcast 冲突时必须有 `playbackContextId` 及三个 `current*Version/Revision`；仅 requestId 内容冲突时省略 |
| `stale_version` | base cursor 落后或超前于 canonical cursor | `false` | 对应 `current*` cursor 必需 |
| `client_sequence_conflict` | `clientSeq` 重复但内容不同或倒退 | `false` | `currentClientSeq` 必需 |
| `capability_required` | 连接未协商到动作所需 capability/角色依赖 | `false` | 无 |
| `rate_limited` | 超出连接、用户或 action 限额 | `true` | `retryAfterMs` 必需且为正整数 |
| `internal_error` | 未预期服务端错误 | `true` | 无；不得暴露堆栈、路径或数据库内容 |

`playback.context.list` 的“没有匹配的 active Context”不是错误，必须返回同 requestId 的成功 direct
response，且 `payload.contexts` 为空数组。该查询不得用 `not_found`、`context_closed` 或
`authority_offline` 表示空结果；后两者只可能在客户端随后使用已发现或缓存的 Context ID 执行
status/control 时发生。调用方没有 controller 角色时返回 `forbidden`；未协商
`playbackContextV2:true` 时返回 `capability_required`。

除表中条件字段外，错误 payload 只允许 `action`、`code`、`message`、`retryable`、
`playbackContextId`、`currentControlVersion`、`currentQueueRevision`、`currentVersion`、
`currentClientSeq`、`retryAfterMs`。不适用的可选字段必须省略，不得写 JSON `null`。

`device.register` strict ACK 尚未成功、Socket 还没有绑定 provenance 时，任何具有合法
requestId/action 的失败请求都是 bootstrap error：仍须使用同 `requestId` 的 `system.error` 和正确
`payload.action`，但顶层必须省略 `connectionNonce` / `connectionEpoch`。这包括过早发送的
`device.list`、`playback.context.list` 或其他业务 action，它们返回 `unauthorized`。注册成功后的
任何错误不再享有此例外，必须带该 Socket 的 provenance。

### 4.3 请求结算矩阵

每个 action 的成功结算方式唯一如下：

| action | 唯一成功结算方式 |
| --- | --- |
| `auth.login`、`device.register` | correlated `system.ack` |
| `device.list` | 同 `requestId` 的 direct `device.list` response |
| `device.setVolume` | correlated `system.ack` |
| `playback.context.list` | 同 `requestId` 的 direct `playback.context.list` response，payload 为第 6.1 节 Context binding 列表 |
| `playback.context.ensure` | 同 `requestId` 的 direct `playback.context.ensure` response，payload 为第 6.2 节完整 snapshot |
| `playback.context.status` | 同 `requestId` 的 direct `playback.context.status` response，payload 为第 6.3 节完整 status |
| `playback.context.subscribe`、`unsubscribe`、`close`、`prepare` | correlated `system.ack` |
| `queue.context.sync`、`queue.playItem`、`player.play`、`player.pause`、`player.seek`、`player.next`、`player.prev` | correlated `system.ack` |
| `follow.start`、`follow.stop` | correlated `system.ack` |
| `playback.handoff.start`、`playback.handoff.cancel` | correlated `system.ack` |
| `broadcast.start`、`broadcast.status`、`broadcast.play`、`broadcast.pause`、`broadcast.seek`、`broadcast.playItem`、`broadcast.queue.sync`、`broadcast.stop` | correlated `system.ack` |
| `device.volume.update`、`playback.update`、`playback.context.prepared`、`playback.ready`、`playback.handoff.complete` | event/state-confirmed；服务端不回 ACK，按第 4.5、5.2、5.4、6.0、6.2.3、6.6、6.9 节状态推进 |
| `system.ping` | 同 `requestId` 的 direct `system.pong` response |

`playback.control.settled` 不在客户端请求结算矩阵中：它是第 6.6.5 节 server-only transaction terminal
push，客户端发送该 action 必须返回 `not_supported`。

`playback.context.subscribe` 成功只 ACK。客户端随后显式发送
`playback.context.status` 完成水合；服务端不得把未关联的 status push 当成 subscribe 的结算。
List/Ensure 不允许用简略 ACK 代替 direct response。

### 4.4 幂等、重复请求与重连

1. `playbackContextId` 由服务端在首次 ensure 时生成，客户端不得指定或复用。ID 一旦进入 closed
   tombstone 永久不可复用。重复 ensure 的长期幂等作用域是
   `(authenticated user, stable clientId)`，不是客户端提供的 Context ID。
2. 服务端必须缓存最近 `(connectionNonce, requestId)` 的 request fingerprint 与结算结果至少
   60 秒；Socket 断开时立即清理。客户端在同一连接中永不复用 requestId。缓存期内同一键重复
   到达时原样重放结果，不得再次产生副作用；同一键的 action 或 payload 不同则返回 `conflict`。
   缓存期外的长期幂等由 stable client ensure、Context/Handoff/Broadcast 等逻辑 ID 保证。
3. 重复 subscribe/unsubscribe、close、`follow.start`/`stop`、handoff cancel/complete 和
   `broadcast.stop` 均必须幂等。资源已处于目标状态时返回与首次成功等价的 ACK 或 canonical
   confirmation，不得为 event-confirmed 动作补发 ACK。
4. 每个 Context 同时只能有一个非终态 handoff。同一 source/target 的 start 重试返回已有
   `handoffId`/`prepareId`；不同 target 返回 `conflict`。
5. 同一已认证用户以相同 `clientId` 完成新注册后，新 sid 原子替换旧 sid，并立即断开旧 sid。
   authority 路由还必须匹配持久化的 `deviceSessionId`；相同 clientId、不同 deviceSessionId 的
   新连接不能仅凭注册自动继承旧 authority，只有随后合法的 `playback.context.ensure` 可以按第 11
   条原子重绑离线旧 session。
6. Socket 断开必须清除该 sid 的临时订阅和 client→sid 映射，但不得删除持久化 Context。
   重连 controller 必须使用新的 requestId 重新发送 `playback.context.list`；发现 Context 后重新
   subscribe，再显式请求 status。服务端不得假设 Context binding 查询结果或 room membership
   跨连接保留。同一 clientId/deviceSessionId 重连可以重新发现原 Context；相同 clientId 但不同
   deviceSessionId 的连接在 ensure 完成重绑前不得匹配旧 binding。
7. event/state-confirmed 请求重复且内容相同时，不重新 mutation 或全局广播；服务端只向当前请求
   Socket 重放以下无 requestId 的 canonical confirmation：`device.volume.update` 与 `playback.update` 重放缓存的 canonical
   update；`playback.context.prepared` 重放当前 prepare 结算；`playback.ready` 重放 Handoff 当前 status；`playback.handoff.complete` 重放 completed
   status 与当前 Context status。不得重发 prepare/commit/release、切换 authority 或递增 cursor。
8. Context close 后必须保留持久化 terminal tombstone，`playbackContextId` 不可复用。重复 close
   返回等价 ACK；其他 status/mutation 返回 `context_closed`。正常 Context snapshot 不输出
   `state:"closed"`。服务端先向当前 subscribers/followers 推送 closed，再清除该 Context 的全部
   临时订阅和 Follow relationship。关闭的 Context 必须立即从新的 `playback.context.list` 查询
   中消失，并按第 6.1.2 节向该 authority/device pair 发送 binding 失效通知。
9. `playback.context.list` 是无副作用读取。同一连接、同一 requestId 的重复请求按第 2 条重放
   缓存结果；客户端要观察 close、handoff、authority 或 device registration 变化时必须使用新的
   requestId 重新查询。服务端不得用旧查询缓存覆盖新的 canonical binding。
10. Handoff complete 保持 `playbackContextId` 不变，并原子更新 Context 的
   `authorityClientId` / `authorityDeviceSessionId`。完成后，旧 authority/device pair 的新 list
   查询不得再返回该 Context，新 pair 的查询必须返回同一个 Context ID。Context-level controller
   可以继续使用该 ID；以“控制所选设备”为语义的客户端必须解除旧设备绑定并重新发现。服务端
   必须按第 6.1.2 节同时使旧 pair 和新 pair 的 discovery cache 失效。
11. `playback.context.ensure` 必须在 `(authenticated user, stable clientId)` 作用域与 close、
    handoff authority switch 串行化。当前或可重绑的 canonical Context 为 idle 且 ensure 携带非空
    本地快照时，可以在同一原子操作中初始化该 Context；canonical 已为 queue-backed 时 ensure 只返回
    它，不执行无 base cursor 覆盖。没有可恢复 Context 时按请求快照创建 queue-backed 或 idle Context。
    任何分支都不得产生第二个 active Context。存在多个候选、旧 deviceSession 仍在线或同 clientId
    被不同逻辑设备复用时返回 `conflict`。
12. `playback.context.prepare.intentId` 是准备事务的长期幂等键。同一 Context、同一 intentId、相同
    可选初始队列的重试返回当前 prepare 结算，不重复路由；同一 intentId 内容不同返回 `conflict`。
    每个 Context 同时最多一个非终态 prepare。authority deviceSession、Context epoch 或 authority
    client 改变时旧 prepare 立即失败，不得在新设备上继续执行延迟播放意图。
13. `playback.update(origin:"localUser").intentId` 是 authority 本地人工控制的长期幂等键。同一
    Context/epoch、同一 intentId、相同绝对结果的重试必须重放首次 canonical playback.update，不得
    重复递增 cursor或再次 supersede；同一 intentId 内容不同返回 `conflict`。Context close、epoch
    变化、authority client 或 deviceSession 变化后，旧 intentId 不得应用到新 binding。
14. 服务端必须持久化每个 `(playbackContextId, epoch, controlVersion)` 的远程控制事务状态
    `pending|committed|failed|superseded`。同一事务 terminal 后不能变成另一 terminal；相同
    remoteCommand 结果重试只重放 canonical confirmation，不得重复推进 applied cursor 或 Context
    对账。
15. event-confirmed `playback.update` 若因 `appliedControlVersion < lastAppliedControlVersion` 被判定为
    迟到，服务端不得静默丢弃并让请求方等待。相同 requestId 重试必须重放首次生成的 source-only
    当前 passive canonical update；该纠正消息不进入 Context 全局广播。
16. `playback.control.settled` 的幂等主键是
    `(playbackContextId, epoch, commandControlVersion)`。同一 terminal 内容可重复投递并由 Flutter
    忽略；同一键不同 status/errorCode 是 conformance 缺陷，不得改写已持久化 terminal。dependency
    cascade 在全部事务状态原子提交后，按 commandControlVersion 升序逐条发送。

### 4.5 Cursor 的含义与递增矩阵

这些字段互不替代：

- 顶层 `connectionEpoch`：物理 Socket provenance，只用于隔离旧连接消息。
- Context `epoch`：播放时间线 generation；authority 原子切换时递增。
- `version`：任何被接受并物化到权威 Context snapshot 的 mutation 版本。
- `queueRevision`：canonical 队列内容或 `currentIndex` 变化版本。
- `controlVersion`：被接受且会执行播放控制或改变 authority 的操作版本。
- `observedControlVersion`：authority 本地人工操作发生时已观察到的控制版本，不是严格 base cursor，
  也不是客户端申请的新版本。
- `commandControlVersion`：remoteCommand feedback 正在结算的服务端控制事务版本。
- `appliedControlVersion`：DevicePlaybackState 所描述的实际状态已经成功执行到的控制版本。
- `lastAppliedControlVersion`：服务端为当前 authority device 持久化的最高已确认 applied 版本；普通
  进度允许等值更新，低值反馈不得覆盖，高于 canonical controlVersion 的反馈是协议错误。
- `clientSeq`：必需的设备 feedback 序号，作用域为
  `(playbackContextId, clientId, connectionNonce, connectionEpoch)`；该作用域内从 1 单调递增，新物理连接可从 1
  重新开始。`device.volume.update` 使用独立的
  `(user, clientId, deviceSessionId, connectionNonce, connectionEpoch)` 作用域。

| 被接受的动作 | `epoch` | `version` | `queueRevision` | `controlVersion` |
| --- | --- | --- | --- | --- |
| 首次 ensure 按请求快照创建 idle 或 queue-backed Context | 初始化为 1 | 初始化为 1 | 初始化为 1 | 初始化为 1 |
| ensure 返回当前 pair 的既有 Context | 不变 | 不变 | 不变 | 不变 |
| ensure 用非空本地快照初始化当前 idle Context | 不变 | +1 | +1 | +1 |
| ensure 将同 clientId 的离线旧 deviceSession 重新绑定到当前 session | +1 | +1 | 仅同时将 idle 初始化为非空时 +1 | +1 |
| `device.setVolume` / `device.volume.update` | 不变 | 不变 | 不变 | 不变；不访问 Context |
| `playback.context.prepare` / `playback.context.prepared` | 不变 | 不变 | 不变 | 不变；prepare 使用独立 intentId 状态机 |
| `queue.context.sync` | 不变 | +1 | +1 | 当 currentIndex、该 index 的 trackId、position 或 idle/non-empty 边界改变时 +1；这是 authority 已提交的实际 state mutation，control 前进时该 authority 的 applied cursor 同步前进；idle→non-empty 将 state 设为 paused，non-empty→idle 将 state 设为 idle |
| `queue.playItem` | 不变 | +1 | +1 | +1 |
| `player.play` / `pause` / `seek` | 不变 | +1 | 不变 | +1 |
| `player.next` / `prev` | 不变 | +1 | +1（`currentIndex` 改变） | +1 |
| `playback.update(origin:"passive")` | 不变 | 不变 | 不变 | 不变；只更新对应 device state / `clientSeq` |
| `playback.update(origin:"remoteCommand", executionStatus:"committed")` | 不变 | 不变 | 不变 | 不变；将 pending command 结算为 committed，并推进该 device 的 applied cursor |
| `playback.update(origin:"remoteCommand", executionStatus:"failed")` | 不变 | 仅需要把预期 Context 恢复为实际状态时 +1 | 仅需要恢复 currentIndex 时 +1 | 不变；command 版本已占用但 applied cursor 不推进 |
| `playback.update(origin:"localUser", executionStatus:"committed")` | 不变 | +1 | queueIndex 改变时 +1 | +1；服务端从当前 canonical 值递增，并 supersede 旧 pending remote |
| server-only `playback.control.settled` | 不变 | 不变 | 不变 | 不变；只把指定 pending control transaction 写入 failed terminal，不修改实际设备状态 |
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

`observedControlVersion` 不使用普通 base cursor 的精确相等规则。当前 authority 的 localUser update
在 `observedControlVersion <= canonical controlVersion` 时可以按服务端接收顺序被接受；大于
canonical 返回 `bad_request`。服务端接受后必须从当前 canonical controlVersion 加一，不能使用
客户端猜测值。`appliedControlVersion < lastAppliedControlVersion` 的反馈不得覆盖 DevicePlaybackState；
等于 lastApplied 时允许 passive 事实、匹配 pending command 的 failed 结果或相同 terminal 幂等重放；
高于 lastApplied 时必须由按序有效的 pending remote committed 或新接受的 localUser transaction 证明。

低于 lastApplied 的迟到 passive 或与当前 terminal 一致的旧反馈必须使用当前已保存的
DevicePlaybackState 构造 `origin:"passive"` 的 canonical `playback.update`，只发回请求 Socket，不写入、
不推进 `clientSeq`、不向其他 recipient 广播。已 terminal 的同一 commandControlVersion 收到不同
terminal 结果仍返回 `conflict`，不得用 source-only 纠正掩盖协议冲突。

### 4.6 严格 schema 闭合规则

- 每个 action 只允许本文对应表格、示例和共享 envelope 明示的字段；未知 request 字段返回
  `bad_request`。服务端 push 也不得添加未定义字段。
- 可选字段无值时省略，不得传 JSON `null`。数组不得含重复 ID，string 必须去除首尾空白后非空。
- 所有 ID（`requestId`、client/device/context/handoff/prepare/intent/broadcast/timeline ID）最大 128 UTF-8
  bytes；`action` 最大 64 bytes；错误 `message` 最大 512 bytes。
- 单个 transport message 上限为 256 KiB。Engine.IO/WebSocket 层在进入业务 handler 前发现超限时，
  使用 message-too-big 行为关闭连接，不保证返回 system.error。消息进入业务 handler 后发现字段、
  队列（最多 1000 首）、participants（最多 100 个）或其他业务限制超限时，返回同 requestId 的
  `system.error(code:"bad_request")`。两种情况都不得静默截断。
- `timestamp` 只允许第 2.2 节的兼容用途；action schema 未列出的时间字段一律禁止。
- 所有集合语义数组必须去重。`queueSongIds` 必须保留 canonical 播放顺序，严禁排序；roles 固定按
  `player`、`controller` 顺序输出；participants、skippedClientIds、devices 及服务端生成的其他
  client 集合按 `clientId` 升序；`playback.context.list.payload.contexts` 按
  `playbackContextId` 升序。

## 5. 客户端到服务端：全部 strict-v2 请求

除第 3 节的认证、注册、设备列表和 heartbeat 外，以下是 Flutter 必须实现的 normative strict
action allowlist。所有请求均不得含 `sessionId` 或顶层 `targetClientId`。payload 内
target 字段也禁止，例外只有第 5.2 节 `device.setVolume` 与第 5.4 节 `playback.handoff.start`。

字段标记：`R` 必需；`O` 可选；`int>=0` 是 JSON number 且不小于零。

### 5.1 PlaybackContext 生命周期

| Action / type | payload | 服务端动作与响应 |
| --- | --- | --- |
| `playback.context.list` / `state` | `authorityClientId:R string`、`authorityDeviceSessionId:R string` | 仅 controller 可查询。服务端只在当前 authenticated user 的 active Context 中按两个字段精确匹配，并用同 requestId direct 返回第 6.1 节的 0/1/多个 binding；不得 ACK、不得自动订阅、不得返回 queue/playback snapshot。 |
| `playback.context.ensure` / `command` | `deviceSessionId:R string`、`queueSongIds:R distinct string[]`、`currentIndex:C int>=0`、`positionMs:R int>=0`、`state:R idle\|playing\|paused\|stopped` | 仅当前注册 player 可对自己调用；要求 `canPlay:true`，deviceSessionId 必须匹配当前连接。空队列要求 state=idle、positionMs=0 并省略 currentIndex；非空队列要求合法 currentIndex 且 state 非 idle。服务端按第 6.2 节原子返回、重绑、以本地快照初始化或创建该 stable clientId 的唯一 active Context，自动订阅当前 Socket，并使用同 requestId direct 返回完整 snapshot；不得 ACK，不接收 playbackContextId、trackId 或 target 字段。 |
| `playback.context.subscribe` / `state` | `playbackContextId:R` | 将当前 Socket 加入该 context recipient set并返回 ACK；客户端随后显式请求 status。 |
| `playback.context.unsubscribe` / `state` | `playbackContextId:R` | 移除 context recipient；返回 ACK。 |
| `playback.context.status` / `state` | `playbackContextId:R` | 返回第 6.3 的完整 status（同 requestId 直接 action response）。 |
| `playback.context.prepare` / `command` | `playbackContextId:R`、`intentId:R string`、`baseControlVersion:R int>=0`、`initialQueueSongIds:O non-empty distinct string[]`、`currentIndex:C int>=0`、`positionMs:C int>=0` | 仅 controller 可调用。只允许对 idle Context 准备队列；可选初始队列存在时 currentIndex 与 positionMs 必需且 index 必须合法，不存在时两者必须省略。服务端按第 6.2.1—6.2.3 节 ACK、路由并结算。 |
| `playback.context.close` / `command` | `playbackContextId:R` | 当前 authority 或同用户 controller 可关闭。写入不可复用 tombstone，返回 ACK，并向所有订阅者推送 `playback.context.closed`。authority 若仍在线且没有进入应用退出流程，收到 closed 后必须立即重新 ensure 一个 idle Context。 |

Context 授权采用“用户域读取、角色控制”：同一 authenticated user 的已注册设备可 subscribe/status；
controller 可发起控制；当前 authority 必须是在线 player。对携带 `playbackContextId` 的跨用户
访问统一返回 `forbidden`，不得用 `not_found` 泄露资源是否存在；list 不接收 Context ID，按下述
规则先限定用户域并对不可见 binding 返回成功空数组。Context authority 持久绑定 `authorityClientId` 与
`authorityDeviceSessionId`；只有两者均匹配的重连才恢复 authority 路由。

`playback.context.list` 是 remote-control scope discovery，不是模糊搜索：

1. `authorityClientId` 取自所选 `device.list` 项的 `clientId`；
2. `authorityDeviceSessionId` 取自同一项的 `deviceSessionId`；
3. 两个字段都必须精确匹配持久化 Context binding。服务端不得只匹配 clientId，不得使用请求者
   自己的 deviceSessionId，也不得回退到任何 `sessionId`；
4. 查询始终先按当前 authenticated user 限定数据域，再筛选 `lifecycle=active` 和 authority/device
   pair。其他用户的设备或 Context 对该查询不可见，并产生成功空数组；
5. authority 暂时离线不改变 active binding，list 可以返回该 Context；客户端随后发控制命令时
   再由服务端检查当前 Socket，离线则返回 `authority_offline`；
6. 完成注册和 ensure 的在线 player 应精确返回 1 个 Context；返回 0 个只允许发生在 ensure 尚未完成、
   Context 已关闭但新 ensure 尚未完成或服务端异常的短暂窗口，controller 必须等待
   `playback.context.bindings.changed` 并有界重查，不能把“未播放”解释成空结果；返回 1 个时可自动
   subscribe/status；
   返回多个时服务端不得按更新时间、播放状态、队列内容或数组第一项替客户端选择。Flutter 必须
   清除所选设备现有的 Context 水合与控制 cursor，必要时 unsubscribe 旧 Context，并进入本地
   `ambiguous_playback_scope` 状态；不得自动选择，也不得允许用户从 opaque Context ID 中任选一个
   继续控制。该状态不是 wire error code；只有未来契约提供服务端权威的真实音频 active 标记后，
   才能在多结果中选取一个 Context；
7. list 不建立 subscription，也不构成水合。客户端选定唯一 Context 后必须依次执行
   `playback.context.subscribe` 和 `playback.context.status`，并只使用 status 返回的 canonical
   `controlVersion` / `queueRevision` 发送控制请求；
8. 客户端在收到匹配 pair 的 `playback.context.bindings.changed`、`playback.context.closed`、
   Handoff completed 后 authority 变化，或新的 `device.list` 显示目标 deviceSessionId 变化/重连
   时，必须立即暂停控制，使旧 device→Context binding 失效，并用新 requestId 重新 list。若 status
   中的 `authorityClientId` 已不等于所选设备，也必须停止以该设备为目标的控制；不得继续沿用旧选择。

### 5.2 队列、播放状态和控制

设备级音量不属于 PlaybackContext mutation，可在目标在线但没有 Context 时使用：

| Action / type | payload | 服务端动作与响应 |
| --- | --- | --- |
| `device.setVolume` / `command` | `targetClientId:R`、`targetDeviceSessionId:R`、`volume:R int 0..100` | 请求者必须是同用户 controller 且协商 `remoteVolumeControl:true`。服务端按 client/device pair 精确解析当前 Socket；目标必须是 player，并同时协商 `remoteVolumeControl:true`、`canSetVolume:true`。成功向目标单播第 6.0 节 command 并 correlated ACK；目标不存在、离线或 device session 已替换时返回 `not_found`。不得创建、读取或修改 PlaybackContext。 |
| `device.volume.update` / `event` | `deviceSessionId:R`、`volume:R int 0..100`、`clientSeq:R int>=1` | player 上报当前物理连接的实际音量。服务端验证 source client/device/nonce，更新在线瞬态状态，并按第 6.0 节发送 canonical event confirmation；不回 ACK、不持久化、不修改 Context。 |

`device.setVolume` 的 ACK 只证明服务端已验证并把 command 加入目标当前 Socket 的发送路径，不证明
硬件已经达到请求值。controller 必须以随后 `device.volume.update` 的实际值为准。设备本地音量变化
也应主动发送 `device.volume.update`；player 若同时有活动 Context，还应在正常 `playback.update`
中包含相同实际 volume，以维持该 Context 的 DevicePlaybackState。

`baseControlVersion` / `baseQueueRevision` 是客户端请求的乐观并发前置条件。服务端在 `2.4.x`
中必须继续接受并按下表校验这些字段；若 cursor 已过期，返回第 4.2 节的 correlated
`system.error`。服务端接受请求后向客户端推送 canonical `controlVersion` / `queueRevision`，
不得把请求字段 `baseControlVersion` / `baseQueueRevision` 原样转发给 authority。

| Action / type | payload | 服务端动作与响应 |
| --- | --- | --- |
| `queue.context.sync` / `state` | `playbackContextId:R`、`deviceSessionId:R`、`queueSongIds:R distinct string[]`、`currentIndex:C int>=0`、`positionMs:R int>=0`、`baseQueueRevision:R int>=0`、`baseControlVersion:C int>=0` | 仅当前 authority 可发送，deviceSessionId 必须匹配 authority 连接。空队列时 currentIndex 必须省略且 positionMs 必须为 0；非空队列时 currentIndex 必需且合法。队列内容变化校验 baseQueueRevision；index、当前 track、position 或 idle/non-empty 边界变化时 baseControlVersion 必需。按第 4.5 节递增 cursor，ACK 并推送第 6.5 节 canonical queue state。 |
| `playback.context.prepared` / `event` | `playbackContextId:R`、`deviceSessionId:R`、`intentId:R`、`ready:R bool`、`errorCode:C queue_required\|restore_failed\|prepare_timeout\|authority_changed`、`errorMessage:O string` | 当前 authority 对 prepare 给出 event-confirmed 结果。`ready:true` 时 Context 必须已通过 queue sync 变成非空，error 字段禁止；`ready:false` 时 errorCode 必需。服务端不回 ACK，按第 6.2.3 节广播结果。 |
| `playback.update` / `event` | 第 5.2.1 节的公共字段，以及由 `origin` / `executionStatus` 决定的闭合条件字段 | 仅当前 authority player 可发送。passive 只更新事实；remoteCommand 结算服务端已接受的控制事务；localUser 表示已经完成的 Windows 本地人工操作并由服务端分配新版本。三种 shape 都使用第 6.6 节无 requestId canonical confirmation 结算，不回 ACK。 |
| `queue.playItem` / `command` | `playbackContextId:R`、`queueIndex:R int>=0`、`baseQueueRevision:R int>=0`、`baseControlVersion:R int>=0` | 验证 cursors 后选择队列项，向 authority 发送第 6.7 的 server-routed control，返回 ACK；所有 recipients 接收 queue/context state 更新。不同 Socket 间不承诺到达顺序。 |
| `player.play` / `command` | `playbackContextId:R`、`baseControlVersion:R int>=0`、`positionMs:O int>=0` | 验证 authority/cursor 后递增控制版本，并向 authority 发送无 target 的第 6.7 control。 |
| `player.pause` / `command` | 同 `player.play` | 同上。 |
| `player.seek` / `command` | `playbackContextId:R`、`baseControlVersion:R int>=0`、`positionMs:R int>=0` | 同上，`positionMs` 必须有。 |
| `player.next` / `command` | `playbackContextId:R`、`baseControlVersion:R int>=0` | 同上。 |
| `player.prev` / `command` | 同 `player.next` | 同上。 |

所有 player control 的请求者必须具有 controller 角色。服务端按 action 校验当前 authority 的
negotiated `canPlay` / `canPause` / `canSeek`；请求 controller 不需要具备对应播放能力。能力不足
返回 `capability_required`，authority 离线返回 `authority_offline`，两者都不得改变 cursor。
`queue.playItem`、`player.next`、`player.prev` 要求 authority `canPlay:true`。

Context 为 idle 时，`queue.playItem` 和全部 `player.*` 请求返回 `queue_required`，不得递增 cursor、
不得向 authority 路由普通控制。controller 必须先完成 `playback.context.prepare`，观察同一个 Context
变为非空，然后使用最新 canonical controlVersion 发送原始控制意图。

为使多 Context fail-closed 不依赖 invalidation event 的到达速度，服务端接受任一
`queue.playItem` / `player.*` 控制前，必须在与该控制 mutation 相同的原子临界区内验证：当前
Context 是其 `authorityClientId` / `authorityDeviceSessionId` pair 唯一的 active Context。若同一
pair 存在其他 active Context，返回 `conflict`，并在 error payload 中带请求的
`playbackContextId`、`currentControlVersion`、`currentQueueRevision`、`currentVersion`；不得向
authority 发送命令，也不得修改任何 cursor。ensure、close、handoff authority switch 与普通
player control 必须按 authority/device pair 串行化，不能在“检查唯一性”和“提交控制”之间插入
binding mutation。

### 5.2.1 `playback.update` 请求的闭合 shape

所有 playback.update 请求公共必需字段：

```text
playbackContextId:R string
deviceSessionId:R string
origin:R passive|remoteCommand|localUser
state:R idle|playing|paused|stopped
positionMs:R int>=0
clientSeq:R int>=1
trackId:C string
volume:O int 0..100
muted:O bool
```

只有当前 Context authority 的当前 client/device/Socket 可以发送。服务端必须验证 authenticated user、
authorityClientId、authorityDeviceSessionId、connectionNonce/connectionEpoch 和 clientSeq 作用域；
controller-only 或旧 authority 返回 `forbidden`，旧 device session 返回 `conflict`。

公共状态规则：

- idle Context 只允许 `state:"idle"`、`positionMs:0` 并省略 trackId；
- queue-backed Context 不允许 state idle，trackId 必需；
- passive/remoteCommand 的 trackId 按 `appliedControlVersion` 对应的已提交事务或已保存 applied
  snapshot 校验；localUser 的 trackId 按绝对 queueIndex 校验。当 `controlVersion >
  appliedControlVersion` 时，实际 track 可以暂时不同于主 Context 最新控制目标；不得继续无条件与
  最新 canonical current item 比较；
- 请求禁止 `sourceClientId`、`controlVersion`、`supersededThroughControlVersion`、queueSongIds、
  currentIndex、queueRevision、version、target 字段和 JSON null。

四种有效组合如下：

| origin / status | 额外必需字段 | 条件/禁止字段 | 服务端语义 |
| --- | --- | --- | --- |
| `passive` | `appliedControlVersion:int>=1` | 禁止 executionStatus、commandControlVersion、intentId、epoch、observedControlVersion、queueIndex、error 字段 | 更新进度、状态、音量等事实；applied 必须等于 lastApplied，不推进任何 Context cursor |
| `remoteCommand` / `committed` | `executionStatus:"committed"`、`commandControlVersion:int>=1`、`appliedControlVersion:int>=1` | applied 必须等于 command；禁止 intentId、epoch、observedControlVersion、queueIndex、error 字段 | 只允许按序结算匹配的 pending command；推进 lastApplied，不再次推进 controlVersion |
| `remoteCommand` / `failed` | `executionStatus:"failed"`、`commandControlVersion:int>=1`、`appliedControlVersion:int>=1`、`errorCode` | applied 必须小于 command；`errorMessage:O string`；禁止 intentId、epoch、observedControlVersion、queueIndex | 将 pending command 结算为 failed；必要时使用新 Context version/Queue revision恢复实际 snapshot，但 controlVersion 不变 |
| `localUser` / `committed` | `executionStatus:"committed"`、`intentId:string`、`epoch:int>=1`、`observedControlVersion:int>=1`、`queueIndex:int>=0`、`trackId:string` | state 只能 playing/paused/stopped；queueIndex 必须在 canonical queue 内且 trackId 匹配；禁止 appliedControlVersion、commandControlVersion、error 字段 | 接受本地人工最终结果，从当前 canonical controlVersion +1，并 supersede 旧 pending remote |

remoteCommand failed 的稳定 `errorCode` 固定为：

```text
playback_failed
track_load_failed
seek_failed
execution_timeout
```

`dependency_failed` 和 `execution_unknown` 不是 Windows 实际执行反馈，不允许出现在客户端发送的
remoteCommand failed `playback.update`；它们只允许出现在第 6.6.5 节服务端生成的
`playback.control.settled`。

`errorMessage` 只用于诊断，客户端逻辑必须依据 errorCode。commandControlVersion 不存在、不是该
authority 收到的命令、已经 terminal，或与 action 预期结果冲突时返回 `conflict`，不得改写事务。

localUser 仅表示已经由 Windows 音频层 committed 的人工 play/pause/seek/选歌结果。Windows 本地
next/previous 必须在上报前转换成绝对 queueIndex/trackId；自然播完后的自动下一首不得伪装成
localUser，也不得获得 supersede 权限。完整 queue 内容改变必须先走显式 queue.context.sync。

服务端接受 localUser update 时必须与普通 control、ensure、close、handoff 和 remote result 结算在
同一 Context 串行区内执行：

1. 验证当前 authority binding、epoch、intentId 和绝对 queueIndex/trackId；
2. 允许 observedControlVersion 小于或等于当前 canonical；大于 canonical 返回 bad_request；
3. 令 `supersededThroughControlVersion` 等于接受前的 canonical controlVersion；
4. 从当前 canonical 值加一，写入新的 controlVersion；
5. 更新实际 state/position/currentIndex；currentIndex 改变时递增 queueRevision；
6. 将所有版本不高于 supersededThrough 且仍为 pending 的远程事务标记为 superseded；已经 committed
   或 failed 的历史不回滚；
7. 将该 authority 的 appliedControlVersion 同步推进到新的 controlVersion；
8. 持久化后按第 6.6 节向源 Socket 和全部合法 recipients 推送 canonical localUser confirmation。

本地操作失败不得发送 committed localUser shape，不得推进版本或 supersede。Windows 必须解除本地
执行屏障并通过 passive 实际状态或本地 UI 错误完成恢复；本 r11 不定义 localUser failed wire shape。

### 5.2.2 远程控制事务与实际执行

服务端接受 queue.playItem 或 player.* 后，必须创建
`(playbackContextId, epoch, controlVersion)` 唯一事务并设置 `status:"pending"`。ACK 只表示请求已
验证、版本已分配且命令已可靠加入当前 authority Socket 的发送路径，不表示音频已经执行。

事务状态机固定为：

```text
pending -> committed
pending -> failed
pending -> superseded
```

terminal 状态不可互换。Windows 收到 server-routed command 后必须按 controlVersion 串行执行，只有
AudioPlayerService/PlaybackActor 返回实际 song/index/state/position 的 committed snapshot 时才发送
remoteCommand committed。加载中的旧 track、临时 pause、buffering 和播放器回调不能作为结算。

服务端接受 remoteCommand committed/failed 前还必须验证：commandControlVersion 不低于当前
lastApplied，且它之前不存在仍为 pending 的更低控制事务；已经 failed/superseded 的低版本可以跨过。
如果更低版本仍 pending，返回 conflict 并记录 Windows 执行乱序，不能提前把 applied 跳到更高版本。

每个 pending transaction 必须在持久化记录中保存 `acceptedAtMs`、`executionTimeoutMs` 与
`watchdogDeadlineAtMs`。`executionTimeoutMs` 默认 15000ms，部署可以调整；服务端 watchdog 固定为
`executionTimeoutMs + 2000ms`，默认 17000ms。服务端在命令与 pending transaction 原子提交、并可靠
加入 authority 当前 Socket 发送路径后启动 watchdog，同时把 executionTimeoutMs 写入第 6.7 节
server-routed command。committed、failed、superseded、authority 断线或服务端 shutdown/recovery 都必须
取消对应 watchdog。

Windows 收到 server-routed command 并放入本地执行队列后，必须使用命令携带的 executionTimeoutMs
启动该 controlVersion 的 audio execution lease，不能在客户端另写不同固定值。期限内成功发送
remoteCommand committed；期限内确认失败发送对应 remoteCommand failed。到达期限时，只有 Windows
能够证明该 lease 已失效、迟到 Future/callback 不会切歌、发送 committed 或修改 Context/Queue/系统
媒体状态，才可发送 `errorCode:"execution_timeout"` 的 remoteCommand failed。

如果 Windows 音频层尚不能停止、取消或隔离迟到执行，该客户端不满足 r11 硬超时 readiness：不得发送
execution_timeout，也不得声称命令已经安全取消。服务端 watchdog 到期后只能按 execution_unknown
结束事务；Flutter 重新水合后由用户决定是否重新操作。

当 `queue.playItem`、`player.next` 或 `player.prev` 进入 failed 时，服务端必须在同一 Context 串行事务中：

1. 结算原 command；
2. 将同一 Context/epoch 中所有更高 controlVersion 且仍为 pending 的远程事务标记为 failed，并写入
   `errorCode:"dependency_failed"`；
3. 保持 canonical controlVersion 为已经分配的最高值，不回退或复用版本；
4. 按 lastApplied snapshot 对账主 Context，必要时只推进 Context version/Queue revision；
5. 广播原 command 的 failed canonical playback.update；
6. 按 commandControlVersion 升序，为每个被 dependency_failed 的事务分别发送第 6.6.5 节
   `playback.control.settled`，其 `dependsOnControlVersion` 指向原失败 command；
7. 发送对账后的 status/queue state。controller 必须按每个 settled 的具体版本结束 pending，全部结算
   后合并请求一次最新 status，不得仅根据当前 controlVersion 猜测失败区间。

Windows 必须按 controlVersion 串行执行。切歌类 command 失败后，它必须丢弃本地队列中所有更高版本的
未完成远程命令，不得把 pause、seek、play 或下一次切歌悄悄执行到旧歌曲上。

服务端 watchdog 到达 `executionTimeoutMs + 2000ms` 时若仍未收到合法 terminal feedback，只能把该
事务结算为 failed/execution_unknown，并发送第 6.6.5 节 `playback.control.settled`。服务端不能生成
remoteCommand failed playback.update、不能复用或伪造 Windows clientSeq，也不能声称电脑已经安全
取消执行。execution_unknown 不证明根命令失败，因此不得把后续事务自动改写为 dependency_failed；
每个仍 pending 的事务按自己的 terminal feedback、watchdog 或连接失效规则独立结算。

Windows 本地人工操作开始后必须暂缓尚未 committed 的远程事务；收到匹配 intentId 的 localUser
canonical confirmation 后，丢弃所有 `controlVersion <= supersededThroughControlVersion` 且仍未完成
的本地远程事务。服务端负责判定和持久化 supersede，Windows 的屏障负责阻止已经送达的旧命令晚于
本地结果执行。更新版本的新远程命令仍可正常执行。

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
- authority 永久离线时，2.4.x 不提供强制接管。controller 关闭旧 Context，目标 player 继续使用自己
  ensure 得到的唯一 Context；旧 ID 因 tombstone 不可复用。

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

下面所有业务消息都必须有顶层 `connectionNonce`、`connectionEpoch`，且不得有任何层级的 `sessionId`。服务端 strict 业务消息不得有 target 字段；收件人由 Socket.IO sid 决定。

### 6.0 设备级音量 command 与实际状态

服务端接受 `device.setVolume` 后，只向精确匹配 `targetClientId` / `targetDeviceSessionId` 的当前
Socket 投递以下无 requestId command：

```json
{
  "type": "command",
  "action": "device.setVolume",
  "connectionNonce": "<target socket nonce>",
  "connectionEpoch": 1,
  "payload": {
    "sourceClientId": "controller-1",
    "volume": 65
  }
}
```

payload 必须且只允许 `sourceClientId`、`volume`，不得复制 target 字段。收件 player 应立即设置
设备音量并上报实际结果；即使请求值与当前值相同，也必须发送 confirmation，使 controller 可以
区分“命令已路由”和“设备实际状态已确认”。

服务端 canonical `device.volume.update`：

```json
{
  "type": "event",
  "action": "device.volume.update",
  "connectionNonce": "<recipient nonce>",
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

服务端必须把 canonical update 发回 source Socket 作为 event confirmation，并发送给同用户所有
协商 `remoteVolumeControl:true` 的 controller。序号作用域为
`(user, clientId, deviceSessionId, connectionNonce, connectionEpoch)`；相同序号相同内容只向重复
请求 Socket 重放，相同序号不同内容或倒退返回 `client_sequence_conflict`。断线后在线 volume
状态和序号作用域一并清除，新物理连接可从 1 开始。

### 6.1 Context discovery 与 binding 失效通知

#### 6.1.1 `playback.context.list`：按 authority/device 发现 Context

客户端从 `device.list` 选择目标 player 后发送：

```json
{
  "type": "state",
  "action": "playback.context.list",
  "requestId": "context-list-1",
  "payload": {
    "authorityClientId": "flutter-windows-9dcc9687-7b3e-4772-b584-a0fc716ce86c",
    "authorityDeviceSessionId": "device:flutter-windows:9dcc9687"
  }
}
```

服务端用同 `requestId` 直接响应，不得先发送 `system.ack`：

```json
{
  "type": "state",
  "action": "playback.context.list",
  "requestId": "context-list-1",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "contexts": [
      {
        "playbackContextId": "playback:aa9202f4-d39d-49e4-b7c0-376708c0efc7",
        "authorityClientId": "flutter-windows-9dcc9687-7b3e-4772-b584-a0fc716ce86c",
        "authorityDeviceSessionId": "device:flutter-windows:9dcc9687"
      }
    ]
  }
}
```

`payload` 必须且只允许 `contexts`。`contexts` 必须是 array；每个项目必须且只允许非空 string
`playbackContextId`、`authorityClientId`、`authorityDeviceSessionId`，并且后两个值必须与请求完全
相同。结果只包含当前 authenticated user 的 active Context，按 `playbackContextId` 升序，且不得
重复 Context ID。

空结果使用相同成功 shape：

```json
{
  "type": "state",
  "action": "playback.context.list",
  "requestId": "context-list-empty-1",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {"contexts": []}
}
```

服务端不得返回 queue、currentIndex、playback state、deviceStates 或 cursor；这些 canonical 数据只
由客户端选定 Context 后的 `playback.context.status` 提供。服务端也不得添加 `active:true`，因为
所有返回项按定义已经是 lifecycle active，而“一个设备的唯一当前 Context”并非数据模型约束。
因此 `contexts.length > 1` 必须由 Flutter 视为歧义并 fail-closed；lifecycle active 不等于“正在驱动
真实音频”，客户端不得把其中任一项解释为当前播放任务。

该 action 的结算规则：

| 条件 | 结算 |
| --- | --- |
| 合法请求，匹配 0/1/多个 active Context | 上述 direct response；包括空数组；多个结果由客户端强制进入 `ambiguous_playback_scope` |
| 缺字段、空字段、类型错误、未知字段或任何 `sessionId` | `system.error(code:"bad_request")` |
| 未登录或未完成设备注册 | `system.error(code:"unauthorized")` |
| 调用方没有 `controller` 角色 | `system.error(code:"forbidden")` |
| 调用连接未协商 `playbackContextV2:true` | `system.error(code:"capability_required")` |
| 部署未实现该 action | `system.error(code:"not_supported")` |

任何失败都使用原 requestId，且 `system.error.payload.action` 必须为 `playback.context.list`。

若已经完成 negotiated strict 注册的连接收到 `not_supported` 或 `capability_required`，说明服务端
违反第 7.3 节的 Core readiness。Flutter 必须立即停止该连接上的远程控制、清除已发现 Context，
并将服务器 profile 标记为不合规；不得回退到 `sessionId`、`deviceSessionId` 或 `clientId` 猜测。

#### 6.1.2 `playback.context.bindings.changed`：失效通知

Context binding 发生变化后，服务端向同一 authenticated user 的所有已注册 strict controller
Socket 推送：

```json
{
  "type": "event",
  "action": "playback.context.bindings.changed",
  "connectionNonce": "<recipient nonce>",
  "connectionEpoch": 1,
  "payload": {
    "authorityClientId": "flutter-windows-9dcc9687-7b3e-4772-b584-a0fc716ce86c",
    "authorityDeviceSessionId": "device:flutter-windows:9dcc9687"
  }
}
```

payload 必须且只允许非空 string `authorityClientId`、`authorityDeviceSessionId`。这是无 requestId
的 invalidation event，不是 list response，不携带 Context ID、数量、queue、状态或 cursor，也不
建立 subscription。重复事件允许，客户端处理必须幂等。服务端不持久化或重放该事件；断线期间
遗漏的变化由客户端重连后的强制 `device.list` / `playback.context.list` 获取 canonical 结果。

服务端必须在以下 canonical mutation 原子提交后发送：

1. `playback.context.ensure` 新建 idle Context 或重绑旧 deviceSession：为受影响的新/旧
   authority/device pair 发送；
2. `playback.context.close` 关闭 active Context：为关闭前的 authority/device pair 发送；
3. `playback.handoff.complete` 原子切换 authority：分别为旧 pair 和新 pair 各发送一次；如果 pair
   完全相同则只发送一次；
4. 其他任何改变 active Context 的 `authorityClientId` 或 `authorityDeviceSessionId` 的操作：为所有
   受影响的旧/新 pair 发送。

事件必须发送给同用户所有 negotiated `playbackContextV2:true` 且具有 `controller` 角色的当前
Socket，而不只发送给该 Context 的 subscribers。跨用户 Socket、player-only Socket 和 legacy
Socket 不得收到。mutation 必须先持久化提交；请求本身按第 4.3 节完成 direct response/ACK 或
event confirmation 后，再发送本事件。事件发送失败不得回滚已提交 mutation。
如果服务端无法把该关键 invalidation 可靠加入某个目标 controller Socket 的发送队列，必须断开
该 Socket，迫使其按重连流程重新发现；不得保持连接并允许它继续使用可能过期的 binding。

Flutter 收到与当前所选设备 pair 匹配的事件后必须立即暂停该设备的所有新控制请求，使缓存的
device→Context binding、playback/queue snapshot 和 cursors 失效，并使用新的 requestId 重新执行
`playback.context.list`。只有重新得到唯一 Context 并完成 subscribe/status 水合后才能恢复控制；
空结果保持禁用，多结果进入 `ambiguous_playback_scope`。事件不匹配当前所选 pair 时可以忽略。

为处理 event 与在途 list response 的交错，Flutter 必须为每个 authority/device pair 维护仅本地使用
的单调 `discoveryGeneration`：发送 list 时记录当前 generation；收到匹配的 bindings.changed、
deviceSession 变化或其他第 5.1 节定义的失效信号时先递增 generation；list response 到达时，只有
其请求记录的 generation 仍等于当前值才允许采用。否则必须丢弃整个响应并用新 requestId 重查。
`discoveryGeneration` 不是 wire 字段，服务端不得回显或持久化。

### 6.2 `playback.context.ensure`：启动时确保唯一 Context

player 完成 negotiated 注册后，必须先读取当时可用的本地播放恢复快照，再立即发送 ensure。
本地已有队列时：

```json
{
  "type": "command",
  "action": "playback.context.ensure",
  "requestId": "context-ensure-1",
  "payload": {
    "deviceSessionId": "device:windows-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 1,
    "positionMs": 12400,
    "state": "paused"
  }
}
```

本地没有任何队列时也必须发送固定 idle shape：

```json
{
  "type": "command",
  "action": "playback.context.ensure",
  "requestId": "context-ensure-idle-1",
  "payload": {
    "deviceSessionId": "device:windows-1",
    "queueSongIds": [],
    "positionMs": 0,
    "state": "idle"
  }
}
```

ensure 请求不携带 trackId；服务端从 `queueSongIds[currentIndex]` 推导。空队列必须省略 currentIndex，
state 必须为 idle，positionMs 必须为 0；非空队列必须有合法 currentIndex，state 只允许
`playing|paused|stopped`。

首次没有服务端 Context 且请求携带非空本地队列时，同 `requestId` 直接创建并返回 queue-backed
Context：

```json
{
  "type": "state",
  "action": "playback.context.ensure",
  "requestId": "context-ensure-1",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:windows-1",
    "authorityClientId": "windows-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 1,
    "trackId": "song-2",
    "state": "paused",
    "positionMs": 12400,
    "queueRevision": 1,
    "controlVersion": 1,
    "version": 1,
    "epoch": 1,
    "timelineId": "timeline-1",
    "serverUpdatedAtMs": 1780000000000
  }
}
```

首次没有服务端 Context 且请求是 idle shape 时，返回同样 cursors 的 idle snapshot：

```json
{
  "playbackContextId": "playback:user:windows-1",
  "authorityClientId": "windows-1",
  "queueSongIds": [],
  "state": "idle",
  "positionMs": 0,
  "queueRevision": 1,
  "controlVersion": 1,
  "version": 1,
  "epoch": 1
}
```

服务端必须在 `(authenticated user, stable clientId)` 原子临界区内执行：

1. 当前 clientId/deviceSessionId 已绑定唯一 active Context：
   - canonical Context 为 idle 且请求携带非空队列时，以请求快照初始化同一个 Context，递增
     version、queueRevision、controlVersion 后返回；
   - canonical Context 已为 queue-backed 时返回现有完整 snapshot，不在 ensure 内用无 base cursor
     的本地快照覆盖 canonical queue；
   - 两边都为 idle 时直接返回，不递增 cursor；
2. 当前 pair 没有 Context，但相同 clientId 只有一个绑定到离线旧 deviceSession 的 active Context：
   保持 playbackContextId，原子重绑到当前 deviceSessionId；旧 canonical 为 idle 且请求非空时同时
   使用请求快照初始化，否则保留旧 canonical queue；cursor 按第 4.5 节组合规则递增后返回；
3. 没有 active Context：由服务端生成不可复用的 playbackContextId，按请求快照直接创建
   queue-backed 或 idle Context；
4. 存在多个候选、旧 deviceSession 仍在线、当前连接不是 player、`canPlay:false` 或请求
   deviceSessionId 不匹配时 fail-closed，不得创建第二个 Context。

ensure 成功后当前 Socket 自动订阅该 Context。新建或重绑必须在 direct response 结算后按第 6.1.2
节发送 bindings.changed；返回现有未变 Context 不发送失效通知。`playback.context.create` 不属于
strict-v2 `2.4.0` action surface，服务端收到时返回 `not_supported`。

如果 ensure 返回的 queue-backed canonical snapshot 与本地快照不同，ensure response 是当前服务端
基线。authority 必须先完成 hydration；确实需要以本地队列替换时，再使用 response 中最新
queueRevision/controlVersion 发送显式 `queue.context.sync`，不得在 ensure 内无版本覆盖。无论队列
是否相同，设备都应请求 `playback.context.status` 读取持久化 DevicePlaybackState，再按第 5.2.1 节
使用 playback.update 上报实际播放状态和位置。首次按本地 snapshot 创建 Context 时，该 snapshot
本身视为 authority 已执行的版本 1；服务端可以在首次 passive update 前保持 deviceStates 为空，
但不得要求客户端再生成一次 localUser 版本 2。

所有 Context snapshot 使用一套条件闭合 schema：

- 公共必需字段：`playbackContextId`、`authorityClientId`、`queueSongIds`、`state`、`positionMs`、
  `queueRevision`、`controlVersion`、`version`、`epoch`；`timelineId`、`serverUpdatedAtMs` 可选；
- idle：`queueSongIds` 必须为空，`state` 必须为 `idle`，`positionMs` 必须为 0，`currentIndex` 与
  `trackId` 必须省略；
- queue-backed：`queueSongIds` 必须非空，`state` 只允许 `playing|paused|stopped`，`currentIndex`
  和 `trackId` 必需，且 `currentIndex < queueSongIds.length`、
  `trackId == queueSongIds[currentIndex]`；
- 不得使用 JSON null、负数 index、假歌曲或 sentinel track 表示 idle。

### 6.2.1 `playback.context.prepare`：让 idle Context 准备队列

controller 在 idle Context 上收到一次用户播放意图后发送：

```json
{
  "type": "command",
  "action": "playback.context.prepare",
  "requestId": "context-prepare-1",
  "payload": {
    "playbackContextId": "playback:user:windows-1",
    "intentId": "remote-play-intent-1",
    "baseControlVersion": 1,
    "initialQueueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "positionMs": 0
  }
}
```

`initialQueueSongIds` 可省略。省略时 `currentIndex`、`positionMs` 也必须省略，authority 只恢复自己的
本地队列；提供时数组必须非空、去重且不超过 1000 首，currentIndex/positionMs 必需且合法。该队列
只是待验证输入，只有 authority 成功执行 `queue.context.sync` 后才成为 canonical queue。

服务端验证同用户 controller、唯一 Context、当前 authority 在线、Context epoch/binding 未变化、
baseControlVersion 精确匹配且 Context 仍为 idle，然后建立最多 10 秒的 prepare 事务，并 ACK：

```json
{
  "type": "system",
  "action": "system.ack",
  "requestId": "context-prepare-1",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "action": "playback.context.prepare",
    "intentId": "remote-play-intent-1",
    "status": "preparing",
    "controlVersion": 1
  }
}
```

如果 ACK 前 Context 已经变为非空，服务端不路由准备命令，ACK 使用 `status:"ready"` 和当时最新
controlVersion。prepare 本身不修改 Context snapshot 或任何 cursor；同一 Context 同时只允许一个
非终态 prepare。

### 6.2.2 server-routed prepare

服务端向当前 authority Socket 发送无 requestId、无 target 字段的 command：

```json
{
  "type": "command",
  "action": "playback.context.prepare",
  "connectionNonce": "<authority nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:windows-1",
    "intentId": "remote-play-intent-1",
    "controlVersion": 1,
    "sourceClientId": "android-controller-1",
    "initialQueueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "positionMs": 0
  }
}
```

authority 优先恢复自己的本地队列；本地没有队列时才验证并采用可选 initialQueue。成功时必须先用
`queue.context.sync` 把同一个 Context 从 idle 变为 queue-backed paused，不能创建新 Context。

### 6.2.3 `playback.context.prepared`：准备结果

authority 可以发送 event-confirmed 结果：

```json
{
  "type": "event",
  "action": "playback.context.prepared",
  "requestId": "prepared-feedback-1",
  "payload": {
    "playbackContextId": "playback:user:windows-1",
    "deviceSessionId": "device:windows-1",
    "intentId": "remote-play-intent-1",
    "ready": false,
    "errorCode": "queue_required",
    "errorMessage": "No recoverable or supplied queue"
  }
}
```

服务端向 subscribers 广播的 canonical result 必须省略 requestId 和 deviceSessionId，并包含
controlVersion：

```json
{
  "type": "event",
  "action": "playback.context.prepared",
  "connectionNonce": "<recipient nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:windows-1",
    "intentId": "remote-play-intent-1",
    "ready": false,
    "errorCode": "queue_required",
    "errorMessage": "No recoverable or supplied queue",
    "controlVersion": 1
  }
}
```

`ready:true` 时 error 字段禁止，并且服务端必须验证 Context 已为非空；`ready:false` 时 errorCode 必需，
只允许 `queue_required|restore_failed|prepare_timeout|authority_changed`。服务端不回 ACK，而是向当前
Context subscribers 广播无 requestId 的同 action canonical result，并增加当时最新
`controlVersion`。若 prepare 期间任一合法 queue sync 把 Context 变为非空，服务端可直接将该 prepare
结算为 ready 并广播成功；之后到达的同 intentId `ready:true` 是幂等重复。10 秒到期时，Context 已
非空则结算 ready，否则结算 `prepare_timeout`。authority/binding/epoch 变化时结算
`authority_changed`。prepare 已结算 ready 后再到达的同 intentId `ready:false` 不得覆盖成功结果；
服务端向该 authority Socket 重放 canonical ready 结算并记录迟到诊断。

controller 收到 ready 或观察到同一 Context 已变为非空后，必须重新读取最新 canonical
controlVersion，再发送最初的 `player.play` 一次。prepare ACK 或 prepared ready 不能直接显示为已经
播放。

### 6.3 `playback.context.status`：水合和广播的权威快照

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
        "appliedControlVersion": 1,
        "clientSeq": 7,
        "serverUpdatedAtMs": 1780000001200
      }
    ]
  }
}
```

idle status 使用相同 action，但主 snapshot 为：

```json
{
  "playbackContext": {
    "playbackContextId": "playback:user:windows-1",
    "authorityClientId": "windows-1",
    "queueSongIds": [],
    "state": "idle",
    "positionMs": 0,
    "queueRevision": 1,
    "controlVersion": 1,
    "version": 1,
    "epoch": 1,
    "serverUpdatedAtMs": 1780000001200
  },
  "deviceStates": []
}
```

主 snapshot 必须遵守第 6.2 节的 idle/queue-backed 条件 schema。idle 不是缺少 Context、loading、
stopped 或播放成功；它只表示控制范围已存在但当前没有歌曲。

`deviceStates` 必须是 object array，且每个项目必须且只允许 `playbackContextId`、`clientId`、
`deviceSessionId`、`state`、`positionMs`、`appliedControlVersion`、`clientSeq`、`serverUpdatedAtMs`，以及
可选 `trackId`、`volume`、`muted`。每个项目的 playbackContextId 必须等于主 snapshot；一个 clientId
或 deviceSessionId 只能出现一次。appliedControlVersion 必须为正整数且不得高于主 snapshot 的
controlVersion。作为 direct response 时顶层带原 requestId；作为后续 hydration push 时必须省略
requestId。device state 为 idle 时 trackId 必须省略且 positionMs 为 0。

当主 snapshot `controlVersion > deviceState.appliedControlVersion` 时，device state 的 track/state/
position 表示 authority 仍在执行旧版本，可以暂时不同于主 snapshot 的最新控制目标。服务端必须按
对应 applied transaction 或已持久化 applied snapshot 验证，不能只与主 snapshot 当前 track 比较。
当 appliedControlVersion 追平 controlVersion 且没有 failed/superseded 对账时，两者必须收敛。

### 6.4 `playback.context.closed`

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

close 表示终止旧 Context ID，不表示让在线 player 永久失去控制范围。仍在线、具备 canPlay 的旧
authority 必须在处理 closed 后立即发送新的 `playback.context.ensure`；服务端不得复用 tombstone ID。
应用退出或设备已经离线时不要求创建替代 Context。

### 6.5 `queue.context.sync`

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
    "trackId": "song-2",
    "state": "paused",
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

queue push 使用与 Context snapshot 相同的条件 schema：公共必需字段为 context、authority、queue、
state、position、queueRevision、controlVersion、version、epoch；serverUpdatedAtMs/timelineId 可选。
非空 queue 必须包含合法 currentIndex、匹配 trackId 和 `playing|paused|stopped` state；空 queue 必须
省略 currentIndex/trackId、positionMs 为 0、state 为 idle。

`queue.context.sync` 从 idle 进入非空时，服务端把 canonical state 设为 paused；从非空清为空时设为
idle。两种边界变化都递增 version、queueRevision 和 controlVersion。普通非空队列内容变化始终递增
version/queueRevision；只有当前 index、track 或 position 的 canonical 值同时变化时才递增
controlVersion。请求不得携带 state 或 trackId，二者由服务端根据队列推导。

### 6.6 `playback.update`

playback.update 是 authority 实际状态和控制事务的 event-confirmed 通道。客户端请求使用第 5.2.1 节
shape；服务端不回 ACK，而是向请求 Socket 和全部合法 Context recipients 广播无 requestId canonical
confirmation。服务端 push 公共必需字段为：

```text
playbackContextId
sourceClientId
deviceSessionId
origin
controlVersion
appliedControlVersion
state
positionMs
clientSeq
serverUpdatedAtMs
```

`controlVersion` 是服务端广播时的最新 canonical 控制版本，`appliedControlVersion` 是该实际设备状态
已经执行到的版本；二者可以不同。服务端 push 禁止 sessionId、authorityClientId、queueSongIds、
currentIndex、queueRevision、version、baseControlVersion、observedControlVersion 和 target 字段。

#### 6.6.1 Passive canonical update

```json
{
  "type": "event",
  "action": "playback.update",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "sourceClientId": "windows-1",
    "deviceSessionId": "device:windows-1",
    "origin": "passive",
    "controlVersion": 48,
    "appliedControlVersion": 47,
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

passive push 只允许公共字段和可选 trackId/volume/muted，不得出现 executionStatus、
commandControlVersion、intentId、queueIndex、supersededThroughControlVersion 或 error 字段。

#### 6.6.2 Remote command committed

```json
{
  "type": "event",
  "action": "playback.update",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "sourceClientId": "windows-1",
    "deviceSessionId": "device:windows-1",
    "origin": "remoteCommand",
    "executionStatus": "committed",
    "commandControlVersion": 47,
    "controlVersion": 48,
    "appliedControlVersion": 47,
    "state": "playing",
    "trackId": "song-2",
    "positionMs": 0,
    "clientSeq": 18,
    "serverUpdatedAtMs": 1780000001200
  }
}
```

commandControlVersion 与 appliedControlVersion 必须相等。controlVersion 可以更高，表示后续命令已经
accepted 但尚未 applied。服务端先持久化 command committed 和 lastApplied，再广播 confirmation。

#### 6.6.3 Remote command failed

```json
{
  "type": "event",
  "action": "playback.update",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "sourceClientId": "windows-1",
    "deviceSessionId": "device:windows-1",
    "origin": "remoteCommand",
    "executionStatus": "failed",
    "commandControlVersion": 47,
    "controlVersion": 47,
    "appliedControlVersion": 46,
    "errorCode": "track_load_failed",
    "errorMessage": "Unable to load requested track",
    "state": "paused",
    "trackId": "song-1",
    "positionMs": 32000,
    "clientSeq": 18,
    "serverUpdatedAtMs": 1780000001200
  }
}
```

failed 不推进 appliedControlVersion，也不回退 controlVersion。若 accepted command 已经改变主 Context
预期 state/currentIndex，服务端必须在同一原子结算中使用更新的 Context version，必要时使用更新的
queueRevision，把主 snapshot 对账回实际状态；不得再次推进 controlVersion，也不得在完全相同的
Context/Queue cursor 下静默改写内容。对账后的 queue.context.sync/status 与 failed playback.update
均在事务提交后发送。

failed push 的 `controlVersion` 是广播时的最新 canonical 值，可以高于 commandControlVersion。若失败的
command 是 `queue.playItem`、`player.next` 或 `player.prev`，服务端按第 5.2.2 节原子结束所有更高
pending remote，并为每个版本分别发送 `playback.control.settled(errorCode:"dependency_failed")`。
Windows remote failed playback.update 只结算其明确报告的 command，不携带或代表其他版本的服务端
主动结算。

#### 6.6.4 Local user committed

```json
{
  "type": "event",
  "action": "playback.update",
  "connectionNonce": "<nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "sourceClientId": "windows-1",
    "deviceSessionId": "device:windows-1",
    "origin": "localUser",
    "intentId": "local-intent-123",
    "executionStatus": "committed",
    "controlVersion": 48,
    "appliedControlVersion": 48,
    "supersededThroughControlVersion": 47,
    "queueIndex": 1,
    "trackId": "song-2",
    "state": "playing",
    "positionMs": 0,
    "clientSeq": 20,
    "serverUpdatedAtMs": 1780000001200
  }
}
```

localUser push 禁止 observedControlVersion 和 commandControlVersion。supersededThroughControlVersion
是接受前的 canonical 上界，不表示其中每条命令都被撤销：只有仍为 pending 的事务进入 superseded，
committed/failed 历史保持不变。queueIndex 必须匹配 canonical queue 中的 trackId；服务端同时发送
更新后的 queue/context canonical state，使 controller 获得新的 queueRevision/version。

`state:"idle"` 只允许用于 canonical queue 为空的 Context，并要求省略 trackId、positionMs 为 0。
canonical queue 非空时 feedback state 不得为 idle。trackId 必须由 appliedControlVersion 对应的事务
目标或已持久化 applied snapshot 证明；低于 lastApplied 的迟到 feedback 不得广播或覆盖，高于
canonical controlVersion 的 feedback 返回 bad_request。相同 applied 版本的 passive update 可以用
更高 clientSeq 更新位置；相同版本不同 terminal 结果返回 conflict。

#### 6.6.5 Server-only `playback.control.settled`

当服务端能够结束 control transaction、但没有新的 Windows 实际 feedback 时，必须使用：

```json
{
  "type": "event",
  "action": "playback.control.settled",
  "connectionNonce": "<recipient nonce>",
  "connectionEpoch": 1,
  "payload": {
    "playbackContextId": "playback:user:main",
    "epoch": 1,
    "commandControlVersion": 48,
    "status": "failed",
    "errorCode": "dependency_failed",
    "dependsOnControlVersion": 47,
    "controlVersion": 49,
    "appliedControlVersion": 46,
    "requestingClientId": "android-1",
    "serverUpdatedAtMs": 1780000001200
  }
}
```

payload 必须且只允许下列字段：

| 字段 | 规则 |
| --- | --- |
| `playbackContextId` | 必需，非空 string |
| `epoch` | 必需，正整数，必须等于事务所属 Context generation |
| `commandControlVersion` | 必需，正整数，被结算的具体远程命令版本 |
| `status` | 必需，当前只能是 `failed` |
| `errorCode` | 必需，只允许 `dependency_failed|execution_unknown` |
| `dependsOnControlVersion` | 仅 dependency_failed 必需，正整数且小于 commandControlVersion；execution_unknown 时禁止 |
| `controlVersion` | 必需，服务端发送时最新 canonical control cursor，必须不小于 commandControlVersion |
| `appliedControlVersion` | 必需，authority 最后已确认的实际 applied cursor，不得高于 controlVersion |
| `requestingClientId` | 必需，最初发送该远程命令的 controller；禁止使用容易混淆的 `sourceClientId` |
| `errorMessage` | 可选，安全诊断 string |
| `serverUpdatedAtMs` | 必需，Unix epoch milliseconds |

该事件是 server-only push：客户端不得发送同名 action；不带 requestId、clientSeq、deviceSessionId、
sourceClientId、实际 track/state/position 或 target 字段，不修改 DevicePlaybackState，也不声称 Windows
执行成功或失败。服务端必须使用 recipient 当前 nonce/epoch 发给全部合法 Context recipients。若最初
接收该 transaction 的 authority Windows Socket 在 settled 产生时仍在线，它必须是 recipient；已经断开
或被替换的旧 Socket 不补发给新物理连接，新连接按 nonce 变化清理旧事务并重新水合。

服务端发送任一 settled 前必须执行固定顺序：验证事务仍为 pending，在同一原子操作中写入
`status:"failed"` 与 errorCode，持久化提交成功后才允许 emit。不得先推送后写数据库。多个事务在同一
操作中结束时，先持久化全部 terminal，再按 commandControlVersion 从小到大逐条发送；一条命令只能
对应一条 settled envelope。

dependency cascade 必须先在一个原子事务中写入全部 terminal 状态，再按 commandControlVersion 从小到
大为每个受影响版本逐条发送 settled。Flutter 去重主键固定为
`(playbackContextId, epoch, commandControlVersion)`：首次收到写 terminal；相同内容重复忽略；同一
主键出现不同 status/errorCode 是协议冲突，记录错误并请求最新 status，不得覆盖第一次 terminal。
旧版本 settled 可以补齐旧 pending，但不得回滚更高 appliedControlVersion 的实际播放状态。

Windows 收到 settled 后必须按 `(playbackContextId, epoch, commandControlVersion)` 找到本地远程事务，
立即使对应 audio execution lease 失效、从执行队列删除该事务，并阻止其迟到 Future/callback 切歌、
发送 committed 或修改 Context、Queue projection 与系统媒体状态。settled 只取消事务，不为 Windows
生成新的实际播放状态；Windows 后续实际状态仍通过自己的 passive playback.update 上报。

authority 断线、Socket 被替换或服务端仍在线时发生 execution_unknown，服务端为每个受影响 pending
事务分别发送 settled。服务端完整重启时所有物理 Socket 都会变化，不要求补发历史 settled；Flutter
必须因 nonce 变化无条件清除旧 pending，再重新 list/subscribe/status。服务端仍持久化 terminal 状态，
用于审计并拒绝迟到 feedback。任何情况下都不得在 authority reconnect/ensure 后自动重发旧 command。

### 6.7 server-routed 控制

服务端应先接受第 5.2 的 command，再向当前 authority Socket 发送**无 target** command。所有 context subscribers 另收 `status` / `queue.context.sync` / `playback.update` 事实状态。

本节描述的是服务端 → 客户端的 accepted control。这里禁止 `baseControlVersion`，只允许
canonical `controlVersion`；它不改变第 5.2 节客户端 → 服务端请求必须携带
`baseControlVersion` 的要求。

#### 6.7.1 多客户端下如何确定唯一执行者

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
6. 接受命令并生成新的 canonical `controlVersion`，同时创建持久化
   `(playbackContextId, epoch, controlVersion, status:"pending")` 控制事务，保存默认 15000ms 或部署调整
   后的 executionTimeoutMs，并启动 `executionTimeoutMs + 2000ms` watchdog；事务的
   `requestingClientId` 写入发起控制的 controller。发送给 Windows 的既有 command 字段仍名为
   `sourceClientId`，表示命令来源而不是收件人。
7. 使用 authority `sid` 对应的 nonce/epoch 构造 envelope，并通过 Socket.IO
   `to=<authority sid>` 单播；不得向 Context room 或 namespace 广播这条执行命令。
8. 向原请求者返回 action-correlated ACK。ACK 只表示 accepted/routed，不表示 Windows 已执行；只有
   第 6.6 节 remoteCommand committed 才把事务结算为实际成功。另行向 Context subscribers广播
   `playback.context.status` 等 canonical 控制目标；状态广播不负责触发音频执行。

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
authority。A 发出 `player.seek` 后，服务端只把第 6.7 节的 command 单播给 C；B 不接收，
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
    "executionTimeoutMs": 15000,
    "positionMs": 42000
  }
}
```

`player.play` / `pause` 可有 `positionMs`；`seek` 必须有；`next` / `prev` 不得有。所有普通 control 必有
`playbackContextId`、正 `controlVersion`、`sourceClientId`、正整数 `executionTimeoutMs`，并禁止
`baseControlVersion` 与 `targetClientId`。executionTimeoutMs 默认 15000，Windows 必须使用该字段建立
执行租约，不得使用另一套硬编码期限。第 6.9 节 Handoff effective-at commit 不属于本控制事务期限，
继续使用 Handoff 独立的 5 秒 complete timeout。

Windows 必须把收到的 `(playbackContextId, controlVersion)` 作为远程执行事务键。执行成功发送
remoteCommand committed；执行失败发送 remoteCommand failed；在 localUser confirmation 中被覆盖的
未完成事务不得继续执行。服务端不得把 ACK、accepted status 或 Socket emit 成功当作 committed。
切歌类事务失败后，Windows 还必须停止并丢弃所有更高版本的未完成 command；断线重连后也不得从本地
队列恢复或重放旧 transaction。

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
    "sourceClientId": "controller-1",
    "executionTimeoutMs": 15000
  }
}
```

所有这些字段必需，revision/version/executionTimeoutMs 均须 `>= 1`；禁止 legacy fields、`positionMs`、
`trackId`、`currentIndex`、`targetClientId`。

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

### 6.10 Broadcast 推送与 status 结果

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
当服务端声称实现本 r11 契约时，`protocolVersion` 必须是 major `2`、minor `>=4`。服务端不得按客户端
差异返回非空-only 与 idle 两套 Context schema，也不得把 `2.3.x` shape 当作本文完成。当 wire shape
再次变化时必须同步更新 protocolVersion 和本文。

**REQ-004 — 收件人 provenance**
在 strict 注册成功后，服务端每次向该 Socket emit 任一 envelope 时，服务端必须注入与该物理 Socket 对应的 `connectionNonce` 和 `connectionEpoch`。

**REQ-005 — Context routing**
当服务端处理 context command 时，服务端必须以 `playbackContextId`、membership、authority 和 cursor 决定授权与收件人；服务端不得使用 `sessionId` 作为 strict 播放主键。

**REQ-006 — 无 direct-target strict 业务 push**
当服务端向 strict client 推送业务动作时，服务端必须按 recipient Socket 逐个发送无 `targetClientId` 的规范 envelope，而不是把 action payload 变成 direct-target command。

**REQ-007 — 失败闭合**
当请求 payload、capability、context membership 或 cursor 无效时，服务端必须返回 correlated `system.error`，且不得退回 legacy/session action。
缺失合法 requestId/action 的请求按第 2.2 节直接断开，是唯一无法 correlated error 的例外。
strict register ACK 前的 correlated error 按第 4.2 节省略 provenance；register ACK 后不得省略。

**REQ-008 — 可选模式**
当连接未在 `negotiatedCapabilities` 获得 Follow、Broadcast 或 Handoff 所需能力时，服务端不得
向该连接执行相应可选模式动作，并对请求返回 `capability_required`。

**REQ-009 — Handoff target 的方向性例外**
当客户端发送 `playback.handoff.start` 时，服务端必须接受并验证 payload 内的
`targetClientId`；当服务端向目标 Socket 或其他 context 成员推送 Handoff 消息时，服务端
不得在 envelope 或 payload 中复制该字段。

**REQ-010 — 2.4.x cursor 契约**
当客户端发送第 5.2 节定义的控制或队列请求时，服务端必须继续接受其
`baseControlVersion` / `baseQueueRevision` 前置条件；服务端不得因为 accepted push 只使用
`controlVersion` / `queueRevision`，就在 `2.4.x` 内删除请求字段。

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
码格式和第 6.10 节的初始/已反馈 paired-field 规则。

**REQ-022 — 超限分层与顺序保持**
当 transport message 超限时服务端必须关闭连接；当已解析业务字段超限时返回 correlated
bad_request。任何序列化、持久化或重启恢复都不得排序 queueSongIds，集合字段必须按第 4.6 节
确定性输出。

**REQ-023 — Context discovery 闭环**
当已注册 controller 按 authority client/device pair 发送 `playback.context.list` 时，服务端必须只在
当前 authenticated user 的 active Context 中精确匹配并返回全部 binding。服务端不得扩展
`device.list` 携带 Context，不得只按 clientId 解析，不得使用 session fallback，不得在多结果时
自行选择，也不得把 list 当作 subscription 或 status 水合。

**REQ-024 — Binding invalidation**
当 ensure、close、handoff complete 或其他 mutation 改变某个 authority/device pair 的 active
Context binding 集合时，服务端必须在提交和请求结算后，按第 6.1.2 节向同用户全部 strict
controller 推送 `playback.context.bindings.changed`。不得只通知 Context subscribers，不得跨用户
发送，也不得在事件中携带猜测的 active Context。

**REQ-025 — Pair-level control serialization**
当服务端接受普通 player control 时，服务端必须在 authority/device pair 级别与 ensure、close、
handoff authority switch 串行化，并原子验证请求 Context 是该 pair 唯一 active Context。出现多
Context 时必须返回带 canonical cursors 的 `conflict`，不得发送命令或 mutation；不得依赖客户端
先收到 binding invalidation 才保证安全。

**REQ-026 — Device-scoped remote volume**
当协商 `remoteVolumeControl:true` 的 controller 对同用户在线 player 发送 `device.setVolume` 时，
服务端必须按 `targetClientId` / `targetDeviceSessionId` 精确解析当前 Socket，并只向该 Socket 发送
无 target 字段的 command。目标必须同时协商 `remoteVolumeControl:true` 与 `canSetVolume:true`；
错误 pair、离线目标或能力不足必须 fail-closed。设备的 `device.volume.update` 必须按连接级
clientSeq event-confirmed、只保存在在线瞬态状态中，并且上述请求、执行与反馈均不得创建或修改
PlaybackContext 及其任何 cursor。

**REQ-027 — Player startup ensure**
当具有 player 角色且 canPlay:true 的设备完成 negotiated 注册时，它必须立即发送
`playback.context.ensure`，并携带当时可用的本地队列、currentIndex、播放状态和位置；只有本机确实
没有队列时才发送 idle shape。服务端必须原子返回当前唯一 Context、重绑同 stable clientId 的离线
旧 Context，或按该快照创建/初始化 Context。服务端不得要求设备先实际发声或由 controller 创建
Context，也不得产生第二个 active Context。

**REQ-028 — Idle Context closed shape**
当 Context 队列为空时，服务端必须输出 `queueSongIds:[]`、`state:"idle"`、`positionMs:0`，并省略
currentIndex/trackId；当队列非空时必须输出合法 currentIndex 与匹配 trackId，且 state 不得为 idle。
任何 request、response、push、持久化恢复和重启水合都必须保持该条件 schema。

**REQ-029 — Prepare before play**
当 controller 对 idle Context 发起 `playback.context.prepare` 时，服务端必须验证 intentId、最新
controlVersion、唯一 authority 和可选初始队列，最多建立一个 10 秒 prepare，并只向当前 authority
路由一次。authority 必须把队列写入同一 Context；controller 只有在 canonical queue 非空后才能使用
最新 controlVersion 发送原始 player.play。

**REQ-030 — Idle control fail-closed**
当 Context 为 idle 时，服务端收到 queue.playItem 或任一 player.* 请求必须返回 queue_required，
且不得递增 cursor、路由普通控制或伪造 playing/paused 状态。

**REQ-031 — Standby Context and Handoff**
当 Handoff 准备把 Context authority 切换到已经拥有 idle Context 的 target player 时，服务端必须在
同一原子提交中先把 target idle Context 写入 terminal tombstone，再安装 transferred Context binding；
如果 target Context 非 idle、存在非终态 prepare 或无法原子退休，则 Handoff 必须在 authority 切换前
返回 conflict。任何分支都不得让 target pair 暴露两个 active Context。

**REQ-032 — Remote control settlement**
当服务端接受 queue.playItem 或 player.* 时，必须为新 controlVersion 创建 pending 事务；correlated
ACK 只表示 accepted/routed。只有当前 authority 的 remoteCommand committed playback.update 可以把
匹配 pending 事务结算为 committed；failed update 结算为 failed；不得以 Socket emit 成功、ACK 或
canonical target snapshot 代替实际执行结果。

**REQ-033 — Canonical versus applied cursor**
当 authority 尚未执行最新控制时，服务端必须允许 `appliedControlVersion < controlVersion`，并在
status deviceStates 和 playback.update 中同时表达两个值。服务端必须按 applied transaction/snapshot
校验实际 track/state/position，不得要求 pending 期间实际 track 永远等于主 Context 最新控制目标。

**REQ-034 — Applied monotonicity**
当 playback.update 的 appliedControlVersion 低于该 device 的 lastAppliedControlVersion 时，服务端
必须忽略其状态副作用并记录迟到反馈；等值允许 passive 事实、匹配 pending command 的 failed 结果
或相同 terminal 幂等重放；高值必须由按序 remote committed 或 localUser transaction 证明。高于
canonical controlVersion 的 feedback 必须返回 bad_request。

**REQ-035 — Local user control allocation**
当当前 authority 发送合法 localUser committed playback.update 时，服务端必须在 Context 串行区从
当前 canonical controlVersion 加一，推进 applied cursor，按绝对 queueIndex/track/state/position
更新实际状态，并把旧 pending remote 标记为 superseded。observedControlVersion 小于或等于 canonical
可接受，大于 canonical 必须拒绝；服务端不得使用客户端猜测的新版本。

**REQ-036 — Local intent idempotency**
当同一 Context/epoch 的 localUser intentId 和内容重复时，服务端必须重放首次 canonical confirmation，
不得再次递增版本或 supersede；相同 intentId 内容不同返回 conflict。authority、deviceSession、epoch
或 Context lifecycle 改变时，旧 intent 不得应用到新 binding。

**REQ-037 — Failed command reconciliation**
当 remote command failed 且 accepted target 已改变主 Context state/currentIndex 时，服务端必须在同一
结算中使用新的 Context version、必要时新的 Queue revision 恢复实际 snapshot，但 controlVersion
保持已分配值。服务端不得在同一完整 cursor 下静默改写 Context，也不得把失败命令标记为 applied。

**REQ-038 — Supersede execution barrier**
当 localUser update 被接受时，服务端必须持久化 supersededThroughControlVersion 并只将对应 pending
事务改为 superseded。Windows 必须暂缓本地 intent 期间的未完成远程命令，收到 canonical localUser
confirmation 后丢弃不高于该上界的未完成事务；更新版本的后续远程命令仍必须可执行。

**REQ-039 — Dependent command failure**
当 `queue.playItem`、`player.next` 或 `player.prev` 失败时，服务端必须在同一 Context/epoch 串行事务中把
所有更高版本且仍 pending 的远程事务标记为 failed/dependency_failed，保持最高 controlVersion 不变，
按 lastApplied 对账实际状态，并按版本升序为每个受影响事务发送独立 playback.control.settled。
Windows 必须丢弃这些后续命令；controller 按具体 commandControlVersion 结束 pending，合并刷新 status
后再操作，不得根据版本区间自行猜测 terminal。

**REQ-040 — Remote execution deadline**
当服务端接受并路由远程事务时，server-routed command 必须携带默认 15000ms 的
executionTimeoutMs，服务端 watchdog 必须是该值加 2000ms。Windows 只有在期限到达且能够证明 audio
execution lease 已失效时才可发送 remoteCommand failed/execution_timeout；服务端 watchdog 到期仍无
terminal feedback 时只能结算为 failed/execution_unknown 并发送 playback.control.settled，不得生成
playback.update 或 clientSeq。

**REQ-041 — Unknown execution is never replayed**
当 authority 断线、连接被替换或服务端重启使 pending command 的执行结果无法证明时，服务端必须把
相关事务结算为 failed/execution_unknown；服务端仍在线时为每个事务发送独立
playback.control.settled。authority reconnect/ensure 后不得自动重投；Flutter 必须清除旧 pending、重新
list/subscribe/status，并只根据新水合状态继续控制。

**REQ-042 — Late feedback source-only correction**
当 authority 发送 appliedControlVersion 低于 lastAppliedControlVersion 的旧状态时，服务端必须忽略其
状态副作用，只向该请求 Socket 返回当前保存的 passive canonical playback.update，并缓存为该请求的
event confirmation。不得广播给其他 recipient；不同 terminal 结果仍返回 conflict。

**REQ-043 — Server settlement identity and ordering**
当服务端发送 playback.control.settled 时，payload 必须使用 requestingClientId 表示原远程命令请求者，
不得使用 sourceClientId、deviceSessionId 或 clientSeq 冒充 authority feedback。dependency cascade 必须
先原子提交全部 terminal，再按 commandControlVersion 递增逐条发送；相同 settlement 重复无副作用，
同一 `(playbackContextId, epoch, commandControlVersion)` 出现不同 terminal 内容必须记录协议冲突。

**REQ-044 — Windows timeout readiness gate**
当 Windows 声称支持 r11 硬超时时，必须证明 lease 失效后能停止、取消或隔离仍在运行的音频操作，迟到
Future/callback 不能切歌、发送 committed、修改 Context/Queue projection 或系统媒体状态，且更高版本
命令仍可执行。未通过自动化测试时不得发送 execution_timeout，也不得把该客户端/部署标为完整 r11
Core ready；服务端 watchdog 只能使用 execution_unknown。

**REQ-045 — Windows consumes server settlement**
当服务端发送 playback.control.settled 时，若最初接收该 command 的 authority Windows Socket 仍在线，
必须把它作为 recipient。Windows 收到后必须按 Context/epoch/commandControlVersion 使对应 execution
lease 失效、从本地队列删除事务并隔离迟到 callback。旧 Socket 已断开或被替换时不得向新物理连接补发
历史 settled；新连接按 nonce 变化清理旧事务。settled 不允许直接修改实际播放状态；任何后续实际事实
仍由 Windows playback.update 报告。

### 7.1 安全与资源限制

1. 除仅绑定 loopback/LAN 且由开发者明确接受风险的本地 lab 外，端点必须使用 TLS
   (`https`/`wss`)；`auth.login` 含明文密码字段，公网明文 HTTP/WebSocket 禁止部署。
2. 服务端不得记录 `auth.login.payload`、密码、完整凭据或包含它们的原始 envelope。审计日志
   只记录 requestId、action、认证后的 user/client、结果 code、延迟和脱敏端点。
3. 生产环境 Engine.IO/Socket.IO Origin 必须使用部署 allowlist；不得在携带凭据时使用通配符
   `*`。只有显式开发模式可启用 `*`，且启动日志必须输出安全警告。非浏览器客户端没有 Origin
   时，必须依靠认证和网络策略，不能伪造 Origin 放行。
4. 默认资源上限：每 IP 同时 10 条未认证连接、每用户 20 条已认证连接；每连接每分钟 120 个
   strict 请求，其中 player control 与 localUser playback.update 合计每秒最多 20 个、普通 passive
   playback.update 每秒最多 10 个，ensure/prepare/handoff/broadcast start 每分钟各 10 个。每个 active
   Context 最多保留最近 512 个 terminal control transaction 和 512 个 local intent dedupe 记录；更老
   记录可压缩为 cursor/tombstone，但不能在仍可能重试的 10 分钟内删除。部署可调低，调高必须有
   负载测试证据。超限返回 `rate_limited` 或在握手阶段拒绝连接。
   server-routed command 的 executionTimeoutMs 默认 15000ms，允许部署调整；服务端 watchdog 固定为
   executionTimeoutMs + 2000ms。调整必须保持有界且不能关闭 terminal settlement。
5. 必须设置 Engine.IO payload 上限不高于 256 KiB；transport 超限使用 message-too-big 行为断开，
   已进入 handler 的业务限制超限返回 correlated `bad_request`。malformed JSON、非 object
   envelope 不得进入 handler；格式合法但不在 allowlist 的 action 返回 `not_supported`；缺失或
   非法 action/requestId 时按第 2.2 节断开。
6. 每个 action 在执行前重新校验 authenticated user、Context membership、角色、capability 和
   当前 sid 绑定；不能只在 subscribe 时授权一次。`playback.context.list` 还必须先按当前用户
   限定查询，再精确匹配 authority client/device pair；错误消息不得泄露其他用户资源是否存在。
7. 必须配置 ping/pong dead-connection cleanup、发送缓冲上限和背压策略。控制命令不可作为
   volatile broadcast；无法可靠单播给 authority 时返回 `authority_offline`。

### 7.2 单实例、持久化与重启

- strict-v2 2.4.x 当前只支持单 realtime worker。服务端在能够识别 Gunicorn/processes/workers 等
  多进程配置时必须启动失败，不得仅警告后继续运行。多 worker 支持必须另立工程目标，并同时
  提供 sticky sessions、跨 worker broker、共享原子 Context、共享 sid/subscription/dedupe store。
- active Context 与 closed tombstone 必须持久化。重启恢复时保留
  `epoch/version/queueRevision/controlVersion`、authority client/device identity、每个 authority device
  的 lastAppliedControlVersion、control transaction terminal/pending 状态和 local intent dedupe 结果；
  清空全部 sid、nonce、connectionEpoch、connection-scoped request cache 和 subscription，等待客户端
  重新注册，并用新的 requestId 重新 list/subscribe/status。
- graceful restart 必须先停止接收新连接，完成或明确失败正在结算的请求，停止创建新
  handoff/broadcast，再关闭 Socket。不能把未完成 handoff 恢复为 completed。
- 重启时非终态 Handoff 进入 `failed`，`errorCode:"server_restart"`；Follow 全部清除；active
  Broadcast 进入 terminal stopped 并冻结 cursor。基础 Context 继续保留。
- authority 断线、连接被替换、graceful shutdown 和重启恢复时，pending remote control 一律结算为
  failed，`errorCode:"execution_unknown"`。服务端仍有在线 controller Socket 时，为每个事务发送独立
  playback.control.settled；完整重启后不补发历史 push。服务端不得在 authority 重新 ensure/status 后
  按原版本重投；客户端必须因 nonce 变化清除旧 pending 并从新 status 恢复，避免重复音频执行。
- `connectionEpoch` 每个新物理连接固定为 1；不得持久化或复用旧 nonce。

### 7.3 Capability profile readiness

Core profile 等于握手/注册、启动 ensure、idle/queue-backed Context、prepare、Context
discovery/binding invalidation、Queue、Player Control 和 playback.update 控制结算，缺一不可；服务端只有在
`playback.context.ensure`、`playback.context.prepare`、`playback.context.prepared`、
`playback.context.list`、`playback.context.bindings.changed` 及其他 Core action 的
request/response/event、cursor、routing、control/applied 分离、terminal transaction、
playback.control.settled、executionTimeoutMs/audio lease、dedupe 和 error conformance 全部通过后，
才可用 major `2`、minor `>=4` 的 `protocolVersion` 接受
`playbackContextV2:true`，否则注册返回 `not_supported`。Handoff、Follow、Broadcast 是三个
独立 profile，部署默认关闭；每个 profile 只有在本文对应状态机和双客户端 conformance 测试完成
后才能在 `negotiatedCapabilities` 返回 true。metadata/profile 的 TOFU 成功不自动证明或开启
任一可选 capability。

`remoteVolumeControl` 属于固定注册 shape 中的能力字段，不存在旧 shape 兼容。服务端只有在设备级
请求、目标路由、在线状态、event confirmation 与断线清理全部通过 conformance 后，才能向请求该
能力的连接协商 true。

## 8. 当前不应实现为 strict-v2 的旧 surface

| 禁止作为 strict 方案 | 原因 |
| --- | --- |
| `session.subscribe` / `session.unsubscribe` | strict 改用 `playback.context.subscribe` / `unsubscribe`。 |
| `queue.session.sync` / `queue.local.set` / `queue.ready.complete` | strict queue 是 `queue.context.sync`，旧队列消息会被 router quarantine。 |
| `sessionId`、`sourceSessionId` | strict playback 主键只能是 `playbackContextId`；设备稳定身份是 `deviceSessionId`。 |
| 服务端业务 push / direct response 的 target 字段 | strict router 拒绝，服务端应按 Socket recipient 分发。客户端请求例外只有 `playback.handoff.start.targetClientId` 与 `device.setVolume` 的精确 target pair。 |
| `player.setVolume` / `player.requestState` | strict 音量是设备级 `device.setVolume`；不要复用 legacy player action。`player.requestState` 仍未纳入。 |
| `auth.login` / `device.register` actionless ACK | probe/negotiated client 会拒绝。 |

`canSetVolume` 只表示 player 能执行本地音量设置；`remoteVolumeControl` 表示连接理解 strict-v2 `2.4.0`
设备级 action。目标 player 必须两者都为 true；controller 自身可以 `canSetVolume:false`。

## 9. 已知边界与联调验收

这是服务端与 Flutter 客户端共同遵守的规范，不证明当前任何一端已经完成实现。双方完成后必须验证：

1. probe 注册成功后客户端保存 profile、主动重连、第二次 negotiated 注册成功；
2. roles 单角色/双角色、完整 negotiatedCapabilities、Core not_supported 和 optional profile 降级；
3. 只有 major `2`、minor `>=4` 的完整固定注册 shape 可进入本文 strict Core；低于 `2.4.0`、其他 major、缺字段、非法 hash/commit、actionless ACK 均 fail-closed；
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
   subscription 和重连水合；
9. Cursor 矩阵每一行的递增、不递增、旧值拒绝、等值去重、必需 clientSeq 和五个 event-confirmed action 的精确单 Socket 重放；
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
   participant 禁止控制、Handoff errorCode 和 terminal 幂等；
15. Broadcast 初始 participantStates 省略 paired feedback 字段，首个 feedback 后两字段同时出现；
16. transport message-too-big 断开、business limit bad_request、非法 requestId/action 断开，以及未知字段/null/rate 限额；
17. nonce CSPRNG/128-bit 熵，以及 queueSongIds 在序列化、持久化和重启后的顺序保持；
18. 单 worker 启动保护、Context/tombstone 重启恢复，以及 Handoff/Follow/Broadcast 显式终止；
19. Android 选择 Windows 后发现唯一 queue-backed Context，status 水合队列/索引/播放状态/cursors，
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
27. negotiated register 返回低于 `2.4.0`、其他 major，或合规连接上 Core action 返回
   `not_supported` / `capability_required` 时，Flutter fail-closed、清理远控状态且绝不 session fallback；
28. passive playback.update 只更新实际进度/状态，controlVersion 和全部 Context cursor 不变；canonical
   push 同时带最新 controlVersion 和 device appliedControlVersion；
29. 手机远程命令 47 ACK 后事务为 pending，Windows committed update 将其结算为 committed，applied
   推进到 47，但 controlVersion 不再次递增；
30. 远程命令 47 失败时 commandControlVersion=47、appliedControlVersion 保持 46，事务进入 failed；
   主 Context 如已写入预期 track/index，使用更新的 Context version/Queue revision 对账回实际状态，
   controlVersion 仍为 47；
31. 切歌命令 47 失败而 48、49 仍 pending 时，47 使用原失败码，48、49 原子进入
   failed/dependency_failed；Windows 不执行 48、49，服务端按 48、49 顺序分别发送
   playback.control.settled，requestingClientId 保留各自原 controller，手机按具体版本清除 pending 并
   合并请求一次 status；
32. 服务端已接受 48、Windows 只执行到 47 时，status/deviceStates 和 playback.update 合法表达
   controlVersion=48/appliedControlVersion=47，实际旧 track 按 applied snapshot 校验而不是被误拒绝；
33. lastApplied 已为 48 后到达 passive/相同 terminal 的 remote 47 feedback 时不覆盖歌曲、状态和
   位置、不广播，只向源 Windows 返回当前 passive canonical update；不同 terminal 返回 conflict，
   applied 高于 canonical 时返回 bad_request；
34. Windows observed 46、服务端 canonical 47 时，合法 localUser committed 获得 48，并只把仍为
   pending 的 47 及更低事务标记为 superseded；committed/failed 历史不回滚；
35. localUser 先获得 47 后，手机仍用 base 46 的命令返回 stale_version 且不执行；
36. 同一 local intentId 相同内容重试重放原 confirmation，不增加到 49；相同 intentId 不同内容返回
   conflict；epoch/authority/deviceSession 改变后旧 intent 不能应用；
37. 本地操作失败不产生 committed localUser update、不推进版本、不 supersede；
38. Windows 本地屏障保证已经送达但尚未完成的旧远程命令不会在 localUser confirmation 后晚执行；
   使用最新 48 的新远程命令 49 仍正常执行；
39. 首次连接前 Windows 已播放时，ensure 按实际 snapshot 创建版本 1，后续 passive update 仍为 applied
   1，不额外生成 localUser 版本 2；
40. server-routed control 携带 executionTimeoutMs=15000；Windows 在期限内成功/失败正常结算，期限到达
   且已证明 execution lease 失效时发送 remote failed/execution_timeout，17 秒后的迟到 callback 不能
   切歌、发送 committed 或覆盖更新状态；
41. 服务端默认 17000ms watchdog 到期仍无 terminal feedback 时，使用
   playback.control.settled/execution_unknown，不生成 playback.update、不使用 clientSeq；
42. authority 断线或服务端重启时所有 pending 进入 failed/execution_unknown；服务端仍在线时逐条发送
   settled，完整重启后客户端按 nonce 清除旧 pending，任何分支都不自动重投旧 command；
43. 相同 `(playbackContextId, epoch, commandControlVersion)` settled 重复到达时 Flutter 幂等忽略；同键
   不同 status/errorCode 记录协议冲突并刷新 status，旧 settled 不回滚更新 applied state；
44. Windows 音频层不能阻止超时后迟到执行时，不发送 execution_timeout，服务端只能 unknown，部署不
   得标为完整 r11 Core ready；
45. dependency_failed/execution_unknown settled 同时发送给仍持有该 transaction 的在线 Windows；Windows
   按 Context/epoch/version 使 lease 失效并删除事务，之后迟到 callback 不切歌、不发送 committed、不
   修改 Context/Queue/系统媒体状态，更高版本命令仍可执行；
46. 自然播完自动下一首不使用 origin localUser，不获得本地人工 supersede 权限；
47. Android 与 Windows 各一台真实 Flutter 客户端联调，记录可复现日志。

## 10. 协议权威性、证据来源与部署证据

本文件是服务端工程师与 Flutter 工程师共同核对的唯一完整 wire contract。其他材料的职责为：

- `ref/emosonic_strict_v2_protocol_metadata_goal.md`：只定义 `device.register` metadata 描述符及其 hash 语义；不覆盖业务 action；
- `ref/emosonic_strict_v2_server_change_note.md`：只记录某个服务端版本声称完成的行为；不得覆盖本文件；
- Flutter `test/fixtures/emo_protocol/strict_v2/manifest.json`：从本文派生的 machine-readable
  conformance inventory；不得覆盖本文，必须由 Flutter conformance tests 保持字段清单一致；
- Flutter `test/fixtures/emo_protocol/strict_v2/`：canonical fixture 根目录；fixture 必须存在且
  SHA-256 匹配。服务端仓库可维护等价测试数据，但不得改变 wire shape。

本文从 Flutter strict-v2 vertical slice 提取；r11 继续使用 `2.4.0`，保留待机 Context、启动 ensure、
远程 prepare 和设备级远程音量，并新增 control/applied 分离、远程事务 terminal 结算与 localUser
playback.update 服务端版本分配。服务端和 Flutter 实现及 fixtures 都必须
更新，并通过 conformance tests 保持一致：

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
