import os
import tempfile
import unittest
from unittest.mock import patch

from core import weread_rescan
from core.weread_rescan import consume_token, issue_token, verify_token


class RescanTokenTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "weread_rescan.json")
        self.secret = patch.object(weread_rescan, "_secret", return_value=b"test-secret")
        self.secret.start()

    def tearDown(self):
        self.secret.stop()
        self.dir.cleanup()

    def test_fresh_token_is_valid(self):
        token = issue_token(path=self.path)
        self.assertTrue(verify_token(token, path=self.path))

    def test_expired_token_is_rejected(self):
        token = issue_token(ttl=60, now=1000, path=self.path)
        self.assertTrue(verify_token(token, now=1059, path=self.path))
        self.assertFalse(verify_token(token, now=1060, path=self.path))

    def test_tampered_token_is_rejected(self):
        expires_at, nonce, signature = issue_token(path=self.path).split(".", 2)
        later = str(int(expires_at) + 3600)
        self.assertFalse(verify_token(f"{later}.{nonce}.{signature}", path=self.path))
        self.assertFalse(verify_token(f"{expires_at}.{nonce}.{'0' * 64}", path=self.path))
        self.assertFalse(verify_token("garbage", path=self.path))
        self.assertFalse(verify_token("", path=self.path))

    def test_token_signed_with_other_secret_is_rejected(self):
        token = issue_token(path=self.path)
        with patch.object(weread_rescan, "_secret", return_value=b"other-secret"):
            self.assertFalse(verify_token(token, path=self.path))

    def test_new_token_supersedes_old_link(self):
        old = issue_token(path=self.path)
        new = issue_token(path=self.path)
        self.assertFalse(verify_token(old, path=self.path))
        self.assertTrue(verify_token(new, path=self.path))

    def test_consumed_token_cannot_be_reused(self):
        token = issue_token(path=self.path)
        consume_token(token, path=self.path)
        self.assertFalse(verify_token(token, path=self.path))

    def test_consuming_stale_token_keeps_current_one(self):
        old = issue_token(path=self.path)
        new = issue_token(path=self.path)
        consume_token(old, path=self.path)
        self.assertTrue(verify_token(new, path=self.path))

    def test_missing_state_file_rejects_everything(self):
        token = issue_token(path=self.path)
        os.remove(self.path)
        self.assertFalse(verify_token(token, path=self.path))


if __name__ == "__main__":
    unittest.main()
