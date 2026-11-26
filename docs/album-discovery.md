# 国区专辑发现

从近 14 个上海日历日的正式播放记录和收藏中选出复听种子。Apple Music 国区目录提供真实发行和可播放曲序，AI 在候选编号内排序并引用种子依据。有效排名按输入复用，每位用户每天最多发起一次有候选的模型请求，与普通日推共用费用账本。

## 接入

1. Oracle 下载器需具备 [国区整专控制接口](https://github.com/jsdfhasuh/apple-music-downloader/blob/222e739/docs/album-discovery-control.md)。将控制接口接入已有的 Oracle 鉴权和交付程序，OpenWrt 按现有流程接收。
2. Supysonic 的 Web 与 daemon 使用同一数据库和同一份配置。两个进程需读取同一设置文件、密钥和 OpenWrt 的收件目录。样例见 [config.sample](../config.sample) 和 [设置 JSON](examples/album-discovery-settings.json)。
3. 设定 `target_user` 为现有 Supysonic 账号，`library_root` 为容器可见的接收目录，现有 OpenWrt 环境为 `/downloads/music/completed`。启动时 Supysonic 会应用数据库迁移。
4. 提供 Apple Music 开发者 token、Oracle 控制 token、Oracle 交付 token。用现有密钥机制挂载文件，或配置样例列出的环境变量。先读取已知国区发行和已留存清单，分别确认两份 Oracle 凭据可以使用。

## 使用

使用目标账号打开 Web「推荐 → 国区专辑发现」，生成今日预演。页面显示候选的种子、代表曲、待核对曲序和上海当日名额。每次手动确认只提交所选专辑，若有多首已核实且有依据的代表曲，可以主动改选。

`aiDiscovery.maxAlbumsPerDay` 按目标曲库控制每天的新派单数。默认 1，设为 0 暂停新单，旧单继续核对。替换设置 JSON 后，下次申请名额时生效。只有明确设置 `autoSubmit: true` 才会在上海时间 `auto_submit_hour`（默认 20 点）起按候选排名自动处理。

Oracle 状态待核实时会保留原预留，收到明确拒绝才释放。已收齐和复用的在途订单不占新单名额。接单后下载失败可在 Oracle 面板重试原任务。新任务与原订单的关联以 Oracle 记录为准，计数沿用原预留日。Web「已确认的专辑」可查看原始日期、Oracle 任务 ID 与缺项。

收件后，定时任务核实 SHA-256、文件大小、版本和本地索引。整专收齐以后，下一份尚未生成的日推最多收一首符合条件的代表曲，优先第 4 位。已生成的日推保持原序。最近 21 天的页面指标包括派单、整专收齐、实际投放和投放后的播放观察。现有播放上报无法识别入口，复听显示来源未知。

## 验收

- 在独立数据库预演，确认没有新派单、曲库写入或日推变更。改 JSON 上限后重复确认，核对两端使用同一名额。
- 在隔离 Oracle 队列模拟接单后响应丢失，重启后按原订单对账。预留日期与 Oracle 任务保持一致。再次确认同一专辑只复用原任务。
- 用历史 SHOEBOX 国区测试样本检查 1–6、8–12 的 11 条原曲序、各文件校验值和独立索引。确认 11 月 26 日已有歌单原序保留，次期 50 首中第 4 位为第 11 轨《EYES NOSE LIPS》，只出现一次。验收使用 `tests/fixtures/album_discovery/` 的隔离样本。
- 通过服务端调度器的 `album-discovery-nightly` 和 `album-discovery-reconcile` 核实两个任务分别运行。设置上限为 0 后，既有订单仍可收件和核对。模拟目录、模型或预算错误，确认普通日推继续保存。
