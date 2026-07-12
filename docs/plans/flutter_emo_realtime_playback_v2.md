# EmoSonic 实时播放协议 · Flutter 接入指南（群播 / 跟播 / 控制 / Handoff）

> 面向 Flutter 工程师。基于当前服务端实现（`supysonic/emo/ws.py` 等）逐行核对，字段名、错误码、payload 形状均以代码为准。
> 传输：Socket.IO，namespace **`/emo`**，路径 **`/emo/ws`**。所有业务消息走 socket 的 **`message`** 事件。
>
> **一句话建议**：新客户端一律走 **PlaybackContext v2**（注册时声明 `capabilities.playbackContextV2=true`），
> 用 `playbackContextId` 作为播放主键，**不要再传 `sessionId`**（strict-v2 下带 `sessionId` 会被 `bad_request` 拒绝）。

---

## 0. 消息信封与通用约定

### 0.1 信封结构

**客户端 → 服务端**（`socket.emit('message', {...})`）：
```json5
{
  "type": "message",           // 客户端发送可省略/任意，服务端按 action 路由
  "action": "player.pause",    // 必填，决定路由
  "payload": { /* 各 action 的字段 */ },
  "requestId": "req-123"       // 强烈建议：用于把 ack/error 对回请求
  // 部分 action 还在顶层带 targetClientId / sourceClientId（见各章）
}
```

**服务端 → 客户端** 有三类 `type`：
| type | 典型 action | 含义 |
| --- | --- | --- |
| `system` | `system.ack` / `system.error` / `system.pong` | 对某次请求的应答（带同一 `requestId`） |
| `state` | `playback.update` / `playback.context.status` / `queue.context.sync` / `device.list` / `broadcast.status` | 状态广播（订阅/参与才会收到） |
| `command` | `player.play` / `player.pause` / `playback.prepare` / `playback.handoff.release` / `broadcast.*` | 服务端下发给**具体设备**执行的指令（带 `targetClientId`） |

所有服务端消息都含 `timestamp`（秒，float）。payload 里若含 `serverUpdatedAtMs`，服务端会额外注入 `serverTimeMs`（当前服务器毫秒），用于本地时钟对齐。

### 0.2 成功 / 失败应答

- 成功：`{type:"system", action:"system.ack", requestId, payload:{...}}` —— `payload` 是各 handler 的返回体。
- 失败：`{type:"system", action:"system.error", requestId, payload:{code, message, ...extra}}`。

### 0.3 错误码总表（客户端应全部处理）

| code | 触发场景 | 附加字段 |
| --- | --- | --- |
| `unauthorized` | 未先 `auth.login` / 凭据错误 | — |
| `forbidden` | 未注册设备、跨用户访问、无控制权、能力不足（如 handoff 目标缺能力） | — |
| `not_found` | context / handoff / broadcast / 目标设备 不存在或离线 | — |
| `bad_request` | 缺必填字段、类型错误、越界、**strict-v2 下带了 `sessionId`** | — |
| `conflict` | 版本冲突（乐观锁） | `currentControlVersion` / `currentQueueRevision` / `currentVersion` 之一或多个 |
| `authority_offline` | v2 控制时 context 的 authority 设备已离线 | — |
| `follow_control_forbidden` | 跟播者试图控制其跟随的源 | — |
| `not_supported` | 未知 action | — |
| `stale_client_seq` | 客户端序列号过期 | `currentClientSeq` |

> `conflict` 时务必读取 `currentControlVersion` 等字段，用它作为下一次请求的 `baseControlVersion` 重试。

---

## 1. 连接与握手（所有操作的前置）

### 1.1 顺序：connect → auth.login → device.register

**① auth.login**（`ALLOWED_PRE_AUTH`，连接后第一步）
```json5
// 发送
{ "action": "auth.login", "requestId": "r1", "payload": { "u": "alice", "p": "password" } }
// ack
{ "action": "system.ack", "requestId": "r1", "payload": { "authenticated": true, "userName": "alice" } }
// 失败 -> code: "unauthorized"
```

**② device.register**（注册本设备，拿到 `clientId` 身份）
```json5
{
  "action": "device.register",
  "requestId": "r2",
  "payload": {
    "clientId": "phone-1",                 // 必填，设备唯一 ID（自己生成并持久化）
    "deviceSessionId": "device:phone-1",   // ★ strict-v2 下必填非空（无兜底），省了 -> bad_request
    "deviceName": "Alice 的手机",
    "alias": "手机",                        // 可选，缺省=deviceName
    "roles": ["player", "controller"],     // player=可被控播放；controller=可远控/发起 handoff
    "capabilities": {
      "playbackContextV2": true,           // ★ 声明走 v2 严格协议（关键）
      "playbackPrepare": true,             // handoff/两阶段目标必备
      "effectiveAtPlayback": true          // handoff/两阶段目标必备
    }
  }
}
// strict-v2 ack: { "payload": { "client": { ... }, "strictV2": { protocolVersion, schemaHash, serverBuildCommit, connectionNonce, connectionEpoch } } }
```

