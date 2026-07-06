# Track Metadata Enrichment Goal v3

> 基于当前 `main` 分支重新对齐后的实施计划。  
> 本计划不推翻 v2 的方向，而是根据当前代码结构重新校准落地顺序、接入点和风险控制。

---

## 1. Goal

为 Emosonic-Server 新增 **Track Metadata Enrichment（曲目元数据补强）** 能力。

该能力用于在媒体入库之后，为本地曲库中的歌曲补充推荐语义数据，例如：

- `language`：语言
- `mood`：情绪
- `scene`：场景
- `tags`：标签
- `summary`：歌曲简介
- `energy`：能量等级
- `valence`：情绪正向程度
- `confidence`：补强置信度

这些数据后续用于：

- 每日推荐歌单生成
- 推荐理由生成
- Recommendation Agent 上下文
- 智能搜索
- 相似歌曲推荐

---

## 2. 当前 main 分支基线

经过当前 `main` 分支代码审查，Track Metadata Enrichment 目前尚未落地。

当前状态：

```text
supysonic/db.py
    尚未导出 TrackMetadata / TrackMetadataEnrichmentTask

supysonic/db_layer/schema.py
    SCHEMA_VERSION 仍为 20260625

supysonic/cli.py
    尚无 metadata enrich 命令组

supysonic/daemon/server.py
    当前 scheduler 只有 review-task-maintenance 和 recommend-refresh

supysonic/scanner_func/scanner_enrich.py
    findLostInformation() 仍然执行 Album Enrichment / 年份修复 / 封面修复 / 艺术家资料修复

supysonic/db_layer/library.py
supysonic/scanner_func/scanner_records.py
    Track 删除逻辑尚未处理 TrackMetadata / TrackMetadataEnrichmentTask
```

因此，v3 计划应当视为：

```text
基于当前 main 新增一条 Track Metadata Enrichment 链路
```

而不是修改已经存在的 Track Metadata 功能。

---

## 3. 核心结论

v3 不需要重新设计功能方向，但必须重新对齐当前 main 的真实结构。

正确链路：

```text
Track 基础入库
  ↓
TrackMetadata 缺失 / 过期
  ↓
CLI 手动触发
  ↓
runTrackMetadataEnrichmentPass()
  ↓
Provider 生成 metadata
  ↓
写入 TrackMetadata
  ↓
推荐理由先消费
  ↓
Daemon 后置自动化
  ↓
ReviewTask / 前端最后接入
```

最重要的原则：

```text
不要把 Track LLM 补强塞进 Scanner / findLostInformation()
不要一开始就做 Daemon 自动跑
不要一开始就做 Track ReviewTask 前端
不要复制 Recommendation Agent 里的 LLM 请求逻辑
不要默认把本地绝对路径发送给 LLM
```

v3 的实现边界需要进一步收紧：

```text
Phase 0 / 1 只建立数据结构、候选识别、状态机和删除清理。
Phase 2 只提供 CLI 候选展示 / dry-run / 本地 provider，不声明 LLM 闭环完成。
Phase 3 新增公共 LLM client，但不一次性重构 Recommendation Agent 的健康指标、缓存、streaming 逻辑。
Phase 4 才接入 LLMMetadataProvider，且必须显式选择 LLM provider。
```

---

## 4. 当前代码接入点审查

### 4.1 数据库模型导出

当前统一数据库门面文件：

```text
supysonic/db.py
```

项目大部分模块通过：

```python
from ..db import Track, Album, User
```

这种方式导入模型。

因此新增模型后必须在 `supysonic/db.py` 中导出：

```python
from .db_layer.track_metadata import (
    TrackMetadata,
    TrackMetadataEnrichmentTask,
)
```

---

### 4.2 Schema / Migration

当前 schema version 位于：

```text
supysonic/db_layer/schema.py
```

项目初始化数据库时使用：

```text
supysonic/schema/sqlite.sql
supysonic/schema/mysql.sql
supysonic/schema/postgres.sql
```

项目升级数据库时使用：

```text
supysonic/schema/migration/sqlite/
supysonic/schema/migration/mysql/
supysonic/schema/migration/postgres/
```

因此新增表不能只写 Peewee Model，必须同步维护：

