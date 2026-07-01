# Flutter Broadcast Playback v1 对接说明

本文档给 Flutter / player 客户端工程师使用，说明如何按 `docs/goal/broadcast.md` 实现第一版群播执行端。

新客户端如果要启用 `effectiveAtPlayback` / `playbackPrepare`，还必须实现
`docs/flutter_effective_at_playback_v2.md` 里的 timing contract。

## 1. 定义

群播是多台 player 设备共同播放同一个 `broadcastId` 下的共享队列。`broadcastId` 是群播权威，设备自己的 `sessionId` 不切换。

v1 不实现：

- `broadcast.join`
- `broadcast.leave`
- ready gate
- 自动加入
- 服务端持久化恢复
- 毫秒级同步

Web 控制台只发协议和展示状态，不执行音频。Flutter / player 客户端负责执行音频命令。

## 2. 本地状态

建议维护：

```dart
class BroadcastPlaybackState {
  String mode = 'solo'; // solo / broadcast
  String? activeBroadcastId;
  String? timelineId;
  List<String> queueSongIds = [];
  int currentIndex = 0;
  String? trackId;
  int positionMs = 0;
  String state = 'stopped'; // playing / paused / stopped / error
  int version = 0;
  int epoch = 0;
  int queueRevision = 0;
  int controlVersion = 0;
  int followDelayMs = 0;
  double serverClockOffsetMs = 0;
}
```

设备仍然保留自己的：

- `clientId`
- `sessionId`
- local queue
- session queue

不要把 `broadcastId` 写进 `sessionId`。

收到带 `timelineId/version/epoch` 的 broadcast payload 时：

- `timelineId` 必须等于 `broadcast:{broadcastId}`。
- `version` 小于等于本地已处理版本时丢弃。
- `epoch` 小于本地已处理 epoch 时丢弃，避免旧歌曲进度覆盖新歌曲。
- `queueRevision` 只表示队列内容版本；播放心跳或 seek 不应改变它。
- `controlVersion` 用于控制命令冲突检测，发控制请求时作为 `baseControlVersion`。

## 3. 收到 `broadcast.start`

服务端会向每台 participant 下发：

```json
{
  "type": "command",
  "action": "broadcast.start",
  "payload": {
    "broadcastId": "broadcast-001",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "trackId": "song-1",
    "positionMs": 0,
    "state": "playing",
    "timelineId": "broadcast:broadcast-001",
    "authorityClientId": "server",
    "originClientId": "phone-1",
    "version": 1,
    "epoch": 1,
    "queueRevision": 1,
    "controlVersion": 1,
    "serverUpdatedAtMs": 1770000000123,
    "serverTimeMs": 1770000000140,
    "updatedAt": 1770000000.123,
    "followDelayMs": 0,
    "autoPlay": true,
    "serverStartAt": null
  }
}
```

客户端处理：

```text
1. mode = broadcast。
2. activeBroadcastId = payload.broadcastId。
3. 保存 queueSongIds/currentIndex/trackId/version/epoch/queueRevision/controlVersion。
4. 加载 trackId。
5. seek 到 positionMs。
6. autoPlay = true 时开始播放。
7. 发送 playback.update，带 broadcastId。
```

如果 `queueSongIds` 为空，服务端会强制 `state = stopped`、`autoPlay = false`，客户端不要开始播放。

## 4. 队列同步

收到 `broadcast.queue.sync`：

```json
{
  "type": "state",
  "action": "broadcast.queue.sync",
  "payload": {
    "broadcastId": "broadcast-001",
    "queueSongIds": ["song-1", "song-4"],
    "currentIndex": 1,
    "trackId": "song-4",
    "positionMs": 0,
    "state": "playing",
    "version": 2,
    "epoch": 2,
    "queueRevision": 2,
    "controlVersion": 2,
    "serverUpdatedAtMs": 1770000000123,
    "serverTimeMs": 1770000000140,
    "updatedByClientId": "pc-1",
    "updatedAt": 1770000000.123
  }
}
```

客户端处理：

```text
if payload.broadcastId != activeBroadcastId:
    只更新 UI，不执行音频
else:
    如果 version/epoch 旧于本地已处理值，丢弃
    更新本地 broadcast queue、version、epoch、queueRevision、controlVersion
    如果当前 track 不在新队列中，按 currentIndex 切换
    否则尽量保持当前播放
```

`broadcast.queue.sync` 主要修改队列。明确切歌请等 `broadcast.playItem`。

## 5. 播放指定歌曲

收到 `broadcast.playItem`：

```json
{
  "type": "command",
  "action": "broadcast.playItem",
  "payload": {
    "broadcastId": "broadcast-001",
    "queueSongIds": ["song-1", "song-4"],
    "queueIndex": 1,
    "trackId": "song-4",
    "positionMs": 0,
    "state": "playing",
    "version": 3,
    "epoch": 3,
    "queueRevision": 2,
    "controlVersion": 3,
    "serverUpdatedAtMs": 1770000000123,
    "serverTimeMs": 1770000000140,
    "updatedAt": 1770000000.123
  }
}
```

客户端处理：

```text
1. 校验 payload.broadcastId == activeBroadcastId。
2. 如果 version/epoch 旧于本地已处理值，丢弃。
3. 更新 queueSongIds/currentIndex/trackId/version/epoch/controlVersion。
4. 加载 trackId。
5. seek 到 positionMs。
6. 开始播放。
7. 上报 playback.update。
```

