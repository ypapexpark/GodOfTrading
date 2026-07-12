import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import publisher


class TelegramBackoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.original_path = publisher._BACKOFF_PATH
        publisher._BACKOFF_PATH = Path(self.tmp.name) / "telegram_backoff.json"
        publisher._backoff_route_reported.clear()
        self.addCleanup(setattr, publisher, "_BACKOFF_PATH", self.original_path)

    def test_retry_after_is_persisted_and_suppresses_next_request(self):
        response = Mock()
        response.json.return_value = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 120},
        }

        with patch.object(publisher.requests, "post", return_value=response) as post:
            self.assertFalse(publisher._post("secret-token", "chat", "first"))
            self.assertFalse(publisher._post("secret-token", "chat", "second"))

        self.assertEqual(post.call_count, 1)
        state = json.loads(publisher._BACKOFF_PATH.read_text(encoding="utf-8"))
        self.assertIn(publisher._token_key("secret-token"), state)
        self.assertNotIn("secret-token", publisher._BACKOFF_PATH.read_text(encoding="utf-8"))

    def test_expired_backoff_allows_request(self):
        publisher._BACKOFF_PATH.write_text(
            json.dumps({publisher._token_key("secret-token"): 1}),
            encoding="utf-8",
        )
        response = Mock()
        response.json.return_value = {"ok": True, "result": {}}

        with patch.object(publisher.requests, "post", return_value=response) as post:
            self.assertTrue(publisher._post("secret-token", "chat", "message"))

        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
