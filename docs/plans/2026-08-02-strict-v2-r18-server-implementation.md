# Emosonic Strict-v2 r18 服务端实施主计划

> 面向执行模型：Luna
>
> 计划性质：冻结契约的代码实施计划，不是产品设计或契约审计
>
> 目标仓库：`jsdfhasuh/Emosonic-Server`
>
> 工作分支：`agent/strict-v2-r5-server-adaptation`
>
> 起始实现基线：`7abed609b386ff1dcaeeee65c89b8cff95dd0e0c`
>
> 冻结文档基线：`08dc67a2a00a5f80311af02fabfa56a5ddf4bb51`
>
> 冻结契约：`2026-08-01-r18` / `protocolVersion 2.8.0`
>
> 建议落库路径：`docs/plans/2026-08-02-strict-v2-r18-server-implementation.md`

## 1. 执行结论

本计划把服务端实现拆成可独立验收的阶段。当前顺序为 `Phase 0 → Phase 1A → Phase 2 → Phase 1B → Phase 3A/3B/3C → Phase 4 → Phase 5 → Phase 6 → Phase 7`。**一次 Luna 会话只执行一个 Phase 或一个明确的子阶段**，不得把多个阶段一次性交给 Luna。

每个 Phase 都必须遵循同一门禁：

1. 确认分支和起始 HEAD；
2. 阅读本 Phase 指定的契约章节、现有代码和测试；
3. 先记录现状和失败基线；
4. 只修改本 Phase 允许的文件范围；
5. 先运行定向测试；
6. 再运行完整服务端测试；
7. 运行 `git diff --check`；
8. 仅在所有本地门禁绿色时提交并推送；
9. 等待远端 CI 的 Python 3.9、Python 3.12、Database Migrations、Docker Build；
10. 远端 CI 绿色后才允许开始下一 Phase。

任一门禁失败时，Luna 必须停止当前 Phase，保留未提交改动，报告精确命令、失败测试、堆栈和涉及文件。不得带着红色状态继续后续 Phase。任何 server→client output validator 只有在其 canonical source、runtime producer 和对应测试同一阶段闭合后才允许 active；不得先收紧 validator，再用 `None`、空串、当前 Socket、当前 authority、latest session 或 placeholder 补值。

## 2. 权威来源与当前基线

### 2.1 唯一产品语义来源

以下 20 个文件共同构成唯一权威契约：

- `specs/emosonic_strict_v2_socketio_server_contract.md`
- 入口文件列出的 `specs/emosonic_strict_v2_contract/**` 全部 19 个分卷

还必须阅读：

- `AGENTS.md`
- `docs/plans/2026-08-01-strict-v2-r18-contract-finalization.md`
- `docs/verification/emosonic_strict_v2_r18_requirement_mapping.md`

如果旧实现、旧 ADR、旧测试、旧 fixture、旧计划和冻结契约冲突，修改实现、测试或派生材料。不得反向修改冻结契约。

### 2.2 已核实的 GitHub 基线

- 分支 HEAD：`08dc67a2a00a5f80311af02fabfa56a5ddf4bb51`
- Draft PR：`#6`
- PR 规模：78 个提交、171 个文件，约 `+75,643 / -1,984`
- PR 必须继续保持 Draft；不得合并、标记 Ready、force-push 或重写历史。
- 当前冻结入口实际 SHA-256：

  `116a36e2359b2f6c525f340187c7daa6fd0e476241c0b190f1145b88b0e6e46f`

- 当前派生代码和 manifest 中仍存在旧 SHA-256：

  `2580851b2059d1d80b2059fe37f2b6e16d156787b741035436009baa3e8af90e`

### 2.3 已核实的 CI 基线

GitHub Actions run `30695817240`：

| Job | 基线结果 |
|---|---|
| Python Tests 3.9 | 失败 |
| Python Tests 3.12 | 失败 |
| Database Migrations | 通过 |
| Docker Build | 通过 |

Python 3.9 和 3.12 都运行 1665 项测试，结果一致：

```text
FAILED (failures=4, errors=2, skipped=3)
```

当前六个失败/错误来自冻结身份和覆盖清单的机械失配，不是 Follow/Handoff 状态机失败：

- `test_frozen_contract_matches_code_and_manifest_hash`
- `test_authoritative_contract_covers_every_r18_requirement`
- `test_evidence_collector_accepts_local_test_only_candidates`
- `test_evidence_collector_binds_clean_tree_to_exact_commit`
- `test_evidence_collector_rejects_premature_formal_readiness`
- `test_packaging_verifier_is_bound_to_r18_protocol_identity`

其中：

- frozen hash 仍固定为旧值；
- requirement coverage 仍只接受 REQ-001—REQ-067；
- fixture/evidence 工具仍主要覆盖 REQ-001—REQ-045；
- evidence collector 因旧 hash 提前失败，尚未执行到原本要测试的 readiness 分支。

不得追逐 Codecov token 信息；它不是当前 Python job 的失败根因。

### 2.4 已核实的代码起点

Luna 不得假设服务端“从零实现”，也不得重写已有 store。当前分层与已知差距如下：

| 层 | 主要文件 | 已有基础 | 最终 r18 关键差距 |
|---|---|---|---|
| Wire schema | `supysonic/emo/strict_v2_contract.py` | closed request/output validator | close、settled、Follow ACK、Handoff 全量 shape、four-cursor error 未闭合 |
| Capability | `supysonic/emo/strict_v2_readiness.py` | profile 协商和 dev/test 开关 | Follow/Handoff 主要只检查 player/canPlay，复合能力与运行时 gate 不完整 |
| Socket runtime | `supysonic/emo/ws.py` | Strict-v2 handler、watchdog、profile 路由 | 文件很大；必须按 handler 小步修改，禁止一次混改全部 profile |
| Context/control store | `supysonic/emo/ws_store.py` | Context、control transaction、prepare、Handoff CAS 骨架 | dependency eligibility、exact requester pair、reconciliation、safe close 不完整 |
| Connection state | `supysonic/emo/ws_state.py` | SID/client/subscription 与旧 Follow/Handoff 缓存 | 只能保存可重建连接态，不能承担 durable safety semantics |
| Broadcast store | `supysonic/emo/broadcast_store.py` | fence、revision、delivery、feedback、terminal recovery、compaction 基础较完整 | 应增量补 action-aware restore、critical delivery、abandon/decommission，不得重写 |
| DB models | `supysonic/db_layer/emo.py` | Context、DeviceState、ControlTransaction、Prepare、Handoff、Broadcast | 缺最终 r18 的若干 exact-pair、lease、proof、outcome 字段/表 |
| DB schema/migration | `supysonic/db_layer/schema.py` 及三数据库 schema/migration | 当前 schema version `20260728` | 必须追加同语义的 SQLite/MySQL/PostgreSQL migration |

