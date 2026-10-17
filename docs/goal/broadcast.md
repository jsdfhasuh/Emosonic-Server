# Goal: 实现 Emosonic 群播 Broadcast Playback v1

> **Superseded / legacy Goal.** 本文仅保留为历史实现证据，不是 strict-v2 r5
> Broadcast contract。当前权威要求见
> `specs/emosonic_strict_v2_socketio_server_contract.md` 和
> `docs/goal/emosonic_strict_v2_r5_server_adaptation.md`。

## 一、总体目标

在跟播 Follow Playback v1 完成后，实现第一版“群播”能力。

群播定义：

多台设备共同播放同一份共享队列。手机、电脑、音箱等设备可以一起播放，任意有权限的参与设备都可以修改群播队列、切歌、暂停、继续、调整进度。

群播与跟播不同：

```text
跟播 follow：
  一台设备跟随另一台设备播放。
  sourceClientId 是权威。
  跟随设备只是复制播放。

群播 broadcast：
  多台设备共同参与一个 broadcastId。
  broadcastId 是权威。
  参与设备共同维护同一份队列和播放状态。
```

本阶段目标是实现：

```text
broadcastId
BroadcastState
participants
broadcast queue
broadcast playback command
broadcast version
broadcast control policy
```

第一版只做群播闭环，不做自动加入、手动加入、ready gate、持久化恢复和毫秒级同步。

本文件作为实现计划模板使用时，v1 范围以本段和“验收标准”为准。文中出现的数据库表、join/leave、ready gate、auto join 仅作为后续阶段预留，不应进入 v1 实现。

---

## 二、设计原则

### 1. 不要把群播塞进 sessionId

现有 `sessionId` 继续表示设备自己的播放上下文。

例如：

```text
phone-1:
  sessionId = root:phone

pc-1:
  sessionId = root:pc

speaker-1:
  sessionId = root:speaker
```

即使三台设备参与同一个群播，它们自己的 `sessionId` 也不切换。

群播状态应由 `broadcastId` 管理：

```text
broadcastId = broadcast-001

participants:
  phone-1
  pc-1
  speaker-1
```

正确模型：

```text
clientId      = 设备是谁
sessionId     = 设备自己的播放上下文
broadcastId   = 群播共享上下文
```

---

### 2. broadcastId 是群播权威

群播队列不能属于手机 session，也不能属于电脑 session。

错误理解：

```text
手机发起群播，所以群播队列属于 root:phone
```

正确理解：

```text
手机只是创建者。
broadcastId 才是群播队列和群播状态的权威。
```

群播队列应该是：

```text
broadcastId -> queueSongIds / currentIndex / positionMs / state / version
```

---

### 3. 控制命令不能只依赖 targetClientId

现有单播控制是：

```text
targetClientId -> 单台设备
```

群播需要新增：

```text
broadcastId -> participants -> 多台设备
```

也就是说，群播命令不是发给一台设备，而是发给群播内所有参与设备。

---

### 4. 区分 participant 和 controller

第一版必须明确两个概念：

```text
participant:
  真正执行音频播放的 player 设备。
  必须 roles 包含 player。
  会出现在 BroadcastState.participants 中。
  会收到 broadcast.play / pause / seek / playItem / stop 等执行命令。

controller:
  控制界面或遥控设备。
  通常 roles 包含 controller。
  可以发起和控制 broadcast，但默认不执行音频播放。
  除非它同时也是 player，否则不要放进 participants。
```

Web 控制台属于 controller，不应该因为发起群播就变成 participant。

---

## 三、核心数据结构

### 1. BroadcastState

新增群播状态：

```json
{
  "broadcastId": "broadcast-001",
  "userName": "root",
  "ownerClientId": "phone-1",
  "participants": ["phone-1", "pc-1", "speaker-1"],
  "queueSongIds": ["song-1", "song-2", "song-3"],
  "currentIndex": 0,
  "trackId": "song-1",
  "positionMs": 0,
  "state": "playing",
  "version": 1,
  "updatedByClientId": "phone-1",
  "createdAt": 1770000000,
  "updatedAt": 1770000000,
  "controlPolicy": "participants_and_controllers_can_control"
}
```

字段说明：

```text
broadcastId:
  群播 ID。

userName:
  所属用户。群播不能跨用户。

ownerClientId:
  发起群播的设备。

participants:
  当前参与群播的设备 clientId 列表。

queueSongIds:
  群播共享队列。

currentIndex:
  当前播放到队列中的第几首。

trackId:
  当前播放歌曲 ID，通常由 queueSongIds[currentIndex] 派生。

positionMs:
  当前播放进度。

state:
  playing / paused / stopped。

version:
  BroadcastState 版本号。队列、当前歌曲、播放状态、进度发生权威变更时都应 version + 1。

updatedByClientId:
  最近一次修改群播状态的设备。

controlPolicy:
  群播控制权限策略。
```

