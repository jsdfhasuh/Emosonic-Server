# Emo Realtime PlaybackContext v2 Strict Architecture Goal

> 目标：将 EmoSonic Server 的实时播放协议从旧 `sessionId` 混合模型，升级为严格的 `PlaybackContext v2` 架构。
> 新客户端全量适配后，服务端不再把 `sessionId` 作为播放主键，不再从 `sessionId` 推导 `playbackContextId`。

---

## 0. 修订后的执行边界

本计划采用“v2 严格、legacy 隔离”的迁移方式，避免第一阶段同时破坏旧客户端和新架构约束。

### 0.1 协议门控

服务端必须按协议路径区分行为：

```text
v2 路径：
  - 新 action：playback.context.* / queue.context.sync
  - 或 device.register 声明 capabilities.playbackContextV2 = true 的客户端发出的 shared action
  - 必须使用 playbackContextId / deviceSessionId
  - payload.sessionId 必须被拒绝，不能参与推导

legacy 路径：
  - 旧 action：session.* / queue.session.sync / queue.local.* / queue.ready.complete
  - 仍可使用 sessionId
  - 第一阶段不主动删除，保持旧客户端可用
  - 不允许被 v2 helper 复用为 fallback

context-compatible 路径：
  - shared action payload 显式带 playbackContextId 或 deviceSessionId
  - 但客户端尚未声明 capabilities.playbackContextV2 = true
  - 服务端按 PlaybackContext 字段处理，不从 sessionId 推导 playbackContextId
  - 为兼容旧 serializer echo，第一阶段不强制拒绝 payload.sessionId
  - 最终退场阶段收紧为只认 v2 capability
```

`playback.update` 是共享 action，因此必须按客户端能力和 payload 分支：

```text
strict v2 playback.update:
  client capabilities.playbackContextV2 = true
  -> strict v2 处理

context-compatible playback.update:
  client 未声明 playbackContextV2
  但 payload 明确包含 playbackContextId / deviceSessionId
  -> 使用 context 分支，不写旧表；不把 sessionId 当主键或 fallback

legacy playback.update:
  client 未声明 playbackContextV2
  且 payload 不包含 v2 字段
  -> 继续走 legacy sessionId 处理，直到最终退场阶段
```

`playbackContextV2` 当前不是既有能力位，必须新增常量：

```text
CAPABILITY_PLAYBACK_CONTEXT_V2 = "playbackContextV2"
```

并通过现有 `_client_supports()` 读取。第一阶段 v2 判定采用：

```text
is_strict_v2 = _client_supports(current_client, CAPABILITY_PLAYBACK_CONTEXT_V2)
is_context_payload = "playbackContextId" in payload or "deviceSessionId" in payload
```

### 0.2 Serializer 边界

v2 不仅不能从 `sessionId` 解析 ID，也不能在 v2 出站 payload 中继续输出 legacy alias。

必须拆分：

```text
legacy serializer:
  可以输出 sessionId / sourceClientId，服务旧客户端

v2 serializer:
  只输出 playbackContextId / deviceSessionId / authorityClientId
  不输出 sessionId
  不把 sourceClientId 当成 authorityClientId alias
```

当前代码中 `PlaybackContext` 和 `DevicePlaybackState` payload 会补 `sessionId`，这是 v2 迁移必须修正的点。

当前 alias 来源必须写准：

```text
_playback_context_payload:
  sessionId     <- record.playback_context_id
  sourceClientId <- record.authority_client_id

_device_playback_state_payload:
  sessionId     <- record.device_session_id
  sourceClientId <- record.owner_client_id
```

这两处没有数据库 `session_id` 列，`sessionId` 只是 legacy serializer alias。v2 应新增独立 serializer 函数，例如：

```text
serializePlaybackContextV2(...)
serializeDevicePlaybackStateV2(...)
```

不要在现有 legacy serializer 里用布尔参数混出两种协议形态。

### 0.3 状态层边界

第一阶段必须先把状态层 API 拆开：

```text
create_playback_context(...)          只能显式创建
update_existing_playback_context(...) 只能更新已存在 context
record_device_playback_state(...)     只记录设备反馈
```

不能继续让 `update_playback_context_queue()` 或 `apply_authority_playback_update()` 隐式创建 `PlaybackContext`。否则 dispatcher 即使严格校验，也会留下内部旁路。

`get_playback_context(...)` 当前已经存在，不需要重复新增。v2 读取路径应该拆成：

```text
get_existing_playback_context(...):
  可以从内存读取
  可以从 EmoPlaybackContext 冷恢复
  不允许从 EmoSessionQueue / legacy queue fallback 创建 context
```

当前 `_get_or_restore_playback_context()` 里从 `getQueueState(playback_context_id)` 恢复 context 的逻辑只能留给 legacy/migration 路径，不能被 v2 action 调用。

### 0.4 第一阶段范围

第一阶段只覆盖普通 PlaybackContext 主链路：

```text
device.register v2
playback.context.create/status/subscribe/unsubscribe
queue.context.sync
playback.update v2 strict
player.pause / player.play / player.seek / player.next / player.prev / queue.playItem 的 v2 context 路由
handoff 的 v2 strict 校验与生命周期补强
```

跟播、群播、local queue、旧 Web 播放器适配放到后续阶段。第一阶段只要求这些旧路径不污染 v2 路径，不要求一次性重构完成。

---

## 1. 背景

当前服务端已经引入了以下新模型：

```text
EmoPlaybackContext
EmoDevicePlaybackState
EmoPlaybackHandoff
```

并且已经具备基础 handoff 能力：

```text
playback.handoff.start
playback.handoff.cancel
playback.handoff.complete
authorityClientId 转移
DevicePlaybackState 反馈
```

但是当前代码仍然处于新旧协议混合状态：

```text
session.subscribe / session.unsubscribe 仍存在
queue.session.sync 仍存在
payload.sessionId 仍被解析
sourceSessionId / followSessionId 仍用于跟播
broadcastId 仍是独立播放模型
playback.update 仍可能写 legacy playback state
queue.session.sync 仍会同时写旧 queue 和新 PlaybackContext
```

本次 goal 的目标不是继续小修 handoff，而是把实时播放模型统一到 `PlaybackContext v2`。

---

## 2. 核心设计原则

### 2.1 新架构只保留四类核心 ID

```text
clientId              当前 WebSocket 客户端
deviceSessionId       设备身份 / 设备房间
playbackContextId     播放任务 / 播放上下文
authorityClientId     当前拥有播放权的客户端
```

### 2.2 sessionId 不再作为 v2 播放协议字段

v2 路径中：

```text
sessionId 不再作为 queue / playback / follow / remote control 的主键
sessionId 不再从客户端新协议 payload 中读取
sessionId 不再被 fallback 成 playbackContextId
sessionId 只允许作为旧数据迁移字段、legacy action 字段或 legacy serializer 字段存在
```

v2 要让 `playbackContextId` 与 `deviceSessionId` 真正分离，前提是客户端确实下发不同的两个 ID。旧 resolver 会把缺失的 v2 ID 兜底到 `sessionId` / `_device_session_id(current_client)`，因此第一阶段客户端接入清单必须明确：

```text
deviceSessionId 由 device.register 建立设备身份
playbackContextId 由 playback.context.create 建立播放任务
任何仍指望 playbackContextId == deviceSessionId 隐式统一的调用方，都应在 strict v2 下得到 bad_request
```

### 2.3 PlaybackContext 表示播放任务，不表示设备

正确关系：

```text
User
 └── PlaybackContext
      ├── playbackContextId
      ├── authorityClientId
      ├── queueSongIds
      ├── currentIndex
      ├── trackId
      ├── state
      ├── positionMs
      ├── queueRevision
      ├── controlVersion
      └── DevicePlaybackState[]
           ├── phone-1
           └── pc-1
```

一个 `PlaybackContext` 可以被多个设备参与。handoff 只是转移 `authorityClientId`，不改变 `playbackContextId`。

### 2.4 PlaybackContext 与旧 sessionId 的角色边界

`PlaybackContext` 和旧 `sessionId` 有相似之处，但不能完全等同。

旧 `sessionId` 同时承担了多种职责：

```text
设备房间 ID
播放队列 ID
播放状态 ID
跟播订阅 ID
远控目标 ID
```

v2 后这些职责被拆开：

