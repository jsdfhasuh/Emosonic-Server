# Track Metadata Application Layer Goal v2

> Goal: 把当前已经落库的 `TrackMetadata` 从“补充字段”升级成 Emosonic 的“语义音乐功能层”。<br>
> 第一批实现必须先完成 **metadata 质量门控**，再进入相似歌曲、场景歌单、筛选、首页卡片和用户画像。

---

## 0. Scope

本文件是 `Track Metadata Enrichment` 已经落地后的应用层后续计划。

它假设当前代码已经具备：

- `TrackMetadata` / `TrackMetadataEnrichmentTask` schema 和 model。
- `supysonic-cli metadata enrich`。
- `LocalMetadataProvider` / `LLMMetadataProvider`。
- daemon 的 `track-metadata-enrichment` 定时任务入口，默认关闭。
- track metadata review UI / inbox 入口。
- 推荐系统读取 `TrackMetadata`。
- Recommendation Agent 上下文包含 `semanticMetadata`。

它不是：

- 新建 enrichment schema 的计划。
- 把 LLM 调用塞回 scanner 的计划。
- 默认开启 daemon LLM 批处理的计划。
- embedding / 向量检索计划。
- 一次性完成全部智能推荐功能的计划。

后续如果把本文件作为 coding goal，推荐只执行 **Phase 0**。Phase 1 之后应拆成独立 goal。

---

## 1. Current Baseline

基于 `/workspace/supysonic` 当前 `master` 工作树核对。当前 HEAD：

```text
749bf5d Show daemon scheduler runs in admin tasks
```

相关近期提交包括：

```text
749bf5d Show daemon scheduler runs in admin tasks
682dbda Expose daemon scheduler run status
3d5adf4 Handle LLM rate limits during metadata enrichment
5381ee6 Stabilize quota abort test ordering
7cd3158 Add track metadata review UI
ae5cc1e Use track metadata in recommendations
7953717 Add track metadata enrichment runner
0c8d175 Add track metadata storage schema
```

关键代码位置：

```text
supysonic/db_layer/track_metadata.py
supysonic/scanner_func/scanner_track_enrich.py
supysonic/scanner_func/scanner_review_tasks.py
supysonic/recommend.py
supysonic/recommendation_agent.py
supysonic/llm_client.py
supysonic/cli.py
supysonic/daemon/server.py
tests/base/test_track_metadata_enrichment.py
tests/base/test_recommend.py
tests/frontend/test_metadata_inbox.py
tests/frontend/test_metadata_review_workspace.py
```

当前已经完成的能力：

- `TrackMetadata` 可保存曲目级 `language / mood / scene / tags / summary / energy / valence / danceability / confidence / provider / source`。
- `TrackMetadataEnrichmentTask` 可记录候选、运行中、失败、重试和完成状态。
- `local` provider 会基于 `genre / year / title / artist / album` 生成弱 metadata。
- `llm` provider 通过 `llm_client.py` 请求 chat completions。
- CLI 支持 `metadata enrich`、`--provider local|llm`、`--dry-run`、`--track-id`、`--failed-only`。
- daemon 支持 `track-metadata-enrichment` job，配置默认关闭。
- 推荐分数已经有 `mood_match / scene_match / tag_match / energy_match`。
- 推荐理由已经能读取 `TrackMetadata`。
- review UI 能展示 track metadata review task。

---

## 2. Current Problems

### Problem A: local provider 会制造低价值 ReviewTask

`LocalMetadataProvider` 只从本地字段生成弱 metadata：

- `tags` 来自 `genre` 和 `year`。
- `summary` 是 `title / artist / album` 拼接。
- `mood / scene / energy / valence / danceability` 基本为空。
- `confidence` 通常是 `0.25` 或 `0.1`。

但 `runTrackMetadataEnrichmentPass()` 当前在写入 metadata 后会直接调用：

```python
createLowConfidenceTrackMetadataReviewTask(track, metadata)
```

