# PlaybackContext v2 实现 · 代码审查改动单

> 审查对象：`2026-07-09-playback-context-v2-strict-architecture*.md` 第一阶段的实现改动
> （未提交，位于工作区 `supysonic/emo/ws.py` / `ws_state.py` / `ws_store.py` + 对应测试）。
> 审查基线：`git diff` 共 +4868 / -400，157 个测试全绿。
>
> **总体结论**：实现质量高，修订版 13 条订正全部落实，strict-v2 主链路正确。
> 下列问题都不是 happy-path 崩溃，而是**重启 / 竞态 / 边界**下的状态分歧。
> 每条都已对照代码逐行复核，附 `file:line`、复现场景、改法、验收点。
>
> 优先级：**M1 → M2 先做**（明确、低风险）；**M3 需先定设计口径**；Minor 按性价比排期。
> 建议每条一个独立 commit，便于回归定位。

> **实施复核（2026-07-10）**：本文件保留为审查基线，正文行号与代码片段均指向审查当时的工作区。M1、M2、M3、m3 已修复；M3 采用“服务端乐观推进、authority feedback 后续校正”的口径。m4 最终口径为仅 strict-v2 拒绝 `sessionId`，context-compatible legacy 客户端继续兼容。m1、m2、m5、m6 保留为后续生命周期或协议统一工作，不属于本次提交范围。

---

## 任务分配总览

| 编号 | 严重度 | 一句话 | 主要文件 | 依赖/前置 |
| --- | --- | --- | --- | --- |
| M1 | Major | 持久化计数器 `or 1` 与内存/ack 的 0 不一致，重启后握手被误拒 | `ws_store.py` | 无 |
| M2 | Major | `handoff.start` 经 legacy 队列隐式创建 context | `ws.py` | 无 |
| M3 | Major(设计) | v2 控制不推进 `currentIndex`/`trackId`，两次快速 next 竞态 | `ws.py` / `ws_state.py` | **需产品/架构先拍板** |
| m1 | Minor | 断连不清 `_device_playback_states`，幽灵设备 + 无界增长 | `ws_state.py` | 与 m3 同源 |
| m2 | Minor | authority 掉线后 context 悬挂，无改派/恢复 | `ws_state.py` / `ws.py` | 依赖 m1 思路 |
| m3 | Minor | create 幂等只看内存，重启后重复 create 版本回退 | `ws_state.py` / `ws.py` | 与 M1 同源，建议一起修 |
| m4 | Minor | legacy 客户端发 v2 payload 带 sessionId 未被拒 | `ws.py` | 无 |
| m5 | Minor | 同一 context 状态用 3 个 action 名 / 2 种形状下发 | `ws.py` + 客户端 | 需与客户端协商 |
| m6 | Minor | 4 个 store 函数已实现有单测但从未接线（死代码） | `ws.py` / `ws_store.py` | 定范围 |

---

## M1 · 持久化计数器 `or 1` 与内存/ack 不一致（Major，先修）

**位置**：`supysonic/emo/ws_store.py:354-357`（create 分支）与 `373-376`（update 分支）。

**现状代码**：
```python
# create 分支
queue_revision=payload.get("queueRevision") or 1,
control_version=payload.get("controlVersion") or 1,
version=payload.get("version") or 1,
epoch=payload.get("epoch") or 1,
# update 分支同样是 `or 1`
```
而 `create_playback_context`（`ws_state.py:782-785`）内存初始化是 **0**：
```python
"queueRevision": 0,
"controlVersion": 0,
"version": 0,
"epoch": 0,
```
`_handle_playback_context_create`（`ws.py:3245-3251`）的 ack 回的是 `serializePlaybackContextV2(playback_context)`，即内存值 **0**。

**问题**：`or 1` 把 0 当 falsy 强制成 1。于是：
- 创建瞬间：内存 = 0、ack 回给 client = 0、DB 落库 = 1。三者不一致。
- client 缓存 `baseControlVersion = 0`。

**复现场景**：
1. v2 client 发 `playback.context.create {playbackContextId:"playback:alice:main", deviceSessionId:"root:pc", queueSongIds:["s1"]}`。ack 返回 `controlVersion: 0`，client 记住 0。
2. 服务端重启，`_playback_contexts` 内存清空，DB 行仍在（`control_version=1`）。
3. client 发 `playback.handoff.start {playbackContextId:"playback:alice:main", targetClientId:"pc-2", baseControlVersion:0}`。
4. `_get_existing_playback_context` 从 DB 恢复，`controlVersion=1`。
5. `ws.py:3570` 比对 `base_control_version(0) != context.controlVersion(1)` → 抛 `ControlConflictError`，握手被无故拒绝。
6. 同理，任何用 epoch / queueRevision 做单调对账的客户端在重启后都会误判「变了」。

