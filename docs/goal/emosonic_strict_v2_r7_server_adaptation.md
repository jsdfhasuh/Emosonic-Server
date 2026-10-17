# Goal: EmoSonic strict-v2 Socket.IO r7 服务端适配

> 状态：In Progress（Goal 0 至 Goal 6 的服务端实现与自动化验证已完成；final conformance
> freeze、唯一 serverBuildCommit 证据和 Android/Windows 验收待完成）
>
> 制定日期：2026-07-15
>
> 目标协议：PlaybackContext strict-v2 `2.2.0` / contract r7
>
> 唯一 wire contract：`specs/emosonic_strict_v2_socketio_server_contract.md`
>
> 冻结 contract SHA-256：`7e5402a4c32fb366c3755239e4993ef5634177e7db9748bff83b32926cbd2b1f`
>
> 制定时基线 commit：`d448ab31b1eee7e2d2aa09d63d417b29dbe53be7`
>
> 实施边界：本 Goal 负责服务端适配、服务端 fixtures/conformance 和联调证据，不修改 Flutter
> 业务实现，也不授予 production rollout 权限。

## 一、Goal 结论

将当前已基本符合 r5 / `2.1.0` 的 strict-v2 服务端增量适配到 r7 / `2.2.0`，补齐以下 Core
闭环：

1. `playback.context.list`：按 authenticated user 和 authority client/device pair 精确发现 active
   PlaybackContext；
2. `playback.context.bindings.changed`：create、close、handoff authority switch 后向全部同用户
   strict controller 发送 binding 失效事件；
3. 多 Context fail-closed：同一 authority/device pair 存在多个 active Context 时，服务端不发送
   普通 player control，并返回带完整 canonical cursor 的 `conflict`；
4. pair-level 线性化：create、close、handoff authority switch 和普通 player control 在同一
   authority/device pair 上串行化；
5. bootstrap provenance：strict register ACK 前的合法 correlated error 可以且必须省略
   connection provenance；register 后仍强制 provenance；
6. 版本和 readiness：服务端只以 `protocolVersion=2.2.0` 声称实现本契约，Core 未完整通过前不得
   协商 `playbackContextV2:true`。

本 Goal 不重新设计 r5 已有 Context、Queue、Control、Follow、Handoff 或 Broadcast wire shape。
所有新增行为必须从 r7 契约派生；实现或历史 Goal 与 r7 冲突时，以 r7 为准。

---

## 二、协议权威性与范围

### 2.1 权威顺序

实现期间按以下优先级判断行为：

1. `specs/emosonic_strict_v2_socketio_server_contract.md` r7；
2. 本 Goal；
3. 当前实现和测试；
4. r5 Goal、旧变更说明和历史设计文档。

本 Goal 只拆解工作，不得新增 r7 未定义的 action、字段、错误码或 fallback。

### 2.2 本 Goal 包含

- strict request/output schema 增量；
- `2.2.0` 注册 metadata、fixtures 和 conformance inventory；
- authority/device binding 的持久化精确查询；
- authority/device pair 级锁和原子多 Context 检查；
- Context list direct response；
- binding invalidation event 及安全 fanout；
- create、close、strict Handoff complete 的失效事件接线；
- pre-register business request 的 `unauthorized` 与 provenance 规则；
- conflict cursor、request cache、emit 背压和断连行为；
- SQLite、PostgreSQL、MySQL 查询索引及 migration；
- 单元、Socket 集成、并发、restart、conformance 和真实 Android/Windows 联调验证。

### 2.3 本 Goal 不包含

- 修改 Flutter 业务代码；
- 用 HTTP `/emo/web-context-bindings` 代替 Socket action；
- 将 Context 信息塞入 `device.list`；
- 增加 legacy session fallback；
- 强制“一台设备永远只能有一个 active Context”的数据库唯一约束；
- multi-worker realtime、Redis broker、transactional outbox；
- 改造 Follow/Broadcast wire contract；
- production rollout、自动开启部署配置或发布镜像。

现有 Web strict 页面可继续使用 `/emo/web-context-bindings`。允许复用底层查询 helper，但本 Goal
不要求迁移 Web 前端到新 Socket action，也不得删除现有 HTTP endpoint。

---

## 三、当前仓库基线与差距

### 3.1 已有基础

当前仓库已经具备：

