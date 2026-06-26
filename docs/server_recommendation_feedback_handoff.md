# EmoSonic 推荐反馈后端对接说明

## 目标

Flutter 端已经把“热门推荐 -> 歌曲三点菜单 -> 不再推荐”从纯本地隐藏升级为“本地优先 + 后端同步”的反馈流。

用户点击“不再推荐”后，客户端会立即隐藏该歌曲，同时通过 Subsonic 风格接口把反馈提交给后端。后端需要把这个反馈纳入 `getRecommendedPlaylists` 的生成过程，避免同一用户持续看到已经明确不喜欢的推荐歌曲。

## 当前 Flutter 端已实现的行为

相关文件：

- `lib/providers/providers.dart`
- `lib/data/services/subsonic/subsonic_api_client.dart`
- `lib/ui/screens/discovery_screen.dart`
- `lib/ui/widgets/song_actions_sheet.dart`

当前推荐请求仍保持不变：

```text
GET /rest/getRecommendedPlaylists.view?count=<N>&u=<user>&t=<token>&s=<salt>&v=1.16.1&c=emosonic&f=json
```

Flutter 兼容两种推荐响应结构：

```json
{
  "playlist": {
    "coverArt": "cover-id",
    "entry": []
  }
}
```

```json
{
  "recommendedPlaylists": {
    "playlist": {
      "coverArt": "cover-id",
      "entry": []
    }
  }
}
```

当前“不再推荐”流程：

1. 用户在热门推荐歌曲的三点菜单点击“不再推荐”。
2. Flutter 先把 `song.id` 写入当前服务器和用户名作用域下的本地 disliked 列表。
3. `effectiveHotRecommendedResultProvider` 立即过滤这些 song id，因此 UI 会马上隐藏。
4. Flutter 写入 pending feedback outbox，然后后台调用后端接口。
5. 后端同步成功后，客户端移除 pending 项。
6. 后端暂不可用、网络失败或返回 failed 时，客户端保留 pending 项，后续自动重试。
7. Snackbar 显示“已从热门推荐隐藏”，用户可点击“撤销”。
8. 撤销会恢复本地展示；如果 dislike 可能已经同步到后端，则补发 `restore`。

本地存储现在按当前服务器和用户隔离，避免多账号或多服务器间 song id 串扰。旧版本全局 key `disliked_recommended_song_ids` 会迁移到当前 server/user 的 v2 key，并删除旧 key，防止后续切账号时重复迁移。

## Flutter 提交反馈接口

Flutter 已接入以下接口：

```text
POST /rest/setRecommendationFeedback.view
```

认证参数沿用现有 Subsonic query 参数。业务参数也放在 query params 中，body 为空 JSON `{}`，这样更贴近现有 Subsonic 服务端参数读取方式。

示例请求：

```text
POST /rest/setRecommendationFeedback.view
  ?id=song-123
  &action=dislike
  &scope=hot_recommended
  &reason=user_dislike
  &source=emosonic
  &u=<user>
  &t=<token>
  &s=<salt>
  &v=1.16.1
  &c=emosonic
  &f=json
```

恢复示例：

```text
POST /rest/setRecommendationFeedback.view
  ?id=song-123
  &action=restore
  &scope=hot_recommended
  &reason=user_dislike
  &source=emosonic
  &u=<user>
  &t=<token>
  &s=<salt>
  &v=1.16.1
  &c=emosonic
  &f=json
```

字段语义：

- `id`：反馈目标 ID。旧 Flutter 当前传歌曲 ID；扩展动作可传 artist id、album id 或 genre 文本。
- `action`：旧 Flutter 当前发送 `dislike` / `restore`；服务端还支持 `dislike_song`、`restore_song`、`hide_artist`、`restore_artist`、`hide_album`、`restore_album`、`like_more`、`not_this_style`、`restore_style`。
- `targetType`：可选。支持 `song` / `artist` / `album` / `genre`；省略时服务端会根据 action 推导。
- `scope`：当前固定为 `hot_recommended`。
- `reason`：当前固定为 `user_dislike`。
- `source`：当前固定为 `emosonic`，方便后端区分来源。

`id` / `targetId` 最长 128 字符。Web Agent 对曲库外歌手提交 `hide_artist` 时会使用清洗后的歌手名，最多 120 字符。`hide_artist` 可以传本地 Artist UUID，也可以传外部/Agent 歌手名；服务端过滤本地曲库时会对歌手名做大小写不敏感和空白规范化匹配。
Subsonic API 和 Web 登录态反馈接口都兼容 `id`、`targetId`、`target_id` 三种目标字段名；旧 Flutter 继续发送 `id` 即可。提交成功的响应会同时返回 `id`、`targetId`、`target_id`，方便客户端复用同一解析逻辑。

