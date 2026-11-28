# Design · R3 B站发现与 Sol 日推

## 1. 决策与边界

新增来源要解决本地召回过于集中的问题。2026-11-28 的一次隔离预演中，Sol 对 150 首本地候选排序，其中 149 首曲库风格标签为「国语流行」，最终 50 首中有 49 首带此标签。这是一次观测，标签覆盖和口味都可能变化。R3 在排序前引入有具体依据的录音，保留只返回本地可播放歌曲的日推契约。

服务端参考提交为 `3889e6bf2ef50c7d81a1367ce743876a6425ccb4`。普通日推和专辑发现已分别设置为 `gpt-6.2-sol`，画像也使用 Sol，聊天使用 Luna；本 change 复用这些入口。实施前读取目标分支上的实际代码，保留更新的等价行为。

```text
昨日 B站记录 → 已有预算的 Sol 识别
  ├─ 直接命名的歌曲、歌手与版本
  └─ 可核实的合作署名，或有真实复听支持的风格 → 有界目录检索 → 具体录音
           ↓ 两路分别保留证据，按录音版本查本地库
  ├─ 唯一可播放 Track → ready
  └─ 确定缺失 → 国区歌曲与完整发行核验 → 共享专辑配额及订单
              → Oracle 下载整专 → OpenWrt 收件、索引与整专核对 → 目标 Track ready
           ↓ 只向目标用户提供当前有效的 Track
  原普通日推召回 → 有界合并 → 原 Sol 排序与多样性筛选 → 下一份尚未生成的日推
```

首版仅针对现有 `[album_discovery].target_user` 和 `library_key`；CN 为明确的范围收敛，非新增多区域能力。Oracle 控制和交付凭证、`library_root`、Apple token（可选）、OpenAI 配置及预算均沿用现有设置。若实际部署不是这套交付链，先记录具体差异，不以此为理由重新建设一套下载系统。

建议新增 `supysonic/bilibili_discovery/{history.py,extractor.py,matcher.py,service.py}`（可按实际规模合并），另加 `db_layer/bilibili_discovery.py`。不强制模块数量，不引入通用插件框架。

## 2. 现有历史 API 与日输入

源服务器已有 `GET /api/history?platform=bilibili&date=YYYY-MM-DD&page=1&pageSize=100`，必要时先 `POST /api/history/sync`，body `{"platform":"bilibili"}`，头 `X-Auth-Token: <现有历史服务密码>`（启用认证时）。认证源以 `docs/API_CLIENT.md` 为准；不接收或复制 B站 Cookie。

```json
{
  "success": true,
  "details": {"bilibili":{"newCount":5,"updateCount":2}}
}
```

上述是现有成功响应形状；检查 `details.bilibili` 存在、没有 `error`，且 newCount/updateCount 为非负整数。顶层 `success:true` 或空 details 不算同步成功。HTTP/auth 错误保持待处理，不把无法查询解释成无记录。正常路径每个新日输入触发一次同步，已落库成功同步结果后不重复触发；请求超时按正常退避再次获取，不高频重发。历史服务器已有自动同步，不改变其周期；同步和读取并发导致数据改变时按下述处理。

```json
{
  "items": [{"id":"123","platform":"bilibili","business":"archive","bvid":"BV_example","cid":456,"title":"歌曲示例 - 艺人示例","author_name":"某UP主","tag_name":"音乐","view_time":1795737600}],
  "total":1,
  "hasMore":false
}
```

示例数据均为模拟字段。历史时间为 Unix 秒，`timestamp` 是另一字段（毫秒），不得混用。源端日期依赖 Node 本地时区，部署要求 `TZ=Asia/Shanghai` 并用日期边界 fixture/读回确认；EmoSonic 按 Shanghai 昨日计算并检查每条 `view_time` 落在对应半开区间。发现跨日数据/时区不符时记录失败，不能悄悄过滤后声称全日完整。

