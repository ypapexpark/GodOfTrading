#!/usr/bin/env python3
from __future__ import annotations

"""
Polymarket 고래 지갑 카피트레이딩 paper bot.

PolyBacktest(/Users/ghp/Projects/PolyBacktest)에서 통계적으로 유의미한 트랙레코드를 가진 것으로
확인된 지갑 9명(polymarket_whale_config.json 스냅샷)의 신규 포지션을 감시해서, 실주문 없이
모의매매로 결과를 누적한다.

Runs a read-only simulation:
  1. 추적 지갑들의 신규 체결(trade)을 data-api.polymarket.com/activity에서 폴링,
  2. 지갑별 마켓당 순포지션이 최소 규모를 새로 넘으면 "카피 신호"로 판정,
  3. 슬리피지를 반영한 가상 진입가로 paper position을 기록,
  4. 마켓 종료 후 Gamma API로 결과를 확인해 정산,
  5. 지갑별 롤링 성과가 백테스트 트랙레코드보다 유의미하게 나빠지면 그 지갑 추종을 자동 중단,
  6. 주기적으로 send_review()로 텔레그램 리포트 전송.

No wallet, API key, signing, or live Polymarket order placement is used.
"""

import argparse
import json
import math
import os
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)

import requests
from dotenv import load_dotenv

from publisher import send_review

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "polymarket_whale_paper_state.json"
JOURNAL_FILE = ROOT / "polymarket_whale_paper_journal.jsonl"
CONFIG_FILE = ROOT / "polymarket_whale_config.json"

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

load_dotenv(ROOT / ".env")

from bot_util import (  # noqa: E402
    KST,
    append_jsonl as _append_jsonl,
    env_bool as _env_bool,
    env_float as _env_float,
    env_int as _env_int,
    json_safe as _json_safe,
    load_json,
    now as _now,
    now_kst as _now_kst,
    read_jsonl as _read_jsonl,
    save_json,
)

INITIAL_BANKROLL = _env_float("POLYMARKET_WHALE_INITIAL_BANKROLL", 1000.0)
BET_FRACTION = _env_float("POLYMARKET_WHALE_BET_FRACTION", 0.02)
COPY_SLIPPAGE = _env_float("POLYMARKET_WHALE_COPY_SLIPPAGE", 0.03)
MIN_NET_USDC = _env_float("POLYMARKET_WHALE_MIN_NET_USDC", 1000.0)
# 고래 순매수가 이 비율×MIN 미만이면 청산 추종 (줄이거나 손절/플립 후 잔량)
EXIT_NET_FRAC = _env_float("POLYMARKET_WHALE_EXIT_NET_FRAC", 0.35)
# 고래의 뒤늦은 축소/반대 체결을 그대로 따라가면 카피 지연과 왕복 슬리피지가
# 겹친다. 기본값은 진입 신호만 카피하고 시장 결과 확정까지 보유한다.
HOLD_TO_RESOLUTION = _env_bool("POLYMARKET_WHALE_HOLD_TO_RESOLUTION", True)
REPORT_INTERVAL_SECONDS = _env_int("POLYMARKET_WHALE_REPORT_INTERVAL", 4 * 3600)
ACTIVITY_POLL_LIMIT = _env_int("POLYMARKET_WHALE_ACTIVITY_LIMIT", 100)
SUSPEND_MIN_SETTLED = _env_int("POLYMARKET_WHALE_SUSPEND_MIN_SETTLED", 8)
SUSPEND_Z = _env_float("POLYMARKET_WHALE_SUSPEND_Z", -2.5)
REACTIVATE_Z = _env_float("POLYMARKET_WHALE_REACTIVATE_Z", -1.0)
# 2026-07-13 한 달 paired 분석(CLOB 단일시장 조회, payload 639/639):
# - 단일 고래 621시장: WR 67.95%, PnL -$421.49 (평균 승 +$8.43 / 패 -$20)
# - 2번째 고래 티켓: n=40, ROI +12.57%
# - 3번째 고래 티켓: n=3, ROI +15.75% (소표본)
# - 반대방향 충돌 제외 후 2·3번째만: n=40, 37승, PnL +$130.01, ROI +16.25%
# 거래 빈도와 초저가 대박의 우측 꼬리를 유지하면서 합의가 늘수록 증액한다.
# 2026-07-13 사용자 확정: 1명째 $10, 2명째 +$15, 3명째 +$20 (시장 최대 $45).
CONSENSUS_MIN_WHALES = _env_int("POLYMARKET_WHALE_CONSENSUS_MIN", 1)
CONSENSUS_MAX_WHALES = _env_int("POLYMARKET_WHALE_CONSENSUS_MAX", 3)
CONSENSUS_TTL_SECONDS = _env_int(
    "POLYMARKET_WHALE_CONSENSUS_TTL_SECONDS", 7 * 24 * 3600
)
CONSENSUS_TIER_USD = {
    1: _env_float("POLYMARKET_WHALE_TIER1_USD", 10.0),
    2: _env_float("POLYMARKET_WHALE_TIER2_USD", 15.0),
    3: _env_float("POLYMARKET_WHALE_TIER3_USD", 20.0),
}
BET_USD = INITIAL_BANKROLL * BET_FRACTION


