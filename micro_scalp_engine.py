"""Micro-scalp canary: 5m breakout/pullback setup + 1m reaccel trigger.

Separate from S1 (15m/5m). Uses tiny risk and short hold so live fills are
observable without replacing the EMA 15m core book.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from timing_trigger import closed_bars, evaluate_reaccel_trigger


ENGINE_VERSION = "2026-08-04-micro-v1"
STRATEGY = "MICRO_SCALP_BREAKOUT_PB"
CANARY_MIN_CLOSED = 12


@dataclass(frozen=True)
class MicroScalpPlan:
    eligible: bool
    reason: str
    score: float = 0.0
    direction: str = "LONG"
    entry: float = 0.0
    stop: float = 0.0
    tps: tuple[dict[str, float], ...] = ()
    atr: float = 0.0
    atr_pct: float = 0.0
    stop_pct: float = 0.0
    stop_atr: float = 0.0
    volume_ratio: float = 0.0
    trigger_volume_ratio: float = 0.0
    signal_bar: str = ""
    trigger_bar: str = ""
    required_win_rate: float = 1.0
    metrics: dict[str, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / length, adjust=False).mean()


def evaluate_micro_scalp(
    df_5m: pd.DataFrame,
    df_1m: pd.DataFrame,
    *,
    live_price: float | None = None,
    round_trip_cost: float = 0.0011,
    spread_pct: float | None = None,
    now: datetime | None = None,
    long_only: bool = False,
    min_score: float = 72.0,
    min_volume_ratio_5m: float = 1.05,
    min_trigger_volume: float = 0.80,
    max_stop_atr: float = 1.80,
    max_stop_pct: float = 1.80,
    max_spread_pct: float = 0.12,
    min_tp1_net_fee_mult: float = 2.0,
) -> MicroScalpPlan:
    """5m structure + completed 1m reaccel. Fail-closed on cost/extension."""
    now = now or datetime.now(timezone.utc)
    d5 = closed_bars(df_5m, "5m", now)
    if len(d5) < 80:
        return MicroScalpPlan(False, "5m 완료봉 부족")
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(d5.columns):
        return MicroScalpPlan(False, "5m OHLCV 부족")

    close5 = d5["close"].astype(float)
    ema20 = close5.ewm(span=20, adjust=False).mean()
    ema50 = close5.ewm(span=50, adjust=False).mean()
    atr5s = _atr(d5)
    atr5 = float(atr5s.iloc[-1])
    signal_close = float(close5.iloc[-1])
    entry = float(live_price or signal_close)
    if atr5 <= 0 or entry <= 0:
        return MicroScalpPlan(False, "ATR/가격 실패")

    signal_bar = (
        d5.index[-1].isoformat()
        if hasattr(d5.index[-1], "isoformat")
        else str(d5.index[-1])
    )
    atr_pct = atr5 / entry * 100

    if float(ema20.iloc[-1]) > float(ema50.iloc[-1]) and close5.iloc[-1] > ema20.iloc[-1]:
        direction = "LONG"
    elif float(ema20.iloc[-1]) < float(ema50.iloc[-1]) and close5.iloc[-1] < ema20.iloc[-1]:
        direction = "SHORT"
    else:
        return MicroScalpPlan(False, "5m 추세 방향 미확인", atr=atr5, atr_pct=atr_pct, signal_bar=signal_bar)

    if long_only and direction != "LONG":
        return MicroScalpPlan(
            False, "MICRO LONG-only", direction=direction, atr=atr5, atr_pct=atr_pct, signal_bar=signal_bar
        )

    if atr_pct < 0.08 or atr_pct > 2.8:
        return MicroScalpPlan(
            False,
            f"5m 변동성 이탈 ATR {atr_pct:.2f}%",
            direction=direction,
            atr=atr5,
            atr_pct=atr_pct,
            signal_bar=signal_bar,
        )
    if spread_pct is not None and spread_pct > float(max_spread_pct):
        return MicroScalpPlan(
            False,
            f"스프레드 {spread_pct:.3f}% > {float(max_spread_pct):.3f}%",
            direction=direction,
            atr=atr5,
            atr_pct=atr_pct,
            signal_bar=signal_bar,
        )

    # Breakout of prior 12-bar range (excluding last bar), then hold.
    look = d5.iloc[-13:-1]
    last5 = d5.iloc[-1]
    prior_high = float(look["high"].max())
    prior_low = float(look["low"].min())
    if direction == "LONG":
        broke = float(last5["close"]) >= prior_high * 0.999
        hold = float(last5["low"]) > prior_low
        bullish = float(last5["close"]) > float(last5["open"])
    else:
        broke = float(last5["close"]) <= prior_low * 1.001
        hold = float(last5["high"]) < prior_high
        bullish = float(last5["close"]) < float(last5["open"])

    vol_base = float(d5["volume"].iloc[-21:-1].median())
    vol_ratio = float(last5["volume"]) / vol_base if vol_base > 0 else 0.0

    # Mild pullback into EMA20 zone in last 6 bars (not chasing pure vertical).
    recent = d5.iloc[-7:-1]
    if direction == "LONG":
        pullback = bool((recent["low"] <= ema20.iloc[-7:-1] + 0.55 * atr5).any())
        # Breakout bars can sit 1.5~2.2 ATR above EMA20; allow up to 2.4 before chase.
        not_extended = (entry - float(ema20.iloc[-1])) / atr5 <= 2.40
    else:
        pullback = bool((recent["high"] >= ema20.iloc[-7:-1] - 0.55 * atr5).any())
        not_extended = (float(ema20.iloc[-1]) - entry) / atr5 <= 2.40

    score = 0.0
    score += 22.0 if broke else 0.0
    score += 12.0 if hold else 0.0
    score += 10.0 if bullish else 0.0
    score += 14.0 if pullback else 0.0
    score += 10.0 if not_extended else 0.0
    score += 14.0 if vol_ratio >= float(min_volume_ratio_5m) else (
        6.0 if vol_ratio >= 0.90 else 0.0
    )

    if not broke or not hold or not bullish:
        return MicroScalpPlan(
            False,
            "5m 돌파·유지 구조 미확인",
            score=score,
            direction=direction,
            atr=atr5,
            atr_pct=atr_pct,
            volume_ratio=round(vol_ratio, 4),
            signal_bar=signal_bar,
        )
    if not pullback or not not_extended:
        return MicroScalpPlan(
            False,
            "5m 눌림/비추격 조건 미달",
            score=score,
            direction=direction,
            atr=atr5,
            atr_pct=atr_pct,
            volume_ratio=round(vol_ratio, 4),
            signal_bar=signal_bar,
        )
    if vol_ratio < float(min_volume_ratio_5m):
        return MicroScalpPlan(
            False,
            f"5m 거래량 {vol_ratio:.2f}x < {float(min_volume_ratio_5m):.2f}x",
            score=score,
            direction=direction,
            atr=atr5,
            atr_pct=atr_pct,
            volume_ratio=round(vol_ratio, 4),
            signal_bar=signal_bar,
        )

    trigger = evaluate_reaccel_trigger(
        df_1m,
        direction,
        timeframe="1m",
        now=now,
        min_volume_ratio=float(min_trigger_volume),
        max_extension_atr=1.15,
        require_ema_stack=True,
    )
    if not trigger.ok:
        return MicroScalpPlan(
            False,
            f"1m 트리거 대기 — {trigger.reason}",
            score=score,
            direction=direction,
            atr=atr5,
            atr_pct=atr_pct,
            volume_ratio=round(vol_ratio, 4),
            signal_bar=signal_bar,
            trigger_bar=trigger.signal_bar,
            metrics={"trigger_vol": trigger.volume_ratio},
        )
    score += 18.0

    if score < float(min_score):
        return MicroScalpPlan(
            False,
            f"점수 {score:.0f} < {float(min_score):.0f}",
            score=score,
            direction=direction,
            atr=atr5,
            atr_pct=atr_pct,
            volume_ratio=round(vol_ratio, 4),
            signal_bar=signal_bar,
            trigger_bar=trigger.signal_bar,
        )

    # Prefer tight scalp stop: structure if close enough, else ATR cap.
    atr_cap = float(max_stop_atr) * 0.85
    if direction == "LONG":
        swing = float(d5["low"].iloc[-5:].min())
        structural = swing - 0.05 * atr5
        atr_stop = entry - max(0.55, min(atr_cap, 1.20)) * atr5
        # Use the higher stop (tighter) so risk stays micro-scalp sized.
        stop = max(structural, atr_stop)
        if stop >= entry:
            stop = atr_stop
        risk = entry - stop
    else:
        swing = float(d5["high"].iloc[-5:].max())
        structural = swing + 0.05 * atr5
        atr_stop = entry + max(0.55, min(atr_cap, 1.20)) * atr5
        stop = min(structural, atr_stop)
        if stop <= entry:
            stop = atr_stop
        risk = stop - entry
    stop_atr = risk / atr5
    stop_pct = risk / entry * 100
    if risk <= 0 or stop_atr > float(max_stop_atr) or stop_pct > float(max_stop_pct):
        return MicroScalpPlan(
            False,
            f"구조손절 과대 {stop_atr:.2f}ATR/{stop_pct:.2f}%",
            score=score,
            direction=direction,
            atr=atr5,
            atr_pct=atr_pct,
            signal_bar=signal_bar,
            trigger_bar=trigger.signal_bar,
        )

    # Fast scalp R:R — 50%@1.0R + 50%@1.8R (weighted ~1.4R gross)
    side = 1.0 if direction == "LONG" else -1.0
    tp1_r, tp2_r = 1.0, 1.8
    tp1_pct, tp2_pct = 0.50, 0.50
    tp1 = entry + side * tp1_r * risk
    tp2 = entry + side * tp2_r * risk
    weighted_gain = tp1_pct * abs(tp1 - entry) + tp2_pct * abs(tp2 - entry)
    cost_cash = entry * max(float(round_trip_cost), 0.0)
    net_gain = max(weighted_gain - cost_cash, 0.0)
    net_loss = risk + cost_cash
    required_wr = net_loss / (net_loss + net_gain) if net_gain > 0 else 1.0
    if required_wr > 0.48:
        return MicroScalpPlan(
            False,
            f"비용후 손익분기 승률 {required_wr*100:.1f}% 과다",
            score=score,
            direction=direction,
            atr=atr5,
            atr_pct=atr_pct,
            signal_bar=signal_bar,
            trigger_bar=trigger.signal_bar,
        )
    tp1_move = abs(tp1 - entry)
    fee_floor = cost_cash * max(float(min_tp1_net_fee_mult), 0.0)
    if fee_floor > 0 and tp1_move + 1e-12 < fee_floor:
        return MicroScalpPlan(
            False,
            f"TP1 이득 < 수수료×{float(min_tp1_net_fee_mult):.1f}",
            score=score,
            direction=direction,
            atr=atr5,
            atr_pct=atr_pct,
            signal_bar=signal_bar,
            trigger_bar=trigger.signal_bar,
        )

    tps = (
        {"price": tp1, "pct": int(tp1_pct * 100), "rr": tp1_r},
        {"price": tp2, "pct": int(tp2_pct * 100), "rr": tp2_r},
    )
    return MicroScalpPlan(
        True,
        "5m 돌파·눌림 + 1m 재가속 통과",
        score=round(score, 2),
        direction=direction,
        entry=entry,
        stop=stop,
        tps=tps,
        atr=atr5,
        atr_pct=round(atr_pct, 4),
        stop_pct=round(stop_pct, 4),
        stop_atr=round(stop_atr, 4),
        volume_ratio=round(vol_ratio, 4),
        trigger_volume_ratio=round(float(trigger.volume_ratio), 4),
        signal_bar=signal_bar,
        trigger_bar=trigger.signal_bar,
        required_win_rate=round(required_wr, 6),
        metrics={
            "prior_high": prior_high,
            "prior_low": prior_low,
            "trigger_strength": round(float(trigger.strength), 4),
            "trigger_extension": round(float(trigger.extension_atr), 4),
        },
    )