结果是：批量跑 local provider 会把大量 `low_confidence` track task 放进 inbox。

### Problem B: ReviewTask helper 的规则没有 provider 门控

`getTrackMetadataReviewIssues()` 目前只看 `confidence`，不看 `provider/source`。

这意味着即使绕开 enrichment pass，后续调用：

```python
createLowConfidenceTrackMetadataReviewTask(...)
createLowConfidenceTrackMetadataReviewTasks(...)
```

仍可能把 local metadata 当成需要人工 review 的低置信度语义结果。

### Problem C: local summary 会覆盖更强的推荐理由

`getRecommendationReason()` 当前优先使用 `_buildTrackMetadataReason(metadata)`。

local provider 的 summary 只是字段拼接，例如：

```text
Song Title by Artist Name from Album Name
```

这类理由可能弱于原有的 genre / artist 推荐理由。

### Problem D: 推荐画像和打分没有 metadata 质量门控

当前推荐链路会读取任意 `TrackMetadata`：

- `_buildMetadataPreferenceProfile()` 会统计 metadata mood / scene / tags / energy。
- `_trackMetadataScore()` 会基于 metadata 参与候选打分。

它们都没有检查：

- `provider/source` 是否来自 `llm`。
- `confidence` 是否达到可用阈值。
- 是否真的存在语义字段。

因此 low-confidence 或 local metadata 会污染语义偏好和候选分。

### Problem E: LLM JSON 解析过严格

`parse_json_object_response()` 当前直接对 message content 执行：

```python
json.loads(content)
```

当上游不支持 `response_format=json_object` 并 fallback 时，模型可能返回：

````text
```json
{...}
```
````

当前实现会把这种响应判为 invalid JSON。

### Problem F: task claim 有并发唯一键风险

`_claimTrackMetadataEnrichmentTask()` 使用 `get_or_create(track=track)`，而 task 表对 `track` 唯一。

如果 CLI 和 daemon 同时 claim 同一首歌，理论上可能触发唯一键冲突。

---

## 3. Quality Policy

### 3.1 Provider 分层

`local provider` 是本地兜底 metadata：

- 可以写入 `TrackMetadata`。
- 可以保存 genre / year 派生出的弱 tags。
- 可以让 CLI / daemon 在无 LLM 配置时可运行、可测试。
- 只能作为最后 fallback 信号。

`local provider` 不应该：

- 创建 `low_confidence` track ReviewTask。
- 进入 inbox 的人工审核队列。
- 覆盖 genre / artist 等强推荐理由。
- 参与 mood / scene / energy 等强语义推荐打分。
- 进入用户听歌画像的核心统计。
- 作为首页智能卡片或场景歌单的主依据。

`llm provider` 是高质量语义 metadata 来源：

- 可用于推荐理由增强。
- 可用于 mood / scene / tag / energy / valence / danceability 打分。
- 可用于相似歌曲、场景歌单、筛选、首页卡片和用户画像。
- 低置信度时才应进入 track metadata review。

### 3.2 High-quality metadata definition

新增统一质量判断，建议放在新模块：

```text
supysonic/track_metadata_quality.py
```

建议 API：

```python
MIN_HIGH_QUALITY_TRACK_METADATA_CONFIDENCE = 0.5

def is_llm_track_metadata(metadata) -> bool:
    ...

def has_semantic_track_metadata(metadata) -> bool:
    ...

def is_high_quality_track_metadata(
    metadata,
    min_confidence: float = MIN_HIGH_QUALITY_TRACK_METADATA_CONFIDENCE,
) -> bool:
    ...

def should_review_track_metadata_confidence(
    metadata,
    confidence_threshold: float = MIN_HIGH_QUALITY_TRACK_METADATA_CONFIDENCE,
) -> bool:
    ...
```

`is_high_quality_track_metadata()` 规则：

