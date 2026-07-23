# Goal: EmoSonic strict-v2 Socket.IO r5 服务端适配

> 状态：Historical（已由 r7 / `2.2.0` Goal 取代，不再作为 wire 权威）
>
> 制定日期：2026-07-12
>
> 目标协议：PlaybackContext strict-v2 `2.1.0`
>
> 唯一 wire contract：`specs/emosonic_strict_v2_socketio_server_contract.md`
>
> 冻结 contract SHA-256：`ca069c6ad52447ea4f7ace7d795460c5ec759e5708b2f45acfbe50903aa4b3a3`
>
> 实施边界：本 Goal 负责服务端适配和服务端验证，不授予 production rollout 权限。

## 一、Goal 结论

将当前 EmoSonic Socket.IO 实现收敛为符合 r5 冻结候选契约的 strict-v2 服务端，并保持
legacy 客户端兼容路径可用。

最终必须形成四个独立 readiness profile：

1. Core：登录、注册、设备列表、heartbeat、PlaybackContext、Queue、Player Control；
2. Follow；
3. Handoff；
4. Broadcast。

Core 完整通过前，服务端不得接受 `playbackContextV2:true` 并返回 strict metadata。三个可选
profile 默认关闭，各自完成全部 schema、权限、状态机、清理、重启和 conformance 验证后，才可在
`negotiatedCapabilities` 中返回 `true`。

本 Goal 不是在现有 handler 上继续补零散字段。实施重点是建立统一 strict 请求入口、确定性状态
转换、持久化幂等边界、按收件人构造的输出工厂，以及可复现的 conformance 测试。

---

## 二、协议权威性与冲突处理

实现期间按以下优先级判断行为：

1. `specs/emosonic_strict_v2_socketio_server_contract.md`；
2. 本 Goal；
3. 当前实现和测试；
4. 历史设计文档、变更说明和旧 Goal。

若低优先级材料与 r5 冲突，必须修改低优先级材料或实现，不得反向解释契约。

特别说明：

- `docs/goal/follow_play.md` 是 legacy `sessionId` Follow 方案，不是 strict-v2 Follow contract；
- `docs/goal/broadcast.md` 使用旧 `targetMode`、`targetClientIds` 和旧 BroadcastState，不是 r5
  Broadcast contract；
- `ref/playback_context_v2_handoff_stabilization_goal.md` 中新增的 wire 字段、状态名或错误码，不能
  进入 strict-v2 `2.x` envelope；
- `docs/emosonic_strict_v2_server_change_note.md` 和 `ref/` 下的说明只能记录实现状态，不能定义新
  wire shape；
- `player.setVolume`、`player.requestState`、`session.subscribe`、`queue.session.sync`、
  `queue.local.set`、`queue.ready.complete` 不得进入 strict allowlist。

---

## 三、当前仓库基线

### 3.1 已有基础

当前仓库已经具备：

- Socket.IO namespace `/emo`、Engine.IO path `/emo/ws`、事件 `message`；
- Flask-SocketIO 连接和测试客户端；
- `supysonic/emo/ws.py` 中的 action routing 和发送逻辑；
- `supysonic/emo/ws_state.py` 中的连接、Context、Follow、Handoff、Broadcast 内存状态；
- `supysonic/emo/ws_store.py` 中的 Context、DeviceState、Handoff 持久化；
- `supysonic/emo/protocol_metadata.py` 和注册描述符；
- 每个物理连接使用 CSPRNG 生成 `connectionNonce`；
- strict recipient provenance 的统一发送 helper 雏形；
- Context、Handoff、Broadcast 和注册 metadata 的既有单元测试。

### 3.2 当前关键缺口

当前实现仍存在以下结构性差距：

1. 缺少 strict action 级闭合 schema 和统一 envelope validator；
2. 非法 `requestId` / `action` 未按契约立即断开；
3. 缺少 `(connectionNonce, requestId)` 结果缓存和 event-confirmed 重放；
4. 注册未返回完整 `negotiatedCapabilities`，也没有部署 readiness 求交集；
5. strict roles 被错误限制为必须同时包含 player 和 controller；
6. `device.list` 仍可能输出 userName、连接时间等内部字段；
7. Context create、queue、control、close 的 cursor 语义与 r5 矩阵不一致；
8. `playback.update` 仍可能修改 Context 并返回 ACK；
9. authority 路由没有完整持久化和校验 `authorityDeviceSessionId`；
10. create 缺少持久化 creation fingerprint，close 缺少完整 tombstone 行为；
11. Follow、Handoff、Broadcast handler 仍使用旧 payload、状态名或结算方式；
12. CORS、payload 上限、限流、背压、单 worker 和 graceful restart 约束未完整落地。

### 3.3 当前测试基线

制定本 Goal 时的只读验证结果：

此基线仅记录制定时状态；开始 Goal 0 时必须重新采集，后续以最新结果为准。

```text
python -m unittest \
  tests.base.test_emo_registration_descriptor \
  tests.base.test_emo_protocol_metadata \
  tests.base.test_emo_ws_state \
  tests.base.test_emo_ws_store

结果：68 tests，2 failures
```

```text
python -m unittest tests.base.test_emo_ws

结果：138 tests，38 errors，2 failures
```

失败主要来自当前工作树处于协议转换中间态：部分实现已改为 direct response，既有测试仍等待旧
ACK；注册描述符、测试 fixture 和实际 ACK shape 也尚未一致。实施时必须先把这些失败分类为：

