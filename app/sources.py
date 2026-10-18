# -*- coding: utf-8 -*-
"""音源管理: 洛雪音源脚本(JS)通过 node 子进程桥在沙箱中运行
设计: 每次解析请求都启动全新桥进程, 用完即杀, 彻底避免进程卡死/管道堆积拖垮服务

扩展(批次/压缩/订阅):
- SourceManager.import_from_files: 多个 .js 文件批量导入(前端 multipart)
- SourceManager.import_from_zip_bytes: ZIP 字节流批量导入
- SourceManager.import_from_url_zip: URL 下载 ZIP 后批量导入
- SubscriptionManager: 订阅分组(URL 列表 + 自动刷新间隔)
- AutoUpdater: 后台线程定期检查订阅组, 间隔到达则刷新
"""
import io
import json
import queue
import subprocess
import threading
import time as _time
import urllib.request
import uuid
import zipfile
from pathlib import Path

BRIDGE_TIMEOUT = 35
META_TIMEOUT = 15


class LxScriptHandle:
    def __init__(self, script_path, bridge_path, node='node'):
        self.script_path = str(script_path)
        self.bridge_path = str(bridge_path)
        self.node = node
        self.id = ''
        self.name = ''
        self.sources = {}
        self.last_success = None
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
                    print('[bridge] 忽略非对象消息: ' + str(line)[:200])
                    continue
                if isinstance(msg, dict):
                    q.put(msg)
                else:
                    print('[bridge] 忽略非对象消息: ' + str(line)[:200])
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
                    print('[script] ' + str(msg.get('msg')))
                    continue
                if msg.get('ok') is False:
                    raise RuntimeError(msg.get('error') or '音源脚本初始化失败')
        except queue.Empty:
            self._kill(proc)
            err = self._read_stderr(proc)
            raise RuntimeError('音源脚本初始化失败: ' + (err.strip() if err.strip() else '未在超时内收到 meta'))
        finally:
            self._kill(proc)

    def resolve(self, source, song, quality):
        proc, q = self._spawn()
        try:
            req = {'source': source, 'action': 'musicUrl',
                   'info': {'musicInfo': dict(song), 'type': quality}}
            proc.stdin.write(json.dumps(req, ensure_ascii=False) + '\n')
            proc.stdin.flush()
            deadline = time.time() + BRIDGE_TIMEOUT
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    raise RuntimeError('音源请求超时')
                msg = q.get(timeout=remain)
                mtype = msg.get('type')
                if mtype in ('bridge', 'meta', 'console'):
                    if mtype == 'console':
                        print('[script] ' + str(msg.get('msg')))
                    continue
                if msg.get('ok') is False:
                    raise RuntimeError(msg.get('error') or '音源解析失败')
                if msg.get('ok'):
                    self.last_success = time.time()
                    return msg.get('data') or {}
                raise RuntimeError('音源返回格式无效')
        except queue.Empty:
            raise RuntimeError('音源请求超时')
        finally:
            self._kill(proc)

    def _throttle(self):
        with self._rate_lock:
            now = time.time()
            wait = self._rate_interval - (now - self._rate_last[0])
            if wait > 0:
                time.sleep(wait)
            self._rate_last[0] = time.time()

    def resolve_multi(self, platform, song, qualities, overall_timeout=30):
        """同一桥进程内依次解析多个音质(校验用, 快 N 倍); 整体超时避免慢源拖累"""
        self._throttle()
        proc, q = self._spawn()
        deadline_all = time.time() + overall_timeout
        try:
            results = {}
            for quality in qualities:
                if time.time() > deadline_all:
                    break
                req = {'source': platform, 'action': 'musicUrl',
                       'info': {'musicInfo': dict(song), 'type': quality}}
                proc.stdin.write(json.dumps(req, ensure_ascii=False) + '\n')
                proc.stdin.flush()
                # 实时性: 单音质最多等 18s, 且不超过整体超时
                deadline = min(time.time() + 18, deadline_all)
                while True:
                    remain = deadline - time.time()
                    if remain <= 0:
                        break
                    try:
                        msg = q.get(timeout=remain)
                    except queue.Empty:
                        break
                    mtype = msg.get('type')
                    if mtype in ('bridge', 'meta', 'console'):
                        if mtype == 'console':
                            print('[script] ' + str(msg.get('msg')))
                        continue
                    if msg.get('ok') is False:
                        break
                    if msg.get('ok'):
                        results[quality] = (msg.get('data') or {}).get('url')
                        break
            if results:
                self.last_success = time.time()
            return results
        finally:
            self._kill(proc)

    def search(self, platform, keyword, page=1, limit=30):
        """脚本原生搜索(需音源声明 search/musicSearch action)"""
        actions = (self.sources.get(platform) or {}).get('actions') or []
        action = 'musicSearch' if 'musicSearch' in actions else 'search'
        proc, q = self._spawn()
        try:
            req = {'source': platform, 'action': action, 'info': {'keyword': keyword, 'page': page, 'limit': limit}}
            proc.stdin.write(json.dumps(req, ensure_ascii=False) + '\n')
            proc.stdin.flush()
            deadline = time.time() + 30
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    raise RuntimeError('搜索超时')
                msg = q.get(timeout=remain)
                mtype = msg.get('type')
                if mtype in ('bridge', 'meta', 'console'):
                    continue
                if msg.get('ok') is False:
                    raise RuntimeError(msg.get('error') or '搜索失败')
                if msg.get('ok'):
                    data = msg.get('data') or {}
                    return data.get('list') or []
        except queue.Empty:
            raise RuntimeError('搜索超时')
        finally:
            self._kill(proc)

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

    def init(self):
        if not self.store_file.exists():
            return
        try:
            entries = json.loads(self.store_file.read_text('utf-8'))
            for e in entries:
                try:
                    raw = e.get('raw') or ''
                    script_file = self._write_script(e['id'], raw)
                    h = LxScriptHandle(script_file, self.bridge_path, self.node)
                    h.id = e['id']
                    h.last_success = e.get('lastSuccess')
                    self.handles[e['id']] = h
                except Exception as ex:
                    print('[sources] 恢复失败 %s: %s' % (e.get('name'), ex))
            print('[sources] 已恢复 %d 个音源' % len(self.handles))
        except Exception:
            pass

    def _write_script(self, sid, raw):
        self.scripts_dir.mkdir(parents=True, exist_ok=True)
        f = self.scripts_dir / (sid + '.js')
        f.write_text(raw, 'utf-8')
        return f

    def _save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        entries = []
        for sid, h in self.handles.items():
            try:
                raw = (self.scripts_dir / (sid + '.js')).read_text('utf-8')
            except Exception:
                raw = ''
            entries.append({'id': sid, 'name': h.name, 'raw': raw, 'lastSuccess': h.last_success})
        self.store_file.write_text(json.dumps(entries, ensure_ascii=False, indent=2), 'utf-8')

    def _add(self, raw):
        sid = 'lx_' + uuid.uuid4().hex[:10]
        script_file = self._write_script(sid, raw)
        h = LxScriptHandle(script_file, self.bridge_path, self.node)
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

    def import_from_zip_bytes(self, raw_bytes):
        """从 ZIP 字节流提取所有 .js, 逐个导入. 跳过目录/隐藏文件/非 .js"""
        added, failed = [], []
        try:
            z = zipfile.ZipFile(io.BytesIO(raw_bytes))
        except Exception as e:
            raise RuntimeError('不是有效的 ZIP 文件: ' + str(e))
        try:
            for name in z.namelist():
                # 跳过目录条目 / 隐藏文件 / 非 .js
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

    def list(self):
        return [{
            'id': sid, 'name': h.name, 'kind': 'lxScript', 'sources': h.sources,
            'lastSuccess': h.last_success,
        } for sid, h in self.handles.items()]

    def close_all(self):
        pass


