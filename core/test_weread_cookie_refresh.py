import sys
import types
import unittest
from unittest.mock import Mock, patch

try:
    import yaml
except ModuleNotFoundError:
    # These focused tests do not call YAML helpers; allow them to run when the
    # local checkout does not have the project's PyYAML dependency installed.
    yaml = types.ModuleType("yaml")
    sys.modules["yaml"] = yaml

from core.weread_cookie_refresh import (
    _ExpiredNoticeTracker,
    _browser_refresh_lock,
    refresh_in_container,
    _cookie_status,
    _extract_cookie_from_page,
    _save_cookie,
    _save_cookie_if_changed,
    _send_cookie_expired_notice,
)


class _Request:
    url = "https://weread.qq.com/web/mp/articles?bookId=test"
    headers = {"cookie": "wr_vid=123; wr_skey=before"}


class _Page:
    def on(self, event, callback):
        self._request_callback = callback

    def goto(self, url, wait_until=None, timeout=None):
        self._request_callback(_Request())


class _Context:
    def cookies(self, url):
        # Simulate Set-Cookie rotating the browser jar after the request was sent.
        return [
            {"name": "wr_vid", "value": "123"},
            {"name": "wr_skey", "value": "after"},
        ]


class WereadCookieRefreshTests(unittest.TestCase):
    def test_extract_prefers_post_navigation_cookie_jar(self):
        cookie = _extract_cookie_from_page(_Page(), _Context(), "https://example.test")
        self.assertEqual(cookie, "wr_vid=123; wr_skey=after")

    def test_save_only_when_cookie_values_change(self):
        current = {"cookie": "wr_vid=123; wr_skey=old"}
        with patch(
            "core.weread_cookie_refresh._load_weread_data",
            return_value=({}, current),
        ):
            with patch("core.weread_cookie_refresh._save_cookie") as save_cookie:
                unchanged = _save_cookie_if_changed(
                    "wr_skey=old; wr_vid=123",
                    "wr_vid=123; wr_skey=old",
                    lic_path="unused.wx.lic",
                )
                self.assertFalse(unchanged)
                save_cookie.assert_not_called()

                changed = _save_cookie_if_changed(
                    "wr_vid=123; wr_skey=new",
                    "wr_vid=123; wr_skey=old",
                    name="Reader",
                    lic_path="unused.wx.lic",
                )
                self.assertTrue(changed)
                save_cookie.assert_called_once_with(
                    "wr_vid=123; wr_skey=new",
                    name="Reader",
                    lic_path="unused.wx.lic",
                )

    def test_cookie_status_distinguishes_auth_failure_from_unknown_error(self):
        def status_for(status_code, payload=None, request_error=None):
            response = types.SimpleNamespace(
                status_code=status_code,
                json=Mock(return_value=payload),
            )
            requests = types.ModuleType("requests")
            requests.get = Mock(
                side_effect=request_error or None,
                return_value=response,
            )
            if request_error:
                requests.get.side_effect = request_error
            with patch.dict(sys.modules, {"requests": requests}):
                return _cookie_status("wr_vid=123; wr_skey=test")

        self.assertEqual(status_for(200, {"errCode": 0, "books": []}), "valid")
        self.assertEqual(status_for(200, {"errCode": "-2012"}), "invalid")
        self.assertEqual(status_for(401, {}), "invalid")
        self.assertEqual(
            status_for(200, {"errCode": -2041, "errMsg": "request blocked"}),
            "unknown",
        )
        self.assertEqual(
            status_for(200, {"errCode": -2041, "errMsg": "login expired"}),
            "invalid",
        )
        self.assertEqual(status_for(200, {"errCode": -9}), "unknown")
        self.assertEqual(status_for(503, {}), "unknown")
        self.assertEqual(
            status_for(0, request_error=TimeoutError("temporary network error")),
            "unknown",
        )

    def _notice_text(self, rescan_url):
        notice_module = types.ModuleType("jobs.notice")
        notice_module.sys_notice = Mock()
        rescan_module = types.ModuleType("core.weread_rescan")
        rescan_module.RESCAN_TTL_SECONDS = 24 * 3600
        rescan_module.issue_token = Mock(return_value="token")
        rescan_module.build_rescan_url = Mock(return_value=rescan_url)
        with patch.dict(sys.modules, {"jobs.notice": notice_module, "core.weread_rescan": rescan_module}):
            _send_cookie_expired_notice()
        notice_module.sys_notice.assert_called_once()
        return notice_module.sys_notice.call_args.kwargs

    def test_expired_cookie_notice_uses_system_notification_without_cookie_value(self):
        args = self._notice_text("")
        self.assertIn("微信读书 Cookie 已失效", args["title"])
        self.assertIn("重新扫码授权", args["text"])
        self.assertNotIn("wr_skey", args["text"])
        self.assertNotIn("](", args["text"])

    def test_expired_cookie_notice_includes_rescan_link(self):
        url = "https://rss.example.com/api/v1/wx/weread/rescan?t=token"
        args = self._notice_text(url)
        # 纯文本 + 裸网址：电脑端微信/企微不解析 <a> 标签，裸网址四端都可点击
        self.assertTrue(args["plain"])
        self.assertIn(f"\n{url}\n", args["text"])
        self.assertNotIn("<a ", args["text"])
        self.assertNotIn("](", args["text"])
        self.assertIn("24 小时内有效", args["text"])
        self.assertNotIn("wr_skey", args["text"])

    def test_does_not_overwrite_a_cookie_saved_during_browser_refresh(self):
        current = {"cookie": "wr_vid=123; wr_skey=scan-new"}
        with patch(
            "core.weread_cookie_refresh._load_weread_data",
            return_value=({}, current),
        ):
            with patch("core.weread_cookie_refresh._save_cookie") as save_cookie:
                changed = _save_cookie_if_changed(
                    "wr_vid=123; wr_skey=stale-browser-result",
                    "wr_vid=123; wr_skey=old",
                    lic_path="unused.wx.lic",
                )
                self.assertFalse(changed)
                save_cookie.assert_not_called()


