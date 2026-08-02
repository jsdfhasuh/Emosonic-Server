# Goal: EmoSonic 网页播放器与控制台 strict-v2 升级

> 状态：Completed（2026-07-13）
>
> 制定日期：2026-07-13
>
> 验证记录：`docs/verification/emosonic_web_strict_v2_20260713.md`
>
> 复审测试：`docs/verification/emosonic_web_strict_v2_post_review_20260714.md`
>
> 目标协议：PlaybackContext strict-v2 `2.1.0`
>
> 唯一 Socket.IO wire contract：`specs/emosonic_strict_v2_socketio_server_contract.md`
>
> 冻结 contract SHA-256：`ca069c6ad52447ea4f7ace7d795460c5ec759e5708b2f45acfbe50903aa4b3a3`
>
> 实施对象：`/player` 网页播放器与 `/control` 网页控制台
>
> 实施边界：本 Goal 负责网页客户端适配、同源浏览器认证和网页联调验证，不授予 production
> rollout 或 conformance freeze 权限。

## 一、Goal 结论

将当前使用 legacy `sessionId`、legacy queue action 和旧 Broadcast/Follow payload 的网页播放器与
控制台迁移为 strict-v2 客户端。

升级后：

1. `/player` 以 strict player 身份注册，成为 PlaybackContext authority；
2. `/control` 以 strict controller 身份注册，通过 Context 和 cursor 控制播放器；
3. 两个页面共用同一个 strict-v2 Socket.IO 客户端实现；
4. Core 完成后再分别开放 Broadcast、Follow 和 Handoff 客户端能力；
5. 网页端不再发送任何层级的 `sessionId`、`sourceSessionId` 或 strict 禁止的旧 action；
6. strict 模式失败时显示明确错误，不自动降级到 legacy；
7. 浏览器认证不暴露用户真实密码，使用同源登录会话签发的一次性浏览器密码凭据。

本 Goal 不是只替换 `device.register` payload。注册完成后，该 Socket 上的所有业务消息都会进入
strict validator，因此 Context、queue、control、feedback、Follow、Handoff 和 Broadcast 必须按
同一协议整体迁移。

---

## 二、协议权威性与冲突处理

实现期间按以下优先级判断行为：

1. `specs/emosonic_strict_v2_socketio_server_contract.md`；
2. 本 Goal；
3. `docs/emosonic_strict_v2_server_change_note.md`；
4. 当前网页模板和测试；
5. legacy Flutter/Web 文档与历史 Goal。

特别说明：

- `docs/emosonic_web_player_goal.md` 描述的是 legacy 网页播放器，不定义 strict wire shape；
- `docs/flutter_emo_socketio_integration.md` 当前仍包含 legacy `sessionId` 示例，不能作为本 Goal 的
  strict 请求依据；
- `docs/goal/follow_play.md` 和 `docs/goal/broadcast.md` 是 superseded/legacy 方案；
- `player.setVolume`、`player.requestState`、`session.subscribe`、`session.unsubscribe`、
  `queue.local.get`、`queue.local.set`、`queue.session.sync`、`queue.ready.complete` 不得进入 strict
  action surface；
- `canSetVolume:true` 只是能力描述，strict-v2 `2.1.0` 没有 `player.setVolume` action；
- 服务端 profile 开启不代表网页端可以提前声明对应 capability；只有客户端完整实现并通过联调后
  才能把 capability 改为 `true`。

---

## 三、当前仓库基线

### 3.1 `/player` 当前状态

`supysonic/templates/player.html` 当前：

- 通过 `/emo` namespace 和 `/emo/ws` path 连接 Socket.IO；
- 发送 `auth.login`，但浏览器会话路径使用空 payload；
- `device.register` 使用 `sessionId`；
- capabilities 使用 `playback/localQueue/sessionQueue/broadcast/volume/seek` 旧字段；
- `playback.update` 包含 `sessionId`、`durationMs`、`queueType`、`queueClientId` 等 strict 禁止字段；
- 使用 `queue.local.*`、`queue.session.sync` 和 legacy Broadcast 消息；
- 已具备浏览器 `<audio>` 播放、用户手势检测、队列、进度、音量和远程命令处理基础。

### 3.2 `/control` 当前状态

`supysonic/templates/control.html` 当前：

