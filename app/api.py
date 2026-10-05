# -*- coding: utf-8 -*-
"""全部 API 路由(Python 版 + 洛雪音源沙箱)"""
import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .services import (SEARCH_IMPL, dav_delete, dav_mkcol, dav_propfind,
                       dav_put, download_bytes, download_head, get_platform_lyric,
                       get_wy_lyric, hot_search, measure_spec, parse_flac_info,
                       safe_name, search_tip, spec_from_flac, upgrade_pic)

router = APIRouter()
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36'
PLATFORM_NAMES = {'wy': '网易云', 'kg': '酷狗', 'tx': 'QQ音乐', 'kw': '酷我', 'mg': '咪咕', 'local': '本地'}


# ---------- 配置 ----------
def _cfg_path(request):
    return request.app.state.data_dir / 'config.json'


def _load_cfg(request):
    f = _cfg_path(request)
    if f.exists():
        try:
            return json.loads(f.read_text('utf-8'))
        except Exception:
            pass
    # 默认指向 Docker Desktop 宿主机 WebDAV(未保存过配置时生效)
    return {'url': 'http://host.docker.internal:5244/dav', 'user': '', 'pass': ''}


def _save_cfg(request, cfg):
    f = _cfg_path(request)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), 'utf-8')


@router.get('/api/config')
def get_config(request: Request):
    cfg = _load_cfg(request)
    return {'webdav': {'url': cfg.get('url', ''), 'user': cfg.get('user', ''), 'pass': ''}}


@router.post('/api/config')
def post_config(body: dict, request: Request):
    b = body or {}
    cfg = _load_cfg(request)
    cfg['url'] = str(b.get('url') or '').strip()
    cfg['user'] = str(b.get('user') or '')
    p = str(b.get('pass') or '')
    if p and p != '***':  # 空或掩码时不覆盖原密码
        cfg['pass'] = p
    _save_cfg(request, cfg)
    return {'ok': True}


# ---------- 搜索 / 歌词 / 解析 ----------
@router.get('/api/debug/search-tx')
def debug_search_tx(q: str = '晴天'):
    from .services import zzc_sign
    import random as _rd
    data = {
        'comm': {'ct': '11', 'cv': '14090508', 'v': '14090508', 'tmeAppID': 'qqmusic',
                 'phonetype': 'EBG-AN10', 'deviceScore': '553.47', 'devicelevel': '50',
                 'newdevicelevel': '20', 'rom': 'HuaWei/EMOTION/EmotionUI_14.2.0', 'os_ver': '12',
                 'OpenUDID': '0', 'OpenUDID2': '0', 'QIMEI36': '0', 'udid': '0', 'chid': '0',
                 'aid': '0', 'oaid': '0', 'taid': '0', 'tid': '0', 'wid': '0', 'uid': '0',
                 'sid': '0', 'modeSwitch': '6', 'teenMode': '0', 'ui_mode': '2', 'nettype': '1020', 'v4ip': ''},
        'req': {'module': 'music.search.SearchCgiService', 'method': 'DoSearchForQQMusicMobile',
                'param': {'search_type': 0, 'searchid': str(_rd.random())[2:],
                          'query': q, 'page_num': 1, 'num_per_page': 5, 'highlight': 0,
                          'nqc_flag': 0, 'multi_zhida': 0, 'cat': 2, 'grp': 1, 'sin': 0, 'sem': 0}},
    }
    text = json.dumps(data, separators=(',', ':'))
    sign = zzc_sign(text)
    try:
        import urllib.request as _ur
        req = _ur.Request('https://u.y.qq.com/cgi-bin/musics.fcg?sign=' + sign, method='POST', headers={
            'User-Agent': 'QQMusic 14090508(android 12)', 'Content-Type': 'application/json',
        }, data=text.encode())
        with _ur.urlopen(req, timeout=15) as r:
            raw = r.read().decode('utf-8', 'replace')
        parsed = json.loads(raw)
        body = ((parsed.get('req') or {}).get('data') or {}).get('body') or {}
        song = body.get('song') or {}
        itemlist = song.get('itemlist') or song.get('list') or []
        return {'sign': sign, 'bodyKeys': list(body.keys()), 'songKeys': list(song.keys())[:20], 'itemlistLen': len(itemlist) if isinstance(itemlist, list) else -1, 'item0': (itemlist[0] if isinstance(itemlist, list) and itemlist else {}).get('title')}
    except Exception as e:
        raise HTTPException(502, str(e))


