# Goal：网页播放器接入 Emosonic 现有 Socket.IO 多端播放体系

## 背景

Emosonic Server 目前已经具备音乐流媒体能力，音频播放应复用现有流媒体接口；同时项目中已经存在一套基于 Socket.IO 的实时通信体系，用于设备注册、播放状态同步、队列同步、单播控制和群播控制。

现有 `/control` 控制台已经实现控制端思路：控制台以 `controller` 角色接入 Socket，并从在线设备中筛选 `roles` 包含 `player` 的客户端作为可控播放器。

因此，网页播放器的目标不是重新做一套控制系统，而是作为一个标准 `player` 客户端接入现有 Socket.IO 协议体系。

本文档以以下现有实现为准：

- `docs/emo_websocket_protocol_v1.md`
- `docs/flutter_media_stream_strategy_headers.md`
- `supysonic/emo/ws.py`
- `supysonic/templates/control.html`

---

## 总目标

实现一个网页播放器页面，使其不仅可以在浏览器中播放音乐，还能作为 Emosonic 多端播放体系中的标准播放端参与实时通信。

播放器需要支持：

1. 本地网页播放
2. Socket 设备注册
3. 播放状态上报
4. 控制台单播控制
5. 本地队列与会话队列同步
6. 群播播放参与
7. 刷新或重连后的状态恢复

最终效果是：

用户打开 `/player` 后，该网页播放器会出现在 `/control` 控制台的在线播放器列表中，并可以被控制台单播控制、同步队列、加入群播。

---

## 一、首版边界

第一版目标是做“可播放的标准 player 端”，不是再做一个控制台。

首版必须做：

- `/player` 页面
- 曲库搜索或歌曲选择入口
- audio 元素播放
- 播放队列
- Socket.IO 注册为 `player`
- 处理 `/control` 下发的命令
- 上报播放状态、队列状态
- 参与 broadcast 命令执行

首版不做：

- 完整替代 `/control`
- 跨用户控制策略重写
- 离线消息投递
- 多 worker Socket 状态同步
- 复杂房间管理 UI

---

## 二、播放器基础能力

网页播放器需要具备完整的基础播放体验：

- 曲库搜索
- 歌曲列表
- 当前播放封面
- 歌名、艺人、专辑信息展示
- 播放 / 暂停
- 上一首 / 下一首
- 进度条
- 音量控制
- 播放队列
- 播放结束后自动下一首

音频流不重新实现，优先复用现有 Subsonic 流媒体入口：

```http
GET /rest/stream.view?id=<trackId>
```

`/rest/stream` 是等价路径，但页面实现建议优先使用 `.view` 形式，以便与现有文档和测试保持一致。

如果播放器需要处理高规格 FLAC、内嵌封面、缓存策略或兼容 variant，应参考 `docs/flutter_media_stream_strategy_headers.md`：

- 可在真正播放前用 `HEAD /rest/stream.view?id=<trackId>` 探测响应头。
- 如果 `HEAD` 不稳定，可退化为 `GET Range: bytes=0-0`。
- 不应为了网页播放器重新实现后端音频转码或推流。

### 曲库搜索与元数据

当前仓库已有 Subsonic 搜索接口：

```http
GET /rest/search3.view?query=<keyword>&songCount=50&f=json
```

实现 `/player` 时需要明确前端鉴权方案，避免在页面 JS 中硬编码或暴露明文密码。可选方案：

1. 复用现有 REST 鉴权参数或 token 策略。
2. 新增 `@login_only` 的轻量 Web JSON 接口，例如 `/player/search`。
3. 将现有 `/control/track-meta` 泛化为播放器也可复用的 track metadata 接口。

无论选择哪种方案，播放器内部队列的 canonical ID 都必须是服务端 track id，即 Socket 协议里的 `queueSongIds` 项。

---

## 三、Socket.IO 接入

网页播放器加载后，连接现有 Socket.IO 服务。

必须同时指定：

- namespace: `/emo`
- path: `/emo/ws`
- business event: `message`

参考现有 `/control` 的连接方式：

```javascript
const socket = io(window.location.origin + "/emo", {
  path: "/emo/ws",
  transports: ["polling"],
});
```

所有业务消息都必须通过 Socket.IO 的 `message` 事件发送，而不是把 action 当成 Socket.IO event 名直接发送。