- 以 `roles:["controller"]` 注册，但使用 legacy `sessionId` 且没有 strict capabilities；
- 按 `selectedSessionId + selectedClientId` 选择设备；
- 使用 `session.subscribe`、`queue.local.*`、`queue.session.sync`；
- player control 仍依赖 legacy target/session 路由；
- Follow 由 controller 页面发起，语义与 strict Follow 不同；
- Broadcast payload 含旧 cursor/target 字段组合；
- 已具备设备列表、播放器选择、Context 类 UI、Follow/Broadcast 面板和命令状态展示基础。

### 3.3 服务端基础

服务端已经提供：

- strict 请求与输出 schema；
- strict 注册 metadata 和 negotiated capabilities；
- PlaybackContext、cursor、authority 路由和持久化；
- Follow、Handoff、Broadcast strict handler；
- request dedupe、connection provenance、重连和错误语义；
- Socket.IO 测试客户端与完整 strict 服务端测试入口。

当前 `strict_v2_conformance.json` 和测试容器中的 profile 开启属于 `local-test-only` 联调配置，不能
作为生产 readiness 或正式 conformance 证据。

---

## 四、范围

### 4.1 本 Goal 包含

- 浏览器同源短期 Socket 登录凭据；
- strict-v2 共享 JavaScript 客户端；
- `/player` 和 `/control` strict 注册；
- strict metadata、provenance、request settlement 和错误处理；
- device identity、Context identity 和 reconnect 状态；
- PlaybackContext create/status/subscribe/unsubscribe/close；
- queue.context.sync、queue.playItem、player controls 和 playback.update；
- strict Broadcast；
- strict Follow；
- strict Handoff；
- 网页 UI 状态、能力展示和失败提示；
- 精确浏览器 payload 的服务端集成测试；
- legacy/strict 显式切换和可验证回退；网页 legacy 路径的最终删除另立后续 Goal。

### 4.2 本 Goal 不包含

- 修改 strict-v2 `2.1.0` wire contract；
- 为 `player.setVolume` 或旧 queue action 私自增加 strict shape；
- 修改 native Flutter 客户端；
- 以网页端联调结果替代服务端 conformance freeze；
- 自动将 local-test-only readiness 发布到生产；
- 新增多 worker strict realtime 支持；
- 绕过浏览器 autoplay、安全 Origin、Cookie 或 CSRF 约束。

---

## 五、总体客户端架构

### 5.1 共享 strict 客户端

新增：

```text
supysonic/static/js/emo_strict_v2_client.js
```

职责：

1. 建立 Socket.IO 连接；
2. 获取一次性浏览器密码；
3. `auth.login -> device.register -> device.list -> ready` 状态机；
4. 生成 requestId 并维护 pending request；
5. 校验 ACK/error 的 requestId 与 `payload.action`；
6. 处理 direct response 和 event-confirmed action；
7. 校验 `protocolVersion`、metadata shape、connectionNonce 和 connectionEpoch；
8. 保存 negotiated capabilities；
9. 维护每个 Context 的 canonical cursor；
10. reconnect 后重新认证、注册、订阅和请求 status；
11. 相同 requestId 重试时保证 payload fingerprint 不变；
12. 将协议事件以回调或浏览器事件提供给 `/player` 和 `/control`。

模板只保留 UI 和媒体执行逻辑，不再分别实现完整握手与 request settlement。

### 5.2 客户端状态机

```text
disconnected
  -> connected
  -> authenticating
  -> authenticated
  -> registering
  -> registered
  -> synchronizing
  -> ready
```

进入 `ready` 前禁止发送 Context mutation、player control、Follow、Handoff 或 Broadcast 请求。
这里的 ready 只表示 strict transport/bootstrap ready，不表示已经存在 active PlaybackContext。

发生下列情况必须关闭当前 strict 连接并在 UI 显示错误：

- protocol major 不是 `2.x`；
- registration ACK 缺字段或 negotiated capability shape 非闭合；
- connectionNonce/connectionEpoch 与注册证据不一致；
- `payload.action` 与 pending request 不一致；
- 同 requestId 收到冲突 settlement；
- strict 输出出现任意层级的 `sessionId` 或 `sourceSessionId`。

同一 `2.1.x` 服务端的 schemaHash/build commit 变化只更新本地观测记录，不自动降级。

---

## 六、浏览器认证设计

### 6.1 问题

strict `auth.login` 要求非空 `u` 和 `p`。网页已经通过 Flask Cookie 登录，但不能把用户真实密码
嵌入 HTML、localStorage 或 JavaScript。

### 6.2 方案

新增同源、已登录用户可访问的一次性浏览器密码接口，例如：

```text
POST /emo/browser-auth-password
```

返回：

