# -*- coding: utf-8 -*-
"""音源管理: 洛雪音源脚本(JS)通过 node 子进程桥在沙箱中运行
设计: 每次解析请求都启动全新桥进程, 用完即杀, 彻底避免进程卡死/管道堆积拖垮服务

扩展(批次/压缩/订阅/评分):
- SourceManager.import_from_files: 多个 .js 文件批量导入(前端 multipart)
- SourceManager.import_from_zip_path / _bytes / _url_zip: ZIP 批量导入(支持流式)
- SubscriptionManager: 订阅分组(URL 列表 + 自动刷新间隔, per-group 锁防并发竞争)
- AutoUpdater: 后台线程并行刷新到期订阅组
- SourceScore: per-source 滚动统计(成功率/平均延迟/按音质成功率), 复合分用于智能选源
- SourceManager.handles_for_score(): 按分数降序返回, 供 resolve-all 优先跑高分源
"""
import io
import json
import logging
import os
import queue
import subprocess
import threading
import time as _time
import urllib.request
import uuid
import zipfile
from pathlib import Path

log = logging.getLogger('musicarchiver.sources')

BRIDGE_TIMEOUT = 35
META_TIMEOUT = 15
SUB_INTERVAL_MIN = 5
SUB_INTERVAL_MAX = 10080  # 1 周
SUB_UPDATE_TIMEOUT = 300
AUTOUPDATER_PARALLEL = 3
SCORE_SAVE_INTERVAL = 30  # 评分落盘间隔(秒), 防每请求写盘


class SourceScore:
    """单源滚动统计: Bayesian 平滑成功率 + 滑动窗口平均延迟 + 按音质分桶成功率.
    复合分 score = 0.4 × 音质匹配 + 0.3 × 速度 + 0.3 × 可靠性, 范围 [0, 1], 越高越优先."""
    RECENT_WINDOW = 20           # 滑动窗口样本数
    BAYES_PRIOR_TRIALS = 5       # 贝叶斯平滑先验次数(防冷启极端)
    BAYES_PRIOR_SUCCESS = 2.5    # 贝叶斯平滑先验成功(0.5 = 中性)
    LATENCY_FAST_MS = 500        # 这延迟以下算极快
    LATENCY_SLOW_MS = 10000      # 这延迟以上算极慢
    WEIGHT_QUALITY = 0.4
    WEIGHT_SPEED = 0.3
    WEIGHT_RELIABILITY = 0.3

    def __init__(self):
        self.successes = 0
        self.failures = 0
        self.last_success_at = None
        self.last_failure_at = None
        self.quality_attempts = {}   # quality -> int
        self.quality_successes = {}
        self.recent_latencies = []   # last RECENT_WINDOW (ms)

    def record(self, success, latency_ms=None, quality=None):
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
        """Bayesian-smoothed: 极少调用时往 0.5 拉, 防极端"""
        if self.total_trials == 0:
            return 0.5
        return (self.successes + self.BAYES_PRIOR_SUCCESS) / (self.total_trials + self.BAYES_PRIOR_TRIALS)

    @property
    def avg_latency_ms(self):
        if self.recent_latencies:
            return sum(self.recent_latencies) / len(self.recent_latencies)
        return (self.LATENCY_FAST_MS + self.LATENCY_SLOW_MS) / 2  # 中位假设

    def quality_score(self, quality):
        """该源对此音质的把握: 0-1, 0.5 = 无数据"""
        a = self.quality_attempts.get(quality, 0)
        if a == 0:
            return 0.5
        s = self.quality_successes.get(quality, 0)
        return (s + 1) / (a + 2)  # Bayesian 平滑

    def score(self, requested_quality=None):
        """综合分: 0-1, 越高越优先."""
        if requested_quality:
            qs = self.quality_score(requested_quality)
        else:
            # 无指定音质: 用总成功率
            ta = sum(self.quality_attempts.values())
            ts = sum(self.quality_successes.values())
            qs = (ts + 1) / (ta + 2) if ta else 0.5
        # 速度分: 0..1, LATENCY_FAST_MS -> 1, LATENCY_SLOW_MS -> 0
        lat = self.avg_latency_ms
        spd = max(0.0, min(1.0, (self.LATENCY_SLOW_MS - lat) / (self.LATENCY_SLOW_MS - self.LATENCY_FAST_MS)))
        rel = self.success_rate
        return qs * self.WEIGHT_QUALITY + spd * self.WEIGHT_SPEED + rel * self.WEIGHT_RELIABILITY

    def to_dict(self):
        return {
            'successes': self.successes,
            'failures': self.failures,
            'successRate': round(self.success_rate, 3),
            'avgLatencyMs': int(self.avg_latency_ms),
            'lastSuccessAt': self.last_success_at,
            'lastFailureAt': self.last_failure_at,
            'qualitySuccess': dict(self.quality_successes),
            'qualityAttempts': dict(self.quality_attempts),
        }

    @classmethod
    def from_dict(cls, d):
        s = cls()
        s.successes = int(d.get('successes') or 0)
        s.failures = int(d.get('failures') or 0)
        s.last_success_at = d.get('lastSuccessAt')
        s.last_failure_at = d.get('lastFailureAt')
        s.quality_successes = dict(d.get('qualitySuccess') or {})
        s.quality_attempts = dict(d.get('qualityAttempts') or {})
        return s


