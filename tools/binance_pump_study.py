"""Recent Binance USD-M pump event study and walk-forward trigger search.

The study deliberately separates three questions:

1. Which completed UTC days printed a high at least 30% above the daily open?
2. What was observable immediately before / at the first +5% breakout?
3. Can a rule selected on the first 20 days retain positive expectancy on the
   final 10 days when entered at the *next* 15-minute open?

It is a research tool only.  It never imports API keys and never places orders.
Public OHLCV/funding data are cached below /tmp to make reruns reproducible.
"""
from __future__ import annotations

import argparse
import gzip
import itertools
import json
import math
import statistics
import time
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)

import ccxt
import numpy as np
import pandas as pd


DAY_MS = 86_400_000
BAR_MS = 15 * 60_000
STUDY_DAYS = 30
TRAIN_DAYS = 20
WARMUP_DAYS = 10
MIN_PRIOR_DAILY_BARS = 20
MIN_PRIOR_DAILY_QVOL = 3_000_000.0
MIN_ROLLING_24H_QVOL = 3_000_000.0
ROUND_TRIP_COST_PCT = 0.16

FEATURE_NAMES = [
    "ret_bar_pct",
    "ret_1h_pct",
    "ret_3h_pct",
    "ret_6h_pct",
    "ret_24h_pct",
    "vol_ratio",
    "body_ratio",
    "vwap_disloc_pct",
    "compression_ratio",
    "atr_pct",
    "day_gain_pct",
    "qvol_24h",
    "taker_buy_ratio",
    "funding_rate_pct",
]


def _utc_day(ms: int) -> int:
    return int(ms // DAY_MS * DAY_MS)


def _iso_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def _safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_")


def _json_dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _load_gzip_json(path: Path):
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _save_gzip_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))


def _fetch_range(exchange, symbol: str, timeframe: str, start_ms: int,
                 end_ms: int, limit: int = 1500) -> list[list[float]]:
    step = DAY_MS if timeframe == "1d" else BAR_MS
    cursor = int(start_ms)
    rows: list[list[float]] = []
    seen = set()
    while cursor < end_ms:
        if timeframe == "15m":
            market = exchange.market(symbol)
            raw_batch = exchange.fapiPublicGetKlines({
                "symbol": market["id"],
                "interval": "15m",
                "startTime": cursor,
                "endTime": end_ms - 1,
                "limit": limit,
            })
            batch = [
                [
                    int(row[0]), float(row[1]), float(row[2]), float(row[3]),
                    float(row[4]), float(row[5]), float(row[9]),
                ]
                for row in raw_batch
            ]
        else:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=limit)
        if not batch:
            break
        for row in batch:
            ts = int(row[0])
            if start_ms <= ts < end_ms and ts not in seen:
                rows.append(row[:7])
                seen.add(ts)
        last_ts = int(batch[-1][0])
        next_cursor = last_ts + step
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        # Binance caps klines below the requested CCXT limit (commonly 1000).
        # A short page therefore does not mean the requested time range ended.
        if last_ts + step >= end_ms:
            break
    rows.sort(key=lambda row: row[0])
    return rows


def _cached_range(exchange, cache_path: Path, symbol: str, timeframe: str,
                  start_ms: int, end_ms: int) -> list[list[float]]:
    cached = _load_gzip_json(cache_path)
    if isinstance(cached, dict):
        rows = cached.get("rows") or []
        covers_end = bool(rows) and int(rows[-1][0]) + (
            DAY_MS if timeframe == "1d" else BAR_MS
        ) >= end_ms
        if (
            int(cached.get("start_ms", 0) or 0) <= start_ms
            and int(cached.get("end_ms", 0) or 0) >= end_ms
            and rows
            and covers_end
        ):
            return [row for row in rows if start_ms <= int(row[0]) < end_ms]
    rows = _fetch_range(exchange, symbol, timeframe, start_ms, end_ms)
    _save_gzip_json(cache_path, {
        "symbol": symbol,
        "timeframe": timeframe,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "rows": rows,
    })
    return rows


def _to_frame(rows: list[list[float]]) -> pd.DataFrame:
    columns = ["timestamp", "open", "high", "low", "close", "volume"]
    if rows and len(rows[0]) >= 7:
        columns.append("taker_buy_volume")
    frame = pd.DataFrame(
        [row[:len(columns)] for row in rows],
        columns=columns,
    )
    if frame.empty:
        return frame
    frame = frame.drop_duplicates("timestamp").sort_values("timestamp")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna().reset_index(drop=True)


