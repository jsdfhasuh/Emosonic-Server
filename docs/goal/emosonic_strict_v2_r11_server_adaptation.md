# Goal: EmoSonic strict-v2 Socket.IO r11 服务端适配

> 状态：In Progress（Goal 0 已完成；Goal 1 的 2.4.0 schema/fixtures 适配进行中）
>
> 制定日期：2026-07-17
>
> 目标协议：PlaybackContext strict-v2 `2.4.0` / contract r11
>
> 已批准的 r11 来源：`ref/change/emosonic_strict_v2_socketio_server_contract.md`
>
> r11 来源 SHA-256：`4bf1a099fd3c060514215c202b7bb3c82b80e9c73959c39782541d8cda9dea96`
>
> 当前规范：`specs/emosonic_strict_v2_socketio_server_contract.md` r11 / `2.4.0`
>
> 制定时基线 commit：`067ddfb3b64facbc7df2a0ef32ac2b011dd1c411`
>
> 实施边界：本 Goal 负责 Supysonic 服务端、仓库内 Web strict 客户端、fixtures、数据库迁移、
> conformance 和服务端联调证据；不修改外部 Flutter 或 Windows 工程。

## 一、最终目标

把当前 r8 / `2.3.0` 服务端升级为 r11 / `2.4.0`，完成以下闭环：

1. Windows 注册后用 `playback.context.ensure` 保证一个稳定 client 只有一个 active Context；
2. 没有队列时也保存合法 idle Context，不再等到实际播放后才创建 Context；
3. controller 必须先用 `playback.context.prepare` 把 idle Context 准备成非空，再发送播放命令；
4. 服务端把“已接受的控制目标”和“Windows 实际执行状态”分开保存；
5. 每个远程控制都有持久化 pending/terminal 事务，ACK 只代表已接受并已路由；
6. `playback.update` 严格区分 passive、远程成功、远程失败和本地人工操作四种来源；
7. 服务端需要主动结束事务时，只发送 server-only `playback.control.settled`，不能伪造 Windows
   `playback.update`；
8. 后一条命令依赖失败时，逐条结束每个具体版本，不能让 Flutter 根据版本区间猜；
9. Windows 默认执行期限为 15000ms，服务端 watchdog 固定晚 2000ms；17 秒到期只能报告
   `execution_unknown`；
10. 断线、Socket 替换和服务重启后不自动重放结果不明的 next、previous、seek 或其他控制；
11. 迟到的旧 Windows 状态不写库、不广播，只把当前实际状态回给发来旧报告的原 Socket；
12. `requestingClientId` 始终表示最初发命令的 controller，不得改回容易混淆的
    `sourceClientId`。

本 Goal 是实施拆解，不改写 wire contract。实现、测试或历史文档与 r11 冲突时，以升级后的
`specs/emosonic_strict_v2_socketio_server_contract.md` r11 为准。

---

## 二、权威顺序和范围

### 2.1 实施时的权威顺序

1. Goal 0 正式提升后的 `specs/emosonic_strict_v2_socketio_server_contract.md` r11；
2. 本 Goal；
3. `ref/change/strict-v2-2.4-r11-flutter-migration-guide.md`；
4. 当前服务端实现和自动化测试；
5. r8、r7、r5 Goal 和其他历史说明。

迁移指南用于解释改动，不得单独增加字段、动作、错误码或改变结算语义。
r11 替代从未发布的 r10；r10 不作为实现、兼容或测试权威。

### 2.2 本 Goal 包含

- 把已批准 r11 契约提升到 `specs/` 权威路径；
- `protocolVersion:2.4.0`、注册描述符、closed schema 和 strict fixtures；
- `playback.context.create` 到 `playback.context.ensure` 的服务端替换；
- idle Context、prepare/prepared 状态机；
- 控制事务、prepare 事务、本地 intent 和 applied cursor 的持久化；
- 四种 `playback.update` 输入和对应 canonical 输出；
- `playback.control.settled` 的持久化后发送、逐命令发送、顺序和幂等；
- 15000ms 执行期限、17000ms watchdog、断线/替换/重启结算；
- stale applied feedback 的 source-only correction；
- Handoff 与 target idle Context 的原子退休；
- 仓库内 Web strict 客户端和测试 fixture 迁移到 `ensure`；
- SQLite、PostgreSQL、MySQL base schema 与 migration；
- 单元、Socket、并发、超时、重启、conformance 和双客户端验收。

### 2.3 本 Goal 不包含

- 修改 Flutter 或 Windows 源码；
- 重新设计 Follow、Broadcast、Handoff 的既有 wire shape；
- 引入 Redis broker、跨 worker 事务、消息队列或 transactional outbox；
- 把 `playback.control.settled` 做成客户端可发送 action；
- 自动重发 pending 或 `execution_unknown` 命令；
- 用 HTTP、legacy `sessionId` 或猜测 client/device 的方式补协议缺口；
- 修改媒体库、扫描器、daemon、转码或用户私有 `supysonic.conf`；
- 自动打开 production rollout。

开发和测试环境可以用明确开关进行本地联调，不要求生产证据或精确 build metadata 才能启动；
production 仍必须 fail-closed。

---

## 三、当前仓库基线和主要差距

### 3.1 已有基础

当前 r8 服务端已经具备：

