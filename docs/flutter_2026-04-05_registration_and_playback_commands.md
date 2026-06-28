# Flutter 对接说明（2026-04-05）

本文档给 Flutter 工程师使用，汇总当前可用的用户注册调用，以及最新的播放控制命令。

服务端基础信息：

- Base URL: `http://<host>:<port>`
- Socket.IO path: `/emo/ws`
- Socket.IO namespace: `/emo`
- 业务事件名: `message`

## 1. 用户注册

### 1.1 Web 注册页

- `GET /user/register`
- `POST /user/register`

注册成功后：

- 服务端会自动登录当前用户
- 页面会跳转到 `returnUrl` 或首页

### 1.2 App 注册接口

- `POST /user/register.json`

支持：

- `application/json`
- `form` 提交

请求示例：

```json
{
  "user": "alice",
  "password": "secret123",
  "passwordConfirm": "secret123",
  "mail": "alice@example.com"
}
```

字段说明：

- `user`: 用户名，必填
- `password`: 密码，必填
- `passwordConfirm`: 二次确认密码，必填
- `mail`: 邮箱，可选

成功响应：

```json
{
  "ok": true,
  "user": {
    "id": "3d7f2a63-f2dc-4d7d-ae03-5bfde2b15f38",
    "name": "alice"
  }
}
```

失败响应示例：

```json
{
  "ok": false,
  "error": "The passwords don't match."
}
```

注意：

- 注册成功后，JSON 接口也会自动建立 session
- 如果服务端关闭注册开关，会返回：
  - HTTP `403`
  - `{"ok": false, "error": "User registration is disabled."}`

## 2. Socket.IO 最新播放控制命令

所有控制命令都通过 `message` 事件发送。

基础格式：

```json
{
  "type": "command",
  "action": "<action-name>",
  "requestId": "req-1",
  "targetClientId": "player-1",
  "payload": {}
}
```

说明：

- `targetClientId`: 要执行命令的目标播放器
- `sourceClientId`: 服务端转发后会自动补给目标设备，表示谁发起了命令

## 3. `queue.playItem`

用途：

- 让目标播放器切到某个队列项并立即播放

支持两种队列：

1. session shared queue
2. device local queue

### 3.1 播放 session 共享队列中的某一项

```json
{
  "type": "command",
  "action": "queue.playItem",
  "requestId": "cmd-queue-1",
  "targetClientId": "player-1",
  "payload": {
    "sessionId": "sess-main",
    "queueIndex": 2
  }
}
```

含义：

- 从 `sess-main` 的 shared queue 中
- 选中索引 `2`
- 让 `player-1` 立即播放

### 3.2 播放设备 local queue 中的某一项

```json
{
  "type": "command",
  "action": "queue.playItem",
  "requestId": "cmd-queue-2",
  "targetClientId": "player-1",
  "payload": {
    "sessionId": "sess-main",
    "clientId": "player-1",
    "queueIndex": 1
  }
}
```

含义：

- 从 `player-1` 自己的 local queue 中
- 选中索引 `1`
- 立即播放

规则：

- `sessionId` 必填
- `queueIndex` 必填，且必须是整数且 `>= 0`
- `clientId` 可选
- 不带 `clientId`：表示 shared session queue
- 带 `clientId`：表示该设备 local queue
- 第一版要求：`clientId == targetClientId`

## 4. `player.requestState`

用途：

- 让目标播放器重新把当前真实状态上报到服务端

请求示例：

```json
{
  "type": "command",
  "action": "player.requestState",
  "requestId": "cmd-state-1",
  "targetClientId": "player-1",
  "payload": {
    "sessionId": "sess-main",
    "includePlayback": true,
    "includeSessionQueue": true,
    "includeLocalQueue": true,
    "includeReadyState": false
  }
}
```

字段说明：

- `sessionId`: 可选；如果传了，必须是非空字符串
- `includePlayback`: 是否上报 `playback.update`
- `includeSessionQueue`: 是否上报 `queue.session.sync`
- `includeLocalQueue`: 是否上报 `queue.local.set`
- `includeReadyState`: 是否额外上报 `queue.ready.complete`

推荐默认策略：

- `includePlayback = true`
- `includeSessionQueue = true`
- `includeLocalQueue = true`
- `includeReadyState = false`

## 5. Flutter 播放器收到命令后的回传要求

### 5.1 收到 `queue.playItem`

播放器执行流程：

1. 判断是 shared queue 还是 local queue
2. 根据 `queueIndex` 找到目标 songId
3. 切到该项并开始播放
4. 成功后回传真实状态

至少回传：

1. `playback.update`
2. `queue.session.sync` 或 `queue.local.set`

可选回传：

3. `queue.ready.complete`

### 5.2 收到 `player.requestState`

播放器按命令里的布尔开关，重新发送对应状态：

- `playback.update`
- `queue.session.sync`
- `queue.local.set`
- 可选 `queue.ready.complete`

## 6. 状态上报格式

### 6.1 `playback.update`

```json
{
  "type": "event",
  "action": "playback.update",
  "requestId": "playback-1",
  "payload": {
    "sessionId": "sess-main",
    "state": "playing",
    "trackId": "songId3",
    "positionMs": 0,
    "volume": 70
  }
}
```

### 6.2 `queue.session.sync`

```json
{
  "type": "state",
  "action": "queue.session.sync",
  "requestId": "queue-sync-1",
  "payload": {
    "sessionId": "sess-main",
    "queueSongIds": ["songId1", "songId2", "songId3"],
    "currentIndex": 2,
    "positionMs": 0
  }
}
```

### 6.3 `queue.local.set`