当前 `strict_v2_contract.py` 的直接旧 shape 包括：

- `playback.context.close` 只要求 `playbackContextId`；
- `playback.handoff.start` 缺 `targetDeviceSessionId`；
- `playback.ready` 缺必需 `deviceSessionId`，`handoffId` 仍可选；
- `playback.handoff.complete` 只有极少字段，缺完整 actual proof；
- Context snapshot validator 缺 `authorityDeviceSessionId`；
- output error 缺 `currentEpoch`；
- settled output 缺 `requestingDeviceSessionId`；
- `volumeState` 的 negotiated recipient 条件无法仅靠静态 validator 完成，必须在 serializer/emit 层按接收端裁剪。

当前 `EmoPlaybackControlTransaction` 已有部分字段，例如 requester client、dependency、accepted time、execution timeout、watchdog deadline；但仍缺 requester device、execution eligibility 和 reconciliation/audit 所需信息。当前 watchdog 以 acceptance 时间启动，必须改为 eligibility 后启动。

当前 Follow 主要依赖 `ws_state.py` 内存 relationship；服务端重启会丢失安全语义。最终 r18 必须以持久化 `FollowSafetyLease` 为安全权威，内存关系只能是可重建缓存。

当前 Handoff 已有“数据库事务内切 authority”的骨架；应保留并扩展，不得另写第二套切换路径。缺口主要是 exact target pair、durable fence/provisional state、完整 proof、fingerprint/replay 和 disconnect/restart 处理。

### 2.5 Phase 1 blocker audit checkpoint

本 checkpoint 是实施事实记录，不是部署证据、`serverBuildCommit`、`schemaHash` 或正式合规证据：

- Phase 0 已在 `7abed609b386ff1dcaeeee65c89b8cff95dd0e0c` 通过本地和远端门禁；
- 原 Phase 1 尝试没有提交；
- 审计时存在 4 个未提交 WIP 文件：
  - `supysonic/emo/strict_v2_contract.py`
  - `supysonic/emo/strict_v2_readiness.py`
  - `supysonic/emo/strict_v2_registration_descriptor.json`
  - `supysonic/emo/ws.py`
- WIP diff 为 `332 insertions`、`57 deletions`；
- WIP 已保存为 stash OID `bc7c734546898f1c801fcfc7f32a7f175a7c4392`；
- WIP binary diff SHA-256 为 `ed6ee061021cd4393eb2bfac5faa7ae580901e95f20d144f18da1988f4aa898d`；
- 62 项定向测试结果为 `6 failures`、`5 errors`；
- 其中前 10 项属于旧 schema、manifest、fixture 或断言基线未同步；
- 第 11 项暴露 requester settlement 缺少 durable canonical source；
- 原 Phase 1 未完成，Phase 2 尚未开始；
- 这些 WIP 和失败结果不能作为实现证据；stash OID 只用于本地 WIP 备份，不能作为部署信息、`serverBuildCommit`、`schemaHash` 或正式合规证据。

## 3. 永久禁止事项

以下限制适用于所有 Phase：

- 不修改 `specs/**` 中的冻结入口或 19 个分卷；
- 不对冻结 finalization plan 做产品语义修改；
- 不创建 r19，不把协议升级到 2.9.0；
- 不增加协议版本协商或 pre-freeze/legacy/session 兼容路径；
- 不修改 Flutter/Dart 客户端仓库；
- 不合并 PR #6，不修改 master，不 force-push，不重写历史；
- 不删除测试，不加 `skip`、`expectedFailure`、xfail，不减少预期 REQ 范围；
- 不把闭合 schema 改成开放 schema，不放宽 `additionalProperties`，不允许未知字段或 JSON `null` 混过验证；
- 不用宽泛异常捕获吞掉失败，不做恒真 validator，不修改 CI 路径或命令绕过失败；
- 不升级依赖来隐藏失败；
- 不伪造 test method、formal evidence、`schemaHash`、`serverBuildCommit`、设备证据或 `Verified` 状态；
- 不提前打开 production readiness/capability；
- 不简单把开发/测试环境的 `PROFILE_IMPLEMENTATION_READY` 全设为 false；显式 dev/test 便利开关必须继续可用，production 必须 fail-closed；
- 不做无关重构、全仓格式化、文件改名或依赖整理；
- 不新增 Follow feedback、control query、Broadcast 动态 membership、repeat/shuffle；
- 不用仅 `clientId` 的模糊路由替代 exact `clientId + deviceSessionId`；
- Handoff provisional `N+1` 绝不能复用普通 control transaction、dependency、watchdog 或 reconciliation；
- 数据库迁移只能追加，不能修改已经存在的旧迁移。

测试只允许在两种情况下修改：

1. 把明确的 pre-freeze 旧断言替换为冻结 r18 的精确断言；
2. 为新实现增加正例、缺字段、未知字段、非法 `null`、越界、并发、回滚、重启和 replay 测试。

## 4. 所有状态改变必须遵守的事务不变量

所有 Context、Control、Follow、Handoff 和 Broadcast 状态改变必须遵循：

```text
验证用户、资源、caller 和 recipient
→ 验证 current physical clientId + deviceSessionId + Socket
→ 按固定顺序取得 Context/pair 锁
→ 在锁内重新验证 cursor、fence、capability 和状态
→ 单个数据库事务内写 canonical state、terminal/outbox/tombstone
→ commit
→ commit 后才 emit/enqueue
```

硬性要求：

- emit/enqueue 成功不能充当数据库 commit 成功的证明；
- commit 失败不得留下内存状态、cursor、outbox 或 push 的部分副作用；
- error path 必须为零副作用，除契约明确要求的 terminal/reconciliation 记录外；
- retry/replay 必须使用契约指定的幂等键，不得重新推进 cursor；
- 取得锁之前和锁内都要验证 exact physical pair，避免 Socket replacement 竞态；
- 用户级、Context 级、pair 级锁顺序必须固定并写入测试，禁止死锁式反向获取。

## 5. 分阶段实施总览

