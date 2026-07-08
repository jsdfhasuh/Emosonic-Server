# EmoSonic 播放上下文与无痕切换重构 Goal 计划

## 1. 目标

本次重构的目标是把当前混在一起的 `sessionId` 拆清楚，为后续“无痕切换播放 / 播放权转移 / 来回切换设备”打基础。

最终模型：

```text
deviceSessionId：设备自己的房间 / 连接归属 / 本地状态隔离
playbackContextId：一套播放任务，包括队列、歌曲、进度、播放状态
authorityClientId：当前哪个设备有权代表这套播放任务上报主播放状态
```

切换播放设备时，不再修改设备的 `sessionId`，只修改播放上下文里的 `authorityClientId`。

例如：

```text
phone-1.deviceSessionId = root:phone
pc-1.deviceSessionId    = root:pc

playbackContextId = playback:alice:main
authorityClientId = phone-1
```

手机切到电脑：

```text
authorityClientId: phone-1 -> pc-1
```

电脑切回手机：

```text
authorityClientId: pc-1 -> phone-1
```

整个过程里：

```text
deviceSessionId 不变
playbackContextId 不变
只切 authorityClientId
```

---

## 2. 当前仓库现状

当前代码里 `sessionId` 同时承担了两个职责：

```text
1. 设备自己的 session / 房间
2. 播放队列和播放状态的上下文 ID
```

数据库目前主要有三张表：

```text
EmoSessionQueue
EmoLocalQueue
EmoPlaybackState
```

其中 `EmoSessionQueue.session_id` 是唯一队列标识，`EmoLocalQueue` 和 `EmoPlaybackState` 都以 `(session_id, owner_client_id)` 作为唯一索引。也就是说，现在数据库层面已经把播放状态和 `sessionId` 绑定得比较深。

内存状态里也类似：

```python
_queues[sessionId]
_local_queues[(sessionId, clientId)]
_playback_states[(sessionId, clientId)]
_playback_timelines[timelineId]
```

现有代码已经有两阶段播放控制基础：

```text
playback.prepare
playback.ready
_pending_prepares
_commit_prepare
controlVersion
queueRevision
clientSeq
```

所以后续 handoff 不需要从零写一套协议，应该复用现有的 prepare / ready / commit 机制。

---

## 3. 核心设计原则

### 3.1 不再通过切换 session 实现播放切换

错误方向：

```text
手机 session -> 电脑 session
电脑 session -> 手机 session
```

正确方向：

```text
同一个 playbackContextId
不同设备之间切换 authorityClientId
```

### 3.2 服务器必须维护权威播放状态

handoff 不能相信客户端传来的旧队列、旧进度、旧播放状态。

服务器要以自己的 `PlaybackContext` 为准：

```text
当前队列
当前歌曲
当前进度
当前播放状态
当前 authorityClientId
当前 controlVersion
当前 queueRevision
```

### 3.3 非权威设备不能覆盖主播放状态

例如手机已经把播放权切给电脑：

```text
authorityClientId = pc-1
```

如果手机之后又发：

```json
{
  "action": "playback.update",
  "payload": {
    "playbackContextId": "playback:alice:main",
    "positionMs": 60000
  }
}
```

服务器只能把它保存成设备反馈，不能覆盖主播放状态。

返回：

```json
{
  "updated": true,
  "deviceFeedback": true,
  "authoritative": false,
  "currentAuthorityClientId": "pc-1"
}
```

---

## 4. 数据库重构计划

### 4.1 保留旧表

第一阶段不要删除旧表：

```text
emo_session_queue
emo_local_queue
emo_playback_state
```

它们用于兼容旧客户端和旧测试。

### 4.2 新增播放上下文表

新增模型：

