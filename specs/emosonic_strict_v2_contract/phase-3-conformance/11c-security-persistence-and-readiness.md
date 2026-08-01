# 阶段 3：安全、持久化与 Capability Readiness

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-07-23-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 7.1—7.3 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
### 7.1 安全与资源限制

1. 除仅绑定 loopback/LAN 且由开发者明确接受风险的本地 lab 外，端点必须使用 TLS
   (`https`/`wss`)；`auth.login` 含明文密码字段，公网明文 HTTP/WebSocket 禁止部署。
2. 服务端不得记录 `auth.login.payload`、密码、完整凭据或包含它们的原始 envelope。审计日志
   只记录 requestId、action、认证后的 user/client、结果 code、延迟和脱敏端点。
3. 生产环境 Engine.IO/Socket.IO Origin 必须使用部署 allowlist；不得在携带凭据时使用通配符
   `*`。只有显式开发模式可启用 `*`，且启动日志必须输出安全警告。非浏览器客户端没有 Origin
   时，必须依靠认证和网络策略，不能伪造 Origin 放行。
4. 默认资源上限：每 IP 同时 10 条未认证连接、每用户 20 条已认证连接；每连接每分钟 120 个
   strict 请求，其中 player control 与 localUser playback.update 合计每秒最多 20 个、普通 passive
   playback.update 与 `broadcast.feedback` 合计每秒最多 10 个，ensure/prepare/handoff/broadcast start
   每分钟各 10 个。每个 active
   Context 最多保留最近 512 个 terminal control transaction 和 512 个 local intent dedupe 记录；更老
   记录可压缩为 cursor/tombstone，但不能在仍可能重试的 10 分钟内删除。部署可调低，调高必须有
   负载测试证据。每个 Broadcast 最多 20 个 ordinary participants；显式列表超限返回
   `bad_request`，隐式选择按 clientId 升序取前 20 个并把其余列入 skippedClientIds；source 与
   controllerOnly 不计入 20。其他连接数/请求频率超限返回
   `rate_limited` 或在握手阶段拒绝连接，不得把 Broadcast participant 超限改成 rate_limited。
   每 authenticated user 最多 256 个 Broadcast recovery slot；每个 ordinary pair 从 start 到 terminal
   restore confirmation 占用一个，不随断线或 full-to-compact 压缩重复计数。超出 slot 的目标进入
   skippedClientIds；最终无可用 participant 时 start 返回 `rate_limited`。
   internal serverReconciliation record 计入 control transaction 持久化上限；当 terminal-gap 审计仍
   引用它时不得提前清理。普通 routed control 的 executionTimeoutMs 部署默认值为 15000。
   每个 exact follower pair 最多一条非终态 FollowSafetyLease，每个 suspended Context 最多一个 Follow
   overlay，每 authenticated user 最多 256 条 active/reconnectGrace/cleanupRequired Follow records。
   cleanup 不受 rate limit，不能删除旧 lease 接受新 start。
5. 必须设置 Engine.IO payload 上限不高于 256 KiB；transport 超限使用 message-too-big 行为断开，
   已进入 handler 的业务限制超限返回 correlated `bad_request`。malformed JSON、非 object
   envelope 不得进入 handler；格式合法但不在 allowlist 的 action 返回 `not_supported`；缺失或
   非法 action/requestId 时按第 2.2 节断开。
6. 每个 action 在执行前重新校验 authenticated user、Context membership、角色、capability 和
   当前 sid 绑定；不能只在 subscribe 时授权一次。解析顺序固定为 envelope/schema、authentication、
   registration/capability、caller role、authenticated-user-scoped lookup、lifecycle/overlay/recovery
   fence、base cursor、mutation。`playback.context.list`、Handoff target 与 volume target 都必须先按
   当前用户限定查询；跨用户与不存在使用相同结果，错误与日志不得泄露其他用户资源是否存在。
7. 必须配置 ping/pong dead-connection cleanup、发送缓冲上限和背压策略。控制命令不可作为
   volatile broadcast；无法可靠单播给 authority 时返回 `authority_offline`。
8. Broadcast terminal tombstone、per-pair delivery outbox 和 start intent 记录不适用第 4 条 10 分钟
   压缩窗口。terminal snapshot/full outbox 必须完整保留 7 天；第 5.5.2 节压缩成功后，
   可删除完整记录，但每个未确认 pair 的 TerminalRecoveryRecord 与 restorePending 必须保留到
   恢复确认。每个 pair 同时最多一条 compact recovery。ACK outcome tombstone 保留到 source
   Context close，且每个 Context 最多 1024 个；每 user 最多 256 个 recovery slots。达上限时
   拒绝新 intent/participant，不删除旧 intent。每个
   Broadcast 还必须保留最近 512 个 target revision ledger，且 10 分钟反馈窗口内的 revision
   不得删除；压缩时 terminal target 的 queueIndex/trackId/position/rate 必须转入 compact record。
   清理必须按 terminalAtServerMs 建立索引，且不得在同一 broadcastId 仍处于
   active/waitingForSource 时运行。

### 7.2 单实例、持久化与重启

- strict-v2 2.8.x 当前只支持单 realtime worker。服务端在能够识别 Gunicorn/processes/workers 等
  多进程配置时必须启动失败，不得仅警告后继续运行。多 worker 支持必须另立工程目标，并同时
  提供 sticky sessions、跨 worker broker、共享原子 Context、共享 sid/subscription/dedupe store。
