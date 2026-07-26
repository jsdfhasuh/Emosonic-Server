# EmoSonic strict-v2 Socket.IO 服务端契约

> 文档状态：Approved r18 source package（待服务端 Goal 0 整体提升至 `specs/`）
> 文档修订：2026-07-23-r18
> 协议版本：`2.8.0`
> 读者：EmoSonic 服务端与 Flutter 工程师
> 范围：PlaybackContext v2 strict-v2。定义客户端与服务端应发送、应接受的协议契约；不保留 `2.3.x` 客户端 shape 或旧 `sessionId` 协议兼容，也不授予 production rollout 权限。

## 权威性与阅读规则

本路径是已经确认的 r18 契约来源包入口。当前服务端仍实现 `2.4.0/r11`，因此在 Goal 0 完成前，
`specs/emosonic_strict_v2_socketio_server_contract.md` 仍是当前已实现版本的正式规范；不得把这两个版本
同时声明为当前唯一权威。Goal 0 必须把本入口和下方全部分卷整体提升到 `specs/`，再开始
`2.8.0/r18` 服务端改造。

完整 r18 规范按文件编号 `01` 至 `14`（含 `06a`—`06d`、`11a`—`11c`）由下方 19 个分卷共同组成；分卷只是为了
按任务读取、降低上下文消耗，不改变 action、字段、错误码、状态机、章节编号或优先级。单个分卷不能
被声明为一份独立或替代协议。

当注册 metadata Goal、服务端变更说明、实现代码、测试名称、历史草案或任一分卷之外的材料与本契约
冲突时，应把冲突视为 conformance 缺陷并修正文档或实现。历史 `*change*.md`、带时间后缀的副本以及
`ref/` 下旧版本均为非权威材料。

阅读某个 profile 时还必须读取阶段 0 的公共 envelope、注册、时钟、ACK、错误、幂等、cursor 和 schema
规则；实现 readiness 或做最终审计时必须再读取阶段 3。跨分卷出现解释疑义时，以原章节编号和完整
有序组合为准，不得只截取局部规则。

## r18 范围摘要

本 r18 使用尚未发布的 strict-v2 `2.8.0` 单一 shape：待机 Context、启动 ensure、prepare 和普通
`playback.update` 控制结算继续作为 Core；Broadcast 改为源 PlaybackContext 的派生镜像，删除可由
controller 提交的独立队列/start 状态和独立播放 cursor，新增 source-aware control、ordinary
participant applied/failed `broadcast.feedback`、playing-only/fresh-state start、跨连接 start intent、
完整 Context/binding/restorePending 屏障、崩溃安全的恢复记录顺序、恢复命令门控、确认前 terminal replay、
严格进度 revision、participant pair 身份、effective-at 时钟 warm-up/迟到处理、精确位置采样时间、
最早未确认 deadline、7 天 full-to-compact terminal recovery、有上界的 intent/recovery 存储、source
自然切歌和 authority 重连语义，并新增按 ordinary pair 隔离的 `deliveryId`/`broadcast.resync`、
source 变化到 push action 的确定性映射、不可变 status anchor、restorePending ensure 结算和
ledger 清理后的 `broadcast.feedback.rejected`。本轮不保留未实现的 `2.5.0/r12`、`2.6.0/r13`、
`2.7.0/r14` Broadcast shape、旧 `sessionId` 或其他兼容分支。只有全部 conformance tests 和真实双客户端
联调通过后才可标记 ready。后续 wire shape 变化必须同步更新 protocolVersion 和对应分卷，不得静默漂移。

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

- 修改 wire shape 时，必须更新对应分卷、协议版本和相关 conformance fixtures；不得把完整正文重新复制回本入口。
- 新规则应放入其所属原章节；只有出现新的独立协议域时才新增分卷。
- 分卷重命名或移动时必须同步更新本清单和仓库引用。
- 服务端工程师只需要接收本入口及 `emosonic_strict_v2_contract/` 目录；历史草案不应交付。
