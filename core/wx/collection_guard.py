"""进程内公众号采集互斥与失败退避。"""

import threading
import time
from contextlib import contextmanager


class CollectionBusyError(RuntimeError):
    """同一个 Feed 当前已经有采集任务在运行。"""


class CollectionCooldownError(RuntimeError):
    """同一个 Feed 仍处于失败冷却期。"""

    def __init__(self, mp_id: str, remaining: float):
        self.remaining = max(0, int(remaining))
        super().__init__(f"Feed {mp_id} 仍在失败冷却期，请 {self.remaining} 秒后重试")


class CollectionGuard:
    """同一进程内按 Feed ID串行化采集，并提供有限指数退避。"""

    _registry_lock = threading.Lock()
    _locks = {}
    _failures = {}
    _cooldown_until = {}
    _base_delay = 60
    _max_delay = 600

    def __init__(self, mp_id: str):
        self.mp_id = str(mp_id or "")
        with self._registry_lock:
            self._lock = self._locks.setdefault(self.mp_id, threading.Lock())

    def _check_cooldown(self):
        now = time.monotonic()
        until = self._cooldown_until.get(self.mp_id, 0)
        if until > now:
            raise CollectionCooldownError(self.mp_id, until - now)

    def acquire(self):
        self._check_cooldown()
        if not self._lock.acquire(blocking=False):
            raise CollectionBusyError(f"Feed {self.mp_id} 当前已有采集任务运行")
        try:
            # 在等待获取锁期间，其他任务可能刚刚设置了冷却状态。
            self._check_cooldown()
        except Exception:
            self._lock.release()
            raise

    def release(self):
        if self._lock.locked():
            self._lock.release()

    def record_failure(self):
        with self._registry_lock:
            failures = self._failures.get(self.mp_id, 0) + 1
            self._failures[self.mp_id] = failures
            delay = min(self._base_delay * (2 ** (failures - 1)), self._max_delay)
            self._cooldown_until[self.mp_id] = time.monotonic() + delay
        return delay

    def record_success(self):
        with self._registry_lock:
            self._failures.pop(self.mp_id, None)
            self._cooldown_until.pop(self.mp_id, None)

    @contextmanager
    def hold(self):
        self.acquire()
        try:
            yield self
        except Exception:
            self.record_failure()
            raise
        finally:
            self.release()