```text
supysonic/db_layer/schema.py
supysonic/schema/sqlite.sql
supysonic/schema/mysql.sql
supysonic/schema/postgres.sql
supysonic/schema/migration/sqlite/<new_version>.sql
supysonic/schema/migration/mysql/<new_version>.sql
supysonic/schema/migration/postgres/<new_version>.sql
```

---

### 4.3 Scanner / Enrichment

当前扫描后的修复流程位于：

```text
supysonic/scanner_func/scanner_enrich.py
```

其中 `findLostInformation()` 当前负责：

```text
Album Enrichment
Album year repair
Album cover repair
Artist profile repair
Missing artist image repair
```

Track Metadata Enrichment 不应直接挂到这里，尤其不能在这里调用 LLM。

原因：

- `findLostInformation()` 属于扫描生命周期的一部分。
- LLM 调用慢且不稳定。
- 大曲库扫描时会明显阻塞。
- 网络/API 失败不应影响媒体扫描。

结论：

```text
Track Metadata Enrichment 应独立于扫描执行。
Scanner 最多标记候选，不直接执行补强。
```

---

### 4.4 Track 创建 / 更新接入点

Track 创建与更新集中在：

```text
supysonic/scanner_func/scanner_persist.py
```

核心函数：

```python
createOrUpdateTrack(...)
```

后续如需在扫描阶段标记 pending，可在该函数成功创建或更新 Track 后接入。

但 v3 第一阶段不强制扫描时创建 Task。

优先使用候选收集规则：

```text
TrackMetadata 不存在
Track.last_modification != TrackMetadata.track_last_modification
manual force
Task status=pending / status=retry
```

这样即使历史 Track 没有 Task，也能被补强。

---

### 4.5 Track 删除清理

当前 Track 删除路径包括：

```text
supysonic/db_layer/library.py
    Folder.__delete_hierarchy()

supysonic/scanner_func/scanner_records.py
    removeFile()
```

当前逻辑主要清理：

```text
last_play
annotations
Track
Folder
```

新增 TrackMetadata 后必须同步清理：

```text
TrackMetadata
TrackMetadataEnrichmentTask
后续可能的 Track ReviewTask
```

否则删除文件、删除文件夹、删除 root folder 时会留下孤儿 metadata/task。

实现注意：

```text
不要只依赖数据库级 ON DELETE CASCADE。
当前项目大量删除逻辑由 Peewee / 手写 SQL 路径触发，测试环境虽开启 SQLite foreign_keys，
但跨数据库行为仍应由代码显式保证。
```

`Folder.__delete_hierarchy()` 当前有两段 Track 删除：

```text
1. Track.delete().where(cond)
2. Track.delete().where(Track.folder_id.in_(folder_ids))
```

因此必须在任何 `Track.delete()` 之前先收集完整 affected track ids：

```text
affected_track_ids =
    Track ids matching cond
    UNION
    Track ids under child folder_ids
```

然后统一清理：

```text
TrackMetadata.delete().where(track_id in affected_track_ids)
TrackMetadataEnrichmentTask.delete().where(track_id in affected_track_ids)
ReviewTask.delete().where(entity_type="track", entity_id in affected_track_ids)
```

`removeFile()` 也必须先拿到 track id，再删除该 track 的 metadata/task，最后再
`delete_instance(recursive=True)`。

---

### 4.6 CLI 入口

当前 CLI 位于：

```text
supysonic/cli.py
```

已有命令组包括：

```text
folder
user
```

v3 第一阶段建议新增：

```bash
supysonic-cli metadata enrich
```

而不是修改 `folder scan`。

---

### 4.7 Daemon Scheduler

当前 Daemon Scheduler 注册位置：

```text
supysonic/daemon/server.py
```

当前已有：

```text
review-task-maintenance
recommend-refresh
```

后续可新增：

```text
track-metadata-enrichment
```

但应放在 CLI 稳定之后，并默认关闭，避免升级后自动请求 LLM。

---

### 4.8 Recommendation Agent / LLM 调用

当前 LLM 请求逻辑主要集中在：

```text
supysonic/recommendation_agent.py
```

已有能力包括：

- OpenAI-compatible `/chat/completions`
- `response_format = {"type": "json_object"}`
- timeout
- retry
- response_format 不支持时 fallback
- upstream error parsing
- invalid JSON handling

