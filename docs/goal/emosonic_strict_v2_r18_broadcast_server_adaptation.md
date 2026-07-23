# Goal: EmoSonic strict-v2 2.8.0 / r18 群播服务端落地

> 状态：Planned（仅完成契约审查和实施拆解，尚未修改服务端）
>
> 制定日期：2026-07-23
>
> 目标协议：PlaybackContext strict-v2 `2.8.0` / contract r18
>
> 最终契约来源：`ref/emosonic_strict_v2_socketio_server_contract - 1901.md`
>
> 目标权威路径：`specs/emosonic_strict_v2_socketio_server_contract.md`
>
> 制定时基线 commit：`f3d549e162cc2594f3cd24da33eb33f28048cc64`
>
> 实施边界：本 Goal 负责 Supysonic 服务端、数据库迁移、仓库内 strict validator、Web strict
> 客户端兼容、自动化测试和服务端联调；不修改外部 Flutter 或 Windows 工程。

## 一、Goal 结论

把当前 strict-v2 `2.4.0` 服务端升级为 `2.8.0`，并把旧的“独立群播队列”实现替换为 source
PlaybackContext 的派生镜像状态机。

最终必须满足以下产品规则：

1. 群播复制 source 当前正在执行的播放任务，不创建第二套队列、cursor 或 PlaybackContext；
2. source 继续通过普通 Context command 和 `playback.update` 执行自己的任务；
3. source 结束群播后只解除复制关系，不恢复、不暂停、不 seek、不替换队列；
4. ordinary participant 进入群播前冻结自己的原 Context，terminal 后恢复原任务；
5. controller-only 只接收群播界面状态，不操作本机音频；
6. ordinary participant 只通过 `broadcast.feedback` 上报镜像执行结果，不能污染原 Context；
7. source、ordinary、controller-only 三类角色使用同一 BroadcastSnapshot，但执行权限和
   per-delivery 字段严格分开；
8. terminal stop、普通 Context 屏障释放、`restorePending`、terminal Snapshot 和 outbox 必须在
   一个数据库事务中同成同败；
9. 相同 start intent、stop、feedback、rejection、resync 和 terminal replay 必须幂等；
10. 完整群播记录、delivery ledger、terminal recovery 和重启恢复必须持久化；
11. `supportsBroadcast:true` 只在 r18 全部状态机和验收通过后开放，中间实现始终协商为 false；
12. `schemaHash` 只是可选诊断信息，缺失、变化或格式异常不得限制连接、Core 或任何 capability。

当前实现与 r18 的差距是结构性的，不能继续在旧 `ws_state.py` Broadcast 字典上零散补字段。本
Goal 采用“先持久化和原子边界，再接 handler 和推送，最后开放 capability”的顺序实施。

---

## 二、权威顺序与 schemaHash 决定

### 2.1 实施时的权威顺序

1. Goal 0 提升并修正后的 `specs/emosonic_strict_v2_socketio_server_contract.md`；
2. 本 Goal；
3. `ref/emosonic_strict_v2_socketio_server_contract - 1901.md`；
4. 当前服务端代码和测试；
5. r11、r12 和其他历史 Goal、旧 Broadcast 文档。

r17 从未发布，r18 继续使用 `protocolVersion:2.8.0`，不增加 r17 兼容分支。strict 连接不得因旧
payload 自动进入 legacy Broadcast handler。

### 2.2 schemaHash 的项目决定

1901 文件第 3.3 和验收条款仍残留“schemaHash 必填、固定 64 位格式、非法时 fail-closed”的旧句子。
它们与项目负责人已经明确的最终决定冲突。Goal 0 必须先把权威契约改成以下口径：

- `strictV2.schemaHash` 是可选部署观测字段；
- 服务端可以继续生成并返回当前 hash；
- 客户端和服务端都不得要求固定值、固定格式或跨版本不变；
- 缺失、变化或格式异常不得导致注册失败、断开连接、停止 strict Core、关闭 capability 或回退
  legacy；
- 协议兼容只依据 `protocolVersion` 和 `negotiatedCapabilities`；
- 不增加 contract hash、build commit、manifest evidence 或签字门禁；
- 开发和测试环境不要求精确 `schemaHash` 或 `serverBuildCommit` 才能联调；
- production 仍按协议 schema、权限、cursor、身份和持久化规则失败闭合。

该决定不是新的 Broadcast wire 字段，不需要提升 protocolVersion。

---

## 三、当前仓库基线

### 3.1 已有基础

当前服务端已经具备：

- strict request/output validator、统一 provenance 注入和 requestId cache；
- `connectionNonce`、`connectionEpoch:1` 和连接替换隔离；
- PlaybackContext、DevicePlaybackState、control transaction、prepare、local intent 和 Handoff
  持久化；
- `epoch/version/queueRevision/controlVersion/appliedControlVersion` 基础状态机；
- Context/pair 进程内串行锁和 Peewee 数据库事务；
- server-routed player/queue control 和 event-confirmed `playback.update`；
- Socket.IO 连接、用户、请求、控制、创建、Handoff 和 Broadcast start 限流；
- 256 KiB transport 上限、发送缓冲保护、单 realtime worker 限制和 graceful shutdown；
- SQLite、MySQL、PostgreSQL schema/migration 框架；
- 旧 strict Broadcast handler、断线 timeout 和基础测试。

### 3.2 当前关键差距

制定本 Goal 时，仓库证据表明：

1. 权威 `specs/`、注册描述符和运行时 metadata 仍是 `2.4.0/r11`；
2. `broadcast.start` 仍接受 `queueSongIds/currentIndex/positionMs/autoPlay`；
3. 当前 Broadcast 保存独立 `version/queueRevision/controlVersion/epoch`，并创建
   `contextType:"broadcast"` 的第二套播放任务；