```json
{
  "userName": "root",
  "oneTimePassword": "browser-otp:<opaque-value>",
  "expiresAtMs": 1783930000000
}
```

网页发送保持 strict wire shape：

```json
{
  "type": "auth",
  "action": "auth.login",
  "requestId": "auth-1",
  "payload": {
    "u": "root",
    "p": "browser-otp:<opaque-value>"
  }
}
```

这里的 `p` 仍是服务端认证层验证的一次性密码，不是客户端自定义的任意 token，也不是“只要存在
Flask session 就忽略 payload”的隐式例外。该扩展保持 `auth.login` 的 `u/p` wire shape，但实施前
必须在服务端 change note 中明确：浏览器 OTP 是 password credential 的一种，并由认证层显式验证。

服务端认证层同时支持普通账户密码和浏览器 OTP。OTP 必须：

- 使用 CSPRNG 或经过认证的签名方案；
- 绑定 authenticated user 和当前浏览器会话；
- 有短有效期；
- 一次性消费或有严格 replay 窗口；
- 不写日志、不进入 URL、不持久化到 localStorage；
- 接口使用同源 Cookie、POST 和 CSRF 防护；
- 响应设置 `Cache-Control: no-store`；
- `payload.u` 必须与签发 OTP 的 session user 一致；
- reconnect 时重新获取，不复用过期 OTP。

若协议审阅不接受 OTP 作为 password credential，本 Goal 必须暂停在 Goal 0，并先修改/重新冻结权威
contract；不得在实现阶段自行把任意 token 填入 `p`。

---

## 七、身份与 Context 发现

### 7.1 设备身份

网页需要区分“稳定 player identity”和“多标签 control identity”。

`/player` 使用 localStorage 保存稳定的：

```text
clientId
deviceSessionId
```

为了避免两个 player 标签页使用同一 clientId 后互相替换，player 必须先取得单标签 owner lock：

- 优先使用 Web Locks API；
- 使用 BroadcastChannel 发布 owner heartbeat；
- 不支持 Web Locks 时使用带过期时间的 localStorage lease；
- secondary tab 不建立 Socket，只显示“网页播放器已在另一个标签页运行”；
- owner tab unload/lease 超时后允许接管；
- 刷新同一 owner tab 时复用 identity 和 Context binding。

`/control` 不持有 Context authority，使用 sessionStorage 保存 per-tab clientId/deviceSessionId，允许多个
控制台标签页同时在线，避免相同 clientId 的 replacement。

建议格式：

```text
web-player-<uuid>
web-player-device:<uuid>
web-control-<uuid>
web-control-device:<uuid>
```

player 刷新和正常 reconnect 必须复用稳定身份；用户明确“重置网页设备”时才生成新身份。control
在同一标签页刷新时复用，在新标签页生成新身份。

### 7.2 Context identity

`playbackContextId` 是一次长期播放任务的 ID，不是 `sessionId` 的改名。Context close 后形成不可
复用 tombstone，因此不得永久使用一个固定 Context ID 并在 close 后再次 create。

`/player` 应：

1. 在第一次出现合法非空队列时生成 `playbackContextId`；
2. 保存到 localStorage；
3. reconnect 后先请求 status，存在则恢复；
4. Context 已 closed 或确实不存在时清理本地绑定；
5. 下次非空队列生成新的 UUID Context；
6. 空队列不发送 create/sync；
7. 用户清空 active queue 时，authority 发送 `playback.context.close`；
8. 只有收到 close ACK 或 canonical closed 后才清理本地 Context binding；
9. close 失败时保留 binding 和 canonical queue，并在 UI 显示错误；
10. 下次出现非空队列时生成全新的 UUID Context，不复用 tombstone ID。

### 7.3 `/control` 如何发现 Context

strict `device.list` 不允许包含 `playbackContextId`。为避免污染 strict wire contract，新增同源、
只读、按当前 authenticated user 隔离的网页 Context binding 接口，例如：

```text
GET /emo/web-context-bindings
```

返回最小字段：

```json
{
  "bindings": [
    {
      "clientId": "web-player-...",
      "deviceSessionId": "web-player-device:...",
      "playbackContextId": "ctx-..."
    }
  ]
}
```

该接口从同用户、未关闭的 PlaybackContext authority 记录生成 binding，并由 `/control` 与当前 strict
`device.list` 的在线 player 集合交叉验证。它只用于网页 UI 发现，不新增 Socket.IO action。不得
跨用户返回绑定，不得返回 DB 主键、路径或内部时间戳；响应必须设置 `Cache-Control: no-store`。