Track Metadata Enrichment 不应复制一套 LLM 请求代码。

建议新增：

```text
supysonic/llm_client.py
```

然后让：

```text
recommendation_agent.py
scanner_track_enrich.py
```

共用该模块。

抽取策略必须分阶段：

```text
第一步只把 OpenAI-compatible chat/completions 的公共请求、错误解析、
response_format fallback、JSON object 解析抽到 llm_client.py。

Recommendation Agent 现有的 health metrics、cache、session、streaming、
repair prompt、前端错误格式暂时保留在 recommendation_agent.py。

如果后续迁移 Recommendation Agent 到 llm_client.py，必须保持现有测试语义，
尤其是 requests.post patch 点、timeout 统计、response_format fallback 行为。
```

因此 Phase 3 的验收标准不是“大重构完成”，而是：

```text
llm_client.py 有独立单测
scanner_track_enrich.py 可使用 llm_client.py
recommendation_agent.py 行为不回退
```

---

### 4.9 Recommend 接入点

当前推荐逻辑位于：

```text
supysonic/recommend.py
```

当前推荐权重包括：

```python
RECOMMENDATION_SCORE_WEIGHTS = {
    "genre_match": 0.30,
    "artist_affinity": 0.25,
    "album_affinity": 0.10,
    "freshness": 0.10,
    "popularity": 0.10,
    "not_played": 0.10,
    "feedback": 0.05,
}
```

当前 `_scoreRecommendationCandidate()` 主要基于：

```text
genre
artist
album
freshness
popularity
not_played
feedback
```

v3 第一阶段不建议直接改推荐权重。

更稳做法：

```text
先让推荐理由读取 TrackMetadata
再逐步将 mood / scene / energy / tags 纳入打分
```

---

## 5. v3 总体架构

```text
Track
  ↓
TrackMetadata 缺失 / 过期
  ↓
collectTracksNeedingEnrichment()
  ↓
runTrackMetadataEnrichmentPass()
  ↓
Provider
  ├── LocalMetadataProvider
  ├── AlbumMetadataProvider
  └── LLMMetadataProvider
  ↓
TrackMetadata
  ↓
Recommend Reason
  ↓
Recommend Score（后续 Phase）
  ↓
Recommendation Agent（后续 Phase）
```

---

## 6. 数据模型设计

### 6.1 TrackMetadata

新增文件：

```text
supysonic/db_layer/track_metadata.py
```

建议字段：

| 字段 | Peewee 建议类型 | SQL 建议 | 约束 |
| --- | --- | --- | --- |
| `id` | `PrimaryKeyField()` | SQLite/MySQL `CHAR(36)`, Postgres `UUID` | primary key |
| `track_id` | `ForeignKeyField(Track, unique=True, backref="metadata")` | FK to `track(id)` | not null, unique |
| `track_last_modification` | `IntegerField()` | integer | not null |
| `language` | `CharField(max_length=16, null=True)` | varchar(16) | nullable |
| `mood_json` | `TextField(null=True)` | text | JSON list text |
| `scene_json` | `TextField(null=True)` | text | JSON list text |
| `tags_json` | `TextField(null=True)` | text | JSON list text |
| `summary` | `TextField(null=True)` | text | nullable |
| `energy` | `IntegerField(null=True)` | integer | 0-100 or null |
| `valence` | `IntegerField(null=True)` | integer | 0-100 or null |
| `danceability` | `IntegerField(null=True)` | integer | 0-100 or null |
| `confidence` | `FloatField(null=True)` | SQLite `REAL`, MySQL `DOUBLE`, Postgres `DOUBLE PRECISION` | 0.0-1.0 or null |
| `provider` | `CharField(max_length=64, null=True)` | varchar(64) | nullable |
| `model` | `CharField(max_length=128, null=True)` | varchar(128) | nullable |
| `source` | `CharField(max_length=64, null=True)` | varchar(64) | nullable |
| `raw_json` | `TextField(null=True)` | text | raw provider output |
| `created_at` | `DateTimeField(default=now)` | datetime/timestamp | not null |
| `updated_at` | `DateTimeField(default=now)` | datetime/timestamp | not null |

说明：