- r5 新预期；
- legacy 回归；
- 当前未提交改动造成的临时不一致；
- 真正的实现缺陷。

不能通过简单删除旧断言恢复绿色，必须为 legacy 和 strict 分别保留明确测试。

---

## 四、范围

### 4.1 本 Goal 包含

- strict-v2 transport 和 envelope 校验；
- auth、register、device list 和 heartbeat；
- ACK、error、direct response 和 event-confirmed 结算；
- request fingerprint、短期去重和逻辑 ID 长期幂等；
- PlaybackContext create/status/subscribe/unsubscribe/close；
- queue.context.sync、queue.playItem 和 player controls；
- device playback feedback；
- Context、closed tombstone、Handoff 和 Broadcast 所需持久化及 migration；
- authority client/device 双重绑定；
- Follow、Handoff、Broadcast 三个独立可选 profile；
- 连接/用户/action 限流、Origin、payload 上限、背压和单 worker 限制；
- restart reconciliation 和 graceful shutdown；
- 注册描述符、schemaHash、配置说明和联调文档；
- 单元、集成、migration、restart 和双客户端 conformance 测试。

### 4.2 本 Goal 不包含

- 修改 r5 wire contract；
- 新增 r5 未定义的 request/push 字段、action 或错误码；
- multi-worker realtime；
- Redis/broker/sticky session 集群方案；
- 删除 legacy Socket.IO surface；
- 修改 Flutter 客户端实现；
- 自动 production rollout；
- `player.setVolume` 或 `player.requestState` strict 版本；
- 通过 legacy session fallback 修复 strict 请求。

---

## 五、必须保持的硬约束

### 5.1 请求与结算

- 每个 strict request 必须有合法、非空且未超限的 `requestId` 和 `action`；
- 无法形成 correlated error 时记录脱敏错误并断开；
- 每个 action 只能使用契约结算矩阵指定的一种成功结算方式；
- ACK/error 的 `payload.action` 必须精确回显原 action；
- 业务 push 不得复用客户端 requestId；
- event/state-confirmed action 不得补 ACK；
- 同一 requestId、同一 fingerprint 只重放，不重复副作用；
- 同一 requestId、不同 fingerprint 返回 `conflict`。

### 5.2 strict 与 legacy 隔离

- strict payload 的任意嵌套层级不得出现 `sessionId`；
- strict client request 顶层禁止 `targetClientId`；
- payload target 唯一例外是 `playback.handoff.start.payload.targetClientId`；
- strict server push/direct response 的顶层和 payload 均不得有 `targetClientId`；
- strict 校验失败不得回退到 legacy handler；
- legacy serializer 可以继续输出 legacy 字段，但不得复用 strict serializer。

### 5.3 provenance

- 每个新物理 Socket 生成至少 128 bit 熵的随机 nonce；
- `connectionEpoch` 固定为整数 `1`；
- strict 注册完成后的每条入站 envelope 逐 sid 注入该收件人的 nonce/epoch；
- 多收件人 fanout 必须逐 sid 构造，不得复用另一收件人的 provenance；
- bootstrap ACK/error 只使用契约允许的 provenance 例外。

### 5.4 Context 和 cursor

- `playbackContextId` 是唯一 strict 播放任务主键；
- create 初始化 `epoch/version/queueRevision/controlVersion` 为 `1`；
- `epoch` 只在 authority 原子切换时递增；
- `playback.update` 不修改 Context cursor；
- queue 和 control mutation 只按 r5 cursor 矩阵递增；
- base cursor 必须精确匹配 canonical cursor；
- close 形成持久化 terminal tombstone，ID 永不复用；
- authority 路由同时匹配 user、clientId、deviceSessionId 和当前 sid。

### 5.5 profile readiness

- Core 缺任一 request/response/cursor/routing/dedupe/error 能力时整体未 ready；
- Handoff、Follow、Broadcast 分别计算 readiness；
- 可选 profile 不允许“有部分 handler就协商为 true”；
- 每个 profile 只有在 `code_conformance_ready && deployment_enabled &&
  client_capability` 成立且角色依赖满足时，才协商为 true；
- `code_conformance_ready` 的唯一运行时来源是随包发布、纳入版本控制的
  `supysonic/emo/strict_v2_conformance.json`；文件缺失、格式非法或 contract SHA-256 不匹配时，
  四个 profile 一律视为 false；
- manifest 中某个 profile 只能在对应 conformance freeze 门禁通过并保存证据后改为 true；部署
  配置不得覆盖该值；
- 后续授权只读取 negotiated 值，不重新信任原始请求值。

---

## 六、建议的 strict 处理架构

strict 请求应经过唯一入口，顺序固定为：

```text
Engine.IO transport limit
  -> JSON object / envelope 基础校验
  -> requestId/action 可关联性校验
  -> 登录与注册阶段检查
  -> strict/legacy 路径确定
  -> strict action allowlist + type + closed schema 校验
  -> request fingerprint / dedupe lookup
  -> user、role、capability、membership、cursor 校验
  -> 解析全部收件人
  -> per-resource 串行化 + 数据库原子提交
  -> 保存 settlement / confirmation
  -> 按 action-specific settlement/order 投递 correlated response 与 canonical push
```

不存在统一的 response-first 顺序：server-routed control 必须先解析 authority 并向其单播
control，再向请求者发送 ACK；handoff start 的 ACK 与发往另一 socket 的 prepare 不承诺
跨 socket 顺序。其他 action 也必须遵循各自的结算和投递顺序。