4. source 被加入 `participants`，没有 ordinary-only participants 语义；
5. participants 只按 clientId 保存，没有冻结 deviceSessionId pair；
6. 当前 participant 结果仍走 `playback.update` 或简化 state，没有 `broadcast.feedback`；
7. 客户端仍可发送 `broadcast.queue.sync`；
8. 当前 stop 直接把 Broadcast `state` 改为 stopped，没有独立 `lifecycleState`；
9. source 断线使用 `broadcast.pause`，没有 `waitingForSource/resume`；
10. 没有 `intentId` 长期幂等、`broadcastRevision`、source cursor 副本或固定 action 映射；
11. 没有 suspended Context/binding 写屏障和 source ownership fence；
12. 没有 `restorePending`、recovery slot、terminal outbox 或 7 天 full-to-compact recovery；
13. 没有 per-pair deliveryId、revision ledger、resync、deadline 和 participant syncStatus；
14. 没有 `broadcast.feedback.rejected` 及 rejection 后的新执行 delivery；
15. `playback.update`/`queue.context.sync` 没有必需的 `positionSampledAtServerMs` 和
    `playbackRate`；
16. server ping 只返回时间，没有记录当前 nonce 的 ping 数量和最近时间；
17. `effectiveAtPlayback` 仍错误依赖 `playbackPrepare`；
18. readiness 仍依赖固定 contract SHA 和 evidence manifest；
19. `schemaHash` 仍被输出 validator 和仓库内 Web strict client 当成必填固定格式；
20. 当前 Broadcast 主状态仅在内存，重启无法满足 terminal 原子释放和恢复义务。

### 3.3 制定时测试基线

```text
python -m unittest \
  tests.base.test_emo_strict_v2_broadcast \
  tests.base.test_emo_strict_v2_contract \
  tests.base.test_emo_protocol_metadata \
  tests.base.test_emo_strict_v2_readiness

结果：66 tests，OK
```

这 66 项验证的是当前 `2.4.0` 和旧 Broadcast 行为，只作为改造前回归基线，不能作为 r18
`supportsBroadcast:true` 的依据。

---

## 四、范围

### 4.1 本 Goal 包含

- 把 1901 最终契约提升到 `specs/`，并落实 schemaHash 项目决定；
- strict-v2 `2.8.0` 注册描述符、validator、action inventory 和 fixtures；
- Core 的 `positionSampledAtServerMs`、`playbackRate`、mandatory pong 和 clock gate；
- source-derived BroadcastSnapshot 和 source Context 原子耦合；
- start intent、participant pair、Context 屏障和 recovery slot；
- Broadcast control、source update 派生、自然切歌、progress 合并和确定 action；
- ordinary `broadcast.feedback`、participantStates、deadline、delivery ledger 和 rejection；
- ordinary resync、source waiting/resume、terminal replay 和 compact restore；
- stop、source 变 idle、source timeout 和服务重启的原子 terminal；
- SQLite、MySQL、PostgreSQL base schema 与 migration；
- 仓库内 Web strict client 对 2.8 metadata/schemaHash 的兼容；
- 单元、Socket、并发、deadline、重启、migration、retention 和真实双设备联调；
- 逐步提交、测试、推送和最终工作区检查。

### 4.2 本 Goal 不包含

- 修改 Flutter 或 Windows 源码；
- 为 r17、r12 或旧 strict Broadcast shape 建兼容分支；
- 把 strict 校验失败回退到 legacy handler；
- 删除非 strict 的 legacy Socket.IO surface；
- Redis、broker、sticky session 或多 realtime worker；
- 把 BroadcastSnapshot 做成第二个 PlaybackContext；
- 持久化 ordinary participant 的镜像队列作为新播放任务；
- 自动修改生产配置或自动打开 `supportsBroadcast`；
- 修改媒体库、扫描器、daemon、转码或用户私有 `supysonic.conf`；
- 增加 contract hash、metadata pin、evidence manifest 或签字流程。

---

## 五、固定实现原则

### 5.1 source Context 是唯一播放事实源

BroadcastSnapshot 只能保存 source Context 和 source DevicePlaybackState 的派生副本：

```text
source PlaybackContext
  -> source DevicePlaybackState
    -> BroadcastSnapshot
      -> ordinary per-pair delivery
```

不得出现：

```text
Broadcast 独立 queue
Broadcast 独立 currentIndex cursor
Broadcast 独立 controlVersion
Broadcast 专用 source PlaybackContext
```

`sourceEpoch/sourceVersion/sourceQueueRevision/sourceControlVersion` 必须在 active/waiting 期间与所引用
source Context 精确相等；terminal 后冻结为历史值。

### 5.2 角色优先级固定

同一 Socket 同时符合多个身份时，只执行最高优先级分支：

```text
sourceAuthority > ordinaryParticipant > controllerOnly
```

- sourceAuthority：只执行普通 Context command，Broadcast push 只更新 lifecycle/UI；
- ordinaryParticipant：执行 mirror target，并发送 `broadcast.feedback`；
- controllerOnly：只更新 lifecycle/UI，不生成 delivery、deadline 或 feedback。

### 5.3 Snapshot、revision 和 delivery 分域

- `broadcastRevision` 标识持久化 BroadcastSnapshot/lifecycle 内容；
- 每次真正提交并准备发送新 Snapshot 时严格 `+1`；
- 多 recipient fanout 不重复加号；
- status、feedback、幂等重放和 ordinary resync 不增加 revision；
- `deliveryId/effectiveAtServerMs/serverTimeMs/deliveryPositionMs/action` 是 per-pair delivery 数据；
- resync 可以替换相同 revision 的 delivery，但不得修改 Snapshot anchor。

### 5.4 接受顺序固定

所有会产生 Broadcast 或 Context mutation 的入口遵守：

