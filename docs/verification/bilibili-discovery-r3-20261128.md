# B 站音乐发现 R3 隔离验证

## 固定输入预演

使用合成的 B 站历史、国区目录响应和临时 SQLite 曲库。上海来源日为
2026-11-27，候选对照和预建歌单日期为 2026-11-28。预演用同一测试用户、
同一曲库、17 条原有播放和同一个历史快照，调用实际发现处理与候选合并入口。

| 指标 | 实测 |
| --- | ---: |
| 可见观看记录 | 9 |
| 模拟提取调用 | 1 |
| 原池 | 150 |
| 原池内保留来源标注 | 3 |
| 池外新增 | 2 |
| 原池尾部移除 | 2 |
| 合并池 | 150 |
| 测试播放记录 | 17 → 17 |
| 测试数据库订单与模拟 Oracle 提交 | 0 |

《潮痕》来自观看标题里明确写到的海岸乐团现场版。曲库里也有录音室版，
只有 UUID 末尾为 `00a2` 的现场版入池。《夜渡潮汐》来自 R&B 编曲线索，
由曲库里两位不同艺人的三天复听支持。模拟目录的歌曲 `9201` 对应本地
UUID 末尾 `00a1`。两首已在库且可播放。原池的 150 首均标为国语流行，
合并池为国语流行 148、另类音乐 1、R&B 1，艺人数量为 30 → 32。

被标记不喜欢的测试录音被排除。《灯塔》仅在曲库里找到录音室版，所需的
混音版无法在模拟国区目录确认。《北窗的灯》匹配模拟国区 song ID `9101`，
发行 `9100` 的曲序为第 1 至第 12 首，本地核对为 0 首。自动下载关闭时
该候选保持 pending。另有一条被判定为非音乐的观看记录。

预建歌单 UUID `00000000-0000-0000-0000-0000000002be` 及曲序
SHA-256 `481e9315c63b5d5cf582754d12265c394b8a4f51886078cb09a209e945468bc3`
在预演前后相同。预演进程阻断网络连接，历史同步与读取均由同一模拟服务
返回，生产历史、歌单、账本和订单没有被访问。预演的排名与 token 未测。
另有模拟排序回归覆盖 150 首排序后选 50 首、池外新增最终入选 0 首的情况。
整专回归用模拟 Oracle 回执、临时 FLAC 文件和 SQLite 索引，验证已远端完成
但未收件、只索引 11/12 首、完整入库、多个目标分别映射及已验证文件失效。

完整输入摘要、曲目与证据见同目录的 `bilibili-discovery-r3-20261128.json`。

## 复现

```powershell
$env:OPENSPEC_TELEMETRY = '0'
npm exec --yes --package=@fission-ai/openspec@2.2.0 -- openspec validate bilibili-discovery-in-emosonic --strict
python -m tests.bilibili_rehearsal
```

## 回归命令

```powershell
python -m unittest -v `
  tests.base.test_album_discovery_service `
  tests.base.test_album_discovery_context `
  tests.base.test_album_discovery_candidates `
  tests.base.test_album_discovery_public_catalog `
  tests.base.test_album_discovery_daily `
  tests.base.test_album_discovery_delivery `
  tests.base.test_album_discovery_quota `
  tests.base.test_openai_recommendations `
  tests.base.test_openai_daily_recommendations `
  tests.base.test_daemon_recommend_refresh `
  tests.base.test_db_layer_contract `
  tests.base.test_emo_schema_migration `
  tests.base.test_bilibili_settings `
  tests.base.test_bilibili_discovery `
  tests.base.test_bilibili_discovery_orders `
  tests.base.test_bilibili_discovery_recommendation
```

数据库迁移回归使用现有 CI 的 `Database Migrations` 任务。该任务在隔离数据库
验证新建、旧 schema 升级、索引与约束，并在输出中记录引擎版本。仅在本地
运行时，需要向 `tests.base.test_emo_schema_migration` 提供专用的空白测试库
`SUPYSONIC_TEST_MYSQL_URI` 和 `SUPYSONIC_TEST_POSTGRES_URI`。测试会重置这些库。

## 回归结果

- `openspec validate bilibili-discovery-in-emosonic --strict` 通过。复跑的预演
  JSON 与留存结果逐字节一致。
- Windows Python 3.11.14 定向回归运行 202 项。200 项通过，MySQL 和
  PostgreSQL 两项在本地跳过并由 CI 补跑。SQLite 3.51.1 建库与升级通过。
- [CI 数据库迁移](https://github.com/jsdfhasuh/Emosonic-Server/actions/runs/46103239021/job/140850190066)
  在提交 `88a525d` 上通过 9 项，实跑 MySQL 8.4.5 和 PostgreSQL 16.9。
  验证了两种引擎从空库建表和从 `20261127` 升级到 `20261128`。测试包含
  原账户、曲库、订单及歌单保留、唯一键、外键删除行为和长快照存取。
- Windows 额外运行 `python -m unittest` 对照基线 `3889e6b` 与初版候选。
  基线 2606 项，初版候选 2633 项，两次均有 10 项失败、75 项错误、
  11 项跳过，失败与错误的测试名称完全相同。现象包括临时日志文件锁、
  测试路径差异和协议文档被检出为 CRLF 后的固定哈希不匹配。
- Python 3.9 在原基线复现了旧 RPC 断言对 `socket.timeout` 的失败。
  提交 `88a525d` 将断言限定在两种套接字超时类型，保留其余检查。
  修正后本地 Python 3.9 定向回归 39 项通过，本地 Python 3.11 RPC
  回归 4 项通过。
- [候选 CI](https://github.com/jsdfhasuh/Emosonic-Server/actions/runs/46103239021)
  全部通过。在 Linux Python 3.9 和 3.12 上各运行全量 2646 项，跳过 4 项。
  两种版本均另运行 5 项网络回归，跳过 1 项。两种版本的打包和 CI 的
  Docker 构建以及镜像元数据检查均通过。

## 真实数据待核对

生产历史源的上海日期边界和同步完整度、真实国区目录的命中与版本、真实
付费排序及账本、Oracle 接单和 OpenWrt 收件与索引、用户实际听感有待实测。