---

### 2. BroadcastParticipantState

第一版维护每个参与设备的执行状态：

```json
{
  "broadcastId": "broadcast-001",
  "clientId": "pc-1",
  "sessionId": "root:pc",
  "state": "playing",
  "trackId": "song-1",
  "positionMs": 12000,
  "syncDriftMs": -200,
  "online": true,
  "errorCode": null,
  "errorMessage": null,
  "lastSeenAt": 1770000000
}
```

用途：

```text
1. UI 显示每台设备是否跟上。
2. 判断某台设备是否播放失败。
3. 判断群播参与者是否离线。
4. 后续支持同步校正。
```

---

## 四、后端状态设计

在 `ws_state.py` 中新增内存结构：

```python
_broadcasts = {}
_broadcast_participants = {}
_broadcast_playback_states = {}
_client_active_broadcast = {}
```

含义：

```text
_broadcasts:
  broadcastId -> BroadcastState

_broadcast_participants:
  broadcastId -> set(clientId)

_broadcast_playback_states:
  (broadcastId, clientId) -> participant playback state

_client_active_broadcast:
  clientId -> broadcastId
```

建议新增方法：

```python
create_broadcast(...)
get_broadcast(broadcast_id)
list_broadcasts(user_name=None)
add_broadcast_participant(broadcast_id, client_id)
remove_broadcast_participant(broadcast_id, client_id)
list_broadcast_participants(broadcast_id)
update_broadcast_queue(...)
update_broadcast_state(...)
update_broadcast_participant_state(...)
stop_broadcast(...)
get_active_broadcast_for_client(client_id)
set_active_broadcast_for_client(client_id, broadcast_id)
clear_active_broadcast_for_client(client_id)
```

第一版以内存实现为主。

如果需要断线恢复，再进入数据库持久化阶段。

---

## 五、持久化设计

第一版固定选择内存实现，不落库。

数据库表只作为后续持久化恢复阶段的预留设计，不属于 v1 验收范围。

### 方案 A：先内存，不落库

优点：

```text
实现快
改动小
适合先验证群播流程
```

缺点：

```text
服务重启后群播丢失
无法恢复 active broadcast
```

### 方案 B：新增数据库表（后续预留）

如果希望群播可恢复，建议新增：

```text
EmoBroadcast
EmoBroadcastParticipant
EmoBroadcastPlaybackState
```

#### EmoBroadcast

```python
class EmoBroadcast(_Model):
    id = PrimaryKeyField()
    broadcast_id = CharField(128, unique=True)
    user_name = CharField(64)
    owner_client_id = CharField(128)
    queue_json = TextField()
    current_index = IntegerField(default=0)
    position_ms = IntegerField(default=0)
    state = CharField(32, default="stopped")
    version = IntegerField(default=1)
    control_policy = CharField(64, default="participants_and_controllers_can_control")
    updated_by_client_id = CharField(128, null=True)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)
```

#### EmoBroadcastParticipant

```python
class EmoBroadcastParticipant(_Model):
    id = PrimaryKeyField()
    broadcast_id = CharField(128)
    client_id = CharField(128)
    session_id = CharField(128)
    user_name = CharField(64)
    role = CharField(32, default="participant")
    joined_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = ((('broadcast_id', 'client_id'), True),)
```

#### EmoBroadcastPlaybackState

```python
class EmoBroadcastPlaybackState(_Model):
    id = PrimaryKeyField()
    broadcast_id = CharField(128)
    client_id = CharField(128)
    session_id = CharField(128)
    state = CharField(32)
    track_id = CharField(128, null=True)
    position_ms = IntegerField(default=0)
    sync_drift_ms = IntegerField(null=True)
    playback_json = TextField(null=True)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = ((('broadcast_id', 'client_id'), True),)
```

后续持久化阶段建议：

```text
先做内存版，跑通流程。
再根据体验决定是否落库。
```

---

## 六、新增 SocketIO 动作

群播动作池（v1 + 后续预留）：

```text
broadcast.start
broadcast.stop
broadcast.queue.sync
broadcast.playItem
broadcast.play
broadcast.pause
broadcast.seek
broadcast.next
broadcast.prev
broadcast.status
broadcast.participants.update
```

第一版只实现：

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

第一版暂不实现：

```text
broadcast.join
broadcast.leave
broadcast.next
broadcast.prev
broadcast.participants.update
ready gate
auto join
database persistence
```

---

## 七、事件协议设计

## 0. 统一 BroadcastState payload