```text
解析并验证请求
  -> 解析全部 source/participant 当前 pair 和 sid
    -> 预留发送容量（需要可靠 source command 时）
      -> 获取排序后的 Context/pair/Broadcast 锁
        -> 数据库事务内重新验证
          -> 写 canonical state、ledger、fence、outbox、幂等结果
            -> commit
              -> 按角色逐 sid 构造并发送
                -> 最后结算请求
```

不得先 emit 再补写 Snapshot、deadline、terminal 或幂等结果。发送失败不得回滚已经送达其他 Socket
的消息；必须依靠已提交 outbox、当前 deliveryId 和幂等重放收敛。

### 5.5 ordinary 原 Context 只冻结，不复制

服务端只保存 ordinary participant 的：

- `suspendedPlaybackContextId`；
- 冻结 client/device pair；
- 入口 `epoch/version/queueRevision/controlVersion/appliedControlVersion`；
- restorePending 和 terminal recovery 所需 target。

服务端不保存 Flutter 原任务完整队列副本，不把镜像执行写入原 Context。原任务内容由原
PlaybackContext 和客户端本地恢复记录共同负责。

### 5.6 terminal 状态分域

```text
BroadcastSnapshot.lifecycleState = stopped
BroadcastSnapshot.state = terminal 前最后 source/mirror anchor
ordinary terminal feedback.state = stopped
```

terminal feedback 的 stopped 表示镜像执行已销毁，不要求等于 Snapshot.state。stop 不修改 source
Context 或 source DevicePlaybackState。

### 5.7 schemaHash 不参与协商

即使服务端继续输出合法 SHA-256，也只允许记录和展示。任何 request validator、output validator、
Web strict client、readiness、CI 或部署脚本都不得通过 schemaHash 缺失、变化或格式异常限制连接或
capability。

---

## 六、目标数据模型

以下是内部建议模型。实现时允许按 Peewee 和数据库限制调整列名，但不能减少表达的状态或原子
边界。核心并发、筛选和 retention 字段应使用明确列；完整 wire 对象可以另存 canonical JSON。

### 6.1 `EmoBroadcast`

每个 broadcastId 一行：

| 字段 | 用途 |
| --- | --- |
| `broadcast_id` | 全局唯一群播 ID |
| `user_name` | authenticated user 隔离 |
| `playback_context_id` | 唯一 source Context |
| `intent_id` | start 长期幂等键 |
| `owner_client_id` | start 请求者 |
| `authority_client_id` | 冻结 source client |
| `authority_device_session_id` | 冻结 source device |
| `lifecycle_state` | active / waitingForSource / stopped |
| `broadcast_revision` | 当前 canonical revision |
| `snapshot_json` | 当前完整 BroadcastSnapshot |
| `authority_disconnect_deadline_ms` | waiting 30 秒 deadline |
| `terminal_at_ms` | terminal 时间 |
| `full_expires_at_ms` | 7 天 full record 压缩时间 |
| `created_at/updated_at` | 运维和清理索引 |

索引至少覆盖：

- `broadcast_id` unique；
- `(user_name, playback_context_id, lifecycle_state)`；
- `(lifecycle_state, authority_disconnect_deadline_ms)`；
- `(lifecycle_state, terminal_at_ms)`。

### 6.2 `EmoBroadcastIntentOutcome`

主键作用域：

```text
(userName, playbackContextId, ownerClientId, intentId)
```

保存：

- request fingerprint；
- 首次 broadcastId；
- 最终 participants 和 skippedClientIds；
- 规范化 start ACK；
- terminalBroadcastRevision；
- 规范化 stop ACK outcome。

完整 Broadcast 压缩后仍保留到 source Context close。每个 source Context 最多 1024 条；达到上限只
拒绝新 intent，旧 intent 仍可重放。

### 6.3 `EmoBroadcastFence`

统一表达 source ownership、ordinary suspended Context 和 restorePending。建议每个被占用资源一行：

| 字段 | 用途 |
| --- | --- |
| `resource_key` | 可确定生成的 context/pair 唯一键 |
| `broadcast_id` | 所属 Broadcast |
| `role` | source / ordinary |
| `phase` | nonterminal / restorePending |
| `playback_context_id` | source 或 suspended Context |
| `client_id/device_session_id` | 冻结 pair |
| `recovery_slot_reserved` | ordinary recovery slot |

`resource_key` 唯一约束负责阻止同一 Context 或 pair 被两个 Broadcast 同时占用。terminal 时删除 source
fence；ordinary fence 从 nonterminal 原子变成 restorePending，直到 terminal applied feedback 后删除。

### 6.4 `EmoBroadcastParticipant`

每个冻结 ordinary pair 一行，unique：

```text
(broadcastId, clientId, deviceSessionId)
```

保存：

- `suspendedPlaybackContextId`；
- suspended epoch/version/queue/control/applied 基线；
- `restorePending`、terminal confirmed；
- `targetBroadcastRevision/targetDeliveryId`；
- `deadlineBroadcastRevision/feedbackDeadlineAtServerMs`；
- `syncStatus`；
- 已成功 applied 组；
- failed/timedOut 条件组；
- last feedback clientSeq/time；
- terminal restoreCompleted。

`online` 不持久化为事实，status 时按冻结 pair 的当前 Socket presence 计算。

### 6.5 `EmoBroadcastRevision`

每个 canonical revision 一行，unique：

```text
(broadcastId, broadcastRevision)
```

保存 immutable Snapshot、canonical action 和创建时间，用于：

- feedback target 校验；
- retained ledger floor；
- 幂等 replay；
- terminal compact；
- 判断 revision_expired / unknown / ahead。

至少保留最近 512 个 revision，并且 10 分钟反馈窗口内不得提前删除。

### 6.6 `EmoBroadcastDelivery`

每次 ordinary execution attempt 一行：