| Phase | 目标 | 允许进入下一阶段的条件 | 建议提交 |
|---|---|---|---|
| 0 | 冻结身份和 CI 基线 | 已完成于 `7abed609`；本地和远端四 job 真实绿色 | `chore: align frozen r18 conformance baseline` |
| 1A | 共享的非持久化 request validation 基础、closed object 工具、静态 capability policy | 只有不提前激活不完整 runtime 的 schema/policy 测试绿色 | `feat: add strict-v2 phase1a validation foundations` |
| 2 | Core durable persistence foundation、startup recovery、restart-safe store API | requester/routed physical identity、eligibility、safe-close 基础和三数据库 migration/reload/upgrade 绿色 | `feat: add r18 persistence foundations` |
| 1B | 只接入已有 canonical source 的共享 live serializer/runtime policy | activation matrix 所有 action 的 source、producer、测试同阶段闭合 | `feat: activate strict-v2 live serializers` |
| 3A/3B/3C | Core safe close、settlement、reconciliation、queue terminal | 各子阶段独立定向、完整回归和远端四 job 绿色 | 分 3 个绿色提交 |
| 4 | Follow 服务端闭环 | Follow restart/reconnect/cleanup 门禁绿色 | 分 3 个绿色提交 |
| 5 | Handoff 独立 provisional lane | proof、原子 complete、disconnect/replay 绿色 | 分 3 个绿色提交 |
| 6 | Broadcast final-r18 restore/terminal/decommission | 新旧 Broadcast 全组合绿色 | 分 3 个绿色提交 |
| 7 | 跨 profile、重启、readiness、证据收口 | 远端全部绿色，mapping 只更新真实服务端证据 | `test: close r18 server conformance evidence` |

## 6. Phase 0：冻结身份与 CI 基线修复

### 6.1 目标

只修复冻结契约提交造成的派生身份、coverage 和 evidence 工具机械失配。**不得修改 runtime、数据库、wire schema 或任何业务状态机。**

### 6.2 允许修改的文件

优先限制在：

- `supysonic/emo/strict_v2_conformance.py`
- `supysonic/emo/strict_v2_conformance.json`
- `tests/fixtures/emo_strict_v2/manifest.json`
- `tests/base/test_emo_strict_v2_conformance.py`
- `tests/base/test_emo_strict_v2_manifest.py`
- `tests/base/test_emo_strict_v2_verification_scripts.py`
- `script/collect_emo_strict_v2_r7_evidence.py`
- `script/verify_emo_strict_v2_packaging.py`
- `script/verify_emo_strict_v2_ears.py`

如发现实际文件名不同，先用 `rg` 定位旧 hash、`REQ-067`、`REQ-045` 的全部引用，并在报告中解释额外文件。

### 6.3 精确步骤

1. 确认工作树干净；若有用户改动，停止并报告，不得覆盖。
2. 确认当前分支和 HEAD；如果 HEAD 已前进，记录新 HEAD 并重新读取 CI，不得 reset 回旧 SHA。
3. 使用仓库已有方式计算冻结入口 SHA-256，确认是 `116a36e...e46f`。
4. 将所有“冻结入口身份”的派生旧 hash 原子更新为新 hash。
5. 将权威契约 coverage 的预期范围从 REQ-001—067 更新为 REQ-001—090。
6. coverage parser 必须同时证明 REQ-068—REQ-090 所在的 11a/11b 权威分卷被入口引用；不得只把数字上限改成 90。
7. formal profile readiness、codeConformanceReady 和 evidence 继续保持 false/empty。
8. 不得给 REQ-068—REQ-090 填入不存在的测试方法。
9. `tests/fixtures/.../manifest.json` 中的 executable requirement evidence 不得仅为“数量到 90”而复制旧测试。Phase 0 可以继续只列有真实测试的方法；如新增独立 `contractRequirements` inventory，它只能是 001—090 的机械清单，必须与 evidence 明确分离，不得被解释为验证完成。
10. `verify_emo_strict_v2_ears.py` 的完整 001—090 可执行证据扩展留到相关 Phase 的真实测试落地；Phase 0 只需消除旧 hash 和契约 inventory 的错误，不伪造覆盖。
11. 逐个复现并修复当前六个失败/错误。

### 6.4 测试门禁

先运行六个失败所属的三个定向测试模块，再运行：

```bash
python -m unittest
python -m unittest tests.net.suite
git diff --check
```

若仓库的 CI 使用 coverage 包装，则额外运行与 `.github/workflows/tests.yaml` 完全一致的命令。不要自行增加 Ruff、Pycodestyle、Black、Mypy；仓库没有这些官方门禁。

### 6.5 提交与停止条件

仅在本地门禁全部绿色时提交：

```text
chore: align frozen r18 conformance baseline
```

推送后等待远端四个 job。任一失败则停止，不进入 Phase 1A。

Phase 0 当前状态：已完成。实现基线为 `7abed609b386ff1dcaeeee65c89b8cff95dd0e0c`，本地和远端 Python Tests 3.9、Python Tests 3.12、Database Migrations、Docker Build 均真实通过。后续实现不得把 Phase 0 的绿色结果解释为 Phase 1A、Phase 1B 或 Phase 2 的证据。

## 7. Phase 1A：共享非持久化基础

### 7.1 覆盖范围

- REQ-001—REQ-014
- REQ-023—REQ-030
- REQ-068—REQ-069
- 与 schema/readiness 有关的 acceptance 78—80

### 7.2 主要文件范围

- `supysonic/emo/strict_v2_contract.py`
- `supysonic/emo/strict_v2_readiness.py`
- `supysonic/emo/strict_v2_registration_descriptor.json`
- 仅在公共 serializer/policy 工具确有必要时读取 `supysonic/emo/ws.py`；本阶段不得激活不完整 output producer
- `tests/fixtures/emo_strict_v2/**`
- contract/readiness/manifest/fixture 的纯 schema/policy 测试

此 Phase 不得实现 Core、Follow、Handoff、Broadcast 的完整业务状态机，不得新建业务表，也不得激活缺少 canonical runtime source 的 server→client output validator 或 producer。

### 7.3 允许落地的共享基础

- 提取并测试 closed object、类型、范围、enum、one-of、unknown-field、显式 `null` 和 bool/int 区分等公共验证工具；
- 只启用与当前 handler 已经一致的 request validation 基础；若最终 request shape 会让旧 handler 接受后执行不完整语义，该 action validator 留到对应 domain phase；
- 实现静态 Follow/Handoff/Broadcast composite capability policy，不把静态协商当作 current physical Socket、clock、freshness 或 runtime gate；
- 保持 registration/capability descriptor closed，并拒绝客户端自报 requester identity；
- 为上述共享基础增加最小正例、缺字段、未知字段、非法 `null`、类型错误、bool 冒充 int、enum、范围和 one-of 负例；
- 任何 output validator 只允许作为未激活的 schema definition 存在，不能被 `_emit_message`、direct response 或 recipient serializer 使用。

