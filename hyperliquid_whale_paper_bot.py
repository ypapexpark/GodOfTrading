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
from collections import defaultdict
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
POLICY = "2026-07-19-hl-position-delta-taker-v2"
LEGACY_POLICY = "2026-07-11-hl-fill-copy-v1"

from bot_util import (  # noqa: E402
    KST,
    append_jsonl,
    json_safe as _json_safe,
    load_json,
    now as _now,
    now_kst as _now_kst,
    save_json,
)


def _load_config() -> dict:
    data = load_json(CONFIG_FILE, default=None)
    if isinstance(data, dict):
        return data
    return {"params": {}, "whales": [], "seed_wallets": []}

def _params(cfg: dict) -> dict:
    p = cfg.get("params") or {}
    return {
        # 2026-07-12: 5000→3000. Open 레그의 ~72%가 $5k 미만이지만 $2k는
        # 분할/시드 노이즈 비중이 큼. $3k는 7d 후보 ~1.25×, 품질 타협점.
        "min_fill_notional_usd": float(p.get("min_fill_notional_usd", 3000)),
        # V2는 개별 fill이 아니라 같은 폴링 구간의 taker open fill을 합산한 뒤,
        # 실제 clearinghouse 순포지션 증가가 이 금액 이상 남아 있을 때만 복사한다.
        "min_aggregate_open_notional_usd": float(
            p.get("min_aggregate_open_notional_usd", 50_000)
        ),
        "min_confirmed_position_notional_usd": float(
            p.get("min_confirmed_position_notional_usd", 50_000)
        ),
        "min_position_equity_pct": float(p.get("min_position_equity_pct", 0.005)),
        "max_signal_lag_seconds": float(p.get("max_signal_lag_seconds", 75)),
        "require_taker_open": bool(p.get("require_taker_open", True)),
        "copy_notional_usd": float(p.get("copy_notional_usd", 25)),
        "max_leverage_copy": float(p.get("max_leverage_copy", 5)),
        "max_open_positions": int(p.get("max_open_positions", 8)),
        "slippage_bps": float(p.get("slippage_bps", 15)),
        "taker_fee_bps": float(p.get("taker_fee_bps", 5)),
        "report_interval_seconds": int(p.get("report_interval_seconds", 4 * 3600)),
        "max_hold_hours": float(p.get("max_hold_hours", 48)),
        "max_hold_hours_v2": float(p.get("max_hold_hours_v2", 168)),
        "min_whale_flat_age_sec": float(p.get("min_whale_flat_age_sec", 180)),
        "min_whale_flat_age_sec_v2": float(
            p.get("min_whale_flat_age_sec_v2", 30)
        ),
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


# 정산 표본 마일스톤 — 도달 시 1회 텔레그램 리마인드 (까먹지 않게)
# 20: 1차 판단 가능 / 30: 모수·min_fill 재평가 / 50: 라이브 검토 후보
SAMPLE_MILESTONES = (20, 30, 50)


def _load_state(cfg: dict) -> dict:
    data = load_json(STATE_FILE, default=None)
    if isinstance(data, dict):
        data.setdefault("sample_milestones_sent", {})
        data.setdefault("policy", POLICY)
        data.setdefault("policy_bankrolls", {})
        data["policy_bankrolls"].setdefault(POLICY, 1000.0)
        data.setdefault("policy_diagnostics", {})
        return data
    return {
        "wallets": {
            w: {"last_fill_time": 0, "status": "active", "copied_keys": {}}
            for w in _wallets(cfg)
        },
        "open_positions": [],
        "bankroll": 1000.0,
        "policy": POLICY,
        "policy_bankrolls": {POLICY: 1000.0},
        "policy_diagnostics": {},
        "last_report_time": 0.0,
        "last_scan": {},
        "sample_milestones_sent": {},
    }


def _save_state(state: dict) -> None:
    save_json(STATE_FILE, state)


def _append(row: dict) -> None:
    append_jsonl(JOURNAL_FILE, row)


def _post(body: dict, retries: int = 3, timeout: int = 25) -> Any:
    """HL info API — 간헐적 네트워크 실패 재시도."""
    last: Exception | None = None
    for i in range(max(1, retries)):
        try:
            r = requests.post(API, json=body, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if i + 1 < retries:
                time.sleep(0.35 * (i + 1))
    assert last is not None
    raise last


def _coin_dex(coin: str) -> str:
    """'xyz:ORCL' → 'xyz', 'BTC' → '' (메인 perp dex)."""
    c = str(coin or "")
    if ":" in c:
        return c.split(":", 1)[0].strip()
    return ""


def _coins_match(a: str, b: str) -> bool:
    """심볼 매칭 (정확 일치 또는 dex 접두 정규화)."""
    if not a or not b:
        return False
    if a == b:
        return True
    # xyz:ORCL vs ORCL (드묾)
    al, bl = a.split(":")[-1], b.split(":")[-1]
    if al == bl and (":" in a or ":" in b):
        # 같은 base 여도 dex 다르면 다른 마켓 — 한쪽만 prefix 있을 때만 허용
        return (":" in a) != (":" in b)
    return False


def _user_fills(addr: str) -> list[dict]:
    data = _post({"type": "userFills", "user": addr, "aggregateByTime": True})
    return data if isinstance(data, list) else []


def _user_fills_since(addr: str, start_time_ms: int) -> list[dict]:
    """Incremental aggregated fills; avoids downloading 2,000 rows every poll."""
    data = _post({
        "type": "userFillsByTime",
        "user": addr,
        "startTime": max(int(start_time_ms), 0),
        "endTime": int(_now() * 1000),
        "aggregateByTime": True,
    })
    return data if isinstance(data, list) else []


def _perp_dex_names() -> list[str]:
    """HIP-3 등 추가 perp dex 이름 목록."""
    try:
        data = _post({"type": "perpDexs"})
        if not isinstance(data, list):
            return []
        out = []
        for d in data:
            if isinstance(d, dict) and d.get("name"):
                out.append(str(d["name"]))
        return out
    except Exception:
        return ["xyz"]  # 폴백: 주식 perp 등


def _mids(coins: list[str] | None = None) -> dict[str, float]:
    """coin -> mid. 필요한 perp dex만 조회하고 HIP-3 접두사를 보존한다."""
    out: dict[str, float] = {}
    requested = list(coins or [])
    dexes = {""}
    if requested:
        dexes.update(_coin_dex(coin) for coin in requested if _coin_dex(coin))
    else:
        dexes.update(_perp_dex_names())
    for dex in sorted(dexes):
        try:
            body = {"type": "allMids"}
            if dex:
                body["dex"] = dex
            data = _post(body)
            if isinstance(data, dict):
                for k, v in data.items():
                    try:
                        raw = str(k)
                        canonical = raw if not dex or ":" in raw else f"{dex}:{raw}"
                        out[canonical] = float(v)
                    except Exception:
                        pass
        except Exception:
            continue
    return out


def _clearinghouse(addr: str, dex: str = "") -> dict | None:
    """포지션 조회. dex='' 메인, 'xyz' 등 HIP-3.

    실패 시 None (빈 dict 과 구분 — 실패를 whale_flat 로 오인 금지).
    """
    body: dict[str, Any] = {"type": "clearinghouseState", "user": addr}
    if dex:
        body["dex"] = dex
    try:
        data = _post(body)
        if isinstance(data, dict) and (
            "assetPositions" in data or "marginSummary" in data
        ):
            return data
        return None
    except Exception:
        return None


def _position_szi(ch: dict, coin: str) -> float | None:
    """clearinghouse 에서 coin 포지션 szi. 없으면 0. 조회 불가면 None 아님 — 호출측에서 ch None 처리."""
    for ap in ch.get("assetPositions") or []:
        if not isinstance(ap, dict):
            continue
        pos_inner = ap.get("position") if isinstance(ap.get("position"), dict) else ap
        c = str(pos_inner.get("coin") or "")
        if not _coins_match(c, coin) and c != coin:
            continue
        try:
            return float(pos_inner.get("szi") or 0)
        except Exception:
            return 0.0
    return 0.0


def _position_snapshot(ch: dict, coin: str) -> dict[str, float] | None:
    """Return the whale's current signed size and conviction for one market."""
    account_value = 0.0
    try:
        account_value = float((ch.get("marginSummary") or {}).get("accountValue") or 0)
    except Exception:
        pass
    for ap in ch.get("assetPositions") or []:
        if not isinstance(ap, dict):
            continue
        position = ap.get("position") if isinstance(ap.get("position"), dict) else ap
        if not _coins_match(str(position.get("coin") or ""), coin):
            continue
        try:
            szi = float(position.get("szi") or 0)
            value = abs(float(position.get("positionValue") or 0))
        except Exception:
            return None
        return {
            "szi": szi,
            "position_notional": value,
            "account_value": account_value,
            "position_equity_pct": value / account_value if account_value > 0 else 0.0,
        }
    return {
        "szi": 0.0,
        "position_notional": 0.0,
        "account_value": account_value,
        "position_equity_pct": 0.0,
    }

def _fill_direction(f: dict) -> str:
    """HL side: B=buy/bid, A=sell/ask. dir 필드가 있으면 우선."""
    d = str(f.get("dir") or "").lower()
    if "long" in d or d in ("open long", "close short"):
        # Open Long / Close Short → 우리는 long 쪽 노출 증가/숏청산 = LONG 카피 신호로 Open Long만
        if "open long" in d:
            return "LONG"
        if "open short" in d:
            return "SHORT"
        # close * 는 진입 카피 대상 아님
        if "close" in d:
            return "CLOSE"
    side_raw = str(f.get("side") or "").lower()
    if side_raw in ("b", "buy"):
        return "LONG"
    return "SHORT"


def _explicit_open_direction(fill: dict) -> str:
    direction = " ".join(str(fill.get("dir") or "").lower().split())
    if direction == "open long":
        return "LONG"
    if direction == "open short":
        return "SHORT"
    return ""


def _record_policy_diagnostics(state: dict, counters: dict[str, int]) -> None:
    root = state.setdefault("policy_diagnostics", {})
    row = root.setdefault(POLICY, {"cumulative": {}})
    cumulative = row.setdefault("cumulative", {})
    for key, value in counters.items():
        cumulative[key] = int(cumulative.get(key) or 0) + int(value)
    row["last"] = dict(counters)
    row["last_at"] = _now_kst()


def scan_signals(state: dict, cfg: dict) -> list[dict]:
    p = _params(cfg)
    signals: list[dict] = []
    diagnostics: dict[str, int] = defaultdict(int)
    for wallet in _wallets(cfg):
        wstate = state["wallets"].setdefault(
            wallet, {"last_fill_time": 0, "status": "active", "copied_keys": {}, "seeded": False}
        )
        if wstate.get("status") == "suspended":
            continue
        last_t = int(wstate.get("last_fill_time") or 0)
        try:
            if not wstate.get("seeded") and last_t <= 0:
                fills = _user_fills(wallet)
            else:
                fills = _user_fills_since(wallet, last_t + 1)
            wstate.pop("last_error", None)
        except Exception as e:
            wstate["last_error"] = str(e)[:200]
            diagnostics["api_error"] += 1
            continue
        fills = sorted(fills, key=lambda fill: int(fill.get("time") or 0))
        if not fills:
            wstate["seeded"] = True
            continue
        if not wstate.get("seeded") and last_t <= 0:
            wstate["last_fill_time"] = max(int(fill.get("time") or 0) for fill in fills)
            wstate["seeded"] = True
            wstate["seeded_at"] = _now_kst()
            diagnostics["cold_seed"] += 1
            continue
        wstate["seeded"] = True
        fresh = [fill for fill in fills if int(fill.get("time") or 0) > last_t]
        if fresh:
            wstate["last_fill_time"] = max(int(fill.get("time") or 0) for fill in fresh)

        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for f in fresh:
            t = int(f.get("time") or 0)
            try:
                px = float(f.get("px") or 0)
                sz = float(f.get("sz") or 0)
                notional = abs(px * sz)
            except Exception:
                diagnostics["invalid_fill"] += 1
                continue
            coin = str(f.get("coin") or "")
            if not coin or coin.startswith("@"):
                diagnostics["unsupported_coin"] += 1
                continue
            direction = _explicit_open_direction(f)
            if not direction:
                diagnostics["not_open"] += 1
                continue
            if p["require_taker_open"] and not bool(f.get("crossed")):
                diagnostics["maker_open"] += 1
                continue
            group = groups.setdefault((coin, direction), {
                "wallet": wallet,
                "coin": coin,
                "direction": direction,
                "notional": 0.0,
                "size": 0.0,
                "weighted_price": 0.0,
                "fill_time": t,
                "first_time": t,
                "start_position": float(f.get("startPosition") or 0),
                "fills": 0,
            })
            group["notional"] += notional
            group["size"] += abs(sz)
            group["weighted_price"] += notional * px
            group["fill_time"] = max(int(group["fill_time"]), t)
            group["first_time"] = min(int(group["first_time"]), t)
            group["fills"] += 1

        ch_cache: dict[str, dict | None] = {}
        for (coin, direction), group in groups.items():
            aggregate_notional = float(group["notional"])
            if aggregate_notional < p["min_aggregate_open_notional_usd"]:
                diagnostics["aggregate_too_small"] += 1
                continue
            lag_seconds = max(_now() - int(group["fill_time"]) / 1000.0, 0.0)
            if lag_seconds > p["max_signal_lag_seconds"]:
                diagnostics["stale_signal"] += 1
                continue
            dex = _coin_dex(coin)
            if dex not in ch_cache:
                ch_cache[dex] = _clearinghouse(wallet, dex=dex)
            ch = ch_cache[dex]
            if ch is None:
                diagnostics["position_api_error"] += 1
                continue
            snapshot = _position_snapshot(ch, coin)
            if not snapshot:
                diagnostics["position_missing"] += 1
                continue
            sign = 1.0 if direction == "LONG" else -1.0
            current_directional_size = sign * float(snapshot["szi"])
            start_directional_size = max(sign * float(group["start_position"]), 0.0)
            if current_directional_size <= 0:
                diagnostics["position_not_same_direction"] += 1
                continue
            vwap = float(group["weighted_price"]) / max(aggregate_notional, 1e-12)
            confirmed_increase = max(current_directional_size - start_directional_size, 0.0) * vwap
            if confirmed_increase < p["min_aggregate_open_notional_usd"]:
                diagnostics["position_increase_too_small"] += 1
                continue
            if snapshot["position_notional"] < p["min_confirmed_position_notional_usd"]:
                diagnostics["position_too_small"] += 1
                continue
            if snapshot["position_equity_pct"] < p["min_position_equity_pct"]:
                diagnostics["conviction_too_small"] += 1
                continue
            key = f"{POLICY}:{wallet}:{coin}:{int(group['fill_time'])}:{direction}"
            if wstate.setdefault("copied_keys", {}).get(key):
                diagnostics["duplicate"] += 1
                continue
            wstate["copied_keys"][key] = True
            if len(wstate["copied_keys"]) > 500:
                items = list(wstate["copied_keys"].items())[-200:]
                wstate["copied_keys"] = dict(items)
            signals.append({
                "wallet": wallet,
                "coin": coin,
                "direction": direction,
                "px": vwap,
                "sz": float(group["size"]),
                "notional": aggregate_notional,
                "fill_time": int(group["fill_time"]),
                "key": key,
                "policy": POLICY,
                "aggregate_fills": int(group["fills"]),
                "signal_lag_seconds": lag_seconds,
                "start_position": float(group["start_position"]),
                "confirmed_szi": float(snapshot["szi"]),
                "confirmed_position_notional": float(snapshot["position_notional"]),
                "confirmed_position_equity_pct": float(snapshot["position_equity_pct"]),
                "confirmed_increase_notional": confirmed_increase,
                "crossed_taker": True,
            })
            diagnostics["accepted"] += 1
    _record_policy_diagnostics(state, diagnostics)
    return signals


def open_paper(signals: list[dict], state: dict, cfg: dict) -> int:
    p = _params(cfg)
    mids = _mids([str(signal.get("coin") or "") for signal in signals])
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
        entry_fee = notional * p["taker_fee_bps"] / 10_000.0
        pos = {
            "policy": str(sig.get("policy") or POLICY),
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
            "source_vwap": round(float(sig.get("px") or 0), 8),
            "signal_lag_seconds": round(float(sig.get("signal_lag_seconds") or 0), 3),
            "aggregate_fills": int(sig.get("aggregate_fills") or 1),
            "confirmed_position_notional": round(
                float(sig.get("confirmed_position_notional") or 0), 2
            ),
            "confirmed_position_equity_pct": round(
                float(sig.get("confirmed_position_equity_pct") or 0), 8
            ),
            "confirmed_increase_notional": round(
                float(sig.get("confirmed_increase_notional") or 0), 2
            ),
            "crossed_taker": bool(sig.get("crossed_taker")),
            "entry_fee_usd": round(entry_fee, 6),
            "slippage_bps": p["slippage_bps"],
            "taker_fee_bps": p["taker_fee_bps"],
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
    """마크 업데이트 + 정산.

    청산 트리거:
      1) 소스 지갑 clearinghouse(해당 dex) 에 같은 방향 포지션 없음 (고래 flat)
      2) max_hold_hours 초과

    중요: xyz:ORCL 등 HIP-3 는 dex='xyz' clearinghouse 를 봐야 함.
    메인 CH 만 보면 항상 flat 오판 → pnl≈0 즉시 정산 버그.
    CH 조회 실패 시에는 정산하지 않고 유지.
    """
    p = _params(cfg)
    settled = 0
    remaining = []
    positions = state.get("open_positions") or []
    mids = _mids([str(position.get("coin") or "") for position in positions])
    # (wallet, dex) -> clearinghouse | None(실패)
    ch_cache: dict[tuple[str, str], dict | None] = {}

    for pos in state.get("open_positions") or []:
        policy = str(pos.get("policy") or LEGACY_POLICY)
        max_hold_hours = (
            p["max_hold_hours_v2"] if policy == POLICY else p["max_hold_hours"]
        )
        max_hold = float(max_hold_hours) * 3600
        min_flat_age = (
            p["min_whale_flat_age_sec_v2"]
            if policy == POLICY else p["min_whale_flat_age_sec"]
        )
        coin = pos["coin"]
        dex = _coin_dex(coin)
        mid = float(mids.get(coin) or pos.get("mark") or pos["entry_price"])
        entry = float(pos["entry_price"])
        qty = float(pos["qty"])
        if policy == POLICY:
            slip = float(pos.get("slippage_bps") or p["slippage_bps"]) / 10_000.0
            executable_exit = (
                mid * (1 - slip) if pos["direction"] == "LONG" else mid * (1 + slip)
            )
            gross_upnl = (
                (executable_exit - entry) * qty
                if pos["direction"] == "LONG"
                else (entry - executable_exit) * qty
            )
            exit_fee = abs(executable_exit * qty) * (
                float(pos.get("taker_fee_bps") or p["taker_fee_bps"]) / 10_000.0
            )
            fees = float(pos.get("entry_fee_usd") or 0) + exit_fee
            upnl = gross_upnl - fees
        elif pos["direction"] == "LONG":
            executable_exit = mid
            gross_upnl = (mid - entry) * qty
            fees = 0.0
            upnl = gross_upnl
        else:
            executable_exit = mid
            gross_upnl = (entry - mid) * qty
            fees = 0.0
            upnl = gross_upnl
        pos["mark"] = mid
        pos["unrealized_pnl"] = round(upnl, 4)
        pos["estimated_gross_pnl"] = round(gross_upnl, 4)
        pos["estimated_fees_usd"] = round(fees, 4)
        pos["dex"] = dex or "main"

        closed = False
        reason = ""
        age = _now() - float(pos.get("opened_ts") or 0)

        # 1) max hold
        if age >= max_hold:
            closed = True
            reason = f"emergency_max_hold_{max_hold_hours}h"

        # 2) whale flat — 올바른 dex CH 필수
        if not closed and age >= min_flat_age:
            w = str(pos["wallet"])
            cache_key = (w, dex)
            if cache_key not in ch_cache:
                ch_cache[cache_key] = _clearinghouse(w, dex=dex)
            ch = ch_cache[cache_key]
            if ch is None:
                # 네트워크/API 실패 → 유지 (false whale_flat 금지)
                remaining.append(pos)
                continue
            szi = _position_szi(ch, coin)
            if szi is None:
                szi = 0.0
            still = False
            if abs(szi) > 1e-12:
                if pos["direction"] == "LONG" and szi > 0:
                    still = True
                elif pos["direction"] == "SHORT" and szi < 0:
                    still = True
                # 반대 방향만 남음 = 우리 카피 방향은 flat
            if not still:
                closed = True
                reason = "whale_flat"

        if closed:
            pnl = float(pos.get("unrealized_pnl") or 0)
            row = {
                **pos,
                "event": "settled",
                "settled_at": _now_kst(),
                "settled_ts": _now(),
                "pnl_usd": round(pnl, 4),
                "gross_pnl_usd": round(gross_upnl, 4),
                "fees_usd": round(fees, 4),
                "exit_price": round(executable_exit, 8),
                "mark_price": mid,
                "settle_reason": reason,
            }
            _append(row)
            state["bankroll"] = float(state.get("bankroll") or 1000) + pnl
            if policy == POLICY:
                bankrolls = state.setdefault("policy_bankrolls", {})
                bankrolls[POLICY] = float(bankrolls.get(POLICY) or 1000) + pnl
            settled += 1
            print(f"  [HL-paper] settle {pos['direction']} {coin} pnl={pnl:+.2f} ({reason})")
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
    all_settled = [r for r in rows if r.get("event") == "settled"]
    settled = [r for r in all_settled if str(r.get("policy") or LEGACY_POLICY) == POLICY]
    legacy_settled = [
        r for r in all_settled if str(r.get("policy") or LEGACY_POLICY) == LEGACY_POLICY
    ]
    opened = [
        r for r in rows
        if r.get("event") == "opened" and str(r.get("policy") or LEGACY_POLICY) == POLICY
    ]
    wins = [r for r in settled if float(r.get("pnl_usd") or 0) > 0]
    pnl = sum(float(r.get("pnl_usd") or 0) for r in settled)
    wr = len(wins) / len(settled) if settled else 0.0
    wallets_n = len(_wallets(cfg))

    # wallet pnl breakdown
    by_w: dict[str, float] = {}
    for r in settled:
        w = str(r.get("wallet") or "?")[:12]
        by_w[w] = by_w.get(w, 0.0) + float(r.get("pnl_usd") or 0)

    lines = [
        f"🌊 <b>[Hyperliquid 고래 Paper v2]</b> — "
        f"{datetime.now(KST).strftime('%m/%d %H:%M KST')}",
        "",
        "🏷 계좌: <b>hl_whale_paper</b> (Poly/Bybit 과 분리)",
        f"v2 bankroll ${float((state.get('policy_bankrolls') or {}).get(POLICY) or 1000):.2f} | "
        f"정산 {len(settled)}건 | 승(PnL&gt;0) {wr:.0%} | 누적 PnL ${pnl:+.2f}",
        f"오픈 {len(state.get('open_positions') or [])}/{p['max_open_positions']} | "
        f"카피 ${p['copy_notional_usd']:.0f}/건 | taker open 합산 "
        f"${p['min_aggregate_open_notional_usd']:,.0f}+ | "
        f"emergency hold {p['max_hold_hours_v2']:.0f}h",
        f"추적 지갑 {wallets_n}개 | v2 진입 기록 {len(opened)}건",
        f"비교 v1 정산 {len(legacy_settled)}건 | "
        f"누적 ${sum(float(r.get('pnl_usd') or 0) for r in legacy_settled):+.2f}",
    ]
    if state.get("open_positions"):
        lines.append("")
        lines.append("오픈:")
        for pos in (state.get("open_positions") or [])[:6]:
            lines.append(
                f"• {pos.get('direction')} {escape(str(pos.get('coin')))} "
                f"uPnL ${float(pos.get('unrealized_pnl') or 0):+.2f} "
                f"via {escape(str(pos.get('wallet') or '')[:10])}..."
            )
    if settled:
        lines.append("")
        lines.append("최근 정산:")
        for r in settled[-5:]:
            lines.append(
                f"• {escape(str(r.get('direction')))} {escape(str(r.get('coin')))} "
                f"${float(r.get('pnl_usd') or 0):+.2f} "
                f"({escape(str(r.get('settle_reason') or ''))})"
            )
    if by_w:
        top = sorted(by_w.items(), key=lambda x: -x[1])[:3]
        weak = sorted(by_w.items(), key=lambda x: x[1])[:2]
        lines.append("")
        lines.append("💡 개선 코멘트")
        if len(settled) < 20:
            lines.append(
                f"• [관찰] 정산 {len(settled)}/20건 — 1차 마일스톤 전. "
                f"도달 시 별도 TG 리마인드가 1회 갑니다."
            )
        elif len(settled) < 30:
            lines.append(
                f"• [관찰] 정산 {len(settled)}/30건 — 재평가 마일스톤 대기 중."
            )
        if top and top[0][1] > 0:
            lines.append(
                f"• [유지] 고성과 지갑 {escape(top[0][0])}... PnL ${top[0][1]:+.2f}"
            )
        if weak and weak[0][1] < 0 and len(settled) >= 10:
            lines.append(
                f"• [건의] 저성과 {escape(weak[0][0])}... PnL ${weak[0][1]:+.2f} — "
                f"제외 검토 (config whales[])."
            )
        lines.append(
            "• [개선] 분기 1회 tools/hl_whale_screen.py --from-leaderboard 로 모수 갱신."
        )
    lines += [
        "",
        "※ 실주문 없음 · taker open + 실제 순포지션 증가만 추종 · 폴리 고래와 계좌 분리.",
    ]
    return "\n".join(lines)


def _read_journal_settled(policy: str | None = None) -> list[dict]:
    rows: list[dict] = []
    if not JOURNAL_FILE.exists():
        return rows
    for line in JOURNAL_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        row_policy = str(r.get("policy") or LEGACY_POLICY)
        if r.get("event") == "settled" and (policy is None or row_policy == policy):
            rows.append(r)
    return rows


def build_sample_milestone_report(state: dict, cfg: dict, milestone: int) -> str:
    """표본 충분 마일스톤 전용 리포트 (4h 정기 리포트와 별도 1회 발송)."""
    p = _params(cfg)
    settled = _read_journal_settled(POLICY)
    n = len(settled)
    pnls = [float(r.get("pnl_usd") or 0) for r in settled]
    wins = sum(1 for x in pnls if x > 0)
    losses = sum(1 for x in pnls if x <= 0)
    wr = wins / n if n else 0.0
    total = sum(pnls)
    avg = total / n if n else 0.0
    avg_w = (sum(x for x in pnls if x > 0) / wins) if wins else 0.0
    avg_l = (sum(x for x in pnls if x <= 0) / losses) if losses else 0.0
    by_reason: dict[str, int] = {}
    by_coin: dict[str, float] = {}
    for r in settled:
        reason = str(r.get("settle_reason") or "?")[:32]
        by_reason[reason] = by_reason.get(reason, 0) + 1
        coin = str(r.get("coin") or "?")
        by_coin[coin] = by_coin.get(coin, 0.0) + float(r.get("pnl_usd") or 0)

    if milestone <= 20:
        verdict = (
            "1차 표본 도달 — min_fill·지갑 모수 큰 변경 전 성과 방향만 확인하세요."
        )
    elif milestone <= 30:
        verdict = (
            "재평가 구간 — $3k min_fill / 12지갑 유지 vs 조정 여부를 결정해도 됩니다."
        )
    else:
        verdict = (
            "라이브 검토 후보 구간 — paper EV·DD·지갑별 편차 보고 "
            "초소액 LIVE 여부를 논의해도 됩니다 (자동 전환 없음)."
        )

    lines = [
        f"📊 <b>[HL Paper 표본 마일스톤 n≥{milestone}]</b> — "
        f"{datetime.now(KST).strftime('%m/%d %H:%M KST')}",
        "",
        "⏰ 까먹지 말라고 보내는 <b>1회성</b> 리마인드입니다.",
        f"정산 <b>{n}</b>건 도달 (목표 게이트 {milestone})",
        "",
        f"v2 bankroll ${float((state.get('policy_bankrolls') or {}).get(POLICY) or 1000):.2f} | "
        f"WR {wr:.0%} ({wins}W/{losses}L) | 누적 PnL <b>${total:+.2f}</b>",
        f"건당 기댓값 ${avg:+.3f} | avgW ${avg_w:+.2f} | avgL ${avg_l:+.2f}",
        f"설정: taker open 합산 ${p['min_aggregate_open_notional_usd']:,.0f}+ | "
        f"카피 ${p['copy_notional_usd']:.0f} | 지갑 {len(_wallets(cfg))}개",
        "",
        f"📌 {escape(verdict)}",
    ]
    if by_reason:
        lines.append("")
        lines.append("정산 사유:")
        for reason, cnt in sorted(by_reason.items(), key=lambda x: -x[1])[:6]:
            lines.append(f"• {escape(reason)} ×{cnt}")
    if by_coin:
        top = sorted(by_coin.items(), key=lambda x: -x[1])[:5]
        weak = sorted(by_coin.items(), key=lambda x: x[1])[:3]
        lines.append("")
        lines.append("코인별 PnL (상위):")
        for c, v in top:
            lines.append(f"• {escape(c)} ${v:+.2f}")
        if weak and weak[0][1] < 0:
            lines.append("코인별 PnL (하위):")
            for c, v in weak:
                lines.append(f"• {escape(c)} ${v:+.2f}")
    lines += [
        "",
        "다음 액션 예:",
        "• 텔레에 「HL paper 리뷰」라고 하면 같이 판단",
        "• 모수 갱신: tools/hl_whale_screen.py --from-leaderboard",
        "• LIVE 전환은 사람 승인 후에만 (이 알림이 자동 켜지 않음)",
        "",
        "※ paper only · 같은 마일스톤은 다시 안 보냄",
    ]
    return "\n".join(lines)


def _maybe_send_sample_milestones(state: dict, cfg: dict) -> list[int]:
    """정산 건수가 마일스톤에 도달하면 1회씩 TG 발송. 발송된 milestone 리스트 반환."""
    settled = _read_journal_settled(POLICY)
    n = len(settled)
    sent_map = state.setdefault("sample_milestones_sent", {})
    fired: list[int] = []
    for m in SAMPLE_MILESTONES:
        key = f"{POLICY}:n{m}"
        if n < m:
            continue
        if sent_map.get(key):
            continue
        msg = build_sample_milestone_report(state, cfg, m)
        if send_review(msg):
            sent_map[key] = {
                "sent_at": _now_kst(),
                "settled_n": n,
                "bankroll": state.get("bankroll"),
            }
            fired.append(m)
            print(f"  [HL-paper] sample milestone n>={m} report sent (settled={n})")
        else:
            print(f"  [HL-paper] sample milestone n>={m} TG 실패 — 다음 주기에 재시도")
            break  # 실패 시 순서 유지하며 재시도
    return fired


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
    # 정산 누적 후 표본 마일스톤 (20/30/50) 1회 리마인드
    milestones = _maybe_send_sample_milestones(state, cfg)
    n_settled = len(_read_journal_settled(POLICY))
    state["last_scan"] = {
        "time": _now_kst(),
        "signals": len(signals),
        "opened": opened,
        "settled": settled,
        "wallets": len(wallets),
        "settled_total": n_settled,
        "milestones_fired": milestones,
    }
    interval = float(_params(cfg).get("report_interval_seconds") or 4 * 3600)
    reported = False
    if report_now or (_now() - float(state.get("last_report_time") or 0) >= interval):
        if send_review(build_report(state, cfg)):
            state["last_report_time"] = _now()
            reported = True
    _save_state(state)
    return {
        "ok": True,
        "account": "hl_whale_paper",
        "wallets": len(wallets),
        "signals": len(signals),
        "opened": opened,
        "settled": settled,
        "settled_total": n_settled,
        "open_positions": len(state.get("open_positions") or []),
        "bankroll": state.get("bankroll"),
        "reported": reported,
        "milestones_fired": milestones,
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