| 字段 | 用途 |
| --- | --- |
| `delivery_id` | Broadcast 内不复用的唯一 ID |
| `broadcast_id/revision` | 指向 immutable target |
| `client_id/device_session_id` | 精确冻结 pair |
| `action` | 本次实际 push action |
| `effective_at_server_ms/server_time_ms` | 可选计划时间 |
| `delivery_position_ms` | 本次投影目标位置 |
| `payload_json` | 可原样重放的 per-recipient payload |
| `connection_nonce` | attempt 绑定的物理连接，可空表示待重连 |
| `is_current` | 该 pair/revision 当前有效 delivery |
| `delivery_status` | pending/sent/settled/superseded |
| `created_at_ms` | 无 effective-at deadline 起点 |

同 revision resync 新增 delivery 并把旧 attempt 标为 superseded；旧 delivery feedback 不得关闭新
deadline。纯传输重发复用同一 delivery 行和计划时间。

### 6.7 `EmoBroadcastFeedbackSettlement`

主键作用域：

```text
(playbackContextId, broadcastId, clientId, deviceSessionId,
 connectionNonce, connectionEpoch, clientSeq)
```

保存 request fingerprint、canonical confirmation 或 rejection、首次 rejection 生成的 follow-up
deliveryId。相同 content 重放原结果；不同 content 或倒退返回 `client_sequence_conflict`，不得再创建
第二个 follow-up delivery。

### 6.8 `EmoBroadcastTerminalRecovery`

full record 达到 7 天后，为每个未确认 pair 原子生成一行，字段严格覆盖契约：

- user、source playbackContextId、broadcastId；
- clientId/deviceSessionId；
- terminalBroadcastRevision/currentDeliveryId；
- suspendedPlaybackContextId 和五个 suspended cursor；
- lastAppliedBroadcastRevision；
- terminal queueIndex/trackId/positionMs/playbackRate；
- terminalAtServerMs。

unique `(userName, clientId, deviceSessionId)` 保证同一 restorePending pair 最多一条 compact record；
按 terminalAtServerMs 建清理/诊断索引。

### 6.9 schema 和 migration

需要同步修改：

- `supysonic/db_layer/emo.py`；
- `supysonic/db.py` 和 db facade exports；
- `supysonic/schema/sqlite.sql`；
- `supysonic/schema/mysql.sql`；
- `supysonic/schema/postgres.sql`；
- 三种 provider 的同日期 migration；
- `supysonic/db_layer/schema.py` 的 `SCHEMA_VERSION`；
- DB model/schema parity 和 migration tests。

旧 `contextType:"broadcast"` 数据不得自动解释成 r18 Broadcast。旧 strict shape 从未发布，因此不做
有损转换；迁移不自动删除旧记录，strict r18 路径也不加载它们。legacy 非 strict 路径如需继续使用，
必须与新模型隔离。

---

## 七、原子事务和锁设计

### 7.1 锁资源

建议扩展当前 `_strict_playback_context_lock_set`，增加确定排序的资源键：

```text
context:<playbackContextId>
pair:<userName>:<clientId>:<deviceSessionId>
broadcast:<broadcastId>
```

所有入口按字符串排序后获取锁，再进入数据库事务。不得在不同 handler 中自行改变顺序。

### 7.2 start 原子事务

start 必须在同一事务完成：

1. 检查同 intent outcome；
2. 锁定并重新读取 source Context、source DevicePlaybackState 和 pending control；
3. 校验 source pair、能力、clock、playing、fresh 和 applied cursor；
4. 锁定每个候选 ordinary pair 和唯一 suspended Context；
5. 排除无唯一 Context、已有 fence、prepare/Handoff 瞬态或 restorePending 的目标；
6. 预留每 user recovery slot，应用 20 participant 上限；
7. 写 Broadcast、revision 1、intent outcome；
8. 写 source/ordinary fences、participants、初始 deliveries、deadline 和 outbox；
9. commit 后逐角色发送；
10. 返回规范化 start ACK。

任一数据库写入失败时不得留下 broadcastId、fence、slot、participant 或 delivery 孤儿。

### 7.3 Broadcast control 原子事务

不能先调用现有 `mutateStrictPlaybackContextControl` 提交，再单独更新 Broadcast。需要把普通 Context
control primitive 重构为可在同一 transaction 中完成：

- base cursor 校验；
- source Context cursor 前进；
- pending control transaction；
- 单次 effective-at/server-time；
- BroadcastSnapshot source cursor 副本；
- `broadcastRevision + 1`；
- source command outbox；
- ordinary delivery/outbox；
- request idempotent outcome。

source 普通 command 与全部 ordinary mirror push 必须复用同一对时间值。

### 7.4 source update/queue sync 原子事务

需要给 `applyStrictPlaybackUpdate` 和 `mutateStrictPlaybackContextQueue` 增加 Broadcast-aware 提交能力。
在同一 Context transaction 中：

- 先结算/修改唯一 source Context；
- 对比提交前后的实际字段；
- 按固定优先级选择一个 action；
- 等值 remote committed 不增加 Broadcast revision；
- 有实际 correction 才写新 revision；
- queue 清空为 idle 时先物化最后非空 terminal，再提交 source idle；
- 任一子操作失败则整个事务无副作用。

### 7.5 stop 原子事务

stop、source timeout、source 变 idle、重启 terminal 共用一个 store primitive：

1. 若已经 terminal，返回首次 stop outcome；
2. Snapshot.lifecycleState 改为 stopped，Snapshot.state 保持最后 anchor；
3. `broadcastRevision + 1`，source cursors 冻结；
4. 删除 source nonterminal fence；
5. ordinary fence 改成 restorePending；
6. 写 terminal revision、per-pair delivery/outbox 和 full retention 时间；
7. 把 terminal revision/stop ACK 写入 intent outcome；
8. commit；
9. 再向 source、owner observer、ordinary 发送 terminal push。

