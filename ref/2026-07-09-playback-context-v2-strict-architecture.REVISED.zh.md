# PlaybackContext v2 计划文档 · 代码核对与修订版

> 本文件是对 `2026-07-09-playback-context-v2-strict-architecture.zh.md` 的核对结果与修订。
> 已逐条对照当前代码（`supysonic/emo/ws.py` 3962 行、`ws_state.py` 1802 行、`ws_store.py` 574 行、
> `db_layer/emo.py` 106 行、`tests/base/test_emo_ws*.py`）验证，所有结论均附 `file:line` 证据。
>
> 结论：**原计划的架构主轴（sessionId 职责拆分为 deviceSessionId / playbackContextId / authorityClientId，
> v2 严格 + legacy 隔离的分阶段迁移）完全正确，可以执行。** 但有若干处对现状的描述与代码不符，
> 会导致实现者写出死代码或错误迁移。下面先给修订清单，再给逐章订正文本。

---

## 0. 总体判定

| 维度 | 判定 |
| --- | --- |
| 架构方向（PlaybackContext 承载播放任务，sessionId 退场） | ✅ 正确 |
| 「v2 严格 / legacy 隔离」分阶段策略 | ✅ 正确，且比一次性重构安全 |
| 对 `ws.py` dispatcher / resolver / 双写现状的描述 | ✅ 基本准确（见 §1） |
| 对状态层实现的描述 | ⚠️ 多数正确，4 处需修正（见 §2） |
| 对持久层 / DB 模型的描述 | ⚠️ 有 1 处明确错误 + 若干遗漏（见 §2） |
| 测试计划 | ⚠️ 命名与现有测试冲突，需改名（见 §3） |

**一句话**：可以按原计划开工，但先把下面 §2 的 8 条订正吃进去，否则第一阶段会踩空。

---

## 1. 已核实正确、可直接依赖的部分

这些原文描述与代码一致，实现时可放心引用：

- `SESSION_ACTIONS = {"session.subscribe", "session.unsubscribe"}` 存在（`ws.py:60`）；dispatcher 是单一
  `if/elif` 链（`EmoNamespace.on_message`，`ws.py:3066` 起），**不是** dict 分发表。
- `_resolve_playback_context_id` 确实按 `playbackContextId → sessionId → _device_session_id` 兜底
  （`ws.py:284-289`）；`_resolve_device_session_id` 按 `deviceSessionId → sessionId → _device_session_id`
  兜底（`ws.py:276-281`）；`_device_session_id` 本身是 `deviceSessionId or sessionId`（`ws.py:270-273`）。
  —— §2.2 / §3.2 / Task 1 的前提成立。
- `device.register` 内联 `device_session_id = payload.get("deviceSessionId") or payload.get("sessionId") or client_id`
  （`ws.py:647`），并把同一个值同时写入 `deviceSessionId` 和 `sessionId`（`ws.py:659-660`），注册后按
  `sessionId` 调 `_restorePersistedState`（`ws.py:3176`）。—— §3.3 成立。
- `playback.update` 走双写：既写 legacy（`state.update_playback_state` `ws.py:3418` + `savePlaybackState`
  `ws.py:3449` + `_broadcast_playback_state` `ws.py:3486`），又写 context（`apply_authority_playback_update`
  `ws.py:3423` + `record_device_playback_state` `ws.py:3432` + `_broadcast_playback_context_state`
  `ws.py:3488`）。—— §3.4 成立。
- `queue.session.sync` 同时写 legacy 队列（`update_queue` + `saveQueueState`）和 context 队列
  （`update_playback_context_queue`），且 **无论哪条分支都无条件写 `EmoSessionQueue`**
  （`ws.py:3668/3694` 与 `3678/3686/3718`）。—— §3.5 成立。
- 状态层持久层的 payload 组装确实补 `sessionId` / `sourceClientId` 别名
  （`ws_store.py` `_playback_context_payload:253/257`、`_device_playback_state_payload:348/350`；
  `ws_state.py` `_copy_playback_context_locked:600-609`）。—— §0.2 / §3.10 / Task 12 成立。
- 远控确实以 `targetClientId` + `sessionId` 为核心；`_resolve_control_target` 存在（`ws.py:680`）。—— §3.6 成立（但见 §2 第 3 条订正定位）。
- 跟播确实用 `sourceSessionId`/`followSessionId` 等（`_handle_follow_start` `ws.py:2387`）。—— §3.7 成立。
- 群播确实是独立模型：`_broadcasts` / `_broadcast_participants` / `_broadcast_playback_states`
  （`ws_state.py:105/107/109`）。—— §3.8 成立。
