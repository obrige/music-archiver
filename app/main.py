import logging
import logging.handlers
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .queue import TaskQueue
from .sources import SourceManager, SubscriptionManager, AutoUpdater

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get('DATA_DIR', str(BASE / 'data'))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONCURRENCY = int(os.environ.get('CONCURRENCY', '3'))
STATIC_DIR = BASE / 'web' / 'static'
BRIDGE = BASE / 'bridge.mjs'
NODE = os.environ.get('NODE', 'node')

# ---- 日志配置 ----
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
LOG_FILE = os.environ.get('LOG_FILE', str(DATA_DIR / 'logs' / 'app.log'))
LOG_FORMAT = '%(asctime)s %(levelname)s %(name)s [%(filename)s:%(lineno)d] %(message)s'

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format=LOG_FORMAT,
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('musicarchiver')

# 文件日志: 可旋转, 防磁盘爆
if LOG_FILE:
    try:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding='utf-8'
        )
        fh.setFormatter(logging.Formatter(LOG_FORMAT, datefmt='%Y-%m-%d %H:%M:%S'))
        fh.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
        logging.getLogger().addHandler(fh)
        log.info('文件日志已启动: %s', LOG_FILE)
    except Exception as e:
        log.warning('无法启动文件日志(%s): %s', LOG_FILE, e)

# uvicorn 默认 logger 接管(确保 access 日志格式一致)
for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
    lg = logging.getLogger(name)
    lg.handlers = []  # 避免重复打印
    lg.propagate = True  # 走 root

sm = SourceManager(DATA_DIR, BRIDGE, NODE)
subm = SubscriptionManager(DATA_DIR, sm)
auto_updater = AutoUpdater(subm)
tq = TaskQueue(CONCURRENCY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    sm.init()
    subm.init()
    tq.start()
    auto_updater.start()
    log.info('music-archiver 启动完成: %d 音源, %d 订阅组, 并发 %d',
             len(sm.list()), len(subm.list()), CONCURRENCY)
    yield
    log.info('music-archiver 关闭中...')
    auto_updater.stop()
    sm.close_all()


app = FastAPI(title='MusicArchiver', lifespan=lifespan)
app.state.sm = sm
app.state.subm = subm
app.state.tq = tq
app.state.data_dir = DATA_DIR

from . import api
app.include_router(api.router)


@app.get('/api/health')
def health():
    return {
        'ok': True,
        'sources': len(sm.list()),
        'subscriptions': len(subm.list()),
        'tasks': len(tq.list()),
    }


if STATIC_DIR.exists():
    app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')


@app.get('/')
def index():
    return FileResponse(str(STATIC_DIR / 'index.html'))


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', '5245')))
