#!/usr/bin/env python3
"""Continuously maintain an incremental OHLCV cache for all Binance perps."""
from __future__ import annotations

import os
import signal
import sys
import threading
import time

os.environ.setdefault("AUTO_TRADE_EXCHANGE", "binance")
os.environ.setdefault("GOT_MARKET_DATA_EXCHANGE", "binance")
os.environ.setdefault("GOT_STATE_NAMESPACE", "binance")

from fetcher import fetch_all_usdt_perpetual_markets, fetch_ohlcv_batch
from binance_api_guard import api_backoff_remaining
from process_lock import try_acquire
from service_status import write_status

SERVICE_NAME = "binance_market_collector"
LOOP_SECONDS = max(
    5.0, float(os.getenv("BINANCE_MARKET_COLLECTOR_LOOP_SECONDS", "10") or 10)
)
UNIVERSE_REFRESH_SECONDS = max(
    60.0,
    float(os.getenv("BINANCE_UNIVERSE_REFRESH_SECONDS", "300") or 300),
)
MAX_WORKERS = max(
    1, min(int(os.getenv("BINANCE_MARKET_COLLECTOR_WORKERS", "8") or 8), 16)
)
# Priority is deliberate: at a busy hourly boundary, the completed 5m trigger
# reaches the scanner first instead of waiting behind 1h backfills.
TIMEFRAME_LIMITS = (
    ("5m", 90),
    ("15m", 140),
    ("1h", 1000),
    ("1d", 90),
    ("1w", 70),
)
_stop = threading.Event()


def _request_stop(*_args) -> None:
    _stop.set()


def run() -> int:
    if not try_acquire(SERVICE_NAME):
        return 0
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    universe: list[dict] = []
    last_universe_refresh = 0.0
    cycles = 0
    while not _stop.is_set():
        cycle_started = time.time()
        total_errors = 0
        fresh_frames = 0
        try:
            shared_backoff = api_backoff_remaining()
            if shared_backoff > 0:
                write_status(
                    SERVICE_NAME,
                    {
                        "ok": False,
                        "phase": "api-backoff",
                        "cycles": cycles,
                        "backoff_seconds": round(shared_backoff, 1),
                    },
                )
                _stop.wait(min(shared_backoff, 30.0))
                continue
            if (
                not universe
                or cycle_started - last_universe_refresh >= UNIVERSE_REFRESH_SECONDS
            ):
                universe = fetch_all_usdt_perpetual_markets(cache_seconds=0)
                last_universe_refresh = time.time()
            symbols = [str(row.get("symbol") or "") for row in universe]
            symbols = [symbol for symbol in symbols if symbol]
            write_status(
                SERVICE_NAME,
                {
                    "ok": True,
                    "phase": "collecting",
                    "symbols": len(symbols),
                    "cycles": cycles,
                    "timeframe": "starting",
                },
            )
            for timeframe, limit in TIMEFRAME_LIMITS:
                if _stop.is_set():
                    break
                frames, errors = fetch_ohlcv_batch(
                    [(symbol, timeframe, limit) for symbol in symbols],
                    max_workers=MAX_WORKERS,
                )
                fresh_frames += len(frames)
                total_errors += len(errors)
                stale = sum(bool(frame.attrs.get("stale")) for frame in frames.values())
                write_status(
                    SERVICE_NAME,
                    {
                        "ok": True,
                        "phase": "collecting",
                        "symbols": len(symbols),
                        "cycles": cycles,
                        "timeframe": timeframe,
                        "frames": fresh_frames,
                        "errors": total_errors,
                        "stale": stale,
                        "cycle_seconds": round(time.time() - cycle_started, 3),
                    },
                )
            cycles += 1
            write_status(
                SERVICE_NAME,
                {
                    "ok": True,
                    "phase": "idle",
                    "symbols": len(symbols),
                    "cycles": cycles,
                    "frames": fresh_frames,
                    "errors": total_errors,
                    "cycle_seconds": round(time.time() - cycle_started, 3),
                    "loop_seconds": LOOP_SECONDS,
                },
            )
        except Exception as exc:
            write_status(
                SERVICE_NAME,
                {
                    "ok": False,
                    "phase": "error",
                    "cycles": cycles,
                    "error": str(exc)[:300],
                },
            )
            print(f"[Binance market collector] cycle error: {exc}")
        _stop.wait(max(0.1, LOOP_SECONDS - (time.time() - cycle_started)))
    write_status(SERVICE_NAME, {"ok": False, "stopped": True, "cycles": cycles})
    return 0


if __name__ == "__main__":
    sys.exit(run())