要点：
- **`roles`**：`player` = 能作为播放设备被控制 / 被 handoff / 参与群播；`controller` = 能远控别的设备、发起 handoff、关闭 context。一个设备可同时具备。
- **`capabilities.playbackContextV2=true`** 决定该客户端进入 **strict-v2** 分支：所有 v2 播放 action 的 payload **禁止出现 `sessionId`**，`playbackContextId` 只从 `payload.playbackContextId` 取（不再从 sessionId 兜底）。
- **strict-v2 下 `deviceSessionId` 是必填非空**（`_register_device`：strict-v2 分支不再从 `sessionId`/`clientId` 兜底）；缺失或空 → `bad_request`。只有非 v2（legacy）客户端才有 `deviceSessionId → sessionId → clientId` 的兜底。strict-v2 的 client info 里也**不含 `sessionId` 键**。
- handoff / 两阶段群播的**目标设备**必须同时具备 `playbackPrepare` + `effectiveAtPlayback`，否则相关操作 `forbidden`。
- 注册成功后服务端会向同用户所有在线设备广播一条 `state` / `device.list`（设备上下线通知）。strict-v2 客户端注册时**不会**触发 legacy 持久态恢复。

### 1.1.1 strictV2 注册握手元数据

当且仅当注册 payload 声明 capabilities.playbackContextV2=true 时，成功 ACK 会额外包含：

~~~json5
{
  "strictV2": {
    "protocolVersion": "2.1.0",
    "schemaHash": "<64位小写 SHA-256>",
    "serverBuildCommit": "<40位小写 Git SHA 或 unknown>",
    "connectionNonce": "<本次 Socket.IO 连接的随机字符串>",
    "connectionEpoch": 1
  }
}
~~~

前三项是可复现的**注册握手 profile / 部署身份**；后两项是当前连接的动态证据。它们都不是所有 realtime action 的完整 schema：

- schemaHash 只覆盖 strict-v2 device.register 请求、对应的 system.ack/system.error 以及必要消息信封；
- 它不保证 playback.update、player.*、follow.*、broadcast.* 或 handoff.* 的 payload 形状；
- Flutter lab 的预期值必须来自部署 manifest、CI 输出或测试环境配置，不能用 Flutter commit、fixture 或刚收到的 ACK 推测；
- serverBuildCommit=unknown 只允许本地开发。正式部署验证必须拒绝 unknown、短 SHA 或非法 SHA；
- connectionNonce 由服务端在每次 Socket.IO 连接建立时随机生成；lab 必须拒绝缺失或空 nonce，并在重连后确认它已变化。它不是部署 manifest 中的预设值；
- connectionEpoch 当前固定为整数 1；lab 必须精确校验该值，未来变更代表动态连接证据语义升级；
- 普通客户端可将 serverBuildCommit 用于诊断；协议兼容应以支持的 protocolVersion 与 schemaHash 为准。
- 非 Docker 的源码、wheel 或 systemd 部署必须在启动服务前显式设置完整 SHA 格式的 `EMO_SERVER_BUILD_COMMIT`；未设置时服务端会报告 `unknown`。

### 1.2 device.list（拉当前在线设备）
```json5
{ "action": "device.list", "requestId": "r3", "payload": {} }
// 服务端回一条 state/device.list: { "payload": { "devices": [ {clientId, deviceName, roles, capabilities, ...}, ... ] } }
```
strict-v2 客户端收到的 device 条目里不含 `sessionId`。

### 1.3 PlaybackContext —— v2 的播放主键（群播/控制/handoff 都围绕它）

`PlaybackContext` = 一个「播放任务」，跨设备共享。核心字段（`serializePlaybackContextV2` 输出，**无 `sessionId`**）：
```json5
{
  "playbackContextId": "playback:alice:main",
  "userName": "alice",
  "authorityClientId": "phone-1",   // 当前真正在播放（拥有播放权）的设备
  "originClientId": "phone-1",
  "queueSongIds": ["s1","s2","s3"], // 共享队列
  "currentIndex": 0,
  "trackId": "s1",
  "state": "playing",               // playing/paused/stopped/...
  "positionMs": 32000,
  "queueRevision": 1,               // 队列版本（乐观锁）
  "controlVersion": 1,              // 控制版本（远控/handoff 乐观锁）
  "version": 1,                     // 整体版本
  "epoch": 1,                       // 切歌代号
  "logicalVolume": 40,              // 若设置：应用内统一音量（设备真实音量放 DevicePlaybackState）
  "serverUpdatedAtMs": 1780000000000
}
```

**必须先创建 context 才能远控/handoff/订阅它**（不再隐式创建）。

---

## 2. PlaybackContext 生命周期（v2 基础，控制/handoff 的前置）

### 2.1 playback.context.create —— 创建播放任务（唯一的创建入口）
```json5
{
  "action": "playback.context.create",
  "requestId": "c1",
  "payload": {
    "playbackContextId": "playback:alice:main",  // 必填非空；strict-v2 禁 sessionId
    "deviceSessionId": "device:phone-1",          // 必填非空（可从注册时的 deviceSessionId 取）
    "queueSongIds": ["s1","s2","s3"],             // 可选，默认 []
    "currentIndex": 0,                             // 队列非空时须 0<=idx<len；空队列须 0
    "positionMs": 0
  }
}
// ack: { "payload": { "created": true, "playbackContext": <v2 context> } }
```
- 发起设备自动成为 `authorityClientId`。
- **幂等**：同 id 已存在则返回 `created:false` + 现有 context（不报错）。跨用户同 id → `forbidden`。
- 只有本 action 能新建普通 v2 context；`queue.context.sync` / `playback.update` 都只更新已存在的 context（缺失返回 `not_found`）。

