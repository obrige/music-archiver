# -*- coding: utf-8 -*-
"""并发任务队列: 线程池执行 + 监听器推送(SSE 用)"""
import queue
import threading
import time
import uuid


class TaskItem:
    def __init__(self, tid, title):
        self.id = tid
        self.title = title
        self.status = 'queued'
        self.progress = 0
        self.log = []
        self.files = 0
        self.done = 0
        self.created_at = time.time()

    def to_dict(self):
        return {
            'id': self.id, 'title': self.title, 'status': self.status,
            'progress': self.progress, 'files': self.files, 'done': self.done,
            'log': self.log[-10:],
        }


class TaskQueue:
    def __init__(self, concurrency=3):
        self.concurrency = concurrency
        self.tasks = []
        self._q = queue.Queue()
        self._lock = threading.Lock()
        self._listeners = set()
        self._workers = []

    def start(self):
        if self._workers:
            return
        for _ in range(self.concurrency):
            t = threading.Thread(target=self._run, daemon=True)
            t.start()
            self._workers.append(t)

    def _run(self):
        while True:
            task, fn = self._q.get()
            task.status = 'running'
            self._emit(task)
            try:
                fn(task)
                if task.status == 'running':
                    task.status = 'success'
                    task.progress = 100
            except Exception as e:
                task.status = 'failed'
                task.log.append('错误: ' + str(e))
            self._emit(task)

    def add(self, title, fn):
        tid = 'task_' + uuid.uuid4().hex[:10]
        task = TaskItem(tid, title)
        with self._lock:
            self.tasks.insert(0, task)
        self._emit(task)
        self._q.put((task, fn))
        return tid

    def log(self, task, msg):
        task.log.append(msg)
        if len(task.log) > 200:
            del task.log[:len(task.log) - 200]
        self._emit(task)

    def progress(self, task, done, files):
        task.done = done
        task.files = files
        task.progress = round(done / files * 100) if files else 0
        self._emit(task)

    def list(self):
        with self._lock:
            return [t.to_dict() for t in self.tasks]

    def subscribe(self, fn):
        self._listeners.add(fn)

    def unsubscribe(self, fn):
        self._listeners.discard(fn)

    def _emit(self, task):
        data = task.to_dict()
        for fn in list(self._listeners):
            try:
                fn(data)
            except Exception:
                pass
