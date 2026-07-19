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
        self.original_dedupe_path = publisher._DEDUPE_PATH
        publisher._BACKOFF_PATH = Path(self.tmp.name) / "telegram_backoff.json"
        publisher._DEDUPE_PATH = Path(self.tmp.name) / "telegram_dedupe.json"
        publisher._backoff_route_reported.clear()
        self.addCleanup(setattr, publisher, "_BACKOFF_PATH", self.original_path)
        self.addCleanup(setattr, publisher, "_DEDUPE_PATH", self.original_dedupe_path)

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

    def test_request_exception_never_logs_bot_token(self):
        token = "very-secret-token"
        error = RuntimeError(f"failed https://api.telegram.org/bot{token}/sendMessage")
        with (
            patch.object(publisher.requests, "post", side_effect=error),
            patch("builtins.print") as output,
        ):
            self.assertFalse(publisher._post(token, "chat", "message"))
        rendered = " ".join(str(arg) for call in output.call_args_list for arg in call.args)
        self.assertNotIn(token, rendered)
        self.assertIn("<redacted>", rendered)

    def test_signal_once_suppresses_duplicate_key(self):
        with patch.object(publisher, "send_signal", return_value=True) as send:
            self.assertTrue(
                publisher.send_signal_once("first", dedupe_key="same", ttl_seconds=60)
            )
            self.assertFalse(
                publisher.send_signal_once("second", dedupe_key="same", ttl_seconds=60)
            )

        send.assert_called_once_with("first")

    def test_signal_once_releases_key_after_failed_send(self):
        with patch.object(publisher, "send_signal", side_effect=[False, True]) as send:
            self.assertFalse(
                publisher.send_signal_once("first", dedupe_key="retry", ttl_seconds=60)
            )
            self.assertTrue(
                publisher.send_signal_once("second", dedupe_key="retry", ttl_seconds=60)
            )

        self.assertEqual(send.call_count, 2)

    def test_bithumb_uses_only_dedicated_route(self):
        env = {
            "BITHUMB_BOT_TOKEN": "bithumb-token",
            "BITHUMB_CHAT_ID": "bithumb-chat",
            "TRADE_BOT_TOKEN": "trade-token",
            "TRADE_CHAT_ID": "trade-chat",
            "SIGNAL_BOT_TOKEN": "signal-token",
            "SIGNAL_CHAT_ID": "signal-chat",
        }
        with (
            patch.dict(publisher.os.environ, env, clear=True),
            patch.object(publisher, "_post", return_value=True) as post,
        ):
            self.assertTrue(publisher.send_bithumb("wallet alert"))

        post.assert_called_once_with("bithumb-token", "bithumb-chat", "wallet alert")

    def test_bithumb_missing_route_never_falls_back(self):
        env = {
            "TRADE_BOT_TOKEN": "trade-token",
            "TRADE_CHAT_ID": "trade-chat",
            "SIGNAL_BOT_TOKEN": "signal-token",
            "SIGNAL_CHAT_ID": "signal-chat",
        }
        with (
            patch.dict(publisher.os.environ, env, clear=True),
            patch.object(publisher, "_post", return_value=True) as post,
        ):
            self.assertFalse(publisher.send_bithumb("wallet alert"))

        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
