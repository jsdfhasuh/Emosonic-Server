# Goal: strict-v2 r18 Queue 位置与控制版本勘误落地

> 状态：Completed（2026-07-31 审查修正）
>
> 制定日期：2026-07-31
>
> 完成日期：2026-07-31
>
> 协议：strict-v2 `2.8.0`
>
> 契约修订：`2026-07-23-r18`，2026-07-31 Queue 位置规则勘误

## 一、目标

修正 `queue.context.sync` 把自然播放位置变化误判为控制操作的问题：只有
`currentIndex`、该索引对应的当前歌曲或 idle/non-empty 边界变化时，才推进
`controlVersion` 和 authority `appliedControlVersion`。

本 Goal 不修改协议版本、`contractRevision`、Socket action、消息字段、错误码
或数据库结构。

## 二、实施前审计

当前 `mutateStrictPlaybackContextQueue` 的 `control_changed` 同时比较了
`record.position_ms != position_ms`。因此 position-only 或普通队列内容变化伴随
自然进度时，会错误要求 `baseControlVersion` 并推进 control/applied cursor。

其他关键路径已经存在：

1. 每次合法 queue sync 原子推进 `version/queueRevision`。
2. currentIndex、当前歌曲和空/非空边界变化会推进 control/applied。
3. stale cursor 在任何写入前拒绝。
4. pending control 会阻止覆盖性的控制事实变化。
5. Context 与 authority device state 在同一事务中更新并支持完整回滚。
6. requestId 结算缓存会重放成功结果，不重复执行 mutation。
7. `player.seek` 与 `playback.update(origin:"localUser")` 已使用独立控制路径。

权威契约的 cursor 表、客户端 action 表和服务端 queue push 说明仍保留 position
触发 controlVersion 的旧文字，需要与本次勘误同步。

## 三、实施计划

1. 从 queue sync 的 `control_changed` 判定中删除 position 差值，继续保存
   `positionMs` 和 `positionSampledAtServerMs` 事实锚点。
2. 更新权威契约中 3 处旧规则，不改变 wire shape 或协议 metadata。
3. 按勘误第 7 节补齐或强化自动测试：
   - 内容变化伴随自然进度，以及 position-only 变化；
   - currentIndex、同索引当前歌曲和 idle/non-empty 边界；
   - player.seek、localUser seek 与 passive progress；
   - stale cursor、事务回滚、requestId 重放；
   - pending remote control 的 control/applied 差距不被 queue sync 清除。
4. 运行专项模块、完整 unittest 和 diff 检查，记录实际结果。

## 四、完成标准

- [x] position-only queue sync 不要求 `baseControlVersion`。
- [x] 自然进度不推进 control/applied cursor。
- [x] 真实索引、当前歌曲和空/非空边界变化仍推进 control/applied。
- [x] 第 7 节全部场景有自动测试证据。
- [x] 权威契约与运行时规则一致，wire 和数据库 shape 不变。
- [x] 专项测试、完整 unittest 和 diff 检查通过。
- [x] 真机状态保持 `pending user validation`。

## 五、真机状态

本次只完成服务端实现与自动测试。Windows 来源设备和 Android 接收设备的完整
日志复核仍为 `pending user validation`，不得用自动测试替代真机结论。

## 六、实施结果

根因位于 `mutateStrictPlaybackContextQueue`：旧实现把
`record.position_ms != position_ms` 纳入 `control_changed`。该规则来自旧契约，
但与 2026-07-31 勘误冲突，因此当前服务端在修改前确实违反修正后的规则，
不是只有测试缺失。

本次完成：

1. `control_changed` 只比较 currentIndex、当前 track 和 idle/non-empty 边界。
2. position-only sync 仍保存 Context 和匹配 authority/device scope 的位置及采样
   时间，但保留既有 `appliedControlVersion`。
3. control fact 变化仍在同一事务推进 control/applied；pending control、stale
   cursor、事务回滚和 requestId 重放规则保持不变。
4. authority 重连后的 position-only sync 不复用旧连接反馈：保留旧 applied，建立
   隐藏的 `clientSeq=0` 基线，并继续要求新连接 fresh feedback 后才能作为
   Broadcast source。
5. 权威契约的 cursor 表、客户端 action 表和服务端 queue 说明已同步更正。

未修改协议版本、`contractRevision`、Socket action、消息字段、错误码、数据库
结构或 Flutter 代码。

## 七、第 7 节测试证据

| # | 场景 | 自动测试 |
| --- | --- | --- |
| 1 | 追加非当前歌曲并自然推进位置 | `test_queue_sync_natural_progress_keeps_control_and_applied` 的 `append` 子场景 |
| 2 | 仅位置和采样时间变化 | `test_queue_sync_natural_progress_keeps_control_and_applied` 的 `position` 子场景；`test_queue_sync_cursor_matrix_and_closed_push_schema` |
| 3 | currentIndex 变化 | `test_queue_sync_cursor_matrix_and_closed_push_schema` 的 `index` 子场景 |
| 4 | 同 index 的当前 track 变化 | `test_queue_sync_cursor_matrix_and_closed_push_schema` 的 `track` 子场景 |
| 5 | idle/non-empty 边界 | `test_queue_sync_crosses_idle_boundary_with_closed_snapshot` |
| 6 | player.seek、localUser seek、passive progress | `test_all_core_controls_follow_cursor_matrix_and_wire_schema`；`test_local_user_seek_advances_control_after_passive_progress` |
| 7 | stale queue/control cursor 零修改 | `test_queue_sync_advances_only_contract_cursors_and_rejects_stale_base`；`test_queue_sync_cursor_matrix_and_closed_push_schema` |
| 8 | 事务失败完整回滚 | `test_queue_sync_rolls_back_context_and_device_state` |
| 9 | 相同 requestId 重放不二次修改 | `test_queue_sync_ack_backpressure_replays_without_second_mutation` |
| 10 | pending remote control 差距不被清除 | `test_queue_sync_preserves_pending_gap_and_rejects_control_change` |