Flutter 会把 Subsonic failed 响应、HTTP 错误、网络异常都视为同步失败，并保留 pending outbox 等待重试。

## 建议后端响应

成功响应建议保持 Subsonic 风格：

```json
{
  "subsonic-response": {
    "status": "ok",
    "version": "1.16.1",
    "recommendationFeedback": {
      "id": "song-123",
      "action": "dislike",
      "scope": "hot_recommended"
    }
  }
}
```

恢复成功：

```json
{
  "subsonic-response": {
    "status": "ok",
    "version": "1.16.1",
    "recommendationFeedback": {
      "id": "song-123",
      "action": "restore",
      "scope": "hot_recommended"
    }
  }
}
```

失败响应可以沿用现有错误结构：

```json
{
  "subsonic-response": {
    "status": "failed",
    "version": "1.16.1",
    "error": {
      "code": 70,
      "message": "invalid recommendation feedback action"
    }
  }
}
```

建议错误语义：

- 未登录或 token 无效：沿用现有鉴权错误。
- `id` 为空：返回 failed，不要写入。
- `action` 不支持：返回 failed，不要写入。
- 歌曲不存在或已删除：推荐返回 ok 并记录该 ID 的反馈，避免客户端反复重试；不要因为缺失歌曲导致 500。
- 重复 dislike：应幂等返回 ok。
- 重复 restore：应幂等返回 ok。

## 推荐数据模型

建议表名：`user_recommendation_feedback`

最小字段：

```text
id                  bigint / uuid primary key
user_id             当前 Subsonic 登录用户
song_id             歌曲 ID，和 getRecommendedPlaylists entry.id 一致
target_type         song / artist / album / genre
target_id           反馈目标 ID
action              dislike_song / hide_artist / hide_album / like_more / restore*
scope               hot_recommended
source              emosonic / web / api
reason              user_dislike 或其他未来原因
created_at          创建时间
updated_at          更新时间
deleted_at          可空，restore 时软删除
```

建议唯一约束：

```text
unique(user_id, target_type, target_id, scope)
```

推荐语义：

- `dislike` / `dislike_song`：对 `(user_id, song, song_id, scope)` upsert 一条有效记录，`deleted_at = null`。
- `hide_artist` / `hide_album` / `not_this_style`：对对应 target upsert 一条有效记录，后续推荐过滤该 target。
- `like_more`：对 song target upsert 一条有效记录，后续推荐把该歌曲的流派、艺人作为额外种子。
- `restore*`：移除或软删除对应 target 的有效记录。推荐软删除，方便后续做用户反馈分析。
- 反馈是用户级偏好，不是全局黑名单。
- 当前 scope 只影响热门推荐，不影响搜索、专辑、歌单、收藏和播放。
- 写入成功会记录低敏 `recommendation event=feedback_updated` 日志，只包含用户、action、targetType、scope、source 和是否 restored，不记录具体 target id。

## getRecommendedPlaylists 需要怎么改

服务端生成推荐时建议按以下顺序处理：

1. 根据原有算法生成候选歌曲，候选数量应大于请求的 `count`，例如 `count * 3` 或固定上限 200。
2. 查询当前用户在 `scope=hot_recommended` 下有效的 song / artist / album / genre 反馈。
3. 从候选列表中过滤 disliked song、hidden artist、hidden album 和 hidden genre；hidden artist 既支持本地 Artist UUID，也支持匹配本地曲库艺人名的外部歌手名。
4. 去重。
5. 尽量补足到请求的 `count`。
6. 返回现有 `playlist.entry` 结构，并附带 `recommendReason`。
7. 写入低敏 `recommendation event=playlist_served` 日志，记录请求数量、返回数量、反馈过滤数量、补足数量和各类反馈计数，不记录歌曲标题或歌曲 ID。

关键点：

- 不要先截断再过滤，否则用户隐藏几首歌后，返回数量会明显不足。
- 如果过滤后不足 `count`，优先继续补候选；确实不足时可以返回较少结果。
- 如果全部被过滤，返回空 `entry` 即可，Flutter 已能处理空状态。
- `coverArt` 应来自过滤后的结果，避免使用已经被过滤掉的歌曲封面。
- 推荐过滤只对当前鉴权用户生效，不能把 A 用户反馈硬过滤到 B 用户。

## Pending outbox 和重试行为

Flutter 有本地 pending feedback outbox，保存字段包括：