- `playback.context.*` / `queue.context.sync` 这些新 action **确实都还不存在**（全库仅出现在计划文档里）。—— Task 3/4/7 的前提成立。
- 运行时容器命名与原文完全一致（`ws_state.py:85-125`）。—— §15 的清单成立。
- 测试都在 `tests/base/`，用 `unittest.TestCase`（非 pytest）。—— §20 路径成立。
- `EmoPlaybackContext` 无 `session_id` 列、以 `playback_context_id` 为唯一键；`EmoPlaybackHandoff`
  已有 `origin_client_id`（`emo.py:99`）与 `request_id`（`emo.py:94`）。—— §17 相关描述成立。

---

## 2. 必须修正的地方（逐条，附证据与订正文本）

### 订正 1 —— `capabilities.playbackContextV2` 目前不存在，是要新增的字段（不是现状）

**原文问题**：§0.1、§4.3、Task 2、Task 5 等多处用 `capabilities.playbackContextV2 = true` 作为 v2 门控依据，
读起来像既有能力。实际全库 **没有任何 v2 能力标志**。

**证据**：`device.register` 只是原样存下 `payload.get("capabilities")`（`ws.py:661`）；能力读取靠
`_client_capabilities`（`ws.py:292`）+ `_client_supports`（`ws.py:297`，`.get(cap) is True`）。当前被服务端识别的能力
**只有** `CAPABILITY_EFFECTIVE_AT = "effectiveAtPlayback"`（`ws.py:110`）和
`CAPABILITY_PLAYBACK_PREPARE = "playbackPrepare"`（`ws.py:111`），供 `_select_playback_protocol`（`ws.py:301-320`）使用。
全库 grep `playbackContextV2` / `contextV2` / `v2` 无命中。

**订正**：在 Commit 0 明确加一步「**新增 `CAPABILITY_PLAYBACK_CONTEXT_V2 = "playbackContextV2"` 常量并接入
`_client_supports`**」。同时说明：**第一阶段 v2 分支判定不能只靠能力位**，因为现有 `playback.update` /
`queue.session.sync` 已经用「payload 是否含 `playbackContextId`/`deviceSessionId`」做 v2 判定
（`is_context_payload`，`ws.py:3411`、`ws.py:3654`）。建议 v2 判定统一为：

```text
is_v2 = _client_supports(client, "playbackContextV2")
        or "playbackContextId" in payload
        or "deviceSessionId" in payload
```

> 复用现有的 `is_context_payload` 语义，避免再造第二套判定。

---

### 订正 2 —— `EmoDevicePlaybackState.mode` 与 `is_authority` 已存在，不是第二阶段要新增的列

**原文问题**：Task 13「第二阶段建议新增字段」把 `EmoDevicePlaybackState` 的 `mode` 列成待加项。

**证据**：`db_layer/emo.py:82` 已有 `mode = CharField(32, default="normal")`；`emo.py:81` 已有
`is_authority = IntegerField(default=0)`。运行时 `record_device_playback_state(..., mode="normal")` 与
`saveDevicePlaybackState(..., mode="normal")` 都已按 `mode` 写入（`ws_state.py:762`、`ws_store.py:392`）。
handoff 完成时已写 `mode="handoff"`（`ws.py` `_handle_handoff_complete`）。

**订正**：`EmoDevicePlaybackState` 第二阶段真正缺的只有 `last_reported_at`。把 Task 13 该表的新增字段改为：

```text
EmoDevicePlaybackState:
  last_reported_at   # 新增（mode / is_authority 已存在，无需新增）
```

---

### 订正 3 —— sessionId 兜底表达式不在 `_resolve_control_target` 里，定位错了

**原文问题**：§3.6 与 Task 6 暗示远控的 `session_id = payload.get("sessionId") or target_client.get("sessionId")`
在 `_resolve_control_target`，并把它列入「需要改造的函数」。

**证据**：`_resolve_control_target`（`ws.py:680-695`）只返回 `(target_client_id, target_sid, target_client)`，
**从不推导 sessionId**。那句兜底表达式实际出现在 4 处：`_handle_server_mediated_control` 的 `player.pause`
分支（`ws.py:1774`）、`player.seek` 兜底分支（`ws.py:1833`）、`_build_source_control_commit_payload`
（`ws.py:1542`）、`_build_seek_media_change_commit_payload`（`ws.py:1618`）。

