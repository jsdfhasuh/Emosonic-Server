# Goal: 实现 Emosonic 跟播 Follow Playback v1

## 一、总体目标

在现有 SocketIO 播放体系基础上，实现第一版“跟播”能力。

跟播定义：

一台设备可以跟随另一台设备播放。被跟随设备是权威播放源，跟随设备不切换自己的 `sessionId`，只复制源设备的队列、当前歌曲、播放状态和播放进度。

本阶段不实现完整群播 `broadcastId`，只完成跟播 v1。

---

## 二、当前代码基础

当前系统已有：

1. `clientId`

   * 用于设备识别和命令路由。

2. `sessionId`

   * 用于设备自己的播放上下文。
   * 用于 session queue、local queue、playback state 的作用域。

3. `session.subscribe`

   * 可以让一个设备订阅另一个设备所在的 `sessionId`。
   * 订阅成功后，服务端会推送该 session 的 queue 和 playback snapshot。

4. `queue.session.sync`

   * 同步 session 共享队列。

5. `queue.local.set`

   * 同步某台设备的 local queue。
   * local queue 的 owner 应使用 `payload.clientId`；如果缺省，则使用当前上报连接的 `clientId`。
   * 当前已有 `targetClientId` 定向推送能力。

6. `playback.update`

   * 播放设备上报当前播放状态。
   * 服务端会把该 session 下播放状态广播给同 session 设备和订阅者。

本阶段应优先复用这些现有能力，避免大规模重构。

---

## 三、本阶段不做的内容

本阶段不要实现完整群播，不要新增 `broadcastId` 相关数据库表。

暂不实现：

1. `broadcast.start`
2. `broadcast.stop`
3. `broadcast.queue.sync`
4. `broadcastId -> participants`
5. 群播共享队列表
6. 群播权限控制
7. 多设备共同编辑同一份 broadcast queue

这些留到下一阶段。

---

## 四、本阶段要实现的核心能力

## 1. 客户端跟播状态

在客户端维护跟播状态：

```json
{
  "mode": "follow",
  "followSourceClientId": "phone-1",
  "followSessionId": "root:phone",
  "followQueue": true,
  "followPlayback": true
}
```

含义：

* 当前设备正在跟随 `phone-1`。
* 当前设备自己的 `sessionId` 不变。
* 当前设备订阅 `phone-1` 所在的 `sessionId`。
* 当前设备只执行来自 `phone-1` 的播放状态和 local queue。
* session queue 按 `followSessionId` 接收，因为它是会话共享队列，`sourceClientId` 只表示最后提交者。

---

## 2. 开始跟播流程

当用户在设备 A 上选择“跟随设备 B 播放”时：

1. 从 `device.list` 中拿到设备 B 的：

   * `clientId`
   * `sessionId`

2. 设备 A 发送：

```json
{
  "type": "state",
  "action": "session.subscribe",
  "payload": {
    "sessionId": "设备B的sessionId"
  }
}
```

3. 设备 A 本地记录：

```json
{
  "mode": "follow",
  "followSourceClientId": "设备B的clientId",
  "followSessionId": "设备B的sessionId",
  "followQueue": true,
  "followPlayback": true
}
```

4. 服务端会推送该 session 的：

   * `queue.session.sync`
   * `queue.local.set`
   * `playback.update`

5. 设备 A 执行状态时按以下规则过滤：

   * `queue.session.sync`：接收 `payload.sessionId == followSessionId` 的共享队列。
   * `queue.local.set`：只执行 `payload.sourceClientId == followSourceClientId` 的本地队列。
   * `playback.update`：只执行 `payload.sourceClientId == followSourceClientId` 的播放状态。

---

## 3. 停止跟播流程

用户点击“停止跟播”时：

1. 发送：

```json
{
  "type": "state",
  "action": "session.unsubscribe",
  "payload": {
    "sessionId": "followSessionId"
  }
}
```

2. 清空本地跟播状态：

```json
{
  "mode": "solo",
  "followSourceClientId": null,
  "followSessionId": null,
  "followQueue": false,
  "followPlayback": false
}
```

3. 停止根据源设备状态自动播放、切歌、seek、暂停。

设备自己的 `sessionId` 不变。

---

## 五、队列跟随规则

跟播设备收到队列事件时，需要区分 session queue 和 local queue。

当前 v1 约定：

* session queue 是 `sessionId` 级别的共享队列。
* local queue 是 `sessionId + clientId` 级别的设备队列。
* 播放设备应尽量保持自己的 local queue 与所在 session queue 一致。
* 如果两者同时存在且不一致，跟播执行优先参考源设备的 `playback.update.trackId/currentIndex/positionMs`，不要仅凭任意队列事件自动切歌。

## 1. 收到 `queue.session.sync`

如果当前处于 follow 模式：