---

## 八、注册与能力声明

### 8.1 `/player` Core 阶段

```json
{
  "clientId": "web-player-<uuid>",
  "deviceSessionId": "web-player-device:<uuid>",
  "deviceName": "Web Player",
  "alias": "网页播放器",
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
    "supportsBroadcast": false
  }
}
```

### 8.2 `/control` Core 阶段

```json
{
  "clientId": "web-control-<uuid>",
  "deviceSessionId": "web-control-device:<uuid>",
  "deviceName": "Web Control Console",
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
    "supportsBroadcast": false
  }
}
```

### 8.3 能力开放顺序

| Capability | Core | 对应功能通过后 |
| --- | --- | --- |
| `playbackContextV2` | player/control `true` | 保持 `true` |
| `playbackPrepare` | player `false` | Handoff prepare/commit 完成后与 effective-at 同时 `true` |
| `effectiveAtPlayback` | player `false` | 定时播放验证完成后与 playbackPrepare 同时 `true` |
| `supportsFollow` | `false` | follower player 完成后 player `true` |
| `supportsBroadcast` | `false` | strict Broadcast 完成后 player/control `true` |

注册 ACK 中 negotiated 值与请求值不一致时，以 negotiated 值为唯一授权依据，UI 必须禁用未协商
功能。

---

## 九、Core action 迁移

### 9.1 Legacy 到 strict 映射

| Legacy 网页行为 | strict-v2 行为 |
| --- | --- |
| `sessionId` | `deviceSessionId + playbackContextId` |
| `session.subscribe/unsubscribe` | `playback.context.subscribe/unsubscribe` |
| `player.requestState` | `playback.context.status` |
| `queue.local.get/set` | 浏览器本地状态，不进入 strict Socket.IO |
| `queue.session.sync` | authority 使用 `queue.context.sync` |
| target/session player control | `playbackContextId + baseControlVersion` |
| legacy `playback.update` | strict feedback shape + `clientSeq` |
| controller 全量改写远端 queue | strict Core 不支持；仅 authority sync |
| `player.setVolume` | strict `2.1.0` 不支持，UI 禁用或保留为本机操作 |

### 9.2 Player Context 创建

只有非空且 track ID 不重复的队列可 create。网页必须在发送前验证：

- `queueSongIds` 非空；
- 每个 ID 非空且不重复；
- 最多 1000 项；
- `currentIndex` 在队列范围内；
- `positionMs >= 0`；
- state 为 playing/paused/stopped。

网页产品策略固定为：禁止向 strict queue 加入重复 song ID，并显示“strict-v2 队列不支持重复曲目”
错误；不得静默去重或改变曲目顺序。

create 使用 direct `playback.context.create` response，不等待额外 ACK。

### 9.3 Queue 同步

authority 保存 canonical `queueRevision` 和 `controlVersion`。队列内容变化发送
`queue.context.sync`；index、当前 track 或 position 变化时携带最新 `baseControlVersion`。

收到 `stale_version`：

1. 请求 `playback.context.status`；
2. 使用 canonical state 更新 UI；
3. 若用户意图仍有效，以新 requestId 和新 base cursor 重试；
4. 不得修改 payload 后复用旧 requestId。

### 9.4 Playback feedback

`playback.update` 只上报设备 feedback：

```json
{
  "playbackContextId": "ctx-1",
  "deviceSessionId": "web-player-device:1",
  "state": "playing",
  "positionMs": 12000,
  "clientSeq": 1,
  "trackId": "track-1",
  "volume": 80,
  "muted": false
}
```

不得包含 queue、currentIndex、durationMs、queueType、broadcastId 或 legacy session 字段。
每个新 physical connection 的 clientSeq 从 1 开始并严格递增。

### 9.5 Player control

`/control` 必须先取得 Context status，缓存 `baseControlVersion`，再发送 player action。

`/player` 收到 server-routed control 后：

1. 校验 provenance 和 playbackContextId；
2. 更新 `<audio>`；
3. 更新本地 queue/index；
4. 发送新的 strict `playback.update`；
5. 不自行修改服务端 cursor。

浏览器 autoplay 被阻止时必须展示明确 UI，不伪造 playing feedback。

---

## 十、Broadcast 升级

完成 Core 后再迁移 Broadcast。

### 10.1 控制台

