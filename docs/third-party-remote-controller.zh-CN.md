# 把第三方客户端注册为 EmoSonic 遥控器

一个家庭自动化程序、网页或桌面工具可以用同一个音乐账号注册独立遥控器，选择该账号的在线播放器，调节音量、切歌，或把播放从电脑交给音箱。以下示例使用 strict-v2 2.9，依据服务端 `9420b3a`。设备列表、会话、能力和播放版本都应在连接后读取。

## 第一次连接

1. 使用 Socket.IO 客户端连接服务器的 HTTP(S) origin，Engine.IO 路径设为 `/emo/ws`，Socket.IO namespace 设为 `/emo`。业务消息都发到该 namespace 的 `message` 事件。跨网络时用 HTTPS。
2. 先发 `auth.login`，字段是 `u` 和 `p`。服务程序可以使用该音乐账号的凭据。内置网页用已登录的网页会话领取一次性口令。凭据从密钥存储读取，不进 URL 和日志。
3. 收到登录成功后发 `device.register`，角色为 `["controller"]`。为这个安装实例生成自己的 `clientId` 和 `deviceSessionId`，保存供重连使用。两个同时运行的遥控器要使用不同的标识。音箱、手机已有的标识留给各自的播放器。
4. 注册应答的 `payload.clientId`、`deviceSessionId` 要与请求相同。`strictV2.protocolVersion` 的主版本须为 2、次版本至少为 9。检查完整的十项 `negotiatedCapabilities` 布尔值以及 `playbackContextV2`，后续按协商结果启用操作。应答外层和 `payload.strictV2` 的 `connectionNonce`、`connectionEpoch` 要一致。它们识别这次连接；部署的提交号和 schema 哈希仅供诊断。
5. 发送 `device.list`，在 `payload.devices` 中找含 `player` 角色的设备。保留选定设备的 `clientId` 与 `deviceSessionId`，显示名可能重复。

### 可直接运行的只读例子

安装 `python -m pip install "python-socketio[client]==5.17.0"`。以下代码保存为 `list_players.py`，设置环境变量 `EMOSONIC_URL`、`EMOSONIC_USER`、`EMOSONIC_CLIENT_ID` 和 `EMOSONIC_DEVICE_SESSION_ID` 后运行。后两个值可以用两个独立的 UUID，首次生成后保存。程序提示输入密码，仅注册和列出播放器。把这一步跑通，再添加控制命令。

```python
import getpass
import os
import queue
import time
import uuid

import socketio

namespace = "/emo"
client = socketio.Client(reconnection=False)
inbox = queue.Queue()
client_id = os.environ["EMOSONIC_CLIENT_ID"]
device_id = os.environ["EMOSONIC_DEVICE_SESSION_ID"]
capabilities = {
    "playbackContextV2": True, "playbackPrepare": False,
    "effectiveAtPlayback": False, "canPlay": False, "canPause": False,
    "canSeek": False, "canSetVolume": False, "supportsFollow": False,
    "supportsBroadcast": True, "remoteVolumeControl": True,
}
client.on("message", inbox.put, namespace=namespace)

def request(kind, action, payload, binding=None):
    rid = str(uuid.uuid4())
    client.emit("message", {"type": kind, "action": action, "requestId": rid,
                            "payload": payload}, namespace=namespace)
    deadline = time.monotonic() + 10
    while True:
        msg = inbox.get(timeout=max(0, deadline - time.monotonic()))
        if msg.get("requestId") != rid:
            continue
        if binding and (msg.get("connectionNonce"), msg.get("connectionEpoch")) != binding:
            continue
        body = msg["payload"]
        if msg["action"] == "system.error":
            raise RuntimeError(f"{action}: {body.get('code')}")
        if msg["action"] == action or (msg["action"] == "system.ack" and body["action"] == action):
            return msg

password = getpass.getpass("EmoSonic password: ")
client.connect(os.environ["EMOSONIC_URL"], namespaces=[namespace], socketio_path="emo/ws")
try:
    request("auth", "auth.login", {"u": os.environ["EMOSONIC_USER"], "p": password})
    ack = request("device", "device.register", {
        "clientId": client_id, "deviceSessionId": device_id,
        "deviceName": "My Remote", "roles": ["controller"], "capabilities": capabilities,
    })
    p = ack["payload"]
    meta = p["strictV2"]
    version = tuple(int(part) for part in meta["protocolVersion"].split("."))
    negotiated = p["negotiatedCapabilities"]
    binding = (meta["connectionNonce"], meta["connectionEpoch"])
    if not (len(version) == 3 and version[0] == 2 and version[1] >= 9
            and (p["clientId"], p["deviceSessionId"]) == (client_id, device_id)
            and set(negotiated) == set(capabilities)
            and all(type(v) is bool and (not v or capabilities[k]) for k, v in negotiated.items())
            and negotiated["playbackContextV2"] and isinstance(binding[0], str) and binding[0]
            and type(binding[1]) is int and binding[1] > 0
            and (ack.get("connectionNonce"), ack.get("connectionEpoch")) == binding):
        raise RuntimeError("Registration mismatch")
    devices = request("state", "device.list", {}, binding)["payload"]["devices"]
    for d in devices:
        if "player" in d["roles"]:
            print(d.get("alias") or d.get("deviceName"), d["clientId"], d["deviceSessionId"])
finally:
    client.disconnect()
```