```text
deviceSessionId      接管旧 sessionId 的设备身份 / 设备房间部分
playbackContextId    接管旧 sessionId 的播放任务 / 播放队列 / 播放状态部分
authorityClientId    表示当前真正负责播放的 client
```

因此可以这样理解：

```text
PlaybackContext = 旧 sessionId 中“播放任务”那一部分
DeviceSession   = 旧 sessionId 中“设备身份”那一部分
```

### 2.5 PlaybackContext 应存放的核心键值

`PlaybackContext` 表示一套共享播放任务，所以它应该保存“多设备之间需要一致”的状态。

#### 必须字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `playbackContextId` | string | 播放任务 ID，v2 播放主键 |
| `userName` | string | 所属用户 |
| `contextType` | string | `normal` / `broadcast`，默认 `normal` |
| `authorityClientId` | string | 当前拥有播放权的 client |
| `originClientId` | string | 最近一次创建 / 控制 / handoff 来源 client |
| `queueSongIds` | string[] | 共享播放队列 |
| `currentIndex` | int | 当前播放队列下标 |
| `trackId` | string | 当前播放歌曲 ID，通常等于 `queueSongIds[currentIndex]` |
| `state` | string | `playing` / `paused` / `stopped` / `buffering` / `ended` |
| `positionMs` | int | 当前播放进度，毫秒 |
| `queueRevision` | int | 队列版本号 |
| `controlVersion` | int | 控制版本号，用于远控和 handoff 冲突检测 |
| `version` | int | PlaybackContext 整体版本号 |
| `epoch` | int | 播放媒体切换代号，用于区分同一首歌内 seek 与切歌 |
| `serverUpdatedAtMs` | int | 服务端更新时间，毫秒 |
| `updatedAt` | number | 服务端更新时间，秒 |

#### 推荐字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `createdByClientId` | string | 创建该 context 的 client |
| `repeatMode` | string | `none` / `one` / `all` |
| `shuffle` | boolean | 是否随机播放 |
| `playbackRate` | number | 播放速度，音乐场景默认 `1.0` |
| `timelineId` | string | 时间线 ID，可由 `playbackContextId` 推导 |

#### 谨慎字段

| 字段 | 建议归属 | 说明 |
| --- | --- | --- |
| `volume` | 优先放 `DevicePlaybackState` | 设备真实音量通常不应该作为跨设备共享状态 |
| `muted` | 优先放 `DevicePlaybackState` | 静音状态通常是设备级状态 |
| `outputDeviceId` | `DevicePlaybackState` | 输出设备属于具体播放设备 |
| `audioDeviceName` | `DevicePlaybackState` | 音频输出设备名称属于具体播放设备 |
| `logicalVolume` | 可选放 `PlaybackContext` | 只有产品明确需要“跨设备统一应用内音量”时才建议加入 |

### 2.6 队列与音量的归属原则

`PlaybackContext` 必须有队列，因为它代表一套播放任务：

```text
queueSongIds
currentIndex
trackId
```

队列属于共享播放任务，不属于某一台设备。

音量则不建议直接作为 `PlaybackContext` 的强共享字段。真实设备音量更适合放在 `DevicePlaybackState`：

```text
手机音量 40%
电脑音量 80%
音箱音量 20%
```

handoff 从手机切到电脑时，通常不应该强制把手机的系统音量同步到电脑。因此 v2 推荐：

```text
PlaybackContext.volume        可选，最好不要作为强依赖
PlaybackContext.logicalVolume 可选，仅用于应用内统一音量
DevicePlaybackState.volume    推荐，用于记录设备真实音量
```

当前数据库里 `EmoPlaybackContext.volume` 和 `EmoDevicePlaybackState.volume` 都已存在。因此这里是行为迁移：v2 不再把设备真实音量写进 `PlaybackContext.volume`；只有产品明确需要跨设备统一应用内音量时，才另行设计 `logicalVolume`。

### 2.7 PlaybackContext 推荐结构

```json
{
  "playbackContextId": "playback:alice:main",
  "userName": "alice",
  "contextType": "normal",

  "authorityClientId": "phone-1",
  "originClientId": "phone-1",
  "createdByClientId": "phone-1",

  "queueSongIds": ["song-1", "song-2"],
  "currentIndex": 0,
  "trackId": "song-1",

  "state": "playing",
  "positionMs": 32000,

  "repeatMode": "none",
  "shuffle": false,
  "playbackRate": 1.0,

  "queueRevision": 1,
  "controlVersion": 1,
  "version": 1,
  "epoch": 1,

  "serverUpdatedAtMs": 1780000000000,
  "updatedAt": 1780000000
}
```

### 2.8 DevicePlaybackState 推荐结构

`DevicePlaybackState` 记录每台设备在某个 `PlaybackContext` 下的真实执行状态。

```json
{
  "playbackContextId": "playback:alice:main",
  "clientId": "phone-1",
  "deviceSessionId": "device:phone-1",

  "state": "playing",
  "trackId": "song-1",
  "positionMs": 32100,
  "volume": 40,
  "muted": false,

  "isAuthority": true,
  "mode": "normal",

  "serverUpdatedAtMs": 1780000000100,
  "updatedAt": 1780000000.1
}
```

字段分工必须保持清晰：

```text
PlaybackContext      存共享播放任务
DevicePlaybackState  存每台设备自己的实际播放状态
```

handoff 后：

```text
playbackContextId 不变
queueSongIds 不变
currentIndex 不变
trackId 不变
state 基本不变
positionMs 继续推进
authorityClientId 改为 targetClientId
```


### 2.9 代码审查后的角色对照：sessionId vs PlaybackContext

基于当前服务端代码，旧 `sessionId` 并不是一个单一概念，而是一个被复用过多的混合 ID。

#### 2.9.1 当前代码中 sessionId 实际承担的职责

当前代码里的 `sessionId` 至少承担了以下职责：

```text
1. 设备房间 / 设备分组
2. 共享队列主键
3. 设备本地队列主键的一部分
4. 设备播放状态主键的一部分
5. 订阅目标
6. 远程控制目标的一部分
7. 跟播目标
```

##### 设备房间 / 设备分组

当前 `device.register` 中，设备注册仍然允许从 `sessionId` 兜底：

```python
device_session_id = payload.get("deviceSessionId") or payload.get("sessionId") or client_id
```

并且会把同一个值同时写入：

```python
"deviceSessionId": device_session_id,
"sessionId": device_session_id,
```

这说明当前代码里的 `sessionId` 很多时候实际表示的是“设备所在房间”或“设备会话”。

##### 共享队列主键

当前运行时状态中有：

```text
_queues: sessionId -> shared room queue snapshot
```

对应数据库旧表也有：

```text
EmoSessionQueue.session_id
```

这说明旧 `sessionId` 曾经负责表示：

```text
这套共享队列属于哪个播放房间
```

##### 设备本地队列主键

当前运行时状态中有：

```text
_local_queues: (sessionId, clientId) -> device-local queue snapshot
```

对应旧表：

```text
EmoLocalQueue.session_id
EmoLocalQueue.owner_client_id
```

这说明旧模型下，设备本地队列是依附在 `sessionId + clientId` 上的。

##### 设备播放状态主键

当前运行时状态中有：

```text
_playback_states: (sessionId, clientId) -> device playback snapshot
```

对应旧表：

```text
EmoPlaybackState.session_id
EmoPlaybackState.owner_client_id
```

这说明旧模型下，播放状态不是独立的播放任务，而是：

```text
某个 client 在某个 sessionId 下的播放状态
```

##### 订阅目标

当前仍然存在：

```text
session.subscribe
session.unsubscribe
```

运行时状态中也有：

```text
_session_subscriptions: sid -> subscribed sessionIds
```

这说明旧 `sessionId` 也是被动观察者 / 控制端订阅状态变化的目标。

##### 远程控制目标

当前远程控制仍然依赖：

```text
targetClientId + sessionId
```

典型逻辑是：

```python
session_id = payload.get("sessionId") or target_client.get("sessionId")
```

然后再根据：

```text
state.get_playback_state(session_id, target_client_id)
state.get_local_queue(session_id, target_client_id)
state.get_queue(session_id)
```

找到播放状态和队列。

这说明旧远控需要调用方同时知道：

```text
要控制哪台设备 targetClientId
这台设备对应哪个 sessionId
```

