# Mood Scene Playlists Productization Goal

> Goal: 将已经完成的 `TrackMetadata` 语义媒体数据真正产品化，形成用户可见、可播放、每日自动更新、自动清理、可保存、可被音乐 Agent 使用的情绪歌单能力。

---

## 0. Code Baseline

本 goal 基于当前分支：

```text
agent/track-metadata-goal-v2
```

代码查阅后确认当前已有基础：

| 能力 | 当前代码位置 | 当前状态 |
|---|---|---|
| 情绪/场景歌单内存结果 | `supysonic/mood_scene_playlists.py` | 已有 `get_mood_scene_playlist()`，支持 night / study / commute / relax / high_energy / low_energy / cantonese / nostalgic / emo |
| 首页智能卡片 | `supysonic/home_smart_cards.py` + `supysonic/templates/home.html` | 已接入首页 `Smart picks`，但不是完整情绪歌单页面 |
| 用户画像聚合 | `supysonic/user_listening_profile.py` | 已能根据 high-quality `TrackMetadata` 聚合 mood / scene / tags / language / energy 等 |
| Agent 上下文 | `supysonic/recommendation_agent.py` | `build_recommendation_agent_context()` 已包含 `listeningProfile`，但 prompt 和页面展示还可加强 |
| Playlist 数据结构 | `supysonic/db_layer/playlists.py` | `Playlist.comment` 可用于标记系统生成来源，`Playlist.tracks` 存储 track id 列表 |
| 播放列表页面 | `supysonic/frontend/playlist.py` + `supysonic/templates/playlists.html` | 普通 Playlist 管理已存在，系统情绪歌单需要纳入展示/跳转策略 |
| daemon 定时任务 | `supysonic/daemon/server.py` | 已有 `recommend-refresh` 和 `track-metadata-enrichment` 的 scheduler 模式，可复用 |
| 配置默认值 | `supysonic/config.py` | `DAEMON` 可新增 mood scene playlist 配置项 |

当前缺口：

- `get_mood_scene_playlist()` 只返回内存结果，还没有创建真实 `Playlist`。
- 还没有独立 `/mood-playlists` 页面。
- 情绪歌单没有每日自动刷新和过期自动删除机制。
- 用户画像还没有展示到 `/user/me` 或 Agent 页面。
- Agent 虽然已有 `listeningProfile`，但 prompt 还没有明确要求用画像解释推荐歌手。

---

## 1. Product Decision

### 1.1 系统每日情绪歌单

情绪歌单应定义为“系统托管的每日歌单”：

- 每天根据最新曲库、最新 `TrackMetadata`、用户反馈偏好重新生成。
- 同一天同一个用户同一个场景只保留一个系统歌单。
- 第二天生成新的每日歌单。
- 过期系统歌单自动删除。
- 用户普通歌单不受影响。

### 1.2 用户保存副本

如果用户觉得某天的情绪歌单好听，应支持“保存为我的歌单”：

- 保存时复制当前系统歌单的 tracks。
- 保存后的 Playlist 不再使用系统清理前缀。
- 后续每日清理不会删除用户保存副本。

### 1.3 质量门控

继续沿用 `track_metadata_quality.py` 的规则：

- 情绪歌单主结果只使用 high-quality LLM metadata。
- local provider 不作为主依据。
- low-confidence LLM metadata 不进入画像和 Agent 判断。
- 结果不足时可以 fallback 到 genre / popularity / recommendation，但必须标注 reason。

---

## 2. Phase 1: Daily Mood Scene Playlist Service

### Task 1.1: 新增服务模块

建议新增：

```text
supysonic/mood_scene_playlist_service.py
```

建议常量：

```python
MOOD_SCENE_PLAYLIST_COMMENT_PREFIX = "mood_scene_playlist:"
SAVED_MOOD_SCENE_PLAYLIST_COMMENT_PREFIX = "saved_mood_scene_playlist:"
DEFAULT_MOOD_SCENE_DAILY_PLAYLIST_LIMIT = 30
DEFAULT_MOOD_SCENE_PLAYLIST_RETENTION_DAYS = 1
```