- strict request/output validator、request cache 和 recipient provenance；
- PlaybackContext create/list/status/subscribe/close；
- binding invalidation、pair-level control guard 和 device-scoped volume；
- queue、普通 player control、playback.update、Handoff、Follow、Broadcast；
- `EmoPlaybackContext`、`EmoDevicePlaybackState`、Handoff 持久化；
- SQLite、PostgreSQL、MySQL schema/migration 测试；
- 单 worker readiness 和 strict send-buffer 保护；
- r8 `2.3.0` manifest、contract hash 和 conformance 结构。

### 3.2 r11 缺口

制定本 Goal 时，仓库证据表明：

1. `specs/`、注册描述符和 manifest 仍是 r8 / `2.3.0`；
2. strict action 仍使用 `playback.context.create`，没有 `playback.context.ensure`；
3. Context 默认状态和 serializer 仍按非 idle 模型设计；
4. 没有 `playback.context.prepare` / `playback.context.prepared` 状态机；
5. 没有持久化控制事务，无法区分 accepted、committed、failed、superseded；
6. `EmoDevicePlaybackState` 没有明确的 applied cursor 和连接级 client sequence；
7. 没有 localUser intent 的长期幂等记录；
8. `playback.update` 没有 r11 四种闭合输入 shape 和严格单调校验；
9. 没有 server-only `playback.control.settled`；
10. server-routed command 不带 `executionTimeoutMs`；
11. 没有 17 秒 watchdog、断线/Socket 替换/restart 的 unknown 结算；
12. 没有 dependency cascade 的原子写库和逐版本 settled；
13. 迟到旧 applied feedback 还没有 source-only correction 语义；
14. Handoff 没有原子退休 target idle Context 的 r11 规则；
15. 仓库内 Web strict fixture 和测试仍发送 `playback.context.create`；
16. conformance inventory 只覆盖到 r8 REQ-026，没有 r11 REQ-027 至 REQ-045。

---

## 四、固定实现原则

### 4.1 实际状态和控制目标必须分开

`EmoPlaybackContext.control_version` 表示服务端已经接受的最新控制版本。Windows 实际执行到哪里，
由 authority `EmoDevicePlaybackState.applied_control_version` 表示。允许：

```text
appliedControlVersion < controlVersion
```

不能因为服务端接受了切歌目标，就提前把 DevicePlaybackState 当成已经切歌成功。

### 4.2 一条远程命令必须有一条持久化事务

事务主键固定为：

```text
(playbackContextId, epoch, commandControlVersion)
```

状态只允许单向变化：

```text
pending -> committed
pending -> failed
pending -> superseded
```

terminal 后不得改变结果。重复相同 terminal 无副作用；不同 terminal 内容是协议冲突。

### 4.3 settled 只结束事务

`playback.control.settled`：

- 只能由服务端发送；
- 只能把指定 pending 事务写成 failed terminal；
- 不改歌曲、播放状态、位置或 DevicePlaybackState；
- 不生成 Windows `clientSeq`；
- 不冒充 Windows `playback.update`；
- 必须先提交数据库，再 emit；
- 一条命令对应一条消息；
- 多条按 `commandControlVersion` 从小到大发送；
- 使用 `requestingClientId`，不使用 `sourceClientId`。

### 4.4 timeout 和 unknown 不混用

```text
execution_timeout
= Windows 已确认 execution lease 失效，迟到任务不会执行

execution_unknown
= 服务端没有收到结果，不知道 Windows 实际执行没有
```

服务端 watchdog、authority 断线、Socket 替换和服务重启只能产生 `execution_unknown`。服务端不能
生成或伪造 `execution_timeout`。

### 4.5 不重放结果不明的命令

pending 因断线、替换或重启进入 `execution_unknown` 后：

- 不投递给新 Socket；
- 不在 ensure 后恢复；
- 不自动重试；
- 客户端重新 list/subscribe/status 后由用户决定下一步。

### 4.6 锁和事务顺序

所有 Context、prepare、control、local intent、dependency cascade 和 Handoff authority switch 使用同一
串行区。固定顺序：

```text
playbackContextId lock
  -> sorted AuthorityPair locks（需要时）
    -> database transaction
      -> commit
        -> strict emit
```

不得在 emit 后补写 terminal，不得在锁外根据旧快照推进 cursor。

### 4.7 Socket 身份不能只看 clientId

控制事务在路由时保存 authority 的：

- clientId；
- deviceSessionId；
- connectionNonce；
- connectionEpoch。

发送 settled 给 Windows 前必须确认仍是原物理 Socket。旧 Socket 已断开或被替换时，不得把历史
settled 发给新连接。

---

## 五、计划中的数据模型

字段名是服务端内部建议；wire 字段仍以 r11 为准。实现时允许按 Peewee/数据库习惯调整内部名字，
但不能减少下面表达的状态。

### 5.1 `EmoDevicePlaybackState` 增量

新增明确列：

| 字段 | 用途 |
| --- | --- |
| `context_epoch` | 防止旧 generation 的状态进入新 binding |
| `applied_control_version` | Windows 最后确认执行的控制版本 |
| `client_seq` | 当前 authority 连接上报事实的单调序号 |

`playback_json` 继续只保存非索引扩展内容，不能把核心并发字段只藏在 JSON 中。