class LxScriptHandle:
    def __init__(self, script_path, bridge_path, node='node', on_dirty=None):
        self.script_path = str(script_path)
        self.bridge_path = str(bridge_path)
        self.node = node
        self.id = ''
        self.name = ''
        self.sources = {}
        self.last_success = None
        self.score = SourceScore()
        self._on_dirty = on_dirty  # callback when score changes (用于触发持久化)
        # 单音源限速(不同音源互不干扰, 每个音源自身请求间隔 0.5s 防 429)
        self._rate_lock = threading.Lock()
        self._rate_last = [0.0]
        self._rate_interval = 0.5
        self._load_meta()

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

    def _load_meta(self):
        proc, q = self._spawn()
        try:
            while True:
                msg = q.get(timeout=META_TIMEOUT)
                mtype = msg.get('type')
                if mtype == 'meta':
                    self.name = msg.get('name') or '未命名音源'
                    self.sources = msg.get('sources') or {}
                    return
                if mtype == 'console':
                    log.debug('script console: %s', msg.get('msg'))
                    continue
                if msg.get('ok') is False:
                    raise RuntimeError(msg.get('error') or '音源脚本初始化失败')
        except queue.Empty:
            self._kill(proc)
            err = self._read_stderr(proc)
            raise RuntimeError('音源脚本初始化失败: ' + (err.strip() if err.strip() else '未在超时内收到 meta'))
        finally:
            self._kill(proc)

    def _record(self, success, latency_ms, quality):
        """记录一次调用结果到评分系统; 标记脏, 由 SourceManager 周期落盘."""
        self.score.record(success, latency_ms, quality)
        if self._on_dirty:
            try:
                self._on_dirty()
            except Exception:
                log.exception('on_dirty callback failed')

    def resolve(self, source, song, quality):
        t0 = _time.time()
        proc, q = self._spawn()
        success = False
        try:
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
                    raise RuntimeError(msg.get('error') or '音源解析失败')
                if msg.get('ok'):
                    success = True
                    self.last_success = _time.time()
                    return msg.get('data') or {}
                raise RuntimeError('音源返回格式无效')
        except queue.Empty:
            raise RuntimeError('音源请求超时')
        finally:
            self._kill(proc)
            ms = int((_time.time() - t0) * 1000)
            self._record(success, ms if success else None, quality)

    def _throttle(self):
        with self._rate_lock:
            now = _time.time()
            wait = self._rate_interval - (now - self._rate_last[0])
            if wait > 0:
                _time.sleep(wait)
            self._rate_last[0] = _time.time()

    def resolve_multi(self, platform, song, qualities, overall_timeout=30):
        """同一桥进程内依次解析多个音质(校验用, 快 N 倍); 整体超时避免慢源拖累"""
        self._throttle()
        proc, q = self._spawn()
        deadline_all = _time.time() + overall_timeout
        try:
            results = {}
            for quality in qualities:
                if _time.time() > deadline_all:
                    break
                req = {'platform': platform, 'action': 'musicUrl',
                       'info': {'musicInfo': dict(song), 'type': quality}}
                proc.stdin.write(json.dumps(req, ensure_ascii=False) + '\n')
                proc.stdin.flush()
                # 实时性: 单音质最多等 18s, 且不超过整体超时
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
                        break
                    if msg.get('ok'):
                        results[quality] = (msg.get('data') or {}).get('url')
                        break
            if results:
                self.last_success = _time.time()
            return results
        finally:
            self._kill(proc)
            # 整体一次计为一次调用, 记到"无指定音质"的综合分
            t_end = _time.time()
            ms = int((t_end - deadline_all + overall_timeout) * 1000) if False else 0
            # 暂用整体耗时估值(总超时作为上限)
            ms = int(overall_timeout * 1000) if not results else int((overall_timeout - max(0, deadline_all - t_end)) * 1000)
            self._record(bool(results), ms, None)

    def search(self, platform, keyword, page=1, limit=30):
        """脚本原生搜索(需音源声明 search/musicSearch action)"""
        actions = (self.sources.get(platform) or {}).get('actions') or []
        action = 'musicSearch' if 'musicSearch' in actions else 'search'
        t0 = _time.time()
        proc, q = self._spawn()
        success = False
        try:
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
                    raise RuntimeError(msg.get('error') or '搜索失败')
                if msg.get('ok'):
                    success = True
                    data = msg.get('data') or {}
                    return data.get('list') or []
        except queue.Empty:
            raise RuntimeError('搜索超时')
        finally:
            self._kill(proc)
            ms = int((_time.time() - t0) * 1000)
            self._record(success, ms if success else None, None)

    def close(self):
        pass