### 7.4 Capability 不变量

- Follow/Handoff/Broadcast 的静态 capability 只是第一层；current physical Socket、player role、canPlay/canPause/canSeek、全部合法 rate、clock/freshness 等运行时条件不得被静态字段替代；
- 不能把 `PROFILE_IMPLEMENTATION_READY` 全关掉来规避测试；
- production fail-closed；明确的 dev/test 模式仍可按 AGENTS 和契约工作；
- 关闭新 session admission 不能阻断已有 Broadcast terminal drain。

### 7.5 测试门禁

每个 action 至少包含：

- 最小合法正例；
- 完整合法正例；
- 每个 required 字段缺失；
- 每个 forbidden/unknown 字段；
- optional 字段显式 null；
- enum/范围/one-of 冲突；
- request validator、closed object 工具和未激活 output schema inventory 的边界检查；最终 output 对称测试留给 activation matrix 对应阶段。

定向 Phase 1A schema/readiness/policy 测试通过后，再运行完整 unittest、net suite、`git diff --check` 和远端四 job。Phase 1A 绿色不等于任何最终 output shape 已经可发出。

建议提交：

```text
feat: add strict-v2 phase1a validation foundations
```

### 7.6 Action activation matrix

矩阵中的“active validator 最早阶段”指 validator、canonical source、runtime producer 和测试可以同阶段闭合的最早阶段；在此之前只能保留未激活的契约资料，不能由占位值满足校验。

| action/behavior | active validator 最早阶段 | durable source 阶段 | runtime producer 阶段 | 测试阶段 | 当前状态 |
|---|---|---|---|---|---|
| Context `authorityDeviceSessionId` | Phase 1B | 现有 live Context canonical 字段 | Phase 1B live serializer | Phase 1B | 基线字段可读；WIP 未激活 |
| live `system.error` four cursors | Phase 1B | 现有 live Context canonical cursors | Phase 1B | Phase 1B | 旧 output shape；不得从用户或当前 Socket 补值 |
| closed/tombstone `system.error` four cursors | Phase 3A | Phase 3A close tombstone | Phase 3A ACK/replay | Phase 3A | 尚无 durable closed source |
| `playback.context.close` | Phase 3A | Phase 3A tombstone/outcome | Phase 3A close admission/replay | Phase 3A | 旧 request 只有 Context ID |
| `executionEligibleAtMs` | Phase 3B | Phase 2 transaction | Phase 3B admission/dependency/watchdog | Phase 3B | 当前只可靠保存 accepted/deadline 旧语义 |
| `dependsOnControlVersion` | Phase 3B | Phase 2 transaction | Phase 3B ordinary control routing | Phase 3B | 不能在最终 dependency 未落库前激活 |
| `playback.control.settled` | Phase 3B | Phase 2 requester/routed identity and terminal fields | Phase 3B settlement/recipient routing | Phase 3B | 当前 WIP 曾提前读取不存在的 requester session |
| Follow frozen baseline ACK | Phase 4B | Phase 4A `FollowSafetyLease` | Phase 4B | Phase 4A/4B | 当前 handler 仍发送 action-only ACK |
| Handoff prepare/commit/status/release/complete proof | Phase 5B/5C | Phase 5A/5B durable fence/provisional/outcome | Phase 5B/5C | Phase 5A/5B/5C | 现有骨架缺 exact physical generation/full proof closure |
| `restore_in_progress` | Phase 6A | Phase 6 restore state | Phase 6A negative prepared/restore output | Phase 6A | 不在 Phase 1A/1B 提前激活 |
| `device.list.volumeState` capability trimming | Phase 1B | 现有 volume state + negotiated capability | Phase 1B recipient serializer | Phase 1B | 只向 `remoteVolumeControl=true` recipient 输出 |
| recovery abandon/decommission | Phase 6C | Phase 6 terminal/decommission tombstone | Phase 6C | Phase 6B/6C | 现有 Broadcast 基础尚未闭合永久 exact-pair 语义 |
| Core prepare independence from Handoff capability | Phase 1B | 现有 capability/readiness source | Phase 1B Core gate/serializer | Phase 1B | 基线 runtime 仍有 `playbackPrepare` gate，不能被静态 policy 掩盖 |

核心规则：任何 active output validator 都必须和 canonical source、runtime producer、recipient routing 及对应测试在同一阶段闭合。不得使用 `None`、空串、默认 cursor、当前 recipient 身份、当前 authority、latest session、raw SID 或其他 placeholder 临时填充。

## 8. Phase 2：Core durable persistence foundation

### 8.1 目标

在写最终业务 handler 或 output producer 前建立后续阶段共用的 canonical durable source、锁、restart-safe store API、startup recovery、bounded retention 和 migration 基础。禁止先用内存临时字段“把状态机跑通”。Phase 2 只建立 durable source，不激活 `playback.control.settled`、Follow 或 Handoff 的最终 producer。

### 8.2 主要文件范围

- `supysonic/db.py`
- `supysonic/db_layer/schema.py`
- `supysonic/db_layer/emo.py`
- `supysonic/emo/ws_store.py`
- `supysonic/emo/broadcast_store.py`
- SQLite/MySQL/PostgreSQL base schema
- 三套同版本 forward migration
- migration/store/restart/rollback 测试

### 8.3 必须持久化的基础

- Context exact authority pair；
- ordinary control admission 必须原子保存 requester 的 `clientId`、`deviceSessionId`、`connectionNonce`、`connectionEpoch`；
- ordinary control admission 必须原子保存 routed authority 的 `clientId`、`deviceSessionId`、`routedConnectionNonce`、`routedConnectionEpoch`；
- 客户端不得自报 requester identity；不得持久化 raw Socket SID；上述字段必须来自 admission 时已认证并在锁内重验的 physical Socket；
- requester nonce/epoch 和 routed nonce/epoch 只用于服务端 physical connection matching，不进入 wire payload；
- closed tombstone 的 `closedFrom*`、final 四 Cursor 和 durable ACK/error outcome；
- ordinary control transaction 的 dependency、`dependsOnControlVersion`、`executionEligibleAtMs`、watchdog 和 terminal 字段；
- `executionEligibleAtMs`，不能仅用 acceptedAt 推算；
- terminal-gap reconciliation record；
- bounded retention、查询索引和清理条件；
- startup recovery 能够在任何 admission、dispatch、watchdog、outbox drain、profile recovery 或 readiness 之前恢复上述 Core durable source。