- `track_id` 建议唯一。
- `track_last_modification` 用于判断文件 tag 更新后是否需要重新补强。
- `mood_json` / `scene_json` / `tags_json` 使用 TEXT 存 JSON。
- `raw_json` 保存原始输出，方便排查。
- 不直接扩展 Track 表，避免主表膨胀。
- 字段命名必须在 Peewee model、三套初始化 schema、三套 migration 中完全一致。
- `created_at` / `updated_at` 可沿用当前仓库中新模型的命名风格；若实现时改成
  `created` / `updated`，三套 SQL 和测试也必须同步。
- SQL 层可加 `ON DELETE CASCADE`，但代码仍必须显式清理，避免不同删除路径行为不一致。
- 建议索引：`track_id` unique、`provider`、`updated_at`。

---

### 6.2 TrackMetadataEnrichmentTask

建议字段：

| 字段 | Peewee 建议类型 | SQL 建议 | 约束 |
| --- | --- | --- | --- |
| `id` | `PrimaryKeyField()` | SQLite/MySQL `CHAR(36)`, Postgres `UUID` | primary key |
| `track_id` | `ForeignKeyField(Track, unique=True, backref="metadata_enrichment_task")` | FK to `track(id)` | not null, unique |
| `status` | `CharField(max_length=32)` | varchar(32) | not null |
| `reason` | `CharField(max_length=64)` | varchar(64) | not null |
| `attempt_count` | `IntegerField(default=0)` | integer | not null |
| `last_error` | `TextField(null=True)` | text | nullable |
| `locked_at` | `DateTimeField(null=True)` | datetime/timestamp | nullable |
| `next_retry_at` | `DateTimeField(null=True)` | datetime/timestamp | nullable |
| `force` | `BooleanField(default=False)` | boolean/tinyint | not null |
| `created_at` | `DateTimeField(default=now)` | datetime/timestamp | not null |
| `updated_at` | `DateTimeField(default=now)` | datetime/timestamp | not null |
| `completed_at` | `DateTimeField(null=True)` | datetime/timestamp | nullable |

`TrackMetadataEnrichmentTask` 是“当前任务状态表”，不是无限增长的 attempt log。
因此 `track_id` 建议唯一。如果后续需要完整历史，应另建
`TrackMetadataEnrichmentAttempt`，不要在第一阶段混入。

状态建议：

```text
pending
running
retry
completed
failed
skipped
```

状态含义：

```text
pending   等待执行
running   已被 worker/CLI claim，locked_at 必须非空
retry     可重试失败，next_retry_at 到期后可再次 claim
completed 已成功写入或确认无需更新，completed_at 必须非空
failed    不再自动重试，需要 manual force 或后续策略恢复
skipped   本次跳过，例如输入不足或 provider disabled
```

reason 建议：

```text
new_track
metadata_missing
tag_updated
manual
manual_force
failed_retry
provider_error
invalid_response
```

注意：

```text
Task 表不是唯一候选来源。
status 表示任务状态，reason 表示进入该状态的原因。
不要使用 failed_retry 作为 status。
```

候选收集必须同时支持：

```text
TrackMetadata 不存在
Track.last_modification != TrackMetadata.track_last_modification
Task status=pending
Task status=retry 且 next_retry_at 到期
manual force
```

建议索引：

```text
track_id unique
status, next_retry_at
status, locked_at
updated_at
```

状态机：

```text
missing/stale metadata -> pending
pending -> running
retry -> running
running -> completed
running -> retry
running -> failed
running -> skipped
failed -> pending only by manual force
```

并发与锁：

```text
claim task 时必须原子地设置 status=running、locked_at=now、attempt_count+1。
如果 running 的 locked_at 超过 stale_lock_seconds，可恢复为 retry。
CLI 和 daemon 可能同时运行，不能只靠进程内锁。
跨 SQLite/MySQL/Postgres 不要依赖 SKIP LOCKED 作为唯一方案。
```

---

## 7. 新增模块设计

### 7.1 scanner_track_enrich.py

新增：

```text
supysonic/scanner_func/scanner_track_enrich.py
```

建议核心函数：

```python
def collectTracksNeedingEnrichment(limit=20, force=False, track_ids=None):
    pass

def runTrackMetadataEnrichmentPass(limit=20, force=False, track_ids=None, provider=None):
    pass

def buildTrackMetadataInput(track):
    pass

def applyTrackMetadataEnrichment(track, enrichment):
    pass

def recordTrackEnrichmentAttempt(track, provider, status, reason=None):
    pass
```

