# Strict-v2 legacy Context 候选规则勘误

> 日期：2026-09-02
> 面向：EmoSonic 服务端与客户端工程师
> 协议：strict-v2 `2.8.0`，契约修订 `2026-08-01-r18`
> 状态：r18 exact-pair 不变量的文字澄清；服务端实现已修复，自动化回归随本文件补齐

## 1. 问题

旧版本迁移可能留下 `lifecycle=active`、`authorityClientId` 非空但
`authority_device_session_id IS NULL` 的 PlaybackContext 行。这类记录不能形成 strict-v2 所要求的
`(authorityClientId, authorityDeviceSessionId)` exact pair。

旧的 ensure 候选查询曾把这些 legacy 行与有效 Context 一起计数。结果是：即使当前 stable clientId
没有可返回或可重绑的 strict-v2 Context，`playback.context.ensure` 仍可能误判为“存在多个候选”并返回
conflict。

这不是 wire shape、协议版本或 r18 的 exact-pair 不变量发生变化，而是持久化兼容边界没有写完整。

## 2. 预期行为

服务端在 `(authenticated user, stable clientId)` 的 ensure 原子临界区内计算 active Context 候选时：

1. 候选必须同时具有非空 `authorityClientId` 与 `authorityDeviceSessionId`，能够形成完整 exact pair。
2. `authority_device_session_id IS NULL` 的 legacy active 行不构成 strict-v2 候选。
3. legacy 行不得作为当前 Context 返回或重绑，也不得参与多个候选 conflict。
4. 过滤后没有候选时，服务端按请求快照创建新的 queue-backed 或 idle Context。
5. 过滤后只有一个有效候选时，服务端按既有 r18 规则返回 exact-pair Context，或把唯一的离线旧
   deviceSession Context 原子重绑到当前 deviceSession。
6. 过滤后存在多个有效非空候选时，服务端必须 fail-closed，并且不得创建、重绑或修改 Context。

## 3. EARS 要求

**REQ-027 clarification — Player startup ensure candidate eligibility**

当服务端处理 `playback.context.ensure` 并为 authenticated user 与 stable clientId 计算 active Context
候选时，服务端必须只把 authority client/device 字段均非空、可形成 exact pair 的 active Context 视为
候选。

当 active 行的 `authority_device_session_id IS NULL` 时，服务端不得把该行用于返回、重绑或多个候选
conflict，也不得仅因 ensure 而删除、关闭、回填或修改该行。

当过滤后的候选数为零时，服务端必须创建新 Context；当候选数为一时，服务端必须执行既有返回或重绑
规则；当存在多个有效非空候选时，服务端必须 fail-closed，且所有 Context 状态保持不变。

## 4. 安全与兼容约束

- 本勘误不批量删除、关闭、回填或迁移 legacy 数据。
- 本勘误不允许把 NULL device-session 猜测、派生或替换为当前 deviceSessionId。
- 本勘误不放宽多个真实 strict-v2 候选时的 fail-closed 行为。
- 本勘误不修改 action、payload、response、错误码、数据库 schema、`protocolVersion`、descriptor、
  `schemaHash`、manifest 或 contract hash。
- 现有唯一 exact-pair 返回、离线 deviceSession 重绑、cursor 递增、binding invalidation 与 requestId
  幂等规则均保持不变。

## 5. 错误处理

- 仅有 legacy NULL 行不再构成 conflict；ensure 创建新 Context 后按正常成功 shape 结算。
- legacy NULL 行与一个有效非空候选并存时，只对该有效候选执行既有返回或重绑规则。
- legacy NULL 行与两个或更多有效非空候选并存时，仍返回既有 conflict；legacy 行不改变冲突结论。
- conflict、事务失败或持久化失败时，不得留下部分创建、部分重绑、cursor 递增或 binding push。

## 6. Given / When / Then 验收

### 6.1 只有 legacy NULL 行

**Given** 同一 authenticated user 与 stable clientId 存在一条或多条
`authority_device_session_id IS NULL` 的 active legacy 行，且不存在有效非空候选。

**When** 当前 player exact pair 发送合法 `playback.context.ensure`。

**Then** 服务端创建并返回绑定当前 exact pair 的新 Context；legacy 行保持 active 且内容不变；不存在
错误重绑或 conflict。

### 6.2 legacy NULL 行与唯一有效候选并存

**Given** 同一 scope 存在 legacy NULL 行，并且仅存在一个能够形成 exact pair 的 active Context。

**When** 当前 player 发送合法 ensure。

**Then** 服务端忽略 legacy 行，只按 r18 既有规则返回该唯一 Context 或执行合法的离线
deviceSession 重绑；不得创建第二个 strict-v2 Context。

### 6.3 legacy NULL 行与两个真实候选并存

**Given** 同一 scope 存在 legacy NULL 行，并且存在两个具有非空 authority device-session 的 active
Context。

**When** 当前 player 发送合法 ensure。

**Then** 服务端返回既有 conflict，不创建、不重绑、不关闭且不修改任何 Context；三条原记录都保持
原 lifecycle 与 binding。

## 7. 自动化回归

服务端测试至少覆盖：

1. legacy NULL 行不进入 ensure 候选查询；
2. legacy NULL 行不会阻止过滤后零候选的新建流程；
3. legacy NULL 行与唯一有效候选并存时，唯一候选仍按既有规则处理；
4. legacy NULL 行与两个有效候选并存时仍 fail-closed；
5. conflict 分支不创建、重绑、关闭或修改任何 Context；
6. canonical 契约、EARS 与集成验收均包含本勘误语义。

对应回归位于：

- `tests/base/test_emo_ws_store.py`
- `tests/base/test_emo_strict_v2_manifest.py`

## 8. Canonical 同步位置

本勘误同步到以下 canonical 文件：

- `specs/emosonic_strict_v2_contract/phase-1-core/07-server-device-context-and-status.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/11a-common-and-core-requirements.md`
- `specs/emosonic_strict_v2_contract/phase-3-conformance/13-integration-acceptance.md`

这些文字是 r18 exact-pair 候选资格的澄清。入口契约、wire schema 与冻结元数据保持不变，不产生 r19。