### 2.2 playback.context.status —— 查询 context + 各设备状态
```json5
{ "action": "playback.context.status", "requestId":"s1", "payload": { "playbackContextId": "playback:alice:main" } }
// ack payload:
{ "playbackContext": <v2 context>, "deviceStates": [ <DevicePlaybackState v2>, ... ] }
```
context 不存在 → `not_found`。`deviceStates` 是各参与设备上报的真实播放状态（`isAuthority`/`mode`/`volume` 服务端总会设置；`muted` **仅当设备在自己的 `playback.update` 里带了才有**；无 `sessionId`）。

### 2.3 playback.context.subscribe / unsubscribe —— 订阅状态推送
```json5
{ "action": "playback.context.subscribe",   "requestId":"sub1", "payload": { "playbackContextId": "playback:alice:main" } }
{ "action": "playback.context.unsubscribe", "requestId":"sub2", "payload": { "playbackContextId": "playback:alice:main" } }
// ack: { "payload": { "subscriptions": ["playback:alice:main", ...] } }
```
- subscribe 成功后，服务端立即向你推一条 `state` / **`playback.context.status`** 快照（`{playbackContext, deviceStates}`）。
- 之后该 context 每次变化，你会收到 `state` / **`playback.update`**（payload = 扁平 v2 context）。
- ⚠️ **注意形状差异**：首帧快照是 `playback.context.status`（嵌套 `{playbackContext, deviceStates}`），后续更新是 `playback.update`（扁平 context）；队列变化是 `queue.context.sync`（扁平 context）。客户端需按 action 名分别解析。（这点服务端有待统一，见团队内 review-fixes 文档 m5；接入时先按现状处理。）

### 2.4 queue.context.sync —— 同步共享队列（仅 authority 可改）
```json5
{
  "action": "queue.context.sync",
  "requestId": "q1",
  "payload": {
    "playbackContextId": "playback:alice:main",
    "deviceSessionId": "device:phone-1",
    "queueSongIds": ["s1","s2","s3","s4"],
    "currentIndex": 1,
    "positionMs": 0,
    "baseQueueRevision": 1               // 可选乐观锁；不匹配 -> conflict + currentQueueRevision
  }
}
// ack: { "payload": { "updated": true, "queue": <v2 context> } }
```
- 只有 `authorityClientId == 你的 clientId` 才能改，否则 `forbidden`（authority mismatch）。
- context 不存在 → `not_found`（不会隐式创建）。
- 成功后**仅当队列身份（`queueSongIds` 或 `currentIndex`）发生变化时** `queueRevision` 才 +1；纯 `positionMs` 变化或原样重发不会 +1。请以 ack 返回的 `queue.queueRevision` 为准，别假设每次 sync 都单调递增。
- 成功后向订阅者/参与者广播 `state` / `queue.context.sync`（payload = 扁平 v2 context）。

### 2.5 playback.context.close —— 关闭 context
```json5
{ "action": "playback.context.close", "requestId":"cl1", "payload": { "playbackContextId": "playback:alice:main" } }
// ack: { "payload": { "closed": true, "playbackContext": <v2 context, state=closed> } }
```
仅 authority 或 controller 可关；关闭后广播状态并清理订阅。

### 2.6 playback.update —— 播放设备上报真实状态（authority 与非 authority 语义不同）
播放设备（尤其 authority）应周期性上报自己的真实播放进度：
```json5
{
  "action": "playback.update",
  "requestId": "u1",
  "payload": {
    "playbackContextId": "playback:alice:main",
    "deviceSessionId": "device:phone-1",
    "state": "playing", "trackId": "s2", "currentIndex": 1, "positionMs": 15000,
    "queueSongIds": ["s1","s2","s3"],     // authority 上报可含队列
    "volume": 40, "muted": false          // 设备真实音量落 DevicePlaybackState
  }
}
```
- **你是 authority**（`authorityClientId==你`）：更新共享 context + 记 DevicePlaybackState(isAuthority=true)，广播 `playback.update`。ack `{updated:true, playbackContextId, authoritative:true, authorityClientId}`。
- **你不是 authority**：只记 DevicePlaybackState(isAuthority=false)，**不覆盖**共享 context。ack `{updated:true, playbackContextId, deviceFeedback:true, authoritative:false, authorityClientId, currentAuthorityClientId}`。
- strict-v2 下 context 不存在 → `not_found`（不隐式创建）；带 `sessionId` → `bad_request`。
- `queueSongIds` 必须是字符串列表；`currentIndex` / `positionMs` 必须是非负整数，且 authority 上报的 `trackId` 必须与队列和索引一致，否则 `bad_request`。

> 这一条是 **handoff 后**「源设备迟到上报无法夺回播放权」的机制来源：交接后源已不是 authority，其 playback.update 只会被当作设备反馈。

---

## 3. 远程控制（控制）

远控 = 一个 controller 设备去驱动 authority 设备的播放。**v2 客户端只需 `playbackContextId`**，服务端自己按 `authorityClientId` 找到目标设备下发指令。