另外要注意：`player.pause` 与非切歌的 `player.seek` 是在 `_handle_server_mediated_control` 里 **内联** 处理的
（`ws.py:1773-1820`、`1832-1896`，走 `state.update_playback_control`），**不经过** commit-payload builder；
只有 `player.play/next/prev/queue.playItem` 和切歌 `seek` 才走 builder。且两条内联分支在
`_select_playback_protocol([target]) == PROTOCOL_LEGACY` 时会 `return False` 短路（`ws.py:1768-1770`）。

**订正**：Task 6「需要改造的函数」清单删掉 `_resolve_control_target`（它只做目标解析，v2 下仍可复用来定位
authority 设备），改为明确列出真正含 sessionId 推导的 4 个位点：

```text
_handle_server_mediated_control      （player.pause / player.seek 内联分支）
_build_source_control_commit_payload
_build_seek_media_change_commit_payload
_validate_source_base_control_version / _current_source_control_version（读取 sessionId 版本）
```

---

### 订正 4 —— `get_playback_context` 已存在；创建路径其实有三条（不是两条）

**原文问题**：§0.3 / Task 3 / Task 11 把状态层写成「只有隐式 upsert，需要新增 create/get/update-existing」。
`get_playback_context` 被当成待加项。

**证据**：
- `get_playback_context(playback_context_id)` **已存在**（`ws_state.py:656`，返回 `_copy_playback_context_locked`）。
- 隐式创建路径不止 `update_playback_context_queue`/`apply_authority_playback_update`（两者都经
  `_get_or_create_playback_context_locked`，`ws_state.py:611`）；还有第三条 **`restore_playback_context`**
  （`ws_state.py:663`），它直接用持久化 payload 覆盖写入 `_playback_contexts`。
- `transfer_playback_authority`（`ws_state.py:882`）相反：不存在 context 时返回 `None`，不创建（`ws_state.py:897-899`）。

**订正**：
- Task 3 / Task 11 的「新增 API」里删掉 `get_playback_context`（已有），保留
  `create_playback_context` / `update_existing_playback_context_queue` / `apply_existing_authority_playback_update`。
- 显式 create/update-existing 拆分时，**必须把 `restore_playback_context` 一并纳入考量**（它是第三条隐式创建入口）：
  要么让它只在「进程重启后从 DB 冷恢复」时调用，要么给它加 `create_if_missing` 显式开关，否则 v2 严格校验会被
  restore 旁路绕过。

---

### 订正 5 —— handoff 幂等键当前是 2 元组，且还有一条「同 context 并发」硬约束被漏掉

**原文问题**：§14 / §17 说「当前 handoff 幂等 key 应升级为 `(user_name, origin_client_id, request_id)`」，
措辞像现状已经接近三元组。

**证据**：现状幂等键是 **2 元组** `(user_name, request_id)`：
`create_playback_handoff` 里 `request_key = (user_name, request_id)`（`ws_state.py:960`），命中即返回既有 handoff；
`get_playback_handoff_by_request` 也按 `(user_name, request_id)` 查（`ws_state.py:1005`）。store 层
`getPlaybackHandoffByRequest(user_name, request_id)`（`ws_store.py:488`）同样是 2 元组。`origin_client_id` 列虽已存在
（`emo.py:99`），但**未参与**幂等键。

**补充（原文遗漏的并发保护）**：`create_playback_handoff` 另有一条独立约束——同一 `playbackContextId`
若已有处于 `preparing/ready/committed` 的 handoff，会抛 `PlaybackAuthorityMismatchError`（`ws_state.py:967-973`）。
这跟 request 幂等是两回事。

**订正**：§17 明确「**从 2 元组升级到 3 元组**」，并要求同步改三处：`_handoff_request_index` 的写入
（`ws_state.py:960/993`）、`get_playback_handoff_by_request`（`ws_state.py:1001-1009`）、
`ws_store.getPlaybackHandoffByRequest`（`ws_store.py:488`）。同时在验收标准里补一条：
「升级幂等键不得破坏『同 context 并发 handoff 被拒』的既有保护」。

---

### 订正 6 —— `_playback_context_subscriptions` 是死容器，订阅要从零搭（比原文更强）

**原文问题**：Task 7 / Task 11 说「当前状态层只有 `_playback_context_subscriptions` 容器，还缺少方法」，
暗示容器已在被使用、只差方法。