凡是会改变群播权威播放状态的动作，都应让服务端更新并广播同一组核心字段：

```json
{
  "broadcastId": "broadcast-001",
  "queueSongIds": ["song-1", "song-2", "song-3"],
  "currentIndex": 0,
  "trackId": "song-1",
  "positionMs": 12000,
  "state": "playing",
  "version": 3,
  "updatedByClientId": "pc-1",
  "updatedAt": 1770000000.123
}
```

适用动作：

```text
broadcast.queue.sync
broadcast.playItem
broadcast.play
broadcast.pause
broadcast.seek
broadcast.stop
```

其中：

```text
updatedAt:
  服务端 epoch seconds。

positionMs:
  与 updatedAt 配套使用。state = playing 时，客户端按 positionMs + (serverNow - updatedAt) 推算目标进度。

version:
  BroadcastState 版本，不只是队列版本。
```

`broadcast.play / pause / seek / playItem` 下发给每台设备的 command payload 也应该包含这些核心字段，避免客户端依赖旧的本地队列状态。

## 1. broadcast.start

### 用途

创建一次群播，并把初始队列下发给目标设备。

### 请求示例

```json
{
  "type": "command",
  "action": "broadcast.start",
  "requestId": "broadcast-start-1",
  "payload": {
    "targetMode": "selectedClients",
    "targetClientIds": ["phone-1", "pc-1", "speaker-1"],
    "queueSongIds": ["song-1", "song-2", "song-3"],
    "currentIndex": 0,
    "positionMs": 0,
    "autoPlay": true,
    "controlPolicy": "participants_and_controllers_can_control"
  }
}
```

### targetMode 支持

```text
selectedClients:
  使用 payload.targetClientIds 指定设备。

allOnlinePlayers:
  当前用户下所有在线 player 设备。

allOnlinePlayersExceptSelf:
  当前用户下所有在线 player，排除发起设备。
```

注意：

```text
targetClientIds 只能选 player 设备。
如果发起者是纯 controller，它可以创建和控制 broadcast，但不会自动加入 participants。
如果发起者同时也是 player，只有被 targetMode 命中时才加入 participants。
```

### 服务端处理

```text
1. 校验当前连接已注册 clientId。
2. 校验 queueSongIds。
3. 如果 queueSongIds 为空，强制 autoPlay = false、state = stopped、currentIndex = 0、positionMs = 0。
4. 如果 queueSongIds 非空，校验 currentIndex 在队列范围内。
5. 根据 targetMode 找到参与设备。
6. 只允许同 userName 的在线 player 参与。
7. 生成 broadcastId。
8. 创建 BroadcastState。
9. 设置 participants。
10. 对每个参与设备发送 broadcast.start command。
11. 返回 ack，包含 BroadcastState 和 skippedClientIds。
```

### 下发给参与设备

```json
{
  "type": "command",
  "action": "broadcast.start",
  "sourceClientId": "phone-1",
  "targetClientId": "pc-1",
  "payload": {
    "broadcastId": "broadcast-001",
    "queueSongIds": ["song-1", "song-2", "song-3"],
    "currentIndex": 0,
    "trackId": "song-1",
    "positionMs": 0,
    "state": "playing",
    "version": 1,
    "updatedAt": 1770000000.123,
    "autoPlay": true,
    "serverStartAt": null
  }
}
```

### 客户端执行

参与设备收到后：

```text
1. 设置 mode = broadcast。
2. 设置 activeBroadcastId = broadcast-001。
3. 加载 queueSongIds。
4. 切到 currentIndex。
5. seek 到 positionMs。
6. 如果 autoPlay = true，则开始播放。
7. 上报 playback.update，带 broadcastId。
```

---

## 2. broadcast.queue.sync

### 用途

修改群播共享队列。

例如：

```text
电脑在群播中添加歌曲
手机在群播中删除歌曲
音箱切换队列顺序
```

### 请求示例

```json
{
  "type": "state",
  "action": "broadcast.queue.sync",
  "requestId": "broadcast-queue-1",
  "payload": {
    "broadcastId": "broadcast-001",
    "queueSongIds": ["song-1", "song-4", "song-5"],
    "currentIndex": 1,
    "positionMs": 0,
    "baseVersion": 1
  }
}
```

### 服务端处理

```text
1. 校验 broadcastId 存在。
2. 校验当前 client 是 participant 或同用户 controller。
3. 校验当前 client 有控制权限。
4. 校验 queueSongIds、currentIndex、positionMs。
5. 校验 baseVersion 必须存在，并且等于当前 BroadcastState.version。
6. 更新 BroadcastState。
7. version + 1。
8. updatedByClientId = 当前 clientId。
9. 广播给所有 participants。
```

### 广播示例