建议核心函数：

```python
def get_mood_scene_playlist_comment(scene_key: str, day: str) -> str:
    ...


def is_system_mood_scene_playlist(playlist) -> bool:
    ...


def get_daily_mood_scene_playlist_name(user, scene_key: str, day: str) -> str:
    ...


def create_or_update_daily_mood_scene_playlist_for_user(
    user,
    scene_key: str,
    limit: int = DEFAULT_MOOD_SCENE_DAILY_PLAYLIST_LIMIT,
    day: str | None = None,
):
    ...


def refresh_daily_mood_scene_playlists_for_user(
    user,
    limit: int = DEFAULT_MOOD_SCENE_DAILY_PLAYLIST_LIMIT,
    day: str | None = None,
):
    ...


def refresh_daily_mood_scene_playlists(
    limit: int = DEFAULT_MOOD_SCENE_DAILY_PLAYLIST_LIMIT,
    day: str | None = None,
    active_users_only: bool = True,
):
    ...


def cleanup_old_mood_scene_playlists(
    retention_days: int = DEFAULT_MOOD_SCENE_PLAYLIST_RETENTION_DAYS,
    current_day: str | None = None,
):
    ...


def save_mood_scene_playlist_copy_for_user(user, source_playlist):
    ...
```

### Task 1.2: Playlist comment 标记

系统每日歌单必须使用可识别 comment：

```text
mood_scene_playlist:{scene_key}:{YYYY-MM-DD}
```

示例：

```text
mood_scene_playlist:emo:2026-07-07
mood_scene_playlist:night:2026-07-07
```

自动删除时只允许删除：

```text
comment.startswith("mood_scene_playlist:")
```

禁止删除：

- 用户普通 Playlist。
- `comment is None` 的 Playlist。
- `saved_mood_scene_playlist:*` 副本。
- 推荐系统已有 `recommended` / `recommend` Playlist。

### Task 1.3: 创建或更新策略

`create_or_update_daily_mood_scene_playlist_for_user()` 规则：

1. 校验 `scene_key` 是 `list_mood_scene_playlist_keys()` 中的合法 key。
2. 调用 `get_mood_scene_playlist(scene_key, limit, user)`。
3. 如果结果为空，不创建 Playlist，返回 skipped 状态。
4. 查找当天同用户同 scene_key 的系统 Playlist：

```python
Playlist.user == user
Playlist.comment == f"mood_scene_playlist:{scene_key}:{day}"
```

5. 如果存在，更新 `name` 和 `tracks`。
6. 如果不存在，创建新的 Playlist。
7. `public` 默认 `False`。
8. 返回结构包含：

```python
{
    "scene_key": scene_key,
    "status": "created" | "updated" | "skipped",
    "playlist": playlist_or_none,
    "track_count": len(results),
}
```

### Task 1.4: 歌单命名规则

建议内部名：

```text
{username}'s {YYYY-MM-DD} {scene_key} mood playlist
```

页面显示中文标签时使用 `SCENE_PLAYLISTS[scene_key]["label"]`：

```text
2026-07-07 夜晚情绪歌单
2026-07-07 学习情绪歌单
2026-07-07 emo 情绪歌单
```

### Task 1.5: 清理策略

`cleanup_old_mood_scene_playlists()` 规则：

- 默认只保留当天系统情绪歌单。
- 可通过 `retention_days` 保留最近 N 天。
- 删除条件必须同时满足：
  - `playlist.comment` 以 `mood_scene_playlist:` 开头。
  - comment 中日期早于保留窗口。
- 日期解析失败的系统情绪歌单不要直接删除，先跳过并记录 warning。
- 返回结构包含删除数量和跳过数量。

---

## 3. Phase 2: Daemon Daily Refresh And Cleanup

### Task 2.1: 新增 daemon 配置

修改：

```text
supysonic/config.py
```

