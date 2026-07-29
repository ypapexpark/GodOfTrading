import unittest
from unittest.mock import patch

import config
import trader


class TelegramPositionOnlyPolicyTest(unittest.TestCase):
    def test_live_position_only_flags(self):
        self.assertTrue(config.TELEGRAM_LIVE_POSITION_ONLY)
        self.assertFalse(config.AUTO_TRADE_DIAGNOSTICS)
        self.assertFalse(config.TELEGRAM_PERIODIC_REPORT_ENABLED)
        self.assertFalse(config.TELEGRAM_RESEARCH_SIGNALS_ENABLED)

    def test_close_notification_has_win_loss_analysis(self):
        record = {
            "num": 99,
            "venue": "bybit",
            "symbol": "BTC/USDT",
            "direction": "LONG",
            "status": "win",
            "pnl_usd": 1.25,
            "entry_price": 100.0,
            "exit_price": 101.5,
            "sl": 99.0,
            "leverage": 5,
            "qty": 1.0,
            "tf": "15m",
            "strategy": "EMA눌림목+거래량급등",
            "exit_reason": "TP 부분익절 후 잔량 보호청산",
            "entry_reasons": ["15m EMA 눌림 + 거래량 급등", "MTF 정렬"],
            "est_sl_loss": 1.0,
            "tps": [{"price": 101.0, "pct": 40, "rr": 1.0}],
            "postmortem": {
                "headline": "승리 원인 후보",
                "r_multiple": 1.25,
                "hold_minutes": 42,
                "primary_hypothesis": {"text": "추세 정렬 구간에서 TP 도달"},
                "causes": [
                    {"text": "EMA 방향 일치"},
                    {"text": "분할 익절 경로 유효"},
                ],
                "lessons": ["동일 조건 주력 유지"],
            },
        }
        msg = trader.build_trade_close_notification(record)
        self.assertIn("포지션 청산", msg)
        self.assertIn("WIN 승", msg)
        self.assertIn("성공 이유 분석", msg)
        self.assertIn("추세 정렬", msg)
        self.assertIn("다음에 살릴 점", msg)

    def test_loss_notification_has_failure_analysis(self):
        record = {
            "num": 100,
            "venue": "bybit",
            "symbol": "ETH/USDT",
            "direction": "LONG",
            "status": "loss",
            "pnl_usd": -0.8,
            "entry_price": 2000.0,
            "exit_price": 1980.0,
            "sl": 1980.0,
            "leverage": 5,
            "qty": 0.1,
            "tf": "15m",
            "strategy": "EMA눌림목+돌파",
            "exit_reason": "SL 손절",
            "entry_reasons": ["돌파 확인"],
            "est_sl_loss": 0.8,
            "tps": [{"price": 2020.0, "pct": 40}],
        }
        msg = trader.build_trade_close_notification(record)
        self.assertIn("LOSS 패", msg)
        self.assertIn("실패 이유 분석", msg)

    def test_entry_notification_mentions_pair_policy(self):
        msg = trader.build_trade_notification(
            "BTC/USDT", "LONG", 5, 0.01, 100.0, 99.0,
            [{"price": 101.0, "pct": 40, "rr": 1.0}],
            60.0,
            tf_key="15m",
            strength="STRONG",
            strategy="EMA눌림목+거래량급등",
            trade_num=1,
            reasons=["테스트 근거"],
        )
        self.assertIn("포지션 진입", msg)
        self.assertIn("진입 근거", msg)
        self.assertIn("청산 시", msg)

    def test_notify_block_never_sends_under_position_only(self):
        with patch.object(trader, "log_trade_candidate"), patch(
            "publisher.send_signal"
        ) as send_sig:
            trader.notify_trade_block(
                "BTC/USDT", "15m", "LONG", "STRONG", "테스트 차단",
                strategy="EMA눌림목+돌파",
                send_telegram=True,
            )
        send_sig.assert_not_called()


if __name__ == "__main__":
    unittest.main()
