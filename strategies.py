"""
추가 진입 전략 — 다이버전스 외 고빈도 자리 포착
1. RSI 과매도/과매수 반전 (스캘핑 주력)
2. EMA 눌림목 진입 (추세 순응 단타)
3. BB 스퀴즈/구조 돌파/거래량 급등 추세

프로 트레이더 원칙:
  다이버전스 = 반전 예고 (드물지만 강력)
  RSI 극단   = 즉각 반전 (잦고 빠름) → 스캘핑 핵심
  EMA 눌림목 = 추세 지속 매매 (승률 최고) → 단타 스윙 핵심
"""
from __future__ import annotations
import time
import pandas as pd
from config import (BB_MID_3D_LOOKBACK, BB_MID_3D_MIN_ABOVE,
                    BB_MID_PULLBACK_TF, BB_MID_WEEK_LOOKBACK,
                    BB_MID_WEEK_MIN_ABOVE, VOLUME_MOMENTUM_BODY_ATR,
                    VOLUME_MOMENTUM_LOOKBACK, VOLUME_MOMENTUM_MIN_VOL,
                    VOLUME_MOMENTUM_TF,
                    PARABOLIC_CYCLE_ENABLED, PARABOLIC_CYCLE_TF,
                    PARABOLIC_IGNITION_MIN_VOL, PARABOLIC_IGNITION_MIN_BODY_RATIO,
                    PARABOLIC_IGNITION_MIN_BAR_GAIN, PARABOLIC_IGNITION_MAX_VWAP_DISLOC,
                    PARABOLIC_REVERSAL_MIN_VWAP_DISLOC, PARABOLIC_REVERSAL_MIN_RSI,
                    PARABOLIC_REVERSAL_LOOKBACK, PARABOLIC_REVERSAL_MIN_PUMP_PCT,
                    PARABOLIC_REVERSAL_MIN_UPWICK, PARABOLIC_REVERSAL_RECENT,
                    PARABOLIC_TP_SCHEME,
                    VWAP_REVERSION_TF, VWAP_REVERSION_MIN_DISLOC,
                    VWAP_REVERSION_MAX_DISLOC, VWAP_REVERSION_RSI_LONG_MIN,
                    VWAP_REVERSION_RSI_LONG_MAX, VWAP_REVERSION_RSI_SHORT_MIN,
                    VWAP_REVERSION_RSI_SHORT_MAX, VWAP_REVERSION_MIN_VOL,
                    RSI2_REVERSION_TF, RSI2_PERIOD, RSI2_LONG_THRESHOLD,
                    RSI2_SHORT_THRESHOLD, RSI2_EXTREME_LONG, RSI2_EXTREME_SHORT,
                    RSI2_MIN_VOL)
from divergence import calc_rsi, calc_macd, _ema_trend, calc_vwap


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.tail(period).mean())


def _vol_ratio(df: pd.DataFrame, period: int = 20) -> float:
    vol = df["volume"]
    avg = float(vol.iloc[-(period + 1):-1].mean())
    return float(vol.iloc[-1] / avg) if avg > 0 else 0.0


def macd_hist_alignment(df: pd.DataFrame, direction: str) -> dict:
    """
    EMA 단타용 MACD 히스토그램 정렬 (2026-07-11).
    크로스 필수 아님. LONG: hist>=0 또는 최근 상승 기울기 / SHORT: 반대.
    """
    if df is None or len(df) < 35:
        return {"ok": False, "value": 0.0, "rising": False, "falling": False, "note": "데이터 부족"}
    hist = calc_macd(df["close"])
    h0 = float(hist.iloc[-1])
    h1 = float(hist.iloc[-2]) if len(hist) > 1 else h0
    h2 = float(hist.iloc[-3]) if len(hist) > 2 else h1
    rising = h0 > h1 or (h0 >= h1 and h1 > h2)
    falling = h0 < h1 or (h0 <= h1 and h1 < h2)
    if direction == "LONG":
        ok = (h0 >= 0.0) or rising
        note = f"hist={h0:.6g} {'+' if rising else ''}{'zero+' if h0 >= 0 else 'below0'}"
    else:
        ok = (h0 <= 0.0) or falling
        note = f"hist={h0:.6g} {'-' if falling else ''}{'zero-' if h0 <= 0 else 'above0'}"
    return {
        "ok": bool(ok),
        "value": round(h0, 6),
        "rising": bool(rising),
        "falling": bool(falling),
        "note": note,
    }


def _base_signal(signal_type: str, direction: str, strength: str,
                 confirmed: int, atr_val: float, ema_t: int,
                 pivot: float, vol_r: float, rsi_val: float,
                 strategy: str, macd_info: dict | None = None) -> dict:
    """공통 신호 포맷 — 기존 detect() 반환값과 호환."""
    macd = macd_info or {"ok": False, "value": 0.0}
    return {
        "signal_type":     signal_type,
        "strength":        strength,
        "confirmed_count": confirmed,
        "atr":             round(atr_val, 8),
        "ema_trend":       ema_t,
        "bars_ago":        0,        # 현재봉 신호 = 항상 신선
        "pivot_price":     round(pivot, 4),
        "strategy":        strategy,
        "is_divergence":    False,
        # 기존 formatter 호환용 (ok/value 형식)
        "rsi":  {"ok": rsi_val > 0,  "value": round(rsi_val, 1)},
        "cci":  {"ok": False,         "value": 0.0},
        "macd": {"ok": bool(macd.get("ok")), "value": float(macd.get("value") or 0.0)},
        "macd_align": macd,
        "obv":  {"ok": False},
        "srsi": {"ok": False,         "value": 0},
        "vol":  {"ok": vol_r >= 1.1, "value": round(vol_r, 2)},
        "cvd":  {"ok": False},
    }


_bb_mid_cache: dict = {}
_BB_MID_CACHE_TTL = 1800