### 5.2 新增 `EmoPlaybackControlTransaction`

至少保存：

| 字段组 | 内容 |
| --- | --- |
| 身份 | user、playbackContextId、epoch、commandControlVersion |
| 原请求者 | requestingClientId |
| 执行者 | authorityClientId、authorityDeviceSessionId、routed nonce/epoch |
| 命令 | action、accepted target/fingerprint |
| 状态 | pending/committed/failed/superseded、errorCode、dependsOnControlVersion |
| deadline | acceptedAtMs、executionTimeoutMs、watchdogDeadlineAtMs |
| terminal | appliedControlVersion、terminal fingerprint、terminalAtMs |
| 审计 | createdAt、updatedAt |

数据库约束：

- 唯一 `(playback_context_id, epoch, command_control_version)`；
- 索引 `(status, watchdog_deadline_at_ms)`；
- 索引 `(playback_context_id, epoch, status, command_control_version)`；
- terminal 内容只允许首次写入，更新必须带 `WHERE status='pending'` 或等价原子保护。

### 5.3 新增 `EmoPlaybackPrepareTransaction`

至少保存：

- playbackContextId、epoch、intentId；
- requestingClientId；
- authority client/device 与路由连接身份；
- request fingerprint、可选 initial queue；
- accepted controlVersion；
- preparing/ready/failed；
- errorCode/errorMessage；
- deadlineAtMs、canonical result、时间戳。

唯一 `(playback_context_id, epoch, intent_id)`。同一 Context/epoch 只允许一个非终态 prepare；三种数据库
不依赖 partial unique index，由 Context 锁加事务查询保证。

### 5.4 新增 `EmoPlaybackLocalIntent`

至少保存：

- playbackContextId、epoch、intentId；
- authority client/device binding；
- request fingerprint；
- 首次 canonical playback.update；
- 分配的 controlVersion；
- supersededThroughControlVersion；
- 时间戳。

唯一 `(playback_context_id, epoch, intent_id)`。相同 fingerprint 重放首次结果，不同 fingerprint 返回
`conflict`。

### 5.5 Context 本身

`EmoPlaybackContext` 继续保存 queue、state、position 和四个 cursor。需要调整默认值和 serializer：

- 新 idle Context：空队列、`state='idle'`、`positionMs=0`；
- idle wire snapshot 省略 currentIndex/trackId；
- queue-backed snapshot 必须输出合法 currentIndex/trackId；
- 数据库内部可以保留 idle 的安全 index 占位，但任何 wire 输出都不得发送该占位；
- close、epoch 改变或 authority 改变时，旧 prepare/local intent 不得进入新 binding。

### 5.6 升级旧数据

迁移时不恢复 r8 的内存 pending 命令。升级策略：

1. 旧 active Context 和 queue/cursors 原样保留；
2. 空队列 Context 规范化为 idle；
3. 旧 DevicePlaybackState 只有在其自身已保存 payload 能证明 applied cursor 时才迁移该值；无法证明
   时内部置为未水合，不把 Context 的 canonical controlVersion 猜成已执行版本，也不输出该旧行；
4. 所有新事务表初始为空；
5. 服务启动时不存在可自动重放的历史 pending；
6. 任何无法证明的运行中命令在升级/重启边界按 unknown 处理，不尝试执行。

---

## 六、实施 Goals

## Goal 0：提升 r11 权威契约并关闭中间版本 readiness

### 工作项

1. 将已批准的 `ref/change/...contract.md` r11 原样提升到 `specs/...contract.md`；
2. 提升后重新计算 SHA-256，必须与本 Goal 头部 r11 来源 hash 相同；
3. 把 r8 Goal、迁移说明和实现注释标为历史依据；
4. 扩展 conformance inventory 到 REQ-001 至 REQ-045；
5. 在实现完成前保持 Core code readiness false，或保持部署开关关闭；
6. 开发/测试显式开关仍允许本地集成，不增加额外生产证据收集门槛；
7. 冻结本次 baseline commit 和初始测试结果。

### 完成门槛

- `specs/` 唯一权威文件为 r11 / `2.4.0`；
- contract、Goal、manifest 计划使用同一 hash；
- 未完成实现的构建不会对生产协商为完整 r11 Core ready。

## Goal 1：版本、action inventory 和 closed schema

### 工作项

1. 注册描述符、metadata 测试和 manifest 改为 `2.4.0`；
2. request action 删除 `playback.context.create`，增加 `playback.context.ensure`、
   `playback.context.prepare`、`playback.context.prepared` 的正确方向；
3. output action 增加 ensure direct response、prepare command/prepared event 和
   `playback.control.settled`；
4. `playback.control.settled` 明确为 server-only；客户端发送时按 strict 禁止 action 统一拒绝且无
   副作用，不为它增加客户端 request handler；
5. 为 idle/queue-backed Context 建立一套条件闭合 serializer/validator；
6. 为四种 `playback.update` 建立互斥 shape validator；
7. server-routed player command 和 queue.playItem 增加必需 `executionTimeoutMs`；
8. settled 严格字段只允许 r11 表中字段，禁止 requestId、clientSeq、deviceSessionId、
   sourceClientId、实际播放字段和 null；
9. 更新 JSON fixtures、manifest action inventory 和 schema negative cases；
10. 旧 `2.3.x` 客户端 fail-closed，不回退旧 create 或旧 playback.update shape。