- active Context 与 closed tombstone 必须持久化。重启恢复时保留
  `epoch/version/queueRevision/controlVersion`、authority client/device identity、每个 authority device
  的 lastAppliedControlVersion、control transaction terminal/pending 状态和 local intent dedupe 结果；
  清空全部 sid、nonce、connectionEpoch、connection-scoped request cache 和 subscription，等待客户端
  重新注册，并用新的 requestId 重新 list/subscribe/status。
- graceful restart 必须先停止接收新连接，完成或明确失败正在结算的请求，停止创建新
  handoff/broadcast，再关闭 Socket。不能把未完成 handoff 恢复为 completed。
- 重启时非终态 Handoff 进入 `failed`，`errorCode:"server_restart"`；Follow relationship/subscription 的
  内存状态清除，但持久化 FollowSafetyLease 必须加载为 reconnectGrace/cleanupRequired 并恢复 suspended
  Context fence，不得自动恢复 mirror audio；`active`
  或 `waitingForSource` Broadcast 进入 terminal stopped 并冻结 cursor。服务端必须持久化其 terminal
  tombstone、ACK outcome tombstone、target revision ledger、restorePending、recovery slot 与 per-pair delivery
  状态；已压缩 TerminalRecoveryRecord 同样必须持久化。等待相同 pair 重连后
  按第 5.5.2 节补发；基础 Context 继续保留。
- 重启时所有 pending ordinary control 必须结算为 `execution_unknown`，并按依赖链递归结算
  `dependency_failed`；不得保留旧 controlVersion 重投、假装 committed 或任意选择 failed/superseded。
  随后只能按 terminal-gap reconciliation 与 fresh actual fact 收敛。
- `connectionEpoch` 每个新物理连接固定为 1；不得持久化或复用旧 nonce。

### 7.3 Capability profile readiness

Core profile 等于握手/注册、启动 ensure、idle/queue-backed Context、prepare、Context
discovery/binding invalidation、Queue、Player Control 和 playback.update 控制结算，缺一不可；服务端只有在
`playback.context.ensure`、`playback.context.prepare`、`playback.context.prepared`、
`playback.context.list`、`playback.context.bindings.changed` 及其他 Core action 的
request/response/event、cursor、routing、control/applied 分离、terminal transaction、
exact authority snapshot/four-cursor error、deterministic dependency、execution eligibility/watchdog、
server-only settlement/cascade、terminal-gap reconciliation、safe close、distinct queue boundary、
dedupe 和 error conformance 全部通过后，才可用 major `2`、minor `>=8` 的 `protocolVersion` 接受
`playbackContextV2:true`，否则注册返回 `not_supported`。Handoff、Follow、Broadcast 是三个
独立 profile，部署默认关闭；每个 profile 只有在本文对应状态机和双客户端 conformance 测试完成
后才能在 `negotiatedCapabilities` 返回 true。Broadcast 还必须完成 source-derived start、source
Context control transaction/cursor 原子耦合、普通 playback.update playbackRate、派生 snapshot、
playing-only start、跨连接 start intent、单 source 非终态约束、自动 source reconnect、ordinary
applied/failed `broadcast.feedback`、participant deadline/status、覆盖全部 Context/binding mutation 的
suspended-Context 屏障、restorePending 再入 fence、客户端恢复命令门控、terminal 原子释放/确认前保留/
7 天 full 补发与后续 compact recovery、fresh source sampled state、注册后 clock warm-up/持续 10 秒
cadence/mandatory server clock/迟到策略、earliest-unconfirmed deadline、逐 push revision +1、`0.5..2.0`
playbackRate 执行承诺、自然切歌 Context-first、source 转 idle 的原子 terminal、terminal cursor
冻结、restore cursor 只比较不写回、bounded intent tombstones、controllerOnly owner 观察副本和
participant/source 双分支、确定性 source→push action、per-pair deliveryId/`broadcast.resync`、不可变
status anchor、terminal Snapshot/feedback state 分域、stopped 原任务恢复、restorePending ensure
结算、关闭后 deadline 字段/timer 语义、20 participant 上限和 `broadcast.feedback.rejected` 验收；缺少任一项时
`supportsBroadcast` 必须协商为
false。

Follow readiness 必须完整覆盖 composite capability/effective-at clock、source current physical fact、
playing/paused/stopped/idle/self gate、start 前 RecoveryRecord、完整 frozen baseline ACK、persistent
FollowSafetyLease、全 suspended Context fence、per-pair/context/user limit、local mirror failure、source
idle、cursor-safe restore、source recovery 与 reconnect grace 双 timer、server/app restart cleanup，以及
Follow source 与 Broadcast source 共存和三种本地 overlay 互斥；缺少任一项时 `supportsFollow` 必须
协商为 false。已有 SafetyLease 的 follow.stop/cleanup 仍必须可用。
Broadcast profile 从 true 切为 false 时仍必须保持第 5.5 节 terminal drain 路径，直到已有
restorePending/TerminalRecoveryRecord 全部闭合。
metadata/profile 的 TOFU 成功不自动证明或开启任一可选 capability。

`remoteVolumeControl` 属于固定注册 shape 中的能力字段，不存在旧 shape 兼容。服务端只有在设备级
请求、目标路由、在线状态、event confirmation 与断线清理全部通过 conformance 后，才能向请求该
能力的连接协商 true。
