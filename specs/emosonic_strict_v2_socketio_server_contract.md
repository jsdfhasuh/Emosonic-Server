# EmoSonic strict-v2 Socket.IO 服务端契约

> 文档状态：Approved r19 authoritative contract
> 契约冻结状态：Frozen
> 实现状态：Contract defined / implementation pending
> 文档修订：2026-09-04-r19
> 协议版本：`2.9.0`
> 读者：EmoSonic 服务端与 Flutter 工程师
> 范围：PlaybackContext v2 strict-v2。定义客户端与服务端应发送、应接受的协议契约；不保留 `2.3.x` 客户端 shape 或旧 `sessionId` 协议兼容，也不授予 production rollout 权限。

## 权威性与阅读规则

本路径是 r19 契约的唯一当前权威入口。服务端实现、注册 metadata、测试和变更说明必须以本入口及
下方全部分卷为准。`2026-09-04-r19 / 2.9.0` 已 Contract Frozen，但契约冻结不表示服务端或 Flutter
已经完成实现，也不表示任何 optional profile ready；具体 capability 仍须按本文 readiness 条件开放。

完整 r19 规范按文件编号 `01` 至 `14`（含 `06a`—`06d`、`11a`—`11c`）由下方 19 个分卷共同组成；分卷只是为了
按任务读取、降低上下文消耗，不改变 action、字段、错误码、状态机、章节编号或优先级。单个分卷不能
被声明为一份独立或替代协议。

r19 沿用未被本轮修改的 r18 分卷正文；这些分卷头部保留其最后一次本地修订身份。凡旧正文把
`2.8.0` 描述为当前协商门槛时，均由本入口、11a 与 13 的 r19 `2.9.0` 门槛取代；其余 wire shape、
字段和状态机继续有效。

当注册 metadata Goal、服务端变更说明、实现代码、测试名称、历史草案或任一分卷之外的材料与本契约
冲突时，应把冲突视为 conformance 缺陷并修正文档或实现。历史 `*change*.md`、带时间后缀的副本以及
`ref/` 下旧版本均为非权威材料。

阅读某个 profile 时还必须读取阶段 0 的公共 envelope、注册、时钟、ACK、错误、幂等、cursor 和 schema
规则；实现 readiness 或做最终审计时必须再读取阶段 3。跨分卷出现解释疑义时，以原章节编号和完整
有序组合为准，不得只截取局部规则。

## 冻结状态、pre-freeze 兼容与后续纪律

本次升级到 `2.9.0`，用于冻结 r18 之后发现的 playback feedback 结算、真实设备状态 readiness 与
watchdog 数据库连接生命周期行为。r18 `2.8.0` 与 r19 `2.9.0` 不保证混跑；双方必须按最终 r19
成组升级。冻结后发现服务端、Flutter、validator、fixture 或测试与本契约不一致时，默认修正实现
适配本 r19，不得反向静默修改契约迁就旧实现。

纯错字、链接、示例或不改变行为的文字澄清使用 r19 errata。冻结后新增 action、字段、状态、错误码、
持久化义务或改变客户端行为，必须进入后续修订并重新评估 `protocolVersion`；不得继续静默改变
`2.9.0` 行为。本次仍不授予 production readiness；显式个人实验室 profile 与生产 fail-closed 规则
保持不变。

## r19 范围摘要

本 r19 使用 strict-v2 `2.9.0` 单一 shape。Core 固定 exact authority pair Context snapshot、四 cursor
error、Core prepare、device volume、带 dependency/execution timeout 的 routed control、requester exact-pair
settlement、transitive cascade、terminal-gap reconciliation、安全 close 与 distinct/no-repeat queue 边界。
Follow 固定 composite capability、current-physical source fact、prewrite/frozen baseline ACK、persistent
FollowSafetyLease、local failure 与 cursor-safe recovery。Handoff 固定 source/target full fence、独立
provisional N+1 execution lane、closed prepare/commit/complete shape、50ms future/1000ms late/1000ms
position proof、near-end fail-fast 与 complete-only authority switch。

Broadcast 是 source PlaybackContext 派生的 fixed-membership soft-sync mirror；ordinary pair 使用 dedicated
feedback、crash-safe restore、action-aware restorePending write gate、terminal delivery gate 与
full-to-compact recovery。管理端 abandon 必须绑定 permanent exact-pair decommission。本轮不保留
`2.5.0/r12`、`2.6.0/r13`、`2.7.0/r14` Broadcast shape、旧 `sessionId` 或其他兼容分支。只有双方 schema/
state implementation、自动化与 Android+Windows 真机证据全部满足本文时才可标记 implementation ready。

