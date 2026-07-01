# Server Authoritative Playback Sync

本文档说明 EmoSonic 服务端为了稳定跟播/群播同步需要做的协议与状态改造。

目标不是让设备彼此直接相信对方的进度，而是让服务端维护一条可排序、可丢弃旧消息、可被所有客户端一致计算的权威播放时间线。

## 1. 背景问题

当前客户端主要依赖 `playback.update.positionMs + updatedAt` 来推算远端进度。这个模型在弱网络或多设备互相回传时容易出现：

- 跟随端短暂比源头更快。
- 旧的 `playback.update` 晚到后覆盖新状态。
- 跟随端回传的状态被其他端误当成源头状态。
- 群播控制和设备本地状态互相影响。
- 客户端各自使用本机时钟推算，误差无法被统一约束。

服务端需要成为“排序和盖章”的唯一权威层。

## 2. 设计原则

1. **受权限约束的多端控制，单向真源**
   - 只有具备控制权限的设备可以发控制命令。
   - 跟随端默认是只读参与者，不能控制源头设备。
   - 服务端负责排序并生成权威 timeline。
   - 客户端只根据服务端 timeline 同步播放，不互相覆盖状态。

2. **服务端时间戳为准**
   - 客户端可以上传自己的 `positionMs`。
   - 服务端必须写入 `serverUpdatedAtMs`。
   - 客户端同步时使用服务端时间轴。

3. **版本号防乱序，但不要把播放心跳和控制冲突混在一起**
   - 同一条 timeline 每次服务端接受新播放锚点时递增 `version`。
   - 客户端按 `timelineId + version` 丢弃旧的播放状态消息。
   - 队列和控制命令使用 `queueRevision/controlVersion` 做冲突检测，避免频繁 `playback.update` 让队列操作误冲突。

4. **epoch 防旧轨道覆盖新轨道**
   - `epoch` 只表示媒体身份代次。
   - 切歌、当前播放项变化、进入/退出群播、切换 source 时递增。
   - 纯 `seek/play/pause` 不递增 `epoch`。
   - 客户端不能用旧 `epoch` 的进度覆盖新 `epoch` 的歌曲。

5. **跟随端默认不保留人为安全延迟**
   - `followDelayMs` 缺省为 `0`。
   - 如果服务端显式下发非零 `followDelayMs`，客户端计算目标进度时再减去这个延迟。

## 3. 新增字段

所有服务端广播的播放状态建议增加以下字段。字段是 additive change，旧客户端可忽略。

