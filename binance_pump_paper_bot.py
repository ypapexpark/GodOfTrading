"""Binance 전종목 급등 초입 레이더 + 체결가능성 반영 paper 엔진.

공개 market-data endpoint만 사용한다. API key, 잔고 조회, 주문 endpoint는 없다.
최근 30일 +30% 급등 연구의 공통점과 OI/시간봉 테이커 게이트를 탐지에 쓰되,
OOS 기대값이 음수였으므로 LIVE로 자동 승격하지 않는다. forming 15m 신호를 더
일찍 수집해 forward 성과를 쌓고, 고점 재돌파 확인 후에만 가상 진입한다.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import statistics
import time
import warnings
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Optional

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)
import requests
from dotenv import load_dotenv

from bot_util import append_jsonl, load_json, now_kst, read_jsonl, save_json
from binance_api_guard import (
    api_backoff_remaining,
    record_api_error,
    reserve_api_weight,
)
from process_lock import release, try_acquire
from publisher import send_signal


ROOT = Path(__file__).parent
STATE_FILE = ROOT / "binance_pump_paper_state.json"
JOURNAL_FILE = ROOT / "binance_pump_paper_journal.jsonl"
API_BASE = "https://fapi.binance.com"
POLICY = "binance_pump_forming15m_oi_confirm_v2"
LOCK_NAME = "binance_pump_paper"
HOUR_MS = 60 * 60_000

# Universe / scan policy.  One all-ticker call per cycle, then only the most
# relevant contracts receive a kline request.
MIN_QVOL_24H = 3_000_000.0
MAX_SHORTLIST = 80
KLINE_WORKERS = 8
TOP_PRICE_MOVERS = 50
TOP_VOLUME_ACCEL = 30
UNIVERSE_TTL_SECONDS = 6 * 3600
HISTORY_SECONDS = 75 * 60

# Research-derived signal.  These values are deliberately not claimed as a
# profitable live rule: the latest held-out test was negative.
MIN_RET_15M_PCT = 1.5
MIN_RET_1H_PCT = 4.0
MIN_VOL_RATIO = 4.0
MIN_BODY_RATIO = 0.65
MIN_TAKER_BUY_RATIO = 0.52
MIN_DAY_GAIN_PCT = 2.0
MAX_DAY_GAIN_PCT = 8.0
MAX_VWAP_DISLOC_PCT = 6.0
MAX_COMPRESSION_RATIO = 1.40
MAX_SPREAD_PCT = 0.12

# Public Binance derivatives aggregates.  The pump-event study found 6h/24h
# OI growth and hourly aggressive-buy flow to be the only useful non-price
# discriminators.  The broadest economically meaningful thresholds are used;
# stricter train-selected levels left almost no OOS trades and still lost.
MIN_OI_CHANGE_6H_PCT = 0.0
MIN_OI_CHANGE_24H_PCT = 0.0
MIN_HOURLY_TAKER_BUY_SELL_RATIO = 1.0

# Forward paper execution policy.
CONFIRM_BUFFER_PCT = 0.10
CONFIRM_TTL_SECONDS = 60 * 60
MAX_CONFIRM_GAP_PCT = 0.50
SYMBOL_COOLDOWN_SECONDS = 12 * 3600
PAPER_INITIAL_BANKROLL = 1_000.0
PAPER_RISK_PCT = 0.0025
PAPER_NOTIONAL_CAP_PCT = 0.15
MAX_OPEN_POSITIONS = 5
MIN_STOP_PCT = 1.8
MAX_STOP_PCT = 3.0
TP1_PCT = 4.0
TP1_SIZE = 0.45
TP2_PCT = 8.0
TP2_SIZE = 0.30
TRAIL_PCT = 4.0
NO_FOLLOW_SECONDS = 60 * 60
TIMEOUT_SECONDS = 8 * 3600
# taker fee 5bp + assumed one-way slippage 3bp.
ONE_WAY_COST_RATE = 0.0008
REPORT_INTERVAL_SECONDS = 4 * 3600
REPORT_RETRY_SECONDS = 5 * 60


def _default_state() -> dict[str, Any]:
    now = time.time()
    return {
        "policy": POLICY,
        "policy_started_ts": now,
        "policy_started_at": now_kst(),
        "bankroll": PAPER_INITIAL_BANKROLL,
        "initial_bankroll": PAPER_INITIAL_BANKROLL,
        "universe": [],
        "universe_updated_ts": 0.0,
        "price_history": {},
        "ticker_snapshot": {},
        "pending": {},
        "positions": {},
        "last_signal_ts": {},
        "last_derivative_gate": {},
        "last_scan": {},
        "last_report_time": 0.0,
        "last_report_attempt_time": 0.0,
        "last_report_delivered": False,
    }


def _load_state() -> dict[str, Any]:
    default = _default_state()
    raw = load_json(STATE_FILE, {}) or {}
    if not isinstance(raw, dict) or raw.get("policy") != POLICY:
        return default
    default.update(raw)
    for key in (
        "price_history", "ticker_snapshot", "pending", "positions",
        "last_signal_ts", "last_derivative_gate", "last_scan",
    ):
        if not isinstance(default.get(key), dict):
            default[key] = {}
    return default


def _api_get(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    if api_backoff_remaining() > 0:
        raise RuntimeError("Binance shared API backoff active")
    params = params or {}
    if path == "/fapi/v1/ticker/24hr" and not params.get("symbol"):
        weight = 40
    elif path == "/fapi/v1/klines":
        limit = int(params.get("limit") or 500)
        weight = 1 if limit < 100 else 2 if limit < 500 else 5 if limit <= 1000 else 10
    else:
        weight = 1
    last_error: Optional[Exception] = None
    for attempt in range(2):
        try:
            reserve_api_weight(weight)
            response = requests.get(API_BASE + path, params=params, timeout=8)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            record_api_error(exc)
            last_error = exc
            if attempt == 0:
                time.sleep(0.25)
    raise RuntimeError(f"Binance public API failed: {path}: {last_error}")


def _active_crypto_symbols(state: dict[str, Any]) -> list[str]:
    now = time.time()
    cached = state.get("universe") or []
    if cached and now - float(state.get("universe_updated_ts") or 0) < UNIVERSE_TTL_SECONDS:
        return [str(symbol) for symbol in cached]

    info = _api_get("/fapi/v1/exchangeInfo")
    symbols = []
    for market in info.get("symbols") or []:
        if market.get("status") != "TRADING":
            continue
        if market.get("contractType") != "PERPETUAL" or market.get("quoteAsset") != "USDT":
            continue
        underlying = str(market.get("underlyingType") or "").upper()
        subtypes = " ".join(str(x).upper() for x in (market.get("underlyingSubType") or []))
        if underlying == "TRADIFI" or "TRADIFI" in subtypes:
            continue
        symbols.append(str(market["symbol"]))
    state["universe"] = sorted(set(symbols))
    state["universe_updated_ts"] = now
    return state["universe"]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _fetch_tickers(symbols: set[str]) -> dict[str, dict[str, float]]:
    rows = _api_get("/fapi/v1/ticker/24hr")
    result: dict[str, dict[str, float]] = {}
    for raw in rows:
        symbol = str(raw.get("symbol") or "")
        if symbol not in symbols:
            continue
        last = _safe_float(raw.get("lastPrice"))
        qvol = _safe_float(raw.get("quoteVolume"))
        if last <= 0 or qvol <= 0:
            continue
        bid = _safe_float(raw.get("bidPrice"), last)
        ask = _safe_float(raw.get("askPrice"), last)
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
        spread = (ask - bid) / mid * 100 if mid > 0 and ask >= bid else 99.0
        result[symbol] = {
            "last": last,
            "bid": bid,
            "ask": ask,
            "qvol": qvol,
            "change_pct": _safe_float(raw.get("priceChangePercent")),
            "spread_pct": spread,
        }
    return result


def _record_price_history(state: dict[str, Any], tickers: dict[str, dict[str, float]], now: float) -> None:
    cutoff = now - HISTORY_SECONDS
    histories = state["price_history"]
    for symbol, ticker in tickers.items():
        history = histories.get(symbol) or []
        history = [row for row in history if len(row) >= 2 and float(row[0]) >= cutoff]
        history.append([round(now, 3), ticker["last"]])
        # Launchd can catch up after sleep; identical timestamps add no value.
        compact = []
        for row in history:
            if compact and abs(float(row[0]) - float(compact[-1][0])) < 1:
                compact[-1] = row
            else:
                compact.append(row)
        histories[symbol] = compact[-160:]
    for symbol in list(histories):
        if symbol not in tickers:
            histories.pop(symbol, None)


def _observed_return(state: dict[str, Any], symbol: str, now: float, seconds: int) -> float:
    history = state["price_history"].get(symbol) or []
    if len(history) < 2:
        return 0.0
    current = _safe_float(history[-1][1])
    target = now - seconds
    prior = None
    for row in history:
        if float(row[0]) <= target:
            prior = _safe_float(row[1])
        else:
            break
    if not prior or current <= 0:
        return 0.0
    return (current / prior - 1) * 100


def _shortlist(state: dict[str, Any], tickers: dict[str, dict[str, float]], now: float) -> list[str]:
    previous = state.get("ticker_snapshot") or {}
    liquid = [
        symbol for symbol, ticker in tickers.items()
        if ticker["qvol"] >= MIN_QVOL_24H
    ]
    by_price = sorted(liquid, key=lambda s: tickers[s]["change_pct"], reverse=True)
    by_volume = sorted(
        liquid,
        key=lambda s: tickers[s]["qvol"] - _safe_float((previous.get(s) or {}).get("qvol")),
        reverse=True,
    )
    observed = sorted(
        liquid,
        key=lambda s: max(
            _observed_return(state, s, now, 5 * 60),
            _observed_return(state, s, now, 15 * 60),
        ),
        reverse=True,
    )
    priority = (
        list(state.get("positions") or {})
        + list(state.get("pending") or {})
        + by_price[:TOP_PRICE_MOVERS]
        + by_volume[:TOP_VOLUME_ACCEL]
        + observed[:20]
    )
    selected = []
    for symbol in priority:
        if symbol in tickers and symbol not in selected:
            selected.append(symbol)
        if len(selected) >= MAX_SHORTLIST:
            break
    state["ticker_snapshot"] = {
        symbol: {"qvol": row["qvol"], "last": row["last"]}
        for symbol, row in tickers.items()
    }
    return selected


def _fetch_klines(symbol: str, interval: str = "15m", limit: int = 110) -> list[list[Any]]:
    rows = _api_get(
        "/fapi/v1/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )
    return rows if isinstance(rows, list) else []


def compute_derivative_features(
    oi_rows: list[dict[str, Any]],
    taker_rows: list[dict[str, Any]],
) -> Optional[dict[str, float]]:
    """Compute only completed-hour features used by the live forward gate."""
    oi = []
    for raw in oi_rows:
        timestamp = int(raw.get("timestamp") or 0)
        value = _safe_float(raw.get("sumOpenInterestValue"), -1.0)
        if timestamp > 0 and value > 0:
            oi.append((timestamp, value))
    oi.sort()
    if len(oi) < 25:
        return None

    current_ts, current = oi[-1]

    def prior(hours: int) -> Optional[float]:
        target = current_ts - hours * HOUR_MS
        chosen = None
        for timestamp, value in oi:
            if timestamp <= target:
                chosen = value
            else:
                break
        return chosen

    prior_6h = prior(6)
    prior_24h = prior(24)
    if not prior_6h or not prior_24h:
        return None

    taker = sorted(
        (
            int(raw.get("timestamp") or 0),
            _safe_float(raw.get("buySellRatio"), -1.0),
        )
        for raw in taker_rows
        if int(raw.get("timestamp") or 0) > 0
    )
    taker = [row for row in taker if row[1] > 0 and row[0] <= current_ts]
    if not taker:
        return None
    return {
        "derivative_bucket_ts": float(current_ts),
        "oi_change_6h_pct": (current / prior_6h - 1) * 100,
        "oi_change_24h_pct": (current / prior_24h - 1) * 100,
        "hourly_taker_buy_sell_ratio": taker[-1][1],
    }


def _fetch_derivative_features(symbol: str, now_ms: int) -> Optional[dict[str, float]]:
    # Exclude the current incomplete hour.  Historical statistics can otherwise
    # leak future trades into a forming 15m signal.
    end_ms = (now_ms // HOUR_MS) * HOUR_MS - 1
    params = {"symbol": symbol, "period": "1h", "limit": 30, "endTime": end_ms}
    oi_rows = _api_get("/futures/data/openInterestHist", params)
    taker_rows = _api_get("/futures/data/takerlongshortRatio", params)
    if not isinstance(oi_rows, list) or not isinstance(taker_rows, list):
        return None
    return compute_derivative_features(oi_rows, taker_rows)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def compute_features(rows: list[list[Any]], now_ms: Optional[int] = None) -> Optional[dict[str, Any]]:
    """Compute the exact observable inputs used by the forming-candle radar."""
    if len(rows) < 100:
        return None
    now_ms = int(now_ms or time.time() * 1000)
    parsed = []
    for row in rows:
        if len(row) < 11:
            return None
        parsed.append({
            "ts": int(row[0]),
            "open": _safe_float(row[1]),
            "high": _safe_float(row[2]),
            "low": _safe_float(row[3]),
            "close": _safe_float(row[4]),
            "volume": _safe_float(row[5]),
            "close_ts": int(row[6]),
            "quote_volume": _safe_float(row[7]),
            "taker_buy_volume": _safe_float(row[9]),
        })
    current = parsed[-1]
    if min(current["open"], current["high"], current["low"], current["close"]) <= 0:
        return None

    previous_volumes = [bar["volume"] for bar in parsed[-33:-1] if bar["volume"] > 0]
    prior_median = statistics.median(previous_volumes) if previous_volumes else 0.0
    candle_range = current["high"] - current["low"]
    body_ratio = (current["close"] - current["open"]) / candle_range if candle_range > 0 else 0.0

    tr_pct = []
    for index, bar in enumerate(parsed):
        prev_close = parsed[index - 1]["close"] if index else bar["close"]
        tr = max(
            bar["high"] - bar["low"],
            abs(bar["high"] - prev_close),
            abs(bar["low"] - prev_close),
        )
        tr_pct.append(tr / bar["close"] * 100 if bar["close"] > 0 else 0.0)
    prior_tr = tr_pct[:-1]
    compression = _mean(prior_tr[-8:]) / _mean(prior_tr[-32:]) if _mean(prior_tr[-32:]) > 0 else 99.0

    window = parsed[-96:]
    rolling_volume = sum(bar["volume"] for bar in window)
    vwap = (
        sum(((bar["high"] + bar["low"] + bar["close"]) / 3) * bar["volume"] for bar in window)
        / rolling_volume
        if rolling_volume > 0 else 0.0
    )
    utc_day_ms = current["ts"] // 86_400_000 * 86_400_000
    day_bars = [bar for bar in parsed if bar["ts"] >= utc_day_ms]
    day_open = day_bars[0]["open"] if day_bars else current["open"]
    prior_high = max(bar["high"] for bar in parsed[-97:-1])
    return {
        "signal_bar_ts": current["ts"],
        "forming": current["close_ts"] >= now_ms,
        "open": current["open"],
        "high": current["high"],
        "low": current["low"],
        "close": current["close"],
        "ret_15m_pct": (current["close"] / current["open"] - 1) * 100,
        "ret_1h_pct": (current["close"] / parsed[-5]["close"] - 1) * 100,
        "vol_ratio": current["volume"] / prior_median if prior_median > 0 else 0.0,
        "body_ratio": body_ratio,
        "taker_buy_ratio": current["taker_buy_volume"] / current["volume"] if current["volume"] > 0 else 0.0,
        "compression_ratio": compression,
        "vwap_disloc_pct": (current["close"] / vwap - 1) * 100 if vwap > 0 else 99.0,
        "day_gain_pct": (current["close"] / day_open - 1) * 100 if day_open > 0 else 0.0,
        "qvol_24h": sum(bar["quote_volume"] for bar in window),
        "breakout": current["close"] >= prior_high,
    }


def signal_reasons(features: dict[str, Any], ticker: dict[str, float]) -> list[str]:
    reasons = []
    checks = (
        (bool(features.get("forming")), "closed_candle"),
        (bool(features.get("breakout")), "no_24h_breakout"),
        (_safe_float(features.get("ret_15m_pct")) >= MIN_RET_15M_PCT, "ret15"),
        (_safe_float(features.get("ret_1h_pct")) >= MIN_RET_1H_PCT, "ret1h"),
        (_safe_float(features.get("vol_ratio")) >= MIN_VOL_RATIO, "volume"),
        (_safe_float(features.get("body_ratio")) >= MIN_BODY_RATIO, "body"),
        (_safe_float(features.get("taker_buy_ratio")) >= MIN_TAKER_BUY_RATIO, "taker_buy"),
        (MIN_DAY_GAIN_PCT <= _safe_float(features.get("day_gain_pct")) <= MAX_DAY_GAIN_PCT, "day_gain"),
        (0 <= _safe_float(features.get("vwap_disloc_pct")) <= MAX_VWAP_DISLOC_PCT, "vwap"),
        (_safe_float(features.get("compression_ratio"), 99) <= MAX_COMPRESSION_RATIO, "compression"),
        (_safe_float(features.get("qvol_24h")) >= MIN_QVOL_24H, "liquidity"),
        (_safe_float(ticker.get("spread_pct"), 99) <= MAX_SPREAD_PCT, "spread"),
    )
    for ok, label in checks:
        if not ok:
            reasons.append(label)
    return reasons


def derivative_signal_reasons(features: dict[str, Any]) -> list[str]:
    checks = (
        (_safe_float(features.get("oi_change_6h_pct"), -999) >= MIN_OI_CHANGE_6H_PCT, "oi6h"),
        (_safe_float(features.get("oi_change_24h_pct"), -999) >= MIN_OI_CHANGE_24H_PCT, "oi24h"),
        (
            _safe_float(features.get("hourly_taker_buy_sell_ratio"), -1)
            >= MIN_HOURLY_TAKER_BUY_SELL_RATIO,
            "hourly_taker_buy",
        ),
    )
    return [label for ok, label in checks if not ok]


def _create_pending(symbol: str, features: dict[str, Any], ticker: dict[str, float], now: float) -> dict[str, Any]:
    signal_high = _safe_float(features["high"])
    return {
        "symbol": symbol,
        "created_ts": now,
        "created_at": now_kst(),
        "expires_ts": now + CONFIRM_TTL_SECONDS,
        "signal_bar_ts": int(features["signal_bar_ts"]),
        "signal_open": _safe_float(features["open"]),
        "signal_high": signal_high,
        "signal_low": _safe_float(features["low"]),
        "signal_mid": (_safe_float(features["open"]) + signal_high) / 2,
        "trigger": signal_high * (1 + CONFIRM_BUFFER_PCT / 100),
        "features": features,
        "spread_pct": ticker["spread_pct"],
    }


def _journal(event: str, **payload: Any) -> None:
    append_jsonl(JOURNAL_FILE, {"event": event, "ts": time.time(), "at": now_kst(), **payload})


def _open_paper_position(
    state: dict[str, Any], pending: dict[str, Any], ticker: dict[str, float], now: float,
) -> Optional[dict[str, Any]]:
    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        _journal("confirmation_blocked", symbol=pending["symbol"], reason="max_open")
        return None
    trigger = _safe_float(pending["trigger"])
    ask = max(_safe_float(ticker.get("ask")), _safe_float(ticker.get("last")))
    if ask <= 0 or ask < trigger:
        return None
    gap_pct = (ask / trigger - 1) * 100
    if gap_pct > MAX_CONFIRM_GAP_PCT:
        _journal(
            "confirmation_blocked", symbol=pending["symbol"], reason="gap_chase",
            gap_pct=gap_pct, trigger=trigger, ask=ask,
        )
        return None

    entry = ask * 1.0001
    signal_low = _safe_float(pending["signal_low"])
    structural_pct = (entry - signal_low) / entry * 100 if entry > signal_low else MAX_STOP_PCT
    stop_pct = min(max(structural_pct, MIN_STOP_PCT), MAX_STOP_PCT)
    bankroll = max(_safe_float(state.get("bankroll")), 0.0)
    risk_notional = bankroll * PAPER_RISK_PCT / (stop_pct / 100)
    notional = min(bankroll * PAPER_NOTIONAL_CAP_PCT, risk_notional)
    committed = sum(_safe_float(pos.get("notional")) for pos in state["positions"].values())
    notional = min(notional, max(0.0, bankroll * 0.75 - committed))
    if notional < 5:
        _journal("confirmation_blocked", symbol=pending["symbol"], reason="paper_capacity")
        return None

    position_id = f"{pending['symbol']}-{int(now * 1000)}"
    position = {
        "id": position_id,
        "symbol": pending["symbol"],
        "entry_ts": now,
        "entry_at": now_kst(),
        "entry": entry,
        "notional": notional,
        "initial_stop_pct": stop_pct,
        "stop": entry * (1 - stop_pct / 100),
        "tp1": entry * (1 + TP1_PCT / 100),
        "tp2": entry * (1 + TP2_PCT / 100),
        "remaining": 1.0,
        "tp1_done": False,
        "tp2_done": False,
        "highest": entry,
        "mfe_pct": 0.0,
        "mae_pct": 0.0,
        "realized_gross_usd": 0.0,
        "fees_usd": notional * ONE_WAY_COST_RATE,
        "signal_mid": pending["signal_mid"],
        "last_bar_ts": int(now * 1000) // 60_000 * 60_000,
        "features": pending["features"],
        "policy": POLICY,
    }
    state["positions"][position_id] = position
    _journal("paper_open", **position)
    return position


def _close_fraction(position: dict[str, Any], fraction: float, price: float) -> None:
    fraction = min(max(fraction, 0.0), _safe_float(position.get("remaining")))
    if fraction <= 0:
        return
    notional = _safe_float(position["notional"])
    entry = _safe_float(position["entry"])
    position["realized_gross_usd"] += notional * fraction * (price / entry - 1)
    position["fees_usd"] += notional * fraction * ONE_WAY_COST_RATE
    position["remaining"] = max(0.0, _safe_float(position["remaining"]) - fraction)


def _settle_position(
    state: dict[str, Any], position_id: str, price: float, reason: str, bar_ts: int,
) -> dict[str, Any]:
    position = state["positions"][position_id]
    _close_fraction(position, _safe_float(position["remaining"]), price)
    net = _safe_float(position["realized_gross_usd"]) - _safe_float(position["fees_usd"])
    net_pct = net / _safe_float(position["notional"]) * 100
    state["bankroll"] = _safe_float(state.get("bankroll")) + net
    settled = {
        **position,
        "exit_ts": bar_ts / 1000,
        "exit_at": datetime.fromtimestamp(bar_ts / 1000, timezone.utc).isoformat(),
        "exit_price": price,
        "exit_reason": reason,
        "net_usd": net,
        "net_pct": net_pct,
        "bankroll_after": state["bankroll"],
    }
    state["positions"].pop(position_id, None)
    _journal("paper_settled", **settled)
    return settled


def _apply_completed_bar(
    state: dict[str, Any], position_id: str, row: list[Any], now: float,
) -> Optional[dict[str, Any]]:
    position = state["positions"][position_id]
    bar_ts = int(row[0])
    high = _safe_float(row[2])
    low = _safe_float(row[3])
    close = _safe_float(row[4])
    entry = _safe_float(position["entry"])
    if min(high, low, close, entry) <= 0:
        return None
    position["mfe_pct"] = max(_safe_float(position["mfe_pct"]), (high / entry - 1) * 100)
    position["mae_pct"] = min(_safe_float(position["mae_pct"]), (low / entry - 1) * 100)

    # Stop first is conservative when a one-minute bar contains both prices.
    if low <= _safe_float(position["stop"]):
        return _settle_position(state, position_id, _safe_float(position["stop"]), "stop", bar_ts)

    if not position["tp1_done"] and high >= _safe_float(position["tp1"]):
        _close_fraction(position, TP1_SIZE, _safe_float(position["tp1"]))
        position["tp1_done"] = True
        position["stop"] = max(_safe_float(position["stop"]), entry * 1.002)
        _journal("paper_partial", symbol=position["symbol"], level="tp1", price=position["tp1"])
    if not position["tp2_done"] and high >= _safe_float(position["tp2"]):
        _close_fraction(position, TP2_SIZE, _safe_float(position["tp2"]))
        position["tp2_done"] = True
        position["stop"] = max(_safe_float(position["stop"]), entry * 1.04)
        _journal("paper_partial", symbol=position["symbol"], level="tp2", price=position["tp2"])
    position["highest"] = max(_safe_float(position["highest"]), high)
    if position["tp2_done"]:
        position["stop"] = max(
            _safe_float(position["stop"]),
            _safe_float(position["highest"]) * (1 - TRAIL_PCT / 100),
        )

    age = now - _safe_float(position["entry_ts"])
    if (
        not position["tp1_done"] and age >= NO_FOLLOW_SECONDS
        and _safe_float(position["mfe_pct"]) < 2.0
        and close < max(entry, _safe_float(position["signal_mid"]))
    ):
        return _settle_position(state, position_id, close, "no_follow_through", bar_ts)
    if age >= TIMEOUT_SECONDS:
        return _settle_position(state, position_id, close, "timeout", bar_ts)
    return None


def _manage_positions(
    state: dict[str, Any], tickers: dict[str, dict[str, float]], now: float,
) -> list[dict[str, Any]]:
    settled = []
    now_ms = int(now * 1000)
    for position_id in list(state["positions"]):
        position = state["positions"].get(position_id)
        if not position:
            continue
        try:
            rows = _fetch_klines(position["symbol"], "1m", 12)
        except Exception as exc:
            print(f"[pump-paper] 1m {position['symbol']} failed: {exc}")
            continue
        for row in rows:
            if position_id not in state["positions"]:
                break
            bar_ts = int(row[0])
            close_ts = int(row[6])
            if bar_ts <= int(position.get("last_bar_ts") or 0) or close_ts >= now_ms:
                continue
            result = _apply_completed_bar(state, position_id, row, now)
            if result:
                settled.append(result)
                break
            state["positions"][position_id]["last_bar_ts"] = bar_ts
    return settled


def _confirm_pending(
    state: dict[str, Any], tickers: dict[str, dict[str, float]], now: float,
) -> list[dict[str, Any]]:
    opened = []
    for symbol in list(state["pending"]):
        pending = state["pending"][symbol]
        if now >= _safe_float(pending.get("expires_ts")):
            state["pending"].pop(symbol, None)
            _journal("confirmation_expired", symbol=symbol, pending=pending)
            continue
        ticker = tickers.get(symbol)
        if not ticker:
            continue
        trigger = _safe_float(pending.get("trigger"))
        ask = max(_safe_float(ticker.get("ask")), _safe_float(ticker.get("last")))
        if ask >= trigger > 0 and (ask / trigger - 1) * 100 > MAX_CONFIRM_GAP_PCT:
            state["pending"].pop(symbol, None)
            _journal(
                "confirmation_blocked", symbol=symbol, reason="gap_chase",
                gap_pct=(ask / trigger - 1) * 100, trigger=trigger, ask=ask,
            )
            continue
        if ask >= trigger > 0 and len(state["positions"]) >= MAX_OPEN_POSITIONS:
            state["pending"].pop(symbol, None)
            _journal("confirmation_blocked", symbol=symbol, reason="max_open")
            continue
        position = _open_paper_position(state, pending, ticker, now)
        if position:
            state["pending"].pop(symbol, None)
            opened.append(position)
    return opened


def _performance() -> dict[str, float]:
    settled = [row for row in read_jsonl(JOURNAL_FILE) if row.get("event") == "paper_settled"]
    pnls = [_safe_float(row.get("net_usd")) for row in settled]
    pct = [_safe_float(row.get("net_pct")) for row in settled]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    equity = peak = max_dd = 0.0
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "n": float(len(settled)),
        "win_rate_pct": len(wins) / len(settled) * 100 if settled else 0.0,
        "net_usd": sum(pnls),
        "avg_net_pct": statistics.mean(pct) if pct else 0.0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else (99.0 if wins else 0.0),
        "max_drawdown_usd": max_dd,
        "without_largest_win_usd": sum(pnls) - max(wins) if wins else sum(pnls),
    }


def _graduation(state: dict[str, Any], perf: dict[str, float]) -> tuple[bool, list[str]]:
    age_days = (time.time() - _safe_float(state.get("policy_started_ts"))) / 86_400
    checks = (
        (perf["n"] >= 60, f"정산 {int(perf['n'])}/60"),
        (age_days >= 14, f"관찰 {age_days:.1f}/14일"),
        (perf["profit_factor"] >= 1.30, f"PF {perf['profit_factor']:.2f}/1.30"),
        (perf["avg_net_pct"] >= 0.25, f"평균 {perf['avg_net_pct']:+.2f}%/+0.25%"),
        (perf["without_largest_win_usd"] > 0, "최대 1승 제외 순익 양수"),
        (perf["max_drawdown_usd"] <= _safe_float(state.get("initial_bankroll")) * 0.12, "DD 12% 이하"),
    )
    return all(ok for ok, _ in checks), [label for ok, label in checks if not ok]


def build_report(state: dict[str, Any]) -> str:
    perf = _performance()
    graduated, missing = _graduation(state, perf)
    scan = state.get("last_scan") or {}
    return "\n".join([
        "🚀 <b>Binance 급등 레이더 · PAPER</b>",
        f"정책 <code>{escape(POLICY)}</code>",
        f"전체 {int(scan.get('ticker_count') or 0)} · 정밀검사 {int(scan.get('shortlist') or 0)} · "
        f"가격후보 {int(scan.get('derivative_candidates') or 0)} · "
        f"OI통과 {int(scan.get('derivative_passed') or 0)} · 신호 {int(scan.get('signals') or 0)}",
        f"대기 {len(state['pending'])} · 보유 {len(state['positions'])}/{MAX_OPEN_POSITIONS}",
        f"가상시드 <b>${_safe_float(state.get('bankroll')):,.2f}</b> "
        f"({_safe_float(state.get('bankroll')) - _safe_float(state.get('initial_bankroll')):+.2f})",
        f"정산 {int(perf['n'])} · 승률 {perf['win_rate_pct']:.1f}% · "
        f"PF {perf['profit_factor']:.2f} · 평균 {perf['avg_net_pct']:+.2f}%",
        f"누적 ${perf['net_usd']:+.2f} · 최대DD ${perf['max_drawdown_usd']:.2f}",
        "졸업게이트: " + ("통과 후보(자동 LIVE 아님)" if graduated else "미통과 — " + ", ".join(missing[:4])),
        "※ OI 강화 OOS도 1건 -2.90%. 실주문 없음, 체결비용 포함 forward 검증 전용.",
    ])


def _signal_message(opened: list[dict[str, Any]], settled: list[dict[str, Any]]) -> str:
    lines = ["🚀 <b>급등 레이더 PAPER 이벤트</b>"]
    for position in opened[:6]:
        f = position.get("features") or {}
        lines.append(
            f"진입 <b>{escape(position['symbol'])}</b> ${position['entry']:.8g} · "
            f"15m {f.get('ret_15m_pct', 0):+.1f}% · 1h {f.get('ret_1h_pct', 0):+.1f}% · "
            f"거래량 {f.get('vol_ratio', 0):.1f}x · 매수 {f.get('taker_buy_ratio', 0)*100:.0f}%"
            f" · OI6h {f.get('oi_change_6h_pct', 0):+.1f}%"
            f" · OI24h {f.get('oi_change_24h_pct', 0):+.1f}%"
            f" · 시간봉 T {f.get('hourly_taker_buy_sell_ratio', 0):.2f}"
        )
    for trade in settled[:6]:
        lines.append(
            f"청산 <b>{escape(trade['symbol'])}</b> {trade['net_pct']:+.2f}% "
            f"(${trade['net_usd']:+.2f}) · {escape(trade['exit_reason'])}"
        )
    lines.append("※ PAPER ONLY · 실제 주문 아님")
    return "\n".join(lines)


def _maybe_send_periodic_report(
    state: dict[str, Any], now: float, *, report_now: bool = False
) -> bool:
    """Retry an undelivered report after reconnect instead of waiting four hours."""
    due = now - _safe_float(state.get("last_report_time")) >= REPORT_INTERVAL_SECONDS
    retry_ready = (
        now - _safe_float(state.get("last_report_attempt_time"))
        >= REPORT_RETRY_SECONDS
    )
    if not report_now and (not due or not retry_ready):
        return False

    state["last_report_attempt_time"] = now
    delivered = send_signal(build_report(state))
    state["last_report_delivered"] = delivered
    if delivered:
        state["last_report_time"] = now
    return delivered


def run_once(*, report_now: bool = False, telegram: bool = True) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    state = _load_state()
    now = time.time()
    symbols = set(_active_crypto_symbols(state))
    tickers = _fetch_tickers(symbols)
    _record_price_history(state, tickers, now)

    settled = _manage_positions(state, tickers, now)
    opened = _confirm_pending(state, tickers, now)
    shortlist = _shortlist(state, tickers, now)

    signals = 0
    failures = 0
    scan_symbols = []
    for symbol in shortlist:
        if symbol in state["pending"]:
            continue
        if any(pos.get("symbol") == symbol for pos in state["positions"].values()):
            continue
        if now - _safe_float(state["last_signal_ts"].get(symbol)) < SYMBOL_COOLDOWN_SECONDS:
            continue
        scan_symbols.append(symbol)

    feature_results: dict[str, Optional[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=KLINE_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_klines, symbol): symbol for symbol in scan_symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                feature_results[symbol] = compute_features(
                    future.result(), int(now * 1000)
                )
            except Exception as exc:
                failures += 1
                print(f"[pump-paper] 15m {symbol} failed: {exc}")

    price_candidates = [
        symbol for symbol in scan_symbols
        if feature_results.get(symbol)
        and not signal_reasons(feature_results[symbol], tickers[symbol])
    ]
    derivative_results: dict[str, Optional[dict[str, float]]] = {}
    with ThreadPoolExecutor(max_workers=min(KLINE_WORKERS, 4)) as pool:
        futures = {
            pool.submit(_fetch_derivative_features, symbol, int(now * 1000)): symbol
            for symbol in price_candidates
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                derivative_results[symbol] = future.result()
            except Exception as exc:
                failures += 1
                derivative_results[symbol] = None
                print(f"[pump-paper] derivatives {symbol} failed: {exc}")

    # State writes remain single-threaded and deterministic.
    derivative_passed = 0
    derivative_blocked = 0
    for symbol in scan_symbols:
        features = feature_results.get(symbol)
        if not features or signal_reasons(features, tickers[symbol]):
            continue
        derivative = derivative_results.get(symbol)
        gate_reasons = ["derivative_data"] if not derivative else derivative_signal_reasons(derivative)
        gate_bar = int(features.get("signal_bar_ts") or 0)
        if gate_reasons:
            derivative_blocked += 1
            if int(state["last_derivative_gate"].get(symbol) or 0) != gate_bar:
                state["last_derivative_gate"][symbol] = gate_bar
                _journal(
                    "derivative_blocked",
                    symbol=symbol,
                    signal_bar_ts=gate_bar,
                    reasons=gate_reasons,
                    price_features=features,
                    derivative_features=derivative or {},
                )
            continue
        features.update(derivative)
        derivative_passed += 1
        pending = _create_pending(symbol, features, tickers[symbol], now)
        state["pending"][symbol] = pending
        state["last_signal_ts"][symbol] = now
        _journal("signal_pending", **pending)
        signals += 1

    state["last_scan"] = {
        "time": now_kst(),
        "ticker_count": len(tickers),
        "shortlist": len(shortlist),
        "derivative_candidates": len(price_candidates),
        "derivative_passed": derivative_passed,
        "derivative_blocked": derivative_blocked,
        "signals": signals,
        "opened": len(opened),
        "settled": len(settled),
        "failures": failures,
    }
    if telegram and (opened or settled):
        send_signal(_signal_message(opened, settled))

    reported = False
    if telegram:
        reported = _maybe_send_periodic_report(
            state, now, report_now=report_now
        )
    save_json(STATE_FILE, state)
    return {
        "ok": True,
        "account": "binance_pump_paper",
        **state["last_scan"],
        "pending": len(state["pending"]),
        "open_positions": len(state["positions"]),
        "bankroll": state["bankroll"],
        "reported": reported,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-now", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--interval", type=float, default=30.0)
    args = parser.parse_args(argv)
    if not try_acquire(LOCK_NAME):
        return 0
    result: dict[str, Any] = {"ok": True}
    try:
        first = True
        while True:
            cycle_started = time.monotonic()
            try:
                result = run_once(
                    report_now=bool(args.report_now and first),
                    telegram=not args.no_telegram,
                )
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
                print(f"[pump-paper] cycle failed: {exc}", flush=True)
            if args.json and not args.daemon:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            else:
                print(f"[pump-paper] {result}", flush=True)
            if not args.daemon:
                break
            first = False
            elapsed = time.monotonic() - cycle_started
            time.sleep(max(1.0, float(args.interval) - elapsed))
    except KeyboardInterrupt:
        result = {"ok": True, "stopped": True}
    finally:
        release(LOCK_NAME)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
