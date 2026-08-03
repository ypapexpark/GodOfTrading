"""Unit tests for 1m timing trigger and micro-scalp engine."""
from datetime import timedelta

import numpy as np
import pandas as pd

import config
from micro_scalp_engine import evaluate_micro_scalp
from timing_trigger import evaluate_reaccel_trigger


def _frame(periods: int, start: str, freq: str, slope: float) -> pd.DataFrame:
    index = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    close = 100 + np.arange(periods) * slope + np.sin(np.arange(periods) / 5) * 0.05
    open_ = close - 0.08
    high = np.maximum(open_, close) + 0.12
    low = np.minimum(open_, close) - 0.12
    volume = np.full(periods, 100.0)
    volume[-1] = 160.0
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def test_reaccel_trigger_long_ok():
    d1 = _frame(90, "2024-01-01", "1min", 0.04)
    d1.iloc[-1, d1.columns.get_loc("open")] = float(d1["close"].iloc[-1]) - 0.15
    now = d1.index[-1].to_pydatetime() + timedelta(minutes=2)
    trig = evaluate_reaccel_trigger(
        d1, "LONG", timeframe="1m", now=now, min_volume_ratio=0.7
    )
    assert trig.ok, trig.reason
    assert trig.direction == "LONG"
    assert trig.volume_ratio >= 0.7


def test_reaccel_trigger_rejects_chase():
    d1 = _frame(90, "2024-01-01", "1min", 0.04)
    # Extreme extension above EMA9
    d1.iloc[-1, d1.columns.get_loc("close")] = float(d1["close"].iloc[-1]) * 1.05
    d1.iloc[-1, d1.columns.get_loc("high")] = float(d1["close"].iloc[-1]) * 1.06
    now = d1.index[-1].to_pydatetime() + timedelta(minutes=2)
    trig = evaluate_reaccel_trigger(
        d1, "LONG", timeframe="1m", now=now, max_extension_atr=0.3
    )
    assert not trig.ok


def test_micro_scalp_builds_plan_on_breakout_and_1m():
    d5 = _frame(120, "2024-01-01", "5min", 0.05)
    ema20 = d5["close"].ewm(span=20, adjust=False).mean()
    # Mild pullback toward EMA20 in recent bars
    for i in (-6, -5, -4):
        d5.iloc[i, d5.columns.get_loc("low")] = float(ema20.iloc[i]) - 0.01
    # Breakout last bar of prior range
    prior_high = float(d5["high"].iloc[-13:-1].max())
    d5.iloc[-1, d5.columns.get_loc("close")] = prior_high * 1.001
    d5.iloc[-1, d5.columns.get_loc("high")] = prior_high * 1.002
    d5.iloc[-1, d5.columns.get_loc("open")] = prior_high * 0.999
    d5.iloc[-1, d5.columns.get_loc("low")] = prior_high * 0.998
    d5.iloc[-1, d5.columns.get_loc("volume")] = 220.0

    d1 = _frame(120, "2024-01-02", "1min", 0.02)
    d1.iloc[-1, d1.columns.get_loc("open")] = float(d1["close"].iloc[-1]) - 0.10
    d1.iloc[-1, d1.columns.get_loc("volume")] = 180.0
    now = d5.index[-1].to_pydatetime() + timedelta(minutes=6)
    live = float(d5["close"].iloc[-1])

    plan = evaluate_micro_scalp(
        d5,
        d1,
        live_price=live,
        # Synthetic prices have tiny stop width; use low cost so fee gate is fair.
        round_trip_cost=0.0002,
        spread_pct=0.02,
        now=now,
        min_score=60.0,
        min_volume_ratio_5m=1.0,
        min_trigger_volume=0.7,
        min_tp1_net_fee_mult=1.0,
    )
    assert plan.eligible, plan.reason
    assert plan.direction == "LONG"
    assert plan.stop < plan.entry
    assert len(plan.tps) == 2


def test_config_flags_enable_ema1m_and_micro():
    assert config.EMA_1M_TRIGGER_ENABLED is True
    assert config.MICRO_SCALP_ENABLED is True
    assert "1m" in config.TIMEFRAMES
    assert config.MICRO_SCALP_ACCOUNT_RISK_PCT <= 0.005
    assert config.MICRO_SCALP_MAX_OPEN_POSITIONS == 1