class SourceManager:
    def __init__(self, data_dir, bridge_path, node='node'):
        self.data_dir = Path(data_dir)
        self.store_file = self.data_dir / 'sources.json'
        self.scripts_dir = self.data_dir / 'scripts'
        self.bridge_path = bridge_path
        self.node = node
        self.handles = {}
        # 原子写入 + 评分落盘
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
                        # 恢复评分
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
        # 启动周期落盘线程
        self._start_periodic_save()

    def _start_periodic_save(self):
        if self._save_thread and self._save_thread.is_alive():
            return
        self._save_thread = threading.Thread(target=self._save_loop, daemon=True, name='sources-save')
        self._save_thread.start()
        log.debug('sources-save: 后台落盘线程启动, 间隔 %ds', SCORE_SAVE_INTERVAL)

    def _mark_dirty(self):
        """评分变更回调: 标记脏, 落盘线程 30s 内写盘."""
        self._dirty = True

    def _save_loop(self):
        while not self._save_stop.is_set():
            # 可中断 sleep
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
        """原子写入: tmp + os.replace, 配合 _save_lock 防并发损坏 sources.json"""
        with self._save_lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            entries = []
            for sid, h in self.handles.items():
                try:
                    raw = (self.scripts_dir / (sid + '.js')).read_text('utf-8')
                except Exception:
                    raw = ''
                entries.append({
                    'id': sid,
                    'name': h.name,
                    'raw': raw,
                    'lastSuccess': h.last_success,
                    'score': h.score.to_dict(),
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
        return self.import_script(text)

    # ---- 批量 / 压缩包 ----
    def import_from_files(self, files):
        """从前端 multipart 传入的多个 File 对象批量导入.
        files: iterable of file-like (有 .read() / .filename) 或 (filename, raw_bytes) 元组"""
        added, failed = [], []
        for item in files:
            try:
                if isinstance(item, tuple):
                    fn, raw_bytes = item
                    fn = fn or '?'
                    raw = raw_bytes.decode('utf-8', 'replace') if isinstance(raw_bytes, (bytes, bytearray)) else str(raw_bytes)
                else:
                    fn = getattr(item, 'filename', None) or '?'
                    raw_bytes = item.read()
                    raw = raw_bytes.decode('utf-8', 'replace') if isinstance(raw_bytes, (bytes, bytearray)) else str(raw_bytes)
                h = self._add(raw)
                added.append({'id': h.id, 'name': h.name, 'file': fn})
            except Exception as e:
                failed.append({'file': getattr(item, 'filename', '?'), 'error': str(e)[:120]})
        if added or failed:
            self._save()
        return {'added': added, 'failed': failed}

    def import_from_zip_path(self, zip_path):
        """从磁盘 ZIP 路径提取所有 .js. 流式上传的主入口(避免 OOM).
        跳过目录条目 / 隐藏文件 / 非 .js."""
        added, failed = [], []
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
                    h = self._add(raw)
                    added.append({'id': h.id, 'name': h.name, 'file': name})
                except Exception as e:
                    failed.append({'file': name, 'error': str(e)[:120]})
        finally:
            try:
                z.close()
            except Exception:
                pass
        if added:
            self._save()
        return {'added': added, 'failed': failed}

    def import_from_zip_bytes(self, raw_bytes):
        """从 ZIP 字节流提取所有 .js. 适合小 ZIP; 大 ZIP 走 import_from_zip_path 流式"""
        added, failed = [], []
        try:
            z = zipfile.ZipFile(io.BytesIO(raw_bytes))
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
                    h = self._add(raw)
                    added.append({'id': h.id, 'name': h.name, 'file': name})
                except Exception as e:
                    failed.append({'file': name, 'error': str(e)[:120]})
        finally:
            try:
                z.close()
            except Exception:
                pass
        if added:
            self._save()
        return {'added': added, 'failed': failed}

    def import_from_url_zip(self, url):
        """下载 URL 指向的 ZIP 后批量导入"""
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.status != 200:
                raise RuntimeError('下载失败 HTTP %s %s' % (r.status, url))
            raw = r.read()
        if not raw:
            raise RuntimeError('下载内容为空')
        return self.import_from_zip_bytes(raw)

    def remove(self, sid):
        h = self.handles.pop(sid, None)
        if h is None:
            return False
        h.close()
        self._save()
        return True

    def get(self, sid):
        return self.handles.get(sid)

    def handles_for(self, platform):
        """支持指定平台的所有音源句柄(自动解析用)"""
        return [h for h in self.handles.values() if platform in h.sources]

    def handles_for_score(self, platform, quality=None):
        """返回按分数降序的句柄列表. quality=None 时按综合分排."""
        handles = self.handles_for(platform)
        return sorted(handles, key=lambda h: -h.score.score(quality))

    def handles_topk(self, platform, quality=None, k=3):
        """按分数取 top-K 句柄. 至少返回 1 个; 不足则全返."""
        if k < 1:
            k = 1
        return self.handles_for_score(platform, quality)[:k]

    def list(self):
        return [{
            'id': sid, 'name': h.name, 'kind': 'lxScript', 'sources': h.sources,
            'lastSuccess': h.last_success, 'score': h.score.to_dict(),
        } for sid, h in self.handles.items()]

    def scores_map(self):
        """id -> { name, score, lastSuccess } 精简视图, 供前端快速获取."""
        return {
            sid: {'name': h.name, 'score': h.score.to_dict(), 'lastSuccess': h.last_success}
            for sid, h in self.handles.items()
        }

    def close_all(self):
        self.stop_periodic_save()


class SubscriptionManager:
    """订阅分组: 一组 URL(.js / .zip 混合), 定时整体刷新.
    per-group 锁防止 update()/remove()/并发的 AutoUpdater 刷新互踩.
    刷新时清理上一次导入的 source, 再重下."""
    def __init__(self, data_dir, source_manager):
        self.data_dir = Path(data_dir)
        self.store_file = self.data_dir / 'subscriptions.json'
        self.sm = source_manager
        self.groups = []
        self._locks = {}
        self._locks_guard = threading.Lock()

    def init(self):
        if not self.store_file.exists():
            return
        try:
            self.groups = json.loads(self.store_file.read_text('utf-8'))
            if not isinstance(self.groups, list):
                self.groups = []
            log.info('subs: 已恢复 %d 个订阅组', len(self.groups))
        except Exception as e:
            log.warning('subs: 读取 subscriptions.json 失败: %s', e)
            self.groups = []

    def _save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
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
            for item in (g.get('urls') or []):
                url = (item.get('url') if isinstance(item, dict) else item) or ''
                kind = (item.get('kind') if isinstance(item, dict) else 'js') or 'js'
                if not url:
                    continue
                try:
                    if kind == 'zip':
                        res = self.sm.import_from_url_zip(url)
                        for a in (res.get('added') or []):
                            new_sids.append(a['id'])
                        for f in (res.get('failed') or []):
                            errors.append({'url': url, 'kind': kind, 'error': '[zip] ' + f.get('error', '')})
                    else:
                        h = self.sm.import_from_url(url)
                        new_sids.append(h.id)
                except Exception as e:
                    errors.append({'url': url, 'kind': kind, 'error': str(e)[:200]})
            if not any(x.get('id') == gid for x in self.groups):
                log.info('订阅组 %s 在更新过程中被删除, 跳过持久化', gid)
                return {'added': new_sids, 'errors': errors, 'lastUpdate': int(_time.time() * 1000), 'deleted': True}
            g['sources'] = new_sids
            g['lastUpdate'] = int(_time.time() * 1000)
            g['lastErrorCount'] = len(errors)
            self._save()
            return {'added': new_sids, 'errors': errors, 'lastUpdate': g['lastUpdate']}


class AutoUpdater:
    """后台守护线程: 每 N 秒扫一遍订阅组, 到点的并行触发 update.
    单组间隔下限 5 分钟(防滥用), 上限 1 周. 并行上限 3 个."""
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