- 使用现成分页读取全部**当前可见**的昨日记录；按 `(business,id,cid,view_time)`（有稳定 event_id 时优先用它）去重，仅用于发现，不把 `id` 当唯一观看事件；目标用户和日期由 Run 限定。保留最小字段 id/business/bvid/cid/title/author/tag/view_time，忽略其他平台。
- 先收集整个日期的记录并保存一个日快照，再进入 AI 提取；个人每日数据量下，查询中断从第 1 页重读比持久化 offset 更简单可靠。成功后的快照不因重启重新抓。检查页循环、异常空页、total 变化和计数矛盾；异常时按退避从头重读。单次请求有 timeout，单次读取有合理页数/体积安全上限；达到上限明确记为未完成，不静默截断。
- 源 API 是 OFFSET 分页且 OID 覆盖旧观看时间，不提供严格快照。即使总数一致也不能证明源端无并发覆盖。此处保存的是可见的日输入，不承诺全播放事件；历史长期归档仍由历史服务器负责。不能恢复源端已丢失记录。若实际使用发现大量漏数，再单独修复源服务的事件身份和稳定排序，而不是本功能的先决开发任务。
- 不访问 DELETE 接口，不下载 B站视频，不通过 LLM 猜测无明确文字证据的 BGM。

## 3. 最小持久化

沿用 Peewee、UUID `PrimaryKeyField`、`connection_scope()` 与现有三类数据库。新增两张表，无独立数据库、事件表、下载表或 outbox。字段名可遵循现有风格，语义如下。

| 模型 | 必需字段与约束 |
|---|---|
| BilibiliDiscoveryRun | id、user FK、source_day（Shanghai date）、status、sync_confirmed_at、snapshot_json、extract_offset、日内衍生检索计划及已处理键/计数（可用同表 JSON）、next_attempt_at、last_error、created_at、updated_at；唯一 `(user, source_day)` |
| BilibiliDiscoveryCandidate | id、run FK、identity_key（基于录音、艺人、版本证据的稳定身份）、title、artist、version、album_hint、source_evidence_json（直接或衍生依据及关联）、status、reason、nullable release FK（AlbumDiscoveryRelease，SET NULL）、apple_song_id、nullable Track FK（SET NULL）、ready_at、next_attempt_at、created_at、updated_at；唯一 `(run, identity_key)` |

Candidate 通过 Run 归属用户。同一录音被直接提及、从合作署名找到、从风格目录找到时，一行可以保存多条带类型的证据；有明确 CN song ID 时据此归并并保留源引用，归并前保留标题、艺人和版本的原始证据。无法确定是否为同录音时分开等待。跨日保留不同观察，推荐层按 Track UUID 去重，下载层按现有库与国区发行去重。纯风格或歌手线索保存在 Run 的有界进度中，查到具体录音才生成歌曲候选。

Run 经 `fetch_pending → extracting → done`，仅在快照、批次提取和当日有界衍生探查完成或逐项记录跳过原因后标 done。后续待下载和入库按 Candidate 状态继续处理。暂时错误保留阶段、原因和下次尝试时间。Candidate 经 `pending → waiting_album → ready` 或 `skipped`；本地强匹配可直接 ready。配额不足、临时目录失败、等待文件/索引保留待处理状态。`skipped` 附非音乐、版本不清、反馈排除、过期等具体原因。订单细节读现有 Order/Release。

数据库事务只覆盖本地状态/唯一键写入，不跨外部 HTTP、LLM 或长文件读取。使用已有 `recommendation_lease`（独立 B站日/目标用户 key）保护有界提取步骤，配合唯一约束、游标条件更新；不要新增通用租约系统。租约覆盖有界一次调用，旧执行不能覆盖已提交进度；远程派单幂等由已有订单协议保障。快照与初始 offset 一起提交；AI 成功批次的候选 upsert 和 offset 一起提交。崩溃发生在付费响应返回但本地结果尚未提交时可能需要再次调用 AI，不能宣称外部调用 exactly-once；现有费用预留/结算与退避仍适用。

