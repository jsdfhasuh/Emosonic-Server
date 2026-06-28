# Flutter Emo Socket.IO 对接文档

本文档给 Flutter 客户端工程师使用，用于对接当前 Emosonic Server 的 Emo 实时控制通道。

## 1. 结论

当前服务端使用的是 `Socket.IO`，不是裸 `WebSocket`。

Flutter 端必须使用 `Socket.IO client`，不要使用普通 WebSocket client。

推荐 Flutter 包：

- `socket_io_client`

当前推荐部署建议：

- 使用 `gevent` 作为正式 Socket.IO 运行时
- 客户端允许 `websocket` 与 `polling` 自动协商

## 2. 服务端连接信息

- 协议：`Socket.IO`
- 基础地址：`http://<host>:5000`
- Socket.IO path：`/emo/ws`
- namespace：`/emo`
- 业务事件名：`message`
- 推荐 transport：`websocket` + `polling`

说明：

- `path` 和 `namespace` 不是一回事
- `path=/emo/ws` 是接入路径
- `namespace=/emo` 是业务命名空间

## 3. 连接流程

客户端必须按以下顺序执行：

1. 连接 Socket.IO
2. 发送 `auth.login`
3. 等待 `auth.login` 的 `system.ack`
4. 发送 `device.register`
5. 等待 `device.register` 的 `system.ack`
6. 发送 `device.list`
7. 收到首次 `device.list` 后进入 ready 状态
8. ready 后才允许发送控制命令和状态同步消息

不要在 `connect` 成功后并发发送多条业务消息。

## 4. 统一消息格式

所有业务消息都通过 Socket.IO 的 `message` 事件发送，消息体为 JSON object。

通用结构：

```json
{
  "type": "command",
  "action": "player.pause",
  "requestId": "req-001",
  "targetClientId": "player-1",
  "payload": {},
  "timestamp": 1710000000
}
```

字段说明：

- `type`：消息大类，如 `auth`、`system`、`device`、`command`、`event`、`state`
- `action`：具体动作名
- `requestId`：请求编号，客户端生成，用于匹配 ack/error
- `targetClientId`：控制消息的目标设备
- `payload`：业务参数
- `timestamp`：客户端本地时间戳，建议带上

## 4.1 `clientId` / `sessionId` 规范

### `clientId`

`clientId` 表示设备实例标识，用于服务端将控制命令路由到正确设备。

要求：

- 同一安装实例内应尽量稳定，不要每次启动随机生成
- 建议首次安装时生成并持久保存到本地
- 同一客户端重启后，应继续使用原来的 `clientId`
- 不同设备必须使用不同的 `clientId`

推荐格式：

- `<app>-<platform>-<stable-id>`
- `<platform>-<device>-<stable-id>`

示例：

- `flutter-android-phone-a1b2c3`
- `flutter-windows-livingroom-01`
- `flutter-ios-ipad-main`

### `sessionId`

`sessionId` 表示播放会话标识，用于服务端识别同一个播放器会话，并恢复该会话的队列和播放状态。

要求：

- `sessionId` 必须稳定，不能按每次连接、每次启动随机生成
- 同一个播放器会话应始终使用同一个 `sessionId`
- 服务端重启后，客户端重新连接时应继续使用原来的 `sessionId`
- 如果一个客户端只是控制器而不是播放器，也应绑定到它要观察或控制的目标会话 `sessionId`

推荐理解：

- `clientId` = 设备是谁
- `sessionId` = 当前控制或播放的是哪一个业务会话

推荐格式：

- `<userName>:<logical-player-name>`
- `<userName>:<room-name>`
- `<userName>:<stable-client-id>`

示例：

- `root:living-room`
- `root:flutter-main`
- `root:player-1`

### 推荐默认策略

如果 Flutter 客户端本身就是播放器，推荐：

- `clientId`：首次安装生成并持久保存
- `sessionId`：固定使用 `<userName>:<logical-player-name>`

如果暂时没有“逻辑播放器名”概念，第一版可直接使用：

- `sessionId = clientId`

但前提仍然是 `clientId` 必须稳定。

### 禁止做法

不要这样做：

- 每次启动随机生成新的 `clientId`
- 每次连接随机生成新的 `sessionId`
- 把 Socket.IO 的临时连接 ID 当作 `sessionId`
- 把 `sessionId` 设计成只在当前进程内有效的临时值

