# 推荐反馈后端实现说明

本文档给客户端工程师对接 `热门推荐 -> 不再推荐` 使用。

## 已实现能力

服务端已经实现用户级推荐反馈闭环：

- 客户端提交 `dislike` / `dislike_song` 后，服务端会记录当前用户对该歌曲的推荐负反馈。
- 客户端提交 `hide_artist`、`hide_album`、`not_this_style` 后，后续推荐会过滤对应艺人、专辑或风格。
- 客户端提交 `like_more` 后，后续补足候选会把该歌曲的流派和艺人作为额外种子。
- 当前用户再次请求 `getRecommendedPlaylists` 时，有效负反馈会从推荐结果中过滤掉。
- 过滤后会按同风格、同歌手、未听过热门、随机 fallback 的顺序尽量补足请求的 `count`。
- 后续服务端生成新的推荐歌单时，也会排除该用户已 dislike 的歌曲。
- 客户端提交 `restore` 后，服务端会软删除该 dislike，之后推荐算法允许该歌曲重新出现。
- 客户端可以通过 `getRecommendationFeedback.view` 拉取当前用户的有效 disliked song ids。
- 反馈按用户隔离，A 用户 dislike 不影响 B 用户。

## 写入反馈接口

```text
POST /rest/setRecommendationFeedback.view
```

认证参数仍沿用现有 Subsonic 参数：

```text
u=<user>
p=<password>
```

或：

```text
u=<user>
t=<token>
s=<salt>
```

业务参数支持 query params，也兼容 JSON body。客户端当前继续用 query params 即可。

### dislike 示例

```text
POST /rest/setRecommendationFeedback.view
  ?id=<songId>
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

### restore 示例

```text
POST /rest/setRecommendationFeedback.view
  ?id=<songId>
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

## 参数语义

| 参数 | 必填 | 当前支持 | 说明 |
| --- | --- | --- | --- |
| `id` | 是 | 任意非空 target id | song 动作使用 `entry.id`；artist/album/style 动作分别使用 artist id、album id 或 genre 文本。 |
| `action` | 是 | 见下方动作表 | 旧客户端可继续发送 `dislike` / `restore`，服务端会按 song 级反馈处理。 |
| `targetType` | 否 | `song` / `artist` / `album` / `genre` | 通常可省略，服务端会根据 action 推导；`restore` 这类通用动作建议显式传入。 |
| `scope` | 否 | `hot_recommended` | 默认 `hot_recommended`。其他 scope 当前会返回 failed。 |
| `reason` | 否 | 建议 `user_dislike` | 默认 `user_dislike`。 |
| `source` | 否 | 建议 `emosonic` | 默认 `api`。 |

`id` / `targetId` 最长 128 字符。Agent 推荐歌手名会被服务端清洗为最多 120 字符，因此 `source=web_agent` 的曲库外 `hide_artist` 反馈可以完整保存并用于后续过滤。`hide_artist` 的 target 可以是本地 Artist UUID，也可以是外部/Agent 歌手名；服务端过滤本地曲库时会对歌手名做大小写不敏感和空白规范化匹配。
Subsonic API 和 Web 登录态反馈接口都兼容 `id`、`targetId`、`target_id` 三种目标字段名；旧客户端继续使用 `id` 即可。提交成功的响应会同时返回 `id`、`targetId`、`target_id`，方便不同客户端复用同一解析逻辑。
如果显式传入 `targetType`，它必须和 `action` 的默认 target 类型一致；例如 `hide_artist` 必须搭配 `artist`，否则服务端返回 failed。通用 `restore` 例外：旧客户端省略 `targetType` 时按 song 恢复，新客户端可以显式传 `artist`、`album` 或 `genre` 来恢复对应 target。
`genre` target 会按大小写不敏感方式规范化，例如 `Rock` 和 `rock` 指向同一条风格反馈，`restore_style` 也可以用不同大小写撤销。

支持动作：

| action | 默认 targetType | 语义 |
| --- | --- | --- |
| `dislike` / `dislike_song` | `song` | 不再推荐这首歌。 |
| `restore` / `restore_song` | `song` | 恢复这首歌。 |
| `hide_artist` | `artist` | 后续热门推荐过滤该艺人。 |
| `restore_artist` | `artist` | 恢复该艺人。 |
| `hide_album` | `album` | 后续热门推荐过滤该专辑。 |
| `restore_album` | `album` | 恢复该专辑。 |
| `like_more` | `song` | 多推荐与该歌曲同流派、同艺人的候选。 |
| `not_this_style` | `genre` | 后续热门推荐过滤该风格。 |
| `restore_style` | `genre` | 恢复该风格。 |

## 成功响应

`dislike` 成功：