```python
class EmoPlaybackContext(_Model):
    id = PrimaryKeyField()
    playback_context_id = CharField(128, unique=True)
    user_name = CharField(64)

    authority_client_id = CharField(128, null=True)
    origin_client_id = CharField(128, null=True)

    queue_json = TextField()
    current_index = IntegerField(default=0)
    track_id = CharField(128, null=True)
    state = CharField(32, default="stopped")
    position_ms = IntegerField(default=0)
    volume = IntegerField(null=True)

    queue_revision = IntegerField(default=1)
    control_version = IntegerField(default=1)
    version = IntegerField(default=1)
    epoch = IntegerField(default=1)

    playback_json = TextField(null=True)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)
```

这张表表示：

```text
这一套播放任务当前是什么状态
当前队列是什么
当前谁是权威播放器
```

### 4.3 新增设备播放反馈表

新增模型：

```python
class EmoDevicePlaybackState(_Model):
    id = PrimaryKeyField()
    playback_context_id = CharField(128)
    device_session_id = CharField(128)
    owner_client_id = CharField(128)
    user_name = CharField(64)

    state = CharField(32)
    track_id = CharField(128, null=True)
    position_ms = IntegerField(default=0)
    volume = IntegerField(null=True)

    is_authority = IntegerField(default=0)
    mode = CharField(32, default="normal")

    playback_json = TextField(null=True)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = ((("playback_context_id", "owner_client_id"), True),)
```

这张表表示：

```text
每台设备自己的播放反馈
```

重点是：

```text
EmoPlaybackContext 是主播放事实
EmoDevicePlaybackState 是设备反馈
```

### 4.4 可选新增 handoff 记录表

建议新增，方便排查问题：

```python
class EmoPlaybackHandoff(_Model):
    id = PrimaryKeyField()
    handoff_id = CharField(128, unique=True)
    request_id = CharField(128, null=True)

    playback_context_id = CharField(128)
    user_name = CharField(64)
    source_client_id = CharField(128)
    target_client_id = CharField(128)
    origin_client_id = CharField(128, null=True)

    status = CharField(32)
    base_control_version = IntegerField(default=0)

    snapshot_json = TextField(null=True)
    error_code = CharField(64, null=True)
    error_message = TextField(null=True)

    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)
```

---

## 5. 数据迁移计划

新增 schema version：

```python
SCHEMA_VERSION = "20260708"
```

新增迁移文件：

```text
supysonic/schema/migration/sqlite/20260708.sql
supysonic/schema/migration/postgres/20260708.sql
supysonic/schema/migration/mysql/20260708.sql
```

迁移逻辑：

```text
1. 创建 emo_playback_context
2. 创建 emo_device_playback_state
3. 创建 emo_playback_handoff
4. 把旧 emo_session_queue 数据迁移到 emo_playback_context
5. 把旧 emo_playback_state 数据迁移到 emo_device_playback_state
6. 不删除旧表
```

兼容规则：

```text
旧 session_id -> playbackContextId
旧 session_id -> deviceSessionId
```

---

## 6. WebSocket 协议改造

### 6.1 device.register

新客户端推荐传：

```json
{
  "clientId": "pc-1",
  "deviceSessionId": "root:pc",
  "roles": ["player"]
}
```

旧客户端仍然可以传：

```json
{
  "clientId": "pc-1",
  "sessionId": "root:pc",
  "roles": ["player"]
}
```

服务器内部统一成：

```text
deviceSessionId = payload.deviceSessionId or payload.sessionId or clientId
```

为了兼容，返回里可以同时带：

```json
{
  "clientId": "pc-1",
  "deviceSessionId": "root:pc",
  "sessionId": "root:pc"
}
```

### 6.2 queue.session.sync

旧 action 名可以先保留，但语义变成更新播放上下文队列。

新 payload：

```json
{
  "playbackContextId": "playback:alice:main",
  "deviceSessionId": "root:phone",
  "queueSongIds": ["song-1", "song-2"],
  "currentIndex": 0,
  "positionMs": 0,
  "baseQueueRevision": 1
}
```

