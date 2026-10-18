<div align="center">

# 🎵 MusicArchiver

**多音源音乐解析 · WebDAV 归档上传 · 一行命令部署**

[![docker build](https://github.com/obrige/music-archiver/actions/workflows/docker.yml/badge.svg)](https://github.com/obrige/music-archiver/actions/workflows/docker.yml)
[![release](https://github.com/obrige/music-archiver/actions/workflows/release.yml/badge.svg)](https://github.com/obrige/music-archiver/actions/workflows/release.yml)
[![ghcr](https://img.shields.io/badge/ghcr.io-music--archiver-2496ED?logo=docker&logoColor=white)](https://github.com/obrige/music-archiver/pkgs/container/music-archiver)

</div>

后端 **Python/FastAPI**，前端纯静态页面（无构建）。
音源通过 `node vm` 沙箱运行任意洛雪脚本（支持 URL / 本地 .js / 文件夹 / ZIP），搜索平台随音源动态显示。

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

Web UI「音源管理」支持 5 种方式（任选）：

- 🔗 **URL 导入** —— 单个 `.js` URL
- 📄 **本地 .js** —— 选一个 `.js` 文件
- 📂 **文件夹批量** —— 选整个目录（含子目录），自动提取所有 `.js`
- 📋 **多选 .js** —— 一次选多个文件批量导入
- 📦 **ZIP 压缩包** —— 上传 ZIP 自动解压提取 `.js`；也可用 **ZIP URL 导入**（指向 `.zip` 的链接）

vm 沙箱只暴露 Node 内置模块（`vm` / `fs` / `crypto` / `zlib` / ...），无需 npm 依赖。

### 📑 订阅分组（自动定时刷新）

把一组 URL（.js 或 .zip 混合）做成一个「订阅组」，设一个刷新周期（如 60 分钟），后台到点自动重下，重下时清掉上一次的旧音源再拉新的。

- **创建**：填名称 + 间隔（分钟，≥5）→ 选 .js / .zip 类型 → 加若干 URL → 创建
- **手动刷新**：点行内「立即刷新」
- **删除**：清空组内所有音源

订阅组配置存在 `DATA_DIR/subscriptions.json`，重启不丢；后台守护线程每 30s 扫一遍，间隔到达自动触发。

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