- strict-v2 request validator、output validator、request cache 和唯一结算保护；
- `connectionNonce` / `connectionEpoch` recipient 注入；
- PlaybackContext create/status/subscribe/unsubscribe/close；
- queue.context.sync、普通 player control 和 playback.update；
- Context、DevicePlaybackState、Handoff 持久化；
- per-Context Python `RLock` 和数据库事务；
- strict send-buffer reservation、限流、单 worker readiness gate；
- Core、Follow、Handoff、Broadcast 测试与 conformance manifest；
- `listUserPlaybackContexts()` 和 Web context bindings 查询逻辑。

### 3.2 r7 关键缺口

制定本 Goal 时，仓库证据表明：

1. 注册描述符、fixtures 和测试仍声明 `protocolVersion:2.1.0`；
2. `ACTION_SCHEMAS` 没有 `playback.context.list`；
3. `STRICT_OUTPUT_ACTIONS`、output type/schema 没有 list response 和
   `playback.context.bindings.changed`；
4. `_DIRECT_RESPONSE_ACTIONS` 没有 Context list；
5. Socket dispatcher 没有 Context list handler；
6. 存储层只有按用户全量列举，没有按 user + authority client/device + active 精确查询；
7. create/close/Handoff complete 不返回统一的内部 binding mutation 结果，也不向非 subscriber
   controller 发失效事件；
8. control mutation 只有 per-Context 锁，没有 authority/device pair 级串行化和唯一 active Context
   检查；
9. output validator 只把 auth/register error 视为 bootstrap，无法表达“业务 action 在 register 前
   返回无 provenance unauthorized”；
10. `ws_state.list_sids(user_name=...)` 当前没有实际应用 `user_name` / `session_id` filter。新事件若
    直接复用，会产生跨用户投递风险；
11. conformance hash、requirements 映射和 evidence 仍绑定 r5 / REQ-001 至 REQ-022；
12. strict Core 当前标记 ready，但尚未包含 r7 list、binding event 和 pair-level control guard。

### 3.3 数据模型判断

`EmoPlaybackContext` 已持久化：

- `playback_context_id`；
- `user_name`；
- `authority_client_id`；
- `authority_device_session_id`；
- `lifecycle`；
- queue/playback/cursors。

r7 不需要新增业务列。为避免高频 discovery 全表扫描，本 Goal 增加非唯一复合索引：

```text
(user_name, lifecycle, authority_client_id, authority_device_session_id)
```

不得增加 authority pair 唯一约束；r7 明确要求服务端能发现多个 Context 并 fail-closed。

---

## 四、必须保持的实现决策

### 4.1 Authority pair

内部统一使用：

```text
AuthorityPair = (
  userName,
  authorityClientId,
  authorityDeviceSessionId,
)
```

任何 query、lock、event recipient 或 control ambiguity 判断都必须包含 userName。不能用 clientId 或
deviceSessionId 单字段代替 pair，也不能从 sessionId 构造 pair。

### 4.2 Discovery 数据源

`playback.context.list` 必须查询持久化 `EmoPlaybackContext`，而不是只查询内存状态或在线设备：

- 只匹配 authenticated user；
- 只匹配 `lifecycle="active"`；
- client/device 两字段精确匹配；
- 按 `playback_context_id` 升序；
- authority 离线仍可返回 binding；
- 关闭的 tombstone 不返回；
- 其他用户或错误 deviceSessionId 返回成功空数组。

### 4.3 锁顺序

固定锁顺序，禁止 handler 自行嵌套：

```text
playbackContextId lock
  -> sorted AuthorityPair locks
    -> database transaction / row validation
```

当前所有受影响 mutation 都只修改一个 PlaybackContext，因此先取得 Context lock，再取得一个或
两个排序后的 pair lock，不会形成跨 Context 锁环：

- create：新 Context lock -> target pair lock；
- close：Context lock -> close 前 pair lock；
- control：Context lock -> 当前 pair lock；
- Handoff complete：Context lock -> sorted(old pair, new pair) locks。

取得 Context lock 后必须重新读取 Context/pair；取得 pair lock 和 DB transaction 后再次验证
lifecycle、authority 和 cursor。禁止基于锁外快照直接提交。

### 4.4 Mutation 与 event

create、close、Handoff complete 的 store API 必须返回内部 mutation metadata，至少包含：

```text
mutated: bool
affectedAuthorityPairs: distinct AuthorityPair[]
canonicalContext: object
```