CONTROL_ACTIONS：`player.play` `player.pause` `player.seek` `player.next` `player.prev` `queue.playItem` `player.setVolume` `player.requestState`。

### 3.1 何时走 v2 路径

满足任一即进入 **v2 context 控制**（`_handle_v2_context_control`）：
- 客户端注册了 `capabilities.playbackContextV2=true`；**或**
- payload 里带了 `playbackContextId` 或 `deviceSessionId`。

否则走 legacy（`targetClientId + sessionId`，见 §3.5）。**新客户端一律用 v2。**

### 3.2 v2 控制请求（统一形状）
```json5
{
  "action": "player.pause",                 // 或 play/seek/next/prev/queue.playItem
  "requestId": "p1",
  "payload": {
    "playbackContextId": "playback:alice:main",  // 必填非空
    "baseControlVersion": 8,                       // 可选乐观锁；!= 当前 controlVersion -> conflict
    "positionMs": 30200                            // seek 必填；pause 可选；play 默认取 context.positionMs
    // queue.playItem: 传 queueIndex 或 trackId
    // next/prev: 不用传 index，服务端按 currentIndex 计算
  }
}
// ack（所有 v2 控制统一）:
{ "payload": { "updated": true, "protocolPath": "playback_context_v2",
               "playbackContext": <v2 context>, "authorityClientId": "phone-1" } }
```

各 action 的 payload 差异：
| action | 关键字段 | 服务端行为 |
| --- | --- | --- |
| `player.play` | `positionMs?`（默认 context.positionMs） | 保持当前 index，令 authority 播放 |
| `player.pause` | `positionMs?` | context `state=paused`（+positionMs），持久化 |
| `player.seek` | **`positionMs` 必填(>=0)** | 只改 positionMs，保持播放态 |
| `player.next` | — | `requestedIndex = currentIndex+1`（越界 `bad_request`） |
| `player.prev` | — | `requestedIndex = max(0, currentIndex-1)` |
| `queue.playItem` | **`queueIndex`(或 `currentIndex` 兜底) 或 `trackId`（二选一必填）** | 索引优先取 `queueIndex`，缺则取 `currentIndex`；两者都无才用 `trackId`（须在队列内）解析出 index |

### 3.3 服务端做了什么
1. 校验 context 存在（否则 `not_found`）、同用户（否则 `forbidden`）。
2. 若传了 `baseControlVersion` 且 != 当前 `controlVersion` → `conflict`（附 `currentControlVersion`，据此重试）。
3. 找 `authorityClientId` 对应设备；**离线 → `authority_offline`**。
4. 服务端先乐观更新并持久化 context，再向订阅者/参与者广播 `state` / `playback.update`。`play/next/prev/playItem` 会立即推进 `state/currentIndex/trackId/positionMs`。
5. 向 authority 设备下发 `command`（如 `player.pause`），payload 含 `{playbackContextId, authorityClientId, controlVersion: 当前+1, ...}`（strict-v2 已去掉 `sessionId`）。authority 随后的 `playback.update` 作为真实设备反馈，可校正乐观状态。

### 3.4 乐观状态与 authority 校正
`player.play/next/prev/queue.playItem` 成功后，ack 和紧随其后的广播已经包含乐观推进后的 `state/currentIndex/trackId/positionMs`。服务端同时推进 `controlVersion/version`；切换曲目时还会推进 `queueRevision/epoch`。

对 Flutter 的含义：
- 可以立即用 ack / 广播更新 UI，但仍应接受 authority 后续 `playback.update` 对状态和进度的校正。
- 连续控制必须使用最近一次响应中的 `controlVersion` 作为下一次 `baseControlVersion`；两次快速 `next` 会基于已推进的索引依次前进，而不会重复命中同一首。

### 3.5 player.setVolume / player.requestState（始终 legacy 转发）
这两个 action **不走** v2/authority 路由，一律按 `targetClientId` 原样转发给目标设备：
```json5
{ "action": "player.setVolume", "requestId":"v1", "targetClientId": "pc-1",
  "payload": { "volume": 55 } }
// ack: { "payload": { "forwarded": true } }  ；目标设备收到 command/player.setVolume
```
即使是 strict-v2 客户端，这两个也**必须在消息顶层带 `targetClientId`**。

### 3.6 legacy 控制路径（了解即可，新端不用）
未声明 v2 且 payload 无 context 标记时，走 `targetClientId + sessionId` 的服务端中介控制：
- 目标同时支持 `effectiveAtPlayback`+`playbackPrepare` → 两阶段（`playback.prepare` → `playback.ready` → 提交），ack `{preparing:true, prepareId, ...}`。
- 仅 `effectiveAtPlayback` → single_future，命令带 `effectiveAtServerMs`（play/next/prev/playItem ≈ 服务器时间+700ms；`player.seek` 例外为 +250ms 且仅在时间线播放中才带）。
- 都不支持 → 原样转发，ack `{forwarded:true}`。

### 3.7 跟播者控制限制
若你正跟播某个源（见 §5），对该源发任何 CONTROL_ACTION 会被 `follow_control_forbidden` 拒绝——跟播者只能看，不能控。

---

## 4. Handoff（无痕切换 / 交接）

