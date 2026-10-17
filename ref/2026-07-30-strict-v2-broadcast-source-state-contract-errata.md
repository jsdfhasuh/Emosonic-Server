# strict-v2 群播源状态契约勘误计划

## 状态

状态：**契约勘误已完成；客户端和服务端实现符合性待后续验证**。

## 问题

strict-v2 r18 对 `PlaybackContext.state`、`DevicePlaybackState.state` 和群播开始条件的说明不够一致：

- 详细群播规则要求以当前 source `DevicePlaybackState.state` 判断是否正在播放；
- `REQ-039` 的文字容易被理解成 PlaybackContext 和 DevicePlaybackState 都必须是 playing；
- status 章节的“applied 追平后两者必须收敛”没有说明收敛范围，按字面会与持续变化的实际位置冲突；
- `queue.context.sync` 没有明确非空队列同步时如何处理原有 Context state；
- 验收清单没有覆盖“Context paused、设备实际 playing”这一合法群播来源。

这导致 Flutter 在发送 `broadcast.start` 前错误要求 Context、Queue 和本机都为 playing，虽然服务器已经
确认当前设备实际为 playing，客户端仍以 `source_not_playing` 超时。

## 预期行为

- PlaybackContext 保存服务器接受的队列、索引、歌曲和控制目标。
- DevicePlaybackState 保存当前设备真正的播放状态、位置和速度。
- `appliedControlVersion == controlVersion` 表示设备已经执行到最新控制版本，不表示实际位置必须等于
  Context 中的控制位置。
- 群播开始只使用 DevicePlaybackState 判断 source 是否正在播放；PlaybackContext 和 Queue 的 state
  不作为额外 playing 门槛。
- source Context 仍必须未关闭、队列非空、authority 在线，设备歌曲必须匹配当前队列项，control/applied
  必须相等且不存在未完成或失败的控制对账。
- passive 更新只更新设备实际事实，不推进 Context cursor；Windows 本地人工操作仍使用 localUser。

## 契约修改

1. 在权威入口记录 2026-07-30 r18 勘误，不改变协议版本和消息格式。
2. 在总览和 status 章节明确 PlaybackContext 与 DevicePlaybackState 的职责和允许差异。
3. 把“必须收敛”改为可验证的版本、歌曲和控制事务规则，明确实际 position/state/rate 可以由等版本
   passive 更新。
4. 明确 `queue.context.sync` 的 state 规则：
   - idle 到非空为 paused；
   - 非空到 idle 为 idle；
   - 非空到非空保留原 Context state；
   - trackId 由 currentIndex 推导。
5. 统一 `broadcast.start`、`REQ-039` 和 readiness 文字：playing 只指当前 source
   DevicePlaybackState。
6. 增加明确验收场景：
   - Context paused、DevicePlaybackState playing、control/applied 相等时允许 start；
   - Context playing、DevicePlaybackState paused 时拒绝 start；
   - 客户端不得仅因 Context/Queue state 不是 playing 而在本地阻止请求。

## 验证

- 搜索完整契约，确认不再存在“Context 和 DevicePlaybackState 都必须 playing”的歧义。
- 检查详细规则、REQ 和集成验收三处语义一致。
- 运行 `git diff --check`。
- 本次不修改 wire fixture、协议版本、schemaHash 或客户端/服务端实现，因此不运行构建。

## 实施结果

- 已在权威入口记录 `2026-07-30 r18 状态来源勘误`。
- 已统一基础状态、队列同步、群播 start、REQ 和集成验收的判断规则。
- 已明确 Context/Queue state 为 paused 而当前 DevicePlaybackState 为 playing 的合法 start 场景。
- 已明确相反场景必须拒绝：Context/Queue state 为 playing，但当前 DevicePlaybackState 为
  paused/stopped。
- 未修改 action、字段、错误码、协议版本、contractRevision、manifest 或任何运行时代码。

## 假设

- 本次作为 strict-v2 `2.8.0` r18 的语义勘误，不新增 action、字段、错误码或状态。
- `test/fixtures/emo_protocol/strict_v2/manifest.json` 的 contractRevision 和
  `server.strictV2Implemented:false` 保持不变。
- Flutter 客户端修复另行实施；本计划只改权威契约。
- 不覆盖工作区中已有的其他修改。