r19 进一步规定：rejected stale passive 不产生 canonical playback.update；Context/Queue snapshot 不得
结算 playback feedback；`clientSeq=0` 内部 baseline 不得输出或满足 profile readiness；Follow 与
Broadcast 只接受当前物理连接的真实 DevicePlaybackState；后台 watchdog 每轮共享一个 owner-aware
数据库连接，并在 MariaDB 1040 时按有界序列退避整轮。

## 按任务最小读取集

| 任务 | 必读分卷 |
| --- | --- |
| 登录、注册、provenance、时钟 | 01—03 |
| 普通 PlaybackContext、queue、player control | 01—04、07—08、11a、11c |
| Follow | 01—05、11a、11c |
| Handoff | 01—05、07、09、11a、11c |
| Broadcast source/start/control | 01—04、06a—06c、07—08、10、11a—11c |
| Broadcast ordinary/feedback/terminal | 01—03、06a、06c—06d、07、10、11a—11c |
| 错误、重连、幂等或 cursor 审计 | 02—03，加目标 action 分卷 |
| 全量 conformance / readiness 审计 | 01—14 |

## 分阶段分卷清单

以下按实施阶段分组展示；重建原契约章节顺序时始终按文件编号 `01` → `14`，不能按目录显示顺序拼接。

### 阶段 0：公共基础

- 01 — [协议总览](emosonic_strict_v2_contract/phase-0-foundation/01-overview.md)
- 02 — [传输、注册与时钟](emosonic_strict_v2_contract/phase-0-foundation/02-transport-registration-and-clock.md)
- 03 — [ACK、错误、幂等、Cursor 与闭合规则](emosonic_strict_v2_contract/phase-0-foundation/03-ack-errors-idempotency-and-cursors.md)

### 阶段 1：Core PlaybackContext

- 04 — [客户端 Core 请求](emosonic_strict_v2_contract/phase-1-core/04-client-core-actions.md)
- 07 — [服务端设备、Context 与 Status 消息](emosonic_strict_v2_contract/phase-1-core/07-server-device-context-and-status.md)
- 08 — [服务端 Queue、Playback 与 Routed Control 消息](emosonic_strict_v2_contract/phase-1-core/08-server-queue-playback-and-controls.md)

### 阶段 2：可选 Profile

- 05 — [客户端 Follow 与 Handoff 请求](emosonic_strict_v2_contract/phase-2-optional-profiles/05-client-follow-and-handoff.md)
- 06a — [客户端 Broadcast Actions](emosonic_strict_v2_contract/phase-2-optional-profiles/06a-client-broadcast-actions.md)
- 06b — [Broadcast Source Context](emosonic_strict_v2_contract/phase-2-optional-profiles/06b-broadcast-source-context.md)
- 06c — [Broadcast Feedback、屏障与重连恢复](emosonic_strict_v2_contract/phase-2-optional-profiles/06c-broadcast-feedback-and-recovery.md)
- 06d — [Flutter Broadcast 角色与 Terminal](emosonic_strict_v2_contract/phase-2-optional-profiles/06d-flutter-broadcast-roles-and-terminal.md)
- 09 — [服务端 Handoff 消息](emosonic_strict_v2_contract/phase-2-optional-profiles/09-server-handoff.md)
- 10 — [服务端 Broadcast 推送与 Status](emosonic_strict_v2_contract/phase-2-optional-profiles/10-server-broadcast.md)

### 阶段 3：实现与验收

- 11a — [公共与 Core 实现要求](emosonic_strict_v2_contract/phase-3-conformance/11a-common-and-core-requirements.md)
- 11b — [Broadcast 实现要求](emosonic_strict_v2_contract/phase-3-conformance/11b-broadcast-requirements.md)
- 11c — [安全、持久化与 Capability Readiness](emosonic_strict_v2_contract/phase-3-conformance/11c-security-persistence-and-readiness.md)
- 12 — [非 strict-v2 旧 Surface](emosonic_strict_v2_contract/phase-3-conformance/12-legacy-surfaces.md)
- 13 — [已知边界与联调验收](emosonic_strict_v2_contract/phase-3-conformance/13-integration-acceptance.md)
- 14 — [协议权威性与部署证据](emosonic_strict_v2_contract/phase-3-conformance/14-authority-and-deployment-evidence.md)

## 维护约束

- Frozen r19 的 wire shape 与行为不再就地修改；后续行为变化进入新修订并重新评估协议版本，纯文字修正走 r19 errata。
- 新规则应放入其所属原章节；只有出现新的独立协议域时才新增分卷。
- 分卷重命名或移动时必须同步更新本清单和仓库引用。
- 服务端工程师只需要接收本入口及 `emosonic_strict_v2_contract/` 目录；历史草案不应交付。