这些做法会导致：

- 服务端无法恢复历史队列
- 服务端无法恢复播放状态
- 同一播放器每次重连都会被识别为新会话

### Flutter 生成策略伪代码

```dart
Future<String> getOrCreateClientId() async {
  final prefs = await SharedPreferences.getInstance();
  final existing = prefs.getString('emo.clientId');
  if (existing != null && existing.isNotEmpty) {
    return existing;
  }

  final generated = 'flutter-android-${const Uuid().v4()}';
  await prefs.setString('emo.clientId', generated);
  return generated;
}

Future<String> getOrCreateSessionId(String userName, String logicalPlayerName) async {
  final prefs = await SharedPreferences.getInstance();
  final key = 'emo.sessionId.$userName.$logicalPlayerName';
  final existing = prefs.getString(key);
  if (existing != null && existing.isNotEmpty) {
    return existing;
  }

  final sessionId = '$userName:$logicalPlayerName';
  await prefs.setString(key, sessionId);
  return sessionId;
}
```

建议持久化保存：

- `emo.clientId`
- `emo.sessionId.<userName>.<logicalPlayerName>`

说明：

- `clientId` 是设备级稳定标识
- `sessionId` 是业务会话级稳定标识
- 两者都不应该随连接重建而变化

## 5. 必须支持的消息

### 5.1 认证：`auth.login`

发送：

```json
{
  "type": "auth",
  "action": "auth.login",
  "requestId": "auth-1",
  "payload": {
    "u": "root",
    "p": "camu1217"
  }
}
```

成功返回：

```json
{
  "type": "system",
  "action": "system.ack",
  "requestId": "auth-1",
  "payload": {
    "authenticated": true,
    "userName": "root"
  }
}
```

失败返回：

```json
{
  "type": "system",
  "action": "system.error",
  "requestId": "auth-1",
  "payload": {
    "code": "unauthorized",
    "message": "Invalid credentials"
  }
}
```

### 5.2 设备注册：`device.register`

认证成功后发送：

```json
{
  "type": "device",
  "action": "device.register",
  "requestId": "register-1",
  "payload": {
    "clientId": "flutter-player-1",
    "deviceName": "Android Player",
    "roles": ["player", "controller"],
    "sessionId": "sess-main",
    "capabilities": {}
  }
}
```

成功返回：

```json
{
  "type": "system",
  "action": "system.ack",
  "requestId": "register-1",
  "payload": {
    "client": {
      "userName": "root",
      "deviceName": "Android Player",
      "roles": ["player", "controller"],
      "sessionId": "sess-main",
      "capabilities": {},
      "clientId": "flutter-player-1",
      "connectedAt": 1774321766.4258795
    }
  }
}
```

### 5.3 设备列表：`device.list`

注册成功后发送：

```json
{
  "type": "device",
  "action": "device.list",
  "requestId": "device-list-1",
  "payload": {}
}
```

返回/广播：

```json
{
  "type": "state",
  "action": "device.list",
  "payload": {
    "devices": [
      {
        "userName": "root",
        "deviceName": "Living Room Player",
        "roles": ["player", "controller"],
        "sessionId": "sess-main",
        "capabilities": {},
        "clientId": "player-1",
        "connectedAt": 1774321766.4258795
      }
    ]
  },
  "timestamp": 1774321766.429714
}
```

注意：

- `device.list` 既可能是主动请求返回
- 也可能是服务端在设备上线/断开后主动广播
- Flutter 端应将其视为“设备状态更新事件”

### 5.4 控制命令：`player.*`

支持动作：

- `player.play`
- `player.pause`
- `player.next`
- `player.prev`
- `player.seek`
- `player.requestState`
- `queue.playItem`

示例：暂停目标播放器

```json
{
  "type": "command",
  "action": "player.pause",
  "requestId": "cmd-1",
  "targetClientId": "player-1",
  "payload": {}
}
```

示例：seek 到 15000ms

```json
{
  "type": "command",
  "action": "player.seek",
  "requestId": "cmd-2",
  "targetClientId": "player-1",
  "payload": {
    "positionMs": 15000
  }
}
```

