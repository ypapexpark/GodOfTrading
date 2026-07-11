"""Market regime classifier + strategy router (Principles catalog P1).

국면: trend / range / high_vol / mixed
측정: ADX(14), ATR% percentile, EMA20 기울기.

라이브 정책(v1):
  - trend: EMA 계열·hidden 정상
  - range: EMA 사이즈 감액, hidden paper (continuation은 추세 필요)
  - high_vol: 전 전략 사이즈 감액, 평균회귀 계열 hard paper
  - mixed: 소폭 감액
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from config import (
    REGIME_ADX_TREND,
    REGIME_ADX_RANGE,
    REGIME_ATR_PCTILE_HIGH,
    REGIME_ATR_PCTILE_RANGE_MAX,
    REGIME_EMA_SLOPE_TREND_PCT,
    REGIME_RANGE_EMA_RISK_MULT,
    REGIME_HIGH_VOL_RISK_MULT,
    REGIME_MIXED_RISK_MULT,
    REGIME_RANGE_BLOCK_HIDDEN,
    REGIME_HIGH_VOL_BLOCK_MEANREV,
)


def _true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev = close.shift(1)
    return pd.concat(
        [high - low, (high - prev).abs(), (low - prev).abs()],
        axis=1,
    ).max(axis=1)


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = _true_range(df)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def calc_adx(df: pd.DataFrame, period: int = 14) -> float:
    """Wilder-style ADX. 데이터 부족 시 0."""
    if df is None or len(df) < period + 5:
        return 0.0
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = _true_range(df)
    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean() / atr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean() / atr.replace(0, np.nan)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    adx = dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    val = float(adx.iloc[-1]) if len(adx) else 0.0
    return 0.0 if np.isnan(val) else round(val, 2)


def classify_regime(df: pd.DataFrame | None, tf_key: str = "") -> dict:
    """
    Returns:
      regime: trend|range|high_vol|mixed|unknown
      adx, atr_pct, atr_percentile, ema_slope_pct, trend_dir, note
    """
    empty = {
        "regime": "unknown",
        "adx": 0.0,
        "atr_pct": 0.0,
        "atr_percentile": 50.0,
        "ema_slope_pct": 0.0,
        "trend_dir": 0,
        "note": "데이터 부족",
        "tf": tf_key,
    }
    if df is None or len(df) < 40:
        return empty

    close = df["close"].astype(float)
    atr = calc_atr(df, 14)
    atr_now = float(atr.iloc[-1] or 0)
    px = float(close.iloc[-1] or 0)
    atr_pct = (atr_now / px * 100) if px > 0 else 0.0

    # ATR% 백분위 (최근 50봉)
    atr_pct_series = (atr / close.replace(0, np.nan) * 100).tail(50).dropna()
    if len(atr_pct_series) >= 10:
        atr_percentile = float((atr_pct_series <= atr_pct).mean() * 100)
    else:
        atr_percentile = 50.0

    ema20 = close.ewm(span=20, adjust=False).mean()
    look = 5
    e0 = float(ema20.iloc[-1])
    e1 = float(ema20.iloc[-1 - look]) if len(ema20) > look else e0
    ema_slope_pct = ((e0 - e1) / e1 * 100) if e1 else 0.0
    if ema_slope_pct > REGIME_EMA_SLOPE_TREND_PCT:
        trend_dir = 1
    elif ema_slope_pct < -REGIME_EMA_SLOPE_TREND_PCT:
        trend_dir = -1
    else:
        trend_dir = 0

    adx = calc_adx(df, 14)

    # 우선순위: high_vol > trend > range > mixed
    if atr_percentile >= REGIME_ATR_PCTILE_HIGH:
        regime = "high_vol"
        note = f"고변동 ATR%ile={atr_percentile:.0f} ADX={adx:.1f}"
    elif adx >= REGIME_ADX_TREND and trend_dir != 0:
        regime = "trend"
        note = (
            f"추세 ADX={adx:.1f} slope={ema_slope_pct:+.2f}% "
            f"dir={'↑' if trend_dir > 0 else '↓'}"
        )
    elif adx <= REGIME_ADX_RANGE and atr_percentile <= REGIME_ATR_PCTILE_RANGE_MAX:
        regime = "range"
        note = f"횡보 ADX={adx:.1f} ATR%ile={atr_percentile:.0f}"
    else:
        regime = "mixed"
        note = f"혼합 ADX={adx:.1f} ATR%ile={atr_percentile:.0f} slope={ema_slope_pct:+.2f}%"

    return {
        "regime": regime,
        "adx": adx,
        "atr_pct": round(atr_pct, 4),
        "atr_percentile": round(atr_percentile, 1),
        "ema_slope_pct": round(ema_slope_pct, 3),
        "trend_dir": trend_dir,
        "note": note,
        "tf": tf_key,
    }


def _is_ema_family(strategy: str) -> bool:
    return "EMA눌림목" in str(strategy or "")


def _is_hidden(strategy: str, signal_type: str) -> bool:
    st = str(signal_type or "")
    sy = str(strategy or "")
    return st in ("hidden_bullish", "hidden_bearish") or sy in (
        "hidden_bullish", "hidden_bearish",
    )


def _is_meanrev(strategy: str, signal_type: str) -> bool:
    st = str(signal_type or "")
    sy = str(strategy or "")
    if st in (
        "rsi2_reversion_long", "rsi2_reversion_short",
        "vwap_reversion_long", "vwap_reversion_short",
        "bullish", "bearish",
    ):
        return True
    if sy in ("RSI2반전", "VWAP회귀", "bullish", "bearish"):
        return True
    return False


def apply_regime_to_trade(
    regime: dict | None,
    strategy: str,
    signal_type: str,
    direction: str,
) -> dict:
    """
    Returns:
      allow (bool): False면 paper_only 또는 hard block (caller 결정)
      paper_only (bool)
      risk_mult (float)
      notes (list[str])
    """
    if not regime or regime.get("regime") in (None, "unknown"):
        return {
            "allow": True,
            "paper_only": False,
            "risk_mult": 1.0,
            "notes": ["레짐 unknown — 라우터 스킵"],
        }

    r = regime.get("regime", "mixed")
    notes: list[str] = [f"레짐 {r}: {regime.get('note', '')}"]
    risk_mult = 1.0
    paper_only = False
    allow = True

    ema = _is_ema_family(strategy)
    hid = _is_hidden(strategy, signal_type)
    mrev = _is_meanrev(strategy, signal_type)

    if r == "trend":
        if mrev:
            paper_only = True
            notes.append("추세 국면 — 평균회귀 paper_only (P1/P5)")
        else:
            notes.append("추세 국면 — 순응/지속 전략 정상")

    elif r == "range":
        if hid and REGIME_RANGE_BLOCK_HIDDEN:
            paper_only = True
            notes.append("횡보 — hidden continuation paper_only (P1/P4)")
        if ema:
            risk_mult *= REGIME_RANGE_EMA_RISK_MULT
            notes.append(
                f"횡보 — EMA 사이즈×{REGIME_RANGE_EMA_RISK_MULT:.2f} (휩쏘 방어)"
            )
        if mrev:
            notes.append("횡보 — 평균회귀 후보 (현행 관찰모드 정책 유지)")

    elif r == "high_vol":
        risk_mult *= REGIME_HIGH_VOL_RISK_MULT
        notes.append(f"고변동 — 전역 리스크×{REGIME_HIGH_VOL_RISK_MULT:.2f}")
        if mrev and REGIME_HIGH_VOL_BLOCK_MEANREV:
            paper_only = True
            notes.append("고변동 — 평균회귀 paper_only")

    else:  # mixed
        risk_mult *= REGIME_MIXED_RISK_MULT
        notes.append(f"혼합 국면 — 리스크×{REGIME_MIXED_RISK_MULT:.2f}")

    # 추세 방향과 완전 역행 EMA/hidden (trend 국면에서만 추가 감액)
    tdir = int(regime.get("trend_dir") or 0)
    if r == "trend" and tdir != 0 and (ema or hid):
        want = 1 if str(direction).upper() == "LONG" else -1
        if want != tdir:
            risk_mult *= 0.70
            notes.append("레짐 추세 역방향 — 추가 리스크×0.70")

    return {
        "allow": allow,
        "paper_only": paper_only,
        "risk_mult": round(risk_mult, 4),
        "notes": notes,
        "regime": r,
    }