def _resample_3d(df: pd.DataFrame) -> pd.DataFrame:
    """1일봉을 3일봉으로 합성한다. 거래소 3d 지원 여부와 무관하게 안정적으로 사용."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.resample("3D").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    })
    return out.dropna()


def _bb_mid_profile(df: pd.DataFrame, lookback: int,
                    min_above: int, label: str) -> dict:
    if df is None or len(df) < 20 + lookback:
        return {
            "ok": False, "label": label, "above": 0, "lookback": lookback,
            "distance_atr": 0.0, "slope_pct": 0.0, "reason": "데이터 부족",
        }

    close = df["close"].astype(float)
    mid = close.rolling(20).mean()
    mid_valid = mid.dropna()
    if len(mid_valid) < lookback:
        return {
            "ok": False, "label": label, "above": 0, "lookback": lookback,
            "distance_atr": 0.0, "slope_pct": 0.0, "reason": "BB 중단 부족",
        }

    recent_close = close.iloc[-lookback:]
    recent_mid = mid.iloc[-lookback:]
    valid = recent_mid.notna()
    above = recent_close[valid] >= recent_mid[valid]
    above_count = int(above.sum())

    latest_close = float(close.iloc[-1])
    latest_mid = float(mid.iloc[-1])
    atr_val = _atr(df)
    distance_atr = (latest_close - latest_mid) / atr_val if atr_val > 0 else 0.0

    slope_ref = float(mid_valid.iloc[-5]) if len(mid_valid) >= 5 else float(mid_valid.iloc[0])
    slope_pct = (latest_mid - slope_ref) / slope_ref * 100 if slope_ref > 0 else 0.0
    latest_above = latest_close >= latest_mid
    ok = above_count >= min_above and latest_above and slope_pct >= -0.20

    return {
        "ok": ok,
        "label": label,
        "above": above_count,
        "lookback": lookback,
        "min_above": min_above,
        "latest_above": latest_above,
        "distance_atr": round(distance_atr, 2),
        "slope_pct": round(slope_pct, 2),
        "reason": (
            f"{label} {above_count}/{lookback}개 BB중단 위, "
            f"거리 {distance_atr:.1f}ATR, 중단기울기 {slope_pct:+.1f}%"
        ),
    }


def get_bb_midline_long_bias(symbol: str) -> dict:
    """
    주봉 + 3일봉 BB 중단 상방 유지 종목을 선별한다.
    조건이 맞으면 하위봉에서는 숏보다 내림롱만 우선 검토한다.
    """
    now = time.time()
    cached = _bb_mid_cache.get(symbol)
    if cached and now - cached["ts"] < _BB_MID_CACHE_TTL:
        return cached["v"]

    try:
        from fetcher import fetch_ohlcv

        weekly = fetch_ohlcv(symbol, "1w", 90)
        daily = fetch_ohlcv(symbol, "1d", 180)
        d3 = _resample_3d(daily)

        week = _bb_mid_profile(
            weekly, BB_MID_WEEK_LOOKBACK, BB_MID_WEEK_MIN_ABOVE, "주봉"
        )
        day3 = _bb_mid_profile(
            d3, BB_MID_3D_LOOKBACK, BB_MID_3D_MIN_ABOVE, "3일봉"
        )

        score = 0
        for p in (week, day3):
            score += 1 if p["above"] >= p.get("min_above", 999) else 0
            score += 1 if p.get("latest_above") else 0
            score += 1 if p["slope_pct"] >= 0 else 0

        ok = week["ok"] and day3["ok"]
        result = {
            "ok": ok,
            "direction": "LONG" if ok else "NEUTRAL",
            "score": score,
            "week": week,
            "day3": day3,
            "note": f"{week['reason']} | {day3['reason']}",
        }
    except Exception as e:
        result = {
            "ok": False, "direction": "NEUTRAL", "score": 0,
            "week": {}, "day3": {}, "note": f"BB중단 조회실패: {e}",
        }

    _bb_mid_cache[symbol] = {"v": result, "ts": now}
    return result


# ─── 전략 1: RSI 극단 반전 ────────────────────────────────────────────────────

def detect_rsi_extreme(df: pd.DataFrame, tf_key: str) -> dict | None:
    """
    RSI 과매도/과매수 반전 신호.

    스캘핑(5m/15m): RSI ≤ 28 / ≥ 72
    단타 스윙(1h+): RSI ≤ 30 / ≥ 70

    추가 조건:
      - 거래량 1.2x 이상 (매도/매수 클라이맥스)
      - 마지막 봉이 반전 방향 (진입 직전 캔들 확인)
      - EMA 역방향이면 RSI 더 극단(≤22 / ≥78)에서만 허용
    """
    if len(df) < 30:
        return None

    close    = df["close"]
    rsi      = calc_rsi(close)
    curr_rsi = float(rsi.iloc[-1])
    atr_val  = _atr(df)
    vol_r    = _vol_ratio(df)
    ema_t    = _ema_trend(close)

    last     = df.iloc[-1]
    last_bull = float(last["close"]) > float(last["open"])

    scalp    = tf_key in ("5m", "15m")
    thr_long  = 28 if scalp else 30
    thr_short = 72 if scalp else 70
    min_vol   = 1.2

    # ── LONG (과매도 반전) ─────────────────────────────────────────────────────
    if curr_rsi <= thr_long and vol_r >= min_vol and last_bull:
        # EMA 역방향(하락 추세)에서는 극단 RSI만 허용
        if ema_t == -1 and curr_rsi > 22:
            return None
        confirmed = 5 if curr_rsi <= 22 else (4 if ema_t == 1 else 3)
        strength  = "VERY STRONG 🔥" if confirmed >= 5 else "STRONG ⚡"
        return _base_signal(
            "rsi_long", "LONG", strength, confirmed,
            atr_val, ema_t, float(df["low"].iloc[-1]),
            vol_r, curr_rsi, "RSI반전",
        )

    # ── SHORT (과매수 반전) ────────────────────────────────────────────────────
    if curr_rsi >= thr_short and vol_r >= min_vol and not last_bull:
        if ema_t == 1 and curr_rsi < 78:
            return None
        confirmed = 5 if curr_rsi >= 78 else (4 if ema_t == -1 else 3)
        strength  = "VERY STRONG 🔥" if confirmed >= 5 else "STRONG ⚡"
        return _base_signal(
            "rsi_short", "SHORT", strength, confirmed,
            atr_val, ema_t, float(df["high"].iloc[-1]),
            vol_r, curr_rsi, "RSI반전",
        )

    return None


# ─── 전략 2: EMA 눌림목 진입 ─────────────────────────────────────────────────

def detect_ema_touch(df: pd.DataFrame, tf_key: str) -> dict | None:
    """
    EMA20 눌림목/반등 매매 — 가장 높은 승률의 추세 순응 전략.

    상승 추세 (EMA20 > EMA50):
      직전봉 저가가 EMA20 근처 → 현재봉 상승 반등 = LONG

    하락 추세 (EMA20 < EMA50):
      직전봉 고가가 EMA20 근처 → 현재봉 하락 재개 = SHORT

    프로 원칙: "추세 방향 눌림목 = 가장 안전한 진입 = 손절 짧고 이익 큼"
    """
    if len(df) < 60:
        return None

    close  = df["close"]
    ema20  = close.ewm(span=20, adjust=False).mean()
    ema50  = close.ewm(span=50, adjust=False).mean()

    c_ema20 = float(ema20.iloc[-1])
    c_ema50 = float(ema50.iloc[-1])
    atr_val = _atr(df)
    vol_r   = _vol_ratio(df)

    last = df.iloc[-1]
    prev = df.iloc[-2]
    last_o, last_c = float(last["open"]), float(last["close"])
    prev_l, prev_h = float(prev["low"]),  float(prev["high"])
    recent = df.iloc[-6:-1]

    touch_zone = atr_val * 0.8   # EMA ± 0.8 ATR = 터치/재진입으로 인정
    body       = abs(last_c - last_o)
    min_body   = atr_val * 0.18  # 현재봉 기반 진입이라 너무 둔하게 보지 않음

    # ── 상승 추세 눌림목 LONG ──────────────────────────────────────────────────
    if c_ema20 > c_ema50 * 1.001:
        pullback_seen = (
            (recent["low"] <= c_ema20 + touch_zone).any()
            or abs(prev_l - c_ema20) <= touch_zone
        )
        candle_bull = last_c > last_o and last_c >= c_ema20 and body >= min_body
        if pullback_seen and candle_bull and vol_r >= 1.0:
            macd_a = macd_hist_alignment(df, "LONG")
            return _base_signal(
                "ema_long", "LONG", "STRONG ⚡", 4,
                atr_val, 1, prev_l, vol_r, 50.0, "EMA눌림목",
                macd_info=macd_a,
            )

    # ── 하락 추세 반등매도 SHORT ───────────────────────────────────────────────
    elif c_ema20 < c_ema50 * 0.999:
        pullback_seen = (
            (recent["high"] >= c_ema20 - touch_zone).any()
            or abs(prev_h - c_ema20) <= touch_zone
        )
        candle_bear = last_c < last_o and last_c <= c_ema20 and body >= min_body
        if pullback_seen and candle_bear and vol_r >= 1.0:
            macd_a = macd_hist_alignment(df, "SHORT")
            return _base_signal(
                "ema_short", "SHORT", "STRONG ⚡", 4,
                atr_val, -1, prev_h, vol_r, 50.0, "EMA눌림목",
                macd_info=macd_a,
            )

    return None


# ─── 전략 2.5: 마이크로 구조 돌파 ────────────────────────────────────────────

def detect_micro_breakout(df: pd.DataFrame, tf_key: str) -> dict | None:
    """
    최근 12봉 구조 돌파.
    피봇 다이버전스보다 빠른 "현재봉 추세 가속" 진입용.
    """
    if len(df) < 80:
        return None

    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    atr_val = _atr(df)
    vol_r   = _vol_ratio(df)
    ema_t   = _ema_trend(close)

    cur_c = float(close.iloc[-1])
    prev_c = float(close.iloc[-2])
    resistance = float(high.iloc[-15:-3].max())
    support    = float(low.iloc[-15:-3].min())

    last3 = df.iloc[-4:-1]
    bull_cnt = int(sum(1 for _, r in last3.iterrows() if r["close"] > r["open"]))
    bear_cnt = int(sum(1 for _, r in last3.iterrows() if r["close"] < r["open"]))

    if cur_c > resistance and prev_c <= resistance and ema_t >= 0 and bull_cnt >= 2 and vol_r >= 1.15:
        confirmed = 5 if vol_r >= 1.5 else 4
        strength  = "VERY STRONG 🔥" if confirmed >= 5 else "STRONG ⚡"
        return _base_signal(
            "micro_breakout_long", "LONG", strength, confirmed,
            atr_val, max(ema_t, 1), float(low.iloc[-1]),
            vol_r, 50.0, "마이크로돌파",
        )

    if cur_c < support and prev_c >= support and ema_t <= 0 and bear_cnt >= 2 and vol_r >= 1.15:
        confirmed = 5 if vol_r >= 1.5 else 4
        strength  = "VERY STRONG 🔥" if confirmed >= 5 else "STRONG ⚡"
        return _base_signal(
            "micro_breakout_short", "SHORT", strength, confirmed,
            atr_val, min(ema_t, -1), float(high.iloc[-1]),
            vol_r, 50.0, "마이크로돌파",
        )

    return None


# ─── 전략 3: BB 스퀴즈 돌파 (VCP 크립토 버전) ───────────────────────────────

def detect_bb_squeeze(df: pd.DataFrame, tf_key: str) -> dict | None:
    """
    볼린저밴드 스퀴즈 → 방향성 돌파 진입.

    미너비니 VCP 원리: 변동성 압축(에너지 축적) → 추세 방향 폭발적 돌파.
    순수 추세추종 전략 — 역추세 진입 없음 (EMA 방향 필수).

    조건:
      1. BB폭 < 직전 20봉 평균의 65% (스퀴즈 확인)
      2. 현재봉 BB 상단/하단 돌파
      3. EMA 추세 동방향 (EMA20 방향 일치)
      4. 거래량 1.3x 이상 (가짜 돌파 필터 — 볼륨 없는 돌파는 패)
    """
    if len(df) < 60:
        return None

    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    period = 20
    sma20  = close.rolling(period).mean()
    std20  = close.rolling(period).std()
    bb_up  = sma20 + 2.0 * std20
    bb_lo  = sma20 - 2.0 * std20
    bb_w   = (bb_up - bb_lo) / sma20   # 비율 정규화 BB폭

    cur_w   = float(bb_w.iloc[-1])
    avg_w   = float(bb_w.iloc[-21:-1].mean())   # 직전 20봉 평균 (현재봉 제외)

    if avg_w <= 0 or cur_w > avg_w * 0.65:
        return None

    atr_val = _atr(df)
    vol_r   = _vol_ratio(df)
    ema_t   = _ema_trend(close)

    cur_c   = float(close.iloc[-1])
    cur_bbu = float(bb_up.iloc[-1])
    cur_bbl = float(bb_lo.iloc[-1])

    # ── 상단 돌파 LONG (상승 추세에서만) ──────────────────────────────────────
    if cur_c > cur_bbu and ema_t >= 0 and vol_r >= 1.3:
        return _base_signal(
            "bb_squeeze_long", "LONG", "STRONG ⚡", 4,
            atr_val, ema_t, float(low.iloc[-1]),
            vol_r, 50.0, "BB스퀴즈",
        )

    # ── 하단 돌파 SHORT (하락 추세에서만) ─────────────────────────────────────
    if cur_c < cur_bbl and ema_t <= 0 and vol_r >= 1.3:
        return _base_signal(
            "bb_squeeze_short", "SHORT", "STRONG ⚡", 4,
            atr_val, ema_t, float(high.iloc[-1]),
            vol_r, 50.0, "BB스퀴즈",
        )

    return None


# ─── 전략 3.5: 뉴스/수급성 거래량 급등 추세 ─────────────────────────────────

def detect_volume_momentum(df: pd.DataFrame, tf_key: str) -> dict | None:
    """
    거래량이 먼저 터지고 가격이 구조를 밀어붙이는 구간을 포착한다.
    뉴스 API 없이도 거래량 급등을 뉴스/수급 이벤트의 선행 대리변수로 사용한다.
    """
    if tf_key not in VOLUME_MOMENTUM_TF:
        return None
    if len(df) < max(80, VOLUME_MOMENTUM_LOOKBACK + 30):
        return None

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_ = df["open"].astype(float)

    atr_val = _atr(df)
    if atr_val <= 0:
        return None
    vol_r = _vol_ratio(df)
    if vol_r < VOLUME_MOMENTUM_MIN_VOL:
        return None

    ema_t = _ema_trend(close)
    cur_o = float(open_.iloc[-1])
    cur_c = float(close.iloc[-1])
    cur_h = float(high.iloc[-1])
    cur_l = float(low.iloc[-1])
    rng = max(cur_h - cur_l, 1e-12)
    body = abs(cur_c - cur_o)
    if body < atr_val * VOLUME_MOMENTUM_BODY_ATR:
        return None

    lookback = int(VOLUME_MOMENTUM_LOOKBACK)
    prev_high = float(high.iloc[-(lookback + 1):-1].max())
    prev_low = float(low.iloc[-(lookback + 1):-1].min())
    close_pos = (cur_c - cur_l) / rng
    last3 = df.iloc[-4:-1]
    bull_cnt = int(sum(1 for _, r in last3.iterrows() if r["close"] > r["open"]))
    bear_cnt = int(sum(1 for _, r in last3.iterrows() if r["close"] < r["open"]))

    confirmed = 5
    if vol_r >= VOLUME_MOMENTUM_MIN_VOL * 1.6:
        confirmed += 1
    if body >= atr_val * 0.80:
        confirmed += 1
    confirmed = min(confirmed, 6)
    strength = "VERY STRONG 🔥" if confirmed >= 5 else "STRONG ⚡"

    if (
        cur_c > cur_o
        and cur_c >= prev_high
        and close_pos >= 0.70
        and bull_cnt >= 2
        and ema_t >= 0
    ):
        sig = _base_signal(
            "volume_momentum_long", "LONG", strength, confirmed,
            atr_val, max(ema_t, 1), float(low.iloc[-4:].min()),
            vol_r, float(calc_rsi(close).iloc[-1]), "거래량급등추세",
        )
        sig["volume_momentum"] = {
            "break_level": round(prev_high, 8),
            "body_atr": round(body / atr_val, 2),
            "close_pos": round(close_pos, 2),
            "note": f"거래량 {vol_r:.1f}x + {lookback}봉 고점 돌파",
        }
        return sig

    if (
        cur_c < cur_o
        and cur_c <= prev_low
        and close_pos <= 0.30
        and bear_cnt >= 2
        and ema_t <= 0
    ):
        sig = _base_signal(
            "volume_momentum_short", "SHORT", strength, confirmed,
            atr_val, min(ema_t, -1), float(high.iloc[-4:].max()),
            vol_r, float(calc_rsi(close).iloc[-1]), "거래량급등추세",
        )
        sig["volume_momentum"] = {
            "break_level": round(prev_low, 8),
            "body_atr": round(body / atr_val, 2),
            "close_pos": round(close_pos, 2),
            "note": f"거래량 {vol_r:.1f}x + {lookback}봉 저점 이탈",
        }
        return sig

    return None


# ─── 전략 4: 주봉+3일봉 BB 중단 상방 유지 → 내림롱 ─────────────────────────

def detect_bb_mid_pullback_long(df: pd.DataFrame, tf_key: str,
                                higher_bias: dict | None = None) -> dict | None:
    """
    상위봉(주봉+3일봉)이 BB 중단 위에서 계속 형성되는 종목만 대상으로,
    하위봉 눌림 후 반등이 확인될 때 LONG 진입한다.
    """
    if tf_key not in BB_MID_PULLBACK_TF:
        return None
    if not higher_bias or not higher_bias.get("ok"):
        return None
    if len(df) < 80:
        return None

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    bb_mid = close.rolling(20).mean()

    if pd.isna(bb_mid.iloc[-1]):
        return None

    atr_val = _atr(df)
    vol_r = _vol_ratio(df)
    rsi_val = float(calc_rsi(close).iloc[-1])
    ema_t = _ema_trend(close)

    last = df.iloc[-1]
    last_o = float(last["open"])
    last_c = float(last["close"])
    prev_h = float(high.iloc[-2])
    support = max(float(ema20.iloc[-1]), float(bb_mid.iloc[-1]))

    recent_low = low.iloc[-6:]
    recent_ema20 = ema20.iloc[-6:]
    recent_mid = bb_mid.iloc[-6:]
    touch_zone = atr_val * (0.75 if tf_key == "15m" else 0.90)

    pullback_seen = bool(
        ((recent_low <= recent_ema20 + touch_zone)
         | (recent_low <= recent_mid + touch_zone)).any()
    )
    candle_reclaim = last_c > last_o and last_c >= support * 0.998
    structure_ok = last_c > prev_h or last_c >= support + atr_val * 0.12
    not_chasing = (last_c - support) <= atr_val * 1.35
    local_trend_ok = last_c >= float(ema50.iloc[-1]) * 0.995 and float(ema20.iloc[-1]) >= float(ema50.iloc[-1]) * 0.995
    rsi_ok = 38 <= rsi_val <= 72

    if not (pullback_seen and candle_reclaim and structure_ok and not_chasing and local_trend_ok and rsi_ok):
        return None
    if vol_r < 0.85:
        return None

    confirmed = 4
    if higher_bias.get("score", 0) >= 5:
        confirmed += 1
    if vol_r >= 1.10:
        confirmed += 1
    confirmed = min(confirmed, 6)
    strength = "VERY STRONG 🔥" if confirmed >= 5 else "STRONG ⚡"

    sig = _base_signal(
        "bb_mid_pullback_long", "LONG", strength, confirmed,
        atr_val, max(ema_t, 1), float(low.iloc[-6:].min()),
        vol_r, rsi_val, "BB중단내림롱",
    )
    sig["bb_mid_bias"] = higher_bias
    sig["divergence_count"] = confirmed
    sig["divergence_quality"] = {
        "max_divergence": 6,
        "max_confirmed": 6,
        "note": f"상위봉 BB중단 상방 유지: {higher_bias.get('note', '')}",
    }
    return sig


# ─── 통합 스캔 ────────────────────────────────────────────────────────────────

# ─── 전략 5: 파라볼릭 급등-반전 사이클 (2026-07-08 신설, 관찰모드) ───────────

def detect_parabolic(df: pd.DataFrame, tf_key: str) -> dict | None:
    """
    파라볼릭 급등-반전 사이클 포착 — 초입 롱 점화 / 고점 숏 반전(단일 함수 2분기).

    기존 EMA눌림목/돌파가 추세미달·돌파불일치로 놓치는 파라볼릭 구간 전용 전략.
    리서치: Bybit 거래대금 상위 45종목 3.5일 1h 스캔에서 "24~48h내 +30%↑ 급등→고점
    형성→10%↑ 되돌림" 7개 실측사례(BLUR/VANRY/YFI/EDGE/TLM/OPG/LIT) 공통패턴 기반.
      · 초입 점화: 거래량 3.1~9.3x + 강한양봉(실체 0.65~0.98) + 봉상승 2.5~11.8%,
        아직 VWAP(24h) 저이격(2.9~7.0%)일 때만 = "막 시작"하는 자리.
      · 고점 반전: 최근 급등이력 + 최근 RECENT봉 블로우오프 고점의 극단 VWAP고이격
        (9~55%) + RSI과매수(66~96) + 상단꼬리 고점봉 + 직전봉 저가이탈 음봉 +
        거래량 클라이맥스 후 감소. (반전확정 바에서는 현재봉 이격이 이미 낮으므로
        이격/RSI는 최근 블로우오프 고점 기준으로 측정 — 실측 반영한 보정.)
    초입(이격≤6%)과 반전(이격≥12%)이 상호배타라 동일심볼 롱→숏 사이클이 겹치지 않음.
    미검증 신규전략 → strength를 VERY STRONG로 두되(관찰 라이브 진입 조건: STRONG은
    페이퍼 전용, 다이버전스 관찰 선례와 동일), main.py가
    PARABOLIC_OBSERVATION_RISK_MULT(0.30) 소액 관찰모드로 사이징한다.
    트레일링은 이번 단계에선 전용 배선 없이 기존 전역 트레일(TRAIL_ATR_MULT)을 그대로 씀.
    """
    if not PARABOLIC_CYCLE_ENABLED or tf_key not in PARABOLIC_CYCLE_TF:
        return None
    need = max(80, PARABOLIC_REVERSAL_LOOKBACK + 20)
    if len(df) < need:
        return None

    close = df["close"].astype(float)
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    open_ = df["open"].astype(float)
    volume = df["volume"].astype(float)

    atr_val = _atr(df)
    if atr_val <= 0:
        return None
    vwap_val = calc_vwap(df, period=24)   # 24봉(1h→24h) 롤링 VWAP
    if vwap_val <= 0:
        return None

    cur_o = float(open_.iloc[-1]); cur_c = float(close.iloc[-1])
    cur_h = float(high.iloc[-1]);  cur_l = float(low.iloc[-1])
    rng = max(cur_h - cur_l, 1e-12)
    body = abs(cur_c - cur_o)
    body_ratio = body / rng
    bar_gain = (cur_c / cur_o - 1) * 100 if cur_o > 0 else 0.0
    vwap_disloc = (cur_c / vwap_val - 1) * 100
    vol_r = _vol_ratio(df)
    rsi_series = calc_rsi(close)
    rsi_now = float(rsi_series.iloc[-1])
    up_wick = (cur_h - max(cur_o, cur_c)) / rng
    ema_t = _ema_trend(close)

    # ── (A) 초입 점화 LONG ──────────────────────────────────────────────────
    # 거래량 급증 + 강한 양봉 + 마이크로 신고가 + 아직 VWAP 저이격(막 시작).
    # ema_t >= 0 요구(하락추세 데드캣 방지). ema 필드는 volume_momentum 관례대로
    # 최소 1로 세워 하위 EMA중립 게이트를 통과시킨다(초입은 EMA가 막 도는 자리).
    lookback_hi = float(high.iloc[-(VOLUME_MOMENTUM_LOOKBACK + 1):-1].max())
    if (
        vol_r >= PARABOLIC_IGNITION_MIN_VOL
        and cur_c > cur_o
        and body_ratio >= PARABOLIC_IGNITION_MIN_BODY_RATIO
        and bar_gain >= PARABOLIC_IGNITION_MIN_BAR_GAIN
        and 0 <= vwap_disloc <= PARABOLIC_IGNITION_MAX_VWAP_DISLOC
        and cur_c >= lookback_hi
        and ema_t >= 0
    ):
        confirmed = 5
        if vol_r >= PARABOLIC_IGNITION_MIN_VOL * 1.6:
            confirmed += 1
        sig = _base_signal(
            "parabolic_ignition_long", "LONG", "VERY STRONG 🔥", min(confirmed, 6),
            atr_val, max(ema_t, 1), float(low.iloc[-3:].min()),
            vol_r, rsi_now, "파라볼릭점화",
        )
        sig["parabolic"] = {
            "phase": "ignition",
            "vol_ratio": round(vol_r, 2),
            "body_ratio": round(body_ratio, 2),
            "bar_gain_pct": round(bar_gain, 2),
            "vwap_disloc_pct": round(vwap_disloc, 2),
            "note": f"파라볼릭 초입: 거래량 {vol_r:.1f}x + VWAP이격 {vwap_disloc:+.1f}%(저이격)",
        }
        sig["tp_scheme_override"] = list(PARABOLIC_TP_SCHEME)   # 러너형 TP(2/5/9 ATR)
        return sig

    # ── (B) 고점 반전 SHORT ─────────────────────────────────────────────────
    # 최근 급등이력(고점형성) + 최근 블로우오프 고점의 극단 VWAP고이격 + RSI과매수
    # + 직전봉 저가이탈 음봉 + 상단꼬리 고점봉 + 거래량 클라이맥스 후 감소.
    win_hi = float(high.iloc[-PARABOLIC_REVERSAL_LOOKBACK:].max())
    win_lo = float(low.iloc[-PARABOLIC_REVERSAL_LOOKBACK:].min())
    pump_pct = (win_hi / win_lo - 1) * 100 if win_lo > 0 else 0.0
    recent_hi = float(high.iloc[-PARABOLIC_REVERSAL_RECENT:].max())
    recent_disloc = (recent_hi / vwap_val - 1) * 100
    recent_rsi_max = float(rsi_series.iloc[-PARABOLIC_REVERSAL_RECENT:].max())
    prev_low = float(low.iloc[-2])
    p_o = float(open_.iloc[-2]); p_c = float(close.iloc[-2])
    p_h = float(high.iloc[-2]);  p_l = float(low.iloc[-2])
    p_rng = max(p_h - p_l, 1e-12)
    prev_upwick = (p_h - max(p_o, p_c)) / p_rng
    vol_recent_max = float(volume.iloc[-PARABOLIC_REVERSAL_RECENT:-1].max())
    cur_vol = float(volume.iloc[-1])
    vol_fading = cur_vol < vol_recent_max
    if (
        pump_pct >= PARABOLIC_REVERSAL_MIN_PUMP_PCT
        and recent_disloc >= PARABOLIC_REVERSAL_MIN_VWAP_DISLOC
        and recent_rsi_max >= PARABOLIC_REVERSAL_MIN_RSI
        and cur_c < cur_o
        and cur_c < prev_low
        and (up_wick >= PARABOLIC_REVERSAL_MIN_UPWICK
             or prev_upwick >= PARABOLIC_REVERSAL_MIN_UPWICK)
        and vol_fading
    ):
        sig = _base_signal(
            "parabolic_reversal_short", "SHORT", "VERY STRONG 🔥", 5,
            atr_val, min(ema_t, -1), recent_hi,
            vol_r, rsi_now, "파라볼릭반전",
        )
        sig["parabolic"] = {
            "phase": "reversal",
            "pump_pct": round(pump_pct, 1),
            "recent_disloc_pct": round(recent_disloc, 1),
            "recent_rsi_max": round(recent_rsi_max, 1),
            "upwick": round(max(up_wick, prev_upwick), 2),
            "note": f"파라볼릭 반전: 급등 +{pump_pct:.0f}% 후 고점이격 {recent_disloc:.0f}% "
                    f"+ RSI {recent_rsi_max:.0f} + 저가이탈 음봉",
        }
        sig["tp_scheme_override"] = list(PARABOLIC_TP_SCHEME)
        return sig

    return None


# ─── 전략 6: VWAP 소폭이격 평균회귀 스캘핑 (2026-07-08 신설, 관찰모드) ─────────

def detect_vwap_reversion(df: pd.DataFrame, tf_key: str) -> dict | None:
    """
    VWAP 소폭이격 평균회귀 — 가격이 세션 VWAP에서 일상적 소폭 이탈했다 되돌아오는 자리.

    문헌: VWAP 평균회귀 스캘핑(고빈도). 기존 calc_vwap(24봉)은 파라볼릭 극단이격/과열
    필터로만 썼고 "소폭 이탈→회귀" 진입은 우리 시스템에 없던 격차 → 신규 구현.
    파라볼릭(이격≥12%)과 이격대(0.6~2.0%)가 상호배타라 충돌 없음.

    LONG : VWAP 아래 소폭(-2.0~-0.6%)에서 되돌림 시작(현재봉 양봉·상승) + ema_t>=0
    SHORT: VWAP 위 소폭(+0.6~+2.0%)에서 되돌림 시작(현재봉 음봉·하락) + ema_t<=0
    RSI(14)는 비극단(회귀 초기)만 허용해 자유낙하/과열 추격을 배제.

    미검증 신규(반사실 표본 0건) → strength VERY STRONG(라이브 조건)으로 두되 main.py가
    VWAP_REVERSION_OBSERVATION_RISK_MULT(0.25)로 소액 관찰 사이징. TP/SL은 우회 없이 기존
    배선 상속 — 15m은 FAST_TP(TP1 1.0ATR)라 "빠른 익절" 설계의도 자연 충족.
    5m은 기존 단독매매 금지 게이트로 라이브 제외 → 실거래는 15m만.
    """
    if tf_key not in VWAP_REVERSION_TF or len(df) < 40:
        return None

    close = df["close"].astype(float)
    open_ = df["open"].astype(float)
    atr_val = _atr(df)
    if atr_val <= 0:
        return None
    vwap_val = calc_vwap(df, period=24)
    if vwap_val <= 0:
        return None

    vol_r = _vol_ratio(df)
    if vol_r < VWAP_REVERSION_MIN_VOL:
        return None
    ema_t = _ema_trend(close)
    rsi_now = float(calc_rsi(close).iloc[-1])

    cur_c = float(close.iloc[-1]); cur_o = float(open_.iloc[-1])
    prev_c = float(close.iloc[-2])
    disloc = (cur_c / vwap_val - 1) * 100          # 현재봉 VWAP이격 %
    prev_disloc = (prev_c / vwap_val - 1) * 100

    # ── LONG: VWAP 아래 소폭이격 + 회귀 시작(위로) + 상승추세 정합 ──────────────
    if (
        -VWAP_REVERSION_MAX_DISLOC <= disloc <= -VWAP_REVERSION_MIN_DISLOC
        and cur_c > cur_o                          # 현재봉 양봉(되돌림 확인)
        and disloc > prev_disloc                   # 직전봉보다 VWAP에 근접(위로 회귀중)
        and VWAP_REVERSION_RSI_LONG_MIN <= rsi_now <= VWAP_REVERSION_RSI_LONG_MAX
        and ema_t >= 0
    ):
        sig = _base_signal(
            "vwap_reversion_long", "LONG", "VERY STRONG 🔥", 5,
            atr_val, max(ema_t, 1), float(df["low"].iloc[-1]),
            vol_r, rsi_now, "VWAP회귀",
        )
        sig["vwap_reversion"] = {
            "disloc_pct": round(disloc, 2),
            "prev_disloc_pct": round(prev_disloc, 2),
            "rsi": round(rsi_now, 1),
            "note": f"VWAP {disloc:+.1f}%(소폭저이격)→회귀 롱, RSI {rsi_now:.0f}",
        }
        return sig

    # ── SHORT: VWAP 위 소폭이격 + 회귀 시작(아래로) + 하락추세 정합 ─────────────
    if (
        VWAP_REVERSION_MIN_DISLOC <= disloc <= VWAP_REVERSION_MAX_DISLOC
        and cur_c < cur_o                          # 현재봉 음봉(되돌림 확인)
        and disloc < prev_disloc                   # 직전봉보다 VWAP에 근접(아래로 회귀중)
        and VWAP_REVERSION_RSI_SHORT_MIN <= rsi_now <= VWAP_REVERSION_RSI_SHORT_MAX
        and ema_t <= 0
    ):
        sig = _base_signal(
            "vwap_reversion_short", "SHORT", "VERY STRONG 🔥", 5,
            atr_val, min(ema_t, -1), float(df["high"].iloc[-1]),
            vol_r, rsi_now, "VWAP회귀",
        )
        sig["vwap_reversion"] = {
            "disloc_pct": round(disloc, 2),
            "prev_disloc_pct": round(prev_disloc, 2),
            "rsi": round(rsi_now, 1),
            "note": f"VWAP {disloc:+.1f}%(소폭고이격)→회귀 숏, RSI {rsi_now:.0f}",
        }
        return sig

    return None


# ─── 전략 7: Connors RSI(2) 초단기 평균회귀 (2026-07-08 신설, 관찰모드) ───────

def detect_rsi2_reversion(df: pd.DataFrame, tf_key: str) -> dict | None:
    """
    Connors RSI(2) 초단기 평균회귀 — 매우 짧은 RSI(2) 극단에서 추세순응 반전.

    문헌: Larry Connors RSI(2). RSI(2)≤10(과매도)에서 매수/≥90(과매수)에서 매도하되
    상위 추세와 같은 방향일 때만(추세순응 눌림/반등). 기존 RSI반전은 RSI(14) 28/72로
    훨씬 느려 이 초단기 엣지를 못 잡던 격차 → 신규 구현. 빠른 청산 지향.

    LONG : rsi2<=10 + ema_t>=0(상승추세 눌림) + 현재봉 반등(양봉 또는 전봉 종가 상회)
    SHORT: rsi2>=90 + ema_t<=0(하락추세 반등) + 현재봉 반락(음봉 또는 전봉 종가 하회)
    극단(rsi2<=5 / >=95)이면 VERY STRONG(라이브), 아니면 STRONG(페이퍼 전용).

    미검증 신규(반사실 표본 0건) → main.py가 RSI2_REVERSION_OBSERVATION_RISK_MULT(0.25)
    소액 관찰 사이징. TP/SL 기존 배선 상속(15m FAST_TP=빠른익절). 5m은 단독매매 금지로
    라이브 제외 → 실거래는 15m만.
    """
    if tf_key not in RSI2_REVERSION_TF or len(df) < 40:
        return None

    close = df["close"].astype(float)
    open_ = df["open"].astype(float)
    atr_val = _atr(df)
    if atr_val <= 0:
        return None
    vol_r = _vol_ratio(df)
    if vol_r < RSI2_MIN_VOL:
        return None
    ema_t = _ema_trend(close)
    rsi2 = float(calc_rsi(close, period=RSI2_PERIOD).iloc[-1])
    if rsi2 != rsi2:   # NaN 방어
        return None

    # 2026-07-08 버그수정: 원래 "현재봉이 이미 양봉/직전종가 상회"를 요구했으나,
    # RSI(2)가 극단으로 떨어지는 건 보통 현재봉이 아직 하락 중이기 때문이라
    # 동시봉에 반등 확인을 요구하면 논리적으로 거의 항상 거짓(실측 1000+봉 0건 발화
    # 확인 후 발견). Connors 원조 기법은 극단 자체에서 바로 진입한다 — 반등을
    # 기다리지 않고 EMA추세 필터만으로 진입, 대신 관찰모드 소액(0.25x)으로 방어한다.

    # ── LONG: 초단기 과매도 + 상승추세 정합 ─────────────────────────────────────
    if (
        rsi2 <= RSI2_LONG_THRESHOLD
        and ema_t >= 0
    ):
        very = rsi2 <= RSI2_EXTREME_LONG
        strength = "VERY STRONG 🔥" if very else "STRONG ⚡"
        sig = _base_signal(
            "rsi2_reversion_long", "LONG", strength, 5 if very else 4,
            atr_val, max(ema_t, 1), float(df["low"].iloc[-1]),
            vol_r, rsi2, "RSI2반전",
        )
        sig["rsi2_reversion"] = {
            "rsi2": round(rsi2, 1),
            "note": f"RSI(2) {rsi2:.0f} 과매도 + 상승추세 눌림 반등",
        }
        return sig

    # ── SHORT: 초단기 과매수 + 하락추세 정합 ────────────────────────────────────
    if (
        rsi2 >= RSI2_SHORT_THRESHOLD
        and ema_t <= 0
    ):
        very = rsi2 >= RSI2_EXTREME_SHORT
        strength = "VERY STRONG 🔥" if very else "STRONG ⚡"
        sig = _base_signal(
            "rsi2_reversion_short", "SHORT", strength, 5 if very else 4,
            atr_val, min(ema_t, -1), float(df["high"].iloc[-1]),
            vol_r, rsi2, "RSI2반전",
        )
        sig["rsi2_reversion"] = {
            "rsi2": round(rsi2, 1),
            "note": f"RSI(2) {rsi2:.0f} 과매수 + 하락추세 반등 반락",
        }
        return sig

    return None


def scan_additional(df: pd.DataFrame, tf_key: str,
                    higher_bias: dict | None = None) -> list:
    """
    다이버전스 외 추가 신호 스캔
    (RSI반전 / EMA눌림목 / BB스퀴즈 / 거래량급등추세 / BB중단내림롱 / 파라볼릭).
    반환: detect()와 동일 포맷의 신호 리스트 (0~3개)
    """
    results = []

    rsi_sig = detect_rsi_extreme(df, tf_key)
    if rsi_sig:
        results.append(rsi_sig)

    ema_sig = detect_ema_touch(df, tf_key)
    if ema_sig:
        # 같은 방향 신호가 이미 있으면 confirmed_count 합산
        existing = next((s for s in results if s.get("signal_type","").endswith(
            "long" if ema_sig["signal_type"] == "ema_long" else "short"
        )), None)
        if existing:
            existing["confirmed_count"] = min(existing["confirmed_count"] + 1, 6)
            existing["strategy"] = existing["strategy"] + "+EMA"
            # EMA MACD 정렬 정보를 합산 신호에 보존 (soft 필터용)
            existing["macd"] = ema_sig.get("macd", existing.get("macd"))
            existing["macd_align"] = ema_sig.get("macd_align")
        else:
            results.append(ema_sig)

    # 거래량/돌파 합산 시에도 EMA 기반이면 MACD 정렬을 계산해 붙인다
    # (EMA 단독이 아니고 나중에 이름이 EMA눌림목+... 로 바뀌는 경우 대비)

    bb_sig = detect_bb_squeeze(df, tf_key)
    if bb_sig:
        existing = next((s for s in results if s.get("signal_type","").endswith(
            "long" if bb_sig["signal_type"] == "bb_squeeze_long" else "short"
        )), None)
        if existing:
            # BB돌파 + 기존 신호 = 강한 합산 (BB는 추세추종 핵심 신호)
            existing["confirmed_count"] = min(existing["confirmed_count"] + 2, 6)
            existing["strategy"] = existing["strategy"] + "+BB"
        else:
            results.append(bb_sig)

    volume_sig = detect_volume_momentum(df, tf_key)
    if volume_sig:
        existing = next((s for s in results if s.get("signal_type", "").endswith(
            "long" if volume_sig["signal_type"] == "volume_momentum_long" else "short"
        )), None)
        if existing:
            existing["confirmed_count"] = min(existing["confirmed_count"] + 2, 6)
            existing["strength"] = "VERY STRONG 🔥" if existing["confirmed_count"] >= 5 else existing["strength"]
            existing["strategy"] = existing["strategy"] + "+거래량급등"
            existing["volume_momentum"] = volume_sig.get("volume_momentum", {})
        else:
            results.append(volume_sig)

    bb_mid_sig = detect_bb_mid_pullback_long(df, tf_key, higher_bias)
    if bb_mid_sig:
        existing = next((s for s in results if s.get("signal_type", "").endswith("long")), None)
        if existing:
            existing["confirmed_count"] = min(existing["confirmed_count"] + 2, 6)
            existing["strength"] = "VERY STRONG 🔥" if existing["confirmed_count"] >= 5 else existing["strength"]
            existing["strategy"] = existing["strategy"] + "+BB중단"
            existing["bb_mid_bias"] = higher_bias
            existing["divergence_quality"] = bb_mid_sig["divergence_quality"]
        else:
            results.append(bb_mid_sig)

    micro_sig = detect_micro_breakout(df, tf_key)
    if micro_sig:
        existing = next((s for s in results if s.get("signal_type","").endswith(
            "long" if micro_sig["signal_type"] == "micro_breakout_long" else "short"
        )), None)
        if existing:
            existing["confirmed_count"] = min(existing["confirmed_count"] + 1, 6)
            existing["strength"] = "VERY STRONG 🔥" if existing["confirmed_count"] >= 5 else existing["strength"]
            existing["strategy"] = existing["strategy"] + "+돌파"
        else:
            results.append(micro_sig)

    # EMA 계열 합산 전략: MACD 정렬이 없으면 방향 기준으로 채움
    for s in results:
        strat = str(s.get("strategy") or "")
        if "EMA눌림목" not in strat:
            continue
        if isinstance(s.get("macd_align"), dict) and "ok" in s["macd_align"]:
            s["macd"] = {
                "ok": bool(s["macd_align"]["ok"]),
                "value": float(s["macd_align"].get("value") or 0.0),
            }
            continue
        st = str(s.get("signal_type", ""))
        if st.endswith("long") or "bull" in st:
            direction = "LONG"
        elif st.endswith("short") or "bear" in st:
            direction = "SHORT"
        else:
            direction = "LONG"
        macd_a = macd_hist_alignment(df, direction)
        s["macd"] = {"ok": bool(macd_a["ok"]), "value": float(macd_a["value"])}
        s["macd_align"] = macd_a

    # 파라볼릭 점화/반전은 EMA눌림목 등 기존 전략과 다른 조건(VWAP이격/블로우오프)을
    # 측정하는 독립 전략이라, 다른 신호와 합산하지 않고 별도 신호로 둔다.
    parabolic_sig = detect_parabolic(df, tf_key)
    if parabolic_sig:
        results.append(parabolic_sig)

    # VWAP 소폭이격 회귀 / Connors RSI(2) 회귀도 파라볼릭처럼 독립 조건(VWAP소폭이격 /
    # 초단기 RSI극단)을 측정하는 별도 전략이라 다른 신호와 합산하지 않고 별도 신호로 둔다.
    vwap_rev_sig = detect_vwap_reversion(df, tf_key)
    if vwap_rev_sig:
        results.append(vwap_rev_sig)

    rsi2_rev_sig = detect_rsi2_reversion(df, tf_key)
    if rsi2_rev_sig:
        results.append(rsi2_rev_sig)

    return results