实现时应避免继续扩大单个 `ws.py`。允许新增小型模块，建议职责如下：

| 模块 | 职责 |
| --- | --- |
| `supysonic/emo/ws.py` | Socket.IO 生命周期、legacy router、调用 strict dispatcher |
| `supysonic/emo/strict_v2_contract.py` | action allowlist、字段/type/limit 校验、输出 schema 校验 |
| `supysonic/emo/strict_v2_runtime.py` | request cache、fingerprint、rate limit、profile readiness |
| `supysonic/emo/strict_v2_conformance.json` | contract hash 和四个 profile 的代码 conformance 状态 |
| `supysonic/emo/ws_state.py` | sid/client/subscription 和进程内串行化状态 |
| `supysonic/emo/ws_store.py` | canonical DB 读取、CAS/事务 mutation、restart reconciliation |
| `supysonic/emo/protocol_metadata.py` | 注册描述符、schemaHash、build commit |

模块名可以在实现时按仓库风格微调，但 validator、runtime policy、持久化 mutation 不应继续混在
一个 action handler 中。

---

## 七、实施 Goal

## Goal 0：冻结基线和 conformance 骨架

### 工作项

1. 保存并审阅当前未提交 strict-v2 diff，不覆盖用户已有改动；
2. 在任何业务实现前，将本 Goal 和唯一 wire contract 纳入版本控制，以独立基线提交固定内容；
3. 校验 contract SHA-256 与本文冻结值一致，并在 conformance fixture 和证据中记录该值；
4. 新增随包发布的 `supysonic/emo/strict_v2_conformance.json`，记录 contract SHA-256 以及 Core、
   Follow、Handoff、Broadcast 的 `code_conformance_ready`；所有值初始为 false；
5. 建立 r5 action 清单，记录每个 action 的：
   - request type；
   - request payload schema；
   - 成功结算方式；
   - error 条件字段；
   - server push schema；
   - role/capability gate；
   - cursor mutation；
6. 将现有测试按 legacy、strict Core、Follow、Handoff、Broadcast 分类；
7. 增加服务端自有 strict-v2 fixture/manifest，fixture 内容逐条来自 normative spec；
8. 为每个 EARS REQ-001 至 REQ-022 建立测试映射表。

### 完成门槛

- 每个 strict action 都能映射到一个 validator 和一个唯一 settlement；
- 本 Goal、contract 和初始 conformance manifest 已被 Git 跟踪，contract 内容与冻结 SHA-256
  一致；
- 初始 conformance manifest 的四个 profile 均为 false，且已验证缺失、非法或 hash 不匹配时
  fail-closed；
- 所有现有失败都有明确归属；
- 没有测试继续把旧 Goal 当成 strict contract；
- 尚未改业务行为时，legacy 基线测试可单独运行并报告结果。

---

## Goal 1：strict envelope、错误与去重入口

### 工作项

1. 配置 Engine.IO message 上限不高于 256 KiB；
2. 对非 object、malformed 和 transport oversize 使用契约要求的关闭行为；
3. 校验 envelope 必需字段、字段方向、type/action 组合和顶层未知字段；
4. 校验 ID/action UTF-8 byte length、字符串 trim、数组去重和业务数量限制；
5. 对每个 action 使用闭合 payload schema，未知字段返回 `bad_request`；
6. 建立固定错误码映射，不再输出 `stale_client_seq`、`follow_control_forbidden` 等旧 strict code；
7. 统一 ACK/error/direct response builder；
8. 建立至少 60 秒的 `(connectionNonce, requestId)` cache；
9. fingerprint 覆盖 action、type 和规范化 payload；
10. 断开时清理该 nonce 的 request cache；
11. 为 `playback.update`、`playback.ready`、`playback.handoff.complete` 实现无 requestId 的
    canonical confirmation 重放；
12. 捕获未预期异常，返回脱敏 `internal_error`，同时保留服务端异常日志。

### 完成门槛

- REQ-001、REQ-007、REQ-011、REQ-012、REQ-014、REQ-020、REQ-022 通过；
- 同一 request 重复 100 次只产生一次 mutation；
- requestId 内容冲突不执行第二次副作用；
- 非法 correlation 字段不会收到服务端伪造 requestId；
- error payload 不含未定义字段或 `null`。

---

## Goal 2：认证、注册、设备和 heartbeat

### 工作项

1. `auth.login` ACK 返回 action、`authenticated:true` 和认证后的 userName；
2. 登录失败使用 bootstrap correlated error，日志不记录密码或完整 payload；
3. heartbeat 只允许完成 device.register 后使用；
4. strict register 允许 player、controller 或两者，固定输出角色顺序；
5. 校验完整 9 个客户端 capability bool 和 capability 组合；
6. 分别记录 Core、Follow、Handoff、Broadcast 的 code conformance readiness 和部署开关；
7. 将 code conformance、部署开关、客户端 capability 和角色依赖求交集；
8. 保存原始 capability 与 negotiated capability 时使用不同内部字段；
9. 后续 strict 授权只使用 negotiated capability；
10. register ACK 返回完整 `negotiatedCapabilities` 和 strictV2 metadata；
11. 更新注册描述符并重新计算 schemaHash；
12. `device.list` 只输出契约字段、稳定排序和 negotiated capabilities；
13. 连接映射按 authenticated user + clientId 隔离；
14. 同用户相同 clientId 新注册时原子替换并主动断开旧 sid；
15. 不同 deviceSessionId 的新连接不得继承旧 authority。

