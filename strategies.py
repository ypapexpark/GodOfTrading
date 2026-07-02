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
                    VOLUME_MOMENTUM_TF)
from divergence import calc_rsi, _ema_trend


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.tail(period).mean())


def _vol_ratio(df: pd.DataFrame, period: int = 20) -> float:
    vol = df["volume"]
    avg = float(vol.iloc[-(period + 1):-1].mean())
    return float(vol.iloc[-1] / avg) if avg > 0 else 0.0


def _base_signal(signal_type: str, direction: str, strength: str,
                 confirmed: int, atr_val: float, ema_t: int,
                 pivot: float, vol_r: float, rsi_val: float,
                 strategy: str) -> dict:
    """공통 신호 포맷 — 기존 detect() 반환값과 호환."""
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
        "macd": {"ok": False,         "value": 0.0},
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
            return _base_signal(
                "ema_long", "LONG", "STRONG ⚡", 4,
                atr_val, 1, prev_l, vol_r, 50.0, "EMA눌림목",
            )

    # ── 하락 추세 반등매도 SHORT ───────────────────────────────────────────────
    elif c_ema20 < c_ema50 * 0.999:
        pullback_seen = (
            (recent["high"] >= c_ema20 - touch_zone).any()
            or abs(prev_h - c_ema20) <= touch_zone
        )
        candle_bear = last_c < last_o and last_c <= c_ema20 and body >= min_body
        if pullback_seen and candle_bear and vol_r >= 1.0:
            return _base_signal(
                "ema_short", "SHORT", "STRONG ⚡", 4,
                atr_val, -1, prev_h, vol_r, 50.0, "EMA눌림목",
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

def scan_additional(df: pd.DataFrame, tf_key: str,
                    higher_bias: dict | None = None) -> list:
    """
    다이버전스 외 추가 신호 스캔
    (RSI반전 / EMA눌림목 / BB스퀴즈 / 거래량급등추세 / BB중단내림롱).
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
        else:
            results.append(ema_sig)

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

    return results