##### 跟播目标

当前 `follow.start` 仍然依赖：

```text
sourceClientId
sourceSessionId
followSessionId
session.subscribe
```

跟播关系绑定的是：

```text
followerClientId -> sourceClientId + sourceSessionId
```

这说明旧模型下，“跟播”跟随的是某台设备的 session，而不是一套独立的播放任务。

#### 2.9.2 当前代码中 PlaybackContext 已经承担的职责

当前代码里的 `PlaybackContext` 已经在接管旧 `sessionId` 中“播放任务”相关的职责。

运行时状态中已有：

```text
_playback_contexts: playbackContextId -> server-owned playback context
_device_playback_states: (playbackContextId, clientId) -> device feedback state
_playback_context_subscriptions: sid -> subscribed playbackContextIds
```

这说明新模型已经开始把“共享播放任务”和“每台设备的实际反馈状态”拆开。

##### PlaybackContext 负责共享播放任务

`PlaybackContext` 当前保存的核心字段包括：

```text
playbackContextId
userName
authorityClientId
originClientId
timelineId
queueSongIds
currentIndex
trackId
state
positionMs
volume
queueRevision
controlVersion
version
epoch
serverUpdatedAtMs
updatedAt
```

这些字段说明它已经承担：

```text
共享队列
当前歌曲
播放状态
播放进度
播放权
冲突检测版本
媒体切换代号
```

##### PlaybackContext 负责主队列

`update_playback_context_queue()` 会更新：

```text
queueSongIds
currentIndex
trackId
positionMs
queueRevision
controlVersion
version
epoch
```

因此 v2 中主队列应该正式从旧 `_queues[sessionId]` 迁移到：

```text
_playback_contexts[playbackContextId]
```

##### PlaybackContext 负责权威播放状态

`apply_authority_playback_update()` 里只有当前 `authorityClientId` 可以更新主 `PlaybackContext`。

非 authority 设备上报时，只能作为：

```text
DevicePlaybackState
```

不能覆盖共享播放状态。

##### PlaybackContext 负责 handoff 承载

`transfer_playback_authority()` 的核心逻辑是：

```text
在同一个 playbackContextId 下，
把 authorityClientId 从 sourceClientId 改成 targetClientId
```

因此 handoff 后：

```text
playbackContextId 不变
authorityClientId 改变
queueSongIds 不变
currentIndex 不变
trackId 不变
state 基本不变
positionMs 继续推进
```

这说明 `PlaybackContext` 是无痕切换的承载对象，而不是设备对象。

#### 2.9.3 两者的本质差别

旧 `sessionId` 和新 `PlaybackContext` 有相似之处，但不能简单等价。

| 对比项 | 旧 sessionId | 新 PlaybackContext |
| --- | --- | --- |
| 核心含义 | 混合 ID | 播放任务 ID |
| 设备身份 | 承担过 | 不承担 |
| 设备房间 | 承担过 | 不承担 |
| 共享队列 | 承担过 | 承担 |
| 当前歌曲 | 间接承担 | 直接承担 |
| 权威播放状态 | 依赖 `sessionId + clientId` | 由 `authorityClientId` 决定 |
| 设备真实状态 | `sessionId + clientId` | `playbackContextId + clientId` |
| 远程控制目标 | `targetClientId + sessionId` | `playbackContextId -> authorityClientId` |
| 跟播目标 | `sourceClientId + sourceSessionId` | `sourcePlaybackContextId` |
| handoff 承载 | 不清晰 | 清晰 |
| 跨设备稳定性 | 弱，设备绑定感强 | 强，context 不随设备变化 |

#### 2.9.4 v2 迁移不是简单字段改名

v2 不能理解成：

```text
把 sessionId 全局替换成 playbackContextId
```

正确迁移应该是拆分旧 `sessionId` 的职责：

```text
旧 sessionId 的设备房间职责
  -> deviceSessionId

旧 sessionId 的播放任务职责
  -> playbackContextId

旧 sessionId + clientId 的设备播放状态
  -> playbackContextId + clientId 的 DevicePlaybackState

旧 session.subscribe
  -> playback.context.subscribe

旧 targetClientId + sessionId 远程控制
  -> playbackContextId + authorityClientId

旧 sourceClientId + sourceSessionId 跟播
  -> sourcePlaybackContextId
```

最终原则：

```text
PlaybackContext 不是 sessionId 的简单替代品。
PlaybackContext 是把旧 sessionId 里“播放任务”那部分抽出来，
做成独立、可跨设备、可 handoff 的播放上下文。
```


---

## 3. 当前代码审查结论

### 3.1 `ws.py` 仍然保留旧 action

当前仍存在：

```text
SESSION_ACTIONS = {"session.subscribe", "session.unsubscribe"}
queue.session.sync
queue.ready.complete
```

这些 action 应在 v2 中替换为：

```text
playback.context.subscribe
playback.context.unsubscribe
queue.context.sync
```

### 3.2 ID 解析仍然混用 sessionId

当前 `_resolve_playback_context_id()` 逻辑大致是：

```python
payload.get("playbackContextId")
or payload.get("sessionId")
or _device_session_id(current_client)
```

这不符合 v2。v2 中必须严格要求：

```python
payload.get("playbackContextId")
```

### 3.3 device.register 仍然从 sessionId 兜底

当前注册设备时仍允许：

```python
payload.get("deviceSessionId") or payload.get("sessionId") or client_id
```

v2 注册路径应改成：

```text
capabilities.playbackContextV2 = true 时 deviceSessionId 必填
不再从 sessionId 兜底
不再把 sessionId 写回 v2 client info
```

legacy 注册路径可以短期保留 `sessionId` 兜底，但必须和 v2 helper 分开。

### 3.4 playback.update 仍然混合旧状态

当前 `playback.update` 已经写入：

```text
EmoPlaybackContext
EmoDevicePlaybackState
```

但同时仍写入：

```text
legacy playback state
savePlaybackState
_broadcast_playback_state
```

v2 分支应移除旧写入路径；legacy 分支第一阶段保留。

### 3.5 queue.session.sync 已经在做 context 更新

当前 `queue.session.sync` 内部已经调用 `update_playback_context_queue()`，说明它实际上已经承担 `queue.context.sync` 的职责。

v2 后应该新增独立协议，并把旧分支隔离为 legacy-only：

```text
legacy: queue.session.sync
v2:     queue.context.sync
```

### 3.6 远程控制仍以 sessionId / targetClientId 为核心

当前远控路径仍然依赖：

```text
targetClientId
sessionId
state.get_playback_state(session_id, target_client_id)
state.get_queue(session_id)
```

v2 后远控应该只传 `playbackContextId`，服务端通过 `authorityClientId` 找到真正播放设备。

### 3.7 跟播仍基于 sourceSessionId

当前 `follow.start` 仍然使用：

```text
sourceClientId
sourceSessionId
followSessionId
session.subscribe
```

v2 后跟播应该改成：

```text
sourcePlaybackContextId
playback.context.subscribe
```

### 3.8 群播仍是独立 Broadcast 模型

当前群播仍基于：

```text
broadcastId
state._broadcasts
state._broadcast_participants
state._broadcast_playback_states
```

v2 最终目标是将群播共享播放状态挂到 `PlaybackContext`，参与者状态挂到 `DevicePlaybackState`。


### 3.9 当前代码处于新旧职责并存状态

当前不是纯旧模型，也不是纯 v2 模型，而是新旧职责同时存在：

```text
旧 sessionId 路径仍负责 queue / local queue / playback state / subscribe / follow / control
PlaybackContext 路径已经开始负责 queue / authority playback state / device feedback / handoff
```

典型例子：

```text
queue.session.sync 既会更新 PlaybackContext，又会写 legacy queue
playback.update 既会写 PlaybackContext / DevicePlaybackState，又会写 legacy PlaybackState
```

因此 v2 重构的重点不是“把字段名换掉”，而是把运行时职责从旧 `sessionId` 模型迁移到新 `PlaybackContext` 模型。

### 3.10 v2 serializer 仍然输出 legacy alias

当前持久化和状态层在组装 `PlaybackContext` / `DevicePlaybackState` payload 时仍会补：

```text
sessionId
sourceClientId
```

这对 legacy 客户端有价值，但不符合 strict v2。v2 必须新增独立 serializer：

