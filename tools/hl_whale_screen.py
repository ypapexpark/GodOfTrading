#!/usr/bin/env python3
"""Hyperliquid 지갑 스크리닝.

1) 수동 주소:
   python3 tools/hl_whale_screen.py --wallets 0xabc...,0xdef...
2) config 재검증:
   python3 tools/hl_whale_screen.py --from-config
3) 공식 리더보드에서 후보 발굴 + config 기록:
   python3 tools/hl_whale_screen.py --from-leaderboard --write-config --top 12

공개 userFills / stats leaderboard 기반. PolyBacktest급 반사실은 아님.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.hyperliquid.xyz/info"
LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
CONFIG = ROOT / "hyperliquid_whale_config.json"


def user_fills(addr: str) -> list[dict]:
    r = requests.post(API, json={"type": "userFills", "user": addr}, timeout=25)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def summarize(addr: str) -> dict:
    fills = user_fills(addr)
    notionals = []
    coins = defaultdict(float)
    buys = sells = big = 0
    now_ms = int(time.time() * 1000)
    recent = 0
    for f in fills:
        try:
            px = float(f.get("px") or 0)
            sz = float(f.get("sz") or 0)
            n = abs(px * sz)
            notionals.append(n)
            coin = f.get("coin") or "?"
            coins[coin] += n
            side = str(f.get("side") or "").lower()
            if side in ("b", "buy"):
                buys += 1
            else:
                sells += 1
            if n >= 5000:
                big += 1
            t = int(f.get("time") or 0)
            if now_ms - t < 7 * 86400 * 1000:
                recent += 1
        except Exception:
            continue
    total_n = sum(notionals)
    return {
        "wallet": addr.lower(),
        "fills_n": len(fills),
        "total_notional": round(total_n, 2),
        "avg_notional": round(total_n / len(notionals), 2) if notionals else 0,
        "buy_n": buys,
        "sell_n": sells,
        "big_fills": big,
        "recent_7d_fills": recent,
        "top_coins": sorted(coins.items(), key=lambda x: -x[1])[:5],
    }


def fetch_leaderboard_candidates(limit_scan: int = 40) -> list[dict]:
    r = requests.get(LEADERBOARD, timeout=90)
    r.raise_for_status()
    rows = r.json().get("leaderboardRows") or []
    cands = []
    for row in rows:
        addr = (row.get("ethAddress") or "").lower()
        if not addr.startswith("0x"):
            continue
        av = float(row.get("accountValue") or 0)
        perfs = {}
        for item in row.get("windowPerformances") or []:
            if isinstance(item, list) and len(item) >= 2 and isinstance(item[1], dict):
                perfs[item[0]] = item[1]

        def pf(w: str, k: str) -> float:
            try:
                return float((perfs.get(w) or {}).get(k) or 0)
            except Exception:
                return 0.0

        mon_pnl, mon_vlm = pf("month", "pnl"), pf("month", "vlm")
        week_pnl, week_vlm = pf("week", "pnl"), pf("week", "vlm")
        day_pnl = pf("day", "pnl")
        if mon_vlm < 5_000_000 and week_vlm < 1_000_000:
            continue
        if mon_pnl <= 0 and week_pnl <= 0:
            continue
        if av < 100_000:
            continue
        score = mon_pnl + week_pnl * 2 + day_pnl
        cands.append({
            "wallet": addr,
            "account_value": round(av, 2),
            "pnl_day": round(day_pnl, 2),
            "pnl_week": round(week_pnl, 2),
            "pnl_month": round(mon_pnl, 2),
            "vlm_week": round(week_vlm, 2),
            "vlm_month": round(mon_vlm, 2),
            "score": score,
            "display": row.get("displayName") or "",
        })
    cands.sort(key=lambda x: -x["score"])
    selected = []
    for s in cands[:limit_scan]:
        try:
            sm = summarize(s["wallet"])
            time.sleep(0.12)
        except Exception as e:
            print(f"  skip {s['wallet'][:12]}... {e}")
            continue
        if sm["fills_n"] < 20 or sm["big_fills"] < 5 or sm["recent_7d_fills"] < 5:
            continue
        s.update(sm)
        s["note"] = "leaderboard_screen"
        selected.append(s)
        print(
            f"OK {s['wallet'][:14]}... monPnL=${s['pnl_month']/1e3:.0f}k "
            f"big={s['big_fills']} recent7d={s['recent_7d_fills']}"
        )
    return selected


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wallets", default="", help="comma-separated 0x addresses")
    ap.add_argument("--from-config", action="store_true")
    ap.add_argument("--from-leaderboard", action="store_true")
    ap.add_argument("--top", type=int, default=12, help="max whales to keep on write")
    ap.add_argument("--write-config", action="store_true")
    args = ap.parse_args()

    rows: list[dict] = []

    if args.from_leaderboard:
        print("fetching leaderboard…")
        rows = fetch_leaderboard_candidates(limit_scan=max(40, args.top * 3))
        rows = rows[: args.top]
    else:
        wallets = []
        if args.from_config and CONFIG.exists():
            cfg = json.loads(CONFIG.read_text())
            wallets.extend(cfg.get("seed_wallets") or [])
            wallets.extend(
                w.get("wallet") for w in (cfg.get("whales") or []) if w.get("wallet")
            )
        if args.wallets:
            wallets.extend([w.strip() for w in args.wallets.split(",") if w.strip()])
        wallets = list(dict.fromkeys(w.lower() for w in wallets if w.startswith("0x")))
        if not wallets:
            print("지갑 없음. --wallets / --from-config / --from-leaderboard")
            return 1
        for w in wallets:
            try:
                s = summarize(w)
                rows.append(s)
                print(
                    f"{w[:12]}... fills={s['fills_n']} big={s['big_fills']} "
                    f"recent7d={s['recent_7d_fills']} notional=${s['total_notional']:.0f}"
                )
            except Exception as e:
                print(f"{w[:12]}... ERR {e}")

    if args.write_config and rows:
        cfg = (
            json.loads(CONFIG.read_text())
            if CONFIG.exists()
            else {"params": {}, "seed_wallets": [], "whales": []}
        )
        if args.from_leaderboard:
            # 전면 교체 (스크리닝 결과로 모수 리셋)
            cfg["whales"] = []
            cfg["generated_at"] = time.strftime("%Y-%m-%d")
            cfg["source"] = LEADERBOARD
        existing = {w.get("wallet") for w in cfg.get("whales") or []}
        for s in rows:
            w = s["wallet"]
            if w in existing and not args.from_leaderboard:
                continue
            entry = {
                "wallet": w,
                "fills_n": s.get("fills_n"),
                "big_fills": s.get("big_fills"),
                "pnl_month": s.get("pnl_month"),
                "pnl_week": s.get("pnl_week"),
                "vlm_month": s.get("vlm_month"),
                "note": s.get("note") or "auto-screen",
            }
            if args.from_leaderboard:
                cfg["whales"].append(entry)
            else:
                cfg.setdefault("whales", []).append(entry)
            existing.add(w)
        cfg["whales"] = (cfg.get("whales") or [])[: args.top]
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"updated {CONFIG} whales={len(cfg['whales'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
