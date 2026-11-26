# 普通远控替换队列并点播

本增补扩展 r19 的 5.2、5.2.2 和 6.7 节，适用于个人实验室部署。沿用
`2.9.0` 注册和现有 `canPlay` 能力。旧请求无需更改。仅看注册版本无法判定对端是否实现本增补。

控制端发送 `type:"command"`、`action:"queue.playItem"`，必需字段仍为
`playbackContextId`、`queueIndex`、`baseQueueRevision`、`baseControlVersion`。
可选字段 `queueSongIds` 是新的完整有序目标列表，含 1–1000 个互异的有效曲目 ID；
`queueIndex` 必须落在该列表中。省略列表时仍按原规约选择现有队列中的曲目。
两条路径都要求同用户 controller、唯一在线 authority、有效物理连接、`canPlay`
和精确匹配的两个游标。跨设备请求始终写入所选 authority 的 Context。

提供列表时，可从 idle 或已有队列发起单次替换并点播。服务端在持久化控制事务中记录
完整列表、索引和预期 queueRevision，接受时仅推进 version 和 controlVersion。
只有 authority 收到包含候选列表的命令。所有公开 Context/queue 快照继续给出原队列。
服务端向 authority 发送已有的第 6.7 节命令，完整列表的执行必须保序。旧 Flutter
authority 开启随机播放时可能重排队列，需使用已修正保序的客户端。

相关 ACK 表示服务端已接受并路由命令。服务端收到同一 Context、epoch 和权威连接对
该命令的 remoteCommand committed 反馈，确认 appliedControlVersion、正在播放的曲目及状态后，
在同一事务写入完整列表、索引、采样位置和播放状态。此时 version 再加一，
queueRevision 比请求基准加一，controlVersion 保持已分配值。服务端发布
`playback.update`、`queue.context.sync` 和 `playback.context.status`。控制端
核对所选 authority 的已执行版本、完整列表与索引。消息之间不要求到达顺序。

同一 Context 有其他待决控制或准备事务时拒绝替换。待决替换与普通控制、队列写入、
空队列初始化互斥。authority 在原队列完成的 localUser 操作可按原规约覆盖它。
明确失败、超时、断线和重启恢复均保留原队列；候选列表不会作为 canonical 广播。
已分配的 controlVersion 不复用，迟到的成功反馈不能重新提交已结束的命令。

旧服务端的闭合 schema 拒绝带此字段的请求。控制端应报告失败，不得丢弃字段
并对旧队列重发。此增补只定义替换并点播，追加、插队、广播和移交继续沿用各自的协议。