```text
PlaybackContext v2 payload:
  playbackContextId
  authorityClientId
  originClientId
  queueSongIds
  currentIndex
  trackId
  state
  positionMs
  queueRevision
  controlVersion
  version
  epoch

DevicePlaybackState v2 payload:
  playbackContextId
  deviceSessionId
  clientId
  state
  trackId
  positionMs
  volume
  muted
  isAuthority
  mode
```

legacy alias 只允许出现在 legacy serializer 中。

### 3.11 代码核对后的补充事实

以下事实会影响实现顺序：

```text
1. playbackContextV2 capability 当前不存在，需要新增 CAPABILITY_PLAYBACK_CONTEXT_V2。
2. get_playback_context 已存在，不需要重复新增。
3. _playback_context_subscriptions 目前只是死容器，只在 init 和断连清理中出现；订阅 API 要从零搭建。
4. queue.context.sync 当前完全不存在，必须新建 handler，不能复用 queue.session.sync。
5. queue.session.sync 当前无条件写 legacy queue / EmoSessionQueue。
6. broadcast action 清单还包括 broadcast.queue.sync / broadcast.playItem。
7. EmoDevicePlaybackState.mode 和 is_authority 已存在；不要为它们写新增列 migration。
8. EmoPlaybackContext.volume 和 EmoDevicePlaybackState.volume 都已存在；音量归属是行为迁移，不是加列。
```

---

## 4. v2 协议目标

### 4.0 Dispatcher 边界

第一阶段不要直接把旧 action 从 dispatcher 删除。正确做法是显式分流：

```text
V2_ACTIONS:
  playback.context.create
  playback.context.status
  playback.context.subscribe
  playback.context.unsubscribe
  playback.context.close
  queue.context.sync

LEGACY_ACTIONS:
  session.subscribe
  session.unsubscribe
  queue.session.sync
  queue.local.get
  queue.local.set
  queue.ready.complete

SHARED_ACTIONS:
  device.register
  device.list
  playback.update
  playback.ready
  player.*
  queue.playItem
  playback.handoff.*
  follow.*
  broadcast.*
```

`SHARED_ACTIONS` 必须根据客户端 capability、payload 字段和 action 语义选择 v2 或 legacy 分支，不能在同一段逻辑里混用 fallback。

### 4.1 最终 v2 action 列表

```text
device.register
device.list

playback.context.create
playback.context.status
playback.context.subscribe
playback.context.unsubscribe
playback.context.close

queue.context.sync

playback.update
playback.ready

player.play
player.pause
player.seek
player.next
player.prev
queue.playItem

playback.handoff.start
playback.handoff.cancel
playback.handoff.complete
playback.handoff.release

follow.start
follow.stop

broadcast.start
broadcast.queue.sync
broadcast.playItem
broadcast.play
broadcast.pause
broadcast.seek
broadcast.stop
broadcast.status
```

其中 `follow.*` 和 `broadcast.*` 是最终目标，不属于第一阶段强制落地范围。

### 4.2 所有 v2 播放相关 action 必须带 playbackContextId

```json
{
  "action": "player.pause",
  "payload": {
    "playbackContextId": "playback:alice:main",
    "baseControlVersion": 8,
    "positionMs": 30200
  }
}
```

### 4.3 所有 v2 设备相关 action 必须带 deviceSessionId

```json
{
  "action": "device.register",
  "payload": {
    "clientId": "phone-1",
    "deviceSessionId": "device:phone-1",
    "deviceName": "Phone",
    "roles": ["player"],
    "capabilities": {
      "playbackContextV2": true,
      "playbackPrepare": true,
      "effectiveAtPlayback": true
    }
  }
}
```

---

## 5. Task 1：严格化 ID 解析

### 当前问题

`sessionId` 仍参与 `deviceSessionId` 和 `playbackContextId` 的推导。

### 目标

```text
v2 deviceSessionId 只来自 payload.deviceSessionId 或 current_client.deviceSessionId
v2 playbackContextId 只来自 payload.playbackContextId
legacy sessionId 解析函数保留给 legacy action
```

### 建议修改

```python
def _reject_session_id_for_strict_v2(payload, strict_v2):
    if strict_v2 and "sessionId" in payload:
        raise ValueError("sessionId is not allowed in PlaybackContext v2 payload")


def _resolve_v2_device_session_id(payload, current_client, strict_v2=False):
    _reject_session_id_for_strict_v2(payload, strict_v2)
    return payload.get("deviceSessionId") or _device_session_id(current_client)


def _resolve_v2_playback_context_id(payload, strict_v2=False):
    _reject_session_id_for_strict_v2(payload, strict_v2)
    return payload.get("playbackContextId")
```

旧 `_resolve_device_session_id()` / `_resolve_playback_context_id()` 不要直接复用到 v2；可以改名为 `_resolve_legacy_*`，仅 legacy action 调用。

`strict_v2=True` 适用于新 v2 action 和声明 `playbackContextV2` 的客户端。仅因 payload 带 `playbackContextId` / `deviceSessionId` 进入 context-compatible 分支时，第一阶段可以容忍 echo 回来的 `sessionId`，但不得使用它推导 `playbackContextId`。

### 验收标准

```text
v2 playback.update 不传 playbackContextId 返回 bad_request
queue.context.sync 不传 playbackContextId 返回 bad_request
v2 player.pause 不传 playbackContextId 返回 bad_request
strict v2 payload.sessionId 返回 bad_request
context-compatible payload.sessionId 不参与主键推导
strict v2 下缺 playbackContextId 时不能 fallback 到 deviceSessionId
legacy queue.session.sync / session.subscribe 仍可按旧协议工作
```

---

## 6. Task 2：device.register v2 化

### 当前问题

设备注册仍然允许 `sessionId` 兜底。

此外，`playbackContextV2` 当前不是既有能力位，需要新增常量并通过现有 capability 机制读取。

### 目标

```text
v2 client deviceSessionId 必填
v2 client info 不再写入 sessionId
device.list 返回 deviceSessionId
legacy client 可以继续返回 sessionId，直到 legacy UI 改造完成
```

### v2 请求示例

```json
{
  "action": "device.register",
  "payload": {
    "clientId": "phone-1",
    "deviceSessionId": "device:phone-1",
    "deviceName": "Pixel Phone",
    "roles": ["player"],
    "capabilities": {
      "playbackContextV2": true,
      "playbackPrepare": true,
      "effectiveAtPlayback": true
    }
  }
}
```

### 验收标准

```text
v2 device.register 缺 deviceSessionId 返回 bad_request
v2 device.register 传 sessionId 返回 bad_request
legacy device.register 仍兼容 sessionId / clientId 兜底
v2 device.list 不再把 sessionId 当主要字段展示
```

---

## 7. Task 3：新增 playback.context.create

### 目标

PlaybackContext 必须由明确动作创建，不再由 `playback.update` 隐式创建。

### 请求示例

```json
{
  "action": "playback.context.create",
  "payload": {
    "playbackContextId": "playback:alice:main",
    "deviceSessionId": "device:phone-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "positionMs": 0,
    "state": "playing"
  }
}
```

### 创建结果

```json
{
  "playbackContextId": "playback:alice:main",
  "authorityClientId": "phone-1",
  "queueSongIds": ["song-1", "song-2"],
  "currentIndex": 0,
  "trackId": "song-1",
  "state": "playing",
  "positionMs": 0,
  "queueRevision": 1,
  "controlVersion": 1,
  "version": 1,
  "epoch": 1
}
```

### 服务端规则

```text
当前 client 自动成为 authorityClientId
playbackContextId 必须唯一
userName 必须绑定当前认证用户
queueSongIds / currentIndex / positionMs 必须校验
```

### 状态层 / 持久层规则

必须新增显式创建 API：

```text
state.create_playback_context(...)
store.createPlaybackContextState(...)
```

`state.get_playback_context(...)` 当前已经存在，不要重复新增。

并且与更新 API 分开：

```text
state.update_existing_playback_context_queue(...)
state.apply_existing_authority_playback_update(...)
store.savePlaybackContextState(...) 只允许更新已存在记录或由调用方明确传 create=True
```

`playback.context.create` 是唯一允许创建普通 v2 context 的实时入口。`queue.context.sync` 和 v2 `playback.update` 只能读取并更新已存在 context。

