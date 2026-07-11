# EmoSonic Server strict-v2 注册握手元数据描述符 Goal

> 目标：让实际部署的 PlaybackContext strict-v2 服务端在 device.register 的成功 ACK 中返回并固定声明：
>
> - protocolVersion
> - schemaHash
> - serverBuildCommit

本 Goal 建立的是一个轻量、可复现的注册握手元数据描述符，不是全量 WebSocket 协议 schema，也不在本次引入所有 realtime action 的运行时 JSON Schema 校验。

---

## 0. 决策记录

采用“轻量元数据描述符”方案：以一个静态、可打包的 JSON 文件描述 strict-v2 的
`device.register` 注册握手，并在对应成功 ACK 中暴露版本、描述符 hash 和服务端构建
commit。

这样 Flutter lab 可以校验它连接的服务端是否理解预期的注册握手 profile，并确认部署来源；
同时不要求为 PlaybackContext、Handoff、Follow、Broadcast 等全部 realtime action 补齐
schema、运行时校验或 serializer 改造。

预计改动为低到中等规模：新增描述符和读取模块，对注册 ACK 增加一个对象，并补充测试、
打包与镜像验证；不涉及数据库、迁移或现有业务状态机。

---

## 1. 决策与边界

### 1.1 本 Goal 解决的问题

strict-v2 客户端完成注册后，需要能从服务端本身取得以下事实：

1. 服务端声明的注册握手 profile 版本；
2. 该 profile 描述符的稳定指纹；
3. 当前服务端镜像所对应的源码提交。

Flutter lab 使用由部署 CI 生成或提供的预期值校验这三项，不能从 Flutter 自身 commit、示例值或测试 fixture 推测服务端身份。

### 1.2 本 Goal 的刻意限制

schemaHash 只标识以下注册握手 profile：

- strict-v2 的 device.register 请求；
- 对该请求的成功 system.ack；
- 对该请求必要的消息信封字段；
- 注册失败时的 system.error 基本信封。

它不标识、更不保证以下内容：

- 所有 PlaybackContext、follow、broadcast、handoff 或 player action 的完整 payload；
- 所有 strict-v2 输出永远不含 sessionId；
- Handoff 的状态机、原子性、serializer 或数据库模型；
- legacy session 协议；
- 服务端对所有 realtime 消息的运行时 JSON Schema 校验。

因此，schemaHash 不是“整个 strict-v2 WebSocket 协议 hash”。它是固定名称的注册握手描述符 hash。后续若要建立全量 strict-v2 契约，必须另立 Goal，并先解决现有 serializer 与 legacy 边界问题。

### 1.3 当前仓库依据

当前仓库已经具备本 Goal 所需的最小基础：

- supysonic/emo/ws.py 定义了 CAPABILITY_PLAYBACK_CONTEXT_V2；
- device.register 已区分 strict-v2 与 legacy；
- strict-v2 注册要求 deviceSessionId 且拒绝注册 payload 中的 sessionId；
- tests/base/test_emo_ws.py 已有认证、注册和读取 ACK 的测试辅助方法；
- Docker Publish 从触发构建的提交检出代码并构建镜像。

当前缺口是：device.register ACK 只返回 client，没有协议版本、描述符 hash 和构建 commit。

已知但不属于本 Goal 的事实也必须在文档中保留：

- 一些 legacy 路径仍使用 sessionId；
- playback.handoff.complete 的现有 ACK 会返回原始 context，可能含 legacy 字段；
- follow、broadcast 和其他 strict-v2 action 不在本描述符的覆盖范围内。

### 1.4 影响判断

本改动应保持为小到中等规模：

- 不修改数据库、迁移或 PlaybackContext 数据结构；
- 不修改 Handoff、Broadcast、Follow 或 legacy session 的业务语义；
- 不修改现有 action 的路由和 serializer；
- 新增一个静态描述符、元数据读取模块、注册 ACK 字段及其验证。

---

## 2. 最终注册 ACK