## 6. 播放、暂停、进度

所有控制命令都包含统一 BroadcastState 核心字段。

如果 Flutter 端主动发送 `broadcast.play/pause/seek/stop`，建议同样带 `baseControlVersion`；服务端会在字段存在时做冲突校验。

### `broadcast.play`

```text
1. 校验 activeBroadcastId。
2. 加载 trackId。
3. seek 到 positionMs。
4. 开始播放。
5. 上报 playback.update。
```

### `broadcast.pause`

```text
1. 校验 activeBroadcastId。
2. seek 到 positionMs。
3. 暂停播放。
4. 上报 playback.update。
```

### `broadcast.seek`

```text
1. 校验 activeBroadcastId。
2. seek 到 positionMs。
3. 如果 payload.state == playing，保持播放。
4. 上报 playback.update。
```

### `broadcast.stop`

```text
1. 校验 activeBroadcastId。
2. 退出 broadcast mode。
3. activeBroadcastId = null。
4. v1 建议停止播放。
5. 上报 playback.update，state = stopped。
```

## 7. 进度估算

服务端 payload 会带 `serverUpdatedAtMs` 和 `serverTimeMs`。客户端优先用 `serverTimeMs` 估算服务端时间偏移；旧消息没有该字段时再回退到顶层 `timestamp`：

```dart
void updateServerClockOffset(Map<String, dynamic> message) {
  final payload = message['payload'] as Map<String, dynamic>? ?? {};
  final serverTimeMs = (payload['serverTimeMs'] as num?)?.toDouble();
  final localNowMs = DateTime.now().millisecondsSinceEpoch;
  if (serverTimeMs != null) {
    broadcastState.serverClockOffsetMs = serverTimeMs - localNowMs;
    return;
  }

  final timestamp = (message['timestamp'] as num?)?.toDouble();
  if (timestamp != null) {
    broadcastState.serverClockOffsetMs = timestamp * 1000 - localNowMs;
  }
}
```

当 `state == playing`：

```dart
final serverNowMs = DateTime.now().millisecondsSinceEpoch +
    broadcastState.serverClockOffsetMs;
final serverUpdatedAtMs =
    (payload['serverUpdatedAtMs'] as num?)?.toDouble() ??
    ((payload['updatedAt'] as num?)?.toDouble() ?? 0) * 1000;
final playbackRate =
    (payload['playbackRate'] as num?)?.toDouble() ?? 1.0;
final followDelayMs =
    (payload['followDelayMs'] as num?)?.toDouble() ?? 0;
final targetPositionMs = max(
  0,
  payloadPositionMs +
      (serverNowMs - serverUpdatedAtMs) * playbackRate -
      followDelayMs,
);
```

当 `state == paused` 或 `stopped`，直接使用 `payload.positionMs`。

v1 不要求毫秒级同步。建议漂移处理：

```text
abs(driftMs) < 300:
    do nothing

300 <= abs(driftMs) < 1000:
    wait for next command or feedback cycle

abs(driftMs) >= 1000:
    seek(targetPositionMs)
```

## 8. 状态上报

参与设备继续使用 `playback.update`：

```json
{
  "type": "event",
  "action": "playback.update",
  "requestId": "broadcast-feedback-1",
  "payload": {
    "sessionId": "root:pc",
    "mode": "broadcast",
    "broadcastId": "broadcast-001",
    "state": "playing",
    "trackId": "song-1",
    "positionMs": 12000,
    "syncDriftMs": -200
  }
}
```

服务端会保存普通 playback state，也会记录 broadcast participant state。

`playback.update` 是执行反馈，不能覆盖 BroadcastState 权威队列和权威播放状态。

## 9. 客户端发起控制

如果 Flutter 端也提供控制入口，修改队列和切歌必须带 `baseControlVersion`。旧字段 `baseVersion` 仍可作为兼容别名，但新客户端应优先发送 `baseControlVersion`。

### `broadcast.queue.sync`

```json
{
  "type": "state",
  "action": "broadcast.queue.sync",
  "requestId": "broadcast-queue-1",
  "payload": {
    "broadcastId": "broadcast-001",
    "queueSongIds": ["song-1", "song-4"],
    "currentIndex": 1,
    "positionMs": 0,
    "baseControlVersion": 2
  }
}
```

### `broadcast.playItem`

```json
{
  "type": "command",
  "action": "broadcast.playItem",
  "requestId": "broadcast-play-item-1",
  "payload": {
    "broadcastId": "broadcast-001",
    "queueIndex": 1,
    "positionMs": 0,
    "baseControlVersion": 3
  }
}
```

如果服务端返回：

```json
{
  "type": "system",
  "action": "system.error",
  "payload": {
    "code": "conflict",
    "currentVersion": 4,
    "currentControlVersion": 4
  }
}
```

客户端应重新拉取 `broadcast.status` 或等待最新广播状态，不要用旧队列覆盖新状态。

## 10. 状态查询

控制端或播放器可查询：

```json
{
  "type": "state",
  "action": "broadcast.status",
  "requestId": "broadcast-status-1",
  "payload": {
    "broadcastId": "broadcast-001"
  }
}
```

响应包含：

- `broadcast`
- `participantStates`

播放器端主要依赖服务端下发的 command 执行音频，`broadcast.status` 可用于 UI 恢复和冲突后重新同步。
