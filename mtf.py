"""
MTF (다중 타임프레임) 확인 모듈
프로 트레이더 핵심 룰: 하위봉 신호 + 상위봉 추세 일치 = 고승률 진입

예시)
  1h 불리시 신호 → 4h EMA 상승 + 1d EMA 상승 = FULL ALIGN 💯
  1h 불리시 신호 → 4h EMA 하락 = 역추세 반등 시도 → 차단

단, 7/7 ELITE 반전/히든 다이버전스는 추세 전환 또는 재개 신호일 수 있어
main.py에서 소액 허용 예외를 둔다. MTF는 하드 차단이 아니라 리스크 조절축이다.
"""
import time
from fetcher import fetch_ohlcv
from divergence import calc_rsi, _ema_trend

# 각 타임프레임의 확인 대상 상위봉
MTF_PARENT = {
    "5m":  ["15m", "1h"],
    "15m": ["1h",  "4h"],
    "1h":  ["4h",  "1d"],
    "4h":  ["1d"],
    "1d":  [],          # 최상위봉 — 추가 확인 불필요
}

MTF_FETCH_LIMIT = {"5m": 50, "15m": 60, "1h": 80, "4h": 60, "1d": 50}

_cache: dict = {}
CACHE_TTL = 600   # 10분 캐시 (같은 스캔 사이클 내 재사용)


def _tf_context(symbol: str, tf: str) -> dict:
    """EMA 추세 + RSI. 10분 캐시로 API 과부하 방지."""
    key = f"{symbol}_{tf}"
    now = time.time()
    if key in _cache and now - _cache[key]["ts"] < CACHE_TTL:
        return _cache[key]["v"]
    try:
        df    = fetch_ohlcv(symbol, tf, MTF_FETCH_LIMIT.get(tf, 80))
        close = df["close"]
        ctx   = {
            "ema":   _ema_trend(close),
            "rsi":   round(float(calc_rsi(close).iloc[-1]), 1),
            "price": round(float(close.iloc[-1]), 4),
        }
    except Exception as e:
        print(f"[MTF] {symbol} {tf} 조회실패: {e}")
        ctx = {"ema": 0, "rsi": 50, "price": 0}
    _cache[key] = {"v": ctx, "ts": now}
    return ctx


def check_mtf(symbol: str, signal_tf: str, direction: str) -> dict:
    """
    상위봉 추세 정렬 확인.
    direction: "LONG" | "SHORT"

    Returns:
        score       (int)  — 정렬된 상위봉 수
        max_score   (int)  — 총 상위봉 수
        aligned     (bool) — 절반 이상 정렬 (진입 허용)
        strong      (bool) — 전부 정렬 → 포지션/레버리지 부스트
        block       (bool) — 전부 역방향 → 진입 차단
        details     (list) — 각 TF 상태 문자열
        boost_pct   (float)— 적용할 포지션 배율 (1.0~1.3)
    """
    parents = MTF_PARENT.get(signal_tf, [])
    if not parents:
        return {
            "score": 2, "max_score": 2,
            "aligned": True, "strong": True, "block": False,
            "details": ["최상위봉 — MTF 불필요"],
            "boost_pct": 1.0,
        }

    TREND = {1: "📈상승", -1: "📉하락", 0: "➡️중립"}
    score   = 0
    details = []

    for ptf in parents:
        ctx = _tf_context(symbol, ptf)
        ema = ctx["ema"]
        rsi = ctx["rsi"]

        # 상위봉 EMA 방향 체크
        ema_ok = (direction == "LONG"  and ema >= 0) or \
                 (direction == "SHORT" and ema <= 0)

        # 상위봉 RSI 극단 체크 (과매수에서 롱, 과매도에서 숏 금지)
        rsi_ok = (direction == "LONG"  and rsi < 72) or \
                 (direction == "SHORT" and rsi > 28)

        tf_ok = ema_ok and rsi_ok
        if tf_ok:
            score += 1

        details.append(
            f"{'✅' if tf_ok else '❌'} {ptf}: EMA{TREND[ema]} RSI{rsi:.0f}"
        )

    n      = len(parents)
    block  = (score == 0 and n >= 2)
    aligned = score >= max(1, n * 0.5)
    strong  = (score == n)

    # 정렬 수준별 포지션 배율
    if strong and n == 2:
        boost_pct = 1.30    # 전부 정렬 (2개) → 30% 부스트
    elif strong and n == 1:
        boost_pct = 1.15    # 단일 상위봉 정렬 → 15% 부스트
    elif aligned:
        boost_pct = 1.0     # 절반 정렬 → 기본
    else:
        boost_pct = 1.0     # block이지만 aligned=True인 경우 방어

    return {
        "score":     score,
        "max_score": n,
        "aligned":   aligned,
        "strong":    strong,
        "block":     block,
        "details":   details,
        "boost_pct": boost_pct,
    }


_macro_cache: dict = {}
MACRO_CACHE_TTL = 3600   # 1시간 캐시 (주봉은 천천히 바뀜)

