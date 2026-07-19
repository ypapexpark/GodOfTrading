#!/usr/bin/env python3
"""Read-only Binance D2 full-universe signal probe (never places orders)."""
from __future__ import annotations

from collections import Counter
import os
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ["AUTO_TRADE_EXCHANGE"] = "binance"

from binance_divergence_engine import (  # noqa: E402
    detect_divergence_setup,
    evaluate_divergence_entry,
    resample_ohlcv,
    select_multitimeframe_setup,
)
from config import (  # noqa: E402
    BINANCE_D2_MIN_24H_VOLUME_USD,
    BINANCE_ROUND_TRIP_EXECUTION_COST,
)
from fetcher import fetch_all_usdt_perpetual_markets, fetch_ohlcv  # noqa: E402


def main() -> int:
    started = time.time()
    rows = fetch_all_usdt_perpetual_markets()
    tiers: Counter[str] = Counter()
    rejections: Counter[str] = Counter()
    triggers: list[str] = []
    errors = 0
    print(f"D2 read-only probe: universe={len(rows)}")
    for index, row in enumerate(rows, start=1):
        symbol = str(row.get("symbol") or "")
        try:
            d15 = fetch_ohlcv(symbol, "15m", 140)
            d1h = fetch_ohlcv(symbol, "1h", 380)
            d4h = resample_ohlcv(d1h, "4h")
            setup, tier = select_multitimeframe_setup(
                {
                    "15m": detect_divergence_setup(d15, timeframe="15m"),
                    "1h": detect_divergence_setup(d1h, timeframe="1h"),
                    "4h": detect_divergence_setup(d4h, timeframe="4h"),
                }
            )
            if setup is None:
                continue
            tiers[tier] += 1
            d5 = fetch_ohlcv(symbol, "5m", 90)
            plan = evaluate_divergence_entry(
                d15,
                d5,
                setup=setup,
                signal_tier=tier,
                higher_frames={
                    "1h": d1h,
                    "4h": d4h,
                    "1d": fetch_ohlcv(symbol, "1d", 90),
                    "1w": fetch_ohlcv(symbol, "1w", 70),
                },
                live_price=float(row.get("last") or d5["close"].iloc[-1]),
                round_trip_cost=BINANCE_ROUND_TRIP_EXECUTION_COST,
                spread_pct=float(row.get("spread_pct") or 0.0) or None,
                quote_volume_usd=float(row.get("volume_usd") or 0.0),
                min_quote_volume_usd=BINANCE_D2_MIN_24H_VOLUME_USD,
            )
            if plan.eligible:
                label = (
                    f"{symbol} {plan.direction} {tier}/{setup.timeframe} "
                    f"{setup.kind} {setup.vote_count}/4 RVOL={plan.volume_ratio_5m:.2f}x"
                )
                triggers.append(label)
                print(f"TRIGGER {label}")
            else:
                rejections[plan.reason.split(" (")[0]] += 1
        except Exception as exc:
            errors += 1
            rejections[f"ERROR:{type(exc).__name__}"] += 1
        if index % 100 == 0:
            print(
                f"progress={index}/{len(rows)} tiers={dict(tiers)} "
                f"triggers={len(triggers)} errors={errors}"
            )
    print(
        f"DONE seconds={time.time()-started:.1f} tiers={dict(tiers)} "
        f"triggers={len(triggers)} errors={errors}"
    )
    print(f"TOP_REJECTIONS {rejections.most_common(12)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
