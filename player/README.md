# EmoSonic 无头播放端与 SSH CLI

独立 Python 播放组件，不导入 Supysonic 的 Flask/数据库运行时。一个
`emosonic-playerd` 管理一个 mpv。手机/电脑 App 与本地 CLI 共用服务端
canonical 播放上下文、authority、队列及 controlVersion；CLI 不直接控制
mpv，不维护第二份业务队列。

## 范围和验证边界

第一阶段仅 Core：播放、暂停、定位、音量、队列、自然下一首、状态与恢复。
Handoff、Follow、Broadcast 不支持，不声明相应能力。手机退出但播放端仍
连接服务器时，不应停播；播放端与服务器失联时暂停，重新确认身份和上下文
后才接受新命令，不重放旧控制。

本目录提供开发代码、测试和部署材料，**不表示已在用户 OpenWrt 部署**。
USB 实际出声、音质、三首连续播放、拔插、资源趋势和对现有 HA/网络服务的
影响均需实机验收。容器构建通过、IPC success、null 输出测试都不等同实际
声卡出声；未有记录的实机项目状态为 **NOT_RUN**。

基线服务端协议为 2.9.0。当前仓库的生产 conformance readiness 未就绪，
本组件不会将其改为 true、关闭校验，或额外要求精确 schemaHash/buildCommit。
部署前核对**实际运行服务器**的版本、既有开发/实验模式及 Core readiness。
真实握手拒绝时应修复配置/协议原因，不伪造注册成功。假服务器测试不能替代
真实服务器的全新 clientId、零上下文注册和第一次播放贯通验收。

## 开发和构建（在自己的 Linux 开发环境）

需要 Python 3.12+；宿主开发测试另外安装官方来源的 mpv。不要把容器用的
Alpine 包直接安装到 OpenWrt。

```sh
python3 -m venv .venv-player
. .venv-player/bin/activate
# Server dependencies are test-only; the player image does not install them.
python -m pip install -r requirements.txt -r ci-requirements.txt ./player
python -m pip check
EMOSONIC_REAL_MPV_TESTS=1 python -m unittest discover -s player/tests -v
emosonic --help
emosonic-playerd --help
```

从仓库根目录构建独立 ARM64 镜像（不发布、不部署）：

```sh
docker buildx build --platform linux/arm64 \
  --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" \
  -f player/Dockerfile -t emosonic-player:local --load player
```

CI `.github/workflows/player-build.yaml` 是独立 build-only 流程：测试、ARM64
镜像构建、非音频入口检查，并保存构建元数据。没有 registry 登录、push、
部署或服务端镜像操作。不要改成 `emosonic-server` 镜像名。只有实际 CI 结果
才是构建证据，提交工作流本身不是成功证明。

基础镜像固定为官方 `python:3.12.16-alpine3.23`，mpv 固定为 Alpine 3.23
的 `0.40.0-r8`，tini 固定为 `0.19.0-r3`，Python 依赖闭包固定在 pyproject。
镜像内保存 `/opt/player/python-packages.txt` 和 `system-packages.txt`。
基础镜像标签及系统间接依赖不是不可变快照；正式发布时记录实际 image digest
及包清单。不能仅看 mpv 包大小推断最终镜像体积。构建会检查 ALSA 输出是否存在。

## 配置和凭据

复制 `config.example.json` 到仓库外的私有目录，填入实际服务器 HTTPS 地址、
用户名和**设备枚举得到的明确 ALSA 名称**。默认拒绝明文 HTTP；
`allow_insecure_http` 仅供隔离本地开发使用，不用于真实账户的公网连接。

密码通过 `password_file` 指定的独立文件读取，不提供命令行密码参数。
不要提交密码文件，不要把含 token 的 stream URL 发到聊天、日志或进程参数。
文件应归容器 UID 10001 所有、仅该 UID 可读（0400/0600），其父目录同样限制
访问；具体文件准备由有权限的部署人员操作。Compose 只读挂载该文件。

`client_id_file` 保存稳定设备身份；`state_dir` 的数据应随升级保留，不能将
含同一 clientId 的卷复制给另一个同时运行的播放器。`runtime_dir` 存放本机
控制 socket 和 mpv IPC，权限 0700，不公开 TCP 端口，不挂载到其他容器。
`initial_volume` 默认 20，但仍需确认硬件混音器、功放与音箱音量。

## App 和 CLI

App 选择“OpenWrt 音箱”后即可使用已实现的 Core 控制。SSH 端可以在同一
播放器容器内运行 CLI，不需要给宿主安装 Python，也不需要公开 socket：