_daily_cache: dict = {}
DAILY_CACHE_TTL = 1800   # 30분 캐시


def get_macro_bias(symbol: str) -> dict:
    """
    주봉(1w) 매크로 바이어스 — 거시 추세 방향 판단.

    EMA20/50 + RSI 조합으로 방향성을 결정.
    같은 방향 신호: 진입 조건 유지.
    반대 방향 신호: confirmed_count 임계값 상향 (더 높은 확신 요구).

    Returns:
        direction: "LONG" | "SHORT" | "NEUTRAL"
        strength:  "STRONG" | "WEAK"   (EMA+RSI 모두 일치 = STRONG)
        ema:       -1 / 0 / 1
        rsi:       float
        note:      str  (텔레그램/콘솔용 설명)
    """
    key = f"{symbol}_1w"
    now = time.time()
    if key in _macro_cache and now - _macro_cache[key]["ts"] < MACRO_CACHE_TTL:
        return _macro_cache[key]["v"]

    try:
        df    = fetch_ohlcv(symbol, "1w", 60)
        close = df["close"]
        ema   = _ema_trend(close)
        rsi   = round(float(calc_rsi(close).iloc[-1]), 1)

        ema_label = "상승" if ema == 1 else ("하락" if ema == -1 else "중립")

        # EMA + RSI 모두 같은 방향이면 STRONG 바이어스
        if ema >= 0 and rsi > 52:
            direction = "LONG"
            strength  = "STRONG" if (ema == 1 and rsi > 55) else "WEAK"
        elif ema <= 0 and rsi < 48:
            direction = "SHORT"
            strength  = "STRONG" if (ema == -1 and rsi < 45) else "WEAK"
        else:
            direction = "NEUTRAL"
            strength  = "WEAK"

        result = {
            "direction": direction,
            "strength":  strength,
            "ema":       ema,
            "rsi":       rsi,
            "note":      f"주봉 EMA{ema_label} RSI{rsi:.0f}",
        }
    except Exception as e:
        print(f"[매크로] {symbol} 주봉 조회 실패: {e}")
        result = {"direction": "NEUTRAL", "strength": "WEAK",
                  "ema": 0, "rsi": 50, "note": "주봉 조회실패"}

    _macro_cache[key] = {"v": result, "ts": now}
    return result


def get_daily_bias(symbol: str) -> dict:
    """
    일봉(1d) 중기 바이어스 — 주봉 매크로와 함께 이중 추세 확인.

    주봉(거시) + 일봉(중기) 이중 일치 = 최고 신뢰도 추세추종 진입 기회.
    역으로, 둘 다 반대면 역추세 = 위험 구간.
    """
    key = f"{symbol}_1d_bias"
    now = time.time()
    if key in _daily_cache and now - _daily_cache[key]["ts"] < DAILY_CACHE_TTL:
        return _daily_cache[key]["v"]

    try:
        df    = fetch_ohlcv(symbol, "1d", 100)
        close = df["close"]
        ema   = _ema_trend(close)
        rsi   = round(float(calc_rsi(close).iloc[-1]), 1)
        ema_label = "상승" if ema == 1 else ("하락" if ema == -1 else "중립")

        if ema >= 0 and rsi > 52:
            direction = "LONG"
            strength  = "STRONG" if (ema == 1 and rsi > 55) else "WEAK"
        elif ema <= 0 and rsi < 48:
            direction = "SHORT"
            strength  = "STRONG" if (ema == -1 and rsi < 45) else "WEAK"
        else:
            direction = "NEUTRAL"
            strength  = "WEAK"

        result = {
            "direction": direction,
            "strength":  strength,
            "ema":       ema,
            "rsi":       rsi,
            "note":      f"일봉 EMA{ema_label} RSI{rsi:.0f}",
        }
    except Exception as e:
        print(f"[일봉바이어스] {symbol} 조회 실패: {e}")
        result = {"direction": "NEUTRAL", "strength": "WEAK",
                  "ema": 0, "rsi": 50, "note": "일봉 조회실패"}

    _daily_cache[key] = {"v": result, "ts": now}
    return result


def mtf_summary(mtf: dict) -> str:
    """텔레그램 메시지용 요약 문자열."""
    score = mtf["score"]
    n     = mtf["max_score"]
    if mtf.get("details") == ["최상위봉 — MTF 불필요"]:
        return "🔭 MTF: 최상위봉"
    label = (
        "✅✅ 전 TF 정렬 — 상위봉 우호 / 포지션 부스트" if mtf["strong"]  else
        "⚠️ 전 TF 역방향 — ELITE 다이버전스 소액 허용"
        if mtf.get("elite_mtf_override") or mtf.get("elite_reversal_override") else
        "⛔ 전 TF 역방향 — 진입 차단"      if mtf["block"]   else
        f"⚡ 부분 정렬 ({score}/{n})"
    )
    detail_str = "  |  ".join(mtf["details"])
    return f"🔭 MTF ({score}/{n}): {label}\n   {detail_str}"