def consensus_bet_usd(consensus_rank: int, scale: float = 1.0) -> float:
    """합의 순번별 증분 사이즈. 라이브 soft-cap은 scale만 낮춘다."""
    base = float(CONSENSUS_TIER_USD.get(consensus_rank, 0.0))
    return round(base * max(float(scale), 0.0), 4)


def _load_config() -> dict[str, Any]:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def _load_state() -> dict[str, Any]:
    data = load_json(STATE_FILE, default=None)
    if isinstance(data, dict):
        return data
    config = _load_config()
    return {
        "wallets": {
            w["wallet"]: {
                "status": "active",
                "expected_win_rate": w["expected_win_rate"],
                "last_seen_ts": 0,
                "net_usdc": {},  # "{market_id}:{outcome_index}" -> 현재 관측된 순매수 USDC
                "signaled": {},  # 이미 카피 신호를 낸 포지션 키 (중복 신호 방지)
                "live_wins": 0,  # 라이브 추종 시작 이후 누적 승 (중단 중엔 그림자추적으로 계속 갱신)
                "live_n": 0,     # 라이브 추종 시작 이후 누적 정산 건수
            }
            for w in config["whales"]
        },
        "open_positions": [],
        "bankroll": INITIAL_BANKROLL,
        "last_report_time": 0.0,
        "last_scan": {},
    }


def _save_state(state: dict[str, Any]) -> None:
    save_json(STATE_FILE, state)


def _get_json(url: str, params: dict[str, Any], timeout: int = 15) -> Any:
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _fetch_wallet_activity(wallet: str, since_ts: int) -> list[dict[str, Any]]:
    rows = _get_json(f"{DATA_API}/activity", {"user": wallet, "limit": ACTIVITY_POLL_LIMIT})
    out = []
    for r in rows:
        if r.get("type") != "TRADE":
            continue
        ts = int(r.get("timestamp", 0) or 0)
        if ts <= since_ts:
            continue
        out.append(r)
    return out


def _fetch_market_state(condition_id: str | None = None, gamma_market_id: str | None = None) -> dict[str, Any] | None:
    if gamma_market_id:
        try:
            return _get_json(f"{GAMMA_API}/markets/{gamma_market_id}", {})
        except Exception:
            return None
    return None


def _current_price(market: dict[str, Any], outcome_index: int) -> float | None:
    try:
        prices = json.loads(market.get("outcomePrices", "[]"))
        return float(prices[outcome_index])
    except Exception:
        return None


def _resolved_outcome(market: dict[str, Any]) -> int | None:
    """종료된 마켓이면 승리한 outcome_index 반환, 아니면 None."""
    if not market.get("closed"):
        return None
    try:
        prices = [float(p) for p in json.loads(market.get("outcomePrices", "[]"))]
    except Exception:
        return None
    for idx, p in enumerate(prices):
        if p >= 0.95:
            return idx
    return None