- `broadcast.start` 使用 `playbackContextId`、queue、index、position、可选 participants/autoPlay；
- `broadcast.status` 使用 `playbackContextId + broadcastId`；
- play/pause/seek/playItem/stop 使用 strict 闭合字段；
- queue sync 只携带 strict 允许的可选 baseQueueRevision/baseControlVersion；
- 删除旧 `targetMode`、`targetClientIds`、`baseVersion` 等字段；
- canonical BroadcastSnapshot 是 UI 唯一状态来源。

### 10.2 播放器

- 处理 strict BroadcastSnapshot；
- 只有 negotiated `supportsBroadcast:true` 才能成为 participant；
- play/pause/seek/playItem/queue.sync 应用到本机 audio/queue；
- 持续以 `playback.update` 回传设备 feedback；
- autoplay 被阻止时保持真实状态并提示用户；
- stop 后清理 Broadcast UI，但不清理设备和基础 Context identity。

participant feedback 必须针对 Broadcast 所属的 playbackContextId 发送合法 `playback.update`，并使用
当前 physical connection 的递增 clientSeq；不能重新发送 legacy Broadcast feedback shape。

完成双浏览器测试后，player/control 才可把 `supportsBroadcast` 改为 true。

---

## 十一、Follow 升级

strict Follow 由实际 follower player 发起，不允许 controller-only `/control` 代表另一台 player
建立 Follow relationship。

因此当前 Follow UI 必须调整：

1. `/control` 可展示可跟随来源，但不能直接发送 follower 的 `follow.start`；
2. `/player` 提供“跟随其他播放源”入口；
3. follower player 通过同源 context bindings 选择在线 source，并获取 `sourcePlaybackContextId`；
4. follower 自己发送：

```json
{
  "sourcePlaybackContextId": "source-context-1",
  "deviceSessionId": "当前 follower deviceSessionId"
}
```

5. start ACK 后显式请求 source `playback.context.status`；
6. follower 使用 canonical source status/queue 更新本机 queue、index、state 和 projected position；
7. source playing 时根据 serverUpdatedAtMs 和已校准的 server clock 预测当前位置；漂移绝对值超过
   `WEB_FOLLOW_DRIFT_THRESHOLD_MS = 1500` 时 seek，小于阈值时不反复抖动校正；
8. follower 只发送自身 `playback.update` feedback，不发送 source queue mutation 或 player control；
9. 未获得浏览器用户播放手势时禁用 Follow start，不先建立一个无法执行的 relationship；
10. 本机播放失败后立即发送 follow.stop，并显示稳定错误；
11. source disconnect/close 后 follower 清理 Follow UI；
12. follower reconnect 后不假定旧瞬态 relationship 仍存在，重新选择并 start；
13. 完成状态复制、漂移、feedback 和失败测试后才声明 `supportsFollow:true`。

如果产品要求控制台远程命令另一台播放器进入 Follow，需要另立协议目标；不得在本 Goal 中通过
伪造 player role 或 targetClientId 绕过 strict 权限模型。

---

## 十二、Handoff 升级

### 12.1 控制台职责

- 使用 selected source Context 的最新 `baseControlVersion`；
- 以 `targetClientId` 选择目标 player；
- 发送 `playback.handoff.start`；
- 展示 preparing/ready/committing/completed/failed/cancelled/timedOut 状态；
- cancel 使用 strict `playbackContextId + handoffId`。

### 12.2 目标播放器职责

目标 `/player` 在完整实现前同时保持 `playbackPrepare:false` 和
`effectiveAtPlayback:false`。实现后：

1. 收到 `playback.prepare`；
2. 校验 prepare 指向的 source playbackContextId、prepareId、handoffId、queue、controlVersion，并
   确认 payload.deviceSessionId 等于当前 target player；
3. 预加载音频与队列但不提前夺取 authority；
4. 已获得用户手势、页面处于可调度状态且可以按服务端时间执行时发送
   `playback.ready ready:true`；
5. 无用户手势或加载失败时发送 `ready:false + errorCode`；
6. 收到带 `handoffId`、`sourceClientId` 和 `effectiveAtServerMs` 的 strict `player.play` commit；
7. 使用已校准 server clock 与单调时钟，在 effectiveAtServerMs 执行播放；
8. 实际开始后发送 `playback.handoff.complete`，可选 positionMs 使用真实媒体位置；
9. source 收到 release 后暂停；
10. completed status 后按新 authority 状态更新 Context；
11. duplicate event 使用相同 requestId/payload，不重复执行副作用。

浏览器无用户手势时建议使用 `autoplay_blocked` errorCode。

