"""Bybit 선물 OHLCV 수집 + 실시간 Market Radar (거래량 Top10)."""
import time
import ccxt
import pandas as pd

_exchange   = None
_radar_cache: dict = {"data": [], "ts": 0}
_RADAR_TTL  = 900   # 15분 캐시 (스캔 5분마다 재사용)

# 레버드/인덱스 토큰 제외 키워드
_EXCLUDE = {"BULL", "BEAR", "UP", "DOWN", "3L", "3S", "HALF", "SOXL", "TQQQ"}

# 코어 심볼: Top10 순위 밖이어도 항상 추적
CORE_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

# 토큰화 주식 (Bybit 선물 xStocks) — 거래량 상위 종목만 선별
# NVDA(4M), TSLA(2M) — 나머지는 거래대금 < 1M 으로 유동성 부족
STOCK_SYMBOLS = ["NVDA/USDT", "TSLA/USDT"]


def _get_exchange():
    global _exchange
    if _exchange is None:
        # defaultType=future → BTC/USDT 요청 시 자동으로 선물 매핑
        _exchange = ccxt.bybit({"options": {"defaultType": "future"}})
    return _exchange


def fetch_ohlcv(symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    ex  = _get_exchange()
    raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df  = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df


def _vol_label(v: float) -> str:
    if v >= 1e9:  return f"{v/1e9:.1f}B"
    if v >= 1e6:  return f"{v/1e6:.0f}M"
    return f"{v/1e3:.0f}K"


def fetch_market_radar(n: int = 10) -> list[dict]:
    """
    바이빗 선물 24h 거래대금 상위 n종목 실시간 조회.
    15분 캐시 — 스캔(5분)마다 API 재호출 방지.

    반환:
    [
      {
        "rank": 1,
        "symbol": "BTC/USDT",       ← 우리 시스템 형식 (ccxt futures 자동 매핑)
        "last": 59594.1,
        "change_pct": -2.0,          ← 24h % 등락
        "volume_usd": 8320000000,
        "volume_label": "8.3B",
      },
      ...
    ]

    Bybit 선물 티커는 'BTC/USDT:USDT' 형식 → 'BTC/USDT'로 변환해서 반환.
    """
    global _radar_cache
    now = time.time()

    if now - _radar_cache["ts"] < _RADAR_TTL and _radar_cache["data"]:
        return _radar_cache["data"]

    try:
        ex      = _get_exchange()
        tickers = ex.fetch_tickers()

        rows = []
        for raw_sym, t in tickers.items():
            # 바이빗 선물 형식: 'BTC/USDT:USDT'
            if not raw_sym.endswith(":USDT"):
                continue

            # 'BTC/USDT:USDT' → 'BTC/USDT' (우리 시스템 형식)
            sym  = raw_sym.split(":")[0]   # 'BTC/USDT'
            coin = sym.split("/")[0]        # 'BTC'

            # 레버드/인덱스 제외
            if any(kw in coin for kw in _EXCLUDE):
                continue

            last = t.get("last") or 0
            if last <= 0:
                continue

            # quoteVolume = USD 거래대금
            qvol = t.get("quoteVolume") or (t.get("baseVolume", 0) * last)
            chg  = t.get("percentage") or 0.0
            rows.append((sym, coin, last, chg, qvol))

        # 거래대금 내림차순 정렬 → 상위 n개
        rows.sort(key=lambda x: x[4], reverse=True)

        radar = [
            {
                "rank":         i + 1,
                "symbol":       sym,
                "coin":         coin,
                "last":         last,
                "change_pct":   round(chg, 2),
                "volume_usd":   qvol,
                "volume_label": _vol_label(qvol),
            }
            for i, (sym, coin, last, chg, qvol) in enumerate(rows[:n])
        ]

        _radar_cache = {"data": radar, "ts": now}
        return radar

    except Exception as e:
        print(f"[Radar] 조회 실패: {e} — 이전 캐시 사용")
        return _radar_cache.get("data", [])


def fetch_top_symbols(n: int = 10) -> list[str]:
    """호환용 래퍼 — symbol 목록만 반환."""
    return [r["symbol"] for r in fetch_market_radar(n)]