### 完成门槛

- request/output validator 与 r11 action 表完全一致；
- create 不再是 2.4.0 strict action；
- idle、四种 update、settled 的多余字段和混合字段全部被拒绝；
- `requestingClientId` 是 settled 中唯一的原请求者字段。

## Goal 2：数据库 migration 和 store 原语

### 工作项

1. 更新 Peewee models 和三种 base schema；
2. 使用下一个未占用 schema version（当前计划 `20260717`）增加 SQLite/PostgreSQL/MySQL migration；
3. 新增 Goal 5 的三张事务表和 DevicePlaybackState 三个核心列；
4. 实现控制事务的 create/get/list/terminal compare-and-set；
5. 实现按 deadline 扫描 pending；
6. 实现 prepare 的长期 intent 幂等和单一非终态保护；
7. 实现 local intent 的 fingerprint/canonical result 重放；
8. 增加按 Context/epoch/version 升序读取 pending 的 store API；
9. 所有 terminal mutation 返回“是否首次改变”和 canonical terminal 内容；
10. serializer 不泄露内部 nonce、fingerprint 或数据库 ID。

### 完成门槛

- 三数据库 clean install 和从 `20260715` upgrade 均成功；
- 唯一键、deadline 索引和 Context/epoch 索引存在；
- 同一 terminal 并发写只有一个首次成功；
- migration 不生成或恢复任何历史 pending command。

## Goal 3：用 `playback.context.ensure` 替换 create

### 工作项

1. 新增 stable clientId 级 ensure 锁，并与 pair/Context 锁使用固定顺序；
2. 实现四种分支：返回当前 pair、重绑唯一离线旧 deviceSession、创建新 Context、冲突拒绝；
3. 当前 Context idle 且 ensure 带非空本地队列时，初始化同一 Context 并按矩阵递增 cursor；
4. 当前 Context 已非空时，ensure 返回服务端 canonical snapshot，不无版本覆盖队列；
5. 新 idle Context 使用服务端生成且不可复用的 playbackContextId；
6. ensure 成功自动订阅当前 Socket；
7. 新建或重绑在 direct response 后发送 bindings.changed；无变化不发送；
8. 多候选、旧 device 在线、角色/能力不符、deviceSession 不匹配时 fail-closed；
9. close 后同一 tombstone ID 不复用；
10. 移除 strict dispatcher 的 create 路径，并迁移仓库内调用者。

### 完成门槛

- 空队列也能创建、发现、订阅和 status 水合 Context；
- 同 stable clientId 不会产生第二个 active Context；
- ensure 幂等重放不重复 mutation 或 bindings.changed；
- Web/测试不再依赖 strict create。

## Goal 4：实现 prepare/prepared 状态机

### 工作项

1. controller 只可对唯一、active、idle Context 发起 prepare；
2. 验证 intentId、baseControlVersion、authority 在线和可选 initial queue；
3. ACK 只返回 preparing 或已 ready，不修改 Context cursor；
4. 只向当前 authority 原 Socket 路由一次 prepare command；
5. 同一 intentId 相同内容重放结算，不重复路由；不同内容 conflict；
6. 同一 Context 同时只允许一个非终态 prepare；
7. 合法 queue sync 把 Context 变非空后，可直接结算 ready；
8. authority prepared feedback 使用 event-confirmed 语义；
9. 10 秒到期：已非空则 ready，否则 prepare_timeout；
10. authority/binding/epoch 改变时结算 authority_changed；
11. ready terminal 不被迟到失败覆盖，只向原 authority 重放 canonical ready；
12. idle Context 的普通 control 继续返回 queue_required，不自动触发播放。

### 完成门槛

- prepare 本身不改 queue、state 或 cursor；
- success 前必须观察同一 Context 的 canonical queue 非空；
- timeout、重复、迟到、authority changed 和并发 queue sync 均有测试；
- controller 仍需用最新 controlVersion 单独发送 player.play。

## Goal 5：实现 applied cursor 和四种 `playback.update`

### 工作项

1. passive：只更新实际事实和 clientSeq，applied 必须等于 lastApplied；
2. remote committed：只允许当前 authority 按序结算匹配 pending，推进 applied；
3. remote failed：结算匹配 pending，不把失败版本标为 applied；
4. localUser committed：由服务端从 canonical controlVersion +1 分配新版本；
5. localUser 使用绝对 queueIndex/track/state/position 校验，客户端不得自带新 controlVersion；
6. 相同 local intent 重放首次 canonical update，不再次递增或 supersede；
7. applied 高于 canonical 返回 bad_request；
8. applied 低于 lastApplied 进入 Goal 9 的 source-only correction；
9. applied 等值允许 passive、匹配 pending failed 和相同 terminal 重放；
10. clientSeq 只来自 Windows feedback；settled 不创建或推进 clientSeq；
11. status deviceStates 同时输出 canonical controlVersion 和实际 appliedControlVersion；
12. broadcast 前先写入 DevicePlaybackState 和对应 terminal。

### 完成门槛

- accepted control 可长期保持 applied < control；
- 四种 update 不能混用字段；
- 同一 terminal 不重复改变状态；
- 实际状态只由合法 Windows update 或已保存 canonical correction 表达。

