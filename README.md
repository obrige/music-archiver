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

\`\`\`
app/          Python后端(FastAPI)
  main.py     入口/静态托管
  api.py      全部 API 路由
  services.py 搜索/FLAC/元数据/WebDAV 客户端
  sources.py  音源管理(node 沙箱桥)
  queue.py    并发任务队列
bridge.mjs    洛雪音源脚本运行桥(node 子进程 + vm 沙箱)
web/static/   前端(原生 HTML/JS/CSS, 无构建)
\`\`\`