---

### 7.2 llm_client.py

新增：

```text
supysonic/llm_client.py
```

建议抽象：

```python
class LlmClientError(Exception):
    pass

class LlmConfigError(LlmClientError):
    pass

class LlmTimeoutError(LlmClientError):
    pass

class LlmUpstreamError(LlmClientError):
    pass

class LlmInvalidResponseError(LlmClientError):
    pass

def build_chat_completion_payload(...):
    pass

def post_chat_completion(...):
    pass

def parse_json_object_response(...):
    pass
```

目的：

```text
避免 Recommendation Agent 和 Track Metadata Enrichment 各维护一套 LLM 调用逻辑。
```

---

## 8. Provider 设计

### 8.1 LocalMetadataProvider

输入来源：

```text
Track.title
Track.artist
Track.album
Track.genre
Track.year
文件名
脱敏后的文件夹路径片段
```

作用：

```text
构建上下文
提供路径 hint
不依赖外部网络
```

---

### 8.2 AlbumMetadataProvider

输入来源：

```text
Album.year
Album.release_date
Album.release_type
Album.album_info_json
primary_genre
styles
genres
providers_used
```

作用：

```text
复用现有 Album Enrichment 成果
避免重复查询外部源
```

---

### 8.3 LLMMetadataProvider

输入：

```text
title
artist
album
genre
year
album_info_json
path_hints
```

输出严格 JSON：

```json
{
  "language": "zh",
  "mood": ["治愈", "安静"],
  "scene": ["夜晚", "学习"],
  "tags": ["流行", "抒情", "氛围感"],
  "summary": "一首偏安静、适合夜晚播放的抒情歌曲。",
  "energy": 35,
  "valence": 60,
  "danceability": 30,
  "confidence": 0.72
}
```

要求：

```text
LLM 不负责事实信息
LLM 只负责推荐语义
输出必须校验
非法输出不能写入正式 Metadata
raw_json 必须保留
```

隐私边界：

```text
默认不得把 Track.path 的绝对路径发送给 LLM。
默认只允许发送 basename、扩展名、最多 1-2 层经过脱敏的目录 token。
如果未来要发送完整 path_hints，必须新增显式 opt-in 配置。
```

### 8.4 配置来源

LLM provider 的 endpoint/model/key 建议先复用现有：

```text
config.RECOMMENDATION_AGENT
```

但触发条件必须独立：

```text
CLI 必须显式传 --provider llm 才能请求 LLM。
Daemon 必须同时满足 track_metadata_enrichment=true 和 provider=llm 才能请求 LLM。
track_metadata_enrichment=false 只表示 daemon 不自动跑，不代表 CLI 禁止手动请求。
```

如果后续发现 Track Metadata 与 Recommendation Agent 需要不同模型，再新增独立：

```text
TRACK_METADATA_ENRICHMENT
```

不要在第一阶段同时引入两套 LLM 配置，避免配置面过大。

---

## 9. 分阶段实施计划

### Phase 0：数据库与基础模型

目标：

```text
让 Track Metadata 数据结构先稳定。
```

修改文件：

```text
supysonic/db_layer/track_metadata.py
supysonic/db.py
supysonic/db_layer/schema.py
supysonic/schema/sqlite.sql
supysonic/schema/mysql.sql
supysonic/schema/postgres.sql
supysonic/schema/migration/sqlite/<new_version>.sql
supysonic/schema/migration/mysql/<new_version>.sql
supysonic/schema/migration/postgres/<new_version>.sql
```

内容：

```text
新增 TrackMetadata
新增 TrackMetadataEnrichmentTask
导出模型
更新 tests/base/test_db_layer_contract.py 的 facade export / shared model 断言
升级 SCHEMA_VERSION
新增三套 schema
新增三套 migration
验证 MySQL migration 不使用 CREATE INDEX IF NOT EXISTS
```

---

### Phase 1：候选收集与删除清理

目标：

```text
能正确识别哪些 Track 需要补强，并且删除 Track 时不留下孤儿数据。
```

修改文件：

```text
supysonic/scanner_func/scanner_track_enrich.py
supysonic/db_layer/library.py
supysonic/scanner_func/scanner_records.py
```

