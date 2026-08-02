# 阶段 3：协议权威性与部署证据

> [返回 r18 权威入口](../../emosonic_strict_v2_socketio_server_contract.md)
> 文档修订：`2026-08-01-r18`；协议版本：`2.8.0`
> 覆盖范围：原契约第 10 节。本文件是完整契约的一个规范分卷，不能脱离入口列出的公共规则单独解释。
## 10. 协议权威性、证据来源与部署证据

本 r18 契约包（权威入口及入口列出的全部分卷）是服务端工程师与 Flutter 工程师共同核对的完整
wire contract；本文件只是其中第 10 节分卷，不能单独替代整个契约包。其他材料的职责为：

`2026-08-01-r18 / 2.8.0` 的契约状态是 Approved + Contract Frozen；实现状态是
`Contract defined / implementation pending`。冻结只确定 normative schema/state machine，不把
pre-freeze `2.8.0` 构建、旧 fixture、旧自动化或单端日志提升为最终 conformance。服务端与 Flutter
必须按最终 r18 成组升级；真机证据决定 implementation readiness，不决定契约权威性或版本身份。

- `ref/emosonic_strict_v2_protocol_metadata_goal.md`：只定义 `device.register` metadata 描述符及其 hash 语义；不覆盖业务 action；
- `ref/emosonic_strict_v2_server_change_note.md`：只记录某个服务端版本声称完成的行为；不得覆盖本契约包；
- Flutter `test/fixtures/emo_protocol/strict_v2/manifest.json`：从本契约包派生的 machine-readable
  conformance inventory；不得覆盖本契约包，必须由 Flutter conformance tests 保持字段清单一致；
- Flutter `test/fixtures/emo_protocol/strict_v2/`：canonical fixture 根目录；fixture 必须存在且
  SHA-256 匹配。服务端仓库可维护等价测试数据，但不得改变 wire shape。

本契约包从 Flutter strict-v2 vertical slice 提取；r18 使用 `2.8.0`，保留待机 Context、启动 ensure、
远程 prepare、设备级远程音量、control/applied 分离、远程事务 terminal 结算与 localUser
playback.update 服务端版本分配，并把 Broadcast 固定为 source PlaybackContext 的派生镜像：start 与
控制不再接受第二套播放内容/cursor，source 使用普通 Context transaction/update，ordinary
participants 使用 revision-aware feedback、原 Context 冻结/恢复和 source-aware reconnect。服务端和
Flutter 实现及 fixtures 都必须更新，并通过 conformance tests 保持一致：

- `lib/services/emo_action_contract_policy.dart`：strict 出站 allowlist、envelope type、字段验证；
- `lib/services/emo_realtime_client.dart`、`emo_socket_connection.dart`：Socket transport、握手、ACK、runtime provenance；
- `lib/services/emo_message_router.dart`：入站 action 路由与 quarantine；
- `lib/services/emo_strict_v2_models.dart`：context、queue、control、feedback 的严格入站解析；
- `lib/services/emo_handoff_controller.dart`、`emo_broadcast_controller.dart`：Handoff / Broadcast 时序和 result 读取；
- Flutter `test/fixtures/emo_protocol/strict_v2/manifest.json` 与同目录 fixtures：客户端 target
  inventory 与可验证证据，不是服务端部署实现或 production readiness 证明。

最终 r18 requirement 的证据必须在
[r18 requirement mapping](../../../docs/verification/emosonic_strict_v2_r18_requirement_mapping.md) 中按以下
五个维度分别记录，不能用其中一项代替另一项：

| 证据维度 | 最低内容 |
| --- | --- |
| 服务端 schema | validator、serializer、metadata/allowlist 与 server fixtures 对最终字段和 one-of 的证明 |
| 服务端状态机 | persistence、transaction、fence、timeout、restart、idempotency 与 routing 的可复现结果 |
| Flutter parser/controller | 入站 parser、出站 policy、controller/lease/recovery 与 Flutter fixtures 的结果 |
| 自动化测试 | 双方针对当前冻结 revision 的测试命令、case 名与成功输出 |
| Android+Windows 真机 | 同一最终 r18 组合的注册、Core、Follow/Handoff/Broadcast、断线/重启/恢复日志 |

任一维度缺失时对应 REQ 保持 `Contract defined / implementation pending`。旧自动化可以作为回归基础，
但不得继续保留没有最终 schema/state/client/device 覆盖的 `Verified`。

仍需由服务端部署和真实设备提供的非代码证据是：授权规则的 conformance 结果、单 worker
启动保护、Context 持久化与重启结果、Android/Windows 双客户端日志、发布与回滚演练记录。协议规则
本身已在本契约包固定；缺少部署证据时必须保持对应 capability 未就绪，不得自行改写 envelope、
correlation、provenance、cursor、routing 或 fail-closed 语义。

冻结不修改协议版本协商，也不改变显式调试环境中 profile implementation readiness 默认 `true` 的
便利行为；该开发开关不能作为 production capability 或上述五类证据。冻结后的行为变化进入 r19 并
重新评估 protocolVersion；不改变行为的文字修正才可作为 r18 errata。