新增默认配置：

```python
DAEMON = {
    "mood_scene_playlists_daily_refresh": True,
    "mood_scene_playlists_refresh_interval": 300,
    "mood_scene_playlist_size": 30,
    "mood_scene_playlist_retention_days": 1,
    "mood_scene_playlists_active_users_only": True,
}
```

说明：

- 该任务不调用 LLM，只使用已有 metadata，因此默认开启是可以接受的。
- 如部署方担心自动创建 Playlist，可通过配置关闭。

### Task 2.2: 注册 scheduler job

修改：

```text
supysonic/daemon/server.py
```

新增 job：

```text
daily-mood-scene-playlists
```

参考现有 `recommend-refresh`：

- 增加 `self.__lastMoodSceneRefreshDay`。
- 每个自然日只刷新一次。
- 执行顺序：
  1. `refresh_daily_mood_scene_playlists(...)`
  2. `cleanup_old_mood_scene_playlists(...)`
- 日志记录：day、created、updated、skipped、deleted。

建议方法：

```python
def __refresh_mood_scene_playlists_if_needed(self, current_day=None):
    ...
```

### Task 2.3: 活跃用户范围

默认只给活跃用户生成：

- 有 `User_Play_Activity` 的用户。
- 或存在 `user.last_play_id` 的用户。

如果当前用户主动打开 `/mood-playlists` 并点击刷新，则无论是否活跃都可以为自己生成。

---

## 4. Phase 3: Mood Playlists Page

### Task 3.1: 新增前端路由

建议新增：

```text
supysonic/frontend/mood_playlists.py
supysonic/templates/mood_playlists.html
```

路由：

```text
GET  /mood-playlists
POST /mood-playlists/refresh
POST /mood-playlists/<scene_key>/refresh
POST /mood-playlists/<scene_key>/save
```

如果项目路由加载需要显式 import，需要在现有前端初始化位置补充导入。

### Task 3.2: 页面展示

页面标题：

```text
Mood Playlists / 情绪歌单
```

展示所有 scene keys：

```text
night
study
commute
relax
high_energy
low_energy
cantonese
nostalgic
emo
```

每个卡片显示：

- 中文 label。
- 今日系统 Playlist 状态。
- 更新时间或生成日期。
- 歌曲数量。
- 前 6-10 首歌曲。
- 每首歌 reason。
- `刷新今日歌单` 按钮。
- `打开播放列表` 按钮。
- `保存为我的歌单` 按钮。

### Task 3.3: 页面数据来源

页面应同时展示：

- 今日系统 Playlist，如果已存在。
- 如果今日系统 Playlist 不存在，则临时调用 `get_mood_scene_playlist()` 预览结果。
- 点击刷新后创建/更新真实 Playlist。

### Task 3.4: 保存为我的歌单

`POST /mood-playlists/<scene_key>/save` 规则：

- 如果今日系统 Playlist 不存在，先生成。
- 复制当前 tracks 到新的普通 Playlist。
- 新 Playlist 不使用 `mood_scene_playlist:` comment 前缀。
- 建议 comment：

```text
saved_mood_scene_playlist:{scene_key}:{YYYY-MM-DD}
```

- 保存后跳转到普通 Playlist 详情页。

### Task 3.5: 导航入口

新增入口建议：

- 首页 `Smart picks` 标题右侧增加 `Open mood playlists / 打开情绪歌单`。
- `/playlist` 页面顶部增加 `Mood playlists / 情绪歌单` 按钮。
- 顶部导航如有空间，可新增二级入口。

---

## 5. Phase 4: User Listening Profile UI

### Task 4.1: 个人资料页传入画像

修改：

```text
supysonic/frontend/user.py
supysonic/templates/profile.html
```

在 `user_profile()` 中调用：

```python
from ..user_listening_profile import build_user_listening_profile

listening_profile = build_user_listening_profile(user)
```

传给模板：

```python
listening_profile=listening_profile
```

### Task 4.2: 展示听歌画像