把一个 context 的播放权（`authorityClientId`）从当前 authority（源）**无痕转移**到目标播放设备。`playbackContextId` 不变、队列不变，只换 authority。采用**两阶段 prepare/ready/commit**。

**目标设备硬性要求**：`roles` 含 `player`，且 `capabilities` 同时有 `playbackPrepare` + `effectiveAtPlayback`，否则 `forbidden`。

### 4.1 完整时序（happy path）
```
控制端/源                服务端                     目标设备(target)
  │  handoff.start ─────▶ │                            │
  │  ◀── ack{preparing}   │ ── command:playback.prepare ──▶ │  (armed 8s 超时)
  │                       │                            │  本地预加载
  │                       │ ◀──── playback.ready{ready:true} │
  │                       │ commit: handoff->ready(armed 5s) │
  │                       │ ── command:player.play ───────▶ │  开始播放
  │                       │ ◀── playback.handoff.complete ── │
  │ ◀ command:playback.handoff.release{reason:handoff_completed}
  │                       │ ── state:playback.update(new authority) ─▶ 订阅者/参与者
```

### 4.2 playback.handoff.start（源或 controller 发起）
```json5
{
  "action": "playback.handoff.start",
  "requestId": "h1",                    // 也用作幂等键的一部分
  "payload": {
    "playbackContextId": "playback:alice:main",  // 必填非空；strict-v2 禁 sessionId
    "targetClientId": "pc-1",                      // 必填非空，且 != source
    "sourceClientId": "phone-1",                   // 可选，缺省=context.authorityClientId；必须==当前 authority
    "baseControlVersion": 8,                       // 可选，缺省=context.controlVersion；须匹配否则 conflict
    "handoffId": "handoff-xxx"                     // 可选，缺省服务端生成 "handoff-<12hex>"
  }
}
// ack:
{ "payload": {
    "preparing": true, "handoffId": "handoff-...", "prepareId": "prep-...",
    "playbackContextId": "...", "sourceClientId": "phone-1", "targetClientId": "pc-1",
    "originClientId": "phone-1", "controlVersion": 9, "status": "preparing"
} }  // 幂等重放时额外带 "duplicate": true
```
- **幂等键 = (userName, originClientId=发起者clientId, requestId)**：重发同一请求返回原 ack + `duplicate:true`。
- 同一 context 已有进行中的 handoff（状态为 `preparing`/`ready`/`committed`）→ `conflict`。
- 发起后服务端向 **target** 下发 `command` / **`playback.prepare`**：
```json5
// target 收到:
{ "type":"command", "action":"playback.prepare", "targetClientId":"pc-1",
  "payload": { "prepareId","handoffId","purpose":"handoff","playbackContextId",
               "deviceSessionId","sourceClientId","targetClientId","authorityClientId","originClientId",
               "queueSongIds":[...],"currentIndex","trackId","positionMs","state",
               "queueRevision","controlVersion","serverTimeMs","expiresAtServerMs" /* now+8000 */ } }
```

### 4.3 playback.ready（target 对 prepare 的应答）
target 预加载完成后发：
```json5
{ "action": "playback.ready", "requestId":"rdy1",
  "payload": { "prepareId": "prep-...", "ready": true, "controlVersion": 9 } }  // controlVersion 须==prepare 里的值
// 可选 clientId：若传，必须等于自己的 clientId，否则 forbidden
```
- `ready:true` 且就绪条件满足 → 服务端提交：handoff 进入 `ready`，armed 5s 完成超时，并向 target 下发 `command` / **`player.play`**（payload 含 `effectiveAtServerMs`、`completeExpiresAtServerMs`、`handoffId`、`trackId`、`positionMs`）。
- `ready:false`（作为必需目标）→ handoff `aborted`，服务端向 target 发 release（reason `aborted`）。
- prepare 8s 未就绪 → `timed_out`，release（reason `timed_out`）。

### 4.4 playback.handoff.complete（target 真正开播后发）
```json5
{ "action":"playback.handoff.complete", "requestId":"h2",
  "payload": { "handoffId":"handoff-...", "playbackContextId":"playback:alice:main", "controlVersion":9 } }
// controlVersion 须匹配 handoff 记录的 controlVersion
// ack: { "payload": { "completed":true, "handoffId","playbackContextId",
//                     "authorityClientId":"pc-1"(新authority), "playback": <RAW context> } }
```
> ⚠️ 注意：complete 的 ack 里 `playback` 是**未经 v2 序列化的原始 context**，会**含 `sessionId`/`sourceClientId` 等 legacy 字段**（其它所有 v2 出口都已剥除）。客户端**不要依赖这里的 `sessionId`**，仍以 `playbackContextId` 为主键；要读干净 context 请以随后广播的 `state`/`playback.update` 为准。（这是后端一处待统一的不一致，见团队 review-fixes 文档；对客户端无害，忽略多余字段即可。）

服务端在此**原子转移 authority** 到 target，然后：
1. 向**旧源**下发 `command` / **`playback.handoff.release`**：
   ```json5
   { "type":"command", "action":"playback.handoff.release", "targetClientId":"phone-1",
     "payload": { "playbackContextId","handoffId","authorityClientId":"pc-1", "reason":"handoff_completed" } }
   ```
   源设备收到后应**停止本地播放**（它已不是 authority）。