不得调用 source pause/stop/seek/queue mutation。

### 7.6 terminal feedback 原子事务

接受 terminal applied feedback 时同一事务：

- 校验当前 delivery、revision、terminal target 和 restoreCompleted；
- 更新 participant applied 状态；
- 清除该 pair restorePending fence；
- 释放 recovery slot；
- 删除 compact recovery（若存在）；
- 结算 feedback dedupe；
- commit 后只向请求 Socket发送 confirmation。

failed、timedOut 和非法 feedback 均不得清除 restorePending。

---

## 八、分阶段实施计划

### Goal 0：冻结权威输入

改动：

- 将 1901 内容提升到 `specs/emosonic_strict_v2_socketio_server_contract.md`；
- 同步 schemaHash 项目决定；
- 标记旧 ref/change、r11/r12 Goal 为历史资料，不改写其历史内容；
- 建立 r18 REQ-001 至 REQ-067 对代码和测试的映射表。

完成条件：

- `specs/` 是唯一当前权威；
- 契约内部不再同时出现“schemaHash 仅观测”和“非法 hash fail-closed”；
- 后续实现不从 1901 临时文件或历史 Goal 推导 wire shape。

### Goal 1：2.8 Core schema 和 metadata

改动：

- 注册 capabilities 固定为 10 个 bool；
- protocolVersion 最终目标设为 2.8.0，但在 Core schema 未完成前不提前宣告 ready；
- schemaHash 从 required/格式校验移除，保留可选输出；
- serverBuildCommit 继续允许 `unknown`，不参与能力判断；
- `effectiveAtPlayback` 不依赖 `playbackPrepare`；
- pong `serverTimeMs` 改为必需；
- `playback.update` 和 `queue.context.sync` 加入位置采样时间和速度；
- 更新 Web strict client、descriptor、fixtures 和 output validator。

完成条件：

- schemaHash 缺失、变化、空值、类型或格式异常都不影响注册；
- protocolVersion/capabilities 是唯一协商依据；
- 2.8 Core request/output schema 全部闭合。

### Goal 2：持久化模型和 store primitives

改动：

- 新增第六节模型、三数据库 schema 和 migration；
- 增加 Broadcast resource locks 和统一 transaction helpers；
- 实现 create/read/list/update/terminal/compact store API；
- 实现 recovery slot 和 20/256/512/1024 上限；
- 用数据库记录替代 strict Broadcast 内存主状态。

完成条件：

- store tests 可以在不启动 Socket.IO 的情况下完整驱动状态机；
- 并发 start 只有一个赢家；
- 事务失败没有孤儿 fence、slot、delivery 或 outbox；
- SQLite/MySQL/PostgreSQL model/schema parity 通过。

### Goal 3：clock、采样时间和 source eligibility

改动：

- session state 记录当前 nonce 的有效 ping 数和最近 ping 时间；
- disconnect/replace 时清空 clock gate；
- DevicePlaybackState 保存接收时间、位置采样时间和 playbackRate；
- 校验 future sample、freshness、settled/applied cursor 和 track；
- 提供统一 source/ordinary effective-at eligibility helper；
- 已知 Track duration 时提供 feedback position 上限校验。

完成条件：

- 少于 3 次 ping 或最近 ping 超过 15 秒不能进入 effective-at 角色；
- source start 同时检查 serverUpdatedAtMs 和 positionSampledAtServerMs 不超过 2000ms；
- playing position 只从采样时间投影。

### Goal 4：source-derived start 和角色 fanout

改动：

- `broadcast.start` 新闭合 payload 和长期 intent；
- source playing/fresh/settled/capability/clock 检查；
- ordinary participant 筛选、pair 冻结、Context barrier 和 recovery slot；
- 初始 Snapshot/revision/delivery/deadline/outbox；
- source lifecycle、controller observation、ordinary execution 三种 per-recipient push；
- source 不重新执行 queue/play/seek/rate。

完成条件：

- start 请求不接受任何播放快照；
- Snapshot 来自同一 source 原子读取；
- source 不在 participants/participantStates；
- controller-only 不产生 delivery；
- 相同 intent 跨 Socket 重放首次 ACK，不重发 start。

### Goal 5：Context 屏障和 source ownership

改动：

- 在所有 Context/binding mutation store API 中调用同一个 fence checker；
- active/waiting ordinary mutation 返回带 canonical cursors 的 conflict；
- restorePending ensure 返回可重放 restore_in_progress；
- source normal queue/player/update 允许，close/Handoff/rebind 拒绝；
- Handoff source/target、ready、complete 同样检查 fence；
- 只读 list/status/subscribe/unsubscribe 保持可用。

完成条件：

- 契约列出的所有 mutation 路径均有正向和拒绝测试；
- 拒绝分支不改变 Context、cursor、binding、outbox 或 push；
- ensure 不能穿透 active 或 restorePending fence。

### Goal 6：Broadcast control 与 source 派生更新

改动：

- play/pause/seek/playItem 使用 source base cursor 和普通 control transaction；
- 客户端 `broadcast.queue.sync` 改为 not_supported；
- source `queue.context.sync` 派生 server-only queue.sync；
- source playback.update 按固定映射选择 action；
- passive playing progress 最多每秒实际 push 一次；
- Snapshot position/serverUpdatedAtMs 锚定 effective-at；
- 显式 command 等值 committed 只结算 source transaction；
- queue 清空走原子 terminal + source idle。

完成条件：

- 一次 source 提交最多一个 action 和一个 revision；
- 不通过位置差值或漂移阈值猜 seek/progress；
- source command 和 ordinary mirror 时间值逐值相等；
- status 返回 immutable Snapshot anchor。

### Goal 7：feedback、participantStates 和 deadline

改动：