```text
songId
action
scope
reason
serverId
username
createdAt
retryCount
```

重试触发点：

- 点击“不再推荐”后立即尝试同步。
- 点击“撤销”后必要时同步 `restore`。
- 应用启动或 provider 初始化时。
- 离线模式切回在线时。
- active server/user 变化时，只重试当前 server/user 的 pending 项。
- 刷新热门推荐时会做轻量重试。

后端暂未上线时，Flutter 不会频繁打扰用户；本地仍会隐藏，失败只记录日志并保留 pending 项。

## 服务端反馈拉取接口

服务端现在已支持反馈拉取接口。Flutter 如果要做多设备同步，可以在应用启动、切换服务器、切换用户、刷新热门推荐页时调用一次，并把返回的服务端 disliked 与本地 disliked 合并。

```text
GET /rest/getRecommendationFeedback.view
```

请求参数：

```text
scope=hot_recommended
u=<user>
t=<token>
s=<salt>
v=1.16.1
c=emosonic
f=json
```

响应建议：

```json
{
  "subsonic-response": {
    "status": "ok",
    "version": "1.12.0",
    "recommendationFeedback": {
      "scope": "hot_recommended",
      "dislikedSongIds": ["song-123", "song-456"],
      "hiddenArtistIds": ["artist-123"],
      "hiddenArtistNames": ["External Agent Artist"],
      "updatedAt": "2026-06-25T09:48:00"
    }
  }
}
```

注意：

- `dislikedSongIds` 只包含当前登录用户仍有效的 song 级 dislike。
- `hiddenArtistIds` 只返回能解析为本地曲库 Artist 的 id；Agent 对曲库外歌手的 `hide_artist` 反馈不会混入该字段。
- `hiddenArtistNames` 只返回不能解析为本地 Artist UUID 的外部歌手名，用于同步 Agent “不感兴趣”反馈。
- `hiddenAlbumIds`、`hiddenGenres`、`likedMoreSongIds` 会返回对应扩展反馈。
- 用户提交 `restore*` 后，再次拉取时对应 target id 会消失。
- 没有有效反馈时，JSON formatter 可能省略空数组字段。
- 当前 Flutter 仍可以保留本地 disliked 作为兜底；服务端拉取用于补齐多设备同步。

## 后端实现建议

后端第一阶段需要完成：

- 实现 `POST /rest/setRecommendationFeedback.view`。
- 实现 `GET /rest/getRecommendationFeedback.view`。
- 参数从 query params 读取；可同时兼容 JSON body，但 Flutter 当前不依赖 body。
- 认证沿用现有 Subsonic 参数。
- 对 `dislike` / `restore` 和扩展动作做幂等处理。
- 在 `getRecommendedPlaylists` 生成结果时，按当前用户和 `hot_recommended` scope 过滤有效 disliked song、hidden artist、hidden album 和 hidden genre。
- 过滤应发生在截断到 `count` 之前，并在过滤后尽量补足请求的 `count`。

后端后续可选增强：

- 支持更多 scope，例如 `daily_mix`、`similar_songs`。
- 将 dislike 作为推荐算法负反馈，而不只是硬过滤。

## 隐私和产品边界

推荐反馈是用户偏好数据，需要按用户隔离。

建议：

- 不把 disliked song ids 写进公开日志。
- 不把 A 用户反馈用于 B 用户的硬过滤。
- 可以用于全局算法统计，但只能使用聚合信号。
- “不再推荐”不等于“不允许播放”，搜索、专辑页、用户歌单、收藏、历史记录都不应被该规则过滤。

## 验收标准

后端完成后，建议用以下用例验收：

1. 用户 A dislike `song-123` 后，刷新 `getRecommendedPlaylists` 不再返回 `song-123`。
2. 用户 B 仍可以在推荐里看到 `song-123`。
3. 用户 A restore `song-123` 后，推荐算法允许再次返回该歌曲。
4. 重复提交同一首歌的 dislike 返回 ok，不产生重复有效记录。
5. 重复提交 restore 返回 ok，不导致 500。
6. 后端临时不可用时，Flutter 会本地隐藏并保留 pending；后端恢复后能收到补发的 dislike。
7. 用户点击“不再推荐”后马上点“撤销”，后端最终状态应为 restore 或无有效 dislike。
8. `count=50` 时，过滤少量歌曲后仍尽量返回接近 50 首。
9. `getRecommendedPlaylists` 保持现有响应结构，旧 Flutter 版本不崩溃。
10. 歌曲不存在或已删除时，提交 dislike 不导致服务端 500。