- metadata 存在。
- `provider == "llm"` 或 `source == "llm"`。
- `confidence is not None`。
- `confidence >= 0.5`。
- 至少存在一个语义字段：
  - `mood`
  - `scene`
  - `tags`
  - `energy`
  - `valence`
  - `danceability`

`should_review_track_metadata_confidence()` 规则：

- metadata 存在。
- `provider == "llm"` 或 `source == "llm"`。
- `confidence is None` 或 `confidence < 0.5`。
- local metadata 永远返回 `False`。

---

## 4. Phase 0: Metadata Quality Gate

Phase 0 是下一次 coding goal 的推荐范围。

### Task 0.1: 新增统一质量 helper

新增 `supysonic/track_metadata_quality.py`，把 provider、confidence、语义字段判断集中起来。

要求：

- helper 只能依赖 metadata 对象本身，避免反向 import `recommend.py` 或 scanner 模块。
- 对 `None`、缺字段、JSON list 为空、数值字段为空都安全返回。
- 保持阈值常量集中，避免 `0.5` 到处散落。

### Task 0.2: ReviewTask 创建增加 provider 门控

修改：

```text
supysonic/scanner_func/scanner_review_tasks.py
supysonic/scanner_func/scanner_track_enrich.py
```

要求：

- `getTrackMetadataReviewIssues()` 只对 `should_review_track_metadata_confidence(metadata)` 返回 `low_confidence`。
- `createLowConfidenceTrackMetadataReviewTask()` 对 local metadata 不创建 pending task。
- 如果已经存在 local metadata 产生的 pending `low_confidence` task，下次触达时应被 confirm，而不是继续 pending。
- `createLowConfidenceTrackMetadataReviewTasks()` 应过滤或跳过 local metadata，避免未来批量入口重新污染 inbox。
- `runTrackMetadataEnrichmentPass()` 可以继续调用 review helper，但 helper 行为必须已经安全；也可以在 pass 中显式 gate，二者至少保留一个集中规则。

### Task 0.3: 推荐理由增加质量门控

修改：

```text
supysonic/recommend.py
```

推荐理由优先级调整为：

```text
liked-more feedback
top genre
top artist
high-quality metadata reason
popular unplayed
genre fallback
artist fallback
generic fallback
```

要求：

- local metadata summary 不覆盖 genre / artist 理由。
- 只有 `is_high_quality_track_metadata(metadata)` 为真时，才允许 `_buildTrackMetadataReason()` 生成用户可见推荐理由。
- 保留已有批量加载 `trackMetadataById`，不要引入 N+1 查询。

### Task 0.4: 推荐画像和打分增加质量门控

修改：

```text
supysonic/recommend.py
```

要求：

- `_buildMetadataPreferenceProfile()` 只统计 high-quality metadata。
- `_trackMetadataScore()` 对非 high-quality metadata 返回全 0。
- Phase 0 中不要做复杂降权；先用明确的 0/正常参与规则，方便测试和回归。
- 如果后续确实需要 local fallback，应另开路径，不混进 mood / scene / energy 强语义权重。

### Task 0.5: LLM JSON 解析增强

修改：

```text
supysonic/llm_client.py
```

解析顺序：

1. 先尝试 `json.loads(content)`。
2. 失败后尝试提取 fenced JSON code block。
3. 再失败后尝试从文本中提取第一个完整 JSON object。
4. 最终结果仍必须是 dict，否则抛 `LlmInvalidResponseError`。

要求：

- 不接受 JSON array 作为最终结果。
- 提取文本中 JSON object 时要避免简单贪婪 regex 吃掉多余内容；优先用括号平衡扫描。
- 保持错误类型兼容现有调用方。

### Task 0.6: task claim 并发保护

修改：

```text
supysonic/scanner_func/scanner_track_enrich.py
```

要求：

- `_claimTrackMetadataEnrichmentTask()` 捕获 Peewee `IntegrityError`。
- 发生唯一键竞争后，重新查询已有 task，再继续原有 claim/update 流程。
- 如果 task 已经是 `running`，仍返回 `None`。
- 不改变 `attempt_count`、`locked_at`、`next_retry_at` 的现有语义。

