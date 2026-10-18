import os
import tempfile
import unittest
from unittest.mock import patch

from core.config import cfg

cfg.config["db"] = "sqlite:///" + os.path.join(tempfile.gettempdir(), "werss-webhook-test.db").replace("\\", "/")
# core.wx 导入时会 cfg.reload()；没有 config.yaml 时会丢掉上面的测试库配置。
cfg.reload = lambda: None

from jobs import mps
from jobs.mps import MessageTaskTracker


def _result(name, success=True, articles=None, status=None):
    articles = articles or []
    return {
        'mp_name': name,
        'success': success,
        'status': status or ('success' if success else 'failed'),
        'articles': articles,
        'article_count': len(articles),
        'message': '',
    }


def _articles(prefix, count):
    return [{'title': f'{prefix}-{i}'} for i in range(count)]


class BatchedWebhookTest(unittest.TestCase):
    def setUp(self):
        self.tracker = MessageTaskTracker()
        self.sent = []
        self.sleep_patch = patch.object(mps.time, 'sleep')
        self.sleep = self.sleep_patch.start()
        self.hook_patch = patch.object(mps, 'web_hook', side_effect=lambda hook: self.sent.append(hook))
        self.hook_patch.start()

    def tearDown(self):
        self.hook_patch.stop()
        self.sleep_patch.stop()

    def _send(self, results):
        return self.tracker._send_batched_webhooks('task', object(), results, {'new_articles': 0})

    def test_all_success_without_articles_still_sends_summary(self):
        self.assertTrue(self._send([_result('a'), _result('b')]))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual([r['mp_name'] for r in self.sent[0].results], ['a', 'b'])

    def test_batches_articles_and_keeps_all_statuses_in_summary(self):
        results = [
            _result('a', articles=_articles('a', 7)),
            _result('empty'),
            _result('bad', success=False),
            _result('limited', success=False, status='rate_limited'),
        ]
        self.assertTrue(self._send(results))
        # 7 篇 → 1 个满批 + 汇总（剩余 2 篇 + 3 个无文章订阅源）
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[0].summary['new_articles'], 5)
        final = self.sent[1]
        self.assertEqual(final.summary['new_articles'], 2)
        self.assertEqual([r['mp_name'] for r in final.results], ['a', 'empty', 'bad', 'limited'])
        self.assertEqual(final.results[0]['message'], 'a-5；a-6')
        # 批次之间有间隔，第一条不等待
        self.assertEqual(self.sleep.call_count, 1)

    def test_failed_batch_does_not_stop_remaining_messages(self):
        calls = []

        def flaky(hook):
            calls.append(hook)
            if len(calls) == 1:
                raise ValueError('errcode=45009')

        with patch.object(mps, 'web_hook', side_effect=flaky):
            ok = self._send([_result('a', articles=_articles('a', 10)), _result('empty')])
        self.assertFalse(ok)
        self.assertEqual(len(calls), 3)

    def test_oversized_summary_is_split_until_it_fits(self):
        from jobs.webhook import WebhookPayloadTooLarge
        delivered = []

        def limited(hook):
            # 模拟企微 4096 字节上限：超过 10 个订阅源的汇总被拒绝
            if len(hook.results) > 10:
                raise WebhookPayloadTooLarge('too large')
            delivered.append([r['mp_name'] for r in hook.results])

        results = [_result(f'mp{i}') for i in range(35)]
        with patch.object(mps, 'web_hook', side_effect=limited):
            ok = self._send(results)
        self.assertTrue(ok)
        # 35 → 17/18 → 8/9/9/9，每个订阅源恰好出现一次且顺序不变
        self.assertEqual([name for chunk in delivered for name in chunk], [f'mp{i}' for i in range(35)])
        self.assertTrue(all(len(chunk) <= 10 for chunk in delivered))
        # 每两条实际发出的消息之间都要留间隔
        self.assertGreaterEqual(self.sleep.call_count, len(delivered) - 1)

    def test_single_oversized_result_is_reported_as_failure(self):
        from jobs.webhook import WebhookPayloadTooLarge
        with patch.object(mps, 'web_hook', side_effect=WebhookPayloadTooLarge('too large')):
            self.assertFalse(self._send([_result('a')]))


class BatchWereadAuthTest(unittest.TestCase):
    def test_valid_token_skips_refresh(self):
        with patch.object(mps, '_check_weread_auth', return_value=('valid', 'ok')), \
                patch('core.weread_cookie_refresh.refresh_in_container') as refresh:
            self.assertEqual(mps._resolve_batch_weread_auth(), ('valid', 'ok'))
            refresh.assert_not_called()

    def test_invalid_token_is_rechecked_after_refresh(self):
        checks = iter([('invalid', 'expired'), ('valid', 'ok')])
        with patch.object(mps, '_check_weread_auth', side_effect=lambda: next(checks)), \
                patch.object(mps, '_in_event_loop', return_value=False), \
                patch('core.weread_cookie_refresh.refresh_in_container', return_value=True):
            self.assertEqual(mps._resolve_batch_weread_auth(), ('valid', 'ok'))

    def test_failed_refresh_keeps_invalid(self):
        with patch.object(mps, '_check_weread_auth', return_value=('invalid', 'expired')) as check, \
                patch.object(mps, '_in_event_loop', return_value=False), \
                patch('core.weread_cookie_refresh.refresh_in_container', return_value=False):
            self.assertEqual(mps._resolve_batch_weread_auth(), ('invalid', 'expired'))
            self.assertEqual(check.call_count, 1)

    def test_manual_api_trigger_does_not_launch_browser(self):
        with patch.object(mps, '_check_weread_auth', return_value=('invalid', 'expired')), \
                patch.object(mps, '_in_event_loop', return_value=True), \
                patch('core.weread_cookie_refresh.refresh_in_container') as refresh:
            self.assertEqual(mps._resolve_batch_weread_auth(), ('invalid', 'expired'))
            refresh.assert_not_called()


class WecomPayloadLimitTest(unittest.TestCase):
    def test_web_hook_keeps_payload_too_large_type(self):
        from jobs import webhook
        from jobs.webhook import MessageWebHook, WebhookPayloadTooLarge

        task = type('Task', (), {'message_type': 1})()
        with patch.object(webhook, 'call_webhook', side_effect=WebhookPayloadTooLarge('x')):
            with self.assertRaises(WebhookPayloadTooLarge):
                webhook.web_hook(MessageWebHook(task=task, results=[_result('a')]))


if __name__ == '__main__':
    unittest.main()
