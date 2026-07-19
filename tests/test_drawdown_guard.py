import unittest
from unittest.mock import patch

import config
import trader


class TimedDrawdownGuardTest(unittest.TestCase):
    def _state(self):
        return {
            "equity_start": 100.0,
            "equity_peak": 100.0,
            "drawdown_guard_peak": 100.0,
            "drawdown_status": "normal",
            "pause_until": 0,
        }

    def test_hard_stop_is_fixed_four_hours_and_does_not_extend(self):
        state = self._state()
        self.assertEqual(config.DRAWDOWN_PAUSE_HOURS, 4)

        with patch.object(trader, "_env_float", return_value=0.0), patch.object(
            trader.time, "time", return_value=1_000.0
        ):
            allowed, reason = trader._apply_drawdown_guard(state, 81.0)

        self.assertFalse(allowed)
        self.assertIn("고정 4시간", reason)
        self.assertEqual(state["hard_stop_started_ts"], 1_000.0)
        self.assertEqual(state["hard_stop_until"], 15_400.0)

        # Equity가 먼저 회복돼도 최초 4시간은 지키며 종료시각은 연장하지 않는다.
        with patch.object(trader, "_env_float", return_value=0.0), patch.object(
            trader.time, "time", return_value=4_600.0
        ):
            allowed, reason = trader._apply_drawdown_guard(state, 90.0)

        self.assertFalse(allowed)
        self.assertIn("자동재개", reason)
        self.assertEqual(state["hard_stop_until"], 15_400.0)

    def test_four_hour_expiry_always_restarts_and_rebases_execution_peak(self):
        state = self._state()
        with patch.object(trader, "_env_float", return_value=0.0), patch.object(
            trader.time, "time", return_value=1_000.0
        ):
            allowed, _ = trader._apply_drawdown_guard(state, 81.0)
        self.assertFalse(allowed)

        # 4시간 뒤 DD가 더 커졌어도 무조건 재개한다.
        with patch.object(trader, "_env_float", return_value=0.0), patch.object(
            trader.time, "time", return_value=15_400.0
        ):
            allowed, reason = trader._apply_drawdown_guard(state, 78.0)

        self.assertTrue(allowed)
        self.assertIn("자동재개", reason)
        self.assertEqual(state["drawdown_guard_peak"], 78.0)
        self.assertEqual(state["drawdown_pct"], 0.0)
        self.assertEqual(state["all_time_drawdown_pct"], 22.0)
        self.assertEqual(state["max_drawdown_pct"], 22.0)
        self.assertEqual(state["hard_stop_until"], 0)
        self.assertEqual(state["hard_stop_resume_count"], 1)

        # 다음 검사에서 과거 최고점 때문에 즉시 재정지되지 않는다.
        with patch.object(trader, "_env_float", return_value=0.0), patch.object(
            trader.time, "time", return_value=15_401.0
        ):
            allowed, _ = trader._apply_drawdown_guard(state, 77.0)

        self.assertTrue(allowed)
        self.assertEqual(state["drawdown_status"], "normal")
        self.assertAlmostEqual(state["drawdown_pct"], 1.28, places=2)

    def test_new_cycle_can_trigger_another_fixed_stop_after_another_eighteen_pct(self):
        state = self._state()
        with patch.object(trader, "_env_float", return_value=0.0), patch.object(
            trader.time, "time", return_value=1_000.0
        ):
            trader._apply_drawdown_guard(state, 81.0)
        with patch.object(trader, "_env_float", return_value=0.0), patch.object(
            trader.time, "time", return_value=15_400.0
        ):
            trader._apply_drawdown_guard(state, 78.0)

        with patch.object(trader, "_env_float", return_value=0.0), patch.object(
            trader.time, "time", return_value=16_000.0
        ):
            allowed, reason = trader._apply_drawdown_guard(state, 63.9)

        self.assertFalse(allowed)
        self.assertIn("18%", reason)
        self.assertEqual(state["hard_stop_started_ts"], 16_000.0)
        self.assertEqual(state["hard_stop_until"], 30_400.0)


if __name__ == "__main__":
    unittest.main()
