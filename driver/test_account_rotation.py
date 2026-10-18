import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core.config import cfg

cfg.config["db"] = "sqlite:///" + os.path.join(tempfile.gettempdir(), "werss-rotation-test.db").replace("\\", "/")
cfg.reload = lambda: None

from driver.wx import Wx


def _session(name):
    return {
        "name": name,
        "token": "t",
        "cookies": [{"name": "slave_sid", "value": "x"}],
        "expiry": {"expiry_timestamp": time.time() + 3600},
    }


SESSIONS = {"gh_a": _session("A"), "gh_b": _session("B"), "gh_c": _session("C")}


def _status(active):
    return {
        "active_account_id": active,
        "accounts": [
            {"account_id": k, "name": v["name"], "token_status": "valid"}
            for k, v in SESSIONS.items()
        ],
    }


class AutoRotateWithoutLivePageTest(unittest.TestCase):
    def _rotate(self, wx, active):
        with patch("driver.token.get_account_status_data", return_value=_status(active)), \
                patch("driver.token.get_account_sessions_data", return_value=SESSIONS):
            return asyncio.run(wx.auto_rotate_account_by_id())

    def test_no_browser_page_rotates_via_cached_session(self):
        # 容器重启后没有打开的后台页面：之前会直接放弃轮换
        wx = Wx.__new__(Wx)
        wx.controller = None
        wx.switch_account = AsyncMock(return_value=True)
        self.assertTrue(self._rotate(wx, "gh_a"))
        wx.switch_account.assert_awaited_once()
        self.assertEqual(wx.switch_account.await_args.args[0], "gh_b")

    def test_wraps_around_after_last_account(self):
        wx = Wx.__new__(Wx)
        wx.controller = None
        wx.switch_account = AsyncMock(return_value=True)
        self._rotate(wx, "gh_c")
        self.assertEqual(wx.switch_account.await_args.args[0], "gh_a")

    def test_unreadable_switch_list_falls_back_to_cache(self):
        wx = Wx.__new__(Wx)
        wx.controller = Mock(page=object(), is_page_valid=Mock(return_value=True))
        wx._open_account_switch_dialog = AsyncMock(side_effect=RuntimeError("logged out"))
        wx._close_account_switch_dialog = AsyncMock()
        wx.switch_account = AsyncMock(return_value=False)
        self.assertFalse(self._rotate(wx, "gh_b"))
        self.assertEqual(wx.switch_account.await_args.args[0], "gh_c")

    def test_single_valid_account_keeps_current(self):
        wx = Wx.__new__(Wx)
        wx.controller = None
        wx.switch_account = AsyncMock()
        only = {"gh_a": SESSIONS["gh_a"]}
        with patch("driver.token.get_account_status_data", return_value=_status("gh_a")), \
                patch("driver.token.get_account_sessions_data", return_value=only):
            self.assertTrue(asyncio.run(wx.auto_rotate_account_by_id()))
        wx.switch_account.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
