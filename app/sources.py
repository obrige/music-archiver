# -*- coding: utf-8 -*-
"""音源管理: 洛雪音源脚本(JS)通过 node 进程桥在沙箱中运行
设计: 每次解析请求都启动全新桥进程, 用完即杀, 彻底避免进程卡死/管道堆积拖垮服务

扩展(批次/压缩/订阅/评分/进程池):
- SourceManager.import_from_files: 多个 .js 文件批量导入(前端 multipart)
- SourceManager.import_from_zip_path / _bytes / _url_zip: ZIP 批量导入(支持流式)
- SubscriptionManager: 订阅分组(URL 列表 + 自动刷新间隔, per-group 锁防并发竞争)
- AutoUpdater: 后台线程并行刷新到期订阅组
- SourceScore: per-source 滚动统计(成功率/平均延迟/按音质成功率), 复合分用于智能选源
- SourceManager.handles_for_score(): 按分数降序返回, 供 resolve-all 优先跑高分源
- LxScriptHandle: 进程池(per-source, max=1, TTL=30s) 复用 node 桥, 减少 spawn 开销
"""
import io
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time as _time
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger('musicarchiver.sources')

BRIDGE_TIMEOUT = 35
META_TIMEOUT = 15
SUB_INTERVAL_MIN = 5
SUB_INTERVAL_MAX = 10080
SUB_UPDATE_TIMEOUT = 300
AUTOUPDATER_PARALLEL = 3
SCORE_SAVE_INTERVAL = 30
BATCH_IMPORT_PARALLEL = 4
STRICT_ACTION_CHECK = True

_VERSION_PATTERNS = [
    (re.compile(r'[Vv]?(\d{2})(\d{2})(\d{2})'), lambda m: f'20{m.group(1)}-{m.group(2)}-{m.group(3)}'),
    (re.compile(r'(\d{4})(\d{2})(\d{2})'), lambda m: f'{m.group(1)}-{m.group(2)}-{m.group(3)}' if 2020 <= int(m.group(1)) <= 2099 else None),
    (re.compile(r'[Vv]?(\d+)\.(\d+)\.(\d+)'), lambda m: f'v{m.group(1)}.{m.group(2)}.{m.group(3)}'),
]


def _extract_version(text):
    if not text:
        return None
    for rx, fmt in _VERSION_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        try:
            v = fmt(m)
            if v:
                return v
        except Exception:
            continue
    return None


def _extract_url_from_data(raw):
    """从脚本返回的 data 字段提取 URL. 兼容:
    - str: 直接返回(老 LX Music 风格, 如 huibq `return body.url`)
    - dict: 优先 .url, 兼容 .src / .data
    - list: 递归首个
    - 其他: None"""
    if raw is None:
        return None
    if isinstance(raw, str):
        if raw.startswith(('http://', 'https://')):
            return raw
        return None
    if isinstance(raw, dict):
        for key in ('url', 'src', 'data', 'location'):
            v = raw.get(key)
            if isinstance(v, str) and v.startswith(('http://', 'https://')):
                return v
        return None
    if isinstance(raw, (list, tuple)) and raw:
        for item in raw:
            u = _extract_url_from_data(item)
            if u:
                return u
    return None


