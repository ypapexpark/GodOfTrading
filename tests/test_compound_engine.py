import unittest
from unittest.mock import patch

import config
import trader


class CompoundEngineTest(unittest.TestCase):
    def test_compound_flags_enabled(self):
        self.assertTrue(config.COMPOUND_ENGINE_ENABLED)
        self.assertTrue(config.SCALP_COMPOUND_ENABLED)
        self.assertEqual(config.SCALP_COMPOUND_TP1_PCT, 40)
        self.assertLessEqual(config.COMPOUND_CORE_RISK_PCT, config.COMPOUND_CORE_RISK_MAX_PCT)
        self.assertLessEqual(config.COMPOUND_CORE_RISK_MAX_PCT, config.MAX_ACCOUNT_RISK_PCT)

    def test_drawdown_softens_risk(self):
        state = {
            "equity_start": 100.0,
            "equity_peak": 100.0,
            "drawdown_guard_peak": 100.0,
            "last_equity": 88.0,  # 12% DD
        }
        scale, notes = trader.get_compound_risk_scale(88.0, state=state)
        self.assertLessEqual(scale, config.COMPOUND_DD_HARD_MULT + 0.01)
        self.assertTrue(any("DD" in n for n in notes))

    def test_near_peak_growth_can_accelerate(self):
        state = {
            "equity_start": 50.0,
            "equity_peak": 100.0,
            "drawdown_guard_peak": 100.0,
            "last_equity": 100.0,
        }
        scale, notes = trader.get_compound_risk_scale(100.0, state=state)
        self.assertGreaterEqual(scale, 1.0)
        self.assertLessEqual(scale, config.COMPOUND_GROWTH_MULT_CAP)

    def test_position_pct_scales_with_equity(self):
        """Same risk% on larger equity → larger dollar risk (복리 본체)."""
        pct1, loss1 = trader.position_pct_for_risk(50.0, 5, 100.0, 98.0, 0.01, 0.5)
        pct2, loss2 = trader.position_pct_for_risk(100.0, 5, 100.0, 98.0, 0.01, 0.5)
        self.assertAlmostEqual(loss2 / loss1, 2.0, places=2)
        # pct of balance can be similar; dollar risk compounds
        self.assertGreater(loss2, loss1)


if __name__ == "__main__":
    unittest.main()
