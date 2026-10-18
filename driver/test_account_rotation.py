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


class ExpiredSessionSkippingTest(unittest.TestCase):
    def test_expired_account_is_skipped_by_rotation(self):
        wx = Wx.__new__(Wx)
        wx.controller = None
        wx.switch_account = AsyncMock(return_value=True)
        status = _status("gh_a")
        status["accounts"][1]["token_status"] = "expired"  # gh_b 已确认失效
        with patch("driver.token.get_account_status_data", return_value=status), \
                patch("driver.token.get_account_sessions_data", return_value=SESSIONS):
            asyncio.run(wx.auto_rotate_account_by_id())
        self.assertEqual(wx.switch_account.await_args.args[0], "gh_c")

    def test_mark_expired_updates_session_and_status(self):
        from driver import token
        sessions = {k: dict(v) for k, v in SESSIONS.items()}
        status = _status("gh_a")
        saved = {}
        with patch.object(token, "get_account_sessions_data", return_value=sessions), \
                patch.object(token, "set_account_sessions_data", side_effect=lambda s: saved.update(sessions=s)), \
                patch.object(token, "get_account_status_data", return_value=status), \
                patch.object(token, "set_account_status_data", side_effect=lambda s: saved.update(status=s)):
            token.mark_account_session_expired("gh_b", "未登录")
        self.assertEqual(saved["sessions"]["gh_b"]["token_status"], "expired")
        self.assertNotIn("token_status", saved["sessions"]["gh_a"])
        rows = {r["account_id"]: r["token_status"] for r in saved["status"]["accounts"]}
        self.assertEqual(rows, {"gh_a": "valid", "gh_b": "expired", "gh_c": "valid"})


class RestoreFailureClassificationTest(unittest.TestCase):
    def _restore(self, page_name):
        from driver.wx import CachedSessionInvalid  # noqa: F401
        wx = Wx.__new__(Wx)
        page = Mock()
        page.context.clear_cookies = AsyncMock()
        page.goto = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        wx.controller = Mock(page=page, is_page_valid=Mock(return_value=True), add_cookies=AsyncMock())
        wx._extract_wechat_data = AsyncMock(return_value={"wx_app_name": page_name})
        wx.WX_HOME = "https://mp.weixin.qq.com/cgi-bin/home"
        with patch("driver.token.get_account_session_data", return_value=SESSIONS["gh_b"]):
            asyncio.run(wx._restore_cached_account_session("gh_b", "B"))

    def test_not_logged_in_page_is_confirmed_invalid(self):
        from driver.wx import CachedSessionInvalid
        with self.assertRaises(CachedSessionInvalid):
            self._restore("")

    def test_other_account_name_is_not_marked_invalid(self):
        from driver.wx import CachedSessionInvalid
        with self.assertRaises(RuntimeError) as ctx:
            self._restore("A")
        self.assertNotIsInstance(ctx.exception, CachedSessionInvalid)

    def test_expired_cache_is_confirmed_invalid(self):
        from driver.wx import CachedSessionInvalid
        wx = Wx.__new__(Wx)
        stale = dict(SESSIONS["gh_b"], expiry={"expiry_timestamp": time.time() - 1})
        with patch("driver.token.get_account_session_data", return_value=stale):
            with self.assertRaises(CachedSessionInvalid):
                asyncio.run(wx._restore_cached_account_session("gh_b", "B"))


if __name__ == "__main__":
    unittest.main()
