import asyncio
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from core.config import cfg

cfg.config["db"] = "sqlite:///" + os.path.join(tempfile.gettempdir(), "werss-rotation-runner-test.db").replace("\\", "/")
cfg.reload = lambda: None

from jobs import mps


class _FakeWx:
    def __init__(self, rotate):
        self._rotate = rotate
        self.controller = object()
        self.closed_in_loop = None
        self.rotated_in_loop = None

    async def auto_rotate_account_by_id(self):
        self.rotated_in_loop = asyncio.get_running_loop()
        return await self._rotate()

    async def Close(self):
        self.closed_in_loop = asyncio.get_running_loop()
        return True


class RunAccountRotationTest(unittest.TestCase):
    def _run(self, wx, timeout=5):
        with patch.object(mps, "WX_API", wx):
            return mps._run_account_rotation(timeout=timeout)

    def test_closes_browser_in_the_same_loop_after_rotation(self):
        async def ok():
            return True

        wx = _FakeWx(ok)
        self.assertEqual(self._run(wx), {"ok": True})
        # 浏览器必须在创建它的事件循环里关闭，不能留给下一轮的新循环
        self.assertIsNotNone(wx.closed_in_loop)
        self.assertIs(wx.closed_in_loop, wx.rotated_in_loop)

    def test_closes_browser_when_rotation_fails(self):
        async def boom():
            raise RuntimeError("switch failed")

        wx = _FakeWx(boom)
        outcome = self._run(wx)
        self.assertFalse(outcome["ok"])
        self.assertIn("switch failed", outcome["error"])
        self.assertIsNotNone(wx.closed_in_loop)

    def test_hung_rotation_times_out_and_drops_controller(self):
        release = threading.Event()

        async def hang():
            while not release.is_set():
                await asyncio.sleep(0.05)
            return True

        wx = _FakeWx(hang)
        try:
            outcome = self._run(wx, timeout=0.3)
        finally:
            release.set()
        self.assertFalse(outcome["ok"])
        self.assertIn("未完成", outcome["error"])
        self.assertIsNone(wx.controller)


if __name__ == "__main__":
    unittest.main()
