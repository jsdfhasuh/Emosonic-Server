# Flutter Follow Playback v1 对接说明

本文档给 Flutter 播放器工程师使用，说明如何基于现有 Emosonic Socket.IO 协议实现第一版跟播。

## 1. 定义

跟播是设备 A 跟随设备 B 播放。设备 B 是权威播放源，设备 A 不切换自己的 `sessionId`，只订阅 B 所在的 `sessionId`，并复制 B 的队列、当前歌曲、播放状态和播放进度。

本阶段不实现 `broadcastId`，不实现群播队列，也不新增群播数据库结构。

## 2. 本地状态

建议维护：

```dart
class FollowPlaybackState {
  String mode = 'solo'; // solo / follow
  String? followSourceClientId;
  String? followSessionId;
  bool followQueue = false;
  bool followPlayback = false;
  double serverClockOffsetMs = 0;
}
```

同时继续维护现有实时状态：

```dart
final sessionQueueBySessionId = <String, Map<String, dynamic>>{};
final localQueueBySessionClient = <String, Map<String, dynamic>>{};
final playbackBySessionClient = <String, Map<String, dynamic>>{};
```

key 规则：

- session queue: `sessionId`
- local queue: `sessionId::sourceClientId`
- playback: `sessionId::sourceClientId`
- timeline version: `timelineId`

收到带 `timelineId/version/epoch` 的播放消息时，按 `timelineId` 保存最近版本：

- `version` 小于等于本地已处理版本时丢弃。
- `epoch` 小于本地已处理 epoch 时丢弃，避免旧歌曲进度覆盖新歌曲。
- `queueRevision` 只用于队列冲突和展示，不要拿播放心跳的 `version` 判断队列能否提交。

## 3. 开始跟播

用户选择“跟随设备 B 播放”时，从 `device.list` 拿到 B 的：

- `clientId`
- `sessionId`

发送：

```json
{
  "type": "state",
  "action": "follow.start",
  "requestId": "follow-start-1",
  "payload": {
    "sourceClientId": "设备B的clientId",
    "sourceSessionId": "设备B的sessionId"
  }
}
```

服务端会保存 follow relationship，并自动订阅源设备 session。收到 `system.ack` 后进入 follow 模式：

```json
{
  "mode": "follow",
  "followSourceClientId": "设备B的clientId",
  "followSessionId": "设备B的sessionId",
  "followQueue": true,
  "followPlayback": true
}
```

订阅成功后，服务端会推送该 session 当前的：

- `queue.session.sync`
- `queue.local.set`
- `playback.update`

## 4. 停止跟播

发送：

```json
{
  "type": "state",
  "action": "follow.stop",
  "requestId": "follow-stop-1",
  "payload": {
    "sourceSessionId": "followSessionId"
  }
}
```

随后清空本地 follow 状态，不再根据源设备事件自动播放、暂停、切歌或 seek。本机自己的 `sessionId` 不变。

## 5. 事件过滤

### `queue.session.sync`

session queue 是会话共享队列，按 `sessionId` 接收：

```text
if mode == 'follow'
and payload.sessionId == followSessionId:
    update session follow queue
else:
    update UI only
```

不要用 `payload.sourceClientId == followSourceClientId` 过滤 session queue。`sourceClientId` 只表示最后提交该 session queue 的设备，可能是控制器。

### `queue.local.set`

local queue 是设备队列，按源设备过滤：

```text
if mode == 'follow'
and payload.sessionId == followSessionId
and payload.sourceClientId == followSourceClientId:
    update source local follow queue
else:
    update UI only
```

### `playback.update`

播放状态按源设备过滤：

```text
if mode == 'follow'
and payload.sessionId == followSessionId
and payload.sourceClientId == followSourceClientId:
    execute follow playback
else:
    update UI only
```

## 6. 队列一致性

v1 期望播放设备尽量保持 session queue 和自己的 local queue 一致。

如果两者同时存在但不一致，执行优先级建议为：

1. 以源设备 `playback.update.trackId` 为当前真实歌曲。
2. 如果 `playback.update.queueType == 'local'`，优先使用 `sessionId::followSourceClientId` 的 local queue。
3. 如果 `playback.update.queueType == 'session'`，优先使用 `followSessionId` 的 session queue。
4. 如果 `queueType` 缺失，先尝试在源 local queue 中匹配 `trackId`，再尝试 session queue。
5. 仍然匹配不到时，直接按 `trackId` 加载播放。

推荐播放器在 `playback.update` 里额外带：

