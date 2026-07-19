"""Binance D2 divergence + volume scalping engine.

The engine is intentionally isolated from the legacy confirmed-count stack.
It uses completed candles only, requires at least three independent oscillator
votes on the same price-pivot pair, and builds an asymmetric stop/target plan
whose expected payoff does not depend on a 60% win rate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import math
from typing import Any

import pandas as pd


ENGINE_VERSION = "2026-07-18-d2-mtf-tiered-trigger"
STRATEGY = "D2_DIVERGENCE_VOLUME_ASYMMETRIC"

_TF_SECONDS = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14_400,
    "1d": 86_400,
    "1w": 604_800,
}


@dataclass(frozen=True)
class DivergenceSetup:
    eligible: bool
    reason: str
    direction: str = "LONG"
    kind: str = "regular"
    votes: tuple[str, ...] = ()
    vote_count: int = 0
    pivot1_index: int = -1
    pivot2_index: int = -1
    pivot1_price: float = 0.0
    pivot2_price: float = 0.0
    bars_ago: int = 999
    atr: float = 0.0
    signal_bar: str = ""
    indicator_values: dict[str, tuple[float, float]] | None = None
    timeframe: str = "15m"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class D2Plan:
    eligible: bool
    reason: str
    direction: str = "LONG"
    divergence_kind: str = "regular"
    indicator_votes: tuple[str, ...] = ()
    score: float = 0.0
    entry: float = 0.0
    stop: float = 0.0
    tps: tuple[dict[str, float], ...] = ()
    atr: float = 0.0
    atr_pct: float = 0.0
    stop_atr: float = 0.0
    stop_pct: float = 0.0
    volume_ratio_5m: float = 0.0
    required_win_rate: float = 1.0
    weighted_reward_r: float = 0.0
    signal_bar: str = ""
    context_votes: dict[str, int] | None = None
    metrics: dict[str, float] | None = None
    signal_tier: str = "C"
    setup_timeframe: str = "15m"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LivePermission:
    allow: bool
    mode: str
    account_risk_pct: float
    reason: str
    closed: int
    pnl_usd: float
    profit_factor: float
    expectancy_r: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _closed_bars(
    df: pd.DataFrame,
    timeframe: str,
    now: datetime | None = None,
) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame()
    out = df.copy()
    seconds = _TF_SECONDS.get(timeframe)
    if not seconds or not isinstance(out.index, pd.DatetimeIndex):
        return out
    now_ts = pd.Timestamp(now or datetime.now(timezone.utc))
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    else:
        now_ts = now_ts.tz_convert("UTC")
    last = out.index[-1]
    if last.tzinfo is None:
        last = last.tz_localize("UTC")
    else:
        last = last.tz_convert("UTC")
    if last + pd.Timedelta(seconds=seconds) > now_ts:
        out = out.iloc[:-1]
    return out


def _atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev).abs(),
            (df["low"] - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def _rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    result = 100 - 100 / (1 + rs)
    return result.fillna(50.0)


def _indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    lowest = low.rolling(14).min()
    highest = high.rolling(14).max()
    stochastic = (close - lowest) / (highest - lowest).replace(0, float("nan")) * 100
    typical = (high + low + close) / 3
    typical_mean = typical.rolling(20).mean()
    mean_dev = typical.rolling(20).apply(
        # raw ndarray avoids constructing tens of thousands of temporary
        # pandas Series during a 500-symbol universe scan. The CCI formula and
        # numerical result are unchanged.
        lambda values: float(abs(values - values.mean()).mean()), raw=True
    )
    cci = (typical - typical_mean) / (0.015 * mean_dev.replace(0, float("nan")))
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_hist = macd - macd.ewm(span=9, adjust=False).mean()
    return pd.DataFrame(
        {
            "rsi": _rsi(close),
            "stoch": stochastic,
            "cci": cci,
            "macd": macd_hist,
        },
        index=df.index,
    )


def _pivot_indices(
    series: pd.Series,
    kind: str,
    left: int = 3,
    right: int = 2,
) -> list[int]:
    values = series.astype(float).to_numpy()
    result: list[int] = []
    for idx in range(left, len(values) - right):
        value = values[idx]
        if not math.isfinite(value):
            continue
        before = values[idx - left:idx]
        after = values[idx + 1:idx + right + 1]
        if kind == "low":
            is_pivot = value <= min(before) and value < min(after)
        else:
            is_pivot = value >= max(before) and value > max(after)
        if is_pivot:
            result.append(idx)
    return result


def _oscillator_vote(
    name: str,
    first: float,
    second: float,
    rising: bool,
    macd_scale: float,
) -> bool:
    if not math.isfinite(first) or not math.isfinite(second):
        return False
    eps = {
        "rsi": 1.0,
        "stoch": 2.5,
        "cci": 8.0,
        "macd": max(macd_scale * 0.05, 1e-12),
    }[name]
    return second > first + eps if rising else second < first - eps


def detect_divergence_setup(
    df: pd.DataFrame,
    *,
    timeframe: str = "15m",
    now: datetime | None = None,
    min_votes: int = 3,
) -> DivergenceSetup:
    """Find the freshest 3-of-4 regular or hidden divergence on one timeframe."""
    bars = _closed_bars(df, timeframe, now)
    required = {"open", "high", "low", "close", "volume"}
    if len(bars) < 80 or not required.issubset(bars.columns):
        return DivergenceSetup(
            False, f"완료된 {timeframe} 데이터 부족", timeframe=timeframe
        )

    atr = float(_atr(bars).iloc[-1])
    if atr <= 0 or not math.isfinite(atr):
        return DivergenceSetup(False, f"{timeframe} ATR 계산 실패", timeframe=timeframe)
    indicators = _indicators(bars)
    macd_scale = float(indicators["macd"].iloc[-50:].std() or 0.0)
    lows = _pivot_indices(bars["low"], "low")
    highs = _pivot_indices(bars["high"], "high")
    candidates: list[DivergenceSetup] = []
    max_bars_ago = {"15m": 8, "1h": 6, "4h": 4}.get(timeframe, 6)

    definitions = (
        ("LONG", "regular", lows, "low", True, -1),
        ("LONG", "hidden", lows, "low", False, 1),
        ("SHORT", "regular", highs, "high", False, 1),
        ("SHORT", "hidden", highs, "high", True, -1),
    )
    for direction, div_kind, pivots, price_column, osc_rising, price_sign in definitions:
        # Inspect a few recent consecutive pivot pairs.  This avoids selecting
        # an older attractive pair while a newer pivot has invalidated it.
        for pair_index in range(len(pivots) - 1, max(len(pivots) - 5, 0), -1):
            if pair_index <= 0:
                break
            first_idx = pivots[pair_index - 1]
            second_idx = pivots[pair_index]
            separation = second_idx - first_idx
            bars_ago = len(bars) - 1 - second_idx
            if separation < 4 or separation > 40 or bars_ago > max_bars_ago:
                continue
            first_price = float(bars[price_column].iloc[first_idx])
            second_price = float(bars[price_column].iloc[second_idx])
            price_delta = (second_price - first_price) * price_sign
            if price_delta < atr * 0.05:
                continue

            votes: list[str] = []
            values: dict[str, tuple[float, float]] = {}
            for name in ("rsi", "stoch", "cci", "macd"):
                first_value = float(indicators[name].iloc[first_idx])
                second_value = float(indicators[name].iloc[second_idx])
                values[name] = (round(first_value, 6), round(second_value, 6))
                if _oscillator_vote(
                    name,
                    first_value,
                    second_value,
                    osc_rising,
                    macd_scale,
                ):
                    votes.append(name)
            if len(votes) < min_votes:
                continue
            signal_idx = bars.index[second_idx]
            signal_time = (
                signal_idx.isoformat()
                if hasattr(signal_idx, "isoformat") else str(signal_idx)
            )
            candidates.append(
                DivergenceSetup(
                    True,
                    f"{timeframe} {div_kind} {direction} 다이버전스 {len(votes)}/4",
                    direction=direction,
                    kind=div_kind,
                    votes=tuple(votes),
                    vote_count=len(votes),
                    pivot1_index=first_idx,
                    pivot2_index=second_idx,
                    pivot1_price=first_price,
                    pivot2_price=second_price,
                    bars_ago=bars_ago,
                    atr=atr,
                    signal_bar=f"{timeframe}:{signal_time}",
                    indicator_values=values,
                    timeframe=timeframe,
                )
            )

    if not candidates:
        return DivergenceSetup(
            False,
            f"{timeframe} RSI/Stoch/CCI/MACD 3-of-4 다이버전스 없음",
            timeframe=timeframe,
        )
    candidates.sort(
        key=lambda setup: (setup.vote_count, -setup.bars_ago, setup.kind == "hidden"),
        reverse=True,
    )
    return candidates[0]


def select_multitimeframe_setup(
    setups: dict[str, DivergenceSetup],
) -> tuple[DivergenceSetup | None, str]:
    """Route setups into A/B/C tiers before applying the 5m execution trigger."""
    higher = [
        setup
        for timeframe in ("4h", "1h")
        if (setup := setups.get(timeframe)) is not None
        and setup.eligible
        and setup.vote_count >= 3
    ]
    if higher:
        higher.sort(
            key=lambda setup: (
                setup.vote_count,
                1 if setup.timeframe == "4h" else 0,
                -setup.bars_ago,
            ),
            reverse=True,
        )
        return higher[0], "A"

    setup_15m = setups.get("15m")
    if setup_15m is not None and setup_15m.eligible:
        return setup_15m, "B" if setup_15m.vote_count >= 4 else "C"
    return None, ""


def resample_ohlcv(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Build a higher OHLCV frame locally from a DatetimeIndex."""
    if df is None or len(df) == 0 or not isinstance(df.index, pd.DatetimeIndex):
        return pd.DataFrame()
    rule = {"4h": "4h"}.get(timeframe)
    if not rule:
        raise ValueError(f"unsupported resample timeframe: {timeframe}")
    return (
        df.resample(rule, label="left", closed="left")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna()
    )