## Goal 6：远程控制接受、持久化和路由

### 工作项

1. 在 Context/pair 串行区验证 controller、唯一 Context、base cursors 和 authority 精确 Socket；
2. 在 mutation 前完成 authority send-buffer reservation；
3. 原子递增 controlVersion、写 accepted target、创建 pending transaction；
4. 保存 requestingClientId 和原 authority connection identity；
5. command 携带 `executionTimeoutMs`，默认 15000，可由部署配置调整为正整数；
6. watchdogDeadline 固定为 accepted/routed 时间加 `executionTimeoutMs + 2000`；
7. command 继续用 `sourceClientId` 表示命令来源，但 settled 禁止复用该字段；
8. 只向 authority 精确 sid 单播，不向 room 广播执行命令；
9. ACK 只在事务提交且 command 已可靠进入该 Socket 发送路径后返回；
10. emit/连接竞争失败时不改投新 Socket，不自动重试，并按 r11 fail-closed 结束已创建事务；
11. canonical target 状态可以向 subscribers 推送，但不能当作 committed；
12. queue.playItem、next、prev 保存足够的 accepted target，供失败后对账。

### 完成门槛

- 每个 accepted control 恰好一条 pending transaction；
- ACK、emit 成功和 target snapshot 都不会把事务标成 committed；
- command 的 nonce/epoch 属于执行 Windows，不属于请求 controller；
- authority 替换竞争中旧命令不会发到新物理 Socket。

## Goal 7：成功、失败、本地操作和依赖失败结算

### 工作项

1. remote committed 原子写 committed、更新 applied 和实际状态，再广播 canonical update；
2. remote failed 原子写 failed，并在需要时用新的 Context version/Queue revision 恢复实际 snapshot；
3. failed reconciliation 不递减或重用 controlVersion；
4. localUser accepted 时保存 supersededThroughControlVersion；
5. 只把上界以内仍 pending 的事务写成 superseded；
6. queue.playItem/next/prev 根命令失败时，在同一事务中找出全部更高 pending；
7. 将每个依赖事务写成 failed/dependency_failed，dependsOn 指向根失败版本；
8. 先提交全部 terminal，再按 commandControlVersion 升序逐条 settled；
9. 一条失败不能用一个 settled 表示整个版本区间；
10. execution_unknown 不证明根命令失败，不触发 dependency_failed cascade；
11. actual snapshot 按 lastApplied 对账，不把未执行 target 保留成事实。

### 完成门槛

- 47 失败、48/49 依赖失败时，数据库有三条明确 terminal；
- 48 和 49 各收到一条 settled，顺序稳定；
- controlVersion 保持最高已分配值；
- Flutter 不需要根据 `(47,49]` 猜 terminal。

## Goal 8：实现 server-only `playback.control.settled`

### 工作项

1. 建立唯一 server-side builder，业务代码不能手写 settled payload；
2. builder 读取已持久化 terminal 和发送时 canonical control/applied cursor；
3. 固定 `status:'failed'`，errorCode 只允许 dependency_failed/execution_unknown；
4. dependency_failed 必带 dependsOn，unknown 禁止该字段；
5. requestingClientId 从原控制事务读取；
6. 每个 recipient 单独注入当前 nonce/epoch；
7. recipients 包含合法 Context recipients，以及仍在线的原 authority Windows Socket；
8. 原 authority 已断线或被替换时，不把 settled 补给新 Socket；
9. settled 发送失败不回滚 terminal；失败 recipient 按 strict critical push 规则断开/重水合；
10. 相同事务相同 settled 可安全重复发送；不同内容记录协议冲突且不覆盖首次 terminal；
11. settled 不修改 Context、DevicePlaybackState、queue、clientSeq 或 request cache；
12. 日志记录 context/epoch/version/error/recipient，不记录队列和敏感连接内容。

### 完成门槛

- 任何 settled 都能在数据库查到先前已提交的 terminal；
- payload 与 r11 字段表完全闭合；
- requestingClientId 始终是原 controller；
- authority Windows 在线时收到，换 Socket 后不补发。

## Goal 9：watchdog、断线、替换、重启和迟到反馈

### 工作项

1. 建立可取消的 per-transaction timer，外加数据库 deadline sweep 作为恢复保护；
2. 17 秒默认 watchdog 只执行 pending -> failed/execution_unknown；
3. watchdog 不生成 playback.update、execution_timeout 或 clientSeq；
4. authority disconnect 时按原 connection identity 批量 unknown；
5. Socket replacement 在新连接接管前先 unknown 旧连接 pending；
6. 服务启动时先把残留 pending 原子写成 execution_unknown，再接受新 strict 控制；
7. 完整重启不补发历史 settled，因为所有物理 Socket 已变化；
8. reconnect/ensure 后不查询 pending 并重新发送 command；
9. Windows 合法 execution_timeout 只通过 remoteCommand failed feedback 进入；
10. 服务端验证该反馈来自原 authority、匹配 pending 且在允许时序内，但不自行声称 Windows 已取消；
11. stale applied feedback 不写库、不广播，向原请求 Socket返回当前 passive canonical update；
12. source-only correction 进入该 requestId 的 event-confirmed cache，重复请求只重放给原 Socket；
13. 同一旧版本但不同 terminal 内容返回 conflict，不能用 correction 覆盖 terminal。