迁移：基线 `SCHEMA_VERSION=20261127`，实施时重新读取实际分支版本并分配更高且唯一的版本。三份 base schema、三份迁移及 `db.py` facade 一并更新。FK 删除语义允许 Track 删除/重扫；不能阻止原库清理。无需修改旧播放记录或旧专辑订单表结构。快照仅用于每日发现/排错；建议用已有维护机制做简单 30 日清理或文档化保留策略，不增加专用定时器；未结算订单及关联候选不能因清理失去核对入口。

## 4. LLM 识别与本地优先

使用 `request_recommendation_json(config, messages, schema, workload="album_discovery", ...)`，沿用现有专辑发现 provider lease、退避、共享 `MetadataBudget` 和已支持的模型/价格校验。首版不改价格表，不调用带隐式重试的通用付费客户端；现有 OpenAI 配置不支持时仅暂停 B站提取并记录原因。与旧专辑发现共享预算可能影响可用额度，这是复用策略的已知取舍；不额外扩张预算。每次 scheduler pass 处理小批次（建议 20–40 条、一次模型调用；输入/输出以现有限制为准），按 offset 逐步完成，避免长任务和对其他业务的占用。

只发送必要的有界标题/类别/作者文字及临时编号，不发 token、文件路径、账号信息或完整 URL。输出采用现有 schema 校验器支持的简单 JSON；每个编号恰有一个结果。直接歌曲须有原文可核验的曲名、歌手和版本线索；一次输入最多接受一个直接录音。仅有明确风格或具名歌手时可输出检索线索，模糊/非音乐分别记 `uncertain` / `non_music`。合集、混剪、未注明 BGM 不当作单首；Live、Cover、Remix 各自核对表演者与版本，缺少证据时不默认普通录音室版。

```json
{"results":[{"id":1,"kind":"song","title":"示例歌曲","artist":"示例艺人","version":"live","style":"","evidence":"示例艺人《示例歌曲》现场版"},{"id":2,"kind":"style_lead","title":"","artist":"","version":"unknown","style":"R&B","evidence":"R&B 编曲赏析"}]}
```

所有源文本是不可信数据。应用检查编号范围、重复/缺失、字段长度和枚举，并将 evidence 与输入原文逐项核对。UP 主身份和宽泛「音乐」分类不足以确定歌手或风格；模型的置信度、建议歌名、URL 和操作指令都不能作为匹配或派单证据。

### 4.1 两种可解释的发现路径

1. **直接识别**。来源文字确实命名录音时，保存原文与版本证据，先查本地；称其为「观看记录提到的录音」。仅凭视频观看无法证明听完、喜欢或在 EmoSonic 播过。
2. **衍生探索**。在固定的用户与上海源日内，按稳定顺序探查以下有证据的线索，查到具体录音后先查本地；称其为「由合作署名/风格找到」，不称用户看过该曲。风格须同时满足可识别的 B 站风格线索、该用户已有重复播放种子覆盖至少两位不同艺人、匹配的规范化风格。沿用 `normalize_discovery_style` 和原 `_style_support` 的支持条件；从现有国区 `style_songs` 取有编辑来源且录音自身风格也匹配的歌曲。合作路径先核实来源艺人的目录身份，再在同一已核验录音的正式署名中找到合作艺人，记录两位 artist ID 和证明该关系的 song ID；只展开一跳，通过 `top_songs` 核实目标歌曲也署名该人。可用 `get_song/get_artists`，或支持 `collaborating_artists` 的适配器。只有 `similar_artists` 或排行榜相邻时不能认定合作。不能消歧、适配器缺证据或目录不支持某渠道时跳过该线索，其余渠道继续。

每用户每源日合计最多 **3 个风格、每风格 3 首；一跳至多 5 位合作歌手、每人 2 首；全部衍生路径合计至多考察 30 首不同录音**。总帽是额外边界，并非凑满目标；风格与合作两路的上限也分别生效。风格先按现有真实复听支持排序，候选顺序及同分键（如 artist/song ID）保持稳定。Run 记住已选线索、已查录音和各限额，跨分页、跨提取批次或重启不重开一轮；同一录音的多条证据可附在同一 Candidate。直接命名的录音单独统计。复用现有目录读接口，不为扩展风格再触发一次模型排序。没有满足证据条件的线索时，零衍生候选是正常结果。