strict-v2 客户端注册成功后，服务端返回既有 client 信息，并新增 strictV2 元数据：

~~~json
{
  "type": "system",
  "action": "system.ack",
  "requestId": "register-phone-1",
  "payload": {
    "client": {
      "userName": "alice",
      "deviceName": "Alice phone",
      "alias": "Alice phone",
      "clientId": "phone-1",
      "deviceSessionId": "device:phone-1",
      "roles": ["player"],
      "capabilities": {
        "playbackContextV2": true,
        "playbackPrepare": true,
        "effectiveAtPlayback": true
      }
    },
    "strictV2": {
      "protocolVersion": "2.0.0",
      "schemaHash": "<64位小写 SHA-256>",
      "serverBuildCommit": "<完整40位小写 Git SHA 或 unknown>"
    }
  }
}
~~~

规则：

1. 只有 capabilities.playbackContextV2 为 true 的成功注册返回 strictV2。
2. legacy 注册保持现有 ACK 形状，不增加 strictV2。
3. strictV2 对象固定包含上述三项；每项都是非空字符串。
4. client 保持当前序列化和字段行为；本 Goal 不借此承诺 client 的全量字段集合。
5. schemaHash 仅代表第 4 节定义的注册握手描述符。

---

## 3. 三项值的语义与 Flutter lab 校验

| 字段 | 服务端含义 | Flutter lab 的校验方式 |
| --- | --- | --- |
| protocolVersion | 注册握手 profile 的语义版本，例如 2.0.0 | 必须属于 lab 支持的版本集合 |
| schemaHash | 注册握手描述符的 canonical SHA-256 指纹 | 必须匹配该部署 profile 的允许 hash |
| serverBuildCommit | 运行中服务端构建所用的源码 commit | 正式部署时必须等于部署 CI/manifest 提供的完整 SHA |

必须区分协议兼容与部署溯源：

- protocolVersion 和 schemaHash 用于判断 lab 是否理解该注册握手 profile；
- serverBuildCommit 用于确认 lab 连到预期部署，不作为普通客户端的通用协议兼容门槛；
- lab 的预期三项值必须来自受信任的部署 manifest、CI 输出或测试环境配置；
- lab 不得先读取 ACK 再把 ACK 自己当作预期值；
- lab 不得使用 Flutter commit、fixture 或示例 hash 作为服务端 build identity。

当 serverBuildCommit 为 unknown 时，普通本地开发可继续使用；Flutter lab 的“正式部署验证”必须失败，并给出明确诊断。

### 3.1 版本升级规则

protocolVersion 与 schemaHash 各自独立：

- 只改实现、测试、文案或镜像基础设施，且描述符语义不变：两者都不变；
- 描述符覆盖范围、请求/ACK 字段、类型、required、enum、约束或消息信封语义变化：schemaHash 必须变化；
- 向后兼容地新增注册握手描述能力：升级 minor，并由 lab 的允许列表决定是否接受；
- 描述符语义不变的实现或文案修复：可升级 patch，schemaHash 保持不变；
- 不兼容的注册握手变化：升级 major；
- 不能用 serverBuildCommit 代替 schemaHash，也不能用 schemaHash 代替版本规则。

---

## 4. Goal 1：建立唯一的注册握手描述符

新增：

~~~text
supysonic/emo/strict_v2_registration_descriptor.json
~~~

建议结构：

~~~json
{
  "protocolName": "emosonic-playback-context-v2-registration",
  "protocolVersion": "2.0.0",
  "coveredActions": {
    "clientToServer": ["device.register"],
    "serverToClient": [
      "system.ack(device.register)",
      "system.error(device.register)"
    ]
  },
  "schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": {}
  }
}
~~~

### 4.1 描述符覆盖要求

schema 至少描述：