def _context_vote(df: pd.DataFrame | None, timeframe: str, direction: str) -> int:
    closed = _closed_bars(df, timeframe) if df is not None else pd.DataFrame()
    if len(closed) < 55:
        return 0
    close = closed["close"].astype(float)
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    slope = float(ema20.iloc[-1] - ema20.iloc[-4])
    long_trend = ema20.iloc[-1] > ema50.iloc[-1] and slope > 0
    short_trend = ema20.iloc[-1] < ema50.iloc[-1] and slope < 0
    if (direction == "LONG" and long_trend) or (direction == "SHORT" and short_trend):
        return 1
    if (direction == "LONG" and short_trend) or (direction == "SHORT" and long_trend):
        return -1
    return 0


def evaluate_divergence_entry(
    df_15m: pd.DataFrame,
    df_5m: pd.DataFrame,
    *,
    setup: DivergenceSetup | None = None,
    signal_tier: str = "C",
    higher_frames: dict[str, pd.DataFrame] | None = None,
    live_price: float | None = None,
    round_trip_cost: float = 0.0016,
    spread_pct: float | None = None,
    quote_volume_usd: float | None = None,
    min_quote_volume_usd: float = 0.0,
    now: datetime | None = None,
) -> D2Plan:
    setup = setup or detect_divergence_setup(df_15m, now=now)
    if not setup.eligible:
        return D2Plan(
            False, setup.reason, signal_tier=signal_tier,
            setup_timeframe=setup.timeframe,
        )
    signal_tier = signal_tier if signal_tier in {"A", "B", "C"} else "C"
    direction = setup.direction
    d15 = _closed_bars(df_15m, "15m", now)
    d5 = _closed_bars(df_5m, "5m", now)
    plan_identity = {
        "direction": direction,
        "divergence_kind": setup.kind,
        "indicator_votes": setup.votes,
        "signal_bar": setup.signal_bar,
        "signal_tier": signal_tier,
        "setup_timeframe": setup.timeframe,
    }
    if len(d5) < 50:
        return D2Plan(False, "완료된 5m 트리거 데이터 부족", **plan_identity)

    atr15 = float(_atr(d15).iloc[-1])
    atr5 = float(_atr(d5).iloc[-1])
    last5 = d5.iloc[-1]
    entry = float(live_price or last5["close"])
    if entry <= 0 or atr15 <= 0 or atr5 <= 0:
        return D2Plan(False, "가격/ATR 계산 실패", **plan_identity)
    if (
        quote_volume_usd is not None
        and min_quote_volume_usd > 0
        and quote_volume_usd < min_quote_volume_usd
    ):
        return D2Plan(
            False,
            f"24h 거래대금 ${quote_volume_usd:,.0f} < ${min_quote_volume_usd:,.0f}",
            **plan_identity,
        )
    if spread_pct is not None and spread_pct > 0.18:
        return D2Plan(
            False, f"스프레드 {spread_pct:.3f}% > 0.18%", **plan_identity,
        )

    ema9 = d5["close"].astype(float).ewm(span=9, adjust=False).mean()
    volume_median = d5["volume"].astype(float).shift(1).rolling(20).median()
    trigger_rows: list[dict[str, float | int | bool]] = []
    for pos in range(len(d5) - 3, len(d5)):
        row = d5.iloc[pos]
        candle_range = max(float(row["high"] - row["low"]), 1e-12)
        close_location = float(row["close"] - row["low"]) / candle_range
        if direction == "LONG":
            candle_ok = float(row["close"]) > float(row["open"])
            ema_ok = float(row["close"]) > float(ema9.iloc[pos])
            previous_ok = float(row["close"]) > float(d5["close"].iloc[pos - 1])
            location_ok = close_location >= 0.60
        else:
            candle_ok = float(row["close"]) < float(row["open"])
            ema_ok = float(row["close"]) < float(ema9.iloc[pos])
            previous_ok = float(row["close"]) < float(d5["close"].iloc[pos - 1])
            location_ok = close_location <= 0.40
        base_volume = float(volume_median.iloc[pos])
        rvol = float(row["volume"]) / base_volume if base_volume > 0 else 0.0
        trigger_rows.append(
            {
                "evidence": sum((candle_ok, ema_ok, previous_ok, location_ok)),
                "candle_ok": candle_ok,
                "direction_shape_ok": candle_ok or location_ok,
                "momentum_ok": ema_ok or previous_ok,
                "rvol": rvol,
                "close_location": close_location,
            }
        )

    direction_bars = sum(
        bool(row["candle_ok"] and row["evidence"] >= 2) for row in trigger_rows
    )
    volume_ratio = max(float(row["rvol"]) for row in trigger_rows)
    if signal_tier == "A":
        trigger_ok = any(
            row["evidence"] >= 2
            and row["momentum_ok"]
            and row["direction_shape_ok"]
            for row in trigger_rows
        )
        volume_floor = 0.0
        extension_limit = 1.50
        trigger_rule = "최근 3개 중 방향증거 2개"
    elif signal_tier == "B":
        trigger_ok = direction_bars >= 2 and volume_ratio >= 1.05
        volume_floor = 1.05
        extension_limit = 1.35
        trigger_rule = "최근 3개 중 방향봉 2개 + RVOL 1.05x"
    else:
        latest = trigger_rows[-1]
        trigger_ok = latest["evidence"] == 4 and latest["rvol"] >= 1.30
        volume_floor = 1.30
        extension_limit = 1.25
        trigger_rule = "최신봉 방향증거 4개 + RVOL 1.30x"
        volume_ratio = float(latest["rvol"])

    if not trigger_ok:
        detail = (
            f"방향봉={direction_bars}/3, 최근증거="
            + "/".join(str(int(row["evidence"])) for row in trigger_rows)
            + f", RVOL={volume_ratio:.2f}x"
        )
        return D2Plan(
            False, f"{signal_tier}등급 5m 실행확인 실패 ({trigger_rule}; {detail})",
            volume_ratio_5m=volume_ratio, **plan_identity,
        )

    trigger_extension = (
        (entry - float(ema9.iloc[-1])) / atr5
        if direction == "LONG"
        else (float(ema9.iloc[-1]) - entry) / atr5
    )
    if trigger_extension > extension_limit:
        return D2Plan(
            False,
            f"{signal_tier}등급 5m EMA9 대비 {trigger_extension:.2f}ATR 추격 "
            f"> {extension_limit:.2f}ATR",
            volume_ratio_5m=volume_ratio, **plan_identity,
        )

    higher_frames = higher_frames or {}
    context_votes = {
        tf: _context_vote(higher_frames.get(tf), tf, direction)
        for tf in ("1h", "4h", "1d", "1w")
    }
    # Hidden divergence is a continuation setup, so it must have actual trend
    # support on either 1h or 4h.  Regular divergence remains a reversal setup
    # and records higher-timeframe opposition without pretending it is invalid.
    if setup.kind == "hidden" and max(context_votes["1h"], context_votes["4h"]) < 1:
        return D2Plan(
            False, "hidden 다이버전스 1h/4h 추세 지지 없음",
            volume_ratio_5m=volume_ratio, context_votes=context_votes,
            **plan_identity,
        )

    side = 1.0 if direction == "LONG" else -1.0
    if setup.timeframe == "15m":
        structural_stop = setup.pivot2_price - side * 0.25 * atr15
    elif direction == "LONG":
        structural_stop = float(d15["low"].iloc[-8:].min()) - 0.20 * atr15
    else:
        structural_stop = float(d15["high"].iloc[-8:].max()) + 0.20 * atr15
    minimum_risk = max(0.45 * atr15, entry * max(round_trip_cost, 0.0) * 2.5)
    if direction == "LONG":
        stop = min(structural_stop, entry - minimum_risk)
        risk = entry - stop
    else:
        stop = max(structural_stop, entry + minimum_risk)
        risk = stop - entry
    stop_atr = risk / atr15
    stop_pct = risk / entry * 100
    if stop <= 0 or risk <= 0:
        return D2Plan(False, "구조손절이 진입가 반대편에 없음", **plan_identity)
    if stop_atr > 1.80 or stop_pct > 3.50:
        return D2Plan(
            False, f"구조손절 과대 {stop_atr:.2f}ATR/{stop_pct:.2f}%",
            volume_ratio_5m=volume_ratio, context_votes=context_votes,
            **plan_identity,
        )

    if setup.kind == "hidden":
        first_rr, first_pct, runner_rr, runner_pct = 1.0, 25, 2.7, 75
        trail_mult = 1.10
    else:
        first_rr, first_pct, runner_rr, runner_pct = 1.0, 30, 2.4, 70
        trail_mult = 0.85
    tp1 = entry + side * first_rr * risk
    tp2 = entry + side * runner_rr * risk
    weighted_reward_r = first_rr * first_pct / 100 + runner_rr * runner_pct / 100
    weighted_gain = weighted_reward_r * risk
    cost_cash = entry * max(round_trip_cost, 0.0)
    net_gain = max(weighted_gain - cost_cash, 0.0)
    net_loss = risk + cost_cash
    required_wr = net_loss / (net_loss + net_gain) if net_gain > 0 else 1.0
    if required_wr > 0.42:
        return D2Plan(
            False, f"비용후 손익분기 승률 {required_wr*100:.1f}% > 42%",
            volume_ratio_5m=volume_ratio,
            required_win_rate=required_wr, weighted_reward_r=weighted_reward_r,
            context_votes=context_votes, **plan_identity,
        )

    score = 50 + setup.vote_count * 8
    score += {"A": 12, "B": 7, "C": 0}[signal_tier]
    score += min(max(volume_ratio - volume_floor, 0.0) * 5, 10)
    score += sum(context_votes.values()) * 2
    score += 5 if trigger_extension <= 0.75 else 0
    tps = (
        {"price": tp1, "pct": first_pct, "rr": first_rr},
        {"price": tp2, "pct": runner_pct, "rr": runner_rr},
    )
    return D2Plan(
        True,
        f"{signal_tier}등급 {setup.timeframe} {setup.kind} divergence + 5m execution",
        score=round(score, 2),
        entry=entry,
        stop=stop,
        tps=tps,
        atr=atr15,
        atr_pct=round(atr15 / entry * 100, 5),
        stop_atr=round(stop_atr, 5),
        stop_pct=round(stop_pct, 5),
        volume_ratio_5m=round(volume_ratio, 5),
        required_win_rate=round(required_wr, 6),
        weighted_reward_r=round(weighted_reward_r, 4),
        context_votes=context_votes,
        **plan_identity,
        metrics={
            "trigger_extension_atr": round(trigger_extension, 5),
            "close_location_5m": round(float(trigger_rows[-1]["close_location"]), 5),
            "directional_bars_5m": float(direction_bars),
            "trigger_evidence_latest": float(trigger_rows[-1]["evidence"]),
            "trigger_volume_floor": volume_floor,
            "trail_atr_mult": trail_mult,
            "setup_bars_ago": float(setup.bars_ago),
            "pivot1_price": setup.pivot1_price,
            "pivot2_price": setup.pivot2_price,
        },
    )


