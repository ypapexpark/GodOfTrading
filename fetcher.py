"""Bybit 선물 OHLCV 수집 + 실시간 Market Radar."""
import json
import time
from pathlib import Path
import ccxt
import pandas as pd
from config import (VOLUME_SURGE_MIN_24H_USD, VOLUME_SURGE_MIN_DELTA_PCT,
                    VOLUME_SURGE_MIN_DELTA_USD)

_exchange   = None
_radar_cache: dict = {"data": [], "ts": 0}
_ticker_cache: dict = {"rows": [], "ts": 0}
_RADAR_TTL  = 900   # 15분 캐시 (스캔 5분마다 재사용)
_RADAR_STATE_FILE = Path(__file__).parent / "market_radar_state.json"

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


def _load_radar_state() -> dict:
    if _RADAR_STATE_FILE.exists():
        try:
            return json.loads(_RADAR_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_radar_state(state: dict) -> None:
    _RADAR_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _fetch_futures_ticker_rows() -> list[dict]:
    """Bybit USDT 선물 티커를 표준 row로 수집하고 15분 캐시한다."""
    global _ticker_cache
    now = time.time()
    if now - _ticker_cache["ts"] < _RADAR_TTL and _ticker_cache["rows"]:
        return _ticker_cache["rows"]

    ex      = _get_exchange()
    tickers = ex.fetch_tickers()

    rows = []
    for raw_sym, t in tickers.items():
        # 바이빗 선물 형식: 'BTC/USDT:USDT'
        if not raw_sym.endswith(":USDT"):
            continue

        # 'BTC/USDT:USDT' → 'BTC/USDT' (우리 시스템 형식)
        sym  = raw_sym.split(":")[0]
        coin = sym.split("/")[0]

        # 레버드/인덱스 제외
        if any(kw in coin for kw in _EXCLUDE):
            continue

        last = t.get("last") or 0
        if last <= 0:
            continue

        # quoteVolume = USD 거래대금
        qvol = t.get("quoteVolume") or (t.get("baseVolume", 0) * last)
        chg  = t.get("percentage") or 0.0
        rows.append({
            "symbol":     sym,
            "coin":       coin,
            "last":       float(last),
            "change_pct": float(chg or 0.0),
            "volume_usd": float(qvol or 0.0),
        })

    rows.sort(key=lambda x: x["volume_usd"], reverse=True)
    _ticker_cache = {"rows": rows, "ts": now}
    return rows


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
        rows = _fetch_futures_ticker_rows()

        radar = [
            {
                "rank":         i + 1,
                "symbol":       row["symbol"],
                "coin":         row["coin"],
                "last":         row["last"],
                "change_pct":   round(row["change_pct"], 2),
                "volume_usd":   row["volume_usd"],
                "volume_label": _vol_label(row["volume_usd"]),
            }
            for i, row in enumerate(rows[:n])
        ]

        _radar_cache = {"data": radar, "ts": now}
        return radar

    except Exception as e:
        print(f"[Radar] 조회 실패: {e} — 이전 캐시 사용")
        return _radar_cache.get("data", [])


def fetch_top_symbols(n: int = 10) -> list[str]:
    """호환용 래퍼 — symbol 목록만 반환."""
    return [r["symbol"] for r in fetch_market_radar(n)]


def fetch_volume_surge_radar(n: int = 5) -> list[dict]:
    """
    직전 레이더 스냅샷 대비 거래대금 증가가 큰 종목을 반환한다.
    24h 절대 Top10에 아직 들지 못한 급등 거래량 종목을 조기 편입하기 위한 보조 레이더.
    """
    try:
        rows = _fetch_futures_ticker_rows()
        now = float(_ticker_cache.get("ts", time.time()) or time.time())
        state = _load_radar_state()
        prev_ts = float(state.get("snapshot_ts", 0) or 0)
        prev = state.get("volumes", {}) or {}

        current = {
            row["symbol"]: {
                "volume_usd": row["volume_usd"],
                "last": row["last"],
                "change_pct": row["change_pct"],
            }
            for row in rows
        }

        # 첫 실행은 비교 기준만 저장한다. 다음 15분 스캔부터 급증 판단 가능.
        if not prev or prev_ts <= 0:
            _save_radar_state({"snapshot_ts": now, "volumes": current})
            return []

        if prev_ts >= now:
            return []

        interval_min = max((now - prev_ts) / 60, 1.0)
        surge = []
        for row in rows:
            sym = row["symbol"]
            p = prev.get(sym, {})
            prev_vol = float(p.get("volume_usd", 0) or 0)
            cur_vol = row["volume_usd"]
            if prev_vol <= 0 or cur_vol < VOLUME_SURGE_MIN_24H_USD:
                continue

            delta = cur_vol - prev_vol
            if delta <= 0:
                continue

            delta_pct = delta / prev_vol * 100
            liquid_delta = delta >= VOLUME_SURGE_MIN_DELTA_USD
            fast_growth = delta_pct >= VOLUME_SURGE_MIN_DELTA_PCT and delta >= VOLUME_SURGE_MIN_DELTA_USD * 0.5
            if not (liquid_delta or fast_growth):
                continue

            surge_score = delta * max(1.0, delta_pct / 10)
            surge.append({
                "symbol": row["symbol"],
                "coin": row["coin"],
                "last": row["last"],
                "change_pct": round(row["change_pct"], 2),
                "volume_usd": cur_vol,
                "volume_label": _vol_label(cur_vol),
                "volume_delta_usd": delta,
                "volume_delta_label": _vol_label(delta),
                "volume_growth_pct": round(delta_pct, 2),
                "interval_min": round(interval_min, 1),
                "surge_score": surge_score,
            })

        surge.sort(key=lambda x: x["surge_score"], reverse=True)
        for i, row in enumerate(surge[:n]):
            row["rank"] = i + 1

        _save_radar_state({"snapshot_ts": now, "volumes": current})
        return surge[:n]

    except Exception as e:
        print(f"[SurgeRadar] 조회 실패: {e}")
        return []
