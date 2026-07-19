import unittest
from unittest.mock import patch

import analyzer


class AnalyzerLiveLearningGuardTest(unittest.TestCase):
    def test_candidate_quality_cannot_resize_or_block_live_trade(self):
        with patch.object(analyzer, "_candidate_eval_rows", side_effect=AssertionError):
            mult, notes = analyzer.get_signal_quality_adjustment(
                "BTC/USDT", "15m", "EMA눌림목+돌파", "LONG"
            )
        self.assertEqual((mult, notes), (1.0, []))

    def test_candidate_quality_cannot_raise_leverage(self):
        with patch.object(analyzer, "_candidate_eval_rows", side_effect=AssertionError):
            leverage, notes = analyzer.get_quality_leverage_adjustment(
                "BTC/USDT", "15m", "EMA눌림목+돌파", "LONG", 7
            )
        self.assertEqual((leverage, notes), (7, []))

    def test_corrupted_realized_ledger_is_not_used_live(self):
        with patch.object(analyzer, "_realized_quality_groups", side_effect=AssertionError):
            mult, notes = analyzer.get_realized_trade_adjustment(
                "BTC/USDT", "15m", "EMA눌림목+돌파", "LONG"
            )
        self.assertEqual((mult, notes), (1.0, []))


if __name__ == "__main__":
    unittest.main()
