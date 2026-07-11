#!/usr/bin/env python3
"""포스트모템 집계.

  python3 tools/postmortem_report.py
  python3 tools/postmortem_report.py --venue binance --last 20
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _path(venue: str) -> Path:
    if venue == "binance":
        return ROOT / "trade_postmortem_binance.jsonl"
    return ROOT / "trade_postmortem.jsonl"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="both", choices=["bybit", "binance", "both"])
    ap.add_argument("--last", type=int, default=30)
    args = ap.parse_args()
    venues = ["bybit", "binance"] if args.venue == "both" else [args.venue]

    for venue in venues:
        p = _path(venue)
        print(f"\n=== {venue} {p.name} ===")
        if not p.exists():
            print("  (파일 없음 — 청산이 아직 없거나 경로 확인)")
            continue
        rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        rows = rows[-args.last:]
        print(f"  rows(last {args.last}): {len(rows)}")
        by_st = Counter(r.get("status") for r in rows)
        print(f"  status: {dict(by_st)}")
        cause_n = Counter()
        cause_pnl = defaultdict(float)
        for r in rows:
            primary = (r.get("primary_cause") or {}).get("code") or "unknown"
            cause_n[primary] += 1
            cause_pnl[primary] += float(r.get("pnl_usd") or 0)
        print("  primary causes:")
        for code, n in cause_n.most_common(12):
            print(f"    {code}: n={n} pnl={cause_pnl[code]:+.2f}")
        print("  recent:")
        for r in rows[-8:]:
            print(f"    {r.get('time')} {r.get('summary_ko')}")


if __name__ == "__main__":
    main()