2. 向订阅者/参与者广播 `state` / `playback.update`（新 authority）。

- 幂等：handoff 已 `completed` 时重发 → `{completed:true, duplicate:true, ...}`。
- 若 5s 内未 complete → 超时，release（reason `timed_out`）。

### 4.5 playback.handoff.cancel（源或目标均可取消）
```json5
{ "action":"playback.handoff.cancel", "requestId":"h3", "payload": { "handoffId":"handoff-..." } }
// ack: { "payload": { "canceled":true, "handoffId", "status":"canceled",
//                     "authorityClientId": <仍为源>, "sourceKeptAuthority": true } }
```
- 中止底层 prepare；若之前是 preparing/ready/committed，向 **target** 发 release（reason `canceled`）。
- authority **不转移**，源保留播放权。
- 幂等：已 completed → `{ignored:true, status:"completed"}`；已终态 → `{ignored:true, status:<终态>}`。

### 4.6 playback.handoff.release 的 `reason` 取值（服务端 → 设备）
| reason | 收到方 | 语义 |
| --- | --- | --- |
| `handoff_completed` | 旧源 | 交接成功，源停止播放 |
| `canceled` | 目标 | 交接被取消，目标放弃 |
| `timed_out` | 目标 | prepare(8s)/complete(5s) 超时 |
| `aborted` | 目标 | prepare 被拒/被 supersede |

客户端**必须实现 `playback.handoff.release` 的处理**：按 reason 停止/放弃对应的播放或预备状态。

### 4.7 超时常量
- prepare 超时：**8000ms**（start 后 target 须在此前 `playback.ready`）。
- complete 超时：**5000ms**（进入 ready 后 target 须在此前 `handoff.complete`）。
- 两阶段提交前置量：350ms（`effectiveAtServerMs = 服务器时间 + 350`）。
- 服务端进程重启后会恢复活动 handoff：过期记录先终止，`preparing` 会重建 prepare，`ready` 会重发 `player.play` 并继续 complete 超时计时；同一 context 在恢复期间仍拒绝并发 handoff。

---

## 5. Follow（跟播）

跟播 = 本设备作为 follower，跟随另一设备/context 的播放时间线，只观察不控制。**v2 与 v1 都已实现**；strict-v2 客户端必须走 v2（`sourcePlaybackContextId`，禁 `sessionId`）。

### 5.1 follow.start
```json5
{
  "action": "follow.start",
  "requestId": "f1",
  "payload": {
    "sourcePlaybackContextId": "playback:alice:main",  // v2 必填非空（也可用 playbackContextId 兜底）
    "deviceSessionId": "device:pad-1"                   // 可选，缺省取本设备 deviceSessionId
    // strict-v2 禁止 sessionId
  }
}
// ack:
{ "payload": {
    "relationship": { "followerClientId":"pad-1", "followerSessionId":"device:pad-1",
                      "sourceClientId":"phone-1", "sourcePlaybackContextId":"playback:alice:main",
                      "sourceSessionId":null,   // v2 分支该字段恒为 null
                      "userName":"alice", "active":true, "createdAtMs":..., "updatedAtMs":... },
    "subscriptions": ["playback:alice:main"]   // 你的 sid 现已订阅的 context 列表
} }
```
行为：
- 服务端把你**自动订阅**到该 context，并**只向你**推一条一次性快照 `state` / `playback.context.status`（`{playbackContext, deviceStates}`）。
- 之后该 context 的所有状态广播你都会收到（因为已订阅）。
- **源设备不会被通知**它多了一个 follower。
- context 不存在 → `not_found`；跨用户 → `forbidden`；源==自己 → `bad_request`。

### 5.2 follow.stop
```json5
{ "action":"follow.stop", "requestId":"f2",
  "payload": { "sourcePlaybackContextId": "playback:alice:main" } }  // 全部可选，缺省用已存关系兜底
// ack: { "payload": { "relationship": <active:false 的关系或 null>, "subscriptions": [剩余订阅] } }
```
- 取消对应 context 订阅；不传具体 id 时清空该 sid 的全部 context 订阅。
- 无关系时不报错，`relationship` 为 null。

### 5.3 跟播者不能控制源
关系激活后，follower 对该源发任何 CONTROL_ACTION（play/pause/next/prev/seek/setVolume/requestState/queue.playItem）→ `follow_control_forbidden`。跟播是只读视图。

> v1 分支（`sourceClientId` + `sourceSessionId`）仍在，但新客户端不要用。

---

## 6. Broadcast（群播）

群播 = owner/controller 让一组 `player` 设备**同步播放**同一队列。基于 `broadcastId`（`broadcast-<12hex>`）。

BROADCAST_ACTIONS：`broadcast.start` `broadcast.status` `broadcast.queue.sync` `broadcast.playItem` `broadcast.play` `broadcast.pause` `broadcast.seek` `broadcast.stop`。所有都需先注册设备；改状态类需**控制权**。strict-v2 客户端：除 start 外所有 action 都必须带 `playbackContextId`，且禁 `sessionId`。

