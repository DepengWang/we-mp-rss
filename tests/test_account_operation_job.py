import asyncio
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from core.config import cfg

cfg.config["db"] = "sqlite:///" + os.path.join(tempfile.gettempdir(), "werss-account-operation-test.db").replace("\\", "/")
cfg.reload = lambda: None

from apis import auth as auth_api
from core.switch_job import SwitchJobManager
from driver.wx import Wx


class _FakeWx:
    def __init__(self):
        self.closed_in_loop = None
        self.abandoned = threading.Event()

    async def Close(self):
        self.closed_in_loop = asyncio.get_running_loop()
        return True

    def abandon_account_operation(self):
        self.abandoned.set()
        return True


class StartAccountOperationJobTest(unittest.TestCase):
    def _run(self, operation, close_browser=False, grace=0.3, wait=3):
        wx = _FakeWx()
        manager = SwitchJobManager()
        job = manager.create_job(user_id="u")
        with patch("driver.wx.WX_API", wx), \
                patch.object(auth_api, "_account_operation_timeout", return_value=0.3), \
                patch.object(auth_api, "ACCOUNT_OPERATION_CLEANUP_GRACE", grace):
            auth_api._start_account_operation_job(
                manager,
                job.job_id,
                operation,
                lambda result, _job: (bool(result), "ok" if result else "failed"),
                close_browser=close_browser,
            )
            deadline = time.time() + wait
            while manager.get_job(job.job_id).finished_at is None and time.time() < deadline:
                time.sleep(0.02)
        return wx, manager.get_job(job.job_id)

    def test_success_closes_browser_in_operation_loop(self):
        loops = {}

        async def op(wx, progress):
            loops["op"] = asyncio.get_running_loop()
            return True

        wx, job = self._run(op, close_browser=True)
        self.assertTrue(job.ok)
        self.assertIs(wx.closed_in_loop, loops["op"])
        self.assertFalse(wx.abandoned.is_set())

    def test_hung_operation_is_cancelled_and_cleans_up_itself(self):
        cleaned = threading.Event()

        async def op(wx, progress):
            try:
                await asyncio.sleep(60)
            finally:
                cleaned.set()

        wx, job = self._run(op)
        self.assertFalse(job.ok)
        self.assertIn("未完成", job.message)
        # 取消后操作自己的 finally 负责恢复队列，看门狗不应插手
        self.assertTrue(cleaned.is_set())
        self.assertFalse(wx.abandoned.wait(0.5))

    def test_watchdog_recovers_when_cleanup_also_hangs(self):
        release = threading.Event()

        async def op(wx, progress):
            try:
                await asyncio.sleep(60)
            finally:
                # 模拟收尾时又卡在一次 Playwright 调用上
                while not release.is_set():
                    try:
                        await asyncio.sleep(0.05)
                    except asyncio.CancelledError:
                        pass

        try:
            wx, job = self._run(op, grace=0.2)
            self.assertTrue(wx.abandoned.is_set())
            self.assertFalse(job.ok)
            self.assertIn("恢复采集队列", job.message)
        finally:
            release.set()


class _FakeQueue:
    def __init__(self, running):
        self._is_running = running
        self.restarted = 0

    def run_task_background(self):
        self.restarted += 1
        self._is_running = True


class AbandonAccountOperationTest(unittest.TestCase):
    def _wx(self):
        wx = Wx.__new__(Wx)
        wx._account_operation_lock = threading.Lock()
        wx._account_operation_seq = 0
        wx.controller = object()
        return wx

    def test_restarts_paused_queues_and_releases_lock(self):
        wx = self._wx()
        self.assertIsNotNone(wx._begin_account_operation())
        main, content = _FakeQueue(False), _FakeQueue(True)
        with patch("core.queue.TaskQueue", main), patch("core.queue.ContentTaskQueue", content):
            self.assertTrue(wx.abandon_account_operation())
        self.assertEqual(main.restarted, 1)
        self.assertEqual(content.restarted, 0)
        self.assertIsNone(wx.controller)
        self.assertFalse(wx._account_operation_lock.locked())

    def test_late_cleanup_of_abandoned_operation_keeps_newer_lock(self):
        wx = self._wx()
        stale = wx._begin_account_operation()
        with patch("core.queue.TaskQueue", _FakeQueue(True)), \
                patch("core.queue.ContentTaskQueue", _FakeQueue(True)):
            wx.abandon_account_operation()
        newer = wx._begin_account_operation()
        self.assertIsNotNone(newer)
        # 被放弃的操作迟到的 finally 不能放掉新操作持有的锁
        self.assertFalse(wx._end_account_operation(stale))
        self.assertTrue(wx._account_operation_lock.locked())
        self.assertTrue(wx._end_account_operation(newer))
        self.assertFalse(wx._account_operation_lock.locked())

    def test_nothing_to_abandon_when_idle(self):
        wx = self._wx()
        self.assertFalse(wx.abandon_account_operation())


if __name__ == "__main__":
    unittest.main()
