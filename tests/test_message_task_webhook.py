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


if __name__ == '__main__':
    unittest.main()
