"""Binance D3: 4h MA200 volume breakout followed by a confirmed long pullback."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import math
from typing import Any

import pandas as pd

from binance_divergence_engine import _atr, _closed_bars


ENGINE_VERSION = "2026-07-18-d3-4h-ma200-bb-mid-pullback"
STRATEGY = "D3_4H_MA200_VOLUME_PULLBACK_LONG"


@dataclass(frozen=True)
class MA200BreakoutSetup:
    eligible: bool
    reason: str
    breakout_bar: str = ""
    breakout_index: int = -1
    bars_since_breakout: int = 999
    below_ma_ratio: float = 0.0
    breakout_volume_ratio: float = 0.0
    breakout_body_atr: float = 0.0
    breakout_extension_atr: float = 0.0
    breakout_close: float = 0.0
    breakout_ma200: float = 0.0
    atr4h: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MA200PullbackPlan:
    eligible: bool
    reason: str
    direction: str = "LONG"
    divergence_kind: str = "ma200_pullback"
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
    metrics: dict[str, Any] | None = None
    signal_tier: str = "PB"
    setup_timeframe: str = "4h"
    strategy: str = STRATEGY
    engine_version: str = ENGINE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bollinger(close: pd.Series, length: int = 20, width: float = 2.0):
    middle = close.rolling(length).mean()
    deviation = close.rolling(length).std(ddof=0)
    return middle, middle + width * deviation, middle - width * deviation


def _long_reversal_rows(df: pd.DataFrame) -> list[dict[str, float]]:
    close = df["close"].astype(float)
    ema9 = close.ewm(span=9, adjust=False).mean()
    volume_base = df["volume"].astype(float).shift(1).rolling(20).median()
    rows: list[dict[str, float]] = []
    for pos in range(len(df) - 3, len(df)):
        row = df.iloc[pos]
        candle_range = max(float(row["high"] - row["low"]), 1e-12)
        location = float(row["close"] - row["low"]) / candle_range
        evidence = sum(
            (
                float(row["close"]) > float(row["open"]),
                float(row["close"]) > float(ema9.iloc[pos]),
                float(row["close"]) > float(close.iloc[pos - 1]),
                location >= 0.60,
            )
        )
        base = float(volume_base.iloc[pos])
        rows.append(
            {
                "evidence": float(evidence),
                "rvol": float(row["volume"]) / base if base > 0 else 0.0,
                "location": location,
            }
        )
    return rows


def detect_ma200_volume_breakout(
    df_4h: pd.DataFrame,
    *,
    now: datetime | None = None,
    max_bars_since: int = 42,
    min_breakout_volume_ratio: float = 2.0,
) -> MA200BreakoutSetup:
    """Detect a fresh completed-4h close above MA200 after below-MA basing."""
    d4 = _closed_bars(df_4h, "4h", now)
    required = {"open", "high", "low", "close", "volume"}
    if len(d4) < 230 or not required.issubset(d4.columns):
        return MA200BreakoutSetup(False, "완료된 4h MA200 데이터 부족")

    close = d4["close"].astype(float)
    ma200 = close.rolling(200).mean()
    atr4 = _atr(d4)
    volume_base = d4["volume"].astype(float).shift(1).rolling(30).median()
    candidates: list[MA200BreakoutSetup] = []
    start = max(212, len(d4) - 1 - max_bars_since)
    for pos in range(start, len(d4)):
        ma = float(ma200.iloc[pos])
        atr = float(atr4.iloc[pos])
        base_volume = float(volume_base.iloc[pos])
        if min(ma, atr, base_volume) <= 0 or not all(
            math.isfinite(value) for value in (ma, atr, base_volume)
        ):
            continue
        pre_close = close.iloc[pos - 12:pos]
        pre_ma = ma200.iloc[pos - 12:pos]
        below_ratio = float((pre_close < pre_ma).mean())
        previous_below = float(close.iloc[pos - 1]) <= float(ma200.iloc[pos - 1])
        row = d4.iloc[pos]
        candle_range = max(float(row["high"] - row["low"]), 1e-12)
        close_location = float(row["close"] - row["low"]) / candle_range
        body_atr = float(row["close"] - row["open"]) / atr
        extension_atr = float(row["close"] - ma) / atr
        volume_ratio = float(row["volume"]) / base_volume
        if below_ratio < 0.75 or not previous_below:
            continue
        if float(row["close"]) <= ma + 0.08 * atr:
            continue
        if body_atr < 0.30 or close_location < 0.65:
            continue
        if volume_ratio < min_breakout_volume_ratio:
            continue
        # 이 엔진은 돌파봉을 추격하지 않고 이후 눌림만 산다. 따라서 XEC처럼
        # 돌파봉이 크게 확장된 사례도 보존하되 비정상 급등(8ATR+)만 제외한다.
        if extension_atr > 8.0:
            continue
        bars_since = len(d4) - 1 - pos
        signal_idx = d4.index[pos]
        signal_time = (
            signal_idx.isoformat()
            if hasattr(signal_idx, "isoformat") else str(signal_idx)
        )
        candidates.append(
            MA200BreakoutSetup(
                True,
                f"4h MA200 거래량 돌파 {volume_ratio:.2f}x",
                breakout_bar=f"4h-ma200:{signal_time}",
                breakout_index=pos,
                bars_since_breakout=bars_since,
                below_ma_ratio=round(below_ratio, 5),
                breakout_volume_ratio=round(volume_ratio, 5),
                breakout_body_atr=round(body_atr, 5),
                breakout_extension_atr=round(extension_atr, 5),
                breakout_close=float(row["close"]),
                breakout_ma200=ma,
                atr4h=atr,
            )
        )

    if not candidates:
        return MA200BreakoutSetup(False, "4h MA200 아래 축적→거래량 종가돌파 없음")
    setup = max(candidates, key=lambda item: item.breakout_index)

    post = d4.iloc[setup.breakout_index + 1:]
    if len(post) >= 2:
        post_ma = ma200.iloc[setup.breakout_index + 1:]
        post_atr = atr4.iloc[setup.breakout_index + 1:]
        breakdown = post["close"].astype(float) < post_ma - 0.60 * post_atr
        if bool(breakdown.rolling(2).sum().fillna(0).ge(2).any()):
            return MA200BreakoutSetup(
                False,
                "돌파 후 4h 종가 2개 연속 MA200 하향 이탈로 무효",
                breakout_bar=setup.breakout_bar,
            )
    return setup


def evaluate_ma200_pullback_entry(
    df_4h: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_5m: pd.DataFrame,
    *,
    setup: MA200BreakoutSetup | None = None,
    live_price: float | None = None,
    round_trip_cost: float = 0.0016,
    spread_pct: float | None = None,
    quote_volume_usd: float | None = None,
    min_quote_volume_usd: float = 0.0,
    now: datetime | None = None,
) -> MA200PullbackPlan:
    setup = setup or detect_ma200_volume_breakout(df_4h, now=now)
    identity = {
        "signal_bar": setup.breakout_bar,
        "metrics": {"setup": setup.to_dict()},
    }
    if not setup.eligible:
        return MA200PullbackPlan(False, setup.reason, **identity)
    if setup.bars_since_breakout < 1:
        return MA200PullbackPlan(False, "돌파 4h봉 직후 — 눌림 대기", **identity)

    d4 = _closed_bars(df_4h, "4h", now)
    d15 = _closed_bars(df_15m, "15m", now)
    d5 = _closed_bars(df_5m, "5m", now)
    if len(d4) < 230 or len(d15) < 60 or len(d5) < 50:
        return MA200PullbackPlan(False, "4h/15m/5m 완료봉 데이터 부족", **identity)
    if (
        quote_volume_usd is not None
        and min_quote_volume_usd > 0
        and quote_volume_usd < min_quote_volume_usd
    ):
        return MA200PullbackPlan(
            False,
            f"24h 거래대금 ${quote_volume_usd:,.0f} < ${min_quote_volume_usd:,.0f}",
            **identity,
        )
    if spread_pct is not None and spread_pct > 0.18:
        return MA200PullbackPlan(False, f"스프레드 {spread_pct:.3f}% > 0.18%", **identity)

    close4 = d4["close"].astype(float)
    ma200 = close4.rolling(200).mean()
    middle, _, _ = _bollinger(close4)
    atr4 = float(_atr(d4).iloc[-1])
    atr15 = float(_atr(d15).iloc[-1])
    atr5 = float(_atr(d5).iloc[-1])
    ma = float(ma200.iloc[-1])
    middle_band = float(middle.iloc[-1])
    entry = float(live_price or d5["close"].iloc[-1])
    if min(entry, ma, middle_band, atr4, atr15, atr5) <= 0:
        return MA200PullbackPlan(False, "가격/MA200/볼밴/ATR 계산 실패", **identity)

    latest4 = float(close4.iloc[-1])
    if latest4 < ma - 0.45 * atr4:
        return MA200PullbackPlan(False, "4h 종가가 MA200 허용범위 아래 — 돌파 유지 실패", **identity)
    post_high = float(d4["high"].iloc[setup.breakout_index:].max())
    pullback_depth_atr = (post_high - entry) / atr4
    if pullback_depth_atr < 0.35:
        return MA200PullbackPlan(
            False, f"돌파 후 눌림 {pullback_depth_atr:.2f}ATR < 0.35ATR", **identity
        )

    ma_zone = ma - 0.25 * atr4 <= entry <= ma + 0.35 * atr4
    # 돌파 이후 가격이 위에서 내려와 4h 볼린저 중단(20SMA)을 재시험하는 구간이다.
    # 중단선 접촉만으로 매수하지 않고 아래 15m/5m 반등 확인을 반드시 통과시킨다.
    bb_zone = middle_band - 0.15 * atr4 <= entry <= middle_band + 0.25 * atr4
    # 두 허용구간이 겹치면 현재가에 실제로 더 가까운 기준선을 채택한다.
    # 이에 따라 손절 기준도 선택된 지지선에 맞춰 일관되게 계산된다.
    use_bb_zone = bb_zone and (not ma_zone or abs(entry - middle_band) <= abs(entry - ma))
    if use_bb_zone:
        zone = "bb_middle_retest"
        zone_label = "4h 볼린저 중단 재지지"
        thesis_floor = middle_band - 0.15 * atr4
    elif ma_zone:
        zone = "ma200_retest"
        zone_label = "4h MA200 재지지"
        thesis_floor = ma - 0.25 * atr4
    else:
        distance_ma = (entry - ma) / atr4
        distance_bb = (entry - middle_band) / atr4
        return MA200PullbackPlan(
            False,
            f"눌림 매수구간 아님 (MA200 {distance_ma:+.2f}ATR, BB중단 {distance_bb:+.2f}ATR)",
            **identity,
        )

    rows15 = _long_reversal_rows(d15)
    rows5 = _long_reversal_rows(d5)
    best15 = max(float(row["evidence"]) for row in rows15)
    best5 = max(float(row["evidence"]) for row in rows5)
    if best15 < 3 or best5 < 2:
        return MA200PullbackPlan(
            False,
            f"눌림 중 반등 미확인 (15m {best15:.0f}/4, 5m {best5:.0f}/4)",
            volume_ratio_5m=max(float(row["rvol"]) for row in rows5),
            **identity,
        )
    ema9_5 = d5["close"].astype(float).ewm(span=9, adjust=False).mean()
    extension5 = (entry - float(ema9_5.iloc[-1])) / atr5
    if extension5 > 1.20:
        return MA200PullbackPlan(
            False, f"5m 반등 추격 {extension5:.2f}ATR > 1.20ATR", **identity
        )

    local_low = float(d15["low"].iloc[-10:].min()) - 0.15 * atr15
    structural_stop = min(local_low, thesis_floor)
    minimum_risk = max(0.55 * atr15, entry * max(round_trip_cost, 0.0) * 2.5)
    stop = min(structural_stop, entry - minimum_risk)
    risk = entry - stop
    stop_atr = risk / atr15
    stop_pct = risk / entry * 100
    if stop <= 0 or risk <= 0:
        return MA200PullbackPlan(False, "눌림 구조손절 계산 실패", **identity)
    if stop_atr > 3.0 or stop_pct > 4.0:
        return MA200PullbackPlan(
            False, f"눌림 구조손절 과대 {stop_atr:.2f}ATR/{stop_pct:.2f}%", **identity
        )

    first_rr, first_pct, runner_rr, runner_pct = 1.0, 30, 2.6, 70
    weighted_reward_r = first_rr * 0.30 + runner_rr * 0.70
    weighted_gain = weighted_reward_r * risk
    cost_cash = entry * max(round_trip_cost, 0.0)
    net_gain = max(weighted_gain - cost_cash, 0.0)
    net_loss = risk + cost_cash
    required_wr = net_loss / (net_loss + net_gain) if net_gain > 0 else 1.0
    if required_wr > 0.42:
        return MA200PullbackPlan(
            False, f"비용후 손익분기 승률 {required_wr*100:.1f}% > 42%", **identity
        )

    max_rvol5 = max(float(row["rvol"]) for row in rows5)
    votes = (
        "ma200_breakout",
        "breakout_volume",
        zone,
        "15m_5m_reversal",
    )
    score = (
        68
        + min(max(setup.breakout_volume_ratio - 2.0, 0.0) * 4, 12)
        + min(best15 + best5, 8)
        - max(setup.bars_since_breakout - 6, 0) * 0.5
    )
    metrics = {
        "setup": setup.to_dict(),
        "zone": zone,
        "zone_label": zone_label,
        "ma200_4h": ma,
        "bb_middle_4h": middle_band,
        "pullback_depth_atr4h": round(pullback_depth_atr, 5),
        "reversal_15m": best15,
        "reversal_5m": best5,
        "trigger_extension_atr": round(extension5, 5),
        "trail_atr_mult": 1.10,
        "max_hold_minutes": 720,
        "progress_check_minutes": 240,
        "progress_min_r": 0.25,
        "trail_activation_r": 1.50,
    }
    return MA200PullbackPlan(
        True,
        f"4h MA200 거래량 돌파 후 {zone_label} + 15m/5m 반등",
        divergence_kind=zone,
        indicator_votes=votes,
        score=round(score, 2),
        entry=entry,
        stop=stop,
        tps=(
            {"price": entry + first_rr * risk, "pct": first_pct, "rr": first_rr},
            {"price": entry + runner_rr * risk, "pct": runner_pct, "rr": runner_rr},
        ),
        atr=atr15,
        atr_pct=round(atr15 / entry * 100, 5),
        stop_atr=round(stop_atr, 5),
        stop_pct=round(stop_pct, 5),
        volume_ratio_5m=round(max_rvol5, 5),
        required_win_rate=round(required_wr, 6),
        weighted_reward_r=round(weighted_reward_r, 4),
        signal_bar=setup.breakout_bar,
        context_votes={"4h_ma200_breakout": 1, "pullback_zone": 1, "reversal": 1},
        metrics=metrics,
    )