**改法（二选一，推荐 A）**：
- **A（推荐）**：`create_playback_context` 内存初始值改成从 **1** 起（`queueRevision/controlVersion/version/epoch` 全部初始为 1），与持久化一致。这样内存、ack、DB 三者统一为 1。需同步检查依赖「初始为 0」的测试（如断言 create 后 `controlVersion==0` 的用例）并更新。
- **B**：保留内存初始 0，把 `ws_store.py` 两处的 `or 1` 改成保留 0 的写法，例如 `payload.get("controlVersion", 1)`（缺键才默认 1，显式 0 要保留）。注意 `0` 仍是合法值，不能再被 `or` 吞掉。

> 选 A 更干净：单调计数器从 1 起是惯例，且避免「0 是否合法」的歧义。选 B 改动面更小但要确保四个字段两处分支都改到。

**验收点**：
- 新增测试：`create` 后立即 `getPlaybackContextState` 读回，断言 `controlVersion / version / queueRevision / epoch` 与 ack payload **完全相等**。
- 新增测试：模拟「create → 清空内存（模拟重启）→ `_get_existing_playback_context` 恢复 → 用 ack 里的 `baseControlVersion` 发 handoff.start」不再抛 `ControlConflictError`。
- 现有 157 测试仍全绿（若选 A，更新受影响的初始值断言）。

---

## M2 · `playback.handoff.start` 经 legacy 队列隐式创建 context（Major，先修）

**位置**：`supysonic/emo/ws.py:3488`（handoff.start 内），根因在 `_get_or_restore_playback_context`（`ws.py:3112-3140`）。

**现状代码**：
```python
# _handle_handoff_start
context = _get_or_restore_playback_context(playback_context_id)   # ws.py:3488
if context is None:
    raise LookupError("Playback context not found")
```
`_get_or_restore_playback_context` 在内存 + 持久化都没有时，会**回退到 legacy `getQueueState`** 并合成一个全新 context：
```python
legacy_queue = getQueueState(playback_context_id)          # ws.py:3121
if legacy_queue is None:
    return None
return state.restore_playback_context(                     # ws.py:3124  ← 写入 _playback_contexts
    playback_context_id, { ...从 legacy 队列字段拼出来... },
)
```
对比：其它所有 v2 handler（create/status/subscribe/close/queue.context.sync/v2 控制）用的都是 `_get_existing_playback_context`（`ws.py:3143-3152`），它**没有** legacy 队列回退，缺失即返回 None。

**问题**：`handoff.start` 的 v2 分支（`strict_v2 or _is_context_payload(payload)`，`ws.py:3478`）也走 `_get_or_restore_playback_context`，于是 strict-v2 客户端能对「只有 legacy `EmoSessionQueue` 行、从没走过 `playback.context.create`」的 id 触发一次隐式创建，违反「只有 `playback.context.create` 能创建 v2 context」的第一阶段硬保证（计划 §21 / 修订订正 4）。

**复现场景**：
1. 某 legacy 会话曾用 id `room-1` 写过 `EmoSessionQueue`（无对应 `EmoPlaybackContext`）。
2. strict-v2 client（`capabilities.playbackContextV2=true`）发 `playback.handoff.start {playbackContextId:"room-1", targetClientId:"player-2"}`。
3. `_resolve_v2_playback_context_id` 返回 `"room-1"`；内存/持久都无 → 命中 `getQueueState("room-1")` 分支 → `restore_playback_context` 写入 `_playback_contexts`。
4. 一个从未 create 的 v2 context 被凭空物化。

**改法**：`_handle_handoff_start` 的 v2 分支改用 `_get_existing_playback_context`（与其它 v2 handler 一致）。
建议按能力/payload 分流，保留 legacy handoff 的旧行为：
```python
if strict_v2 or _is_context_payload(payload):
    context = _get_existing_playback_context(playback_context_id)   # 不再合成
else:
    context = _get_or_restore_playback_context(playback_context_id) # legacy 保持
if context is None:
    raise LookupError("Playback context not found")
```

**验收点**：
- 新增测试：strict-v2 client 对「只有 `EmoSessionQueue` 行、无 `EmoPlaybackContext`」的 id 发 handoff.start → 返回 `not_found`，且 `getPlaybackContextState(id)` 仍为 None（未被创建）。
- 回归：legacy handoff（无 v2 标记）对 legacy 队列 id 的既有行为不变（若有相关旧测试需保持绿）。

