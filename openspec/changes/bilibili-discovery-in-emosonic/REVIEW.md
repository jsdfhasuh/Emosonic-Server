# R3 修订依据 · 2026 年 11 月 28 日

## 实际问题与本次决策

在普通日推切换至 Sol 时，一次真实的隔离预演读取了近 90 天 2,078 次播放、773 首不同曲目，向 Sol 提供 150 首候选。85,173 个 token 包括输入 84,859、输出 314，软件账本结算 0.427435 美元。曲库标签在入池前已集中于国语流行 149 首、Pop 1 首；最终选出的 50 首对应 21 位主艺人。它给出了原候选池的一个实测样本。R3 的验收要测同一快照实际增加了哪些原池外的歌，保留基线快照，不能把这个旧样本充当 B 站已经提升推荐质量的证据。

| R2 中需要调整的地方 | R3 决定 |
| --- | --- |
| B 站主要用来识别视频明确提到的一首歌 | 保留直接录音；另以可核验的合作署名、观看风格与多位艺人的真实复听交集做有限探查。保存证据类型、来源和目标，能解释「这首歌怎么被找到」。 |
| 整个日推共用 48 KiB 描述 | 普通日推默认 Sol，读取已有 7/30/90 天播放摘要及候选的专辑、合作署名和可靠标签。代码中的总窗口 1,050,000 token、输入上限 922,000；48 KiB 适用于显式 Luna。现有计数、预算与缓存仍生效。 |
| 验收靠“候选可出现” | 固定快照对照原池与增强池，区分原池已存在、新增、挤出与最终入选。保留 UUID、版本和来源理由，曲风标签仅用于描述。 |
| 实施入口以旧服务端提交为参照 | 改为包含日推 Sol 的 `3889e6b` 或等效后续版本。B 站开关默认关闭，普通日推已上线，旧专辑发现和新来源共享新专辑总额度。 |

## 一个可复现的入池例子

用模拟的满池场景验收：原池 150 首，B 站线索命中 3 首原池已有录音，另找到 2 首已入库且符合资格的新录音。若有两个可替换位置，增强池应保留 3 个重合标记、加入 2 个新 UUID、移走 2 个原 UUID，仍为 150 首。把两首新歌排在最后，并令前 50 首满足多样性与轮换，最终可有 0 首新歌入选。真实预演逐项报告观测数值。

## 代码定位

下列路径按 Emosonic-Server 的提交 `3889e6bf2ef50c7d81a1367ce743876a6425ccb4` 核对。实际实施先复核目标分支的接口。

| 路径 | 复用点 |
| --- | --- |
| `supysonic/recommend.py`、`supysonic/recommendation_openai_daily.py` | `_createRecommendPlaylistForUserUnlocked`、`daily_candidate_limit`、`select_openai_daily_tracks`。容量为 `min(150, 3 × 目标首数)`，先查当日歌单；Sol 排序、资格复查和多样性筛选均已存在。 |
| `supysonic/openai_recommendations.py`、`metadata_pricing.py`、`album_discovery_context.py`（均在 `supysonic/`） | `daily_request_model` 和 `discovery_request_model`、Sol 长输入计数、共享账本、播放窗口。B 站新增有效证据时才改变对应请求。 |
| `supysonic/album_discovery_candidates.py`、`album_discovery_catalog.py`、`album_discovery_public_catalog.py` | 风格支持、署名、曲目/发行校验和负反馈。按实际 adapter 能力取证，只有相似推荐时不能写成合作署名。 |
| `supysonic/album_discovery_service.py`、`album_discovery_quota.py`、`album_discovery_oracle.py` | `_save_selection` 原本同时保存旧来源选择；`_confirm_one` 调用依赖播放种子的 `_recheck`。小范围提取共用的发行与派单部分。以原 orderId 核对未知接单结果。 |
| `supysonic/album_discovery_delivery.py`、`album_discovery_daily.py` | `reconcile_orders` 的现有调用方位于 service，当前会跳过没有旧 Selection 的发行；开放给有合法 B 站候选关联的发行，沿用文件与索引核验。旧第 4 位投放独立。 |
| `supysonic/daemon/server.py`、`db_layer/schema.py`（位于 `supysonic/`） | 沿用原调度与数据库迁移。该基线 schema 版本为 20261127，执行时分配当时可用的新版本。 |

R2 的外部接口参照为历史服务 `zlxlabs/online-video-history-server@390bf964` 及下载器 `jsdfhasuh/apple-music-downloader@6cae44ad`。B 站历史依赖服务端时区、认证、可见记录和分页一致性；当前计划更新没有调用私人观看历史或测试生产下载。接口 fixture、真实外部响应、真正入库和用户听感在交付时分别标记。

[日推 Sol 基线提交](https://github.com/jsdfhasuh/Emosonic-Server/commit/3889e6bf2ef50c7d81a1367ce743876a6425ccb4) · [R2 引用的历史服务契约](https://github.com/zlxlabs/online-video-history-server/blob/390bf9647585f0ff6020cc62e6cbee31face3c8c/docs/API_CLIENT.md) · [下载器控制协议](https://github.com/jsdfhasuh/apple-music-downloader/blob/6cae44ad8d45a8a333f74f79224fa53f71051a40/docs/album-discovery-control.md)
