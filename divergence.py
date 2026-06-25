"""
다이버전스 감지 엔진 — RSI / MACD / OBV / StochRSI / Volume 5중 확인
"""
import numpy as np
import pandas as pd
from config import (RSI_PERIOD, PIVOT_LEFT, PIVOT_RIGHT, LOOKBACK,
                    RSI_OVERSOLD, RSI_OVERBOUGHT,
                    STOCH_RSI_PERIOD, STOCH_K_SMOOTH,
                    VOL_SPIKE_THRESHOLD, EMA_FAST, EMA_SLOW)


# ─── 지표 계산 ───────────────────────────────────────────────────────────────

def calc_rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).round(4)


def calc_macd(close: pd.Series, fast=12, slow=26, signal=9) -> pd.Series:
    """MACD 히스토그램 반환."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return (macd_line - signal_line).round(6)


def calc_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff()).fillna(0)
    return (direction * volume).cumsum()


def calc_cvd(df: pd.DataFrame) -> pd.Series:
    """
    Synthetic CVD (Cumulative Volume Delta) — OHLCV로 근사.
    캔들 몸통 비율로 매수/매도 압력을 가중해 누적 합산.
    가격이 신고점인데 CVD가 낮아지면 → 매수세 약화 = 반전 선행 신호
    """
    body  = df["close"] - df["open"]
    range_ = (df["high"] - df["low"]).replace(0, np.nan)
    delta  = (body / range_).clip(-1, 1) * df["volume"]
    return delta.cumsum()


def calc_vwap(df: pd.DataFrame, period: int = 100) -> float:
    """VWAP — 최근 period봉 기준 거래량 가중 평균가. 5m/15m 스캘핑 기준선."""
    recent = df.tail(period)
    typical = (recent["high"] + recent["low"] + recent["close"]) / 3
    return float((typical * recent["volume"]).sum() / recent["volume"].sum())


def calc_atr(df: pd.DataFrame, period: int = 14) -> float:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def calc_stoch_rsi(close: pd.Series) -> pd.Series:
    """Stochastic RSI %K (0~100) — RSI에 Stochastic 적용."""
    rsi = calc_rsi(close, RSI_PERIOD)
    rsi_min = rsi.rolling(STOCH_RSI_PERIOD).min()
    rsi_max = rsi.rolling(STOCH_RSI_PERIOD).max()
    raw = (rsi - rsi_min) / (rsi_max - rsi_min + 1e-10) * 100
    return raw.rolling(STOCH_K_SMOOTH).mean().round(2)


# ─── 보조 분석 ───────────────────────────────────────────────────────────────

def _volume_spike(volume: pd.Series, idx: int, window=20) -> tuple[bool, float]:
    """피봇 캔들의 거래량이 평균 대비 VOL_SPIKE_THRESHOLD배 이상인지 확인."""
    if idx < window:
        return False, 0.0
    avg = float(volume.iloc[idx - window:idx].mean())
    cur = float(volume.iloc[idx])
    ratio = round(cur / avg, 2) if avg > 0 else 0.0
    return ratio >= VOL_SPIKE_THRESHOLD, ratio


def _ema_trend(close: pd.Series) -> int:
    """EMA 추세 방향 (+1=상승, -1=하락, 0=중립). 0.3% 허용 범위."""
    ema_fast = close.ewm(span=EMA_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=EMA_SLOW, adjust=False).mean()
    ratio = ema_fast.iloc[-1] / ema_slow.iloc[-1]
    if ratio > 1.003:
        return 1
    if ratio < 0.997:
        return -1
    return 0


# ─── 피봇 탐색 ───────────────────────────────────────────────────────────────

def _pivot_lows(series: pd.Series, left: int, right: int) -> list[int]:
    vals = series.values
    result = []
    for i in range(left, len(vals) - right):
        window = vals[i - left: i + right + 1]
        if vals[i] <= window.min() + 1e-10:
            result.append(i)
    return result


def _pivot_highs(series: pd.Series, left: int, right: int) -> list[int]:
    vals = series.values
    result = []
    for i in range(left, len(vals) - right):
        window = vals[i - left: i + right + 1]
        if vals[i] >= window.max() - 1e-10:
            result.append(i)
    return result


# ─── 신호 확인 (5지표) ───────────────────────────────────────────────────────

def _check_bullish(df, rsi, macd_hist, obv, cvd, stoch_k, volume, prev_i, cur_i) -> dict:
    """가격 저점↓ + 6지표 불리시 다이버전스 확인. CVD 추가(선행지표)."""
    price_prev, price_cur = df["low"].iloc[prev_i], df["low"].iloc[cur_i]
    if price_cur >= price_prev:
        return {}

    # RSI_OVERSOLD=30 → 현재 피봇 RSI < 42 (과매도 권역에서만 허용)
    # 구버전 < 50은 중립구간까지 허용 → 노이즈 신호 과다
    rsi_ok   = bool(rsi.iloc[cur_i] > rsi.iloc[prev_i] and rsi.iloc[cur_i] < RSI_OVERSOLD + 12)
    macd_ok  = bool(macd_hist.iloc[cur_i] > macd_hist.iloc[prev_i])
    obv_ok   = bool(obv.iloc[cur_i] > obv.iloc[prev_i])
    srsi_ok  = bool(stoch_k.iloc[cur_i] > stoch_k.iloc[prev_i] and stoch_k.iloc[cur_i] < 40)
    vol_ok, vol_ratio = _volume_spike(volume, cur_i)
    # CVD: 가격 저점인데 매수압력 증가 = 세력 매집 선행 신호
    cvd_ok   = bool(cvd.iloc[cur_i] > cvd.iloc[prev_i])

    return {
        "signal_type": "bullish",
        "rsi":  {"ok": rsi_ok,  "value": round(float(rsi.iloc[cur_i]), 1)},
        "macd": {"ok": macd_ok, "value": round(float(macd_hist.iloc[cur_i]), 6)},
        "obv":  {"ok": obv_ok,  "value": round(float(obv.iloc[cur_i]), 2)},
        "srsi": {"ok": srsi_ok, "value": round(float(stoch_k.iloc[cur_i]), 1)},
        "vol":  {"ok": vol_ok,  "value": vol_ratio},
        "cvd":  {"ok": cvd_ok,  "value": round(float(cvd.iloc[cur_i]), 2)},
        "pivot_price": price_cur,
        "bar_index": cur_i,
    }


def _check_bearish(df, rsi, macd_hist, obv, cvd, stoch_k, volume, prev_i, cur_i) -> dict:
    """가격 고점↑ + 6지표 베어리시 다이버전스 확인. CVD 추가(선행지표)."""
    price_prev, price_cur = df["high"].iloc[prev_i], df["high"].iloc[cur_i]
    if price_cur <= price_prev:
        return {}

    # RSI_OVERBOUGHT=70 → 현재 피봇 RSI > 58 (과매수 권역에서만 허용)
    rsi_ok   = bool(rsi.iloc[cur_i] < rsi.iloc[prev_i] and rsi.iloc[cur_i] > RSI_OVERBOUGHT - 12)
    macd_ok  = bool(macd_hist.iloc[cur_i] < macd_hist.iloc[prev_i])
    obv_ok   = bool(obv.iloc[cur_i] < obv.iloc[prev_i])
    srsi_ok  = bool(stoch_k.iloc[cur_i] < stoch_k.iloc[prev_i] and stoch_k.iloc[cur_i] > 60)
    vol_ok, vol_ratio = _volume_spike(volume, cur_i)
    # CVD: 가격 고점인데 매도압력 증가 = 세력 분산 선행 신호
    cvd_ok   = bool(cvd.iloc[cur_i] < cvd.iloc[prev_i])

    return {
        "signal_type": "bearish",
        "rsi":  {"ok": rsi_ok,  "value": round(float(rsi.iloc[cur_i]), 1)},
        "macd": {"ok": macd_ok, "value": round(float(macd_hist.iloc[cur_i]), 6)},
        "obv":  {"ok": obv_ok,  "value": round(float(obv.iloc[cur_i]), 2)},
        "srsi": {"ok": srsi_ok, "value": round(float(stoch_k.iloc[cur_i]), 1)},
        "vol":  {"ok": vol_ok,  "value": vol_ratio},
        "cvd":  {"ok": cvd_ok,  "value": round(float(cvd.iloc[cur_i]), 2)},
        "pivot_price": price_cur,
        "bar_index": cur_i,
    }


def _check_hidden_bullish(df, rsi, macd_hist, obv, cvd, stoch_k, volume, prev_i, cur_i) -> dict:
    """가격 저점↑ + 지표 저점↓ — 상승 추세 지속 (히든 불리시). CVD 추가."""
    price_prev, price_cur = df["low"].iloc[prev_i], df["low"].iloc[cur_i]
    if price_cur <= price_prev:
        return {}

    rsi_ok   = bool(rsi.iloc[cur_i] < rsi.iloc[prev_i] and rsi.iloc[cur_i] < 55)
    macd_ok  = bool(macd_hist.iloc[cur_i] < macd_hist.iloc[prev_i])
    obv_ok   = bool(obv.iloc[cur_i] > obv.iloc[prev_i])
    srsi_ok  = bool(stoch_k.iloc[cur_i] < stoch_k.iloc[prev_i] and stoch_k.iloc[cur_i] < 55)
    vol_ok, vol_ratio = _volume_spike(volume, cur_i)
    # CVD: 추세 지속 시 매수 압력이 유지되는지 확인
    cvd_ok   = bool(cvd.iloc[cur_i] > cvd.iloc[prev_i])

    return {
        "signal_type": "hidden_bullish",
        "rsi":  {"ok": rsi_ok,  "value": round(float(rsi.iloc[cur_i]), 1)},
        "macd": {"ok": macd_ok, "value": round(float(macd_hist.iloc[cur_i]), 6)},
        "obv":  {"ok": obv_ok,  "value": round(float(obv.iloc[cur_i]), 2)},
        "srsi": {"ok": srsi_ok, "value": round(float(stoch_k.iloc[cur_i]), 1)},
        "vol":  {"ok": vol_ok,  "value": vol_ratio},
        "cvd":  {"ok": cvd_ok,  "value": round(float(cvd.iloc[cur_i]), 2)},
        "pivot_price": price_cur,
        "bar_index": cur_i,
    }


def _check_hidden_bearish(df, rsi, macd_hist, obv, cvd, stoch_k, volume, prev_i, cur_i) -> dict:
    """
    가격 고점↓ + 지표 고점↑ — 하락 추세 지속 (히든 베어리시).

    하락 추세 중 반등(lower high)이 나오는데 지표는 오히려 더 높게 튀어오름.
    = 반등 구간에서 스마트머니가 분산(distribution) 중 → 하락 재개 예고.
    hidden_bullish의 정확한 대칭 구조:
      hidden_bullish: 저점↑ + 지표↓ → 상승 추세 지속 LONG
      hidden_bearish: 고점↓ + 지표↑ → 하락 추세 지속 SHORT
    """
    price_prev, price_cur = df["high"].iloc[prev_i], df["high"].iloc[cur_i]
    if price_cur >= price_prev:   # 반드시 고점이 낮아야 (lower high)
        return {}

    # 지표는 반등으로 올라가지만 가격은 전 고점 못 넘음 = 힘의 쇠진
    rsi_ok   = bool(rsi.iloc[cur_i] > rsi.iloc[prev_i] and rsi.iloc[cur_i] > 45)
    macd_ok  = bool(macd_hist.iloc[cur_i] > macd_hist.iloc[prev_i])
    # OBV 감소 = 반등 시 매수세 약함, 스마트머니 분산 중
    obv_ok   = bool(obv.iloc[cur_i] < obv.iloc[prev_i])
    srsi_ok  = bool(stoch_k.iloc[cur_i] > stoch_k.iloc[prev_i] and stoch_k.iloc[cur_i] > 45)
    vol_ok, vol_ratio = _volume_spike(volume, cur_i)
    # CVD 감소 = 매도 압력 누적 (가격 반등에도 불구)
    cvd_ok   = bool(cvd.iloc[cur_i] < cvd.iloc[prev_i])

    return {
        "signal_type": "hidden_bearish",
        "rsi":  {"ok": rsi_ok,  "value": round(float(rsi.iloc[cur_i]), 1)},
        "macd": {"ok": macd_ok, "value": round(float(macd_hist.iloc[cur_i]), 6)},
        "obv":  {"ok": obv_ok,  "value": round(float(obv.iloc[cur_i]), 2)},
        "srsi": {"ok": srsi_ok, "value": round(float(stoch_k.iloc[cur_i]), 1)},
        "vol":  {"ok": vol_ok,  "value": vol_ratio},
        "cvd":  {"ok": cvd_ok,  "value": round(float(cvd.iloc[cur_i]), 2)},
        "pivot_price": price_cur,
        "bar_index": cur_i,
    }


# ─── 메인 감지 함수 ──────────────────────────────────────────────────────────

# ─── 구조적 지지/저항 레벨 탐색 ─────────────────────────────────────────────

def find_key_levels(df: pd.DataFrame, window: int = 10, n_levels: int = 8) -> dict:
    """
    구조적 지지/저항 레벨 탐색.
    window봉 기준 로컬 고저점 = 시장이 반응한 핵심 레벨.

    퀀트 원칙: 시장 구조(Market Structure)에서의 다이버전스만 의미 있음.
    아무 자리 다이버전스 ≠ 레벨 다이버전스 — 승률 차이 10~15%.
    """
    highs = df["high"].values
    lows  = df["low"].values
    n     = len(df)

    support_levels    = []
    resistance_levels = []

    for i in range(window, n - window):
        slice_lo = lows[i - window : i + window + 1]
        slice_hi = highs[i - window : i + window + 1]
        if lows[i]  <= slice_lo.min() + 1e-8:
            support_levels.append((i, lows[i]))
        if highs[i] >= slice_hi.max() - 1e-8:
            resistance_levels.append((i, highs[i]))

    # 최근 n_levels개 (오래된 레벨은 가중치 낮음)
    support_levels    = [lv for _, lv in sorted(support_levels,    key=lambda x: x[0], reverse=True)[:n_levels]]
    resistance_levels = [lv for _, lv in sorted(resistance_levels, key=lambda x: x[0], reverse=True)[:n_levels]]

    return {"support": support_levels, "resistance": resistance_levels}


def check_key_level(pivot_price: float, direction: str,
                    key_levels: dict, atr: float) -> dict:
    """
    다이버전스 피봇이 구조적 지지/저항 ±1ATR 이내인지 확인.

    LONG: 피봇 저점이 지지 레벨 근처 (반등 가능성 높은 자리)
    SHORT: 피봇 고점이 저항 레벨 근처 (매도 압력 집중 자리)

    반환: ok=True → 포지션 +20% / ok=False, >2ATR → -20%
    """
    if atr <= 0:
        return {"ok": False, "note": "ATR 없음 — 레벨 체크 스킵", "nearest_atr": 99.0}

    levels = key_levels["support"] if direction == "LONG" else key_levels["resistance"]

    if not levels:
        return {"ok": False, "note": "레벨 없음 — 데이터 부족", "nearest_atr": 99.0}

    nearest_dist = min(abs(pivot_price - lv) for lv in levels)
    nearest_atr  = round(nearest_dist / atr, 2)
    ok           = nearest_atr <= 1.0

    emoji = "✅구조레벨" if ok else ("⚠️근접" if nearest_atr <= 2.0 else "❌레벨외")
    return {
        "ok":          ok,
        "note":        f"최근접레벨까지 {nearest_atr:.1f}ATR {emoji}",
        "nearest_atr": nearest_atr,
    }


def detect(df: pd.DataFrame) -> list[dict]:
    """
    OHLCV DataFrame → 다이버전스 신호 목록 반환.
    6지표(RSI/MACD/OBV/StochRSI/Volume/CVD) 중 3개 이상 확인 시 포함.
    EMA 역추세 신호는 5/6 미만이면 자동 제외.
    각 신호에 구조 레벨(at_key_level) 포함 — main.py에서 포지션 크기 조정에 활용.
    """
    if len(df) < 60:
        return []

    close     = df["close"]
    volume    = df["volume"]
    rsi       = calc_rsi(close)
    macd_hist = calc_macd(close)
    obv       = calc_obv(close, volume)
    cvd       = calc_cvd(df)
    atr       = calc_atr(df)
    stoch_k   = calc_stoch_rsi(close)
    ema_trend = _ema_trend(close)

    # 구조 레벨: 한 번만 계산해서 모든 신호에 재사용
    key_levels = find_key_levels(df)

    n = len(df)
    start = max(PIVOT_LEFT, n - LOOKBACK)
    results = []

    STRENGTH_MAP = {6: "ELITE 💎", 5: "VERY STRONG 🔥", 4: "STRONG ⚡", 3: "MODERATE ⚡"}

    # 저점 기반 (bullish / hidden_bullish)
    low_pivots = _pivot_lows(df["low"], PIVOT_LEFT, PIVOT_RIGHT)
    low_pivots = [i for i in low_pivots if i >= start]

    for idx in range(1, len(low_pivots)):
        prev_i, cur_i = low_pivots[idx - 1], low_pivots[idx]

        for checker in (_check_bullish, _check_hidden_bullish):
            sig = checker(df, rsi, macd_hist, obv, cvd, stoch_k, volume, prev_i, cur_i)
            if not sig:
                continue
            confirmed = sum([sig["rsi"]["ok"], sig["macd"]["ok"], sig["obv"]["ok"],
                             sig["srsi"]["ok"], sig["vol"]["ok"], sig["cvd"]["ok"]])
            if confirmed < 3:
                continue
            if ema_trend == -1 and confirmed < 5:
                continue
            sig["confirmed_count"] = confirmed
            sig["strength"]        = STRENGTH_MAP.get(confirmed, "WEAK")
            sig["atr"]             = round(atr, 2)
            sig["ema_trend"]       = ema_trend
            sig["at_key_level"]    = check_key_level(sig["pivot_price"], "LONG", key_levels, atr)
            results.append(sig)

    # 고점 기반 (bearish / hidden_bearish)
    high_pivots = _pivot_highs(df["high"], PIVOT_LEFT, PIVOT_RIGHT)
    high_pivots = [i for i in high_pivots if i >= start]

    for idx in range(1, len(high_pivots)):
        prev_i, cur_i = high_pivots[idx - 1], high_pivots[idx]

        for checker in (_check_bearish, _check_hidden_bearish):
            sig = checker(df, rsi, macd_hist, obv, cvd, stoch_k, volume, prev_i, cur_i)
            if not sig:
                continue
            confirmed = sum([sig["rsi"]["ok"], sig["macd"]["ok"], sig["obv"]["ok"],
                             sig["srsi"]["ok"], sig["vol"]["ok"], sig["cvd"]["ok"]])
            if confirmed < 3:
                continue
            if ema_trend == 1 and confirmed < 5:
                continue
            sig["confirmed_count"] = confirmed
            sig["strength"]        = STRENGTH_MAP.get(confirmed, "WEAK")
            sig["atr"]             = round(atr, 2)
            sig["ema_trend"]       = ema_trend
            sig["at_key_level"]    = check_key_level(sig["pivot_price"], "SHORT", key_levels, atr)
            results.append(sig)

    # 타입별 최신 1개씩만
    seen = set()
    deduped = []
    last_idx = len(df) - 1
    for s in sorted(results, key=lambda x: x["bar_index"], reverse=True):
        if s["signal_type"] not in seen:
            s["bars_ago"] = last_idx - s["bar_index"]
            deduped.append(s)
            seen.add(s["signal_type"])

    return deduped


# ─── 신호 품질 평가 (프로 트레이더 진입 필터) ────────────────────────────────

def get_freshness_score(bars_ago: int, tf_key: str) -> float:
    """
    신호 신선도 점수 (0.0 ~ 1.0).
    최신 신호일수록 신뢰도 높음 → 포지션 배율에 반영.
    0.0 반환 시 해당 신호 완전 스킵.

    프로 원칙: "다이버전스는 타이밍 게임 — 신선한 신호만 진입 가치가 있다"
    """
    thresholds = {       # (fresh, recent, max)
        "5m":  (3,  6, 10),
        "15m": (3,  5,  8),
        "1h":  (4,  8, 12),
        "4h":  (2,  5,  8),
        "1d":  (1,  2,  3),
    }
    t = thresholds.get(tf_key, (4, 8, 12))
    if bars_ago <= t[0]:  return 1.00  # 매우 신선 → 풀 포지션
    if bars_ago <= t[1]:  return 0.75  # 신선 → 75% 포지션
    if bars_ago <= t[2]:  return 0.50  # 다소 오래됨 → 50% 포지션
    return 0.0                          # 기회 지남 → 스킵


def check_candle_momentum(df: pd.DataFrame, direction: str, bars: int = 3,
                          scalp: bool = False) -> dict:
    """
    캔들 모멘텀 확인 — 스윙 vs 스캘핑 기준 다름.

    [스윙용 원칙] 다이버전스는 '바닥 반전 예고' — 바닥 직후 봉은 아직 하락봉이 많다.
    핵심은 "반전이 시작됐는가?" 이지 "이미 올라가고 있는가?" 가 아니다.
    → 최근 N봉 중 방향일치 봉이 1개 이상 존재 (반전 시작 확인)
    → 마지막 봉이 '강한 역방향 지속' 캔들(몸통이 전체의 70%+, 0.5ATR 이상)이 아닐 것

    [스캘핑용 원칙] 진입 타이밍이 생명 — 마지막 봉이 반드시 방향 일치해야 함.

    Returns:
      ok           (bool)  진입 허용 여부
      aligned_cnt  (int)   방향일치 봉 수
      last_aligned (bool)  마지막 봉 방향 일치 여부
      blocked_by   (str)   차단 이유 ("" = 통과)
      note         (str)   로그 출력용
    """
    recent = df.iloc[-bars:]
    bull = int(sum(1 for _, r in recent.iterrows() if r["close"] > r["open"]))
    bear = bars - bull
    last = df.iloc[-1]

    last_o    = float(last["open"])
    last_c    = float(last["close"])
    last_h    = float(last["high"])
    last_l    = float(last["low"])
    last_bull = last_c > last_o
    body      = abs(last_c - last_o)
    candle_range = last_h - last_l if (last_h - last_l) > 0 else 1e-9

    # ATR 계산 (마지막 20봉 평균)
    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    tr    = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr_val = float(tr.tail(20).mean()) if len(tr) >= 20 else float(body + 1)

    if direction == "LONG":
        aligned_cnt  = bull
        last_aligned = last_bull
        # 강한 역방향 지속 = 큰 음봉 (몸통 비중 70%+ AND 몸통 > 0.5 ATR)
        strong_against = (not last_bull) and (body / candle_range >= 0.70) and (body >= 0.5 * atr_val)
    else:
        aligned_cnt  = bear
        last_aligned = not last_bull
        strong_against = last_bull and (body / candle_range >= 0.70) and (body >= 0.5 * atr_val)

    if scalp:
        # 스캘핑: 마지막 봉 방향 일치 필수
        ok          = last_aligned
        blocked_by  = "" if ok else "스캘핑 — 마지막봉 방향 불일치"
    else:
        # 스윙: 반전 시작(1봉 이상 방향일치) AND 강한 지속 캔들 없음
        ok          = (aligned_cnt >= 1) and (not strong_against)
        blocked_by  = "" if ok else (
            "강한 역방향 지속 캔들 감지 (진입 보류)" if strong_against
            else "방향일치 봉 없음 (아직 반전 미확인)"
        )

    note = (
        f"최근{bars}봉 {'양' if direction == 'LONG' else '음'}봉 {aligned_cnt}/{bars}"
        f"  최신봉{'✅' if last_aligned else '❌'}"
        + (f"  ⚠️강한역방향" if strong_against else "")
    )
    return {
        "ok":          ok,
        "aligned_cnt": aligned_cnt,
        "total":       bars,
        "last_aligned": last_aligned,
        "blocked_by":  blocked_by,
        "note":        note,
    }


def check_entry_zone(signal: dict, current_price: float, direction: str) -> dict:
    """
    진입 구간 적정성 확인 — 이미 많이 움직였으면 쫓지 않음.

    프로 원칙: "모멘텀을 쫓지 않는다. 되돌림/초기 전환에서만 진입한다."
      LONG: 피봇 저점 대비 2.5 ATR 이상 올라갔으면 → 기회 지남
      SHORT: 피봇 고점 대비 2.5 ATR 이상 내려갔으면 → 기회 지남
    """
    atr         = signal.get("atr", 0)
    pivot_price = signal.get("pivot_price", current_price)

    if atr <= 0:
        return {"ok": True, "note": "ATR 없음 — 구간 체크 스킵", "moved_atr": 0.0}

    if direction == "LONG":
        moved_atr = (current_price - pivot_price) / atr
    else:
        moved_atr = (pivot_price - current_price) / atr

    ok   = moved_atr <= 3.5   # 2.5 → 3.5 ATR: 다이버전스 이후 충분한 진입 구간
    note = (
        f"피봇 대비 {'+' if moved_atr >= 0 else ''}{moved_atr:.1f} ATR 이동"
        f"  {'✅진입권' if ok else '❌기회지남(3.5ATR 초과)'}"
    )
    return {"ok": ok, "note": note, "moved_atr": round(moved_atr, 2)}


# ─── 추세 돌파(Breakout) 감지 ─────────────────────────────────────────────────

def detect_breakout(df: pd.DataFrame, lookback_bars: int = 20) -> dict | None:
    """
    추세 돌파(Breakout) 신호 감지.

    다이버전스 = "전환 예고" (바닥/천장에서 반전 포착)
    돌파       = "추세 가속 확인" (이미 방향 결정된 추세에 즉시 합류)

    조건 (4개 독립 확인):
    1. 현재 마감봉이 lookback봉 구조 고점/저점을 이번 봉에서 처음 돌파 (신선도)
    2. 돌파 봉 거래량 ≥ 1.5x 평균 (세력 참여 확인)
    3. EMA fast > slow (추세 방향 일치)
    4. 최근 3봉 중 2봉 이상 방향 일치 캔들 (모멘텀 확인)
    보너스: 돌파봉 크기 > 평균 ATR × 1.2 → ELITE 승격 (강한 돌파)

    반환: None = 돌파 없음 / dict = 돌파 신호 (direction, breakout_level, atr, strength 포함)
    """
    if len(df) < lookback_bars + 10:
        return None

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]

    atr       = calc_atr(df)
    ema_trend = _ema_trend(close)

    # 구조 레벨: -(lookback+3)~-3 구간 → 자기 참조 방지 (최근 3봉 제외)
    lv_slice   = slice(-(lookback_bars + 3), -3)
    resistance = float(high.iloc[lv_slice].max())
    support    = float(low.iloc[lv_slice].min())

    cur_close  = float(close.iloc[-1])
    prev_close = float(close.iloc[-2])
    cur_vol    = float(volume.iloc[-1])
    avg_vol    = float(volume.iloc[-21:-1].mean())
    vol_ratio  = round(cur_vol / avg_vol, 2) if avg_vol > 0 else 0.0
    vol_ok     = vol_ratio >= VOL_SPIKE_THRESHOLD

    # 최근 3개 완성봉 방향 집계
    last3     = df.iloc[-4:-1]
    bull_cnt  = int(sum(1 for _, r in last3.iterrows() if r["close"] > r["open"]))
    bear_cnt  = int(sum(1 for _, r in last3.iterrows() if r["close"] < r["open"]))

    # ATR 확장: 돌파봉 몸통 > 평균 ATR × 1.2 = 강한 모멘텀 폭발
    cur_range  = float(high.iloc[-1] - low.iloc[-1])
    atr_expand = cur_range > atr * 1.2

    # ── LONG 돌파 ─────────────────────────────────────────
    if (cur_close > resistance           # 구조 저항 돌파
            and prev_close <= resistance  # 직전 봉은 미달 (신선한 돌파)
            and ema_trend >= 0            # EMA 상승 방향
            and bull_cnt >= 2             # 모멘텀: 3봉 중 2봉 이상 양봉
            and vol_ok):                  # 거래량 확인
        cnt = 4 + (1 if atr_expand else 0)
        return {
            "signal_type":    "breakout_long",
            "direction":      "LONG",
            "breakout_level": round(resistance, 4),
            "pivot_price":    round(cur_close, 4),
            "atr":            round(atr, 2),
            "vol":            {"ok": True, "value": vol_ratio},
            "ema_trend":      ema_trend,
            "momentum_bars":  bull_cnt,
            "atr_expand":     atr_expand,
            "confirmed_count": cnt,
            "strength":       "ELITE 💎" if cnt >= 5 else "VERY STRONG 🔥",
            "bars_ago":       0,
            "at_key_level":   {"ok": True, "note": "구조 돌파 = 레벨 자체", "nearest_atr": 0.0},
        }

    # ── SHORT 돌파 ────────────────────────────────────────
    if (cur_close < support
            and prev_close >= support
            and ema_trend <= 0
            and bear_cnt >= 2
            and vol_ok):
        cnt = 4 + (1 if atr_expand else 0)
        return {
            "signal_type":    "breakout_short",
            "direction":      "SHORT",
            "breakout_level": round(support, 4),
            "pivot_price":    round(cur_close, 4),
            "atr":            round(atr, 2),
            "vol":            {"ok": True, "value": vol_ratio},
            "ema_trend":      ema_trend,
            "momentum_bars":  bear_cnt,
            "atr_expand":     atr_expand,
            "confirmed_count": cnt,
            "strength":       "ELITE 💎" if cnt >= 5 else "VERY STRONG 🔥",
            "bars_ago":       0,
            "at_key_level":   {"ok": True, "note": "구조 돌파 = 레벨 자체", "nearest_atr": 0.0},
        }

    return None