对直接及衍生候选保留可复查的最短证据链：Run 中的输入编号与引用、来源类型、风格对应的真实复听依据或合作署名录音、最终命名录音以及已确认的目标版本。现有本地播放统计仅由原播放路径写入。用户的个人观看文本保留在受限的 Run 中，排序器只使用紧凑的来源信号。

本地匹配：使用 Track、Album、Artist/TrackArtist（及已有艺人别名）查询；归一化标题、歌手并保留版本差异，有明确专辑/时长/ISRC 等证据时辅助判断。唯一可靠匹配并且目标 Track 文件存在可读 → ready；不核验其余整专曲目，不调用下载。候选有歧义或索引指向缺失文件 → 记录原因等待/跳过，不因搜索失败就自动下载；只有确定本地缺失才进入目录。负反馈、已播放按现有业务判断；B站观看不计入 User_Play_Activity，不冒充真实听歌记录。

## 5. 复用 CN 目录与共享订单

从现有 `AlbumDiscoveryService.from_config()` 取得 catalog；直接识别可调用 `search_seed_songs(title, artist)`，衍生录音用上一步已取得的国区 song ID 并验证目标艺人。必要时 `get_song/get_artists` 补齐身份，`get_release(album_id, refresh=True)` 核对国区实际可播放曲序。使用现有 `CatalogAlbum`、`CatalogTrack`、规范化、版本检查工具。目标歌曲 ID 必须位于完整发行，标题、艺人、演出/混音等版本与已取得的录音证据一致；来源确有专辑信息时也需相符。两个可能的版本无法区分、目录仅在其他区可用、曲序不完整时记录原因。只有具名目标录音已确认、本地确定缺失、当前反馈允许，才可按该 CN album ID 下载整张专辑。纯风格、合作关系本身、搜索首项均不能授权任何发行。使用现有目录反馈检查（抽出 `_DiscoveryPreferences.song_allowed` 的可复用部分，保留已有 seed 检查的原语义）。

**只抽取必要共用逻辑，不重写 AlbumDiscoveryService：**
1. 把 `_save_selection` 中 release get_or_create、冻结 expected_tracks 校验提为小 helper。旧 `_save_selection` 继续执行原有 selection/evidence/placement 约束；B站只保存相同格式 `AlbumDiscoveryRelease` 并关联 Candidate、apple_song_id，不创建 `AlbumDiscoverySelection`、`AlbumDiscoveryPreview`、`AlbumDiscoveryPlacement`。
2. 从 `_confirm_one` 复用/提取不依赖旧 seed 的订单判断和派单 core（已有 `_assess`、`_read_oracle`、`_save_remote_state` 可在清晰参数边界下复用）。B站调用前自己做目标歌曲/目录/反馈核验；不放宽旧 `_recheck` 的真实播放种子条件，不合成假种子。旧入口行为、预演、手动确认、夜间发现保持不变。
3. 共用 `get_album_download_order`、`record_album_discovery_reuse` 和底层 `reserve_album_download`，注意旧 `reserve_discovered_album` 自带原夜间自动开关。在原事务 callback 内保存 release 与 B站候选关联，持久化订单 ID 后才可提交。沿用原 library/CN/album_id 唯一约束。在预留事务内重新读取 JSON `aiDiscovery.maxAlbumsPerDay`，当前个人配置为每天最多 **13 张国区新专辑，两路共用**。B站 `auto_download` 是自己的显式授权，旧 `aiDiscovery.autoSubmit` 继续控制原夜间自动发现。额度 0 禁止任一来源新派单，既有订单仍可核对。额度 13 且旧来源已占 12 时，两条 B站请求并发最多新增 1 张。已有本地完整发行、已接单、在途/已交付复用不消耗新额度。额度不足保持 pending，后续日重试，过期则不新派单。
4. 仅当 catalog 重新确认、反馈允许、B站开关允许且共享预留成功，才调用现有 `OracleAlbumClient.submit(album_id, url, order_id)`。只下载有合法授权/可使用的发行；不修改下载器的授权或媒体保护逻辑。

现成接口（Downloader 无新增路由）：