- 新增 event-confirmed `broadcast.feedback` handler；
- 实现 applied/failed 互斥 shape、error enum 和 source forbidden；
- 实现 delivery/revision/track/index/state/rate 校验；
- position 只做类型、非负和已知媒体长度范围校验；
- 实现独立 clientSeq scope 和 canonical confirmation replay；
- 实现 earliest-unconfirmed deadline 状态机；
- 增加 deadline sweep/background task；
- status 输出全部条件组并保留关闭后的 deadline 字段。

完成条件：

- feedback 不修改 BroadcastSnapshot、source Context 或 cursor；
- pending/lagging 才运行 timer；
- 后续 progress 不延期最早 deadline；
- failed/timedOut 后合法 applied 可以收敛；
- 幂等 feedback 不重建 deadline。

### Goal 8：rejection、resync 和 ordinary 重连

改动：

- 分类 revision_expired/unknown/ahead；
- rejection 只向请求 Socket 发送并结算 clientSeq；
- rejection 后原子创建一个新可执行 delivery；
- active/waiting 使用 resync；
- active playing/paused/stopped 均生成新计划时间；
- waiting 可以省略计划时间；
- Snapshot/revision 保持不变，只更新 per-pair delivery；
- register 后在普通 Context mutation 之前安排 replay。

完成条件：

- 旧 delivery feedback 不能关闭新 attempt；
- 相同 rejection 重放不创建第二个 delivery；
- 其他 participants/source/controller 不收到 ordinary resync；
- status 读取不能代替新 execution delivery。

### Goal 9：source waiting/resume 和 30 秒 timeout

改动：

- source 断线原子进入 waitingForSource；
- 计算安全 pause anchor，但不修改 source Context；
- owner/controller 断线不改变 lifecycle；
- ordinary 断线只改变 presence；
- 相同 source pair 重连后等待合法 fresh playback.update 和 cursor 对账；
- 按实际 playing/paused/stopped 发送唯一 resume；
- 不同 deviceSession 不继承；
- 30 秒到期共用 terminal primitive。

完成条件：

- waiting/resume 只改变 Broadcast revision，不改变 source cursor；
- source resume 不要求手工 broadcast.play；
- timeout 后旧 broadcastId 不可复活。

### Goal 10：terminal、7 天 retention、compact restore 和重启

改动：

- 手工 stop、source idle、source timeout、restart 共用 terminal primitive；
- full terminal replay 在普通 Context command 前执行；
- 每个新物理连接生成新 terminal deliveryId；
- source terminal replay 只清 lifecycle，不要求 feedback；
- 7 天压缩先写全部 compact recovery，再删除 full data；
- compact status recovery one-of 和 broadcast.restore；
- terminal applied 清 restorePending/recovery slot/compact row；
- 启动时 active/waiting Broadcast 原子 terminal；
- 重建 deadline、disconnect timeout 和 compaction sweeper。

完成条件：

- 重复 stop 只重放首次 ACK，不增加 revision；
- stop 后 source 音频没有额外 command；
- 未确认 ordinary pair 始终存在 full 或 compact 恢复路径；
- `supportsBroadcast:false` 时仍能完成已存在 terminal drain；
- 服务重启不会遗留 active/waiting fence。

### Goal 11：readiness、清理旧 strict 分支和最终开放

改动：

- 删除 strict handler 中旧 queue/start/participant feedback 分支；
- legacy 非 strict 路径继续隔离；
- readiness 改为实现支持、部署开关和客户端请求能力的简单求交集；
- 删除固定 contract SHA/evidence manifest 对 runtime capability 的限制；
- 更新 action inventory、fixtures、Web strict client 和文档；
- `supportsBroadcast` 实现开关最后才改为 ready；
- production/development 配置默认仍关闭，由部署配置显式开启。

完成条件：

- strict 客户端不能进入旧 Broadcast handler；
- schemaHash/serverBuildCommit 不影响连接和能力；
- `supportsBroadcast:true` 时 r18 全部 conformance 已通过；
- profile 从 true 关闭后，已有 terminal drain 仍继续。

---

## 九、接口和字段迁移清单

### 9.1 客户端请求

| action | r18 服务端变更 |
| --- | --- |
| `device.register` | 10 bool capability；schemaHash 可选且不门禁 |
| `system.ping` | pong 必含 serverTimeMs，并记录 ping gate |
| `playback.update` | 增加 positionSampledAtServerMs、playbackRate |
| `queue.context.sync` | 增加 positionSampledAtServerMs |
| `playback.context.ensure` | active barrier conflict；restorePending 返回 restore_in_progress |
| `broadcast.start` | 只收 playbackContextId、intentId、可选 participants |
| `broadcast.status` | full/recovery one-of |
| `broadcast.play/pause/seek` | baseControlVersion 必需 |
| `broadcast.playItem` | baseQueueRevision、baseControlVersion 必需 |
| `broadcast.feedback` | 新 event-confirmed applied/failed shape |
| `broadcast.stop` | action-only ACK，长期幂等 |

### 9.2 server-only Broadcast action

必须支持并闭合输出：

```text
broadcast.start
broadcast.play
broadcast.pause
broadcast.seek
broadcast.playItem
broadcast.queue.sync
broadcast.progress
broadcast.state.sync
broadcast.waiting
broadcast.resume
broadcast.resync
broadcast.stop
broadcast.restore
broadcast.feedback
broadcast.feedback.rejected
```

所有 server push `type:"event"`、省略 requestId、逐 sid 注入 provenance，且禁止 target/session 字段。

### 9.3 从 strict Broadcast 删除的旧字段

```text
version
queueRevision
controlVersion
epoch
timelineId
controlPolicy
followDelayMs
participant.sessionId
start.queueSongIds
start.currentIndex
start.positionMs
start.autoPlay
```

### 9.4 新 BroadcastSnapshot 字段

