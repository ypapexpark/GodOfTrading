#!/usr/bin/env python3
"""Cost-aware walk-forward check for the v6 15m EMA entry family.

This is intentionally a small, auditable research harness rather than a full
optimizer.  It compares the loose EMA trigger, the currently rare confluence
trigger, and the proposed strict canary on the same candles.  Signals use only
information available at the close of the signal bar; fills occur at the next
bar open.  If stop and target are both touched in one bar, the stop is assumed
to happen first.
"""
from __future__ import annotations

import argparse
import math
import time
from dataclasses import dataclass

import ccxt
import numpy as np
import pandas as pd


SYMBOLS = (
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT",
    "ADA/USDT", "LINK/USDT", "SUI/USDT", "AVAX/USDT", "BNB/USDT",
)
TF_MS = 15 * 60 * 1000


@dataclass
class Trade:
    venue: str
    symbol: str
    model: str
    signal_time: pd.Timestamp
    exit_time: pd.Timestamp
    r_net: float


def exchange_for(venue: str):
    if venue == "binance":
        return ccxt.binanceusdm({"enableRateLimit": True})
    return ccxt.bybit({
        "enableRateLimit": True,
        "options": {"defaultType": "linear"},
    })


def fetch_bars(ex, symbol: str, days: int) -> pd.DataFrame:
    ex.load_markets()
    candidates = (symbol, f"{symbol}:USDT")
    market_symbol = next((s for s in candidates if s in ex.markets), None)
    if market_symbol is None:
        raise ValueError("linear USDT market not found")

    since = ex.milliseconds() - days * 24 * 60 * 60 * 1000
    rows: list[list] = []
    while since < ex.milliseconds() - TF_MS:
        batch = ex.fetch_ohlcv(market_symbol, "15m", since=since, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        next_since = int(batch[-1][0]) + TF_MS
        if next_since <= since:
            break
        since = next_since
        time.sleep(max(float(getattr(ex, "rateLimit", 50)) / 1000, 0.05))

    if not rows:
        raise ValueError("no OHLCV")
    frame = pd.DataFrame(
        rows, columns=("timestamp", "open", "high", "low", "close", "volume")
    ).drop_duplicates("timestamp")
    frame["time"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    frame = frame.set_index("time").sort_index()
    return frame.iloc[:-1].astype(float)  # current exchange candle may be unfinished


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = out["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr"] = tr.rolling(14).mean()
    out["ema20"] = close.ewm(span=20, adjust=False).mean()
    out["ema50"] = close.ewm(span=50, adjust=False).mean()
    out["ema50_slope"] = out["ema50"].pct_change(4)
    out["vol_ratio"] = out["volume"] / out["volume"].shift(1).rolling(20).mean()

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    out["macd_ok"] = (hist >= 0) | (hist > hist.shift(1))

    out["recent_low"] = out["low"].shift(1).rolling(5).min()
    out["prev_low"] = out["low"].shift(1)
    out["bull_body"] = (out["close"] - out["open"]) >= out["atr"] * 0.18
    out["base"] = (
        (out["ema20"] > out["ema50"] * 1.001)
        & (out["recent_low"] <= out["ema20"] + out["atr"] * 0.8)
        & (out["close"] >= out["ema20"])
        & out["bull_body"]
        & (out["vol_ratio"] >= 1.0)
    )

    resistance = out["high"].shift(3).rolling(12).max()
    bull_count = (out["close"] > out["open"]).shift(1).rolling(3).sum()
    micro = (
        (out["close"] > resistance)
        & (out["close"].shift(1) <= resistance)
        & (bull_count >= 2)
        & (out["vol_ratio"] >= 1.15)
    )
    volume_break = (
        (out["close"] >= out["high"].shift(1).rolling(20).max())
        & (bull_count >= 2)
        & (out["vol_ratio"] >= 2.5)
        & ((out["close"] - out["open"]) >= out["atr"] * 0.55)
    )
    out["combo"] = out["base"] & (micro | volume_break)

    stop = out["prev_low"] - 1.5 * out["atr"]
    out["risk"] = out["close"] - stop
    out["risk_atr"] = out["risk"] / out["atr"]
    out["risk_pct"] = out["risk"] / out["close"]
    out["strict"] = (
        out["base"]
        & out["macd_ok"]
        & (out["ema50_slope"] > 0)
        & out["vol_ratio"].between(1.10, 4.0)
        & out["risk_atr"].between(0.35, 3.0)
        & (out["risk_pct"] <= 0.05)
    )
    return out.replace([np.inf, -np.inf], np.nan)


def simulate(
    venue: str,
    symbol: str,
    df: pd.DataFrame,
    model: str,
    cost_pct: float,
    horizon: int = 24,
) -> list[Trade]:
    trades: list[Trade] = []
    next_free = 0
    signal = df[model].fillna(False).to_numpy(dtype=bool)
    for i in range(80, len(df) - horizon - 1):
        if i < next_free or not signal[i]:
            continue
        entry_i = i + 1
        entry = float(df["open"].iloc[entry_i])
        atr = float(df["atr"].iloc[i])
        pivot = float(df["prev_low"].iloc[i])
        stop = pivot - 1.5 * atr
        risk = entry - stop
        if not math.isfinite(risk) or risk <= 0:
            continue
        if risk / atr > 3.0 or risk / entry > 0.05:
            continue
        target = entry + 1.5 * risk
        exit_i = entry_i + horizon
        gross_r = None
        for j in range(entry_i, min(entry_i + horizon, len(df) - 1) + 1):
            lo = float(df["low"].iloc[j])
            hi = float(df["high"].iloc[j])
            if lo <= stop:  # conservative ordering on ambiguous bars
                gross_r = -1.0
                exit_i = j
                break
            if hi >= target:
                gross_r = 1.5
                exit_i = j
                break
        if gross_r is None:
            exit_price = float(df["close"].iloc[exit_i])
            gross_r = (exit_price - entry) / risk
        cost_r = (entry * cost_pct) / risk
        trades.append(
            Trade(
                venue=venue,
                symbol=symbol,
                model=model,
                signal_time=df.index[i],
                exit_time=df.index[exit_i],
                r_net=float(gross_r - cost_r),
            )
        )
        next_free = exit_i + 1
    return trades


def metrics(trades: list[Trade]) -> dict:
    if not trades:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "mean_r": 0.0, "max_dd_r": 0.0}
    values = np.array([t.r_net for t in trades], dtype=float)
    wins = values[values > 0]
    losses = values[values <= 0]
    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())
    curve = values.cumsum()
    peak = np.maximum.accumulate(np.r_[0.0, curve])
    dd = peak[1:] - curve
    return {
        "n": int(len(values)),
        "wr": float((values > 0).mean()),
        "pf": gross_win / gross_loss if gross_loss > 0 else float("inf"),
        "mean_r": float(values.mean()),
        "max_dd_r": float(dd.max(initial=0.0)),
    }