```text
if mode == "follow"
and payload.sessionId == followSessionId:
    同步该队列为当前跟播队列
else:
    只更新 UI，不执行播放
```

处理内容：

* `queueSongIds`
* `currentIndex`
* `positionMs`

如果 `followQueue = true`，跟随设备需要把这份队列作为当前跟播 session queue。

注意：不要用 `queue.session.sync.sourceClientId` 判断是否来自源设备。该字段只表示最后一次提交 session queue 的设备，可能是控制器。

---

## 2. 收到 `queue.local.set`

如果当前处于 follow 模式：

```text
if mode == "follow"
and payload.sourceClientId == followSourceClientId:
    同步该 local queue 为当前跟播队列
else:
    只更新 UI，不执行播放
```

这里要注意：

* `queue.local.set` 可能来自同 session 下其他设备。
* 必须根据 `sourceClientId` 过滤。
* 不要收到任何 local queue 都自动播放。

---

## 六、播放状态跟随规则

跟播设备收到 `playback.update` 时：

```text
if mode == "follow"
and payload.sourceClientId == followSourceClientId:
    根据源设备播放状态执行跟播
else:
    只更新 UI，不执行播放
```

需要同步的字段：

```text
trackId
state
currentIndex
positionMs
updatedAt
volume，可选
playbackRate，可选
queueType，可选，建议值为 session/local
queueClientId，可选，local queue 时建议为源设备 clientId
```

其中：

* `state = playing`：跟随设备播放。
* `state = paused`：跟随设备暂停。
* `state = stopped`：跟随设备停止或保持空闲。
* `trackId` 改变：切换到对应歌曲。
* `positionMs` 改变：按进度同步策略校正。
* `queueType/queueClientId` 缺失时：按 `trackId` 和 `currentIndex` 在源 session queue / 源 local queue 中匹配，匹配不到时以 `trackId` 直接播放。

---

## 七、跟播进度同步策略

跟播必须同步播放进度。

但不要高频逐毫秒同步，采用以下策略：

```text
关键事件立即同步
播放中低频同步
跟随端本地推算进度
偏差超过阈值再 seek 校正
```

## 1. 目标进度计算

`updatedAt` 使用服务端 epoch seconds。客户端不要直接假设本机时钟与服务端完全一致，建议从收到的每条服务端消息顶层 `timestamp` 估算：

```text
serverClockOffsetMs = message.timestamp * 1000 - Date.now()
serverNowMs = Date.now() + serverClockOffsetMs
```

如果源设备状态是 `playing`：

```text
targetPositionMs = source.positionMs + (serverNowMs - source.updatedAt * 1000)
```

如果源设备状态是 `paused`：

```text
targetPositionMs = source.positionMs
```

---

## 2. 偏差处理

设：

```text
driftMs = localPositionMs - targetPositionMs
```

建议策略：

```text
abs(driftMs) < 300ms:
    不处理

300ms <= abs(driftMs) < 1000ms:
    暂不强制 seek，等待下一次同步

abs(driftMs) >= 1000ms:
    直接 seek 到 targetPositionMs
```

---

## 3. 关键事件必须立即同步

以下情况不要等定时同步：

1. 源设备播放。
2. 源设备暂停。
3. 源设备 seek。
4. 源设备切歌。
5. 源设备下一首。
6. 源设备上一首。
7. 源设备队列变化。
8. 源设备播放结束自动切到下一首。

---

## 八、跟随设备状态上报

跟随设备仍然可以继续上报自己的 `playback.update`。

但要带上跟播信息：

```json
{
  "type": "event",
  "action": "playback.update",
  "payload": {
    "sessionId": "root:pc",
    "mode": "follow",
    "followSourceClientId": "phone-1",
    "state": "playing",
    "trackId": "song-2",
    "positionMs": 32300,
    "syncDriftMs": -200
  }
}
```

注意：

跟随设备的状态只是执行反馈，不能反向覆盖源设备状态。

也就是说：

```text
source playback state = 权威状态
follower playback state = 执行反馈
```

不要把 follower 的 `playback.update` 再同步回 source 形成循环。

---

## 九、UI 要求

## 1. 设备列表

在设备列表中增加操作：

```text
跟随此设备播放
停止跟播
```

设备卡片建议显示：

```text
设备名
clientId
sessionId
当前播放状态
是否可跟随
```

---

## 2. 跟播状态展示

当当前设备处于跟播模式时，显示：

```text
播放模式：跟播
正在跟随：phone-1
源设备进度：xx:xx
本机进度：xx:xx
同步偏差：200ms
状态：正常 / 校正中 / 源设备离线
```

---

## 3. 跟播时的操作限制

跟播模式下，当前设备本地编辑队列时需要提示：

```text
当前正在跟播，编辑本机队列会退出跟播。
```

可以先做简单策略：