```text
playbackContextId
broadcastId
intentId
ownerClientId
authorityClientId
authorityDeviceSessionId
lifecycleState
broadcastRevision
queueSongIds
currentIndex
trackId
positionMs
state
playbackRate
sourceVersion
sourceQueueRevision
sourceControlVersion
sourceEpoch
serverUpdatedAtMs
participants
```

ordinary execution push 另外增加 per-delivery `deliveryId`，并按 action/lifecycle 条件增加
`effectiveAtServerMs/serverTimeMs`。

---

## 十、确定 action 的唯一映射

实现统一纯函数，输入提交前/后 source Context、DevicePlaybackState、transaction 和 lifecycle，输出
一个 action：

| 变化 | action |
| --- | --- |
| 成功 start | start |
| 显式 Broadcast control | play/pause/seek/playItem |
| paused/stopped -> playing | play |
| playing -> paused/stopped | pause |
| 已知 seek 或 localUser 同 track/index 只改位置 | seek |
| queue/index/track 改变 | queue.sync |
| passive 正常 playing 位置推进 | progress |
| rate、paused/stopped 或其他 correction | state.sync |
| source 断线 | waiting |
| source fresh 重连 | resume |
| ordinary pair 重连 | resync |
| terminal | stop |

多字段优先级固定：

```text
stop
> queue/track/index
> waiting/resume
> play/pause
> seek
> playbackRate/state correction
> progress
```

禁止用 position 差值、时间阈值或漂移大小猜 action。

---

## 十一、deadline 和 retention 参数

实现使用单一常量来源：

| 参数 | 值 |
| --- | --- |
| effective-at 最小 lead | 250ms |
| action 最大可追赶迟到 | 1000ms |
| source state freshness | 2000ms |
| ordinary feedback deadline | 8000ms |
| source reconnect timeout | 30000ms |
| feedback retained 窗口 | 至少 10 分钟 |
| revision ledger | 至少最近 512 个且满足反馈窗口 |
| full terminal retention | 7 天 |
| ordinary participants | 最多 20 |
| per-user recovery slots | 最多 256 |
| per-Context intent outcomes | 最多 1024 |

deadline 起点：

```text
有 effectiveAtServerMs：effectiveAtServerMs + 8000
无 effectiveAtServerMs：本次 deliveryCreatedAtServerMs + 8000
```

不得使用旧 Snapshot.serverUpdatedAtMs 建立新 delivery deadline。

---

## 十二、测试计划

### 12.1 validator 和 metadata

- 2.8 request/output 闭合字段；
- 固定 10 capability；
- serverTimeMs 必需；
- server-only action 客户端请求返回 not_supported；
- schemaHash 缺失、变化、空值、类型或格式异常均不影响注册；
- serverBuildCommit unknown 可用；
- strict 不回退 legacy。

### 12.2 store 和数据库

- start/stop/feedback/terminal 原子事务；
- intent、delivery、feedback dedupe；
- fence 唯一约束和 recovery slot；
- 512/1024/256 上限；
- 7 天压缩成功和中途失败回滚；
- 三数据库 base schema/model/migration parity；
- restart 加载和 active/waiting terminal。

### 12.3 Socket 和角色

- source start 不操作音频；
- ordinary start 执行 mirror；
- controller-only 只观察；
- source 与 owner/participant 重叠时不重复投递；
- 每个 recipient 使用自己的 nonce；
- offline/能力/clock/recovery slot 筛选；
- 20 participant 显式拒绝和隐式截取。

### 12.4 source 派生与 cursor

- start 只接受 playing/fresh/settled source；
- source controls 共享 Context transaction；
- source remote equal result 不重复 revision；
- localUser、passive、failed correction 的 action 映射；
- 自然切歌 Context-first；
- source 变 idle 原子 terminal；
- progress 合并未发送不加 revision，发送严格 +1；
- status immutable anchor 和 future anchor max(0) 投影。

### 12.5 feedback 和 deadline

- applied/failed 条件字段；
- source/controller forbidden；
- track/index/state/rate/delivery/revision 校验；
- position 不按网络时间精确比对；
- lagging、failed、timedOut、后续 applied 收敛；
- earliest-unconfirmed 不被新 progress 延期；
- deadline 字段关闭后保留；
- same clientSeq replay/conflict；
- expired/unknown/ahead rejection 和后续新 delivery。

### 12.6 reconnect、terminal 和恢复

- ordinary active playing/paused/stopped resync；
- waiting resync 无时间字段；
- 旧 delivery feedback 无效；
- source waiting/resume actual state；
- 不同 deviceSession 不继承；
- 30 秒 timeout；
- source stop 后继续当前 transport；
- ordinary 原任务 playing/paused/stopped/idle 四种恢复；
- stopped 原任务保持 stopped；
- terminal feedback state 与 Snapshot.state 分域；
- full stop replay、compact restore、status recovery one-of；
- restorePending ensure 和 terminal drain capability exception。

### 12.7 并发和失败注入

- 两个 start 同时竞争同一 source Context；
- 同一 ordinary pair 被两个 Broadcast 竞争；
- stop 与 feedback 并发；
- stop 与 source queue idle 并发；
- resync 与旧 feedback 并发；
- DB commit 失败、outbox 写失败、emit 失败；
- compaction 创建部分 recovery 后失败；
- restart 与 pending deadline/timeout 并发。

### 12.8 验证命令

先运行最窄测试：

```bash
python -m unittest \
  tests.base.test_emo_strict_v2_contract \
  tests.base.test_emo_protocol_metadata \
  tests.base.test_emo_strict_v2_readiness \
  tests.base.test_emo_ws_store \
  tests.base.test_emo_strict_v2_core \
  tests.base.test_emo_strict_v2_broadcast \
  tests.base.test_emo_strict_v2_safety
```

再运行：

```bash
python -m unittest
node --test tests/js/emo_strict_v2_client.test.js
git diff --check
```