### 6.1 broadcast.start
```json5
{
  "action": "broadcast.start",
  "requestId": "b1",
  "payload": {
    "targetMode": "selectedClients",         // 必填: selectedClients | allOnlinePlayers | allOnlinePlayersExceptSelf
    "targetClientIds": ["pc-1","speaker-1"], // targetMode=selectedClients 时必填
    "queueSongIds": ["s1","s2"],             // 必填 list（可空）
    "currentIndex": 0, "positionMs": 0,
    "autoPlay": true,                         // 可选，默认 false
    "controlPolicy": "participants_and_controllers_can_control", // 可选，见下
    "playbackContextId": "broadcast:alice:x"  // strict-v2 必填
  }
}
// ack (legacy/single_future): { started:true, broadcastId, participants:[...], skippedClientIds:[...], broadcast:<obj>, protocolPath }
// ack (two_phase):            { preparing:true, prepareId, broadcastId, participants:[...], protocolPath:"two_phase" }
```
- 只有**同用户、在线、`role=player`** 的目标成为 participant；其余进 `skippedClientIds`；跨用户 → `forbidden`；无有效 participant → `bad_request`。
- 空队列强制 `autoPlay=false, state=stopped`。
- `playbackContextId` 不得复用已有 context：跨用户占用返回 `forbidden`，同用户重复占用返回 `conflict`。
- **两阶段 vs 单阶段**：全部 participant 都支持 `effectiveAtPlayback`+`playbackPrepare` 且需要开播 → 两阶段（先 `playback.prepare`，各 participant 回 `playback.ready`，提交后再下发 `broadcast.start`）；否则 single_future（命令带 `effectiveAtServerMs≈+700ms`）/legacy 立即下发。
- ⚠️ **群播两阶段 prepare 的就绪窗口是 1200ms**（`PREPARE_TIMEOUT_MS`），**不是 handoff 的 8000ms**。participant 收到 `playback.prepare` 后须在 1.2s 内回 `playback.ready`。
- participant 收到 `command` / `broadcast.start`（及后续 play/pause/seek/playItem/stop），payload 是 broadcast 核心态（含 `broadcastId, queueSongIds, currentIndex, trackId, positionMs, state, controlVersion, controlPolicy, effectiveAtServerMs?` 等）。

### 6.2 controlPolicy（谁能控制群播）
| 值 | 含义 |
| --- | --- |
| `owner_only` | 仅创建者 |
| `controllers_only` | 需 `role=controller` |
| `participants_can_control` | 需是 participant |
| `participants_and_controllers_can_control` | participant 或 controller（**默认**） |
owner 永远可控。无权 → `forbidden`。

### 6.3 控制类 action（需控制权）
> ⚠️ **strict-v2 客户端：下面每条(除 `broadcast.start`)都必须带 `playbackContextId`**，否则 `bad_request`。示例已带上。也可只用 `broadcastId` 定位（非 strict-v2 时），但 strict-v2 下 `playbackContextId` 必填。
```json5
// 跳到某首并播放（两阶段可能返回 preparing）
{ "action":"broadcast.playItem", "payload": { "broadcastId":"broadcast-...", "playbackContextId":"broadcast:alice:x", "queueIndex":1, "baseControlVersion":3 } }
// 继续播放当前项
{ "action":"broadcast.play",     "payload": { "broadcastId":"broadcast-...", "playbackContextId":"broadcast:alice:x", "baseControlVersion":3 } }
// 暂停（positionMs 可选，缺省服务端估算）
{ "action":"broadcast.pause",    "payload": { "broadcastId":"broadcast-...", "playbackContextId":"broadcast:alice:x", "positionMs":12000 } }
// 拖动（positionMs 必填，不改播放态）
{ "action":"broadcast.seek",     "payload": { "broadcastId":"broadcast-...", "playbackContextId":"broadcast:alice:x", "positionMs":30000 } }
// 改队列（不改播放意图；仅队列身份变化时 queueRevision +1）
{ "action":"broadcast.queue.sync","payload":{ "broadcastId":"broadcast-...", "playbackContextId":"broadcast:alice:x", "queueSongIds":[...], "currentIndex":0, "baseControlVersion":3 } }
// 结束群播
{ "action":"broadcast.stop",     "payload": { "broadcastId":"broadcast-...", "playbackContextId":"broadcast:alice:x" } }
// 查询（只读，任何同用户设备可查）
{ "action":"broadcast.status",   "payload": { "broadcastId":"broadcast-...", "playbackContextId":"broadcast:alice:x" } }
// -> ack { broadcast:<核心态>, participantStates:[...] }
```
版本冲突要点：`queue.sync` 与 `playItem` **必须**带 `baseControlVersion`（或 `baseVersion`）；`play/pause/seek/stop` 可选。冲突 → `conflict`（附 `currentVersion` + `currentControlVersion`）。broadcast 已停用再控制 → `bad_request`。

### 6.4 participant 上报播放状态
participant 用 **`playback.update`** 且 payload 带 `broadcastId`（strict-v2 还需 `playbackContextId` 且须与 broadcast 匹配）上报自身进度：
```json5
{ "action":"playback.update", "payload": {
    "broadcastId":"broadcast-...", "deviceSessionId":"device:pc-1", "playbackContextId":"broadcast:alice:x",
    "state":"playing", "trackId":"s1", "positionMs":8000 } }
// ack: { updated:true, participantFeedback:true }
```
校验：必须是该 broadcast 的 active participant，否则 `forbidden`。这些状态出现在 `broadcast.status.participantStates`。