def _active_symbols(exchange) -> list[str]:
    markets = exchange.load_markets()
    symbols = []
    for symbol, market in markets.items():
        if not market.get("active"):
            continue
        if not market.get("swap") or not market.get("linear"):
            continue
        if market.get("quote") != "USDT" or market.get("settle") != "USDT":
            continue
        info = market.get("info") or {}
        if str(info.get("underlyingType") or "").upper() != "COIN":
            continue
        if "TRADIFI" in str(info.get("contractType") or "").upper():
            continue
        base = str(market.get("base") or "").upper()
        if not base or any(token in base for token in ("BULL", "BEAR", "UP", "DOWN")):
            continue
        symbols.append(symbol)
    return sorted(set(symbols))


def _daily_events(daily_by_symbol: dict[str, list[list[float]]],
                  study_start: int, current_day: int) -> tuple[list[dict], set[str]]:
    events = []
    liquid_symbols = set()
    for symbol, rows in daily_by_symbol.items():
        if not rows:
            continue
        quote_volumes = [float(row[4]) * float(row[5]) for row in rows]
        recent_qvol = [
            quote_volumes[i]
            for i, row in enumerate(rows)
            if study_start <= int(row[0]) < current_day
        ]
        if len(rows) >= MIN_PRIOR_DAILY_BARS and recent_qvol:
            if statistics.median(recent_qvol) >= MIN_PRIOR_DAILY_QVOL:
                liquid_symbols.add(symbol)

        for index, row in enumerate(rows):
            ts, open_, high, low, close, volume = row
            ts = int(ts)
            if not (study_start <= ts < current_day) or float(open_) <= 0:
                continue
            high_open = (float(high) / float(open_) - 1) * 100
            if high_open < 30:
                continue
            prior = rows[max(0, index - 7):index]
            prior_qvol = [float(x[4]) * float(x[5]) for x in prior]
            prior_median = statistics.median(prior_qvol) if prior_qvol else 0.0
            prior_bars = index
            eligible = (
                prior_bars >= MIN_PRIOR_DAILY_BARS
                and prior_median >= MIN_PRIOR_DAILY_QVOL
            )
            if eligible:
                liquid_symbols.add(symbol)
            events.append({
                "symbol": symbol,
                "day_ms": ts,
                "date_utc": _iso_day(ts),
                "high_open_pct": round(high_open, 3),
                "close_open_pct": round((float(close) / float(open_) - 1) * 100, 3),
                "low_open_pct": round((float(low) / float(open_) - 1) * 100, 3),
                "prior_bars": prior_bars,
                "prior_7d_median_qvol": round(prior_median, 2),
                "eligible": eligible,
            })
    events.sort(key=lambda row: (row["day_ms"], row["symbol"]))
    return events, liquid_symbols