class SourceScore:
    """单源滚动统计: Bayesian 平滑成功率 + 滑动窗口平均延迟 + 按音质分桶成功率."""
    RECENT_WINDOW = 20
    BAYES_PRIOR_TRIALS = 5
    BAYES_PRIOR_SUCCESS = 2.5
    LATENCY_FAST_MS = 500
    LATENCY_SLOW_MS = 10000
    WEIGHT_QUALITY = 0.4
    WEIGHT_SPEED = 0.3
    WEIGHT_RELIABILITY = 0.3

    def __init__(self):
        self.successes = 0
        self.failures = 0
        self.rate_limited = 0
        self.last_success_at = None
        self.last_failure_at = None
        self.last_rate_limit_at = None
        self.quality_attempts = {}
        self.quality_successes = {}
        self.recent_latencies = []

    def record(self, success, latency_ms=None, quality=None, rate_limited=False):
        if rate_limited:
            self.rate_limited += 1
            self.last_rate_limit_at = _time.time()
            return
        if quality:
            self.quality_attempts[quality] = self.quality_attempts.get(quality, 0) + 1
        if success:
            self.successes += 1
            self.last_success_at = _time.time()
            if latency_ms is not None:
                self.recent_latencies.append(int(latency_ms))
                if len(self.recent_latencies) > self.RECENT_WINDOW:
                    self.recent_latencies.pop(0)
            if quality:
                self.quality_successes[quality] = self.quality_successes.get(quality, 0) + 1
        else:
            self.failures += 1
            self.last_failure_at = _time.time()

    @property
    def total_trials(self):
        return self.successes + self.failures

    @property
    def success_rate(self):
        if self.total_trials == 0:
            return 0.5
        return (self.successes + self.BAYES_PRIOR_SUCCESS) / (self.total_trials + self.BAYES_PRIOR_TRIALS)

    @property
    def avg_latency_ms(self):
        if self.recent_latencies:
            return sum(self.recent_latencies) / len(self.recent_latencies)
        return (self.LATENCY_FAST_MS + self.LATENCY_SLOW_MS) / 2

    def quality_score(self, quality):
        a = self.quality_attempts.get(quality, 0)
        if a == 0:
            return 0.5
        s = self.quality_successes.get(quality, 0)
        return (s + 1) / (a + 2)

    def score(self, requested_quality=None):
        if requested_quality:
            qs = self.quality_score(requested_quality)
        else:
            ta = sum(self.quality_attempts.values())
            ts = sum(self.quality_successes.values())
            qs = (ts + 1) / (ta + 2) if ta else 0.5
        lat = self.avg_latency_ms
        spd = max(0.0, min(1.0, (self.LATENCY_SLOW_MS - lat) / (self.LATENCY_SLOW_MS - self.LATENCY_FAST_MS)))
        rel = self.success_rate
        return qs * self.WEIGHT_QUALITY + spd * self.WEIGHT_SPEED + rel * self.WEIGHT_RELIABILITY

    def to_dict(self):
        return {
            'successes': self.successes,
            'failures': self.failures,
            'rateLimited': self.rate_limited,
            'successRate': round(self.success_rate, 3),
            'avgLatencyMs': int(self.avg_latency_ms),
            'lastSuccessAt': self.last_success_at,
            'lastFailureAt': self.last_failure_at,
            'lastRateLimitAt': self.last_rate_limit_at,
            'qualitySuccess': dict(self.quality_successes),
            'qualityAttempts': dict(self.quality_attempts),
        }

    @classmethod
    def from_dict(cls, d):
        s = cls()
        s.successes = int(d.get('successes') or 0)
        s.failures = int(d.get('failures') or 0)
        s.rate_limited = int(d.get('rateLimited') or 0)
        s.last_success_at = d.get('lastSuccessAt')
        s.last_failure_at = d.get('lastFailureAt')
        s.last_rate_limit_at = d.get('lastRateLimitAt')
        s.quality_successes = dict(d.get('qualitySuccess') or {})
        s.quality_attempts = dict(d.get('qualityAttempts') or {})
        return s


