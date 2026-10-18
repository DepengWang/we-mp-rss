import json
import unittest
from unittest.mock import patch

from core.notice import notice
from core.notice.wechat import send_wechat_message

WECOM = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test"


class WechatTextMessageTest(unittest.TestCase):
    def _payload(self, post):
        return json.loads(post.call_args.kwargs["data"])

    @patch("core.notice.wechat.requests.post")
    def test_default_is_markdown(self, post):
        send_wechat_message(WECOM, "t", "### hi")
        self.assertEqual(self._payload(post), {"msgtype": "markdown", "markdown": {"content": "### hi"}})

    @patch("core.notice.wechat.requests.post")
    def test_text_message_for_wechat_viewing(self, post):
        send_wechat_message(WECOM, "t", "点击 https://rss.example.com/x", msgtype="text")
        self.assertEqual(self._payload(post), {"msgtype": "text", "text": {"content": "点击 https://rss.example.com/x"}})

    @patch("core.notice.wechat.requests.post")
    def test_text_is_truncated_to_2048_bytes_without_breaking_utf8(self, post):
        send_wechat_message(WECOM, "t", "中" * 1000, msgtype="text")
        content = self._payload(post)["text"]["content"]
        self.assertLessEqual(len(content.encode("utf-8")), 2048)
        self.assertEqual(set(content), {"中"})

    @patch("core.notice.wechat.requests.post")
    def test_notice_routes_msgtype_to_wechat(self, post):
        notice(WECOM, "t", "plain", msgtype="text")
        self.assertEqual(self._payload(post)["msgtype"], "text")


class SysNoticePlainTest(unittest.TestCase):
    @patch("core.notice.notice")
    def test_plain_notice_has_no_markdown_heading(self, notice_mock):
        from jobs import notice as jobs_notice
        with patch.object(jobs_notice.cfg, "get", return_value={"wechat": WECOM}):
            jobs_notice.sys_notice(title="微信读书 Cookie 已失效", text="正文", plain=True)
        args, kwargs = notice_mock.call_args
        self.assertEqual(args[2], "【微信读书 Cookie 已失效】\n正文")
        self.assertEqual(kwargs["msgtype"], "text")

    @patch("core.notice.notice")
    def test_default_notice_stays_markdown(self, notice_mock):
        from jobs import notice as jobs_notice
        with patch.object(jobs_notice.cfg, "get", return_value={"wechat": WECOM}):
            jobs_notice.sys_notice(title="标题", text="正文", tag="系统通知")
        args, kwargs = notice_mock.call_args
        self.assertTrue(args[2].startswith("### 标题"))
        self.assertEqual(kwargs["msgtype"], "markdown")


if __name__ == "__main__":
    unittest.main()