v2 读取 existing context 时：

```text
允许：从内存读取
允许：从 EmoPlaybackContext 冷恢复
禁止：从 EmoSessionQueue / getQueueState fallback 创建 PlaybackContext
```

### 验收标准

```text
本地播放前必须先 create context
create 后 authorityClientId = 当前 clientId
重复 create 同一个 playbackContextId 返回 conflict 或 idempotent 结果
cross-user playbackContextId 不可访问
create 是唯一能新增普通 v2 PlaybackContext 的实时入口
v2 context lookup 不从 legacy queue fallback
```

---

## 8. Task 4：queue.session.sync 改为 queue.context.sync

### 目标

第一阶段不是直接删除 legacy action，而是新增独立 v2 action：

```text
queue.context.sync
```

并把旧 action 标记为 legacy-only：

```text
queue.session.sync
```

### 请求示例

```json
{
  "action": "queue.context.sync",
  "payload": {
    "playbackContextId": "playback:alice:main",
    "deviceSessionId": "device:phone-1",
    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 1,
    "positionMs": 0,
    "baseQueueRevision": 1
  }
}
```

### 服务端规则

```text
PlaybackContext 必须存在
只有 authorityClientId 可以修改主队列
非 authority 修改队列返回 authority_mismatch
成功后 queueRevision +1
成功后 controlVersion +1
成功后广播 queue.context.sync
成功后只写 EmoPlaybackContext，不写 EmoSessionQueue
```

`queue.context.sync` 必须是独立新 handler，只调用：

```text
state.update_existing_playback_context_queue(...)
_save_playback_context_snapshot(...)
_broadcast_playback_context_queue(...)
```

不得调用：

```text
state.update_queue(...)
saveQueueState(...)
_broadcast_queue(...)
```

不能通过给 `queue.session.sync` 加分支来冒充 v2；当前 `queue.session.sync` 无条件写旧 queue / EmoSessionQueue。

### 验收标准

```text
queue.context.sync 走 v2 dispatcher
queue.session.sync 仍在 legacy dispatcher，但不能调用 v2 helper
queue.context.sync 不创建 context，只更新已存在 context
queue.context.sync 缺 playbackContextId 返回 bad_request
queue.context.sync 非 authority 返回 authority_mismatch
queue.context.sync 不新增 EmoSessionQueue 记录
```

---

## 9. Task 5：playback.update 严格化

### 目标

```text
v2 playback.update 不创建 PlaybackContext
v2 playback.update 不写 legacy EmoPlaybackState
v2 playback.update 只写 EmoPlaybackContext 或 EmoDevicePlaybackState
legacy playback.update 暂时保留旧行为
```

### v2 分支判定

```text
如果 current_client.capabilities.playbackContextV2 = true：
  使用 strict v2 分支，payload.sessionId 返回 bad_request
否则如果 payload 包含 playbackContextId / deviceSessionId：
  使用 context-compatible 分支，不从 sessionId 推导主键，不写旧表
否则：
  使用 legacy 分支
```

### 权限规则

```text
current_client == authorityClientId:
    更新 PlaybackContext
    记录 DevicePlaybackState(isAuthority=true)

current_client != authorityClientId:
    只记录 DevicePlaybackState(isAuthority=false)
    ack deviceFeedback=true
    不允许覆盖 PlaybackContext
```

### 需要移除的 legacy 路径

```text
state.update_playback_state(...)
savePlaybackState(...)
_broadcast_playback_state(...)
```

这些只从 v2 分支移除；legacy 分支在第一阶段可以继续调用。

当前 handler 中这三处是无条件执行的，v2 改造时必须用分支包住：

```text
state.update_playback_state(...)    # 当前普通路径无条件调用
savePlaybackState(...)              # 当前普通路径无条件调用
_broadcast_playback_state(...)      # 当前普通路径无条件调用
```

### 保留的新路径

```text
state.apply_authority_playback_update(...)
state.record_device_playback_state(...)
savePlaybackContextState(...)
saveDevicePlaybackState(...)
_broadcast_playback_context_state(...)
```

### 验收标准

```text
v2 playback.update 缺 playbackContextId 返回 bad_request
strict v2 playback.update 带 sessionId 返回 bad_request
context-compatible playback.update 带 sessionId 时不使用它推导 context
v2 playback.update context 不存在返回 not_found
authority update 可以更新 state / trackId / positionMs
non-authority update 只能 deviceFeedback
source late update after handoff 不能覆盖 target
legacy playback.update 不因 v2 改造回归
v2/context-compatible playback.update 不写 EmoPlaybackState
```

---

## 10. Task 6：远程控制改为 PlaybackContext 驱动

### 当前旧模式

```text
targetClientId + sessionId
```

### v2 新模式

```text
playbackContextId
```

第一阶段新增 v2 控制分支，不直接删除 legacy `targetClientId + sessionId` 分支。客户端声明 `playbackContextV2` 时走 strict v2 分支；未声明 capability 但 payload 带 `playbackContextId` 时走 context-compatible 分支。

### 请求示例

```json
{
  "action": "player.pause",
  "payload": {
    "playbackContextId": "playback:alice:main",
    "baseControlVersion": 8,
    "positionMs": 30200
  }
}
```

### 服务端流程

```text
1. 读取 PlaybackContext
2. 验证 userName
3. 验证 baseControlVersion
4. 找到 authorityClientId
5. 查找 authority 设备 sid
6. authority 离线则返回 authority_offline
7. 计算下一条 controlVersion
8. 根据 action 类型乐观更新 PlaybackContext 和 controlVersion
9. 持久化并广播更新后的 PlaybackContext
10. 将 command 发给 authority 设备；后续 authority playback.update 校正真实设备状态
```

需要明确两类 action 的更新时机：

```text
立即可落库的控制：
  pause / seek 同曲进度
  服务端可先更新 controlVersion 和 positionMs

乐观推进、设备反馈校正的控制：
  play / next / prev / queue.playItem
  服务端立即推进 state / currentIndex / trackId / positionMs
  authority 回 playback.update 后校正最终设备状态
```

对播放和切曲控制，服务端先写入可确定的目标状态，避免连续控制在 authority 回声前重复基于旧索引计算：

```text
player.play / player.next / player.prev / queue.playItem:
  立即写 currentIndex / trackId / state / positionMs
  推进 controlVersion / version
  曲目身份变化时推进 queueRevision / epoch
  command payload 带目标 queueIndex / trackId / controlVersion
  authority playback.update 作为真实反馈，可覆盖或校正乐观状态
```

### 需要改造的函数

```text
_handle_server_mediated_control      # player.pause / player.seek 内联分支
_build_source_control_commit_payload
_build_seek_media_change_commit_payload
_validate_source_base_control_version
_current_source_control_version
```

`_resolve_control_target` 只负责解析目标 client/sid，不负责推导 sessionId；v2 可以复用它来定位 authority 设备，但真正需要移除 sessionId 兜底的是上面列出的函数和内联分支。

### 验收标准

```text
v2 player.pause 不需要 targetClientId
v2 player.seek 不需要 sessionId
v2 player.next/prev 从 PlaybackContext.queueSongIds 计算
v2 queue.playItem 改为 context queue index
authority offline 返回 authority_offline
controlVersion 冲突返回 conflict
legacy targetClientId + sessionId 控制路径不回归
```

---

## 11. Task 7：playback.context.subscribe / status

### 目标

最终退场阶段废弃，第一阶段保留为 legacy-only：

```text
session.subscribe
session.unsubscribe
```

新增：

```text
playback.context.subscribe
playback.context.unsubscribe
playback.context.status
```

### 请求示例

```json
{
  "action": "playback.context.subscribe",
  "payload": {
    "playbackContextId": "playback:alice:main"
  }
}
```

### status 返回示例

```json
{
  "playbackContext": {
    "playbackContextId": "playback:alice:main",
    "authorityClientId": "phone-1",
    "queueSongIds": ["song-1"],
    "currentIndex": 0,
    "trackId": "song-1",
    "state": "playing",
    "positionMs": 30000,
    "queueRevision": 1,
    "controlVersion": 1,
    "version": 1,
    "epoch": 1
  },
  "deviceStates": []
}
```

### 服务端要改