### 完成门槛

- 15000/17000 的计时边界有可控时钟测试；
- watchdog、disconnect、replacement、restart 全部得到 unknown，不出现 timeout；
- next/prev/seek 不在 reconnect 后自动执行；
- 旧 feedback 不影响 Android 或更高 applied 状态。

## Goal 10：Handoff、idle Context 和仓库内 Web 适配

### 工作项

1. Handoff 切换到 target 前查询 target pair 的 active Context；
2. target 只有 idle Context 且无非终态 prepare 时，在同一事务中先 tombstone 该 idle Context；
3. target Context 非 idle、prepare 未结束或无法原子退休时，在 authority switch 前 conflict；
4. Handoff commit 后旧 authority 的 pending/prepare 按契约终止，不进入新 authority；
5. target pair 始终最多暴露一个 active Context；
6. bindings.changed 覆盖被退休 idle Context 和 transferred Context 的 pair 变化；
7. 仓库内 Web strict 代码/fixtures 从 create 改为 ensure；
8. Web 必须支持 idle response，不能伪造歌曲或 index；
9. 保留 legacy Emo endpoint 和 HTTP bindings 行为，不把 2.4.0 shape倒灌到 legacy；
10. Follow/Broadcast 运行新 Context serializer 后做组合回归。

### 完成门槛

- target idle Context 的退休和 transferred binding 是一个原子结果；
- 任意失败分支不出现两个 active Context；
- Web strict 测试不再发送 create；
- legacy/Web 非 strict 行为无回归。

## Goal 11：自动化、数据库和并发验证

### 必测模块

- `tests/base/test_emo_strict_v2_contract.py`；
- `tests/base/test_emo_strict_v2_manifest.py`；
- `tests/base/test_emo_protocol_metadata.py`；
- `tests/base/test_emo_ws_store.py`；
- `tests/base/test_emo_strict_v2_core.py`；
- `tests/base/test_emo_strict_v2_handoff.py`；
- `tests/base/test_emo_strict_v2_safety.py`；
- `tests/base/test_emo_schema_migration.py`；
- `tests/base/test_emo_web_strict_v2.py`；
- 建议新增 `tests/base/test_emo_strict_v2_control_transactions.py`；
- 建议新增 `tests/base/test_emo_strict_v2_prepare.py`；
- 建议新增 `tests/base/test_emo_strict_v2_timeouts.py`。

### 必测场景

1. ensure 的 idle、非空、重绑、冲突和幂等；
2. prepare 的成功、失败、10 秒到期和迟到结果；
3. 四种 playback.update 的正反 schema；
4. applied/canonical cursor 单调性；
5. local intent 重放和内容冲突；
6. remote committed/failed/superseded；
7. dependency cascade 原子性、逐版本 settled 和发送顺序；
8. settled 的字段闭合、requestingClientId 和 recipient；
9. 15 秒 Windows feedback 与 17 秒 server watchdog 的竞态；
10. watchdog、disconnect、replacement、restart unknown；
11. late callback/feedback 不覆盖新状态；
12. source-only correction 不广播；
13. Handoff target idle retirement；
14. 多 Context/pair 并发和锁顺序；
15. SQLite/PostgreSQL/MySQL clean install、upgrade 和索引/约束；
16. request cache、send-buffer full、emit failure 和断连回归；
17. Follow/Handoff/Broadcast/device volume/legacy/Web 回归。

### 建议命令

```bash
python -m unittest tests.base.test_emo_strict_v2_contract
python -m unittest tests.base.test_emo_protocol_metadata
python -m unittest tests.base.test_emo_ws_store
python -m unittest tests.base.test_emo_strict_v2_prepare
python -m unittest tests.base.test_emo_strict_v2_control_transactions
python -m unittest tests.base.test_emo_strict_v2_timeouts
python -m unittest tests.base.test_emo_strict_v2_core
python -m unittest tests.base.test_emo_strict_v2_handoff
python -m unittest tests.base.test_emo_strict_v2_safety
python -m unittest tests.base.test_emo_strict_v2_manifest
python -m unittest tests.base.test_emo_schema_migration
python -m unittest tests.base.test_emo_web_strict_v2
python -m unittest tests.base.test_emo_ws
python -m unittest
```

如果建议的新模块未新增，替换为最终实际 dotted path。

### 完成门槛

- 所有时间测试使用 fake clock，不依赖真实等待 15/17 秒；
- 并发测试使用 barrier 和超时，不能只检查最终数据库值；
- 三数据库 migration 证据一致；
- 全量 unittest 通过。

## Goal 12：Conformance freeze 和真实联调

### 工作项

1. 最终重新计算 r11 contract SHA-256；
2. 更新 `STRICT_V2_CONTRACT_SHA256`、conformance JSON 和 fixture manifest；
3. REQ-001 至 REQ-045 全部映射到实际自动化测试；
4. 重新采集 Core、Follow、Handoff、Broadcast 的组合回归；
5. 记录唯一 serverBuildCommit、contract hash、测试命令和结果；
6. 服务端 code conformance 完成后才允许 code readiness true；
7. production 开关仍保持关闭，直到 Windows 硬超时验收通过；
8. Windows 验收必须模拟：加载超过 15 秒、lease 失效、17 秒以后 callback 返回，但不能切歌、
   committed 或覆盖新状态；
