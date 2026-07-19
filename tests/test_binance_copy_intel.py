import time
import unittest
from unittest.mock import patch

import binance_copy_intel as intel


def _leader_metrics(**overrides):
    row = {
        "runtime_days": 180.0,
        "roi_pct": 25.0,
        "pnl_usdt": 1_000.0,
        "mdd_pct": 8.0,
        "copier_pnl_usdt": 500.0,
        "sharpe": 2.0,
        "winning_day_ratio": 0.70,
    }
    row.update(overrides)
    return row


class BinanceCopyIntelTest(unittest.TestCase):
    def test_history_flags_high_winrate_large_loss_martingale(self):
        now = int(time.time() * 1000)
        rows = []
        for index in range(20):
            pnl = 1.0 if index < 18 else -12.0
            rows.append({
                "status": "All Closed",
                "closingPnl": str(pnl),
                "leverage": "3",
                "opened": now + index * 10_000,
                "closed": now + index * 10_000 + 5_000,
                "avgCost": "100",
                "maxOpenInterest": "1",
                "symbol": "BTCUSDT",
            })
        result = intel.analyze_position_history(rows)
        self.assertTrue(result["martingale_suspected"])
        self.assertIn("high_winrate_large_losses", result["martingale_reasons"])
        self.assertLess(result["without_largest_win_usdt"], 0)

    def test_clean_leader_reaches_shadow_stage(self):
        history = {
            "closed_positions": 100,
            "profit_factor": 1.6,
            "without_largest_win_usdt": 200.0,
            "martingale_suspected": False,
            "high_leverage_ratio": 0.0,
            "symbol_profit_concentration": 0.4,
        }
        score, stage, reasons = intel.score_leader(
            _leader_metrics(), _leader_metrics(), history
        )
        self.assertEqual(stage, "shadow")
        self.assertEqual(reasons, [])
        self.assertGreater(score, 70)

    def test_negative_copier_pnl_is_rejected_even_if_leader_roi_is_high(self):
        history = {
            "closed_positions": 100,
            "profit_factor": 2.0,
            "without_largest_win_usdt": 300.0,
            "martingale_suspected": False,
            "high_leverage_ratio": 0.0,
            "symbol_profit_concentration": 0.2,
        }
        _, stage, reasons = intel.score_leader(
            _leader_metrics(copier_pnl_usdt=-50), _leader_metrics(), history
        )
        self.assertEqual(stage, "reject")
        self.assertIn("30d_copier_pnl<=0", reasons)

    def test_grid_scoring_blocks_high_leverage_futures(self):
        row = {
            "strategyId": 1,
            "strategyType": 2,
            "symbol": "ETHUSDT",
            "roi": "50",
            "pnl": "100",
            "runningTime": 90 * 86400,
            "sevenDayMdd": "5",
            "copyCount": 10,
            "matchedCount": 500,
            "minInvestment": "50",
            "direction": 1,
            "strategyParams": {
                "type": "ARITH",
                "lowerLimit": "1500",
                "upperLimit": "2500",
                "gridCount": 50,
                "leverage": 20,
                "stopLowerLimit": "1400",
            },
        }
        result = intel.score_grid_strategy(row, current_price=2000, seed=1000)
        self.assertEqual(result["stage"], "reject")
        self.assertIn("leverage>5x", result["reasons"])

    def test_grid_scoring_keeps_viable_spot_strategy_on_watch(self):
        row = {
            "strategyId": 2,
            "strategyType": 1,
            "symbol": "BTCUSDT",
            "roi": "15",
            "pnl": "200",
            "runningTime": 120 * 86400,
            "sevenDayMdd": "4",
            "copyCount": 20,
            "matchedCount": 800,
            "minInvestment": "100",
            "direction": 0,
            "strategyParams": {
                "type": "ARITH",
                "lowerLimit": "80000",
                "upperLimit": "120000",
                "gridCount": 20,
            },
        }
        result = intel.score_grid_strategy(row, current_price=100000, seed=1000)
        self.assertEqual(result["stage"], "watch")
        self.assertGreater(result["net_grid_edge_pct"], 0)

    def test_fill_poll_baselines_then_opens_and_closes_shadow(self):
        with patch.object(intel, "PER_LEADER_ALLOCATION_PCT", 0.20), patch.object(
            intel, "TRACKED_LEADERS", 3
        ):
            state = intel._default_state(200.0, "test")
            leader = {
                "portfolio_id": "p1",
                "nickname": "leader",
                "score": 90,
                "detail": {"margin_balance_usdt": 1000.0},
            }
            baseline = {
                "time": 1,
                "symbol": "BTCUSDT",
                "side": "BUY",
                "price": "90",
                "qty": "0.1",
                "quantity": "9",
                "positionSide": "LONG",
                "fee": "-0.01",
                "realizedProfit": "0",
            }
            self.assertEqual(
                intel.process_leader_fills(state, leader, [baseline], lambda _: 100.0), []
            )
            opening = {
                "time": 2,
                "symbol": "BTCUSDT",
                "side": "BUY",
                "price": "100",
                "qty": "1",
                "quantity": "1000",
                "positionSide": "LONG",
                "fee": "-0.5",
                "realizedProfit": "0",
            }
            events = intel.process_leader_fills(
                state, leader, [baseline, opening], lambda _: 100.0
            )
            self.assertEqual(events[0]["action"], "OPEN")
            self.assertEqual(len(state["shadow"]["positions"]), 1)

            closing = {
                "time": 3,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "price": "110",
                "qty": "1",
                "quantity": "1100",
                "positionSide": "LONG",
                "fee": "-0.55",
                "realizedProfit": "100",
            }
            events = intel.process_leader_fills(
                state, leader, [baseline, opening, closing], lambda _: 110.0
            )
            self.assertEqual(events[0]["action"], "CLOSE")
            self.assertGreater(events[0]["net_pnl_usdt"], 0)
            self.assertEqual(state["shadow"]["positions"], {})

    def test_failed_report_stays_due_and_retries_after_backoff(self):
        now = 1_000_000.0
        state = intel._default_state(200.0, "test")
        state["last_report_ts"] = now - intel.REPORT_INTERVAL_SECONDS - 1

        with patch.object(intel, "build_report", return_value="report"), patch.object(
            intel, "send_signal", side_effect=[False, True]
        ) as send:
            self.assertFalse(intel._maybe_send_periodic_report(state, now))
            self.assertNotEqual(state["last_report_ts"], now)
            self.assertFalse(
                intel._maybe_send_periodic_report(
                    state, now + intel.REPORT_RETRY_SECONDS - 1
                )
            )
            self.assertTrue(
                intel._maybe_send_periodic_report(
                    state, now + intel.REPORT_RETRY_SECONDS
                )
            )

        self.assertEqual(send.call_count, 2)
        self.assertEqual(
            state["last_report_ts"], now + intel.REPORT_RETRY_SECONDS
        )


if __name__ == "__main__":
    unittest.main()