def scan_wallets(state: dict[str, Any]) -> list[dict[str, Any]]:
    """추적 지갑들의 신규 체결을 확인하고, 새로 임계치를 넘은 포지션을 신호로 반환.

    중단(suspended)된 지갑도 스캔은 계속한다 — 실제 카피는 안 하지만 그림자추적으로
    성과가 회복되는지 계속 관찰해서 자동 재활성화 판단에 쓴다 (open_paper_positions에서
    is_shadow로 구분).
    """
    signals = []
    for wallet, wstate in state["wallets"].items():
        since_ts = int(wstate.get("last_seen_ts") or 0)
        try:
            activity = _fetch_wallet_activity(wallet, since_ts)
        except Exception as exc:
            wstate["last_error"] = str(exc)
            continue

        if not activity:
            continue

        activity.sort(key=lambda r: r.get("timestamp", 0))
        for r in activity:
            market_id = str(r.get("conditionId") or "")
            outcome_index = r.get("outcomeIndex")
            if market_id == "" or outcome_index is None:
                continue
            key = f"{market_id}:{outcome_index}"
            sign = 1 if r.get("side") == "BUY" else -1
            usdc = float(r.get("usdcSize") or (float(r.get("size", 0)) * float(r.get("price", 0))))
            net = wstate["net_usdc"].get(key, 0.0) + sign * usdc
            wstate["net_usdc"][key] = net
            wstate["last_seen_ts"] = max(wstate["last_seen_ts"], int(r.get("timestamp", 0)))

            # 순매수(BUY 우세)만 진입 — 순매도 큰 쪽은 롱 카피 안 함
            if net >= MIN_NET_USDC and not wstate["signaled"].get(key):
                wstate["signaled"][key] = True
                signals.append({
                    "wallet": wallet,
                    "gamma_market_id": None,  # conditionId만으로는 gamma id를 못 구해 아래서 별도 조회
                    "condition_id": market_id,
                    "outcome_index": outcome_index,
                    "net_usdc": net,
                    "title": r.get("title", ""),
                    "slug": r.get("slug", ""),
                    "detected_at": _now_kst(),
                    "detected_ts": _now(),
                    "kind": "enter",
                })
    return signals


def _gamma_market_by_condition(condition_id: str) -> dict[str, Any] | None:
    try:
        rows = _get_json(f"{GAMMA_API}/markets", {"condition_ids": condition_id})
        return rows[0] if rows else None
    except Exception:
        return None


def _pos_key(pos: dict[str, Any]) -> str:
    return f"{pos.get('condition_id')}:{pos.get('outcome_index')}"


def register_consensus_signal(
    state: dict[str, Any], sig: dict[str, Any]
) -> tuple[int, list[str]]:
    """시장·방향별 고유 고래 합의를 누적하고 현재 합의 순번을 반환."""
    now_ts = _now()
    condition = str(sig.get("condition_id") or "")
    outcome = int(sig.get("outcome_index") or 0)
    key = f"{condition}:{outcome}"
    book = state.setdefault("consensus_candidates", {})
    for old_key, old_record in list(book.items()):
        updated_ts = float(old_record.get("updated_ts") or 0)
        if updated_ts and now_ts - updated_ts > CONSENSUS_TTL_SECONDS:
            book.pop(old_key, None)
    record = book.setdefault(
        key,
        {"wallets": [], "first_seen_at": _now_kst(), "first_seen_ts": now_ts},
    )
    wallets = {str(w) for w in record.get("wallets") or [] if w}
    # 재시작/정책 전환 전 포지션도 이미 확보한 확인 신호로 포함한다.
    for pos in state.get("open_positions") or []:
        if (
            pos.get("condition_id") == condition
            and int(pos.get("outcome_index") or 0) == outcome
        ):
            wallets.update(str(w) for w in pos.get("consensus_wallets") or [] if w)
            if pos.get("wallet"):
                wallets.add(str(pos.get("wallet")))
    if sig.get("wallet"):
        wallets.add(str(sig.get("wallet")))
    record["wallets"] = sorted(wallets)
    record["updated_at"] = _now_kst()
    record["updated_ts"] = now_ts
    record["title"] = sig.get("title") or record.get("title")
    return len(wallets), record["wallets"]


def opposite_consensus_wallets(
    state: dict[str, Any], sig: dict[str, Any]
) -> list[str]:
    """같은 시장의 반대 방향 고래가 이미 관측됐으면 그 지갑 목록을 반환."""
    condition = str(sig.get("condition_id") or "")
    outcome = int(sig.get("outcome_index") or 0)
    wallets: set[str] = set()
    for key, record in (state.get("consensus_candidates") or {}).items():
        prefix, _, raw_outcome = str(key).rpartition(":")
        if prefix != condition:
            continue
        try:
            other_outcome = int(raw_outcome)
        except ValueError:
            continue
        if other_outcome != outcome:
            wallets.update(str(w) for w in record.get("wallets") or [] if w)
    return sorted(wallets)