```sh
# 下列操作要求已由用户完成并验收临时部署。
docker compose -f player/compose.yaml exec player emosonic status
docker compose -f player/compose.yaml exec player emosonic search "歌曲名"
docker compose -f player/compose.yaml exec player emosonic play SONG_ID
docker compose -f player/compose.yaml exec player emosonic pause
docker compose -f player/compose.yaml exec player emosonic resume
docker compose -f player/compose.yaml exec player emosonic next
docker compose -f player/compose.yaml exec player emosonic queue
```

可用参数以 `emosonic --help` 和各子命令帮助为准。CLI 失败必须返回非零退出码；
搜索结果和状态不得显示鉴权流 URL。必须验证手机点播后 CLI 暂停、CLI 切歌后
手机状态更新，以及两边并发控制不重复推进/覆盖较新版本。

## OpenWrt 临时部署前置检查（由用户执行）

用户确认 USB 声卡曾经能正常播放。交接附件另有“驱动未加载/no soundcards”的
时间点记录，尚未独立复验；不要假定当前有声卡，也不要因此重装驱动或修改
既有可用音频设置。

部署者先核实当前内核与匹配的 `snd_usb_audio`、ALSA 设备、数字声卡组 GID、
播放 PCM 节点及控制节点。不要硬编码 `hw:0,0`，不要盲装与定制内核不匹配的
kmod。已有 Docker 不代表内核驱动或音频节点已可用。

Compose 要求明确设置以下非敏感环境变量；缺失时拒绝展开：

- `PLAYER_CONFIG`：仓库外配置文件的绝对路径。
- `PLAYER_PASSWORD_FILE`：受限凭据文件绝对路径，内容不是环境变量。
- `AUDIO_GID`：实际 ALSA 节点组的数字 GID。
- `ALSA_PCM_DEVICE`：确认的 USB 播放 PCM 节点绝对路径。
- `ALSA_CONTROL_DEVICE`：同一声卡控制节点绝对路径。
- `PLAYER_IMAGE`：经验证的独立播放器镜像，默认 `emosonic-player:local`。

Compose 仅映射所选两个 ALSA 节点，无 privileged、host network、公开端口、
Docker socket、CAP_SYS_MODULE；默认 bridge。播放器非 root、只读根文件系统，
使用私有 tmpfs 和状态卷。设备发生编号变化时须重新枚举并更新配置；拔插后
受控重建**仅播放器容器**，不承诺映射自动覆盖热插拔，也不自动恢复旧命令。

临时部署须低音量、短片段、有时限并能停止。确认 USB 音箱实际出声且现有服务
未受影响后，才另行安排驱动和播放器开机持久化。此材料不自动执行这些步骤，
Compose `restart: "no"` 是有意的。资源限额待空闲/播放 RSS、CPU、缓存实测后
设置，只限制新播放器，不修改现有服务。

## 停止与回滚

对已经获准部署的实例，先停止新播放器：

```sh
docker compose -f player/compose.yaml stop player
```

按部署前备份恢复本组件配置及原混音器状态。不要加 `down -v` 丢弃设备身份；
不要为回滚卸载音频模块、重启 OpenWrt、修改 Emosonic Server/HA/数据库或网络
服务。SIGTERM 应终止和回收 mpv；上线前检查停止后没有残留声音和第二个 mpv。

## 验收顺序

1. 全新设备注册 → App 可见 → 建立上下文 → canonical 队列确认 → 第一首实际
   播放 → App 状态正确 → 自然结束下一首。重启已有身份再验证恢复。
2. 测试 seek/音量/暂停、相同歌曲多个 occurrence、next 与 EOF 并发、过期控制、
   迟到 mpv 事件、断线和重连；确认不重放、不双响、不漏掉同一 run 合法 EOF。
3. 实际 USB 放音至少跨三首、手机退出、声卡拔插、进程退出、资源趋势和现有
   HA/网络服务检查；各项单独记录 PASS/FAIL/NOT_RUN。
4. 以后单独实现和验收 Handoff；不得由 Core 成功推断高级同步能力。

参考：[mpv IPC](https://mpv.io/manual/stable/#json-ipc)、
[Python Socket.IO](https://python-socketio.readthedocs.io/en/stable/client.html)、
[官方 Python 镜像](https://hub.docker.com/_/python)、
[Alpine ARM64 mpv](https://pkgs.alpinelinux.org/package/v3.23/community/aarch64/mpv)。

### Current protocol limitation
The selected server's strict contract rejects duplicate song IDs in one queue
(`strict_v2_contract.py`, `_normalize_string_array`). A playlist containing the
same ID twice is rejected explicitly; it is never silently deduplicated. This
is a discovered difference from the handoff's duplicate-occurrence example.
Repeated executions of the same song still have distinct mpv run identities.
