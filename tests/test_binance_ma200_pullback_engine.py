import unittest
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import pandas as pd

from binance_ma200_pullback_engine import (
    ENGINE_VERSION,
    STRATEGY,
    detect_ma200_volume_breakout,
    evaluate_ma200_pullback_entry,
)
from binance_divergence_engine import evaluate_live_permission
import main


def _four_hour_pattern(breakout_volume: float = 320.0) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=260, freq="4h", tz="UTC")
    close = np.full(260, 100.0)
    close[238:250] = 99.0
    close[250] = 100.5
    close[251:258] = [101.0, 101.5, 102.0, 102.4, 102.7, 102.9, 103.0]
    close[258:] = [101.0, 100.1]
    open_ = close - 0.05
    open_[250] = 99.0
    high = np.maximum(open_, close) + 1.0
    low = np.minimum(open_, close) - 1.0
    volume = np.full(260, 100.0)
    volume[250] = breakout_volume
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def _reversal_frame(periods: int, freq: str) -> pd.DataFrame:
    index = pd.date_range("2026-03-01", periods=periods, freq=freq, tz="UTC")
    close = np.full(periods, 99.9)
    close[-3:] = [99.85, 99.95, 100.1]
    open_ = close + 0.02
    open_[-3:] = [99.9, 99.88, 99.92]
    high = np.maximum(open_, close) + 0.15
    low = np.minimum(open_, close) - 0.15
    volume = np.full(periods, 100.0)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def _reversal_frame_at(periods: int, freq: str, price: float) -> pd.DataFrame:
    frame = _reversal_frame(periods, freq)
    return frame + {
        "open": price - 99.9,
        "high": price - 99.9,
        "low": price - 99.9,
        "close": price - 99.9,
        "volume": 0.0,
    }