额外回归覆盖：

- `test_queue_sync_advances_existing_authority_device_state_atomically` 验证真实控制
  变化仍推进 applied，并保留设备 state、playbackRate、volume、muted 和 clientSeq。
- `test_reconnected_source_requires_fresh_feedback_after_queue_sync` 验证重连位置同步
  不伪造 control 进展，Broadcast source 仍要求新连接反馈。

## 八、验证结果

专项命令由上表 11 个 dotted test path 组成：

```bash
python -m unittest \
  tests.base.test_emo_strict_v2_core.StrictV2CoreTestCase.test_queue_sync_natural_progress_keeps_control_and_applied \
  tests.base.test_emo_strict_v2_core.StrictV2CoreTestCase.test_queue_sync_cursor_matrix_and_closed_push_schema \
  tests.base.test_emo_ws_store.EmoWebSocketStoreTestCase.test_queue_sync_crosses_idle_boundary_with_closed_snapshot \
  tests.base.test_emo_strict_v2_core.StrictV2CoreTestCase.test_all_core_controls_follow_cursor_matrix_and_wire_schema \
  tests.base.test_emo_ws_store.EmoWebSocketStoreTestCase.test_local_user_seek_advances_control_after_passive_progress \
  tests.base.test_emo_strict_v2_core.StrictV2CoreTestCase.test_queue_sync_advances_only_contract_cursors_and_rejects_stale_base \
  tests.base.test_emo_ws_store.EmoWebSocketStoreTestCase.test_queue_sync_rolls_back_context_and_device_state \
  tests.base.test_emo_strict_v2_core.StrictV2CoreTestCase.test_queue_sync_ack_backpressure_replays_without_second_mutation \
  tests.base.test_emo_ws_store.EmoWebSocketStoreTestCase.test_queue_sync_preserves_pending_gap_and_rejects_control_change \
  tests.base.test_emo_ws_store.EmoWebSocketStoreTestCase.test_queue_sync_advances_existing_authority_device_state_atomically \
  tests.base.test_emo_strict_v2_broadcast.StrictV2BroadcastTestCase.test_reconnected_source_requires_fresh_feedback_after_queue_sync
```

结果：`Ran 11 tests in 2.139s`，`OK`。

完整命令：

```bash
python -m unittest
```

结果：`Ran 1663 tests in 501.809s`，`OK (skipped=3)`。

`git diff --check` 通过。

## 九、未验证风险

1. 旧 Context 已存在的 `appliedControlVersion < controlVersion` 不会由本修正自动
   结算；仍需检查历史控制事务或使用干净 Context 验收。
2. Flutter 是否按新规则省略 content/position-only sync 的
   `baseControlVersion`，以及本地是否仍阻止 `broadcast.start`，不在本次服务端修改
   范围。
3. Windows 来源与 Android 接收设备的同时间段完整日志和真机行为尚未验证，状态
   保持 `pending user validation`。

## 十、2026-07-31 审查修正

提交 `b1ba67d` 的后续审查发现：authority 以相同 client/device pair 重连后，
如果第一次 `queue.context.sync` 只修改非当前队列内容或完全 no-op，并保持 Context
中的 `positionMs` 不变，服务端不会进入 DevicePlaybackState 的连接作用域重置路径。
旧连接的 `clientSeq>=1` 因此可能被 `broadcast.start` 误当成新连接反馈。

修正结果：

1. 每次携带合法 `connectionNonce` 的已接受 queue sync 都核对持久化
   DevicePlaybackState 的反馈作用域；旧连接反馈存在且 nonce 不同时，保留原
   applied cursor 并建立 `clientSeq=0` 隐藏基线。
2. 同一当前连接的 content-only/no-op sync 直接保留已有设备行，不刷新
   `serverUpdatedAtMs` 或 `positionSampledAtServerMs`，过期反馈不会重新取得群播来源
   资格。
3. 重连后的同位置 content-only 和 no-op sync 都必须等待新连接的 fresh
   `playback.update(clientSeq=1)`，之后 `broadcast.start` 才可接受。
4. 权威契约已删除“position 变化要求 `baseControlVersion`”的残留旧文字。
5. 未修改协议版本、`contractRevision`、Socket action、消息字段、错误码、数据库
   结构或 Flutter 代码。

新增/强化的回归测试：

- `test_same_connection_noop_queue_sync_does_not_refresh_source_feedback`
- `test_reconnected_source_requires_fresh_feedback_after_queue_sync`
- `test_reconnected_source_requires_fresh_feedback_after_noop_queue_sync`

验证结果：

- 关键 4 项：`Ran 4 tests in 1.671s`，`OK`。
- Broadcast 模块：`Ran 310 tests in 43.474s`，`OK`。
- Store/Core 模块：`Ran 134 tests in 14.303s`，`OK`。
- 完整 `python -m unittest`：`Ran 1665 tests in 430.475s`，
  `OK (skipped=3)`。
- `git diff --check` 通过。

Flutter 本地是否仍会阻止 `broadcast.start` 发送，以及 Windows 来源、Android 接收
真机行为，均保持 `pending user validation`。