9. Flutter 验收必须证明 settled 按三字段 key 幂等，旧 settled 不回滚更高实际状态；
10. 保存双客户端日志到 `docs/verification/emosonic_strict_v2_r11/<serverBuildCommit>/`。

### 完成门槛

- manifest、validator、descriptor、contract hash、evidence 和 commit 一致；
- 服务端 REQ-001 至 REQ-045 均有通过证据；
- Windows 未通过 lease/callback 测试时，不允许 production 声称完整 r11 Core ready；
- Android/Flutter、Windows、服务端日志可按 Context/epoch/controlVersion 对齐；
- 未自动执行 production rollout。

---

## 七、REQ-001 至 REQ-045 覆盖表

| 要求 | 主要 Goal |
| --- | --- |
| REQ-001—REQ-009 | Goal 1、Goal 11：结算、协商、provenance、授权、失败闭合 |
| REQ-010—REQ-016 | Goal 1、Goal 3、Goal 6：cursor、幂等、closed schema、角色和能力 |
| REQ-017—REQ-022 | Goal 2、Goal 9、Goal 11：持久化、发送顺序、重启、限额和事件重放 |
| REQ-023—REQ-026 | Goal 3、Goal 10、回归：discovery、binding、pair lock、device volume |
| REQ-027 | Goal 3：player startup ensure |
| REQ-028 | Goal 1、Goal 3：idle Context 闭合 shape |
| REQ-029—REQ-030 | Goal 4：prepare before play、idle control fail-closed |
| REQ-031 | Goal 10：Handoff target idle Context 原子退休 |
| REQ-032—REQ-034 | Goal 5、Goal 6：远程事务、canonical/applied、applied 单调性 |
| REQ-035—REQ-036 | Goal 5：local user 版本分配和 intent 幂等 |
| REQ-037—REQ-039 | Goal 7：失败对账、supersede、dependency failure |
| REQ-040—REQ-041 | Goal 6、Goal 9：执行期限、unknown、禁止重放 |
| REQ-042 | Goal 9：late feedback source-only correction |
| REQ-043 | Goal 7、Goal 8：settled identity、写库后发送、顺序和幂等 |
| REQ-044 | Goal 9、Goal 12：Windows timeout readiness gate |
| REQ-045 | Goal 8、Goal 9、Goal 12：Windows 收 settled、旧 Socket 不补发 |

完整编号核对：`REQ-001`、`REQ-002`、`REQ-003`、`REQ-004`、`REQ-005`、`REQ-006`、
`REQ-007`、`REQ-008`、`REQ-009`、`REQ-010`、`REQ-011`、`REQ-012`、`REQ-013`、
`REQ-014`、`REQ-015`、`REQ-016`、`REQ-017`、`REQ-018`、`REQ-019`、`REQ-020`、
`REQ-021`、`REQ-022`、`REQ-023`、`REQ-024`、`REQ-025`、`REQ-026`、`REQ-027`、
`REQ-028`、`REQ-029`、`REQ-030`、`REQ-031`、`REQ-032`、`REQ-033`、`REQ-034`、
`REQ-035`、`REQ-036`、`REQ-037`、`REQ-038`、`REQ-039`、`REQ-040`、`REQ-041`、
`REQ-042`、`REQ-043`、`REQ-044`、`REQ-045`。

---

## 八、预计文件改动范围

| 文件 | 计划内容 |
| --- | --- |
| `specs/emosonic_strict_v2_socketio_server_contract.md` | 提升为 r11 权威契约 |
| `supysonic/emo/strict_v2_contract.py` | 2.4.0 action、四种 update、idle、settled closed schema |
| `supysonic/emo/ws.py` | ensure、prepare、control transaction、settled、watchdog 和 correction |
| `supysonic/emo/ws_store.py` | 新事务 store、applied cursor、幂等和原子 terminal |
| `supysonic/emo/ws_state.py` | 原 authority Socket 身份、disconnect/replacement 清理 |
| `supysonic/emo/strict_v2_runtime.py` | deadline scheduler/sweep 和启动恢复 |
| `supysonic/emo/protocol_metadata.py` | 2.4.0 metadata 校验 |
| `supysonic/emo/strict_v2_registration_descriptor.json` | protocolVersion 2.4.0 |
| `supysonic/emo/strict_v2_conformance.py/json` | r11 hash、REQ-045 和 readiness evidence |
| `supysonic/db_layer/emo.py` | 新表和 DevicePlaybackState 列 |
| `supysonic/schema/{sqlite,postgres,mysql}.sql` | r11 base schema |
| `supysonic/schema/migration/*/20260717.*` | 三数据库 r11 migration（若该 ID 仍空闲） |
| `tests/fixtures/emo_strict_v2/` | r11 action/schema/manifest fixtures |
| `tests/base/test_emo_*` | contract、store、Socket、超时、并发、migration 回归 |
| `supysonic/web.py`、templates/static（如实际引用 create） | 仓库内 Web strict ensure/idle 适配 |
| `docs/verification/emosonic_strict_v2_r11/` | 最终自动化和双客户端证据 |

不修改 Flutter、Windows、媒体库和私有运行配置。

---

## 九、建议提交顺序

每个中间提交都不能提前声称 r11 Core ready。