### 配置建议

新增明确的 readiness 配置，名称可在实现时统一，例如：

```ini
emo_strict_v2_core_enabled = off
emo_strict_v2_follow_enabled = off
emo_strict_v2_handoff_enabled = off
emo_strict_v2_broadcast_enabled = off
```

这些配置表示 `deployment_enabled`，默认关闭；运维可以在部署审批后开启或再次关闭
profile。最终协商值必须同时满足 `code_conformance_ready && deployment_enabled &&
client_capability` 以及角色依赖。配置不得修改或绕过 `code_conformance_ready`，因此代码或
conformance 未完成时，即使配置为 on 也不得协商为 true。

### 完成门槛

- register request、ACK、error 均通过 descriptor；
- controller-only 和 player-only 注册成功；
- 非法 capability 依赖返回 `bad_request`；
- Core 未 ready 返回 `not_supported`，不进入 legacy；
- optional 未 ready 时注册成功但 negotiated 值为 false；
- `device.list` 不泄露 userName、时间戳、legacy session 或内部状态；
- 旧 sid 被实际断开，不能继续提交 mutation。

---

## Goal 3：PlaybackContext 数据模型、持久化和重启

### 数据模型要求

`EmoPlaybackContext` 至少需要持久保存：

- playbackContextId 和 user；
- authorityClientId；
- authorityDeviceSessionId；
- canonical queue/current index/track/state/position；
- epoch/version/queueRevision/controlVersion；
- creation fingerprint 或等价初始意图证据；
- active/closed terminal 标记；
- closed 时间或内部审计信息；
- timelineId（若启用）；
- canonical 更新时间。

active/closed 可以使用内部字段表示，但内部 lifecycle 字段不得未经 serializer 明示进入 strict
wire snapshot。播放 `state` 对外始终只允许 `playing|paused|stopped`。

### 工作项

1. 为 SQLite、PostgreSQL、MySQL 同步更新 base schema 和 migration；
2. create 在同一事务中建立 Context、creation fingerprint 和 authority identity；
3. creation fingerprint 使用初始 user、authority client/device、queue、index、position、state；
4. 相同 ID、相同初始意图返回原完整 snapshot；
5. 相同 ID、不同意图返回带当前 cursor 的 `conflict`；
6. closed ID 的 create/status/mutation 返回 `context_closed`；
7. create 初始化四个 cursor 为 1，并自动订阅当前 sid；
8. close 原子写 tombstone，version +1，其他 cursor 按契约保持；
9. close 先向当前 recipients 推 closed，再清理临时 subscription/Follow；
10. Context mutation 使用 per-context 串行化边界和 DB transaction/CAS；
11. 服务启动恢复 active Context 和 tombstone，保留全部 cursor；
12. 启动时不恢复 sid、nonce、request cache 或 subscription；
13. 清除 Follow，失败未终态 Handoff，停止 active Broadcast；
14. migration 对历史 0 cursor 采用确定性升级策略，避免升级后首个请求无故 stale；
15. serializer 使用字段 allowlist，不再从内部 dict 删除少数字段后整体输出；
16. create 只允许 player，且 deviceSessionId 必须匹配当前连接；
17. subscribe/status 只允许同一 authenticated user，跨用户统一返回 `forbidden` 且不泄露资源
    是否存在；
18. close 只允许当前 authority 或同用户 controller，并按 contract 分别使用 direct response、
    ACK 和 canonical closed push 结算 create/status/subscribe/unsubscribe/close；
19. 新增统一的三数据库 migration 测试入口和 Docker Compose 测试环境，覆盖 clean install、从
    当前 `20260708` schema 升级、数据保留和 restart hydration。

### 完成门槛

- create、重试、冲突和 closed ID 行为可跨进程重启复现；
- authority client/device identity 重启后完整保留；
- queueSongIds 顺序经 DB round trip 和重启后不变；
- active snapshot 不输出 `state:"closed"`；
- SQLite、PostgreSQL、MySQL migration 均有验证证据；
- create/status/subscribe/unsubscribe/close 的 schema、权限、结算和跨用户错误测试全部通过；
- 故障注入证明 ACK/push 不会早于数据库 commit；commit 后 emit 失败时数据库仍保持 canonical，
  重连客户端可通过 subscribe/status 恢复，且不会回滚或重复 mutation。

数据库 commit 与 Socket emit 之间存在不可消除的进程崩溃窗口，r5 不承诺跨重启 exactly-once
delivery。本 Goal 不以“窗口不存在”为验收条件；若未来要求 durable business-message replay，必须
另立 transactional outbox 工程目标。

---

## Goal 4：Core Queue、Player Control 和 feedback

### Queue

1. `queue.context.sync` 只允许当前 authority 和匹配的 deviceSessionId；
2. queue 内容变化要求并校验 baseQueueRevision；
3. index、当前 track 或 position 的 canonical 值变化时要求 baseControlVersion；
4. 只按 r5 矩阵推进 version、queueRevision、controlVersion；
5. queue sync 不改变播放 state；
6. ACK payload 只有 action；
7. canonical queue push 不携带 request base cursor。

### Player Control