@router.get('/api/debug/search-mg')
def debug_search_mg(q: str = '晴天'):
    from .services import mg_sign, MG_DEVICE
    import time as _t
    ts = str(int(_t.time() * 1000))
    search_switch = urllib.parse.quote(json.dumps({"song": 1, "album": 0, "singer": 0, "tagSong": 1, "mvSong": 0, "bestShow": 1, "songlist": 0, "lyricSong": 0}))
    url = ('https://jadeite.migu.cn/music_search/v3/search/searchAll?isCorrect=0&isCopyright=1&searchSwitch=%s&pageSize=5&text=%s&pageNo=1&sort=0&sid=USS' % (search_switch, urllib.parse.quote(q)))
    try:
        from .services import http_get
        raw = http_get(url, headers={
            'uiVersion': 'A_music_3.6.1', 'deviceId': MG_DEVICE, 'timestamp': ts,
            'sign': mg_sign(ts, q), 'channel': '0146921',
            'User-Agent': 'Mozilla/5.0 (Linux; U; Android 11.0.0; zh-cn; MI 11 Build/OPR1.170623.032) AppleWebKit/534.30 (KHTML, like Gecko) Version/4.0 Mobile Safari/534.30',
        }, timeout=15)
        return raw[:1500]
    except Exception as e:
        raise HTTPException(502, str(e))


@router.post('/api/search')
def search(body: dict, request: Request):
    b = body or {}
    query = str(b.get('query') or '').strip()
    platform = str(b.get('platform') or 'wy')
    limit = int(b.get('limit') or 30)
    if not query:
        raise HTTPException(400, 'query 不能为空')
    sm = request.app.state.sm
    # 脚本原生搜索优先(音源声明 search/musicSearch action 时)
    script_sources = [h for h in sm.handles_for(platform) if 'search' in ((h.sources.get(platform) or {}).get('actions') or []) or 'musicSearch' in ((h.sources.get(platform) or {}).get('actions') or [])]
    impl = SEARCH_IMPL.get(platform)
    if not impl and not script_sources:
        raise HTTPException(400, '不支持的搜索平台: %s' % platform)
    last_err = None
    if script_sources:
        for h in script_sources:
            try:
                songs = h.search(platform, query, 1, min(limit, 30))
                if songs:
                    return {'list': songs, 'total': len(songs), 'source': platform, 'script': h.name}
            except Exception as e:
                last_err = e
    if impl:
        try:
            return impl(query, limit)
        except Exception as e:
            raise HTTPException(502, str(e))
    raise HTTPException(502, str(last_err) if last_err else '脚本搜索失败')


@router.get('/api/hot')
def hot(platform: str = 'wy'):
    try:
        return hot_search(platform)
    except Exception as e:
        raise HTTPException(502, str(e))


@router.get('/api/search/tip')
def tip(platform: str = 'wy', keyword: str = ''):
    if not keyword:
        return {'source': platform, 'list': []}
    try:
        return search_tip(platform, keyword)
    except Exception as e:
        raise HTTPException(502, str(e))


@router.get('/api/lyric')
def lyric(platform: str = 'wy', id: str = ''):
    if not id:
        raise HTTPException(400, 'id 不能为空')
    try:
        if platform == 'wy':
            return get_wy_lyric(id)
        return get_platform_lyric(platform, {'id': id, 'name': '', 'hash': id, 'songmid': id, 'rid': id})
    except Exception as e:
        raise HTTPException(502, str(e))


@router.post('/api/resolve-all/stream')
async def resolve_all_stream(body: dict, request: Request):
    import queue as _q
    import threading as _th
    platform = str(body.get('platform') or '')
    song = body.get('song') or {}
    sm = request.app.state.sm
    handles = sm.handles_for(platform)
    mp3_black = {'128k', '192k', '320k'}
    rq = _q.Queue()

    def worker(h):
        try:
            qs = [q for q in ((h.sources.get(platform) or {}).get('qualitys') or []) if q not in mp3_black]
            if not qs:
                rq.put(('err', h.name, []))
                return
            r = h.resolve_multi(platform, song, qs, overall_timeout=30)
            entries = []
            for q, url in r.items():
                if not url:
                    continue
                head, size = download_head(url, 16384, 15)
                if not head or head[:4] != b'fLaC':
                    continue
                spec = measure_spec(head, q)
                fi = parse_flac_info(head)
                entries.append({'quality': q, 'url': url, 'spec': spec, 'size': size,
                                'sr': fi['sr'] if fi else 0, 'bits': fi['bits'] if fi else 0})
            rq.put(('ok', h.name, entries))
        except Exception:
            rq.put(('err', h.name, []))

    threads = []
    for hh in handles:
        t = _th.Thread(target=worker, args=(hh,), daemon=True)
        t.start()
        threads.append(t)
    remaining = len(threads)

    async def gen():
        nonlocal remaining
        while remaining > 0:
            if await request.is_disconnected():
                break
            try:
                kind, name, entries = rq.get(timeout=1)
                remaining -= 1
                if entries:
                    yield 'data: ' + json.dumps({'source': name, 'list': entries}, ensure_ascii=False) + '\n\n'
            except _q.Empty:
                continue
        yield 'data: ' + json.dumps({'done': True}) + '\n\n'

    return StreamingResponse(gen(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})