```json
{
  "type": "state",
  "action": "broadcast.queue.sync",
  "payload": {
    "broadcastId": "broadcast-001",
    "queueSongIds": ["song-1", "song-4", "song-5"],
    "currentIndex": 1,
    "trackId": "song-4",
    "positionMs": 0,
    "state": "playing",
    "version": 2,
    "updatedByClientId": "pc-1",
    "updatedAt": 1770000000
  }
}
```

### 客户端执行

参与设备收到后：

```text
1. 如果 activeBroadcastId 不等于 payload.broadcastId，则忽略执行，只更新 UI。
2. 更新本地 broadcast queue。
3. 如果当前播放 track 不在新队列中，根据 currentIndex 切换。
4. 如果当前 track 仍然存在，可以尽量保持播放。
```

注意：

```text
broadcast.queue.sync 主要负责队列同步。
如果要明确切歌，应使用 broadcast.playItem。
如果要明确调整进度，应使用 broadcast.seek。
```

---

## 3. broadcast.playItem

### 用途

让所有参与设备播放群播队列中的某一首。

### 请求示例

```json
{
  "type": "command",
  "action": "broadcast.playItem",
  "requestId": "broadcast-play-item-1",
  "payload": {
    "broadcastId": "broadcast-001",
    "queueIndex": 2,
    "positionMs": 0,
    "baseVersion": 2
  }
}
```

### 服务端处理

```text
1. 校验 broadcastId 存在。
2. 校验当前 client 是 participant 或 controller。
3. 校验 baseVersion 必须存在，并且等于当前 BroadcastState.version。
4. 校验 queueIndex 在 broadcast queue 范围内。
5. 更新 BroadcastState:
   currentIndex = queueIndex
   trackId = queueSongIds[queueIndex]
   positionMs = payload.positionMs or 0
   state = playing
   version + 1
   updatedAt = 当前服务端时间
   updatedByClientId = 当前 clientId
6. 返回 ack，ack 中包含最新 BroadcastState。
7. 向所有 participants 转发 broadcast.playItem，command payload 包含统一 BroadcastState 核心字段。
```

### 下发给参与设备

```json
{
  "type": "command",
  "action": "broadcast.playItem",
  "sourceClientId": "pc-1",
  "targetClientId": "speaker-1",
  "payload": {
    "broadcastId": "broadcast-001",
    "queueSongIds": ["song-1", "song-4", "song-5"],
    "queueIndex": 2,
    "trackId": "song-5",
    "positionMs": 0,
    "state": "playing",
    "version": 3,
    "updatedAt": 1770000000.123
  }
}
```

### 客户端执行

```text
1. 确认当前 activeBroadcastId。
2. 从 broadcast queue 中找到 queueIndex。
3. 加载 trackId。
4. seek 到 positionMs。
5. 开始播放。
6. 上报 playback.update，带 broadcastId。
```

---

## 4. broadcast.play

### 用途

让所有参与设备继续播放当前群播曲目。

### 请求示例

```json
{
  "type": "command",
  "action": "broadcast.play",
  "payload": {
    "broadcastId": "broadcast-001"
  }
}
```

### 服务端处理

```text
1. 校验 broadcastId 存在。
2. 校验权限。
3. 校验 broadcast queue 非空，且当前 trackId/currentIndex 有效。
4. 更新 BroadcastState.state = playing。
5. 如果当前 state 原本是 paused/stopped，positionMs 使用 BroadcastState.positionMs。
6. 更新 updatedAt、updatedByClientId、version。
7. 返回 ack，并转发给所有 participants，payload 包含统一 BroadcastState 核心字段。
```

---

## 5. broadcast.pause

### 用途

暂停所有参与设备。

### 请求示例

```json
{
  "type": "command",
  "action": "broadcast.pause",
  "payload": {
    "broadcastId": "broadcast-001"
  }
}
```

### 服务端处理

```text
1. 校验 broadcastId 存在。
2. 校验权限。
3. 更新 BroadcastState.state = paused。
4. 如果 payload.positionMs 存在，使用 payload.positionMs。
5. 如果 payload.positionMs 缺失且原状态为 playing，服务端用 positionMs + (now - updatedAt) 估算暂停点。
6. 更新 updatedAt、updatedByClientId、version。
7. 返回 ack，并转发给所有 participants，payload 包含统一 BroadcastState 核心字段。
```

---

## 6. broadcast.seek

### 用途

调整所有参与设备的播放进度。

### 请求示例

```json
{
  "type": "command",
  "action": "broadcast.seek",
  "payload": {
    "broadcastId": "broadcast-001",
    "positionMs": 45000
  }
}
```

### 服务端处理