建议字段清单必须在编码前与冻结分卷逐项核对；字段名可服从仓库现有命名规范，但语义不得缺失：

| 领域 | 至少需要的持久化语义 |
|---|---|
| Control | requester 四元组、routed authority 四元组、`depends_on_control_version`、`execution_eligible_at_ms`、effective/deadline、terminal kind/code/message、direct dependency、reconciliation linkage |
| Close | request fingerprint、`closedFromEpoch/Version`、final epoch/version/queue/control cursor、durable ACK/error outcome |
| Recovery | startup recovery marker、旧 pending 的 safe terminal outcome、幂等重启记录、bounded retention 和清理条件 |

FollowSafetyLease、Handoff provisional/fence/standby、Broadcast restore/decommission 的 domain-specific durable state 在各自 Phase 4/5/6 建立和激活；Phase 2 只能提供其依赖的 transaction/lock/reload 基础，不提前发送最终 wire output。

### 8.4 Startup recovery 与旧行规则

startup recovery 必须在以下行为之前完成：

- 新 admission；
- pending dispatch；
- watchdog；
- outbox drain；
- profile 恢复；
- 对外 readiness。

旧行处理必须遵守：

- 不得根据 `clientId`、authority、当前 Socket、`DevicePlaybackState` 或“唯一在线设备”推断缺失 identity；
- 不得使用空串、默认 UUID、`unknown`、`0` 或其他 sentinel；
- 旧 terminal 行原样保留；
- 缺完整 physical generation 的旧 pending 根事务以内部 `execution_unknown` 安全终止；
- 其依赖链递归为 `dependency_failed`；
- 旧 pending 不再 eligible、不挂新 watchdog、不重新 dispatch、不推进 canonical cursor；
- 重复启动必须幂等；
- 缺 requester exact pair 的 legacy outcome 不生成非法 settled wire payload，只保留 durable terminal/reconciliation 状态；
- migration 测试必须包含非空旧 transaction，不能只测空表升级。

### 8.5 迁移规则

- 选择一个高于当前 `20260728` 的新 schema version；三数据库使用相同语义和版本；
- 只增加新 migration，不改旧 migration；
- 新安装 base schema 与旧库逐步迁移后的最终结构必须等价；
- nullable/default 只用于兼容升级过程，runtime 写入后必须满足最终不变量；
- 对 SQLite、MySQL、PostgreSQL 都验证升级、重复启动、rollback 和 schema version；
- 不连接或修改用户的真实媒体库/生产数据库。
- append-only migration 不得修改旧 migration；给既有行 backfill 时不得生成假的 `deviceSessionId`，也不得让旧 pending transaction 看起来仍可执行；无法证明执行结果的旧 pending 状态应按上述 restart/unknown 规则安全终结。

### 8.6 测试门禁

- 全新数据库建库；
- 从上一 schema version 升级；
- migration 重入/重复启动；
- 三后端字段、索引、约束语义一致；
- store round-trip；
- server restart/reload 不丢 cursor、terminal、lease、fence；
- 注入 commit/emit/enqueue 失败时无部分状态；
- bounded cleanup 不删除仍有安全含义的 Phase 2 terminal、legacy outcome 或 recovery marker；`cleanupRequired` lease 和永久 tombstone 的清理测试留在 Phase 4/6。

建议提交：

```text
feat: add r18 persistence foundations
```

## 8A. Phase 1B：共享 live serializer 与 runtime policy 接入

### 8A.1 覆盖范围

Phase 1B 只接入已经具有 canonical source、不会触发 tombstone 或 domain state machine 依赖的共享 live behavior：

- active Context 的 `authorityClientId + authorityDeviceSessionId`；
- 仅 live Context 能证明的 four cursor error enrichment；
- negotiated capability recipient trimming，包括 `remoteVolumeControl` 对 `device.list.volumeState` 的裁剪；
- Core prepare 不依赖 Handoff `playbackPrepare` capability；
- 已在 Phase 2 建立 durable source、且其最终 runtime producer 也在本阶段闭合的共享 serializer/policy。

### 8A.2 明确禁止提前激活的 output

以下行为必须留到对应 domain phase，不得在 Phase 1B 通过当前 Context、当前 authority、当前 recipient 或 latest session 拼装：

- closed/tombstone `system.error` replay；
- `playback.control.settled`；
- Follow frozen baseline ACK；
- Handoff prepare/commit/status/release/complete proof；
- Broadcast restore、abandon、terminal 或 decommission output。

### 8A.3 测试门禁

- 每个 active serializer 都要有 canonical source、recipient routing、最小/完整正例和缺字段、unknown、`null`、类型、范围、one-of 负例；
- 验证 Core prepare capability independence 不会绕过 current physical Socket、role、clock、freshness 或 rate gate；
- 先运行 Phase 1B 定向测试，再运行完整 unittest、net suite、`git diff --check`；
- 只有本地绿色才允许提交、推送并等待远端四 job。

建议提交：

```text
feat: activate strict-v2 live serializers
```

## 9. Phase 3：Core control、settlement、reconciliation、safe close 与 queue terminal

### 9.1 覆盖范围

- REQ-025
- REQ-032—REQ-038
- REQ-070—REQ-075
- acceptance 78—84

### 9.2 固定实施顺序

#### 3A：Exact pair、four cursor 与 safe close

- exact authority pair snapshot/routing；
- user-scoped error lookup 与 four cursor；
- close admission、fence、tombstone、ACK replay；
- tombstone 至少保存 user/tenant scope、`playbackContextId`、action domain、request fingerprint、`expectedEpoch/baseVersion`、`closedFromEpoch/closedFromVersion`、final epoch/version/queueRevision/controlVersion 和 durable ACK/error outcome；
- 原子区分 live Context、exact tombstone match、mismatched tombstone、neither 和 live+tombstone invariant breach；
- close 重试只有 exact fingerprint/cursor match 才能零写入重放原 outcome；不得按 `playbackContextId` 单独命中，不得向 foreign user 暴露存在性或 cursor；
- 不得把 `appliedControlVersion` 当成四 Cursor 之一，不得为信息不足的 closed row 伪造 replayable success，不得自行发明契约未规定的 TTL；
- close tombstone 与永久 device-decommission tombstone 分开建模。
- 在 Core handler 接入统一 gate 和固定锁序：排序后的 Context IDs → 排序后的 exact device pairs → profile resource key → DB transaction；不得在进程锁或 DB transaction 内执行 Socket emit。