def _norm_rel(href, base_path):
    """从 propfind href 得到规范化相对路径: 解实体后重新编码(& → %26, 空格 → %20 等)"""
    if not href or not href.startswith(base_path):
        return ''
    rel = href[len(base_path):].lstrip('/').rstrip('/')
    return urllib.parse.quote(urllib.parse.unquote(rel), safe='/')


def _norm_for_match(s):
    """匹配前归一化: 与 safe_name 一致——/ \ 转 &(WebDAV 路径安全), 特殊字符转全角"""
    s = (s or '').strip().lower()
    s = s.replace('/', '&').replace('\\', '&')
    for ch, rep in [('?', '？'), ('#', '＃'), ('*', '＊'), ('%', '％'), (':', '：'), ('"', '＂')]:
        s = s.replace(ch, rep)
    return s


def _match_song(r, song_name, singer):
    """歌曲精确匹配: 歌名/歌手归一化后一字不差(忽略大小写/空格, / \ 视为 &)"""
    nm = _norm_for_match(song_name)
    sg = _norm_for_match(singer)
    for x in (r.get('list') or []):
        xn = _norm_for_match(x.get('name'))
        xs = _norm_for_match(x.get('singer'))
        if xn != nm:
            continue
        if sg and xs and xs != sg:
            continue
        return x
    return None