1. 请求者必须有 controller 角色；
2. authority 必须同用户、在线，并匹配 clientId + deviceSessionId + 当前 sid；
3. play/next/prev/playItem 校验 authority `canPlay`；
4. pause 校验 authority `canPause`；
5. seek 校验 authority `canSeek`；
6. 缺能力返回 `capability_required`，离线返回 `authority_offline`；
7. 在 mutation 前解析并固定唯一 authority sid；
8. mutation 提交后向 authority 单播无 target、无 requestId 的 canonical control；
9. ACK 只结算请求，不承担音频执行；
10. subscribers 只接收事实状态，不接收执行 control。

### Playback feedback

1. `playback.update` 必须带 clientSeq、state、position 和绑定的 deviceSessionId；
2. clientSeq scope 包含 context、client、connectionNonce、connectionEpoch；
3. 相同 seq/内容视为重复，不重复 mutation；
4. 相同 seq/不同内容或倒退返回 `client_sequence_conflict`；
5. feedback 只更新 DevicePlaybackState，不修改 Context snapshot/cursor；
6. 服务端不回 ACK；
7. canonical update 不含 queue/currentIndex/authority/cursor 字段；
8. status 中 DeviceState 使用严格字段 allowlist 并稳定排序。

### 完成门槛

- REQ-005、REQ-006、REQ-010、REQ-013、REQ-016、REQ-018 通过；
- 两个 controller 同时控制时只有一个请求能匹配旧 cursor；
- authority 重连但 deviceSessionId 不同，控制返回 authority_offline；
- next/prev/playItem 后 queueRevision 和 currentIndex 正确推进；
- playback.update 不改变四个 Context cursor；
- strict control push 不包含 requestId、base cursor 或 target。

Goal 0 至 Goal 4 全部通过只代表 Core 业务状态机 ready，尚不能将 strict Core 标记为
ready。

---

## Goal 5：运行安全和部署约束

### 工作项

1. production Origin 使用配置 allowlist，禁止默认 `*`；
2. 仅显式 development 模式允许 wildcard，并输出安全警告；
3. 文档明确公网必须使用 TLS；
4. 默认限制每 IP 同时 10 条未认证连接、每用户同时 20 条已认证连接；握手阶段超限直接拒绝；
5. 默认限制每连接每分钟 120 个 strict 请求、player control 每秒 20 个，create、handoff start、
   broadcast start 每分钟各 10 个；部署可调低，调高必须有负载测试证据；
6. `rate_limited` 返回正整数 retryAfterMs；
7. 配置 Engine.IO ping/pong、发送 buffer 和背压策略；
8. authority 单播无法可靠入队时不接受 mutation；
9. 当 strict Core ready 且检测到 Gunicorn/process workers > 1 时启动失败；
10. 保留线程并发，但所有同资源 mutation 经过资源锁；
11. graceful shutdown 停止接收新连接和新 profile mutation；
12. 完成或明确失败正在结算的请求，再关闭 Socket；
13. 日志只记录 requestId、action、认证身份、结果和延迟；
14. 错误和日志不暴露密码、原始登录 payload、路径、堆栈或数据库内容。

### 完成门槛

- 257 KiB transport message 被关闭且不进入 handler；
- 超长 ID、1001 首 queue、101 participants 返回 correlated bad_request；
- Origin allowlist 和开发 wildcard 均有测试；
- 使用可控时钟验证第 11 条未认证连接、第 21 条已认证连接、第 121 个每分钟请求、第 21 个每秒
  control，以及第 11 个每分钟 create/handoff/broadcast start 的边界行为；
- 限流不会先执行 mutation 再返回 rate_limited，所有 `retryAfterMs` 均为正整数；
- `--processes 2` 在 strict realtime ready 配置下 fail-fast；
- graceful restart 后 Context 保留，瞬态 profile 按契约终止。

### Core conformance freeze

将 conformance manifest 的 Core 标记为 true 前，必须同时满足：

1. Goal 0 至 Goal 5 的完成门槛全部通过；
2. `auth.login`、`device.register`、`device.list`、`system.ping`、全部
   `playback.context.*`、`queue.context.sync`、`queue.playItem`、全部允许的 `player.*` 和
   `playback.update` 均有 request、response/push、schema、权限、cursor 和重复请求测试；
3. REQ-001 至 REQ-007、REQ-010 至 REQ-014、REQ-016 至 REQ-020、REQ-022 的 Core 场景全部
   通过；
4. `python -m unittest tests.base.test_emo_strict_v2_core`、legacy Emo 测试和 Goal 5 的安全/部署
   测试均通过，命令、结果、contract SHA-256 和 serverBuildCommit 保存到验证证据；
5. conformance manifest 的 contract SHA-256 与冻结 contract 一致。

只有该 freeze 通过后，才可在受审阅的变更中将 Core `code_conformance_ready` 改为 true；
`deployment_enabled` 仍保持 off，不能在同一变更中自动 rollout。

---

## Goal 6A：Follow profile

### 工作项

1. 仅 negotiated `supportsFollow:true`、player、canPlay 的连接可发起；
2. ownership 绑定 user、clientId、deviceSessionId 和 sourcePlaybackContextId；
3. 同 source 重复 start 幂等 ACK；
4. 已 Follow 另一 source 时返回 conflict，不隐式切换；
5. start/stop ACK payload 只有 action；
6. start 不主动以未关联 status 代替结算；
7. 客户端随后显式请求 status；
8. disconnect 清除临时 relationship；
9. Context close 终止全部 Follow 并发送 closed；
10. Follow 不授予 control、handoff 或 broadcast 权限；
11. strict push 不出现 sourceSessionId/sessionId。

