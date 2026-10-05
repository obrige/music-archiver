# -*- coding: utf-8 -*-
"""音源管理: 洛雪音源脚本(JS)通过 node 子进程桥在沙箱中运行
设计: 每次解析请求都启动全新桥进程, 用完即杀, 彻底避免进程卡死/管道堆积拖垮服务
"""
import json
import queue
import subprocess
import threading
import time
import urllib.request
import uuid
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
        self._save()
        return h

    def import_script(self, raw):
        return self._add(raw)

    def import_from_url(self, url):
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                raise RuntimeError('下载失败 HTTP %s %s' % (r.status, url))
            text = r.read().decode('utf-8', 'replace')
        return self._add(text)

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