正确形式：

```javascript
socket.emit("message", {
  type: "auth",
  action: "auth.login",
  requestId: "web-player-auth-1",
  payload: {},
  timestamp: Date.now() / 1000,
});
```

错误形式：

```javascript
socket.emit("auth.login", {});
```

连接流程：

1. 建立 Socket.IO 连接到 namespace `/emo`，path `/emo/ws`。
2. 发送 `auth.login`。
3. 等待服务端返回 `system.ack`。
4. 发送 `device.register`。
5. 注册成功后启动 `system.ping` 心跳。
6. 开始处理命令、状态和队列消息。

服务端当前允许未认证前执行：

```text
auth.login
system.ping
```

已注册客户端应每 30 秒发送一次 `system.ping`。服务端默认会根据最近业务消息或 ping 判断设备是否在线。

---

## 四、注册为标准 player 设备

网页播放器认证成功后，需要发送 `device.register`，注册为播放器设备。

建议 payload：

```json
{
  "clientId": "web-player-<stable-browser-id>",
  "deviceName": "Web Player",
  "alias": "网页播放器",
  "roles": ["player"],
  "sessionId": "web-session-<stable-session-id>",
  "capabilities": {
    "playback": true,
    "localQueue": true,
    "sessionQueue": true,
    "broadcast": true,
    "volume": true,
    "seek": true
  }
}
```

这里最关键的是：

```json
"roles": ["player"]
```

因为现有控制台只把 `roles` 包含 `player` 的设备当作可控制播放器。

### 稳定身份要求

`clientId` 和 `sessionId` 不能每次刷新都完全随机生成，否则刷新后服务端恢复队列和播放状态时无法稳定关联同一个网页播放器。

建议：

- `clientId`：首次打开 `/player` 时生成一次，保存在 `localStorage`。
- `sessionId`：默认也保存在 `localStorage`；如果后续有房间/设备选择 UI，再允许用户切换。
- 如果用户主动“重置设备身份”，再清理本地保存的 `clientId` 和 `sessionId`。

示例：

```javascript
function getStableId(storageKey, prefix) {
  const existing = window.localStorage.getItem(storageKey);
  if (existing) {
    return existing;
  }
  const created = `${prefix}-${crypto.randomUUID()}`;
  window.localStorage.setItem(storageKey, created);
  return created;
}
```

---

## 五、播放状态上报

网页播放器需要监听 audio 元素状态变化，并向服务端发送：

```text
playback.update
```

触发时机包括：

- 开始播放
- 暂停
- 切歌
- 拖动进度
- 音量变化
- 播放结束
- 执行远程命令后
- 播放中定时上报当前播放进度

上报必须使用 Socket envelope：

```json
{
  "type": "event",
  "action": "playback.update",
  "requestId": "playback-1",
  "payload": {
    "sessionId": "web-session-xxxx",
    "trackId": "track-uuid",
    "state": "playing",
    "positionMs": 82000,
    "volume": 70,
    "durationMs": 245000,
    "queueType": "local",
    "queueClientId": "web-player-xxxx"
  },
  "timestamp": 1710000000
}
```

Required payload:

- `sessionId`
- `state`
- `positionMs`

Recommended payload:

- `trackId`
- `volume`
- `durationMs`
- `queueType`: `local` 或 `session`
- `queueClientId`: 当前播放 local queue 时为本播放器 `clientId`

服务端会在保存和广播时补充/覆盖 `sourceClientId`，客户端不需要自己伪造其他设备的 `sourceClientId`。

如果处于群播模式，还需要带上：

```json
{
  "broadcastId": "broadcast-xxxx"
}
```

群播状态上报只有在当前播放器是该 broadcast 的 active participant 时才会被服务端接受。

播放进度上报需要节流。建议：

- 播放、暂停、切歌、seek、音量变化时立即上报。
- 正在播放时每 5 到 10 秒上报一次进度。
- 不要直接把 audio 的每次 `timeupdate` 都发到 Socket。

---

## 六、支持控制台单播控制

网页播放器需要处理服务端通过 `message` event 下发的 `type: "command"` 消息，并执行现有控制动作。

服务端当前已定义以下单播控制动作：