**证据**：该容器**完全 vestigial**：只在 `__init__` 声明（`ws_state.py:125`）和断连时 pop（`ws_state.py:222`），
**全库没有任何写入或读取**。而 sessionId 订阅是完整实现的：`subscribe_session`（`ws_state.py:284`）/
`unsubscribe_session`（`292`）/ `list_subscribers`（`306`）。

**订正**：Task 7 描述改为「context 订阅链路需**完全新建**，可直接照搬 `subscribe_session` 三件套的模式」。
并提醒：§11 目标状态里保留 `_playback_context_subscriptions` 是对的，但要注意断连清理逻辑（`ws_state.py:222`）
已经在 pop，新增 writer 后无需再改清理端。

---

### 订正 7 —— `queue.context.sync` 必须是全新 handler，不能复用 `queue.session.sync`

**原文问题**：Task 4 说「把旧 action 标记 legacy-only、新增 `queue.context.sync`」，但没点破一个关键事实：
现有 `queue.session.sync` **无条件** 同时写 `_queues` + `EmoSessionQueue`。

**证据**：`queue.session.sync` handler 里 `state.update_queue(...)`（`ws.py:3678`/`3686`）和
`saveQueueState(...)`（`ws.py:3718`）在 `is_context_payload` 的两个分支里都会执行——**没有任何分支会跳过写旧表**。
因此 §8 的验收项 `test_queue_context_sync_does_not_write_emo_session_queue` 描述的行为**当前不存在**。

**订正**：Task 4 明确「`queue.context.sync` 是**独立新 handler**，只调用
`update_existing_playback_context_queue` + `_save_playback_context_snapshot`，**绝不调用** `update_queue` /
`saveQueueState`」。不能通过给 `queue.session.sync` 加分支来实现——那样旧写路径仍会触发。

---

### 订正 8 —— 三个 ID 目前会塌缩成同一个值，v2 分流依赖「客户端真的传不同 ID」

**原文问题**：全文把 `playbackContextId` / `deviceSessionId` / `sessionId` 当作可独立区分的命名空间，
但没强调一个现实约束。

**证据**：`device.register` 把 `deviceSessionId` 与 `sessionId` 写成**同一个值**（`ws.py:659-660`，兜底到
`client_id`）；`_resolve_playback_context_id` 兜底链是 `playbackContextId → sessionId → _device_session_id`
（`ws.py:284-289`）；`_resolve_device_session_id` 兜底链是 `deviceSessionId → sessionId → _device_session_id`
（`ws.py:276-281`）。**结论**：对一个既不传 `playbackContextId` 也不传 `deviceSessionId` 的客户端，两个 resolver
会塌缩到同一个已注册值。

**订正**：在 §2.2 / Task 1 补一条前置约束：「**v2 要让 `playbackContextId` 与 `deviceSessionId` 真正分离，
前提是客户端确实下发不同的两个 ID**。v2 严格 resolver（拒绝 sessionId 兜底）落地后，任何仍指望
`playbackContextId==deviceSessionId` 隐式统一的旧调用方都会立即 `bad_request`——这正是期望行为，但要在客户端接入
清单里显式列出。」

---

### 订正 9 —— 测试命名与既有测试语义撞车，需加 `v2_` 前缀

**原文问题**：§20 提议的 `test_playback_update_requires_existing_context`、
`test_playback_update_from_non_authority_is_device_feedback_only`、`test_queue_context_sync_requires_existing_context`
与既有 legacy 测试语义重叠。

**证据**：`tests/base/test_emo_ws.py` 已有 `test_new_playback_update_requires_existing_context`（:2412）、
`test_non_authority_playback_update_is_device_feedback_only`（:2010）、
`test_queue_session_sync_creates_playback_context`（:1975）、
`test_handoff_complete_transfers_authority_and_can_switch_back`（:2082）等。

**订正**：§20 所有第一阶段 v2 新测试统一加 `v2_` 前缀（如 `test_v2_playback_update_requires_existing_context`），
并在计划里注明「这些是既有 legacy 测试的 v2 对应版，差异仅在 action 名与『拒绝 sessionId 兜底 / 不写旧表』」。
另外：`tests/base/` 路径正确（unittest 发现，非 pytest；参照 `EmoWebSocketTestCase` 的 setUp 需手动清空 ~20 个
state 字典，含 `_playback_context_subscriptions` / `_handoff_request_index`）。第二阶段的
`test_emo_ws_playback_context_v2_follow_broadcast.py` 保持第二阶段；第一阶段 follow/broadcast 回归已被既有
~30 个 broadcast/follow 测试覆盖，无需新文件。

