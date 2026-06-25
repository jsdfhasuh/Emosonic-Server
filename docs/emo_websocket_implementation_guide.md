# Emo WebSocket 实施与学习指南

这份文档按“你熟悉 REST，还没做过 WebSocket”的前提来写。

## 1. 先理解思维差异

### REST 的思维

- 设计 URL
- 设计 HTTP method
- 设计 request body
- 返回 response body

### WebSocket 的思维

- 先建立连接
- 连接成功后做认证
- 双方持续发送消息
- 服务端维护在线连接和共享状态

Socket.IO 不是“很多个实时 REST 接口”，而是“一个连接上的事件协议 + 消息协议”。

## 2. 在这个项目里怎么放置

推荐职责划分：

- `HTTP` 继续负责登录、媒体查询、页面加载、非实时操作
- `Socket.IO` 负责实时控制、状态推送、队列同步、设备在线状态

对应文件建议：

- `supysonic/emo/client.py`: 普通 HTTP 接口
- `supysonic/emo/ws.py`: Socket.IO 接入与消息分发
- `supysonic/emo/ws_state.py`: 在线连接、设备、队列、播放状态

## 3. 第一版只做最小闭环

第一版不要追求完整播放器平台，只做下面这几个能力：

1. 客户端通过 Socket.IO 连接服务端，命名空间是 `/emo`
2. 客户端发送 `auth.login`
3. 客户端发送 `device.register`
4. 控制端查看设备列表
5. 控制端给目标播放器发送 `player.pause`
6. 播放器返回 `playback.update`
7. 服务端广播新的状态

只要这条链路跑通，你就已经完成了最核心的学习目标。

## 4. 推荐开发顺序

### Step 1: 只实现连接

- 服务端能接受 Socket.IO 连接
- 服务端能收一条文本消息
- 服务端能发回一条固定消息

学习重点：连接生命周期。

### Step 2: 实现 `system.ping` / `system.pong`

这是最适合练手的第一条消息。

学习重点：消息解析和消息回包。

### Step 3: 实现 `auth.login`

- 在连接建立后要求客户端先认证
- 认证成功后才能执行业务消息

学习重点：Socket.IO 连接和用户身份绑定。

### Step 4: 实现 `device.register`

- 客户端注册 `clientId`
- 客户端注册设备名称和角色
- 服务端保存在线设备表

学习重点：连接不等于设备，设备不等于用户。

`device.register` payload 里的名称字段建议这样区分：

- `clientId`: 唯一通讯 ID，用于 `targetClientId` 控制转发
- `deviceName`: 设备原始名称
- `alias`: 用户自定义显示名称，用于控制端展示

控制端显示建议：

```javascript
displayName = device.alias || device.deviceName || device.clientId
```

示例：

```json
{
  "type": "device",
  "action": "device.register",
  "requestId": "register-player-1",
  "payload": {
    "clientId": "player-1",
    "deviceName": "Windows Player",
    "alias": "电脑播放器",
    "roles": ["player"],
    "sessionId": "sess-main"
  }
}
```

### Step 5: 实现定向消息路由

- 一个客户端指定 `targetClientId`
- 服务端找到目标连接并转发

学习重点：实时系统最重要的不是广播，而是“路由到正确连接”。

### Step 6: 实现状态回传

- 播放器执行命令后，主动上报 `playback.update`
- 服务端更新共享状态后，再推送给其他相关客户端

学习重点：服务端权威状态。

### Step 7: 实现 `queue.session.sync`

- 服务端持有当前会话队列
- 队列变化后按 `sessionId` 广播给所有相关客户端

学习重点：共享状态同步。

## 5. 你写代码时要刻意区分的三类消息

### 1) Command

谁要别人做什么。

例子：

- `player.pause`
- `player.seek`

### 2) Event

某个客户端告诉服务端“我发生了什么”。

例子：

- `playback.update`

### 3) State

服务端把当前权威状态推给客户端。

例子：

- `queue.session.sync`
- `device.list`

这是实时消息工程里非常重要的分层。

## 6. 最容易犯的错误

- 把 Socket.IO 当成没有 URL 的 REST
- 只做消息转发，不做服务端状态
- 没有心跳
- 没有 `requestId`
- 没有连接清理
- 没有认证门槛
- 让多个客户端都能随便改同一个设备而不做约束

## 7. 在这个仓库里的手改步骤

### 文件 1: `supysonic/emo/ws_state.py`

先写一个最小状态管理器：

- 保存 `clientId -> sid`
- 保存 `clientId -> device info`
- 保存 `sessionId -> queueSongIds/currentIndex/positionMs`
- 保存 `sessionId -> playbackState`
- 提供注册、注销、列表、更新状态等方法

### 文件 2: `supysonic/emo/ws.py`

实现：

- Socket.IO namespace `/emo` 与 path `/emo/ws`
- 消息 JSON 解析
- 鉴权
- 消息路由
- 广播
- 断线清理

### 文件 3: `supysonic/emo/__init__.py`