这些字段仅用于服务端内部，不能进入 strict wire response。幂等重放或未改变 binding 的重复操作
必须返回 `mutated:false`，不得重复发送 binding event。

### 4.5 Event fanout

`playback.context.bindings.changed` 必须：

- 只发同一 authenticated user；
- 只发已注册、negotiated `playbackContextV2:true`、具有 controller 角色的当前 Socket；
- 不依赖 Context subscription；
- 每个 recipient 单独构造 provenance；
- 不带 requestId，不进入原请求 settlement cache；
- 单个 recipient 发送失败不阻止其他 recipient；
- 无法可靠 reserve/emit 时断开该 recipient Socket；
- mutation 已提交后不因 event 失败回滚。

必须修正 `ws_state.list_sids()` 的 filter 行为，或新增一个具有明确 user/role/capability 语义的安全
recipient helper。不得在业务 helper 中先列全局 sid 再依靠 payload 不含 user 来“隐藏”跨用户投递。

### 4.6 多 Context 控制保护

`queue.playItem` 和所有允许的 `player.*` 在 mutation transaction 内查询当前 authority pair 的 active
Context，最多读取两行即可判断歧义：

- 只有请求 Context 一行：继续 cursor 校验和 mutation；
- 0 行、请求 Context 不属于结果或至少两行：拒绝；
- 多 Context 拒绝使用 `conflict`，携带请求 Context 的
  `playbackContextId/currentControlVersion/currentQueueRevision/currentVersion`；
- 不 reserve/发送 authority command 的副作用，或已 reserve 时必须释放；
- 不修改 Context、queue、cursor 或 DevicePlaybackState。

检查和 mutation 必须处于同一 pair-level 临界区。binding event 是客户端缓存失效机制，不是服务端
接受控制的安全前提。

### 4.7 Readiness 安全

r7 Core 完整通过前：

- 不得发布声称 `protocolVersion=2.2.0` 且 Core ready 的构建；
- 不得把新的 contract hash 与旧 r5 evidence 组合为 ready；
- 中间构建若可能部署，必须保持 `codeConformanceReady:false` 或部署配置
  `emo_strict_v2_core_enabled=off`；
- 最终 freeze 时重新采集 r7 Core、Follow、Handoff、Broadcast evidence。全局 contract hash 改变
  后，旧 evidence 不得直接沿用。

---

## 五、实施 Goals

## Goal 0：冻结 r7 inventory 与安全门禁

### 工作项

1. 确认 r7 contract SHA-256 为本 Goal 头部值；若 contract 再改，先更新本 Goal、fixtures 和 hash；
2. 将现有 r5 Goal 标记为历史实施依据，不得作为 r7 wire 权威；
3. 更新 conformance requirements inventory：REQ-001 至 REQ-025；
4. 为新增 request action、server event 和 pair-level guard 建立测试映射占位；
5. 制定中间构建 readiness 策略，防止未完成 r7 的构建协商 Core；
6. 保存制定时 baseline commit、工作树 diff 和最窄测试结果；
7. 明确本次不删除 legacy action、不修改 Web bindings endpoint。

### 完成门槛

- r7 contract、Goal、manifest 计划使用同一个 SHA-256；
- REQ-001 至 REQ-025 均有拟定测试模块；
- 任一中间部署路径都不会把未完成的 r7 Core 协商为 true。

## Goal 1：协议版本、request/output schema 与 fixtures

### 工作项

1. 将注册描述符 `protocolVersion` 更新为 `2.2.0`；
2. 更新 metadata 单元测试和所有固定 `2.1.0` 断言；
3. 在 `ACTION_SCHEMAS` 增加：

   ```text
   playback.context.list / state
   required: authorityClientId, authorityDeviceSessionId
   optional: none
   ```

4. 在字段 validator 中把两个 authority 字段作为最大 128 UTF-8 bytes 的非空 string；
5. 在 `STRICT_OUTPUT_ACTIONS` 和 type map 增加：
   - `playback.context.list` / state；
   - `playback.context.bindings.changed` / event；
6. 将 Context list 纳入 correlated direct response；bindings.changed 必须拒绝 requestId；
7. 增加 list response output validator：
   - payload 只能有 contexts；
   - item 只能有三个 binding 字段；
   - authority 字段一致；
   - playbackContextId 唯一且升序；