```text
1. 校验 broadcastId 存在。
2. 校验权限。
3. 校验 positionMs 是非负整数。
4. 更新 BroadcastState.positionMs。
5. updatedByClientId = 当前 clientId。
6. 更新 updatedAt、version。
7. 返回 ack，并转发给所有 participants，payload 包含统一 BroadcastState 核心字段。
```

---

## 7. broadcast.stop

### 用途

结束群播。

### 请求示例

```json
{
  "type": "command",
  "action": "broadcast.stop",
  "payload": {
    "broadcastId": "broadcast-001"
  }
}
```

### 服务端处理

```text
1. 校验 broadcastId。
2. 校验权限。
3. 设置 BroadcastState.state = stopped。
4. 更新 updatedAt、updatedByClientId、version。
5. 向所有 participants 下发 broadcast.stop，payload 包含统一 BroadcastState 核心字段。
6. 清理 client_active_broadcast。
7. 第一版保留 stopped BroadcastState 到内存中，便于 status 查询；服务重启后丢失。
```

### 客户端执行

```text
1. 退出 broadcast mode。
2. activeBroadcastId = null。
3. 根据客户端策略：
   - 停止播放；
   - 或保持当前歌曲播放；
   - 或恢复进入群播前的 solo 状态。
```

第一版建议：

```text
收到 broadcast.stop 后停止播放，并退出 broadcast mode。
```

---

## 8. broadcast.status

### 用途

查询某个群播当前状态。

### 请求示例

```json
{
  "type": "state",
  "action": "broadcast.status",
  "payload": {
    "broadcastId": "broadcast-001"
  }
}
```

### 返回示例

```json
{
  "type": "state",
  "action": "broadcast.status",
  "payload": {
    "broadcast": {
      "broadcastId": "broadcast-001",
      "ownerClientId": "phone-1",
      "participants": ["phone-1", "pc-1", "speaker-1"],
      "queueSongIds": ["song-1", "song-2"],
      "currentIndex": 0,
      "trackId": "song-1",
      "positionMs": 12000,
      "state": "playing",
      "version": 3,
      "updatedByClientId": "phone-1",
      "updatedAt": 1770000000.123,
      "controlPolicy": "participants_and_controllers_can_control"
    },
    "participantStates": [
      {
        "clientId": "phone-1",
        "state": "playing",
        "trackId": "song-1",
        "positionMs": 12100,
        "syncDriftMs": 0
      },
      {
        "clientId": "pc-1",
        "state": "playing",
        "trackId": "song-1",
        "positionMs": 11900,
        "syncDriftMs": -200
      }
    ]
  }
}
```

---

## 八、playback.update 扩展

参与群播的设备继续使用现有 `playback.update`，但 payload 需要带上：

```json
{
  "type": "event",
  "action": "playback.update",
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

服务端处理：

```text
1. 仍然保存普通 playback state。
2. 如果 payload.broadcastId 存在：
   - 校验 client 是该 broadcast participant。
   - 更新 broadcast participant playback state。
   - v1 至少保证 broadcast.status 能查询到最新 participant state。
   - 是否主动广播 participant state 可以后续按 UI 需要扩展。
```

注意：

```text
参与设备的 playback.update 是执行反馈。
broadcastId 的 BroadcastState 才是群播权威。
```

不要让某一台设备的 `playback.update` 随意覆盖 BroadcastState。

BroadcastState 只由以下动作更新：

```text
broadcast.queue.sync
broadcast.playItem
broadcast.play
broadcast.pause
broadcast.seek
broadcast.stop
```

---

## 九、权限设计

第一版默认：

```text
controlPolicy = participants_and_controllers_can_control
```

含义：

```text
同用户下的 broadcast participants 和 roles 包含 controller 的控制端都可以改队列、切歌、暂停、seek。
controller 不会因此成为 participant，也不会收到音频执行命令。
```

后续可扩展：

```text
owner_only:
  只有 ownerClientId 可以控制。

controllers_only:
  ownerClientId 和 roles 包含 controller 的设备可以控制。

participants_can_control:
  ownerClientId 和所有参与设备可以控制。

participants_and_controllers_can_control:
  ownerClientId、参与设备和同用户 controller 都可以控制。
```

权限校验函数建议：

```python
_can_control_broadcast(client, broadcast):
    if client.userName != broadcast.userName:
        return False

    if client.clientId == broadcast.ownerClientId:
        return True

    is_participant = client.clientId in broadcast.participants
    is_controller = "controller" in client.roles

    if broadcast.controlPolicy == "participants_can_control":
        return is_participant

    if broadcast.controlPolicy == "controllers_only":
        return is_controller

    if broadcast.controlPolicy == "participants_and_controllers_can_control":
        return is_participant or is_controller

    if broadcast.controlPolicy == "owner_only":
        return False

    return False
