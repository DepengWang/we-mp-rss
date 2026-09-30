import unittest

from driver.account_sessions import retain_recent_valid_sessions


def _session(account_id, authorized_at, last_active_at=None, expiry=20_000):
    return {
        "account_id": account_id,
        "token": f"token-{account_id}",
        "cookies": [{"name": "sid", "value": account_id}],
        "expiry": {"expiry_timestamp": expiry},
        "token_status": "valid",
        "authorized_at": authorized_at,
        "last_active_at": last_active_at or authorized_at,
    }


class RecentAccountSessionTests(unittest.TestCase):
    def test_keeps_only_three_newest_complete_unexpired_sessions(self):
        now = 10_000
        sessions = {
            "old": _session("old", 1, expiry=now - 1),
            "a": _session("a", 2),
            "b": _session("b", 3),
            "c": _session("c", 4),
            "d": _session("d", 5),
            "missing-cookie": {**_session("missing-cookie", 6), "cookies": []},
        }

        retained = retain_recent_valid_sessions(sessions, limit=3, now=now)

        self.assertEqual(list(retained), ["d", "c", "b"])

    def test_current_active_session_is_kept_as_second_after_next_scan(self):
        now = 10_000
        sessions = {
            "first": _session("first", 1, last_active_at=99),
            "second": _session("second", 2, last_active_at=2),
            "third": _session("third", 3, last_active_at=3),
            "new": _session("new", 100, last_active_at=100),
        }

        retained = retain_recent_valid_sessions(sessions, limit=3, now=now)

        self.assertEqual(list(retained), ["new", "first", "third"])
        self.assertNotIn("second", retained)

    def test_rejects_expired_or_incomplete_sessions(self):
        now = 10_000
        sessions = {
            "no-token": {**_session("no-token", 1), "token": ""},
            "expired": _session("expired", 2, expiry=now),
            "complete": _session("complete", 3),
        }

        retained = retain_recent_valid_sessions(sessions, now=now)

        self.assertEqual(list(retained), ["complete"])


if __name__ == "__main__":
    unittest.main()