旧 payload 继续兼容：

```json
{
  "sessionId": "root:phone",
  "queueSongIds": ["song-1"],
  "currentIndex": 0,
  "positionMs": 0
}
```

兼容映射：

```text
playbackContextId = payload.playbackContextId or payload.sessionId
deviceSessionId = payload.deviceSessionId or payload.sessionId or currentClient.deviceSessionId
```

### 6.3 playback.update

新规则：

```text
所有设备的 playback.update 都保存为设备反馈
只有 authorityClientId 对应的设备，才能更新 EmoPlaybackContext
```

权威设备更新：

```json
{
  "updated": true,
  "authoritative": true,
  "playbackContextId": "playback:alice:main",
  "authorityClientId": "pc-1"
}
```

非权威设备更新：

```json
{
  "updated": true,
  "deviceFeedback": true,
  "authoritative": false,
  "currentAuthorityClientId": "pc-1"
}
```

---

## 7. 内存状态改造

在 `WebSocketState.__init__()` 中新增：

```python
self._playback_contexts = {}
self._device_playback_states = {}
self._handoffs = {}
self._handoff_request_index = {}
self._playback_context_subscriptions = {}
```

保留旧字段用于兼容：

```python
self._queues
self._local_queues
self._playback_states
self._playback_timelines
```

新增核心方法：

```python
get_playback_context(playback_context_id)

restore_playback_context(playback_context_id, payload)

update_playback_context_queue(...)

record_device_playback_state(...)

apply_authority_playback_update(...)

transfer_playback_authority(...)
```

其中最重要的是：

```python
transfer_playback_authority(
    playback_context_id,
    source_client_id,
    target_client_id,
    expected_control_version=None,
)
```

它负责：

```text
检查 source 是当前 authority
检查 controlVersion 是否匹配
把 authorityClientId 切到 target
递增 controlVersion
递增 version
必要时递增 epoch
```

---

## 8. Handoff 协议设计

新增 action：

```text
playback.handoff.start
playback.handoff.cancel
playback.handoff.complete
```

复用已有：

```text
playback.prepare
playback.ready
```

完整流程：

```text
1. source 或控制端发送 playback.handoff.start
2. server 从 PlaybackContext 获取权威快照
3. server 给 target 发送 playback.prepare
4. target 加载歌曲、定位进度，回复 playback.ready
5. server 给 target 发送 player.play / commit
6. target 真正开始播放后发送 playback.handoff.complete
7. server 把 authorityClientId 从 source 切到 target
8. server 通知 source release / pause
9. server 广播新的 authoritative playback.update
```

---

## 9. Handoff Start

请求：

```json
{
  "action": "playback.handoff.start",
  "requestId": "handoff-1",
  "payload": {
    "playbackContextId": "playback:alice:main",
    "sourceClientId": "phone-1",
    "targetClientId": "pc-1",
    "baseControlVersion": 8
  }
}
```

服务端校验：

```text
sourceClientId 必须是当前 authorityClientId
targetClientId 必须在线
source 和 target 必须属于同一个用户
target 必须具备 player 能力
target 最好支持 playbackPrepare
baseControlVersion 必须匹配当前 controlVersion
同一个 playbackContextId 不能有未完成 handoff
```

注意：

```text
handoff 快照必须来自服务器 PlaybackContext
不能相信客户端 payload 里的 queue / position
```

---

## 10. Handoff Prepare

发送给 target：

```json
{
  "action": "playback.prepare",
  "payload": {
    "prepareId": "prepare-xxxx",
    "handoffId": "handoff-xxxx",
    "purpose": "handoff",

    "playbackContextId": "playback:alice:main",
    "deviceSessionId": "root:pc",

    "sourceClientId": "phone-1",
    "targetClientId": "pc-1",
    "authorityClientId": "phone-1",

    "queueSongIds": ["song-1", "song-2"],
    "currentIndex": 0,
    "trackId": "song-1",
    "positionMs": 30000,
    "state": "playing",

    "queueRevision": 3,
    "controlVersion": 9,
    "serverTimeMs": 1780000000000,
    "expiresAtServerMs": 1780000008000
  }
}
```

