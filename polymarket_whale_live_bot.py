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
import csv
import io
import json
import os
import sys
import time
import warnings
import zipfile
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
    quote_buy_usd,
    smoke_test as clob_smoke,
)
from publisher import send_review

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

STATE_FILE = ROOT / "polymarket_whale_live_state.json"
JOURNAL_FILE = ROOT / "polymarket_whale_live_journal.jsonl"
DISCOVERY_WATCHLIST_FILE = ROOT / "polymarket_wallet_watchlist.json"
DISCOVERY_RADAR_STATE_FILE = ROOT / "polymarket_wallet_radar_state.json"

# fast_directional_mirror_v5: 총자산의 1%, 티켓당 최대 $10(수수료 포함)으로
# 단일 고래도 추종하되, 시장별 양방향 재고는 제외하고 누적 순매수 단계만 복제한다.
INITIAL_BANKROLL = float(os.getenv("POLYMARKET_LIVE_BANKROLL", "800") or 800)
BET_FRACTION = float(os.getenv("POLYMARKET_LIVE_RISK_FRACTION", "0.01") or 0.01)
BET_USD_CAP = float(os.getenv("POLYMARKET_LIVE_MARKET_RISK_CAP", "10") or 10)
MAX_OPEN = int(os.getenv("POLYMARKET_LIVE_SAFE_MAX_OPEN", "30") or 30)
MAX_DAILY_LOSS_FRACTION = float(
    os.getenv("POLYMARKET_LIVE_MAX_DAILY_LOSS_FRACTION", "0.08") or 0.08
)
MAX_POLICY_DRAWDOWN_FRACTION = float(
    os.getenv("POLYMARKET_LIVE_MAX_POLICY_DRAWDOWN_FRACTION", "0.20") or 0.20
)
MAX_DAILY_TURNOVER_FRACTION = float(
    os.getenv("POLYMARKET_LIVE_MAX_DAILY_TURNOVER_FRACTION", "1.00") or 1.00
)
# crypto 7% fee schedule까지 덮는 보수적 현금비용 버퍼. 주문 amount는 이 버퍼를
# 제외한 금액으로 줄여 실제 차감액이 시장별 risk budget을 넘지 않게 한다.
TAKER_FEE_BUFFER_PCT = float(
    os.getenv("POLYMARKET_LIVE_TAKER_FEE_BUFFER_PCT", "0.07") or 0.07
)
LIVE_MIN_CONSENSUS = int(
    os.getenv("POLYMARKET_LIVE_MIN_CONSENSUS", "1") or 1
)
LIVE_CONSENSUS_WINDOW_SECONDS = int(
    os.getenv("POLYMARKET_LIVE_CONSENSUS_WINDOW_SECONDS", "120") or 120
)
MAX_SOURCE_PRICE_DRIFT = float(
    os.getenv("POLYMARKET_LIVE_MAX_SOURCE_PRICE_DRIFT", "0.03") or 0.03
)
LIVE_SIGNAL_POLICY = "fast_directional_mirror_v5"
COPY_SLIPPAGE = float(os.getenv("POLYMARKET_WHALE_COPY_SLIPPAGE", "0.03") or 0.03)
MIN_NET_USDC = float(
    os.getenv("POLYMARKET_LIVE_MIN_DIRECTIONAL_USDC", "250") or 250
)
# 지갑 장기 기대승률보다 진입가격이 최소 5%p 낮아야 카피한다.
# 0.999 같은 고가 추격은 한 번의 패배가 다수의 미세수익을 지우므로 차단한다.
MIN_ENTRY_EDGE = float(os.getenv("POLYMARKET_LIVE_MIN_ENTRY_EDGE", "0.05") or 0.05)
MAX_ENTRY_PRICE = float(os.getenv("POLYMARKET_LIVE_MAX_ENTRY_PRICE", "0.85") or 0.85)
MAX_COMMITTED_FRACTION = float(
    os.getenv("POLYMARKET_LIVE_SAFE_MAX_COMMITTED_FRACTION", "0.30") or 0.30
)
MAX_SIGNAL_AGE_SECONDS = int(
    os.getenv("POLYMARKET_LIVE_MAX_SIGNAL_AGE_SECONDS", "120") or 120
)
MAX_MARKET_COMMITTED_FRACTION = float(
    os.getenv("POLYMARKET_LIVE_MAX_MARKET_COMMITTED_FRACTION", "0.02") or 0.02
)
MAX_MARKET_COMMITTED_USD = float(
    os.getenv("POLYMARKET_LIVE_MAX_MARKET_COMMITTED_USD", "20") or 20
)
WHALE_REDUCTION_EXIT_RATIO = float(
    os.getenv("POLYMARKET_LIVE_WHALE_REDUCTION_EXIT_RATIO", "0.50") or 0.50
)
MAINTENANCE_INTERVAL_SECONDS = int(
    os.getenv("POLYMARKET_LIVE_MAINTENANCE_INTERVAL_SECONDS", "60") or 60
)
PREQUOTE_REQUIRED = (
    os.getenv("POLYMARKET_LIVE_PREQUOTE_REQUIRED", "true").strip().lower()
    == "true"
)
PREQUOTE_COST_BUFFER_PCT = float(
    os.getenv("POLYMARKET_LIVE_PREQUOTE_COST_BUFFER_PCT", "0.015") or 0.015
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

EDGE_FILTER_ENABLED = env_bool("POLYMARKET_LIVE_EDGE_FILTER_ENABLED", True)


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
    """수수료를 포함한 시장별 최대 현금위험 예산."""
    bank = float((state or {}).get("bankroll") or INITIAL_BANKROLL)
    raw = min(max(bank, 0.0) * BET_FRACTION, BET_USD_CAP)
    return int(raw * 10_000) / 10_000


def _order_notional_for_risk_budget(risk_budget: float) -> float:
    """최악 수수료를 더해도 ``risk_budget`` 안에 드는 FOK BUY amount."""
    raw = max(float(risk_budget), 0.0) / (
        1 + max(TAKER_FEE_BUFFER_PCT, 0.0)
    )
    # 반올림으로 위험예산을 몇 micro-dollar라도 넘기지 않게 아래로 절삭한다.
    return int(raw * 10_000) / 10_000


def _ensure_safe_policy_state(state: dict[str, Any]) -> bool:
    """정책 전환 시 과거 activity/합의 누계를 새 정책과 완전히 분리한다."""
    if state.get("signal_policy") == LIVE_SIGNAL_POLICY:
        return False
    equity = float(state.get("bankroll") or 0)
    now_ts = _now()
    for wstate in (state.get("wallets") or {}).values():
        # 커서는 유지해 과거 거래를 재생하지 않고, 부분 수집으로 오염된 방향성
        # 누계만 비운다. 새 정책 이후 완전 페이지로 들어온 체결만 사용한다.
        wstate["net_usdc"] = {}
        wstate["net_shares_v5"] = {}
        wstate["signaled"] = {}
        wstate["market_flow_v2"] = {}
        wstate["directional_signal_levels_v5"] = {}
        wstate["activity_seen_v2"] = []
        wstate.pop("classification_v2", None)
    state["consensus_candidates"] = {}
    state["live_consensus_v4"] = {}
    state["pending_mirror_signals_v5"] = {}
    state["signal_policy"] = LIVE_SIGNAL_POLICY
    state["signal_policy_started_at"] = _now_kst()
    state["signal_policy_started_ts"] = now_ts
    state["risk_policy_v4"] = {
        "policy_started_at": _now_kst(),
        "policy_started_ts": now_ts,
        "policy_start_equity": equity,
        "day": _today(),
        "day_start_equity": equity,
        "day_turnover_usd": 0.0,
        "current_equity": equity,
        "daily_pnl": 0.0,
        "policy_pnl": 0.0,
        "halted": False,
        "halt_reason": "",
    }
    _append({
        "event": "live_policy_started",
        "signal_policy": LIVE_SIGNAL_POLICY,
        "equity_baseline": round(equity, 6),
        "at": _now_kst(),
    })
    return True


def _update_equity_risk_state(state: dict[str, Any], equity: float) -> dict[str, Any]:
    risk = state.get("risk_policy_v4")
    if not isinstance(risk, dict):
        return {}
    today = _today()
    if risk.get("day") != today:
        risk["day"] = today
        risk["day_start_equity"] = equity
        risk["day_turnover_usd"] = 0.0
        risk["halted"] = False
        risk["halt_reason"] = ""
    day_start = max(float(risk.get("day_start_equity") or equity), 0.0)
    policy_start = max(float(risk.get("policy_start_equity") or equity), 0.0)
    daily_pnl = equity - day_start
    policy_pnl = equity - policy_start
    daily_loss = max(0.0, -daily_pnl)
    policy_loss = max(0.0, -policy_pnl)
    reason = ""
    if day_start > 0 and daily_loss >= day_start * MAX_DAILY_LOSS_FRACTION:
        reason = (
            f"현 정책 일손실 {daily_loss:.2f} >= "
            f"{MAX_DAILY_LOSS_FRACTION:.1%} ({day_start * MAX_DAILY_LOSS_FRACTION:.2f})"
        )
    elif policy_start > 0 and policy_loss >= policy_start * MAX_POLICY_DRAWDOWN_FRACTION:
        reason = (
            f"현 정책 누적낙폭 {policy_loss:.2f} >= "
            f"{MAX_POLICY_DRAWDOWN_FRACTION:.1%} ({policy_start * MAX_POLICY_DRAWDOWN_FRACTION:.2f})"
        )
    risk["current_equity"] = equity
    risk["daily_pnl"] = daily_pnl
    risk["policy_pnl"] = policy_pnl
    risk["daily_loss"] = daily_loss
    risk["policy_loss"] = policy_loss
    if reason:
        risk["halted"] = True
        risk["halt_reason"] = reason
    else:
        risk["halted"] = False
        risk["halt_reason"] = ""
    state["daily_loss"] = daily_loss
    state["trading_halted"] = bool(risk.get("halted"))
    state["trading_halt_reason"] = str(risk.get("halt_reason") or "")
    return risk


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


def _risk_ok(state: dict[str, Any], cash_risk: float) -> tuple[bool, str]:
    # dry-run/shadow 포지션은 실주문 한도에 넣지 않음 (LIVE 전환 직후 막히던 문제)
    open_n = len([
        p for p in state.get("open_positions") or []
        if not p.get("is_shadow") and p.get("live") is True and not p.get("dry_run")
    ])
    # MAX_OPEN<=0 → paper와 동일하게 동시 한도 없음
    if MAX_OPEN > 0 and open_n >= MAX_OPEN:
        return False, f"동시 실포지션 {open_n}>={MAX_OPEN}"
    risk = state.get("risk_policy_v4") or {}
    if risk.get("halted"):
        return False, str(risk.get("halt_reason") or "현 정책 손실중단")
    # 손실 뒤에도 초기 시드로 한도를 부풀리지 않고 실제 확정잔고 기준으로 축소한다.
    bank = float(state.get("bankroll") or 0)
    if bank <= 0:
        return False, "확정 bankroll 0 이하"
    if cash_risk > min(bank * BET_FRACTION, BET_USD_CAP) + 1e-6:
        return False, "단건이 현 정책 시장위험 예산 초과"
    committed = sum(
        float(p.get("cash_risk_usd") or p.get("bet_usd") or 0)
        for p in state.get("open_positions") or []
        if not p.get("is_shadow") and p.get("live") is True and not p.get("dry_run")
    )
    if committed + cash_risk > bank * MAX_COMMITTED_FRACTION:
        return False, (
            f"총투입 ${committed + cash_risk:.2f} > bankroll "
            f"{MAX_COMMITTED_FRACTION:.0%} (${bank * MAX_COMMITTED_FRACTION:.2f})"
        )
    turnover = float(risk.get("day_turnover_usd") or 0)
    turnover_cap = bank * MAX_DAILY_TURNOVER_FRACTION
    if turnover + cash_risk > turnover_cap:
        return False, (
            f"일거래 ${turnover + cash_risk:.2f} > equity "
            f"{MAX_DAILY_TURNOVER_FRACTION:.0%} (${turnover_cap:.2f})"
        )
    return True, ""


def _entry_edge_ok(state: dict[str, Any], sig: dict, entry_price: float) -> tuple[bool, str]:
    if not EDGE_FILTER_ENABLED:
        return True, ""
    wallets = [str(w) for w in sig.get("consensus_wallets") or [] if w]
    if not wallets:
        wallets = [str(sig.get("wallet") or "")]
    expected_values = [
        float(((state.get("wallets") or {}).get(wallet) or {}).get("expected_win_rate") or 0.5)
        for wallet in wallets
    ]
    expected = min(expected_values) if expected_values else 0.5
    fee_adjusted_entry = min(entry_price * (1 + TAKER_FEE_BUFFER_PCT), 0.999)
    max_allowed = min(MAX_ENTRY_PRICE, expected - MIN_ENTRY_EDGE)
    if max_allowed <= 0 or fee_adjusted_entry > max_allowed:
        return False, (
            f"수수료포함 진입 {fee_adjusted_entry:.3f} > 허용 {max_allowed:.3f} "
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
    """v5는 고래의 반대편 매수·보유축소를 감지해 기존 포지션을 정리한다."""
    remaining: list[dict] = []
    closed = 0
    thresh = paper.MIN_NET_USDC * paper.EXIT_NET_FRAC
    for pos in state.get("open_positions") or []:
        wstate = state.get("wallets", {}).get(pos.get("wallet") or "") or {}
        key = f"{pos.get('condition_id')}:{pos.get('outcome_index')}"
        net = float((wstate.get("net_usdc") or {}).get(key, 0.0))
        reason = ""
        if pos.get("signal_policy") == LIVE_SIGNAL_POLICY:
            outcome_index = int(pos.get("outcome_index") or 0)
            flow = (wstate.get("market_flow_v2") or {}).get(
                str(pos.get("condition_id") or "")
            ) or {}
            selected_buy = max(
                float(flow.get(f"buy_{outcome_index}") or 0), 0.0
            )
            opposite_buy = max(
                float(flow.get(f"buy_{1 - outcome_index}") or 0), 0.0
            )
            opposite_ratio = (
                opposite_buy / selected_buy if selected_buy > 0 else 0.0
            )
            current_whale_shares = float(
                (wstate.get("net_shares_v5") or {}).get(key, 0.0)
            )
            entry_whale_shares = max(
                float(pos.get("whale_net_shares_at_entry") or 0), 0.0
            )
            if opposite_ratio >= paper.WHALE_MAX_OPPOSITE_BUY_RATIO:
                reason = "whale_became_two_sided"
            elif (
                entry_whale_shares > 0
                and current_whale_shares
                <= entry_whale_shares * WHALE_REDUCTION_EXIT_RATIO
            ):
                reason = "whale_position_reduced"
        elif not HOLD_TO_RESOLUTION and net < thresh:
            reason = "whale_exit_reduce"

        if reason:
            ok = _live_early_exit(pos, state, reason=reason)
            if ok:
                closed += 1
            else:
                remaining.append(pos)  # 매도 실패 시 유지 후 재시도
            continue
        remaining.append(pos)
    state["open_positions"] = remaining
    return closed


def _register_live_consensus(
    state: dict[str, Any], sig: dict[str, Any]
) -> tuple[int, list[str], list[float], list[str]]:
    """짧은 창 안의 방향별 지갑과 원체결가를 보존하는 신호 장부."""
    now_ts = _now()
    condition = str(sig.get("condition_id") or "")
    outcome = int(sig.get("outcome_index") or 0)
    key = f"{condition}:{outcome}"
    book = state.setdefault("live_consensus_v4", {})
    for old_key, record in list(book.items()):
        updated = float((record or {}).get("updated_ts") or 0)
        if not updated or now_ts - updated > LIVE_CONSENSUS_WINDOW_SECONDS:
            book.pop(old_key, None)

    record = book.setdefault(key, {"signals": {}, "first_seen_ts": now_ts})
    wallet = str(sig.get("wallet") or "")
    record.setdefault("signals", {})[wallet] = {
        "source_trade_ts": float(sig.get("source_trade_ts") or now_ts),
        "source_trade_price": float(sig.get("source_trade_price") or 0),
        "detected_ts": float(sig.get("detected_ts") or now_ts),
    }
    record["updated_ts"] = now_ts
    record["updated_at"] = _now_kst()
    record["title"] = sig.get("title") or record.get("title")
    wallets = sorted(str(w) for w in record["signals"] if w)
    prices = [
        float(row.get("source_trade_price") or 0)
        for row in record["signals"].values()
        if float(row.get("source_trade_price") or 0) > 0
    ]
    opposite_wallets: set[str] = set()
    for other_key, other in book.items():
        prefix, _, raw_outcome = str(other_key).rpartition(":")
        if prefix != condition or raw_outcome == str(outcome):
            continue
        opposite_wallets.update(str(w) for w in (other.get("signals") or {}) if w)
    return len(wallets), wallets, prices, sorted(opposite_wallets)


def _source_price_ok(source_prices: list[float], effective_entry: float) -> tuple[bool, str]:
    valid = [float(price) for price in source_prices if 0 < float(price) < 1]
    if len(valid) < LIVE_MIN_CONSENSUS:
        return False, "고래 원체결가 표본 부족"
    max_source = max(valid)
    allowed = min(max_source * (1 + MAX_SOURCE_PRICE_DRIFT), 0.999)
    if effective_entry > allowed:
        return False, (
            f"고래 체결가 추격초과: 진입 {effective_entry:.3f} > "
            f"허용 {allowed:.3f}"
        )
    return True, ""


def _mirror_signal_key(sig: dict[str, Any]) -> str:
    return ":".join((
        str(sig.get("wallet") or ""),
        str(sig.get("condition_id") or ""),
        str(sig.get("outcome_index") if sig.get("outcome_index") is not None else ""),
        str(int(sig.get("signal_level") or 1)),
    ))


def _remember_pending_mirror_signal(
    state: dict[str, Any], sig: dict[str, Any]
) -> None:
    row = dict(sig)
    row.pop("wallet_classification", None)
    row["pending_since_ts"] = float(row.get("pending_since_ts") or _now())
    state.setdefault("pending_mirror_signals_v5", {})[
        _mirror_signal_key(sig)
    ] = row


def _clear_pending_mirror_signal(
    state: dict[str, Any], sig: dict[str, Any]
) -> None:
    (state.get("pending_mirror_signals_v5") or {}).pop(
        _mirror_signal_key(sig), None
    )


def _fresh_pending_mirror_signals(state: dict[str, Any]) -> list[dict[str, Any]]:
    now_ts = _now()
    book = state.setdefault("pending_mirror_signals_v5", {})
    fresh: list[dict[str, Any]] = []
    for key, row in list(book.items()):
        try:
            source_ts = float(
                row.get("source_trade_ts") or row.get("detected_ts") or 0
            )
        except (TypeError, ValueError):
            source_ts = 0.0
        if source_ts <= 0 or now_ts - source_ts > MAX_SIGNAL_AGE_SECONDS:
            book.pop(key, None)
            continue
        fresh.append(dict(row))
    return fresh


def open_live_positions(signals: list[dict], state: dict[str, Any]) -> int:
    opened = 0
    base_cash_risk_budget = _ticket_usd(state)
    dry = not live_enabled()
    batch_directions: dict[str, set[int]] = {}
    for sig in signals:
        try:
            outcome = int(sig.get("outcome_index"))
        except (TypeError, ValueError):
            continue
        if outcome in {0, 1}:
            batch_directions.setdefault(
                str(sig.get("condition_id") or ""), set()
            ).add(outcome)
    conflicted_conditions = {
        condition for condition, outcomes in batch_directions.items()
        if len(outcomes) > 1
    }

    for sig in signals:
        try:
            outcome_index = int(sig.get("outcome_index"))
        except (TypeError, ValueError):
            outcome_index = -1
        if outcome_index not in {0, 1}:
            _clear_pending_mirror_signal(state, sig)
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked",
                "reason": "invalid_outcome_index",
                "wallet": sig.get("wallet"),
                "condition_id": sig.get("condition_id"),
                "outcome_index": sig.get("outcome_index"),
                "at": _now_kst(),
                "title": sig.get("title"),
            })
            continue
        if str(sig.get("condition_id") or "") in conflicted_conditions:
            _clear_pending_mirror_signal(state, sig)
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked",
                "reason": "same_batch_opposite_direction_conflict",
                "wallet": sig.get("wallet"),
                "condition_id": sig.get("condition_id"),
                "outcome_index": outcome_index,
                "at": _now_kst(),
                "title": sig.get("title"),
            })
            continue

        now_ts = _now()
        raw_source_ts = sig.get("source_trade_ts") or sig.get("detected_ts")
        try:
            source_trade_ts = float(raw_source_ts or now_ts)
        except (TypeError, ValueError):
            source_trade_ts = now_ts
        signal_age = max(now_ts - source_trade_ts, 0.0)
        if signal_age > MAX_SIGNAL_AGE_SECONDS:
            _clear_pending_mirror_signal(state, sig)
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked",
                "reason": "stale_whale_signal",
                "wallet": sig.get("wallet"),
                "condition_id": sig.get("condition_id"),
                "outcome_index": outcome_index,
                "source_trade_ts": source_trade_ts,
                "signal_age_seconds": round(signal_age, 3),
                "max_signal_age_seconds": MAX_SIGNAL_AGE_SECONDS,
                "at": _now_kst(),
                "title": sig.get("title"),
            })
            continue

        wallet_state = (state.get("wallets") or {}).get(str(sig.get("wallet") or "")) or {}
        classification = sig.get("wallet_classification") or wallet_state.get("classification_v2") or {}
        if wallet_state.get("status") != "active":
            _clear_pending_mirror_signal(state, sig)
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked",
                "reason": "wallet_not_active",
                "wallet": sig.get("wallet"),
                "condition_id": sig.get("condition_id"),
                "at": _now_kst(),
                "title": sig.get("title"),
            })
            continue
        wallet_risk_mult = min(
            max(float(wallet_state.get("live_risk_mult") or 1.0), 0.10), 1.0
        )
        cash_risk_budget = int(
            base_cash_risk_budget * wallet_risk_mult * 10_000
        ) / 10_000
        if float(sig.get("opposite_buy_ratio") or 0) >= paper.WHALE_MAX_OPPOSITE_BUY_RATIO:
            _clear_pending_mirror_signal(state, sig)
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked", "reason": "two_sided_whale_exposure",
                "wallet": sig.get("wallet"), "condition_id": sig.get("condition_id"),
                "opposite_buy_ratio": sig.get("opposite_buy_ratio"),
                "at": _now_kst(), "title": sig.get("title"),
            })
            continue

        # 이 지점부터의 실패는 호가·시장별/계좌별 위험한도처럼 잠시 뒤 해소될 수
        # 있으므로 원 고래 체결 후 2분 동안 다음 스캔에서 재시도한다.
        _remember_pending_mirror_signal(state, sig)

        existing_market = [
            pos for pos in state.get("open_positions") or []
            if pos.get("condition_id") == sig.get("condition_id")
            and pos.get("live") is True
            and not pos.get("dry_run")
            and not pos.get("is_shadow")
        ]
        opposite_existing = [
            pos for pos in existing_market
            if int(pos.get("outcome_index") or 0) != outcome_index
        ]
        legacy_existing = [
            pos for pos in existing_market
            if pos.get("signal_policy") != LIVE_SIGNAL_POLICY
        ]
        existing_market_risk = sum(
            float(pos.get("cash_risk_usd") or pos.get("bet_usd") or 0)
            for pos in existing_market
        )
        market_risk_cap = min(
            float(state.get("bankroll") or 0) * MAX_MARKET_COMMITTED_FRACTION,
            MAX_MARKET_COMMITTED_USD,
        )
        if (
            opposite_existing
            or legacy_existing
            or existing_market_risk + cash_risk_budget > market_risk_cap + 1e-6
        ):
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked",
                "reason": (
                    "mirror_opposite_or_legacy_position"
                    if opposite_existing or legacy_existing
                    else "mirror_market_risk_cap"
                ),
                "wallet": sig.get("wallet"),
                "condition_id": sig.get("condition_id"),
                "outcome_index": outcome_index,
                "existing_market_risk": round(existing_market_risk, 6),
                "market_risk_cap": round(market_risk_cap, 6),
                "at": _now_kst(),
                "title": sig.get("title"),
            })
            continue

        consensus_rank, consensus_wallets, source_prices, opposite_wallets = (
            _register_live_consensus(state, sig)
        )
        sig["consensus_wallets"] = consensus_wallets
        sig["consensus_source_prices"] = source_prices
        if opposite_wallets:
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked", "reason": "consensus_opposite_direction_conflict",
                "wallet": sig.get("wallet"), "condition_id": sig.get("condition_id"),
                "consensus_wallets": consensus_wallets,
                "opposite_wallets": opposite_wallets,
                "at": _now_kst(), "title": sig.get("title"),
            })
            continue
        if consensus_rank < LIVE_MIN_CONSENSUS:
            _append({
                "event": "consensus_observed",
                "signal_policy": LIVE_SIGNAL_POLICY,
                "wallet": sig.get("wallet"),
                "condition_id": sig.get("condition_id"),
                "outcome_index": outcome_index,
                "consensus_rank": consensus_rank,
                "consensus_wallets": consensus_wallets,
                "at": _now_kst(),
                "title": sig.get("title"),
            })
            continue

        market = paper._gamma_market_by_condition(sig["condition_id"])
        if not market or market.get("closed"):
            continue

        price = paper._current_price(market, outcome_index)
        if price is None or price <= 0 or price >= 1:
            continue
        entry_price = min(price * (1 + COPY_SLIPPAGE), 0.999)
        bet = _order_notional_for_risk_budget(cash_risk_budget)
        if bet <= 0:
            continue
        ok, why = _risk_ok(state, cash_risk_budget)
        if not ok:
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked", "reason": why, "wallet": sig.get("wallet"),
                "at": _now_kst(), "title": sig.get("title"),
            })
            print(f"  [live-block] {why}")
            continue
        token_id = _token_id_for_outcome(market, outcome_index)
        if not token_id:
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked",
                "reason": "no_token_id",
                "wallet": sig.get("wallet"),
                "condition_id": sig.get("condition_id"),
                "outcome_index": outcome_index,
                "at": _now_kst(),
                "title": market.get("question"),
            })
            continue

        prequote = quote_buy_usd(token_id, bet, max_price=entry_price)
        if not prequote.get("ok") and PREQUOTE_REQUIRED:
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked",
                "reason": "prequote_no_full_fill",
                "error": prequote.get("error"),
                "wallet": sig.get("wallet"),
                "condition_id": sig.get("condition_id"),
                "outcome_index": outcome_index,
                "token_id": token_id,
                "bet_usd": bet,
                "best_ask": prequote.get("best_ask"),
                "fillable_usd": prequote.get("fillable_usd"),
                "max_order_price": round(entry_price, 6),
                "at": _now_kst(),
                "title": market.get("question"),
            })
            continue

        if prequote.get("ok"):
            book_vwap = float(prequote.get("vwap") or entry_price)
            worst_ask = float(prequote.get("worst_ask") or book_vwap)
            order_price_limit = min(
                entry_price,
                worst_ask * (1 + PREQUOTE_COST_BUFFER_PCT),
                0.999,
            )
            effective_entry = min(
                book_vwap * (1 + PREQUOTE_COST_BUFFER_PCT), 0.999
            )
        else:
            book_vwap = entry_price
            order_price_limit = entry_price
            effective_entry = entry_price
        edge_ok, edge_reason = _entry_edge_ok(state, sig, effective_entry)
        if not edge_ok:
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked", "reason": edge_reason,
                "wallet": sig.get("wallet"), "at": _now_kst(),
                "title": market.get("question"),
                "entry_price": round(effective_entry, 4),
                "book_vwap": round(book_vwap, 6),
            })
            print(f"  [live-block] {edge_reason}")
            continue
        source_ok, source_reason = _source_price_ok(source_prices, effective_entry)
        if not source_ok:
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked", "reason": source_reason,
                "wallet": sig.get("wallet"), "condition_id": sig.get("condition_id"),
                "source_prices": source_prices,
                "entry_price": round(effective_entry, 6),
                "at": _now_kst(), "title": market.get("question"),
            })
            continue

        order_res = place_buy_usd(
            token_id, bet, price_hint=order_price_limit, dry_run=dry,
        )
        if not order_res.get("ok"):
            _append({
                "event": "order_failed",
                "error": order_res.get("error"),
                "token_id": token_id,
                "bet_usd": bet,
                "cash_risk_budget": cash_risk_budget,
                "at": _now_kst(),
                "title": market.get("question"),
            })
            print(f"  [order-fail] {order_res.get('error')}")
            continue

        actual_bet = float(order_res.get("filled_usd") or bet)
        actual_shares = float(
            order_res.get("filled_shares")
            or (actual_bet / max(effective_entry, 1e-9))
        )
        actual_entry = float(
            order_res.get("fill_price")
            or effective_entry
        )
        actual_cash_risk = actual_bet * (1 + TAKER_FEE_BUFFER_PCT)
        pos = {
            "wallet": sig["wallet"],
            "gamma_market_id": market.get("id"),
            "condition_id": sig["condition_id"],
            "outcome_index": sig["outcome_index"],
            "token_id": token_id,
            "title": market.get("question", sig.get("title")),
            "slug": sig.get("slug"),
            "entry_price": round(actual_entry, 6),
            "quote_entry_price": round(order_price_limit, 6),
            "gamma_price_guard": round(entry_price, 6),
            "prequote_best_ask": prequote.get("best_ask"),
            "prequote_vwap": prequote.get("vwap"),
            "prequote_worst_ask": prequote.get("worst_ask"),
            "prequote_fillable_usd": prequote.get("fillable_usd"),
            "prequote_cost_buffer_pct": PREQUOTE_COST_BUFFER_PCT,
            "bet_usd": round(actual_bet, 6),
            "cash_risk_usd": round(actual_cash_risk, 6),
            "cash_risk_budget_usd": round(cash_risk_budget, 6),
            "wallet_risk_mult": round(wallet_risk_mult, 4),
            "wallet_promotion_stage": wallet_state.get("promotion_stage", "static_live"),
            "taker_fee_buffer_pct": TAKER_FEE_BUFFER_PCT,
            "consensus_rank": consensus_rank,
            "consensus_wallets": consensus_wallets,
            "consensus_source_prices": [round(v, 6) for v in source_prices],
            "signal_policy": LIVE_SIGNAL_POLICY,
            "position_role": "fast_directional_mirror_entry",
            "signal_level": int(sig.get("signal_level") or 1),
            "whale_net_usdc_at_entry": round(float(sig.get("net_usdc") or 0), 6),
            "whale_net_shares_at_entry": round(float(sig.get("net_shares") or 0), 6),
            "shares_est": round(actual_shares, 6),
            "fill_status": order_res.get("fill_status"),
            "trade_ids": order_res.get("trade_ids") or [],
            "transaction_hashes": order_res.get("transaction_hashes") or [],
            "source_trade_ts": source_trade_ts,
            "source_trade_price": sig.get("source_trade_price"),
            "signal_age_seconds": round(signal_age, 3),
            "exit_policy": "mirror_whale_inventory_v5",
            "is_shadow": False,
            "live": not dry,
            "dry_run": dry,
            "order_id": order_res.get("order_id"),
            "opened_at": _now_kst(),
            "opened_ts": _now(),
        }
        state["open_positions"].append(pos)
        _clear_pending_mirror_signal(state, sig)
        if not dry:
            risk = state.get("risk_policy_v4") or {}
            risk["day_turnover_usd"] = float(risk.get("day_turnover_usd") or 0) + actual_cash_risk
        record = (state.get("live_consensus_v4") or {}).get(
            f"{sig.get('condition_id')}:{outcome_index}"
        )
        if isinstance(record, dict):
            record["executed"] = True
            record["executed_at"] = _now_kst()
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
        bet_usd = float(pos.get("bet_usd") or 0)
        shares = float(pos.get("shares_est") or 0)
        if shares <= 0:
            shares = bet_usd / max(float(pos.get("entry_price") or 0), 1e-9)
        payout = shares if won else 0.0
        pnl = payout - bet_usd
        is_shadow = pos.get("is_shadow", False)
        result = {
            **pos,
            "event": "settled",
            "settled_at": _now_kst(),
            "settled_ts": _now(),
            "won": won,
            "pnl_usd": round(pnl, 2),
            "pnl_pct": round(pnl / bet_usd, 4) if bet_usd else 0,
            "settle_reason": "market_resolved",
            "settlement_source": "gamma_resolution_confirmed_fill",
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


def _fetch_accounting_snapshot() -> dict[str, Any]:
    """공식 equity.csv를 읽어 0가치 패배 토큰까지 포함한 총자산을 반환."""
    wallet = get_wallet_address()
    if not wallet:
        return {"ok": False, "error": "wallet address unavailable"}
    try:
        resp = requests.get(
            "https://data-api.polymarket.com/v1/accounting/snapshot",
            params={"user": wallet},
            timeout=25,
        )
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
            equity_rows = list(csv.DictReader(
                archive.read("equity.csv").decode("utf-8-sig").splitlines()
            ))
            position_rows = list(csv.DictReader(
                archive.read("positions.csv").decode("utf-8-sig").splitlines()
            ))
        if not equity_rows:
            raise ValueError("empty equity.csv")
        row = equity_rows[-1]
        return {
            "ok": True,
            "cash": float(row.get("cashBalance") or 0),
            "position_value": float(row.get("positionsValue") or 0),
            "equity": float(row.get("equity") or 0),
            "valuation_time": row.get("valuationTime"),
            "position_rows": len(position_rows),
            "zero_value_position_rows": sum(
                float(pos.get("size") or 0) > 0
                and float(pos.get("curPrice") or 0) <= 0
                for pos in position_rows
            ),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:180]}


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


