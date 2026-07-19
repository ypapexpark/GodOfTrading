#!/usr/bin/env python3
"""Explicitly rebase drawdown state after a user-confirmed deposit/withdrawal.

This never changes trade history. It records the previous risk state in
``capital_rebases`` and atomically replaces only the selected venue state file.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))


def _state_path(venue: str) -> Path:
    return ROOT / ("trade_state_binance.json" if venue == "binance" else "trade_state.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", choices=("bybit", "binance"), required=True)
    ap.add_argument("--equity", type=float, required=True)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--reset-daily-loss", action="store_true")
    args = ap.parse_args()
    if args.equity <= 0:
        raise SystemExit("equity must be positive")

    path = _state_path(args.venue)
    state = json.loads(path.read_text(encoding="utf-8"))
    previous = {
        key: state.get(key)
        for key in (
            "equity_start", "equity_peak", "last_equity", "drawdown_pct",
            "max_drawdown_pct", "all_time_drawdown_pct", "drawdown_guard_peak",
            "drawdown_status", "pause_until", "hard_stop_started_ts",
            "hard_stop_until", "daily_loss", "consec_loss",
        )
    }
    now = time.time()
    equity = round(args.equity, 4)
    state.setdefault("capital_rebases", []).append({
        "timestamp": now,
        "time": datetime.fromtimestamp(now, KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "venue": args.venue,
        "reason": args.reason,
        "new_equity": equity,
        "previous": previous,
    })
    state.update({
        "equity_start": equity,
        "equity_peak": equity,
        "drawdown_guard_peak": equity,
        "last_equity": equity,
        "drawdown_pct": 0.0,
        "all_time_drawdown_pct": 0.0,
        "drawdown_status": "normal",
        "pause_until": 0,
        "hard_stop_started_ts": 0,
        "hard_stop_until": 0,
    })
    if args.reset_daily_loss:
        state["daily_loss"] = 0.0
        state["consec_loss"] = 0

    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    print(json.dumps({
        "venue": args.venue,
        "equity": equity,
        "daily_loss": state.get("daily_loss"),
        "capital_rebases": len(state["capital_rebases"]),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