### Task 0.7: 测试更新

更新或新增测试：

```text
tests/base/test_track_metadata_enrichment.py
tests/base/test_recommend.py
```

建议覆盖：

- local provider enrich 后仍写入 `TrackMetadata`。
- local provider enrich 后不创建 pending `low_confidence` ReviewTask。
- local metadata 的 `getTrackMetadataReviewIssues()` 返回空。
- 已存在的 local pending `low_confidence` task 在下次触达时被 confirm。
- llm provider 且 `confidence < 0.5` 会创建 pending `low_confidence` ReviewTask。
- llm provider 且 `confidence >= 0.5` 不创建 pending ReviewTask，并能 confirm 旧 pending task。
- local metadata summary 不覆盖 top genre / top artist 推荐理由。
- high-quality llm metadata 能生成 metadata reason。
- low-confidence llm metadata 不参与推荐理由和语义打分。
- `_buildMetadataPreferenceProfile()` 不统计 local metadata。
- fenced JSON 和带前后解释文本的 JSON object 能被解析。
- invalid JSON 仍按原路径标记 `REASON_INVALID_RESPONSE`。
- `_claimTrackMetadataEnrichmentTask()` 遇到唯一键竞争后不失败。

推荐先跑窄测试：

```bash
python -m unittest tests.base.test_track_metadata_enrichment
python -m unittest tests.base.test_recommend
```

如果改动触及 review UI 或 inbox，再追加：

```bash
python -m unittest tests.frontend.test_metadata_inbox
python -m unittest tests.frontend.test_metadata_review_workspace
```

---

## 5. Phase 0 Acceptance Criteria

Phase 0 完成时必须满足：

- local provider 仍能写入 `TrackMetadata`。
- local provider 不会创建 pending `low_confidence` track ReviewTask。
- local metadata 不会进入 low-confidence review bulk 创建。
- local metadata summary 不会覆盖 genre / artist 推荐理由。
- local metadata 不参与 mood / scene / tag / energy 强语义打分。
- high-quality llm metadata 可以生成推荐理由并参与语义打分。
- low-confidence llm metadata 会进入 track metadata review。
- high-confidence llm metadata 会 confirm 旧的 pending low-confidence task。
- LLM 返回 fenced JSON 或带前后文本的 JSON object 时可正常解析。
- CLI 和 daemon 同时 claim 同一 track 时不会因唯一键冲突失败。

---

## 6. Phase 1: Similar Tracks MVP

Phase 1 只能在 Phase 0 完成后开始。

目标：给单首歌返回可解释的相似歌曲结果。

建议新增：

```text
supysonic/similar_tracks.py
```

核心函数：

```python
def get_similar_tracks(track_id, limit: int = 10, user=None):
    ...
```

第一版规则：

- high-quality metadata 正常参与。
- mood / scene 重合高权重。
- tags 重合中权重。
- language 相同中低权重。
- energy / valence / danceability 接近按距离给分。
- genre 相同作为 fallback。
- hidden / disliked track 过滤。
- 结果包含 `score` 和 `reasons`。
- 避免结果全部来自同一 artist。

验收：

- 给定一首歌能返回稳定排序结果。
- mood / scene 相同的歌曲排在前面。
- energy 接近的歌曲排在前面。
- local metadata 不会强行排到前面。
- 无 metadata 时 fallback 到 genre，不报错。

---

## 7. Phase 2: Mood / Scene Playlists

目标：基于 high-quality metadata 生成固定场景歌单。

第一批场景：

- 夜晚
- 学习
- 通勤
- 放松
- 高能量
- 低能量
- 粤语
- 怀旧
- emo

规则：

- 默认只用 high-quality metadata。
- 结果不足时 fallback 到 genre / play_count / random。
- 每首歌返回命中原因。