```text
SESSION_ACTIONS 第一阶段保留为 legacy-only
state.subscribe_session 改为 legacy-only
state.subscribe_playback_context 正式使用
_push_session_snapshot 改为 _push_playback_context_snapshot
v2 device.register 不再按 sessionId 调 _restorePersistedState
```

当前状态层的 `_playback_context_subscriptions` 只是死容器：初始化和断连清理里存在，但没有任何写入/读取方法。context 订阅链路必须从零新建，可参考 `subscribe_session` / `unsubscribe_session` / `list_subscribers` 的模式。必须新增：

```text
state.subscribe_playback_context(sid, playbackContextId)
state.unsubscribe_playback_context(sid, playbackContextId=None)
state.list_playback_context_subscribers(playbackContextId, userName=None)
state.list_context_participant_sids(playbackContextId, userName=None)
```

`_push_playback_context_snapshot` 必须使用 v2 serializer：

```text
payload.playbackContext 不含 sessionId
payload.deviceStates[*] 不含 sessionId
payload.deviceStates[*].clientId 使用 owner client
```

### 广播规则

当前 `_broadcast_playback_context_state()` 是发给同用户所有 sid。v2 应改为只发给：

```text
订阅该 playbackContextId 的 sid
authority 设备 sid
参与该 context 的设备 sid
```

### 验收标准

```text
订阅 context 后收到 PlaybackContext snapshot
handoff 后订阅者收到 authorityClientId 变化
非订阅者不收到无关 context 广播
v2 device.register 不再自动 restore session state
legacy session.subscribe 行为不回归
```

---

## 12. Task 8：跟播改为 sourcePlaybackContextId

> 第二阶段任务。第一阶段只要求 legacy follow 不污染 v2 PlaybackContext 主路径。

### 当前旧模式

```text
sourceClientId
sourceSessionId
followSessionId
session.subscribe
```

### v2 新模式

```text
sourcePlaybackContextId
```

### 请求示例

```json
{
  "action": "follow.start",
  "payload": {
    "sourcePlaybackContextId": "playback:alice:main"
  }
}
```

### 新关系

```text
followerClientId -> sourcePlaybackContextId
```

### 行为

```text
follow.start 后自动 playback.context.subscribe
follower 收到 PlaybackContext snapshot
follower 本地按 context 播放
follower playback.update 写 DevicePlaybackState(mode=follow)
follower 不能控制 sourcePlaybackContext
```

### 验收标准

```text
follow.start 不再需要 sourceSessionId
follow.start 不再要求 sourceClientId 在线
只要 PlaybackContext 存在并属于同用户即可跟播
follow.stop 取消 context subscription
follower playback.update 不覆盖 source PlaybackContext
第一阶段 legacy follow 测试不回归
```

---

## 13. Task 9：群播改成 Broadcast PlaybackContext

> 第二阶段任务。第一阶段只要求 legacy broadcast 继续工作，并且 broadcast participant feedback 不写普通 PlaybackContext authority 状态。

### 当前旧模式

```text
broadcastId
state._broadcasts
state._broadcast_participants
state._broadcast_playback_states
broadcast.queue.sync
broadcast.playItem
broadcast.status
```

### v2 目标

群播共享播放状态进入 PlaybackContext：

```text
playbackContextId = broadcast:alice:xxxx
contextType = broadcast
authorityClientId = owner/controller
participants = [...]
```

### 建议字段

短期可以放到 `playback_json`：

```json
{
  "contextType": "broadcast",
  "ownerClientId": "phone-1",
  "participants": ["phone-1", "pc-1"],
  "controlPolicy": "participants_and_controllers_can_control"
}
```

后续可正规化为数据库字段：

```text
context_type
owner_client_id
participants_json
control_policy
```

### 请求示例

```json
{
  "action": "broadcast.start",
  "payload": {
    "playbackContextId": "broadcast:alice:main",
    "participants": ["phone-1", "pc-1"],
    "queueSongIds": ["song-1"],
    "currentIndex": 0,
    "positionMs": 0,
    "autoPlay": true
  }
}
```

### 行为

```text
broadcast.start 创建 contextType=broadcast 的 PlaybackContext
participants 收到 playback.prepare / player.play
participant playback.update 写 DevicePlaybackState(mode=broadcast)
broadcast.status 可由 playback.context.status 组装
```

### 验收标准

```text
群播共享队列来自 PlaybackContext
每个参与设备状态来自 DevicePlaybackState
broadcast participant feedback 不进入普通 authority 更新
broadcast.stop 将 PlaybackContext.state 改为 stopped 或 ended
第一阶段 legacy broadcast 测试不回归
```

---

## 14. Task 10：Handoff 保持纯 PlaybackContext

### v2 请求

```json
{
  "action": "playback.handoff.start",
  "payload": {
    "playbackContextId": "playback:alice:main",
    "targetClientId": "pc-1",
    "baseControlVersion": 8
  }
}
```

### 规则

```text
handoff 不创建 PlaybackContext
handoff 不改变 playbackContextId
handoff 只改变 authorityClientId
source 和 target 必须属于同一 user
target 必须在线
target 必须支持 playbackPrepare 和 effectiveAtPlayback
source 必须是当前 authority，或当前 client 是 controller 且显式指定 source
```

### 还需要补齐的生命周期问题

```text
cancel 后必须 abort pending prepare
complete timeout 后必须通知 target pause/release
幂等 key 从 (userName, requestId) 升级为 (userName, originClientId, requestId)
controller 发起时 originClientId 必须是真实发起者 current_client.clientId
complete 成功后统一发送 playback.handoff.release
```

当前运行时 handoff index 是 `(userName, requestId)`，持久层 `getPlaybackHandoffByRequest(user_name, request_id)` 也是 2 元组查询。升级时必须同步修改：

```text
state.create_playback_handoff(...)
state.get_playback_handoff_by_request(...)
ws_store.getPlaybackHandoffByRequest(...)
_handle_handoff_start(...) 调用点
```

同时保留既有保护：同一 `playbackContextId` 上如果已有 `preparing` / `ready` / `committed` handoff，新的 handoff 仍必须被拒绝。

`originClientId` 不能继续默认等于 `sourceClientId`。source 设备直接发起时两者可以相同；controller 发起时：

```text
originClientId = controller clientId
sourceClientId = 当前 authorityClientId
```

`playback.handoff.release` 是 v2 出站 command/state，不应复用普通 `player.pause` 表达释放语义。payload 至少包含：

```text
playbackContextId
handoffId
authorityClientId
reason: handoff_completed | canceled | timed_out | aborted
```

### 验收标准

```text
handoff 后 playbackContextId 不变
authorityClientId source -> target
source late update 只能 deviceFeedback
requestId 幂等从 (userName, requestId) 升级到 (userName, originClientId, requestId)
同一 playbackContextId 上已有 preparing / ready / committed handoff 时，新 handoff 仍被拒绝
cancel 后 late ready 不触发 player.play
timeout 后 target 收到 playback.handoff.release
target 不支持 playbackPrepare / effectiveAtPlayback 时返回 bad_request 或 forbidden
```

---

## 15. Task 11：状态层清理

### 当前新旧状态并存

```text
_queues
_local_queues
_playback_states
_session_subscriptions

_playback_contexts
_device_playback_states
_playback_context_subscriptions
_handoffs
```

### v2 目标

运行时核心状态只保留：

```text
_clients
_client_to_sid
_playback_contexts
_device_playback_states
_playback_context_subscriptions
_handoffs
_pending_prepares
```

但第一阶段不要先删字段。第一阶段的关键是让 v2 action 只能调用 v2 状态 API：

```text
create_playback_context
get_playback_context              # 已存在，可复用
update_existing_playback_context_queue
apply_existing_authority_playback_update
record_device_playback_state
subscribe_playback_context
unsubscribe_playback_context
list_playback_context_subscribers
```

旧 API 保留但改名或标注为 legacy-only：

```text
update_queue
update_local_queue
update_playback_state
update_playback_control
subscribe_session
restore_playback_state
restore_queue
legacy_queue_to_playback_context_restore
```

v2 action 可以从 `EmoPlaybackContext` 冷恢复已有 context，但不能调用 legacy queue fallback 创建 context。

### 清理策略

第一阶段先不删除旧字段，只是不让 v2 action 走旧字段。

第二阶段稳定后删除或 legacy-only：

```text
_queues
_playback_states
_session_subscriptions
```