8. 增加 bindings.changed output validator，payload 只能有两个 authority 字段；
9. 更新 `tests/fixtures/emo_strict_v2/manifest.json`：
   - protocolVersion 2.2.0；
   - 新 list request action；
   - create/close/Handoff 的 serverPushes 增加 bindings.changed；
   - contract section 编号同步 r7；
   - requirements 扩展至 REQ-025；
10. 增加 canonical request、空/单/多 list response 和 binding event fixtures；
11. 更新输出 action 数量和闭合 schema 测试；
12. 不把 bindings.changed 加入客户端 request `ACTION_SCHEMAS`。

### 完成门槛

- list request、list response 和 binding event 的所有未知字段/null/type/sort 测试通过；
- list response 必须有 requestId，binding event 必须没有 requestId；
- r5 客户端 action 仍保持原 schema；
- manifest actions 与 executable request validators 精确一致。

## Goal 2：持久化 discovery query、索引与 pair lock

### 工作项

1. 在 `ws_store.py` 增加精确查询，例如：

   ```python
   listActivePlaybackContextBindings(
       user_name,
       authority_client_id,
       authority_device_session_id,
   )
   ```

2. query 必须在数据库层一次完成 user/lifecycle/pair 过滤并按 Context ID 排序；
3. serializer 只返回三个 r7 binding 字段，不复用完整 Context serializer；
4. 为 SQLite、PostgreSQL、MySQL base schema 增加非唯一复合索引；
5. 使用下一个未占用 migration ID（计划 `20260715`）为三种数据库增加同名语义索引；
6. 更新 Peewee model Meta indexes；
7. 增加 AuthorityPair key normalization 和 lock registry；
8. 重构现有 per-context decorator，使 create/close/control/Handoff complete 能遵循固定锁顺序；
9. pair lock 只存在于单 realtime worker 进程；继续依赖 r7 的单 worker 启动约束；
10. 新增内部异常 `PlaybackContextAuthorityAmbiguousError`，携带请求 Context canonical snapshot；
11. 不改变 Context wire snapshot，也不新增数据库业务列。

### 完成门槛

- 同用户精确 pair 返回 active Context，其他用户/错误 session/closed 返回空；
- 结果稳定升序，authority 离线不影响查询；
- SQLite、PostgreSQL、MySQL clean install 和 migration 后索引存在；
- 无数据库唯一约束阻止多 Context 测试；
- 锁顺序测试不存在死锁或锁外提交。

## Goal 3：实现 `playback.context.list`

### 工作项

1. 在 strict dispatcher 中增加独立 list 分支，不能进入 legacy/session resolver；
2. 请求必须完成 auth 和 device.register；
3. 当前 client 必须 negotiated `playbackContextV2:true` 且具有 controller 角色；
4. 调用 Goal 2 的持久化精确查询；
5. 使用同 requestId 的 direct response，禁止先 ACK；
6. 空结果返回 `{contexts:[]}`，不得使用 not_found/context_closed/authority_offline；
7. 多结果全部返回，服务端不排序以外做选择，也不建立 subscription；
8. request cache 重放同一 direct response；新 requestId 必须重新查询 canonical binding；
9. 日志只记录 user、requestId、authority client、结果数量和结果 code，不打印队列或完整 Context；
10. 跨用户 pair 和不存在 pair 使用相同成功空结果，避免枚举；
11. 保留 Web `/emo/web-context-bindings`，可选择复用 store helper，但不得改变其既有 JSON。

### 完成门槛

- 0/1/多个结果 direct response 测试通过；
- controller-only 和 player+controller 可查询，player-only 返回 forbidden；
- 同 clientId 不同 deviceSessionId 返回空；
- 请求和响应不出现任何 sessionId；
- 相同 requestId 缓存重放不重新查库，新 requestId 能观察 create/close/handoff 变化。

## Goal 4：实现 binding invalidation event

### Recipient 前置

1. 修正 `ws_state.list_sids()` 对 user/session filter 不生效的问题，或新增更窄的安全 helper；
2. 增加跨用户回归测试，证明 device list 和新 binding event 都不会发给其他用户；
3. recipient 最终由 authenticated user、registered client、controller role、negotiated capability 四项
   共同筛选。

### Event helper

实现统一 helper，例如：

```text
_broadcast_playback_context_bindings_changed(
  user_name,
  affected_pairs,
)
```

要求：