### 完成门槛

- Follow 全套 schema、权限、重复、断连、close 测试通过；
- profile 未 ready 时返回 capability_required；
- 通过后才允许 `supportsFollow:true` 出现在 negotiatedCapabilities。

---

## Goal 6B：Handoff profile

### 工作项

1. start 校验 controller、source authority、source canPause 和 target 全部能力；
2. target 绑定同用户 player、clientId、deviceSessionId 和 sid；
3. 每个 Context 同时最多一个非终态 Handoff；
4. 相同 source/target 重试返回同一 handoffId/prepareId；
5. start ACK 严格使用 r5 五个结果字段；
6. prepare 只发 target，使用严格字段 allowlist；
7. ready 为 event-confirmed，不回 ACK；
8. ready 后生成至少提前 250 ms 的 commit，并进入 committing；
9. complete 为唯一 authority 原子切换点，不回 ACK；
10. authority switch 在同一事务推进 epoch/version/controlVersion；
11. completed status、Context status、source release 按契约顺序发送；
12. cancel 幂等且使用 `cancelled`；
13. timeout 使用 `timedOut`；
14. 标准 errorCode 只使用契约值，扩展码校验正则；
15. target/source 断连使用正确 terminal transition；
16. restart 将非终态 Handoff 标为 failed/server_restart；
17. event-confirmed duplicate 只向当前请求 sid 重放 canonical confirmation。

### 完成门槛

- preparing/ready/committing 三个 timeout 和所有终态通过 fake clock 测试；
- complete/cancel/timeout 并发只有一个终态；
- authority、Context cursor、DeviceState 和 Handoff DB 状态原子一致；
- prepare/commit/release 都只投递规定收件人；
- profile 通过后才协商 playbackPrepare/effectiveAtPlayback。

---

## Goal 6C：Broadcast profile

### 工作项

1. strict start 改用可选 `participants`，移除旧 targetMode/targetClientIds；
2. 当前 Context authority 强制加入 participants；
3. participant 必须满足同用户、在线 player 和四项执行能力；
4. owner/controller 与 participant 权限按 r5 固定规则实现；
5. start ACK 只返回 action、started、broadcastId、participants、skippedClientIds；
6. participant/skipped 集合稳定排序，queue 保持原顺序；
7. 所有 push 使用完整且闭合的 BroadcastSnapshot；
8. status 使用 correlated ACK，不再额外发送 direct status response；
9. participantStates 覆盖最终 participants；
10. 无 feedback 时省略 clientSeq/serverUpdatedAtMs；
11. 有 feedback 后两字段成对出现；
12. mutation 串行化并按 r5 矩阵推进三个 Broadcast cursor；
13. queue.sync 条件性要求两个 base cursor；
14. stop 不可逆，重复 stop 幂等；
15. owner 断开不自动停止；
16. authority 断开立即 paused，30 秒后 terminal stop；
17. authority 以相同 client/device 重连时取消 timer，但不自动恢复播放；
18. restart 将 active Broadcast terminal stop 并冻结 cursor；
19. 多 participant push 逐 sid 注入各自 provenance。

### 完成门槛

- start/status/play/pause/seek/playItem/queue.sync/stop 全套 schema 通过；
- 0 feedback、部分 feedback、全部 feedback 三种 status shape 通过；
- authority 强制 participant、跨用户、能力不足和全 skipped 场景通过；
- disconnect 30 秒策略使用可控时钟测试；
- profile 通过后才协商 supportsBroadcast。

### Optional profile conformance freeze

Follow、Handoff、Broadcast 分别 freeze，互不继承。将任一 profile 的
`code_conformance_ready` 改为 true 前，必须：

1. 运行该 profile 的完整 schema、权限、状态机、幂等、断连、重启、并发和双客户端测试；
2. 运行对应固定入口 `tests.base.test_emo_strict_v2_follow`、
   `tests.base.test_emo_strict_v2_handoff` 或 `tests.base.test_emo_strict_v2_broadcast`；
3. 将命令、结果、contract SHA-256、serverBuildCommit 和双客户端 build ID 保存到 verification
   目录；
4. 确认 manifest contract SHA-256 与冻结 contract 一致，并以独立受审阅变更只切换该 profile
   的 code conformance 状态；
5. 保持对应 `deployment_enabled` 为 off。一个 profile freeze 不得改变其他 profile。

---

## Goal 7：描述符、文档、联调和冻结验收

### 工作项

1. 更新 strict registration descriptor 的 required、enum、pattern、additionalProperties；
2. descriptor 覆盖完整 negotiatedCapabilities 和 strictV2 metadata；
3. 重新计算并测试 schemaHash；
4. 验证 wheel 和 sdist 同时包含 registration descriptor 与 `strict_v2_conformance.json`，安装后
   runtime 能读取并校验 contract SHA-256；
5. 更新配置样例、部署文档和服务端联调说明；
6. 历史 Goal 增加醒目的 superseded/legacy 标记，不删除历史证据；
7. 运行完整 unittest 和 coverage；
8. 按第 8.3 节的统一入口运行 SQLite、PostgreSQL、MySQL migration 验证；
9. 运行 transport、restart、duplicate、race、双客户端 conformance；
10. 由联调负责人使用固定 build ID 的 Android 与 Windows 客户端完成 probe、reconnect、device
   list、Core 控制，并记录负责人、客户端 build ID、服务端 commit、时间和逐项结果；
