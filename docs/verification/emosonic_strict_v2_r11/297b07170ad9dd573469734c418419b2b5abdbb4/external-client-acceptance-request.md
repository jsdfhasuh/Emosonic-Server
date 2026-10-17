# strict-v2 r11 外部客户端验收交接单

这份文件只说明还缺什么测试。服务端已经完成，不需要 Flutter 或 Windows 工程师重新审查服务端实现。

固定信息：

- 协议：strict-v2 `2.4.0 / r11`
- 服务端 build：`297b07170ad9dd573469734c418419b2b5abdbb4`
- 契约 SHA-256：`4bf1a099fd3c060514215c202b7bb3c82b80e9c73959c39782541d8cda9dea96`
- 服务端自动化结果：`PASS`
- production readiness：仍为 `false`

## Flutter 工程师需要返回什么

请用 Flutter 自动化测试证明下面四件事：

1. 第一次收到 `playback.control.settled` 时，按
   `playbackContextId + epoch + commandControlVersion` 找到 pending 命令，只结束这条命令；
2. 再收到完全相同的 settled 时直接忽略，不重复提示、不重复刷新、不再次改变 UI；
3. 同一个三字段键收到不同 `status` 或 `errorCode` 时，保留第一次 terminal，记录协议冲突并请求最新
   status，不能用第二条覆盖第一条；
4. 较旧版本的 settled 可以补齐旧 pending，但不能把更新版本的歌曲、播放状态、位置或 applied cursor
   回滚。

测试消息必须包含 `requestingClientId`，且它等于最初发命令的 Flutter 控制端。不要使用
`sourceClientId` 代替。

请返回：

- Flutter build/commit；
- 测试命令；
- 测试退出码和通过数量；
- 一份日志，至少能看到同一个 `playbackContextId`、`epoch`、`commandControlVersion` 的首次处理、
  重复忽略、冲突处理和旧版本不回滚结果。

建议日志文件名：`flutter-settled-idempotency.log`。

## Windows 工程师需要返回什么

先模拟一条音频加载超过 15 秒的远程命令，然后证明：

1. Windows 使用命令携带的 `executionTimeoutMs:15000`，不是自己另写一个超时；
2. 15 秒到达后，对应 execution lease 已经失效；
3. 原音频任务在第 17 秒或更晚返回时，不能切歌；
4. 迟到 callback 不能发送 committed；
5. 迟到 callback 不能修改 Context、队列投影或系统媒体状态；
6. 后续更高 `commandControlVersion` 的命令仍能正常执行；
7. 收到服务端 settled 后，会按 Context、epoch、command version 使本地 lease 失效、从队列删除事务，
   并阻止迟到 committed；
8. 断线、Socket 替换或重启后，不会自动重放结果不明的 next、previous、seek 等命令。

只有同时证明第 2—5 项时，Windows 才能发送 `execution_timeout`。如果当前音频层做不到，请明确返回：

```text
hard timeout not ready
Windows does not send execution_timeout
server watchdog settles execution_unknown
```

这不是测试造假或失败隐藏，而是契约要求的安全降级；此时 production readiness 必须继续为
`false`。

请返回：

- Windows build/commit；
- 测试命令；
- 测试退出码和通过数量；
- 一份带时间点的日志，至少包含 0 秒接收、15 秒 lease 失效、17 秒以后旧 callback 返回、没有副作用、
  更高版本命令成功。

建议日志文件名：`windows-execution-lease.log`。

## 日志如何对齐

两边日志都保留下面几个值即可：

```text
playbackContextId
epoch
commandControlVersion
requestingClientId
status / errorCode
client build or commit
server build commit
```

不要写密码、Token、Cookie 或完整认证请求。

拿到两份真实日志后，把它们放进本目录，再更新 Goal 检查表。没有日志前不能把 production readiness
改成 `true`。