示例：让目标播放器从 session 队列中切到指定项并播放

```json
{
  "type": "command",
  "action": "queue.playItem",
  "requestId": "cmd-3",
  "targetClientId": "player-1",
  "payload": {
    "sessionId": "sess-main",
    "queueIndex": 2
  }
}
```

示例：让目标播放器从自己的 local queue 中切到指定项并播放

```json
{
  "type": "command",
  "action": "queue.playItem",
  "requestId": "cmd-4",
  "targetClientId": "player-1",
  "payload": {
    "sessionId": "sess-main",
    "clientId": "player-1",
    "queueIndex": 1
  }
}
```

规则：

- `sessionId` 必填
- `queueIndex` 必填，且必须是整数且 >= 0
- `clientId` 可选
- 不带 `clientId` 时，表示从 shared session queue 中选歌
- 带 `clientId` 时，表示从该设备 local queue 中选歌
- 第一版要求 `clientId == targetClientId`
- 播放器执行后必须回传 `playback.update`

示例：请求目标播放器重新上报当前状态

```json
{
  "type": "command",
  "action": "player.requestState",
  "requestId": "cmd-5",
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

规则：

- `sessionId` 可选；如果带了，必须是非空字符串
- `includePlayback` 可选，布尔值
- `includeSessionQueue` 可选，布尔值
- `includeLocalQueue` 可选，布尔值
- `includeReadyState` 可选，布尔值
- Flutter 播放器收到后，应按这些开关重新发送对应状态
- 如果开关没带，播放器可以按自己的默认策略处理，推荐默认上传：
  - `playback.update`
  - `queue.session.sync`
  - `queue.local.set`

服务端对控制端返回：

```json
{
  "type": "system",
  "action": "system.ack",
  "requestId": "cmd-1",
  "payload": {
    "forwarded": true
  }
}
```

目标播放器会收到同一条 `player.*` 命令消息。

### 5.5 播放状态回传：`playback.update`

播放器执行命令后，必须主动回传当前真实状态：

```json
{
  "type": "event",
  "action": "playback.update",
  "requestId": "playback-1",
  "payload": {
    "sessionId": "sess-main",
    "state": "paused",
    "trackId": "track-123",
    "positionMs": 81234,
    "volume": 70
  }
}
```

服务端收到后会：

- 更新权威状态
- 广播新的 `playback.update`

广播示例：

```json
{
  "type": "state",
  "action": "playback.update",
  "payload": {
    "sessionId": "sess-main",
    "sourceClientId": "player-1",
    "state": "paused",
    "trackId": "track-123",
    "positionMs": 81234,
    "volume": 70,
    "updatedAt": 1774322000.123
  }
}
```

说明：

- `sourceClientId` 表示最后一次上报该播放状态的设备
- Flutter 控制器可用它判断是哪台播放器发出的状态变化

### 5.6 会话队列同步：`queue.session.sync`

发送：

```json
{
  "type": "state",
  "action": "queue.session.sync",
  "requestId": "queue-1",
  "payload": {
    "sessionId": "sess-main",
    "queueSongIds": ["songId1", "songId2", "songId3"],
    "currentIndex": 2,
    "positionMs": 8000
  }
}
```

服务端会：

- 更新权威队列
- 广播新的 `queue.session.sync`

广播示例：

```json
{
  "type": "state",
  "action": "queue.session.sync",
  "payload": {
    "sessionId": "sess-main",
    "sourceClientId": "controller-1",
    "queueSongIds": ["songId1", "songId2", "songId3"],
    "currentIndex": 2,
    "positionMs": 8000,
    "updatedAt": 1774322100.123
  }
}
```

字段要求：

- `sessionId`：必填，稳定的会话标识
- `queueSongIds`：必填，字符串数组
- `currentIndex`：必填，整数
- `positionMs`：必填，整数

校验建议：

- `queueSongIds` 非空时，`currentIndex` 必须在有效范围内
- 空队列时使用：

```json
{
  "sessionId": "sess-main",
  "queueSongIds": [],
  "currentIndex": 0,
  "positionMs": 0
}
```

说明：

- `sourceClientId` 表示最后一次提交该会话队列的设备
- 这对控制器 UI 识别“是谁改了队列”很有帮助

### 5.7 设备本地队列：`queue.local.get` / `queue.local.set`

这两条消息当前服务端已经实现。

语义：

- `queue.local.get`
  - 获取某个设备在某个房间中的本地草稿队列
- `queue.local.set`
  - 更新某个设备在某个房间中的本地草稿队列
  - `payload.clientId` 表示要保存的 local queue owner
  - 如果省略 `payload.clientId`，服务端使用当前连接注册的 `clientId`

本地队列更新会推送给：

- 同一 `sessionId` 下的在线设备
- 当前订阅了该 `sessionId` 的控制端

`queue.local.get` 请求：

```json
{
  "type": "state",
  "action": "queue.local.get",
  "requestId": "local-get-1",
  "payload": {
    "sessionId": "sess-main",
    "clientId": "player-1"
  }
}
```

`queue.local.set` 请求：

```json
{
  "type": "state",
  "action": "queue.local.set",
  "requestId": "local-set-1",
  "payload": {
    "sessionId": "sess-main",
    "clientId": "player-1",
    "queueSongIds": ["songId1", "songId2", "songId3"],
    "currentIndex": 1,
    "positionMs": 0
  }
}
```

成功后：

- 服务端返回 `system.ack`
- 并广播一条 `state / queue.local.set` 快照给同 session 成员和订阅者
- 广播 payload 的 `sourceClientId` 表示 local queue owner，不一定是发起请求的控制器

本地队列快照示例：

```json
{
  "type": "state",
  "action": "queue.local.set",
  "payload": {
    "sessionId": "sess-main",
    "sourceClientId": "player-1",
    "queueSongIds": ["songId1", "songId2", "songId3"],
    "currentIndex": 1,
    "positionMs": 0,
    "updatedAt": 1774322100.123
  }
}
```

### 5.8 队列准备完成通知：`queue.ready.complete`

这条消息用于播放器通知控制端和订阅者：

- 某条 shared queue 或 local queue 已经在本地准备完成

它是一个状态通知事件，不替代权威队列状态。

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

校验规则：

- 当前连接必须已经 `device.register`
- `sessionId` 必填
- `queueType` 只能是 `session` 或 `local`
- `queueSongIds` 必须是字符串数组
- `local` 模式下 `clientId` 必须等于当前设备自己的 `clientId`

服务端接受后会：

- 返回 `system.ack`
- 广播一条 `state / queue.ready.complete` 给同 session 成员和订阅者
- 广播 payload 中会补上 `sourceClientId`

这个动作适合做 UI 层“队列已经准备好了”的提示，不应用来替代：

- `queue.session.sync`
- `queue.local.set`
- `playback.update`

### 5.9 心跳：`system.ping`

发送：

```json
{
  "type": "system",
  "action": "system.ping",
  "requestId": "ping-1",
  "payload": {}
}
```

返回：

```json
{
  "type": "system",
  "action": "system.pong",
  "requestId": "ping-1",
  "payload": {}
}
```

说明：

- Socket.IO 自身已有底层保活
- 客户端注册设备后建议每 30 秒发送一次业务层 `system.ping`
- 服务端默认 90 秒未收到任何业务消息或 `system.ping` 时，会把该客户端视为离线并从 `/devices` 列表中清理

## 6. 错误消息格式

所有业务错误都走：

```json
{
  "type": "system",
  "action": "system.error",
  "requestId": "xxx",
  "payload": {
    "code": "bad_request|unauthorized|forbidden|not_found|not_supported",
    "message": "..."
  }
}
```

Flutter 端应根据 `requestId` 匹配对应请求，并将错误信息抛给上层 UI 或日志系统。

## 7. Flutter 客户端建议状态机

建议客户端维护以下状态：

- `disconnected`
- `connected`
- `authenticated`
- `registered`
- `ready`

推荐状态流转：

- `connect` -> 发送 `auth.login`
- 收到 `auth.login ack` -> 发送 `device.register`
- 收到 `device.register ack` -> 发送 `device.list`
- 收到首次 `device.list` -> 进入 `ready`

只有进入 `ready` 后，才允许发控制命令。

## 8. Flutter 端建议封装接口

建议封装一个 `EmoRealtimeClient`，最少包含：

- `connect()`
- `disconnect()`
- `login(user, password)`
- `registerDevice(clientId, deviceName, roles, sessionId)`
- `requestDeviceList()`
- `sendPlay(targetClientId)`
- `sendPause(targetClientId)`
- `sendNext(targetClientId)`
- `sendPrev(targetClientId)`
- `sendSeek(targetClientId, positionMs)`
- `syncSessionQueue(sessionId, queueSongIds, currentIndex, positionMs)`
- `getLocalQueue(sessionId, clientId)`
- `setLocalQueue(sessionId, clientId, queueSongIds, currentIndex, positionMs)`
- `updatePlaybackState(...)`

并建议向 UI 层暴露：

- `onConnectionStateChanged`
- `onDeviceListUpdated`
- `onPlaybackStateUpdated`
- `onQueueUpdated`
- `onLocalQueueUpdated`
- `onCommandReceived`
- `onError`

## 9. 角色建议

客户端注册时使用 `roles`：

- 纯播放器：`["player"]`
- 纯遥控器：`["controller"]`
- 既能播放又能控制：`["player", "controller"]`

如果 Flutter 客户端既能播放又能控制别人，建议注册：

```json
["player", "controller"]
```

## 10. 对接验收标准

Flutter 工程师接入完成后，至少验证：

1. 能连接 `http://<host>:5000`
2. 使用 path `/emo/ws`
3. 使用 namespace `/emo`
4. 能成功完成 `auth.login`
5. 能成功完成 `device.register`
6. 能收到 `device.list`
7. 控制端能发 `player.pause`
8. 播放端能收到 `player.pause`
9. 播放端能发 `playback.update`
10. 控制端能收到 `playback.update`
11. 控制端能发 `queue.session.sync`
12. 同一 `sessionId` 的客户端能收到新的会话队列广播

