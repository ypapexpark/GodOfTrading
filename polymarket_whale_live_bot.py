#!/usr/bin/env python3
"""Polymarket 고래 카피 — 초소액 LIVE 경로.

paper 봇과 동일 시그널(scan_wallets)을 쓰되:
  - 상태/저널은 *_live_* 파일로 분리
  - 기본은 dry-run (POLYMARKET_LIVE_TRADING_ENABLED 필요)
  - 동시 포지션·단건·일손실 캡
  - 지갑 suspend 로직 공유

사용:
  python3 polymarket_whale_live_bot.py              # dry-run 또는 live(플래그 시)
  python3 polymarket_whale_live_bot.py --smoke
  python3 polymarket_whale_live_bot.py --report-now
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL.*", category=Warning)

from dotenv import load_dotenv
import requests

import polymarket_whale_paper_bot as paper
from polymarket_clob_exec import (
    get_wallet_address,
    get_usdc_balance_approx,
    live_enabled,
    place_buy_usd,
    place_sell_shares,
    smoke_test as clob_smoke,
)
from publisher import send_review

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

STATE_FILE = ROOT / "polymarket_whale_live_state.json"
JOURNAL_FILE = ROOT / "polymarket_whale_live_journal.jsonl"

# 현재 라이브 원금은 $1100, 티켓은 equity×2%와 $15 cap 중 작은 값이다.
# 합의정책은 1·2·3번째 고유 고래에 $10·$15·$20, 동일 시장 최대 $45다.
INITIAL_BANKROLL = float(os.getenv("POLYMARKET_LIVE_BANKROLL", "800") or 800)
BET_FRACTION = float(os.getenv("POLYMARKET_LIVE_BET_FRACTION", "0.02") or 0.02)
BET_USD_CAP = float(os.getenv("POLYMARKET_LIVE_BET_USD_CAP", "15") or 15)
# 0 = paper와 동일 무제한
MAX_OPEN = int(os.getenv("POLYMARKET_LIVE_MAX_OPEN", "0") or 0)
MAX_DAILY_LOSS = float(os.getenv("POLYMARKET_LIVE_MAX_DAILY_LOSS", "120") or 120)
COPY_SLIPPAGE = float(os.getenv("POLYMARKET_WHALE_COPY_SLIPPAGE", "0.03") or 0.03)
MIN_NET_USDC = float(os.getenv("POLYMARKET_WHALE_MIN_NET_USDC", "1000") or 1000)
# 지갑 장기 기대승률보다 진입가격이 최소 5%p 낮아야 카피한다.
# 0.999 같은 고가 추격은 한 번의 패배가 다수의 미세수익을 지우므로 차단한다.
MIN_ENTRY_EDGE = float(os.getenv("POLYMARKET_LIVE_MIN_ENTRY_EDGE", "0.05") or 0.05)
MAX_ENTRY_PRICE = float(os.getenv("POLYMARKET_LIVE_MAX_ENTRY_PRICE", "0.85") or 0.85)
MAX_COMMITTED_FRACTION = float(
    os.getenv("POLYMARKET_LIVE_MAX_COMMITTED_FRACTION", "0.40") or 0.40
)
# 텔레그램: paper와 동일 — 건당 즉시 알림 없이 주기 리포트만 (부담 방지)
REPORT_INTERVAL_SECONDS = int(os.getenv("POLYMARKET_LIVE_REPORT_INTERVAL", str(4 * 3600)) or (4 * 3600))
# paper/live가 동일한 청산 실험 정책을 공유한다.
HOLD_TO_RESOLUTION = paper.HOLD_TO_RESOLUTION


from bot_util import (  # noqa: E402
    KST,
    append_jsonl,
    env_bool,
    json_safe as _json_safe,
    now as _now,
    now_kst as _now_kst,
)

EDGE_FILTER_ENABLED = env_bool("POLYMARKET_LIVE_EDGE_FILTER_ENABLED", False)


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _append(row: dict) -> None:
    append_jsonl(JOURNAL_FILE, row)


def _load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if st.get("last_reset") != _today():
                st["daily_loss"] = 0.0
                st["last_reset"] = _today()
            # 시드 증액(예: 200→800) 시 내부 bankroll 상향 1회 — 단건이 $4로 줄어들던 문제 방지
            seeded = str(st.get("bankroll_env_seed") or "")
            if seeded != str(INITIAL_BANKROLL):
                cur = float(st.get("bankroll") or 0)
                if INITIAL_BANKROLL > cur:
                    st["bankroll"] = INITIAL_BANKROLL
                st["bankroll_env_seed"] = INITIAL_BANKROLL
            return st
        except Exception:
            pass
    # seed wallets from paper config
    paper_state = paper._load_state()
    return {
        "mode": "live" if live_enabled() else "dry_run",
        "wallets": paper_state.get("wallets") or {},
        "open_positions": [],
        "bankroll": INITIAL_BANKROLL,
        "bankroll_env_seed": INITIAL_BANKROLL,
        "daily_loss": 0.0,
        "last_reset": _today(),
        "last_report_time": 0.0,
        "last_scan": {},
        "orders_blocked": 0,
    }


def _ticket_usd(state: dict[str, Any] | None = None) -> float:
    """paper 비율 티켓. 일손실 한도 초과는 진입차단 대신 50% 소프트캡."""
    bank = float((state or {}).get("bankroll") or INITIAL_BANKROLL)
    base = min(max(bank, 0.0) * BET_FRACTION, BET_USD_CAP)
    if float((state or {}).get("daily_loss") or 0) >= MAX_DAILY_LOSS:
        base *= 0.5
    return round(base, 4)


def _save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(
        json.dumps(_json_safe(state), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _token_id_for_outcome(market: dict[str, Any], outcome_index: int) -> str | None:
    raw = market.get("clobTokenIds")
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        if ids and 0 <= outcome_index < len(ids):
            return str(ids[outcome_index])
    except Exception:
        return None
    return None


def _risk_ok(state: dict[str, Any], bet: float) -> tuple[bool, str]:
    # dry-run/shadow 포지션은 실주문 한도에 넣지 않음 (LIVE 전환 직후 막히던 문제)
    open_n = len([
        p for p in state.get("open_positions") or []
        if not p.get("is_shadow") and p.get("live") is True and not p.get("dry_run")
    ])
    # MAX_OPEN<=0 → paper와 동일하게 동시 한도 없음
    if MAX_OPEN > 0 and open_n >= MAX_OPEN:
        return False, f"동시 실포지션 {open_n}>={MAX_OPEN}"
    # 손실 뒤에도 초기 시드로 한도를 부풀리지 않고 실제 확정잔고 기준으로 축소한다.
    bank = float(state.get("bankroll") or 0)
    if bank <= 0:
        return False, "확정 bankroll 0 이하"
    if bet > bank * 0.25:
        return False, "단건이 bankroll 25% 초과"
    committed = sum(
        float(p.get("bet_usd") or 0)
        for p in state.get("open_positions") or []
        if not p.get("is_shadow") and p.get("live") is True and not p.get("dry_run")
    )
    if committed + bet > bank * MAX_COMMITTED_FRACTION:
        return False, (
            f"총투입 ${committed + bet:.2f} > bankroll "
            f"{MAX_COMMITTED_FRACTION:.0%} (${bank * MAX_COMMITTED_FRACTION:.2f})"
        )
    return True, ""


def _entry_edge_ok(state: dict[str, Any], sig: dict, entry_price: float) -> tuple[bool, str]:
    if not EDGE_FILTER_ENABLED:
        return True, ""
    wstate = (state.get("wallets") or {}).get(sig.get("wallet") or "") or {}
    expected = float(wstate.get("expected_win_rate") or 0.5)
    max_allowed = min(MAX_ENTRY_PRICE, expected - MIN_ENTRY_EDGE)
    if max_allowed <= 0 or entry_price > max_allowed:
        return False, (
            f"가격엣지 부족: 진입 {entry_price:.3f} > 허용 {max_allowed:.3f} "
            f"(지갑 기대승률 {expected:.1%} - 안전마진 {MIN_ENTRY_EDGE:.0%}p)"
        )
    return True, ""


def _live_early_exit(
    pos: dict[str, Any],
    state: dict[str, Any],
    *,
    reason: str,
    market: dict[str, Any] | None = None,
) -> bool:
    """고래 축소/플립 시 실매도(또는 dry) 후 로컬 정산."""
    dry = not live_enabled() or bool(pos.get("dry_run")) or bool(pos.get("is_shadow"))
    if market is None:
        market = paper._fetch_market_state(gamma_market_id=pos.get("gamma_market_id"))
    oidx = int(pos.get("outcome_index") or 0)
    price = paper._current_price(market, oidx) if market else None
    if price is None or price <= 0:
        price = float(pos.get("entry_price") or 0.5)
    exit_price = max(min(price * (1 - COPY_SLIPPAGE), 0.999), 0.001)
    entry = max(float(pos.get("entry_price") or 0.5), 1e-6)
    bet = float(pos.get("bet_usd") or _ticket_usd(state))
    shares = bet / entry

    token_id = pos.get("token_id") or (
        _token_id_for_outcome(market, oidx) if market else None
    )
    if not dry and not pos.get("is_shadow"):
        if not token_id:
            _append({
                "event": "order_failed", "error": "exit no token_id",
                "reason": reason, "at": _now_kst(), "title": pos.get("title"),
            })
            return False
        sell = place_sell_shares(
            str(token_id), shares, price_hint=exit_price, dry_run=False,
        )
        if not sell.get("ok"):
            _append({
                "event": "order_failed",
                "error": f"exit:{sell.get('error')}",
                "reason": reason,
                "at": _now_kst(),
                "title": pos.get("title"),
            })
            print(f"  [live-exit-fail] {sell.get('error')}")
            return False

    proceeds = shares * exit_price
    pnl = proceeds - bet
    result = {
        **pos,
        "event": "settled",
        "settled_at": _now_kst(),
        "settled_ts": _now(),
        "won": pnl > 0,
        "pnl_usd": round(pnl, 2),
        "pnl_pct": round(pnl / bet, 4) if bet else 0,
        "exit_price": round(exit_price, 4),
        "settle_reason": reason,
        "early_exit": True,
    }
    _append(result)
    if not pos.get("is_shadow"):
        state["bankroll"] = float(state.get("bankroll") or INITIAL_BANKROLL) + pnl
        if pnl < 0:
            state["daily_loss"] = float(state.get("daily_loss") or 0) + abs(pnl)
    wstate = state.get("wallets", {}).get(pos.get("wallet") or "")
    if isinstance(wstate, dict):
        key = f"{pos.get('condition_id')}:{pos.get('outcome_index')}"
        wstate.setdefault("signaled", {})[key] = False
        wstate["live_n"] = int(wstate.get("live_n") or 0) + 1
        if pnl > 0:
            wstate["live_wins"] = int(wstate.get("live_wins") or 0) + 1
        _update_wallet_status_live(str(pos.get("wallet")), wstate)
    print(f"  [live-exit] {reason} {(pos.get('title') or '')[:36]} pnl={pnl:+.2f}")
    return True


def follow_whale_exits_live(state: dict[str, Any]) -> int:
    """고래 net 축소 → 매도 추종."""
    if HOLD_TO_RESOLUTION:
        return 0

    remaining: list[dict] = []
    closed = 0
    thresh = paper.MIN_NET_USDC * paper.EXIT_NET_FRAC
    for pos in state.get("open_positions") or []:
        wstate = state.get("wallets", {}).get(pos.get("wallet") or "") or {}
        key = f"{pos.get('condition_id')}:{pos.get('outcome_index')}"
        net = float((wstate.get("net_usdc") or {}).get(key, 0.0))
        if net < thresh:
            ok = _live_early_exit(pos, state, reason="whale_exit_reduce")
            if ok:
                closed += 1
            else:
                remaining.append(pos)  # 매도 실패 시 유지 후 재시도
            continue
        remaining.append(pos)
    state["open_positions"] = remaining
    return closed


def open_live_positions(signals: list[dict], state: dict[str, Any]) -> int:
    opened = 0
    base_bet = _ticket_usd(state)
    dry = not live_enabled()

    for sig in signals:
        market = paper._gamma_market_by_condition(sig["condition_id"])
        if not market or market.get("closed"):
            continue

        existing_market = [
            pos for pos in state.get("open_positions") or []
            if pos.get("condition_id") == sig.get("condition_id")
        ]
        consensus_rank, consensus_wallets = paper.register_consensus_signal(state, sig)
        opposite_wallets = paper.opposite_consensus_wallets(state, sig)
        if opposite_wallets:
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked",
                "reason": "consensus_opposite_direction_conflict",
                "wallet": sig.get("wallet"),
                "condition_id": sig.get("condition_id"),
                "outcome_index": sig.get("outcome_index"),
                "consensus_wallets": consensus_wallets,
                "opposite_wallets": opposite_wallets,
                "at": _now_kst(),
                "title": market.get("question"),
            })
            continue
        if consensus_rank > paper.CONSENSUS_MAX_WHALES:
            continue
        same_direction_open = [
            pos for pos in existing_market
            if int(pos.get("outcome_index") or 0) == int(sig.get("outcome_index"))
        ]
        target_tickets = consensus_rank
        if len(same_direction_open) >= target_tickets:
            continue

        tier_scale = base_bet / BET_USD_CAP if BET_USD_CAP > 0 else 1.0
        bet = paper.consensus_bet_usd(consensus_rank, scale=tier_scale)
        if bet <= 0:
            continue
        ok, why = _risk_ok(state, bet)
        if not ok:
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked", "reason": why, "wallet": sig.get("wallet"),
                "at": _now_kst(), "title": sig.get("title"),
            })
            print(f"  [live-block] {why}")
            continue

        same_wallet_market = [
            pos for pos in state.get("open_positions") or []
            if pos.get("wallet") == sig.get("wallet")
            and pos.get("condition_id") == sig.get("condition_id")
        ]
        if HOLD_TO_RESOLUTION and same_wallet_market:
            # 최초 진입을 만기까지 유지한다. 같은 outcome 재매수와 반대 outcome
            # 플립 모두 무시해 중간 청산 및 양방향 노출을 막는다.
            if any(
                int(pos.get("outcome_index")) != int(sig.get("outcome_index"))
                for pos in same_wallet_market
            ):
                state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
                _append({
                    "event": "blocked",
                    "reason": "hold_to_resolution_ignore_whale_flip",
                    "wallet": sig.get("wallet"),
                    "condition_id": sig.get("condition_id"),
                    "at": _now_kst(),
                    "title": market.get("question"),
                })
            continue

        # 플립: 같은 지갑·마켓 반대 outcome 보유 시 먼저 청산
        still: list[dict] = []
        flip_exit_failed = False
        for pos in state.get("open_positions") or []:
            if (
                pos.get("wallet") == sig.get("wallet")
                and pos.get("condition_id") == sig.get("condition_id")
                and int(pos.get("outcome_index")) != int(sig.get("outcome_index"))
            ):
                if not _live_early_exit(
                    pos, state, reason="whale_flip_opposite", market=market
                ):
                    # 매도 실패 포지션을 로컬에서 지우면 실제 잔고가 고아가 되고,
                    # 반대편까지 신규 매수해 양쪽 노출이 생긴다. 유지 후 재시도한다.
                    still.append(pos)
                    flip_exit_failed = True
            else:
                still.append(pos)
        state["open_positions"] = still
        if flip_exit_failed:
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked",
                "reason": "flip_exit_failed_keep_position",
                "wallet": sig.get("wallet"),
                "condition_id": sig.get("condition_id"),
                "at": _now_kst(),
                "title": market.get("question"),
            })
            continue

        if any(
            p.get("wallet") == sig.get("wallet")
            and p.get("condition_id") == sig.get("condition_id")
            and int(p.get("outcome_index")) == int(sig.get("outcome_index"))
            for p in state.get("open_positions") or []
        ):
            continue

        price = paper._current_price(market, sig["outcome_index"])
        if price is None or price <= 0 or price >= 1:
            continue
        entry_price = min(price * (1 + COPY_SLIPPAGE), 0.999)
        edge_ok, edge_reason = _entry_edge_ok(state, sig, entry_price)
        if not edge_ok:
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked", "reason": edge_reason,
                "wallet": sig.get("wallet"), "at": _now_kst(),
                "title": market.get("question"), "entry_price": round(entry_price, 4),
            })
            print(f"  [live-block] {edge_reason}")
            continue
        is_shadow = state["wallets"].get(sig["wallet"], {}).get("status") == "suspended"
        token_id = _token_id_for_outcome(market, int(sig["outcome_index"]))

        order_res = None
        if not is_shadow:
            if not token_id:
                _append({"event": "blocked", "reason": "no token_id", "at": _now_kst()})
                continue
            order_res = place_buy_usd(
                token_id, bet, price_hint=entry_price, dry_run=dry,
            )
            if not order_res.get("ok"):
                _append({
                    "event": "order_failed",
                    "error": order_res.get("error"),
                    "token_id": token_id,
                    "bet_usd": bet,
                    "at": _now_kst(),
                    "title": market.get("question"),
                })
                print(f"  [order-fail] {order_res.get('error')}")
                continue

        actual_bet = float((order_res or {}).get("filled_usd") or bet)
        actual_shares = float(
            (order_res or {}).get("filled_shares")
            or (actual_bet / max(entry_price, 1e-9))
        )
        actual_entry = float(
            (order_res or {}).get("fill_price")
            or (actual_bet / max(actual_shares, 1e-9))
        )
        pos = {
            "wallet": sig["wallet"],
            "gamma_market_id": market.get("id"),
            "condition_id": sig["condition_id"],
            "outcome_index": sig["outcome_index"],
            "token_id": token_id,
            "title": market.get("question", sig.get("title")),
            "slug": sig.get("slug"),
            "entry_price": round(actual_entry, 6),
            "quote_entry_price": round(entry_price, 4),
            "bet_usd": round(actual_bet, 6),
            "consensus_rank": consensus_rank,
            "consensus_wallets": consensus_wallets,
            "signal_policy": "scaled_whale_consensus_v3",
            "shares_est": round(actual_shares, 6),
            "fill_status": (order_res or {}).get("fill_status"),
            "trade_ids": (order_res or {}).get("trade_ids") or [],
            "transaction_hashes": (order_res or {}).get("transaction_hashes") or [],
            "exit_policy": (
                "hold_to_resolution_v1" if HOLD_TO_RESOLUTION else "follow_whale_exit_v1"
            ),
            "is_shadow": is_shadow,
            "live": (not dry and not is_shadow),
            "dry_run": dry or is_shadow,
            "order_id": (order_res or {}).get("order_id"),
            "opened_at": _now_kst(),
            "opened_ts": _now(),
        }
        state["open_positions"].append(pos)
        _append({**pos, "event": "opened", "mode": "dry_run" if pos["dry_run"] else "live"})
        opened += 1
        tag = "DRY" if pos["dry_run"] else "LIVE"
        print(
            f"  [{tag}] open {pos['title'][:40]} "
            f"${actual_bet:.2f} @ {actual_entry:.3f}"
        )
    return opened


def settle_positions(state: dict[str, Any]) -> int:
    """paper와 동일 정산. live 포지션도 마켓 resolution 기준 paper PnL 추적
    (실제 USDC 잔고 동기화는 2단계에서 강화)."""
    # 임시로 paper settle 재사용 위해 journal 분리 — 로직 복제
    remaining = []
    settled = 0
    for pos in state.get("open_positions", []):
        market = paper._fetch_market_state(gamma_market_id=pos.get("gamma_market_id"))
        if not market:
            remaining.append(pos)
            continue
        winner_idx = paper._resolved_outcome(market)
        if winner_idx is None:
            mark = paper._current_price(market, int(pos.get("outcome_index") or 0))
            if mark is not None and mark > 0:
                pos["mark_price"] = round(float(mark), 4)
                pos["mark_updated_at"] = _now_kst()
            remaining.append(pos)
            continue
        won = winner_idx == pos["outcome_index"]
        payout = pos["bet_usd"] / pos["entry_price"] if won else 0.0
        pnl = payout - pos["bet_usd"]
        is_shadow = pos.get("is_shadow", False)
        result = {
            **pos,
            "event": "settled",
            "settled_at": _now_kst(),
            "won": won,
            "pnl_usd": round(pnl, 2),
            "pnl_pct": round(pnl / pos["bet_usd"], 4) if pos["bet_usd"] else 0,
        }
        _append(result)
        if not is_shadow:
            state["bankroll"] = float(state.get("bankroll") or INITIAL_BANKROLL) + pnl
            if pnl < 0:
                state["daily_loss"] = float(state.get("daily_loss") or 0) + abs(pnl)
        settled += 1
        wstate = state["wallets"].get(pos["wallet"])
        if wstate is not None:
            wstate["live_n"] = wstate.get("live_n", 0) + 1
            wstate["live_wins"] = wstate.get("live_wins", 0) + (1 if won else 0)
            _update_wallet_status_live(pos["wallet"], wstate)
    state["open_positions"] = remaining
    return settled


def _update_wallet_status_live(wallet: str, wstate: dict[str, Any]) -> None:
    """paper._update_wallet_status 와 동일 기준, 저널은 live 파일."""
    n = wstate.get("live_n", 0)
    if n < paper.SUSPEND_MIN_SETTLED:
        return
    z = paper._wallet_z(wstate)
    if z is None:
        return
    if wstate.get("status") == "active" and z <= paper.SUSPEND_Z:
        wstate["status"] = "suspended"
        wstate["suspended_at"] = _now_kst()
        wstate["suspended_reason"] = (
            f"누적 {n}건 z={z:.2f} (기준 {paper.SUSPEND_Z}) — live 추종 자동중단"
        )
        _append({
            "event": "wallet_suspended", "wallet": wallet, "z": round(z, 2),
            "reason": wstate["suspended_reason"], "at": wstate["suspended_at"],
        })
        send_review(
            f"⛔ <b>[Poly Whale LIVE 지갑 중단]</b>\n"
            f"{escape(wallet[:14])}... z={z:.2f}"
        )
    elif wstate.get("status") == "suspended" and z >= paper.REACTIVATE_Z:
        wstate["status"] = "active"
        wstate["reactivated_at"] = _now_kst()
        _append({
            "event": "wallet_reactivated", "wallet": wallet, "z": round(z, 2),
            "at": wstate["reactivated_at"],
        })



def _summarize_actual_positions(rows: list[dict]) -> dict[str, Any]:
    active = [
        r for r in rows
        if float(r.get("size", 0) or 0) > 0
        and float(r.get("currentValue", 0) or 0) > 0.01
    ]
    pnl_values = [float(r.get("cashPnl", 0) or 0) for r in active]
    return {
        "ok": True,
        "count": len(active),
        "invested": sum(float(r.get("initialValue", 0) or 0) for r in active),
        "value": sum(float(r.get("currentValue", 0) or 0) for r in active),
        "unrealized": sum(pnl_values),
        "profit": sum(v for v in pnl_values if v > 0),
        "loss": sum(v for v in pnl_values if v < 0),
        "profit_count": sum(v > 0.005 for v in pnl_values),
        "loss_count": sum(v < -0.005 for v in pnl_values),
        "flat_count": sum(abs(v) <= 0.005 for v in pnl_values),
        "positions": sorted(
            active,
            key=lambda r: abs(float(r.get("cashPnl", 0) or 0)),
            reverse=True,
        ),
    }


def _journal_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not JOURNAL_FILE.exists():
        return rows
    for line in JOURNAL_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _bot_token_ids(rows: list[dict[str, Any]] | None = None) -> set[str]:
    return {
        str(r.get("token_id"))
        for r in (rows if rows is not None else _journal_rows())
        if r.get("event") == "opened"
        and r.get("live") is True
        and not r.get("dry_run")
        and r.get("token_id")
    }


def _fetch_actual_portfolio(token_ids: set[str] | None = None) -> dict[str, Any]:
    """Public Data API portfolio; report-only and never signs or places orders."""
    wallet = get_wallet_address()
    if not wallet:
        return {"ok": False, "error": "wallet address unavailable"}
    try:
        resp = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": wallet, "sizeThreshold": 0},
            timeout=20,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list):
            raise ValueError("positions response is not a list")
        if token_ids is not None:
            rows = [r for r in rows if str(r.get("asset") or "") in token_ids]
        return _summarize_actual_positions(rows)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:180]}


def _summarize_actual_closed(rows: list[dict], token_ids: set[str]) -> dict[str, Any]:
    selected = [r for r in rows if str(r.get("asset") or "") in token_ids]
    pnl_values = [float(r.get("realizedPnl", 0) or 0) for r in selected]
    today_loss = 0.0
    for row, pnl in zip(selected, pnl_values):
        if pnl >= 0:
            continue
        try:
            closed_day = datetime.fromtimestamp(float(row.get("timestamp") or 0), KST).strftime("%Y-%m-%d")
        except Exception:
            continue
        if closed_day == _today():
            today_loss += abs(pnl)
    return {
        "ok": True,
        "count": len(selected),
        "wins": sum(v > 0.005 for v in pnl_values),
        "losses": sum(v < -0.005 for v in pnl_values),
        "flat": sum(abs(v) <= 0.005 for v in pnl_values),
        "realized": sum(pnl_values),
        "profit": sum(v for v in pnl_values if v > 0),
        "loss": sum(v for v in pnl_values if v < 0),
        "today_loss": today_loss,
        "positions": selected,
    }


def _fetch_actual_closed(token_ids: set[str]) -> dict[str, Any]:
    wallet = get_wallet_address()
    if not wallet or not token_ids:
        return {"ok": False, "error": "wallet/token registry unavailable"}
    try:
        rows: list[dict] = []
        for offset in range(0, 500, 50):
            resp = requests.get(
                "https://data-api.polymarket.com/closed-positions",
                params={
                    "user": wallet,
                    "limit": 50,
                    "offset": offset,
                    "sortBy": "TIMESTAMP",
                    "sortDirection": "DESC",
                },
                timeout=20,
            )
            resp.raise_for_status()
            page = resp.json()
            if not isinstance(page, list):
                raise ValueError("closed positions response is not a list")
            rows.extend(page)
            if len(page) < 50:
                break
        return _summarize_actual_closed(rows, token_ids)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:180]}


def _sync_actual_accounting(state: dict[str, Any]) -> dict[str, Any]:
    """실제 현금+포지션 평가액으로 총자산 및 원금 대비 손익을 재기준화."""
    token_ids = _bot_token_ids()
    closed = _fetch_actual_closed(token_ids)
    if not closed.get("ok"):
        return closed
    portfolio = _fetch_actual_portfolio(token_ids)
    cash = get_usdc_balance_approx()
    if not portfolio.get("ok") or cash < 0:
        return {"ok": False, "error": "actual cash/portfolio unavailable"}
    realized = float(closed.get("realized") or 0)
    previous = (state.get("actual_accounting") or {}).get("realized")
    position_value = float(portfolio.get("value") or 0)
    equity = cash + position_value
    all_time_pnl = equity - INITIAL_BANKROLL
    state["bankroll"] = equity
    state["daily_loss"] = float(closed.get("today_loss") or 0)
    state["actual_accounting"] = {
        k: v for k, v in closed.items() if k not in {"ok", "positions"}
    } | {
        "principal": INITIAL_BANKROLL,
        "cash": cash,
        "position_value": position_value,
        "equity": equity,
        "all_time_pnl": all_time_pnl,
        "synced_at": _now_kst(),
    }
    if previous is None or abs(float(previous) - realized) > 0.005:
        _append({
            "event": "accounting_reconciled",
            "at": _now_kst(),
            "actual_realized_pnl": round(realized, 6),
            "actual_closed": int(closed.get("count") or 0),
            "equity": round(equity, 6),
            "all_time_pnl": round(all_time_pnl, 6),
        })
    return closed


def _local_open_summary(state: dict[str, Any]) -> dict[str, float]:
    positions = [
        p for p in state.get("open_positions") or []
        if not p.get("is_shadow") and p.get("live") is True and not p.get("dry_run")
    ]
    invested = sum(float(p.get("bet_usd") or 0) for p in positions)
    pnl_values = []
    for pos in positions:
        entry = max(float(pos.get("entry_price") or 0), 1e-9)
        mark = float(pos.get("mark_price") or entry)
        shares = float(pos.get("shares_est") or (float(pos.get("bet_usd") or 0) / entry))
        pnl_values.append(shares * mark - float(pos.get("bet_usd") or 0))
    return {
        "count": len(positions),
        "invested": invested,
        "value": invested + sum(pnl_values),
        "unrealized": sum(pnl_values),
        "profit": sum(v for v in pnl_values if v > 0),
        "loss": sum(v for v in pnl_values if v < 0),
        "profit_count": sum(v > 0.005 for v in pnl_values),
        "loss_count": sum(v < -0.005 for v in pnl_values),
        "flat_count": sum(abs(v) <= 0.005 for v in pnl_values),
        "positions": positions,
    }


def _paper_comparison(live_rows: list[dict[str, Any]]) -> dict[str, float]:
    paper_rows = paper._read_jsonl(paper.JOURNAL_FILE)
    paper_settled = [
        r for r in paper_rows
        if r.get("event") == "settled" and not r.get("is_shadow")
    ]
    live_open_ts = [
        float(r.get("opened_ts") or 0)
        for r in live_rows
        if r.get("event") == "opened" and r.get("live") is True
    ]
    start = min(live_open_ts) if live_open_ts else 0.0
    same_period = [
        r for r in paper_settled
        if float(r.get("opened_ts") or 0) >= start
    ]
    all_pnl = sum(float(r.get("pnl_usd") or 0) for r in paper_settled)
    same_pnl = sum(float(r.get("pnl_usd") or 0) for r in same_period)
    same_bet = sum(float(r.get("bet_usd") or 0) for r in same_period)
    largest = max(
        (float(r.get("pnl_usd") or 0) for r in paper_settled),
        default=0.0,
    )
    return {
        "all_count": len(paper_settled),
        "all_pnl": all_pnl,
        "same_count": len(same_period),
        "same_pnl": same_pnl,
        "same_roi": same_pnl / same_bet if same_bet else 0.0,
        "largest_win": largest,
        "all_without_largest": all_pnl - largest,
    }


def build_report(state: dict[str, Any]) -> str:
    from polymarket_whale_insights import build_insight_comments

    mode = "LIVE" if live_enabled() else "DRY-RUN"
    rows = _journal_rows()
    settled = [r for r in rows if r.get("event") == "settled" and not r.get("is_shadow")]
    fails = [r for r in rows if r.get("event") == "order_failed"]
    blocked = [r for r in rows if r.get("event") == "blocked"]
    wins = [r for r in settled if r.get("won")]
    losses = [r for r in settled if not r.get("won")]
    pnl = sum(float(r.get("pnl_usd") or 0) for r in settled)
    gross_profit = sum(max(float(r.get("pnl_usd") or 0), 0.0) for r in settled)
    gross_loss = sum(min(float(r.get("pnl_usd") or 0), 0.0) for r in settled)
    wr = len(wins) / len(settled) if settled else 0.0
    bank = float(state.get("bankroll") or INITIAL_BANKROLL)
    bet = _ticket_usd(state)
    tier_scale = bet / BET_USD_CAP if BET_USD_CAP > 0 else 1.0
    local_port = _local_open_summary(state)
    token_ids = _bot_token_ids(rows)
    actual_port = _fetch_actual_portfolio(token_ids)
    actual_closed = _fetch_actual_closed(token_ids)
    policy_token_ids = {
        str(r.get("token_id"))
        for r in rows
        if r.get("event") == "opened"
        and r.get("live") is True
        and r.get("exit_policy") == "hold_to_resolution_v1"
        and r.get("token_id")
    }
    actual_policy = _fetch_actual_closed(policy_token_ids)
    paper_cmp = _paper_comparison(rows)
    port = actual_port if actual_port.get("ok") else local_port
    if actual_closed.get("ok"):
        display_n = int(actual_closed.get("count") or 0)
        display_wins = int(actual_closed.get("wins") or 0)
        display_losses = int(actual_closed.get("losses") or 0)
        display_pnl = float(actual_closed.get("realized") or 0)
        display_profit = float(actual_closed.get("profit") or 0)
        display_loss = float(actual_closed.get("loss") or 0)
    else:
        display_n = len(settled)
        display_wins = len(wins)
        display_losses = len(losses)
        display_pnl = pnl
        display_profit = gross_profit
        display_loss = gross_loss
    display_wr = display_wins / display_n if display_n else 0.0
    accounting = state.get("actual_accounting") or {}
    cash_est = float(accounting.get("cash") or (bank - float(port.get("value") or 0)))
    # 메시지 안의 현금 + 방금 조회한 평가액이 총자산과 항상 일치해야 한다.
    # accounting.equity는 직전 스캔 값이라 가격 변동 뒤에는 몇 달러 어긋날 수 있다.
    equity_est = cash_est + float(port.get("value") or 0)
    all_time_pnl = equity_est - INITIAL_BANKROLL
    sync_note = ""
    if actual_port.get("ok") and int(actual_port.get("count") or 0) != int(local_port["count"]):
        sync_note = (
            f"⚠️ 실지갑 {actual_port['count']}건 ≠ 로컬 {int(local_port['count'])}건 — "
            "청산/정산 동기화 점검 중"
        )

    lines = [
        f"🐋 <b>[Polymarket 고래 카피 {mode}]</b> — {datetime.now(KST).strftime('%m/%d %H:%M KST')}",
        "",
        "💼 <b>현재 포트폴리오</b>",
        f"• 오픈 <b>{int(port.get('count') or 0)}건</b> "
        f"(수익중 {int(port.get('profit_count') or 0)} / "
        f"손실중 {int(port.get('loss_count') or 0)} / "
        f"보합 {int(port.get('flat_count') or 0)})",
        f"• 투입 <b>${float(port.get('invested') or 0):.2f}</b> → "
        f"평가 <b>${float(port.get('value') or 0):.2f}</b> | "
        f"미실현 <b>${float(port.get('unrealized') or 0):+.2f}</b>",
        f"• 미실현 수익 <b>+${float(port.get('profit') or 0):.2f}</b> | "
        f"미실현 손실 <b>-${abs(float(port.get('loss') or 0)):.2f}</b>",
        f"• 실현 가용 ${cash_est:.2f} | 현재 총자산 <b>${equity_est:.2f}</b>",
        "",
        "📊 <b>실지갑 자산 성과</b>",
        f"• 원금 ${INITIAL_BANKROLL:.2f} 대비 총손익 <b>${all_time_pnl:+.2f}</b> "
        f"({all_time_pnl / INITIAL_BANKROLL:+.1%})",
        f"• 상환·매도 완료 API 표본 {display_n}건: "
        f"{display_wins}수익 / {display_losses}손실 | PnL ${display_pnl:+.2f}",
        "• 위 표본은 가치 0인 미상환 패배 토큰을 누락할 수 있어 전체 승패로 사용하지 않음",
        f"• 현재 미실현 <b>${float(port.get('unrealized') or 0):+.2f}</b> | "
        f"오늘 확정손실 ${float(state.get('daily_loss') or 0):.2f}",
        f"• 로컬 구장부(참고): {len(settled)}건 PnL ${pnl:+.2f} — 미체결 포함으로 폐기 예정",
        "",
        "🧪 <b>페이퍼 비교</b>",
        f"• 전체 {int(paper_cmp['all_count'])}건 PnL ${paper_cmp['all_pnl']:+.2f}",
        f"• 라이브 시작 후 동기간 {int(paper_cmp['same_count'])}건 "
        f"PnL ${paper_cmp['same_pnl']:+.2f} (ROI {paper_cmp['same_roi']:+.1%})",
        f"• 최대 1건 ${paper_cmp['largest_win']:+.2f} | "
        f"그 1건 제외 전체 ${paper_cmp['all_without_largest']:+.2f}",
        "",
        "⚙️ <b>운영 상태</b>",
        f"• 로컬 오픈 {int(local_port['count'])}건 | "
        f"수정 후 차단 {max(len(blocked) - int((state.get('execution_baseline') or {}).get('blocked', 0)), 0)} | "
        f"주문실패 {max(len(fails) - int((state.get('execution_baseline') or {}).get('fails', 0)), 0)}",
        "• 청산정책: "
        + ("최초 진입 후 시장 결과까지 보유" if HOLD_TO_RESOLUTION else "고래 축소·플립 추종"),
        f"• 진입정책: 1명째 ${paper.consensus_bet_usd(1, tier_scale):.2f} | "
        f"2명째 +${paper.consensus_bet_usd(2, tier_scale):.2f} | "
        f"3명째 +${paper.consensus_bet_usd(3, tier_scale):.2f} | 시장 최대 "
        f"${sum(paper.consensus_bet_usd(rank, tier_scale) for rank in (1, 2, 3)):.2f}",
        "• 중복·충돌: 동일 고래 반복 및 반대 방향 고래 신호 차단",
        (
            f"• 신규 보유정책 상환확인 표본: {int(actual_policy.get('count') or 0)}건 "
            f"({int(actual_policy.get('wins') or 0)}승/"
            f"{int(actual_policy.get('losses') or 0)}패) | "
            f"PnL ${float(actual_policy.get('realized') or 0):+.2f}"
            if actual_policy.get("ok")
            else "• 신규 보유정책 실체결: 공개 정산내역 조회 실패"
        ),
        f"• 추가조건: paper 동일 신호"
        + (f" + 가격엣지 필터 {MIN_ENTRY_EDGE:.0%}p" if EDGE_FILTER_ENABLED else " (가격엣지 필터 OFF)"),
        f"• 총투입≤확정 bankroll {MAX_COMMITTED_FRACTION:.0%} | "
        f"일손실 ${MAX_DAILY_LOSS:.0f} 초과 시 단건 50% 축소",
    ]
    if sync_note:
        lines.append(sync_note)
    if not actual_port.get("ok"):
        lines.append("ℹ️ 실지갑 조회 실패 — 로컬 마크 기준 추정치 표시")

    actual_rows = actual_port.get("positions") or []
    if actual_rows:
        lines += ["", "🔎 <b>변동 큰 오픈 포지션</b>"]
        for pos in actual_rows[:5]:
            pp = float(pos.get("cashPnl", 0) or 0)
            icon = "🟢" if pp >= 0 else "🔴"
            title = escape(str(pos.get("title") or "?"))[:46]
            outcome = escape(str(pos.get("outcome") or "?"))[:18]
            lines.append(
                f"{icon} {title} / {outcome} | ${pp:+.2f} "
                f"(${float(pos.get('currentValue', 0) or 0):.2f})"
            )
    try:
        cfg_whales = paper._load_config().get("whales") or []
    except Exception:
        cfg_whales = []
    token_wallet = {
        str(r.get("token_id")): str(r.get("wallet") or "?")
        for r in rows
        if r.get("event") == "opened" and r.get("token_id")
    }
    actual_settled_for_insights = [
        {
            "wallet": token_wallet.get(str(r.get("asset") or ""), "?"),
            "pnl_usd": float(r.get("realizedPnl") or 0),
            "won": float(r.get("realizedPnl") or 0) > 0,
        }
        for r in (actual_closed.get("positions") or [])
    ]
    comments = build_insight_comments(
        mode=mode,
        state=state,
        settled=actual_settled_for_insights,
        config_whales=cfg_whales,
        order_failed=fails[int((state.get("execution_baseline") or {}).get("fails", 0)):],
        blocked=blocked[int((state.get("execution_baseline") or {}).get("blocked", 0)):],
        bet_fraction=BET_FRACTION,
        max_open=MAX_OPEN,
    )
    lines.append("")
    lines.append("💡 <b>개선·건의 코멘트</b> (자동 제안 · config 자동수정 없음)")
    for c in comments:
        lines.append(f"• {escape(c)}")

    lines += [
        "",
        "※ 모수 추가: 후보 지갑 → paper 관찰 → config whales[] 반영 → live.",
        "※ 건당 TG 없음 · 주기 리포트만 (paper와 동일 운용).",
    ]
    return "\n".join(lines)


def _ensure_wallets_from_config(state: dict[str, Any]) -> None:
    """config 9지갑이 state에 없으면 추가 (last_seen 은 0부터 — 과거 신호 폭주 방지 위해
    paper state 의 last_seen 을 가져와 동기화)."""
    cfg = paper._load_config()
    paper_st = paper._load_state()
    for w in cfg.get("whales") or []:
        addr = w.get("wallet")
        if not addr:
            continue
        if addr not in state["wallets"]:
            src = (paper_st.get("wallets") or {}).get(addr) or {}
            state["wallets"][addr] = {
                "status": src.get("status", "active"),
                "expected_win_rate": w.get("expected_win_rate", 0.5),
                "last_seen_ts": int(src.get("last_seen_ts") or 0),
                "net_usdc": dict(src.get("net_usdc") or {}),
                "signaled": dict(src.get("signaled") or {}),
                "live_wins": int(src.get("live_wins") or 0),
                "live_n": int(src.get("live_n") or 0),
            }


def run_once(report_now: bool = False) -> dict[str, Any]:
    # paper 모듈 상수/설정 정렬
    paper.MIN_NET_USDC = MIN_NET_USDC
    paper.COPY_SLIPPAGE = COPY_SLIPPAGE

    state = _load_state()
    _ensure_wallets_from_config(state)
    state["mode"] = "live" if live_enabled() else "dry_run"
    policy_name = (
        "hold_to_resolution_v1" if HOLD_TO_RESOLUTION else "follow_whale_exit_v1"
    )
    if state.get("exit_policy") != policy_name:
        state["exit_policy"] = policy_name
        state["exit_policy_started_at"] = _now_kst()
    if state.get("execution_policy") != "matched_only_v1":
        existing_rows = _journal_rows()
        state["execution_policy"] = "matched_only_v1"
        state["execution_policy_started_at"] = _now_kst()
        state["execution_baseline"] = {
            "fails": sum(r.get("event") == "order_failed" for r in existing_rows),
            "blocked": sum(r.get("event") == "blocked" for r in existing_rows),
        }
    state["signal_policy"] = "scaled_whale_consensus_v3"

    settled = settle_positions(state)
    actual_accounting = _sync_actual_accounting(state)
    signals = paper.scan_wallets(state)
    whale_exits = follow_whale_exits_live(state)
    opened = open_live_positions(signals, state)

    report_due = report_now or (
        _now() - float(state.get("last_report_time") or 0) >= REPORT_INTERVAL_SECONDS
    )
    # 주문 뒤 현금이 달라질 수 있으므로 텔레그램 직전에 실지갑을 한 번 더 동기화한다.
    if report_due:
        actual_accounting = _sync_actual_accounting(state)

    state["last_scan"] = {
        "time": _now_kst(),
        "signals": len(signals),
        "opened": opened,
        "settled": settled,
        "whale_exits": whale_exits,
        "exit_policy": policy_name,
        "signal_policy": state["signal_policy"],
        "consensus_candidates": len(state.get("consensus_candidates") or {}),
        "actual_accounting_ok": bool(actual_accounting.get("ok")),
        "mode": state["mode"],
        "wallets": len(state.get("wallets") or {}),
        "live_flag": live_enabled(),
    }
    # paper와 동일: 건당 TG 없이 주기 리포트만 (기본 4h)
    if report_due:
        if send_review(build_report(state)):
            state["last_report_time"] = _now()
    _save_state(state)
    return {
        "mode": state["mode"],
        "signals": len(signals),
        "opened": opened,
        "settled": settled,
        "open_positions": len(state.get("open_positions") or []),
        "bankroll": state.get("bankroll"),
        "live_enabled": live_enabled(),
        "bet_usd": _ticket_usd(state),
        "bet_fraction": BET_FRACTION,
        "actual_realized_pnl": (
            actual_accounting.get("realized") if actual_accounting.get("ok") else None
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--report-now", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.smoke:
        info = clob_smoke()
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0 if info.get("client_ok") or not live_enabled() else 1

    result = run_once(report_now=args.report_now)
    if args.json:
        print(json.dumps(_json_safe(result), ensure_ascii=False))
    else:
        print(f"[PolymarketWhaleLive] {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