绿色提交：

```text
feat: enforce r18 context identity and safe close
```

#### 3B：Control transaction 与 settlement

- admission 时确定 deterministic dependency；
- 只有 `queue.playItem/player.next/player.prev` 是 track-changing；
- dependent command hold；
- dependency terminal 后才设置 `executionEligibleAtMs`；
- timeout 从 eligibility 开始；
- watchdog = `eligibleAt + executionTimeoutMs + 2000`；
- 内部 transaction terminal outcome 与 wire status/errorCode 分开建模；内部可以记录 committed/failed/execution_unknown/dependency_failed，但 `playback.control.settled` wire payload 的 `status` 固定为 `failed`；
- `playback.control.settled.errorCode` 只允许 `execution_unknown` 或 `dependency_failed`，不得扩展 committed settled；ordinary remote command 的 committed/failed 继续使用 `playback.update`；
- recursive dependency cascade，每项记录直接依赖；
- admission 原子保存 requester `clientId/deviceSessionId/connectionNonce/connectionEpoch` 和 routed authority `clientId/deviceSessionId/routedConnectionNonce/routedConnectionEpoch`；客户端不得自报 requester 字段，不持久化 raw Socket SID；
- settlement 收件人集合严格由三条独立路径组成：当前 Context subscribers、完整匹配原 requester pair+nonce+epoch 的 physical Socket、完整匹配原 authority pair+routed nonce+routed epoch 的 physical Socket；最终按 SID 去重；
- 同一 client/device 的 replacement Socket 不得仅凭 requester 身份补收旧 settlement；后来成为 subscriber 时只能通过 subscriber 路径收到一次；
- unknown/failed 不伪造 `playback.update`。

绿色提交：

```text
feat: implement r18 control settlement
```

#### 3C：Reconciliation 与 queue boundary

- terminal-gap reconciliation 创建新的 reconciliation record R；
- 不改写旧 terminal；
- 原子更新 canonical Context/device actual；
- reconciliation 不进入 AudioExecutionLease；
- first-prev 重播第一个 distinct track；
- last-next 进入 stopped、保留 last index、position 0；
- natural terminal 只推进 version 一次，不推进 queue/control cursor；
- 不引入 repeat/shuffle。

绿色提交：

```text
feat: reconcile r18 terminal control gaps
```

### 9.3 Core 测试矩阵

- 依赖链 A→B→C 的 success、failed、unknown、dependency_failed；
- dependency 等待超过 execution timeout 但未 eligible 时不超时；
- effectiveAt 超过契约迟到阈值时 `effective_at_missed`；
- disconnect、Socket replacement、server restart、watchdog 四类 unknown；
- inline failed reconciliation 与 passive later reconciliation；
- localUser/supersede/authority change 并发；
- close 与 pending control/Follow/Handoff/Broadcast fence 冲突；
- close 的 live、exact tombstone、mismatched tombstone、neither 和 invariant breach 分支；
- close replay 的 fingerprint/cursor、foreign user isolation、duplicate close 和 restart replay；
- duplicate terminal、duplicate close、stale cursor、future epoch；
- requester exact pair、nonce/epoch、authority routed generation、Socket replacement 和 subscriber 去重；
- 缺 requester physical identity 的旧 pending transaction、startup recovery、重复启动和不重新 dispatch；
- first-prev、last-next、single-item queue、natural end 重复 callback；
- commit、emit、enqueue 故障注入。

Phase 3 三个子阶段都要各自完成定向测试、完整测试、独立提交和远端 CI，不得合成一个巨型提交。

## 10. Phase 4：Follow 服务端闭环

### 10.1 覆盖范围

- REQ-076—REQ-081
- acceptance 85—94 的服务端可自动化部分

### 10.2 服务端边界

服务端实现：

- composite capability；
- source current physical fact、clock 与 freshness；
- frozen suspended baseline ACK；
- persistent FollowSafetyLease；
- source/follower/suspended authority exact identity 和契约要求的 physical generation；
- immutable frozen baseline、phase/deadline/fence、start/stop/cleanup fingerprint；
- durable ACK/error/terminal outcome、reconnectGrace、cleanupRequired；
- suspended Context fence；
- relationship/subscription；
- source recovery window；
- follower reconnectGrace/cleanupRequired；
- server restart reload；
- stop/cleanup 幂等与上限例外。

`FollowRecoveryRecord` 是 Flutter 本地持久化对象，服务端不得伪造或把它实现成服务器表来代替 FollowSafetyLease。

Follow start 冻结 ACK 必须从同一个事务快照得到，并包含：

```text
sourcePlaybackContextId
suspendedPlaybackContextId
suspendedAuthorityClientId
suspendedAuthorityDeviceSessionId
suspendedEpoch
suspendedVersion
suspendedQueueRevision
suspendedControlVersion
suspendedAppliedControlVersion
```

### 10.3 固定子阶段

#### 4A：Lease、fence、限制与 restart reload

```text
feat: persist r18 follow safety leases
```

#### 4B：Start preflight、source fact 与 frozen ACK

```text
feat: enforce r18 follow start baseline
```

#### 4C：Idle、disconnect、reconnect、cleanup 与 replay

```text
feat: implement r18 follow recovery cleanup
```

### 10.4 Follow 硬不变量

- Follow mirror 不写 source Context 或 suspended Context；
- Follow 模式不允许用 ordinary `playback.update` 描述 mirror；
- 不新增 `follow.feedback`；
- source idle 保留 relationship 和 fence；
- cleanupRequired lease 不按普通 TTL 删除；
- 客户端未安全恢复时不得提前释放 suspended fence；
- Follow source 可以与 Broadcast source 共存；
- follower overlay 与 Broadcast ordinary/Handoff target 按契约互斥；
- stop/cleanup 不得因达到 pair/context/user 上限而被拒绝；
- ACK baseline 每个字段都来自同一冻结事务快照。
- FollowSafetyLease、fence 和 frozen baseline 必须在同一锁序和同一 DB transaction 内落库，commit 后才发送 ACK/push；
- ACK replay 只能读取 durable outcome，不能重新读取 live Context 拼装；
- 不得使用 raw SID、pair-only physical fallback 或当前最新 session 替代 durable physical generation。