| 方法 | URL | 数据/语义 |
|---|---|---|
| GET | `/api/discovery/albums/{albumId}` | 可选 orderId/taskId，读接单/关联/状态 |
| POST | `/api/discovery/albums/{albumId}` | `{url,orderId}`，使用原持久化订单 ID |
| POST | `/api/discovery/albums/{albumId}/close-if-unaccepted` | 原请求体；封闭未接受订单，防止延迟接单 |
| GET | `/delivery/v1/batches/{batchId}` | 原交付 manifest，使用独立交付凭证 |

响应包含 albumId/orderId/status/taskId/taskStatus/batchIds；已有 client 校验 `submitted/associated/absent/rejected/unknown` 及身份、manifest。沿用 control Bearer 与 delivery Bearer 两个既有密钥，不再暴露普通 `/api/downloads` 给新自动化路径。新订单只能由已获原预留触发，重复确认关联同一订单；超时使用原 orderId 核对和既有 `close_if_unaccepted` 机制，不凭 GET 空结果释放或生成新 orderId。原 retry/taskId 变化与原 quota_date 保持。确认终态失败由原下载器重试路径处理，本功能不自行无限重派。

## 6. 入库和核对的精确改动

实际 `reconcile_orders()` 当前查未 ready/仍 reserved 的 release，再取第一个旧 `AlbumDiscoverySelection`；无 selection 就 continue。若直接共用订单但不改这里，B站下载会永久无法入库。最小修复：允许存在合法 B站 Candidate→release 关联且 apple_song_id 在冻结曲表中的 release 成为核对对象；仍然使用同一 `reconcile_orders` 调度。旧 selection 存在时保持原代表曲选择；B站独有时用已验证候选 song_id 作为 `reconcile_album` 参数，仅作技术核对，不创建旧 selection/placement。整专核对返回每首 `song_id → track_id`，所以同张专辑不同目标歌曲分别映射，不被单一代表曲覆盖。

- Oracle → OpenWrt 保持既有收件/确认/清理和本地音乐目录索引路径。远端完成不等于收件，本地收件不等于索引成功。复用 `reconcile_album(..., deliveries=..., previous_tracks=..., require_receipts=原订单策略)`，不弱化原 SHA-256/大小/曲序/版本/唯一索引检查。已下载整专须满足既有全专核验；不新增共享卷要求、manifest 格式或文件扫描系统。
- 使用现有 watcher/导入扫描。若部署实测收件后未索引，优先核对原链路配置，必要时使用已有授权扫描操作对配置的音乐目录补扫一次；不在每次 B站轮询全库扫描，不新增管理员 HTTP 自调用。下次已有 reconcile 读取真实 Track。
- 现有 `_save_inventory` 用 CAS 写 release 核验结果并更新旧 selection。保持原 CAS 和旧 selection 更新；只有成功持久化 ready 证据后 B站候选才可关联真实 Track。B站 job 可从已保存的 release.reconciliation 投影 ready；投影失败则下次重读，无需再次下载或重复哈希整专。关联已经 ready 的 release 时也按现有证据查目标歌曲，不能等待一个永远不再执行的旧核对任务。
- 已有 ready 证据失效时不继续提供 B站来源信号。使用 `verified_file_is_current` 的目标音轨检查（文件身份未变不重新 hash），必要时按现有核对路径重新检查关联发行，不开第二套轮询。没有当前 Track 或身份冲突时置等待/跳过；不能仅凭旧 UUID、文件名或 Downloader completed 变 ready。
- 暂停 B站功能/新单时，已接受或结果不明的订单仍按原订单核对；B站独有、尚未确认接受的预留不得在关闭新单后发起新 POST 派单，使用原 inspect/closure 机制处理。若同一订单还存在合法旧 selection，旧来源按其既有授权继续。不得因过期或删候选直接删除已接受/不确定订单。无有效来源关联的 release 不凭空提交新单。

## 7. 日推接入：不改算法、不等下载