- device.register 的 strict-v2 请求信封和 payload；
- strict-v2 注册必须声明 capabilities.playbackContextV2=true；
- strict-v2 注册的 clientId 与 deviceSessionId 为非空字符串；
- strict-v2 注册 payload 禁止 sessionId；
- 成功 system.ack 的 type、action、requestId、payload.client 和 payload.strictV2；
- strictV2 内 protocolVersion、schemaHash、serverBuildCommit 的字符串形状；
- 注册失败 system.error 的基本信封和 code/message 形状；
- 注册相关消息的 nullable 字段、enum 和必要数值约束。

其中，payload.client 只要求稳定的注册核心字段，并允许当前服务端已有的额外 client 字段；payload.strictV2 必须禁止未知字段。serverBuildCommit 必须匹配 unknown 或 ^[0-9a-f]{40}$，schemaHash 必须匹配 ^[0-9a-f]{64}$。

以下内容必须明确标为未覆盖：

- auth.login 是注册前置条件，但不属于本描述符；
- device.list、queue.context.sync、playback.update、player.*、follow.*、broadcast.*、handoff.*；
- legacy session.*、queue.local.*、queue.session.sync 和旧 serializer；
- 非注册时出现的 sessionId 行为。

该 schema 可以使用本地 $defs 与本地 $ref；除 $schema 元 schema 标识外，不允许外部 $ref，以保证 hash 不依赖网络资源。

### 4.2 描述符 hash 的范围

schemaHash 的输入不是单独的 schema 字段，而是下列稳定对象：

~~~python
fingerprint_source = {
    "protocolName": contract["protocolName"],
    "coveredActions": contract["coveredActions"],
    "schema": contract["schema"],
}
~~~

protocolVersion 不纳入 hash，因为它表达独立的兼容策略。这样覆盖范围改变也会改变 hash，避免“文档 scope 已变但 hash 未变”的错误。

使用：