* 跟播中禁用本机队列编辑。
* 用户点击本机播放其他歌曲时，自动停止跟播。

---

## 十、异常处理

## 1. 源设备离线

如果 `followSourceClientId` 对应设备离线：

```text
1. 显示“源设备已离线”
2. 停止继续跟随
3. 本机可以保持当前播放，也可以暂停
```

第一版建议：

```text
源设备离线后，本机继续播放当前歌曲，但退出 follow 模式。
```

---

## 2. 源设备无队列

如果源设备没有队列：

```text
1. 不自动播放
2. UI 显示“源设备暂无播放队列”
```

---

## 3. 歌曲加载失败

如果跟随设备无法加载源设备当前歌曲：

```text
1. 上报 playback.update，state = error
2. 带 errorCode / errorMessage
3. UI 显示“跟播失败：歌曲加载失败”
```

---

## 十一、后端是否需要改动

第一版尽量不大改后端。

优先只做必要补强：

1. 确认 `session.subscribe` 可以稳定订阅同用户下其他设备的 `sessionId`。
2. 确认订阅后会推送 session snapshot。
3. 确认 `playback.update`、`queue.session.sync`、`queue.local.set` 会广播给订阅者。
4. 如果 `updatedAt` 缺失或不稳定，需要保证所有 playback payload 都包含 `updatedAt`。
5. 如果客户端无法区分 source，需要确保所有 queue/playback 状态都带 `sourceClientId`。
6. `queue.local.set` 保存 owner 应优先使用 `payload.clientId`，并校验该 client 属于当前用户和目标 `sessionId`。
7. Web 控制台只做跟播状态观察、订阅和协议展示，不在浏览器里执行真实音频跟播。

暂时不要新增数据库表。

---

## 十二、测试目标

需要补充或检查以下测试。

## 1. session subscribe snapshot 测试

场景：

```text
pc-1 subscribe phone-1 session
```

预期：

```text
pc-1 收到 phone-1 session 的 queue.session.sync
pc-1 收到 phone-1 session 的 playback.update
```

---

## 2. playback update to subscriber 测试

场景：

```text
phone-1 更新 playback.update
pc-1 已订阅 phone-1 session
```

预期：

```text
pc-1 收到 playback.update
payload.sourceClientId = phone-1
```

---

## 3. queue update to subscriber 测试

场景：

```text
phone-1 更新 queue.session.sync
pc-1 已订阅 phone-1 session
```

预期：

```text
pc-1 收到 queue.session.sync
payload.sessionId = phone-1 的 sessionId
payload.sourceClientId 可以是 phone-1，也可以是提交该 session queue 的 controller
```

---

## 4. local queue target test

场景：

```text
phone-1 queue.local.set targetClientId = pc-1
```

预期：

```text
pc-1 收到 queue.local.set
payload.sourceClientId = phone-1
```

---

## 5. local queue owner test

场景：

```text
controller-1 queue.local.set
payload.sessionId = root:phone
payload.clientId = phone-1
```

预期：

```text
服务端保存 root:phone + phone-1 的 local queue
广播 payload.sourceClientId = phone-1
不会保存为 controller-1 的 local queue
```

---

## 6. 客户端 follow filtering 测试

场景：

```text
pc-1 订阅 root:phone
root:phone session 下有 phone-1 和 phone-2 状态
```

预期：

```text
pc-1 只执行 sourceClientId = followSourceClientId 的播放状态
pc-1 接收 followSessionId 的 session queue，不按 session queue sourceClientId 过滤
其他状态只更新 UI，不执行播放
```

---

## 十三、验收标准

本阶段完成后，应满足：

1. 用户可以在设备列表中选择“跟随某台设备播放”。
2. 跟随设备不切换自己的 `sessionId`。
3. 跟随设备可以收到源设备的队列和播放状态。
4. 跟随设备只执行指定 `followSourceClientId` 的播放状态和 local queue。
5. 源设备播放、暂停、切歌、seek 时，跟随设备能同步执行。
6. 跟随设备根据 `positionMs + updatedAt` 计算目标进度。
7. 跟随设备进度偏差过大时自动 seek 校正。
8. 停止跟播后，跟随设备不再响应源设备状态。
9. 源设备离线时，跟随设备能退出 follow 模式或提示异常。
10. 不引入 `broadcastId` 数据库结构。
11. 不破坏现有单播控制能力。
12. 原有测试通过，并新增跟播相关测试。

---

## 十四、后续阶段预留

跟播 v1 完成后，再进入群播阶段。

群播阶段再新增：

```text
broadcastId
BroadcastState
participants
broadcast queue
broadcast version
broadcast control policy
```

对应事件：

```text
broadcast.start
broadcast.stop
broadcast.queue.sync
broadcast.playItem
broadcast.seek
broadcast.pause
```

本次任务不要提前实现这些。
