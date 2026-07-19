#!/usr/bin/env python3
"""Read-only full-universe probe for the D3 4h MA200 pullback strategy."""
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

from binance_divergence_engine import _atr, _closed_bars, resample_ohlcv  # noqa: E402
from binance_ma200_pullback_engine import (  # noqa: E402
    detect_ma200_volume_breakout,
    evaluate_ma200_pullback_entry,
)
from config import (  # noqa: E402
    BINANCE_MA200_PULLBACK_MIN_24H_VOLUME_USD,
    BINANCE_ROUND_TRIP_EXECUTION_COST,
)
from fetcher import fetch_all_usdt_perpetual_markets, fetch_ohlcv  # noqa: E402


def _cross_diagnostics(d4h):
    bars = _closed_bars(d4h, "4h")
    close = bars["close"].astype(float)
    ma = close.rolling(200).mean()
    atr = _atr(bars)
    vol_base = bars["volume"].astype(float).shift(1).rolling(30).median()
    result = []
    ema = close.ewm(span=200, adjust=False).mean()
    for pos in range(max(200, len(bars) - 60), len(bars)):
        if close.iloc[pos - 1] <= ma.iloc[pos - 1] and close.iloc[pos] > ma.iloc[pos]:
            row = bars.iloc[pos]
            candle_range = max(float(row["high"] - row["low"]), 1e-12)
            result.append(
                {
                    "bar": str(bars.index[pos]),
                    "bars_ago": len(bars) - 1 - pos,
                    "below_ratio_12": round(
                        float((close.iloc[pos - 12:pos] < ma.iloc[pos - 12:pos]).mean()), 3
                    ),
                    "volume_ratio": round(float(row["volume"]) / float(vol_base.iloc[pos]), 3),
                    "body_atr": round(float(row["close"] - row["open"]) / float(atr.iloc[pos]), 3),
                    "close_location": round(
                        float(row["close"] - row["low"]) / candle_range, 3
                    ),
                    "extension_atr": round(
                        float(row["close"] - ma.iloc[pos]) / float(atr.iloc[pos]), 3
                    ),
                }
            )
        elif close.iloc[pos - 1] <= ema.iloc[pos - 1] and close.iloc[pos] > ema.iloc[pos]:
            result.append(
                {
                    "bar": str(bars.index[pos]),
                    "bars_ago": len(bars) - 1 - pos,
                    "average": "EMA200",
                    "volume_ratio": round(float(bars["volume"].iloc[pos]) / float(vol_base.iloc[pos]), 3),
                    "close": float(close.iloc[pos]),
                    "ema200": float(ema.iloc[pos]),
                }
            )
    return result


def main() -> int:
    started = time.time()
    rows = fetch_all_usdt_perpetual_markets()
    symbol_filter = sys.argv[1].upper() if len(sys.argv) > 1 else ""
    if symbol_filter:
        rows = [row for row in rows if str(row.get("symbol") or "").upper() == symbol_filter]
    setups = 0
    triggers: list[str] = []
    rejections: Counter[str] = Counter()
    errors = 0
    xec_result = "1000XEC/USDT not evaluated"
    print(f"D3 read-only probe: universe={len(rows)}")
    for index, row in enumerate(rows, start=1):
        symbol = str(row.get("symbol") or "")
        try:
            d1h = fetch_ohlcv(symbol, "1h", 1000)
            d4h = resample_ohlcv(d1h, "4h")
            setup = detect_ma200_volume_breakout(d4h)
            if not setup.eligible:
                if symbol == "1000XEC/USDT":
                    xec_result = setup.reason
                if symbol_filter:
                    print(f"CROSS_DIAGNOSTICS {_cross_diagnostics(d4h)}")
                continue
            setups += 1
            d15 = fetch_ohlcv(symbol, "15m", 140)
            d5 = fetch_ohlcv(symbol, "5m", 90)
            plan = evaluate_ma200_pullback_entry(
                d4h,
                d15,
                d5,
                setup=setup,
                live_price=float(row.get("last") or d5["close"].iloc[-1]),
                round_trip_cost=BINANCE_ROUND_TRIP_EXECUTION_COST,
                spread_pct=float(row.get("spread_pct") or 0.0) or None,
                quote_volume_usd=float(row.get("volume_usd") or 0.0),
                min_quote_volume_usd=BINANCE_MA200_PULLBACK_MIN_24H_VOLUME_USD,
            )
            if symbol == "1000XEC/USDT":
                xec_result = plan.reason
            if plan.eligible:
                label = (
                    f"{symbol} {(plan.metrics or {}).get('zone_label')} "
                    f"breakout_vol={setup.breakout_volume_ratio:.2f}x "
                    f"bars={setup.bars_since_breakout} stop={plan.stop_pct:.2f}%"
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
                f"progress={index}/{len(rows)} setups={setups} "
                f"triggers={len(triggers)} errors={errors}"
            )
    print(
        f"DONE seconds={time.time()-started:.1f} setups={setups} "
        f"triggers={len(triggers)} errors={errors}"
    )
    print(f"XEC {xec_result}")
    print(f"TOP_REJECTIONS {rejections.most_common(12)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