def early_exit_position(
    pos: dict[str, Any],
    state: dict[str, Any],
    *,
    reason: str,
    market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """해소 전 조기 청산 (고래 축소/플립 추종). 시세 매도 가정."""
    if market is None:
        market = _fetch_market_state(gamma_market_id=pos.get("gamma_market_id"))
    price = _current_price(market, int(pos["outcome_index"])) if market else None
    if price is None or price <= 0:
        price = float(pos.get("entry_price") or 0.5)
    exit_price = max(min(price * (1 - COPY_SLIPPAGE), 0.999), 0.001)
    entry = max(float(pos.get("entry_price") or 0.5), 1e-6)
    bet = float(pos.get("bet_usd") or BET_USD)
    shares = bet / entry
    proceeds = shares * exit_price
    pnl = proceeds - bet
    won = pnl > 0
    result = {
        **pos,
        "event": "settled",
        "settled_at": _now_kst(),
        "settled_ts": _now(),
        "won": won,
        "pnl_usd": round(pnl, 2),
        "pnl_pct": round(pnl / bet, 4) if bet else 0.0,
        "exit_price": round(exit_price, 4),
        "settle_reason": reason,
        "early_exit": True,
    }
    _append_jsonl(JOURNAL_FILE, result)
    if not pos.get("is_shadow"):
        state["bankroll"] = float(state.get("bankroll") or INITIAL_BANKROLL) + pnl
    # 재진입 가능하도록 signaled 해제
    wstate = state.get("wallets", {}).get(pos.get("wallet") or "")
    if isinstance(wstate, dict):
        wstate.setdefault("signaled", {})[_pos_key(pos)] = False
    print(
        f"  [paper-exit] {reason} {(pos.get('title') or '')[:36]} "
        f"pnl={pnl:+.2f} @ {exit_price:.3f}"
    )
    return result


def follow_whale_exits(state: dict[str, Any]) -> int:
    """고래가 해당 outcome 순매수를 크게 줄이면 우리도 조기 청산."""
    if HOLD_TO_RESOLUTION:
        return 0

    remaining: list[dict] = []
    closed = 0
    thresh = MIN_NET_USDC * EXIT_NET_FRAC
    for pos in state.get("open_positions") or []:
        wstate = state.get("wallets", {}).get(pos.get("wallet") or "") or {}
        net = float((wstate.get("net_usdc") or {}).get(_pos_key(pos), 0.0))
        # 순매수가 임계 미만 = 고래가 대부분 정리/매도
        if net < thresh:
            early_exit_position(pos, state, reason="whale_exit_reduce")
            closed += 1
            # 지갑 통계 (해소와 동일하게 live_n 갱신은 settle_positions 경로만 — 여기선 paper 간단)
            if not pos.get("is_shadow"):
                ws = state.get("wallets", {}).get(pos.get("wallet") or "")
                if isinstance(ws, dict):
                    ws["live_n"] = int(ws.get("live_n") or 0) + 1
                    if float(pos.get("bet_usd") or 0) and True:
                        # won flag set in early_exit via journal only; recompute from last
                        pass
            continue
        remaining.append(pos)
    state["open_positions"] = remaining
    return closed


def open_paper_positions(signals: list[dict[str, Any]], state: dict[str, Any]) -> int:
    opened = 0
    for sig in signals:
        market = _gamma_market_by_condition(sig["condition_id"])
        if not market or market.get("closed"):
            continue  # 이미 종료됐으면 카피 의미 없음

        existing_market = [
            pos for pos in state.get("open_positions") or []
            if pos.get("condition_id") == sig["condition_id"]
        ]
        consensus_rank, consensus_wallets = register_consensus_signal(state, sig)
        opposite_wallets = opposite_consensus_wallets(state, sig)
        if opposite_wallets:
            _append_jsonl(JOURNAL_FILE, {
                "event": "consensus_conflict_blocked",
                "wallet": sig.get("wallet"),
                "condition_id": sig.get("condition_id"),
                "outcome_index": sig.get("outcome_index"),
                "consensus_wallets": consensus_wallets,
                "opposite_wallets": opposite_wallets,
                "at": _now_kst(),
                "title": market.get("question"),
            })
            continue
        if consensus_rank > CONSENSUS_MAX_WHALES:
            continue
        same_direction_open = [
            pos for pos in existing_market
            if int(pos.get("outcome_index") or 0) == int(sig["outcome_index"])
        ]
        # 1·2·3번째 고유 고래마다 한 번씩만 증분 진입한다.
        target_tickets = consensus_rank
        if len(same_direction_open) >= target_tickets:
            continue

        same_wallet_market = [
            pos for pos in state.get("open_positions") or []
            if pos.get("wallet") == sig["wallet"]
            and pos.get("condition_id") == sig["condition_id"]
        ]
        if HOLD_TO_RESOLUTION and same_wallet_market:
            continue

        # 같은 지갑·같은 마켓 반대 outcome 보유 중이면 먼저 청산 (플립 추종)
        still_open: list[dict] = []
        for pos in state.get("open_positions") or []:
            if (
                pos.get("wallet") == sig["wallet"]
                and pos.get("condition_id") == sig["condition_id"]
                and int(pos.get("outcome_index")) != int(sig["outcome_index"])
            ):
                early_exit_position(
                    pos, state, reason="whale_flip_opposite", market=market,
                )
            else:
                still_open.append(pos)
        state["open_positions"] = still_open

        # 이미 같은 키 오픈이면 스킵
        if any(
            p.get("wallet") == sig["wallet"]
            and p.get("condition_id") == sig["condition_id"]
            and int(p.get("outcome_index")) == int(sig["outcome_index"])
            for p in state.get("open_positions") or []
        ):
            continue

        price = _current_price(market, sig["outcome_index"])
        if price is None or price <= 0 or price >= 1:
            continue
        entry_price = min(price * (1 + COPY_SLIPPAGE), 0.999)
        bet_usd = consensus_bet_usd(consensus_rank)
        if bet_usd <= 0:
            continue
        is_shadow = state["wallets"].get(sig["wallet"], {}).get("status") == "suspended"

        pos = {
            "wallet": sig["wallet"],
            "gamma_market_id": market.get("id"),
            "condition_id": sig["condition_id"],
            "outcome_index": sig["outcome_index"],
            "title": market.get("question", sig["title"]),
            "slug": sig["slug"],
            "entry_price": round(entry_price, 4),
            "bet_usd": bet_usd,
            "consensus_rank": consensus_rank,
            "consensus_wallets": consensus_wallets,
            "signal_policy": "scaled_whale_consensus_v3",
            "exit_policy": (
                "hold_to_resolution_v1" if HOLD_TO_RESOLUTION else "follow_whale_exit_v1"
            ),
            "is_shadow": is_shadow,  # True면 중단된 지갑의 회복 관찰용, 실제 잔고에 반영 안 함
            "opened_at": _now_kst(),
            "opened_ts": _now(),
        }
        state["open_positions"].append(pos)
        _append_jsonl(JOURNAL_FILE, {**pos, "event": "opened"})
        opened += 1
    return opened


def settle_positions(state: dict[str, Any]) -> int:
    remaining = []
    settled = 0
    for pos in state.get("open_positions", []):
        market = _fetch_market_state(gamma_market_id=pos["gamma_market_id"])
        if not market:
            remaining.append(pos)
            continue
        winner_idx = _resolved_outcome(market)
        if winner_idx is None:
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
            "pnl_pct": round(pnl / pos["bet_usd"], 4),
        }
        _append_jsonl(JOURNAL_FILE, result)
        if not is_shadow:
            state["bankroll"] = state.get("bankroll", INITIAL_BANKROLL) + pnl
        settled += 1

        wstate = state["wallets"].get(pos["wallet"])
        if wstate is not None:
            wstate["live_n"] = wstate.get("live_n", 0) + 1
            wstate["live_wins"] = wstate.get("live_wins", 0) + (1 if won else 0)
            _update_wallet_status(pos["wallet"], wstate)

    state["open_positions"] = remaining
    return settled


