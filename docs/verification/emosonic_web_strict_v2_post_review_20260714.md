# EmoSonic Web strict-v2 复审测试文档

日期：2026-07-14（Asia/Shanghai）

对象：`/player` 网页播放器、`/control` 网页控制台、浏览器 OTP 认证、
PlaybackContext strict-v2 共享客户端及服务端路由。

## 1. 测试目标

本轮测试验证代码复审后的修复不会破坏 legacy 或 strict-v2 既有行为，重点覆盖：

1. 反向代理 HTTPS 下的浏览器 OTP 同源校验；
2. 以 `browser-otp:` 开头的真实账户密码兼容性；
3. 多播放器并存时反馈和 Handoff 响应不会串单；
4. 请求超时后的合法迟到响应不会触发协议错误；
5. settlement 历史有界，不会随运行时间无限增长；
6. 大播放队列按批次请求元数据，不生成超长 URL；
7. Context、Follow、Broadcast 和 Handoff 在目标媒体缺失时不会播放旧音频；
8. Handoff prepare/commit、Broadcast paused 状态和设备重置的 Context 生命周期安全；
9. 普通部署只有同时开启 development mode 和本地测试证据开关时才允许联调；
10. legacy 页面、strict-v2 服务端 profile、文档和发行包保持可用。

## 2. 测试环境

- Python：3.13.11
- Node.js：18.20.8
- Sphinx：9.1.0
- 测试框架：Python `unittest`、Node 内置 test runner
- 数据库：测试用例创建的临时 SQLite 数据库

测试不得读取或改写生产媒体库、生产数据库或私有 `supysonic.conf`。

## 3. 自动化测试

### 3.1 复审修复定向测试

```bash
python -m unittest \
  tests.base.test_emo_web_strict_v2 \
  tests.frontend.test_web_strict_v2
```

预期：21 项测试全部通过。

覆盖 OTP、代理 Origin、密码回退、精确模板行为、元数据分批、旧媒体保护、
Handoff 校验、设备重置和 Broadcast paused 状态。

### 3.2 测试部署 readiness 门禁

```bash
python -m unittest \
  tests.base.test_config \
  tests.base.test_emo_strict_v2_conformance \
  tests.base.test_emo_strict_v2_readiness \
  tests.base.test_emo_strict_v2_safety \
  tests.base.test_emo_strict_v2_core.StrictV2CoreTestCase.test_non_testing_deployment_requires_dual_local_evidence_gate
```

预期：36 项通过，1 项平台相关测试跳过。

重点检查默认关闭、仅开启一个开关仍 fail-closed、双重门禁允许正常 Strict V2
注册，以及启用 Core 后的单进程约束。

### 3.3 共享 JavaScript 客户端

```bash
node --test tests/js/emo_strict_v2_client.test.js
```

预期：18 项测试全部通过。

重点检查 request settlement、迟到响应 tombstone、连接 provenance、Context cursor、
settlement 历史上限及 owner lock。

### 3.4 strict-v2 与 legacy 回归

```bash
python -m unittest \
  tests.emo_legacy_suite \
  tests.base.test_emo_strict_v2_core \
  tests.base.test_emo_strict_v2_follow \
  tests.base.test_emo_strict_v2_handoff \
  tests.base.test_emo_strict_v2_broadcast
```

预期：174 项测试全部通过。

### 3.5 完整 Python 回归

```bash
python -m unittest
```

预期：完整测试套件通过；允许仓库中已有的显式 skip。

### 3.6 文档、语法和发行包

```bash
node --check supysonic/static/js/emo_strict_v2_client.js
node --check tests/browser/emo_web_strict_v2_acceptance.js
sphinx-build -M html docs docs/_build
python -m build --no-isolation
git diff --check
```

预期：JavaScript 语法、Sphinx HTML、wheel、sdist 和 Git 空白检查全部通过。

## 4. 真实浏览器验收

浏览器验收脚本会启动隔离的本地服务器和临时媒体库，不使用生产数据：

```bash
node tests/browser/emo_web_strict_v2_acceptance.js
```

前置条件：Node 能加载 `playwright`，且 Chromium、Firefox 浏览器二进制可用。

验收矩阵：

- Chromium 桌面 `/player`；
- Firefox 桌面 `/player`；
- Chromium 移动视口 `/control`；
- 双播放器 Context、Follow、Broadcast 和 Handoff；
- 30 次前台 Handoff 音频起播计时，绝对误差均不超过 200 ms；
- 页面无未捕获异常，strict 错误不自动降级到 legacy。

复审后还需特别人工确认：

1. 删除目标曲目或令媒体加载失败时，目标播放器保持停止且不播放上一首音频；
2. Handoff prepare 返回 `ready:false`，commit 不启动错误媒体；
3. paused Broadcast 快照在参与端显示 paused；
4. 有活动 Broadcast/Handoff，或离线但仍有关联 Context 时，重置设备被拒绝；
5. 1000 首队列的元数据请求被拆分为每批最多 50 首。

## 5. 本次执行记录

| 检查项 | 结果 |
| --- | --- |
| 复审修复定向 Python 测试 | PASS，21 项 |
| 测试部署 readiness 门禁 | PASS，36 项，跳过 1 项 |
| JavaScript 客户端测试 | PASS，18 项 |
| strict-v2 与 legacy 回归 | PASS，174 项 |
| 完整 Python 回归 | PASS，1223 项，跳过 3 项 |
| Sphinx HTML | PASS |
| wheel / sdist | PASS |
| JavaScript 语法与 `git diff --check` | PASS |
| 修复后的真实浏览器验收 | 未重跑：当前 Node 环境缺少 `playwright` 模块 |

2026-07-13 的浏览器验收结果保留在
`docs/verification/emosonic_web_strict_v2_20260713.md`，但它属于本轮复审修复前的基线，
不能替代修复后的浏览器复验。

## 6. 通过标准

合并前至少满足：

- 第 3 节全部通过；
- 没有新增未解释的 warning、日志泄密或协议降级；
- 生产默认仍为 `emo_web_realtime_protocol = legacy`，可选 Web profile 默认关闭；
- 若要启用 Follow、Broadcast 或 Handoff，必须补跑第 4 节并保存新的浏览器证据。