class SubscriptionManager:
    """订阅分组: 一组 URL(.js / .zip 混合), 定时整体刷新. 刷新时清理上一次导入的 source, 再重下"""
    def __init__(self, data_dir, source_manager):
        self.data_dir = Path(data_dir)
        self.store_file = self.data_dir / 'subscriptions.json'
        self.sm = source_manager
        self.groups = []

    def init(self):
        if not self.store_file.exists():
            return
        try:
            self.groups = json.loads(self.store_file.read_text('utf-8'))
            if not isinstance(self.groups, list):
                self.groups = []
            print('[subs] 已恢复 %d 个订阅组' % len(self.groups))
        except Exception:
            self.groups = []

    def _save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store_file.write_text(json.dumps(self.groups, ensure_ascii=False, indent=2), 'utf-8')

    def list(self):
        return self.groups

    def add(self, name, urls, interval_min=60):
        gid = 'sub_' + uuid.uuid4().hex[:10]
        self.groups.append({
            'id': gid,
            'name': name,
            'urls': urls,  # [{url, kind: 'js'|'zip'}]
            'intervalMin': max(5, int(interval_min)),
            'lastUpdate': None,
            'sources': [],  # 该组当前关联的 source id, 刷新时清掉
        })
        self._save()
        return gid

    def remove(self, gid):
        # 同时清理该组关联的 source
        g = next((g for g in self.groups if g['id'] == gid), None)
        if g is None:
            return False
        for sid in (g.get('sources') or []):
            try:
                self.sm.remove(sid)
            except Exception:
                pass
        self.groups = [g for g in self.groups if g['id'] != gid]
        self._save()
        return True

    def update(self, gid):
        """刷新指定组: 删旧 source, 重下所有 URL, 记录新 ids"""
        g = next((g for g in self.groups if g['id'] == gid), None)
        if g is None:
            raise RuntimeError('订阅组不存在')
        # 清理旧 source
        for sid in (g.get('sources') or []):
            try:
                self.sm.remove(sid)
            except Exception:
                pass
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
        g['sources'] = new_sids
        g['lastUpdate'] = int(_time.time() * 1000)
        self._save()
        return {'added': new_sids, 'errors': errors, 'lastUpdate': g['lastUpdate']}


class AutoUpdater:
    """后台守护线程: 每 N 秒扫一遍订阅组, 间隔到达则触发 update.
    默认 30s 一次扫描, 单组间隔最低 5 分钟(防滥用)."""
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
        print('[auto-updater] 启动, 扫描间隔 %ds' % self.check_interval)

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                now_ms = int(_time.time() * 1000)
                for g in list(self.sm.list()):
                    interval_ms = int(g.get('intervalMin') or 60) * 60 * 1000
                    last = int(g.get('lastUpdate') or 0)
                    if now_ms - last >= interval_ms:
                        try:
                            self.sm.update(g['id'])
                            print('[auto-updater] 刷新订阅组: %s (%d 个 source)' % (g.get('name'), len(g.get('sources') or [])))
                        except Exception as e:
                            print('[auto-updater] 刷新失败 %s: %s' % (g.get('name'), e))
            except Exception as e:
                print('[auto-updater] 循环异常: ' + str(e))
            # 可中断 sleep
            for _ in range(self.check_interval):
                if self._stop.is_set():
                    return
                _time.sleep(1)