~~~python
canonical = json.dumps(
    fingerprint_source,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")

schema_hash = hashlib.sha256(canonical).hexdigest()
~~~

输出必须满足：

~~~text
^[0-9a-f]{64}$
~~~

缩进、空格、换行和对象 key 顺序不得影响 hash；覆盖范围、字段类型、required、enum 或约束变化必须改变 hash。

### 4.3 “唯一权威”的准确含义

该 JSON 文件是以下两项的唯一来源：

1. 服务端 ACK 中 protocolVersion 与 schemaHash 的值；
2. 注册握手描述和对应 conformance fixture。

它不是所有 realtime handler 的唯一来源。运行时不因本 Goal 对每一条 WebSocket 消息执行 JSON Schema 验证；本 Goal 只对注册握手添加 descriptor conformance tests。

---

## 5. Goal 2：实现协议元数据模块

新增：

~~~text
supysonic/emo/protocol_metadata.py
~~~

提供：

~~~python
get_strict_v2_registration_descriptor()
get_strict_v2_protocol_version()
calculate_strict_v2_schema_hash(fingerprint_source)
get_strict_v2_schema_hash()
get_server_build_commit()
get_strict_v2_metadata()
~~~

### 5.1 行为要求

- 从与模块同目录的 strict_v2_registration_descriptor.json 读取，不依赖当前工作目录；
- 验证 protocolName、protocolVersion、coveredActions 和 schema 的存在及基本类型；
- 配置或 JSON 损坏时抛出包含文件路径和缺失字段信息的明确异常；
- get_strict_v2_metadata() 返回新的 dict，避免调用者修改缓存对象；
- serverBuildCommit 只从 EMO_SERVER_BUILD_COMMIT 读取，绝不从本地 Git、Flutter 仓库或 fixture 推断。

### 5.2 缓存和环境变量

可以用 functools.lru_cache(maxsize=1) 缓存：

- 已解析的描述符；
- 基于描述符计算的 schemaHash。

不要缓存 get_server_build_commit()。读取一个环境变量的成本可以忽略，这样测试和进程环境覆盖不会得到陈旧 commit。

### 5.3 Python 与依赖

运行时模块只使用标准库，并保持 Python 3.7 可用的写法，因为 setup.cfg 目前声明 python_requires >=3.7。Path(__file__).with_name(...) 可用于定位资源。

JSON Schema 的形式正确性和 registration fixture 校验只在测试中执行。将 jsonschema 加入 ci-requirements.txt，不作为服务端运行时依赖。

---

## 6. Goal 3：修改 strict-v2 注册 ACK

修改：

~~~text
supysonic/emo/ws.py
~~~

当前：

~~~python
_send_ack(request_id, {"client": current_client})
~~~

调整为：

~~~python
ack_payload = {"client": current_client}

if _is_strict_playback_context_v2(current_client):
    ack_payload["strictV2"] = get_strict_v2_metadata()

_send_ack(request_id, ack_payload)
~~~

要求：

- strict-v2 成功注册必须返回三项元数据；
- legacy 成功注册保持当前 ACK 形状；
- 不改变 register 的角色、能力、deviceSessionId 或 sessionId 校验；
- 不触碰 PlaybackContext、Handoff、Follow、Broadcast、session 或 serializer 的业务处理。

---

## 7. Goal 4：注入并验证服务端构建 commit

### 7.1 Dockerfile

增加：

~~~dockerfile
ARG SERVER_BUILD_COMMIT=unknown
ENV EMO_SERVER_BUILD_COMMIT=$SERVER_BUILD_COMMIT
LABEL org.opencontainers.image.revision=$SERVER_BUILD_COMMIT
~~~

运行时从 EMO_SERVER_BUILD_COMMIT 读取 serverBuildCommit。

值规则：

- 未设置、空字符串或不符合完整小写 SHA 的值：返回 unknown，并记录 warning；
- 合法正式值必须满足 ^[0-9a-f]{40}$；
- 本地开发镜像允许 unknown；
- 发布工作流必须在 push 前断言值不是 unknown，且精确等于 github.sha。

### 7.2 Docker Publish

修改：

~~~text
.github/workflows/docker-publish.yaml
~~~

发布工作流不能在唯一的 build-and-push 步骤中先 push 再验证。必须采用以下顺序：

1. 以 SERVER_BUILD_COMMIT=${{ github.sha }} 构建一个本地可运行的验证镜像，push=false 且 load=true；
2. 在该镜像内断言 get_strict_v2_metadata() 的 serverBuildCommit 等于 github.sha，并满足 SHA 格式；
3. 断言镜像的 org.opencontainers.image.revision label 也等于 github.sha；
4. 只有前三步成功，才以相同 build arg 与 OCI labels 运行 build-and-push。

允许验证阶段与发布阶段各构建一次；如需优化可共享 Buildx cache，但不能以“验证已 push 的镜像”代替 push 前验证。

### 7.3 普通 CI Docker Build

修改：

~~~text
.github/workflows/tests.yaml
~~~

普通 CI 应：

1. 用 SERVER_BUILD_COMMIT=${{ github.sha }} 构建 emosonic-server:test；
2. docker run 该本地镜像，并以会失败的 Python assert 验证 runtime metadata；
3. docker image inspect emosonic-server:test，验证 OCI revision label。

验证命令必须在不匹配时返回非零状态，不能只 print 字典。不要依赖未由 CI 创建的 test_supysonic 容器名。

### 7.4 非 Docker 正式部署

若服务端以源码、wheel、systemd 或其他方式部署，部署系统必须显式设置 EMO_SERVER_BUILD_COMMIT 为完整 SHA。未设置时返回 unknown；这类部署不能通过 Flutter lab 的正式部署身份校验。

---

## 8. Goal 5：确保描述符被打包

修改：

~~~text
setup.cfg
MANIFEST.in
~~~

setup.cfg 增加：

~~~ini
[options.package_data]
supysonic.emo =
    strict_v2_registration_descriptor.json
~~~

MANIFEST.in 显式包含：

~~~text
include supysonic/emo/strict_v2_registration_descriptor.json
~~~

必须验证：

1. 仓库源码运行可以读取；
2. 从 wheel 安装后可以读取；
3. sdist 中确实含有该 JSON，且从 sdist 安装后可以读取；
4. Docker 镜像中可以读取；
5. 读取不依赖当前工作目录。

打包改动应运行 python -m build；若运行 python setup.py sdist，还必须接受其生成 man page 的既有行为。

---

## 9. Goal 6：测试

新增：

~~~text
tests/base/test_emo_protocol_metadata.py
tests/base/test_emo_registration_descriptor.py
~~~

### 9.1 元数据单元测试

覆盖：

- protocolVersion 能读取，初始值为 2.0.0，缺失或类型错误时明确失败；
- protocolName、coveredActions 与 schema 的基本结构有效；
- schemaHash 为 64 位小写十六进制；
- 相同 fingerprint_source 重复计算一致；
- key 顺序、空格和缩进变化不影响 hash；
- coveredActions 变化、字段类型变化、required 变化、enum 变化都会改变 hash；
- 合法完整 SHA 原样返回；
- 未配置、空值或非法 commit 返回 unknown；
- 不从 Flutter、fixture 或本地 Git 推断 commit；
- get_server_build_commit 不会因上一测试的环境变量缓存而得到陈旧值。

### 9.2 描述符 conformance 测试

使用 CI-only jsonschema，测试必须：

- 验证 schema 自身可由 Draft 2020-12 validator 检查；
- 验证一个合法 strict-v2 device.register 请求；
- 验证带 sessionId 的 strict-v2 注册请求不符合描述符；
- 验证合法的 strict-v2 注册 ACK；
- 验证 legacy ACK 不被误当作 strict-v2 ACK。

这些测试只覆盖第 4 节的注册握手。不得把它们宣传为全量 realtime 协议校验。

### 9.3 WebSocket 注册测试

修改：

~~~text
tests/base/test_emo_ws.py
~~~

复用现有 connect_authenticated_client、register_device 和 get_ack。

新增断言：

- strict-v2 注册 ACK 有 payload.client 与 payload.strictV2；
- strictV2 等于 get_strict_v2_metadata() 的三个值；
- schemaHash、protocolVersion 和测试环境中的 serverBuildCommit 格式正确；
- legacy 注册成功、保留 sessionId 兼容，且不包含 strictV2；
- 既有 strict-v2 缺 deviceSessionId、携带 sessionId，以及 legacy sessionId 注册测试继续通过。

### 9.4 构建与镜像测试

CI 至少执行：

~~~text
python -m unittest tests.base.test_emo_protocol_metadata
python -m unittest tests.base.test_emo_registration_descriptor
python -m unittest tests.base.test_emo_ws
python -m build
~~~

并在 Docker build job 中运行第 7.3 节的 runtime metadata 与 OCI label 断言。

---

## 10. 文档与 Flutter lab 接入

修改：

~~~text
docs/plans/flutter_emo_realtime_playback_v2.md
~~~

新增或更新注册章节，明确：

- strictV2 是注册握手元数据，不是全量 realtime schema 声明；
- 只有 strict-v2 注册 ACK 返回该对象；
- lab 的三项预期值来自部署 manifest/CI，而非 Flutter 自身；
- unknown 仅允许本地开发，不允许正式部署验证；
- 其它 v2 action 继续遵循现有接入文档，它们不由本描述符 hash 覆盖。

---

## 11. 文件改动范围

### 新增

~~~text
supysonic/emo/strict_v2_registration_descriptor.json
supysonic/emo/protocol_metadata.py
tests/base/test_emo_protocol_metadata.py
tests/base/test_emo_registration_descriptor.py
.github/pull_request_template.md
~~~

### 修改

~~~text
supysonic/emo/ws.py
tests/base/test_emo_ws.py
setup.cfg
MANIFEST.in
ci-requirements.txt                  # 增加 CI-only jsonschema
Dockerfile
.github/workflows/docker-publish.yaml
.github/workflows/tests.yaml
docs/plans/flutter_emo_realtime_playback_v2.md
~~~

### 不修改

~~~text
supysonic/emo/ws_state.py
supysonic/emo/ws_store.py
supysonic/db_layer/emo.py
数据库迁移
PlaybackContext/Handoff 状态结构
legacy session 数据结构
非注册 action 的 serializer 与路由
~~~

---

## 12. 建议提交拆分

### Commit 1

~~~text
Add strict-v2 registration descriptor
~~~

新增描述符、hash 计算、descriptor conformance tests 和 package data 配置。

### Commit 2

~~~text
Expose strict-v2 registration metadata
~~~

在 strict-v2 device.register ACK 中返回三项，并补 WebSocket 兼容测试。

### Commit 3

~~~text
Verify server build identity in container images
~~~

注入 build commit、OCI label、普通 CI 镜像断言与发布前验证。

### Commit 4

~~~text
Document strict-v2 registration handshake verification
~~~

更新 Flutter lab 的信任来源、校验规则和本描述符边界。

---

## 13. Definition of Done

- 存在唯一的 strict_v2_registration_descriptor.json；
- descriptor 明确写出覆盖 action 与未覆盖范围；
- protocolVersion 从 descriptor 读取；
- schemaHash 自动计算，且覆盖 protocolName、coveredActions 与 schema；
- hash 对格式变化稳定、对描述符语义变化敏感；
- strict-v2 注册 ACK 返回三项；
- legacy 注册行为保持兼容；
- 元数据模块不依赖当前工作目录或本地 Git；
- wheel、sdist 与 Docker 镜像都包含 descriptor；
- 普通 CI 镜像中 runtime commit 与 OCI label 都等于 github.sha；
- 发布工作流在 push 前验证镜像 metadata；
- 正式发布镜像不会返回 unknown、短 SHA 或非法 SHA；
- Flutter lab 可从受信任部署 manifest 逐字校验三项；
- 文档明确这是注册握手描述符，而非全量 strict-v2 协议契约；
- Python 3.9 至 3.13 CI 通过，运行时模块保持 Python 3.7 兼容。

---

## 14. 风险与防护

### 注册描述符与代码漂移

- 用 registration request/ACK conformance tests 防止漂移；
- 修改 device.register 或 strictV2 ACK 时，必须同步更新 descriptor；
- PR 模板增加“注册描述符是否需要更新”的检查项；
- 不把未覆盖 action 的行为误写入本 descriptor。

### JSON 未被打包

- setup.cfg 通过 package_data 打进 wheel；
- MANIFEST.in 显式打进 sdist；
- CI 从构建产物和安装后的包读取，而非只在仓库工作树读取。

### 发布镜像身份错误

- Publish Action 显式传入 ${{ github.sha }}；
- push 前在本地 loaded image 中验证 runtime metadata 与 OCI label；
- 验证失败时不执行 push。

### 误把 hash 当作全协议保证

- ACK 和 Flutter 文档均明确 scope；
- 不承诺 handoff、broadcast、follow 或 serializer 的全量一致性；
- 全量契约须另行设计、版本化并完成对应的 runtime/conformance 工作。

---

## 15. 预计改动量

~~~text
registration descriptor JSON       约 80～180 行
protocol_metadata.py               约 70～110 行
ws.py                              约 10～20 行
测试                              约 150～260 行
打包、Docker、workflows、文档      约 60～120 行
~~~

总体判断：

~~~text
开发复杂度：低到中等
业务回归风险：低
数据库风险：无
Handoff/serializer 风险：本 Goal 不触及
CI 工作流复杂度：中等（因必须在 push 前验证）
~~~

---

## 16. 非本 Goal 范围

本次不处理：

- 全量 PlaybackContext strict-v2 JSON Schema；
- 运行时对所有 WebSocket action 的 schema validation；
- PlaybackContext 状态机修复；
- Handoff 原子事务、activate 协议或 raw context serializer 修复；
- session 协议退场；
- Redis 多 worker；
- Broadcast、Follow 的完整 schema；
- 自动生成 Flutter model；
- 数据库存储协议版本；
- 用 Git commit 替代 schemaHash。

本 Goal 只建立 strict-v2 注册握手的协议版本、描述符身份、服务端构建身份和可验证的部署闭环。
