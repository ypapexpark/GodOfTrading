#!/usr/bin/env python3
"""EMA 계열 체결에 MACD 히스토그램 정렬 필터를 반사실 적용.

실거래 당시 entry_context.macd 는 대부분 ok=False/value=0 (하드코딩) 이라
진입 timestamp 기준으로 OHLCV를 다시 받아 히스토그램 정렬을 재계산한다.

사용:
  python3 tools/macd_ema_filter_cf.py
  python3 tools/macd_ema_filter_cf.py --venue bybit
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ccxt  # noqa: E402
from strategies import macd_hist_alignment  # noqa: E402

TF_MAP = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}


def _load_trades(venue: str) -> list[dict]:
    path = ROOT / ("trade_state_binance.json" if venue == "binance" else "trade_state.json")
    if not path.exists():
        return []
    hist = json.loads(path.read_text()).get("trade_history") or []
    out = []
    for t in hist:
        if t.get("status") not in ("win", "loss"):
            continue
        if "EMA눌림목" not in str(t.get("strategy") or ""):
            continue
        if not t.get("timestamp"):
            continue
        out.append(t)
    return out


def _ex(venue: str):
    if venue == "binance":
        return ccxt.binanceusdm({"enableRateLimit": True})
    return ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "linear"}})


def _fetch_df(ex, symbol: str, tf: str, ts: float):
    import pandas as pd

    fsym = symbol if ":" in symbol else f"{symbol.split('/')[0]}/USDT:USDT"
    # 진입 시점 이전 120봉
    since_ms = int((ts - 120 * _tf_seconds(tf)) * 1000)
    rows = ex.fetch_ohlcv(fsym, TF_MAP.get(tf, tf), since=since_ms, limit=150)
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = df["ts"] / 1000.0
    # 진입 시각 직전 봉까지
    df = df[df["ts"] <= ts + 60].tail(100)
    if len(df) < 40:
        return None
    return df


def _tf_seconds(tf: str) -> int:
    return {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}.get(tf, 900)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="bybit", choices=["bybit", "binance", "both"])
    args = ap.parse_args()
    venues = ["bybit", "binance"] if args.venue == "both" else [args.venue]

    for venue in venues:
        trades = _load_trades(venue)
        print(f"\n=== {venue} EMA closed trades: {len(trades)} ===")
        if not trades:
            continue
        ex = _ex(venue)
        aligned_w = aligned_l = 0
        mis_w = mis_l = 0
        aligned_pnl = mis_pnl = 0.0
        all_pnl = 0.0
        skipped = 0
        by_dir = defaultdict(lambda: {"a_n": 0, "a_pnl": 0.0, "m_n": 0, "m_pnl": 0.0})

        for i, t in enumerate(trades):
            try:
                df = _fetch_df(ex, t["symbol"], t.get("tf") or "15m", float(t["timestamp"]))
            except Exception as e:
                skipped += 1
                if skipped <= 3:
                    print(f"  skip {t.get('symbol')}: {e}")
                time.sleep(0.2)
                continue
            if df is None:
                skipped += 1
                continue
            direction = t.get("direction") or "LONG"
            align = macd_hist_alignment(df, direction)
            pnl = float(t.get("pnl_usd") or 0)
            win = t.get("status") == "win"
            all_pnl += pnl
            d = direction
            if align["ok"]:
                aligned_pnl += pnl
                if win:
                    aligned_w += 1
                else:
                    aligned_l += 1
                by_dir[d]["a_n"] += 1
                by_dir[d]["a_pnl"] += pnl
            else:
                mis_pnl += pnl
                if win:
                    mis_w += 1
                else:
                    mis_l += 1
                by_dir[d]["m_n"] += 1
                by_dir[d]["m_pnl"] += pnl
            if (i + 1) % 10 == 0:
                print(f"  ... {i+1}/{len(trades)}")
            time.sleep(0.15)

        a_n = aligned_w + aligned_l
        m_n = mis_w + mis_l
        print(f"evaluated: {a_n + m_n}  skipped: {skipped}")
        print(f"BASE (all EMA): n={a_n+m_n} pnl={all_pnl:+.2f}")
        if a_n:
            print(
                f"MACD aligned KEEP: n={a_n} WR={aligned_w/a_n*100:.1f}% "
                f"pnl={aligned_pnl:+.2f}"
            )
        if m_n:
            print(
                f"MACD misalign DROP: n={m_n} WR={mis_w/m_n*100:.1f}% "
                f"pnl={mis_pnl:+.2f}"
            )
            print(
                f"→ filter ON (drop misalign): n={a_n} pnl={aligned_pnl:+.2f} "
                f"(delta vs all {aligned_pnl - all_pnl:+.2f})"
            )
            # soft 0.70 on misalign: keep all but scale misalign pnl * 0.70 as size proxy
            soft_pnl = aligned_pnl + mis_pnl * 0.70
            print(f"→ soft×0.70 on misalign (size proxy): pnl≈{soft_pnl:+.2f}")
        for d, v in by_dir.items():
            print(
                f"  {d}: aligned n={v['a_n']} pnl={v['a_pnl']:+.2f} | "
                f"mis n={v['m_n']} pnl={v['m_pnl']:+.2f}"
            )


if __name__ == "__main__":
    main()
