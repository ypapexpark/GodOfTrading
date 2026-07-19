import json
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import pandas as pd

import config
import main
import trader
from scalping_engine import (
    ENGINE_VERSION,
    STRATEGY,
    _closed_bars,
    evaluate_live_permission,
    evaluate_scalp,
)


def _trend_frame(periods: int, start: str, freq: str, slope: float) -> pd.DataFrame:
    index = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    close = 100 + np.arange(periods) * slope + np.sin(np.arange(periods) / 4) * 0.1
    open_ = close - 0.15
    high = close + 0.35
    low = open_ - 0.35
    volume = np.full(periods, 100.0)
    volume[-1] = 140.0
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def _valid_frames() -> tuple[pd.DataFrame, pd.DataFrame, object]:
    d15 = _trend_frame(120, "2024-01-01", "15min", 0.10)
    ema20 = d15["close"].ewm(span=20, adjust=False).mean()
    d15.iloc[-4, d15.columns.get_loc("low")] = float(ema20.iloc[-4]) + 0.05
    d5 = _trend_frame(120, "2024-01-02", "5min", 0.05)
    d5.iloc[-1, d5.columns.get_loc("open")] = float(d5["close"].iloc[-1]) - 0.20
    now = d15.index[-1].to_pydatetime() + timedelta(minutes=16)
    return d15, d5, now


class ScalpingEngineTest(unittest.TestCase):
    def test_valid_closed_candle_pullback_builds_cost_aware_plan(self):
        d15, d5, now = _valid_frames()
        plan = evaluate_scalp(
            d15,
            d5,
            live_price=float(d15["close"].iloc[-1]),
            round_trip_cost=config.BYBIT_ROUND_TRIP_EXECUTION_COST,
            spread_pct=0.02,
            now=now,
        )

        self.assertTrue(plan.eligible, plan.reason)
        self.assertGreaterEqual(plan.score, 72)
        self.assertLessEqual(plan.stop_atr, 2.0)
        self.assertLessEqual(plan.required_win_rate, 0.47)
        self.assertEqual(sum(int(tp["pct"]) for tp in plan.tps), 100)

    def test_live_extension_is_not_chased(self):
        d15, d5, now = _valid_frames()
        base = float(d15["close"].iloc[-1])
        plan = evaluate_scalp(
            d15,
            d5,
            live_price=base * 1.03,
            round_trip_cost=config.BYBIT_ROUND_TRIP_EXECUTION_COST,
            now=now,
        )

        self.assertFalse(plan.eligible)
        self.assertIn("추격", plan.reason)

    def test_bear_trend_builds_symmetric_short_plan(self):
        d15 = _trend_frame(120, "2024-01-01", "15min", -0.10)
        d15[["open", "close"]] = d15[["close", "open"]].to_numpy()
        ema20 = d15["close"].ewm(span=20, adjust=False).mean()
        d15.iloc[-4, d15.columns.get_loc("high")] = float(ema20.iloc[-4]) - 0.05
        d5 = _trend_frame(120, "2024-01-02", "5min", -0.05)
        d5[["open", "close"]] = d5[["close", "open"]].to_numpy()
        now = d15.index[-1].to_pydatetime() + timedelta(minutes=16)

        plan = evaluate_scalp(
            d15,
            d5,
            live_price=float(d15["close"].iloc[-1]),
            round_trip_cost=config.BYBIT_ROUND_TRIP_EXECUTION_COST,
            now=now,
        )

        self.assertTrue(plan.eligible, plan.reason)
        self.assertEqual(plan.direction, "SHORT")
        self.assertGreater(plan.stop, plan.entry)
        self.assertTrue(all(tp["price"] < plan.entry for tp in plan.tps))

    def test_forming_bar_is_removed(self):
        frame = _trend_frame(10, "2024-01-01", "15min", 0.1)
        now = frame.index[-1].to_pydatetime() + timedelta(minutes=5)
        closed = _closed_bars(frame, "15m", now)
        self.assertEqual(len(closed), len(frame) - 1)

    def test_governor_never_borrows_legacy_performance(self):
        legacy_rows = [
            {
                "status": "win",
                "strategy": "EMA눌림목+돌파",
                "engine_version": "legacy-v6",
                "pnl_usd": 10.0,
            }
            for _ in range(30)
        ]
        with TemporaryDirectory() as tmp:
            Path(tmp, "trade_state.json").write_text(
                json.dumps({"trade_history": legacy_rows}), encoding="utf-8"
            )
            permission = evaluate_live_permission(root=Path(tmp), venue="bybit")

        self.assertTrue(permission.allow)
        self.assertEqual(permission.mode, "canary")
        self.assertEqual(permission.closed, 0)
        self.assertEqual(permission.account_risk_pct, 0.0025)

    def test_eight_current_version_losses_stop_live_orders(self):
        rows = [
            {
                "status": "loss",
                "strategy": STRATEGY,
                "engine_version": ENGINE_VERSION,
                "pnl_usd": -1.0,
                "est_sl_loss": 1.0,
            }
            for _ in range(8)
        ]
        with TemporaryDirectory() as tmp:
            Path(tmp, "trade_state.json").write_text(
                json.dumps({"trade_history": rows}), encoding="utf-8"
            )
            permission = evaluate_live_permission(root=Path(tmp), venue="bybit")

        self.assertFalse(permission.allow)
        self.assertEqual(permission.mode, "shadow")
        self.assertEqual(permission.closed, 8)

    def test_replacement_switch_disables_legacy_live_entries(self):
        self.assertTrue(config.SCALP_ENGINE_ENABLED)
        self.assertFalse(config.LEGACY_AUTO_TRADE_ENABLED)

    def test_primary_scan_fetches_only_engine_timeframes(self):
        frame = _trend_frame(120, "2024-01-01", "5min", 0.01)
        calls = []

        def fake_fetch(symbol, timeframe, limit):
            calls.append((symbol, timeframe, limit))
            return frame

        with patch.object(main, "fetch_ohlcv", side_effect=fake_fetch), patch.object(
            main, "_try_scalping_engine_trade"
        ) as try_trade:
            evaluated = main._run_s1_primary_scan(["BTC/USDT"], {})

        self.assertEqual(evaluated, 1)
        self.assertEqual([call[1] for call in calls], ["15m", "5m"])
        try_trade.assert_called_once()

    def test_cash_stop_risk_includes_execution_cost(self):
        loss = trader._planned_stop_loss_usd(2.0, 100.0, 99.0, 0.0017)
        self.assertAlmostEqual(loss, 2.34, places=8)


if __name__ == "__main__":
    unittest.main()