- 导入 `ws.py`，让路由真正注册到蓝图上

### 文件 4: `supysonic/config.py`

- 补齐 `mount_emosonic` 默认值
- 可选补充 Socket.IO 开关或心跳配置

### 文件 5: `run_supysonic.py`

- 保持开发入口简洁
- 让本地开发可以直接调试普通 HTTP 和 Socket.IO

### 文件 6: `tests/base/test_emo_ws_state.py`

先补最小测试，测试状态管理器，而不是一上来做完整 WebSocket 集成测试。

## 8. 为什么先测状态管理器

因为 Socket.IO 集成测试比普通单元测试复杂很多。

你先把这些逻辑测稳：

- 注册设备
- 注销设备
- 更新队列
- 更新播放状态
- 列出在线设备

这样调实时连接时心里会更稳。

## 9. 第一版上线前至少手测这些场景

1. 未认证连接发业务消息，被拒绝
2. 认证成功后能注册设备
3. 设备列表能返回
4. 控制端能给目标客户端发 `pause`
5. 播放端能回传 `playback.update`
6. 服务端能把状态广播回控制端
7. 客户端断开后在线列表及时消失
8. 心跳超时后连接被清理

## 10. 你现在最值得学会的不是库，而是抽象

学习重点不是某个 Python Socket.IO 包怎么调用，而是这几个问题：

- 连接建立以后，身份怎么绑定？
- 消息怎么分类？
- 状态由谁说了算？
- 目标连接怎么找到？
- 断线以后怎么清理？

这些问题想明白了，你换任何实时通讯框架都能做。

## 11. 当前仓库里已经能复用的东西

- 用户认证能力：`supysonic/emo/__init__.py`
- 用户管理：`supysonic/managers/user.py`
- 任务状态雏形：`supysonic/TaskManger.py`
- 开发入口：`run_supysonic.py`

## 12. 推荐你的学习节奏

### Day 1

- 读协议文档
- 跑通连接
- 实现 ping/pong

### Day 2

- 实现 auth
- 实现 device.register
- 实现 device.list

### Day 3

- 实现 player.pause
- 实现 playback.update
- 实现最小广播

### Day 4

- 实现 queue.session.sync
- 做两个客户端互控演示

### Day 5

- 补测试
- 清理协议细节
- 整理异常处理和日志

## 13. 队列协议建议

建议把正式会话队列定义成 song-id 模式，而不是对象数组模式。

推荐 payload：

```json
{
  "sessionId": "root:living-room",
  "queueSongIds": ["songId1", "songId2", "songId3"],
  "currentIndex": 2,
  "positionMs": 8000
}
```

这样做的好处：

- 载荷更小
- 客户端状态更贴近真实播放器模型
- 服务端只需要维护队列引用和播放位置
- 曲目详情可以按 song id 单独获取

推荐规则：

- `queueSongIds` 必填
- `currentIndex` 必填
- `positionMs` 必填
- 服务端正式共享队列按 `sessionId` 归属
- 命令仍按 `clientId` 定向路由

## 14. 最终建议

- 第一版优先做“单进程开发态原型”
- 不要一开始就考虑多 worker 或跨进程广播
- 先把协议和状态模型学透，再谈生产级部署

## 15. 最小联调脚本

仓库里已经提供了一个最小示例脚本：`script/emo_ws_demo.py`

注意：这个脚本现在使用的是 `python-socketio` 客户端，不是原始 WebSocket 客户端。

先启动你的应用，然后开两个终端分别运行：

### 终端 1：播放器

```bash
/root/enter/envs/supysonic/bin/python script/emo_ws_demo.py player \
  --url http://127.0.0.1:5000 \
  --user alice \
  --password Alic3 \
  --client-id player-1 \
  --device-name "Living Room Player" \
  --session-id sess-main \
  --roles player controller
```

### 终端 2：控制端

```bash
/root/enter/envs/supysonic/bin/python script/emo_ws_demo.py controller \
  --url http://127.0.0.1:5000 \
  --user alice \
  --password Alic3 \
  --client-id controller-1 \
  --device-name "Phone Remote" \
  --session-id sess-main \
  --target-client-id player-1
```

控制端启动后会进入交互模式，可以手动输入：

```text
play
pause
next
prev
seek 15000
list
queue /path/to/queue.json
quit
```

如果你想先把队列同步给播放器，可以准备一个 JSON 文件，例如：

```json
[
  {"trackId": "1"},
  {"trackId": "2"}
]
```

然后这样运行控制端：

```bash
/root/enter/envs/supysonic/bin/python script/emo_ws_demo.py controller \
  --url http://127.0.0.1:5000 \
  --user alice \
  --password Alic3 \
  --client-id controller-1 \
  --device-name "Phone Remote" \
  --session-id sess-main \
  --queue-json /path/to/queue.json
```

这个脚本的目标不是做完整播放器，而是让你看到：

- 如何连接 Socket.IO
- 如何先认证再注册设备
- 如何从一个客户端向另一个客户端发命令
- 如何让播放器回传 `playback.update`