class LxScriptHandle:
    """Per-source node bridge handle. 每次解析用 _acquire/_release 池化复用进程,
    大幅减少 cold-start 100ms spawn 开销."""
    POOL_MAX = 1
    POOL_TTL = 30
    POOL_MAX_AGE = 600

    def __init__(self, script_path, bridge_path, node='node', on_dirty=None):
        self.script_path = str(script_path)
        self.bridge_path = str(bridge_path)
        self.node = node
        self.id = ''
        self.name = ''
        self.sources = {}
        self.last_success = None
        self.score = SourceScore()
        self._on_dirty = on_dirty
        self._raw_cache = None
        self._rate_lock = threading.Lock()
        self._rate_last = [0.0]
        self._rate_interval = 0.5
        self.init_status = 'pending'
        self.init_error = None
        self._pool = []
        self._pool_lock = threading.Lock()
        self._load_meta()

    def _get_raw(self):
        if self._raw_cache is None:
            try:
                with open(self.script_path, 'r', encoding='utf-8') as f:
                    self._raw_cache = f.read()
            except Exception:
                self._raw_cache = ''
        return self._raw_cache

    def _spawn(self):
        proc = subprocess.Popen(
            [self.node, self.bridge_path, self.script_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', bufsize=1,
        )
        q = queue.Queue()
        t = threading.Thread(target=self._read_loop, args=(proc, q), daemon=True)
        t.start()
        return proc, q

    @staticmethod
    def _read_loop(proc, q):
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    log.debug('bridge: 忽略非对象消息: %s', str(line)[:200])
                    continue
                if isinstance(msg, dict):
                    q.put(msg)
                else:
                    log.debug('bridge: 忽略非对象消息: %s', str(line)[:200])
        except Exception:
            pass

    @staticmethod
    def _kill(proc):
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    @staticmethod
    def _read_stderr(proc):
        err = ''
        try:
            if proc.stderr:
                err = (proc.stderr.read() or '')[:400]
        except Exception:
            err = ''
        return err

    def _acquire(self):
        with self._pool_lock:
            now = _time.time()
            fresh = []
            for entry in self._pool:
                proc, q, ts, ct = entry[0], entry[1], entry[2], entry[3]
                if proc.poll() is not None:
                    continue
                if (now - ts) > self.POOL_TTL or (now - ct) > self.POOL_MAX_AGE:
                    self._kill(proc)
                    continue
                fresh.append(entry)
            self._pool = fresh
            while self._pool:
                entry = self._pool.pop(0)
                proc, q = entry[0], entry[1]
                if proc.poll() is None:
                    self._drain_queue(q)
                    return proc, q
            return self._spawn()

    def _release(self, proc, q):
        with self._pool_lock:
            try:
                if proc.poll() is not None:
                    return
                while len(self._pool) >= self.POOL_MAX:
                    old_proc = self._pool.pop(0)[0]
                    self._kill(old_proc)
                now = _time.time()
                self._pool.append((proc, q, now, now))
            except Exception:
                try:
                    self._kill(proc)
                except Exception:
                    pass

    def _drain_queue(self, q):
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass

    def _load_meta(self):
        proc, q = self._acquire()
        try:
            while True:
                msg = q.get(timeout=META_TIMEOUT)
                mtype = msg.get('type')
                if mtype == 'meta':
                    self.name = msg.get('name') or '未命名音源'
                    raw_sources = msg.get('sources') or {}
                    self.sources = {}
                    for plat, info in raw_sources.items():
                        info = dict(info or {})
                        actions = info.get('actions')
                        if not actions:
                            actions = ['musicUrl']
                        info['actions'] = list(actions)
                        self.sources[plat] = info
                    self.init_status = 'ok'
                    return
                if mtype == 'console':
                    log.debug('script console: %s', msg.get('msg'))
                    continue
                if msg.get('ok') is False:
                    raise RuntimeError(msg.get('error') or '音源脚本初始化失败')
        except queue.Empty:
            err = self._read_stderr(proc)
            self.init_status = 'fail'
            self.init_error = (err.strip() if err.strip() else '未在超时内收到 meta')
            self._kill(proc)
            raise RuntimeError('音源脚本初始化失败: ' + self.init_error)
        except Exception as e:
            self.init_status = 'fail'
            self.init_error = str(e)[:200]
            raise
        finally:
            try:
                self._release(proc, q)
            except Exception:
                pass

    def _record(self, success, latency_ms, quality, rate_limited=False):
        self.score.record(success, latency_ms, quality, rate_limited=rate_limited)
        if self._on_dirty:
            try:
                self._on_dirty()
            except Exception:
                log.exception('on_dirty callback failed')

    @staticmethod
    def _detect_rate_limit(err_msg):
        if not err_msg:
            return False
        e = err_msg.lower()
        return ('code 5' in e) or ('too many requests' in e) or ('rate limit' in e) or ('限流' in err_msg) or ('频繁' in err_msg)

    def _actions_for(self, platform):
        info = self.sources.get(platform) or {}
        return info.get('actions') or []

    def _supports_action(self, platform, action):
        actions = self._actions_for(platform)
        if not actions:
            return True
        return action in actions

    def resolve(self, source, song, quality):
        if not self._supports_action(source, 'musicUrl'):
            raise RuntimeError(f'{self.name or "音源"} 不支持 {source} 的 musicUrl (actions={self._actions_for(source)})')

        t0 = _time.time()
        proc, q = None, None
        success = False
        rate_limited = False
        try:
            proc, q = self._acquire()
            req = {'source': source, 'action': 'musicUrl',
                   'info': {'musicInfo': dict(song), 'type': quality}}
            proc.stdin.write(json.dumps(req, ensure_ascii=False) + '\n')
            proc.stdin.flush()
            deadline = _time.time() + BRIDGE_TIMEOUT
            while True:
                remain = deadline - _time.time()
                if remain <= 0:
                    raise RuntimeError('音源请求超时')
                msg = q.get(timeout=remain)
                mtype = msg.get('type')
                if mtype in ('bridge', 'meta', 'console'):
                    if mtype == 'console':
                        log.debug('script console: %s', msg.get('msg'))
                    continue
                if msg.get('ok') is False:
                    err = msg.get('error') or '音源解析失败'
                    rate_limited = self._detect_rate_limit(err)
                    raise RuntimeError(err)
                if msg.get('ok'):
                    success = True
                    self.last_success = _time.time()
                    return msg.get('data')
                raise RuntimeError('音源返回格式无效')
        except queue.Empty:
            if proc is not None:
                self._kill(proc)
                proc = None
            raise RuntimeError('音源请求超时')
        finally:
            ms = int((_time.time() - t0) * 1000)
            self._record(success, ms if success else None, quality, rate_limited=rate_limited)
            if proc is not None:
                try:
                    if proc.poll() is None:
                        self._release(proc, q)
                    else:
                        self._kill(proc)
                except Exception:
                    pass

    def _throttle(self):
        with self._rate_lock:
            now = _time.time()
            wait = self._rate_interval - (now - self._rate_last[0])
            if wait > 0:
                _time.sleep(wait)
            self._rate_last[0] = _time.time()

    def resolve_multi(self, platform, song, qualities, overall_timeout=30):
        if not self._supports_action(platform, 'musicUrl'):
            raise RuntimeError(f'{self.name or "音源"} 不支持 {platform} 的 musicUrl')

        self._throttle()
        proc, q = None, None
        deadline_all = _time.time() + overall_timeout
        results = {}
        try:
            proc, q = self._acquire()
            for quality in qualities:
                if _time.time() > deadline_all:
                    break
                req = {'platform': platform, 'action': 'musicUrl',
                       'info': {'musicInfo': dict(song), 'type': quality}}
                proc.stdin.write(json.dumps(req, ensure_ascii=False) + '\n')
                proc.stdin.flush()
                deadline = min(_time.time() + 18, deadline_all)
                while True:
                    remain = deadline - _time.time()
                    if remain <= 0:
                        break
                    try:
                        msg = q.get(timeout=remain)
                    except queue.Empty:
                        break
                    mtype = msg.get('type')
                    if mtype in ('bridge', 'meta', 'console'):
                        if mtype == 'console':
                            log.debug('script console: %s', msg.get('msg'))
                        continue
                    if msg.get('ok') is False:
                        log.debug('%s: quality %s 失败: %s', self.name, quality, msg.get('error'))
                        break
                    if msg.get('ok'):
                        url = _extract_url_from_data(msg.get('data'))
                        if url:
                            results[quality] = url
                        break
            if results:
                self.last_success = _time.time()
            return results
        finally:
            t_end = _time.time()
            ms = int(overall_timeout * 1000) if not results else int((overall_timeout - max(0, deadline_all - t_end)) * 1000)
            self._record(bool(results), ms, None)
            if proc is not None:
                try:
                    if proc.poll() is None:
                        self._release(proc, q)
                    else:
                        self._kill(proc)
                except Exception:
                    pass

    def search(self, platform, keyword, page=1, limit=30):
        actions = self._actions_for(platform)
        if actions and 'musicSearch' not in actions and 'search' not in actions:
            raise RuntimeError(f'{self.name or "音源"} 不支持 {platform} 的搜索 (actions={actions})')

        actions = self.sources.get(platform) or {}
        actions = actions.get('actions') or []
        action = 'musicSearch' if 'musicSearch' in actions else 'search'
        t0 = _time.time()
        proc, q = None, None
        success = False
        rate_limited = False
        try:
            proc, q = self._acquire()
            req = {'source': platform, 'action': action, 'info': {'keyword': keyword, 'page': page, 'limit': limit}}
            proc.stdin.write(json.dumps(req, ensure_ascii=False) + '\n')
            proc.stdin.flush()
            deadline = _time.time() + 30
            while True:
                remain = deadline - _time.time()
                if remain <= 0:
                    raise RuntimeError('搜索超时')
                msg = q.get(timeout=remain)
                mtype = msg.get('type')
                if mtype in ('bridge', 'meta', 'console'):
                    continue
                if msg.get('ok') is False:
                    err = msg.get('error') or '搜索失败'
                    rate_limited = self._detect_rate_limit(err)
                    raise RuntimeError(err)
                if msg.get('ok'):
                    success = True
                    data = msg.get('data') or {}
                    return data.get('list') or []
        except queue.Empty:
            if proc is not None:
                self._kill(proc)
                proc = None
            raise RuntimeError('搜索超时')
        finally:
            ms = int((_time.time() - t0) * 1000)
            self._record(success, ms if success else None, None, rate_limited=rate_limited)
            if proc is not None:
                try:
                    if proc.poll() is None:
                        self._release(proc, q)
                    else:
                        self._kill(proc)
                except Exception:
                    pass

    def close(self):
        with self._pool_lock:
            for proc, q, _, _ in self._pool:
                self._kill(proc)
            self._pool = []


class SourceManager:
    def __init__(self, data_dir, bridge_path, node='node'):
        self.data_dir = Path(data_dir)
        self.store_file = self.data_dir / 'sources.json'
        self.scripts_dir = self.data_dir / 'scripts'
        self.bridge_path = bridge_path
        self.node = node
        self.handles = {}
        self._save_lock = threading.Lock()
        self._dirty = False
        self._save_stop = threading.Event()
        self._save_thread = None

    def init(self):
        if self.store_file.exists():
            try:
                entries = json.loads(self.store_file.read_text('utf-8'))
                recovered = 0
                for e in entries:
                    try:
                        raw = e.get('raw') or ''
                        script_file = self._write_script(e['id'], raw)
                        h = LxScriptHandle(script_file, self.bridge_path, self.node, on_dirty=self._mark_dirty)
                        h.id = e['id']
                        h.last_success = e.get('lastSuccess')
                        h.init_status = e.get('initStatus', 'ok')
                        h.init_error = e.get('initError')
                        score_data = e.get('score')
                        if score_data:
                            h.score = SourceScore.from_dict(score_data)
                        self.handles[e['id']] = h
                        recovered += 1
                    except Exception as ex:
                        log.warning('恢复音源 %s 失败: %s', e.get('name'), ex)
                log.info('sources: 已恢复 %d 个音源', recovered)
            except Exception as e:
                log.warning('sources: 读取 sources.json 失败: %s', e)
        self._start_periodic_save()

    def _start_periodic_save(self):
        if self._save_thread and self._save_thread.is_alive():
            return
        self._save_thread = threading.Thread(target=self._save_loop, daemon=True, name='sources-save')
        self._save_thread.start()
        log.debug('sources-save: 后台落盘线程启动, 间隔 %ds', SCORE_SAVE_INTERVAL)

    def _mark_dirty(self):
        self._dirty = True

    def _save_loop(self):
        while not self._save_stop.is_set():
            for _ in range(SCORE_SAVE_INTERVAL):
                if self._save_stop.is_set():
                    return
                _time.sleep(1)
            if self._dirty:
                self._dirty = False
                try:
                    self._save()
                    log.debug('sources-save: 评分落盘完成')
                except Exception:
                    log.exception('sources-save: 落盘失败')

    def stop_periodic_save(self):
        self._save_stop.set()
        if self._save_thread:
            self._save_thread.join(timeout=2)

    def _write_script(self, sid, raw):
        self.scripts_dir.mkdir(parents=True, exist_ok=True)
        f = self.scripts_dir / (sid + '.js')
        f.write_text(raw, 'utf-8')
        return f

    def _save(self):
        with self._save_lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            entries = []
            for sid, h in self.handles.items():
                raw = h._get_raw() if hasattr(h, '_get_raw') else ''
                entries.append({
                    'id': sid,
                    'name': h.name,
                    'raw': raw,
                    'lastSuccess': h.last_success,
                    'score': h.score.to_dict(),
                    'initStatus': getattr(h, 'init_status', 'ok'),
                    'initError': getattr(h, 'init_error', None),
                })
            content = json.dumps(entries, ensure_ascii=False, indent=2)
            tmp = self.store_file.with_suffix('.json.tmp')
            try:
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.store_file)
            except Exception:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass
                raise

    def _add(self, raw):
        sid = 'lx_' + uuid.uuid4().hex[:10]
        script_file = self._write_script(sid, raw)
        h = LxScriptHandle(script_file, self.bridge_path, self.node, on_dirty=self._mark_dirty)
        h.id = sid
        self.handles[sid] = h
        return h

    def _add_batch(self, raws_with_names):
        if not raws_with_names:
            return []

        def _do_one(item):
            fn, raw = item
            try:
                h = self._add(raw)
                return (fn, h, None)
            except Exception as e:
                return (fn, None, str(e)[:120])

        workers = min(len(raws_with_names), BATCH_IMPORT_PARALLEL)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(_do_one, raws_with_names))

    def import_script(self, raw):
        h = self._add(raw)
        self._save()
        return h

    def import_from_url(self, url):
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                raise RuntimeError('下载失败 HTTP %s %s' % (r.status, url))
            text = r.read().decode('utf-8', 'replace')
            final_url = r.geturl()
        return self.import_script(text), final_url

    # ---- 批量 / 压缩包 ----
    def import_from_files(self, files):
        items = []
        for f in files:
            try:
                if isinstance(f, tuple):
                    items.append((f[0] or '?', f[1]))
                else:
                    raw_bytes = f.read()
                    items.append((getattr(f, 'filename', None) or '?', raw_bytes))
            except Exception as e:
                items.append((getattr(f, 'filename', '?'), None))

        parsed = []
        for fn, raw_bytes in items:
            if raw_bytes is None:
                continue
            try:
                if isinstance(raw_bytes, (bytes, bytearray)):
                    raw_text = raw_bytes.decode('utf-8', 'replace')
                else:
                    raw_text = str(raw_bytes)
            except Exception:
                continue
            parsed.append((fn, raw_text))

        results = self._add_batch(parsed)
        added = []
        failed = []
        for fn, h, err in results:
            if h:
                added.append({'id': h.id, 'name': h.name, 'file': fn, 'initStatus': h.init_status,
                              'initError': h.init_error if h.init_status != 'ok' else None})
            else:
                failed.append({'file': fn, 'error': err or '未知错误'})
        if added:
            self._save()
        return {'added': added, 'failed': failed}

    def _zip_extract_js(self, zip_path):
        out = []
        try:
            z = zipfile.ZipFile(zip_path)
        except Exception as e:
            raise RuntimeError('不是有效的 ZIP 文件: ' + str(e))
        try:
            for name in z.namelist():
                if name.endswith('/'):
                    continue
                base = name.split('/')[-1]
                if base.startswith('.') or not base.lower().endswith('.js'):
                    continue
                try:
                    raw = z.read(name).decode('utf-8', 'replace')
                    out.append((name, raw))
                except Exception as e:
                    out.append((name, 'PARSE_ERR:' + str(e)))
        finally:
            try:
                z.close()
            except Exception:
                pass
        return out

    def import_from_zip_path(self, zip_path):
        entries = self._zip_extract_js(zip_path)
        results = self._add_batch([(fn, raw) for fn, raw in entries if not raw.startswith('PARSE_ERR:')])
        added = []
        failed = []
        for (fn, raw), (_fn2, h, err) in zip(entries, results):
            if raw.startswith('PARSE_ERR:'):
                failed.append({'file': fn, 'error': raw[10:]})
            elif h:
                added.append({'id': h.id, 'name': h.name, 'file': fn, 'initStatus': h.init_status,
                              'initError': h.init_error if h.init_status != 'ok' else None})
            else:
                failed.append({'file': fn, 'error': err or '未知错误'})
        if added:
            self._save()
        return {'added': added, 'failed': failed}

    def import_from_zip_bytes(self, raw_bytes):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
            tmp.write(raw_bytes)
            tmp_path = tmp.name
        try:
            return self.import_from_zip_path(tmp_path)
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    def import_from_url_zip(self, url):
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.status != 200:
                raise RuntimeError('下载失败 HTTP %s %s' % (r.status, url))
            raw = r.read()
            final_url = r.geturl()
        if not raw:
            raise RuntimeError('下载内容为空')
        return self.import_from_zip_bytes(raw), final_url

    def remove(self, sid):
        h = self.handles.pop(sid, None)
        if h is None:
            return False
        h.close()
        h._raw_cache = None
        self._save()
        return True

    def get(self, sid):
        return self.handles.get(sid)

    def handles_for(self, platform):
        return [h for h in self.handles.values() if platform in h.sources]

    def handles_for_score(self, platform, quality=None):
        handles = self.handles_for(platform)
        return sorted(handles, key=lambda h: -h.score.score(quality))

    def handles_topk(self, platform, quality=None, k=3):
        if k < 1:
            k = 1
        return self.handles_for_score(platform, quality)[:k]

    def list(self):
        return [{
            'id': sid, 'name': h.name, 'kind': 'lxScript', 'sources': h.sources,
            'lastSuccess': h.last_success, 'score': h.score.to_dict(),
            'initStatus': getattr(h, 'init_status', 'ok'),
            'initError': getattr(h, 'init_error', None),
        } for sid, h in self.handles.items()]

    def scores_map(self):
        return {
            sid: {'name': h.name, 'score': h.score.to_dict(), 'lastSuccess': h.last_success,
                  'initStatus': getattr(h, 'init_status', 'ok')}
            for sid, h in self.handles.items()
        }

    def close_all(self):
        self.stop_periodic_save()
        for h in self.handles.values():
            try:
                h.close()
            except Exception:
                pass