在 `profile.html` 增加区块：

```text
Listening Profile / 听歌画像
```

展示：

- `topMoods`
- `topScenes`
- `topTags`
- `topLanguages`
- `averageEnergy`
- `averageValence`
- `averageDanceability`
- `recent7Days`
- `recent30Days`

空状态：

```text
暂无足够听歌数据
```

### Task 4.3: 展示要求

- 不展示复杂 JSON。
- 使用 chip / list 风格，保持和现有 console UI 一致。
- 数值字段为空时显示 `-`。
- local metadata 和 low-confidence metadata 不会被 `build_user_listening_profile()` 统计，页面不需要重复过滤。

---

## 6. Phase 5: Recommendation Agent Integration

### Task 5.1: 强化 system prompt

修改：

```text
supysonic/recommendation_agent.py
```

当前 `build_recommendation_agent_context()` 已包含：

```python
"listeningProfile": listening_profile
```

需要强化 `_build_system_prompt()`：

- 推荐歌手时必须参考 `context.listeningProfile`。
- 结合用户常听 mood / scene / tags / language / averageEnergy。
- 用户问“为什么推荐”时，解释推荐和画像之间的关系。
- 画像不是唯一依据，还要结合 `playHistory`、`history.topArtists`、`history.favoriteGenres`、`currentRecommendationTracks` 和 feedback。
- 不要使用 low-quality semantic metadata。

建议增加类似说明：

```text
When recommending artists, use context.listeningProfile as an explicit signal.
If the user asks why, explain how the recommendation relates to their top moods,
scenes, tags, languages, and energy profile. Do not overfit to one field; combine
profile, play history, current recommendations, and feedback.
```

中文交互时，最终 reply 仍由现有 language 规则控制。

### Task 5.2: Agent 页面展示画像摘要

在音乐 Agent 页面增加：

```text
Agent context / Agent 参考画像
```

显示：

- 常听情绪前三。
- 常听场景前三。
- 常听标签前三。
- 常听语言前三。
- 平均能量。

要求：

- 页面展示的数据与 Agent context 使用同一来源：`build_user_listening_profile(request.user)`。
- 空数据时显示“暂无足够数据”。

### Task 5.3: 用户画像 API

新增 JSON endpoint：

```text
GET /api/me/listening-profile
```

返回：

```json
{
  "trackCount": 12,
  "playCount": 45,
  "topMoods": [],
  "topScenes": [],
  "topTags": [],
  "topLanguages": [],
  "averageEnergy": 46.5,
  "averageValence": 52.3,
  "averageDanceability": 41.0,
  "recent7Days": {},
  "recent30Days": {}
}
```

权限：

- 普通用户只能读取自己的画像。
- 管理员可选支持读取指定用户，但不是第一优先级。

---

## 7. Phase 6: Tests

### Task 6.1: Base tests

新增或更新：

```text
tests/base/test_mood_scene_playlists.py
tests/base/test_daily_mood_scene_playlists.py
tests/base/test_user_listening_profile.py
```

覆盖：

- `get_mood_scene_playlist()` 保持现有行为。
- daily mood scene playlist 创建 Playlist。
- 同一天同用户同 scene_key 重复执行只更新，不重复创建。
- 第二天生成新的每日 Playlist。
- 旧系统 Playlist 按 retention 删除。
- 普通 Playlist 不会被删除。
- `saved_mood_scene_playlist:*` 不会被系统清理删除。
- 结果为空时不创建 Playlist。
- Playlist tracks 顺序稳定。

### Task 6.2: Frontend tests

新增或更新：

```text
tests/frontend/test_mood_playlists.py
tests/frontend/test_user_profile.py
tests/frontend/test_recommendation_agent.py
```

覆盖：