这里要特别注意：

```text
playbackContextId 是正在被接管的播放任务
deviceSessionId 是 target 自己的设备房间
二者不能混
```

---

## 11. Handoff Complete

target 播放成功后发送：

```json
{
  "action": "playback.handoff.complete",
  "requestId": "handoff-complete-1",
  "payload": {
    "handoffId": "handoff-xxxx",
    "playbackContextId": "playback:alice:main",
    "state": "playing",
    "trackId": "song-1",
    "positionMs": 30200,
    "controlVersion": 9
  }
}
```

服务端处理：

```text
1. 检查 handoff 存在
2. 检查发送者是 targetClientId
3. 检查 playbackContextId 匹配
4. 检查 controlVersion 匹配
5. 把 authorityClientId 改为 targetClientId
6. 保存新的 PlaybackContext
7. 广播 authoritative playback.update
8. 通知 source 释放播放权
```

广播示例：

```json
{
  "playbackContextId": "playback:alice:main",
  "authorityClientId": "pc-1",
  "sourceClientId": "pc-1",
  "state": "playing",
  "trackId": "song-1",
  "positionMs": 30200,
  "queueRevision": 3,
  "controlVersion": 9,
  "version": 11,
  "epoch": 3,
  "authoritative": true,
  "handoffId": "handoff-xxxx"
}
```

---

## 12. 失败和超时处理

建议超时时间：

```text
prepare timeout：8000ms
complete timeout：5000ms
```

失败规则：

```text
target 没 ready：source 继续保持 authority
target ready 但没 complete：source 继续保持 authority
handoff cancel：source 继续保持 authority
handoff completed 后再 cancel：忽略
```

失败返回：

```json
{
  "action": "playback.handoff.status",
  "payload": {
    "handoffId": "handoff-xxxx",
    "status": "timed_out",
    "authorityClientId": "phone-1",
    "sourceKeptAuthority": true
  }
}
```

---

## 13. 来回切换要求

必须支持：

```text
phone -> pc
pc -> phone
phone -> tablet
tablet -> web
```

关键要求：

```text
同一个 playbackContextId 保持不变
每次只改变 authorityClientId
deviceSessionId 永远不因为 handoff 改变
```

测试场景：

```text
1. phone 创建 playbackContext
2. phone 是 authority
3. phone -> pc handoff 成功
4. pc 成为 authority
5. pc -> phone handoff 成功
6. phone 再次成为 authority
7. playbackContextId 全程不变
8. phone 和 pc 的 deviceSessionId 全程不变
```

---

## 14. 测试计划

新增测试文件：

```text
tests/base/test_emo_playback_context.py
tests/base/test_emo_playback_handoff.py
tests/base/test_emo_playback_context_schema.py
```

核心测试：

```text
test_device_register_accepts_device_session_id
test_legacy_device_register_session_id_maps_to_device_session_id
test_queue_session_sync_creates_playback_context
test_legacy_queue_session_sync_maps_session_id_to_playback_context_id
test_authority_playback_update_updates_context
test_non_authority_playback_update_is_device_feedback_only
test_source_update_after_authority_transfer_does_not_override_target
test_handoff_start_sends_prepare_to_target_without_changing_device_session
test_handoff_start_requires_source_to_be_authority
test_handoff_ready_commits_target_play
test_handoff_complete_transfers_authority
test_handoff_failure_keeps_source_authority
test_handoff_can_switch_back
test_duplicate_handoff_start_request_is_idempotent
test_handoff_complete_from_wrong_client_is_forbidden
```

原有测试也必须继续通过：

```text
tests/base/test_emo_ws.py
```

---