class BinanceMA200PullbackEngineTest(unittest.TestCase):
    def test_detects_below_ma_basing_and_volume_breakout(self):
        d4 = _four_hour_pattern()
        now = d4.index[-1].to_pydatetime() + timedelta(hours=5)

        setup = detect_ma200_volume_breakout(d4, now=now)

        self.assertTrue(setup.eligible, setup.reason)
        self.assertEqual(setup.breakout_index, 250)
        self.assertEqual(setup.bars_since_breakout, 9)
        self.assertGreaterEqual(setup.below_ma_ratio, 0.75)
        self.assertGreater(setup.breakout_volume_ratio, 3.0)

    def test_rejects_same_cross_without_volume_expansion(self):
        d4 = _four_hour_pattern(breakout_volume=120.0)
        now = d4.index[-1].to_pydatetime() + timedelta(hours=5)

        setup = detect_ma200_volume_breakout(d4, now=now)

        self.assertFalse(setup.eligible)
        self.assertIn("거래량 종가돌파 없음", setup.reason)

    def test_ma200_retest_with_lower_timeframe_reversal_builds_long_plan(self):
        d4 = _four_hour_pattern()
        d15 = _reversal_frame(100, "15min")
        d5 = _reversal_frame(100, "5min")
        now = d4.index[-1].to_pydatetime() + timedelta(hours=5)
        setup = detect_ma200_volume_breakout(d4, now=now)

        plan = evaluate_ma200_pullback_entry(
            d4,
            d15,
            d5,
            setup=setup,
            live_price=100.1,
            quote_volume_usd=10_000_000,
            min_quote_volume_usd=5_000_000,
            now=now,
        )

        self.assertTrue(plan.eligible, plan.reason)
        self.assertEqual(plan.direction, "LONG")
        self.assertEqual(plan.signal_tier, "PB")
        self.assertEqual(plan.metrics["zone"], "ma200_retest")
        self.assertLess(plan.stop, plan.entry)
        self.assertEqual([tp["pct"] for tp in plan.tps], [30, 70])
        self.assertLessEqual(plan.required_win_rate, 0.42)

    def test_bollinger_middle_retest_builds_long_plan(self):
        d4 = _four_hour_pattern()
        now = d4.index[-1].to_pydatetime() + timedelta(hours=5)
        setup = detect_ma200_volume_breakout(d4, now=now)
        middle = float(d4["close"].rolling(20).mean().iloc[-1])

        plan = evaluate_ma200_pullback_entry(
            d4,
            _reversal_frame_at(100, "15min", middle),
            _reversal_frame_at(100, "5min", middle),
            setup=setup,
            live_price=middle,
            quote_volume_usd=10_000_000,
            min_quote_volume_usd=5_000_000,
            round_trip_cost=0.0,
            now=now,
        )

        self.assertTrue(plan.eligible, plan.reason)
        self.assertEqual(plan.metrics["zone"], "bb_middle_retest")
        self.assertAlmostEqual(plan.metrics["bb_middle_4h"], middle)
        self.assertIn("볼린저 중단", plan.reason)

    def test_old_bollinger_lower_only_zone_is_not_an_entry(self):
        d4 = _four_hour_pattern()
        now = d4.index[-1].to_pydatetime() + timedelta(hours=5)
        setup = detect_ma200_volume_breakout(d4, now=now)
        close = d4["close"].astype(float)
        middle = close.rolling(20).mean()
        lower = middle - 2.0 * close.rolling(20).std(ddof=0)
        lower_price = float(lower.iloc[-1])

        plan = evaluate_ma200_pullback_entry(
            d4,
            _reversal_frame_at(100, "15min", lower_price),
            _reversal_frame_at(100, "5min", lower_price),
            setup=setup,
            live_price=lower_price,
            now=now,
        )

        self.assertFalse(plan.eligible)
        self.assertNotEqual((plan.metrics or {}).get("zone"), "bb_lower_retest")

    def test_zone_touch_without_reversal_does_not_buy_falling_candle(self):
        d4 = _four_hour_pattern()
        d15 = _reversal_frame(100, "15min")
        d5 = _reversal_frame(100, "5min")
        for frame in (d15, d5):
            frame.iloc[-3:, frame.columns.get_loc("open")] = 100.2
            frame.iloc[-3:, frame.columns.get_loc("close")] = [100.0, 99.9, 99.8]
            frame.iloc[-3:, frame.columns.get_loc("high")] = 100.3
            frame.iloc[-3:, frame.columns.get_loc("low")] = 99.7
        now = d4.index[-1].to_pydatetime() + timedelta(hours=5)
        setup = detect_ma200_volume_breakout(d4, now=now)

        plan = evaluate_ma200_pullback_entry(
            d4, d15, d5, setup=setup, live_price=100.1, now=now
        )

        self.assertFalse(plan.eligible)
        self.assertIn("반등 미확인", plan.reason)

    def test_d3_live_permission_does_not_borrow_d2_results(self):
        with TemporaryDirectory() as tmp:
            Path(tmp, "trade_state_binance.json").write_text(
                json.dumps(
                    {
                        "trade_history": [
                            {
                                "status": "loss",
                                "strategy": "D2_DIVERGENCE_VOLUME_ASYMMETRIC",
                                "engine_version": "other",
                                "pnl_usd": -1,
                            }
                            for _ in range(20)
                        ]
                    }
                ),
                encoding="utf-8",
            )
            permission = evaluate_live_permission(
                root=Path(tmp),
                strategy=STRATEGY,
                engine_version=ENGINE_VERSION,
                engine_label="D3",
            )

        self.assertTrue(permission.allow)
        self.assertEqual(permission.mode, "fixed")
        self.assertEqual(permission.account_risk_pct, 0.0025)
        self.assertEqual(permission.closed, 0)

    def test_d3_wrapper_routes_separate_strategy_and_state_namespace(self):
        plan = evaluate_ma200_pullback_entry(
            _four_hour_pattern(),
            _reversal_frame(100, "15min"),
            _reversal_frame(100, "5min"),
            setup=detect_ma200_volume_breakout(
                _four_hour_pattern(),
                now=_four_hour_pattern().index[-1].to_pydatetime() + timedelta(hours=5),
            ),
            live_price=100.1,
            now=_four_hour_pattern().index[-1].to_pydatetime() + timedelta(hours=5),
        )
        with patch("main._try_binance_d2_trade") as routed:
            main._try_binance_ma200_pullback_trade("XEC/USDT", plan)

        kwargs = routed.call_args.kwargs
        self.assertEqual(kwargs["engine_code"], "D3")
        self.assertEqual(kwargs["strategy"], STRATEGY)
        self.assertEqual(kwargs["engine_state_key"], "binance_ma200_pullback_engine")
        self.assertFalse(kwargs["is_divergence"])


if __name__ == "__main__":
    unittest.main()