def _run_backfill(task_q, task, cfg, sm):
    import threading as _th
    base_path = urllib.parse.urlparse(cfg['url']).path.rstrip('/')
    try:
        items = dav_propfind(cfg, '', '1')
    except Exception as e:
        task_q.log(task, '无法读取曲库根目录: ' + str(e))
        return
    folders = [it for it in items if it.get('isDir') and it.get('displayname')
               and urllib.parse.unquote(it.get('href') or '').rstrip('/') != base_path]
    planned = max(len(folders), 1)
    for fi, folder in enumerate(folders):
        total = fi + 1
        name = folder['displayname']
        if ' - ' in name:
            song_name, singer = name.rsplit(' - ', 1)
        else:
            song_name, singer = name, ''
        task_q.log(task, '== %s ==' % name)
        href = folder['href']
        rel_enc = _norm_rel(href, base_path)
        try:
            existing_items = dav_propfind(cfg, rel_enc, '1')
        except Exception:
            existing_items = []
        existing_specs = set()
        for it in existing_items:
            dn = it.get('displayname') or ''
            if dn.endswith('.flac') and '(' in dn and dn.rfind(')') > dn.find('('):
                existing_specs.add(dn[dn.find('(') + 1:dn.rfind(')')])
        has_lrc = any((it.get('displayname') or '').endswith('.lrc') for it in existing_items)
        has_jpg = any((it.get('displayname') or '').endswith('.jpg') for it in existing_items)
        lock = _th.Lock()
        bar = {'n': 0}
        first_lrc = [None]
        first_jpg = [None]

        def platform_work(platform):
            try:
                r = SEARCH_IMPL[platform](song_name + ' ' + singer, 5)
                song = _match_song(r, song_name, singer)
                if not song:
                    task_q.log(task, '[%s] 未找到精确匹配歌曲' % platform)
                    return


                entries = _collect_entries(platform, song, sm.handles_for(platform))
                if not entries:
                    task_q.log(task, '[%s] 未解析出可用FLAC' % platform)
                    return
                uploads = []
                for e in entries:
                    if e['spec'] in existing_specs:
                        continue
                    data = download_bytes(e['url'], 180)
                    if not data or data[:4] != b'fLaC':
                        continue
                    uploads.append((e['spec'], data))
                if not uploads:
                    task_q.log(task, '[%s] 无新增规格(均已存在)' % platform)
                if uploads:
                    with lock:
                        cur = dav_propfind(cfg, rel_enc, '1')
                        if not any(it.get('isDir') for it in cur):
                            dav_mkcol(cfg, rel_enc)
                        for spec, data in uploads:
                            # 同大小 = 完全相同, 跳过
                            if any((it.get('size') and it['size'] == len(data)) for it in cur if not it.get('isDir')):
                                continue
                            # 同名但不同大小(不同平台编码)则加序号
                            fname = '%s (%s).flac' % (name, spec)
                            n = 2
                            while any((it.get('displayname') or '') == fname for it in cur):
                                fname = '%s (%s)%d.flac' % (name, spec, n)
                                n += 1
                            st = dav_put(cfg, rel_enc + '/' + urllib.parse.quote(fname, safe=''), data, 'audio/flac')
                            task_q.log(task, '[%s] 补齐 %s %s' % (platform, fname, '成功' if st < 300 else 'HTTP %s' % st))
                            existing_specs.add(spec)
            except Exception as e:
                task_q.log(task, '[%s] %s' % (platform, str(e)[:80]))
            finally:
                bar['n'] += 1
                task_q.progress(task, total, planned)

        threads = []
        for p in ['wy', 'tx', 'kg', 'kw', 'mg']:
            t = _th.Thread(target=platform_work, args=(p,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        # 歌词: 依次尝试各平台, 上传后验证确实存在才算完成
        if not has_lrc:
            for lp in ['wy', 'tx', 'kg', 'kw', 'mg']:
                try:
                    r = SEARCH_IMPL[lp](song_name + ' ' + singer, 5)
                    song = _match_song(r, song_name, singer)
                    if not song:
                        continue
                    task_q.log(task, '[%s] 获取歌词...' % lp)
                    l = get_platform_lyric(lp, song)
                    if not l.get('lyric'):
                        task_q.log(task, '[%s] 无歌词' % lp)
                        continue
                    cur = dav_propfind(cfg, rel_enc, '1')
                    lname = name + '.lrc'
                    if not any((it.get('displayname') or '') == lname for it in cur):
                        if not any(it.get('isDir') for it in cur):
                            dav_mkcol(cfg, rel_enc)
                        dav_put(cfg, rel_enc + '/' + urllib.parse.quote(lname, safe=''), l['lyric'].encode('utf-8'), 'text/plain; charset=utf-8')
                    cur2 = dav_propfind(cfg, rel_enc, '1')
                    if any((it.get('displayname') or '') == lname for it in cur2):
                        task_q.log(task, '歌词已补并验证')
                        break
                    task_q.log(task, '[%s] 歌词上传未生效, 换下一接口' % lp)
                except Exception as e:
                    task_q.log(task, '[%s] 歌词失败: %s' % (lp, str(e)[:60]))
        # 封面: 依次尝试各平台, 上传后验证
        if not has_jpg:
            for cp in ['wy', 'tx', 'kg', 'kw', 'mg']:
                try:
                    r = SEARCH_IMPL[cp](song_name + ' ' + singer, 5)
                    song = _match_song(r, song_name, singer)
                    if not song or not song.get('picUrl'):
                        continue
                    task_q.log(task, '[%s] 获取封面...' % cp)
                    data = download_bytes(upgrade_pic(song['picUrl']), 30)
                    if not data or len(data) <= 100:
                        task_q.log(task, '[%s] 封面下载失败' % cp)
                        continue
                    cur = dav_propfind(cfg, rel_enc, '1')
                    cname = name + '.jpg'
                    if not any((it.get('displayname') or '') == cname for it in cur):
                        if not any(it.get('isDir') for it in cur):
                            dav_mkcol(cfg, rel_enc)
                        dav_put(cfg, rel_enc + '/' + urllib.parse.quote(cname, safe=''), data, 'image/jpeg')
                    cur2 = dav_propfind(cfg, rel_enc, '1')
                    if any((it.get('displayname') or '') == cname for it in cur2):
                        task_q.log(task, '封面已补并验证')
                        break
                    task_q.log(task, '[%s] 封面上传未生效, 换下一接口' % cp)
                except Exception as e:
                    task_q.log(task, '[%s] 封面失败: %s' % (cp, str(e)[:60]))

        task_q.progress(task, total, planned)
    task_q.log(task, '补全完成')


@router.post('/api/backfill')
def backfill(body: dict, request: Request):
    tq = request.app.state.tq
    cfg = _load_cfg(request)
    if not cfg.get('url'):
        raise HTTPException(400, '请先配置 WebDAV')
    tid = tq.add('曲库补全(缺失音质/歌词/封面)',
                 lambda task: _run_backfill(tq, task, cfg, request.app.state.sm))
    return {'id': tid}


@router.post('/api/resolve-all')
def resolve_all(body: dict, request: Request):
    b = body or {}
    platform = str(b.get('platform') or '')
    song = b.get('song') or {}
    qualities = b.get('qualities') if isinstance(b.get('qualities'), list) else []
    sm = request.app.state.sm
    source_id = str(b.get('sourceId') or '')
    if source_id and sm.get(source_id):
        handles = [sm.get(source_id)]
    else:
        handles = sm.handles_for(platform)
    if not qualities:
        # 兜底: 该平台所有音源声明的全部音质
        for h in handles:
            for q in (h.sources.get(platform) or {}).get('qualitys') or []:
                if q not in qualities:
                    qualities.append(q)
    if not qualities or not handles:
        return {'list': []}
    # 所有音源同时(并行)解析, 一起出力
    # 每家只测自己声明的音质标签; 128k/192k/320k(MP3档)直接拉黑
    mp3_black = {'128k', '192k', '320k'}
    collected = {}
    if handles:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(len(handles), 1)) as ex:
            futs = []
            for h in handles:
                declared = ((h.sources.get(platform) or {}).get('qualitys')) or []
                qs = [q for q in declared if q not in mp3_black]
                if not qs:
                    continue
                futs.append((ex.submit(h.resolve_multi, platform, song, qs), h))
            for fut, h in futs:
                try:
                    r = fut.result(timeout=22)  # 单源整体超时 22s, 挂的源不阻塞
                    for q, url in r.items():
                        if url:
                            collected.setdefault(q, []).append(url)
                except Exception:
                    continue
    priority = {'128k': 0, '192k': 1, '320k': 2, 'flac': 3, 'flac24bit': 4,
                'hires': 5, 'hiRes': 5, 'sky': 5, 'jyeffect': 5, 'jymaster': 6,
                'atmos': 6, 'atmos_plus': 6, 'dolby': 6, 'master': 6}
    # 下载文件头实测规格 + 文件大小
    entries = []
    tasks = []
    for q, urls in collected.items():
        for url in urls:
            tasks.append((q, url))
    def _head_worker(q, url):
        head, size = download_head(url, 16384, 15)
        if not head or head[:4] != b'fLaC':
            return None
        spec = measure_spec(head, q)
        fi = parse_flac_info(head)
        return {'quality': q, 'url': url, 'spec': spec, 'size': size,
                'sr': fi['sr'] if fi else 0, 'bits': fi['bits'] if fi else 0}
    if tasks:
        from concurrent.futures import ThreadPoolExecutor as _T2
        with _T2(max_workers=min(len(tasks), 30)) as ex:
            for res in ex.map(lambda t: _head_worker(t[0], t[1]), tasks):
                if res:
                    entries.append(res)
    # 按文件大小去重: 同大小 = 同一文件, 保留低音质标注; 不同大小的全部显示
    by_size = {}
    for e in entries:
        key = e['size'] if e['size'] else e['url']  # 大小未知时退回 URL
        p = priority.get(e['quality'], 5)
        if key not in by_size or p < priority.get(by_size[key]['quality'], 5):
            by_size[key] = e
    result = list(by_size.values())
    result.sort(key=lambda x: priority.get(x['quality'], 5))
    return {'list': result}


@router.post('/api/resolve')
def resolve(body: dict, request: Request):
    b = body or {}
    platform = str(b.get('platform') or '')
    song = b.get('song') or {}
    quality = str(b.get('quality') or 'flac')
    sm = request.app.state.sm
    sid = str(b.get('sourceId') or '')
    if sid and sm.get(sid):
        handles = [sm.get(sid)]
    else:
        # 自动: 按顺序尝试所有支持该平台的音源
        handles = sm.handles_for(platform)
    if not handles:
        raise HTTPException(404, '没有支持该平台的音源')
    last_err = None
    for h in handles:
        try:
            return h.resolve(platform, song, quality)
        except Exception as e:
            last_err = e
    raise HTTPException(502, str(last_err) if last_err else '音源解析失败')


# ---------- 音源管理 ----------
@router.get('/api/sources')
def sources(request: Request):
    return request.app.state.sm.list()


@router.post('/api/sources/import-url')
def import_url(body: dict, request: Request):
    url = str((body or {}).get('url') or '').strip()
    if not re.match(r'^https?://', url):
        raise HTTPException(400, 'url 必须为 http(s) 链接')
    try:
        h = request.app.state.sm.import_from_url(url)
        return {'id': h.id, 'name': h.name, 'sources': h.sources}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post('/api/sources/import-file')
def import_file(body: dict, request: Request):
    raw = str((body or {}).get('script') or '')
    if not raw.strip():
        raise HTTPException(400, '脚本内容为空')
    try:
        h = request.app.state.sm.import_script(raw)
        return {'id': h.id, 'name': h.name, 'sources': h.sources}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.delete('/api/sources/{sid}')
def remove_source(sid: str, request: Request):
    ok = request.app.state.sm.remove(sid)
    if not ok:
        raise HTTPException(404, '音源不存在')
    return {'ok': True}


# ---------- WebDAV ----------
@router.get('/api/webdav/list')
def webdav_list(request: Request, path: str = ''):
    cfg = _load_cfg(request)
    if not cfg.get('url'):
        raise HTTPException(400, '请先配置 WebDAV')
    try:
        # path 为 href 形式(/dav/xxx/), 转成相对 WebDAV 根的路径
        base_path = urllib.parse.urlparse(cfg['url']).path.rstrip('/')
        rel = path
        if base_path and rel.startswith(base_path):
            rel = rel[len(base_path):]
        rel = rel.lstrip('/').rstrip('/')
        items = dav_propfind(cfg, rel, '1')
        # 过滤自身(响应 href 是 URL 编码的, 需解码比较)
        self_href = urllib.parse.quote((base_path or '') + '/' + rel if rel else (base_path or '') + '/')
        return [it for it in items if urllib.parse.unquote(it['href']).rstrip('/') != urllib.parse.unquote(self_href).rstrip('/')]
    except Exception as e:
        raise HTTPException(502, str(e))


@router.delete('/api/webdav/delete')
def webdav_delete(body: dict, request: Request):
    cfg = _load_cfg(request)
    href = str((body or {}).get('href') or '')
    if not href:
        raise HTTPException(400, 'href 不能为空')
    # href 解实体后规范化编码(& → %26 等), 传给 davDelete
    try:
        base_path = urllib.parse.urlparse(cfg.get('url', '')).path.rstrip('/')
        if re.match(r'^https?:', href):
            href = urllib.parse.urlparse(href).path
        rel = _norm_rel(href, base_path)
    except Exception:
        rel = href
    try:
        status = dav_delete(cfg, rel)
        return {'ok': status < 300}
    except Exception as e:
        raise HTTPException(502, str(e))


# ---------- 任务 ----------

def _collect_entries(platform, song, handles):
    """所有音源并行解析 -> 文件头实测 -> 只留 FLAC -> 按大小去重"""
    mp3_black = {'128k', '192k', '320k'}
    collected = {}
    if handles:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(len(handles), 1)) as ex:
            futs = []
            for h in handles:
                declared = ((h.sources.get(platform) or {}).get('qualitys')) or []
                qs = [q for q in declared if q not in mp3_black]
                if not qs:
                    continue
                futs.append((ex.submit(h.resolve_multi, platform, song, qs), h))
            for fut, h in futs:
                try:
                    r = fut.result(timeout=30)
                    for q, url in r.items():
                        if url:
                            collected.setdefault(q, []).append(url)
                except Exception:
                    continue
    entries = []
    for q, urls in collected.items():
        for url in urls:
            head, size = download_head(url, 16384, 15)
            if not head or head[:4] != b'fLaC':
                continue
            spec = measure_spec(head, q)
            fi = parse_flac_info(head)
            entries.append({'quality': q, 'url': url, 'spec': spec, 'size': size,
                            'sr': fi['sr'] if fi else 0, 'bits': fi['bits'] if fi else 0})
    by_size = {}
    for e in entries:
        key = e['size'] if e['size'] else e['url']
        old = by_size.get(key)
        if not old or (e['sr'] or 0) * 1000 + (e['bits'] or 0) > (old['sr'] or 0) * 1000 + (old['bits'] or 0):
            by_size[key] = e
    return list(by_size.values())