class ExpiredNoticeTrackerTests(unittest.TestCase):
    def test_single_invalid_check_does_not_notify(self):
        tracker = _ExpiredNoticeTracker(threshold=2)
        self.assertFalse(tracker.record("invalid"))
        # 线上观察到的循环：失效一次后下一轮保活自行恢复
        self.assertFalse(tracker.record("valid"))
        self.assertFalse(tracker.record("invalid"))

    def test_consecutive_invalid_notifies_once_per_outage(self):
        tracker = _ExpiredNoticeTracker(threshold=2)
        self.assertFalse(tracker.record("invalid"))
        self.assertTrue(tracker.record("invalid"))
        tracker.mark_sent()
        self.assertFalse(tracker.record("invalid"))
        # 恢复后再次连续失效，可以重新提醒
        tracker.record("valid")
        self.assertFalse(tracker.record("invalid"))
        self.assertTrue(tracker.record("invalid"))

    def test_unknown_neither_counts_nor_resets(self):
        tracker = _ExpiredNoticeTracker(threshold=2)
        tracker.record("invalid")
        self.assertFalse(tracker.record("unknown"))
        self.assertTrue(tracker.record("invalid"))

    def test_failed_send_allows_retry_next_cycle(self):
        tracker = _ExpiredNoticeTracker(threshold=2)
        tracker.record("invalid")
        self.assertTrue(tracker.record("invalid"))
        # 未 mark_sent（发送失败），下一轮仍应提醒
        self.assertTrue(tracker.record("invalid"))