1. pair 去重并使用确定性顺序；
2. 每个 pair 对每个 recipient 发送一条无 requestId event；
3. 逐 sid 注入各自 provenance；
4. 使用 strict send-buffer reservation；
5. reserve 失败或 emit 抛错时断开该 sid，继续其他 recipients；
6. event 不进入原 mutation request cache；
7. 不持久化、不重放；重连依靠新的 list；
8. fanout 日志只记录 user、pair、recipient count 和失败 sid 数量。

### Mutation 接线

1. create store/handler 保留 `_created`，只有真实创建时产生 pair event；
2. close store 返回 `mutated` 和 close 前 pair，重复 close 不重复 event；
3. strict Handoff complete 返回 `_mutated`、old pair、new pair；重复 complete 不重复 event；
4. handoff old/new pair 相同则只发一次；
5. mutation commit 和请求结算完成后再发 event；
6. Context closed/status/handoff canonical push 的既有结算方式保持不变；
7. 一个 recipient 失败不得回滚数据库或阻断其他 recipient。

### 完成门槛

- create/close/handoff complete 的真实 mutation 各产生正确事件；
- 幂等重放不重复事件；
- 非 subscriber controller 也收到；player-only、legacy、其他用户收不到；
- old/new pair handoff 测试覆盖；
- send buffer 满或 emit 异常时目标 sid 被断开，其他 controller 仍收到；
- event 没有 requestId、Context ID、queue、状态或 cursor。

## Goal 5：Pair-level control serialization 与 ambiguity conflict

### 工作项

1. 将 create、close、`completeStrictPlaybackHandoff` 和
   `mutateStrictPlaybackContextControl` 接入 Goal 2 的锁顺序；
2. control transaction 内查询当前 pair 的 active Context，读取最多两行判断歧义；
3. 唯一结果必须是请求 Context，否则抛出 ambiguity exception；
4. ws error mapping 使用 `conflict`，并返回：
   - playbackContextId；
   - currentControlVersion；
   - currentQueueRevision；
   - currentVersion；
5. ambiguity conflict 不发送 authority command，不递增 cursor；
6. 如果 authority emit reservation 已在 store check 前取得，异常路径必须释放；
7. 保留 stale_version 优先级：唯一 Context 时再校验 base cursor；多 Context 时先 conflict；
8. queue.context.sync、playback.update、close 仍可用于权威设备收敛/解除歧义，不受普通 control guard
   阻止；
9. 不把客户端 `ambiguous_playback_scope` 作为 wire error code；
10. 增加并发 barrier 测试，验证线性化结果只有两种：
    - control 先提交，随后 create/handoff 改变 binding；
    - binding mutation 先提交，control 返回 conflict；
11. 禁止出现“binding mutation 已提交但 control 仍成功”的第三种结果。

### 完成门槛

- 两个 active Context 时 pause/play/seek/next/prev/playItem 全部 conflict；
- conflict payload 四个 Context/cursor 字段完整；
- conflict 前后数据库、内存和 authority 收件箱均无控制副作用；
- close 一个 Context 后重新 list/status，剩余唯一 Context 可恢复控制；
- create、close、handoff、control 并发测试在重复运行下稳定，无死锁。

## Goal 6：Bootstrap error provenance 与注册门控

### 工作项

1. 将“是否已完成 strict register”作为 output validation context，而不是只根据 error action 猜测；
2. 扩展 `validate_strict_output` 或 recipient builder，使其能区分：
   - register 前 correlated error：必须无 nonce/epoch；
   - register 后 strict error：必须有 nonce/epoch；
3. auth/register 成功 ACK 继续使用现有 bootstrap 规则；
4. 在 envelope correlation 合法后、业务 payload schema 之前执行 auth/register gate；
5. register 前发送 device.list、playback.context.list 或其他业务 action，统一返回 unauthorized；
6. 非法/缺失 action/requestId 仍按契约直接断开，不发送伪造 error；
7. register 后同样请求必须经过完整 strict schema；
8. request cache 的 bootstrap key 继续绑定当前物理连接，不跨 reconnect 重放；
9. 不放宽注册后 provenance validator。

### 完成门槛

- register 前 Context list unauthorized 且无 provenance；
- register 后 Context list error 必有当前 Socket provenance；
- 旧 nonce 的错误消息不能被新连接接受；
- malformed correlation 仍直接断开；
- output validator 单元测试覆盖四种组合：pre/post register × provenance present/absent。

## Goal 7：Conformance、回归与文档冻结