def _run_upload(task_q, task, handles, cfg, platform, songs, qualities, with_lyric, with_cover):
    priority = {'128k': 0, '192k': 1, '320k': 2, 'flac': 3, 'flac24bit': 4,
                'hires': 5, 'hiRes': 5, 'sky': 5, 'jyeffect': 5, 'jymaster': 6,
                'atmos': 6, 'atmos_plus': 6, 'dolby': 6, 'master': 6}
    planned = sum(
        1
        + (1 if with_lyric and song.get('id') else 0)
        + (1 if with_cover and song.get('picUrl') else 0)
        for song in songs
    )
    total = 0
    for song in songs:
        name = (song.get('name') or '未知') + ' - ' + (song.get('singer') or '未知')
        dir_name = safe_name(name)
        dir_path = urllib.parse.quote(dir_name, safe='')
        task_q.log(task, '== %s ==' % name)
        # 1. 所有音源并行解析 + 实测 + 按大小去重
        try:
            entries = _collect_entries(platform, song, handles)
        except Exception as e:
            task_q.log(task, '解析失败: ' + str(e))
            continue
        if not entries:
            task_q.log(task, '该曲无可用 FLAC 音质')
            continue
        # 3. 全部 FLAC 上传(不同规格文件名不同, 天然并存; 同大小/同名跳过)
        for e in entries:
            q, url, spec = e['quality'], e['url'], e['spec']
            total += 1
            task_q.log(task, '[%s] %s 下载中...' % (q, spec))
            try:
                data = download_bytes(url, 180)
                if not data or data[:4] != b'fLaC':
                    task_q.log(task, '[%s] 下载失败或非FLAC' % q)
                    continue
                existing = dav_propfind(cfg, dir_path, '1')
                # 同大小 = 完全相同, 跳过
                if any((it.get('size') and it['size'] == len(data)) for it in existing if not it.get('isDir')):
                    task_q.log(task, '已存在相同文件(同大小), 跳过')
                    continue
                # 同名但不同大小(不同平台编码)则加序号
                fname = '%s (%s).flac' % (dir_name, spec)
                n = 2
                while any((it.get('displayname') or '') == fname for it in existing):
                    fname = '%s (%s)%d.flac' % (dir_name, spec, n)
                    n += 1
                if not any(it.get('isDir') for it in existing):
                    dav_mkcol(cfg, dir_path)
                st = dav_put(cfg, dir_path + '/' + urllib.parse.quote(fname, safe=''), data, 'audio/flac')
                if st < 300:
                    task_q.log(task, '上传成功 ' + fname)
                else:
                    task_q.log(task, '上传失败 HTTP %s' % st)
            except Exception as e:
                task_q.log(task, '[%s] %s' % (q, str(e)))
            task_q.progress(task, total, planned)
        # 4. 歌词 + 封面(全平台歌词)
        if with_lyric and song.get('id'):
            total += 1
            try:
                l = get_platform_lyric(platform, song)
                if l.get('lyric'):
                    lname = dir_name + '.lrc'
                    existing = dav_propfind(cfg, dir_path, '1')
                    if not any(it['displayname'] == lname for it in existing):
                        dav_mkcol(cfg, dir_path)
                        dav_put(cfg, dir_path + '/' + urllib.parse.quote(lname, safe=''),
                                l['lyric'].encode('utf-8'), 'text/plain; charset=utf-8')
                        task_q.log(task, '歌词已上传')
                    else:
                        task_q.log(task, '歌词已存在, 跳过')
                else:
                    task_q.log(task, '无歌词')
            except Exception as e:
                task_q.log(task, '歌词获取失败: ' + str(e))
            task_q.progress(task, total, planned)
        if with_cover and song.get('picUrl'):
            total += 1
            try:
                data = download_bytes(upgrade_pic(str(song['picUrl'])), 30)
                if data and len(data) > 100:
                    cname = dir_name + '.jpg'
                    existing = dav_propfind(cfg, dir_path, '1')
                    if not any(it['displayname'] == cname for it in existing):
                        dav_mkcol(cfg, dir_path)
                        dav_put(cfg, dir_path + '/' + urllib.parse.quote(cname, safe=''), data, 'image/jpeg')
                        task_q.log(task, '封面已上传')
                    else:
                        task_q.log(task, '封面已存在, 跳过')
                else:
                    task_q.log(task, '封面下载失败')
            except Exception as e:
                task_q.log(task, '封面失败: ' + str(e))
            task_q.progress(task, total, planned)


