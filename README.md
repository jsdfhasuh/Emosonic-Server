# Emosonic Server

Emosonic Server 是基于 Flask 的音乐流媒体与运维平台，脱胎于 [spl0k/supysonic](https://github.com/spl0k/supysonic)，当前分支由 jsdfhasuh 持续扩展与维护。

## 功能特性

- 浏览音乐库，支持按文件夹或标签查看
- 流式播放多种音频文件格式
- 音频转码
- 用户播放列表和随机播放列表
- 封面图片管理
- 曲目/专辑收藏与评分
- Last.fm scrobbling
- ListenBrainz scrobbling
- Jukebox 模式
- 从 Spotify、Last.fm 等渠道补全缺失的艺术家封面、专辑封面和专辑年份
- 通过本地 NFO 文件组织艺术家信息、专辑信息和曲目信息
- Web 端艺术家信息维护
- Emosonic 扩展 API 与 Socket.IO 能力

## Docker 镜像

项目镜像通过 GitHub Actions 自动发布到 GitHub Container Registry：

```bash
docker pull ghcr.io/jsdfhasuh/emosonic-server:latest
```

可用镜像地址：

```text
ghcr.io/jsdfhasuh/emosonic-server
```

常用 tag：

```text
latest        默认分支 master 构建出的最新版
master        master 分支构建结果
1.0.0         通过 git tag v1.0.0 发布的正式版本
1.0           通过 git tag v1.0.0 自动生成的大版本/小版本 tag
sha-xxxxxxx   对应具体 commit 的短 SHA，方便回滚和排查
```

正式部署建议优先使用明确版本号，例如：

```bash
docker pull ghcr.io/jsdfhasuh/emosonic-server:1.0.0
```

`latest` 适合测试和快速体验，不建议在生产环境只依赖 `latest`。

## 快速运行

先复制并修改配置文件：

```bash
cp config.sample supysonic.conf
```

使用 GHCR 镜像运行：

```bash
docker run -d \
  --name emosonic-server \
  -p 5000:5000 \
  -v /path/to/your/music:/music \
  -v /path/to/your/config/supysonic.conf:/app/supysonic.conf \
  -v /path/to/your/logs:/log \
  ghcr.io/jsdfhasuh/emosonic-server:latest
```

访问地址：

```text
http://服务器IP:5000
```

如果 GHCR 镜像被设置为私有，先登录再拉取：

```bash
echo YOUR_GITHUB_TOKEN | docker login ghcr.io -u jsdfhasuh --password-stdin
```

## 本地构建

也可以在仓库根目录自行构建镜像：

```bash
docker build -t emosonic-server .
```

运行本地构建的镜像：

```bash
docker run -d \
  --name emosonic-server \
  -p 5000:5000 \
  -v /path/to/your/music:/music \
  -v /path/to/your/config/supysonic.conf:/app/supysonic.conf \
  emosonic-server
```

## 发布镜像

Docker 镜像发布由 `.github/workflows/docker-publish.yaml` 管理。

推送到 `master` 分支会自动构建并发布：

```bash
git push origin master
```

发布正式版本时，建议打 Git tag：

```bash
git tag v1.0.0
git push origin v1.0.0
```

触发后会生成类似以下镜像：

```text
ghcr.io/jsdfhasuh/emosonic-server:1.0.0
ghcr.io/jsdfhasuh/emosonic-server:1.0
ghcr.io/jsdfhasuh/emosonic-server:sha-xxxxxxx
```

## 配置说明

默认会从以下位置读取配置：

```text
/etc/supysonic
~/.supysonic
~/.config/supysonic/supysonic.conf
./supysonic.conf
```

常用配置文件为：

```text
supysonic.conf
```

建议从 `config.sample` 复制后修改。常见挂载方式：

```bash
-v /path/to/your/config/supysonic.conf:/app/supysonic.conf
```

扫描器生成的临时元数据默认位于 `/tmp/supysonic`。如需固定目录，可在 `[base]` 中配置：

```ini
[base]
tempdatafolder = /var/supysonic/tempdata
```

## 开发环境

如果只需要使用本地 Python 环境进行开发或维护，可先激活自己的环境：

```bash
conda activate supysonic
```

安装依赖：

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
```

运行测试：

```bash
coverage run -m unittest
```

网络相关测试可单独运行：

```bash
coverage run -a -m unittest tests.net.suite
```

## script

`build_nfo.py` 可用于组织本地音乐文件的 NFO 文件。

## NFO 文件格式

1. NFO 文件必须命名为 `album.nfo`，并放置在曲目文件夹中。
2. NFO 文件必须是 XML 格式。
3. NFO 文件必须包含以下标签：
   - `<album>`：专辑信息根元素。
   - `<track>`：曲目信息。
   - `<lock_data>`：布尔值，表示数据是否被锁定，可选。
4. 每个 `<track>` 元素中建议包含：
   - `<title>`：曲目标题。
   - `<cdnum>`：CD 编号，必须是整数。
   - `<position>`：曲目在 CD 中的位置。
   - `<artist>`：曲目艺术家。
5. 专辑层级建议包含：
   - `<artist>`：艺术家。
   - `<albumartist>`：专辑艺术家。
   - `<year>`：专辑年份，可选。

示例：

```xml
<?xml version="1.0" encoding="utf-8"?>
<album>
  <lock_data>False</lock_data>
  <track>
    <title>Many Shades Of Black</title>
    <cdnum>1</cdnum>
    <position>10</position>
    <artist>Adele</artist>
  </track>
  <track>
    <title>Best For Last</title>
    <cdnum>1</cdnum>
    <position>02</position>
    <artist>Adele</artist>
  </track>
  <artist>Adele</artist>
  <albumartist>Adele</albumartist>
</album>
```

## 相关链接

- [Supysonic](https://github.com/spl0k/supysonic)
- [Last.fm][lastfm]
- [ListenBrainz][listenbrainz]

[lastfm]: https://www.last.fm/
[listenbrainz]: https://listenbrainz.org/