def _wallet_z(wstate: dict[str, Any]) -> float | None:
    n = wstate.get("live_n", 0)
    if n <= 0:
        return None
    expected = wstate.get("expected_win_rate", 0.5)
    wins = wstate.get("live_wins", 0)
    se = math.sqrt(n * expected * (1 - expected))
    if se <= 0:
        return None
    return (wins - n * expected) / se


def _update_wallet_status(wallet: str, wstate: dict[str, Any]) -> None:
    """누적(라이브 추종 시작 이후) z-score로 중단/재활성화 판단.
    고정 크기 최근 N건 창 대신 누적치를 쓰는 이유: 표본이 작을 때(초반) 우연한 몇 건의
    불운으로 성급히 중단되는 걸 막고, 표본이 쌓일수록 판단이 통계적으로 견고해지게 하기 위함.
    중단 기준(SUSPEND_Z)과 재활성화 기준(REACTIVATE_Z)에 히스테리시스를 둬서 경계값 근처에서
    상태가 계속 왔다갔다(flapping)하는 것도 방지한다."""
    n = wstate.get("live_n", 0)
    if n < SUSPEND_MIN_SETTLED:
        return
    z = _wallet_z(wstate)
    if z is None:
        return

    if wstate["status"] == "active" and z <= SUSPEND_Z:
        wstate["status"] = "suspended"
        wstate["suspended_at"] = _now_kst()
        wstate["suspended_reason"] = (
            f"누적 {n}건 z={z:.2f} (기준 {SUSPEND_Z}) — 백테스트 기대승률 대비 유의미하게 저조. "
            f"중단 중에도 그림자추적은 계속하며, z가 {REACTIVATE_Z} 이상으로 회복되면 자동 재활성화됨."
        )
        _append_jsonl(JOURNAL_FILE, {
            "event": "wallet_suspended", "wallet": wallet, "z": round(z, 2),
            "reason": wstate["suspended_reason"], "at": wstate["suspended_at"],
        })
        send_review(
            f"⛔ <b>[Polymarket 고래 추종 자동중단]</b>\n"
            f"지갑 {escape(wallet[:14])}...\n{escape(wstate['suspended_reason'])}"
        )
    elif wstate["status"] == "suspended" and z >= REACTIVATE_Z:
        wstate["status"] = "active"
        wstate["reactivated_at"] = _now_kst()
        _append_jsonl(JOURNAL_FILE, {
            "event": "wallet_reactivated", "wallet": wallet, "z": round(z, 2),
            "at": wstate["reactivated_at"],
        })
        send_review(
            f"✅ <b>[Polymarket 고래 추종 자동재개]</b>\n"
            f"지갑 {escape(wallet[:14])}... 누적 {n}건 z={z:.2f}로 회복돼 카피 재개."
        )


