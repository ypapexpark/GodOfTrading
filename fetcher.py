"""USDT perpetual OHLCV collection + real-time market radar."""
from __future__ import annotations

import json
import hashlib
import os
import re
import threading
import time
import warnings
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)

import math

import ccxt
import pandas as pd
import requests
from binance_api_guard import (
    api_backoff_remaining,
    record_api_error,
    reserve_api_weight,
)
from config import (BTC_SYNC_BETA_LOOKBACK, BTC_SYNC_LOOKBACK,
                    BTC_SYNC_MAX_SPREAD_PCT, BTC_SYNC_MIN_24H_USD,
                    BTC_SYNC_MIN_ABS_GAP_PCT, BTC_SYNC_MIN_VOL_RATIO,
                    BTC_SYNC_MIN_BTC_MOVE_PCT, BTC_SYNC_MIN_CORRELATION,
                    BTC_SYNC_REVERSION_ZSCORE, BTC_SYNC_TIMEFRAME, BTC_SYNC_TOP_N,
                    HYPERLIQUID_API_URL, HYPERLIQUID_CANDLE_TOP_N,
                    HYPERLIQUID_LOOKBACK_BARS, HYPERLIQUID_MAX_FUNDING_ABS,
                    HYPERLIQUID_MIN_15M_MOVE_PCT, HYPERLIQUID_MIN_1H_MOVE_PCT,
                    HYPERLIQUID_MIN_24H_USD, HYPERLIQUID_MIN_OI_USD,
                    HYPERLIQUID_MIN_VOL_RATIO, HYPERLIQUID_TIMEFRAME,
                    HYPERLIQUID_TOP_N,
                    VOLUME_SURGE_MIN_24H_USD, VOLUME_SURGE_MIN_DELTA_PCT,
                    VOLUME_SURGE_MIN_DELTA_USD)
from venue_runtime import market_data_venue, namespaced_data_path, venue_label

_exchange   = None
_radar_cache: dict = {"data": [], "ts": 0}
_ticker_cache: dict = {"rows": [], "ts": 0}
_full_perpetual_cache: dict = {"rows": [], "ts": 0}
_btc_sync_cache: dict = {"data": [], "ts": 0}
_hyperliquid_cache: dict = {"data": [], "ts": 0}
_RADAR_TTL  = 900   # 15분 캐시 (스캔 5분마다 재사용)
_RADAR_STATE_FILE = namespaced_data_path("market_radar_state.json", market_data_venue())
_HYPERLIQUID_STATE_FILE = namespaced_data_path("hyperliquid_radar_state.json", market_data_venue())

# Binance 전체종목 스캔은 매 실행마다 1h 1,000봉을 500여 종목에 다시 요청하면
# 구조적으로 수 분 이상 걸린다. 완료봉은 바뀌지 않으므로 디스크에 보존하고 새로
# 생긴 봉만 합치는 증분 캐시를 사용한다. 스캐너와 별도 수집 프로세스가 같은 캐시를
# 공유하므로 재시작 뒤에도 초기 백필을 반복하지 않는다.
_OHLCV_CACHE_DIR = Path(__file__).parent / ".cache" / "ohlcv"
_BINANCE_FAPI_BASE = os.getenv(
    "BINANCE_FAPI_PUBLIC_BASE", "https://fapi.binance.com"
).rstrip("/")
_TF_MILLISECONDS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}
_cache_locks_guard = threading.Lock()
_cache_locks: dict[str, threading.Lock] = {}
_binance_weight_guard = threading.Lock()
_binance_weight_events: deque[tuple[float, int]] = deque()
_BINANCE_PUBLIC_WEIGHT_PER_MINUTE = max(
    300,
    int(os.getenv("BINANCE_PUBLIC_WEIGHT_PER_MINUTE", "1800") or 1800),
)

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
        venue = market_data_venue()
        if venue == "binance":
            _exchange = ccxt.binanceusdm({"enableRateLimit": True})
        else:
            _exchange = ccxt.bybit({
                "options": {"defaultType": "future"},
                "enableRateLimit": True,
            })
    return _exchange


def _market_symbol(exchange, symbol: str) -> str:
    try:
        exchange.load_markets()
        if symbol in exchange.markets:
            return symbol
        futures_symbol = f"{symbol}:USDT" if ":" not in symbol else symbol
        if futures_symbol in exchange.markets:
            return futures_symbol
        compact = symbol.replace("/", "")
        for market_symbol, market in exchange.markets.items():
            if market.get("id") == compact or market_symbol.split(":")[0] == symbol:
                return market_symbol
    except Exception:
        pass
    return symbol