---

## M3 · v2 控制不推进 `currentIndex`/`trackId`，两次快速 next 竞态（Major，需先定口径）

**位置**：`_handle_v2_context_control`（`ws.py:2055-2092`）+ `apply_playback_context_control`（`ws_state.py:953-980`）。

**现状**：play/next/prev/queue.playItem 分支把算好的新 index 放进**下发给 authority 的** `command_payload`：
```python
command_payload["queueIndex"] = requested_index      # ws.py:2084
command_payload["currentIndex"] = requested_index
command_payload["trackId"] = queue_song_ids[requested_index]
```
但落到状态层的 `apply_playback_context_control`（`ws_state.py:953-980`）**只改** `state / positionMs / controlVersion / version / originClientId`，**从不动** `currentIndex / trackId / queueRevision / epoch`。即 index 只在 authority 稍后回 `playback.update` 时才真正前进。

**问题（两个真实缺陷）**：
1. **竞态**：context 当前 `currentIndex=0`、`controlVersion=0`。控制端连发两次 `player.next`：
   - 第一次 `baseControlVersion=0` 通过，算 `requested_index = 0+1 = 1`，下发「play s2」，context `controlVersion→1`（但 `currentIndex` 仍 0）。
   - authority 尚未 echo 时，第二次 `player.next {baseControlVersion:1}` 到达，匹配当前 `controlVersion=1` → 又算 `requested_index = 0+1 = 1`，**再次下发「play s2」**，永远到不了 s3。
   - 现实网络延迟下两次快速 next 都指向 index 1。
2. **断连分歧持久化**：广播出去的 context `controlVersion++` 但 `trackId` 还是旧的；若 authority 慢/掉线永不 echo，这个「版本前进但曲目没变」的状态会被 `_update_playback_context_snapshot` 持久化，durable 分歧。

**注意（这是设计口径问题，不是纯 bug）**：`test_v2_player_next_uses_context_queue` 明确断言控制后 context `currentIndex` **保持 0**，说明当前是**有意的「authority 为唯一真相、服务端纯转发、等 echo 才落 index」**设计。所以要先定口径：

- **口径 A（乐观推进，推荐给交互流畅）**：下发命令时就把 `currentIndex / trackId`（play 还有 positionMs）乐观写进 context，authority echo 时再对账。改 `apply_playback_context_control` 接收并写入 index/trackId。需同步改 `test_v2_player_next_uses_context_queue` 的断言。
- **口径 B（保持 authority 唯一真相，改动最小）**：不推进 index，但**堵竞态**——让第二条控制无法复用 pre-echo 的旧 version。做法：控制成功即把 controlVersion 当作「已消费到 requested_index」的凭据，服务端在 authority echo 前拒绝基于同一 `currentIndex` 的重复 next/prev（或要求 client 用返回的新 `controlVersion` 且服务端记录 pending 目标 index，next 基于 pending 而非 committed index 计算）。

> 建议：若产品追求「点两下快进两首」的直觉，选 A；若坚持设备回声为准，至少要实现 B 的防重（否则连点 next 卡在同一首是可感知 bug）。**这条请先给结论，我再落实。**

**验收点（口径定了之后补）**：
- A：连发两次 `player.next` → 目标依次为 index 1、index 2；context `currentIndex/trackId` 随命令前进；authority echo 后一致。
- B：连发两次 `player.next`（authority 未 echo）→ 第二次要么被防重拒绝、要么目标为 index 2；不出现两次都打 index 1。
- 两种口径都要覆盖：authority 掉线后 context 不出现「controlVersion 前进但 trackId 停滞」的持久化分歧。

---

## m1 · 断连不清 `_device_playback_states`，幽灵设备 + 无界增长（Minor）

**位置**：`ws_state.py:216-232`（`unregister_session`）、`ws_state.py:192` 一带（`prune_stale_clients`）；读取方 `list_device_playback_states`（被 `_build_playback_context_status_payload` `ws.py:3190` 调用）。

**问题**：断连时 `unregister_session` 清了 sid / client / 订阅，但**从不删** `_device_playback_states[(context_id, client_id)]`。`playback.context.status` 走 `list_device_playback_states` 且**不按在线状态过滤**，于是已下线设备会被当成活跃设备一直上报（携带其最后 position / isAuthority），且该 map 随设备 churn 无界增长。