`_local_queues` 需要重新定义：

```text
保留方案 A：按 deviceSessionId + clientId 保存设备本地队列
保留方案 B：按 playbackContextId + clientId 保存 context 内本地队列
删除方案 C：客户端本地队列不再由服务端持久化
```

第一阶段默认不重构 local queue。如果某个 v2 控制路径必须读取本地队列，才采用方案 A 作为过渡：

```text
(deviceSessionId, clientId)
```

原因是 local queue 更像设备私有状态，不应该依附 PlaybackContext。完整 local queue 协议迁移放第二阶段。

---

## 16. Task 12：持久化层清理

### 当前旧表仍参与运行时

```text
EmoSessionQueue
EmoLocalQueue
EmoPlaybackState
```

当前新表：

```text
EmoPlaybackContext
EmoDevicePlaybackState
EmoPlaybackHandoff
```

### v2 目标

新客户端播放全流程只写：

```text
EmoPlaybackContext
EmoDevicePlaybackState
EmoPlaybackHandoff
```

旧表只用于 migration 或 legacy fallback。

### 需要做

```text
getQueueState / saveQueueState 标记 legacy-only
getPlaybackState / savePlaybackState 标记 legacy-only
播放实时路径移除旧 store 调用
新增 serializePlaybackContextV2 / serializeDevicePlaybackStateV2
legacy serializer 保留 sessionId / sourceClientId alias
v2 serializer 禁止输出 sessionId
新增 createPlaybackContextState，避免 savePlaybackContextState 隐式 upsert 创建
新增 updatePlaybackContextState，更新不存在时返回 not_found
新增 getPlaybackContextWithDeviceStates
新增 listUserPlaybackContexts
新增 deletePlaybackContext / expirePlaybackContext
```

命名约定沿用现有 store 风格：

```text
函数名：camelCase，例如 getPlaybackContextState
参数和 Peewee 列：snake_case，例如 playback_context_id
payload 键：camelCase，例如 playbackContextId
```

`listUserPlaybackContexts` 可以基于 `EmoPlaybackContext.user_name` 实现，但当前 `user_name` 没有索引；如果后续高频查询，第二阶段再补索引。

`saveDevicePlaybackState(is_authority=True)` 当前会先把同 context 下其他设备的 `is_authority` 降级。拆分 create/update 时必须保留这个行为。

### 验收标准

```text
v2 新播放流程不写 EmoSessionQueue
v2 新播放流程不写 EmoPlaybackState
v2 status / context push payload 不含 sessionId
旧表数据可以迁移到 PlaybackContext
测试可以断言旧表没有新增记录
legacy 流程仍按旧 store 函数读写，直到退场阶段
```

---

## 17. Task 13：数据库模型调整

### 阶段策略

第一阶段不建议立即新增数据库列，避免把协议重构、状态层重构和三种数据库迁移绑在同一个变更里。

第一阶段：

```text
contextType / participants / controlPolicy 等新语义先放 playback_json
Peewee 模型只补必要的 serializer / helper
sqlite / mysql / postgres migration 不新增列
不为 volume / mode / is_authority 写重复 migration
```

第二阶段：

```text
follow / broadcast 完成 PlaybackContext 化后，再正规化字段
同时补 sqlite / mysql / postgres migration
补 schema migration tests
```

### 第二阶段建议新增字段

`EmoPlaybackContext`：

```text
context_type: normal | broadcast
owner_client_id: nullable
participants_json: nullable
control_policy: nullable
closed_at: nullable
expires_at: nullable
```

`EmoDevicePlaybackState`：

```text
last_reported_at: nullable
```

`mode` 和 `is_authority` 已存在，不需要新增。`last_reported_at` 只有在必须区分“设备真实上报时间”和“DB row updated_at”时才新增；否则优先复用现有 `updated_at` / payload `serverUpdatedAtMs`。

`EmoPlaybackHandoff`：

```text
origin_client_id 已有，但幂等查询要使用
```

### 幂等索引建议

当前 handoff 幂等 key 应升级为：

```text
(user_name, origin_client_id, request_id)
```

第一阶段如果不改表结构，必须在运行时 index 里使用同样 key；第二阶段再补数据库索引。

---

## 18. 推荐分阶段 commit

### Commit 0

```text
Split legacy and PlaybackContext v2 protocol boundaries
```

内容：

```text
新增 CAPABILITY_PLAYBACK_CONTEXT_V2 = "playbackContextV2"
新增 strict v2 / context-compatible / legacy 判定矩阵
新增 v2-only ID resolver
新增 legacy-only resolver 标注
新增 v2 serializer，禁止输出 sessionId
保留旧 dispatcher 行为
补 legacy 不回归测试
```

### Commit 1

```text
Introduce PlaybackContext v2 protocol actions
```

内容：

```text
新增 playback.context.create/status/subscribe/unsubscribe
新增 queue.context.sync
新增 v2 validation helpers
新增显式 create/update-existing 状态层 API
不删除旧逻辑
补 v2 基础测试
```

### Commit 2

```text
Make playback updates strict to existing contexts
```

内容：

```text
v2 playback.update 不再隐式创建 context
v2 playback.update 不再写 legacy EmoPlaybackState
非 authority 只写 DevicePlaybackState
legacy playback.update 保持兼容
```

### Commit 3

```text
Route player controls through playback contexts
```

内容：

```text
player.pause/play/seek/next/prev 改为 playbackContextId
服务端通过 authorityClientId 路由
保留 legacy targetClientId + sessionId 控制路径
```

### Commit 4

```text
Move follow playback to context subscriptions
```

内容：

```text
第二阶段
follow.start 使用 sourcePlaybackContextId
移除 sourceSessionId
follow.stop 取消 context subscribe
```

### Commit 5

```text
Unify broadcast playback with PlaybackContext
```

内容：

```text
第二阶段
broadcast.start 创建 broadcast context
participant state 写 DevicePlaybackState
broadcast status 从 context 组装
```

### Commit 6

```text
Remove realtime sessionId playback paths
```

内容：

```text
最终退场阶段，新客户端全量适配后执行
移除 SESSION_ACTIONS
移除 queue.session.sync 运行时路径
移除 session subscribe restore
旧 store 函数标记 migration-only
更新 /player 和 docs，删除 legacy sessionId 指引
```

---

## 19. 第一阶段最小落地范围

第一阶段不要一次把群播和跟播都完全重构，否则改动过大。

建议第一阶段只做：

```text
1. protocol gate：区分 strict v2 / context-compatible / legacy 分支
2. v2 serializer：v2 出站 payload 不含 sessionId
3. v2-only ID resolver：不从 sessionId fallback
4. playback.context.create
5. playback.context.status
6. playback.context.subscribe/unsubscribe
7. queue.context.sync
8. v2 playback.update strict
9. v2 player.pause / play / seek / next / prev / queue.playItem 走 playbackContextId
10. handoff 走纯 playbackContextId，并补 release / timeout / cancel 语义
11. strict v2 payload 带 sessionId 返回 bad_request
12. v2 新流程不写 EmoSessionQueue / EmoPlaybackState
```

第一阶段明确不做：

```text
1. 不删除 session.subscribe / queue.session.sync
2. 不重构 queue.local.*
3. 不把 follow.* 改成 sourcePlaybackContextId
4. 不把 broadcast.* 改成 PlaybackContext
5. 不新增 broadcast/follow 正规化数据库字段
6. 不要求旧 Web 播放器立刻切 v2
```

跟播、群播、local queue 和旧 Web 播放器迁移放第二阶段。

---

## 20. 测试计划

建议新增：

```text
tests/base/test_emo_ws_playback_context_v2.py
tests/base/test_emo_ws_playback_context_v2_serializers.py
tests/base/test_emo_ws_playback_context_v2_controls.py
tests/base/test_emo_ws_playback_context_v2_handoff.py
tests/base/test_emo_ws_playback_context_v2_follow_broadcast.py
```

其中 `test_emo_ws_playback_context_v2_follow_broadcast.py` 属于第二阶段；第一阶段只需要保证 legacy follow/broadcast 不回归。

### 第一阶段必须覆盖

