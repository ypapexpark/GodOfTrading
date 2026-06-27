"""
추가 진입 전략 — 다이버전스 외 고빈도 자리 포착
1. RSI 과매도/과매수 반전 (스캘핑 주력)
2. EMA 눌림목 진입 (추세 순응 단타)

프로 트레이더 원칙:
  다이버전스 = 반전 예고 (드물지만 강력)
  RSI 극단   = 즉각 반전 (잦고 빠름) → 스캘핑 핵심
  EMA 눌림목 = 추세 지속 매매 (승률 최고) → 단타 스윙 핵심
"""
from __future__ import annotations
import pandas as pd
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
        # 기존 formatter 호환용 (ok/value 형식)
        "rsi":  {"ok": rsi_val > 0,  "value": round(rsi_val, 1)},
        "macd": {"ok": False,         "value": 0.0},
        "obv":  {"ok": False},
        "srsi": {"ok": False,         "value": 0},
        "vol":  {"ok": vol_r >= 1.1, "value": round(vol_r, 2)},
        "cvd":  {"ok": False},
    }


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


# ─── 통합 스캔 ────────────────────────────────────────────────────────────────

def scan_additional(df: pd.DataFrame, tf_key: str) -> list:
    """
    다이버전스 외 추가 신호 스캔 (RSI반전 / EMA눌림목 / BB스퀴즈).
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