### 10.5 Follow 测试矩阵

- source Socket replacement/nonce mismatch；
- stale/future fact、clock unavailable、rate 不支持；
- start 原子写入失败；
- ACK baseline 逐字段一致；
- source idle、恢复、超时；
- follower 断线、30 秒 grace、重连、cleanupRequired；
- server 在 acquiring/active/stopPending/cleanupRequired 重启；
- duplicate start/stop/cleanup ACK replay；
- pair/context/user 容量和 cleanup 例外；
- 与 Core、Broadcast、Handoff 的占用矩阵。

## 11. Phase 5：Handoff 独立 provisional lane

### 11.1 覆盖范围

- REQ-031
- REQ-082—REQ-086
- acceptance 95—103

### 11.2 固定子阶段

#### 5A：Exact target pair 与全生命周期 fence

```text
feat: fence r18 handoff exact pairs
```

#### 5B：独立 provisional N+1 lane

```text
feat: add isolated r18 handoff provisional lane
```

#### 5C：Proof、原子 complete、disconnect 与 replay

```text
feat: complete r18 handoff atomically
```

### 11.3 Handoff 硬不变量

- target 必须是 exact `targetClientId + targetDeviceSessionId`；
- source/target 必须同时绑定 nonce + epoch physical generation；不得以 pair-only、raw SID 或 latest online session fallback；
- source Context、target pair、standby Context 从 start 到 terminal 全程 fenced；
- provisional `N+1` 以 `(playbackContextId, epoch, handoffId)` 隔离；
- provisional lane 不创建普通 control transaction，不参加 dependency/watchdog/reconciliation；
- complete 是唯一 authority switch；
- source 在 complete 之前继续播放；
- source actual 改变时先产生 `source_changed`，再提交 canonical fact；
- complete 验证 future ≤ 50ms、sample age ≤ 2000ms、late ≤ 1000ms、position error ≤ 1000ms，并满足 sample 相对 effectiveAt 的约束；
- `complete.clientSeq` 消耗 target 的普通 `playback.update` 序列空间；
- start/ready/complete/cancel/release 的 fingerprint、durable terminal/replay outcome 和 lifecycle/effective/deadline 必须持久化；
- failed/cancel/restart 后 canonical controlVersion 仍是 N，普通事务可以合法使用 N+1；
- duplicate replay 不得再次退休 standby、切换 authority、增加 cursor 或释放 fence。

Handoff complete 的原子顺序必须是：

```text
验证 still-active handoff、exact target physical connection 与全部 proof
→ 验证 source/target/standby fences 和 canonical N 仍未改变
→ 同一 DB transaction 写 target DeviceState actual proof
→ 切 authority exact pair
→ 推进契约规定的 epoch/version/control cursor
→ 写 completed durable outcome/fingerprint
→ 释放或终结相关 durable fences
→ commit
→ commit 后发送 ACK/push
```

任何验证、disconnect、restart 或 commit 失败都不得切 authority，也不得永久消耗普通 controlVersion `N+1`。

### 11.4 Handoff 测试矩阵

- target pair 不在线、Socket replacement、能力/clock/rate 失败；
- prepare enqueue failure、ready negative、commit enqueue failure；
- source/target 在各阶段断线；
- server 在各阶段重启；
- duplicate start/ready/complete/cancel/status/release；
- normal mutation 与 Handoff 并发；
- proof 的每个边界值和边界外值；
- commit DB failure、emit failure、ACK loss；
- failed 后普通 N+1 transaction；
- exact-pair route，禁止 clientId-only fallback。

## 12. Phase 6：Broadcast final-r18 restore、terminal 与 decommission

### 12.1 覆盖范围

- 重新验证 REQ-039—REQ-067；
- 实现 REQ-087—REQ-090；
- acceptance 37—77 与 104—110。

不能只实现新增四条。最终 mapping 已把 pre-freeze Broadcast 工作降为 pending，必须在最终 Core/identity/capability 组合下重新验证。

### 12.2 固定子阶段

#### 6A：Core 集成与 action-aware restore matrix

```text
feat: integrate r18 broadcast restore gates
```

#### 6B：Negative cleanup 与 terminal delivery gate

```text
feat: enforce r18 broadcast terminal delivery
```

#### 6C：Fixed membership、soft sync、abandon 与 decommission

```text
feat: decommission r18 broadcast pairs atomically
```

### 12.3 Broadcast 硬不变量

- active/waiting occupancy 返回契约规定的 conflict；
- terminal restorePending 对受限 action 返回 `restore_in_progress`；
- matching negative ready/prepared cleanup 不初始化播放、不推进 cursor、不 commit、不清 gate；
- terminal replay 必须先于 ordinary Context business delivery；
- enqueue terminal 失败立即断开对应 Socket，不能继续普通业务；
- capability 关闭不能阻断已有 terminal drain；
- membership 是固定 exact-pair 集合，不动态增删；
- soft-sync 的 applied 不是持续 drift SLA；
- recovery abandon 与 permanent exact-pair decommission 同一事务；
- recovery reference、abandon fingerprint/outcome 和永久 `(user, clientId, deviceSessionId)` decommission tombstone 必须 durable；
- decommission tombstone 不受普通容量清理或 TTL 删除；创建 presence/SID 之前拒绝旧 pair，在线旧 Socket 同步断开，普通重连不能复活；
- permanent tombstone 只能随账号整体删除；
- decommission 必须在创建 presence/sid 之前拒绝，在线旧 Socket 同步断开；
- 被 decommission 的旧 deviceSession 永久拒绝，不能被普通重连复活。

关键 terminal delivery 不能复用会吞掉 emit/enqueue 失败的通用 post-commit helper。注册流程必须先可靠排入该 exact pair 的 terminal replay，成功后才能开放 presence、device.list 和普通 Context business；失败则立即 fail closed 并断开 Socket。

### 12.4 Broadcast 测试矩阵

- restorePending 的 action allow/deny 表逐项测试；
- negative ready/prepared 的 matching/mismatching id；
- terminal-before-business 与 enqueue failure；
- full-to-compact recovery、deliveryId、outbox replay；
- restart、7 天压缩、容量上限；
- fixed membership 和 soft-sync 边界；
- abandon/decommission 原子 rollback；
- 在线 Socket 断开、旧 session 永久拒绝、新 exact pair 行为；
- pre-freeze acceptance 37—77 在 final-r18 组合下全回归。