```json
{
  "type": "state",
  "action": "queue.local.set",
  "requestId": "local-queue-1",
  "payload": {
    "sessionId": "sess-main",
    "clientId": "player-1",
    "queueSongIds": ["songId8", "songId9", "songId10"],
    "currentIndex": 1,
    "positionMs": 0
  }
}
```

### 6.4 `queue.ready.complete`

这个动作只是“准备完成通知”，不能替代权威状态。

shared queue 示例：

```json
{
  "type": "state",
  "action": "queue.ready.complete",
  "requestId": "ready-1",
  "payload": {
    "sessionId": "sess-main",
    "queueType": "session",
    "queueSongIds": ["songId1", "songId2", "songId3"]
  }
}
```

local queue 示例：

```json
{
  "type": "state",
  "action": "queue.ready.complete",
  "requestId": "ready-2",
  "payload": {
    "sessionId": "sess-main",
    "queueType": "local",
    "clientId": "player-1",
    "queueSongIds": ["songId8", "songId9", "songId10"]
  }
}
```

## 7. 当前服务端广播行为

- `playback.update`
  - 会广播给同 session 成员和订阅者
- `queue.session.sync`
  - 会广播给同 session 成员和订阅者
- `queue.local.set`
  - 现在也会广播给同 session 成员和订阅者
- `queue.ready.complete`
  - 会广播给同 session 成员和订阅者

`session.subscribe` 初始快照会下发：

- `queue.session.sync`
- 所有该 session 下的 `queue.local.set`
- 所有该 session 下的 `playback.update`

## 8. Flutter 侧建议

建议维护三份本地状态：

1. `sessionQueueBySessionId`
2. `localQueueBySessionClient`
3. `playbackBySessionClient`

建议 key：

- shared queue: `sessionId`
- local queue: `sessionId::clientId`
- playback: `sessionId::clientId`

其中：

- 接收 `queue.local.set` 广播时，用 `payload.sourceClientId` 作为 local queue 所属设备标识
- 发送 `queue.local.set` 请求时，用 `payload.clientId` 指定 local queue owner；省略时表示当前连接自己的 local queue
- `playback.update` 建议用 `payload.sourceClientId` 作为 playback 所属设备标识

## 9. 推荐对接顺序

1. 先接 `POST /user/register.json`
2. 注册成功后建立会话
3. 接 Socket.IO 登录与设备注册
4. 接 `queue.playItem`
5. 接 `player.requestState`
6. 确保播放器能正确回传：
   - `playback.update`
   - `queue.session.sync`
   - `queue.local.set`
   - 可选 `queue.ready.complete`

## 10. App 接入 Last.fm

当前服务端对 App 已提供注册接口：

- `POST /user/register.json`

但 Last.fm 绑定仍然是网页 OAuth 回调模式，不是纯 JSON 接口。

当前可复用的服务端绑定入口：

- `/user/me/lastfm/link?token=...`

### 10.1 推荐接入流程

App 侧建议拆成两步，不要把“注册”和“Last.fm 绑定”混成一次请求。

1. 先注册
2. 再单独发起 Last.fm 授权

### 10.2 第一步：注册

请求：

```json
{
  "user": "alice",
  "password": "secret123",
  "passwordConfirm": "secret123",
  "mail": "alice@example.com"
}
```

接口：

- `POST /user/register.json`

成功后：

- 服务端会自动建立 session
- App 必须保留 cookie / session

这是后续 Last.fm 绑定能否成功的关键。

### 10.3 第二步：发起 Last.fm 授权

App 里点击 `Link Last.fm` 后，打开系统浏览器或内嵌浏览器，访问：

```text
https://www.last.fm/api/auth/?api_key=<apiKey>&cb=<serverBaseUrl>/user/me/lastfm/link
```

其中：

- `<apiKey>` 来自服务端 Last.fm 配置
- `<serverBaseUrl>` 例如 `http://192.168.1.10:5000`

示例：

```text
https://www.last.fm/api/auth/?api_key=YOUR_LASTFM_API_KEY&cb=http://192.168.1.10:5000/user/me/lastfm/link
```

### 10.4 回调行为

用户在 Last.fm 授权后，Last.fm 会回调：

```text
<serverBaseUrl>/user/me/lastfm/link?token=...
```

服务端会：

1. 从回调 URL 里读取 `token`
2. 调用 Last.fm 接口换取 session key
3. 把结果写入当前已登录用户：
   - `lastfm_session`
   - `lastfm_status`

### 10.5 App 侧必须注意的点

`/user/me/lastfm/link` 依赖当前登录用户，所以 App 必须保证：

- 注册成功后保存并复用同一个 session cookie
- 打开授权页时，浏览器访问服务端回调地址时仍能识别这个登录态

如果 session 丢失，服务端就不知道要给哪个用户绑定 Last.fm。

### 10.6 推荐 Flutter 实现方式

1. 用支持 cookie jar 的 HTTP client 调 `/user/register.json`
2. 注册成功后保存 cookie
3. 在 App 中提供 `Link Last.fm` 按钮
4. 点击后打开：

```text
https://www.last.fm/api/auth/?api_key=<apiKey>&cb=<serverBaseUrl>/user/me/lastfm/link
```

5. 用户授权完成后返回 App
6. App 再主动刷新当前用户信息
7. 根据用户状态更新 UI，显示 Last.fm 已绑定

### 10.7 当前限制

当前第一版不提供纯 App 的 Last.fm JSON 绑定接口。

也就是说：

- App 不能只靠一个 POST 接口完成 Last.fm 绑定
- 仍需要走网页授权 + 服务端回调

### 10.8 未来可扩展方向

如果未来要更适合 App，可以再新增两类接口：

1. 获取 Last.fm 授权 URL
2. App 拿到 token 后，提交给服务端完成绑定

但这不属于当前版本。