1. r11 contract promotion、inventory 和 readiness off；
2. 2.4.0 request/output schema、fixtures 和 negative tests；
3. 三数据库 migration、models 和 store 原语；
4. ensure、idle Context 和 Web strict 迁移；
5. prepare/prepared 状态机；
6. applied cursor、四种 playback.update 和 local intent；
7. remote control transaction、executionTimeoutMs 和 authority route；
8. remote terminal、failed reconciliation、supersede 和 dependency cascade；
9. server-only settled、watchdog、disconnect/restart 和 source-only correction；
10. Handoff idle retirement 和全 profile 回归；
11. r11 conformance freeze 和联调证据。

每个提交运行最窄测试；影响跨模块时再扩大到全量 `python -m unittest`。

---

## 十、主要风险和控制

### 风险 1：服务端把 accepted 当成已执行

控制：控制事务和 DevicePlaybackState 分表；ACK/emit/target snapshot 不能写 committed。

### 风险 2：17 秒伪造 Windows timeout

控制：server watchdog 的唯一 errorCode 是 execution_unknown；execution_timeout 只接受合法 Windows
failed feedback。

### 风险 3：settled 先推送后写库

控制：统一 compare-and-set store API；emit helper 只接受已提交 terminal，不接受 pending 对象。

### 风险 4：依赖失败只发一个范围通知

控制：数据库逐事务 terminal；发送列表按版本排序，一条记录构造一条 settled。

### 风险 5：旧 Socket 命令作用到新连接

控制：事务绑定 routing nonce/epoch；replacement 先 unknown 旧事务；禁止 reconnect replay。

### 风险 6：迟到 callback 或反馈覆盖新状态

控制：服务端 applied 单调校验和 source-only correction；Windows 硬超时由外部验收门槛保证。

### 风险 7：migration 误造 pending

控制：新事务表空建；旧 r8 状态只做确定性 applied 初始化，不猜测执行中的命令。

### 风险 8：Handoff 短暂暴露两个 Context

控制：退休 target idle Context 与安装 transferred binding 使用同一数据库事务和锁顺序。

### 风险 9：Web strict 继续发送 create

控制：同一提交迁移仓库内 Web fixtures/调用点，服务端不保留 2.4.0 create fallback。

### 风险 10：contract、hash 和实现漂移

控制：manifest 测试读取真实 `specs/` bytes；最终只在唯一 committed build 上 freeze evidence。

---

## 十一、Definition of Done

只有以下全部满足，服务端 r11 实施才完成：

1. `specs/` 为 r11，注册 metadata 为 `2.4.0`；
2. `playback.context.create` 已退出 2.4.0 strict action surface；
3. ensure 能创建/重绑唯一 idle 或 queue-backed Context；
4. idle Context 的所有 request/response/push/restart shape 闭合；
5. prepare/prepared 10 秒状态机、幂等和 authority change 完整；
6. 每条远程控制有唯一持久化事务；
7. ACK 只表示 accepted/routed；
8. status 和 update 能同时表达 canonical 与 applied cursor；
9. 四种 playback.update 严格校验并持久化；
10. local intent 重放不重复分配版本；
11. remote failed 能恢复实际 snapshot 而不回退 controlVersion；
12. dependency cascade 原子写库并逐版本升序 settled；
13. settled 只由服务端发送、使用 requestingClientId、先写库后发送；
14. settled 不改实际播放状态、不生成 clientSeq；
15. executionTimeoutMs 默认 15000，watchdog 默认 17000；
16. server watchdog 只能生成 execution_unknown；
17. disconnect/replacement/restart 后不自动重放 pending；
18. 原 Windows 在线时收到 settled，新 Socket 不补收历史 settled；
19. stale applied feedback 不写库、不广播，只 source-only correction；
20. Handoff target idle Context 原子退休；
21. SQLite、PostgreSQL、MySQL clean install 和 upgrade 通过；
22. REQ-001 至 REQ-045 全部有测试映射和通过证据；
23. Core、Follow、Handoff、Broadcast、device volume、legacy 和 Web 回归通过；
24. 全量 `python -m unittest` 通过；
25. contract hash、manifest、descriptor、conformance 和 serverBuildCommit 一致；
26. Windows 硬超时迟到 callback 验收通过前，production 不声称完整 r11 ready；
27. 未自动执行 production rollout。

---

## 十二、执行检查表

- [x] Goal 0：提升 r11 权威契约并关闭中间 readiness
- [ ] Goal 1：更新 2.4.0 action inventory 和 closed schema
- [ ] Goal 2：完成三数据库 migration 和 store 原语
- [ ] Goal 3：用 ensure 替换 create，支持 idle Context
- [ ] Goal 4：实现 prepare/prepared
- [ ] Goal 5：实现 applied cursor、四种 update 和 local intent
- [ ] Goal 6：实现远程控制事务、期限和精确路由
- [ ] Goal 7：实现 committed/failed/superseded/dependency 结算
- [ ] Goal 8：实现 server-only settled
- [ ] Goal 9：实现 watchdog、断线、重启和 source-only correction
- [ ] Goal 10：完成 Handoff idle retirement 和 Web 适配
- [ ] Goal 11：完成自动化、并发和三数据库验证
- [ ] Goal 12：完成 conformance freeze 和真实联调
- [ ] 确认 Definition of Done 全部满足