def _fmt_usd(v: float) -> str:
    return f"${v:+.2f}"


def _fmt_pct(v: float) -> str:
    return f"{v*100:+.2f}%"


def build_report(state: dict[str, Any]) -> str:
    from polymarket_whale_insights import build_insight_comments

    rows = _read_jsonl(JOURNAL_FILE)
    settled_all = [r for r in rows if r.get("event") == "settled"]
    settled = [r for r in settled_all if not r.get("is_shadow")]
    shadow = [r for r in settled_all if r.get("is_shadow")]
    wins = [r for r in settled if r.get("won")]
    pnl = sum(float(r.get("pnl_usd") or 0) for r in settled)
    win_rate = len(wins) / len(settled) if settled else 0.0
    largest_win = max(
        (float(r.get("pnl_usd") or 0) for r in settled),
        default=0.0,
    )
    policy_settled = [
        r for r in settled
        if r.get("exit_policy") == "hold_to_resolution_v1"
    ]
    policy_pnl = sum(float(r.get("pnl_usd") or 0) for r in policy_settled)

    active = [w for w, s in state["wallets"].items() if s["status"] == "active"]
    suspended = [w for w, s in state["wallets"].items() if s["status"] == "suspended"]

    lines = [
        f"🐋 <b>[Polymarket 고래 카피트레이딩 Paper 리포트]</b> — {datetime.now(KST).strftime('%m/%d %H:%M KST')}",
        "",
        f"가상 잔고: ${state.get('bankroll', INITIAL_BANKROLL):.2f} (시작 ${INITIAL_BANKROLL:.0f})",
        f"누적 정산(실카피): {len(settled)}건 | 승률 {win_rate:.1%} | 누적 PnL {_fmt_usd(pnl)}",
        f"최대 단일 수익 {_fmt_usd(largest_win)} | 해당 1건 제외 누적 {_fmt_usd(pnl - largest_win)}",
        f"오픈 포지션: {len(state.get('open_positions', []))}개"
        + (f" (그림자추적 정산 {len(shadow)}건 별도)" if shadow else ""),
        "청산 정책: "
        + ("최초 진입 후 시장 결과까지 보유" if HOLD_TO_RESOLUTION else "고래 축소·플립 추종"),
        f"진입 정책: 1명째 ${consensus_bet_usd(1):.2f} | "
        f"2명째 +${consensus_bet_usd(2):.2f} | "
        f"3명째 +${consensus_bet_usd(3):.2f} | 시장 최대 "
        f"${sum(CONSENSUS_TIER_USD.values()):.2f} | 동일 고래 반복·반대방향 차단",
        f"신규 보유정책 표본: 정산 {len(policy_settled)}건 | PnL {_fmt_usd(policy_pnl)}",
        f"추적 지갑: 활성 {len(active)}명 / 중단(회복관찰중) {len(suspended)}명",
    ]
    if suspended:
        lines.append("")
        lines.append("⛔ 중단된 지갑 (그림자추적으로 회복 시 자동재개):")
        for w in suspended:
            ws = state["wallets"][w]
            lines.append(
                f"• {escape(w[:14])}... 누적 z={_wallet_z(ws):.2f} "
                f"— {escape(ws.get('suspended_reason',''))}"
            )

    recent = settled[-5:]
    if recent:
        lines.append("")
        lines.append("최근 정산(실카피):")
        for r in recent:
            lines.append(
                f"• {escape(str(r.get('title'))[:40])} — "
                f"{'승' if r.get('won') else '패'} {_fmt_usd(float(r.get('pnl_usd') or 0))}"
            )

    try:
        cfg_whales = _load_config().get("whales") or []
    except Exception:
        cfg_whales = []
    comments = build_insight_comments(
        mode="PAPER",
        state=state,
        settled=settled_all,
        config_whales=cfg_whales,
        bet_fraction=BET_FRACTION,
    )
    lines.append("")
    lines.append("💡 <b>개선·건의 코멘트</b> (자동 제안, 모수 변경은 수동)")
    for c in comments:
        lines.append(f"• {escape(c)}")

    lines += ["", "※ 실주문 없음. 슬리피지 3%p 가정 반영, 실제 체결과는 차이가 있을 수 있습니다."]
    return "\n".join(lines)


