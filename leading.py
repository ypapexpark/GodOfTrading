"""
선행지표 모듈 — 펀딩비 / 오픈인터레스트 / 매수매도비율
후행지표(RSI/MACD)보다 먼저 시장 방향을 포착해서 진입 게이트로 활용
"""
import os
import time
import ccxt
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# 펀딩비 게이트 임계값
FUNDING_LONG_MAX  =  0.05   # 이 이상이면 롱 진입 차단 (레버리지 롱 과밀집 = 청산 위험)
FUNDING_SHORT_MAX = -0.05   # 이 이하이면 숏 진입 차단 (레버리지 숏 과밀집 = 스퀴즈 위험)

_cache: dict = {}
CACHE_TTL = 300   # 5분 캐시


def _ex():
    return ccxt.bybit({
        "apiKey":  os.getenv("BYBIT_API_KEY", ""),
        "secret":  os.getenv("BYBIT_API_SECRET", ""),
        "options": {"defaultType": "linear"},
        "enableRateLimit": True,
    })


def _futures_symbol(symbol: str) -> str:
    if ":" not in symbol:
        return symbol.split("/")[0] + "/USDT:USDT"
    return symbol


def get_funding_rate(symbol: str) -> float:
    """현재 펀딩비 (%). 양수=롱 과밀집 위험, 음수=숏 과밀집 위험."""
    key = f"fr_{symbol}"
    if key in _cache and time.time() - _cache[key]["ts"] < CACHE_TTL:
        return _cache[key]["val"]
    try:
        fsym = _futures_symbol(symbol)
        fr   = _ex().fetch_funding_rate(fsym)
        val  = float(fr.get("fundingRate", 0)) * 100
        _cache[key] = {"val": val, "ts": time.time()}
        return val
    except Exception as e:
        print(f"[선행] {symbol} 펀딩비 조회 실패: {e}")
        return 0.0


def get_oi_change(symbol: str) -> float:
    """OI 변화율 (%, 최근 vs 5분 전). 양수=신규포지션↑ 추세강화, 음수=포지션청산↓."""
    key = f"oi_{symbol}"
    if key in _cache and time.time() - _cache[key]["ts"] < CACHE_TTL:
        return _cache[key]["val"]
    try:
        fsym = _futures_symbol(symbol)
        ex   = _ex()
        # 현재 OI
        oi_now = float(ex.fetch_open_interest(fsym).get("openInterestAmount", 0))
        # 5분 전 OI 이력
        hist   = ex.fetch_open_interest_history(fsym, "5m", limit=2)
        if len(hist) >= 2:
            oi_prev = float(hist[-2].get("openInterestAmount", oi_now))
            val = (oi_now - oi_prev) / (oi_prev + 1e-10) * 100
        else:
            val = 0.0
        _cache[key] = {"val": val, "ts": time.time()}
        return val
    except Exception as e:
        print(f"[선행] {symbol} OI 조회 실패: {e}")
        return 0.0


def get_market_context(symbol: str, direction: str) -> dict:
    """
    선행지표 종합 판단.
    Returns:
        favorable (bool)  — 이 방향으로 진입해도 선행지표 OK?
        funding   (float) — 현재 펀딩비 %
        oi_chg    (float) — OI 변화율 %
        score     (int)   — +2~-2 (양수=방향 유리)
        reason    (str)
    """
    funding = get_funding_rate(symbol)
    oi_chg  = get_oi_change(symbol)
    score   = 0
    reasons = []

    if direction == "LONG":
        if funding > FUNDING_LONG_MAX:
            score   -= 2
            reasons.append(f"펀딩비 과도 양수 {funding:+.3f}% — 롱 청산 위험")
        elif funding < -0.01:
            score   += 1
            reasons.append(f"펀딩비 음수 {funding:+.3f}% — 숏 언와인딩 기대")

        if oi_chg > 0.5:
            score   += 1
            reasons.append(f"OI 증가 {oi_chg:+.2f}% — 롱 포지션 신규 유입")
        elif oi_chg < -0.5:
            score   -= 1
            reasons.append(f"OI 감소 {oi_chg:+.2f}% — 포지션 청산 중")

    else:  # SHORT
        if funding < FUNDING_SHORT_MAX:
            score   -= 2
            reasons.append(f"펀딩비 과도 음수 {funding:+.3f}% — 숏 스퀴즈 위험")
        elif funding > 0.01:
            score   += 1
            reasons.append(f"펀딩비 양수 {funding:+.3f}% — 롱 언와인딩 기대")

        if oi_chg > 0.5:
            score   += 1
            reasons.append(f"OI 증가 {oi_chg:+.2f}% — 숏 포지션 신규 유입")
        elif oi_chg < -0.5:
            score   -= 1
            reasons.append(f"OI 감소 {oi_chg:+.2f}% — 숏 포지션 청산 중")

    favorable = score >= -1   # -2 이하만 차단 (너무 극단적일 때만)
    reason    = " / ".join(reasons) if reasons else "선행지표 중립"

    return {
        "favorable": favorable,
        "funding":   round(funding, 4),
        "oi_chg":    round(oi_chg, 3),
        "score":     score,
        "reason":    reason,
    }