```json
{
  "subsonic-response": {
    "status": "ok",
    "version": "1.12.0",
      "recommendationFeedback": {
        "id": "<songId>",
        "targetType": "song",
        "targetId": "<songId>",
        "action": "dislike",
        "scope": "hot_recommended"
      }
  }
}
```

`restore` 成功：

```json
{
  "subsonic-response": {
    "status": "ok",
    "version": "1.12.0",
      "recommendationFeedback": {
        "id": "<songId>",
        "targetType": "song",
        "targetId": "<songId>",
        "action": "restore",
        "scope": "hot_recommended"
      }
  }
}
```

注意：`version` 由服务端现有 Subsonic formatter 决定，目前是 `1.12.0`。

## 拉取反馈接口

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

成功响应：

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
        "hiddenAlbumIds": ["album-123"],
        "hiddenGenres": ["rock"],
        "likedMoreSongIds": ["song-789"],
        "updatedAt": "2026-06-25T09:48:00"
      }
  }
}
```

说明：

- `dislikedSongIds` 只包含当前用户、当前 scope 下仍有效的 song 级 dislike。
- `hiddenArtistIds` 只包含当前用户当前 scope 下仍有效、且能解析为本地曲库 Artist 的 id；Agent 对曲库外歌手的 `hide_artist` 反馈不会混入该字段。
- `hiddenArtistNames` 只包含当前用户当前 scope 下仍有效、不能解析为本地 Artist UUID 的外部歌手名，用于同步 Agent “不感兴趣”反馈。
- `hiddenAlbumIds`、`hiddenGenres` 和 `likedMoreSongIds` 只包含当前用户当前 scope 下仍有效的扩展反馈。
- `restore` 后对应歌曲不会再出现在 `dislikedSongIds`。
- `updatedAt` 是当前用户该 scope 最近一次反馈更新时间；没有反馈时为空字符串。
- JSON formatter 会省略空数组，因此没有有效 dislike 时可能不返回 `dislikedSongIds` 字段。

## 失败响应

失败仍沿用 Subsonic 风格：

```json
{
  "subsonic-response": {
    "status": "failed",
    "version": "1.12.0",
    "error": {
      "code": 0,
      "message": "invalid recommendation feedback action"
    }
  }
}
```

客户端应继续把 `status=failed`、HTTP 错误、网络异常都当作同步失败，并保留 pending outbox 等待重试。

## 幂等语义

- 重复写入同一 `(user, targetType, targetId, scope)`：返回 ok，不产生重复有效记录。
- 重复 restore 同一 target：返回 ok，不产生 500。
- 歌曲已删除或不存在：仍可记录反馈并返回 ok，避免客户端无限重试。
- `restore` 后服务端保留历史记录，但标记为软删除；有效 dislike 集合里不再包含该歌曲。
- 写入成功会记录一条低敏 `recommendation event=feedback_updated` 日志，只包含用户、action、targetType、scope、source 和是否 restored，不记录具体 target id。

## 推荐列表过滤语义

`GET /rest/getRecommendedPlaylists.view` 已接入过滤：

- 服务端按当前鉴权用户读取 `scope=hot_recommended` 的有效反馈。
- 返回前过滤 disliked songs、hidden artists、hidden albums 和 hidden genres；hidden artist 既支持本地 Artist UUID，也支持匹配本地曲库艺人名的外部歌手名。
- `like_more` 会作为补足候选的额外种子，让同流派、同艺人歌曲更容易进入推荐。
- 过滤后会去重，并从备用候选池尽量补足到请求的 `count`。
- `songCount`、`duration`、`coverArt`、`entry` 都基于过滤和补足后的结果。
- 每个 `entry` 会附带 `recommendReason`，说明推荐依据，例如用户常听的流派、常听艺人、未听过热门歌曲或补充多样性。
- 每次返回会记录一条低敏 `recommendation event=playlist_served` 日志，包含请求数量、返回数量、被反馈过滤数量、补足数量和各类有效反馈计数，不记录歌曲标题或歌曲 ID。
- 如果过滤后为空，服务端返回空推荐歌单；JSON formatter 可能省略空 `entry` 字段。
- 过滤只影响热门推荐，不影响搜索、专辑页、歌单、收藏、历史记录和播放。

## 后续生成推荐歌单

服务端生成每日推荐歌单时也会读取当前用户反馈集合：

- dislike / hide_artist / hide_album / not_this_style 后，新生成的推荐歌单不会再包含对应歌曲、艺人、专辑或风格。
- restore 后，后续新生成的推荐歌单允许对应 target 再次出现。
- like_more 后，新生成和补足推荐会把该歌曲的流派、艺人作为额外倾向。
- 每日推荐生成使用统一评分排序，而不是按规则简单拼接。当前权重为：

```text
score =
  genre_match_score * 0.30