验收：

- 至少 3 个场景有单元测试。
- 空结果时不报错。
- 每个场景排序稳定。

---

## 8. Phase 3: Metadata Advanced Filtering

目标：让用户按语义字段浏览曲库。

筛选字段：

- language
- mood
- scene
- tags
- energy range
- valence range
- danceability range
- confidence
- provider

默认：

- 只展示 high-quality metadata。
- 隐藏 local provider 低价值结果。

可选开关：

- Include local provider results
- Include low-confidence results

验收：

- 支持组合筛选，例如 `粤语 + 怀旧`。
- 支持数值范围，例如 `energy 20-50`。
- 支持分页。
- 无 metadata 歌曲不报错。

---

## 9. Phase 4: Home Smart Cards

目标：首页展示用户能直接感知的智能推荐卡片。

卡片建议：

- 今晚适合听
- 适合学习
- 粤语怀旧
- 高能量恢复一下
- 最近常听氛围的相似歌曲
- 你可能喜欢的治愈歌曲

规则：

- 优先使用 high-quality metadata。
- local metadata 不作为卡片主依据。
- 用户历史不足时使用通用场景歌单。
- 结果不足时 fallback 到普通推荐系统。

验收：

- 首页至少展示 3 个智能卡片。
- 每张卡片 6-10 首歌。
- 卡片结果有推荐原因。
- 没有用户历史时也能展示。

---

## 10. Phase 5: User Listening Profile

目标：把播放历史转成可解释的用户偏好。

统计内容：

- 常听 mood
- 常听 scene
- 常听 tags
- 平均 energy
- 平均 valence
- 主要 language
- 最近 7 天变化
- 最近 30 天变化

规则：

- 默认只统计 high-quality metadata。
- local provider 不进入 mood / scene 核心画像。
- 播放次数参与加权。

验收：

- 能生成用户画像 dict。
- 推荐系统可复用画像。
- Recommendation Agent 上下文包含画像摘要。
- 测试覆盖播放次数加权。

---

## 11. Phase 6: Natural Language Track Search

目标：让用户可以用自然语言查找本地曲库歌曲。

第一版不调用 LLM，先做关键词映射：

- 晚上 -> scene: 深夜 / 夜晚聆听
- 安静 -> mood: 平静, energy <= 45
- 燃 -> energy >= 70
- 粤语 -> language: yue
- 写代码 -> scene: 专注 / 学习
- emo -> mood: 感伤 / 忧郁

验收：

- 自然语言能转成 metadata filter。
- 结果能解释命中原因。
- 没结果时返回相近推荐。
- 不调用 LLM 也能工作。

---

## 12. Recommended Commit Order For Phase 0

1. Add track metadata quality helper.
2. Gate low-confidence ReviewTask creation by provider/source.
3. Gate recommendation reasons, metadata preference profile, and metadata scoring.
4. Harden LLM JSON parsing.
5. Protect enrichment task claim against unique-key races.

---

## 13. Ready-to-use Goal

后续可以直接用下面这段作为下一次 coding goal：

```text
Implement Phase 0 from ref/track_metadata_application_goal_v2.md only.

Scope:
- Add a shared TrackMetadata quality helper.
- Stop local provider metadata from creating low_confidence track ReviewTasks.
- Ensure local metadata does not override genre/artist recommendation reasons.
- Ensure only high-quality LLM metadata participates in semantic recommendation profile/scoring.
- Harden LLM JSON object parsing for fenced JSON and text-wrapped JSON objects.
- Protect TrackMetadataEnrichmentTask claim against unique-key races.

Do not implement similar tracks, scene playlists, filtering UI, home smart cards,
user profile, natural-language search, embeddings, scanner LLM calls, or daemon
default behavior changes.

Verify with the narrow affected unittest modules first:
- python -m unittest tests.base.test_track_metadata_enrichment
- python -m unittest tests.base.test_recommend
```
