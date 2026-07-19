"""Cost-aware short-horizon trend/pullback engine.

This module is deliberately independent from the legacy ``confirmed_count``
stack.  It only consumes OHLCV data, returns a deterministic trade plan, and
evaluates the *current engine version* for live promotion.  Older strategy
versions can never lend performance to a new version.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import math
from typing import Any

import pandas as pd


ENGINE_VERSION = "2026-07-18-s1-cost-aware-pullback"
STRATEGY = "SCALP_TREND_PULLBACK"


_TF_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}


@dataclass(frozen=True)
class ScalpPlan:
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
    trend_strength: float = 0.0
    pullback_depth_atr: float = 0.0
    trigger_strength: float = 0.0
    required_win_rate: float = 1.0
    signal_bar: str = ""
    metrics: dict[str, float] | None = None

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
    expectancy_usd: float
    conservative_mean_r: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _closed_bars(df: pd.DataFrame, timeframe: str, now: datetime | None = None) -> pd.DataFrame:
    """Return completed candles only; never score an exchange's forming bar."""
    if df is None or len(df) == 0:
        return pd.DataFrame()
    out = df.copy()
    seconds = _TF_SECONDS.get(timeframe)
    if not seconds or not isinstance(out.index, pd.DatetimeIndex):
        return out
    now = now or datetime.now(timezone.utc)
    now_ts = pd.Timestamp(now)
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


def _safe_ratio(numerator: float, denominator: float) -> float:
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator <= 0:
        return 0.0
    return numerator / denominator