实际 `_buildRecommendedTracks()` 从全库选择未听且未被负反馈屏蔽的曲目，然后多样性筛选截断；仅把新 Track 写入库不保证模型能看到 B站信号。先固定同轮 baseline，方便查明来源是否确实带来池外歌曲。

只修改 `_createRecommendPlaylistForUserUnlocked` 中调用 `select_openai_daily_tracks` 之前的候选组装，并给 `recommendation_openai_daily` 的 context 构建增加可选来源映射。
- 只针对目标用户且 feature enabled，读取 ready 候选，优先最近 ready/发现；推荐信号建议保留 ready 后 14 天（初版常量，不新增配置）；待新派单候选按 source_day 后 14 天过期，已接受订单不取消，晚到入库仍可从 ready 起计算候选窗口。按 Track UUID 合并，不按观看次数人工提高评分。
- 与原资格保持一致：实际 Track、原未播放集合、原负反馈、同一音乐库；已播放/隐藏/不喜欢不进入 B站增强池。不存在或不可播放的 B站 Track 不注入；下载歌曲需当前核验证据。保留原始 baseline 列表作为无 AI/原有 fallback 的输入。
- 给现有 AI 候选池一个小范围来源机会：`candidateCount=daily_candidate_limit(numSongs)`，最多 150。新增进入池的 B站 Track 上限 `min(10, max(1, candidateCount//10))`（candidateCount=0 时不做）。原池已有 Track 只附来源信号，不占新增名额；可入池的额外 UUID 按 ready_at 降序、source_day 降序、Track UUID 升序先填空位，剩余数量替换原池末尾相同数量未带来源信号的曲目，保留其他原候选的相对顺序；可替换数量不足就少加。最终继续执行原 `select_diverse_recommendations`。不设来源最终保底或位置，不因风格标签集中硬凑品类。未开启 OpenAI 日推时保持本地算法。配置为 50 首时，Sol 看到至多 150 首，输出所有候选的临时编号顺序，既有选择器再挑出至多 50 首。
- 模型 payload 只给来源类别（直接提及、合作署名或风格支持）和来源年龄等有界信号，不传 BVID、观看标题、UP主或路径。一首歌可同时有多种依据，不能将衍生结果描述成看过或喜欢。仅在确有信号时为增强 prompt 分配版本，参与现有成功缓存键（实际模型、context、候选 UUID 顺序）；如增强改变响应失败冷却的语义，将增强失败与原路径隔离。关闭、非目标用户或无有效信号时使用原 context、prompt、缓存键及候选顺序。
- **Sol 普通日推**沿用当前 `daily_model` 与 `workload="daily"` 的 Responses 调用，保留候选完整可靠标签、专辑和合作署名、近 7/30/90 天播放上下文。按源码现有限制（总上下文 1,050,000，最大输入 922,000 token）、长请求预检、共享账本预留和结算运行。百万上下文是上限；不额外发送原始观看历史或补齐长度。**显式选择 Luna** 时仍走紧凑上下文、48 KiB 字节上限及原有字段裁剪。紧凑来源信号须计入同一字节校验，增强无法装入时先恢复原候选与原路由再发请求；不能在付费失败后为了比较模型再调一次。B站提取走已有 `album_discovery` 工作负载，两个工作负载各按真实用途记入同一费用账本。功能不修改账户预算或密钥。
- 保留原临时 ID 校验（已有重复补齐策略）、按最新反馈和历史复查、曲目身份检查、多样性及轮换。对 B站额外候选再查目标文件和 Track 是否仍有效。来源或模型不可用时遵循既有 fallback/错误策略，本地 fallback 必须收到保存的原 baseline，不因付费调用失败强排 B站曲目。最终结果允许 0 首 B站歌曲。
- `_save_new_recommended_playlist` 和旧固定位置投放仍保留。B站来源不创建旧 selection，因此不能通过它被强制放入第 4 位；如果相同曲目独立存在真实旧 selection，仍可按旧功能投放，不能错误归因于 B站。
- 绝不新增日推 readiness gate、全局等待、自动重建/追加。同一天歌单已经存在就原样返回；候选未赶上本次生成，则参与下一份尚未生成的歌单。在推荐提前于 04:30 时，昨天发现的歌可能到次日才参与，这一取舍是有意的。其他用户日推不受 B站任务状态控制。无既有听歌活动的用户维持现状，不引入冷启动功能。