```json
{
  "timelineId": "session:root:phone:client:phone-1",
  "authorityClientId": "phone-1",
  "originClientId": "controller-1",
  "version": 42,
  "epoch": 7,
  "queueRevision": 12,
  "controlVersion": 18,
  "serverUpdatedAtMs": 1782730000123,
  "serverTimeMs": 1782730000456,
  "clientInstanceId": "phone-1:boot-20260629-001",
  "clientSeq": 118,
  "playbackRate": 1.0,
  "followDelayMs": 0
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `timelineId` | string | 是 | 权威时间线 ID。设备播放建议 `session:{sessionId}:client:{clientId}`；群播建议 `broadcast:{broadcastId}`。 |
| `authorityClientId` | string | 是 | 当前播放事实的权威设备。跟播时是被跟随设备，不是跟随端。 |
| `originClientId` | string | 否 | 触发本次变更的设备。比如控制器点 seek，则为控制器 clientId。 |
| `version` | int | 是 | 播放 timeline 单调递增版本。服务端接受新的播放锚点或权威控制后递增，用于客户端丢弃旧播放状态。 |
| `epoch` | int | 是 | 媒体身份代次。当前播放项、source 或 broadcast 生命周期变化时递增。 |
| `queueRevision` | int | 建议 | 队列内容、顺序、当前索引变化时递增，用于队列冲突检测。不要因普通播放心跳递增。 |
| `controlVersion` | int | 建议 | 权威控制状态版本。broadcast v1 可继续把现有 `version` 当作 `controlVersion` 的兼容别名。 |
| `serverUpdatedAtMs` | int | 是 | 服务端提交这条 timeline anchor 的毫秒时间戳。 |
| `serverTimeMs` | int | 建议 | 服务端发送消息时的当前毫秒时间，用于客户端估算时钟偏移。 |
| `clientInstanceId` | string | 建议 | 客户端进程实例 ID。App 重启或播放器进程重建时变化，用于限定 `clientSeq` 的作用域。 |
| `clientSeq` | int | 建议 | 同一 `clientInstanceId` 内单调递增序号，用于服务端丢弃同一设备的乱序旧状态。 |
| `playbackRate` | number | 建议 | 播放速率，默认 `1.0`。 |
| `followDelayMs` | int | 建议 | 服务端建议跟随延迟，缺省为 `0`。 |

版本字段规则：

- `version` 解决播放状态消息乱序。
- `queueRevision` 解决队列覆盖冲突。
- `controlVersion` 解决控制命令基于旧状态的问题。
- 客户端展示播放状态时按 `version` 丢弃旧消息。
- 客户端发起 `queue.session.sync`、`broadcast.queue.sync`、`broadcast.playItem` 时应带对应的 `baseQueueRevision` 或 `baseControlVersion`。
- 为兼容现有 broadcast v1，服务端可以继续接受 `baseVersion`，语义等同于 `baseControlVersion`。

`clientSeq` 作用域规则：

- `clientSeq` 必须绑定 `clientInstanceId`。
- 同一 `clientId + clientInstanceId` 内，`clientSeq` 必须单调递增。
- 如果 `clientInstanceId` 变化，服务端应重置该 client 的 seq 窗口。
- 缺少 `clientInstanceId` 的旧客户端视为 legacy。服务端可以接受，但不能用其旧 `clientSeq` 严格拒绝新实例消息。

兼容字段：

- 继续保留旧字段 `updatedAt`，值设为 `serverUpdatedAtMs / 1000`。
- 顶层 `timestamp` 可以继续保留，建议也等于服务端当前时间秒。
- 新客户端优先使用 `serverUpdatedAtMs`，旧客户端继续使用 `updatedAt`。

## 4. 播放状态广播格式

### 4.1 设备播放状态 `playback.update`

```json
{
  "type": "state",
  "action": "playback.update",
  "sourceClientId": "phone-1",
  "timestamp": 1782730000.456,
  "payload": {
    "sessionId": "root:phone",
    "sourceClientId": "phone-1",
    "timelineId": "session:root:phone:client:phone-1",
    "authorityClientId": "phone-1",
    "originClientId": "phone-1",
    "trackId": "song-1",
    "state": "playing",
    "positionMs": 58000,
    "playbackRate": 1.0,
    "version": 42,
    "epoch": 7,
    "queueRevision": 12,
    "controlVersion": 18,
    "clientInstanceId": "phone-1:boot-20260629-001",
    "clientSeq": 118,
    "serverUpdatedAtMs": 1782730000123,
    "serverTimeMs": 1782730000456,
    "updatedAt": 1782730000.123,
    "volume": 70
  }
}
```

语义：

- `positionMs` 是 `serverUpdatedAtMs` 时刻的播放锚点。
- 如果 `state == "playing"`，客户端可以按服务端时间继续推进。
- 如果 `state != "playing"`，客户端必须直接使用 `positionMs`，不要按时间推进。

### 4.2 会话队列 `queue.session.sync`

```json
{
  "type": "state",
  "action": "queue.session.sync",
  "sourceClientId": "phone-1",
  "payload": {
    "sessionId": "root:phone",
    "sourceClientId": "phone-1",
    "timelineId": "session:root:phone:client:phone-1",
    "authorityClientId": "phone-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "positionMs": 58000,
    "version": 42,
    "epoch": 7,
    "queueRevision": 12,
    "controlVersion": 18,
    "serverUpdatedAtMs": 1782730000123,
    "serverTimeMs": 1782730000456,
    "updatedAt": 1782730000.123
  }
}
```

队列和播放状态必须共享同一个 `timelineId/epoch`。`version` 用于播放状态排序，`queueRevision` 用于队列冲突检测。切歌或换队列时，服务端应先更新 timeline，再广播 queue/playback 快照。

## 5. 群播状态格式

群播应由服务端直接维护 `broadcast:{broadcastId}` timeline。所有参与者只跟随这条 broadcast timeline。

```json
{
  "type": "command",
  "action": "broadcast.seek",
  "payload": {
    "broadcastId": "broadcast-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "trackId": "song-1",
    "state": "playing",
    "positionMs": 58000,
    "autoPlay": true,
    "version": 43,
    "epoch": 7,
    "queueRevision": 12,
    "controlVersion": 18,
    "timelineId": "broadcast:broadcast-1",
    "authorityClientId": "server",
    "originClientId": "controller-1",
    "serverUpdatedAtMs": 1782730001000,
    "serverTimeMs": 1782730001020,
    "updatedAt": 1782730001.0,
    "followDelayMs": 0
  }
}
```

群播规则：

- `broadcast.start` 新建 timeline，`version = 1`，`epoch = 1`。
- `broadcast.seek`、`broadcast.pause`、`broadcast.play` 递增 `version/controlVersion`，不递增 `epoch`。
- `broadcast.playItem` 递增 `version/controlVersion`。如果当前播放项变化，同时递增 `epoch`。
- `broadcast.queue.sync` 递增 `queueRevision/controlVersion/version`。只有当前播放项或队列身份变化时才递增 `epoch`。
- `broadcast.stop` 递增 `version/controlVersion`，清理 active broadcast；如果后续新建 broadcast，使用新的 `broadcastId/timelineId` 和新的 `epoch = 1`。
- 参与设备回传的 `playback.update` 不能覆盖 broadcast timeline，只能作为 participant feedback。

## 6. 服务端状态模型

建议服务端保存两类状态。

### 6.1 Device Playback Timeline

按 `timelineId = session:{sessionId}:client:{sourceClientId}` 保存：

```ts
type PlaybackTimeline = {
  timelineId: string;
  sessionId: string;
  authorityClientId: string;
  originClientId?: string;
  trackId?: string;
  state: 'playing' | 'paused' | 'stopped' | 'buffering' | 'error';
  positionMs: number;
  playbackRate: number;
  version: number;
  epoch: number;
  queueRevision: number;
  controlVersion: number;
  serverUpdatedAtMs: number;
  lastClientSeqByClientInstance: Record<string, number>;
  queueSongIds?: string[];
  currentIndex?: number;
};
```

### 6.2 Broadcast Timeline

按 `timelineId = broadcast:{broadcastId}` 保存：

```ts
type BroadcastTimeline = {
  timelineId: string;
  broadcastId: string;
  participants: string[];
  ownerClientId?: string;
  originClientId?: string;
  queueSongIds: string[];
  currentIndex: number;
  trackId?: string;
  state: 'playing' | 'paused' | 'stopped';
  positionMs: number;
  playbackRate: number;
  version: number;
  epoch: number;
  queueRevision: number;
  controlVersion: number;
  serverUpdatedAtMs: number;
  followDelayMs: number;
};
```

### 6.3 Follow Relationship State

跟随关系必须由服务端维护，不能只相信客户端在 `playback.update` 里自报的 `mode=follow` 或 `followSourceClientId`。

建议保存：

```ts
type FollowRelationship = {
  followerClientId: string;
  followerSessionId: string;
  sourceClientId: string;
  sourceSessionId: string;
  userName: string;
  active: boolean;
  createdAtMs: number;
  updatedAtMs: number;
};
```

服务端规则：

- `follow.start` 或现有等价动作创建 follow relationship。
- `follow.stop`、断线清理或 source 下线时关闭 relationship。
- 判断“某个客户端是否是跟随端”时必须查询服务端 relationship。
- payload 中的 `mode=follow`、`followSourceClientId` 只作为反馈字段，不能授予或撤销权限。
- 如果客户端自报的 source 和服务端记录不一致，按服务端记录处理，并可记录告警日志。

## 7. 权限模型

服务端必须先判定客户端在当前模式下的权限，再处理控制命令或状态覆盖。

| 客户端状态 | 可做 | 不可做 |
| --- | --- | --- |
| 本机/solo 播放设备 | 上报自己的 `playback.update`，执行发给自己的 `player.*` 命令 | 覆盖其他设备 timeline |
| 普通控制器 | 向有权限的目标设备发送 `player.*` 命令 | 直接伪造目标设备的 `playback.update` |
| 跟随端/follow participant | 发送自己的播放反馈、`syncDriftMs`、退出跟随 | 控制源头设备，覆盖源头 timeline，修改源头队列 |
| 跟随源/source | 上报源头 timeline，接受有权限控制器的命令 | 被跟随端的反馈覆盖 |
| 群播 owner/controller | 按 `controlPolicy` 控制 broadcast timeline | 绕过 broadcast version/epoch |
| 群播普通参与者 | 跟随 broadcast timeline，发送 participant feedback | 覆盖 broadcast timeline，除非 `controlPolicy` 明确允许 |

权限判定必须使用服务端状态：

- follow 权限来自 `FollowRelationship`。
- broadcast 权限来自 `BroadcastTimeline.participants/controlPolicy/ownerClientId`。
- 设备控制权限来自当前用户、目标设备和角色。
- 客户端 payload 里的 `mode`、`roles`、`followSourceClientId` 不能覆盖服务端注册状态。

跟随端允许的动作建议限定为：

- `follow.stop` 或等价的退出跟随动作。
- 自己设备的 `playback.update(mode=follow)` 反馈。
- 自己设备的音量、本地设置、连接状态上报。

跟随端不应被允许发送以下命令到源头设备：

- `player.play`
- `player.pause`
- `player.seek`
- `player.next`
- `player.prev`
- `queue.session.sync`
- `queue.local.set`
- `queue.playItem`

如果服务端收到跟随端对源头设备的控制命令，应返回：

```json
{
  "type": "system",
  "action": "system.error",
  "requestId": "seek-1",
  "payload": {
    "code": "forbidden",
    "message": "Follow participants cannot control the source timeline"
  }
}
```

## 8. 入站消息处理规则

### 8.1 `playback.update`

服务端收到设备上报：

1. 解析 `sessionId/sourceClientId/trackId/state/positionMs/clientInstanceId/clientSeq`。
2. 校验 `sourceClientId` 必须等于当前已注册设备的 `clientId`，普通客户端不能替其他设备上报。
3. 如果有 `clientInstanceId/clientSeq`，且 `clientSeq <= lastClientSeqByClientInstance[clientInstanceId]`，丢弃或返回 `stale_client_seq`。
4. 如果 `clientInstanceId` 变化，重置该 client 的 seq 窗口。
5. 以服务端当前时间写入 `serverUpdatedAtMs`。
6. 递增该 timeline 的 `version`。
7. 如果媒体身份变化，递增 `epoch`。媒体身份建议定义为 `trackId + currentIndex + queueRevision + authorityClientId`。
8. 广播带 `serverUpdatedAtMs/version/epoch` 的 `playback.update`。

服务端不得直接信任客户端传入的 `updatedAt` 作为权威时间戳。客户端传入的 `updatedAt` 最多只用于日志诊断。

### 8.2 跟随端 `playback.update`

跟随端回传通常会带：

```json
{
  "mode": "follow",
  "followSourceClientId": "phone-1",
  "syncDriftMs": -220
}
```

服务端规则：

- 可以保存为该跟随设备自己的状态。
- 可以用于 participant feedback 或设备列表展示。
- **不能**用它覆盖 `followSourceClientId` 对应的 source timeline。
- **不能**把跟随端的 `positionMs` 广播成源头 session 的权威进度。
- **不能**因此获得源头 timeline 的控制权限。
- 是否处于 follow 模式必须以服务端 `FollowRelationship` 为准。payload 里的 `mode=follow` 只作为客户端反馈。

### 8.3 控制命令

控制命令包括：

- `player.play`
- `player.pause`
- `player.seek`
- `player.next`
- `player.prev`
- `queue.playItem`
- `broadcast.*`

服务端规则：

1. 验证权限。
2. 给命令分配 `requestId`、`originClientId`、服务端时间。
3. 对 broadcast timeline，服务端可以立即更新并广播权威状态。
4. 对普通设备 timeline，服务端应转发给目标设备；目标设备执行后回传 `playback.update`，服务端再盖章广播。
5. 如果需要低延迟 UI，可以额外广播 `playback.pending`，但不要用 pending 覆盖 confirmed timeline。
6. 如果发送方是跟随端，且命令目标是 `followSourceClientId`，必须拒绝，除非未来显式设计了授权的“协同控制”模式。
7. `queue.session.sync` 应校验 `baseQueueRevision`；broadcast 队列/切歌命令应校验 `baseControlVersion`，兼容现有 `baseVersion`。

## 9. 客户端同步公式

### 9.1 服务端时钟偏移

客户端不能直接假设本机时钟等于服务端时钟。每次收到带 `serverTimeMs` 的消息时，可以用本地收包时间估算 offset：

```text
receiveLocalMs = now()
rawOffsetMs = serverTimeMs - receiveLocalMs
serverClockOffsetMs = smooth(previousOffsetMs, rawOffsetMs)
```

如果客户端主动 ping 服务端，推荐使用 RTT 修正：

```text
sendLocalMs = ping send time
receiveLocalMs = pong receive time
rttMs = receiveLocalMs - sendLocalMs
estimatedServerAtReceiveMs = serverTimeMs + rttMs / 2
rawOffsetMs = estimatedServerAtReceiveMs - receiveLocalMs
serverClockOffsetMs = smooth(previousOffsetMs, rawOffsetMs)
```

平滑策略建议：

```text
if no previous offset:
  serverClockOffsetMs = rawOffsetMs