def evaluate_scalp(
    df_15m: pd.DataFrame,
    df_5m: pd.DataFrame,
    *,
    live_price: float | None = None,
    round_trip_cost: float = 0.0011,
    spread_pct: float | None = None,
    now: datetime | None = None,
) -> ScalpPlan:
    """Evaluate one symmetric, closed-candle trend/pullback setup.

    Hard gates are limited to market structure, executable stop distance and
    cost.  The remaining evidence is additive so a single lagging indicator
    cannot silently turn the engine into a no-trade system.
    """
    d15 = _closed_bars(df_15m, "15m", now)
    d5 = _closed_bars(df_5m, "5m", now)
    if len(d15) < 80 or len(d5) < 60:
        return ScalpPlan(False, "완료봉 데이터 부족")

    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(d15.columns) or not required.issubset(d5.columns):
        return ScalpPlan(False, "OHLCV 컬럼 부족")

    close15 = d15["close"].astype(float)
    ema20 = close15.ewm(span=20, adjust=False).mean()
    ema50 = close15.ewm(span=50, adjust=False).mean()
    atr15s = _atr(d15)
    atr15 = float(atr15s.iloc[-1])
    signal_close = float(close15.iloc[-1])
    entry = float(live_price or signal_close)
    if atr15 <= 0 or entry <= 0:
        return ScalpPlan(False, "ATR/가격 계산 실패")

    signal_bar = d15.index[-1].isoformat() if hasattr(d15.index[-1], "isoformat") else str(d15.index[-1])
    atr_pct = atr15 / entry * 100
    signed_trend_strength = (float(ema20.iloc[-1]) - float(ema50.iloc[-1])) / atr15
    signed_ema20_slope = (float(ema20.iloc[-1]) - float(ema20.iloc[-4])) / atr15
    if ema20.iloc[-1] > ema50.iloc[-1] and signed_ema20_slope > 0:
        direction = "LONG"
    elif ema20.iloc[-1] < ema50.iloc[-1] and signed_ema20_slope < 0:
        direction = "SHORT"
    else:
        return ScalpPlan(
            False, "15m 방향성 추세 미확인", atr=atr15,
            atr_pct=atr_pct, signal_bar=signal_bar,
        )
    trend_strength = abs(signed_trend_strength)
    ema20_slope = abs(signed_ema20_slope)

    # Hard market-structure gates: trade in the established direction and do
    # not buy a volatility shock or an already extended print.
    if atr_pct < 0.12 or atr_pct > 3.0:
        return ScalpPlan(False, f"15m 변동성 범위 이탈 ATR {atr_pct:.2f}%", direction=direction, atr=atr15, atr_pct=atr_pct, signal_bar=signal_bar)
    live_extension = (
        (entry - float(ema20.iloc[-1])) / atr15
        if direction == "LONG"
        else (float(ema20.iloc[-1]) - entry) / atr15
    )
    if live_extension > 1.35:
        return ScalpPlan(False, f"실시간 가격 EMA20 대비 {live_extension:.2f}ATR 추격", direction=direction, atr=atr15, atr_pct=atr_pct, signal_bar=signal_bar)
    if spread_pct is not None and spread_pct > 0.18:
        return ScalpPlan(False, f"스프레드 {spread_pct:.3f}% > 0.18%", direction=direction, atr=atr15, atr_pct=atr_pct, signal_bar=signal_bar)

    recent = d15.iloc[-7:-1]
    recent_ema = ema20.iloc[-7:-1]
    pullback_probe = recent["low"] if direction == "LONG" else recent["high"]
    pullback_gap = float((pullback_probe - recent_ema).abs().min())
    pullback_depth_atr = pullback_gap / atr15
    pullback_seen = bool(
        (recent["low"] <= recent_ema + 0.45 * atr15).any()
        if direction == "LONG"
        else (recent["high"] >= recent_ema - 0.45 * atr15).any()
    )
    held_trend = bool(
        float(recent["low"].min()) > float(ema50.iloc[-1]) - 0.65 * atr15
        if direction == "LONG"
        else float(recent["high"].max()) < float(ema50.iloc[-1]) + 0.65 * atr15
    )

    last15 = d15.iloc[-1]
    directional_reclaim = bool(
        (
            float(last15["close"]) > float(last15["open"])
            and float(last15["close"]) >= float(ema20.iloc[-1])
        )
        if direction == "LONG"
        else (
            float(last15["close"]) < float(last15["open"])
            and float(last15["close"]) <= float(ema20.iloc[-1])
        )
    )
    vol_base = float(d15["volume"].iloc[-21:-1].median())
    volume_ratio = _safe_ratio(float(last15["volume"]), vol_base)

    close5 = d5["close"].astype(float)
    ema9_5 = close5.ewm(span=9, adjust=False).mean()
    ema21_5 = close5.ewm(span=21, adjust=False).mean()
    atr5 = float(_atr(d5).iloc[-1])
    last5 = d5.iloc[-1]
    vol5_base = float(d5["volume"].iloc[-21:-1].median())
    vol5_ratio = _safe_ratio(float(last5["volume"]), vol5_base)
    trigger_strength = (
        (
            float(close5.iloc[-1]) - float(ema21_5.iloc[-1])
            if direction == "LONG"
            else float(ema21_5.iloc[-1]) - float(close5.iloc[-1])
        ) / atr5
        if atr5 > 0 else 0.0
    )
    trigger_ok = bool(
        (
            close5.iloc[-1] > ema9_5.iloc[-1] > ema21_5.iloc[-1]
            and close5.iloc[-1] > close5.iloc[-2]
            and float(last5["close"]) > float(last5["open"])
        )
        if direction == "LONG"
        else (
            close5.iloc[-1] < ema9_5.iloc[-1] < ema21_5.iloc[-1]
            and close5.iloc[-1] < close5.iloc[-2]
            and float(last5["close"]) < float(last5["open"])
        )
    )
    trigger_extension = (
        float(close5.iloc[-1] - ema9_5.iloc[-1])
        if direction == "LONG"
        else float(ema9_5.iloc[-1] - close5.iloc[-1])
    )
    trigger_not_chased = bool(atr5 > 0 and trigger_extension / atr5 <= 1.15)

    score = 0.0
    score += 22.0 if trend_strength >= 0.35 else (14.0 if trend_strength > 0 else 0.0)
    score += 8.0 if ema20_slope >= 0.12 else 4.0
    score += 20.0 if pullback_seen else 0.0
    score += 8.0 if held_trend else 0.0
    score += 12.0 if directional_reclaim else 0.0
    score += 18.0 if trigger_ok else 0.0
    score += 5.0 if trigger_not_chased else 0.0
    score += 4.0 if 1.0 <= volume_ratio <= 4.5 else (2.0 if volume_ratio >= 0.75 else 0.0)
    score += 3.0 if vol5_ratio >= 0.8 else 0.0

    if not pullback_seen or not held_trend:
        return ScalpPlan(False, "방향 추세 내 눌림 구조 미확인", score=score, direction=direction, atr=atr15, atr_pct=atr_pct, signal_bar=signal_bar)
    if not directional_reclaim or not trigger_ok or not trigger_not_chased:
        return ScalpPlan(False, "15m 방향재개 또는 5m 재가속 미확인", score=score, direction=direction, atr=atr15, atr_pct=atr_pct, signal_bar=signal_bar)
    if score < 72.0:
        return ScalpPlan(False, f"비용후 진입점수 {score:.0f}/100 < 72", score=score, direction=direction, atr=atr15, atr_pct=atr_pct, signal_bar=signal_bar)

    if direction == "LONG":
        swing_extreme = float(d15["low"].iloc[-7:].min())
        structural_stop = swing_extreme - 0.12 * atr15
        minimum_stop = entry - 0.70 * atr15
        stop = min(structural_stop, minimum_stop)
        risk = entry - stop
    else:
        swing_extreme = float(d15["high"].iloc[-7:].max())
        structural_stop = swing_extreme + 0.12 * atr15
        minimum_stop = entry + 0.70 * atr15
        stop = max(structural_stop, minimum_stop)
        risk = stop - entry
    stop_atr = risk / atr15
    stop_pct = risk / entry * 100
    if stop <= 0 or stop_atr > 2.0 or stop_pct > 2.5:
        return ScalpPlan(False, f"구조손절 과대 {stop_atr:.2f}ATR/{stop_pct:.2f}%", score=score, direction=direction, atr=atr15, atr_pct=atr_pct, signal_bar=signal_bar)

    # A 60/40 split gives 1.52R gross weighted reward.  Entry is rejected if
    # fees make the implied break-even win rate too demanding.
    side = 1.0 if direction == "LONG" else -1.0
    tp1 = entry + side * 1.20 * risk
    tp2 = entry + side * 2.00 * risk
    weighted_gain = 0.60 * abs(tp1 - entry) + 0.40 * abs(tp2 - entry)
    cost_cash = entry * max(round_trip_cost, 0.0)
    net_gain = max(weighted_gain - cost_cash, 0.0)
    net_loss = risk + cost_cash
    required_wr = net_loss / (net_loss + net_gain) if net_gain > 0 else 1.0
    if required_wr > 0.47:
        return ScalpPlan(False, f"비용후 손익분기 승률 {required_wr*100:.1f}% 과다", score=score, direction=direction, atr=atr15, atr_pct=atr_pct, signal_bar=signal_bar)

    tps = (
        {"price": tp1, "pct": 60, "rr": 1.2},
        {"price": tp2, "pct": 40, "rr": 2.0},
    )
    return ScalpPlan(
        True,
        "closed-candle trend/pullback/trigger 통과",
        score=round(score, 2),
        direction=direction,
        entry=entry,
        stop=stop,
        tps=tps,
        atr=atr15,
        atr_pct=round(atr_pct, 4),
        stop_pct=round(stop_pct, 4),
        stop_atr=round(stop_atr, 4),
        volume_ratio=round(volume_ratio, 4),
        trend_strength=round(trend_strength, 4),
        pullback_depth_atr=round(pullback_depth_atr, 4),
        trigger_strength=round(trigger_strength, 4),
        required_win_rate=round(required_wr, 6),
        signal_bar=signal_bar,
        metrics={
            "ema20_slope_atr": round(ema20_slope, 4),
            "live_extension_atr": round(live_extension, 4),
            "volume_ratio_15m": round(volume_ratio, 4),
            "volume_ratio_5m": round(vol5_ratio, 4),
        },
    )


