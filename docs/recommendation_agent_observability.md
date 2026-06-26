# Recommendation Agent 可观测性说明

本文档用于服务端和前端联调 Recommendation Agent 健康检查、运行指标和日志。

## 健康检查接口

```text
GET /recommendations/agent/health
```

该接口需要当前 Web 用户登录。返回示例：

```json
{
  "enabled": true,
  "configured": true,
  "model": "gpt-example",
  "apiBaseUrl": "https://api.example.com/v1",
  "lastSuccessAt": "2026-06-25T10:20:00",
  "lastError": null,
  "metrics": {
    "agent_request_count": 3,
    "agent_success_count": 2,
    "agent_error_count": 1,
    "agent_timeout_count": 1,
    "agent_cache_hit_count": 1,
    "agent_latency_ms": 1200,
    "agent_average_latency_ms": 930,
    "agent_payload_size_bytes": 2489,
    "agent_filtered_local_artist_count": 2,
    "agent_last_filtered_local_artist_count": 0,
    "agent_filtered_feedback_artist_count": 1,
    "agent_last_filtered_feedback_artist_count": 1,
    "agent_empty_result_count": 1
  }
}
```

## 字段语义

- `enabled`：配置中是否启用 Agent。
- `configured`：`enabled`、`api_base_url`、`api_key`、`model` 是否都有效。
- `model`：当前配置的模型名。
- `apiBaseUrl`：当前配置的 OpenAI-compatible API base URL。
- `lastSuccessAt`：最近一次成功返回 Agent 结果的时间。
- `lastError`：最近一次失败的错误码、消息、时间和安全的诊断详情。不会包含 API key。
- `metrics.agent_request_count`：Agent 请求次数，包含普通 JSON 和 SSE 流式请求。
- `metrics.agent_success_count`：成功返回结果的次数，包含缓存命中。
- `metrics.agent_error_count`：失败次数，包含配置错误、超时、上游错误和无效响应。
- `metrics.agent_timeout_count`：最终以超时失败结束的次数。
- `metrics.agent_cache_hit_count`：短期结果缓存命中的次数。
- `metrics.agent_latency_ms`：最近一次完成请求的耗时。
- `metrics.agent_average_latency_ms`：当前进程内已完成请求的平均耗时。
- `metrics.agent_payload_size_bytes`：最近一次发往模型的请求 payload 字节数。
- `metrics.agent_filtered_local_artist_count`：累计过滤掉的本地曲库歌手数量。
- `metrics.agent_last_filtered_local_artist_count`：最近一次完成请求过滤掉的本地曲库歌手数量。
- `metrics.agent_filtered_feedback_artist_count`：累计过滤掉的 Agent “不感兴趣”歌手数量。
- `metrics.agent_last_filtered_feedback_artist_count`：最近一次完成请求过滤掉的 Agent “不感兴趣”歌手数量。
- `metrics.agent_empty_result_count`：成功响应但没有可展示推荐歌手的次数。

## 结构化日志

每次 Agent 请求完成后，服务端会写入一条 `recommendation_agent_metrics` 日志，包含：

```text
status
cache_hit
agent_request_count
agent_success_count
agent_error_count
agent_timeout_count
agent_cache_hit_count
agent_latency_ms
agent_average_latency_ms
agent_payload_size_bytes
agent_filtered_local_artist_count
agent_last_filtered_local_artist_count
agent_filtered_feedback_artist_count
agent_last_filtered_feedback_artist_count
agent_empty_result_count
```

失败时仍保留既有的 `recommendation_agent_failed` 和前端 route 层错误日志。指标日志只记录聚合计数、耗时、payload 大小和过滤数量，不记录 API key、模型请求正文、曲目详情或用户私密 token。

## 推荐接口日志

热门推荐接口会写入低敏聚合日志，方便排查反馈过滤和补足行为：

```text
recommendation event=playlist_served user=alice source=playlist requested_count=50 source_track_count=50 returned_count=50 filtered_feedback_track_count=3 backfilled_track_count=3 disliked_song_count=3 hidden_artist_count=1 hidden_album_count=0 hidden_genre_count=0 liked_more_count=1
```

反馈写入接口会记录动作维度，不记录具体 target id：

```text
recommendation event=feedback_updated user=alice target_type=artist action=hide_artist scope=hot_recommended source=emosonic restored=false
```

这些日志不会记录歌曲标题、歌手名、target id、token、密码、salt、API key 或模型请求正文。

## Agent 缓存行为

Agent JSON 接口和 SSE 流式接口共享同一套用户级短期缓存：

- cache key 会包含用户 ID、模型、语言、问题、播放历史紧凑摘要（最多 50 条，含 id/title/artist/album/genre/duration/playCount/playedAt）、当前推荐曲目紧凑摘要（id/title/artist/album/genre/playCount）、反馈摘要和有效上一轮推荐歌手摘要（含 reason、genres、starter tracks、similarTo、confidence、mood 等清洗后字段）。
- 缓存唯一性按 `(user_id, context_hash)` 约束，不能跨用户读写。
- 命中缓存时不会请求上游模型，响应会带 `cache.hit=true`，`agent.cached=true`。
- SSE 命中缓存时会先发送 `status=cached`，再发送 `final` payload。
- `forceRefresh=true` 或 `forceRefresh=1` 会绕过缓存并重新请求模型，JSON 和 SSE 两条路径一致。
- 缓存命中仍会计入 `agent_request_count` 和 `agent_success_count`，并额外增加 `agent_cache_hit_count`。

## Agent 会话记忆

每次成功响应都会保存一条用户级 Agent session，包含用户消息、Agent 回复、推荐歌手、模型、语言和 `contextSummary`。`contextSummary` 会保存当次历史摘要、推荐摘要，以及最多 8 首当前推荐歌曲的紧凑摘要，供页面刷新后的最近会话和后续追问使用。

## Agent 输出结构

Agent 成功响应会返回：

```json
{
  "reply": "string",
  "recommendedArtists": [
    {
      "name": "artist name",
      "reason": "why recommended",
      "genres": ["genre"],
      "starterTracks": ["track"],
      "similarTo": ["local artist"],
      "confidence": 0.82,
      "mood": ["relaxed", "melodic"]
    }
  ],
  "nextActions": [
    "Generate starter tracks",
    "Try a different style"
  ]
}
```

兼容说明：

- 旧模型只返回 `name`、`reason`、`genres`、`starterTracks` 时仍然可用。
- `similarTo`、`mood` 会被清洗为短字符串数组。
- `confidence` 会被限制在 `0` 到 `1`。
- `nextActions` 会作为前端快捷追问按钮优先展示。
