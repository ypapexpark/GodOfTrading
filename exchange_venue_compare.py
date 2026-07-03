#!/usr/bin/env python3
from __future__ import annotations

"""
Read-only Bybit vs Binance USD-M futures venue comparison.

Fetches public order books and estimates spread/slippage for the same symbols.
No API keys or order placement are used.

Examples:
  python3 exchange_venue_compare.py
  python3 exchange_venue_compare.py --order-usd 5000 --symbols BTC/USDT ETH/USDT SOL/USDT
"""

import argparse
import json
import sys
import warnings
from datetime import datetime, timezone
from typing import Any

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)

import ccxt


DEFAULT_SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT")


def _venue_clients() -> dict[str, Any]:
    return {
        "bybit": ccxt.bybit({"options": {"defaultType": "future"}, "enableRateLimit": True}),
        "binance": ccxt.binanceusdm({"enableRateLimit": True}),
    }


def _normalize_symbol(exchange: Any, symbol: str) -> str:
    exchange.load_markets()
    if symbol in exchange.markets:
        return symbol
    fut = f"{symbol}:USDT" if ":" not in symbol else symbol
    if fut in exchange.markets:
        return fut
    compact = symbol.replace("/", "")
    for market_symbol, market in exchange.markets.items():
        if market.get("id") == compact or market_symbol.split(":")[0] == symbol:
            return market_symbol
    raise ValueError(f"{exchange.id} market not found: {symbol}")


def _estimate_buy(order_book: dict[str, Any], order_usd: float) -> tuple[float | None, float]:
    remaining = order_usd
    base = 0.0
    spent = 0.0
    for price, amount in order_book.get("asks", []):
        price = float(price)
        amount = float(amount)
        capacity = price * amount
        take = min(remaining, capacity)
        if take <= 0:
            continue
        base += take / price
        spent += take
        remaining -= take
        if remaining <= 1e-9:
            break
    return (spent / base if base > 0 else None), max(remaining, 0.0)


def _estimate_sell(order_book: dict[str, Any], order_usd: float) -> tuple[float | None, float]:
    bids = order_book.get("bids", [])
    best_bid = float(bids[0][0]) if bids else 0.0
    if best_bid <= 0:
        return None, order_usd
    target_base = order_usd / best_bid
    remaining_base = target_base
    received = 0.0
    sold = 0.0
    for price, amount in bids:
        price = float(price)
        amount = float(amount)
        take_base = min(remaining_base, amount)
        if take_base <= 0:
            continue
        received += take_base * price
        sold += take_base
        remaining_base -= take_base
        if remaining_base <= 1e-12:
            break
    unfilled_usd = remaining_base * best_bid if remaining_base > 0 else 0.0
    return (received / sold if sold > 0 else None), unfilled_usd


def inspect_venue(exchange: Any, symbol: str, order_usd: float) -> dict[str, Any]:
    market_symbol = _normalize_symbol(exchange, symbol)
    ticker = exchange.fetch_ticker(market_symbol)
    book = exchange.fetch_order_book(market_symbol, limit=50)
    bid = float(ticker.get("bid") or (book["bids"][0][0] if book.get("bids") else 0) or 0)
    ask = float(ticker.get("ask") or (book["asks"][0][0] if book.get("asks") else 0) or 0)
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
    spread_pct = (ask - bid) / mid * 100 if mid > 0 and ask >= bid else None
    buy_avg, buy_unfilled = _estimate_buy(book, order_usd)
    sell_avg, sell_unfilled = _estimate_sell(book, order_usd)
    buy_slip_pct = (buy_avg - ask) / ask * 100 if buy_avg and ask > 0 else None
    sell_slip_pct = (bid - sell_avg) / bid * 100 if sell_avg and bid > 0 else None
    return {
        "venue": exchange.id,
        "symbol": symbol,
        "market_symbol": market_symbol,
        "bid": bid,
        "ask": ask,
        "spread_pct": spread_pct,
        "quote_volume": ticker.get("quoteVolume"),
        "order_usd": order_usd,
        "buy_avg": buy_avg,
        "buy_slippage_pct": buy_slip_pct,
        "buy_unfilled_usd": buy_unfilled,
        "sell_avg": sell_avg,
        "sell_slippage_pct": sell_slip_pct,
        "sell_unfilled_usd": sell_unfilled,
    }


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):,.{digits}f}"
    except Exception:
        return str(value)


def print_table(rows: list[dict[str, Any]]) -> None:
    print(f"Bybit vs Binance public futures venue comparison ({datetime.now(timezone.utc).isoformat()})")
    print("symbol venue    bid        ask        spr%     buyAvg     buySlip%  sellAvg    sellSlip% quoteVol")
    for row in rows:
        print(
            f"{row['symbol']:<8} {row['venue']:<8} "
            f"{_fmt(row['bid'], 4):>10} {_fmt(row['ask'], 4):>10} "
            f"{_fmt(row['spread_pct'], 4):>8} "
            f"{_fmt(row['buy_avg'], 4):>10} {_fmt(row['buy_slippage_pct'], 4):>9} "
            f"{_fmt(row['sell_avg'], 4):>10} {_fmt(row['sell_slippage_pct'], 4):>9} "
            f"{_fmt(row.get('quote_volume'), 0):>10}"
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--order-usd", type=float, default=1000.0)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    clients = _venue_clients()
    rows: list[dict[str, Any]] = []
    for symbol in args.symbols:
        for exchange in clients.values():
            try:
                rows.append(inspect_venue(exchange, symbol, args.order_usd))
            except Exception as exc:
                rows.append({
                    "venue": getattr(exchange, "id", "unknown"),
                    "symbol": symbol,
                    "error": str(exc),
                })
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print_table([row for row in rows if not row.get("error")])
        errors = [row for row in rows if row.get("error")]
        if errors:
            print("")
            print("Errors:")
            for row in errors:
                print(f"{row['venue']} {row['symbol']}: {row['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