def _history_path(root: Path, venue: str) -> Path:
    return root / ("trade_state_binance.json" if venue == "binance" else "trade_state.json")


def _current_version_rows(root: Path, venue: str, engine_version: str) -> list[dict]:
    path = _history_path(root, venue)
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8")).get("trade_history", [])
    except Exception:
        return []
    result = []
    for row in rows:
        if row.get("status") not in {"win", "loss"}:
            continue
        ctx = row.get("entry_context") or {}
        version = row.get("engine_version") or ctx.get("engine_version")
        if version == engine_version and row.get("strategy") == STRATEGY:
            result.append(row)
    return result


def evaluate_live_permission(
    *,
    root: Path,
    venue: str,
    engine_version: str = ENGINE_VERSION,
    binance_canary_enabled: bool = False,
) -> LivePermission:
    """Current-version-only canary/probation/champion gate."""
    venue = str(venue or "bybit").lower()
    rows = _current_version_rows(root, venue, engine_version)
    pnls = [float(row.get("pnl_usd") or 0.0) for row in rows]
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    pnl = sum(pnls)
    pf = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    expectancy = pnl / len(rows) if rows else 0.0
    r_values = []
    for row in rows:
        risk = float(row.get("est_sl_loss") or (row.get("entry_context") or {}).get("est_sl_loss") or 0.0)
        if risk > 0:
            r_values.append(float(row.get("pnl_usd") or 0.0) / risk)
    conservative_r = None
    if r_values:
        mean_r = sum(r_values) / len(r_values)
        if len(r_values) > 1:
            variance = sum((value - mean_r) ** 2 for value in r_values) / (len(r_values) - 1)
            conservative_r = mean_r - math.sqrt(variance) / math.sqrt(len(r_values))
        else:
            conservative_r = mean_r

    base = dict(
        closed=len(rows),
        pnl_usd=round(pnl, 8),
        profit_factor=round(pf, 8) if math.isfinite(pf) else 999.0,
        expectancy_usd=round(expectancy, 8),
        conservative_mean_r=round(conservative_r, 8) if conservative_r is not None else None,
    )
    if venue == "binance" and not binance_canary_enabled:
        return LivePermission(False, "shadow", 0.0, "Binance 신규 엔진은 API 복구·별도 승인 전 shadow", **base)

    if len(rows) < 8:
        return LivePermission(
            True,
            "canary",
            0.0025,
            f"현 버전 실체결 OOS {len(rows)}/8건 — 계좌위험 0.25% 고정",
            **base,
        )
    if len(rows) < 20:
        if pnl <= 0 or expectancy <= 0 or pf < 1.0 or (conservative_r is not None and conservative_r <= -0.10):
            return LivePermission(False, "shadow", 0.0, f"canary 조기중단 n={len(rows)} PF={pf:.2f} E=${expectancy:+.3f}", **base)
        return LivePermission(True, "probation", 0.0035, f"canary 1차통과 n={len(rows)} PF={pf:.2f}", **base)

    if pnl <= 0 or expectancy <= 0 or pf < 1.15 or (conservative_r is None or conservative_r <= 0):
        return LivePermission(False, "shadow", 0.0, f"정식 승격 실패 n={len(rows)} PF={pf:.2f} E=${expectancy:+.3f}", **base)
    return LivePermission(True, "champion", 0.0050, f"현 버전 정식 승격 n={len(rows)} PF={pf:.2f}", **base)