```text
player.play
player.pause
player.next
player.prev
player.seek
player.setVolume
player.requestState
queue.playItem
```

播放器需要实现对应行为：

| Action | 网页播放器行为 |
|---|---|
| `player.play` | 播放当前曲目；如果没有当前曲目，可播放队列当前项 |
| `player.pause` | 暂停当前曲目 |
| `player.next` | 播放下一首 |
| `player.prev` | 播放上一首 |
| `player.seek` | 跳转到 `payload.positionMs` |
| `player.setVolume` | 设置 `payload.volume`，范围 0 到 100 |
| `player.requestState` | 按请求立即回传当前播放、队列、ready 状态 |
| `queue.playItem` | 根据 `payload.queueIndex` 播放指定队列项 |

服务端单播控制依赖 `targetClientId` 路由，并保持跨用户控制限制。播放器端只需要处理发给自己的命令。

### `queue.playItem` 规则

`queue.playItem` 的 payload 必须包含：

```json
{
  "sessionId": "web-session-xxxx",
  "queueIndex": 2
}
```

如果 payload 不包含 `clientId`，播放器应从 session queue 里取 `queueIndex` 对应歌曲。

如果 payload 包含：

```json
{
  "clientId": "web-player-xxxx"
}
```

播放器应从该 `clientId` 对应的 local queue 里取 `queueIndex` 对应歌曲。对网页播放器自身来说，只有 `clientId === selfClientId` 时才应当把它当成本地队列播放命令。

执行 `queue.playItem` 后，播放器必须至少回传：

1. `playback.update`
2. 当前队列类型对应的 `queue.session.sync` 或 `queue.local.set`

这样 `/control` 才能看到当前 index、track 和 position。

### `player.requestState` 规则

收到 `player.requestState` 后，播放器需要读取 payload 中的 flags：

```json
{
  "includePlayback": true,
  "includeSessionQueue": true,
  "includeLocalQueue": true,
  "includeReadyState": false
}
```

然后按需发布：

- `playback.update`
- `queue.session.sync`
- `queue.local.set`
- `queue.ready.complete`

缺省 flags 可以按播放器默认策略处理。第一版建议缺省时至少回传 `playback.update` 和当前 active queue。

---

## 七、支持队列同步

网页播放器需要同时支持两类队列。

协议使用 song-id based queue，不使用旧的 object-array queue 格式。

标准队列 payload：

```json
{
  "sessionId": "web-session-xxxx",
  "queueSongIds": ["song-id-1", "song-id-2"],
  "currentIndex": 0,
  "positionMs": 0
}
```

规则：

- `queueSongIds` 必须是字符串数组；非空时每项必须是非空字符串，空队列时为 `[]`。
- `currentIndex` 必须是整数。
- `positionMs` 必须是整数。
- 队列非空时，`currentIndex` 必须在数组范围内。
- 队列为空时，必须使用 `currentIndex = 0` 且 `positionMs = 0`。

### 1. 本地队列 local queue

本地队列属于某个播放器设备，服务端按 `sessionId + clientId` 保存。

对应事件：

```text
queue.local.get
queue.local.set
queue.ready.complete
```

播放器需要做到：

- 本地播放列表变化时，上报 `queue.local.set`。
- 上报 `queue.local.set` 时带上自己的 `clientId`，或省略 `clientId` 让服务端使用当前注册客户端。
- 收到 `queue.local.set` 时，先检查 `payload.sourceClientId`。
- 只有 `payload.sourceClientId === selfClientId` 时，才把它应用为自己的本地队列。
- 如果 `payload.sourceClientId` 是其他设备，应缓存为“其他设备 local queue”或忽略，不能覆盖自己的本地队列。
- 播放某个本地队列项后，同步 `currentIndex` 和 `positionMs`。

示例：

```json
{
  "type": "state",
  "action": "queue.local.set",
  "requestId": "local-set-1",
  "payload": {
    "sessionId": "web-session-xxxx",
    "clientId": "web-player-xxxx",
    "queueSongIds": ["song-id-1", "song-id-2"],
    "currentIndex": 1,
    "positionMs": 0
  }
}
```

`queue.ready.complete` 不是队列快照。它表示某个播放器已经完成指定队列的加载或准备工作。播放器在收到远程队列并完成本地加载后，可以按需要发送：