@router.get('/api/tasks')
def tasks(request: Request):
    return {'list': request.app.state.tq.list()}


@router.post('/api/tasks')
def create_task(body: dict, request: Request):
    b = body or {}
    source_id = str(b.get('sourceId') or '')
    platform = str(b.get('platform') or '')
    songs = b.get('songs') if isinstance(b.get('songs'), list) else []
    qualities = b.get('qualities') if isinstance(b.get('qualities'), list) else ['flac']
    with_lyric = b.get('withLyric') is not False
    with_cover = b.get('withCover') is not False
    tq = request.app.state.tq
    sm = request.app.state.sm
    if source_id and sm.get(source_id):
        handles = [sm.get(source_id)]
    else:
        handles = sm.handles_for(platform)
        if not handles:
            raise HTTPException(404, '没有支持该平台的音源')
    cfg = _load_cfg(request)
    if not cfg.get('url'):
        raise HTTPException(400, '请先配置 WebDAV')
    if not songs:
        raise HTTPException(400, 'songs 不能为空')
    tid = tq.add('批量上传 %d 首' % len(songs),
                 lambda task: _run_upload(tq, task, handles, cfg, platform, songs, qualities, with_lyric, with_cover))
    return {'id': tid}


# ---------- SSE ----------
def _sse(obj):
    return 'data: ' + json.dumps(obj, ensure_ascii=False) + '\n\n'