def _feature_frame(rows: list[list[float]]) -> pd.DataFrame:
    frame = _to_frame(rows)
    if frame.empty or len(frame) < 110:
        return pd.DataFrame()
    prev_close = frame["close"].shift(1)
    true_range = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - prev_close).abs(),
        (frame["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    tr_pct = true_range / frame["close"].replace(0, np.nan) * 100
    frame["atr_pct"] = tr_pct.rolling(14).mean()
    frame["compression_ratio"] = (
        tr_pct.shift(1).rolling(8).mean()
        / tr_pct.shift(1).rolling(32).mean().replace(0, np.nan)
    )
    frame["ret_bar_pct"] = (frame["close"] / frame["open"] - 1) * 100
    for label, bars in (("1h", 4), ("3h", 12), ("6h", 24), ("24h", 96)):
        frame[f"ret_{label}_pct"] = (frame["close"] / frame["close"].shift(bars) - 1) * 100
    prior_volume = frame["volume"].shift(1).rolling(32).median()
    frame["vol_ratio"] = frame["volume"] / prior_volume.replace(0, np.nan)
    candle_range = (frame["high"] - frame["low"]).replace(0, np.nan)
    frame["body_ratio"] = (frame["close"] - frame["open"]) / candle_range
    prior_high = frame["high"].shift(1).rolling(96).max()
    frame["breakout"] = frame["close"] >= prior_high
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3
    rolling_volume = frame["volume"].rolling(96).sum()
    frame["vwap_24h"] = (
        (typical * frame["volume"]).rolling(96).sum()
        / rolling_volume.replace(0, np.nan)
    )
    frame["vwap_disloc_pct"] = (frame["close"] / frame["vwap_24h"] - 1) * 100
    frame["qvol"] = frame["close"] * frame["volume"]
    frame["qvol_24h"] = frame["qvol"].rolling(96).sum()
    if "taker_buy_volume" in frame:
        frame["taker_buy_ratio"] = (
            frame["taker_buy_volume"] / frame["volume"].replace(0, np.nan)
        )
    else:
        frame["taker_buy_ratio"] = np.nan
    frame["funding_rate_pct"] = np.nan
    frame["day_ms"] = (frame["timestamp"].astype("int64") // DAY_MS) * DAY_MS
    day_open = frame.groupby("day_ms")["open"].transform("first")
    frame["day_gain_pct"] = (frame["close"] / day_open - 1) * 100
    return frame.replace([np.inf, -np.inf], np.nan)


def _cached_funding(exchange, cache_path: Path, symbol: str,
                    start_ms: int, end_ms: int) -> list[dict]:
    cached = _load_gzip_json(cache_path)
    if isinstance(cached, dict) and cached.get("rows"):
        if (
            int(cached.get("start_ms", 0) or 0) <= start_ms
            and int(cached.get("end_ms", 0) or 0) >= end_ms
        ):
            return cached["rows"]
    rows = exchange.fetch_funding_rate_history(symbol, since=start_ms, limit=1000)
    clean = []
    for row in rows:
        timestamp = int(row.get("timestamp") or 0)
        if not (start_ms <= timestamp < end_ms):
            continue
        rate = row.get("fundingRate")
        if rate is None:
            rate = (row.get("info") or {}).get("fundingRate")
        try:
            clean.append({"timestamp": timestamp, "rate": float(rate)})
        except (TypeError, ValueError):
            continue
    clean.sort(key=lambda row: row["timestamp"])
    _save_gzip_json(cache_path, {
        "symbol": symbol,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "rows": clean,
    })
    return clean


def _attach_funding(frame: pd.DataFrame, funding_rows: list[dict]) -> pd.DataFrame:
    if frame.empty or not funding_rows:
        return frame
    funding_ts = np.array([int(row["timestamp"]) for row in funding_rows], dtype=np.int64)
    funding_rate = np.array([float(row["rate"]) * 100 for row in funding_rows])
    candle_ts = frame["timestamp"].astype("int64").to_numpy()
    indices = np.searchsorted(funding_ts, candle_ts, side="right") - 1
    values = np.full(len(frame), np.nan)
    valid = indices >= 0
    values[valid] = funding_rate[indices[valid]]
    frame["funding_rate_pct"] = values
    return frame


def _snapshot(row: pd.Series, symbol: str, phase: str) -> dict:
    result = {
        "symbol": symbol,
        "phase": phase,
        "timestamp": int(row["timestamp"]),
        "time_utc": datetime.fromtimestamp(
            int(row["timestamp"]) / 1000, timezone.utc
        ).isoformat(),
        "breakout": bool(row.get("breakout", False)),
    }
    for name in FEATURE_NAMES:
        value = float(row.get(name, math.nan))
        result[name] = round(value, 6) if math.isfinite(value) else None
    return result


def _simulate_trade(frame: pd.DataFrame, signal_index: int,
                    horizon_bars: int = 48) -> dict | None:
    entry_index = signal_index + 1
    if entry_index >= len(frame):
        return None
    entry = float(frame.at[entry_index, "open"])
    if entry <= 0:
        return None
    stop = entry * 0.96
    tp1 = entry * 1.05
    tp2 = entry * 1.10
    remaining = 1.0
    gross = 0.0
    tp1_done = False
    tp2_done = False
    highest = entry
    exit_index = min(entry_index + horizon_bars - 1, len(frame) - 1)
    mfe = 0.0
    mae = 0.0

    for index in range(entry_index, exit_index + 1):
        high = float(frame.at[index, "high"])
        low = float(frame.at[index, "low"])
        close = float(frame.at[index, "close"])
        mfe = max(mfe, (high / entry - 1) * 100)
        mae = min(mae, (low / entry - 1) * 100)

        # Same-bar path is unknowable.  Stop-first is the conservative choice.
        if low <= stop:
            gross += remaining * (stop / entry - 1) * 100
            remaining = 0.0
            exit_index = index
            break

        if not tp1_done and high >= tp1:
            gross += 0.40 * 5.0
            remaining -= 0.40
            tp1_done = True
            stop = max(stop, entry * 1.003)

        if tp1_done and not tp2_done and high >= tp2:
            gross += 0.30 * 10.0
            remaining -= 0.30
            tp2_done = True
            highest = max(highest, high)
            stop = max(stop, entry * 1.05)
            continue

        if tp2_done:
            highest = max(highest, high)
            stop = max(stop, highest * 0.94)

        if index == exit_index and remaining > 0:
            gross += remaining * (close / entry - 1) * 100
            remaining = 0.0

    net = gross - ROUND_TRIP_COST_PCT
    return {
        "entry_price": entry,
        "entry_ts": int(frame.at[entry_index, "timestamp"]),
        "exit_ts": int(frame.at[exit_index, "timestamp"]),
        "net_pct": round(net, 6),
        "gross_pct": round(gross, 6),
        "mfe_pct": round(mfe, 6),
        "mae_pct": round(mae, 6),
        "tp1": tp1_done,
        "tp2": tp2_done,
    }


def _simulate_confirmed_trade(frame: pd.DataFrame, signal_index: int,
                              horizon_bars: int = 32) -> dict | None:
    """Buy only if price proves continuation above the signal candle high.

    The old pump path bought the next open unconditionally.  OOS failures often
    never printed another high.  A stop-entry valid for four 15m bars converts
    those cases into cancelled ideas instead of full-stop losses.
    """
    signal_high = float(frame.at[signal_index, "high"])
    signal_low = float(frame.at[signal_index, "low"])
    trigger = signal_high * 1.001
    entry_index = None
    for index in range(signal_index + 1, min(signal_index + 5, len(frame))):
        if float(frame.at[index, "high"]) >= trigger:
            entry_index = index
            break
    if entry_index is None:
        return None

    entry = trigger
    structural_stop_pct = (entry - signal_low) / entry if entry > signal_low else 0.03
    stop_pct = min(max(structural_stop_pct, 0.018), 0.030)
    stop = entry * (1 - stop_pct)
    tp1 = entry * 1.04
    tp2 = entry * 1.08
    signal_mid = (float(frame.at[signal_index, "open"]) + signal_high) / 2
    remaining = 1.0
    gross = 0.0
    tp1_done = False
    tp2_done = False
    highest = entry
    mfe = 0.0
    mae = 0.0
    exit_reason = "timeout"
    exit_index = min(entry_index + horizon_bars - 1, len(frame) - 1)

    for index in range(entry_index, exit_index + 1):
        high = float(frame.at[index, "high"])
        low = float(frame.at[index, "low"])
        close = float(frame.at[index, "close"])
        mfe = max(mfe, (high / entry - 1) * 100)
        mae = min(mae, (low / entry - 1) * 100)

        # Conservative same-bar assumption once the stop-entry has triggered.
        if low <= stop:
            gross += remaining * (stop / entry - 1) * 100
            remaining = 0.0
            exit_index = index
            exit_reason = "stop"
            break

        if not tp1_done and high >= tp1:
            gross += 0.45 * 4.0
            remaining -= 0.45
            tp1_done = True
            stop = max(stop, entry * 1.002)

        if tp1_done and not tp2_done and high >= tp2:
            gross += 0.30 * 8.0
            remaining -= 0.30
            tp2_done = True
            highest = max(highest, high)
            stop = max(stop, entry * 1.04)
            continue

        if tp2_done:
            highest = max(highest, high)
            stop = max(stop, highest * 0.96)

        bars_held = index - entry_index + 1
        if (
            not tp1_done
            and bars_held >= 4
            and mfe < 2.0
            and close < max(entry, signal_mid)
        ):
            gross += remaining * (close / entry - 1) * 100
            remaining = 0.0
            exit_index = index
            exit_reason = "no_follow_through"
            break

        if index == exit_index and remaining > 0:
            gross += remaining * (close / entry - 1) * 100
            remaining = 0.0

    net = gross - ROUND_TRIP_COST_PCT
    return {
        "entry_price": entry,
        "entry_ts": int(frame.at[entry_index, "timestamp"]),
        "exit_ts": int(frame.at[exit_index, "timestamp"]),
        "net_pct": round(net, 6),
        "gross_pct": round(gross, 6),
        "mfe_pct": round(mfe, 6),
        "mae_pct": round(mae, 6),
        "tp1": tp1_done,
        "tp2": tp2_done,
        "exit_reason": exit_reason,
        "confirmation_delay_bars": entry_index - signal_index,
        "initial_stop_pct": round(stop_pct * 100, 4),
    }


def _candidate_rows(frame: pd.DataFrame, symbol: str, study_start: int,
                    current_day: int, pump_days: set[int]) -> list[dict]:
    mask = (
        (frame["timestamp"] >= study_start)
        & (frame["timestamp"] < current_day)
        & frame["breakout"].fillna(False)
        & (frame["ret_bar_pct"] >= 1.0)
        & (frame["ret_1h_pct"] >= 1.5)
        & (frame["vol_ratio"] >= 1.5)
        & (frame["body_ratio"] >= 0.45)
        & (frame["vwap_disloc_pct"] >= 0)
        & (frame["vwap_disloc_pct"] <= 14)
        & (frame["day_gain_pct"] >= 2)
        & (frame["day_gain_pct"] <= 20)
        & (frame["qvol_24h"] >= MIN_ROLLING_24H_QVOL)
    )
    candidates = []
    for index in frame.index[mask.fillna(False)]:
        baseline = _simulate_trade(frame, int(index))
        sim = _simulate_confirmed_trade(frame, int(index))
        if not sim:
            continue
        row = frame.loc[index]
        candidate = {
            "symbol": symbol,
            "signal_ts": int(row["timestamp"]),
            "day_ms": int(row["day_ms"]),
            "pump_day": int(row["day_ms"]) in pump_days,
            "breakout": bool(row["breakout"]),
        }
        for name in FEATURE_NAMES:
            value = float(row[name])
            candidate[name] = value if math.isfinite(value) else None
        if baseline:
            candidate["baseline_net_pct"] = float(baseline["net_pct"])
        candidate.update(sim)
        candidates.append(candidate)
    return candidates


def _dedupe(rows: Iterable[dict], cooldown_ms: int = 12 * 60 * 60_000) -> list[dict]:
    selected = []
    last_by_symbol: dict[str, int] = {}
    for row in sorted(rows, key=lambda item: (item["signal_ts"], item["symbol"])):
        last = last_by_symbol.get(row["symbol"], -10**30)
        if int(row["signal_ts"]) - last < cooldown_ms:
            continue
        selected.append(row)
        last_by_symbol[row["symbol"]] = int(row["signal_ts"])
    return selected


def _metrics(rows: list[dict]) -> dict:
    pnl = [float(row["net_pct"]) for row in rows]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "n": len(rows),
        "win_rate_pct": round(len(wins) / len(rows) * 100, 3) if rows else 0.0,
        "avg_net_pct": round(statistics.mean(pnl), 4) if pnl else 0.0,
        "median_net_pct": round(statistics.median(pnl), 4) if pnl else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else 99.0,
        "sum_net_pct": round(sum(pnl), 4),
        "max_drawdown_pct_points": round(max_dd, 4),
        "tp1_rate_pct": round(sum(bool(row["tp1"]) for row in rows) / len(rows) * 100, 3) if rows else 0.0,
        "tp2_rate_pct": round(sum(bool(row["tp2"]) for row in rows) / len(rows) * 100, 3) if rows else 0.0,
        "pump_day_precision_pct": round(sum(bool(row["pump_day"]) for row in rows) / len(rows) * 100, 3) if rows else 0.0,
        "median_mfe_pct": round(statistics.median(float(row["mfe_pct"]) for row in rows), 4) if rows else 0.0,
        "median_mae_pct": round(statistics.median(float(row["mae_pct"]) for row in rows), 4) if rows else 0.0,
    }


def _rule_grid(candidates: list[dict], split_ms: int) -> list[dict]:
    grids = itertools.product(
        (1.5, 2.5, 3.5),       # 15m body gain
        (2.5, 4.0, 6.0),       # 1h momentum
        (1.5, 2.5, 4.0),       # volume ratio
        (0.55, 0.65),          # body / full range
        (6.0, 9.0),            # max VWAP extension
        (8.0, 12.0),           # max UTC-day gain at signal
        (1.10, 1.40),          # prior volatility compression ceiling
        (0.52, 0.54, 0.56),    # taker-buy share: observable buy aggression
    )
    results = []
    for values in grids:
        (
            ret_bar,
            ret_1h,
            vol_ratio,
            body,
            vwap_max,
            day_max,
            compression,
            taker_buy_min,
        ) = values
        matched = [
            row for row in candidates
            if row["ret_bar_pct"] >= ret_bar
            and row["ret_1h_pct"] >= ret_1h
            and row["vol_ratio"] >= vol_ratio
            and row["body_ratio"] >= body
            and row["vwap_disloc_pct"] <= vwap_max
            and row["day_gain_pct"] <= day_max
            and row["compression_ratio"] <= compression
            and row.get("taker_buy_ratio") is not None
            and row["taker_buy_ratio"] >= taker_buy_min
        ]
        train = _dedupe(row for row in matched if row["signal_ts"] < split_ms)
        test = _dedupe(row for row in matched if row["signal_ts"] >= split_ms)
        train_metrics = _metrics(train)
        # A spectacular dozen is exactly how the previous pump logic became
        # overfit.  Keep the search honest by requiring a usable train cohort.
        if train_metrics["n"] < 30:
            continue
        rule = {
            "ret_bar_min": ret_bar,
            "ret_1h_min": ret_1h,
            "vol_ratio_min": vol_ratio,
            "body_ratio_min": body,
            "vwap_disloc_max": vwap_max,
            "day_gain_max": day_max,
            "compression_max": compression,
            "taker_buy_ratio_min": taker_buy_min,
        }
        score = (
            train_metrics["avg_net_pct"] * math.sqrt(train_metrics["n"])
            + min(train_metrics["profit_factor"], 4.0) * 0.15
            - train_metrics["max_drawdown_pct_points"] * 0.02
        )
        results.append({
            "rule": rule,
            "train": train_metrics,
            "test": _metrics(test),
            "score": round(score, 6),
            "train_examples": train[-8:],
            "test_examples": test[-8:],
        })
    results.sort(key=lambda row: row["score"], reverse=True)
    return results


def _summarize_snapshots(rows: list[dict]) -> dict:
    summary = {"n": len(rows)}
    for feature in FEATURE_NAMES:
        values = [float(row[feature]) for row in rows if row.get(feature) is not None]
        if not values:
            continue
        summary[feature] = {
            "median": round(statistics.median(values), 4),
            "p25": round(float(np.percentile(values, 25)), 4),
            "p75": round(float(np.percentile(values, 75)), 4),
        }
    summary["prevalence"] = {
        "breakout_pct": round(sum(bool(row.get("breakout")) for row in rows) / len(rows) * 100, 2) if rows else 0,
        "vol_ratio_ge_3_pct": round(sum((row.get("vol_ratio") or 0) >= 3 for row in rows) / len(rows) * 100, 2) if rows else 0,
        "body_ratio_ge_06_pct": round(sum((row.get("body_ratio") or 0) >= 0.6 for row in rows) / len(rows) * 100, 2) if rows else 0,
        "compression_le_1_pct": round(sum((row.get("compression_ratio") or 99) <= 1 for row in rows) / len(rows) * 100, 2) if rows else 0,
        "vwap_disloc_le_8_pct": round(sum(0 <= (row.get("vwap_disloc_pct") or -99) <= 8 for row in rows) / len(rows) * 100, 2) if rows else 0,
    }
    return summary


def _markdown_report(result: dict) -> str:
    best = result.get("best_rule") or {}
    lines = [
        "# Binance 30% Pump Study",
        "",
        f"- Study UTC: {result['study']['start_utc']} ~ {result['study']['end_utc_exclusive']}",
        f"- Active swaps: {result['universe']['active_symbols']}",
        f"- Liquid study universe: {result['universe']['liquid_symbols']}",
        f"- 30% intraday events: {result['events']['total']} (eligible {result['events']['eligible']}, new/illiquid {result['events']['excluded']})",
        "",
        "## Observable pattern",
        "",
        "```json",
        json.dumps(result.get("snapshot_summary", {}), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Best train-selected rule",
        "",
        "```json",
        json.dumps(best, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Decision",
        "",
        result.get("decision", ""),
        "",
        "## Eligible events",
        "",
        "| UTC day | Symbol | High/Open | Close/Open | Prior 7d quote volume |",
        "|---|---:|---:|---:|---:|",
    ]
    for event in result.get("eligible_events", []):
        lines.append(
            f"| {event['date_utc']} | {event['symbol']} | "
            f"{event['high_open_pct']:+.1f}% | {event['close_open_pct']:+.1f}% | "
            f"${event['prior_7d_median_qvol']:,.0f} |"
        )
    return "\n".join(lines) + "\n"


def run(args) -> dict:
    now_ms = int(time.time() * 1000)
    current_day = _utc_day(now_ms)
    study_start = current_day - STUDY_DAYS * DAY_MS
    fetch_start = study_start - WARMUP_DAYS * DAY_MS
    split_ms = study_start + TRAIN_DAYS * DAY_MS
    cache_root = Path(args.cache_dir).expanduser().resolve()
    exchange = ccxt.binanceusdm({"enableRateLimit": True})
    symbols = _active_symbols(exchange)
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]

    daily_by_symbol: dict[str, list[list[float]]] = {}
    daily_failures = []
    print(f"[daily] active symbols={len(symbols)}", flush=True)
    for index, symbol in enumerate(symbols, start=1):
        try:
            daily_by_symbol[symbol] = _cached_range(
                exchange,
                cache_root / "daily" / f"{_safe_symbol(symbol)}.json.gz",
                symbol,
                "1d",
                study_start - 30 * DAY_MS,
                current_day,
            )
        except Exception as exc:
            daily_failures.append({"symbol": symbol, "error": str(exc)[:160]})
        if index % 50 == 0:
            print(f"[daily] {index}/{len(symbols)}", flush=True)

    events, liquid_symbols = _daily_events(daily_by_symbol, study_start, current_day)
    eligible_events = [event for event in events if event["eligible"]]
    event_symbols = {event["symbol"] for event in eligible_events}
    study_symbols = sorted(liquid_symbols | event_symbols)
    if args.max_study_symbols:
        study_symbols = study_symbols[: args.max_study_symbols]
    print(
        f"[events] total={len(events)} eligible={len(eligible_events)} "
        f"study_symbols={len(study_symbols)}",
        flush=True,
    )

    events_by_symbol: dict[str, list[dict]] = defaultdict(list)
    for event in eligible_events:
        events_by_symbol[event["symbol"]].append(event)

    pre_snapshots = []
    onset_snapshots = []
    control_snapshots = []
    candidates = []
    intraday_failures = []

    for number, symbol in enumerate(study_symbols, start=1):
        try:
            rows = _cached_range(
                exchange,
                cache_root / "m15_taker_v2" / f"{_safe_symbol(symbol)}.json.gz",
                symbol,
                "15m",
                fetch_start,
                current_day,
            )
            frame = _feature_frame(rows)
            if frame.empty:
                continue
            funding_rows = _cached_funding(
                exchange,
                cache_root / "funding" / f"{_safe_symbol(symbol)}.json.gz",
                symbol,
                fetch_start,
                current_day,
            )
            frame = _attach_funding(frame, funding_rows)
            pump_days = {int(event["day_ms"]) for event in events_by_symbol.get(symbol, [])}
            candidates.extend(
                _candidate_rows(frame, symbol, study_start, current_day, pump_days)
            )
            for event in events_by_symbol.get(symbol, []):
                day_rows = frame.index[frame["day_ms"] == int(event["day_ms"])].tolist()
                if not day_rows:
                    continue
                day_open = float(frame.at[day_rows[0], "open"])
                onset = next(
                    (
                        idx for idx in day_rows
                        if float(frame.at[idx, "close"]) >= day_open * 1.05
                    ),
                    None,
                )
                if onset is None:
                    onset = next(
                        (
                            idx for idx in day_rows
                            if float(frame.at[idx, "high"]) >= day_open * 1.05
                        ),
                        None,
                    )
                if onset is None:
                    continue
                onset_snapshots.append(_snapshot(frame.loc[onset], symbol, "onset"))
                if onset > 0:
                    pre_snapshots.append(_snapshot(frame.loc[onset - 1], symbol, "pre"))

                offset = int((int(frame.at[onset, "timestamp"]) - int(event["day_ms"])) // BAR_MS)
                controls_added = 0
                for back in range(1, 8):
                    control_day = int(event["day_ms"]) - back * DAY_MS
                    if control_day in pump_days:
                        continue
                    same_day = frame.index[frame["day_ms"] == control_day].tolist()
                    if offset >= len(same_day):
                        continue
                    control_snapshots.append(
                        _snapshot(frame.loc[same_day[offset]], symbol, "control")
                    )
                    controls_added += 1
                    if controls_added >= 3:
                        break
        except Exception as exc:
            intraday_failures.append({"symbol": symbol, "error": str(exc)[:160]})
        if number % 25 == 0:
            print(
                f"[15m] {number}/{len(study_symbols)} candidates={len(candidates)}",
                flush=True,
            )

    grid = _rule_grid(candidates, split_ms)
    best = grid[0] if grid else {}
    if best:
        rule = best["rule"]
        best_matched = [
            row for row in candidates
            if row["ret_bar_pct"] >= rule["ret_bar_min"]
            and row["ret_1h_pct"] >= rule["ret_1h_min"]
            and row["vol_ratio"] >= rule["vol_ratio_min"]
            and row["body_ratio"] >= rule["body_ratio_min"]
            and row["vwap_disloc_pct"] <= rule["vwap_disloc_max"]
            and row["day_gain_pct"] <= rule["day_gain_max"]
            and row["compression_ratio"] <= rule["compression_max"]
            and row.get("taker_buy_ratio") is not None
            and row["taker_buy_ratio"] >= rule["taker_buy_ratio_min"]
        ]
        # Keep the complete selected cohort in the machine-readable artifact so
        # additional public features (OI, positioning) can be tested against
        # every train/OOS trade instead of only the last eight display examples.
        best["train_all"] = _dedupe(
            row for row in best_matched if row["signal_ts"] < split_ms
        )
        best["test_all"] = _dedupe(
            row for row in best_matched if row["signal_ts"] >= split_ms
        )
    test_metrics = best.get("test", {})
    if (
        test_metrics.get("n", 0) >= 8
        and test_metrics.get("avg_net_pct", 0) > 0
        and test_metrics.get("profit_factor", 0) >= 1.20
    ):
        decision = (
            "OOS 통과: 신규 로직은 초소액 canary 후보. 단, 30일 단일 구간이므로 "
            "계좌위험 0.15% 이하와 독립 30건 승격 조건을 유지한다."
        )
    else:
        decision = (
            "OOS 미통과 또는 표본 부족: 탐지/후보로그만 적용하고 실주문은 paper-only로 둔다. "
            "급등 사례만 보고 라이브로 켜면 선택편향이 된다."
        )

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "study": {
            "start_ms": study_start,
            "split_ms": split_ms,
            "end_ms_exclusive": current_day,
            "start_utc": _iso_day(study_start),
            "split_utc": _iso_day(split_ms),
            "end_utc_exclusive": _iso_day(current_day),
            "pump_definition": "completed UTC daily high/open >= +30%",
            "entry_assumption": "signal bar close, fill at next 15m open",
            "exit_model": (
                "signal-high stop-entry valid 1h; 1.8~3.0% structural SL; "
                "45%@+4%; 30%@+8%; 25% 4% trail; no-follow-through exit; 8h timeout"
            ),
            "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
        },
        "universe": {
            "active_symbols": len(symbols),
            "daily_downloaded": len(daily_by_symbol),
            "liquid_symbols": len(study_symbols),
            "daily_failures": daily_failures,
            "intraday_failures": intraday_failures,
        },
        "events": {
            "total": len(events),
            "eligible": len(eligible_events),
            "excluded": len(events) - len(eligible_events),
            "close_up_30": sum(event["close_open_pct"] >= 30 for event in events),
        },
        "eligible_events": eligible_events,
        "excluded_events": [event for event in events if not event["eligible"]],
        "snapshot_summary": {
            "pre_pump": _summarize_snapshots(pre_snapshots),
            "onset": _summarize_snapshots(onset_snapshots),
            "matched_nonpump_control": _summarize_snapshots(control_snapshots),
        },
        "candidate_count": len(candidates),
        "grid_rules_tested": len(grid),
        "best_rule": best,
        "top_rules": grid[:10],
        "decision": decision,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cache-dir",
        default="/tmp/got_binance_pump_study",
        help="Public market-data cache directory",
    )
    parser.add_argument(
        "--output-json",
        default="binance_pump_study_latest.json",
    )
    parser.add_argument(
        "--output-md",
        default="BINANCE_PUMP_STUDY_LATEST.md",
    )
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--max-study-symbols", type=int, default=0)
    args = parser.parse_args()
    result = run(args)
    json_path = Path(args.output_json).expanduser().resolve()
    md_path = Path(args.output_md).expanduser().resolve()
    _json_dump(json_path, result)
    md_path.write_text(_markdown_report(result), encoding="utf-8")
    print(f"[done] {json_path}", flush=True)
    print(f"[done] {md_path}", flush=True)
    print(json.dumps({
        "events": result["events"],
        "universe": {
            "active_symbols": result["universe"]["active_symbols"],
            "liquid_symbols": result["universe"]["liquid_symbols"],
        },
        "candidate_count": result["candidate_count"],
        "best_rule": result.get("best_rule", {}),
        "decision": result["decision"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