这个例子一次只处理一个查询。长期运行的客户端要用 `requestId` 路由并发请求，单独处理主动推送，并在断开时结束旧连接上所有等待者。重新登录注册，重新读取设备、订阅和状态，再开放控制。高频音量滑块可合并未发送的值；超时的切歌、接力等操作先读回状态，不自动重发。

## 操作与确认

业务信封沿用上面的 `type`、`action`、`requestId` 和 `payload`。每个新操作使用独立请求 ID。查询可能直接以同名 action 返回数据，写操作通常先收到 `system.ack`。用本次连接的 nonce/epoch 和请求 ID 匹配应答。订阅事件通常没有请求 ID，要按其业务标识匹配。

| 目的 | 发送 | 如何判断结果 |
| --- | --- | --- |
| 获取某个播放器的任务 | `playback.context.list`，type 为 `state`，带 `authorityClientId`、`authorityDeviceSessionId` | 从结果选取属于该设备的活动任务。若有多个，交给用户选择。对选定的 `playbackContextId` 依次发 `playback.context.subscribe`、`playback.context.status`，两者都是 `state`。 |
| 调设备音量 | `device.setVolume`，type 为 `command`，带 `targetClientId`、`targetDeviceSessionId`、整数 `volume`（0–100） | 不需要播放任务。控制端须协商 `remoteVolumeControl`，目标支持远程音量和 `canSetVolume`。等同一设备会话的 `device.volume.update` 或刷新设备列表读 `volumeState`，核对实际值。 |
| 播放、暂停、上下首、定位 | `player.play`、`player.pause`、`player.prev`、`player.next`、`player.seek`，type 为 `command` | 带 `playbackContextId` 和刚读到的 `baseControlVersion`；定位另带整数毫秒 `positionMs`。等执行设备的播放反馈或状态回读，核对执行版本、曲目和状态。 |
| 按队列位置点歌 | `queue.playItem`，type 为 `command` | 带任务 ID、从 0 开始的 `queueIndex`、`baseQueueRevision` 和 `baseControlVersion`。当前版本支持在同一操作附 `queueSongIds` 提交要播放的队列，按实际曲序和执行版本确认。重复歌曲按位置区分。 |
| 初始化空任务 | `playback.context.prepare`，type 为 `command` | 在已选定的空任务上带 `intentId`、`baseControlVersion`、`initialQueueSongIds`、`currentIndex`、`positionMs` 和任务 ID。等执行设备报告准备结果并发布队列。 |
| 从 A 接力到 B | `playback.handoff.start`，type 为 `command` | 发送下方示例。收到 `preparing` 后保留 `handoffId`，等 `playback.handoff.status` 中同一 ID 的 `completed`，并核对新执行设备。可用 `playback.handoff.cancel` 取消允许取消的交接，带任务 ID 和 `handoffId`。 |
| 多设备群播 | `broadcast.start`，type 为 `command`，带源任务 ID、独立 `intentId` 和选定的 `participants`（其他播放器的 clientId 列表） | 须协商 `supportsBroadcast`。保存 `broadcastId`，通过 `broadcast.status`（type 为 `state`）查看各设备进度和错误。群主或源按权限可发 `broadcast.play/pause/seek/playItem/stop`。播放类命令取新的控制版本，群内选曲还需队列版本。 |
| 一台播放器跟听另一台 | 由跟听端自己发 `follow.start` / `follow.stop` | Follow 需要 `player` 身份并由跟听设备执行音频。独立遥控器可用群播组织多台设备同步播放。 |

### Handoff 请求示例

假设 A 的任务为 `ctx-A`，查询得到当前控制版本 17；B 的在线会话为 `speaker-B` / `device-B`。订阅 A 的任务后发送以下对象。设备名和版本来自本次查询，示例值需要替换。

```json
{
  "type": "command",
  "action": "playback.handoff.start",
  "requestId": "bafc7b47-38fa-4df6-8029-2997bc9e49b4",
  "payload": {
    "playbackContextId": "ctx-A",
    "targetClientId": "speaker-B",
    "targetDeviceSessionId": "device-B",
    "baseControlVersion": 17
  }
}
```

源设备由任务的当前执行者确定。目标需要在线、能够预载和定时起播，且处于服务端允许接管的状态。原设备与目标负责上报播放进度和时钟；目标负责报告 ready 与 complete。遥控器既不用拉音频流，也不用代设备确认成功。成功可再读该任务的 `authorityClientId`、`authorityDeviceSessionId`，应该已指向 B。

若返回 `conflict`，重新读取上下文和设备状态，检查目标是否正忙、播放进度是否过期或正在交接。若状态为 `failed`、`timedOut` 或 `cancelled`，保留服务器给出的错误信息。遥控器自身超时只表示结果未确认，可查询原任务当前归属。新请求发出前再次确认目标、队列索引及控制版本。

可对照本仓库的[严格协议字段定义](../supysonic/emo/strict_v2_contract.py)、[网页控制器](../supysonic/templates/control_strict_v2.html)以及[三端接力测试](../tests/base/test_emo_strict_v2_handoff.py)。