## 15. 分阶段实施计划

### Phase 1：数据库和模型拆分

修改文件：

```text
supysonic/db_layer/emo.py
supysonic/db.py
supysonic/schema/sqlite.sql
supysonic/schema/postgres.sql
supysonic/schema/mysql.sql
supysonic/schema/migration/*/20260708.sql
supysonic/db_layer/schema.py
```

目标：

```text
新增 PlaybackContext / DevicePlaybackState / Handoff 表
旧表保留
迁移脚本可用
schema 测试通过
```

### Phase 2：设备注册和 ID 兼容

修改文件：

```text
supysonic/emo/ws.py
```

目标：

```text
支持 deviceSessionId
兼容旧 sessionId
返回里同时带 deviceSessionId 和 sessionId
```

### Phase 3：内存状态改造

修改文件：

```text
supysonic/emo/ws_state.py
```

目标：

```text
新增 _playback_contexts
新增 _device_playback_states
新增 authority 判断
新增 transfer_playback_authority
```

### Phase 4：持久化 store 改造

修改文件：

```text
supysonic/emo/ws_store.py
```

目标：

```text
新增 getPlaybackContextState
新增 savePlaybackContextState
新增 getDevicePlaybackState
新增 saveDevicePlaybackState
旧 getQueueState / getPlaybackState 保持兼容
```

### Phase 5：queue 和 playback.update 改造

修改文件：

```text
supysonic/emo/ws.py
```

目标：

```text
queue.session.sync 写入 PlaybackContext
playback.update 区分 authority 和 device feedback
非 authority 不能覆盖主播放状态
```

### Phase 6：handoff 协议实现

修改文件：

```text
supysonic/emo/ws.py
supysonic/emo/ws_state.py
supysonic/emo/ws_store.py
```

目标：

```text
实现 playback.handoff.start
实现 playback.handoff.cancel
实现 playback.handoff.complete
复用 playback.prepare / playback.ready
完成 authorityClientId 转移
```

### Phase 7：follow / broadcast 兼容检查

目标：

```text
现有 follow 测试继续通过
现有 broadcast 测试继续通过
broadcast participant 继续走 participant feedback
follow 后续再逐步改成 playbackContextId 模型
```

---

## 16. 验收标准

完成后必须满足：

```text
1. 新旧客户端都能注册设备
2. 旧 sessionId payload 仍然可用
3. 新 playbackContextId payload 可用
4. queue.session.sync 可以创建和更新 PlaybackContext
5. authority 设备的 playback.update 可以更新主播放状态
6. 非 authority 设备的 playback.update 只能作为设备反馈
7. phone -> pc handoff 可以成功
8. pc -> phone handoff 可以成功
9. handoff 失败时 source 继续播放
10. handoff 成功后 source 的延迟 update 不能覆盖 target
11. handoff 过程中 deviceSessionId 不发生变化
12. 同一个 playbackContextId 支持来回切换
13. 现有 test_emo_ws.py 不被破坏
```

---

## 17. 非目标

第一版不做：

```text
真正毫秒级无缝音频拼接
跨用户 handoff
多个 authority 同时控制一个 playbackContext
把 broadcast 完全合并进 playbackContext
删除旧 sessionId 字段
删除旧 EmoSessionQueue / EmoPlaybackState 表
大规模前端 UI 重构
```

第一版只解决核心架构问题：

```text
设备 session 和播放上下文拆开
服务器维护播放权
handoff 可以可靠切换和切回来
旧 source 不能覆盖新 target
```

---

## 18. 最终架构原则

后续代码里要遵守：

```text
sessionId：legacy 兼容字段
deviceSessionId：设备自己的房间
playbackContextId：正在播放的上下文
authorityClientId：当前权威播放器
```

一句话总结：

```text
不要通过切换 session 实现播放切换。
应该通过固定 playbackContextId，切换 authorityClientId 实现播放权转移。
```