## 11. 重要注意事项

- 这不是裸 WebSocket，是 `Socket.IO`
- `path` 和 `namespace` 不是一回事：
  - path = `/emo/ws`
  - namespace = `/emo`
- 所有业务消息都通过 `message` 事件发送
- 客户端主动发送的业务消息建议全部带 `requestId`
- 必须串行等待 ack，不能一连上就并发发三条业务消息
- `device.list` 可能是主动请求返回，也可能是服务端广播

## 12. Flutter 伪代码示例

```dart
final socket = io(
  'http://127.0.0.1:5000/emo',
  OptionBuilder()
      .setTransports(['websocket', 'polling'])
      .setPath('/emo/ws')
      .disableAutoConnect()
      .build(),
);

socket.onConnect((_) {
  socket.emit('message', {
    'type': 'auth',
    'action': 'auth.login',
    'requestId': 'auth-1',
    'payload': {'u': userName, 'p': password},
  });
});

socket.on('message', (data) {
  final action = data['action'];
  final requestId = data['requestId'];

  if (action == 'system.ack' && requestId == 'auth-1') {
    socket.emit('message', {
      'type': 'device',
      'action': 'device.register',
      'requestId': 'register-1',
      'payload': {
        'clientId': 'flutter-player-1',
        'deviceName': 'Android Player',
        'roles': ['player', 'controller'],
        'sessionId': 'sess-main',
        'capabilities': {},
      }
    });
    return;
  }

  if (action == 'system.ack' && requestId == 'register-1') {
    socket.emit('message', {
      'type': 'device',
      'action': 'device.list',
      'requestId': 'device-list-1',
      'payload': {},
    });
    return;
  }

  if (action == 'device.list') {
    // ready
  }

  if (action == 'player.pause') {
    // execute local pause
    socket.emit('message', {
      'type': 'event',
      'action': 'playback.update',
      'requestId': 'playback-1',
      'payload': {
        'sessionId': 'sess-main',
        'state': 'paused',
        'trackId': 'track-123',
        'positionMs': 81234,
        'volume': 70,
      }
    });
  }
});

socket.connect();
```

## 13. 最短交付摘要

给 Flutter 工程师的最短版本：

- 协议：`Socket.IO`
- baseUrl：`http://<host>:5000`
- path：`/emo/ws`
- namespace：`/emo`
- event：统一使用 `message`
- 顺序：`auth.login -> device.register -> device.list -> ready`
- ready 后才发 `player.*` / `queue.session.sync`
- 播放器执行命令后必须主动发 `playback.update`