class SubscriptionManager:
    """订阅分组: 一组 URL(.js / .zip 混合), 定时整体刷新. per-group 锁防并发竞争."""
    def __init__(self, data_dir, source_manager):
        self.data_dir = Path(data_dir)
        self.store_file = self.data_dir / 'subscriptions.json'
        self.sm = source_manager
        self.groups = []
        self.last_versions = {}
        self._locks = {}
        self._locks_guard = threading.Lock()

    def init(self):
        if not self.store_file.exists():
            return
        try:
            self.groups = json.loads(self.store_file.read_text('utf-8'))
            if not isinstance(self.groups, list):
                self.groups = []
            for g in self.groups:
                gid = g.get('id')
                lv = g.get('lastVersions')
                if isinstance(lv, dict) and gid:
                    self.last_versions[gid] = lv
            log.info('subs: 已恢复 %d 个订阅组', len(self.groups))
        except Exception as e:
            log.warning('subs: 读取 subscriptions.json 失败: %s', e)
            self.groups = []

    def _save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for g in self.groups:
            gid = g.get('id')
            if gid and gid in self.last_versions:
                g['lastVersions'] = self.last_versions[gid]
        tmp = self.store_file.with_suffix('.json.tmp')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(json.dumps(self.groups, ensure_ascii=False, indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.store_file)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            raise

    def _lock_for(self, gid):
        with self._locks_guard:
            lk = self._locks.get(gid)
            if lk is None:
                lk = threading.Lock()
                self._locks[gid] = lk
            return lk

    def list(self):
        return self.groups

    def add(self, name, urls, interval_min=60):
        try:
            iv = int(interval_min)
        except Exception:
            iv = 60
        iv = max(SUB_INTERVAL_MIN, min(SUB_INTERVAL_MAX, iv))
        gid = 'sub_' + uuid.uuid4().hex[:12]
        self.groups.append({
            'id': gid,
            'name': name,
            'urls': urls,
            'intervalMin': iv,
            'lastUpdate': None,
            'sources': [],
            'lastErrorCount': 0,
            'lastVersions': {},
        })
        self._save()
        return gid

    def remove(self, gid):
        with self._lock_for(gid):
            g = next((g for g in self.groups if g['id'] == gid), None)
            if g is None:
                return False
            for sid in (g.get('sources') or []):
                try:
                    self.sm.remove(sid)
                except Exception as ex:
                    log.warning('清理订阅组 %s 旧源 %s 失败: %s', gid, sid, ex)
            self.groups = [g for g in self.groups if g['id'] != gid]
            self._save()
        with self._locks_guard:
            self._locks.pop(gid, None)
        self.last_versions.pop(gid, None)
        return True

    def update(self, gid):
        with self._lock_for(gid):
            g = next((g for g in self.groups if g['id'] == gid), None)
            if g is None:
                raise RuntimeError('订阅组不存在')
            for sid in (g.get('sources') or []):
                try:
                    self.sm.remove(sid)
                except Exception as ex:
                    log.warning('清旧源 %s 失败: %s', sid, ex)
            g['sources'] = []
            new_sids, errors = [], []
            per_url_ver = {}
            for item in (g.get('urls') or []):
                url = (item.get('url') if isinstance(item, dict) else item) or ''
                kind = (item.get('kind') if isinstance(item, dict) else 'js') or 'js'
                if not url:
                    continue
                final_url = None
                try:
                    if kind == 'zip':
                        res_tuple = self.sm.import_from_url_zip(url)
                        if isinstance(res_tuple, tuple) and len(res_tuple) == 2:
                            res, final_url = res_tuple
                        else:
                            res = res_tuple
                        for a in (res.get('added') or []):
                            new_sids.append(a['id'])
                        for f in (res.get('failed') or []):
                            errors.append({'url': url, 'kind': kind, 'error': '[zip] ' + f.get('error', '')})
                    else:
                        ret = self.sm.import_from_url(url)
                        if isinstance(ret, tuple) and len(ret) == 2:
                            h, final_url = ret
                        else:
                            h = ret
                        new_sids.append(h.id)
                    ver = _extract_version(final_url or url)
                    if ver:
                        per_url_ver[url] = ver
                except Exception as e:
                    errors.append({'url': url, 'kind': kind, 'error': str(e)[:200]})
            if not any(x.get('id') == gid for x in self.groups):
                log.info('订阅组 %s 在更新过程中被删除, 跳过持久化', gid)
                return {'added': new_sids, 'errors': errors, 'lastUpdate': int(_time.time() * 1000), 'deleted': True}
            g['sources'] = new_sids
            g['lastUpdate'] = int(_time.time() * 1000)
            g['lastErrorCount'] = len(errors)
            if per_url_ver:
                self.last_versions[gid] = per_url_ver
                g['lastVersions'] = per_url_ver
            self._save()
            return {'added': new_sids, 'errors': errors, 'lastUpdate': g['lastUpdate'], 'versions': per_url_ver}


class AutoUpdater:
    """后台守护线程: 每 N 秒扫一遍订阅组, 到点的并行触发 update."""
    def __init__(self, sub_manager, check_interval_sec=30):
        self.sm = sub_manager
        self.check_interval = max(5, int(check_interval_sec))
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name='auto-updater')
        self._thread.start()
        log.info('auto-updater: 启动, 扫描间隔 %ds, 并行上限 %d', self.check_interval, AUTOUPDATER_PARALLEL)

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                now_ms = int(_time.time() * 1000)
                due = [
                    g for g in self.sm.list()
                    if now_ms - int(g.get('lastUpdate') or 0) >= int(g.get('intervalMin') or 60) * 60 * 1000
                ]
                if due:
                    self._run_due(due)
            except Exception:
                log.exception('auto-updater: 循环异常')
            for _ in range(self.check_interval):
                if self._stop.is_set():
                    return
                _time.sleep(1)

    def _run_due(self, due):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        workers = min(len(due), AUTOUPDATER_PARALLEL)
        log.info('auto-updater: %d 个订阅组到期, 并行 %d', len(due), workers)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(self.sm.update, g['id']): g for g in due}
            for fut in as_completed(futs):
                g = futs[fut]
                try:
                    r = fut.result(timeout=SUB_UPDATE_TIMEOUT)
                    log.info('auto-updater: 刷新 %s, 成功 %d / 错误 %d',
                             g.get('name'), len(r.get('added', [])), len(r.get('errors', [])))
                except Exception as e:
                    log.warning('auto-updater: 刷新 %s 失败: %s', g.get('name'), e)