```json
{
  "queueType": "local",
  "queueClientId": "player-1"
}
```

这两个字段是推荐字段，当前服务端会透传并保存到 playback JSON。

## 7. 进度同步

服务端广播会带 `serverUpdatedAtMs` 和 `serverTimeMs`。Flutter 端优先用 `serverTimeMs` 估算服务端时间偏移；旧消息没有该字段时再回退到顶层 `timestamp`：

```dart
void updateServerClockOffset(Map<String, dynamic> message) {
  final payload = message['payload'] as Map<String, dynamic>? ?? {};
  final serverTimeMs = (payload['serverTimeMs'] as num?)?.toDouble();
  final localNowMs = DateTime.now().millisecondsSinceEpoch;
  if (serverTimeMs != null) {
    followState.serverClockOffsetMs = serverTimeMs - localNowMs;
    return;
  }

  final timestamp = (message['timestamp'] as num?)?.toDouble();
  if (timestamp != null) {
    followState.serverClockOffsetMs = timestamp * 1000 - localNowMs;
  }
}
```

源设备状态为 `playing` 时：

```dart
final serverNowMs = DateTime.now().millisecondsSinceEpoch +
    followState.serverClockOffsetMs;
final serverUpdatedAtMs =
    (payload['serverUpdatedAtMs'] as num?)?.toDouble() ??
    ((payload['updatedAt'] as num?)?.toDouble() ?? 0) * 1000;
final playbackRate =
    (payload['playbackRate'] as num?)?.toDouble() ?? 1.0;
final followDelayMs =
    (payload['followDelayMs'] as num?)?.toDouble() ?? 700;
final targetPositionMs = max(
  0,
  sourcePositionMs +
      (serverNowMs - serverUpdatedAtMs) * playbackRate -
      followDelayMs,
);
```

源设备状态为 `paused` 时：

```dart
final targetPositionMs = sourcePositionMs;
```

建议漂移处理：

```text
abs(driftMs) < 300:
    do nothing

300 <= abs(driftMs) < 1000:
    wait for next sync

abs(driftMs) >= 1000:
    seek(targetPositionMs)
```

播放、暂停、seek、切歌、上一首、下一首和队列变化属于关键事件，必须立即处理，不要等低频同步。

## 8. 状态上报

跟随设备仍然可以上报自己的 `playback.update`，但这只是执行反馈，不能反向覆盖源设备的权威状态。

建议带上：

```json
{
  "type": "event",
  "action": "playback.update",
  "requestId": "follow-feedback-1",
  "payload": {
    "sessionId": "自己的sessionId",
    "mode": "follow",
    "followSourceClientId": "phone-1",
    "state": "playing",
    "trackId": "song-2",
    "positionMs": 32300,
    "syncDriftMs": -200
  }
}
```

客户端收到自己的反馈广播时，不要把它当作源设备状态再次触发跟播。

## 9. local queue 保存语义

发送 `queue.local.set` 时：

- `payload.clientId` 是 local queue owner。
- 省略 `payload.clientId` 时，owner 是当前连接自己的 `clientId`。
- 服务端广播里的 `payload.sourceClientId` 表示 local queue owner。

控制器给播放器保存 local queue 的示例：

```json
{
  "type": "state",
  "action": "queue.local.set",
  "requestId": "local-set-player-1",
  "payload": {
    "sessionId": "root:phone",
    "clientId": "phone-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "positionMs": 0
  }
}
```

服务端广播：

```json
{
  "type": "state",
  "action": "queue.local.set",
  "payload": {
    "sessionId": "root:phone",
    "sourceClientId": "phone-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "positionMs": 0,
    "updatedAt": 1774322100.123
  }
}
```

## 10. 源设备离线

源设备离线可通过 `device.list` 消失或心跳超时体现。v1 建议：

1. 显示“源设备已离线”。
2. 清空 follow 状态。
3. 本机继续播放当前歌曲，或按产品策略暂停。

## 11. 最小验收

- 可以从设备列表选择一个源设备进入 follow 模式。
- 跟随设备不修改自己的 `sessionId`。
- 可以收到源 session 的 session queue、源 local queue 和源 playback。
- session queue 按 `followSessionId` 接收。
- local queue 和 playback 按 `followSourceClientId` 执行。
- `playing/paused/stopped`、切歌和 seek 能同步执行。
- 进度使用 `serverUpdatedAtMs/serverTimeMs` 和服务端时间偏移计算，并减去 `followDelayMs`。
- 停止跟播后不再执行源设备状态。
