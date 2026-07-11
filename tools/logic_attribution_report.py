#!/usr/bin/env python3
"""실거래 로직 귀속 리포트: 기존 엔진 vs 2026-07-11 신규 스택.

  python3 tools/logic_attribution_report.py
  python3 tools/logic_attribution_report.py --venue both
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(venue: str) -> list[dict]:
    path = ROOT / (
        "trade_state_binance.json" if venue == "binance" else "trade_state.json"
    )
    if not path.exists():
        return []
    hist = json.loads(path.read_text()).get("trade_history") or []
    return [t for t in hist if t.get("status") in ("win", "loss", "open") or t.get("entry_context")]


def _attr(t: dict) -> dict:
    ctx = t.get("entry_context") or {}
    a = t.get("logic_attribution") or ctx.get("logic_attribution") or {}
    if not a and t.get("logic_stack_version"):
        a = {"stack_version": t.get("logic_stack_version"), "new_stack_applied": t.get("new_stack_applied")}
    return a if isinstance(a, dict) else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="both", choices=["bybit", "binance", "both"])
    args = ap.parse_args()
    venues = ["bybit", "binance"] if args.venue == "both" else [args.venue]

    for venue in venues:
        trades = _load(venue)
        print(f"\n=== {venue} trades with history: {len(trades)} ===")
        by_ver = defaultdict(lambda: {"n": 0, "w": 0, "l": 0, "pnl": 0.0, "open": 0})
        by_new = defaultdict(lambda: {"n": 0, "w": 0, "l": 0, "pnl": 0.0})
        feat_pnl = defaultdict(lambda: {"n": 0, "pnl": 0.0})

        tagged = 0
        for t in trades:
            a = _attr(t)
            if not a and not t.get("logic_stack_version"):
                ver = "pre-tag / legacy"
            else:
                tagged += 1
                ver = a.get("stack_version") or t.get("logic_stack_version") or "tagged"
            st = t.get("status")
            pnl = float(t.get("pnl_usd") or 0)
            by_ver[ver]["n"] += 1
            if st == "win":
                by_ver[ver]["w"] += 1
                by_ver[ver]["pnl"] += pnl
            elif st == "loss":
                by_ver[ver]["l"] += 1
                by_ver[ver]["pnl"] += pnl
            elif st == "open":
                by_ver[ver]["open"] += 1

            if a:
                key = "new_stack" if a.get("new_stack_applied") else "legacy_only_size"
                by_new[key]["n"] += 1
                if st in ("win", "loss"):
                    by_new[key]["pnl"] += pnl
                    if st == "win":
                        by_new[key]["w"] += 1
                    else:
                        by_new[key]["l"] += 1
                for f in a.get("new_features") or []:
                    feat_pnl[f]["n"] += 1
                    if st in ("win", "loss"):
                        feat_pnl[f]["pnl"] += pnl

        print(f"  tagged with logic_attribution: {tagged}")
        print("  by stack_version:")
        for ver, v in sorted(by_ver.items()):
            print(
                f"    {ver}: n={v['n']} open={v['open']} "
                f"W{v['w']}/L{v['l']} pnl={v['pnl']:+.2f}"
            )
        print("  by new_stack_applied (tagged only):")
        for k, v in by_new.items():
            wr = v["w"] / (v["w"] + v["l"]) * 100 if (v["w"] + v["l"]) else 0
            print(f"    {k}: n={v['n']} WR={wr:.0f}% pnl={v['pnl']:+.2f}")
        if feat_pnl:
            print("  new_features (co-occurrence, not pure A/B):")
            for f, v in sorted(feat_pnl.items(), key=lambda x: x[1]["pnl"]):
                print(f"    {f}: n={v['n']} pnl={v['pnl']:+.2f}")

        # journal tail
        jpath = ROOT / (
            "trade_execution_journal_binance.jsonl"
            if venue == "binance"
            else "trade_execution_journal.jsonl"
        )
        if jpath.exists():
            lines = jpath.read_text().splitlines()[-5:]
            print("  recent journal opened with tags:")
            for line in lines:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("event") != "opened":
                    continue
                la = o.get("logic_attribution") or {}
                print(
                    f"    {o.get('time')} {o.get('symbol')} {o.get('strategy')} "
                    f"new={la.get('new_stack_applied')} feats={la.get('new_features')}"
                )


if __name__ == "__main__":
    main()