```

---

## 十、版本控制

群播允许多台设备共同修改队列，所以必须有版本号。

字段：

```text
version
baseVersion
updatedByClientId
updatedAt
```

### 推荐第一版策略

采用严格冲突检测：

```text
对 broadcast.queue.sync 和 broadcast.playItem：
  payload.baseVersion 必须存在。

如果 payload.baseVersion 不等于当前 BroadcastState.version：
  返回 system.error code = conflict
```

错误示例：

```json
{
  "type": "system",
  "action": "system.error",
  "payload": {
    "code": "conflict",
    "message": "Broadcast queue version conflict",
    "currentVersion": 3
  }
}
```

对 broadcast.play / pause / seek / stop：

```text
第一版可以不强制 baseVersion，但每次权威状态变更都必须 version + 1。
如果后续出现控制竞争，再把 baseVersion 扩展到所有控制动作。
```

---

## 十一、播放同步策略

第一版群播不是严格毫秒级同步。

采用普通同步：

```text
1. 服务端向所有 participants 下发同一条 broadcast command。
2. 每台设备收到后立即执行。
3. 每台设备上报 playback.update。
4. UI 根据 syncDriftMs 显示偏差。
```

### 可选增强：serverStartAt

如果要让多设备更接近同时播放，可以增加：

```json
{
  "serverStartAt": 1770000001.500
}
```

含义：

```text
所有设备在服务器时间 1770000001.500 开始播放。
```

第一版可以先保留字段，但不强制实现。

---

## 十二、设备上线后的加入策略

第一版不做自动加入，也不做手动 `broadcast.join / broadcast.leave`。

原因：

```text
1. 避免设备刚上线就莫名开始播放。
2. 避免第一版同时处理 participant 生命周期、恢复策略和权限边界。
3. 先把 start 时选定的一组在线 player 闭环跑通。
```

后续 v1.1 再做：

```text
broadcast.join
broadcast.leave
participants.update
```

自动加入也放到后续阶段：

```text
autoJoinBroadcast = true
```

自动加入逻辑可以放到 `device.register` 后：

```text
如果当前用户有 active broadcast，并且设备 roles 包含 player：
  自动 join 最新 active broadcast
```

第一版不建议直接自动加入，避免用户刚上线就莫名播放。

---

## 十三、后端文件修改建议

### 1. `supysonic/emo/ws_state.py`

新增：

```text
_broadcasts
_broadcast_participants
_broadcast_playback_states
_client_active_broadcast
```

新增 broadcast 相关状态方法。

---

### 2. `supysonic/emo/ws.py`

新增 action 分类：

```python
BROADCAST_ACTIONS = {
    "broadcast.start",
    "broadcast.stop",
    "broadcast.queue.sync",
    "broadcast.playItem",
    "broadcast.play",
    "broadcast.pause",
    "broadcast.seek",
    "broadcast.status",
}
```

在 `on_message` 中增加处理分支。

新增函数：

```python
_handle_broadcast_start(...)
_handle_broadcast_stop(...)
_handle_broadcast_queue_sync(...)
_handle_broadcast_play_item(...)
_handle_broadcast_play(...)
_handle_broadcast_pause(...)
_handle_broadcast_seek(...)
_handle_broadcast_status(...)
_forward_broadcast_command(...)
_broadcast_broadcast_state(...)
_broadcast_participants_update(...)
_validate_broadcast_queue_payload(...)
_can_control_broadcast(...)
```

---

### 3. `supysonic/db_layer/emo.py`

第一版如果只做内存，可以不改。

如果要持久化，再新增：

```text
EmoBroadcast
EmoBroadcastParticipant
EmoBroadcastPlaybackState
```

---

### 4. `tests/base/test_emo_ws.py`

新增 broadcast 测试。

---

## 十四、客户端行为

### 1. 收到 broadcast.start

客户端应：

```text
1. mode = broadcast。
2. activeBroadcastId = payload.broadcastId。
3. 保存 broadcast queue。
4. 加载 currentIndex 对应歌曲。
5. seek 到 positionMs。
6. autoPlay 为 true 时开始播放。
7. 上报 playback.update，带 broadcastId。
```

---

### 2. 收到 broadcast.queue.sync

客户端应：

```text
1. 校验 payload.broadcastId == activeBroadcastId。
2. 更新本地 broadcast queue。
3. 更新 version。
4. 不要随意切歌，除非 currentIndex 指向的歌曲变化，或者后续收到 broadcast.playItem。
```

---

### 3. 收到 broadcast.playItem

客户端应：

```text
1. 校验 activeBroadcastId。
2. 从 broadcast queue 中取 queueIndex。
3. 加载对应 trackId。
4. seek 到 positionMs。
5. 开始播放。
6. 上报 playback.update。
```

---

### 4. 收到 broadcast.pause

客户端应：

```text
1. 暂停当前播放。
2. 上报 playback.update。
```

---

### 5. 收到 broadcast.seek

客户端应：

```text
1. seek 到 positionMs。
2. 如果 state 是 playing，继续播放。
3. 上报 playback.update。
```

---

### 6. 收到 broadcast.stop

客户端应：

```text
1. 退出 broadcast mode。
2. activeBroadcastId = null。
3. 停止播放或恢复单播状态。
```

第一版建议停止播放。

---

## 十五、UI 要求

v1 UI 分两类：

```text
Web 控制台：
  只展示和控制 broadcast 协议状态，不执行音频播放。

