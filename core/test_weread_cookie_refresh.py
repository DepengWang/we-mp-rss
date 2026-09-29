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
    _cookie_status,
    _extract_cookie_from_page,
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

    def test_expired_cookie_notice_uses_system_notification_without_cookie_value(self):
        notice_module = types.ModuleType("jobs.notice")
        notice_module.sys_notice = Mock()
        with patch.dict(sys.modules, {"jobs.notice": notice_module}):
            _send_cookie_expired_notice()

        notice_module.sys_notice.assert_called_once()
        args = notice_module.sys_notice.call_args.kwargs
        self.assertIn("微信读书 Cookie 已失效", args["title"])
        self.assertIn("重新扫码授权", args["text"])
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


if __name__ == "__main__":
    unittest.main()