浏览器后台节流、document hidden、用户手势不足或无法满足调度条件时，target 必须在 prepare 阶段
返回 ready:false，不得进入 committing。

Handoff capability 是不可拆分的开放门槛：只有 prepare、strict player.play commit、定时执行、
complete、release、timeout 和重复请求测试全部通过后，才同时声明 `playbackPrepare:true` 与
`effectiveAtPlayback:true`。任一条件未完成时两者都保持 false，网页 Handoff 不开放。

前台浏览器 timing 验收固定为：至少 30 次 commit，`effectiveAtServerMs` 到真实 audio start 的绝对
误差每次不超过 200ms；document hidden 或调度条件不满足时不得计入成功样本，而应在 prepare 阶段
返回 ready:false。未满足该门槛不得声明 effectiveAtPlayback。

---

## 十三、显式兼容与 rollout

新增配置，例如：

```ini
emo_web_realtime_protocol = legacy
```

可选值只允许：

```text
legacy
strict_v2
```

规则：

- 配置在服务端渲染页面时确定；
- strict 页面不得在 register not_supported 后自动改发 legacy payload；
- strict 失败显示协议错误和 server metadata；
- `/player` 与 `/control` 应在同一次部署中切换，避免两个页面使用不同的 Context 模型；
- Goal 1 和 Goal 2 完成时该配置仍必须保持 legacy；
- 只有 Goal 3 的 Player/Control Core 联合测试通过后，test 环境才允许第一次切到 strict_v2；
- test 环境先切 strict_v2，完成验收后再讨论默认值；
- 本 Goal 完成时仍保留显式 legacy 模式；删除网页 legacy 路径必须另立后续 Goal，并在 production
  rollout 和回退窗口结束后执行。

本配置只选择网页客户端实现，不覆盖服务端 conformance readiness，也不能使未 ready profile 变为
ready。

---

## 十四、实施阶段

### Goal 0：冻结网页请求 fixture

- 记录现有 `/player` 和 `/control` legacy action inventory；
- 为 strict auth/register/device.list/Core/Broadcast/Follow/Handoff 建立 JSON fixture；
- fixture 直接通过 `validate_strict_request()`；
- 标记每个 action 的 ACK/direct/event-confirmed settlement；
- 明确所有禁止字段和禁止 action。

完成门槛：fixture 与 `ACTION_SCHEMAS` 一致，旧 `sessionId` fixture 明确失败。

### Goal 1：共享 transport、浏览器一次性密码和注册

- 实现 `emo_strict_v2_client.js`；
- 实现 browser-auth-password；
- `/player` 和 `/control` 在测试 fixture/隐藏测试入口中完成 strict 注册；
- metadata/provenance/request correlation 完成；
- ready 前 action gate 完成；
- UI 展示 protocolVersion、build、negotiated capabilities 和连接状态。

完成门槛：两个网页客户端的 exact strict fixture 注册成功且没有 legacy session 字段；真实页面仍由
`emo_web_realtime_protocol=legacy` 渲染，不得提前把现有业务流切成 strict。

### Goal 2：Player Core

- Context ID 创建/恢复/关闭；
- Context create/status；
- queue.context.sync；
- strict playback.update/clientSeq；
- strict player control 执行；
- reconnect 恢复；
- 单 player 标签 owner lock；
- 空队列 close/new Context；
- 重复曲目明确拒绝；
- autoplay blocked 处理。

完成门槛：一个网页 player 可以创建并恢复 Context，通过 strict 测试 controller 执行 control 并回传
canonical feedback；真实 `/control` 页面仍保持 legacy，直到 Goal 3 完成。

### Goal 3：Control Core

- strict device list；
- web-context-bindings；
- Context subscribe/status；
- cursor cache；
- play/pause/seek/next/prev/queue.playItem；
- stale_version、authority_offline、capability_required UI；
- 禁用 strict 不支持的远程 setVolume 和全量远端 queue rewrite。

完成门槛：控制台刷新、播放器重连和 cursor 冲突均可恢复，不产生 legacy action；Player/Control
联合 fixture 和浏览器测试通过后，test 环境才允许首次切换 strict_v2。

### Goal 4：Broadcast

- strict Broadcast payload；
- controller owner 和 player participant；
- canonical snapshot UI；
- participant feedback；
- disconnect/reconnect/stop；
- 双网页播放器联调。

完成门槛：通过后才打开 supportsBroadcast。

### Goal 5：Follow