```text
test_v2_device_register_requires_device_session_id
test_legacy_device_register_still_accepts_session_id

test_v2_playback_context_create_sets_current_client_as_authority

test_v2_playback_context_status_returns_context_and_device_states

test_v2_playback_context_status_uses_v2_serializer_without_session_id

test_v2_playback_context_subscribe_pushes_snapshot

test_v2_queue_context_sync_requires_existing_context

test_v2_queue_context_sync_requires_authority

test_v2_queue_context_sync_does_not_write_emo_session_queue

test_v2_playback_update_requires_existing_context

test_v2_playback_update_from_authority_updates_context

test_v2_playback_update_from_non_authority_is_device_feedback_only

test_v2_playback_update_does_not_write_emo_playback_state

test_v2_player_pause_controls_context_authority

test_v2_player_seek_updates_context_control_version

test_v2_player_play_routes_to_context_authority

test_v2_player_next_uses_context_queue

test_v2_player_prev_uses_context_queue

test_v2_queue_play_item_uses_context_queue

test_v2_handoff_keeps_same_playback_context_id

test_v2_handoff_transfers_authority_client_id

test_v2_handoff_cancel_aborts_pending_prepare

test_v2_handoff_timeout_releases_target

test_v2_handoff_idempotency_is_scoped_by_origin_client

test_v2_handoff_source_origin_client_id_is_source

test_v2_handoff_controller_origin_client_id_is_controller

test_strict_v2_session_id_payload_is_rejected

test_context_compatible_session_id_payload_is_not_used_as_context_id

test_legacy_queue_session_sync_still_works_before_final_removal

test_legacy_playback_update_still_works_before_final_removal
```

### 第二阶段补充覆盖

```text
test_follow_start_subscribes_playback_context
test_follow_update_is_device_feedback_only
test_broadcast_start_creates_broadcast_context
test_broadcast_participant_update_writes_device_state
```

其中最重要的是：

```text
test_strict_v2_session_id_payload_is_rejected
test_v2_playback_context_status_uses_v2_serializer_without_session_id
test_v2_queue_context_sync_does_not_write_emo_session_queue
```

这些测试用来防止后面又把 `sessionId` 混回 v2 播放协议，或让 v2 新路径继续写旧表。

---

## 21. 验收标准

第一阶段完成后，必须满足：

```text
1. strict v2 客户端所有 v2 播放 action 不再传 sessionId
2. v2 / context-compatible 分支不再从 sessionId 推导 playbackContextId
3. v2 playback.update 不创建 PlaybackContext
4. PlaybackContext 只能通过 playback.context.create 创建
5. queue.context.sync 只更新已有 PlaybackContext
6. v2 远程控制只需要 playbackContextId，不需要 targetClientId
7. handoff 后 playbackContextId 不变，只改 authorityClientId
8. source late update 不能覆盖 target
9. v2 新播放流程不写 EmoSessionQueue / EmoPlaybackState
10. v2 serializer 不输出 sessionId
11. legacy sessionId 路径被隔离，不被 v2 helper fallback
12. context-compatible payload 即使带 sessionId，也不能用它作为 context 主键
13. legacy Web 播放器、follow、broadcast 不回归
14. PlaybackContext 必须包含共享队列字段 queueSongIds / currentIndex / trackId
15. 设备真实音量 / 静音 / 输出设备状态必须归属 DevicePlaybackState
16. 只有明确需要跨设备统一应用内音量时，才在 PlaybackContext 增加 logicalVolume
17. v2 迁移不是 sessionId 到 playbackContextId 的简单改名
18. v2 主链路中设备房间职责必须由 deviceSessionId 承担
19. v2 主链路中播放任务职责必须由 playbackContextId 承担
20. v2 主链路中设备状态必须由 playbackContextId + clientId 承担
21. v2 主链路中远控必须使用 playbackContextId -> authorityClientId
```

最终退场阶段完成后，额外满足：

```text
1. follow 基于 sourcePlaybackContextId
2. broadcast 以 PlaybackContext 承载共享播放状态
3. 旧 sessionId 只用于 migration，不再用于实时协议
4. SESSION_ACTIONS / queue.session.sync / legacy playback state 实时路径被删除
5. /player 和客户端文档不再要求稳定 sessionId
```

---

## 22. 推荐实现顺序

第一阶段优先顺序：

```text
1. 加 protocol gate 和 v2 serializer，不删旧 action
2. 拆 v2-only resolver 和 legacy resolver
3. 加显式 PlaybackContext create / update-existing 状态层 API
4. 加 playback.context.* 和 queue.context.sync
5. v2 playback.update strict
6. v2 远控 context 化
7. handoff 生命周期补强
8. 新客户端接入 v2 action
```

第二阶段优先顺序：

```text
1. 跟播 context 化
2. 群播 context 化
3. local queue 归属重新定义
4. /player Web 播放器迁移到 v2
5. 删除旧 sessionId 播放路径
```

这样可以保证服务端每一步都有可测试状态，不会一次性把远控、群播、跟播全部打断。

---

## 23. 推荐文档与分支命名

推荐文档路径：

```text
docs/plans/2026-07-09-playback-context-v2-strict-architecture.zh.md
```

推荐分支名：

```text
feature/playback-context-v2-strict
```

推荐总 commit 标题：

```text
Refactor realtime playback to strict PlaybackContext v2
```

---

## 24. REVISED 版合并索引

`2026-07-09-playback-context-v2-strict-architecture.REVISED.zh.md` 中的订正已合并回本主计划，落点如下：

| 订正 | 主计划落点 |
| --- | --- |
| 1. `playbackContextV2` 是新增 capability，第一阶段按 capability OR payload 字段分流 | §0.1、§4.0、Task 1/2/5 |
| 2. `EmoDevicePlaybackState.mode` / `is_authority` 已存在 | §17 Task 13 |
| 3. `sessionId` 远控兜底不在 `_resolve_control_target`，而在内联控制和 commit builder 相关函数 | §10 Task 6 |
| 4. `get_playback_context` 已存在，需新增的是显式 create / update-existing 边界 | §0.3、Task 3、Task 11 |
| 5. handoff 幂等键从 2 元组升到 3 元组，且保留同 context 并发保护 | §14、§17 |
| 6. `_playback_context_subscriptions` 当前只是死容器，context 订阅链路要新建 | §3.11、Task 7、Task 11 |
| 7. `queue.context.sync` 必须是独立 handler，不能复用 `queue.session.sync` | Task 4 |
| 8. 三个 ID 当前会塌缩，v2 依赖客户端真实传入不同 ID | §0.1、§2.2、Task 1 |
| 9. v2 新测试统一加 `test_v2_` 前缀，避免和 legacy 测试语义撞车 | §20 |
| 10. `broadcast.queue.sync` / `broadcast.playItem` 补入 action 清单，serializer alias 来源列写准 | §0.2、§4.1、§13 |
| 11. `volume` 两张表都已有，音量归属是行为迁移，不是加列 | §2.6、§17 |
| 12. store 层沿用 camelCase 函数、snake_case 参数/列、camelCase payload 键 | Task 12 |
| 13. v2 `playback.update` 的旧写入点必须用分支隔离，context 不存在返回 `not_found` | Task 5 |

此外，主计划已统一第一阶段远控范围：`player.pause / player.play / player.seek / player.next / player.prev / queue.playItem` 都走 `playbackContextId` 分支；其中 `play / next / prev / queue.playItem` 会先乐观推进共享 context，再由 authority `playback.update` 校正真实设备状态。

---

## 25. 总结

这次 v2 的核心不是新增一个功能，而是确定实时播放协议的主轴：

```text
播放任务 = PlaybackContext
共享队列 = PlaybackContext.queueSongIds / currentIndex / trackId
设备身份 = deviceSessionId
播放权 = authorityClientId
设备反馈 = DevicePlaybackState
设备真实音量 = DevicePlaybackState.volume

旧 sessionId 的设备房间职责 = deviceSessionId
旧 sessionId 的播放任务职责 = playbackContextId
旧 sessionId + clientId 的设备状态职责 = playbackContextId + clientId
```

第一阶段，`sessionId` 从 v2 实时播放路径中退场，并被隔离在 legacy 路径。最终退场阶段，新客户端全量适配后，再删除 legacy realtime sessionId 路径。

所有后续能力，包括：

```text
本地播放
远程控制
无痕切换 handoff
跟播
群播
状态恢复
订阅广播
```

都应该围绕 `playbackContextId` 统一设计。