```json
{
  "type": "state",
  "action": "queue.ready.complete",
  "payload": {
    "sessionId": "web-session-xxxx",
    "queueType": "local",
    "clientId": "web-player-xxxx",
    "queueSongIds": ["song-id-1", "song-id-2"]
  }
}
```

### 2. 会话队列 session queue

会话队列属于当前 `sessionId`，可被多个设备共享。

对应事件：

```text
queue.session.sync
```

播放器需要做到：

- 收到 `queue.session.sync` 且 `payload.sessionId === selfSessionId` 时，更新当前共享队列。
- 控制台同步队列后，播放器可以按 `currentIndex` 加载对应歌曲。
- 播放器切歌或 seek 后，如当前播放源是 session queue，应回传完整 `queue.session.sync`。
- 不要只发送新的 `positionMs`，`queue.session.sync` 是完整替换快照。

示例：

```json
{
  "type": "state",
  "action": "queue.session.sync",
  "requestId": "session-queue-1",
  "payload": {
    "sessionId": "web-session-xxxx",
    "queueSongIds": ["song-id-1", "song-id-2"],
    "currentIndex": 0,
    "positionMs": 0
  }
}
```

---

## 八、支持群播播放

网页播放器需要作为群播参与端，处理现有 broadcast 协议。

服务端当前已定义群播动作：

```text
broadcast.start
broadcast.stop
broadcast.queue.sync
broadcast.playItem
broadcast.play
broadcast.pause
broadcast.seek
broadcast.status
```

播放器需要实现：

| Action | 网页播放器行为 |
|---|---|
| `broadcast.start` | 进入群播模式，加载群播队列 |
| `broadcast.queue.sync` | 更新群播队列 |
| `broadcast.playItem` | 播放群播队列指定索引 |
| `broadcast.play` | 播放当前群播曲目 |
| `broadcast.pause` | 暂停当前群播曲目 |
| `broadcast.seek` | 跳转到指定进度 |
| `broadcast.stop` | 退出群播模式 |
| `broadcast.status` | 接收或展示当前群播状态 |

群播 payload 会包含核心字段：

```json
{
  "broadcastId": "broadcast-xxxx",
  "queueSongIds": ["song-id-1", "song-id-2"],
  "currentIndex": 0,
  "trackId": "song-id-1",
  "positionMs": 0,
  "state": "playing",
  "version": 1,
  "updatedByClientId": "web-control-xxxx",
  "updatedAt": 1710000000
}
```

网页播放器进入群播模式后：

- active queue 应切换为 broadcast queue。
- `playback.update` 必须带 `broadcastId`。
- 本地普通 next/prev 应优先遵循群播队列。
- 收到 `broadcast.stop` 后退出群播模式，但不必清空普通 local queue 或 session queue。
- 如果浏览器阻止自动播放，应在 UI 上保持待播放状态，并在用户点击后继续执行当前群播状态。

网页播放器需要补齐的是：作为 `player` 端真正执行这些命令，而不是只展示群播状态。

---

## 九、状态恢复

网页播放器刷新页面或 Socket 重连后，需要重新：

1. 连接 namespace `/emo`，path `/emo/ws`。
2. 执行 `auth.login`。
3. 执行 `device.register`。
4. 使用稳定的 `clientId` 和 `sessionId`。
5. 接收服务端恢复的队列与播放状态。
6. 主动请求可能缺失的本地队列。

服务端在设备注册后会尝试推送已有的：

- `queue.session.sync`
- `queue.local.set`
- `playback.update`

但当前实现会跳过向同一个 `sourceClientId` 回推它自己的 local queue。因此网页播放器注册成功后，应主动发送一次：

```json
{
  "type": "state",
  "action": "queue.local.get",
  "requestId": "local-queue-restore-1",
  "payload": {
    "sessionId": "web-session-xxxx",
    "clientId": "web-player-xxxx"
  }
}
```

恢复策略建议：

- 先恢复服务端推送的 session queue。
- 再恢复或请求自己的 local queue。
- 再应用最近的 `playback.update` 到 UI。
- 不要在刷新后自动强制播放，除非浏览器允许且用户此前已经与页面交互。
- 如果当前处于群播中，但刷新后未收到 active broadcast 状态，页面应显示普通模式，等待后续 broadcast 命令。