def fmt(name: str, rows: list[Trade]) -> str:
    m = metrics(rows)
    pf = "inf" if math.isinf(m["pf"]) else f"{m['pf']:.2f}"
    return (
        f"{name:16s} n={m['n']:4d} WR={m['wr']*100:5.1f}% "
        f"PF={pf:>5s} meanR={m['mean_r']:+.3f} maxDD={m['max_dd_r']:.1f}R"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=45)
    parser.add_argument("--symbols", type=int, default=10)
    parser.add_argument(
        "--venues", nargs="+", choices=("bybit", "binance"),
        default=("bybit", "binance"),
    )
    args = parser.parse_args()
    all_trades: list[Trade] = []
    for venue in args.venues:
        ex = exchange_for(venue)
        cost_pct = 0.0017 if venue == "bybit" else 0.0016
        venue_trades: list[Trade] = []
        for symbol in SYMBOLS[: max(1, min(args.symbols, len(SYMBOLS)))]:
            try:
                df = enrich(fetch_bars(ex, symbol, args.days))
                for model in ("base", "combo", "strict"):
                    venue_trades.extend(simulate(venue, symbol, df, model, cost_pct))
                print(f"[{venue}] {symbol}: {len(df)} closed bars")
            except Exception as exc:
                print(f"[{venue}] {symbol}: SKIP {exc}")
        all_trades.extend(venue_trades)
        print(f"\n{venue.upper()} full sample")
        for model in ("base", "combo", "strict"):
            print(fmt(model, [t for t in venue_trades if t.model == model]))

        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=max(7, int(args.days * 0.30)))
        print(f"{venue.upper()} out-of-sample since {cutoff.date()}")
        for model in ("base", "combo", "strict"):
            subset = [
                t for t in venue_trades
                if t.model == model and t.signal_time >= cutoff
            ]
            print(fmt(model, subset))
        print()
    return 0 if all_trades else 2


if __name__ == "__main__":
    raise SystemExit(main())
