import unittest
from unittest.mock import patch

import main


class BinanceSizingTest(unittest.TestCase):
    def test_margin_cap_compounds_with_equity(self):
        self.assertAlmostEqual(main._binance_margin_cap_usd(174.16027752), 17.416, places=3)
        self.assertEqual(main._binance_margin_cap_usd(1_000.0), 100.0)

    def test_margin_cap_rejects_nonpositive_balance(self):
        self.assertEqual(main._binance_margin_cap_usd(0.0), 0.0)

    def test_canary_open_position_override_blocks_fourth_position(self):
        snapshot = {
            "ok": True,
            "count": 3,
            "margin_used": 5.0,
            "long_margin": 5.0,
            "short_margin": 0.0,
            "sl_risk": 0.1,
            "equity": 100.0,
            "free": 95.0,
        }
        with patch(
            "trade_router.get_portfolio_risk_snapshot", return_value=snapshot
        ):
            _, _, _, block = main._apply_portfolio_capacity_gate(
                100.0, 0.02, 0.1, 10.0, "LONG",
                min_execution_margin_usd=1.0,
                max_open_positions_override=3,
            )
        self.assertIn("동시 포지션 안전한도 3개", block)


if __name__ == "__main__":
    unittest.main()
