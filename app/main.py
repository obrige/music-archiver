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
    yield
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
    return {'ok': True, 'sources': len(sm.list()), 'tasks': len(tq.list())}


if STATIC_DIR.exists():
    app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')


@app.get('/')
def index():
    return FileResponse(str(STATIC_DIR / 'index.html'))


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', '5245')))