@router.get('/api/tasks/events')
async def task_events(request: Request):
    tq = request.app.state.tq
    q = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def push(data):
        loop.call_soon_threadsafe(q.put_nowait, data)

    tq.subscribe(push)

    async def gen():
        try:
            yield _sse({'type': 'hello'})
            for t in tq.list():
                yield _sse({'type': 'task', 'task': t})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=20)
                    yield _sse({'type': 'task', 'task': data})
                except asyncio.TimeoutError:
                    yield _sse({'type': 'ping'})
        finally:
            tq.unsubscribe(push)

    return StreamingResponse(gen(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive'})


# ---------- 流代理 ----------
def _sniff_type(data):
    """嗅探文件头识别真实格式(上游 CDN 常把 FLAC 标成 audio/mpeg)"""
    if data[:4] == b'fLaC':
        return 'audio/flac', '.flac'
    if data[:3] == b'ID3' or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return 'audio/mpeg', '.mp3'
    if data[:4] == b'OggS':
        return 'audio/ogg', '.ogg'
    if data[:4] == b'\x00\x00\x00\x18' and data[4:8] == b'ftyp':
        return 'audio/mp4', '.m4a'
    if data[4:8] == b'ftyp':
        return 'audio/mp4', '.m4a'
    return None, None


def _proxy(request: Request, url: str, attach: bool, filename):
    if not re.match(r'^https?://', url):
        raise HTTPException(400, 'url 参数无效')
    headers = {'User-Agent': UA, 'Referer': 'https://music.163.com/'}
    range_header = request.headers.get('range')
    if range_header:
        headers['Range'] = range_header
    req = urllib.request.Request(url, headers=headers)
    try:
        upstream = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        upstream = e
    status = getattr(upstream, 'status', getattr(upstream, 'code', 200))
    resp_headers = {}
    if upstream.headers.get('Content-Type'):
        resp_headers['Content-Type'] = upstream.headers['Content-Type']
    if upstream.headers.get('Content-Length'):
        resp_headers['Content-Length'] = upstream.headers['Content-Length']
    if range_header and upstream.headers.get('Content-Range'):
        resp_headers['Content-Range'] = upstream.headers['Content-Range']
    if upstream.headers.get('Accept-Ranges'):
        resp_headers['Accept-Ranges'] = upstream.headers['Accept-Ranges']

    first = b''
    if status < 400:
        try:
            first = upstream.read(16384)
        except Exception:
            first = b''
        ctype, ext = _sniff_type(first)
        if ctype:
            resp_headers['Content-Type'] = ctype
            if attach and filename:
                base = filename.rsplit('.', 1)[0] if '.' in filename else filename
                filename = base + ext
    if attach and filename:
        resp_headers['Content-Disposition'] = "attachment; filename*=UTF-8''" + urllib.parse.quote(filename)

    def gen():
        try:
            if first:
                yield first
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    return StreamingResponse(gen(), status_code=status, headers=resp_headers)


@router.get('/api/stream')
def stream(request: Request, url: str = ''):
    return _proxy(request, url, attach=False, filename=None)


@router.head('/api/stream')
def stream_head(request: Request, url: str = ''):
    resp = _proxy(request, url, attach=False, filename=None)
    resp.body = b''
    return resp


@router.get('/api/download')
def download(request: Request, url: str = '', name: str = 'audio.flac'):
    return _proxy(request, url, attach=True, filename=name)