内容：

```text
实现 collectTracksNeedingEnrichment()
实现 task claim / retry / stale lock 基础状态机
删除 Track 前按 affected_track_ids 统一清理 TrackMetadata
删除 Track 前按 affected_track_ids 统一清理 TrackMetadataEnrichmentTask
为未来 track ReviewTask 预留显式清理点
```

---

### Phase 2：CLI 候选展示 / dry-run / 本地补强

目标：

```text
先通过 CLI 跑通候选收集、dry-run、任务状态更新和 provider 注入。
本阶段不声明 LLM 补强闭环完成。
```

修改文件：

```text
supysonic/cli.py
```

新增命令：

```bash
supysonic-cli metadata enrich --dry-run --limit 10
supysonic-cli metadata enrich --provider local --limit 10
supysonic-cli metadata enrich --track-id <track_id> --force --dry-run
supysonic-cli metadata enrich --failed-only --limit 20
```

要求：

```text
不要修改 folder scan
不要依赖 daemon
不要接前端
不要默认请求 LLM
Click 命令必须通过 click.pass_obj 读取现有 config
实际入口仍依赖 cli.main() 前的 init_database(config.BASE["database_uri"])
```

---

### Phase 3：公共 LLM Client

目标：

```text
抽出通用 LLM 请求能力，避免复制 Recommendation Agent 逻辑。
保持 Recommendation Agent 现有行为和测试稳定。
```

新增文件：

```text
supysonic/llm_client.py
```

后续调整：

```text
scanner_track_enrich.py 使用 llm_client.py
recommendation_agent.py 只做小步适配；不要在本阶段迁移 health/cache/streaming
```

---

### Phase 4：LLMMetadataProvider

目标：

```text
生成并写入歌曲推荐语义 metadata。
```

内容：

```text
构建 track input
仅当 --provider llm 或 daemon provider=llm 时调用 LLM
从 config.RECOMMENDATION_AGENT 读取 endpoint/model/key
默认不发送本地绝对路径
校验 JSON
写入 TrackMetadata
保存 raw_json
更新 task 状态
记录 provider result
非法输出进入 failed / retry
```

---

### Phase 5：推荐理由接入

目标：

```text
先让用户看见推荐理由变聪明。
```

修改文件：

```text
supysonic/recommend.py
```

先不改 score，只改 reason：

```text
因为这首歌和你最近常听的“安静 / 治愈”歌曲接近。
因为这首歌适合“夜晚 / 学习”场景。
```

注意：

```text
读取 TrackMetadata 时要批量加载，避免 N+1 查询。
```

---

### Phase 6：Daemon 后置自动补强

目标：

```text
CLI 稳定后再做后台自动补强。
```

修改文件：

```text
supysonic/daemon/server.py
supysonic/config.py
config.sample
docs/setup/configuration.rst
```

新增配置建议：

```python
"track_metadata_enrichment": False,
"track_metadata_enrichment_provider": "local",
"track_metadata_enrichment_interval": 300,
"track_metadata_enrichment_batch_size": 10,
"track_metadata_enrichment_stale_lock_seconds": 900,
"track_metadata_enrichment_send_path_hints": False,
```

默认：

```text
track_metadata_enrichment = false
track_metadata_enrichment_provider = local
track_metadata_enrichment_send_path_hints = false
```

原因：

```text
避免用户升级后自动请求 LLM。
```

---

### Phase 7：推荐打分增强

目标：

```text
让推荐算法真正利用 TrackMetadata。
```

内容：

```text
用户播放历史中统计 mood / scene / tags 偏好
候选歌曲批量加载 metadata
增加 mood_match / scene_match / energy_match / tag_match
重新调整 RECOMMENDATION_SCORE_WEIGHTS
```

---

### Phase 8：ReviewTask / 前端审核

目标：

```text
支持人工审核低置信度 Track Metadata。
```

暂缓原因：

```text
当前 metadata 前端主要支持 album / artist
Track review 不是简单加 entity_type 即可
```

后续需要新增：

```text
track inbox card
track review detail page
track issue labels
low_confidence / abnormal_result / conflict
confirm / dismiss 行为
可选 NFO 写回
```

---

## 10. 测试计划

### 10.1 数据库 / Migration