## 8. 调度与配置

复用 `IntervalScheduler` 注册一个 `bilibili-discovery`，默认跟随现有推荐检查间隔（300s）；启动立即唤醒，内部按上海日历判断，仅在每日 04:30 之后创建昨日 Run，唯一日期键避免重跑。早于 04:30 可继续恢复旧任务；已存在未完成 Run 按持久化进度继续，过期未派单候选不新派单。首版不自动批量补抓服务停机期间所有未建 Run 的历史，记录日期空档；长期完整归档独立。每次短批次、有限网络请求，遇限流/预算/网络问题按现有退避和 next_attempt_at 返回；不在后台线程 sleep 等下载完成，不阻塞已有 reconcile、播放和日推。

只新增必要配置，示例（真实地址与密钥由部署方填入）：

```ini
[bilibili_discovery]
enabled = off
auto_download = off
history_api_url = https://history.example.invalid
history_api_token_file = /run/secrets/bili-history-token
start_time = 04:30
```

5 个新配置项；时间默认上海 04:30，其余沿用 `[album_discovery]`、现有 JSON `aiDiscovery.maxAlbumsPerDay`、`[recommendation_agent]` 与现有费用配置。`enabled=off` 不执行新发现/新来源增强；`enabled=on,auto_download=off` 可做识别、匹配、已在途核对及本地候选参与推荐，但不新派单。只读预演在隔离库或单测 fixture 中运行，不修改生产歌单。自动下载经本次实施验收后由用户配置启用，无每首确认步骤。正常部署使用私网/TLS 和现有鉴权；鉴权失败不要猜密码或绕过。日志沿用 Scheduler `/admin/tasks` 和原日志体系，提供日输入条数、识别/本地/待入库/ready/跳过数量、共享订单 ID 及简短原因，避免原始观看标题/凭证进共享日志；不新增 Web 页面。

## 9. 验收与范围限制

以 `tasks.md` 和三份 spec 为验收清单。离线测试覆盖昨天的分页/时区与再次运行、直接录音与衍生证据区分、纯标签或不同版本不会下单、按日探查上限和重启去重、已有曲免下载、两来源同国区发行共用订单及并发名额、未知接单与重启恢复、没有旧 selection 的整专入库核对、失效文件及反馈排除、三数据库建库及升级。设候选满 150 且本地标签高度集中的 fixture，断言至少一个可核验的池外 Track 经合并进入模型；使前 50 首本身满足多样性、轮换和资格规则，并让模型把全部新增 UUID 放在末尾，断言最终 B站入选数可为 0。另测来源为空、开关关闭、其他用户和 Sol/Luna 各自原缓存/长度路径，以及本地 fallback 收到原候选。

隔离预演以同一用户、同一曲库快照及同一源日固定本地候选基线；只计算一次候选前后差集，无须发出两次付费请求。记录输入条数、各类有效/跳过/歧义原因、已核实的国区录音和发行、已在原池的 UUID 数、真正新增/被替换数、风格标签分布（包括缺失标签），以及实际执行排序时最终歌单中的来源数量；仅对照候选时标为未测。逐项解释每个新增录音来自哪条依据、匹配哪个版本、若缺失会走哪个已有整专、库内已匹配几首、为何可派或跳过。报告中限定观看原文等隐私材料的可见范围。使用 fixture 时标注来源和模拟环节；若另有授权可以在隔离存储用一次实际 Sol 排序并报告输入/输出 token、结算及缓存，核对正式歌单 ID 和曲序、订单数均与预演前一致。预演不向 Oracle 提交订单；通过模拟回执和隔离索引验证接单后的链路。喜好改善依赖后续实际聆听，不将新增数量或标签分布作为口味改善证明。

预演可重放已确认的日快照或模拟同步成功。使用真实历史源时仅使用 GET；预演不向真实历史服务发送 POST /api/history/sync。若快照缺少已成功同步的证据，报告注明源日完整性待验。