### 自动化测试

至少新增或扩展：

- `tests/base/test_emo_strict_v2_contract.py`；
- `tests/base/test_emo_strict_v2_manifest.py`；
- `tests/base/test_emo_protocol_metadata.py`；
- `tests/base/test_emo_ws_store.py`；
- `tests/base/test_emo_strict_v2_core.py`；
- `tests/base/test_emo_strict_v2_handoff.py`；
- `tests/base/test_emo_strict_v2_safety.py`；
- `tests/base/test_emo_schema_migration.py`；
- 建议新增 `tests/base/test_emo_strict_v2_discovery.py`，集中覆盖 list/event/pair race。

必须覆盖：

1. list request/output 闭合 schema；
2. user/controller/capability 权限；
3. 0/1/多个、closed、offline、错误 deviceSession；
4. create/close/handoff event recipients；
5. event 重复、emit failure、断连和 reconnect；
6. event 与在途 list response 的两种到达顺序；服务端 fixtures 明确 Flutter 必须使用
   discoveryGeneration 丢弃旧响应；
7. pair-level control conflict 和 cursor 不变；
8. create/control、handoff/control、close/control 并发；
9. pre-register unauthorized provenance；
10. protocolVersion 2.2.0 和低版本 fail-closed fixtures；
11. cross-user sid fanout 回归；
12. legacy Emo 和 Web bindings endpoint 回归。

### 建议验证命令

先窄后宽：

```bash
python -m unittest tests.base.test_emo_strict_v2_contract
python -m unittest tests.base.test_emo_protocol_metadata
python -m unittest tests.base.test_emo_ws_store
python -m unittest tests.base.test_emo_strict_v2_discovery
python -m unittest tests.base.test_emo_strict_v2_core
python -m unittest tests.base.test_emo_strict_v2_handoff
python -m unittest tests.base.test_emo_strict_v2_safety
python -m unittest tests.base.test_emo_strict_v2_manifest
python -m unittest tests.base.test_emo_schema_migration
python -m unittest tests.base.test_emo_ws
python -m unittest
```

如果未新增 discovery 独立模块，将对应测试替换为实际 dotted path。

### Conformance freeze

1. 重新计算 r7 contract SHA-256；
2. 更新 `STRICT_V2_CONTRACT_SHA256`、conformance JSON 和 fixture manifest；
3. 更新 requirements 映射到 REQ-001 至 REQ-025；
4. 所有 profile 重新采集当前 contract hash 下的 evidence；
5. Core 只有在 Goal 0 至 Goal 7 全部通过后才能设置 `codeConformanceReady:true`；
6. Handoff profile 只有在 old/new pair invalidation 测试通过后才能恢复 ready；
7. Follow/Broadcast 即使 wire 未变，也必须重新运行与新 Core/output validator 的组合回归；
8. 将命令、结果、contract hash、serverBuildCommit 和失败注入结果保存到：

   ```text
   docs/verification/emosonic_strict_v2_r7/<serverBuildCommit>/
   ```

### 完成门槛

- REQ-001 至 REQ-025 均有自动化测试映射和通过证据；
- manifest、request validators、output validators、contract hash 和 protocolVersion 一致；
- 全量 unittest 通过；
- 未修改的 legacy/Web 行为有回归证据；
- readiness evidence 不引用旧 r5 contract hash。

## Goal 8：Android / Windows 联调验收

### 场景 A：原始问题

```text
Windows：queueLength=50，currentIndex=39，有唯一 active Context
Android：从 device.list 选择 Windows
```

验证：

1. Android 用 clientId/deviceSessionId 请求 Context list；
2. 返回唯一 playbackContextId；
3. subscribe ACK；
4. status 返回 queue、index=39、state、position 和 cursors；
5. player.pause 在 Windows 执行；
6. queue.playItem 切换正确歌曲；
7. Windows playback.update 被 subscribers 接收；
8. 全链路无 session fallback。

### 场景 B：重连

1. Windows 使用相同 clientId/deviceSessionId 重连；
2. Android 收到 device.list 后使用新 requestId list；
3. 重新 subscribe/status；
4. 控制恢复；
5. 相同 clientId、不同 deviceSessionId 不发现旧 Context。

### 场景 C：多 Context