**复现**：phone-1 发过一次 v2 `playback.update` → 建了 `_device_playback_states[(ctx,"phone-1")]`；phone-1 断连；pad 发 `playback.context.status` → 返回里仍含 phone-1 的陈旧 state。

**改法**：在 `unregister_session`（和 `prune_stale_clients`）里清理该 client 的 `_device_playback_states` 条目；和/或 `list_device_playback_states` 按当前在线 client 过滤。注意别误删仍在线设备的记录。

**验收点**：设备断连后 `playback.context.status` 的 `deviceStates` 不再包含该 client;反复上下线后 map 大小不持续增长。

---

## m2 · authority 掉线后 context 悬挂，无改派/恢复路径（Minor）

**位置**：`ws_state.py:216-232`（断连处理）；触发点 `_handle_v2_context_control`（`ws.py:2019-2023`，authority 离线抛 `PlaybackAuthorityOfflineError`）。

**问题**：authority 设备掉线后，运行时 + 持久化的 context 仍 `authorityClientId=<掉线 client>`。控制端发 `player.pause` → 解析到该 authority、`get_sid_for_client` 返回 None → `authority_offline`。没有服务端改派 / 自动关闭 / 清空 authority 的恢复路径，context 卡死直到新的 create/handoff。

**改法（与 m1 一起做更顺）**：在 `unregister_session`（或断连处理）里检测「authorityClientId == 离线 client」的 context，二选一：
- 标记 `state=paused/closed` 并 `_broadcast_playback_context_state_v2` 通知订阅者；或
- 清空 `authorityClientId`，允许其它 player 重新认领。
同时顺手清该 client 的 `_device_playback_states`（即 m1）。

**验收点**：authority 掉线后，订阅者收到状态变更(paused/closed 或 authority 清空);控制端不再永久 `authority_offline`。

> 注意：这属于「掉线恢复策略」，改动涉及广播语义，建议和产品确认期望行为（暂停 vs 关闭 vs 可认领）后再动。

---

## m3 · create 幂等只看内存，重启后重复 create 版本回退（Minor，与 M1 同源）

**位置**：`create_playback_context`（`ws_state.py:764-767`）；调用方 `_handle_playback_context_create`（`ws.py:3233-3252`）。

**问题**：幂等判断只查 `self._playback_contexts`（内存）：
```python
context = self._playback_contexts.get(playback_context_id)
if context is not None:
    return self._copy_playback_context_locked(context), False
```
重启后内存空、DB 行在（version=7）。重复 `playback.context.create` 同 id → 内存没有 → 返回 `created=True` + 全新 context（version=0，或 M1 修后为 1）；`_create_playback_context_snapshot → createPlaybackContextState`（`update_existing=False`）见到已存在行 → 返回 False 不写。结果：内存/DB 分歧，client 看到的 version 计数器倒退。

**改法**：`create` 前先尝试从持久化恢复（`getPlaybackContextState`），存在则按幂等返回 `created=False` + 已恢复的 context（沿用其版本号）。可在 `_handle_playback_context_create` 里先 `_get_existing_playback_context`，命中则走幂等分支。

**验收点**：create → 清空内存（模拟重启）→ 同 id 再 create → 返回 `created=False`，且返回的版本号 == DB 里的值（不回退）。

> M1 与 m3 同根（内存/DB 版本一致性），建议同一个 commit 一起修并共用测试。

---

## m4 · legacy 客户端发 v2 payload 带 sessionId 未被拒（Minor）

**位置**：v2 远控入口 `_handle_v2_context_control`（`ws.py:1990-1994、2031`）；`_reject_session_id_for_strict_v2`（`ws.py:332-334`）。

**问题**：`_reject_session_id_for_strict_v2` 只在 `strict_v2`（= 客户端有 `playbackContextV2` 能力）时抛错。若一个 **legacy 能力** 客户端发 `player.pause {playbackContextId:..., sessionId:...}`（payload 是 v2 上下文 payload，被路由进 v2 控制），`strict_v2=False` → sessionId 不被拒，只在 `command_payload.pop("sessionId")` 处静默丢弃。无功能危害（sessionId 不再兜底），但计划里「v2 **payload** 也要拒绝 sessionId」这条对该路径未落实，且无测试覆盖此组合。

**改法**：进入 v2 路径的判定统一为「`strict_v2` **或** `_is_context_payload(payload)`」时就拒绝 sessionId。即把 `_reject_session_id_for_strict_v2(payload, strict_v2)` 的第二参改为 `strict_v2 or _is_context_payload(payload)`，在所有共享 action（含 v2 控制、v2 playback.update、handoff cancel/complete）统一。