def _reconcile_wallet_closed_positions(state: dict[str, Any]) -> int:
    """실지갑에서 사라지고 closed API에 확인된 포지션을 로컬 장부와 합친다.

    positions API에서 잠시 누락된 것만으로는 정산하지 않는다. 같은 토큰의 실제
    closed-position 행이 마지막 로컬 진입 이후에 존재할 때만 수동 매도·외부 청산으로
    확정해 유령 오픈 포지션과 이중 정산을 막는다.
    """
    local_live = [
        p for p in state.get("open_positions") or []
        if p.get("live") is True
        and not p.get("dry_run")
        and not p.get("is_shadow")
        and p.get("token_id")
    ]
    if not local_live:
        return 0

    token_ids = {str(p["token_id"]) for p in local_live}
    portfolio = _fetch_actual_portfolio(token_ids)
    closed = _fetch_actual_closed(token_ids)
    if not portfolio.get("ok") or not closed.get("ok"):
        return 0

    active_assets = {
        str(row.get("asset") or "")
        for row in portfolio.get("positions") or []
        if row.get("asset")
    }
    latest_closed: dict[str, dict[str, Any]] = {}
    for row in closed.get("positions") or []:
        asset = str(row.get("asset") or "")
        if not asset:
            continue
        try:
            row_ts = float(row.get("timestamp") or 0)
        except (TypeError, ValueError):
            row_ts = 0.0
        previous = latest_closed.get(asset)
        try:
            previous_ts = float((previous or {}).get("timestamp") or 0)
        except (TypeError, ValueError):
            previous_ts = 0.0
        if previous is None or row_ts >= previous_ts:
            latest_closed[asset] = row

    by_token: dict[str, list[dict[str, Any]]] = {}
    for pos in local_live:
        by_token.setdefault(str(pos["token_id"]), []).append(pos)

    ledger = state.setdefault("confirmed_close_ledger_v1", {
        "assets": {},
        "closed_positions": 0,
        "realized_pnl": 0.0,
        "started_at": _now_kst(),
    })
    processed = ledger.setdefault("assets", {})
    closed_tokens: set[str] = set()
    reconciled = 0

    for token_id, positions in by_token.items():
        row = latest_closed.get(token_id)
        if token_id in active_assets or row is None:
            continue
        try:
            closed_ts = float(row.get("timestamp") or 0)
        except (TypeError, ValueError):
            closed_ts = 0.0
        latest_open_ts = max(float(p.get("opened_ts") or 0) for p in positions)
        if closed_ts <= 0 or closed_ts + 60 < latest_open_ts:
            continue

        pnl = float(row.get("realizedPnl") or 0)
        total_bought = float(row.get("totalBought") or 0)
        avg_price = float(row.get("avgPrice") or 0)
        fingerprint = f"{closed_ts:.3f}:{pnl:.6f}:{total_bought:.6f}"
        if processed.get(token_id) == fingerprint:
            continue

        local_bet = sum(float(p.get("bet_usd") or 0) for p in positions)
        local_shares = sum(float(p.get("shares_est") or 0) for p in positions)
        actual_cost = avg_price * total_bought
        if actual_cost <= 0:
            actual_cost = local_bet
        base = positions[0]
        wallets = sorted({str(p.get("wallet") or "?") for p in positions})
        settled_at = datetime.fromtimestamp(closed_ts, KST).isoformat()
        _append({
            **base,
            "event": "settled",
            "settled_at": settled_at,
            "settled_ts": closed_ts,
            "won": pnl > 0.005,
            "pnl_usd": round(pnl, 6),
            "pnl_pct": round(pnl / actual_cost, 6) if actual_cost else 0.0,
            "settle_reason": "wallet_position_closed",
            "settlement_source": "polymarket_closed_positions_api",
            "manual_or_external_close": True,
            "fill_status": "confirmed_closed",
            "wallets": wallets,
            "local_ticket_count": len(positions),
            "local_ticket_bet_usd": round(local_bet, 6),
            "local_ticket_shares": round(local_shares, 6),
            "actual_avg_price": avg_price,
            "actual_total_bought": total_bought,
            "actual_cost_usd": round(actual_cost, 6),
            "actual_exit_value_usd": round(actual_cost + pnl, 6),
        })
        for pos in positions:
            wstate = (state.get("wallets") or {}).get(pos.get("wallet") or "")
            if isinstance(wstate, dict):
                key = f"{pos.get('condition_id')}:{pos.get('outcome_index')}"
                wstate.setdefault("signaled", {})[key] = False
        processed[token_id] = fingerprint
        ledger["closed_positions"] = int(ledger.get("closed_positions") or 0) + 1
        ledger["realized_pnl"] = float(ledger.get("realized_pnl") or 0) + pnl
        ledger["last_reconciled_at"] = _now_kst()
        closed_tokens.add(token_id)
        reconciled += 1

    if closed_tokens:
        state["open_positions"] = [
            p for p in state.get("open_positions") or []
            if not (
                p.get("live") is True
                and not p.get("dry_run")
                and not p.get("is_shadow")
                and str(p.get("token_id") or "") in closed_tokens
            )
        ]
    return reconciled