class RefreshInContainerTests(unittest.TestCase):
    def test_skips_without_refresh_url(self):
        with patch("core.weread_cookie_refresh._load_weread_data", return_value=({}, {})), \
                patch("core.weread_cookie_refresh.refresh_weread_cookie") as refresh:
            self.assertFalse(refresh_in_container(timeout_s=1))
            refresh.assert_not_called()

    def test_runs_headless_refresh_and_returns_result(self):
        data = {"cookie_refresh_url": "https://weread.qq.com/web/mp/reader/x"}
        with patch("core.weread_cookie_refresh._load_weread_data", return_value=({}, data)), \
                patch("core.weread_cookie_refresh.refresh_weread_cookie", return_value=True) as refresh:
            self.assertTrue(refresh_in_container(timeout_s=5))
            refresh.assert_called_once_with(
                verbose=True, headless_only=True, force_bundled=True, cooldown_hours=0,
            )

    def test_returns_false_when_browser_lock_is_busy(self):
        data = {"cookie_refresh_url": "https://weread.qq.com/web/mp/reader/x"}
        with patch("core.weread_cookie_refresh._load_weread_data", return_value=({}, data)), \
                patch("core.weread_cookie_refresh.refresh_weread_cookie") as refresh:
            with _browser_refresh_lock:
                self.assertFalse(refresh_in_container(timeout_s=0.1))
            refresh.assert_not_called()


class MultiAccountCookieSaveTests(unittest.TestCase):
    """wx.lic 迁移到 weread_data.accounts[] 后，保活结果必须写回对应账号。"""

    def _save(self, weread_data, cookie, name=""):
        doc = {"weread_data": weread_data, "other": "kept"}
        written = {}
        with patch("core.weread_cookie_refresh._read_lic", return_value=doc), \
                patch("core.weread_cookie_refresh._write_lic",
                      side_effect=lambda path, d: written.update(d)):
            _save_cookie(cookie, name=name, lic_path="unused.wx.lic")
        return written

    def test_updates_matching_account_instead_of_top_level(self):
        written = self._save({
            "cookie_refresh_url": "https://weread.qq.com/web/mp/reader/x",
            "accounts": [
                {"cookie": "wr_vid=1; wr_skey=old", "vid": "1", "name": "主号"},
                {"cookie": "wr_vid=2; wr_skey=b", "vid": "2", "name": "备用"},
            ],
        }, "wr_vid=1; wr_skey=new", name="ignored")
        data = written["weread_data"]
        self.assertNotIn("cookie", data)
        self.assertEqual(data["accounts"][0]["cookie"], "wr_vid=1; wr_skey=new")
        self.assertEqual(data["accounts"][0]["name"], "主号")
        self.assertEqual(data["accounts"][1]["cookie"], "wr_vid=2; wr_skey=b")
        self.assertIn("cookie_refresh_last_ts", data)
        self.assertEqual(written["other"], "kept")

    def test_appends_account_when_vid_is_new(self):
        written = self._save({
            "accounts": [{"cookie": "wr_vid=1; wr_skey=a", "vid": "1", "name": ""}],
        }, "wr_vid=3; wr_skey=c", name="新号")
        accounts = written["weread_data"]["accounts"]
        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[1], {"cookie": "wr_vid=3; wr_skey=c", "vid": "3", "name": "新号"})

    def test_legacy_single_account_format_still_writes_top_level(self):
        written = self._save({"cookie": "wr_vid=1; wr_skey=old", "vid": "1"},
                             "wr_vid=1; wr_skey=new")
        data = written["weread_data"]
        self.assertEqual(data["cookie"], "wr_vid=1; wr_skey=new")
        self.assertNotIn("accounts", data)

    def test_change_detection_compares_primary_account(self):
        current = {"accounts": [{"cookie": "wr_vid=1; wr_skey=old", "vid": "1"}]}
        with patch("core.weread_cookie_refresh._load_weread_data",
                   return_value=({}, current)), \
                patch("core.weread_cookie_refresh._save_cookie") as save_cookie:
            self.assertTrue(_save_cookie_if_changed(
                "wr_vid=1; wr_skey=new", "wr_vid=1; wr_skey=old",
                lic_path="unused.wx.lic",
            ))
            save_cookie.assert_called_once()


if __name__ == "__main__":
    unittest.main()