- Follow UI 移至/增加到 player；
- source Context 选择；
- follower 自发 start/stop；
- canonical source 状态复制与 1500ms 漂移修正；
- follower feedback 与播放失败 stop；
- source close/disconnect 清理；
- 禁止 follower 控制 source；
- 双 player 联调。

完成门槛：通过后才打开 supportsFollow。

### Goal 6：Handoff

- control start/cancel/status；
- target prepare/ready/complete；
- strict player.play commit 与 effectiveAtServerMs 调度；
- source release；
- autoplay/load failure；
- reconnect/timeout/duplicate；
- controller + 双 player 联调。

完成门槛：完整 timing/commit 验证通过后，playbackPrepare 与 effectiveAtPlayback 同时打开；否则两者
保持 false，Handoff 不开放。

### Goal 7：test 切换与保留显式回退

- test 环境 strict_v2 默认；
- 完成完整回归和人工浏览器验收；
- 更新 web player/control 文档；
- 保留 `emo_web_realtime_protocol=legacy` 的显式回退路径；
- 保留服务端 legacy 客户端兼容，不扩大为服务端删除 legacy 的任务；
- 记录固定 build 和浏览器版本证据。

完成门槛：strict_v2 在 test 环境稳定，切回 legacy 仍可工作；网页 legacy 删除另立后续 Goal。

---

## 十五、测试计划

### 15.1 新增建议

```text
tests/base/test_emo_web_strict_v2.py
tests/frontend/test_web_strict_v2.py
tests/js/emo_strict_v2_client.test.js
```

不引入 npm 第三方依赖。Python 侧使用现有 unittest、Flask 测试客户端和 Socket.IO 测试客户端；
共享 JavaScript 客户端使用 Node 内置 `node:test` / `node --test` 验证真实 JS 状态机。开始实现前先在
现有 CI runner 验证 Node 可用；若不可用，必须先解决可执行 JS 测试环境，不能退化为只检查模板
字符串。

### 15.2 自动化覆盖

必须覆盖：

1. browser OTP 只能由已登录同源用户获取；
2. OTP 过期、跨用户、重放和日志脱敏；
3. exact web auth/register payload；
4. 完整 negotiated capabilities 和 metadata；
5. strict ready 前禁止业务 action；
6. device.list direct response；
7. Context create/status/subscribe/close；
8. queue sync 与全部 cursor 冲突；
9. control route 到唯一 authority；
10. playback.update clientSeq 重连重置；
11. 浏览器 payload 任意层级无 sessionId/sourceSessionId；
12. requestId retry 和 fingerprint conflict；
13. player/control disconnect、reconnect 和 replacement；
14. Broadcast 双 player；
15. Follow 双 player；
16. Handoff controller + 双 player；
17. autoplay_blocked；
18. legacy 模式仍渲染旧客户端，strict 模式不包含旧 action；
19. strict 失败不自动 fallback；
20. 未 negotiated capability 的 UI 和请求都被禁止；
21. player 多标签 owner lock 与 lease 接管；
22. shared JS request correlation、direct/ACK/event-confirmed settlement；
23. shared JS reconnect、provenance、metadata、request fingerprint 和 no-fallback；
24. 清空队列 close Context，下一队列使用新 ID；
25. 重复曲目被明确拒绝；
26. Handoff 两个 capability 始终同时 false 或同时 true。

### 15.3 相关回归命令

```bash
python -m unittest tests.frontend.test_player
python -m unittest tests.frontend.test_device_alias_display
python -m unittest tests.frontend.test_web_strict_v2
python -m unittest tests.base.test_emo_web_strict_v2
python -m unittest tests.base.test_emo_strict_v2_core
python -m unittest tests.base.test_emo_strict_v2_follow
python -m unittest tests.base.test_emo_strict_v2_handoff
python -m unittest tests.base.test_emo_strict_v2_broadcast
python -m unittest tests.emo_legacy_suite
node --test tests/js/emo_strict_v2_client.test.js
python -m unittest
```

### 15.4 人工浏览器矩阵

至少验证：

- Chrome/Chromium；
- Firefox；
- 一个桌面浏览器和一个移动浏览器；
- 同一用户、两个独立 player identity（不同浏览器/浏览器 profile）+ 一个 control；
- 页面刷新、网络断开、服务端重启；
- 无用户手势、有用户手势；
- 本地播放、远程控制、Broadcast、Follow、Handoff；
- stale cursor、authority offline、capability required 和 protocol error UI。

---

## 十六、日志与可观测性

网页端应输出结构化但不含凭据的调试摘要：