def _ohlcv_frame(raw: list) -> pd.DataFrame:
    df = pd.DataFrame(
        raw,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    for column in ("open", "high", "low", "close", "volume"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.dropna().sort_index()


def _ohlcv_cache_path(symbol: str, timeframe: str) -> Path:
    venue = market_data_venue()
    normalized_symbol = symbol.upper()
    safe_symbol = re.sub(r"[^A-Za-z0-9_.-]+", "_", normalized_symbol)
    # Several Binance contracts use non-ASCII display symbols. Stripping those
    # names used to collapse 币安人生/USDT, 龙虾/USDT and 我踏马来了/USDT into the
    # same `_USDT` file, contaminating indicators across different markets.
    if re.search(r"[^A-Za-z0-9_.\-/]", normalized_symbol):
        digest = hashlib.sha1(normalized_symbol.encode("utf-8")).hexdigest()[:10]
        safe_symbol = f"{safe_symbol}_{digest}"
    safe_timeframe = re.sub(r"[^A-Za-z0-9_.-]+", "_", timeframe)
    return _OHLCV_CACHE_DIR / venue / f"{safe_symbol}__{safe_timeframe}.json"


def _cache_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _cache_locks_guard:
        return _cache_locks.setdefault(key, threading.Lock())


def _read_ohlcv_cache(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("rows", []) if isinstance(payload, dict) else payload
        return _ohlcv_frame(rows)
    except Exception:
        return pd.DataFrame()


def _write_ohlcv_cache(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for stamp, row in df.tail(1600).iterrows():
        rows.append([
            int(pd.Timestamp(stamp).timestamp() * 1000),
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            float(row["volume"]),
        ])
    payload = {
        "updated_at": time.time(),
        "rows": rows,
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def _latest_completed_open_ms(timeframe: str, now_ms: int | None = None) -> int:
    interval = _TF_MILLISECONDS.get(timeframe, 0)
    if interval <= 0:
        return 0
    now_ms = int(now_ms or time.time() * 1000)
    if timeframe == "1w":
        # Binance weekly klines open Monday 00:00 UTC; Unix epoch is Thursday.
        monday_origin_ms = 4 * 86_400_000  # 1970-01-05 00:00 UTC
        current_open = (
            (now_ms - monday_origin_ms) // interval
        ) * interval + monday_origin_ms
        return current_open - interval
    return (now_ms // interval) * interval - interval


def _frame_is_current(df: pd.DataFrame, timeframe: str, limit: int) -> bool:
    if (
        df is None
        or len(df) == 0
        or len(df) < limit
        or not isinstance(df.index, pd.DatetimeIndex)
    ):
        return False
    expected = _latest_completed_open_ms(timeframe)
    if expected <= 0:
        return False
    last_ms = int(pd.Timestamp(df.index[-1]).timestamp() * 1000)
    return last_ms >= expected


def _binance_kline_weight(limit: int) -> int:
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


def _reserve_binance_public_weight(weight: int) -> None:
    """Reserve from the shared IP budget used by every local Binance bot."""
    reserve_api_weight(weight)


def _fetch_binance_ohlcv_raw(
    symbol: str,
    timeframe: str,
    limit: int,
    since_ms: int | None = None,
    end_ms: int | None = None,
) -> list:
    remaining = api_backoff_remaining()
    if remaining > 0:
        raise RuntimeError(f"Binance shared API backoff {remaining:.1f}s")
    request_limit = max(1, min(int(limit), 1500))
    _reserve_binance_public_weight(_binance_kline_weight(request_limit))
    params: dict[str, int | str] = {
        "symbol": symbol.split(":")[0].replace("/", "").upper(),
        "interval": timeframe,
        "limit": request_limit,
    }
    if since_ms is not None and since_ms > 0:
        params["startTime"] = int(since_ms)
    if end_ms is not None and end_ms > 0:
        params["endTime"] = int(end_ms)
    try:
        response = requests.get(
            f"{_BINANCE_FAPI_BASE}/fapi/v1/klines",
            params=params,
            timeout=12,
        )
        response.raise_for_status()
    except Exception as exc:
        record_api_error(exc)
        raise
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Binance kline 응답 오류: {payload}")
    return [row[:6] for row in payload]


def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    limit: int,
    *,
    force_refresh: bool = False,
    cache_only: bool = False,
) -> pd.DataFrame:
    """Fetch OHLCV, using an incremental persistent cache for Binance.

    Binance requests are capped at the latest completed candle, so a partially
    formed candle is never mistaken for its final OHLCV after the next boundary.
    ``attrs['stale']`` is true only when a refresh failed and the latest
    completed candle is missing.
    """
    if market_data_venue() != "binance" or timeframe not in _TF_MILLISECONDS:
        ex = _get_exchange()
        raw = ex.fetch_ohlcv(
            _market_symbol(ex, symbol), timeframe=timeframe, limit=limit
        )
        df = _ohlcv_frame(raw)
        df.attrs.update({"source": "rest", "stale": False})
        return df

    path = _ohlcv_cache_path(symbol, timeframe)
    lock = _cache_lock(path)
    with lock:
        cached = _read_ohlcv_cache(path)
        if not force_refresh and _frame_is_current(cached, timeframe, limit):
            result = cached.tail(limit).copy()
            result.attrs.update({"source": "cache", "stale": False})
            return result
        if cache_only:
            if not len(cached):
                raise RuntimeError(f"OHLCV cache miss: {symbol} {timeframe}")
            result = cached.tail(limit).copy()
            result.attrs.update({
                "source": "cache-only",
                "stale": not _frame_is_current(
                    cached, timeframe, min(limit, len(cached))
                ),
            })
            return result

        interval_ms = _TF_MILLISECONDS[timeframe]
        fetch_limit = int(limit) if len(cached) < limit else 3
        since_ms = None
        if len(cached) >= limit and isinstance(cached.index, pd.DatetimeIndex):
            last_ms = int(pd.Timestamp(cached.index[-1]).timestamp() * 1000)
            since_ms = max(0, last_ms - interval_ms)
        try:
            latest_completed_ms = _latest_completed_open_ms(timeframe)
            fresh = _ohlcv_frame(
                _fetch_binance_ohlcv_raw(
                    symbol,
                    timeframe,
                    fetch_limit,
                    since_ms=since_ms,
                    end_ms=latest_completed_ms + interval_ms - 1,
                )
            )
            merged = pd.concat([cached, fresh]) if len(cached) else fresh
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            _write_ohlcv_cache(path, merged)
            result = merged.tail(limit).copy()
            result.attrs.update({
                "source": "incremental" if len(cached) else "backfill",
                "stale": not _frame_is_current(merged, timeframe, min(limit, len(merged))),
            })
            return result
        except Exception:
            if len(cached):
                result = cached.tail(limit).copy()
                result.attrs.update({
                    "source": "stale-cache",
                    "stale": not _frame_is_current(cached, timeframe, min(limit, len(cached))),
                })
                return result
            raise


def fetch_ohlcv_batch(
    requests_: list[tuple[str, str, int]],
    *,
    max_workers: int = 8,
    force_refresh: bool = False,
    cache_only: bool = False,
) -> tuple[dict[tuple[str, str], pd.DataFrame], dict[tuple[str, str], str]]:
    """Fetch independent symbol/timeframe requests concurrently.

    The public-weight limiter and persistent cache keep concurrency bounded.
    Errors are returned per key so one delisted symbol cannot abort the universe.
    """
    unique = list(dict.fromkeys(requests_))
    frames: dict[tuple[str, str], pd.DataFrame] = {}
    errors: dict[tuple[str, str], str] = {}
    if not unique:
        return frames, errors
    workers = max(1, min(int(max_workers), len(unique), 16))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ohlcv") as pool:
        futures = {
            pool.submit(
                fetch_ohlcv,
                symbol,
                timeframe,
                limit,
                force_refresh=force_refresh,
                cache_only=cache_only,
            ): (symbol, timeframe)
            for symbol, timeframe, limit in unique
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                frames[key] = future.result()
            except Exception as exc:
                errors[key] = str(exc)[:240]
    return frames, errors


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
    """Collect active venue USDT perpetual tickers as standard rows."""
    global _ticker_cache
    now = time.time()
    if now - _ticker_cache["ts"] < _RADAR_TTL and _ticker_cache["rows"]:
        return _ticker_cache["rows"]

    if market_data_venue() == "binance" and api_backoff_remaining() > 0:
        if _ticker_cache.get("rows"):
            return list(_ticker_cache["rows"])
        raise RuntimeError("Binance shared API backoff active")
    ex = _get_exchange()
    try:
        reserve_api_weight(40)
        tickers = ex.fetch_tickers()
    except Exception as exc:
        record_api_error(exc)
        raise

    rows = []
    for raw_sym, t in tickers.items():
        # USDT perpetual format: 'BTC/USDT:USDT'
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
        bid = float(t.get("bid") or 0)
        ask = float(t.get("ask") or 0)
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
        spread_pct = ((ask - bid) / mid * 100) if mid > 0 and ask >= bid else 0.0
        rows.append({
            "symbol":     sym,
            "coin":       coin,
            "last":       float(last),
            "change_pct": float(chg or 0.0),
            "volume_usd": float(qvol or 0.0),
            "bid":        bid,
            "ask":        ask,
            "spread_pct": float(spread_pct or 0.0),
        })

    rows.sort(key=lambda x: x["volume_usd"], reverse=True)
    _ticker_cache = {"rows": rows, "ts": now}
    return rows


def fetch_market_radar(n: int = 10) -> list[dict]:
    """
    활성 거래소 선물 24h 거래대금 상위 n종목 실시간 조회.
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

    선물 티커는 'BTC/USDT:USDT' 형식 → 'BTC/USDT'로 변환해서 반환.
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
                "bid":          row["bid"],
                "ask":          row["ask"],
                "spread_pct":   row["spread_pct"],
            }
            for i, row in enumerate(rows[:n])
        ]

        _radar_cache = {"data": radar, "ts": now}
        return radar

    except Exception as e:
        print(f"[Radar:{venue_label(market_data_venue())}] 조회 실패: {e} — 이전 캐시 사용")
        return _radar_cache.get("data", [])


def fetch_top_symbols(n: int = 10) -> list[str]:
    """호환용 래퍼 — symbol 목록만 반환."""
    return [r["symbol"] for r in fetch_market_radar(n)]


def fetch_all_usdt_perpetual_markets(cache_seconds: int = 300) -> list[dict]:
    """Return every active linear USDT perpetual on the data venue.

    Unlike the volume radar this applies no rank or coin-name filter. Exchange
    metadata is the source of truth for active, linear, perpetual USDT markets.
    A bulk ticker snapshot supplies price/spread without one request per symbol.
    """
    global _full_perpetual_cache
    now = time.time()
    if (
        now - float(_full_perpetual_cache.get("ts", 0) or 0) < cache_seconds
        and _full_perpetual_cache.get("rows")
    ):
        return list(_full_perpetual_cache["rows"])

    if market_data_venue() == "binance" and api_backoff_remaining() > 0:
        if _full_perpetual_cache.get("rows"):
            return list(_full_perpetual_cache["rows"])
        raise RuntimeError("Binance shared API backoff active")

    ex = _get_exchange()
    try:
        reserve_api_weight(40)
        ex.load_markets()
        tickers = ex.fetch_tickers()
    except Exception as exc:
        record_api_error(exc)
        raise
    rows: list[dict] = []
    seen: set[str] = set()
    for market_symbol, market in ex.markets.items():
        if market.get("active") is False:
            continue
        if not market.get("swap") or not market.get("linear"):
            continue
        # Binance TRADIFI_PERPETUAL markets require a separate agreement
        # (-4411) and are outside this crypto strategy.  CCXT classifies both
        # as swaps, so use the raw contractType when the venue provides it.
        contract_type = str(
            (market.get("info") or {}).get("contractType") or ""
        ).upper()
        if contract_type and contract_type != "PERPETUAL":
            continue
        if str(market.get("quote") or "").upper() != "USDT":
            continue
        if str(market.get("settle") or "").upper() != "USDT":
            continue
        canonical = market_symbol.split(":")[0]
        if canonical in seen:
            continue
        ticker = tickers.get(market_symbol) or tickers.get(canonical) or {}
        last = float(ticker.get("last") or ticker.get("close") or 0.0)
        if last <= 0:
            continue
        bid = float(ticker.get("bid") or 0.0)
        ask = float(ticker.get("ask") or 0.0)
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
        spread_pct = (
            (ask - bid) / mid * 100
            if mid > 0 and ask >= bid else 0.0
        )
        quote_volume = float(
            ticker.get("quoteVolume")
            or float(ticker.get("baseVolume") or 0.0) * last
            or 0.0
        )
        rows.append({
            "symbol": canonical,
            "market_symbol": market_symbol,
            "last": last,
            "bid": bid,
            "ask": ask,
            "spread_pct": spread_pct,
            "volume_usd": quote_volume,
            "contract_type": contract_type or "PERPETUAL",
        })
        seen.add(canonical)

    rows.sort(key=lambda row: row["volume_usd"], reverse=True)
    _full_perpetual_cache = {"rows": rows, "ts": now}
    return list(rows)


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
                "bid": row["bid"],
                "ask": row["ask"],
                "spread_pct": row["spread_pct"],
            })

        surge.sort(key=lambda x: x["surge_score"], reverse=True)
        for i, row in enumerate(surge[:n]):
            row["rank"] = i + 1

        _save_radar_state({"snapshot_ts": now, "volumes": current})
        return surge[:n]

    except Exception as e:
        print(f"[SurgeRadar] 조회 실패: {e}")
        return []


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def _load_hyperliquid_state() -> dict:
    if _HYPERLIQUID_STATE_FILE.exists():
        try:
            return json.loads(_HYPERLIQUID_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_hyperliquid_state(state: dict) -> None:
    _HYPERLIQUID_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _hyperliquid_post(body: dict, timeout: float = 8.0):
    """Hyperliquid public info endpoint 호출 래퍼. 주문/서명은 전혀 사용하지 않는다."""
    res = requests.post(
        HYPERLIQUID_API_URL,
        json=body,
        timeout=timeout,
        headers={"Content-Type": "application/json"},
    )
    res.raise_for_status()
    return res.json()


def _hyperliquid_to_venue_symbol(coin: str, venue_by_coin: dict) -> str:
    """Hyperliquid coin names are conservatively mapped to the active CEX symbol."""
    raw = str(coin or "").strip()
    if not raw or ":" in raw:
        return ""

    # Hyperliquid UI/HyperCore의 일부 spot remap/특수명은 선물 레이더에서 제외한다.
    if raw.startswith("@") or raw.upper() in {"UBTC", "UETH"}:
        return ""

    candidates = [raw, raw.upper()]
    if raw.startswith("k") and len(raw) > 1:
        # Hyperliquid의 k-prefixed micro contracts 일부는 CEX의 1000* 심볼과 대응될 수 있다.
        candidates.append(f"1000{raw[1:].upper()}")
    if raw.upper().startswith("K") and len(raw) > 1:
        candidates.append(f"1000{raw.upper()[1:]}")

    seen = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        sym = venue_by_coin.get(c)
        if sym:
            return sym
    return ""


def _hyperliquid_candles(coin: str, interval: str, bars: int) -> list[dict]:
    now_ms = int(time.time() * 1000)
    minutes = {
        "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
        "1h": 60, "2h": 120, "4h": 240, "8h": 480,
        "12h": 720, "1d": 1440, "3d": 4320, "1w": 10080,
    }.get(interval, 15)
    start_ms = now_ms - int((bars + 4) * minutes * 60 * 1000)
    data = _hyperliquid_post({
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": start_ms,
            "endTime": now_ms,
        },
    })
    return data if isinstance(data, list) else []


def _hyperliquid_candle_metrics(candles: list[dict]) -> dict:
    if not candles or len(candles) < 6:
        return {}

    closes = [_safe_float(c.get("c")) for c in candles]
    opens = [_safe_float(c.get("o")) for c in candles]
    vols = [_safe_float(c.get("v")) for c in candles]
    if closes[-1] <= 0 or closes[-2] <= 0:
        return {}

    ret_15m = (closes[-1] / closes[-2] - 1) * 100
    ret_1h = 0.0
    if len(closes) >= 5 and closes[-5] > 0:
        ret_1h = (closes[-1] / closes[-5] - 1) * 100

    recent_vol = sum(vols[-3:]) / min(len(vols), 3)
    base_slice = vols[-23:-3] if len(vols) >= 23 else vols[:-3]
    base_vol = sum(base_slice) / len(base_slice) if base_slice else 0.0
    vol_ratio = recent_vol / base_vol if base_vol > 0 else 0.0

    last_body_pct = 0.0
    if opens[-1] > 0:
        last_body_pct = (closes[-1] / opens[-1] - 1) * 100

    last_ts = candles[-1].get("t") or candles[-1].get("T") or ""
    return {
        "ret_15m_pct": ret_15m,
        "ret_1h_pct": ret_1h,
        "vol_ratio": vol_ratio,
        "last_body_pct": last_body_pct,
        "last_ts": last_ts,
    }


def fetch_hyperliquid_lead_radar(n: int = 8) -> list[dict]:
    """
    매매전략 5: Hyperliquid Lead Radar.

    Hyperliquid의 거래량/OI/단기 캔들 모멘텀을 읽기 전용으로 수집한 뒤,
    활성 CEX 거래소에도 상장된 종목만 후보로 돌려준다. 이 레이더는
    Hyperliquid에서 주문하지 않고, 기존 전략의 스캔 대상/진입 근거/가산점으로만 쓰인다.
    """
    global _hyperliquid_cache
    now = time.time()
    if now - _hyperliquid_cache["ts"] < _RADAR_TTL and _hyperliquid_cache["data"]:
        return _hyperliquid_cache["data"]

    try:
        venue_rows = _fetch_futures_ticker_rows()
        venue_by_coin = {row["coin"]: row["symbol"] for row in venue_rows}
        venue_by_symbol = {row["symbol"]: row for row in venue_rows}

        raw = _hyperliquid_post({"type": "metaAndAssetCtxs"})
        if not isinstance(raw, list) or len(raw) < 2:
            return _hyperliquid_cache.get("data", [])

        meta = raw[0] or {}
        contexts = raw[1] or []
        universe = meta.get("universe", []) if isinstance(meta, dict) else []

        state = _load_hyperliquid_state()
        prev_assets = state.get("assets", {}) or {}
        snapshot = {}
        base_rows = []
        for asset, ctx in zip(universe, contexts):
            if not isinstance(asset, dict) or not isinstance(ctx, dict):
                continue
            if asset.get("isDelisted"):
                continue
            coin = str(asset.get("name") or "").strip()
            if not coin or any(kw in coin.upper() for kw in _EXCLUDE):
                continue

            symbol = _hyperliquid_to_venue_symbol(coin, venue_by_coin)
            if not symbol:
                continue

            mark = _safe_float(ctx.get("markPx") or ctx.get("midPx") or ctx.get("oraclePx"))
            prev_day = _safe_float(ctx.get("prevDayPx"))
            volume_usd = _safe_float(ctx.get("dayNtlVlm"))
            oi_base = _safe_float(ctx.get("openInterest"))
            oi_usd = oi_base * mark if mark > 0 else 0.0
            funding = _safe_float(ctx.get("funding"))
            if mark <= 0 or volume_usd < HYPERLIQUID_MIN_24H_USD:
                continue

            day_change = (mark / prev_day - 1) * 100 if prev_day > 0 else 0.0
            prev = prev_assets.get(symbol, {})
            prev_oi_usd = _safe_float(prev.get("oi_usd"))
            oi_growth_pct = (
                (oi_usd / prev_oi_usd - 1) * 100 if prev_oi_usd > 0 else 0.0
            )
            venue_row = venue_by_symbol.get(symbol, {})
            venue_change = _safe_float(venue_row.get("change_pct"))
            lead_gap = day_change - venue_change

            snapshot[symbol] = {
                "coin": coin,
                "volume_usd": volume_usd,
                "oi_usd": oi_usd,
                "mark": mark,
                "day_change_pct": day_change,
            }
            base_rows.append({
                "symbol": symbol,
                "coin": symbol.split("/")[0],
                "hyperliquid_coin": coin,
                "mark": mark,
                "day_change_pct": day_change,
                "bybit_change_pct": venue_change,
                "venue_change_pct": venue_change,
                "lead_gap_pct": lead_gap,
                "volume_usd": volume_usd,
                "volume_label": _vol_label(volume_usd),
                "open_interest_usd": oi_usd,
                "open_interest_label": _vol_label(oi_usd),
                "oi_growth_pct": oi_growth_pct,
                "funding": funding,
                "max_leverage": asset.get("maxLeverage"),
            })

        base_rows.sort(
            key=lambda r: (
                r["volume_usd"] * 0.65
                + r["open_interest_usd"] * 0.35
                + abs(r["day_change_pct"]) * 1_000_000
            ),
            reverse=True,
        )
        base_rows = base_rows[:int(HYPERLIQUID_TOP_N)]

        candidates = []
        for row in base_rows[:HYPERLIQUID_CANDLE_TOP_N]:
            try:
                candles = _hyperliquid_candles(
                    row["hyperliquid_coin"],
                    HYPERLIQUID_TIMEFRAME,
                    HYPERLIQUID_LOOKBACK_BARS,
                )
                metrics = _hyperliquid_candle_metrics(candles)
            except Exception:
                metrics = {}
            time.sleep(0.03)
            if not metrics:
                continue

            ret_15m = float(metrics.get("ret_15m_pct", 0) or 0)
            ret_1h = float(metrics.get("ret_1h_pct", 0) or 0)
            vol_ratio = float(metrics.get("vol_ratio", 0) or 0)
            lead_ret = ret_1h if abs(ret_1h) >= abs(ret_15m) else ret_15m
            direction = "LONG" if lead_ret > 0 else "SHORT"
            strong_move = (
                abs(ret_1h) >= HYPERLIQUID_MIN_1H_MOVE_PCT
                or abs(ret_15m) >= HYPERLIQUID_MIN_15M_MOVE_PCT
            )
            if not strong_move:
                continue
            if vol_ratio < HYPERLIQUID_MIN_VOL_RATIO:
                continue
            if row["open_interest_usd"] < HYPERLIQUID_MIN_OI_USD:
                continue

            funding_abs = abs(float(row.get("funding", 0) or 0))
            funding_penalty = 0.70 if funding_abs >= HYPERLIQUID_MAX_FUNDING_ABS else 1.0
            oi_bonus = 1.0 + min(max(float(row.get("oi_growth_pct", 0) or 0), 0.0), 150.0) / 200.0
            liq_bonus = 1.0 + min(row["volume_usd"] / 250_000_000, 1.0) * 0.40
            score = abs(lead_ret) * max(1.0, vol_ratio) * oi_bonus * liq_bonus * funding_penalty

            out = dict(row)
            out.update({
                "direction": direction,
                "lead_ret_pct": round(lead_ret, 2),
                "ret_15m_pct": round(ret_15m, 2),
                "ret_1h_pct": round(ret_1h, 2),
                "vol_ratio": round(vol_ratio, 2),
                "last_body_pct": round(float(metrics.get("last_body_pct", 0) or 0), 3),
                "last_ts": metrics.get("last_ts", ""),
                "score": round(score, 2),
                "funding_overheated": funding_abs >= HYPERLIQUID_MAX_FUNDING_ABS,
            })
            candidates.append(out)

        candidates.sort(key=lambda x: x["score"], reverse=True)
        for i, row in enumerate(candidates[:n]):
            row["rank"] = i + 1

        _save_hyperliquid_state({
            "snapshot_ts": now,
            "assets": snapshot,
        })
        _hyperliquid_cache = {"data": candidates[:n], "ts": now}
        return candidates[:n]
    except Exception as e:
        print(f"[HyperliquidRadar] 조회 실패: {e}")
        return _hyperliquid_cache.get("data", [])


def _window_return_pct(df: pd.DataFrame, lookback: int) -> float:
    """최근 lookback봉 기준 종가 수익률(%)."""
    if df is None or len(df) <= lookback:
        return 0.0
    start = float(df["close"].iloc[-lookback - 1])
    end = float(df["close"].iloc[-1])
    if start <= 0:
        return 0.0
    return (end / start - 1) * 100


def _volume_ratio(df: pd.DataFrame, lookback: int) -> float:
    """최근 3봉 거래량이 이전 lookback봉 평균 대비 얼마나 커졌는지."""
    if df is None or len(df) < lookback + 4:
        return 0.0
    recent = float(df["volume"].iloc[-3:].mean())
    base = float(df["volume"].iloc[-lookback - 3:-3].mean())
    if base <= 0:
        return 0.0
    return recent / base


def _aligned_btc_sync_metrics(sym_df: pd.DataFrame, btc_df: pd.DataFrame,
                              lookback: int, beta_lookback: int) -> dict:
    """BTC와 종목 캔들을 같은 타임스탬프로 맞춰 베타 보정 괴리를 계산한다."""
    if sym_df is None or btc_df is None:
        return {}
    joined = pd.concat(
        {
            "sym_close": sym_df["close"],
            "btc_close": btc_df["close"],
            "sym_volume": sym_df["volume"],
            "sym_open": sym_df["open"],
        },
        axis=1,
        join="inner",
    ).dropna()
    min_len = max(int(lookback) + 4, int(beta_lookback) + 4)
    if len(joined) < min_len:
        return {}

    sym_start = float(joined["sym_close"].iloc[-lookback - 1])
    sym_end = float(joined["sym_close"].iloc[-1])
    btc_start = float(joined["btc_close"].iloc[-lookback - 1])
    btc_end = float(joined["btc_close"].iloc[-1])
    if min(sym_start, sym_end, btc_start, btc_end) <= 0:
        return {}

    sym_ret = (sym_end / sym_start - 1) * 100
    btc_ret = (btc_end / btc_start - 1) * 100

    rets = joined[["sym_close", "btc_close"]].pct_change().dropna().tail(beta_lookback)
    if len(rets) < max(12, beta_lookback // 3):
        return {}
    btc_var = float(rets["btc_close"].var() or 0.0)
    if btc_var <= 0:
        beta = 1.0
    else:
        beta = float(rets["sym_close"].cov(rets["btc_close"]) / btc_var)
    if not math.isfinite(beta):
        beta = 1.0
    beta = max(0.15, min(beta, 4.0))

    corr = float(rets["sym_close"].corr(rets["btc_close"]) or 0.0)
    if not math.isfinite(corr):
        corr = 0.0
    expected_ret = btc_ret * beta
    raw_gap = sym_ret - btc_ret
    beta_gap = sym_ret - expected_ret

    residual = (rets["sym_close"] - beta * rets["btc_close"]) * 100
    resid_std = float(residual.std() or 0.0) * (lookback ** 0.5)
    zscore = beta_gap / resid_std if resid_std > 0 else 0.0
    if not math.isfinite(zscore):
        zscore = 0.0

    vol_ratio = _volume_ratio(joined.rename(columns={"sym_volume": "volume"}), lookback)
    last_open = float(joined["sym_open"].iloc[-1])
    last_close = float(joined["sym_close"].iloc[-1])
    last_body_pct = ((last_close - last_open) / last_open * 100) if last_open > 0 else 0.0

    gap_sign = 1 if beta_gap > 0 else -1
    last_reverting = (
        (gap_sign > 0 and last_body_pct < 0)
        or (gap_sign < 0 and last_body_pct > 0)
    )
    mode = "momentum"
    direction = "LONG" if beta_gap > 0 else "SHORT"
    if abs(zscore) >= BTC_SYNC_REVERSION_ZSCORE and last_reverting:
        mode = "reversion"
        direction = "SHORT" if beta_gap > 0 else "LONG"

    return {
        "symbol_ret_pct": sym_ret,
        "btc_ret_pct": btc_ret,
        "expected_ret_pct": expected_ret,
        "raw_gap_pct": raw_gap,
        "beta_gap_pct": beta_gap,
        "gap_pct": beta_gap,
        "beta": beta,
        "correlation": corr,
        "gap_zscore": zscore,
        "vol_ratio": vol_ratio,
        "last_body_pct": last_body_pct,
        "last_ts": joined.index[-1].isoformat(),
        "sync_mode": mode,
        "direction": direction,
    }


def fetch_btc_sync_dislocations(n: int = BTC_SYNC_TOP_N) -> list[dict]:
    """
    BTC 기준 동조/괴리 레이더.

    최근 1시간(기본 5m×12) 동안 BTC와 각 종목의 공통 타임스탬프 캔들을 맞춘 뒤,
    최근 6시간 베타를 적용해 "실제 종목수익률 - BTC베타×BTC수익률" 괴리를 계산한다.
    이 값이 크고 거래량도 함께 증가하면, 해당 종목은 "BTC와 동기화되지 않은 가격 이벤트"
    후보로 보고 기존 전략 스캔 대상과 전략 3 직접매매 엔진에 전달한다.

    이 함수는 차익거래 체결 엔진이 아니다. 레이턴시/수수료/호가공백을 고려하지 않은
    탐지 레이어이며, 실거래 여부는 main.py의 전략별 R:R/ROI/리스크 게이트가 결정한다.
    """
    global _btc_sync_cache
    now = time.time()
    if now - _btc_sync_cache["ts"] < _RADAR_TTL:
        return _btc_sync_cache["data"]

    try:
        rows = [
            row for row in _fetch_futures_ticker_rows()[:n]
            if row["symbol"] != "BTC/USDT" and row["volume_usd"] >= BTC_SYNC_MIN_24H_USD
        ]
        lookback = int(BTC_SYNC_LOOKBACK)
        beta_lookback = int(BTC_SYNC_BETA_LOOKBACK)
        limit = max(lookback + beta_lookback + 8, 120)
        btc_df = fetch_ohlcv("BTC/USDT", BTC_SYNC_TIMEFRAME, limit)

        candidates = []
        for row in rows:
            sym = row["symbol"]
            if row.get("spread_pct", 0) > BTC_SYNC_MAX_SPREAD_PCT:
                continue
            try:
                df = fetch_ohlcv(sym, BTC_SYNC_TIMEFRAME, limit)
            except Exception:
                continue
            metrics = _aligned_btc_sync_metrics(df, btc_df, lookback, beta_lookback)
            if not metrics:
                continue
            btc_ret = float(metrics["btc_ret_pct"])
            sym_ret = float(metrics["symbol_ret_pct"])
            gap = float(metrics["gap_pct"])
            vol_ratio = float(metrics["vol_ratio"])
            corr = abs(float(metrics["correlation"]))
            if abs(btc_ret) < BTC_SYNC_MIN_BTC_MOVE_PCT:
                continue
            if corr < BTC_SYNC_MIN_CORRELATION:
                continue
            if abs(gap) < BTC_SYNC_MIN_ABS_GAP_PCT or vol_ratio < BTC_SYNC_MIN_VOL_RATIO:
                continue

            direction = metrics["direction"]
            z_bonus = 1.0 + min(abs(float(metrics["gap_zscore"])), 4.0) * 0.15
            score = abs(gap) * max(1.0, vol_ratio) * z_bonus
            candidates.append({
                "symbol": sym,
                "coin": row["coin"],
                "direction": direction,
                "btc_ret_pct": round(btc_ret, 2),
                "symbol_ret_pct": round(sym_ret, 2),
                "gap_pct": round(gap, 2),
                "raw_gap_pct": round(float(metrics["raw_gap_pct"]), 2),
                "expected_ret_pct": round(float(metrics["expected_ret_pct"]), 2),
                "beta": round(float(metrics["beta"]), 3),
                "correlation": round(float(metrics["correlation"]), 3),
                "gap_zscore": round(float(metrics["gap_zscore"]), 2),
                "sync_mode": metrics["sync_mode"],
                "last_body_pct": round(float(metrics["last_body_pct"]), 3),
                "last_ts": metrics["last_ts"],
                "vol_ratio": round(vol_ratio, 2),
                "spread_pct": round(float(row.get("spread_pct", 0) or 0), 3),
                "volume_usd": row["volume_usd"],
                "volume_label": _vol_label(row["volume_usd"]),
                "score": round(score, 2),
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        for i, row in enumerate(candidates):
            row["rank"] = i + 1
        _btc_sync_cache = {"data": candidates, "ts": now}
        return candidates
    except Exception as e:
        print(f"[BTCSyncRadar] 조회 실패: {e}")
        return _btc_sync_cache.get("data", [])
