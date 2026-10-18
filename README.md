# MusicArchiver

多音源音乐解析 + WebDAV 归档上传。后端 **Python/FastAPI**, 前端**纯静态页面(亮色系, 无构建)**。
音源为**洛雪音源脚本**(JS), 通过 **node 子进程 + vm 沙箱**运行, 支持 URL / 本地 .js 导入任意洛雪生态脚本。
搜索平台跟随当前音源声明的平台**动态显示**。

## 功能

- 搜索: 平台随音源动态
- 试听/下载: 音源沙箱解析直链, 后端代理播放
- 上传: 单首/批量, FLAC -> WebDAV, 自动命名/去重/fLaC校验, 歌词 .lrc + 封面 .jpg
- 任务: 并发队列(默认3), SSE 实时进度, 音源持久化
- 曲库: WebDAV 浏览/删除

## 目录结构

```
app/             Python后端(FastAPI)
  main.py        入口/静态托管
  api.py         全部 API 路由
  services.py    搜索/FLAC/元数据/WebDAV 客户端
  sources.py     音源管理(node 沙箱桥)
  queue.py       并发任务队列
  bridge.mjs     洛雪音源脚本运行桥(node 子进程 + vm 沙箱)
web/static/      前端(原生 HTML/JS/CSS, 无构建)
.github/workflows/
  docker.yml     CI: 构建并推送 Docker 镜像到 GHCR
  release.yml    CI: 打包源码 zip + tar.gz 并创建 GitHub Release
```

## 部署 & 持续集成

### 本地运行

使用 docker-compose:

```bash
docker compose up -d
```

或手动运行:

```bash
pip install -r requirements.txt
node bridge.mjs &      # 音源桥
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Docker 镜像 (GHCR)

`.github/workflows/docker.yml` 在以下时机自动构建并推送到 `ghcr.io/obrige/music-archiver`:

| 触发条件 | 标签 |
| --- | --- |
| push `main` | `latest`, `main`, `sha-<long>` |
| push tag `v*` | `1.2.3`, `1.2`, `1`, `latest`, `sha-<long>` |
| pull request | `pr-<n>` (仅构建, 不推送) |
| 手动 `workflow_dispatch` | 默认 `latest` |

启用 GHA 缓存 (`type=gha`) 加速重复构建。

```bash
docker pull ghcr.io/obrige/music-archiver:latest
docker run -d --name music-archiver -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  ghcr.io/obrige/music-archiver:latest
```

### Release 源码包

`.github/workflows/release.yml` 在 push tag `v*` (或 Actions 页面手动 `Run workflow`) 时自动:

1. 打包源码 `zip` + `tar.gz` (排除 `.git`、`.github`、`dist`、`__pycache__`、`node_modules`、`venv` 等)
2. 创建 GitHub Release 并附上压缩包, 自动生成 release notes

```bash
git tag v1.0.0
git push origin v1.0.0
# → https://github.com/obrige/music-archiver/releases/tag/v1.0.0
```

带 `-` 的 tag (如 `v1.0.0-rc1`) 会自动标记为 prerelease。
