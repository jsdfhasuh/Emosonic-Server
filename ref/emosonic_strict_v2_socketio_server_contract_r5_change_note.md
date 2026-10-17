# EmoSonic strict-v2 Socket.IO r4 → r5 服务端改动说明

> 目标版本：`2026-07-12-r5`
> 协议版本：`2.1.0`（主版本 `2.x`）
> 最终完整契约：`specs/emosonic_strict_v2_socketio_server_contract.md`
> 本文用途：只列出服务端工程师基于 r4 需要补充的差异，不替代完整契约。

## 总体结论

r4 的 capability 协商、Context/closed tombstone 持久化、单 realtime worker、权限模型、
authority client/device 双重绑定、Cursor、Follow/Handoff/Broadcast 状态机均保留。r5 没有重新设计
业务 action，只补齐八项边界和证据规则。

完成本文修改并通过完整契约的 conformance tests 后，可将 r5 作为 `2.1.0` 冻结基线。此前
实验性 `2.1.0` 部署不计入兼容性承诺；r5 冻结后，破坏性变更必须升为 `3.x`。

## 服务端必须修改

### 1. 更新注册描述符和 schemaHash

`device.register` 成功 ACK 的注册描述符必须覆盖：

- `payload.action`；
- `payload.clientId`；
- `payload.deviceSessionId`；
- 完整 9 个 bool 的 `payload.negotiatedCapabilities`；
- `payload.strictV2.protocolVersion`；
- `payload.strictV2.schemaHash`；
- `payload.strictV2.serverBuildCommit`；
- `payload.strictV2.connectionNonce`；
- `payload.strictV2.connectionEpoch`。

上述字段的名称、类型、required、枚举或约束发生变化时，必须更新注册描述符并重新计算
`schemaHash`。不得继续返回基于旧 ACK shape 计算的 hash。`auth.login` 不属于该描述符。

### 2. 加固 connectionNonce

每个物理 Socket 的 nonce 必须：

- 由密码学安全随机数生成器生成；
- 至少包含 128 bit 随机熵；
- 编码为非空 string；
- 新物理连接重新生成；
- 禁止使用时间戳、递增数字、进程 ID 或其他可预测值。

`connectionEpoch` 继续固定为整数 `1`。不得复用旧 nonce。

### 3. 固定 event/state-confirmed 重放

命中 `(connectionNonce, requestId)` 缓存且 fingerprint 相同时，不重新执行副作用或全局广播：

| 重复请求 | 只发给重复请求 Socket | 不得执行 |
| --- | --- | --- |
| `playback.update` | 缓存的 canonical `playback.update` | 不重新写状态，不广播 subscribers |
| `playback.ready` | handoff 当前 `playback.handoff.status` | 不重发 prepare/commit，不推进状态机 |
| `playback.handoff.complete` | completed status + 当前 context status | 不重发 release，不切换 authority，不递增 cursor |

这些 confirmation 都省略原请求的 `requestId`。

### 4. 统一 Handoff errorCode

Handoff `errorCode` 必须匹配：

```text
^[a-z][a-z0-9_]{0,63}$
```

标准值：

- `prepare_failed`
- `prepare_timeout`
- `commit_timeout`
- `target_disconnected`
- `source_disconnected`
- `server_restart`

target 可以在 `playback.ready.ready:false` 中返回符合相同格式的稳定扩展码。`errorMessage` 不得
包含凭据、文件路径、堆栈或内部数据库信息。

### 5. 补齐 Broadcast 初始 participantStates

`broadcast.status.payload.participantStates` 必须覆盖最终 participants，并按 `clientId` 升序。

- 尚未收到 participant feedback：省略 `clientSeq` 和 `serverUpdatedAtMs`，state/position 使用
  BroadcastSnapshot 的初始 canonical 值；
- 收到首个 feedback 后：两字段必须同时出现，且 `clientSeq >= 1`；
- 不得用 `clientSeq:0` 或 JSON null 表示“尚无 feedback”。

### 6. 区分 transport 超限和业务超限

- Engine.IO/WebSocket transport 层发现 message 超过 256 KiB：使用 message-too-big 行为关闭连接，
  不保证返回 `system.error`；
- 消息已进入 business handler 后发现字段、队列、participants 或其他业务限制超限：返回同
  requestId 的 `system.error(code:"bad_request")`；
- 两种情况都不得静默截断 payload。

### 7. 固定集合顺序但保留队列顺序

- `queueSongIds` 保留 canonical 播放顺序，严禁排序；
- roles 固定输出 `player`、`controller` 顺序；
- `participants`、`skippedClientIds` 和服务端生成的 device/client 集合按 `clientId` 升序；
- 所有集合语义数组去重。

### 8. 使用正确的 Flutter conformance 路径

完整契约引用的 Flutter 证据路径为：

```text
test/fixtures/emo_protocol/strict_v2/manifest.json
test/fixtures/emo_protocol/strict_v2/
```

不存在 `ref/manifest.json` 或 `ref/fixtures/strict_v2/`。这些 Flutter fixtures 不是服务端部署
readiness 证明；服务端可在自己的仓库维护等价 fixtures，但不得改变 wire shape。

## 不需要回退的 r4 决策

以下规则保持不变：

- 注册 ACK 返回 `authenticated/userName` 和完整 `negotiatedCapabilities`；
- `connectionEpoch` 固定为 `1`；
- `playback.update.clientSeq` 必需；
- queue sync 在改变 control 域时要求 `baseControlVersion`；
- authority 使用 `clientId + deviceSessionId` 双重绑定；
- active Context 和 closed tombstone 持久化；
- strict-v2 2.x 只允许单 realtime worker；
- Handoff、Follow、Broadcast 只按 negotiated capability 开放；
- release/profile/production capability 不由本文自动开启。

## 服务端验收清单

- [ ] 注册描述符包含 negotiatedCapabilities、nonce/epoch，schemaHash 已重新计算；
- [ ] nonce 使用 CSPRNG 且随机熵不少于 128 bit；
- [ ] 三个 event-confirmed action 的重复请求不会产生重复副作用；
- [ ] Handoff errorCode 格式和标准值通过测试；
- [ ] Broadcast 在尚无 feedback 时不输出 null 或 clientSeq 0；
- [ ] transport 过大关闭连接，business limit 超限返回 correlated bad_request；
- [ ] queueSongIds 顺序在序列化、持久化和重启恢复后保持不变；
- [ ] r5 完整契约的 ACK、error、cursor、routing、重启和双客户端测试通过。

## Flutter 侧后续工作（无需服务端代改）

Flutter 仓库将在协议冻结后单独完成：

- 解析并应用 `negotiatedCapabilities`；
- 校验 `auth.login` ACK 的 `authenticated/userName`；
- queue sync 按 control 域变化携带 `baseControlVersion`；
- 将 `playback.update.clientSeq` 在 validator/manifest 中改为必需；
- 更新 manifest、fixtures 和 r5 conformance tests；
- 完成 Android + Windows 双客户端真实联调。

服务端工程师不应因为 Flutter 当前尚未完成这些适配而回退 r5 wire contract。
