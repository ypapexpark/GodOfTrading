"""Closed-candle lower-TF reacceleration triggers (1m/5m).

Used by:
  - EMA 15m core live path (timing layer)
  - Micro-scalp engine (setup + trigger)

Never scores the exchange's still-forming bar.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd


_TF_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
}


@dataclass(frozen=True)
class TriggerDecision:
    ok: bool
    reason: str
    timeframe: str = "1m"
    direction: str = ""
    strength: float = 0.0
    volume_ratio: float = 0.0
    extension_atr: float = 0.0
    signal_bar: str = ""
    metrics: dict[str, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def closed_bars(
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


def evaluate_reaccel_trigger(
    df: pd.DataFrame,
    direction: str,
    *,
    timeframe: str = "1m",
    now: datetime | None = None,
    min_volume_ratio: float = 0.75,
    max_extension_atr: float = 1.25,
    require_ema_stack: bool = True,
) -> TriggerDecision:
    """Confirm completed-bar reacceleration in ``direction``.

    LONG: close > EMA9 > EMA21 (optional stack), green close, rising close,
    volume participation, not already extended past max_extension_atr.
    SHORT is the mirror.
    """
    side = str(direction or "").upper()
    base = TriggerDecision(False, "init", timeframe=timeframe, direction=side)
    if side not in {"LONG", "SHORT"}:
        return TriggerDecision(False, f"방향 오류: {direction}", timeframe=timeframe)

    d = closed_bars(df, timeframe, now)
    if len(d) < 40:
        return TriggerDecision(
            False, f"{timeframe} 완료봉 부족 ({len(d)})", timeframe=timeframe, direction=side
        )
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(d.columns):
        return TriggerDecision(False, "OHLCV 컬럼 부족", timeframe=timeframe, direction=side)

    close = d["close"].astype(float)
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    atr = float(_atr(d).iloc[-1])
    last = d.iloc[-1]
    entry = float(close.iloc[-1])
    if atr <= 0 or entry <= 0:
        return TriggerDecision(False, "ATR/가격 실패", timeframe=timeframe, direction=side)

    vol_base = float(d["volume"].iloc[-21:-1].median())
    vol_ratio = float(last["volume"]) / vol_base if vol_base > 0 else 0.0
    signal_bar = (
        d.index[-1].isoformat()
        if hasattr(d.index[-1], "isoformat")
        else str(d.index[-1])
    )

    if side == "LONG":
        stack_ok = bool(close.iloc[-1] > ema9.iloc[-1] > ema21.iloc[-1])
        soft_stack = bool(close.iloc[-1] > ema9.iloc[-1] and ema9.iloc[-1] >= ema21.iloc[-1] * 0.999)
        candle_ok = bool(float(last["close"]) > float(last["open"]))
        rising = bool(close.iloc[-1] > close.iloc[-2])
        strength = (float(close.iloc[-1]) - float(ema21.iloc[-1])) / atr
        extension = (float(close.iloc[-1]) - float(ema9.iloc[-1])) / atr
    else:
        stack_ok = bool(close.iloc[-1] < ema9.iloc[-1] < ema21.iloc[-1])
        soft_stack = bool(close.iloc[-1] < ema9.iloc[-1] and ema9.iloc[-1] <= ema21.iloc[-1] * 1.001)
        candle_ok = bool(float(last["close"]) < float(last["open"]))
        rising = bool(close.iloc[-1] < close.iloc[-2])
        strength = (float(ema21.iloc[-1]) - float(close.iloc[-1])) / atr
        extension = (float(ema9.iloc[-1]) - float(close.iloc[-1])) / atr

    ema_ok = stack_ok if require_ema_stack else soft_stack
    metrics = {
        "volume_ratio": round(vol_ratio, 4),
        "strength_atr": round(float(strength), 4),
        "extension_atr": round(float(extension), 4),
        "atr": round(atr, 8),
    }

    if not ema_ok:
        return TriggerDecision(
            False,
            f"{timeframe} EMA 스택 미정렬",
            timeframe=timeframe,
            direction=side,
            strength=float(strength),
            volume_ratio=vol_ratio,
            extension_atr=float(extension),
            signal_bar=signal_bar,
            metrics=metrics,
        )
    if not candle_ok:
        return TriggerDecision(
            False,
            f"{timeframe} 방향 캔들 미확인",
            timeframe=timeframe,
            direction=side,
            strength=float(strength),
            volume_ratio=vol_ratio,
            extension_atr=float(extension),
            signal_bar=signal_bar,
            metrics=metrics,
        )
    if not rising:
        return TriggerDecision(
            False,
            f"{timeframe} 직전봉 대비 재가속 미확인",
            timeframe=timeframe,
            direction=side,
            strength=float(strength),
            volume_ratio=vol_ratio,
            extension_atr=float(extension),
            signal_bar=signal_bar,
            metrics=metrics,
        )
    if vol_ratio < float(min_volume_ratio):
        return TriggerDecision(
            False,
            f"{timeframe} 거래량 {vol_ratio:.2f}x < {float(min_volume_ratio):.2f}x",
            timeframe=timeframe,
            direction=side,
            strength=float(strength),
            volume_ratio=vol_ratio,
            extension_atr=float(extension),
            signal_bar=signal_bar,
            metrics=metrics,
        )
    if extension > float(max_extension_atr):
        return TriggerDecision(
            False,
            f"{timeframe} 추격 과열 {extension:.2f}ATR > {float(max_extension_atr):.2f}",
            timeframe=timeframe,
            direction=side,
            strength=float(strength),
            volume_ratio=vol_ratio,
            extension_atr=float(extension),
            signal_bar=signal_bar,
            metrics=metrics,
        )

    return TriggerDecision(
        True,
        f"{timeframe} 재가속 트리거 OK (vol {vol_ratio:.2f}x, ext {extension:.2f}ATR)",
        timeframe=timeframe,
        direction=side,
        strength=float(strength),
        volume_ratio=vol_ratio,
        extension_atr=float(extension),
        signal_bar=signal_bar,
        metrics=metrics,
    )