def _sync_actual_accounting(state: dict[str, Any]) -> dict[str, Any]:
    """공식 accounting snapshot 총자산으로 손익·손실중단 기준을 동기화."""
    token_ids = _bot_token_ids()
    closed = _fetch_actual_closed(token_ids)
    snapshot = _fetch_accounting_snapshot()
    if snapshot.get("ok"):
        cash = float(snapshot.get("cash") or 0)
        position_value = float(snapshot.get("position_value") or 0)
        equity = float(snapshot.get("equity") or (cash + position_value))
    else:
        # snapshot 장애 시 봇 저널 토큰만 합산하면 수동/과거 보유분을 빠뜨려
        # equity를 과소계상한다. 공개 positions 전체를 합산해 대체한다.
        portfolio = _fetch_actual_portfolio(None)
        cash = get_usdc_balance_approx()
        if not portfolio.get("ok") or cash < 0:
            return {"ok": False, "error": "actual accounting snapshot unavailable"}
        position_value = float(portfolio.get("value") or 0)
        equity = cash + position_value
    realized = float(closed.get("realized") or 0) if closed.get("ok") else 0.0
    previous = (state.get("actual_accounting") or {}).get("realized")
    all_time_pnl = equity - INITIAL_BANKROLL
    state["bankroll"] = equity
    state["actual_accounting"] = {
        k: v for k, v in closed.items() if k not in {"ok", "positions"}
    } | {
        "closed_api_sample_unreliable": True,
        "principal": INITIAL_BANKROLL,
        "cash": cash,
        "position_value": position_value,
        "equity": equity,
        "all_time_pnl": all_time_pnl,
        "accounting_source": (
            "polymarket_accounting_snapshot"
            if snapshot.get("ok") else "cash_plus_positions_fallback"
        ),
        "valuation_time": snapshot.get("valuation_time"),
        "position_rows": snapshot.get("position_rows"),
        "zero_value_position_rows": snapshot.get("zero_value_position_rows"),
        "synced_at": _now_kst(),
    }
    _update_equity_risk_state(state, equity)
    if previous is None or abs(float(previous) - realized) > 0.005:
        _append({
            "event": "accounting_reconciled",
            "at": _now_kst(),
            "actual_realized_pnl": round(realized, 6),
            "actual_closed": int(closed.get("count") or 0),
            "equity": round(equity, 6),
            "all_time_pnl": round(all_time_pnl, 6),
        })
    return {"ok": True, **state["actual_accounting"]}


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
    local_port = _local_open_summary(state)
    token_ids = _bot_token_ids(rows)
    actual_port = _fetch_actual_portfolio(token_ids)
    actual_closed = _fetch_actual_closed(token_ids)
    policy_token_ids = {
        str(r.get("token_id"))
        for r in rows
        if r.get("event") == "opened"
        and r.get("live") is True
        and r.get("signal_policy") == LIVE_SIGNAL_POLICY
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
    risk = state.get("risk_policy_v4") or {}
    confirmed_ledger = state.get("confirmed_close_ledger_v1") or {}
    cash_est = float(accounting.get("cash") or (bank - float(port.get("value") or 0)))
    equity_est = float(accounting.get("equity") or (cash_est + float(port.get("value") or 0)))
    all_time_pnl = equity_est - INITIAL_BANKROLL
    sync_note = ""
    if actual_port.get("ok") and int(actual_port.get("count") or 0) != int(local_port["count"]):
        sync_note = (
            f"⚠️ 실지갑 {actual_port['count']}건 ≠ 로컬 {int(local_port['count'])}건 — "
            "closed-position API 기준 자동 동기화 대기 중"
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
        f"• 불완전 closed-positions 참고표본 {display_n}건: "
        f"{display_wins}수익 / {display_losses}손실 | PnL ${display_pnl:+.2f}",
        "• 위 표본은 0가치 패배를 누락하므로 손익·손실정지 계산에 사용하지 않음",
        f"• 현재 미실현 <b>${float(port.get('unrealized') or 0):+.2f}</b> | "
        f"v5 오늘 equity 손익 ${float(risk.get('daily_pnl') or 0):+.2f}",
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
        f"• 수동·외부 청산 동기화 {int(confirmed_ledger.get('closed_positions') or 0)}건 | "
        f"확인 PnL ${float(confirmed_ledger.get('realized_pnl') or 0):+.2f}",
        "• 청산정책: 고래 보유축소 또는 반대 outcome 매수 시 포지션 정리",
        f"• v5 진입: 15초 병렬 스캔, 활성 고래의 한쪽 순매수 "
        f"${MIN_NET_USDC:.0f} 단계마다 추종",
        f"• 시장 현금위험≤min(equity {BET_FRACTION:.1%}, ${BET_USD_CAP:.0f}) "
        f"(현재 ${bet:.2f}) | 주문액은 수수료 {TAKER_FEE_BUFFER_PCT:.0%} 차감",
        f"• 시장 누적위험≤min(equity {MAX_MARKET_COMMITTED_FRACTION:.0%}, "
        f"${MAX_MARKET_COMMITTED_USD:.0f}); 양방향 시장·반대 신호 차단",
        "• maker형 지갑 전체를 복제하지 않고 한쪽 재고 시장만 진입",
        (
            f"• v5 상환 API 참고표본: {int(actual_policy.get('count') or 0)}건 "
            f"({int(actual_policy.get('wins') or 0)}승/"
            f"{int(actual_policy.get('losses') or 0)}패) | "
            f"PnL ${float(actual_policy.get('realized') or 0):+.2f}"
            if actual_policy.get("ok")
            else "• v5 실체결: 공개 정산내역 조회 실패"
        ),
        f"• 가격조건: 수수료 포함 엣지 {MIN_ENTRY_EDGE:.0%}p + "
        f"고래 원체결가 추격≤{MAX_SOURCE_PRICE_DRIFT:.0%}",
        f"• 주문검증: 원 고래 체결 {MAX_SIGNAL_AGE_SECONDS // 60}분 이내 + "
        f"실제 CLOB 전액 사전견적(호가변동 여유 {PREQUOTE_COST_BUFFER_PCT:.1%})",
        f"• 총투입≤equity {MAX_COMMITTED_FRACTION:.0%} | "
        f"일회전≤{MAX_DAILY_TURNOVER_FRACTION:.0%} | "
        f"일손실 {MAX_DAILY_LOSS_FRACTION:.0%}/v5낙폭 {MAX_POLICY_DRAWDOWN_FRACTION:.0%}에서 완전중단",
        f"• v5 상태: {'⛔ 중단 — ' + str(risk.get('halt_reason') or '') if risk.get('halted') else '✅ 진입 가능'}",
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
    """고정 config와 전진검증을 통과한 discovery 지갑을 동기화한다.

    discovery 지갑은 watchlist의 ``live_approved``만 허용한다. paper 후보가 실수로
    실주문 경로에 들어가지 않으며, 승인에서 빠진 동적 지갑은 즉시 retired 된다.
    """
    cfg = paper._load_config()
    paper_st = paper._load_state()
    static_whales = list(cfg.get("whales") or [])
    static_addresses = {
        str(row.get("wallet") or "").lower() for row in static_whales
        if row.get("wallet")
    }
    try:
        watchlist = json.loads(DISCOVERY_WATCHLIST_FILE.read_text(encoding="utf-8"))
    except Exception:
        watchlist = {}
    approved = [
        {
            **row,
            "wallet": str(row.get("wallet") or "").lower(),
            "discovery_managed": True,
        }
        for row in (watchlist.get("live_approved") or [])
        if row.get("wallet")
    ]
    approved_addresses = {row["wallet"] for row in approved}
    blocked_legacy_addresses = {
        str(row.get("wallet") or "").lower()
        for row in (watchlist.get("blocked_live") or [])
        if row.get("wallet")
    }
    try:
        radar_state = json.loads(
            DISCOVERY_RADAR_STATE_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        radar_state = {}

    for w in static_whales + approved:
        addr = w.get("wallet")
        if not addr:
            continue
        addr = str(addr).lower()
        if addr not in state["wallets"]:
            src = (
                (radar_state.get("wallets") or {}).get(addr)
                if w.get("discovery_managed")
                else (paper_st.get("wallets") or {}).get(addr)
            ) or {}
            state["wallets"][addr] = {
                "status": src.get("status", "active"),
                "expected_win_rate": w.get("expected_win_rate", 0.5),
                "last_seen_ts": int(src.get("last_seen_ts") or _now()),
                "net_usdc": dict(src.get("net_usdc") or {}),
                "net_shares_v5": dict(src.get("net_shares_v5") or {}),
                "signaled": dict(src.get("signaled") or {}),
                "directional_signal_levels_v5": dict(
                    src.get("directional_signal_levels_v5") or {}
                ),
                "market_flow_v2": dict(src.get("market_flow_v2") or {}),
                "activity_seen_v2": list(src.get("activity_seen_v2") or []),
                "live_wins": int(src.get("live_wins") or 0),
                "live_n": int(src.get("live_n") or 0),
                "discovery_managed": bool(w.get("discovery_managed")),
                "live_risk_mult": float(w.get("live_risk_mult") or 1.0),
                "promotion_stage": w.get("stage", "static_live"),
            }
            if src.get("classification_v2"):
                state["wallets"][addr]["classification_v2"] = dict(
                    src.get("classification_v2") or {}
                )
        elif w.get("discovery_managed"):
            current = state["wallets"][addr]
            current["discovery_managed"] = True
            current["expected_win_rate"] = w.get(
                "expected_win_rate", current.get("expected_win_rate", 0.5)
            )
            if current.get("status") == "discovery_retired":
                current["status"] = "active"
            current["live_risk_mult"] = float(w.get("live_risk_mult") or 1.0)
            current["promotion_stage"] = w.get("stage", "live_approved")

    for addr, wstate in state.get("wallets", {}).items():
        if addr in static_addresses and addr in blocked_legacy_addresses:
            if wstate.get("status") != "legacy_validation_rejected":
                wstate["status"] = "legacy_validation_rejected"
                wstate["legacy_validation_rejected_at"] = _now_kst()
            continue
        if (
            addr in static_addresses
            and addr not in blocked_legacy_addresses
            and wstate.get("status") == "legacy_validation_rejected"
        ):
            wstate["status"] = "active"
            wstate["legacy_validation_reactivated_at"] = _now_kst()
        if (
            wstate.get("discovery_managed")
            and addr not in approved_addresses
            and addr not in static_addresses
            and wstate.get("status") != "discovery_retired"
        ):
            wstate["status"] = "discovery_retired"
            wstate["discovery_retired_at"] = _now_kst()


def _prune_invalid_outcome_state(state: dict[str, Any]) -> int:
    """과거에 쌓인 outcome 999/sentinel을 신호·합의 상태에서 1회 제거한다."""
    if state.get("invalid_outcome_pruned_v1"):
        return 0

    def valid_key(key: Any) -> bool:
        _prefix, sep, raw_outcome = str(key).rpartition(":")
        return bool(sep) and raw_outcome in {"0", "1"}

    removed = 0
    for wstate in (state.get("wallets") or {}).values():
        for field in ("net_usdc", "signaled"):
            rows = wstate.get(field)
            if not isinstance(rows, dict):
                continue
            for key in list(rows):
                if not valid_key(key):
                    rows.pop(key, None)
                    removed += 1
    consensus = state.get("consensus_candidates")
    if isinstance(consensus, dict):
        for key in list(consensus):
            if not valid_key(key):
                consensus.pop(key, None)
                removed += 1
    state["invalid_outcome_pruned_v1"] = {
        "at": _now_kst(),
        "removed": removed,
    }
    _append({
        "event": "state_reconciled",
        "reason": "invalid_outcome_pruned",
        "removed": removed,
        "at": _now_kst(),
    })
    return removed


def run_once(report_now: bool = False) -> dict[str, Any]:
    # paper 모듈 상수/설정 정렬
    paper.MIN_NET_USDC = MIN_NET_USDC
    paper.COPY_SLIPPAGE = COPY_SLIPPAGE

    state = _load_state()
    _ensure_wallets_from_config(state)
    invalid_outcomes_pruned = _prune_invalid_outcome_state(state)
    state["mode"] = "live" if live_enabled() else "dry_run"
    policy_name = "mirror_whale_inventory_v5"
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
    maintenance_due = (
        state.get("signal_policy") != LIVE_SIGNAL_POLICY
        or not state.get("actual_accounting")
        or _now() - float(state.get("last_maintenance_ts") or 0)
        >= MAINTENANCE_INTERVAL_SECONDS
    )
    wallet_closes_reconciled = 0
    settled = 0
    if maintenance_due:
        wallet_closes_reconciled = _reconcile_wallet_closed_positions(state)
        settled = settle_positions(state)
        actual_accounting = _sync_actual_accounting(state)
        if actual_accounting.get("ok"):
            state["last_maintenance_ts"] = _now()
    else:
        cached = state.get("actual_accounting") or {}
        actual_accounting = {"ok": bool(cached.get("equity") is not None), **cached}
    policy_started = _ensure_safe_policy_state(state)
    if policy_started:
        _update_equity_risk_state(state, float(state.get("bankroll") or 0))
    if not actual_accounting.get("ok"):
        risk = state.setdefault("risk_policy_v4", {})
        risk["halted"] = True
        risk["halt_reason"] = "실계좌 accounting snapshot 조회 실패"
        state["trading_halted"] = True
        state["trading_halt_reason"] = risk["halt_reason"]
    # 임의 지갑의 체결을 실시간으로 알려주는 공개 WebSocket은 없으므로 Data API를
    # 병렬 폴링한다. 지갑 전체가 maker여도 시장별 한쪽 재고만 골라낼 수 있게 한다.
    scanned_signals = paper.scan_wallets(
        state,
        include_suspended=False,
        block_market_maker_wallets=False,
        repeat_directional_steps=True,
        parallel_fetch=True,
    )
    pending_signals = _fresh_pending_mirror_signals(state)
    merged_signals = {
        _mirror_signal_key(sig): sig
        for sig in pending_signals + scanned_signals
    }
    signals = list(merged_signals.values())
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
        "wallet_closes_reconciled": wallet_closes_reconciled,
        "whale_exits": whale_exits,
        "exit_policy": policy_name,
        "signal_policy": state["signal_policy"],
        "policy_started": policy_started,
        "maintenance_due": maintenance_due,
        "consensus_candidates": len(state.get("consensus_candidates") or {}),
        "actual_accounting_ok": bool(actual_accounting.get("ok")),
        "invalid_outcomes_pruned": invalid_outcomes_pruned,
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
        "wallet_closes_reconciled": wallet_closes_reconciled,
        "open_positions": len(state.get("open_positions") or []),
        "bankroll": state.get("bankroll"),
        "live_enabled": live_enabled(),
        "bet_usd": _ticket_usd(state),
        "bet_fraction": BET_FRACTION,
        "closed_api_reference_pnl": (
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