数据库 migration 影响无法由默认 SQLite 完全覆盖时，必须额外运行仓库现有 DB layer/schema contract
测试，并检查 MySQL/PostgreSQL SQL 语法和索引名。

### 12.9 真实双客户端验收

自动化全部通过后，至少使用：

- 一个 source player；
- 一个 ordinary player；
- 可选 controller-only；

验证 start、play/pause/seek、自然切歌、ordinary 断线重连、source 断线恢复、stop 和 ordinary 原任务
恢复。真实日志必须能够按 playbackContextId/broadcastId/revision/deliveryId/clientSeq 还原状态转换，
且不得记录密码、token 或完整敏感 payload。

---

## 十三、建议提交拆分

每个提交保持测试可解释，不混入无关重构：

1. `Document strict-v2 2.8 r18 server adaptation`：权威契约和 Goal；
2. `Update strict-v2 2.8 core schemas`：metadata、validator、clock/sample/rate；
3. `Persist strict-v2 broadcast lifecycle`：models、schema、migration、store primitives；
4. `Derive broadcast from source context`：start、roles、controls、source updates；
5. `Enforce broadcast context fences`：ordinary/source/restorePending guards；
6. `Add broadcast feedback and delivery tracking`：feedback、deadline、status、rejection；
7. `Recover broadcast participants across reconnects`：resync、waiting/resume、terminal replay；
8. `Compact terminal broadcast recovery`：7 天 retention、restore、restart sweeper；
9. `Complete strict-v2 r18 conformance`：fixtures、Web client、acceptance、readiness cleanup。

每次提交前执行受影响的最窄测试和 `git diff --check`；最后一次执行完整测试。实施请求明确授权时，
完成后按本项目工作流提交并推送，无需增加额外 release ceremony。

---

## 十四、完成定义

只有同时满足以下条件，才允许把 Broadcast implementation readiness 标记为 true：

- [ ] `specs/` 已成为修正后的 2.8.0/r18 唯一权威；
- [ ] schemaHash 缺失、变化、格式异常均不限制连接或 capability；
- [ ] 2.8 Core schema、mandatory pong、position sample 和 playbackRate 已完成；
- [ ] Broadcast 不创建第二套 queue/Context/cursor；
- [ ] start 只从 fresh、settled、playing source 派生；
- [ ] source/ordinary/controller-only 三角色闭合；
- [ ] source Context control 与 Broadcast revision 在同一事务；
- [ ] ordinary suspended Context/binding 屏障覆盖全部 mutation；
- [ ] restorePending ensure 返回无副作用 restore_in_progress；
- [ ] stop 原子释放屏障、安装 restorePending 和 terminal outbox；
- [ ] source stop 后没有额外 transport command；
- [ ] ordinary playing/paused/stopped/idle 原任务恢复正确；
- [ ] feedback applied/failed、deadline、status 和 clientSeq 闭合；
- [ ] deliveryId/resync/rejection 后新 delivery 闭合；
- [ ] waiting/resume/timeout 和不同 deviceSession 规则闭合；
- [ ] terminal full replay、7 天 compact recovery 和 restore 闭合；
- [ ] restart 把 active/waiting 原子转为 terminal；
- [ ] 20/256/512/1024 上限和 retention 清理闭合；
- [ ] SQLite/MySQL/PostgreSQL migration/schema parity 通过；
- [ ] strict 自动化、完整 unittest、JS 测试全部通过；
- [ ] 真实 source + ordinary 双客户端联调通过；
- [ ] 日志可追踪状态机且不泄露凭据；
- [ ] `supportsBroadcast:false` 到 true 的切换只发生在上述条件全部完成之后。

在此之前，服务端即使已经存在部分新 handler，也必须继续协商：

```json
{"supportsBroadcast": false}
```

已有 `restorePending` 或 TerminalRecoveryRecord 的 terminal drain 是唯一例外：即使当前 capability 为
false，也必须继续向精确冻结 pair 补发 stop/restore，直到恢复确认。

---

## 十五、已知风险和处理

### 15.1 DB 状态和 Socket 发送无法成为一个物理事务

网络发送不能与 SQLite/MySQL/PostgreSQL commit 组成同一原子操作。处理方式是先提交 immutable
Snapshot、delivery/outbox 和幂等结果，再发送；失败由同 delivery replay 或新物理连接 resync/terminal
replay 收敛。不得通过“发送成功后才写库”规避该问题。

### 15.2 当前 store primitive 会自行提交

现有 Context control、queue sync 和 playback update store 函数各自拥有 transaction。r18 要求 source
Context 与 Broadcast projection 同成同败，因此必须重构 store 层事务边界，不能在 handler 中串联两个
已提交操作伪装成原子。

### 15.3 多数据库并发能力不同

SQLite 主要依靠单 worker、进程锁和数据库 transaction；MySQL/PostgreSQL 可使用行锁。实现不得依赖
某个 provider 独有的 partial unique index。跨 provider 唯一占用优先使用显式 fence resource_key。

### 15.4 retention 数据量

progress 最多每秒一个 revision，但 7 天 full terminal 和 10 分钟 ledger 仍会产生数据。清理任务必须
按生命周期和时间索引批量处理，且先保证恢复记录再删除 full data。不能为了控制数据量提前删除仍可
反馈的 delivery/revision。

### 15.5 旧 Web strict client

仓库内 Web strict client 当前会拒绝非法 schemaHash，并仍理解旧 Broadcast shape。服务端宣告 2.8.0
前必须同步更新其 metadata 处理和 fixtures；否则本仓库自己的客户端会拒绝本仓库自己的服务端。

### 15.6 capability 过早开放

最大的产品风险是部分 handler 完成后提前返回 `supportsBroadcast:true`。实现过程中必须让内部 Broadcast
readiness 默认为 false；只有第十四节全部完成才可改变实现 readiness，部署配置仍保持显式控制。
