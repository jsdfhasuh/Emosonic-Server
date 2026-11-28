# Tasks · R3

按以下顺序在服务端工作分支实施。完成的项目勾选并保留对应验证结果。

## 1. 基线与昨日输入

- [x] 读取服务端 AGENTS.md 与本 change。确认普通日推已具备 `3889e6b` 或等效功能；核对原候选、Sol/Luna、国区目录 adapter、Oracle 控制与收件、schema 版本及历史 API。对照同名 change 后合入本版。
- [x] 新增默认关闭的 B 站配置、Run 与 Candidate 两张表、facade、SQLite/MySQL/PostgreSQL base 与 upgrade migration；将直接/衍生证据及来源日存入同一体系。验证唯一键、nullable Track/release、旧账户/曲库/订单/歌单保留。
- [x] 在原 Daemon 注册一个任务；按上海昨日通过现有 sync/query 接口获取可见历史。确认 `details.bilibili`、时区、分页、一致性与成功空日；不完整输入记录原因。快照、offset、已提交批次可在重启后复用。
- [x] 用 fixture 覆盖日期边界、重页、鉴权失败、源端已覆盖事件、相同日重复 tick，以及获取或处理批次时中断的恢复。测试数据使用模拟标题和身份。

## 2. 识别、有限扩展与本地匹配

- [x] 接入已有有预算的结构化 AI 提取。使用临时 ID、最少的必要来源文字与类型；校验编号和证据片段。分别存直接录音与衍生线索；不能把 UP 主当作艺人、把观看当作播放或向模型转交凭证。已提交批次不重复付费，预算/接口失败按既有退避恢复。
- [x] 对直接身份先查本地唯一可播放版本。对合作线索验证共同署名并限制一跳；对风格线索将来源证据与真实复听交叉验证，再使用可用的本地或 CN 目录能力找到具体歌。保留来源日的跨批次探查上限、稳定去重和来源关系。使用不同输入覆盖直接 Live、原唱/翻唱冲突、可核实合作、纯标签无支持、同一命中多路径、无可用目录结果。
- [x] 核验负反馈和版本；反馈排除、歧义、不完整证据及不可读本地文件给出原因。对已在库和明确缺失分别作出可复核的决定。

## 3. 复用国区整专派单与 OpenWrt 入库

- [x] 抽取最小的共享发行保存与派单逻辑。针对明确缺失的具体歌，重查 CN song ID、艺人、版本、album ID、完整曲序与曲库重合项。派单单位为整张发行。跨来源关联同一订单、quota_date、持久 orderId，不生成旧 Selection/Placement 或假播放种子。
- [x] 用隔离测试验证两开关、共享 `aiDiscovery.maxAlbumsPerDay`、零额度、剩最后一格时两个来源并发抢额、同专辑不同目标曲、Oracle 可能已接单但响应丢失。保留原 inspect / close-if-unaccepted 行为。检查已接受/未知结果在开关关闭后仍能核对。
- [x] 扩展已有 `reconcile_orders()` 接受合法的 B 站独有发行，沿用交付清单、文件大小/指纹、完整曲序与索引检查。验证远端完成但未收件、12 首只索引 11 首、全部索引、已 ready 的共享发行、Track 失效，以及各目标 Apple song ID 各自映射到正确本地 UUID。

## 4. 普通日推接入

- [x] 在原 AI 调用前分别保留 baseline 和增强候选。对目标用户合并 14 天内 ready 且仍可播放的 Track；原池重合只标来源。池外新增先填空位，再按设计稳定替换原池末尾未标来源的曲目，可替换数不足则少加。通常 150 首池最多新增 10 个池外 UUID；最终 50 首可无一入选。覆盖满池、未满池、零容量、重复 UUID、小池及运行中失效。
- [x] 按日推配置复用 Sol 详细上下文，保留显式 Luna 的 48 KiB 限制、模型专有缓存/冷却、真实输入计数与共享预算。仅有来源信号时改变增强 prompt/hash。验证无信号/非目标用户、关闭、模型拒绝、原池回退、本地去重与多样性、同日歌单不重算及旧第 4 位投放。其他业务设置沿用现值。

## 5. 隔离预演与交付

- [x] 用同一日、同一用户、同一固定输入与历史快照对照原池和增强池。输出原池数、原池已存在且实际保留的来源、净新增、挤出、合并后数量；至少列一条直接和衍生路径的具体证据、版本、库内命中或 CN 发行、已入库/待处理状态。真实数据没有合格结果时报告 0，另用标明的 fixture 验证成功分支。
- [x] 预演走只读生产输入或脱敏 fixture，写入隔离存储并使用只读/模拟下载接口。真实历史源只用 GET，不调用 POST /api/history/sync；已确认快照可重放，未验证的同步完整性单独标记。保存前后正式歌单 UUID 与曲序摘要，检查新生产订单数为 0。排名如未执行则标记未测；若单独获得真实模型调用授权，复用一次配置的排序并记录实际模型、输入/输出 token、账本金额与最终入选数。原池与增强池的比较不要求两次付费生成。
- [x] 执行定向 unittest，包括受影响的 `tests.base.test_album_discovery_service`、`test_album_discovery_context`、`test_album_discovery_candidates`、`test_album_discovery_daily`、`test_openai_recommendations`、`test_openai_daily_recommendations`、`test_daemon_recommend_refresh`、`test_db_layer_contract`、`test_emo_schema_migration`，以及对应新增用例。跑现有三种数据库的迁移检查，列出实跑引擎与版本；缺失的环境如实记录。
- [x] 运行 `openspec validate bilibili-discovery-in-emosonic --strict`。更新服务端配置样例与短使用说明，提交变更并记录可复现命令、测试结果、预演证据和未实测项。按用户授权区分代码交付、真实调用和生产启用。

预演采用合成输入和临时曲库。验证结果见 docs/verification/bilibili-discovery-r3-20261128.md。真实历史、排序、下载和交付待另行实测。