---

### 订正 10 —— `broadcast.*` action 清单不完整；serializer alias 来源列要写准

**原文问题**：§4.1 的 `broadcast.*` 列表漏了两个真实存在的 action；§0.2/§3.10 对 sessionId alias 的来源列描述笼统。

**证据**：
- 实际 `BROADCAST_ACTIONS`（`ws.py:67-76`）含 `broadcast.queue.sync` 与 `broadcast.playItem`，§4.1 未列。
- alias 注入点精确为两处 serializer：`_playback_context_payload`（`ws_store.py:246`）里
  `"sessionId": record.playback_context_id`（`:253`）、`"sourceClientId": record.authority_client_id`（`:257`）；
  `_device_playback_state_payload`（`ws_store.py:341`）里 `"sessionId": record.device_session_id`（`:348`）、
  `"sourceClientId": record.owner_client_id`（`:350`）。**两处的 `sessionId` 来源列不同**
  （context 来自 `playback_context_id`，device 来自 `device_session_id`），`sourceClientId` 来源列也不同。
- `EmoPlaybackContext` / `EmoDevicePlaybackState` **没有 `session_id` 列**——sessionId 纯属 serializer 侧别名。

**订正**：§4.1 补全 `broadcast.queue.sync` / `broadcast.playItem`；§0.2 把「v2 serializer 禁止输出 sessionId」
落到这两个具体函数，并注明「两处 alias 来源列不同，v2 serializer 应新增独立函数而非在原函数里加分支」。

---

### 订正 11 —— `volume` 已同时存在于两张表，§2.6 的建议是「行为迁移」而非「加列」

**原文问题**：§2.5/§2.6 把 `volume` 归属讨论写得像需要新增列。

**证据**：`EmoPlaybackContext.volume`（`emo.py:61`）与 `EmoDevicePlaybackState.volume`（`emo.py:79`）**都已存在**；
运行时 `apply_authority_playback_update` 会把 `volume` 写进 context（`ws_state.py`）。

**订正**：§2.6 说明「两张表都已有 `volume` 列，无需 schema 变更；这里是**行为决策**——v2 停止把设备真实音量写入
`PlaybackContext.volume`，改只写 `DevicePlaybackState.volume`。是否引入 `logicalVolume` 才是唯一可能的加列项，
且默认不加。」

---

### 订正 12 —— store 层命名约定要写清（camelCase 函数 / snake_case 列 / camelCase payload 键）

**原文问题**：Task 12 新增函数名（`createPlaybackContextState` 等）用了 camelCase，但没说明列名/参数是 snake_case，
容易在实现时混淆。

**证据**：`ws_store.py` 公有函数名是 camelCase（`getQueueState`/`savePlaybackContextState`），
但参数与 Peewee 列是 snake_case（`session_id`/`playback_context_id`/`authority_client_id`），返回 payload 键又是
camelCase（`sessionId`/`playbackContextId`）。`createPlaybackContextState` / `getPlaybackContextWithDeviceStates` /
`listUserPlaybackContexts` / `deletePlaybackContext` / `expirePlaybackContext` 全部**不存在**（确为待加）。

**订正**：Task 12 补一行约定：「新增函数遵循既有约定——**camelCase 函数名、snake_case 参数与列、camelCase payload 键**」。
并注意：`listUserPlaybackContexts` 可行（`EmoPlaybackContext.user_name` 存在，`emo.py:53`），但目前
`user_name` **无索引**（仅 `playback_context_id` unique），若要频繁按用户查，第二阶段应补索引。
另外 `saveDevicePlaybackState` 在 `is_authority=True` 时会先把同 context 其他设备的 `is_authority` 批量降级
（`ws_store.py:411-419`）——create/update 拆分时必须保留这个降级步骤。

---

### 订正 13 —— Task 5「移除 legacy 写入」要落到具体行；v2 playback.update 的 not_found 已部分具备

**原文问题**：Task 5 列出要从 v2 分支移除 `update_playback_state` / `savePlaybackState` / `_broadcast_playback_state`，
但没说这些在当前 handler 里是**无条件**执行的。

**证据**：`playback.update` 普通路径里 `state.update_playback_state(...)`（`ws.py:3418`）、
`savePlaybackState(...)`（`ws.py:3449`）、`_broadcast_playback_state(...)`（`ws.py:3486`）在
authority / 非 authority 两种情况下都会跑，**不受 `is_context_payload` 控制**。而 not_found 校验已部分具备：
`is_context_payload` 为真且 context 无法恢复时会抛 `LookupError("Playback context not found")`（`ws.py:3414-3415`）。

