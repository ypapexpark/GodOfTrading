#!/usr/bin/env python3
"""고래 카피(폴리 LIVE + HL paper) 환경 점검 + 수동 할 일 출력.

  python3 tools/whale_copy_setup_check.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def main() -> int:
    print("=" * 60)
    print("Whale Copy Setup Check")
    print("=" * 60)
    print(f"Python: {sys.version.split()[0]}  ({sys.executable})")

    # --- Polymarket LIVE ---
    print("\n## A. Polymarket Whale LIVE")
    live = os.getenv("POLYMARKET_LIVE_TRADING_ENABLED", "").strip().lower() == "true"
    key = bool(os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("POLY_PRIVATE_KEY"))
    print(f"  LIVE flag: {live}")
    print(f"  PRIVATE_KEY set: {key}")

    clob_ok = False
    clob_name = None
    for name in ("py_clob_client", "py_clob_client_v2"):
        try:
            __import__(name)
            clob_ok = True
            clob_name = name
            break
        except Exception:
            pass
    print(f"  CLOB package: {clob_name or 'NOT INSTALLED'}")
    if not clob_ok:
        ver = tuple(int(x) for x in sys.version.split()[0].split(".")[:3])
        if ver < (3, 9, 10):
            print("  ⚠ py-clob-client 는 Python >= 3.9.10 필요 (현재 3.9.6 등 불가)")
            print("    → brew install python@3.12 후 그 파이썬으로 pip install py-clob-client")
        else:
            print("    → pip install py-clob-client")

    try:
        from polymarket_clob_exec import smoke_test
        print("  smoke:", smoke_test())
    except Exception as e:
        print("  smoke err:", e)

    paper_state = ROOT / "polymarket_whale_paper_state.json"
    live_state = ROOT / "polymarket_whale_live_state.json"
    if paper_state.exists():
        s = json.loads(paper_state.read_text())
        print(f"  paper bankroll: ${float(s.get('bankroll') or 0):.2f} open={len(s.get('open_positions') or [])}")
    if live_state.exists():
        s = json.loads(live_state.read_text())
        print(f"  live  bankroll: ${float(s.get('bankroll') or 0):.2f} open={len(s.get('open_positions') or [])} mode={s.get('mode')}")

    # --- HL paper ---
    print("\n## B. Hyperliquid Whale Paper")
    cfg_path = ROOT / "hyperliquid_whale_config.json"
    wallets = []
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
        wallets = list(cfg.get("seed_wallets") or [])
        wallets += [w.get("wallet") for w in (cfg.get("whales") or []) if isinstance(w, dict)]
    env_w = os.getenv("HL_WHALE_WALLETS", "")
    if env_w:
        wallets += [x.strip() for x in env_w.split(",") if x.strip()]
    wallets = [w for w in wallets if str(w).startswith("0x")]
    print(f"  tracked wallets: {len(set(wallets))}")
    if not wallets:
        print("  ⚠ seed 없음 — hyperliquid_whale_config.json 의 seed_wallets 채우기")
        print("    또는: export HL_WHALE_WALLETS=0xabc...,0xdef...")

    print("\n## 수동 TODO (당신)")
    n = 1
    if not clob_ok:
        print(f"  {n}. Python 3.10+ 준비 + py-clob-client 설치")
        n += 1
    if not key:
        print(f"  {n}. .env 에 POLYMARKET_PRIVATE_KEY=0x... (소액 전용 지갑 권장)")
        n += 1
    if live:
        print(f"  {n}. LIVE=true 인 상태 — 단건 $5 cap 인지 재확인")
        n += 1
    else:
        print(f"  {n}. dry-run 유지 중. 실주문 시 POLYMARKET_LIVE_TRADING_ENABLED=true")
        n += 1
    print(f"  {n}. Polymarket용 소액 USDC(Polygon) 입금 (예: $50~200)")
    n += 1
    if not wallets:
        print(f"  {n}. HL 고래 주소 3~10개 seed_wallets 에 추가")
        n += 1
        print(f"     예: python3 tools/hl_whale_screen.py --wallets 0x... --write-config")
        n += 1
    print(f"  {n}. (선택) 규제/약관·지역 이용 가능 여부 본인 확인")
    n += 1
    print(f"  {n}. 준비 끝나면: 「폴리 live 스모크 다시」 / 「HL seed 넣었어」 라고 말하기")

    print("\n## 자동으로 이미 도는 것")
    print("  - com.polymarket.whale.paper (기존 paper)")
    print("  - com.polymarket.whale.live (dry-run 스캔, LIVE 전 주문 안 함)")
    print("  - com.hyperliquid.whale.paper (지갑 생기면 카피 시작)")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