## 13. Phase 7：跨 profile、重启、readiness 与证据收口

### 13.1 必须验证

- 单 realtime worker 保护；
- graceful shutdown；
- pending control restart → unknown/cascade；
- Handoff restart → failed/cleanup；
- Follow restart → cleanupRequired fence；
- Broadcast active/waiting restart → terminal stopped；
- permanent decommission restart reload；
- Core/Follow/Handoff/Broadcast 相互占用矩阵；
- production fail-closed；
- explicit dev/test 便利开关仍可用；
- capability 关闭不阻断已有 Broadcast terminal drain；
- bounded storage、清理和幂等记录保留语义；
- fixture manifest、EARS verifier 和 requirement inventory 最终覆盖 REQ-001—REQ-090，且每项只引用真实存在、真实运行的测试方法。

### 13.2 Mapping 更新规则

可以更新：

- 服务端 schema/parser 证据列；
- 服务端状态机/持久化证据列；
- 实际运行的自动化测试证据列。

必须保持未完成：

- Flutter parser/controller；
- Android + Windows 双端真机证据；
- 任何仍依赖真实跨端联调的条目。

不得把总体状态改为 `Verified`。服务端全部绿色也只能说明“server implementation candidate complete”；契约总状态仍受客户端和设备证据约束。

### 13.3 最终本地门禁

严格使用仓库已有命令和 CI 定义：

```bash
python -m unittest
python -m unittest tests.net.suite
git diff --check
```

再运行 `.github/workflows/tests.yaml` 中与 build、migration、Docker 对应的仓库命令。禁止自行发明 lint 门禁。

### 13.4 最终远端门禁

推送后必须等待并记录：

- Python Tests 3.9；
- Python Tests 3.12；
- Database Migrations；
- Docker Build。

本地通过不得称为 CI 通过。只有远端四 job 全绿才能报告“server CI green”。PR 仍保持 Draft，不自动合并。

## 14. Luna 每个 Phase 的执行协议

每次只把“本计划 + 一个 Phase 启动提示词”交给 Luna。Luna 的输出必须包含以下检查表：

```text
Phase：
起始 branch/HEAD：
工作树基线：clean / user changes present
读取的契约章节：
本 Phase 允许文件：
实际修改文件：
超出计划文件及原因：
基线失败：
修复后的定向测试（命令、exit code、测试数）：
完整 unittest（命令、exit code、测试数）：
net suite（命令、exit code、测试数）：
git diff --check：
数据库后端结果（如适用）：
提交 SHA 和 message：
推送分支：
远端 CI run URL：
Python 3.9：
Python 3.12：
Database Migrations：
Docker Build：
已实现的 REQ/acceptance：
仍 pending 的服务端项目：
仍 pending 的客户端/真机项目：
是否允许进入下一 Phase：yes / no
```

若答案是 `no`，Luna 必须停止，不得自行开始下一 Phase。

## 15. 历史 Phase 0 启动提示词（已完成，不得再次执行）

以下提示词仅保留 Phase 0 的历史执行记录。Phase 0 已在 `7abed609` 完成；不得用本节重新启动 Phase 0，也不得把其中的旧起始基线当作当前实现基线。

```text
请在仓库 jsdfhasuh/Emosonic-Server 的分支
agent/strict-v2-r5-server-adaptation
上执行：

docs/plans/2026-08-02-strict-v2-r18-server-implementation.md
中的 Phase 0，且只执行 Phase 0。

开始前完整阅读：
- AGENTS.md
- 上述实施主计划
- specs/emosonic_strict_v2_socketio_server_contract.md
- 入口引用的全部 19 个权威分卷
- docs/plans/2026-08-01-strict-v2-r18-contract-finalization.md
- docs/verification/emosonic_strict_v2_r18_requirement_mapping.md

预期起始 HEAD 是：
08dc67a2a00a5f80311af02fabfa56a5ddf4bb51

若 HEAD 已前进，不要 reset；记录实际 HEAD，重新核对该 HEAD 的 CI 后继续。
若工作树包含非本任务的用户改动，不要覆盖，停止并报告。

本轮目标仅为修复冻结 r18 身份、requirement inventory 和现有 CI 基线。
不得修改 specs/**、runtime 状态机、数据库、wire schema、Follow、Handoff 或 Broadcast 业务逻辑。

当前已知：
- frozen entry 实际 SHA-256：116a36e2359b2f6c525f340187c7daa6fd0e476241c0b190f1145b88b0e6e46f
- 派生材料旧 SHA-256：2580851b2059d1d80b2059fe37f2b6e16d156787b741035436009baa3e8af90e
- requirement coverage 仍只接受 REQ-001—067
- fixture/evidence 工具主要仍停在 REQ-001—045
- 当前 Python 3.9/3.12 均为 1665 tests，4 failures、2 errors、3 skipped

必须精确修复主计划列出的六个失败/错误。
不得通过删除、skip、expectedFailure、减少范围、伪造 testMethods、放宽 validator、修改 CI 或提前打开 readiness 来变绿。
formal profile readiness/evidence 保持 false/empty。

先运行定向测试，再运行：
- python -m unittest
- python -m unittest tests.net.suite
- git diff --check

不要引入 ruff、pycodestyle、black、mypy；仓库没有这些官方门禁。

只有所有本地检查绿色时才提交并推送：
chore: align frozen r18 conformance baseline

推送后等待并报告远端：
- Python Tests 3.9
- Python Tests 3.12
- Database Migrations
- Docker Build

任一检查失败就停止，不提交红色状态，不开始 Phase 1。
最终按主计划第 14 节模板汇报。
```

## 16. 项目负责人在每个 Phase 后的审查问题

在把下一 Phase 发给 Luna 前，至少检查：

1. 它是否修改了 `specs/**` 或协议版本？
2. 它是否删除、跳过、弱化或错误复用了测试？
3. 它是否把未完成 REQ、Flutter 或设备证据标成完成？
4. 新 schema 是否仍为 closed，null/unknown/forbidden 是否有负例？
5. 数据库迁移是否追加并覆盖三后端？
6. 状态改变是否 commit 后 emit，rollback 是否零副作用？
7. 路由是否始终 exact pair？
8. Handoff provisional 是否完全独立于普通 control transaction？
9. dev/test 与 production readiness 是否都保持契约要求？
10. 本地与远端结果是否被准确区分？
11. 当前提交是否只包含一个可审查的绿色检查点？
12. PR 是否继续保持 Draft？

任一答案不明确时，不启动下一 Phase。