**订正**：Task 5 明确「v2 分支需**用条件包住**这三处旧写调用（`ws.py:3418/3449/3486`），仅 legacy 分支执行；
context not_found 逻辑已存在于 `ws.py:3414-3415`，v2 严格化只需确保 v2 路径不再经隐式 create 兜过这条校验
（即改调 `apply_existing_authority_playback_update`，缺 context 直接 not_found）」。

---

## 3. 对原计划各 Task 的「保留 / 修改」快速索引

| 原文 Task | 判定 | 需要的动作 |
| --- | --- | --- |
| §0.1 协议门控 | 保留 | 加订正 1：v2 判定复用 `is_context_payload`，新增 `playbackContextV2` 常量 |
| §0.2 Serializer 边界 | 保留 | 加订正 10：落到两个具体 serializer 函数，来源列不同 |
| §0.3 状态层边界 | 保留 | 加订正 4：`restore_playback_context` 是第三条隐式创建入口 |
| Task 1 严格化 ID 解析 | 保留 | 加订正 8：强调三 ID 塌缩前提 |
| Task 2 device.register v2 | 保留 | 依赖订正 1 的能力位 |
| Task 3 playback.context.create | 保留 | 删掉「新增 get_playback_context」（已有），保留其余 |
| Task 4 queue.context.sync | 保留 | 加订正 7：必须独立 handler，不复用 |
| Task 5 playback.update 严格化 | 保留 | 加订正 13：三处旧写调用需条件包裹 |
| Task 6 远控 context 化 | 保留 | 加订正 3：改函数清单定位 |
| Task 7 subscribe/status | 保留 | 加订正 6：订阅链路从零搭 |
| Task 8 跟播（二阶段） | 保留 | 无变更 |
| Task 9 群播（二阶段） | 保留 | 加订正 10：补全 broadcast action 清单 |
| Task 10 handoff 纯 context | 保留 | 加订正 5：幂等键 2→3 元组 + 并发保护 |
| Task 11 状态层清理 | 保留 | 加订正 4/6 |
| Task 12 持久层清理 | 保留 | 加订正 12：命名约定 + 降级步骤 |
| Task 13 DB 模型 | **修改** | 加订正 2：`mode`/`is_authority` 已存在，只缺 `last_reported_at` |
| §2.5/2.6 音量归属 | **修改** | 加订正 11：两表已有 volume 列，是行为迁移非加列 |
| §20 测试计划 | **修改** | 加订正 9：v2 测试统一加 `v2_` 前缀 |

---

## 4. 一处需要产品/架构确认的取舍

计划把「v2 判定」同时挂在 **能力位** 和 **payload 字段** 两个信号上。这两者语义不同，需明确优先级：

- **payload 含 `playbackContextId`/`deviceSessionId`** —— 现状就用这个（`is_context_payload`），改动小、可立即落地，但任何旧客户端只要偶然带上这些字段就会被判为 v2。
- **能力位 `playbackContextV2`** —— 更干净、更符合「客户端全量适配」的叙事，但要先给所有客户端加握手，第一阶段落不了地。

**建议（已写进订正 1）**：第一阶段用 `能力位 OR payload 字段` 的并集，既能让现有 `is_context_payload` 路径继续工作，又为将来「只认能力位」留出收紧空间。最终退场阶段再收紧为「只认能力位」。若你更倾向第一阶段就严格「只认能力位」，需要把客户端接入排到 Commit 0 之前——这会拖慢第一阶段，我不推荐。

---

## 5. 结论

原计划可以执行，架构判断正确。落地前把上面 **13 条订正** 吃进对应 Task 即可；其中真正会「踩空」的高优先级是：

1. **订正 1**（`playbackContextV2` 是要新增的，不是现状）——否则门控逻辑无处可挂。
2. **订正 7**（`queue.context.sync` 必须独立 handler）——否则 `不写 EmoSessionQueue` 的验收永远过不了。
3. **订正 2**（`mode`/`is_authority` 已存在）——否则会写一条报错的 migration。
4. **订正 4 + 6**（`get_playback_context` 已有；`restore_playback_context` 是第三条创建入口；订阅容器是死的）——否则要么写重复 API，要么留下 restore 旁路。

其余订正是精度问题（函数定位、命名约定、测试改名、字段归属），不影响方向，但能省下返工。
