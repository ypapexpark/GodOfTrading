import json
import time
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import pandas as pd

import config
import binance_trader
import fetcher
import main
import trader
from binance_divergence_engine import (
    ENGINE_VERSION,
    STRATEGY,
    DivergenceSetup,
    detect_divergence_setup,
    evaluate_divergence_entry,
    evaluate_live_permission,
    resample_ohlcv,
    select_multitimeframe_setup,
)


def _frame(periods: int, freq: str, slope: float = 0.0) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=periods, freq=freq, tz="UTC")
    close = 100 + np.arange(periods) * slope + np.sin(np.arange(periods) / 5) * 0.05
    open_ = close - np.sign(slope or 1) * 0.08
    high = np.maximum(open_, close) + 0.35
    low = np.minimum(open_, close) - 0.35
    volume = np.full(periods, 100.0)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


class BinanceD2EngineTest(unittest.TestCase):
    def test_three_of_four_regular_bullish_is_a_setup(self):
        frame = _frame(110, "15min")
        frame.iloc[90, frame.columns.get_loc("low")] = 99.5
        frame.iloc[104, frame.columns.get_loc("low")] = 98.5
        indicators = pd.DataFrame(
            {
                "rsi": np.full(len(frame), 50.0),
                "stoch": np.full(len(frame), 50.0),
                "cci": np.full(len(frame), 0.0),
                "macd": np.zeros(len(frame)),
            },
            index=frame.index,
        )
        indicators.loc[frame.index[90], ["rsi", "stoch", "cci", "macd"]] = [30, 20, -100, 0.2]
        indicators.loc[frame.index[104], ["rsi", "stoch", "cci", "macd"]] = [35, 25, -80, -0.2]
        now = frame.index[-1].to_pydatetime() + timedelta(minutes=16)

        with patch(
            "binance_divergence_engine._pivot_indices",
            side_effect=[[90, 104], []],
        ), patch("binance_divergence_engine._indicators", return_value=indicators):
            setup = detect_divergence_setup(frame, now=now)

        self.assertTrue(setup.eligible, setup.reason)
        self.assertEqual(setup.direction, "LONG")
        self.assertEqual(setup.kind, "regular")
        self.assertEqual(set(setup.votes), {"rsi", "stoch", "cci"})

    def test_entry_plan_is_asymmetric_and_cost_aware(self):
        d15 = _frame(120, "15min", slope=0.002)
        d5 = _frame(100, "5min", slope=0.002)
        d5.iloc[-1, d5.columns.get_loc("volume")] = 160.0
        previous_close = float(d5["close"].iloc[-2])
        last_close = previous_close + 0.15
        d5.iloc[-1, d5.columns.get_loc("close")] = last_close
        d5.iloc[-1, d5.columns.get_loc("open")] = previous_close - 0.10
        d5.iloc[-1, d5.columns.get_loc("high")] = last_close + 0.05
        d5.iloc[-1, d5.columns.get_loc("low")] = previous_close - 0.15
        setup = DivergenceSetup(
            True,
            "regular LONG 3/4",
            direction="LONG",
            kind="regular",
            votes=("rsi", "stoch", "cci"),
            vote_count=3,
            pivot1_index=70,
            pivot2_index=108,
            pivot1_price=99.7,
            pivot2_price=99.5,
            bars_ago=2,
            atr=0.8,
            signal_bar=d15.index[108].isoformat(),
        )
        now = d15.index[-1].to_pydatetime() + timedelta(minutes=16)
        plan = evaluate_divergence_entry(
            d15,
            d5,
            setup=setup,
            higher_frames={},
            live_price=float(d5["close"].iloc[-1]),
            round_trip_cost=config.BINANCE_ROUND_TRIP_EXECUTION_COST,
            spread_pct=0.02,
            now=now,
        )

        self.assertTrue(plan.eligible, plan.reason)
        self.assertAlmostEqual(plan.weighted_reward_r, 1.98, places=6)
        self.assertLessEqual(plan.required_win_rate, 0.42)
        self.assertLess(plan.stop, plan.entry)
        self.assertEqual([tp["pct"] for tp in plan.tps], [30, 70])
        self.assertGreater(plan.tps[-1]["price"] - plan.entry, plan.entry - plan.stop)

    def test_multitimeframe_router_uses_a_then_b_then_c_tiers(self):
        setup_a = DivergenceSetup(
            True, "1h regular LONG 3/4", vote_count=3,
            votes=("rsi", "stoch", "cci"), timeframe="1h",
        )
        setup_b = DivergenceSetup(
            True, "15m regular LONG 4/4", vote_count=4,
            votes=("rsi", "stoch", "cci", "macd"), timeframe="15m",
        )
        setup_c = DivergenceSetup(
            True, "15m regular LONG 3/4", vote_count=3,
            votes=("rsi", "stoch", "cci"), timeframe="15m",
        )

        selected, tier = select_multitimeframe_setup({"1h": setup_a, "15m": setup_b})
        self.assertIs(selected, setup_a)
        self.assertEqual(tier, "A")
        self.assertEqual(select_multitimeframe_setup({"15m": setup_b})[1], "B")
        self.assertEqual(select_multitimeframe_setup({"15m": setup_c})[1], "C")

    def test_a_tier_does_not_require_130_percent_five_minute_volume(self):
        d15 = _frame(120, "15min", slope=0.002)
        d5 = _frame(100, "5min", slope=0.002)
        setup = DivergenceSetup(
            True, "1h regular LONG 3/4", direction="LONG", kind="regular",
            votes=("rsi", "stoch", "cci"), vote_count=3,
            pivot1_price=99.7, pivot2_price=99.5, bars_ago=2,
            atr=1.0, signal_bar="1h:signal", timeframe="1h",
        )
        plan = evaluate_divergence_entry(
            d15, d5, setup=setup, signal_tier="A",
            higher_frames={}, live_price=float(d5["close"].iloc[-1]),
        )

        self.assertTrue(plan.eligible, plan.reason)
        self.assertEqual(plan.signal_tier, "A")
        self.assertEqual(plan.setup_timeframe, "1h")
        self.assertLess(plan.volume_ratio_5m, 1.30)

    def test_b_and_c_tiers_keep_distinct_five_minute_thresholds(self):
        d15 = _frame(120, "15min", slope=0.002)
        d5 = _frame(100, "5min", slope=0.002)
        setup = DivergenceSetup(
            True, "15m regular LONG 4/4", direction="LONG", kind="regular",
            votes=("rsi", "stoch", "cci", "macd"), vote_count=4,
            pivot1_price=99.7, pivot2_price=99.5, bars_ago=2,
            atr=1.0, signal_bar="15m:signal", timeframe="15m",
        )
        d5.iloc[-2, d5.columns.get_loc("volume")] = 106.0

        b_plan = evaluate_divergence_entry(
            d15, d5, setup=setup, signal_tier="B",
            higher_frames={}, live_price=float(d5["close"].iloc[-1]),
        )
        c_plan = evaluate_divergence_entry(
            d15, d5, setup=setup, signal_tier="C",
            higher_frames={}, live_price=float(d5["close"].iloc[-1]),
        )

        self.assertTrue(b_plan.eligible, b_plan.reason)
        self.assertFalse(c_plan.eligible)
        self.assertIn("C등급 5m 실행확인 실패", c_plan.reason)

    def test_one_hour_data_is_resampled_to_four_hour_ohlcv(self):
        d1h = _frame(96, "1h", slope=0.01)
        d4h = resample_ohlcv(d1h, "4h")

        self.assertEqual(len(d4h), 24)
        self.assertAlmostEqual(d4h["volume"].iloc[0], 400.0)
        self.assertAlmostEqual(d4h["open"].iloc[0], d1h["open"].iloc[0])
        self.assertAlmostEqual(d4h["close"].iloc[0], d1h["close"].iloc[3])

    def test_current_version_stays_fixed_live_after_eight_losses(self):
        with TemporaryDirectory() as tmp:
            permission = evaluate_live_permission(root=Path(tmp))
            self.assertTrue(permission.allow)
            self.assertEqual(permission.mode, "fixed")
            self.assertEqual(permission.account_risk_pct, 0.0025)

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
            Path(tmp, "trade_state_binance.json").write_text(
                json.dumps({"trade_history": rows}), encoding="utf-8"
            )
            after_losses = evaluate_live_permission(root=Path(tmp))

        self.assertTrue(after_losses.allow)
        self.assertEqual(after_losses.mode, "fixed")
        self.assertEqual(after_losses.account_risk_pct, 0.0025)
        self.assertEqual(after_losses.closed, 8)
        self.assertIn("성과 차단·증감 없음", after_losses.reason)

    def test_position_count_does_not_block_d2_when_risk_capacity_exists(self):
        snapshot = {
            "ok": True,
            "count": 99,
            "margin_used": 0.0,
            "long_margin": 0.0,
            "short_margin": 0.0,
            "sl_risk": 0.0,
            "equity": 100.0,
            "free": 100.0,
        }
        with patch("trade_router.get_portfolio_risk_snapshot", return_value=snapshot):
            pct, risk, notes, block = main._apply_portfolio_capacity_gate(
                100.0,
                0.05,
                0.25,
                5.0,
                "LONG",
                min_execution_margin_usd=1.0,
                enforce_position_count=False,
            )

        self.assertEqual(block, "")
        self.assertGreater(pct, 0)
        self.assertGreater(risk, 0)
        self.assertTrue(any("고정 포지션 개수 제한 없음" in note for note in notes))

    def test_live_configuration_targets_binance_full_universe(self):
        self.assertTrue(config.BINANCE_D2_ENGINE_ENABLED)
        # D2 후보 스캔은 계속하되, 비용후 음의 기대값이
        # 확인된 현 버전의 신규 실주문은 C1 challenger로 넘겼다.
        self.assertFalse(config.BINANCE_D2_LIVE_ENABLED)
        self.assertTrue(config.BINANCE_C1_ENGINE_ENABLED)
        self.assertFalse(config.BINANCE_C1_LIVE_ENABLED)
        self.assertTrue(config.BINANCE_C1_AUTO_PROMOTE_ENABLED)
        self.assertEqual(config.BINANCE_D2_SETUP_TIMEFRAME, "15m")
        self.assertEqual(config.BINANCE_D2_TRIGGER_TIMEFRAME, "5m")

    def test_full_universe_uses_exchange_metadata_not_radar_name_filter(self):
        class FakeExchange:
            markets = {
                "PUMP/USDT:USDT": {
                    "active": True, "swap": True, "linear": True,
                    "quote": "USDT", "settle": "USDT",
                },
                "BTC/USDT:USDT": {
                    "active": True, "swap": True, "linear": True,
                    "quote": "USDT", "settle": "USDT",
                },
                "OLD/USDT:USDT": {
                    "active": False, "swap": True, "linear": True,
                    "quote": "USDT", "settle": "USDT",
                },
                "ETH/USD:ETH": {
                    "active": True, "swap": True, "linear": False,
                    "quote": "USD", "settle": "ETH",
                },
                "AAPL/USDT:USDT": {
                    "active": True, "swap": True, "linear": True,
                    "quote": "USDT", "settle": "USDT",
                    "info": {"contractType": "TRADIFI_PERPETUAL"},
                },
            }

            def load_markets(self):
                return self.markets

            def fetch_tickers(self):
                return {
                    "PUMP/USDT:USDT": {"last": 1.0, "bid": 0.99, "ask": 1.01, "quoteVolume": 10},
                    "BTC/USDT:USDT": {"last": 100.0, "bid": 99.9, "ask": 100.1, "quoteVolume": 20},
                    "AAPL/USDT:USDT": {"last": 200.0, "bid": 199.9, "ask": 200.1, "quoteVolume": 30},
                }

        fetcher._full_perpetual_cache = {"rows": [], "ts": 0}
        with patch.object(fetcher, "_get_exchange", return_value=FakeExchange()):
            rows = fetcher.fetch_all_usdt_perpetual_markets()

        self.assertEqual([row["symbol"] for row in rows], ["BTC/USDT", "PUMP/USDT"])

    def test_d2_monitor_keeps_atr_gap_when_roi_step_advances(self):
        state = {
            "positions": {
                "BTC/USDT": {
                    "direction": "LONG",
                    "entry_price": 100.0,
                    "initial_qty": 1.0,
                    "opened_ts": time.time(),
                    "sl_price": 99.0,
                    "initial_sl_price": 99.0,
                    "atr": 1.0,
                    "leverage": 10,
                    "exit_policy": "d2_asymmetric",
                    "tp1_lock_r": 0.20,
                    "trail_atr_mult": 0.85,
                    "trail_activation_r": 2.0,
                    "progress_check_minutes": 30,
                    "progress_min_r": 0.5,
                    "max_hold_minutes": 90,
                }
            }
        }

        class FakeExchange:
            def load_markets(self):
                return None

            def fetch_positions(self, symbols):
                return [{
                    "contracts": 1.0,
                    "markPrice": 101.2,
                    "entryPrice": 100.0,
                    "leverage": 10,
                }]

        with patch.object(binance_trader, "is_execution_api_healthy", return_value=True), patch.object(
            binance_trader, "_ex", return_value=FakeExchange()
        ), patch.object(trader, "_load_state", side_effect=lambda: state), patch.object(
            trader, "_save_state", return_value=None
        ), patch.object(
            binance_trader, "_set_stop_loss", side_effect=lambda ex, fsym, direction, qty, stop: stop
        ) as set_stop:
            binance_trader.monitor_positions()

        self.assertTrue(set_stop.called)
        protected = float(set_stop.call_args.args[-1])
        self.assertGreater(protected, 100.0)
        self.assertLessEqual(protected, 101.2 - 0.8)


if __name__ == "__main__":
    unittest.main()