### 6.5 重连恢复
广播核心状态、owner、控制策略、延迟参数和 participant feedback 会持久化。服务端进程重启后可按 PlaybackContext 恢复 active broadcast 及参与者映射；设备重连时会主动收到一条 `state` / `broadcast.status` 快照。

---

## 7. Flutter 客户端接入清单

### 7.1 连接层
- [ ] 正式 Flutter lab 从受信任的部署 manifest/CI 读取 strictV2 三项静态预期值，并在注册 ACK 后校验；同时拒绝空 connectionNonce、重连后确认其变化，并要求 connectionEpoch=1。
- [ ] Socket.IO 连接到 namespace `/emo`，路径 `/emo/ws`；所有业务消息用 `message` 事件收发。
- [ ] 统一的请求封装：每条请求生成 `requestId`，用 `Completer`/`Future` 按 `requestId` 匹配 `system.ack` / `system.error`，带超时。
- [ ] 统一错误处理：把 §0.3 的 9 个 code 映射成业务异常；`conflict` 时读取 `currentControlVersion` 等做重试。
- [ ] 断线重连后重新 `auth.login` → `device.register`（`clientId` 持久化复用），并重新 `playback.context.subscribe` / 重新拉 `broadcast.status`。

### 7.2 能力与角色
- [ ] 注册时 `capabilities.playbackContextV2=true`，进入 strict-v2；**全局禁止再发 `sessionId`**。
- [ ] 能被控播放的设备 `roles` 含 `player` 且带 `playbackPrepare`+`effectiveAtPlayback`（否则不能作为 handoff/两阶段群播目标）。
- [ ] 能远控/发起 handoff/关闭 context 的设备 `roles` 含 `controller`。

### 7.3 状态消费（订阅端）
- [ ] 处理三种状态推送并归一到一个 context 模型：`playback.context.status`（嵌套 `{playbackContext, deviceStates}`）、`playback.update`（扁平 context）、`queue.context.sync`（扁平 context）。
- [ ] 处理 `device.list`（设备上下线）。
- [ ] **控制回声延迟**：next/prev/playItem 后不要立即信任广播里的 `currentIndex/trackId`，等 authority 的 `playback.update` 回声（见 §3.4）。

### 7.4 指令执行（player 设备端）
- [ ] 处理 `command` 类：`player.play/pause/seek/next/prev` `queue.playItem` `player.setVolume` `player.requestState`。
- [ ] 处理 `playback.prepare`（预加载后回 `playback.ready`）。**就绪窗口按来源区分：handoff prepare = 8000ms，群播两阶段 prepare = 1200ms**（超时则该次 prepare 失败）。
- [ ] 处理 `player.play`（handoff/两阶段提交后，按 `effectiveAtServerMs` 定时开播），开播后发 `playback.handoff.complete`（5s 内）。
- [ ] 处理 `playback.handoff.release`：按 `reason`（`handoff_completed`/`canceled`/`timed_out`/`aborted`）停止或放弃。
- [ ] authority 设备周期性发 `playback.update` 上报真实进度。

### 7.5 时钟对齐
- [ ] 利用 payload 里的 `serverTimeMs` 与本地时间估算偏移，用于 `effectiveAtServerMs` / `expiresAtServerMs` 的定时执行。

### 7.6 典型端到端流程速查
| 场景 | 步骤 |
| --- | --- |
| 本机开播 | `device.register` → `playback.context.create` → 本机播放并周期 `playback.update` |
| 遥控另一台 | 目标先 create context 并成为 authority → 控制端 `player.*` {playbackContextId} |
| 手机→电脑交接 | `handoff.start` → 电脑 `playback.ready` → 电脑收 `player.play` 开播 → 电脑 `handoff.complete` → 手机收 `release(handoff_completed)` 停 |
| 跟播别人 | `follow.start` {sourcePlaybackContextId} → 收 `playback.context.status` 快照 → 持续收 `playback.update`（只读） |
| 多设备同放 | owner `broadcast.start` {targetMode, queueSongIds, autoPlay} → participant 收 `broadcast.start`/`playback.prepare` → participant 回 `playback.ready`（两阶段）并 `playback.update` 上报 |

---

## 8. 附：v2 与 legacy 的取舍（给后端/客户端对齐）

- **新客户端只实现 v2**：`playbackContextId` 为播放主键，`deviceSessionId` 为设备身份，`authorityClientId` 为播放权，`DevicePlaybackState` 为设备真实状态（含 volume/muted）。
- 服务端在过渡期仍保留 legacy（`sessionId` + `targetClientId`、`queue.session.sync`、`session.subscribe`），但 strict-v2 客户端不会命中，也**不应**依赖。
- M1/M2/M3 已修复：计数器可跨重启一致恢复，strict-v2 handoff 不再从 legacy 队列隐式创建 context，控制会乐观推进索引。客户端当前仍需兼容 §2.3 的状态 action/形状差异，以及 §4.4 的 handoff complete 原始 context ack。

> 本文所有字段、错误码、时序均按当前 `supysonic/emo/ws.py` 实现核对。若服务端改动，以代码为准并同步本文。