1. Windows 创建第二个 active Context；
2. Android 即使不是新 Context subscriber，也收到 bindings.changed；
3. Android 暂停控制并重新 list；
4. list 返回多个 Context，客户端进入 ambiguous_playback_scope；
5. event 到达前抢发的 control 由服务端 conflict 拒绝；
6. close 一个 Context 后再次 event/list/status，唯一 Context 恢复控制。

### 场景 D：Handoff

1. Context 从 Windows handoff 到另一 player；
2. old/new pair controller 均收到 invalidation；
3. 旧 Windows list 不再返回该 Context；
4. 新 player list 返回同一个 Context ID；
5. 已订阅 Context 的 controller 收敛到新 authority；
6. 旧 Context ID 没有被错误 tombstone 或重新创建。

### 场景 E：事件可靠性

1. 注入某 controller send buffer full；
2. mutation 保持 committed；
3. 该 controller Socket 被断开；
4. 其他 controller 正常收到 event；
5. 断开客户端重连后通过 canonical list 恢复。

### 联调完成门槛

- Android 与 Windows 日志能按 requestId、playbackContextId、controlVersion、queueRevision 对齐；
- 多 Context、close、handoff、重连均 fail-closed；
- 任何错误路径都没有 clientId/sessionId 猜测 Context；
- 联调记录绑定唯一 serverBuildCommit、Flutter build ID 和 r7 contract hash。

---

## 六、文件改动范围

### 预计修改

| 文件 | 计划内容 |
| --- | --- |
| `supysonic/emo/strict_v2_contract.py` | request/output action、schema、direct response、bootstrap validation |
| `supysonic/emo/ws.py` | list handler、event fanout、mutation 接线、error mapping、pre-register gate |
| `supysonic/emo/ws_store.py` | binding query、pair lock、mutation metadata、ambiguity guard |
| `supysonic/emo/ws_state.py` | 安全 sid filtering/recipient enumeration |
| `supysonic/emo/protocol_metadata.py` | 2.2.0 metadata 读取和测试适配（如需） |
| `supysonic/emo/strict_v2_registration_descriptor.json` | protocolVersion 2.2.0 |
| `supysonic/emo/strict_v2_conformance.py` | r7 contract hash |
| `supysonic/emo/strict_v2_conformance.json` | r7 readiness/evidence |
| `supysonic/db_layer/emo.py` | discovery 复合索引 |
| `supysonic/schema/sqlite.sql` | base schema 索引 |
| `supysonic/schema/postgres.sql` | base schema 索引 |
| `supysonic/schema/mysql.sql` | base schema 索引 |
| `supysonic/schema/migration/*/<next>.sql` | 三数据库索引 migration |
| `tests/fixtures/emo_strict_v2/` | r7 manifest 和 JSON fixtures |
| `tests/base/test_emo_*` | schema、store、Socket、并发、安全、migration 测试 |
| `docs/emosonic_strict_v2_*.rst` | 2.2.0 部署和联调说明 |
| `docs/verification/emosonic_strict_v2_r7/` | 最终验证证据 |

### 可能新增

- `tests/base/test_emo_strict_v2_discovery.py`；
- 三数据库 `20260715` migration；
- r7 canonical fixture 文件；
- r7 implementation audit/change note。

### 不应修改

- legacy action wire shape；
- Flutter 源码；
- `supysonic.conf` 私有运行配置；
- media library、scanner、daemon、transcoding 和数据库业务数据；
- `device.list` strict device object 字段集合。

---

## 七、建议提交顺序

每个提交都必须保留可解释的 readiness 状态；禁止中间提交声称 r7 Core ready。

1. **r7 inventory 与 readiness gate**
   - protocol/contract inventory、REQ-025、版本测试骨架；
2. **request/output schema**
   - list request、list response、bindings.changed validator 和 fixtures；
3. **store discovery 与 migration**
   - query、索引、pair lock primitive；
4. **Context list handler**
   - auth/role/user scope/direct response；
5. **binding invalidation fanout**
   - recipient filter、create/close/handoff 接线、emit failure disconnect；
6. **pair-level control guard**
   - ambiguity conflict、cursor、并发测试；
7. **bootstrap provenance**
   - pre-register unauthorized 和 output validation context；
8. **conformance freeze**
   - 2.2.0 descriptor、最终 hash、evidence、readiness true；
9. **docs and acceptance evidence**
   - deployment notes、双客户端联调记录。

如果提交 2 至 7 可能被单独部署，Core code readiness 必须保持 false。只有提交 8 完整通过后才能
恢复 true。