---

## 十、浏览器播放限制

浏览器通常会限制未经用户交互的自动播放。

因此验收远程播放控制时需要明确前置条件：

- 用户已经打开 `/player`。
- 用户至少点击过一次页面播放相关控件，或浏览器允许该站点自动播放。
- 如果远程 `player.play` 或 `broadcast.play` 被浏览器拒绝，播放器应保留目标曲目和队列状态，并显示需要用户交互的状态。

实现上需要捕获：

```javascript
audio.play().catch((error) => {
  // show blocked state and keep pending playback intent
});
```

被浏览器阻止播放时仍应上报可诊断状态，例如：

```json
{
  "state": "paused",
  "trackId": "song-id-1",
  "positionMs": 0
}
```

第一版可以只在 UI 上提示“等待用户点击播放”，不需要绕过浏览器策略。

---

## 十一、页面定位

网页播放器页面不应该做成另一个复杂控制台。

建议页面定位为：

```text
播放器本体 + 简单 Socket 状态栏
```

主要展示：

- 当前播放
- 播放队列
- 曲库搜索
- 播放控制
- Socket 连接状态
- Client ID
- Session ID
- 当前模式：本地播放 / 单播受控 / 群播中

不建议第一版在播放器里重复实现完整控制台功能，因为现有 `/control` 已经承担了控制端角色。

---

## 十二、推荐实现拆分

建议按以下顺序实现：

1. 新增 `/player` 路由和模板。
2. 接入曲库搜索和 track metadata。
3. 完成本地 audio 播放、进度、音量、队列。
4. 接入 Socket.IO `auth.login` 和 `device.register`。
5. 在 `/control` 中确认能看到 `roles: ["player"]` 的网页播放器。
6. 实现 `playback.update` 上报。
7. 实现单播命令处理。
8. 实现 `queue.local.*` 和 `queue.session.sync`。
9. 实现状态恢复和 `queue.local.get`。
10. 实现 broadcast 命令处理。
11. 增加最小测试和手动验收记录。

---

## 十三、验收标准

第一版完成后，需要满足以下验收点：

1. 打开 `/player` 可以正常搜索并播放曲库音乐。
2. `/player` 会连接 namespace `/emo`，path `/emo/ws`。
3. `/player` 所有业务消息都通过 Socket.IO `message` event 发送。
4. `/player` 会注册为 `roles: ["player"]`。
5. `/player` 刷新后复用稳定 `clientId` 和 `sessionId`。
6. `/control` 页面能看到该网页播放器在线。
7. `/control` 可以对网页播放器执行播放、暂停、上一首、下一首、跳转、音量设置。
8. 网页播放器执行远程命令后会回传 `playback.update`。
9. 网页播放器播放状态变化后，`/control` 能看到实时状态变化。
10. `/control` 下发 `queue.local.set` 后，只有目标 `sourceClientId` 对应的网页播放器会应用为自己的 local queue。
11. `/control` 下发 `queue.session.sync` 后，网页播放器能更新 session queue。
12. `queue.playItem` 能分别从 session queue 和 self local queue 播放指定 index。
13. 刷新 `/player` 后，设备能重新注册，并恢复 session queue、自己的 local queue 和最近播放状态。
14. `/control` 发起群播后，网页播放器能加入群播并执行播放、暂停、跳转、切歌。
15. 群播中的 `playback.update` 会带上 `broadcastId`。
16. 浏览器阻止自动播放时，页面不会丢失远程播放意图，并能提示用户点击继续。

---

## 十四、建议验证命令

文档或前端页面改动后，至少运行：

```bash
python -m unittest tests.base.test_emo_ws
```

如果新增了 `/player` 路由、搜索接口或模板测试，补充运行对应 frontend 测试，例如：

```bash
python -m unittest tests.frontend.test_i18n
python -m unittest tests.api.test_search
python -m unittest tests.api.test_media
```

手动验收需要同时打开：

```text
/player
/control
```

并确认 `/player` 出现在 `/control` 的在线播放器列表中。

---

## 最终目标一句话

将网页播放器从“单机网页播放页面”升级为 Emosonic Socket.IO 体系中的标准 `player` 客户端，使其能够被现有 `/control` 控制台发现、控制、同步队列，并参与群播播放。