else:
  serverClockOffsetMs = previousOffsetMs * 0.9 + rawOffsetMs * 0.1
```

如果 `abs(rawOffsetMs - previousOffsetMs) > 3000`，说明系统时间或网络状态突变，可以直接采用新 offset 并重新计算目标位置。

### 9.2 目标进度计算

客户端收到权威 timeline 后，使用：

```text
serverNowMs = localNowMs + serverClockOffsetMs
elapsedMs = max(0, serverNowMs - serverUpdatedAtMs)

if state == playing:
  targetPositionMs = positionMs + elapsedMs * playbackRate - followDelayMs
else:
  targetPositionMs = positionMs

targetPositionMs = clamp(targetPositionMs, 0, durationMs)
```

推荐默认：

```text
followDelayMs = 0
```

客户端 drift 修正建议：

- `abs(driftMs) <= 150`：不处理。
- `150 < abs(driftMs) <= 800`：临时调速，例如 `0.98x` 或 `1.02x`。
- `abs(driftMs) > 800`：seek 到 `targetPositionMs`。
- 如果跟随端超前，优先 seek 回 `targetPositionMs`，不要继续按本机进度外推。

## 10. 错误响应

仍使用现有 `system.error`，建议补齐以下错误码。

```json
{
  "type": "system",
  "action": "system.error",
  "requestId": "seek-1",
  "payload": {
    "code": "conflict",
    "message": "Command version is older than the current timeline",
    "timelineId": "broadcast:broadcast-1",
    "currentVersion": 44,
    "currentControlVersion": 18
  }
}
```

错误码建议：

| code | 场景 |
| --- | --- |
| `bad_request` | 缺少必填字段或字段类型错误。 |
| `unauthorized` | 未登录。 |
| `forbidden` | 无权控制目标设备或群播。 |
| `follow_control_forbidden` | 跟随端尝试控制源头 timeline。 |
| `timeline_not_found` | timeline/broadcast/session 不存在。 |
| `stale_version` | 命令基于旧版本。新 timeline 协议可使用。 |
| `stale_client_seq` | 设备上报序号旧于服务端已接受序号。 |
| `conflict` | 群播或队列状态冲突，客户端应重新拉取状态。现有 broadcast v1 已使用该错误码。 |

兼容建议：

- v1 broadcast 继续返回 `conflict/currentVersion`。
- 新客户端同时识别 `conflict` 和 `stale_version`。
- 两者的客户端处理一致：丢弃本地待提交状态，拉取 `broadcast.status` 或等待最新 timeline。

## 11. 兼容与迁移

### Phase 1：服务端 additive 字段

服务端先在所有相关广播中增加：

- `timelineId`
- `version`
- `epoch`
- `queueRevision`
- `controlVersion`
- `serverUpdatedAtMs`
- `serverTimeMs`
- `updatedAt = serverUpdatedAtMs / 1000`

旧客户端继续读 `updatedAt`，新客户端开始读 `serverUpdatedAtMs/version/epoch/queueRevision/controlVersion`。

### Phase 2：客户端启用权威 timeline

客户端改为：

- 使用 `serverUpdatedAtMs` 推算进度。
- 按 `timelineId/version/epoch` 丢弃旧播放消息。
- 队列控制使用 `baseQueueRevision`，broadcast 控制使用 `baseControlVersion`；兼容旧 `baseVersion`。
- 如服务端显式下发 `followDelayMs`，按该值扣减；缺省按 `0` 处理。
- 跟随端的 `playback.update` 只作为反馈，不作为源头状态。

### Phase 3：服务端启用严格乱序保护

客户端稳定后，服务端要求新客户端上传 `clientInstanceId/clientSeq`。缺失这些字段的旧客户端可以继续兼容，但服务端应标记为 legacy，并保守处理其状态覆盖。

## 12. 验收用例

### 12.1 跟随端不能快于源头

给定：

- 源头上报 `positionMs = 60000`
- 服务端盖章 `serverUpdatedAtMs = 100000`
- 跟随端在 `serverNowMs = 101000` 收到
- `followDelayMs = 0`

期望：

```text
target = 60000 + 1000 - 0 = 61000
```

跟随端目标位置应落后源头约 `700ms`，不能是 `61000` 或更大。

### 12.2 旧版本不能覆盖新版本

消息顺序：

1. 收到 `version = 42, positionMs = 60000`
2. 收到 `version = 41, positionMs = 58000`

期望：

- 客户端和服务端都保留 `version = 42`。
- `version = 41` 被丢弃。

### 12.3 播放心跳不阻塞队列提交

给定：

- 当前 `version = 100`
- 当前 `queueRevision = 5`
- 控制台基于 `baseQueueRevision = 5` 编辑队列
- 编辑期间播放设备连续上报，使 `version` 变成 `105`

当控制台提交 `queue.session.sync(baseQueueRevision = 5)`。

期望：

- 如果队列未被其他人改过，提交成功。
- 不应因为 `version = 105` 而误判队列冲突。
- 成功后 `queueRevision = 6`。

### 12.4 切歌后旧进度不能覆盖新歌

消息顺序：

1. `epoch = 8, trackId = song-2, positionMs = 0`
2. 迟到消息 `epoch = 7, trackId = song-1, positionMs = 120000`

期望：

- 客户端继续显示 `song-2`。
- 旧 `song-1` 消息被丢弃。

### 12.5 跟随端反馈不覆盖源头

给定：

- 源头 `sourceClientId = phone-1`
- 跟随端 `sourceClientId = laptop-1, mode = follow, followSourceClientId = phone-1`

期望：

- `laptop-1` 的 `playback.update` 只能更新 `session:{laptopSession}:client:laptop-1`。
- 不得更新 `session:{phoneSession}:client:phone-1`。

### 12.6 跟随端不能控制源头

给定：

- `laptop-1` 正在 follow `phone-1`
- `followSourceClientId = phone-1`

当 `laptop-1` 发送：

```json
{
  "action": "player.seek",
  "targetClientId": "phone-1",
  "payload": {
    "positionMs": 90000
  }
}
```

期望：

- 服务端返回 `system.error`。
- `code = "follow_control_forbidden"` 或 `forbidden`。
- `phone-1` 的 timeline 不变。

### 12.7 群播 seek 版本递增

给定 broadcast `version = 10`。

当控制器发送 `broadcast.seek(positionMs = 90000)`。

期望服务端广播：

- `timelineId = broadcast:{broadcastId}`
- `version = 11`
- `controlVersion` 递增
- `epoch` 不变
- `serverUpdatedAtMs` 为服务端时间
- 所有参与者只按这条 timeline 同步

### 12.8 App 重启后 clientSeq 不误拒绝

给定：

- `phone-1` 上一实例 `clientInstanceId = A`，最后接受 `clientSeq = 200`
- App 重启后注册 `clientInstanceId = B`，首次上报 `clientSeq = 1`

期望：

- 服务端接受 `clientSeq = 1`。
- 服务端把 seq 窗口切换到 `phone-1 + B`。

### 12.9 服务端 follow 状态优先于客户端自报

给定：

- 服务端记录 `laptop-1` follow `phone-1`
- `laptop-1` 发送 `player.seek` 到 `phone-1`
- payload 没有带 `mode=follow`

期望：

- 服务端仍按 `FollowRelationship` 判定它是跟随端。
- 命令被拒绝。

## 13. 最小交付清单

服务端最小需要完成：

1. 所有出站 `playback.update` 由服务端写 `serverUpdatedAtMs/version/epoch`。
2. 所有出站 `queue.session.sync` 带 `timelineId/version/epoch/queueRevision/controlVersion`。
3. `updatedAt` 改为服务端时间，继续兼容秒单位。
4. `clientInstanceId/clientSeq` 支持实例级乱序保护。
5. 跟随关系由服务端保存，跟随端的 `playback.update(mode=follow)` 不覆盖源头 timeline。
6. broadcast timeline 由服务端维护 `version/epoch/queueRevision/controlVersion/serverUpdatedAtMs`。
7. `seek/play/pause` 不递增 `epoch`，切歌或媒体身份变化才递增 `epoch`。
8. 队列提交使用 `baseQueueRevision`；broadcast 控制使用 `baseControlVersion`，并兼容现有 `baseVersion`。
9. 客户端文档包含 `serverTimeMs` offset 估算公式和 `followDelayMs` 目标进度公式。
10. 跟随端不能控制源头设备；相关命令必须返回 `forbidden` 或 `follow_control_forbidden`。
11. 订阅快照和后续广播都必须带这些新字段。

完成以上 11 点后，Flutter 端就可以改为权威 timeline 同步，跟播和群播都会更稳定。