+ artist_affinity_score * 0.25
+ album_affinity_score * 0.10
+ freshness_score * 0.10
+ popularity_score * 0.10
+ not_played_score * 0.10
+ feedback_score * 0.05
```

其中 `feedback_score` 主要来自 `like_more`，负反馈仍先作为过滤条件处理。
- 同分候选会使用一个按 `recommendationDay + track id` 计算的稳定排序字段：同一天结果可复现，不同日期会在同分歌曲之间轮换，避免连续几天完全固定。
- 已经生成过的旧推荐歌单不会被物理改写；但 API 返回时仍会实时过滤，因此客户端看不到已 dislike 的歌曲。

## 数据隔离

反馈表按用户隔离：

```text
unique(user_id, target_type, target_id, scope)
```

因此：

- 用户 A dislike `song-123` 后，A 的热门推荐过滤 `song-123`。
- 用户 B 不受影响，仍可能看到 `song-123`。

## 客户端建议

- 用户点击“不再推荐”后继续本地立即隐藏，提升 UI 响应速度。
- 后端同步失败时继续保留 pending outbox。
- 撤销时发送 `restore`。
- 应用启动、切换服务器、切换用户、刷新推荐页时，可以调用 `getRecommendationFeedback.view` 合并服务端 disliked。
- 刷新推荐时可以继续先做本地 disliked 过滤；服务端也会过滤，二者是兜底关系。

## 服务端 Web 推荐页

服务端 `/recommendations` 页面已为每日推荐表格接入反馈入口：

- `Hide` / `不再推荐`：提交 `dislike_song`，并立即隐藏当前歌曲行。
- `More Like This` / `多推荐类似`：提交 `like_more`，当前行会标记为正反馈。
- `Less Artist` / `少推荐该艺人`：提交 `hide_artist`，并立即隐藏同艺人推荐行。
- `Less Album` / `少推荐该专辑`：提交 `hide_album`，并立即隐藏同专辑推荐行。
- `Less Style` / `少推荐该风格`：提交 `not_this_style`，并立即隐藏同风格推荐行。
- 页面顶部反馈状态条提供 `Undo` / `撤销`，会提交对应 `restore*` 动作。

Web 页面使用登录态专用接口：

```text
POST /recommendations/feedback
```

该接口复用同一张推荐反馈表，只面向当前登录的 Web 用户，不需要客户端传 Subsonic token。

Agent 推荐歌手卡片也复用该接口：

- `Save` / `加入待听`：保存到浏览器本地待听列表，不写服务端数据库。
- `More details` / `查看更多`：把当前歌手转成追问，继续请求 Agent。
- `Starter playlist` / `生成入门歌单`：把当前歌手和 starter tracks 提交到 `POST /recommendations/agent/starter-playlist`。
  如果 starter tracks 能匹配本地曲库同名歌手歌曲，服务端会创建普通播放列表；否则创建一条 `MusicRequest`，把曲库外歌手和入门歌曲登记到请求板。
  该快捷动作对同一用户幂等：同名、同 starter track 顺序的 Agent 歌单或 pending 请求已存在时会复用已有记录，并在 JSON 里返回 `reused: true`。
- `Not interested` / `不感兴趣`：提交 `hide_artist`，`target_type=artist`，`target_id` 为 Agent 返回的外部歌手名，`source=web_agent`。
- Agent 后续请求会把这些外部歌手名放入 `context.recommendationFeedback.hiddenArtistNames`，并在服务端最终结果中过滤同名或别名歌手。
- Agent 缓存 key 包含 `recommendationFeedback`，所以点“不感兴趣”后不会继续命中反馈前的旧缓存。

## 联调验收

建议验证：

1. 用户 A dislike 某首推荐歌后，刷新热门推荐不再返回该歌曲。
2. 用户 B 仍可看到该歌曲。
3. 用户 A restore 后，刷新/后续生成允许该歌曲再次出现。
4. 重复 dislike 返回 ok，服务端只有一条有效记录。
5. 重复 restore 返回 ok，不报 500。
6. dislike 已删除或不存在的歌曲 ID 返回 ok。
7. 过滤后 `coverArt` 不使用已过滤歌曲的专辑封面。
8. `count=50` 时，过滤掉少量歌曲后仍尽量返回 50 首且不重复。
9. `getRecommendationFeedback.view` 返回当前用户有效 disliked song ids。
10. 用户 A 的反馈拉取结果不包含用户 B 的 disliked song ids。
11. restore 后再次拉取不再包含该 song id。
12. 旧客户端继续请求 `getRecommendedPlaylists` 不崩溃。