测试：

```text
TrackMetadata 可创建
TrackMetadataEnrichmentTask 可创建
TrackMetadata.track_id 唯一
TrackMetadataEnrichmentTask.track_id 唯一
schema version 正确升级
sqlite/mysql/postgres 初始化 schema 都包含新表和索引
sqlite/mysql/postgres migration 都包含新表和索引
supysonic.db facade 导出 TrackMetadata / TrackMetadataEnrichmentTask
db_layer contract 测试仍通过
```

---

### 10.2 候选收集

测试：

```text
metadata 不存在时进入候选
metadata 已存在且 last_modification 未变化时跳过
Track.last_modification 变化时进入候选
force=True 时强制进入候选
Task status=pending 时进入候选
Task status=retry 且 next_retry_at 到期时进入候选
Task status=retry 但 next_retry_at 未到期时跳过
Task status=running 且 locked_at 未过期时跳过
Task status=running 且 locked_at 过期时恢复为 retry 或重新 claim
failed 任务只有 manual force 或 failed-only 明确参数才进入候选
```

---

### 10.3 删除清理

测试：

```text
removeFile 删除单曲时清理 TrackMetadata
removeFile 删除单曲时清理 TrackMetadataEnrichmentTask
Folder.__delete_hierarchy 第一段 cond 删除时清理 metadata/task
Folder.__delete_hierarchy 第二段 child folder_id 删除时清理 metadata/task
删除 root folder 时不留下孤儿 metadata
不依赖数据库 cascade 也能清理干净
```

---

### 10.4 LLM Provider

测试：

```text
正常 JSON 写入 TrackMetadata
非法 JSON 标记 failed
timeout 标记 retry / failed
provider error 不影响后续曲目
confidence 越界会被修正或拒绝
raw_json 被保存
默认 payload 不包含本地绝对路径
默认 path_hints 只包含脱敏 basename / directory token
只有显式 opt-in 才允许完整路径 hint
--provider local 不会调用 requests.post
--provider llm 才会调用 llm_client
```

---

### 10.5 日志

测试日志事件：

```text
track_metadata_enrichment_pass_start
track_metadata_enrichment_provider_result
track_metadata_enrichment_applied
track_metadata_enrichment_no_change
track_metadata_enrichment_track_failed
track_metadata_enrichment_pass_end
```

---

### 10.6 推荐接入

测试：

```text
有 TrackMetadata 时推荐理由使用 mood / scene / summary
无 TrackMetadata 时推荐理由回退旧逻辑
批量加载 metadata，避免 N+1 查询
```

---

### 10.7 CLI / Config / Scheduler

测试：

```text
metadata enrich --dry-run 只输出候选，不写 TrackMetadata
metadata enrich --provider local 可在无 LLM 配置时运行
metadata enrich 默认不请求 LLM
metadata enrich --provider llm 缺少 RECOMMENDATION_AGENT 配置时报清晰错误
Click command 通过 obj config 使用测试数据库
Daemon 默认不注册启用 track-metadata-enrichment job
track_metadata_enrichment=true 但 provider=local 不请求 LLM
track_metadata_enrichment=true 且 provider=llm 才请求 LLM
```

---

## 11. 暂缓事项

以下内容不进入 Phase 0-2：

```text
Daemon 自动跑
前端按钮
Track ReviewTask 页面
推荐权重大改
Embedding 相似歌曲
音频分析 BPM / Key
外部 Last.fm / MusicBrainz track 级补强
NFO 写回
```

---

## 12. v3 最终结论

v3 的落地顺序应为：

```text
Phase 0：数据库与基础模型
Phase 1：候选收集与删除清理
Phase 2：CLI 候选展示 / dry-run / 本地补强
Phase 3：公共 LLM Client
Phase 4：LLMMetadataProvider
Phase 5：推荐理由接入
Phase 6：Daemon 后置自动补强
Phase 7：推荐打分增强
Phase 8：ReviewTask / 前端审核
```

最重要的变化：

```text
不是重新设计功能
而是基于当前 main 分支重新对齐接入点
```

核心原则：

```text
扫描不跑 LLM
CLI 先跑通候选 / dry-run / provider 注入
Daemon 默认关闭
推荐先 reason 后 score
ReviewTask / 前端最后做
```