11. 可选 profile 分别联调，不因 Core ready 自动开放；
12. 将 protocolVersion、schemaHash、contract SHA-256、serverBuildCommit、自动化命令输出和人工
    联调记录保存到 `docs/verification/emosonic_strict_v2_r5/<serverBuildCommit>/`；
13. 由人工审阅 conformance 证据并确认 release candidate；交付时所有
    `emo_strict_v2_*_enabled` 仍保持 off，本 Goal 不审批或执行 rollout。

### 完成门槛

- REQ-001 至 REQ-022 均有自动化测试证据；
- `python -m unittest` 通过；
- `python -m unittest tests.net.suite` 的运行结果已记录；
- coverage 命令结果已记录；
- descriptor 与真实 ACK fixture 双向一致；
- wheel 和 sdist 均包含 conformance manifest，安装产物的缺失/非法/hash mismatch 路径全部
  fail-closed；
- 双客户端真实联调通过，且证据包含负责人、双方 build ID、服务端 commit 和逐项结果；
- 没有 optional profile 在未通过自身门禁时返回 true；
- verification 目录内容完整且可关联到唯一 contract SHA-256 和 serverBuildCommit；
- production rollout 明确不属于本 Goal，完成本 Goal 不改变任何生产部署开关。

---

## 八、数据库和 migration 计划

### 8.1 预计影响

| 文件 | 计划内容 |
| --- | --- |
| `supysonic/db_layer/emo.py` | Context authority device、creation、terminal 字段；必要的 profile 持久化模型 |
| `supysonic/emo/ws_store.py` | 原子 Context/Handoff/Broadcast mutation 和 restart reconciliation |
| `supysonic/schema/sqlite.sql` | 新安装 schema |
| `supysonic/schema/postgres.sql` | 新安装 schema |
| `supysonic/schema/mysql.sql` | 新安装 schema |
| `supysonic/schema/migration/*/<version>.*` | 三种数据库升级脚本 |
| `supysonic/db_layer/schema.py` | schema version |

### 8.2 迁移原则

- migration 必须可重复验证，不能依赖实时在线设备；
- 历史 active Context 保留 queue 和 cursor，不得把 queue 排序；
- 缺 authorityDeviceSessionId 的旧 Context不得猜测另一在线设备；
- 无法证明 authority device identity 时保留逻辑 authority，但路由按 offline 处理；
- 历史 closed/expired 状态迁移成 terminal tombstone；
- 历史 0 cursor 统一提升到至少 1，并记录迁移策略；
- 非终态旧 Handoff 在升级/restart reconciliation 中显式失败；
- active 旧 Broadcast 显式停止；
- migration 不创建跨用户 device/client 绑定；
- 先在复制数据库验证，再进入真实部署。

### 8.3 可复现验证环境

- 唯一测试入口为 `python -m unittest tests.base.test_emo_schema_migration`；SQLite 使用临时文件，
  PostgreSQL 和 MySQL 分别读取 `SUPYSONIC_TEST_POSTGRES_URI` 与
  `SUPYSONIC_TEST_MYSQL_URI`；
- 新增 `tests/compose.emo_migrations.yml`，初始兼容矩阵为 PostgreSQL 16 和 MySQL 8.4；Compose
  必须固定 patch tag 或 image digest，并将实际 image digest 写入验证证据；Compose 的本地默认
  URI 固定为 `postgresql://supysonic:supysonic@127.0.0.1:55432/supysonic_test` 和
  `mysql://supysonic:supysonic@127.0.0.1:53306/supysonic_test`，环境变量可覆盖；
- 本地和 CI 使用相同流程：

```text
docker compose -f tests/compose.emo_migrations.yml up -d --wait
python -m unittest tests.base.test_emo_schema_migration
docker compose -f tests/compose.emo_migrations.yml down -v
```

- 测试必须分别验证全新 base schema，以及从冻结的 `20260708` fixture 升级到新版本；每次验证
  从全新数据库开始，不依赖开发机现有数据；
- `.github/workflows/tests.yaml` 增加独立 `database-migrations` job，运行同一测试入口并保存数据库
  版本、image digest、迁移前后 schema version 和测试结果；
- 真实数据库复制件演练属于部署证据，不得替代上述自动化 migration 测试。

---

## 九、测试矩阵

| 类别 | 必测场景 |
| --- | --- |
| Envelope | 缺字段、空 ID、超长 ID、未知字段、错误 type、顶层 target、嵌套 sessionId |
| Settlement | ACK、direct response、event-confirmed、error、重复 request、fingerprint 冲突 |
| Register | probe、negotiated reconnect、单角色、能力依赖、Core disabled、optional disabled |
| Provenance | 新连接 nonce、旧 sid 隔离、fanout 每 sid nonce、bootstrap 例外 |
| Context | create、相同意图重试、不同意图冲突、status、subscribe、unsubscribe、close、tombstone |
| Cursor | queue only、index change、position change、play/pause/seek、next/prev/playItem、handoff |
| Routing | controller A 请求、authority C 执行、B 只收状态、authority offline、device mismatch |
| Feedback | seq 递增、相同重复、内容冲突、重连从 1、非 authority feedback、status hydration |
| Follow | start/stop 幂等、已有另一 source、disconnect、Context close、无控制权 |
| Handoff | success、ready false、prepare timeout、commit timeout、source/target disconnect、restart |
| Broadcast | participant 筛选、全 skipped、cross-user、cursor、status 初始值、authority reconnect timer |
| Persistence | DB round trip、restart、queue 顺序、cursor 保留、closed ID 不复用 |
| Concurrency | 双 control、双 create、双 handoff start、complete/cancel、close/complete、timer/mutation |
| Security | Origin、TLS 文档、payload limit、连接限流、action 限流、日志脱敏、internal_error |
| Deployment | 单 worker、multi-worker fail-fast、graceful restart、build metadata |