**验收点**：新增测试——legacy 能力 client 发带 `playbackContextId + sessionId` 的 `player.pause` → 返回 `bad_request`。

> 若担心影响过渡期 legacy 客户端，可先只在 strict_v2 客户端严格，payload 路径记 warning 日志、下阶段再收紧——请按客户端接入节奏定。

---

## m5 · 同一 context 状态用 3 个 action 名 / 2 种形状下发（Minor，需与客户端协商）

**位置**：`_push_playback_context_snapshot`（`ws.py:3202-3212`，subscribe/status 用）、`_broadcast_playback_context_state_v2`（`ws.py:517-539`，实时态用）、`_broadcast_context_queue_v2`（`ws.py:480-502`，队列用）。

**问题**：同一「context 状态」被三种方式下发：
- 订阅/状态快照：action `playback.context.status`，**嵌套** `{playbackContext, deviceStates}`。
- 实时权威更新：action `playback.update`，**扁平** 的 serializePlaybackContextV2(context)。
- 队列变更：action `queue.context.sync`，扁平。

客户端要为「同一个 context 的实时态」处理 3 个 action 名 + 2 种嵌套形状；且 `playback.update` 与**客户端上行发布**的 action 同名，state-echo 与对端设备发布语义上有歧义。

**改法**：实时态复用 `playback.context.status` 同一信封（`{playbackContext, deviceStates}`），让首帧快照与后续更新共用一个 action 名与形状。

**验收点**：订阅后收到的首帧与后续更新 action 名、payload 形状一致；客户端解析路径单一。

> 这条改的是对外协议，**必须先跟 Flutter/Web 客户端负责人对齐**再动，否则破坏现有客户端。可作为独立小任务，不阻塞 M1/M2。

---

## m6 · 4 个 store 函数已实现有单测但从未接线（Minor / 死代码）

**位置**：`ws_store.py:415-604` —— `listUserPlaybackContexts` / `getPlaybackContextWithDeviceStates` / `deletePlaybackContext` / `expirePlaybackContext`。有 `tests/base/test_emo_ws_store.py` 覆盖，但 `ws.py` 从不 import/调用。

**问题**：v2 context 没有过期 / 硬删除 / 按用户列举的接线。`playback.context.close` 只经 `_update_playback_context_snapshot` 翻 state，DB 行与其 `EmoDevicePlaybackState` 行长期驻留。

**改法（二选一，按第一阶段范围定）**：
- 接线：`close`/timeout 走 `expirePlaybackContext` 或 `deletePlaybackContext`；status/list 场景用 `getPlaybackContextWithDeviceStates`/`listUserPlaybackContexts`。
- 或明确标注「留待第二阶段（生命周期 / GC）」，避免被当成已完成能力。

**验收点**：若接线，`close` 后 DB 行按预期 expired/deleted；若暂不接，在计划/代码注释标注 out-of-scope。

---

## 建议 commit 拆分

```
fix(emo-v2): keep create counters consistent between memory/ack/db   # M1 + m3
fix(emo-v2): stop handoff.start from implicitly creating context     # M2
fix(emo-v2): <optimistic-advance | anti-replay> for context control  # M3（口径定后）
fix(emo-v2): prune device states + recover orphaned authority on dc  # m1 + m2
fix(emo-v2): reject sessionId for v2 payloads from legacy clients     # m4
chore(emo-v2): unify context state action/shape (需客户端协同)        # m5（协商后）
chore(emo-v2): wire or scope out context lifecycle helpers            # m6
```

## 复核确认（供工程师参考）

以下均已对照当前工作区代码逐行确认属实，非推测：
- M1：`ws_store.py:354-357/373-376` 的 `or 1` vs `ws_state.py:782-785` 的 0 vs `ws.py:3245-3251` ack 回内存值。
- M2：`ws.py:3488` 用 `_get_or_restore_playback_context`，其 `ws.py:3121-3124` 从 `getQueueState` 合成并写 `_playback_contexts`；其它 v2 handler 用 `_get_existing_playback_context`（`ws.py:3143-3152`，无合成）。
- M3：`ws.py:2084-2092` 只把 index 放进 `command_payload`；`ws_state.py:969-977` 的 `apply_playback_context_control` 不写 `currentIndex/trackId`；`test_v2_player_next_uses_context_queue` 断言 index 保持 0（有意设计）。

## 未列入（第一阶段明确不做，非缺陷）

follow.* / broadcast.* 的完整 context 化、local queue 归属重定义、DB 新列、/player Web 迁移 —— 均属第二阶段，本轮不评。