def evaluate_live_permission(
    *,
    root: Path,
    venue: str = "binance",
    engine_version: str = ENGINE_VERSION,
    strategy: str = STRATEGY,
    engine_label: str = "D2",
) -> LivePermission:
    """Return fixed-size live permission while retaining outcome statistics.

    Closed-trade PnL/PF/expectancy is observational only.  It must not block a
    fresh strategy-qualified D2/D3 signal or automatically increase/decrease
    its size.  Account and order safety limits remain enforced by the execution
    layer.
    """
    path = root / ("trade_state_binance.json" if venue == "binance" else "trade_state.json")
    rows: list[dict] = []
    if path.exists():
        try:
            history = json.loads(path.read_text(encoding="utf-8")).get("trade_history", [])
        except Exception:
            history = []
        for row in history:
            ctx = row.get("entry_context") or {}
            version = row.get("engine_version") or ctx.get("engine_version")
            if (
                row.get("status") in {"win", "loss"}
                and row.get("strategy") == strategy
                and version == engine_version
            ):
                rows.append(row)

    pnls = [float(row.get("pnl_usd") or 0.0) for row in rows]
    wins = sum(value for value in pnls if value > 0)
    losses = -sum(value for value in pnls if value < 0)
    profit_factor = wins / losses if losses > 0 else (999.0 if wins > 0 else 0.0)
    r_values = []
    for row in rows:
        planned_risk = float(
            row.get("est_sl_loss")
            or (row.get("entry_context") or {}).get("est_sl_loss")
            or 0.0
        )
        if planned_risk > 0:
            r_values.append(float(row.get("pnl_usd") or 0.0) / planned_risk)
    expectancy_r = sum(r_values) / len(r_values) if r_values else 0.0
    base = {
        "closed": len(rows),
        "pnl_usd": round(sum(pnls), 8),
        "profit_factor": round(profit_factor, 8),
        "expectancy_r": round(expectancy_r, 8),
    }
    return LivePermission(
        True,
        "fixed",
        0.0025,
        (
            f"{engine_label} 고정 소액 실매매 — 성과 차단·증감 없음, "
            f"계좌위험 0.25% (관찰 n={len(rows)} PF={profit_factor:.2f} "
            f"E={expectancy_r:+.2f}R)"
        ),
        **base,
    )