---

## 八、风险与控制措施

### 风险 1：跨用户 event 泄露

现状 `list_sids(user_name=...)` filter 不生效。

控制：先修 recipient enumeration，并新增两个用户、相同 clientId 的 Socket 测试；不允许在该测试
完成前接线 bindings.changed。

### 风险 2：锁顺序死锁

控制：所有受影响 store mutation 只使用统一 lock bundle；固定 Context lock -> sorted pair locks；
并发测试使用 barrier 和超时，不能只断言最终值。

### 风险 3：事件到达前控制穿透

控制：多 Context 检查必须在服务端 control transaction 内完成；event 只负责客户端缓存失效。

### 风险 4：幂等重放重复 event

控制：store 返回 `mutated`，只有 binding set 真实变化才 fanout；request cache 重放只重放原结算，
不重新执行 post-commit event。

### 风险 5：emit failure 留下陈旧 controller

控制：critical invalidation reserve/emit 失败时断开该 sid；mutation 不回滚；重连强制 list。

### 风险 6：版本过早升级

控制：2.2.0 metadata、r7 hash 和 Core ready 必须在最终 freeze 同时成立；旧 evidence 清零并重采。

### 风险 7：output validator 放宽过度

控制：provenance 是否必需由 recipient registration state 显式传入；不能把所有 system.error 都当作
bootstrap。

### 风险 8：Web bindings 回归

控制：保留 endpoint 和 JSON；若复用新 query helper，运行 Web strict-v2 和 browser acceptance
测试。

### 风险 9：migration 差异

控制：三数据库使用相同索引语义和不同合法 DDL；clean install、upgrade 和重复 migration 都测试。

### 风险 10：contract/manifest/hash 漂移

控制：manifest test 绑定实际 contract bytes；任何 contract 修改都要求重新 freeze 和 evidence。

---

## 九、Definition of Done

只有以下全部满足，Goal 才完成：

1. 服务端注册 metadata 返回 `protocolVersion:2.2.0`；
2. `playback.context.list` request/direct response 完全符合 r7 闭合 schema；
3. discovery 使用 user + authority client/device + active 的持久化精确查询；
4. 空、单、多个 Context 结果均符合契约；
5. `playback.context.bindings.changed` 按 r7 发送给全部同用户 strict controller；
6. 非 subscriber controller 收到 event，其他用户/player-only/legacy 收不到；
7. event emit 失败断开对应 sid，不回滚 mutation；
8. create/close/handoff 幂等重放不重复 event；
9. 普通 player control 在多 Context 时返回完整 cursor conflict；
10. 多 Context conflict 不发送 authority command、不修改 cursor；
11. create/close/handoff/control 在 pair 级线性化，无死锁；
12. register 前业务 error 无 provenance，register 后严格要求 provenance；
13. `device.list` 未新增任何 Context 字段；
14. 全链路没有 session/client/device ID 猜测 Context；
15. SQLite、PostgreSQL、MySQL base schema 和 migration 具备 discovery 索引；
16. r7 manifest 覆盖 list action、binding push 和 REQ-001 至 REQ-025；
17. contract hash、manifest、conformance constant、evidence 和 serverBuildCommit 一致；
18. Core、Handoff 及全量 unittest 通过；Follow/Broadcast 组合回归通过；
19. legacy Emo 和 Web strict bindings 行为无回归；
20. Android/Windows 场景 A 至 E 有可复现日志；
21. Core readiness 只在全部证据完成后恢复 true；
22. 未执行 production rollout，部署开关仍由外部审批控制。

---

## 十、执行检查表

- [x] Goal 0：冻结 r7 inventory 和 readiness 策略
- [x] Goal 1：更新 2.2.0 schema、output action 和 fixtures
- [x] Goal 2：实现 binding query、索引和 pair lock
- [x] Goal 3：实现 playback.context.list
- [x] Goal 4：实现 bindings.changed 和安全 fanout
- [x] Goal 5：实现 pair-level ambiguity control conflict
- [x] Goal 6：修正 bootstrap provenance 和 pre-register gate
- [ ] Goal 7：自动化测试已通过；final conformance evidence/readiness freeze 待唯一 committed build
- [ ] Goal 8：完成 Android / Windows 联调验收
- [ ] 保存 r7 final verification evidence（working-tree automation evidence 已保存）
- [ ] 确认 Definition of Done 全部满足
