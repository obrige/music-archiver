<div align="center">

# 🎵 MusicArchiver

**多音源音乐解析 · WebDAV 归档上传 · 一行命令部署**

[![docker build](https://github.com/obrige/music-archiver/actions/workflows/docker.yml/badge.svg)](https://github.com/obrige/music-archiver/actions/workflows/docker.yml)
[![release](https://github.com/obrige/music-archiver/actions/workflows/release.yml/badge.svg)](https://github.com/obrige/music-archiver/actions/workflows/release.yml)
[![ghcr](https://img.shields.io/badge/ghcr.io-music--archiver-2496ED?logo=docker&logoColor=white)](https://github.com/obrige/music-archiver/pkgs/container/music-archiver)

</div>

后端 **Python/FastAPI**，前端纯静态页面（无构建）。
音源通过 **node vm 沙箱** 运行任意洛雪脚本（支持 URL / 本地 .js 导入），搜索平台随音源动态显示。

---

## 🚀 30 秒跑起来

**前置条件**：已装 [Docker](https://docs.docker.com/get-docker/)，有一个可写的 [WebDAV](https://en.wikipedia.org/wiki/WebDAV) 服务（Nextcloud / 坚果云 / 自建皆可）。

```bash
curl -fLO https://raw.githubusercontent.com/obrige/music-archiver/main/docker-compose.ghcr.yml
docker compose -f docker-compose.ghcr.yml up -d
```

浏览器打开 <http://localhost:5245>，首次启动填一下 WebDAV 凭据即可使用。

---

## ✨ 它能做什么

| | |
|---|---|
| 🔍 **多平台搜索** | 平台随当前音源动态显示；默认音源已覆盖主流平台 |
| 🎧 **试听 / 下载** | 音源沙箱解析直链，后端代理播放，无需登录平台 |
| ⬆️ **FLAC 归档** | 单首 / 批量上传至 WebDAV，自动命名 / 去重 / 校验 |
| 📝 **元数据齐全** | 同步歌词 `.lrc` + 封面 `.jpg` |
| 📋 **任务队列** | 并发 3 个（可调），SSE 实时进度，音源持久化 |
| 📚 **曲库浏览** | WebDAV 端浏览 / 删除已归档曲目 |

---

## 📦 部署方式

### 🌟 方式 1：直接拉 GHCR 镜像（推荐）

一行命令起步。多架构镜像（amd64 + arm64），自动重启 + 健康检查。

要**固定版本**避免 `latest` 漂移，编辑 compose 的 `image:` 字段：

```yaml
image: ghcr.io/obrige/music-archiver:X.Y.Z   # 或 :sha-<commit>
```

所有 tag 见 [Packages 页](https://github.com/obrige/music-archiver/pkgs/container/music-archiver)。

### 🛠️ 方式 2：本地源码构建（开发用）

```bash
git clone https://github.com/obrige/music-archiver
cd music-archiver
docker compose up -d          # 用 docker-compose.yml (build: .)
```

### 🐍 方式 3：纯 Python 跑（调试）

```bash
pip install -r requirements.txt
node bridge.mjs &             # 音源桥
uvicorn app.main:app --reload --port 5245
```

---

## ⚙️ 配置

所有配置走环境变量，在 compose 的 `environment:` 里改：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `5245` | HTTP 监听端口 |
| `DATA_DIR` | `/data` | 音源 / 任务 / 曲库元数据持久化目录 |
| `CONCURRENCY` | `3` | 同时上传 / 下载的并发数 |

> 💡 WebDAV 凭据**不**走环境变量 —— 在 Web UI 首次启动填入，存到 `DATA_DIR` 下的 SQLite。

---

## 🧩 添加音源

Web UI 的「音源管理」页三种方式均可：

- 🔗 **URL** —— 粘贴洛雪脚本 HTTP(S) 链接，自动拉取
- 📋 **粘贴源码** —— 直接粘贴 JS 代码
- 📁 **上传文件** —— 上传本地 `.js` 文件

切换音源立即生效，平台列表自动刷新。支持任何洛雪（LX Music）兼容脚本 —— vm 沙箱只暴露 Node 内置模块（`vm` / `fs` / `crypto` / `zlib` / ...），无需 npm 依赖。

---

## ❓ 常见问题

<details>
<summary><b>Q：拉 GHCR 镜像慢 / 超时？</b></summary>

GHCR 在境外，国内服务器直连可能很慢。三种方案：

1. **Docker 镜像加速**：编辑 `/etc/docker/daemon.json` 加 `registry-mirrors`（阿里云 / 腾讯云 / 高校等）
2. **第三方 GHCR mirror**：例如 `ghcr.nju.edu.cn`，`docker pull ghcr.nju.edu.cn/obrige/music-archiver:latest` 后改 compose 的 `image`
3. **海外反代**：海外 VPS 上 nginx stream 代理 `ghcr.io`

</details>

<details>
<summary><b>Q：怎么改端口？</b></summary>

改 compose 里 ports 映射：`"5245:5245"` → `"9000:5245"`。**左边**是宿主机端口，**右边**是容器端口（别动）。

</details>

<details>
<summary><b>Q：数据存在哪里？怎么备份？</b></summary>

挂载的 `./data` 目录（宿主机当前路径），包含 SQLite 数据库 + 下载的临时 FLAC。WebDAV 端的音乐本身在你自己的 WebDAV 上，不在这里。

**备份：**

```bash
tar czf music-archiver-backup-$(date +%Y%m%d).tar.gz ./data
```

</details>

<details>
<summary><b>Q：健康检查一直 unhealthy？</b></summary>

`docker compose ps` 看 STATUS。如果显示 unhealthy：

1. `docker compose logs -f` 看启动报错
2. WebDAV 凭据错也会导致启动失败（首次启动会校验连通性）
3. 检查端口冲突：`ss -tlnp | grep 5245`

</details>

<details>
<summary><b>Q：怎么升级到新版本？</b></summary>

```bash
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

`./data` 是 volume，升级不会丢数据。

</details>

<details>
<summary><b>Q：音源是哪里来的？</b></summary>

洛雪脚本是开源社区维护的 LX Music 生态 JS 脚本，通过 `node:vm` 沙箱隔离运行，**不能访问本地文件系统 / 进程 / 网络以外的资源**。本项目只是脚本的**解析执行器 + 上传归档工具**，不分发、不托管音源。

</details>

---

## 🧱 项目结构

```
app/
│  main.py           入口 / 静态托管
│  api.py            所有 API 路由
│  services.py       搜索 / FLAC / 元数据 / WebDAV 客户端
│  sources.py        音源管理 (node vm 沙箱桥)
│  queue.py          并发任务队列
bridge.mjs          洛雪脚本沙箱运行桥
crypto-js.js        洛雪脚本沙箱使用的加密库
web/static/         前端 (HTML/JS/CSS, 无构建)
docker-compose.yml       本地构建 (build: .)
docker-compose.ghcr.yml  直接拉 GHCR 镜像 (推荐)
.github/workflows/
  docker.yml     CI: 构建并推送 Docker 镜像到 GHCR
  release.yml    CI: 打包源码 zip + tar.gz 并创建 Release
```

---

## 🤖 CI/CD

| 触发 | 行为 |
|---|---|
| push `main` | 构建并推 GHCR，tag `latest` + `main` + `sha-<long>` |
| push tag `v*` | 同上 + tag `X.Y.Z` 系列；自动打包并上传 Release 源码包 |
| pull request | 仅构建，不推送（tag `pr-<n>`） |
| 手动 `workflow_dispatch` | 手动触发 |

<div align="center">

# 🎵 MusicArchiver

**多音源音乐解析 · WebDAV 归档上传 · 一行命令部署**

[![docker build](https://github.com/obrige/music-archiver/actions/workflows/docker.yml/badge.svg)](https://github.com/obrige/music-archiver/actions/workflows/docker.yml)
[![release](https://github.com/obrige/music-archiver/actions/workflows/release.yml/badge.svg)](https://github.com/obrige/music-archiver/actions/workflows/release.yml)
[![ghcr](https://img.shields.io/badge/ghcr.io-music--archiver-2496ED?logo=docker&logoColor=white)](https://github.com/obrige/music-archiver/pkgs/container/music-archiver)

</div>

后端 **Python/FastAPI**，前端纯静态页面（无构建）。音源通过 `node vm` 沙箱运行任意洛雪脚本（URL / 本地 .js 导入），搜索平台随音源动态显示。

---

## 🚀 30 秒跑起来

前置：[Docker](https://docs.docker.com/get-docker/) + 可写 [WebDAV](https://en.wikipedia.org/wiki/WebDAV) 服务。

```bash
curl -fLO https://raw.githubusercontent.com/obrige/music-archiver/main/docker-compose.ghcr.yml
docker compose -f docker-compose.ghcr.yml up -d
```

打开 <http://localhost:5245>，首次启动填 WebDAV 凭据即可。

---

## ✨ 功能

- 🔍 多平台搜索（随音源动态显示）
- 🎧 试听 / 下载（沙箱解析直链，后端代理）
- ⬆️ FLAC → WebDAV 归档（自动命名 / 去重 / 校验 + 歌词 `.lrc` + 封面 `.jpg`）
- 📋 任务队列（并发 3，SSE 进度，音源持久化）
- 📚 曲库浏览 / 删除

## ⚙️ 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `5245` | HTTP 端口 |
| `DATA_DIR` | `/data` | 元数据持久化目录（SQLite 等） |
| `CONCURRENCY` | `3` | 上传/下载并发数 |

WebDAV 凭据在 Web UI 填入（存 SQLite），不走环境变量。

## 🧩 添加音源

Web UI「音源管理」支持：URL 拉取、粘贴源码、上传 `.js` 文件。vm 沙箱只暴露 Node 内置模块（`vm` / `fs` / `crypto` / `zlib` / ...）。

## ❓ FAQ

- **拉镜像慢？** GHCR 在境外，用 Docker daemon `registry-mirrors`、第三方 GHCR mirror（如 `ghcr.nju.edu.cn`）、或海外 nginx stream 反代。
- **改端口？** compose 里 `"5245:5245"` → `"<host>:5245"`，改左不改右。
- **升级？** `docker compose -f docker-compose.ghcr.yml pull && up -d`，`./data` volume 不丢。
- **数据在哪？** 挂载的 `./data`（宿主机），WebDAV 端音乐在你自己的 WebDAV。备份：`tar czf data-$(date +%F).tar.gz ./data`。

## 📦 其他部署

- **本地构建**（开发）：`git clone ... && docker compose up -d`（用 `docker-compose.yml`，`build: .`）
- **纯 Python**（调试）：`pip install -r requirements.txt && node bridge.mjs &` + `uvicorn app.main:app --reload --port 5245`

## 🤖 发版

```bash
git tag vX.Y.Z && git push origin vX.Y.Z
```

docker + release workflow 自动跑：推 GHCR 镜像 + 打包源码 zip/tar.gz 到 Release。带 `-` 的 tag 自动 prerelease。

## 🤝 贡献

PR 欢迎。功能改动建议先开 Issue 讨论。
---

## 🤝 贡献

欢迎 PR。建议：

- 🆕 **新增平台**：扩展 `app/services.py` 或提交兼容音源脚本
- 🐛 **Bug 报告**：提供复现步骤 + 日志片段
- 💡 **新功能**：先开 Issue 讨论，避免大改冲突
