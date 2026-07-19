import time
import unittest
from unittest.mock import patch

import binance_pump_paper_bot as pump


def make_klines(*, trigger=True):
    now_ms = int(time.time() * 1000)
    start = now_ms // 900_000 * 900_000 - 109 * 900_000
    rows = []
    price = 100.0
    for i in range(110):
        ts = start + i * 900_000
        open_price = price
        close = price * 1.0002
        high = max(open_price, close) * 1.002
        low = min(open_price, close) * 0.998
        volume = 1_000.0
        taker = 500.0
        if i == 105:
            close = 98.0
            open_price = close
        if i == 109 and trigger:
            open_price = 100.0
            close = 104.2
            high = 104.3
            low = 99.9
            volume = 5_000.0
            taker = 3_000.0
        price = close
        rows.append([
            ts, str(open_price), str(high), str(low), str(close), str(volume),
            ts + 899_999, str(close * volume), 100, str(taker), str(taker * close), "0",
        ])
    return rows


class PumpPaperTest(unittest.TestCase):
    def test_derivative_gate_uses_completed_hour_oi_and_taker_flow(self):
        hour_ms = pump.HOUR_MS
        oi_rows = [
            {
                "timestamp": i * hour_ms,
                "sumOpenInterestValue": str(100.0 + i),
            }
            for i in range(30)
        ]
        taker_rows = [
            {"timestamp": i * hour_ms, "buySellRatio": "1.08"}
            for i in range(30)
        ]

        features = pump.compute_derivative_features(oi_rows, taker_rows)

        self.assertIsNotNone(features)
        self.assertGreater(features["oi_change_6h_pct"], 0)
        self.assertGreater(features["oi_change_24h_pct"], 0)
        self.assertEqual([], pump.derivative_signal_reasons(features))

    def test_derivative_gate_fails_closed_on_falling_oi(self):
        hour_ms = pump.HOUR_MS
        oi_rows = [
            {
                "timestamp": i * hour_ms,
                "sumOpenInterestValue": str(200.0 - i),
            }
            for i in range(30)
        ]
        taker_rows = [
            {"timestamp": i * hour_ms, "buySellRatio": "1.10"}
            for i in range(30)
        ]

        features = pump.compute_derivative_features(oi_rows, taker_rows)

        self.assertIn("oi6h", pump.derivative_signal_reasons(features))
        self.assertIn("oi24h", pump.derivative_signal_reasons(features))

    def test_feature_gate_detects_research_pattern(self):
        rows = make_klines()
        features = pump.compute_features(rows, int(time.time() * 1000))
        self.assertIsNotNone(features)
        ticker = {"spread_pct": 0.02}
        self.assertEqual([], pump.signal_reasons(features, ticker))
        self.assertGreaterEqual(features["ret_15m_pct"], 1.5)
        self.assertGreaterEqual(features["taker_buy_ratio"], 0.52)

    def test_confirmation_opens_seed_scaled_paper_position(self):
        state = pump._default_state()
        features = pump.compute_features(make_klines(), int(time.time() * 1000))
        pending = pump._create_pending(
            "TESTUSDT", features, {"spread_pct": 0.02}, time.time()
        )
        ticker = {
            "last": pending["trigger"] * 1.001,
            "ask": pending["trigger"] * 1.001,
        }
        with patch.object(pump, "_journal"):
            position = pump._open_paper_position(state, pending, ticker, time.time())
        self.assertIsNotNone(position)
        self.assertLessEqual(position["notional"], state["bankroll"] * 0.15)
        self.assertGreaterEqual(position["initial_stop_pct"], 1.8)
        self.assertLessEqual(position["initial_stop_pct"], 3.0)

    def test_stop_settlement_includes_execution_cost(self):
        state = pump._default_state()
        now = time.time()
        position = {
            "id": "p1", "symbol": "TESTUSDT", "entry_ts": now - 120,
            "entry": 100.0, "notional": 100.0, "initial_stop_pct": 3.0,
            "stop": 97.0, "tp1": 104.0, "tp2": 108.0, "remaining": 1.0,
            "tp1_done": False, "tp2_done": False, "highest": 100.0,
            "mfe_pct": 0.0, "mae_pct": 0.0, "realized_gross_usd": 0.0,
            "fees_usd": 100.0 * pump.ONE_WAY_COST_RATE, "signal_mid": 100.0,
            "last_bar_ts": 0, "features": {}, "policy": pump.POLICY,
        }
        state["positions"] = {"p1": position}
        row = [int(now * 1000), "100", "101", "96", "97", "1", int(now * 1000) + 59_999]
        with patch.object(pump, "_journal"):
            settled = pump._apply_completed_bar(state, "p1", row, now)
        self.assertEqual("stop", settled["exit_reason"])
        self.assertAlmostEqual(-3.16, settled["net_pct"], places=6)
        self.assertNotIn("p1", state["positions"])

    def test_gap_chase_is_not_opened(self):
        state = pump._default_state()
        pending = {"symbol": "TESTUSDT", "trigger": 100.0}
        ticker = {"last": 101.0, "ask": 101.0}
        with patch.object(pump, "_journal"):
            result = pump._open_paper_position(state, pending, ticker, time.time())
        self.assertIsNone(result)
        self.assertFalse(state["positions"])

    def test_failed_report_stays_due_and_retries_after_backoff(self):
        now = 1_000_000.0
        state = pump._default_state()
        state["last_report_time"] = now - pump.REPORT_INTERVAL_SECONDS - 1

        with patch.object(pump, "build_report", return_value="report"), patch.object(
            pump, "send_signal", side_effect=[False, True]
        ) as send:
            self.assertFalse(pump._maybe_send_periodic_report(state, now))
            self.assertNotEqual(state["last_report_time"], now)
            self.assertFalse(
                pump._maybe_send_periodic_report(
                    state, now + pump.REPORT_RETRY_SECONDS - 1
                )
            )
            self.assertTrue(
                pump._maybe_send_periodic_report(
                    state, now + pump.REPORT_RETRY_SECONDS
                )
            )

        self.assertEqual(send.call_count, 2)
        self.assertEqual(
            state["last_report_time"], now + pump.REPORT_RETRY_SECONDS
        )


if __name__ == "__main__":
    unittest.main()
