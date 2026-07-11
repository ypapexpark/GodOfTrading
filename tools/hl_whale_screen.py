#!/usr/bin/env python3
"""Hyperliquid 지갑 스크리닝 (가벼운 버전).

공개 userFills 로 최근 체결을 모아 활동성·방향 편향을 요약한다.
완전한 백테스트(폴리 PolyBacktest급)는 별도 — 여기는 seed 후보 발굴용.

  python3 tools/hl_whale_screen.py --wallets 0xabc...,0xdef...
  python3 tools/hl_whale_screen.py --from-config
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.hyperliquid.xyz/info"
CONFIG = ROOT / "hyperliquid_whale_config.json"


def user_fills(addr: str) -> list[dict]:
    r = requests.post(API, json={"type": "userFills", "user": addr}, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def summarize(addr: str) -> dict:
    fills = user_fills(addr)
    notionals = []
    coins = defaultdict(float)
    buys = sells = 0
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
        except Exception:
            continue
    total_n = sum(notionals)
    return {
        "wallet": addr,
        "fills_n": len(fills),
        "total_notional": round(total_n, 2),
        "avg_notional": round(total_n / len(notionals), 2) if notionals else 0,
        "buy_n": buys,
        "sell_n": sells,
        "top_coins": sorted(coins.items(), key=lambda x: -x[1])[:5],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallets", default="", help="comma-separated 0x addresses")
    ap.add_argument("--from-config", action="store_true")
    ap.add_argument("--write-config", action="store_true", help="merge into whales if active enough")
    args = ap.parse_args()

    wallets = []
    if args.from_config and CONFIG.exists():
        cfg = json.loads(CONFIG.read_text())
        wallets.extend(cfg.get("seed_wallets") or [])
        wallets.extend(w.get("wallet") for w in (cfg.get("whales") or []) if w.get("wallet"))
    if args.wallets:
        wallets.extend([w.strip() for w in args.wallets.split(",") if w.strip()])
    wallets = list(dict.fromkeys(w for w in wallets if w.startswith("0x")))

    if not wallets:
        print("지갑 없음. --wallets 0x... 또는 config seed_wallets 채우기")
        print("예: python3 tools/hl_whale_screen.py --wallets 0xYOUR...")
        return 1

    rows = []
    for w in wallets:
        try:
            s = summarize(w)
            rows.append(s)
            print(
                f"{w[:12]}... fills={s['fills_n']} notional=${s['total_notional']:.0f} "
                f"B/S={s['buy_n']}/{s['sell_n']} top={s['top_coins'][:3]}"
            )
        except Exception as e:
            print(f"{w[:12]}... ERR {e}")

    if args.write_config and rows:
        cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {"params": {}, "seed_wallets": [], "whales": []}
        existing = {w.get("wallet") for w in cfg.get("whales") or []}
        for s in rows:
            if s["fills_n"] < 5:
                continue
            if s["wallet"] in existing:
                continue
            cfg.setdefault("whales", []).append({
                "wallet": s["wallet"],
                "fills_n": s["fills_n"],
                "total_notional": s["total_notional"],
                "note": "auto-screen light",
            })
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"updated {CONFIG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