def _maybe_send_report(state: dict[str, Any], force: bool = False) -> bool:
    if not force and _now() - float(state.get("last_report_time") or 0.0) < REPORT_INTERVAL_SECONDS:
        return False
    delivered = send_review(build_report(state))
    if delivered:
        state["last_report_time"] = _now()
    return delivered


def run_once(report_now: bool = False) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    state = _load_state()
    policy_name = (
        "hold_to_resolution_v1" if HOLD_TO_RESOLUTION else "follow_whale_exit_v1"
    )
    if state.get("exit_policy") != policy_name:
        state["exit_policy"] = policy_name
        state["exit_policy_started_at"] = _now_kst()

    settled = settle_positions(state)
    signals = scan_wallets(state)
    # 고래 축소 청산 추종 → 그다음 신규/플립 진입
    exited = follow_whale_exits(state)
    opened = open_paper_positions(signals, state)

    state["last_scan"] = {
        "time": _now_kst(),
        "signals": len(signals),
        "opened": opened,
        "settled": settled,
        "whale_exits": exited,
        "exit_policy": policy_name,
        "signal_policy": "scaled_whale_consensus_v3",
        "consensus_candidates": len(state.get("consensus_candidates") or {}),
    }

    reported = _maybe_send_report(state, force=report_now)
    _save_state(state)

    return {
        "signals": len(signals), "opened": opened, "settled": settled,
        "open_positions": len(state["open_positions"]), "reported": reported,
        "bankroll": state.get("bankroll"),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-now", action="store_true", help="Send a Telegram report now")
    parser.add_argument("--json", action="store_true", help="Print machine-readable result")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    result = run_once(report_now=args.report_now)
    if args.json:
        print(json.dumps(_json_safe(result), ensure_ascii=False))
    else:
        print(f"[PolymarketWhalePaper] {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