```text
connection state
requestId
action
settlement type
error code
playbackContextId
clientId/deviceSessionId
cursor summary
protocolVersion/schemaHash/serverBuildCommit
```

不得记录：

- auth payload；
- browser OTP；
- Cookie；
- 完整原始 envelope；
- 用户密码；
- 媒体库本地路径。

UI 至少区分：transport connected、authenticated、registered、synchronizing、ready、protocol
error、offline。

---

## 十七、风险与强制决策

1. **浏览器认证**：不能继续依赖空 auth payload，也不能把真实密码传给模板；必须先完成并审阅一次性
   浏览器密码语义，不能把任意 token 当作 p。
2. **Context 发现**：strict device.list 不含 playbackContextId；必须采用同源 binding 接口或经审阅的
   其他非 wire 方案，不能污染 device.list。
3. **空队列**：strict create/sync 要求非空队列；清空队列固定执行 Context close，成功后新队列使用
   新 Context ID。
4. **重复曲目**：strict queue 不允许重复 ID；网页固定拒绝重复曲目并显示错误，不能静默去重。
5. **远程音量**：strict 2.1.0 无 player.setVolume；不能伪造 action。
6. **Follow 发起方**：controller 不能冒充 follower player。
7. **autoplay**：浏览器限制是运行时失败条件，不能因 capability=true 而假装一定可播。
8. **cursor**：所有 mutation 必须来自 canonical cursor；不能继续依赖本地乐观 session 状态。
9. **混合模式**：player/control 必须一起切换，不长期维护相互不一致的 Context 模型。
10. **readiness**：local-test-only manifest 只用于联调，不能随本 Goal 自动升级为正式 evidence。
11. **多标签页**：稳定 player identity 必须有单 owner lock；不能依赖服务端 replacement 制造断线循环。
12. **Handoff**：playbackPrepare/effectiveAtPlayback 必须同时开放；不能发布半套 Handoff capability。

---

## 十八、Definition of Done

本 Goal 完成必须同时满足：

1. `/player` 和 `/control` 使用同一 strict-v2 客户端模块；
2. 浏览器认证不使用真实账户密码或空 strict auth payload，并显式验证一次性浏览器密码；
3. 两个页面 strict device.register 成功并验证 metadata；
4. 所有网页 strict envelope 无 sessionId/sourceSessionId；
5. 网页 strict 模式不发送禁止的 legacy action；
6. player 可创建、恢复、同步和关闭 Context；
7. control 可发现 Context、订阅、请求 status 并用 cursor 控制；
8. player 执行控制后发送合法 playback.update；
9. reconnect 后 identity、subscription、status 和 clientSeq 正确恢复；
10. stale_version、authority_offline、capability_required 和 protocol error 有明确 UI；
11. Broadcast 双 player 完整通过后才协商 supportsBroadcast；
12. Follow 由 follower player 发起且通过双 player 验证后才协商 supportsFollow；
13. Handoff 完整 prepare/commit timing/ready/complete/release 后才同时协商 playbackPrepare 和
    effectiveAtPlayback；
14. Handoff 未完成时两个 capability 都保持 false；
15. strict 模式失败不自动 fallback；
16. legacy 服务端测试保持绿色；
17. Python targeted tests、Node 原生 JS tests、完整 unittest 和人工浏览器矩阵完成；
18. 固定 server commit、网页资源 build、浏览器版本、命令和结果保存到 verification 记录；
19. 文档不再把 legacy Web/Flutter 示例描述为 strict-v2；
20. production rollout、正式 conformance readiness 和 manifest evidence 由独立审阅流程决定；
21. player 多标签页不会因相同 clientId 互相 replacement；
22. 清空队列与重复曲目策略有自动化和 UI 验收；
23. 本 Goal 结束时 strict/legacy 显式切换均可工作，legacy 删除属于后续 Goal。

---

## 十九、建议提交拆分

1. `web-strict-v2: freeze browser fixtures and forbidden legacy inventory`
2. `web-strict-v2: add browser one-time password and shared socket client`
3. `web-strict-v2: migrate player registration and Core context`
4. `web-strict-v2: migrate control registration and Core controls`
5. `web-strict-v2: add strict web Broadcast`
6. `web-strict-v2: add player-owned strict Follow`
7. `web-strict-v2: add web Handoff prepare and completion`
8. `web-strict-v2: switch test deployment and retain explicit legacy rollback`

每个提交只开放已经通过对应测试的 capability，不将 Core、Broadcast、Follow、Handoff readiness
混在同一个不可审阅的大变更中。