- `/mood-playlists` 可以打开。
- 页面展示所有 scene keys。
- 页面展示歌曲和 reason。
- 点击刷新能创建/更新今日 Playlist。
- 点击保存能创建普通 Playlist 副本。
- 创建后可以跳转到 `/playlist/<uid>`。
- 个人资料页展示听歌画像。
- 无播放记录时页面正常。
- Agent context 包含 `listeningProfile`。
- Agent prompt 明确要求使用画像解释推荐。

### Task 6.3: Daemon tests

新增或更新 daemon/scheduler 相关测试，覆盖：

- `daily-mood-scene-playlists` job 注册。
- 同一天只执行一次刷新。
- 新一天再次刷新。
- job 执行时调用 refresh 和 cleanup。
- 日志/结果包含 created / updated / skipped / deleted。

### 推荐测试命令

```bash
python -m unittest tests.base.test_mood_scene_playlists
python -m unittest tests.base.test_daily_mood_scene_playlists
python -m unittest tests.base.test_user_listening_profile
python -m unittest tests.frontend.test_mood_playlists
python -m unittest tests.frontend.test_user_profile
python -m unittest tests.frontend.test_recommendation_agent
```

如果 daemon 改动较多，追加：

```bash
python -m unittest tests.base.test_daemon
python -m unittest tests.base.test_scheduler
```

---

## 8. Non-goals

本 goal 不做：

- embedding / 向量检索。
- 新的 LLM metadata schema。
- 无痕切换播放。
- 默认开启 daemon LLM 批处理。
- 大规模 UI 重构。
- 修改 `TrackMetadata` 数据库结构。
- 修改现有普通 Playlist 的权限模型。

---

## 9. Acceptance Criteria

完成后必须满足：

- 用户可以打开 `/mood-playlists` 独立情绪歌单页面。
- 页面展示 night / study / commute / relax / high_energy / low_energy / cantonese / nostalgic / emo。
- 用户可以手动刷新今日情绪歌单。
- 系统每天自动创建或更新真实 Playlist。
- 同一天重复刷新只更新，不创建重复 Playlist。
- 第二天能生成新的每日系统 Playlist。
- 旧系统情绪歌单会按 retention 自动删除。
- 普通用户 Playlist 和用户保存副本不会被自动删除。
- 情绪歌单每首歌有推荐原因。
- 情绪歌单主结果只使用 high-quality LLM metadata。
- local provider 和 low-confidence metadata 不污染歌单、画像和 Agent 判断。
- 用户可以在个人资料页看到听歌画像。
- 音乐 Agent 推荐歌手时会使用 `listeningProfile`。
- Agent 能解释推荐与用户听歌偏好的关系。
- 相关 base/frontend/daemon 测试通过。

---

## 10. Suggested Commit Order

1. Add daily mood scene playlist service helpers.
2. Add Playlist comment naming, create/update, cleanup, and save-copy logic.
3. Add daemon config and scheduler job.
4. Add `/mood-playlists` frontend page and refresh/save actions.
5. Add listening profile display on user profile page.
6. Strengthen Recommendation Agent prompt and Agent profile summary UI.
7. Add tests for service, frontend, daemon, and Agent/profile integration.

---

## 11. Ready-to-use Coding Prompt

```text
Implement the mood scene playlists productization goal from
mood_scene_playlists_product_goal.md.

Scope:
- Add a daily mood scene playlist service that creates/updates real Playlist rows
  from get_mood_scene_playlist().
- Mark system-generated daily mood playlists with comment
  mood_scene_playlist:{scene_key}:{YYYY-MM-DD}.
- Refresh daily mood playlists automatically through a daemon scheduler job.
- Automatically delete expired system mood playlists while preserving normal user
  playlists and saved copies.
- Add a /mood-playlists page with scene cards, reasons, refresh actions, open
  Playlist actions, and save-as-my-playlist behavior.
- Display user listening profile on /user/me.
- Strengthen Recommendation Agent prompt and UI so artist recommendations can use
  and explain listeningProfile.

Do not implement embeddings, a new metadata schema, playback handoff, daemon LLM
batch default changes, or a large UI rewrite.

Verify with targeted base/frontend/daemon tests first.
```