并发测试不能只断言“不抛异常”。必须断言：

- 唯一 winner；
- loser 的固定错误码和当前 cursor；
- DB、内存和所有 push 的 canonical 值一致；
- 没有重复副作用；
- 重启后结果不改变。

---

## 十、建议提交拆分

为保持 review 范围清晰，建议按以下提交拆分，不把全部工作压成一个 commit：

1. 冻结并提交 contract、Goal 和四项均为 false 的初始 conformance manifest；
2. conformance fixtures、action matrix 和测试分类；
3. strict envelope validator、错误和 settlement factory；
4. request cache、fingerprint 和 event confirmation；
5. auth/register/device list/negotiated capabilities；
6. Context schema migration、tombstone 和 creation fingerprint；
7. Context cursor 和 queue mutation；
8. player control routing 和 playback feedback；
9. transport/security/single-worker/graceful restart；
10. Follow profile；
11. Handoff profile；
12. Broadcast profile；
13. descriptor/schemaHash、打包验证、文档和最终 conformance 证据。

每个提交必须：

- 保持 legacy 测试通过，或明确记录暂时失败及后续恢复提交；
- 运行对应最窄测试；
- 不混入无关重构；
- 不在 optional profile 未完成时提前开启 capability；
- conformance manifest 的 false→true 变更必须是对应 freeze 通过后的独立受审阅提交，且不得同时
  修改 deployment enabled 配置；
- 不修改 frozen wire contract 来迁就当前实现。

---

## 十一、风险与控制措施

### 风险 1：strict 和 legacy handler 继续交叉

控制：注册完成后固定协议模式；strict validator 成功后只进入 strict dispatcher；禁止基于单个
payload 字段再次猜测协议。

### 风险 2：`ws.py` 继续膨胀导致结算重复

控制：抽离 contract/runtime；handler 返回结构化 mutation/result，由统一 settlement layer 发送。

### 风险 3：内存先变、数据库后写造成状态分裂

控制：DB transaction/CAS 是 canonical mutation 点；提交后用返回行刷新内存，再发送消息。

### 风险 4：旧测试绿色但不符合 r5

控制：每个 EARS requirement 必须映射到新 conformance test；legacy test 不能替代 strict test。

### 风险 5：optional capability 被过早打开

控制：code conformance readiness 与部署开关双门禁；服务启动日志输出四个 profile 的两项
状态及最终 readiness。

### 风险 6：migration 破坏现有播放数据

控制：三数据库 migration 测试、复制库演练、升级前备份、只做可解释的确定性转换。

### 风险 7：当前未提交改动被覆盖

控制：实施前审阅 `git diff`；按现有工作树继续修改，不执行 reset/checkout，不回退用户文件。

---

## 十二、Definition of Done

只有同时满足以下条件，本 Goal 才算完成：

1. normative spec 未被实现反向修改；
2. Core 全部 action 的 request、response、push 都是闭合 schema；
3. 每个 request 只结算一次；
4. request dedupe 和 event-confirmed replay 完整；
5. auth/register/device/heartbeat 符合 bootstrap 和 provenance 规则；
6. negotiatedCapabilities 是 code conformance、部署开关、客户端能力和角色依赖的交集；
7. Context create fingerprint、authority device identity 和 closed tombstone 持久化；
8. cursor 只按 r5 矩阵推进；
9. authority control 只单播唯一当前 authority sid；
10. playback.update 不修改 Context，不返回 ACK；
11. strict payload 无 sessionId，业务 push 无 targetClientId；
12. transport limit、Origin、限流、背压和日志脱敏通过测试；
13. strict realtime 多 worker 配置 fail-fast；
14. restart reconciliation 符合 Context/Handoff/Follow/Broadcast 规则；
15. SQLite、PostgreSQL、MySQL schema 和 migration 同步；
16. Follow、Handoff、Broadcast 未 ready 时 negotiated 值均为 false；
17. optional profile 只有通过自身完整门禁后才可开启；
18. descriptor、schemaHash、conformance manifest、安装产物和真实 register ACK 一致；
19. 全量 unittest、coverage 和服务端 conformance 结果已记录；
20. Android + Windows 双客户端真实联调通过；
21. 文档明确区分“实现完成”“profile ready”和 Goal 外的 production rollout；
22. 交付构建中的四个 deployment enabled 配置默认保持 off；production rollout 不属于本 Goal
    的 Definition of Done。

---

## 十三、执行检查表

- [ ] Goal 0：冻结基线和 conformance 骨架
- [ ] Goal 1：strict envelope、错误与去重入口
- [ ] Goal 2：认证、注册、设备和 heartbeat
- [ ] Goal 3：PlaybackContext 数据模型、持久化和重启
- [ ] Goal 4：Core Queue、Player Control 和 feedback
- [ ] Goal 5：运行安全和部署约束
- [ ] Goal 6A：Follow profile
- [ ] Goal 6B：Handoff profile
- [ ] Goal 6C：Broadcast profile
- [ ] Goal 7：描述符、文档、联调和冻结验收
