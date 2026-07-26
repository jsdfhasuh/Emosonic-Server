# 阶段 0：传输、注册与时钟

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-07-23-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 2—3 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
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
  -> playback.context.status（读取并应用服务端 canonical queue/playback/cursors）
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
- `effectiveAtPlayback:true` 表示连接具备服务端时钟映射和计划执行能力，可以独立于
  `playbackPrepare` 协商；Handoff target 才同时要求两者为 true。

请求中的 capabilities 描述客户端自身能力；服务端必须将它们与部署 readiness 求交集，并在 ACK
中返回完整 `negotiatedCapabilities`。Core profile 未 ready 时不得静默降级：服务端返回
`not_supported`，不返回 strict metadata，也不转入 legacy。可选 profile 未 ready 时注册仍可
成功，但对应 negotiated 值必须为 false。

协商结果还必须满足角色和能力依赖：`supportsFollow:true` 要求 player + canPlay；
`playbackPrepare:true` 只可授予能预加载 Handoff target 的 player；`effectiveAtPlayback:true` 只可授予
能满足第 3.5 节时钟与迟到规则的 player。controller-only 连接可以协商 `supportsBroadcast:true` 以
创建和控制 Broadcast，但只有同时具备 player + canPlay + canPause + canSeek 的连接才可成为
ordinary participant。

`supportsBroadcast:true` 是复合执行承诺，不只表示理解 action 名。作为 source 或 ordinary participant
的连接还必须协商 `playbackContextV2:true`、`effectiveAtPlayback:true`，具有 player 角色以及
`canPlay:true`、`canPause:true`、`canSeek:true`，并能够准确设置并保持本文允许的任意合法 `playbackRate`
（有限 number，`0.5 <= playbackRate <= 2.0`），并按 effective-at 执行 queue/play/pause/seek/速度变化；本版本不另加 playback-rate capability 字段。不能满足
该承诺的 player 必须请求或协商 `supportsBroadcast:false`。服务端选择目标时必须跳过未协商成功的
设备，并把显式请求中的此类 ordinary 设备列入 `skippedClientIds`，不得先加入后再静默忽略
playbackRate。source 还必须是当前 Context 在线 authority pair、通过第 3.5 节时钟门禁，并满足第
5.5.1 节 fresh、settled、playing 状态门禁；source 不要求 `playbackPrepare`、`canSetVolume` 或
`remoteVolumeControl`。

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
      "protocolVersion": "2.8.0",
      "schemaHash": "optional-observation-value",
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
| `strictV2.protocolVersion` | 是 | 解析 numeric major/minor；major 必须为 `2` 且 minor 必须 `>=8`，不得用字符串字典序比较；其他版本 fail-closed |
| `strictV2.schemaHash` | 否 | 可选部署观测值；缺失、变化、空值、类型或格式异常均不得使注册失败、断开连接、停止 strict Core、关闭 capability 或回退 legacy。收到时可以原样记录或忽略，不参与兼容判断 |
| `strictV2.serverBuildCommit` | 是 | 40 位小写 Git SHA，或精确值 `unknown` |
| `strictV2.connectionNonce` | 是 | 非空 string |
| `strictV2.connectionEpoch` | 是 | 精确为整数 `1`，不能是 string |
| `negotiatedCapabilities` | 是 | 与请求一致的完整固定 10 个 bool 字段；后续 capability gate 的唯一依据 |

合规服务端只在 `payload.strictV2` 输出 metadata，且只使用 `serverBuildCommit`。Flutter 对
`serverCommit` 或 ACK payload 顶层 metadata 的读取仅是历史部署兼容，不属于合规服务端输出
schema。strict ACK 不返回 `client` 对象；设备详情统一通过 `device.list` 获取。

注册握手描述符必须覆盖 ACK 的 `payload.action`、`clientId`、`deviceSessionId`、固定 10 个 bool
的 `negotiatedCapabilities`，以及 `strictV2` 中必需的 protocolVersion、serverBuildCommit、
connectionNonce、connectionEpoch；描述符可以另行记录可选 schemaHash，但不得把它加入 required
列表或格式门禁。上述规范字段的名称、类型、required、枚举或约束发生变化时必须更新描述符；
服务端若继续生成 schemaHash，可以同步重新计算，但客户端不得比较固定值或用它限制连接和能力。
`auth.login` 不属于注册描述符。

`schemaHash` 缺失或发生任意变化时，客户端最多更新该服务器的本地观测记录；不会因此降级或
fallback。wire contract 变化时必须同步更新 protocolVersion 和 schema 描述，不得只改实现。

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

只有完成 `device.register` 后才允许应用层 heartbeat；未注册请求返回 `unauthorized`。每个
player 连接注册成功后必须立即顺序发送 ping，每收到一个合法 pong 后再发下一个，
直到当前 connectionNonce 至少有 3 个有效 clock 样本。协商 `effectiveAtPlayback:true` 的连接从
注册成功起就必须始终保持最大 10 秒的 ping 间隔，不得等到进入 Broadcast/Handoff 才加快。
其他 ready 连接在初始 3 样本完成后约每 30 秒发送：

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

`serverTimeMs:int>=0` 是 strict-v2 `2.8.x` 必需字段，表示服务端构造 pong payload 时的 Unix epoch
milliseconds；缺失或非法时客户端必须把该连接的 `effectiveAtPlayback` 视为不可用。客户端以本地
send/receive midpoint 估算 server offset 和 RTT，并保留最近 5 个样本。执行任何 effective-at action 前
必须至少有 3 个样本、最新样本不超过 15 秒，且
`clockUncertaintyMs = max(latestRttMs / 2, recentOffsetRangeMs) <= 50`；否则不得声称已应用，并按动作通道
报告稳定错误 `clock_unsynchronized`。

服务端必须按 connectionNonce 记录已处理的合法 ping 数和最近时间。当前 nonce 少于 3 个
ping，或最近 ping 超过 15 秒时，服务端不得把该 player 选为 Broadcast source/ordinary
participant 或 Handoff target，也不得向它分发 effective-at action。Broadcast 显式目标进入
`skippedClientIds`；必须定向的 Handoff/source action 返回 `conflict`。这是动态可用性门槛，不修改
注册时的静态 negotiated capability。

客户端收到计划 action 时，必须用估算 server clock 判断：

1. 尚未到 `effectiveAtServerMs`：在该时刻执行；
2. 已迟到但不超过 1000ms：立即追赶；playing target 将 positionMs 按迟到时间和 playbackRate 向前
   投影，paused/stopped target 使用原 positionMs；
3. 迟到超过 1000ms：不得把 action 报为成功。ordinary Broadcast participant 发送 failed
   `broadcast.feedback` 完整 shape（`errorCode:"effective_at_missed"`）；source 普通 Context command 发送
   remoteCommand failed `playback.update`；Handoff target 按 commit timeout/failed 结算。

服务端生成 effective-at 时仍必须满足本文对应最小 lead；250ms 是绝对下限，不是对高 RTT 或加载耗时
的成功保证。客户端实时 clock quality 不改变注册时静态 capability，但决定本次 action 的 applied/failed
结果。