Flutter / player 客户端：
  执行 broadcast.start / queue.sync / playItem / play / pause / seek / stop 等音频命令。
```

### 1. 设备列表

增加群播入口：

```text
开始群播
结束群播
```

`加入群播` / `退出群播` 入口留到 v1.1。

设备选择：

```text
选择参与设备：
  [x] 手机
  [x] 电脑
  [x] 音箱
```

---

### 2. 群播状态面板

显示：

```text
播放模式：群播
broadcastId：broadcast-001
发起设备：phone-1
参与设备：phone-1、pc-1、speaker-1
当前歌曲：song-1
当前进度：00:30
队列版本：v3
最近修改：pc-1
```

---

### 3. 参与设备状态

每台设备显示：

```text
设备名
在线 / 离线
播放状态
当前进度
同步偏差 syncDriftMs
错误状态 errorCode / errorMessage
```

---

### 4. 群播控制按钮

```text
添加歌曲
删除歌曲
播放指定歌曲
暂停全部
继续全部
拖动进度
结束群播
```

Web 控制台第一版应至少支持：

```text
1. 用当前队列或测试队列发起 broadcast.start。
2. 选择 allOnlinePlayers 或 selectedClients。
3. 展示 broadcastId、state、trackId、version、participants。
4. 展示 participantStates。
5. 发送 broadcast.queue.sync，必须带 baseVersion。
6. 发送 broadcast.playItem，必须带 baseVersion。
7. 发送 broadcast.play / pause / seek / stop。
8. 不在 Web 控制台执行音频播放。
```

---

## 十六、异常处理

### 1. 目标设备离线

`broadcast.start` 时，如果目标设备离线：

```text
忽略离线设备
ack 中返回 skippedClientIds
```

返回示例：

```json
{
  "started": true,
  "broadcastId": "broadcast-001",
  "participants": ["phone-1", "pc-1"],
  "skippedClientIds": ["speaker-1"]
}
```

---

### 2. 群播中设备掉线

如果 participant 掉线：

```text
1. 标记该 participant offline。
2. 不立即删除 participant。
3. UI 显示离线。
4. 如果设备重新上线，可手动 join 或后续自动恢复。
```

---

### 3. 队列为空

如果 `queueSongIds` 为空：

```text
broadcast.start 可以创建 stopped 状态。
autoPlay 必须为 false。
currentIndex = 0。
positionMs = 0。
```

---

### 4. 歌曲加载失败

参与设备加载失败时：

```json
{
  "action": "playback.update",
  "payload": {
    "mode": "broadcast",
    "broadcastId": "broadcast-001",
    "state": "error",
    "trackId": "song-1",
    "errorCode": "track_load_failed",
    "errorMessage": "Failed to load track"
  }
}
```

服务端记录 participant state，但不要因此停止整个 broadcast。

---

## 十七、测试目标

### 1. broadcast.start selected clients

场景：

```text
phone-1 发起 broadcast.start
targetClientIds = [phone-1, pc-1]
```

预期：

```text
服务端创建 broadcastId
participants 包含 phone-1 和 pc-1
phone-1 和 pc-1 都收到 broadcast.start
ack 返回 broadcast state
```

---

### 2. broadcast.start all online players

场景：

```text
phone-1 发起 allOnlinePlayers
同用户下有 phone-1、pc-1、web-control-1
```

预期：

```text
只选择 roles 包含 player 的设备
不选择纯 controller
```

---

### 3. cross-user forbidden

场景：

```text
userA 试图把 userB 的设备加入 broadcast
```

预期：

```text
返回 forbidden
```

---

### 4. controller can start without becoming participant

场景：

```text
web-control-1 是纯 controller
web-control-1 发起 broadcast.start
targetClientIds = [phone-1, pc-1]
```

预期：

```text
服务端创建 broadcast
participants 只包含 phone-1 和 pc-1
web-control-1 不在 participants 中
web-control-1 可以继续发送有权限的 broadcast 控制动作
web-control-1 不收到音频执行 command
```

---

### 5. broadcast.queue.sync

场景：

```text
pc-1 是 broadcast participant
pc-1 修改队列
```

预期：

```text
BroadcastState.queueSongIds 更新
version + 1
所有 participants 收到 broadcast.queue.sync
updatedByClientId = pc-1
```

---

### 6. queue version conflict

场景：

```text
当前 version = 3
client 提交 baseVersion = 2
```

预期：

```text
返回 system.error code = conflict
BroadcastState 不被覆盖
```

---

### 7. broadcast.playItem

场景：

```text
pc-1 发起 broadcast.playItem queueIndex = 1
```

预期：

```text
BroadcastState.currentIndex = 1
BroadcastState.state = playing
所有 participants 收到 broadcast.playItem
```

---

### 8. broadcast.seek

场景：

```text
phone-1 发起 broadcast.seek positionMs = 45000
```

预期：

```text
BroadcastState.positionMs = 45000
所有 participants 收到 broadcast.seek
```

---

### 9. broadcast.pause

场景：

```text
phone-1 发起 broadcast.pause
```

预期：

```text
BroadcastState.state = paused
所有 participants 收到 broadcast.pause
```

---

### 10. playback.update with broadcastId

场景：

```text
pc-1 上报 playback.update，payload 带 broadcastId
```

预期：

```text
普通 playback state 正常保存
broadcast participant state 也被更新
```

---

### 11. broadcast.stop

场景：

```text
owner 发起 broadcast.stop
```

预期：

```text
所有 participants 收到 broadcast.stop
activeBroadcastId 清理
BroadcastState.state = stopped
```

---

### 12. Web 控制台展示和控制

场景：

```text
Web 控制台作为纯 controller 打开。
```

预期：

```text
页面包含群播状态面板和控制按钮
发起 broadcast.start 时不把 Web 控制台加入 participants
broadcast.queue.sync 和 broadcast.playItem 都带 baseVersion
收到 ack 或 broadcast.status 后更新展示
Web 控制台不执行音频播放
```

---

## 十八、验收标准

本阶段完成后，应满足：

1. 用户可以选择多台在线 player 设备发起群播。
2. 服务端生成唯一 broadcastId。
3. 群播队列归属于 broadcastId，而不是任何一个设备 sessionId。
4. 参与设备不切换自己的 sessionId。
5. 参与设备收到 broadcast.start 后可以加载队列并播放。
6. 纯 controller 可以发起和控制群播，但不会进入 participants，也不会收到音频执行命令。
7. 任意有权限的 participant 或 controller 可以修改群播队列。
8. 群播队列修改后，所有 participants 都能收到同步。
9. 任意有权限的 participant 或 controller 可以执行 playItem、pause、seek。
10. 所有参与设备继续用 playback.update 上报执行状态。
11. playback.update 带 broadcastId 时，服务端能记录 participant 状态。
12. 群播控制不能跨用户。
13. baseVersion 能够防止旧状态覆盖新状态。
14. broadcast.stop 后所有参与设备退出群播。
15. 第一版不实现 broadcast.join / broadcast.leave / ready gate / 持久化恢复。
16. Web 控制台可以展示和发送群播协议命令，但不执行音频播放。
17. 文档中提供的协议字段足够 Flutter / player 客户端实现群播执行。
18. 不破坏现有单播和跟播能力。
19. 原有测试全部通过，并新增 broadcast 相关测试。

---

## 十九、实现顺序建议

建议 Codex 按以下顺序实现：

```text
1. 在 ws_state.py 增加 broadcast 内存状态结构。
2. 在 ws.py 增加 BROADCAST_ACTIONS 和基础分支。
3. 实现 broadcast.start。
4. 实现向 participants 转发 broadcast.start。
5. 实现 playback.update 中 broadcastId participant state 记录。
6. 实现 broadcast.status。
7. 实现 broadcast.queue.sync 和 version。
8. 实现 broadcast.playItem。
9. 实现 broadcast.pause / broadcast.play / broadcast.seek。
10. 实现 broadcast.stop。
11. 补充 Web 控制台展示和控制入口。
12. 补充后端测试和 Web 控制台静态/渲染测试。
13. 整理 Flutter / player 客户端实现协议说明。
14. 跑现有测试，确保单播和跟播不受影响。
```

第一版先不要做复杂的自动加入、严格多设备毫秒同步、持久化恢复。

先把这个闭环跑通：

```text
创建群播 -> 多设备收到队列 -> 多设备播放 -> 设备上报状态 -> 任意参与者改队列/进度 -> 所有设备同步 -> 停止群播
```
