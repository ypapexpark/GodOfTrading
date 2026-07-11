#!/usr/bin/env python3
"""Hyperliquid 고래 지갑 paper 카피 봇 (골격).

폴리 고래 봇과 같은 패턴:
  1) config의 whales/seed 지갑 userFills 폴링
  2) 큰 신규 체결 → paper 포지션 (고정 노셔널, 레버 캡)
  3) 마크 가격으로 미실현 추적 / 청산 감지 시 정산
  4) 텔레그램 리포트

실주문 없음. 지갑 리스트는 tools/hl_whale_screen.py 로 채운다.

  python3 hyperliquid_whale_paper_bot.py
  python3 hyperliquid_whale_paper_bot.py --report-now
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL.*", category=Warning)

import requests
from dotenv import load_dotenv

from publisher import send_review

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

CONFIG_FILE = ROOT / "hyperliquid_whale_config.json"
STATE_FILE = ROOT / "hyperliquid_whale_paper_state.json"
JOURNAL_FILE = ROOT / "hyperliquid_whale_paper_journal.jsonl"
API = "https://api.hyperliquid.xyz/info"
KST = timezone(timedelta(hours=9))


def _now() -> float:
    return time.time()


def _now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")


def _json_safe(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return 0.0
    return str(v)


def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {"params": {}, "whales": [], "seed_wallets": []}
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def _params(cfg: dict) -> dict:
    p = cfg.get("params") or {}
    return {
        "min_fill_notional_usd": float(p.get("min_fill_notional_usd", 5000)),
        "copy_notional_usd": float(p.get("copy_notional_usd", 25)),
        "max_leverage_copy": float(p.get("max_leverage_copy", 5)),
        "max_open_positions": int(p.get("max_open_positions", 8)),
        "slippage_bps": float(p.get("slippage_bps", 15)),
    }


def _wallets(cfg: dict) -> list[str]:
    out = []
    # env override/add: HL_WHALE_WALLETS=0xabc,0xdef
    import os
    env_w = os.getenv("HL_WHALE_WALLETS", "").strip()
    if env_w:
        for part in env_w.split(","):
            a = part.strip().lower()
            if a.startswith("0x"):
                out.append(a)
    for w in cfg.get("seed_wallets") or []:
        if isinstance(w, str) and w.startswith("0x"):
            out.append(w.lower())
    for w in cfg.get("whales") or []:
        addr = (w.get("wallet") if isinstance(w, dict) else w) or ""
        if str(addr).startswith("0x"):
            out.append(str(addr).lower())
    return list(dict.fromkeys(out))


def _load_state(cfg: dict) -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "wallets": {
            w: {"last_fill_time": 0, "status": "active", "copied_keys": {}}
            for w in _wallets(cfg)
        },
        "open_positions": [],
        "bankroll": 1000.0,
        "last_report_time": 0.0,
        "last_scan": {},
    }


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(_json_safe(state), ensure_ascii=False, indent=2), encoding="utf-8")


def _append(row: dict) -> None:
    with JOURNAL_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_json_safe(row), ensure_ascii=False) + "\n")


def _post(body: dict) -> Any:
    r = requests.post(API, json=body, timeout=20)
    r.raise_for_status()
    return r.json()


def _user_fills(addr: str) -> list[dict]:
    data = _post({"type": "userFills", "user": addr})
    return data if isinstance(data, list) else []


def _mids() -> dict[str, float]:
    """coin -> mid price."""
    try:
        data = _post({"type": "allMids"})
        if isinstance(data, dict):
            return {k: float(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _clearinghouse(addr: str) -> dict:
    try:
        return _post({"type": "clearinghouseState", "user": addr}) or {}
    except Exception:
        return {}


def scan_signals(state: dict, cfg: dict) -> list[dict]:
    p = _params(cfg)
    signals = []
    for wallet in _wallets(cfg):
        wstate = state["wallets"].setdefault(
            wallet, {"last_fill_time": 0, "status": "active", "copied_keys": {}}
        )
        if wstate.get("status") == "suspended":
            continue
        try:
            fills = _user_fills(wallet)
        except Exception as e:
            wstate["last_error"] = str(e)
            continue
        # newest first typically — sort by time
        fills = sorted(fills, key=lambda f: int(f.get("time") or 0))
        last_t = int(wstate.get("last_fill_time") or 0)
        for f in fills:
            t = int(f.get("time") or 0)
            if t <= last_t:
                continue
            try:
                px = float(f.get("px") or 0)
                sz = float(f.get("sz") or 0)
                notional = abs(px * sz)
            except Exception:
                continue
            wstate["last_fill_time"] = max(int(wstate.get("last_fill_time") or 0), t)
            if notional < p["min_fill_notional_usd"]:
                continue
            coin = str(f.get("coin") or "")
            if not coin or coin.startswith("@"):
                continue  # skip some spot/special
            side_raw = str(f.get("side") or "").lower()
            direction = "LONG" if side_raw in ("b", "buy") else "SHORT"
            key = f"{wallet}:{coin}:{t}:{direction}"
            if wstate.setdefault("copied_keys", {}).get(key):
                continue
            wstate["copied_keys"][key] = True
            # prune keys map
            if len(wstate["copied_keys"]) > 500:
                # keep arbitrary tail
                items = list(wstate["copied_keys"].items())[-200:]
                wstate["copied_keys"] = dict(items)
            signals.append({
                "wallet": wallet,
                "coin": coin,
                "direction": direction,
                "px": px,
                "sz": sz,
                "notional": notional,
                "fill_time": t,
                "key": key,
            })
    return signals


def open_paper(signals: list[dict], state: dict, cfg: dict) -> int:
    p = _params(cfg)
    mids = _mids()
    opened = 0
    for sig in signals:
        open_n = len(state.get("open_positions") or [])
        if open_n >= p["max_open_positions"]:
            break
        # one position per coin
        if any(x.get("coin") == sig["coin"] for x in state.get("open_positions") or []):
            continue
        mid = float(mids.get(sig["coin"]) or sig["px"] or 0)
        if mid <= 0:
            continue
        slip = p["slippage_bps"] / 10000.0
        entry = mid * (1 + slip) if sig["direction"] == "LONG" else mid * (1 - slip)
        notional = p["copy_notional_usd"]
        pos = {
            "wallet": sig["wallet"],
            "coin": sig["coin"],
            "direction": sig["direction"],
            "entry_price": round(entry, 6),
            "notional_usd": notional,
            "qty": round(notional / entry, 8),
            "max_leverage": p["max_leverage_copy"],
            "whale_notional": round(sig["notional"], 2),
            "opened_at": _now_kst(),
            "opened_ts": _now(),
            "source_fill_time": sig["fill_time"],
        }
        state.setdefault("open_positions", []).append(pos)
        _append({**pos, "event": "opened"})
        opened += 1
        print(
            f"  [HL-paper] {sig['direction']} {sig['coin']} ${notional:.0f} "
            f"(whale ${sig['notional']:.0f}) via {sig['wallet'][:10]}..."
        )
    return opened


def mark_and_settle(state: dict, cfg: dict) -> int:
    """마크 업데이트. 고래가 포지션을 닫으면(같은 코인 반대 대량 체결 단순화) 정산.
    v1: 미실현만 갱신, 자동 청산은 보유 12시간 또는 mid 기반 임의 청산 없음.
    청산 트리거: 소스 지갑 clearinghouse에 해당 코인 포지션 없음.
    """
    settled = 0
    remaining = []
    mids = _mids()
    for pos in state.get("open_positions") or []:
        coin = pos["coin"]
        mid = float(mids.get(coin) or pos["entry_price"])
        entry = float(pos["entry_price"])
        qty = float(pos["qty"])
        if pos["direction"] == "LONG":
            upnl = (mid - entry) * qty
        else:
            upnl = (entry - mid) * qty
        pos["mark"] = mid
        pos["unrealized_pnl"] = round(upnl, 4)

        # whale flat? clearinghouse 정상 응답일 때만 판정
        closed = False
        try:
            ch = _clearinghouse(pos["wallet"])
            if not isinstance(ch, dict) or (
                "assetPositions" not in ch and "marginSummary" not in ch
            ):
                remaining.append(pos)
                continue
            asset_pos = ch.get("assetPositions") or []
            still = False
            for ap in asset_pos:
                if not isinstance(ap, dict):
                    continue
                pos_inner = ap.get("position") if isinstance(ap.get("position"), dict) else ap
                c = pos_inner.get("coin")
                try:
                    szi = float(pos_inner.get("szi") or 0)
                except Exception:
                    szi = 0.0
                if c == coin and abs(szi) > 1e-12:
                    still = True
                    break
            if not still and _now() - float(pos.get("opened_ts") or 0) > 120:
                closed = True
        except Exception:
            closed = False

        if closed:
            pnl = float(pos.get("unrealized_pnl") or 0)
            row = {
                **pos,
                "event": "settled",
                "settled_at": _now_kst(),
                "pnl_usd": round(pnl, 4),
                "exit_price": mid,
            }
            _append(row)
            state["bankroll"] = float(state.get("bankroll") or 1000) + pnl
            settled += 1
            print(f"  [HL-paper] settle {pos['direction']} {coin} pnl={pnl:+.2f}")
        else:
            remaining.append(pos)
    state["open_positions"] = remaining
    return settled


def build_report(state: dict, cfg: dict) -> str:
    p = _params(cfg)
    rows = []
    if JOURNAL_FILE.exists():
        for line in JOURNAL_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    settled = [r for r in rows if r.get("event") == "settled"]
    pnl = sum(float(r.get("pnl_usd") or 0) for r in settled)
    lines = [
        f"🌊 <b>[Hyperliquid 고래 Paper 카피]</b> — {datetime.now(KST).strftime('%m/%d %H:%M KST')}",
        f"bankroll ${float(state.get('bankroll') or 1000):.2f} | 정산 {len(settled)} | PnL ${pnl:+.2f}",
        f"오픈 {len(state.get('open_positions') or [])}/{p['max_open_positions']} | "
        f"카피 ${p['copy_notional_usd']:.0f} | min whale fill ${p['min_fill_notional_usd']:.0f}",
        f"추적 지갑 {len(_wallets(cfg))}개",
        "",
        "※ 실주문 없음. seed/whales 비어 있으면 동작 안 함 → tools/hl_whale_screen.py",
    ]
    for pos in (state.get("open_positions") or [])[:5]:
        lines.append(
            f"• {pos.get('direction')} {escape(str(pos.get('coin')))} "
            f"uPnL ${float(pos.get('unrealized_pnl') or 0):+.2f}"
        )
    return "\n".join(lines)


def run_once(report_now: bool = False) -> dict:
    cfg = _load_config()
    wallets = _wallets(cfg)
    state = _load_state(cfg)
    # ensure new wallets
    for w in wallets:
        state["wallets"].setdefault(w, {"last_fill_time": 0, "status": "active", "copied_keys": {}})

    if not wallets:
        result = {
            "ok": False,
            "error": "no wallets — fill hyperliquid_whale_config.json seed_wallets or whales",
            "wallets": 0,
        }
        print(f"[HLWhalePaper] {result}")
        return result

    settled = mark_and_settle(state, cfg)
    signals = scan_signals(state, cfg)
    opened = open_paper(signals, state, cfg)
    state["last_scan"] = {
        "time": _now_kst(),
        "signals": len(signals),
        "opened": opened,
        "settled": settled,
        "wallets": len(wallets),
    }
    if report_now or (_now() - float(state.get("last_report_time") or 0) > 4 * 3600):
        if send_review(build_report(state, cfg)):
            state["last_report_time"] = _now()
    _save_state(state)
    return {
        "ok": True,
        "wallets": len(wallets),
        "signals": len(signals),
        "opened": opened,
        "settled": settled,
        "open_positions": len(state.get("open_positions") or []),
        "bankroll": state.get("bankroll"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report-now", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = run_once(report_now=args.report_now)
    if args.json:
        print(json.dumps(_json_safe(result), ensure_ascii=False))
    else:
        print(f"[HLWhalePaper] {result}")
    return 0 if result.get("ok", True) or result.get("wallets") == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
